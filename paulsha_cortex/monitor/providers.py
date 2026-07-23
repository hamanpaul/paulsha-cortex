"""Authoritative source providers for the unified Monitor read model."""
from __future__ import annotations

import hashlib
import base64
import binascii
import json
import math
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import quote

import yaml

from paulsha_cortex.config import paths

from .work_models import ProviderSnapshot, WorkSource


_ARCHIVE_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-(?P<name>.+)$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(parts: Sequence[bytes]) -> str:
    value = hashlib.sha256()
    for part in parts:
        value.update(len(part).to_bytes(8, "big"))
        value.update(part)
    return value.hexdigest()


def _read_revision(path: Path) -> str:
    return f"local-sha256:{_digest((path.read_bytes(),))}"


def _safe_file(root: Path, path: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise OSError(f"artifact escapes repository root: {path}") from error
    if not resolved.is_file():
        raise OSError(f"artifact is not a regular file: {path}")
    return resolved


class RepoWorkProvider:
    """Scan only the fixed artifact paths defined by the accepted contract."""

    def __init__(self, repo_root: str | Path, *, repo: str) -> None:
        self.repo_root = Path(repo_root)
        self.repo = repo
        self.provider_id = f"repo:{repo}"

    def scan(self) -> ProviderSnapshot:
        attempted_at = _utcnow()
        try:
            sources = self._scan_sources()
            observations = self._scan_observations(sources)
            collisions = self._active_archive_collisions()
            revision = "repo-overlay:" + _digest(
                tuple(
                    f"{source.source_id}\0{source.revision}".encode("utf-8")
                    for source in sources
                )
            )
        except (OSError, UnicodeError, ValueError) as error:
            return ProviderSnapshot(
                provider_id=self.provider_id,
                status="degraded",
                last_attempt_at=attempted_at,
                last_success_at=None,
                revision=None,
                diagnostics=(f"repo scan unavailable: {error}",),
                sources=(),
                observations={},
            )
        if collisions:
            return ProviderSnapshot(
                provider_id=self.provider_id,
                status="degraded",
                last_attempt_at=attempted_at,
                last_success_at=None,
                revision=None,
                diagnostics=tuple(
                    f"active/archive collision: {name}" for name in collisions
                ),
                sources=sources,
                observations=observations,
            )
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="ok",
            last_attempt_at=attempted_at,
            last_success_at=attempted_at,
            revision=revision,
            diagnostics=(),
            sources=sources,
            observations=observations,
        )

    def _scan_observations(self, sources: Sequence[WorkSource]) -> dict:
        signals: list[dict[str, object]] = []
        for source in sources:
            if source.kind not in {
                "todo",
                "superpowers_spec",
                "superpowers_plan",
                "openspec",
            }:
                continue
            paths_to_check: list[Path]
            if source.kind == "openspec":
                change = self.repo_root / "openspec" / "changes" / source.ref
                paths_to_check = [change / "proposal.md", change / "design.md", change / "tasks.md"]
            else:
                paths_to_check = [self.repo_root / source.ref]
            work_id = next(
                (
                    value
                    for path in paths_to_check
                    if path.is_file()
                    if (value := _frontmatter_work_item(path)) is not None
                ),
                None,
            )
            if work_id is None:
                continue
            signals.append(
                {
                    "work_id": work_id,
                    "kind": "artifact_slug",
                    "value": source.ref,
                    "source_ids": [source.source_id],
                    "weight": 1.0,
                }
            )
        return {"inferred_signals": signals}

    def _scan_sources(self) -> tuple[WorkSource, ...]:
        definitions = (
            ("todo", "docs/superpowers/workstreams/**/todo.md"),
            ("superpowers_spec", "docs/superpowers/specs/**/*.md"),
            ("superpowers_plan", "docs/superpowers/plans/**/*.md"),
        )
        sources: list[WorkSource] = []
        for kind, pattern in definitions:
            for discovered in sorted(self.repo_root.glob(pattern)):
                path = _safe_file(self.repo_root, discovered)
                relative = discovered.relative_to(self.repo_root).as_posix()
                sources.append(
                    WorkSource(
                        source_id=f"{kind}:{self.repo}:{relative}",
                        kind=kind,
                        ref=relative,
                        revision=_read_revision(path),
                        status="active",
                        confidence="confirmed",
                        provider=self.provider_id,
                    )
                )

        changes = self.repo_root / "openspec" / "changes"
        if changes.is_dir():
            for change_dir in sorted(changes.iterdir()):
                if change_dir.name == "archive" or not change_dir.is_dir():
                    continue
                files = self._openspec_files(change_dir)
                if not files:
                    continue
                revision_parts: list[bytes] = []
                for path in files:
                    safe = _safe_file(self.repo_root, path)
                    relative = path.relative_to(self.repo_root).as_posix()
                    revision_parts.extend((relative.encode("utf-8"), safe.read_bytes()))
                sources.append(
                    WorkSource(
                        source_id=f"openspec:{self.repo}:{change_dir.name}",
                        kind="openspec",
                        ref=change_dir.name,
                        revision=f"local-sha256:{_digest(tuple(revision_parts))}",
                        status="active",
                        confidence="confirmed",
                        provider=self.provider_id,
                    )
                )
        return tuple(sorted(sources, key=lambda source: (source.kind, source.ref)))

    @staticmethod
    def _openspec_files(change_dir: Path) -> tuple[Path, ...]:
        files = [
            path
            for name in ("proposal.md", "design.md", "tasks.md")
            if (path := change_dir / name).is_file()
        ]
        specs = change_dir / "specs"
        if specs.is_dir():
            files.extend(path for path in specs.rglob("*.md") if path.is_file())
        return tuple(sorted(files))

    def _active_archive_collisions(self) -> tuple[str, ...]:
        changes = self.repo_root / "openspec" / "changes"
        archive = changes / "archive"
        if not changes.is_dir() or not archive.is_dir():
            return ()
        active = {
            path.name
            for path in changes.iterdir()
            if path.is_dir() and path.name != "archive"
        }
        archived: set[str] = set()
        for path in archive.iterdir():
            if not path.is_dir():
                continue
            match = _ARCHIVE_DATE_PREFIX.match(path.name)
            archived.add(match.group("name") if match else path.name)
        return tuple(sorted(active & archived))


