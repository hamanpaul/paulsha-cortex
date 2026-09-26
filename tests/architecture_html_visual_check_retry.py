#!/usr/bin/env python3
"""只對已知 Chrome CDP target discovery timeout 重試 Archify visual-check。"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


CDP_TIMEOUT_SIGNATURE = "Target.getTargets: timed out after 15000ms"
MAX_RETRIES = 2


def _bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8", errors="replace")


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def run_visual_check(*, html: Path, archify: Path, output: Path, node: str = "node") -> int:
    """執行 visual-check，並為每次嘗試保留原始輸出與診斷。"""
    output.mkdir(parents=True, exist_ok=True)
    max_attempts = MAX_RETRIES + 1
    command = [node, str(archify), "visual-check", str(html), "--json"]
    combined_log = output / "visual-check.log"
    combined_log.write_text("", encoding="utf-8")

    for attempt in range(1, max_attempts + 1):
        started = datetime.now(timezone.utc).isoformat()
        spawn_error = ""
        try:
            result = subprocess.run(command, capture_output=True, check=False)
            returncode = int(result.returncode)
            stdout = _bytes(result.stdout)
            stderr = _bytes(result.stderr)
        except OSError as exc:
            returncode = 127
            stdout = b""
            stderr = b""
            spawn_error = f"{type(exc).__name__}: {exc}"

        is_cdp_timeout = (
            returncode != 0
            and CDP_TIMEOUT_SIGNATURE.encode("ascii") in stdout + b"\n" + stderr
        )
        attempt_json = output / f"visual-check-attempt-{attempt}.json"
        attempt_log = output / f"visual-check-attempt-{attempt}.log"
        attempt_json.write_bytes(stdout)
        (output / "visual-check.json").write_bytes(stdout)

        lines = [
            f"嘗試：{attempt}/{max_attempts}",
            f"開始時間 UTC：{started}",
            f"命令：{shlex.join(command)}",
            f"退出狀態：{returncode}",
            f"CDP target timeout 特徵：{'符合' if is_cdp_timeout else '不符合'}",
        ]
        if spawn_error:
            lines.append(f"啟動錯誤：{spawn_error}")
        if stderr:
            lines.extend(("stderr：", _text(stderr).rstrip("\n")))
        log_text = "\n".join(lines) + "\n"
        attempt_log.write_text(log_text, encoding="utf-8")
        with combined_log.open("a", encoding="utf-8") as stream:
            stream.write(log_text)

        if stderr:
            print(f"--- visual-check 第 {attempt}/{max_attempts} 次 stderr ---", file=sys.stderr)
            sys.stderr.write(_text(stderr))
            if not stderr.endswith(b"\n"):
                sys.stderr.write("\n")
        if spawn_error:
            print(f"Architecture HTML visual-check 啟動失敗：{spawn_error}", file=sys.stderr)

        if returncode == 0:
            print(f"Architecture HTML visual-check 第 {attempt}/{max_attempts} 次成功。")
            return 0
        if not is_cdp_timeout:
            print(
                f"Architecture HTML visual-check 第 {attempt}/{max_attempts} 次發生非 CDP timeout 失敗；停止重試。",
                file=sys.stderr,
            )
            return returncode
        if attempt < max_attempts:
            print(
                f"Architecture HTML visual-check 第 {attempt}/{max_attempts} 次符合 CDP target timeout 特徵；將重試。",
                file=sys.stderr,
            )
            continue

        with combined_log.open("a", encoding="utf-8") as stream:
            stream.write(f"結束摘要：達到 {max_attempts} 次執行上限，持續出現 CDP target timeout。\n")
        print(
            f"Architecture HTML visual-check 在 {max_attempts} 次執行後仍遇到 CDP target timeout；失敗關閉。",
            file=sys.stderr,
        )
        return returncode

    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--archify", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    return run_visual_check(html=args.html, archify=args.archify, output=args.output, node=args.node)


if __name__ == "__main__":
    raise SystemExit(main())
