"""#499／#500／#487／#485：統一 outcome classification taxonomy 的回歸測試。

四張 issue 是同一型缺陷的第三～六次命中：分類器把「不該當證據的文字」餵進
關鍵字比對，或反過來把「該當證據的結構化終局記錄」整個忽略。本檔的每一個
fixture 都用 issue 現場的實際輸出文字，確保修法對著真正的形狀。

- #499：Claude review 429 被投影成 foreign-review-absent、provider_outcome null。
- #500：tool parser 的 `timeout` 文字被誤判成 network transient。
- #487：init skill 清單裡的 `doc-coauthoring` 被誤判成 OAuth auth failure。
- #485：Codex 的 stdin banner 讓每次 foreign review 都成 invalid-process-output。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.coordinator import manager, outcome_taxonomy
from paulsha_cortex.coordinator.provider_outcome import (
    ProviderOutcome,
    SignalAuthority,
    classify_provider_failure,
)
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.github_rate_limit import is_auth_signal


# --------------------------------------------------------------- issue 現場 fixture


# #499 現場（job task-3-private-repo-and-forbidden-documentation-scan-build-3，
# 2026-08-13 00:54 Asia/Taipei）：五小時 session 額度用罄，stream-json 帶完整的
# 結構化限流證據。
ISSUE_499_CLAUDE_RATE_LIMIT_LOG = "\n".join(
    [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "tools": ["Bash", "Read"],
                "skills": ["copilot-sdk", "doc-coauthoring", "docx"],
            }
        ),
        json.dumps(
            {
                "type": "system",
                "subtype": "rate_limit_event",
                "rate_limit_event": {
                    "status": "rejected",
                    "rateLimitType": "five_hour",
                    "resetsAt": 1786554000,
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "api_error_status": 429,
                "terminal_reason": "api_error",
            }
        ),
    ]
)


# #500 現場（job task-4-deliver-export-tree-orchestrator-build-5）：controller
# SIGTERM 停掉 no-progress 的 Edit 迴圈；log tail 裡稍早有一筆被拒的 Bash 工具
# 呼叫，其 permission-denial 文字含 `timeout` 字樣。
ISSUE_500_TOOL_PARSER_TIMEOUT_LOG = "\n".join(
    [
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": "Parser aborted (timeout, resource limit, or over-length)",
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "[Request interrupted by user]",
                "terminal_reason": "aborted_streaming",
            }
        ),
    ]
)


# #487 現場（0.1.8 @ dc8a968）：連續四次 `InputValidationError: JSON parse
# failed` 後被人工中斷；log 含 Claude init 的正常 skill 清單。
ISSUE_487_DOC_COAUTHORING_LOG = "\n".join(
    [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "skills": ["copilot-sdk", "doc-coauthoring", "docx"],
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "重試中"}]},
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "result": "InputValidationError: JSON parse failed",
            }
        ),
    ]
)


# #485 現場（Codex CLI 0.147.0，`codex exec ... --json`）：JSONL 串流前先印一行
# adapter 自有 banner。
ISSUE_485_CODEX_BANNER_LOG = "\n".join(
    [
        "Reading additional input from stdin...",
        json.dumps({"type": "thread.started", "thread_id": "th_1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
    ]
)


# --------------------------------------------------------------- #499 build/review 分類


def test_issue_499_structured_rate_limit_event_classifies_as_rate_limited():
    """429 必須以結構化權威落 rate_limited，且保留 provider 給的重置時刻。"""

    result = classify_provider_failure(exit_code=1, output=ISSUE_499_CLAUDE_RATE_LIMIT_LOG)

    assert result.outcome is ProviderOutcome.RATE_LIMITED
    assert result.authority is SignalAuthority.STRUCTURED
    assert result.retryable is True
    assert result.reset_at == 1786554000


def test_issue_499_reset_at_survives_the_registry_payload_roundtrip():
    result = classify_provider_failure(exit_code=1, output=ISSUE_499_CLAUDE_RATE_LIMIT_LOG)
    payload = result.to_dict()

    assert payload["reset_at"] == 1786554000
    from paulsha_cortex.coordinator.provider_outcome import ProviderFailureClassification

    assert ProviderFailureClassification.from_dict(payload) == result


def test_provider_outcome_payload_without_reset_at_stays_four_keyed():
    """舊四鍵形狀不得改變——已部署的狀態檔仍要能被 fail-closed 驗證讀回。"""

    result = classify_provider_failure(exit_code=1, output="rate limit exceeded")

    assert set(result.to_dict()) == {"outcome", "authority", "reason", "retryable"}


def test_issue_499_review_lane_projects_provider_outcome_instead_of_absent():
    """review lane 的終局投影：不得再是 provider_outcome null ＋ foreign-review-absent。"""

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        reg = JobRegistry(state_path=root / "jobs.json")
        builder = reg.create_job(
            task="slice-review-429",
            persona="builder",
            branch="feature/slice-review-429",
            pane="",
            worktree=str(root / "candidate"),
            executor="copilot",
            session_name="slice-review-429",
            pid=1,
            log_path="/builder-log",
        )
        reviewer_log = root / "review.jsonl"
        reviewer_log.write_text(ISSUE_499_CLAUDE_RATE_LIMIT_LOG + "\n", encoding="utf-8")
        reviewer = reg.create_job(
            task="slice-review-429",
            persona="reviewer",
            kind="review",
            branch="feature/slice-review-429",
            pane="",
            worktree=str(root / "review"),
            executor="claude",
            model_id="claude-sonnet-4.5",
            independence_domain="anthropic",
            session_name="slice-review-429-2",
            pid=2,
            log_path=str(reviewer_log),
            subject_head="b" * 40,
            spec_hash="new-spec",
            plan_hash="new-plan",
            verification_hash="new-verification",
        )
        # `Dispatcher._finalize_headless` 在 finalize 當下寫下的分類。
        classification = classify_provider_failure(
            exit_code=1, output=ISSUE_499_CLAUDE_RATE_LIMIT_LOG
        )
        reg.update_headless_result(
            reviewer["job_id"],
            status="failed",
            exit_code=1,
            provider_outcome=classification.to_dict(),
        )
        reg.create_slice(
            slice_id="slice-review-429",
            spec_path=str(root / "spec.md"),
            spec_hash="new-spec",
            plan_path=str(root / "plan.md"),
            plan_hash="new-plan",
            target_branch="main",
            target_remote="origin",
            verification_hash="new-verification",
            verification={"docs_class": "code", "review_policy": "required"},
            dispatch_base="a" * 40,
            builder_job_id=builder["job_id"],
            reviewer_job_id=reviewer["job_id"],
            candidate="b" * 40,
        )
        reg.update_slice("slice-review-429", state="building", candidate="b" * 40)
        reg.update_slice("slice-review-429", state="reviewing", candidate="b" * 40)
        hdir = root / "handoff"

        manager.complete_tick(
            _NoopDispatcher(reg),
            handoff_dir=str(hdir),
            clock=lambda: "T0",
            git_runner=lambda args: SimpleNamespace(returncode=0, stdout="b" * 40, stderr=""),
        )

        manifest = json.loads((hdir / "slice-review-429.json").read_text(encoding="utf-8"))
        assert manifest["gate_reason"] == "foreign-review-provider-rate_limited"
        assert manifest["gate_reason"] != "foreign-review-absent"
        assert manifest["provider_outcome"] is not None
        assert manifest["provider_outcome"]["outcome"] == "rate_limited"
        assert manifest["provider_outcome"]["reset_at"] == 1786554000
        # 後續處置維持現狀：仍是 needs_human，只修分錯類本身。
        assert manifest["gate_status"] == "needs_human"


def test_issue_499_unclassified_review_failure_keeps_the_legacy_reason():
    """未分類（legacy／繞過 dispatcher）的 reviewer 失敗不得被偽造分類。"""

    assert manager._review_failure_gate_reason({"job_id": "r-1"}) == "foreign-review-absent"
    assert (
        manager._review_failure_gate_reason(
            {
                "job_id": "r-1",
                "provider_outcome": {
                    "outcome": "unknown",
                    "authority": "hint",
                    "reason": "no definitive signal (exit 1)",
                    "retryable": False,
                },
            }
        )
        == "foreign-review-absent"
    )


# --------------------------------------------------------------- #500 tool parser timeout


def test_issue_500_tool_parser_timeout_is_not_a_network_transient():
    """被 controller 中斷的 job 不得因 nested tool result 的 `timeout` 字樣成為
    可重試的 network transient。"""

    result = classify_provider_failure(exit_code=1, output=ISSUE_500_TOOL_PARSER_TIMEOUT_LOG)

    assert result.outcome is not ProviderOutcome.TRANSIENT
    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.authority is SignalAuthority.STRUCTURED
    assert result.retryable is False


def test_issue_500_nested_tool_result_alone_never_reaches_the_keyword_table():
    """就算終局記錄看不出中斷，nested tool result 也不該驅動 transient 判定。"""

    log = "\n".join(
        [
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "content": "Parser aborted (timeout, resource limit, or over-length)",
                            }
                        ]
                    },
                }
            ),
            json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "stopped"}),
        ]
    )

    result = classify_provider_failure(exit_code=1, output=log)

    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.authority is SignalAuthority.HINT


def test_real_provider_transient_still_classifies_as_transient():
    """反向護欄：provider 層真的回 503 時，transient 判定必須維持不變。"""

    log = json.dumps(
        {"type": "result", "subtype": "error", "is_error": True, "result": "503 Service Unavailable"}
    )

    result = classify_provider_failure(exit_code=1, output=log)

    assert result.outcome is ProviderOutcome.TRANSIENT
    assert result.retryable is True


def test_truncated_leading_line_is_discarded_when_the_tail_parses_as_jsonl():
    """64 KiB tail 的開頭殘行是截斷產物，不是 CLI 輸出——不得當證據。"""

    log = "\n".join(
        [
            'sult":"Parser aborted (timeout, resource limit, or over-length)"}]}}',
            json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "stopped"}),
        ]
    )

    result = classify_provider_failure(exit_code=1, output=log)

    assert result.outcome is ProviderOutcome.UNKNOWN


# --------------------------------------------------------------- #487 doc-coauthoring


def test_issue_487_doc_coauthoring_is_not_an_auth_failure():
    result = classify_provider_failure(exit_code=1, output=ISSUE_487_DOC_COAUTHORING_LOG)

    assert result.outcome is not ProviderOutcome.AUTH
    assert result.outcome is ProviderOutcome.UNKNOWN


def test_issue_487_oauth_signal_is_bounded_to_a_standalone_token():
    # 根因：無界的 `oauth` 命中 `coauthoring` 裡的子字串。
    assert is_auth_signal("...copilot-sdk,doc-coauthoring,docx...") is False
    # 真正的 OAuth 失敗仍必須命中（issue 明列的正向保留）。
    assert is_auth_signal("OAuth token invalid") is True
    assert is_auth_signal("error: oauth-2.0 authorization failed") is True


def test_issue_487_init_metadata_is_not_classification_evidence():
    evidence = outcome_taxonomy.parse_stream_evidence(ISSUE_487_DOC_COAUTHORING_LOG)

    assert "doc-coauthoring" not in evidence.provider_text
    assert "doc-coauthoring" not in evidence.model_text


def test_genuine_auth_failure_still_classifies_as_auth():
    """反向護欄：真的 auth 失敗仍須落 auth（issue 要求保留正向測試）。"""

    log = json.dumps(
        {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": "Bad credentials -- run `claude auth login`",
        }
    )

    result = classify_provider_failure(exit_code=1, output=log)

    assert result.outcome is ProviderOutcome.AUTH


# --------------------------------------------------------------- #485 Codex stdin banner


def test_issue_485_codex_banner_no_longer_fails_the_jsonl_purity_check():
    with tempfile.TemporaryDirectory() as d:
        log_path = Path(d) / "review.jsonl"
        log_path.write_text(ISSUE_485_CODEX_BANNER_LOG + "\n", encoding="utf-8")

        assert manager._review_log_has_only_json_lines(str(log_path)) is True


def test_issue_485_unexpected_non_json_text_still_fails_closed():
    with tempfile.TemporaryDirectory() as d:
        log_path = Path(d) / "review.jsonl"
        log_path.write_text(
            "Reading additional input from stdin...\n"
            "Segmentation fault (core dumped)\n"
            + json.dumps({"type": "turn.completed"})
            + "\n",
            encoding="utf-8",
        )

        assert manager._review_log_has_only_json_lines(str(log_path)) is False


def test_issue_485_banner_in_the_middle_of_the_stream_is_not_stripped():
    """banner 的語意是「串流開始前印的那一行」；出現在中段的同一句話不是 banner。"""

    with tempfile.TemporaryDirectory() as d:
        log_path = Path(d) / "review.jsonl"
        log_path.write_text(
            json.dumps({"type": "thread.started"})
            + "\nReading additional input from stdin...\n"
            + json.dumps({"type": "turn.completed"})
            + "\n",
            encoding="utf-8",
        )

        assert manager._review_log_has_only_json_lines(str(log_path)) is False


def test_issue_485_codex_review_reaches_verdict_validation():
    """end-to-end：banner ＋ 合法 JSONL 的 Codex review 必須走到 verdict 驗證階段
    （此處 verdict 檔缺席，故落 `verdict-missing` 而非 `invalid-process-output`）。"""

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        reg = JobRegistry(state_path=root / "jobs.json")
        builder = reg.create_job(
            task="slice-codex-banner",
            persona="builder",
            branch="feature/slice-codex-banner",
            pane="",
            worktree=str(root / "candidate"),
            executor="copilot",
            session_name="slice-codex-banner",
            pid=1,
            log_path="/builder-log",
        )
        reg.attach_launch_handle(
            builder["job_id"],
            executor="copilot",
            model_id="claude-haiku-4.5",
            session_name="slice-codex-banner",
            pid=1,
            log_path="/builder-log",
        )
        reviewer_log = root / "review.jsonl"
        reviewer_log.write_text(ISSUE_485_CODEX_BANNER_LOG + "\n", encoding="utf-8")
        reviewer = reg.create_job(
            task="slice-codex-banner",
            persona="reviewer",
            kind="review",
            branch="feature/slice-codex-banner",
            pane="",
            worktree=str(root / "review"),
            executor="codex",
            model_id="gpt-5.4",
            independence_domain="openai",
            session_name="slice-codex-banner-2",
            pid=2,
            log_path=str(reviewer_log),
            subject_head="b" * 40,
            spec_hash="new-spec",
            plan_hash="new-plan",
            verification_hash="new-verification",
        )
        reg.update_status(reviewer["job_id"], "exited")
        reg.create_slice(
            slice_id="slice-codex-banner",
            spec_path=str(root / "spec.md"),
            spec_hash="new-spec",
            plan_path=str(root / "plan.md"),
            plan_hash="new-plan",
            target_branch="main",
            target_remote="origin",
            verification_hash="new-verification",
            verification={"docs_class": "code", "review_policy": "required"},
            dispatch_base="a" * 40,
            builder_job_id=builder["job_id"],
            reviewer_job_id=reviewer["job_id"],
            candidate="b" * 40,
        )
        reg.update_slice("slice-codex-banner", state="building", candidate="b" * 40)
        reg.update_slice("slice-codex-banner", state="reviewing", candidate="b" * 40)
        hdir = root / "handoff"

        manager.complete_tick(
            _NoopDispatcher(reg),
            handoff_dir=str(hdir),
            clock=lambda: "T0",
            git_runner=lambda args: SimpleNamespace(returncode=0, stdout="b" * 40, stderr=""),
        )

        manifest = json.loads((hdir / "slice-codex-banner.json").read_text(encoding="utf-8"))
        assert manifest["gate_verdict"]["reason"] == "verdict-missing"
        assert manifest["gate_verdict"]["reason"] != "invalid-process-output"


def test_strip_known_process_banners_only_removes_exact_leading_matches():
    assert outcome_taxonomy.strip_known_process_banners(
        ["Reading additional input from stdin...", '{"type":"turn.completed"}']
    ) == ['{"type":"turn.completed"}']
    assert outcome_taxonomy.strip_known_process_banners(
        ["Reading additional input from stdin... plus extra", '{"type":"turn.completed"}']
    ) == ["Reading additional input from stdin... plus extra", '{"type":"turn.completed"}']
    assert outcome_taxonomy.strip_known_process_banners([]) == []


# --------------------------------------------------------------- 收編：三 lane 共用同一份表


def test_planning_lane_consumes_the_shared_marker_table():
    """#533 的 planning 先行實作已收編：planning lane 與本模組同一張表。"""

    assert manager._PLANNING_TRANSIENT_SERVICE_MARKERS is outcome_taxonomy.TRANSIENT_SERVICE_MARKERS
    # #533 的判準行為逐字不變。
    assert (
        manager._is_planning_transient_service_failure(
            "primary-integration-malformed: ValueError: planning launcher returned no JSON "
            "object: Error: Eligibility check failed: UNAVAILABLE (code 503)"
        )
        is True
    )
    assert (
        manager._is_planning_transient_service_failure(
            "planning launcher returned no JSON object: 我先說明一下我的規劃方向"
        )
        is False
    )


def test_outcome_families_cover_the_four_declared_classes():
    families = {family.value for family in outcome_taxonomy.OutcomeFamily}

    assert {"transient-service", "content", "environment", "auth"} <= families


def test_every_text_and_structured_signal_maps_to_a_family():
    for signal in outcome_taxonomy.TextSignal:
        assert signal in outcome_taxonomy.FAMILY_BY_TEXT_SIGNAL
    for kind in outcome_taxonomy.StructuredKind:
        assert kind in outcome_taxonomy.FAMILY_BY_STRUCTURED_KIND


class _NoopDispatcher:
    """已是終局狀態的 job 不需要 poll；complete_tick 只會讀 registry。"""

    def __init__(self, registry: JobRegistry) -> None:
        self._registry = registry

    def poll_headless_done(self, job_id: str) -> dict:
        return self._registry.get_job(job_id)
