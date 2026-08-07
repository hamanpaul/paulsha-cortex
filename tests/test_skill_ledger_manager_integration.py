"""`manager.run_tick` 的 `ledger_recorder` / `skill_janitor` 注入點（issue #204）。

比照既有 `reaper=` 注入測試骨架（`tests/test_coordinator_manager.py` 的
`RunTickTests.test_reaper_*` 系列）：同款「預設 None 不啟用、傳入後 complete
之後呼叫一次、例外一律吸收不破壞 tick」不變量，換成 skill 治理的兩個新 hook。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.registry import JobRegistry


class FakeDispatcher:
    """包真 JobRegistry；poll_headless_done 依 poll_map 腳本化轉態。"""

    def __init__(self, registry: JobRegistry, poll_map: dict | None = None) -> None:
        self._registry = registry
        self._poll_map = poll_map or {}

    def poll_headless_done(self, job_id: str) -> dict:
        status = self._poll_map.get(job_id)
        if status is None:
            return self._registry.get_job(job_id)
        return self._registry.update_headless_result(
            job_id, status=status, exit_code=0 if status == "exited" else 1
        )


def _reg(tmp: str) -> JobRegistry:
    return JobRegistry(state_path=Path(tmp) / "jobs.json")


def _make_job(reg: JobRegistry, slice_id: str) -> dict:
    return reg.create_job(
        task=slice_id, persona="builder", branch=f"feature/{slice_id}",
        pane="", worktree=f"/wt/{slice_id}",
        executor="copilot", session_name=slice_id, pid=4242,
        log_path=f"/logs/{slice_id}.jsonl",
        workflow_card="skill-card-a",
        workflow_run_id="wf-1",
        workflow_claim_key="claim-1",
    )


class TestLedgerRecorderInjection(unittest.TestCase):
    def test_ledger_recorder_called_with_completed_jobs_and_recorded_in_summary(self) -> None:
        calls: list[list[dict]] = []
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "z")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"

            def _recorder(jobs: list[dict]) -> list[dict]:
                calls.append(jobs)
                return [{"job_id": j["job_id"], "recorded": True} for j in jobs]

            summary = manager.run_tick(
                disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0",
                ledger_recorder=_recorder,
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(calls[0]), 1)
            self.assertEqual(calls[0][0]["job_id"], job["job_id"])
            self.assertEqual(calls[0][0]["workflow_card"], "skill-card-a")
            self.assertEqual(summary["skill_usage_events"], [{"job_id": job["job_id"], "recorded": True}])
            self.assertEqual(summary["completed"], [{"slice_id": "z", "gate_status": "workflow-tracked"}])

    def test_ledger_recorder_exception_does_not_break_tick(self) -> None:
        def _boom(jobs: list[dict]) -> list[dict]:
            raise RuntimeError("ledger 爆炸")

        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "w")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(
                disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0",
                ledger_recorder=_boom,
            )
            self.assertIsNone(summary["skill_usage_events"])
            self.assertTrue(any(e.get("stage") == "skill_ledger" for e in summary["errors"]))
            self.assertEqual(summary["completed"], [{"slice_id": "w", "gate_status": "workflow-tracked"}])

    def test_no_ledger_recorder_disables_hook(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0")
            self.assertIsNone(summary["skill_usage_events"])
            self.assertFalse(any(e.get("stage") == "skill_ledger" for e in summary["errors"]))


class TestSkillJanitorInjection(unittest.TestCase):
    def test_skill_janitor_result_recorded_in_summary(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(
                disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0",
                skill_janitor=lambda: calls.append(1) or {"cold_skills": [], "proposals_created": []},
            )
            self.assertEqual(calls, [1])
            self.assertEqual(summary["skill_janitor"], {"cold_skills": [], "proposals_created": []})

    def test_skill_janitor_exception_does_not_break_tick(self) -> None:
        def _boom() -> dict:
            raise RuntimeError("janitor 爆炸")

        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(
                disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0", skill_janitor=_boom,
            )
            self.assertIsNone(summary["skill_janitor"])
            self.assertTrue(any(e.get("stage") == "skill_janitor" for e in summary["errors"]))

    def test_no_skill_janitor_disables_hook(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0")
            self.assertIsNone(summary["skill_janitor"])
            self.assertFalse(any(e.get("stage") == "skill_janitor" for e in summary["errors"]))


class TestCompleteTickCompletedJobs(unittest.TestCase):
    def test_completed_jobs_mirrors_completed_with_full_job_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "z")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            self.assertEqual(len(summary["completed_jobs"]), 1)
            snapshot = summary["completed_jobs"][0]
            self.assertEqual(snapshot["job_id"], job["job_id"])
            self.assertEqual(snapshot["workflow_card"], "skill-card-a")
            self.assertEqual(snapshot["status"], "exited")


if __name__ == "__main__":
    unittest.main()
