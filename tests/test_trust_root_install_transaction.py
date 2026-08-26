"""Phase 2 trust-root installer RED contract: preflight and transactions.

The backend below is an in-memory OS seam.  A focused unit run must not inspect the
host, invoke sudo, or contact systemd; production apply consumes explicit facts and
typed steps through the same seam.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import backend as install_backend
from paulsha_cortex.trust_root.install import (
    AccountCollisionError,
    InstallDriftError,
    InstallError,
    InstallPlanError,
    InstallReceipt,
    UnsafeInstallPathError,
    apply_plan,
    new_install_receipt,
    plan_sha256,
    rollback_receipt,
    validate_preflight,
)


@pytest.fixture(autouse=True)
def _synthetic_transaction_plans_skip_complete_authority_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These seam tests intentionally isolate selected steps from a full plan."""

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
    monkeypatch.setattr(
        install_core,
        "_validate_finalized_apply_surfaces",
        lambda _plan, _steps: None,
    )


def _accounts(tmp_path: Path) -> list[dict[str, object]]:
    return [
        {
            "name": "cortex-manager",
            "uid": 991,
            "gid": 991,
            "home": str(tmp_path / "var/lib/cortex-manager"),
            "shell": "/usr/sbin/nologin",
        },
        {
            "name": "cortex-reviewer-planner",
            "uid": 992,
            "gid": 992,
            "home": str(tmp_path / "var/lib/cortex-reviewer-planner"),
            "shell": "/usr/sbin/nologin",
        },
        {
            "name": "cortex-builder",
            "uid": 993,
            "gid": 993,
            "home": str(tmp_path / "var/lib/cortex-builder"),
            "shell": "/usr/sbin/nologin",
        },
        {
            "name": "cortex-gate",
            "uid": 994,
            "gid": 994,
            "home": str(tmp_path / "var/lib/cortex-gate"),
            "shell": "/usr/sbin/nologin",
        },
    ]


def _step(tmp_path: Path, step_id: str, digest: str) -> dict[str, object]:
    return {
        "step_id": step_id,
        "kind": "asset",
        "path": str(tmp_path / "target" / step_id),
        "owner": "root",
        "group": "root",
        "mode": "0750",
        "acls": [{"account": "cortex-manager", "perms": "rX"}],
        # chmod must precede all ACL writes: chmod after setfacl rewrites the mask.
        "operations": ["snapshot", "chown", "chmod", "set_acl"],
        "desired_sha256": digest,
        "durable": step_id == "state-root",
    }


def _plan(tmp_path: Path) -> dict[str, object]:
    target = tmp_path / "target"
    return {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {"commit": "a" * 40},
        "candidate": {
            "wheel_sha256": "b" * 64,
            "bundle_sha256": "c" * 64,
        },
        "accounts": _accounts(tmp_path),
        "roots": {
            "deploy": str(target / "deploy"),
            "state": str(target / "state-root"),
            "systemd": str(target / "systemd"),
            "polkit": str(target / "polkit"),
        },
        "required_credentials": [],
        "apply_order": [
            _step(tmp_path, "state-root", "1" * 64),
            _step(tmp_path, "manager-unit", "2" * 64),
        ],
    }


def _safe_facts(plan: dict[str, object]) -> dict[str, object]:
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
        "paths": {
            step["path"]: {"exists": False, "is_symlink": False}
            for step in plan["apply_order"]
            if "path" in step
        },
    }


class RecordingBackend:
    """Small explicit backend contract used by the transaction tests."""

    def __init__(self, plan: dict[str, object]) -> None:
        self.facts = _safe_facts(plan)
        self.states: dict[str, dict[str, object]] = {}
        self.applied: list[str] = []
        self.rolled_back: list[str] = []
        self.unknown: set[str] = set()
        self.fail_on: str | None = None
        self.fail_after_mutation: str | None = None
        self.fail_with_partial_mutation: str | None = None
        self.fail_stop: str | None = None
        self.stopped: list[str] = []
        self.credential_rollbacks = 0
        self.creation_identities: dict[str, dict[str, object]] = {}
        self.venv_activation: dict[str, object] = {"exists": False}

    def preflight_facts(self, _plan) -> dict[str, object]:
        return deepcopy(self.facts)

    def inspect_step(self, step) -> dict[str, object]:
        return deepcopy(self.states.get(step["step_id"], {"exists": False}))

    def inspect_venv_activation(self, _step) -> dict[str, object]:
        return deepcopy(self.venv_activation)

    def _apply_step(self, step, creation_checkpoint=None) -> dict[str, object]:
        step_id = step["step_id"]
        if self.fail_on == step_id:
            self.fail_on = None
            raise RuntimeError(f"injected interruption at {step_id}")
        prior = self.inspect_step(step)
        state = {
            "exists": True,
            "installed_sha256": step["desired_sha256"],
            "owner": step["owner"],
            "group": step["group"],
            "mode": step["mode"],
            "acl": deepcopy(step["acls"]),
        }
        self.states[step_id] = state
        if not prior.get("exists") and step.get("kind") == "asset":
            authority = {
                "device": 1,
                "inode": len(self.creation_identities) + 100,
                "file_type": step.get("asset_type", "file"),
            }
            self.creation_identities[step_id] = authority
            if creation_checkpoint is not None:
                creation_checkpoint(authority)
        self.applied.append(step_id)
        if self.fail_with_partial_mutation == step_id:
            self.fail_with_partial_mutation = None
            self.states[step_id]["installed_sha256"] = "partial"
            raise RuntimeError(f"injected partial-mutation interruption at {step_id}")
        if self.fail_after_mutation == step_id:
            self.fail_after_mutation = None
            raise RuntimeError(f"injected post-mutation interruption at {step_id}")
        return {"prior": prior, **state}

    def apply_step(self, step) -> dict[str, object]:
        return self._apply_step(step)

    def apply_step_checkpointed(self, step, expected_prior, creation_checkpoint):
        assert self.inspect_step(step) == expected_prior
        return self._apply_step(step, creation_checkpoint)

    def replace_step_checkpointed(
        self, step, expected_prior, replacement_checkpoint
    ):
        assert self.inspect_step(step) == expected_prior
        file_type = (
            "directory"
            if step.get("kind") == "repository"
            else step.get("asset_type", "file")
        )
        authority = {
            "device": 1,
            "inode": len(self.creation_identities) + 1000,
            "file_type": file_type,
        }
        self.creation_identities[step["step_id"]] = authority
        replacement_checkpoint(authority)
        outcome = self._apply_step(step)
        return {"replacement_authority": authority, **outcome}

    def creation_authority_matches(self, step, authority) -> bool:
        return authority == self.creation_identities.get(step["step_id"])

    def cleanup_prepared_replacement(self, _entry) -> None:
        return None

    def rollback_step(self, entry) -> None:
        self.rolled_back.append(entry["step_id"])
        prior = deepcopy(entry["prior"])
        if prior.get("exists"):
            self.states[entry["step_id"]] = prior
        else:
            self.states.pop(entry["step_id"], None)
            self.creation_identities.pop(entry["step_id"], None)

    def list_unknown_state(self, _receipt) -> tuple[str, ...]:
        return tuple(sorted(self.unknown))

    def stop_service(self, name: str) -> None:
        self.stopped.append(name)
        if self.fail_stop == name:
            raise RuntimeError(f"injected stop failure: {name}")

    def rollback_credentials(self, _receipt):
        self.credential_rollbacks += 1
        return ()


