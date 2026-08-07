from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import verification
from .claim import sizing_band as compute_sizing_band


DEFAULT_WORKFLOW_COMBO = "feature-oneshot"
WORKFLOW_MANIFEST_VERSION = 1
WORKFLOW_PHASES = ("claim", "define", "plan", "build", "verify", "review", "ship")
SHIP_TRANSITION_STAGES = ("local-closeout", "pr-preflight", "external-ship")
WORKFLOW_GATE_STATUSES = frozenset({"pending", "running", "passed", "failed"})
WORKFLOW_FACETS = frozenset(
    {"needs_human", "blocked", "degraded", "needs_decomposition", "planning_released"}
)
STEP_GATE_RESULTS = frozenset({"pending", "running", "passed", "failed", "needs_human", "blocked", "skipped"})
# #205：run-scoped 模型鏈覆寫／解析結果三段固定為 planner／builder／reviewer，
# 與 WorkflowStep.persona 的合法值對齊（見 deck.schema 對 persona 的定義）。
MODEL_CHAIN_PERSONAS = frozenset({"planner", "builder", "reviewer"})
MODEL_CHAIN_RESOLUTION_SOURCES = frozenset({"override", "registry"})
COMBO_SELECTION_SOURCES = frozenset({"task-type-auto", "explicit-override", "bypass-default"})


def _validate_model_chain_override(value: object, *, field_name: str) -> None:
    """#205 D1：run-scoped 覆寫格式——{persona: {"executor":.., "model_id":..}}。"""
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"workflow run {field_name} 必須為null或dict")
    for persona, row in value.items():
        if persona not in MODEL_CHAIN_PERSONAS:
            raise ValueError(f"workflow run {field_name} persona 非法: {persona!r}")
        if (
            not isinstance(row, dict)
            or set(row) != {"executor", "model_id"}
            or not isinstance(row.get("executor"), str)
            or not row["executor"]
            or not isinstance(row.get("model_id"), str)
            or not row["model_id"]
        ):
            raise ValueError(f"workflow run {field_name}[{persona!r}] 格式錯誤")


def _validate_model_chain_resolution(value: object, *, field_name: str) -> None:
    """#205 D5：解析結果稽核紀錄——{persona: {executor, model_id,
    independence_domain, source}}，source 只能是 override 或 registry。"""
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"workflow run {field_name} 必須為null或dict")
    required_keys = {"executor", "model_id", "independence_domain", "source"}
    for persona, row in value.items():
        if persona not in MODEL_CHAIN_PERSONAS:
            raise ValueError(f"workflow run {field_name} persona 非法: {persona!r}")
        if not isinstance(row, dict) or set(row) != required_keys:
            raise ValueError(f"workflow run {field_name}[{persona!r}] 格式錯誤")
        for key in ("executor", "model_id", "independence_domain"):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError(f"workflow run {field_name}[{persona!r}].{key} 必須為非空字串")
        if row.get("source") not in MODEL_CHAIN_RESOLUTION_SOURCES:
            raise ValueError(f"workflow run {field_name}[{persona!r}].source 非法: {row.get('source')!r}")


def _validate_combo_selection(value: object) -> None:
    if value is None:
        return
    required_keys = {"source", "task_type", "combo", "reason"}
    if not isinstance(value, dict) or set(value) != required_keys:
        raise ValueError("workflow run combo_selection 格式錯誤")
    source = value.get("source")
    if source not in COMBO_SELECTION_SOURCES:
        raise ValueError(f"workflow run combo_selection.source 非法: {source!r}")
    task_type = value.get("task_type")
    if task_type is not None and (not isinstance(task_type, str) or not task_type):
        raise ValueError("workflow run combo_selection.task_type 必須為null或非空字串")
    combo = value.get("combo")
    if not isinstance(combo, str) or not combo:
        raise ValueError("workflow run combo_selection.combo 必須為非空字串")
    reason = value.get("reason")
    if (
        not isinstance(reason, str)
        or not reason
        or len(reason) > 500
    ):
        raise ValueError("workflow run combo_selection.reason 必須為 1–500 字字串")


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
    skill_ref: str | None = None
    action: str | None = None
    commit_policy: str | None = None
    test_policy: str | None = None

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
        for field, value in (
            ("skill_ref", self.skill_ref),
            ("action", self.action),
            ("commit_policy", self.commit_policy),
            ("test_policy", self.test_policy),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"workflow step {field} 必須為null或非空字串")
        if self.commit_policy not in {None, "forbidden", "optional", "required"}:
            raise ValueError("workflow step commit_policy 非法")
        if self.test_policy not in {None, "none", "red-required", "focused", "full"}:
            raise ValueError("workflow step test_policy 非法")

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
            "skill_ref": self.skill_ref,
            "action": self.action,
            "commit_policy": self.commit_policy,
            "test_policy": self.test_policy,
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
            skill_ref=payload.get("skill_ref"),
            action=payload.get("action"),
            commit_policy=payload.get("commit_policy"),
            test_policy=payload.get("test_policy"),
        )


