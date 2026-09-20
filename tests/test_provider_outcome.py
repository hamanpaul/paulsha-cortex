"""#384：`coordinator.provider_outcome` 的 typed 分類與中間 authority 等級測試。

Root cause：`completion.classify_completion` 只有 exited/failed 兩值，`manager.py`
的 slice lane／workflow lane 因此把任何 executor 失敗一律壓平成寫死的
``"builder-failed"``／``"job-failed"``，無分類、無 retry、無 backoff。本模組是
分類器本身；`manager.py`／`dispatcher.py`／`registry.py` 的接線測試在其他檔案。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from paulsha_cortex.coordinator.provider_outcome import (
    RETRYABLE_OUTCOMES,
    ProviderFailureClassification,
    ProviderOutcome,
    SignalAuthority,
    classification_from_job,
    classify_launch_failure,
    classify_provider_failure,
    read_log_tail,
)

_COPILOT_EFFORT_UNSUPPORTED = (
    'Reasoning effort "xhigh" is not supported for model '
    '"mai-code-1-flash-picker"'
)


# --------------------------------------------------------------- classify_provider_failure


def test_exit_code_zero_never_reports_a_retryable_outcome():
    result = classify_provider_failure(exit_code=0, output="all good")
    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.authority is SignalAuthority.HINT
    assert result.retryable is False


@pytest.mark.parametrize(
    "output",
    [
        "Error: secondary rate limit exceeded. Please wait and try again.",
        "You have exceeded a rate limit. Try again later.",
        "abuse detection mechanism triggered",
        "HTTP 429 Too Many Requests",
        "Retry-After: 60",
    ],
)
def test_rate_limit_signals_classify_as_rate_limited_and_retryable(output):
    result = classify_provider_failure(exit_code=1, output=output)
    assert result.outcome is ProviderOutcome.RATE_LIMITED
    assert result.authority is SignalAuthority.TEXT_SIGNAL
    assert result.retryable is True


def test_text_rate_limit_parses_reset_hint_without_changing_payload_shape() -> None:
    now = int(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc).timestamp())
    pdt = timezone(timedelta(hours=-7), name="PDT")

    result = classify_provider_failure(
        exit_code=1,
        output="rate limit exceeded -- Try again at Sep 7th 12:23 PM",
        now=now,
        tz=pdt,
    )

    assert result.outcome is ProviderOutcome.RATE_LIMITED
    assert result.reset_at == int(datetime(2026, 9, 7, 12, 23, tzinfo=pdt).timestamp())
    assert result.reset_parser == {
        "rule_version": "v1",
        "timezone": "PDT",
        "base_year": 2026,
        "base_reference_epoch": now,
    }
    assert set(result.to_dict()) == {
        "outcome",
        "authority",
        "reason",
        "retryable",
        "reset_at",
    }


def test_structured_reset_signal_keeps_reset_at_without_parser_metadata() -> None:
    output = "\n".join(
        [
            '{"type":"system","subtype":"rate_limit_event","rate_limit_event":{"status":"rejected","rateLimitType":"five_hour","resetsAt":1786554000}}',
            '{"type":"result","subtype":"error","is_error":true,"api_error_status":429,"terminal_reason":"api_error"}',
        ]
    )

    result = classify_provider_failure(exit_code=1, output=output)

    assert result.outcome is ProviderOutcome.RATE_LIMITED
    assert result.reset_at == 1786554000
    assert result.reset_parser is None


def test_rate_limit_signal_wins_even_when_message_also_mentions_authenticate():
    # #369/#370 的教訓：GitHub 的限流訊息常同時帶 "authenticate"／"login" 字樣；
    # rate limit 判定必須排在 auth 判定之前，否則限流會被誤判成登出。
    output = "secondary rate limit hit -- please re-authenticate to raise your quota"
    result = classify_provider_failure(exit_code=1, output=output)
    assert result.outcome is ProviderOutcome.RATE_LIMITED


@pytest.mark.parametrize(
    "output",
    [
        "Please login to continue",
        "authentication required: run `claude auth login`",
        "device code flow required",
        "bad credentials",
        "401 Unauthorized",
        "OAuth token invalid",
    ],
)
def test_auth_signals_classify_as_auth_and_not_retryable(output):
    result = classify_provider_failure(exit_code=1, output=output)
    assert result.outcome is ProviderOutcome.AUTH
    assert result.authority is SignalAuthority.TEXT_SIGNAL
    assert result.retryable is False


@pytest.mark.parametrize(
    "output",
    [
        "monthly limit reached",
        "insufficient credits",
        "please review your billing details",
    ],
)
def test_quota_signals_classify_as_quota_and_not_retryable(output):
    result = classify_provider_failure(exit_code=1, output=output)
    assert result.outcome is ProviderOutcome.QUOTA
    assert result.retryable is False


def test_quota_exceeded_wording_is_claimed_by_rate_limit_not_quota():
    # #369/#370 的 rate-limit 正則已經把 "quota exceeded"／"usage limit" 算進
    # rate-limit（兩者語意接近：等時間窗過了就會恢復）。本模組刻意不重複收這兩個
    # 措辭到 QUOTA 類別，這裡把這個設計決策釘成回歸測試，避免日後有人「修好」
    # 成兩邊都收、卻不小心讓 rate-limit 判定順序被打亂。
    result = classify_provider_failure(exit_code=1, output="quota exceeded for this billing period")
    assert result.outcome is ProviderOutcome.RATE_LIMITED


@pytest.mark.parametrize(
    "output",
    [
        "connection reset by peer",
        "read ETIMEDOUT",
        "connect timeout",
        "503 Service Unavailable",
        "504 Gateway Timeout",
    ],
)
def test_transient_signals_classify_as_transient_and_retryable(output):
    result = classify_provider_failure(exit_code=1, output=output)
    assert result.outcome is ProviderOutcome.TRANSIENT
    assert result.authority is SignalAuthority.TEXT_SIGNAL
    assert result.retryable is True


@pytest.mark.parametrize(
    "output",
    [
        "I cannot assist with that request due to our content policy.",
        "This request violates the usage policy.",
        "response blocked by safety filter",
    ],
)
def test_content_signals_classify_as_content_and_not_retryable(output):
    result = classify_provider_failure(exit_code=1, output=output)
    assert result.outcome is ProviderOutcome.CONTENT
    assert result.retryable is False


def test_no_signal_classifies_as_unknown_hint_and_not_retryable():
    result = classify_provider_failure(exit_code=1, output="stack trace: NullPointerException at foo.py:12")
    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.authority is SignalAuthority.HINT
    assert result.retryable is False


def test_none_output_does_not_raise():
    result = classify_provider_failure(exit_code=1, output=None)
    assert result.outcome is ProviderOutcome.UNKNOWN


def test_exit_127_without_readable_output_stays_unknown_hint():
    result = classify_provider_failure(exit_code=127, output=None)
    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.authority is SignalAuthority.HINT
    assert result.retryable is False
    assert result.reroutable is False
    assert "not enough to prove missing executable" in result.reason


def test_only_rate_limited_and_transient_are_retryable_outcomes():
    assert RETRYABLE_OUTCOMES == {ProviderOutcome.RATE_LIMITED, ProviderOutcome.TRANSIENT}


# --------------------------------------------------------------- authority level design


def test_effort_not_supported_is_reroutable_but_not_retryable():
    result = classify_provider_failure(exit_code=1, output=_COPILOT_EFFORT_UNSUPPORTED)
    assert result.outcome is ProviderOutcome.EFFORT_NOT_SUPPORTED
    assert result.retryable is False
    assert result.reroutable is True
    assert "reroutable" not in result.to_dict()


def test_exit_127_empty_output_is_executable_not_found_and_reroutable():
    result = classify_provider_failure(exit_code=127, output="")
    assert result.outcome is ProviderOutcome.EXECUTABLE_NOT_FOUND
    assert result.authority is SignalAuthority.TEXT_SIGNAL
    assert result.retryable is False
    assert result.reroutable is True


@pytest.mark.parametrize(
    "shared_binary", ["bash", "sh", "git", "systemctl", "systemd-run"]
)
def test_same_line_launch_enoent_for_shared_infrastructure_is_not_reroutable(
    shared_binary: str,
) -> None:
    result = classify_provider_failure(
        exit_code=1,
        output=f"subprocess.Popen(...): [Errno 2] No such file or directory: '{shared_binary}'",
    )

    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.authority is SignalAuthority.HINT
    assert result.reroutable is False


def test_same_line_launch_enoent_for_unknown_target_is_not_reroutable() -> None:
    result = classify_provider_failure(
        exit_code=1,
        output="subprocess.Popen(...): [Errno 2] No such file or directory: 'pytest'",
    )

    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.authority is SignalAuthority.HINT
    assert result.reroutable is False


def test_launch_failure_only_classifies_missing_executable_when_exception_proves_it():
    missing_program = classify_launch_failure(
        exc=FileNotFoundError(2, "No such file or directory", "copilot"),
        executor="copilot",
        worktree="/tmp/worktree",
    )
    missing_cwd = classify_launch_failure(
        exc=FileNotFoundError(2, "No such file or directory", "/tmp/worktree"),
        executor="copilot",
        worktree="/tmp/worktree",
    )

    assert missing_program.outcome is ProviderOutcome.EXECUTABLE_NOT_FOUND
    assert missing_program.reroutable is True
    assert missing_cwd.outcome is ProviderOutcome.LAUNCH_FAILED
    assert missing_cwd.reroutable is False


@pytest.mark.parametrize("shared_binary", ["bash", "sh", "git", "systemctl", "systemd-run"])
def test_launch_failure_keeps_shared_launch_infrastructure_as_launch_failed(
    shared_binary: str,
) -> None:
    result = classify_launch_failure(
        exc=FileNotFoundError(2, "No such file or directory", shared_binary),
        executor="copilot",
        worktree="/tmp/worktree",
    )

    assert result.outcome is ProviderOutcome.LAUNCH_FAILED
    assert result.reroutable is False


def test_text_signal_authority_is_between_structured_and_hint():
    # 中間 authority 等級的存在性斷言：TEXT_SIGNAL 分類仍可 retryable=True
    # （驅動 bounded/reversible 動作），HINT 分類則永遠 retryable=False——
    # 即使兩者的 outcome 剛好相同，authority 才是決定可否自動 retry 的軸。
    text_signal = ProviderFailureClassification(
        outcome=ProviderOutcome.RATE_LIMITED, authority=SignalAuthority.TEXT_SIGNAL, reason="x"
    )
    hint = ProviderFailureClassification(
        outcome=ProviderOutcome.RATE_LIMITED, authority=SignalAuthority.HINT, reason="x"
    )
    assert text_signal.retryable is True
    assert hint.retryable is False


# --------------------------------------------------------------- to_dict / from_dict roundtrip


def test_to_dict_from_dict_roundtrip():
    original = classify_provider_failure(exit_code=1, output="429 too many requests")
    payload = original.to_dict()
    restored = ProviderFailureClassification.from_dict(payload)
    assert restored == original


def test_from_dict_rejects_malformed_payload():
    assert ProviderFailureClassification.from_dict(None) is None
    assert ProviderFailureClassification.from_dict({}) is None
    assert ProviderFailureClassification.from_dict({"outcome": "not-a-real-outcome", "authority": "hint", "reason": "x", "retryable": False}) is None
    assert ProviderFailureClassification.from_dict("rate_limited") is None


# --------------------------------------------------------------- classification_from_job


def test_classification_from_job_reads_stored_dict():
    classification = classify_provider_failure(exit_code=1, output="rate limit exceeded")
    job = {"job_id": "x-1", "status": "failed", "provider_outcome": classification.to_dict()}
    assert classification_from_job(job) == classification


def test_classification_from_job_returns_none_when_absent():
    assert classification_from_job({"job_id": "x-1", "status": "failed"}) is None
    assert classification_from_job({"job_id": "x-1", "status": "failed", "provider_outcome": None}) is None


# --------------------------------------------------------------- read_log_tail


def test_read_log_tail_returns_none_for_missing_path(tmp_path):
    assert read_log_tail(None) is None
    assert read_log_tail(str(tmp_path / "does-not-exist.jsonl")) is None


def test_read_log_tail_reads_full_small_file(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text("line one\nrate limit exceeded\n", encoding="utf-8")
    text = read_log_tail(str(path))
    assert "rate limit exceeded" in text


def test_read_log_tail_bounds_large_files(tmp_path):
    path = tmp_path / "big.jsonl"
    filler = "x" * 1000
    path.write_text(f"{filler}\nrate limit exceeded at the very end\n", encoding="utf-8")
    text = read_log_tail(str(path), max_bytes=64)
    assert text is not None
    assert "rate limit exceeded at the very end" in text
    assert len(text) <= 64 + 1  # +1 為 utf-8 replace 邊界寬容
