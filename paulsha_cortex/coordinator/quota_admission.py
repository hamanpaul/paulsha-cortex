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
    "ReconcileOutcome",
    "DispatchContext",
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


def _pool_key(pool_ref: Mapping[str, str]) -> tuple[str, str, str, str]:
    return tuple(pool_ref[key] for key in ("authority_id", "account_id", "pool_id", "revision"))


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


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
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_parent()
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise AdmissionDecisionCorrupt("admission-decision-store-open-failed") from exc
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
) -> tuple[ReconcileOutcome, ...]:
    """掃描曾經 enforced-admit 的決策，把 ``bound`` reservation 導向終局。

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
    return outcomes
