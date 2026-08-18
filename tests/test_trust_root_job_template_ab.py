"""operator 0816 第三輪裁決 **A+B** 的程式碼側驗收（#584）。

A＝三分 UID 定案（`permgen.DEFAULT_SCHEME`）；B＝root-owned 模板 unit ＋
Manager-owned spec spool ＋ root-owned shim（`PSC_JOB_RUNNER=systemd-template`）。

覆蓋面：

1. **三分定案**——`DEFAULT_SCHEME` 是三分、CLI 未指定時出三分、二分仍可顯式取得。
2. **模板模式 argv／spec**——argv 封閉（只有 unit 名可變）、spec **不含身分欄位**、
   spec 的 env **不含任何 token 類**、spec 原子落地且 mode 讓 job 讀得到。
3. **spool 資產入登記表**——`job-spec-spool` 在 R1 登記表、writer 只有 Manager、
   builder 零寫入（permgen 不變式）、layout 對得上 `config.paths`。
4. **fail-fast 三案**——模板未安裝／shim 未安裝／spool 缺席、instance 已在跑、
   spec 寫入失敗，一律 `DiagnosticReason` fail-closed，且**不退回其他模式**。
5. **direct／systemd-run 零回歸**——第三種模式存在不改前兩種的任何一個位元組。
6. **shim**——stub 內容由 permgen 產生；shim 邏輯模組對 symlink／schema／身分欄位／
   憑證 env 逐條 fail-closed，成功路徑接管 log、chdir、exec。

全程不跑 systemctl、不建帳號、不碰 polkit、不安裝任何 unit。
"""
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import paulsha_cortex.coordinator.job_runner as job_runner
import paulsha_cortex.coordinator.job_shim as job_shim
import paulsha_cortex.coordinator.launcher as launcher_module
from paulsha_cortex.config import paths as config_paths
from paulsha_cortex.coordinator.job_runner import JobRunnerError
from paulsha_cortex.coordinator.launcher import SubprocessLauncher
from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.registry import Principal

_ISOLATED_AGENTS_ROOT = tempfile.mkdtemp(prefix="psc-agents-root-")

_BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    # #679：job 的 PATH 只由本角色的 `PSC_*_PATH` 決定（daemon 的 PATH 不再轉發），
    # 未宣告即 fail-closed。三個角色各給一份，測試才驗得到「不會互相污染」。
    "PSC_BUILDER_PATH": "/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin",
    "PSC_REVIEWER_PATH": "/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin",
    "PSC_GATE_PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin",
    "HOME": "/var/lib/cortex-manager",
    # conftest 的 `_clear_runtime_env` 把 PSC_AGENTS_ROOT 指向 per-test 暫存目錄，
    # 但本檔的 launch 測試以 `clear=True` 重建整份 environ（要驗的就是白名單本身），
    # 那層保護因此被清掉。顯式帶上一個 per-process 暫存根：`launcher.launch()` 會在
    # coordinator 樹底下建這個 job 的成果 bundle spool（#623），少了它會落到 operator
    # 的真實 `$HOME`——正是 conftest #303 註記要防的事。
    "PSC_AGENTS_ROOT": _ISOLATED_AGENTS_ROOT,
    "LANG": "en_US.UTF-8",
}

_SECRET_ENV = {
    "GH_TOKEN": "gh-secret",
    "GITHUB_TOKEN": "github-secret",
    "COPILOT_GITHUB_TOKEN": "copilot-secret",
    "ANTHROPIC_API_KEY": "anthropic-secret",
    "OPENAI_API_KEY": "openai-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "CLAUDE_CONFIG_DIR": "/var/lib/cortex-manager/.claude",
    "GH_CONFIG_DIR": "/var/lib/cortex-manager/.config/gh",
    "BASH_ENV": "/tmp/credential-exporter",
    "NODE_OPTIONS": "--require /tmp/inject.js",
    "PGPASSWORD": "postgres-secret",
}


class _FakeProc:
    def __init__(self, *, pid: int = 5150, exit_status: int | None = None) -> None:
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


def _template_env(spool: str, **overrides: str) -> dict[str, str]:
    env = {
        **_BASE_ENV,
        **_SECRET_ENV,
        job_runner.JOB_RUNNER_ENV: job_runner.RUNNER_SYSTEMD_TEMPLATE,
        # #657：spool 是 per-principal 的（builder／review／gate 各一個變數）。
        # 測試把三個都指向同一個暫存目錄——這裡驗的是「spec 內容與起動形狀」，
        # 不是隔離；隔離由 `tests/test_per_principal_spec_spool_657.py` 驗。
        job_runner.JOB_SPEC_SPOOL_ENV: spool,
        job_runner.REVIEW_JOB_SPEC_SPOOL_ENV: spool,
        job_runner.GATE_JOB_SPEC_SPOOL_ENV: spool,
    }
    env.update(overrides)
    return env


def _preflight_patches(*, unit_active: bool = False, **overrides):
    defaults = {
        "which": mock.patch.object(
            job_runner.shutil, "which", return_value="/usr/bin/systemctl"
        ),
        "booted": mock.patch.object(job_runner, "_systemd_booted", return_value=True),
        "account": mock.patch.object(job_runner, "_account_exists", return_value=True),
        "group": mock.patch.object(job_runner, "_group_exists", return_value=True),
        "unit_file": mock.patch.object(
            job_runner, "_unit_file_installed", return_value=True
        ),
        "shim": mock.patch.object(job_runner, "_is_executable", return_value=True),
        "active": mock.patch.object(
            job_runner, "_unit_is_active", return_value=unit_active
        ),
        # #657：preflight 現在會算「該 job 身分讀不讀得到自己的 spool」的
        # **effective** 權限。上面那些 seam 宣稱的帳號（cortex-builder…）在單 UID
        # 的開發機／CI 上並不存在，因此這裡把同一條 seam 一併 stub 掉——真正驗這條
        # 語意的是 `tests/test_per_principal_spec_spool_657.py`（自建真實 ACL 樹）。
        "spool_readable": mock.patch.object(
            job_runner, "_spool_readable_by", return_value=(True, "")
        ),
        "spec_readable": mock.patch.object(
            job_runner, "_spec_readable_by", return_value=(True, "")
        ),
    }
    defaults.update(overrides)
    return list(defaults.values())


def _launch_template(
    launcher: SubprocessLauncher,
    *,
    popen: _RecordingPopen,
    slice_id: str = "psc-0042-template",
    spool: str | None = None,
    env_overrides: dict[str, str] | None = None,
    patches=None,
    workdir: str | None = None,
) -> dict[str, str]:
    """在完全受控的 seam 下跑一次 template 模式 launch，回傳現場路徑。"""

    original = launcher_module.subprocess.Popen
    launcher_module.subprocess.Popen = popen
    created = tempfile.TemporaryDirectory() if workdir is None else None
    root = workdir or created.name  # type: ignore[union-attr]
    try:
        spool_dir = spool if spool is not None else str(Path(root) / "job-specs")
        Path(spool_dir).mkdir(parents=True, exist_ok=True)
        env = _template_env(spool_dir, **(env_overrides or {}))
        log_dir = str(Path(root) / "logs")
        with mock.patch.dict(os.environ, env, clear=True):
            with _nested(_preflight_patches() if patches is None else patches):
                launcher.launch(
                    slice_id=slice_id,
                    prompt="PROMPT",
                    worktree=root,
                    log_dir=log_dir,
                )
        return {
            "root": root,
            "spool": spool_dir,
            "log_dir": log_dir,
            "log_path": str(Path(log_dir) / f"{slice_id}.jsonl"),
        }
    finally:
        launcher_module.subprocess.Popen = original
        if created is not None:
            created.cleanup()


