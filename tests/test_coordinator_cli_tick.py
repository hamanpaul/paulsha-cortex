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
    def send(self, *a, **k):  # pragma: no cover
        raise AssertionError("tick 不應送 pane")


class _FakeCreator(WorktreeCreator):
    def create(self, *a, **k):  # pragma: no cover
        raise AssertionError("tick 不應建 worktree")


def _run_tick(d, *extra_argv, reaper=None):
    """跑 tick 並回 (rc, summary)；可注入 fake reaper 驗證 production path 不會呼叫。"""
    reg = JobRegistry(state_path=Path(d) / "jobs.json")
    specs = Path(d) / "specs"
    specs.mkdir(exist_ok=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(
            ["tick", "--specs-dir", str(specs), "--require-idle", "--max-load=-1", *extra_argv],
            registry=reg, pane_sender=_FakeSender(), worktree_creator=_FakeCreator(),
            reaper=reaper,
        )
    return rc, json.loads(buf.getvalue())


class CliTickTests(unittest.TestCase):
    def test_tick_idle_skip_prints_skipped(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as d:
            rc, summary = _run_tick(d, reaper=lambda: calls.append(1) or {"ran": True})
            self.assertEqual(rc, 0)
            self.assertEqual(summary["dispatch_skipped"], "not-idle")
            self.assertEqual(calls, [])
            self.assertIsNone(summary["reaped"])

    def test_tick_default_does_not_wire_reaper(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as d:
            rc, summary = _run_tick(d, reaper=lambda: calls.append(1) or {"ran": True, "applied": True})
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [])
            self.assertIsNone(summary["reaped"])


if __name__ == "__main__":
    unittest.main()