class WorkflowRegistryProvider:
    """Read repo-scoped WorkflowRun v2 records without adopting legacy slices."""

    def __init__(self, repo: str, *, state_path: str | Path | None = None) -> None:
        self.repo = repo
        self.provider_id = f"workflow:{repo}"
        self.state_path = (
            Path(state_path)
            if state_path is not None
            else paths.coordinator_root() / "jobs.json"
        )

    def scan(self) -> ProviderSnapshot:
        attempted_at = _utcnow()
        if not self.state_path.exists():
            return ProviderSnapshot(
                provider_id=self.provider_id,
                status="ok",
                last_attempt_at=attempted_at,
                last_success_at=attempted_at,
                revision="registry:absent",
                diagnostics=(),
                sources=(),
                observations={},
            )
        try:
            raw = self.state_path.read_bytes()
            payload = json.loads(raw)
            if not isinstance(payload, Mapping):
                raise ValueError("registry root must be an object")
            version = payload.get("schema_version")
            if version == 1:
                _validate_workflow_v1_root(payload)
                return ProviderSnapshot(
                    provider_id=self.provider_id,
                    status="ok",
                    last_attempt_at=attempted_at,
                    last_success_at=attempted_at,
                    revision=f"registry-sha256:{_digest((raw,))}",
                    diagnostics=(),
                    sources=(),
                    observations={},
                )
            if "workflows" in payload:
                rows = _validate_canonical_coordinator_v2_root(payload)
            else:
                _validate_workflow_v2_root(payload)
                rows = payload["workflow_runs"]
            sources: list[WorkSource] = []
            links: dict[str, str] = {}
            diagnostics: list[str] = []
            validated_completions: dict[str, list[dict[str, object]]] = {}
            for row in rows:
                _validate_workflow_v2_row(row)
                if row.get("repo") != self.repo:
                    continue
                run_id = _nonempty(row.get("run_id"), "run_id")
                work_id = _nonempty(row.get("work_id"), "work_id")
                try:
                    completion = _validated_workflow_completion(
                        row, state_path=self.state_path
                    )
                except _WorkflowCompletionValidationError as error:
                    diagnostics.append(
                        f"workflow completion skipped: {run_id}: {error}"
                    )
                    continue
                status = _nonempty(row.get("status", row.get("current_phase")), "status")
                source_id = f"workflow_run:{self.repo}:{run_id}"
                sources.append(
                    WorkSource(
                        source_id=source_id,
                        kind="workflow_run",
                        ref=run_id,
                        revision=f"registry:{payload.get('sequence', payload.get('seq', 0))}",
                        status=status,
                        confidence="confirmed",
                        provider=self.provider_id,
                    )
                )
                _add_workflow_link(links, source_id, work_id)
                for ref in row.get("issue_refs", []):
                    _add_workflow_link(links, f"github_issue:{ref}", work_id)
                for ref in row.get("pr_refs", []):
                    _add_workflow_link(links, f"github_pr:{ref}", work_id)
                for ref in row.get("openspec_refs", []):
                    for canonical_id in (
                        f"openspec:{self.repo}:{ref}",
                        f"github_openspec:{self.repo}:{ref}:active",
                        f"github_openspec:{self.repo}:{ref}:archived",
                    ):
                        _add_workflow_link(links, canonical_id, work_id)
                if completion is not None:
                    validated_completions.setdefault(work_id, []).append(completion)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            return ProviderSnapshot(
                provider_id=self.provider_id,
                status="degraded",
                last_attempt_at=attempted_at,
                last_success_at=None,
                revision=None,
                diagnostics=(f"workflow registry unavailable: {error}",),
                sources=(),
                observations={},
            )
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="ok",
            last_attempt_at=attempted_at,
            last_success_at=attempted_at,
            revision=f"registry-sha256:{_digest((raw,))}",
            diagnostics=tuple(diagnostics),
            sources=tuple(sources),
            observations={
                "workflow_links": links,
                "validated_completions": validated_completions,
            },
        )


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


