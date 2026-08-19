"""#716（選項 F）：sandbox mode 由**卡片契約**機械導出，不由 persona 一刀切。

## 這張票在修什麼

0819 的 build job `wf-6c37c77ca1-worktree-isolation-7` 逐字：

    "error":"permission profiles requiring direct runtime enforcement are
             incompatible with --use-legacy-landlock"

病因不是 git、不是 `$CODEX_HOME`、不是 `$TMPDIR`，是 **argv 上的
`--sandbox workspace-write`**。codex 由它導出 `:workspace` 族 permission profile，
該族要求 direct runtime enforcement，而 legacy landlock 路徑不實作它
（`linux-sandbox/src/linux_run_main.rs:318` 的 fail-closed 檢查）。

判準是一條**性質**而不是某個具名 profile：profile 只要攜帶**任何** filesystem 寫入
授權就要求 direct runtime enforcement（`-P` 實測：`extends=":read-only"` rc=0／
`":none"` rc=0／`":workspace"` panic／`":read-only"` **加一條**
`filesystem = { "<path>" = "write" }` **panic**）。這是 **session 層級**判定，發生在
任何命令執行**之前**——所以模型的唯讀 `git rev-parse HEAD` 與一條寫入命令 panic 得
一模一樣。

而 `build_codex_argv` **完全不看 `commit_policy`**：`read_only` 是 launcher 維度
（`as_read_only()`），builder 一律走 `else` 分支拿 `workspace-write`。於是一張
`commit_policy=forbidden` 且 `declared_outputs` 為空的唯讀 build 卡拿到寫入授權
——**那是獨立成立的最小權限缺陷**，與 landlock 無關；legacy landlock 只是讓它從
「權限給多了」變成當場 panic。

## 本檔釘住什麼

1. `DerivationTableTests`——規則是**登記表上的一格**，五種寫入契約一格不少，
   `grants_filesystem_write` 與 mode 一致（那一欄記的就是 codex 的判準），
   planner／reviewer 恆為 `read-only`。
2. `CardContractPredicateTests`——降級判準**兩個條件都要明確成立**，
   缺欄／型別不對／非空一律維持現狀。
3. `CodexArgvTests`——argv 上的 mode 來自登記表而非第二份字面量；
   write-forbidden 的 build 卡與今天**只差 mode 一個 token**；
   planner／reviewer 的 argv **逐字不變**。
4. `LauncherContractTests`——`as_write_forbidden()` 的四種輸入，
   以及**它不改變 job 角色**（write-forbidden 的 build 卡仍以 builder 起跑）。
5. `WorkflowSpecializationTests`——`manager._specialize_workflow_launcher()`
   對 `worktree-isolation`（本票的原症狀卡）真的套用降級，對寫入卡不套用。
6. `ProbeDerivationTests`——探針的 mode 清單由**同一張表**導出、含
   `workspace-write`、每個 mode 都有負向對照、每一條命令都帶 `-c`
   （`codex sandbox` 忽略 `config.toml` 的 `sandbox_mode`）。

OS 層語意（真的裝一次 landlock、真的 panic 一次）在單 UID／無 systemd 的 CI 上重現
不了，因此以具名 skip 標出，不留假綠——見 `OsLevelSemanticsTests`。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from paulsha_cortex.coordinator import manager as manager_module
from paulsha_cortex.coordinator.launcher import SubprocessLauncher, build_codex_argv
from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.registry import JobWriteContract


def _argv(**kwargs) -> list[str]:
    return build_codex_argv(prompt="P", slice_id="wf-716", log_dir="/lg", **kwargs)


def _sandbox_value(argv: list[str]) -> str:
    return argv[argv.index("--sandbox") + 1]


# ---------------------------------------------------------------------------
# 1. 規則是登記表上的一格
# ---------------------------------------------------------------------------

class DerivationTableTests(unittest.TestCase):

    def test_every_write_contract_has_exactly_one_row(self) -> None:
        """漏一格 ⇒ registry **載不起來**（import 期斷言）。

        這條與 #708 的 `JOB_LOG_SPOOLS`／#710 的 `JOB_WORKSPACE_REACH`／#712 的
        `JOB_GIT_WORKSPACE_TRUST` 同型：那三票的病因都是「同一條性質在 N 個地方各自
        被決定」。本測試只是把 import 期已經強制的事寫成可讀的斷言。
        """

        declared = [row.contract for row in registry.SANDBOX_MODE_DERIVATION]
        self.assertEqual(len(declared), len(set(declared)), declared)
        self.assertEqual(set(declared), set(JobWriteContract))

    def test_write_grant_flag_matches_the_mode(self) -> None:
        """`grants_filesystem_write` 記的是 codex 的判準，不是偏好。"""

        for row in registry.SANDBOX_MODE_DERIVATION:
            if row.sandbox_mode is None:
                continue
            self.assertEqual(
                row.grants_filesystem_write,
                row.sandbox_mode == registry.SANDBOX_MODE_WORKSPACE_WRITE,
                row.contract,
            )

    def test_planner_and_reviewer_stay_read_only(self) -> None:
        """**現行行為不得改變**：那兩格 0819 實機在真實 agent loop 下 rc=0。"""

        for contract in (
            JobWriteContract.PLANNER_READ_ONLY, JobWriteContract.REVIEWER_REVIEW_ONLY
        ):
            self.assertEqual(
                registry.sandbox_mode_for(contract), registry.SANDBOX_MODE_READ_ONLY
            )

    def test_write_forbidden_is_read_only_and_carries_no_write_grant(self) -> None:
        """本票新增的那一格：唯讀 build 卡不再攜帶任何 filesystem 寫入授權。"""

        row = next(
            r for r in registry.SANDBOX_MODE_DERIVATION
            if r.contract is JobWriteContract.BUILDER_WRITE_FORBIDDEN
        )
        self.assertEqual(row.sandbox_mode, registry.SANDBOX_MODE_READ_ONLY)
        self.assertFalse(row.grants_filesystem_write)

    def test_write_cards_still_get_workspace_write(self) -> None:
        """**誠實邊界**：F 只解唯讀卡，寫入卡那一半未解（#716 仍 open）。"""

        self.assertEqual(
            registry.sandbox_mode_for(JobWriteContract.BUILDER_WORKSPACE_WRITE),
            registry.SANDBOX_MODE_WORKSPACE_WRITE,
        )

    def test_unknown_contract_fails_closed(self) -> None:
        with self.assertRaises(KeyError):
            registry.sandbox_mode_for("builder-write-forbidden")  # type: ignore[arg-type]

    def test_every_row_carries_a_note(self) -> None:
        for row in registry.SANDBOX_MODE_DERIVATION:
            self.assertTrue(row.note, row.contract)

    def test_derivation_precedence_never_relaxes_the_read_only_lanes(self) -> None:
        """`write_forbidden` 不得把 planner／reviewer 拉到別的形態。"""

        self.assertIs(
            registry.derive_job_write_contract(read_only=True, write_forbidden=True),
            JobWriteContract.PLANNER_READ_ONLY,
        )
        self.assertIs(
            registry.derive_job_write_contract(review_only=True, write_forbidden=True),
            JobWriteContract.REVIEWER_REVIEW_ONLY,
        )

    def test_contradictory_contracts_fail_closed(self) -> None:
        for kwargs in (
            {"read_only": True, "review_only": True},
            {"allow_unsafe": True, "read_only": True},
            {"allow_unsafe": True, "write_forbidden": True},
            {"commit_required": True, "read_only": True},
            {"commit_required": True, "write_forbidden": True},
        ):
            with self.assertRaises(ValueError, msg=kwargs):
                registry.derive_job_write_contract(**kwargs)


