"""#1225：Monitor 不得覆寫 installer 管理的 run dir 權限（ACL mask）。"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.monitor.service import ProjectMonitorService


def _prepare(run_dir: Path) -> None:
    fake = SimpleNamespace(_config=SimpleNamespace(socket_path=run_dir / "monitor.sock"))
    ProjectMonitorService._prepare_run_dir(fake)  # type: ignore[arg-type]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_missing_run_dir_is_created_private(tmp_path: Path) -> None:
    run_dir = tmp_path / "run" / "cortex"
    _prepare(run_dir)
    assert _mode(run_dir) == 0o700


def test_existing_installer_managed_run_dir_keeps_its_acl_mask(tmp_path: Path) -> None:
    # 帶 named-user ACL 的目錄，group 位元就是 ACL mask（installer 要 r-x）；
    # 舊實作無條件 chmod 0700 會把 mask 清成 ---，upgrade 的 provenance 因此不符。
    run_dir = tmp_path / "run" / "cortex"
    run_dir.mkdir(parents=True)
    os.chmod(run_dir, 0o750)
    _prepare(run_dir)
    assert _mode(run_dir) == 0o750


def test_group_or_other_writable_run_dir_is_still_tightened(tmp_path: Path) -> None:
    run_dir = tmp_path / "run" / "cortex"
    run_dir.mkdir(parents=True)
    os.chmod(run_dir, 0o777)
    _prepare(run_dir)
    assert _mode(run_dir) == 0o700
