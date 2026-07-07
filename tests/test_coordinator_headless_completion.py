from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paulsha_cortex.coordinator.dispatcher import Dispatcher, exit_sentinel_path
from paulsha_cortex.coordinator.registry import JobRegistry


def _seed_job(state: Path, *, log_path: str, pid: int = 999999) -> None:
    """以「啟動進程」身分寫一筆 dispatched headless job 到持久化狀態檔。"""
    reg = JobRegistry(state_path=state)
    reg.create_job(
        task="slice-a",
        persona="builder",
        branch="feature/slice-a",
        pane="",
        worktree="/wt/slice-a",
        executor="copilot",
        session_name="slice-a",
        pid=pid,
        log_path=log_path,
    )


class CrossProcessCompletionTests(unittest.TestCase):
    """模擬：job 由 A 進程啟動並持久化，由 *全新* B 進程 poll；
    不得依賴 os.waitpid（跨進程必 ChildProcessError）。"""

    def test_sentinel_exit0_marks_done_from_fresh_process(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            log_path = Path(d) / "slice-a.jsonl"
            log_path.write_text('{"type":"result","ok":true}\n', encoding="utf-8")
            _seed_job(state, log_path=str(log_path))
            # 子進程已退出並寫下 sentinel exit code 0
            exit_sentinel_path(str(log_path)).write_text("0", encoding="utf-8")

            # 全新 registry/Dispatcher（非啟動者）；不注入 pid_waiter
            fresh_reg = JobRegistry(state_path=state)
            disp = Dispatcher(fresh_reg, pane_sender=None, worktree_creator=None)
            updated = disp.poll_headless_done("slice-a-1")

            self.assertEqual(updated["status"], "done")
            self.assertEqual(updated["exit_code"], 0)
            self.assertEqual(JobRegistry(state_path=state).get_job("slice-a-1")["status"], "done")

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
            # 用本進程 pid 當「仍存活」的子進程；無 sentinel → 仍在跑
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
            # pid 不存在（極大 pid）→ os.kill(pid,0) raise ProcessLookupError；
            # 無 sentinel → 子進程死了卻沒留 exit code → fail-closed 標 failed
            _seed_job(state, log_path=str(log_path), pid=2_000_000_000)

            fresh_reg = JobRegistry(state_path=state)
            disp = Dispatcher(fresh_reg, pane_sender=None, worktree_creator=None)
            updated = disp.poll_headless_done("slice-a-1")

            self.assertEqual(updated["status"], "failed")


if __name__ == "__main__":
    unittest.main()
