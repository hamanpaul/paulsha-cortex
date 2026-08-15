"""v4 R1（方案 A）：responsibility **coverage** validator 的 shadow 骨架。

## 為什麼要有這個模組

v4 的核心論點：safety truth 不該依賴 ``current_phase``（workflow topology），
而該依賴「責任覆蓋」（responsibility coverage）。今日 production 的唯一真相源是
:meth:`WorkflowManifest.validate_manager_spine`——它驗的是**執行拓撲**（phase 單調
排列、phase↔persona 綁定、ship 前有 review step）。本模組新增一個**獨立**的
coverage validator，從「這份 workflow 是否覆蓋每一個必要 safety responsibility」
的角度重新判定同一份 manifest。

## R1 的鐵律：零行為變更（shadow only）

R1 是重構的**觀測期**，不是切換期：

1. **topology validator 仍是 production 唯一真相源**——``validate_manager_spine()``
   一個 byte 都沒動，所有 gate/dispatch/acceptance 決策仍完全由它主導。
2. **coverage validator 的判定只進 telemetry**——:func:`run_coverage_shadow` 在
   production 呼叫點旁並行跑兩個 validator、比對、把 disagreement 落成結構化
   telemetry，供兩週觀測期累積資料後再決定是否進 R2 啟用。
3. **shadow 絕不影響 production**——:func:`run_coverage_shadow` 全程包在
   ``try/except`` 內、永不 raise，且受 ``PSC_RESPONSIBILITY_COVERAGE`` 閘控
   （``off`` 連比對都不跑）。無論 coverage validator 判 pass 或 fail，後續的
   ``validate_manager_spine()`` 呼叫結果與本模組落地前**逐位元組相同**。

## R1 的 scope 邊界（誠實聲明）

- coverage validator 讀的是 **manifest 的 step.phase**，經
  :data:`_PHASE_TO_STAGE`（legacy ``phase → responsibility`` adapter）投影成
  :class:`SafetyStage`。manifest step 今日**不**攜帶 ``satisfies``——把它投影進
  manifest 會改變 manifest 的序列化 bytes（= 行為變更），屬 R2。
- Card 的 optional ``satisfies``（見 ``deck/schema.py``）是**宣告 seam**：它是
  capability declaration（「這張卡負責哪個 responsibility」），**不是**
  self-certification。:func:`resolve_card_satisfies` 是它的 deck-side adapter——
  有宣告用宣告、沒宣告則從 ``phase`` 推導。R1 完整驗證並單測這個 adapter，但
  **尚未**把它 projection 進 manifest（那是 R2 的責任契約）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from paulsha_cortex.config.paths import coverage_shadow_telemetry_root

from .workflow import WORKFLOW_PHASES, WorkflowManifest

logger = logging.getLogger(__name__)

#: telemetry 落檔的 schema 版本（獨立於 manifest schema）。
SHADOW_TELEMETRY_SCHEMA = "1"

#: 回滾開關的環境變數名。``off`` = 完全停用 shadow（連比對都不跑）；預設 ``on``。
COVERAGE_ENV_FLAG = "PSC_RESPONSIBILITY_COVERAGE"

_FILENAME_SAFE = re.compile(r"[^a-z0-9._-]+")


class SafetyStage(Enum):
    """一個 workflow 必須覆蓋的 safety **responsibility**（責任），不是執行拓撲。

    v4 constitution：「七步是 Safety Responsibility（責任覆蓋），不是 Workflow
    topology（執行拓撲）。Pattern 可以繞路解題，但不能繞過 Safety Responsibility。」
    這個列舉即那組責任的載體——與現行七 phase 一一對映（見 :data:`_PHASE_TO_STAGE`），
    但語意是「責任是否被滿足」而非「這個 phase 是否照拓撲順序出現」。

    ``INTAKE`` 與 ``DELIVERY`` 是 Manager-owned authority boundary（constitution
    decision 3：Claim/Ship 為 Manager 的分權邊界，只收斂不放寬）——見
    :data:`MANAGER_AUTHORITY_STAGES`。這對映關係也預留 R2 的
    Compact/Standard/Elevated 責任分級：Compact 仍要求全部責任，只是 Define/Plan
    可由既有可信 evidence 覆蓋（本 R1 不實作 reuse，僅建立責任列舉本身）。
    """

    INTAKE = "intake"  # claim：Manager 授權邊界——工作在授權下被納入
    SPECIFICATION = "specification"  # define：問題／需求被界定
    PLANNING = "planning"  # plan：解法被規劃
    IMPLEMENTATION = "implementation"  # build：變更被實作
    VERIFICATION = "verification"  # verify：變更被驗證（測試／檢查）
    REVIEW = "review"  # review：獨立審查
    DELIVERY = "delivery"  # ship：Manager 授權邊界——交付


#: 全部 safety responsibility，依責任生命週期的 canonical 順序。
SAFETY_STAGES: tuple[SafetyStage, ...] = tuple(SafetyStage)

#: Manager-owned authority boundary（constitution decision 3）。
MANAGER_AUTHORITY_STAGES: frozenset[SafetyStage] = frozenset(
    {SafetyStage.INTAKE, SafetyStage.DELIVERY}
)

#: legacy ``phase → responsibility`` adapter：讓不帶顯式 ``satisfies`` 的現行 card／
#: manifest（step 只帶 phase）也能被 coverage validator 讀。key 為 WORKFLOW_PHASES
#: 的 phase 字面值，與 ``deck/compile.py`` 的 phase 產線同一組約定。
_PHASE_TO_STAGE: dict[str, SafetyStage] = {
    "claim": SafetyStage.INTAKE,
    "define": SafetyStage.SPECIFICATION,
    "plan": SafetyStage.PLANNING,
    "build": SafetyStage.IMPLEMENTATION,
    "verify": SafetyStage.VERIFICATION,
    "review": SafetyStage.REVIEW,
    "ship": SafetyStage.DELIVERY,
}

# build-time 完整性檢查：phase↔stage 必須雙滿射，任何一邊漏掉都是編碼錯誤。
assert set(_PHASE_TO_STAGE) == set(WORKFLOW_PHASES), "phase↔stage 對映不完整"
assert set(_PHASE_TO_STAGE.values()) == set(SAFETY_STAGES), "stage 未被 phase 全覆蓋"

#: coverage validator 認得的 responsibility 名稱（deck ``satisfies`` 宣告的合法值域）。
SAFETY_STAGE_NAMES: frozenset[str] = frozenset(stage.value for stage in SAFETY_STAGES)


def stage_for_phase(phase: str) -> SafetyStage:
    """legacy adapter：把一個 workflow phase 投影成它負責的 safety responsibility。"""
    try:
        return _PHASE_TO_STAGE[phase]
    except KeyError as exc:  # pragma: no cover - phase 合法性由 WorkflowStep 保證
        raise ValueError(f"未知 workflow phase，無法對映 safety stage: {phase!r}") from exc


def resolve_card_satisfies(card: Any) -> tuple[SafetyStage, ...]:
    """Card 的 ``satisfies`` adapter：**宣告優先、phase 兜底**。

    - 若 card 顯式宣告 ``satisfies``（capability declaration），以宣告為準（去重、
      保序）；未知的 responsibility 名稱一律略過（coverage validator 是責任名稱的
      唯一權威，deck 層刻意不驗語意，見 ``deck/schema.py`` 對 ``satisfies`` 的註解）。
    - 否則從 ``card.phase`` 經 :func:`stage_for_phase` 推導——這正是「現有不帶
      ``satisfies`` 的 card 也能被 coverage validator 讀」的兜底路徑。

    **這是宣告 seam，不是 self-certification**：它只表達「這張卡宣稱負責哪個
    responsibility」，覆蓋是否真的成立由 evidence 決定（R2）。
    """
    declared = getattr(card, "satisfies", ()) or ()
    stages: list[SafetyStage] = []
    for name in declared:
        if name in SAFETY_STAGE_NAMES:
            stage = SafetyStage(name)
            if stage not in stages:
                stages.append(stage)
    if stages:
        return tuple(stages)
    phase = getattr(card, "phase", None)
    if phase in _PHASE_TO_STAGE:
        return (_PHASE_TO_STAGE[phase],)
    return ()


@dataclass(frozen=True)
class ResponsibilityCoverage:
    """一份 workflow 是否覆蓋每個必要 safety responsibility 的結構。

    - ``required``：本次判定要求覆蓋的責任集合（R1 shadow 一律為全部
      :data:`SAFETY_STAGES`，與 topology validator「必須涵蓋完整 phase spine」對齊）。
    - ``covered``：實際被至少一個 step 覆蓋到的責任。
    - ``missing``：``required`` 中未被覆蓋者（依 canonical 順序）。
    - ``satisfied_by``：每個責任由哪些 card id 覆蓋（供 disagreement 溯源）。
    """

    required: frozenset[SafetyStage]
    covered: frozenset[SafetyStage]
    missing: tuple[SafetyStage, ...]
    satisfied_by: Mapping[SafetyStage, tuple[str, ...]]

    @property
    def is_complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": [stage.value for stage in SAFETY_STAGES if stage in self.required],
            "covered": [stage.value for stage in SAFETY_STAGES if stage in self.covered],
            "missing": [stage.value for stage in self.missing],
            "satisfied_by": {
                stage.value: list(self.satisfied_by.get(stage, ()))
                for stage in SAFETY_STAGES
                if stage in self.covered
            },
        }


@dataclass(frozen=True)
class Verdict:
    """一個 validator 的 pass/fail 判定＋原因（fail 時為訊息，pass 時為 None）。"""

    passed: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reason": self.reason}


@dataclass(frozen=True)
class ShadowComparison:
    """一次 shadow 比對的完整結果（純資料，供落 telemetry 與測試斷言）。"""

    manifest_combo: str
    manifest_task_slug: str
    manifest_version: int
    step_count: int
    topology: Verdict
    coverage_verdict: Verdict
    coverage: ResponsibilityCoverage
    callsite: str
    context: Mapping[str, Any]
    recorded_at: str

    @property
    def agreement(self) -> bool:
        return self.topology.passed == self.coverage_verdict.passed

    @property
    def disagreement_kind(self) -> str | None:
        if self.agreement:
            return None
        if self.topology.passed and not self.coverage_verdict.passed:
            return "topology-pass-coverage-fail"
        return "topology-fail-coverage-pass"

    def to_dict(self) -> dict[str, Any]:
        disagreement: dict[str, Any] | None = None
        if not self.agreement:
            disagreement = {
                "kind": self.disagreement_kind,
                "topology_reason": self.topology.reason,
                "coverage_reason": self.coverage_verdict.reason,
                "missing_responsibilities": [stage.value for stage in self.coverage.missing],
            }
        return {
            "schema_version": SHADOW_TELEMETRY_SCHEMA,
            "recorded_at": self.recorded_at,
            "callsite": self.callsite,
            "manifest": {
                "combo": self.manifest_combo,
                "task_slug": self.manifest_task_slug,
                "version": self.manifest_version,
                "steps": self.step_count,
            },
            "context": dict(self.context),
            "topology": self.topology.to_dict(),
            "coverage": {
                **self.coverage_verdict.to_dict(),
                **self.coverage.to_dict(),
            },
            "agreement": self.agreement,
            "disagreement": disagreement,
        }


# ---------------------------------------------------------------------------
# validators
# ---------------------------------------------------------------------------


def topology_verdict(manifest: WorkflowManifest) -> Verdict:
    """production topology validator 的 pass/fail **觀測**（不改變其行為）。

    刻意直接呼叫 ``validate_manager_spine()`` 並捕捉其例外——如此 shadow 對 topology
    的判定與 production **就是同一份邏輯**，不另抄一份、不可能 drift。
    """
    try:
        manifest.validate_manager_spine()
    except ValueError as exc:
        return Verdict(passed=False, reason=str(exc))
    return Verdict(passed=True, reason=None)


def evaluate_coverage(
    manifest: WorkflowManifest,
    *,
    required: Iterable[SafetyStage] = SAFETY_STAGES,
) -> ResponsibilityCoverage:
    """coverage validator：從責任覆蓋角度獨立判定同一份 manifest。

    R1 shadow 讀 manifest 的 ``step.phase``，經 legacy adapter 投影成
    :class:`SafetyStage`，記錄每個責任由哪些 card 覆蓋，再對照 ``required`` 算出
    ``missing``。**與 topology 無關**：不看順序、不看 persona 綁定、不看 ship 前是否
    有 review step——只問「每個必要責任是否被某個 step 覆蓋」。
    """
    required_set = frozenset(required)
    satisfied_by: dict[SafetyStage, list[str]] = {}
    for step in manifest.steps:
        stage = stage_for_phase(step.phase)
        satisfied_by.setdefault(stage, [])
        if step.card not in satisfied_by[stage]:
            satisfied_by[stage].append(step.card)
    covered = frozenset(satisfied_by)
    missing = tuple(stage for stage in SAFETY_STAGES if stage in required_set and stage not in covered)
    return ResponsibilityCoverage(
        required=required_set,
        covered=covered,
        missing=missing,
        satisfied_by={stage: tuple(cards) for stage, cards in satisfied_by.items()},
    )


def coverage_verdict(coverage: ResponsibilityCoverage) -> Verdict:
    """把 :class:`ResponsibilityCoverage` 收斂成 pass/fail 判定。"""
    if coverage.is_complete:
        return Verdict(passed=True, reason=None)
    missing = ", ".join(stage.value for stage in coverage.missing)
    return Verdict(passed=False, reason=f"未覆蓋的 safety responsibility: {missing}")


# ---------------------------------------------------------------------------
# shadow 比對＋telemetry
# ---------------------------------------------------------------------------


def shadow_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """回滾開關：``PSC_RESPONSIBILITY_COVERAGE=off`` → False（連比對都不跑）。

    未設或設為 ``off`` 以外的值 → True（預設 shadow）。``off`` 判定 case-insensitive、
    去前後空白。
    """
    env = os.environ if environment is None else environment
    return env.get(COVERAGE_ENV_FLAG, "").strip().lower() != "off"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def compare_manifest(
    manifest: WorkflowManifest,
    *,
    callsite: str = "unknown",
    context: Mapping[str, Any] | None = None,
    recorded_at: str | None = None,
) -> ShadowComparison:
    """純函式：跑兩個 validator 並打包成 :class:`ShadowComparison`（不落檔）。"""
    topo = topology_verdict(manifest)
    coverage = evaluate_coverage(manifest)
    return ShadowComparison(
        manifest_combo=manifest.combo,
        manifest_task_slug=manifest.task_slug,
        manifest_version=manifest.version,
        step_count=len(manifest.steps),
        topology=topo,
        coverage_verdict=coverage_verdict(coverage),
        coverage=coverage,
        callsite=callsite,
        context=dict(context or {}),
        recorded_at=recorded_at or _utcnow(),
    )


def _record_filename(comparison: ShadowComparison) -> str:
    flat_ts = _FILENAME_SAFE.sub("", comparison.recorded_at.lower())
    slug = _FILENAME_SAFE.sub("-", comparison.manifest_task_slug.lower()).strip("-") or "manifest"
    return f"{flat_ts}-{slug}-{uuid.uuid4().hex[:8]}.json"


def write_shadow_record(
    comparison: ShadowComparison,
    *,
    root: str | Path | None = None,
) -> Path | None:
    """原子寫入一則 shadow telemetry 記錄；**永不 raise**，失敗回 ``None``。

    比照 ``monitor/event_spool.py`` 的寫入端語意（一檔一記錄、``.`` 前綴 temp 檔 →
    fsync → ``os.replace``，因此不需鎖、consumer 不可能讀到半寫入檔）。shadow 掛在
    production 派工路徑上，寫不進去絕不能影響派工本體——所以吞掉全部例外、記 debug。
    """
    directory = Path(root) if root is not None else coverage_shadow_telemetry_root()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        body = (json.dumps(comparison.to_dict(), ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        handle_fd, temp_name = tempfile.mkstemp(prefix=".coverage-", suffix=".tmp", dir=directory)
        temp_path = Path(temp_name)
        try:
            os.fchmod(handle_fd, 0o600)
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            final_path = directory / _record_filename(comparison)
            os.replace(temp_path, final_path)
        except BaseException:
            try:
                os.close(handle_fd)
            except OSError:
                pass
            temp_path.unlink(missing_ok=True)
            raise
    except Exception as error:  # noqa: BLE001 - fire-and-forget 的全部意義
        logger.debug("coverage shadow telemetry write dropped: %s", error)
        return None
    return final_path


def run_coverage_shadow(
    manifest: WorkflowManifest,
    *,
    callsite: str = "unknown",
    context: Mapping[str, Any] | None = None,
    root: str | Path | None = None,
) -> ShadowComparison | None:
    """production 呼叫點旁的 shadow 入口——**永不影響 production**。

    語意合約（R1 鐵律）：

    1. ``PSC_RESPONSIBILITY_COVERAGE=off`` → 直接回 ``None``，連比對都不跑。
    2. 否則跑 coverage validator 與 topology validator（後者只是**觀測**現行
       ``validate_manager_spine()``），比對後把結果落成 telemetry。
    3. **全程 try/except、永不 raise**——任何 shadow 內部錯誤（比對 bug、寫檔失敗、
       telemetry 目錄不可寫……）一律吞成 debug log，讓呼叫端後續的
       ``validate_manager_spine()`` 照舊執行、結果逐位元組不變。

    回傳 :class:`ShadowComparison`（供測試斷言）或 ``None``（停用／發生被吞的例外）。
    coverage validator 的判定**只**在這裡進 telemetry，絕不回傳給任何 gate 消費。
    """
    try:
        if not shadow_enabled():
            return None
        comparison = compare_manifest(manifest, callsite=callsite, context=context)
        write_shadow_record(comparison, root=root)
        if not comparison.agreement:
            logger.info(
                "coverage shadow disagreement (%s): topology.passed=%s coverage.passed=%s missing=%s",
                comparison.disagreement_kind,
                comparison.topology.passed,
                comparison.coverage_verdict.passed,
                [stage.value for stage in comparison.coverage.missing],
            )
        return comparison
    except Exception as error:  # noqa: BLE001 - shadow 絕不影響 production
        logger.debug("coverage shadow skipped after error: %s", error)
        return None
