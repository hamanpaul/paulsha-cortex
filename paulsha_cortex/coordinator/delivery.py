"""State and metadata contracts for Manager-owned delivery orchestration."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from . import completion, review as foreign_review, verification
from .claim import (
    GITHUB_PROVIDER_ID,
    PROVIDER_MAX_AGE_SECONDS,
    WorkAuthority,
    work_authority_digest,
)
from .github_delivery import (
    DeliveryFacts,
    DeliveryPolicy,
    GateResult,
    GitHubDeliveryClient,
    RemoteClosureFacts,
    evaluate_remote_closure,
    _SHIP_CAPABILITY,
)
from .preflight import PreflightResult


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
    if not metadata.labels or any(
        not isinstance(label, str) or not label.strip() for label in metadata.labels
    ) or len(set(metadata.labels)) != len(metadata.labels):
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
        _require_finite_epoch(now_epoch, field="review epoch")
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
        _require_finite_epoch(now_epoch, field="review request epoch")
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
        _require_finite_epoch(now_epoch, field="review fix epoch")
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
        review_id: int,
        submitted_at_epoch: int | float,
        error: bool = False,
    ) -> "ReviewDecision":
        _require_finite_epoch(now_epoch, field="review observation epoch")
        _require_finite_epoch(submitted_at_epoch, field="review submitted epoch")
        if self.requested_at is None:
            raise ValueError("Copilot review was not requested for current HEAD")
        if head != self.head:
            return ReviewDecision(self, "needs_human", "copilot-old-head-review")
        if not isinstance(review_id, int) or isinstance(review_id, bool) or review_id <= 0:
            raise ValueError("review_id must be a positive integer")
        submitted_at = float(submitted_at_epoch)
        if submitted_at < self.requested_at or submitted_at > float(now_epoch):
            return ReviewDecision(self, "needs_human", "copilot-review-outside-request-epoch")
        elapsed = float(now_epoch) - self.requested_at
        if elapsed < 0 or elapsed > REVIEW_TIMEOUT_SECONDS:
            return ReviewDecision(self, "needs_human", "copilot-review-timeout")
        if error:
            return ReviewDecision(self, "needs_human", "copilot-error-review")
        if not isinstance(finding_count, int) or isinstance(finding_count, bool) or finding_count < 0:
            raise ValueError("finding_count must be a non-negative integer")
        if finding_count == 0:
            return ReviewDecision(
                self,
                "passed",
                None,
                head=self.head,
                review_id=review_id,
            )
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
    head: str | None = None
    review_id: int | None = None


@dataclass(frozen=True)
class ForeignReviewEvidence:
    path: str
    expected_hash: str


@dataclass(frozen=True)
class MaintainerReviewEvidence:
    path: str
    expected_hash: str


def _validate_maintainer_review_evidence(
    evidence: MaintainerReviewEvidence,
    *,
    authority: WorkAuthority,
    pr_number: int,
    expected_head: str,
) -> dict[str, object]:
    path = Path(evidence.path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o222:
        raise ValueError("maintainer review evidence must be immutable absolute file")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("maintainer review evidence unreadable") from exc
    if (
        not isinstance(body, dict)
        or body.get("schema") != "cortex-maintainer-review/v1"
        or body.get("repo") != authority.repo
        or body.get("work_id") != authority.work_id
        or body.get("authority_digest") != work_authority_digest(authority)
        or body.get("pr_number") != pr_number
        or body.get("candidate") != expected_head
        or body.get("verdict") != "approved"
        or body.get("findings") != []
        or verification.canonical_json_hash(body) != evidence.expected_hash
    ):
        raise RuntimeError("maintainer review does not authorize exact HEAD")
    return body


@dataclass(frozen=True)
class ShipResult:
    delivery_facts: DeliveryFacts
    expected_head: str
    expected_tree_hash: str


@dataclass(frozen=True)
class ClosureResult:
    facts: RemoteClosureFacts
    completion_record: Mapping[str, object]


def _validate_work_authority(
    authority: WorkAuthority,
    *,
    now_epoch: int | float,
) -> None:
    if (
        not isinstance(authority, WorkAuthority)
        or not authority.github_provider_id.startswith(GITHUB_PROVIDER_ID)
        or not authority.github_provider_revision
        or not authority.mapped_issues
        or not authority.mapped_prs
        or not authority.mapped_openspec
        or not authority.mapped_todo_paths
        or not authority.confirmed_todo
        or not authority.source_revisions
        or not isinstance(now_epoch, (int, float))
        or isinstance(now_epoch, bool)
        or not math.isfinite(float(now_epoch))
        or not math.isfinite(authority.github_last_success_epoch)
    ):
        raise ValueError("confirmed WorkAuthority is incomplete")
    if (
        len(authority.mapped_prs) != 1
        or len(authority.mapped_openspec) != 1
        or len(authority.mapped_todo_paths) != 1
    ):
        raise ValueError("confirmed WorkAuthority must have a single delivery target")
    age = float(now_epoch) - authority.github_last_success_epoch
    if age < 0 or age > PROVIDER_MAX_AGE_SECONDS:
        raise RuntimeError("provider degraded or stale")


def _validate_foreign_review(
    evidence: ForeignReviewEvidence,
    *,
    expected_head: str,
) -> dict[str, object]:
    path = Path(evidence.path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("foreign review evidence path must be absolute and not a symlink")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("foreign review evidence unreadable") from exc
    normalized = foreign_review.validate_gate_evaluation(payload)
    if (
        not isinstance(evidence.expected_hash, str)
        or len(evidence.expected_hash) != 64
        or verification.canonical_json_hash(normalized) != evidence.expected_hash.lower()
    ):
        raise ValueError("foreign review evidence hash mismatch")
    identities = normalized["launch_identity"]
    builder = identities["builder"]
    reviewer = identities["reviewer"]
    if (
        normalized["state"] != "passed"
        or normalized["candidate"] != expected_head.lower()
        or builder is None
        or reviewer is None
        or builder["independence_domain"] == reviewer["independence_domain"]
    ):
        raise RuntimeError("foreign review does not authorize exact HEAD")
    return normalized


class ShipOrchestrator:
    """Single admission point for immutable-HEAD delivery and terminal closure."""

    def __init__(
        self,
        *,
        github: GitHubDeliveryClient,
        now: Callable[[], float],
    ) -> None:
        self._github = github
        self._now = now

    def merge_if_ready(
        self,
        *,
        repo: str,
        pr_number: int,
        change: str,
        expected_head: str,
        expected_tree_hash: str,
        authority: WorkAuthority,
        preflight: PreflightResult,
        copilot: ReviewDecision | None,
        foreign_review: ForeignReviewEvidence,
        maintainer_review: MaintainerReviewEvidence | None = None,
    ) -> ShipResult:
        _require_sha(expected_head)
        _require_sha(expected_tree_hash)
        now_epoch = self._now()
        _validate_work_authority(authority, now_epoch=now_epoch)
        if repo != authority.repo:
            raise RuntimeError("ship repo does not match WorkAuthority")
        if (
            not preflight.passed
            or preflight.head.lower() != expected_head.lower()
            or preflight.tree_hash.lower() != expected_tree_hash.lower()
        ):
            raise RuntimeError("preflight does not authorize exact HEAD/tree")
        if (copilot is None) == (maintainer_review is None):
            raise RuntimeError("exactly one current-HEAD delivery review is required")
        if copilot is not None:
            if (
                copilot.action != "passed"
                or copilot.head != expected_head
                or copilot.review_id is None
                or copilot.loop.head != expected_head
                or copilot.loop.requested_at is None
                or copilot.loop.fix_rounds > MAX_FIX_ROUNDS
                or float(now_epoch) - copilot.loop.requested_at < 0
                or float(now_epoch) - copilot.loop.requested_at > REVIEW_TIMEOUT_SECONDS
            ):
                raise RuntimeError("Copilot review epoch has not passed")
            review_kind = "copilot"
        else:
            _validate_maintainer_review_evidence(
                maintainer_review,
                authority=authority,
                pr_number=pr_number,
                expected_head=expected_head,
            )
            review_kind = "maintainer-review"
        _validate_foreign_review(foreign_review, expected_head=expected_head)
        facts = self._github.merge_if_ready(
            repo=repo,
            pr_number=pr_number,
            change=change,
            policy=DeliveryPolicy(
                expected_head=expected_head,
                required_closing_issues=authority.mapped_issues,
                copilot_review_id=copilot.review_id if copilot is not None else None,
                copilot_requested_at_epoch=(copilot.loop.requested_at if copilot is not None else None),
                review_kind=review_kind,
            ),
            _capability=_SHIP_CAPABILITY,
        )
        return ShipResult(
            delivery_facts=facts,
            expected_head=expected_head,
            expected_tree_hash=expected_tree_hash,
        )

    def verify_remote_closure(
        self,
        *,
        repo: str,
        pr_number: int,
        change: str,
        authority: WorkAuthority,
        todo_paths: tuple[str, ...],
        expected_head: str,
        completion_payload: object,
        run_id: str,
        workflow_step_ids: tuple[str, ...],
        trusted_evidence_refs: tuple[Mapping[str, str], ...],
        coordinator_root: str | Path | None = None,
    ) -> ClosureResult:
        now_epoch = self._now()
        _validate_work_authority(authority, now_epoch=now_epoch)
        if repo != authority.repo:
            raise RuntimeError("closure repo does not match WorkAuthority")
        facts = self._github.fetch_remote_closure(
            repo=repo,
            pr_number=pr_number,
            change=change,
            required_issues=authority.mapped_issues,
            todo_paths=todo_paths,
        )
        pre_record = replace(facts, completion_record_valid=True)
        gate = evaluate_remote_closure(
            facts=pre_record,
            required_issues=authority.mapped_issues,
            expected_head=expected_head,
        )
        if not gate.allowed:
            raise RuntimeError(f"remote closure blocked: {', '.join(gate.reasons)}")
        normalized = completion.validate_completion_record(completion_payload)
        if normalized["candidate"] != expected_head.lower():
            raise RuntimeError("completion record candidate does not match delivered HEAD")
        if normalized["target_ref_sha"] != facts.default_head:
            raise RuntimeError("completion record target ref does not match remote default snapshot")
        expected_work_authority = {
            "repo": authority.repo,
            "work_id": authority.work_id,
            "snapshot_hash": authority.snapshot_hash,
            "provider_id": authority.github_provider_id,
            "provider_revision": authority.github_provider_revision,
            "source_revisions": sorted(authority.source_revisions),
            "mapped_issues": sorted(authority.mapped_issues),
            "mapped_prs": sorted(authority.mapped_prs),
            "mapped_openspec": sorted(authority.mapped_openspec),
            "mapped_todo_paths": sorted(authority.mapped_todo_paths),
            "pr_number": pr_number,
            "change": change,
            "todo_paths": sorted(todo_paths),
            "merge_commit": facts.merge_commit.lower(),
            "run_id": run_id,
            "workflow_step_ids": sorted(workflow_step_ids),
            "trusted_evidence_refs": sorted(
                (dict(item) for item in trusted_evidence_refs),
                key=lambda item: item["kind"],
            ),
        }
        expected_record = dict(normalized)
        expected_record["work_authority"] = expected_work_authority
        if not completion.completion_records_semantically_match(
            normalized,
            expected_record,
        ):
            raise RuntimeError("completion record WorkAuthority does not match remote delivery")
        record = completion.write_completion_record(
            normalized,
            coordinator_root=coordinator_root,
        )
        reread = completion.read_completion_record(
            record["path"],
            expected_hash=record["hash"],
        )
        if reread["candidate"] != expected_head.lower():
            raise RuntimeError("completion record reread does not match delivered HEAD")
        if not completion.completion_records_semantically_match(
            reread,
            expected_record,
        ):
            raise RuntimeError("completion record reread WorkAuthority mismatch")
        final_facts = replace(facts, completion_record_valid=True)
        final_gate = evaluate_remote_closure(
            facts=final_facts,
            required_issues=authority.mapped_issues,
            expected_head=expected_head,
        )
        if not final_gate.allowed:
            raise RuntimeError(f"remote closure blocked: {', '.join(final_gate.reasons)}")
        return ClosureResult(facts=final_facts, completion_record=record)


def _require_sha(value: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ValueError("HEAD must be a 40-character hexadecimal SHA")


def _require_finite_epoch(value: int | float, *, field: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{field} must be finite")
