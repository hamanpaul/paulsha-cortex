"""cortex install service——render→copy→daemon-reload→enable，冪等。"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Sequence


def _template(name: str) -> str:
    return (resources.files("paulsha_cortex.deploy") / "templates" / name).read_text()


def _service_script_path() -> Path:
    return Path(str(resources.files("paulsha_cortex") / "scripts" / "service-manager.sh"))


def render_units(instance: str, interval: int) -> dict[str, str]:
    service = _template("manager.service.tmpl").replace("__INSTANCE__", instance)
    service = service.replace("__SERVICE_SCRIPT__", str(_service_script_path()))
    timer = _template("manager.timer.tmpl").replace("__INSTANCE__", instance)
    timer = re.sub(r"^OnUnitActiveSec=.*$", f"OnUnitActiveSec={interval}", timer, flags=re.M)
    return {f"{instance}-manager.service": service, f"{instance}-manager.timer": timer}


def _systemctl_available() -> bool:
    if shutil.which("systemctl") is None:
        return False
    probe = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True)
    return probe.returncode == 0


def install_service(instance: str, interval: int) -> int:
    home = Path.home()
    unit_dir = home / ".config" / "systemd" / "user"
    runtime_dir = home / ".agents" / "core" / "runtime"
    for directory in (unit_dir, runtime_dir, home / ".agents" / "specs"):
        directory.mkdir(parents=True, exist_ok=True)
    for name, content in render_units(instance, interval).items():
        (unit_dir / name).write_text(content)
    env_file = runtime_dir / f"{instance}-manager.env"
    if not env_file.exists():
        env_file.write_text("")
    if not _systemctl_available():
        print(f"systemd 不可用：單元已落檔 {unit_dir}，請改用 service-manager.sh 前景模式")
        return 0
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", f"{instance}-manager.timer"], check=True)
    print(f"installed: {instance}-manager.{{service,timer}}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cortex install")
    sub = parser.add_subparsers(dest="target", required=True)
    svc = sub.add_parser("service")
    svc.add_argument("--instance", default="paulshaclaw")
    svc.add_argument("--interval", type=int, default=300)
    args = parser.parse_args(argv)
    return install_service(args.instance, args.interval)
