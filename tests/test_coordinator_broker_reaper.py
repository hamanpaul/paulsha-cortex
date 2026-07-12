"""paulsha_cortex.coordinator.broker_reaper 的單測。

注入 runner 假裝 subprocess，驗證命令組裝（--apply 與否）、不存在/例外的 fail-safe，
不真的執行腳本或殺行程（hermetic）。
"""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from paulsha_cortex.coordinator import broker_reaper, cli


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class BrokerReaperTests(unittest.TestCase):
    def test_script_missing_is_safe_noop(self) -> None:
        res = broker_reaper.reap_orphan_brokers(script_path="/no/such/reap.sh")
        self.assertFalse(res["ran"])
        self.assertEqual(res["reason"], "script-not-found")

    def test_default_apply_is_dry_run(self) -> None:
        seen = {}

        def runner(cmd, **kw):
            seen["cmd"] = cmd
            return _Proc(returncode=0, stdout="")

        res = broker_reaper.reap_orphan_brokers(
            script_path=__file__, runner=runner,
        )
        self.assertTrue(res["ran"])
        self.assertFalse(res["applied"])
        self.assertNotIn("--apply", seen["cmd"])

    def test_apply_true_requires_cwd_root(self) -> None:
        with self.assertRaises(ValueError):
            broker_reaper.reap_orphan_brokers(apply=True, script_path=__file__)

    def test_apply_true_passes_apply_flag_and_canonical_cwd_root(self) -> None:
        seen = {}

        def runner(cmd, **kw):
            seen["cmd"] = cmd
            return _Proc(returncode=0, stdout="無孤兒 codex broker。")

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project = root / "real-project"
            project.mkdir()
            alias = root / "project-link"
            alias.symlink_to(project)
            res = broker_reaper.reap_orphan_brokers(
                apply=True, cwd_root=alias, script_path=__file__, runner=runner,
            )

        self.assertTrue(res["ran"])
        self.assertTrue(res["applied"])
        self.assertEqual(res["returncode"], 0)
        self.assertIn("--apply", seen["cmd"])
        self.assertEqual(
            seen["cmd"][-2:],
            ["--cwd-root", str(project.resolve())],
        )
        self.assertEqual(seen["cmd"][0], "bash")

    def test_runner_exception_is_swallowed(self) -> None:
        def runner(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 30)

        res = broker_reaper.reap_orphan_brokers(script_path=__file__, runner=runner)
        self.assertFalse(res["ran"])
        self.assertIn("exec-error", res["reason"])

    def test_reap_brokers_cli_defaults_to_dry_run(self) -> None:
        seen = {}
        stdout = io.StringIO()
        stderr = io.StringIO()

        def fake_reap(**kwargs):
            seen.update(kwargs)
            return {"ran": True, "applied": kwargs["apply"], "returncode": 0}

        with mock.patch.object(cli.broker_reaper, "reap_orphan_brokers", side_effect=fake_reap):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                rc = cli.main(["reap-brokers"])

        self.assertEqual(rc, 0)
        self.assertFalse(seen["apply"])
        self.assertIsNone(seen.get("cwd_root"))
        self.assertEqual(json.loads(stdout.getvalue())["applied"], False)
        self.assertEqual(stderr.getvalue(), "")

    def test_reap_brokers_cli_returns_nonzero_when_script_missing(self) -> None:
        stdout = io.StringIO()

        with mock.patch.object(
            cli.broker_reaper,
            "reap_orphan_brokers",
            return_value={"ran": False, "reason": "script-not-found"},
        ):
            with redirect_stdout(stdout):
                rc = cli.main(["reap-brokers"])

        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(stdout.getvalue())["reason"], "script-not-found")

    def test_reap_brokers_cli_returns_nonzero_when_reaper_exec_fails(self) -> None:
        stdout = io.StringIO()

        with mock.patch.object(
            cli.broker_reaper,
            "reap_orphan_brokers",
            return_value={"ran": False, "reason": "exec-error: boom"},
        ):
            with redirect_stdout(stdout):
                rc = cli.main(["reap-brokers"])

        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(stdout.getvalue())["reason"], "exec-error: boom")

    def test_reap_brokers_cli_returns_nonzero_when_script_exits_nonzero(self) -> None:
        stdout = io.StringIO()

        with mock.patch.object(
            cli.broker_reaper,
            "reap_orphan_brokers",
            return_value={"ran": True, "applied": True, "returncode": 7, "output": "", "stderr": "fail"},
        ):
            with redirect_stdout(stdout):
                rc = cli.main(["reap-brokers", "--apply", "--cwd-root", "."])

        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(stdout.getvalue())["returncode"], 7)

    def test_reap_brokers_cli_apply_requires_cwd_root(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = cli.main(["reap-brokers", "--apply"])
        self.assertEqual(rc, 2)
        self.assertIn("--cwd-root", stderr.getvalue())

    def test_reap_brokers_cli_apply_resolves_cwd_root(self) -> None:
        seen = {}
        stdout = io.StringIO()

        def fake_reap(**kwargs):
            seen.update(kwargs)
            return {"ran": True, "applied": kwargs["apply"], "returncode": 0}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project = root / "real-project"
            project.mkdir()
            alias = root / "project-link"
            alias.symlink_to(project)
            with mock.patch.object(cli.broker_reaper, "reap_orphan_brokers", side_effect=fake_reap):
                with redirect_stdout(stdout):
                    rc = cli.main(["reap-brokers", "--apply", "--cwd-root", str(alias)])

        self.assertEqual(rc, 0)
        self.assertTrue(seen["apply"])
        self.assertEqual(seen["cwd_root"], project.resolve())

    def test_default_script_path_points_at_repo_script(self) -> None:
        # 預設指向 repo 內固化的腳本（存在即代表路徑解析正確）
        self.assertTrue(Path(broker_reaper.DEFAULT_SCRIPT).name == "reap-codex-brokers.sh")
        self.assertTrue(Path(broker_reaper.DEFAULT_SCRIPT).is_file())


if __name__ == "__main__":
    unittest.main()
