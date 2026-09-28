"""Read-only legacy inventory for trust-root legacy adoption (#1122).

A Phase 2b host was deployed by hand and never produced an installer receipt,
so the transactional installer can neither fresh-install over it nor upgrade it
with ``--prior-receipt``.  Legacy adoption adds a third kind of provenance: a
root-captured, operator-reviewed inventory whose digest a later plan binds.
The capture side (PR-2) never mutates the host.  The planning side (PR-3, at
the end of this module) gives ``legacy_policy`` its meaning, records the host
overlay digest, binds an inventory to a plan and derives one disposition per
inventoried object, emitting ``legacy-quarantine`` steps.  Apply refuses those
plans until apply-time re-capture, the quarantine backend and rollback land.

Inventory schema v1 (canonical JSON, ASCII, sorted keys, one trailing newline):

``inventory_sha256``
    Self digest over the *stable* projection: every top-level field except
    ``inventory_sha256``, ``collector`` and ``volatile``.  ``volatile`` carries
    what legitimately moves while a legacy service keeps running (capture time,
    hostname, ``active_state``, in-flight counts, census and state counts,
    owner census, directory entry counts); apply re-computes those as gates
    instead of requiring equality.
``host.binding_sha256``
    Domain-separated SHA-256 of ``/etc/machine-id``.  The raw identity is never
    written.
``scope`` / ``scope_sha256``
    What the inventory covers, derived only from the effective config (config
    plus host overlay) and bundle: roots, account names and homes, the plan's
    managed path set, credential destinations, discovery rules and the census
    rules.  A plan re-derives it to refuse an inventory captured for another
    config.
``host_overlay``
    Digest and keys of the host overlay *without* its ``legacy_adoption``
    block: that block names this inventory's digest, so it cannot feed it.

Credential-class objects (adapter destinations, ``*.env``, ``*oauth*`` and
similar names, credential directories) are recorded by type, owner, mode and
inode identity only; their contents are never opened.  Regular files above
:data:`CONTENT_HASH_MAX_BYTES` and symlinks are recorded by metadata / target.
"""
from __future__ import annotations

import ctypes
import errno
import fnmatch
import functools
import grp
import hashlib
import importlib.metadata
import json
import os
import platform
import posixpath
import pwd
import re
import socket
import stat
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Mapping, Protocol, Sequence

from .backend import (
    _durable_in_flight_job_count,
    _in_flight_process_count,
    _password_locked,
    _read_acl,
    _run,
)
from .core import (
    ASSET_PRIOR_SNAPSHOT_MAX_BYTES,
    InstallError,
    InstallPlanError,
    UnsafeInstallPathError,
    _DIRECTORY_OPEN_FLAGS,
    _PRINCIPAL_ACCOUNTS,
    _PROVIDER_ALLOWLIST,
    _assert_managed_parent_topology,
    _credential_adapter_for,
    _open_directory_chain,
    _reject_sensitive_config,
    _reject_symlink_ancestors,
    _rename_noreplace_at,
    _write_all,
    canonical_receipt_path,
)


INVENTORY_SCHEMA_VERSION = 1
INVENTORY_KIND = "paulsha-cortex/trust-root-legacy-inventory"
SCOPE_SCHEMA_VERSION = 1

#: Content is hashed only up to the bound a managed file's rollback snapshot
#: may hold; anything larger (legacy toolchain binaries) is metadata only.
CONTENT_HASH_MAX_BYTES = ASSET_PRIOR_SNAPSHOT_MAX_BYTES
INVENTORY_MAX_BYTES = 256 * 1024 * 1024

HOST_OVERLAY_KEYS = frozenset(
    {
        "accounts",
        "service_accounts",
        "operator_account",
        "external_reader_account",
        "providers",
        "legacy_adoption",
    }
)
_OVERLAY_ACCOUNT_KEYS = frozenset({"uid", "gid"})
_OVERLAY_SERVICE_ACCOUNT = "cortex-egress"
_OVERLAY_SERVICE_ACCOUNT_KEYS = frozenset({"uid", "gid", "home"})
_OVERLAY_PROVIDER_KEYS = frozenset({"builder"})

_HOST_BINDING_DOMAIN = b"paulsha-cortex/trust-root/legacy-host-binding/v1\n"
_MANAGER_ACCOUNT = _PRINCIPAL_ACCOUNTS["manager"]
_JOB_SEGMENT = "<job-id>"
_TRIE_TERMINAL = "\x00"

#: Leftovers inside a managed directory that a later plan quarantines.
RESIDUE_PATTERNS = (
    "*.tmp",
    "*.tmp.*",
    "*.bak",
    "*.bak-*",
    "*.bak.*",
    "*.orig",
    "*.rej",
    "*.swp",
    "*~",
)
#: Basenames (case-insensitive) whose contents are never read.
CREDENTIAL_NAME_PATTERNS = (
    "*.env",
    "*.env.*",
    "*oauth*",
    "*token*",
    "*secret*",
    "*credential*",
    "*password*",
    "*passwd*",
    "auth.json",
    "hosts.yml",
    "hosts.json",
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ecdsa*",
    "id_ed25519*",
)
#: Any path containing one of these components is credential class.
CREDENTIAL_DIRECTORY_NAMES = (
    ".copilot",
    "github-copilot",
    ".ssh",
    ".gnupg",
    ".docker",
)

DISCOVERY_RULES = (
    "authority:units",
    "authority:polkit",
    "authority:shim",
    "authority:toolchain_wrappers",
    "authority:environment",
    "systemd-dropin",
    "systemd-wants",
    "env-dir",
    "deploy-top",
    "toolchain-bin",
    "home-top",
    "state-top",
    "managed-subdir",
    "managed-residue",
)
_AUTHORITY_CATEGORIES = ("units", "polkit", "shim", "toolchain_wrappers", "environment")
_AUTHORITY_RULES = frozenset(
    {*(f"authority:{name}" for name in _AUTHORITY_CATEGORIES), "systemd-dropin", "systemd-wants"}
)

_FILE_TYPES = (
    (stat.S_ISREG, "file"),
    (stat.S_ISDIR, "directory"),
    (stat.S_ISLNK, "symlink"),
    (stat.S_ISFIFO, "fifo"),
    (stat.S_ISSOCK, "socket"),
    (stat.S_ISBLK, "block-device"),
    (stat.S_ISCHR, "char-device"),
)
_KNOWN_FILE_TYPES = frozenset(name for _test, name in _FILE_TYPES) | {"unknown"}
_CAPTURE_ATTEMPTS = 3
_VANISHED_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})

#: Where the design publishes inventories: installer-owned, outside every root
#: and managed path the inventory records.
INSTALLER_LEGACY_DIRECTORY = "/var/lib/cortex-installer/legacy/"

