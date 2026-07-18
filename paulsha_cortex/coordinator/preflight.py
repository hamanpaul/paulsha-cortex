"""Typed, shell-free execution of quick policy and CI-parity preflight gates."""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from paulsha_cortex.config import paths

from . import verification


Runner = Callable[..., object]
DEFAULT_FULL_SUITE_MAX_AGE_SECONDS = 900
FULL_SUITE_EVIDENCE_SCHEMA = "cortex-full-suite-evidence/v1"
SHELL_EXECUTABLES = frozenset({"bash", "sh", "dash", "zsh", "ksh", "fish"})


@dataclass(frozen=True)
class FullSuiteEvidence:
    schema: str
    tree_hash: str
    completed_at_epoch: float
    command: tuple[str, ...]
    evidence_hash: str


@dataclass(frozen=True)
class PreflightRequest:
    pr_number: int | None = None
    metadata_path: str | None = None
    skip_tests: bool = False
    tree_hash: str | None = None


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


def canonical_full_suite_evidence_path(
    tree_hash: str,
    *,
    state_root: str | Path | None = None,
) -> Path:
    if not _is_sha(tree_hash):
        raise ValueError("full-suite evidence tree_hash invalid")
    root = Path(state_root) if state_root is not None else paths.coordinator_root()
    return root.resolve() / "evidence" / "full-suite" / f"{tree_hash.lower()}.json"


def load_full_suite_evidence(
    *,
    tree_hash: str,
    state_root: str | Path | None = None,
) -> FullSuiteEvidence:
    evidence_path = canonical_full_suite_evidence_path(tree_hash, state_root=state_root)
    if evidence_path.is_symlink() or not evidence_path.is_file():
        raise ValueError("full-suite evidence unavailable")
    if evidence_path.stat().st_mode & 0o222:
        raise ValueError("full-suite evidence must be immutable")
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("full-suite evidence unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != {"payload", "payload_hash"}:
        raise ValueError("full-suite evidence malformed")
    body = payload.get("payload")
    expected_hash = payload.get("payload_hash")
    if not isinstance(body, dict) or set(body) != {
        "schema",
        "tree_hash",
        "completed_at_epoch",
        "command",
    }:
        raise ValueError("full-suite evidence malformed")
    if body.get("schema") != FULL_SUITE_EVIDENCE_SCHEMA:
        raise ValueError("full-suite evidence schema unsupported")
    if not _is_sha(body.get("tree_hash")) or body["tree_hash"].lower() != tree_hash.lower():
        raise ValueError("full-suite evidence tree_hash invalid")
    completed = body.get("completed_at_epoch")
    command = body.get("command")
    if (
        not isinstance(completed, (int, float))
        or isinstance(completed, bool)
        or not math.isfinite(float(completed))
    ):
        raise ValueError("full-suite evidence timestamp invalid")
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg for arg in command):
        raise ValueError("full-suite evidence command invalid")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(ch not in "0123456789abcdefABCDEF" for ch in expected_hash)
        or verification.canonical_json_hash(body) != expected_hash.lower()
    ):
        raise ValueError("full-suite evidence hash mismatch")
    return FullSuiteEvidence(
        schema=FULL_SUITE_EVIDENCE_SCHEMA,
        tree_hash=body["tree_hash"].lower(),
        completed_at_epoch=float(completed),
        command=tuple(command),
        evidence_hash=expected_hash.lower(),
    )


