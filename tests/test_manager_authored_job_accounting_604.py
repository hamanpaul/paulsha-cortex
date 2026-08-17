"""#604：gate ledger 與 exit sentinel 的**作者**必須是 Manager，不是被隔離的 job。

## 這張票在修什麼

登記表資產 `gate-ledger`（＝Manager 的 dispatch log 目錄，同時放 `<slice>.gates.json`
與 `<slice>.exit`）宣告 `writers=(MANAGER,)`。Phase 2b 之前這條宣告只是**同 UID 的
巧合**——兩個檔實際上都由 `launcher.build_wrapper_script` 產生的 wrapper 在 **job 進程
內**寫。2026-08-17 OS 隔離實機上線（`PSC_JOB_RUNNER=systemd-template`，job 真的以
`uid=cortex-builder` 跑）之後，同一段程式碼同時有兩個問題：

- **信任面**：exit sentinel 是 `dispatcher.poll_headless_done` 的第一判準、gate ledger
  是 `terminal_contract.authorize_terminal` 採信 `passed` 的唯一背書，兩者卻由被驗方
  自己的進程寫。#540 的 gate acceptance chain 要求 model 既不能自證成功、也不能自證
  失敗。
- **可行性**：那個目錄在 Phase 2b 是 `0700 cortex-manager`，且**不在** builder 模板
  unit 的 `ReadWritePaths=` 內（`ProtectSystem=strict`）。job 寫進去必然 EROFS。

本檔把修法的三條性質釘成不變式：

1. **結構事實**（`StructuralAuthorshipTests`）——permgen 導出的權限與 job unit 的
   `ReadWritePaths=` 證明 builder 對該目錄零寫入權。這是「job 不可能是合法作者」的
   機械證據，也是下面兩條的前提。
2. **降權模式下 job 不再被要求寫**（`DegradedWrapperTests`）——job 側 wrapper 內不得
   再出現 sentinel 重導向或 gate ledger writer；sentinel 改由 Manager 側的 exit 記帳
   shell 寫（`job_runner.build_manager_exit_recorder_argv`）。
3. **採信端拒絕外來作者**（`ForeignAuthorRejectionTests`）——即使檔案真的出現在該
   位置，只要擁有者不是 Manager，ledger 一律 fail closed、sentinel 一律不採信。

`direct` 模式（同 UID）的行為逐字不變，由 `DirectModeZeroRegressionTests` 釘住。

全程不跑 systemd、不建帳號、不 chown（測試進程不具 root）；「外來作者」以替換
`terminal_contract._effective_uid` 模擬——被比對的一端是檔案系統上真實的 `st_uid`。
"""
from __future__ import annotations

import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import paulsha_cortex.coordinator.job_runner as job_runner
import paulsha_cortex.coordinator.launcher as launcher_module
from paulsha_cortex.coordinator import dispatcher, terminal_contract
from paulsha_cortex.coordinator.job_runner import JobRunnerError
from paulsha_cortex.coordinator.launcher import SubprocessLauncher
from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.registry import Principal, asset_by_id

_ISOLATED_AGENTS_ROOT = tempfile.mkdtemp(prefix="psc-agents-root-")

_BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/var/lib/cortex-manager",
    # conftest 的 `_clear_runtime_env` 把 PSC_AGENTS_ROOT 指向 per-test 暫存目錄，
    # 但本檔的 launch 測試以 `clear=True` 重建整份 environ（要驗的就是白名單本身），
    # 那層保護因此被清掉。顯式帶上一個 per-process 暫存根：`launcher.launch()` 會在
    # coordinator 樹底下建這個 job 的成果 bundle spool（#623），少了它會落到 operator
    # 的真實 `$HOME`——正是 conftest #303 註記要防的事。
    "PSC_AGENTS_ROOT": _ISOLATED_AGENTS_ROOT,
    "LANG": "en_US.UTF-8",
    "PSC_REPO_ROOT": "/opt/cortex/venv/lib/python3/site-packages",
}


