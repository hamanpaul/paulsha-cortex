from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import completion, verification
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import (
    PlanningArtifactAuthority,
    WorkflowStep,
)

FROZEN_PLAN_REF = "docs/superpowers/plans/sizing-stability-direction.md"


def _step() -> WorkflowStep:
    return WorkflowStep(
        phase="plan",
        persona="planner",
        card="writing-plans",
        executor="agy",
        model="gemini-3.1-pro-high",
        domain="google",
        inputs=("openspec/changes/sizing-stability-direction/tasks.md",),
        outputs=(FROZEN_PLAN_REF,),
        gate_result="pending",
    )


def _write_frozen_plan(tmp_path: Path) -> tuple[Path, str]:
    artifact = tmp_path / FROZEN_PLAN_REF
    artifact.parent.mkdir(parents=True, exist_ok=True)
    frozen_bytes = (
        b"---\nstatus: accepted\nwork_item: sizing-stability-direction\n---\n"
        b"# Frozen sizing stability plan\n"
    )
    artifact.write_bytes(frozen_bytes)
    return artifact, hashlib.sha256(frozen_bytes).hexdigest()


def _create_run(
    registry: JobRegistry,
    *,
    planning_digest: str,
    sizing_score: int | None = None,
    sizing_band: str | None = None,
):
    return registry._manager_create_workflow_run(
        work_id="sizing-stability-direction",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "a" * 64,
        source_revision="source-revision-before-repair",
        workspace_root="/tmp/paulsha-cortex",
        combo="small-fix",
        current_phase="plan",
        steps=(_step(),),
        issue_refs=("hamanpaul/paulsha-cortex#831",),
        openspec_refs=("sizing-stability-direction",),
        attempts={"plan": 2},
        evidence_refs=("evidence/planning/frozen.json",),
        planning_authority=(
            PlanningArtifactAuthority(
                ref=FROZEN_PLAN_REF,
                kind="plan",
                work_id="sizing-stability-direction",
                baseline_sha256=planning_digest,
            ),
        ),
        sizing_score=sizing_score,
        sizing_band=sizing_band,
    )


def _assert_bytes_and_sha_unchanged(
    path: Path,
    expected_bytes: bytes,
    expected_sha256: str,
) -> None:
    actual = path.read_bytes()
    assert actual == expected_bytes
    assert hashlib.sha256(actual).hexdigest() == expected_sha256


