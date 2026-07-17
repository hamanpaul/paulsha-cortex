"""Thread-safe Work Item read model, versioned envelopes, and socket client."""
from __future__ import annotations

import json
import re
import socket
import subprocess
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .config import default_socket_path
from .correlation import correlate_work_sources
from .lifecycle import project_work_items
from .models import ProjectState
from .providers import GitHubWorkProvider, RepoWorkProvider
from .work_models import ProviderSnapshot
from .work_models import WorkItem
from .work_snapshot import WorkSnapshot, WorkSnapshotStore


WORK_API_SCHEMA = "cortex-work/v1"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_state(state: str) -> str:
    normalized = "ongoing" if state == "on-going" else state
    if normalized not in {"topic", "todo", "ongoing", "done"}:
        raise ValueError(f"unsupported work state: {state!r}")
    return normalized


@dataclass(frozen=True)
class WorkChangeEvent:
    sequence: int
    work_item: WorkItem
    removed: bool = False


class WorkReadModelStore:
    def __init__(
        self,
        snapshot: WorkSnapshot,
        *,
        explanations: Mapping[str, Mapping] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._snapshot = snapshot
        self._items = {item.work_id: item for item in snapshot.work_items}
        self._explanations = {
            work_id: dict(explanation)
            for work_id, explanation in (explanations or {}).items()
        }

    @classmethod
    def empty(cls) -> "WorkReadModelStore":
        return cls(
            WorkSnapshot(
                sequence=0,
                written_at=_utcnow(),
                providers={},
                work_items=(),
                source_owners={},
                exclusions=(),
            )
        )

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._snapshot.sequence

    def current_items(self) -> tuple[WorkItem, ...]:
        with self._lock:
            return tuple(
                sorted(self._items.values(), key=lambda item: (item.repo, item.work_id))
            )

    def current_snapshot(self) -> WorkSnapshot:
        with self._lock:
            return self._snapshot

    def replace(
        self,
        snapshot: WorkSnapshot,
        *,
        explanations: Mapping[str, Mapping] | None = None,
    ) -> tuple[WorkChangeEvent, ...]:
        with self._lock:
            previous = self._items
            current = {item.work_id: item for item in snapshot.work_items}
            events: list[WorkChangeEvent] = []
            sequence = self._snapshot.sequence
            for work_id in sorted(previous.keys() - current.keys()):
                sequence += 1
                events.append(WorkChangeEvent(sequence, previous[work_id], removed=True))
            for work_id, item in sorted(current.items()):
                if previous.get(work_id) != item:
                    sequence += 1
                    events.append(WorkChangeEvent(sequence, item))
            sequence = max(sequence, snapshot.sequence)
            if sequence != snapshot.sequence:
                snapshot = WorkSnapshot(
                    sequence=sequence,
                    written_at=snapshot.written_at,
                    providers=snapshot.providers,
                    work_items=snapshot.work_items,
                    source_owners=snapshot.source_owners,
                    exclusions=snapshot.exclusions,
                )
            self._snapshot = snapshot
            self._items = current
            if explanations is not None:
                self._explanations = {
                    work_id: dict(explanation)
                    for work_id, explanation in explanations.items()
                }
            return tuple(events)

    def list_work_items(
        self,
        *,
        repo: str | None = None,
        states: Sequence[str] = (),
        include_done: bool = False,
        explain: bool = False,
    ) -> dict:
        normalized_states = {_normalize_state(state) for state in states}
        with self._lock:
            items = []
            explanations: dict[str, Mapping] = {}
            for item in sorted(self._items.values(), key=lambda row: (row.repo, row.work_id)):
                if repo is not None and item.repo != repo:
                    continue
                if not include_done and item.state == "done":
                    continue
                if normalized_states and item.state not in normalized_states:
                    continue
                items.append(item.to_dict())
                if explain:
                    explanations[item.work_id] = self._explanation(item.work_id)
            envelope = self._envelope()
            envelope["items"] = items
            if explain:
                envelope["explanations"] = explanations
            return envelope

    def get_work_item(self, work_id: str) -> dict:
        with self._lock:
            item = self._items.get(work_id)
            if item is None:
                raise KeyError(work_id)
            envelope = self._envelope()
            envelope["item"] = item.to_dict()
            return envelope

    def explain_work_item(self, work_id: str) -> dict:
        envelope = self.get_work_item(work_id)
        envelope["explanation"] = self._explanation(work_id)
        return envelope

    def _explanation(self, work_id: str) -> Mapping:
        return self._explanations.get(
            work_id,
            {
                "work_id": work_id,
                "authoritative_links": [],
                "inferred_signals": [],
                "competing_candidates": [],
                "exclusions": [],
                "reducer_trace": [],
            },
        )

    def _envelope(self) -> dict:
        providers = []
        for provider_id in sorted(self._snapshot.providers):
            provider = self._snapshot.providers[provider_id]
            row = {"provider_id": provider_id, **provider.to_dict()}
            providers.append(row)
        degraded = any(row["status"] == "degraded" for row in providers) or any(
            "degraded" in item.facets for item in self._items.values()
        )
        return {
            "schema": WORK_API_SCHEMA,
            "generated_at": self._snapshot.written_at,
            "sequence": self._snapshot.sequence,
            "degraded": degraded,
            "providers": providers,
        }


class MonitorSocketClient:
    def __init__(self, socket_path: str | Path | None = None, *, timeout: float = 5.0) -> None:
        self.socket_path = Path(socket_path) if socket_path is not None else default_socket_path()
        self.timeout = timeout

    def request(self, payload: Mapping) -> dict:
        body = (json.dumps(dict(payload), ensure_ascii=False) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(str(self.socket_path))
            client.sendall(body)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        if not chunks:
            raise RuntimeError("monitor socket returned no response")
        try:
            response = json.loads(b"".join(chunks).split(b"\n", 1)[0])
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid monitor response: {error}") from error
        if not isinstance(response, dict):
            raise RuntimeError("invalid monitor response object")
        return response


class WorkModelRefresher:
    """Run providers, correlate/reduce, persist, then emit read-model events."""

    def __init__(
        self,
        *,
        durable_store: WorkSnapshotStore,
        read_store: WorkReadModelStore,
        github_provider_factory: Callable[[str], GitHubWorkProvider] | None = None,
    ) -> None:
        self.durable_store = durable_store
        self.read_store = read_store
        self.github_provider_factory = github_provider_factory or GitHubWorkProvider
        self._lock = threading.Lock()

    def refresh(
        self,
        projects: Sequence[ProjectState],
        *,
        include_github: bool,
    ) -> tuple[WorkChangeEvent, ...]:
        with self._lock:
            previous = self.read_store.current_snapshot()
            providers = dict(previous.providers)
            projected_items: list[WorkItem] = []
            source_owners: dict[str, str] = {}
            exclusions: list[Mapping[str, str]] = []
            explanations: dict[str, Mapping] = {}
            attempted_at = _utcnow()
            for project in sorted(projects, key=lambda item: item.path):
                if project.legacy:
                    continue
                root = Path(project.path)
                repo, is_github = _repo_identity(root, project.project_id)
                local_result = RepoWorkProvider(root, repo=repo).scan()
                previous_local = providers.get(local_result.provider_id)
                local = _retain_last_good(previous_local, local_result)
                providers[local.provider_id] = local
                relevant = [local]
                github_id = f"github:{repo}"
                if include_github and is_github:
                    github_result = self.github_provider_factory(repo).scan()
                    github = _retain_last_good(providers.get(github_id), github_result)
                    providers[github_id] = github
                    relevant.append(github)
                elif github_id in providers:
                    relevant.append(providers[github_id])
                sources = tuple(
                    source for provider in relevant for source in provider.sources
                )
                correlation = correlate_work_sources(root, repo, sources)
                if correlation.degraded and local_result.status == "ok":
                    collision_result = replace(
                        local_result,
                        status="degraded",
                        last_success_at=None,
                        revision=None,
                        diagnostics=correlation.diagnostics,
                        sources=(),
                    )
                    local = _retain_last_good(previous_local, collision_result)
                    providers[local.provider_id] = local
                    relevant[0] = local
                degraded_notes = [
                    note
                    for provider in relevant
                    if provider.status == "degraded"
                    for note in provider.diagnostics
                ]
                if degraded_notes and not correlation.degraded:
                    correlation = replace(
                        correlation,
                        degraded=True,
                        diagnostics=tuple(degraded_notes),
                    )
                prior = tuple(item for item in previous.work_items if item.repo == repo)
                projection = project_work_items(
                    correlation,
                    repo=repo,
                    updated_at=attempted_at,
                    previous_items=prior,
                )
                projected_items.extend(projection.items)
                explanations.update(projection.explanations)
                if correlation.degraded:
                    projected_ids = {item.work_id for item in projection.items}
                    source_owners.update(
                        (source_id, owner)
                        for source_id, owner in previous.source_owners.items()
                        if owner in projected_ids
                    )
                    exclusions.extend(previous.exclusions)
                else:
                    source_owners.update(correlation.source_owners)
                    exclusions.extend(correlation.exclusions)
            snapshot = WorkSnapshot(
                sequence=previous.sequence + 1,
                written_at=attempted_at,
                providers=providers,
                work_items=tuple(projected_items),
                source_owners=source_owners,
                exclusions=tuple(_dedupe_mappings(exclusions)),
            )
            self.durable_store.write(snapshot)
            return self.read_store.replace(snapshot, explanations=explanations)


def _retain_last_good(
    previous: ProviderSnapshot | None, result: ProviderSnapshot
) -> ProviderSnapshot:
    if result.status == "ok" or previous is None:
        return replace(result, sources=()) if result.status == "degraded" else result
    return replace(
        result,
        last_success_at=previous.last_success_at,
        revision=previous.revision,
        sources=previous.sources,
    )


_GITHUB_SSH = re.compile(r"^(?:ssh://)?git@github\.com[:/](?P<repo>[^/]+/[^/]+?)(?:\.git)?$")
_GITHUB_HTTPS = re.compile(r"^https?://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$")


def _repo_identity(root: Path, fallback: str) -> tuple[str, bool]:
    if fallback.count("/") == 1 and all(fallback.split("/")):
        return fallback, True
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return fallback, False
    if completed.returncode != 0:
        return fallback, False
    remote = completed.stdout.strip()
    for pattern in (_GITHUB_SSH, _GITHUB_HTTPS):
        match = pattern.fullmatch(remote)
        if match:
            return match.group("repo"), True
    return fallback, False


def _dedupe_mappings(rows: Sequence[Mapping[str, str]]) -> tuple[Mapping[str, str], ...]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    result: list[Mapping[str, str]] = []
    for row in rows:
        key = tuple(sorted(row.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(row))
    return tuple(result)
