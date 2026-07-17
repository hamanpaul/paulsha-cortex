"""Pure claim policy used by the Manager single-writer workflow."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import quote


AUTO_LABEL = "cortex:auto-on-going"


@dataclass(frozen=True)
class ClaimCandidate:
    repo: str
    work_id: str
    source_revisions: tuple[str, ...]
    confirmed_todo: bool
    confirmed_issue: int | None
    auto_label: bool
    provider_fresh: bool
    active_run_id: str | None
    active_claim_key: str | None


@dataclass(frozen=True)
class ClaimDecision:
    action: str
    reason: str | None = None
    claim_key: str | None = None
    run_id: str | None = None


def _validate_candidate(candidate: ClaimCandidate) -> None:
    repo_valid = re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", candidate.repo)
    work_id_valid = re.fullmatch(r"[a-z0-9][a-z0-9-]*", candidate.work_id)
    if repo_valid is None or work_id_valid is None:
        raise ValueError("claim candidate repo/work_id invalid")
    if any(not isinstance(revision, str) or not revision.strip() for revision in candidate.source_revisions):
        raise ValueError("source revisions must be non-empty strings")
    if candidate.confirmed_issue is not None and (
        not isinstance(candidate.confirmed_issue, int)
        or isinstance(candidate.confirmed_issue, bool)
        or candidate.confirmed_issue <= 0
    ):
        raise ValueError("confirmed_issue must be a positive integer or null")
    if candidate.active_run_id is None and candidate.active_claim_key is not None:
        raise ValueError("active_claim_key requires active_run_id")
    if candidate.active_run_id is not None:
        if not isinstance(candidate.active_run_id, str) or not candidate.active_run_id.strip():
            raise ValueError("active_run_id must be a non-empty string")
        if (
            not isinstance(candidate.active_claim_key, str)
            or not candidate.active_claim_key.startswith("claim:v1:")
            or len(candidate.active_claim_key) != len("claim:v1:") + 64
            or any(ch not in "0123456789abcdef" for ch in candidate.active_claim_key[-64:])
        ):
            raise ValueError("active workflow requires its persisted claim key")


def build_claim_key(candidate: ClaimCandidate) -> str:
    _validate_candidate(candidate)
    if not candidate.source_revisions:
        raise ValueError("new claim requires authoritative source revisions")
    payload = {
        "repo": candidate.repo,
        "work_id": candidate.work_id,
        "source_revisions": sorted(candidate.source_revisions),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"claim:v1:{digest}"


def _existing(candidate: ClaimCandidate) -> ClaimDecision | None:
    if candidate.active_run_id is None:
        return None
    return ClaimDecision(
        action="resume",
        reason="active-workflow",
        claim_key=candidate.active_claim_key,
        run_id=candidate.active_run_id,
    )


def decide_manual_start(candidate: ClaimCandidate) -> ClaimDecision:
    _validate_candidate(candidate)
    existing = _existing(candidate)
    if existing is not None:
        return existing
    if not candidate.provider_fresh:
        return ClaimDecision(action="blocked", reason="provider-degraded-or-stale")
    if not candidate.confirmed_todo:
        return ClaimDecision(action="refuse", reason="confirmed-todo-required")
    if candidate.confirmed_issue is None:
        return ClaimDecision(action="needs_human", reason="missing_issue")
    return ClaimDecision(action="claim", claim_key=build_claim_key(candidate))


def decide_auto_claim(candidate: ClaimCandidate) -> ClaimDecision:
    _validate_candidate(candidate)
    existing = _existing(candidate)
    if existing is not None:
        return existing
    if not candidate.provider_fresh:
        return ClaimDecision(action="blocked", reason="provider-degraded-or-stale")
    if not candidate.confirmed_todo:
        return ClaimDecision(action="ignore", reason="confirmed-todo-required")
    if candidate.confirmed_issue is None:
        return ClaimDecision(action="needs_human", reason="missing_issue")
    if not candidate.auto_label:
        return ClaimDecision(action="ignore", reason="auto-label-missing")
    return ClaimDecision(action="claim", claim_key=build_claim_key(candidate))


def build_label_argv(*, repo: str, issue: int, enabled: bool) -> list[str]:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
        raise ValueError("repo must be owner/name")
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
        raise ValueError("issue must be a positive integer")
    if enabled:
        return [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repo}/issues/{issue}/labels",
            "-f",
            f"labels[]={AUTO_LABEL}",
        ]
    return [
        "gh",
        "api",
        "--method",
        "DELETE",
        f"repos/{repo}/issues/{issue}/labels/{quote(AUTO_LABEL, safe='')}",
    ]
