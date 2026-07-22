from __future__ import annotations

import importlib.metadata
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping


def _installed_version() -> str:
    try:
        return importlib.metadata.version("paulsha-cortex")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0+unknown"


def _systemctl_unit_rows(unit_names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if shutil.which("systemctl") is None:
        return {}
    try:
        raw = subprocess.run(
            ["systemctl", "--user", "show", "--property=Id,LoadState,ActiveState,SubState,MainPID", *unit_names],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if raw.returncode != 0:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] = {}
    current_id: str | None = None
    for line in raw.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            if current_id is not None:
                rows[current_id] = dict(current)
            current = {}
            current_id = None
            continue
        key, separator, value = stripped.partition("=")
        if not separator:
            continue
        if key == "Id":
            current_id = value
        current[key] = value
    if current_id is not None:
        rows[current_id] = dict(current)
    return rows


def _unit_exec_path(unit_path: Path) -> str | None:
    try:
        lines = unit_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for raw in lines:
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        value = line.split("=", 1)[1].lstrip("-@:+!")
        try:
            argv = shlex.split(value)
        except ValueError:
            return None
        if not argv:
            return None
        return argv[0]
    return None


def _exec_path_stale(exec_path: str | None) -> bool:
    if not exec_path:
        return False
    candidate = Path(exec_path).expanduser()
    return candidate.is_absolute() and not candidate.exists()


def _unit_status(unit_name: str, unit_path: Path, live_rows: Mapping[str, Mapping[str, Any]]) -> str:
    row = live_rows.get(unit_name)
    if row is not None:
        active = str(row.get("ActiveState") or "unknown")
        sub = str(row.get("SubState") or "unknown")
        return f"{active}/{sub}"
    if unit_path.exists():
        return "configured"
    return "missing"


def _unit_pid(unit_name: str, live_rows: Mapping[str, Mapping[str, Any]]) -> int | None:
    raw = live_rows.get(unit_name, {}).get("MainPID")
    if raw in (None, "", "0"):
        return None
    try:
        pid = int(str(raw))
    except ValueError:
        return None
    return pid if pid > 0 else None


def probe_service_runtime(instance: str) -> dict[str, Any]:
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    unit_root = home / ".config" / "systemd" / "user"
    unit_names = (
        f"{instance}-manager.service",
        f"{instance}-manager.timer",
        f"{instance}-monitor.service",
    )
    live_rows = _systemctl_unit_rows(unit_names)
    units: dict[str, dict[str, Any]] = {}
    for unit_name in unit_names:
        unit_path = unit_root / unit_name
        exec_path = _unit_exec_path(unit_path) if unit_name.endswith(".service") else None
        units[unit_name] = {
            "path": str(unit_path),
            "present": unit_path.exists(),
            "status": _unit_status(unit_name, unit_path, live_rows),
            "pid": _unit_pid(unit_name, live_rows),
            "exec_path": exec_path,
            "stale": _exec_path_stale(exec_path),
        }
    mode = "systemd" if any(unit["present"] for unit in units.values()) else "unmanaged"
    return {
        "instance": instance,
        "mode": mode,
        "version": _installed_version(),
        "units": units,
    }
