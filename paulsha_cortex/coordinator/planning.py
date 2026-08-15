from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .._yaml import YAMLError, safe_load
from .model_identities import (
    CapabilityProbe,
    IdentityRegistry,
    ModelIdentity,
    select_secondary_planner,
)
from .workflow import GateEvidenceRef

PLANNING_KINDS = ("spec", "design", "plan")
QUESTION_PACK_SCHEMA_VERSION = 1
BRAINSTORM_EVIDENCE_SCHEMA_VERSION = 1
_STANDALONE_MARKERS = frozenset({"tbd", "[tbd]", "decision: tbd", "決策：未定"})
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST_ITEM_RE = re.compile(r"^(?:[-+*]|\d+[.)])\s+(\S.*)$")
# issue #520：驗收判準與 integrator prompt 的標題要求過去是兩份各自維護的真實來源，
# 已因不同步造成兩次確定性失敗。這裡是唯一真檔：`_ACCEPTED_HEADINGS` 依「首選在前」
# 排序（給 prompt 用的顯示形），`_REQUIRED_HEADINGS` 由它 casefold 派生（給驗收用），
# prompt 文字則由 `required_heading_hint()` 機械產生，不得在 prompt 端另寫一份。
_ACCEPTED_HEADINGS: dict[str, tuple[str, ...]] = {
    "spec": ("Requirements", "Requirement", "Problem", "Problem and Outcome", "Goals"),
    "design": ("Decisions", "Decision", "Design", "Architecture"),
    "plan": ("Tasks", "Task"),
}
_REQUIRED_HEADINGS = {
    kind: frozenset(title.casefold() for title in titles)
    for kind, titles in _ACCEPTED_HEADINGS.items()
}


def required_heading_hint() -> str:
    """回傳 integrator prompt 用的必要標題說明（由上面的判準常數機械產生）。

    issue #520：舊 prompt 手寫「required headings: Requirements for spec, Decisions
    for design, Tasks for plan」，原意是逐 kind 對應，字面卻同樣可讀成「必要標題就是
    `Requirements for spec`」。模型採了後者、產出 `## Requirements for spec`，而
    `_has_required_heading()` 是 casefold 後**完全相等**比對（`_headings_and_markers()`
    的正規化只剝編號前綴，不剝 ` for spec` 尾綴），於是必然 `required-section-missing`。
    此處逐 kind 給出精確標題、明確禁止附加 kind 名稱，並揭露完整可接受集合，讓模型有
    合法替代選項而非單點命中。
    """
    preferred = ", ".join(
        f'exactly "## {_ACCEPTED_HEADINGS[kind][0]}" for kind={kind}' for kind in PLANNING_KINDS
    )
    forbidden = ", ".join(f'"for {kind}"' for kind in PLANNING_KINDS)
    alternatives = "; ".join(
        f"{kind}: {', '.join(_ACCEPTED_HEADINGS[kind])}" for kind in PLANNING_KINDS
    )
    return (
        "The required heading depends on the artifact kind: use "
        f"{preferred}. The heading text is that word alone; do not append the kind name or "
        f"any other suffix such as {forbidden} to it. Heading text is matched "
        "case-insensitively against a fixed set, so any one of these is also accepted — "
        f"{alternatives}."
    )


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanningScope:
    repo: str
    work_id: str
    source_revision: str

    def __post_init__(self) -> None:
        for field, value in (
            ("repo", self.repo),
            ("work_id", self.work_id),
            ("source_revision", self.source_revision),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"planning scope {field} must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {
            "repo": self.repo.strip(),
            "work_id": self.work_id.strip(),
            "source_revision": self.source_revision.strip(),
        }


@dataclass(frozen=True)
class PlanningArtifact:
    kind: str
    ref: str
    text: str


@dataclass(frozen=True)
class BlockingMarker:
    kind: str
    line: int
    text: str


@dataclass(frozen=True)
class ArtifactAssessment:
    artifact: PlanningArtifact
    accepted: bool
    reasons: tuple[str, ...]
    blocking_markers: tuple[BlockingMarker, ...]


def _frontmatter_and_body(text: str) -> tuple[dict[str, object], str, int]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, 0
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        return {}, text, 0
    frontmatter_lines = lines[1:closing]
    seen_top_level: set[str] = set()
    for raw in frontmatter_lines:
        if not raw or raw[0].isspace() or ":" not in raw:
            continue
        key = raw.split(":", 1)[0].strip()
        if key in seen_top_level:
            return {}, "\n".join(lines[closing + 1 :]), closing + 1
        seen_top_level.add(key)
    try:
        payload = safe_load("\n".join(frontmatter_lines))
    except YAMLError:
        return {}, "\n".join(lines[closing + 1 :]), closing + 1
    if not isinstance(payload, dict):
        payload = {}
    return payload, "\n".join(lines[closing + 1 :]), closing + 1


def _headings_and_markers(body: str, *, line_offset: int) -> tuple[set[str], tuple[BlockingMarker, ...]]:
    headings: set[str] = set()
    markers: list[BlockingMarker] = []
    in_fence = False
    fence_token: str | None = None
    open_questions_level: int | None = None
    for body_index, raw in enumerate(body.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            token = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_token = token
            elif token == fence_token:
                in_fence = False
                fence_token = None
            continue
        if in_fence:
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip().casefold()
            title = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", title).rstrip(":：")
            headings.add(title)
            if title in {"open questions", "open question", "未決問題"}:
                open_questions_level = level
            elif open_questions_level is not None and level <= open_questions_level:
                open_questions_level = None
            continue
        line_number = line_offset + body_index
        if stripped.casefold() in _STANDALONE_MARKERS:
            markers.append(BlockingMarker("standalone", line_number, stripped))
            continue
        item = _LIST_ITEM_RE.match(stripped)
        if open_questions_level is not None:
            if item and item.group(1).strip().casefold() not in {"none", "n/a", "無", "無。"}:
                markers.append(BlockingMarker("open-question", line_number, item.group(1).strip()))
    return headings, tuple(markers)


def _has_required_heading(kind: str, headings: set[str]) -> bool:
    if kind == "plan":
        return any(title in {"task", "tasks"} or title.startswith("task ") for title in headings)
    required = _REQUIRED_HEADINGS[kind]
    return any(title in required for title in headings)


def assess_planning_artifact(artifact: PlanningArtifact) -> ArtifactAssessment:
    if artifact.kind not in PLANNING_KINDS:
        raise ValueError(f"unknown planning artifact kind: {artifact.kind}")
    frontmatter, body, offset = _frontmatter_and_body(artifact.text)
    headings, markers = _headings_and_markers(body, line_offset=offset)
    reasons: list[str] = []
    status = frontmatter.get("status")
    if not isinstance(status, str) or status.strip().casefold() != "accepted":
        reasons.append("status-not-accepted")
    if not _has_required_heading(artifact.kind, headings):
        reasons.append("required-section-missing")
    if markers:
        reasons.append("blocking-decision")
    return ArtifactAssessment(artifact, not reasons, tuple(reasons), markers)


@dataclass(frozen=True)
class PlanningQuestion:
    question_id: str
    kind: str
    prompt: str
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "kind": self.kind,
            "prompt": self.prompt,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class QuestionPack:
    pack_id: str
    questions: tuple[PlanningQuestion, ...]
    schema_version: int = QUESTION_PACK_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "questions": [question.to_dict() for question in self.questions],
        }


