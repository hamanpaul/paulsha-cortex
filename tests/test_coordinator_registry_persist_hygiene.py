from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import registry as registry_module
from paulsha_cortex.coordinator.registry import JobRegistry


def _registry_with_slice(tmp_path: Path, *, history_limit: int = 500) -> JobRegistry:
    registry = JobRegistry(
        state_path=tmp_path / "jobs.json",
        history_limit=history_limit,
    )
    registry.create_slice(
        slice_id="slice-a",
        spec_path="specs/slice-a.md",
        spec_hash="spec-hash",
        plan_path="plans/slice-a.md",
        plan_hash="plan-hash",
        target_branch="feature/slice-a",
    )
    return registry


def _record_evidence(registry: JobRegistry, index: int) -> dict[str, object]:
    return registry.record_action(
        "slice-a",
        action="verification-failed",
        actor="manager",
        evidence_refs=[f"evidence/{index}.json"],
    )


def test_persist_skips_identical_bytes_without_creating_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    job = registry.create_job(
        task="baseline",
        persona="builder",
        branch="feature/baseline",
        pane="%0",
        worktree=str(tmp_path / "worktree"),
    )
    before = state_path.stat()
    real_mkstemp = registry_module.tempfile.mkstemp
    mkstemp_calls = 0

    def count_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        nonlocal mkstemp_calls
        mkstemp_calls += 1
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(registry_module.tempfile, "mkstemp", count_mkstemp)
    registry.update_status(str(job["job_id"]), str(job["status"]))
    for _ in range(3):
        registry._persist()

    after = state_path.stat()
    assert mkstemp_calls == 0
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)
    assert list(tmp_path.glob("tmp*.tmp")) == []
    assert list(tmp_path.glob("tmp*.rollback.bak")) == []


def test_rollback_snapshot_uses_hardlink_and_restores_after_replace_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    registry._persist()
    before = state_path.read_bytes()
    real_link = registry_module.os.link
    link_calls: list[tuple[Path, Path]] = []

    def count_link(source: str | os.PathLike[str], destination: str | os.PathLike[str], **kwargs: object) -> None:
        link_calls.append((Path(source), Path(destination)))
        real_link(source, destination, **kwargs)

    real_fsync_directory = registry_module._fsync_directory
    fsync_calls = 0

    def fail_once(directory: Path) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            raise OSError("post-replace directory sync failed")
        real_fsync_directory(directory)

    monkeypatch.setattr(registry_module.os, "link", count_link)
    monkeypatch.setattr(registry_module, "_fsync_directory", fail_once)
    registry._seq += 1

    with pytest.raises(OSError, match="post-replace directory sync failed"):
        registry._persist()

    assert len(link_calls) == 1
    assert link_calls[0][0] == state_path
    assert state_path.read_bytes() == before
    assert registry._seq == 0
    assert list(tmp_path.glob("tmp*.tmp")) == []
    assert list(tmp_path.glob("tmp*.rollback.bak")) == []


@pytest.mark.parametrize("link_error", [errno.EPERM, errno.EXDEV])
def test_hardlink_error_falls_back_to_copy_and_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_error: int,
) -> None:
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    registry._persist()
    link_calls = 0

    def reject_link(*args: object, **kwargs: object) -> None:
        nonlocal link_calls
        link_calls += 1
        raise OSError(link_error, "hard links unavailable")

    monkeypatch.setattr(registry_module.os, "link", reject_link)
    registry._seq += 1
    registry._persist()

    assert link_calls == 1
    assert json.loads(state_path.read_bytes())["seq"] == 1
    assert list(tmp_path.glob("tmp*.tmp")) == []
    assert list(tmp_path.glob("tmp*.rollback.bak")) == []


def test_hardlink_fallback_copy_can_restore_after_replace_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    registry._persist()
    before = state_path.read_bytes()

    def reject_link(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "hard links unavailable")

    real_fsync_directory = registry_module._fsync_directory
    fsync_calls = 0

    def fail_once(directory: Path) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            raise OSError("post-replace directory sync failed")
        real_fsync_directory(directory)

    monkeypatch.setattr(registry_module.os, "link", reject_link)
    monkeypatch.setattr(registry_module, "_fsync_directory", fail_once)
    registry._seq += 1

    with pytest.raises(OSError, match="post-replace directory sync failed"):
        registry._persist()

    assert state_path.read_bytes() == before
    assert registry._seq == 0
    assert list(tmp_path.glob("tmp*.tmp")) == []
    assert list(tmp_path.glob("tmp*.rollback.bak")) == []


