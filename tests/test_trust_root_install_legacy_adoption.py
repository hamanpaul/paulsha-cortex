"""Legacy adoption PR-4: apply, quarantine, receipt provenance and rollback proof (#1122).

Planning (PR-3) binds a reviewed legacy inventory to a plan.  Apply re-captures
that inventory under the transaction lock with the services stopped and fails
before the first mutation on any drift; ``legacy-quarantine`` steps move legacy
objects aside with ``renameat2(RENAME_NOREPLACE)``; receipts record where each
adopted object's provenance came from; rollback moves quarantined objects back
and proves the host equals the reviewed inventory again.

The transaction runs against an in-memory install backend (the same seam the
transaction tests use).  Quarantine moves, however, run the real backend code
on a temporary tree, so the rollback proof re-captures real inodes.  Nothing
here needs root, sudo, systemd or the host's accounts.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import backend as install_backend
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import legacy
from paulsha_cortex.trust_root.install.core import (
    AccountCollisionError,
    InstallDriftError,
    InstallError,
    InstallPlanError,
    InstallReceipt,
    apply_plan,
    new_install_receipt,
    plan_sha256,
    rollback_receipt,
    validate_preflight,
)

from test_trust_root_install_legacy_inventory import (  # noqa: E402
    LEGACY_IDS,
    MACHINE_ID,
    FakeLegacyHost,
    _collect,
    _plan_for,
)
from test_trust_root_install_legacy_plan import (  # noqa: E402
    QUARANTINE_ROOT,
    _capture,
    _request,
    _setup,
)


# ---------------------------------------------------------------------------
# an in-memory install backend whose legacy-quarantine steps are real
# ---------------------------------------------------------------------------

TREE_SHA256 = "e" * 64
VENV_LINK = "venvs/candidate"


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _desired_state(step: dict) -> dict[str, object]:
    state: dict[str, object] = {"exists": True, "installed_sha256": step["desired_sha256"]}
    for key in ("owner", "group", "mode"):
        if key in step:
            state[key] = step[key]
    if "acls" in step:
        state["acl"] = deepcopy(step["acls"])
    return state


def _facts(plan: dict, accounts: dict | None = None, **extra) -> dict[str, object]:
    facts: dict[str, object] = {
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
        "account_uids": {},
        "group_gids": {},
        "groups": {},
        "primary_gid_users": {},
        "group_names_by_gid": {},
        "paths": {
            step["path"]: {"exists": False, "is_symlink": False}
            for step in plan["apply_order"]
            if isinstance(step.get("path"), str)
        },
    }
    facts.update(accounts or {})
    facts.update(extra)
    return facts


class MemoryInstallBackend:
    """Model every managed step in memory; quarantine steps move real objects."""

    def __init__(self, plan: dict, *, facts: dict | None = None) -> None:
        self.plan = plan
        self.facts = facts if facts is not None else _facts(plan)
        self.states: dict[str, dict[str, object]] = {}
        self.enabled: set[str] = set()
        self.venv_slot = False
        self.venv_link: dict[str, object] | None = None
        self.identities: dict[str, dict[str, object]] = {}
        self.log: list[list[str]] = []
        self.fail_after_mutation: str | None = None
        self.fail_before_mutation: str | None = None
        self.fail_with_partial: str | None = None
        self.reloads = 0
        self.stopped: list[str] = []

    # -- legacy guard ---------------------------------------------------
    def _quarantine_steps(self) -> list[dict]:
        return [step for step in self.plan["apply_order"] if step["kind"] == "legacy-quarantine"]

    def _guard(self, path: object, step_id: str) -> None:
        """No managed step may be inspected while a legacy object is in its place."""

        if not isinstance(path, str):
            return
        for quarantine in self._quarantine_steps():
            if not _within(path, quarantine["path"]):
                continue
            try:
                observed = os.lstat(quarantine["path"])
            except FileNotFoundError:
                continue
            expected = quarantine["expected"]
            if (observed.st_dev, observed.st_ino) == (expected["dev"], expected["ino"]):
                raise AssertionError(
                    f"{step_id} was inspected while legacy {quarantine['path']} is in place"
                )

    # -- inspection -----------------------------------------------------
    live_paths = False
    stable_replacement_identity = False

    def preflight_facts(self, _plan) -> dict[str, object]:
        facts = deepcopy(self.facts)
        if self.live_paths:
            # Like the real backend: path facts are read at every apply.
            facts["paths"] = _path_facts(self.plan)
        return facts

    def _venv_state(self, step: dict) -> dict[str, object]:
        if not self.venv_slot:
            return {"exists": False}
        state: dict[str, object] = {
            "exists": True,
            "installed_sha256": None,
            "slot_sha256": step["desired_sha256"],
            "path": step["path"],
            "tree_sha256": TREE_SHA256,
        }
        if self.venv_link == {"exists": True, "is_symlink": True, "link_target": VENV_LINK}:
            state.update({"installed_sha256": step["desired_sha256"], "link_target": VENV_LINK})
        return state

    def inspect_step(self, step) -> dict[str, object]:
        step_id = step["step_id"]
        kind = step["kind"]
        self.log.append(["inspect", step_id])
        if kind == "legacy-quarantine":
            return dict(install_backend._legacy_quarantine_state(step))
        self._guard(step.get("path"), step_id)
        if kind == "venv":
            return self._venv_state(step)
        if kind == "systemctl":
            if step["action"] == "daemon-reload":
                return {"exists": True, "installed_sha256": step["desired_sha256"]}
            enabled = step["unit"] in self.enabled
            return {
                "exists": enabled,
                "installed_sha256": step["desired_sha256"] if enabled else None,
            }
        return deepcopy(self.states.get(step_id, {"exists": False}))

    def inspect_venv_activation(self, step) -> dict[str, object]:
        self.log.append(["activation", step["step_id"]])
        if self.venv_link is not None:
            return deepcopy(self.venv_link)
        self._guard(step["active_link"], step["step_id"])
        path = step["active_link"]
        try:
            observed = os.lstat(path)
        except FileNotFoundError:
            return {"exists": False}
        if stat.S_ISLNK(observed.st_mode):
            return {"exists": True, "is_symlink": True, "link_target": os.readlink(path)}
        return {"exists": True, "is_symlink": False}

    # -- mutation -------------------------------------------------------
    def _apply(self, step, creation_checkpoint=None) -> dict[str, object]:
        step_id = step["step_id"]
        kind = step["kind"]
        if self.fail_before_mutation == step_id:
            self.fail_before_mutation = None
            raise RuntimeError(f"injected failure before mutating {step_id}")
        prior = self.inspect_step(step)
        self.log.append(["apply", step_id])
        if kind == "legacy-quarantine":
            outcome = dict(install_backend._legacy_quarantine_move(step))
            if self.fail_after_mutation == step_id:
                self.fail_after_mutation = None
                raise RuntimeError(f"injected post-mutation interruption at {step_id}")
            return outcome
        if kind == "venv":
            if creation_checkpoint is not None:
                creation_checkpoint({"path": step["path"], "tree_sha256": TREE_SHA256})
            self.venv_slot = True
            self.venv_link = {"exists": True, "is_symlink": True, "link_target": VENV_LINK}
            state = self._venv_state(step)
        elif kind == "systemctl":
            if step["action"] == "enable":
                self.enabled.add(step["unit"])
            state = self.inspect_step(step)
        else:
            state = _desired_state(step)
            if prior.get("exists") and kind == "account":
                state = deepcopy(prior)
            self.states[step_id] = state
            if (
                not prior.get("exists")
                and kind == "asset"
                and step.get("asset_type", "file") in {"file", "directory"}
            ):
                authority = {
                    "device": 1,
                    "inode": len(self.identities) + 100,
                    "file_type": step.get("asset_type", "file"),
                }
                self.identities[step_id] = authority
                if creation_checkpoint is not None:
                    creation_checkpoint(authority)
        if self.fail_with_partial == step_id:
            self.fail_with_partial = None
            self.states[step_id] = {**deepcopy(prior), "installed_sha256": "partial"}
            raise RuntimeError(f"injected partial mutation at {step_id}")
        if self.fail_after_mutation == step_id:
            self.fail_after_mutation = None
            raise RuntimeError(f"injected post-mutation interruption at {step_id}")
        return {"prior": prior, **deepcopy(state)}

    def apply_step(self, step) -> dict[str, object]:
        return self._apply(step)

    def apply_step_checkpointed(self, step, expected_prior, creation_checkpoint):
        assert self.inspect_step(step) == expected_prior
        return self._apply(step, creation_checkpoint)

    def replace_step_checkpointed(self, step, expected_prior, replacement_checkpoint):
        assert self.inspect_step(step) == expected_prior
        self.log.append(["replace", step["step_id"]])
        file_type = "directory" if step["kind"] == "repository" else step.get("asset_type", "file")
        authority = {"device": 1, "inode": len(self.identities) + 1000, "file_type": file_type}
        if self.stable_replacement_identity:
            # A metadata replacement keeps the object's inode, so a replayed
            # replacement reports the identity its first attempt recorded.
            authority = self.identities.setdefault(step["step_id"], authority)
        self.identities[step["step_id"]] = authority
        replacement_checkpoint(authority)
        if self.fail_with_partial == step["step_id"]:
            self.fail_with_partial = None
            self.states[step["step_id"]] = {**deepcopy(expected_prior), "mode": "0000"}
            raise RuntimeError(f"injected partial replacement at {step['step_id']}")
        self.states[step["step_id"]] = _desired_state(step)
        return {
            "prior": deepcopy(expected_prior),
            "replacement_authority": authority,
            **_desired_state(step),
        }

    def creation_authority_matches(self, step, authority) -> bool:
        return authority == self.identities.get(step["step_id"])

    def cleanup_prepared_replacement(self, _entry) -> None:
        return None

    def rollback_step(self, entry) -> None:
        step = entry["step"]
        step_id = entry["step_id"]
        self.log.append(["rollback", step_id])
        prior = deepcopy(entry["prior"])
        if step["kind"] == "legacy-quarantine":
            install_backend._legacy_quarantine_restore(entry)
            return
        if step["kind"] == "account":
            return  # accounts are retained, as on a real host
        if step["kind"] == "venv":
            self.venv_link = prior if prior.get("exists") else None
            return
        if step["kind"] == "systemctl":
            if step["action"] == "enable" and not prior.get("exists"):
                self.enabled.discard(step["unit"])
            return
        if prior.get("exists"):
            self.states[step_id] = prior
        else:
            self.states.pop(step_id, None)
            self.identities.pop(step_id, None)

    def list_unknown_state(self, _receipt) -> tuple[str, ...]:
        return ()

    def stop_service(self, name: str) -> None:
        self.stopped.append(name)

    def rollback_credentials(self, _receipt):
        return ()

    def reload_systemd_units(self) -> None:
        self.reloads += 1
        self.log.append(["daemon-reload", ""])


# ---------------------------------------------------------------------------
# regression: a plan without a legacy block behaves exactly as before
# ---------------------------------------------------------------------------

_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def _normalize(value: object, prefix: str) -> str:
    """Canonical JSON with the tmp prefix and digest identities factored out.

    Every 64-hex digest becomes ``<Hn>`` by first occurrence, so equal and
    unequal digests stay equal and unequal; only the per-run temporary path
    (which every path-bearing digest covers) is removed.
    """

    text = json.dumps(value, sort_keys=True, separators=(",", ":")).replace(prefix, "<TMP>")
    names: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        return names.setdefault(match.group(0), f"<H{len(names)}>")

    return _HEX64.sub(replace, text)


def _receipt_view(receipt: InstallReceipt) -> dict[str, object]:
    document = receipt.to_dict()
    for key in ("receipt_id", "plan", "plan_sha256", "effective_receipt_path"):
        document.pop(key, None)
    return document


# Recorded from the pre-PR-4 installer (base: legacy plan, 7ddf1957) running
# this exact scenario.  A plan without a legacy block must keep producing the
# same receipts, backend calls and rollback report; never regenerate this to
# make a legacy change pass.
REGRESSION_GOLDEN_SHA256 = (
    "c755a6a451755088616a56c4c40fb19319d50a7ac22ca4c31b561dcddc0e46d9"
)


def test_plan_without_legacy_block_keeps_base_behaviour(tmp_path: Path) -> None:
    plan = _plan_for(tmp_path, None)
    assert "legacy_adoption" not in plan
    backend = MemoryInstallBackend(plan)
    receipt = new_install_receipt(plan)
    digest = plan_sha256(plan)
    trace: list[object] = []

    backend.fail_after_mutation = "generated:units/cortex-manager.service"
    with pytest.raises(RuntimeError, match="post-mutation"):
        apply_plan(plan, confirm_sha256=digest, receipt=receipt, backend=backend)
    trace.append(_receipt_view(receipt))
    apply_plan(plan, confirm_sha256=digest, receipt=receipt, backend=backend)
    trace.append(_receipt_view(receipt))
    apply_plan(plan, confirm_sha256=digest, receipt=receipt, backend=backend)
    trace.append(_receipt_view(receipt))

    # An upgrade with a changed operator ACL hands off through the prior receipt.
    receipt._document["qualified"] = True
    upgrade = _plan_for(tmp_path, {"operator_account": "legacy-operator"})
    upgraded = new_install_receipt(upgrade)
    apply_plan(
        upgrade,
        confirm_sha256=plan_sha256(upgrade),
        receipt=upgraded,
        prior_receipt=receipt,
        backend=backend,
    )
    trace.append(_receipt_view(upgraded))

    report = rollback_receipt(upgraded, backend=backend)
    trace.append(report.to_dict())
    trace.append(_receipt_view(upgraded))
    trace.append(backend.log)

    normalized = _normalize(trace, str(tmp_path))
    assert hashlib.sha256(normalized.encode()).hexdigest() == REGRESSION_GOLDEN_SHA256


# ---------------------------------------------------------------------------
# a planned legacy host
# ---------------------------------------------------------------------------

_QUARANTINE_VALIDATORS = ("_validate_quarantine_ancestor", "_validate_quarantine_directory")
_ORIGINAL_VALIDATORS: dict[str, object] = {}


@pytest.fixture(autouse=True)
def _test_user_stands_in_for_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The quarantine chain must be root-only; here the test user plays root.

    Only the owner is relaxed: components the installer creates or reuses at
    or below the quarantine root must still be private 0700 directories, and
    no component may be a symlink.
    """

    for name in _QUARANTINE_VALIDATORS:
        _ORIGINAL_VALIDATORS.setdefault(name, getattr(install_backend, name, None))
    _relax_quarantine_owner(monkeypatch)


