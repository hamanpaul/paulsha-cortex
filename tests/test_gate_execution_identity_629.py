"""#629：gate 執行身分——第四個帳號 `cortex-gate`。

`#604`／PR `#628` 把 gate ledger 與 exit sentinel 的**作者**收斂到 Manager，但刻意
沒做執行面：operator 宣告的 gate 命令跑在 builder 完全掌控內容的工作樹上，`pytest`
會載入該樹的 `conftest.py`／plugin ⇒ 執行者取得任意程式碼執行。後果是
`PSC_JOB_RUNNER=systemd-template` 下 build 卡恆因 `require_ledger` fail closed。

本檔守七組性質：

1. gate 確實以**第四個身分**執行（不變式），且該身分與既有三個逐一不同；
2. 該身分**不能**寫 Manager durable state、**不能**寫 builder 工作區、**不能**碰
   verdict spool；
3. builder 無法預先佔位 ledger 落點（pre-seed 守衛，沿用 #639 的 `spool_slot`）；
4. polkit 新字幹的正向放行與反向拒絕（含名稱混淆，比照 #647 的十種）；
5. 各份模板 unit 的加固表除剖面差異外逐項相同（**集合比對，不硬編**）；
6. spool 內容一律以**不受信任輸入**對待；
7. `direct` 模式零回歸。

## #638 的教訓（本檔的 skip 紀律）

有幾條語意在單 UID／寬鬆環境裡**測不出來**——不是難測，是「測了也永遠綠」：跨 UID
的 `wx` 無 `r` ACL 是否真的擋住讀取、polkit 是否真的拒絕、seal 之後 producer 是否
真的進不去、`cortex-gate` 對 builder 工作樹的 `rX` 是否真的成立。那些一律**明確
skip 並說明理由**，不用 `sudo -u`／裸跑當替身（那只會產生一個永遠綠、什麼都沒驗到
的測試）。真實驗證在 runbook 第 5-2b／5-7／9b 步；具備條件的機器可設
`PSC_TEST_REAL_HARDENING=1` 啟用。
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from paulsha_cortex.config import paths
from paulsha_cortex.coordinator import (
    gate_ledger,
    gate_runner,
    job_runner,
    spool_slot,
    terminal_contract,
)
from paulsha_cortex.coordinator.launcher import SubprocessLauncher
from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.registry import Principal

REAL_HARDENING = os.environ.get("PSC_TEST_REAL_HARDENING") == "1"

SCHEME = permgen.DEFAULT_SCHEME
LAYOUT = permgen.DEFAULT_LAYOUT
JOB_LAYOUT = LAYOUT.with_job_segment("%i")

GATE_ACCOUNT = "cortex-gate"
BUILDER_ACCOUNT = "cortex-builder"
REVIEW_ACCOUNT = "cortex-reviewer-planner"
MANAGER_ACCOUNT = "cortex-manager"

_BASE_ENV = {
    # daemon 自己的 PATH。#679 起它**不會**流進 job——job 的 PATH 只由
    # `PSC_GATE_PATH` 決定，未宣告即 fail-closed。
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    job_runner.GATE_PATH_ENV: "/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": "/var/lib/cortex-manager",
    "LANG": "en_US.UTF-8",
    "PSC_REPO_ROOT": "/opt/cortex",
    "PSC_SLICE_ID": "psc-0629-build",
}


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


class _RecordingRunner:
    """假的 `subprocess.run`：記下 argv，並可選擇性地把 gate 的產出寫進 spool。"""

    def __init__(self, *, returncode: int = 0, payload: object | None = None) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.payload = payload

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.payload is not None:
            # 真實部署裡這一段是 gate unit 內的 `gate_ledger` 做的；測試在這裡
            # 模擬「它交付了什麼」，好讓 Manager 端的驗證邏輯被真正走到。
            Path(self._out).write_text(
                json.dumps(self.payload), encoding="utf-8"
            )
        return _FakeCompleted(self.returncode)

    def expect_output(self, path: str | Path) -> "_RecordingRunner":
        self._out = str(path)
        return self


def _function_body_source(func) -> str:
    """函式的**程式碼本體**（剝掉 docstring，且不含註解）。

    `ast.unparse` 一併丟掉註解是刻意的：本檔用它來斷言「這段程式碼不再呼叫某個
    函式」，而註解與 docstring 裡提到那個名字是合理的（它們正在解釋為什麼不再呼叫）。
    """

    src = textwrap.dedent(inspect.getsource(func))
    node = ast.parse(src).body[0]
    body = getattr(node, "body", [])
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        node.body = body[1:]
    return ast.unparse(node)


def _nested(patches):
    class _Ctx:
        def __enter__(self):
            for p in patches:
                p.start()
            return self

        def __exit__(self, *exc):
            for p in reversed(patches):
                p.stop()
            return False

    return _Ctx()


def _preflight_patches(binary: str = "/usr/bin/systemctl"):
    return [
        mock.patch.object(job_runner.shutil, "which", return_value=binary),
        mock.patch.object(job_runner, "_systemd_booted", return_value=True),
        mock.patch.object(job_runner, "_account_exists", return_value=True),
        mock.patch.object(job_runner, "_group_exists", return_value=True),
        mock.patch.object(job_runner, "_unit_file_installed", return_value=True),
        mock.patch.object(job_runner, "_is_executable", return_value=True),
        mock.patch.object(job_runner, "_unit_is_active", return_value=False),
        # #657：preflight 現在會算「該 job 身分讀不讀得到自己的 spool」的 effective
        # 權限；本檔宣稱的帳號在單 UID 的開發機／CI 上不存在，故同一條 seam 一併
        # stub。真正驗這條語意的是 `tests/test_per_principal_spec_spool_657.py`。
        mock.patch.object(job_runner, "_spool_readable_by", return_value=(True, "")),
        mock.patch.object(job_runner, "_spec_readable_by", return_value=(True, "")),
    ]


def _ok_payload(*, slice_id: str = "psc-0629-build", exit_code: int = 0) -> dict:
    return {
        "schema_version": terminal_contract.GATE_LEDGER_SCHEMA_VERSION,
        "kind": terminal_contract.GATE_LEDGER_KIND,
        "slice_id": slice_id,
        "gates": [
            {
                "name": "pytest",
                "command": "python3 -m pytest -q",
                "exit_code": exit_code,
                "status": "passed" if exit_code == 0 else "failed",
                "detail": "",
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. 執行身分：第四個帳號，且與既有三個逐一不同
# ---------------------------------------------------------------------------

class GateIdentityTests(unittest.TestCase):
    """#629 的核心不變式：gate 既非 builder、非 Manager，也非 reviewer／planner。"""

    def test_the_gate_principal_maps_to_a_distinct_fourth_account(self) -> None:
        gate = SCHEME.resolve(Principal.GATE)
        self.assertEqual(gate, GATE_ACCOUNT)
        for other in (
            Principal.BUILDER,
            Principal.REVIEWER,
            Principal.PLANNER,
            Principal.MANAGER,
            Principal.MONITOR,
            Principal.HEADLESS_HOOK,
        ):
            self.assertNotEqual(gate, SCHEME.resolve(other), other)
        self.assertNotEqual(gate, SCHEME.durable_state_owner)
        self.assertNotEqual(gate, SCHEME.deploy_account)
        self.assertEqual(len(SCHEME.declared_accounts()), 5)  # 四個服務帳號 ＋ root

    def test_gate_is_an_untrusted_execution_principal_but_not_a_model_persona(self) -> None:
        """它必須落在「Manager-owned 樹零寫入」的那個集合裡，但不需要模型的前置物。"""
        self.assertIn(Principal.GATE, registry.UNTRUSTED_EXECUTION_PRINCIPALS)
        self.assertNotIn(Principal.GATE, registry.HEADLESS_PERSONAS)
        self.assertIn(GATE_ACCOUNT, SCHEME.headless_accounts())
        self.assertNotIn(GATE_ACCOUNT, SCHEME.model_job_accounts())
        # 不跑模型 ⇒ 不該有 root-owned 的 ~/.codex 與 executor 憑證骨架。
        scaffold = {path for path, *_rest in LAYOUT.scaffold_directories(SCHEME)}
        self.assertIn(LAYOUT.cache_of(GATE_ACCOUNT), scaffold)
        self.assertIn(LAYOUT.home_of(GATE_ACCOUNT), scaffold)
        self.assertNotIn(LAYOUT.codex_hooks_dir_of(GATE_ACCOUNT), scaffold)
        self.assertNotIn(LAYOUT.executor_credential_dir_of(GATE_ACCOUNT), scaffold)

    def test_the_gate_unit_hardcodes_the_gate_account(self) -> None:
        unit = permgen.build_job_unit(SCHEME, principal=Principal.GATE)
        self.assertEqual(unit.unit_name, "cortex-gate-job@.service")
        self.assertEqual(unit.account, GATE_ACCOUNT)
        self.assertIn(f"User={GATE_ACCOUNT}\n", unit.content)
        self.assertIn(f"Group={GATE_ACCOUNT}\n", unit.content)
        # 身分只有 root-owned unit 檔一個來源——ExecStart 同樣寫死成 shim。
        self.assertEqual(unit.exec_start, f"{LAYOUT.job_shim} %i")

    def test_job_runner_role_table_and_permgen_agree_on_the_gate_account(self) -> None:
        """兩邊刻意不互相 import，由本測項釘住逐字相等（沿用 #615 的成對契約模式）。"""
        self.assertEqual(job_runner.DEFAULT_GATE_ACCOUNT, SCHEME.resolve(Principal.GATE))
        self.assertEqual(
            job_runner.DEFAULT_GATE_TEMPLATE_UNIT,
            f"{permgen.job_unit_stem(LAYOUT, Principal.GATE)}@.service",
        )
        self.assertEqual(
            permgen.JOB_PATH_ENV_BY_PRINCIPAL[Principal.GATE],
            job_runner.JOB_ROLE_CONFIG[job_runner.JOB_ROLE_GATE].path_env,
        )
        self.assertIn(Principal.GATE, permgen.DOWNGRADED_JOB_PRINCIPALS)
        self.assertEqual(
            len(permgen.DOWNGRADED_JOB_PRINCIPALS), len(job_runner.JOB_ROLES)
        )

    def test_gate_unit_serves_no_model_persona(self) -> None:
        """`JOB_PRINCIPAL_PERSONAS` 必須機器可讀地說「沒有任何 persona 以 gate 起跑」。"""
        served = permgen.JOB_PRINCIPAL_PERSONAS[Principal.GATE]
        self.assertEqual(served, frozenset({Principal.GATE}))
        self.assertEqual(served & registry.HEADLESS_PERSONAS, frozenset())

    def test_the_launcher_never_routes_a_model_job_to_the_gate_role(self) -> None:
        """三個 persona 的 `_job_role()` 都不得是 gate——gate 不跑模型。"""
        for label, launcher in (
            ("builder", SubprocessLauncher("claude")),
            ("reviewer", SubprocessLauncher("claude").as_review_only(
                terminal_kind="workflow-review-result")),
            ("planner", SubprocessLauncher("claude").as_read_only()),
        ):
            self.assertNotEqual(launcher._job_role(), job_runner.JOB_ROLE_GATE, label)


