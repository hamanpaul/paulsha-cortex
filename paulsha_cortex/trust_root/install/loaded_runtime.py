"""Loaded↔installed runtime comparison shared by `cortex upgrade` and RC (#841, #1263).

The comparison reads only enumerated tokens from ``cortex service status
--system --json``; detail strings never leave it, so the result is safe to put
in upgrade reports and qualification evidence.
"""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Mapping

from .core import InstallError

_DIAGNOSTIC_TOKEN = re.compile(r"[A-Za-z0-9_.:+-]{1,64}")


def diagnostic_token(value: object) -> str:
    """Short enumerated strings pass; anything else becomes its type name."""

    if value is None:
        return "none"
    if isinstance(value, str) and _DIAGNOSTIC_TOKEN.fullmatch(value):
        return value
    return f"<{type(value).__name__}>"


def runtime_expected(receipt: Mapping[str, object]) -> dict[str, object]:
    plan = receipt.get("plan")
    candidate = plan.get("candidate") if isinstance(plan, Mapping) else None
    identity = plan.get("repo_identity") if isinstance(plan, Mapping) else None
    return {
        "receipt_id": receipt.get("receipt_id"),
        "wheel_sha256": candidate.get("wheel_sha256") if isinstance(candidate, Mapping) else None,
        "candidate_commit": identity.get("commit") if isinstance(identity, Mapping) else None,
    }


def loaded_runtime_mismatch(payload: object, expected: Mapping[str, object]) -> str:
    """Compare Manager/Monitor's loaded artifact and receipt with ``expected``."""

    service = payload.get("service") if isinstance(payload, Mapping) else None
    loaded = service.get("loaded_runtime") if isinstance(service, Mapping) else None
    if not isinstance(loaded, Mapping):
        return "loaded_runtime=unknown"
    expected_receipt = expected.get("receipt_id")
    expected_wheel = expected.get("wheel_sha256")
    expected_commit = expected.get("candidate_commit")
    if not all(
        isinstance(value, str) and value
        for value in (expected_receipt, expected_wheel, expected_commit)
    ):
        return "expected_receipt=unknown"
    mismatches: list[str] = []
    for name in ("manager", "monitor"):
        report = loaded.get(name)
        if not isinstance(report, Mapping):
            mismatches.append(f"{name}=unknown")
            continue
        comparison = report.get("comparison")
        trust_root = report.get("trust_root")
        installed = report.get("installed_artifact")
        if not all(
            isinstance(value, Mapping) for value in (comparison, trust_root, installed)
        ):
            mismatches.append(f"{name}=unknown")
            continue
        if any(
            comparison.get(key) != "match"
            for key in ("artifact_status", "config_status", "process_status")
        ):
            mismatches.append(f"{name}_runtime=mismatch")
        if trust_root.get("status") != "verified":
            mismatches.append(f"{name}_trust={diagnostic_token(trust_root.get('status'))}")
        if trust_root.get("receipt_id") != expected_receipt:
            state = "mismatch" if isinstance(trust_root.get("receipt_id"), str) else "unknown"
            mismatches.append(f"{name}_receipt={state}")
        for label, value in (
            ("loaded_wheel", comparison.get("loaded_wheel_sha256")),
            ("installed_wheel", installed.get("wheel_sha256")),
            ("receipt_wheel", trust_root.get("wheel_sha256")),
        ):
            if value != expected_wheel:
                state = "mismatch" if isinstance(value, str) else "unknown"
                mismatches.append(f"{name}_{label}={state}")
        loaded_commit = trust_root.get("candidate_commit")
        if loaded_commit != expected_commit:
            state = "mismatch" if isinstance(loaded_commit, str) else "unknown"
            mismatches.append(f"{name}_commit={state}")
    return " ".join(mismatches)


def installed_runtime_env(
    deploy_root: Path, state_root: Path, *, owner_uid: int = 0
) -> dict[str, str]:
    """The installed, root-owned PSC runtime projection for operator CLI probes."""

    path = deploy_root / "etc" / "cortex-manager.env"
    try:
        observed = path.lstat()
    except FileNotFoundError as exc:
        raise InstallError("installed Manager environment is absent") from exc
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != owner_uid
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise InstallError("installed Manager environment is not root-controlled")
    env = {
        "HOME": "/root",
        "PATH": f"{deploy_root}/venv/bin:{deploy_root}/toolchain/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for raw in path.read_text(encoding="utf-8", errors="strict").splitlines():
        key, separator, encoded = raw.partition("=")
        if not separator or not key.startswith("PSC_"):
            continue
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise InstallError(f"invalid installed runtime value for {key}") from exc
        if not isinstance(value, str) or "\x00" in value:
            raise InstallError(f"invalid installed runtime value for {key}")
        env[key] = value
    env.setdefault("PSC_CONTROL_ROOT", str(state_root / "control"))
    env.setdefault("PSC_COORDINATOR_ROOT", str(state_root / "coordinator"))
    env.setdefault("PSC_SPECS_ROOT", str(state_root / "specs"))
    env.setdefault("PSC_MONITOR_STATE_ROOT", str(state_root / "monitor"))
    return env
