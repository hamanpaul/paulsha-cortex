from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from paulsha_cortex.config import paths
from paulsha_cortex.control import constants, contract
from paulsha_cortex.control.client import read_status
from paulsha_cortex.coordinator import autonomy
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.doctor import run_doctor
from paulsha_cortex.monitor.config import default_socket_path, load_config
from paulsha_cortex.monitor.work_api import MonitorSocketClient

from . import COMMANDS, PorcelainCommand, register
from ._runtime_probe import probe_service_runtime

INSPECT_SCHEMA = "cortex-porcelain/inspect/v1"


def register_commands() -> None:
    if "inspect" in COMMANDS:
        return
    register(PorcelainCommand(name="inspect", help="唯讀檢查 status/job/ready/work/doctor/service", run=main))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex inspect")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="讀取 manager status 快照")
    status.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")

    job = sub.add_parser("job", help="顯示單一 Job snapshot")
    job.add_argument("job_id")
    job.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")

    ready = sub.add_parser("ready", help="列出目前 ready slice")
    ready.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")

    work = sub.add_parser("work", help="顯示單一 Work Item")
    work.add_argument("work_id")
    work.add_argument("--repo", default=None, help="指定 owner/repo")
    work.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")

    doctor = sub.add_parser("doctor", help="讀取 doctor probe 摘要")
    doctor.add_argument("--probe-live", action="store_true", help="執行 live probe")
    doctor.add_argument("--repo", default=None, help="live probe 使用的 owner/repo")
    doctor.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
    doctor.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")

    service = sub.add_parser("service", help="檢查 service runtime 與 unit exec path")
    service.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
    service.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")
    return parser


