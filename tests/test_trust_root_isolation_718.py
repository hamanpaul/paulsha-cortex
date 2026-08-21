"""RED contract tests for #718 per-job trust-root isolation.

These tests deliberately describe the one surface table that the implementation
must project into provisioning, generated units, replica properties, and probes.
They must remain failing until that table and its consumers are implemented.
"""

from __future__ import annotations

import pytest
import subprocess
import sys

from paulsha_cortex.coordinator import spool_slot
from paulsha_cortex.coordinator import job_runner
from paulsha_cortex.trust_root import permgen


EXPECTED_SURFACES = (
    "commit-spool",
    "monitor-event-spool",
    "review-verdict-spool",
    "gate-ledger-spool",
    "gate-worktree",
    "builder-job-log",
    "reviewer-job-log",
    "gate-job-log",
    "builder-codex-home",
    "reviewer-codex-home",
    "builder-runtime-cache",
    "reviewer-runtime-cache",
)


def _surfaces():
    """Resolve the planned canonical table without hiding the RED failure."""

    table = getattr(permgen, "PER_JOB_WRITABLE_SURFACES", None)
    assert table is not None, "permgen must expose the canonical per-job surface table"
    return table


def test_one_canonical_table_covers_every_declared_writable_surface() -> None:
    rows = _surfaces()
    ids = tuple(row.surface_id for row in rows)
    assert ids == EXPECTED_SURFACES
    assert len(ids) == len(set(ids))
    for row in rows:
        assert row.slot_template
        assert row.provisioner
        assert row.probe


def test_all_consumers_are_derived_from_the_same_surface_rows() -> None:
    rows = _surfaces()
    rendered = permgen.render_job_writable_properties(instance="job-a")
    assert rendered == tuple(
        f"ReadWritePaths={row.writable_root}/{job_runner.template_instance_id('job-a')}"
        for row in rows
    )


@pytest.mark.parametrize("surface_id", EXPECTED_SURFACES[:5])
def test_canonical_slot_is_instance_scoped_and_rejects_unsafe_identity(surface_id: str) -> None:
    own = spool_slot.canonical_job_slot(surface_id, "job-a")
    foreign = spool_slot.canonical_job_slot(surface_id, "job-b")
    assert own != foreign
    assert own.name == job_runner.template_instance_id("job-a")
    assert foreign.name == job_runner.template_instance_id("job-b")
    assert own.parent == foreign.parent

    for unsafe in ("", ".", "..", "job/a", "job\\a", "job\x00a", "job a"):
        with pytest.raises((ValueError, spool_slot.SpoolSlotError)):
            spool_slot.canonical_job_slot(surface_id, unsafe)


def test_slot_template_contains_concrete_instance_and_never_the_writable_root() -> None:
    rows = _surfaces()
    for row in rows:
        rendered = row.slot_template.replace("%i", job_runner.template_instance_id("job-a"))
        assert "%i" not in rendered
        assert rendered.endswith("/" + job_runner.template_instance_id("job-a"))
        assert rendered != row.writable_root
        assert not rendered.startswith(row.writable_root + "/job-a/job-a")


@pytest.mark.parametrize("surface_id", EXPECTED_SURFACES[:5])
def test_every_surface_slot_matches_the_systemd_instance(surface_id: str) -> None:
    raw_job_id = "wf-32bb2160d8-subagent-build-69"
    slot = spool_slot.canonical_job_slot(surface_id, raw_job_id)
    assert slot.name == job_runner.template_instance_id(raw_job_id)


def test_event_producer_uses_explicit_surface_root_once(tmp_path) -> None:
    from paulsha_cortex.monitor.event_spool import EventSpool

    root = tmp_path / "event-spool"
    spool = EventSpool(root, job_id="wf-32bb2160d8-subagent-build-69")
    assert spool.root.parent == root
    assert spool.root.name == job_runner.template_instance_id(
        "wf-32bb2160d8-subagent-build-69"
    )


