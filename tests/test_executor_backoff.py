"""#850: executor backoff store strict reader must not collapse corrupt state into missing."""

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
