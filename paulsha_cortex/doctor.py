"""Secret-safe deployment diagnostics for the unified lifecycle runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import quote

DOCTOR_SCHEMA = "cortex-doctor/v1"
AUTO_LABEL = "cortex:auto-on-going"
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
INSTANCE_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
Runner = Callable[..., object]
AgyProbe = Callable[[], tuple[bool, str]]


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status: str
    detail: str
    required: bool

    def __post_init__(self) -> None:
        if self.status not in {"pass", "warn", "fail"}:
            raise ValueError(f"invalid doctor probe status: {self.status}")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "required": self.required,
        }


@dataclass(frozen=True)
class DoctorReport:
    probes: tuple[ProbeResult, ...]

    @property
    def ok(self) -> bool:
        return not any(probe.required and probe.status == "fail" for probe in self.probes)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": DOCTOR_SCHEMA,
            "ok": self.ok,
            "probes": [probe.to_dict() for probe in self.probes],
        }


def _process(
    runner: Runner,
    argv: list[str],
) -> tuple[int, str]:
    try:
        raw = runner(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except Exception:
        return 1, ""
    returncode = getattr(raw, "returncode", None)
    stdout = getattr(raw, "stdout", "")
    if not isinstance(returncode, int):
        return 1, ""
    return returncode, stdout if isinstance(stdout, str) else ""


def _valid_repo(value: str | None) -> bool:
    if value is None or REPO_RE.fullmatch(value) is None:
        return False
    owner, name = value.split("/", 1)
    return owner not in {".", ".."} and name not in {".", ".."}


def _preflight_probe(env: Mapping[str, str]) -> ProbeResult:
    try:
        _load_runtime_preflight_command(env)
    except (ImportError, OSError, ValueError):
        return ProbeResult("preflight", "fail", "runtime validator rejected preflight command", True)
    return ProbeResult("preflight", "pass", "runtime validator accepted typed executable", True)


def _load_runtime_preflight_command(env: Mapping[str, str]) -> tuple[str, ...]:
    """Use the delivery runtime's single command validator; missing PR C fails closed."""
    from .coordinator.preflight import load_preflight_command

    return load_preflight_command(env=env)


def _identity_probe(env: Mapping[str, str], agents_root: Path) -> ProbeResult:
    config_root = Path(
        env.get("PSC_PROJECT_CONFIG_ROOT", str(agents_root / "config" / "paulsha"))
    ).expanduser()
    try:
        schema_version = _load_runtime_model_identities(config_root)
    except (ImportError, OSError, ValueError):
        return ProbeResult("model-identities", "fail", "runtime validator rejected identity registry", True)
    return ProbeResult(
        "model-identities",
        "pass",
        f"runtime-validated schema v{schema_version} with canonical agy identity",
        True,
    )


def _load_runtime_model_identities(config_root: Path) -> int:
    """Validate the exact registry consumed by planner/reviewer selection."""
    from .coordinator.model_identities import (
        AGY_DOMAIN,
        AGY_LIVE_PROBE,
        AGY_MODEL_ID,
        load_model_identities,
    )

    registry = load_model_identities(config_root)
    identity = registry.get("agy", AGY_MODEL_ID)
    if (
        identity is None
        or identity.independence_domain != AGY_DOMAIN
        or "planning" not in identity.capabilities
        or identity.live_probe != AGY_LIVE_PROBE
    ):
        raise ValueError("canonical agy planning identity missing")
    return int(registry.schema_version)


def _default_agy_probe() -> tuple[bool, str]:
    try:
        from .coordinator.model_identities import (
            AGY_DOMAIN,
            AGY_MODEL_ID,
            probe_agy_capability,
        )
    except ImportError:
        return False, "unavailable"
    result = probe_agy_capability()
    matches = (
        result.executor == "agy"
        and result.model_id == AGY_MODEL_ID
        and result.independence_domain == AGY_DOMAIN
    )
    ready = bool(result.ready) and matches
    return ready, "ready" if ready else "unavailable"