def test_legacy_workflow_reload_preserves_frozen_planning_history_without_backfill(
    tmp_path: Path,
) -> None:
    artifact, artifact_sha256 = _write_frozen_plan(tmp_path)
    state = tmp_path / "jobs.json"
    created = _create_run(
        JobRegistry(state_path=state),
        planning_digest=artifact_sha256,
    )
    payload = json.loads(state.read_text(encoding="utf-8"))
    legacy_run = payload["workflows"][0]
    # This is the pre-sizing shape: no algorithm/provenance snapshot fields.
    legacy_run.pop("sizing_score")
    legacy_run.pop("sizing_band")
    state.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # This is the complete persisted legacy baseline.  Only the two sizing
    # fields are absent; every identity, binding, step, planning, attempt and
    # evidence field remains part of the expected returned state.
    expected_returned_state = dict(legacy_run)
    expected_returned_state["sizing_score"] = None
    expected_returned_state["sizing_band"] = None
    frozen_bytes = state.read_bytes()
    frozen_sha256 = hashlib.sha256(frozen_bytes).hexdigest()
    frozen_artifact_bytes = artifact.read_bytes()

    first_registry = JobRegistry(state_path=state)
    first = first_registry.get_workflow_run(created.run_id)
    fresh_registry = JobRegistry(state_path=state)
    second = fresh_registry.get_workflow_run(created.run_id)

    for run in (first, second):
        # Comparing the complete projection catches read-time field loss such
        # as issue_refs/openspec_refs disappearing while selected assertions
        # still pass.  Legacy sizing fields stay absent on disk but resolve to
        # the model's documented None defaults in the returned object.
        assert run.to_dict() == expected_returned_state
    assert first is not second
    assert first.issue_refs == ("hamanpaul/paulsha-cortex#831",)
    assert first.openspec_refs == ("sizing-stability-direction",)
    assert first.planning_authority[0].baseline_sha256 == artifact_sha256
    persisted_legacy = json.loads(state.read_text(encoding="utf-8"))["workflows"][0]
    assert "sizing_score" not in persisted_legacy
    assert "sizing_band" not in persisted_legacy
    _assert_bytes_and_sha_unchanged(state, frozen_bytes, frozen_sha256)
    _assert_bytes_and_sha_unchanged(artifact, frozen_artifact_bytes, artifact_sha256)

    # Negative control: a history mutation must make the immutability oracle fail.
    mutated = json.loads(state.read_text(encoding="utf-8"))
    mutated["workflows"][0]["planning_source_revision"] = "source-revision-mutated"
    state.write_text(
        json.dumps(mutated, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _assert_bytes_and_sha_unchanged(state, frozen_bytes, frozen_sha256)


def test_versioned_workflow_reload_preserves_non_null_sizing_and_frozen_plan(
    tmp_path: Path,
) -> None:
    artifact, artifact_sha256 = _write_frozen_plan(tmp_path)
    state = tmp_path / "jobs.json"
    created = _create_run(
        JobRegistry(state_path=state),
        planning_digest=artifact_sha256,
        sizing_score=6,
        sizing_band="yellow",
    )
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 2
    expected_returned_state = persisted["workflows"][0]
    frozen_bytes = state.read_bytes()
    frozen_sha256 = hashlib.sha256(frozen_bytes).hexdigest()
    frozen_artifact_bytes = artifact.read_bytes()

    first = JobRegistry(state_path=state).get_workflow_run(created.run_id)
    fresh = JobRegistry(state_path=state).list_workflow_runs()[0]

    assert first.to_dict() == expected_returned_state
    assert fresh.to_dict() == expected_returned_state
    assert first.sizing_score == fresh.sizing_score == 6
    assert first.sizing_band == fresh.sizing_band == "yellow"
    assert first.planning_authority[0].baseline_sha256 == artifact_sha256
    _assert_bytes_and_sha_unchanged(state, frozen_bytes, frozen_sha256)
    _assert_bytes_and_sha_unchanged(artifact, frozen_artifact_bytes, artifact_sha256)

    # A real frozen artifact mutation must be visible to the byte/hash oracle;
    # the digest is not a synthetic placeholder detached from fixture bytes.
    artifact.write_bytes(frozen_artifact_bytes + b"mutated\n")
    with pytest.raises(AssertionError):
        _assert_bytes_and_sha_unchanged(artifact, frozen_artifact_bytes, artifact_sha256)


def _verification_evidence(root: Path, *, slice_id: str, candidate: str) -> dict[str, str]:
    return verification.write_verification_evidence(
        {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": slice_id,
            "candidate": candidate,
            "status": "verified",
            "summary": "sizing-history-regression",
            "details": {"ok": True},
        },
        coordinator_root=root,
    )


def test_completion_and_evidence_reload_keep_bytes_and_hashes_and_reject_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "coordinator"
    candidate = "c" * 40
    evidence = _verification_evidence(root, slice_id="sizing-history", candidate=candidate)
    record = completion.write_completion_record(
        {
            "schema_version": completion.COMPLETION_SCHEMA_VERSION,
            "slice_id": "sizing-history",
            "spec_hash": "1" * 64,
            "plan_hash": "2" * 64,
            "verification_hash": "3" * 64,
            "builder_job_id": "builder-1",
            "reviewer_job_id": None,
            "dispatch_base": "a" * 40,
            "candidate": candidate,
            "target_branch": "main",
            "target_remote": "origin",
            "target_ref": "refs/remotes/origin/main",
            "target_ref_sha": "d" * 40,
            "verification_evidence_path": evidence["path"],
            "verification_evidence_hash": evidence["hash"],
            "review_policy": "not-required",
            "docs_class": "informational",
            "review_evaluation_path": None,
            "review_evaluation_hash": None,
            "completed_at": "2026-09-08T00:00:00+00:00",
            # Legacy records intentionally omit sizing_score/sizing_band.
        },
        coordinator_root=root,
    )
    record_path = Path(record["path"])
    evidence_path = Path(evidence["path"])
    record_bytes = record_path.read_bytes()
    evidence_bytes = evidence_path.read_bytes()
    record_sha256 = hashlib.sha256(record_bytes).hexdigest()
    evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()

    first = completion.read_completion_record(record_path, expected_hash=record["hash"])
    fresh = completion.read_completion_record(record_path, expected_hash=record["hash"])
    assert first == fresh == record["payload"]
    assert "sizing_score" not in first
    assert "sizing_band" not in first
    _assert_bytes_and_sha_unchanged(record_path, record_bytes, record_sha256)
    _assert_bytes_and_sha_unchanged(evidence_path, evidence_bytes, evidence_sha256)

    # Negative control: mutating the addressed evidence must fail both the
    # byte/hash oracle and the CompletionRecord read-time reference check.
    changed_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    changed_evidence["details"]["ok"] = False
    evidence_path.write_text(
        json.dumps(changed_evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _assert_bytes_and_sha_unchanged(evidence_path, evidence_bytes, evidence_sha256)
    with pytest.raises(ValueError, match="verification_evidence_path hash mismatch"):
        completion.read_completion_record(record_path, expected_hash=record["hash"])
