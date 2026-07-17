from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_WORKFLOW_COMBO = "feature-oneshot"
WORKFLOW_MANIFEST_VERSION = 1
WORKFLOW_PHASES = ("claim", "define", "plan", "build", "verify", "review", "ship")
WORKFLOW_GATE_STATUSES = frozenset({"pending", "running", "passed", "failed"})
WORKFLOW_FACETS = frozenset({"needs_human", "blocked", "degraded"})
STEP_GATE_RESULTS = frozenset({"pending", "running", "passed", "failed", "needs_human", "blocked", "skipped"})


@dataclass(frozen=True)
class PlanningArtifactAuthority:
    """Scan-time ownership and CAS baseline for one canonical planning artifact."""

    ref: str
    kind: str
    work_id: str
    baseline_sha256: str

    def __post_init__(self) -> None:
        path = Path(self.ref)
        if (
            not isinstance(self.ref, str)
            or not self.ref
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != self.ref
        ):
            raise ValueError("planning authority ref 必須為canonical repo-relative path")
        if self.kind not in {"spec", "design", "plan"}:
            raise ValueError("planning authority kind 非法")
        if not isinstance(self.work_id, str) or not self.work_id:
            raise ValueError("planning authority work_id 必須為非空字串")
        if (
            not isinstance(self.baseline_sha256, str)
            or len(self.baseline_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.baseline_sha256)
        ):
            raise ValueError("planning authority baseline_sha256 格式錯誤")

    def to_dict(self) -> dict[str, str]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "work_id": self.work_id,
            "baseline_sha256": self.baseline_sha256,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "PlanningArtifactAuthority":
        if not isinstance(payload, dict) or set(payload) != {
            "ref", "kind", "work_id", "baseline_sha256"
        }:
            raise ValueError("planning authority 格式錯誤")
        return cls(
            ref=payload["ref"],
            kind=payload["kind"],
            work_id=payload["work_id"],
            baseline_sha256=payload["baseline_sha256"],
        )


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
class GateEvidenceRef:
    """A typed, immutable locator for one independent workflow gate."""

    kind: str
    ref: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"brainstorm", "foreign-review", "copilot"}:
            raise ValueError(f"workflow gate evidence kind 非法: {self.kind!r}")
        if not isinstance(self.ref, str) or not self.ref.strip():
            raise ValueError("workflow gate evidence ref 必須為非空字串")
        if self.sha256 is not None and (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.sha256)
        ):
            raise ValueError("workflow gate evidence sha256 格式錯誤")

    def to_dict(self) -> dict[str, str]:
        payload = {"kind": self.kind, "ref": self.ref}
        if self.sha256 is not None:
            payload["sha256"] = self.sha256
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> GateEvidenceRef:
        if not isinstance(payload, dict) or not {"kind", "ref"}.issubset(payload) or set(payload) - {"kind", "ref", "sha256"}:
            raise ValueError("workflow gate evidence 格式錯誤")
        return cls(kind=payload["kind"], ref=payload["ref"], sha256=payload.get("sha256"))


