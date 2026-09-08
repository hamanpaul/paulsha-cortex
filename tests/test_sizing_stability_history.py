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


def _step() -> WorkflowStep:
    return WorkflowStep(
        phase="plan",
        persona="planner",
        card="writing-plans",
        executor="agy",
        model="gemini-3.1-pro-high",
        domain="google",
        inputs=("openspec/changes/sizing-stability-direction/tasks.md",),
        outputs=("docs/superpowers/plans/sizing-stability-direction.md",),
        gate_result="pending",
    )


def _create_run(registry: JobRegistry):
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
                ref="docs/superpowers/plans/sizing-stability-direction.md",
                kind="plan",
                work_id="sizing-stability-direction",
                baseline_sha256="b" * 64,
            ),
        ),
        sizing_score=None,
        sizing_band=None,
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
    state = tmp_path / "jobs.json"
    created = _create_run(JobRegistry(state_path=state))
    payload = json.loads(state.read_text(encoding="utf-8"))
    legacy_run = payload["workflows"][0]
    # This is the pre-sizing shape: no algorithm/provenance snapshot fields.
    legacy_run.pop("sizing_score")
    legacy_run.pop("sizing_band")
    state.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    frozen_bytes = state.read_bytes()
    frozen_sha256 = hashlib.sha256(frozen_bytes).hexdigest()

    first = JobRegistry(state_path=state).get_workflow_run(created.run_id)
    second = JobRegistry(state_path=state).get_workflow_run(created.run_id)

    for run in (first, second):
        assert run.sizing_score is None
        assert run.sizing_band is None
        assert run.source_revision == "source-revision-before-repair"
        assert run.planning_source_revision == "source-revision-before-repair"
        assert run.planning_authority[0].baseline_sha256 == "b" * 64
        assert run.attempts == {"plan": 2}
        assert run.evidence_refs == ("evidence/planning/frozen.json",)
    assert "sizing_score" not in json.loads(state.read_text(encoding="utf-8"))["workflows"][0]
    _assert_bytes_and_sha_unchanged(state, frozen_bytes, frozen_sha256)

    # Negative control: a history mutation must make the immutability oracle fail.
    mutated = json.loads(state.read_text(encoding="utf-8"))
    mutated["workflows"][0]["planning_source_revision"] = "source-revision-mutated"
    state.write_text(
        json.dumps(mutated, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _assert_bytes_and_sha_unchanged(state, frozen_bytes, frozen_sha256)


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