def _relax_quarantine_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    uid = os.geteuid()

    def ancestor(observed: os.stat_result, path: Path) -> None:
        if not stat.S_ISDIR(observed.st_mode):
            raise install_core.UnsafeInstallPathError(f"not a directory: {path}")

    def directory(observed: os.stat_result, path: Path) -> None:
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != uid
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise install_core.UnsafeInstallPathError(
                f"not a private quarantine directory: {path}"
            )

    monkeypatch.setattr(install_backend, "_validate_quarantine_ancestor", ancestor, raising=False)
    monkeypatch.setattr(install_backend, "_validate_quarantine_directory", directory, raising=False)


def _path_facts(plan: dict) -> dict[str, dict[str, object]]:
    paths: dict[str, dict[str, object]] = {}
    for step in plan["apply_order"]:
        path = step.get("path")
        if not isinstance(path, str):
            continue
        try:
            observed = os.lstat(path)
        except FileNotFoundError:
            paths[path] = {"exists": False, "is_symlink": False}
            continue
        paths[path] = {"exists": True, "is_symlink": stat.S_ISLNK(observed.st_mode)}
        if stat.S_ISREG(observed.st_mode):
            paths[path]["size"] = observed.st_size
    return paths


def _legacy_facts(plan: dict, host: FakeLegacyHost) -> dict[str, object]:
    """Preflight facts the real backend would read from this legacy host."""

    names = {row["name"] for row in (*plan["accounts"], *plan["service_accounts"])}
    accounts = {
        row.name: {
            "name": row.name,
            "uid": row.uid,
            "gid": row.gid,
            "home": row.home,
            "shell": row.shell,
            "supplementary_groups": sorted(
                group.name for group in host.groups if row.name in group.members
            ),
            "password_locked": True,
        }
        for row in host.users
        if row.name in names
    }
    primary: dict[int, list[str]] = {}
    for row in host.users:
        primary.setdefault(row.gid, []).append(row.name)
    by_gid: dict[int, list[str]] = {}
    for group in host.groups:
        by_gid.setdefault(group.gid, []).append(group.name)
    paths = _path_facts(plan)
    return _facts(
        plan,
        accounts={
            "accounts": accounts,
            "account_uids": {row.uid: row.name for row in host.users},
            "group_gids": {group.gid: group.name for group in host.groups},
            "groups": {
                group.name: {
                    "name": group.name,
                    "gid": group.gid,
                    "members": sorted(set(group.members)),
                }
                for group in host.groups
                if group.name in names
            },
            "primary_gid_users": {gid: sorted(set(rows)) for gid, rows in primary.items()},
            "group_names_by_gid": {gid: sorted(set(rows)) for gid, rows in by_gid.items()},
            "paths": paths,
        },
    )


