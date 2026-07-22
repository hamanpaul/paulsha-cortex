from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from paulsha_cortex.control import constants, contract
from paulsha_cortex.coordinator.registry import JobRegistry

from . import COMMANDS, PorcelainCommand, register

REQUEST_SCHEMA = "cortex-porcelain/request/v1"
DEFAULT_WAIT_TIMEOUT_SECONDS = 120.0
WAIT_POLL_INTERVAL_SECONDS = 0.5


def register_commands() -> None:
    if "request" in COMMANDS:
        return
    register(PorcelainCommand(name="request", help="追蹤 control request", run=main))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex request")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="列出最近 control requests")
    listing.add_argument("--recent", type=int, default=None, help="只顯示最近 N 筆 request")
    listing.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/request/v1 JSON")

    show = sub.add_parser("show", help="顯示單一 request 狀態")
    show.add_argument("request_id")
    show.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/request/v1 JSON")

    wait = sub.add_parser("wait", help="等待 request 進入 terminal 狀態")
    wait.add_argument("request_id")
    wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_SECONDS, help="等待秒數")
    wait.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/request/v1 JSON")

    logs = sub.add_parser("logs", help="顯示 request 的 done payload 與關聯 job")
    logs.add_argument("request_id")
    logs.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/request/v1 JSON")
    return parser


def _load_request(request_id: str) -> dict[str, Any] | None:
    payload = contract.read_json(constants.requests_dir() / f"{request_id}.json")
    if payload is None:
        return None
    try:
        return contract.validate_request(payload)
    except ValueError as exc:
        raise ValueError(f"request {request_id} payload invalid: {exc}") from exc


def _load_done(request_id: str) -> dict[str, Any] | None:
    payload = contract.read_json(constants.done_dir() / f"{request_id}.json")
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError(f"request {request_id} done payload invalid: expected JSON object")
    bound_req_id = payload.get("req_id")
    if isinstance(bound_req_id, str) and bound_req_id and bound_req_id != request_id:
        raise ValueError(
            f"request {request_id} done payload invalid: req_id mismatch ({bound_req_id})"
        )
    return dict(payload)


