from __future__ import annotations

import shlex

import pytest

import paulsha_cortex.coordinator.launcher as launcher_module
from paulsha_cortex.coordinator.launcher import SubprocessLauncher, build_agy_argv
from paulsha_cortex.coordinator.model_identities import AGY_MODEL_ID, load_model_identities


def test_agy_argv_is_headless_plan_sandbox_and_keeps_prompt_single() -> None:
    argv = build_agy_argv(
        prompt="first line\nsecond line",
        slice_id="plan-demo",
        log_dir="/tmp/logs",
        model="Gemini 3.1 Pro (High)",
        read_only=True,
    )

    assert argv == [
        "agy",
        "--print",
        "first line\nsecond line",
        "--mode",
        "plan",
        "--sandbox",
        "--model",
        "Gemini 3.1 Pro (High)",
    ]
    assert "--dangerously-skip-permissions" not in argv


def test_agy_reviewer_argv_grants_only_the_disposable_checkout() -> None:
    argv = build_agy_argv(
        prompt="inspect",
        slice_id="verify-demo",
        log_dir="/tmp/logs",
        worktree="/tmp/reviewer-checkout",
        review_only=True,
    )

    assert argv[0:6] == [
        "agy",
        "--print",
        "inspect",
        "--mode",
        "plan",
        "--sandbox",
    ]
    assert argv[6:8] == ["--add-dir", "/tmp/reviewer-checkout"]
    assert "--dangerously-skip-permissions" not in argv


def test_agy_builder_argv_uses_accept_edits_and_scopes_worktree() -> None:
    argv = build_agy_argv(
        prompt="implement",
        slice_id="build-demo",
        log_dir="/tmp/logs",
        worktree="/tmp/builder-checkout",
    )

    assert argv[:5] == [
        "agy",
        "--print",
        "implement",
        "--mode",
        "accept-edits",
    ]
    assert argv[5:7] == ["--add-dir", "/tmp/builder-checkout"]
    assert "--sandbox" not in argv
    assert "--dangerously-skip-permissions" not in argv


def test_agy_builder_unsafe_argv_adds_permission_bypass() -> None:
    argv = build_agy_argv(
        prompt="implement",
        slice_id="build-demo",
        log_dir="/tmp/logs",
        worktree="/tmp/builder-checkout",
        allow_unsafe=True,
    )

    assert argv[:5] == [
        "agy",
        "--print",
        "implement",
        "--mode",
        "accept-edits",
    ]
    assert argv[5:7] == ["--add-dir", "/tmp/builder-checkout"]
    assert "--sandbox" not in argv
    assert "--dangerously-skip-permissions" in argv


def test_agy_launcher_accepts_explicit_unsafe_builder_mode() -> None:
    launcher = SubprocessLauncher(executor="agy", allow_unsafe=True)

    assert launcher.executor == "agy"


def test_agy_registry_build_capability_matches_writable_launcher_shape(tmp_path) -> None:
    registry = load_model_identities(tmp_path, use_packaged_default=True)
    identity = registry.require("agy", AGY_MODEL_ID)
    worktree = tmp_path / "builder-checkout"
    argv = build_agy_argv(
        prompt="implement",
        slice_id="build-demo",
        log_dir="/tmp/logs",
        worktree=str(worktree),
    )

    registry_declares_build = "build" in identity.capabilities
    expected_mode = "accept-edits" if registry_declares_build else "plan"
    expected_sandbox = not registry_declares_build
    actual_mode = argv[argv.index("--mode") + 1]
    actual_worktree = argv[argv.index("--add-dir") + 1]
    launcher_is_writable = (
        actual_mode == "accept-edits"
        and "--sandbox" not in argv
        and actual_worktree == str(worktree.resolve())
    )
    assert registry_declares_build
    assert actual_mode == expected_mode
    assert ("--sandbox" in argv) is expected_sandbox
    assert registry_declares_build is launcher_is_writable


@pytest.mark.parametrize(
    "builder_options",
    ({}, {"allow_unsafe": True}, {"commit_required": True}),
    ids=("default", "unsafe", "commit-required"),
)
def test_agy_builder_requires_a_worktree(builder_options) -> None:
    with pytest.raises(ValueError, match="agy builder requires a worktree"):
        build_agy_argv(
            prompt="implement",
            slice_id="build-demo",
            log_dir="/tmp/logs",
            **builder_options,
        )


def test_agy_commit_required_argv_adds_linked_git_write_dirs(monkeypatch, tmp_path) -> None:
    git_write_dirs = (
        "/tmp/worktree-git",
        "/tmp/common-objects",
        "/tmp/refs/heads",
        "/tmp/logs/refs/heads",
    )
    monkeypatch.setattr(
        launcher_module,
        "_linked_worktree_git_write_dirs",
        lambda worktree: git_write_dirs,
    )
    worktree = tmp_path / "builder-checkout"
    argv = build_agy_argv(
        prompt="implement",
        slice_id="build-demo",
        log_dir="/tmp/logs",
        worktree=str(worktree),
        commit_required=True,
    )

    add_dirs = [
        argv[index + 1]
        for index, value in enumerate(argv)
        if value == "--add-dir"
    ]
    assert add_dirs == [str(worktree.resolve()), *git_write_dirs]


def test_agy_launcher_forwards_commit_required_to_argv_builder(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    def fake_builder(**kwargs):
        calls.append(kwargs)
        return ["agy"]

    class FakeProcess:
        pid = 123

    monkeypatch.setitem(launcher_module._ARGV_BUILDERS, "agy", fake_builder)
    monkeypatch.setattr(
        launcher_module.subprocess,
        "Popen",
        lambda argv, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        launcher_module.job_workspace,
        "prepare_commit_spool",
        lambda **kwargs: tmp_path / "commit.bundle",
    )
    monkeypatch.setenv("PSC_JOB_RUNNER", "direct")

    SubprocessLauncher("agy").as_commit_required().launch(
        slice_id="agy-commit",
        prompt="implement",
        worktree=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
    )

    assert calls and calls[0]["commit_required"] is True


def test_agy_commit_required_launcher_emits_real_scoped_git_dirs(monkeypatch, tmp_path) -> None:
    git_write_dirs = (
        str(tmp_path / "worktree-git"),
        str(tmp_path / "common-objects"),
        str(tmp_path / "refs"),
        str(tmp_path / "logs-refs"),
    )
    calls: list[list[str]] = []

    class FakeProcess:
        pid = 124

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return FakeProcess()

    monkeypatch.setattr(
        launcher_module,
        "_linked_worktree_git_write_dirs",
        lambda worktree: git_write_dirs,
    )
    monkeypatch.setattr(launcher_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        launcher_module.job_workspace,
        "prepare_commit_spool",
        lambda **kwargs: tmp_path / "commit.bundle",
    )
    monkeypatch.setenv("PSC_JOB_RUNNER", "direct")

    SubprocessLauncher("agy", commit_required=True).launch(
        slice_id="agy-commit-real-builder",
        prompt="implement",
        worktree=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
    )

    assert calls
    inner_argv = shlex.split(calls[0][2].split(";", 1)[0])
    add_dirs = [
        inner_argv[index + 1]
        for index, value in enumerate(inner_argv)
        if value == "--add-dir"
    ]
    assert inner_argv[:5] == [
        "agy",
        "--print",
        "implement",
        "--mode",
        "accept-edits",
    ]
    assert add_dirs == [str(tmp_path.resolve()), *git_write_dirs]
    assert "--sandbox" not in inner_argv
