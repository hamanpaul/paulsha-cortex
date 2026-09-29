"""Root-gated local backend for the trust-root transaction engine."""
from __future__ import annotations

import base64
import configparser
import ctypes
import errno
import functools
import grp
import hashlib
import json
import os
import platform
import posixpath
import pwd
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .core import (
    ASSET_PRIOR_SNAPSHOT_MAX_BYTES,
    InstallDriftError,
    InstallError,
    InstallPlanError,
    InstallReceipt,
    QuarantineSubstitutionError,
    UnsafeInstallPathError,
    _DIRECTORY_OPEN_FLAGS,
    _account_digest,
    _assert_fd_path_binding,
    _desired_digest,
    _directory_inventory_sha256,
    _open_directory_chain,
    _observed_drift_detail,
    _open_parent_directory,
    _read_fd_bytes,
    _reject_symlink_ancestors,
    credential_destination,
)


def _run(
    argv: Sequence[str],
    *,
    check: bool = False,
    input_text: str | None = None,
    pass_fds: Sequence[int] = (),
    env: Mapping[str, str] | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one typed argv.  Shell text is never accepted by this backend."""

    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise InstallPlanError(f"invalid argv: {argv!r}")
    identity: dict[str, object] = {}
    if uid is not None or gid is not None:
        target_uid = os.geteuid() if uid is None else uid
        target_gid = os.getegid() if gid is None else gid
        if os.geteuid() == 0:
            identity = {
                "user": target_uid,
                "group": target_gid,
                "extra_groups": (),
            }
        elif target_uid != os.geteuid() or target_gid != os.getegid():
            raise PermissionError("cannot run command as the requested repository owner")
    result = subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        input=input_text,
        pass_fds=tuple(pass_fds),
        env=None if env is None else dict(env),
        **identity,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise InstallError(f"{argv[0]} failed ({result.returncode}): {detail}")
    return result


def _account_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _resolve_uid(value: object) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value:
        raise InstallPlanError(f"invalid owner: {value!r}")
    try:
        return int(value) if value.isdigit() else pwd.getpwnam(value).pw_uid
    except KeyError as exc:
        raise InstallPlanError(f"unknown owner account: {value}") from exc


def _resolve_gid(value: object) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value:
        raise InstallPlanError(f"invalid group: {value!r}")
    try:
        return int(value) if value.isdigit() else grp.getgrnam(value).gr_gid
    except KeyError as exc:
        raise InstallPlanError(f"unknown group: {value}") from exc


def _mode(value: object) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[0-7]{4}", value):
        raise InstallPlanError(f"invalid mode: {value!r}")
    return int(value, 8)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_open_file(descriptor: int) -> str:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise InstallDriftError("opened source is not a single-link regular file")
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    after = os.fstat(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise InstallDriftError("opened source changed during hashing")
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    """Hash every installed venv leaf, including relative names and symlinks."""

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda row: row.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == ".cortex-tree.sha256":
            continue
        observed = path.lstat()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(format(stat.S_IMODE(observed.st_mode), "04o").encode("ascii") + b"\0")
        if stat.S_ISLNK(observed.st_mode):
            digest.update(b"L\0" + os.readlink(path).encode("utf-8") + b"\0")
        elif stat.S_ISREG(observed.st_mode):
            digest.update(b"F\0" + _sha256_file(path).encode("ascii") + b"\0")
        elif stat.S_ISDIR(observed.st_mode):
            digest.update(b"D\0")
        else:
            raise InstallDriftError(f"venv contains unsupported filesystem object: {path}")
    return digest.hexdigest()


def _copy_verified_file(source: Path, destination: Path, expected: str) -> None:
    """Copy one locked artifact from a no-follow descriptor and verify in-flight."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise InstallDriftError(f"locked artifact is not a single-link regular file: {source}")
        digest = hashlib.sha256()
        with os.fdopen(source_fd, "rb", closefd=False) as input_stream, destination.open("xb") as output_stream:
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        after = os.fstat(source_fd)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ) or digest.hexdigest() != expected:
            raise InstallDriftError(f"locked artifact changed while being copied: {source}")
    finally:
        os.close(source_fd)


def _relocate_venv_shebangs(temporary: Path, slot: Path) -> None:
    """Retarget console scripts before the hidden venv is atomically renamed."""

    source_prefix = os.fsencode(f"#!{temporary}/")
    destination_prefix = os.fsencode(f"#!{slot}/")
    for path in sorted((temporary / "bin").iterdir(), key=lambda row: row.name):
        observed = path.lstat()
        if stat.S_ISLNK(observed.st_mode):
            continue
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise InstallDriftError(f"venv bin contains an unsafe object: {path}")
        with path.open("rb") as stream:
            prefix = stream.read(len(source_prefix))
        if prefix != source_prefix:
            continue
        payload = path.read_bytes()
        with path.open("r+b") as stream:
            stream.write(destination_prefix)
            stream.write(payload[len(source_prefix) :])
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())


def _account_state(step: Mapping[str, object]) -> dict[str, object]:
    name = step.get("name")
    if not isinstance(name, str) or not name:
        raise InstallPlanError("account step requires a name")
    try:
        account = pwd.getpwnam(name)
    except KeyError:
        try:
            group = grp.getgrnam(name)
        except KeyError:
            return {"exists": False, "group_exists": False}
        return {
            "exists": False,
            "group_exists": True,
            "group_gid": group.gr_gid,
            "group_members": sorted(set(getattr(group, "gr_mem", ()))),
        }
    try:
        group = grp.getgrnam(name)
    except KeyError:
        return {"exists": True, "installed_sha256": None}
    observed = {
        "name": name,
        "uid": account.pw_uid,
        "gid": account.pw_gid,
        "home": account.pw_dir,
        "login_program": account.pw_shell,
    }
    if group.gr_gid != account.pw_gid:
        return {"exists": True, **observed, "installed_sha256": None}
    return {
        "exists": True,
        **observed,
        "installed_sha256": _account_digest(observed),
    }


def _venv_state(step: Mapping[str, object]) -> dict[str, object]:
    slot = Path(str(step.get("path", "")))
    active = Path(str(step.get("active_link", "")))
    expected = step.get("wheel_sha256")
    if not slot.is_absolute() or not active.is_absolute() or not isinstance(expected, str):
        raise InstallPlanError("venv step requires absolute slot/link and wheel hash")
    try:
        observed = slot.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        return {"exists": True, "installed_sha256": None, "path": str(slot)}
    tree_sha256 = _tree_sha256(slot)
    slot_matches = _venv_slot_matches(slot, expected, tree_sha256=tree_sha256)
    try:
        link_target = active.readlink()
    except (OSError, ValueError):
        return {
            "exists": True,
            "installed_sha256": None,
            "slot_sha256": expected if slot_matches else None,
            "path": str(slot),
            "tree_sha256": tree_sha256,
        }
    resolved_target = (active.parent / link_target).resolve(strict=False)
    if not slot_matches or resolved_target != slot.resolve(strict=False):
        return {
            "exists": True,
            "installed_sha256": None,
            "slot_sha256": expected if slot_matches else None,
            "path": str(slot),
            "tree_sha256": tree_sha256,
        }
    return {
        "exists": True,
        "installed_sha256": expected,
        "slot_sha256": expected,
        "path": str(slot),
        "tree_sha256": tree_sha256,
        "link_target": str(link_target),
    }


def _venv_activation_state(step: Mapping[str, object]) -> dict[str, object]:
    """Snapshot the rollback-relevant active-link state without following it."""

    active = Path(str(step.get("active_link", "")))
    if not active.is_absolute():
        raise InstallPlanError("venv step requires an absolute active link")
    try:
        observed = active.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if not stat.S_ISLNK(observed.st_mode):
        return {"exists": True, "is_symlink": False}
    try:
        target = os.readlink(active)
    except OSError as exc:
        raise InstallDriftError(f"active venv link cannot be inspected: {active}") from exc
    return {"exists": True, "is_symlink": True, "link_target": target}


def _venv_slot_matches(
    slot: Path, expected: str, *, tree_sha256: str | None = None
) -> bool:
    marker = slot / ".cortex-wheel.sha256"
    tree_marker = slot / ".cortex-tree.sha256"
    try:
        return (
            slot.is_dir()
            and not slot.is_symlink()
            and marker.is_file()
            and not marker.is_symlink()
            and marker.read_text(encoding="ascii").strip() == expected
            and tree_marker.is_file()
            and not tree_marker.is_symlink()
            and tree_marker.read_text(encoding="ascii").strip()
            == (tree_sha256 if tree_sha256 is not None else _tree_sha256(slot))
            and (slot / "bin/python").is_file()
        )
    except OSError:
        return False


def _venv_staging_path(slot: Path) -> Path:
    return slot.with_name(f".{slot.name}.cortex-staging")


def _venv_staging_authority(
    slot: Path,
    staging: Path,
    *,
    state: str,
    tree_sha256: str | None = None,
) -> dict[str, object]:
    authority: dict[str, object] = {
        "state": state,
        "path": str(slot),
        "staging_path": str(staging),
    }
    if state in {"building", "ready"}:
        observed = (staging if staging.exists() else slot).lstat()
        authority.update({"device": observed.st_dev, "inode": observed.st_ino})
    if state == "ready":
        if not isinstance(tree_sha256, str):
            raise InstallError("ready venv authority requires a tree hash")
        authority["tree_sha256"] = tree_sha256
    return authority


def _cleanup_bound_venv_staging(
    step: Mapping[str, object], authority: object
) -> None:
    """Remove only the exact receipt-bound unpublished staging directory."""

    if not isinstance(authority, Mapping) or "state" not in authority:
        return
    slot = Path(str(step.get("path", "")))
    staging = _venv_staging_path(slot)
    if (
        authority.get("path") != str(slot)
        or authority.get("staging_path") != str(staging)
    ):
        raise InstallDriftError("venv staging authority path drifted")
    try:
        observed = staging.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise InstallDriftError("venv staging authority is unsafe")
    state = authority.get("state")
    if state in {"building", "ready"} and (
        authority.get("device") != observed.st_dev
        or authority.get("inode") != observed.st_ino
    ):
        raise InstallDriftError("venv staging inode authority drifted")
    if state == "ready" and authority.get("tree_sha256") != _tree_sha256(staging):
        raise InstallDriftError("venv staging tree authority drifted")
    if state not in {"planned", "building", "ready"}:
        raise InstallDriftError("venv staging authority state is invalid")
    shutil.rmtree(staging)
    parent_fd = os.open(staging.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _read_acl(path: Path) -> list[dict[str, object]]:
    if shutil.which("getfacl") is None:
        raise InstallDriftError("getfacl is unavailable; ACL state is untrusted")
    # ``-E`` suppresses the ``\t#effective:...`` annotation getfacl appends
    # when the ACL mask narrows an entry.  The recorded perms must be the
    # entry's own bits: those are what ``setfacl -m`` restores on rollback.
    result = _run(("getfacl", "-cpE", str(path)))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise InstallDriftError(
            f"getfacl failed while inspecting ACL for {path} ({result.returncode}): {detail}"
        )
    # Defensive: strip any trailing comment even if a getfacl build ignores
    # ``-E``.  POSIX ACL entry text never contains ``#``.
    lines = [raw.partition("#")[0].strip() for raw in result.stdout.splitlines()]
    rows: list[dict[str, object]] = []
    for raw in lines:
        default = raw.startswith("default:")
        body = raw.removeprefix("default:")
        entry_type, separator, remainder = body.partition(":")
        if not separator or entry_type not in {"user", "group", "mask", "other"}:
            continue
        account, separator, perms = remainder.partition(":")
        if not separator:
            continue
        if not account and not default and entry_type in {"user", "other"}:
            continue
        if not account and entry_type == "group" and not (
            default or any(
                candidate.startswith(("user:", "group:"))
                and candidate.split(":", 2)[1]
                for candidate in lines
            )
        ):
            continue
        row: dict[str, object] = {
            "account": account,
            "perms": perms.replace("-", ""),
            "default": default,
        }
        if entry_type != "user" or not account:
            row["entry_type"] = entry_type
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            bool(row["default"]),
            str(row.get("entry_type", "user")),
            str(row["account"]),
            str(row["perms"]),
        ),
    )


def _read_prior_file(path: Path, observed: os.stat_result) -> bytes:
    """Read a managed file's prior bytes through a bounded no-follow descriptor."""

    def too_large(size: int) -> InstallDriftError:
        return InstallDriftError(
            f"existing managed file is too large to snapshot for rollback: {path} "
            f"is {size} bytes (limit {ASSET_PRIOR_SNAPSHOT_MAX_BYTES}); move it "
            "aside before apply"
        )

    if observed.st_size > ASSET_PRIOR_SNAPSHOT_MAX_BYTES:
        raise too_large(observed.st_size)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeInstallPathError(
                f"install asset must not be a symlink: {path}"
            ) from exc
        raise InstallDriftError(f"cannot snapshot managed file {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            observed.st_dev,
            observed.st_ino,
        ):
            raise InstallDriftError(f"managed file changed during snapshot: {path}")
        chunks: list[bytes] = []
        remaining = ASSET_PRIOR_SNAPSHOT_MAX_BYTES + 1
        while remaining > 0 and (chunk := os.read(descriptor, min(remaining, 1024 * 1024))):
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > ASSET_PRIOR_SNAPSHOT_MAX_BYTES:
            raise too_large(len(content))
        return content
    finally:
        os.close(descriptor)


def _snapshot(
    path: Path,
    *,
    capture_content: bool = True,
    inventory_sink: Callable[[str, tuple[str, ...]], None] | None = None,
) -> dict[str, object]:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if stat.S_ISLNK(observed.st_mode):
        raise UnsafeInstallPathError(f"install asset must not be a symlink: {path}")
    snapshot: dict[str, object] = {
        "exists": True,
        "is_directory": stat.S_ISDIR(observed.st_mode),
        "owner": _account_name(observed.st_uid),
        "group": _group_name(observed.st_gid),
        "mode": format(stat.S_IMODE(observed.st_mode), "04o"),
        "acl": _read_acl(path),
    }
    if stat.S_ISDIR(observed.st_mode):
        snapshot["device"] = observed.st_dev
        snapshot["inode"] = observed.st_ino
        # Only the digest and count enter the state (and thus every receipt
        # checkpoint); the full list is bound to the receipt as a side file
        # when this state becomes rollback authority.
        rows = tuple(_directory_inventory(path))
        digest = _directory_inventory_sha256(rows)
        snapshot["children_count"] = len(rows)
        snapshot["children_sha256"] = digest
        if inventory_sink is not None:
            inventory_sink(digest, rows)
        try:
            snapshot["is_mountpoint"] = path.is_mount()
        except OSError as exc:
            raise InstallDriftError(
                f"cannot determine managed directory mount status {path}: {exc}"
            ) from exc
    if stat.S_ISREG(observed.st_mode) and capture_content:
        content = _read_prior_file(path, observed)
        snapshot["content_base64"] = base64.b64encode(content).decode("ascii")
        snapshot["installed_sha256"] = hashlib.sha256(content).hexdigest()
    return snapshot


