"""#840：投影動態派工決策與額度等待來源（refine R10）。

本模組是**唯讀投影**——只消費 #839 ``AdmissionDecisionStore`` 的既有 decision
receipt（Manager-owned、Trust Root 登記為 Monitor 唯讀）與 #527
``DiagnosticReason``（``WorkflowRun.needs_human_reason``），不寫入
registry／workflow 檔案，也不重算 #828 的 actual/planned/last identity 或
#830 的非 Job 決策契約——那些既有 producer 原樣消費、原樣帶出。

單一投影函式 :func:`project_workflow_quota_admission` 同時供
``manager.workflow_status_entry``（``cortex inspect status`` 的 ``attention``
清單）與 ``monitor.providers.WorkflowRegistryProvider``（``cortex work
show`` 的 observations 通道）呼叫，確保同一份 snapshot 的 decision／status／
reason／freshness 不會因為兩個呈現面各自重算而長得不一樣。

allowlist：只輸出下面列舉的非機敏欄位；``selected``／``excluded`` 即使呼叫端
（測試或未來 producer）塞進了非預期 key（例如 credential、env、raw prompt），
本模組也只挑出白名單內的欄位，不逐字轉發。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..coordinator import quota_admission as _quota_admission

PROJECTION_SCHEMA = "cortex-quota-decision-projection/v1"

#: decision receipt 的 ``selected``／``excluded`` 候選身分只允許帶出這些欄位
#: ——executor／model_id／independence_domain 是既有派工路徑既有的公開身分
#: 詞彙（見 `manager._workflow_execution_identity`），不含任何 credential 或
#: provider 原始 payload。
_ALLOWED_CANDIDATE_KEYS: tuple[str, ...] = ("executor", "model_id", "independence_domain")
_ALLOWED_EXCLUDED_KEYS: tuple[str, ...] = ("executor", "model_id", "exclusion_reason")

#: `manager._quota_admission_stop`／`_quota_admission_config_invalid_stop`
#: 寫入 `needs_human_reason.reason` 的兩個 quota-wait 分類碼——只有這兩個
#: reason 代表『目前這個 run 卡在額度等待，不是 Job』。
QUOTA_WAIT_REASONS = frozenset({"quota-admission-insufficient", "quota-config-invalid"})

#: #527 `DiagnosticReason.to_dict()` 允許帶出的欄位——本模組只挑白名單內的，
#: 不逐字轉發整份理由（理由本身已經過 `DiagnosticReason` 驗證與長度上限，
#: 這裡的白名單是第二層防線，供未來欄位擴充時不必回頭改本模組）。
_ALLOWED_WAIT_KEYS: tuple[str, ...] = ("reason", "detail", "source", "recorded_at", "next_step_hint")


def _allowlisted_mapping(value: object, keys: Sequence[str]) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {key: value[key] for key in keys if key in value}


def _allowlisted_sequence(values: object, keys: Sequence[str]) -> list[dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in values:
        allowlisted = _allowlisted_mapping(item, keys)
        if allowlisted is not None:
            result.append(allowlisted)
    return result


def _classify_demand(demand_version: object) -> str:
    if not isinstance(demand_version, str) or not demand_version:
        return "unknown"
    if demand_version == "not-applicable":
        return "not-applicable"
    if demand_version == _quota_admission.DEMAND_FIXTURE_VERSION:
        return "estimated"
    return "confirmed"


def _classify_observation(state: object) -> str:
    if state == "known":
        return "confirmed"
    if state == "unmanaged":
        return "not-applicable"
    # 缺席（#840 之前寫的舊 receipt）或明確 "unknown" 都一律回 unknown——
    # 不得把『沒有這筆資料』呈現成『資料齊全但恰好是 known』。
    return "unknown"


@dataclass
class _CachedDecision:
    decision: "_quota_admission.AdmissionDecision"
    read_at_ms: int


class DecisionReadCache:
    """decision store 讀取失敗時的 last-good 快取（純記憶體，不落地）。

    ``AdmissionDecisionStore`` 是 append-only、單一 ``decision_id`` 的內容
    不可變（見該類別文件字串），因此**正常情況**下每次直接重讀就能得到與
    上次相同的結果——restart 之後天然一致，不需要跨行程持久化這份快取。

    這份快取只在**同一個投影呼叫端 process 存活期間**、store 暫時讀不到
    （檔案損毀／IO 錯誤）時，提供『上一次成功讀到的內容』；且**必須**同時
    附上 ``stale``／``stale_reason``／``stale_since_ms``，不得讓呼叫端誤以為
    那是這次重新讀到的新鮮值（見票面 AC：『不得把過期選模或 unknown
    remaining 呈現成現在可派』）。

    同一份 cache 實例應該在**同一次 status snapshot** 內共用（例如
    ``manager_daemon.py`` 的 ``provider()`` closure 每輪重建一個），確保這一
    輪快照裡所有 section 讀到的是同一次 store 存取結果。
    """

    def __init__(self) -> None:
        self._cache: dict[str, _CachedDecision] = {}

    def get(
        self,
        store: "_quota_admission.AdmissionDecisionStore",
        decision_id: str,
        *,
        now_ms: int,
    ) -> tuple["_quota_admission.AdmissionDecision | None", dict[str, Any] | None]:
        """回傳 ``(decision, stale_info)``。

        ``stale_info`` 非 ``None`` 代表這次讀取失敗（store 損毀／IO 錯誤）或
        該 decision_id 從 append-only store 裡『消失』（不該發生，但仍保守
        回報而非靜默吞掉）；此時 ``decision`` 是快取的 last-good（可能仍是
        ``None``，若從未成功讀過）。
        """

        try:
            decision = store.get(decision_id)
        except _quota_admission.AdmissionDecisionCorrupt as exc:
            cached = self._cache.get(decision_id)
            return (
                cached.decision if cached is not None else None,
                {
                    "stale": True,
                    "stale_reason": f"decision-store-read-failed:{exc}",
                    "stale_since_ms": cached.read_at_ms if cached is not None else None,
                    "observed_at_ms": now_ms,
                },
            )
        if decision is not None:
            self._cache[decision_id] = _CachedDecision(decision=decision, read_at_ms=now_ms)
            return decision, None
        cached = self._cache.get(decision_id)
        if cached is not None:
            return cached.decision, {
                "stale": True,
                "stale_reason": "decision-disappeared-from-store",
                "stale_since_ms": cached.read_at_ms,
                "observed_at_ms": now_ms,
            }
        return None, None


def _project_persona_decision(
    persona: str,
    *,
    pointer: object,
    profile_binding: object,
    store: "_quota_admission.AdmissionDecisionStore",
    cache: DecisionReadCache,
    now_ms: int,
) -> dict[str, Any] | None:
    if not isinstance(pointer, Mapping):
        return None
    decision_id = pointer.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id:
        return None
    decision, stale_info = cache.get(store, decision_id, now_ms=now_ms)
    if decision is None:
        payload: dict[str, Any] = {
            "persona": persona,
            "decision_id": decision_id,
            "source": "decision-store",
            "available": False,
            "stale": False,
            "gap_reason": "decision-not-found",
        }
        if stale_info is not None:
            payload.update(stale_info)
            payload.pop("gap_reason", None)
        return payload
    requested_profile_key = None
    resolved_profile_key = decision.profile_key
    if isinstance(profile_binding, Mapping):
        request_key = profile_binding.get("request_key")
        if isinstance(request_key, str) and request_key:
            requested_profile_key = request_key
        resolved_key = profile_binding.get("resolved_key")
        if isinstance(resolved_key, str) and resolved_key:
            resolved_profile_key = resolved_key
    payload = {
        "persona": persona,
        "decision_id": decision.decision_id,
        "source": "decision-store",
        "available": True,
        "stale": False,
        "mode": decision.mode,
        "outcome": decision.outcome,
        "policy_version": decision.policy_version,
        "policy_config_revision": decision.policy_config_revision,
        "observation_version": decision.observation_version,
        "demand_version": decision.demand_version,
        "qualification_version": decision.qualification_version,
        "requested_profile_key": requested_profile_key,
        "resolved_profile_key": resolved_profile_key,
        "generated_at_ms": decision.generated_at_ms,
        "reservation_id": decision.reservation_id,
        "selected": _allowlisted_mapping(decision.selected, _ALLOWED_CANDIDATE_KEYS),
        "excluded": _allowlisted_sequence(decision.excluded, _ALLOWED_EXCLUDED_KEYS),
        "classification": {
            "demand": _classify_demand(decision.demand_version),
            "observation": _classify_observation(decision.selected_observation_state),
        },
        "selected_feasible": decision.selected_feasible,
    }
    if stale_info is not None:
        payload.update(stale_info)
    return payload


def _project_wait(needs_human_reason: object) -> dict[str, Any] | None:
    if not isinstance(needs_human_reason, Mapping):
        return None
    reason = needs_human_reason.get("reason")
    if reason not in QUOTA_WAIT_REASONS:
        return None
    payload = {key: needs_human_reason[key] for key in _ALLOWED_WAIT_KEYS if key in needs_human_reason}
    context = needs_human_reason.get("context")
    if isinstance(context, Mapping):
        # #527 `DiagnosticReason.context` 已由該型別驗證過（string→string、
        # 每值上限 200 字、key 數上限 16）；本模組不重驗，原樣帶出。
        payload["context"] = dict(context)
    return payload


def project_workflow_quota_admission(
    *,
    run_id: str,
    quota_admission: Mapping[str, Mapping[str, str]] | None,
    needs_human_reason: Mapping[str, Any] | None,
    execution_profile_bindings: Mapping[str, Mapping[str, Any]] | None = None,
    store: "_quota_admission.AdmissionDecisionStore",
    cache: DecisionReadCache | None = None,
    now_ms: int,
) -> dict[str, Any]:
    """單一 run 的 quota-admission 決策投影——``attention``／``work show`` 共用。

    純函式（不寫任何檔案）；``store``／``cache`` 由呼叫端注入，同一次
    snapshot 內請傳同一個 ``cache`` 實例，確保跨 section 一致（見票面 AC3）。
    """

    resolved_cache = cache if cache is not None else DecisionReadCache()
    personas: dict[str, Any] = {}
    if isinstance(quota_admission, Mapping):
        for persona, pointer in quota_admission.items():
            if not isinstance(persona, str) or not persona:
                continue
            profile_binding = None
            if isinstance(execution_profile_bindings, Mapping):
                profile_binding = execution_profile_bindings.get(persona)
            projected = _project_persona_decision(
                persona,
                pointer=pointer,
                profile_binding=profile_binding,
                store=store,
                cache=resolved_cache,
                now_ms=now_ms,
            )
            if projected is not None:
                personas[persona] = projected
    return {
        "schema": PROJECTION_SCHEMA,
        "run_id": run_id,
        "as_of_ms": now_ms,
        "personas": personas,
        "wait": _project_wait(needs_human_reason),
    }
