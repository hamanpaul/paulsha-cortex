"""Shared fakes for `cortex upgrade` tests: no root, no network, no systemd."""
from __future__ import annotations

from pathlib import Path

from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import upgrade
from paulsha_cortex.trust_root.install.core import (
    InstallReceipt,
    new_install_receipt,
    plan_sha256,
)
from paulsha_cortex.trust_root.install.release_ingress import parse_version

PRIOR_WHEEL = "1" * 64
PRIOR_COMMIT = "b" * 40
NEW_WHEEL = "2" * 64
NEW_COMMIT = "a" * 40


def plan_document(
    tmp_path: Path,
    *,
    version: str,
    wheel_sha256: str,
    commit: str,
    overlay_sha: str | None = None,
) -> dict[str, object]:
    plan: dict[str, object] = {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": commit,
        },
        "candidate": {
            "candidate_sha": commit,
            "wheel_sha256": wheel_sha256,
            "bundle_sha256": "c" * 64,
            "wheel": {
                "path": f"dist/paulsha_cortex-{version}-py3-none-any.whl",
                "sha256": wheel_sha256,
            },
        },
        "accounts": [
            {
                "name": "cortex-builder",
                "uid": 993,
                "gid": 993,
                "home": "/var/lib/cortex-builder",
                "shell": "/usr/sbin/nologin",
            }
        ],
        "roots": {
            "deploy": str(tmp_path / "opt/cortex"),
            "state": str(tmp_path / "var/lib/cortex"),
            "systemd": str(tmp_path / "etc/systemd/system"),
            "polkit": str(tmp_path / "etc/polkit-1/rules.d"),
        },
        "apply_order": [],
        "required_credentials": [],
    }
    if overlay_sha is not None:
        plan["host_overlay_sha256"] = overlay_sha
    plan["receipt_path"] = str(install_core.canonical_receipt_path(plan))
    return plan


def durable_prior(
    tmp_path: Path, *, version: str = "0.1.12", qualified: bool = True
) -> InstallReceipt:
    """A real on-disk receipt; callers patch `_validate_receipt_parent/_file`."""

    plan = plan_document(
        tmp_path, version=version, wheel_sha256=PRIOR_WHEEL, commit=PRIOR_COMMIT
    )
    path = (tmp_path / "var/lib/cortex-install-receipts" / "prior.json").absolute()
    receipt = new_install_receipt(plan, path=path)
    receipt._document.update(state="applied", qualified=qualified)
    receipt._persist()
    return receipt


def make_prior(
    tmp_path: Path, *, version: str = "0.1.12", overlay_sha: str | None = None
) -> upgrade.PriorReceipt:
    """An in-memory prior receipt for coordinator tests."""

    plan = plan_document(
        tmp_path,
        version=version,
        wheel_sha256=PRIOR_WHEEL,
        commit=PRIOR_COMMIT,
        overlay_sha=overlay_sha,
    )
    path = tmp_path / "var/lib/cortex-install-receipts" / "prior.json"
    document = {
        "receipt_id": "prior-receipt",
        "plan": plan,
        "plan_sha256": plan_sha256(plan),
        "state": "applied",
        "qualified": True,
        "credentials": [],
    }
    return upgrade.PriorReceipt(
        path=path,
        receipt=InstallReceipt(document, path=path),
        version=parse_version(version),
    )
