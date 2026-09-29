"""Builder-identity half of the three-UID workspace reclaim protocol.

The Manager may request this fixed operation through the root-owned builder
template, but this module never accepts an arbitrary deletion path.  The
workspace must be a direct child of the configured pool and its marker must
match the Manager-authored marker snapshot carried in the protected job spec.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence
from uuid import uuid4

from . import job_workspace
from . import job_runner
from ..config import paths
from ..trust_root.permgen import DEFAULT_SCHEME

PRESERVE_FILE_MAX_BYTES = 4 * 1024 * 1024
PRESERVE_FILE_MAX_COUNT = 512
PRESERVE_BUNDLE_MAX_BYTES = 256 * 1024 * 1024
_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")


def _git_status(workspace: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
        check=False,
        timeout=job_workspace.WORKSPACE_DIRTY_SCAN_TIMEOUT_SECONDS,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace")[-500:]
        raise RuntimeError(f"dirty scan failed: {detail}")
    records = [part for part in result.stdout.split(b"\0") if part]
    paths: list[str] = []
    index = 0
    while index < len(records):
        row = records[index]
        index += 1
        if len(row) < 4:
            raise RuntimeError("dirty scan returned malformed status")
        name = row[3:].decode("utf-8", "surrogateescape")
        # Rename/copy records contain a second NUL-delimited path.
        if row[:2].decode("ascii", "ignore").strip() in {"R", "C"} and index < len(records):
            paths.append(records[index].decode("utf-8", "surrogateescape"))
            index += 1
        paths.append(name)
    return list(dict.fromkeys(paths))


def _safe_relative(name: str) -> Path:
    rel = PurePosixPath(name)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise RuntimeError("dirty scan returned a path outside the workspace")
    return Path(*rel.parts)


def _preserve(workspace: Path, paths: Sequence[str], preserve_root: Path) -> tuple[Path, int]:
    if len(paths) > PRESERVE_FILE_MAX_COUNT:
        raise RuntimeError("dirty workspace exceeds the preserve file-count limit")
    entries: list[tuple[str, Path, os.stat_result]] = []
    for name in paths:
        relative = _safe_relative(name)
        source = workspace / relative
        info = source.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_size > PRESERVE_FILE_MAX_BYTES:
            raise RuntimeError("dirty workspace file exceeds the preserve size limit")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RuntimeError("unsupported dirty workspace entry")
        entries.append((name, source, info))
    archive = preserve_root / f"{workspace.name}-{uuid4().hex}"
    archive.mkdir(mode=0o755, parents=False, exist_ok=False)
    copied = 0
    try:
        for name, source, info in entries:
            relative = _safe_relative(name)
            destination = archive / relative
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            if stat.S_ISLNK(info.st_mode):
                os.symlink(os.readlink(source), destination)
            elif stat.S_ISREG(info.st_mode):
                if source.stat().st_size > PRESERVE_FILE_MAX_BYTES:
                    raise RuntimeError("dirty workspace file grew beyond the preserve size limit")
                shutil.copyfile(source, destination, follow_symlinks=False)
                os.chmod(destination, 0o644)
            elif stat.S_ISDIR(info.st_mode):
                destination.mkdir(mode=0o755, exist_ok=True)
            else:
                raise RuntimeError("unsupported dirty workspace entry")
            copied += 1
        return archive, copied
    except Exception:
        shutil.rmtree(archive, ignore_errors=True)
        raise


def _archive_commit(workspace: Path, archive: Path, base: str) -> str:
    if _COMMIT_RE.fullmatch(base) is None:
        raise RuntimeError("workspace marker base commit is malformed")
    result = subprocess.run(
        ["git", "-C", str(workspace), "bundle", "create", str(archive / "workspace-head.bundle"), "HEAD", f"^{base}"],
        capture_output=True,
        check=False,
        timeout=job_workspace.WORKSPACE_DIRTY_SCAN_TIMEOUT_SECONDS,
    )
    bundle = archive / "workspace-head.bundle"
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace")[-500:]
        raise RuntimeError(f"workspace commit preservation failed: {detail}")
    if not bundle.is_file() or bundle.stat().st_size > PRESERVE_BUNDLE_MAX_BYTES:
        raise RuntimeError("workspace commit bundle is absent or exceeds the size limit")
    os.chmod(bundle, 0o644)
    return str(bundle)


def reclaim_owned_workspace(
    *,
    workspace: str | Path,
    pool_root: str | Path,
    preserve_root: str | Path,
    expected_marker: Mapping[str, object],
) -> dict[str, object]:
    """Scan, preserve, and clear one marker-proven builder clone.

    The pool parent remains Manager-owned; the caller removes the now-empty
    workspace after this function succeeds.
    """

    pool = Path(pool_root).resolve(strict=True)
    target = Path(workspace)
    if target.is_symlink() or not target.is_dir():
        raise RuntimeError("workspace is not a real directory")
    resolved = target.resolve(strict=True)
    if resolved.parent != pool:
        raise RuntimeError("workspace is outside the configured job pool")
    marker_path = job_workspace.marker_path(resolved)
    marker_info = marker_path.lstat()
    if not stat.S_ISREG(marker_info.st_mode):
        raise RuntimeError("workspace marker is not a regular file")
    observed = job_workspace.read_marker(resolved)
    if not isinstance(observed, dict) or dict(observed) != dict(expected_marker):
        raise RuntimeError("workspace marker does not match the Manager snapshot")
    if observed.get("model") != job_workspace.WORKSPACE_MODEL:
        raise RuntimeError("workspace marker model is unsupported")
    if not isinstance(observed.get("attempt_id"), str) or not isinstance(observed.get("owner_identity"), dict):
        raise RuntimeError("workspace marker has no owner-bound identity")

    dirty = _git_status(resolved)
    archive, preserved = _preserve(resolved, dirty, Path(preserve_root)) if dirty else (None, 0)
    head_result = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        check=False,
        timeout=job_workspace.WORKSPACE_DIRTY_SCAN_TIMEOUT_SECONDS,
    )
    if head_result.returncode:
        raise RuntimeError("workspace HEAD cannot be verified")
    head = head_result.stdout.decode("ascii", "strict").strip().lower()
    base = str(observed.get("base") or "").lower()
    if _COMMIT_RE.fullmatch(head) is None or _COMMIT_RE.fullmatch(base) is None:
        raise RuntimeError("workspace HEAD or marker base is malformed")
    preserved_commit = head != base
    if preserved_commit:
        if archive is None:
            archive = Path(preserve_root) / f"{resolved.name}-{uuid4().hex}"
            archive.mkdir(mode=0o755, parents=False, exist_ok=False)
        _archive_commit(resolved, archive, base)
    # Re-read ownership immediately before mutation.  This rejects marker swaps
    # between the first validation and the destructive phase.
    if job_workspace.read_marker(resolved) != observed:
        raise RuntimeError("workspace marker changed during reclaim")
    for child in list(resolved.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    return {
        "schema_version": 1,
        "status": "cleared",
        "workspace_name": resolved.name,
        "preserved_files": preserved,
        "preserved_commit": preserved_commit,
        "preserve_path": str(archive) if archive else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--pool-root", required=True)
    parser.add_argument("--preserve-root", required=True)
    parser.add_argument("--marker-json", required=True)
    args = parser.parse_args(argv)
    try:
        expected = json.loads(args.marker_json)
        if not isinstance(expected, dict):
            raise RuntimeError("marker snapshot is malformed")
        result = reclaim_owned_workspace(
            workspace=args.workspace,
            pool_root=args.pool_root,
            preserve_root=args.preserve_root,
            expected_marker=expected,
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"owner reclaim failed: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return 1


def reclaim_through_builder_unit(
    *, workspace: str | Path, marker: Mapping[str, object]
) -> dict[str, object]:
    """Run the fixed helper under the installed, root-owned builder template."""

    env = dict(os.environ)
    workspace_path = Path(workspace).resolve(strict=True)
    source_repo = marker.get("source_repo")
    if not isinstance(source_repo, str) or not source_repo.startswith("/"):
        raise RuntimeError("workspace marker has no absolute source repository")
    job_id = workspace_path.name
    if not job_runner.instance_name_valid(job_id):
        raise RuntimeError("workspace name cannot be used as a builder unit instance")
    executor = (env.get("PSC_MANAGER_EXECUTOR") or "").strip()
    if not executor:
        raise RuntimeError("PSC_MANAGER_EXECUTOR is required for the builder reclaim unit")
    plan = job_runner.prepare_systemd_template(
        env, job_id=job_id, executor=executor, role=job_runner.JOB_ROLE_BUILDER
    )
    pool_root = paths.worktree_root().resolve(strict=True)
    archive_bundle = job_workspace.prepare_commit_spool(
        spool_key=plan.instance, coordinator_root=paths.coordinator_root()
    )
    archive_root = archive_bundle.parent / "reclaim-preserved"
    archive_root.mkdir(mode=0o700, exist_ok=True)
    manager_account = DEFAULT_SCHEME.durable_state_owner
    if not manager_account:
        raise RuntimeError("the installed UID scheme has no Manager durable-state owner")
    builder_account = plan.account
    acl = shutil.which(job_workspace.SETFACL_PROGRAM)
    if acl is None:
        raise RuntimeError("setfacl is unavailable for the reclaim archive boundary")
    # Manager can inspect the preserved evidence; builder can create only in
    # this per-invocation spool slot.  No ACL is added to the workspace for Manager.
    subprocess.run(
        [
            acl, "-m",
            f"u:{manager_account}:rwx,u:{builder_account}:rwx,d:u:{manager_account}:rwx,d:u:{builder_account}:rwx",
            str(archive_root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    log = job_workspace.prepare_job_log_spool(
        principal_id="builder",
        spool_key=plan.instance,
        manager_log_path=paths.coordinator_root() / "logs" / "reclaim" / f"{job_id}.jsonl",
    )
    job_env = job_runner.build_job_env(
        manager_env=env,
        job_id=job_id,
        slice_id=job_id,
        repo_root=source_repo,
        workspace=workspace_path,
        role=job_runner.JOB_ROLE_BUILDER,
    )
    command = [
        "/opt/cortex/venv/bin/python",
        "-m",
        "paulsha_cortex.coordinator.owner_reclaim",
        "--workspace", str(workspace_path),
        "--pool-root", str(pool_root),
        "--preserve-root", str(archive_root),
        "--marker-json", json.dumps(dict(marker), sort_keys=True, separators=(",", ":")),
    ]
    spec = job_runner.build_job_spec(
        job_id=job_id,
        instance=plan.instance,
        unit=plan.unit,
        command=command,
        working_directory=str(workspace_path),
        log_path=str(log),
        env=job_env,
    )
    job_runner.write_job_spec(plan.spec_path, spec, account=plan.account)
    start = job_runner.build_systemctl_start_argv(systemctl=plan.binary, unit=plan.unit)
    completed = subprocess.run(
        start,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    if completed.returncode:
        raise RuntimeError(f"builder reclaim unit failed: {completed.stderr[-500:]}")
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(result, dict) or result.get("status") != "cleared":
            continue
        if result.get("workspace_name") != workspace_path.name:
            raise RuntimeError("builder reclaim completion names a different workspace")
        preserved = result.get("preserved_files")
        preserve_path = result.get("preserve_path")
        if not isinstance(preserved, int) or preserved < 0:
            raise RuntimeError("builder reclaim completion has an invalid preserve count")
        preserved_commit = result.get("preserved_commit") is True
        if preserved == 0 and not preserved_commit:
            if preserve_path is not None:
                raise RuntimeError("builder reclaim returned an empty preserve archive")
        else:
            archive = Path(str(preserve_path))
            if archive.parent != archive_root or archive.is_symlink() or not archive.is_dir():
                raise RuntimeError("builder reclaim archive is outside its Manager spool slot")
            observed_count = 0
            for directory, child_dirs, child_files in os.walk(archive, followlinks=False):
                observed_count += sum(
                    1 for name in child_dirs
                    if (Path(directory) / name).is_symlink()
                )
                observed_count += len(child_files)
            expected_count = preserved + (1 if preserved_commit else 0)
            if observed_count != expected_count:
                raise RuntimeError("builder reclaim archive count does not match its completion")
            if preserved_commit:
                bundle = archive / "workspace-head.bundle"
                if not bundle.is_file() or bundle.is_symlink():
                    raise RuntimeError("builder reclaim omitted the local commit bundle")
        job_workspace.seal_commit_spool(archive_bundle)
        return result
    raise RuntimeError("builder reclaim unit returned no completion record")


if __name__ == "__main__":  # pragma: no cover - installed helper entry point
    raise SystemExit(main())
