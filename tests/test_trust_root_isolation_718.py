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
        f"ReadWritePaths={spool_slot.canonical_job_slot(row.surface_id, 'job-a')}"
        for row in rows
    )


@pytest.mark.parametrize("surface_id", EXPECTED_SURFACES)
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


@pytest.mark.parametrize("surface_id", EXPECTED_SURFACES)
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
    """The emitted installer must preserve desired config/hooks on every rerun."""
    completed = subprocess.run(
        [sys.executable, "-m", "paulsha_cortex.trust_root", "scaffold", "four-way"],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = completed.stdout.splitlines()
    file_lines = [line for line in lines if "config.toml" in line or "hooks.json" in line]
    assert file_lines
    assert all("/dev/null" not in line for line in file_lines)
    assert all("if [ ! -e " in line or line.startswith(("chown ", "chmod ")) for line in file_lines)
    assert any("{}" in line and "hooks.json" in line for line in file_lines)


def test_missing_instance_is_fail_closed_before_rendering_properties() -> None:
    with pytest.raises((ValueError, KeyError)):
        permgen.render_job_writable_properties(instance=None)  # type: ignore[arg-type]


def test_slot_shape_rejects_symlink_and_non_directory() -> None:
    with pytest.raises((ValueError, spool_slot.SpoolSlotError, NotImplementedError)):
        spool_slot.validate_job_slot_shape("/tmp/not-a-slot", allow_symlink=False)
