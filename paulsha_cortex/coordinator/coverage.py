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

## Go/No-Go 的讀端：aggregation reader

R1 的 Go/No-Go 判準是「兩週 telemetry 中所有 disagreement 可解釋」，因此 sink
本身不夠——還需要一支把整個 ``coverage-shadow/`` 目錄收斂成統計的**唯讀** reader：

    python -m paulsha_cortex.coordinator.coverage --report [--json]

:func:`build_shadow_report` 是它的純函式核心：總筆數／agreement 比例／
disagreement 依 kind 分組（理論上只有 ``topology-fail-coverage-pass``）／每組的
combo・task_slug・callsite 分佈與樣本明細（含 ``satisfied_by``，足供人工逐筆解釋）。
單筆 JSON 壞損只跳過並計數，絕不炸掉整份報告——telemetry 是觀測資料，一顆壞檔讓
Go/No-Go 讀不出來是完全不成比例的代價。

reader 同時順帶做 **TTL 清掃**（:data:`DEFAULT_SHADOW_TTL_SECONDS`，預設 30 天，
比照 D4 event spool 的 ``DEFAULT_EVENT_TTL_SECONDS`` 慣例）：**只在 reader 執行時
清**，不引入任何常駐 daemon 邏輯。刪不掉（唯讀掛載、Phase 2 之後 Manager-owned
樹對 operator 唯讀）時只計數不 raise，report 照樣讀得出來。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

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


# ---------------------------------------------------------------------------
# aggregation reader ＋ retention（R1 Go/No-Go 的直接輸入）
# ---------------------------------------------------------------------------

#: 彙總報告的 schema 版本（獨立於單筆 telemetry 的 :data:`SHADOW_TELEMETRY_SCHEMA`）。
SHADOW_REPORT_SCHEMA = "1"

#: shadow telemetry 的預設保留期。比照 D4 event spool 的
#: ``DEFAULT_EVENT_TTL_SECONDS``：TTL 只在 reader 執行時順帶清掃，不加常駐 daemon。
DEFAULT_SHADOW_TTL_SECONDS = 30 * 86_400.0

#: 每組 disagreement 預設附幾筆樣本明細（``0`` ＝全部）。
DEFAULT_SAMPLE_LIMIT = 5

