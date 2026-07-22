from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

from paulsha_cortex.control import constants, contract
from paulsha_cortex.coordinator.registry import JobRegistry


REQUEST_SCHEMA = "cortex-porcelain/request/v1"


def _load_cli():
    for module_name in (
        "paulsha_cortex.cli",
        "paulsha_cortex.porcelain",
        "paulsha_cortex.porcelain.request",
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
def control_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    control_root = tmp_path / "control"
    coordinator_root = tmp_path / "coordinator"
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(control_root))
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(coordinator_root))
    return {"control_root": control_root, "coordinator_root": coordinator_root}


def _write_request(
    req_id: str,
    *,
    req_type: str = "work-action",
    args: dict[str, object] | None = None,
    created_at: str = "2026-07-22T04:00:00+00:00",
) -> dict[str, object]:
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "req_id": req_id,
        "type": req_type,
        "args": dict(args or {"action": "start", "repo": "example/acme", "work_id": "porcelain-request"}),
        "requested_by": "builder:test",
        "created_at": created_at,
    }
    contract.atomic_write_json(constants.requests_dir() / f"{req_id}.json", payload)
    return payload


def _write_done(
    req_id: str,
    *,
    status: str = "ok",
    result: dict[str, object] | None = None,
    error: str | None = None,
    started_at: str = "2026-07-22T04:00:05+00:00",
    finished_at: str = "2026-07-22T04:00:10+00:00",
) -> dict[str, object]:
    payload = contract.build_done(
        req_id=req_id,
        status=status,
        result=result,
        error=error,
        started_at=started_at,
    )
    payload["finished_at"] = finished_at
    contract.atomic_write_json(constants.done_dir() / f"{req_id}.json", payload)
    return payload


def _set_mtime(path: Path, when: int) -> None:
    os.utime(path, (when, when))


def test_request_list_json_merges_pending_and_done_sorted_by_mtime(
    control_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    pending_id = "20260722T040001Z-" + "1" * 32
    done_id = "20260722T040000Z-" + "2" * 32

    _write_request(pending_id, created_at="2026-07-22T04:00:01+00:00")
    _write_request(done_id, created_at="2026-07-22T04:00:00+00:00")
    _write_done(done_id, result={"job_id": "slice-a-1"})

    _set_mtime(constants.requests_dir() / f"{pending_id}.json", 2_000_000_010)
    _set_mtime(constants.requests_dir() / f"{done_id}.json", 2_000_000_000)
    _set_mtime(constants.done_dir() / f"{done_id}.json", 2_000_000_001)

    assert _run_cli(["request", "list", "--json"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema"] == REQUEST_SCHEMA
    assert [row["request_id"] for row in payload["requests"]] == [pending_id, done_id]
    assert [row["state"] for row in payload["requests"]] == ["pending", "done"]
    assert payload["requests"][1]["type"] == "work-action"
    assert captured.err == ""


@pytest.mark.parametrize(
    ("terminal_error", "expected_state"),
    [
        (None, "pending"),
        ("RuntimeError: daemon exploded", "done"),
    ],
)
def test_request_show_json_reports_pending_and_terminal_state(
    control_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    terminal_error: str | None,
    expected_state: str,
) -> None:
    req_id = "20260722T040010Z-" + "3" * 32
    request = _write_request(req_id)
    if terminal_error is not None:
        _write_done(req_id, status="error", error=terminal_error)

    assert _run_cli(["request", "show", req_id, "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == REQUEST_SCHEMA
    assert payload["request"]["request_id"] == req_id
    assert payload["request"]["state"] == expected_state
    assert payload["request"]["type"] == request["type"]
    if terminal_error is None:
        assert payload["request"]["args"] == request["args"]
    else:
        assert payload["request"]["error"] == terminal_error


def test_request_wait_returns_zero_for_successful_done_payload(
    control_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    req_id = "20260722T040020Z-" + "4" * 32
    _write_request(req_id)
    _write_done(req_id, result={"job_id": "slice-a-1"})

    assert _run_cli(["request", "wait", req_id, "--timeout", "0"]) == 0
    assert capsys.readouterr().err == ""


def test_request_wait_returns_one_for_terminal_error(
    control_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    req_id = "20260722T040030Z-" + "5" * 32
    _write_request(req_id)
    _write_done(req_id, status="error", error="RuntimeError: boom")

    assert _run_cli(["request", "wait", req_id, "--timeout", "0"]) == 1
    assert capsys.readouterr().err == ""


def test_request_wait_returns_three_with_tracking_hint_on_timeout(
    control_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    req_id = "20260722T040040Z-" + "6" * 32
    _write_request(req_id)

    assert _run_cli(["request", "wait", req_id, "--timeout", "0"]) == 3

    captured = capsys.readouterr()
    assert req_id in (captured.out + captured.err)
    assert "request show" in (captured.out + captured.err)


def test_request_logs_json_includes_related_job_metadata(
    control_runtime: dict[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    req_id = "20260722T040050Z-" + "7" * 32
    _write_request(req_id)
    log_path = tmp_path / "logs" / "slice-a.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"event":"builder"}\n', encoding="utf-8")

    registry = JobRegistry(state_path=control_runtime["coordinator_root"] / "jobs.json")
    job = registry.create_job(
        task="slice-a",
        persona="builder",
        branch="feature/slice-a",
        pane="builder:1",
        worktree=str(tmp_path / "wt"),
        log_path=str(log_path),
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    _write_done(req_id, result={"job_id": job["job_id"], "run_id": "workflow-" + "a" * 20})

    assert _run_cli(["request", "logs", req_id, "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == REQUEST_SCHEMA
    assert payload["request_id"] == req_id
    assert payload["done"]["result"]["job_id"] == job["job_id"]
    assert payload["related_jobs"][0]["job_id"] == job["job_id"]
    assert payload["related_jobs"][0]["log_path"] == str(log_path)
