from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path

import pytest

from paulsha_cortex.porcelain import capacity_gate


def _load_cli():
    for module_name in (
        "paulsha_cortex.cli",
        "paulsha_cortex.porcelain",
        "paulsha_cortex.porcelain.capacity_gate",
    ):
        sys.modules.pop(module_name, None)
    return importlib.import_module("paulsha_cortex.cli")


def _run_cli(argv: list[str], stdin_text: str) -> tuple[int, str]:
    cli = _load_cli()
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    sys.stdin = io.StringIO(stdin_text)
    sys.stdout = io.StringIO()
    try:
        try:
            code = cli.main(argv)
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 1
        return code, sys.stdout.getvalue()
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout


# ---------------------------------------------------------------------------
# classify_tool
# ---------------------------------------------------------------------------


def test_task_tool_always_gated() -> None:
    assert capacity_gate.classify_tool("Task", {}) is True


def test_agent_tool_always_gated() -> None:
    assert capacity_gate.classify_tool("Agent", {}) is True


def test_plain_bash_not_gated() -> None:
    assert capacity_gate.classify_tool("Bash", {"command": "ls -la"}) is False


def test_headless_launcher_bash_gated() -> None:
    assert capacity_gate.classify_tool("Bash", {"command": "codex exec 'do the thing'"}) is True
    assert capacity_gate.classify_tool("Bash", {"command": "copilot -p 'do the thing'"}) is True
    assert capacity_gate.classify_tool("Bash", {"command": "claude -p 'do the thing'"}) is True


def test_non_bash_non_task_tool_never_gated() -> None:
    assert capacity_gate.classify_tool("Read", {"file_path": "/tmp/x"}) is False
    assert capacity_gate.classify_tool("Edit", {}) is False


# ---------------------------------------------------------------------------
# evaluate_gate
# ---------------------------------------------------------------------------


def test_evaluate_gate_busy_returns_ask() -> None:
    status = {"degraded": False, "daemon": {"idle": False, "pid": 123}}
    result = capacity_gate.evaluate_gate(tool_name="Task", tool_input={}, status=status)
    assert result["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_evaluate_gate_idle_allows() -> None:
    status = {"degraded": False, "daemon": {"idle": True, "pid": 123}}
    result = capacity_gate.evaluate_gate(tool_name="Task", tool_input={}, status=status)
    assert "hookSpecificOutput" not in result
    assert result == {}


def test_evaluate_gate_degraded_status_treated_busy() -> None:
    # degraded 代表「不知道 daemon 是否忙碌」，須保守擋，不可靜默放行。
    status = {"degraded": True}
    result = capacity_gate.evaluate_gate(tool_name="Task", tool_input={}, status=status)
    assert result["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_non_gated_tool_ignores_busy_status() -> None:
    status = {"degraded": False, "daemon": {"idle": False, "pid": 123}}
    result = capacity_gate.evaluate_gate(tool_name="Read", tool_input={"file_path": "/tmp/x"}, status=status)
    assert result == {}


def test_evaluate_gate_headless_bash_busy_returns_ask() -> None:
    status = {"degraded": False, "daemon": {"idle": False}}
    result = capacity_gate.evaluate_gate(
        tool_name="Bash", tool_input={"command": "codex exec 'do work'"}, status=status
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_evaluate_gate_plain_bash_busy_still_allows() -> None:
    status = {"degraded": False, "daemon": {"idle": False}}
    result = capacity_gate.evaluate_gate(tool_name="Bash", tool_input={"command": "ls -la"}, status=status)
    assert result == {}


# ---------------------------------------------------------------------------
# CLI: `cortex capacity-gate check`
# ---------------------------------------------------------------------------


def test_cli_reads_status_path_override_busy(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps({"degraded": False, "daemon": {"idle": False, "pid": 1}}),
        encoding="utf-8",
    )
    stdin_payload = json.dumps({"tool_name": "Task", "tool_input": {}})

    code, out = _run_cli(
        ["capacity-gate", "check", "--status-path", str(status_path)], stdin_payload
    )

    assert code == 0
    decision = json.loads(out)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_cli_reads_status_path_override_idle(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps({"degraded": False, "daemon": {"idle": True, "pid": 1}}),
        encoding="utf-8",
    )
    stdin_payload = json.dumps({"tool_name": "Task", "tool_input": {}})

    code, out = _run_cli(
        ["capacity-gate", "check", "--status-path", str(status_path)], stdin_payload
    )

    assert code == 0
    decision = json.loads(out)
    assert decision == {}


def test_cli_missing_status_path_treated_degraded_and_asks(tmp_path: Path) -> None:
    # fixture 檔案不存在時（例如 status.json 尚未寫出），CLI 必須視為 degraded
    # 並保守 ask，而不是靜默放行。
    missing_path = tmp_path / "does-not-exist.json"
    stdin_payload = json.dumps({"tool_name": "Task", "tool_input": {}})

    code, out = _run_cli(
        ["capacity-gate", "check", "--status-path", str(missing_path)], stdin_payload
    )

    assert code == 0
    decision = json.loads(out)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_cli_non_gated_tool_allows_even_when_busy(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps({"degraded": False, "daemon": {"idle": False, "pid": 1}}),
        encoding="utf-8",
    )
    stdin_payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}})

    code, out = _run_cli(
        ["capacity-gate", "check", "--status-path", str(status_path)], stdin_payload
    )

    assert code == 0
    assert json.loads(out) == {}
