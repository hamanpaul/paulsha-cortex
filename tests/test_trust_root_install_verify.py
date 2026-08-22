"""Phase 2 trust-root installer RED contract: attestation and verify evidence."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import (
    ActivationError,
    activate_receipt,
    apply_plan,
    attest_generated_inventory,
    new_install_receipt,
    plan_sha256,
    verify_receipt,
)
from paulsha_cortex.trust_root.install import backend as backend_module
from paulsha_cortex.trust_root.install import core as install_core


@pytest.fixture(autouse=True)
def _synthetic_verify_plans_skip_complete_authority_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verification seam tests omit unrelated installer authority steps."""

    monkeypatch.setattr(
        install_core, "_validate_repo_identity", lambda plan: plan["repo_identity"]
    )
    monkeypatch.setattr(
        install_core, "_validate_apply_account_inventories", lambda _plan: {}
    )
    monkeypatch.setattr(
        install_core, "_validate_required_credentials", lambda _plan: None
    )
    monkeypatch.setattr(
        install_core, "_validate_candidate_venv", lambda _plan, _steps: None
    )
    monkeypatch.setattr(
        install_core, "_validate_account_step_bijection", lambda _rows, _steps: None
    )
    monkeypatch.setattr(
        install_core,
        "_validate_repository_step_bijection",
        lambda _plan, _steps, _identity: None,
    )
    monkeypatch.setattr(
        install_core, "_validate_canonical_receipt_path", lambda _plan: None
    )


def _artifact(content: str, *, owner: str = "root", mode: str = "0644"):
    return {"content": content, "owner": owner, "group": "root", "mode": mode}


