from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import autonomy, broker_reaper, manager
from .dispatcher import Dispatcher
from .launcher import _ARGV_BUILDERS, AgentLauncher, SubprocessLauncher
from .registry import JobRegistry
from .seams import PaneSender, ScriptWorktreeCreator, TmuxPaneSender, WorktreeCreator


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
    parser = argparse.ArgumentParser(prog="python -m paulsha_cortex.coordinator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dispatch = sub.add_parser("dispatch", help="派一個 job 進 pane+worktree")
    p_dispatch.add_argument("--task", required=True)
    p_dispatch.add_argument("--persona", required=True)
    p_dispatch.add_argument("--pane", required=True)
    p_dispatch.add_argument("--command", required=True)

    sub.add_parser("jobs", help="列出所有 job")

    p_stat = sub.add_parser("stat", help="查單一 job")
    p_stat.add_argument("job_id")

    p_ready = sub.add_parser("ready", help="列出就緒（dispatch:auto∧有plan∧depends_on全滿足）的單位")
    p_ready.add_argument("--specs-dir", required=True)

    p_fanout = sub.add_parser("fanout", help="對就緒集經 Dispatcher 並行派工")
    p_fanout.add_argument("--specs-dir", required=True)
    p_fanout.add_argument("--persona", default="builder")
    p_fanout.add_argument(
        "--executor",
        choices=sorted(_ARGV_BUILDERS),
        default=None,
        help="設定後走 headless launcher 路徑（copilot/claude/codex）；未設則沿用舊 tmux pane 路徑",
    )
    p_fanout.add_argument("--allow-unsafe", action="store_true")
    p_fanout.add_argument("--model", default=None)

    p_complete = sub.add_parser(
        "complete",
        help="完成側 tick：輪詢 in-flight job → 寫 handoff manifest → 釋放下游",
    )
    p_complete.add_argument("--handoff-dir", default=autonomy.DEFAULT_HANDOFF_DIR)
    p_complete.add_argument(
        "--specs-dir", default=None,
        help="設定後據 dependency graph 觀測算出本趟釋放的下游（released）",
    )

    p_reap = sub.add_parser("reap-brokers", help="操作員 dry-run/apply 孤兒 codex broker 回收")
    p_reap.add_argument("--apply", action="store_true")
    p_reap.add_argument("--cwd-root")

    p_tick = sub.add_parser(
        "tick",
        help="完整 manager tick：fanout→complete（idle-gated）",
    )
    p_tick.add_argument("--specs-dir", required=True)
    p_tick.add_argument("--persona", default="builder")
    p_tick.add_argument("--executor", choices=sorted(_ARGV_BUILDERS), default=None)
    p_tick.add_argument("--handoff-dir", default=autonomy.DEFAULT_HANDOFF_DIR)
    p_tick.add_argument("--require-idle", action="store_true")
    p_tick.add_argument("--max-load", type=float, default=1.0)
    p_tick.add_argument("--allow-unsafe", action="store_true")
    p_tick.add_argument("--model", default=None)

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
) -> int:
    args = _build_parser().parse_args(argv)

    # 未注入 → 接線真實 seam（CLI 預設行為）；測試一律全注入 fake
    reg = registry if registry is not None else JobRegistry()
    sender = pane_sender if pane_sender is not None else TmuxPaneSender()
    creator = worktree_creator if worktree_creator is not None else ScriptWorktreeCreator()

    if args.cmd == "dispatch":
        disp = Dispatcher(reg, sender, creator)
        job = disp.dispatch(
            task=args.task, persona=args.persona,
            pane_id=args.pane, command=args.command,
        )
        print(json.dumps(job, ensure_ascii=False))
        return 0

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

    if args.cmd == "complete":
        disp = Dispatcher(reg, sender, creator)
        metas = autonomy.scan_specs(args.specs_dir) if args.specs_dir else None
        summary = manager.complete_tick(disp, handoff_dir=args.handoff_dir, metas=metas)
        print(json.dumps(summary, ensure_ascii=False))
        return 0

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

    if args.cmd == "tick":
        disp = Dispatcher(reg, sender, creator)
        metas = autonomy.scan_specs(args.specs_dir)
        # predicate 綁定 args.handoff_dir（避免 fanout 側對 DEFAULT_HANDOFF_DIR、complete
        # 側對 args.handoff_dir 的不一致）；同一 predicate 餵 unsafe 守門與 run_tick。
        predicate = is_satisfied if is_satisfied is not None else (
            lambda s: autonomy.default_is_satisfied(s, handoff_dir=args.handoff_dir)
        )
        try:
            _refuse_unsafe_fanout(metas, predicate, allow_unsafe=args.allow_unsafe)
        except ValueError as exc:  # --allow-unsafe 就緒集 >1 → fail-closed
            print(f"錯誤: {exc}", file=sys.stderr)
            return 1
        active_launcher = _resolve_launcher(
            args.executor, launcher, allow_unsafe=args.allow_unsafe, model=args.model
        )
        summary = manager.run_tick(
            disp, metas=metas, launcher=active_launcher, persona=args.persona,
            is_satisfied=predicate, handoff_dir=args.handoff_dir,
            require_idle=args.require_idle, max_load=args.max_load,
        )
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    if args.cmd in ("ready", "fanout"):
        predicate = is_satisfied if is_satisfied is not None else autonomy.default_is_satisfied
        metas = autonomy.scan_specs(args.specs_dir)
        try:
            if args.cmd == "ready":
                ready = autonomy.ready_units(metas, predicate)
                print(json.dumps(ready, ensure_ascii=False))
                return 0
            # fanout：reuse Phase 2 Dispatcher（注入或預設 seam）
            disp = Dispatcher(reg, sender, creator)
            # --allow-unsafe 旁路沙箱/核可 → fail-closed：就緒集 >1 即拒（避免大量越權派工）。
            _refuse_unsafe_fanout(metas, predicate, allow_unsafe=args.allow_unsafe)
            # --executor 設定（或測試注入 launcher）→ 走 headless launcher 路徑：
            # SubprocessLauncher 啟動 agent，dispatch_ready 記 executor/session/pid/log，
            # 且不再經 tmux pane send（與舊路徑互斥，無 double dispatch）。
            active_launcher = _resolve_launcher(
                args.executor, launcher, allow_unsafe=args.allow_unsafe, model=args.model
            )
            # git_runner 未注入 → 不傳（沿用 Dispatcher 預設真 git）；測試一律注入 fake
            jobs = autonomy.dispatch_ready(
                metas, predicate, disp, persona=args.persona,
                git_runner=git_runner, launcher=active_launcher,
            )
            print(json.dumps(jobs, ensure_ascii=False))
            return 0
        except (ValueError, autonomy.DispatchReadyRequiresLauncherError) as exc:
            # 循環相依 → refuse；fan-out 無 headless launcher → fail-fast（提示 --executor）
            print(f"錯誤: {exc}", file=sys.stderr)
            return 1

    return 2  # pragma: no cover（argparse required=True 已擋）


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
