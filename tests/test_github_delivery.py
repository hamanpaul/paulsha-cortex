from __future__ import annotations

from dataclasses import replace

import pytest

from paulsha_cortex.coordinator.github_delivery import (
    COPILOT_REVIEWER_LOGIN,
    CopilotReview,
    DeliveryFacts,
    DeliveryPolicy,
    GitHubCheck,
    ReviewThread,
    RemoteClosureFacts,
    build_copilot_request_argv,
    build_merge_argv,
    evaluate_delivery_gate,
    evaluate_remote_closure,
)


HEAD = "a" * 40


def _facts() -> DeliveryFacts:
    return DeliveryFacts(
        head=HEAD,
        mergeable=True,
        mergeable_state="clean",
        checks=(GitHubCheck(name="pytest", status="completed", conclusion="success"),),
        copilot_reviews=(
            CopilotReview(
                review_id=7,
                commit_id=HEAD,
                state="COMMENTED",
                body="review complete",
                author=COPILOT_REVIEWER_LOGIN,
                submitted_at_epoch=100,
            ),
        ),
        review_threads=(ReviewThread(thread_id="t1", resolved=True, outdated=False),),
        closing_issues=(14,),
        active_openspec_absent=True,
        archive_present=True,
    )


def test_delivery_gate_accepts_exact_current_head_terminal_green() -> None:
    result = evaluate_delivery_gate(
        facts=_facts(),
        policy=_policy(),
    )
    assert result.allowed
    assert result.reasons == ()


@pytest.mark.parametrize(
    ("facts", "reason"),
    (
        (replace(_facts(), head="b" * 40), "head-race"),
        (
            replace(
                _facts(),
                checks=(GitHubCheck(name="pytest", status="in_progress", conclusion=None),),
            ),
            "checks-not-terminal-green",
        ),
        (
            replace(
                _facts(),
                checks=(GitHubCheck(name="pytest", status="completed", conclusion="cancelled"),),
            ),
            "checks-not-terminal-green",
        ),
        (
            replace(
                _facts(),
                copilot_reviews=(
                    CopilotReview(
                        review_id=8,
                        commit_id="b" * 40,
                        state="COMMENTED",
                        body="old head",
                        author=COPILOT_REVIEWER_LOGIN,
                        submitted_at_epoch=100,
                    ),
                ),
            ),
            "copilot-current-head-review-missing",
        ),
        (
            replace(
                _facts(),
                copilot_reviews=(
                    CopilotReview(
                        review_id=7,
                        commit_id=HEAD,
                        state="COMMENTED",
                        body="Copilot encountered an error while reviewing this pull request",
                        author=COPILOT_REVIEWER_LOGIN,
                        submitted_at_epoch=100,
                    ),
                ),
            ),
            "copilot-error-review",
        ),
        (
            replace(
                _facts(),
                review_threads=(ReviewThread(thread_id="t2", resolved=False, outdated=False),),
            ),
            "review-thread-open",
        ),
        (replace(_facts(), mergeable=False), "not-mergeable"),
        (replace(_facts(), closing_issues=()), "closing-issue-missing"),
        (replace(_facts(), active_openspec_absent=False), "active-openspec-present"),
        (replace(_facts(), archive_present=False), "openspec-archive-missing"),
    ),
)
def test_delivery_gate_fails_closed(facts: DeliveryFacts, reason: str) -> None:
    result = evaluate_delivery_gate(
        facts=facts,
        policy=_policy(),
    )
    assert not result.allowed
    assert reason in result.reasons


def test_outdated_unresolved_thread_is_non_blocking() -> None:
    facts = replace(
        _facts(),
        review_threads=(ReviewThread(thread_id="old", resolved=False, outdated=True),),
    )
    assert evaluate_delivery_gate(
        facts=facts,
        policy=_policy(),
    ).allowed


def test_typed_github_commands_request_copilot_and_merge_commit_only() -> None:
    assert build_copilot_request_argv(repo="hamanpaul/paulsha-cortex", pr_number=15) == [
        "gh",
        "api",
        "--method",
        "POST",
        "repos/hamanpaul/paulsha-cortex/pulls/15/requested_reviewers",
        "-f",
        "reviewers[]=copilot-pull-request-reviewer[bot]",
    ]
    merge = build_merge_argv(
        repo="hamanpaul/paulsha-cortex",
        pr_number=15,
        expected_head=HEAD,
    )
    assert merge == [
        "gh",
        "pr",
        "merge",
        "15",
        "--repo",
        "hamanpaul/paulsha-cortex",
        "--merge",
        "--match-head-commit",
        HEAD,
    ]
    assert "--auto" not in merge


def _policy() -> DeliveryPolicy:
    return DeliveryPolicy(
        expected_head=HEAD,
        required_closing_issues=(14,),
        copilot_review_id=7,
        copilot_requested_at_epoch=90,
    )


def test_delivery_gate_requires_exact_bot_review_from_request_epoch() -> None:
    review = _facts().copilot_reviews[0]
    for invalid in (
        replace(review, author="copilot-helper"),
        replace(review, submitted_at_epoch=89),
        replace(review, review_id=99),
    ):
        result = evaluate_delivery_gate(
            facts=replace(_facts(), copilot_reviews=(invalid,)),
            policy=_policy(),
        )
        assert not result.allowed
        assert "copilot-current-head-review-missing" in result.reasons


def test_remote_closure_is_strict_conjunction() -> None:
    facts = RemoteClosureFacts(
        merge_commit="c" * 40,
        default_head="d" * 40,
        merge_is_ancestor=True,
        merge_is_merge_commit=True,
        issue_states={14: "closed"},
        active_openspec_absent=True,
        archive_present=True,
        todo_complete=True,
        todo_revisions={"todo.md": "d" * 40},
        completion_record_valid=True,
    )
    assert evaluate_remote_closure(facts=facts, required_issues=(14,)).allowed
    for field in (
        "merge_is_ancestor",
        "merge_is_merge_commit",
        "active_openspec_absent",
        "archive_present",
        "todo_complete",
        "completion_record_valid",
    ):
        assert not evaluate_remote_closure(
            facts=replace(facts, **{field: False}),
            required_issues=(14,),
        ).allowed
    assert not evaluate_remote_closure(
        facts=replace(facts, issue_states={14: "open"}),
        required_issues=(14,),
    ).allowed
