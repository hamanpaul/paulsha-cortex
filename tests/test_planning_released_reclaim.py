"""#299：abandon 已釋放（superseded＋planning_released）的 run 不得短路同
claim_key 的重新 claim（#256 D4 釋放語意）。

registry 層（`_manager_create_workflow_run` 以 attempt 鹽化 run_id）本已支援
abandon→reclaim 循環；缺口在 `work_bridge.start_canonical_workflow` 的
existing-run reuse guard 對 superseded run 無條件短路。本測試鎖定過濾 helper
與 registry reclaim 循環兩層。
"""

from __future__ import annotations

from pathlib import Path

from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.work_bridge import _claimable_existing_runs
from paulsha_cortex.coordinator.workflow import WorkflowStep

CLAIM_KEY = "claim:v1:" + "a" * 64


def _step() -> WorkflowStep:
    return WorkflowStep(
        phase="define",
        persona="planner",
        card="brainstorming",
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        gate_result="pending",
    )


def _create_run(registry: JobRegistry, **overrides: object):
    fields: dict[str, object] = {
        "work_id": "released-reclaim-work",
        "repo": "hamanpaul/paulsha-cortex",
        "claim_key": CLAIM_KEY,
        "source_revision": "rev-a",
        "workspace_root": "/tmp/workspace",
        "combo": "feature-oneshot",
        "current_phase": "define",
        "steps": (_step(),),
        "issue_refs": ("hamanpaul/paulsha-cortex#299",),
        "attempts": {"claim": 1},
        "facets": ("needs_human",),
    }
    fields.update(overrides)
    return registry._manager_create_workflow_run(**fields)


def test_released_run_not_claimable_match(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _create_run(registry)
    registry._manager_abandon_workflow_run(
        run.run_id, evidence_ref="evidence/work-abandon/test-released.json"
    )
    released = registry.get_workflow_run(run.run_id)
    assert released.status == "superseded"
    assert "planning_released" in released.facets

    assert _claimable_existing_runs(registry, CLAIM_KEY) == []


def test_ongoing_run_still_matches(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _create_run(registry)
    matches = _claimable_existing_runs(registry, CLAIM_KEY)
    assert [match.run_id for match in matches] == [run.run_id]


def test_unreleased_superseded_run_still_matches(tmp_path: Path) -> None:
    """digest 前進造成的 superseded（無 planning_released）維持原短路行為。"""
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _create_run(registry)
    registry._manager_update_workflow_run(run.run_id, status="superseded")
    matches = _claimable_existing_runs(registry, CLAIM_KEY)
    assert [match.run_id for match in matches] == [run.run_id]
    assert "planning_released" not in matches[0].facets


def test_registry_reclaim_creates_fresh_run_after_release(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    first = _create_run(registry)
    registry._manager_abandon_workflow_run(
        first.run_id, evidence_ref="evidence/work-abandon/test-cycle.json"
    )
    second = _create_run(registry)
    assert second.run_id != first.run_id
    assert second.status == "ongoing"
    old = registry.get_workflow_run(first.run_id)
    assert old.status == "superseded"
    assert "planning_released" in old.facets
