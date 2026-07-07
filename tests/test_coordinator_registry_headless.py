from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paulsha_cortex.coordinator.completion import classify_completion
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.registry import JobRegistry


class CompletionTests(unittest.TestCase):
    def test_exit0_with_success_jsonl_is_done(self) -> None:
        self.assertEqual(
            classify_completion(exit_code=0, last_jsonl_line='{"type":"result","ok":true}'),
            "done",
        )

    def test_nonzero_exit_is_failed(self) -> None:
        self.assertEqual(classify_completion(exit_code=1, last_jsonl_line=None), "failed")

    def test_unparseable_jsonl_fallbacks_to_exit_code(self) -> None:
        self.assertEqual(classify_completion(exit_code=0, last_jsonl_line="not json"), "done")
        self.assertEqual(classify_completion(exit_code=2, last_jsonl_line="not json"), "failed")


class HeadlessRegistryFieldsTests(unittest.TestCase):
    def test_create_job_records_headless_session_fields(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            job = reg.create_job(
                task="slice-a",
                persona="builder",
                branch="feature/slice-a",
                pane="%0",
                worktree="/wt/slice-a",
                executor="copilot",
                session_name="slice-a",
                pid=123,
                log_path="/logs/slice-a.jsonl",
                exit_code=0,
            )

            self.assertEqual(job["executor"], "copilot")
            self.assertEqual(job["session_name"], "slice-a")
            self.assertEqual(job["pid"], 123)
            self.assertEqual(job["log_path"], "/logs/slice-a.jsonl")
            self.assertEqual(job["exit_code"], 0)
            self.assertEqual(reg.get_job("slice-a-1")["executor"], "copilot")

    def test_update_headless_result_rejects_invalid_status_zh_tw(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            reg.create_job(
                task="slice-a",
                persona="builder",
                branch="feature/slice-a",
                pane="",
                worktree="/wt/slice-a",
                executor="copilot",
                session_name="slice-a",
                pid=123,
                log_path="/logs/slice-a.jsonl",
            )
            # 非 done/failed 的 status 須以 zh-tw 拒絕，且帶回收到的值供除錯
            with self.assertRaisesRegex(ValueError, "running"):
                reg.update_headless_result("slice-a-1", status="running", exit_code=0)


class HeadlessCompletionPollingTests(unittest.TestCase):
    def test_poll_headless_done_marks_done_and_persists_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            log_path = Path(d) / "slice-a.jsonl"
            log_path.write_text('{"type":"progress"}\n{"type":"result","ok":true}\n', encoding="utf-8")
            reg = JobRegistry(state_path=state)
            reg.create_job(
                task="slice-a",
                persona="builder",
                branch="feature/slice-a",
                pane="",
                worktree="/wt/slice-a",
                executor="copilot",
                session_name="slice-a",
                pid=123,
                log_path=str(log_path),
            )
            disp = Dispatcher(reg, pane_sender=None, worktree_creator=None)

            updated = disp.poll_headless_done("slice-a-1", pid_waiter=lambda pid: 0)

            self.assertEqual(updated["status"], "done")
            self.assertEqual(updated["exit_code"], 0)
            self.assertEqual(reg.get_job("slice-a-1")["exit_code"], 0)

    def test_poll_headless_done_marks_failed_for_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "slice-a.jsonl"
            log_path.write_text("not json\n", encoding="utf-8")
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            reg.create_job(
                task="slice-a",
                persona="builder",
                branch="feature/slice-a",
                pane="",
                worktree="/wt/slice-a",
                executor="copilot",
                session_name="slice-a",
                pid=123,
                log_path=str(log_path),
            )
            disp = Dispatcher(reg, pane_sender=None, worktree_creator=None)

            updated = disp.poll_headless_done("slice-a-1", pid_waiter=lambda pid: 2)

            self.assertEqual(updated["status"], "failed")
            self.assertEqual(updated["exit_code"], 2)

    def test_poll_headless_done_keeps_running_process_dispatched(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            reg.create_job(
                task="slice-a",
                persona="builder",
                branch="feature/slice-a",
                pane="",
                worktree="/wt/slice-a",
                executor="copilot",
                session_name="slice-a",
                pid=123,
                log_path=str(Path(d) / "missing.jsonl"),
            )
            disp = Dispatcher(reg, pane_sender=None, worktree_creator=None)

            updated = disp.poll_headless_done("slice-a-1", pid_waiter=lambda pid: None)

            self.assertEqual(updated["status"], "dispatched")
            self.assertIsNone(updated["exit_code"])


if __name__ == "__main__":
    unittest.main()
