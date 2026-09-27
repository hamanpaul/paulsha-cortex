"""#839：額度感知准入（quota-aware admission）與安全 attempt 邊界 fallback。

依賴（不重做，只消費）：

- #835 ``execution_profile``／``execution_adapters``：已核可的
  ``ExecutionProfileBinding``（``resolved_key`` 即 profile_key）。
- #836 ``quota_observation``／``quota_shadow``：``PoolDescriptor``、
  ``UnitDefinition``、``ProfilePoolBinding``、``QuotaShadowService.project()``
  的唯讀觀測投影（sufficient／insufficient／unknown）。
- #838 ``quota_reservation``：``QuotaReservationAuthority`` 的原子
  reserve/bind/settle/release/reconcile 生命週期。本模組**只消費**這個狀態
  機、不修改它的轉移規則。
- #842 ``qualification_lifecycle``：呼叫端已用
  ``manager._bind_workflow_execution_profile`` 硬濾過 pin／權限／角色／
  reviewer independence／qualification；本模組**不重驗**這些，只在已核可
  的候選上疊一層「這個候選現在有沒有額度」。

契約邊界（本票刻意不做的事）：

- 不重排候選、不重建候選分層排序、不重驗 pin/independence/qualification/
  role——那些是既有 manager 派工路徑的責任。本模組收到的每一個候選都已經
  是「除了額度以外都核可」的候選。
- 不做 #837 operational usage forecast。:func:`estimate_demand` 目前只有
  明確標示版本的 fixture（:data:`DEMAND_FIXTURE_VERSION`），或呼叫端注入的
  estimator＋其自帶版本字串；本模組的正確性不依賴 demand 數字是否真實反映
  任務成本，只依賴 demand 有一個誰都能追的版本化來源，且這個版本字串會
  逐字寫入 decision receipt（見 :class:`AdmissionDecision`）。
- 不啟動任何 provider CLI／不讀 credential——額度餘量完全來自呼叫端已經
  用 #836 建好的 ``QuotaShadowService`` 投影。
- 不觸碰 ``quota_reservation.py`` 的狀態機或其 opt-in 開關；#839 自己的
  opt-in 開關（:func:`quota_admission_enabled`）與 #838 的開關各自獨立，
  兩者都預設 off／shadow，rollback 各自只需要改回對應環境變數。

先 shadow，再小範圍 opt-in：:func:`quota_admission_enabled` 預設 False，
呼叫端在 shadow 模式下只應呼叫 :func:`assess_candidate_quota` 記錄「這個
候選現在看起來夠不夠」而不改變既有派工結果；只有在 opt-in 開啟後才呼叫
:func:`reserve_for_candidate` 真正原子預留額度、才可能因為額度不足而拒絕
派工。rollback 只需要把環境變數改回非 ``on``（或整條不接線），不需要刪除
已經寫下的 decision receipt／reservation／consumption 證據。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import quota_observation as schema
from .quota_reservation import (
    PoolDemand,
    QuotaReservationAuthority,
    ReservationResult,
    TransitionResult,
)
from .quota_shadow import QuotaShadowService

__all__ = [
    "ADMISSION_POLICY_VERSION",
    "DEMAND_FIXTURE_VERSION",
    "AdmissionDecisionCorrupt",
    "PoolAssessment",
    "CandidateAssessment",
    "AdmissionDecision",
    "AdmissionDecisionStore",
    "quota_admission_enabled",
    "pools_for_profile",
    "binding_kind_for_profile",
    "UnboundProfileUsage",
    "unbound_profile_keys_report",
    "estimate_demand",
    "assess_candidate_quota",
    "observation_fingerprint",
    "decision_id_for",
    "build_pool_demands",
    "build_capacity_by_pool",
    "reserve_for_candidate",
    "generation_attempt_id",
    "reserve_for_candidate_with_generation_fallback",
    "settle_reservation_after_spawn_failure",
    "release_reservation_before_spawn",
    "reconcile_bound_reservations",
    "reconcile_reserved_reservations",
    "ReconcileOutcome",
    "InFlightDispatchTracker",
    "IN_FLIGHT_DISPATCHES",
    "DispatchContext",
    "QuotaConfigInvalid",
    "QuotaPoolsConfigError",
    "QuotaPoolsConfig",
    "parse_quota_pools_config",
    "load_quota_pools_config",
    "QUOTA_POOLS_CONFIG_SCHEMA",
]

#: decision receipt 上釘住的准入政策版本；改變准入邏輯本身（不是 demand／
#: observation 的來源版本）時才需要 bump。
ADMISSION_POLICY_VERSION = "quota-admission-policy:v1"

#: #837 forecast 尚未落地前的明確 placeholder：每個候選 pool/window 一律
#: 記 demand="1"（該 window 自己的計量單位）。任何用到這個常數的 receipt
#: 都可以憑這個版本字串精確認出「這筆 demand 不是真實用量預測」。
DEMAND_FIXTURE_VERSION = "demand-fixture:v1"

_ENV_ENFORCE_FLAG = "PSC_QUOTA_ADMISSION_ENFORCE"
_MAX_STORE_BYTES = 32 * 1024 * 1024
_MAX_RECORDS = 200_000
_MAX_ATTEMPTS_PER_DECISION = 64


class AdmissionDecisionCorrupt(ValueError):
    """decision receipt store 結構損毀、超限或版本未知——fail closed。"""


def quota_admission_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """#839 自己的 opt-in 開關；大小寫不敏感的 ``on`` 才是 True。

    預設（未設定／任何拼錯的值）一律 False——shadow 模式下呼叫端只應觀測、
    不應改變既有派工結果。獨立於 #838 的
    ``quota_reservation.reservation_authority_enabled()``：兩者都要 on
    才會真的原子預留額度並可能拒絕派工。
    """
    env = os.environ if environment is None else environment
    return env.get(_ENV_ENFORCE_FLAG, "").strip().lower() == "on"


class InFlightDispatchTracker:
    """對抗審查第四輪 MAJOR（quota_admission.py:1119）：本 process 內、目前
    正在 ``reserve()``→``bind()`` 之間 provisioning 的 reservation 集合。

    periodic tick 的 :func:`reconcile_reserved_reservations` 掃描與正在
    進行中的 dispatch 可能同時跑在同一個 process 裡（單一 Manager
    instance）——sweep 只靠 lease 是否過期無法分辨『provisioning 就是比這次
    lease 長，owner（也就是這個 process 自己）其實還活著』和『owner 早已
    不存在（crash／別的 instance）』。這個純記憶體、不耐久的集合就是那個
    區分依據：dispatch 拿到 grant 之後立刻 :meth:`mark_started`，
    bind()／失敗路徑結束後立刻 :meth:`mark_finished`（成對、finally 保
    證）；sweep 命中 in-flight 的 reservation 一律續租而不釋放，即使 lease
    早已過期。

    process 重啟後這個集合自然清空——回退到既有『依 lease／registry 事實
    判定』行為，不引入任何新的耐久狀態；不耐久正是設計：這個集合只回答
    『這個 process 自己知不知道還有誰在用』，crash 之後這個問題天然沒有
    答案，必須靠既有的 lease＋registry 證據鏈接手，不能假裝這個集合能夠
    跨 process／跨 restart 存活。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()

    def mark_started(self, reservation_id: str) -> None:
        with self._lock:
            self._in_flight.add(reservation_id)

    def mark_finished(self, reservation_id: str) -> None:
        with self._lock:
            self._in_flight.discard(reservation_id)

    def is_in_flight(self, reservation_id: str) -> bool:
        with self._lock:
            return reservation_id in self._in_flight


#: process-global 單一實例——dispatch 路徑與 periodic tick 的收斂掃描必須
#: 讀寫同一份記憶體集合，因此不能放在每次呼叫都重新建構的 `DispatchContext`
#: 上（`manager_daemon._quota_admission_context_for()` 每次 dispatch／
#: periodic tick 都會建一個新的 `DispatchContext`，若把追蹤集合放在它上面，
#: 每次呼叫看到的都會是一個空集合，形同沒有追蹤）。
IN_FLIGHT_DISPATCHES = InFlightDispatchTracker()


@dataclass(frozen=True)
class QuotaConfigInvalid:
    """production 接線 a：operator quota-pools 設定檔存在但無效，且 opt-in
    enforce（``PSC_QUOTA_ADMISSION_ENFORCE=on``）已開時，``manager_daemon``
    傳給 ``quota_admission_context`` 的訊號物件——**不是** :class:`DispatchContext`。

    ``manager._dispatch_workflow_card`` 認得這個型別，在建立任何 job／
    worktree 之前就直接 fail closed，回精確的 ``quota-config-invalid`` 等待
    理由（零 job、零假 job_id）。shadow 模式（enforce 未開）下設定檔無效時
    **不會**用到這個類別——那種情況呼叫端應記錄錯誤後改傳 ``None``，維持
    『整條線沒接上』的既有保守行為，不是傳這個訊號物件。
    """

    reason: str


@dataclass(frozen=True)
class DispatchContext:
    """manager.py 派工路徑接線用的一次性打包——沒有它時完全 no-op。

    這是本模組唯一預期由 ``manager._dispatch_workflow_card`` 消費的介面；
    缺席（``None``）代表部署尚未接上真實的 #836 descriptors／bindings／
    #838 authority，派工路徑因此完全不呼叫本模組任何一行——比環境變數關掉
    更保守的預設：連 shadow 觀測都不會發生，不是「開關 off」而是「整條線
    沒接上」。

    ``environment`` 只給 :func:`quota_admission_enabled` 用；預設 ``None``
    時該函式自己讀 ``os.environ``。
    """

    authority: QuotaReservationAuthority
    store: "AdmissionDecisionStore"
    shadow: QuotaShadowService
    descriptors: tuple[schema.PoolDescriptor, ...]
    unit_catalog: tuple[schema.UnitDefinition, ...]
    bindings: tuple[schema.ProfilePoolBinding, ...]
    lease_ms: int = 900_000
    environment: Mapping[str, str] | None = None
    #: #837 forecast 落地前，operator 設定檔選填的『終局 usage metric → unit
    #: ref』對照（見 :func:`parse_quota_pools_config`），給periodic tick 收斂
    #: 終局 job 時呼叫 ``QuotaShadowService.record_terminal_usage`` 用；缺席
    #: 時一律回空字典，行為與完全不呼叫 record_terminal_usage 相同（fail
    #: soft，不擋容量釋放）。
    usage_unit_refs: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    #: #840 對抗審查修復：operator quota-pools 設定檔的 ``config_revision``
    #: （見 :class:`QuotaPoolsConfig`）在 #839 落地時只用來做快取鍵，從未往下
    #: 傳給 decision receipt——投影面因此完全看不出「這筆決策是用哪一版
    #: operator 設定算出來的」。這裡加一個純 provenance 欄位補上；缺席（沿用
    #: 既有呼叫端未帶這個參數）時維持明確的 ``"unknown"``，不臆測。
    config_revision: str = "unknown"


