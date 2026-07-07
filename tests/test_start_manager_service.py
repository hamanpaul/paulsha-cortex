from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
START_SH = REPO / "paulsha_cortex" / "scripts" / "service-manager.sh"

# 抽出 start_manager_service 函式單獨跑（避免 source 整支 service-manager.sh 的副作用）
# NB: `}}` 是 Python str.format() 對字面 `}` 的跳脫；.format() 後 sed pattern
# 變成 /^}/，只配對行首（未縮排）的函式收尾大括號，正確框出整個函式。
_HARNESS = """
set -euo pipefail
fn="$(sed -n '/^start_manager_service()/,/^}}/p' "{start_sh}")"
eval "$fn"
start_manager_service
"""


def _run(stub_dir: Path, env: dict) -> subprocess.CompletedProcess:
    script = _HARNESS.format(start_sh=str(START_SH))
    full_env = {**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}", **env}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full_env)


def _write_stub(d: Path, name: str, body: str) -> None:
    p = d / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(0o755)


def _systemctl_ok(sd: Path, log: Path) -> None:
    """systemctl 樁：所有子命令成功，並把 argv 記到 log。"""
    _write_stub(sd, "systemctl", f'echo "$*" >> "{log}"\nexit 0\n')


def _make_unit(home: Path, instance: str) -> None:
    """模擬 timer unit 已實例化到 ~/.config/systemd/user。"""
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"{instance}-manager.timer").write_text("[Timer]\n", encoding="utf-8")


class StartManagerServiceTests(unittest.TestCase):
    def test_disabled_skips(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sd = Path(d)
            _write_stub(sd, "systemctl", 'echo "SYSTEMCTL $*" >> "' + str(sd / "systemctl.log") + '"\nexit 0\n')
            res = _run(sd, {"PSC_MANAGER_DISABLED": "1", "PSC_INSTANCE": "demo"})
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("disabled", res.stdout)
            self.assertFalse((sd / "systemctl.log").exists())

    def test_no_user_systemd_graceful_skip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sd = Path(d)
            # systemctl 存在但 `--user show-environment` 失敗 → 模擬 WSL 無 user systemd
            _write_stub(sd, "systemctl", 'if [[ "$1 $2" == "--user show-environment" ]]; then exit 1; fi\nexit 0\n')
            res = _run(sd, {"PSC_INSTANCE": "demo"})
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("skipped", res.stderr + res.stdout)

    def test_starts_timer_when_unit_present(self) -> None:
        # unit 已安裝 → 不重裝，直接 start
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "home"
            home.mkdir()
            _make_unit(home, "demo")
            sd = root / "stub"
            sd.mkdir()
            calls = root / "calls.log"
            _systemctl_ok(sd, calls)
            installer_log = root / "installer.log"
            installer = root / "installer.sh"
            _write_stub(installer.parent, installer.name, f'echo "INSTALL $*" >> "{installer_log}"\nexit 0\n')
            res = _run(sd, {"PSC_INSTANCE": "demo", "HOME": str(home), "PSC_MANAGER_INSTALLER": str(installer)})
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("--user start demo-manager.timer", calls.read_text(encoding="utf-8"))
            self.assertFalse(installer_log.exists(), "unit 已存在不應重裝")

    def test_installs_when_unit_absent(self) -> None:
        # unit 未實例化 → 先跑 installer，再 start（修 #155）
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "home"
            home.mkdir()
            sd = root / "stub"
            sd.mkdir()
            calls = root / "calls.log"
            _systemctl_ok(sd, calls)
            installer_log = root / "installer.log"
            installer = root / "installer.sh"
            _write_stub(installer.parent, installer.name, f'echo "INSTALL $*" >> "{installer_log}"\nexit 0\n')
            res = _run(sd, {"PSC_INSTANCE": "demo", "HOME": str(home), "PSC_MANAGER_INSTALLER": str(installer)})
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(installer_log.exists(), "unit 不存在時必須呼叫 installer")
            self.assertIn("INSTALL demo", installer_log.read_text(encoding="utf-8"))
            self.assertIn("--user start demo-manager.timer", calls.read_text(encoding="utf-8"))

    def test_install_failure_non_fatal(self) -> None:
        # installer 失敗 → graceful（returncode 0），且不嘗試 start
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "home"
            home.mkdir()
            sd = root / "stub"
            sd.mkdir()
            calls = root / "calls.log"
            _systemctl_ok(sd, calls)
            installer = root / "installer.sh"
            _write_stub(installer.parent, installer.name, "exit 1\n")
            res = _run(sd, {"PSC_INSTANCE": "demo", "HOME": str(home), "PSC_MANAGER_INSTALLER": str(installer)})
            self.assertEqual(res.returncode, 0, res.stderr)
            calls_text = calls.read_text(encoding="utf-8") if calls.exists() else ""
            self.assertNotIn("--user start demo-manager.timer", calls_text)
            self.assertIn("install failed", res.stderr + res.stdout)


if __name__ == "__main__":
    unittest.main()
