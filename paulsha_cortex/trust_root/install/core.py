"""Transactional trust-root installation primitives.

Planning in this module is deliberately rootless and deterministic.  Mutation is
performed only through an explicit backend seam so the same transaction rules can
be exercised without inspecting or changing the host.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import tempfile
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, MutableMapping, Protocol, Sequence
from urllib.parse import parse_qsl, urlsplit

from .. import permgen, registry


class InstallError(RuntimeError):
    """Base class for trust-root installation failures."""


class InstallPlanError(InstallError):
    pass


class UnsafeInstallPathError(InstallPlanError):
    pass


class AccountCollisionError(InstallError):
    pass


class InstallDriftError(InstallError):
    pass


class CredentialImportError(InstallError):
    pass


class ActivationError(InstallError):
    pass


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _as_plan_dict(plan: Mapping[str, object]) -> dict[str, object]:
    return deepcopy(dict(plan))


def canonical_plan_bytes(plan: Mapping[str, object]) -> bytes:
    """Return the single canonical byte representation used by confirmation."""

    return _canonical_bytes(_as_plan_dict(plan))


def plan_sha256(plan: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_regular_file(path: Path, *, label: str) -> Path:
    candidate = path.expanduser()
    try:
        observed = candidate.lstat()
    except OSError as exc:
        raise InstallPlanError(f"{label} cannot be read: {candidate}: {exc}") from exc
    if stat.S_ISLNK(observed.st_mode):
        raise UnsafeInstallPathError(f"{label} must not be a symlink: {candidate}")
    if not stat.S_ISREG(observed.st_mode):
        raise InstallPlanError(f"{label} must be a regular file: {candidate}")
    _reject_symlink_ancestors(candidate, label=label, include_leaf=False)
    return candidate.absolute()


def _reject_symlink_ancestors(
    path: Path, *, label: str, include_leaf: bool = True
) -> None:
    current = path if include_leaf else path.parent
    for candidate in (current, *current.parents):
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UnsafeInstallPathError(
                f"cannot inspect {label} path component {candidate}: {exc}"
            ) from exc
        if stat.S_ISLNK(mode):
            raise UnsafeInstallPathError(
                f"{label} path contains a symlink component: {candidate}"
            )


_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _open_directory_chain(
    path: Path,
    *,
    create: bool = False,
    create_mode: int = 0o700,
    authority: list[tuple[int, int]] | None = None,
) -> int:
    """Open an absolute directory component-wise and optionally record authority."""

    if not path.is_absolute() or ".." in path.parts:
        raise UnsafeInstallPathError(f"unsafe directory authority path: {path}")
    descriptor = os.open("/", _DIRECTORY_OPEN_FLAGS)
    if authority is not None:
        authority.clear()
        root = os.fstat(descriptor)
        authority.append((root.st_dev, root.st_ino))
    try:
        for component in path.parts[1:]:
            try:
                next_descriptor = os.open(
                    component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, create_mode, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(
                    component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor
                )
            except OSError as exc:
                raise UnsafeInstallPathError(
                    f"directory authority contains an unsafe component: {path}"
                ) from exc
            os.close(descriptor)
            descriptor = next_descriptor
            if authority is not None:
                observed = os.fstat(descriptor)
                authority.append((observed.st_dev, observed.st_ino))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_parent_directory(
    path: Path,
    *,
    create: bool = False,
    create_mode: int = 0o700,
    authority: list[tuple[int, int]] | None = None,
) -> tuple[int, str]:
    if not path.is_absolute() or ".." in path.parts or not path.name:
        raise UnsafeInstallPathError(f"unsafe authority leaf path: {path}")
    return (
        _open_directory_chain(
            path.parent,
            create=create,
            create_mode=create_mode,
            authority=authority,
        ),
        path.name,
    )


def _assert_fd_path_binding(
    path: Path,
    descriptor: int,
    *,
    directory: bool,
    parent_authority: Sequence[tuple[int, int]] | None = None,
) -> None:
    """Fail if the canonical pathname no longer names the held inode."""

    current_authority: list[tuple[int, int]] | None = (
        [] if parent_authority is not None else None
    )
    parent_fd, leaf = _open_parent_directory(path, authority=current_authority)
    flags = (
        getattr(os, "O_PATH", os.O_RDONLY)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        if parent_authority is not None and tuple(current_authority or ()) != tuple(
            parent_authority
        ):
            raise UnsafeInstallPathError(
                f"authority path was replaced while held: {path}"
            )
        try:
            current_fd = os.open(leaf, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise UnsafeInstallPathError(
                f"authority path changed while held: {path}"
            ) from exc
        try:
            held = os.fstat(descriptor)
            current = os.fstat(current_fd)
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                raise UnsafeInstallPathError(
                    f"authority path was replaced while held: {path}"
                )
        finally:
            os.close(current_fd)
    finally:
        os.close(parent_fd)


def _read_fd_bytes(descriptor: int) -> bytes:
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        return stream.read()


def _validate_absolute_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise UnsafeInstallPathError(f"{label} must be a non-empty absolute path")
    raw = Path(value)
    if not raw.is_absolute():
        raise UnsafeInstallPathError(f"{label} must be absolute: {value!r}")
    if ".." in raw.parts:
        raise UnsafeInstallPathError(f"{label} contains a lexical path escape: {value}")
    _reject_symlink_ancestors(raw, label=label)
    return str(raw)


_FORBIDDEN_CONFIG_FIELDS = frozenset(
    {
        "credential_bytes",
        "github_token",
        "operator_home",
        "password",
        "secret",
        "token",
    }
)
_FORBIDDEN_CONFIG_FIELD_FRAGMENTS = (
    "api_key",
    "apikey",
    "assertion",
    "auth",
    "bearer",
    "cookie",
    "credential",
    "password",
    "passphrase",
    "private",
    "secret",
    "session",
    "token",
)

_INSTALL_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "scheme",
        "instance",
        "repo_identity",
        "operator_account",
        "external_reader_account",
        "accounts",
        "service_accounts",
        "roots",
        "source_repositories",
        "legacy_policy",
        "providers",
        "toolchain",
    }
)
_REPO_IDENTITY_KEYS = frozenset({"remote", "commit"})
_ACCOUNT_CONFIG_KEYS = frozenset({"uid", "gid", "home", "shell"})
_ROOT_CONFIG_KEYS = frozenset({"deploy", "state", "systemd", "polkit"})
_PROVIDER_KEYS = frozenset({"builder", "reviewer-planner", "manager"})
_PROVIDER_ALLOWLIST = {
    "builder": frozenset({"codex"}),
    "reviewer-planner": frozenset({"codex", "agy", "copilot"}),
    "manager": frozenset({"github"}),
}


def _require_exact_keys(
    value: object,
    *,
    label: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InstallPlanError(f"configuration {label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise InstallPlanError(f"configuration {label} keys must be strings")
    actual = set(value)
    unknown = sorted(actual - required - optional)
    missing = sorted(required - actual)
    if unknown or missing:
        details: list[str] = []
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        if missing:
            details.append("missing=" + ",".join(missing))
        raise InstallPlanError(
            f"configuration {label} keys must match the schema: " + "; ".join(details)
        )
    return value


def _validate_repository_remote(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise InstallPlanError("repo_identity.remote is required")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise InstallPlanError("repo_identity.remote is invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
    ):
        raise InstallPlanError(
            "repo_identity.remote must be an HTTPS URL without userinfo, query, or fragment"
        )
    return value


def _validate_install_config_schema(config: Mapping[str, object]) -> None:
    _require_exact_keys(config, label="top-level", required=_INSTALL_CONFIG_KEYS)
    repo = _require_exact_keys(
        config.get("repo_identity"),
        label="repo_identity",
        required=_REPO_IDENTITY_KEYS,
    )
    _validate_repository_remote(repo.get("remote"))

    accounts = config.get("accounts")
    if not isinstance(accounts, Mapping):
        raise InstallPlanError("configuration accounts must be an object")
    for name, row in accounts.items():
        _require_exact_keys(
            row,
            label=f"accounts.{name}",
            required=_ACCOUNT_CONFIG_KEYS,
        )

    service_accounts = config.get("service_accounts")
    if not isinstance(service_accounts, Mapping):
        raise InstallPlanError("configuration service_accounts must be an object")
    for name, row in service_accounts.items():
        _require_exact_keys(
            row,
            label=f"service_accounts.{name}",
            required=_ACCOUNT_CONFIG_KEYS,
        )

    _require_exact_keys(
        config.get("roots"), label="roots", required=_ROOT_CONFIG_KEYS
    )
    providers = _require_exact_keys(
        config.get("providers"),
        label="providers",
        required=_PROVIDER_KEYS,
    )
    for principal in sorted(_PROVIDER_KEYS):
        configured = providers[principal]
        if type(configured) is not list:
            raise InstallPlanError(f"providers.{principal} must be a list")
        if not configured:
            raise InstallPlanError(f"providers.{principal} must not be empty")
        if any(type(provider) is not str for provider in configured):
            raise InstallPlanError(
                f"providers.{principal} entries must be strings"
            )
        if len(configured) != len(set(configured)):
            raise InstallPlanError(
                f"providers.{principal} contains a duplicate"
            )
        if any(provider not in _PROVIDER_ALLOWLIST[principal] for provider in configured):
            raise InstallPlanError(
                f"provider is not allowed for {principal}"
            )

    toolchain = config.get("toolchain")
    if not isinstance(toolchain, Mapping) or not toolchain:
        raise InstallPlanError("configuration toolchain must be a non-empty object")
    for name, raw in toolchain.items():
        _require_exact_keys(
            raw,
            label=f"toolchain.{name}",
            required=frozenset({"version", "sha256"}),
            optional=frozenset({"shape", "entrypoint"}),
        )


def _credential_bearing_url(value: str) -> bool:
    """Return whether an HTTP(S) URL carries userinfo or secret-like parameters."""

    if "://" not in value:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        # A malformed URL-shaped value is not safe configuration material.
        return True
    if parsed.scheme.casefold() not in {"http", "https"}:
        return False
    if parsed.username is not None or parsed.password is not None:
        return True
    sensitive = _FORBIDDEN_CONFIG_FIELD_FRAGMENTS
    parameters = (
        *parse_qsl(parsed.query, keep_blank_values=True),
        *parse_qsl(parsed.fragment, keep_blank_values=True),
    )
    return any(
        any(fragment in key.casefold() for fragment in sensitive)
        for key, _unused in parameters
    )


def _reject_sensitive_config(value: object, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.casefold()
            if (
                normalized in _FORBIDDEN_CONFIG_FIELDS
                or any(fragment in normalized for fragment in _FORBIDDEN_CONFIG_FIELD_FRAGMENTS)
                or normalized.endswith(
                ("_password", "_secret", "_token")
                )
            ):
                raise InstallPlanError(
                    f"configuration field {'.'.join((*path, key))} is forbidden"
                )
            _reject_sensitive_config(child, path=(*path, key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_sensitive_config(child, path=(*path, str(index)))
    elif isinstance(value, str) and _credential_bearing_url(value):
        label = ".".join(path) or "<root>"
        raise InstallPlanError(
            f"configuration field {label} contains a credential-bearing URL"
        )


def _configured_scheme(config: Mapping[str, object]) -> permgen.UidScheme:
    if config.get("scheme") != "four-way":
        raise InstallPlanError("new trust-root installs require scheme=four-way")
    operator = config.get("operator_account")
    external = config.get("external_reader_account")
    if not isinstance(operator, str) or not operator:
        raise InstallPlanError("operator_account is required")
    if not isinstance(external, str) or not external:
        raise InstallPlanError("external_reader_account is required; use <absent> explicitly")
    return permgen.replace(
        permgen.FOUR_WAY_SCHEME,
        operator_account=operator,
        external_reader_account=external,
    )


def _account_rows(config: Mapping[str, object], scheme: permgen.UidScheme) -> list[dict[str, object]]:
    raw_accounts = config.get("accounts")
    if not isinstance(raw_accounts, Mapping):
        raise InstallPlanError("accounts must be an object")
    required = {
        account
        for principal in (
            registry.Principal.MANAGER,
            registry.Principal.REVIEWER,
            registry.Principal.BUILDER,
            registry.Principal.GATE,
        )
        if (account := scheme.resolve(principal)) is not None
    }
    if set(raw_accounts) != required:
        raise InstallPlanError(
            f"four-way accounts must be exactly {sorted(required)}"
        )
    rows: list[dict[str, object]] = []
    seen_uids: set[int] = set()
    seen_gids: set[int] = set()
    for name in sorted(required):
        raw = raw_accounts.get(name)
        if not isinstance(raw, Mapping):
            raise InstallPlanError(f"account {name} must be an object")
        uid = raw.get("uid")
        gid = raw.get("gid")
        shell = raw.get("shell")
        if not isinstance(uid, int) or uid <= 0 or uid in seen_uids:
            raise InstallPlanError(f"account {name} has an invalid or duplicate uid")
        if not isinstance(gid, int) or gid <= 0 or gid in seen_gids:
            raise InstallPlanError(f"account {name} has an invalid or duplicate gid")
        if not isinstance(shell, str) or not shell.startswith("/"):
            raise InstallPlanError(f"account {name} shell must be absolute")
        home = _validate_absolute_path(raw.get("home"), label=f"accounts.{name}.home")
        rows.append({"name": name, "uid": uid, "gid": gid, "home": home, "shell": shell})
        seen_uids.add(uid)
        seen_gids.add(gid)
    return rows


def _service_account_rows(config: Mapping[str, object]) -> list[dict[str, object]]:
    raw_accounts = config.get("service_accounts")
    if not isinstance(raw_accounts, Mapping) or set(raw_accounts) != {
        permgen.EGRESS_PROXY.account
    }:
        raise InstallPlanError(
            "service_accounts must declare exactly the dedicated cortex-egress account"
        )
    name = permgen.EGRESS_PROXY.account
    raw = raw_accounts[name]
    if not isinstance(raw, Mapping):
        raise InstallPlanError(f"service account {name} must be an object")
    uid = raw.get("uid")
    gid = raw.get("gid")
    shell = raw.get("shell")
    if not isinstance(uid, int) or uid <= 0:
        raise InstallPlanError(f"service account {name} has an invalid uid")
    if not isinstance(gid, int) or gid <= 0:
        raise InstallPlanError(f"service account {name} has an invalid gid")
    if not isinstance(shell, str) or not shell.startswith("/"):
        raise InstallPlanError(f"service account {name} shell must be absolute")
    home = _validate_absolute_path(raw.get("home"), label=f"service_accounts.{name}.home")
    return [{"name": name, "uid": uid, "gid": gid, "home": home, "shell": shell}]


def _layout_from_config(config: Mapping[str, object], accounts: Sequence[Mapping[str, object]]) -> tuple[permgen.PathLayout, dict[str, str]]:
    raw_roots = config.get("roots")
    if not isinstance(raw_roots, Mapping):
        raise InstallPlanError("roots must be an object")
    roots = {
        name: _validate_absolute_path(raw_roots.get(name), label=f"roots.{name}")
        for name in ("deploy", "state", "systemd", "polkit")
    }
    instance = config.get("instance", "cortex")
    if not isinstance(instance, str) or not instance:
        raise InstallPlanError("instance is required")
    source_repos = config.get("source_repositories")
    if not isinstance(source_repos, list) or not source_repos or not all(
        isinstance(row, str) and row and "/" not in row and row not in {".", ".."}
        for row in source_repos
    ):
        raise InstallPlanError("source_repositories must contain safe repo slugs")
    homes = [Path(str(row["home"])) for row in accounts]
    home_root = os.path.commonpath([str(path.parent) for path in homes])
    layout = permgen.PathLayout(
        agents_root=roots["state"],
        worktree_root=f"{roots['state']}/worktree",
        deploy_root=roots["deploy"],
        instance=instance,
        home_root=home_root,
        source_repo_slugs=tuple(source_repos),
    )
    mismatched_homes = [
        str(row["name"])
        for row in accounts
        if layout.home_of(str(row["name"])) != str(row["home"])
    ]
    if mismatched_homes:
        raise InstallPlanError(
            "account homes must equal the PathLayout-derived paths: "
            + ", ".join(sorted(mismatched_homes))
        )
    return layout, roots


def _manager_environment(
    *,
    config: Mapping[str, object],
    scheme: permgen.UidScheme,
    layout: permgen.PathLayout,
) -> str:
    remote = config.get("repo_identity", {}).get("remote") if isinstance(
        config.get("repo_identity"), Mapping
    ) else None
    if not isinstance(remote, str) or not remote:
        raise InstallPlanError("repo_identity.remote is required")
    identity = remote.removesuffix(".git").rstrip("/").rsplit("/", 2)
    if len(identity) < 2:
        raise InstallPlanError("repo_identity.remote cannot be converted to owner/repo")
    repo_slug = layout.source_repo_slugs[0]
    values = {
        "PSC_INSTANCE": layout.instance,
        "PSC_AGENTS_ROOT": layout.agents_root,
        "PSC_PROJECT_CONFIG_ROOT": layout.project_config_root,
        "PSC_WORKTREE_ROOT": layout.worktree_root,
        "PSC_DEGRADED_OPERATION": "per-case-approval",
        "PSC_REPO_ROOT": layout.source_repo_paths()[0],
        "PSC_REPO_IDENTITY": "/".join(identity[-2:]),
        "PSC_MANAGER_EXECUTOR": "codex",
        "PSC_MANAGER_INTERVAL_SECONDS": "60",
        "PSC_MANAGER_GITHUB_INTERVAL_MS": "600000",
        "PSC_GATE_CMD_PYTEST": "python3 -m pytest -q",
        "PSC_GATE_TIMEOUT": "900",
        "PSC_PREFLIGHT_CMD": layout.preflight_command_value(),
        "PSC_JOB_RUNNER": "systemd-template",
        "PSC_BUILDER_ACCOUNT": str(scheme.resolve(registry.Principal.BUILDER)),
        "PSC_BUILDER_HOME": layout.home_of(
            str(scheme.resolve(registry.Principal.BUILDER))
        ),
        "PSC_BUILDER_PATH": layout.job_path_value(),
        "PSC_REVIEWER_ACCOUNT": str(scheme.resolve(registry.Principal.REVIEWER)),
        "PSC_REVIEWER_HOME": layout.home_of(
            str(scheme.resolve(registry.Principal.REVIEWER))
        ),
        "PSC_REVIEWER_PATH": layout.job_path_value(),
        "PSC_GATE_ACCOUNT": str(scheme.resolve(registry.Principal.GATE)),
        "PSC_GATE_HOME": layout.home_of(str(scheme.resolve(registry.Principal.GATE))),
        "PSC_GATE_PATH": layout.job_path_value(),
        "PSC_GATE_PYTHON": f"{layout.venv_root}/bin/python3",
        "PSC_GATE_HARDENING_PROFILE": "strict",
    }
    if not repo_slug:
        raise InstallPlanError("at least one source repository is required")
    return "".join(
        f'{key}={json.dumps(value, ensure_ascii=False)}\n'
        for key, value in values.items()
    )


def _artifact_dict(
    *, content: str, path: str, owner: str = "root", group: str = "root", mode: str = "0644"
) -> dict[str, str]:
    return {
        "path": path,
        "owner": owner,
        "group": group,
        "mode": mode,
        "content": content,
    }


def _generated_inventory(
    scheme: permgen.UidScheme,
    layout: permgen.PathLayout,
    roots: Mapping[str, str],
    permission_plan: permgen.PermissionPlan,
    toolchain: object,
    config: Mapping[str, object],
) -> dict[str, dict[str, dict[str, str]]]:
    units = [
        permgen.build_egress_proxy_unit(scheme, layout),
        permgen.build_manager_unit(scheme, layout, permission_plan),
        permgen.build_monitor_unit(scheme, layout, permission_plan),
    ]
    for principal in permgen.downgraded_job_principals(scheme):
        for profile in permgen.HARDENING_PROFILES:
            units.append(
                permgen.build_job_unit(
                    scheme,
                    layout,
                    principal=principal,
                    plan=permission_plan,
                    profile=profile,
                )
            )
    unit_inventory = {
        unit.unit_name: _artifact_dict(
            content=unit.content,
            path=str(Path(roots["systemd"]) / unit.unit_name),
        )
        for unit in units
    }

    shim = permgen.build_job_shim(scheme, layout)
    shim_inventory = {
        Path(shim.install_path).name: _artifact_dict(
            content=shim.content,
            path=shim.install_path,
            owner=shim.owner,
            group=shim.group,
            mode=shim.mode_str,
        )
    }
    rule = permgen.build_polkit_rule(scheme, layout)
    rule_path = str(Path(roots["polkit"]) / Path(rule.install_path).name)
    polkit_inventory = {
        Path(rule.install_path).name: _artifact_dict(content=rule.content, path=rule_path)
    }

    gitconfigs: dict[str, dict[str, str]] = {}
    for principal, asset_id in permgen.ACCOUNT_GITCONFIG_ASSETS.items():
        generated = permgen.build_account_gitconfig(scheme, layout, principal)
        gitconfigs[asset_id] = _artifact_dict(
            content=generated.content,
            path=generated.install_path,
            owner=generated.owner,
            group=generated.group,
            mode=generated.mode_str,
        )
    gitconfigs["manager-gh-config"] = _artifact_dict(
        content="git_protocol: https\nprompt: disabled\n",
        path=layout.gh_settings_of(layout.manager_account),
    )

    resolved_asset_paths = layout.asset_paths()
    enforcement = {
        asset_id: _artifact_dict(
            content=permgen.CODEX_HOOKS_SEED_CONTENT + "\n",
            path=resolved_asset_paths[asset_id],
        )
        for asset_id in sorted(permgen.ENFORCEMENT_LEAF_ASSETS)
    }

    wrappers: dict[str, dict[str, str]] = {}
    if isinstance(toolchain, Mapping):
        for raw_name in sorted(toolchain):
            name = str(raw_name)
            if not name or name in {".", ".."} or "/" in name:
                raise InstallPlanError(f"unsafe toolchain program name: {name!r}")
            wrapper_path = f"{layout.toolchain_bin}/{name}"
            executable = f"{layout.toolchain_lib}/{name}"
            tool = toolchain[raw_name]
            if not isinstance(tool, Mapping):
                raise InstallPlanError(f"toolchain entry must be an object: {name}")
            shape = tool.get("shape", "file")
            if shape == "file":
                content = f'#!/bin/sh\nexec "{executable}" "$@"\n'
            elif shape == "tree":
                entrypoint = tool.get("entrypoint")
                if not isinstance(entrypoint, str):
                    raise InstallPlanError(f"tree toolchain entrypoint is required: {name}")
                relative = PurePosixPath(entrypoint)
                if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                    raise InstallPlanError(f"unsafe tree toolchain entrypoint: {name}")
                content = (
                    "#!/bin/sh\n"
                    f'exec /usr/bin/node "{executable}/{relative.as_posix()}" "$@"\n'
                )
            else:
                raise InstallPlanError(f"unsupported toolchain shape for {name}: {shape}")
            wrappers[name] = _artifact_dict(
                content=content, path=wrapper_path, mode="0755"
            )
    environment = {
        Path(layout.env_file).name: _artifact_dict(
            content=_manager_environment(config=config, scheme=scheme, layout=layout),
            path=layout.env_file,
        )
    }
    return {
        "units": unit_inventory,
        "shim": shim_inventory,
        "polkit": polkit_inventory,
        "gitconfigs": gitconfigs,
        "toolchain_wrappers": wrappers,
        "environment": environment,
        "enforcement": enforcement,
    }


def _desired_digest(step: Mapping[str, object]) -> str:
    content = step.get("content")
    if isinstance(content, str):
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    raw_acls = step.get("acls", [])
    normalized_acls = [
        {
            "account": row.get("account"),
            "perms": str(row.get("perms", "")).replace("X", "x").replace("-", ""),
            "default": bool(row.get("default", False)),
        }
        for row in raw_acls
        if isinstance(row, Mapping)
    ]
    semantic = {
        "path": step.get("path"),
        "owner": step.get("owner"),
        "group": step.get("group"),
        "mode": step.get("mode"),
        "acls": normalized_acls,
        "asset_type": step.get("asset_type"),
        "target": step.get("target"),
        "commit": step.get("commit"),
        "remote": step.get("remote"),
        "source_sha256": step.get("source_sha256"),
        "adoption_policy": step.get("adoption_policy"),
        "durable": step.get("durable"),
    }
    return hashlib.sha256(_canonical_bytes(semantic)).hexdigest()


def _account_digest(account: Mapping[str, object]) -> str:
    """Hash the complete immutable service-account identity."""

    return hashlib.sha256(
        _canonical_bytes(
            {
                key: account.get(key)
                for key in ("name", "uid", "gid", "home", "login_program")
            }
        )
    ).hexdigest()


def _apply_steps(
    *,
    scaffolds: Sequence[Mapping[str, object]],
    assets: Sequence[Mapping[str, object]],
    generated: Mapping[str, Mapping[str, Mapping[str, str]]],
) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    directory_steps: list[dict[str, object]] = []
    symlink_steps: list[dict[str, object]] = []
    for scaffold in scaffolds:
        step: dict[str, object] = {
            "step_id": f"scaffold:{scaffold['path']}",
            "kind": "asset",
            "asset_type": "directory",
            "path": scaffold["path"],
            "owner": scaffold["owner"],
            "group": scaffold["group"],
            "mode": scaffold["mode"],
            "acls": [],
            "operations": ["snapshot", "chown", "chmod", "set_acl"],
            "durable": False,
        }
        step["desired_sha256"] = _desired_digest(step)
        directory_steps.append(step)
    for asset in assets:
        path = asset.get("path")
        if not isinstance(path, str) or "<job-id>" in path:
            continue
        if bool(asset.get("is_symlink")):
            target = asset.get("symlink_target")
            if not isinstance(target, str):
                raise InstallPlanError(f"symlink asset lacks target: {asset.get('asset_id')}")
            step = {
                "step_id": f"asset:{asset['asset_id']}",
                "kind": "asset",
                "asset_type": "symlink",
                "path": path,
                "target": target,
                "owner": asset["owner"],
                "group": asset["group"],
                "operations": ["snapshot", "symlink", "lchown"],
                "durable": False,
            }
            step["desired_sha256"] = _desired_digest(step)
            symlink_steps.append(step)
            continue
        if not bool(asset.get("is_directory")):
            continue
        acls = deepcopy(asset.get("acls", []))
        if not asset.get("sticky") and isinstance(acls, list):
            explicit_defaults = {
                row.get("account")
                for row in acls
                if isinstance(row, Mapping) and row.get("default")
            }
            acls += [
                {**dict(row), "default": True}
                for row in acls
                if isinstance(row, Mapping)
                and not row.get("default")
                and row.get("account") not in explicit_defaults
            ]
        step: dict[str, object] = {
            "step_id": f"asset:{asset['asset_id']}",
            "kind": "asset",
            "asset_type": "directory",
            "path": path,
            "owner": asset["owner"],
            "group": asset["group"],
            "mode": asset["mode"],
            "acls": acls,
            "operations": ["snapshot", "chown", "chmod", "set_acl"],
            "durable": asset.get("tree") == "durable-state",
        }
        step["desired_sha256"] = _desired_digest(step)
        directory_steps.append(step)
    # A child mkdir(parents=True) must never manufacture a later-controlled
    # ancestor with ambient root ownership.  Apply all managed directories in
    # path topology order, then create symlinks only after their targets exist.
    steps.extend(
        sorted(
            directory_steps,
            key=lambda row: (
                len(PurePosixPath(str(row["path"])).parts),
                str(row["path"]),
                str(row["step_id"]),
            ),
        )
    )
    steps.extend(symlink_steps)
    for category in (
        "units",
        "shim",
        "polkit",
        "gitconfigs",
        "toolchain_wrappers",
        "environment",
        "enforcement",
    ):
        for name, artifact in sorted(generated.get(category, {}).items()):
            step = {
                "step_id": f"generated:{category}/{name}",
                "kind": "asset",
                "asset_type": "file",
                "path": artifact["path"],
                "owner": artifact["owner"],
                "group": artifact["group"],
                "mode": artifact["mode"],
                "acls": [],
                "operations": ["snapshot", "write", "chown", "chmod", "set_acl"],
                "content": artifact["content"],
                "durable": False,
            }
            step["desired_sha256"] = _desired_digest(step)
            steps.append(step)
    return steps


def _assert_managed_parent_topology(plan: Mapping[str, object]) -> None:
    """Reject plans that would make the backend invent an unmanaged parent."""

    steps = plan.get("apply_order")
    roots = plan.get("roots")
    if not isinstance(steps, list) or not isinstance(roots, Mapping):
        raise InstallPlanError("plan path topology is invalid")
    directory_positions = {
        Path(str(step["path"])): index
        for index, step in enumerate(steps)
        if isinstance(step, Mapping)
        and step.get("kind") == "asset"
        and step.get("asset_type") == "directory"
        and isinstance(step.get("path"), str)
    }
    allowed_external_parents: set[Path] = set()
    for name in ("deploy", "state"):
        value = roots.get(name)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise InstallPlanError(f"plan root {name} is invalid")
        allowed_external_parents.add(Path(value).parent)
    for name in ("systemd", "polkit"):
        value = roots.get(name)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise InstallPlanError(f"plan root {name} is invalid")
        allowed_external_parents.add(Path(value))
    for field in ("accounts", "service_accounts"):
        rows = plan.get(field, [])
        if not isinstance(rows, list):
            raise InstallPlanError(f"plan {field} is invalid")
        for row in rows:
            home = row.get("home") if isinstance(row, Mapping) else None
            if not isinstance(home, str) or not Path(home).is_absolute():
                raise InstallPlanError(f"plan {field} home is invalid")
            allowed_external_parents.add(Path(home).parent)

    for index, step in enumerate(steps):
        if not isinstance(step, Mapping) or step.get("kind") not in {
            "asset",
            "repository",
            "toolchain",
            "venv",
        }:
            continue
        raw_path = step.get("path")
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise InstallPlanError(
                f"managed step has invalid absolute path: {step.get('step_id')}"
            )
        parent = Path(raw_path).parent
        parent_position = directory_positions.get(parent)
        if parent_position is None:
            if parent not in allowed_external_parents:
                raise InstallPlanError(
                    "managed step has an unmanaged immediate parent: "
                    f"{step.get('step_id')} -> {parent}"
                )
        elif parent_position >= index:
            raise InstallPlanError(
                "managed parent must precede its child: "
                f"{step.get('step_id')} -> {parent}"
            )


def build_install_plan(
    *, config: Mapping[str, object], candidate_wheel: Path, bundle: Path
) -> dict[str, object]:
    """Build a pure, exact-artifact-bound four-way desired-state plan."""

    _validate_install_config_schema(config)
    if config.get("schema_version") != 1:
        raise InstallPlanError("config schema_version must be 1")
    _reject_sensitive_config(config)
    wheel_path = _validate_regular_file(Path(candidate_wheel), label="candidate wheel")
    bundle_path = _validate_regular_file(Path(bundle), label="bundle")
    scheme = _configured_scheme(config)
    accounts = _account_rows(config, scheme)
    service_accounts = _service_account_rows(config)
    principal_uids = {int(row["uid"]) for row in accounts}
    principal_gids = {int(row["gid"]) for row in accounts}
    for row in service_accounts:
        if int(row["uid"]) in principal_uids or int(row["gid"]) in principal_gids:
            raise InstallPlanError(
                f"service account {row['name']} must use a dedicated uid and gid"
            )
    layout, roots = _layout_from_config(config, accounts)
    permission_plan = permgen.generate_plan(scheme)
    permgen.assert_principals_resolved(permission_plan, scheme)
    asset_paths = layout.asset_paths()
    assets: list[dict[str, object]] = []
    for entry in permission_plan.entries:
        row = entry.to_dict()
        row["path"] = asset_paths.get(entry.asset_id)
        if entry.is_symlink:
            row["symlink_target"] = layout.symlink_targets().get(entry.asset_id)
        assets.append(row)
    # The transaction's typed venv step owns this path as the atomic active
    # symlink.  The legacy permgen scaffold describes it as a directory; do not
    # execute two incompatible desired states for the same path.
    active_venv_link = f"{roots['deploy']}/venv"
    scaffolds = [
        {
            "path": path,
            "owner": owner,
            "group": group,
            "mode": format(mode, "04o"),
        }
        for path, owner, group, mode in layout.scaffold_directories(scheme)
        if path != active_venv_link
    ]
    generated = _generated_inventory(
        scheme,
        layout,
        roots,
        permission_plan,
        config.get("toolchain", {}),
        config,
    )
    required_credentials = [
        {"principal": str(principal), "provider": str(provider)}
        for principal, providers in sorted(
            (config.get("providers") or {}).items()
            if isinstance(config.get("providers"), Mapping)
            else ()
        )
        for provider in (providers if isinstance(providers, list) else ())
    ]
    legacy_policy = config.get("legacy_policy")
    if legacy_policy not in {"quarantine", "reject"}:
        raise InstallPlanError("legacy_policy must be quarantine or reject")
    repo_identity = config.get("repo_identity")
    if not isinstance(repo_identity, Mapping):
        raise InstallPlanError("repo_identity must be an object")
    commit = repo_identity.get("commit")
    if not isinstance(commit, str) or len(commit) != 40 or any(
        char not in "0123456789abcdefABCDEF" for char in commit
    ):
        raise InstallPlanError("repo_identity.commit must be a 40-hex SHA")
    install_steps = _apply_steps(
        scaffolds=scaffolds,
        assets=assets,
        generated=generated,
    )
    state_root_steps = [
        step
        for step in install_steps
        if step.get("kind") == "asset"
        and step.get("asset_type") == "directory"
        and step.get("path") == roots["state"]
    ]
    if len(state_root_steps) != 1:
        raise InstallPlanError("managed state root must map to exactly one directory step")
    state_root_steps[0]["durable"] = True
    state_root_steps[0]["adoption_policy"] = "empty-managed-root-mount"
    state_root_steps[0]["desired_sha256"] = _desired_digest(state_root_steps[0])
    plan: dict[str, object] = {
        "schema_version": 1,
        "scheme": "four-way",
        "instance": config.get("instance", "cortex"),
        "repo_identity": deepcopy(dict(repo_identity)),
        "candidate": {
            "wheel_sha256": _sha256_file(wheel_path),
            "bundle_sha256": _sha256_file(bundle_path),
        },
        "operator_account": config["operator_account"],
        "external_reader_account": config["external_reader_account"],
        "legacy_policy": legacy_policy,
        "accounts": accounts,
        "service_accounts": service_accounts,
        "roots": roots,
        "source_repositories": list(layout.source_repo_slugs),
        "scaffolds": scaffolds,
        "assets": assets,
        "generated": generated,
        "provider_manifest": deepcopy(config.get("providers", {})),
        "toolchain_manifest": deepcopy(config.get("toolchain", {})),
        "required_credentials": required_credentials,
        "apply_order": [
            *(
                {
                    "step_id": f"account:{row['name']}",
                    "kind": "account",
                    "name": row["name"],
                    "uid": row["uid"],
                    "gid": row["gid"],
                    "home": row["home"],
                    "login_program": row["shell"],
                    "desired_sha256": _account_digest(
                        {
                            "name": row["name"],
                            "uid": row["uid"],
                            "gid": row["gid"],
                            "home": row["home"],
                            "login_program": row["shell"],
                        }
                    ),
                    "rollback_policy": "retain",
                }
                for row in (*accounts, *service_accounts)
            ),
            *install_steps,
            {
                "step_id": "candidate-venv",
                "kind": "venv",
                "path": (
                    f"{roots['deploy']}/venvs/{_sha256_file(wheel_path)}"
                ),
                "active_link": active_venv_link,
                "wheel_source": str(wheel_path),
                "wheel_sha256": _sha256_file(wheel_path),
                "wheelhouse": [
                    {
                        "source": str(wheel_path),
                        "sha256": _sha256_file(wheel_path),
                    }
                ],
                "wheelhouse_locked": True,
                "desired_sha256": _sha256_file(wheel_path),
                "rollback_policy": "restore-link-retain-slot",
            },
            {
                "step_id": "systemd:daemon-reload",
                "kind": "systemctl",
                "action": "daemon-reload",
                "operations": ["daemon-reload"],
                "desired_sha256": hashlib.sha256(
                    b"systemctl:daemon-reload\n"
                ).hexdigest(),
            },
            *(
                {
                    "step_id": f"systemd:enable:{unit}",
                    "kind": "systemctl",
                    "action": "enable",
                    "unit": unit,
                    "operations": ["enable"],
                    "desired_sha256": hashlib.sha256(
                        f"systemctl:enable:{unit}\n".encode("utf-8")
                    ).hexdigest(),
                }
                for unit in (
                    "cortex-egress-proxy.service",
                    "cortex-manager.service",
                    "cortex-monitor.service",
                )
            ),
        ],
        "activation_order": [
            "cortex-egress-proxy.service",
            "cortex-manager.service",
            "cortex-monitor.service",
        ],
        "minimum_disk_free_bytes": 1024 * 1024 * 1024,
    }
    state_root = Path(roots["state"])
    wheel_prefix = str(plan["candidate"]["wheel_sha256"])[:12]  # type: ignore[index]
    plan["receipt_path"] = str(
        state_root / "install-receipts" / f"{commit.lower()}-{wheel_prefix}.json"
    )
    _assert_managed_parent_topology(plan)
    return plan


def validate_bundle_manifest(bundle_path: Path) -> dict[str, object]:
    """Validate the qualification bundle schema and every referenced hash."""

    manifest_path = _validate_regular_file(bundle_path, label="bundle manifest")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallPlanError(f"invalid bundle manifest: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise InstallPlanError("bundle schema_version must be 1")
    candidate_sha = payload.get("candidate_sha")
    if (
        not isinstance(candidate_sha, str)
        or len(candidate_sha) != 40
        or any(char not in "0123456789abcdefABCDEF" for char in candidate_sha)
    ):
        raise InstallPlanError("bundle candidate_sha must be 40-hex")

    def member(raw: object, *, label: str) -> dict[str, str]:
        if not isinstance(raw, Mapping):
            raise InstallPlanError(f"bundle {label} must be an object")
        rel = raw.get("path")
        expected = raw.get("sha256")
        if not isinstance(rel, str) or not isinstance(expected, str) or len(expected) != 64:
            raise InstallPlanError(f"bundle {label} requires path and sha256")
        posix = PurePosixPath(rel)
        if posix.is_absolute() or ".." in posix.parts or not posix.parts:
            raise UnsafeInstallPathError(f"bundle {label} has unsafe relative path: {rel}")
        resolved = manifest_path.parent.joinpath(*posix.parts)
        resolved = _validate_regular_file(resolved, label=f"bundle {label}")
        try:
            resolved.relative_to(manifest_path.parent.absolute())
        except ValueError as exc:
            raise UnsafeInstallPathError(f"bundle {label} escapes its root: {rel}") from exc
        actual = _sha256_file(resolved)
        if actual != expected:
            raise InstallPlanError(
                f"bundle {label} sha256 mismatch: expected {expected}, got {actual}"
            )
        return {"path": rel, "sha256": expected, "resolved_path": str(resolved)}

    wheel = member(payload.get("wheel"), label="wheel")
    raw_wheelhouse = payload.get("wheelhouse")
    if not isinstance(raw_wheelhouse, list):
        raise InstallPlanError("bundle wheelhouse must be a list")
    wheelhouse = [member(row, label=f"wheelhouse[{index}]") for index, row in enumerate(raw_wheelhouse)]
    generated_raw = payload.get("generated_artifacts")
    if not isinstance(generated_raw, (list, dict)):
        raise InstallPlanError("bundle generated_artifacts must be a list or object")
    generated_rows = list(generated_raw.values()) if isinstance(generated_raw, dict) else generated_raw
    generated = [
        member(row, label=f"generated_artifacts[{index}]")
        for index, row in enumerate(generated_rows)
    ]
    raw_toolchain = payload.get("toolchain")
    if not isinstance(raw_toolchain, list) or not raw_toolchain:
        raise InstallPlanError("bundle toolchain must be a non-empty list")
    toolchain: list[dict[str, str]] = []
    seen_tools: set[str] = set()
    for index, raw in enumerate(raw_toolchain):
        if not isinstance(raw, Mapping):
            raise InstallPlanError(f"bundle toolchain[{index}] must be an object")
        name = raw.get("name")
        version = raw.get("version")
        shape = raw.get("shape")
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
            or name in seen_tools
            or not isinstance(version, str)
            or not version
            or shape not in {"file", "tree"}
        ):
            raise InstallPlanError(f"bundle toolchain[{index}] identity is invalid")
        seen_tools.add(name)
        validated_tool = {
            "name": name,
            "version": version,
            "shape": str(shape),
            **member(raw, label=f"toolchain[{index}]"),
        }
        if shape == "tree":
            entrypoint = raw.get("entrypoint")
            installed_sha256 = raw.get("installed_sha256")
            relative = PurePosixPath(str(entrypoint))
            if (
                not isinstance(entrypoint, str)
                or relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
                or not isinstance(installed_sha256, str)
                or len(installed_sha256) != 64
                or any(char not in "0123456789abcdef" for char in installed_sha256)
            ):
                raise InstallPlanError(f"bundle tree toolchain[{index}] metadata is invalid")
            validated_tool["entrypoint"] = entrypoint
            validated_tool["installed_sha256"] = installed_sha256
        toolchain.append(validated_tool)
    raw_repositories = payload.get("source_repositories")
    if not isinstance(raw_repositories, list) or not raw_repositories:
        raise InstallPlanError("bundle source_repositories must be a non-empty list")
    source_repositories: list[dict[str, str]] = []
    seen_repos: set[str] = set()
    for index, raw in enumerate(raw_repositories):
        if not isinstance(raw, Mapping):
            raise InstallPlanError(f"bundle source_repositories[{index}] must be an object")
        slug = raw.get("slug")
        commit = raw.get("commit")
        remote = raw.get("remote")
        if (
            not isinstance(slug, str)
            or not slug
            or slug in seen_repos
            or "/" in slug
            or not isinstance(commit, str)
            or len(commit) != 40
            or any(char not in "0123456789abcdef" for char in commit)
            or not isinstance(remote, str)
            or not remote.startswith("https://")
        ):
            raise InstallPlanError(f"bundle source_repositories[{index}] identity is invalid")
        seen_repos.add(slug)
        source_repositories.append(
            {
                "slug": slug,
                "commit": commit,
                "remote": remote,
                **member(raw, label=f"source_repositories[{index}]"),
            }
        )
    return {
        "schema_version": 1,
        "candidate_sha": candidate_sha,
        "wheel": wheel,
        "wheelhouse": wheelhouse,
        "generated_artifacts": generated,
        "toolchain": toolchain,
        "source_repositories": source_repositories,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
    }


def bind_bundle_artifacts(
    plan: Mapping[str, object], manifest: Mapping[str, object]
) -> dict[str, object]:
    """Bind validated tool binaries and source bundles into typed apply steps."""

    bound = deepcopy(dict(plan))
    configured_tools = bound.get("toolchain_manifest")
    tools = manifest.get("toolchain")
    if not isinstance(configured_tools, Mapping) or not isinstance(tools, list):
        raise InstallPlanError("toolchain configuration and bundle inventory are required")
    by_name = {
        str(row.get("name")): row for row in tools if isinstance(row, Mapping)
    }
    if set(configured_tools) != set(by_name):
        raise InstallPlanError("config toolchain names must exactly match the bundle")
    roots = bound.get("roots")
    if not isinstance(roots, Mapping) or not isinstance(roots.get("deploy"), str):
        raise InstallPlanError("plan roots are invalid")
    tool_steps: list[dict[str, object]] = []
    for name in sorted(by_name):
        configured = configured_tools[name]
        bundled = by_name[name]
        if (
            not isinstance(configured, Mapping)
            or configured.get("version") != bundled.get("version")
            or configured.get("sha256") != bundled.get("sha256")
            or configured.get("shape", "file") != bundled.get("shape")
            or configured.get("entrypoint") != bundled.get("entrypoint")
        ):
            raise InstallPlanError(f"config toolchain pin does not match bundle: {name}")
        tool_steps.append(
            {
                "step_id": f"toolchain:{name}",
                "kind": "toolchain",
                "name": name,
                "version": bundled["version"],
                "shape": bundled["shape"],
                "entrypoint": bundled.get("entrypoint"),
                "source": bundled["resolved_path"],
                "source_sha256": bundled["sha256"],
                "path": f"{roots['deploy']}/toolchain/lib/{name}",
                "owner": "root",
                "group": "root",
                "mode": "0755",
                "desired_sha256": bundled.get("installed_sha256", bundled["sha256"]),
                "operations": ["snapshot", "copy-locked", "chown", "chmod"],
            }
        )

    repositories = manifest.get("source_repositories")
    configured_assets = bound.get("assets", [])
    source_assets = [
        row
        for row in configured_assets
        if isinstance(row, Mapping) and row.get("asset_id") == "repo-source-tree"
    ] if isinstance(configured_assets, list) else []
    configured_slugs = bound.get("source_repositories")
    bundled_slugs = {
        str(row.get("slug"))
        for row in repositories
        if isinstance(row, Mapping)
    } if isinstance(repositories, list) else set()
    if (
        not isinstance(repositories, list)
        or not isinstance(configured_slugs, list)
        or not all(isinstance(slug, str) and slug for slug in configured_slugs)
        or bundled_slugs != set(configured_slugs)
        or len(source_assets) != 1
    ):
        raise InstallPlanError("source repository bundle does not match the plan")
    source_container = source_assets[0]
    source_root = source_container.get("path")
    if not isinstance(source_root, str) or not source_root.startswith("/"):
        raise InstallPlanError("source repository container path is invalid")
    repo_steps: list[dict[str, object]] = []
    repo_identity = bound.get("repo_identity")
    for row in repositories:
        assert isinstance(row, Mapping)
        if not isinstance(repo_identity, Mapping) or any(
            row.get(key) != repo_identity.get(key) for key in ("commit", "remote")
        ):
            raise InstallPlanError("source repository identity does not match repo_identity")
        repo_step = {
            "step_id": f"repository:{row['slug']}",
            "kind": "repository",
            "slug": row["slug"],
            "source": row["resolved_path"],
            "source_sha256": row["sha256"],
            "commit": row["commit"],
            "remote": row["remote"],
            "path": f"{source_root.rstrip('/')}/{row['slug']}",
            "owner": source_container["owner"],
            "group": source_container["group"],
            "mode": source_container["mode"],
            "durable": True,
            "operations": ["snapshot", "clone-bundle", "checkout", "chown"],
        }
        repo_step["desired_sha256"] = _desired_digest(repo_step)
        repo_steps.append(repo_step)

    order = bound.get("apply_order")
    if not isinstance(order, list):
        raise InstallPlanError("plan apply_order is invalid")
    container_index = next(
        (
            index
            for index, step in enumerate(order)
            if isinstance(step, Mapping)
            and step.get("step_id") == "asset:repo-source-tree"
        ),
        None,
    )
    if container_index is None:
        raise InstallPlanError("source repository container apply step is missing")
    insertion = container_index + 1
    bound["apply_order"] = [
        *order[:insertion],
        *repo_steps,
        *tool_steps,
        *order[insertion:],
    ]
    _assert_managed_parent_topology(bound)
    return bound


@dataclass(frozen=True)
class PreflightReport:
    failures: tuple[dict[str, str], ...]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "failures": [dict(row) for row in self.failures]}


def _account_has_receipt_provenance(
    *,
    plan: Mapping[str, object],
    desired: Mapping[str, object],
    receipt: "InstallReceipt | None",
) -> bool:
    """Require a plan-bound journal proving this transaction created the account.

    Exact passwd fields are necessary but not sufficient for adoption: an unrelated
    pre-existing account can be made to look identical.  A prepared/completed entry
    whose durable prior state was absent is the narrow provenance this installer owns.
    Clean rollback retains service accounts, so the archived rollback journal is also
    an admissible source for a later replay of the same exact plan.
    """

    if receipt is None:
        return False
    document = receipt.to_dict()
    if document.get("plan_sha256") != plan_sha256(plan):
        return False
    name = desired.get("name")
    planned_step = next(
        (
            step
            for step in plan.get("apply_order", [])
            if isinstance(step, Mapping)
            and step.get("kind") == "account"
            and step.get("name") == name
        ),
        None,
    )
    if not isinstance(planned_step, Mapping):
        return False
    for field in ("journal", "rollback_journal"):
        entries = document.get(field, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            prior = entry.get("prior")
            if (
                entry.get("step_id") == planned_step.get("step_id")
                and entry.get("step") == planned_step
                and entry.get("status") in {"prepared", "completed"}
                and isinstance(prior, Mapping)
                and prior.get("exists") is False
            ):
                return True
    return False


def _account_group_has_receipt_provenance(
    *,
    plan: Mapping[str, object],
    desired: Mapping[str, object],
    receipt: "InstallReceipt | None",
) -> bool:
    """Prove an orphan group was created by this exact account transaction."""

    if receipt is None:
        return False
    document = receipt.to_dict()
    if document.get("plan_sha256") != plan_sha256(plan):
        return False
    planned_step = next(
        (
            step
            for step in plan.get("apply_order", [])
            if isinstance(step, Mapping)
            and step.get("kind") == "account"
            and step.get("name") == desired.get("name")
        ),
        None,
    )
    if not isinstance(planned_step, Mapping):
        return False
    entries = document.get("journal", [])
    if not isinstance(entries, list):
        return False
    for entry in entries:
        prior = entry.get("prior") if isinstance(entry, Mapping) else None
        if (
            isinstance(entry, Mapping)
            and entry.get("step_id") == planned_step.get("step_id")
            and entry.get("step") == planned_step
            and entry.get("status") == "prepared"
            and isinstance(prior, Mapping)
            and prior.get("exists") is False
            and prior.get("group_exists") is False
        ):
            return True
    return False


def _prepared_account_group_is_replayable(
    step: Mapping[str, object],
    prior: Mapping[str, object],
    installed: Mapping[str, object],
) -> bool:
    """Recognize the exact groupadd-only state owned by a prepared journal."""

    return bool(
        step.get("kind") == "account"
        and prior.get("exists") is False
        and prior.get("group_exists") is False
        and installed.get("exists") is False
        and installed.get("group_exists") is True
        and installed.get("group_gid") == step.get("gid")
        and installed.get("group_members") == []
    )


def _step_has_receipt_provenance(
    *,
    plan: Mapping[str, object],
    step: Mapping[str, object],
    receipt: "InstallReceipt",
) -> bool:
    """Prove an exact existing asset came from this plan-bound transaction.

    An exact filesystem shape is not ownership provenance.  First install may
    therefore not adopt an unrelated asset or repository merely because it
    happens to match.  A prior prepared/completed entry whose original state
    was absent proves creation by this receipt.  Later reinstall cycles carry
    that proof forward with ``adopted_from_receipt``.
    """

    document = receipt.to_dict()
    if document.get("plan_sha256") != plan_sha256(plan):
        return False
    for field in ("journal", "rollback_journal"):
        entries = document.get(field, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            prior = entry.get("prior")
            if (
                entry.get("step_id") == step.get("step_id")
                and entry.get("step") == step
                and entry.get("status") in {"prepared", "completed"}
                and isinstance(prior, Mapping)
                and (
                    prior.get("exists") is False
                    or (
                        entry.get("adopted_from_receipt") is True
                        and entry.get("adopted_mount_root") is None
                    )
                )
            ):
                return True
    return False


def _mount_adoption_authority_from_receipt(
    *,
    plan: Mapping[str, object],
    step: Mapping[str, object],
    receipt: "InstallReceipt",
    installed: Mapping[str, object],
) -> dict[str, int] | None:
    """Carry a mount adoption forward only while its inode is still mounted."""

    current = _observed_mount_authority(installed)
    if current is None:
        return None
    document = receipt.to_dict()
    if document.get("plan_sha256") != plan_sha256(plan):
        return None
    for field in ("journal", "rollback_journal"):
        entries = document.get(field, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if (
                isinstance(entry, Mapping)
                and entry.get("step_id") == step.get("step_id")
                and entry.get("step") == step
                and entry.get("status") in {"prepared", "completed"}
                and _valid_mount_adoption_authority(
                    entry.get("adopted_mount_root"), step=step
                )
                and dict(entry["adopted_mount_root"]) == current
            ):
                return current
    return None


def validate_preflight(
    plan: Mapping[str, object],
    facts: Mapping[str, object],
    *,
    receipt: "InstallReceipt | None" = None,
) -> PreflightReport:
    failures: list[dict[str, str]] = []
    for name in ("systemd", "polkit", "cgroup_v2", "acl"):
        if facts.get(name) is not True:
            failures.append({"code": f"missing_{name}", "detail": f"{name} is required"})
    minimum = plan.get("minimum_disk_free_bytes", 1024 * 1024 * 1024)
    free = facts.get("disk_free_bytes", 0)
    if not isinstance(free, int) or not isinstance(minimum, int) or free < minimum:
        failures.append({"code": "insufficient_disk", "detail": f"free={free}, required={minimum}"})
    if facts.get("universal_nopasswd"):
        failures.append({"code": "universal_nopasswd", "detail": "universal NOPASSWD is forbidden"})
    if facts.get("in_flight_jobs") not in (0, None):
        failures.append({"code": "in_flight_jobs", "detail": "jobs are still in flight"})

    observed_accounts = facts.get("accounts", {})
    if not isinstance(observed_accounts, Mapping):
        observed_accounts = {}
    observed_uids = facts.get("account_uids", {})
    if not isinstance(observed_uids, Mapping):
        observed_uids = {}
    observed_gids = facts.get("group_gids", {})
    if not isinstance(observed_gids, Mapping):
        observed_gids = {}
    observed_groups = facts.get("groups", {})
    if not isinstance(observed_groups, Mapping):
        observed_groups = {}
    observed_primary_gid_users = facts.get("primary_gid_users", {})
    if not isinstance(observed_primary_gid_users, Mapping):
        observed_primary_gid_users = {}
    observed_group_names_by_gid = facts.get("group_names_by_gid", {})
    if not isinstance(observed_group_names_by_gid, Mapping):
        observed_group_names_by_gid = {}
    desired_accounts = [
        row
        for key in ("accounts", "service_accounts")
        for row in plan.get(key, [])
    ]
    for desired in desired_accounts:
        if not isinstance(desired, Mapping) or not isinstance(desired.get("name"), str):
            raise InstallPlanError("plan account entries must be typed objects")
        name = str(desired["name"])
        observed = observed_accounts.get(name)
        uid_owner = observed_uids.get(desired.get("uid"))
        if uid_owner is not None and uid_owner != name:
            raise AccountCollisionError(
                f"desired uid {desired.get('uid')} is already owned by {uid_owner}"
            )
        desired_gid = desired.get("gid")
        group_names = observed_group_names_by_gid.get(desired_gid, [])
        if not isinstance(group_names, list) or not all(
            isinstance(group_name, str) for group_name in group_names
        ):
            raise AccountCollisionError(
                f"desired gid {desired_gid} group names are not proven"
            )
        foreign_group_names = sorted(set(group_names) - {name})
        if foreign_group_names:
            raise AccountCollisionError(
                f"desired gid {desired_gid} is a shared gid with group names: "
                + ", ".join(foreign_group_names)
            )
        gid_owner = observed_gids.get(desired_gid)
        if not group_names and gid_owner is not None and gid_owner != name:
            raise AccountCollisionError(
                f"desired gid {desired_gid} is already owned by {gid_owner}"
            )
        primary_members = observed_primary_gid_users.get(desired_gid, [])
        if not isinstance(primary_members, list) or not all(
            isinstance(member, str) for member in primary_members
        ):
            raise AccountCollisionError(
                f"desired gid {desired_gid} primary members are not proven"
            )
        foreign_primary_members = sorted(set(primary_members) - {name})
        if foreign_primary_members:
            raise AccountCollisionError(
                f"desired gid {desired_gid} has foreign primary members: "
                + ", ".join(foreign_primary_members)
            )
        observed_group = observed_groups.get(name)
        if observed_group is not None and (
            not isinstance(observed_group, Mapping)
            or observed_group.get("gid") != desired_gid
        ):
            raise AccountCollisionError(f"existing group {name} does not match the plan")
        if isinstance(observed_group, Mapping):
            supplementary_members = observed_group.get("members")
            if not isinstance(supplementary_members, list) or not all(
                isinstance(member, str) for member in supplementary_members
            ):
                raise AccountCollisionError(
                    f"existing group {name} members are not proven"
                )
            foreign_supplementary_members = sorted(
                set(supplementary_members) - {name}
            )
            if foreign_supplementary_members:
                raise AccountCollisionError(
                    f"existing group {name} has foreign supplementary members: "
                    + ", ".join(foreign_supplementary_members)
                )
        if observed is None:
            if observed_group is not None and not _account_group_has_receipt_provenance(
                plan=plan, desired=desired, receipt=receipt
            ):
                raise AccountCollisionError(
                    f"existing group {name} lacks trusted receipt provenance"
                )
            continue
        if not isinstance(observed, Mapping) or any(
            observed.get(key) != desired.get(key) for key in ("uid", "gid", "home", "shell")
        ):
            raise AccountCollisionError(f"existing account {name} does not match the plan")
        if observed.get("supplementary_groups") != []:
            raise AccountCollisionError(
                f"existing account {name} supplementary groups are not proven empty"
            )
        if observed.get("password_locked") is not True:
            raise AccountCollisionError(
                f"existing account {name} password lock state is not proven locked"
            )
        if not _account_has_receipt_provenance(
            plan=plan, desired=desired, receipt=receipt
        ):
            raise AccountCollisionError(
                f"existing account {name} lacks trusted prior receipt provenance"
            )

    services = facts.get("services", {})
    if isinstance(services, Mapping):
        active = sorted(
            str(name)
            for name, state in services.items()
            if state == "active"
        )
        if active:
            failures.append(
                {
                    "code": "services_active",
                    "detail": "services must be stopped before apply: "
                    + ", ".join(active),
                }
            )
        unproven = sorted(
            str(name)
            for name, state in services.items()
            if state not in {"active", "inactive", "failed", "not-found"}
        )
        if unproven:
            failures.append(
                {
                    "code": "service_state_unproven",
                    "detail": "service status could not be proven: "
                    + ", ".join(unproven),
                }
            )

    observed_paths = facts.get("paths", {})
    if not isinstance(observed_paths, Mapping):
        observed_paths = {}
    for step in plan.get("apply_order", []):
        if not isinstance(step, Mapping):
            raise InstallPlanError("apply_order entries must be typed objects")
        path = step.get("path")
        observed = observed_paths.get(path, {})
        if (
            isinstance(observed, Mapping)
            and observed.get("is_symlink")
            and step.get("asset_type") != "symlink"
        ):
            raise UnsafeInstallPathError(f"apply path became a symlink: {path}")
    return PreflightReport(tuple(failures))


class InstallBackend(Protocol):
    def preflight_facts(self, plan: Mapping[str, object]) -> Mapping[str, object]: ...
    def inspect_step(self, step: Mapping[str, object]) -> Mapping[str, object]: ...
    def apply_step(self, step: Mapping[str, object]) -> Mapping[str, object]: ...
    def apply_step_checkpointed(
        self,
        step: Mapping[str, object],
        creation_checkpoint: Callable[[Mapping[str, object]], None],
    ) -> Mapping[str, object]: ...
    def creation_authority_matches(
        self, step: Mapping[str, object], authority: Mapping[str, object]
    ) -> bool: ...
    def rollback_step(self, entry: Mapping[str, object]) -> None: ...
    def list_unknown_state(self, receipt: "InstallReceipt") -> Sequence[str]: ...
    def start_service(self, name: str) -> None: ...
    def stop_service(self, name: str) -> None: ...


class InstallReceipt:
    """Mutable transaction state with optional atomic on-disk persistence."""

    def __init__(self, document: Mapping[str, object], *, path: Path | None = None) -> None:
        self._document: dict[str, object] = deepcopy(dict(document))
        self.path = path

    def to_dict(self) -> dict[str, object]:
        return deepcopy(self._document)

    def _persist(self) -> None:
        if self.path is not None:
            _atomic_write_receipt_json(self.path, self._document)

    @classmethod
    def load(cls, path: Path) -> "InstallReceipt":
        if not path.is_absolute() or ".." in path.parts:
            raise UnsafeInstallPathError(f"receipt path must be safe and absolute: {path}")
        parent_fd: int | None = None
        receipt_fd: int | None = None
        try:
            parent_fd, leaf = _open_receipt_parent_directory(path)
            receipt_fd = os.open(
                leaf,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
            before = os.fstat(receipt_fd)
            _validate_receipt_file(before, path)
            payload = json.loads(_read_fd_bytes(receipt_fd).decode("utf-8"))
            after = os.fstat(receipt_fd)
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
                raise UnsafeInstallPathError(
                    f"receipt changed while being read: {path}"
                )
            _assert_fd_path_binding(path, receipt_fd, directory=False)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InstallError(f"cannot load receipt {path}: {exc}") from exc
        finally:
            if receipt_fd is not None:
                os.close(receipt_fd)
            if parent_fd is not None:
                os.close(parent_fd)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise InstallError(f"invalid receipt schema: {path}")
        plan = payload.get("plan")
        if not isinstance(plan, Mapping) or payload.get("plan_sha256") != plan_sha256(plan):
            raise InstallError(f"receipt embedded plan hash is invalid: {path}")
        if payload.get("repo_identity") != plan.get("repo_identity") or payload.get(
            "candidate"
        ) != plan.get("candidate"):
            raise InstallError(f"receipt identity fields are inconsistent: {path}")
        journal = payload.get("journal")
        planned_steps = {
            str(step.get("step_id")): step
            for step in plan.get("apply_order", [])
            if isinstance(step, Mapping)
        }
        if not isinstance(journal, list):
            raise InstallError(f"receipt journal is invalid: {path}")
        seen: set[str] = set()
        for entry in journal:
            if not isinstance(entry, Mapping):
                raise InstallError(f"receipt journal entry is invalid: {path}")
            step_id = str(entry.get("step_id"))
            if step_id in seen or entry.get("step") != planned_steps.get(step_id):
                raise InstallError(f"receipt journal is not bound to its plan: {path}")
            if entry.get("status", "completed") not in {"prepared", "completed"}:
                raise InstallError(f"receipt journal status is invalid: {path}")
            authority = entry.get("creation_authority")
            mount_authority = entry.get("adopted_mount_root")
            prior = entry.get("prior")
            planned_step = planned_steps.get(step_id)
            if authority is not None and (
                not isinstance(planned_step, Mapping)
                or not isinstance(prior, Mapping)
                or not _prepared_prior_absent_leaf(planned_step, prior)
                or not _valid_creation_authority(
                    authority,
                    file_type=planned_step.get("asset_type", "file"),
                )
            ):
                raise InstallError(
                    f"receipt journal creation authority is invalid: {path}"
                )
            if mount_authority is not None and not _valid_mount_adoption_authority(
                mount_authority, step=planned_step
            ):
                raise InstallError(
                    f"receipt journal mount authority is invalid: {path}"
                )
            seen.add(step_id)
        rollback_journal = payload.get("rollback_journal", [])
        if not isinstance(rollback_journal, list):
            raise InstallError(f"receipt rollback journal is invalid: {path}")
        rollback_seen: set[str] = set()
        for entry in rollback_journal:
            if not isinstance(entry, Mapping):
                raise InstallError(f"receipt rollback journal entry is invalid: {path}")
            step_id = str(entry.get("step_id"))
            if (
                step_id in rollback_seen
                or entry.get("step") != planned_steps.get(step_id)
                or entry.get("status", "completed")
                not in {"prepared", "completed"}
            ):
                raise InstallError(
                    f"receipt rollback journal is not bound to its plan: {path}"
                )
            authority = entry.get("creation_authority")
            mount_authority = entry.get("adopted_mount_root")
            prior = entry.get("prior")
            planned_step = planned_steps.get(step_id)
            if authority is not None and (
                not isinstance(planned_step, Mapping)
                or not isinstance(prior, Mapping)
                or not _prepared_prior_absent_leaf(planned_step, prior)
                or not _valid_creation_authority(
                    authority,
                    file_type=planned_step.get("asset_type", "file"),
                )
            ):
                raise InstallError(
                    f"receipt rollback creation authority is invalid: {path}"
                )
            if mount_authority is not None and not _valid_mount_adoption_authority(
                mount_authority, step=planned_step
            ):
                raise InstallError(
                    f"receipt rollback mount authority is invalid: {path}"
                )
            rollback_seen.add(step_id)
        credentials = payload.get("credentials")
        if not isinstance(credentials, list) or any(
            not isinstance(row, Mapping)
            or set(row) != {"principal", "provider", "mode", "sha256"}
            or row.get("mode") != "0600"
            or not isinstance(row.get("sha256"), str)
            or len(str(row.get("sha256"))) != 64
            for row in credentials
        ):
            raise InstallError(f"receipt credential metadata is invalid: {path}")
        credential_journal = payload.get("credential_journal", [])
        if not isinstance(credential_journal, list) or any(
            not isinstance(row, Mapping)
            or set(row)
            not in (
                {"principal", "provider", "mode", "sha256", "status"},
                {
                    "principal",
                    "provider",
                    "mode",
                    "sha256",
                    "status",
                    "temp_name",
                },
            )
            or row.get("mode") != "0600"
            or row.get("status") != "prepared"
            or not isinstance(row.get("sha256"), str)
            or len(str(row.get("sha256"))) != 64
            or any(char not in "0123456789abcdef" for char in str(row["sha256"]))
            or (
                "temp_name" in row
                and (
                    not isinstance(row.get("temp_name"), str)
                    or not str(row["temp_name"]).startswith(".")
                    or row["temp_name"] in {".", ".."}
                    or Path(str(row["temp_name"])).name != row["temp_name"]
                    or len(str(row["temp_name"])) > 255
                )
            )
            for row in credential_journal
        ):
            raise InstallError(f"receipt credential journal is invalid: {path}")
        completed_identities = [
            (str(row["principal"]), str(row["provider"])) for row in credentials
        ]
        prepared_identities = [
            (str(row["principal"]), str(row["provider"]))
            for row in credential_journal
        ]
        if (
            len(completed_identities) != len(set(completed_identities))
            or len(prepared_identities) != len(set(prepared_identities))
            or set(completed_identities) & set(prepared_identities)
        ):
            raise InstallError(f"receipt credential authority is duplicated: {path}")
        activation_journal = payload.get("activation_journal", [])
        if not isinstance(activation_journal, list) or any(
            not isinstance(row, Mapping)
            or set(row) != {"service", "status"}
            or row.get("service") not in _SERVICE_ORDER
            or row.get("status") not in {"prepared", "completed"}
            for row in activation_journal
        ):
            raise InstallError(f"receipt activation journal is invalid: {path}")
        activation_services = [str(row["service"]) for row in activation_journal]
        if (
            len(activation_services) != len(set(activation_services))
            or activation_services
            != sorted(activation_services, key=_SERVICE_ORDER.index)
        ):
            raise InstallError(f"receipt activation authority is invalid: {path}")
        expected_executables = payload.get("expected_service_executables")
        if expected_executables is not None and (
            not isinstance(expected_executables, Mapping)
            or set(expected_executables) != set(_SERVICE_ORDER)
            or any(
                not isinstance(row, Mapping)
                or set(row) != {"exec_path", "sha256"}
                or not isinstance(row.get("exec_path"), str)
                or not str(row.get("exec_path")).startswith("/")
                or not isinstance(row.get("sha256"), str)
                or len(str(row.get("sha256"))) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in str(row.get("sha256"))
                )
                for row in expected_executables.values()
            )
        ):
            raise InstallError(f"receipt service executable binding is invalid: {path}")
        venv_steps = [
            step
            for step in plan.get("apply_order", [])
            if isinstance(step, Mapping) and step.get("kind") == "venv"
        ]
        candidate_venv = payload.get("candidate_venv")
        if len(venv_steps) > 1 or (
            candidate_venv is not None
            and (
                len(venv_steps) != 1
                or not isinstance(candidate_venv, Mapping)
                or set(candidate_venv) != {"path", "tree_sha256"}
                or candidate_venv.get("path") != venv_steps[0].get("path")
                or not isinstance(candidate_venv.get("path"), str)
                or not Path(str(candidate_venv.get("path"))).is_absolute()
                or not isinstance(candidate_venv.get("tree_sha256"), str)
                or len(str(candidate_venv.get("tree_sha256"))) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in str(candidate_venv.get("tree_sha256"))
                )
            )
        ) or (
            venv_steps
            and payload.get("state") == "applied"
            and candidate_venv is None
        ):
            raise InstallError(f"receipt candidate venv binding is invalid: {path}")
        return cls(payload, path=path)


def _validate_receipt_parent(observed: os.stat_result, path: Path) -> None:
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != 0
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise InstallError(
            "receipt parent/ancestor must be canonical, root-owned, and "
            f"non-writable: {path}"
        )


def _validate_receipt_file(observed: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise UnsafeInstallPathError(
            f"receipt must be a single-link regular file: {path}"
        )
    if observed.st_uid != 0 or stat.S_IMODE(observed.st_mode) != 0o600:
        raise InstallError(f"receipt must be root-owned mode 0600: {path}")


def _open_receipt_parent_directory(
    path: Path, *, create: bool = False
) -> tuple[int, str]:
    """Open receipt authority while validating every pathname component.

    A trusted direct parent is insufficient when an attacker-writable ancestor
    can replace it.  Each component is therefore opened relative to the held
    predecessor, checked before traversal continues, and the final descriptor
    is rebound to the canonical pathname before it is returned.
    """

    if not path.is_absolute() or ".." in path.parts or not path.name:
        raise UnsafeInstallPathError(f"unsafe receipt authority path: {path}")
    descriptor = os.open("/", _DIRECTORY_OPEN_FLAGS)
    current = Path("/")
    try:
        _validate_receipt_parent(os.fstat(descriptor), current)
        for component in path.parent.parts[1:]:
            current /= component
            try:
                next_descriptor = os.open(
                    component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                try:
                    next_descriptor = os.open(
                        component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor
                    )
                except OSError as exc:
                    raise UnsafeInstallPathError(
                        f"receipt authority contains an unsafe component: {current}"
                    ) from exc
            except OSError as exc:
                raise UnsafeInstallPathError(
                    f"receipt authority contains an unsafe component: {current}"
                ) from exc
            try:
                _validate_receipt_parent(os.fstat(next_descriptor), current)
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        if path.parent != Path("/"):
            _assert_fd_path_binding(path.parent, descriptor, directory=True)
        return descriptor, path.name
    except BaseException:
        os.close(descriptor)
        raise


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise InstallError("receipt checkpoint write made no progress")
        view = view[written:]


def _atomic_write_receipt_json(path: Path, value: object) -> None:
    """Publish one receipt checkpoint beneath a held, trusted parent fd."""

    parent_fd: int | None = None
    existing_fd: int | None = None
    temporary_fd: int | None = None
    temporary_name: str | None = None
    try:
        parent_fd, leaf = _open_receipt_parent_directory(path, create=True)
        try:
            existing_fd = os.open(
                leaf,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            existing_fd = None
        except OSError as exc:
            raise UnsafeInstallPathError(
                f"cannot safely open existing receipt authority: {path}"
            ) from exc
        if existing_fd is not None:
            _validate_receipt_file(os.fstat(existing_fd), path)
            _assert_fd_path_binding(path, existing_fd, directory=False)

        for _attempt in range(32):
            temporary_name = f".{leaf}.{uuid.uuid4().hex}.tmp"
            try:
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
                break
            except FileExistsError:
                temporary_name = None
        if temporary_fd is None or temporary_name is None:
            raise InstallError("cannot allocate a receipt checkpoint file")
        os.fchmod(temporary_fd, 0o600)
        _validate_receipt_file(os.fstat(temporary_fd), path)
        _write_all(temporary_fd, _canonical_bytes(value))
        os.fsync(temporary_fd)
        os.replace(
            temporary_name,
            leaf,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        _assert_fd_path_binding(path, temporary_fd, directory=False)
    finally:
        if temporary_name is not None and parent_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        if temporary_fd is not None:
            os.close(temporary_fd)
        if existing_fd is not None:
            os.close(existing_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def atomic_write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    if ".." in path.parts or not path.is_absolute():
        raise UnsafeInstallPathError(f"JSON output path must be safe and absolute: {path}")
    _reject_symlink_ancestors(path, label="JSON output")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
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


def new_install_receipt(
    plan: Mapping[str, object], *, path: Path | None = None
) -> InstallReceipt:
    receipt = InstallReceipt(
        {
            "schema_version": 1,
            "receipt_id": str(uuid.uuid4()),
            "plan_sha256": plan_sha256(plan),
            "plan": _as_plan_dict(plan),
            "repo_identity": deepcopy(plan.get("repo_identity", {})),
            "candidate": deepcopy(plan.get("candidate", {})),
            "state": "planned",
            "journal": [],
            "credentials": [],
            "credential_journal": [],
            "activation_journal": [],
            "services_started": False,
            "activated": False,
            "qualified": False,
        },
        path=path,
    )
    receipt._persist()
    return receipt


def _validate_operations(step: Mapping[str, object]) -> None:
    operations = step.get("operations", [])
    if not isinstance(operations, list):
        raise InstallPlanError(f"step {step.get('step_id')} operations must be a list")
    if step.get("acls"):
        try:
            acl_index = operations.index("set_acl")
            chmod_index = operations.index("chmod")
            chown_index = operations.index("chown")
        except ValueError as exc:
            raise InstallPlanError("ACL steps require chown, chmod, then set_acl") from exc
        if not (chown_index < chmod_index < acl_index):
            raise InstallPlanError("ACL must be applied after chown and chmod")


def _state_matches(step: Mapping[str, object], state: Mapping[str, object]) -> bool:
    if not state.get("exists"):
        return False
    pairs = (
        ("installed_sha256", "desired_sha256"),
        ("owner", "owner"),
        ("group", "group"),
        ("mode", "mode"),
    )
    for actual_key, desired_key in pairs:
        desired = step.get(desired_key)
        if desired is not None and state.get(actual_key) != desired:
            return False
    desired_acl = step.get("acls")
    if desired_acl is not None and state.get("acl", []) != desired_acl:
        return False
    return True


def _observed_mount_authority(
    installed: Mapping[str, object],
) -> dict[str, int] | None:
    device = installed.get("device")
    inode = installed.get("inode")
    if (
        installed.get("is_mountpoint") is not True
        or not isinstance(device, int)
        or isinstance(device, bool)
        or device < 0
        or not isinstance(inode, int)
        or isinstance(inode, bool)
        or inode <= 0
    ):
        return None
    return {"device": device, "inode": inode}


def _valid_mount_adoption_authority(
    authority: object, *, step: object
) -> bool:
    return bool(
        isinstance(step, Mapping)
        and step.get("kind") == "asset"
        and step.get("asset_type") == "directory"
        and step.get("durable") is True
        and step.get("adoption_policy") == "empty-managed-root-mount"
        and isinstance(authority, Mapping)
        and set(authority) == {"device", "inode"}
        and _observed_mount_authority(
            {"is_mountpoint": True, **dict(authority)}
        )
        is not None
    )


def _receipt_bootstrap_children(
    *, plan: Mapping[str, object], receipt: "InstallReceipt | None"
) -> list[str] | None:
    """Return the exact state-root children created by the default receipt."""

    roots = plan.get("roots")
    configured = plan.get("receipt_path")
    if (
        receipt is None
        or receipt.path is None
        or not isinstance(roots, Mapping)
        or not isinstance(roots.get("state"), str)
        or not isinstance(configured, str)
        or receipt.path != Path(configured)
    ):
        return None
    state_root = Path(roots["state"])
    try:
        relative = receipt.path.relative_to(state_root)
    except ValueError:
        return None
    if len(relative.parts) != 2 or relative.parts[0] != "install-receipts":
        return None
    return [relative.parts[0], relative.as_posix()]


def _explicit_empty_managed_mount_is_adoptable(
    *,
    plan: Mapping[str, object],
    step: Mapping[str, object],
    installed: Mapping[str, object],
    receipt: "InstallReceipt | None" = None,
) -> bool:
    """Recognize the one first-install directory created by a volume mount.

    A Docker named volume necessarily materializes its mountpoint before Cortex
    can create it.  Adoption is therefore limited to the exact managed state
    root, only while it is empty apart from the default receipt bootstrap and
    already matches the complete desired owner/mode/ACL state checked by the
    caller.
    """

    roots = plan.get("roots")
    children = installed.get("children")
    receipt_children = _receipt_bootstrap_children(plan=plan, receipt=receipt)
    allowed_children = children == [] or (
        receipt_children is not None and children == receipt_children
    )
    return bool(
        isinstance(roots, Mapping)
        and isinstance(roots.get("state"), str)
        and step.get("kind") == "asset"
        and step.get("asset_type") == "directory"
        and step.get("durable") is True
        and step.get("path") == roots["state"]
        and step.get("adoption_policy") == "empty-managed-root-mount"
        and _observed_mount_authority(installed) is not None
        and allowed_children
    )


def _valid_creation_authority(
    authority: object, *, file_type: object | None = None
) -> bool:
    if not isinstance(authority, Mapping) or set(authority) != {
        "device",
        "inode",
        "file_type",
    }:
        return False
    device = authority.get("device")
    inode = authority.get("inode")
    observed_type = authority.get("file_type")
    return bool(
        isinstance(device, int)
        and not isinstance(device, bool)
        and device >= 0
        and isinstance(inode, int)
        and not isinstance(inode, bool)
        and inode > 0
        and observed_type in {"file", "directory"}
        and (file_type is None or observed_type == file_type)
    )


def _prepared_prior_absent_leaf(
    step: Mapping[str, object], prior: Mapping[str, object]
) -> bool:
    return bool(
        step.get("kind") == "asset"
        and step.get("asset_type", "file") in {"file", "directory"}
        and not prior.get("exists")
    )


def _prepared_leaf_has_rollback_authority(
    *,
    backend: InstallBackend,
    step: Mapping[str, object],
    entry: Mapping[str, object],
    installed: Mapping[str, object],
) -> bool:
    prior = entry.get("prior")
    if not isinstance(prior, Mapping) or not _prepared_prior_absent_leaf(step, prior):
        return True
    if _state_matches(step, installed):
        return True
    authority = entry.get("creation_authority")
    file_type = step.get("asset_type", "file")
    if not _valid_creation_authority(authority, file_type=file_type):
        return False
    matcher = getattr(backend, "creation_authority_matches", None)
    if not callable(matcher):
        return False
    try:
        return bool(matcher(step, authority))
    except Exception:
        return False


def _candidate_venv_step(
    plan: Mapping[str, object],
) -> Mapping[str, object] | None:
    steps = plan.get("apply_order", [])
    if not isinstance(steps, list):
        raise InstallPlanError("apply_order must be a list")
    candidates = [
        step
        for step in steps
        if isinstance(step, Mapping) and step.get("kind") == "venv"
    ]
    if len(candidates) > 1:
        raise InstallPlanError("apply_order must contain at most one candidate venv")
    return candidates[0] if candidates else None


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _bind_candidate_venv(
    *,
    plan: Mapping[str, object],
    receipt: InstallReceipt,
    backend: InstallBackend,
) -> None:
    step = _candidate_venv_step(plan)
    if step is None:
        return
    observed = backend.inspect_step(step)
    tree_sha256 = observed.get("tree_sha256")
    path = step.get("path")
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or observed.get("path") != path
        or observed.get("installed_sha256") != step.get("desired_sha256")
        or not _valid_sha256(tree_sha256)
    ):
        raise InstallDriftError("installed candidate venv tree is not attestable")
    receipt._document["candidate_venv"] = {
        "path": path,
        "tree_sha256": tree_sha256,
    }
    receipt._persist()


def _attest_candidate_venv(
    *,
    plan: Mapping[str, object],
    receipt: InstallReceipt,
    backend: object,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    step = _candidate_venv_step(plan)
    if step is None:
        return None, None
    binding = receipt._document.get("candidate_venv")
    expected_path = binding.get("path") if isinstance(binding, Mapping) else None
    expected_tree = (
        binding.get("tree_sha256") if isinstance(binding, Mapping) else None
    )
    evidence = {
        "path": expected_path,
        "tree_sha256": expected_tree,
        "observed_tree_sha256": None,
    }
    inspector = getattr(backend, "inspect_step", None)
    if not callable(inspector):
        return evidence, {
            "code": "candidate_venv_tree_drift",
            "artifact": "candidate-venv",
            "expected": expected_tree,
            "installed": None,
        }
    try:
        observed = inspector(step)
    except Exception as exc:
        return evidence, {
            "code": "candidate_venv_tree_drift",
            "artifact": "candidate-venv",
            "expected": expected_tree,
            "installed": None,
            "inspection_error": type(exc).__name__,
        }
    actual_tree = observed.get("tree_sha256") if isinstance(observed, Mapping) else None
    evidence["observed_tree_sha256"] = actual_tree
    matches = (
        isinstance(observed, Mapping)
        and isinstance(binding, Mapping)
        and set(binding) == {"path", "tree_sha256"}
        and expected_path == step.get("path")
        and observed.get("path") == expected_path
        and observed.get("installed_sha256") == step.get("desired_sha256")
        and _valid_sha256(expected_tree)
        and actual_tree == expected_tree
    )
    if matches:
        return evidence, None
    return evidence, {
        "code": "candidate_venv_tree_drift",
        "artifact": "candidate-venv",
        "expected": expected_tree,
        "installed": actual_tree,
    }


def _prepared_step_requires_replay(step: Mapping[str, object]) -> bool:
    """Return whether a prepared step has no inspectable completion evidence."""

    return (
        step.get("kind") == "systemctl"
        and step.get("action") == "daemon-reload"
    )


def _planned_service_exec_paths(
    plan: Mapping[str, object],
) -> dict[str, str]:
    generated = plan.get("generated")
    units = generated.get("units") if isinstance(generated, Mapping) else None
    if not isinstance(units, Mapping):
        raise InstallPlanError("plan generated service units are invalid")
    paths: dict[str, str] = {}
    for service in _SERVICE_ORDER:
        row = units.get(service)
        content = row.get("content") if isinstance(row, Mapping) else None
        if not isinstance(content, str):
            raise InstallPlanError(f"plan service unit is missing: {service}")
        executable = next(
            (
                line.partition("=")[2].split()[0]
                for line in _functional_lines(content)
                if line.startswith("ExecStart=") and line.partition("=")[2].split()
            ),
            None,
        )
        if not isinstance(executable, str) or not executable.startswith("/"):
            raise InstallPlanError(
                f"plan service unit lacks an absolute ExecStart: {service}"
            )
        paths[service] = executable
    return paths


def _bind_expected_service_executables(
    *,
    plan: Mapping[str, object],
    receipt: InstallReceipt,
    identities: Mapping[str, object],
) -> None:
    planned_paths = _planned_service_exec_paths(plan)
    bindings: dict[str, dict[str, str]] = {}
    for service, planned_path in planned_paths.items():
        identity = identities.get(service)
        if not isinstance(identity, Mapping):
            raise InstallDriftError(
                f"installed service executable identity is missing: {service}"
            )
        actual_path = identity.get("exec_path")
        executable_hash = identity.get("exec_sha256")
        if actual_path != planned_path:
            raise InstallDriftError(
                f"installed service executable path does not match plan: {service}"
            )
        if (
            not isinstance(executable_hash, str)
            or len(executable_hash) != 64
            or any(char not in "0123456789abcdef" for char in executable_hash)
        ):
            raise InstallDriftError(
                f"installed service executable hash is invalid: {service}"
            )
        bindings[service] = {
            "exec_path": planned_path,
            "sha256": executable_hash,
        }
    receipt._document["expected_service_executables"] = bindings
    receipt._persist()


def apply_plan(
    plan: Mapping[str, object],
    *,
    confirm_sha256: str,
    receipt: InstallReceipt,
    backend: InstallBackend,
) -> InstallReceipt:
    expected_hash = plan_sha256(plan)
    if confirm_sha256 != expected_hash or receipt._document.get("plan_sha256") != expected_hash:
        raise InstallPlanError("confirm-sha256 does not match the canonical plan sha256")
    steps = plan.get("apply_order", [])
    if not isinstance(steps, list):
        raise InstallPlanError("apply_order must be a list")
    for step in steps:
        if not isinstance(step, Mapping) or step.get("kind") not in {
            "account",
            "asset",
            "systemctl",
            "venv",
            "toolchain",
            "repository",
        }:
            raise InstallPlanError(f"unknown or untyped apply step kind: {step!r}")
        _validate_operations(step)
    facts = backend.preflight_facts(plan)
    report = validate_preflight(plan, facts, receipt=receipt)
    if not report.ok:
        details = "; ".join(row["detail"] for row in report.failures)
        raise InstallError(f"preflight failed: {details}")

    receipt._document["state"] = "applying"
    receipt._persist()
    journal = receipt._document.get("journal")
    if not isinstance(journal, list):
        raise InstallError("receipt journal is invalid")
    completed = {str(row.get("step_id")): row for row in journal if isinstance(row, Mapping)}
    for step in steps:
        step_id = str(step.get("step_id"))
        if step_id in completed:
            entry = completed[step_id]
            installed = backend.inspect_step(step)
            if _state_matches(step, installed) and not (
                entry.get("status") == "prepared"
                and _prepared_step_requires_replay(step)
            ):
                if entry.get("status") == "prepared":
                    entry["status"] = "completed"
                    entry.update(installed)
                    receipt._persist()
                continue
            if entry.get("status") != "prepared":
                raise InstallDriftError(f"completed install step drifted: {step_id}")
            prior = entry.get("prior")
            if not isinstance(prior, Mapping):
                raise InstallDriftError(
                    f"prepared install step lacks prior state: {step_id}"
                )
            if dict(installed) != dict(prior):
                # A crash may happen after only part of a backend step mutated
                # the host.  The prepared journal is already the rollback
                # authority only after the backend durably binds a newly
                # created leaf's inode.  A prepared intent alone must never
                # delete a third-party object created before our mutation.
                if not _prepared_account_group_is_replayable(step, prior, installed):
                    if not _prepared_leaf_has_rollback_authority(
                        backend=backend,
                        step=step,
                        entry=entry,
                        installed=installed,
                    ):
                        raise InstallDriftError(
                            "prepared install step lacks matching creation "
                            f"authority: {step_id}"
                        )
                    backend.rollback_step(entry)
                    restored = backend.inspect_step(step)
                    if dict(restored) != dict(prior):
                        raise InstallDriftError(
                            f"prepared install step could not restore prior state: {step_id}"
                        )
        else:
            prior = dict(backend.inspect_step(step))
            adopted_from_receipt = False
            adopted_mount_root: dict[str, int] | None = None
            replayed_mount_root = False
            if (
                step.get("kind") in {"asset", "repository"}
                and _state_matches(step, prior)
            ):
                adopted_from_receipt = _step_has_receipt_provenance(
                    plan=plan, step=step, receipt=receipt
                )
                adopted_mount_root = _mount_adoption_authority_from_receipt(
                    plan=plan, step=step, receipt=receipt, installed=prior
                )
                replayed_mount_root = adopted_mount_root is not None
                if (
                    adopted_mount_root is None
                    and _explicit_empty_managed_mount_is_adoptable(
                        plan=plan,
                        step=step,
                        installed=prior,
                        receipt=receipt,
                    )
                ):
                    adopted_mount_root = _observed_mount_authority(prior)
                if not (adopted_from_receipt or adopted_mount_root):
                    raise InstallDriftError(
                        f"existing {step.get('kind')} lacks trusted receipt provenance: {step_id}"
                    )
            entry = {
                "step_id": step_id,
                "step": deepcopy(dict(step)),
                "status": "prepared",
                "prior": prior,
            }
            if adopted_from_receipt:
                entry["adopted_from_receipt"] = True
            if adopted_mount_root is not None:
                entry["adopted_mount_root"] = adopted_mount_root
                if replayed_mount_root:
                    entry["adopted_from_receipt"] = True
            journal.append(entry)
            completed[step_id] = entry
            receipt._persist()
        def checkpoint_creation(authority: Mapping[str, object]) -> None:
            file_type = step.get("asset_type", "file")
            prior = entry.get("prior")
            if (
                not isinstance(prior, Mapping)
                or not _prepared_prior_absent_leaf(step, prior)
                or not _valid_creation_authority(authority, file_type=file_type)
            ):
                raise InstallError(
                    f"backend returned invalid creation authority: {step_id}"
                )
            previous = entry.get("creation_authority")
            if previous is not None and previous != authority:
                raise InstallDriftError(
                    f"backend changed creation authority: {step_id}"
                )
            entry["creation_authority"] = deepcopy(dict(authority))
            try:
                receipt._persist()
            except BaseException:
                if previous is None:
                    entry.pop("creation_authority", None)
                else:
                    entry["creation_authority"] = previous
                raise

        try:
            checkpointed_apply = getattr(backend, "apply_step_checkpointed", None)
            outcome = dict(
                checkpointed_apply(step, checkpoint_creation)
                if callable(checkpointed_apply)
                else backend.apply_step(step)
            )
            outcome_authority = outcome.get("creation_authority")
            if outcome_authority is not None:
                if not _valid_creation_authority(
                    outcome_authority,
                    file_type=step.get("asset_type", "file"),
                ) or (
                    entry.get("creation_authority") is not None
                    and entry.get("creation_authority") != outcome_authority
                ):
                    raise InstallDriftError(
                        f"backend returned conflicting creation authority: {step_id}"
                    )
        except BaseException:
            # If the backend demonstrably made no mutation, keep the receipt as
            # compact as the pre-two-phase format. If state changed, the durable
            # prepared entry is the replay/rollback authority.
            observed = backend.inspect_step(step)
            if dict(observed) == dict(entry.get("prior", {})):
                journal.remove(entry)
                completed.pop(step_id, None)
                receipt._persist()
            raise
        entry.update({key: value for key, value in outcome.items() if key != "prior"})
        entry["status"] = "completed"
        receipt._persist()
    _bind_candidate_venv(plan=plan, receipt=receipt, backend=backend)
    identity_reader = getattr(backend, "service_identities", None)
    if callable(identity_reader):
        identities = identity_reader()
        if not isinstance(identities, Mapping):
            raise InstallDriftError("installed service identities are invalid")
        _bind_expected_service_executables(
            plan=plan, receipt=receipt, identities=identities
        )
    receipt._document["state"] = "applied"
    receipt._persist()
    return receipt


@dataclass(frozen=True)
class RollbackReport:
    retained_unknown: tuple[str, ...]
    retained_drift: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "retained_unknown": list(self.retained_unknown),
            "retained_drift": [dict(row) for row in self.retained_drift],
        }


def _activation_entries(receipt: InstallReceipt) -> list[dict[str, object]]:
    journal = receipt._document.setdefault("activation_journal", [])
    if not isinstance(journal, list) or any(
        not isinstance(entry, dict)
        or set(entry) != {"service", "status"}
        or entry.get("service") not in _SERVICE_ORDER
        or entry.get("status") not in {"prepared", "completed"}
        for entry in journal
    ):
        raise InstallError("receipt activation journal is invalid")
    services = [str(entry["service"]) for entry in journal]
    if len(services) != len(set(services)) or services != sorted(
        services, key=_SERVICE_ORDER.index
    ):
        raise InstallError("receipt activation authority is invalid")
    return journal


def _sync_activation_state(receipt: InstallReceipt) -> None:
    services = [str(entry["service"]) for entry in _activation_entries(receipt)]
    receipt._document["running_services"] = services
    receipt._document["services_started"] = bool(services)


def _forget_activation_entry(
    receipt: InstallReceipt, entry: dict[str, object]
) -> None:
    journal = _activation_entries(receipt)
    index = journal.index(entry)
    journal.pop(index)
    _sync_activation_state(receipt)
    try:
        receipt._persist()
    except BaseException:
        journal.insert(index, entry)
        _sync_activation_state(receipt)
        raise


def rollback_receipt(
    receipt: InstallReceipt, *, backend: InstallBackend
) -> RollbackReport:
    journal = receipt._document.get("journal", [])
    if not isinstance(journal, list):
        raise InstallError("receipt journal is invalid")
    retained_drift: list[dict[str, object]] = []
    service_stop_failures: list[str] = []
    activation_journal = _activation_entries(receipt)
    if not activation_journal and receipt._document.get("services_started"):
        raw_running = receipt._document.get("running_services")
        running = (
            [service for service in _SERVICE_ORDER if service in raw_running]
            if isinstance(raw_running, list) and raw_running
            else list(_SERVICE_ORDER)
        )
        activation_journal.extend(
            {"service": service, "status": "completed"} for service in running
        )
        _sync_activation_state(receipt)
        receipt._persist()
    for activation_entry in reversed(list(activation_journal)):
        service = str(activation_entry["service"])
        try:
            backend.stop_service(service)
        except Exception as exc:
            service_stop_failures.append(service)
            retained_drift.append(
                {
                    "step_id": f"service:{service}",
                    "observed": {"error": str(exc)},
                }
            )
        else:
            _forget_activation_entry(receipt, activation_entry)
    if service_stop_failures:
        receipt._document["state"] = "rollback-blocked"
        receipt._document["activated"] = False
        receipt._document["qualified"] = False
        _sync_activation_state(receipt)
        receipt._document["rollback"] = {
            "retained_unknown": [],
            "retained_drift": retained_drift,
        }
        receipt._persist()
        return RollbackReport((), tuple(retained_drift))

    if journal and receipt._document.get("state") != "rolling-back":
        # Archive the full authority before removing entries one by one.  A
        # crash can then resume from the remaining live journal without losing
        # retained-account provenance.
        receipt._document["state"] = "rolling-back"
        receipt._document["rollback_journal"] = deepcopy(journal)
        receipt._persist()
    elif journal:
        archived = receipt._document.get("rollback_journal")
        if not isinstance(archived, list) or not archived:
            raise InstallError("resumed rollback lacks archived journal authority")

    credential_rollback = getattr(backend, "rollback_credentials", None)
    if callable(credential_rollback):
        retained_drift.extend(
            {
                "step_id": f"credential:{row.get('credential', 'unknown')}",
                "observed": dict(row),
            }
            for row in credential_rollback(receipt)
            if isinstance(row, Mapping)
        )
    def forget_install_entry(entry: Mapping[str, object]) -> None:
        index = journal.index(entry)
        removed = journal.pop(index)
        try:
            receipt._persist()
        except BaseException:
            journal.insert(index, removed)
            raise

    for entry in reversed(list(journal)):
        if not isinstance(entry, Mapping):
            continue
        step = entry.get("step", entry)
        if not isinstance(step, Mapping):
            continue
        installed = backend.inspect_step(step)
        prior = entry.get("prior")
        if isinstance(prior, Mapping) and dict(installed) == dict(prior):
            # The prior rollback may have completed its mutation just before a
            # receipt checkpoint failed.  This entry is already restored, not
            # post-install drift.
            forget_install_entry(entry)
            continue
        if entry.get("status") == "prepared":
            if not isinstance(prior, Mapping):
                retained_drift.append(
                    {"step_id": entry.get("step_id"), "observed": dict(installed)}
                )
                continue
            if (
                dict(installed) != dict(prior)
                and not _prepared_leaf_has_rollback_authority(
                    backend=backend,
                    step=step,
                    entry=entry,
                    installed=installed,
                )
            ):
                retained_drift.append(
                    {"step_id": entry.get("step_id"), "observed": dict(installed)}
                )
                continue
            try:
                if dict(installed) != dict(prior):
                    backend.rollback_step(entry)
                restored = backend.inspect_step(step)
            except Exception as exc:
                retained_drift.append(
                    {
                        "step_id": entry.get("step_id"),
                        "observed": {"error": str(exc)},
                    }
                )
                continue
            if (
                dict(restored) != dict(prior)
                and step.get("rollback_policy") != "retain"
            ):
                retained_drift.append(
                    {"step_id": entry.get("step_id"), "observed": dict(restored)}
                )
                continue
            forget_install_entry(entry)
            continue
        if not _state_matches(step, installed):
            retained_drift.append(
                {"step_id": entry.get("step_id"), "observed": dict(installed)}
            )
            continue
        backend.rollback_step(entry)
        forget_install_entry(entry)
    unknown = tuple(backend.list_unknown_state(receipt))
    receipt._document["state"] = "rolled-back"
    receipt._document["activated"] = False
    receipt._document["qualified"] = False
    _sync_activation_state(receipt)
    credentials_retained = any(
        str(row.get("step_id", "")).startswith("credential:")
        for row in retained_drift
    )
    if not credentials_retained:
        receipt._document["credentials"] = []
        receipt._document["credential_journal"] = []
    receipt._document["rollback"] = {
        "retained_unknown": list(unknown),
        "retained_drift": retained_drift,
    }
    receipt._persist()
    return RollbackReport(unknown, tuple(retained_drift))


@dataclass(frozen=True)
class CredentialMetadata:
    principal: str
    provider: str
    mode: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "principal": self.principal,
            "provider": self.provider,
            "mode": self.mode,
            "sha256": self.sha256,
        }


_CREDENTIAL_ADAPTERS: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {
    ("builder", "codex"): ("auth.json", (".codex", "auth.json")),
    ("reviewer-planner", "codex"): (
        "auth.json",
        (".codex", "auth.json"),
    ),
    ("reviewer-planner", "agy"): (
        "oauth_creds.json",
        ("cache", "gemini", "oauth_creds.json"),
    ),
    ("reviewer-planner", "copilot"): (
        "hosts.json",
        (".config", "github-copilot", "hosts.json"),
    ),
    ("manager", "github"): (
        "hosts.yml",
        (".config", "gh", "hosts.yml"),
    ),
}

_PRINCIPAL_ACCOUNTS = {
    "builder": "cortex-builder",
    "reviewer-planner": "cortex-reviewer-planner",
    "manager": "cortex-manager",
}


def credential_destination(
    receipt: InstallReceipt, *, principal: str, provider: str
) -> tuple[Path, int, int]:
    adapter = _CREDENTIAL_ADAPTERS.get((principal, provider))
    if adapter is None:
        raise CredentialImportError(
            f"provider/principal pair is not allowed: {principal}/{provider}"
        )
    account_name = _PRINCIPAL_ACCOUNTS.get(principal)
    plan = receipt._document.get("plan")
    accounts = plan.get("accounts", []) if isinstance(plan, Mapping) else []
    account = next(
        (
            row
            for row in accounts
            if isinstance(row, Mapping) and row.get("name") == account_name
        ),
        None,
    )
    if not isinstance(account, Mapping) or not all(
        isinstance(account.get(key), expected)
        for key, expected in (("home", str), ("uid", int), ("gid", int))
    ):
        raise CredentialImportError(f"receipt lacks account identity for {principal}")
    _name, destination_parts = adapter
    return (
        Path(str(account["home"])).joinpath(*destination_parts),
        int(account["uid"]),
        int(account["gid"]),
    )


def _open_unnamed_credential_tmpfile(parent_fd: int) -> int | None:
    """Prefer Linux O_TMPFILE; return None for a journaled named fallback."""

    flag = getattr(os, "O_TMPFILE", 0)
    try:
        linkat = getattr(ctypes.CDLL(None), "linkat")
    except (AttributeError, OSError):
        return None
    if not flag or linkat is None or not Path("/proc/self/fd").is_dir():
        return None
    try:
        return os.open(
            ".",
            os.O_RDWR | flag | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        if exc.errno in {
            errno.EINVAL,
            errno.EISDIR,
            errno.ENOSYS,
            errno.EOPNOTSUPP,
        }:
            return None
        raise


def _publish_credential_tmpfile(
    descriptor: int, parent_fd: int, destination_name: str
) -> None:
    """Atomically link an O_TMPFILE inode at its final name."""

    libc = ctypes.CDLL(None, use_errno=True)
    linkat = libc.linkat
    linkat.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    )
    linkat.restype = ctypes.c_int
    if linkat(
        descriptor,
        ctypes.c_char_p(b""),
        parent_fd,
        ctypes.c_char_p(os.fsencode(destination_name)),
        0x1000,  # AT_EMPTY_PATH
    ) != 0:
        error = ctypes.get_errno()
        if error not in {errno.ENOENT, errno.EPERM}:
            raise OSError(error, os.strerror(error))
        os.link(
            f"/proc/self/fd/{descriptor}",
            destination_name,
            dst_dir_fd=parent_fd,
            follow_symlinks=True,
        )


def _credential_temp_name(destination_name: str, digest: str) -> str:
    prefix = destination_name[:96]
    return f".{prefix}.cortex-pending-{digest[:24]}"


def _open_regular_at(parent_fd: int, name: str) -> int:
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    observed = os.fstat(descriptor)
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        os.close(descriptor)
        raise CredentialImportError("credential destination is not a safe regular file")
    return descriptor


def _credential_source_metadata(observed: os.stat_result) -> tuple[int, ...]:
    """Return identity/shape authority fields; content is checked separately."""

    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_nlink,
        observed.st_uid,
        observed.st_gid,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _assert_credential_source_unchanged(
    path: Path,
    *,
    parent_authority: Sequence[tuple[int, int]],
    metadata: tuple[int, ...],
    content: bytes,
    digest: bytes,
) -> None:
    """Reopen source authority and prove both metadata and bytes are unchanged."""

    current_authority: list[tuple[int, int]] = []
    parent_fd = authority_fd = content_fd = -1
    try:
        parent_fd, source_name = _open_parent_directory(
            path, authority=current_authority
        )
        if tuple(current_authority) != tuple(parent_authority):
            raise CredentialImportError(
                "credential source changed during validation"
            )
        authority_fd = os.open(
            source_name,
            getattr(os, "O_PATH", os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        if _credential_source_metadata(os.fstat(authority_fd)) != metadata:
            raise CredentialImportError(
                "credential source changed during validation"
            )
        content_fd = os.open(
            f"/proc/self/fd/{authority_fd}",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        if _credential_source_metadata(os.fstat(content_fd)) != metadata:
            raise CredentialImportError(
                "credential source changed during validation"
            )
        observed_content = _read_fd_bytes(content_fd)
        if (
            hashlib.sha256(observed_content).digest() != digest
            or observed_content != content
            or _credential_source_metadata(os.fstat(content_fd)) != metadata
            or _credential_source_metadata(os.fstat(authority_fd)) != metadata
        ):
            raise CredentialImportError(
                "credential source changed during validation"
            )
        _assert_fd_path_binding(
            path,
            authority_fd,
            directory=False,
            parent_authority=parent_authority,
        )
    except CredentialImportError:
        raise
    except (OSError, UnsafeInstallPathError) as exc:
        raise CredentialImportError(
            "credential source changed during validation"
        ) from exc
    finally:
        for descriptor in (content_fd, authority_fd, parent_fd):
            if descriptor >= 0:
                os.close(descriptor)


def import_credential(
    receipt: InstallReceipt,
    *,
    principal: str,
    provider: str,
    source: Path,
    destination_root: Path,
    destination_uid: int | None = None,
    destination_gid: int | None = None,
) -> CredentialMetadata:
    if receipt._document.get("state") != "applied":
        raise CredentialImportError("credentials may only be imported into an applied receipt")
    adapter = _CREDENTIAL_ADAPTERS.get((principal, provider))
    if adapter is None:
        raise CredentialImportError(
            f"provider/principal pair is not allowed: {principal}/{provider}"
        )
    allowed_name, destination_parts = adapter
    if source.name != allowed_name:
        raise CredentialImportError(
            f"{provider} source filename is outside the allowlist; expected {allowed_name}"
        )
    source = source.expanduser().absolute()
    source_authority: list[tuple[int, int]] = []
    try:
        source_parent_fd, source_name = _open_parent_directory(
            source, authority=source_authority
        )
    except UnsafeInstallPathError as exc:
        raise CredentialImportError(
            "credential source path contains a symlink"
        ) from exc
    except OSError as exc:
        raise CredentialImportError("credential source is not readable") from exc
    source_authority_fd = -1
    source_fd = -1
    try:
        # O_PATH binds the leaf inode without reading a FIFO/device; the content
        # descriptor is then reopened from this trusted kernel-held authority.
        source_authority_fd = os.open(
            source_name,
            getattr(os, "O_PATH", os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=source_parent_fd,
        )
    except OSError as exc:
        os.close(source_parent_fd)
        if exc.errno == errno.ELOOP:
            raise CredentialImportError(
                "credential source must not be a symlink"
            ) from exc
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            raise CredentialImportError("credential source is not readable") from exc
        raise CredentialImportError(
            "credential source changed or cannot be opened safely"
        ) from exc
    try:
        initial = os.fstat(source_authority_fd)
        if stat.S_ISLNK(initial.st_mode):
            raise CredentialImportError("credential source must not be a symlink")
        if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
            raise CredentialImportError(
                "credential source must be a single-link regular file"
            )
        source_metadata = _credential_source_metadata(initial)
        try:
            source_fd = os.open(
                f"/proc/self/fd/{source_authority_fd}",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(source_fd)
            if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
                raise CredentialImportError(
                    "credential source changed during validation"
                )
            content = _read_fd_bytes(source_fd)
        except OSError as exc:
            raise CredentialImportError("credential source read failed") from exc
        final = os.fstat(source_fd)
        final_authority = os.fstat(source_authority_fd)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or _credential_source_metadata(final) != source_metadata
            or _credential_source_metadata(final_authority) != source_metadata
        ):
            raise CredentialImportError("credential source changed during validation")
        try:
            _assert_fd_path_binding(
                source,
                source_authority_fd,
                directory=False,
                parent_authority=source_authority,
            )
        except UnsafeInstallPathError as exc:
            raise CredentialImportError(
                "credential source changed during validation"
            ) from exc
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        os.close(source_authority_fd)
        os.close(source_parent_fd)
    source_digest = hashlib.sha256(content).digest()

    def assert_source_unchanged() -> None:
        _assert_credential_source_unchanged(
            source,
            parent_authority=source_authority,
            metadata=source_metadata,
            content=content,
            digest=source_digest,
        )

    assert_source_unchanged()
    digest = source_digest.hex()
    destination = destination_root.joinpath(*destination_parts)
    if destination_uid is not None or destination_gid is not None:
        if (
            not isinstance(destination_uid, int)
            or destination_uid < 0
            or not isinstance(destination_gid, int)
            or destination_gid < 0
        ):
            raise CredentialImportError(
                "credential destination uid/gid must be non-negative integers"
            )
    credentials = receipt._document.setdefault("credentials", [])
    if not isinstance(credentials, list):
        raise CredentialImportError("receipt credentials field is invalid")
    credential_journal = receipt._document.setdefault("credential_journal", [])
    if not isinstance(credential_journal, list):
        raise CredentialImportError("receipt credential journal is invalid")
    metadata = CredentialMetadata(principal, provider, "0600", digest)
    metadata_row = metadata.to_dict()
    previous = next(
        (
            row
            for row in credentials
            if isinstance(row, Mapping)
            and row.get("principal") == principal
            and row.get("provider") == provider
        ),
        None,
    )
    pending = next(
        (
            row
            for row in credential_journal
            if isinstance(row, Mapping)
            and row.get("principal") == principal
            and row.get("provider") == provider
        ),
        None,
    )
    if pending is not None and any(
        pending.get(key) != value for key, value in metadata_row.items()
    ):
        raise CredentialImportError(
            f"credential import has conflicting prepared authority: {principal}/{provider}"
        )

    def persist_completion(prepared: Mapping[str, object]) -> None:
        prior_credentials = deepcopy(credentials)
        prior_journal = deepcopy(credential_journal)
        credentials[:] = [
            row
            for row in credentials
            if not (
                isinstance(row, Mapping)
                and row.get("principal") == principal
                and row.get("provider") == provider
            )
        ]
        credentials.append(metadata_row)
        credential_journal.remove(prepared)
        try:
            receipt._persist()
        except OSError as exc:
            credentials[:] = prior_credentials
            credential_journal[:] = prior_journal
            raise CredentialImportError(
                "credential receipt persistence failed"
            ) from exc
        except BaseException:
            credentials[:] = prior_credentials
            credential_journal[:] = prior_journal
            raise

    try:
        parent_fd, destination_name = _open_parent_directory(
            destination, create=True, create_mode=0o700
        )
    except (OSError, UnsafeInstallPathError) as exc:
        raise CredentialImportError("credential destination preparation failed") from exc
    descriptor: int | None = None
    published = False

    def remove_published_destination() -> None:
        if not published or descriptor is None:
            return
        try:
            current = os.stat(
                destination_name, dir_fd=parent_fd, follow_symlinks=False
            )
            held = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino):
                os.unlink(destination_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
        except OSError:
            pass

    try:
        try:
            installed_fd = _open_regular_at(parent_fd, destination_name)
        except FileNotFoundError:
            installed_fd = None
        except OSError as exc:
            raise CredentialImportError(
                "credential destination inspection failed"
            ) from exc
        if installed_fd is not None:
            try:
                observed = os.fstat(installed_fd)
                installed_digest = hashlib.sha256(
                    _read_fd_bytes(installed_fd)
                ).hexdigest()
                _assert_fd_path_binding(
                    destination, installed_fd, directory=False
                )
            finally:
                os.close(installed_fd)
            owner_ok = destination_uid is None or observed.st_uid == destination_uid
            group_ok = destination_gid is None or observed.st_gid == destination_gid
            authority = pending if pending is not None else previous
            if (
                stat.S_IMODE(observed.st_mode) == 0o600
                and owner_ok
                and group_ok
                and authority is not None
                and authority.get("sha256") == digest
                and installed_digest == digest
            ):
                assert_source_unchanged()
                if pending is not None:
                    persist_completion(pending)
                return metadata
            raise CredentialImportError(
                "credential destination already exists without matching receipt "
                f"authority: {principal}/{provider}"
            )
        if pending is None:
            pending = {**metadata_row, "status": "prepared"}
            credential_journal.append(pending)
        try:
            receipt._persist()
        except OSError as exc:
            raise CredentialImportError(
                "credential receipt persistence failed"
            ) from exc

        temp_name = pending.get("temp_name")
        if temp_name is not None:
            if (
                not isinstance(temp_name, str)
                or Path(temp_name).name != temp_name
                or not temp_name.startswith(".")
                or temp_name in {".", ".."}
            ):
                raise CredentialImportError("credential fallback journal is invalid")
            try:
                temp_observed = os.stat(
                    temp_name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                temp_observed = None
            if temp_observed is not None:
                if (
                    stat.S_ISREG(temp_observed.st_mode)
                    and temp_observed.st_nlink == 1
                    and stat.S_IMODE(temp_observed.st_mode) == 0
                ):
                    os.unlink(temp_name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                else:
                    descriptor = _open_regular_at(parent_fd, temp_name)
                    temp_observed = os.fstat(descriptor)
                    temp_digest = hashlib.sha256(
                        _read_fd_bytes(descriptor)
                    ).hexdigest()
                    owner_ok = (
                        destination_uid is None
                        or temp_observed.st_uid == destination_uid
                    )
                    group_ok = (
                        destination_gid is None
                        or temp_observed.st_gid == destination_gid
                    )
                    if (
                        stat.S_IMODE(temp_observed.st_mode) != 0o600
                        or not owner_ok
                        or not group_ok
                        or temp_digest != digest
                    ):
                        raise CredentialImportError(
                            "credential fallback temp does not match journal authority"
                        )
                    assert_source_unchanged()
                    os.replace(
                        temp_name,
                        destination_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    published = True
                    os.fsync(parent_fd)
                    assert_source_unchanged()
                    _assert_fd_path_binding(
                        destination, descriptor, directory=False
                    )
                    os.close(descriptor)
                    descriptor = None
                    persist_completion(pending)
                    return metadata

        named_fallback = temp_name is not None
        if not named_fallback:
            descriptor = _open_unnamed_credential_tmpfile(parent_fd)
            named_fallback = descriptor is None
        if named_fallback:
            if temp_name is None:
                temp_name = _credential_temp_name(destination_name, digest)
                assert isinstance(pending, MutableMapping)
                pending["temp_name"] = temp_name
                try:
                    receipt._persist()
                except OSError as exc:
                    raise CredentialImportError(
                        "credential receipt persistence failed"
                    ) from exc
            descriptor = os.open(
                temp_name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0,
                dir_fd=parent_fd,
            )
        assert descriptor is not None
        with os.fdopen(os.dup(descriptor), "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if destination_uid is not None and destination_gid is not None:
            os.fchown(descriptor, destination_uid, destination_gid)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        assert_source_unchanged()
        if named_fallback:
            assert isinstance(temp_name, str)
            os.replace(
                temp_name,
                destination_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        else:
            _publish_credential_tmpfile(descriptor, parent_fd, destination_name)
        published = True
        os.fsync(parent_fd)
        assert_source_unchanged()
        _assert_fd_path_binding(destination, descriptor, directory=False)
    except CredentialImportError:
        remove_published_destination()
        raise
    except UnsafeInstallPathError as exc:
        remove_published_destination()
        raise CredentialImportError(
            "credential destination changed during write"
        ) from exc
    except OSError as exc:
        raise CredentialImportError("credential destination write failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)
    persist_completion(pending)
    return metadata


_SERVICE_ORDER = (
    "cortex-egress-proxy.service",
    "cortex-manager.service",
    "cortex-monitor.service",
)


def activate_receipt(
    receipt: InstallReceipt, *, backend: InstallBackend
) -> InstallReceipt:
    if receipt._document.get("state") != "applied":
        raise ActivationError("receipt must be fully applied before activation")
    plan = receipt._document.get("plan", {})
    if not isinstance(plan, Mapping):
        raise ActivationError("receipt plan is invalid")
    _candidate_evidence, candidate_failure = _attest_candidate_venv(
        plan=plan,
        receipt=receipt,
        backend=backend,
    )
    if candidate_failure is not None:
        raise ActivationError("candidate venv tree drift")
    required = plan.get("required_credentials", []) if isinstance(plan, Mapping) else []
    imported = {
        (row.get("principal"), row.get("provider"))
        for row in receipt._document.get("credentials", [])
        if isinstance(row, Mapping)
    }
    missing = [
        row
        for row in required
        if isinstance(row, Mapping)
        and (row.get("principal"), row.get("provider")) not in imported
    ]
    if missing:
        rendered = ", ".join(
            f"{row.get('principal')}/{row.get('provider')}" for row in missing
        )
        raise ActivationError(f"missing required credential: {rendered}")
    validator = getattr(backend, "validate_credentials", None)
    if callable(validator):
        failures = tuple(str(row) for row in validator(receipt))
        if failures:
            raise ActivationError(
                "imported credential validation failed: " + "; ".join(failures)
            )
    activation_journal = _activation_entries(receipt)
    if activation_journal:
        raise ActivationError(
            "receipt has unfinished activation authority; roll it back first"
        )
    failed_service = "service"
    failure_phase = "start"
    try:
        for service in _SERVICE_ORDER:
            failed_service = service
            entry: dict[str, object] = {
                "service": service,
                "status": "prepared",
            }
            activation_journal.append(entry)
            # The prepared entry is durable before systemctl can mutate the
            # unit.  It remains conservative authority if this persist fails.
            failure_phase = "prepared activation checkpoint"
            receipt._persist()
            failure_phase = "start"
            try:
                backend.start_service(service)
            except Exception:
                # A failed systemctl start can still leave a partially-active
                # unit. Include the attempted service in reverse compensation.
                raise
            entry["status"] = "completed"
            try:
                failure_phase = "completed activation checkpoint"
                receipt._persist()
            except BaseException:
                # The last durable state is prepared.  Preserve the same state
                # in memory so compensation/recovery never trusts completion.
                entry["status"] = "prepared"
                raise
        receipt._document.pop("activation_failure", None)
        _sync_activation_state(receipt)
        # Activation is intentionally provisional until verify emits passing evidence.
        receipt._document["activated"] = False
        receipt._document["qualified"] = False
        failure_phase = "final activation checkpoint"
        receipt._persist()
    except Exception as exc:
        compensation_failures: list[str] = []
        compensation_persistence_failures: list[str] = []
        for entry in reversed(list(activation_journal)):
            service = str(entry["service"])
            try:
                backend.stop_service(service)
            except Exception:
                compensation_failures.append(service)
            else:
                try:
                    _forget_activation_entry(receipt, entry)
                except Exception:
                    # The helper restores the entry on checkpoint failure.  It
                    # remains replay authority while compensation continues
                    # with every earlier service.
                    compensation_persistence_failures.append(service)
        _sync_activation_state(receipt)
        receipt._document["activated"] = False
        receipt._document["qualified"] = False
        receipt._document["activation_failure"] = {
            "phase": failure_phase,
            "failed_service": failed_service,
            "compensation_failures": list(reversed(compensation_failures)),
            "compensation_persistence_failures": list(
                compensation_persistence_failures
            ),
        }
        failure_record_persisted = True
        try:
            receipt._persist()
        except Exception:
            # The per-service activation entries were already durable before
            # every start.  Preserve and report that replay boundary even if
            # the aggregate failure record cannot be checkpointed.
            failure_record_persisted = False
        detail = (
            f"failed to start {failed_service}: {exc}"
            if failure_phase == "start"
            else f"failed {failure_phase} for {failed_service}: {exc}"
        )
        if compensation_failures:
            detail += "; failed to stop " + ", ".join(
                reversed(compensation_failures)
            )
        if compensation_persistence_failures:
            detail += "; failed to persist compensation for " + ", ".join(
                compensation_persistence_failures
            )
        if not failure_record_persisted:
            detail += "; failed to persist aggregate activation failure"
        raise ActivationError(detail) from exc
    return receipt


@dataclass(frozen=True)
class AttestationReport:
    warnings: tuple[dict[str, object], ...]
    failures: tuple[dict[str, object], ...]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "warnings": [dict(row) for row in self.warnings],
            "failures": [dict(row) for row in self.failures],
        }


_INVENTORY_CATEGORIES = (
    "units",
    "shim",
    "polkit",
    "gitconfigs",
    "toolchain_wrappers",
    "environment",
    "enforcement",
)


def _functional_lines(content: str) -> list[str]:
    return [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    ]


def attest_generated_inventory(
    *,
    expected: Mapping[str, Mapping[str, Mapping[str, str]]],
    installed: Mapping[str, Mapping[str, Mapping[str, str]]],
) -> AttestationReport:
    warnings: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for category in _INVENTORY_CATEGORIES:
        expected_rows = expected.get(category)
        installed_rows = installed.get(category)
        if not isinstance(expected_rows, Mapping) or not isinstance(installed_rows, Mapping):
            failures.append(
                {
                    "code": "missing_inventory_category",
                    "artifact": category,
                }
            )
            continue
        for unexpected in sorted(set(installed_rows) - set(expected_rows)):
            failures.append(
                {
                    "code": "unexpected_authority_artifact",
                    "artifact": f"{category}/{unexpected}",
                }
            )
        for name, expected_row in expected_rows.items():
            artifact = f"{category}/{name}"
            actual_row = installed_rows.get(name)
            if not isinstance(actual_row, Mapping):
                failures.append({"code": "missing_artifact", "artifact": artifact})
                continue
            metadata_drift = {
                key: {"expected": expected_row.get(key), "installed": actual_row.get(key)}
                for key in ("owner", "group", "mode")
                if expected_row.get(key) != actual_row.get(key)
            }
            if metadata_drift:
                failures.append(
                    {
                        "code": "metadata_drift",
                        "artifact": artifact,
                        "fields": metadata_drift,
                    }
                )
            expected_content = str(expected_row.get("content", ""))
            actual_content = str(actual_row.get("content", ""))
            expected_functional = _functional_lines(expected_content)
            actual_functional = _functional_lines(actual_content)
            if expected_functional != actual_functional:
                missing = [
                    line for line in expected_functional if line not in actual_functional
                ]
                failures.append(
                    {
                        "code": "functional_drift",
                        "artifact": artifact,
                        "missing_functional_lines": missing,
                    }
                )
            elif expected_content != actual_content:
                warnings.append(
                    {"code": "comment_only_drift", "artifact": artifact}
                )
    return AttestationReport(tuple(warnings), tuple(failures))


@dataclass(frozen=True)
class VerificationResult:
    report: AttestationReport
    evidence: Mapping[str, object]

    @property
    def ok(self) -> bool:
        return self.report.ok

    def to_dict(self) -> dict[str, object]:
        return deepcopy(dict(self.evidence))


def verify_receipt(
    receipt: InstallReceipt,
    *,
    plan: Mapping[str, object],
    expected_inventory: Mapping[str, Mapping[str, Mapping[str, str]]],
    installed_inventory: Mapping[str, Mapping[str, Mapping[str, str]]],
    service_identities: Mapping[str, Mapping[str, str]],
    evidence_path: Path,
    service_controller: object | None = None,
) -> VerificationResult:
    if not receipt._document.get("services_started"):
        raise ActivationError("services must be started before verify")
    if receipt._document.get("plan_sha256") != plan_sha256(plan):
        raise InstallDriftError("receipt is not bound to the supplied plan")
    report = attest_generated_inventory(
        expected=expected_inventory, installed=installed_inventory
    )
    candidate_venv, candidate_failure = _attest_candidate_venv(
        plan=plan,
        receipt=receipt,
        backend=service_controller,
    )
    if candidate_failure is not None:
        report = AttestationReport(
            report.warnings, (*report.failures, candidate_failure)
        )
    identity_failures: list[dict[str, object]] = []
    expected_users: dict[str, str] = {}
    expected_exec_paths: dict[str, str] = {}
    expected_executables = receipt._document.get("expected_service_executables")
    unit_rows = expected_inventory.get("units", {})
    if isinstance(unit_rows, Mapping):
        for service in _SERVICE_ORDER:
            row = unit_rows.get(service)
            if not isinstance(row, Mapping):
                identity_failures.append(
                    {"code": "missing_service_unit", "artifact": service}
                )
                continue
            for line in _functional_lines(str(row.get("content", ""))):
                if line.startswith("User="):
                    expected_users[service] = line.partition("=")[2]
                elif line.startswith("ExecStart="):
                    expected_exec_paths[service] = line.partition("=")[2].split()[0]
    for service in _SERVICE_ORDER:
        identity = service_identities.get(service)
        if not isinstance(identity, Mapping):
            identity_failures.append(
                {"code": "missing_service_identity", "artifact": service}
            )
            continue
        expected_user = expected_users.get(service)
        if expected_user and identity.get("user") != expected_user:
            identity_failures.append(
                {
                    "code": "service_user_drift",
                    "artifact": service,
                    "expected": expected_user,
                    "installed": identity.get("user"),
                }
            )
        expected_exec_path = expected_exec_paths.get(service)
        binding = (
            expected_executables.get(service)
            if isinstance(expected_executables, Mapping)
            else None
        )
        if not isinstance(binding, Mapping):
            identity_failures.append(
                {
                    "code": "missing_expected_service_executable",
                    "artifact": service,
                }
            )
        elif expected_exec_path and binding.get("exec_path") != expected_exec_path:
            identity_failures.append(
                {
                    "code": "service_exec_binding_drift",
                    "artifact": service,
                    "expected": expected_exec_path,
                    "installed": binding.get("exec_path"),
                }
            )
        if expected_exec_path and identity.get("exec_path") != expected_exec_path:
            identity_failures.append(
                {
                    "code": "service_exec_path_drift",
                    "artifact": service,
                    "expected": expected_exec_path,
                    "installed": identity.get("exec_path"),
                }
            )
        executable_hash = identity.get("exec_sha256")
        if (
            not isinstance(executable_hash, str)
            or len(executable_hash) != 64
            or any(char not in "0123456789abcdef" for char in executable_hash)
        ):
            identity_failures.append(
                {"code": "missing_service_exec_hash", "artifact": service}
            )
        elif isinstance(binding, Mapping) and executable_hash != binding.get("sha256"):
            identity_failures.append(
                {
                    "code": "service_exec_hash_drift",
                    "artifact": service,
                    "expected": binding.get("sha256"),
                    "installed": executable_hash,
                }
            )
        if identity.get("active_state") != "active":
            identity_failures.append(
                {
                    "code": "service_not_active",
                    "artifact": service,
                    "installed": identity.get("active_state"),
                }
            )
    if identity_failures:
        report = AttestationReport(
            report.warnings, (*report.failures, *identity_failures)
        )
    artifact_hashes: dict[str, str] = {}
    for category, rows in installed_inventory.items():
        if not isinstance(rows, Mapping):
            continue
        for name, row in rows.items():
            if isinstance(row, Mapping):
                content = str(row.get("content", "")).encode("utf-8")
                artifact_hashes[f"{category}/{name}"] = hashlib.sha256(content).hexdigest()
    evidence: dict[str, object] = {
        "schema_version": 1,
        "result": "pass" if report.ok else "fail",
        "plan_sha256": plan_sha256(plan),
        "receipt_id": receipt._document["receipt_id"],
        "candidate": deepcopy(plan.get("candidate", {})),
        "service_identities": deepcopy(dict(service_identities)),
        "artifact_hashes": artifact_hashes,
        "attestation": report.to_dict(),
    }
    if candidate_venv is not None:
        evidence["candidate_venv"] = candidate_venv
    atomic_write_json(evidence_path.absolute(), evidence, mode=0o600)
    receipt._document["activated"] = report.ok
    receipt._document["qualified"] = report.ok
    if not report.ok and service_controller is not None:
        stop_failures: list[str] = []
        for service in reversed(_SERVICE_ORDER):
            try:
                service_controller.stop_service(service)
            except Exception:
                stop_failures.append(service)
        receipt._document["running_services"] = list(reversed(stop_failures))
        receipt._document["services_started"] = bool(stop_failures)
        if stop_failures:
            receipt._document["verification_stop_failures"] = list(
                reversed(stop_failures)
            )
        else:
            receipt._document.pop("verification_stop_failures", None)
    receipt._document["verification_evidence"] = {
        "sha256": _sha256_file(evidence_path),
        "result": evidence["result"],
    }
    receipt._persist()
    return VerificationResult(report, evidence)
