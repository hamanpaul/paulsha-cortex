from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from paulsha_cortex.config import paths
from paulsha_cortex.monitor.registry import ProjectEntry, load_hippo_projects

ENV_CONFIG_VAR = "PAULSHACLAW_CONFIG"
NEW_ENV_CONFIG_VAR = "PSC_MONITOR_CONFIG"
ALLOWED_LEGACY_POLICIES = ("list-only", "hide")


def default_config_path() -> Path:
    # 回傳現行預設 manual 路徑（project-cortex.yaml）——與 _resolve_config_source 的
    # 優先序一致；勿反向導回 legacy paulshaclaw.yaml（GitHub review #3）。
    return _new_manual_path()


def _new_manual_path() -> Path:
    return paths.project_config_root() / "project-cortex.yaml"


def _legacy_manual_path() -> Path:
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
    github_refresh_interval_seconds: int = 300
    provider_stale_after_seconds: int = 900
    legacy_policy: str = "list-only"
    socket_path: Path = field(default_factory=default_socket_path)
    ignore_dirs: tuple[str, ...] = ()
    hippo_projects: tuple[ProjectEntry, ...] = ()


def _resolve_config_source(config_path: Path | None) -> Path | None:
    if config_path is not None:
        return Path(config_path)
    for env in (NEW_ENV_CONFIG_VAR, ENV_CONFIG_VAR):
        raw = os.environ.get(env, "").strip()
        if not raw:
            continue
        if env == ENV_CONFIG_VAR:
            warnings.warn(
                "PAULSHACLAW_CONFIG 已 deprecated，改用 project-cortex.yaml",
                stacklevel=2,
            )
        return Path(raw).expanduser()
    new = _new_manual_path()
    if new.exists():
        return new
    legacy = _legacy_manual_path()
    if legacy.exists():
        warnings.warn(
            f"讀取 deprecated legacy monitor 設定 {legacy}，請遷移至 {new}",
            stacklevel=2,
        )
        return legacy
    return None


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


def _load_manual_config(resolved: Path) -> MonitorConfig:
    if not resolved.exists():
        raise FileNotFoundError(f"設定檔不存在：{resolved}")

    try:
        payload = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as error:
        raise ValueError(f"設定檔讀取或解析失敗：{resolved} ({error})") from error

    if not isinstance(payload, dict):
        raise ValueError(f"設定檔必須是 mapping：{resolved}")

    workspaces = _parse_workspaces(payload.get("workspaces"))
    monitor = _parse_monitor_section(payload.get("monitor"))

    legacy_policy = str(monitor.get("legacy_policy", "list-only"))
    if legacy_policy not in ALLOWED_LEGACY_POLICIES:
        raise ValueError(
            f"config.monitor.legacy_policy 必須是 {ALLOWED_LEGACY_POLICIES} 之一，得到 {legacy_policy!r}"
        )

    intervals: dict[str, int] = {}
    for field_name, default in (
        ("poll_interval_seconds", 60),
        ("rescan_interval_seconds", 300),
        ("watch_debounce_ms", 500),
        ("github_refresh_interval_seconds", 300),
        ("provider_stale_after_seconds", 900),
    ):
        value = monitor.get(field_name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"config.monitor.{field_name} 必須是正整數，得到 {value!r}")
        intervals[field_name] = value
    poll_interval = intervals["poll_interval_seconds"]
    rescan_interval = intervals["rescan_interval_seconds"]
    debounce = intervals["watch_debounce_ms"]

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
        github_refresh_interval_seconds=intervals["github_refresh_interval_seconds"],
        provider_stale_after_seconds=intervals["provider_stale_after_seconds"],
        legacy_policy=legacy_policy,
        socket_path=socket_path,
        ignore_dirs=ignore_dirs,
    )


def load_config(*, config_path: Path | None = None) -> MonitorConfig:
    """Load the global paulshaclaw config.

    Resolution order: explicit `config_path` → `PSC_MONITOR_CONFIG` env →
    `PAULSHACLAW_CONFIG` env → `project-cortex.yaml` → legacy `paulshaclaw.yaml`.
    """
    resolved = _resolve_config_source(config_path)
    hippo = tuple(load_hippo_projects())
    if resolved is None:
        if not hippo:
            raise FileNotFoundError(
                "無 project 設定：manual（project-cortex.yaml / legacy）與 "
                "project-hippo.yaml 皆不存在"
            )
        return MonitorConfig(workspaces=(), hippo_projects=hippo)
    return replace(_load_manual_config(resolved), hippo_projects=hippo)
