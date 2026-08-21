"""cortex 傘狀入口：install 子樹走 installer，其餘透傳 coordinator CLI。"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Sequence

_USAGE = "usage: cortex [-h] [--version] <command> [<args>...]\n"
_HELP = """\
usage: cortex [-h] [--version] <command> [<args>...]

paulsha-cortex 檔案驅動的 Agent 派工與交付治理 CLI

options:
  -h, --help      show this help message and exit
  --version       show installed version and exit

setup and workflow commands:
  install service  安裝 manager service/timer 與 monitor 的 systemd --user units
  deck             預覽或產生 dispatch:hold 的 slice specs
  skill            skill usage ledger 檢視與 park/janitor 操作（inspect/park/restore/...）
  monitor          掃描專案文件並輸出 Project Monitor 狀態
  egress-proxy     出口 proxy 服務（#716；--check 只印生效設定與白名單）
  list             列出統一 Work Item read model
  work show        顯示單一 Work Item 與可解釋關聯
  doctor           檢查 gh、preflight、model identity、agy 與 service paths
  control lock-path 印出 manager.lock 契約路徑（shell wrapper／daemon 同源，整合用途）
  relay-hook       執行封裝內 relay hook（整合用途）

coordinator commands:
  status           讀取 manager daemon 綜合狀態
  ready            列出符合派工條件的 specs
  jobs, stat       查詢 Job 執行紀錄與 combo/retry 彙總
  fanout           派送目前 ready 的 slices
  tick             執行 fanout + completion/review 流程
  complete         輪詢既有 jobs 並執行 verification/review/completion
  slice-action     對 needs_human slice 執行允許的 recovery action
  work             透過 Manager 單一 writer 執行 work lifecycle action
  outcome          讀取 canonical engineering outcome outbox（唯讀）
  reap-brokers     dry-run 或受限清理孤兒 Codex broker
  dispatch         已停用的舊低階入口

run 'cortex <command> --help' for command-specific help.
"""

_WORK_HELP = """\
usage: cortex work <show|gc|link|unlink|intake|start|resume|retry-build|retry-card|retry-verify|retry-review|recover-planning|recover-pre-candidate|recover-repair-commit|regenerate-gates|abandon|retire-delivered|recover-superseded|reset-reclaim-budget|refreeze-base|auto|review-attest|ship> ...

work item commands:
  show      從 Monitor 讀取 Work Item 與關聯解釋
  gc        proposal-first 回收殘留 build worktree 與已 merge local branch（唯讀 registry）
  link      由 Manager 寫入 confirmed association
  unlink    由 Manager 寫入 exclusion
  intake    「拿到一個 issue/task 就進件」的單一入口，等價於（必要時）link 後接 start
  start     手動 claim 並建立 WorkflowRun（可用 --combo 明示 override）
  resume    恢復 needs_human／blocked workflow
  retry-build  以 exact Candidate CAS 重開最後一個 builder card
  retry-card  以 exact WorkflowRun CAS + --card 重派最早一張尚未採信的 builder（含中段卡）或 reviewer card（verify／review；已採信的 evidence 不可重派）
  retry-verify  以 exact Candidate CAS 只重跑 verification，不重建 candidate
  retry-review  以 exact Candidate CAS 只重跑 foreign review，不重跑 builder
  abandon   以 exact WorkflowRun CAS 將 pre-delivery run 標成 superseded
  retire-delivered  以 exact WorkflowRun CAS 退休交付已在管線外完成、pr_refs 全 terminal（merged/closed）的孤兒 run
  recover-superseded  以 exact WorkflowRun CAS 撿回被識別失誤 supersede 的已驗證 run（需 candidate＋PR、verify/review phase、同 work 無 ongoing run；動作＝復歸 ongoing＋official authority-restart）
  recover-planning  對 define/needs_human 的 planning 失敗作可恢復重跑
  recover-pre-candidate  對 candidate 產生前的 builder 失敗作可恢復重跑並回收 worktree
  recover-repair-commit  對 repair commit 已存在但缺 terminal evidence 的 build 失敗做具 CAS 的採納恢復
  regenerate-gates  以 exact WorkflowRun CAS，對既有 builder job log 依當前 PSC_GATE_CMD_* 宣告重跑 gate 並重寫 ledger（不改判、不重派 builder）
  reset-reclaim-budget  明示重置 semantic-reclaim 世代熔斷計數（需 --actor／--reason，落稽核 evidence）
  refreeze-base  以 exact WorkflowRun CAS 把還活著的 run 的候選 git base 重新凍結到目前的 origin/main（需 --actor／--reason，fast-forward only，落稽核 evidence；已有被採信 candidate／in-flight job／build branch 帶外來 commit 時一律拒絕）
  auto      管理 cortex:auto-on-going issue label
  review-attest  建立 exact-HEAD maintainer review evidence
  ship      執行 fail-closed delivery state machine

`cortex stat --combo-selections` 可彙總自動選牌／override／bypass 的來源與 task_type。

