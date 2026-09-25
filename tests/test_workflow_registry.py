from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import registry as registry_module
from paulsha_cortex.coordinator.registry import JobRegistry, RegistryRevisionConflict
from paulsha_cortex.coordinator.workflow import (
    PlanningArtifactAuthority,
    SHIP_TRANSITION_STAGES,
    WorkflowRun,
    WorkflowStep,
    validate_ship_stage_transition,
)

from diagnostic_fixtures import fixture_needs_human_reason


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
        source_revision="rev-a",
        workspace_root="/tmp/paulsha-cortex",
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
        planning_authority=(
            PlanningArtifactAuthority(
                ref="docs/superpowers/plans/unified-work-lifecycle.md",
                kind="plan",
                work_id="unified-work-lifecycle",
                baseline_sha256="a" * 64,
            ),
        ),
        needs_human_reason=fixture_needs_human_reason(),
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
    assert created.planning_source_revision == "rev-a"
    assert created.planning_authority == (
        PlanningArtifactAuthority(
            ref="docs/superpowers/plans/unified-work-lifecycle.md",
            kind="plan",
            work_id="unified-work-lifecycle",
            baseline_sha256="a" * 64,
        ),
    )

    restarted = JobRegistry(state_path=state)
    duplicate = _create_run(restarted)
    assert duplicate == created
    assert restarted.list_workflow_runs() == [created]
    assert len(json.loads(state.read_text(encoding="utf-8"))["workflows"]) == 1

    legacy_payload = created.to_dict()
    legacy_payload.pop("planning_source_revision")
    assert WorkflowRun.from_dict(legacy_payload).planning_source_revision is None


def test_workflow_run_update_is_typed_persisted_and_rejects_phase_regression(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    created = _create_run(registry)

    updated = registry._manager_update_workflow_run(
        created.run_id,
        current_phase="build",
        source_revision="rev-b",
        attempts={"plan": 1, "build": 2},
        evidence_refs=("evidence/question-pack.json", "evidence/build.json"),
        facets=(),
        gate_status="running",
    )
    assert updated.current_phase == "build"
    assert updated.source_revision == "rev-b"
    assert updated.planning_source_revision == "rev-a"
    assert updated.attempts["build"] == 2
    assert updated.gate_status == "running"
    assert JobRegistry(state_path=state).get_workflow_run(created.run_id) == updated

    with pytest.raises(ValueError, match="phase transition"):
        registry._manager_update_workflow_run(created.run_id, current_phase="plan")


@pytest.mark.parametrize(
    ("current", "new"),
    [
        ("local-closeout", "local-closeout"),
        ("local-closeout", "pr-preflight"),
        ("pr-preflight", "external-ship"),
    ],
)
def test_ship_stage_transition_accepts_monotonic_progress(current: str, new: str) -> None:
    assert SHIP_TRANSITION_STAGES == ("local-closeout", "pr-preflight", "external-ship")
    validate_ship_stage_transition(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        ("pr-preflight", "local-closeout"),
        ("external-ship", "pr-preflight"),
        ("local-closeout", "external-ship"),
        ("bogus", "local-closeout"),
    ],
)
def test_ship_stage_transition_rejects_regression_and_invalid_values(current: str, new: str) -> None:
    with pytest.raises(ValueError, match="ship transition stage"):
        validate_ship_stage_transition(current, new)


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


