"""`cortex upgrade <version>`: one root command from release ingress to a verified receipt (#1263).

The installed CLI coordinates: it verifies the release itself, decides the
current receipt from the receipt chain, plans as an unprivileged account,
binds the plan sha, and holds the maintenance lease in-process.  Every receipt
mutation -- apply, credential inheritance, activate, verify, rollback -- runs
through the sealed candidate CLI with the lease token, exactly as
trust-root-transactional-install.md does by hand.

`/opt/cortex/venv` is a symlink that apply switches to the candidate slot.  This
module therefore imports everything at module level, and `_pin_import_paths()`
resolves the running slot before anything else happens.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import os
import pwd
import re
import secrets
import signal
import stat
import sys
import time
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess
from typing import Iterator, Mapping

from . import cli as install_cli
from .backend import JOB_ACCOUNT_NAMES, _run
from .core import (
    InstallError,
    InstallReceipt,
    _rename_noreplace_at,
    _write_all,
    atomic_write_json,
    plan_sha256,
    validate_prior_receipt_handoff,
)
from .legacy import LocalLegacyHostBackend, host_overlay_record, validate_host_overlay
from .loaded_runtime import installed_runtime_env, loaded_runtime_mismatch, runtime_expected
from .receipt_chain import effective_receipt
from .release_ingress import (
    OFFICIAL_REPOSITORY,
    DirectoryReleaseFetcher,
    GitHubReleaseFetcher,
    ReleaseFetcher,
    SealedCandidate,
    format_version,
    ingest_release,
    parse_version,
    wheel_version,
)

QUALIFICATION_ENV = "PSC_UPGRADE_QUALIFICATION"
_STATE_ROOT = Path("/var/lib/cortex")
_OWNER_UID = 0
_CHAIN_STOP = Path("/")
_IDLE_POLL_SECONDS = 5.0
_STATUS_SETTLE_SECONDS = 60.0
_STATUS_POLL_SECONDS = 2.0
_JOB_ACCOUNTS = JOB_ACCOUNT_NAMES
_sleep = time.sleep
_monotonic = time.monotonic
_chown = os.chown
_lookup_account = pwd.getpwnam


class UpgradeError(InstallError):
    """`cortex upgrade` refused or stopped."""


@dataclass(frozen=True)
class UpgradeOptions:
    version: str | None = None
    wait_idle: int = 0
    json_output: bool = False
    recover: bool = False
    status: bool = False
    release_source: Path | None = None
    # RC-qualification-only (PSC_UPGRADE_QUALIFICATION=1): lets a drill rerun
    # the exact same version, and (Task 8) relaxes the host-overlay-digest
    # equality check against the prior plan.  Never used in production.
    allow_same_version: bool = False
    prior_receipt: Path | None = None


def _absolute(value: object, *, flag: str) -> Path | None:
    if value is None:
        return None
    path = Path(str(value))
    if not path.is_absolute() or ".." in path.parts:
        raise UpgradeError(f"{flag} must be an absolute path without '..'")
    return path


def options_from_args(args: Namespace, *, environ: Mapping[str, str]) -> UpgradeOptions:
    """Parsed arguments to options; test-only flags need PSC_UPGRADE_QUALIFICATION=1."""

    test_only = [
        flag
        for flag, value in (
            ("--release-source", args.release_source),
            ("--allow-same-version", args.allow_same_version),
            ("--prior-receipt", args.prior_receipt),
        )
        if value
    ]
    if test_only and environ.get(QUALIFICATION_ENV) != "1":
        raise UpgradeError(
            f"{', '.join(test_only)} is accepted only for RC qualification "
            f"({QUALIFICATION_ENV}=1)"
        )
    if args.wait_idle < 0:
        raise UpgradeError("--wait-idle must be zero or more seconds")
    if args.recover or args.status:
        if args.version is not None or test_only or args.wait_idle:
            raise UpgradeError("--recover and --status take no version or upgrade options")
    elif args.version is None:
        raise UpgradeError("a target version is required, e.g. `cortex upgrade 0.1.13`")
    else:
        parse_version(args.version)
    return UpgradeOptions(
        version=args.version,
        wait_idle=args.wait_idle,
        json_output=bool(args.json_output),
        recover=bool(args.recover),
        status=bool(args.status),
        release_source=_absolute(args.release_source, flag="--release-source"),
        allow_same_version=bool(args.allow_same_version),
        prior_receipt=_absolute(args.prior_receipt, flag="--prior-receipt"),
    )


def _pin_import_paths() -> None:
    """Resolve the running venv slot so apply's symlink switch cannot swap our code."""

    prefix = Path(sys.prefix)
    resolved = prefix.resolve()
    if resolved == prefix:
        return
    old = str(prefix)
    pinned: list[str] = []
    for entry in sys.path:
        if entry == old:
            pinned.append(str(resolved))
        elif entry.startswith(old + os.sep):
            pinned.append(str(resolved) + entry[len(old):])
        else:
            pinned.append(entry)
    sys.path[:] = pinned
    importlib.invalidate_caches()


