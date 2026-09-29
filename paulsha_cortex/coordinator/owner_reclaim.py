"""Builder-identity half of the three-UID workspace reclaim protocol (#1167).

The Manager has no ACL on builder-created inodes, so it cannot clear a used
builder clone itself.  It asks the root-owned builder template unit
(``cortex-job@<slot>.service``) to run this fixed helper as the builder UID
against exactly one existing pool slot, then removes the emptied directory
entry it owns.

Threat model (full text: ``docs/three-uid-workspace-reclaim.md``):

* The helper never gains privilege.  It runs as the builder account inside the
  template unit, whose mount namespace makes only ``<pool>/<instance>`` and that
  instance's spools writable.  The builder account's per-job named ACL already
  lets it delete its own slot, so a builder calling this CLI directly can reach
  nothing it could not ``rm -rf`` anyway.
* "Manager approved" must still be true.  The workspace marker is readable (and,
  inside the builder-writable ``.git``, replaceable) by the builder, so a marker
  match proves nothing about Manager intent.  The helper therefore requires a
  Manager-authored approval that the builder cannot forge: the job spec the
  Manager wrote for this unit instance in the builder spec spool (Manager-owned,
  builder read-only).  The spec must name this exact workspace and carry this
  exact argv, which binds the pool, the preserve location, the marker digest and
  a per-invocation nonce.  The Manager deletes the spec as soon as the unit has
  finished, so an approval is single use; a leftover copy cannot clear a later
  attempt because that attempt has a different marker digest.
* The helper's completion record is never the basis of a Manager decision.  The
  log it lands in is writable by any builder process that shares the instance's
  mounts, and the nonce is in the builder-readable spec.  The Manager refuses to
  start while another builder unit holds the instance, and after the unit it
  seals the slot, adopts every archive it observes in the preserve area into
  Manager-only evidence, judges success by the systemd result and by the
  workspace being observably empty, and only then cross-checks the record.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence
from uuid import uuid4

from . import job_runner, job_workspace, spool_slot
from ..config import paths
from ..trust_root.permgen import DEFAULT_SCHEME

MODULE = "paulsha_cortex.coordinator.owner_reclaim"
#: The installed interpreter the approved command names.  The approval compares the
#: argv after ``-m <MODULE>``; the interpreter itself comes from the Manager spec.
RECLAIM_PYTHON = "/opt/cortex/venv/bin/python"
#: Where the helper looks for its approval.  This is deliberately a constant and
#: not an argument or environment value: a builder invoking the CLI controls both.
#: It is the builder spec spool the installed Manager writes to (the installed
#: runtime does not override ``PSC_JOB_SPEC_SPOOL``); the Manager half refuses to
#: start a reclaim when its own spool differs, instead of producing an approval
#: that the helper would never find.
APPROVAL_SPEC_SPOOL = job_runner.DEFAULT_JOB_SPEC_SPOOL
APPROVAL_MAX_BYTES = 1024 * 1024
#: Per-instance preserve area inside the builder-writable commit-spool slot.  The
#: Manager moves each verified archive out of it into the Manager-only evidence
#: tree, so the slot stays reusable and the builder cannot touch the evidence.
RECLAIM_ARCHIVE_DIRNAME = "reclaim-preserved"
RECLAIM_LOG_FILENAME = "reclaim.jsonl"
RECLAIM_UNIT_TIMEOUT_SECONDS = 1800
PRESERVE_FILE_MAX_BYTES = 4 * 1024 * 1024
PRESERVE_FILE_MAX_COUNT = 512
PRESERVE_BUNDLE_MAX_BYTES = 256 * 1024 * 1024
#: Builder-created archive directories keep an ACL mask that includes ``w`` so the
#: Manager's named entry (inherited from the preserve area) can move and later
#: prune them; files only need to be readable by the Manager.
ARCHIVE_DIR_MODE = 0o770
ARCHIVE_FILE_MODE = 0o640
_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


def marker_digest(marker: Mapping[str, object]) -> str:
    """SHA-256 of the canonical JSON form of a parsed workspace marker."""

    payload = json.dumps(
        dict(marker), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def reclaim_arguments(
    *,
    workspace: str | Path,
    pool_root: str | Path,
    preserve_root: str | Path,
    marker_sha256: str,
    nonce: str,
) -> list[str]:
    """The helper argv after ``-m MODULE``; the approval must carry it verbatim."""

    return [
        "--workspace", str(workspace),
        "--pool-root", str(pool_root),
        "--preserve-root", str(preserve_root),
        "--marker-sha256", marker_sha256,
        "--nonce", nonce,
    ]


def reclaim_evidence_root() -> Path:
    """Manager-only durable home of preserved reclaim archives."""

    return paths.coordinator_root() / "evidence" / "worktree-reclaim"


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
    paths_found: list[str] = []
    index = 0
    while index < len(records):
        row = records[index]
        index += 1
        if len(row) < 4:
            raise RuntimeError("dirty scan returned malformed status")
        name = row[3:].decode("utf-8", "surrogateescape")
        # Rename/copy records contain a second NUL-delimited path.
        if row[:2].decode("ascii", "ignore").strip() in {"R", "C"} and index < len(records):
            paths_found.append(records[index].decode("utf-8", "surrogateescape"))
            index += 1
        paths_found.append(name)
    return list(dict.fromkeys(paths_found))


def _safe_relative(name: str) -> Path:
    rel = PurePosixPath(name)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise RuntimeError("dirty scan returned a path outside the workspace")
    return Path(*rel.parts)


def _archive_dir(path: Path) -> None:
    path.mkdir(mode=ARCHIVE_DIR_MODE, parents=False, exist_ok=False)
    os.chmod(path, ARCHIVE_DIR_MODE)


def _preserve(workspace: Path, names: Sequence[str], preserve_root: Path) -> tuple[Path, int]:
    if len(names) > PRESERVE_FILE_MAX_COUNT:
        raise RuntimeError("dirty workspace exceeds the preserve file-count limit")
    entries: list[tuple[str, Path, os.stat_result]] = []
    for name in names:
        relative = _safe_relative(name)
        source = workspace / relative
        info = source.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_size > PRESERVE_FILE_MAX_BYTES:
            raise RuntimeError("dirty workspace file exceeds the preserve size limit")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RuntimeError("unsupported dirty workspace entry")
        entries.append((name, source, info))
    archive = preserve_root / f"{workspace.name}-{uuid4().hex}"
    _archive_dir(archive)
    copied = 0
    try:
        for name, source, info in entries:
            relative = _safe_relative(name)
            destination = archive / relative
            parent = archive
            for part in relative.parts[:-1]:
                parent = parent / part
                if not parent.is_dir():
                    _archive_dir(parent)
            if stat.S_ISLNK(info.st_mode):
                os.symlink(os.readlink(source), destination)
            elif stat.S_ISREG(info.st_mode):
                if source.stat().st_size > PRESERVE_FILE_MAX_BYTES:
                    raise RuntimeError("dirty workspace file grew beyond the preserve size limit")
                shutil.copyfile(source, destination, follow_symlinks=False)
                os.chmod(destination, ARCHIVE_FILE_MODE)
            elif stat.S_ISDIR(info.st_mode):
                if not destination.is_dir():
                    _archive_dir(destination)
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
    bundle = archive / "workspace-head.bundle"
    result = subprocess.run(
        ["git", "-C", str(workspace), "bundle", "create", str(bundle), "HEAD", f"^{base}"],
        capture_output=True,
        check=False,
        timeout=job_workspace.WORKSPACE_DIRTY_SCAN_TIMEOUT_SECONDS,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace")[-500:]
        raise RuntimeError(f"workspace commit preservation failed: {detail}")
    if not bundle.is_file() or bundle.stat().st_size > PRESERVE_BUNDLE_MAX_BYTES:
        raise RuntimeError("workspace commit bundle is absent or exceeds the size limit")
    os.chmod(bundle, ARCHIVE_FILE_MODE)
    return str(bundle)


def reclaim_owned_workspace(
    *,
    workspace: str | Path,
    pool_root: str | Path,
    preserve_root: str | Path,
    expected_marker_sha256: str,
) -> dict[str, object]:
    """Scan, preserve, and clear one marker-proven builder clone.

    The pool parent remains Manager-owned; the caller removes the now-empty
    workspace after this function succeeds.  Authorization is not decided here:
    :func:`main` verifies the Manager approval before calling this function.
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
    if not isinstance(observed, dict) or marker_digest(observed) != expected_marker_sha256:
        raise RuntimeError("workspace marker does not match the Manager-approved digest")
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
            _archive_dir(archive)
        _archive_commit(resolved, archive, base)
    # Re-read ownership immediately before mutation.  This rejects marker swaps
    # between the first validation and the destructive phase.
    current = job_workspace.read_marker(resolved)
    if not isinstance(current, dict) or marker_digest(current) != expected_marker_sha256:
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