#: 記錄缺 ``disagreement.kind``（舊 schema／半截資料）時的分組名。
UNKNOWN_DISAGREEMENT_KIND = "unknown"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    """寬鬆解析 ISO-8601 時間戳；解析不出來回 ``None`` 而非 raise。

    比 ``monitor/event_spool.py`` 的 :func:`parse_event_timestamp` 寬鬆一級：那邊是
    寫入端契約（壞掉就該隔離），這邊是**唯讀 reader**，任何一筆解析不出來都只能降級
    成「用檔案 mtime 判齡」，絕不能讓整份報告讀不出來。
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_name(value: object) -> str:
    """把任意欄位值收斂成分佈統計用的 key（缺漏／型別不對一律 ``-``）。"""
    return value if isinstance(value, str) and value else "-"


def _ranked(counter: Counter[str]) -> dict[str, int]:
    """分佈計數的 canonical 排序：count 由大到小，同 count 依名稱字典序。"""
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


@dataclass(frozen=True)
class DisagreementGroup:
    """同一個 ``disagreement.kind`` 的全部記錄之彙總。

    ``combos`` / ``task_slugs`` / ``callsites`` / ``missing_responsibilities`` 是分佈
    計數（供「這族 disagreement 集中在哪些 manifest／呼叫點」的判讀），``samples`` 是
    逐筆明細（供人工解釋單一案例）。
    """

    kind: str
    count: int
    combos: Mapping[str, int]
    task_slugs: Mapping[str, int]
    callsites: Mapping[str, int]
    missing_responsibilities: Mapping[str, int]
    samples: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "count": self.count,
            "combos": dict(self.combos),
            "task_slugs": dict(self.task_slugs),
            "callsites": dict(self.callsites),
            "missing_responsibilities": dict(self.missing_responsibilities),
            "samples": [dict(sample) for sample in self.samples],
        }


@dataclass(frozen=True)
class ShadowReport:
    """整個 ``coverage-shadow/`` 目錄的彙總——R1 Go/No-Go 判讀的單一輸入。"""

    root: Path
    generated_at: str
    root_exists: bool
    root_error: str | None
    total: int
    agreements: int
    disagreements: int
    groups: tuple[DisagreementGroup, ...]
    corrupt: tuple[str, ...]
    swept: tuple[str, ...]
    sweep_failed: tuple[str, ...]
    ttl_seconds: float | None
    earliest: str | None
    latest: str | None

    @property
    def agreement_rate(self) -> float | None:
        """agreement 佔比（``total`` 為 0 時為 ``None``，不編造 100%）。"""
        if self.total <= 0:
            return None
        return self.agreements / self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SHADOW_REPORT_SCHEMA,
            "generated_at": self.generated_at,
            "root": str(self.root),
            "root_exists": self.root_exists,
            "root_error": self.root_error,
            "records": {
                "total": self.total,
                "agreement": self.agreements,
                "disagreement": self.disagreements,
                "agreement_rate": self.agreement_rate,
            },
            "window": {"earliest": self.earliest, "latest": self.latest},
            "corrupt": {"count": len(self.corrupt), "files": list(self.corrupt)},
            "retention": {
                "ttl_seconds": self.ttl_seconds,
                "swept": {"count": len(self.swept), "files": list(self.swept)},
                "sweep_failed": {
                    "count": len(self.sweep_failed),
                    "files": list(self.sweep_failed),
                },
            },
            "disagreements": [group.to_dict() for group in self.groups],
        }

    def render_text(self) -> str:
        lines = [
            "coverage shadow telemetry 報告",
            f"root: {self.root}",
        ]
        if not self.root_exists:
            lines.append("（telemetry 目錄尚不存在——shadow 還沒寫過任何記錄）")
        if self.root_error:
            lines.append(f"（目錄讀取失敗：{self.root_error}）")
        rate = self.agreement_rate
        rate_text = "n/a" if rate is None else f"{rate * 100:.1f}%"
        lines.append(
            f"records: {self.total}"
            f"（agreement {self.agreements} / disagreement {self.disagreements}"
            f"；agreement rate {rate_text}）"
        )
        lines.append(f"window: {self.earliest or '-'} .. {self.latest or '-'}")
        lines.append(f"壞檔（跳過、未計入統計）: {len(self.corrupt)}")
        if self.ttl_seconds is None:
            lines.append("retention: 本次未清掃（--no-sweep）")
        else:
            lines.append(
                f"retention: TTL {self.ttl_seconds / 86_400:.1f} 天"
                f"；本次清掃 {len(self.swept)} 筆"
                f"；清掃失敗 {len(self.sweep_failed)} 筆"
            )
        lines.append("")
        if not self.groups:
            lines.append("disagreement 分組: （無）——所有記錄兩方判定一致。")
            return "\n".join(lines) + "\n"
        lines.append("disagreement 分組:")
        for group in self.groups:
            lines.append(f"  [{group.kind}] {group.count} 筆")
            lines.append(f"    combo:     {_render_distribution(group.combos)}")
            lines.append(f"    task_slug: {_render_distribution(group.task_slugs)}")
            lines.append(f"    callsite:  {_render_distribution(group.callsites)}")
            lines.append(
                f"    missing:   {_render_distribution(group.missing_responsibilities)}"
            )
            lines.append(f"    樣本（{len(group.samples)}／{group.count}）:")
            for index, sample in enumerate(group.samples, start=1):
                lines.extend(_render_sample(index, sample))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _render_distribution(distribution: Mapping[str, int]) -> str:
    if not distribution:
        return "（無）"
    return "  ".join(f"{key}={count}" for key, count in distribution.items())


def _render_sample(index: int, sample: Mapping[str, Any]) -> list[str]:
    context = _as_mapping(sample.get("context"))
    context_text = (
        "  ".join(f"{key}={context[key]}" for key in sorted(context)) or "（無）"
    )
    satisfied_by = _as_mapping(sample.get("satisfied_by"))
    lines = [
        f"      {index}) {sample.get('recorded_at') or '-'}"
        f"  file={sample.get('file') or '-'}",
        f"         callsite={sample.get('callsite') or '-'}"
        f"  combo={sample.get('combo') or '-'}"
        f"  task_slug={sample.get('task_slug') or '-'}"
        f"  steps={sample.get('steps')}",
        f"         context: {context_text}",
        f"         topology: {sample.get('topology_reason') or 'pass'}",
        f"         coverage: {sample.get('coverage_reason') or 'pass'}",
        f"         missing: {', '.join(sample.get('missing') or ()) or '（無）'}",
    ]
    if satisfied_by:
        rendered = "; ".join(
            f"{stage}={','.join(str(card) for card in cards)}"
            for stage, cards in satisfied_by.items()
            if isinstance(cards, (list, tuple))
        )
        lines.append(f"         satisfied_by: {rendered}")
    return lines


def _sample_of(payload: Mapping[str, Any], filename: str) -> dict[str, Any]:
    """把一筆 telemetry 投影成報告樣本——**足供人工逐筆解釋**的最小欄位集。"""
    manifest = _as_mapping(payload.get("manifest"))
    disagreement = _as_mapping(payload.get("disagreement"))
    topology = _as_mapping(payload.get("topology"))
    coverage_payload = _as_mapping(payload.get("coverage"))
    missing = disagreement.get("missing_responsibilities")
    if not isinstance(missing, list):
        missing = coverage_payload.get("missing")
    return {
        "file": filename,
        "recorded_at": _as_text(payload.get("recorded_at")),
        "callsite": _as_text(payload.get("callsite")),
        "combo": _as_text(manifest.get("combo")),
        "task_slug": _as_text(manifest.get("task_slug")),
        "steps": manifest.get("steps"),
        "context": dict(_as_mapping(payload.get("context"))),
        "topology_reason": _as_text(disagreement.get("topology_reason"))
        or _as_text(topology.get("reason")),
        "coverage_reason": _as_text(disagreement.get("coverage_reason"))
        or _as_text(coverage_payload.get("reason")),
        "missing": [str(item) for item in missing] if isinstance(missing, list) else [],
        "covered": [
            str(item) for item in coverage_payload.get("covered", []) or ()
        ],
        "satisfied_by": dict(_as_mapping(coverage_payload.get("satisfied_by"))),
    }


class _GroupAccumulator:
    """單一 disagreement kind 的可變累加器（只在 :func:`build_shadow_report` 內用）。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.count = 0
        self.combos: Counter[str] = Counter()
        self.task_slugs: Counter[str] = Counter()
        self.callsites: Counter[str] = Counter()
        self.missing: Counter[str] = Counter()
        self.samples: list[Mapping[str, Any]] = []

    def add(self, payload: Mapping[str, Any], filename: str, *, sample_limit: int) -> None:
        manifest = _as_mapping(payload.get("manifest"))
        self.count += 1
        self.combos[_as_name(manifest.get("combo"))] += 1
        self.task_slugs[_as_name(manifest.get("task_slug"))] += 1
        self.callsites[_as_name(payload.get("callsite"))] += 1
        sample = _sample_of(payload, filename)
        for stage in sample["missing"]:
            self.missing[stage] += 1
        if sample_limit <= 0 or len(self.samples) < sample_limit:
            self.samples.append(sample)

    def freeze(self) -> DisagreementGroup:
        return DisagreementGroup(
            kind=self.kind,
            count=self.count,
            combos=_ranked(self.combos),
            task_slugs=_ranked(self.task_slugs),
            callsites=_ranked(self.callsites),
            missing_responsibilities=_ranked(self.missing),
            samples=tuple(self.samples),
        )


