"""Legacy adoption PR-2: the read-only legacy inventory collector (#1122).

Everything here runs rootless against a temporary tree.  Root-only surfaces
(passwd/group, shadow, systemd, the privilege drop of the writable census) go
through an injectable backend; only the real ``access(2)`` census test needs
root and is skipped otherwise.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import legacy
from paulsha_cortex.trust_root.install.core import InstallError, InstallPlanError


REPO_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_SHA = "c" * 40


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_bundle(tmp_path: Path) -> Path:
    """Write a complete, hash-consistent qualification bundle manifest."""

    root = tmp_path / "bundle"
    if (root / "bundle.json").exists():
        return root / "bundle.json"
    root.mkdir()
    wheel = root / "paulsha_cortex-0.1.12-py3-none-any.whl"
    wheel.write_bytes(b"legacy inventory candidate wheel\n")
    generated = root / "generated-inventory.json"
    generated.write_bytes(b"{}\n")
    repository = root / "paulsha-cortex.bundle"
    repository.write_bytes(b"legacy inventory source bundle\n")
    tools = []
    for name, version in (("agy", "1.2.11"), ("codex", "0.157.0"), ("copilot", "1.0.88")):
        tool = root / f"{name}-{version}.bin"
        tool.write_bytes(f"{name} {version}\n".encode())
        tools.append(
            {
                "name": name,
                "version": version,
                "shape": "file",
                "path": tool.name,
                "sha256": _sha256(tool),
            }
        )
    manifest = {
        "schema_version": 1,
        "candidate_sha": CANDIDATE_SHA,
        "wheel": {"path": wheel.name, "sha256": _sha256(wheel)},
        "wheelhouse": [{"path": wheel.name, "sha256": _sha256(wheel)}],
        "generated_artifacts": [
            {"path": generated.name, "sha256": _sha256(generated)}
        ],
        "toolchain": tools,
        "source_repositories": [
            {
                "slug": "paulsha-cortex",
                "commit": CANDIDATE_SHA,
                "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
                "path": repository.name,
                "sha256": _sha256(repository),
            }
        ],
    }
    path = root / "bundle.json"
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return path


def _release_config(tmp_path: Path, bundle: Path) -> Path:
    output = tmp_path / "release-install-config.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "qualification" / "write_install_config.py"),
            "--bundle",
            str(bundle),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def _normalized_plan_sha256(plan: dict, tmp_path: Path) -> str:
    """Hash a plan with the per-run temporary prefix factored out.

    ``receipt_path`` is dropped because it is itself a digest of the rest of the
    plan, so it would carry the temporary prefix in hashed form.
    """

    prefix = str(tmp_path)

    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(child) for key, child in value.items()}
        if isinstance(value, list):
            return [normalize(child) for child in value]
        if isinstance(value, str):
            return value.replace(prefix, "<TMP>")
        return value

    document = normalize(plan)
    document.pop("receipt_path")
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# Golden digest of the release install config planned by the installer before
# legacy adoption existed (base: #1123 receipt bounds).  A legacy-free plan
# must stay byte-identical, so this constant must never be regenerated to make
# a legacy change pass.  Regenerated for non-legacy changes only: #716 added
# `version: "1"` to the root-owned manager gh config (manager-gh-config asset),
# and #716 again added `StateDirectory=` for the gate worktree slot to the gate
# job template units (the slot must exist before namespace setup), and #716
# once more added the toolchain-first `PATH` to the manager EnvironmentFile
# (the ship lane calls `openspec` by name), and #716 again added
# `DO_NOT_TRACK=1` there (openspec telemetry crashes under `--jitless`).
RELEASE_PLAN_GOLDEN_SHA256 = (
    "56b820032411a4823761e8ae43e4ff916329201ef49e1e5c7c7aeaec540932d6"
)


def test_release_config_plan_is_byte_identical_without_legacy_inputs(
    tmp_path: Path,
) -> None:
    bundle = _write_bundle(tmp_path)
    config = _release_config(tmp_path, bundle)
    output = tmp_path / "plan.json"

    assert install_cli.main(
        ["plan", "--config", str(config), "--bundle", str(bundle), "--output", str(output)]
    ) == 0

    plan = json.loads(output.read_text(encoding="utf-8"))
    assert "legacy_adoption" not in plan
    assert plan["legacy_policy"] == "quarantine"
    assert _normalized_plan_sha256(plan, tmp_path) == RELEASE_PLAN_GOLDEN_SHA256


# ---------------------------------------------------------------------------
# a Phase 2b-shaped legacy host under a temporary root
# ---------------------------------------------------------------------------

#: The legacy host kept the ids its accounts were created with; the release
#: config's 991-995 belong to unrelated system identities there.
LEGACY_IDS = {
    "cortex-manager": (999, 987),
    "cortex-reviewer-planner": (997, 986),
    "cortex-builder": (995, 985),
    "cortex-gate": (994, 984),
    "cortex-egress": (993, 983),
}
RELEASE_IDS = {
    "cortex-manager": (991, 991),
    "cortex-reviewer-planner": (992, 992),
    "cortex-builder": (993, 993),
    "cortex-gate": (994, 994),
    "cortex-egress": (995, 995),
}
MACHINE_ID = "0123456789abcdef0123456789abcdef"
SECRET = b"legacy-secret-bytes-must-never-be-read\n"
CAPTURED_AT = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _host_config(tmp_path: Path, bundle: Path, *, state: str = "var/lib/cortex") -> dict:
    host = tmp_path / "host"
    manifest = json.loads(bundle.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "scheme": "four-way",
        "instance": "cortex",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": CANDIDATE_SHA,
        },
        "operator_account": "root",
        "external_reader_account": "<absent>",
        "accounts": {
            name: {
                "uid": uid,
                "gid": gid,
                "home": str(host / "var/lib" / name),
                "shell": "/usr/sbin/nologin",
            }
            for name, (uid, gid) in RELEASE_IDS.items()
            if name != "cortex-egress"
        },
        "service_accounts": {
            "cortex-egress": {
                "uid": 995,
                "gid": 995,
                "home": str(host / "var/lib/cortex-egress"),
                "shell": "/usr/sbin/nologin",
            }
        },
        "roots": {
            "deploy": str(host / "opt/cortex"),
            "state": str(host / state),
            "systemd": str(host / "etc/systemd/system"),
            "polkit": str(host / "etc/polkit-1/rules.d"),
        },
        "source_repositories": ["paulsha-cortex"],
        "legacy_policy": "quarantine",
        "providers": {
            "builder": ["codex"],
            "reviewer-planner": ["agy", "copilot"],
            "manager": ["github"],
        },
        "toolchain": {
            row["name"]: {
                "version": row["version"],
                "sha256": row["sha256"],
                "shape": row["shape"],
            }
            for row in manifest["toolchain"]
        },
    }


def _legacy_overlay(tmp_path: Path) -> dict:
    return {
        "accounts": {
            name: {"uid": uid, "gid": gid}
            for name, (uid, gid) in LEGACY_IDS.items()
            if name != "cortex-egress"
        },
        "service_accounts": {
            "cortex-egress": {
                "uid": 993,
                "gid": 983,
                "home": str(tmp_path / "host/srv/cortex-egress"),
            }
        },
        "operator_account": "legacy-operator",
    }


def _plan_for(tmp_path: Path, overlay: dict | None, **config_options) -> dict:
    bundle = _write_bundle(tmp_path)
    config = _host_config(tmp_path, bundle, **config_options)
    return install_cli._bound_plan_from_config(
        legacy.apply_host_overlay(config, overlay), bundle
    )


def _seed_legacy_host(tmp_path: Path, plan: dict) -> dict[str, Path]:
    """Lay out the objects a hand-deployed Phase 2b host carries."""

    host = tmp_path / "host"
    deploy = Path(plan["roots"]["deploy"])
    state = Path(plan["roots"]["state"])
    systemd = Path(plan["roots"]["systemd"])
    polkit = Path(plan["roots"]["polkit"])
    builder = host / "var/lib/cortex-builder"
    reviewer = host / "var/lib/cortex-reviewer-planner"
    manager = host / "var/lib/cortex-manager"
    gate = host / "var/lib/cortex-gate"
    for directory in (
        deploy / "etc",
        deploy / "toolchain/bin",
        deploy / "toolchain/lib/codex",
        deploy / "venv/bin",
        deploy / "venv.bak-20260821",
        deploy / "operator-backups",
        state / "coordinator/review-sandboxes",
        state / "coordinator/commit-spool",
        state / "legacy-imported",
        state / "specs-phase2-a",
        state / "worktree",
        systemd / "cortex-reviewer-job@.service.d",
        systemd / "multi-user.target.wants",
        polkit,
        builder / ".codex",
        builder / ".copilot",
        builder / "cache",
        reviewer / ".codex",
        reviewer / "cache/gemini/antigravity-cli",
        manager / ".config/gh",
        gate,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "venv_cortex": deploy / "venv/bin/cortex",
        "env": deploy / "etc/cortex-manager.env",
        "env_backup": deploy / "etc/cortex-manager.env.bak-pre-drift",
        "oauth": deploy / "etc/copilot-oauth-config.json",
        "agy_settings": deploy / "etc/agy-reviewer-settings.json",
        "claude": deploy / "toolchain/bin/claude",
        "codex": deploy / "toolchain/bin/codex",
        "copilot": deploy / "toolchain/bin/copilot",
        "unit": systemd / "cortex-manager.service",
        "dropin_dir": systemd / "cortex-reviewer-job@.service.d",
        "dropin": systemd / "cortex-reviewer-job@.service.d/agy-review-settings.conf",
        "wants": systemd / "multi-user.target.wants/cortex-manager.service",
        "foreign_wants": systemd / "multi-user.target.wants/ssh.service",
        "foreign_unit": systemd / "unrelated.service",
        "polkit": polkit / "49-cortex-downgrade.rules",
        "foreign_polkit": polkit / "50-other.rules",
        "jobs": state / "coordinator/jobs.json",
        "sandboxes": state / "coordinator/review-sandboxes",
        "residue_tmp": state / "coordinator/tmpabc123.tmp",
        "residue_bak": state / "coordinator/jobs.json.rollback.bak",
        "commit_spool": state / "coordinator/commit-spool",
        "legacy_imported": state / "legacy-imported",
        "legacy_imported_child": state / "legacy-imported/old.json",
        "specs": state / "specs-phase2-a",
        "stray": state / "stray.txt",
        "worktree_slot": state / "worktree/job-1",
        "builder_auth": builder / ".codex/auth.json",
        "builder_copilot": builder / ".copilot",
        "builder_copilot_config": builder / ".copilot/config.json",
        "builder_cache": builder / "cache",
        "reviewer_auth": reviewer / ".codex/auth.json",
        "reviewer_token": reviewer / "cache/gemini/antigravity-cli/antigravity-oauth-token",
        "manager_hosts": manager / ".config/gh/hosts.yml",
    }
    paths["venv_cortex"].write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    paths["agy_settings"].write_text("{}\n", encoding="utf-8")
    with paths["claude"].open("wb") as stream:
        stream.truncate(legacy.CONTENT_HASH_MAX_BYTES + 1)
    paths["codex"].symlink_to("../lib/codex/codex")
    paths["copilot"].write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    paths["unit"].write_text("[Service]\nUser=cortex-manager\n", encoding="utf-8")
    paths["dropin"].write_text("[Service]\nBindReadOnlyPaths=/nowhere\n", encoding="utf-8")
    paths["wants"].symlink_to(paths["unit"])
    paths["foreign_wants"].symlink_to("/lib/systemd/system/ssh.service")
    paths["foreign_unit"].write_text("[Service]\n", encoding="utf-8")
    paths["polkit"].write_text("// legacy\n", encoding="utf-8")
    paths["foreign_polkit"].write_text("// other\n", encoding="utf-8")
    paths["jobs"].write_text('{"jobs": []}\n', encoding="utf-8")
    paths["residue_tmp"].write_text("partial\n", encoding="utf-8")
    paths["residue_bak"].write_text("old\n", encoding="utf-8")
    paths["legacy_imported_child"].write_text("{}\n", encoding="utf-8")
    paths["stray"].write_text("stray\n", encoding="utf-8")
    paths["worktree_slot"].mkdir()
    (paths["sandboxes"] / "job-9").mkdir()
    (paths["sandboxes"] / "job-9/out.txt").write_text("out\n", encoding="utf-8")
    (paths["sandboxes"] / "job-9/link").symlink_to("/etc")
    secrets = [
        paths[name]
        for name in (
            "env",
            "env_backup",
            "oauth",
            "builder_auth",
            "builder_copilot_config",
            "reviewer_auth",
            "reviewer_token",
            "manager_hosts",
        )
    ]
    for secret in secrets:
        secret.write_bytes(SECRET)
        # Any real read of a credential now fails, not only the fake's.
        secret.chmod(0)
    paths["secrets"] = secrets  # type: ignore[assignment]
    return paths


def _default_services(plan: dict, seeded: dict[str, Path]) -> dict[str, dict[str, object]]:
    systemd = Path(plan["roots"]["systemd"])
    return {
        unit: {
            "load_state": "loaded",
            "unit_file_state": "enabled",
            "fragment_path": str(systemd / unit),
            "drop_in_paths": [],
            "user": user,
            "exec_path": str(seeded["venv_cortex"]),
            "active_state": "active",
            "sub_state": "running",
        }
        for unit, user in (
            ("cortex-egress-proxy.service", "cortex-egress"),
            ("cortex-manager.service", "cortex-manager"),
            ("cortex-monitor.service", "cortex-manager"),
        )
    }


class FakeLegacyHost(legacy.LocalLegacyHostBackend):
    """Real read-only file access over the temporary tree; fake identities."""

    def __init__(self, plan: dict, seeded: dict[str, Path]) -> None:
        super().__init__(require_root=False)
        homes = {
            row["name"]: row["home"]
            for row in (*plan["accounts"], *plan["service_accounts"])
        }
        self.users = [
            legacy.PasswdEntry(name, uid, gid, homes[name], "/usr/sbin/nologin")
            for name, (uid, gid) in LEGACY_IDS.items()
        ] + [
            legacy.PasswdEntry("systemd-resolve", 991, 991, "/run/systemd", "/usr/sbin/nologin"),
            legacy.PasswdEntry("legacy-operator", 4242, 4242, "/home/legacy-operator", "/bin/bash"),
            legacy.PasswdEntry("fixture-owner", os.getuid(), os.getgid(), "/", "/bin/sh"),
        ]
        self.groups = [
            legacy.GroupEntry(name, gid, ()) for name, (_uid, gid) in LEGACY_IDS.items()
        ] + [
            legacy.GroupEntry("systemd-resolve", 991, ()),
            legacy.GroupEntry("render", 992, ()),
            legacy.GroupEntry("kvm", 993, ("legacy-operator",)),
            legacy.GroupEntry("sgx", 994, ()),
            legacy.GroupEntry("input", 995, ()),
            legacy.GroupEntry("legacy-operator", 4242, ()),
            legacy.GroupEntry("fixture-owner", os.getgid(), ()),
        ]
        self.forbidden_reads = {str(path) for path in seeded["secrets"]}  # type: ignore[union-attr]
        self.forbidden_reads.add(str(seeded["claude"]))
        self.acls: dict[str, list[dict[str, object]]] = {}
        self.services = _default_services(plan, seeded)
        self.in_flight_value: dict[str, object] = {"job_processes": 0, "durable_jobs": 0}
        self.writable: dict[int, set[str]] = {}
        self.unstable: dict[int, set[str]] = {}
        self.read_calls: list[str] = []
        self.census_calls: list[tuple[legacy.CensusIdentity, list[str]]] = []
        self.machine = MACHINE_ID
        self.host_name = "legacy-host"
        self.sudoers: dict[str, object] = {"accounts": [], "unproven": None}
        self.sudoers_calls: list[list[str]] = []

    def machine_id(self) -> str:
        return self.machine

    def sudoers_verdict(self, plan):
        self.sudoers_calls.append(
            [row["name"] for key in ("accounts", "service_accounts") for row in plan[key]]
        )
        return dict(self.sudoers)

    def hostname(self) -> str:
        return self.host_name

    def passwd_entries(self):
        return tuple(self.users)

    def group_entries(self):
        return tuple(self.groups)

    def group_list(self, name: str, gid: int):
        return sorted({gid, *(row.gid for row in self.groups if name in row.members)})

    def password_locked(self, name: str):
        return True

    def read_acl(self, path: str):
        return [dict(row) for row in self.acls.get(str(path), [])]

    def sha256_file(self, path: str, observed: os.stat_result) -> str:
        self.read_calls.append(str(path))
        if str(path) in self.forbidden_reads:
            raise AssertionError(f"inventory read content it must not read: {path}")
        return super().sha256_file(path, observed)

    def service_status(self, unit: str):
        return dict(self.services[unit])

    def in_flight(self, job_uids, plan):
        return dict(self.in_flight_value)

    def writable_paths(self, identity: legacy.CensusIdentity, tree: legacy.CensusTree):
        paths = list(tree.paths())
        self.census_calls.append((identity, paths))
        for path in paths:
            assert not os.path.islink(path), f"census was handed a symlink: {path}"
        roots = self.writable.get(identity.uid, set())
        unstable = self.unstable.get(identity.uid, set())
        return legacy.CensusResult(
            writable=tuple(
                path
                for path in paths
                if any(path == root or path.startswith(root + "/") for root in roots)
            ),
            unstable=tuple(path for path in paths if path in unstable),
        )


def _legacy_setup(tmp_path: Path, overlay: dict | None = None):
    overlay = _legacy_overlay(tmp_path) if overlay is None else overlay
    plan = _plan_for(tmp_path, overlay)
    seeded = _seed_legacy_host(tmp_path, plan)
    return plan, overlay, FakeLegacyHost(plan, seeded), seeded


def _collect(plan: dict, overlay: dict | None, backend, *, at: datetime = CAPTURED_AT) -> dict:
    return legacy.collect_legacy_inventory(
        plan=plan, backend=backend, host_overlay=overlay, captured_at=at
    )


def _row(document: dict, section: str, path: Path) -> dict:
    matches = [row for row in document[section] if row["path"] == str(path)]
    assert len(matches) == 1, f"{path} not exactly once in {section}"
    return matches[0]


def _redigest(document: dict) -> dict:
    changed = deepcopy(document)
    changed["inventory_sha256"] = legacy.inventory_stable_sha256(changed)
    return changed


# ---------------------------------------------------------------------------
# host overlay
# ---------------------------------------------------------------------------


def test_host_overlay_merges_only_allowlisted_fields(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    config = _host_config(tmp_path, bundle)
    overlay = {
        **_legacy_overlay(tmp_path),
        "external_reader_account": "legacy-reader",
        "providers": {"builder": ["agy"]},
        "legacy_adoption": {"inventory_sha256": "0" * 64},
    }

    effective = legacy.apply_host_overlay(config, overlay)

    assert effective["accounts"]["cortex-builder"] == {
        "uid": 995,
        "gid": 985,
        "home": config["accounts"]["cortex-builder"]["home"],
        "shell": "/usr/sbin/nologin",
    }
    assert effective["service_accounts"]["cortex-egress"] == {
        "uid": 993,
        "gid": 983,
        "home": str(tmp_path / "host/srv/cortex-egress"),
        "shell": "/usr/sbin/nologin",
    }
    assert effective["operator_account"] == "legacy-operator"
    assert effective["external_reader_account"] == "legacy-reader"
    assert effective["providers"]["builder"] == ["agy"]
    assert effective["providers"]["reviewer-planner"] == ["agy", "copilot"]
    assert "legacy_adoption" not in effective
    assert effective["roots"] == config["roots"]
    # The release config itself is never modified.
    assert config["accounts"]["cortex-builder"]["uid"] == 993


@pytest.mark.parametrize(
    ("overlay", "named"),
    [
        ({"roots": {"state": "/srv/cortex"}}, "roots.state"),
        ({"accounts": {"cortex-builder": {"home": "/srv/builder"}}}, "accounts.cortex-builder.home"),
        ({"accounts": {"cortex-builder": {"shell": "/bin/sh"}}}, "accounts.cortex-builder.shell"),
        (
            {"service_accounts": {"cortex-egress": {"shell": "/bin/sh"}}},
            "service_accounts.cortex-egress.shell",
        ),
        ({"service_accounts": {"cortex-other": {"uid": 7}}}, "service_accounts.cortex-other.uid"),
        ({"providers": {"reviewer-planner": ["codex"]}}, "providers.reviewer-planner"),
        ({"legacy_policy": "reject"}, "legacy_policy"),
        ({"toolchain": {"codex": {"version": "9"}}}, "toolchain.codex.version"),
        ({"repo_identity": {"commit": "d" * 40}}, "repo_identity.commit"),
    ],
)
def test_host_overlay_rejects_and_names_keys_outside_the_allowlist(
    overlay: dict, named: str
) -> None:
    with pytest.raises(InstallPlanError, match=re.escape(named)):
        legacy.validate_host_overlay(overlay)


@pytest.mark.parametrize(
    "overlay",
    [
        {"accounts": {"cortex-builder": {"uid": True}}},
        {"accounts": {"cortex-builder": {"uid": 0}}},
        {"service_accounts": {"cortex-egress": {"home": "relative/home"}}},
        {"providers": {"builder": ["copilot"]}},
        {"providers": {"builder": []}},
        {"operator_account": ""},
        {"legacy_adoption": ["not", "an", "object"]},
        {"legacy_adoption": {"github_token": "x"}},
    ],
)
def test_host_overlay_rejects_invalid_allowlisted_values(overlay: dict) -> None:
    with pytest.raises(InstallPlanError):
        legacy.validate_host_overlay(overlay)


def test_host_overlay_account_must_exist_in_the_release_config(tmp_path: Path) -> None:
    config = _host_config(tmp_path, _write_bundle(tmp_path))
    with pytest.raises(InstallPlanError, match="cortex-stranger"):
        legacy.apply_host_overlay(config, {"accounts": {"cortex-stranger": {"uid": 7}}})


def test_host_overlay_record_ignores_the_legacy_adoption_block(tmp_path: Path) -> None:
    base = _legacy_overlay(tmp_path)
    record = legacy.host_overlay_record(base)

    assert record is not None
    assert "accounts.cortex-builder.uid" in record["keys"]
    assert "service_accounts.cortex-egress.home" in record["keys"]
    # The adoption block binds this inventory's digest, so it cannot feed it.
    assert legacy.host_overlay_record(
        {**base, "legacy_adoption": {"inventory_sha256": "a" * 64}}
    ) == record
    assert legacy.host_overlay_record({"legacy_adoption": {}}) is None
    assert legacy.host_overlay_record(None) is None
    changed = deepcopy(base)
    changed["accounts"]["cortex-builder"]["uid"] = 1995
    assert legacy.host_overlay_record(changed)["sha256"] != record["sha256"]


# ---------------------------------------------------------------------------
# schema, digest and host binding
# ---------------------------------------------------------------------------


def test_inventory_is_a_self_digested_canonical_schema_v1_record(tmp_path: Path) -> None:
    plan, overlay, backend, _seeded = _legacy_setup(tmp_path)

    document = _collect(plan, overlay, backend)
    inventory = legacy.LegacyInventory.from_document(document)

    assert document["schema_version"] == 1
    assert document["kind"] == legacy.INVENTORY_KIND
    assert inventory.inventory_sha256 == legacy.inventory_stable_sha256(document)
    assert inventory.scope_sha256 == legacy.scope_sha256(document["scope"])
    assert document["scope"] == legacy.legacy_scope(plan)
    assert document["host_overlay"] == legacy.host_overlay_record(overlay)
    assert document["collector"]["candidate_sha"] == CANDIDATE_SHA
    assert document["volatile"]["captured_at"] == "2026-09-28T12:00:00Z"

    output = tmp_path / "out" / "legacy-inventory.json"
    output.parent.mkdir()
    legacy.publish_inventory(output, document)
    raw = output.read_bytes()
    assert raw == legacy.canonical_inventory_bytes(document)
    assert raw.endswith(b"\n") and raw.isascii()
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert legacy.LegacyInventory.load(output).document == inventory.document

    tampered = deepcopy(document)
    tampered["managed_paths"][0]["step_id"] = "asset:forged"
    with pytest.raises(InstallPlanError, match="inventory_sha256 does not match"):
        legacy.LegacyInventory.from_document(tampered)
    unknown = deepcopy(document)
    unknown["extra"] = True
    with pytest.raises(InstallPlanError, match="top-level keys"):
        legacy.LegacyInventory.from_document(unknown)
    bad_scope = _redigest({**document, "scope_sha256": "0" * 64})
    with pytest.raises(InstallPlanError, match="scope_sha256"):
        legacy.LegacyInventory.from_document(bad_scope)
    pretty = tmp_path / "out" / "pretty.json"
    pretty.write_text(json.dumps(document, indent=2), encoding="ascii")
    with pytest.raises(InstallPlanError, match="canonical"):
        legacy.LegacyInventory.load(pretty)


def test_inventory_digest_ignores_volatile_fields(tmp_path: Path) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)
    baseline = _collect(plan, overlay, backend)

    for row in backend.services.values():
        row["active_state"] = "inactive"
        row["sub_state"] = "dead"
    backend.in_flight_value = {"job_processes": 3, "durable_jobs": 2}
    backend.host_name = "renamed-host"
    backend.writable = {
        LEGACY_IDS["cortex-builder"][0]: {str(seeded["builder_cache"])}
    }
    # New job data inside an adopted directory moves only counts.
    (seeded["jobs"].parent / "evidence-001.json").write_text("{}\n", encoding="utf-8")
    later = _collect(plan, overlay, backend, at=datetime(2026, 9, 29, tzinfo=timezone.utc))

    assert later["volatile"] != baseline["volatile"]
    assert later["volatile"]["services"]["cortex-manager.service"]["active_state"] == "inactive"
    assert later["inventory_sha256"] == baseline["inventory_sha256"]


def test_inventory_digest_changes_with_every_stable_field(tmp_path: Path) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)
    digests = [_collect(plan, overlay, backend)["inventory_sha256"]]
    coordinator = seeded["jobs"].parent

    def changes(mutate) -> None:
        mutate()
        digest = _collect(plan, overlay, backend)["inventory_sha256"]
        assert digest not in digests
        digests.append(digest)

    changes(lambda: coordinator.chmod(0o750))
    changes(
        lambda: backend.acls.__setitem__(
            str(coordinator),
            [{"account": "legacy-operator", "perms": "rx", "default": False}],
        )
    )
    changes(lambda: (seeded["dropin_dir"] / "extra.conf").write_text("[Service]\n"))
    changes(lambda: (coordinator / "tmpxyz.tmp").write_text("partial\n"))
    changes(lambda: seeded["unit"].write_text("[Service]\nUser=cortex-builder\n"))
    changes(lambda: backend.services["cortex-manager.service"].update(unit_file_state="disabled"))
    changes(lambda: backend.users.append(legacy.PasswdEntry("intruder", 995, 1, "/", "/bin/sh")))
    changes(lambda: backend.writable.update({LEGACY_IDS["cortex-gate"][0]: {str(seeded["stray"])}}))


def test_host_binding_is_a_domain_separated_hash_never_the_machine_id(
    tmp_path: Path,
) -> None:
    plan, overlay, backend, _seeded = _legacy_setup(tmp_path)

    binding = legacy.host_binding_sha256(MACHINE_ID)
    document = _collect(plan, overlay, backend)

    assert document["host"] == {"binding_sha256": binding}
    assert binding != hashlib.sha256(MACHINE_ID.encode()).hexdigest()
    assert MACHINE_ID not in legacy.canonical_inventory_bytes(document).decode()
    assert legacy.host_binding_sha256("f" * 32) != binding
    with pytest.raises(InstallError, match="machine-id"):
        legacy.host_binding_sha256("not-a-machine-id")
    backend.machine = "f" * 32
    assert _collect(plan, overlay, backend)["inventory_sha256"] != document["inventory_sha256"]


def test_scope_binds_roots_homes_and_managed_paths_but_not_account_ids(
    tmp_path: Path,
) -> None:
    overlay = _legacy_overlay(tmp_path)
    scope = legacy.legacy_scope(_plan_for(tmp_path, overlay))

    renumbered = deepcopy(overlay)
    renumbered["accounts"]["cortex-gate"] = {"uid": 1994, "gid": 1984}
    assert legacy.legacy_scope(_plan_for(tmp_path, renumbered)) == scope

    moved_home = deepcopy(overlay)
    moved_home["service_accounts"]["cortex-egress"]["home"] = str(tmp_path / "host/srv/egress")
    assert legacy.legacy_scope(_plan_for(tmp_path, moved_home)) != scope

    builder_agy = {**overlay, "providers": {"builder": ["agy"]}}
    assert legacy.legacy_scope(_plan_for(tmp_path, builder_agy)) != scope

    other_state = legacy.legacy_scope(_plan_for(tmp_path, overlay, state="var/lib/cortex-alt"))
    assert other_state != scope
    assert other_state["roots"]["state"].endswith("var/lib/cortex-alt")

    managed = {row["path"] for row in scope["managed_paths"]}
    assert str(tmp_path / "host/opt/cortex/venv") in managed
    assert str(tmp_path / "host/var/lib/cortex/coordinator") in managed
    assert scope["census"]["principals"] == [
        "cortex-builder",
        "cortex-egress",
        "cortex-gate",
        "cortex-reviewer-planner",
    ]


def test_account_rows_record_identity_group_lock_and_id_holders(tmp_path: Path) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)
    rows = {row["name"]: row for row in _collect(plan, overlay, backend)["accounts"]}

    builder = rows["cortex-builder"]
    assert builder["desired"]["uid"] == 995 and builder["desired"]["gid"] == 985
    assert builder["passwd"] == {
        "uid": 995,
        "gid": 985,
        "home": builder["desired"]["home"],
        "shell": "/usr/sbin/nologin",
    }
    assert builder["group"] == {"name": "cortex-builder", "gid": 985, "members": []}
    assert builder["supplementary_groups"] == []
    assert builder["password_locked"] is True
    assert builder["uid_holders"] == {"995": ["cortex-builder"]}
    assert rows["cortex-egress"]["kind"] == "service"

    # Planned with the release ids instead, the collisions are on record.
    release_plan = _plan_for(tmp_path, None)
    release = {
        row["name"]: row
        for row in _collect(release_plan, None, FakeLegacyHost(release_plan, seeded))["accounts"]
    }
    assert release["cortex-builder"]["uid_holders"]["993"] == ["cortex-egress"]
    assert release["cortex-builder"]["gid_holders"]["993"]["groups"] == ["kvm"]
    assert release["cortex-manager"]["uid_holders"]["991"] == ["systemd-resolve"]


# ---------------------------------------------------------------------------
# metadata-only classes
# ---------------------------------------------------------------------------


def test_credential_objects_are_recorded_by_metadata_only(tmp_path: Path) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)

    document = _collect(plan, overlay, backend)
    encoded = legacy.canonical_inventory_bytes(document)

    assert not set(backend.read_calls) & backend.forbidden_reads
    assert SECRET not in encoded
    assert hashlib.sha256(SECRET).hexdigest().encode() not in encoded
    credential_lstat_keys = {"type", "uid", "gid", "mode", "dev", "ino"}
    credentials = {
        (row["principal"], row["provider"]): row for row in document["credentials"]
    }
    builder_codex = credentials[("builder", "codex")]
    assert builder_codex["path"] == str(seeded["builder_auth"])
    assert builder_codex["configured"] is True
    assert set(builder_codex["lstat"]) == credential_lstat_keys
    assert builder_codex["lstat"]["ino"] == seeded["builder_auth"].lstat().st_ino
    assert credentials[("reviewer-planner", "agy")]["lstat"]["type"] == "file"
    assert credentials[("manager", "github")]["lstat"]["mode"] == "0000"
    assert credentials[("reviewer-planner", "copilot")]["lstat"] is None
    assert credentials[("reviewer-planner", "codex")]["configured"] is False

    env = _row(document, "managed_paths", seeded["env"])
    assert env["class"] == "credential"
    assert env["content"] == {"omitted": "credential"}
    assert env["acl"] is None
    assert set(env["lstat"]) == credential_lstat_keys
    for name in ("env_backup", "oauth", "builder_copilot"):
        row = _row(document, "discovered", seeded[name])
        assert row["class"] == "credential"
        assert row["content"] == {"omitted": "credential"}
        assert set(row["lstat"]) == credential_lstat_keys

    # The schema itself refuses a credential row that carries content.
    forged = deepcopy(document)
    _row(forged, "managed_paths", seeded["env"])["content"] = {"sha256": "0" * 64}
    with pytest.raises(InstallPlanError, match="credential class"):
        legacy.LegacyInventory.from_document(_redigest(forged))


def test_schema_rederives_the_credential_class_from_the_path(tmp_path: Path) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)
    document = _collect(plan, overlay, backend)

    forged = deepcopy(document)
    row = _row(forged, "discovered", seeded["oauth"])
    row["class"] = "other"
    row["content"] = {"sha256": "0" * 64}
    row["lstat"].update(nlink=1, size=len(SECRET))
    with pytest.raises(InstallPlanError, match="credential classification"):
        legacy.LegacyInventory.from_document(_redigest(forged))


def test_capture_re_observes_objects_that_change_or_vanish_mid_read(
    tmp_path: Path,
) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)
    hash_file = backend.sha256_file
    raced: list[str] = []

    def racing_sha256(path: str, observed: os.stat_result) -> str:
        if path == str(seeded["stray"]) and not raced:
            raced.append(path)
            seeded["stray"].write_text("rewritten while it was captured\n", encoding="utf-8")
            raise legacy.InventoryRaceError("changed under the reader")
        if path == str(seeded["residue_tmp"]):
            seeded["residue_tmp"].unlink()
            raise FileNotFoundError(2, "renamed away by the running Manager", path)
        return hash_file(path, observed)

    backend.sha256_file = racing_sha256  # type: ignore[method-assign]
    document = _collect(plan, overlay, backend)

    stray = _row(document, "discovered", seeded["stray"])
    assert raced
    assert stray["content"] == {"sha256": _sha256(seeded["stray"])}
    assert stray["lstat"]["size"] == seeded["stray"].stat().st_size
    assert str(seeded["residue_tmp"]) not in {row["path"] for row in document["discovered"]}


def test_local_backend_refuses_to_hash_a_file_changed_since_its_lstat(
    tmp_path: Path,
) -> None:
    backend = legacy.LocalLegacyHostBackend(require_root=False)
    grown = tmp_path / "grown.txt"
    grown.write_text("one\n", encoding="utf-8")
    observed = os.lstat(grown)
    grown.write_text("a longer body\n", encoding="utf-8")
    with pytest.raises(legacy.InventoryRaceError):
        backend.sha256_file(str(grown), observed)

    replaced = tmp_path / "replaced.txt"
    replaced.write_text("x\n", encoding="utf-8")
    observed = os.lstat(replaced)
    other = tmp_path / "other.txt"
    other.write_text("x\n", encoding="utf-8")
    os.replace(other, replaced)
    with pytest.raises(legacy.InventoryRaceError):
        backend.sha256_file(str(replaced), observed)

    link = tmp_path / "link.txt"
    link.symlink_to(grown)
    with pytest.raises(OSError):
        backend.sha256_file(str(link), os.lstat(grown))


def test_large_files_and_symlinks_are_recorded_without_reading_content(
    tmp_path: Path,
) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)

    document = _collect(plan, overlay, backend)

    claude = _row(document, "discovered", seeded["claude"])
    assert claude["content"] == {"omitted": "oversize"}
    assert claude["lstat"]["size"] == legacy.CONTENT_HASH_MAX_BYTES + 1
    assert str(seeded["claude"]) not in backend.read_calls
    codex = _row(document, "managed_paths", seeded["codex"])
    assert codex["lstat"]["type"] == "symlink"
    assert codex["content"] == {"target": "../lib/codex/codex"}
    copilot = _row(document, "managed_paths", seeded["copilot"])
    assert copilot["content"] == {"sha256": _sha256(seeded["copilot"])}
    venv = _row(document, "managed_paths", seeded["venv_cortex"].parent.parent)
    assert venv["kind"] == "venv-link"
    assert venv["lstat"]["type"] == "directory"


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_discovery_follows_verify_authority_rules_and_lists_unmanaged_state(
    tmp_path: Path,
) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)
    document = _collect(plan, overlay, backend)
    discovered = {row["path"]: row for row in document["discovered"]}
    managed = {row["path"] for row in document["managed_paths"]}

    def rules(name: str) -> list[str]:
        return discovered[str(seeded[name])]["rules"]

    # Generated managed objects are inventoried as managed, not discovered.
    for name in ("unit", "polkit", "env", "codex", "copilot"):
        assert str(seeded[name]) in managed
        assert str(seeded[name]) not in discovered
    assert "authority:units" in rules("dropin_dir")
    assert rules("dropin") == ["systemd-dropin"]
    assert rules("wants") == ["systemd-wants"]
    assert discovered[str(seeded["dropin"])]["class"] == "authority"
    assert discovered[str(seeded["dropin"])]["content"] == {
        "sha256": _sha256(seeded["dropin"])
    }
    for name in ("foreign_wants", "foreign_unit", "foreign_polkit", "jobs", "legacy_imported_child"):
        assert str(seeded[name]) not in discovered
    assert rules("env_backup") == ["authority:environment", "env-dir", "managed-residue"]
    assert rules("oauth") == ["env-dir"]
    assert discovered[str(seeded["agy_settings"])]["class"] == "other"
    assert rules("claude") == ["authority:toolchain_wrappers", "toolchain-bin"]
    assert discovered[str(seeded["claude"])]["class"] == "authority"
    assert "deploy-top" in discovered[str(tmp_path / "host/opt/cortex/venv.bak-20260821")]["rules"]
    assert "home-top" in rules("builder_copilot")
    assert rules("legacy_imported") == ["managed-subdir", "state-top"]
    assert rules("specs") == ["managed-subdir", "state-top"]
    assert rules("stray") == ["state-top"]
    assert rules("sandboxes") == ["managed-subdir"]
    assert rules("worktree_slot") == ["managed-subdir"]
    assert rules("residue_tmp") == ["managed-residue"]
    assert rules("residue_bak") == ["managed-residue"]
    assert discovered[str(seeded["stray"])]["content"] == {"sha256": _sha256(seeded["stray"])}


# ---------------------------------------------------------------------------
# writable census
# ---------------------------------------------------------------------------


def test_census_reports_writable_paths_outside_declared_assets(tmp_path: Path) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)
    builder_uid = LEGACY_IDS["cortex-builder"][0]
    reviewer_uid = LEGACY_IDS["cortex-reviewer-planner"][0]
    backend.writable = {
        # Declared: a per-job worktree slot, the commit spool, its own cache.
        builder_uid: {
            str(seeded["worktree_slot"]),
            str(seeded["commit_spool"]),
            str(seeded["builder_cache"]),
        },
        # Undeclared: the legacy review sandboxes and everything below them.
        reviewer_uid: {str(seeded["sandboxes"])},
    }

    document = _collect(plan, overlay, backend)
    census = document["census"]

    assert census["cortex-builder"]["writable_outside_declared"] == []
    assert census["cortex-reviewer-planner"] == {
        "status": "checked",
        "identity": {"uid": reviewer_uid, "gid": 986, "groups": [986]},
        "writable_outside_declared": [
            {"path": str(seeded["sandboxes"]), "type": "directory"}
        ],
        "unstable": [],
    }
    assert census["cortex-gate"]["writable_outside_declared"] == []
    counts = document["volatile"]["census"]["cortex-reviewer-planner"]
    assert counts["outside_declared"] == 3  # the directory, job-9 and out.txt
    assert counts["writable"] == 3
    # Every principal is checked with its own kernel identity, never a symlink.
    assert sorted(identity.uid for identity, _paths in backend.census_calls) == sorted(
        uid for name, (uid, _gid) in LEGACY_IDS.items() if name != "cortex-manager"
    )
    checked = backend.census_calls[0][1]
    assert str(seeded["sandboxes"] / "job-9/link") not in checked
    assert str(seeded["sandboxes"] / "job-9/out.txt") in checked
    assert str(seeded["builder_auth"]) in checked


def test_census_records_an_absent_principal(tmp_path: Path) -> None:
    plan, overlay, backend, _seeded = _legacy_setup(tmp_path)
    backend.users = [row for row in backend.users if row.name != "cortex-gate"]

    document = _collect(plan, overlay, backend)

    assert document["census"]["cortex-gate"] == {
        "status": "absent",
        "identity": None,
        "writable_outside_declared": [],
        "unstable": [],
    }
    assert {row["name"]: row for row in document["accounts"]}["cortex-gate"]["passwd"] is None


def test_declared_writable_patterns_come_from_plan_writers_and_desired_modes(
    tmp_path: Path,
) -> None:
    plan = _plan_for(tmp_path, _legacy_overlay(tmp_path))
    state = tmp_path / "host/var/lib/cortex"

    builder = legacy.declared_writable_patterns(plan, "cortex-builder")
    reviewer = legacy.declared_writable_patterns(plan, "cortex-reviewer-planner")

    assert str(state / "worktree/<job-id>") in builder
    assert str(state / "coordinator/commit-spool") in builder
    assert str(tmp_path / "host/var/lib/cortex-builder/cache") in builder
    assert str(state / "coordinator/review-verdicts") in reviewer
    assert str(state / "coordinator/review-sandboxes") not in reviewer


def _self_identity() -> legacy.CensusIdentity:
    return legacy.CensusIdentity(os.getuid(), os.getgid(), tuple(sorted(os.getgroups())))


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses DAC write checks")
def test_writable_census_asks_the_kernel_as_the_calling_identity(tmp_path: Path) -> None:
    backend = legacy.LocalLegacyHostBackend(require_root=False)
    root = tmp_path / "census"
    root.mkdir()
    writable_file = root / "writable.txt"
    readonly_file = root / "readonly.txt"
    readonly_dir = root / "readonly-dir"
    writable_dir = root / "writable-dir"
    closed = root / "closed"
    for path in (writable_file, readonly_file):
        path.write_text("x\n", encoding="utf-8")
    for path in (readonly_dir, writable_dir, closed):
        path.mkdir()
    (closed / "inner.txt").write_text("x\n", encoding="utf-8")
    (root / "link").symlink_to(writable_file)
    tree = legacy.build_census_tree(backend, [str(root)])
    readonly_file.chmod(0o444)
    readonly_dir.chmod(0o555)
    closed.chmod(0o600)  # no search permission: nothing below is reachable
    try:
        result = backend.writable_paths(_self_identity(), tree)
    finally:
        readonly_dir.chmod(0o755)
        closed.chmod(0o755)

    assert sorted(result.writable) == sorted(
        [str(root), str(writable_file), str(writable_dir), str(closed)]
    )
    assert result.unstable == ()
    assert str(root / "link") not in list(tree.paths())
    with pytest.raises(PermissionError, match="needs root"):
        backend.writable_paths(
            legacy.CensusIdentity(os.getuid() + 1, os.getgid(), ()), tree
        )


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses DAC write checks")
def test_census_marks_entries_swapped_for_symlinks_as_unstable(tmp_path: Path) -> None:
    backend = legacy.LocalLegacyHostBackend(require_root=False)
    root = tmp_path / "census"
    (root / "sub").mkdir(parents=True)
    victim = root / "victim.txt"
    victim.write_text("x\n", encoding="utf-8")
    (root / "sub/inner.txt").write_text("x\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "inner.txt").write_text("x\n", encoding="utf-8")
    readonly = tmp_path / "readonly.txt"
    readonly.write_text("x\n", encoding="utf-8")
    readonly.chmod(0o444)
    tree = legacy.build_census_tree(backend, [str(root)])
    # After enumeration and before the census, a job swaps both entries.
    victim.unlink()
    victim.symlink_to(readonly)
    (root / "sub/inner.txt").unlink()
    (root / "sub").rmdir()
    (root / "sub").symlink_to(elsewhere)

    result = backend.writable_paths(_self_identity(), tree)

    assert sorted(result.unstable) == sorted([str(victim), str(root / "sub")])
    assert str(victim) not in result.writable
    assert str(root / "sub") not in result.writable
    # The swapped directory is never entered through its new symlink.
    assert str(root / "sub/inner.txt") not in (*result.writable, *result.unstable)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses DAC write checks")
def test_census_catches_a_swap_between_its_stat_and_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = legacy.LocalLegacyHostBackend(require_root=False)
    root = tmp_path / "census"
    root.mkdir()
    victim = root / "victim.txt"
    victim.write_text("x\n", encoding="utf-8")
    moved = root / "moved.txt"
    tree = legacy.build_census_tree(backend, [str(root)])
    access = legacy._faccessat2

    def swapping_access(dir_fd: int, name: str, mode: int) -> None:
        if name == victim.name and not moved.exists():
            victim.rename(moved)
            victim.symlink_to(moved)
        access(dir_fd, name, mode)

    monkeypatch.setattr(legacy, "_faccessat2", swapping_access)
    result = backend.writable_paths(_self_identity(), tree)

    assert str(victim) in result.unstable
    assert str(victim) not in result.writable


@pytest.mark.parametrize("error", [errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP])
def test_census_fails_closed_without_nofollow_faccessat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    backend = legacy.LocalLegacyHostBackend(require_root=False)
    root = tmp_path / "census"
    root.mkdir()
    (root / "file.txt").write_text("x\n", encoding="utf-8")
    tree = legacy.build_census_tree(backend, [str(root)])

    def unsupported(dir_fd: int, name: str, mode: int) -> None:
        raise OSError(error, os.strerror(error), name)

    monkeypatch.setattr(legacy, "_faccessat2", unsupported)
    with pytest.raises(InstallError, match="AT_SYMLINK_NOFOLLOW"):
        backend.writable_paths(_self_identity(), tree)


def test_census_records_unstable_entries_outside_declared_areas(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)
    reviewer_uid = LEGACY_IDS["cortex-reviewer-planner"][0]
    builder_uid = LEGACY_IDS["cortex-builder"][0]
    backend.unstable = {
        reviewer_uid: {str(seeded["stray"])},
        # Inside the builder's own declared worktree slot: already writable.
        builder_uid: {str(seeded["worktree_slot"])},
    }

    document = _collect(plan, overlay, backend)
    census = document["census"]

    assert census["cortex-reviewer-planner"]["status"] == "unstable"
    assert census["cortex-reviewer-planner"]["unstable"] == [str(seeded["stray"])]
    assert census["cortex-builder"]["status"] == "checked"
    assert census["cortex-builder"]["unstable"] == []
    backend.unstable = {}
    assert _collect(plan, overlay, backend)["inventory_sha256"] != document["inventory_sha256"]

    path = tmp_path / "inventory.json"
    legacy.publish_inventory(path, document)
    assert install_cli.main(["legacy", "show", "--inventory", str(path)]) == 0
    out = capsys.readouterr().out
    assert "UNSTABLE" in out and str(seeded["stray"]) in out


def test_legacy_inventory_cli_flags_an_unstable_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _plan, backend, seeded, argv, output, _constructed = _cli_setup(tmp_path, monkeypatch)
    backend.unstable = {LEGACY_IDS["cortex-gate"][0]: {str(seeded["stray"])}}

    assert install_cli.main(argv) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["census_stable"] is False
    assert "census is unstable" in captured.err
    census = legacy.LegacyInventory.load(output).document["census"]
    assert census["cortex-gate"]["status"] == "unstable"


@pytest.mark.skipif(os.geteuid() != 0, reason="dropping to another account needs root")
def test_writable_census_drops_to_the_job_identity_as_root() -> None:
    base = Path(tempfile.mkdtemp(prefix="legacy-census-"))
    try:
        base.chmod(0o755)
        private = base / "root-0644.txt"
        shared = base / "world-0666.txt"
        closed = base / "root-dir-0755"
        open_dir = base / "world-dir-0777"
        private.write_text("x\n", encoding="utf-8")
        shared.write_text("x\n", encoding="utf-8")
        private.chmod(0o644)
        shared.chmod(0o666)
        closed.mkdir(mode=0o755)
        open_dir.mkdir()
        open_dir.chmod(0o777)
        nobody = legacy.CensusIdentity(65534, 65534, (65534,))

        backend = legacy.LocalLegacyHostBackend()
        result = backend.writable_paths(nobody, legacy.build_census_tree(backend, [str(base)]))

        assert sorted(result.writable) == sorted([str(shared), str(open_dir)])
        assert result.unstable == ()
        assert os.geteuid() == 0 and os.getegid() == 0
    finally:
        for path in sorted(base.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        base.rmdir()


def test_state_summary_counts_the_adoption_gates_outside_the_digest(
    tmp_path: Path,
) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)
    state = Path(plan["roots"]["state"])
    seeded["stray"].chmod(0o666)
    sticky = state / "shared-tmp"
    sticky.mkdir()
    sticky.chmod(0o1777)
    seeded["specs"].chmod(0o2755)

    document = _collect(plan, overlay, backend)
    summary = document["volatile"]["state_summary"]

    assert summary["world_writable"] == 1  # the sticky directory does not count
    assert summary["setid"] == (1 if seeded["specs"].stat().st_mode & stat.S_ISGID else 0)
    assert summary["external_symlinks"] == 1  # review-sandboxes/job-9/link -> /etc
    assert summary["nouser"] == 0 and summary["nogroup"] == 0
    assert summary["owner_census"] == {"fixture-owner:fixture-owner": summary["entries"]}
    assert summary["entries"] == len(list(state.rglob("*"))) + 1

    backend.users = [row for row in backend.users if row.name != "fixture-owner"]
    orphaned = _collect(plan, overlay, backend)
    assert orphaned["volatile"]["state_summary"]["nouser"] == summary["entries"]


def test_walk_never_follows_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub/file").write_text("x\n", encoding="utf-8")
    (root / "escape").symlink_to(outside)

    walked = {
        path: stat.S_ISLNK(observed.st_mode)
        for path, observed in legacy.LocalLegacyHostBackend(require_root=False).walk(str(root))
    }

    assert walked == {
        str(root): False,
        str(root / "sub"): False,
        str(root / "sub/file"): False,
        str(root / "escape"): True,
    }


def test_collection_is_read_only(tmp_path: Path) -> None:
    plan, overlay, backend, _seeded = _legacy_setup(tmp_path)
    host = tmp_path / "host"

    def snapshot() -> dict[str, tuple]:
        rows = {}
        for path in sorted(host.rglob("*")):
            observed = path.lstat()
            rows[str(path)] = (
                observed.st_mode,
                observed.st_uid,
                observed.st_gid,
                observed.st_size,
                observed.st_mtime_ns,
                observed.st_ino,
            )
        return rows

    before = snapshot()
    _collect(plan, overlay, backend)
    assert snapshot() == before


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlay: dict | None = None):
    plan, overlay, backend, seeded = _legacy_setup(tmp_path, overlay)
    bundle = _write_bundle(tmp_path)
    config = tmp_path / "install-config.json"
    config.write_text(json.dumps(_host_config(tmp_path, bundle)), encoding="utf-8")
    overlay_path = tmp_path / "host-overlay.yaml"
    overlay_path.write_text(json.dumps(overlay), encoding="utf-8")
    output_dir = tmp_path / "inventories"
    output_dir.mkdir()
    constructed: list[FakeLegacyHost] = []

    def factory():
        constructed.append(backend)
        return backend

    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "LocalLegacyHostBackend", factory)
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "host-locks")
    monkeypatch.setattr(
        install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "maintenance-state"
    )
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _observed, _path: None)
    argv = [
        "legacy",
        "inventory",
        "--config",
        str(config),
        "--host-overlay",
        str(overlay_path),
        "--bundle",
        str(bundle),
        "--output",
        str(output_dir / "inventory.json"),
    ]
    return plan, backend, seeded, argv, output_dir / "inventory.json", constructed


def test_legacy_inventory_cli_previews_the_plan_and_the_sudoers_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1282: the root capture is where S2 review starts; it reports what a
    # plan would refuse and the sudoers preflight non-root review cannot read.
    plan, backend, _seeded, argv, output, _constructed = _cli_setup(tmp_path, monkeypatch)

    assert install_cli.main(argv) == 0

    captured = capsys.readouterr()
    emitted = json.loads(captured.out)
    assert emitted["plan_preview"]["ready"] is True
    assert emitted["plan_preview"]["failures"] == []
    assert emitted["plan_preview"]["quarantine"] > 0
    assert emitted["cortex_account_universal_nopasswd"] == {"accounts": [], "unproven": None}
    assert backend.sudoers_calls == [
        [row["name"] for key in ("accounts", "service_accounts") for row in plan[key]]
    ]
    assert "would refuse" not in captured.err
    assert "NOPASSWD" not in captured.err
    assert output.is_file()


def test_legacy_inventory_cli_names_plan_refusals_and_sudoers_findings_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan, backend, _seeded, argv, output, _constructed = _cli_setup(tmp_path, monkeypatch)
    (Path(plan["roots"]["deploy"]) / "mystery.txt").write_text("?\n", encoding="utf-8")
    backend.sudoers = {"accounts": ["cortex-builder"], "unproven": None}

    # The capture itself still succeeds: it is read-only and complete.
    assert install_cli.main(argv) == 0

    captured = capsys.readouterr()
    emitted = json.loads(captured.out)
    assert output.is_file()
    assert emitted["plan_preview"]["ready"] is False
    assert any("mystery.txt" in failure for failure in emitted["plan_preview"]["failures"])
    assert emitted["cortex_account_universal_nopasswd"]["accounts"] == ["cortex-builder"]
    assert "would refuse" in captured.err and "mystery.txt" in captured.err
    assert "cortex-builder" in captured.err and "NOPASSWD" in captured.err

    backend.sudoers = {"accounts": [], "unproven": "visudo not found in /usr/sbin"}
    output.unlink()
    assert install_cli.main(argv) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["cortex_account_universal_nopasswd"]["unproven"]
    assert "visudo not found" in captured.err


def test_local_backend_sudoers_verdict_covers_every_plan_declared_cortex_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _overlay, _backend, _seeded = _legacy_setup(tmp_path)
    seen: dict[str, object] = {}

    def verdict(accounts, *, passwd_records, group_records):
        seen["accounts"] = [row["name"] for row in accounts]
        seen["records"] = (len(passwd_records) > 0, len(group_records) > 0)
        return {"accounts": [], "unproven": None}

    monkeypatch.setattr(legacy, "_cortex_account_universal_nopasswd", verdict)

    host = legacy.LocalLegacyHostBackend(require_root=False)
    assert host.sudoers_verdict(plan) == {"accounts": [], "unproven": None}
    assert seen["accounts"] == [
        row["name"] for key in ("accounts", "service_accounts") for row in plan[key]
    ]
    assert seen["records"] == (True, True)


def test_legacy_inventory_cli_requires_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _plan, _backend, _seeded, argv, output, constructed = _cli_setup(tmp_path, monkeypatch)
    monkeypatch.undo()
    monkeypatch.setattr(install_cli.os, "geteuid", lambda: 4242)
    monkeypatch.setattr(install_cli, "LocalLegacyHostBackend", lambda: constructed.append(1))

    assert install_cli.main(argv) == 1

    assert "requires root" in capsys.readouterr().err
    assert not output.exists()
    assert constructed == []


def test_legacy_inventory_cli_writes_once_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _plan, backend, _seeded, argv, output, _constructed = _cli_setup(tmp_path, monkeypatch)

    assert install_cli.main(argv) == 0
    emitted = json.loads(capsys.readouterr().out)
    inventory = legacy.LegacyInventory.load(output)
    assert emitted["census_stable"] is True
    assert emitted["output"] == str(output)
    assert emitted["inventory_sha256"] == inventory.inventory_sha256
    assert emitted["scope_sha256"] == inventory.scope_sha256
    assert emitted["host_binding_sha256"] == inventory.host_binding_sha256
    assert inventory.document["host_overlay"] is not None
    assert stat.S_IMODE(output.stat().st_mode) == 0o644

    before = output.read_bytes()
    calls = len(backend.census_calls)
    assert install_cli.main(argv) == 1
    assert "exists" in capsys.readouterr().err
    assert output.read_bytes() == before
    assert len(backend.census_calls) == calls, "an existing output must fail before collection"


@pytest.mark.parametrize("marker", ["receipt-parent", "maintenance-snapshot", "lease-marker"])
def test_legacy_inventory_cli_refuses_a_receipt_managed_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    marker: str,
) -> None:
    plan, backend, _seeded, argv, output, _constructed = _cli_setup(tmp_path, monkeypatch)
    if marker == "receipt-parent":
        Path(install_core.canonical_receipt_path(plan)).parent.mkdir(parents=True)
    elif marker == "maintenance-snapshot":
        snapshot = tmp_path / "maintenance-state" / "maintenance-snapshot.json"
        snapshot.parent.mkdir()
        snapshot.write_text("{}\n", encoding="ascii")
    else:
        lock = tmp_path / "host-locks" / "maintenance.lock"
        lock.parent.mkdir()
        lock.write_text('{"plan_sha256":"0","token_sha256":"0"}\n', encoding="ascii")
        lock.chmod(0o600)

    assert install_cli.main(argv) == 1

    assert "--prior-receipt" in capsys.readouterr().err
    assert not output.exists()
    assert backend.census_calls == [] and backend.read_calls == []


def test_legacy_inventory_cli_refuses_during_an_active_maintenance_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan, backend, _seeded, argv, output, _constructed = _cli_setup(tmp_path, monkeypatch)

    with install_cli._maintenance_lease(plan):
        assert install_cli.main(argv) == 1

    assert "--prior-receipt" in capsys.readouterr().err
    assert not output.exists()
    assert backend.census_calls == []


def test_legacy_inventory_cli_refuses_while_a_transaction_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _plan, backend, _seeded, argv, output, _constructed = _cli_setup(tmp_path, monkeypatch)

    with install_cli._host_lock(leaf="transaction.lock", conflict="held by test"):
        assert install_cli.main(argv) == 1

    assert "transaction" in capsys.readouterr().err
    assert not output.exists()
    assert backend.census_calls == []


def test_legacy_inventory_cli_rejects_a_disallowed_overlay_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _write_bundle(tmp_path)
    config = tmp_path / "install-config.json"
    config.write_text(json.dumps(_host_config(tmp_path, bundle)), encoding="utf-8")
    overlay_path = tmp_path / "bad-overlay.yaml"
    overlay_path.write_text(
        json.dumps({**_legacy_overlay(tmp_path), "roots": {"state": "/srv/cortex"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(
        install_cli, "LocalLegacyHostBackend", lambda: pytest.fail("backend constructed")
    )

    assert install_cli.main(
        [
            "legacy",
            "inventory",
            "--config",
            str(config),
            "--host-overlay",
            str(overlay_path),
            "--bundle",
            str(bundle),
            "--output",
            str(tmp_path / "inventory.json"),
        ]
    ) == 1

    assert "roots.state" in capsys.readouterr().err
    assert not (tmp_path / "inventory.json").exists()


def test_legacy_show_summarizes_an_inventory_without_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)
    backend.writable = {LEGACY_IDS["cortex-reviewer-planner"][0]: {str(seeded["sandboxes"])}}
    document = _collect(plan, overlay, backend)
    path = tmp_path / "inventory.json"
    legacy.publish_inventory(path, document)
    monkeypatch.setattr(install_cli.os, "geteuid", lambda: 4242)

    assert install_cli.main(["legacy", "show", "--inventory", str(path)]) == 0

    out = capsys.readouterr().out
    assert document["inventory_sha256"] in out
    assert "cortex-builder (principal)" in out
    assert str(seeded["sandboxes"]) in out
    assert str(seeded["dropin"]) in out
    assert "[credential]" in out
    assert SECRET.decode().strip() not in out


def test_legacy_show_rejects_a_tampered_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan, overlay, backend, _seeded = _legacy_setup(tmp_path)
    path = tmp_path / "inventory.json"
    legacy.publish_inventory(path, _collect(plan, overlay, backend))
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b'"mode":"0755"', b'"mode":"0777"', 1))
    assert path.read_bytes() != raw

    assert install_cli.main(["legacy", "show", "--inventory", str(path)]) == 1
    assert "legacy inventory is invalid" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the output never lands inside what the inventory records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "host/var/lib/cortex/coordinator/inventory.json",
        "host/var/lib/cortex/inventory.json",
        "host/var/lib/cortex/coordinator/quota-observations/inventory.json",
        "host/opt/cortex/etc/inventory.json",
        "host/etc/systemd/system/inventory.json",
        "host/etc/polkit-1/rules.d/inventory.json",
        "host/var/lib/cortex-builder/inventory.json",
        "host/srv/cortex-egress/inventory.json",
    ],
)
def test_legacy_inventory_cli_refuses_an_output_inside_the_inventoried_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    relative: str,
) -> None:
    _plan, backend, _seeded, argv, _output, _constructed = _cli_setup(tmp_path, monkeypatch)
    target = tmp_path / relative
    argv[-1] = str(target)

    assert install_cli.main(argv) == 1

    err = capsys.readouterr().err
    assert "inside" in err and "/var/lib/cortex-installer/legacy/" in err
    assert not target.exists()
    assert backend.census_calls == [] and backend.read_calls == []


@pytest.mark.parametrize("into_scope", [True, False])
def test_legacy_inventory_cli_refuses_an_output_through_a_symlinked_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    into_scope: bool,
) -> None:
    _plan, backend, seeded, argv, output, _constructed = _cli_setup(tmp_path, monkeypatch)
    real_parent = seeded["jobs"].parent if into_scope else output.parent
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent)
    argv[-1] = str(alias / "inventory.json")

    assert install_cli.main(argv) == 1

    assert "symlink" in capsys.readouterr().err
    assert not (real_parent / "inventory.json").exists()
    assert backend.census_calls == []


def test_publish_refuses_to_write_inside_the_inventoried_scope(tmp_path: Path) -> None:
    plan, overlay, backend, seeded = _legacy_setup(tmp_path)
    document = _collect(plan, overlay, backend)
    target = seeded["jobs"].parent / "inventory.json"

    with pytest.raises(InstallPlanError, match="cortex-installer/legacy"):
        legacy.publish_inventory(target, document)
    assert not target.exists()
