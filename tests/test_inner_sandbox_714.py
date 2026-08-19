"""#714：executor 自帶的內層沙箱 × systemd 外層加固面，以及 `last.json` 的落點。

## 這張票在修什麼

`#712`／PR #713 落地後 builder job 真的跑了 30 分鐘，然後 19 行 job log 裡 **5 個
`command_execution` 全部 `status: failed`**，逐字都是

    bwrap: Can't read /proc/sys/kernel/overflowuid: No such file or directory

模型最後合理地回 `needs_human`，Manager 端落成 `card-terminal-schema-retry-exhausted`
——**症狀離病因四層遠**。

## 0819 實機逐條量測（`psc_run_under` 全量導出，D13）

保留 bubblewrap（票上的路線 A）要付**四條**放寬：

    1  jit 剖面原樣            Can't read /proc/sys/kernel/overflowuid   ← ProcSubset=pid
    2  +ProcSubset=all         No permissions to create a new namespace  ← RestrictNamespaces
    3  +RestrictNamespaces=no  loopback: Failed to create NETLINK_ROUTE socket ← RestrictAddressFamilies
    4  +AF_NETLINK             Failed to make / slave: Operation not permitted ← SystemCallFilter
    5  +SystemCallFilter 加 @mount  rc=0

第 2、4 條放寬的正是 **user namespace ＋ mount**——外層加固面存在的理由本身；第 4 條
的鍵還在 :data:`permgen.PROFILE_LOCKED_KEYS` 上。裁決因此更正為票上的 **C**：換一個
不需要 bubblewrap 的執行形態（codex 的 landlock ＋ seccomp 路徑），外層**一條都不動**，
只全域放行 `@sandbox`（四支只能讓行程把自己關得更緊的 syscall）。

## 本檔釘住什麼

1. `SandboxRegistryTests`——形態是**登記表上的一格**，由它機械導出需求；未量過的
   executor 那一格是空的，未知 executor fail-closed。
2. `HardeningSurfaceTests`——`@sandbox` 在**每一份** unit 上、`@mount` 一份都沒有、
   `SystemCallFilter` 仍是鎖定鍵（沒有任何剖面分岔它）。
3. `CodexArgvTests`——argv 上的形態選擇來自登記表，不是第二份字面量。
4. `LastMessagePathTests`／`DegradedLaunchTests`——缺陷 2：`-o` 的落點改由 **job 自己
   那份 log** 導出，因此兩種派工模式都落在寫得進去的那一格，而且**都帶 job id**。
5. `ProbeGeneratorTests`——反向不變式探針的四個方向都在，且不自組 property／setenv。

OS 層語意（真的起一個 unit、真的裝一次 landlock）在單 UID／無 systemd 的 CI 上重現
不了，因此以具名 skip 標出，不留假綠——見 `OsLevelSemanticsTests`。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import paulsha_cortex.coordinator.job_runner as job_runner
import paulsha_cortex.coordinator.launcher as launcher_module
from paulsha_cortex.coordinator import job_workspace
from paulsha_cortex.coordinator.launcher import (
    SubprocessLauncher,
    build_claude_argv,
    build_codex_argv,
    build_copilot_argv,
)
from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.registry import Principal

_ISOLATED_AGENTS_ROOT = tempfile.mkdtemp(prefix="psc-agents-root-714-")

_BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PSC_BUILDER_PATH": "/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin",
    "PSC_REVIEWER_PATH": "/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin",
    "PSC_GATE_PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin",
    "HOME": "/var/lib/cortex-manager",
    "PSC_AGENTS_ROOT": _ISOLATED_AGENTS_ROOT,
    "LANG": "en_US.UTF-8",
    "PSC_REPO_ROOT": "/opt/cortex/venv/lib/python3/site-packages",
}


def _all_units() -> dict[str, str]:
    """本 repo 產生的全部八份 unit（6 份 job 模板 ＋ manager ＋ monitor）。"""

    scheme = permgen.SCHEMES["four-way"]
    units = {
        "manager": permgen.build_manager_unit(scheme).content,
        "monitor": permgen.build_monitor_unit(scheme).content,
    }
    for principal in (Principal.BUILDER, Principal.REVIEWER, Principal.GATE):
        for profile in permgen.HARDENING_PROFILES:
            unit = permgen.build_job_unit(scheme, principal=principal, profile=profile)
            units[unit.unit_name] = unit.content
    return units


def _service_values(content: str, key: str) -> list[str]:
    out: list[str] = []
    section = ""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section != "Service" or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            out.append(value.strip())
    return out


# ---------------------------------------------------------------------------
# 1. 形態是登記表上的一格，需求由它機械導出
# ---------------------------------------------------------------------------

class SandboxRegistryTests(unittest.TestCase):

    def test_only_codex_has_a_measured_inner_sandbox(self) -> None:
        """**量到的才填。**

        `copilot` 同樣是 agentic CLI、同樣是 node 型，但它的內層沙箱形態 0819 沒有被
        量過——因此那一格是空的。這與 `filtered_syscalls` 只填「有 audit record 背書」
        的那幾支是同一條規矩：登記表存的是量測，不是形態推論。
        """

        measured = {
            tool.name for tool in permgen.TOOLCHAIN_PROGRAMS if tool.inner_sandbox
        }
        self.assertEqual(measured, {"codex"})

    def test_codex_spec_is_the_landlock_shape(self) -> None:
        spec = permgen.executor_inner_sandbox("codex")
        assert spec is not None
        self.assertEqual(spec.kind, "landlock-seccomp")
        self.assertEqual(spec.argv, ("--enable", "use_legacy_landlock"))
        self.assertEqual(spec.syscall_groups, ("@sandbox",))
        # 取捨必須是明載的：#714 的 operator 指示逐字要求「沒有 PID namespace」這件事
        # 寫進登記表 note，不留成隱性假設。
        self.assertTrue(spec.accepted_loss)
        self.assertTrue(
            any("PID namespace" in item for item in spec.accepted_loss),
            spec.accepted_loss,
        )

    def test_unknown_executor_fails_closed(self) -> None:
        with self.assertRaises(permgen.UnknownExecutorProfileError):
            permgen.executor_inner_sandbox("gpt-9000")

    def test_executors_without_a_measured_sandbox_return_none(self) -> None:
        for name in ("claude", "agy", "copilot"):
            self.assertIsNone(permgen.executor_inner_sandbox(name), name)

    def test_surfaces_are_derived_and_satisfied(self) -> None:
        surfaces = permgen.inner_sandbox_surfaces()
        self.assertEqual(
            {(item.program, item.surface) for item in surfaces}, {("codex", "codex")}
        )
        for item in surfaces:
            self.assertTrue(item.satisfied, f"{item.program} @ {item.detail}")
            self.assertEqual(item.missing_groups, ())

    def test_import_time_assertion_catches_a_removed_group(self) -> None:
        """把 `@sandbox` 從加固表拿掉 ⇒ 模組層的斷言當場炸掉。

        這條是本票整個機制的承重點：需求與加固面之間不是註解上的約定，是 import 當下
        會失敗的一條斷言。少了它，「argv 換了形態、加固面沒跟上」會安靜地退回
        「job 沒有內層沙箱」——而那個狀態在 log 上看起來一切正常。
        """

        crippled = tuple(
            (key, "@system-service" if key == "SystemCallFilter" else value, why)
            for key, value, why in permgen._HARDENING
        )
        with mock.patch.object(permgen, "_HARDENING", crippled):
            with self.assertRaises(ValueError) as ctx:
                permgen._validate_inner_sandbox_support()
        self.assertIn("@sandbox", str(ctx.exception))
        # 還原後仍成立（確認上面測的是暫時的替換，不是把真相改壞了）。
        permgen._validate_inner_sandbox_support()

    def test_locked_key_requirement_is_enforced(self) -> None:
        """處置必須是**全域**，因此那個鍵必須留在鎖定表上。"""

        self.assertIn(permgen.INNER_SANDBOX_SYSCALL_KEY, permgen.PROFILE_LOCKED_KEYS)
        with mock.patch.object(permgen, "PROFILE_LOCKED_KEYS", frozenset()):
            with self.assertRaises(ValueError):
                permgen._validate_inner_sandbox_support()


# ---------------------------------------------------------------------------
# 2. 加固面：全域加一個方向相反的群組，一條放寬都沒有
# ---------------------------------------------------------------------------

class HardeningSurfaceTests(unittest.TestCase):

    def test_every_unit_allows_the_measured_group(self) -> None:
        for name, content in _all_units().items():
            values = _service_values(content, "SystemCallFilter")
            self.assertEqual(len(values), 1, name)
            self.assertIn("@sandbox", values[0].split(), name)

    def test_no_profile_diverges_on_the_syscall_filter(self) -> None:
        """全域＝八份 unit 的值**逐字相同**（這正是「不是某份剖面偷偷多開一支」）。"""

        values = {
            _service_values(content, "SystemCallFilter")[0]
            for content in _all_units().values()
        }
        self.assertEqual(len(values), 1, values)

    def test_route_a_relaxations_are_absent_everywhere(self) -> None:
        """路線 A 那四條放寬，**一條都沒有**落在任何 unit 上。

        這一條把「我們沒有走 A」變成機器擋得住的事實。`ProcSubset=pid` 與
        `RestrictNamespaces=yes` 逐字還在，`@mount` 一份都沒有。
        """

        for name, content in _all_units().items():
            self.assertEqual(_service_values(content, "ProcSubset"), ["pid"], name)
            self.assertEqual(
                _service_values(content, "RestrictNamespaces"), ["yes"], name
            )
            self.assertEqual(
                _service_values(content, "RestrictAddressFamilies"),
                ["AF_UNIX AF_INET AF_INET6"],
                name,
            )
            self.assertNotIn(
                "@mount", _service_values(content, "SystemCallFilter")[0].split(), name
            )

    def test_the_fatality_key_is_untouched(self) -> None:
        """#673 的那一行仍承重——本票只動白名單，沒動過濾語意。"""

        for name, content in _all_units().items():
            self.assertEqual(
                _service_values(content, permgen.SECCOMP_FATALITY_KEY), ["EPERM"], name
            )


