from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Sequence

from . import autonomy, broker_reaper
from .launcher import _ARGV_BUILDERS, AgentLauncher, SubprocessLauncher
from .registry import JobRegistry
from .seams import PaneSender, ScriptWorktreeCreator, TmuxPaneSender, WorktreeCreator

DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_REQUEST_POLL_INTERVAL_SECONDS = 0.1
DEFAULT_REQUESTED_BY = "coordinator-cli"


def _resolve_launcher(executor, injected, *, allow_unsafe, model):
    """注入優先；否則僅在 executor 指定時建 SubprocessLauncher（帶 allow_unsafe/model）。"""
    if injected is not None:
        return injected
    if executor is None:
        return None
    return SubprocessLauncher(executor=executor, allow_unsafe=allow_unsafe, model=model)


def _refuse_unsafe_fanout(metas, predicate, *, allow_unsafe, max_ready=1):
    """fail-closed：--allow-unsafe 旁路各 executor 的沙箱/核可，故僅允許 ≤max_ready 個就緒
    slice（canary 一次一個）。就緒集過大（如誤指 specs-dir、或真實 specs 多個 dispatch:auto）
    時拒絕，避免一次對多個 slice 大量自主越權派工。"""
    if not allow_unsafe:
        return
    ready = autonomy.ready_units(metas, predicate)
    if len(ready) > max_ready:
        raise ValueError(
            f"--allow-unsafe 僅允許 ≤{max_ready} 個就緒 slice（canary 一次一個），實得 {len(ready)}"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex",
        description="讀取 manager 狀態，或透過 control queue 執行派工與交付 gate。",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dispatch = sub.add_parser(
        "dispatch",
        help="已停用的舊低階入口（請改用 fanout/tick）",
        description="此入口缺少 spec/verification metadata，固定拒絕執行；請改用 fanout 或 tick。",
    )
    p_dispatch.add_argument("--task", required=True)
    p_dispatch.add_argument("--persona", required=True)
    p_dispatch.add_argument("--pane", required=True)
    p_dispatch.add_argument("--command", required=True)

    sub.add_parser("jobs", help="列出所有 Job 執行紀錄")

    p_stat = sub.add_parser("stat", help="查單一 Job 執行紀錄")
    p_stat.add_argument("job_id")

    p_ready = sub.add_parser("ready", help="列出 dispatch:auto、plan 與 dependency 均就緒的 specs")
    p_ready.add_argument("--specs-dir", required=True, help="要掃描的 spec 目錄")

    p_fanout = sub.add_parser("fanout", help="透過 manager daemon 派送目前 ready 的 slices")
    p_fanout.add_argument("--specs-dir", required=True, help="要掃描的 spec 目錄")
    p_fanout.add_argument("--persona", default="builder", help="builder persona role（預設：builder）")
    p_fanout.add_argument(
        "--executor",
        choices=sorted(_ARGV_BUILDERS),
        default=None,
        help="headless executor；未指定時使用 daemon 預設值",
    )
    p_fanout.add_argument(
        "--allow-unsafe",
        action="store_true",
        help="高風險：旁路 executor approval/sandbox；只允許單一 ready slice canary",
    )
    p_fanout.add_argument("--model", default=None, help="原樣傳給 builder executor 的 model ID")

    p_complete = sub.add_parser(
        "complete",
        help="輪詢既有 jobs，執行 verification/review/completion gate",
    )
    p_complete.add_argument(
        "--handoff-dir",
        default=autonomy.DEFAULT_HANDOFF_DIR,
        help=f"handoff 目錄（預設為相對路徑：{autonomy.DEFAULT_HANDOFF_DIR}）",
    )
    p_complete.add_argument(
        "--specs-dir", default=None,
        help="設定後據 dependency graph 觀測算出本趟釋放的下游（released）",
    )
    p_complete.add_argument(
        "--review-executor", choices=sorted(_ARGV_BUILDERS), default=None,
        help="foreign reviewer executor",
    )
    p_complete.add_argument("--review-model", default=None, help="foreign reviewer model ID")

    p_slice_action = sub.add_parser("slice-action", help="對 needs_human slice 送出本機 recovery action")
    p_slice_action.add_argument("slice_id")
    p_slice_action.add_argument("action", choices=["retry-build", "retry-verify", "retry-review", "abandon"])
    p_slice_action.add_argument("--actor", required=True)

    p_work = sub.add_parser("work", help="透過 manager daemon 執行 work lifecycle mutation")
    p_work.add_argument("action", choices=["link", "unlink", "start", "resume", "auto", "ship"])
    p_work.add_argument("work_id")
    p_work.add_argument("--repo", required=True)
    p_work.add_argument("--issue", type=int)
    p_work.add_argument("--kind", choices=["github_issue", "github_pr", "openspec", "path"])
    p_work.add_argument("--ref")
    toggle = p_work.add_mutually_exclusive_group()
    toggle.add_argument("--enable", action="store_true")
    toggle.add_argument("--disable", action="store_true")
    p_work.add_argument("--payload", help="額外 manager-side evidence refs JSON object")

    sub.add_parser("status", help="讀取 manager daemon 的 ready/held/slices/attention 快照")

    p_reap = sub.add_parser("reap-brokers", help="操作員 dry-run/apply 孤兒 codex broker 回收")
    p_reap.add_argument("--apply", action="store_true")
    p_reap.add_argument("--cwd-root")

    p_tick = sub.add_parser(
        "tick",
        help="執行完整 manager tick：fanout → verification/review/completion",
    )
    p_tick.add_argument("--specs-dir", required=True, help="要掃描的 spec 目錄")
    p_tick.add_argument("--persona", default="builder", help="builder persona role（預設：builder）")
    p_tick.add_argument(
        "--executor", choices=sorted(_ARGV_BUILDERS), default=None,
        help="headless builder executor；未指定時使用 daemon 預設值",
    )
    p_tick.add_argument(
        "--handoff-dir",
        default=autonomy.DEFAULT_HANDOFF_DIR,
        help=f"handoff 目錄（預設為相對路徑：{autonomy.DEFAULT_HANDOFF_DIR}）",
    )
    p_tick.add_argument("--require-idle", action="store_true", help="系統負載高於 --max-load 時不派新工作")
    p_tick.add_argument("--max-load", type=float, default=1.0, help="--require-idle 的 1-minute load threshold")
    p_tick.add_argument(
        "--allow-unsafe",
        action="store_true",
        help="高風險：旁路 executor approval/sandbox；只允許單一 ready slice canary",
    )
    p_tick.add_argument("--model", default=None, help="原樣傳給 builder executor 的 model ID")
    p_tick.add_argument(
        "--review-executor", choices=sorted(_ARGV_BUILDERS), default=None,
        help="foreign reviewer executor",
    )
    p_tick.add_argument("--review-model", default=None, help="foreign reviewer model ID")

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    registry: JobRegistry | None = None,
    pane_sender: PaneSender | None = None,
    worktree_creator: WorktreeCreator | None = None,
    is_satisfied=None,
    git_runner=None,
    launcher: AgentLauncher | None = None,
    reaper=None,
    control_read_status: Callable[[], dict] | None = None,
    control_submit_request: Callable[[str, dict, str], str] | None = None,
    control_poll_done: Callable[[str, float, float], dict | None] | None = None,
) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "dispatch":
        print(
            "錯誤: 低階 dispatch 已停用；缺少 spec / verification metadata。"
            "請改用 fanout/tick 或 control plane dispatch request。",
            file=sys.stderr,
        )
        return 1

    if args.cmd == "reap-brokers":
        if args.apply and not args.cwd_root:
            print("錯誤: --apply 需要搭配 --cwd-root", file=sys.stderr)
            return 2
        cwd_root = Path(args.cwd_root).resolve() if args.cwd_root else None
        summary = broker_reaper.reap_orphan_brokers(apply=args.apply, cwd_root=cwd_root)
        print(json.dumps(summary, ensure_ascii=False))
        if not summary.get("ran"):
            return 1
        return 0 if summary.get("returncode", 0) == 0 else 1

    read_status_fn, submit_request_fn, poll_done_fn = _resolve_control_hooks(
        control_read_status=control_read_status,
        control_submit_request=control_submit_request,
        control_poll_done=control_poll_done,
    )

    if args.cmd == "status":
        print(json.dumps(read_status_fn(), ensure_ascii=False))
        return 0

    if args.cmd == "ready":
        predicate = is_satisfied if is_satisfied is not None else autonomy.default_is_satisfied
        metas = autonomy.scan_specs(args.specs_dir)
        try:
            ready = autonomy.ready_units(metas, predicate)
            print(json.dumps(ready, ensure_ascii=False))
            return 0
        except (ValueError, autonomy.DispatchReadyRequiresLauncherError) as exc:
            print(f"錯誤: {exc}", file=sys.stderr)
            return 1

    if args.cmd == "complete":
        request_args = {"handoff_dir": args.handoff_dir}
        if args.specs_dir:
            request_args["specs_dir"] = args.specs_dir
        if args.review_executor is not None:
            request_args["review_executor"] = args.review_executor
        if args.review_model is not None:
            request_args["review_model"] = args.review_model
        return _submit_mutation_request(
            "complete",
            request_args,
            read_status_fn=read_status_fn,
            submit_request_fn=submit_request_fn,
            poll_done_fn=poll_done_fn,
        )

    if args.cmd == "slice-action":
        return _submit_mutation_request(
            "slice-action",
            {"slice_id": args.slice_id, "action": args.action, "actor": args.actor},
            read_status_fn=read_status_fn,
            submit_request_fn=submit_request_fn,
            poll_done_fn=poll_done_fn,
        )

    if args.cmd == "work":
        request_args = {
            "action": args.action,
            "repo": args.repo,
            "work_id": args.work_id,
        }
        if args.issue is not None:
            request_args["issue"] = args.issue
        if args.kind is not None:
            request_args["kind"] = args.kind
        if args.ref is not None:
            request_args["ref"] = args.ref
        if args.enable or args.disable:
            request_args["enabled"] = bool(args.enable)
        if args.payload:
            try:
                extra = json.loads(Path(args.payload).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                print(f"錯誤: work payload unreadable: {exc}", file=sys.stderr)
                return 2
            if not isinstance(extra, dict):
                print("錯誤: work payload must be a JSON object", file=sys.stderr)
                return 2
            protected = {"action", "repo", "work_id"}
            if protected & set(extra):
                print("錯誤: work payload cannot override action/repo/work_id", file=sys.stderr)
                return 2
            request_args.update(extra)
        return _submit_mutation_request(
            "work-action",
            request_args,
            read_status_fn=read_status_fn,
            submit_request_fn=submit_request_fn,
            poll_done_fn=poll_done_fn,
        )

    if args.cmd == "tick":
        request_args = {
            "specs_dir": args.specs_dir,
            "persona": args.persona,
            "handoff_dir": args.handoff_dir,
            "require_idle": args.require_idle,
            "max_load": args.max_load,
            "allow_unsafe": args.allow_unsafe,
            "model": args.model,
        }
        if args.executor is not None:
            request_args["executor"] = args.executor
        if args.review_executor is not None:
            request_args["review_executor"] = args.review_executor
        if args.review_model is not None:
            request_args["review_model"] = args.review_model
        return _submit_mutation_request(
            "tick",
            request_args,
            read_status_fn=read_status_fn,
            submit_request_fn=submit_request_fn,
            poll_done_fn=poll_done_fn,
        )

    if args.cmd == "fanout":
        request_args = {
            "specs_dir": args.specs_dir,
            "persona": args.persona,
            "allow_unsafe": args.allow_unsafe,
            "model": args.model,
        }
        if args.executor is not None:
            request_args["executor"] = args.executor
        return _submit_mutation_request(
            "fanout",
            request_args,
            read_status_fn=read_status_fn,
            submit_request_fn=submit_request_fn,
            poll_done_fn=poll_done_fn,
        )

    # 讀取型命令以下才需要本地 snapshot 物件。
    reg = registry if registry is not None else JobRegistry()

    if args.cmd == "jobs":
        print(json.dumps(reg.list_jobs(), ensure_ascii=False))
        return 0

    if args.cmd == "stat":
        try:
            job = reg.get_job(args.job_id)
        except KeyError as exc:
            print(f"錯誤: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(job, ensure_ascii=False))
        return 0

    return 2  # pragma: no cover（argparse required=True 已擋）


def _resolve_control_hooks(
    *,
    control_read_status: Callable[[], dict] | None,
    control_submit_request: Callable[[str, dict, str], str] | None,
    control_poll_done: Callable[[str, float, float], dict | None] | None,
) -> tuple[
    Callable[[], dict],
    Callable[[str, dict, str], str],
    Callable[[str, float, float], dict | None],
]:
    if control_read_status and control_submit_request and control_poll_done:
        return control_read_status, control_submit_request, control_poll_done
    from paulsha_cortex.control import client as control_client

    return (
        control_read_status or control_client.read_status,
        control_submit_request or control_client.submit_request,
        control_poll_done or control_client.poll_done,
    )


def _submit_mutation_request(
    req_type: str,
    args: dict,
    *,
    read_status_fn: Callable[[], dict],
    submit_request_fn: Callable[[str, dict, str], str],
    poll_done_fn: Callable[[str, float, float], dict | None],
) -> int:
    status = read_status_fn()
    if isinstance(status, dict) and status.get("degraded"):
        reason = status.get("degraded_reason") or "unknown"
        print(
            f"錯誤: manager daemon 未就緒（{reason}）；無法處理 {req_type}，請先啟動 daemon。",
            file=sys.stderr,
        )
        return 1
    req_id = submit_request_fn(req_type, dict(args), DEFAULT_REQUESTED_BY)
    done = poll_done_fn(req_id, DEFAULT_REQUEST_TIMEOUT_SECONDS, DEFAULT_REQUEST_POLL_INTERVAL_SECONDS)
    if not isinstance(done, dict):
        print(
            f"錯誤: manager daemon 未在 {DEFAULT_REQUEST_TIMEOUT_SECONDS:.1f}s 內完成 {req_type} request: {req_id}",
            file=sys.stderr,
        )
        return 1
    if done.get("status") != "ok":
        print(f"錯誤: {done.get('error') or 'unknown request error'}", file=sys.stderr)
        return 1
    print(json.dumps(done.get("result"), ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
