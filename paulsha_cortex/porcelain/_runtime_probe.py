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

#: `systemctl show` 對這些屬性的每個值各輸出一行 `Key=value`（不是重複宣告）。
_MULTI_VALUED_SHOW_PROPERTIES = frozenset({"EnvironmentFiles"})


def _installed_version() -> str:
    try:
        return importlib.metadata.version("paulsha-cortex")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0+unknown"


def _systemctl_unit_rows(
    unit_names: tuple[str, ...], *, scope: str = "user"
) -> dict[str, dict[str, Any]]:
    if shutil.which("systemctl") is None:
        return {}
    if scope not in {"user", "system"}:
        raise ValueError("systemd scope must be user or system")
    arguments = ["systemctl", *(["--user"] if scope == "user" else []), "show"]
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
    # `systemctl show` 以空行分隔各 unit 的區塊，區塊內屬性依 systemd 內部順序
    # 輸出——`Id=` 通常在中間，不是第一行（live 驗收發現：舊 parser 以 `Id=`
    # 當區塊起點，丟掉排在它前面的 ExecStart／Environment／EnvironmentFiles／
    # WorkingDirectory，真實 service 因此永遠被判成 unknown）。因此先切區塊、
    # 再在區塊內找 Id。多值屬性（每個值各佔一行，例如每個 EnvironmentFile 一行
    # `EnvironmentFiles=`）以空白串接成單一字串，交給既有逐項解析；其他鍵重複
    # 仍視為輸出格式異常。
    rows: dict[str, dict[str, Any]] = {}
    block: dict[str, Any] = {}

    def save_block() -> None:
        nonlocal block
        unit_id = block.get("Id")
        if isinstance(unit_id, str) and unit_id:
            rows[unit_id] = dict(block)
        block = {}

    for line in raw.stdout.splitlines():
        if not line.strip():
            save_block()
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if key in block:
            if key in _MULTI_VALUED_SHOW_PROPERTIES:
                previous = block[key]
                block[key] = f"{previous} {value}".strip() if previous else value
                continue
            block["_malformed_show_output"] = True
        block[key] = value
    save_block()
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
    instance: str, *, home: Path | None = None, scope: str = "user"
) -> dict[str, dict[str, Any]]:
    """探測每個 unit 目前狀態，含 systemd 有效屬性原始內容（未做安全過濾）。

    只在 ``probe_service_runtime`` 內部呼叫一次；其計算出的
    ``_environment_overlay`` 含真實環境值，任何要把結果交給 JSON 輸出或 CLI
    顯示的呼叫端，都必須在使用前明確 ``pop`` 掉這個鍵（見
    ``probe_service_runtime`` 的說明）。"""
    home = (home or Path(os.environ.get("HOME", str(Path.home())))).expanduser()
    if scope not in {"user", "system"}:
        raise ValueError("systemd scope must be user or system")
    unit_root = (
        home / ".config" / "systemd" / "user"
        if scope == "user"
        else Path("/etc/systemd/system")
    )
    unit_names = (
        f"{instance}-manager.service",
        *((f"{instance}-manager.timer",) if scope == "user" else ()),
        f"{instance}-monitor.service",
    )
    live_rows = _systemctl_unit_rows(unit_names, scope=scope)
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
        if (
            isinstance(systemd_properties, Mapping)
            and systemd_properties.get("LoadState") == "not-found"
        ):
            # systemd 根本沒載入這個 unit（例如 unit 檔已寫入但尚未 daemon-reload，
            # 或 user manager 裡沒有這個 instance）：`show` 仍回 0，但只給殘缺的
            # 屬性集合（無 ExecStart／EnvironmentFiles、FragmentPath 為空），
            # 無法替這個 home 底下的宣告背書。視為 systemd 對這個 unit 不可用、
            # 走檔案 fallback；不能把殘缺屬性當成「宣告存在但無法解析」的
            # unknown。bad-setting／error 等宣告本身有問題的狀態不在此列，
            # 維持 fail closed。
            systemd_properties = None
        fragment_path = (
            systemd_properties.get("FragmentPath")
            if isinstance(systemd_properties, Mapping)
            else None
        )
        if isinstance(fragment_path, str) and fragment_path.startswith("/"):
            if scope == "user" and Path(fragment_path) != unit_path:
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
    instance: str, *, home: Path | None = None, scope: str = "user"
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
    units = _probe_units_raw(instance, home=home, scope=scope)
    from ..runtime_attestation import service_declaration_projection, service_environment_overlay

    service_declaration = service_declaration_projection(units, instance=instance)
    environment_overlay = service_environment_overlay(units, instance=instance)
    for row in units.values():
        row.pop("systemd", None)
        row.pop("_systemd_unavailable", None)
    has_units = any(unit["present"] for unit in units.values())
    mode = ("systemd" if scope == "user" else "systemd-system") if has_units else "unmanaged"
    return {
        "instance": instance,
        "mode": mode,
        "version": _installed_version(),
        "units": units,
        "service_declaration": service_declaration,
        "_environment_overlay": environment_overlay,
    }
