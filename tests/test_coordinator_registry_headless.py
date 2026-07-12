from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paulsha_cortex.coordinator.completion import classify_completion
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.registry import (
    COORDINATOR_STATE_SCHEMA_VERSION,
    JobRegistry,
)


def _create_slice_jobs(reg: JobRegistry, slice_id: str) -> tuple[str, str]:
    builder = reg.create_job(
        task=slice_id,
        persona="builder",
        branch=f"feature/{slice_id}",
        pane="%0",
        worktree=f"/wt/{slice_id}",
    )
    reviewer = reg.create_job(
        task=slice_id,
        persona="reviewer",
        branch=f"feature/{slice_id}",
        pane="%1",
        worktree=f"/wt/{slice_id}-review",
    )
    return builder["job_id"], reviewer["job_id"]


class CompletionTests(unittest.TestCase):
    def test_exit0_with_success_jsonl_is_exited(self) -> None:
        self.assertEqual(
            classify_completion(exit_code=0, last_jsonl_line='{"type":"result","ok":true}'),
            "exited",
        )

    def test_nonzero_exit_is_failed(self) -> None:
        self.assertEqual(classify_completion(exit_code=1, last_jsonl_line=None), "failed")

    def test_unparseable_jsonl_fallbacks_to_exit_code(self) -> None:
        self.assertEqual(classify_completion(exit_code=0, last_jsonl_line="not json"), "exited")
        self.assertEqual(classify_completion(exit_code=2, last_jsonl_line="not json"), "failed")