def _record_paths(directory: Path) -> tuple[list[Path], str | None]:
    """列出目錄下的 telemetry 檔；不可讀時回 ``([], 錯誤訊息)``。

    刻意跳過 dotfile：sink 的半寫入 temp 檔是 ``.coverage-*.tmp``，與 D4 event spool
    同一個約定（掃描端跳過 dotfile ⟹ consumer 不可能讀到半寫入檔）。
    """
    try:
        entries = sorted(directory.iterdir())
    except OSError as error:
        return [], str(error)
    return [
        path
        for path in entries
        if path.suffix == ".json" and not path.name.startswith(".") and path.is_file()
    ], None


def build_shadow_report(
    *,
    root: str | Path | None = None,
    ttl_seconds: float | None = DEFAULT_SHADOW_TTL_SECONDS,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
    now: datetime | None = None,
) -> ShadowReport:
    """讀完整個 telemetry 目錄並收斂成 :class:`ShadowReport`（順帶做 TTL 清掃）。

    語意合約：

    1. **壞檔容錯**——單筆讀不到／不是 JSON／不是 object 一律跳過並計入
       ``corrupt``，絕不讓整份報告失敗。
    2. **TTL 清掃**（``ttl_seconds=None`` 停用）——記錄的 ``recorded_at`` 超過 TTL 就
       刪；``recorded_at`` 缺漏或解析不出來（含壞檔）時降級用檔案 mtime 判齡，讓壞檔
       也會隨時間退場而不是永久堆積。刪不掉只計入 ``sweep_failed``，不 raise。
    3. **被清掉的記錄不計入統計**——報告描述的是「保留窗內」的母體。
    """
    directory = Path(root) if root is not None else coverage_shadow_telemetry_root()
    horizon = now or _now_utc()
    root_exists = directory.is_dir()
    paths, root_error = _record_paths(directory) if root_exists else ([], None)

    total = 0
    agreements = 0
    corrupt: list[str] = []
    swept: list[str] = []
    sweep_failed: list[str] = []
    groups: dict[str, _GroupAccumulator] = {}
    earliest: datetime | None = None
    latest: datetime | None = None
    earliest_text: str | None = None
    latest_text: str | None = None

    for path in paths:
        payload = _load_record(path)
        stamp = _parse_timestamp(payload.get("recorded_at")) if payload is not None else None
        if stamp is None:
            stamp = _file_mtime(path)
        if (
            ttl_seconds is not None
            and stamp is not None
            and (horizon - stamp).total_seconds() > ttl_seconds
        ):
            if _unlink_record(path):
                swept.append(path.name)
            else:
                sweep_failed.append(path.name)
            continue
        if payload is None:
            corrupt.append(path.name)
            continue
        total += 1
        recorded_text = _as_text(payload.get("recorded_at"))
        if stamp is not None:
            if earliest is None or stamp < earliest:
                earliest, earliest_text = stamp, recorded_text or stamp.isoformat()
            if latest is None or stamp > latest:
                latest, latest_text = stamp, recorded_text or stamp.isoformat()
        if payload.get("agreement") is True:
            agreements += 1
            continue
        kind = _as_text(_as_mapping(payload.get("disagreement")).get("kind"))
        kind = kind or UNKNOWN_DISAGREEMENT_KIND
        groups.setdefault(kind, _GroupAccumulator(kind)).add(
            payload, path.name, sample_limit=sample_limit
        )

    ordered = tuple(
        accumulator.freeze()
        for accumulator in sorted(
            groups.values(), key=lambda item: (-item.count, item.kind)
        )
    )
    return ShadowReport(
        root=directory,
        generated_at=_utcnow(),
        root_exists=root_exists,
        root_error=root_error,
        total=total,
        agreements=agreements,
        disagreements=total - agreements,
        groups=ordered,
        corrupt=tuple(corrupt),
        swept=tuple(swept),
        sweep_failed=tuple(sweep_failed),
        ttl_seconds=ttl_seconds,
        earliest=earliest_text,
        latest=latest_text,
    )


