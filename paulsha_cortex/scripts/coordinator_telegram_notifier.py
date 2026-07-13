#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CHECKBOX_RE = re.compile(r"^- \[ \]", re.MULTILINE)
TERMINAL = {"exited", "failed"}


@dataclass
class TaskMeta:
    workstream: str
    topic: str
    worktree: Path
    todo_total: int


def run_cmd(cmd: List[str]) -> str:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc.stdout


def send_notify(api_token_path: Path, text: str) -> None:
    api_token = api_token_path.read_text(encoding="utf-8").strip()
    payload = json.dumps({"text": text}, ensure_ascii=False)
    cmd = [
        "curl",
        "-s",
        "-X",
        "POST",
        "http://127.0.0.1:7777/notify",
        "-H",
        f"Authorization: Bearer {api_token}",
        "-H",
        "Content-Type: application/json",
        "-d",
        payload,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"notify curl failed: {proc.stderr.strip()}")


def unchecked_count(path: Path) -> int:
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    return len(CHECKBOX_RE.findall(text))


def remaining_count(meta: TaskMeta) -> int:
    task_path = meta.worktree / "docs/superpowers/workstreams" / meta.workstream / "task.md"
    todo_path = meta.worktree / "docs/superpowers/workstreams" / meta.workstream / "todo.md"
    return unchecked_count(task_path) + unchecked_count(todo_path)


def load_meta(path: Path) -> List[TaskMeta]:
    rows: List[TaskMeta] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ws, topic, worktree, total = line.split("\t")
        rows.append(TaskMeta(ws, topic, Path(worktree), int(total)))
    return rows


def _state_path(state_root: Path) -> Path:
    return state_root if state_root.suffix == ".json" else state_root / "jobs.json"


def default_api_token_path() -> Path:
    root = os.environ.get("PSC_MAX_ROOT", "").strip()
    if root:
        return Path(root).expanduser() / "api-token"
    return Path.home() / ".max" / "api-token"


def latest_job_status_by_topic(state_root: Path, limit: int = 500) -> Dict[str, str]:
    payload = json.loads(_state_path(state_root).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("coordinator 狀態檔格式錯誤：頂層必須為 object")
    jobs = payload.get("jobs", [])
    if not isinstance(jobs, list):
        raise ValueError("coordinator 狀態檔格式錯誤：jobs 必須為 list")
    out: Dict[str, str] = {}
    for row in reversed(jobs[-limit:]):
        if not isinstance(row, dict):
            continue
        topic = row.get("topic") or row.get("task")
        status = row.get("status")
        if not topic or not status:
            continue
        if topic not in out:
            out[topic] = status
    return out


def fmt_progress(meta: TaskMeta, job_status: str, remaining: int) -> str:
    total = max(meta.todo_total, 1)
    in_dev = max(total - remaining, 0)
    progress = int(round(((total - remaining) / total) * 100))
    return (
        f"{meta.workstream}: 狀態={job_status} / 待辦={remaining} / "
        f"開發驗證中工作={in_dev} / 剩餘工作數量={remaining} / 工作完成進度={progress}%"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--meta-file", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--interval-sec", type=int, default=1800)
    parser.add_argument("--api-token-path", default=str(default_api_token_path()))
    args = parser.parse_args()

    meta = load_meta(Path(args.meta_file))
    state_root = Path(args.state_root)
    token_path = Path(args.api_token_path)

    total_all = sum(x.todo_total for x in meta)
    next_notify = time.time() + max(60, args.interval_sec)
    print(
        f"[notifier] start run_id={args.run_id} tasks={len(meta)} total_todo={total_all}",
        flush=True,
    )

    while True:
        try:
            status_map = latest_job_status_by_topic(state_root)
        except Exception as exc:  # noqa: BLE001
            send_notify(token_path, f"進度更新錯誤 run_id={args.run_id}：{exc}")
            time.sleep(60)
            continue

        lines: List[str] = []
        success = 0
        failed = 0
        sum_remaining = 0
        all_terminal = True

        for item in meta:
            status = status_map.get(item.topic, "queued")
            remaining = remaining_count(item)
            sum_remaining += remaining
            lines.append(fmt_progress(item, status, remaining))

            if status == "exited":
                success += 1
            if status == "failed":
                failed += 1
            if status not in TERMINAL:
                all_terminal = False

        overall_progress = int(round(((total_all - sum_remaining) / max(total_all, 1)) * 100))
        now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        print(
            f"[notifier] loop run_id={args.run_id} all_terminal={all_terminal} "
            f"remaining={sum_remaining} progress={overall_progress}%",
            flush=True,
        )

        if time.time() >= next_notify:
            msg = "\n".join([
                f"進度更新 run_id={args.run_id} @ {now}",
                *lines,
                (
                    f"總覽: 待辦={sum_remaining} / 開發驗證中工作={max(total_all - sum_remaining, 0)} / "
                    f"剩餘工作數量={sum_remaining} / 工作完成進度={overall_progress}%"
                ),
            ])
            send_notify(token_path, msg)
            next_notify = time.time() + max(60, args.interval_sec)

        if all_terminal:
            summary = "\n".join([
                f"任務總結 run_id={args.run_id} @ {now}",
                f"任務總數={len(meta)} / 成功={success} / 失敗或中止={failed}",
                f"總待辦={total_all} / 剩餘={sum_remaining} / 完成進度={overall_progress}%",
                *lines,
            ])
            send_notify(token_path, summary)
            return 0

        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
