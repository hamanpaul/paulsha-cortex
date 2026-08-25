"""Command-line surface for privileged trust-root installation."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from .backend import LocalInstallBackend
from .core import (
    InstallError,
    InstallPlanError,
    InstallReceipt,
    UnsafeInstallPathError,
    activate_receipt,
    apply_plan,
    atomic_write_json,
    bind_bundle_artifacts,
    build_install_plan,
    canonical_receipt_path,
    import_credential,
    new_install_receipt,
    plan_sha256,
    rollback_receipt,
    validate_apply_plan,
    validate_bundle_manifest,
    verify_receipt,
)


def _load_mapping(path: Path, *, label: str) -> dict[str, object]:
    if path.is_symlink():
        raise UnsafeInstallPathError(f"{label} must not be a symlink: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InstallPlanError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstallPlanError(f"{label} must contain an object")
    return payload


def _load_plan(path: Path) -> dict[str, object]:
    return _load_mapping(path, label="install plan")


def _require_root() -> None:
    if os.geteuid() != 0:
        raise PermissionError("this trust-root command requires root; rerun it with sudo")


def _receipt_path(plan: Mapping[str, object]) -> Path:
    value = plan.get("receipt_path")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise InstallPlanError("plan does not contain an absolute receipt_path")
    return Path(value)


def _emit(payload: object) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex install trust-root",
        description="Plan, apply, credential, activate, verify, and roll back a four-way trust root.",
    )
    sub = parser.add_subparsers(dest="trust_root_command", required=True)

    plan = sub.add_parser("plan", help="produce a rootless exact-artifact desired-state plan")
    plan.add_argument("--config", required=True)
    plan.add_argument("--bundle", required=True)
    plan.add_argument("--output", required=True)

    apply = sub.add_parser("apply", help="apply an exact confirmed plan as root")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--confirm-sha256", required=True)
    apply.add_argument(
        "--receipt",
        help="optional absolute receipt path; defaults to the plan's receipt_path",
    )

    credentials = sub.add_parser("credentials", help="explicit credential import")
    credential_sub = credentials.add_subparsers(dest="credential_command", required=True)
    credential_import = credential_sub.add_parser("import")
    credential_import.add_argument("--receipt", required=True)
    credential_import.add_argument("--principal", required=True)
    credential_import.add_argument("--provider", required=True)
    credential_import.add_argument("--source", required=True)

    activate = sub.add_parser("activate", help="start egress, Manager, then Monitor")
    activate.add_argument("--receipt", required=True)

    verify = sub.add_parser("verify", help="attest installed artifacts and emit evidence")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--json", dest="json_output", action="store_true")
    verify.add_argument("--evidence", required=True)

    rollback = sub.add_parser("rollback", help="roll back only receipt-owned state")
    rollback.add_argument("--receipt", required=True)
    return parser


def _plan_command(args: argparse.Namespace) -> int:
    config = _load_mapping(Path(args.config), label="trust-root config")
    manifest = validate_bundle_manifest(Path(args.bundle))
    repo_identity = config.get("repo_identity")
    if not isinstance(repo_identity, Mapping) or repo_identity.get("commit") != manifest["candidate_sha"]:
        raise InstallPlanError(
            "config repo_identity.commit must equal bundle candidate_sha"
        )
    wheel = manifest["wheel"]
    if not isinstance(wheel, Mapping):
        raise InstallPlanError("validated bundle wheel is invalid")
    plan = build_install_plan(
        config=config,
        candidate_wheel=Path(str(wheel["resolved_path"])),
        bundle=Path(str(manifest["manifest_path"])),
    )
    plan = bind_bundle_artifacts(plan, manifest)
    candidate = plan.get("candidate")
    if not isinstance(candidate, dict):
        raise InstallPlanError("generated plan candidate is invalid")
    candidate.update(
        {
            "candidate_sha": manifest["candidate_sha"],
            "wheel": {
                "path": wheel["path"],
                "sha256": wheel["sha256"],
            },
            "wheelhouse": [
                {"path": row["path"], "sha256": row["sha256"]}
                for row in manifest["wheelhouse"]
            ],
            "generated_artifacts": [
                {"path": row["path"], "sha256": row["sha256"]}
                for row in manifest["generated_artifacts"]
            ],
        }
    )
    venv_step = next(
        (
            step
            for step in plan.get("apply_order", [])
            if isinstance(step, dict) and step.get("kind") == "venv"
        ),
        None,
    )
    if not isinstance(venv_step, dict):
        raise InstallPlanError("generated plan lacks the candidate venv step")
    venv_step["wheel_source"] = wheel["resolved_path"]
    venv_step["wheelhouse"] = [
        {"source": row["resolved_path"], "sha256": row["sha256"]}
        for row in manifest["wheelhouse"]
    ]
    if not any(
        row["sha256"] == wheel["sha256"] for row in venv_step["wheelhouse"]
    ):
        raise InstallPlanError("bundle wheelhouse must include the exact candidate wheel")
    plan["receipt_path"] = str(canonical_receipt_path(plan))
    output = Path(args.output).expanduser().absolute()
    atomic_write_json(output, plan, mode=0o600)
    _emit({"output": str(output), "plan_sha256": plan_sha256(plan)})
    return 0


def _apply_command(args: argparse.Namespace) -> int:
    _require_root()
    plan = _load_plan(Path(args.plan))
    validate_apply_plan(plan, confirm_sha256=args.confirm_sha256)
    receipt_path = (
        Path(args.receipt).expanduser()
        if args.receipt is not None
        else _receipt_path(plan)
    )
    if not receipt_path.is_absolute() or ".." in receipt_path.parts:
        raise UnsafeInstallPathError(
            f"receipt path must be absolute and contain no '..': {receipt_path}"
        )
    receipt = (
        InstallReceipt.load(receipt_path, expected_plan=plan)
        if receipt_path.exists()
        else new_install_receipt(plan, path=receipt_path)
    )
    apply_plan(
        plan,
        confirm_sha256=args.confirm_sha256,
        receipt=receipt,
        backend=LocalInstallBackend(),
    )
    _emit(
        {
            "receipt": str(receipt_path),
            "receipt_id": receipt.to_dict()["receipt_id"],
            "state": receipt.to_dict()["state"],
        }
    )
    return 0


def _credential_command(args: argparse.Namespace) -> int:
    _require_root()
    receipt = InstallReceipt.load(Path(args.receipt))
    plan = receipt.to_dict().get("plan", {})
    accounts = plan.get("accounts", []) if isinstance(plan, Mapping) else []
    account_name = {
        "builder": "cortex-builder",
        "reviewer-planner": "cortex-reviewer-planner",
        "manager": "cortex-manager",
    }.get(args.principal)
    account = next(
        (
            row
            for row in accounts
            if isinstance(row, Mapping) and row.get("name") == account_name
        ),
        None,
    )
    if (
        not isinstance(account, Mapping)
        or not isinstance(account.get("home"), str)
        or not isinstance(account.get("uid"), int)
        or not isinstance(account.get("gid"), int)
    ):
        raise InstallPlanError(
            f"receipt plan lacks the home for principal {args.principal}"
        )
    metadata = import_credential(
        receipt,
        principal=args.principal,
        provider=args.provider,
        source=Path(args.source),
        destination_root=Path(str(account["home"])),
        destination_uid=account["uid"],
        destination_gid=account["gid"],
    )
    _emit(metadata.to_dict())
    return 0


def _activate_command(args: argparse.Namespace) -> int:
    _require_root()
    receipt = InstallReceipt.load(Path(args.receipt))
    activate_receipt(receipt, backend=LocalInstallBackend())
    _emit(
        {
            "receipt_id": receipt.to_dict()["receipt_id"],
            "services_started": True,
            "qualified": False,
        }
    )
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    _require_root()
    receipt = InstallReceipt.load(Path(args.receipt))
    document = receipt.to_dict()
    plan = document.get("plan")
    if not isinstance(plan, Mapping):
        raise InstallPlanError("receipt plan is invalid")
    expected = plan.get("generated")
    if not isinstance(expected, Mapping):
        raise InstallPlanError("plan generated inventory is invalid")
    backend = LocalInstallBackend()
    result = verify_receipt(
        receipt,
        plan=plan,
        expected_inventory=expected,  # type: ignore[arg-type]
        installed_inventory=backend.installed_inventory(plan),
        service_identities=backend.service_identities(),
        evidence_path=Path(args.evidence).expanduser().absolute(),
        service_controller=backend,
    )
    if args.json_output:
        _emit(result.to_dict())
    else:
        sys.stdout.write(f"trust-root verify: {'PASS' if result.ok else 'FAIL'}\n")
    return 0 if result.ok else 1


def _rollback_command(args: argparse.Namespace) -> int:
    _require_root()
    receipt = InstallReceipt.load(Path(args.receipt))
    result = rollback_receipt(receipt, backend=LocalInstallBackend())
    _emit(result.to_dict())
    return 0 if not result.retained_drift else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.trust_root_command == "plan":
            return _plan_command(args)
        if args.trust_root_command == "apply":
            return _apply_command(args)
        if args.trust_root_command == "credentials":
            return _credential_command(args)
        if args.trust_root_command == "activate":
            return _activate_command(args)
        if args.trust_root_command == "verify":
            return _verify_command(args)
        if args.trust_root_command == "rollback":
            return _rollback_command(args)
    except (InstallError, PermissionError, OSError, ValueError) as exc:
        sys.stderr.write(f"trust-root install failed: {exc}\n")
        return 1
    raise AssertionError("unreachable trust-root command")
