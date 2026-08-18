"""issue #682（#672 票 A）：錯誤語意三分 ＋ 逐候選拒因表。

修法前 `select_secondary_planner()` 失敗時只回一個字面值
`no-heterogeneous-planner`——它把三類結構上完全不同的失敗（憑證缺失／
executor 啟動失敗／輸出不合約）壓成同一個「拓撲問題」。#670 就是被這樣誤診的：
真因是 `agy models` 兩欄漂移 ＋ code fence，blocking reason 卻說「沒有異質
planner」，排查方向整個帶偏，最後靠人工重跑六遍才看出來。

本檔釘住的契約：
- 每一個 planning-capable 候選為什麼落選都逐條記在 `SecondarySelection.rejections`；
- `run_heterogeneous_brainstorm` 把該表渲染進 `BrainstormResult.reason`，
  且該 reason **可用正規表示式釘住必含拒因表**（issue #682 驗收第一條）；
- PR #674 補的 probe stdout 節錄**活著抵達** blocking reason，不在中途被壓掉；
- 拒因表中出現 environment 級拒因時 `_classify_planning_failure` 改判
  `environment`（讓 `recover-planning` 有路），全部是拓撲拒因時仍為 `content`
  （反向誤報同樣不可接受）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.model_identities import (
    ENVIRONMENT_GRADE_PLANNING_FAMILIES,
    PLANNING_FAILURE_EXECUTOR,
    PLANNING_FAILURE_EXECUTOR_SILENT_EXIT,
    PLANNING_FAILURE_JOB_START,
    PLANNING_FAILURE_OUTPUT,
    PLANNING_FAILURE_UNCLASSIFIED,
    REJECTION_PROBE_ABSENT,
    REJECTION_PROBE_IDENTITY_MISMATCH,
    REJECTION_PROBE_NOT_READY,
    REJECTION_SAME_DOMAIN,
    REJECTION_TABLE_DETAIL_LIMIT,
    REJECTION_TABLE_TOTAL_LIMIT,
    CandidateRejection,
    CapabilityProbe,
    IdentityRegistry,
    is_environment_grade_rejection_reason,
    probe_agy_capability,
    render_secondary_rejection_reason,
    select_secondary_planner,
)
from paulsha_cortex.coordinator.planning import (
    PlanningArtifact,
    PlanningScope,
    assess_planning_completeness,
    run_heterogeneous_brainstorm,
)

# ---------------------------------------------------------------------------
# 驗收條件（issue #682）：blocking reason 必含拒因表，且形狀可機械釘住。
#
#   no-heterogeneous-planner grade=<environment|content> candidates=<N> (<逐條>)
#
# 三段都是必要的：`grade` 讓下游分類不必去 substring-search 模型輸出；
# `candidates` 讓「表裡應該有幾條」成為可核對的數字，截斷再嚴也不會把
# 「有幾個候選、少了誰」變成不可知。
# ---------------------------------------------------------------------------
REJECTION_TABLE_RE = re.compile(
    r"\Ano-heterogeneous-planner grade=(?P<grade>environment|content) "
    r"candidates=(?P<count>\d+) \((?P<table>.+)\)\Z"
)
#: 逐條的頭段：`<executor>/<model_id>[<domain>]: <拒因>`。
REJECTION_ENTRY_RE = re.compile(
    r"(?P<executor>[a-z0-9_.-]+)/(?P<model_id>[^\[\]/]+)\[(?P<domain>[^\[\]]+)\]: "
    r"(?P<reason>same-domain|probe-absent|probe-not-ready|probe-identity-mismatch)"
)


SCOPE = PlanningScope(
    repo="hamanpaul/paulsha-cortex",
    work_id="unified-work-lifecycle",
    source_revision="tree:0123456789abcdef",
)

INCOMPLETE_SPEC = """\
---
status: draft
---
# Feature specification

## Requirements

