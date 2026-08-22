"""Phase 2 trust-root installer RED contract: attestation and verify evidence."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from paulsha_cortex.trust_root.install import (
    activate_receipt,
    apply_plan,
    attest_generated_inventory,
    new_install_receipt,
    plan_sha256,
    verify_receipt,
)


def _artifact(content: str, *, owner: str = "root", mode: str = "0644"):
    return {"content": content, "owner": owner, "group": "root", "mode": mode}


def _inventory() -> dict[str, dict[str, dict[str, str]]]:
    return {
        "units": {
            "cortex-egress-proxy.service": _artifact(
                "# generated\n[Service]\nUser=cortex-egress-proxy\nProtectSystem=strict\n"
            ),
            "cortex-manager.service": _artifact(
                "# generated\n[Service]\nUser=cortex-manager\n"
                "ReadWritePaths=/var/lib/cortex\n"
            ),
            "cortex-monitor.service": _artifact(
                "# generated\n[Service]\nUser=cortex-manager\n"
                "ReadWritePaths=/var/lib/cortex/monitor\n"
            ),
        },
        "shim": {
            "cortex-job-shim": _artifact(
                "#!/opt/cortex/venv/bin/python\nfrom paulsha_cortex import cli\n",
                mode="0755",
            )
        },
        "polkit": {
            "49-cortex-job.rules": _artifact(
                "polkit.addRule(function(action, subject) { return null; });\n"
            )
        },
        "gitconfigs": {
            "manager-gitconfig": _artifact(
                "# root-owned Manager git config\n"
                "[safe]\n\tdirectory = /var/lib/cortex/repos/paulsha-cortex\n"
                "[credential \"https://github.com\"]\n"
                "\thelper = !/usr/bin/gh auth git-credential\n"
            ),
            "builder-gitconfig": _artifact(
                "[safe]\n\tdirectory = /var/lib/cortex/repos/paulsha-cortex\n"
            ),
        },
        "toolchain_wrappers": {
            "codex": _artifact(
                "#!/bin/sh\nexec /opt/cortex/toolchain/lib/codex \"$@\"\n",
                mode="0755",
            ),
            "agy": _artifact("#!/bin/sh\nexec /opt/cortex/toolchain/lib/agy \"$@\"\n", mode="0755"),
        },
        "enforcement": {},
    }


def _codes(rows) -> set[str]:
    return {row["code"] for row in rows}


def test_comment_only_drift_warns_but_does_not_fail() -> None:
    expected = _inventory()
    installed = deepcopy(expected)
    installed["units"]["cortex-manager.service"]["content"] = (
        "# local explanatory comment\n"
        "[Service]\nUser=cortex-manager\nReadWritePaths=/var/lib/cortex\n"
    )

    report = attest_generated_inventory(expected=expected, installed=installed)

    assert report.ok
    rendered = report.to_dict()
    assert _codes(rendered["warnings"]) == {"comment_only_drift"}
    assert rendered["failures"] == []


def test_missing_functional_unit_line_fails_even_when_metadata_matches() -> None:
    expected = _inventory()
    installed = deepcopy(expected)
    installed["units"]["cortex-manager.service"]["content"] = (
        "# generated\n[Service]\nUser=cortex-manager\n"
    )

    report = attest_generated_inventory(expected=expected, installed=installed)

    assert not report.ok
    rendered = report.to_dict()
    assert "functional_drift" in _codes(rendered["failures"])
    finding = next(
        row for row in rendered["failures"] if row["code"] == "functional_drift"
    )
    assert finding["artifact"] == "units/cortex-manager.service"
    assert "ReadWritePaths=/var/lib/cortex" in finding["missing_functional_lines"]


def test_missing_generated_inventory_category_fails_closed() -> None:
    expected = _inventory()
    installed = deepcopy(expected)
    del installed["toolchain_wrappers"]

    report = attest_generated_inventory(expected=expected, installed=installed)

    assert not report.ok
    assert "missing_inventory_category" in _codes(report.to_dict()["failures"])


def test_manager_git_credential_helper_removal_is_functional_drift_763() -> None:
    expected = _inventory()
    installed = deepcopy(expected)
    content = installed["gitconfigs"]["manager-gitconfig"]["content"]
    installed["gitconfigs"]["manager-gitconfig"]["content"] = content.replace(
        '\n[credential "https://github.com"]\n'
        "\thelper = !/usr/bin/gh auth git-credential\n",
        "\n",
    )

    report = attest_generated_inventory(expected=expected, installed=installed)

    assert not report.ok
    finding = next(
        row for row in report.to_dict()["failures"] if row["code"] == "functional_drift"
    )
    assert finding["artifact"] == "gitconfigs/manager-gitconfig"
    assert any(
        "/usr/bin/gh auth git-credential" in line
        for line in finding["missing_functional_lines"]
    )


class VerifyBackend:
    def __init__(self) -> None:
        self.started: list[str] = []

    def preflight_facts(self, _plan):
        return {
            "systemd": True,
            "polkit": True,
            "cgroup_v2": True,
            "acl": True,
            "disk_free_bytes": 2 * 1024 * 1024 * 1024,
            "universal_nopasswd": False,
            "in_flight_jobs": 0,
            "services": {
                "cortex-egress-proxy.service": "inactive",
                "cortex-manager.service": "inactive",
                "cortex-monitor.service": "inactive",
            },
            "accounts": {},
            "paths": {},
        }

    def inspect_step(self, _step):  # pragma: no cover - empty plan contract
        raise AssertionError

    def apply_step(self, _step):  # pragma: no cover - empty plan contract
        raise AssertionError

    def start_service(self, name: str) -> None:
        self.started.append(name)

    def stop_service(self, _name: str) -> None:  # pragma: no cover - success path
        raise AssertionError


def _activated_receipt():
    plan = {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": "a" * 40,
        },
        "candidate": {
            "wheel_sha256": "b" * 64,
            "bundle_sha256": "c" * 64,
        },
        "accounts": [],
        "required_credentials": [],
        "apply_order": [],
    }
    backend = VerifyBackend()
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    activate_receipt(receipt, backend=backend)
    return plan, receipt


def test_verify_writes_hash_bound_evidence_and_only_then_marks_activation(
    tmp_path: Path,
) -> None:
    plan, receipt = _activated_receipt()
    expected = _inventory()
    installed = deepcopy(expected)
    evidence_path = tmp_path / "evidence" / "trust-root-install.json"
    service_identities = {
        "cortex-egress-proxy.service": {
            "user": "cortex-egress-proxy",
            "exec_sha256": "d" * 64,
        },
        "cortex-manager.service": {
            "user": "cortex-manager",
            "exec_sha256": "e" * 64,
        },
        "cortex-monitor.service": {
            "user": "cortex-manager",
            "exec_sha256": "f" * 64,
        },
    }

    result = verify_receipt(
        receipt,
        plan=plan,
        expected_inventory=expected,
        installed_inventory=installed,
        service_identities=service_identities,
        evidence_path=evidence_path,
    )

    assert result.ok
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["schema_version"] == 1
    assert evidence["result"] == "pass"
    assert evidence["plan_sha256"] == plan_sha256(plan)
    assert evidence["receipt_id"] == receipt.to_dict()["receipt_id"]
    assert evidence["candidate"] == plan["candidate"]
    assert evidence["service_identities"] == service_identities
    manager_content = installed["units"]["cortex-manager.service"]["content"].encode()
    assert evidence["artifact_hashes"]["units/cortex-manager.service"] == hashlib.sha256(
        manager_content
    ).hexdigest()
    assert receipt.to_dict()["activated"] is True
    assert receipt.to_dict()["qualified"] is True
