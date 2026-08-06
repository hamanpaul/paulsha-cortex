"""#302（暫名）：registry 載入層的 claim_key 全域唯一性與 abandon→reclaim 語意矛盾。

`_manager_create_workflow_run` 允許 released（superseded＋planning_released）run 之後
以同 claim_key、attempt 鹽化 run_id 建新 run（#256 D4／#299）；但 `_load_state` 對
claim_key 做全域唯一性 fail-closed，重 claim 一旦 persist，manager 重啟即無法載回
自己的狀態檔。唯一性應只約束 ongoing runs；run_id 唯一性維持全域。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

CLAIM_KEY = "claim:v1:" + "c" * 64


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
        "work_id": "released-claimkey-load-work",
        "repo": "hamanpaul/paulsha-cortex",
        "claim_key": CLAIM_KEY,
        "source_revision": "rev-a",
        "workspace_root": "/tmp/workspace",
        "combo": "feature-oneshot",
        "current_phase": "define",
        "steps": (_step(),),
        "issue_refs": ("hamanpaul/paulsha-cortex#302",),
        "attempts": {"claim": 1},
        "facets": ("needs_human",),
    }
    fields.update(overrides)
    return registry._manager_create_workflow_run(**fields)


def test_reload_after_release_reclaim_cycle(tmp_path: Path) -> None:
    """abandon→reclaim persist 後，同一狀態檔必須能重新載入。"""
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    first = _create_run(registry)
    registry._manager_abandon_workflow_run(
        first.run_id, evidence_ref="evidence/work-abandon/reload-cycle.json"
    )
    second = _create_run(registry)
    assert second.run_id != first.run_id

    reloaded = JobRegistry(state_path=state_path)
    runs = {run.run_id: run for run in reloaded.list_workflow_runs()}
    assert set(runs) == {first.run_id, second.run_id}
    assert runs[first.run_id].status == "superseded"
    assert runs[second.run_id].status == "ongoing"


def test_duplicate_ongoing_claim_key_still_fail_closed(tmp_path: Path) -> None:
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    first = _create_run(registry)
    registry._manager_abandon_workflow_run(
        first.run_id, evidence_ref="evidence/work-abandon/dup-ongoing.json"
    )
    _create_run(registry)

    import json

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    for row in payload["workflows"]:
        if row["run_id"] == first.run_id:
            row["status"] = "ongoing"
            row["facets"] = []
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="重複識別"):
        JobRegistry(state_path=state_path)


def test_duplicate_run_id_still_fail_closed(tmp_path: Path) -> None:
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    run = _create_run(registry)

    import json

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    duplicate = dict(payload["workflows"][0])
    duplicate["claim_key"] = "claim:v1:" + "d" * 64
    payload["workflows"].append(duplicate)
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    assert duplicate["run_id"] == run.run_id
    with pytest.raises(ValueError, match="重複識別"):
        JobRegistry(state_path=state_path)