# ---------------------------------------------------------------------------
# 2. 降級判準：兩個條件都要明確成立
# ---------------------------------------------------------------------------

class CardContractPredicateTests(unittest.TestCase):

    def test_forbidden_plus_no_declared_outputs_downgrades(self) -> None:
        self.assertTrue(
            registry.card_contract_forbids_workspace_write(
                commit_policy="forbidden", declared_outputs=[]
            )
        )
        self.assertTrue(
            registry.card_contract_forbids_workspace_write(
                commit_policy="forbidden", declared_outputs=()
            )
        )

    def test_declared_outputs_present_keeps_workspace_write(self) -> None:
        self.assertFalse(
            registry.card_contract_forbids_workspace_write(
                commit_policy="forbidden", declared_outputs=["docs/x.md"]
            )
        )

    def test_missing_or_other_commit_policy_keeps_workspace_write(self) -> None:
        """**缺欄不猜**——保守方向逐字是「解不出來 ⇒ 維持 workspace-write」。"""

        for policy in (None, "", "optional", "required", "FORBIDDEN", 0, True):
            self.assertFalse(
                registry.card_contract_forbids_workspace_write(
                    commit_policy=policy, declared_outputs=[]
                ),
                policy,
            )

    def test_non_sequence_declared_outputs_keeps_workspace_write(self) -> None:
        """契約形狀本身不對 ⇒ 那是「解不出來」，不是「宣告了空產出」。"""

        for outputs in (None, "", "docs/x.md", 0, {}, object()):
            self.assertFalse(
                registry.card_contract_forbids_workspace_write(
                    commit_policy="forbidden", declared_outputs=outputs
                ),
                outputs,
            )


