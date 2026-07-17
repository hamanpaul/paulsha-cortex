"""Typed, shell-free execution of quick policy and CI-parity preflight gates."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


Runner = Callable[..., object]
DEFAULT_FULL_SUITE_MAX_AGE_SECONDS = 900


@dataclass(frozen=True)
class FullSuiteEvidence:
    tree_hash: str
    passed: bool
    completed_at_epoch: int | float


@dataclass(frozen=True)
class PreflightRequest:
    pr_number: int | None = None
    metadata_path: str | None = None
    skip_tests: bool = False
    tree_hash: str | None = None
    now_epoch: int | float | None = None
    full_suite: FullSuiteEvidence | None = None


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    failed_stage: str | None
    policy: CommandResult
    ci_parity: CommandResult | None


def load_preflight_command(
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    source = os.environ if env is None else env
    raw = source.get("PSC_PREFLIGHT_CMD", "").strip()
    if not raw:
        raise ValueError("PSC_PREFLIGHT_CMD is required")
    try:
        command = tuple(shlex.split(raw))
    except ValueError as exc:
        raise ValueError("PSC_PREFLIGHT_CMD is malformed") from exc
    if not command:
        raise ValueError("PSC_PREFLIGHT_CMD is required")
    executable = Path(command[0])
    if executable.is_absolute() and not (
        executable.is_file() and os.access(executable, os.X_OK)
    ):
        raise ValueError(f"PSC_PREFLIGHT_CMD executable unavailable: {executable}")
    return command


def _validate_skip_tests(request: PreflightRequest) -> None:
    if not request.skip_tests:
        return
    evidence = request.full_suite
    if (
        evidence is None
        or not evidence.passed
        or request.tree_hash is None
        or evidence.tree_hash != request.tree_hash
    ):
        raise ValueError("--skip-tests requires passed evidence for the exact current tree")
    if request.now_epoch is None:
        raise ValueError("--skip-tests requires an evidence timestamp")
    age = float(request.now_epoch) - float(evidence.completed_at_epoch)
    if age < 0 or age > DEFAULT_FULL_SUITE_MAX_AGE_SECONDS:
        raise ValueError("--skip-tests full-suite evidence is stale")


def build_preflight_argv(
    *,
    command: Sequence[str],
    request: PreflightRequest,
) -> list[str]:
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError("preflight command must be a non-empty typed argv")
    has_pr = request.pr_number is not None
    has_metadata = request.metadata_path is not None
    if has_pr == has_metadata:
        raise ValueError("preflight requires exactly one of pr_number or metadata_path")
    argv = list(command)
    if has_pr:
        if (
            not isinstance(request.pr_number, int)
            or isinstance(request.pr_number, bool)
            or request.pr_number <= 0
        ):
            raise ValueError("pr_number must be a positive integer")
        argv.extend(["--pr", str(request.pr_number)])
    else:
        metadata = Path(str(request.metadata_path))
        if not metadata.is_absolute():
            raise ValueError("metadata_path must be absolute")
        argv.extend(["--metadata", str(metadata)])
    _validate_skip_tests(request)
    if request.skip_tests:
        argv.append("--skip-tests")
    return argv


def _coerce_result(argv: Sequence[str], result: object) -> CommandResult:
    returncode = getattr(result, "returncode", None)
    if not isinstance(returncode, int):
        raise RuntimeError("preflight runner returned no integer returncode")
    stdout = getattr(result, "stdout", "")
    stderr = getattr(result, "stderr", "")
    return CommandResult(
        argv=tuple(argv),
        returncode=returncode,
        stdout=stdout if isinstance(stdout, str) else "",
        stderr=stderr if isinstance(stderr, str) else "",
    )


def _run(
    *,
    argv: Sequence[str],
    cwd: Path,
    runner: Runner,
) -> CommandResult:
    raw = runner(
        list(argv),
        cwd=str(cwd),
        shell=False,
        capture_output=True,
        text=True,
    )
    return _coerce_result(argv, raw)


def run_preflight(
    *,
    repo_root: str | Path,
    command: Sequence[str],
    request: PreflightRequest,
    runner: Runner = subprocess.run,
) -> PreflightResult:
    root = Path(repo_root).resolve()
    policy_argv = ["python3", "-m", "policy_check", "--repo", "."]
    policy = _run(argv=policy_argv, cwd=root, runner=runner)
    if policy.returncode != 0:
        return PreflightResult(
            passed=False,
            failed_stage="policy",
            policy=policy,
            ci_parity=None,
        )
    ci_argv = build_preflight_argv(command=command, request=request)
    ci_parity = _run(argv=ci_argv, cwd=root, runner=runner)
    return PreflightResult(
        passed=ci_parity.returncode == 0,
        failed_stage=None if ci_parity.returncode == 0 else "ci-parity",
        policy=policy,
        ci_parity=ci_parity,
    )
