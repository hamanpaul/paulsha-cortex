from __future__ import annotations

import re
import time
from pathlib import Path

from .models import ProjectState, Signal, StageRef, StageView, TaskRef

UNCHECKED_BULLET = re.compile(r"^\s*-\s*\[\s\]\s+(.+?)\s*$")
CHECKED_BULLET = re.compile(r"^\s*-\s*\[[xX]\]\s+(.+?)\s*$")
SECTION_HEADER = re.compile(r"^\s*##\s+(.+?)\s*$")


def _read_text_safe(path: Path) -> tuple[str | None, str | None]:
    """Return (text, error). Reading errors → (None, reason)."""
    try:
        return path.read_text(encoding="utf-8"), None
    except (UnicodeDecodeError, OSError) as error:
        return None, f"degraded: {type(error).__name__}: {error}"


def _iter_section(text: str, section_title: str) -> list[tuple[int, str]]:
    """Return [(line_no, line_text)] for every line in the requested section."""
    lines = text.splitlines()
    in_section = False
    collected: list[tuple[int, str]] = []
    for idx, raw in enumerate(lines, start=1):
        header_match = SECTION_HEADER.match(raw)
        if header_match:
            current = header_match.group(1).strip()
            in_section = current.lower() == section_title.lower()
            continue
        if in_section:
            collected.append((idx, raw))
    return collected


def parse_todo_current_sprint(todo_md: Path) -> tuple[TaskRef, ...]:
    """Return open (`- [ ]`) checkbox items in the Current Sprint section."""
    text, error = _read_text_safe(todo_md)
    if error or text is None:
        return ()
    items: list[TaskRef] = []
    for line_no, line in _iter_section(text, "Current Sprint"):
        match = UNCHECKED_BULLET.match(line)
        if match:
            items.append(TaskRef(text=match.group(1).strip(), line_no=line_no))
    return tuple(items)


def parse_blockers(todo_md: Path) -> tuple[str, ...]:
    """Return every checkbox-line under the Blockers section, regardless of state."""
    text, error = _read_text_safe(todo_md)
    if error or text is None:
        return ()
    items: list[str] = []
    for _line_no, line in _iter_section(text, "Blockers"):
        match = UNCHECKED_BULLET.match(line) or CHECKED_BULLET.match(line)
        if match:
            items.append(match.group(1).strip())
    return tuple(items)


def _list_workstream_dirs(project_dir: Path) -> list[Path]:
    workstreams_root = project_dir / "docs" / "superpowers" / "workstreams"
    if not workstreams_root.is_dir():
        return []
    return sorted(
        child
        for child in workstreams_root.iterdir()
        if child.is_dir() and child.name.startswith("stage")
    )


def _make_view(workstream_dir: Path) -> tuple[StageView | None, list[Signal]]:
    """Build a StageView for an in-progress stage, plus diagnostic signals."""
    todo_md = workstream_dir / "todo.md"
    signals: list[Signal] = []

    if not todo_md.is_file():
        return None, signals

    text, error = _read_text_safe(todo_md)
    if error:
        signals.append(Signal(kind="todo", path=str(todo_md), note=error))
        return (
            StageView(
                stage_id=workstream_dir.name,
                workstream_path=str(workstream_dir),
                processing_task=None,
                next_task=None,
                blockers=(),
                degraded=True,
            ),
            signals,
        )

    open_items = parse_todo_current_sprint(todo_md)
    blockers = parse_blockers(todo_md)
    signals.append(Signal(kind="todo", path=str(todo_md)))

    if not open_items:
        return None, signals

    processing = open_items[0]
    nxt = open_items[1] if len(open_items) > 1 else None

    return (
        StageView(
            stage_id=workstream_dir.name,
            workstream_path=str(workstream_dir),
            processing_task=processing,
            next_task=nxt,
            blockers=blockers,
        ),
        signals,
    )


def extract_project_state(project_dir: Path, *, workspace_name: str) -> ProjectState:
    """Derive ProjectState from a tracked project's artifacts.

    Single source of truth: this function only reads files; it never persists
    parallel state (spec §B4).
    """
    in_progress: list[StageView] = []
    pending: list[StageRef] = []
    signals: list[Signal] = []

    for workstream_dir in _list_workstream_dirs(project_dir):
        view, view_signals = _make_view(workstream_dir)
        signals.extend(view_signals)
        if view is not None:
            in_progress.append(view)
        else:
            pending.append(
                StageRef(
                    stage_id=workstream_dir.name,
                    workstream_path=str(workstream_dir),
                )
            )

    # Completed-stage detection from openspec/changes/archive/* — best-effort,
    # purely additive. (Branch-state inspection deferred to GitInspector in a
    # later patch; spec §B3 + design §4 decision #5 keep it pluggable.)
    completed: list[StageRef] = []
    archive_root = project_dir / "openspec" / "changes" / "archive"
    if archive_root.is_dir():
        for entry in sorted(archive_root.iterdir()):
            if entry.is_dir() and "stage" in entry.name:
                completed.append(
                    StageRef(stage_id=entry.name, workstream_path=str(entry))
                )
        signals.append(Signal(kind="archive", path=str(archive_root)))

    return ProjectState(
        project_id=project_dir.name,
        workspace=workspace_name,
        path=str(project_dir),
        completed_stages=tuple(completed),
        in_progress_stages=tuple(in_progress),
        pending_stages=tuple(pending),
        legacy=False,
        last_seen_at=time.time(),
        source_signals=tuple(signals),
    )