class VenvTransactionBackend(RecordingBackend):
    """Model the retained content-addressed slot and mutable active link."""

    def __init__(self, plan: dict[str, object]) -> None:
        super().__init__(plan)
        self.slot_exists = False
        self.tree_sha256 = "e" * 64

    def inspect_step(self, step) -> dict[str, object]:
        if step.get("kind") != "venv":
            return super().inspect_step(step)
        if not self.slot_exists:
            return {"exists": False}
        state = {
            "exists": True,
            "installed_sha256": None,
            "slot_sha256": step["desired_sha256"],
            "path": step["path"],
            "tree_sha256": self.tree_sha256,
        }
        if self.venv_activation == {
            "exists": True,
            "is_symlink": True,
            "link_target": "venvs/candidate",
        }:
            state.update(
                {
                    "installed_sha256": step["desired_sha256"],
                    "link_target": "venvs/candidate",
                }
            )
        return state

    def apply_step(self, step) -> dict[str, object]:
        prior = self.inspect_step(step)
        self.slot_exists = True
        self.venv_activation = {
            "exists": True,
            "is_symlink": True,
            "link_target": "venvs/candidate",
        }
        self.applied.append(step["step_id"])
        return {"prior": prior, **self.inspect_step(step)}

    def apply_step_checkpointed(self, step, expected_prior, _creation_checkpoint):
        assert self.inspect_step(step) == expected_prior
        prior = self.inspect_step(step)
        self.slot_exists = True
        _creation_checkpoint(
            {"path": step["path"], "tree_sha256": self.tree_sha256}
        )
        self.venv_activation = {
            "exists": True,
            "is_symlink": True,
            "link_target": "venvs/candidate",
        }
        self.applied.append(step["step_id"])
        return {"prior": prior, **self.inspect_step(step)}

    def rollback_step(self, entry) -> None:
        self.rolled_back.append(entry["step_id"])
        self.venv_activation = deepcopy(entry["prior"])