_AT_SYMLINK_NOFOLLOW = 0x100
#: ``faccessat2`` joined the unified syscall table in Linux 5.8.
_FACCESSAT2_SYSCALL = {
    "x86_64": 439,
    "aarch64": 439,
    "arm64": 439,
    "armv7l": 439,
    "armv8l": 439,
    "i386": 439,
    "i686": 439,
    "riscv64": 439,
    "s390x": 439,
    "ppc64le": 439,
    "loongarch64": 439,
}
_NOFOLLOW_ACCESS_UNSUPPORTED = frozenset(
    {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
)
_CENSUS_EXIT_UNSUPPORTED = 5
_CENSUS_EXIT_IDENTITY = 4
_PATH_DIRECTORY_FLAGS = (
    getattr(os, "O_PATH", os.O_RDONLY)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class InventoryRaceError(InstallError):
    """An object changed between its ``lstat`` and the read that describes it."""


# ---------------------------------------------------------------------------
# canonical encoding and digests
# ---------------------------------------------------------------------------


def canonical_inventory_bytes(document: Mapping[str, object]) -> bytes:
    """ASCII canonical JSON; ``ensure_ascii`` keeps non-UTF-8 names encodable."""

    return (
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_inventory_bytes(value)).hexdigest()  # type: ignore[arg-type]


_UNSTABLE_TOP_LEVEL_KEYS = frozenset({"inventory_sha256", "collector", "volatile"})


def inventory_stable_sha256(document: Mapping[str, object]) -> str:
    """Digest of every field that must be equal when the inventory is re-captured."""

    return _digest(
        {
            key: value
            for key, value in document.items()
            if key not in _UNSTABLE_TOP_LEVEL_KEYS
        }
    )


def scope_sha256(scope: Mapping[str, object]) -> str:
    return _digest(scope)


def host_binding_sha256(machine_id: str) -> str:
    """Bind a host without emitting its machine identity."""

    value = machine_id.strip()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise InstallError("machine-id is not a 32-hex systemd machine identity")
    return hashlib.sha256(_HOST_BINDING_DOMAIN + value.encode("ascii")).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


# ---------------------------------------------------------------------------
# host overlay
# ---------------------------------------------------------------------------


def _dotted_leaves(prefix: str, value: object) -> list[str]:
    if isinstance(value, Mapping) and value:
        leaves: list[str] = []
        for key, child in value.items():
            leaves.extend(_dotted_leaves(f"{prefix}.{key}", child))
        return leaves
    return [prefix]


def _positive_id(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise InstallPlanError(f"host overlay {label} must be a positive integer")
    return value


def validate_host_overlay(overlay: object) -> dict[str, object]:
    """Validate an explicit host delta against the allowlist.

    Only account/group ids, the egress service account home, the operator and
    external reader accounts, the builder provider list and the
    ``legacy_adoption`` block may differ from the release install config.  Any
    other key fails and is named, so a host cannot silently re-root or re-shape
    the release-sealed config.
    """

    if overlay is None:
        return {}
    if not isinstance(overlay, Mapping) or not all(
        isinstance(key, str) for key in overlay
    ):
        raise InstallPlanError("host overlay must be an object with string keys")
    disallowed: list[str] = []
    for key, value in overlay.items():
        if key not in HOST_OVERLAY_KEYS:
            disallowed.extend(_dotted_leaves(key, value))
            continue
        if key == "accounts":
            if not isinstance(value, Mapping):
                raise InstallPlanError("host overlay accounts must be an object")
            for name, row in value.items():
                if not isinstance(name, str) or not name:
                    raise InstallPlanError("host overlay account names must be strings")
                if not isinstance(row, Mapping) or not row:
                    raise InstallPlanError(
                        f"host overlay accounts.{name} must be a non-empty object"
                    )
                for field, child in row.items():
                    if field not in _OVERLAY_ACCOUNT_KEYS:
                        disallowed.extend(_dotted_leaves(f"accounts.{name}.{field}", child))
        elif key == "service_accounts":
            if not isinstance(value, Mapping):
                raise InstallPlanError("host overlay service_accounts must be an object")
            for name, row in value.items():
                if name != _OVERLAY_SERVICE_ACCOUNT:
                    disallowed.extend(_dotted_leaves(f"service_accounts.{name}", row))
                    continue
                if not isinstance(row, Mapping) or not row:
                    raise InstallPlanError(
                        f"host overlay service_accounts.{name} must be a non-empty object"
                    )
                for field, child in row.items():
                    if field not in _OVERLAY_SERVICE_ACCOUNT_KEYS:
                        disallowed.extend(
                            _dotted_leaves(f"service_accounts.{name}.{field}", child)
                        )
        elif key == "providers":
            if not isinstance(value, Mapping):
                raise InstallPlanError("host overlay providers must be an object")
            for field, child in value.items():
                if field not in _OVERLAY_PROVIDER_KEYS:
                    disallowed.extend(_dotted_leaves(f"providers.{field}", child))
    if disallowed:
        raise InstallPlanError(
            "host overlay key is not allowed: " + ", ".join(sorted(disallowed))
        )

    for name, row in (overlay.get("accounts") or {}).items():
        for field, child in row.items():
            _positive_id(child, label=f"accounts.{name}.{field}")
    for name, row in (overlay.get("service_accounts") or {}).items():
        for field, child in row.items():
            if field == "home":
                if (
                    not isinstance(child, str)
                    or not PurePosixPath(child).is_absolute()
                    or ".." in PurePosixPath(child).parts
                ):
                    raise InstallPlanError(
                        f"host overlay service_accounts.{name}.home must be an absolute path"
                    )
            else:
                _positive_id(child, label=f"service_accounts.{name}.{field}")
    for field in ("operator_account", "external_reader_account"):
        if field in overlay and (
            not isinstance(overlay[field], str) or not overlay[field]
        ):
            raise InstallPlanError(f"host overlay {field} must be a non-empty string")
    providers = overlay.get("providers")
    if isinstance(providers, Mapping) and "builder" in providers:
        builder = providers["builder"]
        if (
            type(builder) is not list
            or not builder
            or any(type(item) is not str for item in builder)
            or len(builder) != len(set(builder))
            or any(item not in _PROVIDER_ALLOWLIST["builder"] for item in builder)
        ):
            raise InstallPlanError(
                "host overlay providers.builder must be a non-empty list of allowed builder providers"
            )
    if "legacy_adoption" in overlay and not isinstance(
        overlay["legacy_adoption"], Mapping
    ):
        # The block's own schema belongs to planning; the inventory only
        # needs to know it is not part of the effective config.
        raise InstallPlanError("host overlay legacy_adoption must be an object")
    _reject_sensitive_config(overlay, subject="host overlay")
    return deepcopy(dict(overlay))


def apply_host_overlay(
    config: Mapping[str, object], overlay: Mapping[str, object] | None
) -> dict[str, object]:
    """Return the effective install config; ``legacy_adoption`` stays outside it."""

    validated = validate_host_overlay(overlay)
    effective = deepcopy(dict(config))
    accounts = effective.get("accounts")
    for name, row in (validated.get("accounts") or {}).items():
        if not isinstance(accounts, dict) or not isinstance(accounts.get(name), Mapping):
            raise InstallPlanError(f"host overlay account is not in the install config: {name}")
        accounts[name] = {**dict(accounts[name]), **dict(row)}
    services = effective.get("service_accounts")
    for name, row in (validated.get("service_accounts") or {}).items():
        if not isinstance(services, dict) or not isinstance(services.get(name), Mapping):
            raise InstallPlanError(
                f"host overlay service account is not in the install config: {name}"
            )
        services[name] = {**dict(services[name]), **dict(row)}
    for field in ("operator_account", "external_reader_account"):
        if field in validated:
            effective[field] = validated[field]
    providers = validated.get("providers")
    if isinstance(providers, Mapping) and "builder" in providers:
        current = effective.get("providers")
        if not isinstance(current, Mapping):
            raise InstallPlanError("install config providers must be an object")
        effective["providers"] = {**dict(current), "builder": list(providers["builder"])}
    return effective


def host_overlay_record(overlay: Mapping[str, object] | None) -> dict[str, object] | None:
    """Digest and keys of the overlay minus ``legacy_adoption`` (``None`` when empty)."""

    validated = validate_host_overlay(overlay)
    projection = {key: value for key, value in validated.items() if key != "legacy_adoption"}
    if not projection:
        return None
    keys: list[str] = []
    for key, value in projection.items():
        keys.extend(_dotted_leaves(key, value))
    return {"sha256": _digest(projection), "keys": sorted(keys)}


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


def _plan_roots(plan: Mapping[str, object]) -> dict[str, str]:
    roots = plan.get("roots")
    if not isinstance(roots, Mapping):
        raise InstallPlanError("plan roots are invalid")
    result: dict[str, str] = {}
    for name in ("deploy", "state", "systemd", "polkit"):
        value = roots.get(name)
        if not isinstance(value, str) or not PurePosixPath(value).is_absolute():
            raise InstallPlanError(f"plan root {name} is invalid")
        result[name] = value
    return result


def _plan_account_rows(plan: Mapping[str, object]) -> list[tuple[str, Mapping[str, object]]]:
    rows: list[tuple[str, Mapping[str, object]]] = []
    for field, kind in (("accounts", "principal"), ("service_accounts", "service")):
        values = plan.get(field)
        if not isinstance(values, list):
            raise InstallPlanError(f"plan {field} is invalid")
        for row in values:
            if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
                raise InstallPlanError(f"plan {field} rows must be typed objects")
            rows.append((kind, row))
    return sorted(rows, key=lambda item: str(item[1]["name"]))


def _managed_steps(plan: Mapping[str, object]) -> list[dict[str, object]]:
    """Every host path the plan declares, one row per path."""

    steps = plan.get("apply_order")
    if not isinstance(steps, list):
        raise InstallPlanError("plan apply_order is invalid")
    rows: dict[str, dict[str, object]] = {}
    for step in steps:
        if not isinstance(step, Mapping):
            raise InstallPlanError("apply_order entries must be typed objects")
        if step.get("kind") == QUARANTINE_STEP_KIND:
            # A quarantine step moves a legacy object away; it declares no
            # desired path, so the scope of a bound plan equals the plan's.
            continue
        candidates: list[tuple[str, str, str]] = []
        path = step.get("path")
        if isinstance(path, str):
            candidates.append((path, str(step.get("step_id")), str(step.get("kind"))))
        link = step.get("active_link")
        if step.get("kind") == "venv" and isinstance(link, str):
            candidates.append((link, f"{step.get('step_id')}:active_link", "venv-link"))
        for candidate, step_id, kind in candidates:
            if not PurePosixPath(candidate).is_absolute():
                raise InstallPlanError(f"managed path is not absolute: {candidate}")
            if candidate in rows:
                raise InstallPlanError(f"plan declares one path twice: {candidate}")
            asset_type = step.get("asset_type") if kind == "asset" else None
            rows[candidate] = {
                "path": candidate,
                "step_id": step_id,
                "kind": kind,
                "asset_type": asset_type if isinstance(asset_type, str) else None,
            }
    return [rows[path] for path in sorted(rows)]


def credential_destinations(plan: Mapping[str, object]) -> list[dict[str, object]]:
    """Every adapter destination for every allowed principal/provider pair."""

    homes = {
        str(row["name"]): str(row.get("home"))
        for _kind, row in _plan_account_rows(plan)
    }
    configured = plan.get("provider_manifest")
    configured = configured if isinstance(configured, Mapping) else {}
    rows: list[dict[str, object]] = []
    for principal in sorted(_PROVIDER_ALLOWLIST):
        account = _PRINCIPAL_ACCOUNTS[principal]
        home = homes.get(account)
        if home is None:
            raise InstallPlanError(f"plan lacks the account for principal {principal}")
        providers = configured.get(principal)
        providers = providers if isinstance(providers, list) else []
        for provider in sorted(_PROVIDER_ALLOWLIST[principal]):
            adapter = _credential_adapter_for(principal, provider)
            if adapter is None:
                continue
            rows.append(
                {
                    "principal": principal,
                    "provider": provider,
                    "path": str(PurePosixPath(home).joinpath(*adapter.destination_parts)),
                    "configured": provider in providers,
                }
            )
    return rows


def _generated_parents(plan: Mapping[str, object]) -> dict[str, list[str]]:
    generated = plan.get("generated")
    if not isinstance(generated, Mapping):
        raise InstallPlanError("plan generated inventory is invalid")
    parents: dict[str, list[str]] = {}
    for category in _AUTHORITY_CATEGORIES:
        rows = generated.get(category)
        if not isinstance(rows, Mapping):
            parents[category] = []
            continue
        parents[category] = sorted(
            {
                posixpath.dirname(str(row.get("path")))
                for row in rows.values()
                if isinstance(row, Mapping) and isinstance(row.get("path"), str)
            }
        )
    return parents


def _env_dir(plan: Mapping[str, object], roots: Mapping[str, str]) -> str:
    parents = _generated_parents(plan)["environment"]
    return parents[0] if len(parents) == 1 else f"{roots['deploy']}/etc"


def _toolchain_bin(plan: Mapping[str, object], roots: Mapping[str, str]) -> str:
    parents = _generated_parents(plan)["toolchain_wrappers"]
    return parents[0] if len(parents) == 1 else f"{roots['deploy']}/toolchain/bin"


def _census_principals(plan: Mapping[str, object]) -> list[str]:
    return [
        str(row["name"])
        for _kind, row in _plan_account_rows(plan)
        if row["name"] != _MANAGER_ACCOUNT
    ]


def _minimal_roots(candidates: Sequence[str]) -> list[str]:
    ordered = sorted({posixpath.normpath(path) for path in candidates})
    kept: list[str] = []
    for path in ordered:
        if any(path == root or path.startswith(root.rstrip("/") + "/") for root in kept):
            continue
        kept.append(path)
    return kept


def _census_roots(plan: Mapping[str, object]) -> list[str]:
    roots = _plan_roots(plan)
    homes = [str(row.get("home")) for _kind, row in _plan_account_rows(plan)]
    return _minimal_roots(
        [roots["deploy"], roots["state"], roots["systemd"], roots["polkit"], *homes]
    )


def _mode_bits(value: object) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-7]{3,4}", value):
        return int(value, 8)
    return None


def _step_grants_write(step: Mapping[str, object], account: str) -> bool:
    mode = _mode_bits(step.get("mode"))
    if mode is not None:
        if step.get("owner") == account and mode & 0o200:
            return True
        if step.get("group") == account and mode & 0o020:
            return True
        if mode & 0o002:
            return True
    for row in step.get("acls", []) or []:
        if (
            isinstance(row, Mapping)
            and row.get("account") == account
            and "w" in str(row.get("perms", "")).lower()
        ):
            return True
    return False


def declared_writable_patterns(plan: Mapping[str, object], account: str) -> list[str]:
    """Paths the plan intends ``account`` to write; ``<job-id>`` is one segment.

    A path is declared for the account when it equals or lies below one of
    these: the permgen writer set of an asset, or a managed directory/file whose
    desired owner, group, mode or ACL grants the account write.
    """

    patterns: set[str] = set()
    assets = plan.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, Mapping):
                continue
            path = asset.get("path")
            writers = asset.get("writer_accounts")
            if (
                isinstance(path, str)
                and PurePosixPath(path).is_absolute()
                and isinstance(writers, list)
                and account in writers
            ):
                patterns.add(path)
    for step in plan.get("apply_order", []) or []:
        if (
            isinstance(step, Mapping)
            and step.get("kind") == "asset"
            and step.get("asset_type") in {"directory", "file"}
            and isinstance(step.get("path"), str)
            and _step_grants_write(step, account)
        ):
            patterns.add(str(step["path"]))
    return sorted(patterns)


def legacy_scope(plan: Mapping[str, object]) -> dict[str, object]:
    """Everything the inventory covers, as a pure function of the plan."""

    roots = _plan_roots(plan)
    accounts = [
        {"name": str(row["name"]), "kind": kind, "home": str(row.get("home"))}
        for kind, row in _plan_account_rows(plan)
    ]
    homes = sorted({row["home"] for row in accounts})
    parents = _generated_parents(plan)
    authority_match = {
        "units": "name-prefix:cortex",
        "polkit": "name-contains:cortex",
        "shim": "name-prefix:cortex",
        "toolchain_wrappers": "all",
        "environment": "suffix:.env|name-prefix:cortex",
    }
    rules = [
        {"rule": f"authority:{category}", "parents": parents[category], "match": authority_match[category]}
        for category in _AUTHORITY_CATEGORIES
    ]
    rules.extend(
        [
            {
                "rule": "systemd-dropin",
                "parents": [roots["systemd"]],
                "match": "children-of-cortex-directories",
            },
            {
                "rule": "systemd-wants",
                "parents": [roots["systemd"]],
                "match": "name-prefix:cortex-in-.wants|.requires",
            },
            {"rule": "env-dir", "parents": [_env_dir(plan, roots)], "match": "all"},
            {"rule": "deploy-top", "parents": [roots["deploy"]], "match": "all"},
            {"rule": "toolchain-bin", "parents": [_toolchain_bin(plan, roots)], "match": "all"},
            {"rule": "home-top", "parents": homes, "match": "all"},
            {"rule": "state-top", "parents": [roots["state"]], "match": "all"},
            {
                "rule": "managed-subdir",
                "parents": ["<managed-directories>"],
                "match": "unmanaged-directory",
            },
            {
                "rule": "managed-residue",
                "parents": ["<managed-directories>"],
                "match": "residue-patterns",
            },
        ]
    )
    principals = _census_principals(plan)
    return {
        "schema_version": SCOPE_SCHEMA_VERSION,
        "instance": str(plan.get("instance", "cortex")),
        "roots": roots,
        "accounts": accounts,
        "managed_paths": [
            {"path": row["path"], "step_id": row["step_id"]}
            for row in _managed_steps(plan)
        ],
        "credential_destinations": credential_destinations(plan),
        "discovery": {
            "rules": rules,
            "residue_patterns": list(RESIDUE_PATTERNS),
            "credential_name_patterns": list(CREDENTIAL_NAME_PATTERNS),
            "credential_directory_names": list(CREDENTIAL_DIRECTORY_NAMES),
            "content_hash_max_bytes": CONTENT_HASH_MAX_BYTES,
        },
        "census": {
            "principals": principals,
            "roots": _census_roots(plan),
            "excludes": ["symlink"],
            "declared_writable": {
                name: declared_writable_patterns(plan, name) for name in principals
            },
        },
    }


# ---------------------------------------------------------------------------
# backend seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PasswdEntry:
    name: str
    uid: int
    gid: int
    home: str
    shell: str


@dataclass(frozen=True)
class GroupEntry:
    name: str
    gid: int
    members: tuple[str, ...]


@dataclass(frozen=True)
class CensusIdentity:
    """The exact kernel identity a job account runs with."""

    uid: int
    gid: int
    groups: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return {"uid": self.uid, "gid": self.gid, "groups": list(self.groups)}


@dataclass(frozen=True)
class CensusRoot:
    """A census root and the identity of the directory it is reached from."""

    path: str
    dev: int
    ino: int
    is_dir: bool
    parent: str
    parent_dev: int
    parent_ino: int


@dataclass
class CensusTree:
    """Root-enumerated census input: every non-symlink object with its inode.

    ``entries`` maps a directory to its non-symlink children
    ``(name, dev, ino, is_dir)``.  The census re-walks this tree as the job
    account, entry by entry relative to a verified directory descriptor.
    """

    roots: list[CensusRoot]
    entries: dict[str, list[tuple[str, int, int, bool]]]
    count: int = 0

    def paths(self) -> Iterator[str]:
        for root in self.roots:
            yield root.path
            if not root.is_dir:
                continue
            stack = [root.path]
            while stack:
                directory = stack.pop()
                for name, _dev, _ino, is_dir in self.entries.get(directory, ()):
                    child = _join(directory, name)
                    yield child
                    if is_dir:
                        stack.append(child)


@dataclass(frozen=True)
class CensusResult:
    """Paths the identity may write, and paths that changed under the census."""

    writable: tuple[str, ...]
    unstable: tuple[str, ...]


class LegacyHostBackend(Protocol):
    """Read-only host observations; a fake replaces it in tests."""

    def machine_id(self) -> str: ...
    def hostname(self) -> str: ...
    def passwd_entries(self) -> Sequence[PasswdEntry]: ...
    def group_entries(self) -> Sequence[GroupEntry]: ...
    def group_list(self, name: str, gid: int) -> Sequence[int]: ...
    def password_locked(self, name: str) -> bool | None: ...
    def lstat(self, path: str) -> os.stat_result | None: ...
    def is_mountpoint(self, path: str) -> bool: ...
    def list_directory(self, path: str, observed: os.stat_result) -> Sequence[str]: ...
    def readlink(self, path: str) -> str: ...
    def read_acl(self, path: str) -> Sequence[Mapping[str, object]]: ...
    def sha256_file(self, path: str, observed: os.stat_result) -> str: ...
    def walk(self, root: str) -> Iterator[tuple[str, os.stat_result]]: ...
    def service_status(self, unit: str) -> Mapping[str, object]: ...
    def in_flight(self, job_uids: Mapping[str, int], plan: Mapping[str, object]) -> Mapping[str, object]: ...
    def writable_paths(
        self, identity: CensusIdentity, tree: CensusTree
    ) -> CensusResult: ...


_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_SYSTEMCTL_PROPERTIES = (
    "LoadState",
    "UnitFileState",
    "FragmentPath",
    "DropInPaths",
    "User",
    "ExecStart",
    "ActiveState",
    "SubState",
)


def _join(parent: str, name: str) -> str:
    return f"/{name}" if parent == "/" else f"{parent}/{name}"


class LocalLegacyHostBackend:
    """Real read-only host backend; construction enforces the root boundary."""

    def __init__(self, *, require_root: bool = True) -> None:
        if require_root and os.geteuid() != 0:
            raise PermissionError("legacy inventory requires root")

    def machine_id(self) -> str:
        try:
            raw = Path("/etc/machine-id").read_bytes()[:128]
        except OSError as exc:
            raise InstallError(f"cannot read the host machine identity: {exc}") from exc
        return raw.decode("ascii", errors="replace").strip()

    def hostname(self) -> str:
        return socket.gethostname()

    def passwd_entries(self) -> Sequence[PasswdEntry]:
        return tuple(
            PasswdEntry(row.pw_name, row.pw_uid, row.pw_gid, row.pw_dir, row.pw_shell)
            for row in pwd.getpwall()
        )

    def group_entries(self) -> Sequence[GroupEntry]:
        return tuple(
            GroupEntry(row.gr_name, row.gr_gid, tuple(row.gr_mem)) for row in grp.getgrall()
        )

    def group_list(self, name: str, gid: int) -> Sequence[int]:
        return tuple(os.getgrouplist(name, gid))

    def password_locked(self, name: str) -> bool | None:
        return _password_locked(name)

    def lstat(self, path: str) -> os.stat_result | None:
        try:
            return os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None

    def is_mountpoint(self, path: str) -> bool:
        return os.path.ismount(path)

    def _open_directory(self, path: str, observed: os.stat_result) -> int:
        descriptor = os.open(path, _DIRECTORY_OPEN_FLAGS)
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino):
            os.close(descriptor)
            raise InventoryRaceError(f"directory changed while it was inventoried: {path}")
        return descriptor

    def list_directory(self, path: str, observed: os.stat_result) -> Sequence[str]:
        descriptor = self._open_directory(path, observed)
        try:
            return sorted(os.listdir(descriptor))
        finally:
            os.close(descriptor)

    def readlink(self, path: str) -> str:
        return os.readlink(path)

    def read_acl(self, path: str) -> Sequence[Mapping[str, object]]:
        return _read_acl(Path(path))

    def sha256_file(self, path: str, observed: os.stat_result) -> str:
        descriptor = os.open(path, _READ_FLAGS)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino) != (observed.st_dev, observed.st_ino)
                or before.st_size != observed.st_size
                or before.st_size > CONTENT_HASH_MAX_BYTES
            ):
                raise InventoryRaceError(f"file changed while it was inventoried: {path}")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > CONTENT_HASH_MAX_BYTES:
                    raise InventoryRaceError(f"file grew while it was inventoried: {path}")
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (total, after.st_size, after.st_mtime_ns) != (
                before.st_size,
                before.st_size,
                before.st_mtime_ns,
            ):
                # The recorded size and digest must describe the same bytes.
                raise InventoryRaceError(f"file changed while it was inventoried: {path}")
            return digest.hexdigest()
        finally:
            os.close(descriptor)

    def walk(self, root: str) -> Iterator[tuple[str, os.stat_result]]:
        """Yield ``root`` and every descendant without following a symlink.

        Directories are opened relative to their already-open parent with
        ``O_NOFOLLOW`` and checked against the inode that was listed, so a
        job account swapping a directory for a symlink cannot redirect the
        walk outside the tree.  Only the current directory chain stays open.
        """

        observed = self.lstat(root)
        if observed is None:
            return
        yield root, observed
        if not stat.S_ISDIR(observed.st_mode):
            return
        stack: list[list[object]] = [[self._open_directory(root, observed), root, None]]
        try:
            while stack:
                frame = stack[-1]
                descriptor, path, pending = frame
                assert isinstance(descriptor, int) and isinstance(path, str)
                if pending is None:
                    pending = []
                    try:
                        with os.scandir(descriptor) as entries:
                            for entry in entries:
                                child = _join(path, entry.name)
                                try:
                                    child_stat = entry.stat(follow_symlinks=False)
                                except FileNotFoundError:
                                    continue
                                yield child, child_stat
                                if stat.S_ISDIR(child_stat.st_mode):
                                    pending.append(
                                        (entry.name, child, child_stat.st_dev, child_stat.st_ino)
                                    )
                    except OSError as exc:
                        raise InstallError(f"cannot enumerate {path}: {exc}") from exc
                    pending.reverse()
                    frame[2] = pending
                assert isinstance(pending, list)
                if not pending:
                    os.close(descriptor)
                    stack.pop()
                    continue
                name, child, device, inode = pending.pop()
                try:
                    child_descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        # Replaced by a symlink after it was listed: never follow.
                        continue
                    raise InstallError(f"cannot enumerate {child}: {exc}") from exc
                current = os.fstat(child_descriptor)
                if (current.st_dev, current.st_ino) != (device, inode):
                    os.close(child_descriptor)
                    continue
                stack.append([child_descriptor, child, None])
        finally:
            for frame in stack:
                os.close(frame[0])  # type: ignore[arg-type]

    def service_status(self, unit: str) -> Mapping[str, object]:
        argv = ["systemctl", "show", unit]
        argv.extend(f"--property={name}" for name in _SYSTEMCTL_PROPERTIES)
        argv.append("--no-pager")
        try:
            result = _run(tuple(argv))
        except OSError as exc:
            raise InstallError(f"cannot inspect service {unit}: {exc}") from exc
        if result.returncode != 0:
            raise InstallError(f"cannot inspect service {unit}")
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        match = re.search(r"(?:path=|argv\[\]=)(/[^ ;]+)", values.get("ExecStart", ""))
        return {
            "load_state": values.get("LoadState", ""),
            "unit_file_state": values.get("UnitFileState", ""),
            "fragment_path": values.get("FragmentPath", ""),
            "drop_in_paths": sorted(values.get("DropInPaths", "").split()),
            "user": values.get("User", ""),
            "exec_path": match.group(1) if match else "",
            "active_state": values.get("ActiveState", ""),
            "sub_state": values.get("SubState", ""),
        }

    def in_flight(
        self, job_uids: Mapping[str, int], plan: Mapping[str, object]
    ) -> Mapping[str, object]:
        processes = _in_flight_process_count(
            [{"name": name, "uid": uid} for name, uid in job_uids.items()]
        )
        try:
            durable: int | None = _durable_in_flight_job_count(plan)
        except InstallError:
            # The live registry can change under a running Manager; the count
            # is a volatile gate that apply re-derives with services stopped.
            durable = None
        return {"job_processes": processes, "durable_jobs": durable}

    def writable_paths(self, identity: CensusIdentity, tree: CensusTree) -> CensusResult:
        """Let the kernel decide, as ``identity``, which tree entries it may write.

        A forked child drops to the exact account identity (supplementary
        groups, then gid, then uid) and re-walks the root-enumerated tree: each
        directory is reached with ``openat(O_PATH|O_NOFOLLOW)`` relative to its
        already verified parent and must still be the enumerated inode, so the
        kernel checks traversal as the job account and no symlink is ever
        followed.  Each entry is judged with ``faccessat2(AT_SYMLINK_NOFOLLOW)``
        on that directory descriptor, bracketed by two no-follow ``fstatat``
        calls; an entry that became a symlink or changed inode at any point is
        reported unstable instead of being skipped.  The child never execs:
        changing credentials clears its dumpable flag, so the job account
        cannot ptrace the census and forge its answer.  Without root only the
        caller's own identity can be checked.
        """

        drop = os.geteuid() == 0
        if drop:
            if identity.uid == 0 or identity.gid == 0:
                raise InstallError("the writable census never runs as root")
        elif (identity.uid, identity.gid) != (os.getuid(), os.getgid()):
            raise PermissionError("the writable census needs root to assume another account")
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the forked child
            status = 2
            try:
                os.close(read_fd)
                if drop:
                    os.setgroups(list(identity.groups))
                    os.setresgid(identity.gid, identity.gid, identity.gid)
                    os.setresuid(identity.uid, identity.uid, identity.uid)
                    if os.getresuid() != (identity.uid,) * 3 or os.getresgid() != (
                        identity.gid,
                    ) * 3:
                        os._exit(_CENSUS_EXIT_IDENTITY)
                _census_as_current_identity(tree, write_fd)
                status = 0
            except _NoFollowAccessUnsupported:
                status = _CENSUS_EXIT_UNSUPPORTED
            except BaseException:
                status = 2
            finally:
                os._exit(status)
        os.close(write_fd)
        chunks: list[bytes] = []
        try:
            while True:
                chunk = os.read(read_fd, 1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(read_fd)
            _pid, wait_status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(wait_status)
        if code == _CENSUS_EXIT_UNSUPPORTED:
            raise InstallError(
                "the writable census needs faccessat2 with AT_SYMLINK_NOFOLLOW "
                "(Linux 5.8+); this kernel cannot judge an entry without following "
                "a symlink or ignoring its ACL, so the census refuses to run"
            )
        if code != 0:
            raise InstallError(f"writable census as uid {identity.uid} did not complete")
        writable: list[str] = []
        unstable: list[str] = []
        for record in b"".join(chunks).split(b"\0"):
            if not record:
                continue
            (writable if record[:1] == b"W" else unstable).append(os.fsdecode(record[1:]))
        return CensusResult(writable=tuple(writable), unstable=tuple(unstable))


class _NoFollowAccessUnsupported(Exception):
    """faccessat2 cannot evaluate an entry without following a symlink."""


@functools.lru_cache(maxsize=1)
def _libc_syscall():  # type: ignore[no-untyped-def]
    function = ctypes.CDLL(None, use_errno=True).syscall
    function.restype = ctypes.c_long
    return function


def _faccessat2(dir_fd: int, name: str, mode: int) -> None:
    """``faccessat2(dir_fd, name, mode, AT_SYMLINK_NOFOLLOW)`` straight to the kernel.

    Deliberately not ``os.access(..., follow_symlinks=False)``: on a kernel
    without faccessat2, glibc emulates the flag from mode bits and silently
    ignores ACLs.  The raw syscall either answers with the kernel's full
    permission check or fails (ENOSYS / EINVAL), which the census treats as
    unsupported instead of falling back to a symlink-following check.
    """

    number = _FACCESSAT2_SYSCALL.get(platform.machine())
    if number is None:
        raise OSError(errno.ENOSYS, "faccessat2 is not known for this architecture", name)
    result = _libc_syscall()(
        ctypes.c_long(number),
        ctypes.c_long(dir_fd),
        ctypes.c_char_p(os.fsencode(name)),
        ctypes.c_long(mode),
        ctypes.c_long(_AT_SYMLINK_NOFOLLOW),
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


_WRITABLE, _READONLY, _UNSTABLE, _UNREACHABLE = "writable", "readonly", "unstable", "unreachable"


def _census_check(directory_fd: int, name: str, device: int, inode: int) -> str:
    """Judge one entry relative to a verified directory descriptor, no-follow."""

    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except PermissionError:
        return _UNREACHABLE  # no search permission on the directory itself
    except OSError as exc:
        if _is_vanished(exc):
            return _UNSTABLE
        raise
    if stat.S_ISLNK(before.st_mode) or (before.st_dev, before.st_ino) != (device, inode):
        return _UNSTABLE
    try:
        _faccessat2(directory_fd, name, os.W_OK)
        writable = True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            writable = False
        elif exc.errno == errno.ETXTBSY:
            writable = True  # write permission exists; the file is merely running
        elif _is_vanished(exc):
            return _UNSTABLE
        elif exc.errno in _NOFOLLOW_ACCESS_UNSUPPORTED:
            raise _NoFollowAccessUnsupported(exc.errno) from exc
        else:
            raise
    try:
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        if _is_vanished(exc):
            return _UNSTABLE
        raise
    if stat.S_ISLNK(after.st_mode) or (after.st_dev, after.st_ino) != (
        before.st_dev,
        before.st_ino,
    ):
        return _UNSTABLE
    return _WRITABLE if writable else _READONLY


def _open_path_chain(path: str) -> int:
    """Open ``path`` as ``O_PATH`` one no-follow component at a time."""

    descriptor = os.open("/", _PATH_DIRECTORY_FLAGS)
    try:
        for component in PurePosixPath(path).parts[1:]:
            following = os.open(component, _PATH_DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _census_as_current_identity(tree: CensusTree, out_fd: int) -> None:
    """Census body run in the forked child after it assumed the job identity."""

    buffer = bytearray()

    def emit(tag: bytes, path: str) -> None:
        buffer.extend(tag + os.fsencode(path) + b"\0")
        if len(buffer) >= 1 << 16:
            _write_all(out_fd, bytes(buffer))
            buffer.clear()

    for root in tree.roots:
        try:
            parent_fd = _open_path_chain(root.parent)
        except PermissionError:
            continue  # the account cannot even reach this root
        except OSError as exc:
            if _is_vanished(exc):
                emit(b"U", root.path)
                continue
            raise
        frames: list[list[object]] = []
        try:
            current = os.fstat(parent_fd)
            if (current.st_dev, current.st_ino) != (root.parent_dev, root.parent_ino):
                emit(b"U", root.path)
                continue
            first = (posixpath.basename(root.path), root.dev, root.ino, root.is_dir)
            frames.append([parent_fd, root.parent, iter([first]), False])
            while frames:
                frame = frames[-1]
                directory_fd, directory, pending, owned = frame
                item = next(pending, None)  # type: ignore[call-overload]
                if item is None:
                    frames.pop()
                    if owned:
                        os.close(directory_fd)  # type: ignore[arg-type]
                    continue
                name, device, inode, is_dir = item
                path = _join(str(directory), name)
                verdict = _census_check(directory_fd, name, device, inode)  # type: ignore[arg-type]
                if verdict == _UNREACHABLE:
                    frame[2] = iter(())  # nothing below this directory is reachable
                    continue
                if verdict == _UNSTABLE:
                    emit(b"U", path)
                    continue
                if verdict == _WRITABLE:
                    emit(b"W", path)
                if not is_dir:
                    continue
                try:
                    child_fd = os.open(name, _PATH_DIRECTORY_FLAGS, dir_fd=directory_fd)  # type: ignore[arg-type]
                except PermissionError:
                    continue
                except OSError as exc:
                    if _is_vanished(exc):
                        emit(b"U", path)
                        continue
                    raise
                current = os.fstat(child_fd)
                if (current.st_dev, current.st_ino) != (device, inode):
                    os.close(child_fd)
                    emit(b"U", path)
                    continue
                frames.append([child_fd, path, iter(tree.entries.get(path, ())), True])
        finally:
            for frame in frames:
                if frame[3]:
                    os.close(frame[0])  # type: ignore[arg-type]
            os.close(parent_fd)
    if buffer:
        _write_all(out_fd, bytes(buffer))


def build_census_tree(
    backend: LegacyHostBackend,
    roots: Sequence[str],
    visit: Callable[[str, os.stat_result], None] | None = None,
) -> CensusTree:
    """Enumerate census roots as root; symlinks are visited but never entered."""

    tree = CensusTree(roots=[], entries={})
    for raw_root in roots:
        root = posixpath.normpath(raw_root)
        for path, observed in backend.walk(root):
            if visit is not None:
                visit(path, observed)
            if stat.S_ISLNK(observed.st_mode):
                continue
            is_dir = stat.S_ISDIR(observed.st_mode)
            if path == root:
                parent = posixpath.dirname(root)
                parent_stat = backend.lstat(parent)
                if parent_stat is None or not stat.S_ISDIR(parent_stat.st_mode):
                    raise InstallError(f"census root parent is not a directory: {parent}")
                tree.roots.append(
                    CensusRoot(
                        path=root,
                        dev=observed.st_dev,
                        ino=observed.st_ino,
                        is_dir=is_dir,
                        parent=parent,
                        parent_dev=parent_stat.st_dev,
                        parent_ino=parent_stat.st_ino,
                    )
                )
            else:
                parent, name = posixpath.split(path)
                tree.entries.setdefault(parent, []).append(
                    (name, observed.st_dev, observed.st_ino, is_dir)
                )
            tree.count += 1
    return tree


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------


def _file_type(mode: int) -> str:
    for test, name in _FILE_TYPES:
        if test(mode):
            return name
    return "unknown"


def _lstat_record(observed: os.stat_result, *, credential: bool) -> dict[str, object]:
    kind = _file_type(observed.st_mode)
    record: dict[str, object] = {
        "type": kind,
        "uid": observed.st_uid,
        "gid": observed.st_gid,
        "mode": format(stat.S_IMODE(observed.st_mode), "04o"),
        "dev": observed.st_dev,
        "ino": observed.st_ino,
    }
    # Directory size and link count move with their entries; a credential's
    # size would leak about its content.
    if kind == "file" and not credential:
        record["nlink"] = observed.st_nlink
        record["size"] = observed.st_size
    return record


def _is_credential_path(path: str, credential_paths: frozenset[str]) -> bool:
    if path in credential_paths:
        return True
    name = posixpath.basename(path).lower()
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in CREDENTIAL_NAME_PATTERNS):
        return True
    return any(part in CREDENTIAL_DIRECTORY_NAMES for part in path.split("/"))


def _content_record(
    backend: LegacyHostBackend,
    path: str,
    observed: os.stat_result,
    *,
    credential: bool,
) -> dict[str, object]:
    if credential:
        return {"omitted": "credential"}
    kind = _file_type(observed.st_mode)
    if kind == "file":
        if observed.st_size > CONTENT_HASH_MAX_BYTES:
            return {"omitted": "oversize"}
        return {"sha256": backend.sha256_file(path, observed)}
    if kind == "symlink":
        return {"target": backend.readlink(path)}
    if kind == "directory":
        return {"mountpoint": bool(backend.is_mountpoint(path))}
    return {"omitted": "special"}


def _is_vanished(exc: OSError) -> bool:
    return exc.errno in _VANISHED_ERRNOS


def _observe(
    backend: LegacyHostBackend, path: str, *, credential: bool
) -> tuple[os.stat_result | None, dict[str, object] | None]:
    """Return a consistent (lstat, content) pair, or ``(None, None)`` when absent.

    A running legacy service may rename or rewrite an object between its
    ``lstat`` and the read that describes it; the pair is re-observed a few
    times instead of recording metadata and content of two different objects.
    """

    for _attempt in range(_CAPTURE_ATTEMPTS):
        observed = backend.lstat(path)
        if observed is None:
            return None, None
        try:
            return observed, _content_record(backend, path, observed, credential=credential)
        except InventoryRaceError:
            continue
        except OSError as exc:
            if _is_vanished(exc):
                continue
            raise
    raise InstallError(f"{path} kept changing while it was inventoried; rerun the capture")


def _verify_authority_bearing(category: str, name: str) -> bool:
    """Mirror of the verify-time authority enumeration predicate."""

    return (
        category == "toolchain_wrappers"
        or (category == "environment" and PurePosixPath(name).suffix == ".env")
        or name.startswith("cortex")
        or (category == "polkit" and "cortex" in name)
    )


def _account_rows(
    plan: Mapping[str, object], backend: LegacyHostBackend
) -> tuple[list[dict[str, object]], dict[str, PasswdEntry], list[PasswdEntry], list[GroupEntry]]:
    passwd = list(backend.passwd_entries())
    groups = list(backend.group_entries())
    by_name = {row.name: row for row in passwd}
    groups_by_name = {row.name: row for row in groups}
    rows: list[dict[str, object]] = []

    def uid_holders(uid: int) -> list[str]:
        return sorted({row.name for row in passwd if row.uid == uid})

    def gid_holders(gid: int) -> dict[str, list[str]]:
        return {
            "groups": sorted({row.name for row in groups if row.gid == gid}),
            "primary_users": sorted({row.name for row in passwd if row.gid == gid}),
        }

    for kind, desired in _plan_account_rows(plan):
        name = str(desired["name"])
        observed = by_name.get(name)
        group = groups_by_name.get(name)
        uids = {desired.get("uid")}
        gids = {desired.get("gid")}
        if observed is not None:
            uids.add(observed.uid)
            gids.add(observed.gid)
        if group is not None:
            gids.add(group.gid)
        rows.append(
            {
                "name": name,
                "kind": kind,
                "desired": {
                    "uid": desired.get("uid"),
                    "gid": desired.get("gid"),
                    "home": desired.get("home"),
                    "shell": desired.get("shell"),
                },
                "passwd": None
                if observed is None
                else {
                    "uid": observed.uid,
                    "gid": observed.gid,
                    "home": observed.home,
                    "shell": observed.shell,
                },
                "group": None
                if group is None
                else {"name": group.name, "gid": group.gid, "members": sorted(set(group.members))},
                "supplementary_groups": sorted(
                    {row.name for row in groups if name in row.members}
                ),
                "password_locked": None if observed is None else backend.password_locked(name),
                "uid_holders": {
                    str(uid): uid_holders(uid) for uid in sorted(u for u in uids if isinstance(u, int))
                },
                "gid_holders": {
                    str(gid): gid_holders(gid) for gid in sorted(g for g in gids if isinstance(g, int))
                },
            }
        )
    return rows, by_name, passwd, groups


def _managed_rows(
    plan: Mapping[str, object],
    backend: LegacyHostBackend,
    credential_paths: frozenset[str],
) -> tuple[list[dict[str, object]], dict[str, os.stat_result]]:
    rows: list[dict[str, object]] = []
    directories: dict[str, os.stat_result] = {}
    for step in _managed_steps(plan):
        path = str(step["path"])
        credential = _is_credential_path(path, credential_paths)
        observed, content = _observe(backend, path, credential=credential)
        row: dict[str, object] = {
            **step,
            "class": "credential" if credential else "managed",
            "lstat": None,
            "acl": None,
            "content": content,
        }
        if observed is not None:
            row["lstat"] = _lstat_record(observed, credential=credential)
            if not credential and _file_type(observed.st_mode) in {"file", "directory"}:
                row["acl"] = [dict(entry) for entry in backend.read_acl(path)]
            if (
                step["kind"] == "asset"
                and step["asset_type"] == "directory"
                and stat.S_ISDIR(observed.st_mode)
            ):
                directories[path] = observed
        rows.append(row)
    return rows, directories


def _list_names(
    backend: LegacyHostBackend, parent: str
) -> tuple[os.stat_result | None, Sequence[str]]:
    for _attempt in range(_CAPTURE_ATTEMPTS):
        observed = backend.lstat(parent)
        if observed is None or not stat.S_ISDIR(observed.st_mode):
            return observed, ()
        try:
            return observed, backend.list_directory(parent, observed)
        except InventoryRaceError:
            continue
        except OSError as exc:
            if _is_vanished(exc):
                continue
            raise
    raise InstallError(f"{parent} kept changing while it was inventoried; rerun the capture")


def _children(
    backend: LegacyHostBackend, parent: str
) -> list[tuple[str, os.stat_result]]:
    rows: list[tuple[str, os.stat_result]] = []
    for name in _list_names(backend, parent)[1]:
        path = _join(parent, name)
        child = backend.lstat(path)
        if child is not None:
            rows.append((path, child))
    return rows


def _discovered_rows(
    plan: Mapping[str, object],
    backend: LegacyHostBackend,
    *,
    managed: frozenset[str],
    managed_directories: Mapping[str, os.stat_result],
    credential_paths: frozenset[str],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    roots = _plan_roots(plan)
    found: dict[str, tuple[os.stat_result, set[str]]] = {}
    entry_counts: dict[str, int] = {}

    def add(path: str, observed: os.stat_result, rule: str) -> None:
        if path in managed:
            return
        found.setdefault(path, (observed, set()))[1].add(rule)

    for category, parents in _generated_parents(plan).items():
        for parent in parents:
            for path, observed in _children(backend, parent):
                if _verify_authority_bearing(category, posixpath.basename(path)):
                    add(path, observed, f"authority:{category}")
    for path, observed in _children(backend, roots["systemd"]):
        name = posixpath.basename(path)
        if name.startswith("cortex") and stat.S_ISDIR(observed.st_mode):
            for child, child_stat in _children(backend, path):
                add(child, child_stat, "systemd-dropin")
        if stat.S_ISDIR(observed.st_mode) and name.endswith((".wants", ".requires")):
            for child, child_stat in _children(backend, path):
                if posixpath.basename(child).startswith("cortex"):
                    add(child, child_stat, "systemd-wants")
    for rule, parents in (
        ("env-dir", [_env_dir(plan, roots)]),
        ("deploy-top", [roots["deploy"]]),
        ("toolchain-bin", [_toolchain_bin(plan, roots)]),
        ("home-top", sorted({str(row.get("home")) for _k, row in _plan_account_rows(plan)})),
        ("state-top", [roots["state"]]),
    ):
        for parent in parents:
            for path, observed in _children(backend, parent):
                add(path, observed, rule)
    for parent, observed in sorted(managed_directories.items()):
        try:
            names = backend.list_directory(parent, observed)
        except (InventoryRaceError, OSError) as exc:
            # Its managed row already records this inode; a replaced or
            # vanished managed directory makes the capture inconsistent.
            raise InstallError(
                f"managed directory changed during the capture: {parent}; rerun the capture"
            ) from exc
        entry_counts[parent] = len(names)
        for name in names:
            path = _join(parent, name)
            if path in managed:
                continue
            child = backend.lstat(path)
            if child is None:
                continue
            if stat.S_ISDIR(child.st_mode):
                add(path, child, "managed-subdir")
            elif stat.S_ISREG(child.st_mode) and any(
                fnmatch.fnmatchcase(name, pattern) for pattern in RESIDUE_PATTERNS
            ):
                add(path, child, "managed-residue")

    rows: list[dict[str, object]] = []
    for path in sorted(found):
        _listed, rules = found[path]
        credential = _is_credential_path(path, credential_paths)
        observed, content = _observe(backend, path, credential=credential)
        if observed is None:
            continue  # gone before it could be described
        if credential:
            klass = "credential"
        elif rules & _AUTHORITY_RULES:
            klass = "authority"
        else:
            klass = "other"
        rows.append(
            {
                "path": path,
                "rules": sorted(rules),
                "class": klass,
                "lstat": _lstat_record(observed, credential=credential),
                "content": content,
            }
        )
    return rows, entry_counts


def _credential_rows(
    plan: Mapping[str, object], backend: LegacyHostBackend
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in credential_destinations(plan):
        observed = backend.lstat(str(row["path"]))
        rows.append(
            {
                **row,
                "lstat": None
                if observed is None
                else _lstat_record(observed, credential=True),
            }
        )
    return rows


def _service_rows(
    plan: Mapping[str, object],
    backend: LegacyHostBackend,
    credential_paths: frozenset[str],
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, str]]]:
    units = plan.get("activation_order")
    if not isinstance(units, list) or not all(isinstance(unit, str) for unit in units):
        raise InstallPlanError("plan activation_order is invalid")
    stable: dict[str, dict[str, object]] = {}
    volatile: dict[str, dict[str, str]] = {}
    for unit in sorted(units):
        status = backend.service_status(unit)
        exec_path = str(status.get("exec_path") or "")
        executable: dict[str, object] | None = None
        if exec_path:
            credential = _is_credential_path(exec_path, credential_paths)
            observed, content = _observe(backend, exec_path, credential=credential)
            executable = {
                "lstat": None
                if observed is None
                else _lstat_record(observed, credential=credential),
                "content": content,
            }
        stable[unit] = {
            "load_state": str(status.get("load_state", "")),
            "unit_file_state": str(status.get("unit_file_state", "")),
            "fragment_path": str(status.get("fragment_path", "")),
            "drop_in_paths": sorted(str(item) for item in status.get("drop_in_paths", []) or []),
            "user": str(status.get("user", "")),
            "exec_path": exec_path,
            "exec": executable,
        }
        volatile[unit] = {
            "active_state": str(status.get("active_state", "")),
            "sub_state": str(status.get("sub_state", "")),
        }
    return stable, volatile


def _pattern_trie(patterns: Sequence[str]) -> dict[str, object]:
    trie: dict[str, object] = {}
    for pattern in patterns:
        node = trie
        for part in PurePosixPath(pattern).parts[1:]:
            node = node.setdefault(part, {})  # type: ignore[assignment]
        node[_TRIE_TERMINAL] = True
    return trie


def _declared(trie: Mapping[str, object], path: str) -> bool:
    frontier: list[Mapping[str, object]] = [trie]
    for part in path.split("/")[1:]:
        following: list[Mapping[str, object]] = []
        for node in frontier:
            if node.get(_TRIE_TERMINAL):
                return True
            for key in (part, _JOB_SEGMENT):
                child = node.get(key)
                if isinstance(child, Mapping):
                    following.append(child)
        if not following:
            return False
        frontier = following
    return any(node.get(_TRIE_TERMINAL) for node in frontier)


def _collapse(paths: Sequence[str]) -> list[str]:
    """Keep only the outermost paths; a writable directory stands for its subtree."""

    kept: set[str] = set()
    for path in sorted(set(paths), key=lambda value: (value.count("/"), value)):
        parent = path
        covered = False
        while True:
            parent = posixpath.dirname(parent)
            if parent in kept:
                covered = True
                break
            if parent in {"/", ""}:
                break
        if not covered:
            kept.add(path)
    return sorted(kept)


def _external_symlink(path: str, target: str, root: str) -> bool:
    resolved = posixpath.normpath(
        target if target.startswith("/") else posixpath.join(posixpath.dirname(path), target)
    )
    return not (resolved == root or resolved.startswith(root.rstrip("/") + "/"))


def _census(
    plan: Mapping[str, object],
    backend: LegacyHostBackend,
    *,
    passwd_by_name: Mapping[str, PasswdEntry],
    passwd: Sequence[PasswdEntry],
    groups: Sequence[GroupEntry],
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, int]], dict[str, object]]:
    roots = _plan_roots(plan)
    state_root = posixpath.normpath(roots["state"])
    user_names = {row.uid: row.name for row in passwd}
    group_names = {row.gid: row.name for row in groups}
    tally = dict.fromkeys(
        ("entries", "nouser", "nogroup", "setid", "world_writable", "external_symlinks"), 0
    )
    owners: dict[str, int] = {}

    def summarize(path: str, observed: os.stat_result) -> None:
        if not (path == state_root or path.startswith(state_root + "/")):
            return
        tally["entries"] += 1
        owner = (
            f"{user_names.get(observed.st_uid, observed.st_uid)}:"
            f"{group_names.get(observed.st_gid, observed.st_gid)}"
        )
        owners[owner] = owners.get(owner, 0) + 1
        tally["nouser"] += observed.st_uid not in user_names
        tally["nogroup"] += observed.st_gid not in group_names
        mode = observed.st_mode
        if stat.S_ISLNK(mode):
            try:
                target = backend.readlink(path)
            except OSError:
                return  # removed while the tree was walked
            tally["external_symlinks"] += _external_symlink(path, target, state_root)
            return
        tally["setid"] += bool(mode & (stat.S_ISUID | stat.S_ISGID))
        tally["world_writable"] += bool(
            mode & stat.S_IWOTH and not (stat.S_ISDIR(mode) and mode & stat.S_ISVTX)
        )

    tree = build_census_tree(backend, _census_roots(plan), visit=summarize)
    summary: dict[str, object] = {**tally, "owner_census": dict(sorted(owners.items()))}

    stable: dict[str, dict[str, object]] = {}
    counts: dict[str, dict[str, int]] = {}
    for principal in _census_principals(plan):
        entry = passwd_by_name.get(principal)
        if entry is None:
            stable[principal] = {
                "status": "absent",
                "identity": None,
                "writable_outside_declared": [],
                "unstable": [],
            }
            counts[principal] = {
                "checked": 0,
                "writable": 0,
                "outside_declared": 0,
                "unstable": 0,
            }
            continue
        identity = CensusIdentity(
            uid=entry.uid,
            gid=entry.gid,
            groups=tuple(sorted(set(backend.group_list(principal, entry.gid)))),
        )
        result = backend.writable_paths(identity, tree)
        trie = _pattern_trie(declared_writable_patterns(plan, principal))
        outside = [path for path in result.writable if not _declared(trie, path)]
        # A change inside the account's own declared area hides nothing it could
        # not already write; anywhere else it is a fail-closed census finding.
        unstable = sorted({path for path in result.unstable if not _declared(trie, path)})
        rows: list[dict[str, object]] = []
        for path in _collapse(outside):
            observed = backend.lstat(path)
            rows.append(
                {"path": path, "type": None if observed is None else _file_type(observed.st_mode)}
            )
        stable[principal] = {
            "status": "unstable" if unstable else "checked",
            "identity": identity.to_dict(),
            "writable_outside_declared": rows,
            "unstable": unstable,
        }
        counts[principal] = {
            "checked": tree.count,
            "writable": len(result.writable),
            "outside_declared": len(outside),
            "unstable": len(result.unstable),
        }
    return stable, counts, summary


