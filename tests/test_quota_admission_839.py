"""#839：額度感知准入與安全 attempt 邊界 fallback。

驗收對應原票「最小機械驗收」七條：
- AC1：原池耗盡同池替代拒絕、獨立池合格替代可選；短窗夠週窗不足也拒絕。
- AC2：negative——unknown remaining、無綁定（unmanaged）零 spawn且精確拒因。
- AC3：兩 process barrier 共 #838 reservation authority，race 落敗不造假 job。
- AC4：fake executor 派前可用、spawn 時視為 429（settle failed）耐久記消耗。
- AC5：同 decision 不重派（reserve 冪等）；restart 不確定 liveness 不洗預算。
- AC6：真 producer 形狀（reconcile 掃描不假造 job_id、不誤放 bound）。
- AC7：shadow 預設不改既有結果；opt-in 開關語意。
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import quota_admission as admission
from paulsha_cortex.coordinator import quota_observation as schema
from paulsha_cortex.coordinator.quota_ledger import QuotaEventLedger
from paulsha_cortex.coordinator.quota_reservation import PoolDemand, QuotaReservationAuthority
from paulsha_cortex.coordinator.quota_shadow import QuotaShadowService


_PROFILE_A = "epk:v1:resolved:" + "a" * 64
_PROFILE_B = "epk:v1:resolved:" + "b" * 64
_NOW = 1_800_000_000_000


def _profile_ref(key: str) -> dict[str, object]:
    return {"state": "known", "value": {"schema_version": 1, "key": key}}


def _pool_descriptor(
    *,
    account: str = "account-shared",
    pool: str = "pool-shared",
    unit_id: str = "token",
    windows: tuple[tuple[str, int], ...] = (("short", 300_000), ("week", 604_800_000)),
) -> schema.PoolDescriptor:
    unit = {
        "unit_id": unit_id, "version": "1", "quantity_kind": "amount",
        "semantics_ref": "fixture:native-token/v1",
    }
    return schema.parse_pool_descriptor(
        {
            "schema_version": 1,
            "authority_id": "operator-budget-authority",
            "account_id": account,
            "pool_id": pool,
            "revision": "1",
            "authority_ref": "fixture:operator-pool-map/v1",
            "provenance_refs": ["fixture:pool-map/v1"],
            "units": [unit],
            "windows": [
                {
                    "window_id": window_id, "kind": "rolling",
                    "unit_ref": {"unit_id": unit_id, "version": "1"},
                    "duration_ms": duration_ms,
                }
                for window_id, duration_ms in windows
            ],
        }
    )


def _pool_ref(descriptor: schema.PoolDescriptor) -> dict[str, str]:
    return {
        "authority_id": descriptor.authority_id, "account_id": descriptor.account_id,
        "pool_id": descriptor.pool_id, "revision": descriptor.revision,
    }


def _constraints(descriptors) -> list[dict[str, object]]:
    constraints = []
    for descriptor in descriptors:
        for window in descriptor.to_dict()["windows"]:
            constraints.append(
                {"state": "known", "value": {"pool_ref": _pool_ref(descriptor), "window_id": window["window_id"]}}
            )
    return constraints


def _binding(descriptors, profile_key: str, *, binding_id: str = "binding") -> schema.ProfilePoolBinding:
    return schema.parse_binding(
        {
            "schema_version": 1, "binding_id": binding_id, "revision": "1",
            "subject": {"kind": "profile", "profile_ref": _profile_ref(profile_key)},
            "constraints": _constraints(descriptors),
            "coverage": {"state": "complete", "gaps": []},
        },
        descriptors=tuple(descriptors),
    )


def _observation(
    descriptor, window_id: str, *, value: str, observed_at_ms: int, profile_key: str = _PROFILE_A,
    unit_id: str = "token",
) -> schema.QuotaObservation:
    payload = {
        "schema_version": 1,
        "observation_id": f"fixture-{descriptor.pool_id}-{window_id}-{observed_at_ms}",
        "scope": {"state": "known", "value": {"pool_ref": _pool_ref(descriptor), "window_id": window_id}},
        "profile_ref": _profile_ref(profile_key),
        "unit_ref": {"state": "known", "value": {"unit_id": unit_id, "version": "1"}},
        "window_instance": {"kind": "unknown", "reason": "missing-window-instance"},
        "measurement": {
            "kind": "remaining_snapshot", "metric_id": "remaining",
            "quantity": {"state": "observed", "amount": {"kind": "exact", "value": value}},
        },
        "observed_at_ms": {"state": "known", "value": observed_at_ms},
        "received_at_ms": observed_at_ms,
        "ttl_ms": {"state": "known", "value": 60_000},
        "reset_at_ms": {"state": "unknown", "reason": "missing-reset"},
        "source": {
            "source_id": "fixture-provider", "source_schema": "fixture-quota-v1",
            "adapter_version": "fixture-adapter-v1", "authority_ref": "fixture:provider-contract/v1",
            "method": "provider_status", "provenance_refs": ["fixture:source-document/v1"],
            "event_identity": {"state": "unknown", "reason": "provider-has-no-event-id"},
        },
        "coverage": {"state": "complete", "gaps": []},
    }
    return schema.parse_observation(payload, descriptors=(descriptor,), unit_catalog=())


def _shadow_with(descriptor, window_id: str, value: str, *, profile_key: str = _PROFILE_A) -> QuotaShadowService:
    service = QuotaShadowService.in_memory()
    observation = _observation(descriptor, window_id, value=value, observed_at_ms=_NOW, profile_key=profile_key)
    result = service.record_observation(observation.to_dict(), descriptors=(descriptor,), unit_catalog=())
    assert result.accepted == 1
    return service


# ---------------------------------------------------------------------------
# AC7：shadow 預設不改既有結果；opt-in 開關語意
# ---------------------------------------------------------------------------


def test_enforcement_flag_defaults_off_and_only_on_is_true() -> None:
    assert admission.quota_admission_enabled({}) is False
    assert admission.quota_admission_enabled({"PSC_QUOTA_ADMISSION_ENFORCE": "off"}) is False
    assert admission.quota_admission_enabled({"PSC_QUOTA_ADMISSION_ENFORCE": "garbage"}) is False
    assert admission.quota_admission_enabled({"PSC_QUOTA_ADMISSION_ENFORCE": "ON"}) is True
    assert admission.quota_admission_enabled({"PSC_QUOTA_ADMISSION_ENFORCE": "on"}) is True


# ---------------------------------------------------------------------------
# AC1／AC2：pools_for_profile／assess_candidate_quota
# ---------------------------------------------------------------------------


def test_unmanaged_profile_without_binding_is_trivially_feasible() -> None:
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    shadow = _shadow_with(descriptor, "short", "0")
    assessment, demand_version = admission.assess_candidate_quota(
        executor="claude", model_id="sonnet", independence_domain="claude",
        profile_key=_PROFILE_B,  # 沒有任何 binding 指到這個 profile
        bindings=(_binding((descriptor,), _PROFILE_A),),
        descriptors=(descriptor,), unit_catalog=(), shadow=shadow, now_ms=_NOW,
    )
    assert assessment.feasible is True
    assert assessment.observation_state == "unmanaged"
    assert assessment.pools == ()
    assert demand_version == "not-applicable"


def test_ac1_short_window_sufficient_but_weekly_pool_exhausted_denies() -> None:
    descriptor = _pool_descriptor(windows=(("short", 300_000), ("week", 604_800_000)))
    shadow = QuotaShadowService.in_memory()
    for window_id, value in (("short", "50"), ("week", "0")):
        observation = _observation(descriptor, window_id, value=value, observed_at_ms=_NOW)
        shadow.record_observation(observation.to_dict(), descriptors=(descriptor,), unit_catalog=())
    binding = _binding((descriptor,), _PROFILE_A)
    assessment, _ = admission.assess_candidate_quota(
        executor="claude", model_id="sonnet", independence_domain="claude", profile_key=_PROFILE_A,
        bindings=(binding,), descriptors=(descriptor,), unit_catalog=(), shadow=shadow, now_ms=_NOW,
    )
    assert assessment.feasible is False
    assert assessment.exclusion_reason == "insufficient-quota"
    week_row = next(pool for pool in assessment.pools if pool.window_id == "week")
    assert week_row.assessment == "insufficient"
    short_row = next(pool for pool in assessment.pools if pool.window_id == "short")
    assert short_row.assessment == "sufficient"


def test_ac1_independent_pool_with_quota_is_feasible_same_pool_alternative_is_not() -> None:
    exhausted = _pool_descriptor(account="account-a", pool="pool-a", windows=(("short", 300_000),))
    independent = _pool_descriptor(account="account-b", pool="pool-b", windows=(("short", 300_000),))
    shadow = QuotaShadowService.in_memory()
    for descriptor, value in ((exhausted, "0"), (independent, "50")):
        observation = _observation(descriptor, "short", value=value, observed_at_ms=_NOW)
        shadow.record_observation(observation.to_dict(), descriptors=(descriptor,), unit_catalog=())
    descriptors = (exhausted, independent)
    # 同池換 model：仍綁同一個 exhausted pool（換模型名字不能偷換到不同 authority）。
    same_pool_binding = _binding((exhausted,), _PROFILE_A, binding_id="same-pool")
    same_pool_assessment, _ = admission.assess_candidate_quota(
        executor="claude", model_id="opus", independence_domain="claude", profile_key=_PROFILE_A,
        bindings=(same_pool_binding,), descriptors=descriptors, unit_catalog=(), shadow=shadow, now_ms=_NOW,
    )
    assert same_pool_assessment.feasible is False
    assert same_pool_assessment.exclusion_reason == "insufficient-quota"

    independent_binding = _binding((independent,), _PROFILE_B, binding_id="independent-pool")
    independent_assessment, _ = admission.assess_candidate_quota(
        executor="codex", model_id="gpt-5", independence_domain="codex", profile_key=_PROFILE_B,
        bindings=(independent_binding,), descriptors=descriptors, unit_catalog=(), shadow=shadow, now_ms=_NOW,
    )
    assert independent_assessment.feasible is True


def test_ac2_unknown_remaining_denies_with_precise_reason() -> None:
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    shadow = QuotaShadowService.in_memory()  # 沒有任何觀測 -> missing-snapshot -> unknown
    binding = _binding((descriptor,), _PROFILE_A)
    assessment, _ = admission.assess_candidate_quota(
        executor="claude", model_id="sonnet", independence_domain="claude", profile_key=_PROFILE_A,
        bindings=(binding,), descriptors=(descriptor,), unit_catalog=(), shadow=shadow, now_ms=_NOW,
    )
    assert assessment.feasible is False
    assert assessment.exclusion_reason == "unknown-remaining-quota"
    assert assessment.pools[0].assessment == "unknown"
    assert "missing-snapshot" in assessment.pools[0].coverage_gaps


def test_custom_demand_estimator_requires_explicit_version() -> None:
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    with pytest.raises(ValueError):
        admission.estimate_demand(
            [(_pool_ref(descriptor), "short")], estimator=lambda pool_ref, window_id: "2",
        )


# ---------------------------------------------------------------------------
# AC3：兩 process barrier 共 reservation authority，race 落敗不造假 job
# ---------------------------------------------------------------------------


def _race_worker(store_path: str, attempt_id: str, barrier, result_queue) -> None:
    authority = QuotaReservationAuthority(store_path)
    descriptor_pool_ref = {
        "authority_id": "operator-budget-authority", "account_id": "account-shared",
        "pool_id": "pool-shared", "revision": "1",
    }
    from paulsha_cortex.coordinator.quota_admission import CandidateAssessment, PoolAssessment

    assessment = CandidateAssessment(
        executor="claude", model_id="sonnet", independence_domain="claude", profile_key=_PROFILE_A,
        feasible=True, exclusion_reason=None,
        pools=(
            PoolAssessment(
                pool_ref=descriptor_pool_ref, window_id="week",
                remaining={"state": "observed", "amount": {"kind": "exact", "value": "1"}},
                demand="1", assessment="sufficient", coverage_gaps=(),
            ),
        ),
        observation_state="known", coverage_gaps=(),
    )
    barrier.wait(timeout=10)
    try:
        from paulsha_cortex.coordinator import quota_admission as admission_module

        result = admission_module.reserve_for_candidate(
            authority, run_id="run-1", card_id="card-1",
            decision_id=admission_module.decision_id_for(
                run_id="run-1", card_id="card-1", attempt_id=attempt_id, profile_key=_PROFILE_A,
                mode="enforced",
            ),
            attempt_id=attempt_id, assessment=assessment,
            observation_version="obs-v1", demand_version="demand-v1", lease_ms=60_000, now_ms=_NOW,
        )
        result_queue.put((attempt_id, result.status))
    except Exception as exc:  # noqa: BLE001 - 回報給 parent 判斷，不吞掉
        result_queue.put((attempt_id, f"error:{exc!r}"))


def test_ac3_two_process_barrier_one_unit_pool_exactly_one_winner(tmp_path: Path) -> None:
    store_path = str(tmp_path / "reservations.jsonl")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(target=_race_worker, args=(store_path, f"attempt-{i}", barrier, result_queue))
        for i in range(2)
    ]
    for process in processes:
        process.start()
    results = [result_queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    statuses = [status for _, status in results]
    assert statuses.count("granted") == 1, results
    assert statuses.count("denied") == 1, results


# ---------------------------------------------------------------------------
# AC4／AC5：fake executor 派前可用、spawn 時 429；reserve 冪等；bind→settle
# ---------------------------------------------------------------------------


def _feasible_assessment(descriptor) -> "admission.CandidateAssessment":
    return admission.CandidateAssessment(
        executor="claude", model_id="sonnet", independence_domain="claude", profile_key=_PROFILE_A,
        feasible=True, exclusion_reason=None,
        pools=(
            admission.PoolAssessment(
                pool_ref=_pool_ref(descriptor), window_id="short",
                remaining={"state": "observed", "amount": {"kind": "exact", "value": "5"}},
                demand="1", assessment="sufficient", coverage_gaps=(),
            ),
        ),
        observation_state="known", coverage_gaps=(),
    )


def test_ac4_spawn_time_429_settles_failed_and_frees_capacity_for_next_attempt(tmp_path: Path) -> None:
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    assessment = _feasible_assessment(descriptor)
    decision_id = admission.decision_id_for(
        run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A,
        mode="enforced",
    )
    reserved = admission.reserve_for_candidate(
        authority, run_id="run-1", card_id="card-1", decision_id=decision_id, attempt_id="job-1",
        assessment=assessment, observation_version="obs-v1", demand_version="demand-v1",
        lease_ms=60_000, now_ms=_NOW,
    )
    assert reserved.status == "granted"
    bound = authority.bind(
        reservation_id=reserved.reservation_id, owner_token=reserved.owner_token,
        attempt_id="job-1", job_id="job-1", expected_sequence=reserved.sequence, now_ms=_NOW,
    )
    assert bound.status == "ok"
    # fake executor：spawn 時 429（launcher.launch 丟例外）——settle(failed) 記消耗，
    # 不是品質判斷。
    settled = admission.settle_reservation_after_spawn_failure(
        authority, reservation_id=reserved.reservation_id, owner_token=reserved.owner_token,
        attempt_id="job-1", expected_sequence=bound.sequence, now_ms=_NOW + 1,
        note="fake-executor-429",
    )
    assert settled.status == "ok"
    assert settled.state == "settled"
    # 該 pool 容量已釋回（settled 不再計入 committed）。
    committed = authority.committed(now_ms=_NOW + 2)
    key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    assert committed.get(key, "0") == "0"


def test_ac5_same_attempt_reserve_is_idempotent_not_double_spent(tmp_path: Path) -> None:
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    assessment = _feasible_assessment(descriptor)
    decision_id = admission.decision_id_for(
        run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A,
        mode="enforced",
    )
    first = admission.reserve_for_candidate(
        authority, run_id="run-1", card_id="card-1", decision_id=decision_id, attempt_id="job-1",
        assessment=assessment, observation_version="obs-v1", demand_version="demand-v1",
        lease_ms=60_000, now_ms=_NOW,
    )
    # 重送同一個 attempt（restart／resume 補送）：同一個 decision_id，冪等回放。
    second = admission.reserve_for_candidate(
        authority, run_id="run-1", card_id="card-1", decision_id=decision_id, attempt_id="job-1",
        assessment=assessment, observation_version="obs-v1", demand_version="demand-v1",
        lease_ms=60_000, now_ms=_NOW + 1,
    )
    assert first.status == "granted"
    assert second.status == "duplicate"
    assert second.reservation_id == first.reservation_id
    committed = authority.committed(now_ms=_NOW + 2)
    key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    assert committed[key] == "1"  # 不是 "2"——沒有因為重送而重複扣款


def test_ac1_new_attempt_after_safe_boundary_fallback_gets_a_fresh_decision() -> None:
    # 安全 attempt 邊界 fallback：換一個新的 attempt_id 才會算出不同的
    # decision_id，不會跟舊 attempt 的 reservation 混在一起、也不會延用舊的
    # owner_token 搬活舊 session。
    first = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A, mode="enforced")
    second = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-2", profile_key=_PROFILE_A, mode="enforced")
    assert first != second


# ---------------------------------------------------------------------------
# AC6：reconcile 掃描——不假造 job_id、不誤放 bound、restart 不確定 liveness 不洗預算
# ---------------------------------------------------------------------------


def _admit_and_bind(authority, store, *, attempt_id: str, job_id: str, descriptor) -> admission.AdmissionDecision:
    assessment = _feasible_assessment(descriptor)
    decision_id = admission.decision_id_for(
        run_id="run-1", card_id="card-1", attempt_id=attempt_id, profile_key=_PROFILE_A,
        mode="enforced",
    )
    reserved = admission.reserve_for_candidate(
        authority, run_id="run-1", card_id="card-1", decision_id=decision_id, attempt_id=attempt_id,
        assessment=assessment, observation_version="obs-v1", demand_version="demand-v1",
        lease_ms=60_000, now_ms=_NOW,
    )
    assert reserved.status == "granted"
    bound = authority.bind(
        reservation_id=reserved.reservation_id, owner_token=reserved.owner_token,
        attempt_id=attempt_id, job_id=job_id, expected_sequence=reserved.sequence, now_ms=_NOW,
    )
    assert bound.status == "ok"
    decision = admission.AdmissionDecision(
        decision_id=decision_id, run_id="run-1", card_id="card-1", attempt_id=attempt_id,
        profile_key=_PROFILE_A, mode="enforced", outcome="admit",
        policy_version=admission.ADMISSION_POLICY_VERSION, observation_version="obs-v1",
        demand_version="demand-v1", qualification_version="not-enforced", generated_at_ms=_NOW,
        selected={"executor": "claude", "model_id": "sonnet"}, reservation_id=reserved.reservation_id,
        job_id=job_id,
    )
    store.record(decision)
    return decision


def test_ac6_reconcile_keeps_uncertain_reservation_when_job_lookup_misses(tmp_path: Path) -> None:
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_and_bind(authority, store, attempt_id="job-1", job_id="job-1", descriptor=descriptor)

    outcomes = admission.reconcile_bound_reservations(
        authority=authority, store=store,
        job_lookup=lambda job_id: None,  # restart 後查不到——不能因此假設已終止
        job_outcome=lambda job: None,
        now_ms=_NOW + 1,
    )
    assert len(outcomes) == 1
    assert outcomes[0].decision_id == decision.decision_id
    assert outcomes[0].action == "reconciled"
    status = authority.status(decision.reservation_id, now_ms=_NOW + 1)
    assert status.state == "bound"  # 容量仍然保留，沒有被 TTL／查不到就釋放


def test_ac6_reconcile_settles_when_job_registry_confirms_terminal(tmp_path: Path) -> None:
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_and_bind(authority, store, attempt_id="job-1", job_id="job-1", descriptor=descriptor)

    outcomes = admission.reconcile_bound_reservations(
        authority=authority, store=store,
        job_lookup=lambda job_id: {"job_id": job_id, "status": "exited", "exit_code": 1},
        job_outcome=lambda job: "failed" if job.get("exit_code") else "succeeded",
        now_ms=_NOW + 1,
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == "settled"
    status = authority.status(decision.reservation_id, now_ms=_NOW + 1)
    assert status.state == "released"
    committed = authority.committed(now_ms=_NOW + 2)
    key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    assert committed.get(key, "0") == "0"


def test_ac6_reconcile_keeps_alive_job_bound_and_renews_lease(tmp_path: Path) -> None:
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_and_bind(authority, store, attempt_id="job-1", job_id="job-1", descriptor=descriptor)

    outcomes = admission.reconcile_bound_reservations(
        authority=authority, store=store,
        job_lookup=lambda job_id: {"job_id": job_id, "status": "running"},
        job_outcome=lambda job: None,
        now_ms=_NOW + 1, renew_lease_ms=120_000,
    )
    assert outcomes[0].action == "reconciled"
    assert "confirmed-alive" in outcomes[0].detail
    status = authority.status(decision.reservation_id, now_ms=_NOW + 1)
    assert status.state == "bound"
    assert status.lease_expires_at_ms == _NOW + 1 + 120_000


# ---------------------------------------------------------------------------
# 對抗審查 MAJOR（manager.py:13981）：reconcile_reserved_reservations —— 收斂
# create_job() 耐久寫入後、bind() 前 crash 留下的無 job_id reserved reservation。
# ---------------------------------------------------------------------------


def _admit_reserve_only(authority, store, *, attempt_id: str, descriptor) -> admission.AdmissionDecision:
    """比照 `_admit_and_bind`，但刻意不呼叫 `bind()`——模擬 `create_job()`
    耐久寫入後、`bind()` 之前 crash 留下的『reserved 但無 job_id』狀態。"""
    assessment = _feasible_assessment(descriptor)
    decision_id = admission.decision_id_for(
        run_id="run-1", card_id="card-1", attempt_id=attempt_id, profile_key=_PROFILE_A,
        mode="enforced",
    )
    reserved = admission.reserve_for_candidate(
        authority, run_id="run-1", card_id="card-1", decision_id=decision_id, attempt_id=attempt_id,
        assessment=assessment, observation_version="obs-v1", demand_version="demand-v1",
        lease_ms=60_000, now_ms=_NOW,
    )
    assert reserved.status == "granted"
    decision = admission.AdmissionDecision(
        decision_id=decision_id, run_id="run-1", card_id="card-1", attempt_id=attempt_id,
        profile_key=_PROFILE_A, mode="enforced", outcome="admit",
        policy_version=admission.ADMISSION_POLICY_VERSION, observation_version="obs-v1",
        demand_version="demand-v1", qualification_version="not-enforced", generated_at_ms=_NOW,
        selected={"executor": "claude", "model_id": "sonnet"}, reservation_id=reserved.reservation_id,
    )
    store.record(decision)
    return decision


def test_reserved_unbound_lease_not_expired_is_left_untouched(tmp_path: Path) -> None:
    """create_job() 之後、bind() 之前 crash——lease 未到期時可能只是
    create_job() 仍在進行中，不得因此釋放容量。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_reserve_only(authority, store, attempt_id="job-1", descriptor=descriptor)

    outcomes = admission.reconcile_reserved_reservations(
        authority=authority,
        job_lookup_by_decision=lambda run_id, card_id, decision_id: None,
        job_outcome=lambda job: None,
        now_ms=_NOW + 1,
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == "skipped"
    status = authority.status(decision.reservation_id, now_ms=_NOW + 1)
    assert status.state == "reserved"


def test_reserved_unbound_lease_expired_and_no_job_found_is_released(tmp_path: Path) -> None:
    """lease 已過期且 registry 查無對應 job（真的沒建成）——安全釋放容量，
    evidence 標記 recovered-unbound。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_reserve_only(authority, store, attempt_id="job-1", descriptor=descriptor)

    outcomes = admission.reconcile_reserved_reservations(
        authority=authority,
        job_lookup_by_decision=lambda run_id, card_id, decision_id: None,
        job_outcome=lambda job: None,
        now_ms=_NOW + 120_000,  # lease_ms=60_000 早已過期
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == "settled"
    assert "recovered-unbound" in outcomes[0].detail
    status = authority.status(decision.reservation_id, now_ms=_NOW + 120_000)
    assert status.state == "released"
    committed = authority.committed(now_ms=_NOW + 120_000)
    key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    assert committed.get(key, "0") == "0"


def test_reserved_unbound_job_found_alive_renews_lease_and_keeps_capacity(tmp_path: Path) -> None:
    """create_job() 成功建立、crash 發生在 bind() 之前——registry 找得到那筆
    job（依 attempt 反查），job 本身從未真正 spawn（非終局）：續 lease、
    維持 reserved，不釋放容量。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_reserve_only(authority, store, attempt_id="job-1", descriptor=descriptor)

    outcomes = admission.reconcile_reserved_reservations(
        authority=authority,
        job_lookup_by_decision=lambda run_id, card_id, decision_id: {"job_id": "j-1", "status": "dispatched"},
        job_outcome=lambda job: None,
        now_ms=_NOW + 120_000, renew_lease_ms=60_000,
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == "reconciled"
    assert "confirmed-alive" in outcomes[0].detail
    status = authority.status(decision.reservation_id, now_ms=_NOW + 120_000)
    assert status.state == "reserved"
    assert status.lease_expires_at_ms == _NOW + 120_000 + 60_000


def test_reserved_unbound_lookup_failure_is_left_untouched_not_released(tmp_path: Path) -> None:
    """job_lookup_by_decision 本身查詢失敗（拋例外）——一律不動，不得誤判成
    『查無此 job』而釋放容量（即使 lease 已過期）。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_reserve_only(authority, store, attempt_id="job-1", descriptor=descriptor)

    def _boom(run_id, card_id, decision_id):
        raise RuntimeError("registry temporarily unavailable")

    outcomes = admission.reconcile_reserved_reservations(
        authority=authority,
        job_lookup_by_decision=_boom,
        job_outcome=lambda job: None,
        now_ms=_NOW + 120_000,
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == "skipped"
    assert outcomes[0].detail == "job-lookup-failed"
    status = authority.status(decision.reservation_id, now_ms=_NOW + 120_000)
    assert status.state == "reserved"


def test_reserved_unbound_job_found_terminal_is_released_defensively(tmp_path: Path) -> None:
    """防禦性分支：協定上 reserved 不該有終局 job（spawn 一定在 bind 之後），
    但若 registry 事實真的顯示終局，仍安全收斂釋放容量而不假設。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_reserve_only(authority, store, attempt_id="job-1", descriptor=descriptor)

    outcomes = admission.reconcile_reserved_reservations(
        authority=authority,
        job_lookup_by_decision=lambda run_id, card_id, decision_id: {"job_id": "j-1", "status": "exited", "exit_code": 0},
        job_outcome=lambda job: "succeeded",
        now_ms=_NOW + 1,
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == "settled"
    status = authority.status(decision.reservation_id, now_ms=_NOW + 1)
    assert status.state == "released"


def _reserve_only_never_write_receipt(authority, *, attempt_id: str, descriptor) -> str:
    """模擬對抗審查第三輪 MAJOR（quota_admission.py:1008）的觸發情境：合法
    持有者 `reserve_for_candidate()` 成功之後、寫入 admit receipt 之前
    crash（或 receipt 寫入本身失敗）——**完全不呼叫** `store.record()`，這筆
    reservation 因此對任何『依賴 decision store 反查』的收斂路徑徹底隱形。
    回傳這筆 reservation 的 id。"""
    assessment = _feasible_assessment(descriptor)
    decision_id = admission.decision_id_for(
        run_id="run-1", card_id="card-1", attempt_id=attempt_id, profile_key=_PROFILE_A,
        mode="enforced",
    )
    reserved = admission.reserve_for_candidate(
        authority, run_id="run-1", card_id="card-1", decision_id=decision_id, attempt_id=attempt_id,
        assessment=assessment, observation_version="obs-v1", demand_version="demand-v1",
        lease_ms=60_000, now_ms=_NOW,
    )
    assert reserved.status == "granted"
    return reserved.reservation_id


def test_reserved_reservation_without_any_decision_receipt_is_still_reconciled(tmp_path: Path) -> None:
    """核心情境（對抗審查第三輪 MAJOR quota_admission.py:1008）：這筆
    reservation 從未被任何 `AdmissionDecisionStore` 記錄過——舊實作的收斂
    掃描以 `store.enforced_admitted()` 反查候選，對這筆完全看不到，容量永久
    卡住、之後同 attempt 重送永遠撞 `held-elsewhere`。新實作改以
    `authority.list_by_state("reserved", ...)` 為出發點，不依賴任何下游
    receipt，一樣能在 lease 過期後正確收斂釋放容量。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    reservation_id = _reserve_only_never_write_receipt(authority, attempt_id="job-1", descriptor=descriptor)

    outcomes = admission.reconcile_reserved_reservations(
        authority=authority,
        job_lookup_by_decision=lambda run_id, card_id, decision_id: None,
        job_outcome=lambda job: None,
        now_ms=_NOW + 120_000,  # lease_ms=60_000 早已過期
    )
    assert len(outcomes) == 1
    assert outcomes[0].reservation_id == reservation_id
    assert outcomes[0].action == "settled"
    assert "recovered-unbound" in outcomes[0].detail
    status = authority.status(reservation_id, now_ms=_NOW + 120_000)
    assert status.state == "released"
    committed = authority.committed(now_ms=_NOW + 120_000)
    key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    assert committed.get(key, "0") == "0"


# ---------------------------------------------------------------------------
# 對抗審查第四輪 MAJOR（quota_admission.py:1119）：reconcile_reserved_reservations
# 新增 grace_ms 寬限與 in_flight 排除——provisioning 正常地比 lease 長時，
# sweep 不得因為單純的 lease 過期就把仍在使用中的 reservation 誤判成 crash
# 殘留並釋放。
# ---------------------------------------------------------------------------


def test_reserved_unbound_lease_expired_but_within_grace_is_left_untouched(tmp_path: Path) -> None:
    """lease 剛過期、但仍在 grace_ms 寬限窗口內——不得釋放（即使查無對應
    job）；這是 dispatch 側續租之外的第二道安全邊界。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_reserve_only(authority, store, attempt_id="job-1", descriptor=descriptor)

    # lease_ms=60_000；now = _NOW + 90_000 已過期 30 秒，但 grace_ms=60_000
    # 還在寬限內（過期時刻 + grace = _NOW + 120_000 > now）。
    outcomes = admission.reconcile_reserved_reservations(
        authority=authority,
        job_lookup_by_decision=lambda run_id, card_id, decision_id: None,
        job_outcome=lambda job: None,
        now_ms=_NOW + 90_000,
        grace_ms=60_000,
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == "skipped"
    assert outcomes[0].detail == "lease-not-expired"
    status = authority.status(decision.reservation_id, now_ms=_NOW + 90_000)
    assert status.state == "reserved"


def test_reserved_unbound_lease_expired_beyond_grace_is_released(tmp_path: Path) -> None:
    """超過 lease + grace_ms 才真正視為過期，維持既有『安全釋放』行為——
    grace_ms 只延後判定時機，不改變最終會釋放的事實。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_reserve_only(authority, store, attempt_id="job-1", descriptor=descriptor)

    outcomes = admission.reconcile_reserved_reservations(
        authority=authority,
        job_lookup_by_decision=lambda run_id, card_id, decision_id: None,
        job_outcome=lambda job: None,
        now_ms=_NOW + 60_000 + 60_000 + 1,  # 剛好超過 lease(60s) + grace(60s)
        grace_ms=60_000,
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == "settled"
    assert "recovered-unbound" in outcomes[0].detail
    status = authority.status(decision.reservation_id, now_ms=_NOW + 120_001)
    assert status.state == "released"


def test_reserved_unbound_in_flight_dispatch_renews_even_after_lease_expired(tmp_path: Path) -> None:
    """本 process 標記為 in-flight 的 reservation——即使 lease（含 grace）
    早已過期，也不得釋放。

    對抗審查第五輪 MAJOR：這個檢查排在 job 反查之前，且一律直接 `skipped`
    ——不對這筆 reservation 呼叫 `reconcile()`／`renew()`（不寫入任何事件、
    不推進 sequence）。原因：dispatch 側（manager.py）持有的
    `quota_reservation_handle["sequence"]` 是它自己續租後記住的值；sweep
    若在這個窗口內對同一筆 reservation 寫入任何事件（即使只是續租
    lease），都會讓 dispatch 手上的 sequence 過期，稍後的 `bind()` 就會
    因 `expected_sequence` 不符而失敗（見 #839 第五輪 MAJOR
    manager.py:14511 的重試修法）。sweep 什麼都不做，正確性交給 dispatch
    自己的續租呼叫；lease 是否過期因此保持原樣，`reserved` 狀態與容量都
    不變。"""
    from paulsha_cortex.coordinator.quota_admission import InFlightDispatchTracker

    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_reserve_only(authority, store, attempt_id="job-1", descriptor=descriptor)
    original_status = authority.status(decision.reservation_id, now_ms=_NOW)

    tracker = InFlightDispatchTracker()
    tracker.mark_started(decision.reservation_id)

    def _job_lookup_must_not_be_called(run_id, card_id, decision_id):
        raise AssertionError("in-flight reservation 必須在 job 反查之前就被跳過")

    outcomes = admission.reconcile_reserved_reservations(
        authority=authority,
        job_lookup_by_decision=_job_lookup_must_not_be_called,
        job_outcome=lambda job: None,
        now_ms=_NOW + 10_000_000,  # 遠遠超過 lease + grace
        grace_ms=0,
        in_flight=tracker,
        renew_lease_ms=300_000,
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == "skipped"
    assert "in-flight" in outcomes[0].detail
    status = authority.status(decision.reservation_id, now_ms=_NOW + 10_000_000)
    assert status.state == "reserved"
    # sweep 完全沒有寫入任何事件——sequence／lease 都維持 reserve() 當下的原值。
    assert status.sequence == original_status.sequence
    assert status.lease_expires_at_ms == original_status.lease_expires_at_ms
    committed = authority.committed(now_ms=_NOW + 10_000_000)
    key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    assert committed.get(key, "0") == "1"


def test_reserved_unbound_not_in_flight_of_this_process_is_released_normally(tmp_path: Path) -> None:
    """`in_flight` 給定但這筆 reservation 不在集合裡（別的 reservation 才是
    in-flight，或這個 process 從未標記過任何東西）——照既有 lease＋grace
    規則正常釋放，`in_flight` 存在本身不會讓其他不相關的 reservation 也豁免。"""
    from paulsha_cortex.coordinator.quota_admission import InFlightDispatchTracker

    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_reserve_only(authority, store, attempt_id="job-1", descriptor=descriptor)

    tracker = InFlightDispatchTracker()
    tracker.mark_started("some-other-reservation-id")

    outcomes = admission.reconcile_reserved_reservations(
        authority=authority,
        job_lookup_by_decision=lambda run_id, card_id, decision_id: None,
        job_outcome=lambda job: None,
        now_ms=_NOW + 120_000,
        grace_ms=0,
        in_flight=tracker,
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == "settled"
    assert "recovered-unbound" in outcomes[0].detail
    status = authority.status(decision.reservation_id, now_ms=_NOW + 120_000)
    assert status.state == "released"


def test_in_flight_dispatch_tracker_mark_finished_removes_entry() -> None:
    from paulsha_cortex.coordinator.quota_admission import InFlightDispatchTracker

    tracker = InFlightDispatchTracker()
    tracker.mark_started("resv-1")
    assert tracker.is_in_flight("resv-1") is True
    tracker.mark_finished("resv-1")
    assert tracker.is_in_flight("resv-1") is False
    # mark_finished 對從未標記過的 id 是 no-op，不拋例外。
    tracker.mark_finished("resv-never-started")


# ---------------------------------------------------------------------------
# 對抗審查第四輪 MAJOR（quota_admission.py:1119）：lease_ms 下限——太短的
# lease 讓 provisioning 正常耗時就足以撞上過期，拒絕載入設定。
# ---------------------------------------------------------------------------


def _valid_quota_pools_config_payload(*, lease_ms: int = 900_000) -> dict:
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    return {
        "schema": admission.QUOTA_POOLS_CONFIG_SCHEMA,
        "config_revision": "test-1",
        "descriptors": [descriptor.to_dict()],
        "unit_catalog": [],
        "bindings": [binding.to_dict()],
        "lease_ms": lease_ms,
    }


def test_quota_pools_config_rejects_lease_ms_below_floor() -> None:
    payload = _valid_quota_pools_config_payload(lease_ms=59_999)
    with pytest.raises(admission.QuotaPoolsConfigError):
        admission.parse_quota_pools_config(payload)


def test_quota_pools_config_accepts_lease_ms_at_floor() -> None:
    payload = _valid_quota_pools_config_payload(lease_ms=60_000)
    config = admission.parse_quota_pools_config(payload)
    assert config.lease_ms == 60_000


# ---------------------------------------------------------------------------
# 對抗審查第四輪 MAJOR（manager.py:11491／quota_admission.py 的
# reconcile_bound_reservations）：bound 收斂改以 authority 為真相，不再依賴
# store.enforced_admitted() 反查。
# ---------------------------------------------------------------------------


def _reserve_bind_never_write_receipt(authority, *, attempt_id: str, job_id: str, descriptor) -> str:
    """模擬「這筆 reservation 真的 bind() 過，但 AdmissionDecisionStore 完全
    沒有任何一筆 receipt 記著它」的現場——不管是因為同一個 decision_id 先被
    別的 mode 佔用（見 `decision_id_for` 的 mode 隔離修法之前的舊行為）、
    receipt 寫入本身失敗，還是任何其他原因，這支 helper 都**完全不呼叫**
    `store.record()`，只留下 authority 裡真正的狀態。回傳 reservation_id。"""
    assessment = _feasible_assessment(descriptor)
    decision_id = admission.decision_id_for(
        run_id="run-1", card_id="card-1", attempt_id=attempt_id, profile_key=_PROFILE_A,
        mode="enforced",
    )
    reserved = admission.reserve_for_candidate(
        authority, run_id="run-1", card_id="card-1", decision_id=decision_id, attempt_id=attempt_id,
        assessment=assessment, observation_version="obs-v1", demand_version="demand-v1",
        lease_ms=60_000, now_ms=_NOW,
    )
    assert reserved.status == "granted"
    bound = authority.bind(
        reservation_id=reserved.reservation_id, owner_token=reserved.owner_token,
        attempt_id=attempt_id, job_id=job_id, expected_sequence=reserved.sequence, now_ms=_NOW,
    )
    assert bound.status == "ok"
    return reserved.reservation_id


def test_bound_reservation_without_any_decision_receipt_is_still_reconciled(tmp_path: Path) -> None:
    """核心情境（對抗審查第四輪 MAJOR）：這筆 bound reservation 從未被任何
    `AdmissionDecisionStore` 記錄過——舊實作以 `store.enforced_admitted()`
    反查候選，對這筆完全看不到，永遠不會被 settle／reconcile，容量永久卡
    死；job 終局的 usage 也永遠補記不到。新實作改以
    `authority.list_by_state("bound", ...)` 為出發點，不依賴任何下游
    receipt 是否成功寫入。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")  # 空的，從未寫入任何 receipt
    reservation_id = _reserve_bind_never_write_receipt(
        authority, attempt_id="job-1", job_id="job-1", descriptor=descriptor,
    )

    outcomes = admission.reconcile_bound_reservations(
        authority=authority, store=store,
        job_lookup=lambda job_id: {"job_id": job_id, "status": "exited", "exit_code": 0},
        job_outcome=lambda job: "succeeded",
        now_ms=_NOW + 1,
    )
    assert len(outcomes) == 1
    assert outcomes[0].reservation_id == reservation_id
    assert outcomes[0].action == "settled"
    status = authority.status(reservation_id, now_ms=_NOW + 1)
    assert status.state == "released"
    committed = authority.committed(now_ms=_NOW + 1)
    key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    assert committed.get(key, "0") == "0"


def test_on_settled_receives_none_decision_when_receipt_missing_and_usage_recording_is_skipped(
    tmp_path: Path,
) -> None:
    """`on_settled` 拿不到對應 receipt 時收到 `None`（不是拋例外），呼叫端
    （`manager._quota_admission_record_terminal_usage` 的包裝 lambda）據此
    安全略過 usage 記錄——收斂本身（容量釋放）完全不受影響。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    reservation_id = _reserve_bind_never_write_receipt(
        authority, attempt_id="job-1", job_id="job-1", descriptor=descriptor,
    )

    seen: list[object] = []
    outcomes = admission.reconcile_bound_reservations(
        authority=authority, store=store,
        job_lookup=lambda job_id: {"job_id": job_id, "status": "exited", "exit_code": 0},
        job_outcome=lambda job: "succeeded",
        now_ms=_NOW + 1,
        on_settled=lambda decision, job: seen.append(decision),
    )
    assert len(outcomes) == 1
    assert seen == [None]
    status = authority.status(reservation_id, now_ms=_NOW + 1)
    assert status.state == "released"


def test_bound_reservation_with_receipt_present_still_passes_decision_to_on_settled(tmp_path: Path) -> None:
    """既有行為的回歸保護：receipt 確實存在時，`on_settled` 仍收到完整的
    `AdmissionDecision`（不是退化成 `None`）——authority-based 出發點不代表
    丟棄可用的 receipt 資訊。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_and_bind(authority, store, attempt_id="job-1", job_id="job-1", descriptor=descriptor)

    seen: list[object] = []
    admission.reconcile_bound_reservations(
        authority=authority, store=store,
        job_lookup=lambda job_id: {"job_id": job_id, "status": "exited", "exit_code": 0},
        job_outcome=lambda job: "succeeded",
        now_ms=_NOW + 1,
        on_settled=lambda decision, job: seen.append(decision),
    )
    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0].decision_id == decision.decision_id
    assert seen[0].profile_key == _PROFILE_A


# ---------------------------------------------------------------------------
# AdmissionDecisionStore：append-only、冪等重放、內容衝突 fail closed
# ---------------------------------------------------------------------------


def _shadow_decision(decision_id: str, *, outcome: str = "wait", reason: str | None = "quota-admission-insufficient") -> admission.AdmissionDecision:
    return admission.AdmissionDecision(
        decision_id=decision_id, run_id="run-1", card_id="card-1", attempt_id="job-1",
        profile_key=_PROFILE_A, mode="shadow", outcome=outcome,
        policy_version=admission.ADMISSION_POLICY_VERSION, observation_version="obs-v1",
        demand_version="demand-v1", qualification_version="not-enforced", generated_at_ms=_NOW,
        selected=None if outcome == "wait" else {"executor": "claude", "model_id": "sonnet"},
        reason=reason if outcome == "wait" else None,
    )


def test_decision_store_replay_is_idempotent_conflict_is_fail_closed(tmp_path: Path) -> None:
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision_id = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A, mode="shadow")
    decision = _shadow_decision(decision_id)
    first = store.record(decision)
    replay = store.record(decision)
    assert first.decision_id == replay.decision_id == decision_id
    conflicting = _shadow_decision(decision_id, reason="different-reason")
    with pytest.raises(admission.AdmissionDecisionCorrupt):
        store.record(conflicting)


def test_decision_requires_selected_on_admit_and_reason_on_wait() -> None:
    with pytest.raises(ValueError):
        admission.AdmissionDecision(
            decision_id="adm:v1:x", run_id="run-1", card_id="card-1", attempt_id="job-1",
            profile_key=_PROFILE_A, mode="shadow", outcome="admit",
            policy_version=admission.ADMISSION_POLICY_VERSION, observation_version="obs-v1",
            demand_version="demand-v1", qualification_version="not-enforced", generated_at_ms=_NOW,
            selected=None,
        )
    with pytest.raises(ValueError):
        admission.AdmissionDecision(
            decision_id="adm:v1:y", run_id="run-1", card_id="card-1", attempt_id="job-1",
            profile_key=_PROFILE_A, mode="shadow", outcome="wait",
            policy_version=admission.ADMISSION_POLICY_VERSION, observation_version="obs-v1",
            demand_version="demand-v1", qualification_version="not-enforced", generated_at_ms=_NOW,
            reason=None,
        )


# ---------------------------------------------------------------------------
# 對抗審查 MAJOR（quota_admission.py:658）：AdmissionDecisionStore.record()
# 每次 append 都要 fsync 父目錄與其上層——比照 #838 quota_reservation 的
# _open_for_append，避免第一筆 grant 的 dirent 在崩潰後消失、重啟看到空
# store 而重放出第二份不同內容的 decision。
# ---------------------------------------------------------------------------


def test_record_fsyncs_new_store_directory_and_parent(tmp_path: Path, monkeypatch) -> None:
    import paulsha_cortex.coordinator.quota_admission as module

    synced: list[str] = []
    real = module._fsync_directory
    monkeypatch.setattr(module, "_fsync_directory", lambda path: (synced.append(str(path)), real(path))[1])
    path = tmp_path / "quota-admission-decisions" / "decisions.jsonl"
    store = admission.AdmissionDecisionStore(path)
    decision_id = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A, mode="shadow")
    store.record(_shadow_decision(decision_id))
    assert str(tmp_path) in synced
    assert str(tmp_path / "quota-admission-decisions") in synced


def test_record_fsyncs_directories_on_every_append_not_only_first(tmp_path: Path, monkeypatch) -> None:
    """前一個呼叫者可能在 O_CREAT 後、目錄 fsync 前崩潰；後續 record() 看到
    檔案已存在時仍必須 fsync 目錄，不得只在檔案不存在時才補（對抗審查要求
    「每次 append 都 fsync」，不是「首次建立才 fsync」）。"""
    import paulsha_cortex.coordinator.quota_admission as module

    path = tmp_path / "decisions.jsonl"
    store = admission.AdmissionDecisionStore(path)
    first_id = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A, mode="shadow")
    store.record(_shadow_decision(first_id))
    assert path.exists()

    synced: list[str] = []
    real = module._fsync_directory
    monkeypatch.setattr(module, "_fsync_directory", lambda p: (synced.append(str(p)), real(p))[1])
    second_id = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-2", profile_key=_PROFILE_A, mode="shadow")
    store.record(_shadow_decision(second_id))
    assert str(tmp_path) in synced


