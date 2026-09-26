from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from paulsha_cortex.control import constants, contract


RUN_SCHEMA = "cortex-porcelain/run/v1"
REQUEST_ID = "20260723T010203Z-" + "a" * 32
RETRY_CARD_RUN_ID = "workflow-e45c1bd257ceec1c8285"
RETRY_CARD_WORK_ID = "copilot-review-late-observation"


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


def _retry_card_command(*options: str) -> list[str]:
    return [
        "run",
        "work",
        "retry-card",
        RETRY_CARD_WORK_ID,
        "--repo",
        "hamanpaul/paulsha-cortex",
        *options,
    ]


@pytest.mark.parametrize("source", ["flag", "payload"])
def test_run_work_retry_build_rejects_expected_run_id_before_submission(
    source: str,
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    command = [
        "run",
        "work",
        "retry-build",
        RETRY_CARD_WORK_ID,
        "--repo",
        "hamanpaul/paulsha-cortex",
    ]
    if source == "flag":
        command.extend(
            ["--expected-run-id", RETRY_CARD_RUN_ID, "--expected-candidate", "a" * 40]
        )
    else:
        payload = tmp_path / "retry-build.json"
        payload.write_text(
            json.dumps(
                {
                    "expected_candidate": "a" * 40,
                    "expected_run_id": RETRY_CARD_RUN_ID,
                }
            ),
            encoding="utf-8",
        )
        command.extend(["--payload", str(payload)])

    assert _run_cli(command) == 2

    assert "retry-build" in capsys.readouterr().err
    assert not (control_runtime / "requests" / f"{REQUEST_ID}.json").exists()


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


def test_run_work_start_forwards_combo(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        _run_cli(
            [
                "run", "work", "start", "porcelain-run-combo",
                "--repo", "hamanpaul/paulsha-cortex",
                "--combo", "fix-standard",
            ]
        )
        == 3
    )

    request = _submitted_request()
    assert request["args"]["combo"] == "fix-standard"
    assert capsys.readouterr().err == ""


def test_run_work_resume_drops_combo(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--combo 標註為 start 專用；resume 帶 --combo 時 porcelain 不得轉送，

    否則未經驗證的 combo 可能經 manager 一路滲透到
    ``start_canonical_workflow``（code review finding）。
    """

    assert (
        _run_cli(
            [
                "run", "work", "resume", "porcelain-run-combo",
                "--repo", "hamanpaul/paulsha-cortex",
                "--combo", "fix-standard",
            ]
        )
        == 3
    )

    request = _submitted_request()
    assert "combo" not in request["args"]
    assert capsys.readouterr().err == ""


def test_run_work_intake_forwards_combo(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """issue #203：intake 內部等價於 start，porcelain 也要轉送 --combo。"""

    assert (
        _run_cli(
            [
                "run", "work", "intake", "porcelain-run-combo",
                "--repo", "hamanpaul/paulsha-cortex",
                "--combo", "fix-standard",
            ]
        )
        == 3
    )

    request = _submitted_request()
    assert request["args"]["action"] == "intake"
    assert request["args"]["combo"] == "fix-standard"
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
    assert "--card" in captured.out
    assert "regenerate-gates 專用" in captured.out
    assert captured.err == ""


def test_run_work_retry_card_forwards_card_and_run_scoped_builder_override(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        _run_cli(
            _retry_card_command(
                "--expected-run-id",
                RETRY_CARD_RUN_ID,
                "--card",
                "worktree-isolation",
                "--builder-executor",
                "copilot",
                "--builder-model",
                "gpt-5.4",
            )
        )
        == 3
    )

    request = _submitted_request()
    assert request["type"] == "work-action"
    assert request["args"] == {
        "action": "retry-card",
        "work_id": RETRY_CARD_WORK_ID,
        "repo": "hamanpaul/paulsha-cortex",
        "expected_run_id": RETRY_CARD_RUN_ID,
        "card": "worktree-isolation",
        "builder_executor": "copilot",
        "builder_model": "gpt-5.4",
    }
    assert capsys.readouterr().err == ""


def test_run_work_regenerate_gates_forwards_card(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        _run_cli(
            [
                "run", "work", "regenerate-gates", RETRY_CARD_WORK_ID,
                "--repo", "hamanpaul/paulsha-cortex",
                "--expected-run-id", RETRY_CARD_RUN_ID,
                "--card", "tdd-red",
            ]
        )
        == 3
    )

    request = _submitted_request()
    assert request["type"] == "work-action"
    assert request["args"] == {
        "action": "regenerate-gates",
        "work_id": RETRY_CARD_WORK_ID,
        "repo": "hamanpaul/paulsha-cortex",
        "expected_run_id": RETRY_CARD_RUN_ID,
        "card": "tdd-red",
    }
    assert capsys.readouterr().err == ""


def test_run_work_retry_card_preserves_payload_only_card_workaround(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    payload_path = tmp_path / "retry-card.json"
    payload_path.write_text(json.dumps({"card": "worktree-isolation"}), encoding="utf-8")

    assert (
        _run_cli(
            _retry_card_command(
                "--expected-run-id",
                RETRY_CARD_RUN_ID,
                "--payload",
                str(payload_path),
            )
        )
        == 3
    )

    request = _submitted_request()
    assert request["args"]["card"] == "worktree-isolation"
    assert request["args"]["expected_run_id"] == RETRY_CARD_RUN_ID
    assert capsys.readouterr().err == ""


def test_run_work_retry_card_allows_matching_payload_card_and_preserves_supported_fields(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    payload_path = tmp_path / "retry-card.json"
    payload_path.write_text(
        json.dumps({"card": "worktree-isolation", "actor": "operator"}),
        encoding="utf-8",
    )

    assert (
        _run_cli(
            _retry_card_command(
                "--expected-run-id",
                RETRY_CARD_RUN_ID,
                "--card",
                "worktree-isolation",
                "--payload",
                str(payload_path),
            )
        )
        == 3
    )

    request = _submitted_request()
    assert request["args"]["card"] == "worktree-isolation"
    assert request["args"]["actor"] == "operator"
    assert capsys.readouterr().err == ""


def test_run_work_retry_card_rejects_conflicting_payload_card_before_request_write(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    payload_path = tmp_path / "retry-card.json"
    payload_path.write_text(json.dumps({"card": "review-verdict"}), encoding="utf-8")

    assert (
        _run_cli(
            _retry_card_command(
                "--expected-run-id",
                RETRY_CARD_RUN_ID,
                "--card",
                "worktree-isolation",
                "--payload",
                str(payload_path),
            )
        )
        == 2
    )

    assert contract.read_json(constants.requests_dir() / f"{REQUEST_ID}.json") is None
    assert "conflicts with --card" in capsys.readouterr().err


def test_run_work_card_is_rejected_for_non_retry_card_action_before_request_write(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        _run_cli(
            [
                "run",
                "work",
                "resume",
                RETRY_CARD_WORK_ID,
                "--repo",
                "hamanpaul/paulsha-cortex",
                "--card",
                "worktree-isolation",
            ]
        )
        == 2
    )

    assert contract.read_json(constants.requests_dir() / f"{REQUEST_ID}.json") is None
    assert "only valid for retry-card" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("options", "expected_error"),
    [
        (("--expected-run-id", RETRY_CARD_RUN_ID), "requires exact card id"),
        (
            ("--expected-run-id", RETRY_CARD_RUN_ID, "--card", "Bad card"),
            "requires exact card id",
        ),
        (("--card", "worktree-isolation"), "requires exact expected_run_id"),
        (
            ("--expected-run-id", "stale-run-id", "--card", "worktree-isolation"),
            "requires exact expected_run_id",
        ),
    ],
)
def test_run_work_retry_card_contract_rejects_invalid_card_or_run_id_before_request_write(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    options: tuple[str, ...],
    expected_error: str,
) -> None:
    assert _run_cli(_retry_card_command(*options)) == 2

    assert contract.read_json(constants.requests_dir() / f"{REQUEST_ID}.json") is None
    assert expected_error in capsys.readouterr().err


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
