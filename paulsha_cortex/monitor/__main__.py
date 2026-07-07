from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .config import load_config
from .scanner import scan_workspaces
from .service import ProjectMonitorService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paulsha_cortex.monitor",
        description="Stage 9 Project Monitor",
    )
    parser.add_argument("--config", help="path to paulshaclaw.yaml", default=None)
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single scan, dump JSON snapshot to stdout, exit 0",
    )
    return parser


def _snapshot_payload(states) -> dict[str, object]:
    return {"projects": [asdict(state) for state in states]}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config_path = Path(args.config) if args.config else None
        config = load_config(config_path=config_path)
    except (FileNotFoundError, ValueError) as error:
        print(f"錯誤: {error}", file=sys.stderr)
        return 1

    if args.once:
        try:
            states = scan_workspaces(config)
        except (FileNotFoundError, ValueError, OSError) as error:
            print(f"錯誤: {error}", file=sys.stderr)
            return 1

        payload = _snapshot_payload(states)
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return 0

    service: ProjectMonitorService | None = None
    try:
        service = ProjectMonitorService(config=config)
        service.run_forever()
    except KeyboardInterrupt:
        if service is not None:
            service.stop()
        return 0
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as error:
        print(f"錯誤: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
