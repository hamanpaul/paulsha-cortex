from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from paulsha_cortex.coordinator import cli


def _run_tick(*extra_argv, read_status=None, submit_request=None, poll_done=None):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(
            ["tick", "--specs-dir", "specs", "--require-idle", "--max-load=-1", *extra_argv],
            control_read_status=read_status or (lambda: {"degraded": False, "degraded_reason": None}),
            control_submit_request=submit_request or (lambda _type, _args, _requested_by: "req-1"),
            control_poll_done=poll_done or (
                lambda _req_id, _timeout, _poll_interval=0.5: {
                    "status": "ok",
                    "result": {
                        "dispatch_skipped": "not-idle",
                        "dispatched": [],
                        "completed": [],
                        "errors": [],
                        "reaped": None,
                    },
                }
            ),
        )
    return rc, json.loads(buf.getvalue())


class CliTickTests(unittest.TestCase):
    def test_tick_submits_request_and_prints_summary(self) -> None:
        submitted: list[tuple[str, dict, str]] = []

        rc, summary = _run_tick(
            submit_request=lambda req_type, args, requested_by: submitted.append(
                (req_type, dict(args), requested_by)
            )
            or "req-1"
        )

        self.assertEqual(rc, 0)
        self.assertEqual(summary["dispatch_skipped"], "not-idle")
        self.assertEqual(summary["reaped"], None)
        self.assertEqual(
            submitted,
            [
                (
                    "tick",
                    {
                        "specs_dir": "specs",
                        "persona": "builder",
                        "handoff_dir": "runtime/handoff",
                        "require_idle": True,
                        "max_load": -1.0,
                        "allow_unsafe": False,
                        "model": None,
                    },
                    "coordinator-cli",
                )
            ],
        )

    def test_tick_fails_when_daemon_missing(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli.main(
                ["tick", "--specs-dir", "specs"],
                control_read_status=lambda: {"degraded": True, "degraded_reason": "missing"},
                control_submit_request=lambda *_args: "req-ignored",
            )
        self.assertNotEqual(rc, 0)
        self.assertIn("manager daemon", err.getvalue())


if __name__ == "__main__":
    unittest.main()