def _pool_key(pool_ref: Mapping[str, str]) -> tuple[str, str, str, str]:
    return tuple(pool_ref[key] for key in ("authority_id", "account_id", "pool_id", "revision"))


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    """比照 #838 ``quota_reservation._fsync_directory`` 逐字複製——兩個模組
    刻意各自獨立成檔（見模組 docstring），因此各自持有一份小寫的目錄
    fsync helper，不互相 import 對方的內部函式。"""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        dir_fd = os.open(path, flags)
    except OSError as exc:
        raise AdmissionDecisionCorrupt("admission-decision-store-dir-fsync-failed") from exc
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        raise AdmissionDecisionCorrupt("admission-decision-store-dir-fsync-failed") from exc
    finally:
        os.close(dir_fd)


# ---------------------------------------------------------------------------
# profile → pool/window 對照（消費 #836 operator 設定的 ProfilePoolBinding）
# ---------------------------------------------------------------------------


def _binding_pool_windows(
    binding: schema.ProfilePoolBinding,
) -> tuple[tuple[dict[str, str], str], ...]:
    """展開單一 binding 的 constraints 為 ``(pool_ref, window_id)`` 序列。

    純粹的 wire 展開，不做任何 subject 比對——留給呼叫端決定這個 binding
    是否適用於目前的候選（見 :func:`_matches_exact`／:func:`_matches_identity`）。
    """
    wire = binding.to_dict()
    result: list[tuple[dict[str, str], str]] = []
    for constraint in wire.get("constraints", []):
        if not isinstance(constraint, dict) or constraint.get("state") != "known":
            continue
        value = constraint.get("value")
        if not isinstance(value, dict):
            continue
        pool_ref = value.get("pool_ref")
        window_id = value.get("window_id")
        if not isinstance(pool_ref, dict) or not isinstance(window_id, str):
            continue
        try:
            _pool_key(pool_ref)
        except KeyError:
            continue
        result.append((dict(pool_ref), window_id))
    return tuple(result)


def _matches_exact(subject: Mapping[str, object], profile_key: str) -> bool:
    """#836 既有的『精確』比對：subject 直接列舉 resolved profile key
    （``kind == "profile"``）或把它收進一份 group 成員清單
    （``kind == "group"``）。"""
    kind = subject.get("kind")
    if kind == "profile":
        profile_ref = subject.get("profile_ref")
        if not isinstance(profile_ref, dict) or profile_ref.get("state") != "known":
            return False
        value = profile_ref.get("value")
        return isinstance(value, dict) and value.get("key") == profile_key
    if kind == "group":
        members = subject.get("members")
        if not isinstance(members, dict) or members.get("state") != "known":
            return False
        return any(
            isinstance(item, dict) and item.get("key") == profile_key
            for item in members.get("value", [])
        )
    return False


def _matches_identity(subject: Mapping[str, object], executor: str, model_id: str) -> bool:
    """#1116：『穩定』比對——subject 直接綁 executor＋model_id
    （``kind == "identity"``），不看任何隨卡片而變的 resolved profile key。"""
    if subject.get("kind") != "identity":
        return False
    return subject.get("executor") == executor and subject.get("model_id") == model_id


def _resolve_binding_coverage(
    profile_key: str,
    *,
    executor: str | None,
    model_id: str | None,
    bindings: Sequence[schema.ProfilePoolBinding],
) -> tuple[tuple[tuple[dict[str, str], str], ...], str]:
    """回傳 ``(pool_windows, binding_kind)``——單一來源同時供
    :func:`pools_for_profile`／:func:`binding_kind_for_profile`／
    :func:`assess_candidate_quota` 消費，避免三處各自重掃一次 ``bindings``。

    優先序（#1116 票面）：resolved profile key 精確綁定（``binding_kind
    == "exact"``）＞ executor＋model_id 穩定 subject 綁定（``"identity"``）；
    找到精確綁定就**只**採用精確綁定的 pool/window 集合，不與穩定綁定的
    結果合併——避免同一候選『同時命中兩者』時被重複計算成兩份 pool 需求，
    也保留操作者刻意用精確綁定覆寫穩定綁定的能力（例如某張卡故意分流到
    另一個 pool）。兩者都找不到時回 ``binding_kind == "none"``（見
    :func:`assess_candidate_quota` 的 ``observation_state="unmanaged"``
    分支）——這個候選目前完全不受任何 binding 涵蓋。
    """
    exact_matched: list[tuple[dict[str, str], str]] = []
    seen: set[tuple[tuple[str, str, str, str], str]] = set()
    for binding in bindings:
        subject = binding.to_dict().get("subject")
        if not isinstance(subject, dict) or not _matches_exact(subject, profile_key):
            continue
        for pool_ref, window_id in _binding_pool_windows(binding):
            key = (_pool_key(pool_ref), window_id)
            if key in seen:
                continue
            seen.add(key)
            exact_matched.append((pool_ref, window_id))
    if exact_matched:
        return tuple(exact_matched), "exact"

    if isinstance(executor, str) and executor and isinstance(model_id, str) and model_id:
        identity_matched: list[tuple[dict[str, str], str]] = []
        seen = set()
        for binding in bindings:
            subject = binding.to_dict().get("subject")
            if not isinstance(subject, dict) or not _matches_identity(subject, executor, model_id):
                continue
            for pool_ref, window_id in _binding_pool_windows(binding):
                key = (_pool_key(pool_ref), window_id)
                if key in seen:
                    continue
                seen.add(key)
                identity_matched.append((pool_ref, window_id))
        if identity_matched:
            return tuple(identity_matched), "identity"

    return (), "none"


def pools_for_profile(
    profile_key: str,
    *,
    executor: str | None = None,
    model_id: str | None = None,
    bindings: Sequence[schema.ProfilePoolBinding],
) -> tuple[tuple[dict[str, str], str], ...]:
    """從 #836 的 ``ProfilePoolBinding`` 找出這個 profile 綁定的 pool/window。

    找不到任何 binding 時回空 tuple——代表這個 profile 目前不受 quota 管理
    （例如尚未替這個 executor 設定觀測來源），本模組據此視為『不受額度限制』
    （見 :func:`assess_candidate_quota` 的 ``observation_state="unmanaged"``
    分支），維持既有派工行為，不因為尚未接上真實觀測就無故擋派。

    ``executor``／``model_id`` 為 #1116 新增的選填參數——缺席（沿用既有呼叫
    端不帶這兩個參數）時行為與 #1116 之前逐字相同，只比對 resolved profile
    key 精確綁定；帶入時額外把 executor＋model_id 穩定 subject 綁定納入優先序
    最低的備援來源（見 :func:`_resolve_binding_coverage`）。
    """
    pool_windows, _ = _resolve_binding_coverage(
        profile_key, executor=executor, model_id=model_id, bindings=bindings,
    )
    return pool_windows


def binding_kind_for_profile(
    profile_key: str,
    *,
    executor: str | None = None,
    model_id: str | None = None,
    bindings: Sequence[schema.ProfilePoolBinding],
) -> str:
    """#1116：唯讀回報這個候選『目前是靠哪一種 binding 涵蓋』——
    ``"exact"``（resolved profile key 精確綁定）／``"identity"``（executor＋
    model_id 穩定 subject 綁定）／``"none"``（兩者都沒有，即
    unmanaged）。供 decision receipt／狀態投影／`bindings --report` 共用同一套
    判定，不各自重新實作一次比對規則。
    """
    _, binding_kind = _resolve_binding_coverage(
        profile_key, executor=executor, model_id=model_id, bindings=bindings,
    )
    return binding_kind


# ---------------------------------------------------------------------------
# #1116：唯讀報表——目前派工用到、但現有 bindings 完全沒有涵蓋的 resolved
# profile key（operator 診斷用；不寫任何狀態、不呼叫 shadow／reservation）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnboundProfileUsage:
    """一組『曾經被派工使用、但目前沒有任何 binding 涵蓋』的 resolved profile
    key 彙總——`executor`／`model_id` 來自當時 decision receipt 的
    ``selected``，`decision_count`／`last_seen_ms` 是純粹的出現次數／最近一次
    時間戳，供 operator 判斷這個缺口是不是還在發生。"""

    executor: str
    model_id: str
    profile_key: str
    decision_count: int
    last_seen_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor": self.executor,
            "model_id": self.model_id,
            "profile_key": self.profile_key,
            "decision_count": self.decision_count,
            "last_seen_ms": self.last_seen_ms,
        }


