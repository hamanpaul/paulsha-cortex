from __future__ import annotations

import json
import io
from pathlib import Path

import pytest

from paulsha_cortex import command_policy_hook
from paulsha_cortex.coordinator import dispatcher, launcher


@pytest.mark.parametrize(
    ("executor", "payload"),
    [
        ("codex", {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}),
        ("copilot", {"toolName": "shell", "toolArgs": {"command": "rm -rf /"}}),
        (
            "agy",
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf /"}}},
        ),
        ("claude", {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}),
    ],
)
def test_executor_hook_protocol_denies_and_audits_critical_command(
    executor, payload, tmp_path
) -> None:
    event_path = tmp_path / f"{executor}.jsonl"
    output, exit_code, event, terminate = command_policy_hook.process_hook_input(
        json.dumps(payload),
        executor=executor,
        environ={"PSC_JOB_ID": "job-1", "PSC_COMMAND_POLICY_EVENTS": str(event_path)},
        terminate_job=True,
    )

    assert exit_code == 0
    assert output
    assert event is not None
    assert event["rule_id"] == "protected-root-destruction"
    assert event["severity"] == "critical"
    assert event["argv"] == ["rm", "-rf", "/"]
    assert len(event["policy_sha256"]) == 64
    assert terminate
    saved = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])
    assert saved == event


def test_hook_is_noop_without_manager_job_marker(tmp_path) -> None:
    output, exit_code, event, terminate = command_policy_hook.process_hook_input(
        '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}',
        executor="codex",
        environ={"PSC_COMMAND_POLICY_EVENTS": str(tmp_path / "events.jsonl")},
        terminate_job=True,
    )

    assert (output, exit_code, event, terminate) == ({}, 0, None, False)


def test_allowed_shell_command_is_not_audited(tmp_path) -> None:
    event_path = tmp_path / "events.jsonl"
    output, exit_code, event, terminate = command_policy_hook.process_hook_input(
        json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo 'rm -rf /'"},
        }),
        executor="claude",
        environ={"PSC_JOB_ID": "job-2", "PSC_COMMAND_POLICY_EVENTS": str(event_path)},
        terminate_job=True,
    )

    assert (output, exit_code, event, terminate) == ({}, 0, None, False)
    assert not event_path.exists()


def test_audit_write_failure_denies_tool_call(tmp_path) -> None:
    output, exit_code, event, terminate = command_policy_hook.process_hook_input(
        '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}',
        executor="codex",
        environ={"PSC_JOB_ID": "job-3", "PSC_COMMAND_POLICY_EVENTS": ""},
    )

    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert exit_code == 2
    assert event is None
    assert not terminate


def test_third_high_severity_event_terminates_job(tmp_path) -> None:
    event_path = tmp_path / "events.jsonl"
    payload = '{"tool_name":"Bash","tool_input":{"command":"gh repo delete o/r --yes"}}'
    results = [
        command_policy_hook.process_hook_input(
            payload,
            executor="codex",
            environ={"PSC_JOB_ID": "job-4", "PSC_COMMAND_POLICY_EVENTS": str(event_path)},
            terminate_job=True,
        )
        for _ in range(3)
    ]

    assert [result[3] for result in results] == [False, False, True]
    assert len(event_path.read_text(encoding="utf-8").splitlines()) == 3


def test_hook_main_terminates_executor_process_group_after_critical_hit(
    tmp_path, monkeypatch
) -> None:
    stdin = io.TextIOWrapper(
        io.BytesIO(b'{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}')
    )
    event_path = tmp_path / "events.jsonl"
    monkeypatch.setattr(command_policy_hook.sys, "stdin", stdin)
    monkeypatch.setattr(
        command_policy_hook.os,
        "environ",
        {
            "PSC_JOB_ID": "job-kill",
            "PSC_COMMAND_POLICY_EVENTS": str(event_path),
        },
    )
    killed = []
    monkeypatch.setattr(command_policy_hook.os, "getpgrp", lambda: 123)
    monkeypatch.setattr(command_policy_hook.os, "killpg", lambda group, sig: killed.append((group, sig)))

    assert command_policy_hook.main(["--executor", "claude"]) == 0
    assert killed == [(123, command_policy_hook.signal.SIGTERM)]