# ---------------------------------------------------------------------------
# 3. argv：形態選擇的唯一真相在登記表
# ---------------------------------------------------------------------------

class CodexArgvTests(unittest.TestCase):

    def _argv(self, **kwargs) -> list[str]:
        return build_codex_argv(prompt="P", slice_id="wf-1", log_dir="/lg", **kwargs)

    def test_the_flag_rides_exactly_on_the_contracts_that_attach_it(self) -> None:
        """#716 B 後半：附掛條件跟著契約走，旗標逐字只上 read-only 族的 argv。

        寫入卡（預設 builder 與 commit-required）**不帶**——`danger-full-access`
        那一列沒有內層沙箱，旗標對它是無意義殘留，只會在 job log 開頭多印一筆
        deprecation 的 error item；write-forbidden 的 build 卡（builder persona、
        read-only mode）**照帶**，它的 landlock 今天是好的、真的在擋。
        """

        spec = permgen.executor_inner_sandbox("codex")
        assert spec is not None
        flag = " ".join(spec.argv)
        for kwargs in ({}, {"commit_required": True}):
            self.assertNotIn(flag, " ".join(self._argv(**kwargs)), kwargs)
        self.assertIn(flag, " ".join(self._argv(write_forbidden=True)))

    def test_read_only_and_review_only_carry_it_too(self) -> None:
        """**不以「這個 lane 會不會跑命令」當判準。**

        `#673` 的教訓是剖面判準看不到第二層；同一個錯誤在這裡的形狀是「planner 不跑
        命令，所以不必給它內層沙箱」——那是把一個**行為假設**寫成加固決定。哪天 planner
        真的跑了一條命令，症狀會與 #714 逐字相同。
        """

        for kwargs in ({"read_only": True}, {"review_only": True}):
            argv = self._argv(**kwargs)
            self.assertIn("--enable", argv, kwargs)
            self.assertIn("use_legacy_landlock", argv, kwargs)

    def test_bypass_mode_does_not_pick_a_shape(self) -> None:
        """`--dangerously-bypass-approvals-and-sandbox` 已經整個關掉內層沙箱。"""

        argv = self._argv(allow_unsafe=True)
        self.assertNotIn("use_legacy_landlock", argv)

    def test_other_executors_are_untouched(self) -> None:
        """形態是 per-executor 的量測；沒量過的 executor 的 argv **逐字不變**。"""

        for builder in (build_claude_argv, build_copilot_argv):
            argv = builder(prompt="P", slice_id="wf-1", log_dir="/lg")
            self.assertNotIn("use_legacy_landlock", argv, builder.__name__)