# ---------------------------------------------------------------------------
# #840 對抗審查修復 item 1／2（quota_admission.py 約 667／762）：
# `AdmissionDecision.selected_observation_state`／`selected_feasible`／
# `policy_config_revision` 是 #839 receipt 上新增的三個選填欄位，與 #840 同一
# 個 release 一起發布（#839-only、從未發布的中間版本讀不懂三個新 key 不構成
# schema bump 理由）。讀取端必須容忍舊 row 缺這三個 key；`record()` 的
# 冪等重放比對也必須是「正規化後比較」，不能因為 raw dict 的 key 集合不同
# 就把語意等價的重放誤判成衝突。
# ---------------------------------------------------------------------------


def test_store_get_tolerates_legacy_row_missing_840_optional_keys(tmp_path: Path) -> None:
    """讀取端（#840 之前寫的 #839-only row）必須容忍缺
    `selected_observation_state`／`selected_feasible`／`policy_config_revision`
    三個 #840 新增選填欄位，不得判成 `admission-decision-store-invalid-record`。"""
    path = tmp_path / "decisions.jsonl"
    decision_id = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A, mode="shadow")
    legacy_row = {
        "schema_version": 1, "decision_id": decision_id, "run_id": "run-1", "card_id": "card-1",
        "attempt_id": "job-1", "profile_key": _PROFILE_A, "mode": "shadow", "outcome": "wait",
        "policy_version": admission.ADMISSION_POLICY_VERSION, "observation_version": "obs-v1",
        "demand_version": "demand-v1", "qualification_version": "not-enforced",
        "generated_at_ms": _NOW, "selected": None, "reservation_id": None, "excluded": [],
        "reason": "quota-admission-insufficient", "job_id": None,
        # 刻意不含三個 #840 選填欄位——模擬 #839-only（#840 之前）寫入的舊 row。
    }
    path.write_text(json.dumps(legacy_row, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    store = admission.AdmissionDecisionStore(path)

    decision = store.get(decision_id)
    assert decision is not None
    assert decision.selected_observation_state is None
    assert decision.selected_feasible is None
    assert decision.policy_config_revision is None


def test_record_replay_of_legacy_row_missing_840_optional_keys_is_idempotent_not_conflict(
    tmp_path: Path,
) -> None:
    """既有紀錄缺 #840 三個選填欄位時，重送同一個 decision_id（三欄位在新
    decision 上同樣是 None，語意等價於『缺席』）必須被視為冪等重放，不是
    `admission-decision-id-conflict`；真的矛盾內容仍必須 fail closed。"""
    path = tmp_path / "decisions.jsonl"
    decision_id = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A, mode="shadow")
    legacy_row = {
        "schema_version": 1, "decision_id": decision_id, "run_id": "run-1", "card_id": "card-1",
        "attempt_id": "job-1", "profile_key": _PROFILE_A, "mode": "shadow", "outcome": "wait",
        "policy_version": admission.ADMISSION_POLICY_VERSION, "observation_version": "obs-v1",
        "demand_version": "demand-v1", "qualification_version": "not-enforced",
        "generated_at_ms": _NOW, "selected": None, "reservation_id": None, "excluded": [],
        "reason": "quota-admission-insufficient", "job_id": None,
    }
    path.write_text(json.dumps(legacy_row, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    store = admission.AdmissionDecisionStore(path)

    replay_decision = admission.AdmissionDecision(
        decision_id=decision_id, run_id="run-1", card_id="card-1", attempt_id="job-1",
        profile_key=_PROFILE_A, mode="shadow", outcome="wait",
        policy_version=admission.ADMISSION_POLICY_VERSION, observation_version="obs-v1",
        demand_version="demand-v1", qualification_version="not-enforced", generated_at_ms=_NOW,
        reason="quota-admission-insufficient",
        selected_observation_state=None, selected_feasible=None, policy_config_revision=None,
    )
    replayed = store.record(replay_decision)
    assert replayed.decision_id == decision_id
    assert replayed.reason == "quota-admission-insufficient"

    conflicting = admission.AdmissionDecision(
        decision_id=decision_id, run_id="run-1", card_id="card-1", attempt_id="job-1",
        profile_key=_PROFILE_A, mode="shadow", outcome="wait",
        policy_version=admission.ADMISSION_POLICY_VERSION, observation_version="obs-v1",
        demand_version="demand-v1", qualification_version="not-enforced", generated_at_ms=_NOW,
        reason="quota-config-invalid",
    )
    with pytest.raises(admission.AdmissionDecisionCorrupt):
        store.record(conflicting)
