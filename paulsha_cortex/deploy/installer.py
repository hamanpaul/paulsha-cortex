"""cortex install service——render→copy→daemon-reload→enable，冪等。"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Sequence

_INSTANCE_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def _template(name: str) -> str:
    return (resources.files("paulsha_cortex.deploy") / "templates" / name).read_text()


def _service_script_path() -> Path:
    return Path(str(resources.files("paulsha_cortex") / "scripts" / "service-manager.sh"))


def render_units(instance: str, interval: int) -> dict[str, str]:
    service = _template("manager.service.tmpl").replace("__INSTANCE__", instance)
    service = service.replace("__SERVICE_SCRIPT__", str(_service_script_path()))
    timer = _template("manager.timer.tmpl").replace("__INSTANCE__", instance)
    timer = re.sub(r"^OnUnitActiveSec=.*$", f"OnUnitActiveSec={interval}", timer, flags=re.M)
    monitor = _template("monitor.service.tmpl").replace("__INSTANCE__", instance)
    monitor = monitor.replace("__PY__", sys.executable)
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


def _write_managed_env(env_file: Path, managed: dict[str, str]) -> None:
    """更新 managed keys（PY / PSC_REPO_ROOT），就地保留其餘 operator 手動行與註解。

    每次 install 只覆寫本函式管理的鍵；既有的 PSC_MANAGER_SPECS_DIR、
    PSC_WORKTREE_ROOT、PSC_CONTROL_ROOT 等 operator 設定不得被清掉。
    """
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    remaining = dict(managed)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")


def install_service(instance: str, interval: int, repo_root: Path) -> int:
    home = Path.home()
    agents_root = home / ".agents"
    unit_dir = home / ".config" / "systemd" / "user"
    runtime_dir = agents_root / "core" / "runtime"
    for directory in (
        unit_dir,
        runtime_dir,
        agents_root / "specs",
        agents_root / "monitor",
        agents_root / "config" / "paulsha",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for name, content in render_units(instance, interval).items():
        (unit_dir / name).write_text(content)
    env_file = runtime_dir / f"{instance}-manager.env"
    _write_managed_env(
        env_file,
        {
            "PY": sys.executable,
            "PSC_REPO_ROOT": str(repo_root),
            "PSC_RUN_ROOT": str(agents_root / "run" / instance),
            "PSC_MONITOR_STATE_ROOT": str(agents_root / "monitor"),
            "PSC_PROJECT_CONFIG_ROOT": str(agents_root / "config" / "paulsha"),
        },
    )
    if not _systemctl_available():
        print(f"systemd 不可用：單元已落檔 {unit_dir}，請改用 service-manager.sh 前景模式")
        return 0
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", f"{instance}-monitor.service"], check=True)
    subprocess.run(["systemctl", "--user", "enable", f"{instance}-manager.timer"], check=True)
    print(f"installed: {instance}-manager.{{service,timer}} + {instance}-monitor.service")
    return 0


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
    except ValueError as exc:
        parser.error(str(exc))
    return install_service(instance, interval, repo_root)