def _collector_version() -> str:
    try:
        return importlib.metadata.version("paulsha-cortex")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def collect_legacy_inventory(
    *,
    plan: Mapping[str, object],
    backend: LegacyHostBackend,
    host_overlay: Mapping[str, object] | None = None,
    captured_at: datetime | None = None,
) -> dict[str, object]:
    """Capture a legacy host read-only and return a validated inventory document."""

    scope = legacy_scope(plan)
    credential_paths = frozenset(
        str(row["path"]) for row in scope["credential_destinations"]  # type: ignore[union-attr]
    )
    accounts, passwd_by_name, passwd, groups = _account_rows(plan, backend)
    managed_rows, managed_directories = _managed_rows(plan, backend, credential_paths)
    managed = frozenset(str(row["path"]) for row in managed_rows)
    discovered, entry_counts = _discovered_rows(
        plan,
        backend,
        managed=managed,
        managed_directories=managed_directories,
        credential_paths=credential_paths,
    )
    services, service_states = _service_rows(plan, backend, credential_paths)
    census, census_counts, state_summary = _census(
        plan, backend, passwd_by_name=passwd_by_name, passwd=passwd, groups=groups
    )
    job_uids = {
        name: passwd_by_name[name].uid
        for name in _census_principals(plan)
        if name in passwd_by_name
    }
    moment = (captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidate = plan.get("candidate")
    candidate_sha = candidate.get("candidate_sha") if isinstance(candidate, Mapping) else None
    document: dict[str, object] = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "kind": INVENTORY_KIND,
        "collector": {
            "cortex_version": _collector_version(),
            "candidate_sha": candidate_sha if isinstance(candidate_sha, str) else None,
        },
        "host": {"binding_sha256": host_binding_sha256(backend.machine_id())},
        "scope": scope,
        "scope_sha256": scope_sha256(scope),
        "host_overlay": host_overlay_record(host_overlay),
        "accounts": accounts,
        "managed_paths": managed_rows,
        "discovered": discovered,
        "credentials": _credential_rows(plan, backend),
        "services": services,
        "census": census,
        "volatile": {
            "captured_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hostname": backend.hostname(),
            "services": service_states,
            "in_flight": dict(backend.in_flight(job_uids, plan)),
            "census": census_counts,
            "state_summary": state_summary,
            "managed_directory_entries": dict(sorted(entry_counts.items())),
        },
    }
    document["inventory_sha256"] = inventory_stable_sha256(document)
    return dict(LegacyInventory.from_document(document).document)


