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

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..coordinator import quota_admission as _quota_admission

#: 對抗審查第三輪（MAJOR）：`current_identity_by_persona` 完全沒被呼叫端
#: 傳入（舊呼叫方式，例如本檔既有全部單元測試）時，attempt-比對邏輯必須
#: 逐字停用——不得把『呼叫端沒有能力算出』與『呼叫端算出來、但這個
#: persona 目前沒有未通過的卡』混為一談（後者才是「推導不出就視為不
#: 相符」）。用 sentinel 區分兩者，不能用 ``None`` 兼職兩個意思。
_ATTEMPT_CHECK_DISABLED = object()

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


def _file_identity(path: Path) -> tuple[int, int, int, int] | None:
    """回傳 ``path`` 目前的檔案身分：``(size, mtime_ns, inode, mode)``。

    只用來判斷『這個 store 檔案自從上次讀取後有沒有變』——append-only
    寫入至少會改變 size／mtime；純權限變更（例如 #840 對抗審查驗收情境
    裡用 chmod 模擬的損毀）不改變 size／mtime／inode，但會改變
    ``st_mode``——``AdmissionDecisionStore._check_file`` 的 fail-closed
    判定正是靠檔案權限位，因此權限位必須是身分的一部分，否則『純改權限、
    內容沒變』會被誤判成『沒變』而漏讀，讓已經不再合法的檔案繼續沿用上一
    次讀到的索引。刻意不用 ``st_ctime``：純 metadata 變更在部分檔案系統
    （包含本專案 CI／開發機常見的 WSL2 環境）上不保證更新 ctime 解析度，
    不是可靠訊號。檔案不存在（尚未寫過任何 decision）回傳 ``None``。
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns, info.st_ino, info.st_mode)


def _parent_identity(path: Path) -> tuple[int, int, int] | None:
    """回傳 ``path``（store 檔案）父目錄目前的 ``(mode, uid, inode)``。

    對抗審查第三輪（MAJOR）：`_load_index` 過去只把 :func:`_file_identity`
    （store **檔案本身**的身分）當快取鍵——`AdmissionDecisionStore._check_parent`
    的安全檢查是驗**父目錄**的權限位（`0o077` 遮罩），純把父目錄
    chmod 成不安全權限（例如 `0o777`）完全不動檔案本身的 size／mtime／
    inode／mode，因此 `_file_identity` 判定『沒變』→ 沿用快取索引 → 從未
    真的呼叫 `store.all_rows()`／`_check_parent()`，讓已經不安全的目錄繼續
    回報『新鮮』決策。

    修法：把父目錄身分也併入快取鍵——目錄權限一旦變動，這裡算出的 tuple
    就會不同，強迫 :meth:`DecisionReadCache._load_index` 落回真正呼叫
    ``store.all_rows()``，由該呼叫鏈既有的 ``_check_parent()`` 做同一套
    fail-closed 判定（本函式不重新定義『安全』的語意，只負責讓比對不被
    繞過）。不用 ``st_mtime``／``st_size``——目錄內容變動（decisions.jsonl
    的 rename／unlink）已經由 `_file_identity` 覆蓋，這裡只多補權限維度。
    目錄不存在回 ``None``（比照 `_check_parent(allow_missing=True)` 的
    既有語意，交由後續 `store.all_rows()` 走它自己的判定，不在這裡搶先
    定調）。
    """
    try:
        info = path.lstat()
    except OSError:
        return None
    return (stat.S_IMODE(info.st_mode), info.st_uid, info.st_ino)


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

    同一份 cache 實例必須在**同一次 status snapshot** 內共用，確保這一輪
    快照裡所有 section 讀到的是同一次 store 存取結果；同時**必須跨快照
    存活**（daemon 生命週期內只建一個實例，不逐輪重建）——否則上一輪成功
    讀到的 decision 會在下一輪重建時被丟棄，store 一旦暫時讀不到（損毀／
    權限錯誤）就沒有 last-good 可回退，退化成單純的『這次讀不到』（對抗
    審查第一輪 MAJOR）。``manager_daemon.py`` 的
    ``build_runtime_status_provider()`` 在外層（呼叫一次、跨輪共用）建立
    ``quota_decision_cache``，內層 ``provider()`` closure 每輪呼叫時沿用同
    一個實例，不重建。
    """

    def __init__(self) -> None:
        self._cache: dict[str, _CachedDecision] = {}
        # #840 對抗審查修復（MAJOR，manager.py 熱路徑）：`decision_id` →
        # raw row 的全檔索引，一個 store 路徑最多存一份，只在檔案身分
        # （見 `_file_identity`）真的變了才重讀。修好之前，`get()` 對每個
        # persona／run 都直接呼叫 `store.get()`，等於整份 append-only 檔案
        # 在同一輪 status 快照裡被重複線性掃描 N 次（N＝persona×run 數）。
        # value 是 ``(identity, status, payload)``：`status="ok"` 時
        # `payload` 是索引 dict；`status="error"` 時 `payload` 是失敗訊息
        # 字串（供在同一個身分下重放同一個例外，不必真的重新開檔）。
        # `identity` 是 ``(檔案身分, 父目錄身分)`` 的組合鍵——見
        # `_parent_identity` 文件字串：父目錄權限被放寬時檔案本身的
        # size／mtime／inode／mode 都不會變，組合鍵才能偵測到（對抗審查
        # 第三輪 MAJOR）。
        self._index_by_path: dict[
            str,
            tuple[
                tuple[tuple[int, int, int, int] | None, tuple[int, int, int] | None],
                str,
                Any,
            ],
        ] = {}

    def _load_index(
        self, store: "_quota_admission.AdmissionDecisionStore"
    ) -> dict[str, dict[str, Any]]:
        path_key = str(store.path)
        # 對抗審查第三輪（MAJOR）：組合鍵＝檔案身分＋父目錄身分。單獨比對
        # 檔案身分繞得過『父目錄權限被放寬、檔案內容沒變』這種攻擊面——見
        # `_parent_identity` 文件字串。
        identity = (_file_identity(store.path), _parent_identity(store.path.parent))
        cached = self._index_by_path.get(path_key)
        if cached is not None and cached[0] == identity:
            _, status, payload = cached
            if status == "ok":
                return payload
            raise _quota_admission.AdmissionDecisionCorrupt(payload)
        try:
            rows = store.all_rows()
        except _quota_admission.AdmissionDecisionCorrupt as exc:
            self._index_by_path[path_key] = (identity, "error", str(exc))
            raise
        index = {
            row["decision_id"]: row
            for row in rows
            if isinstance(row, Mapping) and isinstance(row.get("decision_id"), str)
        }
        self._index_by_path[path_key] = (identity, "ok", index)
        return index

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

        內部經 :meth:`_load_index` 走同一份『整檔索引』——同一個 store 檔案
        身分不變時，無論呼叫幾次 :meth:`get`（幾個 persona、幾個 run）都只
        真的讀一次檔案，索引在記憶體內查表。
        """

        try:
            index = self._load_index(store)
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
        row = index.get(decision_id)
        decision = _quota_admission.AdmissionDecision.from_row(row) if row is not None else None
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


def _attempt_mismatch_reason(
    decision: "_quota_admission.AdmissionDecision",
    current_identity: object,
    *,
    needs_human_reason: object = None,
) -> str | None:
    """判斷已找到的 ``decision`` 是否仍對應這個 persona『目前』在處理的卡。

    對抗審查第三輪（MAJOR）：`quota_admission` 指標一旦寫入就留在
    ``WorkflowRun`` 上，直到下一次同一個 persona 的 admission 決策覆寫它
    為止——`retry-card`（或世代 replay）重置卡片後、新 attempt 尚未寫出
    receipt 前，指標仍指著上一個（已經結束）attempt 的舊 receipt。這裡只
    比對呼叫端從既有 ``WorkflowRun.steps``（``card``／``executor``／
    ``model``）推導出的『這個 persona 目前最早未通過的卡』，不重算 #839
    的候選排序、原子預留或 job-count-based ``attempt_id``——那些仍是
    quota_admission 模組自己的權責。

    ``current_identity`` 非 ``Mapping``（呼叫端算不出目前卡，例如 persona
    已無未通過的卡）一律視為不相符，不臆測。回 ``None`` 代表相符；非
    ``None`` 是機器可讀的不相符原因。
    """
    if not isinstance(current_identity, Mapping):
        return "current-card-not-derivable"
    if current_identity.get("card") != decision.card_id:
        return "card-superseded"
    if decision.outcome == "admit":
        selected = decision.selected if isinstance(decision.selected, Mapping) else None
        if selected is None:
            return "admit-missing-selected"
        # admit 決策的身分解析（`step.executor`／`step.model`）與 admission
        # 決策同一次候選迴圈迭代內依序寫入（見 `manager._record_resolved_model_chain`
        # 呼叫點）；`retry-card` 重置這張卡時把兩者都清成 ``None``（見
        # `registry._manager_reset_workflow_for_retry_card`），因此『目前
        # 這張卡的身分』與這筆決策的 ``selected`` 不再一致，正是本檢查要
        # 抓的訊號。
        if (
            current_identity.get("executor") != selected.get("executor")
            or current_identity.get("model") != selected.get("model_id")
        ):
            return "identity-reset-since-decision"
        return None
    if decision.outcome == "wait":
        # wait 決策從未寫入 step 身分（見 `manager._quota_admission_stop`／
        # `_quota_admission_config_invalid_stop`：全數候選被拒時不建立任何
        # job），只看身分分不出新舊 attempt。這兩條路徑都同時把 run 標成
        # needs_human 並寫入同一個 quota 等待理由；`retry-card` 重置時會清掉
        # needs_human_reason。因此 wait receipt 只有在 run 目前的
        # needs_human_reason 仍是這筆決策的理由時才算當前——理由已清除或
        # 換成別的，代表 operator 已重試、新 attempt 尚未寫出 receipt。
        current_reason = (
            needs_human_reason.get("reason") if isinstance(needs_human_reason, Mapping) else None
        )
        if current_reason != getattr(decision, "reason", None):
            return "wait-cleared-since-decision"
        return None
    return "unknown-outcome"


def _project_persona_decision(
    persona: str,
    *,
    pointer: object,
    profile_binding: object,
    current_identity: object,
    store: "_quota_admission.AdmissionDecisionStore",
    cache: DecisionReadCache,
    now_ms: int,
    needs_human_reason: object = None,
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
    if current_identity is not _ATTEMPT_CHECK_DISABLED:
        mismatch_reason = _attempt_mismatch_reason(
            decision, current_identity, needs_human_reason=needs_human_reason
        )
        if mismatch_reason is not None:
            # 不相符：呈現「目前 attempt 尚無決策」，不得沿用舊 attempt 的
            # mode／outcome／selected（見票面：『不得沿用舊 attempt』）。
            return {
                "persona": persona,
                "decision_id": decision.decision_id,
                "source": "decision-store",
                "available": False,
                "stale": False,
                "gap_reason": "quota-decision-attempt-superseded",
                "mismatch_reason": mismatch_reason,
            }
    requested_profile_key = None
    resolved_profile_key = decision.profile_key
    # #840 對抗審查修復第二輪（MAJOR，約本檔原 250 行）：`execution_profile_bindings`
    # 是 #835 既有的 per-persona『最近一次真正派工成功』快照——只有真的走到
    # `manager._record_resolved_model_chain()` 才會被覆寫（見該函式呼叫點），
    # retry-card 之後這次 attempt 若全數候選被拒（`outcome == "wait"`，
    # `decision.selected is None`），這次 attempt 從未走到那個寫入點，
    # 這個 persona 底下的 binding 因此**必然**是上一個仍 admit 的 attempt
    # 留下的舊值。舊實作不論 outcome 一律套用，等於把上一輪的 requested／resolved
    # profile 借給這次根本沒有選中任何候選的 wait receipt——只有
    # `outcome == "admit"`（等價於 `decision.selected is not None`）時，
    # `execution_profile_bindings[persona]` 才保證與這個 decision 屬於同一個
    # attempt（兩者在 `manager._dispatch_workflow_card` 同一次候選迴圈迭代裡
    # 依序寫入，見該函式文件字串），才可以疊加；wait receipt 一律呈現
    # requested=none／resolved=unknown，不臆測，`excluded` 仍照舊帶出被排除
    # 候選清單（不受本次修法影響）。
    if decision.outcome == "admit" and decision.selected is not None:
        if isinstance(profile_binding, Mapping):
            request_key = profile_binding.get("request_key")
            if isinstance(request_key, str) and request_key:
                requested_profile_key = request_key
            resolved_key = profile_binding.get("resolved_key")
            if isinstance(resolved_key, str) and resolved_key:
                resolved_profile_key = resolved_key
    else:
        resolved_profile_key = "unknown"
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


def current_identity_by_persona_from_steps(steps: object) -> dict[str, dict[str, object]]:
    """從 workflow steps 推導『每個 persona 目前最早未通過的卡』的身分。

    `cortex work show`（Monitor 讀 registry row，steps 為 dict）與
    `cortex inspect status`（Manager 讀 `WorkflowRun`，steps 為 `WorkflowStep`
    物件）共用同一個判準，兩條路徑對同一狀態的 attempt 比對結果才會一致。
    只依賴既有欄位（``persona``／``card``／``gate_result``／``executor``／
    ``model``），沿用 `registry._manager_reset_workflow_for_retry_card` 挑
    「最早一張尚未通過的卡」的判準（`gate_result != "passed"`，取第一個）；
    同一 persona 有多張未通過的卡時只取最早一張——那正是下一次會被派工的卡。
    """
    if not isinstance(steps, (list, tuple)):
        return {}

    def _field(step: object, name: str) -> object:
        if isinstance(step, Mapping):
            return step.get(name)
        return getattr(step, name, None)

    result: dict[str, dict[str, object]] = {}
    for step in steps:
        if step is None:
            continue
        persona = _field(step, "persona")
        if not isinstance(persona, str) or not persona or persona in result:
            continue
        if _field(step, "gate_result") == "passed":
            continue
        result[persona] = {
            "card": _field(step, "card"),
            "executor": _field(step, "executor"),
            "model": _field(step, "model"),
        }
    return result


def project_workflow_quota_admission(
    *,
    run_id: str,
    quota_admission: Mapping[str, Mapping[str, str]] | None,
    needs_human_reason: Mapping[str, Any] | None,
    execution_profile_bindings: Mapping[str, Mapping[str, Any]] | None = None,
    current_identity_by_persona: Mapping[str, Mapping[str, Any]] | None = None,
    store: "_quota_admission.AdmissionDecisionStore",
    cache: DecisionReadCache | None = None,
    now_ms: int,
) -> dict[str, Any]:
    """單一 run 的 quota-admission 決策投影——``attention``／``work show`` 共用。

    純函式（不寫任何檔案）；``store``／``cache`` 由呼叫端注入，同一次
    snapshot 內請傳同一個 ``cache`` 實例，確保跨 section 一致（見票面 AC3）。

    ``current_identity_by_persona``（對抗審查第三輪 MAJOR）：呼叫端從
    ``WorkflowRun.steps`` 推導出的『每個 persona 目前最早未通過的卡』
    （``{"card":..., "executor":..., "model":...}``），供 :func:`_attempt_mismatch_reason`
    判斷 `quota_admission` 指標是否仍對應這個 persona 目前的 attempt——見
    該函式文件字串。**省略此參數維持逐字既有行為**（不做任何 attempt
    比對），只有呼叫端明確傳入時才生效；這是刻意的可選欄位加法（比照
    #835／#839 既有模式），現有呼叫端（例如本模組所有既有單元測試）不必
    跟著改。
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
            if current_identity_by_persona is None:
                current_identity = _ATTEMPT_CHECK_DISABLED
            else:
                current_identity = current_identity_by_persona.get(persona)
            projected = _project_persona_decision(
                persona,
                pointer=pointer,
                profile_binding=profile_binding,
                current_identity=current_identity,
                store=store,
                cache=resolved_cache,
                now_ms=now_ms,
                needs_human_reason=needs_human_reason,
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
