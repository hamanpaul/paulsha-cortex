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
