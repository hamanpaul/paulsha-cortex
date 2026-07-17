"""cortex 傘狀入口：install 子樹走 installer，其餘透傳 coordinator CLI。"""
from __future__ import annotations

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
  relay-hook       執行封裝內 relay hook（整合用途）

coordinator commands:
  status           讀取 manager daemon 綜合狀態
  ready            列出符合派工條件的 specs
  jobs, stat       查詢 Job 執行紀錄
  fanout           派送目前 ready 的 slices
  tick             執行 fanout + completion/review 流程
  complete         輪詢既有 jobs 並執行 verification/review/completion
  slice-action     對 needs_human slice 執行允許的 recovery action
  reap-brokers     dry-run 或受限清理孤兒 Codex broker
  dispatch         已停用的舊低階入口

run 'cortex <command> --help' for command-specific help.
"""


def _relay_hook_script_path() -> Path:
    return Path(str(resources.files("paulsha_cortex") / "scripts" / "psc-relay-hook.sh"))


def main(argv: Sequence[str] | None = None) -> int:
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

    from paulsha_cortex.coordinator.cli import main as coordinator_main

    return int(coordinator_main(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
