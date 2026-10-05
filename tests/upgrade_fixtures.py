"""Shared fakes for `cortex upgrade` tests: no root, no network, no systemd."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import upgrade
from paulsha_cortex.trust_root.install.core import (
    InstallReceipt,
    new_install_receipt,
    plan_sha256,
)
from paulsha_cortex.trust_root.install.release_ingress import (
    ReleaseAsset,
    ReleaseMetadata,
    SealedCandidate,
    asset_names,
    tree_sha256,
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


def make_sealed(
    tmp_path: Path,
    *,
    version: str = "0.1.13",
    commit: str = NEW_COMMIT,
    wheel_sha256: str = NEW_WHEEL,
) -> SealedCandidate:
    attempt = tmp_path / "installer" / version / "attempt-test"
    venv = attempt / "venv"
    (venv / "bin").mkdir(parents=True)
    cli = venv / "bin" / "cortex"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)
    input_root = attempt / "input"
    input_root.mkdir()
    (input_root / "bundle.json").write_text("{}\n", encoding="utf-8")
    (input_root / "install-config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    wheel_name, input_name, qualification_name = asset_names(version)

    def asset(name: str, digest: str = "0" * 64) -> ReleaseAsset:
        return ReleaseAsset(
            name,
            digest,
            1,
            f"https://github.com/hamanpaul/paulsha-cortex/releases/download/v{version}/{name}",
        )

    metadata = ReleaseMetadata(
        version=version,
        tag=f"v{version}",
        commit=commit,
        wheel=asset(wheel_name, wheel_sha256),
        install_input=asset(input_name),
        qualification=asset(qualification_name),
    )
    return SealedCandidate(
        metadata=metadata,
        attempt_dir=attempt,
        input_root=input_root,
        bundle=input_root / "bundle.json",
        install_config=input_root / "install-config.yaml",
        venv=venv,
        cli=cli,
        tree_sha256=tree_sha256(venv, owner_uid=os.getuid()),
        owner_uid=os.getuid(),
    )


class FakePlanCli:
    """Stands in for `<sealed>/bin/cortex install trust-root plan ...` run unprivileged."""

    def __init__(self, plan: dict[str, object], *, reported_sha: str | None = None) -> None:
        self.plan = plan
        self.reported_sha = reported_sha
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv, *, check=False, env=None, uid=None, gid=None, **_kwargs):
        argv = tuple(argv)
        self.calls.append({"argv": argv, "env": dict(env or {}), "uid": uid, "gid": gid})
        output = Path(argv[argv.index("--output") + 1])
        payload = install_core.canonical_plan_bytes(self.plan)
        output.write_bytes(payload)
        sha = self.reported_sha or hashlib.sha256(payload).hexdigest()
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"output": str(output), "plan_sha256": sha}), ""
        )


def account(uid: int, gid: int) -> SimpleNamespace:
    return SimpleNamespace(pw_uid=uid, pw_gid=gid)
