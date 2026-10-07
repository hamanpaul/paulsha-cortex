from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, work_actions

from diagnostic_fixtures import fixture_needs_human_reason
from test_retry_build_archive_drift_1259 import _Fixture


def _seed_failed_repair_build(fixture: _Fixture):
    registry = fixture.registry
    run = registry.get_workflow_run(fixture.run.run_id)
    build_steps = [step for step in run.steps if step.phase == "build"]
    repair_card = build_steps[-1].card
    steps = tuple(
        replace(step, gate_result="passed")
        if step.phase == "build" and step.card != repair_card
        else replace(step, gate_result="pending")
        if step.phase == "build"
        else step
        for step in run.steps
    )
    run = replace(
        run,
        current_phase="build",
        steps=steps,
        attempts={**run.attempts, "build": 2},
        verified_head=None,
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=fixture_needs_human_reason(
            "builder-terminalization-failed", candidate=fixture.candidate
        ).to_dict(),
    )
    registry._workflows[registry._find_workflow_run_index(run.run_id)] = run
    registry._persist()
    failed_job = registry.create_job(
        task="wf-demo-subagent-build",
        persona="builder",
        branch="feature/12-demo",
        pane="",
        worktree=str(fixture.repo_root),
        dispatch_head=fixture.candidate,
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=repair_card,
        workflow_phase="build",
        workflow_repo_root=str(fixture.repo_root),
        workflow_input_root=str(fixture.repo_root),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(
        failed_job["job_id"], status="failed", exit_code=1
    )
    return registry.get_workflow_run(run.run_id), failed_job


def test_retry_build_recovers_killed_repair_with_existing_pr_and_advanced_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(tmp_path, monkeypatch, with_pr=True)
    run, failed_job = _seed_failed_repair_build(fixture)

    assert fixture.authority.mapped_prs == (77,)
    assert run.pr_refs == ("acme/demo#77",)
    assert work_actions.work_authority_digest(fixture.authority) != run.source_revision

    status = manager.workflow_status_entry(fixture.registry, run)
    assert "retry-build" in status["next_actions"]
    assert "resume" not in status["next_actions"]

    result = fixture.executor()(fixture.retry_build_request())

    dispatched = result["result"]["dispatch"]
    assert dispatched["kind"] == "job"
    after = fixture.registry.get_workflow_run(run.run_id)
    assert after.current_phase == "build"
    assert after.candidate_head == fixture.candidate
    assert after.attempts["build"] == 3
    assert "needs_human" not in after.facets
    jobs = fixture.registry.list_jobs()
    new_job = jobs[-1]
    assert new_job["job_id"] != failed_job["job_id"]
    assert new_job["status"] in {"dispatched", "running"}
    assert new_job["workflow_card"] == "subagent-build"
    assert new_job["dispatch_head"] == fixture.candidate
    assert fixture.launch_calls == ["codex/gpt-primary"]
    assert fixture.registry.get_job(failed_job["job_id"])["workflow_evidence"] is None

    [receipt] = fixture.receipts()
    assert manager._retry_build_receipt_candidate(
        after, coordinator_root=fixture.coordinator
    ) == fixture.candidate
    assert receipt.is_file()
