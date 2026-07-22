from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from . import COMMANDS, PorcelainCommand, register
from . import inspect as inspect_family
from . import service as service_family

BOOTSTRAP_SCHEMA = "cortex-porcelain/bootstrap/v1"
_EXECUTOR_CANDIDATES: tuple[str, ...] = ("copilot", "claude", "codex")


def register_commands() -> None:
    if "bootstrap" in COMMANDS:
        return
    register(PorcelainCommand(name="bootstrap", help="從 preflight 到 service 啟動與健檢", run=main))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex bootstrap")
    parser.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
    parser.add_argument("--repo-root", default=str(Path.cwd()))
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true", help="只做 preflight 與預覽後續命令")
    parser.add_argument(
        "--sample",
        nargs="?",
        const="feature-oneshot",
        metavar="COMBO",
        help="選配：建立第一個 sample workflow（省略值時預設 feature-oneshot）",
    )
    parser.add_argument("--task", help="sample workflow 的 task 描述")
    parser.add_argument("--change", help="sample workflow 的 OpenSpec change ID")
    parser.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/bootstrap/v1 JSON")
    parser.add_argument("--start", dest="start", action="store_true", default=True, help="安裝後立即啟動 service")
    parser.add_argument("--no-start", dest="start", action="store_false", help="只安裝，不啟動 service")
    return parser


