"""Transactional trust-root installation primitives.

Planning in this module is deliberately rootless and deterministic.  Mutation is
performed only through an explicit backend seam so the same transaction rules can
be exercised without inspecting or changing the host.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, MutableMapping, Protocol, Sequence

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


def _reject_sensitive_config(value: object, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.casefold()
            if normalized in _FORBIDDEN_CONFIG_FIELDS or normalized.endswith(
                ("_password", "_secret", "_token")
            ):
                raise InstallPlanError(
                    f"configuration field {'.'.join((*path, key))} is forbidden"
                )
            _reject_sensitive_config(child, path=(*path, key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_sensitive_config(child, path=(*path, str(index)))


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
    return layout, roots


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

    wrappers: dict[str, dict[str, str]] = {}
    if isinstance(toolchain, Mapping):
        for raw_name in sorted(toolchain):
            name = str(raw_name)
            if not name or name in {".", ".."} or "/" in name:
                raise InstallPlanError(f"unsafe toolchain program name: {name!r}")
            wrapper_path = f"{layout.toolchain_bin}/{name}"
            executable = f"{layout.toolchain_lib}/{name}"
            content = f'#!/bin/sh\nexec "{executable}" "$@"\n'
            wrappers[name] = _artifact_dict(
                content=content, path=wrapper_path, mode="0755"
            )
    return {
        "units": unit_inventory,
        "shim": shim_inventory,
        "polkit": polkit_inventory,
        "gitconfigs": gitconfigs,
        "toolchain_wrappers": wrappers,
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
    }
    return hashlib.sha256(_canonical_bytes(semantic)).hexdigest()


def _apply_steps(
    *, assets: Sequence[Mapping[str, object]], generated: Mapping[str, Mapping[str, Mapping[str, str]]]
) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    for asset in assets:
        path = asset.get("path")
        if not isinstance(path, str) or "<job-id>" in path:
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
        steps.append(step)
    for category in ("units", "shim", "polkit", "gitconfigs", "toolchain_wrappers"):
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


def build_install_plan(
    *, config: Mapping[str, object], candidate_wheel: Path, bundle: Path
) -> dict[str, object]:
    """Build a pure, exact-artifact-bound four-way desired-state plan."""

    if config.get("schema_version") != 1:
        raise InstallPlanError("config schema_version must be 1")
    _reject_sensitive_config(config)
    wheel_path = _validate_regular_file(Path(candidate_wheel), label="candidate wheel")
    bundle_path = _validate_regular_file(Path(bundle), label="bundle")
    scheme = _configured_scheme(config)
    accounts = _account_rows(config, scheme)
    layout, roots = _layout_from_config(config, accounts)
    permission_plan = permgen.generate_plan(scheme)
    permgen.assert_principals_resolved(permission_plan, scheme)
    asset_paths = layout.asset_paths()
    assets: list[dict[str, object]] = []
    for entry in permission_plan.entries:
        row = entry.to_dict()
        row["path"] = asset_paths.get(entry.asset_id)
        assets.append(row)
    generated = _generated_inventory(
        scheme, layout, roots, permission_plan, config.get("toolchain", {})
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
        "roots": roots,
        "assets": assets,
        "generated": generated,
        "provider_manifest": deepcopy(config.get("providers", {})),
        "toolchain_manifest": deepcopy(config.get("toolchain", {})),
        "required_credentials": required_credentials,
        # Account adoption/creation and the hash-locked venv cutover are
        # deliberately first.  The initial production backend supports typed
        # file assets and systemctl only, so it rejects these known-but-
        # unsupported steps before mutating the filesystem rather than ever
        # claiming a partial installation is "applied".
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
                }
                for row in accounts
            ),
            {
                "step_id": "candidate-venv",
                "kind": "venv",
                "path": f"{roots['deploy']}/venv.new",
                "wheel_sha256": _sha256_file(wheel_path),
                "wheelhouse_locked": True,
            },
            *_apply_steps(assets=assets, generated=generated),
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
    return {
        "schema_version": 1,
        "candidate_sha": candidate_sha,
        "wheel": wheel,
        "wheelhouse": wheelhouse,
        "generated_artifacts": generated,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
    }


@dataclass(frozen=True)
class PreflightReport:
    failures: tuple[dict[str, str], ...]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "failures": [dict(row) for row in self.failures]}


def validate_preflight(
    plan: Mapping[str, object], facts: Mapping[str, object]
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
    for desired in plan.get("accounts", []):
        if not isinstance(desired, Mapping) or not isinstance(desired.get("name"), str):
            raise InstallPlanError("plan account entries must be typed objects")
        name = str(desired["name"])
        observed = observed_accounts.get(name)
        if observed is None:
            continue
        if not isinstance(observed, Mapping) or any(
            observed.get(key) != desired.get(key) for key in ("uid", "gid", "home", "shell")
        ):
            raise AccountCollisionError(f"existing account {name} does not match the plan")

    observed_paths = facts.get("paths", {})
    if not isinstance(observed_paths, Mapping):
        observed_paths = {}
    for step in plan.get("apply_order", []):
        if not isinstance(step, Mapping):
            raise InstallPlanError("apply_order entries must be typed objects")
        path = step.get("path")
        observed = observed_paths.get(path, {})
        if isinstance(observed, Mapping) and observed.get("is_symlink"):
            raise UnsafeInstallPathError(f"apply path became a symlink: {path}")
    return PreflightReport(tuple(failures))


class InstallBackend(Protocol):
    def preflight_facts(self, plan: Mapping[str, object]) -> Mapping[str, object]: ...
    def inspect_step(self, step: Mapping[str, object]) -> Mapping[str, object]: ...
    def apply_step(self, step: Mapping[str, object]) -> Mapping[str, object]: ...
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
            atomic_write_json(self.path, self._document, mode=0o600)

    @classmethod
    def load(cls, path: Path) -> "InstallReceipt":
        _reject_symlink_ancestors(path, label="receipt")
        try:
            if path.is_symlink():
                raise UnsafeInstallPathError(f"receipt must not be a symlink: {path}")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(f"cannot load receipt {path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise InstallError(f"invalid receipt schema: {path}")
        return cls(payload, path=path)


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
        }:
            raise InstallPlanError(f"unknown or untyped apply step kind: {step!r}")
        _validate_operations(step)
    facts = backend.preflight_facts(plan)
    report = validate_preflight(plan, facts)
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
            installed = backend.inspect_step(step)
            if not _state_matches(step, installed):
                raise InstallDriftError(f"completed install step drifted: {step_id}")
            continue
        outcome = dict(backend.apply_step(step))
        entry = {"step_id": step_id, "step": deepcopy(dict(step)), **outcome}
        journal.append(entry)
        receipt._persist()
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


def rollback_receipt(
    receipt: InstallReceipt, *, backend: InstallBackend
) -> RollbackReport:
    journal = receipt._document.get("journal", [])
    if not isinstance(journal, list):
        raise InstallError("receipt journal is invalid")
    retained_drift: list[dict[str, object]] = []
    retained_entries: list[object] = []
    for entry in reversed(journal):
        if not isinstance(entry, Mapping):
            continue
        step = entry.get("step", entry)
        if not isinstance(step, Mapping):
            continue
        installed = backend.inspect_step(step)
        if not _state_matches(step, installed):
            retained_drift.append(
                {"step_id": entry.get("step_id"), "observed": dict(installed)}
            )
            retained_entries.append(entry)
            continue
        backend.rollback_step(entry)
    unknown = tuple(backend.list_unknown_state(receipt))
    receipt._document["state"] = "rolled-back"
    receipt._document["activated"] = False
    receipt._document["qualified"] = False
    receipt._document["rollback"] = {
        "retained_unknown": list(unknown),
        "retained_drift": retained_drift,
    }
    # A clean rollback is a supported reinstall boundary.  Preserve the old
    # journal as audit data while making apply_order replayable from step zero.
    receipt._document["rollback_journal"] = deepcopy(journal)
    receipt._document["journal"] = list(reversed(retained_entries))
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
        (".gemini", "oauth_creds.json"),
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


def import_credential(
    receipt: InstallReceipt,
    *,
    principal: str,
    provider: str,
    source: Path,
    destination_root: Path,
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
    try:
        mode = source.lstat().st_mode
    except OSError as exc:
        raise CredentialImportError(f"credential source is not readable: {exc}") from exc
    if stat.S_ISLNK(mode):
        raise CredentialImportError("credential source must not be a symlink")
    if not stat.S_ISREG(mode):
        raise CredentialImportError("credential source must be a regular file")
    _reject_symlink_ancestors(source, label="credential source", include_leaf=False)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise CredentialImportError(
            f"credential source changed or cannot be opened safely: {exc}"
        ) from exc
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (source.lstat().st_dev, source.lstat().st_ino):
            raise CredentialImportError("credential source changed during validation")
        with os.fdopen(source_fd, "rb") as stream:
            content = stream.read()
        source_fd = -1
    finally:
        if source_fd >= 0:
            os.close(source_fd)
    digest = hashlib.sha256(content).hexdigest()
    destination = destination_root.joinpath(*destination_parts)
    if destination.is_symlink():
        raise CredentialImportError("credential destination must not be a symlink")
    _reject_symlink_ancestors(destination, label="credential destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
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
    metadata = CredentialMetadata(principal, provider, "0600", digest)
    credentials = receipt._document.setdefault("credentials", [])
    if not isinstance(credentials, list):
        raise CredentialImportError("receipt credentials field is invalid")
    credentials[:] = [
        row
        for row in credentials
        if not (
            isinstance(row, Mapping)
            and row.get("principal") == principal
            and row.get("provider") == provider
        )
    ]
    credentials.append(metadata.to_dict())
    receipt._persist()
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
    started: list[str] = []
    try:
        for service in _SERVICE_ORDER:
            backend.start_service(service)
            started.append(service)
    except Exception as exc:
        for service in reversed(started):
            try:
                backend.stop_service(service)
            except Exception:
                pass
        receipt._document["services_started"] = False
        receipt._document["activated"] = False
        receipt._document["qualified"] = False
        receipt._persist()
        service = _SERVICE_ORDER[len(started)] if len(started) < len(_SERVICE_ORDER) else "service"
        raise ActivationError(f"failed to start {service}: {exc}") from exc
    receipt._document["services_started"] = True
    # Activation is intentionally provisional until verify emits passing evidence.
    receipt._document["activated"] = False
    receipt._document["qualified"] = False
    receipt._persist()
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
) -> VerificationResult:
    if not receipt._document.get("services_started"):
        raise ActivationError("services must be started before verify")
    if receipt._document.get("plan_sha256") != plan_sha256(plan):
        raise InstallDriftError("receipt is not bound to the supplied plan")
    report = attest_generated_inventory(
        expected=expected_inventory, installed=installed_inventory
    )
    identity_failures: list[dict[str, object]] = []
    expected_users: dict[str, str] = {}
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
                    break
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
        executable_hash = identity.get("exec_sha256")
        if not isinstance(executable_hash, str) or len(executable_hash) != 64:
            identity_failures.append(
                {"code": "missing_service_exec_hash", "artifact": service}
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
    atomic_write_json(evidence_path.absolute(), evidence, mode=0o600)
    receipt._document["activated"] = report.ok
    receipt._document["qualified"] = report.ok
    receipt._document["verification_evidence"] = {
        "sha256": _sha256_file(evidence_path),
        "result": evidence["result"],
    }
    receipt._persist()
    return VerificationResult(report, evidence)