@dataclass(frozen=True)
class PriorReceipt:
    path: Path
    receipt: InstallReceipt
    version: tuple[int, int, int]

    @property
    def document(self) -> dict[str, object]:
        return self.receipt.to_dict()

    @property
    def plan(self) -> Mapping[str, object]:
        plan = self.document.get("plan")
        if not isinstance(plan, Mapping):
            raise UpgradeError("the current receipt has no embedded plan")
        return plan


def locate_prior(options: UpgradeOptions) -> PriorReceipt:
    """The current receipt: the receipt chain in production, explicit only for RC."""

    if options.prior_receipt is not None:
        receipt = InstallReceipt.load(options.prior_receipt)
    else:
        receipt = effective_receipt(_STATE_ROOT)
    document = receipt.to_dict()
    if document.get("state") != "applied" or document.get("qualified") is not True:
        raise UpgradeError(
            "the current receipt is not applied and qualified; finish or recover it first"
        )
    if receipt.path is None:
        raise UpgradeError("the current receipt has no durable path")
    plan = document.get("plan")
    candidate = plan.get("candidate") if isinstance(plan, Mapping) else None
    wheel = candidate.get("wheel") if isinstance(candidate, Mapping) else None
    version = wheel_version(wheel.get("path") if isinstance(wheel, Mapping) else None)
    return PriorReceipt(path=receipt.path, receipt=receipt, version=version)


def check_target_version(
    target: str, current: tuple[int, int, int], *, allow_same_version: bool
) -> tuple[int, int, int]:
    wanted = parse_version(target)
    if wanted > current or (allow_same_version and wanted == current):
        return wanted
    raise UpgradeError(
        f"cortex upgrade only moves forward: current {format_version(current)}, "
        f"requested {target}; use the installer rollback or the runbook to go back"
    )


