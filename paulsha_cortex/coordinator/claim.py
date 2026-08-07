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
from paulsha_cortex.deck.schema import BAND_LEVELS

from . import verification

AUTO_LABEL = "cortex:auto-on-going"
WORK_SNAPSHOT_SCHEMA = "work-items-snapshot/v1"
GITHUB_PROVIDER_ID = "github"
PROVIDER_MAX_AGE_SECONDS = 900
DERIVED_AUTHORITY_KINDS = frozenset({"workflow_run", "completion_record"})

# #206：穩定 reason code，供 upstream（durable done record／manager log）在不
# 重新解析訊息文字的情況下辨識是哪一種 authority 驗證失敗。canonical 與 legacy
# schema 各自的 provider 失敗一律區分 -canonical / -legacy 後綴（AC3）。
REASON_ROW_MALFORMED = "row-malformed"
REASON_IDENTITY_INVALID = "identity-invalid"
REASON_PROVIDER_MISSING_CANONICAL = "provider-authority-missing-canonical"
REASON_PROVIDER_INVALID_CANONICAL = "provider-authority-invalid-canonical"
REASON_PROVIDER_MISSING_LEGACY = "provider-authority-missing-legacy"
REASON_PROVIDER_INVALID_LEGACY = "provider-authority-invalid-legacy"

_UNSAFE_LABEL_PREFIXES = ("/", "~")


