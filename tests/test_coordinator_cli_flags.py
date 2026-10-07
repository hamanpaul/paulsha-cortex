from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from paulsha_cortex.control import client as control_client, contract as control_contract
from paulsha_cortex.coordinator import (
    cli,
    manager as coordinator_manager,
    work_actions as coordinator_work_actions,
    work_bridge,
)
from paulsha_cortex.coordinator.cli import _build_parser, _refuse_unsafe_fanout, _resolve_launcher
from paulsha_cortex.coordinator.launcher import SubprocessLauncher
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry


def _meta(slice_id: str) -> dict:
    return {"slice_id": slice_id, "dispatch": "auto", "plan": "p.md", "depends_on": []}


class ResolveLauncherTests(unittest.TestCase):
    def test_builds_subprocess_launcher_with_flags(self) -> None:
        lr = _resolve_launcher("copilot", None, allow_unsafe=True, model="claude-haiku-4.5")
        self.assertIsInstance(lr, SubprocessLauncher)
        self.assertTrue(lr._allow_unsafe)
        self.assertEqual(lr.model, "claude-haiku-4.5")
        self.assertEqual(lr.executor, "copilot")

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
        self.assertIsNone(args.review_executor)
        self.assertIsNone(args.review_model)

    def test_slice_action_help_mentions_review_identity_override(self) -> None:
        # #396 item 4：retry-review 落 needs_human(reviewer-identity-missing) 時，
        # slice-action 介面先前沒有 --review-executor/--review-model 可帶——
        # 比照 complete/tick 既有的 identity override 機制補上。
        parser = _build_parser()
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as exc:
            with redirect_stdout(buf):
                parser.parse_args(["slice-action", "--help"])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("--review-executor", buf.getvalue())
        self.assertIn("--review-model", buf.getvalue())
        self.assertIn("foreign reviewer", buf.getvalue())

    def test_slice_action_parses_review_identity_override(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "slice-action", "slice-a", "retry-review", "--actor", "operator",
                "--review-executor", "codex", "--review-model", "gpt-5.4",
            ]
        )
        self.assertEqual(args.review_executor, "codex")
        self.assertEqual(args.review_model, "gpt-5.4")

    def test_slice_action_review_identity_choices_reject_unknown_executor(self) -> None:
        # issue #442：`cg` 由「未知 executor」範例改為真正的合法選項（見
        # test_coordinator_launcher.py 的 cg 回歸測試），這裡改用一個確定不在
        # `_ARGV_BUILDERS` 的字面值，繼續守住「未知 executor 一律被 argparse
        # choices 拒絕」這條契約本身，而不是巧合地依賴 cg 曾經未支援。
        parser = _build_parser()
        with self.assertRaises(SystemExit) as exc:
            parser.parse_args(
                [
                    "slice-action", "slice-a", "retry-review", "--actor", "operator",
                    "--review-executor", "not-a-real-executor",
                ]
            )
        self.assertEqual(exc.exception.code, 2)

    def test_slice_action_forwards_review_identity_into_submitted_request(self) -> None:
        submitted = []
        rc = cli.main(
            [
                "slice-action", "slice-a", "retry-review", "--actor", "operator",
                "--review-executor", "codex", "--review-model", "gpt-5.4",
            ],
            control_read_status=lambda: {"degraded": False},
            control_submit_request=lambda kind, args, actor: submitted.append(
                (kind, args, actor)
            )
            or "req-slice-1",
            control_poll_done=lambda *_args, **_kwargs: {
                "status": "ok",
                "result": {"launched": True},
            },
        )
        self.assertEqual(rc, 0)
        self.assertEqual(submitted[0][0], "slice-action")
        self.assertEqual(
            submitted[0][1],
            {
                "slice_id": "slice-a",
                "action": "retry-review",
                "actor": "operator",
                "review_executor": "codex",
                "review_model": "gpt-5.4",
            },
        )

    def test_slice_action_omits_review_identity_when_not_provided(self) -> None:
        # 沒帶 --review-executor/--review-model 時 request args 維持既有形狀
        # （不夾帶 None 值），不影響既有 daemon 端測試對 args 字典的精確比對。
        submitted = []
        rc = cli.main(
            ["slice-action", "slice-a", "retry-build", "--actor", "operator"],
            control_read_status=lambda: {"degraded": False},
            control_submit_request=lambda kind, args, actor: submitted.append(
                (kind, args, actor)
            )
            or "req-slice-2",
            control_poll_done=lambda *_args, **_kwargs: {
                "status": "ok",
                "result": {},
            },
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            submitted[0][1],
            {"slice_id": "slice-a", "action": "retry-build", "actor": "operator"},
        )


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

    def test_work_retry_build_rejects_expected_run_id_flag(self) -> None:
        submitted = []
        error = io.StringIO()
        with redirect_stderr(error):
            rc = cli.main(
                [
                    "work", "retry-build", "demo", "--repo", "acme/demo",
                    "--expected-run-id", "workflow-" + "a" * 20,
                ],
                control_read_status=lambda: {"degraded": False},
                control_submit_request=lambda kind, args, actor: submitted.append(args)
                or "req-1",
                control_poll_done=lambda *_args, **_kwargs: {
                    "status": "ok",
                    "result": {"action": "retry-build"},
                },
            )

        self.assertEqual(rc, 2)
        self.assertEqual(submitted, [])
        self.assertIn("expected_candidate", error.getvalue())

    def test_work_retry_build_rejects_expected_run_id_in_payload(self) -> None:
        submitted = []
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            payload = f"{root}/retry-build.json"
            with open(payload, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "expected_candidate": "a" * 40,
                        "expected_run_id": "workflow-" + "a" * 20,
                    },
                    handle,
                )
            with redirect_stderr(error):
                rc = cli.main(
                    [
                        "work", "retry-build", "demo", "--repo", "acme/demo",
                        "--payload", payload,
                    ],
                    control_read_status=lambda: {"degraded": False},
                    control_submit_request=lambda kind, args, actor: submitted.append(args)
                    or "req-1",
                    control_poll_done=lambda *_args, **_kwargs: {
                        "status": "ok",
                        "result": {"action": "retry-build"},
                    },
                )

        self.assertEqual(rc, 2)
        self.assertEqual(submitted, [])
        self.assertIn("expected_candidate", error.getvalue())

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

    def test_verify_attest_parser_writes_exact_candidate_and_full_suite_payload(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            payload_path = Path(root) / "verify.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "full_suite_command": "python -m pytest -q",
                        "result_summary": {"passed": 100, "failed": 0},
                    }
                ),
                encoding="utf-8",
            )
            req_ids: list[str] = []

            def done(req_id, *_args, **_kwargs):
                req_ids.append(req_id)
                return {"status": "ok", "result": {"action": "verify-attested"}}

            with mock.patch.dict(os.environ, {"PSC_CONTROL_ROOT": root}, clear=False):
                with redirect_stdout(io.StringIO()):
                    rc = cli.main(
                        [
                            "work", "verify-attest", "demo", "--repo", "acme/demo",
                            "--actor", "operator", "--expected-candidate", "a" * 40,
                            "--payload", str(payload_path),
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
            self.assertEqual(request["args"]["action"], "verify-attest")
            self.assertEqual(request["args"]["actor"], "operator")
            self.assertEqual(request["args"]["expected_candidate"], "a" * 40)
            self.assertEqual(request["args"]["result_summary"], {"passed": 100, "failed": 0})

    def test_review_disposition_parser_writes_operator_decision_request(self) -> None:
        submitted = []

        def submit(req_type, args, requested_by):
            submitted.append((req_type, args, requested_by))
            return "request-1"

        rc = cli.main(
            [
                "work", "review-disposition", "demo", "--repo", "acme/demo",
                "--actor", "maintainer", "--reason", "討論完成，finding 不阻擋合併。",
            ],
            control_read_status=lambda: {"degraded": False},
            control_submit_request=submit,
            control_poll_done=lambda *_args, **_kwargs: {
                "status": "ok",
                "result": {"action": "review-disposition-recorded"},
            },
        )

        self.assertEqual(rc, 0)
        self.assertEqual(submitted[0][0], "work-action")
        self.assertEqual(submitted[0][1]["action"], "review-disposition")
        self.assertEqual(submitted[0][1]["actor"], "maintainer")
        self.assertEqual(submitted[0][1]["reason"], "討論完成，finding 不阻擋合併。")

    def test_work_abandon_parser_writes_exact_run_cas_and_reason(self) -> None:
        submitted = []

        def submit(req_type, args, requested_by):
            submitted.append((req_type, args, requested_by))
            return "request-1"

        rc = cli.main(
            [
                "work", "abandon", "demo", "--repo", "acme/demo",
                "--actor", "operator",
                "--expected-run-id", "workflow-" + "a" * 20,
                "--reason", "Superseded by the terminal canary.",
            ],
            control_read_status=lambda: {"degraded": False},
            control_submit_request=submit,
            control_poll_done=lambda *_args, **_kwargs: {
                "status": "ok",
                "result": {"action": "abandoned"},
            },
        )

        self.assertEqual(rc, 0)
        self.assertEqual(submitted[0][0], "work-action")
        self.assertEqual(submitted[0][1]["action"], "abandon")
        self.assertEqual(
            submitted[0][1]["expected_run_id"], "workflow-" + "a" * 20
        )
        self.assertEqual(
            submitted[0][1]["reason"], "Superseded by the terminal canary."
        )

    def test_work_start_forwards_combo(self) -> None:
        submitted = []

        def submit(req_type, args, requested_by):
            submitted.append((req_type, args, requested_by))
            return "request-1"

        rc = cli.main(
            [
                "work", "start", "demo", "--repo", "acme/demo",
                "--combo", "fix-standard",
            ],
            control_read_status=lambda: {"degraded": False},
            control_submit_request=submit,
            control_poll_done=lambda *_args, **_kwargs: {
                "status": "ok",
                "result": {"action": "started"},
            },
        )

        self.assertEqual(rc, 0)
        self.assertEqual(submitted[0][1]["combo"], "fix-standard")

    def test_work_start_and_intake_forward_builder_override_into_request_and_run(self) -> None:
        expected_override = {
            "builder": {"executor": "codex", "model_id": "gpt-6-luna"}
        }
        resolved_chain = {
            "planner": {
                "executor": "claude",
                "model": "planner-one",
                "model_id": "planner-one",
                "source": "shared-default",
            },
            "builder": {
                "executor": "codex",
                "model": "gpt-6-luna",
                "model_id": "gpt-6-luna",
                "source": "run-override",
            },
            "reviewer": {
                "executor": "claude",
                "model": "reviewer-anthropic",
                "model_id": "reviewer-anthropic",
                "source": "shared-default",
            },
        }
        identity_registry = IdentityRegistry.from_rows(
            [
                {
                    "executor": "codex",
                    "model_id": "spark",
                    "independence_domain": "openai",
                    "capabilities": ["build"],
                },
                {
                    "executor": "codex",
                    "model_id": "gpt-6-luna",
                    "independence_domain": "openai",
                    "capabilities": ["build"],
                },
                {
                    "executor": "copilot",
                    "model_id": "builder-two",
                    "independence_domain": "microsoft",
                    "capabilities": ["build"],
                },
                {
                    "executor": "claude",
                    "model_id": "planner-one",
                    "independence_domain": "anthropic",
                    "capabilities": ["planning"],
                },
                {
                    "executor": "claude",
                    "model_id": "reviewer-anthropic",
                    "independence_domain": "anthropic",
                    "capabilities": ["review"],
                },
                {
                    "executor": "agy",
                    "model_id": "reviewer-google",
                    "independence_domain": "google",
                    "capabilities": ["review"],
                },
            ]
        )

        for action in ("start", "intake"):
            with self.subTest(action=action):
                submitted: list[tuple[str, dict[str, object], str]] = []
                captured: dict[str, object] = {}
                stdout = io.StringIO()

                class FakeRun:
                    def __init__(self, model_chain_override: dict[str, dict[str, str]] | None):
                        self.model_chain_override = model_chain_override

                    def to_dict(self) -> dict[str, object]:
                        return {"model_chain_override": self.model_chain_override}

                def fake_start_canonical_workflow(**kwargs):
                    captured["start_kwargs"] = kwargs
                    return FakeRun(kwargs.get("model_chain_override"))

                def fake_execute_work_action(
                    *,
                    args,
                    requested_by,
                    workflow_registry,
                    workflow_starter,
                    **_kwargs,
                ):
                    run = workflow_starter(object(), "claim:v1:" + "1" * 64, None)
                    if args["action"] != "intake":
                        return {"action": args["action"], "run": run.to_dict()}
                    authority = SimpleNamespace(
                        repo=args["repo"],
                        work_id=args["work_id"],
                        mapped_issues=(1,),
                        mapped_todo_paths=(),
                        confirmed_todo=None,
                    )
                    with (
                        mock.patch.object(
                            coordinator_work_actions,
                            "_claim_action",
                            return_value={"action": "claim", "run": run.to_dict()},
                        ),
                        mock.patch.object(
                            work_bridge,
                            "load_model_identities",
                            return_value=identity_registry,
                        ),
                    ):
                        result = coordinator_work_actions._intake_action(
                            args=args,
                            authority=authority,
                            requested_by=requested_by,
                            now_epoch=0,
                            state_path=Path("unused-runs.json"),
                            snapshot_path=None,
                            workflow_registry=workflow_registry,
                            workflow_starter=workflow_starter,
                        )
                    return result

                with tempfile.TemporaryDirectory() as root:
                    registry = JobRegistry(state_path=Path(root) / "jobs.json")

                    def submit(req_type, args, requested_by):
                        submitted.append((req_type, args, requested_by))
                        return "request-1"

                    def poll_done(req_id, timeout, interval):
                        result = coordinator_manager.apply_work_action(
                            args=submitted[0][1],
                            requested_by="operator",
                            registry=registry,
                        )
                        return {"status": "ok", "result": result}

                    with (
                        mock.patch.object(
                            coordinator_work_actions,
                            "execute_work_action",
                            fake_execute_work_action,
                        ),
                        mock.patch.object(
                            work_bridge,
                            "start_canonical_workflow",
                            fake_start_canonical_workflow,
                        ),
                    ):
                        with redirect_stdout(stdout):
                            rc = cli.main(
                                [
                                    "work",
                                    action,
                                    "demo",
                                    "--repo",
                                    "acme/demo",
                                    "--builder-executor",
                                    "codex",
                                    "--builder-model",
                                    "gpt-6-luna",
                                ],
                                control_read_status=lambda: {"degraded": False},
                                control_submit_request=submit,
                                control_poll_done=poll_done,
                            )

                self.assertEqual(rc, 0)
                self.assertEqual(submitted[0][1]["builder_executor"], "codex")
                self.assertEqual(submitted[0][1]["builder_model"], "gpt-6-luna")
                self.assertEqual(
                    captured["start_kwargs"]["model_chain_override"], expected_override
                )
                expected_output = {
                    "action": "claim" if action == "intake" else action,
                    "run": {"model_chain_override": expected_override},
                }
                if action == "intake":
                    expected_output["linked"] = False
                    expected_output["link_result"] = None
                    expected_output["resolved_model_chain"] = resolved_chain
                self.assertEqual(
                    json.loads(stdout.getvalue()),
                    expected_output,
                )

    def test_work_start_rejects_partial_model_chain_override(self) -> None:
        submitted = []
        error = io.StringIO()

        with redirect_stderr(error):
            rc = cli.main(
                [
                    "work",
                    "start",
                    "demo",
                    "--repo",
                    "acme/demo",
                    "--builder-executor",
                    "codex",
                ],
                control_read_status=lambda: {"degraded": False},
                control_submit_request=lambda kind, args, actor: submitted.append(args)
                or "request-1",
                control_poll_done=lambda *_args, **_kwargs: {
                    "status": "ok",
                    "result": {"action": "start"},
                },
            )

        self.assertEqual(rc, 2)
        self.assertEqual(submitted, [])
        self.assertIn("requires both executor and model", error.getvalue())

    def test_work_resume_rejects_model_chain_override_flags(self) -> None:
        submitted = []
        error = io.StringIO()

        with redirect_stderr(error):
            rc = cli.main(
                [
                    "work",
                    "resume",
                    "demo",
                    "--repo",
                    "acme/demo",
                    "--builder-executor",
                    "codex",
                    "--builder-model",
                    "gpt-6-luna",
                ],
                control_read_status=lambda: {"degraded": False},
                control_submit_request=lambda kind, args, actor: submitted.append(args)
                or "request-2",
                control_poll_done=lambda *_args, **_kwargs: {
                    "status": "ok",
                    "result": {"action": "resume"},
                },
            )

        self.assertEqual(rc, 2)
        self.assertEqual(submitted, [])
        self.assertEqual(
            error.getvalue(),
            "錯誤: --planner-executor／--planner-model／--builder-executor／--builder-model／"
            "--reviewer-executor／--reviewer-model 只支援 work start／intake／rechain／"
            "supersede-attempt。\n",
        )
        parser = _build_parser()
        root_subcommands = next(action for action in parser._actions if action.dest == "cmd")
        work_parser = root_subcommands.choices["work"]
        registered_flags = {
            option
            for action in work_parser._actions
            for option in action.option_strings
        }
        expected_flags = tuple(
            f"--{field.replace('_', '-')}" for field in cli._WORK_MODEL_CHAIN_FIELDS
        )
        self.assertTrue(set(expected_flags).issubset(registered_flags))
        self.assertIn("／".join(expected_flags), error.getvalue())
        self.assertNotIn("--planner_executor", error.getvalue())

    def test_work_resume_preserves_model_chain_payload_fields(self) -> None:
        submitted = []

        with tempfile.TemporaryDirectory() as root:
            payload = Path(root) / "payload.json"
            payload.write_text(
                json.dumps(
                    {
                        "builder_executor": "codex",
                        "builder_model": "gpt-6-luna",
                    }
                ),
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                rc = cli.main(
                    [
                        "work",
                        "resume",
                        "demo",
                        "--repo",
                        "acme/demo",
                        "--payload",
                        str(payload),
                    ],
                    control_read_status=lambda: {"degraded": False},
                    control_submit_request=lambda kind, args, actor: submitted.append(args)
                    or "request-3",
                    control_poll_done=lambda *_args, **_kwargs: {
                        "status": "ok",
                        "result": {"action": "resume"},
                    },
                )

        self.assertEqual(rc, 0)
        self.assertEqual(submitted[0]["builder_executor"], "codex")
        self.assertEqual(submitted[0]["builder_model"], "gpt-6-luna")

    def test_work_start_rejects_conflicting_model_chain_payload(self) -> None:
        submitted = []
        error = io.StringIO()

        with tempfile.TemporaryDirectory() as root:
            payload = Path(root) / "payload.json"
            payload.write_text(
                json.dumps(
                    {
                        "builder_executor": "claude",
                        "builder_model": "sonnet",
                    }
                ),
                encoding="utf-8",
            )
            with redirect_stderr(error):
                rc = cli.main(
                    [
                        "work",
                        "start",
                        "demo",
                        "--repo",
                        "acme/demo",
                        "--builder-executor",
                        "codex",
                        "--builder-model",
                        "gpt-6-luna",
                        "--payload",
                        str(payload),
                    ],
                    control_read_status=lambda: {"degraded": False},
                    control_submit_request=lambda kind, args, actor: submitted.append(args)
                    or "request-3",
                    control_poll_done=lambda *_args, **_kwargs: {
                        "status": "ok",
                        "result": {"action": "start"},
                    },
                )

        self.assertEqual(rc, 2)
        self.assertEqual(submitted, [])
        self.assertIn("builder_executor", error.getvalue())
        self.assertIn("不一致", error.getvalue())

    def test_work_resume_drops_combo(self) -> None:
        """--combo 標註為 start 專用；resume 帶 --combo 時 CLI 不得轉送，

        否則未經驗證的 combo 可能經 manager 一路滲透到
        ``start_canonical_workflow``（code review finding）。
        """

        submitted = []

        def submit(req_type, args, requested_by):
            submitted.append((req_type, args, requested_by))
            return "request-1"

        rc = cli.main(
            [
                "work", "resume", "demo", "--repo", "acme/demo",
                "--combo", "fix-standard",
            ],
            control_read_status=lambda: {"degraded": False},
            control_submit_request=submit,
            control_poll_done=lambda *_args, **_kwargs: {
                "status": "ok",
                "result": {"action": "resumed"},
            },
        )

        self.assertEqual(rc, 0)
        self.assertNotIn("combo", submitted[0][1])

    def test_work_intake_forwards_combo(self) -> None:
        """issue #203：intake 內部等價於 start，--combo 一樣要轉送（見
        test_work_start_forwards_combo 的對稱案例）。
        """

        submitted = []

        def submit(req_type, args, requested_by):
            submitted.append((req_type, args, requested_by))
            return "request-1"

        rc = cli.main(
            [
                "work", "intake", "demo", "--repo", "acme/demo",
                "--combo", "fix-standard",
            ],
            control_read_status=lambda: {"degraded": False},
            control_submit_request=submit,
            control_poll_done=lambda *_args, **_kwargs: {
                "status": "ok",
                "result": {"action": "intake"},
            },
        )

        self.assertEqual(rc, 0)
        self.assertEqual(submitted[0][0], "work-action")
        self.assertEqual(submitted[0][1]["action"], "intake")
        self.assertEqual(submitted[0][1]["combo"], "fix-standard")

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