# ---------------------------------------------------------------------------
# 1：結構事實——builder 對 gate-ledger 目錄零寫入權
# ---------------------------------------------------------------------------

class StructuralAuthorshipTests(unittest.TestCase):
    """登記表宣告 ⟷ permgen 產出 ⟷ job unit 三者一致：job 不可能是合法作者。"""

    def setUp(self) -> None:
        self.scheme = permgen.DEFAULT_SCHEME
        self.plan = permgen.generate_plan(self.scheme)
        self.entry = self.plan.by_id("gate-ledger")

    def test_registry_declares_manager_as_the_only_writer(self) -> None:
        asset = asset_by_id("gate-ledger")
        self.assertEqual(asset.writers, (Principal.MANAGER,))
        self.assertFalse(asset.headless_writable())

    def test_permgen_gives_no_job_account_any_write_access(self) -> None:
        writable = self.plan.all_writable_accounts(self.entry)
        self.assertEqual(writable, {self.scheme.durable_state_owner})
        for principal in (Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER):
            account = self.scheme.resolve(principal)
            if account == self.scheme.durable_state_owner:
                continue
            self.assertNotIn(account, writable, principal)
        # group/other 一個寫入位都沒有——連「同 group 就寫得到」這條路都不存在。
        self.assertEqual(self.entry.mode & 0o077, 0)

    def test_builder_job_unit_read_write_paths_never_cover_the_ledger_dir(self) -> None:
        """`ProtectSystem=strict` 下，沒被 RWP 涵蓋＝唯讀掛載＝EROFS。

        這條是本票診斷的核心證據：job 側的 wrapper 就算照舊執行那兩行寫入，在實機上
        也只會失敗。要求 job 去寫一個它結構上寫不到的檔，得到的不是「安全」而是
        「每個 job 都因為沒有 sentinel 被判 failed」。
        """

        unit = permgen.build_job_unit(self.scheme, principal=Principal.BUILDER)
        ledger_dir = permgen.asset_paths()["gate-ledger"]
        for rwp in unit.read_write_paths:
            self.assertFalse(
                permgen._is_within(ledger_dir, rwp),
                f"job unit 不該對 gate-ledger 目錄可寫: {rwp}",
            )

    def test_exit_sentinel_and_gate_ledger_share_the_same_asset(self) -> None:
        """兩個檔同目錄、同性質，因此同一個登記表資產必須同時涵蓋它們。"""

        log_path = f"{permgen.asset_paths()['gate-ledger']}/psc-1/psc-1.jsonl"
        sentinel = str(dispatcher.exit_sentinel_path(log_path))
        ledger = str(terminal_contract.gate_ledger_path(log_path))
        ledger_dir = permgen.asset_paths()["gate-ledger"]
        self.assertTrue(permgen._is_within(sentinel, ledger_dir), sentinel)
        self.assertTrue(permgen._is_within(ledger, ledger_dir), ledger)
        derived = asset_by_id("gate-ledger").derived_in
        self.assertTrue(
            any("exit_sentinel_path" in item for item in derived),
            f"sentinel 的推導點必須登記在 gate-ledger 資產上: {derived}",
        )


# ---------------------------------------------------------------------------
# 2：降權模式下 job 側 wrapper 不再寫 sentinel／ledger
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, *, pid: int = 6060, exit_status: int | None = None) -> None:
        self.pid = pid
        self._exit_status = exit_status

    def poll(self) -> int | None:
        return self._exit_status


class _RecordingPopen:
    def __init__(self, *, proc: _FakeProc | None = None) -> None:
        self.calls: list[dict] = []
        self._proc = proc or _FakeProc()

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        return self._proc

    @property
    def call(self) -> dict:
        return self.calls[0]