def _directory_inventory(path: Path) -> list[str]:
    """Record every descendant through stable, no-follow directory descriptors."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallDriftError(f"cannot inventory managed directory {path}: {exc}") from exc

    def walk(current_fd: int, prefix: str) -> list[str]:
        before = os.fstat(current_fd)
        if not stat.S_ISDIR(before.st_mode):
            raise InstallDriftError(f"inventory member is not a directory: {path / prefix}")
        with os.scandir(current_fd) as entries:
            children = sorted(
                (
                    entry.name,
                    entry.stat(follow_symlinks=False),
                )
                for entry in entries
            )
        rows: list[str] = []
        for name, child_state in children:
            relative = f"{prefix}/{name}" if prefix else name
            rows.append(relative)
            if not stat.S_ISDIR(child_state.st_mode):
                continue
            try:
                child_fd = os.open(name, flags, dir_fd=current_fd)
            except OSError as exc:
                raise InstallDriftError(
                    f"cannot inventory managed directory {path / relative}: {exc}"
                ) from exc
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (
                    child_state.st_dev,
                    child_state.st_ino,
                ):
                    raise InstallDriftError(
                        f"managed directory changed during inventory: {path / relative}"
                    )
                rows.extend(walk(child_fd, relative))
            finally:
                os.close(child_fd)
        after = os.fstat(current_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise InstallDriftError(
                f"managed directory changed during inventory: {path / prefix}"
            )
        return rows

    try:
        before = os.fstat(descriptor)
        rows = walk(descriptor, "")
        after = os.fstat(descriptor)
        observed = path.lstat()
        if (
            not stat.S_ISDIR(before.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or (before.st_dev, before.st_ino) != (observed.st_dev, observed.st_ino)
        ):
            raise InstallDriftError(f"managed directory changed during inventory: {path}")
        return sorted(rows)
    except OSError as exc:
        raise InstallDriftError(f"cannot inventory managed directory {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _symlink_state(step: Mapping[str, object]) -> dict[str, object]:
    path = Path(str(step.get("path", "")))
    target = Path(str(step.get("target", "")))
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if not stat.S_ISLNK(observed.st_mode):
        return {"exists": True, "installed_sha256": None}
    link_target = Path(os.readlink(path))
    resolved = (path.parent / link_target).resolve(strict=False)
    matches = (
        target.is_absolute()
        and resolved == target.resolve(strict=False)
        and _account_name(observed.st_uid) == step.get("owner")
        and _group_name(observed.st_gid) == step.get("group")
    )
    return {
        "exists": True,
        "owner": _account_name(observed.st_uid),
        "group": _group_name(observed.st_gid),
        "target": str(link_target),
        "installed_sha256": step.get("desired_sha256") if matches else None,
    }


def _toolchain_state(step: Mapping[str, object]) -> dict[str, object]:
    path = Path(str(step.get("path", "")))
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    shape = step.get("shape", "file")
    if shape == "tree":
        entrypoint = path / str(step.get("entrypoint", ""))
        expected_uid = _resolve_uid(step.get("owner"))
        expected_gid = _resolve_gid(step.get("group"))
        content_matches = (
            stat.S_ISDIR(observed.st_mode)
            and not stat.S_ISLNK(observed.st_mode)
            and entrypoint.is_file()
            and not entrypoint.is_symlink()
            and _tree_sha256(path) == step.get("desired_sha256")
            and _tree_owned_and_nonwritable(path, expected_uid, expected_gid)
        )
    else:
        content_matches = (
            stat.S_ISREG(observed.st_mode)
            and observed.st_nlink == 1
            and _sha256_file(path) == step.get("desired_sha256")
        )
    matches = (
        content_matches
        and _account_name(observed.st_uid) == step.get("owner")
        and _group_name(observed.st_gid) == step.get("group")
        and format(stat.S_IMODE(observed.st_mode), "04o") == step.get("mode")
    )
    return {
        "exists": True,
        "owner": _account_name(observed.st_uid),
        "group": _group_name(observed.st_gid),
        "mode": format(stat.S_IMODE(observed.st_mode), "04o"),
        "installed_sha256": step.get("desired_sha256") if matches else None,
    }


def _locked_tree_manifest(archive: Path) -> dict[str, dict[str, object]]:
    """Validate and describe receipt-owned archive leaves without extracting."""

    with tarfile.open(archive, mode="r:*") as bundle:
        members = bundle.getmembers()
        manifest: dict[str, dict[str, object]] = {}
        for member in members:
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise InstallDriftError(f"toolchain archive has unsafe member: {member.name}")
            if not (member.isdir() or member.isfile() or member.issym()):
                raise InstallDriftError(f"toolchain archive has unsupported member: {member.name}")
            normalized = relative.as_posix().rstrip("/")
            if not normalized or normalized in manifest:
                raise InstallDriftError(
                    f"toolchain archive has duplicate or empty member: {member.name}"
                )
            if (member.isdir() or member.isfile()) and member.mode & 0o022:
                raise InstallDriftError(
                    f"toolchain archive member is group/other-writable: {member.name}"
                )
            row: dict[str, object] = {
                "kind": "directory" if member.isdir() else "file",
                "mode": member.mode & 0o777,
            }
            if member.issym():
                target = Path(member.linkname)
                depth = 0
                safe_target = not target.is_absolute()
                for component in (*relative.parent.parts, *target.parts):
                    if component in {"", "."}:
                        continue
                    if component == "..":
                        if depth == 0:
                            safe_target = False
                            break
                        depth -= 1
                    else:
                        depth += 1
                if not safe_target:
                    raise InstallDriftError(f"toolchain archive symlink escapes: {member.name}")
                row = {"kind": "symlink", "target": member.linkname}
            elif member.isfile():
                source = bundle.extractfile(member)
                if source is None:
                    raise InstallDriftError(
                        f"toolchain archive member is unreadable: {member.name}"
                    )
                digest = hashlib.sha256()
                with source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                row["sha256"] = digest.hexdigest()
            manifest[normalized] = row
        symlinks = {
            relative for relative, row in manifest.items() if row["kind"] == "symlink"
        }
        for relative in manifest:
            parents = Path(relative).parents
            if any(parent.as_posix() in symlinks for parent in parents if parent.as_posix() != "."):
                raise InstallDriftError(
                    f"toolchain archive member is nested below a symlink: {relative}"
                )
        for relative in tuple(manifest):
            for parent in Path(relative).parents:
                normalized = parent.as_posix()
                if normalized == ".":
                    break
                manifest.setdefault(
                    normalized, {"kind": "directory", "mode": None}
                )
        return manifest


def _extract_locked_tree(archive: Path, destination: Path) -> None:
    """Extract only validated regular files, directories, and in-tree symlinks."""

    _locked_tree_manifest(archive)
    with tarfile.open(archive, mode="r:*") as bundle:
        members = bundle.getmembers()
        for member in sorted((row for row in members if row.isdir()), key=lambda row: row.name):
            path = destination / member.name
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, member.mode & 0o777)
        for member in sorted((row for row in members if row.isfile()), key=lambda row: row.name):
            path = destination / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise InstallDriftError(f"toolchain archive member is unreadable: {member.name}")
            with source, path.open("xb") as stream:
                shutil.copyfileobj(source, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, member.mode & 0o777)
        for member in sorted((row for row in members if row.issym()), key=lambda row: row.name):
            path = destination / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(member.linkname)


_REPOSITORY_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "HOME": "/nonexistent",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": os.defpath,
}
_REPOSITORY_GIT_PREFIX = (
    "git",
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)


#: Auto gc/maintenance may prune worktree registries, expire reflogs, or pack
#: refs in the background.  Installer mutations of an existing source tree
#: never start it, so the Manager's runtime state stays exactly as inspected.
_REPOSITORY_MUTATION_CONFIG = (
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)


# ---------------------------------------------------------------------------
# #1124: Manager runtime state the repository step reconciles
# ---------------------------------------------------------------------------
#
# The repository step installs a detached clone of the candidate bundle at
# ``<repo-source-tree>/<slug>``, and that same checkout is the Manager's
# ``PSC_REPO_ROOT`` (``core._manager_environment``).  The Manager owns the tree
# (0817 ruling, ``docs/superpowers/specs/trust-root-isolation-spec.md``) and
# writes to it while jobs run; builder and gate jobs work in per-job clones
# under ``PSC_WORKTREE_ROOT`` and never write here.  A ``--prior-receipt``
# upgrade therefore reconciles exactly the runtime state declared below
# instead of refusing it.  Every other difference stays fail-closed, and the
# no-follow owner/group, world-writable, and symlink-escape walk still covers
# every member of the tree, the runtime state included.
#
# Each declared shape names the production writer it was derived from
# (package paths are relative to ``paulsha_cortex/``; ``qualification/`` is
# the release-candidate harness at the repository root):
#
# * git config ``user.name`` / ``user.email`` (plain single-line values):
#   ``coordinator/seams.py:351-355`` copies the source tree's *local* identity
#   into every per-job clone -- the builder's root-owned gitconfig carries none
#   -- and ``qualification/driver.py:3126-3142`` writes it, as the Manager, on
#   the checkout it registers under ``repo-source-tree``.
# * linked worktrees at ``.psc-review-worktrees/<slice>-<reviewer job>``:
#   ``coordinator/review.py:26,222-227`` (path) and ``review.py:510``
#   (``git worktree add --detach``); the reviewer only reads them.
# * linked worktrees at ``.psc-verification-worktrees/<slice>-<sha12>``:
#   ``coordinator/verification.py:29,748-749`` (path), ``:1131`` (add) and
#   ``:1201`` (remove).  A crash between the two leaves one behind.  A registry
#   entry whose directory is gone is inert and also accepted:
#   ``review.py:505-508`` deletes the directory even when
#   ``git worktree remove`` failed.
# * refs, by namespace:
#   ``refs/heads/feature/*`` -- ``coordinator/seams.py:249`` (``branch -f``)
#   and ``coordinator/job_workspace.py:1131-1132`` (bundle harvest); every
#   branch name is ``feature/<id>`` (``dispatcher.py:50``, ``autonomy.py:946``,
#   ``manager.py:4758``);
#   ``refs/remotes/origin/*`` -- ``fetch --no-tags origin <branch>`` under the
#   installer's own fetch refspec (``manager.py:1854``,
#   ``claim_readiness.py:354``, ``autonomy.py:1068``,
#   ``github_delivery.py:443-449``);
#   ``refs/tags/archive/<work>-<sha8>`` -- ``work_actions.py:6057,6075``;
#   ``refs/cortex/reclaimed/<workspace>/<stamp>-<sha12>`` --
#   ``job_workspace.py:118,1224-1225``;
#   ``refs/cortex/main-sync{,-quarantine}/<run>`` --
#   ``work_bridge.py:69-74,1403`` and ``manager.py:4991,5045``;
#   ``refs/cortex/mirror/<hash16>/{default,pull/<n>}`` --
#   ``monitor/git_mirror.py:133,211-215``.
#
# Deliberately *not* declared:
#
# * ``branch.<name>.*`` upstream config.  No trust-root writer exists:
#   ``seams.py:249`` branches from an exact commit id, which never records an
#   upstream, and the upstream that ``clone --branch`` records lives in the
#   per-job clone and is removed there (``seams.py:343-347``).  Upstream
#   config on a production checkout comes from the pre-#623 worktree/operator
#   era; that legacy checkout is quarantined and re-cloned by #1122.
# * linked worktrees anywhere else, including outside the source tree (the
#   pre-#623 pool worktrees are the same legacy state).  Out-of-tree
#   directories are never walked: their registry entry alone fails closed.
# * every other config section or key -- notably executable or redirecting
#   ones such as ``core.hooksPath``, ``core.fsmonitor``, ``include*``,
#   ``filter.*``, ``alias.*``, ``credential.*``, ``url.*``, ``extensions.*``
#   -- and every other ref namespace (``refs/replace/*`` would redirect the
#   objects a checkout reads).
_RUNTIME_WORKTREE_ROOTS: tuple[str, ...] = (
    ".psc-review-worktrees",
    ".psc-verification-worktrees",
)
#: ``<slice id>-<job id>`` / ``<slice id>-<sha12>`` in ASCII
#: (``verification.py:24``, ``spool_slot.py:103``).
_RUNTIME_WORKTREE_NAME = re.compile(r"[A-Za-z0-9_.-]+")
#: What ``git worktree add --detach`` writes into ``.git/worktrees/<id>``.
_RUNTIME_WORKTREE_ADMIN_ENTRIES = frozenset(
    {"HEAD", "ORIG_HEAD", "commondir", "gitdir", "index", "locked", "logs"}
)
_RUNTIME_IDENTITY_KEYS = frozenset({"name", "email"})
_RUNTIME_IDENTITY_VALUE = re.compile(r"[^\x00-\x1f\x7f]{1,256}")
_RUNTIME_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_RUNTIME_REF_SEGMENTS = r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*"
_RUNTIME_REF_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"refs/heads/feature/{_RUNTIME_REF_SEGMENTS}",
        rf"refs/remotes/origin/{_RUNTIME_REF_SEGMENTS}",
        r"refs/tags/archive/[A-Za-z0-9._-]+-[0-9a-f]{8}",
        r"refs/cortex/reclaimed/[A-Za-z0-9][A-Za-z0-9_.-]*"
        r"/[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}",
        r"refs/cortex/main-sync/[A-Za-z0-9][A-Za-z0-9_-]{0,127}",
        r"refs/cortex/main-sync-quarantine/[A-Za-z0-9][A-Za-z0-9_-]{0,127}",
        r"refs/cortex/mirror/[0-9a-f]{16}/(?:default|pull/[1-9][0-9]*)",
    )
)


def _config_key_label(section: str, key: str) -> str:
    """Spell a parsed key the way ``git config`` names it."""

    match = re.fullmatch(r'(\S+)\s+"(.*)"', section)
    if match is not None:
        return f"{match.group(1).lower()}.{match.group(2)}.{key}"
    return f"{section.lower()}.{key}"


def _repository_config_state(
    path: Path, *, remote: object, uid: int, gid: int
) -> tuple[bool, str | None, tuple[str, ...]]:
    """Accept the installer's clone config plus the declared runtime identity.

    Returns ``(safe, drift, runtime_keys)``.  The installer's exact ``core``
    and ``remote "origin"`` sections stay mandatory and immutable; the only
    additional section is ``[user]`` carrying plain ``name``/``email`` values.
    """

    git_dir = path / ".git"
    config_path = git_dir / "config"
    try:
        git_state = git_dir.lstat()
        config_state = config_path.lstat()
    except OSError:
        return False, "repository .git/config is missing", ()
    if (
        not stat.S_ISDIR(git_state.st_mode)
        or stat.S_ISLNK(git_state.st_mode)
        or git_state.st_uid != uid
        or git_state.st_gid != gid
    ):
        return False, "repository .git is not an owner-held directory", ()
    if (
        not stat.S_ISREG(config_state.st_mode)
        or stat.S_ISLNK(config_state.st_mode)
        or config_state.st_nlink != 1
        or config_state.st_uid != uid
        or config_state.st_gid != gid
    ):
        return False, "repository .git/config is not an owner-held regular file", ()
    if not isinstance(remote, str):
        return False, "repository step remote is invalid", ()
    parser = configparser.RawConfigParser(
        strict=True,
        interpolation=None,
        delimiters=("=",),
        comment_prefixes=("#", ";"),
        inline_comment_prefixes=None,
    )
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, UnicodeError, configparser.Error):
        return False, "repository .git/config is not a plain git config", ()
    expected = {
        "core": {
            "repositoryformatversion": "0",
            "filemode": "true",
            "bare": "false",
            "logallrefupdates": "true",
        },
        'remote "origin"': {
            "url": remote,
            "fetch": "+refs/heads/*:refs/remotes/origin/*",
        },
    }
    runtime: list[str] = []
    for section in parser.sections():
        values = dict(parser.items(section, raw=True))
        declared = expected.get(section)
        if declared is not None:
            for key, value in values.items():
                label = _config_key_label(section, key)
                if key not in declared:
                    return False, f"undeclared git config key: {label}", ()
                if value != declared[key]:
                    return False, f"git config key differs from the clone: {label}", ()
            continue
        if section == "user":
            for key, value in values.items():
                if key not in _RUNTIME_IDENTITY_KEYS:
                    return False, f"undeclared git config key: user.{key}", ()
                if _RUNTIME_IDENTITY_VALUE.fullmatch(value) is None:
                    return False, f"git config user.{key} is not a plain value", ()
                runtime.append(f"user.{key}")
            continue
        if values:
            label = _config_key_label(section, next(iter(values)))
            return False, f"undeclared git config key: {label}", ()
        return False, f"undeclared git config section: {section}", ()
    for section, values in expected.items():
        if not parser.has_section(section) or set(
            dict(parser.items(section, raw=True))
        ) != set(values):
            return False, f"git config section differs from the clone: {section}", ()
    return True, None, tuple(sorted(runtime))


def _read_runtime_metadata(path: Path) -> str | None:
    """Read one short single-line git metadata file without following links."""

    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        return None
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_size > 4096:
            return None
        payload = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = text[:-1] if text.endswith("\n") else text
    if not text or "\n" in text or "\0" in text:
        return None
    return text


def _runtime_worktree_paths(path: Path) -> tuple[Path, ...]:
    """Subtrees a main-checkout mutation must leave to the Manager."""

    return (
        *(path / root for root in _RUNTIME_WORKTREE_ROOTS),
        path / ".git" / "worktrees",
    )


class _RuntimeDrift(Exception):
    """Internal: the first runtime-allowlist violation, as operator text."""


def _runtime_directory_members(path: Path, label: str) -> set[str] | None:
    """List a real (non-symlink) directory; ``None`` when it does not exist."""

    try:
        observed = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _RuntimeDrift(f"cannot inspect {label}") from exc
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise _RuntimeDrift(f"{label} is not a directory")
    try:
        return set(os.listdir(path))
    except OSError as exc:
        raise _RuntimeDrift(f"cannot inspect {label}") from exc


def _runtime_worktree_row(path: Path, real_root: Path, entry: str) -> dict[str, object]:
    """Prove one ``.git/worktrees/<id>`` entry is a declared runtime checkout."""

    label = f".git/worktrees/{entry}"
    admin = path / ".git" / "worktrees" / entry
    members = _runtime_directory_members(admin, f"linked worktree metadata {label}")
    undeclared = sorted((members or set()) - _RUNTIME_WORKTREE_ADMIN_ENTRIES)
    if undeclared:
        raise _RuntimeDrift(f"undeclared linked worktree metadata: {label}/{undeclared[0]}")
    if members and "logs" in members:
        logs = _runtime_directory_members(admin / "logs", f"{label}/logs")
        if logs is None or logs - {"HEAD"}:
            raise _RuntimeDrift(f"undeclared linked worktree metadata: {label}/logs")
    gitdir = _read_runtime_metadata(admin / "gitdir")
    head = _read_runtime_metadata(admin / "HEAD")
    if gitdir is None or _read_runtime_metadata(admin / "commondir") != "../..":
        raise _RuntimeDrift(f"linked worktree metadata is not the git-written shape: {label}")
    if head is None or _RUNTIME_OBJECT_ID.fullmatch(head) is None:
        raise _RuntimeDrift(f"linked worktree is not a detached checkout: {label}")
    target = Path(gitdir)
    checkout = target.parent
    # ``git worktree add`` names the registry after the checkout directory and
    # appends a counter when that name is already taken.
    suffix = entry[len(checkout.name):] if entry.startswith(checkout.name) else None
    if not (
        target.is_absolute()
        and target.name == ".git"
        and checkout.parent.parent == real_root
        and checkout.parent.name in _RUNTIME_WORKTREE_ROOTS
        and _RUNTIME_WORKTREE_NAME.fullmatch(checkout.name) is not None
        and checkout.name not in {".", ".."}
        and suffix is not None
        and re.fullmatch(r"[0-9]*", suffix) is not None
    ):
        raise _RuntimeDrift(
            f"linked worktree outside the runtime allowlist: {label} -> {gitdir}"
        )
    relative = f"{checkout.parent.name}/{checkout.name}"
    try:
        checkout_state = (path / relative).lstat()
    except FileNotFoundError:
        present = False
    except OSError as exc:
        raise _RuntimeDrift(f"cannot inspect runtime worktree: {relative}") from exc
    else:
        if (
            not stat.S_ISDIR(checkout_state.st_mode)
            or stat.S_ISLNK(checkout_state.st_mode)
            or _read_runtime_metadata(path / relative / ".git")
            != f"gitdir: {real_root / '.git' / 'worktrees' / entry}"
        ):
            raise _RuntimeDrift(
                f"runtime worktree does not point back to its registry: {relative}"
            )
        present = True
    return {"id": entry, "path": relative, "head": head, "present": present}


def _repository_runtime_worktrees(
    path: Path,
) -> tuple[str | None, list[dict[str, object]], frozenset[str]]:
    """Validate every linked worktree against the declared runtime roots.

    Returns ``(drift, rows, status_entries)``.  ``drift`` is ``None`` only when
    every ``.git/worktrees/<id>`` registry entry is the git-written shape of a
    detached checkout at ``<root>/<name>`` for a declared runtime root, and
    every member of a runtime root is such a registered checkout.
    ``status_entries`` are the exact untracked lines ``git status`` prints in
    the main worktree for those nested checkouts.
    """

    real_root = Path(os.path.realpath(path))
    try:
        roots = {
            runtime_root: _runtime_directory_members(
                path / runtime_root, f"runtime worktree root {runtime_root}"
            )
            for runtime_root in _RUNTIME_WORKTREE_ROOTS
        }
        entries = _runtime_directory_members(path / ".git" / "worktrees", ".git/worktrees")
        rows: dict[str, dict[str, object]] = {}
        for entry in sorted(entries or ()):
            row = _runtime_worktree_row(path, real_root, entry)
            if row["path"] in rows:
                raise _RuntimeDrift(f"linked worktree registered twice: {row['path']}")
            rows[str(row["path"])] = row
        for runtime_root, children in roots.items():
            for child in sorted(children or ()):
                relative = f"{runtime_root}/{child}"
                if relative not in rows or rows[relative]["present"] is not True:
                    raise _RuntimeDrift(
                        f"unregistered entry in runtime worktree root: {relative}"
                    )
    except _RuntimeDrift as drift:
        return str(drift), [], frozenset()
    status_entries = frozenset(
        f"?? {relative}/" for relative, row in rows.items() if row["present"]
    )
    return None, list(rows.values()), status_entries


def _repository_runtime_refs(
    prefix: Sequence[str], *, uid: int, gid: int
) -> tuple[str | None, int, str]:
    """Accept only refs in the declared runtime namespaces.

    Returns ``(drift, count, sha256)`` over the sorted ``<ref> <object>`` rows,
    so a mutation of the tree can prove it left every Manager ref in place.
    """

    listing = _run(
        (*prefix, "for-each-ref", "--format=%(refname) %(objectname) %(symref)"),
        env=_REPOSITORY_GIT_ENV,
        uid=uid,
        gid=gid,
    )
    if listing.returncode != 0:
        return "git for-each-ref failed", 0, ""
    rows: list[str] = []
    for line in listing.stdout.splitlines():
        refname, _separator, remainder = line.partition(" ")
        objectname, _separator, symref = remainder.partition(" ")
        if symref:
            return f"undeclared symbolic ref: {refname}", 0, ""
        if not any(pattern.fullmatch(refname) for pattern in _RUNTIME_REF_PATTERNS):
            return f"undeclared ref: {refname}", 0, ""
        if _RUNTIME_OBJECT_ID.fullmatch(objectname) is None:
            return f"ref does not name an object: {refname}", 0, ""
        rows.append(f"{refname} {objectname}")
    rows.sort()
    digest = hashlib.sha256("".join(f"{row}\n" for row in rows).encode("utf-8"))
    return None, len(rows), digest.hexdigest()


def _repository_tree_drift(
    path: Path, *, uid: int, gid: int, owner: object, group: object
) -> str | None:
    """Name the first member whose owner, write bit, or link target is unsafe."""

    anchor = path.resolve(strict=True)
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        for name in (".", *directories, *files):
            candidate = Path(root) if name == "." else Path(root) / name
            relative = candidate.relative_to(path).as_posix()
            try:
                candidate_state = candidate.lstat()
            except OSError:
                return f"cannot inspect repository member: {relative}"
            if candidate_state.st_uid != uid or candidate_state.st_gid != gid:
                return (
                    f"foreign owner: {relative} is "
                    f"{_account_name(candidate_state.st_uid)}:"
                    f"{_group_name(candidate_state.st_gid)}, expected {owner}:{group}"
                )
            if (
                not stat.S_ISLNK(candidate_state.st_mode)
                and stat.S_IMODE(candidate_state.st_mode) & 0o002
            ):
                return f"world-writable repository member: {relative}"
            if stat.S_ISLNK(candidate_state.st_mode):
                target = Path(os.readlink(candidate))
                resolved = (candidate.parent / target).resolve(strict=False)
                try:
                    resolved.relative_to(anchor)
                except ValueError:
                    return f"symlink escapes the repository: {relative}"
    return None


def _refuse_runtime_root_checkout(
    prefix: Sequence[str], commit: str, *, uid: int, gid: int
) -> None:
    """Refuse a checkout that would write into the Manager's worktree roots."""

    listing = _run(
        (*prefix, "ls-tree", "--name-only", commit, "--", *_RUNTIME_WORKTREE_ROOTS),
        env=_REPOSITORY_GIT_ENV,
        uid=uid,
        gid=gid,
    )
    if listing.returncode != 0:
        raise InstallDriftError(f"repository commit cannot be listed: {commit}")
    tracked = listing.stdout.splitlines()
    if tracked:
        raise InstallDriftError(
            "repository commit tracks a Manager runtime worktree root: "
            f"{tracked[0]}"
        )


def _refuse_ignored_content_overwrite(
    prefix: Sequence[str], commit: str, *, uid: int, gid: int
) -> None:
    """Refuse a checkout that would overwrite or delete ignored untracked content.

    Ignored files never show in ``status``, so the clean check cannot see them.
    ``checkout --force`` treats them as expendable: a commit that starts
    tracking the same path (or a parent/child of it) overwrites or removes them,
    and rollback has no copy to restore (#1124 review).
    """

    ignored = _run(
        (
            *prefix,
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
        ),
        env=_REPOSITORY_GIT_ENV,
        uid=uid,
        gid=gid,
    )
    if ignored.returncode != 0:
        raise InstallDriftError("repository ignored content cannot be listed")
    entries = [entry for entry in ignored.stdout.split("\0") if entry]
    if not entries:
        return
    ignored_dirs = {entry.rstrip("/") for entry in entries if entry.endswith("/")}
    ignored_files = {entry for entry in entries if not entry.endswith("/")}
    listing = _run(
        (*prefix, "ls-tree", "-r", "-z", "--name-only", "--full-tree", commit),
        env=_REPOSITORY_GIT_ENV,
        uid=uid,
        gid=gid,
    )
    if listing.returncode != 0:
        raise InstallDriftError(f"repository commit cannot be listed: {commit}")
    tracked = [entry for entry in listing.stdout.split("\0") if entry]
    tracked_paths = set(tracked)
    for path in tracked:
        parts = path.split("/")
        prefixes = {"/".join(parts[:index]) for index in range(1, len(parts) + 1)}
        if path in ignored_files or prefixes & ignored_dirs:
            raise InstallDriftError(
                f"repository commit would overwrite ignored untracked content: {path}"
            )
    for entry in (*ignored_files, *ignored_dirs):
        parts = entry.split("/")
        parents = {"/".join(parts[:index]) for index in range(1, len(parts))}
        if parents & tracked_paths:
            raise InstallDriftError(
                "repository commit would overwrite ignored untracked content: "
                f"{entry}"
            )


