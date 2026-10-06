#!/usr/bin/env python3
"""Check installer launch authorities through the production provisioning APIs."""
from __future__ import annotations

import argparse
import json
import os
import pwd
import sys
from pathlib import Path
from typing import Mapping

from paulsha_cortex.coordinator import spool_slot
from paulsha_cortex.trust_root.install import core
from paulsha_cortex.trust_root.install import InstallReceipt
from paulsha_cortex.trust_root.install.backend import LocalInstallBackend

try:
    from qualification import legacy_fixture
except ModuleNotFoundError:  # run as qualification/launch_authority_probe.py from a checkout
    import legacy_fixture  # type: ignore[no-redef]


def _read_document(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _manager_environment(plan: Mapping[str, object]) -> tuple[dict[str, str], Path]:
    generated = plan.get("generated")
    environment = generated.get("environment") if isinstance(generated, Mapping) else None
    row = environment.get("cortex-manager.env") if isinstance(environment, Mapping) else None
    path = Path(str(row.get("path"))) if isinstance(row, Mapping) else None
    if path is None or not path.is_file() or path.is_symlink():
        raise ValueError("installed Manager EnvironmentFile is unavailable")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        key, separator, encoded = raw.partition("=")
        if not separator or not key.startswith("PSC_"):
            continue
        value = json.loads(encoded)
        if not isinstance(value, str):
            raise ValueError("Manager EnvironmentFile contains a non-string value")
        values[key] = value
    return values, path


def _drop_to_manager(plan: Mapping[str, object]) -> None:
    accounts = plan.get("accounts")
    account_rows = accounts if isinstance(accounts, list) else []
    account = next(
        (
            row
            for row in account_rows
            if isinstance(row, Mapping) and row.get("name") == "cortex-manager"
        ),
        None,
    )
    if not isinstance(account, Mapping):
        raise ValueError("install plan lacks Manager account identity")
    try:
        manager = pwd.getpwnam("cortex-manager")
    except KeyError as exc:
        raise ValueError("installed Manager account is unavailable") from exc
    if (manager.pw_uid, manager.pw_gid) != (account.get("uid"), account.get("gid")):
        raise ValueError("installed Manager account differs from the install plan")
    if os.geteuid() == manager.pw_uid:
        return
    if os.geteuid() != 0:
        raise PermissionError("launch authority probe must run as root or cortex-manager")
    os.initgroups(manager.pw_name, manager.pw_gid)
    os.setgid(manager.pw_gid)
    os.setuid(manager.pw_uid)


def _validate_launch_authority_layout(plan: Mapping[str, object]) -> None:
    layout_version = core.LAUNCH_AUTHORITY_LAYOUT_VERSION
    if plan.get("launch_layout_version") != layout_version:
        raise ValueError(
            f"install plan does not declare launch authority layout v{layout_version}"
        )


def run(plan_path: Path, receipt_path: Path) -> dict[str, object]:
    plan = _read_document(plan_path)
    receipt_document = _read_document(receipt_path)
    if receipt_document.get("state") != "applied" or receipt_document.get("qualified") is not True:
        raise ValueError("authority probe requires the applied, qualified install receipt")
    _validate_launch_authority_layout(plan)
    receipt = InstallReceipt(receipt_document, path=receipt_path)
    credential_failures = LocalInstallBackend().validate_credentials(receipt)
    if credential_failures:
        raise ValueError("installed credential authority failed validation")
    manager_env, _env_path = _manager_environment(plan)
    os.environ.update(manager_env)
    _drop_to_manager(plan)

    accounts = plan.get("accounts")
    required = plan.get("required_credentials")
    if not isinstance(accounts, list) or not isinstance(required, list):
        raise ValueError("install plan credential roster is invalid")
    account_by_name = {
        str(row.get("name")): row
        for row in accounts
        if isinstance(row, Mapping) and isinstance(row.get("name"), str)
    }
    checked: list[dict[str, str]] = []
    for row in required:
        if not isinstance(row, Mapping):
            raise ValueError("install plan credential row is invalid")
        principal, provider = row.get("principal"), row.get("provider")
        if not isinstance(principal, str) or not isinstance(provider, str):
            raise ValueError("install plan credential identity is invalid")
        check = legacy_fixture.provider_check_for_principal(principal)
        if principal == "manager":
            checked.append(
                {"principal": principal, "provider": provider, "check": check}
            )
            continue
        runtime_principal = {"builder": "builder", "reviewer-planner": "reviewer"}.get(
            principal
        )
        account_name = {
            "builder": "cortex-builder",
            "reviewer-planner": "cortex-reviewer-planner",
        }.get(principal)
        account = account_by_name.get(str(account_name))
        if runtime_principal is None or not isinstance(account, Mapping):
            raise ValueError("credential principal has no launcher runtime account")
        controls = spool_slot.canonical_codex_controls(runtime_principal)
        job_id = f"qualification-{runtime_principal}-{provider}"
        spool_slot.provision_runtime_surfaces(
            principal=runtime_principal,
            job_id=job_id,
            canonical_codex_home=controls,
            account=str(account["name"]),
            seed_credential=provider == "codex",
        )
        if provider == "copilot":
            authority = spool_slot.copilot_oauth_authority()
            spool_slot.provision_copilot_home(
                principal=runtime_principal,
                job_id=job_id,
                authority=authority,
                account=str(account["name"]),
            )
        checked.append(
            {"principal": principal, "provider": provider, "check": check}
        )
    return {"status": "passed", "providers": checked}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.plan, args.receipt)
    except Exception as exc:
        print(f"launch authority probe failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
