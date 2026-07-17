"""Authoritative source providers for the unified Monitor read model."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence

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
