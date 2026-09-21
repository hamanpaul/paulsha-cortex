"""#497 / T06 RED：凍結 recovery registry receipt 的 additive load/copy 契約。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from paulsha_cortex.coordinator import verification
from paulsha_cortex.coordinator.registry import JobRegistry


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _recovery_request(
    *,
    request_id: str = "recovery-request-1",
    target_branch: str = "feature/slice-a",
    candidate: str = "candidate-sha",
    proof_ref: str = "evidence/recover-1.json",
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "caller": "builder",
        "target": {
            "slice_id": "slice-a",
            "target_branch": target_branch,
            "candidate": candidate,
        },
        "proof_requirements": {
            "required_refs": [proof_ref],
            "must_match_candidate": candidate,
        },
        "context": {
            "actor": "operator",
            "job_id": "builder-1",
            "workflow_run_id": "workflow-1",
            "legacy_fingerprint": "legacy-fingerprint-1",
        },
    }


def _stored_receipt(
    request: dict[str, Any],
    *,
    version: int = 1,
    receipt_kind: str = "recovery",
    recorded_at: str = "2026-09-21T00:00:00+00:00",
) -> dict[str, Any]:
    return {
        "version": version,
        "receipt_kind": receipt_kind,
        "request": request,
        "request_bytes": _canonical_json_bytes(request).hex(),
        "payload_digest": verification.canonical_json_hash(request),
        "recorded_at": recorded_at,
    }


def _stored_disposition(
    *,
    version: int = 1,
    request_id: str = "recovery-request-1",
    status: str = "completed",
    completed_at: str = "2026-09-21T00:00:00+00:00",
) -> dict[str, Any]:
    return {
        "version": version,
        "request_id": request_id,
        "status": status,
        "completed_at": completed_at,
    }


def _slice_row(
    *,
    recovery_receipts: list[dict[str, Any]] | None = None,
    recovery_dispositions: list[dict[str, Any]] | None = None,
    recovery_checkpoints: list[dict[str, Any]] | None = None,
    recovery_receipt_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "slice_id": "slice-a",
        "spec": {"path": "specs/slice-a.md", "hash": "spec-sha"},
        "plan": {"path": "plans/slice-a.md", "hash": "plan-sha"},
        "target_branch": "feature/slice-a",
        "target_remote": "origin",
        "dispatch_base": "base-sha",
        "builder_job_id": None,
        "reviewer_job_id": None,
        "candidate": "candidate-sha",
        "state": "needs_human",
        "gate_state": "failed",
        "verification": {"hash": "0" * 64, "contract": None},
        "current_verification_evidence_hash": None,
        "current_evidence_refs": [],
        "current_evaluation_refs": [],
        "evidence_history": [],
        "evaluation_history": [],
        "actions": [],
        "created_at": "2026-09-21T00:00:00+00:00",
        "updated_at": "2026-09-21T00:00:00+00:00",
        "recovery_receipts": recovery_receipts or [],
        "recovery_dispositions": recovery_dispositions or [],
        "recovery_checkpoints": recovery_checkpoints or [],
        "recovery_receipt_history": recovery_receipt_history or [],
    }


def _state_payload(slice_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "seq": 0,
        "jobs": [],
        "slices": [slice_row],
        "workflows": [],
        "legacy_records": {"source_schema_version": 1, "seq": 0, "jobs": [], "slices": []},
        "reclaim_resets": [],
    }


def _write_state(state_path: Path, payload: dict[str, Any]) -> bytes:
    original = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    state_path.write_bytes(original)
    return original


def _mutate_bad_digest(receipt: dict[str, Any]) -> None:
    receipt["payload_digest"] = "0" * 64


def _mutate_bad_target(receipt: dict[str, Any]) -> None:
    receipt["request"]["target"] = {
        "slice_id": "slice-a",
        "candidate": "candidate-sha",
    }


def _mutate_missing_disposition_version(disposition: dict[str, Any]) -> None:
    disposition.pop("version")


def _mutate_bad_disposition_version(disposition: dict[str, Any]) -> None:
    disposition["version"] = 99


def _mutate_unknown_disposition_key(disposition: dict[str, Any]) -> None:
    disposition["unexpected"] = "nope"


def test_legacy_receipt_bytes_complete_golden_request_and_history_survive_read_reload(
    tmp_path: Path,
) -> None:
    state = tmp_path / "jobs.json"
    current_request = _recovery_request()
    historical_request = _recovery_request(
        request_id="recovery-request-0",
        proof_ref="evidence/recover-0.json",
    )
    current = _stored_receipt(current_request)
    historical = _stored_receipt(
        historical_request,
        recorded_at="2026-09-20T00:00:00+00:00",
    )
    dispositions = [
        _stored_disposition(
            request_id=historical_request["request_id"],
            completed_at=historical["recorded_at"],
        )
    ]
    payload = _state_payload(
        _slice_row(
            recovery_receipts=[current],
            recovery_dispositions=dispositions,
            recovery_receipt_history=[historical],
        )
    )
    original = _write_state(state, payload)

    registry = JobRegistry(state_path=state)
    loaded = registry.get_slice("slice-a")
    assert loaded["recovery_receipts"] == [current]
    assert loaded["recovery_dispositions"] == dispositions
    assert loaded["recovery_receipt_history"] == [historical]
    assert loaded["recovery_receipts"][0]["request"] == current_request
    assert loaded["recovery_receipts"][0]["request_bytes"] == _canonical_json_bytes(
        current_request
    ).hex()
    assert loaded["recovery_receipts"][0]["payload_digest"] == verification.canonical_json_hash(
        current_request
    )

    reloaded = JobRegistry(state_path=state)
    replayed = reloaded.get_slice("slice-a")
    assert replayed["recovery_receipts"] == [current]
    assert replayed["recovery_receipt_history"] == [historical]
    assert state.read_bytes() == original


def test_unknown_recovery_receipt_version_is_rejected_fail_closed(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    payload = _state_payload(
        _slice_row(
            recovery_receipts=[_stored_receipt(_recovery_request(), version=99)],
        )
    )
    _write_state(state, payload)

    with pytest.raises(ValueError, match="receipt|version"):
        JobRegistry(state_path=state)


@pytest.mark.parametrize(
    "mutator",
    [
        _mutate_bad_disposition_version,
        _mutate_missing_disposition_version,
        _mutate_unknown_disposition_key,
    ],
)
def test_recovery_disposition_contract_is_versioned_and_exact_key_fail_closed(
    tmp_path: Path,
    mutator,
) -> None:
    state = tmp_path / "jobs.json"
    disposition = _stored_disposition()
    mutator(disposition)
    payload = _state_payload(_slice_row(recovery_dispositions=[disposition]))
    _write_state(state, payload)

    with pytest.raises(ValueError, match="recovery disposition|version"):
        JobRegistry(state_path=state)


def test_request_id_collision_across_receipt_kinds_is_rejected_fail_closed(
    tmp_path: Path,
) -> None:
    state = tmp_path / "jobs.json"
    request = _recovery_request()
    payload = _state_payload(
        _slice_row(
            recovery_receipts=[_stored_receipt(request, receipt_kind="recovery")],
            recovery_checkpoints=[_stored_receipt(request, receipt_kind="checkpoint")],
        )
    )
    _write_state(state, payload)

    with pytest.raises(ValueError, match="receipt|request_id|collision"):
        JobRegistry(state_path=state)


@pytest.mark.parametrize("mutator", [_mutate_bad_digest, _mutate_bad_target])
def test_recovery_receipt_digest_and_target_contract_is_validated_fail_closed(
    tmp_path: Path,
    mutator,
) -> None:
    state = tmp_path / "jobs.json"
    receipt = _stored_receipt(_recovery_request())
    mutator(receipt)
    payload = _state_payload(_slice_row(recovery_receipts=[receipt]))
    _write_state(state, payload)

    with pytest.raises(ValueError, match="receipt|digest|target"):
        JobRegistry(state_path=state)


def test_recovery_receipt_nested_mutation_does_not_alias_live_registry_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "jobs.json"
    receipt = _stored_receipt(_recovery_request())
    payload = _state_payload(_slice_row(recovery_receipts=[receipt]))
    _write_state(state, payload)

    registry = JobRegistry(state_path=state)
    observed = registry.get_slice("slice-a")
    observed["recovery_receipts"][0]["request"]["proof_requirements"]["required_refs"].append(
        "evidence/mutated.json"
    )

    fresh = registry.get_slice("slice-a")
    assert fresh["recovery_receipts"][0]["request"]["proof_requirements"]["required_refs"] == [
        "evidence/recover-1.json"
    ]

    reloaded = JobRegistry(state_path=state)
    assert reloaded.get_slice("slice-a")["recovery_receipts"][0]["request"] == _recovery_request()
