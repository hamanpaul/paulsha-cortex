from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

from . import constants, contract


class ControlPlaneCoordinator:
    def __init__(self, *, requested_by: str = "telegram") -> None:
        self.requested_by = requested_by

    def create_job(self, *, phase: str, scope: str, payload: dict[str, object]) -> dict[str, object]:
        args: dict[str, Any] = {"slice_id": scope}
        specs_dir = payload.get("specs_dir")
        if isinstance(specs_dir, str) and specs_dir.strip():
            args["specs_dir"] = specs_dir
        handoff_dir = payload.get("handoff_dir")
        if isinstance(handoff_dir, str) and handoff_dir.strip():
            args["handoff_dir"] = handoff_dir
        force_hold = payload.get("force_hold")
        if isinstance(force_hold, bool):
            args["force_hold"] = force_hold
        req_id = submit_request("dispatch", args, self.requested_by)
        return {"job_id": req_id, "phase": phase, "scope": scope}

    def wait_done(self, req_id: str, timeout: float, poll_interval: float = 0.5) -> dict[str, Any] | None:
        return poll_done(req_id, timeout=timeout, poll_interval=poll_interval)


def submit_request(req_type: str, args: dict[str, Any], requested_by: str) -> str:
    request = contract.build_request(req_type=req_type, args=args, requested_by=requested_by)
    contract.atomic_write_json(constants.requests_dir() / f"{request['req_id']}.json", request)
    return request["req_id"]


def read_status() -> dict[str, Any]:
    payload = contract.read_json(constants.status_path())
    if not isinstance(payload, dict):
        return _degraded_status("missing")
    updated_at = payload.get("updated_at")
    age = _status_age_seconds(updated_at)
    daemon_pid_present = _daemon_pid_present(payload)
    daemon_alive = _daemon_pid_alive(payload) if daemon_pid_present else False
    if daemon_pid_present and not daemon_alive:
        return _degraded_status("dead", payload)
    if daemon_alive:
        # The daemon process is up (possibly busy on a long request). A busy
        # daemon is NOT degraded even if the status is older than the freshness
        # window; only flag it once it has stalled far beyond any real request.
        if age is not None and age <= constants.STATUS_STALLED_AFTER_SECONDS:
            return _ok_status(payload, updated_at)
        return _degraded_status("stalled", payload)
    # No live daemon pid to confirm — fall back to age-based staleness.
    if age is None or age > constants.STATUS_STALE_AFTER_SECONDS:
        return _degraded_status("stale", payload)
    return _ok_status(payload, updated_at)


def _ok_status(payload: dict[str, Any], updated_at: object) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version", constants.SCHEMA_VERSION),
        "updated_at": updated_at,
        "daemon": payload.get("daemon"),
        "ready": list(payload.get("ready", [])),
        "held": list(payload.get("held", [])),
        "slices": list(payload.get("slices", [])),
        "attention": list(payload.get("attention", [])),
        "in_flight": list(payload.get("in_flight", [])),
        "recent_done": list(payload.get("recent_done", [])),
        "degraded": False,
        "degraded_reason": None,
    }


def _daemon_pid_present(payload: dict[str, Any]) -> bool:
    daemon = payload.get("daemon")
    if not isinstance(daemon, dict):
        return False
    pid = daemon.get("pid")
    return isinstance(pid, int) and pid > 0


def _daemon_pid_alive(payload: dict[str, Any]) -> bool:
    if not _daemon_pid_present(payload):
        return False
    pid = payload["daemon"]["pid"]
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def poll_done(req_id: str, timeout: float, poll_interval: float = 0.5) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(timeout, 0.0)
    done_path = constants.done_dir() / f"{req_id}.json"
    while True:
        payload = contract.read_json(done_path)
        if payload is not None:
            return payload
        if time.monotonic() >= deadline:
            return None
        if poll_interval > 0:
            time.sleep(poll_interval)


def _degraded_status(reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "updated_at": None,
        "daemon": None,
        "ready": [],
        "held": list(payload.get("held", [])) if isinstance(payload, dict) else [],
        "slices": list(payload.get("slices", [])) if isinstance(payload, dict) else [],
        "attention": list(payload.get("attention", [])) if isinstance(payload, dict) else [],
        "in_flight": [],
        "recent_done": [],
        "degraded": True,
        "degraded_reason": reason,
    }


def _status_age_seconds(updated_at: object) -> float | None:
    """Age of the status in seconds, or None if the timestamp is unusable."""
    if not isinstance(updated_at, str):
        return None
    try:
        parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    return age.total_seconds()
