from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.registry import stage_execution_receipt
import test_recovery_action_rechain_1173 as fixture


def _seed_job(registry, run, *, status="exited", with_evidence=True):
    artifact = Path(run.workspace_root) / "old-attempt.log"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"old attempt output\n")
    receipt = stage_execution_receipt(
        repo=run.repo, work_id=run.work_id, run_id=run.run_id,
        claim_key=run.claim_key, card="implement", phase="build",
        executor="claude", model="sonnet", base_sha="a" * 40,
        candidate_sha=fixture.CANDIDATE, frozen_input_hashes=(),
        action="implement", test_policy="focused", execution_profile_key="profile-v1",
    )
    registry.reserve_job_id("demo")
    job = registry.create_job(
        task="demo", persona="builder", branch="feature/demo", pane="",
        worktree=str(Path(run.workspace_root) / "build"), kind="build",
        workflow_run_id=run.run_id, workflow_claim_key=run.claim_key,
        workflow_repo=run.repo, workflow_card="implement", workflow_phase="build",
        source_revision=run.source_revision,
        log_path=str(artifact),
        workflow_stage_execution_key=receipt["key"],
        workflow_stage_execution_receipt=receipt,
    )
    if status == "running":
        registry.update_status(job["job_id"], "running")
    else:
        registry.update_headless_result(
            job["job_id"], status=status,
            exit_code=0 if status == "exited" else 1,
        )
        if with_evidence:
            registry.bind_workflow_evidence(
                job["job_id"],
                locator={"kind": "workflow-test", "path": str(artifact), "hash": "f" * 64},
                subject_head=fixture.CANDIDATE,
            )
    return registry.get_job(job["job_id"])


def _args(run, job, **updates):
    return {
        "action": "supersede-attempt", "repo": fixture.REPO,
        "work_id": fixture.WORK_ID, "actor": "operator",
        "reason": "quota forecast was contradicted by measured external usage",
        "expected_run_id": run.run_id,
        "expected_candidate": fixture.CANDIDATE,
        "expected_era": fixture.ERA,
        "expected_job_id": job["job_id"],
        "card": "implement", **updates,
    }


def test_supersede_attempt_rejects_active_job_without_side_effects(tmp_path):
    registry, run = fixture._registry(tmp_path)
    job = _seed_job(registry, run, status="running")
    before_run = registry.get_workflow_run(run.run_id).to_dict()
    before_job = registry.get_job(job["job_id"])
    before_audit = sorted((tmp_path / "evidence").glob("**/*")) if (tmp_path / "evidence").exists() else []

    with pytest.raises(RuntimeError, match="active workflow job"):
        work_actions._supersede_attempt_action(
            args=_args(run, job), authority=fixture._authority(),
            state_path=tmp_path / "state.json", workflow_registry=registry,
        )

    assert registry.get_workflow_run(run.run_id).to_dict() == before_run
    assert registry.get_job(job["job_id"]) == before_job
    after_audit = sorted((tmp_path / "evidence").glob("**/*")) if (tmp_path / "evidence").exists() else []
    assert after_audit == before_audit


def test_supersede_attempt_requires_exact_run_candidate_era_and_job(tmp_path):
    registry, run = fixture._registry(tmp_path)
    job = _seed_job(registry, run)
    for field, value in (("expected_candidate", "f" * 40), ("expected_era", "claim:v1:" + "d" * 64), ("expected_job_id", "demo-999")):
        before = registry.get_workflow_run(run.run_id).to_dict()
        with pytest.raises(RuntimeError, match="CAS mismatch"):
            work_actions._supersede_attempt_action(
                args=_args(run, job, **{field: value}), authority=fixture._authority(),
                state_path=tmp_path / "state.json", workflow_registry=registry,
            )
        assert registry.get_workflow_run(run.run_id).to_dict() == before
        assert registry.get_job(job["job_id"]) == job


def test_supersede_attempt_preserves_old_job_receipt_and_replay_is_idempotent(tmp_path):
    registry, run = fixture._registry(tmp_path)
    accepted_job = _seed_job(registry, run)
    accepted_bytes = json.dumps(registry.get_job(accepted_job["job_id"]), sort_keys=True)
    job = _seed_job(registry, run, status="failed", with_evidence=False)
    original_job_bytes = json.dumps(registry.get_job(job["job_id"]), sort_keys=True)
    args = _args(run, job)
    first = work_actions._supersede_attempt_action(
        args=args, authority=fixture._authority(), state_path=tmp_path / "state.json",
        workflow_registry=registry,
    )
    after_first_job = json.dumps(registry.get_job(job["job_id"]), sort_keys=True)
    fresh = type(registry)(state_path=tmp_path / "jobs.json")
    second = work_actions._supersede_attempt_action(
        args=args, authority=fixture._authority(), state_path=tmp_path / "state.json",
        workflow_registry=fresh,
    )

    assert first["action"] == "supersede-attempt"
    assert first["already_applied"] is False
    assert second["already_applied"] is True
    assert json.dumps(fresh.get_job(job["job_id"]), sort_keys=True) == original_job_bytes == after_first_job
    assert first["evidence"] == second["evidence"]
    assert first["evidence"]["ref"] in fresh.get_workflow_run(run.run_id).evidence_refs
    assert len(list((tmp_path / "evidence" / "work-attempt-supersession").glob("*.json"))) == 1
    assert Path(fresh.get_job(job["job_id"])["log_path"]).read_bytes() == b"old attempt output\n"
    assert json.dumps(fresh.get_job(accepted_job["job_id"]), sort_keys=True) == accepted_bytes


def test_supersede_attempt_replays_audit_after_crash_before_registry_commit(tmp_path, monkeypatch):
    registry, run = fixture._registry(tmp_path)
    _seed_job(registry, run)
    job = _seed_job(registry, run, status="failed", with_evidence=False)
    args = _args(run, job)
    monkeypatch.setattr(registry, "_persist", lambda: (_ for _ in ()).throw(RuntimeError("crash before registry commit")))

    with pytest.raises(RuntimeError, match="crash before registry commit"):
        work_actions._supersede_attempt_action(
            args=args, authority=fixture._authority(),
            state_path=tmp_path / "state.json", workflow_registry=registry,
        )
    fresh = type(registry)(state_path=tmp_path / "jobs.json")
    assert "needs_human" in fresh.get_workflow_run(run.run_id).facets
    replay = work_actions._supersede_attempt_action(
        args=args, authority=fixture._authority(),
        state_path=tmp_path / "state.json", workflow_registry=fresh,
    )
    assert replay["already_applied"] is False
    assert replay["evidence"]["ref"] in fresh.get_workflow_run(run.run_id).evidence_refs
    assert len(list((tmp_path / "evidence" / "work-attempt-supersession").glob("*.json"))) == 1