def _only_spec(spool: str) -> dict:
    files = sorted(Path(spool).glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# A：三分 UID 定案
# ---------------------------------------------------------------------------

class ThreeWaySchemeIsTheDecisionTests(unittest.TestCase):
    def test_default_scheme_is_the_widest_split_available(self) -> None:
        """預設永遠是**分得最細**的那一個方案（#629 起＝四分）。

        0816 第三輪裁決 A 的性質是「預設就是最安全的那一個，要退回較寬鬆的方案必須
        顯式打出來」。#629 把定案往前推一格（多出 `cortex-gate`），因此這裡斷言的
        是那個**性質**而不是某個字面 id：預設方案的帳號數必須 ≥ 其餘每一個方案，
        且三分／二分都仍在表上、都要顯式指定才拿得到。
        """
        self.assertIs(permgen.DEFAULT_SCHEME, permgen.FOUR_WAY_SCHEME)
        self.assertEqual(permgen.DEFAULT_SCHEME_ID, "four-way")
        widest = max(
            len(s.declared_accounts()) for s in permgen.SCHEMES.values()
        )
        self.assertEqual(len(permgen.DEFAULT_SCHEME.declared_accounts()), widest)
        for legacy_id in ("two-way", "three-way"):
            self.assertIn(legacy_id, permgen.SCHEMES)
            self.assertIsNot(permgen.SCHEMES[legacy_id], permgen.DEFAULT_SCHEME)

    def test_three_way_remains_available_and_declares_no_gate_identity(self) -> None:
        """三分沒有第四個帳號——而且那是**明示的決定**，不是遺漏（#629）。"""
        three = permgen.THREE_WAY_SCHEME
        self.assertEqual(
            three.account_of[Principal.GATE], permgen.ABSENT_ACCOUNT
        )
        self.assertIsNone(three.resolve(Principal.GATE))
        # 明示不存在 ⇒ 不算未對應，產生器照樣輸出得了三分的其餘部分。
        self.assertNotIn(Principal.GATE, three.unresolved_principals())
        self.assertNotIn("cortex-gate", three.declared_accounts())

    def test_three_way_splits_the_model_personas_off_the_spawn_authority(self) -> None:
        """裁決的判準：持 spawn 授權的帳號不得跑任何模型程式碼。"""
        scheme = permgen.DEFAULT_SCHEME
        manager = scheme.durable_state_owner
        self.assertEqual(manager, "cortex-manager")
        # polkit 的 subject（＝持 start 授權者）就是 durable_state_owner。
        rule = permgen.build_polkit_rule(scheme)
        self.assertEqual(rule.subject_account, manager)
        # 三個跑模型的 persona 全部落在其他帳號。
        for persona in (Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER):
            self.assertNotEqual(scheme.resolve(persona), manager, persona)

    def test_two_way_remains_available_for_backwards_compatibility(self) -> None:
        self.assertIn("two-way", permgen.SCHEMES)
        self.assertEqual(permgen.SCHEMES["two-way"], permgen.TWO_WAY_SCHEME)

    def test_cli_defaults_to_three_way_and_two_way_needs_an_explicit_token(self) -> None:
        from paulsha_cortex.trust_root.__main__ import main

        for argv, expected in (
            (["unit", "--job"], "cortex-builder"),
            (["unit", "--manager"], "cortex-manager"),
            (["unit", "two-way", "--manager"], "cortex-svc"),
        ):
            with mock.patch("sys.stdout") as out:
                self.assertEqual(main(argv), 0)
            printed = "".join(
                str(call.args[0]) for call in out.write.call_args_list if call.args
            )
            self.assertIn(f"User={expected}", printed, argv)

    def test_cli_polkit_defaults_to_plan_b(self) -> None:
        from paulsha_cortex.trust_root.__main__ import main

        with mock.patch("sys.stdout") as out:
            self.assertEqual(main(["polkit"]), 0)
        printed = "".join(
            str(call.args[0]) for call in out.write.call_args_list if call.args
        )
        self.assertIn("方案 B", printed)
        self.assertIn("cortex-job@", printed)

    def test_every_permgen_invariant_runs_on_three_way(self) -> None:
        """三分下同一套 policy 仍成立：headless 帳號對 Manager-owned 樹零寫入。"""
        scheme = permgen.DEFAULT_SCHEME
        plan = permgen.generate_plan(scheme)
        headless = scheme.headless_accounts()
        self.assertEqual(len(plan.entries), len(registry.ASSET_REGISTRY))
        for entry in plan.entries:
            if entry.owner_class is not permgen.OwnerClass.MANAGER_STATE:
                continue
            self.assertFalse(
                plan.all_writable_accounts(entry) & headless,
                (entry.asset_id, plan.all_writable_accounts(entry)),
            )


class PolkitPlanBTests(unittest.TestCase):
    """B 案的 polkit 輸出：subject／action／verb／unit pattern 全部收窄。"""

    def _rule(self):
        return permgen.build_polkit_rule(
            permgen.DEFAULT_SCHEME, permgen.DEFAULT_LAYOUT, plan=permgen.PolkitPlan.TEMPLATE
        )

    def test_subject_is_cortex_manager(self) -> None:
        rule = self._rule()
        self.assertEqual(rule.subject_account, "cortex-manager")
        self.assertIn('subject.user !== "cortex-manager"', rule.content)

    def test_action_and_verbs(self) -> None:
        rule = self._rule()
        self.assertEqual(rule.allowed_verbs, ("start", "stop"))
        self.assertIn(
            'action.id !== "org.freedesktop.systemd1.manage-units"', rule.content
        )
        for verb in ("reload", "mask", "set-property", "restart"):
            self.assertEqual(
                permgen.evaluate_polkit(
                    rule,
                    user="cortex-manager",
                    action_id=permgen.POLKIT_ACTION,
                    unit="cortex-job@x.service",
                    verb=verb,
                ),
                "NO",
                verb,
            )

    def test_unit_pattern_is_the_template_instance_shape(self) -> None:
        rule = self._rule()
        # #643：pattern 改成「每個加固剖面一個字幹」的列舉交替，因此不再是單一
        # 字面前綴；語意（放行 strict 模板的實例）逐字不變。
        self.assertTrue(rule.unit_pattern.startswith("^"))
        self.assertTrue(rule.unit_pattern.endswith(r"@[a-z0-9][a-z0-9._-]{0,62}\.service$"))
        self.assertEqual(
            permgen.evaluate_polkit(
                rule,
                user="cortex-manager",
                action_id=permgen.POLKIT_ACTION,
                unit="cortex-job@psc-0042-deadbeef.service",
                verb="start",
            ),
            "YES",
        )

    def test_transient_units_are_never_authorised(self) -> None:
        """B 案的重點：manage-units 對 transient unit 一律不放行。

        transient 有兩種形狀會出現在 `action.lookup("unit")`：A 案自己命名的
        `cortex-job-<id>.service`，以及 systemd 對匿名 transient 自動生成的
        `run-uNNNN.service`。兩者都必須是 NO，否則「不能傳 User=root」就破功了。
        """
        rule = self._rule()
        for unit in (
            "cortex-job-psc-0042-deadbeef.service",
            "run-u1234.service",
            "run-r9c0ffee.service",
            "cortex-job.service",
        ):
            self.assertEqual(
                permgen.evaluate_polkit(
                    rule,
                    user="cortex-manager",
                    action_id=permgen.POLKIT_ACTION,
                    unit=unit,
                    verb="start",
                ),
                "NO",
                unit,
            )
        # 明細缺席（StartTransientUnit 的 polkit 檢查不帶 unit 明細）→ 拒。
        self.assertEqual(
            permgen.evaluate_polkit(
                rule, user="cortex-manager", action_id=permgen.POLKIT_ACTION,
                unit=None, verb=None,
            ),
            "NO",
        )

    def test_plan_b_has_no_residual_os_level_risk(self) -> None:
        self.assertEqual(self._rule().residual_risks, ())


class SchemeDerivedHomeTests(unittest.TestCase):
    """三分定案暴露的兩個既有缺口（#614 runbook 實測）——HOME 與 scaffold。

    兩者的根是同一條：帳號相關路徑被寫成二分時代的字面量，換 scheme 就漂移。
    修法也是同一條：由 `scheme` 現場導出，不列舉。
    """

    def test_manager_home_follows_the_scheme_account(self) -> None:
        layout = permgen.DEFAULT_LAYOUT
        two = permgen.build_manager_unit(permgen.TWO_WAY_SCHEME, layout)
        three = permgen.build_manager_unit(permgen.THREE_WAY_SCHEME, layout)
        self.assertIn("Environment=HOME=/var/lib/cortex-svc\n", two.content)
        self.assertIn("Environment=HOME=/var/lib/cortex-manager\n", three.content)
        self.assertIn("Environment=XDG_CACHE_HOME=/var/lib/cortex-manager/cache\n", three.content)
        # 三分下絕不可再出現二分時代的帳號樹。
        self.assertNotIn("cortex-svc", three.content)

    def test_job_home_follows_the_resolved_job_account(self) -> None:
        layout = permgen.DEFAULT_LAYOUT
        unit = permgen.build_job_unit(permgen.THREE_WAY_SCHEME, layout)
        self.assertIn("Environment=HOME=/var/lib/cortex-builder\n", unit.content)
        self.assertIn(
            "Environment=XDG_CACHE_HOME=/var/lib/cortex-builder/cache\n", unit.content
        )

    def test_scaffold_covers_every_account_the_scheme_resolves(self) -> None:
        for scheme, expected in (
            (permgen.TWO_WAY_SCHEME, {"cortex-svc", "cortex-builder"}),
            (
                permgen.THREE_WAY_SCHEME,
                {"cortex-manager", "cortex-reviewer-planner", "cortex-builder"},
            ),
        ):
            dirs = permgen.DEFAULT_LAYOUT.scaffold_directories(scheme)
            by_path = {path: (owner, mode) for path, owner, _g, mode in dirs}
            for account in expected:
                home = permgen.DEFAULT_LAYOUT.home_of(account)
                cache = permgen.DEFAULT_LAYOUT.cache_of(account)
                self.assertIn(home, by_path, (scheme.scheme_id, account))
                self.assertEqual(by_path[home], ("root", 0o755), (scheme.scheme_id, account))
                self.assertEqual(by_path[cache], (account, 0o700), (scheme.scheme_id, account))
            # 無多餘：不得殘留任何非本 scheme 帳號的 HOME。
            homes = {
                p for p in by_path if p.startswith(permgen.DEFAULT_LAYOUT.home_root + "/cortex-")
            }
            resolved = {
                permgen.DEFAULT_LAYOUT.home_of(a) for a in expected
            } | {
                permgen.DEFAULT_LAYOUT.cache_of(a) for a in expected
            } | {
                permgen.DEFAULT_LAYOUT.codex_hooks_dir_of(a) for a in expected
            } | {
                # #685：per-(account, executor) 憑證表產生的骨架——`IN_PLACE_FILE` 的
                # root-owned 父目錄，與 `HOME_REDIRECT_TREE` 落在該帳號 `cache` 底下的
                # 目標。**逐格由那張表導出、不手抄**，因此加一格憑證時這裡自動涵蓋；
                # 兩者都仍在本 scheme 解析得到的帳號 HOME 底下，「無多餘」的原意
                # （不得殘留別的 scheme 的帳號樹）未被放寬。
                target
                for _aid, acct, path, cred in permgen.DEFAULT_LAYOUT.credential_placements()
                if acct in expected
                for target in (
                    (permgen.DEFAULT_LAYOUT.credential_target_of(acct, cred),)
                    if cred.shape is permgen.CredentialShape.HOME_REDIRECT_TREE
                    else (path.rsplit("/", 1)[0],)
                )
            } | {
                # #666：durable state owner 另有 root-owned 的 `~/.config` 與
                # `~/.config/gh`（`gh` 的登入態落點）。仍在**本 scheme 解析得到的
                # 帳號**底下，因此「無多餘」這條的原意未被放寬。
                f"{permgen.DEFAULT_LAYOUT.home_of(scheme.durable_state_owner)}/.config",
                permgen.DEFAULT_LAYOUT.gh_config_dir_of(scheme.durable_state_owner),
            }
            self.assertTrue(homes <= resolved, (scheme.scheme_id, homes - resolved))

    def test_every_model_job_account_gets_a_root_owned_codex_dir(self) -> None:
        """跑模型的帳號都不得替換自己的 codex 設定落點。

        **#698 之後兩個帳號的形狀又收斂回同一種**（#685 曾讓它們分岔）：
        `~/.codex` 是 root-owned ＋ sticky 的**真目錄**，登記表資產
        `<前綴>-codex-state`。因此它**不在骨架清單裡**——留在骨架會是第二份真相，
        而 `_dedupe_scaffold()` 只留第一筆 ⇒ 骨架那份 0755 會把 sticky 位靜默蓋掉。

        「job 換不掉」這條性質由兩層守：HOME 是 root-owned（換不掉整棵樹），樹自己是
        root-owned ＋ sticky（換不掉裡面 root 的檔）。
        """
        scheme = permgen.THREE_WAY_SCHEME
        layout = permgen.DEFAULT_LAYOUT
        plan = permgen.generate_plan(scheme)
        paths = layout.asset_paths()
        by_path = {
            path: owner
            for path, owner, _g, _m in layout.scaffold_directories(scheme)
        }
        for account in ("cortex-builder", "cortex-reviewer-planner"):
            codex_home = layout.codex_hooks_dir_of(account)
            # (1) 不在骨架（它是登記表資產，由權限計畫落位）。
            self.assertNotIn(codex_home, by_path)
            # (2) 是那個帳號的 sticky 樹資產，root-owned ＋ sticky。
            asset_id = f"{layout.credential_prefix_of(account)}-codex-state"
            self.assertEqual(paths[asset_id], codex_home)
            entry = plan.by_id(asset_id)
            self.assertEqual(entry.owner, "root")
            self.assertTrue(entry.sticky)
            # (3) HOME 那一層仍是 root-owned（換不掉整棵樹）。
            self.assertEqual(by_path[layout.home_of(account)], "root")
        # #698：codex 已不是 symlink 型資產，因此不在 `symlink_targets()` 上。
        # 仍是 symlink 的那兩格（agy／claude）目標由該帳號自己擁有、落在既有的
        # `cache` 底下（不新增可寫面）——那條性質沒有改變。
        planner = "cortex-reviewer-planner"
        self.assertNotIn("reviewer-planner-codex-state", layout.symlink_targets())
        for asset_id in ("reviewer-planner-agy-state", "reviewer-planner-claude-state"):
            target = layout.symlink_targets()[asset_id]
            self.assertEqual(by_path[target], planner)
            self.assertTrue(target.startswith(layout.cache_of(planner) + "/"), target)

    def test_custom_home_root_needs_no_code_change(self) -> None:
        alt = permgen.PathLayout(home_root="/srv/homes")
        self.assertEqual(alt.home_of("cortex-manager"), "/srv/homes/cortex-manager")
        unit = permgen.build_manager_unit(permgen.THREE_WAY_SCHEME, alt)
        self.assertIn("Environment=HOME=/srv/homes/cortex-manager\n", unit.content)


class M2ExtensionPointTests(unittest.TestCase):
    """#615（M2：reviewer/planner 啟動面降權）——第二實例化確實是換參數。

    這個類原本釘的是「擴充點存在且真的通」（M2 未實作時的前置驗收）。M2 落地後
    改釘「擴充點真的只換了參數」：`build_job_unit`／`build_polkit_rule` 的內部沒有
    任何 `if principal is …`，兩個角色的差異全部由 scheme 的帳號映射帶出來。
    """

    def test_a_second_template_instance_is_a_parameter_change(self) -> None:
        scheme = permgen.DEFAULT_SCHEME
        layout = permgen.DEFAULT_LAYOUT
        unit = permgen.build_job_unit(scheme, layout, principal=Principal.REVIEWER)
        self.assertEqual(unit.unit_name, "cortex-reviewer-job@.service")
        self.assertEqual(unit.account, "cortex-reviewer-planner")
        self.assertIn("User=cortex-reviewer-planner\n", unit.content)
        self.assertIn("Environment=HOME=/var/lib/cortex-reviewer-planner\n", unit.content)
        # 與 builder 的模板互不重疊（unit 名不同 ⇒ 每一份的授權面各自具名）。
        builder_unit = permgen.build_job_unit(scheme, layout)
        self.assertNotEqual(unit.unit_name, builder_unit.unit_name)
        self.assertNotEqual(unit.account, builder_unit.account)

    def test_the_deployed_polkit_rule_covers_every_downgraded_role(self) -> None:
        scheme = permgen.DEFAULT_SCHEME
        rule = permgen.build_polkit_rule(scheme, plan=permgen.PolkitPlan.TEMPLATE)
        for principal in permgen.DOWNGRADED_JOB_PRINCIPALS:
            for profile in permgen.HARDENING_PROFILES:
                stem = permgen.job_unit_stem(permgen.DEFAULT_LAYOUT, principal, profile)
                self.assertEqual(
                    permgen.evaluate_polkit(
                        rule,
                        user=rule.subject_account,
                        action_id=permgen.POLKIT_ACTION,
                        unit=f"{stem}@x.service",
                        verb="start",
                    ),
                    "YES",
                    stem,
                )
        self.assertEqual(
            rule.target_accounts,
            ("cortex-builder", "cortex-reviewer-planner", "cortex-gate"),
        )
        # 仍然只有一個放行出口。
        self.assertEqual(rule.content.count("polkit.Result.YES"), 1)
        self.assertEqual(rule.content.count("polkit.addRule("), 1)

    def test_builder_stem_is_unchanged_by_the_extension_point(self) -> None:
        self.assertEqual(
            permgen.job_unit_stem(permgen.DEFAULT_LAYOUT, Principal.BUILDER), "cortex-job"
        )
        self.assertEqual(
            job_runner.DEFAULT_TEMPLATE_UNIT,
            f"{permgen.job_unit_stem(permgen.DEFAULT_LAYOUT)}@.service",
        )

    def test_runner_side_second_instance_is_a_role_lookup(self) -> None:
        """啟動器側的第二實例化＝查一張表，兩個角色不共用一組 env 變數。"""
        self.assertEqual(
            job_runner.resolve_template_unit({}, role=job_runner.JOB_ROLE_REVIEW),
            "cortex-reviewer-job@.service",
        )
        self.assertEqual(
            job_runner.resolve_job_account({}, role=job_runner.JOB_ROLE_REVIEW),
            "cortex-reviewer-planner",
        )
        self.assertEqual(job_runner.resolve_template_unit({}), "cortex-job@.service")
        self.assertEqual(job_runner.resolve_job_account({}), "cortex-builder")
        self.assertEqual(
            job_runner.template_unit_name(
                "demo-deadbeef",
                template=job_runner.resolve_template_unit(
                    {}, role=job_runner.JOB_ROLE_REVIEW
                ),
            ),
            "cortex-reviewer-job@demo-deadbeef.service",
        )


# ---------------------------------------------------------------------------
# B：spool 資產入登記表
# ---------------------------------------------------------------------------

class JobSpecSpoolAssetTests(unittest.TestCase):
    def test_asset_is_registered_and_manager_owned(self) -> None:
        """容器（#657 之後 reader 只有 Manager）與 builder 那一格。"""
        container = registry.asset_by_id("job-spec-spool")
        self.assertIs(container.tree, registry.TrustTree.MANAGER_OWNED)
        self.assertEqual(container.writers, (Principal.MANAGER,))
        # #657：容器本身不再授任何 job 帳號——它只是一個 0700 的殼，job 在這一層
        # 只會拿到機械導出的 `--x`（走得進自己那格、列不出別人的）。
        self.assertNotIn(Principal.BUILDER, container.readers)
        self.assertEqual(
            container.path_resolver, "paulsha_cortex.config.paths:job_spec_spool_root"
        )
        asset = registry.asset_by_id(
            registry.job_spec_spool_asset_id(Principal.BUILDER)
        )
        self.assertIs(asset.tree, registry.TrustTree.MANAGER_OWNED)
        self.assertEqual(asset.writers, (Principal.MANAGER,))
        self.assertIn(Principal.BUILDER, asset.readers)
        self.assertEqual(
            asset.path_resolver, "paulsha_cortex.config.paths:job_spec_spool_for"
        )
        self.assertEqual(asset.path_resolver_args, ("builder",))

    def test_registry_equation_still_holds(self) -> None:
        result = registry.check_registry_equation()
        self.assertTrue(result.ok, result.failure_summary())

    def test_builder_has_zero_write_on_the_spool(self) -> None:
        """permgen 不變式：builder 只讀得到 spec，改不了自己的命令列。"""
        for scheme in (permgen.DEFAULT_SCHEME, permgen.TWO_WAY_SCHEME):
            plan = permgen.generate_plan(scheme)
            entry = plan.by_id(registry.job_spec_spool_asset_id(Principal.BUILDER))
            builder = scheme.resolve(Principal.BUILDER)
            writable = plan.all_writable_accounts(entry)
            self.assertEqual(writable, frozenset({scheme.durable_state_owner}), scheme.scheme_id)
            self.assertNotIn(builder, writable, scheme.scheme_id)
            # 讀得到：owner-only mode ＋ per-account 唯讀 ACL。
            self.assertEqual(entry.mode, 0o700, scheme.scheme_id)
            acl = {a.account: a.perms for a in entry.acls}
            self.assertEqual(acl.get(builder), "rX", scheme.scheme_id)
            self.assertFalse(any(a.writable for a in entry.acls), scheme.scheme_id)

    def test_layout_path_matches_the_path_contract(self) -> None:
        expected_dirname = config_paths.JOB_SPEC_SPOOL_DIRNAME
        self.assertTrue(
            permgen.DEFAULT_LAYOUT.job_spec_spool_root.endswith("/" + expected_dirname)
        )
        self.assertEqual(
            permgen.DEFAULT_LAYOUT.asset_paths()["job-spec-spool"],
            permgen.DEFAULT_LAYOUT.job_spec_spool_root,
        )
        # #657：per-principal 那一族同樣要落在 asset_paths()——漏一項的症狀是
        # `plan_to_commands()` 對它用 placeholder 路徑（＝該身分零授權）。
        for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
            self.assertEqual(
                permgen.DEFAULT_LAYOUT.asset_paths()[
                    registry.job_spec_spool_asset_id(principal)
                ],
                f"{permgen.DEFAULT_LAYOUT.job_spec_spool_root}/{principal.value}",
                principal,
            )

    def test_spool_is_not_writable_from_the_job_unit(self) -> None:
        unit = permgen.build_job_unit(permgen.DEFAULT_SCHEME, permgen.DEFAULT_LAYOUT)
        for rwp in unit.read_write_paths:
            self.assertFalse(
                permgen.DEFAULT_LAYOUT.job_spec_spool_root.startswith(rwp.rstrip("/") + "/")
                or permgen.DEFAULT_LAYOUT.job_spec_spool_root == rwp,
                rwp,
            )


class JobRunnerPermgenContractTests(unittest.TestCase):
    """`job_runner` 的模板常數與 permgen layout 是成對契約（不得漂移）。"""

    def test_defaults_match_the_layout(self) -> None:
        layout = permgen.DEFAULT_LAYOUT
        # #657：預設值是 builder **自己那一格**，不是容器。
        self.assertEqual(
            job_runner.DEFAULT_JOB_SPEC_SPOOL,
            layout.job_spec_spool_for(Principal.BUILDER),
        )
        self.assertEqual(job_runner.DEFAULT_JOB_SHIM, layout.job_shim)
        self.assertEqual(
            job_runner.DEFAULT_TEMPLATE_UNIT,
            permgen.build_job_unit(permgen.DEFAULT_SCHEME, layout).unit_name,
        )

    def test_generated_instance_matches_the_polkit_pattern(self) -> None:
        rule = permgen.build_polkit_rule(
            permgen.DEFAULT_SCHEME, plan=permgen.PolkitPlan.TEMPLATE
        )
        for job_id in ("psc-0042-template", "workflow/abc def", "9-leading-digit"):
            instance = job_runner.template_instance_id(job_id)
            unit = job_runner.template_unit_name(instance)
            self.assertEqual(
                permgen.evaluate_polkit(
                    rule,
                    user=rule.subject_account,
                    action_id=permgen.POLKIT_ACTION,
                    unit=unit,
                    verb="start",
                ),
                "YES",
                (job_id, unit),
            )

    def test_builder_account_default_matches_three_way(self) -> None:
        self.assertEqual(
            job_runner.resolve_builder_account({}),
            permgen.DEFAULT_SCHEME.resolve(Principal.BUILDER),
        )


# ---------------------------------------------------------------------------
# B：instance 名與 argv
# ---------------------------------------------------------------------------

class TemplateInstanceNameTests(unittest.TestCase):
    def test_instance_is_traceable_and_unique(self) -> None:
        a = job_runner.template_instance_id("psc-0042-template")
        b = job_runner.template_instance_id("psc/0042 template")
        self.assertIn("psc-0042-template", a)
        self.assertNotEqual(a, b, "消毒後撞形也不得撞名（sha8 後綴）")
        self.assertTrue(job_runner.instance_name_valid(a))
        self.assertTrue(job_runner.instance_name_valid(b))

    def test_empty_job_id_is_fail_closed(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            job_runner.template_instance_id("   ")
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-instance-name-invalid"
        )

    def test_instance_never_carries_shell_or_unit_metacharacters(self) -> None:
        instance = job_runner.template_instance_id("a b;rm -rf /@x$(id)`id`")
        self.assertNotIn("@", instance)
        self.assertNotIn(";", instance)
        self.assertNotIn("$", instance)
        self.assertNotIn("/", instance)

    def test_template_unit_name_composition(self) -> None:
        self.assertEqual(
            job_runner.template_unit_name("demo-deadbeef"),
            "cortex-job@demo-deadbeef.service",
        )

    def test_malformed_template_config_is_fail_closed(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            job_runner.template_unit_name("demo", template="cortex-job.service")
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-template-unit-invalid"
        )

    def test_injected_instance_is_rejected(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            job_runner.template_unit_name("evil@other")
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-instance-name-invalid"
        )


def _unwrap_exit_recorder(argv: list[str]) -> list[str]:
    """剝掉 #604 的 Manager 側 exit 記帳 shell，取回封閉的 systemctl client argv。"""

    assert argv[:2] == ["bash", "-c"], argv
    head, sep, tail = argv[2].partition("; rc=$?; ")
    assert sep, argv[2]
    assert tail.startswith('printf %s "$rc" > '), tail
    return shlex.split(head)


class SystemctlArgvTests(unittest.TestCase):
    def test_argv_is_closed(self) -> None:
        argv = job_runner.build_systemctl_start_argv(
            systemctl="/usr/bin/systemctl", unit="cortex-job@demo.service"
        )
        self.assertEqual(
            argv,
            [
                "/usr/bin/systemctl",
                "start",
                "--wait",
                "--no-ask-password",
                "cortex-job@demo.service",
            ],
        )

    def test_argv_never_carries_identity_or_properties(self) -> None:
        argv = job_runner.build_systemctl_start_argv(
            systemctl="/usr/bin/systemctl", unit="cortex-job@demo.service"
        )
        joined = " ".join(argv)
        for forbidden in ("--uid", "--gid", "--property", "--setenv", "-p ", "--no-block"):
            self.assertNotIn(forbidden, joined, forbidden)


# ---------------------------------------------------------------------------
# B：spec 內容
# ---------------------------------------------------------------------------

class JobSpecContentTests(unittest.TestCase):
    def test_launch_writes_one_spec_and_starts_the_instance(self) -> None:
        popen = _RecordingPopen()
        ctx = _launch_template(SubprocessLauncher("codex"), popen=popen)
        # #604：最外層是 Manager 側的 exit 記帳 shell（sentinel 的寫者不再是 job）；
        # 它包住的才是那條封閉的 systemctl client argv。
        argv = _unwrap_exit_recorder(popen.call["argv"])
        self.assertEqual(argv[:4], ["/usr/bin/systemctl", "start", "--wait", "--no-ask-password"])
        # #643：`codex` 是 node 型 ⇒ jit 剖面的模板（`cortex-job-jit@`）。
        self.assertTrue(argv[4].startswith("cortex-job-jit@"), argv[4])
        self.assertTrue(argv[4].endswith(".service"))
        self.assertEqual(popen.call["cwd"], None)
        self.assertEqual(popen.call["stdin"], subprocess.DEVNULL)

    def test_spec_never_carries_identity(self) -> None:
        """User 不在 spec 裡——它在 root-owned 的 unit 檔。"""
        with tempfile.TemporaryDirectory() as d:
            _launch_template(SubprocessLauncher("codex"), popen=_RecordingPopen(), workdir=d)
            spec = _only_spec(str(Path(d) / "job-specs"))
        for key in job_runner.SPEC_FORBIDDEN_KEYS:
            self.assertNotIn(key, spec, key)
        blob = json.dumps(spec, ensure_ascii=False)
        for token in ("cortex-builder", "User=", "--uid"):
            self.assertNotIn(token, blob, token)

    def test_spec_env_has_no_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _launch_template(SubprocessLauncher("copilot"), popen=_RecordingPopen(), workdir=d)
            spec = _only_spec(str(Path(d) / "job-specs"))
        env = spec["env"]
        for name in _SECRET_ENV:
            self.assertNotIn(name, env, name)
        blob = json.dumps(spec, ensure_ascii=False)
        for value in _SECRET_ENV.values():
            self.assertNotIn(value, blob, value)

    def test_spec_carries_command_worktree_log_and_whitelisted_env(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ctx = _launch_template(
                SubprocessLauncher("codex"), popen=_RecordingPopen(), workdir=d
            )
            spec = _only_spec(str(Path(d) / "job-specs"))
        self.assertEqual(spec["spec_version"], job_runner.JOB_SPEC_VERSION)
        self.assertEqual(spec["command"][:2], ["bash", "-c"])
        self.assertEqual(spec["working_directory"], ctx["root"])
        self.assertEqual(spec["log_path"], ctx["log_path"])
        # #679：spec 的 PATH 來自 `PSC_BUILDER_PATH`，**不是** daemon 的 PATH。
        self.assertEqual(spec["env"]["PATH"], _BASE_ENV["PSC_BUILDER_PATH"])
        self.assertNotEqual(spec["env"]["PATH"], _BASE_ENV["PATH"])
        self.assertEqual(spec["env"]["PSC_JOB_ID"], "psc-0042-template")
        self.assertEqual(
            spec["unit"],
            job_runner.template_unit_name(
                spec["instance"],
                template=job_runner.template_unit_for_profile(
                    job_runner.DEFAULT_TEMPLATE_UNIT, job_runner.HARDENING_PROFILE_JIT
                ),
            ),
        )

    def test_spec_uses_bash_c_not_login_shell(self) -> None:
        """#588 第 2 點：降權模式一律 `bash -c`（login shell 會重新匯入 ~/.profile）。"""
        with tempfile.TemporaryDirectory() as d:
            _launch_template(SubprocessLauncher("codex"), popen=_RecordingPopen(), workdir=d)
            spec = _only_spec(str(Path(d) / "job-specs"))
        self.assertEqual(spec["command"][1], "-c")
        self.assertNotEqual(spec["command"][1], "-lc")

    def test_spec_file_is_readable_by_the_job_account(self) -> None:
        """mode 必須留下 group-read 位，否則繼承的唯讀 ACL 會被 mask 掉。"""
        with tempfile.TemporaryDirectory() as d:
            _launch_template(SubprocessLauncher("codex"), popen=_RecordingPopen(), workdir=d)
            spec_file = sorted(Path(d, "job-specs").glob("*.json"))[0]
            mode = stat.S_IMODE(spec_file.stat().st_mode)
        self.assertEqual(mode, 0o640)

    def test_spec_write_is_atomic_and_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _launch_template(SubprocessLauncher("codex"), popen=_RecordingPopen(), workdir=d)
            leftovers = [p.name for p in Path(d, "job-specs").iterdir()]
        self.assertEqual(len(leftovers), 1, leftovers)
        self.assertTrue(leftovers[0].endswith(".json"), leftovers)

    def test_build_job_spec_rejects_relative_paths_and_empty_command(self) -> None:
        """#687：`["bash", ""]` 那一格改成 `["", "-c", "true"]`（空的是 argv[0]）。

        原判準是 `not all(argv)`＝「每個元素都必須非空」。票 E（#686）把 planning
        接上同一條 spec 通道之後，`claude` 的 production argv
        （`… --tools "" …`，CLI 成文 API「`Use "" to disable all tools`」）在**每一次**
        define 都撞上它。判準因此收斂成「`argv` 非空且 `argv[0]` 非空」，其餘元素
        依 POSIX 語意允許空字串；完整論證見
        `job_runner.malformed_job_command()` 與 `tests/test_planning_job_argv_687.py`。
        本格改測**現在仍應被擋**的那一種，不是把斷言刪掉。
        """

        base = {
            "job_id": "j",
            "instance": "j-deadbeef",
            "unit": "cortex-job@j-deadbeef.service",
            "command": ["bash", "-c", "true"],
            "working_directory": "/wt",
            "log_path": "/logs/j.jsonl",
            "env": {"PATH": "/usr/bin"},
        }
        for override in (
            {"command": []},
            {"command": ["", "-c", "true"]},
            {"working_directory": "relative/wt"},
            {"log_path": "relative.jsonl"},
        ):
            with self.assertRaises(JobRunnerError) as ctx:
                job_runner.build_job_spec(**{**base, **override})
            self.assertEqual(
                ctx.exception.diagnostic.reason, "job-runner-job-spec-invalid", override
            )

    def test_build_job_spec_rejects_credential_env(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            job_runner.build_job_spec(
                job_id="j",
                instance="j-deadbeef",
                unit="cortex-job@j-deadbeef.service",
                command=["bash", "-c", "true"],
                working_directory="/wt",
                log_path="/logs/j.jsonl",
                env={"PATH": "/usr/bin", "GH_TOKEN": "leak"},
            )
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-credential-env-leak"
        )


# ---------------------------------------------------------------------------
# B：fail-fast 三案
# ---------------------------------------------------------------------------

class TemplateFailFastTests(unittest.TestCase):
    def _expect_fail(self, *, reason: str, patches, spool: str | None = None) -> None:
        popen = _RecordingPopen()
        with self.assertRaises(JobRunnerError) as ctx:
            _launch_template(
                SubprocessLauncher("codex"), popen=popen, patches=patches, spool=spool
            )
        self.assertEqual(ctx.exception.diagnostic.reason, reason)
        self.assertEqual(popen.calls, [], "fail-closed：一個 job 都不得被 spawn")

    def test_template_unit_not_installed(self) -> None:
        self._expect_fail(
            reason="job-runner-job-template-missing",
            patches=_preflight_patches(
                unit_file=mock.patch.object(
                    job_runner, "_unit_file_installed", return_value=False
                )
            ),
        )

    def test_shim_not_installed(self) -> None:
        self._expect_fail(
            reason="job-runner-job-shim-missing",
            patches=_preflight_patches(
                shim=mock.patch.object(job_runner, "_is_executable", return_value=False)
            ),
        )

    def test_instance_already_running(self) -> None:
        self._expect_fail(
            reason="job-runner-template-instance-busy",
            patches=_preflight_patches(unit_active=True),
        )

    def test_spec_spool_missing(self) -> None:
        popen = _RecordingPopen()
        with tempfile.TemporaryDirectory() as d:
            missing = str(Path(d) / "no-such-spool")
            with self.assertRaises(JobRunnerError) as ctx:
                _launch_template(
                    SubprocessLauncher("codex"),
                    popen=popen,
                    patches=_preflight_patches(),
                    env_overrides={job_runner.JOB_SPEC_SPOOL_ENV: missing},
                    workdir=d,
                )
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-job-spec-spool-missing"
        )
        self.assertEqual(popen.calls, [])

    def test_spec_write_failure_is_fail_closed(self) -> None:
        popen = _RecordingPopen()
        boom = mock.patch.object(
            job_runner.os, "replace", side_effect=OSError("read-only file system")
        )
        with self.assertRaises(JobRunnerError) as ctx:
            _launch_template(
                SubprocessLauncher("codex"),
                popen=popen,
                patches=_preflight_patches() + [boom],
            )
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-job-spec-write-failed"
        )
        self.assertEqual(popen.calls, [], "spec 寫不進去就不得 spawn")

    def test_systemctl_missing_is_fail_closed(self) -> None:
        self._expect_fail(
            reason="job-runner-systemctl-missing",
            patches=_preflight_patches(
                which=mock.patch.object(job_runner.shutil, "which", return_value=None)
            ),
        )

    def test_never_falls_back_to_another_mode(self) -> None:
        """fail-closed 的定義：不是「換一種方式跑」，是「不跑」。"""
        src = Path(job_runner.__file__).read_text(encoding="utf-8")
        for fallback in ("RUNNER_DIRECT\n        return", "except JobRunnerError"):
            self.assertNotIn(fallback, src, fallback)

    def test_start_confirmation_is_fail_closed_without_sentinel(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            job_runner.confirm_template_instance_started(
                process=_FakeProc(exit_status=1),
                sentinel="/nonexistent/sentinel",
                unit="cortex-job@demo.service",
                account="cortex-builder",
                timeout_ms=0,
                monotonic=lambda: 0.0,
                sleep=lambda _s: None,
            )
        diagnostic = ctx.exception.diagnostic
        self.assertEqual(
            diagnostic.reason, "job-runner-template-instance-start-failed"
        )
        self.assertEqual(diagnostic.context["unit"], "cortex-job@demo.service")
        self.assertIn("journalctl", diagnostic.detail)

    def test_polkit_denial_text_reaches_the_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "job.jsonl"
            log_path.write_text(
                "Failed to start cortex-job@x.service: Access denied\n", encoding="utf-8"
            )
            with self.assertRaises(JobRunnerError) as ctx:
                job_runner.confirm_template_instance_started(
                    process=_FakeProc(exit_status=1),
                    sentinel=str(Path(d) / "missing.exit"),
                    unit="cortex-job@x.service",
                    account="cortex-builder",
                    log_path=str(log_path),
                    timeout_ms=0,
                    monotonic=lambda: 0.0,
                    sleep=lambda _s: None,
                )
        self.assertIn("Access denied", ctx.exception.diagnostic.detail)

    def test_fast_job_with_sentinel_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sentinel = Path(d) / "job.exit"
            sentinel.write_text("0", encoding="utf-8")
            job_runner.confirm_template_instance_started(
                process=_FakeProc(exit_status=0),
                sentinel=str(sentinel),
                unit="cortex-job@x.service",
                account="cortex-builder",
                timeout_ms=200,
                monotonic=lambda: 0.0,
                sleep=lambda _s: None,
            )


# ---------------------------------------------------------------------------
# harvest 相容：log／sentinel 路徑不變
# ---------------------------------------------------------------------------

class HarvestCompatibilityTests(unittest.TestCase):
    def test_log_and_sentinel_paths_are_unchanged(self) -> None:
        popen = _RecordingPopen()
        with tempfile.TemporaryDirectory() as d:
            ctx = _launch_template(SubprocessLauncher("codex"), popen=popen, workdir=d)
            spec = _only_spec(ctx["spool"])
        # harvest（`dispatcher.poll_headless_done` → `_read_exit_sentinel`）讀的是
        # `<log_dir>/<slice>.jsonl` 與同名 `.exit`——兩者都必須與 direct 模式一致。
        sentinel = str(Path(ctx["log_dir"]) / "psc-0042-template.exit")
        self.assertEqual(spec["log_path"], str(Path(ctx["log_dir"]) / "psc-0042-template.jsonl"))
        # #604：**路徑不變，作者改變**。sentinel 現在由 Manager 側的 exit 記帳 shell
        # 寫（`popen` 的 argv），job spec 的 command 內不得再出現它——那個目錄是
        # `0700 cortex-manager`，job 寫進去只會 EROFS，而且那本來就是自報。
        self.assertIn(sentinel, popen.call["argv"][2])
        self.assertNotIn(sentinel, spec["command"][2])

    def test_log_file_is_truncated_then_appended(self) -> None:
        """systemctl client 用 append fd，避免蓋掉 shim 已經寫進去的內容。"""
        with tempfile.TemporaryDirectory() as d:
            log_dir = Path(d) / "logs"
            log_dir.mkdir(parents=True)
            stale = log_dir / "psc-0042-template.jsonl"
            stale.write_text("STALE FROM LAST ROUND\n", encoding="utf-8")
            _launch_template(SubprocessLauncher("codex"), popen=_RecordingPopen(), workdir=d)
            self.assertEqual(stale.read_text(encoding="utf-8"), "")


# ---------------------------------------------------------------------------
# 零回歸：direct 與 systemd-run
# ---------------------------------------------------------------------------

class NoRegressionTests(unittest.TestCase):
    def test_mode_table_extends_without_reordering(self) -> None:
        self.assertEqual(job_runner.RUNNER_MODES[0], job_runner.RUNNER_DIRECT)
        self.assertEqual(job_runner.RUNNER_MODES[1], job_runner.RUNNER_SYSTEMD_RUN)
        self.assertEqual(job_runner.RUNNER_MODES[2], job_runner.RUNNER_SYSTEMD_TEMPLATE)
        self.assertEqual(job_runner.resolve_runner_mode({}), job_runner.RUNNER_DIRECT)

    def test_default_is_still_direct_with_login_shell(self) -> None:
        popen = _RecordingPopen()
        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = popen
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, dict(_BASE_ENV), clear=True
            ):
                SubprocessLauncher("codex").launch(
                    slice_id="psc-0001-demo",
                    prompt="PROMPT",
                    worktree=d,
                    log_dir=str(Path(d) / "logs"),
                )
        finally:
            launcher_module.subprocess.Popen = original
        self.assertEqual(popen.call["argv"][:2], ["bash", "-lc"])
        self.assertNotIn("stdin", popen.call)

    def test_template_mode_string_is_the_only_new_trigger(self) -> None:
        for value in ("systemd_template", "template", "systemd-templates"):
            with self.assertRaises(JobRunnerError):
                job_runner.resolve_runner_mode({job_runner.JOB_RUNNER_ENV: value})
        self.assertEqual(
            job_runner.resolve_runner_mode(
                {job_runner.JOB_RUNNER_ENV: "SYSTEMD-TEMPLATE"}
            ),
            job_runner.RUNNER_SYSTEMD_TEMPLATE,
        )

    def test_systemd_run_mode_still_produces_the_transient_argv(self) -> None:
        popen = _RecordingPopen()
        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = popen
        env = {
            **_BASE_ENV,
            **_SECRET_ENV,
            job_runner.JOB_RUNNER_ENV: job_runner.RUNNER_SYSTEMD_RUN,
        }
        patches = [
            mock.patch.object(
                job_runner.shutil, "which", return_value="/usr/bin/systemd-run"
            ),
            mock.patch.object(job_runner, "_systemd_booted", return_value=True),
            mock.patch.object(job_runner, "_account_exists", return_value=True),
            mock.patch.object(job_runner, "_group_exists", return_value=True),
        ]
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, env, clear=True
            ):
                with _nested(patches):
                    SubprocessLauncher("codex").launch(
                        slice_id="psc-0001-demo",
                        prompt="PROMPT",
                        worktree=d,
                        log_dir=str(Path(d) / "logs"),
                    )
        finally:
            launcher_module.subprocess.Popen = original
        # #604：A 案與 B 案一樣，最外層是 Manager 側的 exit 記帳 shell。
        argv = _unwrap_exit_recorder(popen.call["argv"])
        self.assertEqual(argv[0], "/usr/bin/systemd-run")
        for flag in ("--pipe", "--wait", "--collect", "--uid=cortex-builder"):
            self.assertIn(flag, argv)
        self.assertEqual(popen.call["cwd"], os.path.realpath(popen.call["cwd"]))

    def test_reviewer_and_planner_take_the_review_template(self) -> None:
        """#615（M2）：兩個 persona 都走模板路徑，且走的是 **reviewer 那一份**。

        M1 期間這條測的是「reviewer／planner 永不走模板」——那是當時的誠實邊界。
        M2 之後守的性質變成「它們走的是自己那一份模板、不是 builder 的」：走錯一份
        會讓 reviewer 以 `cortex-builder` 起跑，等於把 verdict 的寫入面交還給 builder。
        """
        for kwargs in (
            {"review_only": True, "review_terminal_kind": "workflow-review-result"},
            {"read_only": True},
        ):
            popen = _RecordingPopen()
            launcher = SubprocessLauncher("codex", **kwargs)
            original = launcher_module.subprocess.Popen
            launcher_module.subprocess.Popen = popen
            try:
                with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                    os.environ,
                    _template_env(str(Path(d) / "job-specs")),
                    clear=True,
                ), _nested(_preflight_patches()):
                    Path(d, "job-specs").mkdir(parents=True, exist_ok=True)
                    launcher.launch(
                        slice_id="psc-0002-review",
                        prompt="PROMPT",
                        worktree=d,
                        log_dir=str(Path(d) / "logs"),
                    )
                    specs = sorted(Path(d, "job-specs").iterdir())
                    self.assertEqual(len(specs), 1, kwargs)
                    spec = json.loads(specs[0].read_text(encoding="utf-8"))
            finally:
                launcher_module.subprocess.Popen = original
            # codex＝node 型 ⇒ jit 剖面；角色＝review ⇒ reviewer 的字幹。
            self.assertTrue(
                spec["unit"].startswith("cortex-reviewer-job-jit@"), (kwargs, spec["unit"])
            )
            self.assertNotIn("cortex-job@", spec["unit"])
            # 身分欄位仍然不得出現在 spec 裡（唯一來源是 root-owned unit 的 User=）。
            self.assertEqual(job_runner.forbidden_spec_keys(spec), [], kwargs)
            argv = _unwrap_exit_recorder(popen.call["argv"])
            self.assertEqual(argv[0], "/usr/bin/systemctl", kwargs)
            self.assertIn(spec["unit"], argv, kwargs)


