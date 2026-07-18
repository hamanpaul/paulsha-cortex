from __future__ import annotations

import pytest

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


def test_agy_launcher_refuses_unsafe_mode_instead_of_silently_bypassing() -> None:
    with pytest.raises(ValueError, match="agy.*unsafe"):
        build_agy_argv(
            prompt="P",
            slice_id="s",
            log_dir="/tmp/logs",
            allow_unsafe=True,
        )
    with pytest.raises(ValueError, match="agy.*unsafe"):
        SubprocessLauncher("agy", allow_unsafe=True)