@dataclass(frozen=True)
class CompletenessReport:
    complete: bool
    assessments: tuple[ArtifactAssessment, ...]
    missing_kinds: tuple[str, ...]
    default_question_pack: QuestionPack

    def to_dict(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "missing_kinds": list(self.missing_kinds),
            "artifacts": [
                {
                    "kind": assessment.artifact.kind,
                    "ref": assessment.artifact.ref,
                    "accepted": assessment.accepted,
                    "reasons": list(assessment.reasons),
                    "blocking_markers": [asdict(marker) for marker in assessment.blocking_markers],
                }
                for assessment in self.assessments
            ],
        }


def _make_question(kind: str, prompt: str, source_refs: tuple[str, ...]) -> PlanningQuestion:
    identity = {"kind": kind, "prompt": prompt, "source_refs": list(source_refs)}
    return PlanningQuestion(
        question_id="q-" + _hash_payload(identity)[:16],
        kind=kind,
        prompt=prompt,
        source_refs=source_refs,
    )


def _build_default_question_pack(
    assessments: tuple[ArtifactAssessment, ...], missing_kinds: tuple[str, ...]
) -> QuestionPack:
    questions: list[PlanningQuestion] = []
    # #408（補完）：missing-{kind} 問題的 source_refs 過去只取「同 kind 的
    # assessments refs」。同 kind 有草稿（rejected/draft）時這是對的——重寫要
    # 以草稿為本（見 test_rejected_artifacts_remain_authoritative_sources_...）；
    # 但 todo 錨定的 work item（如 small-fix combo）該 kind 完全不存在，
    # source_refs 恆為空 tuple，造成兩個下游斷點：
    # (a) `_planning_destinations` 的 openspec／workstream 錨點推導拿不到任何
    #     路徑 → destinations 空 → integrator 發明路徑必被 governed-roots 拒；
    # (b) `_planning_source_material` 無檔可讀 → secondary planner 兩手空空。
    # 故補 fallback：同 kind refs 為空時退到全部 accepted artifacts 的 refs
    # ——既有權威素材正是「建立 accepted {kind} 需要什麼權威內容」的來源。
    accepted_refs = tuple(
        assessment.artifact.ref for assessment in assessments if assessment.accepted
    )
    for kind in missing_kinds:
        same_kind_refs = tuple(
            assessment.artifact.ref
            for assessment in assessments
            if assessment.artifact.kind == kind
        )
        questions.append(
            _make_question(
                f"missing-{kind}",
                f"What authoritative content is required to create an accepted {kind}?",
                same_kind_refs or accepted_refs,
            )
        )
    for assessment in assessments:
        if not assessment.blocking_markers:
            continue
        questions.append(
            _make_question(
                "blocking-decision",
                f"What evidence resolves the blocking decision in {assessment.artifact.ref}?",
                (assessment.artifact.ref,),
            )
        )
    body = [question.to_dict() for question in questions]
    return QuestionPack(pack_id="qp-" + _hash_payload(body)[:24], questions=tuple(questions))


def assess_planning_completeness(artifacts: Iterable[PlanningArtifact]) -> CompletenessReport:
    assessments = tuple(assess_planning_artifact(artifact) for artifact in artifacts)
    accepted_kinds = {assessment.artifact.kind for assessment in assessments if assessment.accepted}
    missing_kinds = tuple(kind for kind in PLANNING_KINDS if kind not in accepted_kinds)
    pack = _build_default_question_pack(assessments, missing_kinds)
    has_blockers = any(assessment.blocking_markers for assessment in assessments)
    return CompletenessReport(not missing_kinds and not has_blockers, assessments, missing_kinds, pack)


# --- #208 設計 A.1 第 3 點／#212：plan review gate（三項判定） ----------------
#
# 三份文件皆 accepted（assess_planning_completeness 通過）之後才跑的一層語意審查，
# 是「唯一需要模型的前置閘」，但本函式落地的只是機械骨架與判定契約——三項判定中
# 可機械判定的部分（契約相容性、封套查表）直接機械做；plan review 的 model
# dispatch（effort=high、prompt cache）沿用既有 planning 流程身分，不在此模組。
#
# 三項判定（cost order，任一不過即 fail closed）：
#   1. completeness            —— plan 為每個 acceptance surface 備有對應 task
#   2. contract_compatibility  —— plan scope 與呼叫端算好的 R-09/R-16/R-19/R-22
#                                  等 applicable_contract_rules 相容；plan
#                                  frontmatter 明確排除的項目與規則要求衝突時，
#                                  是 hippo #18 第 9 條要在此攔截的 terminal case
#   3. envelope                —— plan 宣告的 invariant_count／artifact_classes
#                                  落在 #209 builder 封套內；封套資料缺席
#                                  （#209 未落地）時記 envelope_unavailable 並以
#                                  可觀測 bypass 通過（對齊 #202 定案），有資料
#                                  而超封套才 fail closed
#
# 失敗分類比照 claim_readiness.ReadinessOutcome：只有 policy-scope-conflict 是
# terminal（回傳給呼叫端轉 needs_human），其餘（含 envelope 超界）都是可重試
# 訊號（回派 planner）——這與 claim_readiness.capability_probe 對「capability
# 不足」同樣視為可重試、而非 terminal 的既有先例一致。
PLAN_REVIEW_CHECK_ORDER = ("completeness", "contract_compatibility", "envelope")

# 契約相容性的規則→Tasks 關鍵字對照（啟發式子字串比對，大小寫不敏感）。
# 只涵蓋 policy checklist 目前對「plan 尚未有程式碼變更」語意有意義的四條規則；
# 是否適用（哪些規則命中）由呼叫端依 scope/code_paths 算好以 frozenset[str] 餵入，
# 與 #221 compute_sizing_score 的 applicable_contract_rules 共用同一份計算結果。
_CONTRACT_RULE_TASK_KEYWORDS: dict[str, frozenset[str]] = {
    "R-09": frozenset({"changelog"}),
    "R-16": frozenset({"cli"}),
    "R-19": frozenset({"test", "測試"}),
    "R-22": frozenset({"doc", "docs", "文件"}),
}
CONTRACT_COMPATIBILITY_RULES = frozenset(_CONTRACT_RULE_TASK_KEYWORDS)


def _collect_task_items(body: str) -> tuple[str, ...]:
    """收集 Tasks/Task heading 下的清單項目文字。

    比照 _headings_and_markers() 對 Open Questions 的 heading-level 追蹤手法
    （鎖定 heading、往下收集清單項目、遇到同級或更高層 heading 才停止），
    抽出一個 Tasks 版本；152-156 行 _has_required_heading 已有的 task heading
    判斷（"task"/"tasks" 或 "task " 前綴）在這裡重用同一組字面比對規則。
    """
    items: list[str] = []
    in_fence = False
    fence_token: str | None = None
    task_level: int | None = None
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            token = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_token = token
            elif token == fence_token:
                in_fence = False
                fence_token = None
            continue
        if in_fence:
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip().casefold()
            title = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", title).rstrip(":：")
            if title in {"task", "tasks"} or title.startswith("task "):
                task_level = level
            elif task_level is not None and level <= task_level:
                task_level = None
            continue
        if task_level is None:
            continue
        item = _LIST_ITEM_RE.match(stripped)
        if item:
            items.append(item.group(1).strip())
    return tuple(items)