# ---------------------------------------------------------------------------
# schema validation
# ---------------------------------------------------------------------------


_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "inventory_sha256",
        "collector",
        "host",
        "scope",
        "scope_sha256",
        "host_overlay",
        "accounts",
        "managed_paths",
        "discovered",
        "credentials",
        "services",
        "census",
        "volatile",
    }
)
_ACCOUNT_ROW_KEYS = frozenset(
    {
        "name",
        "kind",
        "desired",
        "passwd",
        "group",
        "supplementary_groups",
        "password_locked",
        "uid_holders",
        "gid_holders",
    }
)
_MANAGED_ROW_KEYS = frozenset(
    {"path", "step_id", "kind", "asset_type", "class", "lstat", "acl", "content"}
)
_DISCOVERED_ROW_KEYS = frozenset({"path", "rules", "class", "lstat", "content"})
_CREDENTIAL_ROW_KEYS = frozenset({"principal", "provider", "path", "configured", "lstat"})
_SERVICE_ROW_KEYS = frozenset(
    {"load_state", "unit_file_state", "fragment_path", "drop_in_paths", "user", "exec_path", "exec"}
)
_CENSUS_ROW_KEYS = frozenset({"status", "identity", "writable_outside_declared", "unstable"})
_VOLATILE_KEYS = frozenset(
    {
        "captured_at",
        "hostname",
        "services",
        "in_flight",
        "census",
        "state_summary",
        "managed_directory_entries",
    }
)
_LSTAT_BASE_KEYS = frozenset({"type", "uid", "gid", "mode", "dev", "ino"})


