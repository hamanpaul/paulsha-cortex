"""State and metadata contracts for Manager-owned delivery orchestration."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .github_delivery import GateResult


CONVENTIONAL_ZH_TW_TITLE_RE = re.compile(
    r"^(?:feat|fix|docs|test|chore|refactor|perf|build|ci)(?:\([a-z0-9._/-]+\))?!?:\s+.*[\u3400-\u9fff]"
)
CLOSING_RE_TEMPLATE = r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#{issue}\b"
REVIEW_TIMEOUT_SECONDS = 15 * 60
MAX_FIX_ROUNDS = 2


@dataclass(frozen=True)
class ArchiveGateFacts:
    tasks_complete: bool
    canonical_specs_valid: bool
    doc_references_valid: bool
    changelog_present: bool


def build_openspec_archive_argv(change: str) -> list[str]:
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", change) is None:
        raise ValueError("OpenSpec change must be a safe slug")
    return ["openspec", "archive", "-y", change]


def validate_archive_gate(facts: ArchiveGateFacts) -> GateResult:
    reasons: list[str] = []
    if not facts.tasks_complete:
        reasons.append("openspec-tasks-incomplete")
    if not facts.canonical_specs_valid:
        reasons.append("canonical-specs-invalid")
    if not facts.doc_references_valid:
        reasons.append("doc-reference-invalid")
    if not facts.changelog_present:
        reasons.append("changelog-missing")
    return GateResult(allowed=not reasons, reasons=tuple(reasons))


@dataclass(frozen=True)
class PullRequestMetadata:
    title: str
    body: str
    labels: tuple[str, ...]


def validate_pr_metadata(
    *,
    metadata: PullRequestMetadata,
    required_issues: tuple[int, ...],
) -> GateResult:
    reasons: list[str] = []
    if CONVENTIONAL_ZH_TW_TITLE_RE.match(metadata.title) is None:
        reasons.append("pr-title-not-zh-tw-conventional")
    for issue in required_issues:
        if re.search(CLOSING_RE_TEMPLATE.format(issue=issue), metadata.body) is None:
            reasons.append(f"closing-keyword-missing:{issue}")
    if any(not isinstance(label, str) or not label.strip() for label in metadata.labels):
        reasons.append("pr-label-invalid")
    return GateResult(allowed=not reasons, reasons=tuple(reasons))


@dataclass(frozen=True)
class ReviewLoop:
    head: str
    fix_rounds: int
    epoch_started_at: float
    requested_at: float | None

    @classmethod
    def start(cls, *, head: str, now_epoch: int | float) -> "ReviewLoop":
        _require_sha(head)
        return cls(
            head=head,
            fix_rounds=0,
            epoch_started_at=float(now_epoch),
            requested_at=None,
        )

    @property
    def needs_request(self) -> bool:
        return self.requested_at is None

    def mark_requested(
        self,
        *,
        head: str,
        now_epoch: int | float,
    ) -> "ReviewLoop":
        if head != self.head:
            raise ValueError("cannot request review for a non-current HEAD")
        if self.requested_at is not None:
            return self
        return replace(self, requested_at=float(now_epoch))

    def advance_after_fix(
        self,
        *,
        head: str,
        now_epoch: int | float,
    ) -> "ReviewLoop":
        _require_sha(head)
        if head == self.head:
            raise ValueError("fix must produce a new HEAD")
        if self.fix_rounds >= MAX_FIX_ROUNDS:
            raise ValueError("Copilot fix budget exhausted")
        return ReviewLoop(
            head=head,
            fix_rounds=self.fix_rounds + 1,
            epoch_started_at=float(now_epoch),
            requested_at=None,
        )

    def record_review(
        self,
        *,
        head: str,
        now_epoch: int | float,
        finding_count: int,
        error: bool = False,
    ) -> "ReviewDecision":
        if self.requested_at is None:
            raise ValueError("Copilot review was not requested for current HEAD")
        if head != self.head:
            return ReviewDecision(self, "needs_human", "copilot-old-head-review")
        elapsed = float(now_epoch) - self.requested_at
        if elapsed < 0 or elapsed > REVIEW_TIMEOUT_SECONDS:
            return ReviewDecision(self, "needs_human", "copilot-review-timeout")
        if error:
            return ReviewDecision(self, "needs_human", "copilot-error-review")
        if not isinstance(finding_count, int) or isinstance(finding_count, bool) or finding_count < 0:
            raise ValueError("finding_count must be a non-negative integer")
        if finding_count == 0:
            return ReviewDecision(self, "passed", None)
        if self.fix_rounds >= MAX_FIX_ROUNDS:
            return ReviewDecision(
                self,
                "needs_human",
                "copilot-finding-budget-exhausted",
            )
        return ReviewDecision(self, "fix_required", "copilot-findings")


@dataclass(frozen=True)
class ReviewDecision:
    loop: ReviewLoop
    action: str
    reason: str | None


def _require_sha(value: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ValueError("HEAD must be a 40-character hexadecimal SHA")