def test_headless_hook_default_writer_uses_authoritative_job_slot(
    tmp_path, monkeypatch
) -> None:
    from paulsha_cortex.monitor import event_spool
    from paulsha_cortex.porcelain import headless_hook

    root = tmp_path / "event-spool"
    monkeypatch.setattr(event_spool, "monitor_event_spool_root", lambda: root)
    job_id = "wf-32bb2160d8-subagent-build-69"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "gh issue comment 718 -R hamanpaul/paulsha-cortex"},
    }
    assert headless_hook.emit_for_tool_use(
        payload, env={"PSC_JOB_ID": job_id}
    ) == ("hamanpaul/paulsha-cortex#718",)
    owned = spool_slot.canonical_job_slot(
        "monitor-event-spool", job_id, writable_root=root
    )
    assert len(tuple(owned.glob("*.json"))) == 1
    assert tuple(root.glob("*.json")) == ()


def test_scaffold_rerun_never_truncates_deployed_codex_policy(tmp_path) -> None:
    """Installer migrates real deployed bytes and never emits policy stubs."""
    completed = subprocess.run(
        [sys.executable, "-m", "paulsha_cortex.trust_root", "scaffold", "four-way"],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = completed.stdout.splitlines()
    file_lines = [line for line in lines if "codex-controls" in line]
    assert file_lines
    assert all("if [ ! -e " in line or line.startswith("install -d ") for line in file_lines)
    assert not any("deployment-owned Codex configuration" in line for line in lines)
    assert not any("printf" in line and "hooks.json" in line for line in lines)
    assert any("cp -a" in line and "/.codex" in line for line in file_lines)
    assert any("auth.json" in line and "install -D" in line for line in lines)


def test_canonical_authorities_are_registry_backed_deployment_assets() -> None:
    from paulsha_cortex.trust_root.registry import ASSET_REGISTRY

    registered = {asset.asset_id for asset in ASSET_REGISTRY}
    paths = permgen.DEFAULT_LAYOUT.asset_paths()
    assert paths["codex-control-root"] == permgen.DEFAULT_LAYOUT.codex_control_root
    assert paths["codex-credential-root"] == permgen.DEFAULT_LAYOUT.codex_credential_root
    assert {"codex-control-root", "codex-credential-root"} <= registered


def test_missing_instance_is_fail_closed_before_rendering_properties() -> None:
    with pytest.raises((ValueError, KeyError)):
        permgen.render_job_writable_properties(instance=None)  # type: ignore[arg-type]


def test_slot_shape_rejects_symlink_and_non_directory() -> None:
    with pytest.raises((ValueError, spool_slot.SpoolSlotError, NotImplementedError)):
        spool_slot.validate_job_slot_shape("/tmp/not-a-slot", allow_symlink=False)


@pytest.mark.parametrize("principal", (
    permgen.Principal.BUILDER,
    permgen.Principal.REVIEWER,
    permgen.Principal.GATE,
))
def test_production_job_unit_consumes_only_applicable_table_slots(principal) -> None:
    unit = permgen.build_job_unit(permgen.FOUR_WAY_SCHEME, principal=principal)
    applicable = tuple(
        row for row in _surfaces() if principal.value in row.principals
    )
    for row in applicable:
        expected = f"{permgen._surface_root(row, permgen.DEFAULT_LAYOUT)}/%i"
        assert expected in unit.read_write_paths
        assert row.writable_root not in unit.read_write_paths
    for row in _surfaces():
        assert row.writable_root not in unit.read_write_paths
    assert not any(path.endswith("/cache") for path in unit.read_write_paths)
    assert not any(path.endswith("/.codex") for path in unit.read_write_paths)


def test_reviewer_never_receives_builder_event_producer_slot() -> None:
    reviewer = permgen.build_job_unit(
        permgen.FOUR_WAY_SCHEME, principal=permgen.Principal.REVIEWER
    )
    event = spool_slot.writable_surface("monitor-event-spool")
    assert event.principals == ("builder",)
    assert permgen._surface_root(event, permgen.DEFAULT_LAYOUT) + "/%i" not in reviewer.read_write_paths


@pytest.mark.parametrize("role", (job_runner.JOB_ROLE_BUILDER, job_runner.JOB_ROLE_REVIEW))
def test_job_env_uses_authoritative_per_job_codex_and_cache_slots(role: str) -> None:
    env = job_runner.build_job_env(
        manager_env={job_runner.resolve_job_role(role).path_env: "/usr/bin"},
        job_id="job-a", slice_id="slice", repo_root="/repo", workspace=None, role=role,
    )
    instance = job_runner.template_instance_id("job-a")
    principal = "reviewer" if role == job_runner.JOB_ROLE_REVIEW else "builder"
    assert env["CODEX_HOME"].endswith(f"/runtime/codex-home/{principal}/{instance}")
    assert env["XDG_CACHE_HOME"].endswith(f"/runtime/job-cache/{principal}/{instance}")


def test_registry_provisioner_preserves_controls_and_isolates_foreign_slot(
    tmp_path, monkeypatch
) -> None:
    from paulsha_cortex.config import paths

    monkeypatch.setenv("PSC_AGENTS_ROOT", str(tmp_path))
    canonical = tmp_path / "config" / "codex-controls" / "builder"
    (canonical / "plugins").mkdir(parents=True)
    (canonical / "skills").mkdir()
    (canonical / "config.toml").write_text("model = 'deployed'\n")
    (canonical / "hooks.json").write_text("{}\n")
    authority = spool_slot.credential_authority("builder")
    authority.parent.mkdir(parents=True, exist_ok=True)
    authority.write_text('{"seed":true}\n')
    own = spool_slot.provision_runtime_surfaces(
        principal="builder", job_id="job-a", canonical_codex_home=canonical
    )
    foreign = spool_slot.provision_runtime_surfaces(
        principal="builder", job_id="job-b", canonical_codex_home=canonical
    )
    assert len(own) == len(foreign) == 2
    codex_a = next(path for path in own if "codex-home" in str(path))
    codex_b = next(path for path in foreign if "codex-home" in str(path))
    before = (codex_b / "hooks.json").read_bytes()
    (codex_a / "auth.json").write_text('{"refresh":true}\n', encoding="utf-8")
    spool_slot.provision_runtime_surfaces(
        principal="builder", job_id="job-a", canonical_codex_home=canonical
    )
    assert (codex_a / "auth.json").read_text(encoding="utf-8") == '{"refresh":true}\n'
    assert (codex_b / "hooks.json").read_bytes() == before
    for leaf in ("config.toml", "hooks.json", "plugins", "skills"):
        assert (codex_a / leaf).stat().st_mode & 0o222 == 0
    assert paths.agents_root() == tmp_path


def test_runtime_surface_asset_ids_are_real_and_match_layout() -> None:
    from paulsha_cortex.trust_root.registry import ASSET_REGISTRY

    registered = {asset.asset_id for asset in ASSET_REGISTRY}
    layout_paths = permgen.DEFAULT_LAYOUT.asset_paths()
    for row in _surfaces()[-4:]:
        assert row.asset_id in registered
        assert layout_paths[row.asset_id] == permgen._surface_root(row, permgen.DEFAULT_LAYOUT)


def test_provision_projects_canonical_controls_and_auth(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "runtime-root"
    canonical = tmp_path / "account-home" / ".codex"
    (canonical / "plugins" / "vendor").mkdir(parents=True)
    (canonical / "skills").mkdir()
    (canonical / "plugins" / "vendor" / "plugin.json").write_text('{"real":true}\n')
    (canonical / "skills" / "policy.md").write_text("deployed\n")
    (canonical / "config.toml").write_text("model = 'deployed'\n")
    (canonical / "hooks.json").write_text('{"hooks":["deployed"]}\n')
    (canonical / "auth.json").write_text('{"token":"existing"}\n')
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(runtime))
    authority = spool_slot.credential_authority("builder")
    authority.parent.mkdir(parents=True, exist_ok=True)
    authority.write_bytes((canonical / "auth.json").read_bytes())

    slots = spool_slot.provision_runtime_surfaces(
        principal="builder", job_id="job-a", canonical_codex_home=canonical
    )
    codex = next(path for path in slots if "codex-home" in str(path))
    assert (codex / "config.toml").read_bytes() == (canonical / "config.toml").read_bytes()
    assert (codex / "plugins/vendor/plugin.json").read_bytes() == (canonical / "plugins/vendor/plugin.json").read_bytes()
    assert (codex / "auth.json").read_bytes() == (canonical / "auth.json").read_bytes()
    assert (codex / "auth.json").stat().st_mode & 0o060 == 0o060


def test_manager_unreadable_legacy_home_never_degrades_to_stub_controls(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(tmp_path / "runtime"))
    legacy = tmp_path / "legacy" / ".codex"
    target = tmp_path / "legacy-target"
    target.mkdir()
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(target, target_is_directory=True)
    with pytest.raises(spool_slot.SpoolSlotError):
        spool_slot.readable_codex_home(legacy, require_auth=False)
    monkeypatch.setenv("PSC_CODEX_CONTROL_ROOT", str(tmp_path / "missing-controls"))
    with pytest.raises(spool_slot.SpoolSlotError):
        spool_slot.canonical_codex_controls("builder")


def test_auth_refresh_is_committed_as_next_job_seed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(tmp_path))
    controls = tmp_path / "config" / "codex-controls" / "builder"
    (controls / "plugins").mkdir(parents=True)
    (controls / "skills").mkdir()
    (controls / "config.toml").write_text("model = 'deployed'\n")
    (controls / "hooks.json").write_text("{}\n")
    authority = spool_slot.credential_authority("builder")
    authority.parent.mkdir(parents=True, exist_ok=True)
    authority.write_text('{"refresh":"old"}\n')
    first = spool_slot.provision_runtime_surfaces(
        principal="builder", job_id="job-a", canonical_codex_home=controls
    )[0]
    (first / "auth.json").write_text('{"refresh":"new"}\n')
    spool_slot.commit_runtime_credential(principal="builder", job_id="job-a")
    second = spool_slot.provision_runtime_surfaces(
        principal="builder", job_id="job-b", canonical_codex_home=controls
    )[0]
    assert (second / "auth.json").read_text() == '{"refresh":"new"}\n'
    assert (second / "config.toml").read_bytes() == (first / "config.toml").read_bytes()