def test_terminal_workflow_evidence_locator_is_single_assignment_and_durable(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    job = registry.create_job(
        task="workflow-card", persona="builder", branch="feature/work", pane="",
        worktree=str(tmp_path), exit_code=0, workflow_run_id="workflow-1",
        workflow_claim_key="repo/work/rev", workflow_repo="owner/repo",
        workflow_card="build", workflow_phase="build", workflow_repo_root=str(tmp_path),
        source_revision="rev",
        workflow_output_baseline=(
            {"path": "reports/build.md", "sha256": "d" * 64},
        ),
    )
    registry.update_status(str(job["job_id"]), "exited")
    locator = {"kind": "build", "path": "evidence/workflow/card.json", "hash": "a" * 64}

    registry.bind_workflow_evidence(str(job["job_id"]), locator=locator, subject_head="b" * 40)
    restored = JobRegistry(state_path=state).get_job(str(job["job_id"]))
    assert restored["workflow_evidence"] == locator
    assert restored["subject_head"] == "b" * 40
    assert restored["workflow_output_baseline"] == [
        {"path": "reports/build.md", "sha256": "d" * 64}
    ]

    restored["workflow_output_baseline"][0]["sha256"] = "e" * 64
    assert registry.get_job(str(job["job_id"]))["workflow_output_baseline"][0]["sha256"] == "d" * 64

    with pytest.raises(ValueError, match="衝突"):
        registry.bind_workflow_evidence(
            str(job["job_id"]),
            locator={**locator, "hash": "c" * 64},
            subject_head="b" * 40,
        )


def test_conflict_restores_latest_durable_snapshot_and_fields(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    registry.create_job(
        task="slice-a",
        persona="builder",
        branch="feature/slice-a",
        pane="%0",
        worktree="/wt/slice-a",
    )
    registry.create_slice(
        slice_id="slice-a",
        spec_path="specs/slice-a.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-a.md",
        plan_hash="plan-sha",
        target_branch="main",
        dispatch_base="base-sha",
        builder_job_id=None,
        reviewer_job_id=None,
        candidate=None,
    )
    workflow = _create_run(registry)

    stale = JobRegistry(state_path=state)
    expected_revision = hashlib.sha256(state.read_bytes()).hexdigest()

    fresh = JobRegistry(state_path=state)
    fresh.record_action(
        "slice-a",
        action="builder-started",
        actor="builder",
        state="running",
    )
    actual_revision = hashlib.sha256(state.read_bytes()).hexdigest()

    with pytest.raises(RegistryRevisionConflict) as excinfo:
        stale.record_action(
            "slice-a",
            action="operator-abandon",
            actor="operator",
            state="failed",
            gate_state="failed",
        )

    restored = JobRegistry(state_path=state)
    conflict = excinfo.value
    assert conflict.expected_revision == expected_revision
    assert conflict.actual_revision == actual_revision
    assert conflict.state_path == stale.canonical_state_path
    assert stale.list_jobs() == restored.list_jobs()
    assert stale.list_slices() == restored.list_slices()
    assert stale.list_workflow_runs() == restored.list_workflow_runs()
    assert stale.list_legacy_records() == restored.list_legacy_records()
    assert stale.list_reclaim_resets() == restored.list_reclaim_resets()
    assert stale.get_workflow_run(workflow.run_id) == restored.get_workflow_run(workflow.run_id)
    assert stale._seq == restored._seq
    assert stale._loaded_revision == actual_revision


def test_canonical_state_path_collapses_symlinked_directory_spellings(tmp_path: Path) -> None:
    actual_dir = tmp_path / "actual"
    actual_dir.mkdir()
    alias_dir = tmp_path / "alias"
    alias_dir.symlink_to(actual_dir, target_is_directory=True)

    direct = JobRegistry(state_path=actual_dir / "jobs.json")
    aliased = JobRegistry(state_path=alias_dir / "jobs.json")
    aliased.create_slice(
        slice_id="slice-a",
        spec_path="specs/slice-a.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-a.md",
        plan_hash="plan-sha",
        target_branch="main",
        dispatch_base="base-sha",
        builder_job_id=None,
        reviewer_job_id=None,
        candidate=None,
    )

    assert direct.canonical_state_path == actual_dir / "jobs.json"
    assert aliased.canonical_state_path == direct.canonical_state_path
    assert aliased.state_transaction_lock_path == actual_dir / "jobs.json.transaction.lock"
    assert aliased.state_transaction_lock_path.read_bytes() == b""


def test_canonical_state_path_expands_home_spellings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    registry = JobRegistry(state_path="~/jobs.json")

    assert registry.canonical_state_path == tmp_path.resolve() / "jobs.json"
    assert registry.state_transaction_lock_path == (
        tmp_path.resolve() / "jobs.json.transaction.lock"
    )


def test_state_file_symlink_replacement_keeps_target_unchanged(tmp_path: Path) -> None:
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target_state = target_dir / "jobs.json"
    seeded = JobRegistry(state_path=target_state)
    seeded.create_slice(
        slice_id="slice-a",
        spec_path="specs/slice-a.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-a.md",
        plan_hash="plan-sha",
        target_branch="main",
        dispatch_base="base-sha",
        builder_job_id=None,
        reviewer_job_id=None,
        candidate=None,
    )
    original_target_bytes = target_state.read_bytes()

    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link_state = link_dir / "jobs-link.json"
    link_state.symlink_to(target_state)

    registry = JobRegistry(state_path=link_state)
    registry.record_action(
        "slice-a",
        action="via-symlink",
        actor="builder",
        state="running",
    )

    assert registry.canonical_state_path == link_dir.resolve() / "jobs-link.json"
    assert registry.state_transaction_lock_path == (
        link_dir.resolve() / "jobs-link.json.transaction.lock"
    )
    assert registry.state_transaction_lock_path.read_bytes() == b""
    assert not link_state.is_symlink()
    assert target_state.read_bytes() == original_target_bytes

    payload = json.loads(link_state.read_text(encoding="utf-8"))
    assert payload["slices"][0]["actions"][0]["action"] == "via-symlink"
    assert payload["slices"][0]["state"] == "running"


def test_stale_v1_migration_has_no_backup_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "jobs.json"
    original = json.dumps(_legacy_v1_payload(), ensure_ascii=False, indent=2).encode("utf-8")
    state.write_bytes(original)
    replacement_payload = {
        "schema_version": 2,
        "seq": 7,
        "jobs": [],
        "slices": [],
        "workflows": [],
        "legacy_records": {"source_schema_version": 1, "seq": 0, "jobs": [], "slices": []},
        "reclaim_resets": [],
    }
    replacement_bytes = json.dumps(
        replacement_payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")

    original_read = registry_module.JobRegistry._read_durable_snapshot
    call_count = 0

    def race_read(self):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            state.write_bytes(replacement_bytes)
        return original_read(self)

    monkeypatch.setattr(registry_module.JobRegistry, "_read_durable_snapshot", race_read)
    registry = JobRegistry(state_path=state)

    assert state.read_bytes() == replacement_bytes
    assert list(tmp_path.glob("jobs.json.v1.*.bak")) == []
    assert registry.list_jobs() == []
    assert registry.list_slices() == []
    assert registry.list_legacy_records() == replacement_payload["legacy_records"]
    assert registry._loaded_revision == hashlib.sha256(replacement_bytes).hexdigest()
