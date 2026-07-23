from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from paulsha_cortex.control import constants, contract


RECOVER_SCHEMA = "cortex-porcelain/recover/v1"
REQUEST_ID = "20260723T020304Z-" + "b" * 32


def _load_cli():
    for module_name in (
        "paulsha_cortex.cli",
        "paulsha_cortex.porcelain",
        "paulsha_cortex.porcelain.recover",
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
    error: str | None = None,
) -> None:
    contract.atomic_write_json(
        constants.done_dir() / f"{REQUEST_ID}.json",
        contract.build_done(
            req_id=REQUEST_ID,
            status=status,
            result={"recovered": status == "ok"},
            error=error,
            started_at="2026-07-23T02:03:05+00:00",
        ),
    )


@pytest.mark.parametrize(
    ("argv", "request_type", "expected_args"),
    [
        (
            [
                "recover",
                "slice",
                "slice-92",
                "retry-review",
                "--actor",
                "operator@example",
            ],
            "slice-action",
            {
                "slice_id": "slice-92",
                "action": "retry-review",
                "actor": "operator@example",
            },
        ),
        (
            [
                "recover",
                "work",
                "porcelain-run-recover",
                "abandon",
                "--repo",
                "hamanpaul/paulsha-cortex",
                "--actor",
                "operator@example",
                "--expected-run-id",
                "workflow-" + "c" * 20,
                "--reason",
                "Superseded by operator recovery.",
            ],
            "work-action",
            {
                "work_id": "porcelain-run-recover",
                "action": "abandon",
                "repo": "hamanpaul/paulsha-cortex",
                "actor": "operator@example",
                "expected_run_id": "workflow-" + "c" * 20,
                "reason": "Superseded by operator recovery.",
            },
        ),
    ],
)
def test_recover_mutations_map_to_existing_request_types_and_arguments(
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


@pytest.mark.parametrize(
    "argv",
    [
        ["recover", "slice", "slice-92", "retry-build"],
        [
            "recover",
            "work",
            "porcelain-run-recover",
            "resume",
            "--repo",
            "hamanpaul/paulsha-cortex",
        ],
    ],
)
def test_recover_actor_is_required_before_queue_write(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    assert _run_cli(argv) == 2

    assert contract.read_json(constants.requests_dir() / f"{REQUEST_ID}.json") is None
    assert "--actor" in capsys.readouterr().err


def test_recover_brokers_reap_delegates_to_existing_primitive(
    control_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from paulsha_cortex.coordinator import broker_reaper

    calls: list[tuple[bool, Path | None]] = []

    def fake_reap(*, apply: bool, cwd_root: Path | None):
        calls.append((apply, cwd_root))
        return {"ran": True, "returncode": 0, "reaped": 2}

    monkeypatch.setattr(broker_reaper, "reap_orphan_brokers", fake_reap)

    assert _run_cli(["recover", "brokers", "reap", "--apply", "--cwd-root", "/tmp/project"]) == 0

    assert calls == [(True, Path("/tmp/project").resolve())]
    assert capsys.readouterr().err == ""


def test_recover_service_restart_delegates_to_porcelain_service(
    control_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = importlib.import_module("paulsha_cortex.porcelain.service")
    calls: list[list[str]] = []

    def fake_service_main(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(service, "main", fake_service_main)

    assert _run_cli(["recover", "service", "restart", "--instance", "beta"]) == 0

    assert calls == [["restart", "--instance", "beta"]]
    assert capsys.readouterr().err == ""


def test_recover_service_restart_does_not_expose_json_flag(
    control_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = importlib.import_module("paulsha_cortex.porcelain.service")
    called = False

    def fake_service_main(argv):
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(service, "main", fake_service_main)

    assert _run_cli(["recover", "service", "restart", "--instance", "beta", "--json"]) == 2

    captured = capsys.readouterr()
    assert called is False
    assert "unrecognized arguments: --json" in captured.err


def test_recover_json_emits_one_versioned_pending_document(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        _run_cli(
            [
                "recover",
                "slice",
                "slice-92",
                "retry-build",
                "--actor",
                "operator@example",
                "--json",
            ]
        )
        == 3
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert captured.err == ""
    assert payload == {
        "schema": RECOVER_SCHEMA,
        "request_id": REQUEST_ID,
        "action": "slice retry-build",
        "accepted": True,
        "status": "pending",
        "hint": f"cortex request wait {REQUEST_ID}",
    }


@pytest.mark.parametrize(
    ("done_status", "done_error", "expected_exit"),
    [
        ("ok", None, 0),
        ("error", "recovery rejected", 1),
    ],
)
def test_recover_wait_returns_terminal_exit_code(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    done_status: str,
    done_error: str | None,
    expected_exit: int,
) -> None:
    _write_done(status=done_status, error=done_error)

    assert (
        _run_cli(
            [
                "recover",
                "slice",
                "slice-92",
                "retry-build",
                "--actor",
                "operator@example",
                "--wait",
                "--timeout",
                "0",
            ]
        )
        == expected_exit
    )
    assert REQUEST_ID in capsys.readouterr().out


def test_recover_wait_timeout_returns_three_and_repeats_tracking_hint(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        _run_cli(
            [
                "recover",
                "work",
                "porcelain-run-recover",
                "resume",
                "--repo",
                "hamanpaul/paulsha-cortex",
                "--actor",
                "operator@example",
                "--wait",
                "--timeout",
                "0",
            ]
        )
        == 3
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert combined.count(REQUEST_ID) >= 2
    assert f"cortex request show {REQUEST_ID}" in combined


@pytest.mark.parametrize(
    "argv",
    [
        [
            "recover",
            "slice",
            "slice-92",
            "retry-build",
            "--actor",
            "operator@example",
            "--allow-unsafe",
        ],
        ["recover", "brokers", "reap", "--allow-unsafe"],
        ["recover", "service", "restart", "--allow-unsafe"],
    ],
)
def test_recover_does_not_expose_unsafe_bypass_flags(
    control_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    assert _run_cli(argv) == 2

    assert contract.read_json(constants.requests_dir() / f"{REQUEST_ID}.json") is None
    assert "--allow-unsafe" in capsys.readouterr().err
