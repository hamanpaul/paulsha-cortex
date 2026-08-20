"""Tick-failure resilience for the manager daemon's periodic scheduler.

Covers issue #249: a periodic tick that raises must not turn the
``tick_interval`` schedule into a hot loop. These tests exercise
``run_loop``'s failure-backoff, circuit-breaker, health-signal, and
error-log de-duplication behaviour using fully injected fake clocks/sleeps
(never a real ``time.sleep``).
"""

from __future__ import annotations

from paulsha_cortex.control import constants, contract
from paulsha_cortex.coordinator import manager_daemon


class _FakeClock:
    """Deterministic monotonic clock: value only advances via ``sleep``.

    Mirrors real ``time.monotonic()``/``time.sleep()`` semantics: multiple
    reads within the same "instant" (before any sleep) return the same
    value, and only the loop's end-of-round ``sleep_fn`` call moves time
    forward. This lets tests reason about elapsed wall-clock time across
    many rounds without ever performing a real sleep.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _write_request(req_id: str, **overrides) -> dict:
    request = {
        "schema_version": constants.SCHEMA_VERSION,
        "req_id": req_id,
        "type": "tick",
        "args": {"executor": "copilot"},
        "requested_by": "cockpit",
        "created_at": "2026-07-03T09:00:00+00:00",
    }
    request.update(overrides)
    contract.atomic_write_json(constants.requests_dir() / f"{req_id}.json", request)
    return request


def _expected_backoff_attempt_clocks(tick_interval: float, count: int) -> list[float]:
    """Replicate the production backoff formula to compute expected attempt times."""
    clocks: list[float] = []
    elapsed = 0.0
    consecutive_failures = 0
    for _ in range(count):
        elapsed += manager_daemon._tick_backoff_seconds(tick_interval, consecutive_failures)
        clocks.append(elapsed)
        consecutive_failures += 1
    return clocks


def test_periodic_tick_backoff_prevents_hot_retry_and_resets_after_success(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    clock = _FakeClock()
    call_clocks: list[float] = []

    def periodic_tick_runner() -> dict:
        call_clocks.append(clock.value)
        if len(call_clocks) <= 2:
            raise ValueError("boom")
        return {"dispatch_skipped": False}

    started = manager_daemon.run_loop(
        request_executor=lambda req: {"dispatched": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=periodic_tick_runner,
        poll_interval=1.0,
        tick_interval=10.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        pid=1,
        max_rounds=85,
    )

    assert started is True
    # 1st attempt at t=10 (base tick_interval) fails; backoff to +20 -> 2nd
    # attempt at t=30 fails; backoff to +40 -> 3rd attempt at t=70 succeeds;
    # normal cadence resumes -> 4th attempt at t=80 (just +10, not +80).
    assert call_clocks == [10.0, 30.0, 70.0, 80.0]
    # 85 rounds at a 1s poll would be ~85 calls under the old hot-loop bug;
    # backoff keeps the call count nowhere near that.
    assert len(call_clocks) < 10

    status = contract.read_json(constants.status_path())
    assert status["daemon"]["consecutive_tick_failures"] == 0
    assert status["daemon"]["tick_circuit_open"] is False
    assert status["daemon"]["last_tick_error"] is None


def test_periodic_tick_circuit_breaker_stops_calls_but_keeps_draining_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    clock = _FakeClock()
    tick_interval = 1.0
    poll_interval = 1.0

    periodic_calls: list[float] = []

    def periodic_tick_runner() -> dict:
        periodic_calls.append(clock.value)
        raise ValueError("periodic tick always fails")

    processed_req_ids: list[str] = []
    seed_counter = {"n": 0}

    def request_executor(req: dict) -> dict:
        processed_req_ids.append(req["req_id"])
        seed_counter["n"] += 1
        _write_request(f"20260703T090000Z-seed{seed_counter['n']:05d}", type="dispatch")
        return {"dispatched": []}

    _write_request("20260703T090000Z-seed00000", type="dispatch")

    attempt_clocks = _expected_backoff_attempt_clocks(
        tick_interval, manager_daemon.TICK_CIRCUIT_BREAKER_THRESHOLD
    )
    max_rounds = int(attempt_clocks[-1]) + 10  # buffer, still far short of the cooldown

    started = manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=periodic_tick_runner,
        poll_interval=poll_interval,
        tick_interval=tick_interval,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        pid=1,
        max_rounds=max_rounds,
    )

    assert started is True
    # Circuit opens exactly at the threshold-th consecutive failure and stays
    # open for the (much longer) cooldown window, so no further periodic
    # attempts happen within this test's round budget.
    assert len(periodic_calls) == manager_daemon.TICK_CIRCUIT_BREAKER_THRESHOLD
    # The request queue (the operator rescue channel) drains one request per
    # round throughout -- entirely unaffected by the periodic circuit breaker.
    assert len(processed_req_ids) == max_rounds

    status = contract.read_json(constants.status_path())
    daemon = status["daemon"]
    assert daemon["tick_circuit_open"] is True
    assert daemon["consecutive_tick_failures"] == manager_daemon.TICK_CIRCUIT_BREAKER_THRESHOLD
    assert daemon["last_tick_error"]["type"] == "ValueError"


def test_status_reflects_tick_failure_and_resets_after_success(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))

    def failing_runner() -> dict:
        raise ValueError("boom: sentinel reason")

    failure_points = iter([0.0, 5.0, 5.0])
    manager_daemon.run_loop(
        request_executor=lambda req: {"dispatched": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=failing_runner,
        poll_interval=0.0,
        tick_interval=5.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: next(failure_points),
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    status_after_failure = contract.read_json(constants.status_path())
    daemon_after_failure = status_after_failure["daemon"]
    assert daemon_after_failure["consecutive_tick_failures"] == 1
    assert daemon_after_failure["tick_circuit_open"] is False
    assert daemon_after_failure["last_tick_error"]["type"] == "ValueError"
    assert "sentinel reason" in daemon_after_failure["last_tick_error"]["reason"]
    assert daemon_after_failure["last_tick_at"] is None  # never succeeded yet

    success_points = iter([0.0, 5.0, 5.0])
    manager_daemon.run_loop(
        request_executor=lambda req: {"dispatched": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=5.0,
        now_fn=lambda: "2026-07-03T09:06:00+00:00",
        monotonic_fn=lambda: next(success_points),
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    status_after_success = contract.read_json(constants.status_path())
    daemon_after_success = status_after_success["daemon"]
    assert daemon_after_success["consecutive_tick_failures"] == 0
    assert daemon_after_success["tick_circuit_open"] is False
    assert daemon_after_success["last_tick_error"] is None
    assert daemon_after_success["last_tick_at"] == "2026-07-03T09:06:00+00:00"


def test_periodic_tick_cadence_unaffected_when_no_failures_occur(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    clock = _FakeClock()
    call_clocks: list[float] = []

    def periodic_tick_runner() -> dict:
        call_clocks.append(clock.value)
        return {"dispatch_skipped": False}

    manager_daemon.run_loop(
        request_executor=lambda req: {"dispatched": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=periodic_tick_runner,
        poll_interval=1.0,
        tick_interval=10.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        pid=1,
        max_rounds=35,
    )

    # Untouched regression: three ticks land exactly on tick_interval
    # multiples, with zero drift from the new backoff/circuit machinery.
    assert call_clocks == [10.0, 20.0, 30.0]


def test_log_error_deduplicates_repeated_signature_with_periodic_summary(capsys):
    manager_daemon._reset_log_error_dedup_state()
    exc = ValueError("same failure every time")
    total_calls = 220

    for _ in range(total_calls):
        manager_daemon._log_error(exc)

    output_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]

    interval = manager_daemon.LOG_ERROR_SUMMARY_INTERVAL
    # #765 補遺：首次出現另附 traceback（未 raise 的例外＝1 行例外文字）。
    expected_lines = 2 + (total_calls - 1) // interval
    assert len(output_lines) == expected_lines
    assert len(output_lines) < total_calls
    # First occurrence is always printed in full, never suppressed.
    assert "same failure every time" in output_lines[0]
    # At least one summary line proves the error is still recurring.
    assert any("repeated" in line and "same failure every time" in line for line in output_lines[1:])

    manager_daemon._log_error(RuntimeError("a totally different problem"))
    stderr_tail = capsys.readouterr().err
    assert "a totally different problem" in stderr_tail


def test_log_error_deduplicates_interleaved_signatures_independently(capsys):
    """issue #374：單槽去重被多筆錯誤輪替瓦解的回歸測試。

    daemon 每輪 tick 會交錯產生多個不同 signature（實測每輪 14 個，來自
    #373 的 14 個受害 run）。修復前的單槽實作只存一筆 signature+count，
    一旦下一筆 signature 不同就整槽重置——於是 ``state["signature"] !=
    signature`` 恆真，每筆都印、抑制摘要永遠不會觸發（#249 的抑制路徑
    形同虛設）。本測試以 3 個 signature 各送 200 筆、輪流交錯（呼叫序為
    sig1, sig2, sig3, sig1, sig2, sig3, ...），驗證每個 signature 各自
    獨立計數、各自的週期摘要都要出現，且交錯的不同 signature 不得互相
    重置對方的計數。

    在修復前（單槽）此測試必為 RED：交錯情境下抑制數＝0，
    ``len(sig1_lines) == 200``（等同 total_calls，完全沒有被抑制）。
    """
    manager_daemon._reset_log_error_dedup_state()
    signatures = [
        ValueError("interleaved failure alpha"),
        ValueError("interleaved failure beta"),
        ValueError("interleaved failure gamma"),
    ]
    calls_per_signature = 200
    total_calls = calls_per_signature * len(signatures)

    for _ in range(calls_per_signature):
        for exc in signatures:
            manager_daemon._log_error(exc)

    output_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]

    # 核心斷言：交錯不得瓦解抑制——印出行數必須遠少於送入筆數。
    assert len(output_lines) < total_calls

    interval = manager_daemon.LOG_ERROR_SUMMARY_INTERVAL
    # #765 補遺：首次出現另附 traceback 1 行。
    expected_lines_per_signature = 2 + (calls_per_signature - 1) // interval
    for exc in signatures:
        marker = str(exc)
        sig_lines = [line for line in output_lines if marker in line]
        # 每個 signature 各自的抑制節奏都要符合既有單一 signature 語意
        # （首筆必印＋每 interval 筆一次摘要），交錯不改變這個節奏。
        assert len(sig_lines) == expected_lines_per_signature, (
            f"{marker!r} 印出 {len(sig_lines)} 行，預期 {expected_lines_per_signature} 行"
        )
        assert marker in sig_lines[0]
        assert any("repeated" in line for line in sig_lines[1:])


def test_log_error_dedup_lru_eviction_evicts_oldest_slot(capsys):
    """LRU 容量上限：signature 數超過 ``LOG_ERROR_DEDUP_MAX_SLOTS`` 時，
    最久未使用的 slot 必須被淘汰；被淘汰的 signature 再次出現時視為
    首見（重新印出完整行，而非被抑制）。這是 signature 含
    ``source_revision``（#373 的迴圈會改寫它）情境下避免記憶體無界
    成長的必要防線。"""
    manager_daemon._reset_log_error_dedup_state()
    max_slots = manager_daemon.LOG_ERROR_DEDUP_MAX_SLOTS

    first_signature_exc = ValueError("eviction-candidate-0")
    manager_daemon._log_error(first_signature_exc)
    capsys.readouterr()  # 清空第一筆的輸出，只關注後續行為

    # 灌入 max_slots 個「全新」signature，把最早的那個擠出 LRU。
    for index in range(1, max_slots + 1):
        manager_daemon._log_error(ValueError(f"eviction-filler-{index}"))
    capsys.readouterr()

    # 原本第一個 signature 已被淘汰，理應被視為「首見」而非抑制重複。
    manager_daemon._log_error(first_signature_exc)
    stderr_tail = capsys.readouterr().err
    assert "eviction-candidate-0" in stderr_tail
    assert "repeated" not in stderr_tail


def test_safe_tick_error_summary_redacts_paths_and_caps_length(tmp_path: Path):
    """受測路徑一律由 tmp_path 動態組出——測試檔本身不得出現使用者絕對路徑的
    結構字面值（``/home/<name>/``），否則會反過來觸發 R-21 secret scan。
    這條規則在本輪已重複踩中三次，故於此明文記錄。"""
    leaked = tmp_path / ".agents" / "core" / "registry" / "db.sqlite"
    exc = ValueError(f"failed reading {leaked} during scan")

    summary = manager_daemon._safe_tick_error_summary(exc)

    assert summary["type"] == "ValueError"
    assert str(tmp_path) not in summary["reason"]
    assert "db.sqlite" not in summary["reason"]
    assert "<path>" in summary["reason"]

    long_exc = RuntimeError("x" * 500)
    long_summary = manager_daemon._safe_tick_error_summary(long_exc)
    assert len(long_summary["reason"]) <= manager_daemon.TICK_ERROR_REASON_MAX_LENGTH + 1
