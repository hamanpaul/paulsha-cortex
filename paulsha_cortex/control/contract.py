from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import constants

REQUEST_TYPES = frozenset({"tick", "fanout", "dispatch"})


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_req_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex}"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temp_path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.rename(temp_path, target)
    except BaseException:
        if temp_path.exists():
            temp_path.unlink()
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_request(*, req_type: str, args: dict[str, Any], requested_by: str) -> dict[str, Any]:
    if req_type not in REQUEST_TYPES:
        raise ValueError(f"unsupported request type: {req_type}")
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "req_id": generate_req_id(),
        "type": req_type,
        "args": dict(args),
        "requested_by": requested_by,
        "created_at": utcnow(),
    }


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != constants.SCHEMA_VERSION:
        raise ValueError(
            f"invalid schema_version: expected {constants.SCHEMA_VERSION}, got {payload.get('schema_version')!r}"
        )
    req_id = payload.get("req_id")
    if not isinstance(req_id, str) or not req_id:
        raise ValueError("request req_id must be a non-empty string")
    req_type = payload.get("type")
    if req_type not in REQUEST_TYPES:
        raise ValueError(f"unsupported request type: {req_type!r}")
    args = payload.get("args")
    if not isinstance(args, dict):
        raise ValueError("request args must be an object")
    requested_by = payload.get("requested_by")
    if not isinstance(requested_by, str) or not requested_by:
        raise ValueError("request requested_by must be a non-empty string")
    created_at = payload.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise ValueError("request created_at must be a non-empty string")
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "req_id": req_id,
        "type": req_type,
        "args": dict(args),
        "requested_by": requested_by,
        "created_at": created_at,
    }


def build_done(
    *,
    req_id: str,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "req_id": req_id,
        "status": status,
        "result": result,
        "error": error,
        "started_at": started_at,
        "finished_at": utcnow(),
    }


def build_status(
    *,
    ready: list[Any],
    in_flight: list[dict[str, Any]],
    recent_done: list[dict[str, Any]],
    daemon: dict[str, Any],
    updated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "updated_at": updated_at,
        "daemon": daemon,
        "ready": list(ready),
        "in_flight": list(in_flight),
        "recent_done": list(recent_done),
    }
