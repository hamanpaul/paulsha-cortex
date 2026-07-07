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
    """跑 tick 並回 (rc, summary)；一律注入 fake seam（含 reaper）保持 hermetic。"""
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
        with tempfile.TemporaryDirectory() as d:
            # 注入 fake reaper 保持 hermetic（預設 reap 開，否則會跑真實腳本回收行程）
            rc, summary = _run_tick(d, reaper=lambda: {"ran": False, "reason": "test"})
            self.assertEqual(rc, 0)
            self.assertEqual(summary["dispatch_skipped"], "not-idle")

    def test_tick_default_wires_reaper(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as d:
            rc, summary = _run_tick(d, reaper=lambda: calls.append(1) or {"ran": True, "applied": True})
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [1])  # 預設 reap 開 → janitor 被呼叫一次
            self.assertEqual(summary["reaped"], {"ran": True, "applied": True})

    def test_tick_no_reap_disables_janitor(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as d:
            # 即使注入 reaper，--no-reap 也應使其不被呼叫（active_reaper=None）
            rc, summary = _run_tick(d, "--no-reap", reaper=lambda: calls.append(1) or {"ran": True})
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [])
            self.assertIsNone(summary["reaped"])


if __name__ == "__main__":
    unittest.main()