DRIFTED_DIRECTORY = "asset:coordinator-root-tree"


class LegacyCase:
    """A Phase 2b-shaped host, its reviewed inventory and the bound plan."""

    def __init__(self, tmp_path: Path, *, host=None, request: dict | None = None) -> None:
        config, bundle, overlay, fake, seeded, base = _setup(tmp_path)
        if host is not None:
            host(seeded, fake, base)
        self.inventory_path = _capture(tmp_path, base, overlay, fake)
        block = _request(tmp_path, self.inventory_path, **(request or {}))
        self.plan = install_cli._plan_document(
            config,
            bundle,
            overlay={**overlay, "legacy_adoption": block},
            legacy_inventory=self.inventory_path,
            machine_id=MACHINE_ID,
        )
        self.tmp_path = tmp_path
        self.config, self.bundle, self.overlay = config, bundle, overlay
        self.seeded, self.base, self.host = seeded, base, fake
        # The lease stopped the legacy services before apply.
        for row in fake.services.values():
            row.update({"active_state": "inactive", "sub_state": "dead"})
        fake.running_units = []  # type: ignore[attr-defined]
        fake.running_cortex_units = lambda: list(fake.running_units)  # type: ignore[attr-defined]
        self.backend = self.memory_backend()
        self.digest = plan_sha256(self.plan)

    @property
    def block(self) -> dict:
        return self.plan["legacy_adoption"]

    @property
    def quarantine_root(self) -> Path:
        return self.tmp_path / QUARANTINE_ROOT

    def covered(self) -> list[str]:
        return [step["path"] for step in self.quarantine_steps()]

    def memory_backend(self) -> MemoryInstallBackend:
        backend = MemoryInstallBackend(self.plan, facts=_legacy_facts(self.plan, self.host))
        backend.live_paths = True
        backend.stable_replacement_identity = True
        for step in self.plan["apply_order"]:
            if step["step_id"] not in self.block["adopted"]:
                continue
            if step["kind"] == "systemctl":
                backend.enabled.add(step["unit"])
                continue
            backend.states[step["step_id"]] = _desired_state(step)
        backend.states[DRIFTED_DIRECTORY] = {
            **_desired_state(self.step(DRIFTED_DIRECTORY)),
            "mode": "0777",
            "installed_sha256": None,
        }
        return backend

    def step(self, step_id: str) -> dict:
        return next(step for step in self.plan["apply_order"] if step["step_id"] == step_id)

    def quarantine_steps(self) -> list[dict]:
        return [step for step in self.plan["apply_order"] if step["kind"] == "legacy-quarantine"]

    def context(self, *, inventory_path: Path | None = None):
        path = inventory_path or self.inventory_path
        return legacy.LegacyApplyContext(
            self.plan,
            legacy.LegacyInventory.load(path),
            self.host,
            inventory_path=str(path),
        )

    def apply(self, receipt: InstallReceipt, **options) -> InstallReceipt:
        if "legacy" not in options:
            options["legacy"] = self.context()
        options.setdefault("backend", self.backend)
        return apply_plan(self.plan, confirm_sha256=self.digest, receipt=receipt, **options)

    def recaptured_sha256(self) -> str:
        return legacy.inventory_stable_sha256(_collect(self.base, self.overlay, self.host))

    def snapshot(self) -> dict[str, tuple[int, int] | None]:
        """Where every quarantined object is now, by inode."""

        rows: dict[str, tuple[int, int] | None] = {}
        for step in self.quarantine_steps():
            try:
                observed = os.lstat(step["path"])
            except FileNotFoundError:
                rows[step["path"]] = None
            else:
                rows[step["path"]] = (observed.st_dev, observed.st_ino)
        return rows

    def legacy_identities(self) -> dict[str, tuple[int, int]]:
        return {
            step["path"]: (step["expected"]["dev"], step["expected"]["ino"])
            for step in self.quarantine_steps()
        }


