"""#838：跨 instance 共享的原子 quota reservation authority。

驗收對應原票「最小機械驗收」六條：
- AC1：兩 process barrier 同池剩一單位，恰好一個成功；不依 model 拆池。
- AC2：多 pool all-or-none，無半張 grant，失敗不扣其他池。
- AC3：crash matrix（reserve／bind／settle 各階段以 failpoint 注入）可安全 reconcile。
- AC4：negative——lease 過期非活 job 證明、liveness unknown、錯 owner/CAS/attempt、
  損毀 state、負值/非有限需求皆不可增加可派額度。
- AC5：replay/reset——同 decision 同 receipt 冪等、late terminal 只結原 attempt、
  新觀測版本不清有效 reservation。
- AC6：bounded worker／固定輪數壓力測試 + 耐久 audit。
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from paulsha_cortex.coordinator.quota_reservation import (
    PoolDemand,
    QuotaReservationAuthority,
    ReservationCorrupt,
    reservation_authority_enabled,
)


def _pool(pool_id: str = "pool-x", revision: str = "r1") -> dict[str, str]:
    return {
        "authority_id": "authority-a",
        "account_id": "acct-1",
        "pool_id": pool_id,
        "revision": revision,
    }


def _demand(amount: str, *, pool_id: str = "pool-x", window_id: str = "week") -> PoolDemand:
    return PoolDemand(pool_ref=_pool(pool_id=pool_id), window_id=window_id, amount=amount)


def _cap(amount: str, *, pool_id: str = "pool-x", window_id: str = "week"):
    return {(tuple(_pool(pool_id=pool_id)[k] for k in
                    ("authority_id", "account_id", "pool_id", "revision")), window_id): amount}


NOW = 1_700_000_000_000


# ---------------------------------------------------------------------------
# 開關：預設 shadow／不阻擋
# ---------------------------------------------------------------------------


def test_enforcement_flag_defaults_off_and_only_on_is_true() -> None:
    assert reservation_authority_enabled({}) is False
    assert reservation_authority_enabled({"PSC_QUOTA_RESERVATION_ENFORCE": "off"}) is False
    assert reservation_authority_enabled({"PSC_QUOTA_RESERVATION_ENFORCE": "garbage"}) is False
    assert reservation_authority_enabled({"PSC_QUOTA_RESERVATION_ENFORCE": "ON"}) is True
    assert reservation_authority_enabled({"PSC_QUOTA_RESERVATION_ENFORCE": "on"}) is True


# ---------------------------------------------------------------------------
# AC1：兩 process barrier、同池剩一單位、恰好一個成功
# ---------------------------------------------------------------------------


def _race_reserve_worker(store_path: str, attempt_id: str, barrier, result_queue) -> None:
    authority = QuotaReservationAuthority(store_path)
    barrier.wait(timeout=10)
    try:
        result = authority.reserve(
            run_id="run-1", card_id="card-1", decision_id="decision-race",
            attempt_id=attempt_id, pools=(_demand("1"),), capacity_by_pool=_cap("1"),
            observation_version="obs-v1", demand_version="demand-v1",
            lease_ms=60_000, now_ms=NOW,
        )
        result_queue.put((attempt_id, result.status))
    except Exception as exc:  # noqa: BLE001 - 回報給 parent 判斷，不吞掉
        result_queue.put((attempt_id, f"error:{exc!r}"))


def test_ac1_two_process_barrier_one_unit_pool_exactly_one_winner(tmp_path: Path) -> None:
    store_path = str(tmp_path / "reservations.jsonl")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    result_queue = context.Queue()
    # 兩個 process 用**不同** attempt_id（不同 decision）搶**同一個** pool/window
    # 僅剩的 1 單位容量——這正是「不同 instance 同 pool 共 authority」的核心情境：
    # 誰先搶到都行，但兩者不能都成功。
    processes = [
        context.Process(target=_race_reserve_worker, args=(store_path, f"attempt-{i}", barrier, result_queue))
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

    authority = QuotaReservationAuthority(store_path)
    committed = authority.committed(now_ms=NOW)
    key = (("authority-a", "acct-1", "pool-x", "r1"), "week")
    assert committed[key] == "1"


def test_ac1_pool_ref_has_no_model_dimension() -> None:
    """pool_ref 沿用 #836 契約（authority/account/pool/revision），刻意不含
    model——換 model 名字無法偷換到不同的 authority／拆開共用池。"""
    demand = _demand("1")
    assert set(demand.pool_ref) == {"authority_id", "account_id", "pool_id", "revision"}
    assert "model" not in demand.pool_ref
    assert "model_id" not in demand.pool_ref


# ---------------------------------------------------------------------------
# AC2：多 pool all-or-none，無半張 grant，失敗不扣其他池
# ---------------------------------------------------------------------------


def test_ac2_multi_pool_all_or_none_denies_whole_reservation(tmp_path: Path) -> None:
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    pools = (_demand("1", pool_id="pool-a"), _demand("5", pool_id="pool-b"))
    capacity = {**_cap("10", pool_id="pool-a"), **_cap("1", pool_id="pool-b")}  # pool-b 不足
    result = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-multi", attempt_id="attempt-1",
        pools=pools, capacity_by_pool=capacity, observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    assert result.status == "denied"
    denied_ids = {(item["pool_ref"]["pool_id"], item["window_id"]) for item in result.denied_pools}
    assert denied_ids == {("pool-b", "week")}

    # 失敗不扣其他池：pool-a 的容量完全未被這次失敗的嘗試佔用。
    committed = authority.committed(now_ms=NOW)
    assert committed == {}

    # 另一個只要 pool-a 的請求仍能吃滿 pool-a 的全部容量，證明第一次失敗
    # 沒有留下任何半張 grant。
    only_a = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-a-only", attempt_id="attempt-2",
        pools=(_demand("10", pool_id="pool-a"),), capacity_by_pool=_cap("10", pool_id="pool-a"),
        observation_version="obs-v1", demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    assert only_a.status == "granted"


def test_ac2_write_failure_leaves_no_partial_grant(tmp_path: Path) -> None:
    """寫入故障（failpoint 模擬崩潰）不得留下半張 grant：整筆事件從未落地，
    容量完全未被佔用，caller 可以立刻重試並成功。"""
    calls: list[str] = []

    def failpoint(stage: str) -> None:
        calls.append(stage)
        raise RuntimeError("simulated crash before durable write")

    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl", failpoint=failpoint)
    pools = (_demand("1", pool_id="pool-a"), _demand("1", pool_id="pool-b"))
    capacity = {**_cap("1", pool_id="pool-a"), **_cap("1", pool_id="pool-b")}
    with pytest.raises(RuntimeError, match="simulated crash"):
        authority.reserve(
            run_id="run-1", card_id="card-1", decision_id="decision-crash", attempt_id="attempt-1",
            pools=pools, capacity_by_pool=capacity, observation_version="obs-v1",
            demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
        )
    assert calls == ["reserve-before-append"]

    restarted = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    assert restarted.committed(now_ms=NOW) == {}
    retry = restarted.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-crash", attempt_id="attempt-1",
        pools=pools, capacity_by_pool=capacity, observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    assert retry.status == "granted"


# ---------------------------------------------------------------------------
# AC3：crash matrix（failpoint 注入）+ restart reconcile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["bind-before-append", "settle-before-append", "release-before-append"])
def test_ac3_crash_failpoints_never_apply_a_half_transition(tmp_path: Path, stage: str) -> None:
    path = tmp_path / "reservations.jsonl"
    seed = QuotaReservationAuthority(path)
    granted = seed.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-crash", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    assert granted.status == "granted"
    # release 只允許在 spawn 前（reserved）；bound 之後的取消走 settle(cancelled)。
    if stage == "settle-before-append":
        bound = seed.bind(
            reservation_id=granted.reservation_id, owner_token=granted.owner_token,
            attempt_id="attempt-1", job_id="job-1", expected_sequence=0, now_ms=NOW,
        )
        assert bound.status == "ok"

    def failpoint(current: str) -> None:
        if current == stage:
            raise RuntimeError("simulated crash")

    crashing = QuotaReservationAuthority(path, failpoint=failpoint)
    expected_sequence = 1 if stage == "bind-before-append" else 1
    with pytest.raises(RuntimeError, match="simulated crash"):
        if stage == "bind-before-append":
            crashing.bind(
                reservation_id=granted.reservation_id, owner_token=granted.owner_token,
                attempt_id="attempt-1", job_id="job-1", expected_sequence=0, now_ms=NOW,
            )
        elif stage == "settle-before-append":
            crashing.settle(
                reservation_id=granted.reservation_id, owner_token=granted.owner_token,
                attempt_id="attempt-1", outcome="succeeded", expected_sequence=1, now_ms=NOW,
            )
        else:
            crashing.release(
                reservation_id=granted.reservation_id, owner_token=granted.owner_token,
                attempt_id="attempt-1", reason="fail-before-spawn", expected_sequence=0, now_ms=NOW,
            )

    restarted = QuotaReservationAuthority(path)
    status = restarted.status(granted.reservation_id, now_ms=NOW)
    if stage == "release-before-append":
        assert status.state == "reserved"
        retry = restarted.release(
            reservation_id=granted.reservation_id, owner_token=granted.owner_token,
            attempt_id="attempt-1", reason="fail-before-spawn", expected_sequence=0, now_ms=NOW,
        )
        assert retry.status == "ok"
    elif stage == "bind-before-append":
        assert status.state == "reserved"
        retry = restarted.bind(
            reservation_id=granted.reservation_id, owner_token=granted.owner_token,
            attempt_id="attempt-1", job_id="job-1", expected_sequence=0, now_ms=NOW,
        )
        assert retry.status == "ok"
    else:
        assert status.state == "bound"
        if stage == "settle-before-append":
            retry = restarted.settle(
                reservation_id=granted.reservation_id, owner_token=granted.owner_token,
                attempt_id="attempt-1", outcome="succeeded", expected_sequence=1, now_ms=NOW,
            )
        assert retry.status == "ok"


def test_ac3_restart_after_spawn_success_without_bind_stays_reserved_and_reconciles(tmp_path: Path) -> None:
    """「spawn 成功未綁定」：reserve 後 process 崩潰，bind() 從未被呼叫。restart
    後的新 instance 不知道 owner_token；不能無憑無據放掉，只能靠 reconcile。"""
    path = tmp_path / "reservations.jsonl"
    original = QuotaReservationAuthority(path)
    granted = original.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-orphan", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=1_000, now_ms=NOW,
    )
    assert granted.status == "granted"
    # "process 崩潰"：後續操作用一個全新的 authority instance，且不再持有
    # granted.owner_token（模擬 owner_token 隨行程消失）。
    restarted = QuotaReservationAuthority(path)
    later = NOW + 10_000  # 早已超過 1 秒的 lease
    status = restarted.status(granted.reservation_id, now_ms=later)
    assert status.state == "reserved"
    assert status.display_state == "uncertain"
    # 容量仍被佔用——lease 過期本身不證明可以釋放。
    assert restarted.committed(now_ms=later)[
        (("authority-a", "acct-1", "pool-x", "r1"), "week")
    ] == "1"

    inconclusive = restarted.reconcile(
        reservation_id=granted.reservation_id,
        evidence={"kind": "job-registry-lookup", "detail": "no-record-yet"},
        resolution="inconclusive", expected_sequence=0, now_ms=later,
    )
    assert inconclusive.status == "ok"
    assert inconclusive.display_state == "uncertain"
    assert restarted.committed(now_ms=later)  # 仍持有容量

    terminated = restarted.reconcile(
        reservation_id=granted.reservation_id,
        evidence={"kind": "job-registry-lookup", "detail": "confirmed-not-running"},
        resolution="confirmed-terminated", expected_sequence=1, now_ms=later,
    )
    assert terminated.status == "ok"
    assert terminated.state == "released"
    assert restarted.committed(now_ms=later) == {}


# ---------------------------------------------------------------------------
# AC4：negative
# ---------------------------------------------------------------------------


def test_ac4_expired_lease_alone_never_frees_capacity_or_allows_wrong_owner_release(tmp_path: Path) -> None:
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    granted = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-live", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=1_000, now_ms=NOW,
    )
    later = NOW + 5_000
    # 錯 owner：即使 lease 已過期，沒有正確 owner_token 一樣不能釋放。
    wrong_owner = authority.release(
        reservation_id=granted.reservation_id, owner_token="not-the-real-token",
        attempt_id="attempt-1", reason="cancelled", expected_sequence=0, now_ms=later,
    )
    assert wrong_owner.status == "invalid"
    assert wrong_owner.reason == "owner-mismatch"
    assert authority.committed(now_ms=later)  # 仍持有


def test_ac4_wrong_attempt_id_rejected(tmp_path: Path) -> None:
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    granted = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-attempt", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    result = authority.bind(
        reservation_id=granted.reservation_id, owner_token=granted.owner_token,
        attempt_id="attempt-WRONG", job_id="job-1", expected_sequence=0, now_ms=NOW,
    )
    assert result.status == "invalid"
    assert result.reason == "attempt-mismatch"


def test_ac4_wrong_cas_sequence_rejected(tmp_path: Path) -> None:
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    granted = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-cas", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    stale = authority.bind(
        reservation_id=granted.reservation_id, owner_token=granted.owner_token,
        attempt_id="attempt-1", job_id="job-1", expected_sequence=99, now_ms=NOW,
    )
    assert stale.status == "conflict"
    assert stale.reason == "sequence-mismatch"
    assert stale.sequence == 0


def test_ac4_unknown_liveness_reconcile_stays_uncertain_and_holds_capacity(tmp_path: Path) -> None:
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    granted = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-unknown", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=1_000, now_ms=NOW,
    )
    later = NOW + 5_000
    result = authority.reconcile(
        reservation_id=granted.reservation_id, evidence={"kind": "liveness-probe", "detail": "timeout"},
        resolution="inconclusive", expected_sequence=0, now_ms=later,
    )
    assert result.status == "ok"
    assert result.display_state == "uncertain"
    status = authority.status(granted.reservation_id, now_ms=later)
    assert status.display_state == "uncertain"
    assert authority.committed(now_ms=later)


def test_ac4_corrupt_store_fails_closed_never_treated_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "reservations.jsonl"
    authority = QuotaReservationAuthority(path)
    granted = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-corrupt", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    assert granted.status == "granted"
    # 模擬 torn write：截掉結尾換行，讓最後一行變成不完整的殘骸。
    raw = path.read_bytes()
    path.write_bytes(raw[:-1])

    reopened = QuotaReservationAuthority(path)
    with pytest.raises(ReservationCorrupt):
        reopened.committed(now_ms=NOW)
    with pytest.raises(ReservationCorrupt):
        reopened.status(granted.reservation_id, now_ms=NOW)
    # 損毀時 reserve() 也必須 fail closed，不能把「讀不懂」誤判成「空 ledger、
    # 全部可派」。
    with pytest.raises(ReservationCorrupt):
        reopened.reserve(
            run_id="run-1", card_id="card-1", decision_id="decision-corrupt-2", attempt_id="attempt-1",
            pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
            demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
        )


def _append_raw_row(path: Path, row: dict) -> None:
    import json as _json

    with path.open("a", encoding="utf-8") as handle:
        handle.write(_json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def test_ac4_illegal_transition_history_fails_closed(tmp_path: Path) -> None:
    """損毀但 shape 合法的歷史（reserve→settle→bind）不得把已終局 reservation
    復活成 bound 並重新占用容量；讀回必須 ReservationCorrupt。"""
    path = tmp_path / "reservations.jsonl"
    authority = QuotaReservationAuthority(path)
    granted = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-illegal", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    settled = authority.settle(
        reservation_id=granted.reservation_id, owner_token=granted.owner_token,
        attempt_id="attempt-1", outcome="succeeded", expected_sequence=0, now_ms=NOW + 1,
    )
    assert settled.status == "ok"
    _append_raw_row(path, {
        "schema_version": 1, "kind": "bind", "reservation_id": granted.reservation_id,
        "sequence": 2, "job_id": "job-revived", "event_at_ms": NOW + 2,
    })
    with pytest.raises(ReservationCorrupt):
        QuotaReservationAuthority(path).committed(now_ms=NOW + 3)


def test_ac4_malformed_reconcile_numbers_fail_closed_not_type_error(tmp_path: Path) -> None:
    """reconcile row 的 renew_lease_ms／event_at_ms 型別錯誤必須一致地
    ReservationCorrupt，不得噴裸 TypeError。"""
    path = tmp_path / "reservations.jsonl"
    authority = QuotaReservationAuthority(path)
    granted = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-reconcile", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    _append_raw_row(path, {
        "schema_version": 1, "kind": "reconcile", "reservation_id": granted.reservation_id,
        "sequence": 1, "resolution": "confirmed-alive", "evidence": {},
        "renew_lease_ms": "1000", "event_at_ms": NOW + 1,
    })
    with pytest.raises(ReservationCorrupt):
        QuotaReservationAuthority(path).committed(now_ms=NOW + 2)


def test_ac4_bound_reservation_cannot_be_released_even_with_replayed_owner_token(tmp_path: Path) -> None:
    """reserve() 冪等回放會交回 owner_token；bound（已 spawn）之後不得再以
    release 釋放 lease 並讓容量被重新 grant——只能 settle 或 reconcile。"""
    path = tmp_path / "reservations.jsonl"
    authority = QuotaReservationAuthority(path)
    kwargs = dict(
        run_id="run-1", card_id="card-1", decision_id="decision-bound", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    granted = authority.reserve(**kwargs)
    bound = authority.bind(
        reservation_id=granted.reservation_id, owner_token=granted.owner_token,
        attempt_id="attempt-1", job_id="job-live", expected_sequence=0, now_ms=NOW,
    )
    assert bound.status == "ok"
    replay = QuotaReservationAuthority(path).reserve(**kwargs)
    assert replay.status == "duplicate"
    released = QuotaReservationAuthority(path).release(
        reservation_id=replay.reservation_id, owner_token=replay.owner_token,
        attempt_id="attempt-1", reason="cancelled", expected_sequence=replay.sequence, now_ms=NOW + 1,
    )
    assert released.status == "conflict"
    other = QuotaReservationAuthority(path).reserve(**{**kwargs, "decision_id": "decision-other"})
    assert other.status != "granted"


def test_ac4_reconcile_and_reserve_rows_are_revalidated_on_reload(tmp_path: Path) -> None:
    path = tmp_path / "reservations.jsonl"
    authority = QuotaReservationAuthority(path)
    granted = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-reload", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    _append_raw_row(path, {
        "schema_version": 1, "kind": "reconcile", "reservation_id": granted.reservation_id,
        "sequence": 1, "resolution": "confirmed-terminated", "evidence": {},
        "renew_lease_ms": None, "event_at_ms": NOW + 1,
    })
    with pytest.raises(ReservationCorrupt):
        QuotaReservationAuthority(path).committed(now_ms=NOW + 2)

    import json as _json

    second = tmp_path / "second.jsonl"
    QuotaReservationAuthority(second).reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-window", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    rows = [_json.loads(line) for line in second.read_text(encoding="utf-8").splitlines()]
    rows[0]["pools"][0]["window_id"] = 1
    second.write_text(
        "".join(_json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    with pytest.raises(ReservationCorrupt):
        QuotaReservationAuthority(second).committed(now_ms=NOW + 2)


def test_ac4_negative_or_non_finite_amount_rejected() -> None:
    for bad in ("-1", "nan", "inf", "-inf", "abc", ""):
        with pytest.raises(ValueError):
            PoolDemand(pool_ref=_pool(), window_id="week", amount=bad)


def test_ac4_duplicate_pool_window_within_one_reservation_rejected(tmp_path: Path) -> None:
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    result = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-dup-pool", attempt_id="attempt-1",
        pools=(_demand("1"), _demand("1")), capacity_by_pool=_cap("5"),
        observation_version="obs-v1", demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    assert result.status == "invalid"
    assert authority.committed(now_ms=NOW) == {}


def test_ac4_missing_capacity_for_requested_pool_rejected(tmp_path: Path) -> None:
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    result = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-missing-cap", attempt_id="attempt-1",
        pools=(_demand("1", pool_id="pool-a"), _demand("1", pool_id="pool-b")),
        capacity_by_pool=_cap("5", pool_id="pool-a"),  # pool-b 沒有對應的 capacity
        observation_version="obs-v1", demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    assert result.status == "invalid"
    assert authority.committed(now_ms=NOW) == {}


def test_ac4_negative_or_non_finite_capacity_rejected(tmp_path: Path) -> None:
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    for bad_capacity in ("-1", "nan", "inf"):
        result = authority.reserve(
            run_id="run-1", card_id="card-1", decision_id=f"decision-bad-{bad_capacity}",
            attempt_id="attempt-1", pools=(_demand("1"),),
            capacity_by_pool=_cap(bad_capacity), observation_version="obs-v1",
            demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
        )
        assert result.status == "invalid", bad_capacity
    assert authority.committed(now_ms=NOW) == {}


# ---------------------------------------------------------------------------
# AC5：replay/reset
# ---------------------------------------------------------------------------


def test_ac5_same_decision_same_receipt_is_idempotent(tmp_path: Path) -> None:
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    kwargs = dict(
        run_id="run-1", card_id="card-1", decision_id="decision-idem", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    first = authority.reserve(**kwargs)
    second = authority.reserve(**kwargs)
    assert first.status == "granted"
    assert second.status == "duplicate"
    assert second.reservation_id == first.reservation_id
    assert second.owner_token == first.owner_token
    # 沒有寫入第二筆事件：容量只被佔用一次。
    assert authority.committed(now_ms=NOW)[
        (("authority-a", "acct-1", "pool-x", "r1"), "week")
    ] == "1"


def test_ac5_decision_reused_with_different_composition_is_conflict_not_silent_pick(tmp_path: Path) -> None:
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    first = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-reuse", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    assert first.status == "granted"
    conflicting = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-reuse", attempt_id="attempt-1",
        pools=(_demand("2"),), capacity_by_pool=_cap("2"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    assert conflicting.status == "conflict"


def test_ac5_fresh_observation_version_does_not_wipe_the_existing_reservation(tmp_path: Path) -> None:
    """新觀測版本重試同一個 decision（composition 不變）必須視為冪等重播，
    不清掉既有 reservation、不重複計入 committed。"""
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    first = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-refresh", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    retried = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-refresh", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v2-newer",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW + 500,
    )
    assert retried.status == "duplicate"
    assert retried.reservation_id == first.reservation_id
    assert authority.committed(now_ms=NOW)[
        (("authority-a", "acct-1", "pool-x", "r1"), "week")
    ] == "1"


def test_ac5_late_terminal_resend_settles_only_the_original_attempt_idempotently(tmp_path: Path) -> None:
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    granted = authority.reserve(
        run_id="run-1", card_id="card-1", decision_id="decision-terminal", attempt_id="attempt-1",
        pools=(_demand("1"),), capacity_by_pool=_cap("1"), observation_version="obs-v1",
        demand_version="demand-v1", lease_ms=60_000, now_ms=NOW,
    )
    bound = authority.bind(
        reservation_id=granted.reservation_id, owner_token=granted.owner_token,
        attempt_id="attempt-1", job_id="job-1", expected_sequence=0, now_ms=NOW,
    )
    assert bound.status == "ok"
    first_settle = authority.settle(
        reservation_id=granted.reservation_id, owner_token=granted.owner_token,
        attempt_id="attempt-1", outcome="succeeded", expected_sequence=1, now_ms=NOW,
    )
    assert first_settle.status == "ok"
    # terminal 事件重送（例如 executor webhook 重試）：同一個 outcome 必須是
    # 冪等的 duplicate，不能二次結算、也不能因為 expected_sequence 現在已經
    # 過期就報 conflict。
    resend = authority.settle(
        reservation_id=granted.reservation_id, owner_token=granted.owner_token,
        attempt_id="attempt-1", outcome="succeeded", expected_sequence=1, now_ms=NOW + 10,
    )
    assert resend.status == "duplicate"

    # 另一次重送帶了不同的 outcome：必須是 conflict，不能悄悄改寫終局結果。
    mismatched_resend = authority.settle(
        reservation_id=granted.reservation_id, owner_token=granted.owner_token,
        attempt_id="attempt-1", outcome="failed", expected_sequence=1, now_ms=NOW + 20,
    )
    assert mismatched_resend.status == "conflict"
    assert authority.committed(now_ms=NOW) == {}


# ---------------------------------------------------------------------------
# AC6：bounded worker／固定輪數壓力測試 + 耐久 audit
# ---------------------------------------------------------------------------


def _stress_worker(store_path: str, worker_id: int, barrier, result_queue) -> None:
    authority = QuotaReservationAuthority(store_path)
    barrier.wait(timeout=20)
    try:
        result = authority.reserve(
            run_id="run-stress", card_id="card-stress", decision_id=f"decision-{worker_id}",
            attempt_id="attempt-1", pools=(_demand("1"),), capacity_by_pool=_cap("5"),
            observation_version="obs-v1", demand_version="demand-v1",
            lease_ms=60_000, now_ms=NOW,
        )
        result_queue.put((worker_id, result.status))
    except Exception as exc:  # noqa: BLE001
        result_queue.put((worker_id, f"error:{exc!r}"))


def test_ac6_bounded_worker_pressure_never_overcommits_and_store_stays_auditable(tmp_path: Path) -> None:
    store_path = str(tmp_path / "reservations.jsonl")
    worker_count = 10
    capacity = 5
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(worker_count)
    result_queue = context.Queue()
    processes = [
        context.Process(target=_stress_worker, args=(store_path, i, barrier, result_queue))
        for i in range(worker_count)
    ]
    for process in processes:
        process.start()
    results = [result_queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    granted = [worker_id for worker_id, status in results if status == "granted"]
    denied = [worker_id for worker_id, status in results if status == "denied"]
    errors = [item for item in results if item[1].startswith("error:")]
    assert errors == [], errors
    assert len(granted) == capacity
    assert len(denied) == worker_count - capacity

    authority = QuotaReservationAuthority(store_path)
    key = (("authority-a", "acct-1", "pool-x", "r1"), "week")
    assert authority.committed(now_ms=NOW)[key] == str(capacity)

    # 耐久 audit：依檔案內事件順序（flock 序列化寫入的真實順序）重播，任何一
    # 個前綴的累計持有量都不得超過 capacity——這是比對外 committed() 快照更
    # 強的不變式，直接稽核底層事件流本身沒有任何時刻超額。
    from paulsha_cortex.coordinator.quota_reservation import _fold

    raw_lines = (tmp_path / "reservations.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == capacity  # 只有成功 grant 的那幾筆才會落地成事件
    import json as _json

    running: dict[tuple, int] = {}
    for line in raw_lines:
        row = _json.loads(line)
        assert row["kind"] == "reserve"
        for item in row["pools"]:
            pool_key = (tuple(item["pool_ref"][k] for k in
                               ("authority_id", "account_id", "pool_id", "revision")), item["window_id"])
            running[pool_key] = running.get(pool_key, 0) + int(item["amount"])
            assert running[pool_key] <= capacity


def _round_trip_worker(store_path: str, worker_id: int, rounds: int, barriers, result_queue) -> None:
    authority = QuotaReservationAuthority(store_path)
    outcomes: list[str] = []
    for round_index in range(rounds):
        barriers[round_index].wait(timeout=20)
        result = authority.reserve(
            run_id="run-rounds", card_id="card-rounds",
            decision_id=f"decision-{worker_id}-{round_index}", attempt_id="attempt-1",
            pools=(_demand("1"),), capacity_by_pool=_cap("2"),
            observation_version="obs-v1", demand_version="demand-v1",
            lease_ms=60_000, now_ms=NOW,
        )
        outcomes.append(result.status)
        if result.status == "granted":
            authority.release(
                reservation_id=result.reservation_id, owner_token=result.owner_token,
                attempt_id="attempt-1", reason="cancelled", expected_sequence=0, now_ms=NOW,
            )
    result_queue.put((worker_id, outcomes))


def test_ac6_fixed_rounds_release_and_reacquire_stays_consistent_across_workers(tmp_path: Path) -> None:
    """固定輪數：每輪所有 worker 同時搶同一個容量=2 的 pool，成功者立刻釋放，
    讓下一輪重新競爭——驗證 release 之後容量確實可再被別人拿到，且從未超額。"""
    store_path = str(tmp_path / "reservations.jsonl")
    worker_count = 4
    rounds = 3
    context = multiprocessing.get_context("spawn")
    barriers = [context.Barrier(worker_count) for _ in range(rounds)]
    result_queue = context.Queue()
    processes = [
        context.Process(target=_round_trip_worker, args=(store_path, i, rounds, barriers, result_queue))
        for i in range(worker_count)
    ]
    for process in processes:
        process.start()
    results = [result_queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    for round_index in range(rounds):
        granted_this_round = sum(1 for _, outcomes in results if outcomes[round_index] == "granted")
        assert granted_this_round == 2, (round_index, results)

    authority = QuotaReservationAuthority(store_path)
    assert authority.committed(now_ms=NOW) == {}  # 全部釋放完畢