def _fail(message: str) -> InstallPlanError:
    return InstallPlanError(f"legacy inventory is invalid: {message}")


def _exact(value: object, keys: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise _fail(f"{label} must have exactly the keys {sorted(keys)}")
    return value


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_abs(value: object) -> bool:
    return isinstance(value, str) and value.startswith("/") and "\x00" not in value


def _validate_lstat(value: object, label: str, *, credential: bool) -> None:
    if not isinstance(value, Mapping):
        raise _fail(f"{label}.lstat must be an object")
    keys = set(value)
    kind = value.get("type")
    expected = set(_LSTAT_BASE_KEYS)
    if kind == "file" and not credential:
        expected |= {"nlink", "size"}
    if keys != expected:
        raise _fail(f"{label}.lstat keys are invalid")
    if kind not in _KNOWN_FILE_TYPES:
        raise _fail(f"{label}.lstat.type is invalid")
    if not all(_is_int(value[key]) for key in expected - {"type", "mode"}):
        raise _fail(f"{label}.lstat numbers are invalid")
    if not isinstance(value.get("mode"), str) or not re.fullmatch(r"[0-7]{4}", str(value["mode"])):
        raise _fail(f"{label}.lstat.mode is invalid")


def _validate_content(value: object, label: str, *, credential: bool, kind: object) -> None:
    if not isinstance(value, Mapping) or len(value) != 1:
        raise _fail(f"{label}.content must be a single-key object")
    ((key, item),) = value.items()
    if credential:
        if value != {"omitted": "credential"}:
            raise _fail(f"{label} is credential class and must not carry content")
        return
    if key == "sha256":
        if kind != "file" or not _is_sha256(item):
            raise _fail(f"{label}.content.sha256 is invalid")
    elif key == "target":
        if kind != "symlink" or not isinstance(item, str):
            raise _fail(f"{label}.content.target is invalid")
    elif key == "mountpoint":
        if kind != "directory" or type(item) is not bool:
            raise _fail(f"{label}.content.mountpoint is invalid")
    elif key == "omitted":
        if item not in {"oversize", "special"}:
            raise _fail(f"{label}.content.omitted is invalid")
    else:
        raise _fail(f"{label}.content kind is invalid")


def _validate_acl(value: object, label: str) -> None:
    if not isinstance(value, list):
        raise _fail(f"{label}.acl must be a list")
    for entry in value:
        if (
            not isinstance(entry, Mapping)
            or not {"account", "perms", "default"} <= set(entry)
            or not set(entry) <= {"account", "perms", "default", "entry_type"}
            or not isinstance(entry.get("account"), str)
            or not isinstance(entry.get("perms"), str)
            or type(entry.get("default")) is not bool
        ):
            raise _fail(f"{label}.acl rows are invalid")


def _validate_sorted_unique(keys: Sequence[object], label: str) -> None:
    if any(left >= right for left, right in zip(keys, keys[1:])):  # type: ignore[operator]
        raise _fail(f"{label} must be sorted and unique")


def _validate_accounts(value: object) -> None:
    if not isinstance(value, list):
        raise _fail("accounts must be a list")
    for index, row in enumerate(value):
        label = f"accounts[{index}]"
        row = _exact(row, _ACCOUNT_ROW_KEYS, label)
        if not isinstance(row["name"], str) or row["kind"] not in {"principal", "service"}:
            raise _fail(f"{label} identity is invalid")
        _exact(row["desired"], frozenset({"uid", "gid", "home", "shell"}), f"{label}.desired")
        if row["passwd"] is not None:
            passwd = _exact(row["passwd"], frozenset({"uid", "gid", "home", "shell"}), f"{label}.passwd")
            if not (_is_int(passwd["uid"]) and _is_int(passwd["gid"])):
                raise _fail(f"{label}.passwd ids are invalid")
        if row["group"] is not None:
            group = _exact(row["group"], frozenset({"name", "gid", "members"}), f"{label}.group")
            if not _is_int(group["gid"]) or not isinstance(group["members"], list):
                raise _fail(f"{label}.group is invalid")
        if not isinstance(row["supplementary_groups"], list):
            raise _fail(f"{label}.supplementary_groups is invalid")
        if row["password_locked"] not in {True, False, None}:
            raise _fail(f"{label}.password_locked is invalid")
        if not isinstance(row["uid_holders"], Mapping) or not isinstance(row["gid_holders"], Mapping):
            raise _fail(f"{label} holders are invalid")
    _validate_sorted_unique([row["name"] for row in value], "accounts")


def _validate_path_row(row: Mapping[str, object], label: str) -> None:
    credential = row["class"] == "credential"
    lstat_value = row["lstat"]
    if lstat_value is None:
        if row["content"] is not None or row.get("acl") is not None:
            raise _fail(f"{label} is absent but carries content")
        return
    _validate_lstat(lstat_value, label, credential=credential)
    _validate_content(
        row["content"],
        label,
        credential=credential,
        kind=lstat_value.get("type") if isinstance(lstat_value, Mapping) else None,
    )


def _validate_class(
    row: Mapping[str, object], label: str, credential_paths: frozenset[str]
) -> None:
    # The class is re-derived from the path: a row cannot opt out of the
    # metadata-only treatment by claiming a non-credential class.
    expected = _is_credential_path(str(row["path"]), credential_paths)
    if (row["class"] == "credential") != expected:
        raise _fail(f"{label}.class does not match its credential classification")


def _validate_managed(value: object, credential_paths: frozenset[str]) -> None:
    if not isinstance(value, list):
        raise _fail("managed_paths must be a list")
    for index, row in enumerate(value):
        label = f"managed_paths[{index}]"
        row = _exact(row, _MANAGED_ROW_KEYS, label)
        if not _is_abs(row["path"]) or not isinstance(row["step_id"], str):
            raise _fail(f"{label} identity is invalid")
        if row["class"] not in {"managed", "credential"}:
            raise _fail(f"{label}.class is invalid")
        _validate_class(row, label, credential_paths)
        if row["class"] == "credential" and row["acl"] is not None:
            raise _fail(f"{label} is credential class and must not carry an ACL")
        if row["acl"] is not None:
            _validate_acl(row["acl"], label)
        _validate_path_row(row, label)
    _validate_sorted_unique([row["path"] for row in value], "managed_paths")


def _validate_discovered(
    value: object, managed: set[str], credential_paths: frozenset[str]
) -> None:
    if not isinstance(value, list):
        raise _fail("discovered must be a list")
    for index, row in enumerate(value):
        label = f"discovered[{index}]"
        row = _exact(row, _DISCOVERED_ROW_KEYS, label)
        if not _is_abs(row["path"]) or row["path"] in managed:
            raise _fail(f"{label}.path is invalid or managed")
        rules = row["rules"]
        if (
            not isinstance(rules, list)
            or not rules
            or any(rule not in DISCOVERY_RULES for rule in rules)
            or rules != sorted(set(rules))
        ):
            raise _fail(f"{label}.rules are invalid")
        if row["class"] not in {"authority", "credential", "other"}:
            raise _fail(f"{label}.class is invalid")
        _validate_class(row, label, credential_paths)
        if row["lstat"] is None:
            raise _fail(f"{label} must describe an existing object")
        _validate_path_row({**row, "acl": None}, label)
    _validate_sorted_unique([row["path"] for row in value], "discovered")


def _validate_credentials(value: object) -> None:
    if not isinstance(value, list):
        raise _fail("credentials must be a list")
    for index, row in enumerate(value):
        label = f"credentials[{index}]"
        row = _exact(row, _CREDENTIAL_ROW_KEYS, label)
        if (
            not isinstance(row["principal"], str)
            or not isinstance(row["provider"], str)
            or not _is_abs(row["path"])
            or type(row["configured"]) is not bool
        ):
            raise _fail(f"{label} identity is invalid")
        if row["lstat"] is not None:
            _validate_lstat(row["lstat"], label, credential=True)
    _validate_sorted_unique(
        [(row["principal"], row["provider"]) for row in value], "credentials"
    )


def _validate_services(value: object, credential_paths: frozenset[str]) -> None:
    if not isinstance(value, Mapping):
        raise _fail("services must be an object")
    for unit, row in value.items():
        label = f"services.{unit}"
        row = _exact(row, _SERVICE_ROW_KEYS, label)
        if not isinstance(row["drop_in_paths"], list):
            raise _fail(f"{label}.drop_in_paths is invalid")
        executable = row["exec"]
        if executable is not None:
            executable = _exact(executable, frozenset({"lstat", "content"}), f"{label}.exec")
            if executable["lstat"] is not None:
                credential = _is_credential_path(str(row["exec_path"]), credential_paths)
                _validate_path_row(
                    {"class": "credential" if credential else "managed", **executable},
                    f"{label}.exec",
                )


def _validate_census(value: object) -> None:
    if not isinstance(value, Mapping):
        raise _fail("census must be an object")
    for principal, row in value.items():
        label = f"census.{principal}"
        row = _exact(row, _CENSUS_ROW_KEYS, label)
        if row["status"] not in {"checked", "unstable", "absent"}:
            raise _fail(f"{label}.status is invalid")
        unstable = row["unstable"]
        if not isinstance(unstable, list) or not all(_is_abs(item) for item in unstable):
            raise _fail(f"{label}.unstable is invalid")
        _validate_sorted_unique(unstable, f"{label}.unstable")
        if (row["status"] == "unstable") != bool(unstable):
            raise _fail(f"{label}.status does not match its unstable entries")
        if row["status"] == "absent":
            if row["identity"] is not None or row["writable_outside_declared"] != []:
                raise _fail(f"{label} absent principal carries census data")
        else:
            _exact(row["identity"], frozenset({"uid", "gid", "groups"}), f"{label}.identity")
        rows = row["writable_outside_declared"]
        if not isinstance(rows, list) or any(
            not isinstance(item, Mapping)
            or set(item) != {"path", "type"}
            or not _is_abs(item.get("path"))
            for item in rows
        ):
            raise _fail(f"{label}.writable_outside_declared is invalid")
        _validate_sorted_unique([item["path"] for item in rows], f"{label}.writable_outside_declared")


def _validate_document(document: Mapping[str, object]) -> None:
    if not isinstance(document, Mapping) or set(document) != _TOP_LEVEL_KEYS:
        raise _fail("top-level keys do not match schema v1")
    if document["schema_version"] != INVENTORY_SCHEMA_VERSION:
        raise _fail("schema_version must be 1")
    if document["kind"] != INVENTORY_KIND:
        raise _fail("kind is not a legacy inventory")
    _exact(document["collector"], frozenset({"cortex_version", "candidate_sha"}), "collector")
    host = _exact(document["host"], frozenset({"binding_sha256"}), "host")
    if not _is_sha256(host["binding_sha256"]):
        raise _fail("host.binding_sha256 is invalid")
    scope = document["scope"]
    if not isinstance(scope, Mapping) or scope.get("schema_version") != SCOPE_SCHEMA_VERSION:
        raise _fail("scope is invalid")
    destinations = scope.get("credential_destinations")
    if not isinstance(destinations, list) or not all(
        isinstance(row, Mapping) and _is_abs(row.get("path")) for row in destinations
    ):
        raise _fail("scope.credential_destinations is invalid")
    credential_paths = frozenset(str(row["path"]) for row in destinations)
    if document["scope_sha256"] != scope_sha256(scope):
        raise _fail("scope_sha256 does not match the scope")
    overlay = document["host_overlay"]
    if overlay is not None:
        overlay = _exact(overlay, frozenset({"sha256", "keys"}), "host_overlay")
        if not _is_sha256(overlay["sha256"]) or not isinstance(overlay["keys"], list):
            raise _fail("host_overlay is invalid")
    _validate_accounts(document["accounts"])
    _validate_managed(document["managed_paths"], credential_paths)
    managed = {str(row["path"]) for row in document["managed_paths"]}  # type: ignore[union-attr]
    _validate_discovered(document["discovered"], managed, credential_paths)
    _validate_credentials(document["credentials"])
    _validate_services(document["services"], credential_paths)
    _validate_census(document["census"])
    _exact(document["volatile"], _VOLATILE_KEYS, "volatile")
    if not _is_sha256(document["inventory_sha256"]):
        raise _fail("inventory_sha256 is invalid")
    if document["inventory_sha256"] != inventory_stable_sha256(document):
        raise _fail("inventory_sha256 does not match the stable fields")


@dataclass(frozen=True)
class LegacyInventory:
    """A validated, self-digested legacy inventory document."""

    document: Mapping[str, object]

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> "LegacyInventory":
        # Round-trip through the canonical encoding so the validated object is
        # exactly what would be written and read back.
        try:
            normalized = json.loads(canonical_inventory_bytes(document))
        except (TypeError, ValueError) as exc:
            raise _fail(f"not canonical JSON data: {exc}") from exc
        _validate_document(normalized)
        return cls(normalized)

    @classmethod
    def load(cls, path: Path) -> "LegacyInventory":
        descriptor = os.open(os.fspath(path), _READ_FLAGS)
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode):
                raise UnsafeInstallPathError(f"legacy inventory must be a regular file: {path}")
            if observed.st_size > INVENTORY_MAX_BYTES:
                raise _fail("file is too large")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > INVENTORY_MAX_BYTES:
                    raise _fail("file is too large")
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        raw = b"".join(chunks)
        try:
            document = json.loads(raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _fail(f"cannot decode {path}: {exc}") from exc
        inventory = cls.from_document(document)
        if canonical_inventory_bytes(inventory.document) != raw:
            raise _fail(f"{path} is not in canonical form")
        return inventory

    @property
    def inventory_sha256(self) -> str:
        return str(self.document["inventory_sha256"])

    @property
    def scope_sha256(self) -> str:
        return str(self.document["scope_sha256"])

    @property
    def host_binding_sha256(self) -> str:
        host = self.document["host"]
        assert isinstance(host, Mapping)
        return str(host["binding_sha256"])

    def to_bytes(self) -> bytes:
        return canonical_inventory_bytes(self.document)


def _scope_protected_paths(scope: Mapping[str, object]) -> list[str]:
    paths: set[str] = set()
    roots = scope.get("roots")
    if isinstance(roots, Mapping):
        paths.update(value for value in roots.values() if isinstance(value, str))
    for field, key in (("managed_paths", "path"), ("accounts", "home")):
        rows = scope.get(field)
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, Mapping) and isinstance(row.get(key), str):
                paths.add(str(row[key]))
    return sorted(posixpath.normpath(path) for path in paths if path.startswith("/"))


def check_inventory_output(path: Path, scope: Mapping[str, object]) -> Path:
    """Refuse an output that would put the inventory inside what it records.

    The capture must not mutate the host it describes: the output (and every
    parent) must lie outside every deploy/state/systemd/polkit root, managed
    path and account home in ``scope``, and no component may be a symlink.
    Existing ancestors are also compared by inode, so a bind mount of a
    managed directory is refused as well.
    """

    hint = f"write it to an installer-owned location such as {INSTALLER_LEGACY_DIRECTORY}"
    target = Path(path)
    if not target.is_absolute() or ".." in target.parts or not target.name:
        raise UnsafeInstallPathError(
            f"legacy inventory output must be an absolute path without '..': {path}; {hint}"
        )
    target = Path(posixpath.normpath(str(target)))
    try:
        _reject_symlink_ancestors(target, label="legacy inventory output")
    except UnsafeInstallPathError as exc:
        raise UnsafeInstallPathError(f"{exc}; {hint}") from exc
    resolved = Path(os.path.realpath(target))
    if resolved != target:
        raise UnsafeInstallPathError(
            f"legacy inventory output resolves through a symlink: {target} -> {resolved}; {hint}"
        )
    protected = _scope_protected_paths(scope)
    for candidate in protected:
        if str(target) == candidate or str(target).startswith(candidate.rstrip("/") + "/"):
            raise InstallPlanError(
                f"legacy inventory output {target} is inside {candidate}, which the "
                f"inventory records; the capture must not write into the host it "
                f"describes -- {hint}"
            )
    identities: dict[tuple[int, int], str] = {}
    for candidate in protected:
        try:
            observed = os.lstat(candidate)
        except OSError:
            continue
        if stat.S_ISDIR(observed.st_mode):
            identities[(observed.st_dev, observed.st_ino)] = candidate
    for ancestor in target.parents:
        try:
            observed = os.lstat(ancestor)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UnsafeInstallPathError(
                f"cannot inspect legacy inventory output parent {ancestor}: {exc}"
            ) from exc
        alias = identities.get((observed.st_dev, observed.st_ino))
        if alias is not None:
            raise InstallPlanError(
                f"legacy inventory output {target} is inside {alias} (reached "
                f"through {ancestor}), which the inventory records -- {hint}"
            )
    return target