def unbound_profile_keys_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    bindings: Sequence[schema.ProfilePoolBinding],
) -> tuple[UnboundProfileUsage, ...]:
    """唯讀彙總：``rows``（比照 ``AdmissionDecisionStore.all_rows()`` 的既有
    raw row 形狀）裡曾經 ``outcome == "admit"`` 選中過的 resolved profile
    key，逐一用**目前**傳入的 ``bindings`` 重新判定涵蓋狀態——不信任 row 裡
    舊有的 ``selected_binding_kind``（那是寫入當時的快照，設定檔之後可能
    已經改過），只回報『用目前設定重算仍然是 ``binding_kind ==
    "none"``』的 resolved key，讓 operator 看見『目前』真正還缺 binding 的
    缺口，而不是歷史上曾經缺過、現在已經補上的舊紀錄。

    只讀 ``rows``／``bindings``，不開任何檔案、不呼叫 shadow／reservation、
    不寫任何狀態——呼叫端自行決定 ``rows`` 從哪個 store 讀出來。
    """
    usage: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("outcome") != "admit":
            continue
        selected = row.get("selected")
        profile_key = row.get("profile_key")
        generated_at_ms = row.get("generated_at_ms")
        if not isinstance(selected, Mapping) or not isinstance(profile_key, str):
            continue
        executor = selected.get("executor")
        model_id = selected.get("model_id")
        if not isinstance(executor, str) or not executor:
            continue
        if not isinstance(model_id, str) or not model_id:
            continue
        binding_kind = binding_kind_for_profile(
            profile_key, executor=executor, model_id=model_id, bindings=bindings,
        )
        if binding_kind != "none":
            continue
        key = (executor, model_id, profile_key)
        entry = usage.setdefault(key, {"count": 0, "last_seen_ms": 0})
        entry["count"] += 1
        if isinstance(generated_at_ms, int) and generated_at_ms > entry["last_seen_ms"]:
            entry["last_seen_ms"] = generated_at_ms
    return tuple(
        UnboundProfileUsage(
            executor=executor, model_id=model_id, profile_key=profile_key,
            decision_count=info["count"], last_seen_ms=info["last_seen_ms"],
        )
        for (executor, model_id, profile_key), info in sorted(usage.items())
    )


# ---------------------------------------------------------------------------
# demand estimation（#837 落地前的明確版本化 placeholder）
# ---------------------------------------------------------------------------


def _fixture_demand_estimator(pool_ref: Mapping[str, str], window_id: str) -> str:
    del pool_ref, window_id  # fixture 不看 pool 是誰，一律回報同一個保守值
    return "1"


def estimate_demand(
    pool_windows: Sequence[tuple[Mapping[str, str], str]],
    *,
    estimator: Callable[[Mapping[str, str], str], str] | None = None,
    demand_version: str | None = None,
) -> tuple[dict[tuple[tuple[str, str, str, str], str], str], str]:
    """回傳 ``(demand_by_window, demand_version)``。

    ``estimator`` 缺席時使用 :data:`DEMAND_FIXTURE_VERSION` 標示的固定
    fixture；呼叫端提供自己的 estimator 時**必須**同時提供 ``demand_version``
    ——不允許一個沒有版本字串的『真』預測值躺進 decision receipt，那樣事後
    沒有人能分辨這筆 demand 是不是 #837 落地後的真實預測。
    """
    if estimator is None:
        estimator = _fixture_demand_estimator
        demand_version = demand_version or DEMAND_FIXTURE_VERSION
    elif not demand_version:
        raise ValueError("demand_version is required when a custom estimator is supplied")
    demand_by_window: dict[tuple[tuple[str, str, str, str], str], str] = {}
    for pool_ref, window_id in pool_windows:
        key = (_pool_key(pool_ref), window_id)
        demand_by_window[key] = estimator(pool_ref, window_id)
    return demand_by_window, demand_version


# ---------------------------------------------------------------------------
# 單一候選的額度可行性評估（唯讀，不預留）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolAssessment:
    pool_ref: Mapping[str, str]
    window_id: str
    remaining: Mapping[str, Any]
    demand: str
    assessment: str  # sufficient | insufficient | unknown
    coverage_gaps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_ref": dict(self.pool_ref),
            "window_id": self.window_id,
            "remaining": json.loads(json.dumps(self.remaining)),
            "demand": self.demand,
            "assessment": self.assessment,
            "coverage_gaps": list(self.coverage_gaps),
        }


@dataclass(frozen=True)
class CandidateAssessment:
    executor: str
    model_id: str
    independence_domain: str
    profile_key: str
    feasible: bool
    exclusion_reason: str | None
    pools: tuple[PoolAssessment, ...]
    observation_state: str  # unmanaged | known | unknown
    coverage_gaps: tuple[str, ...]
    #: #1116：這個候選『目前是靠哪一種 binding 涵蓋』——``"exact"``（resolved
    #: profile key 精確綁定）／``"identity"``（executor＋model_id 穩定 subject
    #: 綁定）／``"none"``（兩者都沒有，即 unmanaged）。預設 ``None`` 是給
    #: 不經 :func:`assess_candidate_quota` 直接建構本型別的既有呼叫端（例如
    #: #839 測試 fixture）用的——沒有算過就不臆測，投影面一律視為 unknown，
    #: 不是新增必填欄位逼所有呼叫端改寫。
    binding_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor": self.executor,
            "model_id": self.model_id,
            "independence_domain": self.independence_domain,
            "profile_key": self.profile_key,
            "feasible": self.feasible,
            "exclusion_reason": self.exclusion_reason,
            "pools": [pool.to_dict() for pool in self.pools],
            "observation_state": self.observation_state,
            "coverage_gaps": list(self.coverage_gaps),
            "binding_kind": self.binding_kind,
        }


def assess_candidate_quota(
    *,
    executor: str,
    model_id: str,
    independence_domain: str,
    profile_key: str,
    bindings: Sequence[schema.ProfilePoolBinding],
    descriptors: Sequence[schema.PoolDescriptor],
    unit_catalog: Sequence[schema.UnitDefinition],
    shadow: QuotaShadowService,
    now_ms: int,
    demand_estimator: Callable[[Mapping[str, str], str], str] | None = None,
    demand_version: str | None = None,
) -> tuple[CandidateAssessment, str]:
    """回傳 ``(assessment, demand_version)``；唯讀，不呼叫 reservation。

    一個候選要 ``feasible``，它綁定的**所有** pool/window 都必須
    ``assessment == "sufficient"``——短窗夠、週窗不足一樣視為不可行（原票
    AC1：「短窗夠週窗不足也拒絕」）。找不到任何綁定的候選（``pools`` 為空）
    視為不受額度管理，直接可行。

    #1116：pool/window 綁定改由 :func:`_resolve_binding_coverage` 一次算出
    ``(pool_windows, binding_kind)``——resolved profile key 精確綁定優先，
    executor＋model_id 穩定 subject 綁定次之，兩者都沒有才是
    ``observation_state="unmanaged"``。
    """
    pool_windows, binding_kind = _resolve_binding_coverage(
        profile_key, executor=executor, model_id=model_id, bindings=bindings,
    )
    if not pool_windows:
        return (
            CandidateAssessment(
                executor=executor,
                model_id=model_id,
                independence_domain=independence_domain,
                profile_key=profile_key,
                feasible=True,
                exclusion_reason=None,
                pools=(),
                observation_state="unmanaged",
                coverage_gaps=(),
                binding_kind=binding_kind,
            ),
            demand_version or "not-applicable",
        )
    demand_by_window, resolved_demand_version = estimate_demand(
        pool_windows, estimator=demand_estimator, demand_version=demand_version,
    )
    projection = shadow.project(
        descriptors=tuple(descriptors),
        unit_catalog=tuple(unit_catalog),
        now_utc_ms=now_ms,
        demand_by_window=demand_by_window,
    )
    rows_by_key = {
        (_pool_key(row["pool_ref"]), row["window_id"]): row for row in projection["pools"]
    }
    pool_assessments: list[PoolAssessment] = []
    feasible = True
    exclusion_reason: str | None = None
    all_gaps: set[str] = set()
    for pool_ref, window_id in pool_windows:
        key = (_pool_key(pool_ref), window_id)
        row = rows_by_key.get(key)
        demand_amount = demand_by_window[key]
        if row is None:
            pool_assessments.append(
                PoolAssessment(
                    pool_ref=pool_ref, window_id=window_id,
                    remaining={"state": "unknown", "reason": "pool-not-described"},
                    demand=demand_amount, assessment="unknown",
                    coverage_gaps=("pool-not-described",),
                )
            )
            all_gaps.add("pool-not-described")
            feasible = False
            exclusion_reason = exclusion_reason or "unknown-remaining-quota"
            continue
        row_assessment = row["assessment"]
        pool_assessments.append(
            PoolAssessment(
                pool_ref=pool_ref, window_id=window_id, remaining=row["remaining"],
                demand=demand_amount, assessment=row_assessment,
                coverage_gaps=tuple(row["coverage_gaps"]),
            )
        )
        all_gaps.update(row["coverage_gaps"])
        if row_assessment != "sufficient":
            feasible = False
            if exclusion_reason is None:
                exclusion_reason = (
                    "insufficient-quota" if row_assessment == "insufficient"
                    else "unknown-remaining-quota"
                )
    return (
        CandidateAssessment(
            executor=executor, model_id=model_id, independence_domain=independence_domain,
            profile_key=profile_key, feasible=feasible, exclusion_reason=exclusion_reason,
            pools=tuple(pool_assessments), observation_state=projection["state"],
            coverage_gaps=tuple(sorted(all_gaps)), binding_kind=binding_kind,
        ),
        resolved_demand_version,
    )


# ---------------------------------------------------------------------------
# 原子預留（只替已判定 feasible 的候選預留；不替候選池全體扣額度）
# ---------------------------------------------------------------------------


def observation_fingerprint(assessment: CandidateAssessment) -> str:
    """從一次評估用到的每個 pool 的 remaining／assessment 算出內容指紋。

    純粹是稽核用的版本字串——標記「這個決策當時看到的觀測長什麼樣子」，
    #838 reservation 本身不用它做正確性判斷（見
    ``QuotaReservationAuthority.reserve`` 的冪等鍵只看 ``pools_signature``，
    不看 ``observation_version``）。找不到任何綁定的候選回固定字串。
    """
    if not assessment.pools:
        return "unmanaged"
    payload = [pool.to_dict() for pool in assessment.pools]
    digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()[:16]
    return f"shadow-projection:{digest}"


