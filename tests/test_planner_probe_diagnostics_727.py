"""issue #727：codex planner safe probe 的三個缺陷——落點、串流備援、診斷面。

## 病因（實機 0819 逐字，見 #727 第二則 comment）

模型端**完全正常**：真實加固面複本（`psc_run_under`，jit 剖面）下重跑 probe，
`agent_message` 逐字等於 `_probe_identity` 的 `expected`、rc=0。失敗全部在我們自己
的解析路徑上，而且是三層疊起來的：

1. `_planning_argv()` 對 codex 組 `-o str(Path(temp_dir)/"last.json")`——**第二份
   落點決定**。job 模式下 `temp_dir` 被硬填 `"/tmp"`，那是 unit 的 `PrivateTmp=yes`
   私有 /tmp：job 寫得進去、**Manager 讀不到** ⇒ 第二候選恆為 `None`。
   這與 `#714` 缺陷 2 逐字同型，而那張票已經替 builder lane 修好了
   （`job_workspace.job_last_message_path()`），只是沒涵蓋 planning 這條路。
2. `_extract_json` 的**串流備援根本不能用**：`_find_json_object()` 的頂層語意是
   「整串剛好是一個 JSON 物件」，而 codex 的 `--json` 是 JSONL ⇒ 整串 `json.loads`
   必敗 ⇒ `ValueError: planning launcher returned no JSON object`（含不含開頭那筆
   `use_legacy_landlock` 的 deprecation error item 都一樣）。
3. `safe-probe-failed` 的 diagnostic 只有例外**型別名**（`model_identities.py`
   逐字承認那是刻意的），所以四輪派工看到的都是 `ValueError` 五個字。

三層合起來的後果是**不可辨識性**：「落檔那條斷了」與「模型什麼都沒吐」在 operator
眼裡是同一個字串。本檔把三層各自釘住，外加一條迴歸不變式：`-o` 落點**不得有第二份
字面量決定**。
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from paulsha_cortex.coordinator import (
    job_workspace,
    manager,
    planning_probe_cache,
    planning_runtime,
    spool_slot,
)
from paulsha_cortex.coordinator.model_identities import (
    PLANNING_DIAGNOSTIC_LIMIT,
    PLANNING_FAILURE_EXECUTOR,
    PLANNING_FAILURE_OUTPUT,
    ModelIdentity,
    classify_probe_failure,
    probe_exception_diagnostic,
)


IDENTITY = ModelIdentity(
    executor="codex",
    model_id="gpt-5.3-codex-spark",
    independence_domain="openai",
    capabilities=("planning",),
)

#: `_probe_identity` 對這個 identity 期待的逐字 payload。
EXPECTED = {
    "capability": "cortex-planning-json",
    "executor": "codex",
    "model": "gpt-5.3-codex-spark",
}

#: codex 每次都會先印的那一筆——`--enable use_legacy_landlock` 的 deprecation，
#: **以 `item.type=error` 進 `--json` 串流**（#714 實跑逐字記過）。
DEPRECATION_EVENT = json.dumps(
    {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "error",
            "message": "use_legacy_landlock is deprecated and will be removed soon",
        },
    }
)
AGENT_MESSAGE_EVENT = json.dumps(
    {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "agent_message",
            "text": json.dumps(EXPECTED),
        },
    }
)
TURN_COMPLETED_EVENT = json.dumps(
    {"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 22}}
)


def _stream(*, deprecation: bool) -> str:
    events = [AGENT_MESSAGE_EVENT, TURN_COMPLETED_EVENT]
    if deprecation:
        events.insert(0, DEPRECATION_EVENT)
    return "\n".join(events) + "\n"


# ---------------------------------------------------------------------------
# (2) 串流備援要真的能用——四格矩陣
# ---------------------------------------------------------------------------


class StreamFallbackMatrix(unittest.TestCase):
    """串流含／不含 deprecation error item × `last.json` 存在／不存在。

    修法前這四格裡有**三格**是 `ValueError`（見本檔 docstring 的實測表），而唯一
    活著的那一格（`last.json` 存在）在 job 模式下結構上到不了。
    """

    def test_stream_only_without_deprecation_item(self) -> None:
        self.assertEqual(
            planning_runtime._extract_json_candidates(_stream(deprecation=False), None),
            EXPECTED,
        )

    def test_stream_only_with_deprecation_item(self) -> None:
        """開頭那筆 `item.type=error` **結構上**遮不住輸出本體。

        做法不是「跳過 error」這種特例，而是比照
        `manager._extract_terminal_json()` 由尾端往回找——先看到 `turn.completed`
        （沒有文字欄位、自然跳過），再看到 `agent_message`，那筆 error 永遠排在
        它們後面才被走訪到。
        """

        self.assertEqual(
            planning_runtime._extract_json_candidates(_stream(deprecation=True), None),
            EXPECTED,
        )

    def test_last_message_present_keeps_priority_over_the_stream(self) -> None:
        """兩者皆有時**維持原行為**：第二候選優先，串流那條不發言。"""

        other = {"capability": "cortex-planning-json", "executor": "from-last-json"}
        for deprecation in (False, True):
            with self.subTest(deprecation=deprecation):
                self.assertEqual(
                    planning_runtime._extract_json_candidates(
                        _stream(deprecation=deprecation), json.dumps(other)
                    ),
                    other,
                )

    def test_extract_json_path_variant_covers_the_same_four_cells(self) -> None:
        """`_extract_json(stdout, output_path)` 那條簽章走的是同一份判準。"""

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "planning.last.json"
            for deprecation in (False, True):
                with self.subTest(deprecation=deprecation, last_json=False):
                    self.assertEqual(
                        planning_runtime._extract_json(
                            _stream(deprecation=deprecation), missing
                        ),
                        EXPECTED,
                    )
            missing.write_text(json.dumps(EXPECTED), encoding="utf-8")
            for deprecation in (False, True):
                with self.subTest(deprecation=deprecation, last_json=True):
                    self.assertEqual(
                        planning_runtime._extract_json(
                            _stream(deprecation=deprecation), missing
                        ),
                        EXPECTED,
                    )

    def test_a_real_probe_stream_makes_the_probe_ready(self) -> None:
        """端到端：把實機那份串流餵給 `_probe_identity`，probe **ready**。

        這是本票的解封鎖判準在單元層的投影——實機那一半走真實加固面（見 PR body）。
        """

        class _Runner:
            def __call__(self, argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=_stream(deprecation=True), stderr=""
                )

        probe = planning_runtime._probe_identity(
            IDENTITY,
            worktree=Path(__file__).resolve().parents[1],
            invoker=planning_runtime.InProcessPlanningInvoker(_Runner()),
            timeout_seconds=30,
        )
        self.assertTrue(probe.ready, probe.diagnostic)
        self.assertEqual(probe.executor, "codex")

    def test_the_captured_production_stream_makes_the_probe_ready(self) -> None:
        """0820 實機**真的那一份**串流（不是手造的）——`tests/fixtures/` 下逐位元組保存。

        取得方式（PR body 有完整逐字）：`psc_run_under cortex-reviewer-job-jit`
        全量導出 52 條 property，跑 `_planning_argv()` 產出的**production argv**，
        `-o` 指向 job 的 `PrivateTmp`（＝Manager 讀不到，修法前的那一格）。rc=0，
        `agent_message` 逐字等於 `expected`，且串流最前面**確實有**一筆
        `item.type=error`（0820 那次是 skills context budget 的警告，#714 那次是
        `use_legacy_landlock` 的 deprecation——同一個結構位置）。

        同一份輸入在 `origin/main` 上逐字是
        `ready=False reason=safe-probe-failed diagnostic='ValueError'`。
        """

        stream = (
            Path(__file__).with_name("fixtures") / "codex-planner-probe-0820.jsonl"
        ).read_text(encoding="utf-8")
        # 前提：這份 fixture 真的含那筆開頭的 error item，否則本測試沒有驗到重點。
        self.assertIn('"type":"error"', stream)

        class _Runner:
            def __call__(self, argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout=stream, stderr="")

        probe = planning_runtime._probe_identity(
            IDENTITY,
            worktree=Path(__file__).resolve().parents[1],
            invoker=planning_runtime.InProcessPlanningInvoker(_Runner()),
            timeout_seconds=30,
        )
        self.assertTrue(probe.ready, probe.diagnostic)

    def test_error_events_never_masquerade_as_model_output(self) -> None:
        """codex 的錯誤訊息常內嵌 JSON——**不得**被抽出來當成模型輸出本體。

        0819／0820 實機逐字：
        `unexpected status 400 Bad Request: {"detail":"The 'gpt-5.3-codex-spark'
        model requires a newer version of Codex…"}`。少了這條守衛，一次硬失敗會
        變成一份 `{"detail": …}`，下游只看得到 `identity-mismatch`——比 `ValueError`
        更誤導。
        """

        hostile = (
            json.dumps(
                {
                    "type": "error",
                    "message": 'unexpected status 400 Bad Request: {"detail":"needs newer codex"}',
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "stream_error",
                    "message": 'retrying: {"detail":"needs newer codex"}',
                }
            )
            + "\n"
        )
        with self.assertRaises(ValueError) as ctx:
            planning_runtime._extract_json_candidates(hostile, None)
        self.assertIn("no JSON object", str(ctx.exception))

    def test_non_stream_prose_still_fails_closed(self) -> None:
        """串流備援**不得**變成「從任何散文裡撈 JSON」——非串流輸出照樣 fail-closed。

        `_find_json_object()` 的頂層嚴格語意是 #670 刻意收窄的（帶前言的輸出不得被
        誤判為合法 JSON）。#727 只讓 **JSONL 事件的文字欄位**多一條路，頂層那條一個
        位元組都沒放寬。
        """

        with self.assertRaises(ValueError) as ctx:
            planning_runtime._extract_json_candidates(
                'Commentary.\n```json\n{"schema_version": 1}\n```\n', None
            )
        self.assertIn("no JSON object", str(ctx.exception))

    def test_stream_lines_that_do_not_parse_are_skipped(self) -> None:
        """job 模式的 stdout 是 shim 併流後的產物（R-5），夾雜非 JSON 是常態。"""

        noisy = (
            "warning: something on fd 2\n"
            + DEPRECATION_EVENT
            + "\nnot json at all {{{\n"
            + AGENT_MESSAGE_EVENT
            + "\n"
            + TURN_COMPLETED_EVENT
            + "\ntrailing noise\n"
        )
        self.assertEqual(
            planning_runtime._extract_json_candidates(noisy, None), EXPECTED
        )


# ---------------------------------------------------------------------------
# (1) `-o` 落點由單一來源機械導出
# ---------------------------------------------------------------------------


class LastMessageLandingIsDerivedOnce(unittest.TestCase):
    def test_planning_argv_has_no_landing_of_its_own(self) -> None:
        """`_planning_argv` **必須**由呼叫端交出落點，不得自己組一份。

        這是本票的迴歸不變式：`last_message_path` 是必填的關鍵字參數（沒有預設值
        ——留一個預設值等於把那份決定藏起來），因此漏傳的症狀是 `TypeError`，
        而不是三個月後實機上的一次「argv 指著 A、job 寫得到的是 B」。
        """

        with self.assertRaises(TypeError):
            planning_runtime._planning_argv(  # type: ignore[call-arg]
                IDENTITY, "PROMPT", "/tmp", Path("/scratch/cwd")
            )

    def test_codex_argv_uses_exactly_what_the_caller_handed_over(self) -> None:
        landing = Path("/var/lib/cortex-reviewer-planner/planning-logs/x/planning.last.json")
        argv = planning_runtime._planning_argv(
            IDENTITY, "PROMPT", "/tmp", Path("/scratch/cwd"), last_message_path=landing
        )
        self.assertEqual(argv[argv.index("-o") + 1], str(landing))

    def test_the_rule_is_the_one_714_already_wrote_down(self) -> None:
        """落點規則＝`job_workspace.job_last_message_path()`，**不是**第二條規則。

        `planning_last_message_path()` 刻意只是轉呼叫、一個判斷都沒有；它存在的
        理由是讓兩個 invoker 的呼叫點在 grep 上是同一個字。
        """

        anchor = Path("/lg/inst") / job_workspace.PLANNING_JOB_LOG_FILENAME
        self.assertEqual(
            planning_runtime.planning_last_message_path(anchor),
            job_workspace.job_last_message_path(anchor),
        )
        self.assertEqual(
            planning_runtime.planning_last_message_path(anchor),
            Path("/lg/inst/planning.last.json"),
        )

    def test_no_second_literal_survives_in_the_source(self) -> None:
        """原始碼層面的迴歸釘子：兩支檔案裡不得再出現裸的 `"last.json"` 字面量。

        #714 缺陷 2 在 planning 這條路重演的機制逐字是「第二份落點決定」。字面量
        比對是這條不變式唯一能在單 UID 環境下驗到的形態——真正的落點正確性要跨
        UID 才驗得出來（見 runbook）。
        """

        for module in (planning_runtime, __import__(
            "paulsha_cortex.coordinator.planning_job", fromlist=["planning_job"]
        )):
            source = Path(module.__file__).read_text(encoding="utf-8")
            code = "\n".join(
                line for line in source.splitlines() if not line.lstrip().startswith("#")
            )
            with self.subTest(module=module.__name__):
                self.assertNotIn('/ "last.json"', code)
                self.assertNotIn('"/tmp/last.json"', code)

    def test_in_process_invoker_derives_the_landing_from_the_same_anchor(self) -> None:
        """direct 模式：argv 的 `-o` 與讀回端指的是**同一個物件**。"""

        seen: list[list[str]] = []

        def runner(argv, **kwargs):
            seen.append(list(argv))
            Path(argv[argv.index("-o") + 1]).write_text(
                json.dumps(EXPECTED), encoding="utf-8"
            )
            return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")

        outcome = planning_runtime.InProcessPlanningInvoker(runner).run(
            planning_runtime.PlanningInvocation(
                identity=IDENTITY,
                prompt="PROMPT",
                purpose=planning_runtime.PLANNING_PURPOSE_PROBE,
                timeout_seconds=30,
                worktree=Path(__file__).resolve().parents[1],
            )
        )
        self.assertEqual(json.loads(outcome.output_text or ""), EXPECTED)
        landing = seen[-1][seen[-1].index("-o") + 1]
        self.assertTrue(landing.endswith("/planning.last.json"), landing)
        self.assertIsNotNone(outcome.last_message)
        self.assertIn(landing, outcome.last_message or "")


# ---------------------------------------------------------------------------
# (3) `safe-probe-failed`／`models-probe-failed` 落有界診斷
# ---------------------------------------------------------------------------


class BoundedProbeDiagnostics(unittest.TestCase):
    def test_the_budget_is_the_repo_wide_evidence_budget(self) -> None:
        """`evidence_char_limit=2000` 的慣例——兩個常數必須相等。

        `model_identities` 不 import `manager`（反向會成環），因此兩份字面量的
        一致性由本測試釘住，而不是由 import 期斷言。
        """

        self.assertEqual(PLANNING_DIAGNOSTIC_LIMIT, manager.RETRY_CONTEXT_EVIDENCE_LIMIT)
        self.assertEqual(PLANNING_DIAGNOSTIC_LIMIT, 2000)

    def test_safe_probe_failed_carries_rc_stdout_and_the_landing_state(self) -> None:
        """本票驗收第三條：diagnostic 不再只有例外型別名。"""

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 0, stdout="I am not JSON at all.", stderr=""
            )

        probe = planning_runtime._probe_identity(
            IDENTITY,
            worktree=Path(__file__).resolve().parents[1],
            invoker=planning_runtime.InProcessPlanningInvoker(runner),
            timeout_seconds=30,
        )
        self.assertFalse(probe.ready)
        self.assertEqual(probe.reason, "safe-probe-failed")
        diagnostic = probe.diagnostic or ""
        self.assertNotEqual(diagnostic, "ValueError")
        self.assertTrue(diagnostic.startswith("ValueError "), diagnostic)
        self.assertIn("rc=0", diagnostic)
        self.assertIn("stdout=", diagnostic)
        self.assertIn("I am not JSON", diagnostic)
        # `-o` 那一格到底寫進去了沒——這是四輪派工都問不出來的那一格。
        self.assertIn("last_message=", diagnostic)
        self.assertIn("planning.last.json|<absent>", diagnostic)
        self.assertLessEqual(len(diagnostic), PLANNING_DIAGNOSTIC_LIMIT + 1)

    def test_the_exception_type_stays_anchored_at_token_zero(self) -> None:
        """分級輸入逐字不變——**診斷不得讓任何 probe 換族，更不得讓它 ready**。"""

        cases = (
            (ValueError("x"), PLANNING_FAILURE_OUTPUT),
            (subprocess.TimeoutExpired(cmd="codex", timeout=1), PLANNING_FAILURE_EXECUTOR),
            (PermissionError("x"), PLANNING_FAILURE_EXECUTOR),
        )
        for exc, expected_family in cases:
            with self.subTest(exc=type(exc).__name__):
                bare = probe_exception_diagnostic(exc)
                self.assertEqual(bare, type(exc).__name__)
                self.assertEqual(
                    classify_probe_failure("safe-probe-failed", bare), expected_family
                )
                rich = probe_exception_diagnostic(
                    planning_runtime.attach_probe_diagnostic(
                        exc, "rc=1 stdout=<empty> last_message=/x/planning.last.json|<absent>"
                    )
                )
                self.assertTrue(rich.startswith(type(exc).__name__ + " "), rich)
                self.assertEqual(
                    classify_probe_failure("safe-probe-failed", rich), expected_family
                )

    def test_diagnostic_is_bounded_even_with_a_huge_stdout(self) -> None:
        detail = "rc=1 stdout=" + ("x" * 50_000)
        rich = probe_exception_diagnostic(
            planning_runtime.attach_probe_diagnostic(ValueError("boom"), detail)
        )
        self.assertLessEqual(len(rich), PLANNING_DIAGNOSTIC_LIMIT + 1)
        self.assertTrue(rich.startswith("ValueError "))

    def test_diagnostic_never_carries_stderr(self) -> None:
        """票 A 的邊界 #727 沒有動：stderr 是憑證／路徑原文最容易外洩的通道。"""

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                # 兩種最典型的 stderr 夾帶物：憑證字面值與憑證檔路徑。
                # （路徑刻意寫成 `~` 相對形式——R-21 不容忍原始碼裡的個人絕對路徑，
                # 而本測試要驗的是「diagnostic 不含 stderr」，與路徑形態無關。）
                stderr="OPENAI_API_KEY=sk-should-never-appear ~/.codex/auth.json",
            )

        probe = planning_runtime._probe_identity(
            IDENTITY,
            worktree=Path(__file__).resolve().parents[1],
            invoker=planning_runtime.InProcessPlanningInvoker(runner),
            timeout_seconds=30,
        )
        self.assertFalse(probe.ready)
        self.assertNotIn("sk-should-never-appear", probe.diagnostic or "")
        self.assertNotIn("auth.json", probe.diagnostic or "")

    def test_models_probe_failed_keeps_the_job_error_detail(self) -> None:
        """agy 那一格：`PlanningJobError` 的 detail 不得再被壓成型別名。

        實機 0819 逐字是 `reason=models-probe-failed diagnostic=PlanningJobError`
        ——族名對了、病因全丟（rc／unit／profile／binary／version／seccomp 全在
        `detail` 裡）。
        """

        from paulsha_cortex.coordinator.model_identities import probe_agy_capability
        from paulsha_cortex.coordinator.planning_job import PlanningJobError

        detail = (
            "rc=1 unit=cortex-reviewer-job@x.service profile=strict "
            "binary=/opt/cortex/toolchain/bin/agy version=0.0.1 seccomp_filter_fatal=no"
        )

        def runner(argv, **kwargs):
            raise PlanningJobError(PLANNING_FAILURE_EXECUTOR, detail)

        probe = probe_agy_capability(runner=runner)
        self.assertFalse(probe.ready)
        self.assertEqual(probe.reason, "models-probe-failed")
        self.assertTrue((probe.diagnostic or "").startswith("PlanningJobError "))
        self.assertIn("profile=strict", probe.diagnostic or "")
        self.assertIn("seccomp_filter_fatal=no", probe.diagnostic or "")
        # 分級的輸入是 reason（不是 diagnostic），因此族名逐字不變。
        self.assertEqual(
            classify_probe_failure("models-probe-failed", probe.diagnostic),
            PLANNING_FAILURE_EXECUTOR,
        )

    def test_landing_marker_distinguishes_three_states(self) -> None:
        """`<absent>`／`bytes=N`／`<unresolved:…>` 是三件事，不是一件。"""

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "planning.last.json"
            self.assertTrue(
                planning_runtime._last_message_marker(missing).endswith("|<absent>")
            )
            spool_slot.preseed_job_writable_file(missing)
            self.assertTrue(
                planning_runtime._last_message_marker(missing).endswith("|bytes=0")
            )
            self.assertIsNone(planning_runtime._read_last_message(missing))
            missing.write_text("abc", encoding="utf-8")
            self.assertTrue(
                planning_runtime._last_message_marker(missing).endswith("|bytes=3")
            )
            self.assertEqual(planning_runtime._read_last_message(missing), "abc")


