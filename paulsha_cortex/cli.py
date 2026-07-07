"""cortex 傘狀入口：install 子樹走 installer，其餘透傳 coordinator CLI。"""
from __future__ import annotations

import sys
from typing import Sequence

_USAGE = "usage: cortex {install service|<coordinator subcommand>} <args...>\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write(_USAGE)
        return 2
    if args[0] == "install":
        from paulsha_cortex.deploy.installer import main as install_main

        return int(install_main(args[1:]) or 0)

    from paulsha_cortex.coordinator.cli import main as coordinator_main

    return int(coordinator_main(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