def test_policy_runtime_config_is_job_scoped_and_cleaned(tmp_path) -> None:
    copilot_home = tmp_path / "copilot"
    copilot_path = launcher._install_copilot_command_policy_hook(
        {"COPILOT_HOME": str(copilot_home)}, "job-copilot"
    )
    copilot_payload = json.loads(Path(copilot_path).read_text(encoding="utf-8"))
    assert copilot_payload["hooks"]["preToolUse"][0]["exec"] == "cortex"
    assert copilot_payload["hooks"]["preToolUse"][0]["args"] == [
        "command-policy-hook", "--executor", "copilot"
    ]
    launcher._cleanup_command_policy_runtime("copilot", copilot_path)
    assert not Path(copilot_path).exists()

    agy_home = tmp_path / "agy-home"
    agy_home.mkdir()
    agy_path = launcher._install_agy_command_policy_plugin(
        {"HOME": str(agy_home)}, "job-agy"
    )
    agy_second_path = launcher._install_agy_command_policy_plugin(
        {"HOME": str(agy_home)}, "job-agy"
    )
    assert agy_second_path != agy_path
    plugin = Path(agy_path)
    manifest = json.loads((plugin / "plugin.json").read_text(encoding="utf-8"))
    hooks = json.loads((plugin / "hooks.json").read_text(encoding="utf-8"))
    assert manifest["name"] == plugin.name
    assert hooks["cortex-command-policy"]["PreToolUse"][0]["hooks"][0]["command"] == (
        "cortex command-policy-hook --executor agy"
    )
    launcher._cleanup_command_policy_runtime("agy", agy_path)
    launcher._cleanup_command_policy_runtime("agy", agy_second_path)
    assert not plugin.exists()
    assert not Path(agy_second_path).exists()


def test_executor_policy_matrix_covers_argv_builders_and_required_hook_wiring() -> None:
    assert set(launcher.EXECUTOR_POLICY_ENFORCEMENT) == set(launcher._ARGV_BUILDERS)
    assert all(
        set(roles) == {"planner", "builder", "reviewer"}
        for roles in launcher.EXECUTOR_POLICY_ENFORCEMENT.values()
    )
    for executor, role in (
        ("codex", "builder"),
        ("copilot", "builder"),
        ("agy", "builder"),
        ("claude", "reviewer"),
    ):
        assert launcher.EXECUTOR_POLICY_ENFORCEMENT[executor][role] == "unsupported-measured"

    codex_argv = launcher.build_codex_argv(
        prompt="P", slice_id="j", log_dir="/logs", worktree="/tmp/worktree"
    )
    codex_override = codex_argv[codex_argv.index("-c") + 1]
    assert "hooks.PreToolUse" in codex_override
    assert "command-policy-hook --executor codex" in codex_override

    copilot_argv = launcher.build_copilot_argv(
        prompt="P", slice_id="j", log_dir="/logs", worktree="/tmp/worktree"
    )
    assert "--deny-tool" in copilot_argv
    assert "shell(passwd:*)" in copilot_argv

    claude_builder_argv = launcher.build_claude_argv(
        prompt="P", slice_id="j", log_dir="/logs", worktree="/tmp/worktree"
    )
    builder_settings = json.loads(
        claude_builder_argv[claude_builder_argv.index("--settings") + 1]
    )
    claude_builder_hooks = builder_settings["hooks"]["PreToolUse"]
    claude_bash_hook = next(item for item in claude_builder_hooks if item["matcher"] == "Bash")
    assert "command-policy-hook --executor claude" in claude_bash_hook["hooks"][0]["command"]

    claude_review_argv = launcher.build_claude_argv(
        prompt="P",
        slice_id="j",
        log_dir="/logs",
        worktree="/tmp/worktree",
        review_only=True,
        review_terminal_kind="workflow-review-result",
    )
    review_settings = json.loads(
        claude_review_argv[claude_review_argv.index("--settings") + 1]
    )
    assert review_settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert "command-policy-hook --executor claude" in review_settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_packaged_agy_hook_asset_matches_versioned_source() -> None:
    source = Path("coordinator/data/command-policy-agy-hooks.json")
    packaged = Path("paulsha_cortex/coordinator/data/command-policy-agy-hooks.json")
    assert packaged.read_bytes() == source.read_bytes()


def test_dispatcher_treats_symlinked_command_policy_audit_as_integrity_failure(tmp_path) -> None:
    log_path = tmp_path / "job.jsonl"
    log_path.touch()
    sidecar = log_path.with_suffix(".command-policy.jsonl")
    secret = tmp_path / "secret.jsonl"
    secret.write_text("not an audit event\n", encoding="utf-8")
    sidecar.symlink_to(secret)

    summary = dispatcher._command_policy_event_summary(
        str(log_path), job_id="job-1", executor="codex"
    )

    assert summary is not None
    assert summary["latest"]["rule_id"] == "command-policy-audit-integrity"
    assert summary["latest"]["severity"] == "critical"


def test_audit_redacts_bearer_headers_and_secret_values() -> None:
    provider_token = "ghp_" + "1234567890" + "abcdefghijkl"
    argv = (
        "gh", "api", "-H", "Authorization: Bearer abcdefghijklmnop",
        "--token", provider_token,
    )

    redacted = command_policy_hook._redact_argv(argv)

    assert "abcdefghijklmnop" not in " ".join(redacted)
    assert provider_token not in " ".join(redacted)
    assert redacted[-2:] == ["--token", "[REDACTED]"]
