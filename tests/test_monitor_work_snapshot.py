from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from paulsha_cortex.config.paths import monitor_state_root, work_items_snapshot_path
from paulsha_cortex.monitor.work_models import ProviderSnapshot, WorkItem, WorkSource
from paulsha_cortex.monitor.work_snapshot import (
    SNAPSHOT_SCHEMA,
    SnapshotValidationError,
    WorkSnapshot,
    WorkSnapshotStore,
)


NOW = "2026-07-17T10:00:00Z"


def _source(revision="rev-1"):
    return WorkSource(
        source_id="github_issue:example/acme#14",
        kind="github_issue",
        ref="example/acme#14",
        revision=revision,
        status="open",
        confidence="confirmed",
        provider="github:example/acme",
    )


def _provider(*, status="ok", revision="rev-1", diagnostics=()):
    return ProviderSnapshot(
        provider_id="github:example/acme",
        status=status,
        last_attempt_at=NOW,
        last_success_at=NOW if status == "ok" else None,
        revision=revision if status == "ok" else None,
        diagnostics=diagnostics,
        sources=(_source(revision),) if status == "ok" else (),
    )


def _item(*, facets=()):
    return WorkItem(
        work_id="unified-work-lifecycle",
        repo="example/acme",
        title="Unified lifecycle",
        state="todo",
        phase=None,
        facets=facets,
        sources=(_source(),),
        next_actions=("start",),
        workflow_run_id=None,
        updated_at=NOW,
    )


def _snapshot(provider=None):
    provider = provider or _provider()
    return WorkSnapshot(
        sequence=1,
        written_at=NOW,
        providers={provider.provider_id: provider},
        work_items=(_item(),),
        source_owners={_source().source_id: "unified-work-lifecycle"},
        exclusions=(),
    )


def test_default_and_overridden_snapshot_path(monkeypatch, tmp_path):
    monkeypatch.delenv("PSC_MONITOR_STATE_ROOT", raising=False)
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(tmp_path / "agents"))
    assert monitor_state_root() == tmp_path / "agents" / "monitor"
    assert work_items_snapshot_path() == (
        tmp_path / "agents" / "monitor" / "work-items.snapshot.json"
    )

    monkeypatch.setenv("PSC_MONITOR_STATE_ROOT", str(tmp_path / "override"))
    assert work_items_snapshot_path() == tmp_path / "override" / "work-items.snapshot.json"


def test_round_trip_writes_schema_0600_and_canonical_json(tmp_path):
    path = tmp_path / "state" / "work-items.snapshot.json"
    store = WorkSnapshotStore(path)

    store.write(_snapshot())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == SNAPSHOT_SCHEMA == "work-items-snapshot/v1"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load() == _snapshot()


