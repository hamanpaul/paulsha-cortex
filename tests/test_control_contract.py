from __future__ import annotations

import re
import pytest
from uuid import UUID

from paulsha_cortex.control import constants, contract


def test_control_plane_paths_share_single_root(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))

    assert constants.control_root() == tmp_path
    assert constants.requests_dir() == tmp_path / "requests"
    assert constants.done_dir() == tmp_path / "done"
    assert constants.status_path() == tmp_path / "status.json"
    assert constants.SCHEMA_VERSION == 1


def test_generate_req_id_uses_utc_timestamp_and_uuid():
    req_id = contract.generate_req_id()
    match = re.fullmatch(r"(\d{8}T\d{6}Z)-([0-9a-f]{32})", req_id)

    assert match is not None
    UUID(match.group(2))


def test_request_done_and_status_round_trip_include_schema_version(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))

    request = contract.build_request(
        req_type="tick",
        args={"executor": "copilot", "allow_unsafe": False},
        requested_by="cockpit",
    )
    done = contract.build_done(
        req_id=request["req_id"],
        status="ok",
        result={"dispatched": [], "completed": [], "errors": []},
        started_at="2026-07-03T09:00:00+00:00",
    )
    status = contract.build_status(
        ready=["slice-a"],
        in_flight=[{"job_id": "job-1", "slice_id": "slice-b", "state": "running"}],
        recent_done=[{"slice_id": "slice-c", "gate_status": "passed", "at": "2026-07-03T09:05:00+00:00"}],
        daemon={"pid": 1234, "last_tick_at": "2026-07-03T09:05:00+00:00", "idle": True},
        updated_at="2026-07-03T09:05:00+00:00",
    )

    request_path = constants.requests_dir() / f"{request['req_id']}.json"
    done_path = constants.done_dir() / f"{request['req_id']}.json"
    status_path = constants.status_path()

    contract.atomic_write_json(request_path, request)
    contract.atomic_write_json(done_path, done)
    contract.atomic_write_json(status_path, status)

    assert contract.read_json(request_path) == request
    assert contract.read_json(done_path) == done
    assert contract.read_json(status_path) == status
    assert request["schema_version"] == constants.SCHEMA_VERSION
    assert done["schema_version"] == constants.SCHEMA_VERSION
    assert status["schema_version"] == constants.SCHEMA_VERSION


def test_build_dispatch_request():
    req = contract.build_request(
        req_type="dispatch",
        args={"slice_id": "s1", "handoff_dir": "runtime/handoff", "force_hold": True},
        requested_by="telegram:42",
    )
    assert req["type"] == "dispatch" and req["args"]["slice_id"] == "s1"
    assert req["args"]["handoff_dir"] == "runtime/handoff"


def test_build_complete_request():
    req = contract.build_request(
        req_type="complete",
        args={"handoff_dir": "runtime/handoff", "specs_dir": "specs"},
        requested_by="telegram:42",
    )
    assert req["type"] == "complete"
    assert req["args"]["handoff_dir"] == "runtime/handoff"


def test_unknown_type_still_raises():
    with pytest.raises(ValueError):
        contract.build_request(req_type="nope", args={}, requested_by="x")
