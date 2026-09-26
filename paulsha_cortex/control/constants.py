from __future__ import annotations

import os
from pathlib import Path

from paulsha_cortex.config import paths

SCHEMA_VERSION = 1
# Age past which a status file is treated as stale when the daemon's liveness
# cannot be confirmed (no live pid). A live-but-busy daemon is NOT degraded.
STATUS_STALE_AFTER_SECONDS = float(os.environ.get("PSC_CONTROL_STATUS_STALE_AFTER_SECONDS", "15"))
# Age past which even a live daemon pid is treated as stalled/degraded (hung
# far longer than any plausible request). Must exceed a long tick's duration.
STATUS_STALLED_AFTER_SECONDS = float(os.environ.get("PSC_CONTROL_STATUS_STALLED_AFTER_SECONDS", "120"))
# Manager 的活動檔最後寫入超過 30 分鐘，仍視為 stalled。
STATUS_ACTIVITY_STALE_AFTER_SECONDS = 1800.0


def control_root() -> Path:
    return paths.control_root()


def requests_dir() -> Path:
    return control_root() / "requests"


def done_dir() -> Path:
    return control_root() / "done"


def status_path() -> Path:
    return control_root() / "status.json"


def activity_path() -> Path:
    return control_root() / "activity.json"


def lock_path() -> Path:
    return control_root() / "manager.lock"
