from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable

from .config import MonitorConfig
from .models import ProjectState
from .parser import extract_project_state
from .scanner import scan_workspaces


@dataclass(frozen=True)
class ChangeEvent:
    project_id: str
    sequence: int
    project_state: ProjectState
    removed: bool = False


def _project_signature(state: ProjectState) -> tuple:
    """Stable, comparable shape used to detect "anything changed" for a project.

    We exclude `last_seen_at` and `source_signals` because they tick on every
    refresh even when nothing meaningful changed.
    """
    payload = asdict(state)
    payload.pop("last_seen_at", None)
    payload.pop("source_signals", None)
    return _hashable(payload)


def _hashable(value):
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(v) for v in value)
    return value


class SnapshotStore:
    """In-memory truth for project states + diff + sequence emission.

    Thread-safe; intended to be shared between the scanner loop, the watcher
    callback, and the socket server.
    """

    def __init__(self, *, config: MonitorConfig) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._states: dict[str, ProjectState] = {}
        self._signatures: dict[str, tuple] = {}
        self._sequence = 0

    def load(self) -> tuple[ChangeEvent, ...]:
        """Initial population. Returns no events; consumers fetch the
        snapshot directly via `current_snapshot()` to bootstrap."""
        with self._lock:
            self._states.clear()
            self._signatures.clear()
            for state in scan_workspaces(self._config):
                self._states[state.project_id] = state
                self._signatures[state.project_id] = _project_signature(state)
        return ()

    def refresh(self) -> tuple[ChangeEvent, ...]:
        """Re-scan all workspaces; emit one event per changed project."""
        with self._lock:
            previous_states = self._states
            previous_signatures = self._signatures
            new_states = {s.project_id: s for s in scan_workspaces(self._config)}
            new_signatures = {
                project_id: _project_signature(state)
                for project_id, state in new_states.items()
            }
            events: list[ChangeEvent] = []
            for project_id in sorted(previous_states.keys() - new_states.keys()):
                self._sequence += 1
                events.append(
                    ChangeEvent(
                        project_id=project_id,
                        sequence=self._sequence,
                        project_state=previous_states[project_id],
                        removed=True,
                    )
                )
            for project_id, state in new_states.items():
                signature = new_signatures[project_id]
                if previous_signatures.get(project_id) != signature:
                    self._sequence += 1
                    events.append(
                        ChangeEvent(
                            project_id=project_id,
                            sequence=self._sequence,
                            project_state=state,
                        )
                    )
            self._states = new_states
            self._signatures = new_signatures
            return tuple(events)

    def refresh_projects(self, project_ids: Iterable[str]) -> tuple[ChangeEvent, ...]:
        with self._lock:
            events: list[ChangeEvent] = []
            for project_id in project_ids:
                current = self._states.get(project_id)
                if current is None or current.legacy:
                    continue
                project_dir = Path(current.path)
                if not project_dir.is_dir():
                    self._states.pop(project_id, None)
                    self._signatures.pop(project_id, None)
                    self._sequence += 1
                    events.append(
                        ChangeEvent(
                            project_id=project_id,
                            sequence=self._sequence,
                            project_state=current,
                            removed=True,
                        )
                    )
                    continue
                state = extract_project_state(project_dir, workspace_name=current.workspace)
                state = replace(state, project_id=current.project_id)
                signature = _project_signature(state)
                self._states[project_id] = state
                if self._signatures.get(project_id) == signature:
                    continue
                self._signatures[project_id] = signature
                self._sequence += 1
                events.append(
                    ChangeEvent(
                        project_id=project_id,
                        sequence=self._sequence,
                        project_state=state,
                    )
                )
            return tuple(events)

    def refresh_project(self, project_id: str) -> ChangeEvent | None:
        """Re-scan a single project (used after a debounced watcher fire)."""
        events = self.refresh_projects((project_id,))
        for evt in events:
            if evt.project_id == project_id:
                return evt
        return None

    def current_snapshot(self) -> tuple[ProjectState, ...]:
        with self._lock:
            return tuple(self._states.values())

    def get(self, project_id: str) -> ProjectState | None:
        with self._lock:
            return self._states.get(project_id)

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence
