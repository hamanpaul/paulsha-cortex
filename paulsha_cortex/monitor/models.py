from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TaskRef:
    text: str
    line_no: int


@dataclass(frozen=True)
class StageRef:
    stage_id: str
    workstream_path: str | None = None


@dataclass(frozen=True)
class StageView:
    stage_id: str
    workstream_path: str
    processing_task: TaskRef | None
    next_task: TaskRef | None
    blockers: tuple[str, ...] = ()
    degraded: bool = False


@dataclass(frozen=True)
class Signal:
    kind: str
    path: str
    note: str | None = None


@dataclass(frozen=True)
class ProjectState:
    project_id: str
    workspace: str
    path: str
    completed_stages: tuple[StageRef, ...] = ()
    in_progress_stages: tuple[StageView, ...] = ()
    pending_stages: tuple[StageRef, ...] = ()
    legacy: bool = False
    last_seen_at: float = 0.0
    source_signals: tuple[Signal, ...] = ()