@dataclass(frozen=True)
class PlanReviewCheckResult:
    """一項 plan review 判定的結果。``terminal`` 只在 ``passed`` 為 False 時有意義。"""

    name: str
    passed: bool
    reason: str | None = None
    terminal: bool = False
    observation: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in PLAN_REVIEW_CHECK_ORDER:
            raise ValueError(f"unknown plan review check: {self.name!r}")
        if self.passed and (self.reason is not None or self.terminal):
            raise ValueError("a passed plan review check must not carry reason/terminal")
        if not self.passed and not self.reason:
            raise ValueError("a failed plan review check requires a reason")


@dataclass(frozen=True)
class PlanReviewOutcome:
    """跑完（或短路於）三項判定後的結果，比照 ReadinessOutcome 的 ready/terminal 語意。"""

    ready: bool
    failed_check: str | None
    reason: str | None
    terminal: bool
    checks_run: tuple[str, ...]
    observations: Mapping[str, Mapping[str, object]]


def _plan_review_completeness(
    plan_artifact: PlanningArtifact, acceptance_surfaces: frozenset[str]
) -> PlanReviewCheckResult:
    _, body, _ = _frontmatter_and_body(plan_artifact.text)
    haystack = "\n".join(_collect_task_items(body)).casefold()
    missing = tuple(
        sorted(surface for surface in acceptance_surfaces if surface.casefold() not in haystack)
    )
    if missing:
        return PlanReviewCheckResult(
            "completeness",
            False,
            reason=f"missing-task-for-surface: {', '.join(missing)}",
            observation={"missing_surfaces": missing},
        )
    return PlanReviewCheckResult(
        "completeness", True, observation={"acceptance_surfaces": tuple(sorted(acceptance_surfaces))}
    )


def _plan_review_contract_compatibility(
    plan_artifact: PlanningArtifact, applicable_contract_rules: frozenset[str]
) -> PlanReviewCheckResult:
    unknown_rules = applicable_contract_rules - CONTRACT_COMPATIBILITY_RULES
    if unknown_rules:
        raise ValueError(f"applicable_contract_rules 含未知規則: {sorted(unknown_rules)}")
    frontmatter, body, _ = _frontmatter_and_body(plan_artifact.text)
    excludes_raw = frontmatter.get("scope_excludes", [])
    if not isinstance(excludes_raw, list) or any(not isinstance(item, str) for item in excludes_raw):
        raise ValueError("plan frontmatter scope_excludes 必須為字串列表")
    excludes = frozenset(item.strip().casefold() for item in excludes_raw if item.strip())
    haystack = "\n".join(_collect_task_items(body)).casefold()

    conflicts: list[str] = []
    missing: list[str] = []
    for rule in sorted(applicable_contract_rules):
        keywords = _CONTRACT_RULE_TASK_KEYWORDS[rule]
        if keywords & excludes:
            conflicts.append(rule)
        elif not any(keyword in haystack for keyword in keywords):
            missing.append(rule)

    if conflicts:
        # hippo #18 第 9 條：plan 自身宣告的 scope_excludes 與規則要求互斥，
        # 是本判定唯一的 terminal case（回傳 needs_human，不回派 planner重試）。
        return PlanReviewCheckResult(
            "contract_compatibility",
            False,
            reason=f"policy-scope-conflict: {', '.join(conflicts)}",
            terminal=True,
            observation={"conflicts": tuple(conflicts)},
        )
    if missing:
        return PlanReviewCheckResult(
            "contract_compatibility",
            False,
            reason=f"missing-task-for-rule: {', '.join(missing)}",
            observation={"missing_rules": tuple(missing)},
        )
    return PlanReviewCheckResult(
        "contract_compatibility",
        True,
        observation={"applicable_contract_rules": tuple(sorted(applicable_contract_rules))},
    )


def _plan_review_envelope(
    plan_artifact: PlanningArtifact,
    envelope_lookup: Callable[[], Mapping[str, object] | None] | None,
) -> PlanReviewCheckResult:
    frontmatter, _, _ = _frontmatter_and_body(plan_artifact.text)
    invariant_count = frontmatter.get("invariant_count")
    if not isinstance(invariant_count, int) or isinstance(invariant_count, bool) or invariant_count < 0:
        raise ValueError("plan frontmatter 缺少合法的 invariant_count（需為 >=0 整數宣告）")
    artifact_classes_raw = frontmatter.get("artifact_classes")
    if (
        not isinstance(artifact_classes_raw, list)
        or not artifact_classes_raw
        or any(not isinstance(item, str) or not item.strip() for item in artifact_classes_raw)
    ):
        raise ValueError("plan frontmatter 缺少合法的 artifact_classes（需為非空字串列表宣告）")
    artifact_classes = frozenset(item.strip() for item in artifact_classes_raw)

    # #209（能力封套）未落地：可插拔 provider，缺席時可觀測 bypass 通過。
    envelope = envelope_lookup() if envelope_lookup is not None else None
    if envelope is None:
        return PlanReviewCheckResult(
            "envelope", True, observation={"bypass": "envelope_unavailable"}
        )

    envelope_invariant_count = envelope.get("invariant_count")
    envelope_artifact_classes_raw = envelope.get("artifact_classes")
    if (
        not isinstance(envelope_invariant_count, int)
        or isinstance(envelope_invariant_count, bool)
        or envelope_invariant_count < 0
        or not isinstance(envelope_artifact_classes_raw, list)
        or any(not isinstance(item, str) for item in envelope_artifact_classes_raw)
    ):
        raise ValueError("builder envelope 格式錯誤")
    envelope_artifact_classes = frozenset(str(item).strip() for item in envelope_artifact_classes_raw)
    over_budget = artifact_classes - envelope_artifact_classes
    if invariant_count > envelope_invariant_count or over_budget:
        return PlanReviewCheckResult(
            "envelope",
            False,
            reason="envelope-exceeded",
            observation={
                "invariant_count": invariant_count,
                "envelope_invariant_count": envelope_invariant_count,
                "over_budget_artifact_classes": tuple(sorted(over_budget)),
            },
        )
    return PlanReviewCheckResult(
        "envelope",
        True,
        observation={
            "bypass": None,
            "invariant_count": invariant_count,
            "artifact_classes": tuple(sorted(artifact_classes)),
        },
    )


