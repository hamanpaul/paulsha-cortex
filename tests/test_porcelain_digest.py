from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from paulsha_cortex.control import constants, contract

DIGEST_SCHEMA = "cortex-coordinator/digest/v1"


def _load_cli():
    for module_name in (
        "paulsha_cortex.cli",
        "paulsha_cortex.porcelain",
        "paulsha_cortex.porcelain.digest",
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
def digest_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    control_root = tmp_path / "control"
    coordinator_root = tmp_path / "coordinator"
    control_root.mkdir(parents=True, exist_ok=True)
    coordinator_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(control_root))
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(coordinator_root))
    monkeypatch.delenv("PSC_DIGEST_DELIVERY_CMD", raising=False)

    updated_at = datetime.now(timezone.utc).isoformat()
    payload = contract.build_status(
        ready=["slice-a"],
        in_flight=[],
        recent_done=[{"slice_id": "slice-z", "gate_status": "passed", "at": updated_at}],
        daemon={"pid": os.getpid(), "last_tick_at": updated_at, "idle": False},
        updated_at=updated_at,
    )
    payload["held"] = [{"slice_id": "slice-b", "reasons": ["dispatch-hold"]}]
    payload["attention"] = [
        {"slice_id": "slice-c", "slice_state": "needs_human", "reason": "verify-failed"}
    ]
    contract.atomic_write_json(constants.status_path(), payload)

    return {"control_root": control_root, "coordinator_root": coordinator_root}


def test_digest_emit_writes_file_outbox_by_default_and_reports_json(
    digest_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["digest", "emit", "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)

    assert rendered["schema"] == DIGEST_SCHEMA
    assert rendered["delivery"]["method"] == "file"
    written_path = Path(rendered["delivery"]["path"])
    assert written_path.exists()
    assert written_path.parent == digest_runtime["coordinator_root"] / "digest" / "outbox"
    on_disk = json.loads(written_path.read_text(encoding="utf-8"))
    assert on_disk["attention"][0]["slice_id"] == "slice-c"
    assert on_disk["counts"] == {
        "attention": 1,
        # #669：claim 判定不可 claim 而刻意不建 run 的 work item 計數。
        "not_claimable": 0,
        "ready": 1,
        "held": 1,
        "recent_done": 1,
    }


def test_digest_emit_human_output_lists_attention_and_delivery_path(
    digest_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["digest", "emit"]) == 0
    human = capsys.readouterr().out

    assert "slice-c: verify-failed" in human
    assert "slice-a" in human
    assert "slice-b: dispatch-hold" in human
    assert "delivered: file ->" in human
    outbox_dir = digest_runtime["coordinator_root"] / "digest" / "outbox"
    assert len(list(outbox_dir.glob("*.json"))) == 1


def test_digest_emit_uses_env_delivery_command_and_skips_file_outbox(
    digest_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture_path = tmp_path / "captured.json"
    script = tmp_path / "capture_stdin.py"
    script.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(capture_path)!r}).write_bytes(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PSC_DIGEST_DELIVERY_CMD", f"{sys.executable} {script}")

    assert _run_cli(["digest", "emit", "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)

    assert rendered["delivery"]["method"] == "command"
    assert rendered["delivery"]["returncode"] == 0
    outbox_dir = digest_runtime["coordinator_root"] / "digest" / "outbox"
    assert not outbox_dir.exists()
    captured = json.loads(capture_path.read_text(encoding="utf-8"))
    assert captured["schema"] == DIGEST_SCHEMA
    assert captured["attention"][0]["slice_id"] == "slice-c"


def test_digest_emit_reports_error_and_exits_nonzero_when_delivery_command_fails(
    digest_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PSC_DIGEST_DELIVERY_CMD", f"{sys.executable} -c \"import sys; sys.exit(7)\"")

    assert _run_cli(["digest", "emit"]) == 1
    captured = capsys.readouterr()
    assert "錯誤" in captured.err
    outbox_dir = digest_runtime["coordinator_root"] / "digest" / "outbox"
    assert not outbox_dir.exists()


def test_digest_emit_help_is_discoverable_from_umbrella_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_cli(["--help"]) == 0
    out = capsys.readouterr().out
    assert "digest" in out

    assert _run_cli(["digest", "--help"]) == 0
    help_out = capsys.readouterr().out
    assert "emit" in help_out
