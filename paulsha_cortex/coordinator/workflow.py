from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


DEFAULT_WORKFLOW_COMBO = "feature-oneshot"
WORKFLOW_MANIFEST_VERSION = 1


@dataclass(frozen=True)
class WorkflowStep:
    """Deck card投影出的持久化workflow step契約。"""

    phase: str
    persona: str
    card: str
    executor: str | None
    model: str | None
    domain: str | None
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    gate_result: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowManifest:
    """一次Deck compile的persona-preserving workflow manifest。"""

    combo: str
    task_slug: str
    steps: tuple[WorkflowStep, ...]
    version: int = WORKFLOW_MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