def plan_review_gate(
    *,
    plan_artifact: PlanningArtifact,
    acceptance_surfaces: frozenset[str],
    applicable_contract_rules: frozenset[str],
    envelope_lookup: Callable[[], Mapping[str, object] | None] | None = None,
) -> PlanReviewOutcome:
    """三項判定（完整性／契約相容性／封套相符），依 cost order 短路於首個失敗。

    ``acceptance_surfaces``／``applicable_contract_rules`` 皆由呼叫端算好注入
    （不在此模組反向推導 scope/code_paths），``envelope_lookup`` 是 #209 封套查表
    的占位 provider（``None`` 或回傳 ``None`` 皆視為封套資料缺席）。
    """
    if plan_artifact.kind != "plan":
        raise ValueError(f"plan_review_gate 需要 kind='plan' 的 artifact，實際 {plan_artifact.kind!r}")

    checks: tuple[tuple[str, Callable[[], PlanReviewCheckResult]], ...] = (
        ("completeness", lambda: _plan_review_completeness(plan_artifact, acceptance_surfaces)),
        (
            "contract_compatibility",
            lambda: _plan_review_contract_compatibility(plan_artifact, applicable_contract_rules),
        ),
        ("envelope", lambda: _plan_review_envelope(plan_artifact, envelope_lookup)),
    )
    checks_run: list[str] = []
    observations: dict[str, Mapping[str, object]] = {}
    for name, probe in checks:
        checks_run.append(name)
        result = probe()
        if not result.passed:
            return PlanReviewOutcome(
                ready=False,
                failed_check=name,
                reason=result.reason,
                terminal=result.terminal,
                checks_run=tuple(checks_run),
                observations=observations,
            )
        observations[name] = result.observation
    return PlanReviewOutcome(
        ready=True,
        failed_check=None,
        reason=None,
        terminal=False,
        checks_run=tuple(checks_run),
        observations=observations,
    )


def plan_review_freezes_authority(outcome: PlanReviewOutcome) -> bool:
    """#213（design #208 A.1）：freeze point 移至 plan review 通過之後。

    把 :func:`plan_review_gate` 的判定結果對應到「呼叫端現在可以 freeze 了嗎」——
    只有 ``ready=True`` 才可以，不論不通過的原因是可重試（回派 planner 修訂）還是
    terminal（policy-scope-conflict，轉 needs_human）：兩者都代表 plan 尚未定案，
    freeze 都不該發生。這是 ``claim.ClaimCandidate.active_plan_review_passed``
    該填入的值，讓 plan review 前的 plan 修訂不會被 claim.py 誤判成 authority
    變更（避免觸發 supersede、產生新世代，hippo #18 第 3、7 條）。
    """
    return outcome.ready


# --- #208 設計 H.1／#221：五維 sizing 評分 -----------------------------------
#
# 五維量表（每維 0-2 分，總分 0-10）：
#   機械三維 —— acceptance_surfaces / spec_stability / orchestration，由
#   compute_sizing_score() 依注入參數純函式計算；
#   宣告二維 —— domain_breadth / state_consistency，由 planner 寫在 plan
#   frontmatter（可證偽，供 #210/#137 後驗）。
#
# band 判定（Green/Yellow/Red 閾值套用）屬 #222，本模組只產生分數本身；
# 全程不啟動任何 model session（純函式，不 import model_identities 的
# CLI 呼叫路徑，也不對 planning_runtime.py 產生依賴）。
SIZING_DIMENSIONS = (
    "domain_breadth",
    "state_consistency",
    "acceptance_surfaces",
    "spec_stability",
    "orchestration",
)
# acceptance_surfaces 讀取的 contract 規則白名單（policy checklist R-09/R-16/R-19：
# changelog fragment／CLI help 同步／CI 測試）；呼叫端算好「這個 work item 碰了哪些
# 規則」後以 frozenset 注入，避免 planning.py 反向 import policy_check 或 deck。
ACCEPTANCE_SURFACE_RULES = frozenset({"R-09", "R-16", "R-19"})
# plan frontmatter 宣告欄位骨架（可擴充）：#212 會再掛 invariant_count／artifact_classes
# 等自己的宣告欄位，沿用同一個「讀 frontmatter → 嚴格範圍檢查」helper 即可，不必
# 各自重造一次驗證邏輯。
_SIZING_DECLARED_FIELDS = ("domain_breadth", "state_consistency")


def _declared_dimension(frontmatter: Mapping[str, object], field: str) -> int:
    """讀取 plan frontmatter 宣告欄位，嚴格驗證為 0–2 整數（fail-closed，
    比照 PlanningScope.__post_init__ 風格）。"""
    value = frontmatter.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 2):
        raise ValueError(f"plan frontmatter 缺少合法的 {field}（需為 0–2 整數宣告）")
    return value


@dataclass(frozen=True)
class SizingScore:
    domain_breadth: int
    state_consistency: int
    acceptance_surfaces: int
    spec_stability: int
    orchestration: int

    def __post_init__(self) -> None:
        for name in SIZING_DIMENSIONS:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 2):
                raise ValueError(f"sizing dimension {name} must be an int in [0, 2], got {value!r}")

    @property
    def total(self) -> int:
        return sum(getattr(self, name) for name in SIZING_DIMENSIONS)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {name: getattr(self, name) for name in SIZING_DIMENSIONS}
        payload["total"] = self.total
        return payload


def compute_sizing_score(
    *,
    plan_artifact: PlanningArtifact,
    completeness_report: CompletenessReport,
    gate_spine_count: int,
    applicable_contract_rules: frozenset[str],
    cards_count: int,
    persona_binding_count: int,
) -> SizingScore:
    """五維 sizing 評分（#208 H.1）：三維機械算 + 二維宣告，純函式、不啟動 model session。

    gate_spine_count／applicable_contract_rules／cards_count／persona_binding_count
    皆由呼叫端算好注入（deck combo 的必要核心 gate_spine 計數、R-09/R-16/R-19 適用性、
    combo.cards 數與其中填了 persona_binding 的卡片數）——planning.py 刻意不反向
    import deck 模組，維持既有模組邊界。
    """
    if plan_artifact.kind != "plan":
        raise ValueError(f"compute_sizing_score 需要 kind='plan' 的 artifact，實際 {plan_artifact.kind!r}")
    frontmatter, _, _ = _frontmatter_and_body(plan_artifact.text)
    domain_breadth = _declared_dimension(frontmatter, "domain_breadth")
    state_consistency = _declared_dimension(frontmatter, "state_consistency")

    if gate_spine_count < 0:
        raise ValueError("gate_spine_count 不得為負")
    unknown_rules = applicable_contract_rules - ACCEPTANCE_SURFACE_RULES
    if unknown_rules:
        raise ValueError(f"applicable_contract_rules 含未知規則: {sorted(unknown_rules)}")
    if cards_count < 0 or persona_binding_count < 0 or persona_binding_count > cards_count:
        raise ValueError("cards_count/persona_binding_count 不合法")

    # acceptance_surfaces：核心 gate_spine 計數 + 適用規則數的組合訊號，門檻切三級。
    acceptance_signal = gate_spine_count + len(applicable_contract_rules)
    if acceptance_signal == 0:
        acceptance_surfaces = 0
    elif acceptance_signal <= 2:
        acceptance_surfaces = 1
    else:
        acceptance_surfaces = 2

    # spec_stability：deterministic completeness 結果——缺的 kind 數與是否有
    # blocking markers 各扣一分，不只取 CompletenessReport.complete 的布林值。
    missing_penalty = len(completeness_report.missing_kinds)
    blocking_penalty = 1 if any(
        assessment.blocking_markers for assessment in completeness_report.assessments
    ) else 0
    spec_stability = max(0, 2 - missing_penalty - blocking_penalty)

    # orchestration：card 清單規模 + 有填 persona_binding 的卡片數。
    if cards_count <= 1:
        orchestration = 0
    elif persona_binding_count <= 1:
        orchestration = 1
    else:
        orchestration = 2

    return SizingScore(
        domain_breadth=domain_breadth,
        state_consistency=state_consistency,
        acceptance_surfaces=acceptance_surfaces,
        spec_stability=spec_stability,
        orchestration=orchestration,
    )


