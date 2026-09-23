from __future__ import annotations

from pathlib import Path

_ACTIVE_CHANGE_REF = Path("openspec/changes/recovery-registry-receipt")
_ARCHIVE_ROOT_REF = Path("openspec/changes/archive")
_CHANGE_SLUG = "recovery-registry-receipt"


def recovery_registry_receipt_openspec_refs(repo_root: Path) -> dict[str, str]:
    change_ref = _recovery_registry_receipt_change_dir(repo_root).relative_to(repo_root).as_posix()
    return {
        "proposal": f"{change_ref}/proposal.md",
        "design": f"{change_ref}/design.md",
        "tasks": f"{change_ref}/tasks.md",
    }


def _recovery_registry_receipt_change_dir(repo_root: Path) -> Path:
    active = repo_root / _ACTIVE_CHANGE_REF
    if active.is_dir():
        return active

    archive_root = repo_root / _ARCHIVE_ROOT_REF
    matches = sorted(
        candidate
        for candidate in archive_root.glob(f"*-{_CHANGE_SLUG}")
        if candidate.is_dir() and not candidate.is_symlink()
    )
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(
        "expected active openspec/changes/recovery-registry-receipt or exactly one archived "
        f"match under {_ARCHIVE_ROOT_REF.as_posix()}; found {len(matches)}"
    )
