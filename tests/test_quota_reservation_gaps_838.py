"""#838 補缺：crash matrix 的 after-append 與重啟不重派、window reset 與
reservation×ledger 交叉扣減、跨 host／跨 principal coverage gap 的機讀輸出、
死開關移除。

- G838-1（AC3）：事件已 append、呼叫端還沒拿到結果就 crash（after-append
  failpoint）時，重啟後以同參數重送一律冪等（duplicate）或被拒（conflict），
  絕不套用第二次；Manager 重啟後 periodic resume 遇到仍在跑的 bound job 不
  重派、不再 reserve。
- G838-2（AC5）：window reset（新 snapshot、新 capacity）不清有效
  reservation；job 終局後 reservation 釋放、usage 進 ledger，兩者不同時計；
  跑到一半的 snapshot 已反映部分消耗時保守（straddling → unknown），不做
  第二次扣減；舊 window 的 job 在 reset 後才 settle 不扣新 window。
- G838-3：`cortex quota reservations --json` 機讀輸出 authority 路徑、狀態
  計數、committed 與 coverage gap；`PSC_QUOTA_RESERVATION_ENFORCE` 死開關已
  移除，唯一 enforce 開關是 `PSC_QUOTA_ADMISSION_ENFORCE`。
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import time

import pytest

from paulsha_cortex.coordinator import manager, quota_admission, quota_reservation
from paulsha_cortex.coordinator.quota_reservation import PoolDemand, QuotaReservationAuthority
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.porcelain import quota as quota_cli

from test_quota_admission_dispatch_wiring_839 import _FakeWorktreeCreator, _Launcher
from test_quota_terminal_usage_harvest_836 import (
    _PROFILE_WORKER,
    _ctx,
    _descriptor,
    _dispatch_fixture,
    _finish_job_with_codex_usage,
    _iso,
    _profile_binding,
    _row,
    _snapshot,
    _unit,
    _usage_rows,
)


NOW = 1_700_000_000_000
_POOL = {"authority_id": "authority-a", "account_id": "acct-1", "pool_id": "pool-x", "revision": "r1"}
_KEY = (("authority-a", "acct-1", "pool-x", "r1"), "week")
_ENFORCE = {"PSC_QUOTA_ADMISSION_ENFORCE": "on"}


def _reserve(authority, *, attempt="attempt-1", decision="decision-1", amount="1", capacity="5"):
    return authority.reserve(
        run_id="run-1", card_id="card-1", decision_id=decision, attempt_id=attempt,
        pools=(PoolDemand(pool_ref=_POOL, window_id="week", amount=amount),),
        capacity_by_pool={_KEY: capacity}, observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


class _Crash(RuntimeError):
    pass


def _crash_at(stage: str):
    def failpoint(current: str) -> None:
        if current == stage:
            raise _Crash(f"simulated crash at {current}")
    return failpoint


# ---------------------------------------------------------------------------
# G838-1（AC3）：after-append crash matrix
# ---------------------------------------------------------------------------


def test_ac3_reserve_after_append_crash_replay_returns_the_same_grant(tmp_path: Path) -> None:
    """grant 已落地、呼叫端還沒拿到 owner_token 就 crash：重啟後以同一個
    decision 重送拿回同一筆 reservation（duplicate，owner_token 相同），不另
    grant 第二份容量，之後用拿回的 token 照常 bind。"""
    path = tmp_path / "reservations.jsonl"
    crashing = QuotaReservationAuthority(path, failpoint=_crash_at("reserve-after-append"))
    with pytest.raises(_Crash):
        _reserve(crashing)
    assert _lines(path) == 1

    restarted = QuotaReservationAuthority(path)
    assert restarted.committed(now_ms=NOW) == {_KEY: "1"}
    replay = _reserve(restarted)
    assert replay.status == "duplicate"
    assert replay.state == "reserved" and replay.sequence == 0
    stored = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert replay.owner_token == stored["owner_token"]
    assert _lines(path) == 1
    assert restarted.committed(now_ms=NOW) == {_KEY: "1"}
    bound = restarted.bind(
        reservation_id=replay.reservation_id, owner_token=replay.owner_token,
        attempt_id="attempt-1", job_id="job-1", expected_sequence=0, now_ms=NOW,
    )
    assert bound.status == "ok"


def _seed(path: Path, *, bind: bool):
    seed = QuotaReservationAuthority(path)
    granted = _reserve(seed)
    assert granted.status == "granted"
    if bind:
        assert seed.bind(
            reservation_id=granted.reservation_id, owner_token=granted.owner_token,
            attempt_id="attempt-1", job_id="job-1", expected_sequence=0, now_ms=NOW,
        ).status == "ok"
    return granted


def _transition(authority, stage: str, granted, *, expected_sequence: int, job_id: str = "job-1",
                outcome: str = "succeeded"):
    if stage == "bind":
        return authority.bind(
            reservation_id=granted.reservation_id, owner_token=granted.owner_token,
            attempt_id="attempt-1", job_id=job_id, expected_sequence=expected_sequence, now_ms=NOW,
        )
    if stage == "settle":
        return authority.settle(
            reservation_id=granted.reservation_id, owner_token=granted.owner_token,
            attempt_id="attempt-1", outcome=outcome, expected_sequence=expected_sequence, now_ms=NOW,
        )
    if stage == "release":
        return authority.release(
            reservation_id=granted.reservation_id, owner_token=granted.owner_token,
            attempt_id="attempt-1", reason="fail-before-spawn", expected_sequence=expected_sequence,
            now_ms=NOW,
        )
    if stage == "renew":
        return authority.renew(
            reservation_id=granted.reservation_id, owner_token=granted.owner_token,
            attempt_id="attempt-1", lease_ms=120_000, expected_sequence=expected_sequence, now_ms=NOW,
        )
    assert stage == "reconcile"
    return authority.reconcile(
        reservation_id=granted.reservation_id,
        evidence={"kind": "job-registry-terminal", "job_id": "job-1", "job_outcome": "succeeded"},
        resolution="confirmed-terminated", expected_sequence=expected_sequence, now_ms=NOW,
    )


# (stage, 需要先 bind, crash 後重開的狀態, 重送結果, 重送理由, crash 後 committed)
_AFTER_APPEND_MATRIX = [
    ("bind", False, "bound", "duplicate", None, {_KEY: "1"}),
    ("settle", True, "settled", "duplicate", None, {}),
    ("release", False, "released", "duplicate", None, {}),
    ("renew", False, "reserved", "conflict", "sequence-mismatch", {_KEY: "1"}),
    ("reconcile", True, "released", "conflict", "reservation-already-terminal", {}),
]


@pytest.mark.parametrize(
    ("stage", "needs_bind", "state_after", "replay_status", "replay_reason", "committed"),
    _AFTER_APPEND_MATRIX, ids=[row[0] for row in _AFTER_APPEND_MATRIX],
)
def test_ac3_after_append_crash_replay_never_applies_the_transition_twice(
    tmp_path: Path, stage, needs_bind, state_after, replay_status, replay_reason, committed,
) -> None:
    """事件已 append、fsync，呼叫端還沒收到結果就 crash：重啟後讀到的是已套用
    的狀態；呼叫端依自己手上的舊 sequence 重送，bind／settle／release 回
    duplicate（冪等回放），renew／reconcile 被 CAS 擋下；任何一種都不再
    append 事件、不改 committed。"""
    path = tmp_path / "reservations.jsonl"
    granted = _seed(path, bind=needs_bind)
    before = _lines(path)
    expected_sequence = 1 if needs_bind else 0
    crashing = QuotaReservationAuthority(path, failpoint=_crash_at(f"{stage}-after-append"))
    with pytest.raises(_Crash):
        _transition(crashing, stage, granted, expected_sequence=expected_sequence)
    assert _lines(path) == before + 1  # 事件確實已落地

    restarted = QuotaReservationAuthority(path)
    status = restarted.status(granted.reservation_id, now_ms=NOW)
    assert status.state == state_after
    assert status.sequence == expected_sequence + 1
    replay = _transition(restarted, stage, granted, expected_sequence=expected_sequence)
    assert replay.status == replay_status
    assert replay.reason == replay_reason
    assert _lines(path) == before + 1
    assert restarted.committed(now_ms=NOW) == committed


def test_ac3_bind_replay_duplicate_only_for_the_same_job(tmp_path: Path) -> None:
    """bind 冪等分支：同一個 job_id 重送（不論帶舊或新 sequence）回
    duplicate、不 append；換成別的 job_id 一律 conflict，不改綁。"""
    path = tmp_path / "reservations.jsonl"
    granted = _seed(path, bind=True)
    before = _lines(path)
    authority = QuotaReservationAuthority(path)
    for expected_sequence in (0, 1):
        replay = _transition(authority, "bind", granted, expected_sequence=expected_sequence)
        assert (replay.status, replay.state, replay.sequence) == ("duplicate", "bound", 1)
    other = _transition(authority, "bind", granted, expected_sequence=1, job_id="job-2")
    assert (other.status, other.reason) == ("conflict", "job-id-mismatch")
    assert _lines(path) == before
    assert authority.status(granted.reservation_id, now_ms=NOW).job_id == "job-1"


def _counting_resume(registry, run_id, identities, worktree, coordinator_root, *, ctx, launches):
    class _CountingLauncher(_Launcher):
        def launch(self, *, slice_id, prompt, worktree, log_dir):
            launches.append(slice_id)
            return super().launch(slice_id=slice_id, prompt=prompt, worktree=worktree, log_dir=log_dir)

    dispatcher = type("D", (), {
        "_registry": registry, "_git_runner": None, "_worktree_creator": _FakeWorktreeCreator(worktree),
        # periodic resume 對在跑的 job 先 poll；這裡回 registry 現況（仍 dispatched）。
        "poll_headless_done": lambda self, job_id: registry.get_job(job_id),
    })()
    return manager.resume_workflow_run(
        dispatcher, run_id=run_id, identities=identities,
        launcher_factory=lambda identity: _CountingLauncher(identity.executor, identity.model_id),
        coordinator_root=coordinator_root, quota_admission_context=ctx,
    )


def test_ac3_manager_restart_does_not_redispatch_or_rereserve_a_live_bound_job(tmp_path: Path) -> None:
    registry, run, identities, worktree, ctx, _, _ = _dispatch_fixture(tmp_path, environment=_ENFORCE)
    launches: list[str] = []
    first = _counting_resume(registry, run.run_id, identities, worktree, tmp_path / "coordinator",
                             ctx=ctx, launches=launches)
    job_id = first["job_id"]
    assert first["reason"] == "in-flight"
    assert launches == [job_id]
    reservations = tmp_path / "quota" / "reservations.jsonl"
    decisions = tmp_path / "quota" / "decisions.jsonl"
    reserve_rows = _lines(reservations)
    decision_rows = _lines(decisions)

    # Manager 重啟：新 process 的 registry／authority／store／ledger instance
    # 全部重開同一批檔案，process 內的 in-flight 集合是空的。
    restarted_registry = JobRegistry(state_path=tmp_path / "jobs.json")
    restarted_ctx = _ctx(tmp_path, descriptors=ctx.descriptors, bindings=ctx.bindings, environment=_ENFORCE)
    assert not quota_admission.IN_FLIGHT_DISPATCHES.is_in_flight(
        restarted_ctx.authority.list_by_state("bound", now_ms=int(time.time() * 1000))[0].reservation_id
    )
    swept = manager.reconcile_quota_admission_reservations(
        registry=restarted_registry, quota_admission_context=restarted_ctx,
    )
    assert [item["detail"] for item in swept["bound"]] == ["confirmed-alive:ok"]

    for _ in range(2):  # 連續兩輪 periodic resume
        again = _counting_resume(restarted_registry, run.run_id, identities, worktree,
                                 tmp_path / "coordinator", ctx=restarted_ctx, launches=launches)
        assert again == {"run_id": run.run_id, "current_phase": "build", "job_id": job_id,
                         "reason": "in-flight"}
    assert launches == [job_id]
    assert [job["job_id"] for job in restarted_registry.list_jobs()] == [job_id]
    now_ms = int(time.time() * 1000)
    [bound] = restarted_ctx.authority.list_by_state("bound", now_ms=now_ms)
    assert bound.job_id == job_id
    assert restarted_ctx.authority.list_by_state("reserved", now_ms=now_ms) == ()
    assert restarted_ctx.authority.committed(now_ms=now_ms) == {
        (("operator-budget-authority", "acct-codex", "pool-codex", "1"), "week"): "1"}
    # 只多了 sweep 的那一筆 confirmed-alive reconcile，沒有新的 reserve／bind；
    # 也沒有新的 decision receipt。
    assert _lines(reservations) == reserve_rows + 1
    assert _lines(decisions) == decision_rows


# ---------------------------------------------------------------------------
# G838-2（AC5）：window reset 與 reservation×ledger 交叉
# ---------------------------------------------------------------------------


def _assess(ctx, profile_key: str, *, now_ms: int):
    assessment, demand_version = quota_admission.assess_candidate_quota(
        executor="codex", model_id="gpt-x", independence_domain="openai", profile_key=profile_key,
        bindings=ctx.bindings, descriptors=ctx.descriptors, unit_catalog=ctx.unit_catalog,
        shadow=ctx.shadow, now_ms=now_ms,
    )
    return assessment, demand_version


def _reserve_candidate(ctx, assessment, demand_version, *, attempt: str, now_ms: int):
    return quota_admission.reserve_for_candidate(
        ctx.authority, run_id="run-1", card_id="card-1",
        decision_id=f"adm-{attempt}", attempt_id=attempt, assessment=assessment,
        observation_version=quota_admission.observation_fingerprint(assessment),
        demand_version=demand_version, lease_ms=600_000, now_ms=now_ms,
    )


_WEEK = 604_800_000
_BASE = 1_800_000_000_000


def _week_ctx(tmp_path: Path):
    descriptor = _descriptor(account="acct-shared", pool="pool-shared", windows=(("week", _WEEK),))
    ctx = _ctx(tmp_path, descriptors=(descriptor,),
               bindings=(_profile_binding(descriptor, _PROFILE_WORKER, binding_id="worker"),))
    return ctx, descriptor


def _observe(ctx, descriptor, value: str, *, observed_at_ms: int, reset_at_ms: int) -> None:
    assert ctx.shadow.record_observation(
        _snapshot(descriptor, "week", value, observed_at_ms=observed_at_ms, reset_at_ms=reset_at_ms,
                  profile_key=_PROFILE_WORKER),
        descriptors=ctx.descriptors, unit_catalog=ctx.unit_catalog,
    ).accepted == 1


def _week_key(descriptor):
    return ((descriptor.authority_id, descriptor.account_id, descriptor.pool_id, descriptor.revision), "week")


def test_ac5_window_reset_keeps_the_active_reservation_counted(tmp_path: Path) -> None:
    """舊 window 剩 1、reserve 1 之後 window reset：新 snapshot 給出新 capacity，
    但仍在跑的 reservation 沒被洗掉——新 capacity 1 時再要 1 單位被拒，新
    capacity 2 時才剛好容得下第二份。"""
    ctx, descriptor = _week_ctx(tmp_path)
    old_reset = _BASE + 1_000
    _observe(ctx, descriptor, "1", observed_at_ms=_BASE, reset_at_ms=old_reset)
    assessment, version = _assess(ctx, _PROFILE_WORKER, now_ms=_BASE + 10)
    first = _reserve_candidate(ctx, assessment, version, attempt="attempt-a", now_ms=_BASE + 10)
    assert first.status == "granted"

    # reset 之後的新 window：snapshot 反映新配額 1。
    new_reset = old_reset + _WEEK
    _observe(ctx, descriptor, "1", observed_at_ms=old_reset + 5, reset_at_ms=new_reset)
    assessment, version = _assess(ctx, _PROFILE_WORKER, now_ms=old_reset + 10)
    assert assessment.feasible  # 投影本身只看 ledger：新 window 剩 1
    denied = _reserve_candidate(ctx, assessment, version, attempt="attempt-b", now_ms=old_reset + 10)
    assert denied.status == "denied"
    assert denied.denied_pools[0]["committed"] == "1"
    assert ctx.authority.status(first.reservation_id, now_ms=old_reset + 10).state == "reserved"

    # 更晚的 snapshot 給 2：舊 reservation 仍計入，第二份剛好 grant。
    _observe(ctx, descriptor, "2", observed_at_ms=old_reset + 20, reset_at_ms=new_reset)
    assessment, version = _assess(ctx, _PROFILE_WORKER, now_ms=old_reset + 30)
    second = _reserve_candidate(ctx, assessment, version, attempt="attempt-c", now_ms=old_reset + 30)
    assert second.status == "granted"
    assert ctx.authority.committed(now_ms=old_reset + 30) == {_week_key(descriptor): "2"}


def _settle_through_periodic_reconcile(ctx, reservation, *, job: dict, now_ms: int):
    """用 production 的 bound 收斂＋`on_settled`（`_quota_admission_record_terminal_usage`）
    結算：與 manager `reconcile_quota_admission_reservations` 第 2 步同一組呼叫。"""
    decision = quota_admission.AdmissionDecision(
        decision_id=f"adm-{reservation['attempt']}", run_id="run-1", card_id="card-1",
        attempt_id=reservation["attempt"], profile_key=_PROFILE_WORKER, mode="enforced",
        outcome="admit", policy_version=quota_admission.ADMISSION_POLICY_VERSION,
        observation_version="fixture", demand_version=quota_admission.DEMAND_FIXTURE_VERSION,
        qualification_version="not-applicable", generated_at_ms=now_ms,
        selected={"executor": "codex", "model_id": "gpt-x", "independence_domain": "openai"},
        reservation_id=reservation["reservation_id"],
    )
    if ctx.store.get(decision.decision_id) is None:
        ctx.store.record(decision)
    return quota_admission.reconcile_bound_reservations(
        authority=ctx.authority, store=ctx.store,
        job_lookup=lambda job_id: job if job_id == job["job_id"] else None,
        job_outcome=manager._quota_admission_job_terminal_outcome, now_ms=now_ms,
        on_settled=lambda decision, found: manager._quota_admission_record_terminal_usage(
            ctx, profile_key=decision.profile_key, job=found, now_ms=now_ms,
        ),
    )


def _bind(ctx, granted, *, attempt: str, job_id: str, now_ms: int) -> None:
    assert ctx.authority.bind(
        reservation_id=granted.reservation_id, owner_token=granted.owner_token, attempt_id=attempt,
        job_id=job_id, expected_sequence=granted.sequence, now_ms=now_ms,
    ).status == "ok"


def _available(ctx, descriptor, *, now_ms: int):
    """可再預留量＝投影剩餘（已扣 ledger 內的終局 usage）－ 有效 reservation。"""
    remaining = _row(ctx.shadow.project(descriptors=ctx.descriptors, unit_catalog=ctx.unit_catalog,
                                        now_utc_ms=now_ms), descriptor.pool_id, "week")["remaining"]
    committed = ctx.authority.committed(now_ms=now_ms).get(_week_key(descriptor), "0")
    return remaining, committed


def test_ac5_reservation_and_terminal_usage_are_never_counted_together(tmp_path: Path) -> None:
    """job 跑的期間只有 reservation 佔容量（ledger 還沒有它的 usage）；終局後
    reservation 釋放、usage 進 ledger——任何時點都只有其中一份。重送收斂／重播
    usage 不再扣。"""
    ctx, descriptor = _week_ctx(tmp_path)
    _observe(ctx, descriptor, "10", observed_at_ms=_BASE, reset_at_ms=_BASE + _WEEK)
    assessment, version = _assess(ctx, _PROFILE_WORKER, now_ms=_BASE + 10)
    granted = _reserve_candidate(ctx, assessment, version, attempt="attempt-a", now_ms=_BASE + 10)
    assert granted.status == "granted"
    _bind(ctx, granted, attempt="attempt-a", job_id="job-a", now_ms=_BASE + 20)

    remaining, committed = _available(ctx, descriptor, now_ms=_BASE + 30)
    assert remaining["amount"] == {"kind": "exact", "value": "10"}
    assert committed == "1"

    job = {"job_id": "job-a", "executor": "codex", "model_id": "gpt-x", "status": "exited", "exit_code": 0,
           "usage": {"input_tokens": 3, "output_tokens": 0},
           "started_at": _iso(_BASE + 25), "exited_at": _iso(_BASE + 900)}
    outcomes = _settle_through_periodic_reconcile(
        ctx, {"attempt": "attempt-a", "reservation_id": granted.reservation_id}, job=job, now_ms=_BASE + 1_000,
    )
    assert [o.action for o in outcomes] == ["settled"]
    remaining, committed = _available(ctx, descriptor, now_ms=_BASE + 2_000)
    assert remaining["amount"] == {"kind": "exact", "value": "7"}
    assert committed == "0"

    # 重送：再跑一輪收斂（已終局 → 沒有 bound 可掃）＋ harvest 重播 usage。
    assert list(_settle_through_periodic_reconcile(
        ctx, {"attempt": "attempt-a", "reservation_id": granted.reservation_id}, job=job, now_ms=_BASE + 3_000,
    )) == []
    manager._quota_admission_record_terminal_usage(ctx, profile_key=_PROFILE_WORKER, job=job,
                                                   now_ms=_BASE + 3_000)
    remaining, committed = _available(ctx, descriptor, now_ms=_BASE + 4_000)
    assert remaining["amount"] == {"kind": "exact", "value": "7"}
    assert committed == "0"
    assert len(_usage_rows(tmp_path / "quota" / "events.jsonl")) == 2


def test_ac5_mid_run_snapshot_stays_conservative_and_is_not_deducted_twice(tmp_path: Path) -> None:
    """job 跑到一半 provider snapshot 已反映部分消耗（10→8），reservation 仍佔
    1：這段期間是保守偏差（8－1），不會超發。job 終局後 usage 橫跨該 snapshot
    ——沒有可切分的增量證據，投影轉 unknown（straddling-usage），不做 8－3 的
    第二次扣減；下一張 job 結束後的新 snapshot 才回到確定值。"""
    ctx, descriptor = _week_ctx(tmp_path)
    _observe(ctx, descriptor, "10", observed_at_ms=_BASE, reset_at_ms=_BASE + _WEEK)
    assessment, version = _assess(ctx, _PROFILE_WORKER, now_ms=_BASE + 10)
    granted = _reserve_candidate(ctx, assessment, version, attempt="attempt-a", now_ms=_BASE + 10)
    _bind(ctx, granted, attempt="attempt-a", job_id="job-a", now_ms=_BASE + 20)

    _observe(ctx, descriptor, "8", observed_at_ms=_BASE + 500, reset_at_ms=_BASE + _WEEK)
    remaining, committed = _available(ctx, descriptor, now_ms=_BASE + 600)
    assert (remaining["amount"], committed) == ({"kind": "exact", "value": "8"}, "1")

    job = {"job_id": "job-a", "executor": "codex", "model_id": "gpt-x", "status": "exited", "exit_code": 0,
           "usage": {"input_tokens": 3, "output_tokens": 0},
           "started_at": _iso(_BASE + 25), "exited_at": _iso(_BASE + 900)}
    _settle_through_periodic_reconcile(
        ctx, {"attempt": "attempt-a", "reservation_id": granted.reservation_id}, job=job, now_ms=_BASE + 1_000,
    )
    remaining, committed = _available(ctx, descriptor, now_ms=_BASE + 1_100)
    assert remaining == {"state": "unknown", "reason": "straddling-usage"}
    assert committed == "0"
    # unknown 在 enforce 下不可行：不會拿 8 當容量再超發。
    assessment, _ = _assess(ctx, _PROFILE_WORKER, now_ms=_BASE + 1_100)
    assert not assessment.feasible

    _observe(ctx, descriptor, "7", observed_at_ms=_BASE + 1_200, reset_at_ms=_BASE + _WEEK)
    remaining, _ = _available(ctx, descriptor, now_ms=_BASE + 1_300)
    assert remaining["amount"] == {"kind": "exact", "value": "7"}


def test_ac5_old_window_job_settled_after_reset_does_not_deduct_the_new_window(tmp_path: Path) -> None:
    """job 在舊 window 內開始也結束，但 reset 後才被收斂：它的消耗屬於舊
    window，新 window 的剩餘量不能因此被扣，reservation 照常釋放。"""
    ctx, descriptor = _week_ctx(tmp_path)
    old_reset = _BASE + 2_000
    _observe(ctx, descriptor, "5", observed_at_ms=_BASE, reset_at_ms=old_reset)
    assessment, version = _assess(ctx, _PROFILE_WORKER, now_ms=_BASE + 10)
    granted = _reserve_candidate(ctx, assessment, version, attempt="attempt-a", now_ms=_BASE + 10)
    _bind(ctx, granted, attempt="attempt-a", job_id="job-a", now_ms=_BASE + 20)

    _observe(ctx, descriptor, "30", observed_at_ms=old_reset + 10, reset_at_ms=old_reset + _WEEK)
    job = {"job_id": "job-a", "executor": "codex", "model_id": "gpt-x", "status": "exited", "exit_code": 0,
           "usage": {"input_tokens": 4, "output_tokens": 0},
           "started_at": _iso(_BASE + 25), "exited_at": _iso(_BASE + 1_500)}
    _settle_through_periodic_reconcile(
        ctx, {"attempt": "attempt-a", "reservation_id": granted.reservation_id}, job=job,
        now_ms=old_reset + 100,
    )
    remaining, committed = _available(ctx, descriptor, now_ms=old_reset + 200)
    assert remaining["amount"] == {"kind": "exact", "value": "30"}
    assert committed == "0"
    assert len(_usage_rows(tmp_path / "quota" / "events.jsonl")) == 2  # 消耗仍有耐久紀錄


# ---------------------------------------------------------------------------
# G838-3：coverage gap 機讀輸出、死開關移除
# ---------------------------------------------------------------------------


def test_authority_coverage_declares_cross_host_and_cross_principal_gaps() -> None:
    coverage = quota_reservation.authority_coverage()
    assert coverage["schema"] == "cortex/quota-reservation-coverage/v1"
    assert coverage["state"] == "partial"
    gaps = {gap["scope"]: gap["reason"] for gap in coverage["gaps"]}
    assert gaps == {
        "cross-host": "reservation-authority-host-local-flock",
        "cross-principal": "reservation-store-owner-only-permissions",
        "cross-coordinator-root": "reservation-authority-scoped-to-coordinator-root",
    }
    assert coverage["coordinated"] == {
        "host": "single-host", "principal": "store-owner-uid", "instances": "same-coordinator-root",
    }


def test_cli_quota_reservations_report_counts_states_and_emits_coverage_gaps(tmp_path: Path, capsys) -> None:
    store = tmp_path / "quota-reservations" / "reservations.jsonl"
    authority = QuotaReservationAuthority(store)
    reserved = _reserve(authority, attempt="attempt-r", decision="decision-r")
    bound = _reserve(authority, attempt="attempt-b", decision="decision-b")
    authority.bind(reservation_id=bound.reservation_id, owner_token=bound.owner_token,
                   attempt_id="attempt-b", job_id="job-b", expected_sequence=0, now_ms=NOW)
    settled = _reserve(authority, attempt="attempt-s", decision="decision-s")
    authority.bind(reservation_id=settled.reservation_id, owner_token=settled.owner_token,
                   attempt_id="attempt-s", job_id="job-s", expected_sequence=0, now_ms=NOW)
    authority.settle(reservation_id=settled.reservation_id, owner_token=settled.owner_token,
                     attempt_id="attempt-s", outcome="succeeded", expected_sequence=1, now_ms=NOW)
    assert reserved.status == "granted"

    exit_code = quota_cli.main(["reservations", "--store", str(store), "--json"])
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "cortex-porcelain/quota-reservations-report/v1"
    assert report["authority"] == {"store": str(store), "store_source": "--store", "exists": True}
    assert report["coverage"] == quota_reservation.authority_coverage()
    # lease 60s 早已過期（NOW 是固定過去時間）：reserved／bound 顯示為 uncertain，
    # 但仍計入 committed。
    assert report["states"] == {"reserved": 1, "bound": 1, "settled": 1, "released": 0}
    assert report["uncertain"] == 2
    assert report["committed"] == [{"pool_ref": _POOL, "window_id": "week", "amount": "2"}]


def test_cli_quota_reservations_report_without_store_is_empty_but_still_declares_gaps(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(tmp_path / "coordinator"))
    exit_code = quota_cli.main(["reservations", "--json"])
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["authority"] == {
        "store": str(tmp_path / "coordinator" / "quota-reservations" / "reservations.jsonl"),
        "store_source": "coordinator-root", "exists": False,
    }
    assert report["states"] == {"reserved": 0, "bound": 0, "settled": 0, "released": 0}
    assert report["uncertain"] == 0
    assert report["committed"] == []
    assert {gap["scope"] for gap in report["coverage"]["gaps"]} == {
        "cross-host", "cross-principal", "cross-coordinator-root"}


def test_cli_quota_reservations_report_fails_closed_on_corrupt_store(tmp_path: Path, capsys) -> None:
    store = tmp_path / "quota-reservations" / "reservations.jsonl"
    store.parent.mkdir(mode=0o700)
    store.write_text("{broken\n", encoding="utf-8")
    os.chmod(store, 0o600)
    assert quota_cli.main(["reservations", "--store", str(store), "--json"]) == 1
    assert "reservation-store-invalid-json" in capsys.readouterr().err


def test_dead_reservation_enforce_switch_is_removed_and_admission_switch_is_the_only_gate() -> None:
    """`PSC_QUOTA_RESERVATION_ENFORCE` 從來沒有 production 讀取點：開它不會
    讓 manager 預留，關它也不會讓 enforce 停止預留。移除後唯一開關是
    `PSC_QUOTA_ADMISSION_ENFORCE`（enforce ⇒ 原子預留）。"""
    assert not hasattr(quota_reservation, "reservation_authority_enabled")
    assert "reservation_authority_enabled" not in quota_reservation.__all__
    # 任何讀取 env 的程式都需要這個名字的完整字串常數（`env.get("...")`／
    # `_FLAG = "..."`）；文件字串裡提到它不算。整個套件都不得再有。
    package_root = Path(quota_reservation.__file__).resolve().parents[1]
    readers = [
        str(path.relative_to(package_root))
        for path in package_root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and node.value == "PSC_QUOTA_RESERVATION_ENFORCE"
    ]
    assert readers == []
    assert quota_admission.quota_admission_enabled({"PSC_QUOTA_RESERVATION_ENFORCE": "on"}) is False
    assert quota_admission.quota_admission_enabled(
        {"PSC_QUOTA_ADMISSION_ENFORCE": "on", "PSC_QUOTA_RESERVATION_ENFORCE": "off"}
    ) is True


def test_enforce_dispatch_reserves_without_any_reservation_specific_switch(tmp_path: Path) -> None:
    """只開 `PSC_QUOTA_ADMISSION_ENFORCE`（沒有任何 reservation 專屬開關）：
    派工就走原子預留並 bind；只開舊的 reservation 開關則維持 shadow、不建
    reservation 檔。"""
    registry, run, identities, worktree, ctx, _, _ = _dispatch_fixture(
        tmp_path / "enforce", environment=_ENFORCE,
    )
    launches: list[str] = []
    result = _counting_resume(registry, run.run_id, identities, worktree, tmp_path / "enforce" / "coordinator",
                              ctx=ctx, launches=launches)
    [bound] = ctx.authority.list_by_state("bound", now_ms=int(time.time() * 1000))
    assert bound.job_id == result["job_id"]

    registry, run, identities, worktree, ctx, _, _ = _dispatch_fixture(
        tmp_path / "legacy-switch", environment={"PSC_QUOTA_RESERVATION_ENFORCE": "on"},
    )
    result = _counting_resume(registry, run.run_id, identities, worktree,
                              tmp_path / "legacy-switch" / "coordinator", ctx=ctx, launches=launches)
    assert result["reason"] == "in-flight"
    assert not (tmp_path / "legacy-switch" / "quota" / "reservations.jsonl").exists()
    [decision] = ctx.store.all_rows()
    assert decision["mode"] == "shadow"