def _json_dump(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _inspect_envelope(command: str, **payload: Any) -> dict[str, Any]:
    return {"schema": INSPECT_SCHEMA, "command": command, **payload}


def _print_status(status: dict[str, Any]) -> None:
    sys.stdout.write(f"updated_at: {status.get('updated_at')}\n")
    sys.stdout.write(f"degraded: {status.get('degraded')}\n")
    sys.stdout.write("ready: " + json.dumps(status.get("ready", []), ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.write("held: " + json.dumps(status.get("held", []), ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.write(
        "in_flight: " + json.dumps(status.get("in_flight", []), ensure_ascii=False, sort_keys=True) + "\n"
    )
    sys.stdout.write(
        "recent_done: " + json.dumps(status.get("recent_done", []), ensure_ascii=False, sort_keys=True) + "\n"
    )


def _print_job(job: dict[str, Any]) -> None:
    for key in (
        "job_id",
        "task",
        "persona",
        "kind",
        "status",
        "exit_code",
        "branch",
        "workflow_run_id",
        "workflow_card",
        "workflow_phase",
    ):
        if key in job:
            sys.stdout.write(f"{key}: {job.get(key)}\n")


def _print_ready(ready_rows: list[dict[str, Any]]) -> None:
    for row in ready_rows:
        sys.stdout.write(
            f"{row.get('slice_id', '-')}\t{row.get('path', '-')}\t{row.get('plan', '-')}\n"
        )


def _print_work(item: dict[str, Any]) -> None:
    sys.stdout.write(f"repo: {item.get('repo')}\n")
    sys.stdout.write(f"work_id: {item.get('work_id')}\n")
    sys.stdout.write(f"title: {item.get('title')}\n")
    sys.stdout.write(f"state: {item.get('state')}\n")
    sys.stdout.write(f"phase: {item.get('phase')}\n")
    sys.stdout.write(
        "facets: " + json.dumps(item.get("facets", []), ensure_ascii=False, sort_keys=True) + "\n"
    )


def _print_doctor(report: dict[str, Any]) -> None:
    for probe in report.get("probes", []):
        sys.stdout.write(f"{str(probe.get('status', '')).upper():4} {probe.get('name')}: {probe.get('detail')}\n")


def _print_service(service: dict[str, Any]) -> None:
    sys.stdout.write(f"instance: {service.get('instance')}\n")
    sys.stdout.write(f"mode: {service.get('mode')}\n")
    sys.stdout.write(f"version: {service.get('version')}\n")
    for unit_name, row in sorted(service.get("units", {}).items()):
        sys.stdout.write(
            f"{unit_name}\tstatus={row.get('status')}\tpid={row.get('pid') or '-'}"
            f"\texec_path={row.get('exec_path') or '-'}\tstale={row.get('stale')}\n"
        )


def _run_status(*, json_output: bool) -> int:
    raw_status = contract.read_json(constants.status_path())
    if isinstance(raw_status, dict):
        status = {
            **raw_status,
            "held": list(raw_status.get("held", [])),
            "slices": list(raw_status.get("slices", [])),
            "attention": list(raw_status.get("attention", [])),
            "degraded": bool(raw_status.get("degraded", False)),
            "degraded_reason": raw_status.get("degraded_reason"),
        }
    else:
        status = read_status()
    if json_output:
        _json_dump(_inspect_envelope("status", status=status))
        return 0
    _print_status(status)
    return 0


def _run_job(job_id: str, *, json_output: bool) -> int:
    try:
        job = JobRegistry().get_job(job_id)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc
    if json_output:
        _json_dump(_inspect_envelope("job", job=job))
        return 0
    _print_job(job)
    return 0


def _run_ready(*, json_output: bool) -> int:
    ready_rows = autonomy.ready_units(
        autonomy.scan_specs(str(paths.specs_root())),
        autonomy.default_is_satisfied,
    )
    if json_output:
        _json_dump(_inspect_envelope("ready", ready=ready_rows))
        return 0
    _print_ready(list(ready_rows))
    return 0


def _monitor_socket_path() -> str:
    try:
        return str(load_config().socket_path)
    except FileNotFoundError:
        return str(default_socket_path())


def _load_work_item(work_id: str, *, repo: str | None) -> dict[str, Any]:
    request = {"kind": "get_work_item", "work_id": work_id}
    if repo is not None:
        request["repo"] = repo
    response = MonitorSocketClient(socket_path=_monitor_socket_path()).request(request)
    if not response.get("ok"):
        raise ValueError(str(response.get("error", f"work item not found: {work_id}")))
    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError("Monitor response data 不是 JSON object")
    item = data.get("item")
    if not isinstance(item, dict):
        raise ValueError(f"work item not found: {work_id}")
    return item


def _run_work(work_id: str, *, repo: str | None, json_output: bool) -> int:
    item = _load_work_item(work_id, repo=repo)
    if json_output:
        _json_dump(_inspect_envelope("work", item=item))
        return 0
    _print_work(item)
    return 0


def _run_doctor(
    *,
    probe_live: bool,
    repo: str | None,
    instance: str,
    json_output: bool,
) -> int:
    doctor = run_doctor(probe_live=probe_live, repo=repo, instance=instance).to_dict()
    if json_output:
        _json_dump(_inspect_envelope("doctor", doctor=doctor))
        return 0
    _print_doctor(doctor)
    return 0 if bool(doctor.get("ok")) else 1


def _run_service(*, instance: str, json_output: bool) -> int:
    service = probe_service_runtime(instance)
    if json_output:
        _json_dump(_inspect_envelope("service", service=service))
        return 0
    _print_service(service)
    return 0


def main(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    try:
        if args.command == "status":
            return _run_status(json_output=args.json)
        if args.command == "job":
            return _run_job(args.job_id, json_output=args.json)
        if args.command == "ready":
            return _run_ready(json_output=args.json)
        if args.command == "work":
            return _run_work(args.work_id, repo=args.repo, json_output=args.json)
        if args.command == "doctor":
            return _run_doctor(
                probe_live=args.probe_live,
                repo=args.repo,
                instance=args.instance,
                json_output=args.json,
            )
        if args.command == "service":
            return _run_service(instance=args.instance, json_output=args.json)
    except ValueError as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unsupported inspect command: {args.command}")
    return 2
