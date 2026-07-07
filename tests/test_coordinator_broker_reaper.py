"""paulsha_cortex.coordinator.broker_reaper 的單測。

注入 runner 假裝 subprocess，驗證命令組裝（--apply 與否）、不存在/例外的 fail-safe，
不真的執行腳本或殺行程（hermetic）。
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from paulsha_cortex.coordinator import broker_reaper


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class BrokerReaperTests(unittest.TestCase):
    def test_script_missing_is_safe_noop(self) -> None:
        res = broker_reaper.reap_orphan_brokers(script_path="/no/such/reap.sh")
        self.assertFalse(res["ran"])
        self.assertEqual(res["reason"], "script-not-found")

    def test_apply_true_passes_apply_flag(self) -> None:
        seen = {}

        def runner(cmd, **kw):
            seen["cmd"] = cmd
            return _Proc(returncode=0, stdout="無孤兒 codex broker。")

        res = broker_reaper.reap_orphan_brokers(
            apply=True, script_path=__file__, runner=runner,
        )
        self.assertTrue(res["ran"])
        self.assertTrue(res["applied"])
        self.assertEqual(res["returncode"], 0)
        self.assertIn("--apply", seen["cmd"])
        self.assertEqual(seen["cmd"][0], "bash")

    def test_apply_false_omits_apply_flag(self) -> None:
        seen = {}

        def runner(cmd, **kw):
            seen["cmd"] = cmd
            return _Proc(returncode=0, stdout="")

        res = broker_reaper.reap_orphan_brokers(
            apply=False, script_path=__file__, runner=runner,
        )
        self.assertTrue(res["ran"])
        self.assertFalse(res["applied"])
        self.assertNotIn("--apply", seen["cmd"])

    def test_runner_exception_is_swallowed(self) -> None:
        def runner(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 30)

        res = broker_reaper.reap_orphan_brokers(script_path=__file__, runner=runner)
        self.assertFalse(res["ran"])
        self.assertIn("exec-error", res["reason"])

    def test_default_script_path_points_at_repo_script(self) -> None:
        # 預設指向 repo 內固化的腳本（存在即代表路徑解析正確）
        self.assertTrue(Path(broker_reaper.DEFAULT_SCRIPT).name == "reap-codex-brokers.sh")
        self.assertTrue(Path(broker_reaper.DEFAULT_SCRIPT).is_file())


if __name__ == "__main__":
    unittest.main()