# ---------------------------------------------------------------------------
# 4. 缺陷 2：`last.json` 的落點
# ---------------------------------------------------------------------------

class LastMessagePathTests(unittest.TestCase):

    def test_derived_from_the_jobs_own_log(self) -> None:
        self.assertEqual(
            str(job_workspace.job_last_message_path("/spool/build-logs/wf-7/job.jsonl")),
            "/spool/build-logs/wf-7/job.last.json",
        )
        self.assertEqual(
            str(job_workspace.job_last_message_path("/logs/workflow/wf-7.jsonl")),
            "/logs/workflow/wf-7.last.json",
        )

    def test_default_argv_path_is_no_longer_shared(self) -> None:
        """舊形態是 `<log_dir>/last.json`——**不帶 job id**。

        那不只是授權問題：即使補了授權，並行的兩個 job 也會互相蓋掉。因此連 direct
        模式的預設落點都改成帶 slice id。
        """

        argv = build_codex_argv(prompt="P", slice_id="wf-7", log_dir="/lg")
        target = argv[argv.index("-o") + 1]
        self.assertEqual(target, "/lg/wf-7.last.json")
        self.assertNotEqual(target, "/lg/last.json")

    def test_two_concurrent_jobs_never_share_the_path(self) -> None:
        targets = set()
        for slice_id in ("wf-a", "wf-b"):
            argv = build_codex_argv(prompt="P", slice_id=slice_id, log_dir="/lg")
            targets.add(argv[argv.index("-o") + 1])
        self.assertEqual(len(targets), 2, targets)

    def test_explicit_path_wins(self) -> None:
        argv = build_codex_argv(
            prompt="P", slice_id="wf-7", log_dir="/lg",
            last_message_path="/spool/build-logs/wf-7/job.last.json",
        )
        self.assertEqual(
            argv[argv.index("-o") + 1], "/spool/build-logs/wf-7/job.last.json"
        )


