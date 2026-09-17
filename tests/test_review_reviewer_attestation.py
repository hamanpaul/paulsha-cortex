"""Issue #219: reviewer input attestation.

Reviewer jobs must be able to prove, before dispatch and again at verdict
readback, that they actually saw the exact frozen plan/authority content
pinned for the workflow run — not merely that some file with a matching name
happened to exist somewhere. The hippo #41 v3 incident (jobs 17-19 never
received any of the 7 frozen authority artifacts and still emitted a
confident PASS) is the motivating failure mode.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, review
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority, WorkflowRun, WorkflowStep
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo


def _manifest():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "reviewer attestation", change="reviewer-attestation")
    assert result.workflow_manifest is not None
    return result.workflow_manifest


# ---------------------------------------------------------------------------
# AC1/AC4 — review.verify_authority_in_input_snapshot proves presence+hash
# ---------------------------------------------------------------------------


def test_verify_authority_noop_when_no_authority_declared() -> None:
    # Backward compatible: runs without any frozen planning authority (the
    # overwhelming majority of existing workflows) are never blocked.
    review.verify_authority_in_input_snapshot(authority={}, input_snapshot=[])


def test_verify_authority_passes_when_present_and_matching() -> None:
    review.verify_authority_in_input_snapshot(
        authority={"docs/plan.md": "a" * 64},
        input_snapshot=[
            {"pattern": "docs/plan.md", "path": "docs/plan.md", "sha256": "a" * 64,
             "authority": "planning-authority", "content_ref": "/x"},
        ],
    )


def test_verify_authority_raises_when_ref_missing_from_snapshot() -> None:
    with pytest.raises(ValueError, match="missing frozen authority"):
        review.verify_authority_in_input_snapshot(
            authority={"docs/plan.md": "a" * 64},
            input_snapshot=[],
        )


def test_verify_authority_raises_on_hash_drift() -> None:
    with pytest.raises(ValueError, match="authority hash drift"):
        review.verify_authority_in_input_snapshot(
            authority={"docs/plan.md": "a" * 64},
            input_snapshot=[
                {"pattern": "docs/plan.md", "path": "docs/plan.md", "sha256": "b" * 64,
                 "authority": "planning-authority", "content_ref": "/x"},
            ],
        )


def test_verify_authority_rejects_row_not_tagged_planning_authority() -> None:
    # Defense in depth: a coincidentally hash-matching worktree file is not
    # provenance for the frozen authority baseline unless tagged as such.
    with pytest.raises(ValueError, match="missing frozen authority"):
        review.verify_authority_in_input_snapshot(
            authority={"docs/plan.md": "a" * 64},
            input_snapshot=[
                {"pattern": "docs/plan.md", "path": "docs/plan.md", "sha256": "a" * 64,
                 "authority": "worktree", "content_ref": "/x"},
            ],
        )


# ---------------------------------------------------------------------------
# AC3/AC4 — review.validate_review_verdict backfills the authority hash
# ---------------------------------------------------------------------------


def _base_verdict_kwargs() -> dict:
    return {
        "builder_job_id": "builder-1",
        "reviewer_job_id": "reviewer-1",
        "candidate": "a" * 40,
        "launch_identity": {"executor": "claude", "model_id": "reviewer", "independence_domain": "anthropic"},
    }


def _base_verdict_payload() -> dict:
    kwargs = _base_verdict_kwargs()
    return {
        "schema_version": review.REVIEW_SCHEMA_VERSION,
        "builder_job_id": kwargs["builder_job_id"],
        "reviewer_job_id": kwargs["reviewer_job_id"],
        "candidate": kwargs["candidate"],
        "launch_identity": kwargs["launch_identity"],
        "findings": [],
    }


def test_validate_review_verdict_unaffected_without_expected_authority_hashes() -> None:
    # Backward compatible: no side channel of expected hashes -> old behavior.
    verdict = review.validate_review_verdict(_base_verdict_payload(), **_base_verdict_kwargs())
    assert "authority_hashes" not in verdict


def test_validate_review_verdict_rejects_extra_authority_hashes_when_not_expected() -> None:
    payload = {**_base_verdict_payload(), "authority_hashes": {"docs/plan.md": "a" * 64}}
    with pytest.raises(ValueError, match="unexpected key"):
        review.validate_review_verdict(payload, **_base_verdict_kwargs())


def test_validate_review_verdict_requires_authority_hashes_when_expected() -> None:
    with pytest.raises(ValueError, match="missing keys"):
        review.validate_review_verdict(
            _base_verdict_payload(),
            **_base_verdict_kwargs(),
            expected_authority_hashes={"docs/plan.md": "a" * 64},
        )


def test_validate_review_verdict_rejects_incomplete_authority_hash_set() -> None:
    payload = {**_base_verdict_payload(), "authority_hashes": {}}
    with pytest.raises(ValueError, match="authority_hashes ref set mismatch"):
        review.validate_review_verdict(
            payload,
            **_base_verdict_kwargs(),
            expected_authority_hashes={"docs/plan.md": "a" * 64},
        )


def test_validate_review_verdict_rejects_drifted_authority_hash_value() -> None:
    # The reviewer claims a hash that does not match the frozen baseline: it
    # either never read the pinned content or misreports it. Either way this
    # must not be accepted as a PASS.
    payload = {**_base_verdict_payload(), "authority_hashes": {"docs/plan.md": "b" * 64}}
    with pytest.raises(ValueError, match="authority_hashes drift"):
        review.validate_review_verdict(
            payload,
            **_base_verdict_kwargs(),
            expected_authority_hashes={"docs/plan.md": "a" * 64},
        )


def test_validate_review_verdict_accepts_and_records_matching_authority_hashes() -> None:
    payload = {**_base_verdict_payload(), "authority_hashes": {"docs/plan.md": "a" * 64}}
    verdict = review.validate_review_verdict(
        payload,
        **_base_verdict_kwargs(),
        expected_authority_hashes={"docs/plan.md": "a" * 64},
    )
    assert verdict["authority_hashes"] == {"docs/plan.md": "a" * 64}
    assert verdict["state"] == "passed"


# ---------------------------------------------------------------------------
# AC1/AC2 — manager wiring: dispatch must prove authority even when the
# review card itself declares no explicit inputs (the deck default).
# ---------------------------------------------------------------------------


def test_reviewer_input_patterns_augments_with_frozen_authority_refs() -> None:
    run = WorkflowRun(
        run_id="r", work_id="w", repo="o/r", claim_key="c", source_revision="s" * 64,
        workspace_root="/tmp/x", combo="feature-oneshot", current_phase="review",
        steps=(), issue_refs=(), openspec_refs=(), pr_refs=(), attempts={},
        evidence_refs=(), gate_refs=(), brainstorm_required=False, primary_domain=None,
        candidate_head=None, verified_head=None, facets=(), gate_status="running",
        created_at=manager._utcnow(), updated_at=manager._utcnow(),
        planning_authority=(
            PlanningArtifactAuthority(ref="docs/plan.md", kind="plan", work_id="w", baseline_sha256="a" * 64),
        ),
    )
    assert manager._reviewer_input_patterns(run, ()) == ("docs/plan.md",)
    # Already covered by a declared pattern: no duplicate augmentation.
    assert manager._reviewer_input_patterns(run, ("docs/*.md",)) == ("docs/*.md",)


def test_dispatch_reviewer_input_snapshot_proves_authority_despite_empty_card_inputs(
    tmp_path: Path,
) -> None:
    # code-review declares requires: [] in cards.yaml -- reproduce that gap
    # directly and prove manager._dispatch_workflow_card's reviewer branch
    # still seeds+verifies the frozen plan.
    review_step = next(step for step in _manifest().steps if step.card == "code-review")
    assert review_step.inputs == ()  # sanity: this is exactly the deck default gap

    repo_root = tmp_path / "candidate"
    repo_root.mkdir()
    plan_ref = "docs/superpowers/plans/reviewer-attestation-plan.md"
    plan = repo_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan_bytes = b"# Accepted plan\n\nReview against this.\n"
    plan.write_bytes(plan_bytes)
    digest = hashlib.sha256(plan_bytes).hexdigest()

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="reviewer-attestation",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo_root),
        combo="feature-oneshot",
        current_phase="review",
        steps=_manifest().steps,
        issue_refs=(),
        openspec_refs=("reviewer-attestation",),
        pr_refs=(),
        attempts={"review": 1},
        gate_status="running",
        planning_authority=(
            PlanningArtifactAuthority(
                ref=plan_ref, kind="plan", work_id="reviewer-attestation", baseline_sha256=digest,
            ),
        ),
    )

    patterns = manager._reviewer_input_patterns(run, manager._effective_workflow_inputs(run, review_step))
    assert plan_ref in patterns

    snapshot = manager._workflow_input_snapshot(
        run=run, repo_root=repo_root, patterns=patterns, coordinator_root=tmp_path / "coordinator",
    )
    authority_rows = [row for row in snapshot if row["authority"] == "planning-authority"]
    assert authority_rows == [
        {"pattern": plan_ref, "path": plan_ref, "sha256": digest,
         "authority": "planning-authority", "content_ref": authority_rows[0]["content_ref"]},
    ]
    # The proof step itself must accept this snapshot (AC1).
    review.verify_authority_in_input_snapshot(
        authority={item.ref: item.baseline_sha256 for item in run.planning_authority},
        input_snapshot=snapshot,
    )


def test_dispatch_reviewer_input_snapshot_blocks_when_frozen_plan_drifted(tmp_path: Path) -> None:
    review_step = next(step for step in _manifest().steps if step.card == "code-review")
    repo_root = tmp_path / "candidate"
    repo_root.mkdir()
    plan_ref = "docs/superpowers/plans/reviewer-attestation-plan.md"
    plan = repo_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan.write_bytes(b"# Drifted plan\n")

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="reviewer-attestation",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo_root),
        combo="feature-oneshot",
        current_phase="review",
        steps=_manifest().steps,
        issue_refs=(),
        openspec_refs=("reviewer-attestation",),
        pr_refs=(),
        attempts={"review": 1},
        gate_status="running",
        planning_authority=(
            PlanningArtifactAuthority(
                ref=plan_ref, kind="plan", work_id="reviewer-attestation", baseline_sha256="0" * 64,
            ),
        ),
    )
    patterns = manager._reviewer_input_patterns(run, manager._effective_workflow_inputs(run, review_step))

    with pytest.raises(ValueError, match="planning input drift"):
        manager._workflow_input_snapshot(
            run=run, repo_root=repo_root, patterns=patterns, coordinator_root=tmp_path / "coordinator",
        )


# ---------------------------------------------------------------------------
# AC3/AC4 — manager.terminalize_workflow_job requires and validates the
# echoed authority_hashes before a review job's verdict can be trusted.
# ---------------------------------------------------------------------------


def _review_terminalize_fixture(tmp_path: Path):
    repo_root = tmp_path / "candidate"
    repo_root.mkdir()
    plan_ref = "docs/superpowers/plans/reviewer-attestation-plan.md"
    plan = repo_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan_bytes = b"# Accepted plan\n\nReview against this.\n"
    plan.write_bytes(plan_bytes)
    digest = hashlib.sha256(plan_bytes).hexdigest()
    candidate = "a" * 40

    steps = tuple(
        WorkflowStep.from_dict({
            **step.to_dict(),
            "gate_result": "passed" if step.phase in {"claim", "define", "plan", "build", "verify"} else "pending",
        })
        for step in _manifest().steps
    )
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator_root / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id="reviewer-attestation",
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
        planning_authority=(
            PlanningArtifactAuthority(
                ref=plan_ref, kind="plan", work_id="reviewer-attestation", baseline_sha256=digest,
            ),
        ),
    )
    builder_job = registry.create_job(
        task="builder", persona="builder", kind="build", branch="feature/work",
        pane="", worktree=str(repo_root), executor="codex", model_id="builder",
        independence_domain="openai", subject_head=candidate,
        workflow_run_id=run.run_id, workflow_claim_key=run.claim_key,
        workflow_repo=run.repo, workflow_card="subagent-build", workflow_phase="build",
        workflow_repo_root=str(repo_root), source_revision=run.source_revision,
    )
    registry.update_headless_result(builder_job["job_id"], status="exited", exit_code=0)

    review_step = next(step for step in _manifest().steps if step.card == "code-review")
    patterns = manager._reviewer_input_patterns(run, manager._effective_workflow_inputs(run, review_step))
    snapshot = manager._workflow_input_snapshot(
        run=run, repo_root=repo_root, patterns=patterns, coordinator_root=coordinator_root,
    )
    report_ref = "reports/review/reviewer-attestation.md"
    review_job = registry.create_job(
        task="reviewer", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(repo_root), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head=candidate,
        workflow_run_id=run.run_id, workflow_claim_key=run.claim_key,
        workflow_repo=run.repo, workflow_card="code-review", workflow_phase="review",
        workflow_repo_root=str(repo_root), workflow_input_root=str(repo_root),
        workflow_inputs=patterns, workflow_input_snapshot=snapshot,
        workflow_outputs=(report_ref,), workflow_output_baseline=(),
        workflow_builder_job_id=builder_job["job_id"], source_revision=run.source_revision,
    )
    return registry, run, review_job, report_ref, digest, plan_ref, coordinator_root


def _write_review_log(review_job: dict, payload: dict) -> Path:
    log_path = Path(review_job["worktree"]) / f"{review_job['job_id']}.jsonl"
    log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return log_path


def test_terminalize_review_backfills_missing_authority_hashes_echo(tmp_path: Path) -> None:
    registry, run, review_job, report_ref, digest, plan_ref, coordinator_root = (
        _review_terminalize_fixture(tmp_path)
    )
    log_path = _write_review_log(
        review_job,
        {
            "schema_version": 1, "kind": "workflow-review-result", "reason": "accepted",
            "findings": [], "reports": [{"path": report_ref, "body": "# Review\n\nPassed.\n"}],
        },
    )
    registry.attach_launch_handle(
        review_job["job_id"], executor="claude", model_id="reviewer", log_path=str(log_path)
    )
    registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)

    bound = manager.terminalize_workflow_job(
        registry, job_id=review_job["job_id"], coordinator_root=coordinator_root,
    )
    evidence, _outputs, _path, _digest = manager._read_job_workflow_evidence(
        bound,
        run=run,
        coordinator_root=coordinator_root,
        include_review_authority_metadata=True,
    )
    assert evidence["authority_hashes"] == {plan_ref: digest}
    assert evidence["authority_hashes_source"] == "manager-snapshot"


def test_terminalize_review_rejects_drifted_authority_hashes_echo(tmp_path: Path) -> None:
    registry, run, review_job, report_ref, digest, plan_ref, coordinator_root = (
        _review_terminalize_fixture(tmp_path)
    )
    log_path = _write_review_log(
        review_job,
        {
            "schema_version": 1, "kind": "workflow-review-result", "reason": "accepted",
            "findings": [], "reports": [{"path": report_ref, "body": "# Review\n\nPassed.\n"}],
            "authority_hashes": {plan_ref: "0" * 64},
        },
    )
    registry.attach_launch_handle(
        review_job["job_id"], executor="claude", model_id="reviewer", log_path=str(log_path)
    )
    registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)

    with pytest.raises(ValueError, match="authority_hashes drift"):
        manager.terminalize_workflow_job(
            registry, job_id=review_job["job_id"], coordinator_root=coordinator_root,
        )


def test_terminalize_review_accepts_matching_authority_hashes_echo(tmp_path: Path) -> None:
    registry, run, review_job, report_ref, digest, plan_ref, coordinator_root = (
        _review_terminalize_fixture(tmp_path)
    )
    log_path = _write_review_log(
        review_job,
        {
            "schema_version": 1, "kind": "workflow-review-result", "reason": "accepted",
            "findings": [], "reports": [{"path": report_ref, "body": "# Review\n\nPassed.\n"}],
            "authority_hashes": {plan_ref: digest},
        },
    )
    registry.attach_launch_handle(
        review_job["job_id"], executor="claude", model_id="reviewer", log_path=str(log_path)
    )
    registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)

    bound = manager.terminalize_workflow_job(
        registry, job_id=review_job["job_id"], coordinator_root=coordinator_root,
    )
    assert bound["workflow_evidence"] is not None