# ---------------------------------------------------------------------------
# 2. gate 身分的可寫面：三條「不能碰」
# ---------------------------------------------------------------------------

def _write_face(principal: Principal) -> tuple[str, ...]:
    return permgen.build_job_unit(SCHEME, principal=principal).read_write_paths


def _covers(granted: str, target: str) -> bool:
    g = granted.rstrip("/")
    return target == g or target.startswith(g + "/")


class GateWriteFaceTests(unittest.TestCase):
    """gate 的 `ReadWritePaths=` 由登記表機械導出——本組釘住它導出來的是什麼。"""

    def setUp(self) -> None:
        self.granted = _write_face(Principal.GATE)

    def test_gate_can_write_exactly_its_own_two_trees_plus_cache(self) -> None:
        self.assertEqual(
            set(self.granted),
            {
                LAYOUT.cache_of(GATE_ACCOUNT),
                JOB_LAYOUT.gate_ledger_spool_root,
                JOB_LAYOUT.gate_worktree_root,
            },
        )

    def test_gate_cannot_write_manager_durable_state(self) -> None:
        """含 gate ledger／exit sentinel 的落點——這一條若破，#628 當場失效。"""
        for target in (
            LAYOUT.dispatch_log_root,       # gate-ledger（ledger ＋ exit sentinel）
            LAYOUT.coordinator_root,        # Manager 的 durable state 樹
            LAYOUT.repo_source_root,        # per-job clone 的來源樹
            LAYOUT.job_spec_spool_root,     # job spec（誰的命令列）
            LAYOUT.control_root,
            LAYOUT.monitor_state_root,
        ):
            for granted in self.granted:
                self.assertFalse(_covers(granted, target), (granted, target))

    def test_gate_cannot_write_the_builder_workspace(self) -> None:
        """gate 讀得到被驗的樹（`rX`），但**寫不進去**——副本才是它的可寫面。

        **#710 修改了本測試的兩條斷言，理由逐條**：

        1. `entry.owner` 從 `BUILDER_ACCOUNT` 改成 `SCHEME.durable_state_owner`。
           「整個 clone 由 builder 擁有」這個形態**從來沒有被實作，而且 Manager
           結構上做不到**（`chown` 給另一個使用者要 `CAP_CHOWN`，Manager unit 帶
           `CapabilityBoundingSet=`）——#710 的實機證據是 `stat` 回
           `cortex-manager:cortex-manager 700`。owner 因此改為 Manager，builder 的
           可寫面走具名 ACL。**本測試要守的性質沒有變**：gate 對這棵樹只有讀，
           而下面那兩條斷言（gate 的 perms 不含 `w`、gate 的可寫面不涵蓋這棵樹）
           就是那個性質，它們一行未改。
        2. gate 的 ACL 條目從「恰好一條」改成「access ＋ default 各一條，perms 相同」。
           #710 讓 per-job 那一格的 ACL **由執行期真的套上去**，而 default ACL 是
           「builder 在 run 期間新建的檔 gate 也讀得到」的唯一來源（POSIX：目錄帶
           default ACL 時 umask 不生效，因此不會被 unit 的 `UMask=0077` 壓掉）。
           在此之前那條 default 是 `PermissionEntry.commands()` 自動補的，同樣存在，
           只是不在 `entry.acls` 上——因此這不是新增授權，是把同一條授權從隱式改為
           顯式（`effective_default_acls()` 對修改前後回傳同一組）。
        """
        for granted in self.granted:
            self.assertFalse(_covers(granted, JOB_LAYOUT.asset_paths()["repo-worktree"]))
            self.assertFalse(_covers(granted, LAYOUT.worktree_root))
        entry = next(
            e for e in permgen.generate_plan(SCHEME).entries
            if e.asset_id == "repo-worktree"
        )
        self.assertEqual(entry.owner, SCHEME.durable_state_owner)
        gate_acls = [a for a in entry.acls if a.account == GATE_ACCOUNT]
        self.assertEqual(len(gate_acls), 2, entry.acls)
        self.assertEqual({a.default for a in gate_acls}, {False, True}, entry.acls)
        for acl in gate_acls:
            self.assertEqual(acl.perms, "rX")   # 讀＋traverse，**沒有 w**
            self.assertNotIn("w", acl.perms)

    def test_gate_cannot_touch_the_verdict_spool(self) -> None:
        """#639 剛關掉的通道不得因為多一個帳號而重新打開。"""
        for granted in self.granted:
            self.assertFalse(_covers(granted, JOB_LAYOUT.review_verdict_spool_root))
            self.assertFalse(_covers(granted, JOB_LAYOUT.commit_spool_root))
        for asset_id in ("review-verdict-spool", "review-verdict", "commit-spool"):
            entry = next(
                e for e in permgen.generate_plan(SCHEME).entries if e.asset_id == asset_id
            )
            self.assertNotIn(
                GATE_ACCOUNT, {a.account for a in entry.acls}, asset_id
            )
            self.assertNotIn(GATE_ACCOUNT, entry.writer_accounts, asset_id)

    def test_no_other_role_gains_a_write_face_on_the_gate_trees(self) -> None:
        """反向：builder／reviewer 都不得寫得進 gate 的 spool 或工作區。"""
        for principal in (Principal.BUILDER, Principal.REVIEWER):
            for granted in _write_face(principal):
                for target in (
                    JOB_LAYOUT.gate_ledger_spool_root,
                    JOB_LAYOUT.gate_worktree_root,
                ):
                    self.assertFalse(_covers(granted, target), (principal, granted))

    def test_the_gate_ledger_asset_still_has_manager_as_its_only_writer(self) -> None:
        """權威 ledger 的作者歸屬與 #628 逐字不變（gate 只寫 spool）。"""
        asset = registry.asset_by_id("gate-ledger")
        self.assertEqual(asset.writers, (Principal.MANAGER,))
        entry = next(
            e for e in permgen.generate_plan(SCHEME).entries if e.asset_id == "gate-ledger"
        )
        self.assertEqual(entry.owner, MANAGER_ACCOUNT)
        self.assertEqual(entry.acls, ())

    def test_the_spool_grants_gate_write_only_no_read(self) -> None:
        """`wx` 無 `r`：寫得進自己那格、讀不到別人的（與兩個既有 spool 同形）。"""
        entry = next(
            e for e in permgen.generate_plan(SCHEME).entries
            if e.asset_id == "gate-ledger-spool"
        )
        self.assertEqual(entry.owner, MANAGER_ACCOUNT)
        self.assertEqual(entry.mode, 0o700)
        acls = [a for a in entry.acls if a.account == GATE_ACCOUNT]
        self.assertEqual([a.perms for a in acls], ["wx"])
        self.assertNotIn("r", acls[0].perms)