# ---------------------------------------------------------------------------
# 3. argv：mode 的唯一真相在登記表
# ---------------------------------------------------------------------------

class CodexArgvTests(unittest.TestCase):

    def test_write_forbidden_build_card_gets_read_only(self) -> None:
        self.assertEqual(
            _sandbox_value(_argv(write_forbidden=True)),
            registry.SANDBOX_MODE_READ_ONLY,
        )

    def test_the_only_difference_from_today_is_the_mode_token(self) -> None:
        """**降級只動 mode 一個 token。**

        寫成斷言而不是註解，是因為「順手也把 `--skip-git-repo-check` 加上去」這種
        擴散最容易在 review 時被當成無害——而那張卡跑在 per-job clone 裡，加上去等於
        把一個真的該擋的檢查關掉。
        """

        before = _argv(worktree=None)
        after = _argv(worktree=None, write_forbidden=True)
        self.assertEqual(len(before), len(after))
        diff = [
            (a, b) for a, b in zip(before, after) if a != b
        ]
        self.assertEqual(
            diff,
            [(registry.SANDBOX_MODE_WORKSPACE_WRITE, registry.SANDBOX_MODE_READ_ONLY)],
        )

    def test_planner_and_reviewer_argv_are_byte_identical(self) -> None:
        """本票對 planner／reviewer **一個位元都不動**。"""

        for kwargs in ({"read_only": True}, {"review_only": True}):
            argv = _argv(**kwargs)
            self.assertEqual(
                _sandbox_value(argv), registry.SANDBOX_MODE_READ_ONLY, kwargs
            )
            self.assertIn("--skip-git-repo-check", argv, kwargs)
            # write_forbidden 一併宣告時仍逐字相同（優先序刻意如此）。
            self.assertEqual(argv, _argv(write_forbidden=True, **kwargs), kwargs)

    def test_commit_required_and_default_builders_keep_workspace_write(self) -> None:
        """#716 未解的那一半：會寫檔的卡仍然發 `workspace-write`。"""

        for kwargs in ({}, {"commit_required": True}):
            self.assertEqual(
                _sandbox_value(_argv(**kwargs)),
                registry.SANDBOX_MODE_WORKSPACE_WRITE,
                kwargs,
            )

    def test_bypass_mode_emits_no_sandbox_flag(self) -> None:
        argv = _argv(allow_unsafe=True)
        self.assertNotIn("--sandbox", argv)

    def test_mode_is_not_a_second_literal(self) -> None:
        """argv 發出來的 mode 集合必須**恰好**等於登記表導出的清單。

        這條是探針那一半的閉環：探針的 mode 清單由
        `registry.emitted_sandbox_modes()` 導出，而這裡證明「`build_codex_argv` 真的
        只會發那些」。少了它，有人在 argv 裡寫死第三個 mode，探針不會知道。
        """

        emitted = set()
        for kwargs in (
            {},
            {"commit_required": True},
            {"read_only": True},
            {"review_only": True},
            {"write_forbidden": True},
        ):
            argv = _argv(**kwargs)
            emitted.add(_sandbox_value(argv))
        self.assertEqual(emitted, set(registry.emitted_sandbox_modes()))

    def test_inner_sandbox_flag_still_rides_along(self) -> None:
        """降級不得順手把 #714 的內層沙箱形態弄掉。"""

        spec = permgen.executor_inner_sandbox("codex")
        assert spec is not None
        self.assertIn(" ".join(spec.argv), " ".join(_argv(write_forbidden=True)))