def _iter_request_ids(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return [path.stem for path in directory.glob("*.json") if path.is_file()]


def _request_paths(request_id: str) -> list[Path]:
    return [
        constants.requests_dir() / f"{request_id}.json",
        constants.done_dir() / f"{request_id}.json",
    ]


def _request_sort_key(request_id: str) -> float:
    mtimes = [path.stat().st_mtime for path in _request_paths(request_id) if path.exists()]
    return max(mtimes, default=0.0)


def _request_record(request_id: str) -> dict[str, Any] | None:
    request = _load_request(request_id)
    done = _load_done(request_id)
    if request is None and done is None:
        return None
    record: dict[str, Any] = {
        "request_id": request_id,
        "state": "done" if done is not None else "pending",
    }
    if request is not None:
        record.update(
            {
                "type": request.get("type"),
                "args": request.get("args"),
                "requested_by": request.get("requested_by"),
                "created_at": request.get("created_at"),
            }
        )
    if done is not None:
        record.update(
            {
                "status": done.get("status"),
                "result": done.get("result"),
                "error": done.get("error"),
                "started_at": done.get("started_at"),
                "finished_at": done.get("finished_at"),
            }
        )
    return record


def _require_request_record(request_id: str) -> dict[str, Any]:
    record = _request_record(request_id)
    if record is None:
        raise ValueError(f"request not found: {request_id}")
    return record


def _json_dump(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _print_request_summary(record: dict[str, Any]) -> None:
    sys.stdout.write(f"request_id: {record['request_id']}\n")
    sys.stdout.write(f"state: {record['state']}\n")
    if isinstance(record.get("type"), str):
        sys.stdout.write(f"type: {record['type']}\n")
    if isinstance(record.get("created_at"), str):
        sys.stdout.write(f"created_at: {record['created_at']}\n")
    if isinstance(record.get("finished_at"), str):
        sys.stdout.write(f"finished_at: {record['finished_at']}\n")
    if record.get("status") is not None:
        sys.stdout.write(f"status: {record['status']}\n")
    if record.get("error") is not None:
        sys.stdout.write(f"error: {record['error']}\n")
    if record.get("args") is not None:
        sys.stdout.write(
            "args: " + json.dumps(record["args"], ensure_ascii=False, sort_keys=True) + "\n"
        )
    if record.get("result") is not None:
        sys.stdout.write(
            "result: " + json.dumps(record["result"], ensure_ascii=False, sort_keys=True) + "\n"
        )


def _print_timeout_hint(request_id: str) -> None:
    sys.stderr.write(f"request_id: {request_id}\n")
    sys.stderr.write(f"hint: cortex request show {request_id}\n")


def _related_jobs(done: dict[str, Any] | None) -> list[dict[str, Any]]:
    if done is None:
        return []
    result = done.get("result")
    if not isinstance(result, dict):
        return []
    job_id = result.get("job_id")
    run_id = result.get("run_id")
    if not isinstance(job_id, str) and not isinstance(run_id, str):
        return []
    jobs = JobRegistry().list_jobs()
    related: list[dict[str, Any]] = []
    for job in jobs:
        matches_job = isinstance(job_id, str) and job.get("job_id") == job_id
        matches_run = isinstance(run_id, str) and job.get("workflow_run_id") == run_id
        if not (matches_job or matches_run):
            continue
        related.append(
            {
                "job_id": job.get("job_id"),
                "task": job.get("task"),
                "persona": job.get("persona"),
                "kind": job.get("kind"),
                "status": job.get("status"),
                "exit_code": job.get("exit_code"),
                "branch": job.get("branch"),
                "log_path": job.get("log_path"),
                "workflow_run_id": job.get("workflow_run_id"),
                "workflow_card": job.get("workflow_card"),
                "workflow_phase": job.get("workflow_phase"),
            }
        )
    return related


def _run_list(*, recent: int | None, json_output: bool) -> int:
    request_ids = {
        * _iter_request_ids(constants.requests_dir()),
        * _iter_request_ids(constants.done_dir()),
    }
    ordered = sorted(request_ids, key=_request_sort_key, reverse=True)
    if recent is not None:
        ordered = ordered[: max(recent, 0)]
    rows = [record for request_id in ordered if (record := _request_record(request_id)) is not None]
    if json_output:
        _json_dump({"schema": REQUEST_SCHEMA, "requests": rows})
        return 0
    for row in rows:
        fields = [
            row["request_id"],
            row["state"],
            str(row.get("type") or "-"),
            str(row.get("created_at") or row.get("finished_at") or "-"),
        ]
        sys.stdout.write("\t".join(fields) + "\n")
    return 0


def _run_show(request_id: str, *, json_output: bool) -> int:
    record = _require_request_record(request_id)
    if json_output:
        _json_dump({"schema": REQUEST_SCHEMA, "request": record})
        return 0
    _print_request_summary(record)
    return 0


def _run_wait(request_id: str, *, timeout: float, json_output: bool) -> int:
    _require_request_record(request_id)
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        record = _require_request_record(request_id)
        if record["state"] == "done":
            if json_output:
                _json_dump({"schema": REQUEST_SCHEMA, "request": record})
            else:
                _print_request_summary(record)
            if record.get("error") is not None or record.get("status") != "ok":
                return 1
            return 0
        if time.monotonic() >= deadline:
            hint = f"cortex request show {request_id}"
            if json_output:
                _json_dump(
                    {
                        "schema": REQUEST_SCHEMA,
                        "request": record,
                        "timeout_seconds": max(timeout, 0.0),
                        "hint": hint,
                    }
                )
            else:
                _print_timeout_hint(request_id)
            return 3
        time.sleep(min(WAIT_POLL_INTERVAL_SECONDS, max(deadline - time.monotonic(), 0.0)))


def _run_logs(request_id: str, *, json_output: bool) -> int:
    request = _load_request(request_id)
    done = _load_done(request_id)
    if request is None and done is None:
        raise ValueError(f"request not found: {request_id}")
    payload = {
        "schema": REQUEST_SCHEMA,
        "request_id": request_id,
        "request": _request_record(request_id),
        "done": done,
        "related_jobs": _related_jobs(done),
    }
    if json_output:
        _json_dump(payload)
        return 0
    if payload["request"] is not None:
        _print_request_summary(payload["request"])
    if done is not None:
        sys.stdout.write("done: " + json.dumps(done, ensure_ascii=False, sort_keys=True) + "\n")
    if payload["related_jobs"]:
        for job in payload["related_jobs"]:
            sys.stdout.write(
                "job: " + json.dumps(job, ensure_ascii=False, sort_keys=True) + "\n"
            )
    else:
        sys.stdout.write("related_jobs: []\n")
    return 0


def main(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    try:
        if args.command == "list":
            return _run_list(recent=args.recent, json_output=args.json)
        if args.command == "show":
            return _run_show(args.request_id, json_output=args.json)
        if args.command == "wait":
            return _run_wait(args.request_id, timeout=args.timeout, json_output=args.json)
        if args.command == "logs":
            return _run_logs(args.request_id, json_output=args.json)
    except ValueError as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unsupported request command: {args.command}")
    return 2
