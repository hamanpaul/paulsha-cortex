"""Installed runtime discovery shared by interactive commands and services."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping


INSTANCE_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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
            or value.startswith(("'", '"'))
        ):
            raise ValueError("runtime bootstrap env 格式錯誤")
        values[key] = value
    return values


def resolve_run_root(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve the interactive run root from the selected installed instance."""
    env = os.environ if environment is None else environment
    explicit = env.get("PSC_RUN_ROOT", "").strip()
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_absolute():
            raise ValueError("PSC_RUN_ROOT 必須為絕對路徑")
        return root

    instance = selected_instance(env)
    selected_home = (Path.home() if home is None else Path(home)).expanduser()
    if not selected_home.is_absolute():
        raise ValueError("runtime home 必須為絕對路徑")
    bootstrap = selected_home / ".agents" / "core" / "runtime" / f"{instance}-manager.env"
    installed: dict[str, str] = {}
    if bootstrap.exists() or bootstrap.is_symlink():
        installed = _parse_bootstrap_env(bootstrap)

    installed_run_root = installed.get("PSC_RUN_ROOT", "").strip()
    if installed_run_root:
        root = Path(installed_run_root).expanduser()
        if not root.is_absolute():
            raise ValueError("installed PSC_RUN_ROOT 必須為絕對路徑")
        return root

    agents_value = installed.get("PSC_AGENTS_ROOT", env.get("PSC_AGENTS_ROOT", "")).strip()
    agents = Path(agents_value).expanduser() if agents_value else selected_home / ".agents"
    if not agents.is_absolute():
        raise ValueError("PSC_AGENTS_ROOT 必須為絕對路徑")
    return agents / "run" / instance