BLOCKING: 尚未決定。
"""


def _registry() -> IdentityRegistry:
    """三個候選、三個 domain：codex 與 primary 同 domain，agy／claude 異質。"""

    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "codex",
                "model_id": "gpt-sibling",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "agy",
                "model_id": "gemini-3.1-pro-high",
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": "agy-plan-sandbox",
            },
            {
                "executor": "claude",
                "model_id": "claude-sonnet-4.6",
                "independence_domain": "anthropic",
                "capabilities": ["planning"],
            },
        ]
    )


FENCED_PROBE_STDOUT = (
    '```json {"capability":"cortex-plan-sandbox","model":"gemini-3.1-pro-high", '
    '"note":"模型多寫了一段散文所以 json.loads 失敗"} ```'
)


def _probes(*, agy_reason: str, agy_diagnostic: str) -> dict:
    return {
        ("agy", "gemini-3.1-pro-high"): CapabilityProbe(
            False, "agy", "gemini-3.1-pro-high", "google", agy_reason, agy_diagnostic
        ),
        ("claude", "claude-sonnet-4.6"): CapabilityProbe(
            False,
            "claude",
            "claude-sonnet-4.6",
            "anthropic",
            "safe-probe-failed",
            "FileNotFoundError",
        ),
    }


def _brainstorm(tmp_path: Path, probes) -> object:
    report = assess_planning_completeness(
        [PlanningArtifact(kind="spec", ref="docs/spec.md", text=INCOMPLETE_SPEC)]
    )
    assert report.complete is False
    return run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "gpt-primary"),
        registry=_registry(),
        probes=probes,
        evidence_dir=tmp_path,
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=lambda _: report.default_question_pack.to_dict(),
        secondary_planner=lambda *_: {},
        primary_integrator=lambda *_: {},
    )


# ---------------------------------------------------------------------------
# 1. 逐候選拒因表
# ---------------------------------------------------------------------------


def test_no_heterogeneous_planner_reason_carries_per_candidate_rejections(
    tmp_path: Path,
) -> None:
    """三個候選、三種落選理由，全部逐條出現在 blocking reason 裡。

    這就是「讓誤報不可能」的機制本身：格式問題與拓撲問題在同一個字串裡是
    兩個不同的欄位，讀的人不需要重跑六遍才發現。
    """

    result = _brainstorm(
        tmp_path,
        _probes(agy_reason="malformed-output", agy_diagnostic=FENCED_PROBE_STDOUT),
    )

    assert result.state == "needs_human"
    matched = REJECTION_TABLE_RE.match(result.reason)
    assert matched is not None, result.reason

    entries = REJECTION_ENTRY_RE.findall(matched.group("table"))
    by_executor = {entry[0]: entry for entry in entries}
    # 三個候選都在（primary 自己不算候選，不佔一條無資訊的 same-domain）。
    assert set(by_executor) == {"agy", "claude", "codex"}
    assert int(matched.group("count")) == 3

    assert by_executor["agy"][3] == REJECTION_PROBE_NOT_READY
    assert by_executor["claude"][3] == REJECTION_PROBE_NOT_READY
    assert by_executor["codex"][3] == REJECTION_SAME_DOMAIN
    # 同 domain 的手足是 `gpt-sibling`，不是 primary 自己。
    assert by_executor["codex"][1] == "gpt-sibling"

    # 三分的族名要逐條在列，`malformed-output`（格式）與 `safe-probe-failed`
    # 走 FileNotFoundError（executor 起不來）不得被壓成同一種東西。
    assert PLANNING_FAILURE_OUTPUT in result.reason
    assert PLANNING_FAILURE_EXECUTOR in result.reason
    assert "malformed-output" in result.reason
    assert "safe-probe-failed" in result.reason


def test_probe_diagnostic_survives_into_blocking_reason(tmp_path: Path) -> None:
    """PR #674 的 stdout 節錄必須端到端活著抵達 blocking reason。

    #674 讓 `probe_agy_capability` 失敗時帶 stdout 節錄，但那份節錄過去在
    `select_secondary_planner` 就被 `continue` 吃掉。這條測試釘住兩票的接縫。
    """

    result = _brainstorm(
        tmp_path,
        _probes(agy_reason="malformed-output", agy_diagnostic=FENCED_PROBE_STDOUT),
    )

    # 節錄的可辨識前綴（code fence ＋ JSON 開頭）必須逐字在 reason 裡。
    assert '```json {"capability":"cortex-plan-sandbox"' in result.reason


def test_probe_diagnostic_survives_from_real_agy_probe(tmp_path: Path) -> None:
    """接縫的另一半：節錄真的由 `probe_agy_capability` 產生時也活得到終點。

    不自造 diagnostic 字串，改用 #674 的真實產生路徑（`stdout_excerpt`），
    確認中間沒有任何一層把它壓掉。
    """

    raw = '```json\n{"capability":"cortex-plan-sandbox","model":"gemini-3.1-pro-high"}\n```extra'

    class _Completed:
        def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    responses = iter(
        [_Completed(0, "gemini-3.1-pro-high\n"), _Completed(0, raw)]
    )
    probe = probe_agy_capability(runner=lambda *a, **k: next(responses))
    assert probe.ready is False
    assert probe.reason == "malformed-output"
    assert probe.diagnostic

    result = _brainstorm(
        tmp_path,
        {
            ("agy", "gemini-3.1-pro-high"): probe,
            ("claude", "claude-sonnet-4.6"): CapabilityProbe(
                False,
                "claude",
                "claude-sonnet-4.6",
                "anthropic",
                "safe-probe-failed",
                "FileNotFoundError",
            ),
        },
    )
    assert probe.diagnostic in result.reason


def test_probe_absent_and_identity_mismatch_are_named_separately(tmp_path: Path) -> None:
    """probe 缺席與 probe 身分不符是兩件事，不得共用一個拒因。"""

    result = _brainstorm(
        tmp_path,
        {
            # agy 完全沒被 probe 過。
            ("claude", "claude-sonnet-4.6"): CapabilityProbe.ready_for(
                "claude", "claude-sonnet-4.6", "openai"
            ),
        },
    )
    assert REJECTION_PROBE_ABSENT in result.reason
    assert REJECTION_PROBE_IDENTITY_MISMATCH in result.reason


# ---------------------------------------------------------------------------
# 2. 分類改判
# ---------------------------------------------------------------------------


def test_environment_grade_rejection_reclassifies_to_environment(tmp_path: Path) -> None:
    """拒因表含 environment 級拒因 ⇒ `_classify_planning_failure` 回 environment。

    今天 `no-heterogeneous-planner` 一律落 `content`，而 `content` 在
    `_resume_decision` 一律不浮現 `recover-planning`，等於一條死路。
    """

    result = _brainstorm(
        tmp_path,
        _probes(agy_reason="models-probe-failed", agy_diagnostic="exit-code:1"),
    )
    assert manager._classify_planning_failure(result.reason) == "environment"
    assert REJECTION_TABLE_RE.match(result.reason).group("grade") == "environment"


def test_content_grade_rejections_stay_content(tmp_path: Path) -> None:
    """全部拒因都是拓撲／格式級 ⇒ 仍是 content（反向誤報同樣不可接受）。"""

    result = _brainstorm(
        tmp_path,
        _probes(agy_reason="malformed-output", agy_diagnostic="not-json"),
    )
    # claude 側改成內容級失敗，讓整張表沒有任何 environment 拒因。
    content_only = _brainstorm(
        tmp_path,
        {
            ("agy", "gemini-3.1-pro-high"): CapabilityProbe(
                False, "agy", "gemini-3.1-pro-high", "google", "malformed-output", "not-json"
            ),
            ("claude", "claude-sonnet-4.6"): CapabilityProbe(
                False,
                "claude",
                "claude-sonnet-4.6",
                "anthropic",
                "identity-mismatch",
                "",
            ),
        },
    )
    assert REJECTION_TABLE_RE.match(content_only.reason).group("grade") == "content"
    assert manager._classify_planning_failure(content_only.reason) == "content"
    # 第一個（claude 走 FileNotFoundError）本來就是 environment，兩者不同。
    assert manager._classify_planning_failure(result.reason) == "environment"


def test_all_same_domain_rejections_stay_content(tmp_path: Path) -> None:
    """純拓撲（全部同 domain）⇒ content，不得被誤報成環境問題。"""

    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "codex",
                "model_id": "gpt-sibling",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
        ]
    )
    selection = select_secondary_planner(
        registry=registry, primary=("codex", "gpt-primary"), probes={}
    )
    assert selection.reason == "no-heterogeneous-planner"
    assert [rejection.reason for rejection in selection.rejections] == [REJECTION_SAME_DOMAIN]
    rendered = render_secondary_rejection_reason(selection.reason, selection.rejections)
    assert REJECTION_TABLE_RE.match(rendered).group("grade") == "content"
    assert manager._classify_planning_failure(rendered) == "content"


def test_environment_grade_marker_is_anchored_and_not_spoofable_by_model_output() -> None:
    """grade 標記錨在字串開頭，模型輸出把族名寫進 stdout 也騙不到分類器。

    拒因表的 detail 帶的是**模型回應**。若分類器改用 substring-search
    `planning-executor-failed`，一個回「planning-executor-failed」的模型就能
    把 content 失敗偽裝成 environment。
    """

    spoof = CandidateRejection(
        executor="agy",
        model_id="gemini-3.1-pro-high",
        domain="google",
        reason=REJECTION_PROBE_NOT_READY,
        diagnostic=f"malformed-output {PLANNING_FAILURE_EXECUTOR} {PLANNING_FAILURE_JOB_START}",
        family=PLANNING_FAILURE_OUTPUT,
    )
    rendered = render_secondary_rejection_reason("no-heterogeneous-planner", (spoof,))
    assert PLANNING_FAILURE_EXECUTOR in rendered  # 字面確實出現在 detail 裡
    assert is_environment_grade_rejection_reason(rendered) is False
    assert manager._classify_planning_failure(rendered) == "content"


def test_unrelated_reasons_are_not_environment_graded() -> None:
    for reason in (
        None,
        "",
        "no-heterogeneous-planner",
        "question-pack-malformed: RuntimeError: boom",
        "…tail grade=environment candidates=1 (agy/x[google]: probe-absent)",
    ):
        assert is_environment_grade_rejection_reason(reason) is False


# ---------------------------------------------------------------------------
# 3. 三分的族名與判準
# ---------------------------------------------------------------------------


def test_named_failure_families_exist_and_grade_boundary_is_explicit() -> None:
    """三分的具名常數先定義好（票 C／票 E 才有東西可落），且分級邊界明確。"""

    assert PLANNING_FAILURE_JOB_START == "planning-job-start-failed"
    assert PLANNING_FAILURE_EXECUTOR == "planning-executor-failed"
    assert PLANNING_FAILURE_OUTPUT == "planning-output-malformed"
    assert PLANNING_FAILURE_EXECUTOR_SILENT_EXIT == "executor-silent-exit"
    assert ENVIRONMENT_GRADE_PLANNING_FAMILIES == frozenset(
        {PLANNING_FAILURE_JOB_START, PLANNING_FAILURE_EXECUTOR}
    )
    # 未知 probe 失敗一律 fail-closed 落 content，不擅自宣稱是環境問題。
    assert PLANNING_FAILURE_UNCLASSIFIED not in ENVIRONMENT_GRADE_PLANNING_FAMILIES


@pytest.mark.parametrize(
    ("probe_reason", "diagnostic", "family"),
    [
        ("models-probe-failed", "exit-code:1", PLANNING_FAILURE_EXECUTOR),
        ("models-probe-failed", "FileNotFoundError", PLANNING_FAILURE_EXECUTOR),
        ("smoke-failed", "exit-code:2", PLANNING_FAILURE_EXECUTOR),
        ("model-not-listed", "expected='x' available=[]", PLANNING_FAILURE_OUTPUT),
        ("malformed-output", "not-json", PLANNING_FAILURE_OUTPUT),
        ("identity-mismatch", "", PLANNING_FAILURE_OUTPUT),
        ("safe-probe-failed", "CalledProcessError", PLANNING_FAILURE_EXECUTOR),
        ("safe-probe-failed", "TimeoutExpired", PLANNING_FAILURE_EXECUTOR),
        ("safe-probe-failed", "FileNotFoundError", PLANNING_FAILURE_EXECUTOR),
        ("safe-probe-failed", "ValueError", PLANNING_FAILURE_OUTPUT),
        ("safe-probe-failed", "SomethingBrandNew", PLANNING_FAILURE_UNCLASSIFIED),
        (PLANNING_FAILURE_JOB_START, "polkit-denied", PLANNING_FAILURE_JOB_START),
        ("brand-new-probe-reason", "", PLANNING_FAILURE_UNCLASSIFIED),
    ],
)
def test_probe_failure_family_mapping(probe_reason: str, diagnostic: str, family: str) -> None:
    """probe 失敗 → 三分族的映射是一張明表，不是散落的 if。"""

    from paulsha_cortex.coordinator.model_identities import classify_probe_failure

    assert classify_probe_failure(probe_reason, diagnostic) == family


def test_agy_probe_marks_silent_exit_subclass() -> None:
    """rc≠0 且 stdout／stderr 皆空 ⇒ 具名 `executor-silent-exit`。

    這是整個家族裡最難查的一種——連錯誤訊息都沒有，歸因會落到模型、prompt、
    逾時或憑證，而不會落到執行環境。它必須被顯式命名。
    """

    class _Completed:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    responses = iter([_Completed(0, "gemini-3.1-pro-high\n"), _Completed(1)])
    probe = probe_agy_capability(runner=lambda *a, **k: next(responses))
    assert probe.reason == "smoke-failed"
    assert PLANNING_FAILURE_EXECUTOR_SILENT_EXIT in (probe.diagnostic or "")

    # 有 stderr 就不是 silent。
    noisy = iter([_Completed(0, "gemini-3.1-pro-high\n"), _Completed(2, "", "unsupported flag")])
    probe = probe_agy_capability(runner=lambda *a, **k: next(noisy))
    assert probe.reason == "smoke-failed"
    assert PLANNING_FAILURE_EXECUTOR_SILENT_EXIT not in (probe.diagnostic or "")


# ---------------------------------------------------------------------------
# 4. 截斷策略：長度有上限，但「哪一條被截掉」不得變成不可知
# ---------------------------------------------------------------------------


def test_rejection_table_never_drops_a_candidate_row() -> None:
    """五個候選、每個都帶超長 diagnostic：列數與身分一條不少。"""

    rejections = tuple(
        CandidateRejection(
            executor=f"exec{index}",
            model_id=f"model-{index}",
            domain=f"domain-{index}",
            reason=REJECTION_PROBE_NOT_READY,
            diagnostic="x" * 4000,
            family=PLANNING_FAILURE_OUTPUT,
        )
        for index in range(5)
    )
    rendered = render_secondary_rejection_reason("no-heterogeneous-planner", rejections)
    matched = REJECTION_TABLE_RE.match(rendered)
    assert matched is not None
    assert int(matched.group("count")) == 5
    for index in range(5):
        assert f"exec{index}/model-{index}[domain-{index}]" in rendered
    assert len(rendered) <= REJECTION_TABLE_TOTAL_LIMIT


def test_truncation_accounts_for_every_elided_character() -> None:
    """截掉多少字必須寫在原地——被截的是哪一條、少了多少，都看得見。"""

    long_detail = "y" * 900
    rejections = (
        CandidateRejection(
            executor="agy",
            model_id="gemini-3.1-pro-high",
            domain="google",
            reason=REJECTION_PROBE_NOT_READY,
            diagnostic=long_detail,
            family=PLANNING_FAILURE_OUTPUT,
        ),
    )
    rendered = render_secondary_rejection_reason("no-heterogeneous-planner", rejections)
    kept = REJECTION_TABLE_DETAIL_LIMIT
    assert f"…+{len(long_detail) - kept}c" in rendered
    assert "y" * kept in rendered


def test_over_budget_table_elides_detail_not_identity() -> None:
    """總預算爆掉時先犧牲 detail（並記下犧牲了幾個字），絕不犧牲身分列。"""

    rejections = tuple(
        CandidateRejection(
            executor=f"exec{index}",
            model_id=f"model-{index}",
            domain="domain",
            reason=REJECTION_PROBE_NOT_READY,
            diagnostic="z" * 1000,
            family=PLANNING_FAILURE_EXECUTOR,
        )
        for index in range(20)
    )
    rendered = render_secondary_rejection_reason("no-heterogeneous-planner", rejections)
    matched = REJECTION_TABLE_RE.match(rendered)
    assert matched is not None
    assert int(matched.group("count")) == 20
    for index in range(20):
        assert f"exec{index}/model-{index}[domain]" in rendered
    assert "detail-elided:" in rendered


def test_rendered_table_is_single_line() -> None:
    """reason 會進單行 log／`DiagnosticReason.rendered()`，不得帶換行或控制字元。"""

    rejections = (
        CandidateRejection(
            executor="agy",
            model_id="gemini-3.1-pro-high",
            domain="google",
            reason=REJECTION_PROBE_NOT_READY,
            diagnostic="line one\nline two\r\n\x1b[31mred\x1b[0m\ttab",
            family=PLANNING_FAILURE_OUTPUT,
        ),
    )
    rendered = render_secondary_rejection_reason("no-heterogeneous-planner", rejections)
    assert "\n" not in rendered and "\r" not in rendered and "\t" not in rendered
    assert "\x1b" not in rendered
    assert "line one line two" in rendered


def test_empty_rejection_table_leaves_reason_untouched() -> None:
    """沒有任何候選時 `no-heterogeneous-planner` 是真話（roster 裡真的沒有別人），
    reason 維持原字面值，不生出一張空表。"""

    assert render_secondary_rejection_reason("no-heterogeneous-planner", ()) == (
        "no-heterogeneous-planner"
    )
    assert render_secondary_rejection_reason(None, ()) is None


# ---------------------------------------------------------------------------
# 5. 機密：拒因表不得把憑證／token 帶進 log／evidence／blocking_reason
# ---------------------------------------------------------------------------


def test_rejection_table_carries_no_environment_or_argv_material(tmp_path: Path) -> None:
    """拒因表的輸入只有 roster 常數 ＋ probe 的 reason／diagnostic。

    `CandidateRejection` 沒有任何欄位接得到 env、argv、檔案內容或 stderr；
    probe 的 diagnostic 側最寬的來源是模型 stdout 節錄（固定 probe prompt 的
    回應）與例外**型別名**（`type(exc).__name__`，不含訊息），因此沒有把
    token／憑證帶進去的路徑。這條測試把「欄位面沒有洩漏通道」釘成契約。
    """

    import dataclasses

    fields = {field.name for field in dataclasses.fields(CandidateRejection)}
    assert fields == {"executor", "model_id", "domain", "reason", "diagnostic", "family"}

    secret = "sk-live-DEADBEEFdeadbeef"
    result = _brainstorm(
        tmp_path,
        _probes(agy_reason="malformed-output", agy_diagnostic="not-json"),
    )
    assert secret not in result.reason
    # roster 的三個欄位是常數，probe 兩欄是唯一的自由文字入口。
    assert result.reason.count("grade=") == 1
