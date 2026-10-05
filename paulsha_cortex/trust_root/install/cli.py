"""Command-line surface for privileged trust-root installation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterator, Mapping, Sequence

import yaml

from .backend import LocalInstallBackend
from .legacy import (
    LegacyApplyContext,
    LegacyInventory,
    LocalLegacyHostBackend,
    apply_host_overlay,
    bind_host_overlay,
    check_inventory_output,
    collect_legacy_inventory,
    derive_legacy_adoption,
    host_binding_sha256,
    legacy_adoption_request,
    legacy_scope,
    publish_inventory,
    render_inventory_summary,
    validate_host_overlay,
)
from .core import (
    InstallError,
    InstallPlanError,
    InstallReceipt,
    UnsafeInstallPathError,
    _open_receipt_parent_directory,
    _rename_noreplace_at,
    _write_all,
    activate_receipt,
    apply_plan,
    atomic_write_json,
    bind_bundle_artifacts,
    build_install_plan,
    canonical_receipt_path,
    inherit_prior_credentials,
    import_credential,
    is_inherited_credential,
    new_install_receipt,
    plan_sha256,
    rollback_receipt,
    validate_apply_plan,
    validate_bundle_manifest,
    validate_prior_receipt_handoff,
    verify_receipt,
)
from .legacy_purge import build_legacy_purge_report, purge_legacy_entries


_TRUST_ROOT_LOCK_ROOT = Path("/run/paulsha-cortex-trust-root")
_TRUST_ROOT_MAINTENANCE_ROOT = Path("/var/lib/cortex-installer")
_MAINTENANCE_SERVICES = (
    "cortex-egress-proxy.service",
    "cortex-manager.service",
    "cortex-monitor.service",
)


def _load_mapping(path: Path, *, label: str) -> dict[str, object]:
    if path.is_symlink():
        raise UnsafeInstallPathError(f"{label} must not be a symlink: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InstallPlanError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstallPlanError(f"{label} must contain an object")
    return payload


def _load_plan(path: Path) -> dict[str, object]:
    return _load_mapping(path, label="install plan")


def _require_root() -> None:
    if os.geteuid() != 0:
        raise PermissionError("this trust-root command requires root; rerun it with sudo")


def _receipt_path(plan: Mapping[str, object]) -> Path:
    value = plan.get("receipt_path")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise InstallPlanError("plan does not contain an absolute receipt_path")
    return Path(value)


def _emit(payload: object) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


@contextmanager
def _host_lock_file(*, leaf: str, root: Path | None = None) -> Iterator[int]:
    """Open one validated host-global root-owned trust-root lock file."""

    parent_fd: int | None = None
    lock_fd: int | None = None
    try:
        lock_path = (root or _TRUST_ROOT_LOCK_ROOT) / leaf
        parent_fd, _leaf = _open_receipt_parent_directory(
            lock_path,
            create=True,
        )
        flags = (
            os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        created = False
        try:
            lock_fd = os.open(leaf, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            try:
                lock_fd = os.open(
                    leaf,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_fd,
                )
                created = True
            except FileExistsError:
                lock_fd = os.open(leaf, flags, dir_fd=parent_fd)
        if created:
            os.fchmod(lock_fd, 0o600)
            os.fsync(lock_fd)
            os.fsync(parent_fd)
        observed = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) & 0o077
        ):
            raise UnsafeInstallPathError(
                "trust-root lock is not a private owned regular file"
            )
        yield lock_fd
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if parent_fd is not None:
            os.close(parent_fd)


@contextmanager
def _host_lock(
    *, leaf: str, conflict: str, shared: bool = False
) -> Iterator[int]:
    """Acquire one host-global root-owned trust-root lock."""

    with _host_lock_file(leaf=leaf) as lock_fd:
        try:
            operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(lock_fd, operation | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError(conflict) from exc
        yield lock_fd


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _valid_maintenance_token(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_lock_payload(lock_fd: int, payload: Mapping[str, object] | None) -> None:
    encoded = (
        b""
        if payload is None
        else (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
    )
    os.lseek(lock_fd, 0, os.SEEK_SET)
    os.ftruncate(lock_fd, 0)
    offset = 0
    while offset < len(encoded):
        offset += os.write(lock_fd, encoded[offset:])
    os.fsync(lock_fd)


def _maintenance_lock_payload(
    lock_fd: int, *, allow_absent: bool = False
) -> Mapping[str, object] | None:
    raw = os.pread(lock_fd, 4097, 0)
    if not raw and allow_absent:
        return None
    if not raw or len(raw) > 4096:
        raise InstallError("active maintenance lease metadata is invalid")
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("active maintenance lease metadata is invalid") from exc
    if not isinstance(payload, Mapping) or set(payload) != {
        "plan_sha256",
        "token_sha256",
    }:
        raise InstallError("active maintenance lease metadata is invalid")
    return payload


def _maintenance_payload_authorizes(
    payload: Mapping[str, object],
    plan: Mapping[str, object],
    maintenance_token: str,
) -> bool:
    return bool(
        payload.get("plan_sha256") == plan_sha256(plan)
        and payload.get("token_sha256") == _sha256_text(maintenance_token)
    )


@contextmanager
def _maintenance_admission(
    plan: Mapping[str, object], *, maintenance_token: str | None
) -> Iterator[None]:
    """Block direct mutations while admitting only the active lease holder."""

    if maintenance_token is None:
        with _host_lock_file(leaf="maintenance.lock") as lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallError(
                    "an active trust-root maintenance window requires its exact token"
                ) from exc
            if _maintenance_lock_payload(lock_fd, allow_absent=True) is not None:
                raise InstallError(
                    "a stale trust-root maintenance window requires its exact recovery token"
                )
            if _read_maintenance_snapshot() is not None:
                raise InstallError(
                    "an unfinished maintenance snapshot requires exact-plan recovery"
                )
            yield
        return
    if not _valid_maintenance_token(maintenance_token):
        raise InstallError("maintenance token must be 64 lowercase hex characters")
    with _host_lock_file(leaf="maintenance.lock") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            payload = _maintenance_lock_payload(lock_fd)
            assert payload is not None
            if not _maintenance_payload_authorizes(
                payload, plan, maintenance_token
            ):
                raise InstallError(
                    "maintenance token does not authorize the active plan"
                )
            yield
        else:
            payload = _maintenance_lock_payload(lock_fd, allow_absent=True)
            if payload is None:
                raise InstallError("maintenance token does not name an active lease")
            if not _maintenance_payload_authorizes(
                payload, plan, maintenance_token
            ):
                raise InstallError(
                    "maintenance token does not authorize the active plan"
                )
            # A killed lease helper leaves its private payload behind.  Holding
            # this shared lock plus the transaction lock lets the exact token
            # continue recovery while the durable marker rejects every new
            # cooperative maintenance window or tokenless mutation.
            yield


@contextmanager
def _install_transaction_lock(
    plan: Mapping[str, object], *, maintenance_token: str | None = None
) -> Iterator[None]:
    """Serialize every trust-root transaction across the whole host.

    Accounts and systemd unit names are host-global even when a valid plan uses
    different deploy/state roots.  The lock identity must therefore not be
    derived from any mutable plan path or receipt override.
    """

    canonical_receipt_path(plan)  # validate enough plan identity for callers/tests
    with _maintenance_admission(plan, maintenance_token=maintenance_token):
        with _host_lock(
            leaf="transaction.lock",
            conflict="another trust-root transaction is already running",
        ):
            yield


@contextmanager
def _maintenance_lease(
    plan: Mapping[str, object],
    *,
    recover_stale: bool = False,
    lifecycle_state: dict[str, bool] | None = None,
) -> Iterator[str]:
    """Serialize the full cooperative stop/apply/verify service lifecycle."""

    token = secrets.token_hex(32)
    with _host_lock(
        leaf="maintenance.lock",
        conflict="another trust-root maintenance window is already running",
    ) as lock_fd:
        stale = _maintenance_lock_payload(lock_fd, allow_absent=True)
        snapshot = _read_maintenance_snapshot()
        if snapshot is not None and (
            not recover_stale
            or snapshot.get("plan_sha256") != plan_sha256(plan)
        ):
            raise InstallError(
                "an unfinished maintenance snapshot requires exact-plan recovery"
            )
        if stale is not None:
            if not recover_stale or stale.get("plan_sha256") != plan_sha256(plan):
                raise InstallError(
                    "a stale trust-root maintenance window requires exact-plan recovery"
                )
        _write_lock_payload(
            lock_fd,
            {
                "plan_sha256": plan_sha256(plan),
                "token_sha256": _sha256_text(token),
            },
        )
        try:
            yield token
        finally:
            if lifecycle_state is None or lifecycle_state.get("complete") is True:
                _write_lock_payload(lock_fd, None)


def _maintenance_snapshot_path() -> Path:
    return _TRUST_ROOT_MAINTENANCE_ROOT / "maintenance-snapshot.json"


def _decode_maintenance_snapshot(raw: bytes) -> dict[str, object]:
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("maintenance snapshot is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "plan_sha256",
        "present_services",
        "previously_active",
        "receipt_path",
        "schema_version",
    }:
        raise InstallError("maintenance snapshot is invalid")
    present = payload.get("present_services")
    active = payload.get("previously_active")
    receipt_path = payload.get("receipt_path")
    if (
        payload.get("schema_version") != 1
        or not isinstance(payload.get("plan_sha256"), str)
        or not isinstance(receipt_path, str)
        or not Path(receipt_path).is_absolute()
        or ".." in Path(receipt_path).parts
        or not isinstance(present, list)
        or not isinstance(active, list)
        or any(not isinstance(name, str) for name in present)
        or any(not isinstance(name, str) for name in active)
        or len(present) != len(set(present))
        or len(active) != len(set(active))
        or any(name not in _MAINTENANCE_SERVICES for name in present)
        or any(name not in present for name in active)
    ):
        raise InstallError("maintenance snapshot is invalid")
    return payload


def _read_maintenance_snapshot() -> dict[str, object] | None:
    with _host_lock_file(
        leaf="maintenance-snapshot.lock", root=_TRUST_ROOT_MAINTENANCE_ROOT
    ) as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        parent_fd, leaf = _open_receipt_parent_directory(
            _maintenance_snapshot_path(), create=True
        )
        snapshot_fd: int | None = None
        try:
            try:
                snapshot_fd = os.open(
                    leaf,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return None
            observed = os.fstat(snapshot_fd)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_nlink != 1
                or observed.st_uid != os.geteuid()
                or stat.S_IMODE(observed.st_mode) != 0o600
            ):
                raise UnsafeInstallPathError(
                    "maintenance snapshot is not a private owned regular file"
                )
            raw = os.pread(snapshot_fd, 65537, 0)
            if not raw or len(raw) > 65536:
                raise InstallError("maintenance snapshot is invalid or too large")
            return _decode_maintenance_snapshot(raw)
        finally:
            if snapshot_fd is not None:
                os.close(snapshot_fd)
            os.close(parent_fd)


def _write_maintenance_snapshot(payload: Mapping[str, object]) -> None:
    validated = _decode_maintenance_snapshot(
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "ascii"
        )
    )
    encoded = (
        json.dumps(validated, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    with _host_lock_file(
        leaf="maintenance-snapshot.lock", root=_TRUST_ROOT_MAINTENANCE_ROOT
    ) as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        path = _maintenance_snapshot_path()
        parent_fd, leaf = _open_receipt_parent_directory(path, create=True)
        temporary_name = f".{leaf}.{secrets.token_hex(16)}.tmp"
        temporary_fd: int | None = None
        try:
            try:
                os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise InstallError(
                    "an unfinished maintenance snapshot requires recovery"
                )
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
            os.fchmod(temporary_fd, 0o600)
            _write_all(temporary_fd, encoded)
            os.fsync(temporary_fd)
            _rename_noreplace_at(parent_fd, temporary_name, leaf)
            temporary_name = ""
            os.fsync(parent_fd)
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            if temporary_fd is not None:
                os.close(temporary_fd)
            os.close(parent_fd)


def _clear_maintenance_snapshot(
    plan: Mapping[str, object], *, receipt_path: Path
) -> bool:
    with _host_lock_file(
        leaf="maintenance-snapshot.lock", root=_TRUST_ROOT_MAINTENANCE_ROOT
    ) as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        parent_fd, leaf = _open_receipt_parent_directory(
            _maintenance_snapshot_path(), create=True
        )
        snapshot_fd: int | None = None
        try:
            try:
                snapshot_fd = os.open(
                    leaf,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return False
            observed = os.fstat(snapshot_fd)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_nlink != 1
                or observed.st_uid != os.geteuid()
                or stat.S_IMODE(observed.st_mode) != 0o600
            ):
                raise UnsafeInstallPathError(
                    "maintenance snapshot is not a private owned regular file"
                )
            raw = os.pread(snapshot_fd, 65537, 0)
            if not raw or len(raw) > 65536:
                raise InstallError("maintenance snapshot is invalid or too large")
            snapshot = _decode_maintenance_snapshot(raw)
            if (
                snapshot.get("plan_sha256") != plan_sha256(plan)
                or snapshot.get("receipt_path") != str(receipt_path)
            ):
                raise InstallError(
                    "maintenance snapshot does not authorize this plan and receipt"
                )
            os.unlink(leaf, dir_fd=parent_fd)
            os.fsync(parent_fd)
            return True
        finally:
            if snapshot_fd is not None:
                os.close(snapshot_fd)
            os.close(parent_fd)


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/systemctl", *args],
        check=False,
        capture_output=True,
        text=True,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
    )


def _service_snapshot() -> tuple[list[str], list[str]]:
    present: list[str] = []
    active: list[str] = []
    for service in _MAINTENANCE_SERVICES:
        load = _systemctl("show", "--property=LoadState", "--value", service)
        if load.returncode != 0:
            raise InstallError(f"cannot inspect service load state: {service}")
        if load.stdout.strip() == "not-found":
            continue
        present.append(service)
        observed = _systemctl("is-active", "--quiet", service)
        if observed.returncode == 0:
            active.append(service)
        elif observed.returncode not in {3, 4}:
            raise InstallError(f"cannot inspect service active state: {service}")
    return present, active


def _stop_current_services() -> list[str]:
    stopped: list[str] = []
    for service in _MAINTENANCE_SERVICES:
        load = _systemctl("show", "--property=LoadState", "--value", service)
        if load.returncode != 0:
            raise InstallError(f"cannot inspect service load state: {service}")
        if load.stdout.strip() == "not-found":
            continue
        result = _systemctl("stop", service)
        if result.returncode != 0:
            raise InstallError(f"cannot stop service during recovery: {service}")
        stopped.append(service)
    return stopped


def _restore_snapshot_services(services: Sequence[str]) -> list[str]:
    """Restore a service snapshot and compensate any partial start failure."""

    restored: list[str] = []
    for service in services:
        result = _systemctl("start", service)
        if result.returncode == 0:
            restored.append(service)
            continue
        stop_failures: list[str] = []
        for started in reversed([*restored, service]):
            if _systemctl("stop", started).returncode != 0:
                stop_failures.append(started)
        detail = (
            f"; additionally could not stop: {', '.join(stop_failures)}"
            if stop_failures
            else ""
        )
        raise InstallError(
            f"cannot restore previously active service: {service}{detail}"
        )
    return restored


def _release_maintenance_marker(
    plan: Mapping[str, object],
    *,
    maintenance_token: str,
    receipt_path: Path | None = None,
) -> bool:
    """Clear only an inactive marker owned by the exact plan/token pair."""

    if not _valid_maintenance_token(maintenance_token):
        raise InstallError("maintenance token must be 64 lowercase hex characters")
    with _host_lock(
        leaf="maintenance.lock",
        conflict="the trust-root maintenance helper is still active",
    ) as lock_fd:
        payload = _maintenance_lock_payload(lock_fd, allow_absent=True)
        if payload is None:
            return False
        if not _maintenance_payload_authorizes(payload, plan, maintenance_token):
            raise InstallError(
                "maintenance token does not authorize the stale plan"
            )
        if receipt_path is not None:
            _clear_maintenance_snapshot(plan, receipt_path=receipt_path)
        _write_lock_payload(lock_fd, None)
        return True


def _receipt_plan(receipt: InstallReceipt) -> Mapping[str, object]:
    plan = receipt.to_dict().get("plan")
    if not isinstance(plan, Mapping):
        raise InstallPlanError("receipt plan is invalid")
    return plan


@contextmanager
def _locked_receipt(
    path: Path, *, maintenance_token: str | None = None
) -> Iterator[tuple[InstallReceipt, Mapping[str, object]]]:
    """Reload a receipt under its install-root transaction lock."""

    initial = InstallReceipt.load(path)
    plan = _receipt_plan(initial)
    with _install_transaction_lock(plan, maintenance_token=maintenance_token):
        yield InstallReceipt.load(path, expected_plan=plan), plan


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex install trust-root",
        description="Plan, apply, credential, activate, verify, and roll back a four-way trust root.",
    )
    sub = parser.add_subparsers(dest="trust_root_command", required=True)

    plan = sub.add_parser("plan", help="produce a rootless exact-artifact desired-state plan")
    plan.add_argument("--config", required=True)
    plan.add_argument(
        "--host-overlay",
        help="allowlisted host delta (account ids, egress home, operator/reader, "
        "builder providers, legacy_adoption); the plan records its digest",
    )
    plan.add_argument("--bundle", required=True)
    plan.add_argument(
        "--legacy-inventory",
        help="reviewed legacy inventory the host overlay's legacy_adoption block binds "
        "(legacy_policy: quarantine only)",
    )
    plan.add_argument("--output", required=True)

    apply = sub.add_parser("apply", help="apply an exact confirmed plan as root")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--confirm-sha256", required=True)
    apply.add_argument(
        "--receipt",
        help="optional absolute receipt path; defaults to the plan's receipt_path",
    )
    apply.add_argument(
        "--prior-receipt",
        help="explicit qualified receipt from the previous candidate during upgrade",
    )
    apply.add_argument(
        "--legacy-inventory",
        help="the reviewed legacy inventory a legacy_adoption plan binds; required "
        "for such a plan, mutually exclusive with --prior-receipt",
    )
    apply.add_argument(
        "--maintenance-token",
        help="exact token emitted by an active plan-bound maintenance lease",
    )

    lease = sub.add_parser(
        "lease", help="hold the host-global maintenance window until stdin closes"
    )
    lease.add_argument("--plan", required=True)
    lease.add_argument("--confirm-sha256", required=True)
    lease.add_argument("--receipt", required=True)
    lease_release = sub.add_parser(
        "lease-release",
        help="clear an inactive maintenance marker with its exact plan-bound token",
    )
    lease_release.add_argument("--plan", required=True)
    lease_release.add_argument("--confirm-sha256", required=True)
    lease_release.add_argument("--receipt", required=True)
    lease_release.add_argument("--maintenance-token", required=True)

    recover = sub.add_parser(
        "recover",
        help="recover an interrupted maintenance snapshot and receipt",
    )
    recover.add_argument("--plan", required=True)
    recover.add_argument("--confirm-sha256", required=True)

    credentials = sub.add_parser("credentials", help="explicit credential import")
    credential_sub = credentials.add_subparsers(dest="credential_command", required=True)
    credential_import = credential_sub.add_parser("import")
    credential_import.add_argument("--receipt", required=True)
    credential_import.add_argument("--principal", required=True)
    credential_import.add_argument("--provider", required=True)
    credential_import.add_argument("--source", required=True)
    credential_import.add_argument("--maintenance-token")

    credential_inherit = credential_sub.add_parser(
        "inherit",
        help="hand the prior receipt's recorded credentials to an upgrade receipt",
    )
    credential_inherit.add_argument("--receipt", required=True)
    credential_inherit.add_argument("--prior-receipt", required=True)
    credential_inherit.add_argument("--maintenance-token")

    activate = sub.add_parser("activate", help="start egress, Manager, then Monitor")
    activate.add_argument("--receipt", required=True)
    activate.add_argument("--maintenance-token")

    verify = sub.add_parser("verify", help="attest installed artifacts and emit evidence")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--json", dest="json_output", action="store_true")
    verify.add_argument("--evidence", required=True)
    verify.add_argument("--maintenance-token")

    rollback = sub.add_parser("rollback", help="roll back only receipt-owned state")
    rollback.add_argument("--receipt", required=True)
    rollback.add_argument(
        "--only-incomplete",
        action="store_true",
        help="skip receipts that are not in an incomplete rollback-safe state",
    )
    rollback.add_argument("--maintenance-token")

    legacy = sub.add_parser(
        "legacy", help="read-only inventory of a host deployed without a receipt"
    )
    legacy_sub = legacy.add_subparsers(dest="legacy_command", required=True)
    legacy_inventory = legacy_sub.add_parser(
        "inventory",
        help="capture a root-only, read-only, self-digested legacy inventory",
    )
    legacy_inventory.add_argument("--config", required=True)
    legacy_inventory.add_argument(
        "--host-overlay",
        help="allowlisted host delta (account ids, egress home, operator/reader, builder providers)",
    )
    legacy_inventory.add_argument("--bundle", required=True)
    legacy_inventory.add_argument(
        "--output", required=True, help="new file; an existing path is never overwritten"
    )
    legacy_show = legacy_sub.add_parser(
        "show", help="print a review summary of a legacy inventory"
    )
    legacy_show.add_argument("--inventory", required=True)
    legacy_purge = legacy_sub.add_parser(
        "purge", help="report or purge adoption quarantine after a qualified upgrade"
    )
    legacy_purge.add_argument("--receipt", required=True, help="legacy adoption receipt")
    legacy_purge.add_argument(
        "--receipts-dir", help="receipt directory to inspect for linked successors"
    )
    legacy_purge.add_argument("--report", required=True, help="report file to write/read")
    legacy_purge.add_argument(
        "--confirm-sha256", help="digest printed by the matching dry-run report"
    )
    return parser


def _bound_plan_from_config(
    config: Mapping[str, object], bundle: Path
) -> dict[str, object]:
    """Plan ``config`` against a validated bundle exactly as ``plan`` does."""

    manifest = validate_bundle_manifest(bundle)
    repo_identity = config.get("repo_identity")
    if not isinstance(repo_identity, Mapping) or repo_identity.get("commit") != manifest["candidate_sha"]:
        raise InstallPlanError(
            "config repo_identity.commit must equal bundle candidate_sha"
        )
    wheel = manifest["wheel"]
    if not isinstance(wheel, Mapping):
        raise InstallPlanError("validated bundle wheel is invalid")
    plan = build_install_plan(
        config=config,
        candidate_wheel=Path(str(wheel["resolved_path"])),
        bundle=Path(str(manifest["manifest_path"])),
    )
    plan = bind_bundle_artifacts(plan, manifest)
    candidate = plan.get("candidate")
    if not isinstance(candidate, dict):
        raise InstallPlanError("generated plan candidate is invalid")
    candidate.update(
        {
            "candidate_sha": manifest["candidate_sha"],
            "wheel": {
                "path": wheel["path"],
                "sha256": wheel["sha256"],
            },
            "wheelhouse": [
                {"path": row["path"], "sha256": row["sha256"]}
                for row in manifest["wheelhouse"]
            ],
            "generated_artifacts": [
                {"path": row["path"], "sha256": row["sha256"]}
                for row in manifest["generated_artifacts"]
            ],
        }
    )
    venv_step = next(
        (
            step
            for step in plan.get("apply_order", [])
            if isinstance(step, dict) and step.get("kind") == "venv"
        ),
        None,
    )
    if not isinstance(venv_step, dict):
        raise InstallPlanError("generated plan lacks the candidate venv step")
    venv_step["wheel_source"] = wheel["resolved_path"]
    venv_step["wheelhouse"] = [
        {"source": row["resolved_path"], "sha256": row["sha256"]}
        for row in manifest["wheelhouse"]
    ]
    if not any(
        row["sha256"] == wheel["sha256"] for row in venv_step["wheelhouse"]
    ):
        raise InstallPlanError("bundle wheelhouse must include the exact candidate wheel")
    plan["receipt_path"] = str(canonical_receipt_path(plan))
    return plan


def _host_machine_id() -> str:
    """This host's machine identity; ``/etc/machine-id`` is world-readable."""

    return LocalLegacyHostBackend(require_root=False).machine_id()


