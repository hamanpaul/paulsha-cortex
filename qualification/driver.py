#!/usr/bin/env python3
"""Trusted executable probes for Cortex RC qualification.

The driver is copied into the reference image before the candidate is mounted.
It never imports qualification verdicts from the candidate checkout.  A missing
probe, missing provider-native runtime identity, or missing protected probe
repository is a hard failure.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PROVIDERS = {
    "agy": ("gemini-3.7-flash", "high", "cortex-reviewer-planner"),
    "copilot": ("gpt-5.4", "xhigh", "cortex-reviewer-planner"),
    "codex": ("gpt-5", "normal", "cortex-builder"),
}
SERVICES = (
    "cortex-egress-proxy.service",
    "cortex-manager.service",
    "cortex-monitor.service",
)


@dataclass(frozen=True)
class ProviderPreflightAdapter:
    version: str | None
    version_command: tuple[str, ...]
    status_command: tuple[str, ...] | None


PROVIDER_PREFLIGHTS = {
    # agy 1.1.18 has no status/login/quota subcommand.  In particular, passing
    # the word "status" starts print mode with that word as a prompt.
    "agy": ProviderPreflightAdapter(
        version="1.1.18",
        version_command=("/opt/cortex/toolchain/bin/agy", "--version"),
        status_command=None,
    ),
    # The staged Copilot CLI likewise has no qualification-approved structured
    # login/quota status interface.  Keep this fail-closed rather than guessing
    # from human-readable output.
    "copilot": ProviderPreflightAdapter(
        version=None,
        version_command=("/opt/cortex/toolchain/bin/copilot", "--version"),
        status_command=None,
    ),
    # doctor --json is supported by Codex 0.149.0.  Its schema is inspected
    # below; this pinned version does not expose live login or quota fields.
    "codex": ProviderPreflightAdapter(
        version="0.149.0",
        version_command=("/opt/cortex/toolchain/bin/codex", "--version"),
        status_command=(
            "/opt/cortex/toolchain/bin/codex",
            "doctor",
            "--json",
        ),
    ),
}


class QualificationFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _canonical_json_hash(value: object) -> str:
    content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(content).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as stream:
        stream.write(_canonical_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    argv: Sequence[str],
    *,
    user: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 120,
) -> CommandResult:
    command = list(argv)
    if user is not None:
        command = ["/usr/sbin/runuser", "-u", user, "--", *command]
    process_env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}
    if env:
        process_env.update(env)
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=process_env,
        check=False,
    )
    return CommandResult(
        tuple(command), completed.returncode, completed.stdout, completed.stderr
    )


def _require_success(result: CommandResult, label: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        raise QualificationFailure(f"{label} failed rc={result.returncode}: {detail}")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationFailure(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise QualificationFailure(f"{label} must be a JSON object")
    return value


def _account_env(account: str) -> dict[str, str]:
    home = pwd.getpwnam(account).pw_dir
    env = {
        "HOME": home,
        "PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin",
        "NO_COLOR": "1",
        "CI": "true",
    }
    manager_env = Path("/opt/cortex/etc/cortex-manager.env")
    if manager_env.is_file() and not manager_env.is_symlink():
        for raw in manager_env.read_text(
            encoding="utf-8", errors="strict"
        ).splitlines():
            key, separator, value = raw.partition("=")
            if separator and key in {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}:
                env[key] = value
    return env


def _installed_runtime_env() -> dict[str, str]:
    """Load only the installed, root-owned PSC runtime projection for operator CLI probes."""

    path = Path("/opt/cortex/etc/cortex-manager.env")
    if path.is_symlink() or not path.is_file():
        raise QualificationFailure("installed Manager environment is absent")
    if path.stat().st_uid != 0 or stat.S_IMODE(path.stat().st_mode) & 0o022:
        raise QualificationFailure(
            "installed Manager environment is not root-controlled"
        )
    env = {
        "HOME": "/root",
        "PATH": "/opt/cortex/venv/bin:/opt/cortex/toolchain/bin:/usr/bin:/bin",
    }
    for raw in path.read_text(encoding="utf-8", errors="strict").splitlines():
        key, separator, encoded = raw.partition("=")
        if not separator or not key.startswith("PSC_"):
            continue
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise QualificationFailure(
                f"invalid installed runtime value for {key}"
            ) from exc
        if not isinstance(value, str) or "\x00" in value:
            raise QualificationFailure(f"invalid installed runtime value for {key}")
        env[key] = value
    env.setdefault("PSC_CONTROL_ROOT", "/var/lib/cortex/control")
    env.setdefault("PSC_COORDINATOR_ROOT", "/var/lib/cortex/coordinator")
    env.setdefault("PSC_SPECS_ROOT", "/var/lib/cortex/specs")
    env.setdefault("PSC_MONITOR_STATE_ROOT", "/var/lib/cortex/monitor")
    return env


def _account_runtime_env(account: str) -> dict[str, str]:
    """Project installed runtime roots without changing the probed identity."""

    env = _account_env(account)
    env.update(
        {
            key: value
            for key, value in _installed_runtime_env().items()
            if key.startswith("PSC_")
        }
    )
    return env


def _service_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name in SERVICES:
        active = _run(("systemctl", "is-active", name))
        identity = _run(
            (
                "systemctl",
                "show",
                name,
                "--property=User",
                "--property=Group",
                "--property=MainPID",
                "--no-pager",
            )
        )
        _require_success(identity, f"service identity {name}")
        values = dict(
            line.split("=", 1) for line in identity.stdout.splitlines() if "=" in line
        )
        user = values.get("User", "")
        group = values.get("Group", user)
        try:
            uid = pwd.getpwnam(user).pw_uid
            import grp

            gid = grp.getgrnam(group).gr_gid
        except KeyError as exc:
            raise QualificationFailure(
                f"service {name} has unresolved identity"
            ) from exc
        rows.append(
            {"name": name, "uid": uid, "gid": gid, "active": active.returncode == 0}
        )
    return rows


def _installed_checks(
    *, install_evidence: Path, receipt: Mapping[str, Any], evidence_dir: Path
) -> list[dict[str, str]]:
    install = _load_json(install_evidence, "install verification evidence")
    if (
        install.get("result") != "pass"
        or install.get("attestation", {}).get("ok") is not True
    ):
        raise QualificationFailure("install verification did not pass")
    _write_json(evidence_dir / "install-verification.json", install)
    _write_json(
        evidence_dir / "generated-installed-attestation.json",
        {
            "schema_version": 1,
            "ok": True,
            "attestation": install["attestation"],
            "artifact_hashes": install.get("artifact_hashes", {}),
            "service_identities": install.get("service_identities", {}),
        },
    )
    python = "/opt/cortex/venv/bin/python"
    selfcheck = _run((python, "-m", "paulsha_cortex.trust_root", "selfcheck"))
    _require_success(selfcheck, "trust-root selfcheck")
    selfcheck_payload = json.loads(selfcheck.stdout)
    if (
        selfcheck_payload.get("ok") is not True
        or selfcheck_payload.get("job_writable_count") != 0
    ):
        raise QualificationFailure(
            "trust-root selfcheck reported writable authority state"
        )
    equation = _run((python, "-m", "paulsha_cortex.trust_root", "equation"))
    _require_success(equation, "registry equation")
    equation_payload = json.loads(equation.stdout)
    if equation_payload.get("ok") is not True:
        raise QualificationFailure("registry equation is not balanced")
    _write_json(
        evidence_dir / "install-semantic-checks.json",
        {
            "schema_version": 1,
            "selfcheck": selfcheck_payload,
            "registry_equation": equation_payload,
            "receipt_id": receipt.get("receipt_id"),
        },
    )
    return [
        {"name": "selfcheck", "status": "passed"},
        {"name": "registry-equation", "status": "passed"},
        {"name": "generated-installed-attestation", "status": "passed"},
        {"name": "service-identity-hardening", "status": "passed"},
    ]


def _denied(
    cases: list[dict[str, object]],
    *,
    family: str,
    case_id: str,
    user: str,
    argv: Sequence[str],
    timeout: int = 30,
    expected_returncodes: set[int] | None = None,
) -> None:
    result = _run(argv, user=user, env=_account_env(user), timeout=timeout)
    passed = (
        result.returncode in expected_returncodes
        if expected_returncodes is not None
        else result.returncode != 0
    )
    cases.append(
        {
            "family": family,
            "case": case_id,
            "principal": user,
            "status": "passed" if passed else "failed",
            "returncode": result.returncode,
        }
    )
    if not passed:
        raise QualificationFailure(
            f"{family}/{case_id} did not return an allowed denial as {user}: "
            f"rc={result.returncode}"
        )


def _fs_probe(expression: str) -> tuple[str, ...]:
    """Run a filesystem mutation and expose the actual errno as the exit code."""

    code = (
        "import errno,sys\n"
        "from pathlib import Path\n"
        "try:\n"
        + "\n".join(f"    {line}" for line in expression.splitlines())
        + "\nexcept OSError as exc:\n"
        "    raise SystemExit(exc.errno or 1)\n"
        "raise SystemExit(0)\n"
    )
    return ("/usr/bin/python3", "-c", code)


def _fs_denied(
    cases: list[dict[str, object]],
    *,
    family: str,
    case_id: str,
    user: str,
    expression: str,
) -> None:
    _denied(
        cases,
        family=family,
        case_id=case_id,
        user=user,
        argv=_fs_probe(expression),
        # Sticky-directory ownership checks return EPERM; ordinary DAC and
        # systemd read-only mounts return EACCES/EROFS. ENOENT is a false green.
        expected_returncodes={errno.EACCES, errno.EPERM, errno.EROFS},
    )


def _fs_allowed(
    cases: list[dict[str, object]],
    *,
    family: str,
    case_id: str,
    user: str,
    expression: str,
) -> None:
    """Run a filesystem mutation that the installed plan explicitly allows.

    R9 is authority-aware: a job-visible staging asset may be writable by its
    declared producer, while the same operation must be denied to every other
    headless principal.  Recording the successful operation in the attack
    matrix prevents a missing ACL from being mistaken for a protected asset.
    """

    result = _run(_fs_probe(expression), user=user, env=_account_env(user), timeout=30)
    cases.append(
        {
            "family": family,
            "case": case_id,
            "principal": user,
            "status": "passed" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
        }
    )
    if result.returncode != 0:
        # Keep a failed authority probe actionable: an ACL failure is otherwise
        # reported only as errno 13 and the disposable host is gone before an
        # operator can inspect the parent traverse contract.
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:200]
        match = re.search(r"Path\((['\"])(/[^'\"]+)\1\)", expression)
        if match:
            target = match.group(2)
            diagnostics: list[str] = []
            for argv in (
                ("namei", "-l", target),
                ("getfacl", "-p", str(Path(target).parent)),
                ("getfacl", "-p", target),
            ):
                probe = _run(argv)
                text = (probe.stdout or probe.stderr).strip().replace("\n", " ")
                if text:
                    diagnostics.append(text[:500])
            if diagnostics:
                detail = f"{detail} {' | '.join(diagnostics)}".strip()
        raise QualificationFailure(
            f"{family}/{case_id} authorized as {user} failed "
            f"rc={result.returncode}: {detail}"
        )


def _positive(
    controls: list[dict[str, object]],
    *,
    family: str,
    case_id: str,
    user: str,
    argv: Sequence[str],
) -> None:
    result = _run(argv, user=user, env=_account_env(user), timeout=30)
    controls.append(
        {
            "family": family,
            "case": case_id,
            "principal": user,
            "status": "passed" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
        }
    )
    _require_success(result, f"negative control {case_id}")


def _passed_case(
    cases: list[dict[str, object]],
    *,
    family: str,
    case_id: str,
    user: str,
    argv: Sequence[str],
) -> None:
    """Record a probe whose safe result is a successful bounded inspection."""

    result = _run(argv, user=user, env=_account_env(user), timeout=30)
    cases.append(
        {
            "family": family,
            "case": case_id,
            "principal": user,
            "status": "passed" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
        }
    )
    _require_success(result, f"{family}/{case_id}")


def _runtime_workspace_provisioning_spec(
    assets: Sequence[object],
) -> tuple[Path, tuple[tuple[str, str, str], ...]]:
    """Derive the synthetic per-job workspace from the installed plan.

    The installer intentionally creates only the shared pool.  Qualification
    therefore provisions one disposable per-job slot before probing assets
    whose paths contain ``<job-id>``.  The ACL contract must come from the
    plan's ``repo-worktree`` row; duplicating it in this trusted driver would
    let qualification silently drift from production provisioning.
    """

    matches = [
        asset
        for asset in assets
        if isinstance(asset, Mapping) and asset.get("asset_id") == "repo-worktree"
    ]
    if len(matches) != 1:
        raise QualificationFailure(
            "plan must contain exactly one repo-worktree runtime asset"
        )
    asset = matches[0]
    raw_path = asset.get("path")
    if (
        asset.get("tier") not in {"TIER_0", "TIER_1"}
        or asset.get("runtime_managed") is not True
        or asset.get("is_directory") is not True
        or not isinstance(raw_path, str)
        or raw_path.count("<job-id>") != 1
        or not raw_path.startswith("/")
    ):
        raise QualificationFailure("repo-worktree runtime asset shape is invalid")
    workspace = Path(raw_path.replace("<job-id>", "qualification-probe"))
    if workspace.name != "qualification-probe":
        raise QualificationFailure(
            "repo-worktree placeholder is not the final path segment"
        )

    raw_acls = asset.get("acls")
    if not isinstance(raw_acls, list) or not raw_acls:
        raise QualificationFailure("repo-worktree has no runtime ACL contract")
    paired: dict[str, dict[bool, str]] = {}
    for row in raw_acls:
        if not isinstance(row, Mapping):
            raise QualificationFailure("repo-worktree ACL row is invalid")
        account = row.get("account")
        perms = row.get("perms")
        default = row.get("default")
        if (
            not isinstance(account, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", account) is None
            or not isinstance(perms, str)
            or re.fullmatch(r"[rwxX-]{1,4}", perms) is None
            or not isinstance(default, bool)
        ):
            raise QualificationFailure("repo-worktree ACL row is invalid")
        by_kind = paired.setdefault(account, {})
        if default in by_kind:
            raise QualificationFailure("repo-worktree duplicates an ACL kind")
        by_kind[default] = perms
    if any(set(by_kind) != {False, True} for by_kind in paired.values()):
        raise QualificationFailure(
            "repo-worktree must provide an access/default ACL pair per account"
        )
    grants = tuple(
        (account, by_kind[False], by_kind[True])
        for account, by_kind in sorted(paired.items())
    )
    return workspace, grants


def _provision_runtime_workspace(assets: Sequence[object]) -> Path:
    """Create one disposable slot via the installed production ACL helper."""

    workspace, grants = _runtime_workspace_provisioning_spec(assets)
    pool = workspace.parent
    if not pool.is_dir() or pool.is_symlink() or pool.resolve() != pool:
        raise QualificationFailure("runtime workspace pool is absent or unsafe")
    if workspace.exists() or workspace.is_symlink():
        raise QualificationFailure("runtime qualification workspace already exists")

    workspace.mkdir(mode=0o700)
    cortex_dir = workspace / ".cortex"
    cortex_dir.mkdir(mode=0o700)
    seed = workspace / ".qualification-seed"
    seed.write_bytes(b"runtime workspace ACL seed\n")
    manager = pwd.getpwnam("cortex-manager")
    for path, mode in ((workspace, 0o700), (cortex_dir, 0o700), (seed, 0o600)):
        os.chown(path, manager.pw_uid, manager.pw_gid)
        os.chmod(path, mode)

    # Keep the candidate import outside this trusted process.  The driver then
    # independently attacks the resulting filesystem shape, so candidate code
    # cannot self-attest the verdict.
    code = (
        "import json,sys\n"
        "from paulsha_cortex.coordinator.job_workspace import "
        "WorkspaceAclGrant,grant_workspace_acl\n"
        "rows=json.loads(sys.argv[2])\n"
        "grants=tuple(WorkspaceAclGrant(*row) for row in rows)\n"
        "grant_workspace_acl(sys.argv[1],grants)\n"
    )
    result = _run(
        (
            "/opt/cortex/venv/bin/python",
            "-c",
            code,
            str(workspace),
            json.dumps(grants, separators=(",", ":")),
        ),
        user="cortex-manager",
        env=_installed_runtime_env(),
        timeout=60,
    )
    _require_success(result, "runtime workspace production ACL provisioning")
    return workspace


def _permission_attack_matrix(receipt: Mapping[str, Any], evidence_dir: Path) -> None:
    plan = receipt.get("plan")
    if not isinstance(plan, Mapping):
        raise QualificationFailure("receipt plan is missing")
    cases: list[dict[str, object]] = []
    controls: list[dict[str, object]] = []
    secret = Path("/run/cortex-qualification-secret")
    secret.write_text("qualification-only\n", encoding="utf-8")
    os.chmod(secret, 0o600)
    principals = ("cortex-builder", "cortex-reviewer-planner")
    for principal in principals:
        _fs_denied(
            cases,
            family="capability",
            case_id="T1.1-read-root-secret",
            user=principal,
            expression="Path('/run/cortex-qualification-secret').read_bytes()",
        )
        _fs_denied(
            cases,
            family="capability",
            case_id="T1.2-control-plane",
            user=principal,
            expression="list(Path('/var/lib/cortex/control').iterdir())",
        )
        cli = _run(
            (
                "/opt/cortex/venv/bin/cortex",
                "work",
                "ship",
                "r9-nonexistent",
                "--repo",
                "invalid/qualification",
            ),
            user=principal,
            env=_account_runtime_env(principal),
            timeout=30,
        )
        cli_text = (cli.stdout + cli.stderr).lower()
        cli_denied = cli.returncode != 0 and (
            "permission" in cli_text
            or "not ready" in cli_text
            or "未就緒" in cli_text
            or "denied" in cli_text
        )
        cases.append(
            {
                "family": "capability",
                "case": "T1.3-sensitive-cli",
                "principal": principal,
                "status": "passed" if cli_denied else "failed",
                "returncode": cli.returncode,
            }
        )
        if not cli_denied:
            detail = (cli.stderr or cli.stdout).strip().replace("\n", " ")[:500]
            raise QualificationFailure(
                "capability/T1.3 did not fail for a capability reason as "
                f"{principal}: rc={cli.returncode} output={detail!r}"
            )
        _fs_denied(
            cases,
            family="capability",
            case_id="T1.4-write-control-queue",
            user=principal,
            expression="Path('/var/lib/cortex/control/requests/.r9').write_bytes(b'x')",
        )

    manager_probe = Path("/var/lib/cortex/control/.qualification-negative-control")
    _positive(
        controls,
        family="capability",
        case_id="capability-manager-write",
        user="cortex-manager",
        argv=(
            "/usr/bin/python3",
            "-c",
            f"from pathlib import Path; Path({str(manager_probe)!r}).write_bytes(b'x')",
        ),
    )
    _positive(
        controls,
        family="capability",
        case_id="capability-root-read-secret",
        user="root",
        argv=(
            "/usr/bin/python3",
            "-c",
            "from pathlib import Path; Path('/run/cortex-qualification-secret').read_bytes()",
        ),
    )
    manager_probe.unlink(missing_ok=True)

    assets = plan.get("assets")
    if not isinstance(assets, list):
        raise QualificationFailure("plan asset inventory is missing")
    _provision_runtime_workspace(assets)
    operations = {
        "modify": "p.open('ab').write(b'x')",
        "truncate": "p.open('wb').close()",
        "delete": "p.unlink()",
        "replace": "q=p.with_name(p.name+'.replacement'); q.write_bytes(b'x'); q.replace(p)",
        "symlink-swap": "p.unlink(); p.symlink_to('/run/cortex-qualification-secret')",
        "rollback": "p.write_bytes(b'older-valid-content')",
    }
    covered_assets = 0
    registry_asset_ids: list[str] = []
    authorized_mutations: list[dict[str, str]] = []
    deny_only_assets: list[str] = []
    for asset in assets:
        if not isinstance(asset, Mapping) or asset.get("tier") not in {
            "TIER_0",
            "TIER_1",
        }:
            continue
        asset_id = asset.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            raise QualificationFailure("Tier-0/Tier-1 asset has no asset_id")
        registry_asset_ids.append(asset_id)
        deny_only = asset_id in {"review-verdict"}
        if deny_only:
            # Phase 2a's worktree-local verdict file remains registered as a
            # compatibility asset, but Phase 2b authority is the dedicated
            # review-verdict-spool.  Do not grant a reviewer write path into
            # the builder pool merely to make the legacy row appear writable.
            deny_only_assets.append(asset_id)
        writer_accounts = asset.get("writer_accounts")
        if not isinstance(writer_accounts, list) or any(
            not isinstance(account, str) or not account for account in writer_accounts
        ):
            raise QualificationFailure(
                f"Tier-0/Tier-1 asset has no canonical writer_accounts: {asset_id}"
            )
        raw_path = asset.get("path")
        if not isinstance(raw_path, str) or not raw_path.startswith("/"):
            raise QualificationFailure(
                f"durable asset has no absolute path: {asset_id}"
            )
        concrete = re.sub(r"<[^>]+>", "qualification-probe", raw_path).replace(
            "*", "qualification-probe"
        )
        authority = Path(concrete)
        if deny_only:
            # The legacy verdict row points at a job-visible file shape whose
            # placeholder is shared with the builder worktree in the static
            # registry.  Its Phase 2b authority is intentionally absent; use a
            # root-owned protected parent so the R9 probe cannot inherit the
            # builder ACL from the runtime worktree fixture or give the
            # reviewer a directory-level delete capability.
            container = Path(
                "/var/lib/cortex-reviewer-planner/.cortex-r9-review-verdict"
            )
            container.mkdir(mode=0o700, exist_ok=True)
            os.chown(container, 0, 0)
            os.chmod(container, 0o700)
        elif asset_id == "handoff-manifest":
            # This manager-only child shares the static job placeholder with
            # the runtime worktree.  Give the probe its own Manager-owned
            # parent so headless accounts cannot inherit builder ACLs while
            # the Manager positive control remains meaningful.
            container = Path(
                "/var/lib/cortex-manager/.cortex-r9-handoff-manifest"
            )
            container.mkdir(mode=0o700, exist_ok=True)
            manager = pwd.getpwnam("cortex-manager")
            os.chown(container, manager.pw_uid, manager.pw_gid)
            os.chmod(container, 0o700)
        else:
            container = authority if asset.get("is_directory") else authority.parent
        if not container.is_dir() or container.is_symlink():
            raise QualificationFailure(f"durable asset container is absent: {asset_id}")
        suffix = hashlib.sha256(asset_id.encode()).hexdigest()[:16]
        target = container / f".cortex-r9-{suffix}"
        try:
            target.write_bytes(b"current-valid-content")
        except OSError as exc:
            raise QualificationFailure(
                f"durable-state probe setup failed for {asset_id}: {target}: {exc}"
            ) from exc
        owner_name = asset.get("owner")
        if not isinstance(owner_name, str) or not owner_name:
            raise QualificationFailure(f"durable asset has no owner: {asset_id}")
        # A job-visible spool grants its producer ``wx`` on the directory.  The
        # producer creates the child inode, so the probe must model that
        # ownership before checking content mutations.  Starting with a
        # Manager-owned child would make a valid write-only spool ACL look like
        # a denial.  Runtime-managed worktrees are the exception: their
        # production helper deliberately keeps the Manager as inode owner and
        # grants the job account a named ACL recursively.
        # A deny-only legacy asset is intentionally protected from both
        # headless accounts.  Keep the synthetic probe root-owned so restore
        # remains possible in the rootless Docker fixture without inventing a
        # writer account or a cross-worktree ACL.
        target_owner_name = "root" if deny_only else owner_name
        if (
            asset.get("is_directory") is True
            and asset.get("runtime_managed") is not True
            and not deny_only
        ):
            target_owner_name = next(
                (
                    principal
                    for principal in principals
                    if principal in writer_accounts
                ),
                owner_name,
            )
        try:
            owner = pwd.getpwnam(target_owner_name)
        except KeyError as exc:
            raise QualificationFailure(
                f"durable asset probe owner is not an installed account: {asset_id}"
            ) from exc
        os.chown(target, owner.pw_uid if not deny_only else 0, owner.pw_gid if not deny_only else 0)
        raw_mode = asset.get("mode")
        if not isinstance(raw_mode, str) or re.fullmatch(r"[0-7]{4,5}", raw_mode) is None:
            raise QualificationFailure(f"durable asset mode is invalid: {asset_id}")
        os.chmod(target, 0o600 if deny_only else int(raw_mode, 8) & 0o777)
        raw_acls = asset.get("acls", [])
        if not isinstance(raw_acls, list):
            raise QualificationFailure(f"durable asset ACL inventory is invalid: {asset_id}")
        for row in (() if deny_only else raw_acls):
            if not isinstance(row, Mapping) or row.get("default") is True:
                continue
            account = row.get("account")
            perms = row.get("perms")
            if not isinstance(account, str) or not isinstance(perms, str):
                raise QualificationFailure(f"durable asset ACL row is invalid: {asset_id}")
            _require_success(
                _run(("setfacl", "-m", f"u:{account}:{perms}", str(target))),
                f"R9 ACL proxy setup for {asset_id}/{account}",
            )
        baseline = target.read_bytes()
        covered_assets += 1
        for principal in principals:
            for operation, expression in operations.items():
                case_id = f"{asset_id}:{operation}"
                if principal in writer_accounts and not deny_only:
                    _fs_allowed(
                        cases,
                        family="durable-state",
                        case_id=case_id,
                        user=principal,
                        expression=f"p=Path({str(target)!r})\n{expression}",
                    )
                    authorized_mutations.append(
                        {
                            "asset_id": asset_id,
                            "principal": principal,
                            "operation": operation,
                        }
                    )
                else:
                    _fs_denied(
                        cases,
                        family="durable-state",
                        case_id=case_id,
                        user=principal,
                        expression=f"p=Path({str(target)!r})\n{expression}",
                    )
                replacement = target.with_name(target.name + ".replacement")
                try:
                    replacement.unlink(missing_ok=True)
                    if target.is_symlink():
                        target.unlink()
                    restore_lines = [f"p=Path({str(target)!r})"]
                    if asset.get("is_directory") is True:
                        # Directory assets model a producer-created child;
                        # their parent grants the owner create/unlink rights.
                        restore_lines.append("p.unlink(missing_ok=True)")
                    restore_lines.append("p.write_bytes(b'current-valid-content')")
                    restore = _run(
                        _fs_probe("\n".join(restore_lines)),
                        user=target_owner_name,
                        env=_account_env(target_owner_name),
                        timeout=30,
                    )
                    _require_success(
                        restore,
                        f"R9 owner restore for {asset_id}/{target_owner_name}",
                    )
                    os.chown(
                        target,
                        owner.pw_uid if not deny_only else 0,
                        owner.pw_gid if not deny_only else 0,
                    )
                    os.chmod(target, 0o600 if deny_only else int(raw_mode, 8) & 0o777)
                    for row in (() if deny_only else raw_acls):
                        if not isinstance(row, Mapping) or row.get("default") is True:
                            continue
                        account = row.get("account")
                        perms = row.get("perms")
                        if isinstance(account, str) and isinstance(perms, str):
                            _require_success(
                                _run(("setfacl", "-m", f"u:{account}:{perms}", str(target))),
                                f"R9 ACL proxy restore for {asset_id}/{account}",
                            )
                except OSError as exc:
                    diagnostics: list[str] = []
                    for argv in (
                        ("id",),
                        ("sh", "-c", "grep '^CapEff:' /proc/self/status"),
                        ("stat", "-c", "%F %u:%g %a", str(target)),
                        ("getfacl", "-p", str(target)),
                        ("findmnt", "-T", str(target), "-o", "TARGET,FSTYPE,OPTIONS"),
                    ):
                        probe = _run(argv)
                        text = (probe.stdout or probe.stderr).strip().replace("\n", " ")
                        if text:
                            diagnostics.append(text[:500])
                    raise QualificationFailure(
                        f"durable-state probe restore failed for {asset_id}/{operation}/"
                        f"{principal}: {target}: {exc} {' | '.join(diagnostics)}"
                    ) from exc
                if (
                    not target.is_file()
                    or target.is_symlink()
                    or target.read_bytes() != baseline
                ):
                    raise QualificationFailure(
                        f"durable-state probe changed authority proxy: {asset_id}"
                    )
        if "cortex-manager" in writer_accounts:
            manager_target = target
            manager_created = False
            if target_owner_name != "cortex-manager":
                # A spool producer owns its child inode; Manager's positive
                # control is therefore a separate Manager-created child.  It
                # proves the directory owner can create/write its own state
                # without pretending it may rewrite a producer's payload.
                manager_target = container / f"{target.name}.manager"
                manager_target.write_bytes(b"manager-valid-content")
                manager_owner = pwd.getpwnam("cortex-manager")
                os.chown(manager_target, manager_owner.pw_uid, manager_owner.pw_gid)
                os.chmod(manager_target, 0o600)
                manager_created = True
            _positive(
                controls,
                family="durable-state",
                case_id=f"durable-manager-write:{asset_id}",
                user="cortex-manager",
                argv=(
                    "/usr/bin/python3",
                    "-c",
                    f"from pathlib import Path; Path({str(manager_target)!r}).write_bytes(b'current-valid-content')",
                ),
            )
            if manager_created:
                manager_target.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
    if covered_assets == 0:
        raise QualificationFailure(
            "durable-state matrix covered no Tier-0/Tier-1 assets"
        )

    generated = plan.get("generated")
    if not isinstance(generated, Mapping):
        raise QualificationFailure("generated enforcement inventory is missing")
    enforcement_paths: set[Path] = set()
    for rows in generated.values():
        if isinstance(rows, Mapping):
            enforcement_paths.update(
                Path(str(row["path"]))
                for row in rows.values()
                if isinstance(row, Mapping) and isinstance(row.get("path"), str)
            )
    venv = Path("/opt/cortex/venv")
    enforcement_paths.update(
        path for path in venv.rglob("*.pth") if path.is_file() and not path.is_symlink()
    )
    if not enforcement_paths:
        raise QualificationFailure("enforcement-plane matrix has no concrete paths")
    for path in sorted(enforcement_paths):
        if not path.is_file() or path.is_symlink():
            raise QualificationFailure(f"enforcement authority path is absent: {path}")
        for principal in principals:
            _fs_denied(
                cases,
                family="enforcement-plane",
                case_id=f"inventory-write:{path}",
                user=principal,
                expression=f"Path({str(path)!r}).open('ab').write(b'x')",
            )

    site_packages = next(
        Path("/opt/cortex/venv/lib").glob("python*/site-packages"), None
    )
    if site_packages is None or not site_packages.is_dir():
        raise QualificationFailure("installed site-packages is absent")
    verifier = site_packages / "paulsha_cortex/coordinator/verification.py"
    manager_unit = Path("/etc/systemd/system/cortex-manager.service")
    manager_env = Path("/opt/cortex/etc/cortex-manager.env")
    shim = Path("/opt/cortex/bin/cortex-job-shim")
    t3_probes = {
        "T3.1-verifier-source": (verifier, "append"),
        "T3.2-sitecustomize": (site_packages / "sitecustomize.py", "create"),
        "T3.2-pth": (site_packages / "qualification-evil.pth", "create"),
        "T3.3-manager-unit": (manager_unit, "append"),
        "T3.4-manager-env-redirect": (manager_env, "append"),
        "T3.5-delete-environment-file": (manager_env, "delete"),
        "T3.6-job-shim": (shim, "append"),
        "T3.7-path-python-redirect": (manager_env, "append"),
        "T3.8-named-executor-redirect": (manager_env, "append"),
        "T3.9-builder-codex-hooks": (
            Path("/var/lib/cortex-builder/.codex/hooks.json"),
            "append",
        ),
        "T3.9-reviewer-codex-hooks": (
            Path("/var/lib/cortex-reviewer-planner/.codex/hooks.json"),
            "append",
        ),
        "T3.10-verifier-downgrade": (verifier, "replace"),
    }
    for case_id, (path, operation) in t3_probes.items():
        if operation != "create" and (not path.is_file() or path.is_symlink()):
            raise QualificationFailure(
                f"enforcement probe prerequisite is absent: {path}"
            )
        if operation == "append":
            expression = f"Path({str(path)!r}).open('ab').write(b'x')"
        elif operation == "delete":
            expression = f"Path({str(path)!r}).unlink()"
        elif operation == "replace":
            expression = (
                f"p=Path({str(path)!r})\nq=p.with_name(p.name+'.older')\n"
                "q.write_bytes(b'older')\nq.replace(p)"
            )
        else:
            expression = f"Path({str(path)!r}).write_bytes(b'x')"
        for principal in principals:
            _fs_denied(
                cases,
                family="enforcement-plane",
                case_id=case_id,
                user=principal,
                expression=expression,
            )
        (path.with_name(path.name + ".older")).unlink(missing_ok=True)
        # The matrix deliberately restarts the same unit after every probe.
        # Reset systemd's start-rate counter first, otherwise the harness
        # itself trips StartLimitBurst before the later denial cases run.
        _require_success(
            _run(("systemctl", "reset-failed", "cortex-manager.service")),
            f"reset Manager start counter after {case_id}",
        )
        _require_success(
            _run(("systemctl", "daemon-reload")), f"daemon-reload after {case_id}"
        )
        _require_success(
            _run(("systemctl", "restart", "cortex-manager.service"), timeout=60),
            f"restart after enforcement probe {case_id}",
        )
        _require_success(
            _run(("systemctl", "is-active", "cortex-manager.service")),
            f"Manager active after enforcement probe {case_id}",
        )

    unit_before = manager_unit.read_bytes()
    with manager_unit.open("ab") as stream:
        stream.write(b"\n# qualification negative control\n")
    _require_success(
        _run(("systemctl", "daemon-reload")), "enforcement negative-control reload"
    )
    _require_success(
        _run(("systemctl", "restart", "cortex-manager.service"), timeout=60),
        "enforcement negative-control restart",
    )
    manager_unit.write_bytes(unit_before)
    _require_success(
        _run(("systemctl", "daemon-reload")),
        "enforcement negative-control restore reload",
    )
    _require_success(
        _run(("systemctl", "restart", "cortex-manager.service"), timeout=60),
        "enforcement negative-control restore restart",
    )
    controls.append(
        {
            "family": "enforcement-plane",
            "case": "enforcement-root-modify-restart-restore",
            "principal": "root",
            "status": "passed",
            "returncode": 0,
        }
    )

    manager_pid_result = _run(
        ("systemctl", "show", "cortex-manager.service", "-p", "MainPID", "--value")
    )
    _require_success(manager_pid_result, "Manager MainPID")
    manager_pid = int(manager_pid_result.stdout.strip())
    if manager_pid <= 1:
        raise QualificationFailure("Manager MainPID is not live")
    for principal in principals:
        fd_script = (
            "import fcntl,os\n"
            "protected=('/opt/cortex','/var/lib/cortex','/var/lib/cortex-manager')\n"
            "bad=[]\n"
            "for name in os.listdir('/proc/self/fd'):\n"
            " try:\n"
            "  fd=int(name); target=os.readlink('/proc/self/fd/'+name); flags=fcntl.fcntl(fd,fcntl.F_GETFL)\n"
            " except OSError: continue\n"
            " if target.startswith(protected) and (flags & os.O_ACCMODE) != os.O_RDONLY: bad.append(target)\n"
            "raise SystemExit(1 if bad else 0)\n"
        )
        _passed_case(
            cases,
            family="process",
            case_id="T4.1-no-protected-writable-fd",
            user=principal,
            argv=("/usr/bin/python3", "-c", fd_script),
        )
        _denied(
            cases,
            family="process",
            case_id="T4.2-ptrace-manager",
            user=principal,
            argv=(
                "/usr/bin/python3",
                "-c",
                f"import ctypes,os; r=ctypes.CDLL(None,use_errno=True).ptrace(16,{manager_pid},0,0); raise SystemExit(0 if r==0 else ctypes.get_errno())",
            ),
        )
        for leaf in ("mem", "environ"):
            _denied(
                cases,
                family="process",
                case_id=f"T4.3-read-manager-{leaf}",
                user=principal,
                argv=(
                    "/usr/bin/python3",
                    "-c",
                    f"open('/proc/{manager_pid}/{leaf}','rb').read(1)",
                ),
            )
        stopped = _run(
            ("/bin/kill", "-STOP", str(manager_pid)),
            user=principal,
            env=_account_env(principal),
        )
        if stopped.returncode == 0:
            os.kill(manager_pid, signal.SIGCONT)
            raise QualificationFailure(f"process/T4.4 signal succeeded as {principal}")
        cases.append(
            {
                "family": "process",
                "case": "T4.4-signal-manager",
                "principal": principal,
                "status": "passed",
                "returncode": stopped.returncode,
            }
        )
    _positive(
        controls,
        family="process",
        case_id="process-root-read-environ",
        user="root",
        argv=(
            "/usr/bin/python3",
            "-c",
            f"open('/proc/{manager_pid}/environ','rb').read(1)",
        ),
    )

    gate_fs_probes = {
        "T5.1-write-gate-ledger": "Path('/var/lib/cortex/runtime/dispatch/.r9').write_bytes(b'x')",
        "T5.2-write-manager-state": "Path('/var/lib/cortex/coordinator/.r9').write_bytes(b'x')",
        "T5.4-write-verdict-spool": "Path('/var/lib/cortex/coordinator/review-verdicts/.r9').write_bytes(b'x')",
        "T5.4-write-commit-spool": "Path('/var/lib/cortex/coordinator/commit-spool/.r9').write_bytes(b'x')",
        "T5.8-builder-preseed": "Path('/var/lib/cortex/coordinator/gate-ledger-spool/preseed').mkdir()",
        "T5.10-write-authoritative-ledger": "Path('/var/lib/cortex/runtime/dispatch/qualification.gates.json').write_bytes(b'{}')",
    }
    for case_id, expression in gate_fs_probes.items():
        principal = "cortex-builder" if case_id.startswith("T5.8") else "cortex-gate"
        _fs_denied(
            cases,
            family="gate",
            case_id=case_id,
            user=principal,
            expression=expression,
        )

    worktree_probe = Path("/var/lib/cortex/worktree/qualification-gate-probe")
    worktree_probe.mkdir(mode=0o700, exist_ok=False)
    builder = pwd.getpwnam("cortex-builder")
    os.chown(worktree_probe, builder.pw_uid, builder.pw_gid)
    _require_success(
        _run(("setfacl", "-m", "u:cortex-gate:r-X", str(worktree_probe))),
        "gate worktree read ACL",
    )
    _fs_denied(
        cases,
        family="gate",
        case_id="T5.3-write-builder-worktree",
        user="cortex-gate",
        expression=f"Path({str(worktree_probe / '.r9')!r}).write_bytes(b'x')",
    )
    _positive(
        controls,
        family="gate",
        case_id="gate-read-worktree",
        user="cortex-gate",
        argv=(
            "/usr/bin/python3",
            "-c",
            f"from pathlib import Path; list(Path({str(worktree_probe)!r}).iterdir())",
        ),
    )
    worktree_probe.rmdir()

    other_gate_slot = Path(
        "/var/lib/cortex/coordinator/gate-ledger-spool/qualification-other"
    )
    other_gate_slot.mkdir(mode=0o700, exist_ok=False)
    manager = pwd.getpwnam("cortex-manager")
    os.chown(other_gate_slot, manager.pw_uid, manager.pw_gid)
    _require_success(
        _run(("setfacl", "-m", "u:cortex-gate:wx", str(other_gate_slot))),
        "other gate slot ACL",
    )
    _fs_denied(
        cases,
        family="gate",
        case_id="T5.5-read-other-gate-slot",
        user="cortex-gate",
        expression=f"list(Path({str(other_gate_slot)!r}).iterdir())",
    )
    _denied(
        cases,
        family="gate",
        case_id="T5.6-start-builder-unit",
        user="cortex-gate",
        argv=("/usr/bin/systemctl", "start", "cortex-job@r9.service"),
    )
    legal_instance = "qualification-negctl"
    legal_workspace = Path(f"/var/lib/cortex/worktree/{legal_instance}")
    legal_log = legal_workspace / "identity.log"
    legal_workspace.mkdir(mode=0o700, exist_ok=False)
    os.chown(legal_workspace, builder.pw_uid, builder.pw_gid)
    spec_code = (
        "from paulsha_cortex.coordinator import job_runner\n"
        f"instance={legal_instance!r}\n"
        f"workspace={str(legal_workspace)!r}\n"
        "spec=job_runner.build_job_spec("
        "job_id=instance, instance=instance, unit=f'cortex-job@{instance}.service',"
        "command=['/usr/bin/id'], working_directory=workspace,"
        f"log_path={str(legal_log)!r}, env={{'HOME':'/var/lib/cortex-builder','PATH':'/usr/bin:/bin'}})\n"
        "job_runner.write_job_spec(job_runner.job_spec_path(job_runner.DEFAULT_JOB_SPEC_SPOOL, instance), spec, account='cortex-builder')\n"
    )
    manager_runtime_env = _installed_runtime_env()
    manager_runtime_env["HOME"] = "/var/lib/cortex-manager"
    _require_success(
        _run(
            ("/opt/cortex/venv/bin/python", "-c", spec_code),
            user="cortex-manager",
            env=manager_runtime_env,
        ),
        "gate Manager legal-job negative-control spec",
    )
    legal_start = _run(
        (
            "/usr/bin/systemctl",
            "start",
            "--wait",
            f"cortex-job@{legal_instance}.service",
        ),
        user="cortex-manager",
        env=manager_runtime_env,
        timeout=60,
    )
    _require_success(legal_start, "gate Manager legal-job negative control")
    if not legal_log.is_file() or "cortex-builder" not in legal_log.read_text(
        encoding="utf-8"
    ):
        raise QualificationFailure(
            "gate Manager legal-job control did not execute as cortex-builder"
        )
    controls.append(
        {
            "family": "gate",
            "case": "gate-manager-start-legal-builder-job",
            "principal": "cortex-manager",
            "status": "passed",
            "returncode": 0,
        }
    )
    Path(f"/var/lib/cortex/coordinator/job-specs/builder/{legal_instance}.json").unlink(
        missing_ok=True
    )
    legal_log.unlink(missing_ok=True)
    legal_workspace.rmdir()
    for case_id, root in (
        ("T5.7-no-gate-files-in-source-worktree", Path("/var/lib/cortex/worktree")),
        (
            "T5.9-no-gate-files-in-manager-authority",
            Path("/var/lib/cortex/coordinator"),
        ),
    ):
        gate_uid = pwd.getpwnam("cortex-gate").pw_uid
        if any(
            path.lstat().st_uid == gate_uid
            for path in root.rglob("*")
            if not path.is_symlink()
        ):
            raise QualificationFailure(
                f"gate/{case_id} found gate-owned authority content"
            )
        cases.append(
            {
                "family": "gate",
                "case": case_id,
                "principal": "root",
                "status": "passed",
                "returncode": 0,
            }
        )

    manager_dispatch = Path("/var/lib/cortex/runtime/dispatch/.qualification-negctl")
    _positive(
        controls,
        family="gate",
        case_id="gate-manager-write-dispatch",
        user="cortex-manager",
        argv=(
            "/usr/bin/python3",
            "-c",
            f"from pathlib import Path; Path({str(manager_dispatch)!r}).write_bytes(b'x')",
        ),
    )
    manager_dispatch.unlink(missing_ok=True)
    gate_slot = Path(
        "/var/lib/cortex/coordinator/gate-ledger-spool/qualification-negctl"
    )
    gate_slot.mkdir(mode=0o700, parents=False, exist_ok=False)
    manager = pwd.getpwnam("cortex-manager")
    os.chown(gate_slot, manager.pw_uid, manager.pw_gid)
    acl = _run(("setfacl", "-m", "u:cortex-gate:wx", str(gate_slot)))
    _require_success(acl, "gate negative-control ACL")
    gate_file = gate_slot / "ledger.json"
    _positive(
        controls,
        family="gate",
        case_id="gate-own-slot-write",
        user="cortex-gate",
        argv=(
            "/usr/bin/python3",
            "-c",
            f"from pathlib import Path; Path({str(gate_file)!r}).write_bytes(b'{{}}')",
        ),
    )
    gate_file.unlink(missing_ok=True)
    gate_slot.rmdir()
    other_gate_slot.rmdir()

    families = {str(row["family"]) for row in cases if row.get("status") == "passed"}
    if families != {
        "capability",
        "durable-state",
        "enforcement-plane",
        "process",
        "gate",
    }:
        raise QualificationFailure("attack matrix did not cover all five families")
    control_families = {
        str(row.get("family")) for row in controls if row.get("status") == "passed"
    }
    if control_families != families or any(
        row.get("status") != "passed" for row in controls
    ):
        raise QualificationFailure("attack matrix negative controls are incomplete")
    _write_json(
        evidence_dir / "attack-matrix.json",
        {
            "schema_version": 1,
            "status": "passed",
            "families": sorted(families),
            "cases": cases,
            "negative_controls": controls,
            "authorized_mutations": authorized_mutations,
            "deny_only_assets": sorted(deny_only_assets),
            "covered_assets": covered_assets,
            "registry_asset_ids": sorted(registry_asset_ids),
        },
    )


def _walk_values(value: object, keys: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in keys and isinstance(child, str):
                found.add(child)
            found.update(_walk_values(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.update(_walk_values(child, keys))
    return found


def _walk_scalars(value: object, keys: set[str]) -> list[object]:
    found: list[object] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in keys and isinstance(child, (str, bool, int, float)):
                found.append(child)
            found.extend(_walk_scalars(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_scalars(child, keys))
    return found


def _json_records(output: str) -> list[object]:
    records: list[object] = []
    for line in output.splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not records:
        try:
            records.append(json.loads(output))
        except json.JSONDecodeError:
            pass
    return records


def _provider_preflight(provider: str, account: str) -> dict[str, object]:
    adapter = PROVIDER_PREFLIGHTS[provider]
    version = _run(
        adapter.version_command,
        user=account,
        env=_account_env(account),
        timeout=15,
    )
    if version.returncode != 0:
        raise QualificationFailure(
            f"provider {provider} pinned binary version probe failed"
        )
    if adapter.version is not None and adapter.version not in (
        version.stdout + version.stderr
    ):
        raise QualificationFailure(
            f"provider {provider} pinned binary version did not match "
            f"{adapter.version}"
        )
    if adapter.status_command is None:
        version_label = adapter.version or "staged version"
        raise QualificationFailure(
            f"provider {provider} {version_label} exposes no structured live "
            "login/quota status; qualification fails closed"
        )

    status = _run(
        adapter.status_command,
        user=account,
        env=_account_env(account),
        timeout=45,
    )
    records = _json_records(status.stdout)
    payload = records[0] if len(records) == 1 else None
    if status.returncode != 0 or not isinstance(payload, Mapping):
        raise QualificationFailure(
            f"provider {provider} structured status probe did not return one "
            "successful JSON object"
        )

    # Codex 0.149.0 doctor JSON contains local runtime, configuration, network,
    # and filesystem checks, but no structured live login or quota result.  Do
    # not infer either from overallStatus or synthesize an "available" value.
    raise QualificationFailure(
        f"provider {provider} {adapter.version} structured status lacks live "
        "login/quota fields; qualification fails closed"
    )


def _has_exact_final_assistant_response(records: Sequence[object]) -> bool:
    final_contents: list[str] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if (
            record.get("role") == "assistant"
            and record.get("type") in {"final", "result"}
            and isinstance(record.get("content"), str)
        ):
            final_contents.append(str(record["content"]))
            continue
        if record.get("type") == "assistant.message":
            data = record.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("content"), str):
                final_contents.append(str(data["content"]))
            continue
        if record.get("type") != "item.completed":
            continue
        item = record.get("item")
        if (
            isinstance(item, Mapping)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            final_contents.append(str(item["text"]))
    return final_contents == ["QUALIFICATION_OK"]


def _provider_smokes(evidence_dir: Path) -> list[dict[str, object]]:
    prompt = "Return exactly QUALIFICATION_OK and do not use tools."
    commands = {
        "agy": (
            "/opt/cortex/toolchain/bin/agy",
            "--print",
            prompt,
            "--mode",
            "plan",
            "--sandbox",
            "--model",
            "gemini-3.7-flash",
            "--effort",
            "high",
            "--output-format",
            "json",
            "--disable-slash-commands",
        ),
        "copilot": (
            "/opt/cortex/toolchain/bin/copilot",
            "--prompt",
            prompt,
            "--model",
            "gpt-5.4",
            "--effort",
            "xhigh",
            "--output-format",
            "json",
            "--available-tools=__none__",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--no-remote",
            "--no-remote-export",
            "--no-auto-update",
        ),
        "codex": (
            "/opt/cortex/toolchain/bin/codex",
            "exec",
            "--ignore-user-config",
            "--model",
            "gpt-5",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            prompt,
        ),
    }
    verdicts: list[dict[str, object]] = []
    raw_evidence: dict[str, object] = {"schema_version": 1, "providers": {}}
    for provider, command in commands.items():
        model, effort, account = PROVIDERS[provider]
        preflight = _provider_preflight(provider, account)
        result = _run(command, user=account, env=_account_env(account), timeout=300)
        records = _json_records(result.stdout)
        models = (
            set().union(
                *(
                    _walk_values(
                        row,
                        {
                            "model",
                            "modelid",
                            "modelname",
                            "runtimemodel",
                            "selectedmodel",
                        },
                    )
                    for row in records
                )
            )
            if records
            else set()
        )
        efforts = (
            set().union(
                *(
                    _walk_values(
                        row,
                        {
                            "effort",
                            "reasoningeffort",
                            "runtimeeffort",
                            "selectedeffort",
                        },
                    )
                    for row in records
                )
            )
            if records
            else set()
        )
        response_token = _has_exact_final_assistant_response(records)
        fallback_values = _walk_scalars(
            records, {"fallback", "fallbackmodel", "fallbackused"}
        )
        fallback_observed = any(
            value not in {False, 0, "false", "none", "not-used"}
            for value in fallback_values
        )
        passed = (
            result.returncode == 0
            and models == {model}
            and efforts == {effort}
            and response_token
            and not fallback_observed
        )
        raw_evidence["providers"][provider] = {
            "preflight": preflight,
            "returncode": result.returncode,
            "models": sorted(models),
            "efforts": sorted(efforts),
            "native_metadata": passed,
            "response_token": response_token,
        }
        if not passed:
            raise QualificationFailure(
                f"provider {provider} lacked unique exact native model/effort metadata "
                f"(rc={result.returncode}, models={sorted(models)}, efforts={sorted(efforts)}, "
                f"response_token={response_token})"
            )
        verdicts.append(
            {
                "provider": provider,
                "requested_model": model,
                "runtime_model": next(iter(models)),
                "requested_effort": effort,
                "runtime_effort": next(iter(efforts)),
                "status": "passed",
                "quota": preflight["quota"],
                "fallback": preflight["fallback"],
            }
        )
    _write_json(evidence_dir / "provider-capabilities.json", raw_evidence)
    return verdicts


def _require_installed_manager_gitconfig(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise QualificationFailure("installed Manager gitconfig is absent or a symlink")
    metadata = path.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise QualificationFailure("installed Manager gitconfig is not root-controlled")


def _parse_remote_refs(output: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for raw in output.splitlines():
        sha, separator, ref = raw.partition("\t")
        if not separator or SHA40.fullmatch(sha) is None or not ref.startswith("refs/"):
            raise QualificationFailure("Manager probe returned malformed remote refs")
        refs.append((ref, sha))
    if not refs or len({ref for ref, _sha in refs}) != len(refs):
        raise QualificationFailure(
            "Manager probe returned empty or duplicate remote refs"
        )
    return sorted(refs)


_CREDENTIAL_FILL_PROBE = r"""
import subprocess
import sys

