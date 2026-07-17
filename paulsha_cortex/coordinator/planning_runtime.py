from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .launcher import build_agy_argv
from .model_identities import (
    AGY_MODEL_ID,
    CapabilityProbe,
    IdentityRegistry,
    ModelIdentity,
    load_model_identities,
    probe_agy_capability,
)


@dataclass(frozen=True)
class ProductionPlanningRuntime:
    identity_registry: IdentityRegistry
    probes: Mapping[tuple[str, str], CapabilityProbe]
    primary_questioner: Callable[[Mapping[str, object]], object]
    secondary_planner: Callable[[Mapping[str, object], ModelIdentity], object]
    primary_integrator: Callable[[Mapping[str, object], Mapping[str, object]], object]


def _planning_argv(identity: ModelIdentity, prompt: str, temp_dir: str, worktree: Path) -> list[str]:
    if identity.executor == "agy":
        return build_agy_argv(
            prompt=prompt,
            slice_id="cortex-planning-runtime",
            log_dir=temp_dir,
            worktree=str(worktree),
            allow_unsafe=False,
            model=identity.model_id,
        )
    if identity.executor == "codex":
        return [
            "codex", "exec", prompt, "--json", "--sandbox", "read-only",
            "--model", identity.model_id, "-o", str(Path(temp_dir) / "last.json"),
            "-C", str(worktree),
        ]
    if identity.executor == "claude":
        return [
            "claude", "-p", prompt, "--output-format", "json",
            "--permission-mode", "plan", "--tools", "", "--model", identity.model_id,
            "--add-dir", str(worktree),
        ]
    raise ValueError(f"unsupported read-only planning executor: {identity.executor}")


def _tree_snapshot(root: Path) -> str:
    """Hash every tracked/untracked file without following symlinks.

    The planner runs in a disposable copy, but the operator checkout is also
    hashed before and after launch.  This catches direct writes through an
    absolute path even when the planner exits non-zero.
    """

    digest = hashlib.sha256()
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in sorted(files):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            if path.is_symlink():
                digest.update(b"link\0")
                digest.update(os.readlink(path).encode("utf-8"))
            else:
                digest.update(b"file\0")
                digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _copy_planning_sandbox(worktree: Path, destination: Path) -> None:
    shutil.copytree(
        worktree,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git"),
    )


def _restore_operator_tree(worktree: Path, baseline: Path) -> None:
    for child in worktree.iterdir():
        if child.name == ".git":
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)
    for source in baseline.iterdir():
        target = worktree / source.name
        if source.is_symlink():
            target.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
        elif source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)


def _extract_json(stdout: str, output_path: Path) -> object:
    candidates = [stdout.strip()]
    if output_path.is_file():
        candidates.insert(0, output_path.read_text(encoding="utf-8").strip())
    candidates.extend(reversed([line.strip() for line in stdout.splitlines() if line.strip()]))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            for key in ("result", "content", "message", "text"):
                nested = value.get(key)
                if isinstance(nested, str):
                    try:
                        return json.loads(nested)
                    except json.JSONDecodeError:
                        pass
            return value
    raise ValueError("planning launcher returned no JSON object")


