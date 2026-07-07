from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from paulsha_cortex.config import paths

ENV_CONFIG_VAR = "PAULSHACLAW_CONFIG"
ALLOWED_LEGACY_POLICIES = ("list-only", "hide")


def default_config_path() -> Path:
    return paths.config_path("paulshaclaw.yaml")


def default_socket_path() -> Path:
    return paths.run_root() / "project-monitor.sock"


@dataclass(frozen=True)
class WorkspaceConfig:
    path: Path
    name: str


@dataclass(frozen=True)
class MonitorConfig:
    workspaces: tuple[WorkspaceConfig, ...]
    poll_interval_seconds: int = 60
    rescan_interval_seconds: int = 300
    watch_debounce_ms: int = 500
    legacy_policy: str = "list-only"
    socket_path: Path = field(default_factory=default_socket_path)
    ignore_dirs: tuple[str, ...] = ()


def _resolve_config_source(config_path: Path | None) -> Path:
    if config_path is not None:
        return Path(config_path)
    env_value = os.environ.get(ENV_CONFIG_VAR)
    if env_value:
        return Path(env_value)
    default = default_config_path()
    if default.exists():
        return default
    sample = (
        Path(__file__).resolve().parents[2]
        / "paulshaclaw"
        / "config"
        / "paulshaclaw.sample.yaml"
    )
    if sample.exists():
        return sample
    raise FileNotFoundError(
        f"找不到設定檔：請設置 --config、{ENV_CONFIG_VAR} 或 {default_config_path()}"
    )


def _parse_workspaces(raw: Any) -> tuple[WorkspaceConfig, ...]:
    if not isinstance(raw, list):
        raise ValueError("config.workspaces 必須是清單")
    if len(raw) == 0:
        raise ValueError("config.workspaces 不可為空清單")
    items: list[WorkspaceConfig] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"config.workspaces[{index}] 必須是 mapping")
        path_value = entry.get("path")
        name_value = entry.get("name")
        if not path_value:
            raise ValueError(f"config.workspaces[{index}].path 缺失")
        if not name_value:
            raise ValueError(f"config.workspaces[{index}].name 缺失")
        items.append(
            WorkspaceConfig(
                path=Path(str(path_value)).expanduser(),
                name=str(name_value),
            )
        )
    return tuple(items)


def _parse_monitor_section(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("config.monitor 必須是 mapping")
    return raw


def load_config(*, config_path: Path | None = None) -> MonitorConfig:
    """Load the global paulshaclaw config.

    Resolution order: explicit `config_path` → `PAULSHACLAW_CONFIG` env →
    `~/.config/paulshaclaw/paulshaclaw.yaml` → bundled sample (read-only).
    """
    resolved = _resolve_config_source(config_path)
    if not resolved.exists():
        raise FileNotFoundError(f"設定檔不存在：{resolved}")

    try:
        payload = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ValueError(f"設定檔解析失敗：{resolved} ({error})") from error

    if not isinstance(payload, dict):
        raise ValueError(f"設定檔必須是 mapping：{resolved}")

    workspaces = _parse_workspaces(payload.get("workspaces"))
    monitor = _parse_monitor_section(payload.get("monitor"))

    legacy_policy = str(monitor.get("legacy_policy", "list-only"))
    if legacy_policy not in ALLOWED_LEGACY_POLICIES:
        raise ValueError(
            f"config.monitor.legacy_policy 必須是 {ALLOWED_LEGACY_POLICIES} 之一，得到 {legacy_policy!r}"
        )

    poll_interval = int(monitor.get("poll_interval_seconds", 60))
    rescan_interval = int(monitor.get("rescan_interval_seconds", 300))
    debounce = int(monitor.get("watch_debounce_ms", 500))

    socket_raw = monitor.get("socket_path")
    socket_path = (
        Path(str(socket_raw)).expanduser()
        if socket_raw
        else default_socket_path()
    )

    ignore_raw = monitor.get("ignore_dirs") or ()
    if not isinstance(ignore_raw, (list, tuple)):
        raise ValueError("config.monitor.ignore_dirs 必須是清單")
    ignore_dirs = tuple(str(item) for item in ignore_raw)

    return MonitorConfig(
        workspaces=workspaces,
        poll_interval_seconds=poll_interval,
        rescan_interval_seconds=rescan_interval,
        watch_debounce_ms=debounce,
        legacy_policy=legacy_policy,
        socket_path=socket_path,
        ignore_dirs=ignore_dirs,
    )
