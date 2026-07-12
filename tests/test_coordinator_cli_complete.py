from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from paulsha_cortex.coordinator import cli


class CliCompleteTests(unittest.TestCase):
    def test_complete_subcommand_routes_through_control_plane(self) -> None:
        submitted: list[tuple[str, dict, str]] = []
        polled: list[tuple[str, float, float]] = []

        def fake_submit(req_type: str, args: dict, requested_by: str) -> str:
            submitted.append((req_type, dict(args), requested_by))
            return "req-complete-1"

        def fake_poll(req_id: str, timeout: float, poll_interval: float = 0.5) -> dict:
            polled.append((req_id, timeout, poll_interval))
            return {
                "status": "ok",
                "result": {"completed": [{"slice_id": "slice-cli", "gate_status": "passed"}]},
            }

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(
                ["complete", "--handoff-dir", "runtime/handoff", "--specs-dir", "specs"],
                control_read_status=lambda: {"degraded": False, "degraded_reason": None},
                control_submit_request=fake_submit,
                control_poll_done=fake_poll,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(
            submitted,
            [("complete", {"handoff_dir": "runtime/handoff", "specs_dir": "specs"}, "coordinator-cli")],
        )
        self.assertEqual(polled, [("req-complete-1", 5.0, 0.1)])
        self.assertEqual(
            json.loads(buf.getvalue()),
            {"completed": [{"slice_id": "slice-cli", "gate_status": "passed"}]},
        )

    def test_complete_subcommand_refuses_missing_daemon(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli.main(
                ["complete", "--handoff-dir", "runtime/handoff"],
                control_read_status=lambda: {"degraded": True, "degraded_reason": "missing"},
                control_submit_request=lambda *_args: "req-ignored",
            )

        self.assertNotEqual(rc, 0)
        self.assertIn("manager daemon", err.getvalue())


if __name__ == "__main__":
    unittest.main()
