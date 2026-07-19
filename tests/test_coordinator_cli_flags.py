from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from paulsha_cortex.control import client as control_client, contract as control_contract
from paulsha_cortex.coordinator import cli
from paulsha_cortex.coordinator.cli import _build_parser, _refuse_unsafe_fanout, _resolve_launcher
from paulsha_cortex.coordinator.launcher import SubprocessLauncher


def _meta(slice_id: str) -> dict:
    return {"slice_id": slice_id, "dispatch": "auto", "plan": "p.md", "depends_on": []}


class ResolveLauncherTests(unittest.TestCase):
    def test_builds_subprocess_launcher_with_flags(self) -> None:
        lr = _resolve_launcher("copilot", None, allow_unsafe=True, model="claude-haiku-4.5")
        self.assertIsInstance(lr, SubprocessLauncher)
        self.assertTrue(lr._allow_unsafe)
        self.assertEqual(lr._model, "claude-haiku-4.5")
        self.assertEqual(lr._executor, "copilot")

    def test_respects_injected_launcher(self) -> None:
        sentinel = object()
        self.assertIs(_resolve_launcher("copilot", sentinel, allow_unsafe=True, model="x"), sentinel)

    def test_none_executor_returns_none(self) -> None:
        self.assertIsNone(_resolve_launcher(None, None, allow_unsafe=False, model=None))


class RefuseUnsafeFanoutTests(unittest.TestCase):
    def test_unsafe_refuses_multiple_ready(self) -> None:
        metas = [_meta("a"), _meta("b")]
        with self.assertRaises(ValueError):
            _refuse_unsafe_fanout(metas, lambda s: True, allow_unsafe=True)

    def test_unsafe_allows_single_ready(self) -> None:
        _refuse_unsafe_fanout([_meta("a")], lambda s: True, allow_unsafe=True)  # 不 raise

    def test_safe_mode_unbounded(self) -> None:
        metas = [_meta(f"s{i}") for i in range(5)]
        _refuse_unsafe_fanout(metas, lambda s: True, allow_unsafe=False)  # 不 raise


class ReapBrokerFlagTests(unittest.TestCase):
    def test_reap_brokers_help_mentions_apply_and_cwd_root(self) -> None:
        parser = _build_parser()
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as exc:
            with redirect_stdout(buf):
                parser.parse_args(["reap-brokers", "--help"])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("--apply", buf.getvalue())
        self.assertIn("--cwd-root", buf.getvalue())

    def test_tick_help_no_longer_mentions_no_reap(self) -> None:
        parser = _build_parser()
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as exc:
            with redirect_stdout(buf):
                parser.parse_args(["tick", "--help"])
        self.assertEqual(exc.exception.code, 0)
        self.assertNotIn("--no-reap", buf.getvalue())


class SliceActionFlagTests(unittest.TestCase):
    def test_slice_action_help_mentions_actor_and_actions(self) -> None:
        parser = _build_parser()
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as exc:
            with redirect_stdout(buf):
                parser.parse_args(["slice-action", "--help"])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("--actor", buf.getvalue())
        self.assertIn("retry-build", buf.getvalue())
        self.assertIn("retry-review", buf.getvalue())

    def test_slice_action_requires_actor(self) -> None:
        parser = _build_parser()
        with self.assertRaises(SystemExit) as exc:
            parser.parse_args(["slice-action", "slice-a", "retry-build"])
        self.assertEqual(exc.exception.code, 2)

    def test_slice_action_parses_required_arguments(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["slice-action", "slice-a", "retry-build", "--actor", "operator"])
        self.assertEqual(args.cmd, "slice-action")
        self.assertEqual(args.slice_id, "slice-a")
        self.assertEqual(args.action, "retry-build")
        self.assertEqual(args.actor, "operator")


class WorkActionFlagTests(unittest.TestCase):
    def test_work_retry_build_accepts_exact_candidate_payload(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "work", "retry-build", "demo", "--repo", "acme/demo",
                "--issue", "12", "--actor", "operator", "--payload", "repair.json",
            ]
        )
        self.assertEqual(args.action, "retry-build")
        self.assertEqual(args.payload, "repair.json")

    def test_work_ship_enqueues_payload_without_executing_delivery(self) -> None:
        submitted = []
        with tempfile.TemporaryDirectory() as root:
            payload = f"{root}/ship.json"
            with open(payload, "w", encoding="utf-8") as handle:
                json.dump({"pr_number": 8, "change": "demo"}, handle)
            output = io.StringIO()
            with redirect_stdout(output):
                rc = cli.main(
                    ["work", "ship", "demo", "--repo", "acme/demo", "--payload", payload],
                    control_read_status=lambda: {"degraded": False},
                    control_submit_request=lambda kind, args, actor: submitted.append(
                        (kind, args, actor)
                    )
                    or "req-1",
                    control_poll_done=lambda *_args, **_kwargs: {
                        "status": "ok",
                        "result": {"action": "awaiting-copilot"},
                    },
                )
        self.assertEqual(rc, 0)
        self.assertEqual(submitted[0][0], "work-action")
        self.assertEqual(submitted[0][1]["action"], "ship")
        self.assertEqual(submitted[0][1]["pr_number"], 8)

    def test_review_attest_parser_writes_valid_durable_control_request(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            payload_path = Path(root) / "review.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "verdict": "approved",
                        "summary": "Exact-HEAD review passed.",
                        "findings": [],
                    }
                ),
                encoding="utf-8",
            )
            req_ids: list[str] = []

            def done(req_id, *_args, **_kwargs):
                req_ids.append(req_id)
                return {"status": "ok", "result": {"action": "review-attested"}}

            with mock.patch.dict(os.environ, {"PSC_CONTROL_ROOT": root}, clear=False):
                with redirect_stdout(io.StringIO()):
                    rc = cli.main(
                        [
                            "work", "review-attest", "demo", "--repo", "acme/demo",
                            "--actor", "maintainer", "--payload", str(payload_path),
                        ],
                        control_read_status=lambda: {"degraded": False},
                        control_submit_request=control_client.submit_request,
                        control_poll_done=done,
                    )

            self.assertEqual(rc, 0)
            request = control_contract.read_json(
                Path(root) / "requests" / f"{req_ids[0]}.json"
            )
            self.assertIsNotNone(request)
            self.assertEqual(request["args"]["action"], "review-attest")
            self.assertEqual(request["args"]["actor"], "maintainer")
            self.assertEqual(request["args"]["verdict"], "approved")
            self.assertEqual(request["args"]["findings"], [])

    def test_work_link_parses_typed_kind_and_ref(self) -> None:
        args = _build_parser().parse_args(
            [
                "work",
                "link",
                "demo",
                "--repo",
                "acme/demo",
                "--kind",
                "openspec",
                "--ref",
                "unified-work-lifecycle",
            ]
        )
        self.assertEqual(args.kind, "openspec")
        self.assertEqual(args.ref, "unified-work-lifecycle")

    def test_work_help_exposes_typed_link_contract(self) -> None:
        parser = _build_parser()
        buf = io.StringIO()
        with self.assertRaises(SystemExit):
            with redirect_stdout(buf):
                parser.parse_args(["work", "--help"])
        output = buf.getvalue()
        self.assertIn("--kind", output)
        self.assertIn("--ref", output)
        self.assertIn("--issue", output)


if __name__ == "__main__":
    unittest.main()
