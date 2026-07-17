from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from paulsha_cortex.coordinator.preflight import (
    FullSuiteEvidence,
    PreflightRequest,
    build_preflight_argv,
    load_preflight_command,
    run_preflight,
)


HEAD = "a" * 40


def test_initial_and_existing_pr_preflight_argv() -> None:
    command = ("/opt/tools/preflight.sh",)
    initial = build_preflight_argv(
        command=command,
        request=PreflightRequest(metadata_path="/tmp/pr.json"),
    )
    existing = build_preflight_argv(
        command=command,
        request=PreflightRequest(pr_number=15),
    )
    assert initial == ["/opt/tools/preflight.sh", "--metadata", "/tmp/pr.json"]
    assert existing == ["/opt/tools/preflight.sh", "--pr", "15"]


def test_load_preflight_command_is_typed_and_requires_executable(tmp_path: Path) -> None:
    executable = tmp_path / "preflight"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    env = {"PSC_PREFLIGHT_CMD": f"{executable} --strict"}
    assert load_preflight_command(env=env) == (str(executable), "--strict")
    with pytest.raises(ValueError, match="executable"):
        load_preflight_command(env={"PSC_PREFLIGHT_CMD": str(tmp_path / "missing")})


def test_skip_tests_requires_recent_exact_tree_evidence() -> None:
    request = PreflightRequest(
        pr_number=15,
        skip_tests=True,
        tree_hash=HEAD,
        now_epoch=1_000,
        full_suite=FullSuiteEvidence(tree_hash=HEAD, passed=True, completed_at_epoch=950),
    )
    assert build_preflight_argv(command=("preflight",), request=request)[-1] == "--skip-tests"
    with pytest.raises(ValueError, match="exact current tree"):
        build_preflight_argv(
            command=("preflight",),
            request=replace(
                request,
                full_suite=FullSuiteEvidence(
                    tree_hash="b" * 40,
                    passed=True,
                    completed_at_epoch=950,
                ),
            ),
        )
    with pytest.raises(ValueError, match="stale"):
        build_preflight_argv(
            command=("preflight",),
            request=replace(
                request,
                full_suite=FullSuiteEvidence(
                    tree_hash=HEAD,
                    passed=True,
                    completed_at_epoch=1,
                ),
            ),
        )


def test_run_preflight_runs_quick_policy_then_ci_parity_without_shell(tmp_path: Path) -> None:
    executable = tmp_path / "preflight"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    calls: list[dict[str, object]] = []

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def runner(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        return Result()

    result = run_preflight(
        repo_root=tmp_path,
        command=(str(executable),),
        request=PreflightRequest(pr_number=15),
        runner=runner,
    )
    assert result.passed
    assert calls[0]["argv"] == ["python3", "-m", "policy_check", "--repo", "."]
    assert calls[1]["argv"] == [str(executable), "--pr", "15"]
    assert calls[0]["shell"] is False
    assert calls[1]["shell"] is False


def test_run_preflight_stops_after_failed_quick_policy(tmp_path: Path) -> None:
    calls = []

    class Result:
        returncode = 1
        stdout = ""
        stderr = "failed"

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return Result()

    result = run_preflight(
        repo_root=tmp_path,
        command=("preflight",),
        request=PreflightRequest(pr_number=15),
        runner=runner,
    )
    assert not result.passed
    assert result.failed_stage == "policy"
    assert len(calls) == 1