def test_atomic_replace_failure_preserves_previous_snapshot(monkeypatch, tmp_path):
    path = tmp_path / "work-items.snapshot.json"
    store = WorkSnapshotStore(path)
    previous = _snapshot()
    store.write(previous)
    original = path.read_bytes()

    def explode(_source, _target):
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError, match="simulated crash"):
        store.write(replace(previous, sequence=2))

    assert path.read_bytes() == original
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_fsyncs_file_and_parent_directory(monkeypatch, tmp_path):
    calls: list[int] = []
    real_fsync = os.fsync

    def record(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    WorkSnapshotStore(tmp_path / "work-items.snapshot.json").write(_snapshot())
    assert len(calls) >= 2


def test_unknown_schema_and_ownership_collision_fail_before_overwrite(tmp_path):
    path = tmp_path / "work-items.snapshot.json"
    store = WorkSnapshotStore(path)
    store.write(_snapshot())
    original = path.read_bytes()

    invalid_schema = _snapshot().to_dict()
    invalid_schema["schema"] = "work-items-snapshot/v999"
    with pytest.raises(SnapshotValidationError, match="schema"):
        store.write_payload(invalid_schema)
    assert path.read_bytes() == original

    collision = _snapshot().to_dict()
    collision["source_owners"][_source().source_id] = ["one", "two"]
    with pytest.raises(SnapshotValidationError, match="ownership"):
        store.write_payload(collision)
    assert path.read_bytes() == original


def test_malformed_existing_snapshot_fails_closed(tmp_path):
    path = tmp_path / "work-items.snapshot.json"
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(SnapshotValidationError, match="parse"):
        WorkSnapshotStore(path).load()


def test_failed_provider_retains_last_good_sources_and_revision(tmp_path):
    store = WorkSnapshotStore(tmp_path / "work-items.snapshot.json")
    store.write(_snapshot())
    failed = ProviderSnapshot(
        provider_id="github:example/acme",
        status="degraded",
        last_attempt_at="2026-07-17T10:05:00Z",
        last_success_at=None,
        revision=None,
        diagnostics=("github timeout",),
        sources=(),
    )

    updated = store.record_provider_result(failed, work_items=())

    provider = updated.providers["github:example/acme"]
    assert provider.status == "degraded"
    assert provider.last_attempt_at == "2026-07-17T10:05:00Z"
    assert provider.last_success_at == NOW
    assert provider.revision == "rev-1"
    assert provider.sources == (_source(),)
    assert provider.diagnostics == ("github timeout",)
    assert updated.work_items == _snapshot().work_items


def test_first_degraded_result_does_not_promote_candidate_sources(tmp_path):
    store = WorkSnapshotStore(tmp_path / "work-items.snapshot.json")
    failed = ProviderSnapshot(
        provider_id="repo:example/acme",
        status="degraded",
        last_attempt_at=NOW,
        last_success_at=None,
        revision=None,
        diagnostics=("active/archive collision: duplicate",),
        sources=(
            replace(
                _source(),
                provider="repo:example/acme",
                source_id="openspec:example/acme:duplicate",
                kind="openspec",
                ref="duplicate",
                status="active",
            ),
        ),
    )

    updated = store.record_provider_result(failed, work_items=())

    persisted = updated.providers["repo:example/acme"]
    assert persisted.status == "degraded"
    assert persisted.sources == ()
    assert persisted.revision is None
    assert persisted.last_success_at is None


def test_successful_provider_replaces_sources_and_advances_sequence(tmp_path):
    store = WorkSnapshotStore(tmp_path / "work-items.snapshot.json")
    store.write(_snapshot())
    success = ProviderSnapshot(
        provider_id="github:example/acme",
        status="ok",
        last_attempt_at="2026-07-17T10:05:00Z",
        last_success_at="2026-07-17T10:05:00Z",
        revision="rev-2",
        diagnostics=(),
        sources=(_source("rev-2"),),
    )

    updated = store.record_provider_result(success, work_items=(_item(),))

    assert updated.sequence == 2
    assert updated.providers[success.provider_id] == success
    assert updated.providers[success.provider_id].sources == (_source("rev-2"),)


def test_restart_bootstrap_preserves_items_and_marks_degraded(tmp_path):
    store = WorkSnapshotStore(tmp_path / "work-items.snapshot.json")
    store.write(_snapshot())

    bootstrapped = store.load_for_bootstrap(at="2026-07-17T10:06:00Z")

    assert bootstrapped is not None
    assert bootstrapped.work_items[0].state == "todo"
    assert bootstrapped.work_items[0].facets == ("degraded",)
    provider = bootstrapped.providers["github:example/acme"]
    assert provider.status == "degraded"
    assert provider.sources == (_source(),)
    assert provider.last_success_at == NOW
    assert any("awaiting live refresh" in note for note in provider.diagnostics)


def test_github_freshness_gate_keeps_read_data():
    provider = _provider()
    snapshot = _snapshot(provider)
    stale_at = datetime(2026, 7, 17, 10, 15, 1, tzinfo=timezone.utc)

    assert not snapshot.provider_is_fresh(provider.provider_id, now=stale_at, max_age=900)
    assert snapshot.providers[provider.provider_id].sources == (_source(),)

    fresh_at = datetime.fromisoformat(NOW.replace("Z", "+00:00")) + timedelta(seconds=900)
    assert snapshot.provider_is_fresh(provider.provider_id, now=fresh_at, max_age=900)
