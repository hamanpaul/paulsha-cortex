from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


DEFAULT_WORKFLOW_COMBO = "feature-oneshot"
WORKFLOW_MANIFEST_VERSION = 1
WORKFLOW_PHASES = ("queued", "research", "define", "plan", "build", "verify", "review", "ship")
WORKFLOW_GATE_STATUSES = frozenset({"pending", "running", "passed", "failed"})
WORKFLOW_FACETS = frozenset({"needs_human", "blocked", "degraded"})
STEP_GATE_RESULTS = frozenset({"pending", "running", "passed", "failed", "needs_human", "blocked", "skipped"})


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

    def __post_init__(self) -> None:
        if self.phase not in WORKFLOW_PHASES:
            raise ValueError(f"workflow step phase 非法: {self.phase!r}")
        for field, value in (("persona", self.persona), ("card", self.card)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"workflow step {field} 必須為非空字串")
        for field, value in (("executor", self.executor), ("model", self.model), ("domain", self.domain)):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"workflow step {field} 必須為null或非空字串")
        for field, value in (("inputs", self.inputs), ("outputs", self.outputs)):
            if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"workflow step {field} 必須為字串tuple")
        if self.gate_result not in STEP_GATE_RESULTS:
            raise ValueError(f"workflow step gate_result 非法: {self.gate_result!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "persona": self.persona,
            "card": self.card,
            "executor": self.executor,
            "model": self.model,
            "domain": self.domain,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "gate_result": self.gate_result,
        }

    @classmethod
    def from_dict(cls, payload: object) -> WorkflowStep:
        if not isinstance(payload, dict):
            raise ValueError("workflow step 格式錯誤")
        required = {
            "phase",
            "persona",
            "card",
            "executor",
            "model",
            "domain",
            "inputs",
            "outputs",
            "gate_result",
        }
        if not required.issubset(payload):
            raise ValueError("workflow step 缺必要欄位")
        inputs = payload["inputs"]
        outputs = payload["outputs"]
        if not isinstance(inputs, list) or not isinstance(outputs, list):
            raise ValueError("workflow step inputs/outputs 格式錯誤")
        return cls(
            phase=payload["phase"],
            persona=payload["persona"],
            card=payload["card"],
            executor=payload["executor"],
            model=payload["model"],
            domain=payload["domain"],
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            gate_result=payload["gate_result"],
        )


@dataclass(frozen=True)
class WorkflowManifest:
    """一次Deck compile的persona-preserving workflow manifest。"""

    combo: str
    task_slug: str
    steps: tuple[WorkflowStep, ...]
    version: int = WORKFLOW_MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowRun:
    """Manager-owned workflow aggregate persisted by coordinator registry v2."""

    run_id: str
    work_id: str
    repo: str
    claim_key: str
    combo: str
    current_phase: str
    steps: tuple[WorkflowStep, ...]
    issue_refs: tuple[str, ...]
    openspec_refs: tuple[str, ...]
    pr_refs: tuple[str, ...]
    attempts: dict[str, int]
    evidence_refs: tuple[str, ...]
    facets: tuple[str, ...]
    gate_status: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        for field, value in (
            ("run_id", self.run_id),
            ("work_id", self.work_id),
            ("repo", self.repo),
            ("claim_key", self.claim_key),
            ("combo", self.combo),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"workflow run {field} 必須為非空字串")
        if self.current_phase not in WORKFLOW_PHASES:
            raise ValueError(f"workflow run current_phase 非法: {self.current_phase!r}")
        if not isinstance(self.steps, tuple) or any(not isinstance(step, WorkflowStep) for step in self.steps):
            raise ValueError("workflow run steps 格式錯誤")
        for field, value in (
            ("issue_refs", self.issue_refs),
            ("openspec_refs", self.openspec_refs),
            ("pr_refs", self.pr_refs),
            ("evidence_refs", self.evidence_refs),
        ):
            if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"workflow run {field} 必須為字串tuple")
        if not isinstance(self.attempts, dict) or any(
            not isinstance(key, str) or not key or not isinstance(value, int) or value < 0
            for key, value in self.attempts.items()
        ):
            raise ValueError("workflow run attempts 格式錯誤")
        if (
            not isinstance(self.facets, tuple)
            or len(set(self.facets)) != len(self.facets)
            or any(facet not in WORKFLOW_FACETS for facet in self.facets)
        ):
            raise ValueError("workflow run facets 格式錯誤")
        if self.gate_status not in WORKFLOW_GATE_STATUSES:
            raise ValueError(f"workflow run gate_status 非法: {self.gate_status!r}")
        for field, value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (AttributeError, ValueError) as exc:
                raise ValueError(f"workflow run {field} 必須為ISO8601") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "work_id": self.work_id,
            "repo": self.repo,
            "claim_key": self.claim_key,
            "combo": self.combo,
            "current_phase": self.current_phase,
            "steps": [step.to_dict() for step in self.steps],
            "issue_refs": list(self.issue_refs),
            "openspec_refs": list(self.openspec_refs),
            "pr_refs": list(self.pr_refs),
            "attempts": dict(self.attempts),
            "evidence_refs": list(self.evidence_refs),
            "facets": list(self.facets),
            "gate_status": self.gate_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> WorkflowRun:
        if not isinstance(payload, dict):
            raise ValueError("workflow run 格式錯誤")
        required = {
            "run_id",
            "work_id",
            "repo",
            "claim_key",
            "combo",
            "current_phase",
            "steps",
            "issue_refs",
            "openspec_refs",
            "pr_refs",
            "attempts",
            "evidence_refs",
            "facets",
            "gate_status",
            "created_at",
            "updated_at",
        }
        if not required.issubset(payload):
            raise ValueError("workflow run 缺必要欄位")
        list_fields = ("steps", "issue_refs", "openspec_refs", "pr_refs", "evidence_refs", "facets")
        if any(not isinstance(payload[field], list) for field in list_fields):
            raise ValueError("workflow run list欄位格式錯誤")
        return cls(
            run_id=payload["run_id"],
            work_id=payload["work_id"],
            repo=payload["repo"],
            claim_key=payload["claim_key"],
            combo=payload["combo"],
            current_phase=payload["current_phase"],
            steps=tuple(WorkflowStep.from_dict(step) for step in payload["steps"]),
            issue_refs=tuple(payload["issue_refs"]),
            openspec_refs=tuple(payload["openspec_refs"]),
            pr_refs=tuple(payload["pr_refs"]),
            attempts=payload["attempts"],
            evidence_refs=tuple(payload["evidence_refs"]),
            facets=tuple(payload["facets"]),
            gate_status=payload["gate_status"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
        )


def validate_workflow_phase_transition(current: str, new: str) -> None:
    if current not in WORKFLOW_PHASES or new not in WORKFLOW_PHASES:
        raise ValueError(f"非法 workflow phase transition: {current!r} -> {new!r}")
    if current == new:
        return
    current_index = WORKFLOW_PHASES.index(current)
    new_index = WORKFLOW_PHASES.index(new)
    if current != "queued" and new_index <= current_index:
        raise ValueError(f"非法 workflow phase transition: {current!r} -> {new!r}")
