from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from paulsha_cortex.coordinator import cli
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.seams import PaneSender, WorktreeCreator


class _FakeSender(PaneSender):
    def send(self, *a, **k):  # pragma: no cover - complete 不會用到
        raise AssertionError("complete 不應送 pane")


class _FakeCreator(WorktreeCreator):
    def create(self, *a, **k):  # pragma: no cover
        raise AssertionError("complete 不應建 worktree")


class CliCompleteTests(unittest.TestCase):
    def test_complete_subcommand_writes_manifest_and_prints_summary(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            # 終態但缺 manifest → complete 應 reconcile 補寫（dispatch_head=None 免 git）
            reg.create_job(task="slice-cli", persona="builder", branch="feature/slice-cli",
                           pane="", worktree="/wt/slice-cli", executor="copilot",
                           session_name="slice-cli", pid=1, log_path="/l.jsonl")
            reg.update_headless_result("slice-cli-1", status="done", exit_code=0)
            hdir = Path(d) / "handoff"

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(
                    ["complete", "--handoff-dir", str(hdir)],
                    registry=reg, pane_sender=_FakeSender(), worktree_creator=_FakeCreator(),
                )

            self.assertEqual(rc, 0)
            summary = json.loads(buf.getvalue())
            self.assertEqual(summary["completed"], [{"slice_id": "slice-cli", "gate_status": "passed"}])
            self.assertTrue((hdir / "slice-cli.json").exists())


if __name__ == "__main__":
    unittest.main()