# ---------------------------------------------------------------------------
# shim：stub 產生 ＋ 邏輯模組
# ---------------------------------------------------------------------------

class ShimStubTests(unittest.TestCase):
    def test_stub_execs_the_shim_module_with_the_deploy_interpreter(self) -> None:
        shim = permgen.build_job_shim(permgen.DEFAULT_SCHEME, permgen.DEFAULT_LAYOUT)
        self.assertEqual(shim.install_path, "/opt/cortex/bin/cortex-job-shim")
        self.assertEqual(shim.interpreter, "/opt/cortex/venv/bin/python3")
        self.assertEqual(shim.module, "paulsha_cortex.coordinator.job_shim")
        self.assertTrue(shim.content.startswith("#!/bin/sh\n"))
        self.assertIn(
            'exec "/opt/cortex/venv/bin/python3" -m paulsha_cortex.coordinator.job_shim "$@"',
            shim.content,
        )
        # interpreter 絕不從 PATH 解析：那等於讓 job 決定用哪個 python 跑 root-owned shim。
        self.assertNotIn("/usr/bin/env", shim.content)

    def test_stub_is_root_owned_and_world_readable_only(self) -> None:
        shim = permgen.build_job_shim(permgen.DEFAULT_SCHEME)
        self.assertEqual(shim.owner, "root")
        self.assertEqual(shim.mode, 0o755)
        self.assertEqual(
            shim.commands(),
            [
                "chown root:root /opt/cortex/bin/cortex-job-shim",
                "chmod 0755 /opt/cortex/bin/cortex-job-shim",
            ],
        )

    def test_stub_matches_the_template_unit_exec_start(self) -> None:
        unit = permgen.build_job_unit(permgen.DEFAULT_SCHEME, permgen.DEFAULT_LAYOUT)
        shim = permgen.build_job_shim(permgen.DEFAULT_SCHEME, permgen.DEFAULT_LAYOUT)
        self.assertEqual(unit.exec_start, f"{shim.install_path} %i")

    def test_shim_lives_in_a_root_owned_scaffold_directory(self) -> None:
        dirs = {
            path: (owner, mode)
            for path, owner, _group, mode in permgen.DEFAULT_LAYOUT.scaffold_directories(
                permgen.DEFAULT_SCHEME
            )
        }
        self.assertEqual(dirs[permgen.DEFAULT_LAYOUT.bin_root], ("root", 0o755))

    def test_generator_is_deterministic_and_string_only(self) -> None:
        a = permgen.build_job_shim(permgen.DEFAULT_SCHEME)
        b = permgen.build_job_shim(permgen.DEFAULT_SCHEME)
        self.assertEqual(a.content, b.content)
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertIsInstance(a.content, str)


