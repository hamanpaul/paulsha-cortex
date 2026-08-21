"""RED contract tests for #718 per-job trust-root isolation.

These tests deliberately describe the one surface table that the implementation
must project into provisioning, generated units, replica properties, and probes.
They must remain failing until that table and its consumers are implemented.
"""

from __future__ import annotations

import pytest

from paulsha_cortex.coordinator import spool_slot
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
    row_ids = {row.surface_id for row in rows}

    assert set(permgen.generated_writable_surface_ids()) == row_ids
    assert set(permgen.provisioned_writable_surface_ids()) == row_ids
    assert set(permgen.run_under_writable_surface_ids()) == row_ids
    assert set(permgen.probe_writable_surface_ids()) == row_ids


@pytest.mark.parametrize("surface_id", EXPECTED_SURFACES)
def test_canonical_slot_is_instance_scoped_and_rejects_unsafe_identity(surface_id: str) -> None:
    own = spool_slot.canonical_job_slot(surface_id, "job-a")
    foreign = spool_slot.canonical_job_slot(surface_id, "job-b")
    assert own != foreign
    assert own.name == "job-a"
    assert foreign.name == "job-b"
    assert own.parent == foreign.parent

    for unsafe in ("", ".", "..", "job/a", "job\\a", "job\x00a", "job a"):
        with pytest.raises((ValueError, spool_slot.SpoolSlotError)):
            spool_slot.canonical_job_slot(surface_id, unsafe)


def test_slot_template_contains_concrete_instance_and_never_the_writable_root() -> None:
    rows = _surfaces()
    for row in rows:
        rendered = row.slot_template.replace("%i", "job-a")
        assert "%i" not in rendered
        assert rendered.endswith("/job-a")
        assert rendered != row.writable_root
        assert not rendered.startswith(row.writable_root + "/job-a/job-a")


def test_missing_instance_is_fail_closed_before_rendering_properties() -> None:
    with pytest.raises((ValueError, KeyError)):
        permgen.render_job_writable_properties(instance=None)  # type: ignore[arg-type]


def test_slot_shape_rejects_symlink_and_non_directory() -> None:
    with pytest.raises((ValueError, spool_slot.SpoolSlotError, NotImplementedError)):
        spool_slot.validate_job_slot_shape("/tmp/not-a-slot", allow_symlink=False)
