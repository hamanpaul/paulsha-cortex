"""issue #586：驗證 AF_UNIX bind 沙箱防護（probe 邏輯 + 全套 pytest 不假失敗）。

證明兩件事：

1. ``af_unix_bind_available()`` 忠實反映當前 runtime 能否 ``bind()`` 一個 AF_UNIX
   socket——在 EPERM（沙箱擋 bind／擋 socket 建立）下回 ``False``，能綁時回
   ``True``；且其判定與「當場真的 bind 一次」的 ground truth 一致（與環境無關，
   在 builder 沙箱與正常環境都成立）。

2. 在**模擬 builder 沙箱**（process-wide 把 ``socket.socket.bind`` 打成 EPERM）下，
   跑真正被防護的測試檔（``tests/test_monitor_work_api.py``）時，需要 bind 的測試
   會 **skip 而非 fail**，整個檔案 ``pytest`` **exit 0**——即修復後全套 pytest 在
   builder 沙箱語境不再因 AF_UNIX EPERM 假失敗。
"""

from __future__ import annotations

import errno
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import sandbox_support
from sandbox_support import af_unix_bind_available

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = Path(__file__).resolve().parent


def _actual_bind_capability() -> bool:
    """Ground truth：當場真的建立並 bind 一個 AF_UNIX socket。"""

    with tempfile.TemporaryDirectory(prefix="psc-afunix-truth-") as tmp:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError:
            return False
        try:
            sock.bind(str(Path(tmp) / "truth.sock"))
        except OSError:
            return False
        finally:
            sock.close()
    return True


def test_probe_matches_actual_bind_capability() -> None:
    """probe 的判定必須等於當前環境真實的 bind 能力（環境無關）。"""

    af_unix_bind_available.cache_clear()
    try:
        assert af_unix_bind_available() is _actual_bind_capability()
    finally:
        af_unix_bind_available.cache_clear()


def test_probe_returns_false_when_bind_raises_eperm() -> None:
    """bind() → EPERM（codex workspace-write 的網路隔離）時 probe 回 False。"""

    def _blocked_bind(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise PermissionError(errno.EPERM, "AF_UNIX bind blocked (sim)")

    af_unix_bind_available.cache_clear()
    try:
        with mock.patch.object(sandbox_support.socket.socket, "bind", _blocked_bind):
            assert af_unix_bind_available() is False
    finally:
        af_unix_bind_available.cache_clear()


def test_probe_returns_false_when_socket_creation_raises_eperm() -> None:
    """socket() 建立本身 → EPERM（srt reviewer 沙箱）時 probe 回 False。"""

    af_unix_bind_available.cache_clear()
    try:
        with mock.patch.object(
            sandbox_support.socket,
            "socket",
            side_effect=PermissionError(errno.EPERM, "AF_UNIX create blocked (sim)"),
        ):
            assert af_unix_bind_available() is False
    finally:
        af_unix_bind_available.cache_clear()


def test_probe_returns_true_when_bind_succeeds() -> None:
    """能建立且能 bind 時 probe 回 True（正常環境／CI／manager 不被 over-skip）。"""

    fake_sock = mock.Mock()
    fake_sock.bind.return_value = None
    fake_sock.close.return_value = None

    af_unix_bind_available.cache_clear()
    try:
        with mock.patch.object(sandbox_support.socket, "socket", return_value=fake_sock):
            assert af_unix_bind_available() is True
        fake_sock.bind.assert_called_once()
        fake_sock.close.assert_called_once()
    finally:
        af_unix_bind_available.cache_clear()


_SANDBOX_SIM_PLUGIN = """\
# Simulate the builder sandbox: process-wide block AF_UNIX bind() with EPERM,
# exactly like codex --sandbox workspace-write. Imported via `-p` before pytest
# collects, so sandbox_support's probe sees the block and the guarded tests skip.
import errno
import socket


def _blocked_bind(self, *args, **kwargs):
    raise PermissionError(errno.EPERM, "AF_UNIX bind blocked (issue #586 sandbox sim)")


socket.socket.bind = _blocked_bind
"""


def test_guarded_suite_skips_not_fails_under_simulated_sandbox() -> None:
    """模擬沙箱下，真正的防護測試檔 skip 需 bind 的測試、exit 0（不假失敗）。"""

    with tempfile.TemporaryDirectory(prefix="psc-afunix-simplugin-") as plugin_dir:
        (Path(plugin_dir) / "afunix_sandbox_sim.py").write_text(
            _SANDBOX_SIM_PLUGIN, encoding="utf-8"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [plugin_dir, str(_REPO_ROOT), env.get("PYTHONPATH", "")]
        ).strip(os.pathsep)
        # 只跑一個乾淨切分的檔（5 個 server 測試被防護、其餘 read-model 測試無 bind
        # 必須照常通過），足以證明「skip 而非 fail、exit 0」。
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(_TESTS_DIR / "test_monitor_work_api.py"),
                "-p",
                "afunix_sandbox_sim",
                "-rs",
                "-q",
                "-o",
                "addopts=",
            ],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
    combined = result.stdout + result.stderr
    # exit 0：pytest 只有在零 fail／零 error 時回 0。
    assert result.returncode == 0, combined
    # 至少 5 個 bind 測試被明確 skip（帶 #586 原因），而非靜默沒被收集。
    skipped_match = re.search(r"(\d+) skipped", combined)
    assert skipped_match is not None, combined
    assert int(skipped_match.group(1)) >= 5, combined
    assert "issue #586" in combined