claim 前置條件（intake／start 共用）：work item 必須先進到 lifecycle `todo`
狀態才會產生可 claim 的 `start` next_action。只 link 一個 GitHub issue（`topic`
狀態）不足夠——還需要一個 active 的 Todo 來源（workstream `todo.md` 的 path
link、accepted superpowers spec/plan，或 active OpenSpec change）。純 issue-only
work item 對 intake/start 呼叫 fail-closed 時，錯誤訊息會標示 work item 目前
所在的 lifecycle state 與缺少的 Todo 來源；詳見 docs/unified-work-lifecycle.md。

run 'cortex work show --help' or coordinator mutation help for arguments.
"""


def _relay_hook_script_path() -> Path:
    return Path(str(resources.files("paulsha_cortex") / "scripts" / "psc-relay-hook.sh"))


def _load_porcelain_commands():
    porcelain = importlib.import_module("paulsha_cortex.porcelain")

    porcelain.load_commands()
    return porcelain.COMMANDS


def _help_text() -> str:
    commands = _load_porcelain_commands()
    if not commands:
        return _HELP
    lines = [_HELP.rstrip(), "", "porcelain commands:"]
    for command in sorted(commands.values(), key=lambda item: item.name):
        lines.append(f"  {command.name:<16}{command.help}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, work_client=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write(_USAGE)
        return 2
    if args[0] == "--version":
        try:
            current_version = importlib.metadata.version("paulsha-cortex")
        except importlib.metadata.PackageNotFoundError:
            current_version = "0.0.0+unknown"
        sys.stdout.write(f"cortex {current_version}\n")
        return 0
    if args[0] in {"-h", "--help"}:
        sys.stdout.write(_help_text())
        return 0
    if args[0] == "install":
        from paulsha_cortex.deploy.installer import main as install_main

        return int(install_main(args[1:]) or 0)
    if args[0] == "relay-hook":
        script = str(_relay_hook_script_path())
        try:
            os.execv(script, [script, *args[1:]])
        except OSError:
            # packaged 腳本非可執行（wheel 留 0644）或 noexec 掛載時，
            # 改由 bash 讀取執行（只需讀權限）。
            os.execv("/usr/bin/env", ["env", "bash", script, *args[1:]])
    if args[0] == "deck":
        from paulsha_cortex.deck.cli import main as deck_main

        return int(deck_main(args[1:]) or 0)
    if args[0] == "skill":
        from paulsha_cortex.coordinator.skill_cli import main as skill_main

        return int(skill_main(args[1:]) or 0)
    if args[0] == "monitor":
        from paulsha_cortex.monitor.__main__ import main as monitor_main

        return int(monitor_main(args[1:]) or 0)
    if args[0] == "egress-proxy":
        # #716：出口 proxy 服務的進入點（`cortex-egress-proxy.service` 的 ExecStart=）。
        # 形態與 `cortex monitor` 一致：部署 venv 的 console script ＋ 一個 CLI verb。
        from paulsha_cortex.trust_root.egress_proxy import main as egress_proxy_main

        return int(egress_proxy_main(args[1:]) or 0)
    if args[0] == "list":
        return _work_read_main(args, work_client=work_client)
    if args[0] == "work":
        if len(args) == 1 or args[1] in {"-h", "--help"}:
            sys.stdout.write(_WORK_HELP)
            return 0
        if args[1] == "show":
            return _work_read_main(args, work_client=work_client)
        if args[1] == "gc":
            from paulsha_cortex.coordinator import gc as gc_module

            return int(gc_module.main(args[2:]) or 0)
    if args[0] == "doctor":
        from paulsha_cortex.doctor import main as doctor_main

        return int(doctor_main(args[1:]) or 0)
    if args[0] == "control":
        from paulsha_cortex.control.cli import main as control_main

        return int(control_main(args[1:]) or 0)
    porcelain_commands = _load_porcelain_commands()
    porcelain_command = porcelain_commands.get(args[0])
    if porcelain_command is not None:
        return int(porcelain_command.run(args[1:]) or 0)

    from paulsha_cortex.coordinator.cli import main as coordinator_main

    return int(coordinator_main(args) or 0)


def _build_work_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex", add_help=False)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="列出 Work Items")
    listing.add_argument("--repo", default=None, help="只列指定 owner/repo")
    listing.add_argument(
        "--state",
        action="append",
        choices=("topic", "todo", "ongoing", "on-going", "done"),
        default=[],
        help="依 lifecycle state 過濾；可重複指定",
    )
    listing.add_argument("--all", action="store_true", help="包含 done")
    listing.add_argument("--json", action="store_true", help="輸出 cortex-work/v1 JSON")
    listing.add_argument("--explain", action="store_true", help="附 correlation/reducer 解釋")

    work = sub.add_parser("work", help="單一 Work Item 操作")
    work_sub = work.add_subparsers(dest="work_command", required=True)
    show = work_sub.add_parser("show", help="顯示單一 Work Item")
    show.add_argument("work_id")
    show.add_argument("--repo", default=None, help="指定 owner/repo 以消除同名歧義")
    show.add_argument("--json", action="store_true", help="輸出 cortex-work/v1 JSON")
    show.add_argument("--explain", action="store_true", help="附 correlation/reducer 解釋")
    return parser


def _format_candidate_git_base(payload: object) -> list[str]:
    """`cortex work show` 的候選 git base 區塊（#731 (C)）。

    純函式（不 print、不做 I/O），好讓測試直接對輸出逐字斷言。缺欄位／
    legacy Monitor 回傳沒有這一欄時回空清單，維持舊行為。
    """

    if not isinstance(payload, dict) or not payload:
        return []
    sha = payload.get("sha")
    behind = payload.get("behind_origin_main")
    lines = [f"candidate_git_base: {sha or '-'}"]
    if payload.get("sha_source"):
        lines.append(f"  source: {payload.get('sha_source')}")
    if payload.get("run_id"):
        lines.append(f"  run_id: {payload.get('run_id')}")
    if behind is not None:
        # 誠實標示比較基準：距離是相對 **mirror 上次 fetch 到的 main**，
        # 不是相對 GitHub 此刻的 main——本路徑唯讀，不 fetch。
        lines.append(
            f"  behind {payload.get('measured_against', 'origin/main')}: {behind}"
            f" (mirror main={payload.get('mirror_origin_main') or '-'},"
            f" fetched={str(bool(payload.get('fetched'))).lower()})"
        )
    if payload.get("reason"):
        lines.append(
            f"  reason: {payload.get('reason')}"
            f" (threshold={payload.get('threshold_commits')})"
        )
    # `source_revision` 的誤導本身就是缺陷的一部分（#731）：只要印了 git base，
    # 就一併點明另一欄是什麼，讓兩者不會再被讀成同一件事。
    lines.append(
        "  註：`source_revision` 是 work item 來源材料的 sha256"
        "（authority digest，64-hex），與 git base 無關。"
    )
    return lines


def _work_read_main(args: list[str], *, work_client=None) -> int:
    from paulsha_cortex.monitor.work_api import MonitorSocketClient

    parsed = _build_work_parser().parse_args(args)
    client = work_client or MonitorSocketClient()
    if parsed.command == "list":
        request = {
            "kind": "list_work_items",
            "repo": parsed.repo,
            "states": parsed.state,
            "include_done": parsed.all,
            "explain": parsed.explain,
        }
    else:
        request = {
            "kind": "explain_work_item" if parsed.explain else "get_work_item",
            "work_id": parsed.work_id,
        }
        if parsed.repo is not None:
            request["repo"] = parsed.repo
    try:
        response = client.request(request)
    except (OSError, RuntimeError) as error:
        print(f"錯誤: 無法讀取 Monitor：{error}", file=sys.stderr)
        return 1
    if not response.get("ok"):
        print(f"錯誤: {response.get('error', 'Monitor request failed')}", file=sys.stderr)
        return 1
    data = response.get("data")
    if not isinstance(data, dict):
        print("錯誤: Monitor response data 不是 JSON object", file=sys.stderr)
        return 1
    if parsed.json:
        print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        return 0
    if parsed.command == "list":
        for item in data.get("items", []):
            facets = ",".join(item.get("facets", [])) or "-"
            print(
                f"{item.get('repo', '-')}\t{item.get('work_id', '-')}\t"
                f"{item.get('state', '-')}\t{item.get('phase') or '-'}\t{facets}"
            )
            if parsed.explain:
                identity = f"{item.get('repo')}::{item.get('work_id')}"
                explanation = data.get("explanations", {}).get(
                    identity,
                    data.get("explanations", {}).get(item.get("work_id"), {}),
                )
                print(json.dumps(explanation, ensure_ascii=False, sort_keys=True))
    else:
        item = data.get("item", {})
        print(f"{item.get('work_id', '-')}  {item.get('state', '-')}  {item.get('title', '-')}")
        print(f"repo: {item.get('repo', '-')}")
        print(f"phase: {item.get('phase') or '-'}")
        # 診斷 invariant（#527）：run 掛著 needs_human 時的結構化理由。過去
        # `work show` 只印 work_id/state/title/repo/phase，理由（若存在）連
        # `--json` 都拿不到——這是「四個介面同時沉默」的其中一個。
        blocking = data.get("blocking_reason")
        if isinstance(blocking, dict) and blocking.get("reason"):
            print(f"needs_human: {blocking.get('reason')}: {blocking.get('detail')}")
            print(f"  source: {blocking.get('source')}")
            if blocking.get("run_id"):
                print(f"  run_id: {blocking.get('run_id')}")
            for ref in blocking.get("evidence_refs") or []:
                print(f"  evidence: {ref}")
        # #731 (C)：候選 git base。過去這個事實只存在於候選 worktree 的 `.git`
        # 裡，`work show` 上唯一像版本的欄位是 `source_revision`——那是 work item
        # 來源材料的 sha256（authority digest，64-hex），**與 git base 無關**，
        # 於是 0819 現場它把診斷帶偏了兩次。這裡逐行寫明兩者的分工。
        for line in _format_candidate_git_base(data.get("candidate_git_base")):
            print(line)
        if parsed.explain:
            print(json.dumps(data.get("explanation", {}), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
