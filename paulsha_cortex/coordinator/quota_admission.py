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
    "estimate_demand",
    "assess_candidate_quota",
    "observation_fingerprint",
    "decision_id_for",
    "build_pool_demands",
    "build_capacity_by_pool",
    "reserve_for_candidate",
    "settle_reservation_after_spawn_failure",
    "release_reservation_before_spawn",
    "reconcile_bound_reservations",
    "reconcile_reserved_reservations",
    "ReconcileOutcome",
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


def pools_for_profile(
    profile_key: str,
    *,
    bindings: Sequence[schema.ProfilePoolBinding],
) -> tuple[tuple[dict[str, str], str], ...]:
    """從 #836 的 ``ProfilePoolBinding`` 找出這個 profile 綁定的 pool/window。

    找不到任何 binding 時回空 tuple——代表這個 profile 目前不受 quota 管理
    （例如尚未替這個 executor 設定觀測來源），本模組據此視為『不受額度限制』
    （見 :func:`assess_candidate_quota` 的 ``observation_state="unmanaged"``
    分支），維持既有派工行為，不因為尚未接上真實觀測就無故擋派。
    """
    matched: list[tuple[dict[str, str], str]] = []
    seen: set[tuple[tuple[str, str, str, str], str]] = set()
    for binding in bindings:
        wire = binding.to_dict()
        subject = wire.get("subject")
        if not isinstance(subject, dict):
            continue
        kind = subject.get("kind")
        if kind == "profile":
            profile_ref = subject.get("profile_ref")
            if not isinstance(profile_ref, dict) or profile_ref.get("state") != "known":
                continue
            value = profile_ref.get("value")
            if not isinstance(value, dict) or value.get("key") != profile_key:
                continue
        elif kind == "group":
            members = subject.get("members")
            if not isinstance(members, dict) or members.get("state") != "known":
                continue
            member_keys = {
                item.get("key")
                for item in members.get("value", [])
                if isinstance(item, dict)
            }
            if profile_key not in member_keys:
                continue
        else:
            continue
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
                key = (_pool_key(pool_ref), window_id)
            except KeyError:
                continue
            if key in seen:
                continue
            seen.add(key)
            matched.append((dict(pool_ref), window_id))
    return tuple(matched)


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
    """
    pool_windows = pools_for_profile(profile_key, bindings=bindings)
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
            coverage_gaps=tuple(sorted(all_gaps)),
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


def decision_id_for(*, run_id: str, card_id: str, attempt_id: str, profile_key: str) -> str:
    """給定 (run, card, attempt, profile) 的穩定 decision_id。

    同一個 attempt 重送（restart／resume／late terminal 補送）算出同一個
    decision_id，因此後續 :func:`reserve_for_candidate` 呼叫
    ``QuotaReservationAuthority.reserve()`` 天然冪等——不會因為重送而重新
    扣一次額度，也不會誤造第二個 job。真正的新 attempt（安全 attempt 邊界
    fallback 之後）必須帶新的 ``attempt_id``，才會算出不同的 decision_id。
    """
    digest = hashlib.sha256(
        f"{run_id}\0{card_id}\0{attempt_id}\0{profile_key}".encode()
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
        )


_DECISION_REQUIRED_KEYS = frozenset(
    {
        "schema_version", "decision_id", "run_id", "card_id", "attempt_id", "profile_key",
        "mode", "outcome", "policy_version", "observation_version", "demand_version",
        "qualification_version", "generated_at_ms", "selected", "reservation_id", "excluded",
        "reason", "job_id",
    }
)


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
                if existing == row:
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
            if (
                not isinstance(row, dict)
                or set(row) != _DECISION_REQUIRED_KEYS
                or row.get("schema_version") != 1
                or not isinstance(row.get("decision_id"), str)
                or not row["decision_id"]
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
    on_settled: Callable[[AdmissionDecision, Mapping[str, Any]], None] | None = None,
) -> tuple[ReconcileOutcome, ...]:
    """掃描曾經 enforced-admit 的決策，把 ``bound`` reservation 導向終局。

    ``on_settled``（選填）在某筆 ``bound`` reservation 因為 job 終局被收斂成
    ``settled``（見下方 ``action="settled"`` 分支）時，以 ``(decision, job)``
    呼叫一次——供呼叫端（例如 ``manager_daemon``）在同一次觀察到終局時，
    順手呼叫 ``QuotaShadowService.record_terminal_usage`` 記消耗，不必另外
    重查一次 job／reservation。呼叫失敗（拋例外）不影響本函式已經完成的
    reconcile 決定——額度容量的釋放與 usage 記錄是兩個獨立的失效面，usage
    記錄失敗不該讓已經正確收斂的容量釋放結果跳回一半。

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
    for decision in store.enforced_admitted():
        reservation_id = decision.reservation_id
        if not reservation_id:
            continue
        status = authority.status(reservation_id, now_ms=now_ms)
        if status is None or status.state != "bound":
            continue
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
                ReconcileOutcome(decision_id=decision.decision_id, reservation_id=reservation_id,
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
                ReconcileOutcome(decision_id=decision.decision_id, reservation_id=reservation_id,
                                  action="reconciled", detail=f"confirmed-alive:{result.status}")
            )
            continue
        if outcome not in ("succeeded", "failed", "cancelled"):
            outcomes.append(
                ReconcileOutcome(decision_id=decision.decision_id, reservation_id=reservation_id,
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
            ReconcileOutcome(decision_id=decision.decision_id, reservation_id=reservation_id,
                              action="settled", detail=f"confirmed-terminated:{result.status}")
        )
        if on_settled is not None:
            try:
                on_settled(decision, job)
            except Exception:  # noqa: BLE001 - usage 記錄失敗不得回滾已完成的容量釋放
                pass
    return outcomes


def reconcile_reserved_reservations(
    *,
    authority: QuotaReservationAuthority,
    store: AdmissionDecisionStore,
    job_lookup_by_attempt: Callable[[str, str, str], Mapping[str, Any] | None],
    job_outcome: Callable[[Mapping[str, Any]], str | None],
    now_ms: int,
    renew_lease_ms: int | None = None,
) -> tuple[ReconcileOutcome, ...]:
    """掃描曾經 enforced-admit 的決策，把卡在 ``reserved``（``create_job()``
    耐久寫入後、``bind()`` 之前 crash）的 reservation 導向安全的終局或維持。

    #838 協定是 reserve → 建 job 記錄 → bind(job_id) → 才 spawn；因此
    ``reserved`` 狀態下這筆 reservation **恆不可能有活著的 job 在跑**——但
    仍必須以 registry 事實判定，不能只憑 lease 過期臆測（原票 AC）：

    - ``job_lookup_by_attempt(run_id, card_id, attempt_id)`` 找到對應建立的
      job（crash 發生在 ``create_job()`` 之後、``bind()`` 之前）：
      - job 已終局 → 以 ``reconcile(confirmed-terminated)`` 收斂（`bind()`／
        `settle()` 都需要原 owner 的 ``owner_token``，restart 後的 sweep
        拿不到，見 ``reconcile_bound_reservations`` 同一個理由）。
      - job 仍非終局（正常情況——crash 窗口內的 job 從未真正 spawn，只是
        一筆 inert 的 registry 記錄）→ ``reconcile(confirmed-alive)`` 續
        lease，避免它只因為 lease 過期就被下一輪誤判成可回收。
    - 查無對應 job：
      - lease 已過期 → ``reconcile(confirmed-terminated)`` 收斂釋放容量，
        evidence 標記 ``recovered-unbound``（呼叫端等同「release」語意，但
        走 ``reconcile()``——sweep 沒有原 owner 的 ``owner_token``，
        ``release()`` 一樣需要它）。
      - lease 未過期 → 不動（可能是 ``create_job()`` 還在進行中，尚未
        寫入 registry）。
    - ``job_lookup_by_attempt`` 查詢本身失敗（拋例外）→ 一律不動，不得因為
      查詢失敗就當作『查無此 job』而釋放容量。

    與 :func:`reconcile_bound_reservations` 分成兩支函式：``reserved`` 沒有
    ``job_id`` 可用，查找方式（依 ``attempt_id`` 的 ordinal 反查 registry）
    與 ``bound``（直接用 ``status.job_id``）完全不同，呼叫端提供的 callback
    形狀因此也不同，合併成一支只會讓兩種語意互相混淆。
    """
    outcomes: list[ReconcileOutcome] = []
    for decision in store.enforced_admitted():
        reservation_id = decision.reservation_id
        if not reservation_id:
            continue
        status = authority.status(reservation_id, now_ms=now_ms)
        if status is None or status.state != "reserved":
            continue
        try:
            job = job_lookup_by_attempt(decision.run_id, decision.card_id, decision.attempt_id)
        except Exception:  # noqa: BLE001 - 查詢失敗一律不動，不得誤判成『查無』而釋放
            outcomes.append(
                ReconcileOutcome(decision_id=decision.decision_id, reservation_id=reservation_id,
                                  action="skipped", detail="job-lookup-failed")
            )
            continue
        if job is None:
            if status.lease_expires_at_ms > now_ms:
                # lease 未過期——可能是 create_job() 仍在進行中，不動。
                outcomes.append(
                    ReconcileOutcome(decision_id=decision.decision_id, reservation_id=reservation_id,
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
                ReconcileOutcome(decision_id=decision.decision_id, reservation_id=reservation_id,
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
                ReconcileOutcome(decision_id=decision.decision_id, reservation_id=reservation_id,
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
            ReconcileOutcome(decision_id=decision.decision_id, reservation_id=reservation_id,
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
_QUOTA_POOLS_CONFIG_OPTIONAL_KEYS = frozenset({"lease_ms", "usage_unit_refs"})
_QUOTA_POOLS_CONFIG_ALL_KEYS = _QUOTA_POOLS_CONFIG_REQUIRED_KEYS | _QUOTA_POOLS_CONFIG_OPTIONAL_KEYS
_MAX_LEASE_MS_CONFIG = 31_622_400_000  # 一年——單純防呆上限，比照 #838 的常數


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
    if type(lease_ms) is not int or lease_ms <= 0 or lease_ms > _MAX_LEASE_MS_CONFIG:
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
