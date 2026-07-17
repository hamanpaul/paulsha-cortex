from __future__ import annotations

import time
import threading
import stat
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable

from .config import MonitorConfig
from .fs import checked_stat_mode, stable_path
from .models import ProjectState, Signal
from .parser import extract_project_state
from .scanner import (
    ProjectClassification,
    classify_project_detailed,
    scan_workspaces_detailed,
)


@dataclass(frozen=True)
class ChangeEvent:
    project_id: str
    sequence: int
    project_state: ProjectState
    removed: bool = False


def _project_signature(state: ProjectState) -> tuple:
    """Stable, comparable shape used to detect "anything changed" for a project.

    We exclude only `last_seen_at`; diagnostics like `source_signals` are part
    of the observable monitor state and must propagate to subscribers.
    """
    payload = asdict(state)
    payload.pop("last_seen_at", None)
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
            for state in scan_workspaces_detailed(self._config).states:
                self._states[state.project_id] = state
                self._signatures[state.project_id] = _project_signature(state)
        return ()

    def refresh(self) -> tuple[ChangeEvent, ...]:
        """Re-scan all workspaces; emit one event per changed project."""
        with self._lock:
            previous_states = self._states
            previous_signatures = self._signatures
            result = scan_workspaces_detailed(self._config)
            previous_ids_by_path = {
                str(stable_path(Path(state.path))): project_id
                for project_id, state in previous_states.items()
            }
            new_states: dict[str, ProjectState] = {}
            for scanned in result.states:
                stable_id = previous_ids_by_path.get(
                    str(stable_path(Path(scanned.path))),
                    scanned.project_id,
                )
                state = replace(scanned, project_id=stable_id)
                if stable_id in new_states and new_states[stable_id].path != state.path:
                    raise ValueError(f"duplicate project_id after scan: {stable_id}")
                new_states[stable_id] = state
            for project_id, previous in previous_states.items():
                if project_id in new_states:
                    continue
                path_degraded = _is_under_degraded_root(
                    Path(previous.path),
                    result.degraded_roots,
                )
                workspace_degraded = previous.workspace in result.degraded_workspaces
                if not path_degraded and not workspace_degraded:
                    continue
                degraded_root = next(
                    (
                        root
                        for root in result.degraded_roots
                        if _is_under_degraded_root(Path(previous.path), (root,))
                    ),
                    None,
                )
                note = (
                    f"degraded: scan unavailable under {degraded_root}"
                    if degraded_root is not None
                    else f"degraded: workspace unavailable: {previous.workspace}"
                )
                new_states[project_id] = _with_scan_degraded_signal(
                    previous,
                    (note,),
                )
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
                project_mode, project_error = checked_stat_mode(project_dir)
                if (
                    project_error is not None
                    or project_mode is None
                    or not stat.S_ISDIR(project_mode)
                ):
                    state = _with_scan_degraded_signal(
                        current,
                        (
                            project_error
                            or f"degraded: project unavailable: {project_dir}",
                        ),
                    )
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
                    continue
                classification, classification_error = classify_project_detailed(project_dir)
                if classification_error is not None:
                    state = _with_scan_degraded_signal(current, (classification_error,))
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
                    continue
                if classification == ProjectClassification.LEGACY:
                    if self._config.legacy_policy == "hide":
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
                    state = ProjectState(
                        project_id=current.project_id,
                        workspace=current.workspace,
                        path=str(project_dir),
                        legacy=True,
                        last_seen_at=time.time(),
                    )
                else:
                    state = extract_project_state(project_dir, workspace_name=current.workspace)
                    state = replace(state, project_id=current.project_id)
                    degraded_notes = tuple(
                        signal.note
                        for signal in state.source_signals
                        if signal.note and signal.note.startswith("degraded:")
                    )
                    if degraded_notes:
                        state = _with_scan_degraded_signal(current, degraded_notes)
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


def _is_under_degraded_root(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = stable_path(path)
    for root in roots:
        try:
            resolved.relative_to(stable_path(root))
        except ValueError:
            continue
        return True
    return False


def _with_scan_degraded_signal(
    state: ProjectState,
    diagnostics: tuple[str, ...],
) -> ProjectState:
    retained = tuple(signal for signal in state.source_signals if signal.kind != "scan")
    note = diagnostics[0] if diagnostics else "degraded: scan unavailable"
    return replace(
        state,
        source_signals=retained + (Signal(kind="scan", path=state.path, note=note),),
    )
