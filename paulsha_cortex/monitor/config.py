from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from paulsha_cortex.config import paths
from paulsha_cortex.monitor.registry import ProjectEntry, load_hippo_projects, merge_projects

ENV_CONFIG_VAR = "PAULSHACLAW_CONFIG"
NEW_ENV_CONFIG_VAR = "PSC_MONITOR_CONFIG"
ALLOWED_LEGACY_POLICIES = ("list-only", "hide")
_WARNED_DEPRECATIONS: set[str] = set()


def _warn_deprecated_once(key: str, _message: str) -> bool:
    if key in _WARNED_DEPRECATIONS:
        return False
    return True


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
    # #506：GitHub 請求節流／退避參數（預算計算與取捨見
    # monitor/github_pressure.py 的模組 docstring）。interval 設 0 即停用節流。
    github_request_interval_ms: int = 200
    github_request_jitter_ms: int = 100
    github_throttle_budget_seconds: int = 120
    github_backoff_base_seconds: int = 60
    github_backoff_max_seconds: int = 1800

    def github_throttle_budget(self) -> float:
        """本輪節流可花掉的總睡眠秒數上限。

        #506：節流的用途是攤平 burst，不是把掃描拖到追不上自己的週期。因此
        設定值再大也夾在 ``github_refresh_interval_seconds`` 的一半以下——
        刻意用夾擠而非 fail-loud：既有部署若把 refresh interval 調小，不該讓
        monitor 因為一個預設值就啟動失敗。
        """

        return float(
            min(
                self.github_throttle_budget_seconds,
                self.github_refresh_interval_seconds / 2,
            )
        )


def _resolve_config_source(config_path: Path | None) -> Path | None:
    if config_path is not None:
        return Path(config_path)
    raw_new_env = os.environ.get(NEW_ENV_CONFIG_VAR, "").strip()
    if raw_new_env:
        return Path(raw_new_env).expanduser()

    raw_legacy_env = os.environ.get(ENV_CONFIG_VAR, "").strip()
    if raw_legacy_env:
        raise ValueError("PAULSHACLAW_CONFIG 已 deprecated，改用 project-cortex.yaml")

    new = _new_manual_path()
    if new.exists():
        return new

    legacy = _legacy_manual_path()
    if legacy.exists():
        raise ValueError(
            f"讀取 deprecated legacy monitor 設定 {legacy}，請遷移至 {new}"
        )

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
        # #506：節流預算與退避參數。
        ("github_throttle_budget_seconds", 120),
        ("github_backoff_base_seconds", 60),
        ("github_backoff_max_seconds", 1800),
    ):
        value = monitor.get(field_name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"config.monitor.{field_name} 必須是正整數，得到 {value!r}")
        intervals[field_name] = value
    # #506：節流間隔／jitter 允許 0（代表關閉節流），故與上面的正整數分開驗。
    for field_name, default in (
        ("github_request_interval_ms", 200),
        ("github_request_jitter_ms", 100),
    ):
        value = monitor.get(field_name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"config.monitor.{field_name} 必須是非負整數，得到 {value!r}"
            )
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
        github_request_interval_ms=intervals["github_request_interval_ms"],
        github_request_jitter_ms=intervals["github_request_jitter_ms"],
        github_throttle_budget_seconds=intervals["github_throttle_budget_seconds"],
        github_backoff_base_seconds=intervals["github_backoff_base_seconds"],
        github_backoff_max_seconds=intervals["github_backoff_max_seconds"],
    )


def _get_active_repo_projects() -> list[ProjectEntry]:
    raw = os.environ.get("PSC_REPO_ROOT", "").strip()
    if not raw:
        return []
    repo_path = Path(raw).expanduser()
    if not repo_path.exists():
        return []
    from paulsha_cortex.monitor.fs import stable_path
    return [ProjectEntry(path=stable_path(repo_path), name=repo_path.name, source="repo_root")]


def load_config(*, config_path: Path | None = None) -> MonitorConfig:
    """Load the global paulshaclaw config.

    Resolution order: explicit `config_path` → `PSC_MONITOR_CONFIG` env →
    `project-cortex.yaml`.
    """
    resolved = _resolve_config_source(config_path)
    active_repo_projects = _get_active_repo_projects() if config_path is None else []

    if resolved is None:
        hippo = merge_projects(load_hippo_projects(), active_repo_projects)
        if not hippo:
            raise FileNotFoundError(
                "無 project 設定：manual（project-cortex.yaml / legacy）與 "
                "project-hippo.yaml 皆不存在"
            )
        return MonitorConfig(workspaces=(), hippo_projects=tuple(hippo))
    if config_path is not None:
        return replace(_load_manual_config(resolved), hippo_projects=())
    
    hippo_list = merge_projects(
        load_hippo_projects(resolved.parent / "project-hippo.yaml"),
        active_repo_projects,
    )
    return replace(
        _load_manual_config(resolved),
        hippo_projects=tuple(hippo_list),
    )

