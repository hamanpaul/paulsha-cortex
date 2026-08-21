"""RED contract tests for #718 per-job trust-root isolation.

These tests deliberately describe the one surface table that the implementation
must project into provisioning, generated units, replica properties, and probes.
They must remain failing until that table and its consumers are implemented.
"""

from __future__ import annotations

import pytest
import os
import shlex
import subprocess
import sys
from pathlib import Path

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
    assert any(
        "cp -R --no-preserve=all" in line and "/.codex" in line
        for line in file_lines
    )
    assert any("auth.json" in line and "install -D" in line for line in lines)


def test_canonical_authorities_are_registry_backed_deployment_assets() -> None:
    from paulsha_cortex.trust_root.registry import ASSET_REGISTRY

    registered = {asset.asset_id for asset in ASSET_REGISTRY}
    paths = permgen.DEFAULT_LAYOUT.asset_paths()
    assert paths["codex-control-root"] == permgen.DEFAULT_LAYOUT.codex_control_root
    assert paths["codex-credential-root"] == permgen.DEFAULT_LAYOUT.codex_credential_root
    assert {"codex-control-root", "codex-credential-root"} <= registered


def test_prompt_roots_are_registry_assets_and_sibling_to_spec_spools() -> None:
    from paulsha_cortex.trust_root import registry

    registered = {asset.asset_id for asset in registry.ASSET_REGISTRY}
    for principal in registry.PROMPT_JOB_PRINCIPALS:
        asset_id = registry.job_prompt_root_asset_id(principal)
        prompt_root = permgen.DEFAULT_LAYOUT.job_prompt_root_for(principal)
        spec_root = permgen.DEFAULT_LAYOUT.job_spec_spool_for(principal)
        assert asset_id in registered
        assert prompt_root == str(Path(spec_root).parent.parent / "job-prompts" / principal.value)
        assert Path(prompt_root).parent == Path(spec_root).parent.parent / "job-prompts"
        assert prompt_root not in {
            permgen._surface_root(row, permgen.DEFAULT_LAYOUT)
            for row in permgen.PER_JOB_WRITABLE_SURFACES
        }


def test_claude_workflow_prompt_is_not_an_argv_element() -> None:
    """A real oversized workflow envelope must cross the launcher via stdin."""
    from paulsha_cortex.coordinator.launcher import build_claude_argv, build_wrapper_script

    prompt = "sentinel-prompt-" + ("x" * 140_000)
    inner = build_claude_argv(
        prompt=prompt,
        prompt_via_stdin=True,
        slice_id="job-a",
        log_dir="/tmp/log",
    )
    assert prompt not in inner
    script = build_wrapper_script(
        inner_argv=inner,
        stdin_prompt=prompt,
        prompt_via_stdin=True,
        sentinel="/tmp/exit",
        ledger="/tmp/ledger",
        worktree="/tmp/worktree",
        repo_root=None,
        run_gates=False,
    )
    assert "cat | claude -p" in script
    assert prompt not in script


def test_private_prompt_file_survives_real_oversized_launch(tmp_path) -> None:
    """The >MAX_ARG_STRLEN path reaches a child byte-for-byte without argv/stdin."""
    from paulsha_cortex.coordinator.launcher import build_wrapper_script

    prompt = "workflow-sentinel-" + ("x" * 140_000)
    prompt_file = tmp_path / ".prompt-job-a"
    output_file = tmp_path / "child-bytes"
    sentinel = tmp_path / "job.exit"
    prompt_file.write_bytes(prompt.encode())
    child = [
        sys.executable,
        "-c",
        (
            "import pathlib,sys; "
            f"pathlib.Path({str(output_file)!r}).write_bytes(sys.stdin.buffer.read())"
        ),
    ]
    script = build_wrapper_script(
        inner_argv=child,
        prompt_file=str(prompt_file),
        sentinel=str(sentinel),
        ledger=str(tmp_path / "ledger"),
        worktree=str(tmp_path),
        repo_root=None,
        run_gates=False,
    )
    assert prompt not in script
    subprocess.run(["bash", "-c", script], check=True)
    assert output_file.read_bytes() == prompt.encode()
    assert sentinel.read_text() == "0"