@dataclass(frozen=True)
class WorkflowManifest:
    """一次Deck compile的persona-preserving workflow manifest。"""

    combo: str
    task_slug: str
    steps: tuple[WorkflowStep, ...]
    version: int = WORKFLOW_MANIFEST_VERSION

    def __post_init__(self) -> None:
        if self.version != WORKFLOW_MANIFEST_VERSION:
            raise ValueError(f"workflow manifest version 非法: {self.version!r}")
        for field, value in (("combo", self.combo), ("task_slug", self.task_slug)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"workflow manifest {field} 必須為非空字串")
        if not self.steps:
            raise ValueError("workflow manifest steps 不可為空")

    def validate_manager_spine(self) -> None:
        """Validate the stricter ordering required before Manager can claim it."""
        phase_indexes = [WORKFLOW_PHASES.index(step.phase) for step in self.steps]
        if phase_indexes != sorted(phase_indexes):
            raise ValueError("workflow manifest phases 必須依生命週期單調排列")
        if self.steps[0].phase != "claim":
            raise ValueError("workflow manifest 必須由 claim phase 開始")
        if set(step.phase for step in self.steps) != set(WORKFLOW_PHASES):
            raise ValueError("workflow manifest 必須涵蓋完整 phase spine")
        expected_persona = {
            "claim": "manager",
            "define": "planner",
            "plan": "planner",
            "build": "builder",
            "verify": "reviewer",
            "review": "reviewer",
            "ship": "manager",
        }
        for step in self.steps:
            if step.persona != expected_persona[step.phase]:
                raise ValueError(
                    f"workflow manifest {step.phase} phase 必須綁定 {expected_persona[step.phase]} persona"
                )
        first_ship = next((index for index, step in enumerate(self.steps) if step.phase == "ship"), None)
        if first_ship is not None and not any(
            step.phase == "review" and step.persona == "reviewer"
            for step in self.steps[:first_ship]
        ):
            raise ValueError("workflow manifest ship 前缺少 reviewer step")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "combo": self.combo,
            "task_slug": self.task_slug,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, payload: object) -> WorkflowManifest:
        if not isinstance(payload, dict) or set(payload) != {"version", "combo", "task_slug", "steps"}:
            raise ValueError("workflow manifest 格式錯誤")
        steps = payload["steps"]
        if not isinstance(steps, list):
            raise ValueError("workflow manifest steps 格式錯誤")
        return cls(
            version=payload["version"],
            combo=payload["combo"],
            task_slug=payload["task_slug"],
            steps=tuple(WorkflowStep.from_dict(step) for step in steps),
        )


