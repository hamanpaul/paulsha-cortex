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
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import core as install_core
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

    def preflight_facts(self, _plan) -> dict[str, object]:
        return deepcopy(self.facts)

    def inspect_step(self, step) -> dict[str, object]:
        return deepcopy(self.states.get(step["step_id"], {"exists": False}))

    def apply_step(self, step) -> dict[str, object]:
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
        self.applied.append(step_id)
        if self.fail_with_partial_mutation == step_id:
            self.fail_with_partial_mutation = None
            self.states[step_id]["installed_sha256"] = "partial"
            raise RuntimeError(f"injected partial-mutation interruption at {step_id}")
        if self.fail_after_mutation == step_id:
            self.fail_after_mutation = None
            raise RuntimeError(f"injected post-mutation interruption at {step_id}")
        return {"prior": prior, **state}

    def rollback_step(self, entry) -> None:
        self.rolled_back.append(entry["step_id"])
        prior = deepcopy(entry["prior"])
        if prior.get("exists"):
            self.states[entry["step_id"]] = prior
        else:
            self.states.pop(entry["step_id"], None)

    def list_unknown_state(self, _receipt) -> tuple[str, ...]:
        return tuple(sorted(self.unknown))


def test_preflight_uses_explicit_backend_facts_not_the_host(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    facts = _safe_facts(plan)

    report = validate_preflight(plan, facts)

    assert report.ok
    assert report.failures == ()


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
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    backend = RecordingBackend(plan)
    backend.fail_after_mutation = "state-root"
    receipt = new_install_receipt(plan)

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
    tmp_path: Path,
) -> None:
    path = (tmp_path / "receipt.json").absolute()
    new_install_receipt(_plan(tmp_path), path=path)
    path.chmod(0o666)

    with pytest.raises(Exception, match="root-owned|0600"):
        InstallReceipt.load(path)


def _root_owned_stat(observed: os.stat_result) -> os.stat_result:
    values = list(observed)
    values[stat.ST_UID] = 0
    return os.stat_result(values)


def test_receipt_load_rejects_non_root_owned_parent_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (tmp_path / "receipt.json").absolute()
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
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None,
        raising=False,
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None,
        raising=False,
    )

    with pytest.raises(UnsafeInstallPathError, match="changed|replaced|symlink"):
        InstallReceipt.load(path)

    assert swapped
