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

from .config import load_config
from .correlation import InferredSignal, correlate_work_sources
from .git_mirror import GITHUB_HTTPS_REMOTE, GITHUB_SSH_REMOTE
from .github_pressure import GitHubPressureGate
from .lifecycle import ClosureEvidence, project_work_items
from .models import ProjectState
from .providers import (
    GitHubTerminalProvider,
    GitHubWorkProvider,
    RepoWorkProvider,
    WorkflowRegistryProvider,
)
from ..coordinator.terminal_contract import MAX_SCHEMA_RETRIES as SCHEMA_RETRY_LIMIT
from .work_models import ProviderSnapshot
from .work_models import WorkItem
from .work_snapshot import WorkSnapshot, WorkSnapshotStore, work_key


WORK_API_SCHEMA = "cortex-work/v1"


def _workflow_linked_pr_numbers(
    provider: ProviderSnapshot,
    *,
    repo: str,
) -> tuple[int, ...]:
    links = provider.observations.get("workflow_links", {})
    if not isinstance(links, Mapping):
        return ()
    prefix = f"github_pr:{repo}#"
    numbers = {
        int(source_id[len(prefix) :])
        for source_id in links
        if isinstance(source_id, str)
        and source_id.startswith(prefix)
        and source_id[len(prefix) :].isdigit()
        and int(source_id[len(prefix) :]) > 0
    }
    return tuple(sorted(numbers))


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
        self._last_refresh_error: str | None = snapshot.last_refresh_error
        self._consecutive_refresh_failures: int = snapshot.consecutive_refresh_failures

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

    @property
    def last_refresh_error(self) -> str | None:
        with self._lock:
            return self._last_refresh_error or self._snapshot.last_refresh_error

    @property
    def consecutive_refresh_failures(self) -> int:
        with self._lock:
            return self._consecutive_refresh_failures or self._snapshot.consecutive_refresh_failures

    def record_refresh_failure(self, exc: Exception | str) -> None:
        with self._lock:
            self._last_refresh_error = str(exc)
            self._consecutive_refresh_failures += 1
            self._snapshot = replace(
                self._snapshot,
                last_refresh_error=self._last_refresh_error,
                consecutive_refresh_failures=self._consecutive_refresh_failures,
            )

    def record_refresh_success(self) -> None:
        with self._lock:
            self._last_refresh_error = None
            self._consecutive_refresh_failures = 0
            self._snapshot = replace(
                self._snapshot,
                last_refresh_error=None,
                consecutive_refresh_failures=0,
            )

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
            envelope = self._envelope(repo=repo)
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
                err_msg = work_id
                if self.last_refresh_error is not None:
                    err_msg = f"{work_id} (monitor refresh failing: {self.last_refresh_error})"
                raise KeyError(err_msg)
            if len(matches) > 1:
                raise AmbiguousWorkItemError(work_id)
            item = matches[0]
            envelope = self._envelope(repo=item.repo, work_id=item.work_id)
            envelope["item"] = item.to_dict()
            # #261 D5：schema mismatch retry 計數／上限，讓 retry storm 在面板上就是
            # 可見的數字，operator 不必翻 job jsonl。資料源是 workflow provider 的
            # observations（已隨 snapshot 一起 round-trip），因此不需要為此在
            # WorkflowRun 或 WorkSnapshot 上新增欄位。
            retries = self._schema_retry(item.repo, item.work_id)
            if retries:
                envelope["schema_retry"] = retries
            return envelope

    def _schema_retry(self, repo: str, work_id: str) -> dict:
        for provider_id, provider in self._snapshot.providers.items():
            if not provider_id.startswith("workflow:"):
                continue
            observations = provider.observations
            if not isinstance(observations, Mapping):
                continue
            rows = observations.get("schema_retry", {})
            if not isinstance(rows, Mapping):
                continue
            found = rows.get(work_id)
            if isinstance(found, Mapping) and found:
                return {
                    "limit": SCHEMA_RETRY_LIMIT,
                    "by_card": {
                        str(card): int(count)
                        for card, count in sorted(found.items())
                        if isinstance(count, int) and not isinstance(count, bool)
                    },
                }
        return {}

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

    def _envelope(
        self, *, repo: str | None = None, work_id: str | None = None
    ) -> dict:
        providers = []
        for provider_id in sorted(self._snapshot.providers):
            provider = self._snapshot.providers[provider_id]
            row = {"provider_id": provider_id, **provider.to_dict()}
            providers.append(row)
        scoped_providers = [
            row
            for row in providers
            if repo is None or _provider_repo(row["provider_id"]) in {None, repo}
        ]
        scoped_item_reasons = [
            f"work_item:{item.repo}:{item.work_id} degraded"
            for item in self._items.values()
            if repo is None or item.repo == repo
            if work_id is None or item.work_id == work_id
            if "degraded" in item.facets
        ]
        scoped_gates = self._hard_gates(scoped_providers)
        if not scoped_gates["reasons"]:
            scoped_gates["reasons"] = scoped_item_reasons
        scoped_gates["auto_claim"] = not scoped_gates["reasons"]
        scoped_gates["merge"] = not scoped_gates["reasons"]
        fleet_reasons = list(self._hard_gates(providers)["reasons"])
        if not fleet_reasons:
            fleet_reasons.extend(
                f"work_item:{item.repo}:{item.work_id} degraded"
                for item in self._items.values()
                if "degraded" in item.facets
            )
        fleet_reasons = list(dict.fromkeys(fleet_reasons))
        return {
            "schema": WORK_API_SCHEMA,
            "generated_at": self._snapshot.written_at,
            "sequence": self._snapshot.sequence,
            "degraded": bool(scoped_gates["reasons"]),
            "providers": providers,
            "hard_gates": scoped_gates,
            "fleet_health": {
                "degraded": bool(fleet_reasons),
                "reasons": fleet_reasons,
            },
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
        self.socket_path = Path(socket_path) if socket_path is not None else load_config().socket_path
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
        github_pressure_gate: GitHubPressureGate | None = None,
    ) -> None:
        self.durable_store = durable_store
        self.read_store = read_store
        self.github_provider_factory = github_provider_factory or GitHubWorkProvider
        self.github_terminal_provider_factory = (
            github_terminal_provider_factory or GitHubTerminalProvider
        )
        self._uses_default_github_provider = github_provider_factory is None
        self._uses_default_github_terminal_provider = (
            github_terminal_provider_factory is None
        )
        # #506：跨 repo 共用的節流／退避閘門。掃描是 per-repo 建 provider、
        # 但壓力是 per-token 的，所以閘門必須活得比 provider 久（由 refresher
        # 持有），否則節流只在單一 repo 內生效、退避每個 repo 各燒一次 403。
        self.github_pressure_gate = github_pressure_gate
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
            if include_github and self.github_pressure_gate is not None:
                # #506：一輪 GitHub 掃描開始，重置本輪節流預算。
                self.github_pressure_gate.begin_cycle()
            previous = self.read_store.current_snapshot()
            providers = dict(previous.providers)
            active_provider_ids: set[str] = set()
            projected_items: list[WorkItem] = []
            source_owners: dict[str, str] = {}
            exclusions: list[Mapping[str, str]] = []
            explanations: dict[str, Mapping] = {}
            current_time = self.now()
            if current_time.tzinfo is None:
                raise ValueError("now must include timezone")
            attempted_at = current_time.isoformat().replace("+00:00", "Z")
            active_projects = [p for p in sorted(projects, key=lambda item: item.path) if not p.legacy]
            repo_groups: dict[str, list[tuple[ProjectState, Path, bool]]] = {}
            for project in active_projects:
                root = Path(project.path)
                repo, is_github = _repo_identity(root, project.project_id)
                repo_groups.setdefault(repo, []).append((project, root, is_github))

            for repo, group in sorted(repo_groups.items(), key=lambda row: row[0]):
                # Sort so main git repo (.git is dir) comes first, then shortest path
                def _is_canonical(root: Path) -> int:
                    git_dir = root / ".git"
                    return 0 if git_dir.is_dir() else 1

                sorted_group = sorted(
                    group, key=lambda row: (_is_canonical(row[1]), len(row[1].parts), str(row[1]))
                )
                project, root, is_github = sorted_group[0]
                duplicate_roots = [row[1] for row in sorted_group[1:]]

                local_result = RepoWorkProvider(root, repo=repo).scan()
                if duplicate_roots:
                    dup_paths = ", ".join(str(r) for r in duplicate_roots)
                    local_result = replace(
                        local_result,
                        diagnostics=tuple(
                            (
                                *local_result.diagnostics,
                                f"duplicate checkout of repo {repo} at {dup_paths}; ignoring non-canonical checkouts",
                            )
                        ),
                    )
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
                terminal_id = f"github-terminal:{repo}"
                if include_github and is_github:
                    # #506：預設 provider 才注入壓力閘門；注入自訂 factory 的
                    # 呼叫端（測試／上層組裝）行為完全不變。
                    github_provider = (
                        GitHubWorkProvider(
                            repo, pressure_gate=self.github_pressure_gate
                        )
                        if self._uses_default_github_provider
                        else self.github_provider_factory(repo)
                    )
                    github_result = github_provider.scan()
                    github = _retain_last_good(providers.get(github_id), github_result)
                    providers[github_id] = github
                    relevant.append(github)
                    terminal_provider = (
                        GitHubTerminalProvider(
                            repo,
                            relevant_pr_numbers=_workflow_linked_pr_numbers(
                                workflow,
                                repo=repo,
                            ),
                            pressure_gate=self.github_pressure_gate,
                            # D2：remote 檔案內容與 merge ancestry 走本機 git，
                            # 讀的就是這個 repo 在 workspace 的 canonical checkout
                            # （與 ``RepoWorkProvider`` 同一個 root）。
                            repo_root=root,
                        )
                        if self._uses_default_github_terminal_provider
                        else self.github_terminal_provider_factory(repo)
                    )
                    terminal_result = terminal_provider.scan()
                    terminal = _retain_last_good(
                        providers.get(terminal_result.provider_id), terminal_result
                    )
                    providers[terminal.provider_id] = terminal
                    relevant.append(terminal)
                elif github_id in providers:
                    relevant.append(providers[github_id])
                    if terminal_id in providers:
                        relevant.append(providers[terminal_id])
                freshness_time = self.now()
                if freshness_time.tzinfo is None:
                    raise ValueError("now must include timezone")
                freshness_snapshot = WorkSnapshot(
                    sequence=previous.sequence,
                    written_at=attempted_at,
                    providers=providers,
                    work_items=(),
                    source_owners={},
                    exclusions=(),
                )
                relevant_ids = {provider.provider_id for provider in relevant}
                for authority_id in (github_id, terminal_id):
                    fresh = freshness_snapshot.provider_is_fresh(
                        authority_id,
                        now=freshness_time,
                        max_age=self.stale_after_seconds,
                    )
                    if authority_id not in relevant_ids or fresh:
                        continue
                    stale = replace(
                        providers[authority_id],
                        status="degraded",
                        last_attempt_at=attempted_at,
                        diagnostics=tuple(
                            dict.fromkeys(
                                (
                                    *providers[authority_id].diagnostics,
                                    f"{authority_id} stale",
                                )
                            )
                        ),
                    )
                    providers[authority_id] = stale
                    relevant = [
                        stale if provider.provider_id == authority_id else provider
                        for provider in relevant
                    ]
                active_provider_ids.update(
                    provider.provider_id for provider in relevant
                )
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
                        observations, correlation=correlation, repo=repo
                    ),
                )
                projected_items.extend(projection.items)
                explanations.update(
                    (work_key(repo, work_id), explanation)
                    for work_id, explanation in projection.explanations.items()
                )
                source_owners.update(
                    (source_id, work_key(repo, owner))
                    for source_id, owner in correlation.source_owners.items()
                )
                exclusions.extend(correlation.exclusions)
                if correlation.degraded:
                    projected_ids = {
                        work_key(item.repo, item.work_id) for item in projection.items
                    }
                    current_source_ids = {
                        source.source_id for item in projection.items for source in item.sources
                    }
                    for source_id, owner in previous.source_owners.items():
                        if owner in projected_ids and source_id not in current_source_ids and source_id not in source_owners:
                            source_owners[source_id] = owner
                    exclusions.extend(previous.exclusions)
            providers = {
                provider_id: provider
                for provider_id, provider in providers.items()
                if provider_id in active_provider_ids
            }
            try:
                snapshot = WorkSnapshot(
                    sequence=previous.sequence + 1,
                    written_at=attempted_at,
                    providers=providers,
                    work_items=tuple(projected_items),
                    source_owners=source_owners,
                    exclusions=tuple(_dedupe_mappings(exclusions)),
                )
            except ValueError as error:
                # #523：projection 層的 ownership 驗證失敗，原本會讓整個 refresh 拋
                # 例外——而這個例外發生在 `replace_durably()` **之前**，於是那一輪算出
                # 的 provider 新狀態（包含「rate limit backoff 已結束、provider 恢復
                # ok」這個事實）**一併被丟棄**。`previous` 因此永遠停在崩潰前那一版、
                # `correlation.degraded` 永遠為真，下一輪以相同輸入重演同樣的例外：
                # provider 無法離開 degraded，因為記錄它恢復的那次寫入正是拋例外的
                # 那次寫入。實測形成永不自癒的死鎖，且外觀與單純限流難以區分。
                #
                # projection 是衍生資料，provider 觀測是第一手事實——兩者不該同生共死。
                # 因此降級為「保留上一版 projection，但讓新的 provider 觀測落地」，
                # 並把失敗原因寫進 diagnostics 讓 operator 看得見。若連降級版本都建不
                # 起來，那是真正的資料損毀，照常往外拋。
                degraded_providers = {
                    provider_id: replace(
                        provider,
                        status="degraded",
                        diagnostics=tuple(
                            dict.fromkeys(
                                (
                                    *provider.diagnostics,
                                    "work model projection retained: "
                                    f"{type(error).__name__}: {error}",
                                )
                            )
                        ),
                    )
                    for provider_id, provider in providers.items()
                }
                snapshot = WorkSnapshot(
                    sequence=previous.sequence + 1,
                    written_at=attempted_at,
                    providers=degraded_providers,
                    work_items=previous.work_items,
                    source_owners=dict(previous.source_owners),
                    exclusions=tuple(_dedupe_mappings(list(previous.exclusions))),
                )
                return self.read_store.replace_durably(
                    snapshot, self.durable_store.write
                )
            return self.read_store.replace_durably(
                snapshot, self.durable_store.write, explanations=explanations
            )