def publish_inventory(path: Path, document: Mapping[str, object]) -> str:
    """Write a validated inventory to ``path`` without replacing anything there."""

    inventory = LegacyInventory.from_document(document)
    scope = inventory.document["scope"]
    assert isinstance(scope, Mapping)
    target = check_inventory_output(Path(path), scope)
    payload = inventory.to_bytes()
    try:
        parent_fd = _open_directory_chain(target.parent)
    except FileNotFoundError as exc:
        raise InstallError(
            f"legacy inventory output directory does not exist: {target.parent}"
        ) from exc
    temporary: str | None = None
    temporary_fd: int | None = None
    try:
        temporary = f".{target.name}.{uuid.uuid4().hex}.tmp"
        temporary_fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o644,
            dir_fd=parent_fd,
        )
        os.fchmod(temporary_fd, 0o644)
        _write_all(temporary_fd, payload)
        os.fsync(temporary_fd)
        try:
            _rename_noreplace_at(parent_fd, temporary, target.name)
        except FileExistsError as exc:
            raise InstallError(
                f"refusing to overwrite an existing legacy inventory: {target}"
            ) from exc
        temporary = None
        os.fsync(parent_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# human-readable summary
# ---------------------------------------------------------------------------


def _describe(lstat_value: object) -> str:
    if not isinstance(lstat_value, Mapping):
        return "absent"
    return (
        f"{lstat_value.get('type')} {lstat_value.get('uid')}:{lstat_value.get('gid')} "
        f"{lstat_value.get('mode')}"
    )


def render_inventory_summary(inventory: LegacyInventory) -> str:
    """Operator review summary for ``legacy show``."""

    document = inventory.document
    lines: list[str] = []
    collector = document["collector"]
    volatile = document["volatile"]
    assert isinstance(collector, Mapping) and isinstance(volatile, Mapping)
    lines.append(f"legacy inventory v{document['schema_version']}")
    lines.append(f"  inventory_sha256    {inventory.inventory_sha256}")
    lines.append(f"  scope_sha256        {inventory.scope_sha256}")
    lines.append(f"  host_binding_sha256 {inventory.host_binding_sha256}")
    overlay = document["host_overlay"]
    if isinstance(overlay, Mapping):
        lines.append(
            f"  host_overlay        {overlay['sha256']} ({', '.join(overlay['keys'])})"  # type: ignore[arg-type]
        )
    else:
        lines.append("  host_overlay        none")
    lines.append(
        f"  collector           {collector['cortex_version']} candidate={collector['candidate_sha']}"
    )
    lines.append(
        f"  captured            {volatile['captured_at']} on {volatile['hostname']} (not digested)"
    )

    lines.append("")
    lines.append("accounts (desired -> observed):")
    for row in document["accounts"]:  # type: ignore[union-attr]
        desired = row["desired"]
        observed = row["passwd"]
        if observed is None:
            status = "absent"
            seen = "-"
        else:
            differs = [
                key for key in ("uid", "gid", "home", "shell") if desired[key] != observed[key]
            ]
            status = "match" if not differs else "differs: " + ",".join(differs)
            seen = f"{observed['uid']}:{observed['gid']} {observed['home']} {observed['shell']}"
        lines.append(
            f"  {row['name']} ({row['kind']}): desired {desired['uid']}:{desired['gid']} "
            f"{desired['home']} {desired['shell']} -> {seen} [{status}]"
        )
        lines.append(
            f"    password_locked={row['password_locked']} "
            f"supplementary={','.join(row['supplementary_groups']) or '-'}"
        )
        for uid, holders in sorted(row["uid_holders"].items()):
            foreign = [name for name in holders if name != row["name"]]
            if foreign:
                lines.append(f"    uid {uid} also held by: {', '.join(foreign)}")
        for gid, holders in sorted(row["gid_holders"].items()):
            foreign = sorted(
                {name for name in (*holders["groups"], *holders["primary_users"]) if name != row["name"]}
            )
            if foreign:
                lines.append(f"    gid {gid} also held by: {', '.join(foreign)}")

    managed = document["managed_paths"]
    assert isinstance(managed, list)
    absent = [row for row in managed if row["lstat"] is None]
    lines.append("")
    lines.append(
        f"managed paths: {len(managed)} declared, {len(managed) - len(absent)} present, "
        f"{len(absent)} absent"
    )
    for row in managed:
        if row["lstat"] is None:
            continue
        note = ""
        if row["class"] == "credential":
            note = " [credential: metadata only]"
        elif isinstance(row["content"], Mapping) and "omitted" in row["content"]:
            note = f" [{row['content']['omitted']}: metadata only]"
        lines.append(f"  {row['path']}: {_describe(row['lstat'])}{note}")
    for row in absent:
        lines.append(f"  {row['path']}: absent")

    discovered = document["discovered"]
    assert isinstance(discovered, list)
    lines.append("")
    by_class: dict[str, list[Mapping[str, object]]] = {}
    for row in discovered:
        by_class.setdefault(str(row["class"]), []).append(row)
    lines.append(
        "discovered objects: "
        + ", ".join(f"{name}={len(by_class.get(name, []))}" for name in ("authority", "credential", "other"))
    )
    for klass in ("authority", "credential", "other"):
        for row in by_class.get(klass, []):
            lines.append(
                f"  [{klass}] {row['path']}: {_describe(row['lstat'])} "
                f"rules={','.join(row['rules'])}"  # type: ignore[arg-type]
            )

    lines.append("")
    lines.append("credential destinations (metadata only):")
    for row in document["credentials"]:  # type: ignore[union-attr]
        lines.append(
            f"  {row['principal']}/{row['provider']}"
            f"{' (configured)' if row['configured'] else ''}: {row['path']}: {_describe(row['lstat'])}"
        )

    lines.append("")
    lines.append("services:")
    services = document["services"]
    active = volatile["services"]
    assert isinstance(services, Mapping) and isinstance(active, Mapping)
    for unit, row in sorted(services.items()):
        state = active.get(unit, {})
        lines.append(
            f"  {unit}: load={row['load_state']} unit_file={row['unit_file_state']} "
            f"user={row['user'] or '-'} active={state.get('active_state', '?')}"
        )
        lines.append(f"    fragment={row['fragment_path'] or '-'} exec={row['exec_path'] or '-'}")
        if row["drop_in_paths"]:
            lines.append(f"    drop-ins: {', '.join(row['drop_in_paths'])}")

    lines.append("")
    lines.append("writable census (paths outside plan-declared writable assets):")
    census = document["census"]
    assert isinstance(census, Mapping)
    for principal, row in sorted(census.items()):
        if row["status"] == "absent":
            lines.append(f"  {principal}: account absent")
            continue
        outside = row["writable_outside_declared"]
        lines.append(f"  {principal}: {len(outside) or 'none'}")
        for item in outside:
            lines.append(f"    {item['path']} ({item['type']})")
        if row["status"] == "unstable":
            lines.append(
                f"    UNSTABLE: {len(row['unstable'])} entries changed while checked; "
                "a plan must refuse this inventory"
            )
            for item in row["unstable"]:
                lines.append(f"      {item}")

    lines.append("")
    lines.append("volatile gates (not digested):")
    in_flight = volatile["in_flight"]
    summary = volatile["state_summary"]
    assert isinstance(in_flight, Mapping) and isinstance(summary, Mapping)
    lines.append(
        f"  in_flight: job_processes={in_flight.get('job_processes')} "
        f"durable_jobs={in_flight.get('durable_jobs')}"
    )
    lines.append(
        "  state: "
        + " ".join(
            f"{key}={summary.get(key)}"
            for key in ("entries", "nouser", "nogroup", "setid", "world_writable", "external_symlinks")
        )
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# plan binding: legacy_policy, dispositions and the quarantine step (PR-3)
# ---------------------------------------------------------------------------
#
# ``legacy_policy`` gains its meaning here.  ``reject`` keeps today's
# behaviour and refuses any ``legacy_adoption`` block; ``quarantine`` without a
# block plans exactly as before.  Only ``quarantine`` together with a block
# (which only the host overlay may carry) and ``--legacy-inventory`` binds a
# reviewed inventory: the plan re-derives its scope, checks the self digest,
# the host binding and the overlay the capture used, and derives one
# disposition for every inventoried object.  Anything no rule classifies fails
# planning.  Apply refuses these plans until the apply side lands (PR-4).

LEGACY_PLAN_SCHEMA_VERSION = 1
QUARANTINE_STEP_KIND = "legacy-quarantine"
QUARANTINE_OPERATIONS = ("snapshot", "rename-noreplace")
QUARANTINE_ROLLBACK_POLICY = "restore"
DISPOSITIONS = ("adopt", "adopt-in-place", "quarantine-then-create", "quarantine")
SUMMARY_KEYS = (*DISPOSITIONS, "create", "covered")
_QUARANTINE_DISPOSITIONS = frozenset({"quarantine", "quarantine-then-create"})
QUARANTINE_REASONS = frozenset(
    {
        "authority",
        "credential",
        "deploy-backup",
        "job-worktree-pool",
        "managed-generated",
        "managed-residue",
        "managed-subdir",
        "managed-symlink-mismatch",
        "operator",
        "source-repository",
        "state-top",
        "superseded-by-launcher",
        "toolchain-bin",
        "venv-active-directory",
    }
)
#: Job worktree pools are rebuilt, never adopted (owner ruling, #1122).
JOB_WORKTREE_POOL_STEPS = frozenset(
    {"asset:dispatch-worktree-pool", "asset:gate-worktree-pool"}
)
#: Non-authoritative old copies at the top of the deploy root.
DEPLOY_BACKUP_PATTERNS = ("venv.*", "venv-*", "operator-backups", *RESIDUE_PATTERNS)
#: The #568 reviewer drop-in and its settings file; the reviewer launcher's
#: ``--add-dir`` superseded both (#1125), so they move without a port.
SUPERSEDED_BY_LAUNCHER = ("agy-reviewer-settings.json", "agy-review-settings.conf")
_QUARANTINABLE_TYPES = frozenset({"file", "directory", "symlink"})
_EXPECTED_BASE_KEYS = ("type", "uid", "gid", "mode", "dev", "ino")

_LEGACY_REQUEST_KEYS = frozenset(
    {"inventory_sha256", "quarantine_root", "census_exceptions", "quarantine_paths"}
)
_LEGACY_REQUEST_REQUIRED = frozenset({"inventory_sha256", "quarantine_root"})
_LEGACY_BLOCK_KEYS = frozenset(
    {
        "schema_version",
        "inventory_sha256",
        "scope_sha256",
        "host_binding_sha256",
        "host_overlay_sha256",
        "quarantine_root",
        "adopted",
        "quarantine",
        "census_exceptions",
        "summary",
    }
)
_QUARANTINE_ROW_KEYS = frozenset({"path", "disposition", "reason", "row_sha256", "covers"})
_QUARANTINE_STEP_KEYS = frozenset(
    {
        "step_id",
        "kind",
        "path",
        "destination",
        "expected",
        "row_sha256",
        "operations",
        "rollback_policy",
        "desired_sha256",
    }
)


class LegacyAdoptionPlanError(InstallPlanError):
    """Legacy adoption cannot be planned; ``failures`` names every reason."""

    def __init__(self, failures: Sequence[str]) -> None:
        self.failures = tuple(failures)
        super().__init__(
            "legacy adoption cannot be planned:\n"
            + "\n".join(f"  - {failure}" for failure in self.failures)
        )


def _normalized_absolute(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and value != "/"
        and "\x00" not in value
        and posixpath.normpath(value) == value
    )


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def inventory_row_sha256(section: str, row: Mapping[str, object]) -> str:
    """Digest of one inventory row, bound to the section it came from."""

    return _digest({"section": section, "row": row})


def quarantine_destination(quarantine_root: str, inventory_sha256: str, path: str) -> str:
    """``<quarantine_root>/<inventory_sha[:16]>/root/<original absolute path>``."""

    return f"{quarantine_root.rstrip('/')}/{inventory_sha256[:16]}/root{path}"


def validate_legacy_adoption_request(block: object) -> dict[str, object]:
    """Validate the host overlay's ``legacy_adoption`` block."""

    if not isinstance(block, Mapping) or not all(isinstance(key, str) for key in block):
        raise InstallPlanError("host overlay legacy_adoption must be an object with string keys")
    unknown = sorted(set(block) - _LEGACY_REQUEST_KEYS)
    missing = sorted(_LEGACY_REQUEST_REQUIRED - set(block))
    if unknown or missing:
        details = [
            *(["unknown=" + ",".join(unknown)] if unknown else []),
            *(["missing=" + ",".join(missing)] if missing else []),
        ]
        raise InstallPlanError(
            "host overlay legacy_adoption keys must match the schema: " + "; ".join(details)
        )
    if not _is_sha256(block["inventory_sha256"]):
        raise InstallPlanError("legacy_adoption.inventory_sha256 must be a 64-hex digest")
    if not _normalized_absolute(block["quarantine_root"]):
        raise InstallPlanError(
            "legacy_adoption.quarantine_root must be a normalized absolute path other than /"
        )
    exceptions = block.get("census_exceptions", [])
    if type(exceptions) is not list:
        raise InstallPlanError("legacy_adoption.census_exceptions must be a list")
    rows: list[dict[str, str]] = []
    for item in exceptions:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"path", "principal"}
            or not _normalized_absolute(item["path"])
            or not isinstance(item["principal"], str)
            or not item["principal"]
        ):
            raise InstallPlanError(
                "legacy_adoption.census_exceptions rows must be {path, principal} "
                "with a normalized absolute path"
            )
        rows.append({"path": str(item["path"]), "principal": str(item["principal"])})
    keys = [(row["path"], row["principal"]) for row in rows]
    if len(set(keys)) != len(keys):
        raise InstallPlanError("legacy_adoption.census_exceptions contains a duplicate")
    paths = block.get("quarantine_paths", [])
    if (
        type(paths) is not list
        or not all(_normalized_absolute(path) for path in paths)
        or len(set(paths)) != len(paths)
    ):
        raise InstallPlanError(
            "legacy_adoption.quarantine_paths must be unique normalized absolute paths"
        )
    return {
        "inventory_sha256": block["inventory_sha256"],
        "quarantine_root": block["quarantine_root"],
        "census_exceptions": sorted(rows, key=lambda row: (row["path"], row["principal"])),
        "quarantine_paths": sorted(str(path) for path in paths),
    }


def legacy_adoption_request(
    config: Mapping[str, object],
    overlay: Mapping[str, object] | None,
    *,
    inventory_given: bool,
) -> dict[str, object] | None:
    """Apply ``legacy_policy``: return the adoption request, or ``None``.

    ``reject`` refuses any adoption.  ``quarantine`` without a block plans
    exactly as before.  A block needs ``--legacy-inventory`` and the inventory
    needs a block; the release config itself can never carry one because its
    top-level keys are exact.
    """

    policy = config.get("legacy_policy") if isinstance(config, Mapping) else None
    block = overlay.get("legacy_adoption") if isinstance(overlay, Mapping) else None
    if block is None:
        if inventory_given:
            if policy == "reject":
                raise InstallPlanError(
                    "legacy_policy: reject refuses legacy adoption; --legacy-inventory is not accepted"
                )
            raise InstallPlanError(
                "--legacy-inventory requires a legacy_adoption block in the host overlay"
            )
        return None
    if policy == "reject":
        raise InstallPlanError(
            "legacy_policy: reject refuses a legacy_adoption block; only "
            "legacy_policy: quarantine may adopt a host without a receipt"
        )
    if policy != "quarantine":
        raise InstallPlanError("legacy_policy must be quarantine or reject")
    request = validate_legacy_adoption_request(block)
    if not inventory_given:
        raise InstallPlanError("a legacy_adoption block requires --legacy-inventory")
    return request