def test_record_action_bounds_evidence_evaluation_and_action_history(
    tmp_path: Path,
) -> None:
    registry = _registry_with_slice(tmp_path, history_limit=5)
    for index in range(12):
        _record_evidence(registry, index)
    row = registry.get_slice("slice-a")

    assert len(row["evidence_history"]) == 5
    assert row["evidence_history"][0]["refs"] == ["evidence/0.json"]
    assert row["evidence_history"][-1]["refs"] == ["evidence/11.json"]
    assert len(row["actions"]) == 5
    assert row["actions"][0]["action"] == "verification-failed"
    assert row["history_truncated"] == {"evidence_history": 7, "actions": 7}
    row["history_truncated"]["actions"] = 999
    assert registry.get_slice("slice-a")["history_truncated"]["actions"] == 7

    registry.create_slice(
        slice_id="slice-eval",
        spec_path="specs/slice-eval.md",
        spec_hash="spec-hash",
        plan_path="plans/slice-eval.md",
        plan_hash="plan-hash",
        target_branch="feature/slice-eval",
    )
    for index in range(12):
        registry.record_action(
            "slice-eval",
            action="evaluation-failed",
            actor="manager",
            evaluation_refs=[f"evaluation/{index}.json"],
        )
    evaluation = registry.get_slice("slice-eval")
    assert len(evaluation["evaluation_history"]) == 5
    assert evaluation["evaluation_history"][0]["refs"] == ["evaluation/0.json"]
    assert evaluation["evaluation_history"][-1]["refs"] == ["evaluation/11.json"]
    assert evaluation["history_truncated"] == {"evaluation_history": 7, "actions": 7}


@pytest.mark.parametrize(
    ("limit", "expected_refs", "dropped"),
    [(1, ["evidence/11.json"], 11), (2, ["evidence/0.json", "evidence/11.json"], 10)],
)
def test_record_action_history_limit_one_and_two(
    tmp_path: Path,
    limit: int,
    expected_refs: list[str],
    dropped: int,
) -> None:
    registry = _registry_with_slice(tmp_path, history_limit=limit)
    for index in range(12):
        _record_evidence(registry, index)
    row = registry.get_slice("slice-a")

    assert [entry["refs"][0] for entry in row["evidence_history"]] == expected_refs
    assert row["history_truncated"]["evidence_history"] == dropped
    assert row["history_truncated"]["actions"] == dropped


def test_loading_over_limit_history_trims_once_and_keeps_truncation_count(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "jobs.json"
    registry = _registry_with_slice(tmp_path)
    for index in range(8):
        _record_evidence(registry, index)
    old_mtime_ns = 1_600_000_000_000_000_000
    os.utime(state_path, ns=(old_mtime_ns, old_mtime_ns))

    bounded = JobRegistry(state_path=state_path, history_limit=5)
    row = bounded.get_slice("slice-a")
    assert len(row["evidence_history"]) == 5
    assert row["evidence_history"][0]["refs"] == ["evidence/0.json"]
    assert row["evidence_history"][-1]["refs"] == ["evidence/7.json"]
    assert row["history_truncated"] == {"evidence_history": 3, "actions": 3}
    assert state_path.stat().st_mtime_ns > old_mtime_ns

    stable_bytes = state_path.read_bytes()
    stable_mtime_ns = state_path.stat().st_mtime_ns
    JobRegistry(state_path=state_path, history_limit=5)
    assert state_path.read_bytes() == stable_bytes
    assert state_path.stat().st_mtime_ns == stable_mtime_ns

    _record_evidence(bounded, 8)
    assert bounded.get_slice("slice-a")["history_truncated"] == {
        "evidence_history": 4,
        "actions": 4,
    }


@pytest.mark.parametrize(
    "counter",
    [[], {"unknown": 1}, {"actions": True}, {"actions": -1}],
)
def test_loading_rejects_invalid_history_truncated_counter(
    tmp_path: Path,
    counter: object,
) -> None:
    state_path = tmp_path / "jobs.json"
    registry = _registry_with_slice(tmp_path)
    payload = json.loads(state_path.read_bytes())
    payload["slices"][0]["history_truncated"] = counter
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="history_truncated"):
        JobRegistry(state_path=state_path)


