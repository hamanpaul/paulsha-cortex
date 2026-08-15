"""issue #554：taxonomy marker 詞界化 ＋ worktree drift 改判 environment。

兩個相關缺陷共用同一組 fixture（drift 訊息），因此併在同一份測試裡：

**缺陷一——`<unavailable>` 佔位符誤中 transient marker。**
`planning_runtime._operator_drift_message` 的尾端是 `evidence={location}`，而
`location` 的退化值原本是 `<unavailable>`；`outcome_taxonomy.
TRANSIENT_SERVICE_MARKERS` 含裸 `"unavailable"`（#533 為 agy 的
`UNAVAILABLE (code 503)` 而收）。於是**備份與報告雙雙寫入失敗**時，一個純環境
事件會靠子字串巧合被判成 transient-service。這是 #500（`\\btimeout\\b` 命中
nested tool result）／#487（`oauth` 命中 `doc-coauthoring`）的同族缺陷。

修法兩邊都做，缺一不可：
- marker 比對加詞界——擋住 marker 被埋在更長 token 裡的誤中（`workflow-1a503f`
  裡的 `503`、`envelope_unavailable` 裡的 `unavailable`）；
- 佔位符本身不得含 marker——詞界擋不住「整個 token 就是 marker」的相撞，
  `<unavailable>` 與 `<evidence-unavailable>` 都會照樣命中（`<`／`>`／`-` 都不是
  word char）。

**缺陷二——worktree drift 仍歸 `content` → `recover-planning` 禁用 → 永久死鎖。**
#543 之後 drift 的處置已改為「一個位元組都不動、只備份與報告」，語意上是環境
事件，改判 `environment` 讓 `recover-planning` 可用。判準用穩定前綴，不得依賴
訊息尾段——尾段已經在 #543 改過一次（`changes rolled back` →
`operator content preserved`）。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, outcome_taxonomy, planning_runtime
from paulsha_cortex.coordinator.claim import (
    ClaimCandidate,
    WorkAuthority,
    _resume_decision,
    build_claim_key,
    load_work_authority,
    work_authority_digest,
)
from paulsha_cortex.coordinator.outcome_taxonomy import matches_transient_service_markers
from paulsha_cortex.coordinator.planning_runtime import _operator_drift_message

# `run_heterogeneous_brainstorm` 對 launcher 例外的包裝格式（planning.py 四個
# `except` 分支共用）：`<stage>-<kind>: <ExceptionTypeName>: <str(exc)[:160]>`。
_WRAP = "primary-integration-malformed: ValueError: "

# #543 之前的 drift 訊息尾段。留在測試裡是為了釘住「判準不依賴尾段」——這串字
# 已經被改過一次，再改一次也必須照樣分類正確。
_LEGACY_DRIFT_MESSAGE = (
    "planning launcher modified operator worktree; changes rolled back "
    "(added=1 modified=0 removed=0); evidence=/tmp/psc/report.json"
)


def _drift_message(*, report_path: str | None = None, backup_root: str | None = None) -> str:
    return _operator_drift_message(
        {
            "counts": {"added": 1, "modified": 2, "removed": 0},
            "report_path": report_path,
            "backup_root": backup_root,
        }
    )


# ---------------------------------------------------------------------------
# 缺陷一：佔位符不得含 taxonomy marker
# ---------------------------------------------------------------------------


def test_degraded_drift_message_is_not_a_transient_service_failure() -> None:
    """備份與報告都寫不出去時，drift 仍然不是 transient-service（修法前為 True）。"""

    message = _drift_message()
    assert planning_runtime.PLANNING_WORKTREE_DRIFT_EVIDENCE_PLACEHOLDER in message
    assert matches_transient_service_markers(_WRAP + message) is False


def test_the_old_placeholder_is_the_documented_collision() -> None:
    """釘住缺陷本體：舊佔位符確實會命中 marker，所以它非換不可。"""

    assert matches_transient_service_markers("evidence=<unavailable>") is True
    # 連 issue 裡順手提到的 `<evidence-unavailable>` 也一樣會撞——`-` 不是 word
    # char，詞界照樣成立。這條是給未來改佔位符的人看的。
    assert matches_transient_service_markers("evidence=<evidence-unavailable>") is True


def test_drift_evidence_placeholder_contains_no_marker() -> None:
    """佔位符不變式：任何 taxonomy marker 都不得在佔位符內成立詞界命中。"""

    placeholder = planning_runtime.PLANNING_WORKTREE_DRIFT_EVIDENCE_PLACEHOLDER
    assert matches_transient_service_markers(placeholder) is False
    assert matches_transient_service_markers(f"evidence={placeholder}") is False


def test_normal_drift_message_with_real_evidence_path_stays_clean() -> None:
    message = _drift_message(report_path="/tmp/psc-report.json")
    assert "evidence=/tmp/psc-report.json" in message
    assert matches_transient_service_markers(_WRAP + message) is False


# ---------------------------------------------------------------------------
# 缺陷一：全表詞界不變式（同型風險掃描的可執行版本）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", outcome_taxonomy.TRANSIENT_SERVICE_MARKERS)
def test_every_marker_needs_word_boundaries(marker: str) -> None:
    """全表掃描：**每一個** marker 埋進更長的 word token 裡都不得命中。

    修法前 `"503"`／`"429"` 這種裸短字串對長訊息的誤中面極大——run id、
    content-addressed digest、evidence 路徑裡出現三個數字是家常便飯。
    """

    assert matches_transient_service_markers(f"prefix{marker}suffix") is False
    assert matches_transient_service_markers(f"9{marker}9") is False
    assert matches_transient_service_markers(f"x_{marker}_x") is False
    # 反面：獨立成詞時仍必須命中，詞界化不得把真訊號一起關掉。
    assert matches_transient_service_markers(f"launcher said: {marker} — retry later") is True


def test_short_numeric_markers_no_longer_hit_ids_and_paths() -> None:
    """實際誤中面：run id／digest／evidence 路徑裡的數字片段。"""

    assert (
        matches_transient_service_markers(
            _WRAP
            + "planning launcher modified operator worktree; operator content preserved "
            "(added=1 modified=0 removed=0); evidence=/tmp/x/workflow-1a503f0429ab/report.json"
        )
        is False
    )
    assert matches_transient_service_markers("run_id=workflow-88d089d7429654a75503") is False
    # 真的 HTTP status 仍要命中。
    assert matches_transient_service_markers("Eligibility check failed: UNAVAILABLE (code 503)") is True
    assert matches_transient_service_markers("HTTP/1.1 429 Too Many Requests") is True


def test_internal_snake_case_reason_values_no_longer_hit_unavailable() -> None:
    """`envelope_unavailable`／`provider_unavailable` 是內部欄位值，不是服務錯誤。"""

    assert matches_transient_service_markers("bypass=envelope_unavailable") is False
    assert matches_transient_service_markers("preflight=provider_unavailable") is False
    # `service_unavailable` 自己在表上，仍命中。
    assert matches_transient_service_markers('{"code": "service_unavailable"}') is True


@pytest.mark.parametrize(
    "reason",
    [
        # 詞界化前靠子字串巧合命中的真陽性；改為顯式列舉後必須照樣命中。
        "provider says the account is rate limited until 03:00",
        "rate limiting in effect",
        "two rate limits tripped",
        "primary-integration-malformed: TimeoutExpired: Command '['claude', '--model', "
        "'claude-opus-4', '--print', '--output-format', 'json', '--add-dir', '/tmp/psc-xxxx",
        "primary-integration-malformed: TimeoutError: connection lost",
        "secondary-output-malformed: ValueError: ServiceUnavailable",
    ],
)
def test_boundary_fix_keeps_the_true_positives(reason: str) -> None:
    assert matches_transient_service_markers(reason) is True


def test_shared_marker_table_stays_the_single_source() -> None:
    """#533 的別名不得在本次修改中漂開（`test_outcome_taxonomy` 的同一條約束）。"""

    assert manager._PLANNING_TRANSIENT_SERVICE_MARKERS is outcome_taxonomy.TRANSIENT_SERVICE_MARKERS