# ---------------------------------------------------------------------------
# 3. pre-seed 守衛：沿用 #639 的 spool_slot，不另寫一份
# ---------------------------------------------------------------------------

class PreSeedGuardTests(unittest.TestCase):
    def test_prepare_gate_spool_uses_the_shared_slot_lifecycle(self) -> None:
        """守衛必須是 `spool_slot.create_slot(reset=True)`，不是本模組自己的實作。"""
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(
                spool_slot, "create_slot", wraps=spool_slot.create_slot
            ) as spy:
                gate_runner.prepare_gate_spool(spool_key="job-1", coordinator_root=root)
            self.assertEqual(spy.call_count, 1)
            self.assertIs(spy.call_args.kwargs.get("reset"), True)

    def test_a_preseeded_ledger_is_destroyed_not_consumed(self) -> None:
        """builder 預埋一份「全過」的 ledger，得到的是自己的檔案被刪掉。"""
        with tempfile.TemporaryDirectory() as root:
            slot = gate_runner.gate_ledger_spool_dir(
                spool_key="job-1", coordinator_root=root
            )
            slot.mkdir(parents=True)
            planted = slot / gate_runner.GATE_LEDGER_FILENAME
            planted.write_text(json.dumps(_ok_payload()), encoding="utf-8")
            (slot / "extra-junk").write_text("x", encoding="utf-8")

            out = gate_runner.prepare_gate_spool(
                spool_key="job-1", coordinator_root=root
            )
            self.assertEqual(out, planted)
            self.assertFalse(planted.exists())
            self.assertFalse((slot / "extra-junk").exists())
            self.assertTrue(slot.is_dir())

    def test_a_preseeded_symlink_slot_is_refused(self) -> None:
        """把那一格換成 symlink 等於把落點外包出去——拒絕，不是接受後跟隨。"""
        with tempfile.TemporaryDirectory() as root:
            slot = gate_runner.gate_ledger_spool_dir(
                spool_key="job-1", coordinator_root=root
            )
            slot.parent.mkdir(parents=True, exist_ok=True)
            elsewhere = Path(root) / "elsewhere"
            elsewhere.mkdir()
            slot.symlink_to(elsewhere)
            with self.assertRaises(gate_runner.GateRunnerError) as ctx:
                gate_runner.prepare_gate_spool(spool_key="job-1", coordinator_root=root)
            self.assertEqual(ctx.exception.reason, "gate-spool-unavailable")

    def test_the_slot_is_sealed_after_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            out = gate_runner.prepare_gate_spool(
                spool_key="job-1", coordinator_root=root
            )
            out.write_text(json.dumps(_ok_payload()), encoding="utf-8")
            gate_runner.seal_gate_spool(out)
            self.assertEqual(
                stat.S_IMODE(out.parent.stat().st_mode), spool_slot.SEALED_SLOT_MODE
            )

    def test_unsafe_spool_keys_are_refused_before_any_path_is_built(self) -> None:
        for bad in ("../escape", "/abs", "", "a/b", ".hidden"):
            with self.assertRaises(gate_runner.GateRunnerError) as ctx:
                gate_runner.gate_ledger_spool_dir(spool_key=bad, coordinator_root="/tmp")
            self.assertEqual(ctx.exception.reason, "gate-spool-key-invalid", bad)

    def test_spool_key_derivation_is_shared_with_the_commit_spool(self) -> None:
        """兩個 spool 的 key 必須是**同一個字串**，否則會出現 builder 那格建得起來、
        gate 這格建不起來的錯位。"""
        from paulsha_cortex.coordinator import job_workspace

        job = {
            "job_id": "psc-0629-build",
            "log_path": "/var/lib/cortex/runtime/dispatch/psc-0629-build.jsonl",
        }
        self.assertEqual(
            gate_runner.spool_key_for_job(job), job_workspace.spool_key_for_job(job)
        )
        self.assertEqual(gate_runner.spool_key_for_job(job), "psc-0629-build")


