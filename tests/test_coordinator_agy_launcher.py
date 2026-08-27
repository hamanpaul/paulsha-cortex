from __future__ import annotations

from paulsha_cortex.coordinator.launcher import SubprocessLauncher, build_agy_argv


def test_agy_argv_is_headless_plan_sandbox_and_keeps_prompt_single() -> None:
    argv = build_agy_argv(
        prompt="first line\nsecond line",
        slice_id="plan-demo",
        log_dir="/tmp/logs",
        model="Gemini 3.1 Pro (High)",
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