# ---------------------------------------------------------------------------
# 4. launcher 的建構契約
# ---------------------------------------------------------------------------

class LauncherContractTests(unittest.TestCase):

    def test_as_write_forbidden_downgrades_a_builder(self) -> None:
        launcher = SubprocessLauncher("codex").as_write_forbidden()
        self.assertTrue(launcher._write_forbidden)

    def test_read_only_lanes_are_returned_unchanged(self) -> None:
        """已經至少一樣嚴 ⇒ 原樣回傳，不是靜默降級。"""

        planner = SubprocessLauncher("codex").as_read_only()
        self.assertIs(planner.as_write_forbidden(), planner)
        reviewer = SubprocessLauncher("codex").as_review_only(
            terminal_kind="workflow-review-result"
        )
        self.assertIs(reviewer.as_write_forbidden(), reviewer)

    def test_commit_required_is_a_contract_contradiction(self) -> None:
        commit = SubprocessLauncher("codex").as_commit_required()
        with self.assertRaises(ValueError):
            commit.as_write_forbidden()
        with self.assertRaises(ValueError):
            SubprocessLauncher("codex", write_forbidden=True).as_commit_required()
        with self.assertRaises(ValueError):
            SubprocessLauncher("codex", write_forbidden=True, commit_required=True)

    def test_unsafe_bypass_is_out_of_scope(self) -> None:
        """明確 opt-in 的 bypass 連沙箱都沒有，本票不觸碰它。"""

        unsafe = SubprocessLauncher("codex", allow_unsafe=True)
        self.assertIs(unsafe.as_write_forbidden(), unsafe)

    def test_it_does_not_change_the_job_role(self) -> None:
        """**write-forbidden 的 build 卡仍然是 builder。**

        `_is_review_persona()` 的三個判準（#615）一個都沒動。改了角色會把這張卡搬到
        `cortex-reviewer-planner` 帳號上——那是一個與最小權限方向相反的副作用，而且
        會靜默地改變它能碰到哪些 spool。
        """

        from paulsha_cortex.coordinator import job_runner

        launcher = SubprocessLauncher("codex").as_write_forbidden()
        self.assertEqual(launcher._job_role(), job_runner.JOB_ROLE_BUILDER)
        self.assertFalse(launcher._is_review_persona())

    def test_preflight_reports_the_profile_the_job_will_actually_see(self) -> None:
        """design D2：preflight 報錯剖面就只是安慰劑。"""

        env = SubprocessLauncher("codex").as_write_forbidden().executor_environment()
        self.assertEqual(env.name, "codex:write-forbidden")


# ---------------------------------------------------------------------------
# 5. workflow lane 的接線
# ---------------------------------------------------------------------------

class _RecordingLauncher:
    """只記錄被套用了哪些特化——不繼承 `SubprocessLauncher`（測試 MUST 注入 fake）。"""

    def __init__(self) -> None:
        self.applied: list[str] = []

    def as_read_only(self) -> "_RecordingLauncher":
        self.applied.append("read_only")
        return self

    def as_review_only(self, *, terminal_kind: str) -> "_RecordingLauncher":
        self.applied.append("review_only")
        return self

    def as_commit_required(self) -> "_RecordingLauncher":
        self.applied.append("commit_required")
        return self

    def as_write_forbidden(self) -> "_RecordingLauncher":
        self.applied.append("write_forbidden")
        return self


