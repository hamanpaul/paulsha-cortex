from __future__ import annotations

import json
from types import SimpleNamespace

from paulsha_cortex.coordinator import cli, manager, manager_daemon
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


REPO = "example/cortex"
CANDIDATE = "a" * 40
RECOVERED_ERROR = "API error (attempt 1): request failed (code 502)"


def _step(phase: str, card: str, gate_result: str) -> WorkflowStep:
    persona = "builder" if phase == "build" else "reviewer"
    return WorkflowStep(
        phase=phase,
        persona=persona,
        card=card,
        executor="agy",
        model="gemini-test-model",
        domain="test-domain",
        inputs=(),
        outputs=(),
        gate_result=gate_result,
    )


def _registry_with_recovered_verification(tmp_path, *, accepted: bool) -> JobRegistry:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id="agy-error-presentation-933",
        repo=REPO,
        claim_key=f"{REPO}/agy-error-presentation-933",
        source_revision="b" * 64,
        workspace_root=str(tmp_path / "workspace"),
        combo="feature-oneshot",
        current_phase="review" if accepted else "verify",
        steps=(
            _step("build", "build-primary", "passed"),
            _step("verify", "verification", "passed" if accepted else "pending"),
            _step("review", "code-review", "pending"),
        ),
        candidate_head=CANDIDATE,
        verified_head=CANDIDATE if accepted else None,
        gate_status="running",
    )
    log_path = tmp_path / "verification.jsonl"
    log_lines = [json.dumps({"status": "ERROR", "message": RECOVERED_ERROR})]
    if accepted:
        log_lines.append(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "workflow-verification-result",
                    "status": "verified",
                    "summary": "verification passed",
                    "details": {"text": "tests passed"},
                    "reports": [],
                }
            )
        )
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    verification_job = registry.create_job(
        task=run.work_id,
        persona="reviewer",
        branch="feature/test",
        pane="",
        worktree=str(tmp_path / "verification-worktree"),
        kind="review",
        executor="agy",
        model_id="gemini-test-model",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="verification",
        workflow_phase="verify",
        log_path=str(log_path),
    )
    registry.update_headless_result(
        verification_job["job_id"],
        status="exited" if accepted else "failed",
        exit_code=0 if accepted else 1,
    )
    if accepted:
        registry.bind_workflow_evidence(
            verification_job["job_id"],
            locator={"kind": "verify", "path": "evidence/workflow/verification.json", "hash": "c" * 64},
            subject_head=CANDIDATE,
        )
        stored_job = registry._find_job(verification_job["job_id"])
        stored_job["usage"] = {"input_tokens": 496, "output_tokens": 19}
        stored_job["usage_raw"] = {"source": "agy", "total_tokens": 515}
        registry._persist()
        review_job = registry.create_job(
            task=run.work_id,
            persona="reviewer",
            branch="feature/test",
            pane="",
            worktree=str(tmp_path / "review-worktree"),
            kind="review",
            executor="codex",
            model_id="review-model",
            workflow_run_id=run.run_id,
            workflow_claim_key=run.claim_key,
            workflow_repo=run.repo,
            workflow_card="code-review",
            workflow_phase="review",
        )
        registry.update_status(review_job["job_id"], "running")
    return registry


def test_jobs_projection_shows_accepted_verdict_and_recovered_provider_error(tmp_path):
    registry = _registry_with_recovered_verification(tmp_path, accepted=True)

    verification = next(
        row for row in cli._attribute_jobs(registry.list_jobs(), registry)
        if row["workflow_phase"] == "verify"
    )

    assert verification["status"] == "exited"
    assert verification["workflow_result"] == {
        "status": "verified",
        "recovered_provider_errors": [RECOVERED_ERROR],
    }
    assert verification["usage"] == {"input_tokens": 496, "output_tokens": 19}
    assert verification["usage_raw"] == {"source": "agy", "total_tokens": 515}
    assert verification["workflow_evidence"]["hash"] == "c" * 64


def test_status_projection_shows_recovered_verification_for_active_review(tmp_path, monkeypatch):
    registry = _registry_with_recovered_verification(tmp_path, accepted=True)
    monkeypatch.setattr(
        manager_daemon.candidate_base,
        "resolve_candidate_git_base",
        lambda **_kwargs: SimpleNamespace(to_dict=lambda: None),
    )

    review = manager_daemon._in_flight_status(registry)[0]

    assert review["accepted_workflow_results"] == [
        {
            "card": "verification",
            "status": "verified",
            "recovered_provider_errors": [RECOVERED_ERROR],
        }
    ]


def test_attention_status_projection_shows_accepted_verdict_and_recovered_error(tmp_path):
    registry = _registry_with_recovered_verification(tmp_path, accepted=True)
    run = registry.list_workflow_runs()[0]

    entry = manager.workflow_status_entry(registry, run)

    assert entry["accepted_workflow_results"] == [
        {
            "card": "verification",
            "status": "verified",
            "recovered_provider_errors": [RECOVERED_ERROR],
        }
    ]


def test_jobs_projection_does_not_call_provider_error_a_verdict_without_acceptance(tmp_path):
    registry = _registry_with_recovered_verification(tmp_path, accepted=False)

    verification = cli._attribute_jobs(registry.list_jobs(), registry)[0]

    assert verification["status"] == "failed"
    assert "workflow_result" not in verification
