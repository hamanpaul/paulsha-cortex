from __future__ import annotations

import os
import json
from dataclasses import replace
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import verification
from paulsha_cortex.coordinator.preflight import (
    FULL_SUITE_EVIDENCE_SCHEMA,
    FullSuiteEvidence,
    PreflightRequest,
    build_preflight_argv,
    canonical_full_suite_evidence_path,
    load_preflight_command,
    load_full_suite_evidence,
    run_preflight,
    write_full_suite_evidence_after_run,
)


HEAD = "a" * 40
TREE = "b" * 40


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
    with pytest.raises(ValueError, match="shell wrapper"):
        load_preflight_command(env={"PSC_PREFLIGHT_CMD": "bash -c 'echo unsafe'"})
    with pytest.raises(ValueError, match="shell wrapper"):
        build_preflight_argv(
            command=("/usr/bin/env", "bash", "-c", "echo unsafe"),
            request=PreflightRequest(pr_number=15),
        )


def test_skip_tests_requires_recent_exact_tree_evidence() -> None:
    evidence = FullSuiteEvidence(
        schema=FULL_SUITE_EVIDENCE_SCHEMA,
        tree_hash=TREE,
        completed_at_epoch=950,
        command=("python3", "-m", "pytest"),
        evidence_hash="1" * 64,
    )
    request = PreflightRequest(
        pr_number=15,
        skip_tests=True,
        tree_hash=TREE,
    )
    assert build_preflight_argv(
        command=("preflight",),
        request=request,
        current_tree_hash=TREE,
        full_suite_evidence=evidence,
        now_epoch=1_000,
    )[-1] == "--skip-tests"
    with pytest.raises(ValueError, match="exact current tree"):
        build_preflight_argv(
            command=("preflight",),
            request=request,
            current_tree_hash="c" * 40,
            full_suite_evidence=evidence,
            now_epoch=1_000,
        )
    with pytest.raises(ValueError, match="stale"):
        build_preflight_argv(
            command=("preflight",),
            request=request,
            current_tree_hash=TREE,
            full_suite_evidence=replace(
                evidence,
                completed_at_epoch=1,
            ),
            now_epoch=1_000,
        )


def test_full_suite_evidence_is_created_only_by_actual_successful_run(tmp_path: Path) -> None:
    class Result:
        stderr = ""

        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def runner(argv, **kwargs):
        if "status" in argv:
            return Result("")
        if argv[-1] == "HEAD":
            return Result(HEAD)
        if argv[-1] == "HEAD^{tree}":
            return Result(TREE)
        return Result("tests passed", 0)

    evidence = write_full_suite_evidence_after_run(
        repo_root=tmp_path,
        command=("python3", "-m", "pytest"),
        runner=runner,
        now=lambda: 950,
        state_root=tmp_path / "state",
    )
    assert evidence.tree_hash == TREE
    path = canonical_full_suite_evidence_path(TREE, state_root=tmp_path / "state")
    assert path.stat().st_mode & 0o222 == 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.chmod(0o600)
    payload["payload"]["command"] = ["true"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o400)
    with pytest.raises(ValueError, match="hash mismatch"):
        load_full_suite_evidence(tree_hash=TREE, state_root=tmp_path / "state")

    def failing_runner(argv, **kwargs):
        if "status" in argv:
            return Result("")
        if argv[-1] == "HEAD":
            return Result(HEAD)
        if argv[-1] == "HEAD^{tree}":
            return Result(TREE)
        return Result("failed", 1)

    with pytest.raises(RuntimeError, match="command failed"):
        write_full_suite_evidence_after_run(
            repo_root=tmp_path,
            command=("python3", "-m", "pytest"),
            runner=failing_runner,
            now=lambda: 951,
            state_root=tmp_path / "failed-state",
        )
    assert not canonical_full_suite_evidence_path(
        TREE, state_root=tmp_path / "failed-state"
    ).exists()