class _FakeProc:
    pid = 7140

    def poll(self) -> int | None:
        return None


class _RecordingPopen:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        return _FakeProc()


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


def _template_seams():
    return [
        mock.patch.object(job_runner.shutil, "which", return_value="/usr/bin/systemctl"),
        mock.patch.object(job_runner, "_systemd_booted", return_value=True),
        mock.patch.object(job_runner, "_account_exists", return_value=True),
        mock.patch.object(job_runner, "_group_exists", return_value=True),
        mock.patch.object(job_runner, "_unit_file_installed", return_value=True),
        mock.patch.object(job_runner, "_is_executable", return_value=True),
        mock.patch.object(job_runner, "_unit_is_active", return_value=False),
        mock.patch.object(job_runner, "_spool_readable_by", return_value=(True, "")),
        mock.patch.object(job_runner, "_spec_readable_by", return_value=(True, "")),
    ]


class DegradedLaunchTests(unittest.TestCase):
    """降權派工：`-o` 必須落在 **job 自己那格 log spool**，而不是 Manager 的 log 目錄。"""

    def _launch(self, slice_id: str = "psc-0714-demo") -> tuple[list[str], str, str]:
        popen = _RecordingPopen()
        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = popen
        try:
            with tempfile.TemporaryDirectory() as root:
                spool = str(Path(root) / "job-specs")
                Path(spool).mkdir(parents=True, exist_ok=True)
                env = {
                    **_BASE_ENV,
                    job_runner.JOB_RUNNER_ENV: job_runner.RUNNER_SYSTEMD_TEMPLATE,
                    job_runner.JOB_SPEC_SPOOL_ENV: spool,
                    job_runner.REVIEW_JOB_SPEC_SPOOL_ENV: spool,
                    job_runner.GATE_JOB_SPEC_SPOOL_ENV: spool,
                }
                log_dir = str(Path(root) / "logs")
                with mock.patch.dict(os.environ, env, clear=True):
                    with _nested(_template_seams()):
                        SubprocessLauncher("codex").launch(
                            slice_id=slice_id,
                            prompt="PROMPT",
                            worktree=root,
                            log_dir=log_dir,
                        )
                import json

                spec = json.loads(
                    sorted(Path(spool).glob("*.json"))[0].read_text(encoding="utf-8")
                )
                command = [str(item) for item in spec["command"]]
                return command, str(spec["log_path"]), str(Path(log_dir).resolve())
        finally:
            launcher_module.subprocess.Popen = original

    def test_output_last_message_is_a_sibling_of_the_job_log(self) -> None:
        command, job_log, _log_dir = self._launch()
        joined = " ".join(command)
        expected = str(job_workspace.job_last_message_path(job_log))
        self.assertIn(expected, joined)
        self.assertEqual(Path(expected).parent, Path(job_log).parent)

    def test_the_manager_dispatch_log_dir_is_no_longer_in_the_job_command(self) -> None:
        """#604 的那一格終於乾淨了。

        該檔原本留著一條註記：「codex 的 `-o <log_dir>/last.json` 仍指向同一個目錄
        …… 屬 executor argv 面的獨立缺口，另票處理」——本票就是那一票。
        """

        command, _job_log, log_dir = self._launch()
        joined = " ".join(command)
        self.assertNotIn(f"{log_dir}/last.json", joined)
        self.assertNotIn(f"-o {log_dir}/", joined)

    def test_the_path_carries_the_job_id(self) -> None:
        command_a, log_a, _ = self._launch(slice_id="psc-0714-a")
        command_b, log_b, _ = self._launch(slice_id="psc-0714-b")
        target_a = str(job_workspace.job_last_message_path(log_a))
        target_b = str(job_workspace.job_last_message_path(log_b))
        self.assertNotEqual(target_a, target_b)
        self.assertIn("psc-0714-a", target_a)
        self.assertIn("psc-0714-b", target_b)
        # 兩份 job command（wrapper script 字串）各自只提到自己那一格。
        self.assertIn(target_a, " ".join(command_a))
        self.assertIn(target_b, " ".join(command_b))
        self.assertNotIn(target_b, " ".join(command_a))

    def test_the_job_command_still_picks_the_registry_sandbox_shape(self) -> None:
        """派工出去的 job command 消費的是同一張導出表（#716 B 後半）。

        `_launch()` 走的是預設 builder＝寫入卡契約：mode 是 `danger-full-access`、
        **不帶** legacy landlock 旗標、也**不帶** `--dangerously-bypass-*`（那會連
        #698 封住的 hook 信任閘一起關掉）。
        """

        command, _job_log, _log_dir = self._launch()
        joined = " ".join(command)
        self.assertIn("danger-full-access", joined)
        self.assertNotIn("use_legacy_landlock", joined)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", joined)
        self.assertNotIn("--dangerously-bypass-hook-trust", joined)


