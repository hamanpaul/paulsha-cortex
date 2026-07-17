from __future__ import annotations

from dataclasses import replace

import pytest

from paulsha_cortex.coordinator.delivery import (
    ArchiveGateFacts,
    PullRequestMetadata,
    ReviewLoop,
    build_openspec_archive_argv,
    validate_archive_gate,
    validate_pr_metadata,
)


HEAD1 = "a" * 40
HEAD2 = "b" * 40
HEAD3 = "c" * 40


def test_archive_gate_requires_tasks_specs_docs_and_changelog() -> None:
    facts = ArchiveGateFacts(
        tasks_complete=True,
        canonical_specs_valid=True,
        doc_references_valid=True,
        changelog_present=True,
    )
    assert validate_archive_gate(facts).allowed
    for field in (
        "tasks_complete",
        "canonical_specs_valid",
        "doc_references_valid",
        "changelog_present",
    ):
        result = validate_archive_gate(replace(facts, **{field: False}))
        assert not result.allowed


def test_official_openspec_archive_argv_is_non_interactive_and_typed() -> None:
    assert build_openspec_archive_argv("unified-work-lifecycle") == [
        "openspec",
        "archive",
        "-y",
        "unified-work-lifecycle",
    ]
    with pytest.raises(ValueError, match="safe slug"):
        build_openspec_archive_argv("../escape")


def test_pr_metadata_requires_zh_tw_conventional_title_and_closing_keywords() -> None:
    metadata = PullRequestMetadata(
        title="feat(workflow): 建立統一工作生命週期",
        body="## 摘要\n\n完成工作流程。\n\nCloses #14\nFixes #5\n",
        labels=("enhancement",),
    )
    assert validate_pr_metadata(metadata=metadata, required_issues=(14, 5)).allowed
    assert not validate_pr_metadata(
        metadata=replace(metadata, title="feat(workflow): unified lifecycle"),
        required_issues=(14, 5),
    ).allowed
    assert not validate_pr_metadata(
        metadata=replace(metadata, body="Relates to #14\nFixes #5"),
        required_issues=(14, 5),
    ).allowed


def test_review_loop_invalidates_every_push_and_allows_two_fix_rounds() -> None:
    loop = ReviewLoop.start(head=HEAD1, now_epoch=100)
    assert loop.needs_request
    loop = loop.mark_requested(head=HEAD1, now_epoch=100)
    first = loop.record_review(head=HEAD1, now_epoch=110, finding_count=1)
    assert first.action == "fix_required"
    loop = first.loop.advance_after_fix(head=HEAD2, now_epoch=120)
    assert loop.needs_request
    second = loop.mark_requested(head=HEAD2, now_epoch=120).record_review(
        head=HEAD2,
        now_epoch=130,
        finding_count=1,
    )
    assert second.action == "fix_required"
    loop = second.loop.advance_after_fix(head=HEAD3, now_epoch=140)
    third = loop.mark_requested(head=HEAD3, now_epoch=140).record_review(
        head=HEAD3,
        now_epoch=150,
        finding_count=1,
    )
    assert third.action == "needs_human"
    assert third.reason == "copilot-finding-budget-exhausted"


def test_review_loop_accepts_clean_current_head_and_rejects_old_or_error_review() -> None:
    loop = ReviewLoop.start(head=HEAD1, now_epoch=100).mark_requested(
        head=HEAD1,
        now_epoch=100,
    )
    assert loop.record_review(
        head=HEAD1,
        now_epoch=110,
        finding_count=0,
    ).action == "passed"
    assert loop.record_review(
        head=HEAD2,
        now_epoch=110,
        finding_count=0,
    ).action == "needs_human"
    assert loop.record_review(
        head=HEAD1,
        now_epoch=110,
        finding_count=0,
        error=True,
    ).action == "needs_human"


def test_review_loop_times_out_at_fifteen_minutes() -> None:
    loop = ReviewLoop.start(head=HEAD1, now_epoch=100).mark_requested(
        head=HEAD1,
        now_epoch=100,
    )
    result = loop.record_review(
        head=HEAD1,
        now_epoch=1001,
        finding_count=0,
    )
    assert result.action == "needs_human"
    assert result.reason == "copilot-review-timeout"


def test_review_loop_requires_request_before_review() -> None:
    with pytest.raises(ValueError, match="not requested"):
        ReviewLoop.start(head=HEAD1, now_epoch=100).record_review(
            head=HEAD1,
            now_epoch=101,
            finding_count=0,
        )


def test_repeated_request_does_not_extend_current_head_deadline() -> None:
    loop = ReviewLoop.start(head=HEAD1, now_epoch=100).mark_requested(
        head=HEAD1,
        now_epoch=100,
    )
    assert loop.mark_requested(head=HEAD1, now_epoch=800).requested_at == 100