def test_loading_legacy_slice_without_history_truncated_is_compatible(
    tmp_path: Path,
) -> None:
    registry = _registry_with_slice(tmp_path)
    row_before = registry.get_slice("slice-a")
    assert "history_truncated" not in row_before

    loaded = JobRegistry(state_path=tmp_path / "jobs.json")
    assert "history_truncated" not in loaded.get_slice("slice-a")


def test_environment_history_limit_is_used_when_constructor_value_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PSC_COORDINATOR_SLICE_HISTORY_LIMIT", "2")
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    registry.create_slice(
        slice_id="slice-a",
        spec_path="specs/slice-a.md",
        spec_hash="spec-hash",
        plan_path="plans/slice-a.md",
        plan_hash="plan-hash",
        target_branch="feature/slice-a",
    )
    for index in range(3):
        _record_evidence(registry, index)
    row = registry.get_slice("slice-a")
    assert [entry["refs"][0] for entry in row["evidence_history"]] == [
        "evidence/0.json",
        "evidence/2.json",
    ]
    assert row["history_truncated"] == {"evidence_history": 1, "actions": 1}


def test_constructor_history_limit_overrides_environment_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PSC_COORDINATOR_SLICE_HISTORY_LIMIT", "invalid")
    registry = _registry_with_slice(tmp_path, history_limit=1)
    assert registry._history_limit == 1


@pytest.mark.parametrize("value", ["", "0", "-1", "invalid"])
def test_invalid_environment_history_limit_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("PSC_COORDINATOR_SLICE_HISTORY_LIMIT", value)
    with pytest.raises(ValueError, match="PSC_COORDINATOR_SLICE_HISTORY_LIMIT"):
        JobRegistry(state_path=tmp_path / "jobs.json")


def test_startup_sweeps_only_old_registry_temp_files(tmp_path: Path) -> None:
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    registry._persist()
    original_state = state_path.read_bytes()
    old_time = time.time() - 120

    stale_tmp = tmp_path / "tmpAAAA.tmp"
    stale_rollback = tmp_path / "tmpBBBB.rollback.bak"
    stale_backup_tmp = tmp_path / "tmpCCCC.backup.tmp"
    recent_tmp = tmp_path / "tmpRecent.tmp"
    manual_backup = tmp_path / "jobs.json.bak-manual"
    v1_backup = tmp_path / "jobs.json.v1.20260101T000000000000Z.deadbeef.bak"
    temp_directory = tmp_path / "tmpDirectory.tmp"
    for path in (stale_tmp, stale_rollback, stale_backup_tmp, recent_tmp, manual_backup, v1_backup):
        path.write_text("temporary", encoding="utf-8")
    for path in (stale_tmp, stale_rollback, stale_backup_tmp, manual_backup, v1_backup):
        os.utime(path, (old_time, old_time))
    recent_time = time.time()
    os.utime(recent_tmp, (recent_time, recent_time))
    temp_directory.mkdir()
    symlink = tmp_path / "tmpSymlink.tmp"
    symlink.symlink_to(state_path)

    JobRegistry(state_path=state_path)

    assert not stale_tmp.exists()
    assert not stale_rollback.exists()
    assert not stale_backup_tmp.exists()
    assert recent_tmp.exists()
    assert manual_backup.exists()
    assert v1_backup.exists()
    assert temp_directory.is_dir()
    assert symlink.is_symlink()
    assert state_path.read_bytes() == original_state


def test_startup_sweep_rejects_short_grace_and_preserves_younger_files(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "jobs.json"
    with pytest.raises(ValueError, match="sweep_grace_seconds"):
        JobRegistry(state_path=state_path, sweep_grace_seconds=10)

    tmp_path.mkdir(exist_ok=True)
    recent_for_long_grace = tmp_path / "tmpGrace.tmp"
    recent_for_long_grace.write_text("temporary", encoding="utf-8")
    os.utime(recent_for_long_grace, (time.time() - 120, time.time() - 120))
    JobRegistry(state_path=state_path, sweep_grace_seconds=600)
    assert recent_for_long_grace.exists()


def test_startup_sweep_does_not_create_missing_state_directory(tmp_path: Path) -> None:
    missing_directory = tmp_path / "missing"
    JobRegistry(state_path=missing_directory / "jobs.json")
    assert not missing_directory.exists()
