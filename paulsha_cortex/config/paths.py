"""cortex 路徑契約——鏡射主 repo 治理平面所需的 paths 子集。"""
from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    return Path(value).expanduser()


def _resolve_root(name: str, default: Path) -> Path:
    return _env_path(name) or default


def agents_root() -> Path:
    return _resolve_root("PSC_AGENTS_ROOT", Path.home() / ".agents")


def control_root() -> Path:
    return _resolve_root("PSC_CONTROL_ROOT", agents_root() / "control")


def coordinator_root() -> Path:
    return _resolve_root("PSC_COORDINATOR_ROOT", agents_root() / "coordinator")


def specs_root() -> Path:
    return _resolve_root("PSC_SPECS_ROOT", agents_root() / "specs")


def run_root() -> Path:
    return _resolve_root("PSC_RUN_ROOT", agents_root() / "run")


def monitor_state_root() -> Path:
    """Durable Monitor state; distinct from the runtime socket directory."""
    return _resolve_root("PSC_MONITOR_STATE_ROOT", agents_root() / "monitor")


def work_items_snapshot_path() -> Path:
    return monitor_state_root() / "work-items.snapshot.json"


def config_root() -> Path:
    return _resolve_root("PSC_CONFIG_ROOT", Path.home() / ".config" / "paulshaclaw")


def config_path(*parts: str) -> Path:
    return config_root().joinpath(*parts)


def project_config_root() -> Path:
    return _resolve_root("PSC_PROJECT_CONFIG_ROOT", agents_root() / "config" / "paulsha")


def repo_root() -> Path:
    return _resolve_root("PSC_REPO_ROOT", Path.cwd())


def _canonical_repo_root(repo: Path) -> Path:
    if repo.parent.name == ".worktrees":
        return repo.parent.parent
    return repo


def worktree_root() -> Path:
    """coordinator 派工 worktree 池預設為 sibling `<repo>-worktrees`。"""
    override = _env_path("PSC_WORKTREE_ROOT")
    if override is not None:
        return override
    repo = _canonical_repo_root(repo_root())
    return repo.parent / f"{repo.name}-worktrees"
