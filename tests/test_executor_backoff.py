"""#850 durable executor backoff store coverage."""

from __future__ import annotations

import json
import multiprocessing
import os
import hashlib
import threading
from pathlib import Path

import pytest


def _obs(value) -> str:
    return value if isinstance(value, str) else value.value


def _backoff_payload(backoff) -> dict[str, object] | None:
    if backoff is None:
        return None
    outcome = backoff.outcome
    return {
        "executor": backoff.executor,
        "model_id": backoff.model_id,
        "deadline_epoch": backoff.deadline_epoch,
        "consecutive_hits": backoff.consecutive_hits,
        "last_event_epoch": backoff.last_event_epoch,
        "last_terminal_key": backoff.last_terminal_key,
        "outcome": outcome if isinstance(outcome, str) else outcome.value,
        "reason": backoff.reason,
        "event_count": backoff.event_count,
    }


def _status_payload(result) -> dict[str, object]:
    return {
        "observation": _obs(result.observation),
        "diagnostics": tuple(result.diagnostics),
        "reconciliation": _obs(result.reconciliation),
        "missing_terminal_keys": tuple(getattr(result, "missing_terminal_keys", ())),
        "conflicting_terminal_keys": tuple(
            getattr(result, "conflicting_terminal_keys", ())
        ),
        "evidence_refs": tuple(getattr(result, "evidence_refs", ())),
        "changed": getattr(result, "changed", None),
        "backoff": _backoff_payload(getattr(result, "backoff", None)),
        "last_good": _backoff_payload(getattr(result, "last_good", None)),
    }


def _apply_overrides(module, overrides: dict[str, object] | None) -> None:
    if overrides is None:
        return
    for name, value in overrides.items():
        setattr(module, name, value)


def _outcome_input(
    outcome: object,
    reason: str,
    reset_at: float | None,
    *,
    authority: str = "structured",
    reset_provenance: str | None = None,
    evidence_ref: str = "fixture://terminal",
    payload: object | None = None,
    reset_parser: dict[str, object] | None = None,
    policy_revision: str = "executor-backoff/v1",
    rate_limited_base_seconds: float = 10.0,
    quota_base_seconds: float = 40.0,
    backoff_multiplier_base: float = 2.0,
    backoff_max_exponent: int = 4,
    reset_margin_seconds: float = 5.0,
) -> dict[str, object]:
    normalized_outcome = outcome if isinstance(outcome, str) else outcome.value
    if reset_provenance is None:
        reset_provenance = "structured" if reset_at is not None else "absent"
    expected_retryable = normalized_outcome == "rate_limited"
    if payload is None:
        payload = {
            "outcome": normalized_outcome,
            "authority": authority,
            "reason": reason,
            "retryable": expected_retryable,
        }
        if reset_at is not None:
            payload["reset_at"] = reset_at
    else:
        payload = dict(payload)
        payload.setdefault("outcome", normalized_outcome)
        payload.setdefault("authority", authority)
        payload.setdefault("reason", reason)
        payload.setdefault("retryable", expected_retryable)
        if reset_at is not None:
            payload.setdefault("reset_at", reset_at)
    payload_bytes = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "outcome": normalized_outcome,
        "authority": authority,
        "payload": payload,
        "payload_fingerprint": hashlib.sha256(payload_bytes).hexdigest(),
        "reason": reason,
        "policy_revision": policy_revision,
        "rate_limited_base_seconds": rate_limited_base_seconds,
        "quota_base_seconds": quota_base_seconds,
        "backoff_multiplier_base": backoff_multiplier_base,
        "backoff_max_exponent": backoff_max_exponent,
        "reset_margin_seconds": reset_margin_seconds,
        "reset_provenance": reset_provenance,
        "reset_parser": reset_parser,
        "evidence_ref": evidence_ref,
    }