def _invoke_json(
    identity: ModelIdentity,
    prompt: str,
    *,
    worktree: Path,
    runner: Callable[..., object],
    timeout_seconds: int,
) -> object:
    operator_before = _tree_snapshot(worktree)
    with tempfile.TemporaryDirectory(prefix="cortex-planning-") as temp_dir:
        baseline = Path(temp_dir) / "baseline"
        sandbox = Path(temp_dir) / "checkout"
        _copy_planning_sandbox(worktree, baseline)
        shutil.copytree(baseline, sandbox, symlinks=True)
        sandbox_before = _tree_snapshot(sandbox)
        output_path = Path(temp_dir) / "last.json"
        argv = _planning_argv(identity, prompt, temp_dir, sandbox)
        failure: BaseException | None = None
        result: object | None = None
        try:
            raw = runner(
                argv,
                cwd=str(sandbox),
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            returncode = getattr(raw, "returncode", None)
            stdout = getattr(raw, "stdout", None)
            if returncode != 0 or not isinstance(stdout, str):
                raise ValueError(
                    f"planning launcher failed: {identity.executor}/{identity.model_id}"
                )
            result = _extract_json(stdout, output_path)
        except BaseException as exc:
            failure = exc
        finally:
            if _tree_snapshot(sandbox) != sandbox_before:
                failure = ValueError("planning launcher modified disposable read-only sandbox")
            if _tree_snapshot(worktree) != operator_before:
                _restore_operator_tree(worktree, baseline)
                failure = ValueError("planning launcher modified operator worktree; changes rolled back")
        if failure is not None:
            raise failure
        return result


def _probe_identity(
    identity: ModelIdentity,
    *,
    worktree: Path,
    runner: Callable[..., object],
    timeout_seconds: int,
) -> CapabilityProbe:
    expected = {
        "capability": "cortex-planning-json",
        "executor": identity.executor,
        "model": identity.model_id,
    }
    prompt = "Return only this JSON object and do not call tools: " + json.dumps(
        expected, ensure_ascii=False, separators=(",", ":")
    )
    try:
        value = _invoke_json(
            identity,
            prompt,
            worktree=worktree,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        return CapabilityProbe(
            False,
            identity.executor,
            identity.model_id,
            identity.independence_domain,
            "safe-probe-failed",
            type(exc).__name__,
        )
    if value != expected:
        return CapabilityProbe(
            False,
            identity.executor,
            identity.model_id,
            identity.independence_domain,
            "identity-mismatch",
        )
    return CapabilityProbe.ready_for(
        identity.executor, identity.model_id, identity.independence_domain
    )


def build_production_planning_runtime(
    *,
    primary: tuple[str, str],
    worktree: str | Path,
    runner: Callable[..., object] = subprocess.run,
    timeout_seconds: int = 120,
) -> ProductionPlanningRuntime:
    """Build the daemon's real, safe, heterogeneous planning adapters."""

    root = Path(worktree).resolve()
    registry = load_model_identities()
    probes: dict[tuple[str, str], CapabilityProbe] = {}
    for identity in registry.identities:
        if "planning" not in identity.capabilities:
            continue
        if identity.executor == "agy" and identity.model_id == AGY_MODEL_ID:
            probes[(identity.executor, identity.model_id)] = probe_agy_capability(
                runner=runner, timeout_seconds=min(timeout_seconds, 45)
            )
        else:
            probes[(identity.executor, identity.model_id)] = _probe_identity(
                identity,
                worktree=root,
                runner=runner,
                timeout_seconds=timeout_seconds,
            )

    primary_identity = registry.get(*primary)

    def invoke_primary(prompt: str) -> object:
        if primary_identity is None:
            raise ValueError("primary planning identity is not configured")
        return _invoke_json(
            primary_identity,
            prompt,
            worktree=root,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )

    def questioner(report: Mapping[str, object]) -> object:
        return invoke_primary(
            "Return only the exact question-pack JSON required to resolve this completeness report: "
            + json.dumps(report, ensure_ascii=False, sort_keys=True)
        )

    def secondary(pack: Mapping[str, object], identity: ModelIdentity) -> object:
        return _invoke_json(
            identity,
            "Return only evidence JSON; do not make decisions or edit files. Question pack: "
            + json.dumps(pack, ensure_ascii=False, sort_keys=True),
            worktree=root,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )

    def integrator(pack: Mapping[str, object], evidence: Mapping[str, object]) -> object:
        return invoke_primary(
            "Integrate evidence without editing files. Return integration JSON with an artifacts list; "
            "each artifact must contain kind, path, and complete UTF-8 content. "
            + json.dumps({"question_pack": pack, "secondary_evidence": evidence}, ensure_ascii=False, sort_keys=True)
        )

    return ProductionPlanningRuntime(registry, probes, questioner, secondary, integrator)