def _plan_document(
    config: Mapping[str, object],
    bundle: Path,
    *,
    overlay: Mapping[str, object] | None = None,
    legacy_inventory: Path | None = None,
    machine_id: str | None = None,
) -> dict[str, object]:
    """Plan ``config`` with an optional host overlay and legacy inventory.

    Without an overlay the plan is exactly the release config's plan.  An
    overlay changes only its allowlisted fields and records its digest.  A
    ``legacy_adoption`` block (``legacy_policy: quarantine`` only) binds the
    reviewed inventory, which must have been captured on this host with the
    same config, overlay and bundle.
    """

    validated = validate_host_overlay(overlay) if overlay is not None else None
    request = legacy_adoption_request(
        config, validated, inventory_given=legacy_inventory is not None
    )
    effective = apply_host_overlay(config, validated) if validated is not None else config
    plan = bind_host_overlay(_bound_plan_from_config(effective, Path(bundle)), validated)
    if request is None:
        return plan
    assert legacy_inventory is not None
    inventory = LegacyInventory.load(Path(legacy_inventory).expanduser().absolute())
    return derive_legacy_adoption(
        plan,
        request=request,
        overlay=validated,
        inventory=inventory,
        host_binding_sha256=host_binding_sha256(
            machine_id if machine_id is not None else _host_machine_id()
        ),
    )