def test_template_prompt_channel_is_byte_exact_and_not_renameable_by_real_job_uid(
    tmp_path,
) -> None:
    """Exercise the template-shaped wrapper with real split UIDs when deployed."""
    import shutil

    if os.geteuid() != 0 or shutil.which("runuser") is None or shutil.which("setfacl") is None:
        pytest.skip("requires root plus deployed split-UID accounts")
    try:
        import pwd

        manager_uid = pwd.getpwnam("cortex-manager").pw_uid
        job_account = "cortex-builder"
        pwd.getpwnam(job_account)
        foreign = "cortex-reviewer-planner"
        pwd.getpwnam(foreign)
    except KeyError:
        pytest.skip("deployed split-UID accounts are unavailable")

    spec_spool = tmp_path / "job-specs" / "builder"
    spec_spool.mkdir(parents=True)
    os.chown(spec_spool.parent, manager_uid, manager_uid)
    os.chown(spec_spool, manager_uid, manager_uid)
    os.chmod(spec_spool.parent, 0o700)
    os.chmod(spec_spool, 0o700)
    subprocess.run(["setfacl", "-m", f"u:{job_account}:--x", str(tmp_path)], check=True)
    instance = job_runner.template_instance_id("real-template-prompt")
    prompt_dir = Path(
        job_runner.job_prompt_spool_dir(
            str(spec_spool), principal="builder", instance=instance, account=job_account
        )
    )
    prompt_path = Path(job_runner.job_prompt_path(str(prompt_dir), instance))
    prompt = ("template-sentinel-" + ("x" * 140_000)).encode()
    job_runner.write_job_prompt(str(prompt_path), prompt.decode(), account=job_account)
    from paulsha_cortex.coordinator.launcher import build_wrapper_script

    script = build_wrapper_script(
        inner_argv=[
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
        prompt_file=str(prompt_path),
        sentinel=str(tmp_path / "unused.exit"),
        ledger=str(tmp_path / "ledger"),
        worktree=str(tmp_path),
        repo_root=None,
        run_gates=False,
        write_sentinel=False,
    )
    completed = subprocess.run(
        ["runuser", "-u", job_account, "--", "bash", "-c", script],
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stdout == prompt
    assert subprocess.run(
        ["runuser", "-u", job_account, "--", "mv", str(prompt_dir), str(prompt_dir) + ".moved"],
        check=False,
        capture_output=True,
    ).returncode != 0
    assert subprocess.run(
        ["runuser", "-u", foreign, "--", "cat", str(prompt_path)],
        check=False,
        capture_output=True,
    ).returncode != 0
    assert job_runner.reap_orphaned_prompt_slots(
        str(spec_spool), principal="builder"
    ) == ()


def test_private_prompt_bound_fails_closed_before_publish(tmp_path) -> None:
    path = tmp_path / ".prompt-job-a"
    with pytest.raises(job_runner.JobRunnerError) as excinfo:
        job_runner.write_job_prompt(
            str(path), "too-large", max_bytes=1
        )
    assert excinfo.value.diagnostic.reason == "job-runner-prompt-too-large"
    assert not path.exists()


def test_manager_exit_recorder_removes_private_prompt_after_termination(tmp_path) -> None:
    prompt = tmp_path / ".prompt-job-a"
    prompt.write_text("small\n")
    sentinel = tmp_path / "exit"
    argv = job_runner.build_manager_exit_recorder_argv(
        client_argv=["bash", "-c", "exit 0"],
        sentinel=str(sentinel),
        cleanup_path=str(prompt),
    )
    subprocess.run(argv, check=True)
    assert not prompt.exists()
    assert sentinel.read_text() == "0"


def test_codex_reasoning_effort_is_explicit_for_pinned_models() -> None:
    from paulsha_cortex.coordinator.launcher import build_codex_argv

    luna = build_codex_argv(
        prompt="PROMPT", slice_id="job-a", log_dir="/tmp", model="gpt-5.6-luna"
    )
    spark = build_codex_argv(
        prompt="PROMPT",
        slice_id="job-b",
        log_dir="/tmp",
        model="gpt-5.3-codex-spark",
    )
    assert 'model_reasoning_effort="max"' in luna
    assert 'model_reasoning_effort="xhigh"' in spark


@pytest.mark.parametrize(
    ("principal", "account"),
    (("builder", "cortex-builder"), ("reviewer", "cortex-reviewer-planner")),
)
def test_owner_aware_publisher_real_uid_arms(principal, account, tmp_path) -> None:
    """Exercise unchanged Manager seed and job-owned atomic refresh as real UIDs."""
    import hashlib
    import shutil

    if os.geteuid() != 0 or shutil.which("runuser") is None or shutil.which("setfacl") is None:
        pytest.skip("requires root plus deployed builder/reviewer/runuser/setfacl accounts")
    try:
        import pwd

        manager_uid = pwd.getpwnam("cortex-manager").pw_uid
        job_uid = pwd.getpwnam(account).pw_uid
        foreign = "cortex-reviewer-planner" if account == "cortex-builder" else "cortex-builder"
        pwd.getpwnam(foreign)
    except KeyError:
        pytest.skip("deployed split-UID accounts are unavailable")

    command = spool_slot.publish_runtime_credential_command(
        manager_account="cortex-manager"
    )
    manager_seed_home = tmp_path / f"{principal}-manager-seed"
    manager_seed_home.mkdir()
    seed = manager_seed_home / "auth.json"
    seed.write_bytes(b'{"seed":"manager"}\n')
    os.chown(manager_seed_home, manager_uid, manager_uid)
    os.chown(seed, manager_uid, manager_uid)
    os.chmod(manager_seed_home, 0o711)
    os.chmod(seed, 0o600)
    unchanged = subprocess.run(
        ["runuser", "-u", account, "--", "env", f"CODEX_HOME={manager_seed_home}", "bash", "-c", command],
        check=False,
    )
    assert unchanged.returncode == 0
    assert seed.read_bytes() == b'{"seed":"manager"}\n'
    assert subprocess.run(
        ["runuser", "-u", "cortex-manager", "--", "cat", str(seed)],
        check=False,
        capture_output=True,
    ).stdout == b'{"seed":"manager"}\n'

    job_home = tmp_path / f"{principal}-atomic-refresh"
    job_home.mkdir()
    refreshed = job_home / "auth.json"
    os.chown(job_home, job_uid, job_uid)
    os.chmod(job_home, 0o700)
    subprocess.run(
        ["setfacl", "-m", "u:cortex-manager:--x", str(job_home)],
        check=True,
    )
    # Model the real Codex refresh: UMask=0077, a private temporary inode, and
    # an atomic rename before the owner-aware publisher runs.
    refresh_script = (
        f"umask 0077; tmp={shlex.quote(str(job_home / '.auth.tmp'))}; "
        f"printf '%s\\n' '{{\"seed\":\"job-refresh\"}}' > \"$tmp\"; "
        f"mv -- \"$tmp\" {shlex.quote(str(refreshed))}; {command}"
    )
    refreshed_run = subprocess.run(
        ["runuser", "-u", account, "--", "env", f"CODEX_HOME={job_home}", "bash", "-c", refresh_script],
        check=False,
    )
    assert refreshed_run.returncode == 0
    assert subprocess.run(
        ["runuser", "-u", "cortex-manager", "--", "cat", str(refreshed)],
        check=False,
        capture_output=True,
    ).stdout == b'{"seed":"job-refresh"}\n'
    denied = subprocess.run(
        ["runuser", "-u", foreign, "--", "cat", str(refreshed)],
        check=False,
        capture_output=True,
    )
    assert denied.returncode != 0
    foreign_write = subprocess.run(
        ["runuser", "-u", foreign, "--", "bash", "-c", f"printf x >> {refreshed!s}"],
        check=False,
        capture_output=True,
    )
    assert foreign_write.returncode != 0

    next_home = tmp_path / f"{principal}-next-seed"
    next_home.mkdir()
    next_seed = next_home / "auth.json"
    next_seed.write_bytes(refreshed.read_bytes())
    os.chown(next_home, manager_uid, manager_uid)
    os.chown(next_seed, manager_uid, manager_uid)
    os.chmod(next_home, 0o711)
    os.chmod(next_seed, 0o600)
    subprocess.run(
        ["setfacl", "-m", f"u:{account}:r--,m::r--", str(next_seed)],
        check=True,
    )
    next_read = subprocess.run(
        ["runuser", "-u", account, "--", "cat", str(next_seed)],
        check=False,
        capture_output=True,
    )
    assert next_read.returncode == 0
    assert hashlib.sha256(next_read.stdout).hexdigest() == hashlib.sha256(
        b'{"seed":"job-refresh"}\n'
    ).hexdigest()


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


def test_control_tree_copy_strips_xattrs_hardlinks_and_rejects_special_entries(tmp_path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / "plugins").mkdir(parents=True)
    (source / "plugins" / "plugin.json").write_bytes(b"plugin\n")
    os.link(source / "plugins" / "plugin.json", source / "plugins" / "alias.json")
    xattr_supported = True
    try:
        os.setxattr(source / "plugins" / "plugin.json", b"user.test", b"source-only")
    except OSError:
        xattr_supported = False
    spool_slot._copy_regular_tree(source, destination)
    copied = destination / "plugins"
    assert (copied / "plugin.json").read_bytes() == b"plugin\n"
    assert (copied / "alias.json").read_bytes() == b"plugin\n"
    assert (copied / "plugin.json").stat().st_nlink == 1
    assert (copied / "alias.json").stat().st_nlink == 1
    assert (copied.stat().st_mode & 0o777) == 0o755
    assert (copied / "plugin.json").stat().st_mode & 0o777 == 0o644
    if xattr_supported:
        assert b"user.test" not in os.listxattr(copied / "plugin.json")

    symlink_source = tmp_path / "symlink-source"
    symlink_source.mkdir()
    (symlink_source / "plugins").mkdir()
    (symlink_source / "plugins" / "link").symlink_to(source / "plugins" / "plugin.json")
    with pytest.raises(spool_slot.SpoolSlotError):
        spool_slot._copy_regular_tree(symlink_source / "plugins", tmp_path / "symlink-dest")

    special_source = tmp_path / "special-source"
    special_source.mkdir()
    os.mkfifo(special_source / "fifo")
    with pytest.raises(spool_slot.SpoolSlotError):
        spool_slot._copy_regular_tree(special_source, tmp_path / "special-dest")


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


def test_generated_migration_accepts_only_root_owned_legacy_codex_symlink(tmp_path) -> None:
    """Run the generated migration against a real legacy symlink when root is available."""
    import pwd
    import shutil

    if os.geteuid() != 0 or shutil.which("bash") is None:
        pytest.skip("requires root for generated ownership migration")
    try:
        pwd.getpwnam("cortex-manager")
    except KeyError:
        pytest.skip("cortex-manager is unavailable")

    layout = permgen.PathLayout(
        agents_root=str(tmp_path / "agents"),
        home_root=str(tmp_path / "home"),
        deploy_root=str(tmp_path / "deploy"),
    )
    source_target = tmp_path / "legacy-codex"
    (source_target / "plugins").mkdir(parents=True)
    (source_target / "skills").mkdir()
    (source_target / "plugins" / "plugin.json").write_text('{"plugin":true}\n')
    (source_target / "skills" / "policy.md").write_text("policy\n")
    (source_target / "config.toml").write_text("model = 'deployed'\n")
    (source_target / "hooks.json").write_text("{}\n")
    (source_target / "auth.json").write_text('{"seed":true}\n')
    legacy = Path(layout.home_of("cortex-builder")) / ".codex"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(source_target, target_is_directory=True)
    controls_parent = Path(layout.codex_control_root)
    controls_parent.mkdir(parents=True)
    credential_parent = Path(layout.codex_credential_root) / "builder"
    credential_parent.mkdir(parents=True)

    commands = layout.codex_authority_seed_commands(permgen.FOUR_WAY_SCHEME)
    for command in commands:
        completed = subprocess.run(["bash", "-eu", "-c", command], capture_output=True)
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    migrated = Path(layout.codex_control_root) / "builder"
    assert migrated.is_dir() and not migrated.is_symlink()
    assert (migrated / "config.toml").read_text() == "model = 'deployed'\n"
    assert (migrated / "plugins" / "plugin.json").read_text() == '{"plugin":true}\n'
    assert (migrated / "skills" / "policy.md").read_text() == "policy\n"
    assert (migrated / "config.toml").stat().st_mode & 0o777 == 0o644
    assert (migrated / "plugins").stat().st_mode & 0o777 == 0o755
    assert (Path(layout.codex_credential_root) / "builder" / "auth.json").read_text() == '{"seed":true}\n'


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
    assert "test -O" in command


def test_template_uses_the_canonical_owner_aware_auth_publisher() -> None:
    command = spool_slot.publish_runtime_credential_command(
        manager_account=permgen.FOUR_WAY_SCHEME.durable_state_owner
    ).replace('"$CODEX_HOME/auth.json"', '"$${CODEX_HOME}/auth.json"')
    unit = permgen.build_job_unit(
        permgen.FOUR_WAY_SCHEME, principal=permgen.Principal.BUILDER
    )
    assert f"ExecStopPost=/bin/sh -c '{command}'" in unit.content


@pytest.mark.parametrize(
    ("principal", "executor", "runtime_mode", "exit_code", "expected_status"),
    (
        ("builder", "claude", "direct", 0, "exited"),
        ("reviewer", "claude", "direct", 1, "failed"),
        ("builder", "claude", "systemd-template", 0, "exited"),
        ("reviewer", "copilot", "systemd-template", 1, "failed"),
    ),
)
def test_headless_finalize_uses_typed_lane_not_workflow_kind(
    tmp_path, principal, executor, runtime_mode, exit_code, expected_status
) -> None:
    """Builder/reviewer and direct/isolated non-Codex lanes never harvest Codex state."""
    from paulsha_cortex.coordinator.dispatcher import Dispatcher

    class Registry:
        def __init__(self) -> None:
            self.job = {
                "job_id": "job-a",
                "executor": executor,
                "runtime_mode": runtime_mode,
                "runtime_principal": principal,
            }
            self.updated = None

        def get_job(self, job_id):
            assert job_id == "job-a"
            return dict(self.job)

        def update_headless_result(self, job_id, **kwargs):
            assert job_id == "job-a"
            self.updated = kwargs
            return {**self.job, **kwargs}

    log_path = tmp_path / f"{principal}-{executor}.jsonl"
    log_path.write_text("")
    registry = Registry()
    result = Dispatcher(registry, None, None)._finalize_headless(
        "job-a", exit_code=exit_code, log_path=str(log_path)
    )
    assert result["status"] == expected_status
    assert result.get("runtime_diagnostic") is None
    if exit_code == 0:
        assert registry.updated["provider_outcome"] is None
    else:
        assert registry.updated["provider_outcome"] is not None


@pytest.mark.parametrize(
    "metadata",
    (
        {
            "credential_publish": True,
            "runtime_principal": "builder",
            "runtime_surface": "builder-codex-home",
        },
        {"credential_publish": False},
    ),
)
def test_isolated_codex_finalize_fails_closed_with_durable_runtime_diagnostic(
    tmp_path, monkeypatch, metadata
) -> None:
    from paulsha_cortex.coordinator.dispatcher import Dispatcher

    class Registry:
        def __init__(self) -> None:
            self.job = {
                "job_id": "job-a",
                "executor": "codex",
                "runtime_mode": "systemd-template",
                **metadata,
            }
            self.updated = None

        def get_job(self, job_id):
            assert job_id == "job-a"
            return dict(self.job)

        def update_headless_result(self, job_id, **kwargs):
            assert job_id == "job-a"
            self.updated = kwargs
            return {**self.job, **kwargs}

    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(tmp_path / "coordinator"))
    log_path = tmp_path / "job.jsonl"
    log_path.write_text("")
    registry = Registry()
    result = Dispatcher(registry, None, None)._finalize_headless(
        "job-a", exit_code=0, log_path=str(log_path)
    )
    assert result["runtime_diagnostic"]["source"] == "dispatcher._finalize_headless"
    assert registry.updated["provider_outcome"] is None
    assert registry.updated["status"] == "failed"
    assert registry.updated["runtime_diagnostic"]["reason"] in {
        "runtime-credential-harvest-failed",
        "runtime-publisher-missing",
    }