def decision_id_for(
    *, run_id: str, card_id: str, attempt_id: str, profile_key: str, mode: str
) -> str:
    """給定 (run, card, attempt, profile, mode) 的穩定 decision_id。

    同一個 attempt 重送（restart／resume／late terminal 補送）算出同一個
    decision_id，因此後續 :func:`reserve_for_candidate` 呼叫
    ``QuotaReservationAuthority.reserve()`` 天然冪等——不會因為重送而重新
    扣一次額度，也不會誤造第二個 job。真正的新 attempt（安全 attempt 邊界
    fallback 之後）必須帶新的 ``attempt_id``，才會算出不同的 decision_id。

    對抗審查第四輪 MAJOR（manager.py:11491）：``mode``（必填）納入雜湊
    輸入——shadow 與 enforced 是不同的決策，若同一個 attempt／profile 先以
    shadow 寫過一筆 receipt，之後才切到 opt-in enforce 重試（例如 operator
    在同一個 attempt 存活期間切換 ``PSC_QUOTA_ADMISSION_ENFORCE``），兩者
    必須能夠在 :class:`AdmissionDecisionStore` 裡並存、各自冪等回放，不能
    讓後者的寫入被前者的舊記錄以『同 decision_id、內容不同』擋下（見
    ``AdmissionDecisionStore.record`` 的衝突語意）。呼叫端必須顯式帶入
    ``"shadow"`` 或 ``"enforced"``——不給預設值，逼每個呼叫點誠實回答『這是
    哪一種決策』，避免日後新呼叫點無意間沿用錯的隱含假設。"""
    if mode not in ("shadow", "enforced"):
        raise ValueError(f"invalid decision mode: {mode!r}")
    digest = hashlib.sha256(
        f"{run_id}\0{card_id}\0{attempt_id}\0{profile_key}\0{mode}".encode()
    ).hexdigest()
    return f"adm:v1:{digest}"


def _capacity_for_pool_assessment(pool: PoolAssessment) -> str:
    remaining = pool.remaining
    amount = remaining.get("amount") if isinstance(remaining, Mapping) else None
    if not isinstance(amount, Mapping):
        raise ValueError("cannot derive reservation capacity from a non-observed remaining value")
    if amount.get("kind") == "exact":
        value = amount.get("value")
        if isinstance(value, str):
            return value
    elif amount.get("kind") == "bounds":
        lower = amount.get("lower")
        if isinstance(lower, str):
            # 保守只用下界——不確定的上界不能拿來當可預留的容量上限。
            return lower
    raise ValueError("cannot derive a conservative reservation capacity from remaining amount")


def build_pool_demands(assessment: CandidateAssessment) -> tuple[PoolDemand, ...]:
    if not assessment.feasible:
        raise ValueError("cannot build pool demands for an infeasible candidate assessment")
    return tuple(
        PoolDemand(pool_ref=pool.pool_ref, window_id=pool.window_id, amount=pool.demand)
        for pool in assessment.pools
    )


def build_capacity_by_pool(
    assessment: CandidateAssessment,
) -> dict[tuple[tuple[str, str, str, str], str], str]:
    return {
        (_pool_key(pool.pool_ref), pool.window_id): _capacity_for_pool_assessment(pool)
        for pool in assessment.pools
    }


def reserve_for_candidate(
    authority: QuotaReservationAuthority,
    *,
    run_id: str,
    card_id: str,
    decision_id: str,
    attempt_id: str,
    assessment: CandidateAssessment,
    observation_version: str,
    demand_version: str,
    lease_ms: int,
    now_ms: int,
) -> ReservationResult:
    """只替**這一個**已判定 feasible 的候選原子預留全部所需 pool。

    候選『不受額度管理』（``assessment.pools`` 為空）時不需要，也不應該呼叫
    這支函式——呼叫端應直接視為可派工，不建立空的 reservation。
    """
    if not assessment.feasible:
        return ReservationResult(status="invalid", reason="candidate-not-quota-feasible")
    if not assessment.pools:
        return ReservationResult(status="invalid", reason="candidate-not-quota-managed")
    pools = build_pool_demands(assessment)
    capacity = build_capacity_by_pool(assessment)
    return authority.reserve(
        run_id=run_id, card_id=card_id, decision_id=decision_id, attempt_id=attempt_id,
        pools=pools, capacity_by_pool=capacity, observation_version=observation_version,
        demand_version=demand_version, lease_ms=lease_ms, now_ms=now_ms,
    )


def generation_attempt_id(base_attempt_id: str, generation: int) -> str:
    """給定基底 attempt_id 與世代序號，算出這個世代要用的 attempt_id。

    世代 0 就是 ``base_attempt_id`` 本身（不加任何後綴）——維持
    :func:`decision_id_for` 對世代 0 的既有輸出逐字不變，向後相容尚未觸發
    對抗審查第三輪 MAJOR（manager.py:13961）修法之前就已經存在的
    reservation／decision receipt。世代 ``>=1`` 才附加 ``:g{generation}``。
    """
    if generation < 0:
        raise ValueError("generation must be non-negative")
    if generation == 0:
        return base_attempt_id
    return f"{base_attempt_id}:g{generation}"


def reserve_for_candidate_with_generation_fallback(
    authority: QuotaReservationAuthority,
    *,
    run_id: str,
    card_id: str,
    base_attempt_id: str,
    profile_key: str,
    assessment: CandidateAssessment,
    observation_version: str,
    demand_version: str,
    lease_ms: int,
    now_ms: int,
    max_generations: int = _MAX_ATTEMPTS_PER_DECISION,
) -> tuple[str, str, ReservationResult]:
    """對抗審查第三輪 MAJOR（manager.py:13961）：`reserve_for_candidate` 的
    冪等回放（``duplicate``）只看 pools 組成是否相同，不看那筆舊
    reservation 現在是不是已經終局（``settled``／``released``）。dispatch 在
    建出 job 之前（``create_job()`` 失敗，或已經被
    :func:`release_reservation_before_spawn`／reconcile 收斂終結）就把上一
    個世代的 reservation 結束掉時，`decision_id_for` 依 job 數推算出的
    ``attempt_id`` 完全不會前進（job 從沒真正建立過，計數不會變）——下一次
    retry 用同一個 ``attempt_id``／``decision_id`` 再 reserve，只會拿到同一
    個已終結 reservation 的 ``duplicate`` 回放；若一律當成『別人持有』就會
    讓這張卡永久卡在 held-elsewhere，即使容量其實完全空著。

    這支函式把『世代』疊在 attempt_id 之上
    （:func:`generation_attempt_id`），決定性地往前探測：世代 0 就是
    ``base_attempt_id`` 本身；只要某個世代的 :func:`reserve_for_candidate`
    回傳 ``duplicate`` **且**那筆舊 reservation 已經是終局
    （``state in ("settled", "released")``），代表這個世代已經走完了它的
    生命週期，換下一個世代重算 decision_id 再試一次。遇到其他任何結果
    （``granted``／``denied``／``conflict``／``invalid``，或 ``duplicate``
    但舊 reservation 仍是 ``reserved``／``bound``）就立刻停下並原樣回傳——
    那些情況都不代表『這個世代已經結束』，換世代反而可能誤判仍在使用中的
    reservation。

    冪等性：同一份 authority 內容下，這個探測序列永遠停在同一個世代——同一
    次呼叫只會在探測到的『第一個尚未終結』世代呼叫一次真正可能改變狀態的
    reserve()（更早的世代只讀到 duplicate、不寫入任何新事件），不會一次
    retry 產生兩個 grant；重複呼叫（同一次 retry 因故重送）一樣先重新探測
    到同一個世代，再次冪等回放，或極少數情況下冪等地拿到同一個已授予的
    grant。

    回傳 ``(attempt_id, decision_id, result)``——實際用到的世代 attempt_id
    與其 decision_id，呼叫端據此更新 receipt／reservation handle 的簿記，
    不必自己重算。

    本函式只在 opt-in enforce（且候選已判定可行）時被呼叫（見
    ``manager._dispatch_workflow_card`` 的守衛），因此內部固定用
    ``mode="enforced"`` 算 decision_id——shadow 模式從不呼叫這支函式，不需
    要（也不應該）讓呼叫端額外傳一個永遠是同一個值的參數。"""
    generation = 0
    attempt_id = generation_attempt_id(base_attempt_id, generation)
    decision_id = decision_id_for(
        run_id=run_id, card_id=card_id, attempt_id=attempt_id, profile_key=profile_key,
        mode="enforced",
    )
    while True:
        result = reserve_for_candidate(
            authority,
            run_id=run_id, card_id=card_id, decision_id=decision_id, attempt_id=attempt_id,
            assessment=assessment, observation_version=observation_version,
            demand_version=demand_version, lease_ms=lease_ms, now_ms=now_ms,
        )
        is_terminated_duplicate = (
            result.status == "duplicate" and result.state in ("settled", "released")
        )
        if not is_terminated_duplicate or generation + 1 >= max_generations:
            return attempt_id, decision_id, result
        generation += 1
        attempt_id = generation_attempt_id(base_attempt_id, generation)
        decision_id = decision_id_for(
            run_id=run_id, card_id=card_id, attempt_id=attempt_id, profile_key=profile_key,
            mode="enforced",
        )


def release_reservation_before_spawn(
    authority: QuotaReservationAuthority,
    *,
    reservation_id: str,
    owner_token: str,
    attempt_id: str,
    expected_sequence: int,
    now_ms: int,
    reason: str = "fail-before-spawn",
) -> TransitionResult:
    """job 記錄尚未建立（``bind()`` 尚未呼叫）前放棄這筆 reservation。

    只適用於 reserve() 成功之後、``bind(job_id=...)`` 之前的窗口——例如
    provisioning（worktree／sandbox）在拿到 job_id 之後、真正 spawn 之前失敗。
    一旦 ``bind()`` 已經呼叫過，reservation 進入 ``bound``，只能經
    :func:`settle_reservation_after_spawn_failure` 或 reconcile 結束
    （#838 現行契約；見 ``quota_reservation.QuotaReservationAuthority.release``
    的文件字串）。
    """
    return authority.release(
        reservation_id=reservation_id, owner_token=owner_token, attempt_id=attempt_id,
        reason=reason, expected_sequence=expected_sequence, now_ms=now_ms,
    )