result = subprocess.run(
    ["/usr/bin/git", "credential", "fill"],
    input="protocol=https\nhost=github.com\n\n",
    text=True,
    capture_output=True,
    check=False,
)
if result.returncode != 0:
    raise SystemExit(2)
fields = {}
for line in result.stdout.splitlines():
    key, separator, value = line.partition("=")
    if separator:
        fields[key] = value
if not fields.get("username") or not fields.get("password"):
    raise SystemExit(3)
sys.stdout.write("credential-ok\n")
""".strip()


def _manager_github_probe(
    repository: str,
    candidate_sha: str,
    evidence_dir: Path,
    *,
    source_repo: Path = Path("/var/lib/cortex/repos/paulsha-cortex"),
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise QualificationFailure(
            "protected GitHub probe repository is missing or invalid"
        )
    account = "cortex-manager"
    env = _account_env(account)
    gitconfig = Path(env["HOME"]) / ".gitconfig"
    _require_installed_manager_gitconfig(gitconfig)
    helper = _run(
        (
            "/usr/bin/git",
            "-C",
            str(source_repo),
            "config",
            "--show-origin",
            "--show-scope",
            "--get-all",
            "credential.helper",
        ),
        user=account,
        env=env,
        timeout=30,
    )
    _require_success(helper, "Manager installed credential helper inventory")
    helper_rows = [line.split("\t", 2) for line in helper.stdout.splitlines()]
    expected_origin = f"file:{gitconfig}"
    if helper_rows != [["global", expected_origin, "!/usr/bin/gh auth git-credential"]]:
        raise QualificationFailure(
            "Manager effective credential helper is not the unique installed helper"
        )
    auth = _run(("/usr/bin/gh", "auth", "status"), user=account, env=env, timeout=45)
    _require_success(auth, "Manager gh auth status")
    credential = _run(
        ("/usr/bin/python3", "-c", _CREDENTIAL_FILL_PROBE),
        user=account,
        env=env,
        timeout=45,
    )
    if (
        credential.returncode != 0
        or credential.stdout != "credential-ok\n"
        or credential.stderr
    ):
        raise QualificationFailure("Manager secret-safe credential probe failed")
    remote = f"https://github.com/{repository}.git"
    before = _run(
        ("/usr/bin/git", "ls-remote", remote), user=account, env=env, timeout=60
    )
    _require_success(before, "Manager probe repo ls-remote before")
    before_refs = _parse_remote_refs(before.stdout)
    dry_run = _run(
        (
            "/usr/bin/git",
            "-C",
            str(source_repo),
            "push",
            "--dry-run",
            remote,
            f"{candidate_sha}:refs/heads/cortex-rc-auth-probe",
        ),
        user=account,
        env=env,
        timeout=90,
    )
    _require_success(dry_run, "Manager authenticated dry-run push")
    after = _run(
        ("/usr/bin/git", "ls-remote", remote), user=account, env=env, timeout=60
    )
    _require_success(after, "Manager probe repo ls-remote after")
    after_refs = _parse_remote_refs(after.stdout)
    if before_refs != after_refs:
        raise QualificationFailure("Manager dry-run push changed remote refs")
    before_bytes = _canonical_bytes(before_refs)
    after_bytes = _canonical_bytes(after_refs)
    _write_json(
        evidence_dir / "manager-github-auth.json",
        {
            "schema_version": 1,
            "status": "passed",
            "repository": repository,
            "authenticated": True,
            "dry_run": True,
            "remote_refs_unchanged": True,
            "before_sha256": hashlib.sha256(before_bytes).hexdigest(),
            "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
        },
    )


def _manager_uid() -> int:
    try:
        return pwd.getpwnam("cortex-manager").pw_uid
    except KeyError as exc:
        raise QualificationFailure("Manager service account is absent") from exc


def _manager_file(path: Path, *, label: str, root: Path | None = None) -> bytes:
    if root is not None:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise QualificationFailure(f"{label} escapes Manager state root") from exc
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise QualificationFailure(f"{label} contains a symlink")
    if path.is_symlink() or not path.is_file():
        raise QualificationFailure(f"{label} is absent or not a regular file")
    metadata = path.stat()
    if metadata.st_uid != _manager_uid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise QualificationFailure(f"{label} is not Manager-controlled")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise QualificationFailure(f"{label} is unreadable") from exc


def _json_object(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationFailure(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise QualificationFailure(f"{label} must be a JSON object")
    return payload


def _bound_relative_json(
    root: Path, locator: object, *, label: str
) -> tuple[dict[str, Any], Path, str]:
    if not isinstance(locator, dict) or set(locator) != {"kind", "path", "hash"}:
        raise QualificationFailure(f"{label} locator is malformed")
    relative = Path(str(locator["path"]))
    digest = locator.get("hash")
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[:2] != ("evidence", "workflow")
        or not isinstance(digest, str)
        or SHA256.fullmatch(digest) is None
    ):
        raise QualificationFailure(f"{label} locator is unsafe")
    path = root / relative
    content = _manager_file(path, label=label, root=root)
    actual = hashlib.sha256(content).hexdigest()
    if actual != digest:
        raise QualificationFailure(f"{label} hash mismatch")
    return _json_object(content, label=label), path, actual


def _bound_gate_evidence_path(root: Path, reference: object) -> Path:
    """Canonicalize a gate ref within the installed coordinator evidence root."""

    if not isinstance(reference, str) or not reference or "\x00" in reference:
        raise QualificationFailure("workflow delivery gate locator is unsafe")
    locator = Path(reference)
    if ".." in locator.parts or locator.as_posix() != reference:
        raise QualificationFailure("workflow delivery gate locator is unsafe")
    evidence_root = root / "evidence"
    path = locator if locator.is_absolute() else root / locator
    try:
        relative = path.relative_to(evidence_root)
    except ValueError as exc:
        raise QualificationFailure(
            "workflow delivery gate locator is unsafe"
        ) from exc
    if not relative.parts:
        raise QualificationFailure("workflow delivery gate locator is unsafe")
    return evidence_root.joinpath(*relative.parts)


def _artifact_row(path: Path, *, state_root: Path) -> dict[str, str]:
    try:
        relative = path.relative_to(state_root).as_posix()
    except ValueError as exc:
        raise QualificationFailure(
            "dispatch artifact escapes Cortex state root"
        ) from exc
    return {"path": relative, "sha256": _sha256(path)}


def _terminal_named_values(value: object, name: str) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == name and isinstance(item, str):
                found.add(item)
            found.update(_terminal_named_values(item, name))
    elif isinstance(value, list):
        for item in value:
            found.update(_terminal_named_values(item, name))
    return found


def _validate_dispatch_closeout(
    *,
    repository: str,
    work_id: str,
    issue: int,
    terminal: object,
    coordinator_root: Path,
) -> tuple[list[str], list[dict[str, str]], dict[str, Any]]:
    root = coordinator_root.resolve()
    state_root = root.parent
    registry_path = root / "jobs.json"
    registry_content = _manager_file(
        registry_path, label="coordinator registry", root=root
    )
    registry = _json_object(registry_content, label="coordinator registry")
    if registry.get("schema_version") != 2:
        raise QualificationFailure("coordinator registry schema is not v2")
    jobs = registry.get("jobs")
    workflows = registry.get("workflows")
    if not isinstance(jobs, list) or not isinstance(workflows, list):
        raise QualificationFailure("coordinator registry collections are malformed")
    matches = [
        row
        for row in workflows
        if isinstance(row, dict)
        and row.get("work_id") == work_id
        and row.get("repo") == repository
    ]
    if len(matches) != 1:
        raise QualificationFailure("coordinator registry has no unique bound workflow")
    workflow = matches[0]
    run_id = workflow.get("run_id")
    candidate = workflow.get("candidate_head")
    required_phases = ("claim", "define", "plan", "build", "verify", "review", "ship")
    steps = workflow.get("steps")
    phase_chain = (
        [row.get("phase") for row in steps]
        if isinstance(steps, list) and all(isinstance(row, dict) for row in steps)
        else []
    )
    indexes = [
        required_phases.index(phase)
        for phase in phase_chain
        if phase in required_phases
    ]
    if (
        not isinstance(run_id, str)
        or not run_id
        or SHA40.fullmatch(str(candidate)) is None
        or workflow.get("verified_head") != candidate
        or workflow.get("current_phase") != "ship"
        or workflow.get("status") != "done"
        or workflow.get("gate_status") != "passed"
        or workflow.get("facets") not in ([], ())
        or not isinstance(steps, list)
        or not all(phase in phase_chain for phase in required_phases)
        or indexes != sorted(indexes)
        or any(
            row.get("gate_result") != "passed" for row in steps if isinstance(row, dict)
        )
        or not isinstance(workflow.get("issue_refs"), list)
        or f"{repository}#{issue}" not in workflow["issue_refs"]
    ):
        raise QualificationFailure(
            "workflow terminal phase chain or candidate binding is invalid"
        )
    if _terminal_named_values(terminal, "run_id") not in (
        {run_id},
        set(),
    ) or _terminal_named_values(terminal, "work_id") != {work_id}:
        raise QualificationFailure(
            "CLI terminal is not bound to the completed workflow"
        )

    repo_root = Path(str(workflow.get("workspace_root", "")))
    candidate_check = _run(
        (
            "/usr/bin/git",
            "-C",
            str(repo_root),
            "cat-file",
            "-e",
            f"{candidate}^{{commit}}",
        ),
        user="cortex-manager",
        env=_account_env("cortex-manager"),
        timeout=30,
    )
    _require_success(candidate_check, "completed workflow candidate object")

    bound_jobs = [
        row
        for row in jobs
        if isinstance(row, dict) and row.get("workflow_run_id") == run_id
    ]
    job_phases = {str(row.get("workflow_phase")) for row in bound_jobs}
    if not {"plan", "build", "verify", "review", "ship"} <= job_phases:
        raise QualificationFailure("workflow job phase chain is incomplete")

    artifact_paths: list[Path] = [registry_path]
    verdict_seen = False
    ledgers_seen = 0
    for job in bound_jobs:
        phase = job.get("workflow_phase")
        if (
            job.get("workflow_repo") != repository
            or job.get("status") != "exited"
            or job.get("exit_code") != 0
            or phase not in {"plan", "build", "verify", "review", "ship"}
        ):
            raise QualificationFailure("workflow job authority binding is invalid")
        envelope, evidence_path, _digest = _bound_relative_json(
            root, job.get("workflow_evidence"), label="workflow canonical evidence"
        )
        binding = envelope.get("job")
        if (
            envelope.get("schema_version") != 1
            or envelope.get("kind") != phase
            or not isinstance(binding, dict)
            or binding.get("job_id") != job.get("job_id")
            or binding.get("run_id") != run_id
            or binding.get("claim_key") != job.get("workflow_claim_key")
            or binding.get("repo") != repository
            or binding.get("source_revision") != job.get("source_revision")
            or binding.get("card_id") != job.get("workflow_card")
            or binding.get("phase") != phase
        ):
            raise QualificationFailure("workflow canonical evidence authority mismatch")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise QualificationFailure(
                "workflow canonical evidence payload is malformed"
            )
        payload_candidate = payload.get("candidate")
        expected_evidence_candidate = (
            job.get("subject_head") if phase in {"build", "ship"} else candidate
        )
        if phase in {"build", "verify", "review", "ship"} and (
            SHA40.fullmatch(str(expected_evidence_candidate)) is None
            or payload_candidate != expected_evidence_candidate
        ):
            raise QualificationFailure("workflow evidence candidate mismatch")
        if phase == "review":
            if (
                payload.get("state") != "passed"
                or payload.get("reviewer_job_id") != job.get("job_id")
                or not isinstance(payload.get("builder_job_id"), str)
            ):
                raise QualificationFailure("workflow review verdict authority mismatch")
            verdict_seen = True
        rows = envelope.get("artifacts")
        if not isinstance(rows, list):
            raise QualificationFailure(
                "workflow canonical artifact inventory is malformed"
            )
        job_repo_root = Path(str(job.get("workflow_repo_root", ""))).resolve()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "path",
                "sha256",
                "baseline_sha256",
            }:
                raise QualificationFailure(
                    "workflow canonical artifact locator is malformed"
                )
            relative = Path(str(row["path"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise QualificationFailure("workflow canonical artifact path is unsafe")
            output = job_repo_root / relative
            if (
                output.is_symlink()
                or not output.is_file()
                or _sha256(output) != row.get("sha256")
            ):
                raise QualificationFailure(
                    "workflow canonical artifact is absent or drifted"
                )
            artifact_paths.append(output)
        artifact_paths.append(evidence_path)
        if phase != "ship":
            control_value = job.get("control_log_path") or job.get("log_path")
            if not isinstance(control_value, str) or not control_value:
                raise QualificationFailure(
                    "workflow Manager control log binding is absent"
                )
            control = Path(control_value)
            ledger_path = control.with_name(f"{control.stem}.gates.json")
            ledger = _json_object(
                _manager_file(ledger_path, label="workflow gate ledger", root=root),
                label="workflow gate ledger",
            )
            if (
                ledger.get("schema_version") != 1
                or ledger.get("kind") != "workflow-gate-ledger"
                or not isinstance(ledger.get("slice_id"), str)
                or not isinstance(ledger.get("gates"), list)
            ):
                raise QualificationFailure("workflow gate ledger schema is invalid")
            ledgers_seen += 1
            artifact_paths.append(ledger_path)
    if not verdict_seen or ledgers_seen == 0:
        raise QualificationFailure("workflow verdict or Manager gate ledger is absent")

    bundle_seen = False
    build_jobs = [job for job in bound_jobs if job.get("workflow_phase") == "build"]
    for job in build_jobs:
        slot = job.get("template_instance") or job.get("job_id")
        if (
            not isinstance(slot, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", slot) is None
        ):
            raise QualificationFailure("build commit spool authority is invalid")
        bundle = root / "commit-spool" / slot / "commits.bundle"
        if not bundle.exists():
            continue
        if bundle.is_symlink() or bundle.parent.is_symlink() or not bundle.is_file():
            raise QualificationFailure(
                "build commit bundle is not a regular sealed artifact"
            )
        parent_metadata = bundle.parent.stat()
        if (
            parent_metadata.st_uid != _manager_uid()
            or stat.S_IMODE(parent_metadata.st_mode) & 0o222
        ):
            raise QualificationFailure("build commit bundle slot is not Manager-sealed")
        verify = _run(
            ("/usr/bin/git", "-C", str(repo_root), "bundle", "verify", str(bundle))
        )
        _require_success(verify, "build commit bundle verification")
        heads = _run(("/usr/bin/git", "bundle", "list-heads", str(bundle)))
        _require_success(heads, "build commit bundle heads")
        if not any(
            line.split(maxsplit=1)[0] == job.get("subject_head")
            for line in heads.stdout.splitlines()
        ):
            raise QualificationFailure(
                "build commit bundle does not carry its job candidate"
            )
        bundle_seen = True
        artifact_paths.append(bundle)
    if not bundle_seen:
        raise QualificationFailure("workflow has no verified commit bundle artifact")

    completion_value = workflow.get("completion_record_path")
    if not isinstance(completion_value, str):
        raise QualificationFailure("workflow completion record binding is absent")
    completion_path = Path(completion_value)
    completion_content = _manager_file(
        completion_path, label="workflow completion", root=root
    )
    completion = _json_object(completion_content, label="workflow completion")
    completion_hash = _canonical_json_hash(completion)
    authority = completion.get("work_authority")
    if (
        completion_hash != workflow.get("completion_record_hash")
        or completion.get("candidate") != candidate
        or workflow.get("completion_record_revision") != candidate
        or workflow.get("pr_candidate") != candidate
        or not isinstance(authority, dict)
        or authority.get("repo") != repository
        or authority.get("work_id") != work_id
        or authority.get("run_id") != run_id
        or issue not in authority.get("mapped_issues", [])
        or authority.get("merge_commit") != workflow.get("merge_revision")
    ):
        raise QualificationFailure(
            "workflow completion authority/hash binding is invalid"
        )
    artifact_paths.append(completion_path)

    worktrees = _run(
        ("/usr/bin/git", "-C", str(repo_root), "worktree", "list", "--porcelain")
    )
    _require_success(worktrees, "source repository worktree inventory")
    registered = {
        line.removeprefix("worktree ")
        for line in worktrees.stdout.splitlines()
        if line.startswith("worktree ")
    }
    for job in build_jobs:
        path_value = job.get("worktree")
        if not isinstance(path_value, str) or not path_value:
            raise QualificationFailure("build worktree binding is absent")
        worktree = Path(path_value)
        if worktree.exists() or worktree.is_symlink() or str(worktree) in registered:
            raise QualificationFailure("build worktree reclaim is incomplete")

    gate_refs = workflow.get("gate_refs")
    if not isinstance(gate_refs, list) or not gate_refs:
        raise QualificationFailure("workflow delivery gate refs are absent")
    gate_kinds: set[object] = set()
    gate_paths: set[Path] = set()
    gate_inodes: set[tuple[int, int]] = set()
    evidence_root = root / "evidence"
    if evidence_root.is_symlink() or not evidence_root.is_dir():
        raise QualificationFailure("workflow delivery gate evidence root is unsafe")
    for row in gate_refs:
        if not isinstance(row, dict) or set(row) != {"kind", "ref", "sha256"}:
            raise QualificationFailure("workflow delivery gate locator is malformed")
        expected_hash = row.get("sha256")
        if (
            not isinstance(expected_hash, str)
            or SHA256.fullmatch(expected_hash) is None
        ):
            raise QualificationFailure("workflow delivery gate locator is unsafe")
        path = _bound_gate_evidence_path(root, row["ref"])
        content = _manager_file(
            path, label="workflow delivery gate", root=evidence_root
        )
        metadata = path.stat()
        inode = (metadata.st_dev, metadata.st_ino)
        if (
            hashlib.sha256(content).hexdigest() != expected_hash
            or path in gate_paths
            or inode in gate_inodes
        ):
            raise QualificationFailure("workflow delivery gate hash/path is not unique")
        gate_paths.add(path)
        gate_inodes.add(inode)
        gate_kinds.add(row["kind"])
        artifact_paths.append(path)
    if (
        "foreign-review" not in gate_kinds
        or len(gate_kinds & {"copilot", "maintainer-review"}) != 1
    ):
        raise QualificationFailure(
            "workflow independent/delivery gate authority is incomplete"
        )
    markers = ["bundle", "candidate", "completion", "evidence", "ledger", "verdict"]
    unique_paths = sorted(set(artifact_paths))
    return (
        markers,
        [_artifact_row(path, state_root=state_root) for path in unique_paths],
        workflow,
    )


def _full_dispatch(
    *, repository: str, work_id: str, issue: int, timeout: int, evidence_dir: Path
) -> None:
    if not work_id or issue <= 0:
        raise QualificationFailure("protected full-dispatch work identity is missing")
    runtime_env = _installed_runtime_env()
    intake = _run(
        (
            "/opt/cortex/venv/bin/cortex",
            "work",
            "intake",
            work_id,
            "--repo",
            repository,
            "--issue",
            str(issue),
            "--combo",
            "feature-oneshot",
        ),
        env=runtime_env,
        timeout=60,
    )
    _require_success(intake, "full-dispatch intake")
    deadline = time.monotonic() + timeout
    terminal: object | None = None
    while time.monotonic() < deadline:
        status = _run(
            (
                "/opt/cortex/venv/bin/cortex",
                "work",
                "show",
                work_id,
                "--repo",
                repository,
                "--json",
            ),
            env=runtime_env,
            timeout=30,
        )
        if status.returncode == 0:
            records = _json_records(status.stdout)
            if records:
                terminal = records[-1]
                terminal_states = {
                    value.lower()
                    for value in _walk_values(
                        terminal, {"state", "status", "lifecycle"}
                    )
                }
                rendered = json.dumps(terminal, sort_keys=True).lower()
                if terminal_states & {"done", "delivered", "closed"}:
                    break
                if "needs_human" in rendered or '"failed"' in rendered:
                    raise QualificationFailure(
                        "full dispatch reached a failed/needs_human terminal"
                    )
        time.sleep(10)
    else:
        raise QualificationFailure(
            "full dispatch did not reach terminal closeout before timeout"
        )
    markers, artifact_rows, _workflow = _validate_dispatch_closeout(
        repository=repository,
        work_id=work_id,
        issue=issue,
        terminal=terminal,
        coordinator_root=Path(runtime_env["PSC_COORDINATOR_ROOT"]),
    )
    _write_json(
        evidence_dir / "dispatch-closeout.json",
        {
            "schema_version": 1,
            "status": "passed",
            "repository": repository,
            "work_id": work_id,
            "issue": issue,
            "terminal": terminal,
            "required_markers": markers,
            "artifacts": artifact_rows,
        },
    )


def _artifact_inventory(evidence_dir: Path) -> list[dict[str, str]]:
    paths = sorted(
        path
        for path in evidence_dir.iterdir()
        if path.is_file() and not path.is_symlink()
    )
    rows = [
        {"path": f"evidence/{path.name}", "sha256": _sha256(path)} for path in paths
    ]
    inventory_path = evidence_dir / "artifact-inventory.json"
    _write_json(
        inventory_path,
        {"schema_version": 1, "status": "passed", "artifacts": rows},
    )
    rows.append(
        {"path": "evidence/artifact-inventory.json", "sha256": _sha256(inventory_path)}
    )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--install-evidence", required=True, type=Path)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--wheel-filename", required=True)
    parser.add_argument("--probe-repository", required=True)
    parser.add_argument("--probe-work-id", required=True)
    parser.add_argument("--probe-issue", required=True, type=int)
    parser.add_argument("--dispatch-timeout", type=int, default=7200)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    if SHA40.fullmatch(args.candidate_sha) is None:
        parser.error("candidate SHA is invalid")
    for label, value in (("wheel", args.wheel_sha256), ("bundle", args.bundle_sha256)):
        if SHA256.fullmatch(value) is None:
            parser.error(f"{label} SHA-256 is invalid")
    try:
        receipt = _load_json(args.receipt, "install receipt")
        args.evidence_dir.mkdir(parents=True, exist_ok=False)
        tests = _installed_checks(
            install_evidence=args.install_evidence,
            receipt=receipt,
            evidence_dir=args.evidence_dir,
        )
        _permission_attack_matrix(receipt, args.evidence_dir)
        tests += [
            {"name": f"{family}-attack-matrix", "status": "passed"}
            for family in (
                "capability",
                "durable-state",
                "enforcement-plane",
                "process",
                "gate",
            )
        ]
        tests.append({"name": "negative-controls", "status": "passed"})
        providers = _provider_smokes(args.evidence_dir)
        tests.append({"name": "provider-capability-smoke", "status": "passed"})
        _manager_github_probe(
            args.probe_repository, args.candidate_sha, args.evidence_dir
        )
        tests.append({"name": "manager-github-dry-run-push", "status": "passed"})
        _full_dispatch(
            repository=args.probe_repository,
            work_id=args.probe_work_id,
            issue=args.probe_issue,
            timeout=args.dispatch_timeout,
            evidence_dir=args.evidence_dir,
        )
        tests.append({"name": "full-dispatch-closeout", "status": "passed"})
        tests = [
            {"name": name, "status": "passed"}
            for name in (
                "fresh-install",
                "idempotent-apply",
                "drift-detection",
                "rollback",
                "reinstall",
            )
        ] + tests
        artifacts = _artifact_inventory(args.evidence_dir)
        qualification = {
            "schema_version": 1,
            "status": "passed",
            "candidate_sha": args.candidate_sha,
            "wheel": {"filename": args.wheel_filename, "sha256": args.wheel_sha256},
            "bundle": {"sha256": args.bundle_sha256},
            "image": {"digest": args.image_digest},
            "services": _service_rows(),
            "providers": providers,
            "tests": tests,
            "artifacts": artifacts,
        }
        _write_json(args.output, qualification)
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        QualificationFailure,
    ) as exc:
        print(f"qualification driver failed: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
