#!/usr/bin/env python3
"""Trusted executable probes for Cortex release qualification and deployment canary.

The driver is copied into the reference image before the candidate is mounted.
It never imports qualification verdicts from the candidate checkout. Shared
installer and isolation probes always fail closed; provider-native identity and
protected repository probes are additionally required by deployment-canary mode.
The legacy-adoption mode (#1122) instead binds the in-container adoption
harness evidence to the live receipt and host; it runs no attack matrix and no
provider or repository probe.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import pwd
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from paulsha_cortex.coordinator import job_runner, spool_slot
from paulsha_cortex.trust_root.registry import (
    JobWriteContract,
    inner_sandbox_attached_for,
    sandbox_mode_for,
)
from paulsha_cortex.trust_root.surfaces import writable_surface

try:
    from qualification.contract import (
        CANARY_BUILDER,
        CANARY_GIT_IDENTITY,
        CANARY_REVIEWER,
        PROBE_GATE_PYTEST_VERSION,
        PROVIDERS as PROVIDER_CONTRACTS,
        TOOLCHAIN,
        WHEELS,
        canary_identity,
    )
    from qualification import legacy_fixture
except ModuleNotFoundError:  # 直接以 qualification/driver.py 執行時 sys.path[0] 是 qualification/
    from contract import (  # type: ignore[no-redef]
        CANARY_BUILDER,
        CANARY_GIT_IDENTITY,
        CANARY_REVIEWER,
        PROBE_GATE_PYTEST_VERSION,
        PROVIDERS as PROVIDER_CONTRACTS,
        TOOLCHAIN,
        WHEELS,
        canary_identity,
    )
    import legacy_fixture  # type: ignore[no-redef]


SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
WORK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DEPLOYMENT_CANARY_BUILDER_EXECUTOR, DEPLOYMENT_CANARY_BUILDER_MODEL = canary_identity(
    CANARY_BUILDER
)
DEPLOYMENT_CANARY_REVIEWER_EXECUTOR, DEPLOYMENT_CANARY_REVIEWER_MODEL = canary_identity(
    CANARY_REVIEWER
)
DEPLOYMENT_CANARY_PROBE_CARD = "worktree-isolation"
DEPLOYMENT_CANARY_BUILDER_PATH = "/opt/cortex/toolchain/bin:/usr/bin:/bin"
#: #1096：部署層固定宣告的 `PSC_GATE_CMD_*`（見
#: `docs/superpowers/runbooks/deployment-canary-probe.md` §5：canary 每張非-ship 卡
#: 的 gate 身分只跑 `PSC_GATE_CMD_PYTEST`）。closeout 逐項驗證這個集合裡的每個
#: gate 名稱都存在於 Manager 權威 gate ledger 且為 terminal passed，回歸把 gate
#: 跳過、或跳過後仍宣告 `passed` 都會被擋下，而不是只驗 ledger 的外層形狀。
DEPLOYMENT_CANARY_EXPECTED_GATE_NAMES = frozenset({"pytest"})
MAX_AGENT_LOOP_LOG_BYTES = 128 * 1024 * 1024
MAX_AGENT_LOOP_COMMANDS = 128
MAX_DISPATCH_ARTIFACTS = 128
MAX_DISPATCH_ARTIFACT_PATH_CHARS = 128
PROVIDERS = {
    name: (row["model_id"], row["effort"], row["account"])
    for name, row in PROVIDER_CONTRACTS.items()
}
SERVICES = (
    "cortex-egress-proxy.service",
    "cortex-manager.service",
    "cortex-monitor.service",
)


#: codex app-server 單次 status 交換的時限（秒）；逾時會以新 process 重試一次（#716）。
CODEX_STATUS_PROBE_TIMEOUT_SECONDS = 90


@dataclass(frozen=True)
class ProviderPreflightAdapter:
    version: str | None
    version_command: tuple[str, ...]
    status_command: tuple[str, ...] | None
    status_kind: str | None = None


PROVIDER_PREFLIGHTS = {
    # The pinned AGY exposes the read-only /quota slash command as a structured
    # print-mode response; do not pass a made-up "status" subcommand.
    "agy": ProviderPreflightAdapter(
        version=TOOLCHAIN["agy"]["version"],
        version_command=("/opt/cortex/toolchain/bin/agy", "--version"),
        status_command=(
            "/opt/cortex/toolchain/bin/agy",
            "-p",
            "/quota",
            "--output-format",
            "json",
        ),
        status_kind="agy-quota",
    ),
    # Copilot's pinned headless SDK server exposes structured auth/quota RPCs;
    # do not infer either from the interactive /user or /usage commands.
    # `--no-auto-login` is deliberately absent: on 1.0.88 it stops the server
    # from loading the stored login, so `account.getCurrentAuth` is empty.
    "copilot": ProviderPreflightAdapter(
        version=None,
        version_command=("/opt/cortex/toolchain/bin/copilot", "--version"),
        status_command=(
            "/opt/cortex/toolchain/bin/copilot",
            "--headless",
            "--no-auto-update",
            "--stdio",
        ),
        status_kind="copilot-app-server",
    ),
    # Codex app-server is the pinned CLI's structured account/rate-limit
    # protocol.  ``doctor --json`` only reports local health and is not a
    # provider capability proof.
    "codex": ProviderPreflightAdapter(
        version=TOOLCHAIN["codex"]["version"],
        version_command=("/opt/cortex/toolchain/bin/codex", "--version"),
        status_command=(
            "/opt/cortex/toolchain/bin/codex",
            "app-server",
            "--stdio",
        ),
        status_kind="codex-app-server",
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
        # 與產生的 job unit 相同（`Environment=XDG_CACHE_HOME=<HOME>/cache`）：HOME 本身
        # root-owned，帳號唯一可寫的是 `cache`；copilot 1.0.88 會把自身 pkg 解到
        # `$XDG_CACHE_HOME/copilot/pkg`，沒有這一格就會嘗試建立不可寫的 `~/.cache`。
        "XDG_CACHE_HOME": f"{home}/cache",
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
    # Production keeps the raw registry identity separate from the systemd
    # template instance. Derive `%i` through the same helper so every mounted
    # per-job surface uses the exact instance string.
    legal_job_id = "qualification-negctl"
    legal_instance = job_runner.template_instance_id(legal_job_id)
    legal_workspace = Path(f"/var/lib/cortex/worktree/{legal_instance}")
    legal_log = Path(
        f"/var/lib/cortex/coordinator/commit-spool/build-logs/{legal_instance}/job.jsonl"
    )
    legal_workspace.mkdir(mode=0o700, exist_ok=False)
    os.chown(legal_workspace, builder.pw_uid, builder.pw_gid)
    manager_runtime_env = _installed_runtime_env()
    manager_runtime_env["HOME"] = "/var/lib/cortex-manager"
    # Start the real template unit only after provisioning every registered
    # per-job surface through the same helpers as ``launcher.launch()``.
    # Otherwise systemd namespace setup fails before the builder process runs,
    # and this negative control would merely exercise an incomplete synthetic
    # spec rather than the deployed builder contract.
    prepare_runtime_code = (
        "from paulsha_cortex.coordinator import spool_slot, job_workspace\n"
        f"job_id={legal_job_id!r}\n"
        f"instance={legal_instance!r}\n"
        "job_workspace.prepare_commit_spool(spool_key=instance)\n"
        "spool_slot.provision_runtime_surfaces(\n"
        "    principal='builder', job_id=job_id,\n"
        "    canonical_codex_home=spool_slot.canonical_codex_controls(\n"
        "        'builder', manager_env=__import__('os').environ),\n"
        "    account='cortex-builder')\n"
    )
    _require_success(
        _run(
            ("/opt/cortex/venv/bin/python", "-c", prepare_runtime_code),
            user="cortex-manager",
            env=manager_runtime_env,
        ),
        "gate Manager legal-job runtime-surface provisioning",
    )
    spec_code = (
        "from paulsha_cortex.coordinator import job_runner\n"
        "from paulsha_cortex.coordinator.job_workspace import prepare_job_log_spool\n"
        f"job_id={legal_job_id!r}\n"
        f"instance={legal_instance!r}\n"
        f"workspace={str(legal_workspace)!r}\n"
        "canonical_log=prepare_job_log_spool("
        "principal_id='builder', spool_key=instance, manager_log_path="
        f"{str(legal_log)!r})\n"
        "spec=job_runner.build_job_spec("
        "job_id=job_id, instance=instance, unit=f'cortex-job@{instance}.service',"
        "command=['/usr/bin/id'], working_directory=workspace,"
        "log_path=str(canonical_log), env={'HOME':'/var/lib/cortex-builder','PATH':'/usr/bin:/bin'})\n"
        "job_runner.write_job_spec(job_runner.job_spec_path(job_runner.DEFAULT_JOB_SPEC_SPOOL, instance), spec, account='cortex-builder')\n"
    )
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
    legal_log.parent.rmdir()
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


def _codex_app_server_exchange(
    command: Sequence[str],
    *,
    user: str,
    env: Mapping[str, str],
    timeout: int = 45,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Read Codex account and rate-limit state through its JSON-RPC server.

    The app-server protocol is line-delimited JSON.  The process is always
    short-lived and its output stays in memory; account identifiers and token
    material must never reach qualification logs or error text.
    """

    argv = list(command)
    if user:
        argv = ["/usr/sbin/runuser", "-u", user, "--", *argv]
    process_env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}
    process_env.update(env)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=process_env,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout

    def send(message: Mapping[str, object]) -> None:
        if process.stdin is None:
            raise QualificationFailure("Codex app-server stdin is unavailable")
        process.stdin.write(
            json.dumps(message, separators=(",", ":"), sort_keys=True) + "\n"
        )
        process.stdin.flush()

    def receive(request_id: int, method: str) -> Mapping[str, object]:
        if process.stdout is None:
            raise QualificationFailure("Codex app-server stdout is unavailable")
        timed_out = f"Codex app-server status probe timed out waiting for {method}"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QualificationFailure(timed_out)
            ready, _unused_write, _unused_error = select.select(
                [process.stdout], [], [], remaining
            )
            if not ready:
                raise QualificationFailure(timed_out)
            line = process.stdout.readline()
            if line == "":
                raise QualificationFailure("Codex app-server closed before status response")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise QualificationFailure(
                    "Codex app-server emitted a non-JSON status record"
                ) from exc
            if not isinstance(record, Mapping):
                raise QualificationFailure("Codex app-server status record is not an object")
            # Notifications, including remote-control status updates, have no
            # request id and are ignored while waiting for our response.
            if record.get("id") != request_id:
                continue
            return record

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "cortex_rc",
                        "title": "Cortex RC qualification",
                        "version": "0.1.0",
                    }
                },
            }
        )
        initialize = receive(1, "initialize")
        if "error" in initialize or not isinstance(initialize.get("result"), Mapping):
            raise QualificationFailure("Codex app-server initialize failed")
        send(
            {
                "jsonrpc": "2.0",
                "method": "initialized",
                "params": {},
            }
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/read",
                "params": {"refreshToken": False},
            }
        )
        account = receive(2, "account/read")
        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "account/rateLimits/read",
                "params": None,
            }
        )
        rate_limits = receive(3, "account/rateLimits/read")
        return account, rate_limits
    except (BrokenPipeError, OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure("Codex app-server status probe failed") from exc
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
                process.wait()


def _codex_thread_resume_exchange(
    command: Sequence[str],
    *,
    thread_id: str,
    user: str,
    env: Mapping[str, str],
    timeout: int = 45,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Read then resume one completed Codex thread without starting a turn."""

    argv = list(command)
    if user:
        argv = ["/usr/sbin/runuser", "-u", user, "--", *argv]
    process_env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}
    process_env.update(env)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=process_env,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout

    def send(message: Mapping[str, object]) -> None:
        if process.stdin is None:
            raise QualificationFailure("Codex app-server stdin is unavailable")
        process.stdin.write(
            json.dumps(message, separators=(",", ":"), sort_keys=True) + "\n"
        )
        process.stdin.flush()

    def receive(request_id: int) -> Mapping[str, object]:
        if process.stdout is None:
            raise QualificationFailure("Codex app-server stdout is unavailable")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QualificationFailure(
                    "Codex app-server thread identity probe timed out"
                )
            ready, _unused_write, _unused_error = select.select(
                [process.stdout], [], [], remaining
            )
            if not ready:
                raise QualificationFailure(
                    "Codex app-server thread identity probe timed out"
                )
            line = process.stdout.readline()
            if line == "":
                raise QualificationFailure(
                    "Codex app-server closed before thread identity response"
                )
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise QualificationFailure(
                    "Codex app-server emitted a non-JSON thread identity record"
                ) from exc
            if not isinstance(record, Mapping):
                raise QualificationFailure(
                    "Codex app-server thread identity record is not an object"
                )
            if record.get("id") == request_id:
                return record

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "cortex_rc",
                        "title": "Cortex RC qualification",
                        "version": "0.1.0",
                    }
                },
            }
        )
        initialize = receive(1)
        if "error" in initialize or not isinstance(initialize.get("result"), Mapping):
            raise QualificationFailure("Codex app-server initialize failed")
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "thread/read",
                "params": {"threadId": thread_id, "includeTurns": False},
            }
        )
        persisted = receive(2)
        resume_cwd = env.get("HOME")
        if not isinstance(resume_cwd, str) or not Path(resume_cwd).is_absolute():
            raise QualificationFailure("Codex app-server resume cwd is unavailable")
        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "thread/resume",
                "params": {
                    "threadId": thread_id,
                    "excludeTurns": True,
                    "cwd": resume_cwd,
                },
            }
        )
        return persisted, receive(3)
    except (BrokenPipeError, OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure(
            "Codex app-server thread identity probe failed"
        ) from exc
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
                process.wait()


def _codex_provider_thread_result(
    thread_id: str, *, codex_home: Path | None = None
) -> Mapping[str, object]:
    """Load provider-persisted metadata for one exact Codex thread."""

    adapter = PROVIDER_PREFLIGHTS["codex"]
    if adapter.status_command is None:
        raise QualificationFailure("Codex app-server command is unavailable")
    account_env = _account_env("cortex-builder")
    if codex_home is not None:
        account_env["CODEX_HOME"] = str(codex_home)
    persisted_response, response = _codex_thread_resume_exchange(
        adapter.status_command,
        thread_id=thread_id,
        user="cortex-builder",
        env=account_env,
    )
    persisted_result = persisted_response.get("result")
    persisted_thread = (
        persisted_result.get("thread")
        if isinstance(persisted_result, Mapping)
        else None
    )
    result = response.get("result")
    thread = result.get("thread") if isinstance(result, Mapping) else None
    if (
        "error" in persisted_response
        or "error" in response
        or not isinstance(persisted_thread, Mapping)
        or persisted_thread.get("id") != thread_id
        or not isinstance(result, Mapping)
        or not isinstance(thread, Mapping)
        or thread.get("id") != thread_id
    ):
        raise QualificationFailure("Codex provider thread identity is unavailable")
    combined = dict(result)
    combined["persistedCwd"] = persisted_thread.get("cwd")
    combined["persistedModelProvider"] = persisted_thread.get("modelProvider")
    return combined


def _codex_thread_runtime_identity(
    thread_id: str, *, expected_worktree: str, codex_home: Path
) -> dict[str, str]:
    """Bind requested builder settings to provider-persisted thread metadata."""

    result = _codex_provider_thread_result(thread_id, codex_home=codex_home)
    if (
        result.get("model") != DEPLOYMENT_CANARY_BUILDER_MODEL
        or result.get("reasoningEffort") != PROVIDERS["codex"][1]
        or result.get("modelProvider") != "openai"
        or result.get("persistedModelProvider") != "openai"
        or result.get("persistedCwd") != expected_worktree
    ):
        raise QualificationFailure(
            "Codex agent-loop provider thread identity does not match the bound job"
        )
    return {
        "runtime_model": DEPLOYMENT_CANARY_BUILDER_MODEL,
        "runtime_effort": PROVIDERS["codex"][1],
        "model_provider": "openai",
        "thread_sha256": hashlib.sha256(thread_id.encode()).hexdigest(),
    }


def _codex_preflight_from_responses(
    account_response: Mapping[str, object],
    rate_limits_response: Mapping[str, object],
) -> dict[str, object]:
    """Validate the stable account/rate-limit fields without recording PII."""

    account_result = account_response.get("result")
    account = account_result.get("account") if isinstance(account_result, Mapping) else None
    account_type = account.get("type") if isinstance(account, Mapping) else None
    if account_type not in {"apiKey", "chatgpt"}:
        raise QualificationFailure(
            "provider codex app-server did not report an authenticated account"
        )

    rate_result = rate_limits_response.get("result")
    if not isinstance(rate_result, Mapping):
        raise QualificationFailure(
            "provider codex app-server did not report structured rate limits"
        )
    snapshots: list[Mapping[str, object]] = []
    by_limit = rate_result.get("rateLimitsByLimitId")
    if isinstance(by_limit, Mapping):
        snapshots.extend(value for value in by_limit.values() if isinstance(value, Mapping))
    single = rate_result.get("rateLimits")
    if isinstance(single, Mapping):
        snapshots.append(single)
    if not snapshots:
        raise QualificationFailure(
            "provider codex app-server returned no rate-limit buckets"
        )

    # `credits` 是加購點數；方案內額度可用時 provider 不需要它（#716）。
    ordinary_usage_allowed = rate_result.get("ordinaryUsageAllowed") is True
    observed_window = False
    for snapshot in snapshots:
        if snapshot.get("rateLimitReachedType") is not None:
            raise QualificationFailure(
                "provider codex app-server reports exhausted rate limits"
            )
        if snapshot.get("spendControlReached") is True:
            raise QualificationFailure(
                "provider codex app-server reports spend control reached"
            )
        credits = snapshot.get("credits")
        if isinstance(credits, Mapping) and not ordinary_usage_allowed:
            if credits.get("hasCredits") is not True and credits.get("unlimited") is not True:
                raise QualificationFailure(
                    "provider codex app-server reports no remaining credits"
                )
        for window_name in ("primary", "secondary"):
            window = snapshot.get(window_name)
            if window is None:
                continue
            if not isinstance(window, Mapping):
                raise QualificationFailure(
                    "provider codex app-server returned an invalid rate-limit window"
                )
            used = window.get("usedPercent")
            if not isinstance(used, int) or isinstance(used, bool) or not 0 <= used <= 100:
                raise QualificationFailure(
                    "provider codex app-server returned an invalid usage percentage"
                )
            duration = window.get("windowDurationMins")
            if duration is not None and (
                not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0
            ):
                raise QualificationFailure(
                    "provider codex app-server returned an invalid rate-limit duration"
                )
            resets_at = window.get("resetsAt")
            if resets_at is not None and (
                not isinstance(resets_at, int) or isinstance(resets_at, bool) or resets_at <= 0
            ):
                raise QualificationFailure(
                    "provider codex app-server returned an invalid rate-limit reset"
                )
            observed_window = True
            if used >= 100:
                raise QualificationFailure(
                    "provider codex app-server reports no remaining rate-limit capacity"
                )
    if not observed_window:
        raise QualificationFailure(
            "provider codex app-server returned no usable rate-limit window"
        )
    return {
        "status": "ready",
        "authenticated": True,
        "quota": "available",
        "fallback": False,
    }


def _copilot_app_server_exchange(
    command: Sequence[str],
    *,
    user: str,
    env: Mapping[str, str],
    timeout: int = 45,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Read Copilot auth and quota through its headless SDK JSON-RPC server."""

    argv = list(command)
    if user:
        argv = ["/usr/sbin/runuser", "-u", user, "--", *argv]
    process_env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}
    process_env.update(env)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=process_env,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    buffer = b""

    def send(message: Mapping[str, object]) -> None:
        if process.stdin is None:
            raise QualificationFailure("Copilot app-server stdin is unavailable")
        body = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
        process.stdin.write(
            b"Content-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\n\r\n"
            + body
        )
        process.stdin.flush()

    def receive(request_id: int) -> Mapping[str, object]:
        nonlocal buffer
        if process.stdout is None:
            raise QualificationFailure("Copilot app-server stdout is unavailable")
        while True:
            while b"\r\n\r\n" in buffer:
                header, body = buffer.split(b"\r\n\r\n", 1)
                fields: dict[bytes, bytes] = {}
                for line in header.split(b"\r\n"):
                    key, separator, value = line.partition(b":")
                    if separator:
                        fields[key.strip().lower()] = value.strip()
                length = fields.get(b"content-length")
                if length is None:
                    raise QualificationFailure(
                        "Copilot app-server response has no content length"
                    )
                try:
                    body_length = int(length)
                except ValueError as exc:
                    raise QualificationFailure(
                        "Copilot app-server response has an invalid content length"
                    ) from exc
                if body_length < 0:
                    raise QualificationFailure(
                        "Copilot app-server response has an invalid content length"
                    )
                if len(body) < body_length:
                    break
                payload = body[:body_length]
                buffer = body[body_length:]
                try:
                    record = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise QualificationFailure(
                        "Copilot app-server emitted a non-JSON status record"
                    ) from exc
                if not isinstance(record, Mapping):
                    raise QualificationFailure(
                        "Copilot app-server status record is not an object"
                    )
                if record.get("id") == request_id:
                    return record
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QualificationFailure("Copilot app-server status probe timed out")
            ready, _unused_write, _unused_error = select.select(
                [process.stdout], [], [], remaining
            )
            if not ready:
                raise QualificationFailure("Copilot app-server status probe timed out")
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise QualificationFailure(
                    "Copilot app-server closed before status response"
                )
            buffer += chunk

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "connect", "params": {}})
        connected = receive(1)
        if "error" in connected or not isinstance(connected.get("result"), Mapping):
            raise QualificationFailure("Copilot app-server connect failed")
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account.getCurrentAuth",
                "params": {},
            }
        )
        auth = receive(2)
        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "account.getQuota",
                "params": {},
            }
        )
        quota = receive(3)
        return auth, quota
    except (BrokenPipeError, OSError, subprocess.SubprocessError) as exc:
        raise QualificationFailure("Copilot app-server status probe failed") from exc
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
                process.wait()


