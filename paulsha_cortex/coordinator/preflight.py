"""Typed, shell-free execution of quick policy and CI-parity preflight gates."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import verification


Runner = Callable[..., object]
DEFAULT_FULL_SUITE_MAX_AGE_SECONDS = 900
FULL_SUITE_EVIDENCE_SCHEMA = "cortex-full-suite-evidence/v1"
SHELL_EXECUTABLES = frozenset({"bash", "sh", "dash", "zsh", "ksh", "fish"})


@dataclass(frozen=True)
class FullSuiteEvidence:
    schema: str
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
    full_suite_evidence_path: str | None = None
    full_suite_evidence_hash: str | None = None


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
    head: str
    tree_hash: str


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and verification.SAFE_SHA_RE.fullmatch(value) is not None


def load_full_suite_evidence(
    path: str | Path,
    *,
    expected_hash: str,
) -> FullSuiteEvidence:
    evidence_path = Path(path)
    if not evidence_path.is_absolute() or evidence_path.is_symlink():
        raise ValueError("full-suite evidence path must be absolute and not a symlink")
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("full-suite evidence unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "tree_hash",
        "passed",
        "completed_at_epoch",
    }:
        raise ValueError("full-suite evidence malformed")
    if payload.get("schema") != FULL_SUITE_EVIDENCE_SCHEMA:
        raise ValueError("full-suite evidence schema unsupported")
    if not _is_sha(payload.get("tree_hash")):
        raise ValueError("full-suite evidence tree_hash invalid")
    if payload.get("passed") is not True:
        raise ValueError("full-suite evidence did not pass")
    completed = payload.get("completed_at_epoch")
    if not isinstance(completed, (int, float)) or isinstance(completed, bool):
        raise ValueError("full-suite evidence timestamp invalid")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(ch not in "0123456789abcdefABCDEF" for ch in expected_hash)
        or verification.canonical_json_hash(payload) != expected_hash.lower()
    ):
        raise ValueError("full-suite evidence hash mismatch")
    return FullSuiteEvidence(
        schema=FULL_SUITE_EVIDENCE_SCHEMA,
        tree_hash=payload["tree_hash"].lower(),
        passed=True,
        completed_at_epoch=completed,
    )


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
    _validate_typed_command(command)
    executable = Path(command[0])
    if executable.is_absolute() and not (
        executable.is_file() and os.access(executable, os.X_OK)
    ):
        raise ValueError(f"PSC_PREFLIGHT_CMD executable unavailable: {executable}")
    if not executable.is_absolute() and shutil.which(command[0]) is None:
        raise ValueError(f"PSC_PREFLIGHT_CMD executable unavailable: {command[0]}")
    return command


def _validate_typed_command(command: Sequence[str]) -> None:
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError("preflight command must be a non-empty typed argv")
    index = 0
    if Path(command[0]).name == "env":
        index = 1
        while index < len(command) and (command[index].startswith("-") or "=" in command[index]):
            index += 1
    if index < len(command) and Path(command[index]).name in SHELL_EXECUTABLES:
        if "-c" in command[index + 1 :]:
            raise ValueError("PSC_PREFLIGHT_CMD shell wrapper is not allowed")


def _validate_skip_tests(
    request: PreflightRequest,
    *,
    current_tree_hash: str | None,
    evidence: FullSuiteEvidence | None,
) -> None:
    if not request.skip_tests:
        return
    if (
        evidence is None
        or not evidence.passed
        or not _is_sha(current_tree_hash)
        or evidence.tree_hash != current_tree_hash
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
    current_tree_hash: str | None = None,
    full_suite_evidence: FullSuiteEvidence | None = None,
) -> list[str]:
    _validate_typed_command(command)
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
    _validate_skip_tests(
        request,
        current_tree_hash=current_tree_hash,
        evidence=full_suite_evidence,
    )
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
    now: Callable[[], float] = time.time,
) -> PreflightResult:
    root = Path(repo_root).resolve()
    status_result = _run(
        argv=["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
        cwd=root,
        runner=runner,
    )
    if status_result.returncode != 0 or status_result.stdout.strip():
        raise RuntimeError("preflight requires a clean committed worktree")
    head_result = _run(
        argv=["git", "-C", str(root), "rev-parse", "HEAD"],
        cwd=root,
        runner=runner,
    )
    tree_result = _run(
        argv=["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
        cwd=root,
        runner=runner,
    )
    head = head_result.stdout.strip().lower()
    tree_hash = tree_result.stdout.strip().lower()
    if head_result.returncode != 0 or not _is_sha(head):
        raise RuntimeError("unable to resolve current HEAD")
    if tree_result.returncode != 0 or not _is_sha(tree_hash):
        raise RuntimeError("unable to resolve current tree hash")
    if request.tree_hash is not None and (
        not _is_sha(request.tree_hash) or request.tree_hash.lower() != tree_hash
    ):
        raise ValueError("preflight request does not match current tree")
    evidence = None
    effective_request = replace(request, now_epoch=now())
    if request.skip_tests:
        if request.full_suite_evidence_path is None or request.full_suite_evidence_hash is None:
            raise ValueError("--skip-tests requires durable full-suite evidence")
        evidence = load_full_suite_evidence(
            request.full_suite_evidence_path,
            expected_hash=request.full_suite_evidence_hash,
        )
    policy_argv = ["python3", "-m", "policy_check", "--repo", "."]
    policy = _run(argv=policy_argv, cwd=root, runner=runner)
    if policy.returncode != 0:
        return PreflightResult(
            passed=False,
            failed_stage="policy",
            policy=policy,
            ci_parity=None,
            head=head,
            tree_hash=tree_hash,
        )
    ci_argv = build_preflight_argv(
        command=command,
        request=effective_request,
        current_tree_hash=tree_hash,
        full_suite_evidence=evidence,
    )
    ci_parity = _run(argv=ci_argv, cwd=root, runner=runner)
    final_head_result = _run(
        argv=["git", "-C", str(root), "rev-parse", "HEAD"],
        cwd=root,
        runner=runner,
    )
    final_tree_result = _run(
        argv=["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
        cwd=root,
        runner=runner,
    )
    final_status_result = _run(
        argv=["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
        cwd=root,
        runner=runner,
    )
    final_head = final_head_result.stdout.strip().lower()
    final_tree = final_tree_result.stdout.strip().lower()
    tree_stable = (
        final_head_result.returncode == 0
        and final_tree_result.returncode == 0
        and final_status_result.returncode == 0
        and not final_status_result.stdout.strip()
        and final_head == head
        and final_tree == tree_hash
    )
    return PreflightResult(
        passed=ci_parity.returncode == 0 and tree_stable,
        failed_stage=(
            "tree-race"
            if not tree_stable
            else None if ci_parity.returncode == 0 else "ci-parity"
        ),
        policy=policy,
        ci_parity=ci_parity,
        head=head,
        tree_hash=tree_hash,
    )
