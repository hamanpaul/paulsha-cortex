from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from paulsha_cortex.control import constants, contract


RUN_SCHEMA = "cortex-porcelain/run/v1"
REQUEST_ID = "20260723T010203Z-" + "a" * 32


def _load_cli():
    for module_name in (
        "paulsha_cortex.cli",
        "paulsha_cortex.porcelain",
        "paulsha_cortex.porcelain.run",
    ):
        sys.modules.pop(module_name, None)
    return importlib.import_module("paulsha_cortex.cli")


def _run_cli(argv: list[str]) -> int:
    cli = _load_cli()
    try:
        return cli.main(argv)
    except SystemExit as error:
        code = error.code
        return code if isinstance(code, int) else 1


@pytest.fixture
def control_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    control_root = tmp_path / "control"
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(control_root))
    monkeypatch.setattr(contract, "generate_req_id", lambda: REQUEST_ID)
    return control_root


def _submitted_request() -> dict[str, object]:
    payload = contract.read_json(constants.requests_dir() / f"{REQUEST_ID}.json")
    assert payload is not None
    return payload


def _write_done(
    *,
    status: str = "ok",
    result: dict[str, object] | None = None,
    error: str | None = None,
) -> None:
    contract.atomic_write_json(
        constants.done_dir() / f"{REQUEST_ID}.json",
        contract.build_done(
            req_id=REQUEST_ID,
            status=status,
            result=result,
            error=error,
            started_at="2026-07-23T01:02:04+00:00",
        ),
    )


@pytest.mark.parametrize(
    ("argv", "request_type", "expected_args"),
    [
        (
            [
                "run",
                "tick",
                "--specs-dir",
                "specs/ready",
                "--executor",
                "codex",
                "--model",
                "gpt-builder",
                "--review-executor",
                "claude",
                "--review-model",
                "reviewer",
            ],
            "tick",
            {
                "specs_dir": "specs/ready",
                "executor": "codex",
                "model": "gpt-builder",
                "review_executor": "claude",
                "review_model": "reviewer",
            },
        ),
        (
            [
                "run",
                "fanout",
                "--specs-dir",
                "specs/ready",
                "--executor",
                "copilot",
                "--model",
                "builder",
            ],
            "fanout",
            {
                "specs_dir": "specs/ready",
                "executor": "copilot",
                "model": "builder",
            },
        ),
        (
            [
                "run",
                "complete",
                "--review-executor",
                "codex",
                "--review-model",
                "reviewer",
            ],
            "complete",
            {
                "review_executor": "codex",
                "review_model": "reviewer",
            },
        ),
        (
            [
                "run",
                "work",
                "resume",
                "porcelain-run-recover",
                "--repo",
                "hamanpaul/paulsha-cortex",
                "--actor",
                "operator@example",
            ],
            "work-action",
            {
                "action": "resume",
                "work_id": "porcelain-run-recover",
                "repo": "hamanpaul/paulsha-cortex",
                "actor": "operator@example",
            },
        ),
    ],
)
def test_run_subcommands_map_to_existing_request_types_and_arguments(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    request_type: str,
    expected_args: dict[str, object],
) -> None:
    assert _run_cli(argv) == 3

    request = _submitted_request()
    assert request["type"] == request_type
    assert request["args"] == expected_args
    assert isinstance(request["requested_by"], str) and request["requested_by"]
    assert capsys.readouterr().err == ""


def test_run_without_wait_prints_accepted_request_tracking_block(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["run", "tick"]) == 3

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        f"request_id: {REQUEST_ID}",
        "action: tick",
        "accepted: true",
        "status: pending",
        f"hint: cortex request wait {REQUEST_ID}",
    ]


def test_run_json_emits_one_versioned_pending_document(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["run", "work", "start", "work-92", "--repo", "example/repo", "--json"]) == 3

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert captured.err == ""
    assert payload == {
        "schema": RUN_SCHEMA,
        "request_id": REQUEST_ID,
        "action": "work start",
        "accepted": True,
        "status": "pending",
        "hint": f"cortex request wait {REQUEST_ID}",
    }


def test_run_work_payload_help_describes_json_file_path(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["run", "work", "-h"]) == 0

    captured = capsys.readouterr()
    assert "JSON 檔案路徑" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("done_status", "done_error", "expected_exit"),
    [
        ("ok", None, 0),
        ("error", "RuntimeError: build failed", 1),
    ],
)
def test_run_wait_returns_terminal_exit_code(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    done_status: str,
    done_error: str | None,
    expected_exit: int,
) -> None:
    _write_done(status=done_status, result={"completed": done_status == "ok"}, error=done_error)

    assert _run_cli(["run", "complete", "--wait", "--timeout", "0"]) == expected_exit

    captured = capsys.readouterr()
    assert REQUEST_ID in captured.out
    assert captured.err == ""


def test_run_wait_timeout_returns_three_and_repeats_tracking_hint(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["run", "fanout", "--wait", "--timeout", "0"]) == 3

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert combined.count(REQUEST_ID) >= 2
    assert f"cortex request show {REQUEST_ID}" in combined


def test_run_wait_json_reflects_effective_deployment_executor_and_model(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_done(
        result={
            "executor": "copilot",
            "model": "deployment-builder",
            "summary": {"dispatched": 1},
        }
    )

    assert _run_cli(["run", "tick", "--wait", "--timeout", "0", "--json"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert captured.err == ""
    assert payload["schema"] == RUN_SCHEMA
    assert payload["request_id"] == REQUEST_ID
    assert payload["status"] == "ok"
    assert payload["result"]["executor"] == "copilot"
    assert payload["result"]["model"] == "deployment-builder"
    request = _submitted_request()
    assert "executor" not in request["args"]
    assert "model" not in request["args"]


def test_run_does_not_expose_allow_unsafe(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["run", "tick", "--allow-unsafe"]) == 2

    assert contract.read_json(constants.requests_dir() / f"{REQUEST_ID}.json") is None
    assert "--allow-unsafe" in capsys.readouterr().err