def _plan_command(args: argparse.Namespace) -> int:
    config = _load_mapping(Path(args.config), label="trust-root config")
    overlay = (
        _load_mapping(Path(args.host_overlay), label="host overlay")
        if args.host_overlay is not None
        else None
    )
    plan = _plan_document(
        config,
        Path(args.bundle),
        overlay=overlay,
        legacy_inventory=(
            Path(args.legacy_inventory) if args.legacy_inventory is not None else None
        ),
    )
    output = Path(args.output).expanduser().absolute()
    atomic_write_json(output, plan, mode=0o600)
    payload: dict[str, object] = {"output": str(output), "plan_sha256": plan_sha256(plan)}
    if "host_overlay_sha256" in plan:
        payload["host_overlay_sha256"] = plan["host_overlay_sha256"]
    legacy_block = plan.get("legacy_adoption")
    if isinstance(legacy_block, Mapping):
        quarantine = legacy_block["quarantine"]
        assert isinstance(quarantine, list)
        payload["legacy_adoption"] = {
            "inventory_sha256": legacy_block["inventory_sha256"],
            "quarantine": len(quarantine),
            "summary": legacy_block["summary"],
        }
    _emit(payload)
    return 0


def _legacy_host_backend() -> LocalLegacyHostBackend:
    """The root-only read-only host view legacy re-captures run against."""

    return LocalLegacyHostBackend()


