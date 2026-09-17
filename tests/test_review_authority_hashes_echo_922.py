"""Issue #922 RED coverage for reviewer authority_hashes echo fallback."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority, WorkflowStep
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo


def _manifest():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "reviewer authority hashes echo", change="reviewer-authority-hashes-echo")
    assert result.workflow_manifest is not None
    return result.workflow_manifest


def _review_terminalize_fixture(tmp_path: Path):
    repo_root = tmp_path / "candidate"
    repo_root.mkdir()
    authority_sources = {
        "docs/superpowers/plans/reviewer-authority-hashes-echo.md": b"# Accepted plan\n\nReview against this.\n",
        "docs/superpowers/specs/reviewer-authority-hashes-echo.md": b"# Accepted spec\n\nHonor this authority too.\n",
    }
    authority_hashes: dict[str, str] = {}
    authorities: list[PlanningArtifactAuthority] = []
    for ref, body in authority_sources.items():
        target = repo_root / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        authority_hashes[ref] = digest
        authorities.append(
            PlanningArtifactAuthority(
                ref=ref,
                kind="plan" if "/plans/" in ref else "spec",
                work_id="reviewer-authority-hashes-echo",
                baseline_sha256=digest,
            )
        )
    candidate = "a" * 40

    steps = tuple(
        WorkflowStep.from_dict(
            {
                **step.to_dict(),
                "gate_result": "passed"
                if step.phase in {"claim", "define", "plan", "build", "verify"}
                else "pending",
            }
        )
        for step in _manifest().steps
    )
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator_root / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id="reviewer-authority-hashes-echo",
        repo="owner/repo",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo_root),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={"review": 1},
        candidate_head=candidate,
        verified_head=candidate,
        gate_status="running",
        planning_authority=tuple(authorities),
    )
    builder_job = registry.create_job(
        task="builder",
        persona="builder",
        kind="build",
        branch="feature/work",
        pane="",
        worktree=str(repo_root),
        executor="codex",
        model_id="builder",
        independence_domain="openai",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        workflow_repo_root=str(repo_root),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(builder_job["job_id"], status="exited", exit_code=0)

    review_step = next(step for step in _manifest().steps if step.card == "code-review")
    patterns = manager._reviewer_input_patterns(run, manager._effective_workflow_inputs(run, review_step))
    snapshot = manager._workflow_input_snapshot(
        run=run,
        repo_root=repo_root,
        patterns=patterns,
        coordinator_root=coordinator_root,
    )
    report_ref = "reports/review/reviewer-authority-hashes-echo.md"
    review_job = registry.create_job(
        task="reviewer",
        persona="reviewer",
        kind="review",
        branch="feature/work",
        pane="",
        worktree=str(repo_root),
        executor="claude",
        model_id="reviewer",
        independence_domain="anthropic",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="code-review",
        workflow_phase="review",
        workflow_repo_root=str(repo_root),
        workflow_input_root=str(repo_root),
        workflow_inputs=patterns,
        workflow_input_snapshot=snapshot,
        workflow_outputs=(report_ref,),
        workflow_output_baseline=(),
        workflow_builder_job_id=builder_job["job_id"],
        source_revision=run.source_revision,
    )
    return registry, run, review_job, report_ref, authority_hashes, coordinator_root


def _write_review_log(review_job: dict, payload: dict) -> Path:
    log_path = Path(review_job["worktree"]) / f"{review_job['job_id']}.jsonl"
    log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return log_path


def _terminalize_review(
    tmp_path: Path,
    *,
    authority_hashes: dict[str, str] | None = None,
):
    registry, run, review_job, report_ref, expected_authority_hashes, coordinator_root = (
        _review_terminalize_fixture(tmp_path)
    )
    payload = {
        "schema_version": 1,
        "kind": "workflow-review-result",
        "reason": "accepted",
        "findings": [],
        "reports": [{"path": report_ref, "body": "# Review\n\nPassed.\n"}],
    }
    if authority_hashes is not None:
        payload["authority_hashes"] = authority_hashes
    log_path = _write_review_log(review_job, payload)
    registry.attach_launch_handle(
        review_job["job_id"], executor="claude", model_id="reviewer", log_path=str(log_path)
    )
    registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)
    bound = manager.terminalize_workflow_job(
        registry, job_id=review_job["job_id"], coordinator_root=coordinator_root
    )
    evidence, outputs, _path, _digest = manager._read_job_workflow_evidence(
        bound,
        run=run,
        coordinator_root=coordinator_root,
    )
    return evidence, outputs, report_ref, expected_authority_hashes


def test_terminalize_review_backfills_missing_authority_hashes_from_manager_snapshot(
    tmp_path: Path,
) -> None:
    evidence, outputs, report_ref, expected_authority_hashes = _terminalize_review(tmp_path)
    assert outputs == (report_ref,)
    assert evidence["state"] == "passed"
    assert evidence["authority_hashes"] == expected_authority_hashes
    assert evidence["authority_hashes_source"] == "manager-snapshot"


def test_terminalize_review_marks_reviewer_echo_source_when_authority_hashes_match(
    tmp_path: Path,
) -> None:
    registry, run, review_job, report_ref, expected_authority_hashes, coordinator_root = (
        _review_terminalize_fixture(tmp_path)
    )
    log_path = _write_review_log(
        review_job,
        {
            "schema_version": 1,
            "kind": "workflow-review-result",
            "reason": "accepted",
            "findings": [],
            "reports": [{"path": report_ref, "body": "# Review\n\nPassed.\n"}],
            "authority_hashes": dict(expected_authority_hashes),
        },
    )
    registry.attach_launch_handle(
        review_job["job_id"], executor="claude", model_id="reviewer", log_path=str(log_path)
    )
    registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)
    bound = manager.terminalize_workflow_job(
        registry, job_id=review_job["job_id"], coordinator_root=coordinator_root
    )
    evidence, outputs, _path, _digest = manager._read_job_workflow_evidence(
        bound,
        run=run,
        coordinator_root=coordinator_root,
    )
    assert outputs == (report_ref,)
    assert evidence["authority_hashes"] == expected_authority_hashes
    assert evidence["authority_hashes_source"] == "reviewer-echo"


def test_terminalize_review_rejects_drifted_authority_hashes_with_path(tmp_path: Path) -> None:
    registry, _run, review_job, report_ref, expected_authority_hashes, coordinator_root = (
        _review_terminalize_fixture(tmp_path)
    )
    drift_ref = sorted(expected_authority_hashes)[0]
    drifted = dict(expected_authority_hashes)
    drifted[drift_ref] = "0" * 64
    log_path = _write_review_log(
        review_job,
        {
            "schema_version": 1,
            "kind": "workflow-review-result",
            "reason": "accepted",
            "findings": [],
            "reports": [{"path": report_ref, "body": "# Review\n\nPassed.\n"}],
            "authority_hashes": drifted,
        },
    )
    registry.attach_launch_handle(
        review_job["job_id"], executor="claude", model_id="reviewer", log_path=str(log_path)
    )
    registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)

    with pytest.raises(ValueError, match=rf"authority_hashes drift.*{re.escape(drift_ref)}"):
        manager.terminalize_workflow_job(
            registry, job_id=review_job["job_id"], coordinator_root=coordinator_root
        )


def test_terminalize_review_rejects_partial_authority_hashes_with_path(tmp_path: Path) -> None:
    registry, _run, review_job, report_ref, expected_authority_hashes, coordinator_root = (
        _review_terminalize_fixture(tmp_path)
    )
    ordered_refs = sorted(expected_authority_hashes)
    missing_ref = ordered_refs[-1]
    partial = {ordered_refs[0]: expected_authority_hashes[ordered_refs[0]]}
    log_path = _write_review_log(
        review_job,
        {
            "schema_version": 1,
            "kind": "workflow-review-result",
            "reason": "accepted",
            "findings": [],
            "reports": [{"path": report_ref, "body": "# Review\n\nPassed.\n"}],
            "authority_hashes": partial,
        },
    )
    registry.attach_launch_handle(
        review_job["job_id"], executor="claude", model_id="reviewer", log_path=str(log_path)
    )
    registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)

    with pytest.raises(ValueError, match=rf"authority_hashes ref set mismatch.*{re.escape(missing_ref)}"):
        manager.terminalize_workflow_job(
            registry, job_id=review_job["job_id"], coordinator_root=coordinator_root
        )