def _step(**kwargs) -> SimpleNamespace:
    base = {
        "persona": "builder",
        "phase": "build",
        "card": "some-card",
        "commit_policy": None,
        "outputs": (),
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class WorkflowSpecializationTests(unittest.TestCase):

    def test_the_original_symptom_card_is_downgraded(self) -> None:
        """`worktree-isolation`＝#716 的原症狀卡（`commit_policy=forbidden`、零產出）。

        它的 `commit_policy` 來自 `manager._LEGACY_CARD_EXECUTION` 的補值表，因此這條
        同時證明降級判準吃得到**補值後**的有效契約，而不是只看 `step.commit_policy`。
        """

        launcher = _RecordingLauncher()
        out = manager_module._specialize_workflow_launcher(
            launcher, _step(card="worktree-isolation")
        )
        self.assertIs(out, launcher)
        self.assertEqual(launcher.applied, ["write_forbidden"])

    def test_commit_required_card_is_untouched(self) -> None:
        launcher = _RecordingLauncher()
        manager_module._specialize_workflow_launcher(
            launcher, _step(card="subagent-build")
        )
        self.assertEqual(launcher.applied, ["commit_required"])

    def test_forbidden_card_with_declared_outputs_keeps_workspace_write(self) -> None:
        launcher = _RecordingLauncher()
        manager_module._specialize_workflow_launcher(
            launcher, _step(commit_policy="forbidden", outputs=("reports/x.md",))
        )
        self.assertEqual(launcher.applied, [])

    def test_card_without_any_commit_policy_keeps_workspace_write(self) -> None:
        launcher = _RecordingLauncher()
        manager_module._specialize_workflow_launcher(launcher, _step())
        self.assertEqual(launcher.applied, [])

    def test_planner_and_reviewer_specialization_is_unchanged(self) -> None:
        """planner／reviewer 仍走它們原本那一支，本票沒有插隊。"""

        planner = _RecordingLauncher()
        manager_module._specialize_workflow_launcher(
            planner, _step(persona="planner", commit_policy="forbidden")
        )
        self.assertEqual(planner.applied, ["read_only", "write_forbidden"])
        reviewer = _RecordingLauncher()
        manager_module._specialize_workflow_launcher(
            reviewer, _step(persona="reviewer", phase="verify", commit_policy="forbidden")
        )
        self.assertEqual(reviewer.applied, ["review_only", "write_forbidden"])

    def test_a_launcher_without_the_capability_keeps_today_s_behaviour(self) -> None:
        """capability 缺席 ⇒ 維持現狀（`workspace-write`），**不是** fail-open。

        那正是今天的行為，也正是本票的保守方向：不確定就不降。真實 launcher 一定有
        這支（`LauncherContractTests` 釘住）。
        """

        class _Legacy:
            def __init__(self) -> None:
                self.applied: list[str] = []

            def as_commit_required(self) -> "_Legacy":
                self.applied.append("commit_required")
                return self

        legacy = _Legacy()
        out = manager_module._specialize_workflow_launcher(
            legacy, _step(card="worktree-isolation")
        )
        self.assertIs(out, legacy)
        self.assertEqual(legacy.applied, [])


# ---------------------------------------------------------------------------
# 6. 探針：清單機械導出、每一條都帶 -c
# ---------------------------------------------------------------------------

class ProbeDerivationTests(unittest.TestCase):

    def setUp(self) -> None:
        self.text = "\n".join(
            permgen.build_inner_sandbox_probe(permgen.SCHEMES["four-way"])
        )

    def test_every_emitted_mode_is_probed(self) -> None:
        for mode in registry.emitted_sandbox_modes():
            self.assertIn(f"sandbox_mode='\"{mode}\"'", self.text, mode)

    def test_it_probes_the_production_write_mode(self) -> None:
        """#715 假綠的核心：探針從沒碰過 builder 的 `workspace-write`。"""

        self.assertIn(
            f"sandbox_mode='\"{registry.SANDBOX_MODE_WORKSPACE_WRITE}\"'", self.text
        )

    def test_it_carries_a_self_check_on_the_derived_list(self) -> None:
        """清單不含寫入形態 ⇒ 探針**當場停並印出理由**。"""

        self.assertIn("PSC_716_MODES=(", self.text)
        self.assertIn(
            f'*" {registry.SANDBOX_MODE_WORKSPACE_WRITE} "*', self.text
        )
        self.assertIn("不含 workspace-write", self.text)

    def test_the_generator_refuses_a_read_only_only_derivation(self) -> None:
        """手抄成只剩唯讀族時，產生器自己就 fail-closed。"""

        original = registry.SANDBOX_MODE_DERIVATION
        try:
            registry.SANDBOX_MODE_DERIVATION = tuple(
                row for row in original
                if row.sandbox_mode != registry.SANDBOX_MODE_WORKSPACE_WRITE
            )
            with self.assertRaises(ValueError):
                permgen.build_inner_sandbox_probe(permgen.SCHEMES["four-way"])
        finally:
            registry.SANDBOX_MODE_DERIVATION = original

    def test_each_mode_has_a_negative_control(self) -> None:
        """每個 mode 都要有「不帶旗標必須**仍然**失敗」那一半。"""

        for mode in registry.emitted_sandbox_modes():
            self.assertIn(f"1[{mode}])", self.text, mode)
            self.assertIn(f"3[{mode}])", self.text, mode)
        self.assertIn("Can't read /proc/sys/kernel/overflowuid", self.text)

    def test_the_config_toml_false_green_trap_is_documented(self) -> None:
        """`codex sandbox` **忽略** `config.toml` 的 `sandbox_mode`，只吃 `-c`。"""

        self.assertIn("config.toml", self.text)
        self.assertIn("-c sandbox_mode=", self.text)

    def test_the_expected_red_is_spelled_out(self) -> None:
        """落地後仍是紅的——那是誠實狀態，逐字寫在探針裡。"""

        self.assertIn("rc=101", self.text)
        self.assertIn("linux_run_main.rs:318", self.text)
        self.assertIn("寫入卡那一半未解", self.text)

    def test_it_never_hand_assembles_the_hardening_surface(self) -> None:
        """D13：探針一行 `--property=`／`--setenv=` 都不自組。"""

        self.assertNotIn("--property=", self.text)
        self.assertNotIn("--setenv=", self.text)
        self.assertIn(permgen.PATH_PROBE_HELPER, self.text)


# ---------------------------------------------------------------------------
# 7. 明確測不到的 OS 層語意（#638／#657／#714 的教訓：不留假綠）
# ---------------------------------------------------------------------------

class OsLevelSemanticsTests(unittest.TestCase):

    @unittest.skip(
        "需要真的起一份模板 unit（root ＋ systemd ＋ 四個 job 帳號）並讓 codex 在裡面"
        "真的導出一次 permission profile。本 repo 的測試進程是單 UID、無 systemd 加固"
        "面，那個環境下 bwrap 起得來、`workspace-write` 也不會 panic——兩個方向都重現"
        "不了，跑了只會得到一個與 production 無關的綠燈。實機語意由 "
        "`trust_root inner-sandbox-probe` 產生的 per-mode 矩陣涵蓋，量到的值逐字記在 "
        "PR body 與 runbook 第 4e-2g 步。"
    )
    def test_workspace_write_really_panics_under_the_real_hardening_surface(self) -> None:
        raise AssertionError("unreachable")


class NarrowStepShapeTests(unittest.TestCase):
    """`_specialize_workflow_launcher` 也被 preflight 那條路以更窄的 step 形狀呼叫。

    缺 `outputs` 欄 ⇒ 判準回 `False` ⇒ 維持現狀的 `workspace-write`，與「契約缺欄
    不猜」逐字一致。寫成測試而不是註解，是因為它是一條**真的被踩到**的形狀差異
    （`tests/test_dispatch_runtime_preflight.py` 的 `_FakeStep` 沒有那一欄）。
    """

    def test_a_step_without_outputs_keeps_workspace_write(self) -> None:
        launcher = _RecordingLauncher()
        step = SimpleNamespace(
            persona="builder", phase="build", card="worktree-isolation",
            commit_policy="forbidden",
        )
        manager_module._specialize_workflow_launcher(launcher, step)
        self.assertEqual(launcher.applied, [])