def _tree_owned_and_nonwritable(path: Path, uid: int, gid: int) -> bool:
    """Attest owner/group and the write boundary for every no-follow tree member."""

    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        for candidate in (
            Path(root),
            *(Path(root) / name for name in (*directories, *files)),
        ):
            try:
                observed = candidate.lstat()
            except OSError:
                return False
            if observed.st_uid != uid or observed.st_gid != gid:
                return False
            if (
                not stat.S_ISLNK(observed.st_mode)
                and stat.S_IMODE(observed.st_mode) & 0o022
            ):
                return False
    return True


def _open_relative_directory(root_fd: int, parts: Sequence[str]) -> int:
    """Open a descendant directory one no-follow component at a time."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current = os.dup(root_fd)
    try:
        for component in parts:
            following = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _sha256_file_at(parent_fd: int, name: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise InstallDriftError(f"toolchain member is not a safe regular file: {name}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise InstallDriftError(f"toolchain member changed during rollback: {name}")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _toolchain_member_matches_at(
    parent_fd: int,
    name: str,
    row: Mapping[str, object],
    *,
    uid: int,
    gid: int,
) -> bool:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    if observed.st_uid != uid or observed.st_gid != gid:
        return False
    kind = row.get("kind")
    if kind == "symlink":
        try:
            target = os.readlink(name, dir_fd=parent_fd)
        except OSError:
            return False
        return stat.S_ISLNK(observed.st_mode) and target == row.get("target")
    expected_mode = row.get("mode")
    if expected_mode is not None and stat.S_IMODE(observed.st_mode) != expected_mode:
        return False
    if stat.S_IMODE(observed.st_mode) & 0o022:
        return False
    if kind == "directory":
        return stat.S_ISDIR(observed.st_mode)
    if kind != "file" or not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        return False
    try:
        digest = _sha256_file_at(parent_fd, name)
    except (InstallError, OSError):
        return False
    return digest == row.get("sha256")


def _rollback_prepared_toolchain(step: Mapping[str, object], path: Path) -> None:
    """Remove only archive-proven leaves; unknown or drifted content is retained."""

    source = Path(str(step.get("source", "")))
    source_sha = step.get("source_sha256")
    if (
        not source.is_absolute()
        or source.is_symlink()
        or not source.is_file()
        or source.lstat().st_nlink != 1
        or not isinstance(source_sha, str)
        or _sha256_file(source) != source_sha
    ):
        raise InstallDriftError("prepared toolchain rollback lacks its locked source archive")
    manifest = _locked_tree_manifest(source)
    uid = _resolve_uid(step.get("owner"))
    gid = _resolve_gid(step.get("group"))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = os.open(path, flags)
    except OSError as exc:
        raise UnsafeInstallPathError(
            f"cannot safely open prepared toolchain root {path}: {exc}"
        ) from exc
    try:
        root_state = os.fstat(root_fd)
        for relative, row in sorted(
            manifest.items(),
            key=lambda item: (len(Path(item[0]).parts), item[0]),
            reverse=True,
        ):
            parts = Path(relative).parts
            try:
                parent_fd = _open_relative_directory(root_fd, parts[:-1])
            except OSError:
                continue
            try:
                if not _toolchain_member_matches_at(
                    parent_fd, parts[-1], row, uid=uid, gid=gid
                ):
                    continue
                try:
                    if row.get("kind") == "directory":
                        os.rmdir(parts[-1], dir_fd=parent_fd)
                    else:
                        os.unlink(parts[-1], dir_fd=parent_fd)
                except OSError:
                    pass
            finally:
                os.close(parent_fd)
        observed = path.lstat()
        if (
            stat.S_ISDIR(observed.st_mode)
            and (observed.st_dev, observed.st_ino)
            == (root_state.st_dev, root_state.st_ino)
            and observed.st_uid == uid
            and observed.st_gid == gid
            and stat.S_IMODE(observed.st_mode) == _mode(step.get("mode"))
        ):
            with os.scandir(root_fd) as entries:
                empty = next(entries, None) is None
            if empty:
                try:
                    path.rmdir()
                except OSError:
                    pass
    finally:
        os.close(root_fd)


def _repository_state(step: Mapping[str, object]) -> dict[str, object]:
    """Inspect an installed source tree, reconciling declared runtime state.

    ``drift`` names every allowlist or safety violation (step-independent:
    the same list results for the prior and the upgraded step), and
    ``runtime`` records the reconciled Manager state so a mutation can prove
    it left that state in place (#1124).
    """

    path = Path(str(step.get("path", "")))
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        return {"exists": True, "installed_sha256": None}
    expected_uid = _resolve_uid(step.get("owner"))
    expected_gid = _resolve_gid(step.get("group"))
    drift: list[str] = []
    tree_drift = _repository_tree_drift(
        path,
        uid=expected_uid,
        gid=expected_gid,
        owner=step.get("owner"),
        group=step.get("group"),
    )
    tree_safe = tree_drift is None
    config_safe = False
    runtime_config: tuple[str, ...] = ()
    worktree_drift: str | None = None
    worktrees: list[dict[str, object]] = []
    status_entries: frozenset[str] = frozenset()
    if tree_drift is not None:
        drift.append(tree_drift)
    else:
        config_safe, config_drift, runtime_config = _repository_config_state(
            path,
            remote=step.get("remote"),
            uid=expected_uid,
            gid=expected_gid,
        )
        if config_drift is not None:
            drift.append(config_drift)
        worktree_drift, worktrees, status_entries = _repository_runtime_worktrees(path)
        if worktree_drift is not None:
            drift.append(worktree_drift)
    # Run git only in a tree whose config and worktree metadata are declared
    # state, as its owning account, and without any host/user config.
    # Command-scope overrides are defense in depth against executable
    # fsmonitor/hooks settings and optional index writes.
    prefix = (
        *_REPOSITORY_GIT_PREFIX,
        "-c",
        f"safe.directory={path}",
        "-C",
        str(path),
    )
    git_safe = tree_safe and config_safe and worktree_drift is None
    refs_drift: str | None = None
    refs_count = 0
    refs_sha256 = ""
    dirty: list[str] = []
    if git_safe:
        head, remote, clean, integrity = (
            _run(
                (*prefix, *suffix),
                env=_REPOSITORY_GIT_ENV,
                uid=expected_uid,
                gid=expected_gid,
            )
            for suffix in (
                ("rev-parse", "HEAD"),
                ("remote", "get-url", "origin"),
                ("status", "--porcelain=v1", "--untracked-files=all"),
                ("fsck", "--strict", "--no-dangling"),
            )
        )
        refs_drift, refs_count, refs_sha256 = _repository_runtime_refs(
            prefix, uid=expected_uid, gid=expected_gid
        )
        if refs_drift is not None:
            drift.append(refs_drift)
        # A linked worktree nested in the main checkout is an untracked
        # directory to ``git status``; only the registered runtime ones are
        # expected, anything else is a dirty tree.
        dirty = [
            line for line in clean.stdout.splitlines() if line not in status_entries
        ]
        if clean.returncode != 0:
            drift.append("git status failed")
        elif dirty:
            drift.append(f"working tree is not clean: {dirty[0]}")
        if integrity.returncode != 0:
            drift.append("git fsck --strict failed")
    else:
        head = remote = clean = integrity = subprocess.CompletedProcess(
            list(prefix), 1, "", "repository is not in its declared runtime shape"
        )
    is_clean = clean.returncode == 0 and not dirty
    runtime_safe = git_safe and refs_drift is None
    matches = (
        head.returncode == 0
        and head.stdout.strip() == step.get("commit")
        and remote.returncode == 0
        and remote.stdout.strip() == step.get("remote")
        and is_clean
        and integrity.returncode == 0
        and tree_safe
        and config_safe
        and runtime_safe
        and _account_name(observed.st_uid) == step.get("owner")
        and _group_name(observed.st_gid) == step.get("group")
        and format(stat.S_IMODE(observed.st_mode), "04o") == step.get("mode")
    )
    return {
        "exists": True,
        "owner": _account_name(observed.st_uid),
        "group": _group_name(observed.st_gid),
        "mode": format(stat.S_IMODE(observed.st_mode), "04o"),
        "commit": head.stdout.strip() if head.returncode == 0 else "",
        "remote": remote.stdout.strip() if remote.returncode == 0 else "",
        "clean": is_clean,
        "integrity": integrity.returncode == 0,
        "tree_safe": tree_safe,
        "config_safe": config_safe,
        "runtime_safe": runtime_safe,
        "runtime": {
            "config": list(runtime_config),
            "worktrees": worktrees,
            "refs_count": refs_count,
            "refs_sha256": refs_sha256,
        },
        "drift": drift,
        "installed_sha256": step.get("desired_sha256") if matches else None,
    }


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid, follow_symlinks=False)
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        for name in (*directories, *files):
            os.chown(Path(root) / name, uid, gid, follow_symlinks=False)


def _remove_group_other_write(path: Path, *, skip: Sequence[Path] = ()) -> None:
    """Make a checkout non-writable outside its owning account.

    ``skip`` subtrees (the Manager's runtime worktrees) are neither entered
    nor changed: a main-checkout mutation never touches them.
    """

    skipped = set(skip)
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        directories[:] = [
            name for name in directories if Path(root) / name not in skipped
        ]
        for candidate in (Path(root), *(Path(root) / name for name in (*directories, *files))):
            observed = candidate.lstat()
            if not stat.S_ISLNK(observed.st_mode):
                os.chmod(candidate, stat.S_IMODE(observed.st_mode) & ~0o022)


def _expected_acl_mode(step: Mapping[str, object]) -> str:
    """Return the stat mode after setfacl has recalculated the access mask.

    POSIX ACLs expose their mask through the traditional group mode bits.  The
    plan's ``mode`` is the base owner/group/other mode applied *before* named
    ACLs; comparing it directly with ``lstat`` therefore reports false drift
    whenever a named access entry expands the mask.
    """

    base = _mode(step.get("mode"))
    mask = (base >> 3) & 0o7
    for row in step.get("acls", []):
        if not isinstance(row, Mapping) or row.get("default"):
            continue
        perms = str(row.get("perms", "")).replace("X", "x")
        mask |= (0o4 if "r" in perms else 0) | (0o2 if "w" in perms else 0) | (
            0o1 if "x" in perms else 0
        )
    return format((base & ~0o070) | (mask << 3), "04o")


def _expected_acls(step: Mapping[str, object]) -> list[dict[str, object]]:
    rows = [
        {
            "account": row.get("account"),
            "perms": str(row.get("perms", "")).replace("X", "x").replace("-", ""),
            "default": bool(row.get("default", False)),
        }
        for row in step.get("acls", [])
        if isinstance(row, Mapping)
    ]
    base = _mode(step.get("mode"))

    def rendered(bits: int) -> str:
        return "".join(
            char for bit, char in ((0o4, "r"), (0o2, "w"), (0o1, "x")) if bits & bit
        )

    access_rows = [row for row in rows if not row["default"]]
    if access_rows:
        access_mask = (base >> 3) & 0o7
        for row in access_rows:
            perms = str(row["perms"])
            access_mask |= (0o4 if "r" in perms else 0) | (0o2 if "w" in perms else 0) | (
                0o1 if "x" in perms else 0
            )
        rows += [
            {
                "account": "",
                "perms": rendered((base >> 3) & 0o7),
                "default": False,
                "entry_type": "group",
            },
            {
                "account": "",
                "perms": rendered(access_mask),
                "default": False,
                "entry_type": "mask",
            },
        ]
    default_rows = [row for row in rows if row["default"]]
    if default_rows:
        default_mask = (base >> 3) & 0o7
        for row in default_rows:
            perms = str(row["perms"])
            default_mask |= (0o4 if "r" in perms else 0) | (0o2 if "w" in perms else 0) | (
                0o1 if "x" in perms else 0
            )
        rows += [
            {
                "account": "",
                "perms": rendered((base >> 6) & 0o7),
                "default": True,
                "entry_type": "user",
            },
            {
                "account": "",
                "perms": rendered((base >> 3) & 0o7),
                "default": True,
                "entry_type": "group",
            },
            {
                "account": "",
                "perms": rendered(default_mask),
                "default": True,
                "entry_type": "mask",
            },
            {
                "account": "",
                "perms": rendered(base & 0o7),
                "default": True,
                "entry_type": "other",
            },
        ]
    return sorted(
        rows,
        key=lambda row: (
            bool(row["default"]),
            str(row.get("entry_type", "user")),
            str(row["account"]),
            str(row["perms"]),
        ),
    )


def _in_flight_process_count(accounts: Sequence[Mapping[str, object]]) -> int:
    job_uids = {
        int(row["uid"])
        for row in accounts
        if row.get("name") in {
            "cortex-builder",
            "cortex-reviewer-planner",
            "cortex-gate",
        }
        and isinstance(row.get("uid"), int)
    }
    count = 0
    for status_path in Path("/proc").glob("[0-9]*/status"):
        try:
            uid_line = next(
                line for line in status_path.read_text(encoding="ascii").splitlines()
                if line.startswith("Uid:")
            )
            real_uid = int(uid_line.split()[1])
        except (OSError, StopIteration, ValueError, IndexError):
            continue
        if real_uid in job_uids:
            count += 1
    return count


def _durable_jobs_path(plan: Mapping[str, object]) -> Path:
    """Resolve the coordinator registry from the immutable plan inventory."""

    coordinator_roots = {
        str(step.get("path"))
        for step in plan.get("apply_order", [])
        if isinstance(step, Mapping)
        and step.get("step_id") == "asset:coordinator-root-tree"
        and isinstance(step.get("path"), str)
    }
    if len(coordinator_roots) > 1:
        raise InstallPlanError("plan declares multiple coordinator root assets")
    if coordinator_roots:
        coordinator_root = Path(next(iter(coordinator_roots)))
    else:
        roots = plan.get("roots")
        state = roots.get("state") if isinstance(roots, Mapping) else None
        if not isinstance(state, str) or not state:
            raise InstallPlanError("plan does not declare the durable state root")
        coordinator_root = Path(state) / "coordinator"
    jobs_path = coordinator_root / "jobs.json"
    if not jobs_path.is_absolute() or ".." in jobs_path.parts:
        raise UnsafeInstallPathError(
            f"durable jobs registry path is unsafe: {jobs_path}"
        )
    return jobs_path


def _durable_in_flight_job_count(plan: Mapping[str, object]) -> int:
    """Read the persisted registry without mutating or migrating it."""

    path = _durable_jobs_path(plan)
    _reject_symlink_ancestors(path, label="durable jobs registry", include_leaf=False)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise UnsafeInstallPathError(
            f"cannot safely open durable jobs registry {path}: {exc}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise UnsafeInstallPathError(
                f"durable jobs registry must be a single-link regular file: {path}"
            )
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
                payload = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InstallDriftError(
                f"durable jobs registry cannot be decoded: {path}: {exc}"
            ) from exc
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise InstallDriftError(
                f"durable jobs registry changed during preflight: {path}"
            )
    finally:
        os.close(descriptor)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
        raise InstallDriftError(f"durable jobs registry has an invalid shape: {path}")
    valid_statuses = {"dispatched", "running", "exited", "failed"}
    active = 0
    for row in payload["jobs"]:
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("status"), str)
            or row["status"] not in valid_statuses
        ):
            raise InstallDriftError(f"durable jobs registry has an invalid job row: {path}")
        if row["status"] in {"dispatched", "running"}:
            active += 1
    return active


def _state_matches_step(step: Mapping[str, object], state: Mapping[str, object]) -> bool:
    return bool(
        state.get("exists")
        and state.get("installed_sha256") == step.get("desired_sha256")
        and state.get("owner") == step.get("owner")
        and state.get("group") == step.get("group")
        and state.get("mode") == step.get("mode")
        and state.get("acl", []) == step.get("acls", [])
    )


def _creation_authority(
    observed: os.stat_result, *, file_type: str
) -> dict[str, object]:
    matches_type = (
        stat.S_ISREG(observed.st_mode)
        if file_type == "file"
        else stat.S_ISDIR(observed.st_mode)
        if file_type == "directory"
        else stat.S_ISLNK(observed.st_mode)
        if file_type == "symlink"
        else False
    )
    if not matches_type or (file_type == "file" and observed.st_nlink != 1):
        raise InstallDriftError("created asset inode has an unsafe type or link count")
    return {
        "device": observed.st_dev,
        "inode": observed.st_ino,
        "file_type": file_type,
    }


def _path_matches_creation_authority(
    path: Path, authority: Mapping[str, object]
) -> bool:
    if set(authority) != {"device", "inode", "file_type"}:
        return False
    device = authority.get("device")
    inode = authority.get("inode")
    file_type = authority.get("file_type")
    if (
        not isinstance(device, int)
        or isinstance(device, bool)
        or device < 0
        or not isinstance(inode, int)
        or isinstance(inode, bool)
        or inode <= 0
        or file_type not in {"file", "directory", "symlink"}
    ):
        return False
    try:
        observed = path.lstat()
    except OSError:
        return False
    matches_type = (
        stat.S_ISREG(observed.st_mode)
        if file_type == "file"
        else stat.S_ISDIR(observed.st_mode)
        if file_type == "directory"
        else stat.S_ISLNK(observed.st_mode)
    )
    return bool(
        matches_type
        and (file_type != "file" or observed.st_nlink == 1)
        and (observed.st_dev, observed.st_ino) == (device, inode)
    )


def _sudoers_authenticate_setting(
    row: Mapping[str, object],
) -> tuple[bool, bool | None]:
    options = row.get("Options", [])
    if not isinstance(options, list):
        return False, None
    setting: bool | None = None
    for option in options:
        if not isinstance(option, Mapping):
            return False, None
        if "authenticate" not in option:
            continue
        value = option.get("authenticate")
        if not isinstance(value, bool):
            return False, None
        setting = value
    return True, setting


def _sudoers_host_list_is_universal(hosts: object) -> tuple[bool, bool]:
    if not isinstance(hosts, list) or not hosts:
        return False, False
    universal = False
    for host in hosts:
        if not isinstance(host, Mapping):
            return False, False
        negated = host.get("negated", False)
        if not isinstance(negated, bool):
            return False, False
        if negated:
            return True, False
        hostname = host.get("hostname")
        if isinstance(hostname, str):
            universal = universal or hostname == "ALL" or not hostname.strip("*?")
            continue
        network = host.get("networkaddr")
        if isinstance(network, str):
            universal = universal or network.endswith("/0")
            continue
        netgroup = host.get("netgroup")
        if isinstance(netgroup, str):
            continue
        return False, False
    return True, universal


def _sudoers_commands_are_universal(commands: object) -> tuple[bool, bool]:
    if not isinstance(commands, list) or not commands:
        return False, False
    universal = False
    for command in commands:
        if not isinstance(command, Mapping):
            return False, False
        negated = command.get("negated", False)
        if not isinstance(negated, bool):
            return False, False
        if negated:
            return True, False
        value = command.get("command")
        if not isinstance(value, str):
            return False, False
        universal = universal or value == "ALL"
    return True, universal


def _sudoers_document_has_universal_noauth(document: object) -> bool:
    """Evaluate only validated, alias-expanded cvtsudoers JSON."""

    if not isinstance(document, Mapping):
        return True
    defaults = document.get("Defaults", [])
    specs = document.get("User_Specs", [])
    if not isinstance(defaults, list) or not isinstance(specs, list):
        return True

    default_noauth = False
    for default in defaults:
        if not isinstance(default, Mapping):
            return True
        valid, authenticate = _sudoers_authenticate_setting(default)
        if not valid:
            return True
        if default.get("Binding") is None and authenticate is not None:
            default_noauth = not authenticate
        elif authenticate is False:
            # Scoped Defaults are conservatively relevant unless an explicit
            # PASSWD tag on the command spec proves otherwise.
            default_noauth = True

    for spec in specs:
        if not isinstance(spec, Mapping):
            return True
        valid_hosts, universal_hosts = _sudoers_host_list_is_universal(
            spec.get("Host_List")
        )
        command_specs = spec.get("Cmnd_Specs")
        if not valid_hosts or not isinstance(command_specs, list):
            return True
        if not universal_hosts:
            continue
        for command_spec in command_specs:
            if not isinstance(command_spec, Mapping):
                return True
            valid_commands, universal_commands = _sudoers_commands_are_universal(
                command_spec.get("Commands")
            )
            valid_auth, authenticate = _sudoers_authenticate_setting(command_spec)
            if not valid_commands or not valid_auth:
                return True
            if universal_commands and (
                authenticate is False
                or (authenticate is None and default_noauth)
            ):
                return True
    return False


def _universal_nopasswd(sudoers: Path = Path("/etc/sudoers")) -> bool:
    try:
        observed = sudoers.lstat()
    except OSError:
        return True
    if sudoers.is_symlink() or not sudoers.is_absolute() or not stat.S_ISREG(
        observed.st_mode
    ):
        return True
    visudo = shutil.which("visudo")
    converter = shutil.which("cvtsudoers")
    if visudo is None or converter is None:
        return True
    environment = {"LANG": "C", "LC_ALL": "C", "PATH": os.defpath}
    try:
        validated = _run(
            (visudo, "-c", "-f", str(sudoers)),
            env=environment,
        )
    except OSError:
        return True
    if validated.returncode != 0:
        return True
    try:
        converted = _run(
            (converter, "-f", "json", "-e", str(sudoers)),
            env=environment,
        )
    except OSError:
        return True
    if converted.returncode != 0:
        return True
    try:
        document = json.loads(converted.stdout)
    except (TypeError, json.JSONDecodeError):
        return True
    return _sudoers_document_has_universal_noauth(document)


def _password_locked(name: str) -> bool | None:
    try:
        for line in Path("/etc/shadow").read_text(
            encoding="utf-8", errors="strict"
        ).splitlines():
            account, separator, remainder = line.partition(":")
            if separator and account == name:
                password = remainder.partition(":")[0]
                return password.startswith(("!", "*"))
    except OSError:
        return None
    return None


def _classify_systemctl_is_active(
    result: subprocess.CompletedProcess[str],
    load_state: subprocess.CompletedProcess[str] | None = None,
) -> str:
    """Classify only documented, internally consistent `is-active` results."""

    state = result.stdout.strip()
    if result.returncode == 0 and state == "active":
        return "active"
    if result.returncode == 3 and state in {"inactive", "failed"}:
        return state
    if result.returncode == 4 and load_state is not None:
        if load_state.returncode == 0 and load_state.stdout.strip() == "not-found":
            return "not-found"
    return "error"


def _acl_argument(row: Mapping[str, object]) -> str:
    """Render one inspected or planned ACL row without changing its type."""

    entry_type = row.get("entry_type", "user")
    account = row.get("account")
    perms = row.get("perms")
    if (
        entry_type not in {"user", "group", "mask", "other"}
        or not isinstance(account, str)
        or not isinstance(perms, str)
    ):
        raise InstallPlanError("ACL entries require a valid type, account, and perms")
    prefix = "d:" if row.get("default") else ""
    short = {"user": "u", "group": "g", "mask": "m", "other": "o"}[
        str(entry_type)
    ]
    # ``_read_acl`` records ``---`` as an empty string (receipts hold that
    # form); ``setfacl -m m::`` is rejected as incomplete, so an empty
    # permission set is written as ``-``.
    rendered = perms.replace("X", "x") or "-"
    if entry_type in {"mask", "other"}:
        if account:
            raise InstallPlanError("mask/other ACL entries must not name an account")
        return f"{prefix}{short}::{rendered}"
    return f"{prefix}{short}:{account}:{rendered}"


def _apply_fd_asset_state(
    descriptor: int,
    *,
    owner: object,
    group: object,
    mode: object,
    acls: object,
    directory: bool,
) -> None:
    """Apply exact metadata to a held no-follow inode."""

    if not isinstance(acls, list) or any(
        not isinstance(row, Mapping) for row in acls
    ):
        raise InstallPlanError("ACL state must be an array of objects")
    if not Path("/proc/self/fd").is_dir():
        raise InstallError("/proc/self/fd is required for safe ACL apply")
    target = f"/proc/self/fd/{descriptor}"
    inherited_fds = (descriptor,)
    _run(("setfacl", "-b", target), check=True, pass_fds=inherited_fds)
    if directory:
        _run(("setfacl", "-k", target), check=True, pass_fds=inherited_fds)
    os.fchown(descriptor, _resolve_uid(owner), _resolve_gid(group))
    os.fchmod(descriptor, _mode(mode))
    # One ``-m`` for the whole set: setfacl recalculates the mask after each
    # modification unless the same command names a mask entry, so applying a
    # recorded ``mask::---`` before a named entry would silently widen it.
    arguments = [_acl_argument(row) for row in acls]  # type: ignore[arg-type]
    if any("," in argument for argument in arguments):
        raise InstallPlanError("ACL entries must not contain a comma")
    if arguments:
        _run(
            ("setfacl", "-m", ",".join(arguments), target),
            check=True,
            pass_fds=inherited_fds,
        )


def _snapshot_authority(
    observed: os.stat_result, *, file_type: str
) -> dict[str, object]:
    return _creation_authority(observed, file_type=file_type)


def _replacement_staging_path(step: Mapping[str, object]) -> Path:
    path = Path(str(step.get("path", "")))
    return path.with_name(f".{path.name}.cortex-replacement")


def _replacement_staging_step(step: Mapping[str, object]) -> dict[str, object]:
    staged = dict(step)
    staged["path"] = str(_replacement_staging_path(step))
    return staged


def _replacement_staging_matches(
    step: Mapping[str, object], state: Mapping[str, object]
) -> bool:
    if step.get("asset_type") == "symlink":
        return bool(
            state.get("exists") is True
            and state.get("installed_sha256") == step.get("desired_sha256")
            and state.get("owner") == step.get("owner")
            and state.get("group") == step.get("group")
        )
    return _state_matches_step(step, state)


# ---------------------------------------------------------------------------
# legacy quarantine (#1122): move a legacy object aside, never delete or
# overwrite it, and move it back on rollback
# ---------------------------------------------------------------------------

_RENAME_NOREPLACE = 1
#: ``renameat2`` (Linux 3.15+) by architecture; an unknown one fails closed.
_RENAMEAT2_SYSCALL = {
    "x86_64": 316,
    "i386": 353,
    "i686": 353,
    "aarch64": 276,
    "arm64": 276,
    "riscv64": 276,
    "loongarch64": 276,
    "armv7l": 382,
    "armv8l": 382,
    "s390x": 347,
    "ppc64": 357,
    "ppc64le": 357,
}
_RENAME_UNSUPPORTED_ERRNOS = frozenset(
    {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
)
#: The inventory hashes regular files up to this bound; the quarantine binds
#: exactly the digests the inventory recorded.
_QUARANTINE_HASH_MAX_BYTES = ASSET_PRIOR_SNAPSHOT_MAX_BYTES
_QUARANTINE_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


@functools.lru_cache(maxsize=1)
def _renameat2_syscall():  # type: ignore[no-untyped-def]
    function = ctypes.CDLL(None, use_errno=True).syscall
    function.restype = ctypes.c_long
    return function


def _renameat2_noreplace(
    source_dir_fd: int, source: str, destination_dir_fd: int, destination: str
) -> None:
    """``renameat2(..., RENAME_NOREPLACE)`` straight to the kernel.

    Deliberately not a libc wrapper or a check-then-rename: a kernel or
    filesystem that cannot refuse to replace an existing destination answers
    ENOSYS/EINVAL, and the quarantine then refuses to move at all instead of
    falling back to a racy rename or to a copy.
    """

    number = _RENAMEAT2_SYSCALL.get(platform.machine())
    if number is None:
        raise OSError(
            errno.ENOSYS, "renameat2 is not known for this architecture", source
        )
    result = _renameat2_syscall()(
        ctypes.c_long(number),
        ctypes.c_long(source_dir_fd),
        ctypes.c_char_p(os.fsencode(source)),
        ctypes.c_long(destination_dir_fd),
        ctypes.c_char_p(os.fsencode(destination)),
        ctypes.c_long(_RENAME_NOREPLACE),
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), destination)
        raise OSError(error, os.strerror(error), destination)


def _validate_quarantine_ancestor(observed: os.stat_result, path: Path) -> None:
    """An ancestor of the quarantine root: root-owned, writable by root only."""

    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != 0
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise UnsafeInstallPathError(
            "quarantine ancestor must be a root-owned directory no other account "
            f"can write: {path}"
        )


def _validate_quarantine_directory(observed: os.stat_result, path: Path) -> None:
    """The quarantine root and every directory below it: root-owned 0700."""

    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != 0
        or stat.S_IMODE(observed.st_mode) != 0o700
    ):
        raise UnsafeInstallPathError(
            f"quarantine directory must be root-owned mode 0700: {path}"
        )


def _filesystem_device(descriptor: int) -> int:
    return os.fstat(descriptor).st_dev


def _normalized_absolute_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and value != "/"
        and "\x00" not in value
        and posixpath.normpath(value) == value
    )


def _quarantine_layout(step: Mapping[str, object]) -> tuple[Path, Path, Path]:
    """Return (source, destination, quarantine root) of a bound quarantine step.

    ``destination`` must be ``<quarantine_root>/<16 hex>/root<source>``: the
    root is derived from the step itself, never from a caller.
    """

    source = step.get("path")
    destination = step.get("destination")
    expected = step.get("expected")
    if (
        step.get("kind") != "legacy-quarantine"
        or not _normalized_absolute_path(source)
        or not _normalized_absolute_path(destination)
        or not isinstance(expected, Mapping)
    ):
        raise InstallPlanError(f"invalid legacy quarantine step: {step.get('step_id')}")
    assert isinstance(source, str) and isinstance(destination, str)
    suffix = f"/root{source}"
    prefix = destination[: -len(suffix)] if destination.endswith(suffix) else ""
    root, _separator, bucket = prefix.rpartition("/")
    if not re.fullmatch(r"[0-9a-f]{16}", bucket) or not _normalized_absolute_path(root):
        raise InstallPlanError(
            f"legacy quarantine destination is not bound to a quarantine root: {destination}"
        )
    for key in ("dev", "ino"):
        if type(expected.get(key)) is not int:
            raise InstallPlanError(
                f"legacy quarantine step lacks its inode identity: {step.get('step_id')}"
            )
    return Path(source), Path(destination), Path(root)


def _file_type_name(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _quarantine_record(observed: os.stat_result) -> dict[str, object]:
    return {
        "type": _file_type_name(observed.st_mode),
        "uid": observed.st_uid,
        "gid": observed.st_gid,
        "mode": format(stat.S_IMODE(observed.st_mode), "04o"),
        "dev": observed.st_dev,
        "ino": observed.st_ino,
    }


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _bounded_sha256_at(
    parent_fd: int, name: str, observed: os.stat_result
) -> str | None:
    """Hash a bound-sized regular file that is still ``observed``; else ``None``."""

    if not stat.S_ISREG(observed.st_mode) or observed.st_size > _QUARANTINE_HASH_MAX_BYTES:
        return None
    try:
        descriptor = os.open(name, _QUARANTINE_READ_FLAGS, dir_fd=parent_fd)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (observed.st_dev, observed.st_ino):
            return None
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > _QUARANTINE_HASH_MAX_BYTES:
                return None
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (total, after.st_size, after.st_mtime_ns) != (
            before.st_size,
            before.st_size,
            before.st_mtime_ns,
        ):
            return None
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _quarantine_object_record(
    parent_fd: int, name: str, expected: Mapping[str, object]
) -> dict[str, object] | None:
    """An object's identity plus exactly the content binding the step carries.

    Read with a no-follow ``fstatat`` (and ``openat``/``readlinkat``) on the
    given directory descriptor.  Only a file whose inventory row recorded a
    digest is read; credential class objects carry none and are never opened.
    """

    observed = _stat_at(parent_fd, name)
    if observed is None:
        return None
    record = _quarantine_record(observed)
    if "sha256" in expected:
        record["sha256"] = _bounded_sha256_at(parent_fd, name, observed)
    if "link_target" in expected:
        record["link_target"] = (
            os.readlink(name, dir_fd=parent_fd) if stat.S_ISLNK(observed.st_mode) else None
        )
    return record


def _mismatched(record: Mapping[str, object] | None, expected: Mapping[str, object]) -> list[str]:
    if record is None:
        return ["missing"]
    return sorted(key for key in expected if record.get(key) != expected[key])


_PATH_DIRECTORY_FLAGS = (
    getattr(os, "O_PATH", os.O_RDONLY)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _open_path_chain(path: Path) -> int:
    """Open an absolute directory as an ``O_PATH|O_NOFOLLOW`` descriptor.

    Every component is resolved relative to its already open parent without
    following a symlink, so every later ``fstatat``/``renameat2`` on the
    returned descriptor names an entry of exactly this directory inode.
    """

    if not path.is_absolute() or ".." in path.parts:
        raise UnsafeInstallPathError(f"unsafe directory path: {path}")
    descriptor = os.open("/", _PATH_DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            try:
                next_descriptor = os.open(component, _PATH_DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise UnsafeInstallPathError(
                    f"directory path contains an unsafe component: {path}"
                ) from exc
            os.close(descriptor)
            descriptor = next_descriptor
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise UnsafeInstallPathError(f"directory path component is not a directory: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_existing_directory(path: Path) -> int | None:
    try:
        return _open_path_chain(path)
    except FileNotFoundError:
        return None


def _fsync_directory_at(path_fd: int) -> None:
    """fsync the directory an ``O_PATH`` descriptor names (O_PATH cannot fsync)."""

    descriptor = os.open(
        ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=path_fd,
    )
    try:
        held = os.fstat(path_fd)
        opened = os.fstat(descriptor)
        if (held.st_dev, held.st_ino) != (opened.st_dev, opened.st_ino):
            raise InstallDriftError("directory changed while it was synced")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _authority_parent(
    authority: object, step: Mapping[str, object]
) -> tuple[int, int]:
    """The source parent a prepared quarantine entry bound, after shape checks."""

    expected = step.get("expected")
    parent = authority.get("source_parent") if isinstance(authority, Mapping) else None
    if (
        not isinstance(authority, Mapping)
        or set(authority) != {"source", "source_parent", "destination"}
        or not isinstance(expected, Mapping)
        or authority.get("source") != {"dev": expected.get("dev"), "ino": expected.get("ino")}
        or authority.get("destination") != step.get("destination")
        or not isinstance(parent, Mapping)
        or set(parent) != {"dev", "ino"}
        or type(parent.get("dev")) is not int
        or type(parent.get("ino")) is not int
    ):
        raise InstallPlanError(
            f"legacy quarantine entry lacks its prepared authority: {step.get('step_id')}"
        )
    return int(parent["dev"]), int(parent["ino"])  # type: ignore[arg-type]


def _require_parent(descriptor: int, identity: tuple[int, int], path: Path) -> None:
    observed = os.fstat(descriptor)
    if (observed.st_dev, observed.st_ino) != identity:
        raise InstallDriftError(
            f"the parent directory of {path} is not the one the prepared quarantine "
            "entry recorded; nothing was moved"
        )


def _open_quarantine_chain(directory: Path, root: Path, *, create: bool) -> int | None:
    """Open ``directory`` at or below ``root`` without following any symlink.

    Each component is opened relative to its verified parent.  An ancestor of
    the quarantine root must be a root-owned directory no other account can
    write; the root and every directory below it must be root-owned 0700.
    Missing components are created root-only 0700 when ``create`` is true;
    otherwise ``None`` is returned at the first missing component.
    """

    if directory != root and root not in directory.parents:
        raise UnsafeInstallPathError(
            f"quarantine destination {directory} is not below its root {root}"
        )
    descriptor = os.open("/", _DIRECTORY_OPEN_FLAGS)
    current = Path("/")
    try:
        _validate_quarantine_ancestor(os.fstat(descriptor), current)
        for component in directory.parts[1:]:
            current = current / component
            created = False
            try:
                next_descriptor = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    descriptor = -1
                    return None
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                try:
                    next_descriptor = os.open(
                        component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor
                    )
                except OSError as exc:
                    raise UnsafeInstallPathError(
                        f"quarantine path contains an unsafe component: {current}"
                    ) from exc
            except OSError as exc:
                raise UnsafeInstallPathError(
                    f"quarantine path contains an unsafe component: {current}"
                ) from exc
            try:
                if created:
                    os.fchmod(next_descriptor, 0o700)
                    os.fsync(descriptor)
                validator = (
                    _validate_quarantine_directory
                    if current == root or root in current.parents
                    else _validate_quarantine_ancestor
                )
                validator(os.fstat(next_descriptor), current)
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _quarantine_chain_problem(directory: Path, root: Path) -> str | None:
    """Read-only check of the existing part of a quarantine destination chain."""

    try:
        descriptor = _open_quarantine_chain(directory, root, create=False)
    except UnsafeInstallPathError as exc:
        return str(exc)
    if descriptor is not None:
        os.close(descriptor)
    return None


def _nearest_existing_device(directory: Path) -> int:
    """``st_dev`` of the deepest existing directory on the way to ``directory``."""

    descriptor = os.open("/", _DIRECTORY_OPEN_FLAGS)
    try:
        for component in directory.parts[1:]:
            try:
                next_descriptor = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            except OSError:
                break
            os.close(descriptor)
            descriptor = next_descriptor
        return _filesystem_device(descriptor)
    finally:
        os.close(descriptor)


def _legacy_quarantine_state(step: Mapping[str, object]) -> dict[str, object]:
    """Where a quarantined object is: its source, its destination, and the chain."""

    source, destination, root = _quarantine_layout(step)
    expected = step["expected"]
    assert isinstance(expected, Mapping)
    source_record: dict[str, object] | None = None
    source_parent: dict[str, int] | None = None
    parent_fd = _open_existing_directory(source.parent)
    if parent_fd is not None:
        try:
            parent = os.fstat(parent_fd)
            source_parent = {"dev": parent.st_dev, "ino": parent.st_ino}
            source_record = _quarantine_object_record(parent_fd, source.name, expected)
        finally:
            os.close(parent_fd)
    destination_record: dict[str, object] | None = None
    destination_fd = _open_existing_directory(destination.parent)
    if destination_fd is not None:
        try:
            destination_record = _quarantine_object_record(
                destination_fd, destination.name, expected
            )
        finally:
            os.close(destination_fd)
    return {
        "exists": source_record is not None,
        "source": source_record,
        "source_parent": source_parent,
        "destination": destination_record,
        "destination_device": _nearest_existing_device(destination.parent),
        "destination_chain": _quarantine_chain_problem(destination.parent, root),
    }


def _rename_failure(exc: OSError, source: Path, destination: Path) -> InstallError:
    if exc.errno == errno.EXDEV:
        return InstallDriftError(
            f"{source} and {destination} are on another filesystem for rename(2); "
            "the installer never copies a legacy object, so it stays in place"
        )
    if exc.errno in _RENAME_UNSUPPORTED_ERRNOS:
        return InstallError(
            f"renameat2 with RENAME_NOREPLACE is unavailable for {source} "
            f"({exc.strerror or exc}); the quarantine refuses to move without it"
        )
    return InstallDriftError(f"legacy quarantine could not move {source} to {destination}: {exc}")


def _undo_substituted_move(
    *,
    source_fd: int,
    source: Path,
    destination_fd: int,
    destination: Path,
    moved: dict[str, object] | None,
    changed: Sequence[str],
) -> InstallError:
    """Move a substitute that raced into quarantine straight back, and say so.

    Returns the error to raise: plain drift when the object is back at its
    original name, :class:`QuarantineSubstitutionError` (naming what now sits
    in quarantine) when even the move back is refused.
    """

    fields = ", ".join(changed)
    try:
        _renameat2_noreplace(destination_fd, destination.name, source_fd, source.name)
    except OSError as exc:
        for sync in (lambda: os.fsync(destination_fd), lambda: _fsync_directory_at(source_fd)):
            try:
                sync()
            except OSError:
                pass
        return QuarantineSubstitutionError(
            f"legacy quarantine moved an object that is not the inventoried one "
            f"({fields}) from {source} into {destination} and could not move it back "
            f"({exc.strerror or exc}); it stays there as an unexpected object and is "
            "never restored as legacy",
            unexpected=moved,
        )
    os.fsync(destination_fd)
    _fsync_directory_at(source_fd)
    return InstallDriftError(
        f"legacy quarantine found another object at {source} when it renamed it "
        f"({fields}); the object was moved back and nothing was quarantined"
    )


def _legacy_quarantine_move(entry: Mapping[str, object]) -> dict[str, object]:
    """Move one inventoried legacy object into its private quarantine slot.

    Only a prepared journal entry authorizes a move: it binds the source inode
    and the source's parent directory inode.  The parent is opened
    ``O_PATH|O_NOFOLLOW`` component by component and must be that directory;
    every check (type, owner, mode, inode and, where the inventory recorded
    one, content digest or link target) and the ``renameat2(RENAME_NOREPLACE)``
    run on that one descriptor.  The destination chain is root-only, on the
    source's filesystem, and the destination must not exist.

    A writer of the source's parent can still swap the name between the last
    check and the rename.  The object that arrived is therefore verified at
    once, by the same no-follow checks; anything else is renamed straight back
    and the move fails as drift.  If the move back is refused too, the
    substitute is reported (never completed) so the caller can record it.
    """

    step = entry.get("step")
    if not isinstance(step, Mapping):
        raise InstallPlanError("invalid legacy quarantine entry")
    source, destination, root = _quarantine_layout(step)
    expected = step["expected"]
    assert isinstance(expected, Mapping)
    parent_identity = _authority_parent(entry.get("quarantine_authority"), step)
    try:
        source_fd = _open_path_chain(source.parent)
    except FileNotFoundError as exc:
        raise InstallDriftError(f"legacy quarantine source is missing: {source}") from exc
    try:
        _require_parent(source_fd, parent_identity, source)
        changed = _mismatched(
            _quarantine_object_record(source_fd, source.name, expected), expected
        )
        if changed == ["missing"]:
            raise InstallDriftError(f"legacy quarantine source is missing: {source}")
        if changed:
            raise InstallDriftError(
                f"legacy quarantine source changed since the inventory: {source} "
                f"({', '.join(changed)}); nothing was moved"
            )
        destination_fd = _open_quarantine_chain(destination.parent, root, create=True)
        assert destination_fd is not None
        try:
            if _filesystem_device(destination_fd) != expected["dev"]:
                raise InstallDriftError(
                    f"legacy quarantine destination {destination} is on another "
                    f"filesystem than {source}; a rename cannot move it there and the "
                    "installer never copies a legacy object"
                )
            if _stat_at(destination_fd, destination.name) is not None:
                raise InstallDriftError(
                    f"legacy quarantine destination already exists: {destination}; "
                    f"{source} was left in place"
                )
            try:
                _renameat2_noreplace(
                    source_fd, source.name, destination_fd, destination.name
                )
            except FileExistsError as exc:
                raise InstallDriftError(
                    f"legacy quarantine destination already exists: {destination}; "
                    f"{source} was left in place"
                ) from exc
            except OSError as exc:
                raise _rename_failure(exc, source, destination) from exc
            # Verify what actually arrived before anything else happens.
            moved = _quarantine_object_record(destination_fd, destination.name, expected)
            changed = _mismatched(moved, expected)
            if changed:
                raise _undo_substituted_move(
                    source_fd=source_fd,
                    source=source,
                    destination_fd=destination_fd,
                    destination=destination,
                    moved=moved,
                    changed=changed,
                )
            os.fsync(destination_fd)
            _fsync_directory_at(source_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    return _legacy_quarantine_state(step)


def _legacy_quarantine_restore(entry: Mapping[str, object]) -> None:
    """Move a quarantined object back to its original path, never over anything."""

    step = entry.get("step")
    prior = entry.get("prior")
    if not isinstance(step, Mapping) or not isinstance(prior, Mapping):
        raise InstallError("invalid legacy quarantine rollback entry")
    if entry.get("quarantine_unexpected") is not None:
        raise InstallDriftError(
            "legacy quarantine destination holds an unexpected object; it is never "
            f"restored as legacy: {step.get('destination')}"
        )
    source, destination, root = _quarantine_layout(step)
    parent_identity = _authority_parent(entry.get("quarantine_authority"), step)
    destination_fd = _open_quarantine_chain(destination.parent, root, create=False)
    if destination_fd is None:
        raise InstallDriftError(f"quarantined object is missing: {destination}")
    try:
        moved = _stat_at(destination_fd, destination.name)
        if (
            moved is None
            or _file_type_name(moved.st_mode) != prior.get("type")
            or (moved.st_dev, moved.st_ino) != (prior.get("dev"), prior.get("ino"))
        ):
            raise InstallDriftError(
                f"quarantined object does not match its prior inode: {destination}"
            )
        try:
            source_fd = _open_path_chain(source.parent)
        except FileNotFoundError as exc:
            raise InstallDriftError(
                f"the original parent of {source} is gone; the legacy object stays "
                f"in quarantine at {destination}"
            ) from exc
        try:
            observed_parent = os.fstat(source_fd)
            if (observed_parent.st_dev, observed_parent.st_ino) != parent_identity:
                raise InstallDriftError(
                    f"the parent directory of {source} is not the one the quarantine "
                    f"recorded; the legacy object stays in quarantine at {destination}"
                )
            occupied = (
                f"original path is occupied: {source}; the legacy object stays in "
                f"quarantine at {destination}"
            )
            if _stat_at(source_fd, source.name) is not None:
                raise InstallDriftError(occupied)
            try:
                _renameat2_noreplace(
                    destination_fd, destination.name, source_fd, source.name
                )
            except FileExistsError as exc:
                raise InstallDriftError(occupied) from exc
            except OSError as exc:
                raise _rename_failure(exc, destination, source) from exc
            _fsync_directory_at(source_fd)
            os.fsync(destination_fd)
            restored = _stat_at(source_fd, source.name)
            if restored is None or (restored.st_dev, restored.st_ino) != (
                prior.get("dev"),
                prior.get("ino"),
            ):
                raise InstallDriftError(
                    f"restored object is not the quarantined inode: {source}"
                )
        finally:
            os.close(source_fd)
    finally:
        os.close(destination_fd)


def _mount_id(descriptor: int) -> int:
    """The id of the mount that holds ``descriptor`` (``/proc/self/fdinfo``).

    Unlike ``st_dev`` it also tells a bind mount of the same filesystem apart.
    Without it nothing proves a walk stays on one mount, so it fails closed.
    """

    try:
        with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as handle:
            for line in handle:
                field, _separator, value = line.partition(":")
                if field == "mnt_id":
                    return int(value.strip())
    except (OSError, ValueError) as exc:
        raise InstallDriftError(f"cannot prove which mount holds a tree member: {exc}") from exc
    raise InstallDriftError("cannot prove which mount holds a tree member: no mnt_id")


def _mount_point_problem(relative: str) -> str:
    return (
        f"tree member {relative or '.'} is a mount point (another filesystem or mount "
        "than the tree); nothing in the tree is removed"
    )


def _sha256_at(
    parent_fd: int,
    name: str,
    observed: os.stat_result,
    *,
    mount_id: int | None = None,
    relative: str | None = None,
) -> str:
    descriptor = os.open(name, _QUARANTINE_READ_FLAGS, dir_fd=parent_fd)
    try:
        held = os.fstat(descriptor)
        if not stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) != (
            observed.st_dev,
            observed.st_ino,
        ):
            raise InstallDriftError(f"tree member changed while it was hashed: {name}")
        if mount_id is not None and _mount_id(descriptor) != mount_id:
            raise InstallDriftError(_mount_point_problem(relative or name))
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


#: A tree member recorded for removal: (dev, ino, type).
_TreeManifest = dict[str, tuple[int, int, str]]


def _tree_digest_at(parent_fd: int, name: str) -> tuple[str, _TreeManifest]:
    """``_tree_sha256`` of ``name`` under ``parent_fd``, walked by descriptors.

    Every directory is opened ``O_NOFOLLOW`` relative to its verified parent,
    so the walk never leaves the tree through a symlink.  Besides the digest
    it returns the manifest of every member (relative path -> dev, ino,
    type): a later removal deletes nothing that is not in it.

    The tree must lie on one mount: a member (or the tree itself) whose
    ``st_dev`` or mount id differs from the tree's -- a mount point, bind
    mounts of the same filesystem included -- fails the whole walk before
    anything could be removed.
    """

    root_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
    manifest: _TreeManifest = {}
    records: list[tuple[str, bytes]] = []
    try:
        top = os.fstat(root_fd)
        root_mount = _mount_id(root_fd)
        if top.st_dev != os.fstat(parent_fd).st_dev or root_mount != _mount_id(parent_fd):
            raise InstallDriftError(_mount_point_problem(""))
        manifest[""] = (top.st_dev, top.st_ino, "directory")

        def walk(directory_fd: int, prefix: str) -> None:
            for entry in sorted(os.listdir(directory_fd)):
                relative = f"{prefix}/{entry}" if prefix else entry
                observed = os.stat(entry, dir_fd=directory_fd, follow_symlinks=False)
                if observed.st_dev != top.st_dev:
                    raise InstallDriftError(_mount_point_problem(relative))
                kind = _file_type_name(observed.st_mode)
                manifest[relative] = (observed.st_dev, observed.st_ino, kind)
                if relative == ".cortex-tree.sha256":
                    continue  # the marker that records this very digest
                record = relative.encode("utf-8") + b"\0"
                record += format(stat.S_IMODE(observed.st_mode), "04o").encode("ascii") + b"\0"
                if kind == "symlink":
                    target = os.readlink(entry, dir_fd=directory_fd)
                    record += b"L\0" + target.encode("utf-8") + b"\0"
                elif kind == "file":
                    file_sha256 = _sha256_at(
                        directory_fd, entry, observed, mount_id=root_mount, relative=relative
                    )
                    record += b"F\0" + file_sha256.encode("ascii") + b"\0"
                elif kind == "directory":
                    record += b"D\0"
                else:
                    raise InstallDriftError(f"tree contains an unsupported object: {relative}")
                records.append((relative, record))
                if kind == "directory":
                    child_fd = os.open(entry, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
                    try:
                        held = os.fstat(child_fd)
                        if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino):
                            raise InstallDriftError(f"tree changed while it was hashed: {relative}")
                        if _mount_id(child_fd) != root_mount:
                            raise InstallDriftError(_mount_point_problem(relative))
                        walk(child_fd, relative)
                    finally:
                        os.close(child_fd)

        walk(root_fd, "")
    finally:
        os.close(root_fd)
    digest = hashlib.sha256()
    for _relative, record in sorted(records, key=lambda row: row[0]):
        digest.update(record)
    return digest.hexdigest(), manifest


def _created_tree_identity(path: Path) -> dict[str, object]:
    """Inode and full tree digest of a directory tree a receipt just created.

    Recorded when a legacy adoption creates a tree where a quarantined legacy
    object stood, so rollback can remove exactly that tree -- and nothing a
    later writer added or changed -- before the legacy object moves back.
    """

    parent_fd = _open_path_chain(path.parent)
    try:
        observed = _stat_at(parent_fd, path.name)
        if observed is None or not stat.S_ISDIR(observed.st_mode):
            raise InstallDriftError(f"created tree is not a directory: {path}")
        tree_sha256, manifest = _tree_digest_at(parent_fd, path.name)
        if manifest[""][:2] != (observed.st_dev, observed.st_ino):
            raise InstallDriftError(f"created tree changed while it was recorded: {path}")
        return {"device": observed.st_dev, "inode": observed.st_ino, "tree_sha256": tree_sha256}
    finally:
        os.close(parent_fd)


def _discard_staging_path(quarantine_root: Path, key: str) -> Path:
    """The installer-owned private staging for one discard: ``<root>/.discard/<hash>``."""

    return Path(quarantine_root) / ".discard" / hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _remove_verified_tree(parent_fd: int, name: str, manifest: _TreeManifest) -> None:
    """Delete ``name`` under ``parent_fd`` member by member, only as the manifest proves.

    The walk runs on ``O_NOFOLLOW`` directory descriptors and never follows a
    symlink (a symlink member is unlinked, not entered).  The first member that
    is not the verified one stops the removal: what remains stays where it is.
    A member on another filesystem or mount than the tree (a mount point that
    appeared after the manifest was proven) stops it the same way, before
    anything below it is touched.
    """

    def expect(relative: str, observed: os.stat_result) -> None:
        if manifest.get(relative) != (
            observed.st_dev,
            observed.st_ino,
            _file_type_name(observed.st_mode),
        ):
            raise InstallDriftError(
                f"{relative or name} was not part of the verified tree; nothing more "
                "is removed and the rest stays in the private discard staging"
            )

    def same_mount(relative: str, observed: os.stat_result, descriptor: int | None) -> None:
        if observed.st_dev != top.st_dev or (
            descriptor is not None and _mount_id(descriptor) != root_mount
        ):
            raise InstallDriftError(
                f"{relative or name} is a mount point (another filesystem or mount than "
                "the tree); nothing more is removed and the rest stays in the private "
                "discard staging"
            )

    def clear(directory_fd: int, prefix: str) -> None:
        for entry in sorted(os.listdir(directory_fd)):
            relative = f"{prefix}/{entry}" if prefix else entry
            observed = os.stat(entry, dir_fd=directory_fd, follow_symlinks=False)
            same_mount(relative, observed, None)
            expect(relative, observed)
            if stat.S_ISDIR(observed.st_mode):
                child_fd = os.open(entry, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
                try:
                    held = os.fstat(child_fd)
                    same_mount(relative, held, child_fd)
                    expect(relative, held)
                    clear(child_fd, relative)
                finally:
                    os.close(child_fd)
                os.rmdir(entry, dir_fd=directory_fd)
            else:
                os.unlink(entry, dir_fd=directory_fd)

    expect("", os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
    root_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
    try:
        top = os.fstat(root_fd)
        root_mount = _mount_id(root_fd)
        expect("", top)
        if top.st_dev != os.fstat(parent_fd).st_dev or root_mount != _mount_id(parent_fd):
            raise InstallDriftError(
                f"{name} is a mount point; nothing is removed and it stays in the "
                "private discard staging"
            )
        clear(root_fd, "")
    finally:
        os.close(root_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _stage_for_discard(
    *,
    parent_fd: int,
    path: Path,
    staging: Path,
    staging_fd: int,
    leaf: str,
    device: int,
) -> None:
    """Move ``path`` into the private staging (same filesystem, never replacing)."""

    if _stat_at(staging_fd, leaf) is not None:
        raise InstallDriftError(
            f"a previous discard of {path} is pending in {staging / leaf}; it is kept "
            "for the operator"
        )
    if _filesystem_device(staging_fd) != device:
        raise InstallDriftError(
            f"{path} is on another filesystem than the private discard staging "
            f"{staging}; it is kept (nothing is copied or deleted in place)"
        )
    try:
        _renameat2_noreplace(parent_fd, path.name, staging_fd, leaf)
    except OSError as exc:
        raise _rename_failure(exc, path, staging / leaf) from exc
    _fsync_directory_at(parent_fd)
    os.fsync(staging_fd)


def _restore_from_discard(
    *,
    parent_fd: int,
    path: Path,
    staging: Path,
    staging_fd: int,
    leaf: str,
    problem: str,
) -> InstallDriftError:
    """Move an unproven object back to its path; say where it is either way."""

    try:
        _renameat2_noreplace(staging_fd, leaf, parent_fd, path.name)
    except OSError as exc:
        return InstallDriftError(
            f"{problem}; it could not be moved back ({exc.strerror or exc}) and is left "
            f"in the private discard staging {staging / leaf}"
        )
    _fsync_directory_at(parent_fd)
    os.fsync(staging_fd)
    return InstallDriftError(f"{problem}; it was moved back and is kept")


def _discard_created_tree(
    path: Path,
    identity: Mapping[str, object],
    *,
    quarantine_root: Path,
    key: str,
) -> None:
    """Remove a tree this receipt created, only while it is exactly that tree.

    The tree is first renamed (``RENAME_NOREPLACE``) into an installer-owned,
    root-only 0700 staging directory below the quarantine root, on the same
    filesystem, so no other account can reach it by path any more.  Only
    there are its inode and full tree digest verified; an unproven tree is
    renamed straight back.  The removal then walks descriptors without
    following symlinks and deletes only members of the verified manifest.
    """

    if (
        type(identity.get("device")) is not int
        or type(identity.get("inode")) is not int
        or not isinstance(identity.get("tree_sha256"), str)
    ):
        raise InstallDriftError(f"no creation identity binds the tree at {path}")
    staging = _discard_staging_path(quarantine_root, key)
    parent_fd = _open_path_chain(path.parent)
    try:
        pending_fd = _open_quarantine_chain(staging, Path(quarantine_root), create=False)
        if pending_fd is not None:
            try:
                if _stat_at(pending_fd, "tree") is not None:
                    raise InstallDriftError(
                        f"a previous discard of {path} is pending in {staging / 'tree'}; "
                        "it is kept for the operator"
                    )
            finally:
                os.close(pending_fd)
        observed = _stat_at(parent_fd, path.name)
        if observed is None:
            return
        if not stat.S_ISDIR(observed.st_mode) or (observed.st_dev, observed.st_ino) != (
            identity["device"],
            identity["inode"],
        ):
            raise InstallDriftError(f"{path} is not the tree this receipt created; it is kept")
        staging_fd = _open_quarantine_chain(staging, Path(quarantine_root), create=True)
        assert staging_fd is not None
        try:
            _stage_for_discard(
                parent_fd=parent_fd,
                path=path,
                staging=staging,
                staging_fd=staging_fd,
                leaf="tree",
                device=observed.st_dev,
            )
            problem: str | None = None
            try:
                digest, manifest = _tree_digest_at(staging_fd, "tree")
                if manifest[""][:2] != (
                    identity["device"],
                    identity["inode"],
                ) or digest != identity["tree_sha256"]:
                    problem = f"{path} changed since this receipt created it"
            except (InstallError, OSError) as exc:
                # A mount point inside, a member that changed while it was
                # hashed, an unsupported object: nothing is removed.
                problem = f"{path} cannot be proven to be the tree this receipt created ({exc})"
                manifest = {}
            if problem is not None:
                raise _restore_from_discard(
                    parent_fd=parent_fd,
                    path=path,
                    staging=staging,
                    staging_fd=staging_fd,
                    leaf="tree",
                    problem=problem,
                )
            _remove_verified_tree(staging_fd, "tree", manifest)
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
    finally:
        os.close(parent_fd)


def _remove_created_credential_directories(
    rows: Sequence[object],
    destination: Path,
    *,
    quarantine_root: Path | None,
    key: str,
) -> str | None:
    """Remove the (empty) directories a legacy-receipt import created, innermost first.

    Each directory is renamed into the private discard staging first and only
    there proven to be the recorded inode and empty; ``rmdir`` then removes
    it (and would refuse a directory that gained an entry meanwhile).  An
    unproven directory goes straight back.  Returns why a directory is kept,
    or ``None`` when all are gone.
    """

    if quarantine_root is None:
        return "credential directories are removed only through a legacy quarantine root"
    for item in reversed(list(rows)):
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("path"), str)
            or type(item.get("dev")) is not int
            or type(item.get("ino")) is not int
        ):
            return "credential directory record is invalid"
        path = Path(str(item["path"]))
        if path not in destination.parents:
            return "credential directory record is outside the destination"
        try:
            parent_fd = _open_path_chain(path.parent)
        except FileNotFoundError:
            continue
        try:
            observed = _stat_at(parent_fd, path.name)
            if observed is None:
                continue
            if not stat.S_ISDIR(observed.st_mode) or (observed.st_dev, observed.st_ino) != (
                item["dev"],
                item["ino"],
            ):
                return f"credential directory created by import was replaced: {path}"
            staging = _discard_staging_path(Path(quarantine_root), f"{key}\0{path}")
            staging_fd = _open_quarantine_chain(staging, Path(quarantine_root), create=True)
            assert staging_fd is not None
            try:
                try:
                    _stage_for_discard(
                        parent_fd=parent_fd,
                        path=path,
                        staging=staging,
                        staging_fd=staging_fd,
                        leaf="dir",
                        device=observed.st_dev,
                    )
                except InstallError as exc:
                    return str(exc)
                problem: str | None = None
                try:
                    held_fd = os.open("dir", _DIRECTORY_OPEN_FLAGS, dir_fd=staging_fd)
                    try:
                        held = os.fstat(held_fd)
                        if (held.st_dev, held.st_ino) != (item["dev"], item["ino"]):
                            problem = f"credential directory created by import was replaced: {path}"
                        elif os.listdir(held_fd):
                            problem = f"credential directory created by import is not empty: {path}"
                    finally:
                        os.close(held_fd)
                    if problem is None:
                        # rmdir refuses a directory that gained an entry meanwhile.
                        os.rmdir("dir", dir_fd=staging_fd)
                        os.fsync(staging_fd)
                        continue
                except OSError as exc:
                    problem = f"credential directory created by import could not be removed: {path} ({exc})"
                return str(
                    _restore_from_discard(
                        parent_fd=parent_fd,
                        path=path,
                        staging=staging,
                        staging_fd=staging_fd,
                        leaf="dir",
                        problem=problem,
                    )
                )
            finally:
                os.close(staging_fd)
        finally:
            os.close(parent_fd)
    return None


def _refuse_unprepared_quarantine(step: Mapping[str, object]) -> None:
    if step.get("kind") == "legacy-quarantine":
        raise InstallPlanError(
            "a legacy quarantine moves only through its prepared journal entry "
            f"(quarantine_step): {step.get('step_id')}"
        )


class LocalInstallBackend:
    """Real Linux implementation; construction itself enforces the root boundary."""

    # Recent directory inventories by digest, so binding a just-inspected
    # prior does not walk a large tree again.  Deliberately tiny: a single
    # large state tree can hold hundreds of thousands of rows.
    _INVENTORY_CACHE_LIMIT = 2

    def __init__(self, *, require_root: bool = True) -> None:
        if require_root and os.geteuid() != 0:
            raise PermissionError("trust-root apply/activate/verify/rollback requires root")

    def _remember_inventory(self, digest: str, rows: tuple[str, ...]) -> None:
        cache = self.__dict__.setdefault("_inventory_cache", {})
        cache.pop(digest, None)
        cache[digest] = rows
        while len(cache) > self._INVENTORY_CACHE_LIMIT:
            cache.pop(next(iter(cache)))

    def bind_rollback_inventory(
        self,
        receipt: InstallReceipt,
        step: Mapping[str, object],
        state: Mapping[str, object],
    ) -> None:
        """Bind the descendant list behind a directory prior's digest.

        Called by the transaction before the prepared entry holding ``state``
        is persisted.  The rows must hash to the recorded digest; a tree that
        changed since inspection is drift, never a silently rebased baseline.
        """

        if "children_sha256" not in state and "children_count" not in state:
            return
        digest = state.get("children_sha256")
        count = state.get("children_count")
        if (
            not isinstance(digest, str)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            raise InstallDriftError(
                f"directory prior has an invalid inventory binding: {step.get('step_id')}"
            )
        if count == 0:
            # An empty baseline is fully determined by its digest.
            return
        rows = self.__dict__.get("_inventory_cache", {}).get(digest)
        if rows is None:
            rows = tuple(_directory_inventory(Path(str(step.get("path", "")))))
        if len(rows) != count or _directory_inventory_sha256(rows) != digest:
            raise InstallDriftError(
                "managed directory changed before its rollback inventory was "
                f"bound: {step.get('step_id')}"
            )
        if receipt.bind_directory_inventory(rows) != digest:
            raise InstallDriftError(
                f"rollback inventory binding digest mismatch: {step.get('step_id')}"
            )

    def preflight_facts(self, plan: Mapping[str, object]) -> Mapping[str, object]:
        roots = plan.get("roots", {})
        deploy = Path(str(roots.get("deploy", "/opt/cortex"))) if isinstance(roots, Mapping) else Path("/opt/cortex")
        desired_accounts = [
            row
            for key in ("accounts", "service_accounts")
            for row in plan.get(key, [])
            if isinstance(row, Mapping)
        ]
        passwd_records = list(pwd.getpwall())
        group_records = list(grp.getgrall())
        services: dict[str, str] = {}
        for name in (
            "cortex-egress-proxy.service",
            "cortex-manager.service",
            "cortex-monitor.service",
        ):
            result = (
                _run(("systemctl", "is-active", name))
                if shutil.which("systemctl")
                else None
            )
            load_state = (
                _run(
                    (
                        "systemctl",
                        "show",
                        "--property=LoadState",
                        "--value",
                        name,
                    )
                )
                if result is not None and result.returncode == 4
                else None
            )
            services[name] = (
                _classify_systemctl_is_active(result, load_state)
                if result is not None
                else "error"
            )
        accounts: dict[str, dict[str, object]] = {}
        for row in desired_accounts:
            if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
                continue
            name = str(row["name"])
            try:
                record = pwd.getpwnam(name)
            except KeyError:
                continue
            accounts[name] = {
                "name": name,
                "uid": record.pw_uid,
                "gid": record.pw_gid,
                "home": record.pw_dir,
                "shell": record.pw_shell,
                "supplementary_groups": sorted(
                    group.gr_name
                    for group in group_records
                    if name in group.gr_mem
                ),
                "password_locked": _password_locked(name),
            }
        account_uids = {record.pw_uid: record.pw_name for record in passwd_records}
        all_groups = {record.gr_name: record for record in group_records}
        group_gids = {record.gr_gid: record.gr_name for record in all_groups.values()}
        primary_gid_users: dict[int, list[str]] = {}
        for record in passwd_records:
            primary_gid_users.setdefault(record.pw_gid, []).append(record.pw_name)
        primary_gid_users = {
            gid: sorted(set(names)) for gid, names in primary_gid_users.items()
        }
        group_names_by_gid: dict[int, list[str]] = {}
        for record in group_records:
            group_names_by_gid.setdefault(record.gr_gid, []).append(record.gr_name)
        group_names_by_gid = {
            gid: sorted(set(names)) for gid, names in group_names_by_gid.items()
        }
        groups = {
            name: {
                "name": name,
                "gid": all_groups[name].gr_gid,
                "members": sorted(set(all_groups[name].gr_mem)),
            }
            for row in desired_accounts
            if isinstance(row, Mapping) and isinstance(row.get("name"), str)
            for name in (str(row["name"]),)
            if name in all_groups
        }
        paths: dict[str, dict[str, object]] = {}
        for step in plan.get("apply_order", []):
            if not isinstance(step, Mapping) or not isinstance(step.get("path"), str):
                continue
            path = Path(str(step["path"]))
            try:
                observed = path.lstat()
            except FileNotFoundError:
                paths[str(path)] = {"exists": False, "is_symlink": False}
            else:
                paths[str(path)] = {
                    "exists": True,
                    "is_symlink": stat.S_ISLNK(observed.st_mode),
                }
                if stat.S_ISREG(observed.st_mode):
                    # Lets preflight refuse an oversized prior file up front
                    # instead of the snapshot failing mid-transaction.
                    paths[str(path)]["size"] = observed.st_size
        try:
            disk_free = shutil.disk_usage(deploy.parent).free
        except OSError:
            disk_free = 0
        return {
            "systemd": Path("/run/systemd/system").is_dir()
            and shutil.which("systemctl") is not None,
            "polkit": Path("/usr/share/polkit-1").is_dir(),
            "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
            "acl": shutil.which("getfacl") is not None and shutil.which("setfacl") is not None,
            "disk_free_bytes": disk_free,
            "universal_nopasswd": _universal_nopasswd(),
            "in_flight_jobs": _in_flight_process_count(desired_accounts)
            + _durable_in_flight_job_count(plan),
            "services": services,
            "accounts": accounts,
            "account_uids": account_uids,
            "group_gids": group_gids,
            "groups": groups,
            "primary_gid_users": primary_gid_users,
            "group_names_by_gid": group_names_by_gid,
            "paths": paths,
        }

    def inspect_venv_activation(
        self, step: Mapping[str, object]
    ) -> Mapping[str, object]:
        if step.get("kind") != "venv":
            raise InstallPlanError("venv activation inspection requires a venv step")
        return _venv_activation_state(step)

    def inspect_step(self, step: Mapping[str, object]) -> Mapping[str, object]:
        if step.get("kind") == "legacy-quarantine":
            return _legacy_quarantine_state(step)
        if step.get("kind") == "account":
            return _account_state(step)
        if step.get("kind") == "venv":
            return _venv_state(step)
        if step.get("kind") == "toolchain":
            return _toolchain_state(step)
        if step.get("kind") == "repository":
            return _repository_state(step)
        if step.get("kind") == "systemctl":
            if step.get("action") == "daemon-reload":
                return {
                    "exists": True,
                    "installed_sha256": step.get("desired_sha256"),
                }
            name = str(step.get("unit", ""))
            result = _run(("systemctl", "is-enabled", name))
            enabled = result.returncode == 0
            return {
                "exists": enabled,
                "installed_sha256": step.get("desired_sha256") if enabled else None,
            }
        if step.get("kind") != "asset":
            raise InstallPlanError(f"unsupported step kind: {step.get('kind')}")
        if step.get("asset_type") == "symlink":
            return _symlink_state(step)
        path = Path(str(step.get("path")))
        observed = _snapshot(path, inventory_sink=self._remember_inventory)
        if not observed.get("exists"):
            return observed
        if step.get("asset_type") == "directory":
            actual_mode = observed.get("mode")
            actual_acls = sorted(
                observed.get("acl", []),
                key=lambda row: (
                    bool(row.get("default")),
                    str(row.get("entry_type", "user")),
                    str(row.get("account")),
                    str(row.get("perms")),
                ),
            )
            matches = (
                observed.get("is_directory") is True
                and observed.get("owner") == step.get("owner")
                and observed.get("group") == step.get("group")
                and actual_mode == _expected_acl_mode(step)
                and actual_acls == _expected_acls(step)
            )
            observed["observed_mode"] = actual_mode
            observed["observed_acl"] = actual_acls
            observed["installed_sha256"] = (
                step.get("desired_sha256") if matches else None
            )
            if matches:
                # The transaction layer compares the plan's base mode and ACL
                # spelling. Preserve the kernel-observed values above while
                # reporting the matching desired-state semantics here.
                observed["mode"] = step.get("mode")
                observed["acl"] = list(step.get("acls", []))
        return observed

    def creation_authority_matches(
        self, step: Mapping[str, object], authority: Mapping[str, object]
    ) -> bool:
        if step.get("kind") == "repository":
            pass
        elif step.get("kind") != "asset" or step.get(
            "asset_type", "file"
        ) not in {"file", "directory", "symlink"}:
            return False
        return _path_matches_creation_authority(
            Path(str(step.get("path", ""))), authority
        )

    def apply_step(self, step: Mapping[str, object]) -> Mapping[str, object]:
        _refuse_unprepared_quarantine(step)
        return self._apply_step(
            step,
            expected_prior=None,
            creation_checkpoint=None,
        )

    def quarantine_step(self, entry: Mapping[str, object]) -> Mapping[str, object]:
        """Move a legacy object under the authority of its prepared entry."""

        return _legacy_quarantine_move(entry)

    def created_tree_identity(self, step: Mapping[str, object]) -> Mapping[str, object]:
        """Bind a tree a legacy adoption created (repository clone) to its inode."""

        return _created_tree_identity(Path(str(step.get("path", ""))))

    def discard_created_tree(
        self,
        path: str,
        identity: Mapping[str, object],
        *,
        quarantine_root: str,
        key: str,
    ) -> None:
        """Legacy rollback: remove a tree this receipt created, only if unchanged."""

        _discard_created_tree(
            Path(path), identity, quarantine_root=Path(quarantine_root), key=key
        )

    def apply_step_checkpointed(
        self,
        step: Mapping[str, object],
        expected_prior: Mapping[str, object],
        creation_checkpoint: Callable[[Mapping[str, object]], None],
    ) -> Mapping[str, object]:
        _refuse_unprepared_quarantine(step)
        return self._apply_step(
            step,
            expected_prior=expected_prior,
            creation_checkpoint=creation_checkpoint,
        )

    def replace_step_checkpointed(
        self,
        step: Mapping[str, object],
        expected_prior: Mapping[str, object],
        replacement_checkpoint: Callable[[Mapping[str, object]], None],
    ) -> Mapping[str, object]:
        """Replace only an exact receipt-proven leaf with durable authority."""

        if dict(self.inspect_step(step)) != dict(expected_prior):
            raise InstallDriftError(
                f"replacement prior drifted: {step.get('step_id')}"
            )
        if step.get("kind") == "repository":
            return self._replace_repository_step(
                step, expected_prior, replacement_checkpoint
            )
        if step.get("kind") != "asset":
            raise InstallPlanError(
                f"unsupported receipt-proven replacement: {step.get('kind')}"
            )
        return self._replace_asset_step(
            step, expected_prior, replacement_checkpoint
        )

    def cleanup_prepared_replacement(
        self, entry: Mapping[str, object]
    ) -> None:
        """Remove only an exact deterministic staging leaf after a crash.

        The prior-inode checkpoint is persisted before this name can be
        created.  An incomplete or foreign-looking staging leaf is retained
        and turns rollback into ``rollback-blocked`` instead of a false clean
        result.
        """

        step = entry.get("step")
        if (
            not isinstance(step, Mapping)
            or step.get("kind") != "asset"
            or step.get("asset_type", "file") not in {"file", "symlink"}
        ):
            return
        staging = _replacement_staging_path(step)
        if not (staging.exists() or staging.is_symlink()):
            return
        staged_step = _replacement_staging_step(step)
        installed = dict(self.inspect_step(staged_step))
        if not _replacement_staging_matches(staged_step, installed):
            raise InstallDriftError(
                f"prepared replacement staging is not receipt-bound: {staging}"
            )
        staging.unlink()
        parent_fd = os.open(
            staging.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def _replace_asset_step(
        self,
        step: Mapping[str, object],
        expected_prior: Mapping[str, object],
        replacement_checkpoint: Callable[[Mapping[str, object]], None],
    ) -> Mapping[str, object]:
        path = Path(str(step.get("path", "")))
        if not path.is_absolute() or ".." in path.parts:
            raise UnsafeInstallPathError(f"unsafe asset path: {path}")
        _reject_symlink_ancestors(path, label="asset", include_leaf=False)
        asset_type = step.get("asset_type", "file")
        if asset_type not in {"file", "directory", "symlink"}:
            raise InstallPlanError(f"unsupported asset_type: {asset_type}")

        parent_fd, leaf = _open_parent_directory(path)
        temporary_name: str | None = None
        descriptor: int | None = None
        prior_descriptor: int | None = None
        try:
            if asset_type == "directory":
                descriptor = os.open(
                    leaf,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
                authority = _snapshot_authority(
                    os.fstat(descriptor), file_type="directory"
                )
                replacement_checkpoint(authority)
                if dict(self.inspect_step(step)) != dict(expected_prior):
                    raise InstallDriftError(
                        f"replacement prior changed: {step.get('step_id')}"
                    )
                _assert_fd_path_binding(path, descriptor, directory=True)
                _apply_fd_asset_state(
                    descriptor,
                    owner=step.get("owner"),
                    group=step.get("group"),
                    mode=step.get("mode"),
                    acls=step.get("acls", []),
                    directory=True,
                )
                os.fsync(descriptor)
            elif asset_type == "file":
                content = step.get("content")
                if not isinstance(content, str):
                    raise InstallPlanError(
                        f"file step lacks content: {step.get('step_id')}"
                    )
                prior_descriptor = os.open(
                    leaf,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
                authority = _snapshot_authority(
                    os.fstat(prior_descriptor), file_type="file"
                )
                replacement_checkpoint(authority)
                if dict(self.inspect_step(step)) != dict(expected_prior):
                    raise InstallDriftError(
                        f"replacement prior changed: {step.get('step_id')}"
                    )
                _assert_fd_path_binding(path, prior_descriptor, directory=False)
                self.cleanup_prepared_replacement({"step": step})
                temporary_name = _replacement_staging_path(step).name
                descriptor = os.open(
                    temporary_name,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
                with os.fdopen(os.dup(descriptor), "wb") as stream:
                    stream.write(content.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                _apply_fd_asset_state(
                    descriptor,
                    owner=step.get("owner"),
                    group=step.get("group"),
                    mode=step.get("mode"),
                    acls=step.get("acls", []),
                    directory=False,
                )
                os.replace(
                    temporary_name,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_name = None
                os.fsync(parent_fd)
                _assert_fd_path_binding(path, descriptor, directory=False)
            else:
                target = Path(str(step.get("target", "")))
                if (
                    not target.is_absolute()
                    or ".." in target.parts
                    or not target.is_dir()
                    or target.is_symlink()
                ):
                    raise InstallDriftError(
                        f"symlink target is not an exact directory: {target}"
                    )
                prior_state = os.stat(
                    leaf,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                authority = _snapshot_authority(
                    prior_state, file_type="symlink"
                )
                replacement_checkpoint(authority)
                if (
                    dict(self.inspect_step(step)) != dict(expected_prior)
                    or not _path_matches_creation_authority(path, authority)
                ):
                    raise InstallDriftError(
                        f"replacement prior changed: {step.get('step_id')}"
                    )
                self.cleanup_prepared_replacement({"step": step})
                temporary_name = _replacement_staging_path(step).name
                os.symlink(
                    os.path.relpath(target, path.parent),
                    temporary_name,
                    dir_fd=parent_fd,
                )
                os.chown(
                    temporary_name,
                    _resolve_uid(step.get("owner")),
                    _resolve_gid(step.get("group")),
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                os.replace(
                    temporary_name,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_name = None
                os.fsync(parent_fd)
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            if descriptor is not None:
                os.close(descriptor)
            if prior_descriptor is not None:
                os.close(prior_descriptor)
            os.close(parent_fd)
        installed = dict(self.inspect_step(step))
        if installed.get("installed_sha256") != step.get("desired_sha256"):
            raise InstallDriftError(
                f"asset replacement did not reach desired state: {step.get('step_id')}"
            )
        return {
            "prior": dict(expected_prior),
            "replacement_authority": authority,
            **installed,
        }

    def _replace_repository_step(
        self,
        step: Mapping[str, object],
        expected_prior: Mapping[str, object],
        replacement_checkpoint: Callable[[Mapping[str, object]], None],
    ) -> Mapping[str, object]:
        source = Path(str(step.get("source", "")))
        path = Path(str(step.get("path", "")))
        source_sha = step.get("source_sha256")
        commit = step.get("commit")
        if (
            not source.is_absolute()
            or not path.is_absolute()
            or not isinstance(source_sha, str)
            or not isinstance(commit, str)
            or source.is_symlink()
            or not source.is_file()
            or source.lstat().st_nlink != 1
        ):
            raise InstallDriftError(
                "repository replacement source is missing or hash-mismatched"
            )
        _reject_symlink_ancestors(source, label="repository bundle", include_leaf=False)
        _reject_symlink_ancestors(path, label="repository", include_leaf=False)
        uid = _resolve_uid(step.get("owner"))
        gid = _resolve_gid(step.get("group"))
        descriptor: int | None = None
        source_descriptor: int | None = None
        try:
            source_descriptor = os.open(
                source,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            if _sha256_open_file(source_descriptor) != source_sha:
                raise InstallDriftError(
                    "repository replacement source hash changed"
                )
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            authority = _snapshot_authority(
                os.fstat(descriptor), file_type="directory"
            )
            replacement_checkpoint(authority)
            if dict(self.inspect_step(step)) != dict(expected_prior):
                raise InstallDriftError(
                    f"repository replacement prior changed: {step.get('slug')}"
                )
            _assert_fd_path_binding(path, descriptor, directory=True)
            prefix = (
                *_REPOSITORY_GIT_PREFIX,
                *_REPOSITORY_MUTATION_CONFIG,
                "-c",
                f"safe.directory={path}",
                "-C",
                str(path),
            )
            # The fetch names no destination ref and writes no FETCH_HEAD, and
            # the detached checkout moves only the main worktree's HEAD: the
            # Manager's branches, refs, and linked worktrees stay as inspected
            # (#1124).
            _run(
                (
                    *prefix,
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    f"/proc/self/fd/{source_descriptor}",
                    commit,
                ),
                check=True,
                env=_REPOSITORY_GIT_ENV,
                uid=uid,
                gid=gid,
                pass_fds=(source_descriptor,),
            )
            _refuse_runtime_root_checkout(prefix, commit, uid=uid, gid=gid)
            _refuse_ignored_content_overwrite(prefix, commit, uid=uid, gid=gid)
            _run(
                (*prefix, "checkout", "--detach", "--force", commit),
                check=True,
                env=_REPOSITORY_GIT_ENV,
                uid=uid,
                gid=gid,
            )
            _remove_group_other_write(path, skip=_runtime_worktree_paths(path))
            os.chmod(path, _mode(step.get("mode")))
            _assert_fd_path_binding(path, descriptor, directory=True)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if source_descriptor is not None:
                os.close(source_descriptor)
        installed = dict(self.inspect_step(step))
        if installed.get("installed_sha256") != step.get("desired_sha256"):
            raise InstallDriftError(
                f"repository replacement drifted: {step.get('slug')}"
                + _observed_drift_detail(installed)
            )
        if installed.get("runtime") != expected_prior.get("runtime"):
            raise InstallDriftError(
                "repository replacement changed Manager runtime state: "
                f"{step.get('slug')}"
            )
        return {
            "prior": dict(expected_prior),
            "replacement_authority": authority,
            **installed,
        }

    def _apply_step(
        self,
        step: Mapping[str, object],
        *,
        expected_prior: Mapping[str, object] | None,
        creation_checkpoint: Callable[[Mapping[str, object]], None] | None,
    ) -> Mapping[str, object]:
        kind = step.get("kind")
        inspected_prior = dict(self.inspect_step(step))
        if expected_prior is not None and inspected_prior != dict(expected_prior):
            if (
                kind == "venv"
                and creation_checkpoint is not None
                and isinstance(step.get("wheel_sha256"), str)
            ):
                replay_slot = Path(str(step.get("path", "")))
                replay_staging = _venv_staging_path(replay_slot)
                replay_tree = inspected_prior.get("tree_sha256")
                if (
                    not isinstance(replay_tree, str)
                    or not _venv_slot_matches(
                        replay_slot,
                        str(step["wheel_sha256"]),
                        tree_sha256=replay_tree,
                    )
                ):
                    raise InstallDriftError(
                        f"install step changed at backend boundary: {step.get('step_id')}"
                    )
                creation_checkpoint(
                    _venv_staging_authority(
                        replay_slot,
                        replay_staging,
                        state="ready",
                        tree_sha256=replay_tree,
                    )
                )
            else:
                raise InstallDriftError(
                    f"install step changed at backend boundary: {step.get('step_id')}"
                )
        if kind == "account":
            prior = inspected_prior
            if prior.get("exists"):
                if prior.get("installed_sha256") != step.get("desired_sha256"):
                    raise InstallDriftError(
                        f"existing account does not match desired identity: {step.get('name')}"
                    )
                return {"prior": prior, **prior}
            name = str(step.get("name", ""))
            uid = step.get("uid")
            gid = step.get("gid")
            home = step.get("home")
            login_program = step.get("login_program")
            if (
                not name
                or not isinstance(uid, int)
                or not isinstance(gid, int)
                or not isinstance(home, str)
                or not Path(home).is_absolute()
                or not isinstance(login_program, str)
                or not Path(login_program).is_absolute()
            ):
                raise InstallPlanError(f"invalid account step: {step!r}")
            try:
                existing_group = grp.getgrnam(name)
            except KeyError:
                _run(("groupadd", "--gid", str(gid), "--system", name), check=True)
            else:
                if existing_group.gr_gid != gid:
                    raise InstallDriftError(
                        f"existing group does not match desired gid: {name}"
                    )
            _run(
                (
                    "useradd",
                    "--uid",
                    str(uid),
                    "--gid",
                    str(gid),
                    "--home-dir",
                    home,
                    "--shell",
                    login_program,
                    "--no-create-home",
                    "--system",
                    name,
                ),
                check=True,
            )
            installed = _account_state(step)
            if installed.get("installed_sha256") != step.get("desired_sha256"):
                raise InstallDriftError(f"created account does not match plan: {name}")
            return {"prior": prior, **installed}
        if kind == "venv":
            prior = inspected_prior
            if prior.get("installed_sha256") == step.get("desired_sha256"):
                return {"prior": prior, **prior}
            slot = Path(str(step.get("path", "")))
            active = Path(str(step.get("active_link", "")))
            wheel = Path(str(step.get("wheel_source", "")))
            expected = step.get("wheel_sha256")
            wheelhouse = step.get("wheelhouse")
            if step.get("wheelhouse_locked") is not True:
                raise InstallPlanError("venv wheelhouse must be explicitly locked")
            if (
                not slot.is_absolute()
                or not active.is_absolute()
                or not wheel.is_absolute()
                or not isinstance(expected, str)
                or not isinstance(wheelhouse, list)
                or not wheelhouse
            ):
                raise InstallPlanError("venv step is not fully hash-bound")
            for label, candidate, digest in (
                ("candidate wheel", wheel, expected),
                *(
                    (
                        f"wheelhouse[{index}]",
                        Path(str(row.get("source", ""))),
                        row.get("sha256"),
                    )
                    for index, row in enumerate(wheelhouse)
                    if isinstance(row, Mapping)
                ),
            ):
                if (
                    not candidate.is_absolute()
                    or candidate.is_symlink()
                    or not candidate.is_file()
                    or not isinstance(digest, str)
                    or _sha256_file(candidate) != digest
                ):
                    raise InstallDriftError(f"{label} is missing, unsafe, or hash-mismatched")
                _reject_symlink_ancestors(candidate, label=label, include_leaf=False)
            if len(wheelhouse) != sum(isinstance(row, Mapping) for row in wheelhouse):
                raise InstallPlanError("wheelhouse entries must be typed objects")
            if not any(
                isinstance(row, Mapping) and row.get("sha256") == expected
                for row in wheelhouse
            ):
                raise InstallPlanError("wheelhouse does not contain the candidate wheel")
            slot_ready = _venv_slot_matches(slot, expected)
            if (slot.exists() or slot.is_symlink()) and not slot_ready:
                raise InstallDriftError(f"existing candidate slot is not attestable: {slot}")
            if active.exists() and not active.is_symlink():
                raise InstallDriftError(f"active venv path is not a managed symlink: {active}")
            prior_link: dict[str, object] = {"exists": active.is_symlink()}
            if active.is_symlink():
                prior_link["link_target"] = str(active.readlink())
            if not slot_ready:
                slot.parent.mkdir(parents=True, exist_ok=True)
                _reject_symlink_ancestors(slot, label="candidate venv")
                temporary = _venv_staging_path(slot)
                if temporary.exists() or temporary.is_symlink():
                    raise InstallDriftError(
                        f"candidate venv staging path already exists: {temporary}"
                    )
                if creation_checkpoint is not None:
                    # Publish the exact staging intent before creating the
                    # directory. A crash in the mkdir-to-inode-checkpoint
                    # window is then still recoverable without a pathname
                    # guess.
                    creation_checkpoint(
                        _venv_staging_authority(
                            slot, temporary, state="planned"
                        )
                    )
                temporary_created = False
                try:
                    temporary.mkdir(mode=0o700)
                    temporary_created = True
                    parent_fd = os.open(slot.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
                    if creation_checkpoint is not None:
                        creation_checkpoint(
                            _venv_staging_authority(
                                slot, temporary, state="building"
                            )
                        )
                    _run(("python3", "-m", "venv", str(temporary)), check=True)
                    locked_dir = temporary / ".cortex-wheelhouse"
                    locked_dir.mkdir(mode=0o700)
                    locked_paths: list[tuple[Path, str]] = []
                    seen_names: set[str] = set()
                    for row in wheelhouse:
                        assert isinstance(row, Mapping)
                        source = Path(str(row["source"]))
                        digest = str(row["sha256"])
                        if source.name in seen_names or not source.name.endswith(".whl"):
                            raise InstallPlanError("locked wheelhouse names must be unique wheels")
                        seen_names.add(source.name)
                        copied = locked_dir / source.name
                        _copy_verified_file(source, copied, digest)
                        locked_paths.append((copied, digest))
                    requirements = locked_dir / "requirements.lock"
                    requirements.write_text(
                        "".join(
                            f"{path.as_uri()} --hash=sha256:{digest}\n"
                            for path, digest in locked_paths
                        ),
                        encoding="utf-8",
                    )
                    argv = [
                        str(temporary / "bin/python"),
                        "-m",
                        "pip",
                        "install",
                        "--no-index",
                        "--no-deps",
                        "--require-hashes",
                        "--requirement",
                        str(requirements),
                    ]
                    _run(tuple(argv), check=True)
                    _relocate_venv_shebangs(temporary, slot)
                    _run(
                        (
                            str(temporary / "bin/python"),
                            "-c",
                            "import paulsha_cortex; from paulsha_cortex.cli import main",
                        ),
                        check=True,
                    )
                    (temporary / ".cortex-wheel.sha256").write_text(
                        expected + "\n", encoding="ascii"
                    )
                    os.chmod(temporary, 0o755)
                    (temporary / ".cortex-tree.sha256").write_text(
                        _tree_sha256(temporary) + "\n", encoding="ascii"
                    )
                    slot_tree = _tree_sha256(temporary)
                    if creation_checkpoint is not None:
                        # The complete tree and its inode are durable receipt
                        # authority before the atomic final-name publication.
                        creation_checkpoint(
                            _venv_staging_authority(
                                slot,
                                temporary,
                                state="ready",
                                tree_sha256=slot_tree,
                            )
                        )
                    os.rename(temporary, slot)
                    parent_fd = os.open(slot.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
                except BaseException:
                    if temporary_created:
                        shutil.rmtree(temporary, ignore_errors=True)
                    raise
            slot_state = _venv_state(step)
            slot_tree = slot_state.get("tree_sha256")
            if (
                slot_state.get("exists") is not True
                or slot_state.get("slot_sha256") != expected
                or not isinstance(slot_tree, str)
                or len(slot_tree) != 64
            ):
                raise InstallDriftError("candidate venv slot is not attestable")
            if creation_checkpoint is not None:
                # Persist the exact retained slot before the active-link
                # cutover. A crash after os.replace can then safely restore
                # the prior link without trusting an unbound directory.
                creation_checkpoint(
                    _venv_staging_authority(
                        slot,
                        _venv_staging_path(slot),
                        state="ready",
                        tree_sha256=slot_tree,
                    )
                )
            active.parent.mkdir(parents=True, exist_ok=True)
            temporary_link = active.parent / f".{active.name}.{os.getpid()}.tmp"
            try:
                temporary_link.symlink_to(os.path.relpath(slot, active.parent))
                os.replace(temporary_link, active)
            finally:
                try:
                    temporary_link.unlink()
                except FileNotFoundError:
                    pass
            installed = self.inspect_step(step)
            if installed.get("installed_sha256") != expected:
                raise InstallDriftError("candidate venv cutover did not match the plan")
            return {"prior": prior_link, **installed}
        if kind == "toolchain":
            prior = inspected_prior
            if prior.get("exists"):
                if prior.get("installed_sha256") != step.get("desired_sha256"):
                    raise InstallDriftError(f"existing toolchain binary drifted: {step.get('name')}")
                return {"prior": prior, **prior}
            source = Path(str(step.get("source", "")))
            path = Path(str(step.get("path", "")))
            expected = step.get("desired_sha256")
            source_sha = step.get("source_sha256")
            shape = step.get("shape", "file")
            if (
                not source.is_absolute()
                or not path.is_absolute()
                or not isinstance(expected, str)
                or not isinstance(source_sha, str)
                or source.is_symlink()
                or not source.is_file()
                or source.lstat().st_nlink != 1
                or _sha256_file(source) != source_sha
                or shape not in {"file", "tree"}
            ):
                raise InstallPlanError("toolchain step is not fully hash-bound")
            _reject_symlink_ancestors(source, label="toolchain source", include_leaf=False)
            _reject_symlink_ancestors(path, label="toolchain", include_leaf=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            if shape == "file":
                descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                os.close(descriptor)
                temporary_path = Path(temporary)
                temporary_path.unlink()
                try:
                    _copy_verified_file(source, temporary_path, source_sha)
                    os.chown(temporary_path, _resolve_uid(step.get("owner")), _resolve_gid(step.get("group")))
                    os.chmod(temporary_path, _mode(step.get("mode")))
                    os.rename(temporary_path, path)
                finally:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
            else:
                temporary_path = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
                locked_archive = temporary_path.parent / f".{path.name}.{os.getpid()}.tar"
                try:
                    _copy_verified_file(source, locked_archive, source_sha)
                    _extract_locked_tree(locked_archive, temporary_path)
                    if _tree_sha256(temporary_path) != expected:
                        raise InstallDriftError(f"extracted toolchain tree hash mismatch: {step.get('name')}")
                    _chown_tree(
                        temporary_path,
                        _resolve_uid(step.get("owner")),
                        _resolve_gid(step.get("group")),
                    )
                    os.chmod(temporary_path, _mode(step.get("mode")))
                    os.rename(temporary_path, path)
                finally:
                    shutil.rmtree(temporary_path, ignore_errors=True)
                    try:
                        locked_archive.unlink()
                    except FileNotFoundError:
                        pass
            installed = dict(self.inspect_step(step))
            if installed.get("installed_sha256") != expected:
                raise InstallDriftError(f"installed toolchain binary drifted: {step.get('name')}")
            return {"prior": prior, **installed}
        if kind == "repository":
            prior = inspected_prior
            if prior.get("exists"):
                if prior.get("installed_sha256") != step.get("desired_sha256"):
                    raise InstallDriftError(
                        f"existing repository drifted: {step.get('slug')}"
                        + _observed_drift_detail(prior)
                    )
                return {"prior": prior, **prior}
            source = Path(str(step.get("source", "")))
            path = Path(str(step.get("path", "")))
            source_sha = step.get("source_sha256")
            commit = step.get("commit")
            remote = step.get("remote")
            if (
                not source.is_absolute()
                or not path.is_absolute()
                or not isinstance(source_sha, str)
                or not isinstance(commit, str)
                or not isinstance(remote, str)
                or source.is_symlink()
                or not source.is_file()
                or source.lstat().st_nlink != 1
                or _sha256_file(source) != source_sha
            ):
                raise InstallDriftError("repository source bundle is missing or hash-mismatched")
            _reject_symlink_ancestors(source, label="repository bundle", include_leaf=False)
            _reject_symlink_ancestors(path, label="repository", include_leaf=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            transaction_dir = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
            checkout = transaction_dir / "checkout"
            locked_bundle = transaction_dir / "source.bundle"
            try:
                _copy_verified_file(source, locked_bundle, source_sha)
                _run(
                    (
                        *_REPOSITORY_GIT_PREFIX,
                        "clone",
                        "--no-checkout",
                        str(locked_bundle),
                        str(checkout),
                    ),
                    check=True,
                    env=_REPOSITORY_GIT_ENV,
                )
                _run(
                    (
                        *_REPOSITORY_GIT_PREFIX,
                        "-C",
                        str(checkout),
                        "checkout",
                        "--detach",
                        commit,
                    ),
                    check=True,
                    env=_REPOSITORY_GIT_ENV,
                )
                _run(
                    (
                        *_REPOSITORY_GIT_PREFIX,
                        "-C",
                        str(checkout),
                        "remote",
                        "set-url",
                        "origin",
                        remote,
                    ),
                    check=True,
                    env=_REPOSITORY_GIT_ENV,
                )
                uid = _resolve_uid(step.get("owner"))
                gid = _resolve_gid(step.get("group"))
                _chown_tree(checkout, uid, gid)
                _remove_group_other_write(checkout)
                os.chmod(checkout, _mode(step.get("mode")))
                os.rename(checkout, path)
            finally:
                shutil.rmtree(transaction_dir, ignore_errors=True)
            installed = dict(self.inspect_step(step))
            if installed.get("installed_sha256") != step.get("desired_sha256"):
                raise InstallDriftError(f"installed repository drifted: {step.get('slug')}")
            return {"prior": prior, **installed}
        if kind == "systemctl":
            action = step.get("action")
            unit = step.get("unit")
            if action not in {"enable", "disable", "daemon-reload"}:
                raise InstallPlanError(f"unsupported systemctl action: {action}")
            argv = ["systemctl", str(action)]
            if action != "daemon-reload":
                if not isinstance(unit, str) or not unit:
                    raise InstallPlanError("systemctl step requires a unit")
                argv.append(unit)
            prior = inspected_prior
            _run(argv, check=True)
            return {"prior": prior, **self.inspect_step(step)}
        if kind != "asset":
            raise InstallPlanError(f"unsupported step kind: {kind}")
        path = Path(str(step.get("path")))
        if not path.is_absolute() or ".." in path.parts:
            raise UnsafeInstallPathError(f"unsafe asset path: {path}")
        _reject_symlink_ancestors(path, label="asset", include_leaf=False)
        prior = inspected_prior
        if prior.get("exists"):
            if not _state_matches_step(step, prior):
                raise InstallDriftError(
                    f"existing asset does not match desired state: {step.get('step_id')}"
                )
            return {"prior": prior, **prior}
        asset_type = step.get("asset_type", "file")
        if asset_type == "symlink":
            target = Path(str(step.get("target", "")))
            if not target.is_absolute() or ".." in target.parts:
                raise UnsafeInstallPathError(f"unsafe symlink target: {target}")
            if not target.is_dir() or target.is_symlink():
                raise InstallDriftError(f"symlink target is not an exact directory: {target}")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
            try:
                temporary.symlink_to(os.path.relpath(target, path.parent))
                os.lchown(temporary, _resolve_uid(step.get("owner")), _resolve_gid(step.get("group")))
                os.rename(temporary, path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            installed = dict(self.inspect_step(step))
            if installed.get("installed_sha256") != step.get("desired_sha256"):
                raise InstallDriftError(f"installed symlink does not match desired state: {step.get('step_id')}")
            return {"prior": prior, **installed}
        if asset_type not in {"file", "directory"}:
            raise InstallPlanError(f"unsupported asset_type: {asset_type}")
        # New files and directories can inherit named/default ACLs from their
        # parent. Clear that inherited state before applying the plan's exact
        # ACL set. This is safe only because ``prior`` proved the leaf absent.
        if asset_type == "file" and not isinstance(step.get("content"), str):
            raise InstallPlanError(f"file step lacks content: {step.get('step_id')}")
        created_fd: int | None = None
        parent_authority: list[tuple[int, int]] = []
        authority: dict[str, object] | None = None
        try:
            parent_fd, leaf = _open_parent_directory(
                path, create=True, authority=parent_authority
            )
            try:
                try:
                    if asset_type == "directory":
                        os.mkdir(leaf, 0o700, dir_fd=parent_fd)
                        flags = (
                            os.O_RDONLY
                            | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_NOFOLLOW", 0)
                            | getattr(os, "O_CLOEXEC", 0)
                        )
                    else:
                        flags = (
                            os.O_RDWR
                            | os.O_CREAT
                            | os.O_EXCL
                            | getattr(os, "O_NOFOLLOW", 0)
                            | getattr(os, "O_CLOEXEC", 0)
                        )
                    created_fd = os.open(leaf, flags, 0o600, dir_fd=parent_fd)
                except FileExistsError as exc:
                    raise InstallDriftError(
                        f"asset appeared before exclusive creation: {step.get('step_id')}"
                    ) from exc
            finally:
                os.close(parent_fd)
            authority = _creation_authority(
                os.fstat(created_fd), file_type=str(asset_type)
            )
            if creation_checkpoint is not None:
                creation_checkpoint(authority)
            if asset_type == "file":
                with os.fdopen(os.dup(created_fd), "wb") as stream:
                    stream.write(str(step["content"]).encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
            if not Path("/proc/self/fd").is_dir():
                raise InstallError("/proc/self/fd is required for safe ACL apply")
            acl_target = f"/proc/self/fd/{created_fd}"
            inherited_fds = (created_fd,)
            _run(
                ("setfacl", "-b", acl_target),
                check=True,
                pass_fds=inherited_fds,
            )
            if asset_type == "directory":
                _run(
                    ("setfacl", "-k", acl_target),
                    check=True,
                    pass_fds=inherited_fds,
                )
            uid = _resolve_uid(step.get("owner"))
            gid = _resolve_gid(step.get("group"))
            desired_mode = _mode(step.get("mode"))
            os.fchown(created_fd, uid, gid)
            os.fchmod(created_fd, desired_mode)
            for acl in step.get("acls", []):
                if not isinstance(acl, Mapping):
                    raise InstallPlanError("ACL entries must be objects")
                account = acl.get("account")
                perms = acl.get("perms")
                if not isinstance(account, str) or not isinstance(perms, str):
                    raise InstallPlanError("ACL entries require account and perms")
                prefix = "d:u" if acl.get("default") else "u"
                effective_perms = perms.replace("X", "x")
                _run(
                    ("setfacl", "-m", f"{prefix}:{account}:{effective_perms}", acl_target),
                    check=True,
                    pass_fds=inherited_fds,
                )
            _assert_fd_path_binding(
                path,
                created_fd,
                directory=asset_type == "directory",
                parent_authority=parent_authority,
            )
        finally:
            if created_fd is not None:
                os.close(created_fd)
        installed = dict(self.inspect_step(step))
        if installed.get("installed_sha256") != step.get("desired_sha256"):
            raise InstallDriftError(f"installed asset does not match desired state: {step.get('step_id')}")
        return {"prior": prior, "creation_authority": authority, **installed}

    def rollback_step(self, entry: Mapping[str, object]) -> None:
        step = entry.get("step")
        prior = entry.get("prior", {})
        if not isinstance(step, Mapping) or not isinstance(prior, Mapping):
            raise InstallError("invalid rollback journal entry")
        if step.get("kind") == "legacy-quarantine":
            _legacy_quarantine_restore(entry)
            return
        if step.get("kind") == "account":
            # Service accounts are intentionally retained. Deleting an account
            # safely requires a host-wide owned-file/process proof that the
            # install receipt cannot provide.
            return
        if step.get("kind") == "toolchain":
            path = Path(str(step.get("path", "")))
            if not prior.get("exists") and not path.is_symlink():
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    if entry.get("status") == "prepared" and step.get("shape") == "tree":
                        _rollback_prepared_toolchain(step, path)
                    else:
                        shutil.rmtree(path)
            return
        if step.get("kind") == "repository":
            # Fresh checkouts are durable retained state. An in-place upgrade,
            # however, has an exact prior commit in its journal and must restore
            # that checkout before the old services may be restarted.
            if prior.get("exists"):
                commit = prior.get("commit")
                remote = prior.get("remote")
                if (
                    not isinstance(commit, str)
                    or len(commit) != 40
                    or not isinstance(remote, str)
                ):
                    raise InstallError(
                        "repository rollback lacks exact prior identity"
                    )
                path = Path(str(step.get("path", "")))
                uid = _resolve_uid(prior.get("owner"))
                gid = _resolve_gid(prior.get("group"))
                prefix = (
                    *_REPOSITORY_GIT_PREFIX,
                    *_REPOSITORY_MUTATION_CONFIG,
                    "-c",
                    f"safe.directory={path}",
                    "-C",
                    str(path),
                )
                # Same boundary as the replacement: only the main worktree's
                # checkout and the installer's own remote URL are restored;
                # the Manager's branches, refs, and linked worktrees are not
                # touched (#1124).
                _refuse_runtime_root_checkout(prefix, commit, uid=uid, gid=gid)
                _refuse_ignored_content_overwrite(prefix, commit, uid=uid, gid=gid)
                _run(
                    (*prefix, "checkout", "--detach", "--force", commit),
                    check=True,
                    env=_REPOSITORY_GIT_ENV,
                    uid=uid,
                    gid=gid,
                )
                _run(
                    (*prefix, "remote", "set-url", "origin", remote),
                    check=True,
                    env=_REPOSITORY_GIT_ENV,
                    uid=uid,
                    gid=gid,
                )
                _remove_group_other_write(path, skip=_runtime_worktree_paths(path))
                os.chmod(path, _mode(prior.get("mode")))
                prior_runtime = prior.get("runtime")
                if isinstance(prior_runtime, Mapping):
                    restored = _repository_state(
                        {
                            **step,
                            "owner": prior.get("owner"),
                            "group": prior.get("group"),
                            "mode": prior.get("mode"),
                            "commit": commit,
                            "remote": remote,
                        }
                    )
                    if restored.get("runtime") != prior_runtime:
                        raise InstallDriftError(
                            "repository rollback changed Manager runtime state: "
                            f"{step.get('slug')}"
                        )
            return
        if step.get("kind") == "venv":
            _cleanup_bound_venv_staging(
                step, entry.get("venv_slot_authority")
            )
            active = Path(str(step.get("active_link", "")))
            prior = entry.get("prior", {})
            if not isinstance(prior, Mapping):
                raise InstallError("venv rollback entry lacks prior state")
            current_activation = _venv_activation_state(step)
            if current_activation == dict(prior):
                return
            if not active.is_symlink():
                raise InstallDriftError(f"active venv link drifted: {active}")
            if prior.get("exists"):
                target = prior.get("link_target")
                if not isinstance(target, str) or not target:
                    raise InstallError("venv rollback entry lacks prior link target")
                temporary_link = active.parent / f".{active.name}.{os.getpid()}.rollback"
                temporary_link.symlink_to(target)
                os.replace(temporary_link, active)
            else:
                active.unlink()
            # Published content-addressed slots are retained as the
            # previous-version and forensic boundary. Only an unpublished,
            # receipt-bound staging directory above is removed.
            return
        if step.get("kind") != "asset":
            if step.get("kind") == "systemctl":
                unit = step.get("unit")
                prior = entry.get("prior", {})
                if isinstance(unit, str) and not (
                    isinstance(prior, Mapping) and prior.get("exists")
                ):
                    _run(("systemctl", "disable", unit), check=True)
                return
            raise InstallPlanError(f"unsupported rollback kind: {step.get('kind')}")
        path = Path(str(step.get("path")))
        if (
            entry.get("status") == "prepared"
            and not prior.get("exists")
            and step.get("asset_type", "file") in {"file", "directory"}
        ):
            installed = self.inspect_step(step)
            authority = entry.get("creation_authority")
            has_authority = bool(
                isinstance(authority, Mapping)
                and self.creation_authority_matches(step, authority)
            )
            if not _state_matches_step(step, installed) and not has_authority:
                raise InstallDriftError(
                    "prepared asset lacks matching creation authority: "
                    f"{step.get('step_id')}"
                )
        if step.get("asset_type") == "symlink":
            if prior.get("exists"):
                target = prior.get("target")
                if not isinstance(target, str) or not target or "\x00" in target:
                    raise InstallError("symlink rollback lacks exact prior target")
                parent_fd, leaf = _open_parent_directory(path)
                temporary_name = f".{leaf}.{uuid.uuid4().hex}.rollback"
                try:
                    os.symlink(target, temporary_name, dir_fd=parent_fd)
                    os.chown(
                        temporary_name,
                        _resolve_uid(prior.get("owner")),
                        _resolve_gid(prior.get("group")),
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    os.replace(
                        temporary_name,
                        leaf,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    temporary_name = ""
                    os.fsync(parent_fd)
                finally:
                    if temporary_name:
                        try:
                            os.unlink(temporary_name, dir_fd=parent_fd)
                        except FileNotFoundError:
                            pass
                    os.close(parent_fd)
            elif path.is_symlink():
                path.unlink()
            return
        if not prior.get("exists"):
            if path.is_file() and not path.is_symlink():
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
            return
        if step.get("asset_type", "file") == "directory":
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                _apply_fd_asset_state(
                    descriptor,
                    owner=prior.get("owner"),
                    group=prior.get("group"),
                    mode=prior.get("mode"),
                    acls=prior.get("acl", []),
                    directory=True,
                )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return
        encoded = prior.get("content_base64")
        if not isinstance(encoded, str):
            raise InstallError("file rollback lacks prior content")
        try:
            payload = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as exc:
            raise InstallError("file rollback prior content is invalid") from exc
        parent_fd, leaf = _open_parent_directory(path)
        temporary_name = f".{leaf}.{uuid.uuid4().hex}.rollback"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary_name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
            with os.fdopen(os.dup(descriptor), "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _apply_fd_asset_state(
                descriptor,
                owner=prior.get("owner"),
                group=prior.get("group"),
                mode=prior.get("mode"),
                acls=prior.get("acl", []),
                directory=False,
            )
            os.replace(
                temporary_name,
                leaf,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_name = ""
            os.fsync(parent_fd)
            _assert_fd_path_binding(path, descriptor, directory=False)
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)

    def list_unknown_state(self, receipt: InstallReceipt) -> Sequence[str]:
        retained: list[str] = []
        document = receipt.to_dict()
        journal = document.get("journal", [])
        archived = document.get("rollback_journal", [])
        # Core checkpoints removal of each live journal entry.  The archive is
        # the full rollback-bound inventory needed to identify unknown children
        # after those entries have already been restored and removed.
        journal_rows = (
            archived
            if isinstance(archived, list) and archived
            else journal if isinstance(journal, list) else []
        )
        managed_paths: set[Path] = set()
        for entry in journal_rows:
            step = entry.get("step") if isinstance(entry, Mapping) else None
            if (
                isinstance(step, Mapping)
                and step.get("kind") in {"asset", "repository", "toolchain", "venv"}
                and isinstance(step.get("path"), str)
            ):
                managed_paths.add(Path(str(step["path"])))
        # A legacy object rollback moved back out of quarantine is managed
        # state of its own entry: a directory step that was created in its
        # place (or below it) must not report the restored legacy content as
        # unknown -- but only while that path is the quarantined inode.
        quarantine_sources: dict[Path, tuple[object, object]] = {}
        for entry in journal_rows:
            step = entry.get("step") if isinstance(entry, Mapping) else None
            expected = step.get("expected") if isinstance(step, Mapping) else None
            if (
                isinstance(step, Mapping)
                and step.get("kind") == "legacy-quarantine"
                and isinstance(step.get("path"), str)
                and isinstance(expected, Mapping)
            ):
                quarantine_sources[Path(str(step["path"]))] = (
                    expected.get("dev"),
                    expected.get("ino"),
                )

        def is_restored_quarantine_source(path: Path) -> bool:
            for source, identity in quarantine_sources.items():
                if path != source and source not in path.parents:
                    continue
                try:
                    observed = source.lstat()
                except OSError:
                    return False
                return (observed.st_dev, observed.st_ino) == identity
            return False

        def is_managed_inventory_path(candidate: Path, container: Path) -> bool:
            return any(
                managed != container
                and (
                    candidate == managed
                    or managed in candidate.parents
                    or candidate in managed.parents
                )
                for managed in managed_paths
            )

        for entry in journal_rows:
            if not isinstance(entry, Mapping):
                continue
            step = entry.get("step")
            prior = entry.get("prior")
            if (
                isinstance(step, Mapping)
                and step.get("kind") == "asset"
                and step.get("asset_type") == "directory"
                and isinstance(prior, Mapping)
            ):
                path = Path(str(step.get("path", "")))
                try:
                    if not prior.get("exists"):
                        if is_restored_quarantine_source(path):
                            continue
                        if path.is_symlink() or (path.exists() and not path.is_dir()):
                            retained.append(str(path))
                        elif path.is_dir():
                            for relative in _directory_inventory(path):
                                candidate = path / relative
                                if is_managed_inventory_path(candidate, path):
                                    continue
                                retained.append(str(candidate))
                        continue
                    baseline = prior.get("children")
                    if isinstance(baseline, list) and all(
                        isinstance(row, str) for row in baseline
                    ):
                        # Legacy receipt with the inventory embedded inline.
                        baseline_rows = set(baseline)
                        current = set(_directory_inventory(path))
                    elif "children_sha256" in prior or "children_count" in prior:
                        current_rows = tuple(_directory_inventory(path))
                        if len(current_rows) == prior.get(
                            "children_count"
                        ) and _directory_inventory_sha256(current_rows) == prior.get(
                            "children_sha256"
                        ):
                            # Byte-identical to the receipt-bound baseline.
                            continue
                        try:
                            baseline_rows = set(
                                receipt.directory_inventory(
                                    prior.get("children_sha256"),
                                    prior.get("children_count"),
                                )
                            )
                        except InstallError:
                            # Fail closed: without a trusted baseline nothing
                            # below this directory can be classified, so the
                            # whole directory is retained and rollback blocks.
                            retained.append(str(path))
                            continue
                        current = set(current_rows)
                    else:
                        if path.is_dir() and any(path.iterdir()):
                            retained.append(str(path))
                        continue
                    for relative in sorted(current - baseline_rows):
                        candidate = path / relative
                        if is_managed_inventory_path(candidate, path):
                            continue
                        retained.append(str(candidate))
                except (InstallError, OSError):
                    retained.append(str(path))
            elif (
                isinstance(step, Mapping)
                and step.get("kind") == "toolchain"
                and step.get("shape") == "tree"
                and isinstance(prior, Mapping)
                and prior.get("exists") is False
            ):
                path = Path(str(step.get("path", "")))
                try:
                    source = Path(str(step.get("source", "")))
                    source_sha = step.get("source_sha256")
                    if (
                        not source.is_absolute()
                        or source.is_symlink()
                        or not source.is_file()
                        or source.lstat().st_nlink != 1
                        or not isinstance(source_sha, str)
                        or _sha256_file(source) != source_sha
                    ):
                        raise InstallDriftError("toolchain source cannot be attested")
                    manifest = _locked_tree_manifest(source)
                    owned = set(manifest)
                    current = set(_directory_inventory(path)) if path.is_dir() else set()
                    retained.extend(str(path / relative) for relative in sorted(current - owned))
                    uid = _resolve_uid(step.get("owner"))
                    gid = _resolve_gid(step.get("group"))
                    flags = (
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    root_fd = os.open(path, flags)
                    try:
                        for relative in sorted(current & owned):
                            parts = Path(relative).parts
                            candidate = path / relative
                            try:
                                parent_fd = _open_relative_directory(
                                    root_fd, parts[:-1]
                                )
                            except OSError:
                                retained.append(str(candidate))
                                continue
                            try:
                                if not _toolchain_member_matches_at(
                                    parent_fd,
                                    parts[-1],
                                    manifest[relative],
                                    uid=uid,
                                    gid=gid,
                                ):
                                    retained.append(str(candidate))
                            finally:
                                os.close(parent_fd)
                    finally:
                        os.close(root_fd)
                except (InstallError, OSError, tarfile.TarError):
                    if path.exists() or path.is_symlink():
                        retained.append(str(path))
        return tuple(sorted(set(retained)))

    def validate_credentials(self, receipt: InstallReceipt) -> Sequence[str]:
        failures: list[str] = []
        rows = receipt.to_dict().get("credentials", [])
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, Mapping):
                failures.append("invalid credential metadata")
                continue
            principal = str(row.get("principal", ""))
            provider = str(row.get("provider", ""))
            try:
                destination, uid, gid = credential_destination(
                    receipt, principal=principal, provider=provider
                )
                parent_fd, leaf = _open_parent_directory(destination)
                descriptor: int | None = None
                try:
                    descriptor = os.open(
                        leaf,
                        os.O_RDONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=parent_fd,
                    )
                    observed = os.fstat(descriptor)
                    digest = hashlib.sha256(_read_fd_bytes(descriptor)).hexdigest()
                    _assert_fd_path_binding(destination, descriptor, directory=False)
                    if (
                        not stat.S_ISREG(observed.st_mode)
                        or observed.st_nlink != 1
                        or observed.st_uid != uid
                        or observed.st_gid != gid
                        or stat.S_IMODE(observed.st_mode) != 0o600
                        or digest != row.get("sha256")
                    ):
                        failures.append(
                            f"{principal}/{provider} metadata or hash mismatch"
                        )
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                    os.close(parent_fd)
            except (InstallError, OSError):
                failures.append(f"{principal}/{provider} unavailable")
        return tuple(failures)

    def rollback_credentials(self, receipt: InstallReceipt) -> Sequence[dict[str, object]]:
        retained: list[dict[str, object]] = []
        document = receipt.to_dict()
        completed = document.get("credentials", [])
        prepared = document.get("credential_journal", [])
        rows = [
            *(completed if isinstance(completed, list) else []),
            *(prepared if isinstance(prepared, list) else []),
        ]
        seen: set[tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            principal = str(row.get("principal", ""))
            provider = str(row.get("provider", ""))
            identity = (principal, provider)
            if identity in seen:
                retained.append(
                    {
                        "credential": f"{principal}/{provider}",
                        "reason": "duplicate credential receipt authority",
                    }
                )
                continue
            seen.add(identity)
            try:
                destination, uid, gid = credential_destination(
                    receipt, principal=principal, provider=provider
                )
                parent_fd, leaf = _open_parent_directory(destination)
            except FileNotFoundError:
                continue
            except (InstallError, OSError):
                retained.append(
                    {
                        "credential": f"{principal}/{provider}",
                        "reason": "credential rollback inspection failed",
                    }
                )
                continue
            try:
                descriptor: int | None = None
                try:
                    descriptor = os.open(
                        leaf,
                        os.O_RDONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=parent_fd,
                    )
                except FileNotFoundError:
                    pass
                if descriptor is not None:
                    observed = os.fstat(descriptor)
                    digest = hashlib.sha256(_read_fd_bytes(descriptor)).hexdigest()
                    _assert_fd_path_binding(destination, descriptor, directory=False)
                    if (
                        stat.S_ISREG(observed.st_mode)
                        and observed.st_nlink == 1
                        and observed.st_uid == uid
                        and observed.st_gid == gid
                        and stat.S_IMODE(observed.st_mode) == 0o600
                        and digest == row.get("sha256")
                    ):
                        os.unlink(leaf, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    else:
                        retained.append(
                            {
                                "credential": f"{principal}/{provider}",
                                "reason": "credential drifted after import",
                            }
                        )
                    os.close(descriptor)
                    descriptor = None
                temp_name = row.get("temp_name")
                if temp_name is not None:
                    if (
                        not isinstance(temp_name, str)
                        or Path(temp_name).name != temp_name
                        or not temp_name.startswith(".")
                        or temp_name in {".", ".."}
                    ):
                        retained.append(
                            {
                                "credential": f"{principal}/{provider}",
                                "reason": "credential fallback journal is invalid",
                            }
                        )
                        continue
                    try:
                        temp_observed = os.stat(
                            temp_name, dir_fd=parent_fd, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        if (
                            stat.S_ISREG(temp_observed.st_mode)
                            and temp_observed.st_nlink == 1
                            and stat.S_IMODE(temp_observed.st_mode) == 0
                        ):
                            os.unlink(temp_name, dir_fd=parent_fd)
                            os.fsync(parent_fd)
                            continue
                        temp_fd = os.open(
                            temp_name,
                            os.O_RDONLY
                            | getattr(os, "O_NOFOLLOW", 0)
                            | getattr(os, "O_CLOEXEC", 0),
                            dir_fd=parent_fd,
                        )
                        try:
                            observed = os.fstat(temp_fd)
                            digest = hashlib.sha256(
                                _read_fd_bytes(temp_fd)
                            ).hexdigest()
                            removable = (
                                stat.S_ISREG(observed.st_mode)
                                and observed.st_nlink == 1
                                and observed.st_uid == uid
                                and observed.st_gid == gid
                                and stat.S_IMODE(observed.st_mode) == 0o600
                                and digest == row.get("sha256")
                            )
                        finally:
                            os.close(temp_fd)
                        if removable:
                            os.unlink(temp_name, dir_fd=parent_fd)
                            os.fsync(parent_fd)
                        else:
                            retained.append(
                                {
                                    "credential": f"{principal}/{provider}",
                                    "reason": "credential fallback temp drifted",
                                }
                            )
            except (InstallError, OSError):
                retained.append(
                    {
                        "credential": f"{principal}/{provider}",
                        "reason": "credential rollback inspection failed",
                    }
                )
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                os.close(parent_fd)
        # A legacy adoption receipt also records the credential state
        # directories an import created (``created_directories``): they stand
        # where quarantined legacy directories must move back, so they go too
        # -- but only while empty and still the recorded inode.
        retained_keys = {str(row.get("credential")) for row in retained}
        plan = document.get("plan")
        block = plan.get("legacy_adoption") if isinstance(plan, Mapping) else None
        quarantine_root = block.get("quarantine_root") if isinstance(block, Mapping) else None
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("created_directories"):
                continue
            principal = str(row.get("principal", ""))
            provider = str(row.get("provider", ""))
            key = f"{principal}/{provider}"
            if key in retained_keys:
                continue
            try:
                destination, _uid, _gid = credential_destination(
                    receipt, principal=principal, provider=provider
                )
                problem = _remove_created_credential_directories(
                    list(row["created_directories"]),  # type: ignore[arg-type]
                    destination,
                    quarantine_root=(
                        Path(quarantine_root) if isinstance(quarantine_root, str) else None
                    ),
                    key=f"credential:{key}",
                )
            except (InstallError, OSError) as exc:
                problem = f"credential directory rollback failed: {exc}"
            if problem is not None:
                retained.append({"credential": key, "reason": problem})
        return tuple(retained)

    def start_service(self, name: str) -> None:
        _run(("systemctl", "start", name), check=True)

    def stop_service(self, name: str) -> None:
        _run(("systemctl", "stop", name), check=True)

    def reload_systemd_units(self) -> None:
        """Make systemd re-read unit files that rollback restored or removed."""

        _run(("systemctl", "daemon-reload"), check=True)

    def installed_inventory(self, plan: Mapping[str, object]) -> dict[str, dict[str, dict[str, str]]]:
        generated = plan.get("generated", {})
        installed: dict[str, dict[str, dict[str, str]]] = {}
        if not isinstance(generated, Mapping):
            return installed
        for category, rows in generated.items():
            installed[str(category)] = {}
            if not isinstance(rows, Mapping):
                continue
            for name, expected in rows.items():
                if not isinstance(expected, Mapping):
                    continue
                path = Path(str(expected.get("path", "")))
                # Attestation reads the content itself below; the snapshot only
                # supplies ownership metadata, so it must not embed the bytes.
                snapshot = _snapshot(path, capture_content=False)
                content = ""
                if snapshot.get("exists") and path.is_file():
                    # Path.read_text() enables universal-newline translation and
                    # would turn an unexecutable CRLF shebang into an apparently
                    # valid LF shebang before attestation sees it.
                    content = path.read_bytes().decode("utf-8", errors="replace")
                installed[str(category)][str(name)] = {
                    "content": content,
                    "owner": str(snapshot.get("owner", "")),
                    "group": str(snapshot.get("group", "")),
                    "mode": str(snapshot.get("mode", "")),
                }
        # Enumerate unexpected Cortex authority files as well as desired paths;
        # otherwise the attestor could only prove presence, never exclusivity.
        for category in (
            "units",
            "polkit",
            "shim",
            "toolchain_wrappers",
            "environment",
        ):
            expected_rows = generated.get(category, {})
            if not isinstance(expected_rows, Mapping) or not expected_rows:
                continue
            parents = {
                Path(str(row.get("path", ""))).parent
                for row in expected_rows.values()
                if isinstance(row, Mapping)
            }
            for parent in parents:
                try:
                    candidates = list(parent.iterdir())
                except OSError:
                    continue
                for path in candidates:
                    if path.name in expected_rows:
                        continue
                    authority_bearing = (
                        category == "toolchain_wrappers"
                        or (category == "environment" and path.suffix == ".env")
                        or path.name.startswith("cortex")
                        or (category == "polkit" and "cortex" in path.name)
                    )
                    if not authority_bearing:
                        continue
                    content = ""
                    if path.is_file() and not path.is_symlink():
                        content = path.read_bytes().decode("utf-8", errors="replace")
                    installed.setdefault(category, {})[path.name] = {
                        "content": content,
                        "owner": "",
                        "group": "",
                        "mode": "",
                    }
        return installed

    def service_identities(self) -> dict[str, dict[str, str]]:
        identities: dict[str, dict[str, str]] = {}
        for name in (
            "cortex-egress-proxy.service",
            "cortex-manager.service",
            "cortex-monitor.service",
        ):
            result = _run(
                (
                    "systemctl",
                    "show",
                    name,
                    "--property=User",
                    "--property=ExecStart",
                    "--property=ActiveState",
                    "--no-pager",
                )
            )
            values: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    values[key] = value
            exec_value = values.get("ExecStart", "")
            match = re.search(r"(?:path=|argv\[\]=)(/[^ ;]+)", exec_value)
            exec_path = match.group(1) if match else ""
            executable = Path(exec_path)
            try:
                executable_state = executable.resolve(strict=True).lstat()
            except OSError:
                executable_state = None
            identities[name] = {
                "user": values.get("User", ""),
                "exec_path": exec_path,
                "active_state": values.get("ActiveState", ""),
                "exec_sha256": (
                    _sha256_file(executable.resolve(strict=True))
                    if exec_path
                    and executable_state is not None
                    and stat.S_ISREG(executable_state.st_mode)
                    and executable_state.st_nlink == 1
                    and not stat.S_IMODE(executable_state.st_mode) & 0o022
                    else ""
                ),
            }
        return identities


SystemInstallBackend = LocalInstallBackend