def bind_host_overlay(
    plan: Mapping[str, object], overlay: Mapping[str, object] | None
) -> dict[str, object]:
    """Record the overlay digest; a plan without an overlay is returned unchanged.

    The digest covers the overlay minus ``legacy_adoption`` -- the same record
    the inventory carries -- so it stays stable from adoption to every later
    upgrade that reuses the persisted overlay.
    """

    record = host_overlay_record(overlay)
    if record is None:
        return plan  # type: ignore[return-value]
    bound = deepcopy(dict(plan))
    bound["host_overlay_sha256"] = record["sha256"]
    bound["receipt_path"] = str(canonical_receipt_path(bound))
    return bound


@dataclass
class _Outcome:
    """The disposition of one inventoried object (or why it has none)."""

    path: str
    section: str
    row: Mapping[str, object]
    disposition: str | None = None  # a DISPOSITIONS value, or "create"
    reason: str | None = None
    step_id: str | None = None
    failure: str | None = None
    covered_by: str | None = None

    @property
    def exists(self) -> bool:
        return isinstance(self.row.get("lstat"), Mapping)


def _owner_ids(plan: Mapping[str, object]) -> tuple[dict[str, int], dict[str, int]]:
    users = {"root": 0}
    groups = {"root": 0}
    for _kind, row in _plan_account_rows(plan):
        if isinstance(row.get("uid"), int):
            users[str(row["name"])] = int(row["uid"])  # type: ignore[arg-type]
        if isinstance(row.get("gid"), int):
            groups[str(row["name"])] = int(row["gid"])  # type: ignore[arg-type]
    return users, groups


def _wanted_by(content: str) -> list[str]:
    section = None
    targets: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "Install" and line.startswith("WantedBy="):
            targets.extend(line.split("=", 1)[1].split())
    return targets


def _enablement_links(
    plan: Mapping[str, object], roots: Mapping[str, str]
) -> dict[str, tuple[str, str]]:
    """``<systemd>/<target>.wants/<unit>`` -> (enable step id, unit path)."""

    generated = plan.get("generated")
    units = generated.get("units") if isinstance(generated, Mapping) else None
    units = units if isinstance(units, Mapping) else {}
    links: dict[str, tuple[str, str]] = {}
    for step in plan.get("apply_order", []) or []:
        if (
            not isinstance(step, Mapping)
            or step.get("kind") != "systemctl"
            or step.get("action") != "enable"
            or not isinstance(step.get("unit"), str)
        ):
            continue
        unit = str(step["unit"])
        artifact = units.get(unit)
        if (
            not isinstance(artifact, Mapping)
            or not isinstance(artifact.get("content"), str)
            or not isinstance(artifact.get("path"), str)
        ):
            continue
        for target in _wanted_by(str(artifact["content"])):
            link = _join(roots["systemd"], f"{target}.wants/{unit}")
            links.setdefault(link, (str(step["step_id"]), str(artifact["path"])))
    return links


def _type_failure(path: str, observed: object, expected: str) -> str:
    return (
        f"type: {path} is a {observed}, but the plan manages a {expected} there; "
        "remove or replace it before adoption"
    )


def _classify_managed(
    row: Mapping[str, object],
    step: Mapping[str, object],
    *,
    toolchain_bin: str,
    owners: tuple[Mapping[str, int], Mapping[str, int]],
    operator_paths: frozenset[str],
) -> _Outcome:
    path = str(row["path"])
    outcome = _Outcome(path, "managed_paths", row)
    lstat_value = row["lstat"]
    if not isinstance(lstat_value, Mapping):
        outcome.disposition = "create"
        return outcome
    kind = row["kind"]
    observed = lstat_value["type"]
    step_id = str(row["step_id"])
    credential = row["class"] == "credential"

    def quarantine(disposition: str, reason: str) -> _Outcome:
        outcome.disposition = disposition
        outcome.reason = reason
        return outcome

    def fail(message: str) -> _Outcome:
        outcome.failure = message
        return outcome

    if kind == "asset":
        expected = {"directory": "directory", "symlink": "symlink", "file": "file"}.get(
            str(row["asset_type"])
        )
        if expected is None:
            return fail(f"unclassified: {path} (managed asset of type {row['asset_type']})")
        if observed != expected:
            return fail(_type_failure(path, observed, expected))
        if credential:
            return quarantine("quarantine-then-create", "credential")
        if expected == "directory":
            if step_id in JOB_WORKTREE_POOL_STEPS:
                return quarantine("quarantine-then-create", "job-worktree-pool")
            if path == toolchain_bin:
                return quarantine("quarantine-then-create", "toolchain-bin")
            if path in operator_paths:
                return quarantine("quarantine-then-create", "operator")
            outcome.disposition = "adopt-in-place"
            outcome.step_id = step_id
            return outcome
        if expected == "symlink":
            users, groups = owners
            if (
                row["content"] == {"target": step.get("target")}
                and lstat_value["uid"] == users.get(str(step.get("owner")))
                and lstat_value["gid"] == groups.get(str(step.get("group")))
            ):
                if path in operator_paths:
                    return quarantine("quarantine-then-create", "operator")
                outcome.disposition = "adopt"
                outcome.step_id = step_id
                return outcome
            return quarantine("quarantine-then-create", "managed-symlink-mismatch")
        return quarantine("quarantine-then-create", "managed-generated")
    if kind == "repository":
        if observed != "directory":
            return fail(_type_failure(path, observed, "directory"))
        return quarantine("quarantine-then-create", "source-repository")
    if kind == "toolchain":
        return fail(
            f"install path: {path} already exists without receipt provenance; the "
            "versioned toolchain install path is never replaced in place"
        )
    if kind == "venv":
        return fail(
            f"install path: {path} already exists without receipt provenance; the "
            "candidate venv slot must be created by this plan"
        )
    if kind == "venv-link":
        if observed == "directory":
            return quarantine("quarantine", "venv-active-directory")
        if observed == "symlink":
            return fail(
                f"venv: the active venv link {path} is a symlink without receipt "
                "provenance; nothing proves where it came from"
            )
        return fail(_type_failure(path, observed, "directory or symlink"))
    return fail(f"unclassified: {path} (managed kind {kind})")


def _classify_discovered(
    row: Mapping[str, object],
    *,
    links: Mapping[str, tuple[str, str]],
    operator_paths: frozenset[str],
) -> _Outcome:
    path = str(row["path"])
    outcome = _Outcome(path, "discovered", row)
    lstat_value = row["lstat"]
    assert isinstance(lstat_value, Mapping)
    observed = lstat_value["type"]
    rules = [str(rule) for rule in row["rules"]]  # type: ignore[union-attr]
    klass = row["class"]
    name = posixpath.basename(path)

    def quarantine(reason: str) -> _Outcome:
        outcome.disposition = "quarantine"
        outcome.reason = reason
        return outcome

    def unclassified() -> _Outcome:
        if path in operator_paths:
            return quarantine("operator")
        outcome.failure = (
            f"unclassified: {path} ({observed}, class={klass}, rules={','.join(rules)}); "
            "no disposition rule covers it -- remove it or list it in "
            "legacy_adoption.quarantine_paths after review"
        )
        return outcome

    if observed not in _QUARANTINABLE_TYPES:
        return unclassified()
    if klass == "credential":
        return quarantine("credential")
    if klass == "authority":
        link = links.get(path)
        if (
            link is not None
            and "systemd-wants" in rules
            and observed == "symlink"
            and row["content"] == {"target": link[1]}
            and path not in operator_paths
        ):
            # The enable step would create exactly this link.
            outcome.disposition = "adopt"
            outcome.step_id = link[0]
            return outcome
        if name in SUPERSEDED_BY_LAUNCHER:
            return quarantine("superseded-by-launcher")
        return quarantine("authority")
    if name in SUPERSEDED_BY_LAUNCHER and "env-dir" in rules:
        return quarantine("superseded-by-launcher")
    if "state-top" in rules:
        return quarantine("state-top")
    if "deploy-top" in rules and any(
        fnmatch.fnmatchcase(name, pattern) for pattern in DEPLOY_BACKUP_PATTERNS
    ):
        return quarantine("deploy-backup")
    if "managed-subdir" in rules:
        return quarantine("managed-subdir")
    if "managed-residue" in rules:
        return quarantine("managed-residue")
    return unclassified()


def _classify_credential(
    row: Mapping[str, object], *, operator_paths: frozenset[str]
) -> _Outcome:
    path = str(row["path"])
    outcome = _Outcome(path, "credentials", row)
    lstat_value = row["lstat"]
    assert isinstance(lstat_value, Mapping)
    if lstat_value["type"] not in _QUARANTINABLE_TYPES and path not in operator_paths:
        outcome.failure = (
            f"unclassified: credential destination {path} is a {lstat_value['type']}"
        )
        return outcome
    outcome.disposition = "quarantine"
    outcome.reason = "credential" if path not in operator_paths else "operator"
    return outcome


def _account_failures(
    desired: Mapping[str, object], row: Mapping[str, object]
) -> list[str]:
    """Why an account row does not match the overlay field by field."""

    name = str(desired["name"])
    uid = desired["uid"]
    gid = desired["gid"]
    problems: list[str] = []
    uid_holders = row["uid_holders"]
    gid_holders = row["gid_holders"]
    assert isinstance(uid_holders, Mapping) and isinstance(gid_holders, Mapping)
    foreign_uid = [holder for holder in uid_holders.get(str(uid), []) if holder != name]
    held = gid_holders.get(str(gid), {})
    held = held if isinstance(held, Mapping) else {}
    foreign_groups = [holder for holder in held.get("groups", []) if holder != name]
    foreign_primary = [holder for holder in held.get("primary_users", []) if holder != name]
    if foreign_uid:
        problems.append(f"uid {uid} is held by {', '.join(foreign_uid)}")
    if foreign_groups:
        problems.append(f"gid {gid} is held by group {', '.join(foreign_groups)}")
    if foreign_primary:
        problems.append(f"gid {gid} is the primary group of {', '.join(foreign_primary)}")
    passwd = row["passwd"]
    group = row["group"]
    if not isinstance(passwd, Mapping):
        if group is not None:
            problems.append(f"group {name} exists without the account")
    else:
        for field, expected in (
            ("uid", uid),
            ("gid", gid),
            ("home", desired["home"]),
            ("shell", desired["shell"]),
        ):
            if passwd[field] != expected:
                problems.append(
                    f"{field} is {passwd[field]!r} but the overlay declares {expected!r}"
                )
        if not isinstance(group, Mapping):
            problems.append(f"group {name} is missing")
        else:
            if group["gid"] != gid:
                problems.append(
                    f"group {name} has gid {group['gid']} but the overlay declares {gid}"
                )
            members = [member for member in group["members"] if member != name]  # type: ignore[union-attr]
            if members:
                problems.append(f"group {name} has member(s) {', '.join(members)}")
        supplementary = row["supplementary_groups"]
        if supplementary:
            problems.append(
                f"supplementary groups {', '.join(supplementary)} are not allowed"  # type: ignore[arg-type]
            )
        if row["password_locked"] is not True:
            problems.append("password is not locked")
    return [
        f"account {name}: {problem}; the installer never remaps ids or alters other identities"
        for problem in problems
    ]


def _quarantine_root_problems(root: str, scope: Mapping[str, object]) -> list[str]:
    problems: list[str] = []
    for protected in _scope_protected_paths(scope):
        if _within(root, protected):
            problems.append(
                f"quarantine_root {root} lies inside {protected}, which the inventory records"
            )
        elif _within(protected, root):
            problems.append(
                f"quarantine_root {root} contains {protected}, which the inventory records"
            )
    return problems


def _quarantine_expected(
    lstat_value: Mapping[str, object], content: object
) -> dict[str, object]:
    expected: dict[str, object] = {key: lstat_value[key] for key in _EXPECTED_BASE_KEYS}
    if isinstance(content, Mapping):
        if lstat_value["type"] == "file" and _is_sha256(content.get("sha256")):
            expected["sha256"] = content["sha256"]
        elif lstat_value["type"] == "symlink" and isinstance(content.get("target"), str):
            expected["link_target"] = content["target"]
    return expected


def _quarantine_step_digest(step: Mapping[str, object]) -> str:
    return _digest(
        {
            "kind": QUARANTINE_STEP_KIND,
            "path": step.get("path"),
            "destination": step.get("destination"),
            "expected": step.get("expected"),
            "row_sha256": step.get("row_sha256"),
        }
    )


def _quarantine_step(
    outcome: _Outcome, *, quarantine_root: str, inventory_sha256: str
) -> dict[str, object]:
    lstat_value = outcome.row["lstat"]
    assert isinstance(lstat_value, Mapping)
    step: dict[str, object] = {
        "step_id": f"{QUARANTINE_STEP_KIND}:{outcome.path}",
        "kind": QUARANTINE_STEP_KIND,
        "path": outcome.path,
        "destination": quarantine_destination(quarantine_root, inventory_sha256, outcome.path),
        "expected": _quarantine_expected(lstat_value, outcome.row.get("content")),
        "row_sha256": inventory_row_sha256(outcome.section, outcome.row),
        "operations": list(QUARANTINE_OPERATIONS),
        "rollback_policy": QUARANTINE_ROLLBACK_POLICY,
    }
    step["desired_sha256"] = _quarantine_step_digest(step)
    return step


def _place_quarantine_steps(
    order: Sequence[Mapping[str, object]], steps: Sequence[dict[str, object]]
) -> list[Mapping[str, object]]:
    """Put each quarantine step right after its parent's managed step.

    A path whose parent is not a managed directory (the systemd or polkit
    root, a HOME's parent) goes right after the account steps.  Parents
    precede children in a valid plan, so a quarantine placed after its parent
    precedes every step at or below its own path.
    """

    directories = {
        str(step["path"]): index
        for index, step in enumerate(order)
        if step.get("kind") == "asset"
        and step.get("asset_type") == "directory"
        and isinstance(step.get("path"), str)
    }
    last_account = max(
        (index for index, step in enumerate(order) if step.get("kind") == "account"),
        default=-1,
    )
    anchored: dict[int, list[dict[str, object]]] = {}
    for step in steps:
        anchor = directories.get(posixpath.dirname(str(step["path"])), last_account)
        anchored.setdefault(anchor, []).append(step)
    placed: list[Mapping[str, object]] = list(
        sorted(anchored.get(-1, []), key=lambda row: str(row["path"]))
    )
    for index, step in enumerate(order):
        placed.append(step)
        placed.extend(sorted(anchored.get(index, []), key=lambda row: str(row["path"])))
    return placed


def _covering_root(path: str, roots: frozenset[str]) -> str | None:
    parent = posixpath.dirname(path)
    while parent not in {"/", ""}:
        if parent in roots:
            return parent
        parent = posixpath.dirname(parent)
    return None