def _spawn_record(
    root: str,
    queue,
    *,
    executor: str = "copilot",
    model_id: str = "gpt-5",
    now: float = 100.0,
    outcome: str = "rate_limited",
    reset_at: float | None = None,
    reason: str = "secondary-rate-limit",
    job_id: str = "job-1",
    event_epoch: float = 100.0,
    overrides: dict[str, object] | None = None,
    hook_stage: str | None = None,
    stage_ready=None,
    release=None,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _apply_overrides(executor_backoff, overrides)

    if hook_stage is not None:
        def hook(stage: str) -> None:
            if stage != hook_stage:
                return
            if stage_ready is not None:
                stage_ready.set()
            if release is not None and not release.wait(10):
                raise TimeoutError(f"timed out waiting to release hook {hook_stage}")

        executor_backoff._TEST_HOOK = hook

    try:
        result = executor_backoff.record_backoff(
            root,
            executor,
            model_id,
            now=now,
            outcome=outcome
            if isinstance(outcome, dict)
            else _outcome_input(outcome, reason, reset_at),
            reset_at=reset_at,
            reason=reason,
            job_id=job_id,
            event_epoch=event_epoch,
        )
        queue.put({"result": _status_payload(result)})
    except BaseException as error:  # pragma: no cover - child process path
        queue.put({"error": f"{type(error).__name__}: {error}"})
        raise
    finally:
        executor_backoff._TEST_HOOK = None


def _spawn_active(
    root: str,
    queue,
    *,
    executor: str = "copilot",
    model_id: str = "gpt-5",
    now: float = 100.0,
    overrides: dict[str, object] | None = None,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _apply_overrides(executor_backoff, overrides)
    result = executor_backoff.active_backoff(root, executor, model_id, now=now)
    queue.put(_status_payload(result))


def _inventory_event(
    *,
    executor: str,
    model_id: str,
    job_id: str,
    event_epoch: float,
    outcome: object = "rate_limited",
    reset_at: float | None = None,
    reason: str,
) -> dict[str, object]:
    return {
        "executor": executor,
        "model_id": model_id,
        "job_id": job_id,
        "event_epoch": event_epoch,
        "outcome": _outcome_input(outcome, reason, reset_at),
        "reset_at": reset_at,
        "reason": reason,
    }


def _inventory_payload(
    *events: dict[str, object],
    complete: bool = True,
    readable: bool = True,
    evidence_ref: str = "fixture://inventory",
) -> dict[str, object]:
    return {
        "complete": complete,
        "readable": readable,
        "events": list(events),
        "evidence_ref": evidence_ref,
    }


def _spawn_reconcile(
    root: str,
    queue,
    *,
    executor: str = "copilot",
    model_id: str = "gpt-5",
    now: float = 100.0,
    inventory: dict[str, object] | None = None,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    result = executor_backoff.reconcile_backoff(
        root,
        executor,
        model_id,
        now=now,
        inventory=inventory,
    )
    queue.put(_status_payload(result))


def _spawn_read_store(root: str, queue) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    result = executor_backoff.read_store(Path(root) / executor_backoff.STATE_FILENAME)
    queue.put(
        {
            "observation": _obs(result.observation),
            "diagnostics": tuple(result.diagnostics),
            "payload": result.payload,
            "last_good": result.last_good,
        }
    )


def _spawn_hold_lock(root: str, ready, release) -> None:
    from pathlib import Path

    from paulsha_cortex.coordinator import executor_backoff

    with executor_backoff._root_lock(Path(root), exclusive=True):
        ready.set()
        if not release.wait(10):  # pragma: no cover - failure path
            raise TimeoutError("timed out waiting to release lock holder")


def _record(root: Path, **kwargs):
    from paulsha_cortex.coordinator import executor_backoff

    defaults = {
        "now": 100.0,
        "executor": "copilot",
        "model_id": "gpt-5",
        "outcome": "rate_limited",
        "reset_at": None,
        "reason": "secondary-rate-limit",
        "job_id": "job-1",
        "event_epoch": 100.0,
    }
    defaults.update(kwargs)
    return executor_backoff.record_backoff(
        root,
        defaults["executor"],
        defaults["model_id"],
        now=defaults["now"],
        outcome=defaults["outcome"]
        if isinstance(defaults["outcome"], dict)
        else _outcome_input(
            defaults["outcome"],
            defaults["reason"],
            defaults["reset_at"],
        ),
        reset_at=defaults["reset_at"],
        reason=defaults["reason"],
        job_id=defaults["job_id"],
        event_epoch=defaults["event_epoch"],
    )


def _active(root: Path, *, now: float, executor: str = "copilot", model_id: str = "gpt-5"):
    from paulsha_cortex.coordinator import executor_backoff

    return executor_backoff.active_backoff(root, executor, model_id, now=now)


def _read(root: Path):
    from paulsha_cortex.coordinator import executor_backoff

    return executor_backoff.read_store(root / executor_backoff.STATE_FILENAME)


def _state_path(root: Path) -> Path:
    from paulsha_cortex.coordinator import executor_backoff

    return root / executor_backoff.STATE_FILENAME


def _state_bytes(root: Path) -> bytes:
    return _state_path(root).read_bytes()


def _state_payload(root: Path) -> dict[str, object]:
    return json.loads(_state_path(root).read_text(encoding="utf-8"))


def _ctx():
    return multiprocessing.get_context("spawn")


def test_read_store_classifies_missing_valid_unknown_and_unknown_schema(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    wrong_name = executor_backoff.read_store(tmp_path / "not-the-store.json")
    assert _obs(wrong_name.observation) == "unknown"
    assert "invalid-store-path" in wrong_name.diagnostics

    missing = executor_backoff.read_store(tmp_path / executor_backoff.STATE_FILENAME)
    assert _obs(missing.observation) == "missing"
    assert missing.payload is None
    assert missing.last_good is None

    valid_record = _record(
        tmp_path,
        reset_at=1000.0,
        reason="structured-reset",
        job_id="job-valid",
        event_epoch=100.0,
    )
    assert _obs(valid_record.observation) == "valid"

    valid = executor_backoff.read_store(tmp_path / executor_backoff.STATE_FILENAME)
    assert _obs(valid.observation) == "valid"
    assert valid.payload == _state_payload(tmp_path)
    assert valid.diagnostics == ()

    (tmp_path / executor_backoff.STATE_FILENAME).write_text("{not-json", encoding="utf-8")
    invalid_json = executor_backoff.read_store(tmp_path / executor_backoff.STATE_FILENAME)
    assert _obs(invalid_json.observation) == "unknown"
    assert invalid_json.payload is None
    assert invalid_json.diagnostics

    unknown_schema_payload = {
        "schema": "executor-backoff/v2",
        "capacity_profile": executor_backoff.CAPACITY_PROFILE,
        "scope": str(tmp_path.resolve()),
        "entries": {},
        "events": {},
        "acks": {},
    }
    (tmp_path / executor_backoff.STATE_FILENAME).write_text(
        json.dumps(unknown_schema_payload),
        encoding="utf-8",
    )
    unknown_schema = executor_backoff.read_store(tmp_path / executor_backoff.STATE_FILENAME)
    assert _obs(unknown_schema.observation) == "unknown"
    assert "unknown-schema:executor-backoff/v2" in unknown_schema.diagnostics


def test_read_store_rejects_non_regular_invalid_utf8_and_deep_json(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    state_path = tmp_path / executor_backoff.STATE_FILENAME
    state_path.write_bytes(b"\xff\xfe")
    invalid_utf8 = executor_backoff.read_store(state_path)
    assert _obs(invalid_utf8.observation) == "unknown"
    assert invalid_utf8.diagnostics

    state_path.unlink()
    os.mkfifo(state_path)
    non_regular = executor_backoff.read_store(state_path)
    assert _obs(non_regular.observation) == "unknown"
    assert "regular file" in non_regular.diagnostics[0]

    state_path.unlink()
    nested = "[" * (executor_backoff.MAX_JSON_DEPTH + 1) + "]" * (executor_backoff.MAX_JSON_DEPTH + 1)
    state_path.write_text(nested, encoding="utf-8")
    too_deep = executor_backoff.read_store(state_path)
    assert _obs(too_deep.observation) == "unknown"
    assert "json-depth-exceeded" in too_deep.diagnostics


def test_read_store_rejects_growing_or_oversized_files_before_decode(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    state_path = tmp_path / executor_backoff.STATE_FILENAME
    state_path.write_bytes(b"{" + b"x" * executor_backoff.MAX_STORE_BYTES)

    observed = executor_backoff.read_store(state_path)
    assert _obs(observed.observation) == "unknown"
    assert "store-too-large" in observed.diagnostics


def test_read_store_rejects_node_limit_exceeded_payload(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="baseline",
        now=0.0,
    )
    monkeypatch.setattr(executor_backoff, "MAX_STRUCTURAL_NODES", 5)
    observed = executor_backoff.read_store(_state_path(tmp_path))
    assert _obs(observed.observation) == "unknown"
    assert "json-node-limit-exceeded" in observed.diagnostics


def test_read_store_rejects_growing_file_even_if_growth_is_whitespace(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="baseline",
        now=0.0,
    )
    state_path = _state_path(tmp_path)
    original_read = executor_backoff.os.read
    mutated = False

    def growing_read(fd: int, count: int) -> bytes:
        nonlocal mutated
        chunk = original_read(fd, count)
        if not mutated and chunk:
            mutated = True
            with state_path.open("ab") as handle:
                handle.write(b" ")
        return chunk

    monkeypatch.setattr(executor_backoff.os, "read", growing_read)
    observed = executor_backoff.read_store(state_path)
    assert _obs(observed.observation) == "unknown"
    assert "store-changed-during-read" in observed.diagnostics


def test_read_store_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    state_path = tmp_path / executor_backoff.STATE_FILENAME
    state_path.write_text(
        '{"schema":"executor-backoff/v1","capacity_profile":"bounded-ledger/v1",'
        '"entries":{},"events":{"job-1":{},"job-1":{}},"acks":{}}',
        encoding="utf-8",
    )
    observed = executor_backoff.read_store(state_path)
    assert _obs(observed.observation) == "unknown"
    assert "duplicate-json-key:job-1" in observed.diagnostics


def test_read_store_invalid_unicode_fields_stay_unknown(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="baseline",
        now=0.0,
    )
    payload = _state_payload(tmp_path)
    payload["events"]["job-1"]["executor"] = "\ud800"
    _state_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    observed = executor_backoff.read_store(_state_path(tmp_path))
    assert _obs(observed.observation) == "unknown"
    assert "invalid-executor" in observed.diagnostics


def test_invalid_unicode_inside_payload_fails_closed_for_read_and_record(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    direct = _record(
        tmp_path / "direct",
        job_id="job-direct",
        event_epoch=100.0,
        reset_at=None,
        reason="payload-unicode",
        now=0.0,
        outcome={
            "outcome": "rate_limited",
            "authority": "structured",
            "payload": {
                "outcome": "rate_limited",
                "reason": "payload-unicode",
                "reset_at": None,
                "message": "\ud800",
            },
            "payload_fingerprint": "0" * 64,
            "reason": "payload-unicode",
            "policy_revision": "executor-backoff/v1",
            "rate_limited_base_seconds": 10.0,
            "quota_base_seconds": 40.0,
            "backoff_multiplier_base": 2.0,
            "backoff_max_exponent": 4,
            "reset_margin_seconds": 5.0,
            "reset_provenance": "absent",
            "reset_parser": None,
            "evidence_ref": "fixture://terminal",
        },
    )
    assert _obs(direct.observation) == "unknown"
    assert "invalid-payload" in direct.diagnostics

    _record(
        tmp_path / "read",
        job_id="job-read",
        event_epoch=100.0,
        reset_at=None,
        reason="payload-unicode",
        now=0.0,
    )
    payload = _state_payload(tmp_path / "read")
    payload["events"]["job-read"]["payload"]["message"] = "\ud800"
    _state_path(tmp_path / "read").write_text(json.dumps(payload), encoding="utf-8")

    observed = executor_backoff.read_store(_state_path(tmp_path / "read"))
    assert _obs(observed.observation) == "unknown"
    assert "invalid-payload" in observed.diagnostics


def test_hostile_scalar_subclasses_fail_closed(tmp_path: Path) -> None:
    class WeirdStr(str):
        def encode(self, *args, **kwargs):
            raise RuntimeError("hostile encode")

    class WeirdFloat(float):
        def __float__(self):
            raise RuntimeError("hostile float")

    payload = {
        "outcome": "rate_limited",
        "authority": "structured",
        "reason": WeirdStr("payload-subclass"),
        "retryable": True,
    }
    payload_bytes = json.dumps(
        {
            "outcome": "rate_limited",
            "authority": "structured",
            "reason": "payload-subclass",
            "retryable": True,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    hostile_payload = _record(
        tmp_path,
        job_id="job-hostile-payload",
        event_epoch=100.0,
        reset_at=None,
        reason="payload-subclass",
        now=0.0,
        outcome={
            "outcome": "rate_limited",
            "authority": "structured",
            "payload": payload,
            "payload_fingerprint": hashlib.sha256(payload_bytes).hexdigest(),
            "reason": "payload-subclass",
            "policy_revision": "executor-backoff/v1",
            "rate_limited_base_seconds": 10.0,
            "quota_base_seconds": 40.0,
            "backoff_multiplier_base": 2.0,
            "backoff_max_exponent": 4,
            "reset_margin_seconds": 5.0,
            "reset_provenance": "absent",
            "reset_parser": None,
            "evidence_ref": "fixture://terminal",
        },
    )
    assert _obs(hostile_payload.observation) == "unknown"
    assert "invalid-payload" in hostile_payload.diagnostics

    hostile_epoch = _record(
        tmp_path / "epoch",
        job_id="job-hostile-epoch",
        event_epoch=WeirdFloat(100.0),
        reset_at=None,
        reason="hostile-epoch",
        now=0.0,
    )
    assert _obs(hostile_epoch.observation) == "unknown"
    assert "invalid-event_epoch" in hostile_epoch.diagnostics


def test_read_store_returns_unknown_and_last_good_when_durability_recheck_fails(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        reset_at=1000.0,
        reason="structured-reset",
        job_id="job-1",
        event_epoch=100.0,
    )

    def failing_fsync_directory(path: Path) -> None:
        raise executor_backoff._StoreProblem("unable to fsync executor backoff store directory: denied")

    monkeypatch.setattr(executor_backoff, "_fsync_directory", failing_fsync_directory)
    observed = executor_backoff.read_store(_state_path(tmp_path))
    assert _obs(observed.observation) == "unknown"
    assert observed.last_good is not None
    assert observed.last_good["events"]["job-1"]["terminal_key"] == "job-1"

    active = executor_backoff.active_backoff(tmp_path, "copilot", "gpt-5", now=100.0)
    assert _obs(active.observation) == "unknown"
    assert active.last_good is not None
    assert active.last_good.deadline_epoch == 1005.0


def test_read_store_stat_and_read_faults_stay_unknown(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        reset_at=1000.0,
        reason="structured-reset",
        job_id="job-1",
        event_epoch=100.0,
    )
    state_path = _state_path(tmp_path)
    original_lstat = Path.lstat

    def failing_lstat(self: Path):
        if self == state_path:
            raise PermissionError("stat denied")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", failing_lstat)
    stat_fault = executor_backoff.read_store(state_path)
    assert _obs(stat_fault.observation) == "unknown"
    assert stat_fault.diagnostics

    monkeypatch.setattr(Path, "lstat", original_lstat)
    original_read = executor_backoff.os.read

    def failing_read(fd: int, count: int) -> bytes:
        raise PermissionError("read denied")

    monkeypatch.setattr(executor_backoff.os, "read", failing_read)
    read_fault = executor_backoff.read_store(state_path)
    assert _obs(read_fault.observation) == "unknown"
    assert read_fault.diagnostics
    monkeypatch.setattr(executor_backoff.os, "read", original_read)


def test_record_backoff_persists_schema_identity_and_exact_ack_ledger(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    recorded = _record(
        tmp_path,
        reset_at=1000.0,
        reason="structured-reset",
        job_id="job-1",
        event_epoch=100.0,
        now=100.0,
    )
    assert _obs(recorded.observation) == "valid"
    assert recorded.changed is True
    assert recorded.reconciliation is executor_backoff.ReconciliationStatus.UNVERIFIED
    assert recorded.backoff is not None
    assert recorded.backoff.deadline_epoch == 1005.0
    assert recorded.backoff.consecutive_hits == 1

    payload = _state_payload(tmp_path)
    assert payload["schema"] == executor_backoff.SCHEMA
    assert payload["capacity_profile"] == executor_backoff.CAPACITY_PROFILE
    assert payload["scope"] == str(tmp_path.resolve())
    assert set(payload["events"]) == {"job-1"}
    assert set(payload["acks"]) == {"job-1"}
    assert set(payload["entries"]) == {"copilot/gpt-5"}
    assert payload["events"]["job-1"]["fingerprint"] == payload["acks"]["job-1"]["fingerprint"]
    assert payload["events"]["job-1"]["authority"] == "structured"
    assert payload["events"]["job-1"]["payload"]["reason"] == "structured-reset"
    assert (
        payload["events"]["job-1"]["backoff_multiplier_base"]
        == executor_backoff.POLICY_BACKOFF_MULTIPLIER_BASE
    )
    assert (
        payload["events"]["job-1"]["backoff_max_exponent"]
        == executor_backoff.POLICY_BACKOFF_MAX_EXPONENT
    )
    assert payload["events"]["job-1"]["reset_provenance"] == "structured"

    fresh = _active(tmp_path, now=100.0)
    assert _obs(fresh.observation) == "valid"
    assert fresh.backoff is not None
    assert fresh.backoff.deadline_epoch == 1005.0
    assert fresh.reconciliation is executor_backoff.ReconciliationStatus.UNVERIFIED


def test_aggregate_mismatch_preserves_persisted_last_good_floor(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="first-hit",
        now=0.0,
    )
    payload = _state_payload(tmp_path)
    payload["entries"]["copilot/gpt-5"]["deadline_epoch"] = 999.0
    _state_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    observed = executor_backoff.active_backoff(tmp_path, "copilot", "gpt-5", now=0.0)
    assert _obs(observed.observation) == "unknown"
    assert "aggregate-mismatch" in observed.diagnostics
    assert observed.last_good is not None
    assert observed.last_good.deadline_epoch == 999.0

    store_view = executor_backoff.read_store(_state_path(tmp_path))
    assert _obs(store_view.observation) == "unknown"
    assert store_view.last_good is not None
    assert store_view.last_good["entries"]["copilot/gpt-5"]["deadline_epoch"] == 999.0


def test_aggregate_mismatch_keeps_conservative_floor_when_persisted_deadline_is_shorter(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="first-hit",
        now=0.0,
    )
    payload = _state_payload(tmp_path)
    payload["entries"]["copilot/gpt-5"]["deadline_epoch"] = 110.0
    _state_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    observed = executor_backoff.active_backoff(tmp_path, "copilot", "gpt-5", now=0.0)
    assert _obs(observed.observation) == "unknown"
    assert "aggregate-mismatch" in observed.diagnostics
    assert observed.last_good is not None
    assert observed.last_good.deadline_epoch == 120.0


def test_aggregate_mismatch_uses_recomputed_fields_with_persisted_floor(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="original",
        now=0.0,
    )
    payload = _state_payload(tmp_path)
    payload["entries"]["copilot/gpt-5"]["deadline_epoch"] = 999.0
    payload["entries"]["copilot/gpt-5"]["reason"] = "tampered"
    _state_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    observed = executor_backoff.active_backoff(tmp_path, "copilot", "gpt-5", now=0.0)
    assert _obs(observed.observation) == "unknown"
    assert observed.last_good is not None
    assert observed.last_good.deadline_epoch == 999.0
    assert observed.last_good.reason == "original"


def test_scope_mismatch_stays_unknown_across_different_roots(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _record(
        source_root,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="scope-source",
        now=0.0,
    )
    target_root.mkdir(parents=True, exist_ok=True)
    (_state_path(target_root)).write_bytes(_state_bytes(source_root))

    read_view = executor_backoff.read_store(_state_path(target_root))
    assert _obs(read_view.observation) == "unknown"
    assert "scope-mismatch" in read_view.diagnostics

    active = executor_backoff.active_backoff(target_root, "copilot", "gpt-5", now=0.0)
    assert _obs(active.observation) == "unknown"
    assert "scope-mismatch" in active.diagnostics


def test_reconcile_backoff_reports_complete_pending_and_unknown(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="stored-event",
        now=0.0,
    )
    complete = executor_backoff.reconcile_backoff(
        tmp_path,
        "copilot",
        "gpt-5",
        now=0.0,
        inventory=_inventory_payload(
            _inventory_event(
                executor="copilot",
                model_id="gpt-5",
                job_id="job-1",
                event_epoch=100.0,
                reset_at=None,
                reason="stored-event",
            )
        ),
    )
    assert complete.reconciliation is executor_backoff.ReconciliationStatus.COMPLETE
    assert complete.evidence_refs == ("fixture://inventory",)

    pending = executor_backoff.reconcile_backoff(
        tmp_path,
        "copilot",
        "gpt-5",
        now=0.0,
        inventory=_inventory_payload(
            _inventory_event(
                executor="copilot",
                model_id="gpt-5",
                job_id="job-1",
                event_epoch=100.0,
                reset_at=None,
                reason="stored-event",
            ),
            _inventory_event(
                executor="copilot",
                model_id="gpt-5",
                job_id="job-2",
                event_epoch=110.0,
                reset_at=None,
                reason="pending-event",
            ),
        ),
    )
    assert pending.reconciliation is executor_backoff.ReconciliationStatus.PENDING
    assert pending.missing_terminal_keys == ("job-2",)

    unknown = executor_backoff.reconcile_backoff(
        tmp_path,
        "copilot",
        "gpt-5",
        now=0.0,
        inventory=_inventory_payload(
            _inventory_event(
                executor="copilot",
                model_id="gpt-5",
                job_id="job-1",
                event_epoch=100.0,
                reset_at=None,
                reason="stored-event",
            ),
            readable=False,
        ),
    )
    assert unknown.reconciliation is executor_backoff.ReconciliationStatus.UNKNOWN
    assert unknown.diagnostics == ("inventory-unavailable",)


def test_reconcile_backoff_complete_requires_inventory_to_cover_all_store_events(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="stored-event-1",
        now=0.0,
    )
    _record(
        tmp_path,
        job_id="job-2",
        event_epoch=110.0,
        reset_at=None,
        reason="stored-event-2",
        now=0.0,
    )
    observed = executor_backoff.reconcile_backoff(
        tmp_path,
        "copilot",
        "gpt-5",
        now=0.0,
        inventory=_inventory_payload(
            _inventory_event(
                executor="copilot",
                model_id="gpt-5",
                job_id="job-1",
                event_epoch=100.0,
                reset_at=None,
                reason="stored-event-1",
            )
        ),
    )
    assert observed.reconciliation is executor_backoff.ReconciliationStatus.UNKNOWN
    assert observed.diagnostics == ("inventory-conflict",)
    assert observed.conflicting_terminal_keys == ("job-2",)


def test_reconcile_backoff_rejects_over_cap_inventory(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    monkeypatch.setattr(executor_backoff, "MAX_RETAINED_EVENTS", 1)
    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="stored-event",
        now=0.0,
    )
    observed = executor_backoff.reconcile_backoff(
        tmp_path,
        "copilot",
        "gpt-5",
        now=0.0,
        inventory=_inventory_payload(
            _inventory_event(
                executor="copilot",
                model_id="gpt-5",
                job_id="job-1",
                event_epoch=100.0,
                reset_at=None,
                reason="stored-event",
            ),
            _inventory_event(
                executor="copilot",
                model_id="gpt-5",
                job_id="job-2",
                event_epoch=110.0,
                reset_at=None,
                reason="pending-event",
            ),
        ),
    )
    assert _obs(observed.observation) == "valid"
    assert observed.reconciliation is executor_backoff.ReconciliationStatus.UNKNOWN
    assert "capacity-exceeded" in observed.diagnostics


def test_public_apis_fail_closed_when_close_raises(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="stored-event",
        now=0.0,
    )
    original_close = executor_backoff.os.close

    def flaky_close(fd: int) -> None:
        raise OSError("close denied")

    monkeypatch.setattr(executor_backoff.os, "close", flaky_close)
    active = executor_backoff.active_backoff(tmp_path, "copilot", "gpt-5", now=0.0)
    assert _obs(active.observation) == "unknown"
    assert "unable to close executor backoff lock" in active.diagnostics[0]

    read_view = executor_backoff.read_store(_state_path(tmp_path))
    assert _obs(read_view.observation) == "unknown"
    assert read_view.diagnostics

    reconciled = executor_backoff.reconcile_backoff(
        tmp_path,
        "copilot",
        "gpt-5",
        now=0.0,
        inventory=None,
    )
    assert _obs(reconciled.observation) == "unknown"
    assert reconciled.reconciliation is executor_backoff.ReconciliationStatus.UNKNOWN
    monkeypatch.setattr(executor_backoff.os, "close", original_close)


def test_reconcile_backoff_rejects_inventory_over_byte_cap(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="stored-event",
        now=0.0,
    )
    monkeypatch.setattr(executor_backoff, "MAX_INPUT_BYTES", 512)
    observed = executor_backoff.reconcile_backoff(
        tmp_path,
        "copilot",
        "gpt-5",
        now=0.0,
        inventory=_inventory_payload(
            _inventory_event(
                executor="copilot",
                model_id="gpt-5",
                job_id="job-1",
                event_epoch=100.0,
                reset_at=None,
                reason="stored-event",
            ),
            _inventory_event(
                executor="copilot",
                model_id="gpt-5",
                job_id="job-2",
                event_epoch=110.0,
                reset_at=None,
                reason="x" * 600,
            ),
        ),
    )
    assert _obs(observed.observation) == "valid"
    assert observed.reconciliation is executor_backoff.ReconciliationStatus.UNKNOWN
    assert "capacity-exceeded" in observed.diagnostics


def test_helper_policy_drift_is_rejected_as_unknown_policy(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    monkeypatch.setattr(executor_backoff, "tick_backoff_seconds", lambda base, hits: base)
    observed = _record(
        tmp_path,
        job_id="job-policy",
        event_epoch=100.0,
        reset_at=None,
        reason="policy-drift",
        now=0.0,
    )
    assert _obs(observed.observation) == "unknown"
    assert "unknown-policy" in observed.diagnostics
    assert not _state_path(tmp_path).exists()


def test_stale_reset_hint_is_rejected_fail_closed(tmp_path: Path) -> None:
    observed = _record(
        tmp_path,
        job_id="job-stale-reset",
        event_epoch=100.0,
        reset_at=90.0,
        reason="stale-reset",
        now=100.0,
    )
    assert _obs(observed.observation) == "unknown"
    assert "stale-reset-at" in observed.diagnostics
    assert not _state_path(tmp_path).exists()


@pytest.mark.parametrize("authority", ["banana", "hint"])
def test_invalid_authority_is_rejected_fail_closed(tmp_path: Path, authority: str) -> None:
    observed = _record(
        tmp_path,
        job_id=f"job-{authority}",
        event_epoch=100.0,
        reset_at=None,
        reason="bad-authority",
        now=0.0,
        outcome=_outcome_input(
            "rate_limited",
            "bad-authority",
            None,
            authority=authority,
        ),
    )
    assert _obs(observed.observation) == "unknown"
    assert "invalid-authority" in observed.diagnostics


def test_invalid_reset_provenance_stays_unknown_even_with_matching_fingerprint(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=1000.0,
        reason="structured-reset",
        now=100.0,
    )
    payload = _state_payload(tmp_path)
    event = payload["events"]["job-1"]
    event["reset_provenance"] = "banana"
    event_bytes = json.dumps(
        {key: value for key, value in event.items() if key != "fingerprint"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    event["fingerprint"] = hashlib.sha256(event_bytes).hexdigest()
    payload["acks"]["job-1"]["fingerprint"] = event["fingerprint"]
    _state_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    observed = executor_backoff.read_store(_state_path(tmp_path))
    assert _obs(observed.observation) == "unknown"
    assert "invalid-reset-provenance" in observed.diagnostics


def test_parsed_reset_provenance_with_parser_metadata_is_persisted(tmp_path: Path) -> None:
    recorded = _record(
        tmp_path,
        job_id="job-parsed",
        event_epoch=100.0,
        reset_at=1000.0,
        reason="parsed-reset",
        now=100.0,
        outcome=_outcome_input(
            "rate_limited",
            "parsed-reset",
            1000.0,
            authority="text_signal",
            reset_provenance="parsed",
            reset_parser={
                "rule_version": "v1",
                "timezone": "UTC",
                "base_year": 2026,
                "base_reference_epoch": 100.0,
            },
        ),
    )
    assert _obs(recorded.observation) == "valid"
    payload = _state_payload(tmp_path)
    assert payload["events"]["job-parsed"]["authority"] == "text_signal"
    assert payload["events"]["job-parsed"]["reset_provenance"] == "parsed"
    assert payload["events"]["job-parsed"]["reset_parser"]["timezone"] == "UTC"


def test_parsed_reset_must_not_precede_parser_baseline(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    observed = _record(
        tmp_path,
        job_id="job-bad-parsed-reset",
        event_epoch=40.0,
        reset_at=90.0,
        reason="parsed-reset",
        now=100.0,
        outcome=_outcome_input(
            "rate_limited",
            "parsed-reset",
            90.0,
            authority="text_signal",
            reset_provenance="parsed",
            reset_parser={
                "rule_version": "v1",
                "timezone": "UTC",
                "base_year": 2026,
                "base_reference_epoch": 100.0,
            },
        ),
    )
    assert _obs(observed.observation) == "unknown"
    assert "reset-at-before-base-reference" in observed.diagnostics

    _record(
        tmp_path / "read",
        job_id="job-read",
        event_epoch=40.0,
        reset_at=1000.0,
        reason="parsed-reset",
        now=100.0,
        outcome=_outcome_input(
            "rate_limited",
            "parsed-reset",
            1000.0,
            authority="text_signal",
            reset_provenance="parsed",
            reset_parser={
                "rule_version": "v1",
                "timezone": "UTC",
                "base_year": 2026,
                "base_reference_epoch": 40.0,
            },
        ),
    )
    payload = _state_payload(tmp_path / "read")
    payload["events"]["job-read"]["reset_at"] = 50.0
    payload["events"]["job-read"]["reset_parser"]["base_reference_epoch"] = 100.0
    payload["events"]["job-read"]["payload"]["reset_at"] = 50.0
    payload_body = json.dumps(
        payload["events"]["job-read"]["payload"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    payload["events"]["job-read"]["payload_fingerprint"] = hashlib.sha256(payload_body).hexdigest()
    event = payload["events"]["job-read"]
    event_bytes = json.dumps(
        {key: value for key, value in event.items() if key != "fingerprint"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    event["fingerprint"] = hashlib.sha256(event_bytes).hexdigest()
    payload["acks"]["job-read"]["fingerprint"] = event["fingerprint"]
    _state_path(tmp_path / "read").write_text(json.dumps(payload), encoding="utf-8")

    read_back = executor_backoff.read_store(_state_path(tmp_path / "read"))
    assert _obs(read_back.observation) == "unknown"
    assert "reset-at-before-base-reference" in read_back.diagnostics


def test_parsed_provenance_requires_parser_metadata_and_evidence_ref(tmp_path: Path) -> None:
    missing_parser = _record(
        tmp_path,
        job_id="job-missing-parser",
        event_epoch=100.0,
        reset_at=1000.0,
        reason="parsed-reset",
        now=0.0,
        outcome=_outcome_input(
            "rate_limited",
            "parsed-reset",
            1000.0,
            reset_provenance="parsed",
            reset_parser=None,
        ),
    )
    assert _obs(missing_parser.observation) == "unknown"
    assert "invalid-reset_parser" in missing_parser.diagnostics

    missing_evidence = _record(
        tmp_path,
        job_id="job-missing-evidence",
        event_epoch=100.0,
        reset_at=1000.0,
        reason="parsed-reset",
        now=0.0,
        outcome=_outcome_input(
            "rate_limited",
            "parsed-reset",
            1000.0,
            reset_provenance="parsed",
            reset_parser={
                "rule_version": "v1",
                "timezone": "UTC",
                "base_year": 2026,
                "base_reference_epoch": 100.0,
            },
            evidence_ref="",
        ),
    )
    assert _obs(missing_evidence.observation) == "unknown"
    assert "invalid-evidence_ref" in missing_evidence.diagnostics


def test_parsed_base_year_must_fit_numeric_token_budget(tmp_path: Path) -> None:
    observed = _record(
        tmp_path,
        job_id="job-huge-base-year",
        event_epoch=100.0,
        reset_at=1000.0,
        reason="parsed-reset",
        now=0.0,
        outcome=_outcome_input(
            "rate_limited",
            "parsed-reset",
            1000.0,
            authority="text_signal",
            reset_provenance="parsed",
            reset_parser={
                "rule_version": "v1",
                "timezone": "UTC",
                "base_year": int("9" * 100),
                "base_reference_epoch": 100.0,
            },
        ),
    )
    assert _obs(observed.observation) == "unknown"
    assert "json-number-token-too-large" in observed.diagnostics
    assert not _state_path(tmp_path).exists()


def test_policy_envelope_mismatch_is_rejected(tmp_path: Path) -> None:
    observed = _record(
        tmp_path,
        job_id="job-bad-policy",
        event_epoch=100.0,
        reset_at=None,
        reason="bad-policy",
        now=0.0,
        outcome=_outcome_input(
            "rate_limited",
            "bad-policy",
            None,
            payload={"outcome": "rate_limited", "reason": "bad-policy", "reset_at": None},
            reset_margin_seconds=6.0,
        ),
    )
    assert _obs(observed.observation) == "unknown"
    assert "unknown-policy" in observed.diagnostics


def test_cyclic_payload_fails_closed_without_escaping(tmp_path: Path) -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    observed = _record(
        tmp_path,
        job_id="job-cyclic",
        event_epoch=100.0,
        reset_at=None,
        reason="cyclic-payload",
        now=0.0,
        outcome={
            "outcome": "rate_limited",
            "authority": "structured",
            "payload": cyclic,
            "payload_fingerprint": "0" * 64,
            "reason": "cyclic-payload",
            "policy_revision": "executor-backoff/v1",
            "rate_limited_base_seconds": 10.0,
            "quota_base_seconds": 40.0,
            "backoff_multiplier_base": 2.0,
            "backoff_max_exponent": 4,
            "reset_margin_seconds": 5.0,
            "reset_provenance": "absent",
            "reset_parser": None,
            "evidence_ref": "fixture://terminal",
        },
    )
    assert _obs(observed.observation) == "unknown"
    assert "invalid-payload" in observed.diagnostics


def test_payload_mutation_after_validation_does_not_change_persisted_event(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    payload = {
        "outcome": "rate_limited",
        "authority": "structured",
        "reason": "original",
        "retryable": True,
    }
    payload_bytes = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    outcome = {
        "outcome": "rate_limited",
        "authority": "structured",
        "payload": payload,
        "payload_fingerprint": hashlib.sha256(payload_bytes).hexdigest(),
        "reason": "original",
        "policy_revision": "executor-backoff/v1",
        "rate_limited_base_seconds": 10.0,
        "quota_base_seconds": 40.0,
        "backoff_multiplier_base": 2.0,
        "backoff_max_exponent": 4,
        "reset_margin_seconds": 5.0,
        "reset_provenance": "absent",
        "reset_parser": None,
        "evidence_ref": "fixture://terminal",
    }

    def mutate_payload(stage: str) -> None:
        if stage == "before-replace":
            payload["reason"] = "mutated"

    monkeypatch.setattr(executor_backoff, "_TEST_HOOK", mutate_payload)
    observed = executor_backoff.record_backoff(
        tmp_path,
        "copilot",
        "gpt-5",
        now=0.0,
        outcome=outcome,
        reset_at=None,
        reason="original",
        job_id="job-1",
        event_epoch=100.0,
    )
    monkeypatch.setattr(executor_backoff, "_TEST_HOOK", None)
    assert _obs(observed.observation) == "valid"
    assert _state_payload(tmp_path)["events"]["job-1"]["payload"]["reason"] == "original"


def test_inventory_mutation_after_validation_does_not_change_reconciliation(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="stored-event",
        now=0.0,
    )
    inventory = _inventory_payload(
        _inventory_event(
            executor="copilot",
            model_id="gpt-5",
            job_id="job-1",
            event_epoch=100.0,
            reset_at=None,
            reason="stored-event",
        )
    )
    original_encode = executor_backoff._bounded_canonical_json_bytes
    mutated = False

    def mutating_encode(payload, **kwargs):
        nonlocal mutated
        body = original_encode(payload, **kwargs)
        if (
            not mutated
            and payload is inventory
            and kwargs.get("max_bytes") == executor_backoff.MAX_INPUT_BYTES
        ):
            mutated = True
            inventory["events"][0]["job_id"] = "job-mutated"
        return body

    monkeypatch.setattr(executor_backoff, "_bounded_canonical_json_bytes", mutating_encode)
    observed = executor_backoff.reconcile_backoff(
        tmp_path,
        "copilot",
        "gpt-5",
        now=0.0,
        inventory=inventory,
    )
    assert _obs(observed.observation) == "valid"
    assert observed.reconciliation is executor_backoff.ReconciliationStatus.COMPLETE
    assert observed.diagnostics == ()


def test_custom_mapping_payload_is_rejected_fail_closed(tmp_path: Path) -> None:
    class CustomMapping(dict):
        def items(self):
            raise RuntimeError("should not iterate custom mapping")

    observed = _record(
        tmp_path,
        job_id="job-custom-mapping",
        event_epoch=100.0,
        reset_at=None,
        reason="custom-mapping",
        now=0.0,
        outcome={
            "outcome": "rate_limited",
            "authority": "structured",
            "payload": CustomMapping(
                {"outcome": "rate_limited", "reason": "custom-mapping", "reset_at": None}
            ),
            "payload_fingerprint": "0" * 64,
            "reason": "custom-mapping",
            "policy_revision": "executor-backoff/v1",
            "rate_limited_base_seconds": 10.0,
            "quota_base_seconds": 40.0,
            "backoff_multiplier_base": 2.0,
            "backoff_max_exponent": 4,
            "reset_margin_seconds": 5.0,
            "reset_provenance": "absent",
            "reset_parser": None,
            "evidence_ref": "fixture://terminal",
        },
    )
    assert _obs(observed.observation) == "unknown"
    assert "invalid-payload" in observed.diagnostics


def test_nested_custom_mapping_and_list_payloads_are_rejected_fail_closed(
    tmp_path: Path,
) -> None:
    class CustomMapping(dict):
        pass

    class CustomList(list):
        pass

    mapping_observed = _record(
        tmp_path / "mapping",
        job_id="job-nested-mapping",
        event_epoch=100.0,
        reset_at=None,
        reason="nested-mapping",
        now=0.0,
        outcome={
            "outcome": "rate_limited",
            "authority": "structured",
            "payload": {
                "outcome": "rate_limited",
                "reason": "nested-mapping",
                "reset_at": None,
                "nested": CustomMapping({"k": "v"}),
            },
            "payload_fingerprint": "0" * 64,
            "reason": "nested-mapping",
            "policy_revision": "executor-backoff/v1",
            "rate_limited_base_seconds": 10.0,
            "quota_base_seconds": 40.0,
            "backoff_multiplier_base": 2.0,
            "backoff_max_exponent": 4,
            "reset_margin_seconds": 5.0,
            "reset_provenance": "absent",
            "reset_parser": None,
            "evidence_ref": "fixture://terminal",
        },
    )
    assert _obs(mapping_observed.observation) == "unknown"
    assert "invalid-payload" in mapping_observed.diagnostics

    list_observed = _record(
        tmp_path / "list",
        job_id="job-nested-list",
        event_epoch=100.0,
        reset_at=None,
        reason="nested-list",
        now=0.0,
        outcome={
            "outcome": "rate_limited",
            "authority": "structured",
            "payload": {
                "outcome": "rate_limited",
                "reason": "nested-list",
                "reset_at": None,
                "nested": CustomList(["x"]),
            },
            "payload_fingerprint": "0" * 64,
            "reason": "nested-list",
            "policy_revision": "executor-backoff/v1",
            "rate_limited_base_seconds": 10.0,
            "quota_base_seconds": 40.0,
            "backoff_multiplier_base": 2.0,
            "backoff_max_exponent": 4,
            "reset_margin_seconds": 5.0,
            "reset_provenance": "absent",
            "reset_parser": None,
            "evidence_ref": "fixture://terminal",
        },
    )
    assert _obs(list_observed.observation) == "unknown"
    assert "invalid-payload" in list_observed.diagnostics


def test_quota_outcome_uses_a_longer_base_than_rate_limited(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    rate = _record(
        tmp_path / "rate",
        job_id="job-rate",
        outcome="rate_limited",
        event_epoch=100.0,
        reset_at=None,
        reason="rate-limit",
        now=100.0,
    )
    quota = _record(
        tmp_path / "quota",
        job_id="job-quota",
        outcome="quota",
        event_epoch=100.0,
        reset_at=None,
        reason="quota-hit",
        now=100.0,
    )
    assert executor_backoff.QUOTA_BASE_SECONDS > executor_backoff.RATE_LIMITED_BASE_SECONDS
    assert rate.backoff is not None
    assert quota.backoff is not None
    assert rate.backoff.deadline_epoch == 120.0
    assert quota.backoff.deadline_epoch == 180.0


def test_record_rejects_event_that_would_be_too_large_after_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    reason = "x" * 100
    event = executor_backoff._event_from_inputs(
        executor="copilot",
        model_id="gpt-5",
        outcome=_outcome_input("rate_limited", reason, None),
        reset_at=None,
        reason=reason,
        job_id="job-1",
        event_epoch=100.0,
        budget=executor_backoff._ComputeBudget(),
    )
    full_length = len(executor_backoff._bounded_canonical_json_bytes(event.to_payload()))
    monkeypatch.setattr(executor_backoff, "MAX_EVENT_CANONICAL_BYTES", full_length - 1)

    observed = _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason=reason,
        now=0.0,
    )
    assert _obs(observed.observation) == "unknown"
    assert "event-too-large" in observed.diagnostics
    assert not _state_path(tmp_path).exists()


def test_duplicate_replay_is_unchanged_and_same_job_conflict_is_unknown(tmp_path: Path) -> None:
    first = _record(
        tmp_path,
        reset_at=1000.0,
        reason="structured-reset",
        job_id="job-1",
        event_epoch=100.0,
    )
    before = _state_bytes(tmp_path)
    duplicate = _record(
        tmp_path,
        reset_at=1000.0,
        reason="structured-reset",
        job_id="job-1",
        event_epoch=100.0,
    )
    assert _obs(first.observation) == "valid"
    assert _obs(duplicate.observation) == "valid"
    assert duplicate.changed is False
    assert _state_bytes(tmp_path) == before

    conflict = _record(
        tmp_path,
        reset_at=900.0,
        reason="different-reset",
        job_id="job-1",
        event_epoch=100.0,
    )
    assert _obs(conflict.observation) == "unknown"
    assert "integrity-conflict" in conflict.diagnostics
    assert conflict.last_good is not None
    assert _state_bytes(tmp_path) == before


def test_same_job_replay_stays_idempotent_across_fresh_processes(tmp_path: Path) -> None:
    _record(
        tmp_path,
        reset_at=1000.0,
        reason="structured-reset",
        job_id="job-1",
        event_epoch=100.0,
    )
    before = _state_bytes(tmp_path)

    ctx = _ctx()
    queue = ctx.Queue()
    proc = ctx.Process(
        target=_spawn_record,
        args=(str(tmp_path), queue),
        kwargs={
            "reset_at": 1000.0,
            "reason": "structured-reset",
            "job_id": "job-1",
            "event_epoch": 100.0,
        },
    )
    proc.start()
    proc.join(10)
    assert proc.exitcode == 0
    result = queue.get(timeout=2)["result"]
    assert result["observation"] == "valid"
    assert result["changed"] is False
    assert _state_bytes(tmp_path) == before


def test_event_time_fold_is_order_independent_and_shorter_reset_never_wins(tmp_path: Path) -> None:
    permutations = [
        (
            {"job_id": "job-200", "event_epoch": 200.0, "reset_at": 1000.0, "reason": "new-reset"},
            {"job_id": "job-100", "event_epoch": 100.0, "reset_at": 300.0, "reason": "old-reset"},
        ),
        (
            {"job_id": "job-100", "event_epoch": 100.0, "reset_at": 300.0, "reason": "old-reset"},
            {"job_id": "job-200", "event_epoch": 200.0, "reset_at": 1000.0, "reason": "new-reset"},
        ),
    ]
    for index, ordered_events in enumerate(permutations):
        root = tmp_path / f"perm-{index}"
        for event in ordered_events:
            result = _record(root, **event, now=0.0)
            assert _obs(result.observation) == "valid"
        active = _active(root, now=0.0)
        assert active.backoff is not None
        assert active.backoff.deadline_epoch == 1005.0
        assert active.backoff.consecutive_hits == 2
        assert active.backoff.last_terminal_key == "job-200"

    shorter = _record(
        tmp_path / "shorter-reset",
        job_id="job-200",
        event_epoch=200.0,
        reset_at=1000.0,
        reason="first-reset",
        now=0.0,
    )
    assert shorter.backoff is not None
    second = _record(
        tmp_path / "shorter-reset",
        job_id="job-250",
        event_epoch=250.0,
        reset_at=400.0,
        reason="shorter-reset",
        now=0.0,
    )
    assert second.backoff is not None
    assert second.backoff.deadline_epoch == 1005.0
    assert second.backoff.consecutive_hits == 2


def test_cumulative_byte_budget_is_enforced_across_input_read_and_encode(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    monkeypatch.setattr(executor_backoff, "MAX_BYTE_WORK", 6000)
    reason = "x" * 20
    first = _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason=reason,
        now=0.0,
    )
    assert _obs(first.observation) == "valid"
    before = _state_bytes(tmp_path)

    second = _record(
        tmp_path,
        job_id="job-2",
        event_epoch=110.0,
        reset_at=None,
        reason=reason,
        now=0.0,
    )
    assert _obs(second.observation) == "unknown"
    assert "computation-budget-exceeded" in second.diagnostics
    assert _state_bytes(tmp_path) == before


def test_same_epoch_tie_and_deadline_boundary_fold_deterministically(tmp_path: Path) -> None:
    tie_root = tmp_path / "tie"
    _record(
        tie_root,
        job_id="job-b",
        event_epoch=100.0,
        reset_at=None,
        reason="later-by-key",
        now=0.0,
    )
    _record(
        tie_root,
        job_id="job-a",
        event_epoch=100.0,
        reset_at=None,
        reason="earlier-by-key",
        now=0.0,
    )
    tied = _active(tie_root, now=0.0)
    assert tied.backoff is not None
    assert tied.backoff.consecutive_hits == 2
    assert tied.backoff.deadline_epoch == 140.0
    assert tied.backoff.last_terminal_key == "job-b"

    boundary_root = tmp_path / "boundary"
    _record(
        boundary_root,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="first-hit",
        now=0.0,
    )
    _record(
        boundary_root,
        job_id="job-2",
        event_epoch=120.0,
        reset_at=None,
        reason="new-episode",
        now=0.0,
    )
    boundary = _active(boundary_root, now=0.0)
    assert boundary.backoff is not None
    assert boundary.backoff.consecutive_hits == 1
    assert boundary.backoff.deadline_epoch == 140.0


def test_active_clear_rejects_expiry_keeps_acks_and_duplicate_replay_does_not_revive(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="first-hit",
        now=100.0,
    )

    monkeypatch.setattr(executor_backoff, "_time_now", lambda: 100.0)
    active_clear = executor_backoff.clear_backoff(tmp_path, "copilot", "gpt-5")
    assert _obs(active_clear.observation) == "valid"
    assert active_clear.changed is False
    assert active_clear.diagnostics == ("active-cooldown",)

    assert _active(tmp_path, now=130.0).backoff is None

    monkeypatch.setattr(executor_backoff, "_time_now", lambda: 130.0)
    expired_clear = executor_backoff.clear_backoff(tmp_path, "copilot", "gpt-5")
    assert _obs(expired_clear.observation) == "valid"
    assert expired_clear.changed is False
    assert expired_clear.last_good is not None

    replay = _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="first-hit",
        now=130.0,
    )
    assert _obs(replay.observation) == "valid"
    assert replay.changed is False
    assert replay.backoff is None
    assert _state_payload(tmp_path)["acks"]["job-1"]["terminal_key"] == "job-1"


def test_invalid_identity_and_unsupported_outcome_stay_unknown(tmp_path: Path) -> None:
    bad_identity = _record(
        tmp_path,
        executor="copilot/v2",
        model_id="gpt-5",
        job_id="job-1",
    )
    assert _obs(bad_identity.observation) == "unknown"
    assert "invalid-identity" in bad_identity.diagnostics

    oversized_identity = _record(
        tmp_path,
        executor="e" * 300,
        model_id="m" * 300,
        job_id="job-oversized-identity",
    )
    assert _obs(oversized_identity.observation) == "unknown"
    assert "identity-key-too-large" in oversized_identity.diagnostics
    assert not _state_path(tmp_path).exists()

    unsupported = _record(
        tmp_path,
        outcome="success",
        job_id="job-1",
    )
    assert _obs(unsupported.observation) == "unknown"
    assert "unsupported-outcome" in unsupported.diagnostics

    from paulsha_cortex.coordinator import executor_backoff

    invalid_reconcile = executor_backoff.reconcile_backoff(
        tmp_path,
        "bad/name",
        "gpt-5",
        now=0.0,
        inventory=None,
    )
    assert _obs(invalid_reconcile.observation) == "unknown"
    assert invalid_reconcile.reconciliation is executor_backoff.ReconciliationStatus.UNKNOWN
    assert "invalid-identity" in invalid_reconcile.diagnostics


def test_symlink_loop_roots_fail_closed_across_public_apis(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    root = tmp_path / "loop"
    root.symlink_to(root, target_is_directory=True)

    read_view = executor_backoff.read_store(root / executor_backoff.STATE_FILENAME)
    assert _obs(read_view.observation) == "unknown"
    assert "invalid-coordinator-root" in read_view.diagnostics

    active = executor_backoff.active_backoff(root, "copilot", "gpt-5", now=0.0)
    assert _obs(active.observation) == "unknown"
    assert "invalid-coordinator-root" in active.diagnostics

    recorded = executor_backoff.record_backoff(
        root,
        "copilot",
        "gpt-5",
        now=0.0,
        outcome=_outcome_input("rate_limited", "loop", None),
        reset_at=None,
        reason="loop",
        job_id="job-loop",
        event_epoch=100.0,
    )
    assert _obs(recorded.observation) == "unknown"
    assert "invalid-coordinator-root" in recorded.diagnostics

    cleared = executor_backoff.clear_backoff(root, "copilot", "gpt-5")
    assert _obs(cleared.observation) == "unknown"
    assert "invalid-coordinator-root" in cleared.diagnostics

    reconciled = executor_backoff.reconcile_backoff(
        root,
        "copilot",
        "gpt-5",
        now=0.0,
        inventory=None,
    )
    assert _obs(reconciled.observation) == "unknown"
    assert "invalid-coordinator-root" in reconciled.diagnostics


def test_embedded_nul_roots_fail_closed_across_public_apis(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    bad_root = "\0bad-root"

    read_view = executor_backoff.read_store(Path(bad_root) / executor_backoff.STATE_FILENAME)
    assert _obs(read_view.observation) == "unknown"
    assert "invalid-coordinator-root" in read_view.diagnostics

    active = executor_backoff.active_backoff(bad_root, "copilot", "gpt-5", now=0.0)
    assert _obs(active.observation) == "unknown"
    assert "invalid-coordinator-root" in active.diagnostics

    recorded = executor_backoff.record_backoff(
        bad_root,
        "copilot",
        "gpt-5",
        now=0.0,
        outcome=_outcome_input("rate_limited", "nul-root", None),
        reset_at=None,
        reason="nul-root",
        job_id="job-nul",
        event_epoch=100.0,
    )
    assert _obs(recorded.observation) == "unknown"
    assert "invalid-coordinator-root" in recorded.diagnostics

    cleared = executor_backoff.clear_backoff(bad_root, "copilot", "gpt-5")
    assert _obs(cleared.observation) == "unknown"
    assert "invalid-coordinator-root" in cleared.diagnostics

    reconciled = executor_backoff.reconcile_backoff(
        bad_root,
        "copilot",
        "gpt-5",
        now=0.0,
        inventory=None,
    )
    assert _obs(reconciled.observation) == "unknown"
    assert "invalid-coordinator-root" in reconciled.diagnostics


def test_non_pathlike_roots_and_evil_outcomes_fail_closed(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    active = executor_backoff.active_backoff(123, "copilot", "gpt-5", now=0.0)
    assert _obs(active.observation) == "unknown"
    assert "invalid-coordinator-root" in active.diagnostics

    recorded = executor_backoff.record_backoff(
        123,
        "copilot",
        "gpt-5",
        now=0.0,
        outcome=_outcome_input("rate_limited", "bad-root", None),
        reset_at=None,
        reason="bad-root",
        job_id="job-bad-root",
        event_epoch=100.0,
    )
    assert _obs(recorded.observation) == "unknown"
    assert "invalid-coordinator-root" in recorded.diagnostics

    cleared = executor_backoff.clear_backoff(123, "copilot", "gpt-5")
    assert _obs(cleared.observation) == "unknown"
    assert "invalid-coordinator-root" in cleared.diagnostics

    reconciled = executor_backoff.reconcile_backoff(123, "copilot", "gpt-5", now=0.0, inventory=None)
    assert _obs(reconciled.observation) == "unknown"
    assert "invalid-coordinator-root" in reconciled.diagnostics

    assert _obs(executor_backoff.read_store(123).observation) == "unknown"

    class EvilOutcome:
        @property
        def value(self):
            raise RuntimeError("boom")

    evil = _record(
        tmp_path,
        job_id="job-evil-outcome",
        event_epoch=100.0,
        reset_at=None,
        reason="evil-outcome",
        now=0.0,
        outcome={
            "outcome": EvilOutcome(),
            "authority": "structured",
            "payload": {
                "outcome": "rate_limited",
                "authority": "structured",
                "reason": "evil-outcome",
                "retryable": True,
            },
            "payload_fingerprint": hashlib.sha256(
                json.dumps(
                    {
                        "outcome": "rate_limited",
                        "authority": "structured",
                        "reason": "evil-outcome",
                        "retryable": True,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
            "reason": "evil-outcome",
            "policy_revision": "executor-backoff/v1",
            "rate_limited_base_seconds": 10.0,
            "quota_base_seconds": 40.0,
            "backoff_multiplier_base": 2.0,
            "backoff_max_exponent": 4,
            "reset_margin_seconds": 5.0,
            "reset_provenance": "absent",
            "reset_parser": None,
            "evidence_ref": "fixture://terminal",
        },
    )
    assert _obs(evil.observation) == "unknown"
    assert "unsupported-outcome" in evil.diagnostics


def test_long_root_basename_uses_hashed_lock_name_without_name_max_failure(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    root = tmp_path / ("a" * 240)
    observed = executor_backoff.active_backoff(root, "copilot", "gpt-5", now=0.0)
    assert _obs(observed.observation) == "missing"


def test_huge_epoch_and_now_values_fail_closed(tmp_path: Path) -> None:
    huge_epoch = _record(
        tmp_path,
        job_id="job-huge",
        event_epoch=10**1000,
        reset_at=None,
        reason="huge-epoch",
        now=0.0,
    )
    assert _obs(huge_epoch.observation) == "unknown"
    assert "invalid-event_epoch" in huge_epoch.diagnostics

    from paulsha_cortex.coordinator import executor_backoff

    active = executor_backoff.active_backoff(tmp_path, "copilot", "gpt-5", now=10**1000)
    assert _obs(active.observation) == "unknown"
    assert "invalid-now" in active.diagnostics

    reconciled = executor_backoff.reconcile_backoff(
        tmp_path,
        "copilot",
        "gpt-5",
        now=10**1000,
        inventory=None,
    )
    assert _obs(reconciled.observation) == "unknown"
    assert "invalid-now" in reconciled.diagnostics


def test_capacity_rejects_new_event_but_keeps_exact_duplicate_and_does_not_free_on_clear(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    monkeypatch.setattr(executor_backoff, "MAX_RETAINED_EVENTS", 1)
    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="only-slot",
        now=0.0,
    )
    before = _state_bytes(tmp_path)

    duplicate = _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="only-slot",
        now=0.0,
    )
    assert _obs(duplicate.observation) == "valid"
    assert duplicate.changed is False
    assert _state_bytes(tmp_path) == before

    new_event = _record(
        tmp_path,
        job_id="job-2",
        event_epoch=110.0,
        reset_at=None,
        reason="should-fail",
        now=0.0,
    )
    assert _obs(new_event.observation) == "unknown"
    assert "capacity-exceeded" in new_event.diagnostics
    assert _state_bytes(tmp_path) == before

    monkeypatch.setattr(executor_backoff, "_time_now", lambda: 1000.0)
    cleared = executor_backoff.clear_backoff(tmp_path, "copilot", "gpt-5")
    assert _obs(cleared.observation) == "valid"
    assert cleared.changed is False

    still_full = _record(
        tmp_path,
        job_id="job-3",
        event_epoch=120.0,
        reset_at=None,
        reason="still-no-room",
        now=1000.0,
    )
    assert _obs(still_full.observation) == "unknown"
    assert "capacity-exceeded" in still_full.diagnostics
    assert _state_bytes(tmp_path) == before


def test_fold_event_limit_rejects_second_event_without_rewriting_store(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    monkeypatch.setattr(executor_backoff, "MAX_FOLD_EVENTS", 1)
    first = _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="first-event",
        now=0.0,
    )
    assert _obs(first.observation) == "valid"
    before = _state_bytes(tmp_path)
    second = _record(
        tmp_path,
        job_id="job-2",
        event_epoch=110.0,
        reset_at=None,
        reason="second-event",
        now=0.0,
    )
    assert _obs(second.observation) == "unknown"
    assert "capacity-exceeded" in second.diagnostics
    assert _state_bytes(tmp_path) == before


def test_compute_budget_exceeded_returns_unknown_and_releases_lock(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    calls = 0
    original_checkpoint = executor_backoff._ComputeBudget.checkpoint

    def failing_checkpoint(self) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise executor_backoff._StoreProblem("computation-budget-exceeded")
        original_checkpoint(self)

    monkeypatch.setattr(executor_backoff._ComputeBudget, "checkpoint", failing_checkpoint)
    failed = _record(
        tmp_path,
        job_id="job-budget",
        event_epoch=100.0,
        reason="budget-hit",
        now=0.0,
    )
    assert _obs(failed.observation) == "unknown"
    assert "computation-budget-exceeded" in failed.diagnostics

    monkeypatch.setattr(executor_backoff._ComputeBudget, "checkpoint", original_checkpoint)
    success = _record(
        tmp_path,
        job_id="job-after-budget",
        event_epoch=101.0,
        reason="budget-released",
        now=0.0,
    )
    assert _obs(success.observation) == "valid"
    assert success.changed is True


def test_sort_comparison_budget_rejects_append_without_rewriting_store(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="first-event",
        now=0.0,
    )
    before = _state_bytes(tmp_path)
    monkeypatch.setattr(executor_backoff, "MAX_SORT_COMPARISONS", 1)
    observed = _record(
        tmp_path,
        job_id="job-2",
        event_epoch=110.0,
        reset_at=None,
        reason="second-event",
        now=0.0,
    )
    assert _obs(observed.observation) == "unknown"
    assert "computation-budget-exceeded" in observed.diagnostics
    assert _state_bytes(tmp_path) == before


def test_temp_fsync_failure_preserves_old_bytes_and_does_not_ack(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    original_fsync = executor_backoff.os.fsync
    calls = 0

    def failing_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temp-fsync-failed")
        original_fsync(fd)

    monkeypatch.setattr(executor_backoff.os, "fsync", failing_fsync)
    observed = _record(
        tmp_path,
        job_id="job-temp-fsync",
        event_epoch=100.0,
        reset_at=None,
        reason="temp-fsync-failure",
        now=0.0,
    )
    assert _obs(observed.observation) == "unknown"
    assert "unable to fsync executor backoff temp file" in observed.diagnostics[0]
    assert not _state_path(tmp_path).exists()
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]


def test_temp_close_failure_fails_closed(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    original_fdopen = executor_backoff.os.fdopen

    class ClosingWrapper:
        def __init__(self, handle):
            self._handle = handle

        def write(self, data):
            return self._handle.write(data)

        def flush(self):
            return self._handle.flush()

        def fileno(self):
            return self._handle.fileno()

        def close(self):
            self._handle.close()
            raise OSError("close failed")

    def flaky_fdopen(fd: int, mode: str):
        return ClosingWrapper(original_fdopen(fd, mode))

    monkeypatch.setattr(executor_backoff.os, "fdopen", flaky_fdopen)
    observed = _record(
        tmp_path,
        job_id="job-temp-close",
        event_epoch=100.0,
        reset_at=None,
        reason="temp-close-failure",
        now=0.0,
    )
    assert _obs(observed.observation) == "unknown"
    assert "unable to close executor backoff temp file" in observed.diagnostics[0]
    assert not _state_path(tmp_path).exists()


def test_replace_failure_preserves_old_bytes_and_no_new_ack(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="baseline",
        now=0.0,
    )
    before = _state_bytes(tmp_path)

    def failing_replace(src: Path, dst: Path) -> None:
        raise OSError("replace-failed")

    monkeypatch.setattr(executor_backoff.os, "replace", failing_replace)
    observed = _record(
        tmp_path,
        job_id="job-2",
        event_epoch=110.0,
        reset_at=None,
        reason="replace-failure",
        now=0.0,
    )
    assert _obs(observed.observation) == "unknown"
    assert "unable to replace executor backoff store" in observed.diagnostics[0]
    assert _state_bytes(tmp_path) == before
    assert "job-2" not in _state_payload(tmp_path)["acks"]


def test_post_replace_directory_fsync_failure_reports_commit_durability_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    def failing_directory_fsync(path: Path) -> None:
        raise executor_backoff._StoreProblem(
            "unable to fsync executor backoff store directory: durable-barrier-failed"
        )

    monkeypatch.setattr(executor_backoff, "_fsync_directory", failing_directory_fsync)
    observed = _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="dir-fsync-failure",
        now=0.0,
    )
    assert _obs(observed.observation) == "unknown"
    assert observed.diagnostics[0] == "commit-durability-unknown"
    assert observed.changed is True
    assert observed.last_good is not None
    assert observed.last_good.deadline_epoch == 120.0
    assert _state_path(tmp_path).exists()
    payload = _state_payload(tmp_path)
    assert "job-1" in payload["acks"]


def test_read_paths_can_use_existing_lock_file_without_write_permission(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="baseline",
        now=0.0,
    )
    executor_backoff._lock_path(tmp_path).chmod(0o400)

    read_back = executor_backoff.read_store(_state_path(tmp_path))
    assert _obs(read_back.observation) == "valid"

    active = executor_backoff.active_backoff(tmp_path, "copilot", "gpt-5", now=0.0)
    assert _obs(active.observation) == "valid"
    assert active.backoff is not None


def test_lock_symlink_is_rejected_fail_closed(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    target = tmp_path / "target"
    target.write_text("not-a-lock", encoding="utf-8")
    lock_path = executor_backoff._lock_path(tmp_path)
    lock_path.symlink_to(target)

    active = executor_backoff.active_backoff(tmp_path, "copilot", "gpt-5", now=0.0)
    assert _obs(active.observation) == "unknown"
    assert "unable to open executor backoff lock" in active.diagnostics[0]


def test_missing_store_stays_missing_when_root_parent_is_not_writable(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    parent = tmp_path / "readonly-parent"
    parent.mkdir()
    root = parent / "missing-root"
    parent.chmod(0o500)
    try:
        active = executor_backoff.active_backoff(root, "copilot", "gpt-5", now=0.0)
        observed = executor_backoff.read_store(root / executor_backoff.STATE_FILENAME)
    finally:
        parent.chmod(0o700)
    assert _obs(active.observation) == "missing"
    assert _obs(observed.observation) == "missing"


def test_mkdir_and_mkstemp_faults_fail_closed_instead_of_escaping(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    root = tmp_path / "nested" / "root"
    original_mkdir = Path.mkdir

    def failing_mkdir(self: Path, *args, **kwargs):
        if self == root:
            raise PermissionError("mkdir denied")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)
    mkdir_fault = _record(
        root,
        job_id="job-mkdir",
        event_epoch=100.0,
        reset_at=None,
        reason="mkdir-fault",
        now=0.0,
    )
    assert _obs(mkdir_fault.observation) == "unknown"
    assert "unable to create executor backoff root" in mkdir_fault.diagnostics[0]

    monkeypatch.setattr(Path, "mkdir", original_mkdir)
    root.mkdir(parents=True, exist_ok=True)

    def failing_mkstemp(*args, **kwargs):
        raise PermissionError("mkstemp denied")

    monkeypatch.setattr(executor_backoff.tempfile, "mkstemp", failing_mkstemp)
    mkstemp_fault = _record(
        root,
        job_id="job-mkstemp",
        event_epoch=100.0,
        reset_at=None,
        reason="mkstemp-fault",
        now=0.0,
    )
    assert _obs(mkstemp_fault.observation) == "unknown"
    assert "unable to allocate executor backoff temp file" in mkstemp_fault.diagnostics[0]
    assert not _state_path(root).exists()


def test_aggregate_mismatch_retains_last_good_for_other_identity_from_events(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="copilot-event",
        now=0.0,
        executor="copilot",
        model_id="gpt-5",
    )
    _record(
        tmp_path,
        job_id="job-2",
        event_epoch=100.0,
        reset_at=883.0,
        reason="zed-event",
        now=0.0,
        executor="zed",
        model_id="m2",
    )
    payload = _state_payload(tmp_path)
    payload["entries"]["copilot/gpt-5"]["deadline_epoch"] = 999.0
    _state_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    observed = executor_backoff.active_backoff(tmp_path, "zed", "m2", now=0.0)
    assert _obs(observed.observation) == "unknown"
    assert "aggregate-mismatch" in observed.diagnostics
    assert observed.last_good is not None
    assert observed.last_good.executor == "zed"
    assert observed.last_good.model_id == "m2"
    assert observed.last_good.deadline_epoch == 888.0


def test_cross_process_writers_serialize_and_preserve_both_updates(tmp_path: Path) -> None:
    ctx = _ctx()
    queue1 = ctx.Queue()
    queue2 = ctx.Queue()
    ready = ctx.Event()
    release = ctx.Event()

    writer1 = ctx.Process(
        target=_spawn_record,
        args=(str(tmp_path), queue1),
        kwargs={
            "job_id": "job-1",
            "event_epoch": 100.0,
            "reason": "first-process",
            "now": 0.0,
            "hook_stage": "lock-acquired",
            "stage_ready": ready,
            "release": release,
        },
    )
    writer2 = ctx.Process(
        target=_spawn_record,
        args=(str(tmp_path), queue2),
        kwargs={
            "job_id": "job-2",
            "event_epoch": 110.0,
            "reason": "second-process",
            "now": 0.0,
            "executor": "codex",
            "model_id": "gpt-5-mini",
        },
    )

    writer1.start()
    assert ready.wait(5), "writer1 never acquired the lock"
    writer2.start()
    writer2.join(0.2)
    assert writer2.is_alive(), "writer2 should remain blocked until writer1 releases the lock"
    release.set()
    writer1.join(10)
    writer2.join(10)
    assert writer1.exitcode == 0
    assert writer2.exitcode == 0

    result1 = queue1.get(timeout=2)["result"]
    result2 = queue2.get(timeout=2)["result"]
    assert result1["observation"] == "valid"
    assert result2["observation"] == "valid"

    payload = _state_payload(tmp_path)
    assert set(payload["events"]) == {"job-1", "job-2"}
    assert set(payload["entries"]) == {"copilot/gpt-5", "codex/gpt-5-mini"}


def test_lock_timeout_stays_unknown_without_modifying_store(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    ctx = _ctx()
    ready = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(target=_spawn_hold_lock, args=(str(tmp_path), ready, release))
    holder.start()
    assert ready.wait(5), "lock holder did not start"

    monkeypatch.setattr(executor_backoff, "LOCK_TIMEOUT_SECONDS", 0.05)
    timed_out = _record(
        tmp_path,
        job_id="job-timeout",
        event_epoch=100.0,
        reason="timeout",
        now=0.0,
    )
    assert _obs(timed_out.observation) == "unknown"
    assert "lock-timeout" in timed_out.diagnostics
    assert not _state_path(tmp_path).exists()

    release.set()
    holder.join(10)
    assert holder.exitcode == 0


def test_compute_budget_excludes_lock_wait_time(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    ctx = _ctx()
    ready = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(target=_spawn_hold_lock, args=(str(tmp_path), ready, release))
    holder.start()
    assert ready.wait(5), "lock holder did not start"

    monkeypatch.setattr(executor_backoff, "LOCK_TIMEOUT_SECONDS", 2.0)
    timer = threading.Timer(1.2, release.set)
    timer.start()
    try:
        observed = _record(
            tmp_path,
            job_id="job-after-wait",
            event_epoch=100.0,
            reset_at=None,
            reason="lock-wait",
            now=0.0,
        )
    finally:
        timer.cancel()
        release.set()
        holder.join(10)
    assert holder.exitcode == 0
    assert _obs(observed.observation) == "valid"
    assert observed.diagnostics == ()


def test_reconcile_backoff_compute_budget_excludes_lock_wait_time(
    tmp_path: Path, monkeypatch
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=None,
        reason="stored-event",
        now=0.0,
    )
    ctx = _ctx()
    ready = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(target=_spawn_hold_lock, args=(str(tmp_path), ready, release))
    holder.start()
    assert ready.wait(5), "lock holder did not start"

    monkeypatch.setattr(executor_backoff, "LOCK_TIMEOUT_SECONDS", 2.0)
    timer = threading.Timer(1.2, release.set)
    timer.start()
    try:
        observed = executor_backoff.reconcile_backoff(
            tmp_path,
            "copilot",
            "gpt-5",
            now=0.0,
            inventory=_inventory_payload(
                _inventory_event(
                    executor="copilot",
                    model_id="gpt-5",
                    job_id="job-1",
                    event_epoch=100.0,
                    reset_at=None,
                    reason="stored-event",
                )
            ),
        )
    finally:
        timer.cancel()
        release.set()
        holder.join(10)
    assert holder.exitcode == 0
    assert _obs(observed.observation) == "valid"
    assert observed.reconciliation is executor_backoff.ReconciliationStatus.COMPLETE
    assert observed.diagnostics == ()


def test_first_reader_waits_for_writer_even_when_root_is_created_by_that_writer(
    tmp_path: Path,
) -> None:
    root = tmp_path / "fresh-root"
    ctx = _ctx()
    writer_queue = ctx.Queue()
    ready = ctx.Event()
    release = ctx.Event()
    writer = ctx.Process(
        target=_spawn_record,
        args=(str(root), writer_queue),
        kwargs={
            "job_id": "job-1",
            "event_epoch": 100.0,
            "reset_at": None,
            "reason": "first-writer",
            "now": 0.0,
            "hook_stage": "lock-acquired",
            "stage_ready": ready,
            "release": release,
        },
    )
    writer.start()
    assert ready.wait(5), "writer did not acquire first lock"

    reader_queue = ctx.Queue()
    reader = ctx.Process(
        target=_spawn_active,
        args=(str(root), reader_queue),
        kwargs={"now": 0.0},
    )
    reader.start()
    reader.join(0.2)
    assert reader.is_alive(), "reader should wait for the writer-held root lock"

    release.set()
    writer.join(10)
    reader.join(10)
    assert writer.exitcode == 0
    assert reader.exitcode == 0
    assert writer_queue.get(timeout=2)["result"]["observation"] == "valid"
    assert reader_queue.get(timeout=2)["observation"] == "valid"


def test_reconcile_backoff_uses_fresh_consumer_inventory_after_process_death(
    tmp_path: Path,
) -> None:
    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=1000.0,
        reason="job-1",
        now=0.0,
    )

    ctx = _ctx()
    queue = ctx.Queue()
    ready = ctx.Event()
    release = ctx.Event()
    writer = ctx.Process(
        target=_spawn_record,
        args=(str(tmp_path), queue),
        kwargs={
            "job_id": "job-2",
            "event_epoch": 200.0,
            "reset_at": 1100.0,
            "reason": "job-2",
            "now": 0.0,
            "hook_stage": "before-replace",
            "stage_ready": ready,
            "release": release,
        },
    )
    writer.start()
    assert ready.wait(5), "writer never reached before-replace"
    writer.kill()
    writer.join(10)
    assert writer.exitcode != 0

    inventory = _inventory_payload(
        _inventory_event(
            executor="copilot",
            model_id="gpt-5",
            job_id="job-1",
            event_epoch=100.0,
            reset_at=1000.0,
            reason="job-1",
        ),
        _inventory_event(
            executor="copilot",
            model_id="gpt-5",
            job_id="job-2",
            event_epoch=200.0,
            reset_at=1100.0,
            reason="job-2",
        ),
    )
    reconcile_queue = ctx.Queue()
    consumer = ctx.Process(
        target=_spawn_reconcile,
        args=(str(tmp_path), reconcile_queue),
        kwargs={"now": 0.0, "inventory": inventory},
    )
    consumer.start()
    consumer.join(10)
    assert consumer.exitcode == 0
    reconciled = reconcile_queue.get(timeout=2)
    assert reconciled["reconciliation"] == "pending"
    assert reconciled["missing_terminal_keys"] == ("job-2",)
    assert reconciled["evidence_refs"] == ("fixture://inventory",)


@pytest.mark.parametrize(
    ("stage", "expect_job_2", "expected_reconciliation"),
    [
        ("before-replace", False, "pending"),
        ("after-replace", True, "complete"),
        ("after-directory-fsync", True, "complete"),
    ],
)
def test_real_process_death_windows_leave_old_or_new_commits_but_replay_stays_idempotent(
    tmp_path: Path, stage: str, expect_job_2: bool, expected_reconciliation: str
) -> None:
    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reset_at=1000.0,
        reason="job-1",
        now=0.0,
    )

    ctx = _ctx()
    queue = ctx.Queue()
    ready = ctx.Event()
    release = ctx.Event()
    writer = ctx.Process(
        target=_spawn_record,
        args=(str(tmp_path), queue),
        kwargs={
            "job_id": "job-2",
            "event_epoch": 200.0,
            "reset_at": 1100.0,
            "reason": "job-2",
            "now": 0.0,
            "hook_stage": stage,
            "stage_ready": ready,
            "release": release,
        },
    )
    writer.start()
    assert ready.wait(5), f"writer never reached {stage}"
    writer.kill()
    writer.join(10)
    assert writer.exitcode != 0

    reader_queue = ctx.Queue()
    reader = ctx.Process(target=_spawn_read_store, args=(str(tmp_path), reader_queue))
    reader.start()
    reader.join(10)
    assert reader.exitcode == 0
    observed = reader_queue.get(timeout=2)
    assert observed["observation"] == "valid"
    events = set((observed["payload"] or {}).get("events", {}))
    assert ("job-2" in events) is expect_job_2

    inventory = _inventory_payload(
        _inventory_event(
            executor="copilot",
            model_id="gpt-5",
            job_id="job-1",
            event_epoch=100.0,
            reset_at=1000.0,
            reason="job-1",
        ),
        _inventory_event(
            executor="copilot",
            model_id="gpt-5",
            job_id="job-2",
            event_epoch=200.0,
            reset_at=1100.0,
            reason="job-2",
        ),
    )
    reconcile_queue = ctx.Queue()
    consumer = ctx.Process(
        target=_spawn_reconcile,
        args=(str(tmp_path), reconcile_queue),
        kwargs={"now": 0.0, "inventory": inventory},
    )
    consumer.start()
    consumer.join(10)
    assert consumer.exitcode == 0
    reconciled = reconcile_queue.get(timeout=2)
    assert reconciled["observation"] == "valid"
    assert reconciled["reconciliation"] == expected_reconciliation
    if expected_reconciliation == "pending":
        assert reconciled["missing_terminal_keys"] == ("job-2",)
    else:
        assert reconciled["missing_terminal_keys"] == ()
    assert reconciled["evidence_refs"] == ("fixture://inventory",)

    replay = _record(
        tmp_path,
        job_id="job-2",
        event_epoch=200.0,
        reset_at=1100.0,
        reason="job-2",
        now=0.0,
    )
    assert _obs(replay.observation) == "valid"
    assert replay.changed is (not expect_job_2)


def test_last_slot_race_accepts_only_one_new_event(tmp_path: Path, monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    monkeypatch.setattr(executor_backoff, "MAX_RETAINED_EVENTS", 2)
    _record(
        tmp_path,
        job_id="job-1",
        event_epoch=100.0,
        reason="baseline",
        now=0.0,
    )

    ctx = _ctx()
    queue1 = ctx.Queue()
    queue2 = ctx.Queue()
    overrides = {"MAX_RETAINED_EVENTS": 2}
    writer1 = ctx.Process(
        target=_spawn_record,
        args=(str(tmp_path), queue1),
        kwargs={
            "job_id": "job-2",
            "event_epoch": 110.0,
            "reason": "race-a",
            "now": 0.0,
            "overrides": overrides,
        },
    )
    writer2 = ctx.Process(
        target=_spawn_record,
        args=(str(tmp_path), queue2),
        kwargs={
            "job_id": "job-3",
            "event_epoch": 120.0,
            "reason": "race-b",
            "now": 0.0,
            "overrides": overrides,
        },
    )
    writer1.start()
    writer2.start()
    writer1.join(10)
    writer2.join(10)
    assert writer1.exitcode == 0
    assert writer2.exitcode == 0

    results = [queue1.get(timeout=2)["result"], queue2.get(timeout=2)["result"]]
    observations = sorted(result["observation"] for result in results)
    assert observations == ["unknown", "valid"]
    assert sum(1 for result in results if result["changed"] is True) == 1
    assert sum(1 for result in results if "capacity-exceeded" in result["diagnostics"]) == 1
    assert len(_state_payload(tmp_path)["events"]) == 2