# ---------------------------------------------------------------------------
# 缺陷二：drift 判準用穩定前綴，分類落 environment
# ---------------------------------------------------------------------------


def test_drift_predicate_matches_both_message_generations() -> None:
    """新舊兩種訊息字串都要被認出來——尾段已在 #543 改過一次。"""

    current = _WRAP + _drift_message(report_path="/tmp/psc-report.json")
    legacy = _WRAP + _LEGACY_DRIFT_MESSAGE

    assert manager._is_planning_worktree_drift_failure(current) is True
    assert manager._is_planning_worktree_drift_failure(legacy) is True
    # 連退化到佔位符的那一版也要認得。
    assert manager._is_planning_worktree_drift_failure(_WRAP + _drift_message()) is True


def test_drift_predicate_rejects_the_sandbox_family() -> None:
    """拋棄式沙箱被寫壞是 launcher 行為異常，不是環境事件——維持 `content`。"""

    sandbox = _WRAP + "planning launcher modified disposable read-only sandbox"
    assert manager._is_planning_worktree_drift_failure(sandbox) is False
    assert manager._classify_planning_failure(sandbox) == "content"


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "brainstorm-not-ready",
        "primary-integration-malformed: ValueError: planning launcher result is not "
        "JSON: 我認為這個問題應該從三個面向來分析",
    ],
)
def test_drift_predicate_does_not_touch_other_reasons(reason: str | None) -> None:
    assert manager._is_planning_worktree_drift_failure(reason) is False
    assert manager._classify_planning_failure(reason) == "content"