def _copilot_preflight_from_responses(
    auth_response: Mapping[str, object],
    quota_response: Mapping[str, object],
) -> dict[str, object]:
    """Validate Copilot's provider-native auth and quota snapshots."""

    auth_result = auth_response.get("result")
    auth_info = auth_result.get("authInfo") if isinstance(auth_result, Mapping) else None
    if not isinstance(auth_info, Mapping) or not auth_info:
        raise QualificationFailure(
            "provider copilot app-server did not report an authenticated account"
        )
    quota_result = quota_response.get("result")
    snapshots = (
        quota_result.get("quotaSnapshots")
        if isinstance(quota_result, Mapping)
        else None
    )
    if not isinstance(snapshots, Mapping) or not snapshots:
        raise QualificationFailure(
            "provider copilot app-server did not report structured quota snapshots"
        )
    observed = False
    for snapshot in snapshots.values():
        if not isinstance(snapshot, Mapping):
            raise QualificationFailure(
                "provider copilot app-server returned an invalid quota snapshot"
            )
        if snapshot.get("isUnlimitedEntitlement") is True:
            observed = True
            continue
        remaining = snapshot.get("remainingPercentage")
        if (
            not isinstance(remaining, (int, float))
            or isinstance(remaining, bool)
            or not 0 <= float(remaining) <= 100
        ):
            raise QualificationFailure(
                "provider copilot app-server returned an invalid remaining percentage"
            )
        observed = True
        has_quota = snapshot.get("hasQuota")
        if has_quota is False:
            raise QualificationFailure(
                "provider copilot app-server reports no remaining quota"
            )
        if float(remaining) <= 0 and not (
            snapshot.get("usageAllowedWithExhaustedQuota") is True
            or snapshot.get("overageAllowedWithExhaustedQuota") is True
        ):
            # 額度用盡但帳號允許超額繼續使用時，provider 仍可服務（#716）。
            raise QualificationFailure(
                "provider copilot app-server reports no remaining quota"
            )
    if not observed:
        raise QualificationFailure(
            "provider copilot app-server returned no usable quota snapshot"
        )
    return {
        "status": "ready",
        "authenticated": True,
        "quota": "available",
        "fallback": False,
    }


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

    account_env = _account_env(account)
    if adapter.status_kind == "codex-app-server":
        # #716：app-server 對上游的 account／rate-limit 查詢偶發卡住（本機經 egress
        # proxy 重現過數次不回應，重開一個 process 即正常）。只有傳輸層逾時才以新
        # process 重試一次；認證失敗、額度用盡等明確回答仍立即 fail closed。
        for attempt in range(2):
            try:
                account_response, rate_limits_response = _codex_app_server_exchange(
                    adapter.status_command,
                    user=account,
                    env=account_env,
                    timeout=CODEX_STATUS_PROBE_TIMEOUT_SECONDS,
                )
                break
            except QualificationFailure as exc:
                if attempt == 1 or "status probe timed out" not in str(exc):
                    raise
        return _codex_preflight_from_responses(account_response, rate_limits_response)
    if adapter.status_kind == "copilot-app-server":
        auth_response, quota_response = _copilot_app_server_exchange(
            adapter.status_command,
            user=account,
            env=account_env,
            timeout=45,
        )
        return _copilot_preflight_from_responses(auth_response, quota_response)

    status = _run(
        adapter.status_command,
        user=account,
        env=account_env,
        timeout=45,
    )
    records = _json_records(status.stdout)
    payload = records[0] if len(records) == 1 else None
    if status.returncode != 0 or not isinstance(payload, Mapping):
        raise QualificationFailure(
            f"provider {provider} structured status probe did not return one "
            "successful JSON object"
        )

    if adapter.status_kind == "agy-quota":
        command = payload.get("command")
        data = command.get("data") if isinstance(command, Mapping) else None
        groups = data.get("groups") if isinstance(data, Mapping) else None
        if (
            payload.get("status") != "SUCCESS"
            or not isinstance(command, Mapping)
            or command.get("name") != "usage"
            or not isinstance(groups, list)
            or not groups
        ):
            raise QualificationFailure(
                f"provider {provider} structured quota probe lacks a successful "
                "usage payload"
            )
        remaining: list[float] = []
        for group in groups:
            buckets = group.get("buckets") if isinstance(group, Mapping) else None
            if not isinstance(buckets, list) or not buckets:
                raise QualificationFailure(
                    f"provider {provider} structured quota payload has no buckets"
                )
            for bucket in buckets:
                fraction = bucket.get("remaining_fraction") if isinstance(bucket, Mapping) else None
                if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
                    raise QualificationFailure(
                        f"provider {provider} structured quota payload has an invalid remaining fraction"
                    )
                if not 0 <= float(fraction) <= 1:
                    raise QualificationFailure(
                        f"provider {provider} structured quota payload has an out-of-range remaining fraction"
                    )
                remaining.append(float(fraction))
        if not remaining or min(remaining) <= 0:
            raise QualificationFailure(
                f"provider {provider} structured quota reports no remaining capacity"
            )
        return {
            "status": "ready",
            "authenticated": True,
            "quota": "available",
            "fallback": False,
        }

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
        if (
            isinstance(record.get("conversation_id"), str)
            and record.get("status") == "SUCCESS"
            and isinstance(record.get("response"), str)
        ):
            # agy 1.2.x `--print --output-format json` 以單一 result 物件回覆，並一律在
            # response 尾端附加一個換行；只剝掉這一個換行，其餘仍須逐字相等。
            final_contents.append(str(record["response"]).removesuffix("\n"))
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