# ---------------------------------------------------------------------------
# 順帶：probe cache 的憑證指紋曾經對 operator 說假話
# ---------------------------------------------------------------------------


class CredentialFingerprintStopsLying(unittest.TestCase):
    """`<absent>` 曾經同時代表「沒有這個檔」與「連查都查不動」。

    實機 0819 逐字：`fp.executor_credential =
    /var/lib/cortex-reviewer-planner/.claude/.credentials.json|<absent>`，**而那個檔
    已經存在**（`.claude` → `cache/claude` symlink，`cache/` 是 `0700
    cortex-reviewer-planner`，Manager 在 traverse 那一步拿 `EACCES`）。

    ⚠️ 本測試只釘「不再說假話」。**憑證輪替仍然不會讓 probe cache 失效**——兩個標記
    在同一個部署上都是恆定的。真正的修法需要換一個 Manager 看得見的失效訊號，或讓
    指紋由看得到那一格的身分算，兩條都擴散到部署面，記在 #727 的 PR body 與 comment。
    """

    def test_permission_denied_is_not_reported_as_absent(self) -> None:
        import os
        import tempfile

        if os.geteuid() == 0:  # pragma: no cover - root 下 EACCES 不成立
            self.skipTest("root 繞過 EACCES，這條在 root 下驗不到東西")
        with tempfile.TemporaryDirectory() as tmp:
            locked = Path(tmp) / "cache"
            locked.mkdir()
            leaf = locked / ".credentials.json"
            leaf.write_text("{}", encoding="utf-8")
            os.chmod(locked, 0o000)
            try:
                marker = planning_probe_cache._stat_marker(
                    str(leaf), fields=("size", "mtime_ns")
                )
            finally:
                os.chmod(locked, 0o700)
        self.assertNotIn(planning_probe_cache.FINGERPRINT_ABSENT, marker)
        self.assertIn(planning_probe_cache.FINGERPRINT_UNRESOLVED_PREFIX, marker)
        self.assertIn("PermissionError", marker)

    def test_a_genuinely_missing_file_still_says_absent(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            marker = planning_probe_cache._stat_marker(
                str(Path(tmp) / "nope.json"), fields=("size", "mtime_ns")
            )
        self.assertTrue(marker.endswith(f"|{planning_probe_cache.FINGERPRINT_ABSENT}"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