def _legacy_rollback_host(receipt: InstallReceipt) -> LocalLegacyHostBackend | None:
    """A host view for proving a legacy rollback, when the receipt adopted one."""

    if receipt.to_dict().get("legacy_adoption") is None:
        return None
    return _legacy_host_backend()


def _apply_command(args: argparse.Namespace) -> int:
    _require_root()
    plan = _load_plan(Path(args.plan))
    validate_apply_plan(plan, confirm_sha256=args.confirm_sha256)
    receipt_path = (
        Path(args.receipt).expanduser()
        if args.receipt is not None
        else _receipt_path(plan)
    )
    if not receipt_path.is_absolute() or ".." in receipt_path.parts:
        raise UnsafeInstallPathError(
            f"receipt path must be absolute and contain no '..': {receipt_path}"
        )
    # Refuse a wrong legacy flag combination before any lock, file or host read.
    if args.legacy_inventory is not None and args.prior_receipt is not None:
        raise InstallPlanError(
            "--legacy-inventory and --prior-receipt are mutually exclusive"
        )
    if "legacy_adoption" in plan and args.legacy_inventory is None:
        raise InstallPlanError(
            "this plan carries a legacy_adoption block; apply requires --legacy-inventory"
        )
    if "legacy_adoption" not in plan and args.legacy_inventory is not None:
        raise InstallPlanError(
            "--legacy-inventory is accepted only for a plan that carries a "
            "legacy_adoption block"
        )
    legacy_inventory = (
        Path(args.legacy_inventory).expanduser().absolute()
        if args.legacy_inventory is not None
        else None
    )
    with _install_transaction_lock(
        plan, maintenance_token=args.maintenance_token
    ):
        prior_receipt = (
            InstallReceipt.load(Path(args.prior_receipt))
            if args.prior_receipt is not None
            else None
        )
        if prior_receipt is not None:
            validate_prior_receipt_handoff(plan, prior_receipt)
        legacy_context = (
            LegacyApplyContext(
                plan,
                LegacyInventory.load(legacy_inventory),
                _legacy_host_backend(),
                inventory_path=str(legacy_inventory),
            )
            if legacy_inventory is not None
            else None
        )
        receipt = (
            InstallReceipt.load(receipt_path, expected_plan=plan)
            if receipt_path.exists()
            else new_install_receipt(plan, path=receipt_path)
        )
        apply_plan(
            plan,
            confirm_sha256=args.confirm_sha256,
            receipt=receipt,
            prior_receipt=prior_receipt,
            backend=LocalInstallBackend(),
            legacy=legacy_context,
        )
    _emit(
        {
            "receipt": str(receipt_path),
            "receipt_id": receipt.to_dict()["receipt_id"],
            "state": receipt.to_dict()["state"],
        }
    )
    return 0