def _merge_observations(providers: Sequence[ProviderSnapshot]) -> dict:
    merged: dict[str, object] = {
        "closing_links": {},
        "workflow_links": {},
        "closure_by_work": {},
        "validated_completions": {},
        "schema_retry": {},
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
            retries = observations.get("schema_retry", {})
            if isinstance(retries, Mapping):
                for work_id, rows in retries.items():
                    if isinstance(work_id, str) and isinstance(rows, Mapping):
                        merged["schema_retry"].setdefault(work_id, {}).update(rows)
            completions = observations.get("validated_completions", {})
            if isinstance(completions, Mapping):
                for work_id, rows in completions.items():
                    if isinstance(work_id, str) and isinstance(rows, list):
                        merged["validated_completions"].setdefault(work_id, []).extend(
                            row for row in rows if isinstance(row, Mapping)
                        )
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
        work_id = row.get("work_id")
        kind = row.get("kind")
        value = row.get("value")
        if any(
            not isinstance(field, str) or not field
            for field in (work_id, kind, value)
        ):
            continue
        source_ids = row.get("source_ids")
        if (
            not isinstance(source_ids, (list, tuple))
            or not source_ids
            or any(
                not isinstance(source_id, str) or not source_id
                for source_id in source_ids
            )
        ):
            continue
        try:
            parsed.append(
                InferredSignal(
                    work_id=work_id,
                    kind=kind,
                    value=value,
                    source_ids=tuple(source_ids),
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
    closed_github_ids = {
        source.source_id
        for source in sources
        if source.kind in {"github_issue", "github_pr"} and source.status == "closed"
    }
    for source in sources:
        if source.kind == "openspec" and source.status == "archived":
            continue
        if source.source_id in closed_github_ids:
            continue
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
        if source_id in closed_github_ids:
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
    observations: Mapping, *, correlation=None, repo: str
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
    validated_completions = observations.get("validated_completions", {})
    if not isinstance(validated_completions, Mapping):
        validated_completions = {}
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
        completion_rows = validated_completions.get(group.work_id, [])
        if group.work_id not in combined and not completion_rows:
            continue
        combined.setdefault(group.work_id, {})
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
        from paulsha_cortex.coordinator.claim import semantic_source_revision

        authoritative_revisions: dict[str, str] = {}
        for source in group.sources:
            if source.confidence != "confirmed":
                continue
            semantic = semantic_source_revision(
                repo=repo,
                kind=source.kind,
                ref=source.ref,
                source_id=source.source_id,
                revision=source.revision,
                status=source.status,
            )
            if semantic is None:
                continue
            source_id, revision = semantic
            previous = authoritative_revisions.setdefault(source_id, revision)
            if previous != revision:
                authoritative_revisions = {}
                break
        def completion_matches(completion: object) -> bool:
            if not isinstance(completion, Mapping):
                return False
            supplied = completion.get("source_revisions")
            if not isinstance(supplied, Mapping):
                return False
            normalized: dict[str, str] = {}
            for source in group.sources:
                if source.confidence != "confirmed":
                    continue
                semantic = semantic_source_revision(
                    repo=repo,
                    kind=source.kind,
                    ref=source.ref,
                    source_id=source.source_id,
                    revision=source.revision,
                    status=source.status,
                )
                if semantic is None:
                    continue
                source_id, revision = semantic
                supplied_revision = supplied.get(source_id, supplied.get(source.source_id))
                if supplied_revision == source.revision:
                    supplied_revision = revision
                if supplied_revision != revision:
                    return False
                normalized[source_id] = revision
            return normalized == authoritative_revisions and all(
                remote_prs.get(source.source_id, {}).get("candidate")
                == completion.get("pr_candidate")
                and remote_prs.get(source.source_id, {}).get("merge_revision")
                == completion.get("merge_revision")
                for source in prs
            )

        combined[group.work_id]["completion_record_valid"] = bool(
            authoritative_revisions
        ) and isinstance(completion_rows, list) and any(
            completion_matches(completion) for completion in completion_rows
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


def _provider_repo(provider_id: str) -> str | None:
    for prefix in ("github-terminal:", "github:", "workflow:", "repo:"):
        if provider_id.startswith(prefix):
            return provider_id[len(prefix) :]
    return None


# D2：與 ``git_mirror.LocalGitMirror`` 共用同一組 origin 樣式——provider 認定「這是
# GitHub repo」與 mirror 認定「origin 真的指向它」必須用同一個判準，否則會出現
# 「派了 GitHub provider、鏡像卻拒認 origin」的分歧。
_GITHUB_SSH = GITHUB_SSH_REMOTE
_GITHUB_HTTPS = GITHUB_HTTPS_REMOTE


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