def _inventory() -> dict[str, dict[str, dict[str, str]]]:
    return {
        "units": {
            "cortex-egress-proxy.service": _artifact(
                "# generated\n[Service]\nUser=cortex-egress-proxy\n"
                "ExecStart=/opt/cortex/venv/bin/cortex egress-proxy\n"
                "ProtectSystem=strict\n"
            ),
            "cortex-manager.service": _artifact(
                "# generated\n[Service]\nUser=cortex-manager\n"
                "ExecStart=/opt/cortex/venv/bin/cortex service run\n"
                "ReadWritePaths=/var/lib/cortex\n"
            ),
            "cortex-monitor.service": _artifact(
                "# generated\n[Service]\nUser=cortex-manager\n"
                "ExecStart=/opt/cortex/venv/bin/cortex monitor\n"
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
        "environment": {
            "cortex-manager.env": _artifact(
                'PSC_INSTANCE="cortex"\n'
                'PSC_MANAGER_INTERVAL_SECONDS="60"\n'
            )
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
        "[Service]\nUser=cortex-manager\n"
        "ExecStart=/opt/cortex/venv/bin/cortex service run\n"
        "ReadWritePaths=/var/lib/cortex\n"
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


def test_manager_environment_value_drift_fails_attestation_and_verify(
    tmp_path: Path,
) -> None:
    plan, receipt = _activated_receipt()
    expected = _inventory()
    installed = deepcopy(expected)
    installed["environment"]["cortex-manager.env"]["content"] = (
        installed["environment"]["cortex-manager.env"]["content"].replace(
            'PSC_MANAGER_INTERVAL_SECONDS="60"',
            'PSC_MANAGER_INTERVAL_SECONDS="61"',
        )
    )

    report = attest_generated_inventory(expected=expected, installed=installed)
    result = verify_receipt(
        receipt,
        plan=plan,
        expected_inventory=expected,
        installed_inventory=installed,
        service_identities=_service_identities(),
        evidence_path=tmp_path / "manager-environment-drift.json",
    )

    assert not report.ok
    assert any(
        row["code"] == "functional_drift"
        and row["artifact"] == "environment/cortex-manager.env"
        for row in report.to_dict()["failures"]
    )
    assert not result.ok
    assert result.evidence["result"] == "fail"
    assert receipt.to_dict()["qualified"] is False


def test_manager_environment_comment_only_drift_warns() -> None:
    expected = _inventory()
    installed = deepcopy(expected)
    installed["environment"]["cortex-manager.env"]["content"] = (
        "# locally documented\n"
        + installed["environment"]["cortex-manager.env"]["content"]
    )

    report = attest_generated_inventory(expected=expected, installed=installed)

    assert report.ok
    assert {
        (row["code"], row["artifact"]) for row in report.to_dict()["warnings"]
    } == {("comment_only_drift", "environment/cortex-manager.env")}


def test_unexpected_manager_environment_artifact_fails_attestation() -> None:
    expected = _inventory()
    installed = deepcopy(expected)
    installed["environment"]["cortex-shadow.env"] = _artifact(
        'PSC_INSTANCE="shadow"\n'
    )

    report = attest_generated_inventory(expected=expected, installed=installed)

    assert not report.ok
    assert any(
        row == {
            "code": "unexpected_authority_artifact",
            "artifact": "environment/cortex-shadow.env",
        }
        for row in report.to_dict()["failures"]
    )


def test_installed_inventory_enumerates_unexpected_manager_environment(
    tmp_path: Path,
) -> None:
    environment_root = tmp_path / "opt/cortex/etc"
    environment_root.mkdir(parents=True)
    manager_environment = environment_root / "cortex-manager.env"
    shadow_environment = environment_root / "cortex-shadow.env"
    manager_environment.write_text('PSC_INSTANCE="cortex"\n', encoding="utf-8")
    shadow_environment.write_text('PSC_INSTANCE="shadow"\n', encoding="utf-8")
    plan = {
        "generated": {
            "environment": {
                manager_environment.name: {
                    **_artifact(manager_environment.read_text(encoding="utf-8")),
                    "path": str(manager_environment),
                }
            }
        }
    }

    installed = backend_module.LocalInstallBackend(
        require_root=False
    ).installed_inventory(plan)

    assert set(installed["environment"]) == {
        "cortex-manager.env",
        "cortex-shadow.env",
    }


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

    def service_identities(self):
        return _service_identities()


class CandidateVenvBackend(VerifyBackend):
    def __init__(self, slot: Path) -> None:
        super().__init__()
        self.slot = slot
        self.stopped: list[str] = []

    def inspect_step(self, step):
        assert step["kind"] == "venv"
        return {
            "exists": True,
            "installed_sha256": step["desired_sha256"],
            "path": str(self.slot),
            "tree_sha256": backend_module._tree_sha256(self.slot),
        }

    def apply_step(self, step):
        return {"prior": {"exists": False}, **self.inspect_step(step)}

    def stop_service(self, name: str) -> None:
        self.stopped.append(name)


def _service_identities(
    *, executable_hash: str = "d" * 64, active_state: str = "active"
) -> dict[str, dict[str, str]]:
    return {
        "cortex-egress-proxy.service": {
            "user": "cortex-egress-proxy",
            "exec_path": "/opt/cortex/venv/bin/cortex",
            "exec_sha256": executable_hash,
            "active_state": active_state,
        },
        "cortex-manager.service": {
            "user": "cortex-manager",
            "exec_path": "/opt/cortex/venv/bin/cortex",
            "exec_sha256": executable_hash,
            "active_state": active_state,
        },
        "cortex-monitor.service": {
            "user": "cortex-manager",
            "exec_path": "/opt/cortex/venv/bin/cortex",
            "exec_sha256": executable_hash,
            "active_state": active_state,
        },
    }


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
        "roots": {
            "deploy": "/opt/cortex",
            "state": "/var/lib/cortex",
            "systemd": "/etc/systemd/system",
            "polkit": "/etc/polkit-1/rules.d",
        },
        "required_credentials": [],
        "apply_order": [],
        "generated": _inventory(),
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


def _candidate_venv_receipt(tmp_path: Path):
    slot = tmp_path / "opt/cortex/venvs/candidate"
    package = slot / "lib/python3.12/site-packages/paulsha_cortex/__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text('__version__ = "0.2.0"\n', encoding="utf-8")
    executable = slot / "bin/cortex"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    wheel_sha256 = "b" * 64
    step = {
        "step_id": "candidate-venv",
        "kind": "venv",
        "path": str(slot),
        "active_link": str(tmp_path / "opt/cortex/venv"),
        "wheel_sha256": wheel_sha256,
        "desired_sha256": wheel_sha256,
        "operations": [],
    }
    plan = {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": "a" * 40,
        },
        "candidate": {
            "wheel_sha256": wheel_sha256,
            "bundle_sha256": "c" * 64,
        },
        "accounts": [],
        "roots": {
            "deploy": str(slot),
            "state": str(tmp_path / "var/lib/cortex"),
            "systemd": str(tmp_path / "etc/systemd/system"),
            "polkit": str(tmp_path / "etc/polkit-1/rules.d"),
        },
        "required_credentials": [],
        "apply_order": [step],
        "generated": _inventory(),
    }
    backend = CandidateVenvBackend(slot)
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    return plan, receipt, backend, package, executable


def test_activation_recomputes_candidate_venv_tree_before_starting_services(
    tmp_path: Path,
) -> None:
    _plan, receipt, backend, package, executable = _candidate_venv_receipt(tmp_path)
    binding = receipt.to_dict()["candidate_venv"]
    assert binding == {
        "path": str(backend.slot),
        "tree_sha256": backend_module._tree_sha256(backend.slot),
    }
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    package.write_text('__version__ = "tampered"\n', encoding="utf-8")
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == executable_sha256

    with pytest.raises(ActivationError, match="candidate venv tree drift"):
        activate_receipt(receipt, backend=backend)

    assert backend.started == []


def test_verify_recomputes_and_evidences_candidate_venv_tree(
    tmp_path: Path,
) -> None:
    plan, receipt, backend, package, executable = _candidate_venv_receipt(tmp_path)
    expected_tree = receipt.to_dict()["candidate_venv"]["tree_sha256"]
    activate_receipt(receipt, backend=backend)
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    package.write_text('__version__ = "tampered-after-start"\n', encoding="utf-8")
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == executable_sha256

    result = verify_receipt(
        receipt,
        plan=plan,
        expected_inventory=_inventory(),
        installed_inventory=_inventory(),
        service_identities=_service_identities(),
        evidence_path=tmp_path / "candidate-venv-drift.json",
        service_controller=backend,
    )

    assert not result.ok
    finding = next(
        row
        for row in result.report.to_dict()["failures"]
        if row["code"] == "candidate_venv_tree_drift"
    )
    assert finding["expected"] == expected_tree
    assert finding["installed"] == backend_module._tree_sha256(backend.slot)
    assert result.evidence["candidate_venv"] == {
        "path": str(backend.slot),
        "tree_sha256": expected_tree,
        "observed_tree_sha256": finding["installed"],
    }


def test_verify_writes_hash_bound_evidence_and_only_then_marks_activation(
    tmp_path: Path,
) -> None:
    plan, receipt = _activated_receipt()
    expected = _inventory()
    installed = deepcopy(expected)
    evidence_path = tmp_path / "evidence" / "trust-root-install.json"
    service_identities = _service_identities()

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
    assert receipt.to_dict()["expected_service_executables"] == {
        service: {
            "exec_path": identity["exec_path"],
            "sha256": identity["exec_sha256"],
        }
        for service, identity in service_identities.items()
    }
    manager_content = installed["units"]["cortex-manager.service"]["content"].encode()
    assert evidence["artifact_hashes"]["units/cortex-manager.service"] == hashlib.sha256(
        manager_content
    ).hexdigest()
    assert receipt.to_dict()["activated"] is True
    assert receipt.to_dict()["qualified"] is True


def test_verify_rejects_a_different_well_formed_service_executable_hash(
    tmp_path: Path,
) -> None:
    plan, receipt = _activated_receipt()
    observed = _service_identities()
    observed["cortex-manager.service"]["exec_sha256"] = "e" * 64

    result = verify_receipt(
        receipt,
        plan=plan,
        expected_inventory=_inventory(),
        installed_inventory=_inventory(),
        service_identities=observed,
        evidence_path=tmp_path / "hash-drift.json",
    )

    assert not result.ok
    failures = result.report.to_dict()["failures"]
    assert any(
        row["code"] == "service_exec_hash_drift"
        and row["artifact"] == "cortex-manager.service"
        for row in failures
    )


def test_verify_fails_closed_without_apply_time_executable_binding(
    tmp_path: Path,
) -> None:
    plan, receipt = _activated_receipt()
    del receipt._document["expected_service_executables"]

    result = verify_receipt(
        receipt,
        plan=plan,
        expected_inventory=_inventory(),
        installed_inventory=_inventory(),
        service_identities=_service_identities(),
        evidence_path=tmp_path / "missing-executable-binding.json",
    )

    assert not result.ok
    assert any(
        row["code"] == "missing_expected_service_executable"
        for row in result.report.to_dict()["failures"]
    )


def test_verify_fails_when_live_service_is_inactive_despite_receipt_flag(
    tmp_path: Path,
) -> None:
    plan, receipt = _activated_receipt()
    observed = _service_identities()
    observed["cortex-manager.service"]["active_state"] = "inactive"
    assert receipt.to_dict()["services_started"] is True

    result = verify_receipt(
        receipt,
        plan=plan,
        expected_inventory=_inventory(),
        installed_inventory=_inventory(),
        service_identities=observed,
        evidence_path=tmp_path / "inactive.json",
    )

    assert not result.ok
    assert any(
        row["code"] == "service_not_active"
        and row["artifact"] == "cortex-manager.service"
        for row in result.report.to_dict()["failures"]
    )


def test_verify_fails_closed_when_live_active_state_is_missing(tmp_path: Path) -> None:
    plan, receipt = _activated_receipt()
    observed = _service_identities()
    del observed["cortex-manager.service"]["active_state"]

    result = verify_receipt(
        receipt,
        plan=plan,
        expected_inventory=_inventory(),
        installed_inventory=_inventory(),
        service_identities=observed,
        evidence_path=tmp_path / "missing-active-state.json",
    )

    assert not result.ok
    assert any(
        row["code"] == "service_not_active"
        and row["artifact"] == "cortex-manager.service"
        for row in result.report.to_dict()["failures"]
    )


def test_verify_failure_records_service_that_could_not_be_stopped(
    tmp_path: Path,
) -> None:
    plan, receipt = _activated_receipt()
    observed = _service_identities()
    observed["cortex-manager.service"]["active_state"] = "inactive"

    class StopController:
        def __init__(self) -> None:
            self.stopped: list[str] = []

        def stop_service(self, service: str) -> None:
            self.stopped.append(service)
            if service == "cortex-manager.service":
                raise RuntimeError("injected stop failure")

    controller = StopController()
    result = verify_receipt(
        receipt,
        plan=plan,
        expected_inventory=_inventory(),
        installed_inventory=_inventory(),
        service_identities=observed,
        evidence_path=tmp_path / "stop-failure.json",
        service_controller=controller,
    )

    assert not result.ok
    assert controller.stopped == [
        "cortex-monitor.service",
        "cortex-manager.service",
        "cortex-egress-proxy.service",
    ]
    document = receipt.to_dict()
    assert document["services_started"] is True
    assert document["running_services"] == ["cortex-manager.service"]
    assert document["verification_stop_failures"] == [
        "cortex-manager.service"
    ]