def _lease_command(args: argparse.Namespace) -> int:
    _require_root()
    plan = _load_plan(Path(args.plan))
    validate_apply_plan(plan, confirm_sha256=args.confirm_sha256)
    receipt_path = Path(args.receipt).expanduser()
    if not receipt_path.is_absolute() or ".." in receipt_path.parts:
        raise UnsafeInstallPathError(
            f"receipt path must be absolute and contain no '..': {receipt_path}"
        )
    lifecycle_state = {"complete": False}
    with _maintenance_lease(
        plan,
        lifecycle_state=lifecycle_state,
    ) as maintenance_token:
        try:
            os.lstat(receipt_path)
        except FileNotFoundError:
            pass
        else:
            raise InstallError(
                "current receipt path already exists; run explicit recovery"
            )
        present_services, previously_active = _service_snapshot()
        snapshot = {
            "schema_version": 1,
            "plan_sha256": plan_sha256(plan),
            "receipt_path": str(receipt_path),
            "present_services": present_services,
            "previously_active": previously_active,
        }
        _write_maintenance_snapshot(snapshot)
        _emit(
            {
                "maintenance_lease": True,
                "maintenance_token": maintenance_token,
                "plan_sha256": plan_sha256(plan),
                "present_services": present_services,
                "previously_active": previously_active,
                "receipt_path": str(receipt_path),
                "snapshot_path": str(_maintenance_snapshot_path()),
            }
        )
        sys.stdout.flush()
        # Only an exact completion record clears the durable snapshot/marker.
        # EOF or process death preserves both for `recover`.
        command = sys.stdin.buffer.readline()
        trailing = sys.stdin.buffer.read()
        if command != b"complete\n" or trailing:
            return 1
        _clear_maintenance_snapshot(plan, receipt_path=receipt_path)
        lifecycle_state["complete"] = True
        return 0