@dataclass(frozen=True)
class WorkflowRun:
    """Manager-owned workflow aggregate persisted by coordinator registry v2."""

    run_id: str
    work_id: str
    repo: str
    claim_key: str
    source_revision: str
    workspace_root: str
    combo: str
    current_phase: str
    steps: tuple[WorkflowStep, ...]
    issue_refs: tuple[str, ...]
    openspec_refs: tuple[str, ...]
    pr_refs: tuple[str, ...]
    attempts: dict[str, int]
    evidence_refs: tuple[str, ...]
    gate_refs: tuple[GateEvidenceRef, ...]
    brainstorm_required: bool
    primary_domain: str | None
    candidate_head: str | None
    verified_head: str | None
    facets: tuple[str, ...]
    gate_status: str
    created_at: str
    updated_at: str
    planning_authority: tuple[PlanningArtifactAuthority, ...] = ()

    def __post_init__(self) -> None:
        for field, value in (
            ("run_id", self.run_id),
            ("work_id", self.work_id),
            ("repo", self.repo),
            ("claim_key", self.claim_key),
            ("source_revision", self.source_revision),
            ("workspace_root", self.workspace_root),
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
        if not isinstance(self.gate_refs, tuple) or any(
            not isinstance(item, GateEvidenceRef) for item in self.gate_refs
        ):
            raise ValueError("workflow run gate_refs 格式錯誤")
        if not isinstance(self.planning_authority, tuple) or any(
            not isinstance(item, PlanningArtifactAuthority) for item in self.planning_authority
        ):
            raise ValueError("workflow run planning_authority 格式錯誤")
        authority_refs = [item.ref for item in self.planning_authority]
        if len(authority_refs) != len(set(authority_refs)) or any(
            item.work_id != self.work_id for item in self.planning_authority
        ):
            raise ValueError("workflow run planning_authority ownership衝突")
        gate_kinds = [item.kind for item in self.gate_refs]
        gate_locators = [item.ref for item in self.gate_refs]
        if len(set(gate_kinds)) != len(gate_kinds) or len(set(gate_locators)) != len(gate_locators):
            raise ValueError("workflow gate evidence kinds and refs must be distinct")
        if not isinstance(self.brainstorm_required, bool):
            raise ValueError("workflow run brainstorm_required 必須為bool")
        for field, value in (
            ("primary_domain", self.primary_domain),
            ("candidate_head", self.candidate_head),
            ("verified_head", self.verified_head),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"workflow run {field} 必須為null或非空字串")
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
        if self.gate_status == "passed":
            required_kinds = ["foreign-review"]
            if self.brainstorm_required:
                required_kinds.insert(0, "brainstorm")
            for required_kind in required_kinds:
                if required_kind not in gate_kinds:
                    raise ValueError(f"workflow passed 缺少 {required_kind} gate evidence")
        if self.current_phase == "ship":
            if self.gate_status != "passed":
                raise ValueError("workflow ship gate_status 必須為passed")
            for required_kind in ("foreign-review", "copilot"):
                if required_kind not in gate_kinds:
                    raise ValueError(f"workflow ship 缺少 {required_kind} gate evidence")
            if self.candidate_head is None or self.verified_head != self.candidate_head:
                raise ValueError("workflow ship 必須綁定已驗證的exact candidate HEAD")
            for required_phase in ("verify", "review"):
                phase_steps = [step for step in self.steps if step.phase == required_phase]
                if not phase_steps or any(step.gate_result != "passed" for step in phase_steps):
                    raise ValueError(f"workflow ship 前 {required_phase} steps 必須全部passed")
            builder_domains = {
                step.domain for step in self.steps if step.phase == "build" and step.domain is not None
            }
            reviewer_domains = {
                step.domain
                for step in self.steps
                if step.phase in {"verify", "review"} and step.domain is not None
            }
            if not builder_domains or not reviewer_domains or builder_domains & reviewer_domains:
                raise ValueError("workflow ship 前 reviewer 必須與builder independence domain分離")
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
            "source_revision": self.source_revision,
            "workspace_root": self.workspace_root,
            "combo": self.combo,
            "current_phase": self.current_phase,
            "steps": [step.to_dict() for step in self.steps],
            "issue_refs": list(self.issue_refs),
            "openspec_refs": list(self.openspec_refs),
            "pr_refs": list(self.pr_refs),
            "attempts": dict(self.attempts),
            "evidence_refs": list(self.evidence_refs),
            "gate_refs": [item.to_dict() for item in self.gate_refs],
            "brainstorm_required": self.brainstorm_required,
            "primary_domain": self.primary_domain,
            "candidate_head": self.candidate_head,
            "verified_head": self.verified_head,
            "facets": list(self.facets),
            "gate_status": self.gate_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "planning_authority": [item.to_dict() for item in self.planning_authority],
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
            "source_revision",
            "workspace_root",
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
        gate_refs = payload.get("gate_refs", [])
        if not isinstance(gate_refs, list):
            raise ValueError("workflow run gate_refs 格式錯誤")
        planning_authority = payload.get("planning_authority", [])
        if not isinstance(planning_authority, list):
            raise ValueError("workflow run planning_authority 格式錯誤")
        return cls(
            run_id=payload["run_id"],
            work_id=payload["work_id"],
            repo=payload["repo"],
            claim_key=payload["claim_key"],
            source_revision=payload["source_revision"],
            workspace_root=payload["workspace_root"],
            combo=payload["combo"],
            current_phase=payload["current_phase"],
            steps=tuple(WorkflowStep.from_dict(step) for step in payload["steps"]),
            issue_refs=tuple(payload["issue_refs"]),
            openspec_refs=tuple(payload["openspec_refs"]),
            pr_refs=tuple(payload["pr_refs"]),
            attempts=payload["attempts"],
            evidence_refs=tuple(payload["evidence_refs"]),
            gate_refs=tuple(GateEvidenceRef.from_dict(item) for item in gate_refs),
            brainstorm_required=payload.get("brainstorm_required", False),
            primary_domain=payload.get("primary_domain"),
            candidate_head=payload.get("candidate_head"),
            verified_head=payload.get("verified_head"),
            facets=tuple(payload["facets"]),
            gate_status=payload["gate_status"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            planning_authority=tuple(
                PlanningArtifactAuthority.from_dict(item) for item in planning_authority
            ),
        )


def validate_workflow_phase_transition(current: str, new: str) -> None:
    if current not in WORKFLOW_PHASES or new not in WORKFLOW_PHASES:
        raise ValueError(f"非法 workflow phase transition: {current!r} -> {new!r}")
    if current == new:
        return
    current_index = WORKFLOW_PHASES.index(current)
    new_index = WORKFLOW_PHASES.index(new)
    if new_index != current_index + 1:
        raise ValueError(f"非法 workflow phase transition: {current!r} -> {new!r}")
