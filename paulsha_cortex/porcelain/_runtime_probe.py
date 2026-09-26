from __future__ import annotations

import importlib.metadata
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping


_SHOW_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ExecStart",
    "Environment",
    "EnvironmentFiles",
    "DropInPaths",
    "FragmentPath",
    "WorkingDirectory",
)


def _installed_version() -> str:
    try:
        return importlib.metadata.version("paulsha-cortex")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0+unknown"


def _systemctl_unit_rows(unit_names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if shutil.which("systemctl") is None:
        return {}
    arguments = ["systemctl", "--user", "show"]
    for property_name in _SHOW_PROPERTIES:
        arguments.extend(("-p", property_name))
    arguments.extend(unit_names)
    try:
        raw = subprocess.run(
            arguments,
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

    def save_current() -> None:
        nonlocal current, current_id
        if current_id is not None:
            rows[current_id] = dict(current)
        current = {}
        current_id = None

    for line in raw.stdout.splitlines():
        if not line.strip():
            save_current()
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if key == "Id":
            save_current()
            current_id = value
        if key in current:
            current["_malformed_show_output"] = True
        current[key] = value
    save_current()
    return rows


def _systemctl_exec_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    matches = re.findall(r"\bpath=([^ ;}]+)", value)
    if len(matches) != 1 or not matches[0].startswith("/"):
        return None
    return matches[0]


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


def _stale_suggested_commands(instance: str, unit_name: str) -> list[str]:
    return [
        f"cortex install service --instance {instance}",
        f"systemctl --user restart {unit_name}",
    ]


def _stale_suggestion(instance: str, unit_name: str) -> str:
    install, restart = _stale_suggested_commands(instance, unit_name)
    return f"stale ExecStart detected; rerun `{install}` or `{restart}` after restoring the venv"


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


def _probe_units_raw(
    instance: str, *, home: Path | None = None
) -> dict[str, dict[str, Any]]:
    """探測每個 unit 目前狀態，含 systemd 有效屬性原始內容（未做安全過濾）。

    只在 ``probe_service_runtime`` 內部呼叫一次；其計算出的
    ``_environment_overlay`` 含真實環境值，任何要把結果交給 JSON 輸出或 CLI
    顯示的呼叫端，都必須在使用前明確 ``pop`` 掉這個鍵（見
    ``probe_service_runtime`` 的說明）。"""
    home = (home or Path(os.environ.get("HOME", str(Path.home())))).expanduser()
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
        live = live_rows.get(unit_name)
        systemd_properties = (
            {name: live[name] for name in _SHOW_PROPERTIES if name in live}
            if live is not None
            else None
        )
        if live is not None and live.get("_malformed_show_output") is True:
            systemd_properties = {**(systemd_properties or {}), "_malformed": True}
        fragment_path = (
            systemd_properties.get("FragmentPath")
            if isinstance(systemd_properties, Mapping)
            else None
        )
        if isinstance(fragment_path, str) and fragment_path.startswith("/"):
            if Path(fragment_path) != unit_path:
                # systemd 回報的有效宣告其實指向另一個檔案位置——例如這個
                # instance 名稱剛好撞到使用者 systemd session 底下另一個真正
                # 在跑的 unit（不是這次要探查的 home 底下管理的那個）。這種宣告
                # 與這個 home 無關，不可信任，視為 systemd 對這個 unit 不可用，
                # 改走檔案 fallback，避免把不相關的真實環境／artifact 誤植進來。
                systemd_properties = None
            else:
                unit_path = Path(fragment_path)
        exec_path = None
        if unit_name.endswith(".service"):
            if systemd_properties is None:
                exec_path = _unit_exec_path(unit_path)
            elif "ExecStart" in systemd_properties:
                exec_path = _systemctl_exec_path(systemd_properties.get("ExecStart"))
            else:
                # Legacy status-only output is sufficient for the stale-path hint,
                # but remains incomplete for the artifact projection.
                exec_path = _unit_exec_path(unit_path)
        stale = _exec_path_stale(exec_path)
        row: dict[str, Any] = {
            "path": str(unit_path),
            "present": unit_path.exists(),
            "status": _unit_status(unit_name, unit_path, live_rows),
            "pid": _unit_pid(unit_name, live_rows),
            "exec_path": exec_path,
            "stale": stale,
            "suggested_commands": _stale_suggested_commands(instance, unit_name) if stale else [],
            "suggestion": _stale_suggestion(instance, unit_name) if stale else None,
        }
        if systemd_properties is not None:
            # Kept only until the safe projection below consumes the effective values.
            row["systemd"] = systemd_properties
        else:
            row["_systemd_unavailable"] = True
        units[unit_name] = row
    return units


def probe_service_runtime(
    instance: str, *, home: Path | None = None
) -> dict[str, Any]:
    """探測 manager／monitor 目前狀態，回傳可安全交給 JSON 輸出或 CLI 顯示的
    投影。

    回傳值額外帶一個 ``_environment_overlay`` 鍵，內容是探測當下（濾除機敏
    屬性前）算出的每個 service 有效環境（含真實值，供 doctor／``cortex
    service status`` 內部重建 root／config 用）。這個鍵刻意保留前導底線標示
    「內部用、不得序列化」——任何要把這個函式的回傳值整包（或用
    ``dict(probe)``）轉成 JSON／CLI 輸出的呼叫端，都必須先明確
    ``pop("_environment_overlay", None)`` 再輸出；只需要安全欄位的呼叫端可以
    直接忽略它。"""
    units = _probe_units_raw(instance, home=home)
    from ..runtime_attestation import service_declaration_projection, service_environment_overlay

    service_declaration = service_declaration_projection(units, instance=instance)
    environment_overlay = service_environment_overlay(units, instance=instance)
    for row in units.values():
        row.pop("systemd", None)
        row.pop("_systemd_unavailable", None)
    mode = "systemd" if any(unit["present"] for unit in units.values()) else "unmanaged"
    return {
        "instance": instance,
        "mode": mode,
        "version": _installed_version(),
        "units": units,
        "service_declaration": service_declaration,
        "_environment_overlay": environment_overlay,
    }
