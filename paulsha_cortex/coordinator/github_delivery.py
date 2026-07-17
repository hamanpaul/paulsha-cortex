"""Fail-closed GitHub delivery gates for a single immutable PR HEAD."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
COPILOT_ERROR_MARKERS = (
    "encountered an error",
    "failed to review",
    "unable to review",
)


@dataclass(frozen=True)
class GitHubCheck:
    name: str
    status: str
    conclusion: str | None

    @property
    def terminal_green(self) -> bool:
        return self.status == "completed" and self.conclusion in GREEN_CONCLUSIONS


@dataclass(frozen=True)
class CopilotReview:
    review_id: int
    commit_id: str
    state: str
    body: str

    @property
    def is_error(self) -> bool:
        body = self.body.casefold()
        return any(marker in body for marker in COPILOT_ERROR_MARKERS)


@dataclass(frozen=True)
class ReviewThread:
    thread_id: str
    resolved: bool
    outdated: bool

    @property
    def blocks_merge(self) -> bool:
        return not self.resolved and not self.outdated


@dataclass(frozen=True)
class DeliveryFacts:
    head: str
    mergeable: bool
    mergeable_state: str
    checks: tuple[GitHubCheck, ...]
    copilot_reviews: tuple[CopilotReview, ...]
    review_threads: tuple[ReviewThread, ...]
    closing_issues: tuple[int, ...]
    active_openspec_absent: bool
    archive_present: bool


@dataclass(frozen=True)
class DeliveryPolicy:
    expected_head: str
    required_closing_issues: tuple[int, ...]


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reasons: tuple[str, ...]


def _unique_reasons(reasons: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))


def evaluate_delivery_gate(*, facts: DeliveryFacts, policy: DeliveryPolicy) -> GateResult:
    """Evaluate only remotely re-read facts for the expected immutable HEAD."""

    reasons: list[str] = []
    if facts.head != policy.expected_head:
        reasons.append("head-race")
    if not facts.mergeable or facts.mergeable_state not in {"clean", "has_hooks"}:
        reasons.append("not-mergeable")
    if not facts.checks or any(not check.terminal_green for check in facts.checks):
        reasons.append("checks-not-terminal-green")

    current_reviews = tuple(
        review for review in facts.copilot_reviews if review.commit_id == policy.expected_head
    )
    if not current_reviews:
        reasons.append("copilot-current-head-review-missing")
    elif any(review.is_error for review in current_reviews):
        reasons.append("copilot-error-review")
    elif any(review.state.upper() not in {"COMMENTED", "APPROVED"} for review in current_reviews):
        reasons.append("copilot-review-state-invalid")

    if any(thread.blocks_merge for thread in facts.review_threads):
        reasons.append("review-thread-open")
    missing_issues = set(policy.required_closing_issues) - set(facts.closing_issues)
    if missing_issues:
        reasons.append("closing-issue-missing")
    if not facts.active_openspec_absent:
        reasons.append("active-openspec-present")
    if not facts.archive_present:
        reasons.append("openspec-archive-missing")
    normalized = _unique_reasons(reasons)
    return GateResult(allowed=not normalized, reasons=normalized)

def build_copilot_request_argv(*, repo: str, pr_number: int) -> list[str]:
    if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
        raise ValueError("repo must be owner/name")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise ValueError("pr_number must be a positive integer")
    return [
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{repo}/pulls/{pr_number}/requested_reviewers",
        "-f",
        "reviewers[]=copilot-pull-request-reviewer[bot]",
    ]


def build_merge_argv(*, pr_number: int) -> list[str]:
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise ValueError("pr_number must be a positive integer")
    return ["gh", "pr", "merge", str(pr_number), "--merge"]


@dataclass(frozen=True)
class RemoteClosureFacts:
    merge_commit: str
    merge_is_ancestor: bool
    issue_states: Mapping[int, str]
    active_openspec_absent: bool
    archive_present: bool
    todo_complete: bool
    completion_record_valid: bool


def evaluate_remote_closure(
    *,
    facts: RemoteClosureFacts,
    required_issues: tuple[int, ...],
) -> GateResult:
    reasons: list[str] = []
    if len(facts.merge_commit) != 40 or not facts.merge_is_ancestor:
        reasons.append("merge-ancestry-unverified")
    if any(facts.issue_states.get(issue) != "closed" for issue in required_issues):
        reasons.append("issue-not-closed")
    if not facts.active_openspec_absent:
        reasons.append("active-openspec-present")
    if not facts.archive_present:
        reasons.append("openspec-archive-missing")
    if not facts.todo_complete:
        reasons.append("todo-incomplete")
    if not facts.completion_record_valid:
        reasons.append("completion-record-invalid")
    normalized = _unique_reasons(reasons)
    return GateResult(allowed=not normalized, reasons=normalized)
