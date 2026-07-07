from __future__ import annotations

import time
from enum import Enum
from pathlib import Path

from .config import MonitorConfig
from .models import ProjectState
from .parser import extract_project_state
from .registry import ProjectEntry, merge_projects

# Always-skip directory names that should never be treated as projects.
IMPLICIT_IGNORE = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__"})


class ProjectClassification(str, Enum):
    TRACKED = "tracked"
    LEGACY = "legacy"


def classify_project(project_dir: Path) -> ProjectClassification:
    """Decide whether a project dir is tracked or legacy (design §3.2)."""
    if (project_dir / ".paul-project.yml").is_file():
        return ProjectClassification.TRACKED

    workstreams = project_dir / "docs" / "superpowers" / "workstreams"
    if workstreams.is_dir():
        for child in workstreams.iterdir():
            if not child.is_dir() or not child.name.startswith("stage"):
                continue
            if (child / "todo.md").is_file() or (child / "task.md").is_file():
                return ProjectClassification.TRACKED

    return ProjectClassification.LEGACY


def _list_project_dirs(workspace_root: Path, ignore_dirs: frozenset[str]) -> list[Path]:
    if not workspace_root.exists():
        return []
    if not workspace_root.is_dir():
        return []
    items: list[Path] = []
    for entry in sorted(workspace_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in ignore_dirs:
            continue
        if entry.name in IMPLICIT_IGNORE:
            continue
        items.append(entry)
    return items


def scan_workspaces(config: MonitorConfig) -> tuple[ProjectState, ...]:
    """Walk every configured workspace and produce per-project states.

    Honours `legacy_policy` and `ignore_dirs` per design §3.2 / §3.5.
    Missing workspace paths are silently skipped — the service must not
    crash when a workspace is unmounted (spec §B7).
    """
    ignore = frozenset(config.ignore_dirs)
    legacy_visible = config.legacy_policy != "hide"
    now = time.time()
    manual_projects: list[ProjectEntry] = []
    states: list[ProjectState] = []

    for workspace in config.workspaces:
        for project_dir in _list_project_dirs(workspace.path, ignore):
            manual_projects.append(
                ProjectEntry(
                    path=project_dir.resolve(),
                    name=workspace.name,
                    source="manual",
                )
            )

    for entry in merge_projects(manual_projects, list(config.hippo_projects)):
        project_dir = entry.path
        if not project_dir.exists() or not project_dir.is_dir():
            continue
        classification = classify_project(project_dir)
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
            states.append(state)

    return tuple(states)
