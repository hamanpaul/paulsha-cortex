"""#746：claude reviewer 的內層 sandbox 依 runner mode 分岔（#714 的 reviewer lane 版）。

claude Bash 工具的內層 sandbox 是 bubblewrap，與模板 unit 的加固剖面硬性互斥
（`bwrap: Can't read /proc/sys/kernel/overflowuid`、8/8 命令全滅）。#716 B 的同型
處置：direct（單 UID）逐字維持內層 sandbox；systemd 模板（三分）關內層、外層為
唯一邊界。兩個模式的 `permissions.deny`（憑證／HOME 讀取拒絕）逐字相同。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from paulsha_cortex.coordinator import job_runner, launcher


def _settings(mode: str) -> dict:
    env = {"PSC_JOB_RUNNER": mode}
    with mock.patch.dict(launcher.os.environ, env, clear=False):
        return json.loads(launcher._claude_review_settings("/tmp/wt"))


class ReviewerInnerSandboxTests(unittest.TestCase):
    def test_direct_mode_keeps_the_inner_sandbox_verbatim(self) -> None:
        settings = _settings(job_runner.RUNNER_DIRECT)
        self.assertIs(settings["sandbox"]["enabled"], True)
        self.assertIs(settings["sandbox"]["failIfUnavailable"], True)

    def test_systemd_mode_disables_the_inner_sandbox(self) -> None:
        settings = _settings("systemd-template")
        self.assertEqual(settings["sandbox"], {"enabled": False})

    def test_systemd_mode_allows_bash_explicitly(self) -> None:
        """#748：關內層後 `autoAllowBashIfSandboxed` 消失，dontAsk 下 Bash 需要
        顯式 allow；deny 優先於 allow，憑證拒絕不受影響。"""

        settings = _settings("systemd-template")
        self.assertEqual(settings["permissions"]["allow"], ["Bash"])
        self.assertTrue(settings["permissions"]["deny"])

    def test_direct_mode_has_no_allow_entry(self) -> None:
        settings = _settings(job_runner.RUNNER_DIRECT)
        self.assertNotIn("allow", settings["permissions"])

    def test_permission_denials_are_identical_across_modes(self) -> None:
        direct = _settings(job_runner.RUNNER_DIRECT)
        hardened = _settings("systemd-template")
        self.assertEqual(direct["permissions"]["deny"], hardened["permissions"]["deny"])
        # 憑證讀取拒絕真的在裡面，不是空清單。
        joined = json.dumps(hardened["permissions"])
        self.assertIn(".claude", joined)
        self.assertIn(".ssh", joined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