def _venv_plan(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    plan = _plan(tmp_path)
    step = plan["apply_order"][0]
    step.clear()
    step.update(
        {
            "step_id": "candidate-venv",
            "kind": "venv",
            "path": str(tmp_path / "target/candidate-venv"),
            "active_link": str(tmp_path / "target/venv"),
            "wheel_sha256": "1" * 64,
            "wheel_source": str(tmp_path / "candidate.whl"),
            "wheelhouse": [],
            "wheelhouse_locked": True,
            "operations": [],
            "desired_sha256": "1" * 64,
        }
    )
    plan["accounts"] = []
    plan["apply_order"] = [step]
    return plan, step


def test_preflight_uses_explicit_backend_facts_not_the_host(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    facts = _safe_facts(plan)

    report = validate_preflight(plan, facts)

    assert report.ok
    assert report.failures == ()


def test_systemctl_transport_failure_blocks_before_first_apply_step(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    result = subprocess.CompletedProcess(
        ["systemctl", "is-active", "cortex-manager.service"],
        1,
        stdout="",
        stderr="Failed to connect to bus",
    )
    backend.facts["services"]["cortex-manager.service"] = (
        install_backend._classify_systemctl_is_active(result)
    )
    receipt = new_install_receipt(plan)

    with pytest.raises(InstallError, match="service status could not be proven"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    assert backend.applied == []


def test_existing_account_collision_fails_before_any_mutation(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    facts = _safe_facts(plan)
    manager = dict(plan["accounts"][0])
    manager["uid"] = 65530
    facts["accounts"]["cortex-manager"] = manager

    with pytest.raises(AccountCollisionError, match="cortex-manager"):
        validate_preflight(plan, facts)


def _account_adoption_case(tmp_path: Path):
    desired = dict(_accounts(tmp_path)[0])
    step = {
        "step_id": f"account:{desired['name']}",
        "kind": "account",
        "name": desired["name"],
        "uid": desired["uid"],
        "gid": desired["gid"],
        "home": desired["home"],
        "login_program": desired["shell"],
        "desired_sha256": "9" * 64,
    }
    plan = _plan(tmp_path)
    plan["accounts"] = [desired]
    plan["apply_order"] = [step]
    facts = _safe_facts(plan)
    facts["accounts"] = {
        desired["name"]: {
            **desired,
            "supplementary_groups": [],
            "password_locked": True,
        }
    }
    receipt = new_install_receipt(plan)
    document = receipt.to_dict()
    document["journal"] = [
        {
            "step_id": step["step_id"],
            "step": deepcopy(step),
            "status": "completed",
            "prior": {"exists": False},
            "exists": True,
            "installed_sha256": step["desired_sha256"],
        }
    ]
    trusted_receipt = InstallReceipt(document)
    return plan, facts, trusted_receipt


def test_existing_account_with_unknown_password_lock_state_is_rejected(
    tmp_path: Path,
) -> None:
    plan, facts, receipt = _account_adoption_case(tmp_path)
    facts["accounts"]["cortex-manager"]["password_locked"] = None

    with pytest.raises(AccountCollisionError, match="password.*locked"):
        validate_preflight(plan, facts, receipt=receipt)


def test_existing_account_without_prior_receipt_provenance_is_rejected(
    tmp_path: Path,
) -> None:
    plan, facts, _receipt = _account_adoption_case(tmp_path)

    with pytest.raises(AccountCollisionError, match="receipt|provenance"):
        validate_preflight(plan, facts)


def test_exact_orphan_group_without_receipt_provenance_is_rejected(
    tmp_path: Path,
) -> None:
    plan, facts, _receipt = _account_adoption_case(tmp_path)
    desired = plan["accounts"][0]
    name = desired["name"]
    gid = desired["gid"]
    facts["accounts"] = {}
    facts["account_uids"] = {}
    facts["group_gids"] = {gid: name}
    facts["groups"] = {name: {"name": name, "gid": gid, "members": []}}
    facts["primary_gid_users"] = {gid: []}
    facts["group_names_by_gid"] = {gid: [name]}

    with pytest.raises(AccountCollisionError, match="group.*receipt|provenance"):
        validate_preflight(plan, facts)


def test_same_receipt_replays_account_after_groupadd_crash(tmp_path: Path) -> None:
    plan, base_facts, _receipt = _account_adoption_case(tmp_path)
    desired = plan["accounts"][0]
    step = plan["apply_order"][0]
    name = desired["name"]
    gid = desired["gid"]

    class GroupAddCrashBackend:
        def __init__(self) -> None:
            self.group_exists = False
            self.account_exists = False
            self.apply_attempts = 0
            self.rollback_attempts = 0

        def preflight_facts(self, _plan):
            facts = deepcopy(base_facts)
            facts["accounts"] = {}
            facts["account_uids"] = {}
            facts["group_gids"] = {gid: name} if self.group_exists else {}
            facts["groups"] = (
                {name: {"name": name, "gid": gid, "members": []}}
                if self.group_exists
                else {}
            )
            facts["primary_gid_users"] = {gid: [name]} if self.account_exists else {}
            facts["group_names_by_gid"] = {gid: [name]} if self.group_exists else {}
            if self.account_exists:
                facts["accounts"][name] = {
                    **desired,
                    "supplementary_groups": [],
                    "password_locked": True,
                }
                facts["account_uids"][desired["uid"]] = name
            return facts

        def inspect_step(self, _step):
            if self.account_exists:
                return {
                    "exists": True,
                    "installed_sha256": step["desired_sha256"],
                }
            return {
                "exists": False,
                "group_exists": self.group_exists,
                **({"group_gid": gid, "group_members": []} if self.group_exists else {}),
            }

        def apply_step(self, _step):
            self.apply_attempts += 1
            prior = self.inspect_step(_step)
            if not self.group_exists:
                self.group_exists = True
                raise RuntimeError("injected crash after groupadd")
            self.account_exists = True
            return {"prior": prior, **self.inspect_step(_step)}

        def rollback_step(self, _entry):
            self.rollback_attempts += 1

        def list_unknown_state(self, _receipt):
            return ()

    backend = GroupAddCrashBackend()
    receipt = new_install_receipt(plan)

    with pytest.raises(RuntimeError, match="groupadd"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    prepared = receipt.to_dict()["journal"][0]
    assert prepared["status"] == "prepared"
    assert prepared["prior"] == {"exists": False, "group_exists": False}

    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )

    assert backend.apply_attempts == 2
    assert backend.rollback_attempts == 0
    assert receipt.to_dict()["state"] == "applied"


def test_existing_account_with_matching_prior_receipt_provenance_is_adoptable(
    tmp_path: Path,
) -> None:
    plan, facts, receipt = _account_adoption_case(tmp_path)

    assert validate_preflight(plan, facts, receipt=receipt).ok


def test_exact_prior_plan_receipt_can_handoff_account_provenance(
    tmp_path: Path,
) -> None:
    prior_plan, facts, prior_receipt = _account_adoption_case(tmp_path)
    prior_receipt._document["state"] = "applied"
    prior_receipt._document["qualified"] = True
    next_plan = deepcopy(prior_plan)
    next_plan["repo_identity"]["commit"] = "b" * 40
    next_plan["candidate"]["wheel_sha256"] = "d" * 64
    next_receipt = new_install_receipt(next_plan)

    report = validate_preflight(
        next_plan,
        facts,
        receipt=next_receipt,
        prior_receipt=prior_receipt,
    )

    assert report.ok


def test_prior_receipt_cannot_handoff_changed_account_identity(
    tmp_path: Path,
) -> None:
    prior_plan, facts, prior_receipt = _account_adoption_case(tmp_path)
    prior_receipt._document["state"] = "applied"
    prior_receipt._document["qualified"] = True
    next_plan = deepcopy(prior_plan)
    next_plan["repo_identity"]["commit"] = "b" * 40
    next_plan["candidate"]["wheel_sha256"] = "d" * 64
    next_plan["accounts"][0]["uid"] += 1
    next_plan["apply_order"][0]["uid"] += 1
    next_receipt = new_install_receipt(next_plan)

    with pytest.raises(AccountCollisionError, match="does not match|provenance"):
        validate_preflight(
            next_plan,
            facts,
            receipt=next_receipt,
            prior_receipt=prior_receipt,
        )


def test_prior_receipt_handoff_adopts_unchanged_asset_and_updates_changed_asset(
    tmp_path: Path,
) -> None:
    prior_plan = _plan(tmp_path)
    prior_receipt = new_install_receipt(prior_plan)
    prior_receipt._document["state"] = "applied"
    prior_receipt._document["qualified"] = True
    prior_receipt._document["journal"] = [
        {
            "step_id": step["step_id"],
            "step": deepcopy(step),
            "status": "completed",
            "prior": {"exists": False},
            "exists": True,
            "installed_sha256": step["desired_sha256"],
        }
        for step in prior_plan["apply_order"]
    ]
    next_plan = deepcopy(prior_plan)
    next_plan["repo_identity"]["commit"] = "b" * 40
    next_plan["candidate"]["wheel_sha256"] = "d" * 64
    next_plan["apply_order"][1]["desired_sha256"] = "3" * 64
    next_receipt = new_install_receipt(next_plan)
    backend = RecordingBackend(next_plan)
    for step in prior_plan["apply_order"]:
        backend.states[step["step_id"]] = {
            "exists": True,
            "installed_sha256": step["desired_sha256"],
            "owner": step["owner"],
            "group": step["group"],
            "mode": step["mode"],
            "acl": deepcopy(step["acls"]),
        }

    apply_plan(
        next_plan,
        confirm_sha256=plan_sha256(next_plan),
        receipt=next_receipt,
        prior_receipt=prior_receipt,
        backend=backend,
    )

    journal = next_receipt.to_dict()["journal"]
    assert journal[0]["step_id"] == "state-root"
    assert journal[0]["adopted_from_receipt"] is True
    assert journal[0]["prior"]["installed_sha256"] == "1" * 64
    assert backend.states["manager-unit"]["installed_sha256"] == "3" * 64


def test_prior_receipt_handoff_rejects_another_install_root_before_mutation(
    tmp_path: Path,
) -> None:
    prior_plan = _plan(tmp_path)
    prior_receipt = new_install_receipt(prior_plan)
    prior_receipt._document["state"] = "applied"
    prior_receipt._document["qualified"] = True
    next_plan = deepcopy(prior_plan)
    next_plan["roots"]["state"] += "-other"
    next_receipt = new_install_receipt(next_plan)
    backend = RecordingBackend(next_plan)

    with pytest.raises(InstallPlanError, match="prior receipt|install root"):
        apply_plan(
            next_plan,
            confirm_sha256=plan_sha256(next_plan),
            receipt=next_receipt,
            prior_receipt=prior_receipt,
            backend=backend,
        )

    assert backend.applied == []


def test_prior_receipt_handoff_rejects_late_foreign_asset_before_mutation(
    tmp_path: Path,
) -> None:
    prior_plan = _plan(tmp_path)
    prior_receipt = new_install_receipt(prior_plan)
    prior_receipt._document["state"] = "applied"
    prior_receipt._document["qualified"] = True
    prior_receipt._document["journal"] = [
        {
            "step_id": step["step_id"],
            "step": deepcopy(step),
            "status": "completed",
            "prior": {"exists": False},
            "exists": True,
            "installed_sha256": step["desired_sha256"],
        }
        for step in prior_plan["apply_order"]
    ]
    next_plan = deepcopy(prior_plan)
    next_plan["repo_identity"]["commit"] = "b" * 40
    next_plan["candidate"]["wheel_sha256"] = "d" * 64
    next_plan["apply_order"][1]["desired_sha256"] = "3" * 64
    next_receipt = new_install_receipt(next_plan)
    backend = RecordingBackend(next_plan)
    for step in prior_plan["apply_order"]:
        backend.states[step["step_id"]] = {
            "exists": True,
            "installed_sha256": step["desired_sha256"],
            "owner": step["owner"],
            "group": step["group"],
            "mode": step["mode"],
            "acl": deepcopy(step["acls"]),
        }
    backend.states["manager-unit"]["installed_sha256"] = "9" * 64

    with pytest.raises(InstallDriftError, match="prior receipt provenance"):
        apply_plan(
            next_plan,
            confirm_sha256=plan_sha256(next_plan),
            receipt=next_receipt,
            prior_receipt=prior_receipt,
            backend=backend,
        )

    assert backend.applied == []
    assert next_receipt.to_dict()["state"] == "planned"
    assert next_receipt.to_dict()["journal"] == []


def test_late_toolchain_drift_is_rejected_before_an_earlier_step_mutates(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    toolchain = plan["apply_order"][1]
    toolchain.update(
        {
            "kind": "toolchain",
            "name": "codex",
            "shape": "file",
            "source": str(tmp_path / "codex.source"),
            "source_sha256": "2" * 64,
        }
    )
    backend = RecordingBackend(plan)
    backend.states[toolchain["step_id"]] = {
        "exists": True,
        "installed_sha256": "9" * 64,
        "owner": toolchain["owner"],
        "group": toolchain["group"],
        "mode": toolchain["mode"],
    }
    receipt = new_install_receipt(plan)

    with pytest.raises(InstallDriftError, match="cannot be upgraded in place"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    assert backend.applied == []
    assert receipt.to_dict()["state"] == "planned"


def test_changed_receipt_proven_toolchain_is_rejected_before_earlier_mutation(
    tmp_path: Path,
) -> None:
    prior_plan = _plan(tmp_path)
    toolchain = prior_plan["apply_order"][1]
    toolchain.update(
        {
            "kind": "toolchain",
            "name": "codex",
            "shape": "file",
            "source": str(tmp_path / "codex-old.source"),
            "source_sha256": toolchain["desired_sha256"],
        }
    )
    prior_receipt = new_install_receipt(prior_plan)
    prior_receipt._document["state"] = "applied"
    prior_receipt._document["qualified"] = True
    prior_receipt._document["journal"] = [
        {
            "step_id": step["step_id"],
            "step": deepcopy(step),
            "status": "completed",
            "prior": {"exists": False},
            "exists": True,
            "installed_sha256": step["desired_sha256"],
            "owner": step["owner"],
            "group": step["group"],
            "mode": step["mode"],
            "acl": deepcopy(step["acls"]),
        }
        for step in prior_plan["apply_order"]
    ]
    next_plan = deepcopy(prior_plan)
    next_plan["repo_identity"]["commit"] = "b" * 40
    next_plan["candidate"]["wheel_sha256"] = "d" * 64
    next_plan["apply_order"][0]["desired_sha256"] = "3" * 64
    next_toolchain = next_plan["apply_order"][1]
    next_toolchain["desired_sha256"] = "4" * 64
    next_toolchain["source_sha256"] = "4" * 64
    next_toolchain["source"] = str(tmp_path / "codex-new.source")
    receipt = new_install_receipt(next_plan)
    backend = RecordingBackend(next_plan)
    for step in prior_plan["apply_order"]:
        backend.states[step["step_id"]] = {
            "exists": True,
            "installed_sha256": step["desired_sha256"],
            "owner": step["owner"],
            "group": step["group"],
            "mode": step["mode"],
            "acl": deepcopy(step["acls"]),
        }

    with pytest.raises(InstallDriftError, match="cannot be upgraded in place"):
        apply_plan(
            next_plan,
            confirm_sha256=plan_sha256(next_plan),
            receipt=receipt,
            prior_receipt=prior_receipt,
            backend=backend,
        )

    assert backend.applied == []
    assert receipt.to_dict()["state"] == "planned"
    assert receipt.to_dict()["journal"] == []


def test_first_install_refuses_exact_preexisting_toolchain_without_provenance(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    toolchain = plan["apply_order"][0]
    toolchain.update(
        {
            "kind": "toolchain",
            "name": "codex",
            "shape": "file",
            "source": str(tmp_path / "codex.source"),
            "source_sha256": toolchain["desired_sha256"],
        }
    )
    plan["accounts"] = []
    plan["apply_order"] = [toolchain]
    backend = RecordingBackend(plan)
    backend.states[toolchain["step_id"]] = {
        "exists": True,
        "installed_sha256": toolchain["desired_sha256"],
        "owner": toolchain["owner"],
        "group": toolchain["group"],
        "mode": toolchain["mode"],
        "acl": deepcopy(toolchain["acls"]),
    }
    receipt = new_install_receipt(plan)

    with pytest.raises(InstallDriftError, match="trusted receipt provenance"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    assert backend.applied == []
    assert receipt.to_dict()["state"] == "planned"


def test_foreign_active_venv_is_rejected_before_an_earlier_step_mutates(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    venv = plan["apply_order"][1]
    venv.update(
        {
            "kind": "venv",
            "active_link": str(tmp_path / "target" / "venv"),
            "wheel_sha256": venv["desired_sha256"],
            "wheel_source": str(tmp_path / "candidate.whl"),
            "wheelhouse": [],
            "wheelhouse_locked": True,
        }
    )
    backend = RecordingBackend(plan)
    backend.venv_activation = {
        "exists": True,
        "is_symlink": True,
        "link_target": "venvs/foreign",
    }
    receipt = new_install_receipt(plan)

    with pytest.raises(InstallDriftError, match="prior receipt provenance"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    assert backend.applied == []
    assert receipt.to_dict()["state"] == "planned"


def test_first_install_refuses_a_preexisting_candidate_venv_slot(
    tmp_path: Path,
) -> None:
    plan, _step = _venv_plan(tmp_path)
    backend = VenvTransactionBackend(plan)
    backend.slot_exists = True
    receipt = new_install_receipt(plan)

    with pytest.raises(InstallDriftError, match="slot lacks trusted receipt"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    assert backend.applied == []
    assert receipt.to_dict()["journal"] == []


def test_prior_venv_provenance_binds_the_observed_tree_to_candidate_receipt(
    tmp_path: Path,
) -> None:
    prior_plan, step = _venv_plan(tmp_path)
    prior_receipt = new_install_receipt(prior_plan)
    prior_receipt._document["state"] = "applied"
    prior_receipt._document["qualified"] = True
    prior_receipt._document["candidate_venv"] = {
        "path": step["path"],
        "tree_sha256": "d" * 64,
    }
    prior_receipt._document["journal"] = [
        {
            "step_id": step["step_id"],
            "step": deepcopy(step),
            "status": "completed",
            "prior": {"exists": False},
            "exists": True,
            "installed_sha256": step["desired_sha256"],
            "path": step["path"],
            "tree_sha256": "d" * 64,
        }
    ]
    next_plan = deepcopy(prior_plan)
    next_plan["repo_identity"]["commit"] = "b" * 40
    next_plan["candidate"]["wheel_sha256"] = "2" * 64

    class DriftedPriorTreeBackend:
        def inspect_step(self, _step):
            return {
                "exists": True,
                "installed_sha256": step["desired_sha256"],
                "path": step["path"],
                "tree_sha256": "f" * 64,
            }

    provenance, _prior_step = install_core._prior_venv_provenance(
        plan=next_plan,
        step=next_plan["apply_order"][0],
        prior_receipt=prior_receipt,
        backend=DriftedPriorTreeBackend(),
    )

    assert provenance is False


def test_venv_rollback_forgets_journal_after_restoring_link_and_retaining_slot(
    tmp_path: Path,
) -> None:
    plan, _step = _venv_plan(tmp_path)
    backend = VenvTransactionBackend(plan)
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )

    report = rollback_receipt(receipt, backend=backend)

    assert report.retained_drift == ()
    assert backend.slot_exists is True
    assert backend.venv_activation == {"exists": False}
    assert receipt.to_dict()["journal"] == []
    assert receipt.to_dict()["state"] == "rolled-back"


def test_venv_post_cutover_crash_rolls_back_with_durable_slot_authority(
    tmp_path: Path,
) -> None:
    plan, step = _venv_plan(tmp_path)

    class PostCutoverCrashBackend(VenvTransactionBackend):
        def apply_step_checkpointed(
            self, candidate, expected_prior, creation_checkpoint
        ):
            assert self.inspect_step(candidate) == expected_prior
            self.slot_exists = True
            creation_checkpoint(
                {"path": candidate["path"], "tree_sha256": self.tree_sha256}
            )
            self.venv_activation = {
                "exists": True,
                "is_symlink": True,
                "link_target": "venvs/candidate",
            }
            raise SystemExit("simulated crash after active-link cutover")

    backend = PostCutoverCrashBackend(plan)
    receipt = new_install_receipt(plan)

    with pytest.raises(SystemExit, match="active-link cutover"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    prepared = receipt.to_dict()["journal"][0]
    assert prepared["status"] == "prepared"
    assert prepared["venv_slot_authority"] == {
        "path": step["path"],
        "tree_sha256": backend.tree_sha256,
    }

    report = rollback_receipt(receipt, backend=backend)

    assert report.retained_drift == ()
    assert backend.venv_activation == {"exists": False}
    assert backend.slot_exists is True
    assert receipt.to_dict()["journal"] == []
    assert receipt.to_dict()["state"] == "rolled-back"


def test_venv_reapply_uses_receipt_bound_retained_slot_after_rollback(
    tmp_path: Path,
) -> None:
    plan, _step = _venv_plan(tmp_path)
    backend = VenvTransactionBackend(plan)
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    rollback_receipt(receipt, backend=backend)

    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )

    document = receipt.to_dict()
    assert document["state"] == "applied"
    assert document["journal"][0]["adopted_from_receipt"] is True
    assert backend.venv_activation["link_target"] == "venvs/candidate"


def test_venv_rollback_reports_a_tampered_retained_slot_even_if_link_is_restored(
    tmp_path: Path,
) -> None:
    plan, _step = _venv_plan(tmp_path)
    backend = VenvTransactionBackend(plan)
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    backend.venv_activation = {"exists": False}
    backend.tree_sha256 = "f" * 64

    report = rollback_receipt(receipt, backend=backend)

    assert report.retained_drift[0]["step_id"] == "candidate-venv"
    assert receipt.to_dict()["journal"][0]["step_id"] == "candidate-venv"
    assert receipt.to_dict()["state"] == "rollback-blocked"


def test_retained_account_from_plan_bound_rollback_journal_can_be_reinstalled(
    tmp_path: Path,
) -> None:
    plan, facts, receipt = _account_adoption_case(tmp_path)
    step = plan["apply_order"][0]

    class RetainedAccountBackend:
        def __init__(self) -> None:
            self.applied = 0
            self.rolled_back = 0
            self.state = {
                "exists": True,
                "installed_sha256": step["desired_sha256"],
            }

        def preflight_facts(self, _plan):
            return deepcopy(facts)

        def inspect_step(self, _step):
            return deepcopy(self.state)

        def apply_step(self, _step):
            self.applied += 1
            return {"prior": deepcopy(self.state), **deepcopy(self.state)}

        def rollback_step(self, _entry):
            # Account rollback is deliberately receipt-bounded retain/no-op.
            self.rolled_back += 1

        def list_unknown_state(self, _receipt):
            return ()

    backend = RetainedAccountBackend()

    rollback_receipt(receipt, backend=backend)
    rolled_back = receipt.to_dict()
    assert rolled_back["journal"] == []
    assert rolled_back["rollback_journal"][0]["step_id"] == step["step_id"]

    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )

    assert backend.rolled_back == 1
    assert backend.applied == 1
    assert receipt.to_dict()["state"] == "applied"


@pytest.mark.parametrize("kind", ["asset", "repository"])
def test_first_install_refuses_exact_preexisting_state_without_receipt_provenance(
    tmp_path: Path, kind: str
) -> None:
    plan = _plan(tmp_path)
    step = plan["apply_order"][0]
    step["kind"] = kind
    plan["apply_order"] = [step]
    backend = RecordingBackend(plan)
    backend.states[step["step_id"]] = {
        "exists": True,
        "installed_sha256": step["desired_sha256"],
        "owner": step["owner"],
        "group": step["group"],
        "mode": step["mode"],
        "acl": deepcopy(step["acls"]),
    }
    receipt = new_install_receipt(plan)

    with pytest.raises(InstallDriftError, match="receipt|provenance"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    assert backend.applied == []
    assert receipt.to_dict()["journal"] == []


def test_first_install_adopts_exact_empty_managed_state_mount(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    step = plan["apply_order"][0]
    step.update(
        {
            "asset_type": "directory",
            "adoption_policy": "empty-managed-root-mount",
            "durable": True,
        }
    )
    plan["roots"]["state"] = step["path"]
    plan["apply_order"] = [step]
    backend = RecordingBackend(plan)
    backend.states[step["step_id"]] = {
        "exists": True,
        "installed_sha256": step["desired_sha256"],
        "owner": step["owner"],
        "group": step["group"],
        "mode": step["mode"],
        "acl": deepcopy(step["acls"]),
        "is_mountpoint": True,
        "device": 8,
        "inode": 42,
        "children": [],
    }
    receipt = new_install_receipt(plan)

    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )

    assert backend.applied == [step["step_id"]]
    assert receipt.to_dict()["journal"][0]["adopted_mount_root"] == {
        "device": 8,
        "inode": 42,
    }

    rollback_receipt(receipt, backend=backend)
    retained = deepcopy(backend.states[step["step_id"]])
    retained["children"] = ["durable-child"]
    backend.states[step["step_id"]] = retained

    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )

    assert backend.applied == [step["step_id"], step["step_id"]]
    assert receipt.to_dict()["journal"][0]["adopted_from_receipt"] is True


def test_metadata_upgrade_carries_mount_inode_authority_to_next_upgrade(
    tmp_path: Path,
) -> None:
    prior_plan = _plan(tmp_path)
    prior_step = prior_plan["apply_order"][0]
    prior_step.update(
        {
            "asset_type": "directory",
            "adoption_policy": "empty-managed-root-mount",
            "durable": True,
        }
    )
    prior_plan["roots"]["state"] = prior_step["path"]
    prior_plan["apply_order"] = [prior_step]
    prior_receipt = new_install_receipt(prior_plan)
    prior_receipt._document.update({"state": "applied", "qualified": True})
    prior_receipt._document["journal"] = [
        {
            "step_id": prior_step["step_id"],
            "step": deepcopy(prior_step),
            "status": "completed",
            "prior": {"exists": True},
            "adopted_mount_root": {"device": 8, "inode": 42},
            "exists": True,
            "installed_sha256": prior_step["desired_sha256"],
        }
    ]

    class MountBackend(RecordingBackend):
        def _apply_step(self, step, creation_checkpoint=None):
            identity = {
                key: self.states[step["step_id"]][key]
                for key in ("is_mountpoint", "device", "inode", "children")
            }
            outcome = super()._apply_step(step, creation_checkpoint)
            self.states[step["step_id"]].update(identity)
            return {**outcome, **identity}

    next_plan = deepcopy(prior_plan)
    next_plan["repo_identity"]["commit"] = "b" * 40
    next_plan["candidate"]["wheel_sha256"] = "d" * 64
    next_step = next_plan["apply_order"][0]
    next_step["mode"] = "0700"
    next_step["desired_sha256"] = "3" * 64
    backend = MountBackend(next_plan)
    backend.states[next_step["step_id"]] = {
        "exists": True,
        "installed_sha256": prior_step["desired_sha256"],
        "owner": prior_step["owner"],
        "group": prior_step["group"],
        "mode": prior_step["mode"],
        "acl": deepcopy(prior_step["acls"]),
        "is_mountpoint": True,
        "device": 8,
        "inode": 42,
        "children": [],
    }
    next_receipt = new_install_receipt(next_plan)

    apply_plan(
        next_plan,
        confirm_sha256=plan_sha256(next_plan),
        receipt=next_receipt,
        prior_receipt=prior_receipt,
        backend=backend,
    )

    assert next_receipt.to_dict()["journal"][0]["adopted_mount_root"] == {
        "device": 8,
        "inode": 42,
    }
    next_receipt._document.update({"state": "applied", "qualified": True})

    third_plan = deepcopy(next_plan)
    third_plan["repo_identity"]["commit"] = "c" * 40
    third_plan["candidate"]["wheel_sha256"] = "e" * 64
    third_plan["apply_order"][0]["mode"] = "0750"
    third_plan["apply_order"][0]["desired_sha256"] = "4" * 64
    third_receipt = new_install_receipt(third_plan)
    foreign = deepcopy(backend.states[next_step["step_id"]])
    foreign.update({"device": 9, "inode": 84})
    backend.states[next_step["step_id"]] = foreign

    with pytest.raises(InstallDriftError, match="prior receipt provenance"):
        apply_plan(
            third_plan,
            confirm_sha256=plan_sha256(third_plan),
            receipt=third_receipt,
            prior_receipt=next_receipt,
            backend=backend,
        )

    assert third_receipt.to_dict()["journal"] == []


@pytest.mark.parametrize(
    ("is_mountpoint", "children"),
    [(False, []), (True, ["foreign-state"])],
)
def test_first_install_refuses_unqualified_managed_state_root_adoption(
    tmp_path: Path, is_mountpoint: bool, children: list[str]
) -> None:
    plan = _plan(tmp_path)
    step = plan["apply_order"][0]
    step.update(
        {
            "asset_type": "directory",
            "adoption_policy": "empty-managed-root-mount",
            "durable": True,
        }
    )
    plan["roots"]["state"] = step["path"]
    plan["apply_order"] = [step]
    backend = RecordingBackend(plan)
    backend.states[step["step_id"]] = {
        "exists": True,
        "installed_sha256": step["desired_sha256"],
        "owner": step["owner"],
        "group": step["group"],
        "mode": step["mode"],
        "acl": deepcopy(step["acls"]),
        "is_mountpoint": is_mountpoint,
        "device": 8,
        "inode": 42,
        "children": children,
    }
    receipt = new_install_receipt(plan)

    with pytest.raises(InstallDriftError, match="receipt|provenance"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    assert backend.applied == []
    assert receipt.to_dict()["journal"] == []


def test_default_receipt_bootstrap_is_the_only_allowed_nonempty_mount(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    step = plan["apply_order"][0]
    step.update(
        {
            "asset_type": "directory",
            "adoption_policy": "empty-managed-root-mount",
            "durable": True,
        }
    )
    plan["roots"]["state"] = step["path"]
    plan["apply_order"] = [step]
    receipt_path = Path(str(step["path"])) / "install-receipts/install.json"
    plan["receipt_path"] = str(receipt_path)
    receipt = InstallReceipt(new_install_receipt(plan).to_dict(), path=receipt_path)
    installed = {
        "is_mountpoint": True,
        "device": 8,
        "inode": 42,
        "children": ["install-receipts", "install-receipts/install.json"],
    }

    assert install_core._explicit_empty_managed_mount_is_adoptable(
        plan=plan,
        step=step,
        installed=installed,
        receipt=receipt,
    )
    installed["children"].append("foreign-state")
    assert not install_core._explicit_empty_managed_mount_is_adoptable(
        plan=plan,
        step=step,
        installed=installed,
        receipt=receipt,
    )


@pytest.mark.parametrize("is_mountpoint", [False, True])
def test_mount_adoption_provenance_rejects_replaced_root(
    tmp_path: Path, is_mountpoint: bool
) -> None:
    plan = _plan(tmp_path)
    step = plan["apply_order"][0]
    step.update(
        {
            "asset_type": "directory",
            "adoption_policy": "empty-managed-root-mount",
            "durable": True,
        }
    )
    plan["roots"]["state"] = step["path"]
    plan["apply_order"] = [step]
    backend = RecordingBackend(plan)
    backend.states[step["step_id"]] = {
        "exists": True,
        "installed_sha256": step["desired_sha256"],
        "owner": step["owner"],
        "group": step["group"],
        "mode": step["mode"],
        "acl": deepcopy(step["acls"]),
        "is_mountpoint": True,
        "device": 8,
        "inode": 42,
        "children": [],
    }
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    rollback_receipt(receipt, backend=backend)
    backend.states[step["step_id"]] = {
        "exists": True,
        "installed_sha256": step["desired_sha256"],
        "owner": step["owner"],
        "group": step["group"],
        "mode": step["mode"],
        "acl": deepcopy(step["acls"]),
        "is_mountpoint": is_mountpoint,
        "device": 9,
        "inode": 84,
        "children": ["foreign-state"],
    }

    with pytest.raises(InstallDriftError, match="receipt|provenance"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    assert backend.applied == [step["step_id"]]
    assert receipt.to_dict()["journal"] == []


@pytest.mark.parametrize("status", ["prepared", "completed"])
@pytest.mark.parametrize("is_mountpoint", [False, True])
def test_mount_adoption_journal_revalidates_inode_before_short_circuit(
    tmp_path: Path, status: str, is_mountpoint: bool
) -> None:
    plan = _plan(tmp_path)
    step = plan["apply_order"][0]
    step.update(
        {
            "asset_type": "directory",
            "adoption_policy": "empty-managed-root-mount",
            "durable": True,
        }
    )
    plan["roots"]["state"] = step["path"]
    plan["apply_order"] = [step]
    backend = RecordingBackend(plan)
    exact_state = {
        "exists": True,
        "installed_sha256": step["desired_sha256"],
        "owner": step["owner"],
        "group": step["group"],
        "mode": step["mode"],
        "acl": deepcopy(step["acls"]),
        "is_mountpoint": True,
        "device": 8,
        "inode": 42,
        "children": [],
    }
    backend.states[step["step_id"]] = deepcopy(exact_state)
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    receipt._document["journal"][0]["status"] = status
    backend.states[step["step_id"]] = {
        **exact_state,
        "is_mountpoint": is_mountpoint,
        "device": 9,
        "inode": 84,
    }

    with pytest.raises(InstallDriftError, match="mount.*authority|adopted.*mount"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    assert backend.applied == [step["step_id"]]


@pytest.mark.parametrize("kind", ["asset", "repository"])
def test_exact_retained_state_with_plan_bound_rollback_provenance_is_adoptable(
    tmp_path: Path, kind: str
) -> None:
    plan = _plan(tmp_path)
    step = plan["apply_order"][0]
    step["kind"] = kind
    plan["apply_order"] = [step]
    state = {
        "exists": True,
        "installed_sha256": step["desired_sha256"],
        "owner": step["owner"],
        "group": step["group"],
        "mode": step["mode"],
        "acl": deepcopy(step["acls"]),
    }
    backend = RecordingBackend(plan)
    backend.states[step["step_id"]] = deepcopy(state)
    document = new_install_receipt(plan).to_dict()
    document["rollback_journal"] = [
        {
            "step_id": step["step_id"],
            "step": deepcopy(step),
            "status": "completed",
            "prior": {"exists": False},
            **state,
        }
    ]
    receipt = InstallReceipt(document)

    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )

    assert backend.applied == [step["step_id"]]
    assert receipt.to_dict()["state"] == "applied"

    rollback_receipt(receipt, backend=backend)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )

    assert backend.applied == [step["step_id"], step["step_id"]]
    assert receipt.to_dict()["journal"][0]["adopted_from_receipt"] is True


def test_apply_time_symlink_drift_fails_before_any_mutation(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    first_path = plan["apply_order"][0]["path"]
    backend.facts["paths"][first_path] = {"exists": True, "is_symlink": True}
    receipt = new_install_receipt(plan)

    with pytest.raises(UnsafeInstallPathError, match="symlink"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )
    assert backend.applied == []


def test_apply_requires_the_exact_canonical_plan_hash(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    receipt = new_install_receipt(plan)

    with pytest.raises(InstallPlanError, match="sha256"):
        apply_plan(plan, confirm_sha256="0" * 64, receipt=receipt, backend=backend)
    assert backend.applied == []


def test_acl_is_always_applied_after_chown_and_chmod(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bad = deepcopy(plan)
    bad["apply_order"][0]["operations"] = ["snapshot", "chown", "set_acl", "chmod"]
    backend = RecordingBackend(bad)

    with pytest.raises(InstallPlanError, match="ACL|acl|chmod"):
        apply_plan(
            bad,
            confirm_sha256=plan_sha256(bad),
            receipt=new_install_receipt(bad),
            backend=backend,
        )
    assert backend.applied == []


def test_interrupted_apply_replays_completed_step_and_continues_remaining_once(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    backend.fail_on = "manager-unit"
    receipt = new_install_receipt(plan)

    with pytest.raises(RuntimeError, match="injected interruption"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    after_interrupt = receipt.to_dict()
    assert [row["step_id"] for row in after_interrupt["journal"]] == ["state-root"]
    assert after_interrupt["state"] == "applying"

    completed = apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    assert backend.applied == ["state-root", "manager-unit"]
    assert [row["step_id"] for row in completed.to_dict()["journal"]] == [
        "state-root",
        "manager-unit",
    ]
    assert completed.to_dict()["state"] == "applied"


def test_second_complete_apply_is_idempotent(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    first_calls = list(backend.applied)

    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )

    assert backend.applied == first_calls
    assert len(receipt.to_dict()["journal"]) == len(plan["apply_order"])


def test_post_mutation_crash_replays_from_prejournal_and_rollback_removes_created_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    backend.fail_after_mutation = "state-root"
    receipt_path = (tmp_path / "receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )
    receipt = new_install_receipt(plan, path=receipt_path)

    with pytest.raises(RuntimeError, match="post-mutation"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    journal = receipt.to_dict()["journal"]
    assert journal[0]["step_id"] == "state-root"
    assert journal[0]["status"] == "prepared"
    assert journal[0]["prior"] == {"exists": False}
    assert journal[0]["creation_authority"] == {
        "device": 1,
        "inode": 100,
        "file_type": "file",
    }
    assert InstallReceipt.load(receipt_path).to_dict()["journal"][0][
        "creation_authority"
    ] == journal[0]["creation_authority"]

    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    rollback_receipt(receipt, backend=backend)
    assert "state-root" not in backend.states


def test_partial_mid_step_mutation_is_rolled_back_then_replayed(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    backend.fail_with_partial_mutation = "state-root"
    receipt = new_install_receipt(plan)

    with pytest.raises(RuntimeError, match="partial-mutation"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )
    assert receipt.to_dict()["journal"][0]["status"] == "prepared"
    assert backend.states["state-root"]["installed_sha256"] == "partial"

    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )

    assert backend.rolled_back == ["state-root"]
    assert backend.states["state-root"]["installed_sha256"] == "1" * 64
    assert receipt.to_dict()["journal"][0]["status"] == "completed"


def test_partial_mid_step_mutation_can_be_explicitly_rolled_back(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    backend.fail_with_partial_mutation = "state-root"
    receipt = new_install_receipt(plan)
    with pytest.raises(RuntimeError, match="partial-mutation"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    report = rollback_receipt(receipt, backend=backend)

    assert backend.rolled_back == ["state-root"]
    assert "state-root" not in backend.states
    assert report.retained_drift == ()


@pytest.mark.parametrize("asset_type", ["file", "directory"])
@pytest.mark.parametrize("action", ["resume", "rollback"])
def test_prepared_prior_absent_does_not_delete_third_party_leaf_without_authority(
    tmp_path: Path, asset_type: str, action: str
) -> None:
    target = tmp_path / f"third-party-{asset_type}"
    step = {
        "step_id": f"asset:{asset_type}",
        "kind": "asset",
        "asset_type": asset_type,
        "path": str(target),
        "owner": "root",
        "group": "root",
        "mode": "0700",
        "acls": [],
        "operations": ["snapshot", "chown", "chmod"],
        "desired_sha256": "d" * 64,
    }
    if asset_type == "file":
        step["content"] = "planned\n"
    plan = _plan(tmp_path)
    plan["accounts"] = []
    plan["roots"]["deploy"] = str(tmp_path / "deploy")
    plan["apply_order"] = [step]
    receipt = new_install_receipt(plan)
    receipt._document["state"] = "applying"
    receipt._document["journal"] = [
        {
            "step_id": step["step_id"],
            "step": deepcopy(step),
            "status": "prepared",
            "prior": {"exists": False},
        }
    ]
    if asset_type == "file":
        target.write_text("operator-owned\n", encoding="utf-8")
    else:
        target.mkdir()

    class CrashBeforeMutationBackend:
        def __init__(self) -> None:
            self.rollback_calls = 0
            self.apply_calls = 0

        def preflight_facts(self, _plan):
            return _safe_facts(plan)

        def inspect_step(self, _step):
            if not target.exists():
                return {"exists": False}
            return {
                "exists": True,
                "installed_sha256": "third-party",
            }

        def apply_step(self, _step):
            self.apply_calls += 1
            raise AssertionError("unowned third-party state must fail before apply")

        def rollback_step(self, _entry):
            self.rollback_calls += 1
            target.unlink() if target.is_file() else target.rmdir()

        def list_unknown_state(self, _receipt):
            return ()

    backend = CrashBeforeMutationBackend()

    if action == "resume":
        with pytest.raises(InstallDriftError, match="provenance|creation authority"):
            apply_plan(
                plan,
                confirm_sha256=plan_sha256(plan),
                receipt=receipt,
                backend=backend,
            )
    else:
        report = rollback_receipt(receipt, backend=backend)
        assert report.retained_drift[0]["step_id"] == step["step_id"]

    assert target.exists()
    assert backend.rollback_calls == 0
    assert backend.apply_calls == 0


def test_rollback_persists_each_reversed_entry_and_recovers_after_persist_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    receipt_path = (tmp_path / "receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )
    receipt = new_install_receipt(plan, path=receipt_path)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    original_persist = receipt._persist
    persist_calls = 0

    def crash_on_first_entry_checkpoint() -> None:
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls == 2:
            raise SystemExit("simulated rollback checkpoint crash")
        original_persist()

    receipt._persist = crash_on_first_entry_checkpoint  # type: ignore[method-assign]

    with pytest.raises(SystemExit, match="checkpoint"):
        rollback_receipt(receipt, backend=backend)

    assert backend.rolled_back == ["manager-unit"]
    recovered = InstallReceipt.load(receipt_path)
    assert [row["step_id"] for row in recovered.to_dict()["journal"]] == [
        "state-root",
        "manager-unit",
    ]

    report = rollback_receipt(recovered, backend=backend)

    assert report.retained_drift == ()
    assert backend.rolled_back == ["manager-unit", "state-root"]
    assert backend.states == {}
    assert recovered.to_dict()["journal"] == []


def test_service_stop_failure_blocks_credential_and_runtime_rollback(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    receipt._document["activation_journal"] = [
        {"service": service, "status": "completed"}
        for service in (
            "cortex-egress-proxy.service",
            "cortex-manager.service",
            "cortex-monitor.service",
        )
    ]
    receipt._document["services_started"] = True
    backend.fail_stop = "cortex-manager.service"

    report = rollback_receipt(receipt, backend=backend)

    assert any(
        row["step_id"] == "service:cortex-manager.service"
        for row in report.to_dict()["retained_drift"]
    )
    assert backend.credential_rollbacks == 0
    assert backend.rolled_back == []
    assert set(backend.states) == {"state-root", "manager-unit"}
    assert receipt.to_dict()["state"] == "rollback-blocked"


def test_prepared_daemon_reload_is_replayed_not_promoted_from_synthetic_state(
    tmp_path: Path,
) -> None:
    step = {
        "step_id": "systemd:daemon-reload",
        "kind": "systemctl",
        "action": "daemon-reload",
        "operations": ["daemon-reload"],
        "desired_sha256": "d" * 64,
    }
    plan = _plan(tmp_path)
    plan["accounts"] = []
    plan["apply_order"] = [step]

    class DaemonReloadBackend:
        def __init__(self) -> None:
            self.applied = 0

        def preflight_facts(self, _plan):
            return _safe_facts(plan)

        def inspect_step(self, _step):
            return {"exists": True, "installed_sha256": "d" * 64}

        def apply_step(self, _step):
            self.applied += 1
            return {"exists": True, "installed_sha256": "d" * 64}

    receipt = new_install_receipt(plan)
    receipt._document["state"] = "applying"
    receipt._document["journal"] = [
        {
            "step_id": step["step_id"],
            "step": deepcopy(step),
            "status": "prepared",
            "prior": {"exists": True, "installed_sha256": "d" * 64},
        }
    ]
    backend = DaemonReloadBackend()

    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )

    assert backend.applied == 1
    assert receipt.to_dict()["journal"][0]["status"] == "completed"


def test_replay_stops_when_a_completed_asset_no_longer_matches(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    backend.fail_on = "manager-unit"
    receipt = new_install_receipt(plan)
    with pytest.raises(RuntimeError):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )
    backend.states["state-root"]["installed_sha256"] = "f" * 64

    with pytest.raises(InstallDriftError, match="state-root"):
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )
    assert backend.applied == ["state-root"]


def test_rollback_only_reverses_receipt_entries_and_retains_unknown_state(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    unknown = str(tmp_path / "target/state-root/operator-created.db")
    backend.unknown.add(unknown)

    report = rollback_receipt(receipt, backend=backend)

    assert backend.rolled_back == ["manager-unit", "state-root"]
    assert backend.states == {}
    assert unknown in backend.unknown
    assert report.to_dict()["retained_unknown"] == [unknown]


def test_rollback_does_not_overwrite_post_install_drift(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    backend.states["manager-unit"]["installed_sha256"] = "e" * 64

    report = rollback_receipt(receipt, backend=backend)

    assert "manager-unit" not in backend.rolled_back
    assert backend.states["manager-unit"]["installed_sha256"] == "e" * 64
    assert report.to_dict()["retained_drift"][0]["step_id"] == "manager-unit"


def test_receipt_load_rejects_non_root_or_non_private_authority_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (tmp_path / "receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    with monkeypatch.context() as create_authority:
        create_authority.setattr(
            install_core, "_validate_receipt_file", lambda _observed, _path: None
        )
        new_install_receipt(_plan(tmp_path), path=path)
    path.chmod(0o666)

    with pytest.raises(Exception, match="root-owned|0600"):
        InstallReceipt.load(path)


def test_new_receipt_exclusively_refuses_an_existing_private_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (tmp_path / "receipt.json").absolute()
    original = b"ROOT-OWNED-PRIVATE-BUT-NOT-A-RECEIPT\n"
    path.write_bytes(original)
    path.chmod(0o600)
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )

    with pytest.raises(InstallError, match="exist|exclusive|collision"):
        new_install_receipt(_plan(tmp_path), path=path)

    assert path.read_bytes() == original


def test_new_receipt_is_complete_before_atomic_noreplace_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (tmp_path / "receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )
    real_publish = install_core._rename_noreplace_at
    observed: dict[str, object] = {}

    def inspect_publish(parent_fd: int, source: str, destination: str) -> None:
        assert destination == path.name
        assert not path.exists()
        staged_fd = os.open(source, os.O_RDONLY, dir_fd=parent_fd)
        try:
            staged = json.loads(install_core._read_fd_bytes(staged_fd))
        finally:
            os.close(staged_fd)
        observed.update(staged)
        real_publish(parent_fd, source, destination)

    monkeypatch.setattr(install_core, "_rename_noreplace_at", inspect_publish)

    receipt = new_install_receipt(_plan(tmp_path), path=path)

    assert observed["state"] == "planned"
    assert observed["effective_receipt_path"] == str(path)
    assert InstallReceipt.load(path).to_dict() == receipt.to_dict()
    assert not list(tmp_path.glob(".receipt.json.*.tmp"))


def test_failed_initial_receipt_publish_leaves_no_partial_final_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (tmp_path / "receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core,
        "_rename_noreplace_at",
        lambda *_args: (_ for _ in ()).throw(OSError("injected publish failure")),
    )

    with pytest.raises(UnsafeInstallPathError, match="atomically create"):
        new_install_receipt(_plan(tmp_path), path=path)

    assert not path.exists()
    assert not list(tmp_path.glob(".receipt.json.*.tmp"))


def test_loaded_receipt_binds_actual_path_and_expected_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = (tmp_path / "first.json").absolute()
    second = (tmp_path / "second.json").absolute()
    plan = _plan(tmp_path)
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )
    receipt = new_install_receipt(plan, path=first)

    loaded = InstallReceipt.load(first, expected_plan=plan)

    assert loaded.to_dict()["effective_receipt_path"] == str(first)
    second.write_bytes(first.read_bytes())
    second.chmod(0o600)
    with pytest.raises(InstallError, match="effective.*path|path.*binding"):
        InstallReceipt.load(second, expected_plan=plan)

    changed = deepcopy(plan)
    changed["candidate"]["wheel_sha256"] = "d" * 64
    with pytest.raises(InstallError, match="same plan|plan.*match"):
        InstallReceipt.load(first, expected_plan=changed)
    assert receipt.to_dict()["effective_receipt_path"] == str(first)


def test_checkpoint_refuses_replaced_private_leaf_without_changing_its_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (tmp_path / "receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )
    receipt = new_install_receipt(_plan(tmp_path), path=path)
    replacement = b"UNRELATED-ROOT-OWNED-PRIVATE-FILE\n"
    path.write_bytes(replacement)
    path.chmod(0o600)
    receipt._document["state"] = "applying"

    with pytest.raises(InstallError, match="checkpoint|changed|authority"):
        receipt._persist()

    assert path.read_bytes() == replacement


def test_concurrent_receipt_checkpoints_serialize_and_reject_the_stale_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (tmp_path / "receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )
    plan = _plan(tmp_path)
    new_install_receipt(plan, path=path)
    first = InstallReceipt.load(path, expected_plan=plan)
    second = InstallReceipt.load(path, expected_plan=plan)
    first._document["state"] = "applying"
    second._document["state"] = "rolled-back"
    real_replace = install_core.os.replace
    first_replace_entered = threading.Event()
    second_replace_entered = threading.Event()
    release_first = threading.Event()
    replace_lock = threading.Lock()
    replace_calls = 0

    def delay_first_replace(source, destination, *args, **kwargs):
        nonlocal replace_calls
        if destination == path.name:
            with replace_lock:
                replace_calls += 1
                call = replace_calls
            if call == 1:
                first_replace_entered.set()
                assert release_first.wait(timeout=2)
            elif call == 2:
                second_replace_entered.set()
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(install_core.os, "replace", delay_first_replace)
    outcomes: list[str] = []

    def checkpoint(receipt: InstallReceipt) -> None:
        try:
            receipt._persist()
            outcomes.append("success")
        except InstallError:
            outcomes.append("stale")

    first_thread = threading.Thread(target=checkpoint, args=(first,))
    second_thread = threading.Thread(target=checkpoint, args=(second,))
    first_thread.start()
    assert first_replace_entered.wait(timeout=2)
    second_thread.start()
    raced = second_replace_entered.wait(timeout=0.2)
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not raced
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert sorted(outcomes) == ["stale", "success"]
    assert replace_calls == 1
    assert InstallReceipt.load(path, expected_plan=plan).to_dict()["state"] == "applying"


def test_receipt_lock_is_persistent_private_and_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (tmp_path / "authority" / "receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )

    new_install_receipt(_plan(tmp_path), path=path)

    lock_path = path.parent / f".{path.name}.lock"
    assert lock_path.is_file()
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    path.unlink()
    lock_path.unlink()
    external = tmp_path / "external"
    external.write_bytes(b"DO-NOT-LOCK\n")
    path.parent.mkdir(exist_ok=True)
    lock_path.symlink_to(external)

    with pytest.raises(UnsafeInstallPathError, match="lock|authority|safe"):
        new_install_receipt(_plan(tmp_path), path=path)

    assert external.read_bytes() == b"DO-NOT-LOCK\n"
    assert not path.exists()


def _root_owned_stat(observed: os.stat_result) -> os.stat_result:
    values = list(observed)
    values[stat.ST_UID] = 0
    return os.stat_result(values)


def test_new_receipt_rejects_an_attacker_writable_parent_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = tmp_path / "attacker-writable"
    authority.mkdir(mode=0o777)
    authority.chmod(0o777)
    path = (authority / "receipt.json").absolute()
    real_validate = install_core._validate_receipt_parent

    def validate_target_parent(observed: os.stat_result, candidate: Path) -> None:
        if candidate == authority:
            real_validate(observed, candidate)

    # The pytest root is intentionally user-writable.  Bypass only those test
    # harness ancestors so this case specifically exercises the target parent.
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", validate_target_parent
    )

    with pytest.raises(InstallError, match="root-owned.*non-writable"):
        new_install_receipt(_plan(tmp_path), path=path)

    assert not path.exists()


def test_new_receipt_validates_the_complete_ancestor_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writable_ancestor = tmp_path / "attacker-writable"
    authority = writable_ancestor / "root-owned-child"
    authority.mkdir(parents=True, mode=0o700)
    writable_ancestor.chmod(0o777)
    checked: list[Path] = []

    def reject_writable_ancestor(_observed: os.stat_result, path: Path) -> None:
        checked.append(path)
        if path == writable_ancestor:
            raise InstallError(
                f"receipt authority must be root-owned and non-writable: {path}"
            )

    monkeypatch.setattr(
        install_core,
        "_validate_receipt_parent",
        reject_writable_ancestor,
        raising=False,
    )

    with pytest.raises(InstallError, match="root-owned.*non-writable"):
        new_install_receipt(_plan(tmp_path), path=(authority / "receipt.json").absolute())

    assert writable_ancestor in checked


def test_every_receipt_checkpoint_fails_closed_on_parent_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    receipt_path = (authority / "receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None,
        raising=False,
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None,
        raising=False,
    )
    receipt = new_install_receipt(_plan(tmp_path), path=receipt_path)
    displaced = tmp_path / "displaced-authority"
    external = tmp_path / "external"
    external.mkdir()
    real_replace = os.replace
    swapped = False

    def replace_after_parent_swap(source, destination, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            authority.rename(displaced)
            authority.symlink_to(external, target_is_directory=True)
            if kwargs.get("src_dir_fd") is None:
                # Reproduce the pathname writer vulnerability: an attacker can
                # supply the same temporary leaf after replacing an ancestor.
                old_source = displaced / Path(source).name
                (external / Path(source).name).write_bytes(old_source.read_bytes())
            swapped = True
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(install_core.os, "replace", replace_after_parent_swap)
    receipt._document["state"] = "applying"

    with pytest.raises(UnsafeInstallPathError, match="changed|replaced|unsafe"):
        receipt._persist()

    assert swapped
    assert not (external / receipt_path.name).exists()


def test_receipt_load_rejects_non_root_owned_parent_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (tmp_path / "receipt.json").absolute()
    with monkeypatch.context() as create_authority:
        create_authority.setattr(
            install_core, "_validate_receipt_parent", lambda _observed, _path: None
        )
        create_authority.setattr(
            install_core, "_validate_receipt_file", lambda _observed, _path: None
        )
        new_install_receipt(_plan(tmp_path), path=path)
    real_lstat = Path.lstat

    def root_owned_leaf(candidate: Path):
        observed = real_lstat(candidate)
        return _root_owned_stat(observed) if candidate == path else observed

    monkeypatch.setattr(Path, "lstat", root_owned_leaf)
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None,
        raising=False,
    )

    with pytest.raises(InstallError, match="parent.*root-owned|root-owned.*parent"):
        InstallReceipt.load(path)


def test_receipt_load_fails_closed_when_leaf_is_replaced_after_fd_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (tmp_path / "receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None,
        raising=False,
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None,
        raising=False,
    )
    new_install_receipt(_plan(tmp_path), path=path)
    displaced = tmp_path / "displaced-receipt.json"
    external = tmp_path / "external-receipt.json"
    external.write_bytes(path.read_bytes())
    real_lstat = Path.lstat
    real_loads = json.loads
    swapped = False

    def root_owned_leaf(candidate: Path):
        observed = real_lstat(candidate)
        return _root_owned_stat(observed) if candidate == path else observed

    def loads_and_swap(payload, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            path.rename(displaced)
            path.symlink_to(external)
            swapped = True
        return real_loads(payload, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", root_owned_leaf)
    monkeypatch.setattr(install_core.json, "loads", loads_and_swap)
    with pytest.raises(UnsafeInstallPathError, match="changed|replaced|symlink"):
        InstallReceipt.load(path)

    assert swapped
