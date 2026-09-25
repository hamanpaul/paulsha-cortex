"""Focused journal concurrency and durability coverage for #983."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import time
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import work_actions


_MISSING = object()


def _run_row(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "claim_key": f"{run_id}-claim",
        "snapshot_hash": f"{run_id}-snapshot",
        "source_revisions": [f"{run_id}@authority"],
        "provider_revision": f"{run_id}-provider",
        "authority_digest": f"{run_id}-authority",
        "workflow_step_ids": [f"{run_id}:build:tdd-red"],
    }


def _canonical_payload(payload: object) -> tuple[object, str]:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return json.loads(canonical), hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _publication_entry(payload: object) -> dict[str, object]:
    normalized, digest = _canonical_payload(payload)
    return {"payload": normalized, "payload_sha256": digest}


def _publication_event(
    event_id: str,
    *,
    kind: str,
    intent_payload: object | None = None,
    result_payload: object = _MISSING,
) -> dict[str, object]:
    event: dict[str, object] = {
        "event_id": event_id,
        "kind": kind,
        "intent": _publication_entry(intent_payload),
    }
    if result_payload is not _MISSING:
        event["result"] = _publication_entry(result_payload)
    return event


def _raw_journal(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _initialize_run(path: Path, run_id: str = "run-a") -> dict[str, object]:
    state = work_actions._load_runs(path)
    state["runs"][run_id] = _run_row(run_id)
    work_actions._save_runs(path, state)
    return state


def _hold_delivery_journal_lock(path_text: str, ready: multiprocessing.synchronize.Event) -> None:
    path = Path(path_text)
    with work_actions._DeliveryJournalLock(path):
        ready.set()
        time.sleep(0.3)


def test_stale_snapshot_conflicts_before_overwriting_another_run(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    first_writer = work_actions._load_runs(journal_path)
    stale_writer = work_actions._load_runs(journal_path)

    first_writer["runs"]["run-a"] = _run_row("run-a")
    stale_writer["runs"]["run-b"] = _run_row("run-b")

    work_actions._save_runs(journal_path, first_writer)

    with pytest.raises(RuntimeError, match="stale|conflict"):
        work_actions._save_runs(journal_path, stale_writer)

    persisted = work_actions._load_runs(journal_path)
    assert set(persisted["runs"]) == {"run-a"}
    assert persisted["runs"]["run-a"] == first_writer["runs"]["run-a"]


def test_stale_same_run_conflicts_without_replacing_confirmed_row(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    first_writer = work_actions._load_runs(journal_path)
    stale_writer = work_actions._load_runs(journal_path)

    first_writer["runs"]["run-a"] = _run_row("run-a")
    stale_writer["runs"]["run-a"] = {
        **_run_row("run-a"),
        "authority_digest": "run-a-authority-changed",
    }

    work_actions._save_runs(journal_path, first_writer)

    with pytest.raises(RuntimeError, match="stale|conflict"):
        work_actions._save_runs(journal_path, stale_writer)

    persisted = work_actions._load_runs(journal_path)
    assert persisted["runs"]["run-a"]["authority_digest"] == "run-a-authority"


def test_fresh_reload_after_conflict_preserves_both_rows(tmp_path: Path) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    first_writer = work_actions._load_runs(journal_path)
    stale_writer = work_actions._load_runs(journal_path)

    first_writer["runs"]["run-a"] = _run_row("run-a")
    stale_writer["runs"]["run-b"] = _run_row("run-b")
    work_actions._save_runs(journal_path, first_writer)

    with pytest.raises(RuntimeError, match="stale|conflict"):
        work_actions._save_runs(journal_path, stale_writer)

    fresh = work_actions._load_runs(journal_path)
    fresh["runs"]["run-b"] = _run_row("run-b")
    work_actions._save_runs(journal_path, fresh)

    persisted = work_actions._load_runs(journal_path)
    assert set(persisted["runs"]) == {"run-a", "run-b"}


def test_legacy_revisionless_journal_loads_and_upgrades_on_first_change(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema": "cortex-delivery-journal/v1",
                "runs": {"run-a": _run_row("run-a")},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    loaded = work_actions._load_runs(journal_path)
    assert loaded["revision"] == 0
    loaded["runs"]["run-b"] = _run_row("run-b")

    work_actions._save_runs(journal_path, loaded)

    persisted = _raw_journal(journal_path)
    assert persisted["revision"] == 1
    assert set(persisted["runs"]) == {"run-a", "run-b"}


def test_lock_is_stable_across_processes_and_times_out_when_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    state = work_actions._load_runs(journal_path)
    state["runs"]["run-a"] = _run_row("run-a")

    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    holder = ctx.Process(
        target=_hold_delivery_journal_lock,
        args=(str(journal_path), ready),
    )
    holder.start()
    try:
        assert ready.wait(timeout=2)
        monkeypatch.setattr(
            work_actions, "_DELIVERY_JOURNAL_LOCK_TIMEOUT_SECONDS", 0.05
        )
        with pytest.raises(RuntimeError, match="lock timeout"):
            work_actions._save_runs(journal_path, state)
    finally:
        holder.join(timeout=2)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2)
    assert holder.exitcode == 0

    work_actions._save_runs(journal_path, state)
    assert set(work_actions._load_runs(journal_path)["runs"]) == {"run-a"}


def test_ordinary_save_rejects_direct_publication_event_addition(tmp_path: Path) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    _initialize_run(journal_path)

    state = work_actions._load_runs(journal_path)
    state["runs"]["run-a"]["publication_events"] = {
        "pr-open": _publication_event(
            "pr-open",
            kind="manager_pull_request",
            intent_payload={"kind": "intent", "pr": 8},
        )
    }

    with pytest.raises(RuntimeError, match="publication-entry-conflict"):
        work_actions._save_runs(journal_path, state)

    persisted = _raw_journal(journal_path)
    assert "publication_events" not in persisted["runs"]["run-a"]


def test_ordinary_save_rejects_result_only_publication_forgery(tmp_path: Path) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    _initialize_run(journal_path)

    state = work_actions._load_runs(journal_path)
    state["runs"]["run-a"]["publication_events"] = {
        "pr-open": {
            "event_id": "pr-open",
            "kind": "manager_pull_request",
            "result": _publication_entry({"kind": "result", "pr": 8}),
        }
    }

    with pytest.raises(ValueError, match="malformed"):
        work_actions._save_runs(journal_path, state)


def test_ordinary_save_rejects_publication_replacement_and_removal(tmp_path: Path) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    _initialize_run(journal_path)
    intent_payload = {"kind": "intent", "pr": 8, "head": "a" * 40}
    result_payload = {"kind": "result", "pr": 8, "state": "open"}
    work_actions._append_delivery_publication_event(
        journal_path,
        run_id="run-a",
        event_id="pr-open",
        event_kind="manager_pull_request",
        entry_kind="intent",
        payload=intent_payload,
    )
    work_actions._append_delivery_publication_event(
        journal_path,
        run_id="run-a",
        event_id="pr-open",
        event_kind="manager_pull_request",
        entry_kind="result",
        payload=result_payload,
    )

    replacement = work_actions._load_runs(journal_path)
    replacement["runs"]["run-a"]["publication_events"]["pr-open"]["result"] = _publication_entry(
        {"kind": "result", "pr": 8, "state": "changed"}
    )
    with pytest.raises(RuntimeError, match="publication-entry-conflict"):
        work_actions._save_runs(journal_path, replacement)

    removal = work_actions._load_runs(journal_path)
    removal["runs"]["run-a"].pop("publication_events")
    with pytest.raises(RuntimeError, match="publication-entry-conflict"):
        work_actions._save_runs(journal_path, removal)

    persisted = _raw_journal(journal_path)["runs"]["run-a"]["publication_events"]
    assert persisted["pr-open"]["intent"] == _publication_entry(intent_payload)
    assert persisted["pr-open"]["result"] == _publication_entry(result_payload)


def test_append_delivery_publication_event_enforces_replay_and_order(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    _initialize_run(journal_path)
    base_revision = _raw_journal(journal_path)["revision"]

    intent = {"kind": "intent", "pr": 8, "head": "a" * 40}
    first = work_actions._append_delivery_publication_event(
        journal_path,
        run_id="run-a",
        event_id="pr-open",
        event_kind="manager_pull_request",
        entry_kind="intent",
        payload=intent,
    )
    assert first["outcome"] == "committed"
    assert first["revision"] == base_revision + 1

    replay = work_actions._append_delivery_publication_event(
        journal_path,
        run_id="run-a",
        event_id="pr-open",
        event_kind="manager_pull_request",
        entry_kind="intent",
        payload=intent,
    )
    assert replay["outcome"] == "committed"
    assert replay["revision"] == first["revision"]

    changed_intent = work_actions._append_delivery_publication_event(
        journal_path,
        run_id="run-a",
        event_id="pr-open",
        event_kind="manager_pull_request",
        entry_kind="intent",
        payload={"kind": "intent", "pr": 8, "head": "b" * 40},
    )
    assert changed_intent == {"outcome": "conflict", "reason": "payload-conflict"}

    result_before_intent = work_actions._append_delivery_publication_event(
        journal_path,
        run_id="run-a",
        event_id="pr-close",
        event_kind="manager_pull_request",
        entry_kind="result",
        payload={"kind": "result", "merged": True},
    )
    assert result_before_intent == {
        "outcome": "conflict",
        "reason": "result-before-intent",
    }

    result_payload = {"kind": "result", "merged": True}
    result = work_actions._append_delivery_publication_event(
        journal_path,
        run_id="run-a",
        event_id="pr-open",
        event_kind="manager_pull_request",
        entry_kind="result",
        payload=result_payload,
    )
    assert result["outcome"] == "committed"
    assert result["revision"] == first["revision"] + 1

    replay_result = work_actions._append_delivery_publication_event(
        journal_path,
        run_id="run-a",
        event_id="pr-open",
        event_kind="manager_pull_request",
        entry_kind="result",
        payload=result_payload,
    )
    assert replay_result["outcome"] == "committed"
    assert replay_result["revision"] == result["revision"]

    changed_result = work_actions._append_delivery_publication_event(
        journal_path,
        run_id="run-a",
        event_id="pr-open",
        event_kind="manager_pull_request",
        entry_kind="result",
        payload={"kind": "result", "merged": False},
    )
    assert changed_result == {"outcome": "conflict", "reason": "payload-conflict"}


@pytest.mark.parametrize(
    ("stage", "visible_after_unknown"),
    [
        ("after-file-fsync", False),
        ("after-replace", True),
        ("after-directory-fsync", True),
    ],
)
def test_append_unknown_retries_resolve_exact_event_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    visible_after_unknown: bool,
) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    _initialize_run(journal_path)
    base_revision = _raw_journal(journal_path)["revision"]
    payload = {"kind": "intent", "pr": 8, "head": "a" * 40}

    def hook(*, stage: str, **_: object) -> None:
        if stage == stage_to_fail:
            raise RuntimeError("injected failure")

    stage_to_fail = stage
    monkeypatch.setattr(work_actions, "_DELIVERY_JOURNAL_TEST_HOOK", hook)
    unknown = work_actions._append_delivery_publication_event(
        journal_path,
        run_id="run-a",
        event_id="pr-open",
        event_kind="manager_pull_request",
        entry_kind="intent",
        payload=payload,
    )
    assert unknown["outcome"] == "unknown"

    current = work_actions._load_runs(journal_path)
    event_visible = (
        current["runs"]["run-a"].get("publication_events", {}).get("pr-open") is not None
    )
    assert event_visible is visible_after_unknown

    monkeypatch.setattr(work_actions, "_DELIVERY_JOURNAL_TEST_HOOK", None)
    retry = work_actions._append_delivery_publication_event(
        journal_path,
        run_id="run-a",
        event_id="pr-open",
        event_kind="manager_pull_request",
        entry_kind="intent",
        payload=payload,
    )
    assert retry["outcome"] == "committed"
    expected_revision = base_revision + 1
    assert retry["revision"] == expected_revision
    persisted = _raw_journal(journal_path)
    assert persisted["revision"] == expected_revision
    assert (
        persisted["runs"]["run-a"]["publication_events"]["pr-open"]["intent"]
        == _publication_entry(payload)
    )


def test_legacy_save_unknown_exception_blocks_follow_up_merge_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    journal_path = tmp_path / "delivery-journal.json"
    state = work_actions._load_runs(journal_path)
    state["runs"]["run-a"] = _run_row("run-a")
    side_effects: list[str] = []

    def hook(*, stage: str, **_: object) -> None:
        if stage == "after-directory-fsync":
            raise RuntimeError("persist-then-raise")

    monkeypatch.setattr(work_actions, "_DELIVERY_JOURNAL_TEST_HOOK", hook)

    def legacy_caller() -> None:
        work_actions._save_runs(journal_path, state)
        side_effects.append("merge")

    with pytest.raises(RuntimeError, match="outcome unknown"):
        legacy_caller()

    assert side_effects == []
    monkeypatch.setattr(work_actions, "_DELIVERY_JOURNAL_TEST_HOOK", None)
    persisted = work_actions._load_runs(journal_path)
    assert set(persisted["runs"]) == {"run-a"}
