from __future__ import annotations

from dataclasses import dataclass, replace
import time
from enum import Enum
from pathlib import Path
import stat

from .config import MonitorConfig
from .fs import checked_lstat_mode, checked_resolve, checked_stat_mode, stable_path
from .models import ProjectState
from .parser import extract_project_state
from .registry import ProjectEntry, merge_projects

# Always-skip directory names that should never be treated as projects.
IMPLICIT_IGNORE = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__"})


class ProjectClassification(str, Enum):
    TRACKED = "tracked"
    LEGACY = "legacy"


@dataclass(frozen=True)
class DegradedDiagnostic:
    root: Path
    workspace: str
    note: str


@dataclass(frozen=True)
class ScanResult:
    """One monitor scan plus the roots that could not be read authoritatively."""

    states: tuple[ProjectState, ...]
    degraded_roots: tuple[Path, ...] = ()
    degraded_workspaces: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    degraded_diagnostics: tuple[DegradedDiagnostic, ...] = ()


def classify_project_detailed(
    project_dir: Path,
) -> tuple[ProjectClassification, str | None]:
    policy_mode, error = checked_stat_mode(project_dir / ".paul-project.yml")
    if error is not None:
        return ProjectClassification.LEGACY, error
    if policy_mode is not None and stat.S_ISREG(policy_mode):
        return ProjectClassification.TRACKED, None

    workstreams = project_dir / "docs" / "superpowers" / "workstreams"
    workstreams_mode, error = checked_stat_mode(workstreams)
    if error is not None:
        return ProjectClassification.LEGACY, error
    if workstreams_mode is not None and stat.S_ISDIR(workstreams_mode):
        try:
            for child in workstreams.iterdir():
                child_mode, error = checked_stat_mode(child)
                if error is not None:
                    return ProjectClassification.LEGACY, error
                if child_mode is None or not stat.S_ISDIR(child_mode):
                    continue
                if not child.name.startswith("stage"):
                    continue
                todo_mode, error = checked_stat_mode(child / "todo.md")
                if error is not None:
                    return ProjectClassification.LEGACY, error
                task_mode, error = checked_stat_mode(child / "task.md")
                if error is not None:
                    return ProjectClassification.LEGACY, error
                if (
                    todo_mode is not None
                    and stat.S_ISREG(todo_mode)
                    or task_mode is not None
                    and stat.S_ISREG(task_mode)
                ):
                    return ProjectClassification.TRACKED, None
        except OSError as error:
            return ProjectClassification.LEGACY, f"degraded: {type(error).__name__}: {error}"

    return ProjectClassification.LEGACY, None


def classify_project(project_dir: Path) -> ProjectClassification:
    """Decide whether a project dir is tracked or legacy (design §3.2)."""
    classification, _error = classify_project_detailed(project_dir)
    return classification


def _list_project_dirs_checked(
    workspace_root: Path,
    ignore_dirs: frozenset[str],
) -> tuple[list[Path], str | None]:
    root_mode, error = checked_stat_mode(workspace_root)
    if error is not None:
        return [], error
    if root_mode is None:
        return [], f"degraded: workspace unavailable: {workspace_root}"
    if not stat.S_ISDIR(root_mode):
        return [], f"degraded: workspace is not a directory: {workspace_root}"
    try:
        entries = sorted(workspace_root.iterdir())
    except OSError as error:
        return [], f"degraded: {type(error).__name__}: {error}"
    items: list[Path] = []
    for entry in entries:
        if entry.name in ignore_dirs or entry.name in IMPLICIT_IGNORE:
            continue
        entry_mode, error = checked_stat_mode(entry)
        if error is not None:
            return [], error
        if entry_mode is None:
            lstat_mode, lstat_error = checked_lstat_mode(entry)
            if lstat_error is not None:
                return [], lstat_error
            if lstat_mode is None:
                return [], f"degraded: project vanished during scan: {entry}"
            if stat.S_ISLNK(lstat_mode):
                # Keep the unresolved target locator. If this symlink was a
                # previously healthy project, SnapshotStore can retain that
                # exact last-good path without freezing unrelated projects.
                items.append(entry)
            # A non-project entry is not a transient workspace failure.
            continue
        if not stat.S_ISDIR(entry_mode):
            continue
        items.append(entry)
    return items, None


def _list_project_dirs(workspace_root: Path, ignore_dirs: frozenset[str]) -> list[Path]:
    """Compatibility wrapper for callers that only need the visible entries."""
    items, _error = _list_project_dirs_checked(workspace_root, ignore_dirs)
    return items


def _qualified_duplicate_project_id(states: list[ProjectState], state: ProjectState) -> str:
    workspace_qualified = {
        other.workspace: f"{other.workspace}:{Path(other.path).name}"
        for other in states
    }
    if len(workspace_qualified) == len(states):
        return workspace_qualified[state.workspace]
    return f"{state.workspace}:{stable_path(Path(state.path))}"


