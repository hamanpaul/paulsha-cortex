from __future__ import annotations

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