# ---------------------------------------------------------------------------
# 4. polkit：新字幹的正向放行與反向拒絕（比照 #647 的十種名稱混淆）
# ---------------------------------------------------------------------------

class PolkitGateStemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = permgen.build_polkit_rule(SCHEME, plan=permgen.PolkitPlan.TEMPLATE)

    def _decide(self, unit: str | None, verb: str | None = "start") -> str:
        return permgen.evaluate_polkit(
            self.rule,
            user=self.rule.subject_account,
            action_id=permgen.POLKIT_ACTION,
            unit=unit,
            verb=verb,
        )

    def test_still_exactly_one_rule_and_one_grant(self) -> None:
        """#647 立下的可審查性性質：全檔只有一個 `return YES`、一條 `addRule`。

        #629 擴的是**字幹段**，不是規則數——第二條 `addRule` 會把 subject／action／
        verb／明細缺席四個檢查複製一份，變成兩個要同步維護的放行出口。
        """
        self.assertEqual(self.rule.content.count("polkit.Result.YES"), 1)
        self.assertEqual(self.rule.content.count("polkit.addRule("), 1)

    def test_every_gate_stem_is_authorised(self) -> None:
        for profile in permgen.HARDENING_PROFILES:
            stem = permgen.job_unit_stem(LAYOUT, Principal.GATE, profile)
            self.assertEqual(self._decide(f"{stem}@psc-0629-build.service"), "YES", stem)

    def test_stem_segment_stays_a_closed_enumeration(self) -> None:
        tail = r"@[a-z0-9][a-z0-9._-]{0,62}\.service$"
        pattern = self.rule.unit_pattern
        self.assertTrue(pattern.startswith("^"))
        self.assertTrue(pattern.endswith(tail))
        head = pattern[1 : -len(tail)]
        stems = permgen.job_unit_stems(LAYOUT, permgen.DOWNGRADED_JOB_PRINCIPALS)
        self.assertEqual(head, "(?:" + "|".join(stems) + ")")
        self.assertEqual(
            len(stems),
            len(permgen.DOWNGRADED_JOB_PRINCIPALS) * len(permgen.HARDENING_PROFILES),
        )
        bare = head.replace("(?:", "").replace(")", "")
        for wildcard in (".*", "[^", "\\w", "+", "?", "|.", ".|"):
            self.assertNotIn(wildcard, bare, wildcard)

    def test_ten_gate_name_confusions_are_refused(self) -> None:
        """比照 #647 對 `-jit` 做的十種——名稱混淆一條都不得放行。"""
        for unit in (
            "cortex-gate-jobx@a.service",       # 字幹後多字元
            "cortex-gate-jo@a.service",         # 字幹被截短
            "cortex-job-gate@a.service",        # 段序對調
            "cortex-gate-job@.service",         # `@` 後空 instance
            "Cortex-gate-job@a.service",        # 首字大寫
            "cortex-gate-job@a.socket",         # 換 unit 型別
            "cortex-gate-job@a.service.bak",    # 尾綴混淆
            "cortex-gate-job@a.service\ncortex-gate-job@b.service",  # 換行注入
            "cortex-gate-job@" + "a" * 64 + ".service",              # instance 超長
            "xcortex-gate-job@a.service",       # 字幹前多字元
        ):
            self.assertEqual(self._decide(unit), "NO", unit)

    def test_transient_shapes_are_refused_for_the_gate_stem(self) -> None:
        for unit in (
            "cortex-gate-job-psc-0629-deadbeef.service",
            "cortex-gate-job-jit-psc-0629-deadbeef.service",
            "run-u1234.service",
        ):
            self.assertEqual(self._decide(unit), "NO", unit)

    def test_other_verbs_and_other_subjects_are_refused(self) -> None:
        for verb in ("reload", "mask", "set-property", "restart", "kill", "enable"):
            self.assertEqual(self._decide("cortex-gate-job@a.service", verb), "NO", verb)
        for user in ("nobody", BUILDER_ACCOUNT, REVIEW_ACCOUNT, GATE_ACCOUNT, "root"):
            self.assertEqual(
                permgen.evaluate_polkit(
                    self.rule,
                    user=user,
                    action_id=permgen.POLKIT_ACTION,
                    unit="cortex-gate-job@a.service",
                    verb="start",
                ),
                "NOT_HANDLED",
                user,
            )

    def test_legacy_schemes_do_not_name_a_gate_unit(self) -> None:
        """二分／三分沒有 gate 帳號 ⇒ 規則不該提一份那台機器上不存在的 unit。"""
        for scheme_id in ("two-way", "three-way"):
            scheme = permgen.SCHEMES[scheme_id].with_principal_accounts(
                {Principal.OPERATOR: "ops", Principal.EXTERNAL: "outbox"}
            )
            rule = permgen.build_polkit_rule(scheme)
            self.assertNotIn("cortex-gate-job", rule.unit_pattern, scheme_id)
            self.assertNotIn(GATE_ACCOUNT, rule.target_accounts, scheme_id)
            self.assertEqual(
                permgen.evaluate_polkit(
                    rule,
                    user=rule.subject_account,
                    action_id=permgen.POLKIT_ACTION,
                    unit="cortex-gate-job@a.service",
                    verb="start",
                ),
                "NO",
                scheme_id,
            )

    def test_the_pattern_is_a_valid_anchored_regex(self) -> None:
        compiled = re.compile(self.rule.unit_pattern)
        self.assertIsNotNone(compiled.search("cortex-gate-job@a.service"))
        self.assertIsNone(compiled.search("prefix-cortex-gate-job@a.service"))

    @unittest.skipUnless(
        REAL_HARDENING,
        "polkit 是否真的拒絕，只有在已落檔規則 ＋ 真實 D-Bus 上才驗得到；本機以 "
        "Python 鏡像測的是**產生邏輯**，不是 polkit 的實際決策。真實驗證在 runbook "
        "第 5-7 步（含移除規則後必須失敗的 fail-closed 對照）。",
    )
    def test_real_polkit_decision_for_the_gate_stem(self) -> None:  # pragma: no cover
        raise AssertionError("此測項只在已落檔 polkit 規則的實機上執行（runbook 5-7）。")