class ShimLogicTests(unittest.TestCase):
    def _spec(self, root: Path, **overrides) -> dict:
        spec = {
            "spec_version": job_runner.JOB_SPEC_VERSION,
            "instance": "demo-deadbeef",
            "job_id": "demo",
            "unit": "cortex-job@demo-deadbeef.service",
            "command": ["/bin/true"],
            "working_directory": str(root),
            "log_path": str(root / "job.jsonl"),
            "env": {"PATH": "/usr/bin"},
        }
        spec.update(overrides)
        return spec

    def _write(self, spool: Path, spec: dict, name: str = "demo-deadbeef") -> None:
        spool.mkdir(parents=True, exist_ok=True)
        (spool / f"{name}.json").write_text(
            json.dumps(spec, ensure_ascii=False), encoding="utf-8"
        )

    def test_load_spec_round_trips_a_manager_written_spec(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spool = root / "job-specs"
            spec = self._spec(root)
            self._write(spool, spec)
            loaded = job_shim.load_spec("demo-deadbeef", str(spool))
        self.assertEqual(loaded["command"], ["/bin/true"])

    def test_spec_path_is_derived_never_supplied(self) -> None:
        self.assertEqual(
            job_shim.resolve_spec_path("demo-deadbeef", "/var/lib/cortex/coordinator/job-specs"),
            "/var/lib/cortex/coordinator/job-specs/demo-deadbeef.json",
        )
        for evil in ("../../etc/shadow", "a/b", "", "x" * 200):
            with self.assertRaises(job_shim.ShimError, msg=evil):
                job_shim.resolve_spec_path(evil, "/spool")

    def test_symlinked_spec_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spool = root / "job-specs"
            spool.mkdir(parents=True)
            real = root / "elsewhere.json"
            real.write_text(json.dumps(self._spec(root)), encoding="utf-8")
            (spool / "demo-deadbeef.json").symlink_to(real)
            with self.assertRaises(job_shim.ShimError):
                job_shim.load_spec("demo-deadbeef", str(spool))

    def test_identity_fields_in_spec_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spool = root / "job-specs"
            for key in ("user", "uid", "group", "gid", "exec_start"):
                self._write(spool, self._spec(root, **{key: "root"}))
                with self.assertRaises(job_shim.ShimError, msg=key) as ctx:
                    job_shim.load_spec("demo-deadbeef", str(spool))
                self.assertIn("身分", str(ctx.exception), key)

    def test_credential_env_in_spec_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spool = root / "job-specs"
            self._write(
                spool, self._spec(root, env={"PATH": "/usr/bin", "GH_TOKEN": "leak"})
            )
            with self.assertRaises(job_shim.ShimError):
                job_shim.load_spec("demo-deadbeef", str(spool))

    def test_schema_violations_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spool = root / "job-specs"
            for override in (
                {"spec_version": 99},
                {"command": []},
                {"command": "bash -c true"},
                {"working_directory": "relative"},
                {"log_path": "relative.jsonl"},
                {"env": ["PATH=/usr/bin"]},
                {"instance": "someone-elses-job"},
            ):
                self._write(spool, self._spec(root, **override))
                with self.assertRaises(job_shim.ShimError, msg=str(override)):
                    job_shim.load_spec("demo-deadbeef", str(spool))

    def test_missing_required_key_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spool = root / "job-specs"
            for key in job_runner.SPEC_REQUIRED_KEYS:
                spec = self._spec(root)
                spec.pop(key)
                self._write(spool, spec)
                with self.assertRaises(job_shim.ShimError, msg=key):
                    job_shim.load_spec("demo-deadbeef", str(spool))

    def test_absent_spool_env_is_fail_closed(self) -> None:
        self.assertEqual(
            job_shim.main(["demo-deadbeef"], environ={}), job_shim.EXIT_SPEC_ERROR
        )

    def test_wrong_argument_count_is_fail_closed(self) -> None:
        for args in ([], ["a", "b"]):
            self.assertEqual(
                job_shim.main(args, environ={job_runner.JOB_SPEC_SPOOL_ENV: "/spool"}),
                job_shim.EXIT_SPEC_ERROR,
            )

    def test_main_takes_over_the_log_then_execs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spool = root / "job-specs"
            log = root / "job.jsonl"
            log.write_text("MANAGER PREAMBLE\n", encoding="utf-8")
            self._write(spool, self._spec(root))
            recorded: dict = {}

            def fake_exec(file, argv, env):
                recorded["file"] = file
                recorded["argv"] = list(argv)
                recorded["env"] = dict(env)
                recorded["cwd"] = os.getcwd()
                # 走到這裡代表 log 已被接管：往 stdout 寫一行來證明它落在 log 檔。
                os.write(1, b"FROM JOB\n")
                raise OSError("exec stub")

            saved_out, saved_err = os.dup(1), os.dup(2)
            cwd = os.getcwd()
            try:
                with mock.patch.object(job_shim.os, "execvpe", fake_exec):
                    rc = job_shim.main(
                        ["demo-deadbeef"],
                        environ={job_runner.JOB_SPEC_SPOOL_ENV: str(spool)},
                    )
            finally:
                os.dup2(saved_out, 1)
                os.dup2(saved_err, 2)
                os.close(saved_out)
                os.close(saved_err)
                os.chdir(cwd)
            self.assertEqual(rc, job_shim.EXIT_SPEC_ERROR)
            self.assertEqual(recorded["file"], "/bin/true")
            self.assertEqual(recorded["argv"], ["/bin/true"])
            self.assertEqual(recorded["env"], {"PATH": "/usr/bin"})
            self.assertEqual(Path(recorded["cwd"]).resolve(), root.resolve())
            contents = log.read_text(encoding="utf-8")
            self.assertIn("MANAGER PREAMBLE", contents, "append，不得截掉 Manager 的前導輸出")
            self.assertIn("FROM JOB", contents)

    def test_symlinked_log_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spool = root / "job-specs"
            victim = root / "victim.txt"
            victim.write_text("do not append here\n", encoding="utf-8")
            (root / "job.jsonl").symlink_to(victim)
            self._write(spool, self._spec(root))
            rc = job_shim.main(
                ["demo-deadbeef"], environ={job_runner.JOB_SPEC_SPOOL_ENV: str(spool)}
            )
        self.assertEqual(rc, job_shim.EXIT_SPEC_ERROR)

    def test_shim_and_runner_share_one_schema(self) -> None:
        """讀端與寫端的 schema 常數必須是同一份，不得各抄一遍。"""
        self.assertIs(job_shim.JOB_SPEC_VERSION, job_runner.JOB_SPEC_VERSION)
        self.assertIs(job_shim.SPEC_REQUIRED_KEYS, job_runner.SPEC_REQUIRED_KEYS)
        self.assertIs(job_shim.SPEC_FORBIDDEN_KEYS, job_runner.SPEC_FORBIDDEN_KEYS)

    def test_manager_written_spec_is_accepted_by_the_shim(self) -> None:
        """端到端契約：`job_runner` 寫出的 spec，`job_shim` 必須讀得動。"""
        with tempfile.TemporaryDirectory() as d:
            ctx = _launch_template(
                SubprocessLauncher("codex"), popen=_RecordingPopen(), workdir=d
            )
            instance = sorted(Path(ctx["spool"]).glob("*.json"))[0].stem
            loaded = job_shim.load_spec(instance, ctx["spool"])
        self.assertEqual(loaded["instance"], instance)
        self.assertEqual(loaded["command"][:2], ["bash", "-c"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