def _service_paths_probe(*, home: Path, instance: str, live: bool) -> ProbeResult:
    if INSTANCE_RE.fullmatch(instance) is None:
        return ProbeResult("service-paths", "fail", "instance name is invalid", True)
    required_paths = (
        home / ".config" / "systemd" / "user" / f"{instance}-manager.service",
        home / ".config" / "systemd" / "user" / f"{instance}-manager.timer",
        home / ".config" / "systemd" / "user" / f"{instance}-monitor.service",
        # systemd templates intentionally bootstrap from this fixed location;
        # PSC_AGENTS_ROOT inside the env file controls all runtime data roots.
        home / ".agents" / "core" / "runtime" / f"{instance}-manager.env",
    )
    missing = sum(path.is_symlink() or not path.is_file() for path in required_paths)
    if missing:
        return ProbeResult(
            "service-paths",
            "fail" if live else "warn",
            f"{missing} managed service path(s) missing",
            live,
        )
    expected_env = f"EnvironmentFile=-%h/.agents/core/runtime/{instance}-manager.env"
    try:
        manager_text = required_paths[0].read_text(encoding="utf-8")
        monitor_text = required_paths[2].read_text(encoding="utf-8")
        env_values: dict[str, str] = {}
        for line in required_paths[3].read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                env_values[key] = value
    except (OSError, UnicodeError):
        return ProbeResult("service-paths", "fail", "managed unit/env paths unreadable", True)
    roots = ("PSC_REPO_ROOT", "PSC_RUN_ROOT", "PSC_MONITOR_STATE_ROOT", "PSC_PROJECT_CONFIG_ROOT")
    if (
        expected_env not in manager_text
        or expected_env not in monitor_text
        or any(not Path(env_values.get(name, "")).expanduser().is_absolute() for name in roots)
    ):
        return ProbeResult("service-paths", "fail", "managed unit/env path contract is inconsistent", True)
    return ProbeResult("service-paths", "pass", "managed unit/env path contract is consistent", live)


def _root_is_creatable(path: Path) -> bool:
    if not path.is_absolute() or path.is_symlink():
        return False
    if path.exists():
        return path.is_dir() and os.access(path, os.W_OK | os.X_OK)
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)


def _monitor_path_probes(
    env: Mapping[str, str],
    agents_root: Path,
    *,
    live: bool,
) -> tuple[ProbeResult, ProbeResult]:
    state_root = Path(env.get("PSC_MONITOR_STATE_ROOT", str(agents_root / "monitor"))).expanduser()
    if not state_root.is_absolute():
        state = ProbeResult("monitor-state", "fail", "monitor state root must be absolute", True)
    elif not _root_is_creatable(state_root):
        state = ProbeResult("monitor-state", "fail", "monitor state root is not writable/creatable", True)
    else:
        state = ProbeResult("monitor-state", "pass", "durable state root is writable/creatable", True)

    run_root = Path(env.get("PSC_RUN_ROOT", str(agents_root / "run"))).expanduser()
    if not run_root.is_absolute():
        monitor_socket = ProbeResult("monitor-socket", "fail", "monitor socket root must be absolute", True)
    elif not _root_is_creatable(run_root):
        monitor_socket = ProbeResult("monitor-socket", "fail", "monitor socket root is not writable/creatable", True)
    elif not live:
        monitor_socket = ProbeResult("monitor-socket", "warn", "socket connectivity not probed", False)
    else:
        socket_path = run_root / "project-monitor.sock"
        try:
            if not stat.S_ISSOCK(socket_path.stat().st_mode):
                raise OSError("not a socket")
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.settimeout(1.0)
                client.connect(str(socket_path))
            finally:
                client.close()
        except OSError:
            monitor_socket = ProbeResult("monitor-socket", "fail", "monitor socket is not listening", True)
        else:
            monitor_socket = ProbeResult("monitor-socket", "pass", "monitor socket accepted a live connection", True)
    return state, monitor_socket


def _parse_included_github_response(raw: str) -> tuple[dict[str, object] | None, frozenset[str]]:
    normalized = raw.replace("\r\n", "\n")
    header_text = ""
    body = normalized
    if normalized.startswith("HTTP/") and "\n\n" in normalized:
        header_text, body = normalized.split("\n\n", 1)
    scopes: set[str] = set()
    for line in header_text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "x-oauth-scopes":
            scopes.update(item.strip().lower() for item in value.split(",") if item.strip())
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, frozenset(scopes)
    return (payload if isinstance(payload, dict) else None), frozenset(scopes)


