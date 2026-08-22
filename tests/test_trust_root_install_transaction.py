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
    return {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {"commit": "a" * 40},
        "candidate": {
            "wheel_sha256": "b" * 64,
            "bundle_sha256": "c" * 64,
        },
        "accounts": _accounts(tmp_path),
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

    def preflight_facts(self, _plan) -> dict[str, object]:
        return deepcopy(self.facts)

    def inspect_step(self, step) -> dict[str, object]:
        return deepcopy(self.states.get(step["step_id"], {"exists": False}))

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

    def apply_step_checkpointed(self, step, creation_checkpoint):
        return self._apply_step(step, creation_checkpoint)

    def creation_authority_matches(self, step, authority) -> bool:
        return authority == self.creation_identities.get(step["step_id"])

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
    plan["roots"] = {"state": step["path"]}
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
    plan["roots"] = {"state": step["path"]}
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
    plan["roots"] = {"state": step["path"]}
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
    plan["roots"] = {"state": step["path"]}
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
        with pytest.raises(InstallDriftError, match="creation authority"):
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