def test_authority_migration_whitelists_controls_and_normalizes_live_modes() -> None:
    commands = "\n".join(
        permgen.DEFAULT_LAYOUT.codex_authority_seed_commands(permgen.FOUR_WAY_SCHEME)
    )
    assert "cp -a /var/lib/cortex-builder/.codex " not in commands
    for leaf in ("config.toml", "hooks.json", "plugins", "skills"):
        assert f"/.codex/{leaf}" in commands
    for runtime_leaf in ("sessions", "memories", "installation_id", "auth.json"):
        assert f'cp -a /var/lib/cortex-builder/.codex/{runtime_leaf}' not in commands
    assert '! -type d ! -type f' in commands
    assert 'find "$tmp" -type d -exec chmod 0755' in commands
    assert 'find "$tmp" -type f -exec chmod 0644' in commands
    assert "mktemp -d" in commands


@pytest.mark.parametrize("principal", (permgen.Principal.BUILDER, permgen.Principal.REVIEWER))
def test_generated_unit_publishes_auth_to_manager_after_process_stops(principal) -> None:
    unit = permgen.build_job_unit(permgen.FOUR_WAY_SCHEME, principal=principal)
    text = unit.content
    assert "ExecStopPost=" in text
    assert "setfacl -m u:cortex-manager:r--,m::r--" in text
    assert '$${CODEX_HOME}/auth.json' in text


def test_auth_publish_command_uses_named_manager_acl_not_shared_group() -> None:
    command = spool_slot.publish_runtime_credential_command(
        manager_account=permgen.FOUR_WAY_SCHEME.durable_state_owner
    )
    assert "chmod 0640" in command
    assert "setfacl -m u:cortex-manager:r--,m::r--" in command
