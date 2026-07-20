"""cortex 傘狀入口：install 子樹走 installer，其餘透傳 coordinator CLI。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Sequence

_USAGE = "usage: cortex [-h] <command> [<args>...]\n"
_HELP = """\
usage: cortex [-h] <command> [<args>...]

paulsha-cortex 檔案驅動的 Agent 派工與交付治理 CLI

setup and workflow commands:
  install service  安裝 manager service/timer 與 monitor 的 systemd --user units
  deck             預覽或產生 dispatch:hold 的 slice specs
  monitor          掃描專案文件並輸出 Project Monitor 狀態
  list             列出統一 Work Item read model
  work show        顯示單一 Work Item 與可解釋關聯
  doctor           檢查 gh、preflight、model identity、agy 與 service paths
  relay-hook       執行封裝內 relay hook（整合用途）

coordinator commands:
  status           讀取 manager daemon 綜合狀態
  ready            列出符合派工條件的 specs
  jobs, stat       查詢 Job 執行紀錄
  fanout           派送目前 ready 的 slices
  tick             執行 fanout + completion/review 流程
  complete         輪詢既有 jobs 並執行 verification/review/completion
  slice-action     對 needs_human slice 執行允許的 recovery action
  work             透過 Manager 單一 writer 執行 work lifecycle action
  reap-brokers     dry-run 或受限清理孤兒 Codex broker
  dispatch         已停用的舊低階入口

run 'cortex <command> --help' for command-specific help.
"""

_WORK_HELP = """\
usage: cortex work <show|link|unlink|start|resume|retry-build|abandon|auto|review-attest|ship> ...

work item commands:
  show      從 Monitor 讀取 Work Item 與關聯解釋
  link      由 Manager 寫入 confirmed association
  unlink    由 Manager 寫入 exclusion
  start     手動 claim 並建立 WorkflowRun
  resume    恢復 needs_human／blocked workflow
  retry-build  以 exact Candidate CAS 重開最後一個 builder card
  abandon   以 exact WorkflowRun CAS 將 pre-delivery run 標成 superseded
  auto      管理 cortex:auto-on-going issue label
  review-attest  建立 exact-HEAD maintainer review evidence
  ship      執行 fail-closed delivery state machine

run 'cortex work show --help' or coordinator mutation help for arguments.
"""


def _relay_hook_script_path() -> Path:
    return Path(str(resources.files("paulsha_cortex") / "scripts" / "psc-relay-hook.sh"))


def main(argv: Sequence[str] | None = None, *, work_client=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write(_USAGE)
        return 2
    if args[0] in {"-h", "--help"}:
        sys.stdout.write(_HELP)
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
    if args[0] == "monitor":
        from paulsha_cortex.monitor.__main__ import main as monitor_main

        return int(monitor_main(args[1:]) or 0)
    if args[0] == "list":
        return _work_read_main(args, work_client=work_client)
    if args[0] == "work":
        if len(args) == 1 or args[1] in {"-h", "--help"}:
            sys.stdout.write(_WORK_HELP)
            return 0
        if args[1] == "show":
            return _work_read_main(args, work_client=work_client)
    if args[0] == "doctor":
        from paulsha_cortex.doctor import main as doctor_main

        return int(doctor_main(args[1:]) or 0)

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
        if parsed.explain:
            print(json.dumps(data.get("explanation", {}), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
