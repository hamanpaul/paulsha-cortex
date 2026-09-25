"""RED regression for #983 delivery journal conditional commit.

Two writers that load the same journal baseline must not let the later stale
snapshot silently overwrite the first committed run row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.coordinator import work_actions


def _run_row(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "claim_key": f"{run_id}-claim",
        "snapshot_hash": f"{run_id}-snapshot",
        "source_revisions": [f"{run_id}@authority"],
        "provider_revision": f"{run_id}-provider",
        "authority_digest": f"{run_id}-authority",
        "workflow_step_ids": [f"{run_id}:build:tdd-red"],
    }


def test_stale_snapshot_conflicts_before_overwriting_another_run(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    first_writer = work_actions._load_runs(journal_path)
    stale_writer = work_actions._load_runs(journal_path)

    first_writer["runs"]["run-a"] = _run_row("run-a")
    stale_writer["runs"]["run-b"] = _run_row("run-b")

    work_actions._save_runs(journal_path, first_writer)

    with pytest.raises(RuntimeError, match="stale|conflict"):
        work_actions._save_runs(journal_path, stale_writer)

    persisted = work_actions._load_runs(journal_path)
    assert set(persisted["runs"]) == {"run-a"}
    assert persisted["runs"]["run-a"] == first_writer["runs"]["run-a"]
