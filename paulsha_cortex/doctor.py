"""Secret-safe deployment diagnostics for the unified lifecycle runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import quote

from ._yaml import YAMLError, safe_load


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


def _preflight_probe(env: Mapping[str, str]) -> ProbeResult:
    raw = env.get("PSC_PREFLIGHT_CMD", "").strip()
    if not raw:
        return ProbeResult("preflight", "fail", "PSC_PREFLIGHT_CMD is not configured", True)
    try:
        argv = shlex.split(raw)
    except ValueError:
        return ProbeResult("preflight", "fail", "PSC_PREFLIGHT_CMD is malformed", True)
    if not argv or (Path(argv[0]).name in {"bash", "sh"} and len(argv) > 1 and argv[1] == "-c"):
        return ProbeResult("preflight", "fail", "preflight command is not a typed executable", True)
    executable = Path(argv[0]).expanduser()
    resolved = (
        str(executable)
        if executable.is_absolute()
        else shutil.which(argv[0], path=env.get("PATH"))
    )
    if resolved is None or not Path(resolved).is_file() or not os.access(resolved, os.X_OK):
        return ProbeResult("preflight", "fail", "preflight executable is unavailable", True)
    return ProbeResult("preflight", "pass", "typed executable is available", True)


def _identity_probe(env: Mapping[str, str], agents_root: Path) -> ProbeResult:
    config_root = Path(
        env.get("PSC_PROJECT_CONFIG_ROOT", str(agents_root / "config" / "paulsha"))
    ).expanduser()
    path = config_root / "model-identities.yaml"
    if not path.is_file():
        try:
            from importlib import resources

            candidate = resources.files("paulsha_cortex.coordinator") / "data" / "model-identities.yaml"
            if candidate.is_file():
                path = Path(str(candidate))
        except (ModuleNotFoundError, OSError):
            pass
    try:
        payload = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, YAMLError):
        return ProbeResult("model-identities", "fail", "identity registry is unavailable", True)
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") not in {1, 2}
        or not isinstance(payload.get("identities"), list)
    ):
        return ProbeResult("model-identities", "fail", "identity registry schema is invalid", True)
    return ProbeResult("model-identities", "pass", f"schema v{payload['schema_version']} registry readable", True)


def _default_agy_probe() -> tuple[bool, str]:
    try:
        from .coordinator.model_identities import probe_agy_capability
    except ImportError:
        return False, "unavailable"
    result = probe_agy_capability()
    return bool(result.ready), "ready" if result.ready else "unavailable"


def _service_paths_probe(*, home: Path, agents_root: Path, instance: str, live: bool) -> ProbeResult:
    if INSTANCE_RE.fullmatch(instance) is None:
        return ProbeResult("service-paths", "fail", "instance name is invalid", True)
    required_paths = (
        home / ".config" / "systemd" / "user" / f"{instance}-manager.service",
        home / ".config" / "systemd" / "user" / f"{instance}-manager.timer",
        home / ".config" / "systemd" / "user" / f"{instance}-monitor.service",
        agents_root / "core" / "runtime" / f"{instance}-manager.env",
    )
    missing = sum(not path.is_file() for path in required_paths)
    if missing:
        return ProbeResult(
            "service-paths",
            "fail" if live else "warn",
            f"{missing} managed service path(s) missing",
            live,
        )
    return ProbeResult("service-paths", "pass", "managed unit/env paths present", live)


def _monitor_state_probe(env: Mapping[str, str], agents_root: Path) -> ProbeResult:
    state_root = Path(
        env.get("PSC_MONITOR_STATE_ROOT", str(agents_root / "monitor"))
    ).expanduser()
    if not state_root.is_absolute():
        return ProbeResult("monitor-state", "fail", "monitor state root must be absolute", True)
    candidate = state_root
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists() or not os.access(candidate, os.W_OK):
        return ProbeResult("monitor-state", "fail", "monitor state root is not writable", True)
    return ProbeResult("monitor-state", "pass", "state and socket roots resolve", True)


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
    probes: list[ProbeResult] = [
        _preflight_probe(environment),
        _identity_probe(environment, agents_root),
        _service_paths_probe(home=home_path, agents_root=agents_root, instance=instance, live=probe_live),
        _monitor_state_probe(environment, agents_root),
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
    if repo is None or REPO_RE.fullmatch(repo) is None:
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
        repo_code, repo_stdout = _process(runner, ["gh", "api", f"repos/{repo}"])
        permission = False
        if repo_code == 0:
            try:
                payload = json.loads(repo_stdout)
                permissions = payload.get("permissions", {}) if isinstance(payload, dict) else {}
                permission = isinstance(permissions, dict) and (
                    permissions.get("push") is True or permissions.get("admin") is True
                )
            except json.JSONDecodeError:
                permission = False
        probes.append(
            ProbeResult("gh-permissions", "pass" if permission else "fail", "write permission confirmed" if permission else "write permission unavailable", True)
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
        description="檢查 unified lifecycle 的本機設定；--probe-live 會執行 gh 與 agy capability probes。",
    )
    parser.add_argument("--probe-live", action="store_true", help="執行 gh auth/permission/label 與 agy safe smoke")
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
