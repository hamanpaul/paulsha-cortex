from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
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
            "-C", str(worktree), "--skip-git-repo-check",
        ]
    if identity.executor == "claude":
        return [
            "claude", "-p", prompt, "--output-format", "json",
            "--permission-mode", "plan", "--tools", "", "--model", identity.model_id,
            "--add-dir", str(worktree),
        ]
    raise ValueError(f"unsupported read-only planning executor: {identity.executor}")


def _tree_snapshot(root: Path) -> str:
    """Hash the complete tree shape, content, links, and stable metadata.

    The planner runs in a disposable copy, but the operator checkout is also
    hashed before and after launch.  This catches direct writes through an
    absolute path even when the planner exits non-zero.
    """

    digest = hashlib.sha256()

    def add_metadata(path: Path) -> os.stat_result:
        metadata = path.lstat()
        digest.update(f"{metadata.st_mode}:{metadata.st_uid}:{metadata.st_gid}".encode())
        digest.update(b"\0")
        try:
            names = sorted(os.listxattr(path, follow_symlinks=False))
        except (AttributeError, OSError):
            names = []
        for name in names:
            digest.update(name.encode("utf-8", errors="surrogateescape"))
            digest.update(b"=")
            try:
                digest.update(os.getxattr(path, name, follow_symlinks=False))
            except OSError:
                digest.update(b"<unreadable>")
            digest.update(b"\0")
        return metadata

    def visit(path: Path, relative: Path) -> None:
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        metadata = add_metadata(path)
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(b"link\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif stat.S_ISDIR(metadata.st_mode):
            digest.update(b"dir\0")
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                if child.name == ".git":
                    continue
                visit(child, relative / child.name)
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"file\0")
            digest.update(path.read_bytes())
        else:
            digest.update(b"special\0")
            digest.update(str(metadata.st_rdev).encode())
        digest.update(b"\0")

    visit(root, Path("."))
    return digest.hexdigest()


def _copy_planning_sandbox(worktree: Path, destination: Path) -> None:
    shutil.copytree(
        worktree,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git"),
    )


def _make_tree_traversable(root: Path) -> None:
    """Restore enough owner access to inspect and replace a hostile tree.

    The launcher can chmod directories through an absolute path.  Never follow
    symlinks while recovering access; the immutable baseline restores the
    original metadata after the polluted entries have been removed.
    """

    if root.is_symlink():
        raise RuntimeError("planning recovery root cannot be a symlink")
    os.chmod(root, 0o700, follow_symlinks=False)

    def visit(directory: Path) -> None:
        os.chmod(directory, 0o700, follow_symlinks=False)
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name == ".git" or entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    visit(path)

    visit(root)


def _restore_operator_tree(worktree: Path, baseline: Path) -> None:
    _make_tree_traversable(worktree)
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
    shutil.copystat(baseline, worktree, follow_symlinks=False)
    if _tree_snapshot(worktree) != _tree_snapshot(baseline):
        raise RuntimeError("planning operator restore verification failed")


def _extract_json(stdout: str, output_path: Path) -> object:
    candidates = [stdout.strip()]
    if output_path.is_file():
        candidates.insert(0, output_path.read_text(encoding="utf-8").strip())
    for candidate in candidates:
        fenced = re.fullmatch(
            r"```(?:json)?\s*\n(?P<body>\{.*\})\s*\n```",
            candidate,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if fenced is not None:
            candidate = fenced.group("body")
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
            try:
                sandbox_dirty = _tree_snapshot(sandbox) != sandbox_before
            except BaseException:
                sandbox_dirty = True
                try:
                    _make_tree_traversable(sandbox)
                except BaseException:
                    pass
            if sandbox_dirty:
                failure = ValueError("planning launcher modified disposable read-only sandbox")

            operator_dirty = False
            try:
                operator_dirty = _tree_snapshot(worktree) != operator_before
            except BaseException:
                operator_dirty = True
            if operator_dirty:
                try:
                    _restore_operator_tree(worktree, baseline)
                except BaseException as exc:
                    failure = RuntimeError("planning operator restore failed")
                    failure.__cause__ = exc
                else:
                    failure = ValueError(
                        "planning launcher modified operator worktree; changes rolled back"
                    )
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


def _planning_source_material(
    pack: Mapping[str, object], *, root: Path, max_bytes: int = 262_144
) -> dict[str, str]:
    questions = pack.get("questions")
    if not isinstance(questions, list):
        raise ValueError("planning question pack has no questions")
    refs = sorted(
        {
            ref
            for question in questions
            if isinstance(question, dict)
            for ref in question.get("source_refs", [])
            if isinstance(ref, str)
        }
    )
    material: dict[str, str] = {}
    total = 0
    for ref in refs:
        pure = PurePosixPath(ref)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != ref:
            raise ValueError("planning source ref is not canonical repo-relative")
        current = root
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("planning source ref traverses symlink")
        try:
            target = current.resolve(strict=True)
            target.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("planning source ref is unavailable") from exc
        if not target.is_file():
            raise ValueError("planning source ref is not a file")
        try:
            body = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("planning source ref is unreadable") from exc
        total += len(body.encode("utf-8"))
        if total > max_bytes:
            raise ValueError("planning source material exceeds bounded context")
        material[ref] = body
    return material


def _planning_destinations(pack: Mapping[str, object]) -> dict[str, str]:
    questions = pack.get("questions")
    if not isinstance(questions, list):
        return {}
    slugs = {
        parts[2]
        for question in questions if isinstance(question, dict)
        for ref in question.get("source_refs", [])
        if isinstance(ref, str)
        and (parts := PurePosixPath(ref).parts)[:2] == ("openspec", "changes")
        and len(parts) >= 4
    }
    if len(slugs) != 1:
        return {}
    slug = next(iter(slugs))
    return {
        "spec": f"docs/superpowers/specs/{slug}-spec.md",
        "design": f"docs/superpowers/specs/{slug}-design.md",
        "plan": f"docs/superpowers/plans/{slug}.md",
    }


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
        source_material = _planning_source_material(pack, root=root)
        return _invoke_json(
            identity,
            "Do not call tools, run commands, make decisions, or edit files. Use only the supplied "
            "source material. Return exactly one JSON object with keys schema_version=1, "
            "question_pack_id, and evidence. Evidence must contain every question exactly once; "
            "each row has only question_id, non-empty claims string list, and non-empty source_refs "
            "string list naming supplied sources. Input: "
            + json.dumps(
                {"question_pack": pack, "source_material": source_material},
                ensure_ascii=False,
                sort_keys=True,
            ),
            worktree=root,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )

    def integrator(pack: Mapping[str, object], evidence: Mapping[str, object]) -> object:
        return invoke_primary(
            "Do not call tools or edit files. Integrate only the supplied evidence. Return exactly one "
            "JSON object with schema_version=1, question_pack_id, secondary_evidence_hash, resolutions, "
            "and artifacts. Each resolution has only question_id, decision, artifact_kind, artifact_refs. "
            "Each artifact has only kind, path, content; content must be complete UTF-8 Markdown with "
            "frontmatter status: accepted, the matching work_item, and required headings: Requirements "
            "for spec, Decisions for design, Tasks for plan. Use the supplied destination paths. Input: "
            + json.dumps(
                {
                    "question_pack": pack,
                    "secondary_evidence": evidence,
                    "destinations": _planning_destinations(pack),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    return ProductionPlanningRuntime(registry, probes, questioner, secondary, integrator)
