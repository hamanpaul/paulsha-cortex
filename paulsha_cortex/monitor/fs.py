from __future__ import annotations

import os
from pathlib import Path


def checked_stat_mode(path: Path) -> tuple[int | None, str | None]:
    """Return followed stat mode while preserving errors as degraded diagnostics."""
    try:
        return path.stat().st_mode, None
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, f"degraded: {type(error).__name__}: {error}"


def checked_lstat_mode(path: Path) -> tuple[int | None, str | None]:
    """Return entry mode without following symlinks."""
    try:
        return path.lstat().st_mode, None
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, f"degraded: {type(error).__name__}: {error}"


def checked_resolve(path: Path) -> tuple[Path, str | None]:
    """Resolve symlinks when possible, with a lexical absolute fallback."""
    try:
        return path.resolve(), None
    except OSError as error:
        fallback = Path(os.path.abspath(os.fspath(path)))
        return fallback, f"degraded: {type(error).__name__}: {error}"


def stable_path(path: Path) -> Path:
    """Return a comparable absolute path without allowing resolve errors to escape."""
    resolved, _error = checked_resolve(path)
    return resolved