def _json_dump(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _bootstrap_envelope(**payload: Any) -> dict[str, Any]:
    return {"schema": BOOTSTRAP_SCHEMA, **payload}


def _run(argv: list[str], *, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "sample"


def _preflight_check(name: str, ok: bool, detail: str, *, fix: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "ok": ok, "detail": detail}
    if fix is not None:
        payload["fix"] = fix
    return payload


def _git_repo_root(repo_root: Path) -> tuple[bool, str]:
    result = _run(["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return False, "目前路徑不是 git repo；請切到 repo 內，或用 --repo-root 指向 repo 根目錄。"
    return True, (result.stdout.strip() or str(repo_root))


def run_preflight(*, repo_root: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    python_ok = sys.version_info >= (3, 10)
    checks.append(
        _preflight_check(
            "python",
            python_ok,
            f"Python {sys.version.split()[0]}",
            fix="請使用 Python 3.10 以上版本。" if not python_ok else None,
        )
    )

    git_path = shutil.which("git")
    git_ok = git_path is not None
    checks.append(
        _preflight_check(
            "git",
            git_ok,
            git_path or "git 不在 PATH",
            fix="請先 install Git，並確認 `git --version` 可用。" if not git_ok else None,
        )
    )

    repo_path = Path(repo_root).expanduser().resolve()
    repo_ok = False
    effective_repo_root = str(repo_path)
    if git_ok:
        repo_ok, repo_detail = _git_repo_root(repo_path)
        if repo_ok:
            effective_repo_root = repo_detail
        checks.append(
            _preflight_check(
                "repo",
                repo_ok,
                repo_detail if repo_ok else f"{repo_path}: {repo_detail}",
                fix=repo_detail if not repo_ok else None,
            )
        )
    else:
        checks.append(
            _preflight_check(
                "repo",
                False,
                f"{repo_path}: 無法驗證 repo，因為 git 不可用。",
                fix="先修復 git，再重新執行 bootstrap。",
            )
        )

    gh_path = shutil.which("gh")
    gh_ok = gh_path is not None
    if gh_ok:
        gh_status = _run(["gh", "auth", "status"])
        gh_ok = gh_status.returncode == 0
        gh_detail = "gh auth status ok" if gh_ok else "gh 已安裝，但尚未登入。"
        gh_fix = None if gh_ok else "請先執行 `gh auth login`。"
    else:
        gh_detail = "gh 不在 PATH"
        gh_fix = "請先安裝 GitHub CLI（gh），再執行 `gh auth login`。"
    checks.append(_preflight_check("gh-auth", gh_ok, gh_detail, fix=gh_fix))

    found_executors = [name for name in _EXECUTOR_CANDIDATES if shutil.which(name)]
    checks.append(
        _preflight_check(
            "executor",
            bool(found_executors),
            ", ".join(found_executors) if found_executors else "找不到 executor CLI",
            fix=(
                "請安裝並登入至少一個 executor CLI：copilot、claude 或 codex。"
                if not found_executors
                else None
            ),
        )
    )

    return {"ok": all(check["ok"] for check in checks), "checks": checks, "repo_root": effective_repo_root}


def _planned_commands(
    *,
    instance: str,
    repo_root: str,
    interval: int,
    start: bool,
    sample: str | None,
    task: str | None,
    change: str | None,
) -> list[str]:
    commands = [
        f"cortex service install --instance {instance} --repo-root {repo_root} --interval {interval}",
    ]
    if start:
        commands.append(f"cortex service start --instance {instance}")
    if sample is not None:
        sample_change = change or _slugify(task or sample)
        commands.append(f"cortex init-sample --combo {sample} --task {task or ''} --change {sample_change}".strip())
    return commands


def _next_steps(*, instance: str, start: bool) -> list[str]:
    if start:
        return [
            "cortex inspect status --json",
            f"cortex inspect doctor --instance {instance} --probe-live --json",
            "cortex jobs",
        ]
    return [
        f"cortex service start --instance {instance}",
        "cortex inspect status --json",
    ]


def _print_preflight_failure(preflight: dict[str, Any]) -> None:
    sys.stderr.write("bootstrap preflight 未通過；exit code 4。\n")
    for check in preflight.get("checks", []):
        if not check.get("ok"):
            sys.stderr.write(f"- {check.get('name')}: {check.get('detail')}\n")
            fix = check.get("fix")
            if isinstance(fix, str) and fix:
                sys.stderr.write(f"  修法：{fix}\n")


def _print_human_summary(payload: dict[str, Any]) -> None:
    sys.stdout.write(f"bootstrap ready: {payload.get('instance')}\n")
    sys.stdout.write(f"repo_root: {payload.get('repo_root')}\n")
    install = payload.get("install")
    if isinstance(install, dict):
        sys.stdout.write(f"install: {install.get('message', 'ok')}\n")
    start = payload.get("start")
    if isinstance(start, dict):
        error = start.get("error")
        if isinstance(error, str):
            sys.stdout.write(f"start: {error}\n")
        else:
            sys.stdout.write("start: ok\n")
    elif payload.get("start_skipped"):
        sys.stdout.write("start: skipped (--no-start)\n")
    status = payload.get("status")
    if isinstance(status, dict):
        sys.stdout.write(
            "status: "
            + json.dumps(
                {
                    "ready": status.get("ready", []),
                    "held": status.get("held", []),
                    "degraded": status.get("degraded"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    doctor = payload.get("doctor")
    if isinstance(doctor, dict):
        sys.stdout.write(f"doctor: ok={doctor.get('ok')}\n")
    sample = payload.get("sample")
    if isinstance(sample, dict) and sample.get("requested"):
        if sample.get("ok"):
            sys.stdout.write(f"sample: ok ({sample.get('combo')})\n")
        else:
            sys.stdout.write(f"sample: degraded ({sample.get('error')})\n")
    sys.stdout.write("next:\n")
    for line in payload.get("next_steps", []):
        sys.stdout.write(f"  - {line}\n")


def _result_exit_code(payload: dict[str, Any]) -> int:
    result = payload.get("result")
    if isinstance(result, dict):
        code = result.get("exit_code")
        if isinstance(code, int):
            return code
    return 1


def init_sample(*, combo: str, task: str, change: str | None = None) -> dict[str, Any]:
    from paulsha_cortex.deck.cli import main as deck_main

    effective_change = change or _slugify(task)
    argv = [
        "compile",
        combo,
        "--task",
        task,
        "--change",
        effective_change,
        "--allow-external",
        "--emit",
    ]
    exit_code = int(deck_main(argv) or 0)
    if exit_code != 0:
        raise RuntimeError(f"init-sample failed with exit code {exit_code}")
    return {"combo": combo, "task": task, "change": effective_change, "exit_code": exit_code}


def main(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    effective_sample_task = args.task or "sample workflow"

    preflight = run_preflight(repo_root=args.repo_root)
    payload: dict[str, Any] = _bootstrap_envelope(
        command="bootstrap",
        instance=args.instance,
        repo_root=preflight["repo_root"],
        dry_run=args.dry_run,
        preflight=preflight,
        planned_commands=_planned_commands(
            instance=args.instance,
            repo_root=preflight["repo_root"],
            interval=args.interval,
            start=args.start,
            sample=args.sample,
            task=effective_sample_task if args.sample is not None else args.task,
            change=args.change,
        ),
        next_steps=_next_steps(instance=args.instance, start=args.start),
    )
    if not preflight["ok"]:
        if args.json:
            _json_dump(payload)
        else:
            _print_preflight_failure(preflight)
        return 4

    if args.dry_run:
        if args.json:
            _json_dump(payload)
        else:
            sys.stdout.write("dry-run preview:\n")
            for command in payload["planned_commands"]:
                sys.stdout.write(f"  - {command}\n")
        return 0

    try:
        install_payload = service_family.install(
            instance=args.instance,
            interval=args.interval,
            repo_root=preflight["repo_root"],
        )
    except ValueError as exc:
        install_payload = {"result": {"exit_code": 1}, "error": str(exc), "message": str(exc)}
    payload["install"] = install_payload
    if _result_exit_code(install_payload) != 0:
        if args.json:
            _json_dump(payload)
        else:
            error = install_payload.get("error") or install_payload.get("message") or "service install failed"
            sys.stderr.write(str(error) + "\n")
        return 1

    if args.start:
        start_payload = service_family.start(instance=args.instance)
        payload["start"] = start_payload
        if _result_exit_code(start_payload) != 0:
            if args.json:
                _json_dump(payload)
            else:
                error = start_payload.get("error") or "service start failed"
                sys.stderr.write(str(error) + "\n")
            return 1
    else:
        payload["start_skipped"] = True

    payload["status"] = inspect_family.status_summary()
    payload["doctor"] = inspect_family.doctor_summary(instance=args.instance)

    if args.sample is not None:
        try:
            sample_payload = init_sample(combo=args.sample, task=effective_sample_task, change=args.change)
            payload["sample"] = {"requested": True, "ok": True, **sample_payload}
        except Exception as exc:
            payload["sample"] = {
                "requested": True,
                "ok": False,
                "combo": args.sample,
                "task": effective_sample_task,
                "change": args.change,
                "error": str(exc),
            }
    else:
        payload["sample"] = {"requested": False}

    if args.json:
        _json_dump(payload)
    else:
        _print_human_summary(payload)
    return 0