def test_run_preflight_runs_quick_policy_then_ci_parity_without_shell(tmp_path: Path) -> None:
    executable = tmp_path / "preflight"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    calls: list[dict[str, object]] = []

    class Result:
        def __init__(self, stdout="ok", returncode=0):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def runner(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        if "status" in argv:
            return Result("")
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return Result(HEAD)
        if argv[-2:] == ["rev-parse", "HEAD^{tree}"]:
            return Result(TREE)
        return Result()

    result = run_preflight(
        repo_root=tmp_path,
        command=(str(executable),),
        request=PreflightRequest(pr_number=15),
        runner=runner,
    )
    assert result.passed
    assert calls[3]["argv"] == ["python3", "-m", "policy_check", "--repo", "."]
    assert calls[4]["argv"] == [str(executable), "--pr", "15"]
    assert result.head == HEAD
    assert result.tree_hash == TREE
    assert all(call["shell"] is False for call in calls)


def test_run_preflight_strips_inherited_cortex_runtime_from_gate_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PSC_MANAGER_EXECUTOR", "codex")
    monkeypatch.setenv("PSC_AGENTS_ROOT", "/live/agents")
    monkeypatch.setenv("PSC_REPO_ROOT", "/live/repo")
    monkeypatch.setenv("PREFLIGHT_AUTH_MARKER", "preserved")
    monkeypatch.setenv("HOME", "/operator/home")
    monkeypatch.setenv("XDG_CACHE_HOME", "/operator/cache")
    monkeypatch.setenv("PYTHONUSERBASE", "/operator/python-user-base")
    monkeypatch.setenv("GH_CONFIG_DIR", "/operator/gh-config")
    calls: list[dict[str, object]] = []

    class Result:
        def __init__(self, stdout="ok", returncode=0):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def runner(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        if "status" in argv:
            return Result("")
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return Result(HEAD)
        if argv[-2:] == ["rev-parse", "HEAD^{tree}"]:
            return Result(TREE)
        return Result()

    result = run_preflight(
        repo_root=tmp_path,
        command=("preflight",),
        request=PreflightRequest(pr_number=15),
        runner=runner,
    )

    assert result.passed
    disposable_homes: set[Path] = set()
    for call in calls[3:5]:
        environment = call["env"]
        assert isinstance(environment, dict)
        assert environment["PREFLIGHT_AUTH_MARKER"] == "preserved"
        assert not any(name.startswith("PSC_") for name in environment)
        assert environment["HOME"] != "/operator/home"
        assert environment["XDG_CACHE_HOME"] == str(
            Path(environment["HOME"]) / ".cache"
        )
        assert environment["PYTHONUSERBASE"] == "/operator/python-user-base"
        assert environment["GH_CONFIG_DIR"] == "/operator/gh-config"
        disposable_homes.add(Path(environment["HOME"]))
    assert len(disposable_homes) == 1
    assert not next(iter(disposable_homes)).exists()


def test_run_preflight_stops_after_failed_quick_policy(tmp_path: Path) -> None:
    calls = []

    class Result:
        def __init__(self, returncode=1, stdout=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = "failed" if returncode else ""

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if "status" in argv:
            return Result(0, "")
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return Result(0, HEAD)
        if argv[-2:] == ["rev-parse", "HEAD^{tree}"]:
            return Result(0, TREE)
        return Result()

    result = run_preflight(
        repo_root=tmp_path,
        command=("preflight",),
        request=PreflightRequest(pr_number=15),
        runner=runner,
    )
    assert not result.passed
    assert result.failed_stage == "policy"
    assert len(calls) == 4


def test_run_preflight_rejects_forged_or_empty_tree_request(tmp_path: Path) -> None:
    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def runner(argv, **kwargs):
        if "status" in argv:
            return Result("")
        return Result(TREE if argv[-1] == "HEAD^{tree}" else HEAD)

    with pytest.raises(ValueError, match="current tree"):
        run_preflight(
            repo_root=tmp_path,
            command=("preflight",),
            request=PreflightRequest(pr_number=15, tree_hash=""),
            runner=runner,
        )


def test_run_preflight_invalidates_result_when_tree_changes_during_gate(tmp_path: Path) -> None:
    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    tree_reads = 0

    def runner(argv, **kwargs):
        nonlocal tree_reads
        if argv[-1] == "HEAD":
            return Result(HEAD)
        if argv[-1] == "HEAD^{tree}":
            tree_reads += 1
            return Result(TREE if tree_reads == 1 else "c" * 40)
        if "status" in argv:
            return Result("")
        return Result("ok")

    result = run_preflight(
        repo_root=tmp_path,
        command=("preflight",),
        request=PreflightRequest(pr_number=15),
        runner=runner,
    )
    assert not result.passed
    assert result.failed_stage == "tree-race"


def test_run_preflight_rejects_dirty_worktree(tmp_path: Path) -> None:
    class Result:
        returncode = 0
        stdout = " M tracked.py\n"
        stderr = ""

    with pytest.raises(RuntimeError, match="clean committed worktree"):
        run_preflight(
            repo_root=tmp_path,
            command=("preflight",),
            request=PreflightRequest(pr_number=15),
            runner=lambda argv, **kwargs: Result(),
        )


def test_run_preflight_uses_trusted_clock_for_skip_evidence(tmp_path: Path) -> None:
    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def runner(argv, **kwargs):
        if "status" in argv:
            return Result("")
        if argv[-1] == "HEAD":
            return Result(HEAD)
        if argv[-1] == "HEAD^{tree}":
            return Result(TREE)
        return Result("ok")

    write_full_suite_evidence_after_run(
        repo_root=tmp_path,
        command=("python3", "-m", "pytest"),
        runner=runner,
        now=lambda: 100,
        state_root=tmp_path / "state",
    )

    with pytest.raises(ValueError, match="stale"):
        run_preflight(
            repo_root=tmp_path,
            command=("preflight",),
            request=PreflightRequest(
                pr_number=15,
                skip_tests=True,
                tree_hash=TREE,
            ),
            runner=runner,
            now=lambda: 2_000,
            evidence_state_root=tmp_path / "state",
        )


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), float("-inf")])
def test_evidence_and_preflight_reject_non_finite_timestamps(tmp_path: Path, timestamp: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        build_preflight_argv(
            command=("preflight",),
            request=PreflightRequest(pr_number=15, skip_tests=True),
            current_tree_hash=TREE,
            full_suite_evidence=FullSuiteEvidence(
                schema=FULL_SUITE_EVIDENCE_SCHEMA,
                tree_hash=TREE,
                completed_at_epoch=1,
                command=("pytest",),
                evidence_hash="1" * 64,
            ),
            now_epoch=timestamp,
        )
