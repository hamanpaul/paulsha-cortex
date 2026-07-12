from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path


class FakePaneSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))


class FakeWorktreeCreator:
    def __init__(self, root: str = "/fake/wt") -> None:
        self.root = root
        self.created: list[str] = []

    def create(self, branch: str) -> str:
        self.created.append(branch)
        return f"{self.root}/{branch.replace('/', '-')}"


class _RaisingWorktreeCreator:
    def create(self, branch: str) -> str:
        raise ValueError("boom: worktree add failed")


class DispatcherTests(unittest.TestCase):
    def _make(self, tmp: Path):
        from paulsha_cortex.coordinator.dispatcher import Dispatcher
        from paulsha_cortex.coordinator.registry import JobRegistry

        reg = JobRegistry(state_path=tmp / "jobs.json")
        sender = FakePaneSender()
        creator = FakeWorktreeCreator()
        return Dispatcher(reg, sender, creator), reg, sender, creator

    def test_dispatch_records_job_and_sends_command(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            disp, reg, sender, creator = self._make(Path(d))
            command = 'copilot --model gpt-5.4 --yolo -p "<contract+PROMPT>"'
            job = disp.dispatch(task="slice-a", persona="builder", pane_id="%5", command=command)

            self.assertEqual(job["job_id"], "slice-a-1")
            self.assertEqual(job["status"], "dispatched")
            self.assertEqual(job["pane"], "%5")
            self.assertEqual(creator.created, ["feature/slice-a"])
            self.assertEqual(job["worktree"], "/fake/wt/feature-slice-a")
            self.assertEqual(job["branch"], "feature/slice-a")
            self.assertEqual(sender.sent, [("%5", command)])
            self.assertEqual(len(reg.list_jobs()), 1)
            self.assertEqual(reg.get_job("slice-a-1")["status"], "dispatched")

    def test_worktree_failure_records_no_job_and_sends_nothing(self) -> None:
        from paulsha_cortex.coordinator.dispatcher import Dispatcher
        from paulsha_cortex.coordinator.registry import JobRegistry

        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            sender = FakePaneSender()
            disp = Dispatcher(reg, sender, _RaisingWorktreeCreator())
            with self.assertRaises(ValueError):
                disp.dispatch(task="x", persona="builder", pane_id="%9", command="cmd")
            self.assertEqual(sender.sent, [])
            self.assertEqual(reg.list_jobs(), [])

    def test_poll_done_marks_exited_on_new_commit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            disp, reg, _, _ = self._make(Path(d))
            job = disp.dispatch(
                task="c",
                persona="builder",
                pane_id="%3",
                command="cmd-c",
                git_runner=lambda args: "baselinehead",
            )
            updated = disp.poll_done(job["job_id"], git_runner=lambda args: "deadbeefcafe")
            self.assertEqual(updated["status"], "exited")
            self.assertEqual(reg.get_job("c-1")["status"], "exited")

    def test_poll_done_no_new_commit_keeps_status(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            disp, _, _, _ = self._make(Path(d))
            disp.dispatch(
                task="e",
                persona="builder",
                pane_id="%4",
                command="cmd-e",
                git_runner=lambda args: "samehead",
            )
            updated = disp.poll_done("e-1", git_runner=lambda args: "samehead")
            self.assertEqual(updated["status"], "dispatched")


class CliTests(unittest.TestCase):
    def _fakes(self, tmp: Path):
        from paulsha_cortex.coordinator.registry import JobRegistry

        reg = JobRegistry(state_path=tmp / "jobs.json")
        return reg, FakePaneSender(), FakeWorktreeCreator()

    def test_main_dispatch_without_spec_metadata_is_rejected_and_state_unchanged(self) -> None:
        from paulsha_cortex.coordinator import cli

        with tempfile.TemporaryDirectory() as d:
            reg, sender, creator = self._fakes(Path(d))
            state_path = Path(d) / "jobs.json"
            before = state_path.read_text(encoding="utf-8") if state_path.exists() else None
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(
                    [
                        "dispatch",
                        "--task",
                        "slice-z",
                        "--persona",
                        "builder",
                        "--pane",
                        "%7",
                        "--command",
                        'copilot --model gpt-5.4 --yolo -p "go"',
                    ],
                    registry=reg,
                    pane_sender=sender,
                    worktree_creator=creator,
                )
            after = state_path.read_text(encoding="utf-8") if state_path.exists() else None

            self.assertNotEqual(rc, 0)
            self.assertIn("spec metadata", err.getvalue())
            self.assertEqual(sender.sent, [])
            self.assertEqual(creator.created, [])
            self.assertEqual(reg.list_jobs(), [])
            self.assertEqual(before, after)

    def test_main_jobs_and_stat_are_read_only(self) -> None:
        from paulsha_cortex.coordinator import cli

        with tempfile.TemporaryDirectory() as d:
            reg, sender, creator = self._fakes(Path(d))
            reg.create_job(
                task="j",
                persona="builder",
                branch="feature/j",
                pane="%1",
                worktree="/wt/j",
            )

            state_path = Path(d) / "jobs.json"
            before = state_path.read_text(encoding="utf-8")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["jobs"], registry=reg, pane_sender=sender, worktree_creator=creator)
            self.assertEqual(rc, 0)
            listed = json.loads(buf.getvalue())
            self.assertEqual([j["job_id"] for j in listed], ["j-1"])

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["stat", "j-1"], registry=reg, pane_sender=sender, worktree_creator=creator)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["job_id"], "j-1")

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cli.main(["stat", "nope-9"], registry=reg, pane_sender=sender, worktree_creator=creator)
            self.assertNotEqual(rc, 0)
            self.assertEqual(state_path.read_text(encoding="utf-8"), before)

    def test_complete_submits_control_request_only(self) -> None:
        from paulsha_cortex.coordinator import cli

        submitted: list[tuple[str, dict, str]] = []
        polled: list[tuple[str, float, float]] = []

        def fake_submit(req_type: str, args: dict, requested_by: str) -> str:
            submitted.append((req_type, dict(args), requested_by))
            return "req-1"

        def fake_poll(req_id: str, timeout: float, poll_interval: float = 0.5) -> dict:
            polled.append((req_id, timeout, poll_interval))
            return {
                "status": "ok",
                "result": {"completed": [{"slice_id": "slice-a", "gate_status": "passed"}]},
            }

        with tempfile.TemporaryDirectory() as d:
            reg, sender, creator = self._fakes(Path(d))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(
                    ["complete", "--handoff-dir", str(Path(d) / "handoff")],
                    registry=reg,
                    pane_sender=sender,
                    worktree_creator=creator,
                    control_read_status=lambda: {"degraded": False, "degraded_reason": None},
                    control_submit_request=fake_submit,
                    control_poll_done=fake_poll,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(
                submitted,
                [("complete", {"handoff_dir": str(Path(d) / "handoff")}, "coordinator-cli")],
            )
            self.assertEqual(polled, [("req-1", 5.0, 0.1)])
            self.assertEqual(json.loads(buf.getvalue()), {"completed": [{"slice_id": "slice-a", "gate_status": "passed"}]})
            self.assertEqual(reg.list_jobs(), [])

    def test_mutation_cli_fails_clearly_when_daemon_not_running(self) -> None:
        from paulsha_cortex.coordinator import cli

        submitted: list[tuple[str, dict, str]] = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(
                ["tick", "--specs-dir", "specs"],
                control_read_status=lambda: {"degraded": True, "degraded_reason": "missing"},
                control_submit_request=lambda *args: submitted.append(args) or "req-1",
            )

        self.assertNotEqual(rc, 0)
        self.assertIn("manager daemon", err.getvalue())
        self.assertEqual(submitted, [])


if __name__ == "__main__":
    unittest.main()
