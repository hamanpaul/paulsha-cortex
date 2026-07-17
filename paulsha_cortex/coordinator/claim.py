"""Pure claim policy used by the Manager single-writer workflow."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import quote

from paulsha_cortex.config import paths

from . import verification

AUTO_LABEL = "cortex:auto-on-going"
WORK_SNAPSHOT_SCHEMA = "work-items-snapshot/v1"
GITHUB_PROVIDER_ID = "github"
PROVIDER_MAX_AGE_SECONDS = 900
DERIVED_AUTHORITY_KINDS = frozenset({"workflow_run", "completion_record"})


def semantic_source_revision(
    *, repo: str, kind: str, ref: str, source_id: str, revision: str
) -> tuple[str, str] | None:
    """Return the stable security authority represented by one Monitor source.

    Workflow/completion rows are projections of Manager state and must never
    feed back into a new claim. GitHub timestamps and active/archive OpenSpec
    provider locators are provenance; their closure facts are checked by
    dedicated gates, so identity—not updated_at—is the stable authority here.
    Source membership and locator identity are the claim authority. Provider
    timestamps and content hashes remain provenance: changing either must not
    make a Manager-authored archive/PR refresh look like a second claim. A
    changed target is still security relevant because it changes the stable
    source key/ref set and therefore the authority digest.
    """

    if kind in DERIVED_AUTHORITY_KINDS:
        return None
    if kind in {
        "github_issue",
        "github_pr",
        "todo",
        "superpowers_spec",
        "superpowers_plan",
    }:
        return source_id, f"identity:{ref}"
    if kind == "openspec":
        return f"openspec:{repo}:{ref}", f"identity:{ref}"
    return source_id, revision


@dataclass(frozen=True, init=False)
class WorkAuthority:
    repo: str
    work_id: str
    mapped_issues: tuple[int, ...]
    mapped_prs: tuple[int, ...]
    mapped_openspec: tuple[str, ...]
    mapped_todo_paths: tuple[str, ...]
    confirmed_todo: bool
    auto_label: bool
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
        mapped_prs: tuple[int, ...] = (),
        mapped_openspec: tuple[str, ...] = (),
        mapped_todo_paths: tuple[str, ...] = (),
        confirmed_todo: bool,
        auto_label: bool,
        source_revisions: tuple[str, ...],
        provider_revision: str,
        provider_id: str = GITHUB_PROVIDER_ID,
        last_success_epoch: float,
        snapshot_hash: str,
    ) -> "WorkAuthority":
        authority = object.__new__(cls)
        object.__setattr__(authority, "repo", repo)
        object.__setattr__(authority, "work_id", work_id)
        object.__setattr__(authority, "mapped_issues", mapped_issues)
        object.__setattr__(authority, "mapped_prs", mapped_prs)
        object.__setattr__(authority, "mapped_openspec", mapped_openspec)
        object.__setattr__(authority, "mapped_todo_paths", mapped_todo_paths)
        object.__setattr__(authority, "confirmed_todo", confirmed_todo)
        object.__setattr__(authority, "auto_label", auto_label)
        object.__setattr__(authority, "source_revisions", source_revisions)
        object.__setattr__(authority, "github_provider_id", provider_id)
        object.__setattr__(authority, "github_provider_revision", provider_revision)
        object.__setattr__(authority, "github_last_success_epoch", last_success_epoch)
        object.__setattr__(authority, "snapshot_hash", snapshot_hash)
        return authority


def canonical_work_snapshot_path() -> Path:
    root = os.environ.get("PSC_MONITOR_STATE_ROOT", "").strip()
    state_root = Path(root).expanduser() if root else paths.agents_root() / "monitor"
    return state_root / "work-items.snapshot.json"


def _load_snapshot(snapshot_path: str | Path | None = None) -> tuple[dict, str]:
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
    if github is None:
        # PR A canonical schema keys GitHub providers by repo.
        return payload, verification.canonical_json_hash(payload)
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
    return payload, verification.canonical_json_hash(payload)


def _authority_from_row(
    *, row: object, providers: dict, snapshot_hash: str
) -> WorkAuthority:
    if not isinstance(row, dict):
        raise ValueError("confirmed work authority row malformed")
    repo = row.get("repo")
    work_id = row.get("work_id")
    if "mapped_issues" not in row:
        return _authority_from_canonical_row(
            row=row,
            providers=providers,
            snapshot_hash=snapshot_hash,
        )
    github = providers.get(GITHUB_PROVIDER_ID)
    if not isinstance(github, dict):
        raise ValueError("durable GitHub provider authority missing")
    issues = row.get("mapped_issues")
    prs = row.get("mapped_prs", [])
    changes = row.get("mapped_openspec", [])
    todo_paths = row.get("mapped_todo_paths", [])
    confirmed_todo = row.get("confirmed_todo")
    auto_label = row.get("auto_label", False)
    source_revisions = row.get("source_revisions")
    if (
        not isinstance(issues, list)
        or any(not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0 for issue in issues)
        or len(set(issues)) != len(issues)
        or not isinstance(prs, list)
        or any(not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0 for pr in prs)
        or len(set(prs)) != len(prs)
        or not isinstance(changes, list)
        or any(
            not isinstance(change, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", change) is None
            for change in changes
        )
        or len(set(changes)) != len(changes)
        or not isinstance(todo_paths, list)
        or any(not _safe_todo_path(path) for path in todo_paths)
        or len(set(todo_paths)) != len(todo_paths)
    ):
        raise ValueError("confirmed work authority mapped issues invalid")
    if (
        not isinstance(repo, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None
        or not isinstance(work_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None
    ):
        raise ValueError("confirmed work authority identity invalid")
    if not isinstance(confirmed_todo, bool):
        raise ValueError("confirmed work authority Todo flag invalid")
    if not isinstance(auto_label, bool):
        raise ValueError("confirmed work authority auto label invalid")
    if (
        not isinstance(source_revisions, list)
        or not source_revisions
        or any(not isinstance(value, str) or not value.strip() for value in source_revisions)
    ):
        raise ValueError("confirmed work authority revisions invalid")
    return WorkAuthority._verified(
        repo=repo,
        work_id=work_id,
        mapped_issues=tuple(sorted(issues)),
        mapped_prs=tuple(sorted(prs)),
        mapped_openspec=tuple(sorted(changes)),
        mapped_todo_paths=tuple(sorted(todo_paths)),
        confirmed_todo=confirmed_todo,
        auto_label=auto_label,
        source_revisions=tuple(sorted(source_revisions)),
        provider_revision=github["revision"].strip(),
        last_success_epoch=float(github["last_success_epoch"]),
        snapshot_hash=snapshot_hash,
    )


def _authority_from_canonical_row(
    *, row: dict, providers: dict, snapshot_hash: str
) -> WorkAuthority:
    repo = row.get("repo")
    work_id = row.get("work_id")
    sources = row.get("sources")
    if not isinstance(repo, str) or not isinstance(work_id, str) or not isinstance(sources, list):
        raise ValueError("canonical work authority row malformed")
    provider_id = f"github:{repo}"
    github = providers.get(provider_id)
    if not isinstance(github, dict):
        raise ValueError("durable GitHub provider authority missing")
    revision = github.get("revision")
    last_success_at = github.get("last_success_at")
    if (
        github.get("status") != "ok"
        or not isinstance(revision, str)
        or not revision
        or not isinstance(last_success_at, str)
    ):
        raise ValueError("durable GitHub provider authority invalid")
    try:
        last_success = datetime.fromisoformat(last_success_at.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise ValueError("durable GitHub provider timestamp invalid") from exc
    confirmed = [
        source
        for source in sources
        if isinstance(source, dict) and source.get("confidence") == "confirmed"
    ]
    issues: list[int] = []
    prs: list[int] = []
    changes: list[str] = []
    todo_paths: list[str] = []
    for source in confirmed:
        kind = source.get("kind")
        ref = source.get("ref")
        if kind in {"github_issue", "github_pr"}:
            match = re.fullmatch(rf"{re.escape(repo)}#([1-9][0-9]*)", str(ref or ""))
            if match is None:
                raise ValueError("canonical GitHub work source ref invalid")
            target = issues if kind == "github_issue" else prs
            target.append(int(match.group(1)))
        elif kind == "openspec":
            if not isinstance(ref, str) or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", ref) is None:
                raise ValueError("canonical OpenSpec work source ref invalid")
            changes.append(ref)
        elif kind == "todo":
            if not _safe_todo_path(ref):
                raise ValueError("canonical Todo work source ref invalid")
            todo_paths.append(ref)
    todo_kinds = {"todo", "superpowers_spec", "superpowers_plan", "openspec"}
    confirmed_todo = any(source.get("kind") in todo_kinds for source in confirmed)
    semantic_sources: dict[str, str] = {}
    for source in confirmed:
        source_id = source.get("source_id")
        source_revision = source.get("revision")
        kind = source.get("kind")
        ref = source.get("ref")
        if not all(isinstance(value, str) and value for value in (source_id, source_revision, kind, ref)):
            continue
        semantic = semantic_source_revision(
            repo=repo,
            kind=kind,
            ref=ref,
            source_id=source_id,
            revision=source_revision,
        )
        if semantic is None:
            continue
        key, value = semantic
        previous = semantic_sources.setdefault(key, value)
        if previous != value:
            raise ValueError("confirmed semantic work authority revisions conflict")
    source_revisions = tuple(
        f"{source_id}@{semantic_sources[source_id]}" for source_id in sorted(semantic_sources)
    )
    if not source_revisions:
        raise ValueError("confirmed work authority revisions invalid")
    return WorkAuthority._verified(
        repo=repo,
        work_id=work_id,
        mapped_issues=tuple(sorted(set(issues))),
        mapped_prs=tuple(sorted(set(prs))),
        mapped_openspec=tuple(sorted(set(changes))),
        mapped_todo_paths=tuple(sorted(set(todo_paths))),
        confirmed_todo=confirmed_todo,
        auto_label=False,
        source_revisions=source_revisions,
        provider_revision=revision,
        provider_id=provider_id,
        last_success_epoch=last_success,
        snapshot_hash=snapshot_hash,
    )


def _safe_todo_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    pure = PurePosixPath(value)
    return (
        not pure.is_absolute()
        and ".." not in pure.parts
        and pure.as_posix() == value
        and pure.suffix.lower() == ".md"
    )


def work_authority_digest(authority: WorkAuthority) -> str:
    if not isinstance(authority, WorkAuthority):
        raise ValueError("confirmed WorkAuthority is required")
    payload = {
        "repo": authority.repo,
        "work_id": authority.work_id,
        "provider_id": authority.github_provider_id,
        "source_revisions": list(authority.source_revisions),
        "mapped_issues": list(authority.mapped_issues),
        "mapped_prs": list(authority.mapped_prs),
        "mapped_openspec": list(authority.mapped_openspec),
        "mapped_todo_paths": list(authority.mapped_todo_paths),
        "confirmed_todo": authority.confirmed_todo,
    }
    return verification.canonical_json_hash(payload)


def load_work_authorities(
    *, snapshot_path: str | Path | None = None
) -> tuple[WorkAuthority, ...]:
    payload, digest = _load_snapshot(snapshot_path)
    providers = payload["providers"]
    authorities = tuple(
        _authority_from_row(row=row, providers=providers, snapshot_hash=digest)
        for row in payload["work_items"]
    )
    identities = [(authority.repo, authority.work_id) for authority in authorities]
    if len(set(identities)) != len(identities):
        raise ValueError("confirmed work authority missing or ambiguous")
    return authorities


def load_work_authority(
    *,
    repo: str,
    work_id: str,
    snapshot_path: str | Path | None = None,
) -> WorkAuthority:
    matches = [
        authority
        for authority in load_work_authorities(snapshot_path=snapshot_path)
        if authority.repo == repo and authority.work_id == work_id
    ]
    if len(matches) != 1:
        raise ValueError("confirmed work authority missing or ambiguous")
    return matches[0]


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
    active_status: str | None = None
    active_snapshot_hash: str | None = None
    active_source_revisions: tuple[str, ...] | None = None
    active_provider_revision: str | None = None
    active_authority_digest: str | None = None


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
        if candidate.active_status not in {"ongoing", "needs_human", "blocked", "done"}:
            raise ValueError("active workflow status invalid")
        if (
            not isinstance(candidate.active_snapshot_hash, str)
            or len(candidate.active_snapshot_hash) != 64
            or candidate.active_source_revisions is None
            or not candidate.active_source_revisions
            or not isinstance(candidate.active_provider_revision, str)
            or not candidate.active_provider_revision
            or not isinstance(candidate.active_authority_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", candidate.active_authority_digest) is None
        ):
            raise ValueError("active workflow authority metadata missing")


def build_claim_key(candidate: ClaimCandidate) -> str:
    _validate_candidate(candidate)
    if not candidate.source_revisions:
        raise ValueError("new claim requires authoritative source revisions")
    payload = {
        "repo": candidate.repo,
        "work_id": candidate.work_id,
        "authority_digest": work_authority_digest(candidate.authority),
    }
    digest = verification.canonical_json_hash(payload)
    return f"claim:v1:{digest}"


def _existing(candidate: ClaimCandidate) -> ClaimDecision | None:
    if candidate.active_run_id is None:
        return None
    authority_changed = (
        candidate.active_authority_digest != work_authority_digest(candidate.authority)
        or tuple(sorted(candidate.active_source_revisions or ()))
        != candidate.authority.source_revisions
    )
    if authority_changed:
        return None
    expected_key = build_claim_key(
        replace(
            candidate,
            active_run_id=None,
            active_claim_key=None,
            active_status=None,
            active_snapshot_hash=None,
            active_source_revisions=None,
            active_provider_revision=None,
            active_authority_digest=None,
        )
    )
    if candidate.active_claim_key != expected_key:
        raise ValueError("persisted claim key does not match authority")
    if candidate.active_status == "done":
        return ClaimDecision(
            action="done",
            reason="already-completed",
            claim_key=candidate.active_claim_key,
            run_id=candidate.active_run_id,
        )
    if candidate.active_status == "needs_human":
        return ClaimDecision(
            action="needs_human",
            reason="human-intervention-required",
            claim_key=candidate.active_claim_key,
            run_id=candidate.active_run_id,
        )
    if candidate.active_status == "blocked":
        return ClaimDecision(
            action="blocked",
            reason="persisted-block",
            claim_key=candidate.active_claim_key,
            run_id=candidate.active_run_id,
        )
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
