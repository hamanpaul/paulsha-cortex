from __future__ import annotations

import json
import subprocess
from dataclasses import replace

import pytest

from paulsha_cortex.coordinator import manager, work_actions, work_bridge

from test_main_probe_gate_987 import (
    _advance_origin_main,
    _git,
    _resume,
    _seed_foreign_review,
    _ship_harness,
    _wire_local_origin,
)


def _main_sync_stop(tmp_path, monkeypatch):
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    origin = _wire_local_origin(harness, tmp_path / "origin-fixture")
    main_head = _advance_origin_main(origin, tmp_path / "origin-advance")
    result = _resume(harness)
    assert result["reason"] == "candidate-behind-main"
    return harness, origin, main_head


def _retry_build(harness):
    return work_actions.execute_work_action(
        args={
            "action": "retry-build",
            "repo": "acme/demo",
            "work_id": "work",
            "expected_candidate": harness.candidate,
            "actor": "operator",
            "reason": "依 stop 時的 main-sync evidence 修復候選",
        },
        requested_by="operator",
        snapshot_path=harness.snapshot,
        state_path=harness.state_root / "runs.json",
        workflow_registry=harness.registry,
    )


def test_main_sync_stop_status_exposes_accepted_retry_build_with_instructions(
    tmp_path, monkeypatch
):
    harness, _origin, main_head = _main_sync_stop(tmp_path, monkeypatch)
    run = harness.run
    evidence_ref = run.needs_human_reason["evidence_refs"][0]

    status = manager.workflow_status_entry(harness.registry, run)

    assert "retry-build" in status["next_actions"]
    assert f"cortex run work retry-build work --repo acme/demo --expected-candidate {harness.candidate}" in status["next_step_hint"]
    assert evidence_ref in status["next_step_hint"]
    assert main_head in status["next_step_hint"]

    _retry_build(harness)

    reset = harness.run
    assert reset.current_phase == "build"
    assert "needs_human" not in reset.facets
    build_steps = [step for step in reset.steps if step.phase == "build"]
    assert "main-sync probe evidence" in str(build_steps[-1].action)
    assert evidence_ref in str(build_steps[-1].action)
    assert main_head in str(build_steps[-1].action)
    assert all(
        step.gate_result == "pending"
        for step in reset.steps
        if step.phase in {"verify", "review"}
    )


@pytest.mark.parametrize(
    "invalid_precondition",
    ["build-not-passed", "candidate-context-mismatch", "active-job", "wrong-ship-step"],
)
def test_main_sync_retry_build_is_hidden_and_rejected_without_reset_when_ineligible(
    tmp_path, monkeypatch, invalid_precondition
):
    harness, _origin, _main_head = _main_sync_stop(tmp_path, monkeypatch)
    run = harness.run

    if invalid_precondition == "build-not-passed":
        steps = tuple(
            replace(step, gate_result="failed") if step.phase == "build" else step
            for step in run.steps
        )
        harness.registry._manager_update_workflow_run(run.run_id, steps=steps)
    elif invalid_precondition == "candidate-context-mismatch":
        reason = dict(run.needs_human_reason)
        context = dict(reason["context"])
        main_sync = json.loads(context["main_sync"])
        main_sync["candidate"] = "e" * 40
        context["main_sync"] = json.dumps(main_sync, sort_keys=True, separators=(",", ":"))
        reason["context"] = context
        harness.registry._manager_update_workflow_run(
            run.run_id, needs_human_reason=reason
        )
    elif invalid_precondition == "active-job":
        harness.registry.create_job(
            task="wf-active-builder",
            persona="builder",
            kind="build",
            branch="feature/14-work",
            pane="",
            worktree=str(harness.repo),
            executor="codex",
            model_id="gpt",
            independence_domain="openai",
            subject_head=harness.candidate,
            workflow_run_id=run.run_id,
            workflow_claim_key=run.claim_key,
            workflow_repo=run.repo,
            workflow_card="build-card",
            workflow_phase="build",
            workflow_repo_root=str(harness.repo),
            source_revision=run.source_revision,
        )
    elif invalid_precondition == "wrong-ship-step":
        steps = tuple(
            replace(
                step,
                gate_result="passed",
                executor="cortex-manager",
                model="deterministic",
                domain="cortex",
            )
            if step.phase == "ship"
            else step
            for step in run.steps
        )
        harness.registry._manager_update_workflow_run(run.run_id, steps=steps)

    before = harness.run.to_dict()
    status = manager.workflow_status_entry(harness.registry, harness.run)
    assert "retry-build" not in status["next_actions"]

    with pytest.raises((RuntimeError, ValueError)):
        _retry_build(harness)

    assert harness.run.to_dict() == before


def test_bare_origin_main_sync_retry_rechecks_repaired_candidate_before_ship(
    tmp_path, monkeypatch
):
    harness, _origin, main_head = _main_sync_stop(tmp_path, monkeypatch)
    original_status = manager.workflow_status_entry(harness.registry, harness.run)
    assert "retry-build" in original_status["next_actions"]

    _retry_build(harness)
    reset = harness.run
    assert reset.current_phase == "build"
    assert all(
        step.gate_result == "pending"
        for step in reset.steps
        if step.phase in {"verify", "review"}
    )

    _git(harness.repo, "checkout", "feature/14-work")
    _git(harness.repo, "fetch", "origin", "main")
    _git(harness.repo, "merge", "--no-ff", "--no-edit", "FETCH_HEAD")
    repaired_candidate = _git(harness.repo, "rev-parse", "HEAD")
    _git(harness.repo, "checkout", "main")
    build_passed = tuple(
        replace(step, gate_result="passed")
        if step.phase == "build"
        else step
        for step in reset.steps
    )
    harness.registry._manager_update_workflow_run(
        reset.run_id,
        candidate_head=repaired_candidate,
        steps=build_passed,
    )
    harness.registry._manager_update_workflow_run(
        reset.run_id,
        current_phase="verify",
    )
    verify_passed = tuple(
        replace(step, gate_result="passed") if step.phase == "verify" else step
        for step in build_passed
    )
    harness.registry._manager_update_workflow_run(
        reset.run_id,
        verified_head=repaired_candidate,
        steps=verify_passed,
    )
    review_passed = tuple(
        replace(step, gate_result="passed") if step.phase == "review" else step
        for step in verify_passed
    )
    repaired_run = harness.registry._manager_update_workflow_run(
        reset.run_id,
        current_phase="review",
        steps=review_passed,
        facets=(),
        needs_human_reason=None,
        gate_refs=(),
    )
    _seed_foreign_review(
        registry=harness.registry,
        run=repaired_run,
        repo=harness.repo,
        candidate=repaired_candidate,
        state_root=harness.state_root,
        worktree=harness.repo,
    )
    observed = []
    original_probe = work_bridge._probe_main_sync

    def recording_probe(**kwargs):
        result = original_probe(**kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(work_bridge, "_probe_main_sync", recording_probe)
    outcome = harness.validator(run=harness.run, candidate=repaired_candidate)

    assert outcome["status"] == "pending"
    assert len(observed) == 2
    assert all(probe.relation == "in-sync" for probe in observed)
    assert all(probe.main_head == main_head and probe.merge_base == main_head for probe in observed)
    assert any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert harness.runner.saw_push()