# ---------------------------------------------------------------------------
# 5. 反向不變式探針
# ---------------------------------------------------------------------------

class ProbeGeneratorTests(unittest.TestCase):

    def setUp(self) -> None:
        self.lines = permgen.build_inner_sandbox_probe(permgen.SCHEMES["four-way"])
        self.text = "\n".join(self.lines)

    def test_all_four_directions_are_present(self) -> None:
        # 1) 負向對照：不帶旗標必須仍然死在同一個字串上。
        self.assertIn("Can't read /proc/sys/kernel/overflowuid", self.text)
        # 2) 旗標還在。
        self.assertIn("Unknown feature flag", self.text)
        # 3) 正向。
        self.assertIn("use_legacy_landlock", self.text)
        # 4) 內層真的在擋（寫入 ＋ 網路）。
        self.assertIn("Permission denied", self.text)
        self.assertIn("getent hosts", self.text)

    def test_the_enforcement_step_carries_paired_controls(self) -> None:
        """「被擋」必須配一個「沒有內層沙箱時會過」的對照組。

        少了對照組，最容易寫出的那條檢查是**假的**：拿「寫 job 的 HOME 被擋」當證據
        ——那一格本來就不在 `ReadWritePaths=` 內，`ProtectSystem=strict` 會先回
        `Read-only file system`，內層有沒有裝上完全看不出來。0819 第一版探針就是這樣
        寫的，實跑才發現它在證明外層。
        """

        self.assertIn("OUTER_ALLOWS", self.text)
        self.assertIn("INNER_LEAK", self.text)
        # 被擋的那一格必須落在 job 的可寫面（worktree pool）之內。
        self.assertIn(f"{permgen.DEFAULT_LAYOUT.worktree_root}/probe", self.text)
        # 而不是 job 的 HOME——那是外層擋的，不是內層。
        self.assertNotIn("psc-714-PWN", self.text)

    def test_it_never_hand_assembles_the_hardening_surface(self) -> None:
        """D13：加固面只有一份定義，探針一行 `--property=`／`--setenv=` 都不自組。"""

        self.assertNotIn("--property=", self.text)
        self.assertNotIn("--setenv=", self.text)
        self.assertIn(permgen.PATH_PROBE_HELPER, self.text)

    def test_the_flag_comes_from_the_registry(self) -> None:
        spec = permgen.executor_inner_sandbox("codex")
        assert spec is not None
        self.assertIn(" ".join(spec.argv), self.text)

    def test_it_watches_the_upstream_deprecation_notice(self) -> None:
        """旗標**已被上游宣告要移除**，探針必須把那句話印出來當早期警報。

        而且必須從**真實派工的 job log** 撈——`codex sandbox` 子命令不印那句話（0819
        實測），對著它 grep 只會得到一個看起來很安心、其實什麼都沒驗到的空結果。
        """

        self.assertIn("deprecat", self.text)
        self.assertIn("job.jsonl", self.text)
        spec = permgen.executor_inner_sandbox("codex")
        assert spec is not None
        self.assertTrue(
            any("deprecated" in item for item in spec.accepted_loss),
            spec.accepted_loss,
        )

    def test_it_refuses_an_executor_without_a_measured_sandbox(self) -> None:
        with self.assertRaises(ValueError):
            permgen.build_inner_sandbox_probe(
                permgen.SCHEMES["four-way"], executor="claude"
            )

    def test_it_targets_the_unit_the_executor_actually_runs_on(self) -> None:
        """字幹必須是 codex 真的會跑的那一份（jit 剖面），不是預設那一份。"""

        profile = permgen.executor_hardening_profile("codex")
        stem = permgen.job_unit_stem(
            permgen.DEFAULT_LAYOUT, Principal.BUILDER, profile
        )
        self.assertIn(stem, self.text)


