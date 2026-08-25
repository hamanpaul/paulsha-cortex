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

    def test_sentinel_waits_for_gate_ledger_while_wrapper_is_still_alive(self) -> None:
        """The model sentinel precedes the slower Manager-authored ledger."""
        import os

        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            log_path = Path(d) / "slice-a.jsonl"
            log_path.write_text('{"type":"result","ok":true}\n', encoding="utf-8")
            _seed_job(state, log_path=str(log_path), pid=os.getpid())
            exit_sentinel_path(str(log_path)).write_text("0", encoding="utf-8")

            fresh_reg = JobRegistry(state_path=state)
            disp = Dispatcher(fresh_reg, pane_sender=None, worktree_creator=None)
            updated = disp.poll_headless_done("slice-a-1")

            self.assertEqual(updated["status"], "dispatched")
            self.assertIsNone(updated["exit_code"])

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

    # ----------------------------------------------------------- #384: provider_outcome wiring

    def test_failed_job_with_rate_limit_signal_in_log_gets_typed_classification(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            log_path = Path(d) / "slice-a.jsonl"
            log_path.write_text(
                "starting build...\nError: secondary rate limit exceeded. Please wait.\n",
                encoding="utf-8",
            )
            _seed_job(state, log_path=str(log_path))
            exit_sentinel_path(str(log_path)).write_text("1", encoding="utf-8")

            fresh_reg = JobRegistry(state_path=state)
            disp = Dispatcher(fresh_reg, pane_sender=None, worktree_creator=None)
            updated = disp.poll_headless_done("slice-a-1")

            self.assertEqual(updated["status"], "failed")
            outcome = updated["provider_outcome"]
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome["outcome"], "rate_limited")
            self.assertEqual(outcome["authority"], "text_signal")
            self.assertTrue(outcome["retryable"])

    def test_failed_job_with_no_signal_gets_unknown_hint_classification(self) -> None:
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
            outcome = updated["provider_outcome"]
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome["outcome"], "unknown")
            self.assertEqual(outcome["authority"], "hint")
            self.assertFalse(outcome["retryable"])

    def test_exited_job_has_no_provider_outcome(self) -> None:
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
            self.assertIsNone(updated["provider_outcome"])

    def test_missing_launch_handle_failure_classifies_as_unknown_hint(self) -> None:
        # launch 本身失敗（無 log_path 可分類，read_log_tail 回 None）——分類器
        # 誠實回報「沒有訊號」（unknown/hint），不是偽造出一個具體 outcome，
        # 也不是省略欄位；retryable 仍是 False（HINT 永不驅動 retry）。
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            _seed_job(state, log_path=None, pid=None)

            fresh_reg = JobRegistry(state_path=state)
            disp = Dispatcher(fresh_reg, pane_sender=None, worktree_creator=None)
            updated = disp.poll_headless_done("slice-a-1")

            self.assertEqual(updated["status"], "failed")
            outcome = updated["provider_outcome"]
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome["outcome"], "unknown")
            self.assertEqual(outcome["authority"], "hint")
            self.assertFalse(outcome["retryable"])


if __name__ == "__main__":
    unittest.main()
