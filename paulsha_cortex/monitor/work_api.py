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
from .correlation import InferredSignal, correlate_work_sources
from .lifecycle import ClosureEvidence, project_work_items
from .models import ProjectState
from .providers import (
    GitHubTerminalProvider,
    GitHubWorkProvider,
    RepoWorkProvider,
    WorkflowRegistryProvider,
)
from .work_models import ProviderSnapshot
from .work_models import WorkItem
from .work_snapshot import WorkSnapshot, WorkSnapshotStore, work_key


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


class AmbiguousWorkItemError(LookupError):
    """A bare work ID matches more than one repository."""


class WorkReadModelStore:
    def __init__(
        self,
        snapshot: WorkSnapshot,
        *,
        explanations: Mapping[str, Mapping] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._snapshot = snapshot
        self._items = {(item.repo, item.work_id): item for item in snapshot.work_items}
        self._explanations = {
            identity: dict(explanation)
            for identity, explanation in (explanations or {}).items()
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
        return self.replace_durably(snapshot, None, explanations=explanations)

    def replace_durably(
        self,
        snapshot: WorkSnapshot,
        persist: Callable[[WorkSnapshot], None] | None,
        *,
        explanations: Mapping[str, Mapping] | None = None,
    ) -> tuple[WorkChangeEvent, ...]:
        """Resolve the final sequence, durably write it, then publish in memory."""
        with self._lock:
            previous = self._items
            current = {(item.repo, item.work_id): item for item in snapshot.work_items}
            events: list[WorkChangeEvent] = []
            sequence = self._snapshot.sequence
            for identity in sorted(previous.keys() - current.keys()):
                sequence += 1
                events.append(WorkChangeEvent(sequence, previous[identity], removed=True))
            for identity, item in sorted(current.items()):
                if previous.get(identity) != item:
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
            if persist is not None:
                persist(snapshot)
            self._snapshot = snapshot
            self._items = current
            if explanations is not None:
                self._explanations = {
                    identity: dict(explanation)
                    for identity, explanation in explanations.items()
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
                    explanations[work_key(item.repo, item.work_id)] = self._explanation(
                        item.work_id, repo=item.repo
                    )
            envelope = self._envelope()
            envelope["items"] = items
            if explain:
                envelope["explanations"] = explanations
            return envelope

    def get_work_item(self, work_id: str, *, repo: str | None = None) -> dict:
        with self._lock:
            matches = [
                item
                for (item_repo, item_id), item in self._items.items()
                if item_id == work_id and (repo is None or item_repo == repo)
            ]
            if not matches:
                raise KeyError(work_id)
            if len(matches) > 1:
                raise AmbiguousWorkItemError(work_id)
            item = matches[0]
            envelope = self._envelope()
            envelope["item"] = item.to_dict()
            return envelope

    def explain_work_item(self, work_id: str, *, repo: str | None = None) -> dict:
        envelope = self.get_work_item(work_id, repo=repo)
        envelope["explanation"] = self._explanation(work_id, repo=envelope["item"]["repo"])
        return envelope

    def _explanation(self, work_id: str, *, repo: str | None = None) -> Mapping:
        identity = work_key(repo, work_id) if repo is not None else work_id
        return self._explanations.get(
            identity,
            self._explanations.get(
                work_id,
            {
                "work_id": work_id,
                "authoritative_links": [],
                "inferred_signals": [],
                "competing_candidates": [],
                "exclusions": [],
                "reducer_trace": [],
            },
            ),
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
            "hard_gates": self._hard_gates(providers),
        }

    @staticmethod
    def _hard_gates(providers: Sequence[Mapping]) -> dict:
        reasons: list[str] = []
        for row in providers:
            if row["status"] != "degraded":
                continue
            stale = next(
                (note for note in row.get("diagnostics", []) if note.endswith(" stale")),
                None,
            )
            reasons.append(stale or f"{row['provider_id']} degraded")
        return {
            "auto_claim": not reasons,
            "merge": not reasons,
            "reasons": reasons,
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
        github_terminal_provider_factory: Callable[[str], GitHubTerminalProvider] | None = None,
        workflow_provider_factory: Callable[[str], WorkflowRegistryProvider] | None = None,
        stale_after_seconds: int = 900,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.durable_store = durable_store
        self.read_store = read_store
        self.github_provider_factory = github_provider_factory or GitHubWorkProvider
        self.github_terminal_provider_factory = (
            github_terminal_provider_factory or GitHubTerminalProvider
        )
        self.workflow_provider_factory = workflow_provider_factory or WorkflowRegistryProvider
        self.stale_after_seconds = stale_after_seconds
        self.now = now or (lambda: datetime.now(timezone.utc))
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
            current_time = self.now()
            if current_time.tzinfo is None:
                raise ValueError("now must include timezone")
            attempted_at = current_time.isoformat().replace("+00:00", "Z")
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
                workflow_result = self.workflow_provider_factory(repo).scan()
                workflow = _retain_last_good(
                    providers.get(workflow_result.provider_id), workflow_result
                )
                providers[workflow.provider_id] = workflow
                relevant.append(workflow)
                github_id = f"github:{repo}"
                if include_github and is_github:
                    github_result = self.github_provider_factory(repo).scan()
                    github = _retain_last_good(providers.get(github_id), github_result)
                    providers[github_id] = github
                    relevant.append(github)
                    terminal_result = self.github_terminal_provider_factory(repo).scan()
                    terminal = _retain_last_good(
                        providers.get(terminal_result.provider_id), terminal_result
                    )
                    providers[terminal.provider_id] = terminal
                    relevant.append(terminal)
                elif github_id in providers:
                    relevant.append(providers[github_id])
                    terminal_id = f"github-terminal:{repo}"
                    if terminal_id in providers:
                        relevant.append(providers[terminal_id])
                if github_id in providers and not WorkSnapshot(
                    sequence=previous.sequence,
                    written_at=attempted_at,
                    providers=providers,
                    work_items=(),
                    source_owners={},
                    exclusions=(),
                ).provider_is_fresh(
                    github_id,
                    now=current_time,
                    max_age=self.stale_after_seconds,
                ):
                    stale = providers[github_id]
                    stale = replace(
                        stale,
                        status="degraded",
                        last_attempt_at=attempted_at,
                        diagnostics=tuple(
                            dict.fromkeys((*stale.diagnostics, f"{github_id} stale"))
                        ),
                    )
                    providers[github_id] = stale
                    relevant = [
                        stale if provider.provider_id == github_id else provider
                        for provider in relevant
                    ]
                sources = tuple(
                    source for provider in relevant for source in provider.sources
                )
                observations = _merge_observations(relevant)
                inferred_signals = (
                    *_parse_inferred_signals(observations),
                    *_generate_inferred_signals(sources, observations),
                )
                correlation = correlate_work_sources(
                    root,
                    repo,
                    sources,
                    inferred_signals=inferred_signals,
                    closing_links=observations.get("closing_links", {}),
                    workflow_links=observations.get("workflow_links", {}),
                )
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
                    closure_by_work=_parse_closure_evidence(
                        observations, correlation=correlation
                    ),
                )
                projected_items.extend(projection.items)
                explanations.update(
                    (work_key(repo, work_id), explanation)
                    for work_id, explanation in projection.explanations.items()
                )
                if correlation.degraded:
                    projected_ids = {
                        work_key(item.repo, item.work_id) for item in projection.items
                    }
                    source_owners.update(
                        (source_id, owner)
                        for source_id, owner in previous.source_owners.items()
                        if owner in projected_ids
                    )
                    exclusions.extend(previous.exclusions)
                else:
                    source_owners.update(
                        (source_id, work_key(repo, owner))
                        for source_id, owner in correlation.source_owners.items()
                    )
                    exclusions.extend(correlation.exclusions)
            snapshot = WorkSnapshot(
                sequence=previous.sequence + 1,
                written_at=attempted_at,
                providers=providers,
                work_items=tuple(projected_items),
                source_owners=source_owners,
                exclusions=tuple(_dedupe_mappings(exclusions)),
            )
            return self.read_store.replace_durably(
                snapshot, self.durable_store.write, explanations=explanations
            )


def _merge_observations(providers: Sequence[ProviderSnapshot]) -> dict:
    merged: dict[str, object] = {
        "closing_links": {},
        "workflow_links": {},
        "closure_by_work": {},
        "inferred_signals": [],
        "remote_openspec": {"active": [], "archived": []},
        "remote_openspec_observed": False,
        "remote_todos": [],
        "remote_prs": [],
        "branches": [],
    }
    for provider in providers:
        observations = provider.observations
        if provider.provider_id.startswith("github-terminal:"):
            value = observations.get("closing_links", {})
            if isinstance(value, Mapping):
                merged["closing_links"].update(value)
        if provider.provider_id.startswith("workflow:"):
            value = observations.get("workflow_links", {})
            if isinstance(value, Mapping):
                merged["workflow_links"].update(value)
        closure = observations.get("closure_by_work", {})
        if isinstance(closure, Mapping):
            for work_id, facts in closure.items():
                if isinstance(work_id, str) and isinstance(facts, Mapping):
                    accepted: dict[str, object] = {}
                    if provider.provider_id.startswith("workflow:"):
                        if "completion_record_valid" in facts:
                            accepted["completion_record_valid"] = facts[
                                "completion_record_valid"
                            ]
                    merged["closure_by_work"].setdefault(work_id, {}).update(accepted)
        signals = observations.get("inferred_signals", [])
        if isinstance(signals, list):
            merged["inferred_signals"].extend(signals)
        if provider.provider_id.startswith("github-terminal:"):
            remote = observations.get("remote_openspec", {})
            if isinstance(remote, Mapping):
                for state in ("active", "archived"):
                    values = remote.get(state, [])
                    if isinstance(values, list):
                        merged["remote_openspec"][state].extend(
                            value for value in values if isinstance(value, str)
                        )
            if observations.get("remote_openspec_observed") is True:
                merged["remote_openspec_observed"] = True
            for key in ("remote_todos", "remote_prs", "branches"):
                values = observations.get(key, [])
                if isinstance(values, list):
                    merged[key].extend(
                        value for value in values if isinstance(value, Mapping)
                    )
    return merged


def _parse_inferred_signals(observations: Mapping) -> tuple[InferredSignal, ...]:
    parsed: list[InferredSignal] = []
    for row in observations.get("inferred_signals", []):
        if not isinstance(row, Mapping):
            continue
        try:
            parsed.append(
                InferredSignal(
                    work_id=row["work_id"],
                    kind=row["kind"],
                    value=row["value"],
                    source_ids=tuple(row["source_ids"]),
                    weight=float(row.get("weight", 1.0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(parsed)


def _generate_inferred_signals(
    sources: Sequence, observations: Mapping
) -> tuple[InferredSignal, ...]:
    """Generate display-only fuzzy evidence; correlation still owns competition checks."""
    signals: list[InferredSignal] = []
    artifacts: list[tuple[object, str]] = []
    issues: list[tuple[object, str, str]] = []
    for source in sources:
        if source.kind == "github_issue" and source.title:
            slug = _slug(source.title)
            if slug:
                issues.append((source, slug, source.ref.rsplit("#", 1)[-1]))
                signals.append(
                    InferredSignal(slug, "issue_title", source.title, (source.source_id,), 1.0)
                )
        artifact = _artifact_slug(source)
        if artifact:
            artifacts.append((source, artifact))
            signals.append(
                InferredSignal(
                    artifact, "artifact_slug", source.ref, (source.source_id,), 1.0
                )
            )
    for issue, _, number in issues:
        for artifact_source, artifact in artifacts:
            if re.search(rf"(?<!\d){re.escape(number)}(?!\d)", artifact_source.ref):
                signals.append(
                    InferredSignal(
                        artifact,
                        "issue_token",
                        number,
                        (issue.source_id, artifact_source.source_id),
                        0.8,
                    )
                )
    for branch in observations.get("branches", []):
        if not isinstance(branch, Mapping):
            continue
        source_id = branch.get("source_id")
        ref = branch.get("ref")
        if not isinstance(source_id, str) or not isinstance(ref, str):
            continue
        candidate = _slug(ref.rsplit("/", 1)[-1])
        if candidate:
            candidate = re.sub(r"^\d+-", "", candidate)
        if candidate:
            signals.append(InferredSignal(candidate, "branch_slug", ref, (source_id,), 0.7))
    unique: dict[tuple[str, str, tuple[str, ...]], InferredSignal] = {}
    for signal in signals:
        unique[(signal.work_id, signal.kind, signal.source_ids)] = signal
    return tuple(unique.values())


def _slug(value: str) -> str | None:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug if slug and re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug) else None


def _artifact_slug(source) -> str | None:
    if source.kind == "openspec":
        return _slug(source.ref)
    if source.kind not in {"todo", "superpowers_spec", "superpowers_plan"}:
        return None
    path = Path(source.ref)
    value = path.parent.name if path.name == "todo.md" else path.stem
    value = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", value)
    return _slug(value)


def _parse_closure_evidence(
    observations: Mapping, *, correlation=None
) -> dict[str, ClosureEvidence]:
    fields = ClosureEvidence.__dataclass_fields__
    combined: dict[str, dict[str, bool]] = {}
    for work_id, row in observations.get("closure_by_work", {}).items():
        if not isinstance(work_id, str) or not isinstance(row, Mapping):
            continue
        resolved = work_id
        if work_id.startswith("@source:") and correlation is not None:
            source_id = work_id[len("@source:") :]
            resolved = next(
                (
                    group.work_id
                    for group in correlation.groups
                    if any(source.source_id == source_id for source in group.sources)
                ),
                "",
            )
        if not resolved:
            continue
        facts = combined.setdefault(resolved, {})
        for name in fields:
            if name in {"issues_all_closed", "todo_tasks_complete"}:
                continue
            if name in row:
                facts[name] = row.get(name) is True
    remote = observations.get("remote_openspec", {})
    active = set(remote.get("active", [])) if isinstance(remote, Mapping) else set()
    archived = set(remote.get("archived", [])) if isinstance(remote, Mapping) else set()
    remote_todos = [
        todo
        for todo in observations.get("remote_todos", [])
        if isinstance(todo, Mapping)
        and isinstance(todo.get("revision"), str)
        and re.fullmatch(r"[0-9a-fA-F]{40}", todo["revision"])
        and isinstance(todo.get("path"), str)
        and isinstance(todo.get("complete"), bool)
    ]
    remote_prs = {
        row["source_id"]: row
        for row in observations.get("remote_prs", [])
        if isinstance(row, Mapping)
        and isinstance(row.get("source_id"), str)
        and isinstance(row.get("merged_with_merge_commit"), bool)
    }
    for group in getattr(correlation, "groups", ()):
        if group.work_id not in combined:
            continue
        issues = [
            source
            for source in group.sources
            if source.kind == "github_issue" and source.confidence == "confirmed"
        ]
        combined[group.work_id]["issues_all_closed"] = bool(issues) and all(
            source.status == "closed" for source in issues
        )
        prs = [
            source
            for source in group.sources
            if source.kind == "github_pr" and source.confidence == "confirmed"
        ]
        combined[group.work_id]["pr_merged_with_merge_commit"] = bool(prs) and all(
            source.status == "closed"
            and source.source_id in remote_prs
            and remote_prs[source.source_id]["merged_with_merge_commit"] is True
            for source in prs
        )
        openspec_refs = {
            source.ref
            for source in group.sources
            if source.kind == "openspec" and source.confidence == "confirmed"
        }
        if observations.get("remote_openspec_observed") is True:
            combined[group.work_id]["remote_active_openspec_absent"] = bool(
                openspec_refs
            ) and all(ref not in active for ref in openspec_refs)
            combined[group.work_id]["remote_archive_present"] = bool(
                openspec_refs
            ) and all(ref in archived for ref in openspec_refs)
        doc_todos = [todo for todo in remote_todos if todo.get("work_id") == group.work_id]
        openspec_todos = [
            todo for todo in remote_todos if todo.get("openspec_ref") in openspec_refs
        ]
        todo_evidence = [*doc_todos, *openspec_todos]
        openspec_tasks_complete = not openspec_refs or openspec_refs.issubset(
            {str(todo.get("openspec_ref")) for todo in openspec_todos}
        )
        combined[group.work_id]["todo_tasks_complete"] = bool(todo_evidence) and all(
            todo["complete"] is True for todo in todo_evidence
        ) and openspec_tasks_complete
    return {
        work_id: ClosureEvidence(
            **{name: facts.get(name, False) for name in fields}
        )
        for work_id, facts in combined.items()
    }


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