def _codex_registry_sandbox_argv(
    contract: JobWriteContract, *, trust_root_outer_unit: bool
) -> tuple[str, ...]:
    """由 `registry.SANDBOX_MODE_DERIVATION` 導出 Codex 的 `--sandbox` argv。

    與 `launcher.build_codex_argv()` 消費同一格：mode 取 `sandbox_mode_for()`，
    是否附掛內層沙箱取 `inner_sandbox_attached_for()`。qualification 只接受「不附掛」
    的列——codex 0.157 的 legacy Landlock（`--enable use_legacy_landlock`）起不來，
    登記表若把附掛改回來，這裡 fail-closed，而不是默默發出必死的 argv。
    """

    mode = sandbox_mode_for(contract, trust_root_outer_unit=trust_root_outer_unit)
    if mode is None or inner_sandbox_attached_for(
        contract, trust_root_outer_unit=trust_root_outer_unit
    ):
        raise QualificationFailure(
            f"Codex sandbox row {contract.value} is not usable for qualification"
        )
    return ("--sandbox", mode)


def _codex_provider_smoke_sandbox_argv() -> tuple[str, ...]:
    """Codex provider smoke 的 sandbox argv。

    smoke 由 driver 以 `runuser` 直接啟動，**不在** Trust Root 模板 unit 內，因此取
    登記表的 direct 欄（`trust_root_outer_unit=False`）。它以 `cortex-builder` 執行、
    沒有卡片契約，登記表對「builder 且契約缺欄」的裁決是 `BUILDER_WORKSPACE_WRITE`，
    該列 direct 發 `danger-full-access` 且不附內層沙箱——與 canary builder 卡
    （`worktree-isolation`，`BUILDER_WRITE_FORBIDDEN`）在模板 unit 內 outer-unit 欄
    發出的 `--sandbox` 完全相同。

    `--skip-git-repo-check` 沿用 launcher 對「工作區不是 repo」的既有規則
    （planner／reviewer 列同樣附帶）：smoke 的 cwd 是容器根目錄而不是 per-job clone，
    codex 0.157.1 沒有此旗標會以「Not inside a trusted directory」直接結束。
    """

    return (
        *_codex_registry_sandbox_argv(
            JobWriteContract.BUILDER_WORKSPACE_WRITE, trust_root_outer_unit=False
        ),
        "--skip-git-repo-check",
    )


def _codex_canary_builder_sandbox_argv() -> tuple[str, ...]:
    """canary builder 卡在 Trust Root 模板 unit 內實際收到的 sandbox argv。"""

    return _codex_registry_sandbox_argv(
        JobWriteContract.BUILDER_WRITE_FORBIDDEN, trust_root_outer_unit=True
    )


AGY_CONVERSATION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
# 以 account 身分讀自己 HOME 內 agy 持久化的對話，列出 executor 設定中的模型變體 id
# （例如 `gemini-3.8-flash-high`）。root 不直接開啟帳號可寫樹內的檔案。
AGY_PERSISTED_VARIANTS_SCRIPT = """
import json, re, sqlite3, sys
from pathlib import Path
conversation_id = sys.argv[1]
database = Path.home() / ".gemini" / "antigravity-cli" / "conversations" / f"{conversation_id}.db"
if database.is_symlink() or not database.is_file():
    raise SystemExit("agy conversation database is missing or not a regular file")
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
pattern = re.compile(rb"(?<![A-Za-z0-9._-])gemini-[0-9][A-Za-z0-9._-]*(?![A-Za-z0-9._-])")
variants = set()
for (data,) in connection.execute("select data from executor_metadata"):
    blob = data if isinstance(data, (bytes, bytearray)) else str(data).encode()
    variants.update(match.decode("ascii") for match in pattern.findall(blob))
print(json.dumps(sorted(variants)))
"""


def _agy_persisted_model_variants(conversation_id: str, *, account: str) -> set[str]:
    """Return the model variant ids agy persisted for one exact conversation.

    agy 1.2.x 的 print 輸出不回報 model／effort；它把本次對話實際使用的變體
    （``<model>-<effort>``）寫在對話資料庫的 executor metadata。這與 Codex smoke 讀
    provider 持久化 thread 的 model／reasoningEffort 是同一類原生證據。
    """

    if AGY_CONVERSATION_ID_RE.fullmatch(conversation_id) is None:
        raise QualificationFailure("provider agy conversation identity is malformed")
    result = _run(
        ("/usr/bin/python3", "-I", "-c", AGY_PERSISTED_VARIANTS_SCRIPT, conversation_id),
        user=account,
        env=_account_env(account),
        timeout=60,
    )
    _require_success(result, "provider agy persisted conversation read")
    try:
        variants = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise QualificationFailure(
            "provider agy persisted conversation read returned malformed output"
        ) from exc
    if not isinstance(variants, list) or not all(
        isinstance(value, str) for value in variants
    ):
        raise QualificationFailure(
            "provider agy persisted conversation read returned malformed output"
        )
    return set(variants)


def _provider_smokes(evidence_dir: Path) -> list[dict[str, object]]:
    prompt = "Return exactly QUALIFICATION_OK and do not use tools."
    agy_model, agy_effort, _agy_account = PROVIDERS["agy"]
    copilot_model, copilot_effort, _copilot_account = PROVIDERS["copilot"]
    codex_model, codex_effort, _codex_account = PROVIDERS["codex"]
    codex_sandbox_argv = _codex_provider_smoke_sandbox_argv()
    commands = {
        "agy": (
            "/opt/cortex/toolchain/bin/agy",
            "--print",
            prompt,
            "--mode",
            "plan",
            "--sandbox",
            "--model",
            agy_model,
            "--effort",
            agy_effort,
            "--output-format",
            "json",
            "--disable-slash-commands",
        ),
        "copilot": (
            "/opt/cortex/toolchain/bin/copilot",
            "--prompt",
            prompt,
            "--model",
            copilot_model,
            "--effort",
            copilot_effort,
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
            prompt,
            "--json",
            *codex_sandbox_argv,
            "--model",
            codex_model,
            "-c",
            f'model_reasoning_effort="{codex_effort}"',
        ),
    }
    verdicts: list[dict[str, object]] = []
    raw_evidence: dict[str, object] = {"schema_version": 1, "providers": {}}
    for provider, command in commands.items():
        model, effort, account = PROVIDERS[provider]
        preflight = _provider_preflight(provider, account)
        result = _run(command, user=account, env=_account_env(account), timeout=300)
        records = _json_records(result.stdout)
        if provider == "codex":
            thread_ids = {
                str(row["thread_id"])
                for row in records
                if isinstance(row, Mapping)
                and row.get("type") == "thread.started"
                and isinstance(row.get("thread_id"), str)
                and re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}", str(row["thread_id"])
                )
            }
            if len(thread_ids) != 1:
                raise QualificationFailure(
                    "provider codex returned no unique persisted thread identity"
                )
            persisted = _codex_provider_thread_result(next(iter(thread_ids)))
            if persisted.get("modelProvider") != "openai":
                raise QualificationFailure(
                    "provider codex persisted thread used an unexpected provider"
                )
            models = {
                str(persisted["model"])
                if isinstance(persisted.get("model"), str)
                else ""
            }
            efforts = {
                str(persisted["reasoningEffort"])
                if isinstance(persisted.get("reasoningEffort"), str)
                else ""
            }
        elif provider == "agy":
            conversation_ids = {
                str(row["conversation_id"])
                for row in records
                if isinstance(row, Mapping)
                and isinstance(row.get("conversation_id"), str)
            }
            if len(conversation_ids) != 1 or AGY_CONVERSATION_ID_RE.fullmatch(
                next(iter(conversation_ids))
            ) is None:
                raise QualificationFailure(
                    "provider agy returned no unique conversation identity"
                )
            persisted_variants = _agy_persisted_model_variants(
                next(iter(conversation_ids)), account=account
            )
            if persisted_variants == {f"{model}-{effort}"}:
                models, efforts = {model}, {effort}
            else:
                models, efforts = set(persisted_variants), set()
        else:
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
        if provider == "agy":
            raw_evidence["providers"][provider]["persisted_variants"] = sorted(
                persisted_variants
            )
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