def _plain_plan(root: Path) -> dict:
    """A release-shaped plan without a legacy block, in its own directory."""

    root.mkdir(parents=True, exist_ok=True)
    return _plan_for(root, None)


def _replace_with_same_bytes(path: Path) -> None:
    """Swap in a copy: same bytes and metadata, guaranteed another inode."""

    replacement = path.with_name(f".{path.name}.replacement")
    replacement.write_bytes(path.read_bytes())
    os.chmod(replacement, stat.S_IMODE(path.lstat().st_mode))
    before = path.lstat().st_ino
    os.replace(replacement, path)
    assert path.lstat().st_ino != before


def _mutations(backend: MemoryInstallBackend) -> list[list[str]]:
    return [row for row in backend.log if row[0] in {"apply", "replace", "rollback"}]


def _assert_untouched(case: LegacyCase, receipt: InstallReceipt) -> None:
    document = receipt.to_dict()
    assert document["state"] == "planned"
    assert document["journal"] == []
    assert "legacy_adoption" not in document
    assert _mutations(case.backend) == []
    assert case.snapshot() == case.legacy_identities()
    assert not case.quarantine_root.exists()


# ---------------------------------------------------------------------------
# apply re-captures the inventory before the first mutation
# ---------------------------------------------------------------------------


def _unit_changed(case: LegacyCase, _monkeypatch) -> None:
    case.seeded["unit"].write_text("[Service]\nUser=cortex-manager\nExecStart=/srv/x\n")


def _other_host(case: LegacyCase, _monkeypatch) -> None:
    case.host.machine = "f" * 32


def _other_scope(case: LegacyCase, monkeypatch) -> None:
    original_scope = legacy.legacy_scope
    original_collect = legacy.collect_legacy_inventory

    def widened(plan):
        return {**original_scope(plan), "instance": "elsewhere"}

    def collect(**kwargs):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(legacy, "legacy_scope", widened)
            return original_collect(**kwargs)

    monkeypatch.setattr(legacy, "collect_legacy_inventory", collect)


def _new_writable(case: LegacyCase, _monkeypatch) -> None:
    case.host.writable = {LEGACY_IDS["cortex-builder"][0]: {str(case.seeded["specs"])}}


def _unstable(case: LegacyCase, _monkeypatch) -> None:
    case.host.unstable = {LEGACY_IDS["cortex-gate"][0]: {str(case.seeded["stray"])}}


def _in_flight(case: LegacyCase, _monkeypatch) -> None:
    case.host.in_flight_value = {"job_processes": 1, "durable_jobs": 0}


def _in_flight_unproven(case: LegacyCase, _monkeypatch) -> None:
    case.host.in_flight_value = {"job_processes": 0, "durable_jobs": None}


def _service_active(case: LegacyCase, _monkeypatch) -> None:
    case.host.services["cortex-manager.service"]["active_state"] = "active"


def _template_instance(case: LegacyCase, _monkeypatch) -> None:
    case.host.running_units = [("cortex-reviewer-job@planning-7.service", "active")]


@pytest.mark.parametrize(
    ("drift", "named"),
    [
        (_unit_changed, "digest"),
        (_other_host, "host binding"),
        (_other_scope, "scope"),
        (_new_writable, "cortex-builder can write"),
        (_unstable, "unstable"),
        (_in_flight, "in-flight"),
        (_in_flight_unproven, "in-flight"),
        (_service_active, "cortex-manager.service"),
        (_template_instance, "cortex-reviewer-job@planning-7.service"),
    ],
    ids=[
        "digest",
        "host",
        "scope",
        "census-new-writable",
        "census-unstable",
        "in-flight",
        "in-flight-unproven",
        "active-service",
        "template-instance",
    ],
)
def test_apply_recapture_drift_fails_before_the_first_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift, named: str
) -> None:
    case = LegacyCase(tmp_path)
    drift(case, monkeypatch)
    receipt = new_install_receipt(case.plan)

    with pytest.raises(InstallDriftError, match=re.escape(named)) as caught:
        case.apply(receipt)

    assert "before any mutation" in str(caught.value)
    _assert_untouched(case, receipt)


def test_running_cortex_units_lists_every_cortex_unit_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = (
        "cortex-manager.service loaded inactive dead Cortex Manager\n"
        "● cortex-job@build-3.service loaded failed failed Cortex job\n"
        "cortex-reviewer-job@plan-1.service loaded active running Reviewer job\n"
    )
    calls: list[tuple[str, ...]] = []

    def listed(argv):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=listing, stderr="")

    monkeypatch.setattr(legacy, "_run", listed)
    host = legacy.LocalLegacyHostBackend(require_root=False)

    assert host.running_cortex_units() == [
        ("cortex-job@build-3.service", "failed"),
        ("cortex-manager.service", "inactive"),
        ("cortex-reviewer-job@plan-1.service", "active"),
    ]
    assert calls[0][:2] == ("systemctl", "list-units") and "--all" in calls[0]

    monkeypatch.setattr(
        legacy,
        "_run",
        lambda argv: subprocess.CompletedProcess(argv, 1, stdout="", stderr="no bus"),
    )
    with pytest.raises(InstallError, match="cortex units"):
        host.running_cortex_units()


def test_new_writable_path_is_named_with_its_principal(tmp_path: Path, monkeypatch) -> None:
    case = LegacyCase(tmp_path)
    _new_writable(case, monkeypatch)

    with pytest.raises(InstallDriftError) as caught:
        case.apply(new_install_receipt(case.plan))

    assert f"cortex-builder can write {case.seeded['specs']}" in str(caught.value)


