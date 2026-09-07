from __future__ import annotations

import os
import signal
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


def test_stdin_compatibility_retry_keeps_session_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeProcess:
        pid = 12346

    def fake_popen(argv, **kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            raise TypeError("stdin is not supported by this fake")
        return FakeProcess()

    monkeypatch.setenv("PSC_JOB_RUNNER", "direct")
    monkeypatch.setattr(launcher_module.subprocess, "Popen", fake_popen)

    SubprocessLauncher("claude").launch(
        slice_id="session-retry",
        prompt="PROMPT",
        worktree=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
    )

    assert len(calls) == 2
    assert calls[0]["start_new_session"] is True
    assert calls[1]["start_new_session"] is True
    assert calls[0]["cwd"] == calls[1]["cwd"]
    assert calls[0]["env"] is calls[1]["env"]
    assert calls[0]["stderr"] is calls[1]["stderr"]
    assert calls[0]["stdin"] is subprocess.PIPE
    assert "stdin" not in calls[1]


@pytest.mark.parametrize("message", ("start_new_session is unsupported", "other failure"))
def test_non_stdin_popen_typeerror_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    message: str,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_popen(argv, **kwargs):
        calls.append(dict(kwargs))
        raise TypeError(message)

    monkeypatch.setenv("PSC_JOB_RUNNER", "direct")
    monkeypatch.setattr(launcher_module.subprocess, "Popen", fake_popen)

    with pytest.raises(TypeError, match=message):
        SubprocessLauncher("codex").launch(
            slice_id="session-no-retry",
            prompt="PROMPT",
            worktree=str(tmp_path),
            log_dir=str(tmp_path / "logs"),
        )

    assert len(calls) == 1
    assert calls[0]["start_new_session"] is True


def test_second_stdin_retry_error_is_propagated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_popen(argv, **kwargs):
        calls.append(dict(kwargs))
        raise TypeError("stdin remains unsupported")

    monkeypatch.setenv("PSC_JOB_RUNNER", "direct")
    monkeypatch.setattr(launcher_module.subprocess, "Popen", fake_popen)

    with pytest.raises(TypeError, match="stdin remains unsupported"):
        SubprocessLauncher("claude").launch(
            slice_id="session-double-error",
            prompt="PROMPT",
            worktree=str(tmp_path),
            log_dir=str(tmp_path / "logs"),
        )

    assert len(calls) == 2
    assert all(call["start_new_session"] is True for call in calls)
    assert calls[0]["stdin"] is subprocess.PIPE
    assert "stdin" not in calls[1]


@pytest.mark.skipif(os.name != "posix", reason="headless session isolation is POSIX-specific")
def test_headless_session_owns_its_process_group_before_group_signal(tmp_path) -> None:
    build_kwargs = launcher_module.build_headless_popen_kwargs
    kwargs = build_kwargs(
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin"},
        executor="codex",
    )
    kwargs["stdout"] = subprocess.DEVNULL

    parent_pgid = os.getpgid(0)
    parent_sid = os.getsid(0)
    proc = subprocess.Popen(["bash", "-c", "exec sleep 30"], **kwargs)
    group_owned = False
    try:
        child_pgid = os.getpgid(proc.pid)
        child_sid = os.getsid(proc.pid)
        assert child_pgid == proc.pid
        assert child_sid == proc.pid
        assert child_pgid != parent_pgid
        assert child_sid != parent_sid
        group_owned = True

        os.killpg(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=5) == -signal.SIGTERM
        assert os.getpgid(0) == parent_pgid
        assert os.getsid(0) == parent_sid
    finally:
        if proc.poll() is None:
            if group_owned:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=5)
        if getattr(proc, "stdin", None) is not None:
            proc.stdin.close()
