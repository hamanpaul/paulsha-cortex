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
import shutil
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
        "cortex_account_universal_nopasswd": {"accounts": [], "unproven": None},
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
    #: Publish the venv slot with a staged "ready" authority (inode bound), as
    #: the real backend does; the base scenario keeps the legacy form.
    ready_venv_authority = False
    VENV_INODE = 4242

    def _ready_venv_authority(self, step) -> dict[str, object]:
        slot = Path(step["path"])
        return {
            "state": "ready",
            "path": step["path"],
            "staging_path": str(slot.with_name(f".{slot.name}.cortex-staging")),
            "device": 1,
            "inode": self.VENV_INODE,
            "tree_sha256": TREE_SHA256,
        }

    def created_tree_identity(self, step) -> dict[str, object]:
        identity = {"device": 1, "inode": 7000 + len(self.identities), "tree_sha256": "f" * 64}
        self.identities[f"tree:{step['path']}"] = identity
        return dict(identity)

    def discard_created_tree(self, path: str, identity, *, quarantine_root: str, key: str) -> None:
        self.log.append(["discard", path])
        assert quarantine_root == self.plan["legacy_adoption"]["quarantine_root"]
        venv = next((s for s in self.plan["apply_order"] if s["kind"] == "venv"), None)
        if venv is not None and path == venv["path"]:
            expected = {"device": 1, "inode": self.VENV_INODE, "tree_sha256": TREE_SHA256}
            if dict(identity) != expected or not self.venv_slot:
                raise InstallDriftError(f"venv slot is not the one this receipt created: {path}")
            self.venv_slot = False
            return
        if dict(identity) != self.identities.get(f"tree:{path}"):
            raise InstallDriftError(f"tree is not the one this receipt created: {path}")

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
        assert kind != "legacy-quarantine", "a quarantine moves only through its entry"
        if kind == "venv":
            if creation_checkpoint is not None:
                creation_checkpoint(
                    self._ready_venv_authority(step)
                    if self.ready_venv_authority
                    else {"path": step["path"], "tree_sha256": TREE_SHA256}
                )
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

    def quarantine_step(self, entry) -> dict[str, object]:
        step_id = entry["step_id"]
        if self.fail_before_mutation == step_id:
            self.fail_before_mutation = None
            raise RuntimeError(f"injected failure before mutating {step_id}")
        self.log.append(["apply", step_id])
        outcome = dict(install_backend._legacy_quarantine_move(entry))
        if self.fail_after_mutation == step_id:
            self.fail_after_mutation = None
            raise RuntimeError(f"injected post-mutation interruption at {step_id}")
        return outcome

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
# make a legacy change pass.  Re-recorded only for non-legacy changes: #716
# added `version: "1"` to the manager gh config, and #716 again added
# `StateDirectory=` for the gate worktree slot to the gate job template units
# (generated unit bytes only; no legacy code path changed), and #716 once more
# added the toolchain-first `PATH` to the generated manager EnvironmentFile,
# and #716 again added `DO_NOT_TRACK=1` there (openspec under `--jitless`). #1289
# adds launch-authority installer assets to this base install/apply/rollback trace.
REGRESSION_GOLDEN_SHA256 = (
    "3992b4ba9038e042b536838feacd2b81633e615136315f336870b21f55898674"
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

    def __init__(
        self,
        tmp_path: Path,
        *,
        host=None,
        request: dict | None = None,
        backend_class: type | None = None,
    ) -> None:
        self.backend_class = backend_class or MemoryInstallBackend
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
        backend = self.backend_class(self.plan, facts=_legacy_facts(self.plan, self.host))
        backend.live_paths = True
        backend.stable_replacement_identity = True
        backend.ready_venv_authority = True
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


def _coordinator(case: LegacyCase) -> str:
    # An adopted-in-place directory: nothing in the plan quarantines it.
    return str(Path(case.base["roots"]["state"]) / "coordinator")


def _new_writable(case: LegacyCase, _monkeypatch) -> None:
    case.host.writable = {LEGACY_IDS["cortex-builder"][0]: {_coordinator(case)}}


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

    assert f"cortex-builder can write {_coordinator(case)}," in str(caught.value)


def test_apply_gate_ignores_writable_paths_inside_quarantined_objects(
    tmp_path: Path,
) -> None:
    # #1282: what a job account can write inside an object the plan moves
    # into the quarantine is gone from its path once apply runs; the plan
    # needs no census exception for it and the apply gate does not refuse.
    def sandboxes_writable(seeded, fake, _base) -> None:
        fake.writable = {LEGACY_IDS["cortex-reviewer-planner"][0]: {str(seeded["sandboxes"])}}

    case = LegacyCase(tmp_path, host=sandboxes_writable)
    assert case.block["census_exceptions"] == []
    receipt = new_install_receipt(case.plan)

    case.apply(receipt)

    assert receipt.to_dict()["state"] == "applied"
    assert not os.path.lexists(case.seeded["sandboxes"])


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
        parent = os.lstat(os.path.dirname(step["path"]))
        assert entry["quarantine_authority"] == {
            "source": {"dev": expected["dev"], "ino": expected["ino"]},
            "source_parent": {"dev": parent.st_dev, "ino": parent.st_ino},
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
    original = case.backend.quarantine_step
    calls = {"count": 0}

    def crash_before_the_move(entry):
        if entry["step_id"] == stray["step_id"] and calls["count"] == 0:
            calls["count"] += 1
            raise SystemExit("killed after the prepared checkpoint, before the rename")
        return original(entry)

    case.backend.quarantine_step = crash_before_the_move  # type: ignore[method-assign]
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


def test_quarantine_parent_must_be_the_inventoried_directory(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    stray = case.step(f"legacy-quarantine:{case.seeded['stray']}")
    context = case.context()
    reviewed = context.parent_identity(stray)
    assert reviewed == {
        "dev": case.seeded["stray"].parent.lstat().st_dev,
        "ino": case.seeded["stray"].parent.lstat().st_ino,
    }
    original = context.parent_identity

    def swapped(step):
        bound = original(step)
        if step["step_id"] == stray["step_id"]:
            return {"dev": bound["dev"], "ino": bound["ino"] + 1}
        return bound

    context.parent_identity = swapped  # type: ignore[method-assign]
    receipt = new_install_receipt(case.plan)

    with pytest.raises(InstallDriftError, match="parent directory"):
        case.apply(receipt, legacy=context)

    assert receipt.to_dict()["journal"] == []
    assert _mutations(case.backend) == []
    assert not case.quarantine_root.exists()


def _race_the_stray(case: LegacyCase, monkeypatch, *, reoccupy: bool = False):
    """Swap ``stray.txt`` after the quarantine's checks, before its rename."""

    swap = _SwapBeforeRename(case.seeded["stray"], reoccupy=reoccupy)

    def racing(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
        if source == "stray.txt":
            return swap(source_fd, source, destination_fd, destination)
        return swap.original(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(install_backend, "_renameat2_noreplace", racing)
    return swap


def test_substituted_quarantine_source_is_moved_back_and_never_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = LegacyCase(tmp_path)
    stray = case.step(f"legacy-quarantine:{case.seeded['stray']}")
    swap = _race_the_stray(case, monkeypatch)
    receipt = new_install_receipt(case.plan)

    with pytest.raises(InstallDriftError, match="moved back"):
        case.apply(receipt)

    entry = _entry(receipt.to_dict(), stray["step_id"])
    assert entry["status"] == "prepared"
    assert "quarantine_unexpected" not in entry
    assert case.seeded["stray"].lstat().st_ino == swap.substitute_inode
    assert not os.path.lexists(stray["destination"])
    # Replay refuses the substitute instead of quarantining it.
    with pytest.raises(InstallDriftError, match="stray.txt"):
        case.apply(receipt)
    assert _entry(receipt.to_dict(), stray["step_id"])["status"] == "prepared"

    report = rollback_receipt(receipt, backend=case.backend, legacy_host=case.host)

    assert any(row["step_id"] == stray["step_id"] for row in report.retained_drift)
    assert report.legacy_restored is False
    assert case.seeded["stray"].read_text() == "substitute\n"
    assert install_cli._receipt_restore_safe(receipt.to_dict()) is False


def test_stranded_substitute_is_recorded_and_never_restored_as_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = LegacyCase(tmp_path)
    stray = case.step(f"legacy-quarantine:{case.seeded['stray']}")
    receipt, path = _durable(monkeypatch, case)
    swap = _race_the_stray(case, monkeypatch, reoccupy=True)

    with pytest.raises(install_core.QuarantineSubstitutionError):
        case.apply(receipt)

    loaded = InstallReceipt.load(path, expected_plan=case.plan)
    entry = _entry(loaded.to_dict(), stray["step_id"])
    assert entry["status"] == "prepared"
    assert entry["quarantine_unexpected"]["destination"]["ino"] == swap.substitute_inode
    with pytest.raises(InstallDriftError, match="unexpected"):
        case.apply(loaded)

    report = rollback_receipt(loaded, backend=case.backend, legacy_host=case.host)

    row = next(row for row in report.retained_drift if row["step_id"] == stray["step_id"])
    assert "unexpected" in row["observed"]["error"]
    assert Path(stray["destination"]).read_text() == "substitute\n"
    assert case.seeded["stray"].read_text() == "second occupant\n"
    assert report.legacy_restored is False
    assert loaded.to_dict()["state"] == "rollback-blocked"
    assert install_cli._receipt_restore_safe(loaded.to_dict()) is False
    assert "quarantine_unexpected" in _rollback_entry(loaded.to_dict(), stray["step_id"])


def test_rollback_keeps_a_prepared_quarantine_whose_destination_was_occupied(
    tmp_path: Path,
) -> None:
    case = LegacyCase(tmp_path)
    stray = case.step(f"legacy-quarantine:{case.seeded['stray']}")
    receipt = new_install_receipt(case.plan)
    original = case.backend.quarantine_step

    def killed_before_the_rename(entry):
        if entry["step_id"] == stray["step_id"]:
            raise SystemExit("killed after the prepared checkpoint, before the rename")
        return original(entry)

    case.backend.quarantine_step = killed_before_the_rename  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        case.apply(receipt)
    assert _entry(receipt.to_dict(), stray["step_id"])["status"] == "prepared"
    # Something appears at the (never used) quarantine destination.
    destination = Path(stray["destination"])
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text("planted\n")

    report = rollback_receipt(receipt, backend=case.backend, legacy_host=case.host)

    row = next(row for row in report.retained_drift if row["step_id"] == stray["step_id"])
    assert "destination occupied by an unexpected object" in row["observed"]["error"]
    assert _rollback_entry(receipt.to_dict(), stray["step_id"])["status"] == "prepared"
    assert report.legacy_restored is False
    assert receipt.to_dict()["rollback"]["legacy_restored"] is False
    assert receipt.to_dict()["state"] == "rollback-blocked"
    assert install_cli._receipt_restore_safe(receipt.to_dict()) is False
    assert case.seeded["stray"].lstat().st_ino == stray["expected"]["ino"]
    assert destination.read_text() == "planted\n"


def _rollback_entry(document: dict, step_id: str) -> dict:
    return next(entry for entry in document["journal"] if entry["step_id"] == step_id)


def test_replay_refuses_a_destination_that_is_not_the_recorded_inode(tmp_path: Path) -> None:
    case = LegacyCase(tmp_path)
    stray = case.step(f"legacy-quarantine:{case.seeded['stray']}")
    case.backend.fail_after_mutation = stray["step_id"]
    receipt = new_install_receipt(case.plan)
    with pytest.raises(RuntimeError, match="post-mutation"):
        case.apply(receipt)
    _replace_with(Path(stray["destination"]), "stray\n")

    with pytest.raises(InstallDriftError, match="stray.txt"):
        case.apply(receipt)

    assert _entry(receipt.to_dict(), stray["step_id"])["status"] == "prepared"


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
        (
            lambda d: _quarantine_entry(d)["quarantine_authority"].pop("source_parent"),
            "quarantine",
        ),
        (
            lambda d: _quarantine_entry(d).update(
                quarantine_unexpected={"destination": None, "error": "forged"}
            ),
            "quarantine",
        ),
        (
            lambda d: _entry(d, "account:cortex-builder").update(
                legacy_creation={"device": 1, "inode": 2, "tree_sha256": "3" * 64}
            ),
            "legacy creation",
        ),
        (
            lambda d: _entry(d, "repository:paulsha-cortex")["legacy_creation"].pop(
                "tree_sha256"
            ),
            "legacy creation",
        ),
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
        "quarantine-parent-missing",
        "unexpected-on-a-completed-entry",
        "creation-on-an-adopted-account",
        "creation-without-tree-digest",
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


def _schema_version(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))["schema_version"]


def test_legacy_receipts_are_schema_v3_and_plain_receipts_stay_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = LegacyCase(tmp_path)
    receipt, path = _durable(monkeypatch, case)
    assert _schema_version(path) == 3
    case.apply(receipt)
    assert _schema_version(path) == 3
    assert InstallReceipt.load(path, expected_plan=case.plan).to_dict()["schema_version"] == 3

    plain_path = tmp_path / "receipts" / "plain.json"
    new_install_receipt(_plain_plan(tmp_path / "plain"), path=plain_path)
    assert _schema_version(plain_path) == 2

    # An installer that reads only v1/v2 (every one before legacy adoption)
    # refuses the legacy receipt at load instead of failing mid-rollback on a
    # step kind it does not know.
    monkeypatch.setattr(install_core, "_READABLE_RECEIPT_SCHEMA_VERSIONS", (1, 2))
    with pytest.raises(InstallError, match="invalid receipt schema"):
        InstallReceipt.load(path)


@pytest.mark.parametrize("version", [1, 2])
def test_receipt_v1_v2_must_not_carry_a_legacy_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    case = LegacyCase(tmp_path)
    receipt, path = _durable(monkeypatch, case)
    case.apply(receipt)

    _tampered(path, lambda document: document.update(schema_version=version))

    with pytest.raises(InstallError, match="invalid receipt schema"):
        InstallReceipt.load(path, expected_plan=case.plan)


def test_receipt_v3_must_carry_a_legacy_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _observed, _path: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _observed, _path: None)
    path = tmp_path / "receipts" / "plain.json"
    path.parent.mkdir(mode=0o700)
    new_install_receipt(_plain_plan(tmp_path / "plain"), path=path)

    _tampered(path, lambda document: document.update(schema_version=3))

    with pytest.raises(InstallError, match="invalid receipt schema"):
        InstallReceipt.load(path)


def test_v3_adoption_receipt_hands_off_to_an_upgrade_as_prior_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = LegacyCase(tmp_path)
    adoption, path = _durable(monkeypatch, case)
    case.apply(adoption)
    _tampered(path, lambda document: document.update(qualified=True))
    prior = InstallReceipt.load(path)
    assert prior.to_dict()["schema_version"] == 3
    upgrade = install_cli._plan_document(case.config, case.bundle, overlay=case.overlay)
    case.backend.plan = upgrade
    case.backend.facts = _legacy_facts(upgrade, case.host)
    upgrade_path = tmp_path / "receipts" / "upgrade.json"
    receipt = new_install_receipt(upgrade, path=upgrade_path)

    apply_plan(
        upgrade,
        confirm_sha256=plan_sha256(upgrade),
        receipt=receipt,
        prior_receipt=prior,
        backend=case.backend,
    )

    loaded = InstallReceipt.load(upgrade_path, expected_plan=upgrade)
    assert loaded.to_dict()["state"] == "applied"
    assert loaded.to_dict()["schema_version"] == 2
    assert _schema_version(upgrade_path) == 2


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


class IdentityLegacyBackend(MemoryInstallBackend):
    """The memory backend plus the real quarantine identity (tree digest)."""

    def legacy_quarantine_identity(self, path) -> dict[str, object]:
        try:
            return install_backend._legacy_quarantine_identity(Path(path))
        except PermissionError:
            # A mode-0 fake credential (or a tree holding one): root hashes it
            # on a host, the test user cannot.  Bind it by inode only here.
            observed = os.lstat(path)
            return {
                "device": observed.st_dev,
                "inode": observed.st_ino,
                "type": install_backend._file_type_name(observed.st_mode),
                "tree_sha256": "0" * 64,
            }


def _stale_special_files(seeded, _fake, base) -> None:
    state = Path(base["roots"]["state"])
    os.mknod(seeded["sandboxes"] / "job-9" / "agent.sock", 0o600 | stat.S_IFSOCK)
    os.mkfifo(state / "legacy-imported" / "notify.fifo", 0o600)
    os.mknod(state / "manager-control.sock", 0o600 | stat.S_IFSOCK)


def test_stale_sockets_and_fifos_are_quarantined_recorded_and_restored(
    tmp_path: Path,
) -> None:
    # #1282: the reference host's apply failed after the move with "tree contains an
    # unsupported object" for a stale socket.  The quarantine now records
    # sockets and FIFOs by type and mode, and rollback still restores the
    # exact reviewed inventory digest.
    case = LegacyCase(
        tmp_path, host=_stale_special_files, backend_class=IdentityLegacyBackend
    )
    state = Path(case.base["roots"]["state"])
    top = str(state / "manager-control.sock")
    reasons = {row["path"]: row["reason"] for row in case.block["quarantine"]}
    assert reasons[top] == "state-top"

    receipt = _failed_late(case)

    entries = {entry["step_id"]: entry for entry in receipt.to_dict()["journal"]}
    identities = {
        entries[step["step_id"]]["step"]["path"]: entries[step["step_id"]]["quarantine_identity"]
        for step in case.quarantine_steps()
    }
    assert identities[top]["type"] == "socket"
    special = (top, str(case.seeded["sandboxes"]), str(case.seeded["legacy_imported"]))
    for path in special[1:]:
        assert identities[path]["type"] == "directory"
    for step in case.quarantine_steps():
        if step["path"] in special:
            assert install_backend._legacy_quarantine_identity(
                Path(step["destination"])
            ) == identities[step["path"]]

    report = rollback_receipt(receipt, backend=case.backend, legacy_host=case.host)

    assert report.legacy_restored is True
    assert case.snapshot() == case.legacy_identities()
    assert stat.S_ISSOCK(os.lstat(top).st_mode)
    assert stat.S_ISFIFO(os.lstat(state / "legacy-imported" / "notify.fifo").st_mode)
    assert case.recaptured_sha256() == case.block["inventory_sha256"]


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


def _entry_for(step: dict, *, parent: tuple[int, int] | None = None) -> dict:
    """The prepared journal entry core writes before the move."""

    expected = step["expected"]
    if parent is None:
        observed = Path(step["path"]).parent.lstat()
        parent = (observed.st_dev, observed.st_ino)
    return {
        "step_id": step["step_id"],
        "step": step,
        "status": "prepared",
        "prior": {
            "exists": True,
            **{key: expected[key] for key in ("type", "uid", "gid", "mode", "dev", "ino")},
        },
        "quarantine_authority": {
            "source": {"dev": expected["dev"], "ino": expected["ino"]},
            "source_parent": {"dev": parent[0], "ino": parent[1]},
            "destination": step["destination"],
        },
    }


class _SwapBeforeRename:
    """Stand-in for a writer of the source's parent racing the quarantine.

    The first rename (the move into quarantine) finds a substitute where the
    checked source was: the swap happens after every pre-rename check.  The
    ``reoccupy`` option also refills the original name before the move back.
    """

    def __init__(self, source: Path, *, reoccupy: bool = False) -> None:
        self.source = source
        self.reoccupy = reoccupy
        self.calls: list[tuple[str, str]] = []
        self.substitute_inode: int | None = None
        self.original = install_backend._renameat2_noreplace

    def __call__(self, source_fd: int, source: str, destination_fd: int, destination: str) -> None:
        self.calls.append((source, destination))
        if len(self.calls) == 1:
            _replace_with(self.source, "substitute\n")
            self.substitute_inode = self.source.lstat().st_ino
        elif len(self.calls) == 2 and self.reoccupy:
            self.source.write_text("second occupant\n")
        self.original(source_fd, source, destination_fd, destination)


def _replace_with(path: Path, content: str) -> None:
    replacement = path.with_name(f".{path.name}.swap")
    replacement.write_text(content)
    os.replace(replacement, path)


def test_quarantine_moves_and_restores_the_same_inode(tmp_path: Path) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    inode = source.lstat().st_ino

    state = install_backend._legacy_quarantine_state(step)
    assert state["exists"] is True and state["destination"] is None
    install_backend._legacy_quarantine_move(_entry_for(step))

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
    install_backend._legacy_quarantine_move(_entry_for(step))
    install_backend._legacy_quarantine_restore(_entry_for(step))
    destination.write_text("someone else\n")

    with pytest.raises(InstallDriftError, match="already exists"):
        install_backend._legacy_quarantine_move(_entry_for(step))

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
        install_backend._legacy_quarantine_move(_entry_for(step))
    assert source.read_text() == "legacy\n"
    assert not Path(step["destination"]).exists()

    # A bind mount can share st_dev and still refuse the rename with EXDEV.
    monkeypatch.setattr(install_backend, "_filesystem_device", original_device)

    def cross_device(*_args):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(install_backend, "_renameat2_noreplace", cross_device)
    with pytest.raises(InstallDriftError, match="filesystem"):
        install_backend._legacy_quarantine_move(_entry_for(step))
    assert source.read_text() == "legacy\n"
    assert not Path(step["destination"]).exists()


def test_quarantine_refuses_a_replaced_source(tmp_path: Path) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    _replace_with_same_bytes(source)

    with pytest.raises(InstallDriftError, match="changed"):
        install_backend._legacy_quarantine_move(_entry_for(step))
    assert source.read_text() == "legacy\n"
    assert not Path(step["destination"]).exists()


def test_quarantine_fails_closed_without_renameat2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    monkeypatch.setattr(install_backend, "_RENAMEAT2_SYSCALL", {})

    with pytest.raises(InstallError, match="RENAME_NOREPLACE"):
        install_backend._legacy_quarantine_move(_entry_for(step))
    assert source.read_text() == "legacy\n"
    assert not Path(step["destination"]).exists()

    def unsupported(*_args):
        ctypes_errno = errno.ENOSYS
        raise OSError(ctypes_errno, os.strerror(ctypes_errno))

    monkeypatch.undo()
    _relax_quarantine_owner(monkeypatch)
    monkeypatch.setattr(install_backend, "_renameat2_noreplace", unsupported)
    with pytest.raises(InstallError, match="RENAME_NOREPLACE"):
        install_backend._legacy_quarantine_move(_entry_for(step))
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
        install_backend._legacy_quarantine_move(_entry_for(step))
    root.rmdir()

    (tmp_path / "elsewhere").mkdir(mode=0o700)
    root.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(install_core.UnsafeInstallPathError):
        install_backend._legacy_quarantine_move(_entry_for(step))
    root.unlink()
    assert source.read_text() == "legacy\n"
    assert not any((tmp_path / "elsewhere").iterdir())

    if os.geteuid() != 0:
        # Without the test override the chain must be owned by root.
        for name in _QUARANTINE_VALIDATORS:
            monkeypatch.setattr(install_backend, name, _ORIGINAL_VALIDATORS[name])
        with pytest.raises(install_core.UnsafeInstallPathError):
            install_backend._legacy_quarantine_move(_entry_for(step))
        assert source.read_text() == "legacy\n"


def test_quarantine_moves_a_source_substituted_before_the_rename_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    swap = _SwapBeforeRename(source)
    monkeypatch.setattr(install_backend, "_renameat2_noreplace", swap)

    with pytest.raises(InstallDriftError, match="moved back") as caught:
        install_backend._legacy_quarantine_move(_entry_for(step))

    assert not isinstance(caught.value, install_core.QuarantineSubstitutionError)
    assert [call[0] for call in swap.calls] == ["cortex-old.conf", "cortex-old.conf"]
    assert source.read_text() == "substitute\n"
    assert source.lstat().st_ino == swap.substitute_inode
    assert not os.path.lexists(step["destination"])


def test_quarantine_reports_a_stranded_substitute_when_the_move_back_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    swap = _SwapBeforeRename(source, reoccupy=True)
    monkeypatch.setattr(install_backend, "_renameat2_noreplace", swap)

    with pytest.raises(install_core.QuarantineSubstitutionError) as caught:
        install_backend._legacy_quarantine_move(_entry_for(step))

    assert isinstance(caught.value, InstallDriftError)
    assert caught.value.unexpected["ino"] == swap.substitute_inode
    assert caught.value.unexpected["ino"] != step["expected"]["ino"]
    assert Path(step["destination"]).read_text() == "substitute\n"
    assert source.read_text() == "second occupant\n"


def test_quarantine_refuses_a_swapped_source_parent(tmp_path: Path) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    observed = source.parent.lstat()

    with pytest.raises(InstallDriftError, match="parent"):
        install_backend._legacy_quarantine_move(
            _entry_for(step, parent=(observed.st_dev, observed.st_ino + 1))
        )

    assert source.read_text() == "legacy\n"
    assert not os.path.lexists(step["destination"])


def test_quarantine_never_resolves_the_source_parent_through_a_symlink(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    entry = _entry_for(step)
    # The parent is swapped for a symlink to the very same directory: the
    # recorded inode would match if the chain followed it.
    source.parent.rename(tmp_path / "legacy" / "etc.real")
    source.parent.symlink_to(tmp_path / "legacy" / "etc.real")

    with pytest.raises(install_core.UnsafeInstallPathError):
        install_backend._legacy_quarantine_move(entry)

    assert (tmp_path / "legacy" / "etc.real" / "cortex-old.conf").read_text() == "legacy\n"
    assert not os.path.lexists(step["destination"])


def test_restore_refuses_a_swapped_original_parent(tmp_path: Path) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    entry = _entry_for(step)
    install_backend._legacy_quarantine_move(entry)
    source.parent.rename(tmp_path / "legacy" / "etc.moved")
    source.parent.mkdir()

    with pytest.raises(InstallDriftError, match="parent"):
        install_backend._legacy_quarantine_restore(entry)

    assert not source.exists()
    assert Path(step["destination"]).lstat().st_ino == step["expected"]["ino"]


def test_a_quarantine_moves_only_through_its_prepared_entry(tmp_path: Path) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    backend = install_backend.LocalInstallBackend(require_root=False)

    with pytest.raises(InstallPlanError, match="prepared"):
        backend.apply_step(step)
    with pytest.raises(InstallPlanError, match="prepared"):
        backend.apply_step_checkpointed(step, {}, lambda _authority: None)
    assert source.read_text() == "legacy\n"

    backend.quarantine_step(_entry_for(step))
    assert Path(step["destination"]).lstat().st_ino == step["expected"]["ino"]


def test_restore_refuses_an_occupied_original_path(tmp_path: Path) -> None:
    source = _source(tmp_path)
    step = _file_step(tmp_path, source)
    install_backend._legacy_quarantine_move(_entry_for(step))
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


# ---------------------------------------------------------------------------
# rollback removes what the adoption created in the legacy objects' places
# ---------------------------------------------------------------------------


class FilesystemLegacyBackend(MemoryInstallBackend):
    """The memory backend, except that what apply creates exists for real.

    Directories the plan creates, the source repository clone and the venv
    slot are made on the temporary tree, so a rollback that leaves any of them
    behind shows up in the re-captured inventory -- and keeps a quarantined
    legacy object from moving back -- exactly as on a host.
    """

    ready_venv_authority = True

    @staticmethod
    def _created_directory(step) -> bool:
        return step["kind"] == "asset" and step.get("asset_type") == "directory"

    def _venv_state(self, step) -> dict[str, object]:
        slot = Path(step["path"])
        if not slot.is_dir():
            return {"exists": False}
        state: dict[str, object] = {
            "exists": True,
            "installed_sha256": None,
            "slot_sha256": step["desired_sha256"],
            "path": step["path"],
            "tree_sha256": install_backend._tree_sha256(slot),
        }
        if self.venv_link == {"exists": True, "is_symlink": True, "link_target": VENV_LINK}:
            state.update({"installed_sha256": step["desired_sha256"], "link_target": VENV_LINK})
        return state

    def _apply(self, step, creation_checkpoint=None) -> dict[str, object]:
        if step["kind"] == "venv":
            prior = self.inspect_step(step)
            self.log.append(["apply", step["step_id"]])
            slot = Path(step["path"])
            slot.mkdir()
            (slot / "bin").mkdir()
            (slot / "bin" / "python").write_text("#!/bin/sh\n")
            (slot / ".cortex-wheel.sha256").write_text(step["wheel_sha256"] + "\n")
            observed = slot.lstat()
            authority = {
                "state": "ready",
                "path": step["path"],
                "staging_path": str(slot.with_name(f".{slot.name}.cortex-staging")),
                "device": observed.st_dev,
                "inode": observed.st_ino,
                "tree_sha256": install_backend._tree_sha256(slot),
            }
            if creation_checkpoint is not None:
                creation_checkpoint(authority)
            self.venv_link = {"exists": True, "is_symlink": True, "link_target": VENV_LINK}
            return {"prior": prior, **self._venv_state(step)}
        prior = self.inspect_step(step)
        outcome = super()._apply(step, creation_checkpoint)
        if not prior.get("exists"):
            path = Path(str(step.get("path")))
            if self._created_directory(step):
                path.mkdir(mode=0o755)
            elif step["kind"] == "repository":
                (path / ".git" / "objects").mkdir(parents=True)
                (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
                (path / "README.md").write_text("candidate checkout\n")
        return outcome

    def rollback_step(self, entry) -> None:
        super().rollback_step(entry)
        step = entry["step"]
        if self._created_directory(step) and not entry["prior"].get("exists"):
            # The real backend's rollback of a directory this receipt created.
            self.log.append(["real-rollback", entry["step_id"]])
            install_backend.LocalInstallBackend(require_root=False).rollback_step(entry)

    def created_tree_identity(self, step) -> dict[str, object]:
        return install_backend._created_tree_identity(Path(step["path"]))

    def discard_created_tree(self, path: str, identity, *, quarantine_root: str, key: str) -> None:
        self.log.append(["discard", path])
        install_backend._discard_created_tree(
            Path(path), identity, quarantine_root=Path(quarantine_root), key=key
        )

    def rollback_credentials(self, receipt):
        return install_backend.LocalInstallBackend(require_root=False).rollback_credentials(
            receipt
        )


def _seed_repository_and_copilot(seeded, _fake, base) -> None:
    repository = Path(base["roots"]["state"]) / "repos" / "paulsha-cortex"
    (repository / ".git").mkdir(parents=True)
    (repository / ".git" / "HEAD").write_text("ref: refs/heads/legacy\n")
    (repository / "README.md").write_text("legacy checkout\n")
    copilot = seeded["reviewer_auth"].parent.parent / ".copilot"
    copilot.mkdir()
    (copilot / "config.json").write_bytes(b"legacy copilot login\n")
    (copilot / "config.json").chmod(0)


def _credentials_as_test_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """The receipt's accounts do not exist here; the test user owns the files."""

    original = install_core.credential_destination

    def as_test_user(receipt, *, principal, provider):
        path, _uid, _gid = original(receipt, principal=principal, provider=provider)
        return path, os.getuid(), os.getgid()

    monkeypatch.setattr(install_backend, "credential_destination", as_test_user)


def _home(case: LegacyCase, account: str) -> Path:
    return Path(next(row["home"] for row in case.plan["accounts"] if row["name"] == account))


def _import(case: LegacyCase, receipt: InstallReceipt, principal: str, provider: str) -> None:
    name = install_core.credential_source_basename(principal, provider)
    source = case.tmp_path / "import" / f"{principal}-{provider}" / name
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps({"copilotTokens": {"host": "token"}}) if provider == "copilot" else "token\n"
    )
    account = install_core._PRINCIPAL_ACCOUNTS[principal]
    destination_root = (
        install_core.credential_import_location(
            case.plan, principal=principal, provider=provider
        )[0]
        if provider in {"codex", "copilot"}
        and case.plan.get("launch_layout_version") == install_core.LAUNCH_AUTHORITY_LAYOUT_VERSION
        else _home(case, account)
    )
    if (
        provider in {"codex", "copilot"}
        and case.plan.get("launch_layout_version") == install_core.LAUNCH_AUTHORITY_LAYOUT_VERSION
    ):
        credential_path, _uid, _gid = install_core.credential_destination(
            InstallReceipt({"plan": case.plan}),
            principal=principal,
            provider=provider,
        )
        credential_path.parent.mkdir(parents=True, exist_ok=True)
    install_core.import_credential(
        receipt,
        principal=principal,
        provider=provider,
        source=source,
        destination_root=destination_root,
    )


class AdoptedHost:
    """An applied adoption with real clone, venv slot and imported credentials."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _credentials_as_test_user(monkeypatch)
        self.case = LegacyCase(
            tmp_path, host=_seed_repository_and_copilot, backend_class=FilesystemLegacyBackend
        )
        case = self.case
        self.legacy = case.legacy_identities()
        self.repository = Path(case.base["roots"]["state"]) / "repos" / "paulsha-cortex"
        self.slot = Path(case.step("candidate-venv")["path"])
        self.venvs = self.slot.parent
        self.reviewer = _home(case, "cortex-reviewer-planner")
        self.copilot = self.reviewer / ".copilot"
        self.canonical_copilot = (
            Path(case.plan["roots"]["state"])
            / "config/codex-credentials/reviewer/copilot"
        )
        self.agy = self.reviewer / "cache" / "gemini" / "antigravity-cli"
        self.receipt = new_install_receipt(case.plan)
        case.apply(self.receipt)
        _import(case, self.receipt, "reviewer-planner", "copilot")
        _import(case, self.receipt, "reviewer-planner", "agy")

    def rollback(self):
        return rollback_receipt(
            self.receipt, backend=self.case.backend, legacy_host=self.case.host
        )


def test_legacy_rollback_returns_the_repository_venv_and_credential_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = AdoptedHost(tmp_path, monkeypatch)
    case = host.case
    # Apply created real objects where the legacy ones were quarantined.
    assert (host.repository / "README.md").read_text() == "candidate checkout\n"
    assert (host.slot / "bin" / "python").is_file()
    assert (host.canonical_copilot / "config.json").is_file()
    assert (host.agy / "antigravity-oauth-token").is_file()
    rows = {row["provider"]: row for row in host.receipt.to_dict()["credentials"]}
    assert "created_directories" not in rows["copilot"]
    assert [item["path"] for item in rows["agy"]["created_directories"]] == [str(host.agy)]
    repository_entry = _entry(host.receipt.to_dict(), "repository:paulsha-cortex")
    assert repository_entry["legacy_creation"]["inode"] == host.repository.lstat().st_ino

    report = host.rollback()

    assert report.retained_drift == ()
    assert report.retained_unknown == ()
    assert report.legacy_restored is True
    assert case.snapshot() == host.legacy
    assert (host.repository / "README.md").read_text() == "legacy checkout\n"
    assert not os.path.lexists(host.venvs)
    assert host.copilot.lstat().st_ino == host.legacy[str(host.copilot)][1]
    assert host.agy.lstat().st_ino == host.legacy[str(host.agy)][1]
    assert case.recaptured_sha256() == case.block["inventory_sha256"]
    document = host.receipt.to_dict()
    assert document["state"] == "rolled-back"
    assert document["journal"] == []
    assert install_cli._receipt_restore_safe(document) is True


def test_legacy_rollback_keeps_a_clone_changed_after_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = AdoptedHost(tmp_path, monkeypatch)
    (host.repository / "notes.txt").write_text("written by someone else\n")

    report = host.rollback()

    drift = {row["step_id"] for row in report.retained_drift}
    assert "repository:paulsha-cortex" in drift
    assert f"legacy-quarantine:{host.repository}" in drift
    assert (host.repository / "notes.txt").read_text() == "written by someone else\n"
    assert (host.repository / "README.md").read_text() == "candidate checkout\n"
    assert not any(
        name.endswith(".cortex-discard") for name in os.listdir(host.repository.parent)
    )
    assert report.legacy_restored is False
    assert host.receipt.to_dict()["state"] == "rollback-blocked"
    assert install_cli._receipt_restore_safe(host.receipt.to_dict()) is False


def test_legacy_rollback_keeps_a_venv_slot_with_an_unknown_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = AdoptedHost(tmp_path, monkeypatch)
    (host.slot / "unknown.pth").write_text("import os\n")

    report = host.rollback()

    assert "candidate-venv" in {row["step_id"] for row in report.retained_drift}
    assert (host.slot / "unknown.pth").is_file()
    assert report.legacy_restored is False
    assert install_cli._receipt_restore_safe(host.receipt.to_dict()) is False


def test_legacy_rollback_keeps_a_credential_directory_with_other_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = AdoptedHost(tmp_path, monkeypatch)
    (host.canonical_copilot / "session-state.json").write_text("{}\n")

    report = host.rollback()

    assert any(
        row["step_id"] == "legacy-inventory"
        and str(host.canonical_copilot) in str(row["observed"])
        for row in report.retained_drift
    )
    assert (host.canonical_copilot / "session-state.json").is_file()
    assert not (host.canonical_copilot / "config.json").exists()
    assert report.legacy_restored is False
    assert install_cli._receipt_restore_safe(host.receipt.to_dict()) is False


def test_receipt_load_uses_installer_owned_copilot_authority_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = LegacyCase(tmp_path)
    receipt, path = _durable(monkeypatch, case)
    case.apply(receipt)
    _import(case, receipt, "reviewer-planner", "copilot")
    loaded = InstallReceipt.load(path, expected_plan=case.plan)
    row = loaded.to_dict()["credentials"][0]
    expected = (
        Path(case.plan["roots"]["state"])
        / "config/codex-credentials/reviewer/copilot/config.json"
    )
    assert install_core.credential_destination(
        loaded, principal="reviewer-planner", provider="copilot"
    )[0] == expected
    assert expected.is_file()
    assert "created_directories" not in row


def test_plain_receipt_credential_import_records_no_created_directories(
    tmp_path: Path,
) -> None:
    plan = _plain_plan(tmp_path / "plain")
    receipt = new_install_receipt(plan)
    receipt._document["state"] = "applied"
    home = tmp_path / "home" / "reviewer"
    home.mkdir(parents=True)
    source = tmp_path / "import" / "config.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"copilotTokens": {"host": "token"}}))

    install_core.import_credential(
        receipt,
        principal="reviewer-planner",
        provider="copilot",
        source=source,
        destination_root=install_core.credential_import_location(
            plan, principal="reviewer-planner", provider="copilot"
        )[0],
    )

    assert set(receipt.to_dict()["credentials"][0]) == {"principal", "provider", "mode", "sha256"}
    assert (
        Path(plan["roots"]["state"])
        / "config/codex-credentials/reviewer/copilot/config.json"
    ).is_file()


def _checkout(tmp_path: Path) -> Path:
    tree = tmp_path / "repos" / "checkout"
    (tree / ".git" / "objects").mkdir(parents=True)
    (tree / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tree / "README.md").write_text("created\n")
    (tree / "docs").mkdir()
    (tree / "docs" / "link").symlink_to("../README.md")
    return tree


def _discard(tmp_path: Path, tree: Path, identity) -> None:
    install_backend._discard_created_tree(
        tree, identity, quarantine_root=tmp_path / "quarantine", key=f"tree:{tree}"
    )


def _staged(tmp_path: Path) -> list[str]:
    """Everything left under the private discard staging, relative to it."""

    root = tmp_path / "quarantine" / ".discard"
    if not root.exists():
        return []
    return sorted(
        str(Path(directory, name).relative_to(root))
        for directory, directories, files in os.walk(root)
        for name in (*directories, *files)
    )


def _contents(tree: Path) -> dict[str, object]:
    """Every member of ``tree``: file bytes, symlink target or ``"dir"``."""

    rows: dict[str, object] = {}
    for directory, directories, files in os.walk(tree):
        for name in (*directories, *files):
            path = Path(directory, name)
            if path.is_symlink():
                rows[str(path.relative_to(tree))] = os.readlink(path)
            elif path.is_dir():
                rows[str(path.relative_to(tree))] = "dir"
            else:
                rows[str(path.relative_to(tree))] = path.read_bytes()
    return rows


_STAT_EXTRA_FIELDS = (
    "st_atime",
    "st_mtime",
    "st_ctime",
    "st_atime_ns",
    "st_mtime_ns",
    "st_ctime_ns",
    "st_blksize",
    "st_blocks",
    "st_rdev",
)


def _as_mounted_filesystem(monkeypatch: pytest.MonkeyPatch, top: Path) -> None:
    """Make ``top`` and all below it report another ``st_dev``, as a mount would.

    Mounting needs privileges the tests do not have; ``stat``/``fstat`` of the
    exact inodes below ``top`` answer as another filesystem mounted there.
    """

    base = top.lstat().st_dev
    inodes = {top.lstat().st_ino} | {
        Path(directory, name).lstat().st_ino
        for directory, directories, files in os.walk(top)
        for name in (*directories, *files)
    }
    real_stat, real_fstat = os.stat, os.fstat

    def mounted(result: os.stat_result) -> os.stat_result:
        if result.st_dev != base or result.st_ino not in inodes:
            return result
        values = list(result[:10])
        values[2] = base + 1  # st_dev
        return os.stat_result(values, {name: getattr(result, name) for name in _STAT_EXTRA_FIELDS})

    monkeypatch.setattr(os, "stat", lambda *args, **kwargs: mounted(real_stat(*args, **kwargs)))
    monkeypatch.setattr(os, "fstat", lambda descriptor: mounted(real_fstat(descriptor)))


def _as_bind_mount(monkeypatch: pytest.MonkeyPatch, top: Path) -> None:
    """Make descriptors of ``top`` report another mount id (same ``st_dev``)."""

    inode = top.lstat().st_ino
    real = install_backend._mount_id
    monkeypatch.setattr(
        install_backend,
        "_mount_id",
        lambda descriptor: real(descriptor) + (1 if os.fstat(descriptor).st_ino == inode else 0),
    )


def test_tree_digest_on_descriptors_equals_the_path_digest(tmp_path: Path) -> None:
    tree = _checkout(tmp_path)
    (tree / ".hidden").write_text("dot\n")
    parent = os.open(tree.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        digest, manifest = install_backend._tree_digest_at(parent, tree.name)
    finally:
        os.close(parent)

    assert digest == install_backend._tree_sha256(tree)
    assert manifest[""][:2] == (tree.lstat().st_dev, tree.lstat().st_ino)
    assert manifest["docs/link"][2] == "symlink"


def test_created_tree_discard_removes_the_verified_tree_in_private_staging(
    tmp_path: Path,
) -> None:
    tree = _checkout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not part of the tree\n")
    (tree / "docs" / "escape").symlink_to(outside)
    identity = install_backend._created_tree_identity(tree)

    _discard(tmp_path, tree, identity)

    assert not os.path.lexists(tree)
    assert os.listdir(tree.parent) == []
    # The removal never followed a symlink out of the staging directory.
    assert (outside / "keep.txt").read_text() == "not part of the tree\n"
    assert all(Path(item).name != "tree" for item in _staged(tmp_path))


def test_created_tree_discard_moves_a_changed_tree_back(tmp_path: Path) -> None:
    tree = _checkout(tmp_path)
    identity = install_backend._created_tree_identity(tree)
    (tree / "extra.txt").write_text("added later\n")

    with pytest.raises(InstallDriftError, match="changed"):
        _discard(tmp_path, tree, identity)

    assert (tree / "extra.txt").is_file()
    assert tree.lstat().st_ino == identity["inode"]
    assert all(Path(item).name != "tree" for item in _staged(tmp_path))


def test_created_tree_discard_refuses_a_replaced_tree(tmp_path: Path) -> None:
    tree = _checkout(tmp_path)
    identity = install_backend._created_tree_identity(tree)
    tree.rename(tmp_path / "repos" / "aside")
    tree.mkdir()
    (tree / "README.md").write_text("created\n")

    with pytest.raises(InstallDriftError, match="not the tree"):
        _discard(tmp_path, tree, identity)

    assert (tree / "README.md").is_file()
    assert _staged(tmp_path) == []


def test_created_tree_discard_never_deletes_a_file_added_after_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _checkout(tmp_path)
    identity = install_backend._created_tree_identity(tree)
    original = install_backend._tree_digest_at

    def verified_then_written(parent_fd: int, name: str):
        result = original(parent_fd, name)
        # A writer that still holds a descriptor into the tree adds a file
        # after the digest matched, before the removal walks the tree.
        staged = next(
            Path(directory)
            for directory, _dirs, _files in os.walk(tmp_path / "quarantine")
            if Path(directory).name == "tree"
        )
        (staged / "docs" / "late.txt").write_text("written after the check\n")
        return result

    monkeypatch.setattr(install_backend, "_tree_digest_at", verified_then_written)

    with pytest.raises(InstallDriftError, match="late.txt"):
        _discard(tmp_path, tree, identity)

    # Only objects of the verified manifest were removed; the late file stays.
    late = [item for item in _staged(tmp_path) if item.endswith("docs/late.txt")]
    assert len(late) == 1
    assert not os.path.lexists(tree)

    # The pending discard is explicit: a later attempt names it, removes nothing.
    tree.mkdir()
    with pytest.raises(InstallDriftError, match="pending"):
        _discard(tmp_path, tree, {**identity, "inode": tree.lstat().st_ino})
    assert len([item for item in _staged(tmp_path) if item.endswith("docs/late.txt")]) == 1


def test_created_tree_discard_refuses_another_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _checkout(tmp_path)
    identity = install_backend._created_tree_identity(tree)
    monkeypatch.setattr(
        install_backend, "_filesystem_device", lambda _fd: identity["device"] + 1
    )

    with pytest.raises(InstallDriftError, match="filesystem"):
        _discard(tmp_path, tree, identity)

    assert tree.lstat().st_ino == identity["inode"]
    assert (tree / "README.md").read_text() == "created\n"


@pytest.mark.parametrize("boundary", ["filesystem", "bind-mount"])
def test_created_tree_discard_moves_a_tree_with_a_mount_point_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    tree = _checkout(tmp_path)
    identity = install_backend._created_tree_identity(tree)
    before = _contents(tree)
    # Something is mounted on docs/ after the tree was recorded; what the
    # mount shows equals the recorded content, so the digest still matches.
    if boundary == "filesystem":
        _as_mounted_filesystem(monkeypatch, tree / "docs")
    else:
        _as_bind_mount(monkeypatch, tree / "docs")

    with pytest.raises(InstallDriftError, match="mount point") as caught:
        _discard(tmp_path, tree, identity)

    assert "moved back" in str(caught.value)
    # Nothing was deleted: the whole tree is back where it was.
    assert tree.lstat().st_ino == identity["inode"]
    assert _contents(tree) == before
    assert all(Path(item).name != "tree" for item in _staged(tmp_path))


@pytest.mark.parametrize("boundary", ["filesystem", "bind-mount"])
def test_verified_tree_removal_stops_at_a_mount_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    tree = _checkout(tmp_path)
    (tree / "zdata").mkdir()
    (tree / "zdata" / "payload.bin").write_bytes(b"data on the mounted filesystem\n")
    parent = os.open(tree.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _digest, manifest = install_backend._tree_digest_at(parent, tree.name)
        # A mount appears on a member after the manifest was proven.
        if boundary == "filesystem":
            _as_mounted_filesystem(monkeypatch, tree / "zdata")
        else:
            _as_bind_mount(monkeypatch, tree / "zdata")

        with pytest.raises(InstallDriftError, match="mount point"):
            install_backend._remove_verified_tree(parent, tree.name, manifest)
    finally:
        os.close(parent)

    assert (tree / "zdata" / "payload.bin").read_bytes() == b"data on the mounted filesystem\n"


def test_created_tree_discard_reports_a_tree_it_could_not_move_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _checkout(tmp_path)
    identity = install_backend._created_tree_identity(tree)
    (tree / "extra.txt").write_text("added later\n")
    original = install_backend._renameat2_noreplace
    calls: list[str] = []

    def reoccupied_before_the_move_back(source_fd, source, destination_fd, destination):
        calls.append(source)
        if len(calls) == 2:
            tree.mkdir()  # the original path is taken again
        original(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(install_backend, "_renameat2_noreplace", reoccupied_before_the_move_back)

    with pytest.raises(InstallDriftError, match="could not be moved back") as caught:
        _discard(tmp_path, tree, identity)

    assert ".discard" in str(caught.value)
    assert any(item.endswith("tree/extra.txt") for item in _staged(tmp_path))
    assert os.listdir(tree) == []


def test_credential_directory_is_removed_only_after_staging_proves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    directory = home / ".copilot"
    directory.mkdir(parents=True)
    observed = directory.lstat()
    rows = [{"path": str(directory), "dev": observed.st_dev, "ino": observed.st_ino}]
    destination = directory / "config.json"
    remove = install_backend._remove_created_credential_directories
    root = tmp_path / "quarantine"

    # A file appeared: the directory goes to staging, is found non-empty,
    # and comes straight back.
    (directory / "session.json").write_text("{}\n")
    problem = remove(rows, destination, quarantine_root=root, key="reviewer-planner/copilot")
    assert problem is not None and "not empty" in problem
    assert (directory / "session.json").is_file()
    (directory / "session.json").unlink()

    # Replaced between the check and the rename: the substitute is moved back.
    original = install_backend._renameat2_noreplace
    swapped: list[int] = []

    def swap_first(source_fd, source, destination_fd, destination):
        if not swapped:
            directory.rename(home / ".copilot.original")
            directory.mkdir()
            swapped.append(directory.lstat().st_ino)
        original(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(install_backend, "_renameat2_noreplace", swap_first)
    problem = remove(rows, destination, quarantine_root=root, key="reviewer-planner/copilot")
    assert problem is not None and "replaced" in problem
    assert directory.lstat().st_ino == swapped[0]
    assert (home / ".copilot.original").lstat().st_ino == observed.st_ino
    monkeypatch.setattr(install_backend, "_renameat2_noreplace", original)

    # The proven, empty directory is removed inside staging.
    directory.rmdir()
    (home / ".copilot.original").rename(directory)
    assert remove(rows, destination, quarantine_root=root, key="reviewer-planner/copilot") is None
    assert not os.path.lexists(directory)
    assert all(not item.endswith("/dir") for item in _staged(tmp_path))


def test_legacy_rollback_removes_the_venvs_directory_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = AdoptedHost(tmp_path, monkeypatch)
    case = host.case
    scaffold = f"scaffold:{host.venvs}"
    entry = _entry(host.receipt.to_dict(), scaffold)
    assert entry["prior"] == {"exists": False}
    assert host.venvs.is_dir()

    report = host.rollback()

    assert ["real-rollback", scaffold] in case.backend.log
    assert not os.path.lexists(host.venvs)
    assert report.legacy_restored is True
    assert case.recaptured_sha256() == case.block["inventory_sha256"]
    assert install_cli._receipt_restore_safe(host.receipt.to_dict()) is True


def test_legacy_rollback_keeps_a_clone_with_a_mount_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = AdoptedHost(tmp_path, monkeypatch)
    before = _contents(host.repository)
    inode = host.repository.lstat().st_ino
    # Another filesystem is mounted on .git/objects; it shows the same empty
    # directory, so the clone's tree digest alone would still match.
    _as_mounted_filesystem(monkeypatch, host.repository / ".git" / "objects")

    report = host.rollback()

    drift = {row["step_id"]: row["observed"] for row in report.retained_drift}
    problem = drift["repository:paulsha-cortex"]["error"]
    assert "mount point" in problem and "moved back" in problem
    assert f"legacy-quarantine:{host.repository}" in drift
    # No member was deleted; the clone is back at its path, the legacy
    # repository stays in quarantine.
    assert host.repository.lstat().st_ino == inode
    assert _contents(host.repository) == before
    assert report.legacy_restored is False
    assert host.receipt.to_dict()["state"] == "rollback-blocked"
    assert install_cli._receipt_restore_safe(host.receipt.to_dict()) is False


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_repository_tree_identity_is_stable_across_git_inspection(tmp_path: Path) -> None:
    env = {
        **install_backend._REPOSITORY_GIT_ENV,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", str(source)], check=True, env=env)
    (source / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "init"], check=True, env=env)
    clone = tmp_path / "repos" / "clone"
    clone.parent.mkdir()
    prefix = install_backend._REPOSITORY_GIT_PREFIX
    subprocess.run([*prefix, "clone", "-q", str(source), str(clone)], check=True, env=env)
    identity = install_backend._created_tree_identity(clone)

    for suffix in (
        ("rev-parse", "HEAD"),
        ("status", "--porcelain=v1", "--untracked-files=all"),
        ("fsck", "--strict", "--no-dangling"),
    ):
        subprocess.run(
            [*prefix, "-c", f"safe.directory={clone}", "-C", str(clone), *suffix],
            check=True,
            env=env,
            capture_output=True,
        )

    assert install_backend._created_tree_identity(clone) == identity
    _discard(tmp_path, clone, identity)
    assert not os.path.lexists(clone)