# ---------------------------------------------------------------------------
# 5. 加固表：各份 unit 除剖面差異外逐項相同（集合比對，不硬編）
# ---------------------------------------------------------------------------

def _hardening_table(content: str) -> dict[str, str]:
    """從產出的 unit 抽出實際生效的加固指令（只看真正的指令行，不看註解）。"""
    table: dict[str, str] = {}
    section = None
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            continue
        if section != "[Service]" or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key in {k for k, _v, _w in permgen._HARDENING}:
            table[key] = value
    return table


class GateHardeningParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.units = {
            (principal, profile.profile_id): permgen.build_job_unit(
                SCHEME, principal=principal, profile=profile
            )
            for principal in permgen.DOWNGRADED_JOB_PRINCIPALS
            for profile in permgen.HARDENING_PROFILES
        }

    def test_key_set_is_identical_across_every_template(self) -> None:
        expected = {key for key, _value, _why in permgen._HARDENING}
        for label, unit in self.units.items():
            self.assertEqual(set(_hardening_table(unit.content)), expected, label)

    def test_the_gate_template_matches_the_builder_template_item_by_item(self) -> None:
        """新增一個角色不得順手放寬任何一項加固——**集合比對，不硬編值**。"""
        for profile in permgen.HARDENING_PROFILES:
            builder = _hardening_table(
                self.units[(Principal.BUILDER, profile.profile_id)].content
            )
            gate = _hardening_table(
                self.units[(Principal.GATE, profile.profile_id)].content
            )
            self.assertEqual(gate, builder, profile.profile_id)

    def test_profile_divergence_is_bounded_for_the_gate_role(self) -> None:
        strict = _hardening_table(self.units[(Principal.GATE, "strict")].content)
        jit = _hardening_table(self.units[(Principal.GATE, "jit")].content)
        diff = {k for k in strict if strict[k] != jit[k]}
        self.assertEqual(diff, permgen.PROFILE_DIVERGENCE_KEYS)

    def test_read_write_paths_are_the_only_thing_that_differs_between_roles(self) -> None:
        """角色之間的全部差異都必須是「帳號」帶出來的，加固表一項都不能動。"""
        tables = {
            principal: _hardening_table(self.units[(principal, "strict")].content)
            for principal in permgen.DOWNGRADED_JOB_PRINCIPALS
        }
        self.assertEqual(len({tuple(sorted(t.items())) for t in tables.values()}), 1)


# ---------------------------------------------------------------------------
# 6. 執行面：spec 形狀、spool 消費、不受信任輸入
# ---------------------------------------------------------------------------