# ---------------------------------------------------------------------------
# 6. deprecation 噪音不得污染 terminal 契約
# ---------------------------------------------------------------------------

class DeprecationNoiseTests(unittest.TestCase):
    """codex 對這個旗標的 deprecation 宣告以 `item.type=error` 進 `--json` 串流。

    0819 實機逐字（真實派工的 job log 第一筆）：

        {"type":"item.completed","item":{"id":"item_0","type":"error",
         "message":"`[features].use_legacy_landlock` is deprecated and will be
         removed soon. …"}}

    本票因此欠一條斷言：**那筆 error 不得讓 terminal 契約誤判**。它不是理論——
    `#714` 的原症狀正是「Manager 端看到契約錯誤、病因在四層之下」，再多一個會混淆
    契約的雜訊等於替下一次同型誤診鋪路。
    """

    def test_a_leading_error_item_does_not_shadow_the_terminal_payload(self) -> None:
        import json

        from paulsha_cortex.coordinator import manager as manager_module

        payload = {
            "kind": "workflow-card",
            "schema_version": 1,
            "status": "passed",
            "candidate": "deadbeef",
            "outputs": {},
        }
        lines = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_0",
                        "type": "error",
                        "message": (
                            "`[features].use_legacy_landlock` is deprecated and "
                            "will be removed soon."
                        ),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(payload)},
                }
            ),
        ]
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "job.jsonl"
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            extracted = manager_module._extract_terminal_json(str(log))
        self.assertEqual(extracted.get("status"), "passed")


# ---------------------------------------------------------------------------
# 7. 明確測不到的 OS 層語意（#638／#657 的教訓：不留假綠）
# ---------------------------------------------------------------------------

class OsLevelSemanticsTests(unittest.TestCase):

    @unittest.skip(
        "需要真的起一份模板 unit（root ＋ systemd ＋ 四個 job 帳號）並讓 codex 在裡面"
        "真的裝一次 landlock/seccomp。本 repo 的測試進程是單 UID、無 systemd 加固面，"
        "那個環境下 `landlock_restrict_self` 不會被過濾、bwrap 也起得來——兩個方向都"
        "重現不了，跑了只會得到一個與 production 無關的綠燈（#638／#657 逐字記錄過"
        "這種假綠）。實機語意由 `trust_root inner-sandbox-probe` 產生的四步探針涵蓋，"
        "並貼在 runbook 第 4e-2g 步。"
    )
    def test_inner_sandbox_actually_installs_under_the_real_hardening_surface(self) -> None:
        raise AssertionError("unreachable")