def _diagnostic_label(value: object, *, max_len: int = 200) -> str | None:
    """Best-effort, secret-free label for a snapshot row's identity field.

    Only ever attached to :class:`AuthorityValidationError` for diagnostics
    — never used for authorization decisions. Rejects values that look like
    filesystem paths so a malformed row can never smuggle an absolute path
    into a durable error message (tier: shareable — AI-SEC-001 契約）.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith(_UNSAFE_LABEL_PREFIXES) or "\\" in text:
        return None
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


class AuthorityValidationError(ValueError):
    """Row-scoped authority validation failure with secret-free diagnostics.

    Carries a stable ``reason_code`` plus ``repo``/``work_id``/
    ``provider_id``/``field`` (#206 AC1/AC3) so upstream durable done
    records and Manager logs can record *which* mutation failed without
    re-parsing message text. Remains a ``ValueError`` subclass so existing
    ``except ValueError`` call sites keep working unchanged.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        repo: str | None = None,
        work_id: str | None = None,
        provider_id: str | None = None,
        field: str | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.repo = repo
        self.work_id = work_id
        self.provider_id = provider_id
        self.field = field
        self.base_message = message
        details = [f"reason={reason_code}"]
        for label, value in (
            ("repo", repo),
            ("work_id", work_id),
            ("provider_id", provider_id),
            ("field", field),
        ):
            if value is not None:
                details.append(f"{label}={value}")
        super().__init__(f"{message} ({', '.join(details)})")


def semantic_source_revision(
    *,
    repo: str,
    kind: str,
    ref: str,
    source_id: str,
    revision: str,
    status: str | None = None,
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
    if kind in {"github_issue", "github_pr"}:
        state = str(status or "").lower()
        allowed = (
            {"open", "closed"}
            if kind == "github_issue"
            else {"open", "closed", "merged"}
        )
        if state not in allowed:
            raise AuthorityValidationError(
                f"canonical {kind} lifecycle status invalid",
                reason_code=REASON_ROW_MALFORMED,
                repo=_diagnostic_label(repo),
                field="status",
            )
        return source_id, f"identity:{ref};state:{state}"
    if kind in {"todo", "superpowers_spec", "superpowers_plan"}:
        return source_id, f"identity:{ref}"
    if kind == "openspec":
        state = str(status or "").lower()
        if state not in {"active", "archived"}:
            raise AuthorityValidationError(
                "canonical openspec lifecycle status invalid",
                reason_code=REASON_ROW_MALFORMED,
                repo=_diagnostic_label(repo),
                field="status",
            )
        return f"openspec:{repo}:{ref}", f"identity:{ref};state:{state}"
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
        raise AuthorityValidationError(
            "durable GitHub provider authority missing",
            reason_code=REASON_PROVIDER_MISSING_LEGACY,
            provider_id=GITHUB_PROVIDER_ID,
            field="provider_id",
        )
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
        if not isinstance(revision, str) or not revision.strip():
            field = "revision"
        elif (
            not isinstance(last_success, (int, float))
            or isinstance(last_success, bool)
            or not math.isfinite(float(last_success))
        ):
            field = "last_success_epoch"
        else:
            field = "degraded"
        raise AuthorityValidationError(
            "durable GitHub provider authority invalid",
            reason_code=REASON_PROVIDER_INVALID_LEGACY,
            provider_id=GITHUB_PROVIDER_ID,
            field=field,
        )
    return payload, verification.canonical_json_hash(payload)


def mapped_issue_titles(
    authority: WorkAuthority,
    *,
    snapshot_path: str | Path | None = None,
) -> dict[int, str | None] | None:
    try:
        payload, canonical_hash = _load_snapshot(snapshot_path)
    except ValueError:
        # 呼叫端（work_bridge.start_canonical_workflow → select_combo）已把
        # None 當成「拿不到權威 issue 標題」的既定 bypass 訊號（見下方 hash
        # mismatch 分支）。_load_snapshot 在 durable snapshot 不存在／不可
        # 讀／schema 損壞，或（legacy schema）provider 區塊本身無效時皆會
        # raise ValueError（AuthorityValidationError 亦是其子類別）——這些
        # 都是「這次就是拿不到權威資料」的同一類情境，理應與 hash mismatch
        # 走同一條 fail-soft 路徑，而不是讓例外一路炸穿到 claim 呼叫端。
        # 其餘呼叫者（load_work_authorities／load_work_authority）需要的是
        # 一個可信的 WorkAuthority 本體，沒有安全的預設值可以退，所以維持
        # 現行的 fail-hard 行為不變，只有這裡改。
        return None
    if canonical_hash != authority.snapshot_hash:
        return None
    for row in payload.get("work_items", []):
        if (
            isinstance(row, dict)
            and row.get("repo") == authority.repo
            and row.get("work_id") == authority.work_id
        ):
            sources = row.get("sources")
            if not isinstance(sources, list):
                return {}
            titles: dict[int, str | None] = {}
            for source in sources:
                if not isinstance(source, dict) or source.get("kind") != "github_issue":
                    continue
                match = re.fullmatch(
                    rf"{re.escape(authority.repo)}#([1-9][0-9]*)",
                    str(source.get("ref") or ""),
                )
                if match is None:
                    continue
                number = int(match.group(1))
                title = source.get("title")
                titles[number] = title if isinstance(title, str) else None
            return titles
    return {}


def _authority_from_row(
    *, row: object, providers: dict, snapshot_hash: str
) -> WorkAuthority | None:
    if not isinstance(row, dict):
        raise AuthorityValidationError(
            "confirmed work authority row malformed",
            reason_code=REASON_ROW_MALFORMED,
        )
    repo = row.get("repo")
    work_id = row.get("work_id")
    repo_label = _diagnostic_label(repo)
    work_id_label = _diagnostic_label(work_id)
    if "mapped_issues" not in row:
        return _authority_from_canonical_row(
            row=row,
            providers=providers,
            snapshot_hash=snapshot_hash,
        )
    github = providers.get(GITHUB_PROVIDER_ID)
    if not isinstance(github, dict):
        raise AuthorityValidationError(
            "durable GitHub provider authority missing",
            reason_code=REASON_PROVIDER_MISSING_LEGACY,
            repo=repo_label,
            work_id=work_id_label,
            provider_id=GITHUB_PROVIDER_ID,
            field="providers.github",
        )
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
        raise AuthorityValidationError(
            "confirmed work authority mapped issues invalid",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="mapped_issues",
        )
    repo_valid = isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is not None
    work_id_valid = isinstance(work_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is not None
    if not repo_valid or not work_id_valid:
        raise AuthorityValidationError(
            "confirmed work authority identity invalid",
            reason_code=REASON_IDENTITY_INVALID,
            repo=repo_label,
            work_id=work_id_label,
            field="repo" if not repo_valid else "work_id",
        )
    if not isinstance(confirmed_todo, bool):
        raise AuthorityValidationError(
            "confirmed work authority Todo flag invalid",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="confirmed_todo",
        )
    if not isinstance(auto_label, bool):
        raise AuthorityValidationError(
            "confirmed work authority auto label invalid",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="auto_label",
        )
    if (
        not isinstance(source_revisions, list)
        or not source_revisions
        or any(not isinstance(value, str) or not value.strip() for value in source_revisions)
    ):
        raise AuthorityValidationError(
            "confirmed work authority revisions invalid",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="source_revisions",
        )
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
) -> WorkAuthority | None:
    repo = row.get("repo")
    work_id = row.get("work_id")
    sources = row.get("sources")
    repo_label = _diagnostic_label(repo)
    work_id_label = _diagnostic_label(work_id)
    if not isinstance(repo, str) or not isinstance(work_id, str) or not isinstance(sources, list):
        raise AuthorityValidationError(
            "canonical work authority row malformed",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
        )
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
        raise AuthorityValidationError(
            "canonical work authority identity invalid",
            reason_code=REASON_IDENTITY_INVALID,
            repo=repo_label,
            work_id=work_id_label,
            field="repo",
        )
    next_actions = row.get("next_actions")
    if next_actions is not None and (
        not isinstance(next_actions, list)
        or any(not isinstance(action, str) or not action for action in next_actions)
    ):
        raise AuthorityValidationError(
            "canonical work authority actions malformed",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="next_actions",
        )
    if any(
        not isinstance(source, dict)
        or source.get("confidence") not in {"confirmed", "inferred"}
        for source in sources
    ):
        raise AuthorityValidationError(
            "canonical work authority sources malformed",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="sources",
        )
    if sources and all(source["confidence"] == "inferred" for source in sources):
        return None
    confirmed = [
        source
        for source in sources
        if isinstance(source, dict) and source.get("confidence") == "confirmed"
    ]
    has_workflow = any(source.get("kind") == "workflow_run" for source in confirmed)
    if next_actions is not None and "start" not in next_actions and not has_workflow:
        return None
    todo_kinds = {"todo", "superpowers_spec", "superpowers_plan", "openspec"}
    if not any(source.get("kind") in todo_kinds for source in confirmed):
        return None
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None:
        raise AuthorityValidationError(
            "canonical work authority identity invalid",
            reason_code=REASON_IDENTITY_INVALID,
            repo=repo_label,
            work_id=work_id_label,
            field="work_id",
        )
    provider_id = f"github:{repo}"
    github = providers.get(provider_id)
    if not isinstance(github, dict):
        raise AuthorityValidationError(
            "durable GitHub provider authority missing",
            reason_code=REASON_PROVIDER_MISSING_CANONICAL,
            repo=repo_label,
            work_id=work_id_label,
            provider_id=provider_id,
            field="providers",
        )
    revision = github.get("revision")
    last_success_at = github.get("last_success_at")
    if (
        github.get("status") != "ok"
        or not isinstance(revision, str)
        or not revision
        or not isinstance(last_success_at, str)
    ):
        if github.get("status") != "ok":
            field = "status"
        elif not isinstance(revision, str) or not revision:
            field = "revision"
        else:
            field = "last_success_at"
        raise AuthorityValidationError(
            "durable GitHub provider authority invalid",
            reason_code=REASON_PROVIDER_INVALID_CANONICAL,
            repo=repo_label,
            work_id=work_id_label,
            provider_id=provider_id,
            field=field,
        )
    try:
        last_success = datetime.fromisoformat(last_success_at.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise AuthorityValidationError(
            "durable GitHub provider timestamp invalid",
            reason_code=REASON_PROVIDER_INVALID_CANONICAL,
            repo=repo_label,
            work_id=work_id_label,
            provider_id=provider_id,
            field="last_success_at",
        ) from exc
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
                raise AuthorityValidationError(
                    "canonical GitHub work source ref invalid",
                    reason_code=REASON_ROW_MALFORMED,
                    repo=repo_label,
                    work_id=work_id_label,
                    field="sources.ref",
                )
            target = issues if kind == "github_issue" else prs
            target.append(int(match.group(1)))
        elif kind == "openspec":
            if not isinstance(ref, str) or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", ref) is None:
                raise AuthorityValidationError(
                    "canonical OpenSpec work source ref invalid",
                    reason_code=REASON_ROW_MALFORMED,
                    repo=repo_label,
                    work_id=work_id_label,
                    field="sources.ref",
                )
            changes.append(ref)
        elif kind == "todo":
            if not _safe_todo_path(ref):
                raise AuthorityValidationError(
                    "canonical Todo work source ref invalid",
                    reason_code=REASON_ROW_MALFORMED,
                    repo=repo_label,
                    work_id=work_id_label,
                    field="sources.ref",
                )
            todo_paths.append(ref)
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
            status=source.get("status") if isinstance(source.get("status"), str) else None,
        )
        if semantic is None:
            continue
        key, value = semantic
        previous = semantic_sources.setdefault(key, value)
        if previous != value:
            raise AuthorityValidationError(
                "confirmed semantic work authority revisions conflict",
                reason_code=REASON_ROW_MALFORMED,
                repo=repo_label,
                work_id=work_id_label,
                field="source_revisions",
            )
    source_revisions = tuple(
        f"{source_id}@{semantic_sources[source_id]}" for source_id in sorted(semantic_sources)
    )
    if not source_revisions:
        raise AuthorityValidationError(
            "confirmed work authority revisions invalid",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="source_revisions",
        )
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


def claim_identity_digest(authority: WorkAuthority) -> str:
    """Stable claim identity, excluding the planning-artifact-driven fields
    (#213, design #208 A.1: freeze point moves to *after* plan review passes).

    ``work_authority_digest`` folds in ``mapped_openspec``/``mapped_todo_paths``/
    ``source_revisions`` — exactly the fields a plan -> plan review -> revision
    loop touches as planning artifacts are drafted and rewritten. Comparing
    against the *full* digest while ``planning.plan_review_gate`` has not yet
    returned ``ready=True`` makes every plan revision look like a changed
    authority, so ``_existing()`` treats an active workflow as unmatched and
    the caller mints a fresh claim — the mechanism behind hippo #18's #3/#7
    v3->v4->... authority generation growth. This digest is the light,
    GitHub-anchored identity (``mapped_issues``/``mapped_prs``/``confirmed_todo``)
    a plan revision alone cannot change, used by ``_existing()`` while an
    active run's plan review has not passed yet.
    """
    if not isinstance(authority, WorkAuthority):
        raise ValueError("confirmed WorkAuthority is required")
    payload = {
        "repo": authority.repo,
        "work_id": authority.work_id,
        "provider_id": authority.github_provider_id,
        "mapped_issues": list(authority.mapped_issues),
        "mapped_prs": list(authority.mapped_prs),
        "confirmed_todo": authority.confirmed_todo,
    }
    return verification.canonical_json_hash(payload)


def _load_work_authorities_with_diagnostics(
    *, snapshot_path: str | Path | None = None
) -> tuple[tuple[WorkAuthority, ...], tuple[AuthorityValidationError, ...]]:
    """Parse every row independently (#206 AC4): one row's validation failure
    is recorded as ``AuthorityValidationError`` diagnostics and the row is
    dropped from the result, but parsing continues for the remaining rows —
    an unrelated repo's degraded/malformed provider must never blast-radius
    a healthy repo's work-action (durable "authority invalid" recurrence).

    Fail-closed is preserved: a skipped row's work item simply never appears
    in the returned authorities, so any lookup for it still fails — just
    with the specific skip reason (see ``load_work_authority``) instead of
    aborting the whole snapshot load.
    """
    payload, digest = _load_snapshot(snapshot_path)
    providers = payload["providers"]
    parsed: list[WorkAuthority] = []
    skipped: list[AuthorityValidationError] = []
    for row in payload["work_items"]:
        try:
            authority = _authority_from_row(row=row, providers=providers, snapshot_hash=digest)
        except AuthorityValidationError as exc:
            skipped.append(exc)
            continue
        if authority is not None:
            parsed.append(authority)
    authorities = tuple(parsed)
    identities = [(authority.repo, authority.work_id) for authority in authorities]
    if len(set(identities)) != len(identities):
        raise ValueError("confirmed work authority missing or ambiguous")
    # Source-owner transfers (#217, design #208 D) move an issue's mapped_issues
    # from one work_id to another. If the durable snapshot is ever read back
    # while two different work_ids both confirm the same issue — the mid-
    # transfer state that must never surface — refuse rather than silently
    # picking a "winner": every claim/ship/abandon caller loads authority
    # through here, so this closes the ambiguity at the single choke point.
    # These two integrity checks are snapshot-wide invariants, not per-row
    # parsing failures, so they intentionally keep the pre-#206 raise
    # behaviour rather than joining the per-row isolation above.
    owners: dict[tuple[str, int], str] = {}
    for authority in authorities:
        for issue in authority.mapped_issues:
            key = (authority.repo, issue)
            owner = owners.setdefault(key, authority.work_id)
            if owner != authority.work_id:
                raise ValueError("confirmed work authority missing or ambiguous")
    return authorities, tuple(skipped)


def load_work_authorities(
    *, snapshot_path: str | Path | None = None
) -> tuple[WorkAuthority, ...]:
    authorities, _skipped = _load_work_authorities_with_diagnostics(snapshot_path=snapshot_path)
    return authorities


def load_work_authority(
    *,
    repo: str,
    work_id: str,
    snapshot_path: str | Path | None = None,
) -> WorkAuthority:
    authorities, skipped = _load_work_authorities_with_diagnostics(snapshot_path=snapshot_path)
    matches = [
        authority
        for authority in authorities
        if authority.repo == repo and authority.work_id == work_id
    ]
    if len(matches) == 1:
        return matches[0]
    # #206 AC1/C：目標本身就是被跳過的壞 row → 拋出帶該 row reason code 的錯誤，
    # 而不是泛化的 missing/ambiguous，讓呼叫端能診斷「因為它的 authority 無效」。
    for exc in skipped:
        if exc.repo == repo and exc.work_id in (None, work_id):
            raise exc
    payload, _ = _load_snapshot(snapshot_path)
    if isinstance(payload, dict) and payload.get("last_refresh_error"):
        raise ValueError(
            f"confirmed work authority missing or ambiguous (monitor refresh failed: {payload['last_refresh_error']})"
        )
    raise ValueError("confirmed work authority missing or ambiguous")


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
    active_plan_review_passed: bool = True
    active_claim_identity_digest: str | None = None
    # #256 R2：resume 對 needs_human 必須說得出「為什麼卡住、現在能做什麼」。
    # 以下三個欄位是那個判斷的唯一輸入，全部取自系統寫入的 run 狀態／evidence
    # （current_phase 與 planning failure record），呼叫端自述不得寫入。
    active_phase: str | None = None
    active_planning_failure_classification: str | None = None
    active_planning_failure_reason: str | None = None


@dataclass(frozen=True)
class ClaimDecision:
    action: str
    reason: str | None = None
    claim_key: str | None = None
    run_id: str | None = None
    next_actions: tuple[str, ...] = ()
    # #256 R2：`reason` 是動作語意（呼叫端據以分支，維持穩定）；`blocking_reason`
    # 才是「這個 run 到底為什麼停住」的具體原因，取自 run 自己的 evidence，
    # 拿不到時為 None（不編造）。
    blocking_reason: str | None = None


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
        ("active_plan_review_passed", candidate.active_plan_review_passed),
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
    if candidate.active_planning_failure_classification is not None and (
        candidate.active_planning_failure_classification not in {"environment", "content"}
    ):
        raise ValueError("active planning failure classification invalid")
    if candidate.active_planning_failure_reason is not None and (
        not isinstance(candidate.active_planning_failure_reason, str)
        or not candidate.active_planning_failure_reason.strip()
    ):
        raise ValueError("active planning failure reason must be a non-empty string")
    if (
        candidate.active_planning_failure_classification is None
        and candidate.active_planning_failure_reason is not None
    ):
        raise ValueError("planning failure reason requires its classification")
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
        if candidate.active_status not in {
            "ongoing",
            "needs_human",
            "blocked",
            "done",
            "needs_decomposition",
        }:
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
        if not candidate.active_plan_review_passed and (
            not isinstance(candidate.active_claim_identity_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", candidate.active_claim_identity_digest) is None
        ):
            # #213：plan review 尚未通過（freeze 未發生）時，_existing() 改用
            # claim_identity_digest 比對，這個欄位就是它比對的持久化基準。
            raise ValueError("active workflow pre-freeze identity digest missing")


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
    if not candidate.active_plan_review_passed:
        # #213（design #208 A.1）：freeze point 位於 plan review 通過之後。
        # 這個 run 的 plan 仍在 plan -> revision 迴圈裡（尚未 freeze），只比對
        # 穩定 identity（claim_identity_digest，不含 mapped_openspec/
        # mapped_todo_paths/source_revisions）——plan 修訂造成這些欄位飄移不算
        # authority 變更，不觸發 supersede、不生出新世代（hippo #18 #3/#7）。
        # 持久化的 claim_key 是在 plan 存在之前鎖定的，此時不得拿（帶有目前飄移
        # 欄位的）完整 digest 反向驗證它，所以不做 expected_key 比對。
        if candidate.active_claim_identity_digest != claim_identity_digest(candidate.authority):
            return None
        return _resume_decision(candidate)
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
    return _resume_decision(candidate)


def _resume_decision(candidate: ClaimCandidate) -> ClaimDecision:
    if candidate.active_status == "done":
        return ClaimDecision(
            action="done",
            reason="already-completed",
            claim_key=candidate.active_claim_key,
            run_id=candidate.active_run_id,
            next_actions=(),
        )
    if candidate.active_status == "needs_human":
        # #256 R2：不得只原樣回報狀態。`abandon` 永遠合法（釋放後可重 claim，R3）；
        # `recover-planning` 只有在該 run 自己的 evidence 顯示「停在 define 的
        # 環境類 planning 失敗」時才是合法出口——內容類失敗不得由本路徑繞過
        # （R1 fail-closed），拿不到 evidence 時也不宣稱它可用。
        classification = candidate.active_planning_failure_classification
        recoverable = classification == "environment" and candidate.active_phase == "define"
        next_actions = ("recover-planning", "abandon") if recoverable else ("abandon",)
        blocking_reason = (
            f"planning-failure:{classification}:{candidate.active_planning_failure_reason}"
            if classification is not None and candidate.active_planning_failure_reason
            else None
        )
        return ClaimDecision(
            action="needs_human",
            reason="human-intervention-required",
            claim_key=candidate.active_claim_key,
            run_id=candidate.active_run_id,
            next_actions=next_actions,
            blocking_reason=blocking_reason,
        )
    if candidate.active_status == "blocked":
        return ClaimDecision(
            action="blocked",
            reason="persisted-block",
            claim_key=candidate.active_claim_key,
            run_id=candidate.active_run_id,
            next_actions=("abandon",),
        )
    if candidate.active_status == "needs_decomposition":
        # #223（design #208 H.3）：run 已因 Red band 轉入拆分路由（見
        # workflow_status()／manager._dispatch_workflow_card）。resume 掃描
        # 不得把它當成一般 in-flight run 續跑，必須原樣浮現給呼叫端另行處理
        # （回派 planner 拆分或人工介入），不得以原身分繼續重試。
        return ClaimDecision(
            action="needs_decomposition",
            reason="decomposition-required",
            claim_key=candidate.active_claim_key,
            run_id=candidate.active_run_id,
            next_actions=(),
        )
    return ClaimDecision(
        action="resume",
        reason="active-workflow",
        claim_key=candidate.active_claim_key,
        run_id=candidate.active_run_id,
        next_actions=(),
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


# --- #222（design #208 H.2）：五維 sizing 總分 → band 判定 -------------------
#
# band 字串沿用 deck.schema.BAND_LEVELS（green/yellow/red），不得另立常數或大小
# 寫變體。閾值 Green 0–3／Yellow 4–6／Red 7–10，對應 planning.SizingScore.total
# （五維、每維 0–2、總分 0–10，見 #221）。claim.py／registry.py／completion.py
# 三處共用這份純函式，避免各自硬編碼門檻造成漂移。band 本身只負責重算與記錄
# （#222）；跨帶上升後的拆分「路由」屬 #223，不在本模組範圍。
SIZING_BAND_GREEN_MAX = 3
SIZING_BAND_YELLOW_MAX = 6


def sizing_band(total: int) -> str:
    """五維 sizing 總分（0–10）→ band。呼叫端每次 repair／re-claim 都須重新
    傳入當下算出的 total，不得沿用 claim 當時判定的舊值（#222 驗收條件 3）。
    """
    if not isinstance(total, int) or isinstance(total, bool) or not (0 <= total <= 10):
        raise ValueError("sizing total 必須為 0–10 的整數")
    if total <= SIZING_BAND_GREEN_MAX:
        return BAND_LEVELS[0]
    if total <= SIZING_BAND_YELLOW_MAX:
        return BAND_LEVELS[1]
    return BAND_LEVELS[2]


# --- #223（design #208 H.3）：Red band 拆分路由 -----------------------------
#
# sizing_band()=='red' 的 work item 不得直接進 build，也不得帶著原 run 身分
# 繼續重試（#223 驗收條件 4）：收斂路徑是 needs_decomposition（回派 planner
# 拆分，拆分屬 Yellow 級 planning 工作，在 planner 封套內，design #208 原文）。
# 拆分深度（WorkflowRun.decomposition_depth，根 work item 為 0）每拆一層 +1，
# 上限 DECOMPOSITION_DEPTH_LIMIT 層；逾限改轉 needs_human，不得無限拆分下去
# （#223 驗收條件 3）。呼叫端（manager._dispatch_workflow_card 的 plan phase
# 完成掛載點）只需傳入目前的 decomposition_depth，不必自行重複這條門檻判定。
DECOMPOSITION_DEPTH_LIMIT = 2


def decomposition_route(*, decomposition_depth: int) -> str:
    """Red band 的路由決策：回傳 ``"needs_decomposition"`` 或 ``"needs_human"``。

    只在呼叫端已確認 ``sizing_band(total) == "red"`` 時呼叫；Green/Yellow 不
    經過此函式。
    """
    if (
        not isinstance(decomposition_depth, int)
        or isinstance(decomposition_depth, bool)
        or decomposition_depth < 0
    ):
        raise ValueError("decomposition_depth 必須為非負整數")
    if decomposition_depth >= DECOMPOSITION_DEPTH_LIMIT:
        return "needs_human"
    return "needs_decomposition"