def _lease_release_command(args: argparse.Namespace) -> int:
    _require_root()
    plan = _load_plan(Path(args.plan))
    validate_apply_plan(plan, confirm_sha256=args.confirm_sha256)
    receipt_path = Path(args.receipt).expanduser()
    if not receipt_path.is_absolute() or ".." in receipt_path.parts:
        raise UnsafeInstallPathError(
            f"receipt path must be absolute and contain no '..': {receipt_path}"
        )
    cleared = _release_maintenance_marker(
        plan,
        maintenance_token=args.maintenance_token,
        receipt_path=receipt_path,
    )
    _emit(
        {
            "maintenance_lease_released": True,
            "plan_sha256": plan_sha256(plan),
            "stale_marker_cleared": cleared,
        }
    )
    return 0


def _recover_command(args: argparse.Namespace) -> int:
    _require_root()
    plan = _load_plan(Path(args.plan))
    validate_apply_plan(plan, confirm_sha256=args.confirm_sha256)
    lifecycle_state = {"complete": False}
    with _maintenance_lease(
        plan, recover_stale=True, lifecycle_state=lifecycle_state
    ) as maintenance_token:
        snapshot = _read_maintenance_snapshot()
        if snapshot is None:
            lifecycle_state["complete"] = True
            _emit(
                {
                    "maintenance_recovered": True,
                    "plan_sha256": plan_sha256(plan),
                    "receipt_path": None,
                    "restore_safe": True,
                    "services_restored": [],
                    "services_stopped": [],
                }
            )
            return 0
        if snapshot.get("plan_sha256") != plan_sha256(plan):
            raise InstallError(
                "maintenance snapshot does not authorize this plan"
            )
        receipt_path = Path(str(snapshot["receipt_path"]))
        stopped = _stop_current_services()
        restore_safe = True
        if os.path.lexists(receipt_path):
            with _locked_receipt(
                receipt_path, maintenance_token=maintenance_token
            ) as (receipt, _receipt_plan_value):
                if not _receipt_restore_safe(receipt.to_dict()):
                    rollback_receipt(
                        receipt,
                        backend=LocalInstallBackend(),
                        legacy_host=_legacy_rollback_host(receipt),
                    )
                restore_safe = _receipt_restore_safe(receipt.to_dict())
        if not restore_safe:
            raise InstallError(
                "recovery rollback retained unknown or drifted state; services remain stopped"
            )
        previously_active = snapshot["previously_active"]
        assert isinstance(previously_active, list)
        restored = _restore_snapshot_services(previously_active)
        _clear_maintenance_snapshot(plan, receipt_path=receipt_path)
        lifecycle_state["complete"] = True
        _emit(
            {
                "maintenance_recovered": True,
                "plan_sha256": plan_sha256(plan),
                "receipt_path": str(receipt_path),
                "restore_safe": True,
                "services_restored": restored,
                "services_stopped": stopped,
            }
        )
        return 0