@dataclass(frozen=True)
class GateEvidenceRef:
    """A typed, immutable locator for one independent workflow gate."""

    kind: str
    ref: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"brainstorm", "foreign-review", "copilot", "maintainer-review"}:
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
    planning_source_revision: str | None = None
    status: str = "ongoing"
    completion_record_path: str | None = None
    completion_record_hash: str | None = None
    completion_record_revision: str | None = None
    completion_source_revisions: dict[str, str] = field(default_factory=dict)
    pr_candidate: str | None = None
    merge_revision: str | None = None
    retry_classification: str | None = None
    # #222（design #208 H.2）：五維 sizing 總分／band 的 work item 快照，供中途
    # 查詢。band 字串沿用 deck.schema.BAND_LEVELS，不得另立常數或大小寫變體；
    # 門檻判定的純函式在 claim.sizing_band()（claim.py／registry.py／
    # completion.py 三處共用）。每次 repair／re-claim 都須由呼叫端重新算過再
    # 寫入，這裡只負責持有最新一次的快照，不做「沿用舊值」的隱含保證。
    sizing_score: int | None = None
    sizing_band: str | None = None
    # #223（design #208 H.3）：Red band 拆分次數快照——run 本身是第幾層拆分產物
    # （根 work item 為 0）。上限 2 層（claim.DECOMPOSITION_DEPTH_LIMIT），逾限
    # 由呼叫端轉 needs_human 而非再拆一層；本欄位只負責持有快照，不做遞增邏輯
    # （遞增屬呼叫端建立子 work item run 時的責任）。
    decomposition_depth: int = 0
    # #213（design #208 A.1）：freeze point 移至 plan review 通過之後。此欄位是
    # claim.ClaimCandidate.active_plan_review_passed 讀回的持久化基準——Yellow
    # band 的 run 在 manager._dispatch_workflow_card 的 plan phase 完成掛載點跑
    # planning.plan_review_gate() ready=True 時才寫入 True；Green/Red/None band
    # 從不呼叫 gate，沿用 pre-#213 立即凍結行為（呼叫端一律視為已通過）。只負責
    # 持有最新一次的判定快照，不在本模組做判定本身。
    plan_review_passed: bool = False
    # #211（design #208 A.2）：pre-claim readiness 六道關卡通過後凍結的
    # base_sha／monitor_snapshot_revision 等集合（claim_readiness.FrozenReadinessSet
    # 的 dict 投影），供 builder worktree 建立時消費（#211 收斂）；不得由
    # dispatch 自行重新推導一個可能更新鮮（或更陳舊）的 base。``None`` 表示尚未
    # 凍結或呼叫端未接上 readiness transaction，維持既有行為。
    frozen_readiness: dict[str, Any] | None = None
    # #205 R1/D1：run-scoped planner/builder/reviewer 模型鏈覆寫，claim（或首次
    # dispatch）時凍結；只作用於本 run，完全不觸碰共享 model-identities.yaml。
    # 比照 retry_classification／pr_candidate 的 provenance-only 加法模式——
    # 未指定的段落維持 None，_select_workflow_identity 逐段回退共享 registry。
    model_chain_override: dict[str, dict[str, str]] | None = None
    # #205 R4/D5：三段各自實際解析到的 executor／model／independence_domain
    # 與來源標記（run-scoped override vs 共享 registry），供事後稽核「這次到底
    # 用了什麼模型、為什麼」。每次 dispatch 選定 identity 時逐段覆寫更新，純
    # provenance，不影響既有 workflow 語意。
    resolved_model_chain: dict[str, dict[str, str]] | None = None
    combo_selection: dict[str, Any] | None = None

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
        if self.planning_source_revision is not None and (
            not isinstance(self.planning_source_revision, str)
            or not self.planning_source_revision
        ):
            raise ValueError("workflow run planning_source_revision 必須為null或非空字串")
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
            # #216：retry_classification 只是 provenance（比照 completion.py
            # reused_from 的做法），刻意不在此收斂成封閉集合——閉集合驗證交給
            # work_actions.RetryClassification／completion.py
            # RETRY_CLASSIFICATION_VALUES，避免第三處需要手動同步的列舉。
            ("retry_classification", self.retry_classification),
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
        if self.status not in {"ongoing", "done", "superseded"}:
            raise ValueError(f"workflow run status 非法: {self.status!r}")
        completion_values = (
            self.completion_record_path,
            self.completion_record_hash,
            self.completion_record_revision,
            self.pr_candidate,
            self.merge_revision,
        )
        if any(value is not None for value in completion_values):
            if any(not isinstance(value, str) or not value for value in completion_values):
                raise ValueError("workflow completion fields must be supplied together")
            if not self.completion_source_revisions or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value
                for key, value in self.completion_source_revisions.items()
            ):
                raise ValueError("workflow completion source revisions invalid")
        elif self.completion_source_revisions:
            raise ValueError("workflow completion source revisions require completion fields")
        if self.status == "done" and not all(completion_values):
            raise ValueError("done workflow requires bound completion evidence")
        if (self.sizing_score is None) != (self.sizing_band is None):
            raise ValueError("workflow run sizing_score/sizing_band must be supplied together")
        if self.sizing_score is not None:
            # compute_sizing_band()（claim.sizing_band，#222 H.2）同時完成型別／
            # 範圍檢查與門檻判定；sizing_band 若與門檻算出的預期值不符（含大小寫
            # 變體、非法字串）在此一併 fail-closed，不必另外重複驗證 BAND_LEVELS
            # 成員資格。
            expected_band = compute_sizing_band(self.sizing_score)
            if self.sizing_band != expected_band:
                raise ValueError(
                    "workflow run sizing_band 與 sizing_score 門檻不符"
                    f"（預期 {expected_band!r}，實得 {self.sizing_band!r}）"
                )
        if (
            not isinstance(self.decomposition_depth, int)
            or isinstance(self.decomposition_depth, bool)
            or not (0 <= self.decomposition_depth <= 2)
        ):
            raise ValueError(
                "workflow run decomposition_depth 必須為 0–2 的整數（#223 拆分深度上限）"
            )
        if not isinstance(self.plan_review_passed, bool):
            raise ValueError("workflow run plan_review_passed 必須為bool")
        if self.frozen_readiness is not None:
            base_sha = (
                self.frozen_readiness.get("base_sha")
                if isinstance(self.frozen_readiness, dict)
                else None
            )
            if (
                not isinstance(self.frozen_readiness, dict)
                or not isinstance(base_sha, str)
                or verification.SAFE_SHA_RE.fullmatch(base_sha) is None
            ):
                raise ValueError("workflow run frozen_readiness base_sha 格式錯誤")
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
            if "foreign-review" not in gate_kinds:
                raise ValueError("workflow ship 缺少 foreign-review gate evidence")
            delivery_reviews = {"copilot", "maintainer-review"} & set(gate_kinds)
            if len(delivery_reviews) != 1:
                raise ValueError("workflow ship 必須恰有一種 current-HEAD delivery review gate evidence")
            if self.candidate_head is None or self.verified_head != self.candidate_head:
                raise ValueError("workflow ship 必須綁定已驗證的exact candidate HEAD")
            for required_phase in ("verify", "review", "ship"):
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
        _validate_model_chain_override(self.model_chain_override, field_name="model_chain_override")
        _validate_model_chain_resolution(self.resolved_model_chain, field_name="resolved_model_chain")
        _validate_combo_selection(self.combo_selection)

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
            "planning_source_revision": self.planning_source_revision,
            "status": self.status,
            "completion_record_path": self.completion_record_path,
            "completion_record_hash": self.completion_record_hash,
            "completion_record_revision": self.completion_record_revision,
            "completion_source_revisions": dict(self.completion_source_revisions),
            "pr_candidate": self.pr_candidate,
            "merge_revision": self.merge_revision,
            "retry_classification": self.retry_classification,
            "sizing_score": self.sizing_score,
            "sizing_band": self.sizing_band,
            "decomposition_depth": self.decomposition_depth,
            "plan_review_passed": self.plan_review_passed,
            "frozen_readiness": (
                dict(self.frozen_readiness) if self.frozen_readiness is not None else None
            ),
            "model_chain_override": (
                {persona: dict(row) for persona, row in self.model_chain_override.items()}
                if self.model_chain_override is not None
                else None
            ),
            "resolved_model_chain": (
                {persona: dict(row) for persona, row in self.resolved_model_chain.items()}
                if self.resolved_model_chain is not None
                else None
            ),
            "combo_selection": (
                dict(self.combo_selection) if self.combo_selection is not None else None
            ),
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
            planning_source_revision=payload.get("planning_source_revision"),
            status=payload.get("status", "ongoing"),
            completion_record_path=payload.get("completion_record_path"),
            completion_record_hash=payload.get("completion_record_hash"),
            completion_record_revision=payload.get("completion_record_revision"),
            completion_source_revisions=dict(payload.get("completion_source_revisions", {})),
            pr_candidate=payload.get("pr_candidate"),
            merge_revision=payload.get("merge_revision"),
            retry_classification=payload.get("retry_classification"),
            sizing_score=payload.get("sizing_score"),
            sizing_band=payload.get("sizing_band"),
            decomposition_depth=payload.get("decomposition_depth", 0),
            plan_review_passed=payload.get("plan_review_passed", False),
            frozen_readiness=payload.get("frozen_readiness"),
            model_chain_override=payload.get("model_chain_override"),
            resolved_model_chain=payload.get("resolved_model_chain"),
            combo_selection=payload.get("combo_selection"),
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


def validate_ship_stage_transition(current: str, new: str) -> None:
    if current not in SHIP_TRANSITION_STAGES or new not in SHIP_TRANSITION_STAGES:
        raise ValueError(f"非法 ship transition stage: {current!r} -> {new!r}")
    if current == new:
        return
    current_index = SHIP_TRANSITION_STAGES.index(current)
    new_index = SHIP_TRANSITION_STAGES.index(new)
    if new_index != current_index + 1:
        raise ValueError(f"非法 ship transition stage: {current!r} -> {new!r}")
