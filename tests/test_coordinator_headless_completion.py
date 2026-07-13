from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paulsha_cortex.coordinator.dispatcher import Dispatcher, exit_sentinel_path
from paulsha_cortex.coordinator.registry import JobRegistry


def _seed_job(
    state: Path,
    *,
    log_path: str | None,
    pid: int | None = 999999,
) -> None:
    reg = JobRegistry(state_path=state)
    reg.create_job(
        task="slice-a",
        persona="builder",
        branch="feature/slice-a",
        pane="",
        worktree="/wt/slice-a",
        executor="copilot",
        session_name="slice-a" if pid is not None else None,
        pid=pid,
        log_path=log_path,
    )


class CrossProcessCompletionTests(unittest.TestCase):
    def test_sentinel_exit0_marks_exited_from_fresh_process(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            log_path = Path(d) / "slice-a.jsonl"
            log_path.write_text('{"type":"result","ok":true}\n', encoding="utf-8")
            _seed_job(state, log_path=str(log_path))
            exit_sentinel_path(str(log_path)).write_text("0", encoding="utf-8")

            fresh_reg = JobRegistry(state_path=state)
            disp = Dispatcher(fresh_reg, pane_sender=None, worktree_creator=None)
            updated = disp.poll_headless_done("slice-a-1")

            self.assertEqual(updated["status"], "exited")
            self.assertEqual(updated["exit_code"], 0)
            self.assertEqual(JobRegistry(state_path=state).get_job("slice-a-1")["status"], "exited")

    def test_sentinel_nonzero_marks_failed_from_fresh_process(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            log_path = Path(d) / "slice-a.jsonl"
            log_path.write_text("not json\n", encoding="utf-8")
            _seed_job(state, log_path=str(log_path))
            exit_sentinel_path(str(log_path)).write_text("3", encoding="utf-8")

            fresh_reg = JobRegistry(state_path=state)
            disp = Dispatcher(fresh_reg, pane_sender=None, worktree_creator=None)
            updated = disp.poll_headless_done("slice-a-1")

            self.assertEqual(updated["status"], "failed")
            self.assertEqual(updated["exit_code"], 3)

    def test_no_sentinel_but_process_alive_stays_dispatched(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            log_path = Path(d) / "slice-a.jsonl"
            _seed_job(state, log_path=str(log_path), pid=os.getpid())

            fresh_reg = JobRegistry(state_path=state)
            disp = Dispatcher(fresh_reg, pane_sender=None, worktree_creator=None)
            updated = disp.poll_headless_done("slice-a-1")

            self.assertEqual(updated["status"], "dispatched")
            self.assertIsNone(updated["exit_code"])

    def test_no_sentinel_and_process_dead_marks_failed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            log_path = Path(d) / "slice-a.jsonl"
            log_path.write_text("not json\n", encoding="utf-8")
            _seed_job(state, log_path=str(log_path), pid=2_000_000_000)

            fresh_reg = JobRegistry(state_path=state)
            disp = Dispatcher(fresh_reg, pane_sender=None, worktree_creator=None)
            updated = disp.poll_headless_done("slice-a-1")

            self.assertEqual(updated["status"], "failed")

    def test_missing_launch_handle_from_crash_recovery_becomes_failed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            _seed_job(state, log_path=None, pid=None)

            fresh_reg = JobRegistry(state_path=state)
            disp = Dispatcher(fresh_reg, pane_sender=None, worktree_creator=None)
            updated = disp.poll_headless_done("slice-a-1")

            self.assertEqual(updated["status"], "failed")
            self.assertNotEqual(updated["status"], "running")


if __name__ == "__main__":
    unittest.main()