def _credential_command(args: argparse.Namespace) -> int:
    _require_root()
    with _locked_receipt(
        Path(args.receipt), maintenance_token=args.maintenance_token
    ) as (receipt, plan):
        accounts = plan.get("accounts", [])
        account_name = {
            "builder": "cortex-builder",
            "reviewer-planner": "cortex-reviewer-planner",
            "manager": "cortex-manager",
        }.get(args.principal)
        account = next(
            (
                row
                for row in accounts
                if isinstance(row, Mapping) and row.get("name") == account_name
            ),
            None,
        )
        if (
            not isinstance(account, Mapping)
            or not isinstance(account.get("home"), str)
            or not isinstance(account.get("uid"), int)
            or not isinstance(account.get("gid"), int)
        ):
            raise InstallPlanError(
                f"receipt plan lacks the home for principal {args.principal}"
            )
        metadata = import_credential(
            receipt,
            principal=args.principal,
            provider=args.provider,
            source=Path(args.source),
            destination_root=Path(str(account["home"])),
            destination_uid=account["uid"],
            destination_gid=account["gid"],
        )
    _emit(metadata.to_dict())
    return 0


def _credential_inherit_command(args: argparse.Namespace) -> int:
    _require_root()
    prior_path = Path(args.prior_receipt).expanduser()
    with _locked_receipt(
        Path(args.receipt), maintenance_token=args.maintenance_token
    ) as (receipt, _plan):
        prior = InstallReceipt.load(prior_path)
        rows = inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend()
        )
    _emit(
        {
            "receipt_id": receipt.to_dict()["receipt_id"],
            "inherited": [
                {
                    "principal": row["principal"],
                    "provider": row["provider"],
                    "inherited_from": row["inherited_from"],
                }
                for row in rows
            ],
        }
    )
    return 0


def _activate_command(args: argparse.Namespace) -> int:
    _require_root()
    with _locked_receipt(
        Path(args.receipt), maintenance_token=args.maintenance_token
    ) as (receipt, _plan):
        activate_receipt(receipt, backend=LocalInstallBackend())
    _emit(
        {
            "receipt_id": receipt.to_dict()["receipt_id"],
            "services_started": True,
            "qualified": False,
        }
    )
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    _require_root()
    with _locked_receipt(
        Path(args.receipt), maintenance_token=args.maintenance_token
    ) as (receipt, plan):
        expected = plan.get("generated")
        if not isinstance(expected, Mapping):
            raise InstallPlanError("plan generated inventory is invalid")
        backend = LocalInstallBackend()
        result = verify_receipt(
            receipt,
            plan=plan,
            expected_inventory=expected,  # type: ignore[arg-type]
            installed_inventory=backend.installed_inventory(plan),
            service_identities=backend.service_identities(),
            evidence_path=Path(args.evidence).expanduser().absolute(),
            service_controller=backend,
        )
    if args.json_output:
        _emit(result.to_dict())
    else:
        sys.stdout.write(f"trust-root verify: {'PASS' if result.ok else 'FAIL'}\n")
    return 0 if result.ok else 1


def _rollback_command(args: argparse.Namespace) -> int:
    _require_root()
    with _locked_receipt(
        Path(args.receipt), maintenance_token=args.maintenance_token
    ) as (receipt, _plan):
        document = receipt.to_dict()
        state = document.get("state")
        rollback_eligible = state in {
            "applying",
            "rolling-back",
            "rollback-blocked",
        }
        if args.only_incomplete and not rollback_eligible:
            restore_safe = _receipt_restore_safe(document)
            _emit(
                {
                    "receipt_id": document["receipt_id"],
                    "restore_safe": restore_safe,
                    "rollback_skipped": True,
                    "state": state,
                }
            )
            return 0 if restore_safe or state == "applied" else 1
        result = rollback_receipt(
            receipt,
            backend=LocalInstallBackend(),
            legacy_host=_legacy_rollback_host(receipt),
        )
    payload = result.to_dict()
    payload["restore_safe"] = _receipt_restore_safe(receipt.to_dict())
    _emit(payload)
    return 0 if payload["restore_safe"] is True else 1


_RECEIPT_MANAGED_HINT = (
    "this host is managed by the transactional installer; upgrade it with "
    "`apply --prior-receipt <applied and qualified receipt>` instead of legacy adoption"
)


