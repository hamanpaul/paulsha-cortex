"""Root-gated local backend for the trust-root transaction engine."""
from __future__ import annotations

import base64
import configparser
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .core import (
    InstallDriftError,
    InstallError,
    InstallPlanError,
    InstallReceipt,
    UnsafeInstallPathError,
    _account_digest,
    _assert_fd_path_binding,
    _desired_digest,
    _open_directory_chain,
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
            "path": str(slot),
            "tree_sha256": tree_sha256,
        }
    resolved_target = (active.parent / link_target).resolve(strict=False)
    if not slot_matches or resolved_target != slot.resolve(strict=False):
        return {
            "exists": True,
            "installed_sha256": None,
            "path": str(slot),
            "tree_sha256": tree_sha256,
        }
    return {
        "exists": True,
        "installed_sha256": expected,
        "path": str(slot),
        "tree_sha256": tree_sha256,
        "link_target": str(link_target),
    }


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


def _read_acl(path: Path) -> list[dict[str, object]]:
    if shutil.which("getfacl") is None:
        raise InstallDriftError("getfacl is unavailable; ACL state is untrusted")
    result = _run(("getfacl", "-cp", str(path)))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise InstallDriftError(
            f"getfacl failed while inspecting ACL for {path} ({result.returncode}): {detail}"
        )
    rows: list[dict[str, object]] = []
    for raw in result.stdout.splitlines():
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
                for candidate in result.stdout.splitlines()
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


def _snapshot(path: Path) -> dict[str, object]:
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
        snapshot["children"] = _directory_inventory(path)
        try:
            snapshot["is_mountpoint"] = path.is_mount()
        except OSError as exc:
            raise InstallDriftError(
                f"cannot determine managed directory mount status {path}: {exc}"
            ) from exc
    if stat.S_ISREG(observed.st_mode):
        content = path.read_bytes()
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


def _repository_config_is_canonical(
    path: Path, *, remote: object, uid: int, gid: int
) -> bool:
    """Accept only the detached clone config emitted by the installer."""

    git_dir = path / ".git"
    config_path = git_dir / "config"
    try:
        git_state = git_dir.lstat()
        config_state = config_path.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISDIR(git_state.st_mode)
        or stat.S_ISLNK(git_state.st_mode)
        or git_state.st_uid != uid
        or git_state.st_gid != gid
        or not stat.S_ISREG(config_state.st_mode)
        or stat.S_ISLNK(config_state.st_mode)
        or config_state.st_nlink != 1
        or config_state.st_uid != uid
        or config_state.st_gid != gid
        or not isinstance(remote, str)
    ):
        return False
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
        return False
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
    return parser.sections() == list(expected) and all(
        dict(parser.items(section, raw=True)) == values
        for section, values in expected.items()
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
    path = Path(str(step.get("path", "")))
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        return {"exists": True, "installed_sha256": None}
    expected_uid = _resolve_uid(step.get("owner"))
    expected_gid = _resolve_gid(step.get("group"))
    tree_safe = True
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        for name in (".", *directories, *files):
            candidate = Path(root) if name == "." else Path(root) / name
            try:
                candidate_state = candidate.lstat()
            except OSError:
                tree_safe = False
                break
            if (
                candidate_state.st_uid != expected_uid
                or candidate_state.st_gid != expected_gid
                or (
                    not stat.S_ISLNK(candidate_state.st_mode)
                    and stat.S_IMODE(candidate_state.st_mode) & 0o002
                )
            ):
                tree_safe = False
                break
            if stat.S_ISLNK(candidate_state.st_mode):
                target = Path(os.readlink(candidate))
                resolved = (candidate.parent / target).resolve(strict=False)
                try:
                    resolved.relative_to(path.resolve(strict=True))
                except ValueError:
                    tree_safe = False
                    break
        if not tree_safe:
            break
    config_safe = tree_safe and _repository_config_is_canonical(
        path,
        remote=step.get("remote"),
        uid=expected_uid,
        gid=expected_gid,
    )
    # Inspect only a canonical tree, as its owning account, and without any
    # host/user config.  Command-scope overrides are defense in depth against
    # executable fsmonitor/hooks settings and optional index writes.
    prefix = (
        *_REPOSITORY_GIT_PREFIX,
        "-c",
        f"safe.directory={path}",
        "-C",
        str(path),
    )
    commands: list[subprocess.CompletedProcess[str]] = []
    if config_safe:
        for suffix in (
            ("rev-parse", "HEAD"),
            ("remote", "get-url", "origin"),
            ("status", "--porcelain=v1", "--untracked-files=all"),
            ("fsck", "--strict", "--no-dangling"),
        ):
            commands.append(
                _run(
                    (*prefix, *suffix),
                    env=_REPOSITORY_GIT_ENV,
                    uid=expected_uid,
                    gid=expected_gid,
                )
            )
    if commands:
        head, remote, clean, integrity = commands
    else:
        head = remote = clean = integrity = subprocess.CompletedProcess(
            list(prefix), 1, "", "repository config is not canonical"
        )
    matches = (
        head.returncode == 0
        and head.stdout.strip() == step.get("commit")
        and remote.returncode == 0
        and remote.stdout.strip() == step.get("remote")
        and clean.returncode == 0
        and not clean.stdout.strip()
        and integrity.returncode == 0
        and tree_safe
        and config_safe
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
        "clean": clean.returncode == 0 and not clean.stdout.strip(),
        "integrity": integrity.returncode == 0,
        "tree_safe": tree_safe,
        "config_safe": config_safe,
        "installed_sha256": step.get("desired_sha256") if matches else None,
    }


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid, follow_symlinks=False)
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        for name in (*directories, *files):
            os.chown(Path(root) / name, uid, gid, follow_symlinks=False)