def _dedupe_project_ids(states: list[ProjectState]) -> tuple[ProjectState, ...]:
    grouped: dict[str, list[ProjectState]] = {}
    for state in states:
        grouped.setdefault(state.project_id, []).append(state)
    resolved: list[ProjectState] = []
    for state in states:
        group = grouped[state.project_id]
        if len(group) == 1:
            resolved.append(state)
            continue
        resolved.append(replace(state, project_id=_qualified_duplicate_project_id(group, state)))
    return tuple(resolved)


def scan_workspaces_detailed(config: MonitorConfig) -> ScanResult:
    """Walk workspaces without treating unavailable roots as authoritative emptiness.

    Honours `legacy_policy` and `ignore_dirs` per design §3.2 / §3.5.
    Unavailable workspace paths emit degraded diagnostics so the service can
    retain last-good state without crashing when a workspace is unmounted
    (spec §B7).
    """
    ignore = frozenset(config.ignore_dirs)
    legacy_visible = config.legacy_policy != "hide"
    now = time.time()
    manual_projects: list[ProjectEntry] = []
    states: list[ProjectState] = []
    degraded_roots: list[Path] = []
    degraded_workspaces: list[str] = []
    diagnostics: list[str] = []
    degraded_diagnostics: list[DegradedDiagnostic] = []

    for workspace in config.workspaces:
        project_dirs, error = _list_project_dirs_checked(workspace.path, ignore)
        if error is not None:
            degraded_roots.append(workspace.path)
            degraded_workspaces.append(workspace.name)
            diagnostics.append(error)
            degraded_diagnostics.append(
                DegradedDiagnostic(workspace.path, workspace.name, error)
            )
            continue
        for project_dir in project_dirs:
            resolved_project, resolve_error = checked_resolve(project_dir)
            if resolve_error is not None:
                degraded_roots.append(workspace.path)
                degraded_workspaces.append(workspace.name)
                diagnostics.append(resolve_error)
                degraded_diagnostics.append(
                    DegradedDiagnostic(workspace.path, workspace.name, resolve_error)
                )
                continue
            manual_projects.append(
                ProjectEntry(
                    path=resolved_project,
                    name=workspace.name,
                    source="manual",
                )
            )

    for entry in merge_projects(manual_projects, list(config.hippo_projects)):
        project_dir = entry.path
        project_mode, availability_error = checked_stat_mode(project_dir)
        if availability_error is not None:
            diagnostics.append(availability_error)
            degraded_diagnostics.append(
                DegradedDiagnostic(project_dir, entry.name, availability_error)
            )
        if project_mode is None or not stat.S_ISDIR(project_mode):
            degraded_roots.append(project_dir)
            if not any(str(project_dir) in item for item in diagnostics):
                diagnostic = f"degraded: project unavailable: {project_dir}"
                diagnostics.append(diagnostic)
                degraded_diagnostics.append(
                    DegradedDiagnostic(project_dir, entry.name, diagnostic)
                )
            continue
        classification, classification_error = classify_project_detailed(project_dir)
        if classification_error is not None:
            degraded_roots.append(project_dir)
            diagnostics.append(classification_error)
            degraded_diagnostics.append(
                DegradedDiagnostic(project_dir, entry.name, classification_error)
            )
            continue
        is_legacy = classification == ProjectClassification.LEGACY
        if is_legacy and not legacy_visible:
            continue
        if is_legacy:
            states.append(
                ProjectState(
                    project_id=project_dir.name,
                    workspace=entry.name,
                    path=str(project_dir),
                    legacy=True,
                    last_seen_at=now,
                )
            )
        else:
            state = extract_project_state(
                project_dir,
                workspace_name=entry.name,
            )
            degraded = tuple(
                signal.note
                for signal in state.source_signals
                if signal.note and signal.note.startswith("degraded:")
            )
            if degraded:
                degraded_roots.append(project_dir)
                diagnostics.extend(degraded)
                degraded_diagnostics.extend(
                    DegradedDiagnostic(project_dir, entry.name, diagnostic)
                    for diagnostic in degraded
                )
                continue
            states.append(state)

    return ScanResult(
        states=_dedupe_project_ids(states),
        degraded_roots=tuple(dict.fromkeys(Path(root) for root in degraded_roots)),
        degraded_workspaces=tuple(dict.fromkeys(degraded_workspaces)),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
        degraded_diagnostics=tuple(dict.fromkeys(degraded_diagnostics)),
    )


def scan_workspaces(config: MonitorConfig) -> tuple[ProjectState, ...]:
    """Compatibility projection of the detailed scan result."""
    return scan_workspaces_detailed(config).states
