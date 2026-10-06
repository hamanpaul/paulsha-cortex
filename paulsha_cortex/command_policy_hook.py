"""Fail-closed PreToolUse adapter for the supported headless executors."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import command_policy

MAX_HOOK_INPUT_BYTES = 1024 * 1024
MAX_AUDIT_FILE_BYTES = 1024 * 1024
MAX_AUDIT_EVENTS = 256
MAX_POLICY_VIOLATIONS_PER_JOB = 3
_SECRET_KEY = re.compile(
    r"(?i)(?:token|password|secret|api[_-]?key|credential|authorization|bearer)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|sk-[A-Za-z0-9_-]{16,})"
)
_SECRET_VALUE_FLAGS = frozenset(
    {"--token", "--password", "--secret", "--api-key", "--access-token", "--authorization"}
)
_EXECUTORS = frozenset({"codex", "copilot", "agy", "claude"})
_SHELL_TOOL_NAMES = {
    "codex": frozenset({"bash", "exec_command", "shell"}),
    "copilot": frozenset({"bash", "shell", "powershell", "command"}),
    "agy": frozenset({"run_command", "command", "shell"}),
    "claude": frozenset({"bash", "shell"}),
}


def load_effective_policy(environ: Mapping[str, str] | None = None) -> command_policy.CommandPolicy:
    env = os.environ if environ is None else environ
    baseline = Path(
        env.get(
            "PSC_COMMAND_POLICY_BASELINE",
            str(Path(__file__).resolve().parent / "coordinator/data/command-policy.yaml"),
        )
    )
    base_text = _read_trusted_policy_file(baseline)
    overlay_path = env.get("PSC_COMMAND_POLICY_OVERLAY")
    overlay_text = _read_trusted_policy_file(Path(overlay_path)) if overlay_path else None
    return command_policy.load_policy_with_overlay(base_text, overlay_text)


def process_hook_input(
    raw: bytes | str,
    *,
    executor: str,
    environ: Mapping[str, str] | None = None,
    terminate_job: bool = False,
) -> tuple[dict[str, object], int, dict[str, object] | None, bool]:
    """Evaluate one executor hook payload; return output, exit code, audit event."""

    if executor not in _EXECUTORS:
        raise ValueError(f"unsupported command-policy executor: {executor}")
    env = os.environ if environ is None else environ
    # Hooks can be discovered from user/project configuration by an interactive
    # session. Without the launcher-owned job marker they are a strict no-op.
    if not env.get("PSC_JOB_ID"):
        return {}, 0, None, False
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return _render_block(executor, "command-policy hook input is malformed"), 2, None, False
    if not isinstance(payload, Mapping):
        return _render_block(executor, "command-policy hook input must be an object"), 2, None, False
    tool_name = _tool_name(payload)
    if tool_name.lower() not in _SHELL_TOOL_NAMES[executor]:
        return {}, 0, None, False
    command = _tool_command(payload)
    if not isinstance(command, str) or not command.strip():
        return _render_block(executor, "shell hook command is missing"), 2, None, False
    try:
        policy = load_effective_policy(env)
        decision = command_policy.evaluate_command(command, policy=policy)
    except (OSError, ValueError, command_policy.CommandPolicyError) as exc:
        return _render_block(executor, f"command-policy evaluation failed closed: {type(exc).__name__}"), 2, None, False
    if decision.allowed or decision.match is None:
        return {}, 0, None, False

    job_id = env.get("PSC_JOB_ID", "")
    event = {
        "schema": "cortex/command-policy-event/v1",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "job_id": job_id,
        "executor": executor,
        "rule_id": decision.match.rule_id,
        "category": decision.match.category,
        "severity": decision.match.severity,
        "argv": _redact_argv(decision.match.argv),
        "policy_sha256": command_policy.policy_sha256(policy),
    }
    try:
        event_count = _append_audit_event(env.get("PSC_COMMAND_POLICY_EVENTS", ""), event)
    except OSError as exc:
        return (
            _render_block(executor, f"command-policy audit failed closed: {type(exc).__name__}"),
            2,
            None,
            False,
        )
    reason = f"blocked by command policy rule {decision.match.rule_id}"
    should_terminate = (
        decision.match.severity == "critical"
        or event_count >= MAX_POLICY_VIOLATIONS_PER_JOB
    )
    output = _render_block(executor, reason)
    return output, 0, event, should_terminate and terminate_job


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--executor" or args[1] not in _EXECUTORS:
        sys.stderr.write("usage: cortex command-policy-hook --executor <codex|copilot|agy|claude>\n")
        return 2
    raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
    if len(raw) > MAX_HOOK_INPUT_BYTES:
        result = _render_block(args[1], "command-policy hook input exceeds size limit")
        sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
        return 2
    result, exit_code, _event, terminate = process_hook_input(
        raw,
        executor=args[1],
        terminate_job=True,
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    if terminate:
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except OSError:
            pass
    return exit_code


def _read_trusted_policy_file(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("policy file must be a regular non-symlink file")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise command_policy.CommandPolicyError(f"policy file unavailable: {path.name}") from exc


def _tool_name(payload: Mapping[str, Any]) -> str:
    value = payload.get("tool_name", payload.get("toolName"))
    if isinstance(value, str):
        return value
    call = payload.get("toolCall")
    if isinstance(call, Mapping) and isinstance(call.get("name"), str):
        return str(call["name"])
    return ""


def _tool_command(payload: Mapping[str, Any]) -> str | None:
    for key in ("tool_input", "toolInput", "toolArgs"):
        value = payload.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return None
        if isinstance(value, Mapping):
            for command_key in ("command", "cmd", "CommandLine", "commandLine"):
                command = value.get(command_key)
                if isinstance(command, str):
                    return command
    call = payload.get("toolCall")
    if isinstance(call, Mapping):
        value = call.get("args")
        if isinstance(value, Mapping):
            for command_key in ("command", "cmd", "CommandLine", "commandLine"):
                command = value.get(command_key)
                if isinstance(command, str):
                    return command
    return None


def _render_block(executor: str, reason: str) -> dict[str, object]:
    if executor in {"codex", "claude"}:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    if executor == "copilot":
        return {"permissionDecision": "deny", "permissionDecisionReason": reason}
    return {"decision": "deny", "reason": reason}


def _append_audit_event(path_text: str, event: Mapping[str, object]) -> int:
    if not path_text:
        raise OSError("command-policy audit path is missing")
    path = Path(path_text)
    if not path.is_absolute() or path.parent.is_symlink():
        raise OSError("command-policy audit path is invalid")
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_AUDIT_FILE_BYTES:
            raise OSError("command-policy audit file is not a bounded regular file")
        with os.fdopen(descriptor, "a+", encoding="utf-8", closefd=False) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_AUDIT_FILE_BYTES:
                raise OSError("command-policy audit file is not a bounded regular file")
            handle.write(json.dumps(dict(event), sort_keys=True, ensure_ascii=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            count = _count_audit_events(handle)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return count
    finally:
        os.close(descriptor)


def _count_audit_events(handle) -> int:
    handle.seek(0)
    count = 0
    total = 0
    for line in handle:
        total += len(line.encode("utf-8"))
        if total > MAX_AUDIT_FILE_BYTES or count >= MAX_AUDIT_EVENTS:
            return MAX_POLICY_VIOLATIONS_PER_JOB
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return MAX_POLICY_VIOLATIONS_PER_JOB
        if not isinstance(row, Mapping) or row.get("schema") != "cortex/command-policy-event/v1":
            return MAX_POLICY_VIOLATIONS_PER_JOB
        count += 1
    return count


def _redact_argv(argv: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    redact_next = False
    home = str(Path.home())
    for original in argv[:64]:
        token = original[:512]
        if redact_next:
            result.append("[REDACTED]")
            redact_next = False
            continue
        if token in _SECRET_VALUE_FLAGS:
            result.append(token)
            redact_next = True
            continue
        if _SECRET_VALUE.search(token):
            result.append("[REDACTED]")
            continue
        if _SECRET_KEY.search(token):
            result.append(
                token.split("=", 1)[0] + "=[REDACTED]"
                if "=" in token
                else "[REDACTED]"
            )
            continue
        token = token.replace(home, "$HOME") if home else token
        result.append(token)
    if len(argv) > 64:
        result.append("[TRUNCATED]")
    return result
