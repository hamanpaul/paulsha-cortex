"""Pure claim policy used by the Manager single-writer workflow."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from paulsha_cortex.config import paths

from . import verification

AUTO_LABEL = "cortex:auto-on-going"
WORK_SNAPSHOT_SCHEMA = "work-items-snapshot/v1"
GITHUB_PROVIDER_ID = "github"
PROVIDER_MAX_AGE_SECONDS = 900


@dataclass(frozen=True, init=False)
class WorkAuthority:
    repo: str
    work_id: str
    mapped_issues: tuple[int, ...]
    confirmed_todo: bool
    source_revisions: tuple[str, ...]
    github_provider_id: str
    github_provider_revision: str
    github_last_success_epoch: float
    snapshot_hash: str

    @classmethod
    def _verified(
        cls,
        *,
        repo: str,
        work_id: str,
        mapped_issues: tuple[int, ...],
        confirmed_todo: bool,
        source_revisions: tuple[str, ...],
        provider_revision: str,
        last_success_epoch: float,
        snapshot_hash: str,
    ) -> "WorkAuthority":
        authority = object.__new__(cls)
        object.__setattr__(authority, "repo", repo)
        object.__setattr__(authority, "work_id", work_id)
        object.__setattr__(authority, "mapped_issues", mapped_issues)
        object.__setattr__(authority, "confirmed_todo", confirmed_todo)
        object.__setattr__(authority, "source_revisions", source_revisions)
        object.__setattr__(authority, "github_provider_id", GITHUB_PROVIDER_ID)
        object.__setattr__(authority, "github_provider_revision", provider_revision)
        object.__setattr__(authority, "github_last_success_epoch", last_success_epoch)
        object.__setattr__(authority, "snapshot_hash", snapshot_hash)
        return authority


def canonical_work_snapshot_path() -> Path:
    root = os.environ.get("PSC_MONITOR_STATE_ROOT", "").strip()
    state_root = Path(root).expanduser() if root else paths.agents_root() / "monitor"
    return state_root / "work-items.snapshot.json"


def load_work_authority(
    *,
    repo: str,
    work_id: str,
    snapshot_path: str | Path | None = None,
) -> WorkAuthority:
    path = Path(snapshot_path) if snapshot_path is not None else canonical_work_snapshot_path()
    if path.is_symlink() or not path.is_file():
        raise ValueError("durable work snapshot unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("durable work snapshot unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema") != WORK_SNAPSHOT_SCHEMA:
        raise ValueError("durable work snapshot schema invalid")
    providers = payload.get("providers")
    items = payload.get("work_items")
    if not isinstance(providers, dict) or not isinstance(items, list):
        raise ValueError("durable work snapshot malformed")
    github = providers.get(GITHUB_PROVIDER_ID)
    if not isinstance(github, dict) or github.get("provider_id") != GITHUB_PROVIDER_ID:
        raise ValueError("durable GitHub provider authority missing")
    revision = github.get("revision")
    last_success = github.get("last_success_epoch")
    degraded = github.get("degraded")
    if (
        not isinstance(revision, str)
        or not revision.strip()
        or not isinstance(last_success, (int, float))
        or isinstance(last_success, bool)
        or not math.isfinite(float(last_success))
        or not isinstance(degraded, bool)
        or degraded
    ):
        raise ValueError("durable GitHub provider authority invalid")
    matches = [
        row
        for row in items
        if isinstance(row, dict) and row.get("repo") == repo and row.get("work_id") == work_id
    ]
    if len(matches) != 1:
        raise ValueError("confirmed work authority missing or ambiguous")
    row = matches[0]
    issues = row.get("mapped_issues")
    confirmed_todo = row.get("confirmed_todo")
    source_revisions = row.get("source_revisions")
    if (
        not isinstance(issues, list)
        or not issues
        or any(not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0 for issue in issues)
        or len(set(issues)) != len(issues)
    ):
        raise ValueError("confirmed work authority requires mapped issues")
    if not isinstance(confirmed_todo, bool):
        raise ValueError("confirmed work authority Todo flag invalid")
    if (
        not isinstance(source_revisions, list)
        or not source_revisions
        or any(not isinstance(value, str) or not value.strip() for value in source_revisions)
    ):
        raise ValueError("confirmed work authority revisions invalid")
    digest = verification.canonical_json_hash(payload)
    return WorkAuthority._verified(
        repo=repo,
        work_id=work_id,
        mapped_issues=tuple(sorted(issues)),
        confirmed_todo=confirmed_todo,
        source_revisions=tuple(sorted(source_revisions)),
        provider_revision=revision.strip(),
        last_success_epoch=float(last_success),
        snapshot_hash=digest,
    )


@dataclass(frozen=True)
class ClaimCandidate:
    authority: WorkAuthority
    repo: str
    work_id: str
    source_revisions: tuple[str, ...]
    confirmed_todo: bool
    confirmed_issue: int | None
    auto_label: bool
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
    if not isinstance(candidate.authority, WorkAuthority):
        raise ValueError("confirmed WorkAuthority is required")
    if candidate.repo != candidate.authority.repo or candidate.work_id != candidate.authority.work_id:
        raise ValueError("claim candidate does not match WorkAuthority")
    for field, value in (
        ("confirmed_todo", candidate.confirmed_todo),
        ("auto_label", candidate.auto_label),
    ):
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be boolean")
    if (
        candidate.active_run_id is None
        and candidate.confirmed_todo is not candidate.authority.confirmed_todo
    ):
        raise ValueError("claim Todo flag does not match WorkAuthority")
    if not candidate.source_revisions or any(
        not isinstance(revision, str) or not revision.strip()
        for revision in candidate.source_revisions
    ):
        raise ValueError("source revisions must be non-empty strings")
    if (
        candidate.active_run_id is None
        and tuple(sorted(candidate.source_revisions)) != candidate.authority.source_revisions
    ):
        raise ValueError("claim revisions do not match WorkAuthority")
    if candidate.confirmed_issue is not None and (
        not isinstance(candidate.confirmed_issue, int)
        or isinstance(candidate.confirmed_issue, bool)
        or candidate.confirmed_issue <= 0
    ):
        raise ValueError("confirmed_issue must be a positive integer or null")
    if candidate.confirmed_issue is not None and candidate.confirmed_issue not in candidate.authority.mapped_issues:
        raise ValueError("confirmed_issue is not authorized by WorkAuthority")
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
        "github_provider_revision": candidate.authority.github_provider_revision,
        "work_snapshot_hash": candidate.authority.snapshot_hash,
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


def _authority_is_fresh(authority: WorkAuthority, *, now_epoch: int | float) -> bool:
    if (
        not isinstance(now_epoch, (int, float))
        or isinstance(now_epoch, bool)
        or not math.isfinite(float(now_epoch))
    ):
        raise ValueError("claim clock must be finite")
    age = float(now_epoch) - authority.github_last_success_epoch
    return 0 <= age <= PROVIDER_MAX_AGE_SECONDS


def decide_manual_start(
    candidate: ClaimCandidate,
    *,
    now_epoch: int | float,
) -> ClaimDecision:
    _validate_candidate(candidate)
    existing = _existing(candidate)
    if existing is not None:
        return existing
    if not _authority_is_fresh(candidate.authority, now_epoch=now_epoch):
        return ClaimDecision(action="blocked", reason="provider-degraded-or-stale")
    if not candidate.confirmed_todo:
        return ClaimDecision(action="refuse", reason="confirmed-todo-required")
    if candidate.confirmed_issue is None:
        return ClaimDecision(action="needs_human", reason="missing_issue")
    return ClaimDecision(action="claim", claim_key=build_claim_key(candidate))


def decide_auto_claim(
    candidate: ClaimCandidate,
    *,
    now_epoch: int | float,
) -> ClaimDecision:
    _validate_candidate(candidate)
    existing = _existing(candidate)
    if existing is not None:
        return existing
    if not _authority_is_fresh(candidate.authority, now_epoch=now_epoch):
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