def _load_record(path: Path) -> dict[str, Any] | None:
    """讀一筆 telemetry；讀不到／不是 JSON object 一律回 ``None``（呼叫端計為壞檔）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.debug("coverage shadow record unreadable (%s): %s", path.name, error)
        return None
    if not isinstance(payload, dict):
        logger.debug("coverage shadow record is not a JSON object: %s", path.name)
        return None
    return payload


def _file_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _unlink_record(path: Path) -> bool:
    """刪一筆過期記錄；失敗只記 debug 回 ``False``（唯讀掛載／權限不足是合法現況）。"""
    try:
        path.unlink()
    except OSError as error:
        logger.debug("coverage shadow retention sweep failed (%s): %s", path.name, error)
        return False
    return True


# ---------------------------------------------------------------------------
# CLI（唯讀 on-demand 入口；比照 `python -m paulsha_cortex.trust_root ...` 慣例）
# ---------------------------------------------------------------------------


def _build_report_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m paulsha_cortex.coordinator.coverage",
        description=(
            "讀 coverage validator shadow telemetry 並輸出 disagreement 統計"
            "（R1 Go/No-Go 的直接輸入）；順帶做 TTL 清掃。"
        ),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="輸出彙總報告（目前唯一動作，必須明示）",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="telemetry 目錄（預設 <coordinator_root>/coverage-shadow）",
    )
    parser.add_argument("--json", action="store_true", help="輸出結構化 JSON")
    parser.add_argument(
        "--ttl-days",
        type=float,
        default=DEFAULT_SHADOW_TTL_SECONDS / 86_400,
        help="保留天數，超過即於本次執行順帶刪除（預設 30）",
    )
    parser.add_argument(
        "--no-sweep",
        action="store_true",
        help="完全不清掃，純唯讀讀取",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_SAMPLE_LIMIT,
        help="每組 disagreement 附幾筆樣本明細（0＝全部，預設 5）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_report_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.report:
        parser.print_usage(sys.stderr)
        sys.stderr.write("錯誤: 需指定 --report\n")
        return 2
    if args.ttl_days < 0:
        sys.stderr.write("錯誤: --ttl-days 不可為負\n")
        return 2
    ttl_seconds = None if args.no_sweep else args.ttl_days * 86_400
    report = build_shadow_report(
        root=args.root,
        ttl_seconds=ttl_seconds,
        sample_limit=max(args.samples, 0),
    )
    if args.json:
        sys.stdout.write(
            json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        )
    else:
        sys.stdout.write(report.render_text())
    # 唯讀觀測工具：有沒有 disagreement 是 Go/No-Go 的人工判讀，不是本指令的 exit code。
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 進入點
    raise SystemExit(main())