def _github_write_capabilities_proven(
    payload: Mapping[str, object],
    scopes: frozenset[str],
) -> bool:
    permissions = payload.get("permissions")
    collaborator_write = isinstance(permissions, dict) and (
        permissions.get("push") is True or permissions.get("admin") is True
    )
    token_permissions = payload.get("token_permissions")
    fine_grained = isinstance(token_permissions, dict) and all(
        token_permissions.get(name) in {"write", "admin"}
        for name in ("contents", "issues", "pull_requests")
    )
    classic_scope = "repo" in scopes or (
        payload.get("private") is False and "public_repo" in scopes
    )
    return bool(collaborator_write and (fine_grained or classic_scope))


def run_doctor(
    *,
    probe_live: bool,
    repo: str | None = None,
    instance: str = "cortex",
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: Runner = subprocess.run,
    agy_probe: AgyProbe | None = None,
) -> DoctorReport:
    environment = dict(os.environ if env is None else env)
    home_path = Path(home) if home is not None else Path(environment.get("HOME", str(Path.home())))
    agents_root = Path(environment.get("PSC_AGENTS_ROOT", str(home_path / ".agents"))).expanduser()
    state_probe, socket_probe = _monitor_path_probes(
        environment,
        agents_root,
        live=probe_live,
    )
    probes: list[ProbeResult] = [
        _preflight_probe(environment),
        _identity_probe(environment, agents_root),
        _service_paths_probe(home=home_path, instance=instance, live=probe_live),
        state_probe,
        socket_probe,
    ]
    if not probe_live:
        probes.extend(
            (
                ProbeResult("gh-auth", "warn", "live probe skipped", False),
                ProbeResult("gh-permissions", "warn", "live probe skipped", False),
                ProbeResult("auto-label", "warn", "live probe skipped", False),
                ProbeResult("agy", "warn", "live probe skipped", False),
            )
        )
        return DoctorReport(tuple(probes))
    if not _valid_repo(repo):
        probes.extend(
            (
                ProbeResult("gh-auth", "fail", "--repo owner/name is required", True),
                ProbeResult("gh-permissions", "fail", "repository unavailable", True),
                ProbeResult("auto-label", "fail", "repository label unavailable", True),
            )
        )
    else:
        auth_code, _ = _process(runner, ["gh", "auth", "status"])
        probes.append(
            ProbeResult("gh-auth", "pass" if auth_code == 0 else "fail", "authenticated" if auth_code == 0 else "authentication failed", True)
        )
        repo_code, repo_stdout = _process(
            runner,
            ["gh", "api", "--include", f"repos/{repo}"],
        )
        permission = False
        if repo_code == 0:
            payload, scopes = _parse_included_github_response(repo_stdout)
            permission = payload is not None and _github_write_capabilities_proven(payload, scopes)
        probes.append(
            ProbeResult(
                "gh-permissions",
                "pass" if permission else "fail",
                (
                    "contents/issues/pull-requests write capabilities proven"
                    if permission
                    else "required write capabilities not proven"
                ),
                True,
            )
        )
        label_code, _ = _process(
            runner,
            ["gh", "api", f"repos/{repo}/labels/{quote(AUTO_LABEL, safe='')}"],
        )
        probes.append(
            ProbeResult("auto-label", "pass" if label_code == 0 else "fail", "auto label exists" if label_code == 0 else "auto label missing", True)
        )
    ready, _diagnostic = (agy_probe or _default_agy_probe)()
    probes.append(
        ProbeResult("agy", "pass" if ready else "fail", "safe plan/sandbox capability ready" if ready else "safe agy capability unavailable", True)
    )
    return DoctorReport(tuple(probes))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cortex doctor",
        description="檢查 unified lifecycle 的本機設定；--probe-live 會執行 gh、agy 與 Monitor socket probes。",
    )
    parser.add_argument(
        "--probe-live",
        action="store_true",
        help="執行 gh auth/permission/label、agy safe smoke 與 Monitor socket 連線",
    )
    parser.add_argument("--repo", help="GitHub owner/name；live probe 必填")
    parser.add_argument("--instance", default="cortex", help="systemd instance 前綴")
    parser.add_argument("--json", action="store_true", help="輸出 cortex-doctor/v1 JSON")
    args = parser.parse_args(argv)
    report = run_doctor(
        probe_live=args.probe_live,
        repo=args.repo,
        instance=args.instance,
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        for probe in report.probes:
            print(f"{probe.status.upper():4} {probe.name}: {probe.detail}")
    return 0 if report.ok else 1
