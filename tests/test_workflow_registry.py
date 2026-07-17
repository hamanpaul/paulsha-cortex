from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import registry as registry_module
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowRun, WorkflowStep


def _legacy_v1_payload() -> dict[str, object]:
    job = {"job_id": "legacy-build-1", "status": "exited", "task": "legacy-build"}
    slice_row = {
        "slice_id": "legacy-build",
        "spec": {"path": "specs/legacy-build.md", "hash": "spec-sha"},
        "plan": {"path": "plans/legacy-build.md", "hash": "plan-sha"},
        "target_branch": "main",
        "target_remote": "origin",
        "dispatch_base": "base-sha",
        "builder_job_id": "legacy-build-1",
        "reviewer_job_id": None,
        "candidate": "candidate-sha",
        "state": "completed",
        "gate_state": "passed",
        "verification": {"hash": "verification-sha"},
        "current_evidence_refs": ["evidence.json"],
        "current_evaluation_refs": ["gate.json"],
        "evidence_history": [],
        "evaluation_history": [],
        "actions": [],
        "created_at": "2026-07-17T00:00:00+00:00",
        "updated_at": "2026-07-17T00:00:00+00:00",
    }
    return {
        "schema_version": 1,
        "seq": 1,
        "jobs": [job],
        "slices": [slice_row],
    }


def _step() -> WorkflowStep:
    return WorkflowStep(
        phase="plan",
        persona="planner",
        card="writing-plans",
        executor="agy",
        model="gemini-3.1-pro-high",
        domain="google",
        inputs=("openspec/changes/demo/proposal.md",),
        outputs=("docs/superpowers/plans/demo.md",),
        gate_result="pending",
    )


def _create_run(registry: JobRegistry) -> WorkflowRun:
    return registry._manager_create_workflow_run(
        work_id="unified-work-lifecycle",
        repo="hamanpaul/paulsha-cortex",
        claim_key="hamanpaul/paulsha-cortex/unified-work-lifecycle/rev-a",
        combo="feature-oneshot",
        current_phase="plan",
        steps=(_step(),),
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("unified-work-lifecycle",),
        pr_refs=(),
        attempts={"plan": 1},
        evidence_refs=("evidence/question-pack.json",),
        facets=("needs_human",),
        gate_status="pending",
    )


def test_v1_migration_creates_immutable_backup_and_isolates_legacy_records(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    original = json.dumps(_legacy_v1_payload(), ensure_ascii=False, indent=2).encode("utf-8")
    state.write_bytes(original)

    registry = JobRegistry(state_path=state)

    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["jobs"] == []
    assert payload["slices"] == []
    assert payload["workflows"] == []
    assert payload["legacy_records"]["source_schema_version"] == 1
    assert payload["legacy_records"]["jobs"] == _legacy_v1_payload()["jobs"]
    assert payload["legacy_records"]["slices"] == _legacy_v1_payload()["slices"]
    assert registry.list_jobs() == []
    assert registry.list_slices() == []
    assert registry.list_workflow_runs() == []

    legacy = registry.list_legacy_records()
    for record in legacy["jobs"] + legacy["slices"]:
        assert "work_id" not in record
        assert "workflow_run_id" not in record

    digest = hashlib.sha256(original).hexdigest()
    backups = list(tmp_path.glob(f"jobs.json.v1.*.{digest}.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert backups[0].stat().st_mode & 0o222 == 0


def test_malformed_v1_rejected_without_backup_or_rewrite(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    original = b'{"schema_version": 1, "seq": 0, "jobs": "bad", "slices": []}'
    state.write_bytes(original)

    with pytest.raises(ValueError, match="格式錯誤"):
        JobRegistry(state_path=state)

    assert state.read_bytes() == original
    assert list(tmp_path.glob("jobs.json.v1.*.bak")) == []


def test_backup_failure_leaves_v1_state_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "jobs.json"
    original = json.dumps(_legacy_v1_payload(), ensure_ascii=False, indent=2).encode("utf-8")
    state.write_bytes(original)

    def fail_link(_source: object, _target: object) -> None:
        raise OSError("backup unavailable")

    monkeypatch.setattr(registry_module.os, "link", fail_link)
    with pytest.raises(OSError, match="backup unavailable"):
        JobRegistry(state_path=state)

    assert state.read_bytes() == original


def test_workflow_run_persists_all_fields_and_claim_is_restart_idempotent(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    created = _create_run(registry)

    assert created.work_id == "unified-work-lifecycle"
    assert created.combo == "feature-oneshot"
    assert created.current_phase == "plan"
    assert created.steps == (_step(),)
    assert created.issue_refs == ("hamanpaul/paulsha-cortex#14",)
    assert created.openspec_refs == ("unified-work-lifecycle",)
    assert created.pr_refs == ()
    assert created.attempts == {"plan": 1}
    assert created.evidence_refs == ("evidence/question-pack.json",)
    assert created.facets == ("needs_human",)
    assert created.gate_status == "pending"

    restarted = JobRegistry(state_path=state)
    duplicate = _create_run(restarted)
    assert duplicate == created
    assert restarted.list_workflow_runs() == [created]
    assert len(json.loads(state.read_text(encoding="utf-8"))["workflows"]) == 1


def test_workflow_run_update_is_typed_persisted_and_rejects_phase_regression(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    created = _create_run(registry)

    updated = registry._manager_update_workflow_run(
        created.run_id,
        current_phase="build",
        attempts={"plan": 1, "build": 2},
        evidence_refs=("evidence/question-pack.json", "evidence/build.json"),
        facets=(),
        gate_status="running",
    )
    assert updated.current_phase == "build"
    assert updated.attempts["build"] == 2
    assert updated.gate_status == "running"
    assert JobRegistry(state_path=state).get_workflow_run(created.run_id) == updated

    with pytest.raises(ValueError, match="phase transition"):
        registry._manager_update_workflow_run(created.run_id, current_phase="plan")


def test_malformed_v2_workflow_rejected_without_rewrite(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    original = json.dumps(
        {
            "schema_version": 2,
            "seq": 0,
            "jobs": [],
            "slices": [],
            "workflows": [{"run_id": "incomplete"}],
            "legacy_records": {"source_schema_version": 1, "seq": 0, "jobs": [], "slices": []},
        },
        ensure_ascii=False,
        indent=2,
    )
    state.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="workflow"):
        JobRegistry(state_path=state)

    assert state.read_text(encoding="utf-8") == original


def test_v2_missing_required_root_fields_is_rejected_without_rewrite(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    original = '{"schema_version": 2, "seq": 0, "jobs": [], "slices": []}'
    state.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="workflows.*legacy_records"):
        JobRegistry(state_path=state)

    assert state.read_text(encoding="utf-8") == original


def test_v2_atomic_write_failure_rolls_back_memory_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    registry.create_job(
        task="baseline",
        persona="builder",
        branch="feature/baseline",
        pane="%0",
        worktree="/wt/baseline",
    )
    original = state.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(registry_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        _create_run(registry)

    assert state.read_bytes() == original
    assert registry.list_workflow_runs() == []
    assert not any(path.suffix == ".tmp" for path in tmp_path.iterdir())