def _strict_string_list(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a string list")
    normalized = tuple(item.strip() for item in value)
    if not allow_empty and not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def validate_question_pack(payload: object, *, report: CompletenessReport) -> QuestionPack:
    if not isinstance(payload, dict):
        raise ValueError("question pack must be an object")
    extras = set(payload) - {"schema_version", "pack_id", "questions"}
    if extras:
        raise ValueError(f"question pack unexpected key: {sorted(extras)[0]}")
    if payload.get("schema_version") != QUESTION_PACK_SCHEMA_VERSION:
        raise ValueError("question pack schema_version invalid")
    pack_id = payload.get("pack_id")
    rows = payload.get("questions")
    if not isinstance(pack_id, str) or not pack_id or not isinstance(rows, list):
        raise ValueError("question pack identity/questions invalid")
    questions: list[PlanningQuestion] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"questions[{index}] must be an object")
        extras = set(row) - {"question_id", "kind", "prompt", "source_refs"}
        if extras:
            raise ValueError(f"questions[{index}] unexpected key: {sorted(extras)[0]}")
        question_id = row.get("question_id")
        kind = row.get("kind")
        prompt = row.get("prompt")
        if not all(isinstance(value, str) and value.strip() for value in (question_id, kind, prompt)):
            raise ValueError(f"questions[{index}] has invalid scalar")
        if question_id in seen:
            raise ValueError(f"duplicate question_id: {question_id}")
        seen.add(question_id)
        questions.append(
            PlanningQuestion(
                question_id=question_id.strip(),
                kind=kind.strip(),
                prompt=prompt.strip(),
                source_refs=_strict_string_list(row.get("source_refs"), f"questions[{index}].source_refs", allow_empty=True),
            )
        )
    normalized = QuestionPack(pack_id=pack_id, questions=tuple(questions))
    if normalized.to_dict() != report.default_question_pack.to_dict():
        raise ValueError("question pack does not cover exact completeness blockers")
    return normalized


@dataclass(frozen=True)
class SecondaryEvidenceItem:
    question_id: str
    claims: tuple[str, ...]
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "claims": list(self.claims),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class SecondaryEvidence:
    question_pack_id: str
    items: tuple[SecondaryEvidenceItem, ...]
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "question_pack_id": self.question_pack_id,
            "evidence": [item.to_dict() for item in self.items],
        }


def validate_secondary_evidence(payload: object, *, question_pack: QuestionPack) -> SecondaryEvidence:
    if not isinstance(payload, dict):
        raise ValueError("secondary evidence must be an object")
    extras = set(payload) - {"schema_version", "question_pack_id", "evidence"}
    if extras:
        raise ValueError(f"secondary evidence unexpected key: {sorted(extras)[0]}")
    if payload.get("schema_version") != 1 or payload.get("question_pack_id") != question_pack.pack_id:
        raise ValueError("secondary evidence identity invalid")
    rows = payload.get("evidence")
    if not isinstance(rows, list):
        raise ValueError("secondary evidence must be a list")
    expected = {question.question_id for question in question_pack.questions}
    seen: set[str] = set()
    items: list[SecondaryEvidenceItem] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"evidence[{index}] must be an object")
        extras = set(row) - {"question_id", "claims", "source_refs"}
        if extras:
            raise ValueError(f"evidence[{index}] unexpected key: {sorted(extras)[0]}")
        question_id = row.get("question_id")
        if not isinstance(question_id, str) or question_id not in expected or question_id in seen:
            raise ValueError(f"evidence[{index}].question_id invalid")
        seen.add(question_id)
        items.append(
            SecondaryEvidenceItem(
                question_id=question_id,
                claims=_strict_string_list(row.get("claims"), f"evidence[{index}].claims"),
                source_refs=_strict_string_list(row.get("source_refs"), f"evidence[{index}].source_refs"),
            )
        )
    if seen != expected:
        raise ValueError("secondary evidence does not cover every question")
    return SecondaryEvidence(question_pack.pack_id, tuple(items))