def settle_reservation_after_spawn_failure(
    authority: QuotaReservationAuthority,
    *,
    reservation_id: str,
    owner_token: str,
    attempt_id: str,
    expected_sequence: int,
    now_ms: int,
    note: str | None = None,
) -> TransitionResult:
    """spawn 已經 bind 到 job_id 之後失敗（含派工時 429／infra 錯誤）的終局。

    這裡只負責讓 reservation 進入 ``settled(failed)`` 終局、把這個 attempt
    的額度消耗記成確定發生過——**不**對這次失敗做任何品質判斷。infra／429
    失敗是否要降品質分、是否要記入 #825/#826 的 executor backoff，是呼叫端
    既有失敗分類（``provider_outcome.classify_launch_failure`` 等）的責任，
    本函式不重複也不覆蓋那個判斷（原票 AC4：「infra 不降品質分」）。
    """
    if note is not None and len(note) > 512:
        note = note[:512]
    return authority.settle(
        reservation_id=reservation_id, owner_token=owner_token, attempt_id=attempt_id,
        outcome="failed", expected_sequence=expected_sequence, now_ms=now_ms, note=note,
    )


# ---------------------------------------------------------------------------
# decision receipt：耐久、append-only，綁 job/run/card/attempt/profile 與
# qualification/observation/demand/policy 版本
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionDecision:
    decision_id: str
    run_id: str
    card_id: str
    attempt_id: str
    profile_key: str
    mode: str  # shadow | enforced
    outcome: str  # admit | wait
    policy_version: str
    observation_version: str
    demand_version: str
    qualification_version: str
    generated_at_ms: int
    selected: Mapping[str, Any] | None = None
    reservation_id: str | None = None
    excluded: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    reason: str | None = None
    job_id: str | None = None
    #: #840 對抗審查修復：receipt 過去只留一個不可逆的 ``observation_version``
    #: 指紋，事後完全看不出「當時選中的候選本身」是 sufficient／insufficient／
    #: unknown——shadow 模式下候選可行性不可行一樣會被 admit（見
    #: `manager._dispatch_workflow_card` 的候選迴圈），投影面因此無法區分
    #: 「confirmed 可派」與「shadow 下明知不可行／unknown 仍放行」。
    #: 兩個欄位都是選填（缺席＝#840 之前寫的舊 row，投影面視為 ``unknown``，
    #: 不臆測）：
    #: - ``selected_observation_state``：對應
    #:   ``CandidateAssessment.observation_state``（``unmanaged``／``known``／
    #:   ``unknown``）。
    #: - ``selected_feasible``：對應 ``CandidateAssessment.feasible``——是否
    #:   『所有』綁定 pool 當時都 sufficient。
    #:
    #: **不需要 schema bump**：這三個欄位與 #839 本身同一個 release 一起發布
    #: （#839 從未獨立上線過），因此不存在「#839-only（無 #840）的已發布版本
    #: 讀不懂這三個新 key」的相容性問題——`schema_version` 維持 ``1``。三個
    #: 欄位定位是 v1 內的選填鍵加法（比照 #839
    #: ``_QUOTA_POOLS_CONFIG_OPTIONAL_KEYS`` 的既有模式），不是破壞性 schema
    #: 變更；``_DECISION_REQUIRED_KEYS``／``_DECISION_OPTIONAL_KEYS``（見下方）
    #: 與 ``_read_fd`` 的驗證邏輯保證缺這三個 key 的 row 一樣合法可讀。
    selected_observation_state: str | None = None
    selected_feasible: bool | None = None
    #: #840 對抗審查修復：見 `DispatchContext.config_revision` 的文件字串。
    #: 同樣不需要 schema bump，理由同上。
    policy_config_revision: str | None = None
    #: #1116：對應 `CandidateAssessment.binding_kind`——選中候選當時是靠
    #: ``"exact"``（resolved profile key 精確綁定）還是 ``"identity"``
    #: （executor＋model_id 穩定 subject 綁定）涵蓋，或完全 ``"none"``
    #: （unmanaged）。比照 #840 三個選填欄位的既有加法模式：缺席（#1116
    #: 之前寫的舊 row）投影面視為 unknown，不臆測；不需要 schema bump。
    selected_binding_kind: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("shadow", "enforced"):
            raise ValueError("invalid admission decision mode")
        if self.outcome not in ("admit", "wait"):
            raise ValueError("invalid admission decision outcome")
        if self.outcome == "admit" and self.selected is None:
            raise ValueError("admit outcome requires a selected candidate")
        if self.outcome == "wait" and self.reason is None:
            raise ValueError("wait outcome requires an explicit reason")
        if len(self.excluded) > _MAX_ATTEMPTS_PER_DECISION:
            raise ValueError("too many excluded candidates on one decision")
        if self.selected_observation_state is not None and self.selected_observation_state not in (
            "unmanaged", "known", "unknown",
        ):
            raise ValueError("invalid admission decision selected_observation_state")
        if self.selected_feasible is not None and not isinstance(self.selected_feasible, bool):
            raise ValueError("selected_feasible must be a bool or None")
        if self.selected_binding_kind is not None and self.selected_binding_kind not in (
            "exact", "identity", "none",
        ):
            raise ValueError("invalid admission decision selected_binding_kind")

    def to_row(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "card_id": self.card_id,
            "attempt_id": self.attempt_id,
            "profile_key": self.profile_key,
            "mode": self.mode,
            "outcome": self.outcome,
            "policy_version": self.policy_version,
            "observation_version": self.observation_version,
            "demand_version": self.demand_version,
            "qualification_version": self.qualification_version,
            "generated_at_ms": self.generated_at_ms,
            "selected": (
                json.loads(json.dumps(self.selected)) if self.selected is not None else None
            ),
            "reservation_id": self.reservation_id,
            "excluded": [json.loads(json.dumps(item)) for item in self.excluded],
            "reason": self.reason,
            "job_id": self.job_id,
            # #840：選填欄位一律寫出（值可能是 None），讓 `to_row()` 逐字回放
            # 時一定含這三個 key——同一支程式碼寫兩次同一個 decision_id 才會
            # 是同一份 dict，`record()` 的冪等比對才不會因為欄位集不同而誤判
            # 成 conflict。舊 row（#840 之前寫的）讀回後同樣會補上這三個
            # None，見 `from_row()`。
            "selected_observation_state": self.selected_observation_state,
            "selected_feasible": self.selected_feasible,
            "policy_config_revision": self.policy_config_revision,
            "selected_binding_kind": self.selected_binding_kind,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "AdmissionDecision":
        return cls(
            decision_id=row["decision_id"], run_id=row["run_id"], card_id=row["card_id"],
            attempt_id=row["attempt_id"], profile_key=row["profile_key"], mode=row["mode"],
            outcome=row["outcome"], policy_version=row["policy_version"],
            observation_version=row["observation_version"], demand_version=row["demand_version"],
            qualification_version=row["qualification_version"],
            generated_at_ms=row["generated_at_ms"], selected=row.get("selected"),
            reservation_id=row.get("reservation_id"), excluded=tuple(row.get("excluded", ())),
            reason=row.get("reason"), job_id=row.get("job_id"),
            selected_observation_state=row.get("selected_observation_state"),
            selected_feasible=row.get("selected_feasible"),
            policy_config_revision=row.get("policy_config_revision"),
            selected_binding_kind=row.get("selected_binding_kind"),
        )


_DECISION_REQUIRED_KEYS = frozenset(
    {
        "schema_version", "decision_id", "run_id", "card_id", "attempt_id", "profile_key",
        "mode", "outcome", "policy_version", "observation_version", "demand_version",
        "qualification_version", "generated_at_ms", "selected", "reservation_id", "excluded",
        "reason", "job_id",
    }
)
#: #840：見 `AdmissionDecision.selected_observation_state`／`selected_feasible`／
#: `policy_config_revision` 的文件字串——比照 #839
#: `_QUOTA_POOLS_CONFIG_OPTIONAL_KEYS` 的既有加法模式，讓 #840 之前寫的舊
#: row（缺這三個 key）與之後寫的新 row（一律含，值可能是 ``None``）都合法。
_DECISION_OPTIONAL_KEYS = frozenset(
    {
        "selected_observation_state", "selected_feasible", "policy_config_revision",
        "selected_binding_kind",
    }
)
_DECISION_ALL_KEYS = _DECISION_REQUIRED_KEYS | _DECISION_OPTIONAL_KEYS


class AdmissionDecisionStore:
    """耐久、append-only 的 decision receipt 存放區。

    儲存形態比照 #838 ``quota_reservation`` 的硬化模式（``flock``、
    ``O_NOFOLLOW``、固定權限、fsync、大小上限、損毀一律 fail-closed），但
    receipt 本身沒有狀態機——每個 ``decision_id`` 只寫一次；重送同一個
    decision（restart／resume 補送）必須逐字相同才視為冪等重放，內容不同
    視為衝突（同一個 decision_id 不能代表兩份不同的決策）。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from paulsha_cortex.config.paths import quota_admission_decisions_root

            path = quota_admission_decisions_root() / "decisions.jsonl"
        self.path = Path(path)
        if ".." in self.path.parts:
            raise ValueError("admission decision store path cannot contain parent traversal")

    def record(self, decision: AdmissionDecision) -> AdmissionDecision:
        row = decision.to_row()
        fd = self._open_for_append()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            info = os.fstat(fd)
            self._check_file(info)
            records = self._read_fd(fd, info.st_size)
            for existing in records:
                if existing["decision_id"] != decision.decision_id:
                    continue
                # #840 對抗審查修復（MAJOR）：不能直接拿磁碟上的 raw row 跟
                # 新算出的 `row`（一定含 #840 三個選填欄位，值可能是 None）
                # 做 dict 相等比較——#840 之前寫的舊 row 根本沒有這三個 key，
                # 即使兩邊語意完全一樣（都是同一個 decision、選填欄位都是
                # unknown/None），單純 dict 相等會因為 key 集合不同而判成
                # 「內容不同」，把合法的冪等重放誤判成
                # `admission-decision-id-conflict`。兩邊都先經
                # `from_row()`→`to_row()` 正規化（缺席鍵補齊為 None，即
                # `AdmissionDecision.__init__` 的預設值），才是同一把尺；
                # 正規化後仍不同才是真的矛盾（同一個 decision_id 代表兩份
                # 不同決策）。
                normalized_existing = AdmissionDecision.from_row(existing).to_row()
                if normalized_existing == row:
                    return AdmissionDecision.from_row(existing)
                raise AdmissionDecisionCorrupt("admission-decision-id-conflict")
            if len(records) >= _MAX_RECORDS:
                raise AdmissionDecisionCorrupt("admission-decision-store-record-limit")
            raw = _canonical_bytes(row) + b"\n"
            if info.st_size + len(raw) > _MAX_STORE_BYTES:
                raise AdmissionDecisionCorrupt("admission-decision-store-size-limit")
            cursor = 0
            while cursor < len(raw):
                written = os.write(fd, raw[cursor:])
                if written <= 0:
                    raise OSError("admission decision store append made no progress")
                cursor += written
            os.fsync(fd)
            return decision
        finally:
            os.close(fd)

    def _open_for_append(self) -> int:
        """開啟（必要時建立）store 供寫入。

        比照 #838 ``quota_reservation._open_for_append`` 的硬化模式：不以
        ``exists()`` 判斷「是不是我建立的」——前一個呼叫者可能在 ``O_CREAT``
        後、目錄 fsync 前崩潰，後續呼叫者看到檔案已存在就會誤以為不需要補
        fsync。每次寫入前都 fsync 父目錄與其上層，確保這筆 decision receipt
        的 dirent 在崩潰後仍然存在，重啟不會因為看到空 store 而重放出第二份
        不同內容的 decision（對抗審查第四輪 MAJOR：舊實作只 fsync 檔案本身，
        沒有 fsync 目錄）。
        """
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_parent()
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise AdmissionDecisionCorrupt("admission-decision-store-open-failed") from exc
        try:
            _fsync_directory(parent.parent)
            _fsync_directory(parent)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def get(self, decision_id: str) -> AdmissionDecision | None:
        for row in self._read():
            if row["decision_id"] == decision_id:
                return AdmissionDecision.from_row(row)
        return None

    def all_rows(self) -> list[dict[str, Any]]:
        """回傳目前 store 內所有已驗證過形狀的 raw row。

        #840 對抗審查修復（MAJOR，manager.py 熱路徑）：`decision_projection.
        DecisionReadCache` 需要在一次 status 快照內建一份 ``decision_id`` →
        row 的索引供多個 persona／run 共用查詢，而不是逐個 persona 各自呼叫
        :meth:`get` 造成同一份 append-only 檔在同一輪被重讀多次。此方法只是
        ``_read()`` 的公開包裝——不新增驗證規則，也不改變既有 :meth:`get`／
        :meth:`enforced_admitted` 的讀取語意。
        """
        return self._read()

    def enforced_admitted(self) -> tuple[AdmissionDecision, ...]:
        """回傳所有 ``mode=enforced`` 且 ``outcome=admit`` 的決策。

        供 :func:`reconcile_bound_reservations` 掃描候選；每一筆是否仍
        ``bound`` 仍由呼叫端以 ``authority.status()`` 判斷，本函式只負責
        『這裡有哪些決策曾經真的預留過』。"""
        return tuple(
            AdmissionDecision.from_row(row)
            for row in self._read()
            if row.get("outcome") == "admit" and row.get("mode") == "enforced"
        )

    def _read(self) -> list[dict[str, Any]]:
        self._check_parent(allow_missing=True)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise AdmissionDecisionCorrupt("admission-decision-store-open-failed") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            info = os.fstat(fd)
            self._check_file(info)
            return self._read_fd(fd, info.st_size)
        finally:
            os.close(fd)

    def _check_parent(self, *, allow_missing: bool = False) -> None:
        try:
            info = self.path.parent.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise AdmissionDecisionCorrupt("admission-decision-store-parent-missing")
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise AdmissionDecisionCorrupt("admission-decision-store-parent-permissions-invalid")

    @staticmethod
    def _check_file(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > _MAX_STORE_BYTES
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise AdmissionDecisionCorrupt("admission-decision-store-file-shape-invalid")

    @staticmethod
    def _read_fd(fd: int, size: int) -> list[dict[str, Any]]:
        if size > _MAX_STORE_BYTES:
            raise AdmissionDecisionCorrupt("admission-decision-store-size-limit")
        raw = os.pread(fd, size, 0)
        if len(raw) != size:
            raise AdmissionDecisionCorrupt("admission-decision-store-short-read")
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise AdmissionDecisionCorrupt("admission-decision-store-partial-tail")
        lines = raw.splitlines()
        if len(lines) > _MAX_RECORDS:
            raise AdmissionDecisionCorrupt("admission-decision-store-record-limit")
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for line in lines:
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise AdmissionDecisionCorrupt("admission-decision-store-invalid-json") from exc
            row_keys = set(row) if isinstance(row, dict) else set()
            if (
                not isinstance(row, dict)
                or not _DECISION_REQUIRED_KEYS.issubset(row_keys)
                or not row_keys.issubset(_DECISION_ALL_KEYS)
                or row.get("schema_version") != 1
                or not isinstance(row.get("decision_id"), str)
                or not row["decision_id"]
                or (
                    "selected_observation_state" in row
                    and row["selected_observation_state"] is not None
                    and row["selected_observation_state"] not in ("unmanaged", "known", "unknown")
                )
                or (
                    "selected_feasible" in row
                    and row["selected_feasible"] is not None
                    and not isinstance(row["selected_feasible"], bool)
                )
                or (
                    "policy_config_revision" in row
                    and row["policy_config_revision"] is not None
                    and not isinstance(row["policy_config_revision"], str)
                )
            ):
                raise AdmissionDecisionCorrupt("admission-decision-store-invalid-record")
            if row["decision_id"] in seen_ids:
                raise AdmissionDecisionCorrupt("admission-decision-store-duplicate-decision-id")
            seen_ids.add(row["decision_id"])
            records.append(row)
        return records


# ---------------------------------------------------------------------------
# restart／crash 後的 reconcile 掃描（安全 attempt 邊界的另一半：終局解決）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileOutcome:
    decision_id: str
    reservation_id: str
    action: str  # settled | reconciled | skipped
    detail: str | None = None


def reconcile_bound_reservations(
    *,
    authority: QuotaReservationAuthority,
    store: AdmissionDecisionStore,
    job_lookup: Callable[[str], Mapping[str, Any] | None],
    job_outcome: Callable[[Mapping[str, Any]], str | None],
    now_ms: int,
    renew_lease_ms: int | None = None,
    on_settled: Callable[["AdmissionDecision | None", Mapping[str, Any]], None] | None = None,
) -> tuple[ReconcileOutcome, ...]:
    """把所有 ``bound`` reservation 導向終局或續租。

    對抗審查第四輪 MAJOR（manager.py:11491）：舊實作以
    ``store.enforced_admitted()`` 反查候選——如果同一個 attempt／profile 先
    以 shadow 模式寫過一筆 receipt，之後才切到 opt-in enforce（見
    ``decision_id_for`` 對 ``mode`` 的處理），舊版 ``decision_id`` 不納入
    mode，enforce 那次的 admit receipt 寫入會被 shadow 的舊記錄擋下、永遠
    不會出現在 ``store.enforced_admitted()`` 的結果裡——即使已經修好
    decision_id 的 mode 隔離，這支收斂掃描本身仍然是『先信任 receipt 是否
    成功寫入』的間接查法，任何一次 receipt 寫入失敗（IO／損毀／衝突）都會
    讓對應的 bound reservation 對它完全隱形，容量永久卡死。改以
    ``authority.list_by_state("bound", now_ms=now_ms)`` 為出發點——本
    authority 才是 reservation 的唯一真相來源，`store` 只在需要診斷用的
    ``profile_key``（見 ``on_settled``）時才被動查詢，查不到（receipt 從未
    寫入或已損毀）不影響容量本身的收斂決定，只讓 ``on_settled`` 收到
    ``None`` 而略過那一筆的 usage 記錄。

    ``on_settled``（選填）在某筆 ``bound`` reservation 因為 job 終局被收斂成
    ``settled``（見下方 ``action="settled"`` 分支）時，以
    ``(decision_or_none, job)`` 呼叫一次——供呼叫端（例如 ``manager_daemon``）
    在同一次觀察到終局時，順手呼叫
    ``QuotaShadowService.record_terminal_usage`` 記消耗，不必另外重查一次
    job／reservation。呼叫失敗（拋例外）不影響本函式已經完成的 reconcile
    決定——額度容量的釋放與 usage 記錄是兩個獨立的失效面，usage 記錄失敗
    不該讓已經正確收斂的容量釋放結果跳回一半。

    - job 找到且 ``job_outcome`` 給出確定終局（``succeeded``／``failed``／
      ``cancelled``）→ ``settle()``。
    - job 找到但仍在跑（``job_outcome`` 回 ``None``）→ ``reconcile()`` 帶
      ``confirmed-alive`` 續 lease，讓 :attr:`quota_reservation.ReservationStatus.display_state`
      不會因為單純的 lease 過期就被標成 ``uncertain``。
      job registry 本身查不到（restart 後無法確認存活）→ ``reconcile()``
      帶 ``inconclusive``——**絕不**因為查不到就假設已終止並釋放額度（原票
      AC：restart 不能因為 TTL 過期就洗掉未確定的消耗／預留）。

    這支函式刻意獨立於 #830 的 dispatch producer 之外，不塞進任何一個既有
    job 終局處理路徑——它只依賴 job registry 的唯讀查詢，可以在啟動時、
    periodic tick 或維運腳本裡任意次呼叫，冪等（重覆呼叫對已經是終局的
    reservation 一律 no-op）。
    """
    outcomes: list[ReconcileOutcome] = []
    for status in authority.list_by_state("bound", now_ms=now_ms):
        reservation_id = status.reservation_id
        job_id = status.job_id
        job = job_lookup(job_id) if job_id else None
        if job is None:
            result = authority.reconcile(
                reservation_id=reservation_id,
                evidence={"kind": "job-registry-lookup-miss", "job_id": job_id},
                resolution="inconclusive",
                expected_sequence=status.sequence,
                now_ms=now_ms,
            )
            outcomes.append(
                ReconcileOutcome(decision_id=status.decision_id, reservation_id=reservation_id,
                                  action="reconciled", detail=f"inconclusive:{result.status}")
            )
            continue
        outcome = job_outcome(job)
        if outcome is None:
            result = authority.reconcile(
                reservation_id=reservation_id,
                evidence={"kind": "job-registry-lookup-alive", "job_id": job_id},
                resolution="confirmed-alive",
                expected_sequence=status.sequence,
                now_ms=now_ms,
                renew_lease_ms=renew_lease_ms,
            )
            outcomes.append(
                ReconcileOutcome(decision_id=status.decision_id, reservation_id=reservation_id,
                                  action="reconciled", detail=f"confirmed-alive:{result.status}")
            )
            continue
        if outcome not in ("succeeded", "failed", "cancelled"):
            outcomes.append(
                ReconcileOutcome(decision_id=status.decision_id, reservation_id=reservation_id,
                                  action="skipped", detail=f"unknown-job-outcome:{outcome!r}")
            )
            continue
        # 終局已知（succeeded／failed／cancelled）：改用
        # ``authority.reconcile()`` 的 ``confirmed-terminated`` 分支收斂——
        # 它刻意不驗證 owner_token（見 #838 文件字串），適合「另一個知道
        # 可信 liveness 證據的呼叫端」（reconcile 掃描未必是原 owner
        # process，尤其是 restart 之後）使用；``settle()`` 需要原 owner
        # 的 ``owner_token`` 才能通過驗證，這裡沒有它。
        result = authority.reconcile(
            reservation_id=reservation_id,
            evidence={"kind": "job-registry-terminal", "job_id": job_id, "job_outcome": outcome},
            resolution="confirmed-terminated",
            expected_sequence=status.sequence,
            now_ms=now_ms,
        )
        outcomes.append(
            ReconcileOutcome(decision_id=status.decision_id, reservation_id=reservation_id,
                              action="settled", detail=f"confirmed-terminated:{result.status}")
        )
        if on_settled is not None:
            try:
                decision = store.get(status.decision_id)
            except Exception:  # noqa: BLE001 - receipt 查詢失敗不影響已完成的容量收斂
                decision = None
            try:
                on_settled(decision, job)
            except Exception:  # noqa: BLE001 - usage 記錄失敗不得回滾已完成的容量釋放
                pass
    return outcomes


def reconcile_reserved_reservations(
    *,
    authority: QuotaReservationAuthority,
    job_lookup_by_decision: Callable[[str, str, str], Mapping[str, Any] | None],
    job_outcome: Callable[[Mapping[str, Any]], str | None],
    now_ms: int,
    renew_lease_ms: int | None = None,
    grace_ms: int = 0,
    in_flight: "InFlightDispatchTracker | None" = None,
) -> tuple[ReconcileOutcome, ...]:
    """把卡在 ``reserved``（``create_job()`` 耐久寫入後、``bind()`` 之前
    crash）的 reservation 導向安全的終局或維持。

    對抗審查第三輪 MAJOR（quota_admission.py:1008）：舊實作以
    ``AdmissionDecisionStore.enforced_admitted()`` 反查候選、再用
    ``authority.status()`` 確認狀態——如果合法持有者在 :func:`reserve_for_candidate`
    成功之後、寫入那筆 admit receipt 之前 crash（或 receipt 寫入本身失敗；
    見 ``AdmissionDecisionStore.record()`` 的內容衝突 fail-closed 語意），
    這筆 reservation 就從未出現在 store 的列舉裡，對這支掃描完全隱形——
    ``_committed_totals()`` 不會因為 lease 過期就自動放掉它佔的容量（見
    ``_display_state`` 文件字串：過期本身永不證明可以釋放），之後同一個
    attempt 重送會一直撞到這筆『看不見也放不掉』的 reservation，永遠卡在
    ``held-elsewhere``（見 :func:`reserve_for_candidate_with_generation_fallback`）。

    改以 ``authority.list_by_state("reserved", ...)`` 為出發點——本 authority
    才是 reservation 的唯一真相來源，不必經過任何下游 receipt 是否成功寫入
    （``run_id``／``card_id``／``attempt_id``／``decision_id`` 本來就已經是
    reservation 記錄自己的欄位，見 :class:`~.quota_reservation.ReservationStatus`）。
    #838 協定是 reserve → 建 job 記錄 → bind(job_id) → 才 spawn；因此
    ``reserved`` 狀態下這筆 reservation **恆不可能有活著的 job 在跑**——但
    仍必須以 registry 事實判定，不能只憑 lease 過期臆測（原票 AC）：

    - 這個 process 自己知道這筆 reservation 目前正在 provisioning
      （``in_flight`` 給定且 :meth:`InFlightDispatchTracker.is_in_flight`
      為真——對抗審查第四輪 MAJOR quota_admission.py:1119）→ 一律跳過
      （不反查 job、不續租、不 bind、不 release）。**這個檢查排在 job 反查
      之前**（對抗審查第五輪 MAJOR）：``create_job()`` 之後、``bind()``
      之前這段窗口內 job 記錄已經存在但未終局，若照下面『job 找到但非
      終局』分支續租，會把 sequence 往前推一格，讓原 dispatch 隨後的
      ``bind()`` 因 ``expected_sequence`` 過期而 sequence-mismatch 失敗
      ——本 process 自己就是唯一活躍的 owner，不需要靠反查 job 是否存在
      來確認存活，也不該對它寫入任何事件。
    - ``job_lookup_by_decision(run_id, card_id, decision_id)`` 找到對應建立
      的 job（crash 發生在 ``create_job()`` 之後、``bind()`` 之前）：
      - job 已終局 → 以 ``reconcile(confirmed-terminated)`` 收斂（`bind()`／
        `settle()` 都需要原 owner 的 ``owner_token``，restart 後的 sweep
        拿不到，見 ``reconcile_bound_reservations`` 同一個理由）。
      - job 仍非終局（正常情況——crash 窗口內的 job 從未真正 spawn，只是
        一筆 inert 的 registry 記錄）→ ``reconcile(confirmed-alive)`` 續
        lease，避免它只因為 lease 過期就被下一輪誤判成可回收。
    - 查無對應 job：
      - lease（含 ``grace_ms`` 寬限）已過期 → ``reconcile(confirmed-terminated)``
        收斂釋放容量，evidence 標記 ``recovered-unbound``（呼叫端等同
        「release」語意，但走 ``reconcile()``——sweep 沒有原 owner 的
        ``owner_token``，``release()`` 一樣需要它）。``grace_ms``（預設
        0，逐字沿用舊行為）是額外的安全邊界——即使 dispatch 側的續租
        （見 manager.py 的 provisioning 續租呼叫）因故沒有發生或失敗，
        也不會在 lease 剛過期的那一刻立刻被判定成可回收。
      - lease（含寬限）未過期 → 不動（可能是 ``create_job()`` 還在進行
        中，尚未寫入 registry）。
    - ``job_lookup_by_decision`` 查詢本身失敗（拋例外）→ 一律不動，不得
      因為查詢失敗就當作『查無此 job』而釋放容量。

    與 :func:`reconcile_bound_reservations` 分成兩支函式：``reserved`` 沒有
    ``job_id`` 可用，查找方式（依 ``decision_id`` 精確比對 job 建立時記錄的
    quota 決策身分——對抗審查第四輪 MAJOR manager.py:11603，取代舊版依
    ``attempt_id`` ordinal 猜測、在多 Manager instance 交錯時會誤把不相關
    候選的 job 當成證據的作法）與 ``bound``（直接用 ``status.job_id``）完全
    不同，呼叫端提供的 callback 形狀因此也不同，合併成一支只會讓兩種語意
    互相混淆。
    """
    outcomes: list[ReconcileOutcome] = []
    for status in authority.list_by_state("reserved", now_ms=now_ms):
        reservation_id = status.reservation_id
        if in_flight is not None and in_flight.is_in_flight(reservation_id):
            # #839 對抗審查修復第五輪 MAJOR（quota_admission.py 週期性
            # sweep）：這個檢查必須排在 job 反查之前——舊實作只在『查無對應
            # job』的分支才問 in_flight，但『job 已經被本 process 的
            # create_job() 建出來、bind() 還沒呼叫』這個時間點，job 反查會
            # 命中一筆非終局的 job，落到下面『job 找到但非終局』分支去
            # renew()，把 sequence 往前推一格；原 dispatch 隨後呼叫的
            # bind() 就會因為 expected_sequence 過期而 sequence-mismatch
            # 失敗。本 process 自己就是這筆 reservation 目前唯一活躍的
            # owner，不需要（也不該）靠反查 job 存不存在／終不終局來確認
            # 存活；一律跳過——不續租（不呼叫 reconcile／renew）、不
            # bind、不 release，讓正在進行中的 dispatch 自己（見 manager.py
            # 的 provisioning 續租呼叫）決定要不要續租，sweep 這一輪什麼都
            # 不做，下一輪再看。
            outcomes.append(
                ReconcileOutcome(decision_id=status.decision_id, reservation_id=reservation_id,
                                  action="skipped", detail="in-flight-dispatch-provisioning")
            )
            continue
        try:
            job = job_lookup_by_decision(status.run_id, status.card_id, status.decision_id)
        except Exception:  # noqa: BLE001 - 查詢失敗一律不動，不得誤判成『查無』而釋放
            outcomes.append(
                ReconcileOutcome(decision_id=status.decision_id, reservation_id=reservation_id,
                                  action="skipped", detail="job-lookup-failed")
            )
            continue
        if job is None:
            if status.lease_expires_at_ms + grace_ms > now_ms:
                # lease（含寬限）未過期——可能是 create_job() 仍在進行中，不動。
                outcomes.append(
                    ReconcileOutcome(decision_id=status.decision_id, reservation_id=reservation_id,
                                      action="skipped", detail="lease-not-expired")
                )
                continue
            result = authority.reconcile(
                reservation_id=reservation_id,
                evidence={"kind": "reserved-unbound-lease-expired", "reason": "recovered-unbound"},
                resolution="confirmed-terminated",
                expected_sequence=status.sequence,
                now_ms=now_ms,
            )
            outcomes.append(
                ReconcileOutcome(decision_id=status.decision_id, reservation_id=reservation_id,
                                  action="settled", detail=f"recovered-unbound:{result.status}")
            )
            continue
        outcome = job_outcome(job)
        if outcome in ("succeeded", "failed", "cancelled"):
            # 防禦性分支：協定上 reserved 不該有終局 job（spawn 一定在 bind
            # 之後），但仍依 registry 事實而非協定假設判定。
            result = authority.reconcile(
                reservation_id=reservation_id,
                evidence={"kind": "job-registry-terminal-unbound", "job_outcome": outcome},
                resolution="confirmed-terminated",
                expected_sequence=status.sequence,
                now_ms=now_ms,
            )
            outcomes.append(
                ReconcileOutcome(decision_id=status.decision_id, reservation_id=reservation_id,
                                  action="settled", detail=f"confirmed-terminated:{result.status}")
            )
            continue
        # job 找到但非終局（正常情況——crash 窗口內從未真正 spawn 過）：
        # 續 lease，維持 reserved，不釋放容量。
        result = authority.reconcile(
            reservation_id=reservation_id,
            evidence={"kind": "job-registry-lookup-alive-unbound"},
            resolution="confirmed-alive",
            expected_sequence=status.sequence,
            now_ms=now_ms,
            renew_lease_ms=renew_lease_ms,
        )
        outcomes.append(
            ReconcileOutcome(decision_id=status.decision_id, reservation_id=reservation_id,
                              action="reconciled", detail=f"confirmed-alive:{result.status}")
        )
    return outcomes


# ---------------------------------------------------------------------------
# operator-owned quota-pools 設定檔（schema ``cortex/quota-pools/v1``）——
# production 接線：manager_daemon 讀這份檔案建構 DispatchContext，本模組只
# 消費 #836 既有的 parse_pool_descriptor／parse_unit_definition／parse_binding
# 驗證，不自寫第二套 schema 驗證。
# ---------------------------------------------------------------------------

QUOTA_POOLS_CONFIG_SCHEMA = "cortex/quota-pools/v1"

_QUOTA_POOLS_CONFIG_REQUIRED_KEYS = frozenset(
    {"schema", "config_revision", "descriptors", "unit_catalog", "bindings"}
)
#: `collector_targets` 屬於 #836 provider collector（`quota_collectors.load_collector_config`
#: 解析並驗證）；同一份設定檔同時供 admission 與 collector 使用，admission 只接受
#: 這個鍵存在、不解讀其內容（live 驗收發現：封閉鍵集合把它當未知鍵，shadow 整條
#: 退回「沒接上」）。
_QUOTA_POOLS_CONFIG_OPTIONAL_KEYS = frozenset({"lease_ms", "usage_unit_refs", "collector_targets"})
_QUOTA_POOLS_CONFIG_ALL_KEYS = _QUOTA_POOLS_CONFIG_REQUIRED_KEYS | _QUOTA_POOLS_CONFIG_OPTIONAL_KEYS
_MAX_LEASE_MS_CONFIG = 31_622_400_000  # 一年——單純防呆上限，比照 #838 的常數
#: 對抗審查第四輪 MAJOR（quota_admission.py:1119）：lease 太短時，正常的
#: worktree／sandbox provisioning 耗時就足以撞上 lease 過期——即使已經補上
#: dispatch 側的 provisioning 續租與 sweep 的 in-flight／grace 排除，太短
#: 的 lease 仍會讓這些安全網疲於奔命地追續租，任何一次續租延遲都可能被
#: sweep 誤判為 crash 殘留。1 分鐘是 worktree／sandbox 建立在正常負載下的
#: 保守上界，設定值低於它視為設定錯誤，直接拒絕載入（呼叫端據此回
#: ``quota-config-invalid``），不是業務語意，純粹是防呆下限。
_MIN_LEASE_MS_CONFIG = 60_000


class QuotaPoolsConfigError(ValueError):
    """quota-pools 設定檔結構或內容不合法——呼叫端 fail closed（見票面 a）。"""


@dataclass(frozen=True)
class QuotaPoolsConfig:
    """:func:`load_quota_pools_config` 的回傳型——已驗證、可直接餵進
    :class:`DispatchContext` 的內容。"""

    config_revision: str
    descriptors: tuple[schema.PoolDescriptor, ...]
    unit_catalog: tuple[schema.UnitDefinition, ...]
    bindings: tuple[schema.ProfilePoolBinding, ...]
    lease_ms: int
    usage_unit_refs: Mapping[str, tuple[str, str]]


def parse_quota_pools_config(payload: object) -> QuotaPoolsConfig:
    """驗證並解析一份 ``cortex/quota-pools/v1`` 設定檔的已解碼 JSON 內容。

    完全重用 #836 ``quota_observation`` 的 ``parse_pool_descriptor``／
    ``parse_unit_definition``／``parse_binding``——本函式只負責這份設定檔
    自己的外層 envelope（``schema``／``config_revision``／選填欄位），不重
    寫任何 pool／unit／binding 的形狀驗證。任何一步失敗都 fail closed，拋
    :class:`QuotaPoolsConfigError`（呼叫端據此決定 shadow 降級記錄或
    enforce 下的 ``quota-config-invalid``）。
    """
    if not isinstance(payload, Mapping):
        raise QuotaPoolsConfigError("quota-pools-config-not-a-mapping")
    keys = set(payload)
    if not _QUOTA_POOLS_CONFIG_REQUIRED_KEYS.issubset(keys):
        raise QuotaPoolsConfigError("quota-pools-config-missing-required-key")
    if not keys.issubset(_QUOTA_POOLS_CONFIG_ALL_KEYS):
        raise QuotaPoolsConfigError("quota-pools-config-unknown-key")
    if payload.get("schema") != QUOTA_POOLS_CONFIG_SCHEMA:
        raise QuotaPoolsConfigError("quota-pools-config-unknown-schema")
    config_revision = payload.get("config_revision")
    if not isinstance(config_revision, str) or not config_revision:
        raise QuotaPoolsConfigError("quota-pools-config-invalid-config-revision")

    raw_unit_catalog = payload.get("unit_catalog")
    if not isinstance(raw_unit_catalog, list):
        raise QuotaPoolsConfigError("quota-pools-config-invalid-unit-catalog")
    try:
        unit_catalog = tuple(schema.parse_unit_definition(item) for item in raw_unit_catalog)
    except (schema.QuotaContractError, TypeError, ValueError) as exc:
        raise QuotaPoolsConfigError("quota-pools-config-invalid-unit-definition") from exc

    raw_descriptors = payload.get("descriptors")
    if not isinstance(raw_descriptors, list) or not raw_descriptors:
        raise QuotaPoolsConfigError("quota-pools-config-invalid-descriptors")
    try:
        descriptors = tuple(schema.parse_pool_descriptor(item) for item in raw_descriptors)
    except (schema.QuotaContractError, TypeError, ValueError) as exc:
        raise QuotaPoolsConfigError("quota-pools-config-invalid-pool-descriptor") from exc

    raw_bindings = payload.get("bindings")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise QuotaPoolsConfigError("quota-pools-config-invalid-bindings")
    try:
        bindings = tuple(
            schema.parse_binding(item, descriptors=descriptors) for item in raw_bindings
        )
    except (schema.QuotaContractError, TypeError, ValueError) as exc:
        raise QuotaPoolsConfigError("quota-pools-config-invalid-binding") from exc

    lease_ms = payload.get("lease_ms", 900_000)
    if (
        type(lease_ms) is not int
        or lease_ms < _MIN_LEASE_MS_CONFIG
        or lease_ms > _MAX_LEASE_MS_CONFIG
    ):
        raise QuotaPoolsConfigError("quota-pools-config-invalid-lease-ms")

    raw_usage_unit_refs = payload.get("usage_unit_refs", {})
    if not isinstance(raw_usage_unit_refs, dict):
        raise QuotaPoolsConfigError("quota-pools-config-invalid-usage-unit-refs")
    usage_unit_refs: dict[str, tuple[str, str]] = {}
    for metric, ref in raw_usage_unit_refs.items():
        if (
            not isinstance(metric, str) or not metric
            or not isinstance(ref, list) or len(ref) != 2
            or not all(isinstance(part, str) and part for part in ref)
        ):
            raise QuotaPoolsConfigError("quota-pools-config-invalid-usage-unit-ref-entry")
        usage_unit_refs[metric] = (ref[0], ref[1])

    return QuotaPoolsConfig(
        config_revision=config_revision,
        descriptors=descriptors,
        unit_catalog=unit_catalog,
        bindings=bindings,
        lease_ms=lease_ms,
        usage_unit_refs=usage_unit_refs,
    )


def load_quota_pools_config(path: str | Path | None = None) -> QuotaPoolsConfig | None:
    """讀取並解析 operator-owned quota-pools 設定檔。

    ``path`` 缺省時讀 :func:`paulsha_cortex.config.paths.quota_pools_config_path`
    （支援 ``PSC_QUOTA_POOLS_CONFIG`` 覆寫）。**檔案不存在時回 ``None``**——
    這是唯一「完全不接線」的分支，行為與 #839 落地前逐字相同（見票面 a：
    「檔案不存在 → context 為 None」）。存在但內容不合法時一律拋
    :class:`QuotaPoolsConfigError`，不吞例外、不靜默退回 None——shadow／
    enforce 兩種模式如何降級是呼叫端（``manager_daemon``）的責任，本函式
    只保證『合法才回傳可用物件，不合法一定讓呼叫端知道』。
    """
    if path is None:
        from paulsha_cortex.config.paths import quota_pools_config_path

        path = quota_pools_config_path()
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise QuotaPoolsConfigError("quota-pools-config-read-failed") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise QuotaPoolsConfigError("quota-pools-config-invalid-json") from exc
    return parse_quota_pools_config(payload)
