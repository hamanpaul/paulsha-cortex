"""#497 / T01-T08 / T12-T13 GREEN: recovery registry receipt contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from paulsha_cortex.coordinator import registry as registry_module
from paulsha_cortex.coordinator.registry import JobRegistry

RECOVERY_REQUEST_VERSION = "cortex/recovery-registry-request/v1"
RECOVERY_RECEIPT_VERSION = "cortex/recovery-registry-receipt/v1"
SLICE_BINDING_VERSION = "cortex/slice-binding/v1"
JOB_SUPERSESSION_VERSION = "cortex/job-supersession/v1"
JOB_CONSUMPTION_VERSION = "cortex/job-consumption/v1"
CHECKPOINT_REQUEST_VERSION = "cortex/legacy-binding-checkpoint-request/v1"
CHECKPOINT_SNAPSHOT_VERSION = "cortex/legacy-binding-snapshot/v1"
CHECKPOINT_RECEIPT_VERSION = "cortex/legacy-binding-checkpoint-receipt/v1"
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
EXPECTED_RECOVERY_REQUEST_DIGEST = "bb7644370758db3d3387c8edd311034e54345edae8750cd88f9de187d91bd949"
EXPECTED_CHECKPOINT_FINGERPRINT = "f6b0f5cd07fee74dcff2f2520c77b95fbc3ab8f4f7f7451201153d0464b9f4bb"
EXPECTED_CHECKPOINT_REQUEST_DIGEST = "736960e40dc975e7324b9248a27baed3047ba32541b553c121ffa3e745b6dddd"


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _prefixed_digest(prefix: str, payload: object) -> str:
    return hashlib.sha256(prefix.encode("utf-8") + b"\n" + _canonical_json_bytes(payload)).hexdigest()


def _deepcopy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _proof_ref(ref: str, sha256: str) -> dict[str, str]:
    return {"ref": ref, "sha256": sha256}


def _state_payload(
    slice_row: Mapping[str, Any] | None = None,
    *,
    jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "seq": 0,
        "jobs": jobs or [],
        "slices": [] if slice_row is None else [_deepcopy(slice_row)],
        "workflows": [],
        "legacy_records": {"source_schema_version": 1, "seq": 0, "jobs": [], "slices": []},
        "reclaim_resets": [],
    }


def _write_state(state_path: Path, payload: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    state_path.write_bytes(encoded)
    return encoded


def _create_job(registry: JobRegistry, *, task: str, worktree: Path) -> dict[str, Any]:
    return registry.create_job(
        task=task,
        persona="builder",
        branch=f"feature/{task}",
        pane="",
        worktree=str(worktree),
    )


def _binding_snapshot(slice_row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "binding_version": slice_row["binding_version"],
        "binding_revision": slice_row["binding_revision"],
        "builder_job_id": slice_row["builder_job_id"],
        "reviewer_job_id": slice_row["reviewer_job_id"],
        "candidate": slice_row["candidate"],
        "state": slice_row["state"],
        "gate_state": slice_row["gate_state"],
        "spec": _deepcopy(slice_row["spec"]),
        "plan": _deepcopy(slice_row["plan"]),
        "verification_hash": slice_row["verification"]["hash"],
        "target_branch": slice_row["target_branch"],
        "target_remote": slice_row["target_remote"],
        "dispatch_base": slice_row["dispatch_base"],
    }


def _legacy_binding_snapshot(slice_row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "builder_job_id": slice_row["builder_job_id"],
        "reviewer_job_id": slice_row["reviewer_job_id"],
        "candidate": slice_row["candidate"],
        "state": slice_row["state"],
        "gate_state": slice_row["gate_state"],
        "spec": _deepcopy(slice_row["spec"]),
        "plan": _deepcopy(slice_row["plan"]),
        "verification_hash": slice_row["verification"]["hash"],
        "target_branch": slice_row["target_branch"],
        "target_remote": slice_row["target_remote"],
        "dispatch_base": slice_row["dispatch_base"],
    }


def _create_bound_slice(
    tmp_path: Path,
    *,
    slice_id: str = "slice-a",
) -> tuple[JobRegistry, dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    state_path = tmp_path / f"{slice_id}-jobs.json"
    registry = JobRegistry(state_path=state_path)
    builder = _create_job(
        registry,
        task=f"{slice_id}-builder",
        worktree=tmp_path / "wt" / f"{slice_id}-builder",
    )
    reviewer = _create_job(
        registry,
        task=f"{slice_id}-reviewer",
        worktree=tmp_path / "wt" / f"{slice_id}-reviewer",
    )
    registry.create_slice(
        slice_id=slice_id,
        spec_path=f"specs/{slice_id}.md",
        spec_hash="spec-sha",
        plan_path=f"plans/{slice_id}.md",
        plan_hash="plan-sha",
        target_branch=f"feature/{slice_id}",
        target_remote="origin",
        verification_hash="verification-hash",
        verification={"docs_class": "code"},
        dispatch_base="dispatch-base-sha",
        builder_job_id=builder["job_id"],
        reviewer_job_id=reviewer["job_id"],
        candidate=None,
    )
    registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")
    return registry, builder, reviewer, registry.get_slice(slice_id), state_path


def _recovery_request(
    slice_row: Mapping[str, Any],
    *,
    request_id: str = "recovery-request-1",
    required_steps: tuple[str, ...] = ("reclaim-worktree", "supersede-handoff"),
) -> dict[str, Any]:
    payload = {
        "request_type": "slice-action",
        "action": "recover-pre-candidate",
        "target": {
            "repo": "hamanpaul/paulsha-cortex",
            "work_id": "recovery-registry-receipt",
            "slice_id": slice_row["slice_id"],
        },
        "expected_binding": _binding_snapshot(slice_row),
        "required_steps": list(required_steps),
        "actor": "operator",
        "requested_by": "manager",
        "created_at": "2026-09-21T01:02:03+00:00",
    }
    return {
        "version": RECOVERY_REQUEST_VERSION,
        "request_id": request_id,
        "payload": payload,
        "request_digest": _prefixed_digest(
            RECOVERY_REQUEST_VERSION,
            {"version": RECOVERY_REQUEST_VERSION, "request_id": request_id, "payload": payload},
        ),
    }


def _step_receipts() -> list[dict[str, str]]:
    return [
        {
            "step_id": "reclaim-worktree",
            "ref": "evidence/reclaim-worktree.json",
            "sha256": "1" * 64,
        },
        {
            "step_id": "supersede-handoff",
            "ref": "evidence/supersede-handoff.json",
            "sha256": "2" * 64,
        },
    ]


def _legacy_slice_row(
    *,
    slice_id: str,
    builder_job_id: str,
    reviewer_job_id: str,
) -> dict[str, Any]:
    return {
        "slice_id": slice_id,
        "spec": {"path": f"specs/{slice_id}.md", "hash": "spec-sha"},
        "plan": {"path": f"plans/{slice_id}.md", "hash": "plan-sha"},
        "target_branch": f"feature/{slice_id}",
        "target_remote": "origin",
        "dispatch_base": "dispatch-base-sha",
        "builder_job_id": builder_job_id,
        "reviewer_job_id": reviewer_job_id,
        "candidate": "c" * 40,
        "state": "failed",
        "gate_state": "failed",
        "verification": {
            "hash": "verification-hash",
            "contract": {"docs_class": "code"},
        },
        "current_verification_evidence_hash": None,
        "current_evidence_refs": ["evidence/current.json"],
        "current_evaluation_refs": ["gate/current.json"],
        "evidence_history": [{"action": "verify", "actor": "builder", "refs": ["evidence/current.json"], "at": "2026-09-21T00:00:00+00:00"}],
        "evaluation_history": [{"action": "verify", "actor": "reviewer", "refs": ["gate/current.json"], "at": "2026-09-21T00:00:00+00:00"}],
        "actions": [{"action": "builder-failed", "actor": "builder", "state": "failed", "gate_state": "failed", "at": "2026-09-21T00:00:00+00:00"}],
        "created_at": "2026-09-21T00:00:00+00:00",
        "updated_at": "2026-09-21T00:00:00+00:00",
        "legacy_metadata": {"confirmed": True, "score": -0.0},
    }


def _create_legacy_registry(
    tmp_path: Path,
    *,
    slice_id: str = "legacy-a",
) -> tuple[JobRegistry, dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    state_path = tmp_path / f"{slice_id}-legacy.json"
    registry = JobRegistry(state_path=state_path)
    builder = _create_job(
        registry,
        task=f"{slice_id}-builder",
        worktree=tmp_path / "wt" / f"{slice_id}-builder",
    )
    reviewer = _create_job(
        registry,
        task=f"{slice_id}-reviewer",
        worktree=tmp_path / "wt" / f"{slice_id}-reviewer",
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["slices"] = [_legacy_slice_row(slice_id=slice_id, builder_job_id=builder["job_id"], reviewer_job_id=reviewer["job_id"])]
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    reloaded = JobRegistry(state_path=state_path)
    return reloaded, builder, reviewer, reloaded.get_slice(slice_id), state_path


def _checkpoint_request(
    slice_row: Mapping[str, Any],
    *,
    request_id: str = "checkpoint-request-1",
) -> dict[str, Any]:
    expected_legacy_row = _deepcopy(slice_row)
    expected_legacy_binding = _legacy_binding_snapshot(slice_row)
    expected_job_refs = {
        "builder_job_id": slice_row["builder_job_id"],
        "reviewer_job_id": slice_row["reviewer_job_id"],
    }
    fingerprint = _prefixed_digest(
        CHECKPOINT_SNAPSHOT_VERSION,
        {
            "version": CHECKPOINT_SNAPSHOT_VERSION,
            "slice": expected_legacy_row,
            "binding": expected_legacy_binding,
            "job_refs": expected_job_refs,
        },
    )
    payload = {
        "operation": "checkpoint-legacy-binding",
        "target": {
            "repo": "hamanpaul/paulsha-cortex",
            "work_id": "recovery-registry-receipt",
            "slice_id": slice_row["slice_id"],
        },
        "expected_legacy_row": expected_legacy_row,
        "expected_legacy_binding": expected_legacy_binding,
        "expected_job_refs": expected_job_refs,
        "legacy_snapshot_fingerprint": fingerprint,
        "actor": "operator",
        "requested_by": "manager",
        "created_at": "2026-09-21T04:05:06+00:00",
        "provenance": {
            "owner_action_ref": "actions/operator-approval.json",
            "observed_at": "2026-09-21T04:00:00+00:00",
            "authority_ref": "docs/superpowers/plans/recovery-registry-receipt.md",
            "proof_refs": [
                _proof_ref("evidence/legacy-binding.json", "3" * 64),
                _proof_ref("evidence/owner-approval.json", "4" * 64),
            ],
        },
    }
    return {
        "version": CHECKPOINT_REQUEST_VERSION,
        "request_id": request_id,
        "payload": payload,
        "request_digest": _prefixed_digest(
            CHECKPOINT_REQUEST_VERSION,
            {"version": CHECKPOINT_REQUEST_VERSION, "request_id": request_id, "payload": payload},
        ),
    }


def _set_slice_binding_revision(state_path: Path, *, revision: int) -> None:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["slices"][0]["binding_revision"] = revision
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def test_create_slice_starts_versioned_and_repin_forces_new_generation(tmp_path: Path) -> None:
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    created = registry.create_slice(
        slice_id="slice-a",
        spec_path="specs/slice-a.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-a.md",
        plan_hash="plan-sha",
        target_branch="feature/slice-a",
        target_remote="origin",
        verification_hash="0" * 64,
        verification=None,
        dispatch_base=None,
        builder_job_id=None,
        reviewer_job_id=None,
        candidate=None,
    )

    assert created["binding_version"] == SLICE_BINDING_VERSION
    assert created["binding_revision"] == 1
    assert created["recovery_receipts"] == []
    assert "binding_checkpoint_receipt" not in created

    unchanged = registry.update_slice("slice-a", current_evidence_refs=["evidence/current.json"])
    assert unchanged["binding_revision"] == 1

    repinned = registry.repin_slice(
        "slice-a",
        spec_path="specs/slice-a.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-a.md",
        plan_hash="plan-sha",
        target_branch="feature/slice-a",
        target_remote="origin",
        verification_hash="0" * 64,
        verification=None,
        dispatch_base=None,
    )
    assert repinned["binding_revision"] == 2


def test_legacy_rows_reload_without_backfill_and_legacy_mutators_do_not_upgrade(tmp_path: Path) -> None:
    state_path = tmp_path / "jobs.json"
    payload = _state_payload(
        {
            "slice_id": "legacy-a",
            "spec": {"path": "specs/legacy-a.md", "hash": "spec-sha"},
            "plan": {"path": "plans/legacy-a.md", "hash": "plan-sha"},
            "target_branch": "feature/legacy-a",
            "target_remote": "origin",
            "dispatch_base": None,
            "builder_job_id": None,
            "reviewer_job_id": None,
            "candidate": None,
            "state": "pending",
            "gate_state": "pending",
            "verification": {"hash": "verification-hash", "contract": None},
            "current_verification_evidence_hash": None,
            "current_evidence_refs": [],
            "current_evaluation_refs": [],
            "evidence_history": [],
            "evaluation_history": [],
            "actions": [],
            "created_at": "2026-09-21T00:00:00+00:00",
            "updated_at": "2026-09-21T00:00:00+00:00",
        }
    )
    original = _write_state(state_path, payload)

    registry = JobRegistry(state_path=state_path)
    assert registry.get_slice("legacy-a").get("binding_version") is None
    assert registry.get_slice("legacy-a").get("binding_revision") is None
    assert state_path.read_bytes() == original

    updated = registry.update_slice("legacy-a", state="needs_human", gate_state="needs_human")
    assert "binding_version" not in updated
    assert "binding_revision" not in updated

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert "binding_version" not in persisted["slices"][0]
    assert "binding_revision" not in persisted["slices"][0]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update({"binding_version": SLICE_BINDING_VERSION}),
        lambda row: row.update({"recovery_receipts": []}),
    ],
)
def test_partial_additive_recovery_fields_fail_closed(tmp_path: Path, mutation) -> None:
    state_path = tmp_path / "jobs.json"
    row = {
        "slice_id": "broken-a",
        "spec": {"path": "specs/broken-a.md", "hash": "spec-sha"},
        "plan": {"path": "plans/broken-a.md", "hash": "plan-sha"},
        "target_branch": "feature/broken-a",
        "target_remote": "origin",
        "dispatch_base": None,
        "builder_job_id": None,
        "reviewer_job_id": None,
        "candidate": None,
        "state": "pending",
        "gate_state": "pending",
        "verification": {"hash": "verification-hash", "contract": None},
        "current_verification_evidence_hash": None,
        "current_evidence_refs": [],
        "current_evaluation_refs": [],
        "evidence_history": [],
        "evaluation_history": [],
        "actions": [],
        "created_at": "2026-09-21T00:00:00+00:00",
        "updated_at": "2026-09-21T00:00:00+00:00",
    }
    mutation(row)
    _write_state(state_path, _state_payload(row))

    with pytest.raises(ValueError, match="legacy-binding-unversioned"):
        JobRegistry(state_path=state_path)


def test_prepare_recovery_uses_exact_golden_request_and_replays_prepared_receipts(tmp_path: Path) -> None:
    registry, builder, reviewer, slice_row, state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)

    assert request["request_digest"] == EXPECTED_RECOVERY_REQUEST_DIGEST
    receipt = registry.prepare_recovery("slice-a", request=request)
    assert set(receipt) == {
        "version",
        "request_id",
        "request_digest",
        "payload",
        "phase",
        "prepared_at",
        "completed_at",
        "applied_binding",
        "affected_job_ids",
        "required_steps",
        "step_receipts",
        "result",
    }
    assert receipt["version"] == RECOVERY_RECEIPT_VERSION
    assert receipt["phase"] == "prepared"
    assert receipt["completed_at"] is None
    assert receipt["applied_binding"] is None
    assert receipt["result"] is None
    assert receipt["affected_job_ids"] == [builder["job_id"], reviewer["job_id"]]
    assert receipt["required_steps"] == ["reclaim-worktree", "supersede-handoff"]
    assert receipt["step_receipts"] == []
    assert registry.lookup_registry_request_receipt(request["request_id"]) == {
        "slice_id": "slice-a",
        "kind": "recovery",
        "receipt": receipt,
    }

    before_replay = state_path.read_bytes()
    assert registry.prepare_recovery("slice-a", request=request) == receipt
    assert state_path.read_bytes() == before_replay

    reloaded = JobRegistry(state_path=state_path)
    assert reloaded.prepare_recovery("slice-a", request=request) == receipt
    assert state_path.read_bytes() == before_replay

    conflicting = _recovery_request(
        slice_row,
        request_id=request["request_id"],
        required_steps=("reclaim-worktree", "verify-proof"),
    )
    with pytest.raises(ValueError, match="request-content-conflict"):
        registry.prepare_recovery("slice-a", request=conflicting)

    invented = _deepcopy(request)
    invented["payload"]["request_type"] = "invented-action"
    invented["request_digest"] = _prefixed_digest(
        RECOVERY_REQUEST_VERSION,
        {"version": RECOVERY_REQUEST_VERSION, "request_id": invented["request_id"], "payload": invented["payload"]},
    )
    with pytest.raises(ValueError, match="malformed-recovery-context"):
        registry.prepare_recovery("slice-a", request=invented)


def test_prepare_recovery_rejects_stale_binding_after_aba_cycle(tmp_path: Path) -> None:
    registry, _builder, _reviewer, slice_row, _state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)

    registry.update_slice("slice-a", state="pending", gate_state="pending")
    registry.update_slice("slice-a", state="needs_human", gate_state="needs_human")
    evolved = registry.get_slice("slice-a")

    assert _binding_snapshot(evolved) != _binding_snapshot(slice_row)
    assert {
        key: value
        for key, value in _binding_snapshot(evolved).items()
        if key != "binding_revision"
    } == {
        key: value
        for key, value in _binding_snapshot(slice_row).items()
        if key != "binding_revision"
    }

    with pytest.raises(ValueError, match="stale-binding"):
        registry.prepare_recovery("slice-a", request=request)

    current_request = _recovery_request(evolved, request_id="recovery-request-2")
    prepared = registry.prepare_recovery("slice-a", request=current_request)
    assert prepared["phase"] == "prepared"


@pytest.mark.parametrize("operation", ["repin", "update", "record_action"])
def test_versioned_writers_reject_exhausted_revision_without_partial_mutation(
    tmp_path: Path,
    operation: str,
) -> None:
    registry, _builder, _reviewer, _slice_row, state_path = _create_bound_slice(tmp_path)
    _set_slice_binding_revision(state_path, revision=MAX_SAFE_JSON_INTEGER)
    exhausted = JobRegistry(state_path=state_path)
    before_slice = exhausted.get_slice("slice-a")
    before_bytes = state_path.read_bytes()

    with pytest.raises(ValueError, match="binding_revision exhausted"):
        if operation == "repin":
            exhausted.repin_slice(
                "slice-a",
                spec_path="specs/slice-a.md",
                spec_hash="spec-sha",
                plan_path="plans/slice-a.md",
                plan_hash="plan-sha",
                target_branch="feature/slice-a",
                target_remote="origin",
                verification_hash="verification-hash",
                verification={"docs_class": "code"},
                dispatch_base="dispatch-base-sha",
            )
        elif operation == "update":
            exhausted.update_slice("slice-a", state="failed")
        else:
            exhausted.record_action(
                "slice-a",
                action="operator-note",
                actor="operator",
                state="failed",
            )

    assert exhausted.get_slice("slice-a") == before_slice
    assert state_path.read_bytes() == before_bytes


def test_commit_pre_candidate_recovery_supersedes_jobs_and_is_idempotent(tmp_path: Path) -> None:
    registry, builder, reviewer, slice_row, state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)
    prepared = registry.prepare_recovery("slice-a", request=request)
    completed = registry.commit_pre_candidate_recovery(
        "slice-a",
        request=request,
        step_receipts=_step_receipts(),
    )

    assert completed["phase"] == "complete"
    assert completed["version"] == RECOVERY_RECEIPT_VERSION
    assert completed["prepared_at"] == prepared["prepared_at"]
    assert completed["completed_at"]
    assert completed["result"] == "recovery-complete"
    assert completed["applied_binding"]["binding_version"] == SLICE_BINDING_VERSION
    assert completed["applied_binding"]["binding_revision"] == slice_row["binding_revision"] + 1
    assert completed["applied_binding"]["builder_job_id"] is None
    assert completed["applied_binding"]["reviewer_job_id"] is None
    assert completed["applied_binding"]["candidate"] is None
    assert completed["step_receipts"] == _step_receipts()

    refreshed = registry.get_slice("slice-a")
    assert refreshed["state"] == "pending"
    assert refreshed["gate_state"] == "pending"
    assert refreshed["builder_job_id"] is None
    assert refreshed["reviewer_job_id"] is None
    assert refreshed["candidate"] is None
    assert refreshed["binding_revision"] == slice_row["binding_revision"] + 1
    assert refreshed["recovery_receipts"] == [completed]

    builder_job = registry.get_job(builder["job_id"])
    reviewer_job = registry.get_job(reviewer["job_id"])
    for job in (builder_job, reviewer_job):
        assert job["supersession"]["version"] == JOB_SUPERSESSION_VERSION
        assert job["supersession"]["slice_id"] == "slice-a"
        assert job["supersession"]["binding_revision"] == slice_row["binding_revision"]
        assert job["supersession"]["reason"] == "recover-pre-candidate"
        assert job["supersession"]["superseding_identity"] == {
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
        }

    state_after_commit = state_path.read_bytes()
    assert registry.commit_pre_candidate_recovery(
        "slice-a",
        request=request,
        step_receipts=_step_receipts(),
    ) == completed
    assert state_path.read_bytes() == state_after_commit
    reloaded = JobRegistry(state_path=state_path)
    assert reloaded.commit_pre_candidate_recovery(
        "slice-a",
        request=request,
        step_receipts=_step_receipts(),
    ) == completed
    assert state_path.read_bytes() == state_after_commit

    conflicting_steps = _step_receipts()
    conflicting_steps[0]["sha256"] = "9" * 64
    with pytest.raises(ValueError, match="request-content-conflict"):
        reloaded.commit_pre_candidate_recovery(
            "slice-a",
            request=request,
            step_receipts=conflicting_steps,
        )


def test_commit_pre_candidate_recovery_rejects_incomplete_proof(tmp_path: Path) -> None:
    registry, _builder, _reviewer, slice_row, state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)
    registry.prepare_recovery("slice-a", request=request)
    before = state_path.read_bytes()

    with pytest.raises(ValueError, match="incomplete-recovery-proof"):
        registry.commit_pre_candidate_recovery(
            "slice-a",
            request=request,
            step_receipts=_step_receipts()[:1],
        )

    assert state_path.read_bytes() == before
    assert registry.get_slice("slice-a")["recovery_receipts"][0]["phase"] == "prepared"


def test_commit_pre_candidate_recovery_rejects_exhausted_revision_without_partial_mutation(
    tmp_path: Path,
) -> None:
    registry, builder, reviewer, _slice_row, state_path = _create_bound_slice(tmp_path)
    _set_slice_binding_revision(state_path, revision=MAX_SAFE_JSON_INTEGER)
    exhausted = JobRegistry(state_path=state_path)
    request = _recovery_request(exhausted.get_slice("slice-a"))
    exhausted.prepare_recovery("slice-a", request=request)
    before_bytes = state_path.read_bytes()
    before_slice = exhausted.get_slice("slice-a")

    with pytest.raises(ValueError, match="binding_revision exhausted"):
        exhausted.commit_pre_candidate_recovery(
            "slice-a",
            request=request,
            step_receipts=_step_receipts(),
        )

    assert exhausted.get_slice("slice-a") == before_slice
    assert exhausted.get_job(builder["job_id"]).get("supersession") is None
    assert exhausted.get_job(reviewer["job_id"]).get("supersession") is None
    assert state_path.read_bytes() == before_bytes


def test_commit_pre_candidate_recovery_persist_failure_rolls_back_prepared_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, builder, reviewer, slice_row, state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)
    registry.prepare_recovery("slice-a", request=request)
    prepared_bytes = state_path.read_bytes()

    def fail_write(_payload: object) -> None:
        raise OSError("disk-full")

    monkeypatch.setattr(registry, "_write_payload_atomically", fail_write)
    with pytest.raises(RuntimeError, match="recovery-persistence-failed"):
        registry.commit_pre_candidate_recovery(
            "slice-a",
            request=request,
            step_receipts=_step_receipts(),
        )

    assert state_path.read_bytes() == prepared_bytes
    rolled_back = registry.get_slice("slice-a")
    assert rolled_back["state"] == "needs_human"
    assert rolled_back["gate_state"] == "needs_human"
    assert rolled_back["binding_revision"] == slice_row["binding_revision"]
    assert rolled_back["recovery_receipts"][0]["phase"] == "prepared"
    assert registry.get_job(builder["job_id"]).get("supersession") is None
    assert registry.get_job(reviewer["job_id"]).get("supersession") is None


def test_commit_pre_candidate_recovery_replace_failure_rolls_back_prepared_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, builder, reviewer, slice_row, state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)
    registry.prepare_recovery("slice-a", request=request)
    prepared_bytes = state_path.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(registry_module.os, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="recovery-persistence-failed: replace failed"):
        registry.commit_pre_candidate_recovery(
            "slice-a",
            request=request,
            step_receipts=_step_receipts(),
        )

    assert state_path.read_bytes() == prepared_bytes
    rolled_back = registry.get_slice("slice-a")
    assert rolled_back["state"] == "needs_human"
    assert rolled_back["gate_state"] == "needs_human"
    assert rolled_back["binding_revision"] == slice_row["binding_revision"]
    assert rolled_back["recovery_receipts"][0]["phase"] == "prepared"
    assert registry.get_job(builder["job_id"]).get("supersession") is None
    assert registry.get_job(reviewer["job_id"]).get("supersession") is None


def test_commit_pre_candidate_recovery_directory_fsync_failure_restores_prepared_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, builder, reviewer, slice_row, state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)
    registry.prepare_recovery("slice-a", request=request)
    prepared_bytes = state_path.read_bytes()
    original_fsync = registry_module._fsync_directory
    fsync_calls = 0

    def fail_first_directory_fsync(directory: Path) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            raise OSError("directory fsync failed")
        original_fsync(directory)

    monkeypatch.setattr(registry_module, "_fsync_directory", fail_first_directory_fsync)
    with pytest.raises(RuntimeError, match="recovery-persistence-failed: directory fsync failed"):
        registry.commit_pre_candidate_recovery(
            "slice-a",
            request=request,
            step_receipts=_step_receipts(),
        )

    assert state_path.read_bytes() == prepared_bytes
    rolled_back = registry.get_slice("slice-a")
    assert rolled_back["state"] == "needs_human"
    assert rolled_back["gate_state"] == "needs_human"
    assert rolled_back["binding_revision"] == slice_row["binding_revision"]
    assert rolled_back["recovery_receipts"][0]["phase"] == "prepared"
    assert registry.get_job(builder["job_id"]).get("supersession") is None
    assert registry.get_job(reviewer["job_id"]).get("supersession") is None


def test_commit_pre_candidate_recovery_rollback_failure_surfaces_fatal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _builder, _reviewer, slice_row, _state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)
    registry.prepare_recovery("slice-a", request=request)

    def fail_all_directory_fsync(_directory: Path) -> None:
        raise OSError("directory fsync failed")

    monkeypatch.setattr(registry_module, "_fsync_directory", fail_all_directory_fsync)
    with pytest.raises(
        RuntimeError,
        match=(
            "recovery-persistence-failed: "
            "coordinator state rollback failed after durability fault"
        ),
    ):
        registry.commit_pre_candidate_recovery(
            "slice-a",
            request=request,
            step_receipts=_step_receipts(),
        )


def test_commit_pre_candidate_recovery_conflict_does_not_leave_partial_job_mutations(
    tmp_path: Path,
) -> None:
    registry, builder, reviewer, slice_row, state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)
    registry.prepare_recovery("slice-a", request=request)
    registry.record_job_supersession(
        reviewer["job_id"],
        slice_id="slice-a",
        binding_revision=slice_row["binding_revision"],
        actor="operator",
        reason="different-recovery",
        superseding_identity={"request_id": "other", "request_digest": "e" * 64},
        at="2026-09-21T09:00:00+00:00",
    )

    with pytest.raises(ValueError, match="request-content-conflict"):
        registry.commit_pre_candidate_recovery(
            "slice-a",
            request=request,
            step_receipts=_step_receipts(),
        )

    assert registry.get_job(builder["job_id"]).get("supersession") is None
    registry.create_job(
        task="unrelated",
        persona="builder",
        branch="feature/unrelated",
        pane="",
        worktree=str(tmp_path / "wt" / "unrelated"),
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    builder_row = next(job for job in payload["jobs"] if job["job_id"] == builder["job_id"])
    assert "supersession" not in builder_row


def test_recovery_receipt_reload_rejects_forged_applied_binding(tmp_path: Path) -> None:
    registry, builder, _reviewer, slice_row, state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)
    registry.prepare_recovery("slice-a", request=request)
    registry.commit_pre_candidate_recovery("slice-a", request=request, step_receipts=_step_receipts())

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["slices"][0]["recovery_receipts"][0]["applied_binding"]["builder_job_id"] = builder["job_id"]
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="request-content-conflict"):
        JobRegistry(state_path=state_path)


def test_recovery_receipt_and_lookup_copies_do_not_alias_registry_state(tmp_path: Path) -> None:
    registry, _builder, _reviewer, slice_row, _state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)
    registry.prepare_recovery("slice-a", request=request)

    copied_slice = registry.get_slice("slice-a")
    copied_slice["recovery_receipts"][0]["payload"]["required_steps"].append("tampered")
    copied_lookup = registry.lookup_registry_request_receipt(request["request_id"])
    assert copied_lookup is not None
    copied_lookup["receipt"]["payload"]["target"]["work_id"] = "tampered"

    fresh_slice = registry.get_slice("slice-a")
    assert fresh_slice["recovery_receipts"][0]["payload"]["required_steps"] == [
        "reclaim-worktree",
        "supersede-handoff",
    ]
    assert registry.lookup_registry_request_receipt(request["request_id"]) == {
        "slice_id": "slice-a",
        "kind": "recovery",
        "receipt": fresh_slice["recovery_receipts"][0],
    }


def test_job_consumption_is_idempotent_and_survives_supersession(tmp_path: Path) -> None:
    registry, builder, reviewer, slice_row, _state_path = _create_bound_slice(tmp_path)
    unrelated = _create_job(
        registry,
        task="unrelated-builder",
        worktree=tmp_path / "wt" / "unrelated-builder",
    )
    consumed = registry.record_job_consumption(
        builder["job_id"],
        slice_id="slice-a",
        binding_revision=slice_row["binding_revision"],
        actor="operator",
        completion_identity={"request_id": "consume-1", "request_digest": "5" * 64},
        proof_refs=[_proof_ref("evidence/consume.json", "6" * 64)],
        at="2026-09-21T08:00:00+00:00",
    )
    assert consumed["consumption"]["version"] == JOB_CONSUMPTION_VERSION
    assert consumed["consumption"]["proof_refs"] == [_proof_ref("evidence/consume.json", "6" * 64)]

    replayed = registry.record_job_consumption(
        builder["job_id"],
        slice_id="slice-a",
        binding_revision=slice_row["binding_revision"],
        actor="operator",
        completion_identity={"request_id": "consume-1", "request_digest": "5" * 64},
        proof_refs=[_proof_ref("evidence/consume.json", "6" * 64)],
        at="2026-09-21T08:00:00+00:00",
    )
    assert replayed == consumed

    updated = registry.record_job_supersession(
        builder["job_id"],
        slice_id="slice-a",
        binding_revision=slice_row["binding_revision"],
        actor="operator",
        reason="manual-rebind",
        superseding_identity={"request_id": "rebalance-1", "request_digest": "7" * 64},
        at="2026-09-21T08:10:00+00:00",
    )
    assert updated["consumption"]["version"] == JOB_CONSUMPTION_VERSION
    assert updated["supersession"]["version"] == JOB_SUPERSESSION_VERSION

    with pytest.raises(ValueError, match="request-content-conflict"):
        registry.record_job_consumption(
            builder["job_id"],
            slice_id="slice-a",
            binding_revision=slice_row["binding_revision"],
            actor="operator",
            completion_identity={"request_id": "consume-1", "request_digest": "5" * 64},
            proof_refs=[_proof_ref("evidence/consume-other.json", "8" * 64)],
            at="2026-09-21T08:00:00+00:00",
        )

    with pytest.raises(ValueError, match="request-content-conflict"):
        registry.record_job_consumption(
            unrelated["job_id"],
            slice_id="slice-a",
            binding_revision=slice_row["binding_revision"],
            actor="operator",
            completion_identity={"request_id": "consume-2", "request_digest": "9" * 64},
            proof_refs=[_proof_ref("evidence/unrelated.json", "a" * 64)],
        )
    with pytest.raises(ValueError, match="request-content-conflict"):
        registry.record_job_supersession(
            unrelated["job_id"],
            slice_id="slice-a",
            binding_revision=slice_row["binding_revision"],
            actor="operator",
            reason="wrong-slice",
            superseding_identity={"request_id": "rebalance-2", "request_digest": "b" * 64},
        )
    with pytest.raises(ValueError, match="malformed-recovery-context"):
        registry.record_job_consumption(
            builder["job_id"],
            slice_id="slice-a",
            binding_revision=slice_row["binding_revision"],
            actor="operator",
            completion_identity={"opaque": "bad"},
            proof_refs=[_proof_ref("evidence/identity.json", "1" * 64)],
        )
    with pytest.raises(ValueError, match="malformed-recovery-context"):
        registry.record_job_supersession(
            reviewer["job_id"],
            slice_id="slice-a",
            binding_revision=slice_row["binding_revision"],
            actor="operator",
            reason="bad-identity",
            superseding_identity={"opaque": "bad"},
        )


def test_job_consumption_reload_rejects_wrong_binding_reference(tmp_path: Path) -> None:
    registry, builder, _reviewer, slice_row, state_path = _create_bound_slice(tmp_path)
    registry.record_job_consumption(
        builder["job_id"],
        slice_id="slice-a",
        binding_revision=slice_row["binding_revision"],
        actor="operator",
        completion_identity={"request_id": "consume-1", "request_digest": "c" * 64},
        proof_refs=[_proof_ref("evidence/consume.json", "d" * 64)],
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["jobs"][0]["consumption"]["binding_revision"] = slice_row["binding_revision"] + 99
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="request-content-conflict"):
        JobRegistry(state_path=state_path)


def test_job_supersession_reload_rejects_wrong_binding_reference(tmp_path: Path) -> None:
    registry, builder, _reviewer, slice_row, state_path = _create_bound_slice(tmp_path)
    request = _recovery_request(slice_row)
    registry.prepare_recovery("slice-a", request=request)
    registry.commit_pre_candidate_recovery("slice-a", request=request, step_receipts=_step_receipts())

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["jobs"][0]["supersession"]["slice_id"] = "wrong-slice"
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="request-content-conflict"):
        JobRegistry(state_path=state_path)


def test_checkpoint_legacy_binding_golden_vectors_preserve_row_and_replay(tmp_path: Path) -> None:
    registry, _builder, _reviewer, slice_row, state_path = _create_legacy_registry(tmp_path)
    request = _checkpoint_request(slice_row)

    assert request["payload"]["legacy_snapshot_fingerprint"] == EXPECTED_CHECKPOINT_FINGERPRINT
    assert request["request_digest"] == EXPECTED_CHECKPOINT_REQUEST_DIGEST
    before = state_path.read_bytes()
    receipt = registry.checkpoint_legacy_binding("legacy-a", request=request)
    assert receipt["version"] == CHECKPOINT_RECEIPT_VERSION
    assert receipt["phase"] == "complete"
    assert receipt["result"] == "checkpoint-created"
    assert receipt["applied_binding"]["binding_version"] == SLICE_BINDING_VERSION
    assert receipt["applied_binding"]["binding_revision"] == 1
    assert registry.lookup_registry_request_receipt(request["request_id"]) == {
        "slice_id": "legacy-a",
        "kind": "checkpoint",
        "receipt": receipt,
    }

    checkpointed = registry.get_slice("legacy-a")
    assert checkpointed["binding_version"] == SLICE_BINDING_VERSION
    assert checkpointed["binding_revision"] == 1
    assert checkpointed["binding_checkpoint_receipt"] == receipt
    assert checkpointed["recovery_receipts"] == []
    assert checkpointed["candidate"] == slice_row["candidate"]
    assert checkpointed["updated_at"] == slice_row["updated_at"]
    assert checkpointed["legacy_metadata"] == slice_row["legacy_metadata"]

    bytes_after_checkpoint = state_path.read_bytes()
    assert bytes_after_checkpoint != before
    reloaded = JobRegistry(state_path=state_path)
    assert reloaded.checkpoint_legacy_binding("legacy-a", request=request) == receipt
    assert state_path.read_bytes() == bytes_after_checkpoint


def test_checkpoint_rejects_forged_fingerprint_cross_kind_reuse_and_recheckpoint(tmp_path: Path) -> None:
    registry, _builder, _reviewer, slice_row, _state_path = _create_legacy_registry(tmp_path)
    request = _checkpoint_request(slice_row)

    forged = _deepcopy(request)
    forged["payload"]["legacy_snapshot_fingerprint"] = "0" * 64
    forged["request_digest"] = _prefixed_digest(
        CHECKPOINT_REQUEST_VERSION,
        {"version": CHECKPOINT_REQUEST_VERSION, "request_id": forged["request_id"], "payload": forged["payload"]},
    )
    with pytest.raises(ValueError, match="request-content-conflict"):
        registry.checkpoint_legacy_binding("legacy-a", request=forged)

    checkpointed = registry.checkpoint_legacy_binding("legacy-a", request=request)
    assert checkpointed["result"] == "checkpoint-created"

    with pytest.raises(ValueError, match="legacy-checkpoint-not-applicable"):
        registry.checkpoint_legacy_binding(
            "legacy-a",
            request=_checkpoint_request(registry.get_slice("legacy-a"), request_id="checkpoint-request-2"),
        )

    builder_b = _create_job(
        registry,
        task="slice-b-builder",
        worktree=tmp_path / "wt" / "slice-b-builder",
    )
    reviewer_b = _create_job(
        registry,
        task="slice-b-reviewer",
        worktree=tmp_path / "wt" / "slice-b-reviewer",
    )
    registry.create_slice(
        slice_id="slice-b",
        spec_path="specs/slice-b.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-b.md",
        plan_hash="plan-sha",
        target_branch="feature/slice-b",
        target_remote="origin",
        verification_hash="verification-hash",
        verification={"docs_class": "code"},
        dispatch_base="dispatch-base-sha",
        builder_job_id=builder_b["job_id"],
        reviewer_job_id=reviewer_b["job_id"],
        candidate=None,
    )
    registry.update_slice("slice-b", state="needs_human", gate_state="needs_human")
    recovery_slice = registry.get_slice("slice-b")
    with pytest.raises(ValueError, match="request-content-conflict"):
        registry.prepare_recovery(
            "slice-b",
            request=_recovery_request(recovery_slice, request_id=request["request_id"]),
        )


def test_checkpoint_legacy_binding_persist_failure_rolls_back_legacy_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _builder, _reviewer, slice_row, state_path = _create_legacy_registry(tmp_path)
    request = _checkpoint_request(slice_row)
    before_bytes = state_path.read_bytes()

    def fail_write(_payload: object) -> None:
        raise OSError("disk-full")

    monkeypatch.setattr(registry, "_write_payload_atomically", fail_write)
    with pytest.raises(RuntimeError, match="recovery-persistence-failed"):
        registry.checkpoint_legacy_binding("legacy-a", request=request)

    assert state_path.read_bytes() == before_bytes
    rolled_back = registry.get_slice("legacy-a")
    assert "binding_version" not in rolled_back
    assert "binding_revision" not in rolled_back
    assert "binding_checkpoint_receipt" not in rolled_back


def test_checkpoint_request_validates_provenance_job_refs_and_request_version(tmp_path: Path) -> None:
    registry, _builder, _reviewer, slice_row, _state_path = _create_legacy_registry(tmp_path)
    request = _checkpoint_request(slice_row)

    missing_provenance = _deepcopy(request)
    missing_provenance["payload"]["provenance"].pop("proof_refs")
    missing_provenance["request_digest"] = _prefixed_digest(
        CHECKPOINT_REQUEST_VERSION,
        {
            "version": CHECKPOINT_REQUEST_VERSION,
            "request_id": missing_provenance["request_id"],
            "payload": missing_provenance["payload"],
        },
    )
    with pytest.raises(ValueError, match="malformed-recovery-context"):
        registry.checkpoint_legacy_binding("legacy-a", request=missing_provenance)

    altered_job_refs = _deepcopy(request)
    altered_job_refs["payload"]["expected_job_refs"]["reviewer_job_id"] = None
    altered_job_refs["request_digest"] = _prefixed_digest(
        CHECKPOINT_REQUEST_VERSION,
        {
            "version": CHECKPOINT_REQUEST_VERSION,
            "request_id": altered_job_refs["request_id"],
            "payload": altered_job_refs["payload"],
        },
    )
    with pytest.raises(ValueError, match="request-content-conflict"):
        registry.checkpoint_legacy_binding("legacy-a", request=altered_job_refs)

    bad_version = _deepcopy(request)
    bad_version["version"] = "cortex/legacy-binding-checkpoint-request/v0"
    with pytest.raises(ValueError, match="unsupported-recovery-version"):
        registry.checkpoint_legacy_binding("legacy-a", request=bad_version)


def test_checkpoint_rejects_complete_legacy_row_drift(tmp_path: Path) -> None:
    registry, _builder, _reviewer, slice_row, _state_path = _create_legacy_registry(tmp_path)
    request = _checkpoint_request(slice_row)

    registry.update_slice(
        "legacy-a",
        current_evidence_refs=["evidence/drifted.json"],
    )

    with pytest.raises(ValueError, match="stale-binding"):
        registry.checkpoint_legacy_binding("legacy-a", request=request)


def test_checkpoint_receipt_reload_rejects_rewired_applied_binding(tmp_path: Path) -> None:
    registry, _builder, _reviewer, slice_row, state_path = _create_legacy_registry(tmp_path)
    request = _checkpoint_request(slice_row)
    registry.checkpoint_legacy_binding("legacy-a", request=request)

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["slices"][0]["binding_checkpoint_receipt"]["applied_binding"]["candidate"] = None
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="request-content-conflict"):
        JobRegistry(state_path=state_path)


def test_checkpoint_receipt_reload_rejects_unknown_version(tmp_path: Path) -> None:
    registry, _builder, _reviewer, slice_row, state_path = _create_legacy_registry(tmp_path)
    request = _checkpoint_request(slice_row)
    registry.checkpoint_legacy_binding("legacy-a", request=request)

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["slices"][0]["binding_checkpoint_receipt"]["version"] = (
        "cortex/legacy-binding-checkpoint-receipt/v0"
    )
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported-recovery-version"):
        JobRegistry(state_path=state_path)


def test_checkpoint_request_rejects_cyclic_native_payload(tmp_path: Path) -> None:
    registry, _builder, _reviewer, slice_row, _state_path = _create_legacy_registry(tmp_path)
    request = _checkpoint_request(slice_row)
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle
    request["payload"]["expected_legacy_row"]["cycle"] = cycle

    with pytest.raises(ValueError, match="malformed-recovery-context"):
        registry.checkpoint_legacy_binding("legacy-a", request=request)


def test_checkpoint_receipt_and_slice_copies_do_not_alias_registry_state(tmp_path: Path) -> None:
    registry, _builder, _reviewer, slice_row, _state_path = _create_legacy_registry(tmp_path)
    request = _checkpoint_request(slice_row)
    registry.checkpoint_legacy_binding("legacy-a", request=request)

    copied_slice = registry.get_slice("legacy-a")
    copied_slice["binding_checkpoint_receipt"]["payload"]["provenance"]["proof_refs"].append(
        _proof_ref("evidence/tampered.json", "f" * 64)
    )
    copied_lookup = registry.lookup_registry_request_receipt(request["request_id"])
    assert copied_lookup is not None
    copied_lookup["receipt"]["payload"]["expected_legacy_row"]["legacy_metadata"]["confirmed"] = False

    fresh_slice = registry.get_slice("legacy-a")
    assert fresh_slice["binding_checkpoint_receipt"]["payload"]["provenance"]["proof_refs"] == [
        _proof_ref("evidence/legacy-binding.json", "3" * 64),
        _proof_ref("evidence/owner-approval.json", "4" * 64),
    ]
    assert fresh_slice["binding_checkpoint_receipt"]["payload"]["expected_legacy_row"]["legacy_metadata"] == {
        "confirmed": True,
        "score": -0.0,
    }


def test_checkpoint_replay_after_newer_binding_revision_does_not_roll_back_generation(
    tmp_path: Path,
) -> None:
    registry, _builder, _reviewer, slice_row, _state_path = _create_legacy_registry(tmp_path)
    request = _checkpoint_request(slice_row)
    receipt = registry.checkpoint_legacy_binding("legacy-a", request=request)

    repinned = registry.repin_slice(
        "legacy-a",
        spec_path=slice_row["spec"]["path"],
        spec_hash=slice_row["spec"]["hash"],
        plan_path=slice_row["plan"]["path"],
        plan_hash=slice_row["plan"]["hash"],
        target_branch=slice_row["target_branch"],
        target_remote=slice_row["target_remote"],
        verification_hash=slice_row["verification"]["hash"],
        verification=slice_row["verification"]["contract"],
        dispatch_base=slice_row["dispatch_base"],
    )
    assert repinned["binding_revision"] == 2

    replayed = registry.checkpoint_legacy_binding("legacy-a", request=request)
    assert replayed == receipt
    assert registry.get_slice("legacy-a")["binding_revision"] == 2


def test_checkpoint_only_starts_revision_counting_from_the_new_observation_origin(
    tmp_path: Path,
) -> None:
    registry, _builder, _reviewer, slice_row, state_path = _create_legacy_registry(tmp_path)
    request = _checkpoint_request(slice_row)
    original_bytes = state_path.read_bytes()
    payload = json.loads(original_bytes.decode("utf-8"))
    payload["slices"][0]["legacy_metadata"]["confirmed"] = False
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    state_path.write_bytes(original_bytes)

    reloaded = JobRegistry(state_path=state_path)
    assert reloaded.get_slice("legacy-a").get("binding_revision") is None

    receipt = reloaded.checkpoint_legacy_binding("legacy-a", request=request)
    assert receipt["applied_binding"]["binding_revision"] == 1
    assert reloaded.get_slice("legacy-a")["binding_revision"] == 1