#: permgen 為 durable owner 產生的 GitHub HTTPS credential 範圍與 helper（#716）。
GITHUB_HTTPS_CREDENTIAL_URL = "https://github.com"
MANAGER_GH_CREDENTIAL_HELPER = "!/usr/bin/gh auth git-credential"


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
            "--get-regexp",
            r"^credential\..*helper$",
        ),
        user=account,
        env=env,
        timeout=30,
    )
    # `--get-regexp` 找不到任何鍵時回 1；那代表 installed helper 不存在，歸為同一種
    # 「不是唯一 installed helper」的失敗，而不是 git 本身的錯誤。
    if helper.returncode not in (0, 1):
        _require_success(helper, "Manager installed credential helper inventory")
    helper_rows: list[tuple[str, str, str, str]] = []
    for line in helper.stdout.splitlines():
        scope, origin, entry = (line.split("\t", 2) + ["", "", ""])[:3]
        key, _separator, value = entry.partition(" ")
        helper_rows.append((scope, origin, key, value))
    # permgen.build_account_gitconfig() 只為 durable owner 寫 URL-scoped helper：先以
    # 空值清掉任何繼承的 helper，再只對 https://github.com 委派給 gh（#716）。所有
    # scope 的 credential helper 設定必須恰好是這兩列，repo 本地或其他 URL 再加掛
    # 任何 helper 都會在 reset 之後生效，一律拒絕。
    expected_origin = f"file:{gitconfig}"
    expected_key = f"credential.{GITHUB_HTTPS_CREDENTIAL_URL}.helper"
    if helper_rows != [
        ("global", expected_origin, expected_key, ""),
        ("global", expected_origin, expected_key, MANAGER_GH_CREDENTIAL_HELPER),
    ]:
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
    # `--refs` 只列 refs/*：真實 ls-remote 第一行是 `HEAD`（及 peeled tag `^{}`），
    # 會被 `_parse_remote_refs` 當成畸形而失敗（#716 canary）。
    remote = f"https://github.com/{repository}.git"
    before = _run(
        ("/usr/bin/git", "ls-remote", "--refs", remote), user=account, env=env, timeout=60
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
        ("/usr/bin/git", "ls-remote", "--refs", remote), user=account, env=env, timeout=60
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


PROBE_DEFAULT_BRANCH = "main"
PROBE_AUTHORITY_TIMEOUT_SECONDS = 900
PROBE_AUTHORITY_POLL_SECONDS = 15
PROBE_CHECKOUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
PROJECT_CONFIG_NAME = "project-cortex.yaml"


def _manager_gh_api(path: str, *, timeout: int = 60) -> tuple[int, object]:
    """以 Manager 身分與其已匯入的 gh 登入態讀一個 GitHub REST 資源（只讀）。"""

    account = "cortex-manager"
    result = _run(
        ("/usr/bin/gh", "api", path),
        user=account,
        env=_account_env(account),
        timeout=timeout,
    )
    if result.returncode != 0:
        return result.returncode, None
    try:
        return 0, json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise QualificationFailure(
            f"GitHub API returned malformed JSON for {path}"
        ) from exc


def _production_pr_labels(repository: str, work_id: str, issue: int) -> tuple[str, ...]:
    """ship lane 實際會掛上 PR 的 labels，直接由 production 的 PR metadata 導出。"""

    from types import SimpleNamespace

    from paulsha_cortex.coordinator.work_bridge import _pr_metadata

    metadata = _pr_metadata(
        SimpleNamespace(
            repo=repository, work_id=work_id, issue_refs=(f"{repository}#{issue}",)
        )
    )
    labels = metadata.get("labels")
    if not isinstance(labels, list) or not all(
        isinstance(label, str) and label for label in labels
    ):
        raise QualificationFailure("production PR metadata labels are malformed")
    return tuple(labels)


def _probe_repository_prerequisites(repository: str, work_id: str, issue: int) -> None:
    """intake 前以 Manager 身分預檢 probe repo 的遠端前置條件（runbook §3）。

    這些條件若不成立，canary 會在燒掉 plan／build 之後才於 ship 失敗，而且失敗原因
    只剩一個 needs_human。Copilot code review 的可用性沒有公開 API 可在開 PR 前確認，
    由 runbook 列為前置條件；它不成立時 `_full_dispatch` 會帶出 review gate 的
    blocking reason。
    """

    status, repo = _manager_gh_api(f"repos/{repository}")
    if status != 0 or not isinstance(repo, Mapping):
        raise QualificationFailure(
            "Manager GitHub account cannot read the probe repository"
        )
    permissions = repo.get("permissions")
    problems: list[str] = []
    if repo.get("archived") is not False:
        problems.append("repository is archived")
    if repo.get("default_branch") != PROBE_DEFAULT_BRANCH:
        problems.append(f"default branch is not {PROBE_DEFAULT_BRANCH}")
    if repo.get("has_issues") is not True:
        problems.append("issues are disabled")
    if repo.get("allow_merge_commit") is not True:
        problems.append("merge commits are not allowed (Cortex merges with --merge)")
    if not isinstance(permissions, Mapping) or permissions.get("push") is not True:
        problems.append("Manager account lacks push permission")
    status, issue_payload = _manager_gh_api(f"repos/{repository}/issues/{issue}")
    if status != 0 or not isinstance(issue_payload, Mapping):
        problems.append(f"issue #{issue} is not readable")
    elif "pull_request" in issue_payload or issue_payload.get("state") != "open":
        problems.append(f"#{issue} is not an open issue")
    from urllib.parse import quote

    for label in _production_pr_labels(repository, work_id, issue):
        status, _label = _manager_gh_api(
            f"repos/{repository}/labels/{quote(label, safe='')}"
        )
        if status != 0:
            problems.append(f"PR label {label!r} does not exist")
    # 規則集（任何有讀權的帳號都讀得到）與 classic branch protection（需要 admin；
    # 讀不到代表未設或無權，皆不當成違規）。approving review 規則會讓 Cortex 的
    # `gh pr merge --match-head-commit` 永遠卡住。
    status, rules = _manager_gh_api(
        f"repos/{repository}/rules/branches/{PROBE_DEFAULT_BRANCH}"
    )
    if status == 0 and isinstance(rules, list):
        for rule in rules:
            parameters = rule.get("parameters") if isinstance(rule, Mapping) else None
            if (
                isinstance(rule, Mapping)
                and rule.get("type") == "pull_request"
                and isinstance(parameters, Mapping)
                and int(parameters.get("required_approving_review_count") or 0) > 0
            ):
                problems.append("a ruleset requires approving reviews on main")
                break
    status, reviews = _manager_gh_api(
        f"repos/{repository}/branches/{PROBE_DEFAULT_BRANCH}"
        "/protection/required_pull_request_reviews"
    )
    if (
        status == 0
        and isinstance(reviews, Mapping)
        and int(reviews.get("required_approving_review_count") or 0) > 0
    ):
        problems.append("branch protection requires approving reviews on main")
    if problems:
        raise QualificationFailure(
            "probe repository prerequisites are not met: " + "; ".join(problems)
        )


def _installed_policy_check_version(interpreter: str) -> str:
    code = (
        "import importlib.metadata as m, policy_check.preflight\n"
        "print(m.version('policy-check'))\n"
    )
    account = "cortex-manager"
    result = _run(
        (interpreter, "-c", code), user=account, env=_account_env(account), timeout=60
    )
    if result.returncode != 0:
        raise QualificationFailure(
            f"policy-check is not importable by the Manager through {interpreter}"
        )
    return result.stdout.strip()


def _probe_runtime_prerequisites() -> None:
    """intake 前確認 image／部署 venv 已提供 probe 派工會用到的釘版本工具。"""

    gate = "cortex-gate"
    pytest_version = _run(
        ("python3", "-m", "pytest", "--version"),
        user=gate,
        env=_account_env(gate),
        timeout=60,
    )
    pytest_text = pytest_version.stdout + pytest_version.stderr
    if (
        pytest_version.returncode != 0
        or f"pytest {PROBE_GATE_PYTEST_VERSION}" not in pytest_text
    ):
        raise QualificationFailure(
            "gate identity cannot run the pinned system pytest "
            f"{PROBE_GATE_PYTEST_VERSION} (PSC_GATE_CMD_PYTEST would fail every card)"
        )
    expected = WHEELS["policy_check"]["version"]
    # 部署 venv：PSC_PREFLIGHT_CMD 的 backend；系統層：Manager 以相對名 `python3 -m
    # policy_check` 跑的 preflight policy stage 與 archive gate。
    for interpreter in ("/opt/cortex/venv/bin/python3", "/usr/bin/python3"):
        installed = _installed_policy_check_version(interpreter)
        if installed != expected:
            raise QualificationFailure(
                f"{interpreter} has policy-check {installed!r}, expected {expected}"
            )
    account = "cortex-manager"
    ctags = _run(
        ("ctags", "--version"), user=account, env=_account_env(account), timeout=30
    )
    if ctags.returncode != 0 or "Universal Ctags" not in ctags.stdout:
        raise QualificationFailure(
            "universal-ctags is unavailable to the Manager (policy-check R-22 needs it)"
        )


@dataclass(frozen=True)
class ProbeSourceLayout:
    """已安裝 plan 導出的 probe 落點：state root、來源 repo 容器與既有 slug。"""

    state_root: Path
    source_root: Path
    installed_slugs: frozenset[str]


def _plan_probe_layout(receipt: Mapping[str, Any]) -> ProbeSourceLayout:
    """由已安裝 plan 的 `roots.state` 與 `repo-source-tree` 資產導出 Manager 的來源 repo 容器。"""

    plan = receipt.get("plan")
    assets = plan.get("assets") if isinstance(plan, Mapping) else None
    slugs = plan.get("source_repositories") if isinstance(plan, Mapping) else None
    roots = plan.get("roots") if isinstance(plan, Mapping) else None
    state = roots.get("state") if isinstance(roots, Mapping) else None
    matches = [
        asset
        for asset in (assets if isinstance(assets, list) else [])
        if isinstance(asset, Mapping) and asset.get("asset_id") == "repo-source-tree"
    ]
    if len(matches) != 1 or not isinstance(slugs, list):
        raise QualificationFailure("plan must declare exactly one repo-source-tree")
    raw = matches[0].get("path")
    if (
        not isinstance(raw, str)
        or not raw.startswith("/")
        or not isinstance(state, str)
        or not state.startswith("/")
        or matches[0].get("is_directory") is not True
    ):
        raise QualificationFailure("repo-source-tree asset shape is invalid")
    state_root = Path(state)
    root = Path(raw)
    if (
        root.is_symlink()
        or not root.is_dir()
        or root.resolve() != root
        or root.parent != state_root
        or root.stat().st_uid != _manager_uid()
    ):
        raise QualificationFailure("repo-source-tree is absent, unsafe, or not Manager-owned")
    return ProbeSourceLayout(
        state_root=state_root,
        source_root=root,
        installed_slugs=frozenset(str(slug) for slug in slugs),
    )


def _render_project_config(name: str, path: Path) -> str:
    """`project-cortex.yaml` 的 workspace 列；形狀與 `cortex install service` 寫的相同。"""

    return (
        "workspaces:\n"
        f"  - name: {json.dumps(name)}\n"
        f"    path: {json.dumps(str(path))}\n"
        "    exact_project: true\n"
    )


def _install_root_file(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(temporary, 0, 0)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


_PROBE_RESOLUTION_CODE = r"""
import json
import sys

from paulsha_cortex.coordinator.work_bridge import resolve_trusted_repo_root
from paulsha_cortex.monitor.config import load_config

config = load_config()
print(json.dumps({
    "workspaces": [str(row.path) for row in config.workspaces],
    "resolved": str(resolve_trusted_repo_root(sys.argv[1])),
}))
""".strip()


def _register_probe_checkout(*, receipt: Mapping[str, Any], repository: str) -> Path:
    """以 Manager 身分 clone probe repo 並登記成 Monitor／Manager 認得的 project。

    - 落點：已安裝 plan 的 `repo-source-tree`（`<state>/repos/<name>`），與 installer 放
      受治理 repo 的位置同一個 Manager-owned 容器，job 帳號的唯讀 default ACL 自動繼承。
    - clone 走 Manager 的 root-owned gitconfig（gh credential helper）與 egress proxy，
      與 `_manager_github_probe` 同一條傳輸路徑。
    - 登記：寫 `PSC_PROJECT_CONFIG_ROOT/project-cortex.yaml` 的 exact-project
      workspace——Monitor 的 `load_config()` 與 Manager 的 `resolve_trusted_repo_root()`
      都讀這份設定；不碰 coordinator registry 或 snapshot。寫完以 installed runtime 驗證
      Manager 會把 `owner/name` 恰好解析到這份 checkout，再重啟 Monitor 讓它重讀設定。
    """

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise QualificationFailure("protected GitHub probe repository is missing or invalid")
    layout = _plan_probe_layout(receipt)
    name = repository.split("/", 1)[1]
    if (
        PROBE_CHECKOUT_NAME.fullmatch(name) is None
        or name in {".", ".."}
        or name in layout.installed_slugs
    ):
        raise QualificationFailure("probe repository name cannot be a Manager checkout name")
    checkout = layout.source_root / name
    if checkout.exists() or checkout.is_symlink():
        raise QualificationFailure("probe checkout already exists in the Manager source tree")
    runtime_env = _installed_runtime_env()
    config_root = Path(runtime_env.get("PSC_PROJECT_CONFIG_ROOT", ""))
    project_config = config_root / PROJECT_CONFIG_NAME
    if (
        not config_root.is_absolute()
        or config_root.is_symlink()
        or not config_root.is_dir()
        or not config_root.is_relative_to(layout.state_root)
    ):
        raise QualificationFailure("installed project config root is unavailable or unsafe")
    if os.path.lexists(project_config):
        raise QualificationFailure(
            "project-cortex.yaml already exists; refusing to merge into operator config"
        )

    account = "cortex-manager"
    env = _account_env(account)
    _require_installed_manager_gitconfig(Path(env["HOME"]) / ".gitconfig")
    remote = f"https://github.com/{repository}.git"
    _require_success(
        _run(
            ("/usr/bin/git", "clone", "--quiet", "--", remote, str(checkout)),
            user=account,
            env=env,
            timeout=300,
        ),
        "Manager probe repository clone",
    )
    origin = _run(
        ("/usr/bin/git", "-C", str(checkout), "remote", "get-url", "origin"),
        user=account,
        env=env,
    )
    branch = _run(
        ("/usr/bin/git", "-C", str(checkout), "symbolic-ref", "--short", "HEAD"),
        user=account,
        env=env,
    )
    if origin.stdout.strip() != remote or branch.stdout.strip() != PROBE_DEFAULT_BRANCH:
        raise QualificationFailure("probe checkout origin or default branch is unexpected")
    for key in ("name", "email"):
        _require_success(
            _run(
                (
                    "/usr/bin/git",
                    "-C",
                    str(checkout),
                    "config",
                    "--local",
                    f"user.{key}",
                    CANARY_GIT_IDENTITY[key],
                ),
                user=account,
                env=env,
            ),
            f"probe checkout user.{key}",
        )

    _install_root_file(project_config, _render_project_config(name, checkout))
    resolution = _run(
        ("/opt/cortex/venv/bin/python", "-c", _PROBE_RESOLUTION_CODE, repository),
        user=account,
        env=_account_runtime_env(account),
        timeout=60,
    )
    try:
        _require_success(resolution, "installed runtime probe repository resolution")
        records = _json_records(resolution.stdout)
        resolved = records[-1] if records else None
        if (
            not isinstance(resolved, Mapping)
            or resolved.get("resolved") != str(checkout)
            or str(checkout) not in (resolved.get("workspaces") or [])
        ):
            raise QualificationFailure(
                "installed runtime does not resolve the probe repository to its checkout"
            )
    except QualificationFailure:
        project_config.unlink(missing_ok=True)
        raise
    _require_success(
        _run(("systemctl", "restart", "cortex-monitor.service"), timeout=90),
        "Monitor restart after probe registration",
    )
    _require_success(
        _run(("systemctl", "is-active", "cortex-monitor.service")),
        "Monitor active after probe registration",
    )
    return checkout


def _probe_policy_version(checkout: Path) -> None:
    """probe 的 `policy_version` 必須等於已安裝的 policy-check（引擎 --offline 會驗）。"""

    from paulsha_cortex.project_policy import ProjectPolicyError, resolve_project_policy

    try:
        resolution = resolve_project_policy(checkout)
    except ProjectPolicyError as exc:
        raise QualificationFailure("probe project policy is unreadable") from exc
    payload = resolution.payload if isinstance(resolution.payload, Mapping) else {}
    declared = str(payload.get("policy_version") or "").strip()
    expected = WHEELS["policy_check"]["version"]
    if declared != expected:
        raise QualificationFailure(
            f"probe policy_version {declared!r} does not match installed policy-check {expected}"
        )


_PROBE_AUTHORITY_CODE = r"""
import json
import sys

from paulsha_cortex.coordinator.claim import load_work_authority

try:
    authority = load_work_authority(repo=sys.argv[1], work_id=sys.argv[2])
except ValueError as exc:
    reason = getattr(exc, "reason_code", None) or str(exc)
    print(json.dumps({"ok": False, "reason": str(reason)[:300]}))
    raise SystemExit(0)
print(json.dumps({
    "ok": True,
    "mapped_issues": list(authority.mapped_issues),
    "mapped_openspec": list(authority.mapped_openspec),
}))
""".strip()


def _wait_for_probe_authority(
    *,
    repository: str,
    work_id: str,
    issue: int,
    timeout: int = PROBE_AUTHORITY_TIMEOUT_SECONDS,
) -> None:
    """等 Monitor snapshot 產生 intake 會採信的 confirmed authority 再進件。

    判準就是 intake 自己用的 `load_work_authority()`（含 GitHub provider 的 durable
    snapshot），而且 `--issue N` 必須已在 `mapped_issues`：intake 雖會寫 override link，
    但同一次呼叫不會重新採信。
    """

    account = "cortex-manager"
    deadline = time.monotonic() + timeout
    last = "no Monitor snapshot yet"
    while True:
        result = _run(
            ("/opt/cortex/venv/bin/python", "-c", _PROBE_AUTHORITY_CODE, repository, work_id),
            user=account,
            env=_account_runtime_env(account),
            timeout=60,
        )
        records = _json_records(result.stdout)
        state = records[-1] if records else None
        if result.returncode != 0 or not isinstance(state, Mapping):
            tail = (result.stderr.strip().splitlines() or [""])[-1][:200]
            last = f"authority probe failed rc={result.returncode}: {tail}".rstrip(": ")
        elif state.get("ok") is not True:
            last = str(state.get("reason") or "authority unavailable")
        elif issue not in (state.get("mapped_issues") or []):
            last = (
                f"issue #{issue} is not linked in .cortex/work-items.yaml on "
                f"{PROBE_DEFAULT_BRANCH}"
            )
        else:
            return
        if time.monotonic() >= deadline:
            raise QualificationFailure(
                f"Monitor did not publish a confirmed {repository}/{work_id} authority "
                f"within {timeout}s: {last}"
            )
        time.sleep(PROBE_AUTHORITY_POLL_SECONDS)


def _prepare_probe_dispatch(
    *,
    receipt: Mapping[str, Any],
    repository: str,
    work_id: str,
    issue: int,
) -> None:
    """intake 前的 probe 準備：遠端預檢 → 本機工具預檢 → clone／登記 → 等 authority。"""

    if WORK_ID.fullmatch(work_id) is None or issue <= 0:
        raise QualificationFailure("protected full-dispatch work identity is missing")
    _probe_repository_prerequisites(repository, work_id, issue)
    _probe_runtime_prerequisites()
    checkout = _register_probe_checkout(receipt=receipt, repository=repository)
    _probe_policy_version(checkout)
    _wait_for_probe_authority(repository=repository, work_id=work_id, issue=issue)


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


def _artifact_row(
    path: Path, *, state_root: Path, observed_sha256: str
) -> dict[str, str]:
    try:
        relative = path.relative_to(state_root).as_posix()
    except ValueError as exc:
        raise QualificationFailure(
            "dispatch artifact escapes Cortex state root"
        ) from exc
    if SHA256.fullmatch(observed_sha256) is None:
        raise QualificationFailure("dispatch artifact observation hash is invalid")
    return {"path": relative, "sha256": observed_sha256}


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


def _shell_segments(command: str) -> list[list[str]]:
    """Return simple command segments, recursively unwrapping a shell -c."""

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as exc:
        raise QualificationFailure("Codex agent-loop command is malformed") from exc
    if (
        len(tokens) == 3
        and Path(tokens[0]).name in {"bash", "sh"}
        and tokens[1] in {"-c", "-lc"}
    ):
        return _shell_segments(tokens[2])
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in {";", "&&", "||", "|"}:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _is_expected_head_probe(
    command: str, *, expected_worktree: str
) -> bool:
    """Accept only one exact Git invocation, optionally in Codex's shell envelope.

    The job spec already fixes ``working_directory`` and Codex ``-C`` to the
    Manager-owned worktree. Requiring the absolute system Git path prevents a
    repository-local ``./git`` or PATH substitution, while exact argv equality
    rejects pipes, boolean fallbacks, redirections, aliases, and suffixes. The
    Codex CLI serializes shell-tool executions as a three-argument Bash ``-c``/``-lc``
    argv; only that exact outer shape is unwrapped once.
    """

    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return False
    expected = (
        ["/usr/bin/git", "rev-parse", "HEAD"],
        ["/usr/bin/git", "-C", expected_worktree, "rev-parse", "HEAD"],
    )
    if argv in expected:
        return True
    if (
        len(argv) != 3
        or argv[0] not in {"/bin/bash", "/usr/bin/bash"}
        or argv[1] not in {"-c", "-lc"}
    ):
        return False
    try:
        inner_argv = shlex.split(argv[2], posix=True)
    except ValueError:
        return False
    return inner_argv in expected


def _codex_agent_loop_thread_id(
    logs: Sequence[tuple[str, bytes]],
) -> str:
    thread_ids: set[str] = set()
    for _job_id, content in logs:
        try:
            lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise QualificationFailure("Codex agent-loop log is not UTF-8") from exc
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            thread_id = event.get("thread_id") if isinstance(event, dict) else None
            if (
                isinstance(event, dict)
                and event.get("type") == "thread.started"
                and isinstance(thread_id, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}", thread_id)
            ):
                thread_ids.add(thread_id)
    if len(thread_ids) != 1:
        raise QualificationFailure(
            "Codex agent-loop has no unique provider thread identity"
        )
    return next(iter(thread_ids))


def _codex_agent_loop_observation(
    logs: Sequence[tuple[str, bytes]],
    *,
    expected_head: str,
    expected_worktree: str,
) -> dict[str, object]:
    """Derive non-secret live evidence from Codex JSONL command events.

    The builder log is observational telemetry written by the job, not an
    independent authority surface.  Closeout still binds it to Manager-owned
    workflow identity and records only hashes/booleans in uploaded evidence.
    """

    if len(logs) != 1 or SHA40.fullmatch(expected_head) is None:
        raise QualificationFailure("Codex agent-loop log/head binding is not unique")
    commands: list[str] = []
    outputs: list[str] = []
    builder_job_ids: list[str] = []
    raw_log_sha256 = ""
    for job_id, content in logs:
        if not isinstance(job_id, str) or not job_id or not isinstance(content, bytes):
            raise QualificationFailure("Codex agent-loop log binding is malformed")
        builder_job_ids.append(job_id)
        raw_log_sha256 = hashlib.sha256(content).hexdigest()
        try:
            lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise QualificationFailure("Codex agent-loop log is not UTF-8") from exc
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if (
                not isinstance(item, dict)
                or event.get("type") != "item.completed"
                or item.get("type") != "command_execution"
                or item.get("status") != "completed"
                or type(item.get("exit_code")) is not int
                or item.get("exit_code") != 0
                or not isinstance(item.get("command"), str)
                or not item["command"].strip()
                or not isinstance(item.get("aggregated_output"), str)
                or not item["aggregated_output"].strip()
            ):
                continue
            output_lines = [
                line.strip()
                for line in item["aggregated_output"].splitlines()
                if line.strip()
            ]
            if not _is_expected_head_probe(
                item["command"], expected_worktree=expected_worktree
            ) or output_lines != [expected_head]:
                continue
            commands.append(item["command"])
            outputs.append(item["aggregated_output"])
            if len(commands) > MAX_AGENT_LOOP_COMMANDS:
                raise QualificationFailure(
                    "Codex agent-loop command observation exceeds the evidence bound"
                )
    if not commands:
        raise QualificationFailure(
            "Codex agent-loop has no completed git HEAD proof for the bound worktree"
        )
    thread_id = _codex_agent_loop_thread_id(logs)
    return {
        "schema_version": 1,
        "executor": DEPLOYMENT_CANARY_BUILDER_EXECUTOR,
        "model_id": DEPLOYMENT_CANARY_BUILDER_MODEL,
        "card_id": DEPLOYMENT_CANARY_PROBE_CARD,
        "builder_job_ids": sorted(builder_job_ids),
        "successful_command_count": len(commands),
        "all_outputs_nonempty": True,
        "command_sha256": hashlib.sha256(_canonical_bytes(commands)).hexdigest(),
        "output_sha256": hashlib.sha256(_canonical_bytes(outputs)).hexdigest(),
        "log_sha256": raw_log_sha256,
        "thread_sha256": hashlib.sha256(thread_id.encode()).hexdigest(),
    }


def _bound_codex_builder_log(
    root: Path, job: Mapping[str, object]
) -> tuple[Path, bytes]:
    slot = job.get("template_instance")
    if (
        not isinstance(slot, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", slot) is None
    ):
        raise QualificationFailure("Codex agent-loop template instance is invalid")
    expected = root / "commit-spool" / "build-logs" / slot / "job.jsonl"
    value = job.get("log_path")
    path = Path(value) if isinstance(value, str) else Path()
    if not path.is_absolute() or path != expected:
        raise QualificationFailure("Codex agent-loop log path is not the bound spool")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise QualificationFailure("Codex agent-loop log escapes coordinator root") from exc
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise QualificationFailure("Codex agent-loop log path contains a symlink")
    if not path.is_file():
        raise QualificationFailure("Codex agent-loop log is absent")
    metadata = path.stat()
    if metadata.st_uid != _manager_uid() or stat.S_IMODE(metadata.st_mode) & 0o007:
        raise QualificationFailure("Codex agent-loop log ownership/mode is unsafe")
    if metadata.st_size > MAX_AGENT_LOOP_LOG_BYTES:
        raise QualificationFailure("Codex agent-loop log exceeds the evidence bound")
    try:
        return path, path.read_bytes()
    except OSError as exc:
        raise QualificationFailure("Codex agent-loop log is unreadable") from exc


def _bound_codex_builder_spec(
    root: Path,
    job: Mapping[str, object],
    workflow: Mapping[str, object],
) -> tuple[Path, Path, str]:
    """Validate the Manager-authored launch contract for the observed job."""

    slot = job.get("template_instance")
    job_id = job.get("job_id")
    if (
        not isinstance(slot, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", slot) is None
        or not isinstance(job_id, str)
        or not job_id
    ):
        raise QualificationFailure("Codex agent-loop job spec binding is invalid")
    path = root / "job-specs" / "builder" / f"{slot}.json"
    content = _manager_file(path, label="Codex agent-loop job spec", root=root)
    spec = _json_object(
        content,
        label="Codex agent-loop job spec",
    )
    spec_env = spec.get("env")
    command = spec.get("command")
    worktree = job.get("worktree")
    repo_root = job.get("workflow_repo_root")
    log_path = job.get("log_path")
    unit = spec.get("unit")
    if (
        set(spec) != set(job_runner.SPEC_REQUIRED_KEYS)
        or spec.get("spec_version") != job_runner.JOB_SPEC_VERSION
        or spec.get("instance") != slot
        or spec.get("job_id") != job_id
        or not isinstance(unit, str)
        or re.fullmatch(
            rf"cortex-job(?:-jit)?@{re.escape(slot)}\.service", unit
        )
        is None
        or not isinstance(worktree, str)
        or not Path(worktree).is_absolute()
        or repo_root != worktree
        or spec.get("working_directory") != worktree
        or spec.get("log_path") != log_path
        or not isinstance(spec_env, dict)
        or not isinstance(command, list)
        or len(command) != 3
        or command[:2] != ["bash", "-c"]
        or not isinstance(command[2], str)
    ):
        raise QualificationFailure("Codex agent-loop job spec authority mismatch")
    surface = writable_surface("builder-codex-home")
    expected_codex_home = spool_slot.exact_job_slot(
        surface.surface_id,
        slot,
        writable_root=root.parent / surface.coordinator_relative,
    )
    try:
        relative_codex_home = expected_codex_home.relative_to(root.parent)
    except ValueError as exc:
        raise QualificationFailure("Codex agent-loop runtime home escapes state root") from exc
    cursor = root.parent
    for part in relative_codex_home.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise QualificationFailure("Codex agent-loop runtime home contains a symlink")
    if (
        spec_env.get("CODEX_HOME") != str(expected_codex_home)
        or spec_env.get("PATH") != DEPLOYMENT_CANARY_BUILDER_PATH
        or expected_codex_home.is_symlink()
        or not expected_codex_home.is_dir()
        or expected_codex_home.stat().st_uid != _manager_uid()
    ):
        raise QualificationFailure(
            "Codex agent-loop runtime home is not the exact Manager-owned job slot"
        )
    try:
        job_runner.reject_unsafe_env(
            spec_env, source="qualification._bound_codex_builder_spec"
        )
    except job_runner.JobRunnerError as exc:
        raise QualificationFailure(
            "Codex agent-loop job environment is unsafe"
        ) from exc
    if job_runner.git_config_safe_directories(spec_env) != (worktree,):
        raise QualificationFailure(
            "Codex agent-loop git safe.directory is not the exact bound worktree"
        )
    segments = _shell_segments(command[2])
    if not segments or len(segments[0]) < 4:
        raise QualificationFailure("Codex agent-loop job command identity is invalid")
    prompt = segments[0][3]
    if prompt != _expected_worktree_isolation_prompt(job, workflow):
        raise QualificationFailure("Codex agent-loop job prompt is invalid")
    expected_last_message = Path(log_path).with_name(
        f"{Path(log_path).stem}.last.json"
    )
    expected_argv = [
        "codex",
        "exec",
        "--ignore-user-config",
        prompt,
        "--json",
        *_codex_canary_builder_sandbox_argv(),
        "--model",
        DEPLOYMENT_CANARY_BUILDER_MODEL,
        "-c",
        f'model_reasoning_effort="{PROVIDERS["codex"][1]}"',
        "-o",
        str(expected_last_message),
        "-C",
        worktree,
    ]
    bundle = root / "commit-spool" / slot / "commits.bundle"
    part = Path(f"{bundle}.part")
    quoted_worktree = shlex.quote(worktree)
    quoted_bundle = shlex.quote(str(bundle))
    quoted_part = shlex.quote(str(part))
    bundle_segment = (
        f"git -C {quoted_worktree} bundle create {quoted_part} "
        f'"$(git -C {quoted_worktree} symbolic-ref HEAD)" ^refs/cortex/base '
        f"&& chmod 0644 {quoted_part} && mv -f {quoted_part} {quoted_bundle}"
    )
    quoted_last = shlex.quote(str(expected_last_message))
    publish_last = (
        f"{{ [ -f {quoted_last} ] && chmod 0644 {quoted_last}; }} "
        "2>/dev/null || :"
    )
    expected_script = "; ".join(
        (
            shlex.join(expected_argv),
            "__psc_rc=$?",
            bundle_segment,
            publish_last,
            'exit "$__psc_rc"',
        )
    )
    if command[2] != expected_script:
        raise QualificationFailure("Codex agent-loop job command identity is invalid")
    return path, expected_codex_home, hashlib.sha256(content).hexdigest()


def _worktree_isolation_terminal_schema(run_id: str) -> dict[str, object]:
    """Exact command-free terminal contract accepted by the live probe."""

    return {
        "kind": "workflow-card",
        "schema_version": 2,
        "required": [
            "schema_version",
            "kind",
            "status",
            "run_id",
            "card_id",
            "candidate",
            "outputs",
            "diagnostics",
            "gate_evidence",
        ],
        "fixed": {
            "schema_version": 2,
            "kind": "workflow-card",
            "run_id": run_id,
            "card_id": DEPLOYMENT_CANARY_PROBE_CARD,
            "outputs": [],
        },
        "status": ["passed", "failed", "needs_human"],
        "status_policy": (
            "Report passed only when this card's own action is genuinely complete. "
            "Natural-language confidence, an exit code of 0, and the absence of an "
            "explicit error do NOT authorize passed; if the action is not complete, or "
            "the decision needs a human, report failed or needs_human instead. Scope "
            "discipline: this card's test_policy requires no test run from you, and the "
            "Manager has declared no gate commands either, so no deterministic gate "
            "result is expected from either side. Report the status you actually "
            "observed, keep the concrete detail in diagnostics, and leave gate_evidence "
            "exactly []."
        ),
        "outputs": {
            "type": "array",
            "items": "repo-relative artifact path string matching declared_outputs",
            "must_match_every_declared_output": True,
            "descriptive_objects_forbidden": True,
        },
        "diagnostics": {
            "type": "object",
            "description": (
                "Structured, machine-readable context for this terminal. Required for "
                "every status; put the concrete failure detail here when reporting "
                "failed or needs_human instead of burying it in prose."
            ),
        },
        "gate_evidence": {
            "type": "array",
            "items": {
                "name": "one of allowed_names below",
                "status": "passed | failed",
            },
            "allowed_names": [],
            "description": (
                "This card's test_policy requires no test run from you, so there is no "
                "gate name for you to claim and gate_evidence must be exactly []. A name "
                "of your own invention fails the card closed; put whatever you observed "
                "into diagnostics instead."
            ),
        },
    }


def _expected_worktree_isolation_prompt(
    job: Mapping[str, object], workflow: Mapping[str, object]
) -> str:
    """Reconstruct the only Manager prompt that qualifies as autonomous."""

    run_id = workflow.get("run_id")
    work_id = workflow.get("work_id")
    repository = workflow.get("repo")
    source_revision = job.get("source_revision")
    if not all(
        isinstance(value, str) and value
        for value in (run_id, work_id, repository, source_revision)
    ):
        raise QualificationFailure("Codex agent-loop prompt authority is incomplete")
    contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "workflow-card-prompt",
        "run_id": run_id,
        "work_id": work_id,
        "repo": repository,
        "source_revision": source_revision,
        "phase": "build",
        "card_id": DEPLOYMENT_CANARY_PROBE_CARD,
        "persona": "builder",
        "inputs": [],
        "source_material": [],
        "declared_outputs": [],
        "candidate": None,
        "skill_ref": "superpowers:using-git-worktrees",
        "action": (
            "Confirm the Manager-provisioned worktree; do not create a second worktree."
        ),
        "commit_policy": "forbidden",
        "test_policy": "none",
        "terminal_schema": _worktree_isolation_terminal_schema(run_id),
    }
    refs = workflow.get("openspec_refs", [])
    if refs not in (None, [], ()):
        if (
            not isinstance(refs, (list, tuple))
            or not refs
            or not isinstance(refs[0], str)
            or not refs[0]
        ):
            raise QualificationFailure("Codex agent-loop OpenSpec authority is invalid")
        contract["openspec_ref"] = refs[0]
    return (
        job_runner.WORKTREE_ISOLATION_AUTONOMOUS_PREAMBLE
        + " Contract: "
        + json.dumps(contract, ensure_ascii=False, sort_keys=True)
    )


_DIAGNOSTIC_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _diagnostic_token(value: object) -> str:
    """只輸出短的列舉型字串；其他型別或內容一律遮成型別名（不洩漏 detail 文字）。"""

    if value is None:
        return "none"
    if isinstance(value, str) and _DIAGNOSTIC_TOKEN.fullmatch(value):
        return value
    return f"<{type(value).__name__}>"


def _closeout_diagnostic(
    workflow: Mapping[str, object],
    *,
    steps: object,
    phase_chain: list[object],
    required_phases: tuple[str, ...],
    candidate: object,
    repository: str,
    issue: int,
) -> str:
    """#716：closeout 失敗時列出不成立的條件，讓 canary log 可以直接定位停在哪。"""

    parts = [
        f"current_phase={_diagnostic_token(workflow.get('current_phase'))}",
        f"status={_diagnostic_token(workflow.get('status'))}",
        f"gate_status={_diagnostic_token(workflow.get('gate_status'))}",
    ]
    facets = workflow.get("facets")
    if facets not in ([], ()):
        tokens = [_diagnostic_token(item) for item in facets] if isinstance(facets, (list, tuple)) else [_diagnostic_token(facets)]
        parts.append("facets=" + ",".join(tokens))
    reason = workflow.get("needs_human_reason")
    if isinstance(reason, Mapping):
        parts.append(f"needs_human={_diagnostic_token(reason.get('reason'))}")
    elif reason is not None:
        parts.append(f"needs_human={_diagnostic_token(reason)}")
    missing = [phase for phase in required_phases if phase not in phase_chain]
    if missing:
        parts.append("missing_phases=" + ",".join(missing))
    if isinstance(steps, list):
        failed = [
            f"{_diagnostic_token(row.get('phase'))}:{_diagnostic_token(row.get('card'))}"
            f"={_diagnostic_token(row.get('gate_result'))}"
            for row in steps
            if isinstance(row, Mapping) and row.get("gate_result") != "passed"
        ]
        if failed:
            parts.append("failed_steps=" + ";".join(failed))
    if not isinstance(candidate, str) or SHA40.fullmatch(candidate) is None:
        parts.append("candidate=invalid")
    elif workflow.get("verified_head") != candidate:
        parts.append("verified_head=mismatch")
    issue_refs = workflow.get("issue_refs")
    if not isinstance(issue_refs, list) or f"{repository}#{issue}" not in issue_refs:
        parts.append("issue_refs=unbound")
    return " ".join(parts)


def _validate_dispatch_closeout(
    *,
    repository: str,
    work_id: str,
    issue: int,
    terminal: object,
    coordinator_root: Path,
) -> tuple[list[str], list[dict[str, str]], dict[str, Any], dict[str, object]]:
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
        or not isinstance(candidate, str)
        or SHA40.fullmatch(candidate) is None
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
            "workflow terminal phase chain or candidate binding is invalid: "
            + _closeout_diagnostic(
                workflow,
                steps=steps,
                phase_chain=phase_chain,
                required_phases=required_phases,
                candidate=candidate,
                repository=repository,
                issue=issue,
            )
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

    artifact_digests: dict[Path, str] = {}

    def remember_artifact(path: Path, digest: str) -> None:
        if SHA256.fullmatch(digest) is None:
            raise QualificationFailure("dispatch artifact observation hash is invalid")
        previous = artifact_digests.get(path)
        if previous is not None and previous != digest:
            raise QualificationFailure(
                "dispatch artifact changed between authority observations"
            )
        artifact_digests[path] = digest

    remember_artifact(registry_path, hashlib.sha256(registry_content).hexdigest())
    verdict_seen = False
    ledgers_seen = 0
    # #1096：foreign-review 這個 delivery gate 唯一可信的綁定來源，就是本迴圈稍後
    # 對 review job 已獨立驗過 run_id／repo／candidate／reviewer_job_id 的那份
    # workflow canonical evidence；記住它的 path＋hash，讓 gate_refs 段落能要求
    # 「foreign-review 引用的必須逐字是這一份」，不接受他 run／舊 candidate 的證據。
    review_evidence_locator: tuple[Path, str] | None = None
    for job in bound_jobs:
        phase = job.get("workflow_phase")
        if (
            job.get("workflow_repo") != repository
            or job.get("status") != "exited"
            or job.get("exit_code") != 0
            or phase not in {"plan", "build", "verify", "review", "ship"}
        ):
            raise QualificationFailure("workflow job authority binding is invalid")
        envelope, evidence_path, evidence_digest = _bound_relative_json(
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
            review_evidence_locator = (evidence_path, evidence_digest)
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
            output_digest = _sha256(output) if output.is_file() else ""
            if (
                output.is_symlink()
                or not output.is_file()
                or output_digest != row.get("sha256")
            ):
                raise QualificationFailure(
                    "workflow canonical artifact is absent or drifted"
                )
            remember_artifact(output, output_digest)
        remember_artifact(evidence_path, evidence_digest)
        if phase != "ship":
            control_value = job.get("control_log_path") or job.get("log_path")
            if not isinstance(control_value, str) or not control_value:
                raise QualificationFailure(
                    "workflow Manager control log binding is absent"
                )
            control = Path(control_value)
            ledger_path = control.with_name(f"{control.stem}.gates.json")
            ledger_content = _manager_file(
                ledger_path, label="workflow gate ledger", root=root
            )
            ledger = _json_object(
                ledger_content,
                label="workflow gate ledger",
            )
            ledger_gates = ledger.get("gates")
            if (
                ledger.get("schema_version") != 1
                or ledger.get("kind") != "workflow-gate-ledger"
                # #1096：`slice_id` 綁定這份 ledger 是哪個 job 的，不再只驗型別——
                # 他 job／舊 ledger 只要型別是字串就會被目前的檢查放行。
                or ledger.get("slice_id") != job.get("job_id")
                or not isinstance(ledger_gates, list)
            ):
                raise QualificationFailure("workflow gate ledger schema is invalid")
            # #1096：ledger 過去只驗外層形狀，`gates` 列表內容（有沒有預期的 gate、
            # 是否 passed）完全沒人看——回歸把 pytest gate 跳過、或跳過後仍讓
            # workflow 宣告 passed，都會被目前的檢查放行。這裡逐項驗證每個 gate
            # 條目形狀合法，並要求部署層宣告的每個 gate 名稱
            # （`DEPLOYMENT_CANARY_EXPECTED_GATE_NAMES`）都存在且為 terminal passed。
            observed_gate_status: dict[str, str] = {}
            for gate_row in ledger_gates:
                if (
                    not isinstance(gate_row, dict)
                    or not isinstance(gate_row.get("name"), str)
                    or not gate_row["name"]
                    or not isinstance(gate_row.get("status"), str)
                ):
                    raise QualificationFailure(
                        "workflow gate ledger entry is malformed"
                    )
                observed_gate_status[gate_row["name"]] = gate_row["status"]
            missing_or_failed = sorted(
                name
                for name in DEPLOYMENT_CANARY_EXPECTED_GATE_NAMES
                if observed_gate_status.get(name) != "passed"
            )
            if missing_or_failed:
                raise QualificationFailure(
                    "workflow gate ledger is missing an expected passed gate: "
                    + ", ".join(missing_or_failed)
                )
            ledgers_seen += 1
            remember_artifact(
                ledger_path, hashlib.sha256(ledger_content).hexdigest()
            )
    if not verdict_seen or ledgers_seen == 0:
        raise QualificationFailure("workflow verdict or Manager gate ledger is absent")

    bundle_seen = False
    build_jobs = [job for job in bound_jobs if job.get("workflow_phase") == "build"]
    expected_builder = {
        "executor": DEPLOYMENT_CANARY_BUILDER_EXECUTOR,
        "model_id": DEPLOYMENT_CANARY_BUILDER_MODEL,
    }
    resolved_chain = workflow.get("resolved_model_chain")
    resolved_builder = (
        resolved_chain.get("builder") if isinstance(resolved_chain, dict) else None
    )
    if (
        workflow.get("model_chain_override") != {"builder": expected_builder}
        or not isinstance(resolved_builder, dict)
        or resolved_builder.get("executor") != expected_builder["executor"]
        or resolved_builder.get("model_id") != expected_builder["model_id"]
        or resolved_builder.get("independence_domain") != "openai"
        or resolved_builder.get("source") != "run-override"
        or resolved_builder.get("envelope_source") not in {"default", "measured"}
        or not build_jobs
    ):
        raise QualificationFailure(
            "Codex agent-loop workflow is not bound to the exact builder override"
        )
    for job in build_jobs:
        if (
            job.get("persona") != "builder"
            or job.get("executor") != DEPLOYMENT_CANARY_BUILDER_EXECUTOR
            or job.get("model_id") != DEPLOYMENT_CANARY_BUILDER_MODEL
            or job.get("runtime_principal") != "builder"
            or job.get("runtime_mode") != "systemd-template"
            or job.get("runtime_surface") != "builder-codex-home"
            or job.get("credential_publish") is not True
        ):
            raise QualificationFailure(
                "Codex agent-loop builder identity/runtime binding is invalid"
            )
    probe_jobs = [
        job
        for job in build_jobs
        if job.get("workflow_card") == DEPLOYMENT_CANARY_PROBE_CARD
    ]
    if len(probe_jobs) != 1:
        raise QualificationFailure(
            "Codex agent-loop has no unique worktree-isolation job binding"
        )
    bound_logs: list[tuple[str, bytes]] = []
    probe_codex_homes: list[Path] = []
    for job in probe_jobs:
        spec_path, codex_home, spec_digest = _bound_codex_builder_spec(
            root, job, workflow
        )
        log_path, log_content = _bound_codex_builder_log(root, job)
        remember_artifact(spec_path, spec_digest)
        remember_artifact(log_path, hashlib.sha256(log_content).hexdigest())
        bound_logs.append((str(job["job_id"]), log_content))
        probe_codex_homes.append(codex_home)
    probe_job = probe_jobs[0]
    probe_worktree = str(probe_job["worktree"])
    probe_candidate = probe_job.get("subject_head")
    if not isinstance(probe_candidate, str) or SHA40.fullmatch(probe_candidate) is None:
        raise QualificationFailure(
            "Codex agent-loop probe candidate binding is invalid"
        )
    agent_loop_probe = _codex_agent_loop_observation(
        bound_logs,
        expected_head=probe_candidate,
        expected_worktree=probe_worktree,
    )
    agent_loop_probe["probe_candidate_sha"] = probe_candidate
    agent_loop_probe.update(
        _codex_thread_runtime_identity(
            _codex_agent_loop_thread_id(bound_logs),
            expected_worktree=probe_worktree,
            codex_home=probe_codex_homes[0],
        )
    )
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
        bundle_digest_before = _sha256(bundle)
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
        bundle_digest_after = _sha256(bundle)
        if bundle_digest_after != bundle_digest_before:
            raise QualificationFailure(
                "build commit bundle changed while it was being validated"
            )
        bundle_seen = True
        remember_artifact(bundle, bundle_digest_before)
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
    remember_artifact(
        completion_path, hashlib.sha256(completion_content).hexdigest()
    )

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
        # #1096：evidence 過去只以 kind／path／hash 採信，證據內容從未被讀——他 run
        # 或舊 candidate 遺留的合法檔案，只要湊得出對應的 path＋hash 就能滿足
        # closeout。這裡把內容當 JSON 讀出來，凡是自報 run_id／work_id／candidate
        # 的欄位都必須與本次派工相符（欄位不存在則不強求，避免對未帶這些欄位的
        # 既有 evidence adapter 產生新的形狀假設）；`foreign-review` 另外強制要求
        # 逐字等於本 run 已獨立驗過的 review job workflow evidence（同一份
        # path＋hash），不接受任何「看起來合法」但不是那一份的檔案。
        evidence_payload = _json_object(content, label="workflow delivery gate")
        observed_run_id = evidence_payload.get("run_id")
        observed_work_id = evidence_payload.get("work_id")
        observed_candidate = evidence_payload.get("candidate")
        if (
            (isinstance(observed_run_id, str) and observed_run_id != run_id)
            or (isinstance(observed_work_id, str) and observed_work_id != work_id)
            or (
                isinstance(observed_candidate, str)
                and observed_candidate != candidate
            )
        ):
            raise QualificationFailure(
                "workflow delivery gate evidence is not bound to this dispatch"
            )
        if row["kind"] == "foreign-review" and (
            review_evidence_locator is None
            or path != review_evidence_locator[0]
            or expected_hash != review_evidence_locator[1]
        ):
            raise QualificationFailure(
                "workflow delivery gate foreign-review evidence is not bound to "
                "this run's review verdict"
            )
        gate_paths.add(path)
        gate_inodes.add(inode)
        gate_kinds.add(row["kind"])
        remember_artifact(path, expected_hash)
    if (
        "foreign-review" not in gate_kinds
        or len(gate_kinds & {"copilot", "maintainer-review"}) != 1
    ):
        raise QualificationFailure(
            "workflow independent/delivery gate authority is incomplete"
        )
    markers = [
        "agent-loop-command",
        "bundle",
        "candidate",
        "completion",
        "evidence",
        "ledger",
        "verdict",
    ]
    artifact_rows = [
        _artifact_row(
            path,
            state_root=state_root,
            observed_sha256=artifact_digests[path],
        )
        for path in sorted(artifact_digests)
    ]
    if len(artifact_rows) > MAX_DISPATCH_ARTIFACTS or any(
        len(row["path"]) > MAX_DISPATCH_ARTIFACT_PATH_CHARS
        for row in artifact_rows
    ):
        raise QualificationFailure("dispatch artifact inventory exceeds the evidence bound")
    return (
        markers,
        artifact_rows,
        workflow,
        agent_loop_probe,
    )


def _validate_canary_dispatch_model_identities(
    runtime_env: Mapping[str, str],
) -> None:
    """intake 前確認已安裝的 roster 能授權 canary builder 與獨立 reviewer。

    builder override 的語意檢查要到 build 卡派工時才發生（intake 只驗語法），
    planning 卻在 intake 當下同步執行；這裡先以 Manager 實際讀取的
    `PSC_PROJECT_CONFIG_ROOT` 載入 roster，缺身分、缺能力、domain 相同或 hardened
    相容性不符都 fail-closed，不讓 canary 燒掉一次 planning 才在 build 卡失敗。
    """

    config_root = runtime_env.get("PSC_PROJECT_CONFIG_ROOT")
    if not isinstance(config_root, str) or not config_root:
        raise QualificationFailure("installed model identity overlay root is unavailable")
    from paulsha_cortex.coordinator.model_identities import load_model_identities
    from paulsha_cortex.coordinator.model_resolution import (
        validate_identity_compatibility,
    )

    try:
        identities = load_model_identities(config_root)
        builder = identities.require(
            DEPLOYMENT_CANARY_BUILDER_EXECUTOR, DEPLOYMENT_CANARY_BUILDER_MODEL
        )
        reviewer = identities.require(
            DEPLOYMENT_CANARY_REVIEWER_EXECUTOR, DEPLOYMENT_CANARY_REVIEWER_MODEL
        )
        if (
            not set(CANARY_BUILDER["capabilities"]) <= set(builder.capabilities)
            or builder.independence_domain != CANARY_BUILDER["independence_domain"]
            or not set(CANARY_REVIEWER["capabilities"]) <= set(reviewer.capabilities)
            or reviewer.independence_domain != CANARY_REVIEWER["independence_domain"]
            or reviewer.independence_domain == builder.independence_domain
        ):
            raise ValueError("canary identity capability or independence mismatch")
        validate_identity_compatibility("builder", builder)
        for persona in ("planner", "reviewer"):
            validate_identity_compatibility(persona, reviewer)
    except (KeyError, ValueError) as exc:
        raise QualificationFailure(
            "installed model identity roster cannot authorize the canary builder "
            "and independent reviewer"
        ) from exc


def _dispatch_blocking_summary(terminal: object) -> str:
    """把 `cortex work show --json` 的結構化 blocking reason 帶進失敗訊息。

    例如 Manager 帳號對 private probe 請不到 Copilot review 時，review gate 逾時轉
    needs_human；沒有這段，operator 只看得到一句 needs_human。
    """

    blocking = terminal.get("blocking_reason") if isinstance(terminal, Mapping) else None
    if not isinstance(blocking, Mapping) or not blocking.get("reason"):
        return ""
    detail = str(blocking.get("detail") or "").replace("\n", " ").strip()[:300]
    summary = f": {blocking.get('reason')}"
    return f"{summary} ({detail})" if detail else summary


def _dispatch_work_item(envelope: object, work_id: str) -> Mapping[str, object] | None:
    """`cortex work show --json` 裡屬於本 work item 的 `item` 區段。

    envelope 其他區段（providers、fleet_health 等）也有 `status`／`state`，值可能
    是 closed／done；#716 canary run 36519844244 曾因遞迴掃整份輸出，在 build
    階段就被誤判結案。terminal 判定只看 `item`。
    """

    item = envelope.get("item") if isinstance(envelope, Mapping) else None
    if not isinstance(item, Mapping) or item.get("work_id") != work_id:
        return None
    return item


_DISPATCH_RUN_TERMINAL = frozenset({"done", "completed", "failed", "superseded"})


def _dispatch_run_refs(
    item: Mapping[str, object], statuses: set[str] | None = None
) -> set[str]:
    """本 work item 的 workflow_run source ref；`statuses=None` 取進行中的 run。

    進行中的定義與 `monitor.lifecycle.project_work_items` 相同：status 不在
    done／completed／failed／superseded 之內。
    """

    sources = item.get("sources")
    refs: set[str] = set()
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, Mapping) or source.get("kind") != "workflow_run":
            continue
        status = str(source.get("status", "")).lower()
        active = status not in _DISPATCH_RUN_TERMINAL
        if active if statuses is None else status in statuses:
            refs.add(str(source.get("ref")))
    return refs


def _dispatch_item_verdict(envelope: object, item: Mapping[str, object]) -> str:
    """回傳 done／failed／pending。

    work item 的 `state` 只有在 strict closure（PR merged、issue 全關、openspec
    archive、todo 完成、CompletionRecord 有效）才會是 done；workflow 本身結案時
    `item.sources` 內的 workflow_run 會先變成 done，closeout 另外逐欄驗 registry。
    """

    facets = item.get("facets")
    blocked = (
        isinstance(facets, list) and "needs_human" in facets
    ) or (isinstance(envelope, Mapping) and bool(envelope.get("blocking_reason")))
    if blocked:
        return "failed"
    if _dispatch_run_refs(item):
        return "pending"
    if item.get("state") == "done" or _dispatch_run_refs(item, {"done", "completed"}):
        return "done"
    if _dispatch_run_refs(item, set(_DISPATCH_RUN_TERMINAL)):
        return "failed"
    return "pending"


def _dispatch_item_diagnostic(item: Mapping[str, object] | None) -> str:
    """逾時時最後一次看到的 work item 狀態；只輸出列舉型 token。"""

    if item is None:
        return "item=unobserved"
    sources = item.get("sources")
    runs = [
        f"{_diagnostic_token(source.get('ref'))}:{_diagnostic_token(source.get('status'))}"
        for source in (sources if isinstance(sources, list) else [])
        if isinstance(source, Mapping) and source.get("kind") == "workflow_run"
    ]
    facets = item.get("facets")
    return " ".join(
        (
            f"state={_diagnostic_token(item.get('state'))}",
            "runs=" + (",".join(runs) or "none"),
            "facets="
            + (
                ",".join(_diagnostic_token(facet) for facet in facets) or "none"
                if isinstance(facets, list)
                else _diagnostic_token(facets)
            ),
        )
    )


def _full_dispatch(
    *,
    repository: str,
    work_id: str,
    issue: int,
    release_candidate_sha: str,
    timeout: int,
    evidence_dir: Path,
) -> None:
    if (
        WORK_ID.fullmatch(work_id) is None
        or issue <= 0
        or SHA40.fullmatch(release_candidate_sha) is None
    ):
        raise QualificationFailure("protected full-dispatch work identity is missing")
    runtime_env = _installed_runtime_env()
    _validate_canary_dispatch_model_identities(runtime_env)
    intake = _run(
        (
            "/opt/cortex/venv/bin/cortex",
            "run",
            "work",
            "intake",
            work_id,
            "--repo",
            repository,
            "--issue",
            str(issue),
            "--combo",
            "feature-oneshot",
            "--builder-executor",
            DEPLOYMENT_CANARY_BUILDER_EXECUTOR,
            "--builder-model",
            DEPLOYMENT_CANARY_BUILDER_MODEL,
            "--wait",
            "--timeout",
            "60",
            "--json",
        ),
        env=runtime_env,
        timeout=90,
    )
    _require_success(intake, "full-dispatch intake")
    deadline = time.monotonic() + timeout
    item: Mapping[str, object] | None = None
    observed: Mapping[str, object] | None = None
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
            envelope = records[-1] if records else None
            candidate = _dispatch_work_item(envelope, work_id)
            if candidate is not None:
                observed = candidate
                verdict = _dispatch_item_verdict(envelope, candidate)
                if verdict == "done":
                    item = candidate
                    break
                if verdict == "failed":
                    raise QualificationFailure(
                        "full dispatch reached a failed/needs_human terminal"
                        + _dispatch_blocking_summary(envelope)
                    )
        time.sleep(10)
    else:
        raise QualificationFailure(
            "full dispatch did not reach terminal closeout before timeout: "
            + _dispatch_item_diagnostic(observed)
        )
    markers, artifact_rows, workflow, agent_loop_probe = _validate_dispatch_closeout(
        repository=repository,
        work_id=work_id,
        issue=issue,
        terminal=item,
        coordinator_root=Path(runtime_env["PSC_COORDINATOR_ROOT"]),
    )
    run_id = workflow.get("run_id")
    workflow_candidate = workflow.get("candidate_head")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(workflow_candidate, str)
        or SHA40.fullmatch(workflow_candidate) is None
    ):
        raise QualificationFailure("full-dispatch workflow run identity is unavailable")
    if run_id not in _dispatch_run_refs(item, {"done", "completed"}):
        raise QualificationFailure(
            "CLI terminal is not bound to the completed workflow"
        )
    agent_loop_probe["artifact_set_sha256"] = hashlib.sha256(
        _canonical_bytes(artifact_rows)
    ).hexdigest()
    _write_json(
        evidence_dir / "dispatch-closeout.json",
        {
            "schema_version": 1,
            "status": "passed",
            "repository": repository,
            "work_id": work_id,
            "issue": issue,
            "release_candidate_sha": release_candidate_sha,
            "workflow_candidate_sha": workflow_candidate,
            "terminal": {
                "state": "done",
                "work_id": work_id,
                "run_id": run_id,
            },
            "required_markers": markers,
            "agent_loop_probe": agent_loop_probe,
            "artifacts": artifact_rows,
        },
    )


def _path_identity(path: str) -> tuple[int, int] | None:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return None
    return observed.st_dev, observed.st_ino


def _legacy_adoption_checks(
    *,
    receipt: Mapping[str, Any],
    legacy_dir: Path,
    evidence_dir: Path,
    candidate: Mapping[str, str],
) -> list[dict[str, str]]:
    """Bind the legacy-adoption harness evidence to the live receipt and host.

    The harness (`legacy_adoption.py`) drove the installer CLI; the driver
    re-derives what it can from the final host instead of trusting the harness
    verdict alone: the receipt must be the applied and qualified re-adoption of
    exactly the captured inventory, and every legacy object must still sit in
    its quarantine destination with its legacy inode.
    """

    documents = {
        name: _load_json(legacy_dir / name, f"legacy evidence {name}")
        for name in legacy_fixture.LEGACY_EVIDENCE_FILES
    }
    adoption = documents["legacy-adoption.json"]
    if adoption.get("profile") != legacy_fixture.LEGACY_PROFILE or adoption.get("status") != "passed":
        raise QualificationFailure("legacy-adoption harness evidence is not passed")
    if [row.get("name") for row in adoption.get("steps", [])] != list(
        legacy_fixture.LEGACY_STEPS
    ) or any(row.get("status") != "passed" for row in adoption.get("steps", [])):
        raise QualificationFailure("legacy-adoption harness did not pass every step in order")
    if adoption.get("candidate") != dict(candidate):
        raise QualificationFailure("legacy-adoption harness evidence is bound to another candidate")
    if adoption.get("fixture", {}).get("sha256") != legacy_fixture.manifest_sha256():
        raise QualificationFailure("legacy-adoption harness ran another fixture manifest")
    inventory_sha = adoption.get("inventory", {}).get("inventory_sha256")
    if not isinstance(inventory_sha, str) or SHA256.fullmatch(inventory_sha) is None:
        raise QualificationFailure("legacy-adoption harness evidence lacks the inventory digest")
    if documents["legacy-inventory.json"].get("inventory_sha256") != inventory_sha:
        raise QualificationFailure("legacy inventory evidence is not the inventory the plan bound")
    record = receipt.get("legacy_adoption")
    if (
        not isinstance(record, Mapping)
        or record.get("inventory_sha256") != inventory_sha
        or record.get("apply_inventory_sha256") != inventory_sha
    ):
        raise QualificationFailure(
            "install receipt does not record the legacy apply gate for the captured inventory"
        )
    if receipt.get("state") != "applied" or receipt.get("qualified") is not True:
        raise QualificationFailure("the re-adopted install receipt is not applied and qualified")
    rollback = documents["legacy-rollback.json"]
    if (
        rollback.get("returncode") != 0
        or rollback.get("legacy_restored") is not True
        or rollback.get("restore_safe") is not True
        or rollback.get("retained_unknown") != []
        or rollback.get("retained_drift") != []
    ):
        raise QualificationFailure("legacy rollback evidence is not a restore-safe legacy restore")
    for row in adoption.get("plan", {}).get("quarantine", []):
        if _path_identity(str(row.get("destination"))) != (row.get("dev"), row.get("ino")):
            raise QualificationFailure(
                f"legacy object {row.get('path')} is no longer in its quarantine destination"
            )
    for name, document in documents.items():
        _write_json(evidence_dir / name, document)
    return [{"name": name, "status": "passed"} for name in legacy_fixture.LEGACY_TESTS]


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
    parser.add_argument(
        "--profile",
        required=True,
        choices=("release", "deployment-canary", legacy_fixture.LEGACY_PROFILE),
    )
    parser.add_argument(
        "--legacy-evidence",
        type=Path,
        help="legacy-adoption only: directory the in-container harness wrote",
    )
    parser.add_argument("--probe-repository")
    parser.add_argument("--probe-work-id")
    parser.add_argument("--probe-issue", type=int)
    parser.add_argument("--dispatch-timeout", type=int, default=7200)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    if SHA40.fullmatch(args.candidate_sha) is None:
        parser.error("candidate SHA is invalid")
    for label, value in (("wheel", args.wheel_sha256), ("bundle", args.bundle_sha256)):
        if SHA256.fullmatch(value) is None:
            parser.error(f"{label} SHA-256 is invalid")
    probe_values = (args.probe_repository, args.probe_work_id, args.probe_issue)
    legacy_profile = args.profile == legacy_fixture.LEGACY_PROFILE
    if args.profile in {"release", legacy_fixture.LEGACY_PROFILE} and any(
        value is not None for value in probe_values
    ):
        parser.error(f"{args.profile} profile must not receive external probe inputs")
    if legacy_profile and args.legacy_evidence is None:
        parser.error("legacy-adoption profile requires --legacy-evidence")
    if not legacy_profile and args.legacy_evidence is not None:
        parser.error("--legacy-evidence belongs to the legacy-adoption profile only")
    if args.profile == "deployment-canary" and any(
        value is None for value in probe_values
    ):
        parser.error("deployment-canary profile requires every external probe input")
    try:
        receipt = _load_json(args.receipt, "install receipt")
        args.evidence_dir.mkdir(parents=True, exist_ok=False)
        tests = _installed_checks(
            install_evidence=args.install_evidence,
            receipt=receipt,
            evidence_dir=args.evidence_dir,
        )
        providers: list[dict[str, object]] = []
        if legacy_profile:
            assert args.legacy_evidence is not None
            tests = (
                _legacy_adoption_checks(
                    receipt=receipt,
                    legacy_dir=args.legacy_evidence,
                    evidence_dir=args.evidence_dir,
                    candidate={
                        "candidate_sha": args.candidate_sha,
                        "wheel_sha256": args.wheel_sha256,
                        "bundle_sha256": args.bundle_sha256,
                    },
                )
                + tests
            )
        else:
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
            if args.profile == "deployment-canary":
                providers = _provider_smokes(args.evidence_dir)
                tests.append({"name": "provider-capability-smoke", "status": "passed"})
                assert args.probe_repository is not None
                assert args.probe_work_id is not None
                assert args.probe_issue is not None
                _manager_github_probe(
                    args.probe_repository, args.candidate_sha, args.evidence_dir
                )
                tests.append({"name": "manager-github-dry-run-push", "status": "passed"})
                _prepare_probe_dispatch(
                    receipt=receipt,
                    repository=args.probe_repository,
                    work_id=args.probe_work_id,
                    issue=args.probe_issue,
                )
                _full_dispatch(
                    repository=args.probe_repository,
                    work_id=args.probe_work_id,
                    issue=args.probe_issue,
                    release_candidate_sha=args.candidate_sha,
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
            "schema_version": 2,
            "profile": args.profile,
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
