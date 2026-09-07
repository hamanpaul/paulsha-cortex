from __future__ import annotations

import subprocess

import pytest

import paulsha_cortex.coordinator.launcher as launcher_module
from paulsha_cortex.coordinator.launcher import SubprocessLauncher


@pytest.mark.parametrize(
    ("executor", "expected_stdin"),
    (
        ("copilot", False),
        ("claude", True),
        ("codex", False),
        ("agy", False),
        ("cg", False),
    ),
)
def test_headless_popen_kwargs_include_new_session_without_dropping_existing_io(
    executor: str,
    expected_stdin: bool,
) -> None:
    build_kwargs = getattr(launcher_module, "build_headless_popen_kwargs", None)
    assert callable(build_kwargs), "launcher must expose the shared Popen kwargs helper"

    env = {"PATH": "/usr/bin", "PSC_JOB_ID": "session-red"}
    kwargs = build_kwargs(cwd="/tmp/worktree", env=env, executor=executor)

    assert kwargs["cwd"] == "/tmp/worktree"
    assert kwargs["env"] is env
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["start_new_session"] is True
    if expected_stdin:
        assert kwargs["stdin"] is subprocess.PIPE
    else:
        assert "stdin" not in kwargs


@pytest.mark.parametrize(
    ("executor", "launcher_kwargs"),
    (
        ("copilot", {}),
        ("claude", {}),
        ("codex", {}),
        ("agy", {}),
        ("cg", {"read_only": True}),
    ),
)
def test_every_direct_headless_executor_records_start_new_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    executor: str,
    launcher_kwargs: dict[str, object],
) -> None:
    calls: list[dict[str, object]] = []

    class FakeProcess:
        pid = 12345

    def fake_popen(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return FakeProcess()

    monkeypatch.setenv("PSC_JOB_RUNNER", "direct")
    monkeypatch.setattr(launcher_module.subprocess, "Popen", fake_popen)

    SubprocessLauncher(executor, **launcher_kwargs).launch(
        slice_id=f"session-red-{executor}",
        prompt="PROMPT",
        worktree=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
    )

    assert len(calls) == 1
    assert calls[0]["start_new_session"] is True
    if executor == "claude":
        assert calls[0]["stdin"] is subprocess.PIPE
