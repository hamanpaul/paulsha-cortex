"""cortex 傘狀入口：install 子樹走 installer，其餘透傳 coordinator CLI。"""
from __future__ import annotations

import os
import sys
from importlib import resources
from pathlib import Path
from typing import Sequence

_USAGE = "usage: cortex {install service|relay-hook|<coordinator subcommand>} <args...>\n"


def _relay_hook_script_path() -> Path:
    return Path(str(resources.files("paulsha_cortex") / "scripts" / "psc-relay-hook.sh"))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write(_USAGE)
        return 2
    if args[0] == "install":
        from paulsha_cortex.deploy.installer import main as install_main

        return int(install_main(args[1:]) or 0)
    if args[0] == "relay-hook":
        script = str(_relay_hook_script_path())
        os.execv(script, [script, *args[1:]])

    from paulsha_cortex.coordinator.cli import main as coordinator_main

    return int(coordinator_main(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