_WORKFLOW_V1_KEYS = frozenset({"schema_version", "seq", "jobs", "slices"})
_WORKFLOW_V2_KEYS = frozenset(
    {"schema_version", "sequence", "workflow_runs", "legacy_records"}
)
_WORKFLOW_V2_REQUIRED_ROW_KEYS = frozenset({"run_id", "repo", "work_id"})
_WORKFLOW_V2_OPTIONAL_ROW_KEYS = frozenset(
    {
        "status", "current_phase", "claim_key", "combo", "steps", "issue_refs",
        "openspec_refs", "pr_refs", "attempts", "evidence", "facets",
        "created_at", "updated_at", "completion_record_path",
        "completion_record_hash", "completion_record_revision", "source_revisions",
        "pr_candidate", "merge_revision",
        # Exact coordinator.workflow.WorkflowRun fields.
        "source_revision", "workspace_root", "evidence_refs", "gate_refs",
        "brainstorm_required", "primary_domain", "candidate_head",
        "verified_head", "gate_status", "completion_source_revisions",
        "planning_authority", "planning_source_revision",
    }
)


class _WorkflowCompletionValidationError(ValueError):
    """Bad completion contents for one row; provider may skip that row only."""


def _validate_workflow_v1_root(payload: Mapping) -> None:
    if set(payload) != _WORKFLOW_V1_KEYS:
        raise ValueError("workflow registry v1 root keys are invalid")
    if (
        isinstance(payload.get("seq"), bool)
        or not isinstance(payload.get("seq"), int)
        or not isinstance(payload.get("jobs"), list)
        or not isinstance(payload.get("slices"), list)
    ):
        raise ValueError("workflow registry v1 root values are invalid")


def _validate_workflow_v2_root(payload: Mapping) -> None:
    if payload.get("schema_version") != 2:
        raise ValueError("unsupported workflow registry schema_version")
    if set(payload) != _WORKFLOW_V2_KEYS:
        raise ValueError("workflow registry v2 root keys are invalid")
    sequence = payload.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("workflow registry sequence must be a non-negative integer")
    if not isinstance(payload.get("workflow_runs"), list):
        raise ValueError("workflow_runs must be an array")
    legacy = payload.get("legacy_records")
    if (
        not isinstance(legacy, Mapping)
        or set(legacy) != {"jobs", "slices"}
        or not isinstance(legacy.get("jobs"), list)
        or not isinstance(legacy.get("slices"), list)
    ):
        raise ValueError("workflow registry legacy_records are invalid")


def _validate_canonical_coordinator_v2_root(payload: Mapping) -> list[dict[str, object]]:
    """Read the exact JobRegistry v2 schema without instantiating its writer.

    Monitor is strictly read-only here: importing ``JobRegistry`` would run its
    migration path and violate the single-writer boundary.
    """

    expected = {"schema_version", "seq", "jobs", "slices", "workflows", "legacy_records"}
    if set(payload) != expected or payload.get("schema_version") != 2:
        raise ValueError("canonical coordinator registry root keys are invalid")
    if (
        isinstance(payload.get("seq"), bool)
        or not isinstance(payload.get("seq"), int)
        or payload["seq"] < 0
        or not isinstance(payload.get("jobs"), list)
        or not isinstance(payload.get("slices"), list)
        or not isinstance(payload.get("workflows"), list)
        or not isinstance(payload.get("legacy_records"), Mapping)
    ):
        raise ValueError("canonical coordinator registry root values are invalid")
    from paulsha_cortex.coordinator.workflow import WorkflowRun

    return [WorkflowRun.from_dict(row).to_dict() for row in payload["workflows"]]