class _nested:
    def __init__(self, managers) -> None:
        self._managers = list(managers)

    def __enter__(self):
        self._entered = [m.__enter__() for m in self._managers]
        return self._entered

    def __exit__(self, *exc_info):
        for manager in reversed(self._managers):
            manager.__exit__(*exc_info)
        return False


def _seams(*, template: bool):
    patches = [
        mock.patch.object(
            job_runner.shutil,
            "which",
            return_value="/usr/bin/systemctl" if template else "/usr/bin/systemd-run",
        ),
        mock.patch.object(job_runner, "_systemd_booted", return_value=True),
        mock.patch.object(job_runner, "_account_exists", return_value=True),
        mock.patch.object(job_runner, "_group_exists", return_value=True),
    ]
    if template:
        patches += [
            mock.patch.object(job_runner, "_unit_file_installed", return_value=True),
            mock.patch.object(job_runner, "_is_executable", return_value=True),
            mock.patch.object(job_runner, "_unit_is_active", return_value=False),
        ]
    return patches


def _launch(
    *,
    mode: str,
    popen: _RecordingPopen,
    slice_id: str = "psc-0604-demo",
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """在受控 seam 下跑一次 launch，回傳現場路徑。"""

    original = launcher_module.subprocess.Popen
    launcher_module.subprocess.Popen = popen
    try:
        with tempfile.TemporaryDirectory() as root:
            spool = str(Path(root) / "job-specs")
            Path(spool).mkdir(parents=True, exist_ok=True)
            env = {
                **_BASE_ENV,
                job_runner.JOB_RUNNER_ENV: mode,
                job_runner.JOB_SPEC_SPOOL_ENV: spool,
                **(extra_env or {}),
            }
            log_dir = str(Path(root) / "logs")
            template = mode == job_runner.RUNNER_SYSTEMD_TEMPLATE
            with mock.patch.dict(os.environ, env, clear=True):
                with _nested(_seams(template=template)):
                    SubprocessLauncher("codex").launch(
                        slice_id=slice_id,
                        prompt="PROMPT",
                        worktree=root,
                        log_dir=log_dir,
                    )
            specs = sorted(Path(spool).glob("*.json"))
            return {
                "log_dir": str(Path(log_dir).resolve()),
                "sentinel": str(Path(log_dir).resolve() / f"{slice_id}.exit"),
                "ledger": str(Path(log_dir).resolve() / f"{slice_id}.gates.json"),
                "spec": specs[0].read_text(encoding="utf-8") if specs else "",
            }
    finally:
        launcher_module.subprocess.Popen = original


def _recorder_parts(argv: list[str]) -> tuple[list[str], str]:
    """`(client argv, sentinel)`——同時釘住 Manager 側 exit 記帳 shell 的形狀。"""

    assert argv[:2] == ["bash", "-c"], argv
    head, sep, tail = argv[2].partition("; rc=$?; ")
    assert sep, argv[2]
    assert tail.startswith('printf %s "$rc" > '), tail
    assert tail.endswith('; exit "$rc"'), tail
    written = tail[len('printf %s "$rc" > ') : -len('; exit "$rc"')]
    return shlex.split(head), shlex.split(written)[0]


class DegradedWrapperTests(unittest.TestCase):
    """降權模式：job 側寫入面歸零，sentinel 的寫者換成 Manager。"""

    def test_template_mode_job_command_never_touches_the_manager_log_dir(self) -> None:
        popen = _RecordingPopen()
        paths = _launch(mode=job_runner.RUNNER_SYSTEMD_TEMPLATE, popen=popen)
        spec = json.loads(paths["spec"])
        job_script = " ".join(str(item) for item in spec["command"])
        # job 真正會執行的命令列裡，不得出現這兩個**證據**落點。
        self.assertNotIn(paths["sentinel"], job_script)
        self.assertNotIn(paths["ledger"], job_script)
        self.assertNotIn('printf %s "$?"', job_script)
        self.assertNotIn("paulsha_cortex.coordinator.gate_ledger", job_script)
        # 註：codex 的 `-o <log_dir>/last.json`（`launcher.build_codex_argv`）仍指向
        # 同一個目錄。它**不是**證據面（沒有任何採信路徑讀它，見 `grep last.json`：
        # 只有 planning_runtime 的暫存目錄版本被讀），因此不在本票範圍內；但它在
        # Phase 2b 同樣會 EROFS，屬 executor argv 面的獨立缺口，另票處理。

    def test_template_mode_sentinel_is_written_by_the_manager_side_shell(self) -> None:
        popen = _RecordingPopen()
        paths = _launch(mode=job_runner.RUNNER_SYSTEMD_TEMPLATE, popen=popen)
        client, sentinel = _recorder_parts(popen.call["argv"])
        self.assertEqual(sentinel, paths["sentinel"])
        self.assertEqual(client[0], "/usr/bin/systemctl")
        # 那層 shell 用的是 Manager 自己的 env（polkit 可能要查呼叫端 session），
        # 因此它跑在 Manager 的 uid 下——這正是「作者是 Manager」的來源。
        self.assertIn("PATH", popen.call["env"])

    def test_systemd_run_mode_gets_the_same_treatment(self) -> None:
        popen = _RecordingPopen()
        paths = _launch(mode=job_runner.RUNNER_SYSTEMD_RUN, popen=popen)
        client, sentinel = _recorder_parts(popen.call["argv"])
        self.assertEqual(sentinel, paths["sentinel"])
        self.assertEqual(client[0], "/usr/bin/systemd-run")
        job_script = client[client.index("--") + 1 :][2]
        self.assertNotIn(paths["sentinel"], job_script)
        self.assertNotIn("paulsha_cortex.coordinator.gate_ledger", job_script)

    def test_degraded_launch_never_asks_the_job_to_run_gates(self) -> None:
        """gate 執行面尚未搬走，但**絕不**留在 builder 進程裡自證。

        後續票要把 gate 重跑放到一個既非 builder、也非 Manager 的執行身分下
        （直接放進 Manager 進程會讓 builder 掌控的 `conftest.py` 取得
        `cortex-manager` 的任意程式碼執行）。在那之前降權模式不產生 ledger，
        build 卡照 `require_ledger` fail closed。
        """

        launcher = SubprocessLauncher("codex")
        degraded = {
            **_BASE_ENV,
            job_runner.JOB_RUNNER_ENV: job_runner.RUNNER_SYSTEMD_TEMPLATE,
        }
        with mock.patch.dict(os.environ, degraded, clear=True):
            self.assertFalse(launcher._should_run_gates(dict(_BASE_ENV)))

    def test_manager_exit_recorder_rejects_relative_sentinel(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            job_runner.build_manager_exit_recorder_argv(
                client_argv=["systemctl", "start"], sentinel="logs/x.exit"
            )
        self.assertEqual(ctx.exception.diagnostic.reason, "job-runner-exit-recorder-invalid")

    def test_manager_exit_recorder_really_records_the_client_status(self) -> None:
        """不是比對字串——真的跑一次 bash，確認寫下的就是 client 的 exit code。"""

        import subprocess

        with tempfile.TemporaryDirectory() as d:
            sentinel = str(Path(d) / "s.exit")
            argv = job_runner.build_manager_exit_recorder_argv(
                client_argv=["sh", "-c", "exit 3"], sentinel=sentinel
            )
            proc = subprocess.run(argv, capture_output=True)
            self.assertEqual(proc.returncode, 3)
            self.assertEqual(Path(sentinel).read_text(encoding="utf-8"), "3")


class ManagerAuthoredStartConfirmationTests(unittest.TestCase):
    """記帳 shell 一定會寫 sentinel，因此起動確認不能再拿它當判準。"""

    def test_start_failure_is_still_detected_when_the_sentinel_exists(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sentinel = str(Path(d) / "s.exit")
            Path(sentinel).write_text("1", encoding="utf-8")  # 記帳 shell 已寫下
            with self.assertRaises(JobRunnerError) as ctx:
                job_runner.confirm_template_instance_started(
                    process=_FakeProc(exit_status=1),
                    sentinel=sentinel,
                    unit="cortex-job@demo.service",
                    account="cortex-builder",
                    timeout_ms=0,
                    manager_authored_sentinel=True,
                )
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-template-instance-start-failed"
        )

    def test_successful_short_lived_job_is_not_a_start_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sentinel = str(Path(d) / "s.exit")
            Path(sentinel).write_text("0", encoding="utf-8")
            job_runner.confirm_template_instance_started(
                process=_FakeProc(exit_status=0),
                sentinel=sentinel,
                unit="cortex-job@demo.service",
                account="cortex-builder",
                timeout_ms=0,
                manager_authored_sentinel=True,
            )

    def test_legacy_job_authored_semantics_are_untouched(self) -> None:
        """未傳旗標時＝既有語意（sentinel 存在即視為真的跑過），零回歸。"""

        with tempfile.TemporaryDirectory() as d:
            sentinel = str(Path(d) / "s.exit")
            Path(sentinel).write_text("1", encoding="utf-8")
            job_runner.confirm_transient_unit_started(
                process=_FakeProc(exit_status=1),
                sentinel=sentinel,
                unit="cortex-job-demo.service",
                account="cortex-builder",
                timeout_ms=0,
            )


# ---------------------------------------------------------------------------
# 3：採信端拒絕外來作者
# ---------------------------------------------------------------------------

def _as_foreign_reader():
    """讓本行程「看起來」是另一個 uid——測試檔案的 st_uid 因此變成外來作者。

    刻意不 chown（測試不具 root）：被比對的一端仍是檔案系統上真實的 owner，
    只有比對基準被換掉，判定路徑本身逐字是生產路徑。
    """

    return mock.patch.object(
        terminal_contract, "_effective_uid", return_value=os.getuid() + 4242
    )


def _passing_ledger(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": terminal_contract.GATE_LEDGER_SCHEMA_VERSION,
                "kind": terminal_contract.GATE_LEDGER_KIND,
                "slice_id": "psc-0604",
                "gates": [
                    {
                        "name": "pytest",
                        "command": "python3 -m pytest",
                        "exit_code": 0,
                        "status": "passed",
                        "detail": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _passing_envelope() -> terminal_contract.TerminalEnvelope:
    return terminal_contract.validate_envelope(
        {
            "schema_version": terminal_contract.TERMINAL_SCHEMA_VERSION,
            "kind": "workflow-card",
            "status": "passed",
            "payload": {},
            "gate_evidence": [{"name": "pytest", "status": "passed"}],
        }
    )


class ForeignAuthorRejectionTests(unittest.TestCase):
    def test_foreign_authored_ledger_is_refused_even_when_it_says_everything_passed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "psc-0604.gates.json"
            _passing_ledger(ledger)
            with _as_foreign_reader():
                with self.assertRaises(terminal_contract.TerminalContractError) as ctx:
                    terminal_contract.read_gate_ledger(ledger)
        self.assertEqual(ctx.exception.reason, "gate-ledger-foreign-author")

    def test_foreign_authored_ledger_fails_the_whole_acceptance_chain(self) -> None:
        """#540 的鏈條：ledger 不可信 → `passed` 拿不到授權，且不得靜默降級。"""

        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "psc-0604.gates.json"
            _passing_ledger(ledger)
            with _as_foreign_reader():
                with self.assertRaises(terminal_contract.TerminalContractError) as ctx:
                    terminal_contract.authorize_terminal(
                        _passing_envelope(),
                        ledger_path=ledger,
                        require_ledger=True,
                        expected_gate_names={"pytest"},
                    )
        self.assertEqual(ctx.exception.reason, "gate-ledger-foreign-author")

    def test_manager_authored_ledger_is_accepted(self) -> None:
        """零回歸：direct 模式（同 UID）下這條檢查永遠不會命中。"""

        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "psc-0604.gates.json"
            _passing_ledger(ledger)
            payload, digest = terminal_contract.read_gate_ledger(ledger)
            self.assertEqual(payload["kind"], terminal_contract.GATE_LEDGER_KIND)
            self.assertEqual(len(digest), 64)
            authorization = terminal_contract.authorize_terminal(
                _passing_envelope(),
                ledger_path=ledger,
                require_ledger=True,
                expected_gate_names={"pytest"},
            )
            self.assertEqual(authorization.status, "passed")

    def test_foreign_authored_exit_sentinel_is_not_believed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log_path = str(Path(d) / "psc-0604.jsonl")
            Path(log_path).write_text("", encoding="utf-8")
            sentinel = dispatcher.exit_sentinel_path(log_path)
            sentinel.write_text("0", encoding="utf-8")  # builder 自報「我成功了」
            self.assertEqual(dispatcher._read_exit_sentinel(log_path), 0)
            with _as_foreign_reader():
                self.assertIsNone(dispatcher._read_exit_sentinel(log_path))

    def test_symlinked_sentinel_is_not_believed(self) -> None:
        """擁有者看起來對，但內容來自別處——同樣不採信。"""

        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "elsewhere"
            real.write_text("0", encoding="utf-8")
            log_path = str(Path(d) / "psc-0604.jsonl")
            Path(log_path).write_text("", encoding="utf-8")
            sentinel = dispatcher.exit_sentinel_path(log_path)
            sentinel.symlink_to(real)
            self.assertIsNone(dispatcher._read_exit_sentinel(log_path))

    def test_poll_headless_done_fails_closed_on_a_foreign_sentinel(self) -> None:
        """整條 harvest 路徑：不採信 ≠ 卡住，而是 fail-closed 記成 failed。"""

        class _Registry:
            def __init__(self, job: dict) -> None:
                self.job = job
                self.finalized: dict | None = None

            def get_job(self, job_id: str) -> dict:
                return self.job

            def update_headless_result(self, job_id, *, status, exit_code, provider_outcome):
                self.finalized = {"status": status, "exit_code": exit_code}
                return self.finalized

        with tempfile.TemporaryDirectory() as d:
            log_path = str(Path(d) / "psc-0604.jsonl")
            Path(log_path).write_text("", encoding="utf-8")
            dispatcher.exit_sentinel_path(log_path).write_text("0", encoding="utf-8")
            registry = _Registry({"job_id": "j1", "pid": 4242, "log_path": log_path})
            disp = dispatcher.Dispatcher(registry, None, None)
            with _as_foreign_reader():
                result = disp.poll_headless_done("j1", pid_alive=lambda pid: False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 1)


# ---------------------------------------------------------------------------
# 4：direct 模式零回歸
# ---------------------------------------------------------------------------

class DirectModeZeroRegressionTests(unittest.TestCase):
    def test_direct_wrapper_still_writes_sentinel_and_gate_ledger(self) -> None:
        script = launcher_module.build_wrapper_script(
            inner_argv=["codex", "exec"],
            sentinel="/tmp/s.exit",
            ledger="/tmp/s.gates.json",
            worktree="/tmp/wt",
            repo_root="/repo",
            run_gates=True,
        )
        self.assertIn('printf %s "$?" > /tmp/s.exit', script)
        self.assertIn("paulsha_cortex.coordinator.gate_ledger", script)

    def test_write_sentinel_false_removes_only_that_segment(self) -> None:
        without = launcher_module.build_wrapper_script(
            inner_argv=["codex", "exec"],
            sentinel="/tmp/s.exit",
            ledger="/tmp/s.gates.json",
            worktree="/tmp/wt",
            repo_root="/repo",
            run_gates=False,
            write_sentinel=False,
        )
        self.assertEqual(without, "codex exec")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
