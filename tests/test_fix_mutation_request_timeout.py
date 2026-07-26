from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from paulsha_cortex.coordinator import cli


class MutationRequestTimeoutTests(unittest.TestCase):
    def test_submit_mutation_request_uses_tiered_timeouts(self) -> None:
        def make_submit(req_type: str):
            def submit(_req_type: str, args: dict, requested_by: str) -> str:
                self.assertEqual(_req_type, req_type)
                self.assertEqual(requested_by, "coordinator-cli")
                self.assertEqual(args, {"foo": "bar"})
                return f"req-{req_type}-1"

            return submit

        table = [
            ("fanout", 60.0),
            ("tick", 60.0),
            ("complete", 30.0),
            ("work-action", 30.0),
            ("dispatch", 5.0),
        ]

        for req_type, expected_timeout in table:
            with self.subTest(req_type=req_type):
                polled: list[tuple[str, float, float]] = []

                def fake_poll(req_id: str, timeout: float, poll_interval: float = 0.5) -> dict:
                    polled.append((req_id, timeout, poll_interval))
                    return {"status": "ok", "result": {"ok": True}}

                rc = cli._submit_mutation_request(
                    req_type,
                    {"foo": "bar"},
                    read_status_fn=lambda: {"degraded": False, "degraded_reason": None},
                    submit_request_fn=make_submit(req_type),
                    poll_done_fn=fake_poll,
                )

                self.assertEqual(rc, 0)
                self.assertEqual(polled, [(f"req-{req_type}-1", expected_timeout, 0.1)])

    def test_submit_mutation_request_keeps_success_path_unchanged(self) -> None:
        submitted: list[tuple[str, dict, str]] = []
        polled: list[tuple[str, float, float]] = []

        def fake_submit(req_type: str, args: dict, requested_by: str) -> str:
            submitted.append((req_type, dict(args), requested_by))
            return "req-success-1"

        def fake_poll(req_id: str, timeout: float, poll_interval: float = 0.5) -> dict:
            polled.append((req_id, timeout, poll_interval))
            return {"status": "ok", "result": {"completed": [{"slice_id": "slice-a"}]}}

        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli._submit_mutation_request(
                "complete",
                {"handoff_dir": "runtime/handoff"},
                read_status_fn=lambda: {"degraded": False, "degraded_reason": None},
                submit_request_fn=fake_submit,
                poll_done_fn=fake_poll,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(
            submitted,
            [("complete", {"handoff_dir": "runtime/handoff"}, "coordinator-cli")],
        )
        self.assertEqual(polled, [("req-success-1", 30.0, 0.1)])
        self.assertEqual(
            json.loads(out.getvalue()),
            {"completed": [{"slice_id": "slice-a"}]},
        )

    def test_submit_mutation_request_uses_pending_exit_code_when_timed_out(self) -> None:
        req_id = "req-timeout-1"
        self.assertTrue(hasattr(cli, "EXIT_SUBMITTED_PENDING"), "EXIT_SUBMITTED_PENDING must be introduced")
        pending_code = getattr(cli, "EXIT_SUBMITTED_PENDING")

        def fake_submit(_req_type: str, _args: dict, _requested_by: str) -> str:
            return req_id

        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli._submit_mutation_request(
                "fanout",
                {"specs_dir": "specs"},
                read_status_fn=lambda: {"degraded": False, "degraded_reason": None},
                submit_request_fn=fake_submit,
                poll_done_fn=lambda req_id, timeout, poll_interval=0.5: None,
            )

        self.assertEqual(rc, pending_code)
        self.assertNotEqual(rc, 1)
        message = err.getvalue()
        self.assertIn(req_id, message)
        self.assertTrue(
            "追蹤" in message or "tracking" in message.lower() or "追蹤狀態" in message,
            msg="pending path should include tracking guidance",
        )

        def fake_error_poll(req_id: str, timeout: float, poll_interval: float = 0.5) -> dict:
            return {"status": "error", "error": "daemon rejected", "result": {}}

        rc_error = cli._submit_mutation_request(
            "fanout",
            {"specs_dir": "specs"},
            read_status_fn=lambda: {"degraded": False, "degraded_reason": None},
            submit_request_fn=fake_submit,
            poll_done_fn=fake_error_poll,
        )

        self.assertEqual(rc_error, 1)
        self.assertNotEqual(rc_error, pending_code)


if __name__ == "__main__":
    unittest.main()