def derive_legacy_adoption(
    plan: Mapping[str, object],
    *,
    request: Mapping[str, object],
    overlay: Mapping[str, object] | None,
    inventory: LegacyInventory,
    host_binding_sha256: str,
) -> dict[str, object]:
    """Bind ``inventory`` to ``plan`` and derive every object's disposition.

    Binding failures (digest, scope, overlay, host) stop at once; every
    disposition failure is collected so the operator sees them together.
    """

    if "legacy_adoption" in plan or any(
        isinstance(step, Mapping) and step.get("kind") == QUARANTINE_STEP_KIND
        for step in plan.get("apply_order", []) or []
    ):
        raise InstallPlanError("plan is already bound to a legacy inventory")
    document = inventory.document
    if request["inventory_sha256"] != inventory.inventory_sha256:
        raise LegacyAdoptionPlanError(
            [
                f"inventory_sha256: the legacy_adoption block names "
                f"{request['inventory_sha256']} but --legacy-inventory is "
                f"{inventory.inventory_sha256}"
            ]
        )
    scope = legacy_scope(plan)
    derived_scope = scope_sha256(scope)
    if derived_scope != inventory.scope_sha256:
        raise LegacyAdoptionPlanError(
            [
                f"scope: the inventory covers scope {inventory.scope_sha256} but this "
                f"config, overlay and bundle derive {derived_scope}; recapture it with "
                "the same inputs"
            ]
        )
    overlay_record = host_overlay_record(overlay)
    if document["host_overlay"] != overlay_record:
        raise LegacyAdoptionPlanError(
            [
                "host overlay: the inventory was captured with host overlay "
                f"{(document['host_overlay'] or {}).get('sha256')} but this plan uses "  # type: ignore[union-attr]
                f"{(overlay_record or {}).get('sha256')}"
            ]
        )
    if inventory.host_binding_sha256 != host_binding_sha256:
        raise LegacyAdoptionPlanError(
            [
                "host binding: the inventory was captured on another host "
                f"({inventory.host_binding_sha256}); this host is {host_binding_sha256}"
            ]
        )

    roots = _plan_roots(plan)
    quarantine_root = str(request["quarantine_root"])
    failures: list[str] = list(_quarantine_root_problems(quarantine_root, scope))
    exceptions = list(request["census_exceptions"])  # type: ignore[arg-type]
    principals = set(scope["census"]["principals"])  # type: ignore[index]
    for exception in exceptions:
        if exception["principal"] not in principals:
            failures.append(
                f"census_exceptions: {exception['principal']} is not a job account the "
                f"census checks ({', '.join(sorted(principals))})"
            )
    operator_paths = frozenset(request["quarantine_paths"])  # type: ignore[arg-type]

    # accounts: adopt when they match the overlay field by field
    adopted: dict[str, str] = {}
    summary = dict.fromkeys(SUMMARY_KEYS, 0)
    rows_by_name = {str(row["name"]): row for row in document["accounts"]}  # type: ignore[union-attr]
    for _kind, desired in _plan_account_rows(plan):
        name = str(desired["name"])
        row = rows_by_name.get(name)
        if row is None:
            failures.append(f"account {name}: the inventory has no row for it")
            continue
        problems = _account_failures(desired, row)
        failures.extend(problems)
        if problems:
            continue
        if row["passwd"] is None:
            summary["create"] += 1
        else:
            adopted[f"account:{name}"] = inventory_row_sha256("accounts", row)
            summary["adopt"] += 1

    # every path-bearing object
    steps_by_id = {
        str(step["step_id"]): step
        for step in plan.get("apply_order", []) or []
        if isinstance(step, Mapping)
    }
    toolchain_bin = _toolchain_bin(plan, roots)
    owners = _owner_ids(plan)
    links = _enablement_links(plan, roots)
    outcomes: list[_Outcome] = []
    for row in document["managed_paths"]:  # type: ignore[union-attr]
        step_id = str(row["step_id"])
        step = steps_by_id.get(step_id.removesuffix(":active_link"), {})
        outcomes.append(
            _classify_managed(
                row,
                step,
                toolchain_bin=toolchain_bin,
                owners=owners,
                operator_paths=operator_paths,
            )
        )
    for row in document["discovered"]:  # type: ignore[union-attr]
        outcomes.append(_classify_discovered(row, links=links, operator_paths=operator_paths))
    for row in document["credentials"]:  # type: ignore[union-attr]
        if isinstance(row["lstat"], Mapping):
            outcomes.append(_classify_credential(row, operator_paths=operator_paths))
    existing = {outcome.path for outcome in outcomes if outcome.exists}
    for path in sorted(operator_paths - existing):
        failures.append(
            f"quarantine_paths: {path} is not an existing object in the inventory"
        )

    candidates: dict[str, _Outcome] = {}
    for outcome in outcomes:
        if outcome.failure is None and outcome.disposition in _QUARANTINE_DISPOSITIONS:
            candidates.setdefault(outcome.path, outcome)
    quarantine_roots = frozenset(_minimal_roots(list(candidates)))
    for outcome in outcomes:
        if outcome.exists:
            outcome.covered_by = _covering_root(outcome.path, quarantine_roots)
    for outcome in outcomes:
        if outcome.covered_by is not None:
            summary["covered"] += 1
            continue
        if outcome.failure is not None:
            failures.append(outcome.failure)
            continue
        if outcome.disposition == "create":
            summary["create"] += 1
            continue
        assert outcome.disposition is not None
        summary[outcome.disposition] += 1
        if outcome.disposition in {"adopt", "adopt-in-place"}:
            assert outcome.step_id is not None
            if outcome.step_id in adopted:
                failures.append(
                    f"adopt: {outcome.path} and another object both claim {outcome.step_id}"
                )
            adopted[outcome.step_id] = inventory_row_sha256(outcome.section, outcome.row)
    for root in sorted(quarantine_roots):
        content = candidates[root].row.get("content")
        if isinstance(content, Mapping) and content.get("mountpoint") is True:
            failures.append(
                f"mountpoint: {root} is a mountpoint; a rename cannot move it into quarantine"
            )

    # services must not load cortex units or drop-ins from outside the plan
    services = document["services"]
    assert isinstance(services, Mapping)
    for unit, row in sorted(services.items()):
        managed_unit = _join(roots["systemd"], unit)
        fragment = row["fragment_path"]
        if fragment and fragment != managed_unit:
            failures.append(
                f"service: {unit} is loaded from {fragment}, not from {managed_unit}, "
                "the unit the plan manages"
            )
        for dropin in row["drop_in_paths"]:
            owner_directory = posixpath.basename(posixpath.dirname(str(dropin)))
            if not owner_directory.startswith("cortex"):
                continue  # a distribution-wide drop-in, not cortex state
            if _covering_root(str(dropin), quarantine_roots) is None and dropin not in quarantine_roots:
                failures.append(
                    f"service: drop-in {dropin} for {unit} stays outside the quarantine; "
                    "it would keep altering the unit the plan installs"
                )

    # writable census: stable, and only declared or excepted paths
    census = document["census"]
    assert isinstance(census, Mapping)
    for principal, row in sorted(census.items()):
        if row["status"] == "unstable":
            failures.append(
                f"census: {principal} entries changed while they were checked (unstable): "
                f"{', '.join(row['unstable'])}; recapture with the services stopped"
            )
        for item in row["writable_outside_declared"]:
            path = str(item["path"])
            if not any(
                exception["principal"] == principal and _within(path, exception["path"])
                for exception in exceptions
            ):
                failures.append(
                    f"census: {principal} can write {path}, which is neither a "
                    "plan-declared writable asset nor a legacy_adoption.census_exceptions entry"
                )

    if failures:
        raise LegacyAdoptionPlanError(failures)

    quarantine_rows: list[dict[str, object]] = []
    quarantine_steps: list[dict[str, object]] = []
    for root in sorted(quarantine_roots):
        outcome = candidates[root]
        step = _quarantine_step(
            outcome,
            quarantine_root=quarantine_root,
            inventory_sha256=inventory.inventory_sha256,
        )
        quarantine_steps.append(step)
        quarantine_rows.append(
            {
                "path": root,
                "disposition": outcome.disposition,
                "reason": outcome.reason,
                "row_sha256": step["row_sha256"],
                "covers": sorted(
                    {other.path for other in outcomes if other.covered_by == root}
                ),
            }
        )

    bound = deepcopy(dict(plan))
    order = bound["apply_order"]
    assert isinstance(order, list)
    bound["apply_order"] = _place_quarantine_steps(order, quarantine_steps)
    bound["legacy_adoption"] = {
        "schema_version": LEGACY_PLAN_SCHEMA_VERSION,
        "inventory_sha256": inventory.inventory_sha256,
        "scope_sha256": inventory.scope_sha256,
        "host_binding_sha256": inventory.host_binding_sha256,
        "host_overlay_sha256": None if overlay_record is None else overlay_record["sha256"],
        "quarantine_root": quarantine_root,
        "adopted": dict(sorted(adopted.items())),
        "quarantine": quarantine_rows,
        "census_exceptions": exceptions,
        "summary": summary,
    }
    bound["receipt_path"] = str(canonical_receipt_path(bound))
    _assert_managed_parent_topology(bound)
    validate_legacy_adoption_plan(bound, bound["apply_order"])  # type: ignore[arg-type]
    return bound


def _plan_sorted_unique(keys: Sequence[object], label: str) -> None:
    if any(left >= right for left, right in zip(keys, keys[1:])):  # type: ignore[operator]
        raise InstallPlanError(f"plan {label} must be sorted and unique")


def _adoptable_step(step: Mapping[str, object]) -> bool:
    kind = step.get("kind")
    return (
        kind == "account"
        or (kind == "asset" and step.get("asset_type") in {"directory", "symlink"})
        or (kind == "systemctl" and step.get("action") == "enable")
    )


def _validate_quarantine_expected(value: object, label: str) -> None:
    if not isinstance(value, Mapping):
        raise InstallPlanError(f"{label} expected must be an object")
    keys = set(value)
    extra = keys - set(_EXPECTED_BASE_KEYS)
    if not set(_EXPECTED_BASE_KEYS) <= keys or len(extra) > 1 or not extra <= {
        "sha256",
        "link_target",
    }:
        raise InstallPlanError(f"{label} expected keys are invalid")
    if value["type"] not in _QUARANTINABLE_TYPES:
        raise InstallPlanError(f"{label} expected type is invalid")
    if not all(type(value[key]) is int for key in ("uid", "gid", "dev", "ino")):
        raise InstallPlanError(f"{label} expected numbers are invalid")
    if not isinstance(value["mode"], str) or not re.fullmatch(r"[0-7]{4}", value["mode"]):
        raise InstallPlanError(f"{label} expected mode is invalid")
    if "sha256" in value and (value["type"] != "file" or not _is_sha256(value["sha256"])):
        raise InstallPlanError(f"{label} expected sha256 is invalid")
    if "link_target" in value and (
        value["type"] != "symlink" or not isinstance(value["link_target"], str)
    ):
        raise InstallPlanError(f"{label} expected link_target is invalid")


def validate_legacy_adoption_plan(
    plan: Mapping[str, object], steps: Sequence[Mapping[str, object]]
) -> None:
    """Revalidate a plan's ``legacy_adoption`` block against its own steps.

    Dispositions cannot be re-derived without the inventory; this proves the
    block is internally consistent and bound to the plan: scope, overlay,
    quarantine root and destinations, adopted step ids, and an exact
    bijection between quarantine rows and ``legacy-quarantine`` steps.
    """

    block = plan.get("legacy_adoption")
    quarantine_steps = [step for step in steps if step.get("kind") == QUARANTINE_STEP_KIND]
    if block is None:
        if quarantine_steps:
            raise InstallPlanError("legacy-quarantine steps require a legacy_adoption block")
        return
    if plan.get("legacy_policy") != "quarantine":
        raise InstallPlanError("a legacy_adoption block requires legacy_policy: quarantine")
    if not isinstance(block, Mapping) or set(block) != _LEGACY_BLOCK_KEYS:
        raise InstallPlanError("plan legacy_adoption keys do not match schema v1")
    if block["schema_version"] != LEGACY_PLAN_SCHEMA_VERSION:
        raise InstallPlanError("plan legacy_adoption schema_version must be 1")
    for field in ("inventory_sha256", "scope_sha256", "host_binding_sha256"):
        if not _is_sha256(block[field]):
            raise InstallPlanError(f"plan legacy_adoption.{field} is invalid")
    overlay = block["host_overlay_sha256"]
    if overlay != plan.get("host_overlay_sha256") or (
        overlay is not None and not _is_sha256(overlay)
    ):
        raise InstallPlanError(
            "plan legacy_adoption.host_overlay_sha256 does not match the plan host overlay"
        )
    scope = legacy_scope(plan)
    if scope_sha256(scope) != block["scope_sha256"]:
        raise InstallPlanError("plan legacy_adoption.scope_sha256 does not match the plan scope")
    quarantine_root = block["quarantine_root"]
    if not _normalized_absolute(quarantine_root) or _quarantine_root_problems(
        str(quarantine_root), scope
    ):
        raise InstallPlanError("plan legacy_adoption.quarantine_root is invalid")

    exceptions = block["census_exceptions"]
    principals = set(scope["census"]["principals"])  # type: ignore[index]
    if type(exceptions) is not list or any(
        not isinstance(row, Mapping)
        or set(row) != {"path", "principal"}
        or not _normalized_absolute(row["path"])
        or row["principal"] not in principals
        for row in exceptions
    ):
        raise InstallPlanError("plan legacy_adoption.census_exceptions are invalid")
    _plan_sorted_unique(
        [(row["path"], row["principal"]) for row in exceptions],
        "legacy_adoption.census_exceptions",
    )

    by_id = {str(step.get("step_id")): step for step in steps}
    adopted = block["adopted"]
    if not isinstance(adopted, Mapping):
        raise InstallPlanError("plan legacy_adoption.adopted must be an object")
    for step_id, digest in adopted.items():
        step = by_id.get(str(step_id))
        if step is None or not _adoptable_step(step) or not _is_sha256(digest):
            raise InstallPlanError(
                f"plan legacy_adoption.adopted names a step the plan cannot adopt: {step_id}"
            )

    rows = block["quarantine"]
    if type(rows) is not list:
        raise InstallPlanError("plan legacy_adoption.quarantine must be a list")
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or set(row) != _QUARANTINE_ROW_KEYS
            or not _normalized_absolute(row["path"])
            or row["disposition"] not in _QUARANTINE_DISPOSITIONS
            or row["reason"] not in QUARANTINE_REASONS
            or not _is_sha256(row["row_sha256"])
            or type(row["covers"]) is not list
            or not all(
                _normalized_absolute(item) and str(item).startswith(str(row["path"]) + "/")
                for item in row["covers"]
            )
        ):
            raise InstallPlanError("plan legacy_adoption.quarantine rows are invalid")
        _plan_sorted_unique(row["covers"], f"legacy_adoption.quarantine {row['path']} covers")
    paths = [str(row["path"]) for row in rows]
    _plan_sorted_unique(paths, "legacy_adoption.quarantine")
    if any(_within(other, path) for path in paths for other in paths if other != path):
        raise InstallPlanError("plan legacy_adoption.quarantine paths must not nest")

    steps_by_path: dict[str, Mapping[str, object]] = {}
    for step in quarantine_steps:
        path = step.get("path")
        if not isinstance(path, str) or path in steps_by_path:
            raise InstallPlanError("legacy-quarantine steps require unique paths")
        steps_by_path[path] = step
    if set(steps_by_path) != set(paths):
        raise InstallPlanError(
            "legacy quarantine rows and legacy-quarantine steps are not a bijection"
        )
    managed_paths = {
        str(step["path"])
        for step in steps
        if step.get("kind") in {"asset", "repository"} and isinstance(step.get("path"), str)
    }
    for row in rows:
        path = str(row["path"])
        step = steps_by_path[path]
        label = f"legacy-quarantine step {path}"
        if set(step) != _QUARANTINE_STEP_KEYS or step["step_id"] != f"{QUARANTINE_STEP_KIND}:{path}":
            raise InstallPlanError(f"{label} does not match schema v1")
        if step["destination"] != quarantine_destination(
            str(quarantine_root), str(block["inventory_sha256"]), path
        ):
            raise InstallPlanError(
                f"{label} destination is not bound to the quarantine root and inventory"
            )
        _validate_quarantine_expected(step["expected"], label)
        if step["row_sha256"] != row["row_sha256"]:
            raise InstallPlanError(f"{label} row_sha256 does not match its quarantine row")
        if (
            step["operations"] != list(QUARANTINE_OPERATIONS)
            or step["rollback_policy"] != QUARANTINE_ROLLBACK_POLICY
        ):
            raise InstallPlanError(f"{label} operations or rollback policy are invalid")
        if step["desired_sha256"] != _quarantine_step_digest(step):
            raise InstallPlanError(f"{label} desired_sha256 does not match the step")
        if (row["disposition"] == "quarantine-then-create") != (path in managed_paths):
            raise InstallPlanError(
                f"{label} disposition {row['disposition']} does not match the plan's steps"
            )
    for step_id in adopted:
        target = by_id[str(step_id)].get("path")
        if isinstance(target, str) and any(_within(target, path) for path in paths):
            raise InstallPlanError(
                f"plan legacy_adoption.adopted step {step_id} lies inside a quarantined path"
            )

    summary = block["summary"]
    if (
        not isinstance(summary, Mapping)
        or set(summary) != set(SUMMARY_KEYS)
        or not all(type(value) is int and value >= 0 for value in summary.values())
    ):
        raise InstallPlanError("plan legacy_adoption.summary is invalid")
