"""cortex install service——render→copy→daemon-reload→enable，冪等。"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Sequence

from paulsha_cortex.config import paths

_INSTANCE_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_SUPPORTED_EXECUTORS = frozenset({"copilot", "claude", "codex"})


@dataclass(frozen=True)
class InstallServiceResult:
    exit_code: int
    mode: str
    message: str


def _template(name: str) -> str:
    return (resources.files("paulsha_cortex.deploy") / "templates" / name).read_text()


def _service_script_path() -> Path:
    return Path(str(resources.files("paulsha_cortex") / "scripts" / "service-manager.sh"))


def render_units(instance: str, interval: int, repo_root: Path | str | None = None) -> dict[str, str]:
    service = _template("manager.service.tmpl").replace("__INSTANCE__", instance)
    service = service.replace("__SERVICE_SCRIPT__", str(_service_script_path()))
    timer = _template("manager.timer.tmpl").replace("__INSTANCE__", instance)
    timer = re.sub(r"^OnUnitActiveSec=.*$", f"OnUnitActiveSec={interval}", timer, flags=re.M)
    monitor = _template("monitor.service.tmpl").replace("__INSTANCE__", instance)
    monitor = monitor.replace("__PY__", sys.executable)
    service_root = Path(repo_root).resolve() if repo_root is not None else paths.repo_root().resolve()
    service = service.replace("__REPO_ROOT__", str(service_root))
    monitor = monitor.replace("__REPO_ROOT__", str(service_root))
    return {
        f"{instance}-manager.service": service,
        f"{instance}-manager.timer": timer,
        f"{instance}-monitor.service": monitor,
    }


def _systemctl_available() -> bool:
    if shutil.which("systemctl") is None:
        return False
    probe = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True)
    return probe.returncode == 0


def _resolve_git_repo_root(repo_root: Path) -> Path:
    candidate = repo_root.expanduser().resolve()
    probe = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise ValueError(f"{candidate} 不是 git repo")
    return Path(probe.stdout.strip()).resolve()


def _validate_instance(instance: str) -> str:
    if not _INSTANCE_RE.fullmatch(instance):
        raise ValueError(
            f"instance 名稱不合法（僅允許 [a-z0-9-]，不可含路徑分隔）: {instance!r}"
        )
    return instance


def _validate_interval(interval: int) -> int:
    if interval <= 0:
        raise ValueError(f"interval 必須為正整數，實際 {interval!r}")
    return interval


def _write_managed_env(
    env_file: Path,
    managed: dict[str, str],
    *,
    preserve_existing: frozenset[str] = frozenset(),
) -> None:
    """更新 managed keys（PY / PSC_REPO_ROOT），就地保留其餘 operator 手動行與註解。

    每次 install 只覆寫本函式管理的鍵；既有的 PSC_MANAGER_SPECS_DIR、
    PSC_WORKTREE_ROOT、PSC_CONTROL_ROOT 等 operator 設定不得被清掉。
    """
    if env_file.is_symlink():
        raise ValueError("runtime bootstrap env 不可為 symlink")
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    remaining = dict(managed)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in remaining:
            if key in preserve_existing:
                out.append(line)
                remaining.pop(key)
            else:
                out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")


def _read_plain_env(env_file: Path) -> dict[str, str]:
    if env_file.is_symlink():
        raise ValueError("runtime bootstrap env 不可為 symlink")
    if not env_file.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key.strip() != key or key in values:
            raise ValueError(f"runtime bootstrap env 格式錯誤: {env_file}: {raw!r}")
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"runtime bootstrap env quote invalid: {env_file}: {raw!r}")
            value = value[1:-1]
        values[key] = value
    return values


def _resolve_python_executable(value: str | None) -> str | None:
    if not value:
        return None
    candidate = Path(value.strip()).expanduser()
    if not candidate.is_absolute():
        return None
    if candidate.parent.name != "bin":
        return None
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", candidate.name):
        return None
    return str(candidate.resolve())


def _resolve_agents_root(raw_agents_root: str | None) -> Path | None:
    if not raw_agents_root:
        return None
    candidate = Path(raw_agents_root).expanduser()
    if not candidate.is_absolute():
        return None
    return candidate


def _instance_env_file(runtime_dir: Path, instance: str) -> Path:
    return runtime_dir / f"{instance}.env"


def install_service_result(instance: str, interval: int, repo_root: Path) -> InstallServiceResult:
    home = Path.home()
    bootstrap_root = home / ".agents"
    runtime_dir = bootstrap_root / "core" / "runtime"
    env_file = runtime_dir / f"{instance}-manager.env"
    existing = _read_plain_env(env_file)
    instance_env = _read_plain_env(_instance_env_file(runtime_dir, instance))
    existing_python = _resolve_python_executable(existing.get("PY", ""))
    caller_python = _resolve_python_executable(sys.executable)
    if existing_python and caller_python and existing_python != caller_python:
        raise ValueError(
            f"既有 runtime env 中的 PY 指向 {existing_python}，與目前呼叫者 {caller_python} 不一致，"
            "請先手動清理該 instance env 後再執行 install。"
        )
    persisted_executor = existing.get("PSC_MANAGER_EXECUTOR", "").strip()
    if persisted_executor and persisted_executor not in _SUPPORTED_EXECUTORS:
        raise ValueError("既有 PSC_MANAGER_EXECUTOR 必須為 copilot、claude 或 codex")
    instance_executor = instance_env.get("PSC_MANAGER_EXECUTOR", "").strip()
    if instance_executor and instance_executor not in _SUPPORTED_EXECUTORS:
        raise ValueError("既有 instance PSC_MANAGER_EXECUTOR 必須為 copilot、claude 或 codex")
    agents_root = _resolve_agents_root(existing.get("PSC_AGENTS_ROOT"))
    if agents_root is None:
        agents_root = _resolve_agents_root(os.environ.get("PSC_AGENTS_ROOT", ""))
    if agents_root is None:
        agents_root = bootstrap_root
    unit_dir = home / ".config" / "systemd" / "user"
    # Units always bootstrap their EnvironmentFile from %h/.agents. The env
    # file then redirects all mutable/runtime data through PSC_AGENTS_ROOT and
    # the more-specific PSC_* roots.
    for directory in (
        unit_dir,
        runtime_dir,
        agents_root / "specs",
        agents_root / "monitor",
        agents_root / "config" / "paulsha",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for name, content in render_units(instance, interval, repo_root=repo_root).items():
        (unit_dir / name).write_text(content)
    managed_env = {
        "PY": sys.executable,
        "PSC_INSTANCE": instance,
        "PSC_REPO_ROOT": str(repo_root),
        "PSC_AGENTS_ROOT": str(agents_root),
        "PSC_RUN_ROOT": str(agents_root / "run" / instance),
        "PSC_MONITOR_STATE_ROOT": str(agents_root / "monitor"),
        "PSC_PROJECT_CONFIG_ROOT": str(agents_root / "config" / "paulsha"),
    }
    executor_override = os.environ.get("PSC_MANAGER_EXECUTOR", "").strip()
    if executor_override:
        if executor_override not in _SUPPORTED_EXECUTORS:
            raise ValueError("PSC_MANAGER_EXECUTOR 必須為 copilot、claude 或 codex")
        managed_env["PSC_MANAGER_EXECUTOR"] = executor_override
    _write_managed_env(
        env_file,
        managed_env,
        preserve_existing=frozenset(
            {
                "PSC_INSTANCE",
                "PSC_RUN_ROOT",
                "PSC_MONITOR_STATE_ROOT",
                "PSC_PROJECT_CONFIG_ROOT",
            }
        ),
    )
    if not _systemctl_available():
        return InstallServiceResult(
            exit_code=0,
            mode="fallback",
            message=(
                f"systemd 不可用：單元已落檔 {unit_dir}，請改用 service-manager.sh 前景模式；"
                "fallback 缺少 `cortex service logs --follow` 即時串流。"
            ),
        )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", f"{instance}-monitor.service"], check=True)
    subprocess.run(["systemctl", "--user", "enable", f"{instance}-manager.timer"], check=True)
    return InstallServiceResult(
        exit_code=0,
        mode="systemd",
        message=f"installed: {instance}-manager.{{service,timer}} + {instance}-monitor.service",
    )


def install_service(instance: str, interval: int, repo_root: Path) -> int:
    result = install_service_result(instance, interval, repo_root)
    print(result.message)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cortex install",
        description="安裝 paulsha-cortex systemd --user units；只落檔/enable，不會 start service。",
    )
    sub = parser.add_subparsers(dest="target", required=True)
    svc = sub.add_parser("service", help="安裝 manager service/timer 與 monitor service")
    svc.add_argument("--instance", default="cortex", help="systemd unit 前綴（預設：cortex）")
    svc.add_argument(
        "--interval", type=int, default=300,
        help="deprecated manager timer 的 OnUnitActiveSec 秒數；daemon tick 請用 PSC_MANAGER_INTERVAL_SECONDS",
    )
    svc.add_argument(
        "--repo-root", default=str(Path.cwd()),
        help="被治理的目標 git repo（預設：目前目錄）",
    )
    args = parser.parse_args(argv)
    try:
        instance = _validate_instance(args.instance)
        interval = _validate_interval(args.interval)
        repo_root = _resolve_git_repo_root(Path(args.repo_root))
        return install_service(instance, interval, repo_root)
    except ValueError as exc:
        parser.error(str(exc))
    raise AssertionError("argparse.error must exit")