def _validate_workflow_v2_row(row: object) -> None:
    if not isinstance(row, Mapping):
        raise ValueError("workflow run must be an object")
    keys = set(row)
    if not _WORKFLOW_V2_REQUIRED_ROW_KEYS.issubset(keys):
        raise ValueError("workflow run misses required keys")
    if keys - _WORKFLOW_V2_REQUIRED_ROW_KEYS - _WORKFLOW_V2_OPTIONAL_ROW_KEYS:
        raise ValueError("workflow run contains unsupported keys")
    if "status" not in row and "current_phase" not in row:
        raise ValueError("workflow run requires status or current_phase")
    repo = _nonempty(row.get("repo"), "repo")
    if re.fullmatch(r"[^/#\s]+/[^/#\s]+", repo) is None:
        raise ValueError("workflow repo must be owner/name")
    work_id = _nonempty(row.get("work_id"), "work_id")
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None:
        raise ValueError("workflow work_id must be a slug")
    _nonempty(row.get("run_id"), "run_id")
    _nonempty(row.get("status", row.get("current_phase")), "status")
    for field in ("issue_refs", "pr_refs"):
        refs = _typed_workflow_refs(row.get(field, []), field=field)
        if any(
            re.fullmatch(rf"{re.escape(repo)}#[1-9][0-9]*", ref) is None
            for ref in refs
        ):
            raise ValueError(f"workflow {field} must contain repo-scoped refs")
    openspec_refs = _typed_workflow_refs(
        row.get("openspec_refs", []), field="openspec_refs"
    )
    if any(re.fullmatch(r"[a-z0-9][a-z0-9-]*", ref) is None for ref in openspec_refs):
        raise ValueError("workflow openspec_refs must contain slugs")


def _typed_workflow_refs(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(ref, str) or not ref for ref in value
    ):
        raise ValueError(f"workflow {field} must be an array of strings")
    if len(value) != len(set(value)):
        raise ValueError(f"workflow {field} contains duplicate refs")
    return tuple(value)


def _add_workflow_link(links: dict[str, str], source_id: str, work_id: str) -> None:
    previous = links.setdefault(source_id, work_id)
    if previous != work_id:
        raise ValueError(
            f"workflow authority collision: {source_id} -> {previous}, {work_id}"
        )


def _validated_workflow_completion(
    row: Mapping, *, state_path: Path
) -> dict[str, object] | None:
    fields = (
        row.get("completion_record_path"),
        row.get("completion_record_hash"),
        row.get("completion_record_revision"),
    )
    if all(value is None for value in fields):
        return None
    if any(value is None for value in fields):
        raise ValueError("completion record path/hash/revision must be supplied together")
    record_path, expected_hash, expected_revision = fields
    if not isinstance(record_path, str) or not record_path:
        raise ValueError("completion_record_path must be a non-empty string")
    if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) is None:
        raise ValueError("completion_record_hash must be a 64-char hex digest")
    if not isinstance(expected_revision, str) or re.fullmatch(r"[0-9a-fA-F]{40}", expected_revision) is None:
        raise ValueError("completion_record_revision must be a 40-char commit SHA")
    source_revisions = row.get("source_revisions", row.get("completion_source_revisions"))
    if (
        not isinstance(source_revisions, Mapping)
        or not source_revisions
        or any(
            not isinstance(source_id, str)
            or not source_id
            or not isinstance(revision, str)
            or not revision
            for source_id, revision in source_revisions.items()
        )
    ):
        raise ValueError("completion source_revisions must be a non-empty string map")
    pr_candidate = row.get("pr_candidate")
    merge_revision = row.get("merge_revision")
    for field, value in (
        ("pr_candidate", pr_candidate),
        ("merge_revision", merge_revision),
    ):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
            raise ValueError(f"{field} must be a 40-char commit SHA")
    try:
        path = Path(record_path)
        if path.is_symlink():
            raise ValueError("completion_record_path must not be a symlink")
        resolved = path.resolve(strict=True)
        allowed_root = (state_path.parent / "evidence" / "completion").resolve()
        try:
            resolved.relative_to(allowed_root)
        except ValueError as error:
            raise ValueError("completion_record_path escapes coordinator completion root") from error
        from paulsha_cortex.coordinator.completion import read_completion_record

        record = read_completion_record(resolved, expected_hash=expected_hash.lower())
    except (OSError, ValueError) as error:
        raise _WorkflowCompletionValidationError(str(error)) from error
    normalized_sources = dict(source_revisions)
    authority_record = record.get("work_authority")
    if isinstance(authority_record, Mapping):
        raw_sources = authority_record.get("source_revisions")
        if not isinstance(raw_sources, list) or any(
            not isinstance(value, str) or "@" not in value for value in raw_sources
        ):
            return None
        record_sources = {
            value.rsplit("@", 1)[0]: value.rsplit("@", 1)[1]
            for value in raw_sources
        }
        record_work_id = authority_record.get("work_id")
        record_run_id = authority_record.get("run_id")
        record_merge_revision = authority_record.get("merge_commit")
    else:
        record_sources = record.get("source_revisions")
        record_work_id = record.get("work_id")
        record_run_id = record.get("run_id")
        record_merge_revision = record.get("merge_revision")
    valid = all(
        (
            record.get("candidate") == expected_revision.lower() == pr_candidate.lower(),
            record_work_id == row.get("work_id"),
            record_run_id == row.get("run_id"),
            record_sources == normalized_sources,
            record_merge_revision == merge_revision.lower(),
        )
    )
    if not valid:
        return None
    return {
        "run_id": row["run_id"],
        "pr_candidate": pr_candidate.lower(),
        "merge_revision": merge_revision.lower(),
        "source_revisions": normalized_sources,
    }


