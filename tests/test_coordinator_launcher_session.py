from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

import paulsha_cortex.coordinator.launcher as launcher_module
from paulsha_cortex.coordinator.launcher import SubprocessLauncher


_MATRIX_CASES = tuple(
    (runner, executor)
    for runner in (
        "direct",
        "systemd-run",
        "systemd-template",
    )
    for executor in ("copilot", "claude", "codex", "agy", "cg")
    if not (runner == "systemd-template" and executor == "cg")
)


def _patch_degraded_launch_seams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    runner: str,
    executor: str,
) -> None:
    """Keep the matrix on the production launcher path without live systemd."""

    monkeypatch.setenv("PSC_JOB_RUNNER", runner)
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("PSC_BUILDER_PATH", "/usr/bin")
    monkeypatch.setenv("PSC_REVIEWER_PATH", "/usr/bin")
    monkeypatch.setenv("PSC_GATE_PATH", "/usr/bin")
    monkeypatch.setenv("PSC_BUILDER_HOME", str(tmp_path / "builder-home"))
    monkeypatch.setenv("PSC_REVIEWER_HOME", str(tmp_path / "reviewer-home"))
    monkeypatch.setenv("PSC_GATE_HOME", str(tmp_path / "gate-home"))
    monkeypatch.setenv("PSC_JOB_SPEC_SPOOL", str(tmp_path / "builder-specs"))
    monkeypatch.setenv("PSC_REVIEW_JOB_SPEC_SPOOL", str(tmp_path / "review-specs"))
    monkeypatch.setenv("PSC_GATE_JOB_SPEC_SPOOL", str(tmp_path / "gate-specs"))

    def fake_account(_env, *, role):
        return f"{role}-account"

    def fake_group(_env, *, role):
        return f"{role}-group"

    monkeypatch.setattr(launcher_module.job_runner, "resolve_job_account", fake_account)
    monkeypatch.setattr(launcher_module.job_runner, "resolve_job_group", fake_group)
    monkeypatch.setattr(
        launcher_module.job_runner,
        "preflight_systemd_run",
        lambda **_kwargs: "/usr/bin/systemd-run",
    )
    monkeypatch.setattr(
        launcher_module.job_runner,
        "resolve_template_unit",
        lambda _env, *, role: "cortex-job@.service",
    )
    monkeypatch.setattr(
        launcher_module.job_runner,
        "resolve_job_shim",
        lambda _env: "/usr/bin/cortex-job-shim",
    )
    def principal_spool(_env, *, role):
        principal = "reviewer" if role == launcher_module.job_runner.JOB_ROLE_REVIEW else "builder"
        path = tmp_path / principal
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    monkeypatch.setattr(launcher_module.job_runner, "resolve_job_spec_spool", principal_spool)
    monkeypatch.setattr(
        launcher_module.job_runner,
        "preflight_systemd_template",
        lambda **_kwargs: "/usr/bin/systemctl",
    )
    monkeypatch.setattr(launcher_module.job_runner, "_unit_is_active", lambda *_args: False)
    monkeypatch.setattr(
        launcher_module.job_runner,
        "build_job_env",
        lambda **kwargs: {
            "PATH": "/usr/bin",
            "HOME": str(tmp_path),
            "PSC_JOB_ID": kwargs["job_id"],
            "PSC_SLICE_ID": kwargs["slice_id"],
            "PSC_REPO_ROOT": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        launcher_module.job_runner,
        "resolve_prompt_spec_spool",
        principal_spool,
    )
    monkeypatch.setattr(launcher_module.job_runner, "write_job_prompt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher_module.job_runner, "confirm_transient_unit_started", lambda **_kwargs: None)
    monkeypatch.setattr(launcher_module.job_runner, "confirm_template_instance_started", lambda **_kwargs: None)
    monkeypatch.setattr(
        launcher_module.job_runner,
        "ensure_workspace_reachable",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(launcher_module.job_runner, "write_job_spec", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        launcher_module.job_runner,
        "build_job_spec",
        lambda **kwargs: {
            "instance": kwargs["instance"],
            "unit": kwargs["unit"],
            "log_path": kwargs["log_path"],
        },
    )
    monkeypatch.setattr(
        launcher_module.spool_slot,
        "system_account_exists",
        lambda _account: False,
    )
    monkeypatch.setattr(
        launcher_module.spool_slot,
        "canonical_codex_controls",
        lambda *_args, **_kwargs: str(tmp_path / "codex-controls"),
    )
    monkeypatch.setattr(
        launcher_module.spool_slot,
        "provision_runtime_surfaces",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        launcher_module.spool_slot,
        "copilot_oauth_authority",
        lambda **_kwargs: tmp_path / "copilot-authority" / "config.json",
    )
    monkeypatch.setattr(
        launcher_module.spool_slot,
        "provision_copilot_home",
        lambda **_kwargs: tmp_path / "copilot-home",
    )
    monkeypatch.setattr(
        launcher_module.spool_slot,
        "preseed_job_writable_file",
        lambda path: Path(path).touch() or Path(path),
    )
    monkeypatch.setattr(
        launcher_module.job_workspace,
        "prepare_commit_spool",
        lambda **_kwargs: tmp_path / "commit-spool" / "commits.bundle",
    )

    for principal in ("builder", "reviewer"):
        (tmp_path / principal).mkdir(parents=True, exist_ok=True)

    if runner == "systemd-template":
        def fake_log_spool_dir(*, principal_id: str, spool_key: str):
            return tmp_path / "job-logs" / principal_id / spool_key

        def fake_prepare_job_log_spool(*, principal_id: str, spool_key: str, manager_log_path: str):
            path = fake_log_spool_dir(principal_id=principal_id, spool_key=spool_key)
            path.mkdir(parents=True, exist_ok=True)
            log_path = path / launcher_module.job_workspace.JOB_LOG_FILENAME
            log_path.touch()
            return log_path

        monkeypatch.setattr(launcher_module.job_workspace, "job_log_spool_dir", fake_log_spool_dir)
        monkeypatch.setattr(
            launcher_module.job_workspace,
            "prepare_job_log_spool",
            fake_prepare_job_log_spool,
        )


def _record_subprocess_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    runner: str,
    executor: str,
    require_session: bool = False,
) -> list[dict[str, object]]:
    _patch_degraded_launch_seams(
        monkeypatch,
        tmp_path,
        runner=runner,
        executor=executor,
    )
    calls: list[dict[str, object]] = []

    class FakeProcess:
        pid = 12347

    def fake_popen(argv, **kwargs):
        if require_session:
            assert kwargs.get("start_new_session") is True, "start_new_session flag was dropped"
        calls.append({"argv": argv, **kwargs})
        return FakeProcess()

    monkeypatch.setattr(launcher_module.subprocess, "Popen", fake_popen)
    launcher_kwargs = {"read_only": True} if executor == "cg" else {}
    SubprocessLauncher(executor, **launcher_kwargs).launch(
        slice_id=f"session-matrix-{runner}-{executor}",
        prompt="PROMPT",
        worktree=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
    )
    return calls


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


@pytest.mark.parametrize(("runner", "executor"), _MATRIX_CASES)
def test_every_admitted_runner_executor_records_start_new_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    runner: str,
    executor: str,
) -> None:
    calls = _record_subprocess_launch(
        monkeypatch,
        tmp_path,
        runner=runner,
        executor=executor,
    )

    assert len(calls) == 1
    assert calls[0]["start_new_session"] is True
    if runner == "direct":
        assert calls[0]["cwd"] == str(tmp_path)
        if executor == "claude":
            assert calls[0]["stdin"] is subprocess.PIPE
    else:
        assert calls[0]["argv"][:2] == ["bash", "-c"]
        assert calls[0]["stdin"] is subprocess.DEVNULL
        if runner == "systemd-run":
            assert "systemd-run" in calls[0]["argv"][2]
            assert calls[0]["cwd"] == str(tmp_path)
        else:
            assert "systemctl" in calls[0]["argv"][2]
            assert calls[0]["cwd"] is None


def test_recording_negative_control_catches_helper_wiring_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    real_helper = launcher_module.build_headless_popen_kwargs

    def helper_mutant(**kwargs):
        result = real_helper(**kwargs)
        result.pop("start_new_session")
        return result

    monkeypatch.setattr(launcher_module, "build_headless_popen_kwargs", helper_mutant)
    with pytest.raises(AssertionError, match="start_new_session"):
        _record_subprocess_launch(
            monkeypatch,
            tmp_path,
            runner="direct",
            executor="codex",
            require_session=True,
        )


@pytest.mark.parametrize(
    ("runner", "executor"),
    (("systemd-run", "codex"), ("systemd-template", "claude")),
)
def test_recording_negative_control_catches_single_runner_executor_flag_drop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    runner: str,
    executor: str,
) -> None:
    real_helper = launcher_module.build_headless_popen_kwargs

    def helper_mutant(**kwargs):
        result = real_helper(**kwargs)
        if kwargs["executor"] == executor:
            result.pop("start_new_session")
        return result

    monkeypatch.setattr(launcher_module, "build_headless_popen_kwargs", helper_mutant)
    with pytest.raises(AssertionError, match="start_new_session"):
        _record_subprocess_launch(
            monkeypatch,
            tmp_path,
            runner=runner,
            executor=executor,
            require_session=True,
        )


