from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from paulsha_cortex.control import client as control_client

from . import COMMANDS, PorcelainCommand, register
from .request import DEFAULT_WAIT_TIMEOUT_SECONDS, track_submitted_request

RECOVER_SCHEMA = "cortex-porcelain/recover/v1"
REQUESTED_BY = "coordinator-cli"


def register_commands() -> None:
    if "recover" in COMMANDS:
        return
    register(PorcelainCommand(name="recover", help="執行 slice/work/brokers/service 復原", run=main))


def _add_tracking_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", action="store_true", help="等待 request 進入 terminal 狀態")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help="搭配 --wait 的等待秒數",
    )
    parser.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/recover/v1 JSON")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex recover")
    sub = parser.add_subparsers(dest="command", required=True)

    slice_cmd = sub.add_parser("slice", help="復原 needs_human slice")
    slice_cmd.add_argument("slice_id")
    slice_cmd.add_argument(
        "action",
        choices=("retry-build", "retry-verify", "retry-review", "abandon"),
    )
    slice_cmd.add_argument("--actor", required=True)
    _add_tracking_options(slice_cmd)

    work = sub.add_parser("work", help="復原 work lifecycle")
    work.add_argument("work_id")
    work.add_argument("action", choices=("retry-build", "resume", "abandon"))
    work.add_argument("--repo", required=True)
    work.add_argument("--actor", required=True)
    work.add_argument("--expected-candidate")
    work.add_argument("--expected-run-id")
    work.add_argument("--reason")
    _add_tracking_options(work)

    brokers = sub.add_parser("brokers", help="回收孤兒 Codex broker")
    brokers_sub = brokers.add_subparsers(dest="brokers_command", required=True)
    reap = brokers_sub.add_parser("reap", help="dry-run 或受限回收")
    reap.add_argument("--apply", action="store_true")
    reap.add_argument("--cwd-root")
    reap.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/recover/v1 JSON")

    service = sub.add_parser("service", help="復原 manager service")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    restart = service_sub.add_parser("restart", help="重啟 manager service/timer")
    restart.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
    return parser


def _slice_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "slice_id": args.slice_id,
        "action": args.action,
        "actor": args.actor,
    }


def _work_args(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "work_id": args.work_id,
        "action": args.action,
        "repo": args.repo,
        "actor": args.actor,
    }
    for name in ("expected_candidate", "expected_run_id", "reason"):
        value = getattr(args, name)
        if value is not None:
            payload[name] = value
    return payload


RECOVER_REQUESTS: dict[str, tuple[str, Callable[[argparse.Namespace], dict[str, Any]]]] = {
    "slice": ("slice-action", _slice_args),
    "work": ("work-action", _work_args),
}


def _run_brokers(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from paulsha_cortex.coordinator import broker_reaper

    if args.apply and not args.cwd_root:
        parser.error("--apply requires --cwd-root")
    cwd_root = Path(args.cwd_root).resolve() if args.cwd_root else None
    summary = broker_reaper.reap_orphan_brokers(apply=args.apply, cwd_root=cwd_root)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": RECOVER_SCHEMA,
                    "action": "brokers reap",
                    "status": "ok" if summary.get("ran") and summary.get("returncode", 0) == 0 else "error",
                    "result": summary,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(json.dumps(summary, ensure_ascii=False))
    if not summary.get("ran"):
        return 1
    return 0 if summary.get("returncode", 0) == 0 else 1


def _run_service(args: argparse.Namespace) -> int:
    from . import service

    service_argv = ["restart", "--instance", args.instance]
    return service.main(service_argv)


def main(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    if args.command == "brokers":
        return _run_brokers(args, parser)
    if args.command == "service":
        return _run_service(args)

    request_type, build_args = RECOVER_REQUESTS[args.command]
    action = f"{args.command} {args.action}"
    try:
        request_id = control_client.submit_request(
            request_type,
            build_args(args),
            REQUESTED_BY,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    return track_submitted_request(
        request_id,
        action,
        schema=RECOVER_SCHEMA,
        wait=args.wait,
        timeout=args.timeout,
        json_output=args.json,
    )
