"""#850: executor backoff store strict reader state classification coverage."""

from __future__ import annotations

import os
from pathlib import Path


def test_invalid_store_bytes_stay_unknown_instead_of_falling_back_to_missing(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    store_path = tmp_path / "executor-backoff-state.json"
    store_path.write_text("{not-json", encoding="utf-8")

    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "unknown"
    assert observed.diagnostics


def test_missing_store_is_reported_as_missing(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    store_path = tmp_path / "executor-backoff-state.json"
    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "missing"
    assert observed.payload is None
    assert not observed.diagnostics


def test_invalid_utf8_store_bytes_stay_unknown(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    store_path = tmp_path / "executor-backoff-state.json"
    store_path.write_bytes(b"\xff\xfe")

    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "unknown"
    assert observed.diagnostics


def test_valid_json_object_store_is_reported_as_valid(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    store_path = tmp_path / "executor-backoff-state.json"
    store_path.write_text('{"entries": {}}', encoding="utf-8")

    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "valid"
    assert observed.payload == {"entries": {}}
    assert not observed.diagnostics


def test_non_object_json_store_stays_unknown(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    store_path = tmp_path / "executor-backoff-state.json"
    store_path.write_text('["not-an-object"]', encoding="utf-8")

    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "unknown"
    assert observed.payload is None
    assert observed.diagnostics


def test_fifo_store_stays_unknown_without_blocking(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    store_path = tmp_path / "executor-backoff-state.json"
    os.mkfifo(store_path)

    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "unknown"
    assert observed.payload is None
    assert observed.diagnostics


def test_lstat_fault_stays_unknown(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    store_path = tmp_path / "executor-backoff-state.json"
    original_lstat = Path.lstat

    def failing_lstat(self: Path):
        if self == store_path:
            raise PermissionError("stat denied")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", failing_lstat)
    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "unknown"
    assert observed.payload is None
    assert observed.diagnostics


def test_symlink_target_stat_fault_stays_unknown(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    target_path = tmp_path / "target.json"
    target_path.write_text("{}", encoding="utf-8")
    store_path = tmp_path / "executor-backoff-state.json"
    store_path.symlink_to(target_path)
    original_stat = Path.stat

    def failing_stat(self: Path, *args, **kwargs):
        if self == store_path:
            raise PermissionError("target stat denied")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_stat)
    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "unknown"
    assert observed.payload is None
    assert observed.diagnostics


def test_store_that_disappears_during_read_stays_missing(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    store_path = tmp_path / "executor-backoff-state.json"
    store_path.write_text("{}", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def disappearing_read_bytes(self: Path) -> bytes:
        if self == store_path:
            store_path.unlink()
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", disappearing_read_bytes)
    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "missing"
    assert not observed.diagnostics


def test_read_fault_stays_unknown(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    store_path = tmp_path / "executor-backoff-state.json"
    store_path.write_text("{}", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def failing_read_bytes(self: Path) -> bytes:
        if self == store_path:
            raise PermissionError("read denied")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", failing_read_bytes)
    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "unknown"
    assert observed.payload is None
    assert observed.diagnostics


def test_post_read_recheck_fault_stays_unknown(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    store_path = tmp_path / "executor-backoff-state.json"
    store_path.write_text("{}", encoding="utf-8")
    original_lstat = Path.lstat
    original_read_bytes = Path.read_bytes
    lstat_calls = 0

    def flaky_lstat(self: Path):
        nonlocal lstat_calls
        if self == store_path:
            lstat_calls += 1
            if lstat_calls == 2:
                raise PermissionError("recheck denied")
        return original_lstat(self)

    def missing_read_bytes(self: Path) -> bytes:
        if self == store_path:
            raise FileNotFoundError("gone")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "lstat", flaky_lstat)
    monkeypatch.setattr(Path, "read_bytes", missing_read_bytes)
    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "unknown"
    assert observed.payload is None
    assert observed.diagnostics


def test_dangling_symlink_store_stays_unknown(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    store_path = tmp_path / "executor-backoff-state.json"
    store_path.symlink_to(tmp_path / "missing-target.json")

    observed = executor_backoff.read_store(store_path)
    observation = observed.observation
    if not isinstance(observation, str):
        observation = observation.value

    assert observation == "unknown"
    assert observed.diagnostics
