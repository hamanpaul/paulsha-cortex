from __future__ import annotations

import builtins
from datetime import datetime, timedelta, timezone
import importlib
import sys
import threading
import time

from paulsha_cortex.control import constants, contract


def test_submit_request_writes_atomically(monkeypatch, tmp_path):
    from paulsha_cortex.control import client

    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))

    real_rename = contract.os.rename
    rename_calls: list[tuple[str, str, bool, bool]] = []

    def spy_rename(src, dst):
        rename_calls.append((str(src), str(dst), constants.requests_dir().glob("*.tmp") is not None, Path(src).exists(), Path(dst).exists()))
        return real_rename(src, dst)

    from pathlib import Path

    monkeypatch.setattr(contract.os, "rename", spy_rename)

    req_id = client.submit_request("tick", {"executor": "copilot"}, "cockpit")

    request_path = constants.requests_dir() / f"{req_id}.json"
    written = contract.read_json(request_path)

    assert written is not None
    assert written["req_id"] == req_id
    assert written["type"] == "tick"
    assert written["requested_by"] == "cockpit"
    assert rename_calls
    assert rename_calls[0][3] is True
    assert rename_calls[0][4] is False
    assert list(constants.requests_dir().glob("*.tmp")) == []


def test_concurrent_submit_request_do_not_overwrite(monkeypatch, tmp_path):
    from paulsha_cortex.control import client

    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))

    barrier = threading.Barrier(2)
    req_ids: list[str] = []

    def submit() -> None:
        barrier.wait()
        req_ids.append(client.submit_request("tick", {"executor": "copilot"}, "cockpit"))

    threads = [threading.Thread(target=submit), threading.Thread(target=submit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(req_ids) == 2
    assert len(set(req_ids)) == 2
    assert sorted(path.name for path in constants.requests_dir().glob("*.json")) == sorted(
        f"{req_id}.json" for req_id in req_ids
    )


def test_read_status_degrades_on_missing_or_stale_file(monkeypatch, tmp_path):
    from paulsha_cortex.control import client

    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))

    missing = client.read_status()
    assert missing["degraded"] is True
    assert missing["degraded_reason"] == "missing"
    assert missing["ready"] == []

    contract.atomic_write_json(
        constants.status_path(),
        contract.build_status(
            ready=["slice-a"],
            in_flight=[{"job_id": "job-1", "slice_id": "slice-a", "state": "running"}],
            recent_done=[{"slice_id": "slice-b", "gate_status": "passed", "at": "2026-07-03T09:00:00+00:00"}],
            daemon={"pid": 1, "last_tick_at": "2026-07-03T09:00:00+00:00", "idle": False},
            updated_at="2000-01-01T00:00:00+00:00",
        ),
    )

    stale = client.read_status()
    assert stale["degraded"] is True
    assert stale["degraded_reason"] == "stale"
    assert stale["ready"] == []
    assert stale["in_flight"] == []
    assert stale["recent_done"] == []


def test_read_status_uses_runtime_stale_threshold(monkeypatch, tmp_path):
    from paulsha_cortex.control import client

    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("PSC_CONTROL_STATUS_STALE_AFTER_SECONDS", "1")
    importlib.reload(constants)
    importlib.reload(client)

    fresh_updated_at = datetime.now(timezone.utc).isoformat()
    stale_updated_at = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()

    contract.atomic_write_json(
        constants.status_path(),
        contract.build_status(
            ready=["slice-a"],
            in_flight=[],
            recent_done=[],
            daemon={"pid": 1, "last_tick_at": fresh_updated_at, "idle": False},
            updated_at=fresh_updated_at,
        ),
    )
    fresh = client.read_status()

    contract.atomic_write_json(
        constants.status_path(),
        contract.build_status(
            ready=["slice-a"],
            in_flight=[],
            recent_done=[],
            daemon={"pid": 1, "last_tick_at": stale_updated_at, "idle": False},
            updated_at=stale_updated_at,
        ),
    )
    stale = client.read_status()

    assert fresh["degraded"] is False
    assert stale["degraded"] is True
    assert stale["degraded_reason"] == "stale"

    monkeypatch.delenv("PSC_CONTROL_STATUS_STALE_AFTER_SECONDS", raising=False)
    importlib.reload(constants)
    importlib.reload(client)


def test_read_status_preserves_held_items(monkeypatch, tmp_path):
    import os

    from paulsha_cortex.control import client

    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    updated_at = datetime.now(timezone.utc).isoformat()
    payload = contract.build_status(
        ready=["slice-a"],
        in_flight=[],
        recent_done=[],
        daemon={"pid": os.getpid(), "last_tick_at": updated_at, "idle": False},
        updated_at=updated_at,
    )
    payload["held"] = [{"slice_id": "slice-held", "reasons": ["dispatch-hold"]}]
    contract.atomic_write_json(constants.status_path(), payload)

    status = client.read_status()

    assert status["held"] == [{"slice_id": "slice-held", "reasons": ["dispatch-hold"]}]


def test_read_status_normalizes_missing_held_to_empty_list(monkeypatch, tmp_path):
    import os

    from paulsha_cortex.control import client

    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    updated_at = datetime.now(timezone.utc).isoformat()
    contract.atomic_write_json(
        constants.status_path(),
        contract.build_status(
            ready=["slice-a"],
            in_flight=[],
            recent_done=[],
            daemon={"pid": os.getpid(), "last_tick_at": updated_at, "idle": False},
            updated_at=updated_at,
        ),
    )

    status = client.read_status()

    assert status["held"] == []