@contextmanager
def _legacy_inventory_admission(plan: Mapping[str, object]) -> Iterator[None]:
    """Admit a read-only legacy capture on a quiescent host with no receipt history.

    It holds the maintenance lock shared (no lease may start mid-capture) and
    the host-global transaction lock shared (no mutating transaction runs, while
    concurrent read-only captures may).  It creates nothing outside the
    tmpfs lock directory: the maintenance snapshot is only probed, never opened
    through the helpers that create its persistent parent.
    """

    with _host_lock_file(leaf="maintenance.lock") as maintenance_fd:
        try:
            fcntl.flock(maintenance_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError(
                f"a trust-root maintenance lease is active; {_RECEIPT_MANAGED_HINT}"
            ) from exc
        try:
            marker = _maintenance_lock_payload(maintenance_fd, allow_absent=True)
        except InstallError:
            marker = {"invalid": True}
        if marker is not None:
            raise InstallError(
                f"a trust-root maintenance lease marker exists; {_RECEIPT_MANAGED_HINT}"
            )
        with _host_lock(
            leaf="transaction.lock",
            conflict="another trust-root transaction is running; legacy inventory needs it to finish",
            shared=True,
        ):
            receipt_parent = canonical_receipt_path(plan).parent
            if os.path.lexists(receipt_parent):
                raise InstallError(
                    f"canonical receipt parent exists: {receipt_parent}; {_RECEIPT_MANAGED_HINT}"
                )
            snapshot = _maintenance_snapshot_path()
            if os.path.lexists(snapshot):
                raise InstallError(
                    f"maintenance snapshot exists: {snapshot}; {_RECEIPT_MANAGED_HINT}"
                )
            yield


def _legacy_inventory_command(args: argparse.Namespace) -> int:
    _require_root()
    config = _load_mapping(Path(args.config), label="trust-root config")
    overlay = (
        validate_host_overlay(
            _load_mapping(Path(args.host_overlay), label="host overlay")
        )
        if args.host_overlay is not None
        else None
    )
    plan = _bound_plan_from_config(
        apply_host_overlay(config, overlay), Path(args.bundle)
    )
    # Refuse an output inside anything the capture records before touching it.
    output = check_inventory_output(
        Path(args.output).expanduser().absolute(), legacy_scope(plan)
    )
    if os.path.lexists(output):
        raise InstallError(
            f"legacy inventory output already exists; refusing to overwrite: {output}"
        )
    with _legacy_inventory_admission(plan):
        document = collect_legacy_inventory(
            plan=plan, backend=LocalLegacyHostBackend(), host_overlay=overlay
        )
        publish_inventory(output, document)
    host = document["host"]
    census = document["census"]
    assert isinstance(host, Mapping) and isinstance(census, Mapping)
    census_stable = all(
        isinstance(row, Mapping) and row.get("status") != "unstable"
        for row in census.values()
    )
    if not census_stable:
        sys.stderr.write(
            "trust-root legacy inventory: the writable census is unstable (entries "
            "changed while they were checked); any adoption plan or apply gate must "
            "refuse this inventory -- rerun the capture with the services stopped\n"
        )
    _emit(
        {
            "output": str(output),
            "inventory_sha256": document["inventory_sha256"],
            "scope_sha256": document["scope_sha256"],
            "host_binding_sha256": host["binding_sha256"],
            "census_stable": census_stable,
        }
    )
    return 0


def _legacy_show_command(args: argparse.Namespace) -> int:
    inventory = LegacyInventory.load(Path(args.inventory).expanduser().absolute())
    sys.stdout.write(render_inventory_summary(inventory))
    return 0


def _legacy_purge_command(args: argparse.Namespace) -> int:
    _require_root()
    receipt_path = Path(args.receipt).expanduser().absolute()
    receipts_dir = (
        Path(args.receipts_dir).expanduser().absolute()
        if args.receipts_dir
        else receipt_path.parent
    )
    report_path = Path(args.report).expanduser().absolute()
    report = build_legacy_purge_report(receipt_path, receipts_dir=receipts_dir)
    if args.confirm_sha256 is None:
        atomic_write_json(report_path, report, mode=0o600)
        _emit(report)
        return 0
    if args.confirm_sha256 != report.get("report_sha256"):
        raise InstallPlanError("--confirm-sha256 does not match the current purge report")
    try:
        recorded = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallPlanError(f"cannot read purge report {report_path}: {exc}") from exc
    if recorded != report:
        raise InstallPlanError("purge report file does not match the current report")
    with _locked_receipt(receipt_path) as (receipt, _plan):
        locked_report = build_legacy_purge_report(
            receipt_path, receipts_dir=receipts_dir
        )
        if locked_report != report:
            raise InstallPlanError("purge eligibility changed after the report was written")
        results = purge_legacy_entries(receipt, locked_report)
    _emit({"report_sha256": report["report_sha256"], "purged": results})
    return 0


def _only_inherited_credentials(rows: object) -> bool:
    return isinstance(rows, list) and all(is_inherited_credential(row) for row in rows)


def _receipt_restore_safe(document: Mapping[str, object]) -> bool:
    """Prove that restarting the pre-transaction services is safe."""

    state = document.get("state")
    journal = document.get("journal")
    activation = document.get("activation_journal", [])
    credentials = document.get("credential_journal", [])
    if not all(isinstance(rows, list) for rows in (journal, activation, credentials)):
        return False
    if journal or activation or credentials:
        return False
    if document.get("services_started") is True:
        return False
    if state == "planned":
        return not document.get("credentials", [])
    if state != "rolled-back":
        return False
    rollback = document.get("rollback")
    return bool(
        isinstance(rollback, Mapping)
        and rollback.get("retained_unknown") == []
        and rollback.get("retained_drift") == []
        and _only_inherited_credentials(document.get("credentials", []))
        # A legacy adoption is restore-safe only once a re-capture proved the
        # reviewed legacy host is back.
        and (
            document.get("legacy_adoption") is None
            or rollback.get("legacy_restored") is True
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.trust_root_command == "plan":
            return _plan_command(args)
        if args.trust_root_command == "apply":
            return _apply_command(args)
        if args.trust_root_command == "lease":
            return _lease_command(args)
        if args.trust_root_command == "lease-release":
            return _lease_release_command(args)
        if args.trust_root_command == "recover":
            return _recover_command(args)
        if args.trust_root_command == "credentials":
            if args.credential_command == "inherit":
                return _credential_inherit_command(args)
            return _credential_command(args)
        if args.trust_root_command == "activate":
            return _activate_command(args)
        if args.trust_root_command == "verify":
            return _verify_command(args)
        if args.trust_root_command == "rollback":
            return _rollback_command(args)
        if args.trust_root_command == "legacy":
            if args.legacy_command == "inventory":
                return _legacy_inventory_command(args)
            if args.legacy_command == "show":
                return _legacy_show_command(args)
            return _legacy_purge_command(args)
    except (InstallError, PermissionError, OSError, ValueError) as exc:
        sys.stderr.write(f"trust-root install failed: {exc}\n")
        return 1
    raise AssertionError("unreachable trust-root command")
