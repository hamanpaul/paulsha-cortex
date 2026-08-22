"""Root-gated local backend for the trust-root transaction engine."""
from __future__ import annotations

import base64
import grp
import hashlib
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

from .core import (
    InstallDriftError,
    InstallError,
    InstallPlanError,
    InstallReceipt,
    UnsafeInstallPathError,
    _desired_digest,
    _reject_symlink_ancestors,
)


def _run(argv: Sequence[str], *, check: bool = False, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run one typed argv.  Shell text is never accepted by this backend."""

    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise InstallPlanError(f"invalid argv: {argv!r}")
    result = subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        input=input_text,
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
    if not isinstance(value, str) or not re.fullmatch(r"0[0-7]{3,4}", value):
        raise InstallPlanError(f"invalid mode: {value!r}")
    return int(value, 8)


def _read_acl(path: Path) -> list[dict[str, object]]:
    if shutil.which("getfacl") is None:
        return []
    result = _run(("getfacl", "-cp", str(path)))
    if result.returncode != 0:
        return []
    rows: list[dict[str, object]] = []
    for raw in result.stdout.splitlines():
        default = raw.startswith("default:user:")
        prefix = "default:user:" if default else "user:"
        if not raw.startswith(prefix):
            continue
        body = raw[len(prefix) :]
        account, separator, perms = body.partition(":")
        if not separator or not account:
            continue
        rows.append(
            {
                "account": account,
                "perms": perms.replace("-", ""),
                "default": default,
            }
        )
    return rows


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
    if stat.S_ISREG(observed.st_mode):
        content = path.read_bytes()
        snapshot["content_base64"] = base64.b64encode(content).decode("ascii")
        snapshot["installed_sha256"] = hashlib.sha256(content).hexdigest()
    return snapshot


def _universal_nopasswd() -> bool:
    pattern = re.compile(r"\bALL\s*=\s*\([^)]*\)\s*NOPASSWD\s*:\s*ALL\b")
    candidates = [Path("/etc/sudoers")]
    sudoers_d = Path("/etc/sudoers.d")
    try:
        candidates.extend(
            row for row in sudoers_d.iterdir() if row.is_file() and not row.is_symlink()
        )
    except OSError:
        pass
    for candidate in candidates:
        try:
            if candidate.is_symlink():
                continue
            if pattern.search(candidate.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False


class LocalInstallBackend:
    """Real Linux implementation; construction itself enforces the root boundary."""

    def __init__(self, *, require_root: bool = True) -> None:
        if require_root and os.geteuid() != 0:
            raise PermissionError("trust-root apply/activate/verify/rollback requires root")

    def preflight_facts(self, plan: Mapping[str, object]) -> Mapping[str, object]:
        roots = plan.get("roots", {})
        deploy = Path(str(roots.get("deploy", "/opt/cortex"))) if isinstance(roots, Mapping) else Path("/opt/cortex")
        services: dict[str, str] = {}
        for name in (
            "cortex-egress-proxy.service",
            "cortex-manager.service",
            "cortex-monitor.service",
        ):
            result = _run(("systemctl", "is-active", name)) if shutil.which("systemctl") else None
            services[name] = (
                result.stdout.strip() if result is not None and result.stdout.strip() else "inactive"
            )
        accounts: dict[str, dict[str, object]] = {}
        for row in plan.get("accounts", []):
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
            "in_flight_jobs": 0,
            "services": services,
            "accounts": accounts,
            "paths": paths,
        }

    def inspect_step(self, step: Mapping[str, object]) -> Mapping[str, object]:
        if step.get("kind") == "systemctl":
            name = str(step.get("unit", ""))
            result = _run(("systemctl", "is-enabled", name))
            enabled = result.returncode == 0
            return {
                "exists": enabled,
                "installed_sha256": step.get("desired_sha256") if enabled else None,
            }
        if step.get("kind") != "asset":
            raise InstallPlanError(f"unsupported step kind: {step.get('kind')}")
        path = Path(str(step.get("path")))
        observed = _snapshot(path)
        if not observed.get("exists"):
            return observed
        if step.get("asset_type") == "directory":
            semantic = {
                "path": str(path),
                "owner": observed.get("owner"),
                "group": observed.get("group"),
                "mode": observed.get("mode"),
                "acls": observed.get("acl", []),
                "asset_type": "directory",
            }
            observed["installed_sha256"] = _desired_digest(semantic)
        return observed

    def apply_step(self, step: Mapping[str, object]) -> Mapping[str, object]:
        kind = step.get("kind")
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
            _run(argv, check=True)
            return {"prior": {"exists": False}, **self.inspect_step(step)}
        if kind != "asset":
            raise InstallPlanError(f"unsupported step kind: {kind}")
        path = Path(str(step.get("path")))
        if not path.is_absolute() or ".." in path.parts:
            raise UnsafeInstallPathError(f"unsafe asset path: {path}")
        _reject_symlink_ancestors(path, label="asset")
        prior = _snapshot(path)
        asset_type = step.get("asset_type", "file")
        if asset_type == "directory":
            path.mkdir(parents=True, exist_ok=True)
        elif asset_type == "file":
            content = step.get("content")
            if not isinstance(content, str):
                raise InstallPlanError(f"file step lacks content: {step.get('step_id')}")
            path.parent.mkdir(parents=True, exist_ok=True)
            _reject_symlink_ancestors(path, label="asset")
            descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            temporary_path = Path(temporary)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, path)
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
                raise
        else:
            raise InstallPlanError(f"unsupported asset_type: {asset_type}")
        os.chown(path, _resolve_uid(step.get("owner")), _resolve_gid(step.get("group")), follow_symlinks=False)
        os.chmod(path, _mode(step.get("mode")), follow_symlinks=False)
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
                ("setfacl", "-m", f"{prefix}:{account}:{effective_perms}", str(path)),
                check=True,
            )
        installed = dict(self.inspect_step(step))
        if installed.get("installed_sha256") != step.get("desired_sha256"):
            raise InstallDriftError(f"installed asset does not match desired state: {step.get('step_id')}")
        return {"prior": prior, **installed}

    def rollback_step(self, entry: Mapping[str, object]) -> None:
        step = entry.get("step")
        prior = entry.get("prior", {})
        if not isinstance(step, Mapping) or not isinstance(prior, Mapping):
            raise InstallError("invalid rollback journal entry")
        if step.get("kind") != "asset":
            if step.get("kind") == "systemctl":
                unit = step.get("unit")
                if isinstance(unit, str):
                    _run(("systemctl", "disable", unit), check=True)
                return
            raise InstallPlanError(f"unsupported rollback kind: {step.get('kind')}")
        path = Path(str(step.get("path")))
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
        # Unknown children are never recursively removed; rollback_step already
        # refuses to remove a non-empty directory.  Their names are intentionally
        # not guessed here because a receipt cannot claim ownership of them.
        return ()

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
        return installed

    def service_identities(self) -> dict[str, dict[str, str]]:
        identities: dict[str, dict[str, str]] = {}
        for name in (
            "cortex-egress-proxy.service",
            "cortex-manager.service",
            "cortex-monitor.service",
        ):
            result = _run(
                ("systemctl", "show", name, "--property=User", "--property=ExecStart", "--no-pager")
            )
            values: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    values[key] = value
            exec_value = values.get("ExecStart", "")
            identities[name] = {
                "user": values.get("User", ""),
                "exec_sha256": (
                    hashlib.sha256(exec_value.encode()).hexdigest() if exec_value else ""
                ),
            }
        return identities


SystemInstallBackend = LocalInstallBackend