def _service_status(plan: Mapping[str, object], receipt_path: Path) -> object:
    """`cortex service status --system --json` of the installed CLI, or None."""

    roots = plan.get("roots")
    if not isinstance(roots, Mapping):
        return None
    deploy = Path(str(roots.get("deploy")))
    try:
        env = installed_runtime_env(
            deploy, Path(str(roots.get("state"))), owner_uid=_OWNER_UID
        )
        result = _run(
            (
                str(deploy / "venv" / "bin" / "cortex"),
                "service",
                "status",
                "--system",
                "--json",
                "--install-receipt",
                str(receipt_path),
            ),
            env=env,
        )
    except (InstallError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def await_loaded_runtime(
    plan: Mapping[str, object],
    receipt_path: Path,
    expected: Mapping[str, object],
    *,
    settle_seconds: float | None = None,
) -> tuple[str, object]:
    """Poll until Manager/Monitor load ``expected`` or the settle window closes."""

    window = _STATUS_SETTLE_SECONDS if settle_seconds is None else settle_seconds
    deadline = _monotonic() + window
    while True:
        payload = _service_status(plan, receipt_path)
        mismatch = (
            "service_status=unavailable"
            if payload is None
            else loaded_runtime_mismatch(payload, expected)
        )
        if not mismatch or _monotonic() >= deadline:
            return mismatch, payload
        _sleep(_STATUS_POLL_SECONDS)


def assert_installer_idle() -> None:
    """No maintenance window, stale marker, snapshot or transaction is in flight."""

    with install_cli._host_lock(
        leaf="maintenance.lock",
        conflict=(
            "a trust-root maintenance window is active; wait for it or run "
            "`cortex upgrade --recover`"
        ),
        shared=True,
    ) as lock_fd:
        if install_cli._maintenance_lock_payload(lock_fd, allow_absent=True) is not None:
            raise UpgradeError(
                "a stale trust-root maintenance marker exists; run `cortex upgrade --recover`"
            )
        if os.path.lexists(install_cli._maintenance_snapshot_path()):
            raise UpgradeError(
                "an unfinished maintenance snapshot exists; run `cortex upgrade --recover`"
            )
        with install_cli._host_lock(
            leaf="transaction.lock",
            conflict="another trust-root transaction is running",
            shared=True,
        ):
            return


def in_flight_counts(plan: Mapping[str, object]) -> tuple[int, int | None]:
    """Job-account processes and durable in-flight jobs (legacy adoption's gate)."""

    accounts = plan.get("accounts")
    job_uids = {
        str(row["name"]): int(row["uid"])
        for row in (accounts if isinstance(accounts, list) else [])
        if isinstance(row, Mapping)
        and row.get("name") in _JOB_ACCOUNTS
        and isinstance(row.get("uid"), int)
    }
    observed = LocalLegacyHostBackend().in_flight(job_uids, plan)
    processes = observed.get("job_processes")
    durable = observed.get("durable_jobs")
    return (
        processes if isinstance(processes, int) else 1,
        durable if isinstance(durable, int) else None,
    )


def wait_until_idle(plan: Mapping[str, object], *, wait_seconds: int) -> None:
    """Refuse in-flight jobs; an unreadable durable registry counts as busy."""

    deadline = _monotonic() + wait_seconds
    while True:
        processes, durable = in_flight_counts(plan)
        if processes == 0 and durable == 0:
            return
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise UpgradeError(
                f"in-flight jobs block the upgrade: job processes={processes}, "
                f"durable in-flight jobs={'unknown' if durable is None else durable}; "
                "retry when idle or pass --wait-idle <seconds>"
            )
        _sleep(min(_IDLE_POLL_SECONDS, remaining))


@dataclass(frozen=True)
class Preflight:
    prior: PriorReceipt
    target: tuple[int, int, int]


def preflight(options: UpgradeOptions) -> Preflight:
    """Spec §4 step 1: read-only checks; nothing on the host changes."""

    if options.version is None:
        raise UpgradeError("a target version is required")
    prior = locate_prior(options)
    target = check_target_version(
        options.version, prior.version, allow_same_version=options.allow_same_version
    )
    mismatch, _payload = await_loaded_runtime(
        prior.plan, prior.path, runtime_expected(prior.document)
    )
    if mismatch:
        raise UpgradeError(
            f"the running services do not match the current receipt ({mismatch}); "
            "the effective receipt cannot be confirmed"
        )
    assert_installer_idle()
    wait_until_idle(prior.plan, wait_seconds=options.wait_idle)
    return Preflight(prior=prior, target=target)


# --- plan (spec §4 step 3) -----------------------------------------------------

_HOST_OVERLAY_NAME = "host-overlay.yaml"
_PLAN_ENV = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1"}
_MAX_PLAN_BYTES = 64 * 1024 * 1024
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


@dataclass(frozen=True)
class BoundPlan:
    plan: dict[str, object]
    sha256: str
    durable_path: Path
    receipt_path: Path
    overlay_sha256: str | None


def _read_regular_bytes(path: Path, *, limit: int = _MAX_PLAN_BYTES) -> bytes:
    descriptor = os.open(path, _READ_FLAGS)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise UpgradeError(f"expected a single-link regular file: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UpgradeError(f"file is too large: {path}")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_new_file(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor = os.open(path, _CREATE_FLAGS, 0o600)
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_host_overlay(installer_root: Path) -> dict[str, object] | None:
    """The persisted host overlay minus ``legacy_adoption`` (its digest is unchanged)."""

    path = installer_root / _HOST_OVERLAY_NAME
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != _OWNER_UID
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise UpgradeError(
            f"host overlay must be a regular file owned by uid {_OWNER_UID} "
            f"without group/other write: {path}"
        )
    overlay = install_cli._load_mapping(path, label="host overlay")
    stripped = {key: value for key, value in overlay.items() if key != "legacy_adoption"}
    return validate_host_overlay(stripped) or None


def plan_identity(overlay: Mapping[str, object] | None) -> tuple[int, int]:
    """The overlay's operator account, or ``nobody``; never root."""

    name = overlay.get("operator_account") if overlay is not None else None
    if isinstance(name, str) and name:
        try:
            account = _lookup_account(name)
        except KeyError as exc:
            raise UpgradeError(
                f"host overlay operator_account does not exist on this host: {name}"
            ) from exc
        if account.pw_uid != 0:
            return account.pw_uid, account.pw_gid
    try:
        fallback = _lookup_account("nobody")
    except KeyError as exc:
        raise UpgradeError("no unprivileged account is available to produce the plan") from exc
    if fallback.pw_uid == 0:
        raise UpgradeError("the fallback plan account resolves to root")
    return fallback.pw_uid, fallback.pw_gid


def publish_durable_plan(payload: bytes, expected_sha256: str, *, plans_root: Path) -> Path:
    """Runbook §2 durable publication: never overwrite, reuse only identical bytes."""

    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise UpgradeError("reviewed plan changed before durable publication")
    try:
        os.mkdir(plans_root, 0o700)
    except FileExistsError:
        pass
    root_stat = plans_root.lstat()
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != _OWNER_UID
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise UpgradeError("durable plan root is unsafe")
    target_name = f"{expected_sha256}.json"
    staging_name = f".{target_name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    root_fd = os.open(plans_root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    lock_fd: int | None = None
    staging_fd: int | None = None
    try:
        lock_fd = os.open(
            ".publish.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_fd,
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lock_stat = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != _OWNER_UID
            or lock_stat.st_nlink != 1
            or stat.S_IMODE(lock_stat.st_mode) != 0o600
        ):
            raise UpgradeError("durable plan publication lock is unsafe")
        try:
            existing_fd = os.open(target_name, _READ_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            existing_fd = None
        if existing_fd is not None:
            try:
                observed = os.fstat(existing_fd)
                existing = b""
                while chunk := os.read(existing_fd, 1024 * 1024):
                    existing += chunk
            finally:
                os.close(existing_fd)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != _OWNER_UID
                or observed.st_nlink != 1
                or stat.S_IMODE(observed.st_mode) != 0o600
                or existing != payload
            ):
                raise UpgradeError("existing durable plan does not match reviewed bytes")
        else:
            staging_fd = os.open(staging_name, _CREATE_FLAGS, 0o600, dir_fd=root_fd)
            os.fchmod(staging_fd, 0o600)
            _write_all(staging_fd, payload)
            os.fsync(staging_fd)
            os.close(staging_fd)
            staging_fd = None
            _rename_noreplace_at(root_fd, staging_name, target_name)
            os.fsync(root_fd)
    finally:
        if staging_fd is not None:
            os.close(staging_fd)
        try:
            os.unlink(staging_name, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(root_fd)
    return plans_root / target_name


def _bind_plan_to_release(
    plan: Mapping[str, object],
    *,
    sealed: SealedCandidate,
    prior: PriorReceipt,
    overlay_sha: str | None,
) -> None:
    candidate = plan.get("candidate")
    wheel = candidate.get("wheel") if isinstance(candidate, Mapping) else None
    if not isinstance(candidate, Mapping) or candidate.get("candidate_sha") != sealed.metadata.commit:
        raise UpgradeError("plan candidate is not the release tag target")
    if not isinstance(wheel, Mapping) or wheel.get("sha256") != sealed.metadata.wheel.sha256:
        raise UpgradeError("plan candidate wheel is not the release wheel")
    if format_version(wheel_version(wheel.get("path"))) != sealed.metadata.version:
        raise UpgradeError("plan candidate wheel version is not the requested version")
    if plan.get("host_overlay_sha256") != overlay_sha:
        raise UpgradeError("plan did not bind the host overlay it was given")
    if "legacy_adoption" in plan:
        raise UpgradeError("an upgrade plan must not carry a legacy_adoption block")
    validate_prior_receipt_handoff(plan, prior.receipt)


def produce_plan(
    sealed: SealedCandidate, prior: PriorReceipt, *, options: UpgradeOptions
) -> BoundPlan:
    """Runbook §2 without the human: plan unprivileged, bind the sha, publish durably."""

    installer_root = install_cli._TRUST_ROOT_MAINTENANCE_ROOT
    overlay = read_host_overlay(installer_root)
    record = host_overlay_record(overlay) if overlay is not None else None
    overlay_sha = str(record["sha256"]) if record is not None else None
    # `allow_same_version` relaxes this equality check only for the RC
    # qualification drill (gated by PSC_UPGRADE_QUALIFICATION=1 in
    # `options_from_args`), where rerunning the same version against an
    # already-matching overlay is expected; production upgrades never set it.
    if overlay_sha != prior.plan.get("host_overlay_sha256") and not options.allow_same_version:
        raise UpgradeError(
            "the host overlay differs from the one the current receipt was planned with; "
            "overlay changes go through trust-root-transactional-install.md"
        )
    uid, gid = plan_identity(overlay)
    work = sealed.attempt_dir / "plan"
    os.mkdir(work, 0o700)
    _chown(work, uid, gid)
    home = work / "home"
    os.mkdir(home, 0o700)
    _chown(home, uid, gid)
    overlay_args: tuple[str, ...] = ()
    if overlay is not None:
        overlay_path = sealed.attempt_dir / "host-overlay.json"
        _write_new_file(
            overlay_path,
            (json.dumps(overlay, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o644,
        )
        overlay_args = ("--host-overlay", str(overlay_path))
    output = work / "install-plan.json"
    sealed.assert_unchanged()
    result = _run(
        (
            str(sealed.cli),
            "install",
            "trust-root",
            "plan",
            "--config",
            str(sealed.install_config),
            "--bundle",
            str(sealed.bundle),
            *overlay_args,
            "--output",
            str(output),
        ),
        check=True,
        env={**_PLAN_ENV, "HOME": str(home), "PATH": f"{sealed.venv}/bin:/usr/bin:/bin"},
        uid=uid,
        gid=gid,
    )
    try:
        reported = json.loads(result.stdout)["plan_sha256"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise UpgradeError("candidate plan did not report its plan_sha256") from exc
    payload = _read_regular_bytes(output)
    observed = hashlib.sha256(payload).hexdigest()
    if reported != observed:
        raise UpgradeError("candidate plan sha256 does not match the plan file")
    plan = json.loads(payload.decode("utf-8"))
    if not isinstance(plan, dict) or plan_sha256(plan) != observed:
        raise UpgradeError("plan file is not the canonical plan document")
    _bind_plan_to_release(plan, sealed=sealed, prior=prior, overlay_sha=overlay_sha)
    canonical = Path(str(plan.get("receipt_path")))
    if not canonical.is_absolute() or ".." in canonical.parts:
        raise UpgradeError("plan receipt_path is not absolute")
    durable = publish_durable_plan(payload, observed, plans_root=installer_root / "plans")
    receipt_path = canonical.with_name(f"{canonical.stem}.run-{secrets.token_hex(16)}.json")
    return BoundPlan(
        plan=plan,
        sha256=observed,
        durable_path=durable,
        receipt_path=receipt_path,
        overlay_sha256=overlay_sha,
    )


# --- transaction (spec §4 steps 4–8, §6, §12.4) -----------------------------------

_CANDIDATE_ENV = {"HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1"}
_REPORT_NAME = "upgrade-report.json"
_LAST_REPORT_NAME = "last-upgrade-report.json"
_HALTED_NEXT_ACTION = (
    "services remain stopped and the maintenance snapshot is kept; resolve the "
    "retained state listed above, then run `cortex upgrade --recover` or decide "
    "by hand per trust-root-transactional-install.md §6"
)
# Cap on the rollback child's stderr/stdout we fold into the report; keeps a
# noisy backend traceback from bloating `upgrade-report.json`.
_ROLLBACK_ERROR_LIMIT = 4000


class UpgradeInterrupted(BaseException):
    """INT/TERM inside the maintenance window; handled like any step failure."""


def _interrupt(signum: int, _frame: object) -> None:
    raise UpgradeInterrupted(signal.Signals(signum).name)


@contextmanager
def _signals_raise() -> Iterator[None]:
    previous = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _ignore_interrupts() -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _describe(error: BaseException) -> str:
    if isinstance(error, UpgradeInterrupted):
        return f"interrupted by {error}"
    return str(error) or type(error).__name__


class StepLog:
    """Per-step timing and status for the upgrade report."""

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.current: str | None = None

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        self.current = name
        started = _monotonic()
        try:
            yield
        except BaseException:
            self.rows.append(
                {"name": name, "status": "failed", "seconds": round(_monotonic() - started, 3)}
            )
            raise
        self.rows.append(
            {"name": name, "status": "passed", "seconds": round(_monotonic() - started, 3)}
        )


@dataclass
class _TransactionState:
    previously_active: list[str] = field(default_factory=list)
    apply_attempted: bool = False
    activation_attempted: bool = False
    receipt_id: str | None = None


def _candidate(sealed: SealedCandidate, *arguments: str) -> CompletedProcess[str]:
    """One sealed-candidate installer call; the sealed tree is re-attested first."""

    sealed.assert_unchanged()
    return _run(
        (str(sealed.cli), "install", "trust-root", *arguments),
        env={**_CANDIDATE_ENV, "PATH": f"{sealed.venv}/bin:/usr/bin:/bin"},
    )


def _candidate_json(sealed: SealedCandidate, step: str, *arguments: str) -> dict[str, object]:
    result = _candidate(sealed, *arguments)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise UpgradeError(f"{step} failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UpgradeError(f"{step} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise UpgradeError(f"{step} returned invalid JSON")
    return payload


def _advance(
    sealed: SealedCandidate,
    bound: BoundPlan,
    prior: PriorReceipt,
    token: str,
    report: dict[str, object],
    steps: StepLog,
    state: _TransactionState,
) -> None:
    receipt = str(bound.receipt_path)
    with steps.step("stop-services"):
        install_cli._stop_current_services()
    with steps.step("apply"):
        # Set before the child starts: a signal that lands after the child wrote
        # the receipt but before it returned must still roll that receipt back.
        state.apply_attempted = True
        # Likewise set before the child starts, with the id still unknown: a
        # halted or rolled-back report must still name the receipt holding any
        # retained state even when the child never returned (interrupted, or
        # exited non-zero after writing the file). The success assignment below
        # overwrites this with the real receipt_id once apply actually returns.
        report["receipt"] = {"path": receipt, "receipt_id": None}
        applied = _candidate_json(
            sealed,
            "apply",
            "apply",
            "--plan",
            str(bound.durable_path),
            "--confirm-sha256",
            bound.sha256,
            "--receipt",
            receipt,
            "--prior-receipt",
            str(prior.path),
            "--maintenance-token",
            token,
        )
        state.receipt_id = str(applied.get("receipt_id"))
        report["receipt"] = {"path": receipt, "receipt_id": state.receipt_id}
    with steps.step("credentials"):
        # Called exactly once per transaction attempt — never retried here.
        # `inherit_prior_credentials` (Task 2) refuses a second run against the
        # same receipt by design (`receipt already records credential
        # authority`), so a retry loop around this step would not recover;
        # recovery of a transaction that failed after this step belongs to
        # Task 10's `--recover`, not to a loop inside this function.
        inherited = _candidate_json(
            sealed,
            "credential inheritance",
            "credentials",
            "inherit",
            "--receipt",
            receipt,
            "--prior-receipt",
            str(prior.path),
            "--maintenance-token",
            token,
        )
        report["inherited_credentials"] = inherited.get("inherited", [])
    with steps.step("activate"):
        state.activation_attempted = True
        _candidate_json(
            sealed, "activate", "activate", "--receipt", receipt, "--maintenance-token", token
        )
    with steps.step("verify"):
        evidence = sealed.attempt_dir / "install-verification.json"
        report["verify_evidence"] = str(evidence)
        verified = _candidate(
            sealed,
            "verify",
            "--receipt",
            receipt,
            "--json",
            "--evidence",
            str(evidence),
            "--maintenance-token",
            token,
        )
        if verified.returncode != 0:
            raise UpgradeError("verify did not PASS")
        inactive = [
            service
            for service in install_cli._MAINTENANCE_SERVICES
            if install_cli._systemctl("is-active", "--quiet", service).returncode != 0
        ]
        if inactive:
            raise UpgradeError("services are not active after activation: " + ", ".join(inactive))
    with steps.step("loaded-runtime"):
        expected = runtime_expected({"receipt_id": state.receipt_id, "plan": bound.plan})
        mismatch, _payload = await_loaded_runtime(bound.plan, bound.receipt_path, expected)
        report["loaded_runtime"] = {"expected": expected, "mismatch": mismatch}
        if mismatch:
            raise UpgradeError(f"loaded runtime does not match the new receipt: {mismatch}")


def _abort(
    sealed: SealedCandidate,
    bound: BoundPlan,
    prior: PriorReceipt,
    token: str,
    report: dict[str, object],
    steps: StepLog,
    state: _TransactionState,
    error: BaseException,
    lifecycle: dict[str, bool],
) -> int:
    """Roll the new receipt back; restore services only when that is restore-safe.

    A post-activate rollback that proves ``restore_safe=true`` auto-restores the
    prior services below — the accepted design per spec §6/§7 and `--recover`
    (Task 10 reads the same contract on resume); `restore_safe=false` instead
    halts with services stopped and the maintenance snapshot kept, for the
    operator or `--recover` to resolve by hand.
    """

    _ignore_interrupts()
    report["failed_step"] = steps.current
    report["error"] = _describe(error)
    report["phase"] = "post-activate" if state.activation_attempted else "pre-activate"
    rollback: dict[str, object] = {
        "attempted": False,
        "restore_safe": True,
        "retained_unknown": [],
        "retained_drift": [],
    }
    report["rollback"] = rollback
    if state.apply_attempted and os.path.lexists(bound.receipt_path):
        rollback["attempted"] = True
        payload: object = None
        result: CompletedProcess[str] | None = None
        try:
            result = _candidate(
                sealed,
                "rollback",
                "--receipt",
                str(bound.receipt_path),
                "--maintenance-token",
                token,
            )
            payload = json.loads(result.stdout) if result.stdout.strip() else None
        except (InstallError, OSError, json.JSONDecodeError) as exc:
            rollback["error"] = _describe(exc)
        if result is not None and (result.returncode != 0 or not isinstance(payload, dict)):
            # The real installer always emits a JSON payload before returning,
            # even when restore_safe is false; empty/unparseable stdout here
            # means the child raised before that happened, and only its stderr
            # says why -- without this, that cause is silently discarded and
            # the operator is left with an unexplained "restore_safe: false".
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            rollback["error"] = (
                f"rollback exited {result.returncode}: {detail[:_ROLLBACK_ERROR_LIMIT]}"
            )
        if not isinstance(payload, dict):
            payload = {}
        rollback["restore_safe"] = payload.get("restore_safe") is True
        rollback["retained_unknown"] = list(payload.get("retained_unknown") or [])
        rollback["retained_drift"] = list(payload.get("retained_drift") or [])
    if rollback["restore_safe"] is not True:
        report["result"] = "halted"
        report["next_action"] = _HALTED_NEXT_ACTION
        return 1
    try:
        rollback["services_restored"] = install_cli._restore_snapshot_services(
            state.previously_active
        )
    except InstallError as exc:
        rollback["services_restored"] = []
        rollback["restore_error"] = str(exc)
        report["result"] = "halted"
        report["next_action"] = (
            "restoring the previous services failed; they were stopped again and the "
            "snapshot is kept: run `cortex upgrade --recover`"
        )
        return 1
    if state.previously_active:
        mismatch, status = await_loaded_runtime(
            prior.plan, prior.path, runtime_expected(prior.document)
        )
        rollback["prior_loaded_runtime"] = {"mismatch": mismatch, "service_status": status}
    else:
        mismatch = ""
        rollback["prior_loaded_runtime"] = {
            "mismatch": "",
            "service_status": None,
            "reason": "no-previously-active-services",
        }
    install_cli._clear_maintenance_snapshot(bound.plan, receipt_path=bound.receipt_path)
    lifecycle["complete"] = True
    report["result"] = "rolled-back"
    if mismatch:
        report["next_action"] = (
            "the previous services run again but do not match the current receipt; "
            "inspect `cortex service status --system`"
        )
    return 1


def run_transaction(
    sealed: SealedCandidate,
    bound: BoundPlan,
    prior: PriorReceipt,
    report: dict[str, object],
    steps: StepLog,
) -> int:
    """Lease → snapshot → stop → apply → inherit → activate → verify → loaded runtime."""

    state = _TransactionState()
    lifecycle = {"complete": False}
    with _signals_raise():
        with install_cli._maintenance_lease(bound.plan, lifecycle_state=lifecycle) as token:
            try:
                with steps.step("maintenance-snapshot"):
                    if os.path.lexists(bound.receipt_path):
                        raise UpgradeError(
                            "the new receipt path already exists; run `cortex upgrade --recover`"
                        )
                    present, previously_active = install_cli._service_snapshot()
                    state.previously_active = list(previously_active)
                    install_cli._write_maintenance_snapshot(
                        {
                            "schema_version": 1,
                            "plan_sha256": bound.sha256,
                            "receipt_path": str(bound.receipt_path),
                            "present_services": present,
                            "previously_active": previously_active,
                        }
                    )
            except BaseException:
                # Nothing was stopped yet; the lease marker may go with the lease.
                lifecycle["complete"] = True
                raise
            report["services_stopped"] = True
            try:
                _advance(sealed, bound, prior, token, report, steps, state)
            except BaseException as error:  # noqa: BLE001 — every failure rolls back
                return _abort(
                    sealed, bound, prior, token, report, steps, state, error, lifecycle
                )
            install_cli._clear_maintenance_snapshot(bound.plan, receipt_path=bound.receipt_path)
            lifecycle["complete"] = True
    report["result"] = "upgraded"
    return 0


def _fetcher(options: UpgradeOptions) -> ReleaseFetcher:
    if options.release_source is not None:
        return DirectoryReleaseFetcher(options.release_source)
    return GitHubReleaseFetcher(OFFICIAL_REPOSITORY)


def _report_path(version: str) -> Path:
    return install_cli._TRUST_ROOT_MAINTENANCE_ROOT / version / _REPORT_NAME


def _publish_report(report: Mapping[str, object]) -> None:
    root = install_cli._TRUST_ROOT_MAINTENANCE_ROOT
    atomic_write_json(_report_path(str(report["version"])), report, mode=0o600)
    atomic_write_json(root / _LAST_REPORT_NAME, report, mode=0o600)


def _new_report(version: str, prior: PriorReceipt, steps: StepLog) -> dict[str, object]:
    return {
        "schema_version": 1,
        "version": version,
        # Closed set, written only by this module and by Task 10's `--recover`:
        # "upgraded" | "rolled-back" | "halted" | "refused" | "in-progress" |
        # "recovered" (the last is `--recover`'s own terminal outcome; keep this
        # list in sync with whichever module next widens it).
        "result": None,
        "phase": None,
        "failed_step": None,
        "error": None,
        "started_at": _now(),
        "finished_at": None,
        "prior_receipt": {
            "path": str(prior.path),
            "receipt_id": prior.document.get("receipt_id"),
            "version": format_version(prior.version),
        },
        "candidate": None,
        "plan": None,
        "receipt": None,
        "inherited_credentials": [],
        "verify_evidence": None,
        "loaded_runtime": None,
        "rollback": None,
        "services_stopped": False,
        "next_action": None,
        "steps": steps.rows,
        "report_path": str(_report_path(version)),
    }


def _print_outcome(report: Mapping[str, object], *, json_output: bool) -> None:
    if json_output:
        sys.stdout.write(
            json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        )
        return
    lines = [f"cortex upgrade {report['version']}: {report['result']}"]
    if report.get("failed_step"):
        lines.append(f"  failed step:   {report['failed_step']} ({report.get('error')})")
    prior = report.get("prior_receipt")
    if isinstance(prior, Mapping):
        lines.append(f"  prior receipt: {prior.get('path')} ({prior.get('version')})")
    receipt = report.get("receipt")
    if isinstance(receipt, Mapping):
        lines.append(f"  new receipt:   {receipt.get('path')}")
    plan = report.get("plan")
    if isinstance(plan, Mapping):
        lines.append(f"  plan sha256:   {plan.get('sha256')}")
    if report.get("verify_evidence"):
        lines.append(f"  verify:        {report['verify_evidence']}")
    rollback = report.get("rollback")
    if isinstance(rollback, Mapping):
        lines.append(f"  rollback:      restore_safe={rollback.get('restore_safe')}")
        if rollback.get("error"):
            lines.append(f"  rollback error: {rollback['error']}")
        for key in ("retained_unknown", "retained_drift"):
            for row in rollback.get(key) or []:
                lines.append(f"    {key}: {json.dumps(row, ensure_ascii=False, sort_keys=True)}")
        if "services_restored" in rollback:
            restored = rollback.get("services_restored") or []
            lines.append(
                "  services restored: " + (", ".join(str(name) for name in restored) or "none")
            )
    if report.get("next_action"):
        lines.append(f"  next:          {report['next_action']}")
    lines.append(f"  report:        {report.get('report_path')}")
    sys.stdout.write("\n".join(lines) + "\n")


def perform_upgrade(options: UpgradeOptions) -> int:
    """Spec §4 end to end; every outcome after preflight lands in the report."""

    previous_umask = os.umask(0o077)
    try:
        _pin_import_paths()
        with install_cli._host_lock(
            leaf="upgrade.lock", conflict="another `cortex upgrade` is already running"
        ):
            checked = preflight(options)
            version = format_version(checked.target)
            steps = StepLog()
            report = _new_report(version, checked.prior, steps)
            code = 1
            try:
                with steps.step("ingress"):
                    sealed = ingest_release(
                        version,
                        fetcher=_fetcher(options),
                        installer_root=install_cli._TRUST_ROOT_MAINTENANCE_ROOT,
                        owner_uid=_OWNER_UID,
                        chain_stop=_CHAIN_STOP,
                    )
                report["candidate"] = {
                    "tag": sealed.metadata.tag,
                    "commit": sealed.metadata.commit,
                    "assets": {
                        asset.name: asset.sha256
                        for asset in (
                            sealed.metadata.wheel,
                            sealed.metadata.install_input,
                            sealed.metadata.qualification,
                        )
                    },
                    "attempt_dir": str(sealed.attempt_dir),
                    "cli_tree_sha256": sealed.tree_sha256,
                }
                with steps.step("plan"):
                    bound = produce_plan(sealed, checked.prior, options=options)
                report["plan"] = {
                    "sha256": bound.sha256,
                    "durable_path": str(bound.durable_path),
                    "host_overlay_sha256": bound.overlay_sha256,
                }
                report["result"] = "in-progress"
                _publish_report(report)
                code = run_transaction(sealed, bound, checked.prior, report, steps)
            except BaseException as error:  # noqa: BLE001 — every outcome is reported
                if report["result"] in (None, "in-progress"):
                    stopped = report["services_stopped"] is True
                    report["result"] = "halted" if stopped else "refused"
                    report["failed_step"] = steps.current
                    report["error"] = _describe(error)
                    if stopped:
                        report["next_action"] = (
                            "the upgrade stopped inside the maintenance window; "
                            "run `cortex upgrade --recover`"
                        )
                code = 1
            finally:
                report["finished_at"] = _now()
                _publish_report(report)
            _print_outcome(report, json_output=options.json_output)
            return code
    finally:
        os.umask(previous_umask)


def run_upgrade(options: UpgradeOptions) -> int:
    return perform_upgrade(options)
