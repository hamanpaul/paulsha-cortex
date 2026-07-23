"""Installed runtime discovery shared by interactive commands and services."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping


INSTANCE_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RUNTIME_ROOT_DEFAULTS = {
    "PSC_CONTROL_ROOT": ("control",),
    "PSC_COORDINATOR_ROOT": ("coordinator",),
    "PSC_SPECS_ROOT": ("specs",),
    "PSC_MONITOR_STATE_ROOT": ("monitor",),
    "PSC_PROJECT_CONFIG_ROOT": ("config", "paulsha"),
}


def selected_instance(environment: Mapping[str, str] | None = None) -> str:
    env = os.environ if environment is None else environment
    instance = env.get("PSC_INSTANCE", "cortex").strip()
    if INSTANCE_RE.fullmatch(instance) is None:
        raise ValueError("PSC_INSTANCE 名稱不合法")
    return instance


def _parse_bootstrap_env(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("runtime bootstrap env 必須是 regular non-symlink file")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (
            not separator
            or ENV_KEY_RE.fullmatch(key) is None
            or not value
            or value != value.strip()
        ):
            raise ValueError(f"runtime bootstrap env 格式錯誤: {path}: {raw_line!r}")
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"runtime bootstrap env quote invalid: {path}: {raw_line!r}")
            value = value[1:-1]
        values[key] = value
    return values


def _installed_environment(
    environment: Mapping[str, str],
    *,
    home: Path | None,
) -> tuple[Path, dict[str, str]]:
    selected_home = (Path.home() if home is None else Path(home)).expanduser()
    if not selected_home.is_absolute():
        raise ValueError("runtime home 必須為絕對路徑")
    bootstrap = (
        selected_home
        / ".agents"
        / "core"
        / "runtime"
        / f"{selected_instance(environment)}-manager.env"
    )
    if bootstrap.exists() or bootstrap.is_symlink():
        return selected_home, _parse_bootstrap_env(bootstrap)
    return selected_home, {}


def resolve_run_root(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve the interactive run root from the selected installed instance."""
    env = os.environ if environment is None else environment
    instance = selected_instance(env)
    return resolve_runtime_root(
        "PSC_RUN_ROOT",
        default_parts=("run", instance),
        environment=env,
        home=home,
    )


def resolve_runtime_root(
    name: str,
    *,
    default_parts: tuple[str, ...] | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve one root with process override, then selected installed instance authority."""
    env = os.environ if environment is None else environment
    explicit = env.get(name, "").strip()
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_absolute():
            raise ValueError(f"{name} 必須為絕對路徑")
        return root

    process_agents_value = env.get("PSC_AGENTS_ROOT", "").strip()
    if process_agents_value:
        process_agents = Path(process_agents_value).expanduser()
        if not process_agents.is_absolute():
            raise ValueError("PSC_AGENTS_ROOT 必須為絕對路徑")
        if name == "PSC_AGENTS_ROOT":
            return process_agents
        relative = default_parts if default_parts is not None else RUNTIME_ROOT_DEFAULTS.get(name)
        if relative is None:
            raise ValueError(f"unsupported runtime root: {name}")
        return process_agents.joinpath(*relative)

    selected_home, installed = _installed_environment(env, home=home)
    installed_value = installed.get(name, "").strip()
    if installed_value:
        root = Path(installed_value).expanduser()
        if not root.is_absolute():
            raise ValueError(f"installed {name} 必須為絕對路徑")
        return root

    agents_value = installed.get("PSC_AGENTS_ROOT", "").strip()
    agents = Path(agents_value).expanduser() if agents_value else selected_home / ".agents"
    if not agents.is_absolute():
        raise ValueError("PSC_AGENTS_ROOT 必須為絕對路徑")
    if name == "PSC_AGENTS_ROOT":
        return agents
    relative = default_parts if default_parts is not None else RUNTIME_ROOT_DEFAULTS.get(name)
    if relative is None:
        raise ValueError(f"unsupported runtime root: {name}")
    return agents.joinpath(*relative)


def resolve_project_config_root(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve project config from the same selected installed instance as run root."""
    return resolve_runtime_root(
        "PSC_PROJECT_CONFIG_ROOT",
        environment=environment,
        home=home,
    )
