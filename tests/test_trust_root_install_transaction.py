"""Phase 2 trust-root installer RED contract: preflight and transactions.

The backend below is an in-memory OS seam.  A focused unit run must not inspect the
host, invoke sudo, or contact systemd; production apply consumes explicit facts and
typed steps through the same seam.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import (
    AccountCollisionError,
    InstallDriftError,
    InstallPlanError,
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
        "durable": False,
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
        "accounts": {row["name"]: dict(row) for row in plan["accounts"]},
        "paths": {
            step["path"]: {"exists": False, "is_symlink": False}
            for step in plan["apply_order"]
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
    manager = dict(facts["accounts"]["cortex-manager"])
    manager["uid"] = 65530
    facts["accounts"]["cortex-manager"] = manager

    with pytest.raises(AccountCollisionError, match="cortex-manager"):
        validate_preflight(plan, facts)


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