def _validate_primary_integration(
    payload: object,
    *,
    question_pack: QuestionPack,
    secondary_evidence_hash: str,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("primary integration must be an object")
    extras = set(payload) - {
        "schema_version",
        "question_pack_id",
        "secondary_evidence_hash",
        "resolutions",
        "artifacts",
    }
    if extras:
        raise ValueError(f"primary integration unexpected key: {sorted(extras)[0]}")
    if payload.get("schema_version") != 1:
        raise ValueError("primary integration schema invalid")
    if payload.get("question_pack_id") != question_pack.pack_id:
        raise ValueError("primary integration pack mismatch")
    if payload.get("secondary_evidence_hash") != secondary_evidence_hash:
        raise ValueError("primary integration evidence hash mismatch")
    rows = payload.get("resolutions")
    if not isinstance(rows, list):
        raise ValueError("primary integration resolutions must be a list")
    expected = {question.question_id for question in question_pack.questions}
    seen: set[str] = set()
    normalized: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {
            "question_id",
            "decision",
            "artifact_kind",
            "artifact_refs",
        }:
            raise ValueError(f"resolutions[{index}] invalid keys")
        question_id = row.get("question_id")
        decision = row.get("decision")
        if not isinstance(question_id, str) or question_id not in expected or question_id in seen:
            raise ValueError(f"resolutions[{index}].question_id invalid")
        if not isinstance(decision, str) or not decision.strip():
            raise ValueError(f"resolutions[{index}].decision invalid")
        artifact_kind = row.get("artifact_kind")
        if artifact_kind not in PLANNING_KINDS:
            raise ValueError(f"resolutions[{index}].artifact_kind invalid")
        question = next(item for item in question_pack.questions if item.question_id == question_id)
        if question.kind.startswith("missing-") and artifact_kind != question.kind.removeprefix("missing-"):
            raise ValueError(f"resolutions[{index}].artifact_kind mismatch")
        seen.add(question_id)
        normalized.append(
            {
                "question_id": question_id,
                "decision": decision.strip(),
                "artifact_kind": artifact_kind,
                "artifact_refs": list(
                    _strict_string_list(row.get("artifact_refs"), f"resolutions[{index}].artifact_refs")
                ),
            }
        )
    if seen != expected:
        raise ValueError("primary integration does not resolve every question")
    artifacts_raw = payload.get("artifacts", [])
    if not isinstance(artifacts_raw, list):
        raise ValueError("primary integration artifacts must be a list")
    artifacts: list[dict[str, str]] = []
    for index, row in enumerate(artifacts_raw):
        if not isinstance(row, dict) or set(row) != {"kind", "path", "content"}:
            raise ValueError(f"artifacts[{index}] invalid keys")
        kind = row.get("kind")
        path = row.get("path")
        content = row.get("content")
        if kind not in PLANNING_KINDS or not isinstance(path, str) or not path or not isinstance(content, str):
            raise ValueError(f"artifacts[{index}] invalid")
        artifacts.append({"kind": str(kind), "path": path, "content": content})
    referenced = {
        ref for resolution in normalized for ref in resolution["artifact_refs"]
    }
    if artifacts and {item["path"] for item in artifacts} != referenced:
        raise ValueError("primary integration artifact content/ref mismatch")
    return {
        "schema_version": 1,
        "question_pack_id": question_pack.pack_id,
        "secondary_evidence_hash": secondary_evidence_hash,
        "resolutions": normalized,
        "artifacts": artifacts,
    }


# --- issue #515：post-integration artifact 檢查的 14 個裸 return None ----------
#
# 修法前這個函式的每一條失敗路徑都是 `return None`：symlink、路徑逃逸、非一般
# 檔案、UTF-8 解碼失敗、`assess_planning_artifact()` 判定不合格……語意完全不同
# 的失敗全部塌縮成同一個回傳值，呼叫端只能給出 `primary-artifact-invalid` 五個
# 字。operator 因此無法分辨究竟是「planner 產出的內容不合驗收條件」（可改
# prompt／需人工裁決）還是「檔案系統層的路徑／權限／編碼問題」（環境問題，
# 處置完全不同），也不知道是哪一個 artifact。
#
# 這正是 #397／#408 已針對「裸吞分支」做過兩輪儀器化、本函式是同一類規模最大
# 的殘留。改法與那兩輪一致：把失敗升格成帶原因的值。
#
# **範圍**：只改「回傳什麼」，不改「呼叫端據此做什麼」——`primary-artifact-invalid`
# 仍然 needs_human＋rollback_publication，classification 仍然走既有判準。
@dataclass(frozen=True)
class ArtifactEvidenceFailure:
    """一條 post-integration 檢查失敗的結構化原因。"""

    reason: str
    detail: str
    ref: str | None = None
    assessment: ArtifactAssessment | None = None

    def rendered(self) -> str:
        """壓成單行 reason 字串，供 `BrainstormResult.reason` 使用。

        欄位順序刻意排成 reason → ref → detail：上游
        `manager._publish_planning_artifacts` 的訊息會再被
        `str(exc)[:160]` 截斷一次（見 #513 的教訓），最短、最關鍵的分類碼與
        路徑必須排在最前面才能存活。
        """

        parts = [self.reason]
        if self.ref is not None:
            parts.append(f"ref={self.ref}")
        parts.append(self.detail)
        return " ".join(" ".join(str(part).split()) for part in parts)


# 內容類：planner 寫出來的東西不合驗收條件——改 prompt／人工裁決可能有效。
ARTIFACT_EVIDENCE_CONTENT_REASONS = frozenset(
    {
        "artifact-assessment-rejected",
        "post-integration-incomplete",
        "integration-resolutions-invalid",
    }
)
# 環境類：檔案系統／編碼層面的問題——重跑同一個 planner 不會有幫助。
ARTIFACT_EVIDENCE_ENVIRONMENT_REASONS = frozenset(
    {
        "artifact-root-unresolvable",
        "artifact-path-escapes-root",
        "artifact-symlink-rejected",
        "artifact-not-a-regular-file",
        "artifact-unreadable",
    }
)


def _post_integration_artifact_evidence(
    integration: Mapping[str, object],
    artifact_root: str | Path,
    original_report: CompletenessReport,
    *,
    rejection_recorder: Callable[[ArtifactAssessment], str | None] | None = None,
) -> tuple[dict[str, str], ...] | ArtifactEvidenceFailure:
    """回傳 artifact evidence，或一條說明「為什麼不行」的 :class:`ArtifactEvidenceFailure`。

    ``rejection_recorder`` 由呼叫端注入（manager 會傳
    ``_record_planning_artifact_rejection_evidence`` 的閉包），讓
    assessment 類拒收沿用 #513 的 ``cortex-planning-artifact-rejection/v1``
    evidence 落檔——被拒內容會在稍後的 ``rollback_publication()`` 被撤下，不先
    存一份 operator 就再也看不到 planner 到底寫了什麼。
    """

    try:
        root = Path(artifact_root).resolve()
    except OSError as exc:
        return ArtifactEvidenceFailure(
            "artifact-root-unresolvable",
            f"artifact root 無法解析: {type(exc).__name__}: {str(exc)[:120]}",
        )
    rows = integration.get("resolutions")
    if not isinstance(rows, list):
        return ArtifactEvidenceFailure(
            "integration-resolutions-invalid", "integration.resolutions 不是 list"
        )
    integrated: dict[str, PlanningArtifact] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return ArtifactEvidenceFailure(
                "integration-resolutions-invalid", f"resolutions[{index}] 不是 object"
            )
        kind = row.get("artifact_kind")
        refs = row.get("artifact_refs")
        if kind not in PLANNING_KINDS:
            return ArtifactEvidenceFailure(
                "integration-resolutions-invalid",
                f"resolutions[{index}].artifact_kind 非法: {kind!r}",
            )
        if not isinstance(refs, list) or not refs:
            return ArtifactEvidenceFailure(
                "integration-resolutions-invalid",
                f"resolutions[{index}].artifact_refs 必須是非空 list",
            )
        for ref in refs:
            if not isinstance(ref, str) or not ref.strip():
                return ArtifactEvidenceFailure(
                    "integration-resolutions-invalid",
                    f"resolutions[{index}].artifact_refs 含空白或非字串項目",
                )
            relative = Path(ref)
            if relative.is_absolute() or ".." in relative.parts:
                return ArtifactEvidenceFailure(
                    "artifact-path-escapes-root",
                    "artifact ref 為絕對路徑或含 `..`，逃出 artifact root",
                    ref=ref,
                )
            try:
                unresolved = root / relative
                if unresolved.is_symlink():
                    return ArtifactEvidenceFailure(
                        "artifact-symlink-rejected",
                        "artifact 路徑是 symlink（發佈鏈路一律拒收）",
                        ref=ref,
                    )
                path = unresolved.resolve()
                path.relative_to(root)
                if path.is_symlink() or not path.is_file():
                    return ArtifactEvidenceFailure(
                        "artifact-not-a-regular-file",
                        "artifact 解析後不是一般檔案（symlink／目錄／不存在）",
                        ref=ref,
                    )
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                return ArtifactEvidenceFailure(
                    "artifact-unreadable",
                    f"讀取 artifact 失敗: {type(exc).__name__}: {str(exc)[:120]}",
                    ref=ref,
                )
            artifact = PlanningArtifact(kind=str(kind), ref=ref, text=text)
            assessment = assess_planning_artifact(artifact)
            if not assessment.accepted:
                return _artifact_assessment_failure(assessment, rejection_recorder)
            integrated[ref] = artifact

    post_integration: dict[str, PlanningArtifact] = {}
    for assessment_row in original_report.assessments:
        original = assessment_row.artifact
        relative = Path(original.ref)
        if relative.is_absolute() or ".." in relative.parts:
            return ArtifactEvidenceFailure(
                "artifact-path-escapes-root",
                "既有 artifact ref 為絕對路徑或含 `..`，逃出 artifact root",
                ref=original.ref,
            )
        replacement = integrated.get(original.ref)
        if replacement is not None:
            post_integration[original.ref] = PlanningArtifact(
                kind=original.kind,
                ref=original.ref,
                text=replacement.text,
            )
            continue
        unresolved = root / relative
        try:
            if unresolved.is_symlink() or not unresolved.is_file():
                return ArtifactEvidenceFailure(
                    "artifact-not-a-regular-file",
                    "整合階段未覆寫的既有 artifact 不是一般檔案（symlink／不存在）",
                    ref=original.ref,
                )
            resolved = unresolved.resolve()
            resolved.relative_to(root)
            post_integration[original.ref] = PlanningArtifact(
                kind=original.kind,
                ref=original.ref,
                text=resolved.read_text(encoding="utf-8"),
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return ArtifactEvidenceFailure(
                "artifact-unreadable",
                f"讀取既有 artifact 失敗: {type(exc).__name__}: {str(exc)[:120]}",
                ref=original.ref,
            )
    for ref, artifact in integrated.items():
        post_integration.setdefault(ref, artifact)
    final_artifacts: list[PlanningArtifact] = []
    artifact_evidence: list[dict[str, str]] = []
    for artifact in sorted(post_integration.values(), key=lambda item: (item.kind, item.ref)):
        try:
            relative = Path(artifact.ref)
            unresolved = root / relative
            if unresolved.is_symlink():
                return ArtifactEvidenceFailure(
                    "artifact-symlink-rejected",
                    "落 evidence 前重讀 artifact 時發現路徑已成為 symlink",
                    ref=artifact.ref,
                )
            resolved = unresolved.resolve()
            resolved.relative_to(root)
            content = resolved.read_bytes()
            text = content.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return ArtifactEvidenceFailure(
                "artifact-unreadable",
                f"落 evidence 前重讀 artifact 失敗: {type(exc).__name__}: {str(exc)[:120]}",
                ref=artifact.ref,
            )
        final_artifacts.append(PlanningArtifact(kind=artifact.kind, ref=artifact.ref, text=text))
        artifact_evidence.append(
            {
                "kind": artifact.kind,
                "ref": artifact.ref,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    completeness = assess_planning_completeness(final_artifacts)
    if not completeness.complete:
        rejected = [
            item for item in completeness.assessments if not item.accepted
        ]
        detail = "整合後的 artifact 集合仍不完整"
        if completeness.missing_kinds:
            detail += f"；missing_kinds={','.join(completeness.missing_kinds)}"
        if rejected:
            detail += "；rejected=" + ", ".join(
                f"{item.artifact.ref}({','.join(item.reasons)})" for item in rejected[:3]
            )
        return ArtifactEvidenceFailure("post-integration-incomplete", detail)
    return tuple(artifact_evidence)


def _artifact_assessment_failure(
    assessment: ArtifactAssessment,
    rejection_recorder: Callable[[ArtifactAssessment], str | None] | None,
) -> ArtifactEvidenceFailure:
    """把 `assess_planning_artifact()` 的 reasons／markers 組成結構化原因。

    格式比照 #513 在 `manager._planning_artifact_rejection_message` 定案的
    `(reasons=...; markers=Lnn:...)`，讓兩個拒收點對 operator 長得一樣。
    """

    details = [f"reasons={','.join(assessment.reasons)}"]
    markers = assessment.blocking_markers
    if markers:
        rendered = [
            f"L{marker.line}:" + " ".join(marker.text.split())[:48] for marker in markers[:3]
        ]
        if len(markers) > 3:
            rendered.append(f"+{len(markers) - 3}")
        details.append("markers=" + ", ".join(rendered))
    evidence_ref: str | None = None
    if rejection_recorder is not None:
        # evidence 記錄本身 fail-open（比照 #513 的
        # `_record_planning_artifact_rejection_evidence`）：記不下診斷不得掩蓋
        # 真正的拒收原因。
        try:
            evidence_ref = rejection_recorder(assessment)
        except Exception:  # noqa: BLE001 - evidence 記錄 fail-open
            evidence_ref = None
    if evidence_ref is not None:
        details.append(f"evidence={evidence_ref}")
    return ArtifactEvidenceFailure(
        "artifact-assessment-rejected",
        "; ".join(details),
        ref=assessment.artifact.ref,
        assessment=assessment,
    )


@dataclass(frozen=True)
class PlanningGateRefs:
    brainstorm_peer: GateEvidenceRef | None = None
    foreign_review: GateEvidenceRef | None = None
    copilot: GateEvidenceRef | None = None

    def __post_init__(self) -> None:
        expected = (
            ("brainstorm_peer", self.brainstorm_peer, "brainstorm"),
            ("foreign_review", self.foreign_review, "foreign-review"),
            ("copilot", self.copilot, "copilot"),
        )
        refs: list[str] = []
        for field, value, kind in expected:
            if value is not None and (not isinstance(value, GateEvidenceRef) or value.kind != kind):
                raise ValueError(f"planning gate {field} 必須使用 {kind} kind")
            if value is not None:
                refs.append(value.ref)
        if len(refs) != len(set(refs)):
            raise ValueError("planning gate refs must be distinct")

    def as_tuple(self) -> tuple[GateEvidenceRef, ...]:
        return tuple(
            item for item in (self.brainstorm_peer, self.foreign_review, self.copilot) if item is not None
        )


@dataclass(frozen=True)
class BrainstormResult:
    state: str
    reason: str | None
    secondary_domain: str | None
    gate_refs: PlanningGateRefs
    integration: Mapping[str, object] | None = None


def _write_immutable_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (_canonical_json(payload) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        try:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise FileExistsError(f"conflicting immutable evidence: {path}") from exc
        except OSError as read_exc:
            raise FileExistsError(f"conflicting immutable evidence: {path}") from read_exc
        return
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


SAFE_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def brainstorm_evidence_filename(
    *,
    scope: PlanningScope,
    question_pack_id: str,
    run_id: str | None = None,
) -> str:
    """brainstorm evidence 的檔名（issue #535：世代隔離的 content-addressed 命名）。

    原本檔名只由 `(scope, question_pack_id)` 決定，於是同一個 work item 的**下一
    世代**（前一世代已 abandon）重跑 brainstorm 時，落點與前代殘留檔完全相同；
    模型輸出語意相同但 byte 不同，撞上 `_PlanningPublicationTransaction.publish()`
    的 no-clobber fail-closed，新世代必然 `ValueError: planning artifact no-clobber
    conflict`。

    修法取 issue 建議的第 (b) 案——**讓命名空間帶 run identity**，而不是第 (a) 案
    的「abandon 時把前代 evidence 搬到 archive」：evidence 不可銷毀（審計不可變
    原則）意味著也不該被**搬動**，前代 run 的 `gate_refs`／`evidence_refs` 逐字
    記著絕對路徑，搬檔會讓那些稽核指標整批懸空。帶 run identity 則前代 evidence
    原位不動、原路徑仍可稽核，新世代自然不撞。

    run_id 同時進檔名（可直接看出歸屬，免去 operator 挖 mtime 對時間軸）與 hash
    輸入（避免有人手改檔名就偽造歸屬）。`run_id` 缺席或格式不安全時退回舊命名，
    保持既有呼叫端與既有殘留檔的可讀性。
    """

    identity: str | None = None
    if isinstance(run_id, str) and SAFE_RUN_ID_RE.fullmatch(run_id) is not None:
        identity = run_id
    key_payload: dict[str, object] = {
        "scope": scope.to_dict(),
        "question_pack_id": question_pack_id,
    }
    if identity is not None:
        key_payload["run_id"] = identity
    evidence_key = _hash_payload(key_payload)[:32]
    if identity is None:
        return f"brainstorm-{evidence_key}.json"
    return f"brainstorm-{identity}-{evidence_key}.json"


def run_heterogeneous_brainstorm(
    *,
    report: CompletenessReport,
    primary: tuple[str, str],
    registry: IdentityRegistry,
    probes: Mapping[tuple[str, str], CapabilityProbe],
    evidence_dir: str | Path,
    artifact_root: str | Path,
    scope: PlanningScope,
    primary_questioner: Callable[[Mapping[str, object]], object],
    secondary_planner: Callable[[Mapping[str, object], ModelIdentity], object],
    primary_integrator: Callable[[Mapping[str, object], Mapping[str, object]], object],
    artifact_writer: Callable[[object], Callable[[], None] | None] | None = None,
    evidence_writer: Callable[[Path, object], None] | None = None,
    run_id: str | None = None,
    rejection_recorder: Callable[[ArtifactAssessment], str | None] | None = None,
) -> BrainstormResult:
    empty_refs = PlanningGateRefs()
    if report.complete:
        return BrainstormResult("ready", None, None, empty_refs, None)
    selection = select_secondary_planner(registry=registry, primary=primary, probes=probes)
    if selection.state != "ready" or selection.identity is None:
        return BrainstormResult("needs_human", selection.reason, None, empty_refs, None)
    try:
        questioner_input = {
            **report.to_dict(),
            "default_question_pack": report.default_question_pack.to_dict(),
        }
        pack = validate_question_pack(primary_questioner(questioner_input), report=report)
    except Exception as exc:
        # issue #397：這三處 `except Exception` 過去把底層例外整段壓平成單一
        # 字面值 reason，操作者只看得到分支名稱、看不到底層是哪種例外、訊息
        # 內容是什麼——排障要另外重跑加 print 才查得到（曾經雙重誤導：真正
        # 原因是 planning launcher 把 operator worktree 判成被汙染而
        # ValueError，卻只顯示成籠統的「question-pack-malformed」）。這裡併入
        # 例外型別與訊息前 160 字，供 #393 的 planning-failure evidence 與
        # recover-planning 的 `_read_planning_failure_record` 直接讀出；兩者
        # 都只要求 reason 為非空字串，加長不影響既有契約。
        return BrainstormResult(
            "needs_human",
            f"question-pack-malformed: {type(exc).__name__}: {str(exc)[:160]}",
            None,
            empty_refs,
            None,
        )
    try:
        secondary = validate_secondary_evidence(
            secondary_planner(pack.to_dict(), selection.identity),
            question_pack=pack,
        )
    except Exception as exc:
        return BrainstormResult(
            "needs_human",
            f"secondary-output-malformed: {type(exc).__name__}: {str(exc)[:160]}",
            selection.identity.independence_domain,
            empty_refs,
            None,
        )
    secondary_payload = secondary.to_dict()
    evidence_hash = _hash_payload(secondary_payload)
    callback_payload = {**secondary_payload, "evidence_hash": evidence_hash}
    try:
        integration = _validate_primary_integration(
            primary_integrator(pack.to_dict(), callback_payload),
            question_pack=pack,
            secondary_evidence_hash=evidence_hash,
        )
    except Exception as exc:
        return BrainstormResult(
            "needs_human",
            f"primary-integration-malformed: {type(exc).__name__}: {str(exc)[:160]}",
            selection.identity.independence_domain,
            empty_refs,
            None,
        )
    rollback_publication: Callable[[], None] | None = None
    if artifact_writer is not None:
        try:
            if not integration.get("artifacts"):
                raise ValueError("structured artifact content missing")
            rollback_publication = artifact_writer(integration.get("artifacts", []))
        except Exception as exc:
            # #408：這是 #397 儀器化時漏掉的第四個裸吞分支——artifact write 的
            # 實際拒絕原因（哪條驗證、哪個路徑）必須透傳進 reason，與其餘三個
            # 分支的例外摘要格式一致。
            return BrainstormResult(
                "needs_human",
                f"primary-artifact-write-rejected: {type(exc).__name__}: {str(exc)[:160]}",
                selection.identity.independence_domain,
                empty_refs,
                None,
            )
    artifact_evidence = _post_integration_artifact_evidence(
        integration,
        artifact_root,
        report,
        rejection_recorder=rejection_recorder,
    )
    if isinstance(artifact_evidence, ArtifactEvidenceFailure):
        if rollback_publication is not None:
            rollback_publication()
        # #515：`primary-artifact-invalid` 前綴保留（既有測試與 operator 的
        # grep 習慣以它為錨點），但後面必須帶得出「哪一個 artifact、哪一條
        # 判準」——過去這裡是一個沒有任何附加資訊的字面值。
        return BrainstormResult(
            "needs_human",
            f"primary-artifact-invalid: {artifact_evidence.rendered()}",
            selection.identity.independence_domain,
            empty_refs,
            None,
        )
    evidence_payload = {
        "schema_version": BRAINSTORM_EVIDENCE_SCHEMA_VERSION,
        "kind": "brainstorm-peer",
        "scope": scope.to_dict(),
        "question_pack": pack.to_dict(),
        "secondary_identity": selection.identity.legacy_dict(),
        "secondary_evidence": secondary_payload,
        "secondary_evidence_hash": evidence_hash,
        "primary_integration": integration,
        "artifacts": list(artifact_evidence),
    }
    evidence_path = Path(evidence_dir) / brainstorm_evidence_filename(
        scope=scope, question_pack_id=pack.pack_id, run_id=run_id
    )
    try:
        if evidence_writer is None:
            _write_immutable_json(evidence_path, evidence_payload)
        else:
            evidence_writer(evidence_path, evidence_payload)
    except FileExistsError:
        if rollback_publication is not None:
            rollback_publication()
        return BrainstormResult(
            "needs_human",
            "brainstorm-evidence-conflict",
            selection.identity.independence_domain,
            empty_refs,
            None,
        )
    except OSError:
        if rollback_publication is not None:
            rollback_publication()
        return BrainstormResult(
            "needs_human",
            "brainstorm-evidence-write-failed",
            selection.identity.independence_domain,
            empty_refs,
            None,
        )
    refs = PlanningGateRefs(
        brainstorm_peer=GateEvidenceRef(
            kind="brainstorm",
            ref=str(evidence_path),
            sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        )
    )
    return BrainstormResult(
        "ready",
        None,
        selection.identity.independence_domain,
        refs,
        integration,
    )