def test_read_status_preserves_held_on_degraded_snapshots(monkeypatch, tmp_path):
    import os

    from paulsha_cortex.control import client

    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    held = [{"slice_id": "slice-held", "reasons": ["dispatch-hold"]}]

    stale_updated_at = "2000-01-01T00:00:00+00:00"
    contract.atomic_write_json(
        constants.status_path(),
        {
            "schema_version": constants.SCHEMA_VERSION,
            "updated_at": stale_updated_at,
            "daemon": {"pid": 424242, "last_tick_at": stale_updated_at, "idle": False},
            "ready": ["slice-a"],
            "held": held,
            "in_flight": [],
            "recent_done": [],
        },
    )

    monkeypatch.setattr(client.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    stale = client.read_status()
    assert stale["degraded"] is True
    assert stale["held"] == held

    live_updated_at = (
        datetime.now(timezone.utc) - timedelta(seconds=constants.STATUS_STALLED_AFTER_SECONDS + 30)
    ).isoformat()
    contract.atomic_write_json(
        constants.status_path(),
        {
            "schema_version": constants.SCHEMA_VERSION,
            "updated_at": live_updated_at,
            "daemon": {"pid": os.getpid(), "last_tick_at": live_updated_at, "idle": False},
            "ready": ["slice-a"],
            "held": held,
            "in_flight": [],
            "recent_done": [],
        },
    )

    stalled = client.read_status()
    assert stalled["degraded"] is True
    assert stalled["held"] == held


def test_poll_done_returns_record_or_none(monkeypatch, tmp_path):
    from paulsha_cortex.control import client

    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))

    req = contract.build_request(req_type="tick", args={}, requested_by="telegram")
    done_payload = contract.build_done(req_id=req["req_id"], status="ok", result={"completed": []})

    def delayed_write() -> None:
        time.sleep(0.05)
        contract.atomic_write_json(constants.done_dir() / f"{req['req_id']}.json", done_payload)

    thread = threading.Thread(target=delayed_write)
    thread.start()
    found = client.poll_done(req["req_id"], timeout=0.5, poll_interval=0.01)
    thread.join()

    assert found == done_payload
    assert client.poll_done("missing", timeout=0.0, poll_interval=0.0) is None


def test_control_plane_coordinator_submits_dispatch_request(monkeypatch, tmp_path):
    from paulsha_cortex.control import client

    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))

    coordinator = client.ControlPlaneCoordinator()

    job = coordinator.create_job(
        phase="stage1",
        scope="slice-a",
        payload={"specs_dir": "/repo/specs", "force_hold": True, "ignored": "value"},
    )

    request_path = constants.requests_dir() / f"{job['job_id']}.json"
    request = contract.read_json(request_path)

    assert job == {"job_id": job["job_id"], "phase": "stage1", "scope": "slice-a"}
    assert request is not None
    assert request["type"] == "dispatch"
    assert request["requested_by"] == "telegram"
    assert request["args"] == {
        "slice_id": "slice-a",
        "specs_dir": "/repo/specs",
        "force_hold": True,
    }


def test_control_plane_coordinator_wait_done_reads_done(monkeypatch, tmp_path):
    from paulsha_cortex.control import client

    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))

    coordinator = client.ControlPlaneCoordinator()
    done_payload = contract.build_done(
        req_id="req-123",
        status="ok",
        result={"job_id": "job-1", "slice_id": "slice-a"},
    )
    contract.atomic_write_json(constants.done_dir() / "req-123.json", done_payload)

    found = coordinator.wait_done("req-123", timeout=0.0, poll_interval=0.0)

    assert found == done_payload


def test_client_module_imports_without_coordinator(monkeypatch):
    attempted: list[str] = []
    real_import = builtins.__import__

    def guard(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("paulsha_cortex.coordinator"):
            attempted.append(name)
            raise AssertionError(f"unexpected coordinator import: {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guard)
    sys.modules.pop("paulsha_cortex.control.client", None)
    importlib.import_module("paulsha_cortex.control.client")

    assert attempted == []


# ---- #184/#187 review fix: liveness-aware read_status ----

def _write_status_file(tmp_path, *, pid, age_seconds):
    from pathlib import Path
    updated_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "updated_at": updated_at,
        "daemon": {"pid": pid, "last_tick_at": None, "idle": False},
        "ready": [],
        "in_flight": [],
        "recent_done": [],
    }
    contract.atomic_write_json(constants.status_path(), payload)


def test_read_status_live_busy_daemon_not_degraded(monkeypatch, tmp_path):
    """A live-but-busy daemon (status older than the 15s window) is NOT degraded."""
    import os
    from paulsha_cortex.control import client
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    _write_status_file(tmp_path, pid=os.getpid(), age_seconds=60)  # live pid, stale age
    status = client.read_status()
    assert status["degraded"] is False
    assert status["degraded_reason"] is None


def test_read_status_dead_pid_and_stale_is_degraded(monkeypatch, tmp_path):
    from paulsha_cortex.control import client
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))

    def _dead(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(client.os, "kill", _dead)
    _write_status_file(tmp_path, pid=424242, age_seconds=60)
    status = client.read_status()
    assert status["degraded"] is True
    assert status["degraded_reason"] == "stale"


def test_read_status_live_but_stalled_is_degraded(monkeypatch, tmp_path):
    import os
    from paulsha_cortex.control import client
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    _write_status_file(
        tmp_path, pid=os.getpid(), age_seconds=constants.STATUS_STALLED_AFTER_SECONDS + 30
    )
    status = client.read_status()
    assert status["degraded"] is True
    assert status["degraded_reason"] == "stalled"