def test_cg_template_keeps_unknown_hardening_rejection_before_popen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _patch_degraded_launch_seams(
        monkeypatch,
        tmp_path,
        runner="systemd-template",
        executor="cg",
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        launcher_module.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append({"argv": argv, **kwargs}),
    )

    with pytest.raises(launcher_module.job_runner.JobRunnerError) as caught:
        SubprocessLauncher("cg", read_only=True).launch(
            slice_id="session-matrix-cg-template",
            prompt="PROMPT",
            worktree=str(tmp_path),
            log_dir=str(tmp_path / "logs"),
        )

    assert caught.value.diagnostic.reason == "job-runner-hardening-profile-unknown"
    assert calls == []


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
    try:
        child_pgid = os.getpgid(proc.pid)
        child_sid = os.getsid(proc.pid)
        assert child_pgid == proc.pid
        assert child_sid == proc.pid
        assert child_pgid != parent_pgid
        assert child_sid != parent_sid

        # Ownership is established before the only intentional group signal.
        os.killpg(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=5) == -signal.SIGTERM
        assert os.getpgid(0) == parent_pgid
        assert os.getsid(0) == parent_sid
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        finally:
            if getattr(proc, "stdin", None) is not None:
                proc.stdin.close()


@pytest.mark.skipif(os.name != "posix", reason="headless session isolation is POSIX-specific")
def test_missing_session_negative_control_rejects_before_group_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    kwargs = launcher_module.build_headless_popen_kwargs(
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin"},
        executor="codex",
    )
    kwargs.pop("start_new_session")
    kwargs["stdout"] = subprocess.DEVNULL
    parent_pgid = os.getpgid(0)
    parent_sid = os.getsid(0)
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )

    proc = subprocess.Popen(["bash", "-c", "exec sleep 30"], **kwargs)
    try:
        child_pgid = os.getpgid(proc.pid)
        child_sid = os.getsid(proc.pid)
        ownership = (
            child_pgid == proc.pid
            and child_sid == proc.pid
            and child_pgid != parent_pgid
            and child_sid != parent_sid
        )
        assert not ownership
        assert killpg_calls == []
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        finally:
            if getattr(proc, "stdin", None) is not None:
                proc.stdin.close()
    assert proc.poll() is not None
