from __future__ import annotations

import argparse
import json
import signal
import sys
from dataclasses import asdict
from pathlib import Path

from .config import load_config
from .scanner import scan_workspaces
from .service import ProjectMonitorService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex monitor",
        description="依 workspace 文件訊號推導專案狀態；不取代 coordinator delivery status。",
    )
    parser.add_argument(
        "--config",
        help="project-cortex.yaml 路徑（預設依環境變數與標準位置解析）",
        default=None,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="單次掃描後輸出 JSON 並結束；未指定則啟動長駐服務",
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
    previous_sigterm = None
    try:
        service = ProjectMonitorService(config=config)
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda _signum, _frame: service.stop())
        service.run_forever()
    except KeyboardInterrupt:
        if service is not None:
            service.stop()
        return 0
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as error:
        print(f"錯誤: {error}", file=sys.stderr)
        return 1
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