def verify_manager_approval(
    *,
    argv: Sequence[str],
    workspace: str | Path,
    approval_spool: str | Path,
    manager_uid: int,
) -> dict[str, object]:
    """Require the Manager-authored job spec that authorizes exactly this call.

    The approval lives at ``<approval_spool>/<workspace name>.json`` (the unit
    instance of an existing pool slot is its directory name).  It must be a
    regular file owned by the Manager with no group/other write bit, in a
    Manager-owned spool without group/other write bits, and it must name this
    workspace and carry ``-m MODULE`` followed by exactly ``argv``.
    """

    target = Path(workspace)
    if target.is_symlink() or not target.is_dir():
        raise RuntimeError("workspace is not a real directory")
    resolved = target.resolve(strict=True)
    instance = resolved.name
    if not job_runner.instance_name_valid(instance):
        raise RuntimeError("workspace name is not a builder unit instance")
    spool = Path(approval_spool)
    spool_info = os.lstat(spool)
    if not stat.S_ISDIR(spool_info.st_mode):
        raise RuntimeError("Manager approval spool is not a real directory")
    if spool_info.st_uid != manager_uid or stat.S_IMODE(spool_info.st_mode) & 0o022:
        raise RuntimeError("Manager approval spool is not controlled by the Manager")
    approval = spool / f"{instance}.json"
    try:
        fd = os.open(approval, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise RuntimeError("no Manager approval exists for this workspace") from None
    except OSError as exc:
        raise RuntimeError(f"Manager approval is unreadable or not a regular file: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Manager approval is not a regular file")
        if info.st_uid != manager_uid:
            raise RuntimeError("approval is not Manager-authored")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeError("approval is writable by a principal other than the Manager")
        raw = stream.read(APPROVAL_MAX_BYTES + 1)
    if len(raw) > APPROVAL_MAX_BYTES:
        raise RuntimeError("Manager approval exceeds its size limit")
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError("Manager approval is not valid JSON") from None
    if not isinstance(spec, dict) or spec.get("spec_version") != job_runner.JOB_SPEC_VERSION:
        raise RuntimeError("Manager approval is not a job spec")
    if spec.get("instance") != instance or spec.get("working_directory") != str(resolved):
        raise RuntimeError("Manager approval names a different workspace")
    command = spec.get("command")
    if (
        not isinstance(command, list)
        or command[1:3] != ["-m", MODULE]
        or command[3:] != list(argv)
    ):
        raise RuntimeError("Manager approval does not authorize this reclaim invocation")
    return spec


def _manager_uid() -> int:
    account = DEFAULT_SCHEME.durable_state_owner
    if not account:
        raise RuntimeError("the installed UID scheme has no Manager durable-state owner")
    return pwd.getpwnam(account).pw_uid


def main(
    argv: Sequence[str] | None = None,
    *,
    approval_spool: str | Path | None = None,
    manager_uid: int | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog=f"python -m {MODULE}")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--pool-root", required=True)
    parser.add_argument("--preserve-root", required=True)
    parser.add_argument("--marker-sha256", required=True)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args(arguments)
    try:
        if _SHA256_RE.fullmatch(args.marker_sha256) is None:
            raise RuntimeError("marker digest is malformed")
        if _NONCE_RE.fullmatch(args.nonce) is None:
            raise RuntimeError("reclaim nonce is malformed")
        verify_manager_approval(
            argv=arguments,
            workspace=args.workspace,
            approval_spool=approval_spool if approval_spool is not None else APPROVAL_SPEC_SPOOL,
            manager_uid=manager_uid if manager_uid is not None else _manager_uid(),
        )
        result = reclaim_owned_workspace(
            workspace=args.workspace,
            pool_root=args.pool_root,
            preserve_root=args.preserve_root,
            expected_marker_sha256=args.marker_sha256,
        )
        result["nonce"] = args.nonce
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except (KeyError, OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"owner reclaim failed: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Manager half
# ---------------------------------------------------------------------------


def _start_builder_unit(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """``systemctl start --wait`` for the approved instance (test seam)."""

    return subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=RECLAIM_UNIT_TIMEOUT_SECONDS,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )


def _grant_archive_acl(archive_root: Path, *, manager_account: str, builder_account: str) -> None:
    """Let the builder create the archive and let the Manager read, move and prune it."""

    acl = shutil.which(job_workspace.SETFACL_PROGRAM)
    if acl is None:
        raise RuntimeError("setfacl is unavailable for the reclaim archive boundary")
    subprocess.run(
        [
            acl, "-m",
            f"u:{manager_account}:rwx,u:{builder_account}:rwx,"
            f"d:u:{manager_account}:rwx,d:u:{builder_account}:rwx",
            str(archive_root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _refuse_unreviewed_archive(archive_root: Path) -> None:
    """An archive left by an earlier, unfinished reclaim is evidence: never reset it."""

    if archive_root.is_symlink():
        raise RuntimeError("reclaim archive area is a symlink")
    if archive_root.is_dir() and any(archive_root.iterdir()):
        raise RuntimeError(
            "an earlier reclaim archive for this workspace is still present; "
            "inspect it before retrying"
        )


def _builder_units_for_instance(env: Mapping[str, str], instance: str) -> list[str]:
    """Every builder template unit that can run with this ``%i``.

    Each of them mounts ``<pool>/%i`` and the instance's spools writable, so while
    any one is active a process other than the reclaim helper could write the
    workspace, the preserve area or the reclaim log.
    """

    base = job_runner.resolve_template_unit(env, role=job_runner.JOB_ROLE_BUILDER)
    units: list[str] = []
    for read_only in (False, True):
        contract = job_runner.template_unit_for_workspace_contract(
            base, workspace_read_only=read_only
        )
        for profile in sorted(job_runner.TEMPLATE_UNIT_SUFFIX_BY_PROFILE):
            units.append(
                job_runner.template_unit_name(
                    instance, template=job_runner.template_unit_for_profile(contract, profile)
                )
            )
    return units


def _completion(log: Path, *, instance: str, nonce: str) -> dict[str, object]:
    """The helper's completion record: auxiliary, never the basis of a decision.

    The log is builder-writable and the nonce is readable by the builder in the
    spec, so exactly one nonce-matching record is required; a second one means
    something other than the helper wrote the log.
    """

    matches: list[dict[str, object]] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("nonce") == nonce:
            matches.append(record)
    if not matches:
        raise RuntimeError("builder reclaim unit returned no completion record for this invocation")
    if len(matches) > 1:
        raise RuntimeError(
            "builder reclaim log holds more than one completion record for this invocation"
        )
    [record] = matches
    if record.get("status") != "cleared" or record.get("workspace_name") != instance:
        raise RuntimeError("builder reclaim completion does not describe this workspace")
    return record


def _adopt_preserved(archive_root: Path, *, instance: str) -> list[Path]:
    """Move every archive the unit left behind into Manager-only evidence.

    What is adopted is decided from what the Manager observes in the sealed
    preserve area, not from the builder-writable completion record, so a forged
    record cannot make the Manager leave an archive behind.  Entries the helper
    never creates are left in place and fail the reclaim.
    """

    name = re.compile(rf"^{re.escape(instance)}-[0-9a-f]{{32}}$")
    evidence_root = reclaim_evidence_root()
    evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    adopted: list[Path] = []
    unexpected: list[str] = []
    for entry in sorted(archive_root.iterdir()):
        info = entry.lstat()
        destination = evidence_root / entry.name
        if (
            not stat.S_ISDIR(info.st_mode)
            or name.fullmatch(entry.name) is None
            or os.path.lexists(destination)
        ):
            unexpected.append(entry.name)
            continue
        os.rename(entry, destination)
        adopted.append(destination)
    if unexpected:
        raise RuntimeError(
            "the reclaim preserve area holds entries the helper does not create: "
            + ", ".join(sorted(unexpected))[:300]
        )
    return adopted


_BUNDLE_SIGNATURES = (b"# v2 git bundle\n", b"# v3 git bundle\n")


def _inspect_archive(archive: Path) -> tuple[int, bool]:
    """Manager's own reading of an adopted archive: (preserved entries, has bundle).

    Preserved entries are what ``_preserve`` creates for dirty paths: regular
    files, symlinks and (for an untracked nested repository) empty directories.
    """

    entries = 0
    for directory, child_dirs, child_files in os.walk(archive, followlinks=False):
        for child in (*child_dirs, *child_files):
            item = Path(directory) / child
            info = item.lstat()
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                entries += 1
            elif stat.S_ISDIR(info.st_mode):
                if not any(item.iterdir()):
                    entries += 1
            else:
                raise RuntimeError("reclaim archive holds an unsupported entry")
    bundle = archive / "workspace-head.bundle"
    has_bundle = os.path.lexists(bundle)
    if has_bundle:
        if bundle.is_symlink() or not bundle.is_file():
            raise RuntimeError("reclaim archive commit bundle is not a regular file")
        with bundle.open("rb") as stream:
            if stream.read(16) not in _BUNDLE_SIGNATURES:
                raise RuntimeError("reclaim archive commit bundle is not a git bundle")
        entries -= 1
    return entries, has_bundle


def _reconcile(record: Mapping[str, object], adopted: Sequence[Path]) -> dict[str, object]:
    """Cross-check the completion record against the archives the Manager adopted."""

    preserved = record.get("preserved_files")
    if not isinstance(preserved, int) or isinstance(preserved, bool) or preserved < 0:
        raise RuntimeError("builder reclaim completion has an invalid preserve count")
    preserved_commit = record.get("preserved_commit") is True
    claimed = record.get("preserve_path")
    if len(adopted) > 1:
        raise RuntimeError("the reclaim preserve area held more than one archive")
    if not adopted:
        if preserved or preserved_commit or claimed is not None:
            raise RuntimeError(
                "builder reclaim completion claims preserved content the Manager did not find"
            )
        return {**record, "preserve_path": None}
    [archive] = adopted
    if not isinstance(claimed, str) or Path(claimed).name != archive.name:
        raise RuntimeError("builder reclaim completion does not name the archive the Manager adopted")
    entries, has_bundle = _inspect_archive(archive)
    if entries != preserved or has_bundle != preserved_commit:
        raise RuntimeError("adopted reclaim archive does not match the completion record")
    return {**record, "preserve_path": str(archive)}


def reclaim_through_builder_unit(
    *, workspace: str | Path, marker: Mapping[str, object]
) -> dict[str, object]:
    """Run the fixed helper under the installed builder template for one pool slot.

    The unit instance is the slot's own name (it was the instance of the job
    that used the slot), so the template's ``ReadWritePaths=<pool>/%i`` covers
    exactly this workspace and nothing else.  The job spec written for the unit
    is the helper's approval and is removed as soon as the unit finishes.
    """

    env = dict(os.environ)
    workspace_path = Path(workspace).resolve(strict=True)
    instance = workspace_path.name
    if not job_runner.instance_name_valid(instance):
        raise RuntimeError("workspace name cannot be used as a builder unit instance")
    pool_root = paths.worktree_root().resolve(strict=True)
    if workspace_path.parent != pool_root:
        raise RuntimeError("workspace is outside the configured job pool")
    source_repo = marker.get("source_repo")
    if not isinstance(source_repo, str) or not source_repo.startswith("/"):
        raise RuntimeError("workspace marker has no absolute source repository")
    executor = (env.get("PSC_MANAGER_EXECUTOR") or "").strip()
    if not executor:
        raise RuntimeError("PSC_MANAGER_EXECUTOR is required for the builder reclaim unit")
    plan = job_runner.prepare_systemd_template(
        env,
        job_id=instance,
        instance=instance,
        executor=executor,
        role=job_runner.JOB_ROLE_BUILDER,
    )
    if plan.instance != instance:
        raise RuntimeError("builder reclaim unit instance differs from the pool slot")
    if Path(plan.spool_dir) != Path(APPROVAL_SPEC_SPOOL):
        raise RuntimeError(
            "the builder spec spool differs from the approval spool the reclaim helper trusts"
        )
    manager_account = DEFAULT_SCHEME.durable_state_owner
    if not manager_account:
        raise RuntimeError("the installed UID scheme has no Manager durable-state owner")
    # prepare_systemd_template() only checked the stem it selected.  A builder job
    # for this slot under another stem (other hardening profile, read-only
    # template) would share every writable surface with the helper.
    busy = [
        unit
        for unit in _builder_units_for_instance(env, instance)
        if job_runner._unit_is_active(plan.binary, unit)
    ]
    if busy:
        raise RuntimeError(
            "another builder unit for this workspace is still active: " + ", ".join(busy)
        )
    # The job environment is validated (PATH/HOME contract, git trust) before any
    # side effect, like every other pre-launch check.
    job_env = job_runner.build_job_env(
        manager_env=env,
        job_id=instance,
        slice_id=instance,
        repo_root=source_repo,
        workspace=workspace_path,
        role=job_runner.JOB_ROLE_BUILDER,
    )
    job_env["CODEX_HOME"] = str(spool_slot.exact_job_slot("builder-codex-home", instance))
    job_env["XDG_CACHE_HOME"] = str(spool_slot.exact_job_slot("builder-runtime-cache", instance))
    # Every ReadWritePaths= of the template must exist or systemd fails the
    # namespace setup (226/NAMESPACE) before the helper runs.  The reclaim runs
    # no model, so no credential is copied into its runtime home.
    spool_slot.provision_runtime_surfaces(
        principal="builder",
        instance=instance,
        canonical_codex_home=spool_slot.canonical_codex_controls("builder", manager_env=env),
        account=plan.account,
        seed_credential=False,
    )
    commit_slot = spool_slot.exact_job_slot("commit-spool", instance)
    archive_root = commit_slot / RECLAIM_ARCHIVE_DIRNAME
    _refuse_unreviewed_archive(archive_root)
    spool_slot.create_slot(commit_slot, reset=True)
    archive_root.mkdir(mode=0o700)
    # Manager can inspect and move the preserved evidence; builder can create only
    # in this per-instance slot.  No ACL is added to the workspace for Manager.
    _grant_archive_acl(
        archive_root, manager_account=manager_account, builder_account=plan.account
    )
    # The reclaim log lives in the commit-spool slot that was just reset, not in
    # the instance's build-log slot: resetting that one would erase the log of
    # the builder job whose workspace is being reclaimed.
    log = spool_slot.preseed_job_writable_file(commit_slot / RECLAIM_LOG_FILENAME)
    nonce = secrets.token_hex(16)
    command = [
        RECLAIM_PYTHON,
        "-m",
        MODULE,
        *reclaim_arguments(
            workspace=workspace_path,
            pool_root=pool_root,
            preserve_root=archive_root,
            marker_sha256=marker_digest(marker),
            nonce=nonce,
        ),
    ]
    spec = job_runner.build_job_spec(
        job_id=instance,
        instance=instance,
        unit=plan.unit,
        command=command,
        working_directory=str(workspace_path),
        log_path=str(log),
        env=job_env,
    )
    try:
        job_runner.write_job_spec(plan.spec_path, spec, account=plan.account)
        completed = _start_builder_unit(
            job_runner.build_systemctl_start_argv(systemctl=plan.binary, unit=plan.unit)
        )
    finally:
        # The spec is the helper's approval: single use, whatever happened.
        Path(plan.spec_path).unlink(missing_ok=True)
    # From here on nothing a builder process wrote decides anything.  Seal the
    # slot first: its ACL mask drops to ---, so no builder process can reach the
    # preserve area or the log any more.
    if not spool_slot.seal_slot(commit_slot):
        raise RuntimeError("the reclaim spool slot could not be sealed")
    # Evidence first, whatever the unit reported: every archive the unit left is
    # moved out of the builder's reach before any outcome is decided.
    adopted = _adopt_preserved(archive_root, instance=instance)
    if completed.returncode:
        # The helper's own refusal is on its stderr, which the shim sent to the log.
        try:
            log_tail = log.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            log_tail = ""
        detail = (
            job_runner.read_shim_error(str(log))
            or log_tail
            or completed.stderr
            or completed.stdout
        )
        raise RuntimeError(
            f"builder reclaim unit failed rc={completed.returncode}: {str(detail)[-500:]}"
        )
    # systemd reported success; the workspace must be observably empty before the
    # caller may remove it.  The Manager owns the slot directory, so it can list it.
    if any(workspace_path.iterdir()):
        raise RuntimeError("workspace is not empty after the builder reclaim unit")
    return _reconcile(_completion(log, instance=instance, nonce=nonce), adopted)

if __name__ == "__main__":  # pragma: no cover - installed helper entry point
    raise SystemExit(main())