@pytest.mark.parametrize(
    "message",
    [
        _LEGACY_DRIFT_MESSAGE,
        "planning launcher modified operator worktree; operator content preserved "
        "(added=1 modified=2 removed=0); evidence=/tmp/psc/report.json",
        "planning launcher modified operator worktree; operator content preserved "
        "(added=0 modified=0 removed=0); evidence=<not-written>",
    ],
)
def test_drift_classifies_as_environment(message: str) -> None:
    assert manager._classify_planning_failure(_WRAP + message) == "environment"


def test_prefix_constant_is_actually_the_message_prefix() -> None:
    """常數與訊息不得漂開——判準整個建立在這條上。"""

    assert _drift_message().startswith(planning_runtime.PLANNING_WORKTREE_DRIFT_MESSAGE_PREFIX)
    assert _LEGACY_DRIFT_MESSAGE.startswith(
        planning_runtime.PLANNING_WORKTREE_DRIFT_MESSAGE_PREFIX
    )
    assert manager._PLANNING_WORKTREE_DRIFT_MARKER == (
        planning_runtime.PLANNING_WORKTREE_DRIFT_MESSAGE_PREFIX
    )


# ---------------------------------------------------------------------------
# 缺陷二：recover-planning 對 drift 案例可用（`_resume_decision` 的 next_actions）
# ---------------------------------------------------------------------------


def _snapshot(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-1",
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "mapped_issues": [12],
                        "mapped_prs": [8],
                        "mapped_openspec": ["demo"],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": ["issue:12@open", "openspec:demo@1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _authority(tmp_path: Path) -> WorkAuthority:
    return load_work_authority(
        repo="acme/demo",
        work_id="demo",
        snapshot_path=_snapshot(tmp_path / "snapshot.json"),
    )


def _needs_human_candidate(tmp_path: Path, *, reason: str) -> ClaimCandidate:
    authority = _authority(tmp_path)
    base = ClaimCandidate(
        authority=authority,
        repo="acme/demo",
        work_id="demo",
        source_revisions=authority.source_revisions,
        confirmed_todo=authority.confirmed_todo,
        confirmed_issue=12,
        auto_label=False,
        active_run_id=None,
        active_claim_key=None,
    )
    return replace(
        base,
        active_run_id="workflow-" + "a" * 20,
        active_claim_key=build_claim_key(base),
        active_status="needs_human",
        active_snapshot_hash=authority.snapshot_hash,
        active_source_revisions=authority.source_revisions,
        active_provider_revision=authority.github_provider_revision,
        active_authority_digest=work_authority_digest(authority),
        active_phase="define",
        # 分類刻意由**產品判準**算出來，測試不自己填答案——這條就是把
        # 「drift → environment」與「environment → recover-planning」綁在一起。
        active_planning_failure_classification=manager._classify_planning_failure(reason),
        active_planning_failure_reason=reason,
    )


@pytest.mark.parametrize(
    "message",
    [
        _LEGACY_DRIFT_MESSAGE,
        "planning launcher modified operator worktree; operator content preserved "
        "(added=1 modified=0 removed=0); evidence=/tmp/psc/report.json",
        "planning launcher modified operator worktree; operator content preserved "
        "(added=1 modified=0 removed=0); evidence=<not-written>",
    ],
)
def test_recover_planning_is_available_for_drift(tmp_path: Path, message: str) -> None:
    """drift 卡在 define 時，唯一出口不再只有 abandon。"""

    reason = _WRAP + message
    decision = _resume_decision(_needs_human_candidate(tmp_path, reason=reason))

    assert decision.action == "needs_human"
    assert decision.next_actions == ("recover-planning", "abandon")
    assert decision.blocking_reason == f"planning-failure:environment:{reason}"


def test_content_failures_still_only_offer_abandon(tmp_path: Path) -> None:
    """#393 的 fail-closed 一字未動：內容缺陷仍不得由本路徑繞過。"""

    reason = _WRAP + "planning launcher result is not JSON: 我先說明一下我的規劃方向"
    decision = _resume_decision(_needs_human_candidate(tmp_path, reason=reason))

    assert decision.next_actions == ("abandon",)


def test_drift_reason_stays_single_line_for_recover_planning(tmp_path: Path) -> None:
    """`recover-planning` 拒收含換行的 failure_reason——drift 訊息必須單行。"""

    assert "\n" not in _drift_message()
    assert "\n" not in _drift_message(report_path="/tmp/psc/report.json")
