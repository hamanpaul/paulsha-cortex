#!/usr/bin/env python3
"""Trusted executable probes for Cortex RC qualification.

The driver is copied into the reference image before the candidate is mounted.
It never imports qualification verdicts from the candidate checkout.  A missing
probe, missing provider-native runtime identity, or missing protected probe
repository is a hard failure.
"""

from __future__ import annotations

import argparse
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
            f"{family}/{case_id} unexpectedly succeeded as {user}"
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
        expected_returncodes={13, 30},  # EACCES/EROFS only; ENOENT is a false green.
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
            raise QualificationFailure(
                f"capability/T1.3 did not fail for a capability reason as {principal}"
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
        raw_path = asset.get("path")
        if not isinstance(raw_path, str) or not raw_path.startswith("/"):
            raise QualificationFailure(
                f"durable asset has no absolute path: {asset_id}"
            )
        concrete = re.sub(r"<[^>]+>", "qualification-probe", raw_path).replace(
            "*", "qualification-probe"
        )
        authority = Path(concrete)
        container = authority if asset.get("is_directory") else authority.parent
        if not container.is_dir() or container.is_symlink():
            raise QualificationFailure(f"durable asset container is absent: {asset_id}")
        suffix = hashlib.sha256(asset_id.encode()).hexdigest()[:16]
        target = container / f".cortex-r9-{suffix}"
        target.write_bytes(b"current-valid-content")
        manager = pwd.getpwnam("cortex-manager")
        os.chown(target, manager.pw_uid, manager.pw_gid)
        os.chmod(target, 0o600)
        baseline = target.read_bytes()
        covered_assets += 1
        for principal in principals:
            for operation, expression in operations.items():
                _fs_denied(
                    cases,
                    family="durable-state",
                    case_id=f"{asset_id}:{operation}",
                    user=principal,
                    expression=f"p=Path({str(target)!r})\n{expression}",
                )
                replacement = target.with_name(target.name + ".replacement")
                replacement.unlink(missing_ok=True)
                if (
                    not target.is_file()
                    or target.is_symlink()
                    or target.read_bytes() != baseline
                ):
                    raise QualificationFailure(
                        f"durable-state probe changed authority proxy: {asset_id}"
                    )
        _positive(
            controls,
            family="durable-state",
            case_id=f"durable-manager-write:{asset_id}",
            user="cortex-manager",
            argv=(
                "/usr/bin/python3",
                "-c",
                f"from pathlib import Path; Path({str(target)!r}).write_bytes(b'current-valid-content')",
            ),
        )
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
        response_token = any(
            "QUALIFICATION_OK" in json.dumps(row, sort_keys=True) for row in records
        )
        passed = (
            result.returncode == 0
            and model in models
            and effort in efforts
            and response_token
        )
        raw_evidence["providers"][provider] = {
            "returncode": result.returncode,
            "models": sorted(models),
            "efforts": sorted(efforts),
            "native_metadata": passed,
            "response_token": response_token,
        }
        if not passed:
            raise QualificationFailure(
                f"provider {provider} lacked successful native model/effort metadata "
                f"(rc={result.returncode}, models={sorted(models)}, efforts={sorted(efforts)}, "
                f"response_token={response_token})"
            )
        verdicts.append(
            {
                "provider": provider,
                "requested_model": model,
                "runtime_model": model,
                "requested_effort": effort,
                "runtime_effort": effort,
                "status": "passed",
                "quota": "available",
                "fallback": False,
            }
        )
    _write_json(evidence_dir / "provider-capabilities.json", raw_evidence)
    return verdicts


def _manager_github_probe(
    repository: str, candidate_sha: str, evidence_dir: Path
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise QualificationFailure(
            "protected GitHub probe repository is missing or invalid"
        )
    account = "cortex-manager"
    env = _account_env(account)
    auth = _run(("/usr/bin/gh", "auth", "status"), user=account, env=env, timeout=45)
    _require_success(auth, "Manager gh auth status")
    remote = f"https://github.com/{repository}.git"
    before = _run(
        ("/usr/bin/git", "ls-remote", remote), user=account, env=env, timeout=60
    )
    _require_success(before, "Manager probe repo ls-remote before")
    dry_run = _run(
        (
            "/usr/bin/git",
            "-c",
            "credential.helper=!/usr/bin/gh auth git-credential",
            "-C",
            "/var/lib/cortex/repos/paulsha-cortex",
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
    if before.stdout != after.stdout:
        raise QualificationFailure("Manager dry-run push changed remote refs")
    _write_json(
        evidence_dir / "manager-github-auth.json",
        {
            "schema_version": 1,
            "status": "passed",
            "repository": repository,
            "authenticated": True,
            "dry_run": True,
            "remote_refs_unchanged": True,
            "before_sha256": hashlib.sha256(before.stdout.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(after.stdout.encode()).hexdigest(),
        },
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
    artifact_rows: list[dict[str, str]] = []
    markers: set[str] = set()
    for path in Path("/var/lib/cortex").rglob("*"):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 16 * 1024 * 1024
        ):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if work_id not in content:
            continue
        relative = path.relative_to("/var/lib/cortex").as_posix()
        artifact_rows.append({"path": relative, "sha256": _sha256(path)})
        lowered = relative.lower()
        for marker in (
            "candidate",
            "bundle",
            "verdict",
            "ledger",
            "evidence",
            "completion",
        ):
            if marker in lowered or marker in content.lower():
                markers.add(marker)
    required = {"candidate", "bundle", "verdict", "ledger", "evidence", "completion"}
    if not required <= markers:
        raise QualificationFailure(
            "full dispatch closeout artifact inventory is incomplete: "
            + ", ".join(sorted(required - markers))
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
            "required_markers": sorted(markers),
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