def _remove_group_other_write(path: Path) -> None:
    """Make a freshly cloned tree non-writable outside its owning account."""

    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
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
        or file_type not in {"file", "directory"}
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


class LocalInstallBackend:
    """Real Linux implementation; construction itself enforces the root boundary."""

    def __init__(self, *, require_root: bool = True) -> None:
        if require_root and os.geteuid() != 0:
            raise PermissionError("trust-root apply/activate/verify/rollback requires root")

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
                mode = path.lstat().st_mode
            except FileNotFoundError:
                paths[str(path)] = {"exists": False, "is_symlink": False}
            else:
                paths[str(path)] = {
                    "exists": True,
                    "is_symlink": stat.S_ISLNK(mode),
                }
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

    def inspect_step(self, step: Mapping[str, object]) -> Mapping[str, object]:
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
        observed = _snapshot(path)
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
        if step.get("kind") != "asset" or step.get(
            "asset_type", "file"
        ) not in {"file", "directory"}:
            return False
        return _path_matches_creation_authority(
            Path(str(step.get("path", ""))), authority
        )

    def apply_step(self, step: Mapping[str, object]) -> Mapping[str, object]:
        return self._apply_step(step, creation_checkpoint=None)

    def apply_step_checkpointed(
        self,
        step: Mapping[str, object],
        creation_checkpoint: Callable[[Mapping[str, object]], None],
    ) -> Mapping[str, object]:
        return self._apply_step(step, creation_checkpoint=creation_checkpoint)

    def _apply_step(
        self,
        step: Mapping[str, object],
        *,
        creation_checkpoint: Callable[[Mapping[str, object]], None] | None,
    ) -> Mapping[str, object]:
        kind = step.get("kind")
        if kind == "account":
            prior = _account_state(step)
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
            prior = self.inspect_step(step)
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
                temporary = Path(tempfile.mkdtemp(prefix=f".{slot.name}.", dir=slot.parent))
                try:
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
                    os.rename(temporary, slot)
                except BaseException:
                    shutil.rmtree(temporary, ignore_errors=True)
                    raise
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
            prior = dict(self.inspect_step(step))
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
            prior = dict(self.inspect_step(step))
            if prior.get("exists"):
                if prior.get("installed_sha256") != step.get("desired_sha256"):
                    raise InstallDriftError(f"existing repository drifted: {step.get('slug')}")
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
            prior = self.inspect_step(step)
            _run(argv, check=True)
            return {"prior": prior, **self.inspect_step(step)}
        if kind != "asset":
            raise InstallPlanError(f"unsupported step kind: {kind}")
        path = Path(str(step.get("path")))
        if not path.is_absolute() or ".." in path.parts:
            raise UnsafeInstallPathError(f"unsafe asset path: {path}")
        _reject_symlink_ancestors(path, label="asset", include_leaf=False)
        prior = dict(self.inspect_step(step))
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
            # Repositories are durable state. Even a receipt-created checkout is
            # retained and may only be adopted again at the exact commit/remote.
            return
        if step.get("kind") == "venv":
            active = Path(str(step.get("active_link", "")))
            if not active.is_symlink():
                raise InstallDriftError(f"active venv link drifted: {active}")
            prior = entry.get("prior", {})
            if not isinstance(prior, Mapping):
                raise InstallError("venv rollback entry lacks prior state")
            if prior.get("exists"):
                target = prior.get("link_target")
                if not isinstance(target, str) or not target:
                    raise InstallError("venv rollback entry lacks prior link target")
                temporary_link = active.parent / f".{active.name}.{os.getpid()}.rollback"
                temporary_link.symlink_to(target)
                os.replace(temporary_link, active)
            else:
                active.unlink()
            # Content-addressed slots are retained as the previous-version and
            # forensic boundary; rollback never recursively removes them.
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
            if not prior.get("exists"):
                if path.is_symlink():
                    path.unlink()
            return
        if not prior.get("exists"):
            if path.is_file() and not path.is_symlink():
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
            return
        encoded = prior.get("content_base64")
        if isinstance(encoded, str):
            path.write_bytes(base64.b64decode(encoded.encode("ascii")))
        os.chown(path, _resolve_uid(prior.get("owner")), _resolve_gid(prior.get("group")), follow_symlinks=False)
        os.chmod(path, _mode(prior.get("mode")), follow_symlinks=False)

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
                and step.get("kind") == "asset"
                and isinstance(step.get("path"), str)
            ):
                managed_paths.add(Path(str(step["path"])))
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
                        if path.is_dir() and any(path.iterdir()):
                            retained.append(str(path))
                        continue
                    baseline = prior.get("children")
                    if not isinstance(baseline, list) or not all(
                        isinstance(row, str) for row in baseline
                    ):
                        if path.is_dir() and any(path.iterdir()):
                            retained.append(str(path))
                        continue
                    current = set(_directory_inventory(path))
                    for relative in sorted(current - set(baseline)):
                        candidate = path / relative
                        if any(
                            managed != path
                            and (candidate == managed or managed in candidate.parents)
                            for managed in managed_paths
                        ):
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
        return tuple(retained)

    def start_service(self, name: str) -> None:
        _run(("systemctl", "start", name), check=True)

    def stop_service(self, name: str) -> None:
        _run(("systemctl", "stop", name), check=True)

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
                snapshot = _snapshot(path)
                content = ""
                if snapshot.get("exists") and path.is_file():
                    content = path.read_text(encoding="utf-8", errors="replace")
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
                        content = path.read_text(encoding="utf-8", errors="replace")
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
