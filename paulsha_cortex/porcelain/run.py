from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from paulsha_cortex.control import client as control_client
from paulsha_cortex.control.contract import WORK_ACTIONS, WORK_SOURCE_KINDS

from . import COMMANDS, PorcelainCommand, register
from .request import DEFAULT_WAIT_TIMEOUT_SECONDS, track_submitted_request

RUN_SCHEMA = "cortex-porcelain/run/v1"
REQUESTED_BY = "coordinator-cli"


def register_commands() -> None:
    if "run" in COMMANDS:
        return
    register(PorcelainCommand(name="run", help="執行 tick/fanout/complete/work mutation", run=main))


def _add_tracking_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", action="store_true", help="等待 request 進入 terminal 狀態")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help="搭配 --wait 的等待秒數",
    )
    parser.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/run/v1 JSON")


def _add_executor_options(parser: argparse.ArgumentParser, *, review: bool) -> None:
    parser.add_argument("--specs-dir", default=None, help="覆寫 daemon specs 目錄")
    parser.add_argument("--executor", default=None, help="覆寫 builder executor")
    parser.add_argument("--model", default=None, help="覆寫 builder model ID")
    if review:
        parser.add_argument("--review-executor", default=None, help="覆寫 reviewer executor")
        parser.add_argument("--review-model", default=None, help="覆寫 reviewer model ID")


def _add_work_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", type=int)
    parser.add_argument("--kind", choices=sorted(WORK_SOURCE_KINDS))
    parser.add_argument("--ref")
    parser.add_argument("--actor")
    parser.add_argument("--expected-candidate")
    parser.add_argument("--expected-run-id")
    parser.add_argument("--reason")
    toggle = parser.add_mutually_exclusive_group()
    toggle.add_argument("--enable", action="store_const", dest="enabled", const=True)
    toggle.add_argument("--disable", action="store_const", dest="enabled", const=False)
    parser.set_defaults(enabled=None)
    parser.add_argument("--payload", help="額外 manager-side evidence refs JSON 檔案路徑")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex run")
    sub = parser.add_subparsers(dest="command", required=True)

    tick = sub.add_parser("tick", help="執行完整 manager tick")
    _add_executor_options(tick, review=True)
    _add_tracking_options(tick)

    fanout = sub.add_parser("fanout", help="派送目前 ready slices")
    _add_executor_options(fanout, review=False)
    _add_tracking_options(fanout)

    complete = sub.add_parser("complete", help="執行 verification/review/completion")
    complete.add_argument("--review-executor", default=None, help="覆寫 reviewer executor")
    complete.add_argument("--review-model", default=None, help="覆寫 reviewer model ID")
    _add_tracking_options(complete)

    work = sub.add_parser("work", help="執行 work lifecycle mutation")
    work.add_argument("action", choices=sorted(WORK_ACTIONS))
    work.add_argument("work_id")
    _add_work_options(work)
    _add_tracking_options(work)
    return parser


def _provided(args: argparse.Namespace, names: Sequence[str]) -> dict[str, Any]:
    return {
        name: value
        for name in names
        if (value := getattr(args, name, None)) is not None
    }


def _tick_args(args: argparse.Namespace) -> dict[str, Any]:
    return _provided(
        args,
        ("specs_dir", "executor", "model", "review_executor", "review_model"),
    )


def _fanout_args(args: argparse.Namespace) -> dict[str, Any]:
    return _provided(args, ("specs_dir", "executor", "model"))


def _complete_args(args: argparse.Namespace) -> dict[str, Any]:
    return _provided(args, ("review_executor", "review_model"))


def _work_args(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "action": args.action,
        "work_id": args.work_id,
        "repo": args.repo,
        **_provided(
            args,
            (
                "issue",
                "kind",
                "ref",
                "actor",
                "expected_candidate",
                "expected_run_id",
                "reason",
                "enabled",
            ),
        ),
    }
    if args.payload:
        extra = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        if not isinstance(extra, dict):
            raise ValueError("work payload must be a JSON object")
        protected = {"action", "repo", "work_id"}
        if protected & set(extra):
            raise ValueError("work payload cannot override action/repo/work_id")
        payload.update(extra)
    return payload


RUN_REQUESTS: dict[str, tuple[str, Callable[[argparse.Namespace], dict[str, Any]]]] = {
    "tick": ("tick", _tick_args),
    "fanout": ("fanout", _fanout_args),
    "complete": ("complete", _complete_args),
    "work": ("work-action", _work_args),
}


def main(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    request_type, build_args = RUN_REQUESTS[args.command]
    action = args.command if args.command != "work" else f"work {args.action}"
    try:
        request_id = control_client.submit_request(
            request_type,
            build_args(args),
            REQUESTED_BY,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        return 2
    return track_submitted_request(
        request_id,
        action,
        schema=RUN_SCHEMA,
        wait=args.wait,
        timeout=args.timeout,
        json_output=args.json,
    )