class GateExecutionTests(unittest.TestCase):
    def _run(self, *, root: str, payload: object, returncode: int = 0, env_extra=None):
        spool_dir = str(Path(root) / "job-specs")
        Path(spool_dir).mkdir(parents=True)
        coordinator_root = str(Path(root) / "coordinator")
        worktree = Path(root) / "worktree"
        worktree.mkdir()
        ledger_path = Path(root) / "dispatch" / "psc-0629-build.gates.json"
        env = {
            **_BASE_ENV,
            job_runner.JOB_RUNNER_ENV: job_runner.RUNNER_SYSTEMD_TEMPLATE,
            # #657：gate 讀的是**自己的** spool 變數。
            job_runner.JOB_SPEC_SPOOL_ENV: spool_dir,
            job_runner.GATE_JOB_SPEC_SPOOL_ENV: spool_dir,
            "PSC_GATE_CMD_PYTEST": "python3 -m pytest -q",
            **(env_extra or {}),
        }
        runner = _RecordingRunner(returncode=returncode, payload=payload)
        runner.expect_output(
            gate_runner.gate_spool_ledger_path(
                spool_key="psc-0629-build", coordinator_root=coordinator_root
            )
        )
        with mock.patch.dict(os.environ, env, clear=True), _nested(_preflight_patches()):
            result = gate_runner.run_declared_gates(
                job_id="psc-0629-build",
                spool_key="psc-0629-build",
                ledger_path=ledger_path,
                worktree=worktree,
                coordinator_root=coordinator_root,
                runner=runner,
            )
        specs = sorted(Path(spool_dir).iterdir())
        return {
            "result": result,
            "spec": json.loads(specs[0].read_text(encoding="utf-8")) if specs else None,
            "runner": runner,
            "ledger_path": ledger_path,
        }

    def test_the_gate_runs_under_the_gate_template_unit(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            out = self._run(root=root, payload=_ok_payload())
            # instance 名走與 builder 完全相同的推導（`job_workspace.job_segment`），
            # 因此 unit 名可追蹤且唯一；本測項釘的是**模板字幹**是 gate 的那一份。
            instance = job_runner.template_instance_id("psc-0629-build")
            expected_unit = f"cortex-gate-job@{instance}.service"
            self.assertEqual(out["spec"]["unit"], expected_unit)
            self.assertEqual(out["spec"]["instance"], instance)
            # 起動 argv 是封閉的：沒有 `--property=`／`--uid=`，unit 名是唯一輸入。
            self.assertEqual(
                out["runner"].calls[0],
                [
                    "/usr/bin/systemctl", "start", "--wait", "--no-ask-password",
                    expected_unit,
                ],
            )

    def test_the_spec_carries_no_identity_or_profile_field(self) -> None:
        """身分只由 root-owned unit 的 `User=` 決定（#643／0816 裁決 B 的核心）。"""
        with tempfile.TemporaryDirectory() as root:
            spec = self._run(root=root, payload=_ok_payload())["spec"]
            self.assertEqual(job_runner.forbidden_spec_keys(spec), [])
            for banned in ("User", "user", "account", "uid", "hardening_profile"):
                self.assertNotIn(banned, spec)

    def test_the_gate_command_snapshots_instead_of_running_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            spec = self._run(root=root, payload=_ok_payload())["spec"]
            argv = spec["command"]
            self.assertIn("--snapshot-from", argv)
            source = argv[argv.index("--snapshot-from") + 1]
            snapshot = argv[argv.index("--worktree") + 1]
            self.assertTrue(source.endswith("/worktree"), source)
            self.assertNotEqual(source, snapshot)
            # 副本落在 gate 自己的 pool，不在被驗的樹裡面。
            self.assertIn("/gate-worktree/", snapshot)
            self.assertTrue(
                snapshot.endswith("/" + job_runner.template_instance_id("psc-0629-build")),
                snapshot,
            )
            self.assertFalse(snapshot.startswith(source))

    def test_the_gate_env_carries_the_declaration_but_not_the_identity_config(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            env = self._run(
                root=root,
                payload=_ok_payload(),
                env_extra={
                    "PSC_GATE_ACCOUNT": "cortex-gate",
                    "PSC_GATE_HARDENING_PROFILE": "strict",
                    "GITHUB_TOKEN": "ghp_secret",
                },
            )["spec"]["env"]
            self.assertEqual(env["PSC_GATE_CMD_PYTEST"], "python3 -m pytest -q")
            for leaked in (
                "PSC_GATE_ACCOUNT", "PSC_GATE_HARDENING_PROFILE",
                "PSC_GATE_JOB_TEMPLATE_UNIT", "GITHUB_TOKEN",
            ):
                self.assertNotIn(leaked, env, leaked)

    def test_the_manager_rewrites_the_authoritative_ledger_itself(self) -> None:
        """#628 的作者歸屬：權威 ledger 由 Manager 寫，gate 的那一份留在 spool。"""
        with tempfile.TemporaryDirectory() as root:
            out = self._run(root=root, payload=_ok_payload())
            written = Path(out["ledger_path"])
            self.assertTrue(written.is_file())
            self.assertIsNone(terminal_contract.foreign_evidence_author(written))
            found = terminal_contract.read_gate_ledger(written)
            self.assertIsNotNone(found)
            self.assertEqual([g["name"] for g in found[0]["gates"]], ["pytest"])

    def test_a_missing_spool_ledger_fails_closed_with_the_unit_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(gate_runner.GateRunnerError) as ctx:
                self._run(root=root, payload=None, returncode=4)
            self.assertEqual(ctx.exception.reason, "gate-spool-empty")
            instance = job_runner.template_instance_id("psc-0629-build")
            self.assertIn(f"cortex-gate-job@{instance}.service", str(ctx.exception))
            self.assertIn("systemctl_exit=4", str(ctx.exception))
            self.assertIn(GATE_ACCOUNT, str(ctx.exception))

    def test_an_undeclared_gate_name_in_the_spool_is_refused(self) -> None:
        """spool 是不受信任輸入：被攻陷的 gate 不得發明 operator 沒宣告過的 gate 名。"""
        payload = _ok_payload()
        payload["gates"].append(
            {"name": "totally-made-up", "command": "", "exit_code": 0,
             "status": "passed", "detail": ""}
        )
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(gate_runner.GateRunnerError) as ctx:
                self._run(root=root, payload=payload)
            self.assertEqual(ctx.exception.reason, "gate-spool-unknown-gate")

    def test_exit_code_overrides_the_self_reported_status(self) -> None:
        """記了非 0 exit code 卻標 passed ⇒ 以 exit code 為準（與採信端同一條紀律）。"""
        payload = _ok_payload()
        payload["gates"][0]["exit_code"] = 1
        payload["gates"][0]["status"] = "passed"
        with tempfile.TemporaryDirectory() as root:
            result = self._run(root=root, payload=payload)["result"]
            self.assertEqual(result["gates"][0]["status"], "failed")

    def test_an_oversized_spool_payload_is_refused_or_truncated(self) -> None:
        payload = _ok_payload()
        payload["gates"][0]["detail"] = "x" * 100_000
        with tempfile.TemporaryDirectory() as root:
            result = self._run(root=root, payload=payload)["result"]
            self.assertLessEqual(
                len(result["gates"][0]["detail"]), gate_runner.MAX_DETAIL_CHARS
            )
        flooded = _ok_payload()
        flooded["gates"] = flooded["gates"] * (gate_runner.MAX_LEDGER_ROWS + 1)
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(gate_runner.GateRunnerError) as ctx:
                self._run(root=root, payload=flooded)
            self.assertEqual(ctx.exception.reason, "gate-spool-invalid")

    def test_a_malformed_spool_payload_is_refused(self) -> None:
        for payload in ({"kind": "nope"}, {"gates": "not-a-list"}, []):
            with tempfile.TemporaryDirectory() as root:
                with self.assertRaises(gate_runner.GateRunnerError) as ctx:
                    self._run(root=root, payload=payload)
                self.assertEqual(ctx.exception.reason, "gate-spool-invalid", payload)

    def test_the_gate_role_refuses_an_executor(self) -> None:
        """gate 不跑模型 ⇒ 剖面不得跟著 `PSC_MANAGER_EXECUTOR` 漂移。"""
        with mock.patch.dict(os.environ, {}, clear=True), _nested(_preflight_patches()):
            with self.assertRaises(job_runner.JobRunnerError):
                job_runner.prepare_systemd_template(
                    {
                        job_runner.JOB_SPEC_SPOOL_ENV: "/tmp",
                        job_runner.GATE_JOB_SPEC_SPOOL_ENV: "/tmp",
                        job_runner.BUILDER_PATH_ENV: "/opt/cortex/toolchain/bin:/usr/bin",
                        job_runner.GATE_PATH_ENV: "/opt/cortex/toolchain/bin:/usr/bin",
                    },
                    job_id="j", executor="codex", role=job_runner.JOB_ROLE_GATE,
                )

    def test_model_roles_still_require_an_executor(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), _nested(_preflight_patches()):
            with self.assertRaises(job_runner.JobRunnerError):
                job_runner.prepare_systemd_template(
                    {
                        job_runner.JOB_SPEC_SPOOL_ENV: "/tmp",
                        job_runner.GATE_JOB_SPEC_SPOOL_ENV: "/tmp",
                        job_runner.BUILDER_PATH_ENV: "/opt/cortex/toolchain/bin:/usr/bin",
                        job_runner.GATE_PATH_ENV: "/opt/cortex/toolchain/bin:/usr/bin",
                    },
                    job_id="j", executor=None, role=job_runner.JOB_ROLE_BUILDER,
                )

    def test_the_gate_profile_is_an_operator_decision_and_fails_closed(self) -> None:
        self.assertEqual(job_runner.resolve_gate_hardening_profile({}), "strict")
        self.assertEqual(
            job_runner.resolve_gate_hardening_profile(
                {job_runner.GATE_HARDENING_PROFILE_ENV: "jit"}
            ),
            "jit",
        )
        with self.assertRaises(job_runner.JobRunnerError):
            job_runner.resolve_gate_hardening_profile(
                {job_runner.GATE_HARDENING_PROFILE_ENV: "jti"}
            )

    def test_the_interpreter_must_be_absolute(self) -> None:
        self.assertEqual(
            gate_runner.resolve_gate_python({}), gate_runner.DEFAULT_GATE_PYTHON
        )
        with self.assertRaises(gate_runner.GateRunnerError):
            gate_runner.resolve_gate_python({gate_runner.GATE_PYTHON_ENV: "python3"})


# ---------------------------------------------------------------------------
# 7. 拋棄式副本
# ---------------------------------------------------------------------------

class SnapshotTests(unittest.TestCase):
    def test_the_snapshot_is_a_real_copy_and_the_source_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "src"
            (src / "pkg").mkdir(parents=True)
            (src / "pkg" / "conftest.py").write_text("x = 1\n", encoding="utf-8")
            dst = Path(root) / "snap"
            gate_runner_out = gate_ledger.snapshot_worktree(src, dst)
            self.assertEqual(gate_runner_out, dst)
            self.assertEqual(
                (dst / "pkg" / "conftest.py").read_text(encoding="utf-8"), "x = 1\n"
            )
            # gate 在副本上寫入（`.pytest_cache` 等）不影響來源。
            (dst / ".pytest_cache").mkdir()
            self.assertFalse((src / ".pytest_cache").exists())

    def test_symlinks_are_copied_as_symlinks_never_followed(self) -> None:
        """跟隨 symlink 會把樹外內容複製進 gate 的可寫區，或走進無界遞迴。"""
        with tempfile.TemporaryDirectory() as root:
            outside = Path(root) / "outside"
            outside.mkdir()
            (outside / "secret").write_text("s3cret", encoding="utf-8")
            src = Path(root) / "src"
            src.mkdir()
            (src / "escape").symlink_to(outside)
            (src / "self").symlink_to(src)
            dst = Path(root) / "snap"
            gate_ledger.snapshot_worktree(src, dst)
            self.assertTrue((dst / "escape").is_symlink())
            self.assertTrue((dst / "self").is_symlink())
            self.assertFalse((dst / "escape").joinpath("secret").is_file()
                             and not (dst / "escape").is_symlink())

    def test_unreadable_regenerable_caches_are_skipped_by_name(self) -> None:
        """#736：builder 在 `UMask=0077` 下產生的 `.pytest_cache` mode 0700 ⇒ ACL
        mask `---` ⇒ gate 讀不到；而它是可再生快取、不是候選樹內容，snapshot 依名
        跳過而不是整格死。"""
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "src"
            (src / "tests").mkdir(parents=True)
            (src / "tests" / "test_red.py").write_text("def test(): ...\n", encoding="utf-8")
            unreadable = src / ".pytest_cache"
            unreadable.mkdir()
            (unreadable / "CACHEDIR.TAG").write_text("Signature", encoding="utf-8")
            nested = src / "tests" / "__pycache__"
            nested.mkdir()
            os.chmod(unreadable, 0)
            os.chmod(nested, 0)
            try:
                dst = Path(root) / "snap"
                gate_ledger.snapshot_worktree(src, dst)
                self.assertTrue((dst / "tests" / "test_red.py").is_file())
                # 跳過＝不進副本，任意深度皆然。
                self.assertFalse((dst / ".pytest_cache").exists())
                self.assertFalse((dst / "tests" / "__pycache__").exists())
            finally:
                os.chmod(unreadable, 0o700)
                os.chmod(nested, 0o700)

    def test_an_unreadable_non_cache_entry_still_fails_closed(self) -> None:
        """清單外的不可讀項目照樣 `SnapshotError`——「跳過讀不到的東西」是禁手：
        候選內容讀不到時靜默跳過會讓 gate 在殘缺的樹上判出假 verdict。"""
        if os.geteuid() == 0:  # pragma: no cover - root 不受 mode 0 限制
            self.skipTest("mode 0 對 root 不生效")
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "src"
            src.mkdir()
            candidate = src / "tests"
            candidate.mkdir()
            os.chmod(candidate, 0)
            try:
                with self.assertRaises(gate_ledger.SnapshotError):
                    gate_ledger.snapshot_worktree(src, Path(root) / "snap")
            finally:
                os.chmod(candidate, 0o700)

    def test_a_stale_snapshot_is_rebuilt_not_merged(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "src"
            src.mkdir()
            (src / "new.txt").write_text("new", encoding="utf-8")
            dst = Path(root) / "snap"
            dst.mkdir()
            (dst / "leftover.txt").write_text("old", encoding="utf-8")
            gate_ledger.snapshot_worktree(src, dst)
            self.assertFalse((dst / "leftover.txt").exists())
            self.assertTrue((dst / "new.txt").is_file())

    def test_overlapping_destinations_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "src"
            src.mkdir()
            for dst in (src, src / "inner"):
                with self.assertRaises(gate_ledger.SnapshotError):
                    gate_ledger.snapshot_worktree(src, dst)

    def test_a_missing_source_fails_closed_without_writing_a_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "ledger.json"
            rc = gate_ledger.main(
                [
                    "--out", str(out),
                    "--worktree", str(Path(root) / "snap"),
                    "--snapshot-from", str(Path(root) / "does-not-exist"),
                ]
            )
            self.assertEqual(rc, 74)
            # **一份 ledger 都不寫**：寫「全部 failed」會把「沒驗到」偽裝成「驗過沒過」。
            self.assertFalse(out.exists())


# ---------------------------------------------------------------------------
# 8. direct 模式零回歸
# ---------------------------------------------------------------------------

class DirectModeParityTests(unittest.TestCase):
    """`direct` 下 builder 與 Manager 同 UID，第四個身分沒有邊界可言——行為不變。"""

    def test_direct_mode_writes_the_ledger_in_process_exactly_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            worktree = Path(root) / "wt"
            worktree.mkdir()
            ledger = Path(root) / "out.gates.json"
            env = {"PSC_SLICE_ID": "s1", job_runner.JOB_RUNNER_ENV: "direct"}
            with mock.patch.object(
                gate_ledger, "write_gate_ledger", wraps=gate_ledger.write_gate_ledger
            ) as spy:
                payload = gate_runner.run_declared_gates(
                    job_id="j", spool_key="s1", ledger_path=ledger,
                    worktree=worktree, env=env,
                )
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(payload["gates"], [])
            self.assertTrue(ledger.is_file())

    def test_direct_mode_never_creates_a_gate_spool_or_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            worktree = Path(root) / "wt"
            worktree.mkdir()
            coordinator_root = Path(root) / "coordinator"
            with mock.patch.object(gate_runner, "prepare_gate_spool") as spy:
                gate_runner.run_declared_gates(
                    job_id="j", spool_key="s1",
                    ledger_path=Path(root) / "out.gates.json",
                    worktree=worktree,
                    env={job_runner.JOB_RUNNER_ENV: "direct"},
                    coordinator_root=coordinator_root,
                )
            spy.assert_not_called()
            self.assertFalse(coordinator_root.exists())

    def test_ensure_gate_ledger_is_a_no_op_in_direct_mode(self) -> None:
        job = {
            "workflow_phase": "build",
            "log_path": "/tmp/psc-0629-build.jsonl",
            "worktree": "/tmp",
        }
        self.assertIsNone(
            gate_runner.ensure_gate_ledger(
                job, phases=frozenset({"build"}),
                env={job_runner.JOB_RUNNER_ENV: "direct"},
            )
        )

    def test_ensure_gate_ledger_skips_the_phases_that_never_ran_gates(self) -> None:
        env = {job_runner.JOB_RUNNER_ENV: job_runner.RUNNER_SYSTEMD_TEMPLATE}
        for phase in ("plan", "review", "verify"):
            job = {"workflow_phase": phase, "log_path": "/tmp/a.jsonl", "worktree": "/tmp"}
            self.assertIsNone(
                gate_runner.ensure_gate_ledger(
                    job, phases=frozenset({"build"}), env=env
                ),
                phase,
            )

    def test_ensure_gate_ledger_never_overwrites_an_existing_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "psc-0629-build.jsonl"
            log_path.write_text("", encoding="utf-8")
            terminal_contract.gate_ledger_path(log_path).write_text("{}", encoding="utf-8")
            job = {
                "workflow_phase": "build",
                "log_path": str(log_path),
                "worktree": root,
            }
            self.assertIsNone(
                gate_runner.ensure_gate_ledger(
                    job,
                    phases=frozenset({"build"}),
                    env={job_runner.JOB_RUNNER_ENV: job_runner.RUNNER_SYSTEMD_TEMPLATE},
                )
            )


# ---------------------------------------------------------------------------
# 9. `_regenerate_gates_action` 走同一條執行面
# ---------------------------------------------------------------------------

class RegenerateGatesConvergenceTests(unittest.TestCase):
    """#629：手動救援不得留在 Manager 進程內跑 builder 交出來的 `conftest.py`。"""

    def test_the_action_calls_the_shared_entry_point_not_write_gate_ledger(self) -> None:
        from paulsha_cortex.coordinator import work_actions

        # docstring 會提到舊寫法（那是它為什麼改的說明），因此只看**程式碼本體**。
        # 用 `ast` 剝掉 docstring 而不是 `source.replace(fn.__doc__, "")`：後者在
        # Python 3.13 會失效——3.13 起 `__doc__` 的共同前置縮排在編譯期就被移除，
        # 與原始碼裡的字串不再逐字相等（3.10–3.12 相等，因此那個寫法只在部分版本綠）。
        body = _function_body_source(work_actions._regenerate_gates_action)
        self.assertIn("gate_runner.run_declared_gates(", body)
        self.assertNotIn("gate_ledger.write_gate_ledger(", body)

    def test_the_manager_runs_the_gate_before_the_acceptance_read(self) -> None:
        """證據必須在被讀之前落地，且**不得**在採信開始之後才產生。"""
        import inspect

        from paulsha_cortex.coordinator import manager

        source = inspect.getsource(manager.terminalize_workflow_job)
        run_at = source.index("_run_gate_execution_identity(job, registry=registry)")
        read_at = source.index("_assert_terminal_gate_consistency(raw")
        self.assertLess(run_at, read_at)

    def test_gate_execution_phases_match_the_phases_that_require_a_ledger(self) -> None:
        from paulsha_cortex.coordinator import manager

        self.assertEqual(
            manager.GATE_EXECUTION_PHASES, manager.GATE_LEDGER_REQUIRED_PHASES
        )


# ---------------------------------------------------------------------------
# 10. 單 UID 下測不出來的：明確 skip（#638 的教訓）
# ---------------------------------------------------------------------------

class RealIsolationTests(unittest.TestCase):
    """本組每一項都需要 root ＋ 四個真實帳號 ＋ 已套 ACL 的樹。

    在 CI 容器裡跑出來的「綠」不代表任何事——單 UID 下 `wx` 無 `r` 的 ACL 根本不會
    被評估，`sudo -u` 也不可得。因此一律 skip 並指向 runbook 的實機步驟，**不**用
    裸跑當替身（那只會產生一個永遠綠、什麼都沒驗到的測試）。
    """

    @unittest.skipUnless(
        REAL_HARDENING,
        "需要 root ＋ `cortex-gate` 帳號：驗 gate 對 builder 工作樹的 `rX` 真的成立"
        "（讀得到、寫不進）。runbook 第 9b 步。",
    )
    def test_real_gate_can_read_but_not_write_the_builder_worktree(self) -> None:  # pragma: no cover
        raise AssertionError("實機測項（runbook 9b）。")

    @unittest.skipUnless(
        REAL_HARDENING,
        "需要已套 ACL 的 spool：驗 gate 寫得進自己那格、**讀不到**別人那格"
        "（`wx` 無 `r`），以及 seal 之後連自己那格都進不去。runbook 第 9c 步。",
    )
    def test_real_spool_is_write_only_for_the_gate_account(self) -> None:  # pragma: no cover
        raise AssertionError("實機測項（runbook 9c）。")

    @unittest.skipUnless(
        REAL_HARDENING,
        "需要真實 systemd：驗 gate unit 真的以 `uid=cortex-gate` 執行，且對 "
        "Manager 的 dispatch log 目錄寫入為 EROFS。runbook 第 9a 步。",
    )
    def test_real_gate_unit_runs_as_the_gate_uid(self) -> None:  # pragma: no cover
        raise AssertionError("實機測項（runbook 9a）。")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