def _frontmatter_work_item(path: Path) -> str | None:
    return _frontmatter_work_item_text(path.read_text(encoding="utf-8"), source=str(path))


def _frontmatter_work_item_text(text: str, *, source: str) -> str | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise ValueError(f"unterminated frontmatter: {source}")
    payload = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"frontmatter must be an object: {source}")
    value = payload.get("work_item")
    return value if isinstance(value, str) and value else None


def _markdown_tasks_complete(path: Path) -> bool:
    return _markdown_tasks_complete_text(path.read_text(encoding="utf-8"))


def _markdown_tasks_complete_text(text: str) -> bool:
    tasks = re.findall(r"^\s*[-*]\s+\[([ xX])\]", text, flags=re.MULTILINE)
    return bool(tasks) and all(marker.lower() == "x" for marker in tasks)


class CommandRunner(Protocol):
    def run(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessCommandRunner:
    def run(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


class GitHubWorkProvider:
    """Read GitHub entities through authenticated ``gh api`` JSON only."""

    def __init__(
        self,
        repo: str,
        *,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        if repo.count("/") != 1 or any(not part for part in repo.split("/")):
            raise ValueError("GitHub repo must use owner/name")
        self.repo = repo
        self.provider_id = f"github:{repo}"
        self.runner = runner or SubprocessCommandRunner()
        self.timeout_seconds = timeout_seconds

    def scan(self) -> ProviderSnapshot:
        attempted_at = _utcnow()
        argv = (
            "gh",
            "api",
            "--method",
            "GET",
            "--paginate",
            "--jq",
            ".[]",
            f"repos/{self.repo}/issues?state=all&per_page=100",
        )
        try:
            completed = self.runner.run(argv, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            return self._failure(attempted_at, "github timeout")
        except FileNotFoundError:
            return self._failure(attempted_at, "github CLI unavailable")
        except OSError:
            return self._failure(attempted_at, "github provider I/O failure")
        if completed.returncode != 0:
            message = completed.stderr.decode(errors="replace") if isinstance(completed.stderr, bytes) else completed.stderr
            lowered = (message or "").lower()
            if "401" in lowered or "bad credentials" in lowered or "auth" in lowered:
                diagnostic = "github authentication failed"
            elif "rate limit" in lowered or "403" in lowered:
                diagnostic = "github rate limit exceeded"
            else:
                diagnostic = "github API request failed"
            return self._failure(attempted_at, diagnostic)
        stdout = completed.stdout.decode() if isinstance(completed.stdout, bytes) else completed.stdout
        try:
            entities = [
                json.loads(line)
                for line in stdout.splitlines()
                if line.strip()
            ]
            if any(not isinstance(entity, dict) for entity in entities):
                raise ValueError("GitHub entity is not an object")
            sources = tuple(self._entity_source(entity) for entity in entities)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            return self._failure(attempted_at, "github API returned malformed JSON")
        sources = tuple(sorted(sources, key=lambda source: (source.kind, source.ref)))
        revision = "github-snapshot:" + _digest(
            tuple(
                f"{source.source_id}\0{source.revision}\0{source.status}".encode("utf-8")
                for source in sources
            )
        )
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="ok",
            last_attempt_at=attempted_at,
            last_success_at=attempted_at,
            revision=revision,
            diagnostics=(),
            sources=sources,
        )

    def _failure(self, attempted_at: str, diagnostic: str) -> ProviderSnapshot:
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="degraded",
            last_attempt_at=attempted_at,
            last_success_at=None,
            revision=None,
            diagnostics=(diagnostic,),
            sources=(),
        )

    def _entity_source(self, entity: dict) -> WorkSource:
        number = entity["number"]
        title = entity["title"]
        state = entity["state"]
        node_id = entity["node_id"]
        updated_at = entity["updated_at"]
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError("invalid issue number")
        if any(not isinstance(value, str) or not value for value in (title, state, node_id, updated_at)):
            raise ValueError("invalid GitHub entity fields")
        kind = "github_pr" if "pull_request" in entity else "github_issue"
        ref = f"{self.repo}#{number}"
        return WorkSource(
            source_id=f"{kind}:{ref}",
            kind=kind,
            ref=ref,
            revision=f"github:{node_id}:{updated_at}",
            status=state,
            confidence="confirmed",
            provider=self.provider_id,
            title=title,
        )


class GitHubTerminalProvider:
    """Read closing references and remote default-branch archive evidence."""

    _QUERY = """query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){defaultBranchRef{name target{... on Commit{oid}}} pullRequests(first:100,after:$cursor,states:[OPEN,CLOSED,MERGED]){pageInfo{hasNextPage endCursor} nodes{number body headRefName headRefOid state mergedAt mergeCommit{oid parents(first:3){totalCount}} closingIssuesReferences(first:100){pageInfo{hasNextPage} nodes{number state}}}}}}"""
    _PULL_REQUEST_PAGE_LIMIT = 20

    def __init__(
        self,
        repo: str,
        *,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 30,
        retry_delays: tuple[float, ...] = (2.0, 5.0, 10.0),
        sleeper: Callable[[float], None] = time.sleep,
        relevant_pr_numbers: tuple[int, ...] | None = None,
    ) -> None:
        self.repo = repo
        self.provider_id = f"github-terminal:{repo}"
        self.runner = runner or SubprocessCommandRunner()
        self.timeout_seconds = timeout_seconds
        if any(
            not isinstance(delay, (int, float))
            or isinstance(delay, bool)
            or not math.isfinite(float(delay))
            or delay < 0
            for delay in retry_delays
        ):
            raise ValueError("GitHub terminal retry delays must be finite non-negative numbers")
        self.retry_delays = tuple(float(delay) for delay in retry_delays)
        self.sleeper = sleeper
        if relevant_pr_numbers is not None and (
            len(relevant_pr_numbers) != len(set(relevant_pr_numbers))
            or any(
                not isinstance(number, int)
                or isinstance(number, bool)
                or number <= 0
                for number in relevant_pr_numbers
            )
        ):
            raise ValueError("relevant PR numbers must be unique positive integers")
        self.relevant_pr_numbers = (
            None if relevant_pr_numbers is None else frozenset(relevant_pr_numbers)
        )

    def scan(self) -> ProviderSnapshot:
        attempted_at = _utcnow()
        owner, name = self.repo.split("/", 1)
        try:
            graph = self._json(self._pull_request_argv(owner=owner, name=name))
            repository = graph["data"]["repository"]
            default_branch_ref = repository["defaultBranchRef"]
            pulls = repository["pullRequests"]
            pull_nodes = list(pulls["nodes"])
            page_count = 1
            while pulls["pageInfo"]["hasNextPage"]:
                if page_count >= self._PULL_REQUEST_PAGE_LIMIT:
                    raise ValueError("pull request pagination incomplete")
                cursor = pulls["pageInfo"]["endCursor"]
                if not isinstance(cursor, str) or not cursor:
                    raise ValueError("pull request pagination incomplete")
                graph = self._json(
                    self._pull_request_argv(owner=owner, name=name, cursor=cursor)
                )
                repository = graph["data"]["repository"]
                pulls = repository["pullRequests"]
                pull_nodes.extend(pulls["nodes"])
                page_count += 1
            default_branch = default_branch_ref["name"]
            default_revision = default_branch_ref["target"]["oid"]
            if re.fullmatch(r"[0-9a-fA-F]{40}", default_revision) is None:
                raise ValueError("default branch revision is invalid")
            tree = self._json(
                (
                    "gh", "api", "--method", "GET",
                    f"repos/{self.repo}/git/trees/{default_revision}?recursive=1",
                )
            )
            if tree.get("truncated") is not False:
                raise ValueError("default branch tree is truncated")
            if not isinstance(tree.get("tree"), list):
                raise ValueError("default branch tree entries are invalid")
            remote_todos = self._remote_todos(
                tree,
                default_revision=default_revision.lower(),
            )
            paths = {
                row["path"]
                for row in tree["tree"]
                if isinstance(row, Mapping) and isinstance(row.get("path"), str)
            }
            active_changes = {
                parts[2]
                for path in paths
                if len(parts := path.split("/")) >= 4
                and parts[:2] == ["openspec", "changes"]
                and parts[2] != "archive"
            }
            archived_changes = {
                match.group("name")
                for path in paths
                if path.startswith("openspec/changes/archive/")
                if len(path.split("/")) >= 5
                if (match := _ARCHIVE_DATE_PREFIX.match(path.split("/")[3]))
            }
            if active_changes & archived_changes:
                raise ValueError("remote active/archive OpenSpec collision")
            sources = tuple(
                WorkSource(
                    source_id=f"github_openspec:{self.repo}:{ref}:{status}",
                    kind="openspec",
                    ref=ref,
                    revision=f"github-tree:{default_revision.lower()}",
                    status=status,
                    confidence="confirmed",
                    provider=self.provider_id,
                    title=ref,
                )
                for status, refs in (
                    ("active", sorted(active_changes)),
                    ("archived", sorted(archived_changes)),
                )
                for ref in refs
            )
            links: dict[str, str] = {}
            remote_prs: list[dict[str, object]] = []
            branches: list[dict[str, str]] = []
            for pull in pull_nodes:
                number = pull["number"]
                pr_source_id = f"github_pr:{self.repo}#{number}"
                head_ref = pull.get("headRefName")
                if isinstance(head_ref, str) and head_ref:
                    branches.append({"source_id": pr_source_id, "ref": head_ref})
                closing = pull["closingIssuesReferences"]
                if closing["pageInfo"]["hasNextPage"]:
                    raise ValueError("closing issue pagination incomplete")
                issues = closing["nodes"]
                if issues:
                    primary_issue_source = f"github_issue:{self.repo}#{issues[0]['number']}"
                    links[pr_source_id] = primary_issue_source
                    for issue in issues[1:]:
                        issue_source = f"github_issue:{self.repo}#{issue['number']}"
                        links[issue_source] = primary_issue_source
                merge = pull.get("mergeCommit") or {}
                merge_revision = merge.get("oid")
                parent_count = (merge.get("parents") or {}).get("totalCount")
                merge_commit = bool(
                    pull.get("state") == "MERGED"
                    and pull.get("mergedAt")
                    and isinstance(merge_revision, str)
                    and re.fullmatch(r"[0-9a-fA-F]{40}", merge_revision)
                    and isinstance(parent_count, int)
                    and not isinstance(parent_count, bool)
                    and parent_count >= 2
                    and (
                        self.relevant_pr_numbers is None
                        or number in self.relevant_pr_numbers
                    )
                )
                if merge_commit:
                    comparison = self._json(
                        (
                            "gh", "api", "--method", "GET",
                            f"repos/{self.repo}/compare/{merge_revision}...{default_revision}",
                        )
                    )
                    merge_commit = comparison.get("status") in {"ahead", "identical"}
                candidate = pull.get("headRefOid")
                remote_prs.append(
                    {
                        "source_id": pr_source_id,
                        "candidate": (
                            candidate.lower()
                            if isinstance(candidate, str)
                            and re.fullmatch(r"[0-9a-fA-F]{40}", candidate)
                            else None
                        ),
                        "merge_revision": (
                            merge_revision.lower()
                            if isinstance(merge_revision, str)
                            and re.fullmatch(r"[0-9a-fA-F]{40}", merge_revision)
                            else None
                        ),
                        "merged_with_merge_commit": merge_commit,
                    }
                )
        except subprocess.TimeoutExpired:
            return self._failure(attempted_at, "github terminal timeout")
        except FileNotFoundError:
            return self._failure(attempted_at, "github CLI unavailable")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return self._failure(attempted_at, "github terminal evidence unavailable")
        observations = {
            "closing_links": links,
            "remote_prs": sorted(remote_prs, key=lambda row: str(row["source_id"])),
            "remote_openspec": {
                "active": sorted(active_changes),
                "archived": sorted(archived_changes),
            },
            "remote_openspec_observed": True,
            "default_branch": default_branch,
            "default_revision": default_revision.lower(),
            "remote_todos": remote_todos,
            "branches": branches,
        }
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="ok",
            last_attempt_at=attempted_at,
            last_success_at=attempted_at,
            revision="github-terminal:" + _digest((json.dumps(observations, sort_keys=True).encode(),)),
            diagnostics=(),
            sources=sources,
            observations=observations,
        )

    def _pull_request_argv(
        self,
        *,
        owner: str,
        name: str,
        cursor: str | None = None,
    ) -> tuple[str, ...]:
        argv = [
            "gh", "api", "graphql",
            "-f", f"query={self._QUERY}",
            "-F", f"owner={owner}",
            "-F", f"name={name}",
        ]
        if cursor is not None:
            argv.extend(("-F", f"cursor={cursor}"))
        return tuple(argv)

    def _json(self, argv: Sequence[str]) -> Mapping:
        completed = None
        for attempt in range(len(self.retry_delays) + 1):
            completed = self.runner.run(argv, timeout=self.timeout_seconds)
            if completed.returncode == 0:
                break
            error = f"{completed.stderr}\n{completed.stdout}"
            if (
                attempt >= len(self.retry_delays)
                or re.search(r"\bHTTP (?:502|503|504)\b", error) is None
            ):
                raise OSError("gh api failed")
            self.sleeper(self.retry_delays[attempt])
        if completed is None or completed.returncode != 0:
            raise OSError("gh api failed")
        payload = json.loads(completed.stdout)
        if not isinstance(payload, Mapping):
            raise ValueError("GitHub response must be an object")
        return payload

    def _failure(self, attempted_at: str, diagnostic: str) -> ProviderSnapshot:
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="degraded",
            last_attempt_at=attempted_at,
            last_success_at=None,
            revision=None,
            diagnostics=(diagnostic,),
            sources=(),
            observations={},
        )

    def _remote_todos(
        self,
        tree: Mapping,
        *,
        default_revision: str,
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for entry in tree.get("tree", []):
            if not isinstance(entry, Mapping):
                continue
            path = entry.get("path")
            revision = entry.get("sha")
            if not isinstance(path, str):
                continue
            is_todo = re.fullmatch(
                r"docs/superpowers/workstreams/.+/todo\.md", path
            ) is not None
            archive_match = re.fullmatch(
                r"openspec/changes/archive/(\d{4}-\d{2}-\d{2}-.+)/tasks\.md",
                path,
            )
            if not is_todo and archive_match is None:
                continue
            if entry.get("type") != "blob" or not isinstance(revision, str) or re.fullmatch(
                r"[0-9a-fA-F]{40}", revision
            ) is None:
                raise ValueError("remote Todo tree entry is invalid")
            content_file = self._json(
                (
                    "gh", "api", "--method", "GET",
                    (
                        f"repos/{self.repo}/contents/{quote(path, safe='/')}"
                        f"?ref={default_revision}"
                    ),
                )
            )
            if (
                content_file.get("type") != "file"
                or content_file.get("path") != path
                or not isinstance(content_file.get("sha"), str)
                or content_file["sha"].lower() != revision.lower()
                or content_file.get("encoding") != "base64"
            ):
                raise ValueError("remote Todo content identity mismatch")
            content = content_file.get("content")
            if not isinstance(content, str):
                raise ValueError("remote Todo content is invalid")
            try:
                text = base64.b64decode(
                    re.sub(r"\s+", "", content), validate=True
                ).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as error:
                raise ValueError("remote Todo content is invalid") from error
            row: dict[str, object] = {
                "path": path,
                "revision": revision.lower(),
                "complete": _markdown_tasks_complete_text(text),
            }
            if archive_match is not None:
                archived_name = archive_match.group(1)
                match = _ARCHIVE_DATE_PREFIX.match(archived_name)
                if match is None:
                    raise ValueError("remote archived OpenSpec tasks path is invalid")
                row["openspec_ref"] = match.group("name")
            else:
                work_id = _frontmatter_work_item_text(
                    text, source=f"github:{path}@{revision}"
                )
                if work_id is None:
                    continue
                row["work_id"] = work_id
            rows.append(row)
        return sorted(
            rows,
            key=lambda row: (
                str(row.get("work_id", row.get("openspec_ref", ""))),
                str(row["path"]),
            ),
        )
