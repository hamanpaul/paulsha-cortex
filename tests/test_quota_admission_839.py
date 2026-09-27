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
    first = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A)
    second = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-2", profile_key=_PROFILE_A)
    assert first != second


# ---------------------------------------------------------------------------
# AC6：reconcile 掃描——不假造 job_id、不誤放 bound、restart 不確定 liveness 不洗預算
# ---------------------------------------------------------------------------


def _admit_and_bind(authority, store, *, attempt_id: str, job_id: str, descriptor) -> admission.AdmissionDecision:
    assessment = _feasible_assessment(descriptor)
    decision_id = admission.decision_id_for(
        run_id="run-1", card_id="card-1", attempt_id=attempt_id, profile_key=_PROFILE_A,
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
        authority=authority, store=store,
        job_lookup_by_attempt=lambda run_id, card_id, attempt_id: None,
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
        authority=authority, store=store,
        job_lookup_by_attempt=lambda run_id, card_id, attempt_id: None,
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
        authority=authority, store=store,
        job_lookup_by_attempt=lambda run_id, card_id, attempt_id: {"job_id": "j-1", "status": "dispatched"},
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
    """job_lookup_by_attempt 本身查詢失敗（拋例外）——一律不動，不得誤判成
    『查無此 job』而釋放容量（即使 lease 已過期）。"""
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _admit_reserve_only(authority, store, attempt_id="job-1", descriptor=descriptor)

    def _boom(run_id, card_id, attempt_id):
        raise RuntimeError("registry temporarily unavailable")

    outcomes = admission.reconcile_reserved_reservations(
        authority=authority, store=store,
        job_lookup_by_attempt=_boom,
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
        authority=authority, store=store,
        job_lookup_by_attempt=lambda run_id, card_id, attempt_id: {"job_id": "j-1", "status": "exited", "exit_code": 0},
        job_outcome=lambda job: "succeeded",
        now_ms=_NOW + 1,
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == "settled"
    status = authority.status(decision.reservation_id, now_ms=_NOW + 1)
    assert status.state == "released"


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
    decision_id = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A)
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
    decision_id = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A)
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
    first_id = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-1", profile_key=_PROFILE_A)
    store.record(_shadow_decision(first_id))
    assert path.exists()

    synced: list[str] = []
    real = module._fsync_directory
    monkeypatch.setattr(module, "_fsync_directory", lambda p: (synced.append(str(p)), real(p))[1])
    second_id = admission.decision_id_for(run_id="run-1", card_id="card-1", attempt_id="job-2", profile_key=_PROFILE_A)
    store.record(_shadow_decision(second_id))
    assert str(tmp_path) in synced