def write_full_suite_evidence_after_run(
    *,
    repo_root: str | Path,
    command: Sequence[str],
    runner: Runner = subprocess.run,
    now: Callable[[], float] = time.time,
    state_root: str | Path | None = None,
) -> FullSuiteEvidence:
    _validate_typed_command(command)
    root = Path(repo_root).resolve()
    head, tree_hash = _read_clean_identity(root=root, runner=runner)
    result = _run(argv=command, cwd=root, runner=runner)
    final_head, final_tree = _read_clean_identity(root=root, runner=runner)
    if result.returncode != 0:
        raise RuntimeError("full-suite command failed")
    if final_head != head or final_tree != tree_hash:
        raise RuntimeError("full-suite tree changed during execution")
    completed = now()
    if (
        not isinstance(completed, (int, float))
        or isinstance(completed, bool)
        or not math.isfinite(float(completed))
    ):
        raise ValueError("full-suite completion timestamp must be finite")
    body = {
        "schema": FULL_SUITE_EVIDENCE_SCHEMA,
        "tree_hash": tree_hash,
        "completed_at_epoch": float(completed),
        "command": list(command),
    }
    envelope = {"payload": body, "payload_hash": verification.canonical_json_hash(body)}
    evidence_path = canonical_full_suite_evidence_path(tree_hash, state_root=state_root)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with evidence_path.open("x", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        evidence_path.chmod(0o400)
    except FileExistsError:
        existing = load_full_suite_evidence(tree_hash=tree_hash, state_root=state_root)
        if existing.evidence_hash != envelope["payload_hash"]:
            raise RuntimeError("conflicting immutable full-suite evidence")
        return existing
    return load_full_suite_evidence(tree_hash=tree_hash, state_root=state_root)


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
    now_epoch: int | float | None,
) -> None:
    if not request.skip_tests:
        return
    if (
        evidence is None
        or not _is_sha(current_tree_hash)
        or evidence.tree_hash != current_tree_hash
    ):
        raise ValueError("--skip-tests requires passed evidence for the exact current tree")
    if (
        not isinstance(now_epoch, (int, float))
        or isinstance(now_epoch, bool)
        or not math.isfinite(float(now_epoch))
    ):
        raise ValueError("--skip-tests requires a finite trusted clock")
    age = float(now_epoch) - float(evidence.completed_at_epoch)
    if age < 0 or age > DEFAULT_FULL_SUITE_MAX_AGE_SECONDS:
        raise ValueError("--skip-tests full-suite evidence is stale")


def build_preflight_argv(
    *,
    command: Sequence[str],
    request: PreflightRequest,
    current_tree_hash: str | None = None,
    full_suite_evidence: FullSuiteEvidence | None = None,
    now_epoch: int | float | None = None,
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
        now_epoch=now_epoch,
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


def _read_clean_identity(*, root: Path, runner: Runner) -> tuple[str, str]:
    status = _run(
        argv=["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
        cwd=root,
        runner=runner,
    )
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
    if status.returncode != 0 or status.stdout.strip():
        raise RuntimeError("preflight requires a clean committed worktree")
    if head_result.returncode != 0 or not _is_sha(head):
        raise RuntimeError("unable to resolve current HEAD")
    if tree_result.returncode != 0 or not _is_sha(tree_hash):
        raise RuntimeError("unable to resolve current tree hash")
    return head, tree_hash


def run_preflight(
    *,
    repo_root: str | Path,
    command: Sequence[str],
    request: PreflightRequest,
    runner: Runner = subprocess.run,
    now: Callable[[], float] = time.time,
    evidence_state_root: str | Path | None = None,
) -> PreflightResult:
    root = Path(repo_root).resolve()
    head, tree_hash = _read_clean_identity(root=root, runner=runner)
    if request.tree_hash is not None and (
        not _is_sha(request.tree_hash) or request.tree_hash.lower() != tree_hash
    ):
        raise ValueError("preflight request does not match current tree")
    evidence = None
    now_epoch = now()
    if (
        not isinstance(now_epoch, (int, float))
        or isinstance(now_epoch, bool)
        or not math.isfinite(float(now_epoch))
    ):
        raise ValueError("preflight clock must be finite")
    if request.skip_tests:
        evidence = load_full_suite_evidence(
            tree_hash=tree_hash,
            state_root=evidence_state_root,
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
        request=request,
        current_tree_hash=tree_hash,
        full_suite_evidence=evidence,
        now_epoch=now_epoch,
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
