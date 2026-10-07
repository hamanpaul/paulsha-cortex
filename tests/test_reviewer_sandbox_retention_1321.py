"""#1321: reviewer sandboxes stay available until retry dispatch or adoption."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, planning_runtime
from paulsha_cortex.coordinator.model_identities import IdentityRegistry

from test_reviewer_card_retry_569 import _stuck_reviewer_run


class _FailingReviewLauncher:
    def as_review_only(self, *, terminal_kind: str):
        return self

    def launch(self, **_kwargs):
        raise RuntimeError("injected reviewer dispatch failure")


def _reviewer_recovery_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _snapshot, registry, run, job_id = _stuck_reviewer_run(tmp_path, with_git=True)
    coordinator_root = tmp_path / "coordinator"
    candidate_root = Path(run.workspace_root)
    step = manager._current_workflow_step(run)
    assert step is not None
    monkeypatch.setattr(manager, "_grant_reviewer_sandbox_access", lambda _sandbox: None)

    builder = next(job for job in registry.list_jobs() if job.get("persona") == "builder")
    registry._find_job(str(builder["job_id"]))["worktree"] = str(candidate_root)
    registry._persist()

    effective_inputs = manager._effective_workflow_inputs(run, step)
    input_patterns = manager._reviewer_input_patterns(run, effective_inputs)
    input_snapshot = manager._workflow_input_snapshot(
        run=run,
        repo_root=candidate_root,
        patterns=input_patterns,
        coordinator_root=coordinator_root,
    )
    sandbox, checkout = manager._create_reviewer_sandbox(
        run=run,
        step=step,
        executor="agy",
        candidate_root=candidate_root,
        coordinator_root=coordinator_root,
        input_snapshot=input_snapshot,
        job_id=job_id,
    )

    old_job = registry._find_job(job_id)
    old_job.update(
        {
            "worktree": str(sandbox),
            "workflow_repo_root": str(candidate_root.resolve()),
            "workflow_input_root": str(checkout),
            "workflow_inputs": list(effective_inputs),
            "workflow_input_snapshot": list(input_snapshot),
            "workflow_outputs": list(step.outputs),
            "workflow_output_baseline": list(
                manager._workflow_output_baseline(candidate_root, step.outputs)
            ),
            "workflow_sandbox_hash": planning_runtime._tree_snapshot(candidate_root),
        }
    )
    registry._persist()
    log_path = coordinator_root / "logs" / "workflow" / f"{job_id}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("reviewer exited without terminal JSON\n", encoding="utf-8")
    old_job["log_path"] = str(log_path)
    old_job["executor"] = "agy"
    old_job["model_id"] = "gemini-3.7-flash-high"
    old_job["independence_domain"] = "google"
    registry._persist()
    registry.update_headless_result(job_id, status="exited", exit_code=0)
    return registry, run, job_id, coordinator_root, sandbox, candidate_root


def test_resume_dispatch_failure_keeps_terminal_sandbox_for_next_harvest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, run, job_id, coordinator_root, sandbox, _candidate_root = (
        _reviewer_recovery_fixture(tmp_path, monkeypatch)
    )
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "agy",
                "model_id": "gemini-3.7-flash-high",
                "independence_domain": "google",
                "capabilities": ["review"],
            },
        ]
    )
    monkeypatch.setattr(
        manager,
        "_validated_brainstorm_planning_authority",
        lambda bound_run, **_kwargs: (
            bound_run.planning_authority,
            bound_run.planning_source_revision,
        ),
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    with pytest.raises(RuntimeError, match="injected reviewer dispatch failure"):
        manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=identities,
            launcher_factory=lambda _identity: _FailingReviewLauncher(),
            coordinator_root=coordinator_root,
            operator_resume=True,
        )

    with pytest.raises(ValueError) as harvest_error:
        manager.terminalize_workflow_job(
            registry,
            job_id=job_id,
            coordinator_root=coordinator_root,
        )

    assert str(harvest_error.value) != "workflow input snapshot file missing"
    assert sandbox.is_dir(), f"retry sandbox was removed; next harvest failed: {harvest_error.value}"
    assert (sandbox / "docs/superpowers/plans/reviewer-retry.md").is_file()
    retained_job = registry.get_job(job_id)
    manager._validate_workflow_input_snapshot(
        Path(str(retained_job["workflow_input_root"])),
        retained_job["workflow_input_snapshot"],
        coordinator_root=coordinator_root,
    )


def test_terminal_adoption_discards_sandbox_after_evidence_is_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, run, job_id, coordinator_root, sandbox, _candidate_root = (
        _reviewer_recovery_fixture(tmp_path, monkeypatch)
    )
    step = manager._current_workflow_step(run)
    assert step is not None
    output_refs = [pattern.replace("*", "reviewer-retry") for pattern in step.outputs]
    Path(str(registry.get_job(job_id)["log_path"])).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-verification-result",
                "status": "verified",
                "summary": "verification passed",
                "details": {},
                "reports": [
                    {"path": ref, "body": "# Verification\n\nPassed."}
                    for ref in output_refs
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    adopted = manager.terminalize_workflow_job(
        registry,
        job_id=job_id,
        coordinator_root=coordinator_root,
    )

    assert adopted["workflow_evidence"] is not None
    assert not sandbox.exists()


def test_missing_reviewer_sandbox_has_explicit_recovery_reason_and_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, run, job_id, coordinator_root, sandbox, _candidate_root = (
        _reviewer_recovery_fixture(tmp_path, monkeypatch)
    )
    shutil.rmtree(sandbox)
    step = manager._current_workflow_step(run)
    assert step is not None
    output_refs = [pattern.replace("*", "reviewer-retry") for pattern in step.outputs]
    Path(str(registry.get_job(job_id)["log_path"])).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-verification-result",
                "status": "verified",
                "summary": "verification passed",
                "details": {},
                "reports": [
                    {"path": ref, "body": "# Verification\n\nPassed."}
                    for ref in output_refs
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run = registry._manager_update_workflow_run(
        run.run_id,
        facets=(),
        gate_status="running",
    )
    monkeypatch.setattr(
        manager,
        "_validated_brainstorm_planning_authority",
        lambda bound_run, **_kwargs: (
            bound_run.planning_authority,
            bound_run.planning_source_revision,
        ),
    )

    class _ResumeDispatcher:
        _registry = registry
        _git_runner = None

        def poll_headless_done(self, requested_job_id: str):
            assert requested_job_id == job_id
            return registry.get_job(requested_job_id)

    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "agy",
                "model_id": "gemini-3.7-flash-high",
                "independence_domain": "google",
                "capabilities": ["review"],
            },
        ]
    )

    with pytest.raises(ValueError, match="reviewer-sandbox-discarded-before-adoption"):
        manager.resume_workflow_run(
            _ResumeDispatcher(),
            run_id=run.run_id,
            identities=identities,
            launcher_factory=lambda _identity: _FailingReviewLauncher(),
            coordinator_root=coordinator_root,
        )

    persisted = registry.get_workflow_run(run.run_id)
    reason = dict(persisted.needs_human_reason)
    status = manager.workflow_status_entry(
        registry,
        persisted,
        work_authority_state="available",
    )
    assert reason["reason"] == "reviewer-sandbox-discarded-before-adoption"
    assert "retry-card" in status["next_actions"]
