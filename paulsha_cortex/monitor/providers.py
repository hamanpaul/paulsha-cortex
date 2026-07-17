"""Authoritative source providers for the unified Monitor read model."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

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
        closure: dict[str, dict[str, bool]] = {}
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
            task_files = [path for path in paths_to_check if path.name in {"todo.md", "tasks.md"}]
            if task_files:
                complete = all(_markdown_tasks_complete(path) for path in task_files)
                facts = closure.setdefault(work_id, {})
                facts["todo_tasks_complete"] = (
                    facts.get("todo_tasks_complete", True) and complete
                )
        return {"closure_by_work": closure, "inferred_signals": signals}

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
            else paths.coordinator_root() / "workflows.json"
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
            rows = payload.get("workflow_runs", payload.get("workflows", []))
            if not isinstance(rows, list):
                raise ValueError("workflow_runs must be an array")
            sources: list[WorkSource] = []
            links: dict[str, str] = {}
            closure: dict[str, dict[str, bool]] = {}
            for row in rows:
                if not isinstance(row, Mapping) or row.get("repo") != self.repo:
                    continue
                run_id = _nonempty(row.get("run_id"), "run_id")
                work_id = _nonempty(row.get("work_id"), "work_id")
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
                links[source_id] = work_id
                if _workflow_completion_record_valid(row):
                    closure.setdefault(work_id, {})["completion_record_valid"] = True
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
            diagnostics=(),
            sources=tuple(sources),
            observations={"workflow_links": links, "closure_by_work": closure},
        )


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _workflow_completion_record_valid(row: Mapping) -> bool:
    record_path = row.get("completion_record_path")
    if record_path is None:
        # Registry v2 may persist the Manager's validated result directly.
        return row.get("completion_record_valid") is True
    if not isinstance(record_path, str) or not record_path:
        raise ValueError("completion_record_path must be a non-empty string")
    expected_hash = row.get("completion_record_hash")
    if expected_hash is not None and (not isinstance(expected_hash, str) or not expected_hash):
        raise ValueError("completion_record_hash must be null or a non-empty string")
    from paulsha_cortex.coordinator.completion import read_completion_record

    read_completion_record(record_path, expected_hash=expected_hash)
    return True


def _frontmatter_work_item(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise ValueError(f"unterminated frontmatter: {path}")
    payload = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"frontmatter must be an object: {path}")
    value = payload.get("work_item")
    return value if isinstance(value, str) and value else None


def _markdown_tasks_complete(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
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
            "--slurp",
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
            payload = json.loads(stdout)
            entities = self._flatten_pages(payload)
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

    @staticmethod
    def _flatten_pages(payload: object) -> list[dict]:
        if not isinstance(payload, list):
            raise ValueError("GitHub response is not an array")
        pages = payload if not payload or isinstance(payload[0], list) else [payload]
        entities: list[dict] = []
        for page in pages:
            if not isinstance(page, list):
                raise ValueError("GitHub page is not an array")
            for entity in page:
                if not isinstance(entity, dict):
                    raise ValueError("GitHub entity is not an object")
                entities.append(entity)
        return entities

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
        )


class GitHubTerminalProvider:
    """Read closing references and remote default-branch archive evidence."""

    _QUERY = """query($owner:String!,$name:String!){repository(owner:$owner,name:$name){defaultBranchRef{name} pullRequests(first:100,states:[OPEN,CLOSED,MERGED]){pageInfo{hasNextPage} nodes{number body state mergedAt mergeCommit{oid} closingIssuesReferences(first:100){pageInfo{hasNextPage} nodes{number state}}}}}}"""

    def __init__(
        self,
        repo: str,
        *,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        self.repo = repo
        self.provider_id = f"github-terminal:{repo}"
        self.runner = runner or SubprocessCommandRunner()
        self.timeout_seconds = timeout_seconds

    def scan(self) -> ProviderSnapshot:
        attempted_at = _utcnow()
        owner, name = self.repo.split("/", 1)
        try:
            graph = self._json(
                (
                    "gh", "api", "graphql",
                    "-f", f"query={self._QUERY}",
                    "-F", f"owner={owner}",
                    "-F", f"name={name}",
                )
            )
            repository = graph["data"]["repository"]
            pulls = repository["pullRequests"]
            if pulls["pageInfo"]["hasNextPage"]:
                raise ValueError("pull request pagination incomplete")
            default_branch = repository["defaultBranchRef"]["name"]
            tree = self._json(
                (
                    "gh", "api", "--method", "GET",
                    f"repos/{self.repo}/git/trees/{default_branch}?recursive=1",
                )
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
            links: dict[str, str] = {}
            closure: dict[str, dict[str, bool]] = {}
            for pull in pulls["nodes"]:
                number = pull["number"]
                closing = pull["closingIssuesReferences"]
                if closing["pageInfo"]["hasNextPage"]:
                    raise ValueError("closing issue pagination incomplete")
                issues = closing["nodes"]
                if not issues:
                    continue
                explicit_work_id = _pr_work_id(pull.get("body"))
                primary_issue_source = f"github_issue:{self.repo}#{issues[0]['number']}"
                work_id = explicit_work_id or f"@source:{primary_issue_source}"
                links[f"github_pr:{self.repo}#{number}"] = (
                    explicit_work_id or primary_issue_source
                )
                for issue in issues:
                    issue_source = f"github_issue:{self.repo}#{issue['number']}"
                    if explicit_work_id is not None:
                        links[issue_source] = explicit_work_id
                    elif issue_source != primary_issue_source:
                        links[issue_source] = primary_issue_source
                closure[work_id] = {
                    "pr_merged_with_merge_commit": bool(
                        pull.get("mergedAt") and (pull.get("mergeCommit") or {}).get("oid")
                    ),
                    "issues_all_closed": all(issue.get("state") == "CLOSED" for issue in issues),
                }
        except subprocess.TimeoutExpired:
            return self._failure(attempted_at, "github terminal timeout")
        except FileNotFoundError:
            return self._failure(attempted_at, "github CLI unavailable")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return self._failure(attempted_at, "github terminal evidence unavailable")
        observations = {
            "closing_links": links,
            "closure_by_work": closure,
            "remote_openspec": {
                "active": sorted(active_changes),
                "archived": sorted(archived_changes),
            },
        }
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="ok",
            last_attempt_at=attempted_at,
            last_success_at=attempted_at,
            revision="github-terminal:" + _digest((json.dumps(observations, sort_keys=True).encode(),)),
            diagnostics=(),
            sources=(),
            observations=observations,
        )

    def _json(self, argv: Sequence[str]) -> Mapping:
        completed = self.runner.run(argv, timeout=self.timeout_seconds)
        if completed.returncode != 0:
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


def _pr_work_id(body: object) -> str | None:
    if not isinstance(body, str):
        return None
    match = re.search(r"(?m)^work_item:\s*([a-z0-9][a-z0-9-]*)\s*$", body)
    return match.group(1) if match else None
