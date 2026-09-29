"""RC `legacy-adoption` profile: the Phase 2b-shaped fixture (#1122, PR-5).

The container run cannot happen in unit tests, so the fixture is proven
offline instead: the manifest is shape-checked, laid out rootless under a
temporary root, captured with the real inventory collector (identities and
the writable census come from the manifest), and planned with the real
legacy adoption derivation.  The plan must bind without a single failure and
quarantine exactly the objects the manifest declares.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import legacy


REPO_ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION = REPO_ROOT / "qualification"
MANIFEST = QUALIFICATION / "legacy_fixture.json"
FIXTURE_MODULE = QUALIFICATION / "legacy_fixture.py"
CANDIDATE_SHA = "c" * 40
MACHINE_ID = "fedcba9876543210fedcba9876543210"
RELEASE_IDS = {991, 992, 993, 994, 995}
CORTEX_ACCOUNTS = {
    "cortex-manager",
    "cortex-reviewer-planner",
    "cortex-builder",
    "cortex-gate",
    "cortex-egress",
}


def _load_fixture_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_qualification_legacy_fixture", FIXTURE_MODULE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fixture_module():
    return _load_fixture_module()


@pytest.fixture(scope="module")
def manifest(fixture_module):
    return fixture_module.load_manifest()


# ---------------------------------------------------------------------------
# manifest shape
# ---------------------------------------------------------------------------


def test_manifest_is_valid_and_self_describing(fixture_module, manifest) -> None:
    fixture_module.validate_manifest(manifest)
    assert manifest["schema_version"] == 1
    assert manifest["kind"] == "paulsha-cortex/legacy-adoption-fixture"
    assert fixture_module.manifest_sha256() == hashlib.sha256(MANIFEST.read_bytes()).hexdigest()


def test_manifest_accounts_keep_phase2b_ids_off_the_release_defaults(manifest) -> None:
    accounts = {row["name"]: row for row in manifest["accounts"]}
    assert set(accounts) == CORTEX_ACCOUNTS
    ids = [row["uid"] for row in manifest["accounts"] + manifest["extra_accounts"]]
    gids = [row["gid"] for row in manifest["accounts"] + manifest["extra_accounts"]]
    assert len(set(ids)) == len(ids) and len(set(gids)) == len(gids)
    for row in manifest["accounts"]:
        # Phase 2b shape: ids that are not the release config's, and a
        # primary gid that differs from the uid.
        assert row["uid"] not in RELEASE_IDS and row["gid"] not in RELEASE_IDS
        assert row["uid"] != row["gid"]
    # The legacy egress home is a path that does not exist on the host.
    assert accounts["cortex-egress"]["home"] == "/srv/cortex-egress"
    assert not any(
        entry["path"] == "/srv/cortex-egress" for entry in manifest["entries"]
    )


def test_manifest_covers_every_phase2b_shape(manifest) -> None:
    entries = {entry["path"]: entry for entry in manifest["entries"]}
    # Real venv directory, unversioned toolchain and an oversize binary.
    assert entries["/opt/cortex/venv"]["type"] == "directory"
    assert entries["/opt/cortex/toolchain/bin/claude"]["type"] == "sparse"
    assert entries["/opt/cortex/toolchain/bin/codex"]["type"] == "symlink"
    # Hand-written units plus a drop-in the installer never generates.
    assert "/etc/systemd/system/cortex-manager.service" in entries
    assert "/etc/systemd/system/cortex-reviewer-job@.service.d/agy-review-settings.conf" in entries
    assert "/etc/polkit-1/rules.d/49-cortex-downgrade.rules" in entries
    # ACL-bearing state, undeclared top-level items, unmanaged subdirs, residue.
    assert entries["/var/lib/cortex"]["acl"]
    assert entries["/var/lib/cortex/run/cortex"]["mask"] == "---"
    assert entries["/var/lib/cortex/coordinator/engineering-outcomes"]["default_acl"]
    assert "/var/lib/cortex/legacy-imported" in entries
    assert "/var/lib/cortex/coordinator/review-sandboxes" in entries
    assert "/var/lib/cortex/coordinator/tmpq1w2e3r4.tmp" in entries
    # Worktree pools and a non-canonical source checkout with branch/worktree.
    assert "/var/lib/cortex/worktree" in entries
    assert "/var/lib/cortex/gate-worktree" in entries
    (repository,) = manifest["repositories"]
    assert repository["path"] == "/var/lib/cortex/repos/paulsha-cortex"
    assert repository["branches"] and repository["worktrees"]
    assert repository["origin"].startswith("https://example.invalid/")
    # Legacy /opt/cortex/etc and credential-class files with fake content.
    assert "/opt/cortex/etc/copilot-oauth-config.json" in entries
    credential_paths = {row["path"] for row in manifest["credentials"]}
    assert credential_paths <= set(entries)
    assert all(entries[path].get("class") == "credential" for path in credential_paths)
    assert {(row["principal"], row["provider"]) for row in manifest["credentials"]} == {
        ("builder", "codex"),
        ("manager", "github"),
        ("reviewer-planner", "agy"),
        ("reviewer-planner", "copilot"),
    }


def test_manifest_carries_no_personal_paths_or_real_credentials(manifest) -> None:
    raw = MANIFEST.read_text(encoding="utf-8")
    assert "/home/" not in raw
    assert "/Users/" not in raw
    for pattern in (
        rb"gh[pousr]_[A-Za-z0-9_]{20,}",
        rb"sk-[A-Za-z0-9_-]{20,}",
    ):
        assert not re.search(pattern, raw.encode())
    for entry in manifest["entries"]:
        if entry.get("class") == "credential":
            assert "legacy-fixture-secret-" in entry["content"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc["accounts"][0].update(uid=991), "release"),
        (lambda doc: doc["accounts"].pop(), "cortex-egress"),
        (
            lambda doc: doc["entries"].append(
                # Built from parts: the repository itself must not carry a
                # personal-home-shaped literal (R-21, tier shareable).
                {"path": "/".join(("", "home", "fixture-user", "x")), "type": "file",
                 "owner": "root", "group": "root", "mode": "0644", "content": "x"}
            ),
            "/home/",
        ),
        (
            lambda doc: doc["entries"].append(
                {"path": "/opt/cortex/bin", "type": "directory", "owner": "root",
                 "group": "root", "mode": "0755"}
            ),
            "duplicate",
        ),
        (
            lambda doc: doc["entries"].append(
                {"path": "/opt/cortex/missing/child", "type": "file", "owner": "root",
                 "group": "root", "mode": "0644", "content": "x"}
            ),
            "parent",
        ),
        (lambda doc: doc["entries"][10].update(owner="nobody-known"), "owner"),
        (lambda doc: doc["credentials"][0].update(path="/opt/cortex/bin"), "credential"),
        (lambda doc: doc["expected"]["quarantine"].append("/nowhere"), "expected"),
    ],
    ids=[
        "release-uid",
        "missing-account",
        "personal-path",
        "duplicate-path",
        "missing-parent",
        "unknown-owner",
        "credential-not-a-credential",
        "unknown-expected",
    ],
)
def test_manifest_validation_fails_closed(
    fixture_module, manifest, mutate, message: str
) -> None:
    broken = deepcopy(manifest)
    mutate(broken)
    with pytest.raises(fixture_module.FixtureError, match=re.escape(message)):
        fixture_module.validate_manifest(broken)


def test_host_overlay_is_the_manifest_ids_and_the_legacy_block(
    fixture_module, manifest
) -> None:
    overlay = fixture_module.host_overlay(manifest)
    assert set(overlay) == {"accounts", "service_accounts"}
    assert overlay["accounts"]["cortex-manager"] == {"uid": 1999, "gid": 1987}
    assert overlay["service_accounts"]["cortex-egress"] == {
        "uid": 1993,
        "gid": 1983,
        "home": "/srv/cortex-egress",
    }
    legacy.validate_host_overlay(overlay)

    bound = fixture_module.host_overlay(manifest, inventory_sha256="a" * 64)
    assert bound["legacy_adoption"] == {
        "inventory_sha256": "a" * 64,
        "quarantine_root": "/var/lib/cortex-installer/legacy-quarantine",
        "census_exceptions": [
            {
                "path": "/var/lib/cortex/coordinator/review-sandboxes",
                "principal": "cortex-reviewer-planner",
            }
        ],
        "quarantine_paths": ["/var/lib/cortex-builder/.bash_history"],
    }
    # The overlay digest the plan records excludes the legacy block.
    assert legacy.host_overlay_record(bound) == legacy.host_overlay_record(overlay)


def test_credential_secrets_are_the_fake_credential_contents(
    fixture_module, manifest
) -> None:
    secrets = fixture_module.credential_secrets(manifest)
    assert secrets and all(len(value) >= 8 for value in secrets)
    assert b"legacy-fixture-secret-builder-codex" in secrets


# ---------------------------------------------------------------------------
# seeding: root only inside a container, rootless under a temporary root
# ---------------------------------------------------------------------------


def test_seed_refuses_to_run_outside_a_disposable_container(
    fixture_module, manifest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("container", raising=False)
    monkeypatch.setattr(fixture_module, "_container_markers", lambda: ())
    with pytest.raises(fixture_module.FixtureError, match="container"):
        fixture_module.seed(manifest, root=Path("/"), rootless=False)
    with pytest.raises(fixture_module.FixtureError, match="rootless"):
        fixture_module.seed(manifest, root=Path("/"), rootless=True)


def _seed(fixture_module, manifest, tmp_path: Path) -> Path:
    host = tmp_path / "host"
    host.mkdir(parents=True, exist_ok=True)
    fixture_module.seed(manifest, root=host, rootless=True)
    return host


def test_rootless_seed_lays_out_the_fixture_tree(
    fixture_module, manifest, tmp_path: Path
) -> None:
    host = _seed(fixture_module, manifest, tmp_path)

    venv = host / "opt/cortex/venv"
    assert venv.is_dir() and not venv.is_symlink()
    claude = host / "opt/cortex/toolchain/bin/claude"
    assert claude.stat().st_size == 2097152
    assert claude.stat().st_size > legacy.CONTENT_HASH_MAX_BYTES
    assert os.readlink(host / "opt/cortex/toolchain/bin/codex") == "../lib/codex/codex"
    # Absolute link targets are rebased under the temporary root.
    assert os.readlink(
        host / "etc/systemd/system/multi-user.target.wants/cortex-manager.service"
    ) == str(host / "etc/systemd/system/cortex-manager.service")
    unit = (host / "etc/systemd/system/cortex-manager.service").read_text()
    assert "User=cortex-manager" in unit and "WantedBy=multi-user.target" in unit
    auth = host / "var/lib/cortex-builder/.codex/auth.json"
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    # The source checkout carries a branch and a worktree in the job pool.
    repository = host / "var/lib/cortex/repos/paulsha-cortex"
    branches = subprocess.run(
        ["git", "-C", str(repository), "branch", "--format=%(refname:short)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert {"legacy/operator-hotfix", "legacy/job-legacy-1"} <= set(branches)
    worktree = host / "var/lib/cortex/worktree/job-legacy-1"
    assert (worktree / ".git").is_file()
    assert (repository / "NOTES.local").is_file()
    # The egress home stays absent, as on the reference host.
    assert not (host / "srv/cortex-egress").exists()


def test_seed_refuses_to_overwrite_an_existing_fixture_path(
    fixture_module, manifest, tmp_path: Path
) -> None:
    host = tmp_path / "host"
    (host / "opt/cortex").mkdir(parents=True)
    with pytest.raises(fixture_module.FixtureError, match="already exists"):
        fixture_module.seed(manifest, root=host, rootless=True)


# ---------------------------------------------------------------------------
# offline proof: the fixture inventories and plans without a failure
# ---------------------------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _realistic_bundle(tmp_path: Path) -> Path:
    """A bundle with the release's six-tool toolchain (file and tree shapes)."""

    root = tmp_path / "candidate"
    for directory in ("dist", "wheelhouse", "toolchain", "source"):
        (root / directory).mkdir(parents=True)
    wheel = root / "dist/paulsha_cortex-0.1.12-py3-none-any.whl"
    wheel.write_bytes(b"legacy rc profile candidate wheel\n")
    (root / "wheelhouse" / wheel.name).write_bytes(wheel.read_bytes())
    for name in ("codex", "claude", "copilot", "agy"):
        (root / "toolchain" / name).write_bytes(f"{name}\n".encode())
    trees = tmp_path / "trees"
    for name, entry in (("srt", "bin/cli.js"), ("openspec", "bin/openspec.js")):
        (trees / name / "bin").mkdir(parents=True)
        (trees / name / entry).write_text("// tool\n", encoding="utf-8")
    (root / "source/paulsha-cortex.bundle").write_bytes(b"source bundle\n")
    remote = "https://github.com/hamanpaul/paulsha-cortex.git"
    completed = subprocess.run(
        [
            sys.executable,
            str(QUALIFICATION / "prepare_candidate.py"),
            "--root",
            str(root),
            "--candidate-sha",
            CANDIDATE_SHA,
            "--wheel",
            str(wheel),
            "--tool-file",
            f"codex,0.157.1,{root / 'toolchain/codex'}",
            "--tool-file",
            f"claude,2.1.239,{root / 'toolchain/claude'}",
            "--tool-file",
            f"copilot,1.0.88,{root / 'toolchain/copilot'}",
            "--tool-file",
            f"agy,1.2.11,{root / 'toolchain/agy'}",
            "--tool-tree",
            f"srt,0.0.73,{trees / 'srt'},bin/cli.js",
            "--tool-tree",
            f"openspec,1.10.0,{trees / 'openspec'},bin/openspec.js",
            "--repository",
            f"paulsha-cortex,{CANDIDATE_SHA},{remote},{root / 'source/paulsha-cortex.bundle'}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return root / "bundle.json"


def _rebased_release_config(tmp_path: Path, bundle: Path, host: Path) -> dict:
    """The exact release install config, with every host path under ``host``."""

    output = tmp_path / "install-config.yaml"
    completed = subprocess.run(
        [
            sys.executable,
            str(QUALIFICATION / "write_install_config.py"),
            "--bundle",
            str(bundle),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    config = json.loads(output.read_text(encoding="utf-8"))
    for section in ("accounts", "service_accounts"):
        for row in config[section].values():
            row["home"] = str(host) + row["home"]
    config["roots"] = {key: str(host) + value for key, value in config["roots"].items()}
    return config


class FixtureHost(legacy.LocalLegacyHostBackend):
    """Real read-only access to the seeded tree; identities from the manifest."""

    def __init__(self, fixture_module, manifest: dict, host: Path, plan: dict) -> None:
        super().__init__(require_root=False)
        self.host = host
        rows = manifest["accounts"] + manifest["extra_accounts"]
        self.users = [
            legacy.PasswdEntry(
                row["name"], row["uid"], row["gid"], str(host) + row["home"], row["shell"]
            )
            for row in rows
        ]
        self.groups = [legacy.GroupEntry(row["name"], row["gid"], ()) for row in rows]
        self.uid_names = {row["uid"]: row["name"] for row in rows}
        uids = {"root": 0, **{row["name"]: row["uid"] for row in rows}}
        gids = {"root": 0, **{row["name"]: row["gid"] for row in rows}}
        # Rootless seeding cannot chown; project the manifest's intended
        # ownership so owner-dependent dispositions match the container run.
        self.owners = {
            str(host) + entry["path"]: (uids[entry["owner"]], gids[entry["group"]])
            for entry in manifest["entries"]
        }
        self.tree_owners = []
        for repository in manifest["repositories"]:
            for row in (repository, *repository["worktrees"]):
                self.tree_owners.append(
                    (str(host) + row["path"], (uids[row["owner"]], gids[row["group"]]))
                )
        self.tree_owners.sort(key=lambda item: len(item[0]), reverse=True)
        self.forbidden_reads = {
            str(host) + entry["path"]
            for entry in manifest["entries"]
            if entry.get("class") == "credential"
        }
        self.read_calls: list[str] = []
        self.writable = {
            name: {str(host) + path for path in paths}
            for name, paths in fixture_module.writable_paths(manifest).items()
        }
        systemd = plan["roots"]["systemd"]
        self.services = {
            unit: {
                "load_state": "loaded",
                "unit_file_state": "enabled",
                "fragment_path": f"{systemd}/{unit}",
                "drop_in_paths": [],
                "user": user,
                "exec_path": str(host) + "/opt/cortex/venv/bin/cortex",
                "active_state": "active",
                "sub_state": "running",
            }
            for unit, user in fixture_module.service_users(manifest).items()
        }

    def machine_id(self) -> str:
        return MACHINE_ID

    def hostname(self) -> str:
        return "cortex-qualification"

    def lstat(self, path: str):
        observed = super().lstat(path)
        if observed is None:
            return None
        owner = self.owners.get(str(path))
        if owner is None:
            owner = next(
                (
                    identity
                    for root, identity in self.tree_owners
                    if str(path) == root or str(path).startswith(root + "/")
                ),
                (0, 0),
            )
        values = list(observed[:10])
        values[4], values[5] = owner
        return os.stat_result(
            values,
            {
                "st_atime_ns": observed.st_atime_ns,
                "st_mtime_ns": observed.st_mtime_ns,
                "st_ctime_ns": observed.st_ctime_ns,
            },
        )

    def passwd_entries(self):
        return tuple(self.users)

    def group_entries(self):
        return tuple(self.groups)

    def group_list(self, name: str, gid: int):
        return [gid]

    def password_locked(self, name: str):
        return True

    def read_acl(self, path: str):
        return []

    def sha256_file(self, path: str, observed: os.stat_result) -> str:
        self.read_calls.append(str(path))
        if str(path) in self.forbidden_reads:
            raise AssertionError(f"inventory read a fixture credential: {path}")
        return super().sha256_file(path, observed)

    def service_status(self, unit: str):
        return dict(self.services[unit])

    def in_flight(self, job_uids, plan):
        return {"job_processes": 0, "durable_jobs": 0}

    def writable_paths(self, identity, tree):
        roots = self.writable.get(self.uid_names[identity.uid], set())
        paths = list(tree.paths())
        return legacy.CensusResult(
            writable=tuple(
                path
                for path in paths
                if any(path == root or path.startswith(root + "/") for root in roots)
            ),
            unstable=(),
        )


def _adopt(fixture_module, manifest, tmp_path: Path, *, overlay_changes=None):
    host = tmp_path / "host"
    host.mkdir(parents=True)
    bundle = _realistic_bundle(tmp_path)
    config = _rebased_release_config(tmp_path, bundle, host)
    overlay = fixture_module.host_overlay(manifest, root=host)
    base = install_cli._plan_document(config, bundle, overlay=overlay)
    fixture_module.seed(manifest, root=host, rootless=True)
    backend = FixtureHost(fixture_module, manifest, host, base)
    document = legacy.collect_legacy_inventory(
        plan=base, backend=backend, host_overlay=overlay
    )
    inventory = tmp_path / "inventory" / "legacy-inventory.json"
    inventory.parent.mkdir()
    legacy.publish_inventory(inventory, document)
    bound = fixture_module.host_overlay(
        manifest, root=host, inventory_sha256=document["inventory_sha256"]
    )
    if overlay_changes is not None:
        overlay_changes(bound)
    plan = install_cli._plan_document(
        config,
        bundle,
        overlay=bound,
        legacy_inventory=inventory,
        machine_id=MACHINE_ID,
    )
    return plan, host, backend, document


def test_fixture_inventory_never_reads_a_credential(
    fixture_module, manifest, tmp_path: Path
) -> None:
    _plan, _host, backend, document = _adopt(fixture_module, manifest, tmp_path)
    assert not set(backend.read_calls) & backend.forbidden_reads
    serialized = json.dumps(document)
    for secret in fixture_module.credential_secrets(manifest):
        assert secret.decode() not in serialized


def test_fixture_plans_without_failure_and_quarantines_exactly_the_expected_objects(
    fixture_module, manifest, tmp_path: Path
) -> None:
    plan, host, _backend, _document = _adopt(fixture_module, manifest, tmp_path)

    block = plan["legacy_adoption"]
    quarantined = {row["path"] for row in block["quarantine"]}
    expected = {str(host) + path for path in manifest["expected"]["quarantine"]}
    assert quarantined == expected, {
        "unexpected": sorted(quarantined - expected),
        "missing": sorted(expected - quarantined),
    }
    reasons = {row["path"].removeprefix(str(host)): row["reason"] for row in block["quarantine"]}
    assert reasons["/opt/cortex/venv"] == "venv-active-directory"
    assert reasons["/opt/cortex/toolchain/bin"] == "toolchain-bin"
    assert reasons["/var/lib/cortex/repos/paulsha-cortex"] == "source-repository"
    assert reasons["/var/lib/cortex/worktree"] == "job-worktree-pool"
    assert reasons["/var/lib/cortex/gate-worktree"] == "job-worktree-pool"
    assert reasons["/etc/systemd/system/cortex-reviewer-job@.service.d"] == "authority"
    assert reasons["/opt/cortex/etc/agy-reviewer-settings.json"] == "superseded-by-launcher"
    assert reasons["/var/lib/cortex-builder/.bash_history"] == "operator"
    assert reasons["/var/lib/cortex/legacy-imported"] == "state-top"
    assert reasons["/var/lib/cortex/coordinator/tmpq1w2e3r4.tmp"] == "managed-residue"
    assert reasons["/var/lib/cortex/coordinator/review-sandboxes"] == "managed-subdir"
    assert reasons["/var/lib/cortex-reviewer-planner/.claude"] == "managed-symlink-mismatch"
    assert reasons["/var/lib/cortex-builder/.copilot"] == "credential"

    adopted = set(block["adopted"])
    assert {f"account:{name}" for name in CORTEX_ACCOUNTS} <= adopted
    assert {
        "systemd:enable:cortex-egress-proxy.service",
        "systemd:enable:cortex-manager.service",
        "systemd:enable:cortex-monitor.service",
        "asset:builder-agy-state",
        "asset:reviewer-planner-agy-state",
        "asset:runtime-agents-tree",
        "asset:coordinator-root-tree",
        "asset:repo-source-tree",
        "asset:runtime-run-tree",
    } <= adopted
    assert block["census_exceptions"] == [
        {
            "path": str(host) + "/var/lib/cortex/coordinator/review-sandboxes",
            "principal": "cortex-reviewer-planner",
        }
    ]
    # The adopted samples stay inside adopted-in-place directories.
    for sample in manifest["expected"]["adopted_samples"]:
        assert not any(
            (str(host) + sample).startswith(path + "/") or str(host) + sample == path
            for path in quarantined
        )


def test_fixture_census_exception_is_load_bearing(
    fixture_module, manifest, tmp_path: Path
) -> None:
    def drop_exceptions(bound: dict) -> None:
        bound["legacy_adoption"]["census_exceptions"] = []

    with pytest.raises(legacy.LegacyAdoptionPlanError) as caught:
        _adopt(fixture_module, manifest, tmp_path, overlay_changes=drop_exceptions)
    assert any(
        "cortex-reviewer-planner can write" in failure and "review-sandboxes" in failure
        for failure in caught.value.failures
    )


def test_fixture_operator_quarantine_path_is_load_bearing(
    fixture_module, manifest, tmp_path: Path
) -> None:
    def drop_operator_paths(bound: dict) -> None:
        bound["legacy_adoption"]["quarantine_paths"] = []

    with pytest.raises(legacy.LegacyAdoptionPlanError) as caught:
        _adopt(fixture_module, manifest, tmp_path, overlay_changes=drop_operator_paths)
    assert any(
        failure.startswith("unclassified:") and ".bash_history" in failure
        for failure in caught.value.failures
    )


def test_fixture_credentials_are_reimportable_from_quarantine(
    fixture_module, manifest, tmp_path: Path
) -> None:
    plan, host, _backend, _document = _adopt(fixture_module, manifest, tmp_path)
    required = {
        (row["principal"], row["provider"]) for row in plan["required_credentials"]
    }
    sources = fixture_module.credential_sources(manifest, plan, root=host)
    assert set(sources) == required
    quarantine_root = plan["legacy_adoption"]["quarantine_root"]
    from paulsha_cortex.trust_root.install.core import credential_source_basename

    for (principal, provider), source in sources.items():
        assert source.startswith(quarantine_root + "/")
        assert Path(source).name == credential_source_basename(principal, provider)