def test_apply_accepts_only_the_plan_bound_inventory(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    case.seeded["stray"].write_text("changed after review\n")
    (tmp_path / "other").mkdir()
    other = _capture(tmp_path / "other", case.base, case.overlay, case.host)

    with pytest.raises(InstallPlanError, match="inventory_sha256"):
        case.context(inventory_path=other)


def test_legacy_inventory_and_prior_receipt_are_mutually_exclusive(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    prior = new_install_receipt(case.plan)
    receipt = new_install_receipt(case.plan)

    with pytest.raises(InstallPlanError, match="mutually exclusive"):
        case.apply(receipt, prior_receipt=prior)
    with pytest.raises(InstallPlanError, match="--legacy-inventory"):
        case.apply(receipt, legacy=None)
    _assert_untouched(case, receipt)

    plain = _plain_plan(tmp_path / "plain")
    with pytest.raises(InstallPlanError, match="legacy_adoption"):
        legacy.LegacyApplyContext(
            plain,
            legacy.LegacyInventory.load(case.inventory_path),
            case.host,
            inventory_path=str(case.inventory_path),
        )


def test_cli_apply_enforces_the_legacy_inventory_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    case = LegacyCase(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(case.plan), encoding="utf-8")
    receipt = tmp_path / "receipts" / "receipt.json"
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    base = ["apply", "--plan", str(plan_path), "--confirm-sha256", case.digest]
    base += ["--receipt", str(receipt)]

    assert install_cli.main(
        [*base, "--legacy-inventory", str(case.inventory_path), "--prior-receipt", str(receipt)]
    ) == 1
    assert "mutually exclusive" in capsys.readouterr().err
    assert install_cli.main(base) == 1
    assert "requires --legacy-inventory" in capsys.readouterr().err

    plain = _plain_plan(tmp_path / "plain")
    plain_path = tmp_path / "plain.json"
    plain_path.write_text(json.dumps(plain), encoding="utf-8")
    assert install_cli.main(
        [
            "apply",
            "--plan",
            str(plain_path),
            "--confirm-sha256",
            plan_sha256(plain),
            "--receipt",
            str(receipt),
            "--legacy-inventory",
            str(case.inventory_path),
        ]
    ) == 1
    assert "legacy_adoption" in capsys.readouterr().err
    assert not receipt.parent.exists()


# ---------------------------------------------------------------------------
# a complete adoption
# ---------------------------------------------------------------------------


def test_adoption_quarantines_adopts_and_records_legacy_provenance(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    receipt = new_install_receipt(case.plan)

    case.apply(receipt)

    document = receipt.to_dict()
    assert document["state"] == "applied"
    assert document["legacy_adoption"] == {
        "inventory_sha256": case.block["inventory_sha256"],
        "inventory_path": str(case.inventory_path),
        "host_binding_sha256": case.block["host_binding_sha256"],
        "quarantine_root": case.block["quarantine_root"],
        "apply_inventory_sha256": case.block["inventory_sha256"],
    }
    entries = {entry["step_id"]: entry for entry in document["journal"]}
    assert not any("adopted_from_receipt" in entry for entry in entries.values())
    for step_id, digest in case.block["adopted"].items():
        assert entries[step_id]["adoption"] == {
            "source": "legacy-inventory",
            "row_sha256": digest,
        }, step_id
    assert all(
        "adoption" not in entry
        for step_id, entry in entries.items()
        if step_id not in case.block["adopted"]
    )
    # Every quarantined object moved, by inode, into a private chain.
    for step in case.quarantine_steps():
        entry = entries[step["step_id"]]
        expected = step["expected"]
        assert entry["status"] == "completed"
        assert entry["quarantine_authority"] == {
            "source": {"dev": expected["dev"], "ino": expected["ino"]},
            "destination": step["destination"],
        }
        assert not os.path.lexists(step["path"])
        moved = os.lstat(step["destination"])
        assert (moved.st_dev, moved.st_ino) == (expected["dev"], expected["ino"])
        parent = Path(step["destination"]).parent
        while parent != case.quarantine_root.parent:
            assert stat.S_IMODE(parent.lstat().st_mode) == 0o700, parent
            parent = parent.parent
    # A drifted adopted directory had only its own metadata replaced.
    assert ["replace", DRIFTED_DIRECTORY] in case.backend.log
    assert "replacement_authority" in entries[DRIFTED_DIRECTORY]
    assert entries[DRIFTED_DIRECTORY]["prior"]["mode"] == "0777"
    # Adopted accounts were not mutated.
    for name in LEGACY_IDS:
        assert entries[f"account:{name}"]["prior"]["exists"] is True


def test_interrupted_quarantine_after_the_rename_completes_without_a_second_move(
    tmp_path: Path,
) -> None:
    case = LegacyCase(tmp_path)
    stray = case.step(f"legacy-quarantine:{case.seeded['stray']}")
    case.backend.fail_after_mutation = stray["step_id"]
    receipt = new_install_receipt(case.plan)

    with pytest.raises(RuntimeError, match="post-mutation"):
        case.apply(receipt)
    entry = next(e for e in receipt.to_dict()["journal"] if e["step_id"] == stray["step_id"])
    assert entry["status"] == "prepared"
    assert not case.seeded["stray"].exists()

    case.apply(receipt)

    entry = next(e for e in receipt.to_dict()["journal"] if e["step_id"] == stray["step_id"])
    assert entry["status"] == "completed"
    assert case.backend.log.count(["apply", stray["step_id"]]) == 1
    assert os.lstat(stray["destination"]).st_ino == stray["expected"]["ino"]
    assert receipt.to_dict()["state"] == "applied"


def test_prepared_quarantine_that_never_moved_is_redone(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    stray = case.step(f"legacy-quarantine:{case.seeded['stray']}")
    receipt = new_install_receipt(case.plan)
    original = case.backend.apply_step
    calls = {"count": 0}

    def crash_before_the_move(step):
        if step["step_id"] == stray["step_id"] and calls["count"] == 0:
            calls["count"] += 1
            raise SystemExit("killed after the prepared checkpoint, before the rename")
        return original(step)

    case.backend.apply_step = crash_before_the_move  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        case.apply(receipt)
    entry = next(e for e in receipt.to_dict()["journal"] if e["step_id"] == stray["step_id"])
    assert entry["status"] == "prepared"
    assert case.seeded["stray"].exists()

    case.apply(receipt)

    assert not case.seeded["stray"].exists()
    assert os.lstat(stray["destination"]).st_ino == stray["expected"]["ino"]
    assert receipt.to_dict()["state"] == "applied"


def test_replaced_quarantine_source_fails_before_the_first_mutation(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    receipt = new_install_receipt(case.plan)
    context = case.context()
    original_gate = context.apply_gate

    def gate():
        record = original_gate()
        # Replaced after the gate re-captured the host: same bytes, new inode.
        _replace_with_same_bytes(case.seeded["stray"])
        return record

    context.apply_gate = gate  # type: ignore[method-assign]

    with pytest.raises(InstallDriftError, match="stray.txt"):
        case.apply(receipt, legacy=context)

    assert receipt.to_dict()["journal"] == []
    assert _mutations(case.backend) == []
    assert not case.quarantine_root.exists()


def test_adopt_in_place_replacement_replays_after_a_partial_mutation(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    case.backend.fail_with_partial = DRIFTED_DIRECTORY
    receipt = new_install_receipt(case.plan)

    with pytest.raises(RuntimeError, match="partial replacement"):
        case.apply(receipt)
    entry = next(e for e in receipt.to_dict()["journal"] if e["step_id"] == DRIFTED_DIRECTORY)
    assert entry["status"] == "prepared"
    assert entry["adoption"]["source"] == "legacy-inventory"
    assert "replacement_authority" in entry

    case.apply(receipt)

    rows = [
        row
        for row in case.backend.log
        if row[1] == DRIFTED_DIRECTORY and row[0] != "inspect"
    ]
    assert rows == [
        ["replace", DRIFTED_DIRECTORY],
        ["rollback", DRIFTED_DIRECTORY],
        ["replace", DRIFTED_DIRECTORY],
    ]
    assert case.backend.states[DRIFTED_DIRECTORY] == _desired_state(case.step(DRIFTED_DIRECTORY))
    assert receipt.to_dict()["state"] == "applied"


# ---------------------------------------------------------------------------
# accounts: provenance is relaxed, collisions are not
# ---------------------------------------------------------------------------


def test_legacy_account_provenance_needs_an_exact_inventory_row(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    facts = _legacy_facts(case.plan, case.host)
    rows = case.context().adopted_account_rows()
    assert sorted(rows) == sorted(LEGACY_IDS)

    assert validate_preflight(
        case.plan, facts, legacy_accounts=rows, covered_paths=case.covered()
    ).ok
    with pytest.raises(AccountCollisionError, match="provenance"):
        validate_preflight(case.plan, facts, covered_paths=case.covered())

    forged = deepcopy(rows)
    forged["cortex-builder"]["uid_holders"] = {
        str(LEGACY_IDS["cortex-builder"][0]): ["cortex-builder", "twin"]
    }
    with pytest.raises(AccountCollisionError, match="legacy inventory row"):
        validate_preflight(
            case.plan, facts, legacy_accounts=forged, covered_paths=case.covered()
        )


def test_legacy_provenance_never_relaxes_account_collisions(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    rows = case.context().adopted_account_rows()
    uid, _gid = LEGACY_IDS["cortex-builder"]
    covered = case.covered()

    held = _legacy_facts(case.plan, case.host)
    held["account_uids"][uid] = "systemd-resolve"
    with pytest.raises(AccountCollisionError, match="already owned by systemd-resolve"):
        validate_preflight(case.plan, held, legacy_accounts=rows, covered_paths=covered)

    joined = _legacy_facts(case.plan, case.host)
    joined["groups"]["cortex-builder"]["members"] = ["legacy-operator"]
    with pytest.raises(AccountCollisionError, match="foreign supplementary members"):
        validate_preflight(case.plan, joined, legacy_accounts=rows, covered_paths=covered)

    unlocked = _legacy_facts(case.plan, case.host)
    unlocked["accounts"]["cortex-builder"]["password_locked"] = False
    with pytest.raises(AccountCollisionError, match="password"):
        validate_preflight(case.plan, unlocked, legacy_accounts=rows, covered_paths=covered)


def test_quarantined_paths_are_exempt_from_the_symlink_preflight(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    facts = _legacy_facts(case.plan, case.host)
    rows = case.context().adopted_account_rows()
    wrapper = str(case.seeded["codex"])
    assert facts["paths"][wrapper]["is_symlink"] is True

    with pytest.raises(install_core.UnsafeInstallPathError, match="codex"):
        validate_preflight(case.plan, facts, legacy_accounts=rows)
    assert validate_preflight(
        case.plan, facts, legacy_accounts=rows, covered_paths=case.covered()
    ).ok


# ---------------------------------------------------------------------------
# receipts
# ---------------------------------------------------------------------------


def _durable(monkeypatch: pytest.MonkeyPatch, case: LegacyCase) -> tuple[InstallReceipt, Path]:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _observed, _path: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _observed, _path: None)
    path = case.tmp_path / "receipts" / "adoption.json"
    path.parent.mkdir(mode=0o700)
    return new_install_receipt(case.plan, path=path), path


def _tampered(path: Path, mutate) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document), encoding="utf-8")


def _entry(document: dict, step_id: str) -> dict:
    return next(entry for entry in document["journal"] if entry["step_id"] == step_id)


def _quarantine_entry(document: dict) -> dict:
    return next(e for e in document["journal"] if e["step"]["kind"] == "legacy-quarantine")


@pytest.mark.parametrize(
    ("mutate", "named"),
    [
        (
            lambda d: _entry(d, "account:cortex-builder")["adoption"].update(row_sha256="0" * 64),
            "adoption",
        ),
        (
            lambda d: _entry(d, "account:cortex-builder").update(adopted_from_receipt=True),
            "adoption",
        ),
        (
            lambda d: _entry(d, "asset:repo-source-tree").update(
                adoption={"source": "legacy-inventory", "row_sha256": "1" * 64}
            ),
            "adoption",
        ),
        (lambda d: d.pop("legacy_adoption"), "legacy"),
        (lambda d: d["legacy_adoption"].update(apply_inventory_sha256="2" * 64), "legacy"),
        (lambda d: d["legacy_adoption"].update(quarantine_root="/srv/elsewhere"), "legacy"),
        (
            lambda d: _quarantine_entry(d)["quarantine_authority"].update(
                destination="/srv/elsewhere"
            ),
            "quarantine",
        ),
        (lambda d: _quarantine_entry(d).pop("quarantine_authority"), "quarantine"),
    ],
    ids=[
        "forged-row",
        "posing-as-receipt-adoption",
        "adoption-on-a-created-step",
        "missing-record",
        "apply-digest",
        "quarantine-root",
        "quarantine-destination",
        "quarantine-authority-missing",
    ],
)
def test_receipt_load_rejects_tampered_legacy_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate, named: str
) -> None:
    case = LegacyCase(tmp_path)
    receipt, path = _durable(monkeypatch, case)
    case.apply(receipt)
    assert InstallReceipt.load(path, expected_plan=case.plan).to_dict()["state"] == "applied"

    _tampered(path, mutate)

    with pytest.raises(InstallError, match=named):
        InstallReceipt.load(path, expected_plan=case.plan)


# ---------------------------------------------------------------------------
# rollback restores the legacy host and proves it
# ---------------------------------------------------------------------------

LATE_STEP = "generated:enforcement/reviewer-planner-codex-hooks"


def _failed_late(case: LegacyCase) -> InstallReceipt:
    case.backend.fail_after_mutation = LATE_STEP
    receipt = new_install_receipt(case.plan)
    with pytest.raises(RuntimeError, match="post-mutation"):
        case.apply(receipt)
    assert all(value is None for value in case.snapshot().values())
    return receipt


def test_rollback_restores_every_quarantined_object_and_proves_the_host(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    receipt = _failed_late(case)

    report = rollback_receipt(receipt, backend=case.backend, legacy_host=case.host)

    assert report.legacy_restored is True
    assert report.to_dict()["legacy_restored"] is True
    assert report.retained_drift == ()
    assert case.snapshot() == case.legacy_identities()
    assert case.recaptured_sha256() == case.block["inventory_sha256"]
    document = receipt.to_dict()
    assert document["state"] == "rolled-back"
    assert document["rollback"]["legacy_restored"] is True
    assert install_cli._receipt_restore_safe(document) is True
    # Unit files moved back: systemd re-reads them once, after the last restore.
    assert case.backend.reloads == 1
    last_unit_restore = max(
        index
        for index, row in enumerate(case.backend.log)
        if row[0] == "rollback" and "/etc/systemd/system/" in row[1]
    )
    assert case.backend.log.index(["daemon-reload", ""]) > last_unit_restore

    # The restored host passes the apply gate again.
    case.apply(receipt)
    assert receipt.to_dict()["state"] == "applied"


def test_rollback_after_a_complete_adoption_restores_the_legacy_host(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    receipt = new_install_receipt(case.plan)
    case.apply(receipt)

    report = rollback_receipt(receipt, backend=case.backend, legacy_host=case.host)

    assert report.legacy_restored is True
    assert case.snapshot() == case.legacy_identities()
    assert install_cli._receipt_restore_safe(receipt.to_dict()) is True


def test_rollback_keeps_an_object_in_quarantine_when_its_path_is_reoccupied(
    tmp_path: Path,
) -> None:
    case = LegacyCase(tmp_path)
    receipt = _failed_late(case)
    stray = case.step(f"legacy-quarantine:{case.seeded['stray']}")
    case.seeded["stray"].write_text("written after the quarantine\n")

    report = rollback_receipt(receipt, backend=case.backend, legacy_host=case.host)

    assert report.legacy_restored is False
    assert any(row["step_id"] == stray["step_id"] for row in report.retained_drift)
    assert os.lstat(stray["destination"]).st_ino == stray["expected"]["ino"]
    assert case.seeded["stray"].read_text() == "written after the quarantine\n"
    document = receipt.to_dict()
    assert document["state"] == "rollback-blocked"
    assert install_cli._receipt_restore_safe(document) is False


def test_rollback_is_not_restore_safe_when_the_host_differs_from_the_inventory(
    tmp_path: Path,
) -> None:
    case = LegacyCase(tmp_path)
    receipt = _failed_late(case)
    coordinator = case.base["roots"]["state"] + "/coordinator"
    case.host.acls[coordinator] = [
        {"account": "legacy-operator", "perms": "rwx", "default": False}
    ]

    report = rollback_receipt(receipt, backend=case.backend, legacy_host=case.host)

    assert report.legacy_restored is False
    assert case.snapshot() == case.legacy_identities()
    assert install_cli._receipt_restore_safe(receipt.to_dict()) is False


def test_cli_rollback_and_recover_prove_only_legacy_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.setattr(install_cli, "_legacy_host_backend", lambda: sentinel)

    assert install_cli._legacy_rollback_host(InstallReceipt({"journal": []})) is None
    assert (
        install_cli._legacy_rollback_host(
            InstallReceipt({"journal": [], "legacy_adoption": {"inventory_sha256": "0" * 64}})
        )
        is sentinel
    )


def test_rollback_without_a_legacy_host_cannot_claim_restoration(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    receipt = _failed_late(case)

    report = rollback_receipt(receipt, backend=case.backend)

    assert report.legacy_restored is False
    assert install_cli._receipt_restore_safe(receipt.to_dict()) is False


def test_unknown_state_scan_treats_restored_quarantine_sources_as_managed(
    tmp_path: Path,
) -> None:
    pool = tmp_path / "state" / "worktree"
    (pool / "job-1").mkdir(parents=True)
    (pool / "job-1" / "notes.txt").write_text("legacy\n")
    observed = pool.lstat()
    quarantine = {
        "step_id": f"legacy-quarantine:{pool}",
        "kind": "legacy-quarantine",
        "path": str(pool),
        "destination": f"{tmp_path}/q/{'a' * 16}/root{pool}",
        "expected": {
            "type": "directory",
            "uid": observed.st_uid,
            "gid": observed.st_gid,
            "mode": format(stat.S_IMODE(observed.st_mode), "04o"),
            "dev": observed.st_dev,
            "ino": observed.st_ino,
        },
    }
    created = {
        "step_id": "asset:dispatch-worktree-pool",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(pool),
    }
    document = {
        "journal": [],
        "rollback_journal": [
            {"step_id": quarantine["step_id"], "step": quarantine, "prior": {"exists": True}},
            {"step_id": created["step_id"], "step": created, "prior": {"exists": False}},
        ],
    }
    scan = install_backend.LocalInstallBackend(require_root=False).list_unknown_state

    assert scan(InstallReceipt(document)) == ()

    # A different directory at the same path is not the restored legacy object.
    pool.rename(tmp_path / "state" / "moved-away")
    (pool / "fresh").mkdir(parents=True)
    assert scan(InstallReceipt(document)) == (str(pool / "fresh"),)


# ---------------------------------------------------------------------------
# upgrade from an adoption receipt
# ---------------------------------------------------------------------------


def test_adoption_receipt_hands_off_to_an_upgrade_as_prior_receipt(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    adoption = new_install_receipt(case.plan)
    case.apply(adoption)
    adoption._document["qualified"] = True
    # The next plan comes from the same persisted host overlay.
    upgrade = install_cli._plan_document(case.config, case.bundle, overlay=case.overlay)
    assert "legacy_adoption" not in upgrade
    backend = case.backend
    backend.plan = upgrade
    backend.facts = _legacy_facts(upgrade, case.host)
    receipt = new_install_receipt(upgrade)

    apply_plan(
        upgrade,
        confirm_sha256=plan_sha256(upgrade),
        receipt=receipt,
        prior_receipt=adoption,
        backend=backend,
    )

    entries = {entry["step_id"]: entry for entry in receipt.to_dict()["journal"]}
    assert receipt.to_dict()["state"] == "applied"
    assert entries["account:cortex-builder"]["adopted_from_receipt"] is True
    assert entries[DRIFTED_DIRECTORY]["adopted_from_receipt"] is True

    forged = deepcopy(adoption.to_dict())
    _entry(forged, "account:cortex-builder")["adoption"]["row_sha256"] = "0" * 64
    with pytest.raises(AccountCollisionError, match="cortex-builder"):
        apply_plan(
            upgrade,
            confirm_sha256=plan_sha256(upgrade),
            receipt=new_install_receipt(upgrade),
            prior_receipt=InstallReceipt(forged),
            backend=backend,
        )


def test_quarantine_root_must_not_overlap_the_receipt_directory(tmp_path: Path) -> None:
    receipts = str(tmp_path / "host/var/lib/cortex-install-receipts")
    with pytest.raises(legacy.LegacyAdoptionPlanError) as caught:
        LegacyCase(tmp_path, request={"quarantine_root": receipts})
    assert any("receipt" in failure for failure in caught.value.failures)


# ---------------------------------------------------------------------------
# the quarantine backend on a real filesystem
# ---------------------------------------------------------------------------


def _file_step(tmp_path: Path, source: Path) -> dict:
    observed = source.lstat()
    step = {
        "step_id": f"legacy-quarantine:{source}",
        "kind": "legacy-quarantine",
        "path": str(source),
        "destination": legacy.quarantine_destination(
            str(tmp_path / "quarantine"), "a" * 64, str(source)
        ),
        "expected": {
            "type": "file",
            "uid": observed.st_uid,
            "gid": observed.st_gid,
            "mode": format(stat.S_IMODE(observed.st_mode), "04o"),
            "dev": observed.st_dev,
            "ino": observed.st_ino,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "row_sha256": "b" * 64,
        "operations": ["snapshot", "rename-noreplace"],
        "rollback_policy": "restore",
    }
    step["desired_sha256"] = legacy._quarantine_step_digest(step)
    return step


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "legacy" / "etc" / "cortex-old.conf"
    source.parent.mkdir(parents=True)
    source.write_text("legacy\n")
    return source


def _entry_for(step: dict) -> dict:
    expected = step["expected"]
    return {
        "step_id": step["step_id"],
        "step": step,
        "status": "completed",
        "prior": {
            "exists": True,
            **{key: expected[key] for key in ("type", "uid", "gid", "mode", "dev", "ino")},
        },
        "quarantine_authority": {
            "source": {"dev": expected["dev"], "ino": expected["ino"]},
            "destination": step["destination"],
        },
    }


def test_quarantine_moves_and_restores_the_same_inode(tmp_path: Path) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    inode = source.lstat().st_ino

    state = install_backend._legacy_quarantine_state(step)
    assert state["exists"] is True and state["destination"] is None
    install_backend._legacy_quarantine_move(step)

    assert not source.exists()
    destination = Path(step["destination"])
    assert destination.lstat().st_ino == inode
    assert destination.read_text() == "legacy\n"
    parent = destination.parent
    while parent != tmp_path:
        assert stat.S_IMODE(parent.lstat().st_mode) == 0o700, parent
        parent = parent.parent
    moved = install_backend._legacy_quarantine_state(step)
    assert moved["exists"] is False and moved["source"] is None
    assert moved["destination"]["ino"] == inode

    install_backend._legacy_quarantine_restore(_entry_for(step))
    assert source.lstat().st_ino == inode
    assert not destination.exists()


def test_quarantine_refuses_an_existing_destination(tmp_path: Path) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    destination = Path(step["destination"])
    install_backend._legacy_quarantine_move(step)
    install_backend._legacy_quarantine_restore(_entry_for(step))
    destination.write_text("someone else\n")

    with pytest.raises(InstallDriftError, match="already exists"):
        install_backend._legacy_quarantine_move(step)

    assert source.read_text() == "legacy\n"
    assert source.lstat().st_ino == step["expected"]["ino"]
    assert destination.read_text() == "someone else\n"


def test_quarantine_refuses_another_filesystem_without_copy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    device = source.lstat().st_dev
    original_device = install_backend._filesystem_device
    monkeypatch.setattr(install_backend, "_filesystem_device", lambda _fd: device + 1)

    with pytest.raises(InstallDriftError, match="filesystem"):
        install_backend._legacy_quarantine_move(step)
    assert source.read_text() == "legacy\n"
    assert not Path(step["destination"]).exists()

    # A bind mount can share st_dev and still refuse the rename with EXDEV.
    monkeypatch.setattr(install_backend, "_filesystem_device", original_device)

    def cross_device(*_args):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(install_backend, "_renameat2_noreplace", cross_device)
    with pytest.raises(InstallDriftError, match="filesystem"):
        install_backend._legacy_quarantine_move(step)
    assert source.read_text() == "legacy\n"
    assert not Path(step["destination"]).exists()


def test_quarantine_refuses_a_replaced_source(tmp_path: Path) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    _replace_with_same_bytes(source)

    with pytest.raises(InstallDriftError, match="changed"):
        install_backend._legacy_quarantine_move(step)
    assert source.read_text() == "legacy\n"
    assert not Path(step["destination"]).exists()


def test_quarantine_fails_closed_without_renameat2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    monkeypatch.setattr(install_backend, "_RENAMEAT2_SYSCALL", {})

    with pytest.raises(InstallError, match="RENAME_NOREPLACE"):
        install_backend._legacy_quarantine_move(step)
    assert source.read_text() == "legacy\n"
    assert not Path(step["destination"]).exists()

    def unsupported(*_args):
        ctypes_errno = errno.ENOSYS
        raise OSError(ctypes_errno, os.strerror(ctypes_errno))

    monkeypatch.undo()
    _relax_quarantine_owner(monkeypatch)
    monkeypatch.setattr(install_backend, "_renameat2_noreplace", unsupported)
    with pytest.raises(InstallError, match="RENAME_NOREPLACE"):
        install_backend._legacy_quarantine_move(step)
    assert source.read_text() == "legacy\n"


def test_quarantine_chain_must_be_private_and_symlink_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    root = tmp_path / "quarantine"

    root.mkdir()
    root.chmod(0o755)
    with pytest.raises(install_core.UnsafeInstallPathError, match="quarantine"):
        install_backend._legacy_quarantine_move(step)
    root.rmdir()

    (tmp_path / "elsewhere").mkdir(mode=0o700)
    root.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(install_core.UnsafeInstallPathError):
        install_backend._legacy_quarantine_move(step)
    root.unlink()
    assert source.read_text() == "legacy\n"
    assert not any((tmp_path / "elsewhere").iterdir())

    if os.geteuid() != 0:
        # Without the test override the chain must be owned by root.
        for name in _QUARANTINE_VALIDATORS:
            monkeypatch.setattr(install_backend, name, _ORIGINAL_VALIDATORS[name])
        with pytest.raises(install_core.UnsafeInstallPathError):
            install_backend._legacy_quarantine_move(step)
        assert source.read_text() == "legacy\n"


def test_restore_refuses_an_occupied_original_path(tmp_path: Path) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    install_backend._legacy_quarantine_move(step)
    source.write_text("new occupant\n")

    with pytest.raises(InstallDriftError, match="occupied"):
        install_backend._legacy_quarantine_restore(_entry_for(step))

    assert source.read_text() == "new occupant\n"
    assert Path(step["destination"]).lstat().st_ino == step["expected"]["ino"]


def test_renameat2_noreplace_on_the_real_filesystem(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "a").write_text("a\n")
    (right / "b").write_text("b\n")
    inode = (left / "a").lstat().st_ino
    left_fd = os.open(left, os.O_RDONLY | os.O_DIRECTORY)
    right_fd = os.open(right, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            install_backend._renameat2_noreplace(left_fd, "a", right_fd, "b")
        assert (left / "a").read_text() == "a\n"
        assert (right / "b").read_text() == "b\n"

        install_backend._renameat2_noreplace(left_fd, "a", right_fd, "c")
        assert not (left / "a").exists()
        assert (right / "c").lstat().st_ino == inode
    finally:
        os.close(left_fd)
        os.close(right_fd)
