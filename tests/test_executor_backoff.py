"""#850: executor backoff store strict reader state classification coverage."""

from __future__ import annotations

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
