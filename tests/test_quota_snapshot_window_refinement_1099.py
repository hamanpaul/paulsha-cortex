"""Issue #1099: same-value snapshots can refine an unknown window epoch."""

from __future__ import annotations

import pytest

from paulsha_cortex.coordinator import quota_ledger, quota_shadow
from test_quota_observation_refine_836 import (
    _NOW,
    _PROFILE_A,
    _binding,
    _iso_utc_ms,
    _observation,
    _pool_descriptor,
    _pool_row,
)


@pytest.fixture(params=("file-ledger", "memory-ledger"))
def quota_service(request, tmp_path):
    if request.param == "file-ledger":
        ledger = quota_ledger.QuotaEventLedger(tmp_path / "quota-events.jsonl")
    else:
        ledger = quota_shadow._MemoryLedger()
    return quota_shadow.QuotaShadowService(ledger)


def _snapshot(descriptor, *, value="20", reset_at_ms=None):
    return _observation(
        descriptor,
        "short",
        value=value,
        observed_at_ms=_NOW,
        reset_at_ms=reset_at_ms,
        ttl_ms=300_000,
    )


def _record_snapshot(service, descriptor, observation):
    return service.record_observation(
        observation,
        descriptors=(descriptor,),
        unit_catalog=(),
        idempotency_key="same-poll-snapshot",
    )


def _record_usage(service, descriptor, *, job_id="job-1099"):
    return service.record_terminal_usage(
        {
            "id": job_id,
            "executor": "codex",
            "started_at": _iso_utc_ms(_NOW + 10),
            "finished_at": _iso_utc_ms(_NOW + 20),
            "usage": {"input_tokens": 2},
        },
        profile_key=_PROFILE_A,
        binding=_binding((descriptor,), _PROFILE_A),
        descriptors=(descriptor,),
        unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 30,
    )


def _remaining(service, descriptor):
    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 40
    )
    return _pool_row(report, "pool-shared", "short")


def test_same_value_snapshot_refines_unknown_window_and_enables_terminal_usage(quota_service):
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    unknown_window = _snapshot(descriptor)
    known_window = _snapshot(descriptor, reset_at_ms=_NOW + 300_000)

    assert _record_snapshot(quota_service, descriptor, unknown_window).accepted == 1
    refinement = _record_snapshot(quota_service, descriptor, known_window)
    assert _record_usage(quota_service, descriptor).accepted == 1

    row = _remaining(quota_service, descriptor)
    assert row["remaining"] == {
        "state": "observed",
        "amount": {"kind": "exact", "value": "18"},
    }
    assert "window-epoch-unknown" not in row["coverage_gaps"]
    assert refinement.status == "accepted"


def test_same_timestamp_different_snapshot_value_remains_a_conflict(quota_service):
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    assert _record_snapshot(quota_service, descriptor, _snapshot(descriptor)).accepted == 1

    conflict = _record_snapshot(
        quota_service,
        descriptor,
        _snapshot(descriptor, value="19", reset_at_ms=_NOW + 300_000),
    )

    assert conflict.status == "conflict"
    assert conflict.conflicts == 1


def test_later_snapshot_without_window_does_not_replace_known_window(quota_service):
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    known_window = _snapshot(descriptor, reset_at_ms=_NOW + 300_000)
    unknown_window = _snapshot(descriptor)
    assert _record_snapshot(quota_service, descriptor, known_window).accepted == 1

    later_incomplete = _record_snapshot(quota_service, descriptor, unknown_window)
    assert _record_usage(quota_service, descriptor).accepted == 1

    assert later_incomplete.status == "duplicate"
    assert _remaining(quota_service, descriptor)["remaining"] == {
        "state": "observed",
        "amount": {"kind": "exact", "value": "18"},
    }


def test_identical_snapshot_and_terminal_usage_replays_do_not_double_deduct(quota_service):
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    snapshot = _snapshot(descriptor, reset_at_ms=_NOW + 300_000)
    assert _record_snapshot(quota_service, descriptor, snapshot).accepted == 1

    replayed_snapshot = _record_snapshot(quota_service, descriptor, snapshot)
    first_usage = _record_usage(quota_service, descriptor)
    replayed_usage = _record_usage(quota_service, descriptor)

    assert replayed_snapshot.status == "duplicate"
    assert first_usage.accepted == 1
    assert replayed_usage.status == "duplicate"
    assert _remaining(quota_service, descriptor)["remaining"] == {
        "state": "observed",
        "amount": {"kind": "exact", "value": "18"},
    }