class VersionedRegistryTests(unittest.TestCase):
    def test_clean_start_missing_state_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "absent.json")
            self.assertEqual(reg.list_jobs(), [])
            self.assertEqual(reg.list_slices(), [])

    def test_persisted_root_includes_schema_version_jobs_and_slices(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            reg = JobRegistry(state_path=state)
            reg.create_job(
                task="slice-a",
                persona="builder",
                branch="feature/slice-a",
                pane="%0",
                worktree="/wt/slice-a",
            )

            payload = json.loads(state.read_text(encoding="utf-8"))

            self.assertEqual(
                payload["schema_version"],
                COORDINATOR_STATE_SCHEMA_VERSION,
            )
            self.assertEqual(payload["seq"], 1)
            self.assertEqual(payload["slices"], [])
            self.assertEqual(len(payload["jobs"]), 1)
            self.assertEqual(
                set(payload.keys()),
                {"schema_version", "seq", "jobs", "slices"},
            )

    def test_missing_schema_version_is_rejected_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            original = json.dumps({"seq": 1, "jobs": [], "done": []}, ensure_ascii=False, indent=2)
            state.write_text(original, encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                JobRegistry(state_path=state)

            self.assertIn(str(state), str(ctx.exception))
            self.assertIn("archive/remove", str(ctx.exception))
            self.assertEqual(state.read_text(encoding="utf-8"), original)

    def test_unknown_schema_version_is_rejected_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            original = json.dumps(
                {"schema_version": 999, "seq": 1, "jobs": [], "slices": []},
                ensure_ascii=False,
                indent=2,
            )
            state.write_text(original, encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                JobRegistry(state_path=state)

            self.assertIn(str(state), str(ctx.exception))
            self.assertIn("archive/remove", str(ctx.exception))
            self.assertEqual(state.read_text(encoding="utf-8"), original)

    def test_legacy_done_status_is_rejected_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            original = json.dumps(
                {
                    "schema_version": COORDINATOR_STATE_SCHEMA_VERSION,
                    "seq": 1,
                    "jobs": [
                        {
                            "job_id": "slice-a-1",
                            "task": "slice-a",
                            "persona": "builder",
                            "branch": "feature/slice-a",
                            "pane": "",
                            "worktree": "/wt/slice-a",
                            "status": "done",
                            "dispatch_head": None,
                            "executor": None,
                            "session_name": None,
                            "pid": None,
                            "log_path": None,
                            "exit_code": 0,
                            "created_at": "2026-07-12T00:00:00+00:00",
                        }
                    ],
                    "slices": [],
                },
                ensure_ascii=False,
                indent=2,
            )
            state.write_text(original, encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                JobRegistry(state_path=state)

            self.assertIn(str(state), str(ctx.exception))
            self.assertIn("archive/remove", str(ctx.exception))
            self.assertEqual(state.read_text(encoding="utf-8"), original)

    def test_job_statuses_are_exited_or_failed_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            reg.create_job(
                task="slice-a",
                persona="builder",
                branch="feature/slice-a",
                pane="%0",
                worktree="/wt/slice-a",
            )
            with self.assertRaisesRegex(ValueError, "done"):
                reg.update_status("slice-a-1", "done")
            updated = reg.update_status("slice-a-1", "running")
            self.assertEqual(updated["status"], "running")
            updated = reg.update_status("slice-a-1", "exited")
            self.assertEqual(updated["status"], "exited")

    def test_same_slice_cannot_have_two_active_builders(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            reg.create_job(
                task="slice-a",
                persona="builder",
                branch="feature/slice-a",
                pane="%0",
                worktree="/wt/slice-a",
            )
            with self.assertRaisesRegex(ValueError, "slice-a"):
                reg.create_job(
                    task="slice-a",
                    persona="builder",
                    branch="feature/slice-a",
                    pane="%1",
                    worktree="/wt/slice-a-retry",
                )
            reg.update_status("slice-a-1", "failed")
            retry_job = reg.create_job(
                task="slice-a",
                persona="builder",
                branch="feature/slice-a",
                pane="%1",
                worktree="/wt/slice-a-retry",
            )
            self.assertEqual(retry_job["job_id"], "slice-a-2")


class SliceRecordTests(unittest.TestCase):
    def test_slice_record_preserves_metadata_and_reloadable_histories(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            reg = JobRegistry(state_path=state)
            builder_job_id, reviewer_job_id = _create_slice_jobs(reg, "slice-a")
            created = reg.create_slice(
                slice_id="slice-a",
                spec_path="specs/slice-a.md",
                spec_hash="spec-sha",
                plan_path="plans/slice-a.md",
                plan_hash="plan-sha",
                target_branch="feature/slice-a",
                dispatch_base="base-sha",
                builder_job_id=builder_job_id,
                reviewer_job_id=reviewer_job_id,
                candidate="candidate-sha",
            )
            self.assertEqual(created["spec"]["path"], "specs/slice-a.md")
            self.assertEqual(created["plan"]["hash"], "plan-sha")
            self.assertEqual(created["state"], "pending")
            self.assertEqual(created["gate_state"], "pending")

            reg.update_slice(
                "slice-a",
                state="running",
                gate_state="failed",
                current_evidence_refs=["evidence-1"],
                current_evaluation_refs=["gate-1"],
            )
            reg.record_action(
                "slice-a",
                action="builder-exited",
                actor="builder",
                state="exited",
                evidence_refs=["evidence-2"],
                evaluation_refs=["gate-2"],
            )

            reloaded = JobRegistry(state_path=state)
            stored = reloaded.get_slice("slice-a")

            self.assertEqual(stored["spec"], {"path": "specs/slice-a.md", "hash": "spec-sha"})
            self.assertEqual(stored["plan"], {"path": "plans/slice-a.md", "hash": "plan-sha"})
            self.assertEqual(stored["target_branch"], "feature/slice-a")
            self.assertEqual(stored["dispatch_base"], "base-sha")
            self.assertEqual(stored["builder_job_id"], builder_job_id)
            self.assertEqual(stored["reviewer_job_id"], reviewer_job_id)
            self.assertEqual(stored["candidate"], "candidate-sha")
            self.assertEqual(stored["state"], "exited")
            self.assertEqual(stored["gate_state"], "failed")
            self.assertEqual(stored["current_evidence_refs"], ["evidence-2"])
            self.assertEqual(stored["current_evaluation_refs"], ["gate-2"])
            self.assertEqual([entry["refs"] for entry in stored["evidence_history"]], [["evidence-2"]])
            self.assertEqual([entry["refs"] for entry in stored["evaluation_history"]], [["gate-2"]])
            self.assertEqual([entry["action"] for entry in stored["actions"]], ["builder-exited"])

    def test_create_slice_rejects_nonexistent_builder_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")

            with self.assertRaisesRegex(ValueError, "builder_job_id"):
                reg.create_slice(
                    slice_id="slice-a",
                    spec_path="specs/slice-a.md",
                    spec_hash="spec-sha",
                    plan_path="plans/slice-a.md",
                    plan_hash="plan-sha",
                    target_branch="feature/slice-a",
                    dispatch_base="base-sha",
                    builder_job_id="builder-missing",
                    reviewer_job_id=None,
                    candidate=None,
                )

    def test_slice_transition_validator_rejects_illegal_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            reg.create_slice(
                slice_id="slice-a",
                spec_path="specs/slice-a.md",
                spec_hash="spec-sha",
                plan_path="plans/slice-a.md",
                plan_hash="plan-sha",
                target_branch="feature/slice-a",
                dispatch_base="base-sha",
                builder_job_id=None,
                reviewer_job_id=None,
                candidate=None,
            )
            reg.update_slice("slice-a", state="running")
            reg.update_slice("slice-a", state="exited", gate_state="passed")

            with self.assertRaisesRegex(ValueError, "slice state"):
                reg.update_slice("slice-a", state="running")
            with self.assertRaisesRegex(ValueError, "gate_state"):
                reg.update_slice("slice-a", gate_state="pending")

    def test_get_slice_returns_detached_history_refs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            builder_job_id, reviewer_job_id = _create_slice_jobs(reg, "slice-a")
            reg.create_slice(
                slice_id="slice-a",
                spec_path="specs/slice-a.md",
                spec_hash="spec-sha",
                plan_path="plans/slice-a.md",
                plan_hash="plan-sha",
                target_branch="feature/slice-a",
                dispatch_base="base-sha",
                builder_job_id=builder_job_id,
                reviewer_job_id=reviewer_job_id,
                candidate="candidate-sha",
            )
            reg.record_action(
                "slice-a",
                action="builder-exited",
                actor="builder",
                evidence_refs=["evidence-1"],
                evaluation_refs=["gate-1"],
            )

            returned = reg.get_slice("slice-a")
            returned["spec"]["path"] = "mutated-spec"
            returned["current_evidence_refs"].append("mutated-current-evidence")
            returned["current_evaluation_refs"].append("mutated-current-evaluation")
            returned["evidence_history"][0]["refs"].append("mutated-history-evidence")
            returned["evaluation_history"][0]["refs"].append("mutated-history-evaluation")
            returned["actions"].append({"action": "mutated"})

            fresh = reg.get_slice("slice-a")

            self.assertEqual(fresh["spec"]["path"], "specs/slice-a.md")
            self.assertEqual(fresh["current_evidence_refs"], ["evidence-1"])
            self.assertEqual(fresh["current_evaluation_refs"], ["gate-1"])
            self.assertEqual(fresh["evidence_history"][0]["refs"], ["evidence-1"])
            self.assertEqual(fresh["evaluation_history"][0]["refs"], ["gate-1"])
            self.assertEqual([entry["action"] for entry in fresh["actions"]], ["builder-exited"])

    def test_list_slices_returns_detached_history_refs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            builder_job_id, reviewer_job_id = _create_slice_jobs(reg, "slice-a")
            reg.create_slice(
                slice_id="slice-a",
                spec_path="specs/slice-a.md",
                spec_hash="spec-sha",
                plan_path="plans/slice-a.md",
                plan_hash="plan-sha",
                target_branch="feature/slice-a",
                dispatch_base="base-sha",
                builder_job_id=builder_job_id,
                reviewer_job_id=reviewer_job_id,
                candidate="candidate-sha",
            )
            reg.record_action(
                "slice-a",
                action="builder-exited",
                actor="builder",
                evidence_refs=["evidence-1"],
                evaluation_refs=["gate-1"],
            )

            listed = reg.list_slices()
            listed[0]["evidence_history"][0]["refs"].append("mutated-history-evidence")
            listed[0]["evaluation_history"][0]["refs"].append("mutated-history-evaluation")
            listed[0]["actions"][0]["action"] = "mutated-action"

            fresh = reg.list_slices()[0]

            self.assertEqual(fresh["evidence_history"][0]["refs"], ["evidence-1"])
            self.assertEqual(fresh["evaluation_history"][0]["refs"], ["gate-1"])
            self.assertEqual(fresh["actions"][0]["action"], "builder-exited")

    def test_update_slice_rejects_nonexistent_reviewer_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            builder_job_id, reviewer_job_id = _create_slice_jobs(reg, "slice-a")
            reg.create_slice(
                slice_id="slice-a",
                spec_path="specs/slice-a.md",
                spec_hash="spec-sha",
                plan_path="plans/slice-a.md",
                plan_hash="plan-sha",
                target_branch="feature/slice-a",
                dispatch_base="base-sha",
                builder_job_id=builder_job_id,
                reviewer_job_id=reviewer_job_id,
                candidate="candidate-sha",
            )

            with self.assertRaisesRegex(ValueError, "reviewer_job_id"):
                reg.update_slice("slice-a", reviewer_job_id="reviewer-missing")


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
            with self.assertRaisesRegex(ValueError, "running"):
                reg.update_headless_result("slice-a-1", status="running", exit_code=0)


class HeadlessCompletionPollingTests(unittest.TestCase):
    def test_poll_headless_done_marks_exited_and_persists_exit_code(self) -> None:
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

            self.assertEqual(updated["status"], "exited")
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

    def test_missing_launch_handle_only_fails_never_guesses_running(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            reg.create_job(
                task="slice-a",
                persona="builder",
                branch="feature/slice-a",
                pane="",
                worktree="/wt/slice-a",
            )
            disp = Dispatcher(reg, pane_sender=None, worktree_creator=None)

            updated = disp.poll_headless_done("slice-a-1")

            self.assertEqual(updated["status"], "failed")
            self.assertNotEqual(updated["status"], "running")


if __name__ == "__main__":
    unittest.main()
