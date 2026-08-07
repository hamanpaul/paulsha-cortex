"""cortex 路徑契約——鏡射主 repo 治理平面所需的 paths 子集。"""
from __future__ import annotations

import os
from pathlib import Path

from .runtime import resolve_project_config_root, resolve_run_root, resolve_runtime_root


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    return Path(value).expanduser()


def _resolve_root(name: str, default: Path) -> Path:
    return _env_path(name) or default


def agents_root() -> Path:
    return resolve_runtime_root("PSC_AGENTS_ROOT")


def control_root() -> Path:
    return resolve_runtime_root("PSC_CONTROL_ROOT")


def coordinator_root() -> Path:
    return resolve_runtime_root("PSC_COORDINATOR_ROOT")


def specs_root() -> Path:
    return resolve_runtime_root("PSC_SPECS_ROOT")


def run_root() -> Path:
    return resolve_run_root()


def monitor_state_root() -> Path:
    """Durable Monitor state; distinct from the runtime socket directory."""
    return resolve_runtime_root("PSC_MONITOR_STATE_ROOT")


def work_items_snapshot_path() -> Path:
    return monitor_state_root() / "work-items.snapshot.json"


def skill_registry_root() -> Path:
    """Skill governance 治理平面根目錄（issue #204）：ledger／park state／proposal 共用。

    未宣告獨立 `PSC_*` override——沿用 `agents_root()`（已支援 `PSC_AGENTS_ROOT`
    覆寫）底下的 `registry` 子目錄，理由：這是 `~/.agents` 樹的 mutable runtime
    狀態，跟 coordinator/control/specs/monitor 屬同一族，沒必要另開一個環境變數
    造成 path 契約碎片化。
    """
    return agents_root() / "registry"


def skill_usage_ledger_path() -> Path:
    """Append-only skill usage event ledger（`schema_version`/`event_id`/... 見
    `paulsha_cortex.coordinator.skill_ledger`）。"""
    return skill_registry_root() / "skill_usage.jsonl"


def skill_park_state_path() -> Path:
    """目前已 park 的 skill 清單（可逆狀態，不含歷史紀錄／ledger 本身）。"""
    return skill_registry_root() / "skill_park.json"


def skill_park_proposals_root() -> Path:
    """Janitor 產生、尚待 operator 核准/已核准的 park proposal 檔案目錄。"""
    return skill_registry_root() / "skill_park_proposals"


def config_root() -> Path:
    return _resolve_root("PSC_CONFIG_ROOT", Path.home() / ".config" / "paulshaclaw")


def config_path(*parts: str) -> Path:
    return config_root().joinpath(*parts)


def project_config_root() -> Path:
    return resolve_project_config_root()


def repo_root() -> Path:
    return _resolve_root("PSC_REPO_ROOT", Path.cwd())


def _canonical_repo_root(repo: Path) -> Path:
    if repo.parent.name == ".worktrees":
        return repo.parent.parent
    return repo


def worktree_root_for(repo: Path) -> Path:
    """依給定 repo 計算 worktree pool，預設為 sibling `<repo>-worktrees`。"""
    override = _env_path("PSC_WORKTREE_ROOT")
    if override is not None:
        return override
    repo = _canonical_repo_root(repo)
    return repo.parent / f"{repo.name}-worktrees"


def worktree_root() -> Path:
    """coordinator 派工 worktree pool 的預設路徑。"""
    return worktree_root_for(repo_root())
