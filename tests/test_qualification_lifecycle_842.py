from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from paulsha_cortex.coordinator import execution_adapters, execution_profile, model_resolution
from paulsha_cortex.coordinator.qualification_lifecycle import (
    QualificationConflict,
    QualificationStore,
    _safe_read_json,
)


FIXTURES = Path(__file__).parent / "fixtures" / "patchmud"
REPORT_PATH = FIXTURES / "report-v2" / "positive.json"
REPORT_REVISION = "421fadc7dc16b6ee9020bdc2333ff6fe856accaa"
NOW = "2026-09-26T00:00:01Z"


def _report() -> dict:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def _row(report: dict, profile_key: str | None = None) -> dict:
    rows = report["leaderboards"]["clear_rate"]["rows"]
    if profile_key is None:
        return rows[0]
    return next(row for row in rows if row["profile_id"] == profile_key)


def _import_fixture(
    store: QualificationStore,
    *,
    expected_revision: int = 0,
    idempotency_key: str = "fixture-import",
    test_only: bool = True,
) -> dict:
    report = _report()
    row = _row(report)
    artifact_digest = "sha256:" + hashlib.sha256(REPORT_PATH.read_bytes()).hexdigest()
    result = store.import_report(
        report,
        source_revision=REPORT_REVISION,
        source_artifact_digest=artifact_digest,
        profile_key=row["profile_id"],
        executor="scripted",
        model_id="fixer",
        role="build",
        test_only=test_only,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )
    envelope = json.loads(
        (store.paths.candidates_root / f"{result['candidate_id']}.json").read_text(encoding="utf-8")
    )
    return envelope["payload"]


def _test_candidate() -> dict:
    key = "epk:v1:resolved:" + "a" * 64
    return {
        "schema_version": 1,
        "test_only": True,
        "subject": {
            "executor": "copilot",
            "model_id": "fixture-model",
            "role": "build",
            "report_role": "builder",
        },
        "profile_key": key,
        "cohort": {
            "benchmark_type": "issue-resolution",
            "deck_digest": "sha256:" + "b" * 64,
            "evaluator_revision": "sha256:" + "c" * 64,
            "deck_id": "fixture-deck",
            "loadout": "fixture-loadout-v1",
        },
        "coverage": {
            "state": "complete",
            "expected_encounters": ["encounter-a", "encounter-b"],
            "observed_encounters": ["encounter-a", "encounter-b"],
        },
        "source": {
            "producer": "test-fixture",
            "schema_version": 2,
            "revision": "fixture-revision",
            "report_digest": "sha256:" + "d" * 64,
            "artifact_digest": "sha256:" + "e" * 64,
        },
        "profile_observation": {
            "state": "complete",
            "resolved_key": key,
            "actual_key": "epk:v1:actual:" + "f" * 64,
            "requested": {"effort": {"state": "known", "value": "high"}},
            "resolved": {"loadout": {"state": "known", "value": "fixture-loadout-v1"}},
            "observed": {"loadout": {"state": "known", "value": "fixture-loadout-v1"}},
        },
        "measurement": {
            "verdict": "pass",
            "evaluated_at": "2026-09-25T20:00:00Z",
            "measured": {"clear_rate": 1.0},
            "unknown": {},
        },
        "mapping": {"version": "qualification-report-mapping/v1"},
    }


def _complete_report_binding() -> tuple[dict, execution_adapters.ExecutionProfileBinding]:
    identity = SimpleNamespace(
        executor="copilot", model_id="fixture-model", independence_domain="fixture-builder"
    )
    binding = execution_adapters.resolve_profile(
        identity, "builder", effort="high", requirements={"role": "build"}
    )
    observed = execution_adapters.record_observed(
        binding,
        {
            "verified": True,
            "profile_key": binding.resolved_key,
            "source": "test-only:immutable-patchmud-fixture",
            "conditions": binding.resolved.to_dict()["conditions"],
        },
    )
    report = _report()
    board = report["leaderboards"]["clear_rate"]["rows"]
    target = board[0]
    old_key = target["profile_id"]
    new_key = binding.resolved_key
    loadout = binding.resolved.conditions["loadout"]["value"]["id"]
    target.update(
        {
            "profile_id": new_key,
            "model": "copilot:fixture-model",
            "loadout": loadout,
            "coverage_complete": True,
            "coverage_observed_encounters": list(target["coverage_expected_encounters"]),
            "runs": 2,
            "value": 0.5,
        }
    )
    exact_runs = [row for row in report["runs"] if row["profile_id"] == old_key]
    assert len(exact_runs) == 1
    exact = exact_runs[0]
    exact.update(
        {
            "profile_id": new_key,
            "model": "copilot:fixture-model",
            "loadout": loadout,
            "deck_coverage": {
                "complete": True,
                "expected_encounters": list(target["coverage_expected_encounters"]),
                "observed_encounters": list(target["coverage_expected_encounters"]),
            },
        }
    )
    replica = deepcopy(exact)
    replica["run_id"] = "report-fixer-replica"
    report["runs"].append(replica)
    report["runs_included"] = len(report["runs"])
    _refresh_report_fingerprint(report)
    return report, observed


def _refresh_report_fingerprint(report: dict) -> None:
    stable = {key: value for key, value in report.items() if key not in {"generated_at", "report_fingerprint"}}
    report["report_fingerprint"] = "sha256:" + hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _rekey_report(report: dict, old_key: str, new_key: str) -> None:
    for row in report["leaderboards"]["clear_rate"]["rows"]:
        if row["profile_id"] == old_key:
            row["profile_id"] = new_key
    for run in report["runs"]:
        if run["profile_id"] == old_key:
            run["profile_id"] = new_key
    _refresh_report_fingerprint(report)


def _import_bound_report(
    store: QualificationStore,
    report: dict,
    binding: execution_adapters.ExecutionProfileBinding,
    *,
    expected_revision: int,
    idempotency_key: str,
) -> dict:
    artifact = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return store.import_report(
        report,
        source_revision=REPORT_REVISION,
        source_artifact_digest="sha256:" + hashlib.sha256(artifact).hexdigest(),
        profile_key=binding.resolved_key,
        executor="copilot",
        model_id="fixture-model",
        role="build",
        profile_binding=binding.to_dict(),
        profile_source_revision=REPORT_REVISION,
        profile_source_artifact_digest="sha256:" + hashlib.sha256(
            json.dumps(binding.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        test_only=True,
    )


def _import_test_candidate(
    store: QualificationStore,
    *,
    expected_revision: int = 0,
    idempotency_key: str = "test-candidate",
) -> dict:
    result = store.import_test_candidate(
        _test_candidate(),
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )
    envelope = json.loads(
        (store.paths.candidates_root / f"{result['candidate_id']}.json").read_text(encoding="utf-8")
    )
    return envelope["payload"]


def _review(
    store: QualificationStore,
    candidate: dict,
    *,
    expected_revision: int,
    idempotency_key: str = "test-review",
    verdict: str = "approved",
    test_only: bool = True,
    now: str = NOW,
    reviewed_at: str = "2026-09-26T00:00:00Z",
    expires_at: str = "2026-09-27T00:00:00Z",
) -> dict:
    return store.review_candidate(
        candidate["candidate_id"],
        verdict=verdict,
        reviewer="fixture-reviewer",
        reviewer_authority="test-only" if test_only else "human-receipt:fixture",
        policy_revision="qualification-policy-v1",
        reviewed_at=reviewed_at,
        expires_at=expires_at,
        test_only=test_only,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        now=now,
    )


def _race_mutation(kind: str, root: str, candidate_id: str, barrier, result_queue) -> None:
    store = QualificationStore(root=Path(root))
    barrier.wait(timeout=10)
    try:
        if kind == "approve":
            result = store.review_candidate(
                candidate_id,
                verdict="approved",
                reviewer="fixture-reviewer",
                reviewer_authority="test-only",
                policy_revision="qualification-policy-v1",
                reviewed_at="2026-09-26T00:00:00Z",
                expires_at="2026-09-27T00:00:00Z",
                test_only=True,
                expected_revision=2,
                idempotency_key="race-approve",
                now=NOW,
            )
            result_queue.put((kind, "ok", result["revision"]))
        else:
            result = store.revoke_qualification(
                candidate_id,
                reviewer="fixture-reviewer",
                reviewer_authority="test-only",
                reason="競爭驗收",
                revoked_at=NOW,
                test_only=True,
                expected_revision=2,
                idempotency_key="race-revoke",
                now=NOW,
            )
            result_queue.put((kind, "ok", result["revision"]))
    except QualificationConflict:
        result_queue.put((kind, "conflict", None))


def test_q01_patchmud_fixture_candidate_receipt_roster_query_round_trip(tmp_path: Path) -> None:
    store = QualificationStore(root=tmp_path / "state")
    report_candidate = _import_fixture(store)
    row = _row(_report(), report_candidate["profile_key"])

    assert report_candidate["source"]["revision"] == REPORT_REVISION
    assert report_candidate["source"]["artifact_digest"] == (
        "sha256:" + hashlib.sha256(REPORT_PATH.read_bytes()).hexdigest()
    )
    assert report_candidate["profile_key"] == row["profile_id"]
    assert report_candidate["subject"]["report_role"] == row["role"] == "builder"
    assert report_candidate["coverage"]["state"] == "incomplete"
    assert report_candidate["profile_observation"]["state"] == "unknown"
    with pytest.raises(ValueError, match="not eligible for approved roster"):
        _review(store, report_candidate, expected_revision=1)
    assert store.query_qualification(
        "scripted", "fixer", report_candidate["profile_key"], "build", now=NOW,
        allow_test_receipts=True,
    ) is None

    report, binding = _complete_report_binding()
    imported = _import_bound_report(
        store, report, binding, expected_revision=1, idempotency_key="complete-round-trip"
    )
    candidate = json.loads(
        (store.paths.candidates_root / f"{imported['candidate_id']}.json").read_text(encoding="utf-8")
    )["payload"]
    assert candidate["source"]["schema_version"] == 2
    assert candidate["source"]["revision"] == REPORT_REVISION
    assert candidate["mapping"]["version"] == "execution-qualification-report-mapping/v1"
    assert candidate["coverage"]["state"] == "complete"
    assert candidate["profile_observation"]["resolved_key"] == candidate["profile_key"]
    assert candidate["profile_observation"]["actual_key"] == binding.actual_key
    receipt = _review(store, candidate, expected_revision=2)
    assert receipt["test_only"] is True
    roster = store.read_roster()
    live_eval_roster = model_resolution.parse_eval_roster(store.eval_roster_payload(roster))
    test_eval_roster = model_resolution.parse_eval_roster(
        store.eval_roster_payload(roster, include_test_only=True)
    )
    assert live_eval_roster.entry_for(
        "copilot", "fixture-model", role="build",
        execution_profile_key=candidate["profile_key"],
        cohort_identity={
            "role": candidate["subject"]["report_role"],
            "benchmark_type": candidate["cohort"]["benchmark_type"],
            "profile_id": candidate["profile_key"],
            "deck_digest": candidate["cohort"]["deck_digest"],
            "evaluator_revision": candidate["cohort"]["evaluator_revision"],
        },
    ) is None
    entry = test_eval_roster.entry_for(
        "copilot",
        "fixture-model",
        role="build",
        execution_profile_key=candidate["profile_key"],
        cohort_identity={
            "role": candidate["subject"]["report_role"],
            "benchmark_type": candidate["cohort"]["benchmark_type"],
            "profile_id": candidate["profile_key"],
            "deck_digest": candidate["cohort"]["deck_digest"],
            "evaluator_revision": candidate["cohort"]["evaluator_revision"],
        },
    )
    assert entry is not None and entry.qualified()
    qualification = store.query_qualification(
        "copilot",
        "fixture-model",
        candidate["profile_key"],
        "build",
        now=NOW,
        allow_test_receipts=True,
    )
    assert qualification is not None
    assert {
        key: qualification[key] for key in ("state", "profile_key", "role", "coverage", "revoked")
    } == {
        "state": "approved", "profile_key": candidate["profile_key"], "role": "build",
        "coverage": "complete", "revoked": False,
    }
    assert qualification["receipt"] == receipt["receipt_digest"]


@pytest.mark.parametrize(
    ("profile_key", "role", "message"),
    [
        ("epk:v1:resolved:" + "0" * 64, "build", "exact resolved profile"),
        (None, "review", "role does not match requested execution role"),
    ],
)
def test_q02_profile_and_role_mismatch_fails_closed(
    tmp_path: Path, profile_key: str | None, role: str, message: str
) -> None:
    store = QualificationStore(root=tmp_path / "state")
    report = _report()
    row = _row(report)
    with pytest.raises(ValueError, match=message):
        store.import_report(
            report,
            source_revision=REPORT_REVISION,
            source_artifact_digest="sha256:" + hashlib.sha256(REPORT_PATH.read_bytes()).hexdigest(),
            profile_key=profile_key or row["profile_id"],
            executor="scripted",
            model_id="fixer",
            role=role,
            expected_revision=0,
            idempotency_key="mismatch",
            test_only=True,
        )


def test_q02_model_loadout_adapter_and_deck_mismatches_fail_closed(tmp_path: Path) -> None:
    report, binding = _complete_report_binding()
    row = _row(report, binding.resolved_key)
    store = QualificationStore(root=tmp_path / "model")
    with pytest.raises(ValueError, match="exact executor/model identity"):
        store.import_report(
            report, source_revision=REPORT_REVISION, source_artifact_digest="sha256:" + "1" * 64,
            profile_key=binding.resolved_key, executor="copilot", model_id="different-model",
            role="build", profile_binding=binding.to_dict(), test_only=True,
            expected_revision=0, idempotency_key="model-mismatch",
        )

    loadout_report = deepcopy(report)
    _row(loadout_report, binding.resolved_key)["loadout"] = "other-loadout"
    for run in loadout_report["runs"]:
        if run["profile_id"] == binding.resolved_key:
            run["loadout"] = "other-loadout"
    _refresh_report_fingerprint(loadout_report)
    with pytest.raises(ValueError, match="loadout does not match report cohort"):
        _import_bound_report(
            QualificationStore(root=tmp_path / "loadout"), loadout_report, binding,
            expected_revision=0, idempotency_key="loadout-mismatch",
        )

    alternate = deepcopy(binding.to_dict())
    alternate["descriptor"]["adapter"]["protocol_version"] = "999"
    descriptor = execution_profile.parse_descriptor(alternate["descriptor"])
    for plane in ("requested", "resolved", "observed"):
        alternate[plane]["conditions"]["adapter"] = {
            "state": "known", "value": deepcopy(alternate["descriptor"]["adapter"])
        }
    parsed = {
        plane: execution_profile.parse_profile(alternate[plane], descriptor)
        for plane in ("requested", "resolved", "observed")
    }
    alternate["request_key"] = execution_profile.profile_key(parsed["requested"])
    alternate["resolved_key"] = execution_profile.profile_key(parsed["resolved"])
    alternate["actual_key"] = execution_profile.actual_condition_key(parsed["observed"])
    alternate_binding = execution_adapters.load_profile_binding(alternate)
    version_report = deepcopy(report)
    _rekey_report(version_report, binding.resolved_key, alternate_binding.resolved_key)
    with pytest.raises(ValueError, match="adapter/version does not match executor"):
        _import_bound_report(
            QualificationStore(root=tmp_path / "adapter-version"), version_report,
            alternate_binding, expected_revision=0, idempotency_key="adapter-version-mismatch",
        )

    deck_report = deepcopy(report)
    target_run = next(run for run in deck_report["runs"] if run["profile_id"] == binding.resolved_key)
    target_run["deck_digest"] = "sha256:" + "9" * 64
    _refresh_report_fingerprint(deck_report)
    with pytest.raises(ValueError, match="deck_digest does not match its cohort"):
        _import_bound_report(
            QualificationStore(root=tmp_path / "deck"), deck_report, binding,
            expected_revision=0, idempotency_key="deck-mismatch",
        )
    assert row["profile_id"] == binding.resolved_key


def test_q02_effort_and_coverage_mismatch_remain_unknown_and_unapprovable(tmp_path: Path) -> None:
    report, binding = _complete_report_binding()
    mismatched_conditions = binding.resolved.to_dict()["conditions"]
    mismatched_conditions["effort"] = {"state": "known", "value": "low"}
    effort_binding = execution_adapters.record_observed(
        binding,
        {
            "verified": True,
            "profile_key": binding.resolved_key,
            "source": "test-only:effort-mismatch",
            "conditions": mismatched_conditions,
        },
    )
    store = QualificationStore(root=tmp_path / "effort")
    imported = _import_bound_report(
        store, report, effort_binding, expected_revision=0, idempotency_key="effort-mismatch"
    )
    candidate = json.loads(
        (store.paths.candidates_root / f"{imported['candidate_id']}.json").read_text(encoding="utf-8")
    )["payload"]
    assert candidate["profile_observation"]["state"] == "unknown"
    with pytest.raises(ValueError, match="observed-profile-incomplete"):
        _review(store, candidate, expected_revision=1)

    coverage_report = deepcopy(report)
    target = _row(coverage_report, binding.resolved_key)
    target["coverage_observed_encounters"] = target["coverage_expected_encounters"][:-1]
    target["coverage_complete"] = False
    for run in coverage_report["runs"]:
        if run["profile_id"] == binding.resolved_key:
            run["deck_coverage"]["complete"] = False
            run["deck_coverage"]["observed_encounters"] = run["deck_coverage"]["expected_encounters"][:-1]
    _refresh_report_fingerprint(coverage_report)
    coverage_store = QualificationStore(root=tmp_path / "coverage")
    imported = _import_bound_report(
        coverage_store, coverage_report, binding,
        expected_revision=0, idempotency_key="coverage-mismatch",
    )
    coverage_candidate = json.loads(
        (coverage_store.paths.candidates_root / f"{imported['candidate_id']}.json").read_text(encoding="utf-8")
    )["payload"]
    assert coverage_candidate["coverage"]["state"] == "incomplete"
    with pytest.raises(ValueError, match="coverage-incomplete-or-unknown"):
        _review(coverage_store, coverage_candidate, expected_revision=1)


def test_q03_incomplete_unknown_and_legacy_rows_never_qualify(tmp_path: Path) -> None:
    store = QualificationStore(root=tmp_path / "state")
    candidate = _import_fixture(store)
    with pytest.raises(ValueError, match="coverage-incomplete-or-unknown"):
        _review(store, candidate, expected_revision=1)
    assert store.query_qualification(
        "scripted", "fixer", candidate["profile_key"], "build", now=NOW,
        allow_test_receipts=True,
    ) is None
    legacy_store = QualificationStore(root=tmp_path / "legacy-state")
    migrated = legacy_store.migrate_legacy_roster(
        {
            "schema_version": 2,
            "entries": [
                {
                    "executor": "scripted",
                    "model_id": "fixer",
                    "role": "builder",
                    "execution_profile_key": candidate["profile_key"],
                    "benchmark_type": candidate["cohort"]["benchmark_type"],
                    "deck_digest": candidate["cohort"]["deck_digest"],
                    "evaluator_revision": candidate["cohort"]["evaluator_revision"],
                    "verdict": "pass",
                    "evaluated_at": "2026-09-25T00:00:00Z",
                    "eval_source": "legacy-report",
                    "review_status": "approved",
                    "reviewer": "legacy-reviewer",
                    "reviewed_at": "2026-09-25T00:01:00Z",
                }
            ],
        },
        expected_revision=0,
        idempotency_key="legacy-import",
        source_ref="legacy-roster-fixture",
    )

    assert migrated["state"] == "unknown"
    assert migrated["migrated_rows"] == 1
    assert legacy_store.query_qualification(
        "scripted", "fixer", candidate["profile_key"], "build", now=NOW,
        allow_test_receipts=True,
    ) is None
    state = legacy_store.qualification_status(
        "scripted", "fixer", candidate["profile_key"], "build", now=NOW
    )
    assert state["state"] == "unknown"
    assert "legacy" in state["reason"]


def test_q04_pass_without_human_receipt_or_valid_receipt_never_grants(tmp_path: Path) -> None:
    store = QualificationStore(root=tmp_path / "state")
    candidate = _import_fixture(store)
    assert store.query_qualification(
        "scripted", "fixer", candidate["profile_key"], "build", now=NOW,
        allow_test_receipts=True,
    ) is None
    with pytest.raises(ValueError, match="reviewer"):
        store.review_candidate(
            candidate["candidate_id"],
            verdict="approved",
            reviewer="",
            reviewer_authority="test-only",
            policy_revision="qualification-policy-v1",
            reviewed_at="2026-09-26T00:00:00Z",
            expires_at="2026-09-27T00:00:00Z",
            test_only=True,
            expected_revision=1,
            idempotency_key="missing-reviewer",
            now=NOW,
        )
    with pytest.raises(ValueError, match="not eligible for approved roster"):
        store.review_candidate(
            candidate["candidate_id"], verdict="approved", reviewer="human-reviewer",
            reviewer_authority="test-only", policy_revision="qualification-policy-v1",
            reviewed_at="2026-09-26T00:00:00Z", expires_at="2026-09-27T00:00:00Z",
            test_only=True, expected_revision=1, idempotency_key="incomplete-human-approval", now=NOW,
        )
    rejected = _review(
        store,
        candidate,
        expected_revision=1,
        idempotency_key="reject",
        verdict="rejected",
    )
    assert rejected["state"] == "rejected"
    assert store.query_qualification(
        "scripted", "fixer", candidate["profile_key"], "build", now=NOW,
        allow_test_receipts=True,
    ) is None

    approved = _import_test_candidate(store, expected_revision=2)
    receipt = _review(store, approved, expected_revision=3, idempotency_key="approved")
    assert store.query_qualification(
        "copilot", "fixture-model", approved["profile_key"], "build", now=NOW
    ) is None
    receipt_path = store.paths.receipts_root / f"{receipt['receipt_id']}.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["candidate_id"] = "qcan:v1:" + "9" * 64
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    assert store.query_qualification(
        "copilot", "fixture-model", approved["profile_key"], "build", now=NOW,
        allow_test_receipts=True,
    ) is None


def test_q04_model_profile_apply_does_not_issue_qualification_receipt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from paulsha_cortex.porcelain import model_profile as porcelain_model

    store = QualificationStore(root=tmp_path / "state")
    monkeypatch.setattr(
        porcelain_model, "run_model_profile",
        lambda _options: {"applied": True, "cells": [], "registry_file": str(tmp_path / "models.yaml")},
    )
    assert porcelain_model.main(["profile", "--apply", "--registry-file", str(tmp_path / "models.yaml")]) == 0
    assert "已寫入" in capsys.readouterr().out
    assert store.revision == 0
    assert store.qualification_status(
        "copilot", "fixture-model", _test_candidate()["profile_key"], "build", now=NOW
    )["state"] == "unknown"


def test_q04_model_qualification_cli_help_documents_review_receipt_controls(capsys) -> None:
    from paulsha_cortex.porcelain import model_profile as porcelain_model

    with pytest.raises(SystemExit) as help_exit:
        porcelain_model.main(["--help"])
    assert help_exit.value.code == 0
    model_help = capsys.readouterr().out
    assert "qualification" in model_help and "profile" in model_help

    with pytest.raises(SystemExit) as qualification_exit:
        porcelain_model.main(["qualification", "review", "--help"])
    assert qualification_exit.value.code == 0
    qualification_help = capsys.readouterr().out
    assert all(value in qualification_help for value in (
        "--authority-ref", "--policy-revision", "--expires-at", "--expected-revision", "--test-only"
    ))


def test_q05_fake_clock_expiry_revoke_timezone_and_clock_rollback(tmp_path: Path) -> None:
    store = QualificationStore(root=tmp_path / "state")
    candidate = _import_test_candidate(store)
    _review(store, candidate, expected_revision=1)
    not_yet_effective = store.qualification_status(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-25T23:59:59Z", allow_test_receipts=True,
    )
    assert not_yet_effective == {"state": "unknown", "reason": "review-time-in-future"}
    active = store.query_qualification(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-26T12:00:00+12:00", allow_test_receipts=True,
    )
    assert active is not None and active["coverage"] == "complete"

    expired = store.qualification_status(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-27T00:00:00Z", allow_test_receipts=True,
    )
    assert expired["state"] == "expired"
    rolled_back = store.query_qualification(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-26T12:00:00Z", allow_test_receipts=True,
    )
    assert rolled_back is None
    assert store.qualification_status(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-26T12:00:00Z", allow_test_receipts=True,
    )["state"] == "unknown"

    # 新 generation 可撤銷；撤銷後不會由舊 receipt 重播恢復。
    store.revoke_qualification(
        candidate["candidate_id"],
        reviewer="fixture-reviewer",
        reviewer_authority="test-only",
        reason="撤銷測試",
        revoked_at="2026-09-27T00:00:01Z",
        test_only=True,
        expected_revision=2,
        idempotency_key="revoke-expired",
        now="2026-09-27T00:00:01Z",
    )
    assert store.qualification_status(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-27T00:00:02Z",
    )["state"] == "revoked"

    for timestamp in ("2026-09-26T00:00:00", "2026-02-30T00:00:00Z", "yesterday"):
        with pytest.raises(ValueError, match="timestamp"):
            store.review_candidate(
                candidate["candidate_id"],
                verdict="approved",
                reviewer="fixture-reviewer",
                reviewer_authority="test-only",
                policy_revision="qualification-policy-v1",
                reviewed_at=timestamp,
                expires_at="2026-09-28T00:00:00Z",
                test_only=True,
                expected_revision=3,
                idempotency_key="invalid-time-" + timestamp,
                now="2026-09-27T00:00:02Z",
            )


def test_q06_idempotency_cas_and_old_receipt_replay_do_not_revive(tmp_path: Path) -> None:
    store = QualificationStore(root=tmp_path / "state")
    first = _import_test_candidate(store, idempotency_key="import-1")
    assert _import_test_candidate(store, idempotency_key="import-1")["candidate_id"] == first["candidate_id"]
    approved = _review(store, first, expected_revision=1, idempotency_key="approve-1")
    replay = _review(store, first, expected_revision=1, idempotency_key="approve-1")
    assert replay["receipt_id"] == approved["receipt_id"]

    revoked = store.revoke_qualification(
        first["candidate_id"],
        reviewer="fixture-reviewer",
        reviewer_authority="test-only",
        reason="replay test",
        revoked_at=NOW,
        test_only=True,
        expected_revision=2,
        idempotency_key="revoke-1",
        now=NOW,
    )
    assert store.revoke_qualification(
        first["candidate_id"],
        reviewer="fixture-reviewer",
        reviewer_authority="test-only",
        reason="replay test",
        revoked_at=NOW,
        test_only=True,
        expected_revision=2,
        idempotency_key="revoke-1",
        now=NOW,
    )["receipt_id"] == revoked["receipt_id"]
    assert _review(store, first, expected_revision=1, idempotency_key="approve-1")["receipt_id"] == approved["receipt_id"]
    assert store.qualification_status(
        "copilot", "fixture-model", first["profile_key"], "build", now=NOW
    )["state"] == "revoked"
    with pytest.raises(QualificationConflict, match="idempotency"):
        _review(store, first, expected_revision=2, idempotency_key="approve-1")
    with pytest.raises(QualificationConflict, match="revision"):
        _import_test_candidate(store, expected_revision=0, idempotency_key="stale-new-request")


def test_q07_two_process_publish_revoke_compete_with_one_cas_winner(tmp_path: Path) -> None:
    store = QualificationStore(root=tmp_path / "state")
    candidate = _import_test_candidate(store)
    _review(store, candidate, expected_revision=1)
    candidate_path = store.paths.candidates_root / f"{candidate['candidate_id']}.json"
    # Worker arguments use this fixed path only after the parent confirms its immutable id.
    fixed_candidate_id = candidate["candidate_id"]
    assert fixed_candidate_id.startswith("qcan:v1:")

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_race_mutation,
            args=(kind, str(tmp_path / "state"), candidate["candidate_id"], barrier, result_queue),
        )
        for kind in ("approve", "revoke")
    ]
    for process in processes:
        process.start()
    results = [result_queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sum(result[1] == "ok" for result in results) == 1
    assert sum(result[1] == "conflict" for result in results) == 1
    assert store.revision == 3
    # The final roster points to one current generation with a durable matching receipt.
    state = store.qualification_status(
        "copilot", "fixture-model", candidate["profile_key"], "build", now=NOW,
        allow_test_receipts=True,
    )
    assert state["state"] in {"approved", "revoked"}
    assert store.verify_current_binding("copilot", "fixture-model", candidate["profile_key"], "build")
    assert candidate_path.is_file()


@pytest.mark.parametrize("stage", ["candidate_written", "receipt_written", "index_written", "roster_written", "revoke_index_written"])
def test_q08_crash_failpoints_reconcile_without_half_grant(tmp_path: Path, stage: str) -> None:
    root = tmp_path / stage
    fired = False

    def failpoint(current: str) -> None:
        nonlocal fired
        if current == stage and not fired:
            fired = True
            raise RuntimeError("simulated crash")

    store = QualificationStore(root=root, failpoint=failpoint)
    if stage == "candidate_written":
        with pytest.raises(RuntimeError, match="simulated crash"):
            _import_test_candidate(store)
        restarted = QualificationStore(root=root)
        restarted.reconcile()
        assert restarted.revision == 0
        assert list(restarted.paths.candidates_root.glob("*.json")) == []
        candidate = _import_test_candidate(restarted)
        assert candidate["test_only"] is True
        return

    seed_store = QualificationStore(root=root)
    candidate = _import_test_candidate(seed_store)
    if stage in {"receipt_written", "index_written", "roster_written"}:
        with pytest.raises(RuntimeError, match="simulated crash"):
            _review(store, candidate, expected_revision=1)
        restarted = QualificationStore(root=root)
        restarted.reconcile()
        if stage == "receipt_written":
            assert restarted.qualification_status(
                "copilot", "fixture-model", candidate["profile_key"], "build", now=NOW
            )["state"] == "unknown"
            _review(restarted, candidate, expected_revision=1)
        else:
            assert restarted.query_qualification(
                "copilot", "fixture-model", candidate["profile_key"], "build",
                now=NOW, allow_test_receipts=True,
            ) is not None
    else:
        _review(seed_store, candidate, expected_revision=1)
        store = QualificationStore(root=root, failpoint=failpoint)
        with pytest.raises(RuntimeError, match="simulated crash"):
            store.revoke_qualification(
                candidate["candidate_id"],
                reviewer="fixture-reviewer",
                reviewer_authority="test-only",
                reason="crash during revoke",
                revoked_at=NOW,
                test_only=True,
                expected_revision=2,
                idempotency_key="crash-revoke",
                now=NOW,
            )
        restarted = QualificationStore(root=root)
        restarted.reconcile()
        assert restarted.qualification_status(
            "copilot", "fixture-model", candidate["profile_key"], "build", now=NOW
        )["state"] == "revoked"


def test_q08_state_reads_reject_symlinks_and_hardlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"state":"approved"}\n', encoding="utf-8")
    target.chmod(0o600)
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)
    with pytest.raises(ValueError, match="unreadable"):
        _safe_read_json(alias, label="qualification receipt")

    hardlink = tmp_path / "hardlink.json"
    os.link(target, hardlink)
    with pytest.raises(ValueError, match="single-link"):
        _safe_read_json(target, label="qualification receipt")


def test_q09_lifecycle_and_legacy_migration_leave_history_bytes_unchanged(tmp_path: Path) -> None:
    history = tmp_path / "attempt-evidence-decision.json"
    history.write_bytes(b'{"attempt":"frozen","evidence":"frozen","decision":"frozen"}\n')
    before = hashlib.sha256(history.read_bytes()).hexdigest()
    store = QualificationStore(root=tmp_path / "state")
    candidate = _import_test_candidate(store)
    _review(store, candidate, expected_revision=1)
    store.revoke_qualification(
        candidate["candidate_id"], reviewer="fixture-reviewer",
        reviewer_authority="test-only", reason="history check", revoked_at=NOW,
        test_only=True, expected_revision=2, idempotency_key="history-revoke", now=NOW,
    )
    store.reconcile()
    migrated = QualificationStore(root=tmp_path / "migration-state")
    migrated.migrate_legacy_roster(
        {"schema_version": 1, "entries": []}, expected_revision=0,
        idempotency_key="history-preserving-migration", source_ref="legacy-fixture",
    )
    assert hashlib.sha256(history.read_bytes()).hexdigest() == before


def test_q09_late_report_cannot_replace_the_current_generation(tmp_path: Path) -> None:
    store = QualificationStore(root=tmp_path / "state")
    report = _report()
    newer = deepcopy(report)
    newer["generated_at"] = "2026-09-26T00:00:00Z"
    older = deepcopy(report)
    older["generated_at"] = "2026-09-25T00:00:00Z"

    def add(report_payload: dict, revision: int, idem: str) -> dict:
        artifact = json.dumps(report_payload, ensure_ascii=False, sort_keys=True).encode()
        return store.import_report(
            report_payload, source_revision=REPORT_REVISION,
            source_artifact_digest="sha256:" + hashlib.sha256(artifact).hexdigest(),
            profile_key=_row(report_payload)["profile_id"], executor="scripted", model_id="fixer",
            role="build", test_only=True, expected_revision=revision, idempotency_key=idem,
        )

    current = add(newer, 0, "newer-report")
    late = add(older, 1, "late-report")
    assert current["candidate_id"] != late["candidate_id"]
    with pytest.raises(QualificationConflict, match="late report"):
        _review(store, json.loads(
            (store.paths.candidates_root / f"{late['candidate_id']}.json").read_text(encoding="utf-8")
        )["payload"], expected_revision=2, idempotency_key="review-late-report")
    roster = store.read_roster()
    assert len(roster["entries"]) == 1
    assert roster["entries"][0]["candidate_id"] == current["candidate_id"]


def test_q10_pricing_and_timestamp_do_not_become_execution_qualification(tmp_path: Path) -> None:
    report = _report()
    key = _row(report)["profile_id"]
    report["generated_at"] = "2035-01-01T00:00:00Z"
    report["pricing"] = {"snapshot": "price-only-change"}
    stable = {k: v for k, v in report.items() if k not in {"generated_at", "report_fingerprint"}}
    report["report_fingerprint"] = "sha256:" + hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    row = _row(report)
    deck = {
        "deck_id": row["deck_id"],
        "content_sha256": row["deck_digest"].removeprefix("sha256:"),
        "encounter_count": len(row["coverage_expected_encounters"]),
        "measured_personas": [row["role"]],
    }
    consumed = execution_adapters.profile_report_consumer(
        report,
        expected_profile_key=key,
        source_revision=REPORT_REVISION,
        source_digest=report["report_fingerprint"],
        envelope_context={
            "executor": "scripted", "model_id": "fixer", "persona": "builder",
            "deck": deck, "patchmud_version": report["producer"]["version"],
            "role": row["role"], "benchmark_type": row["benchmark_type"],
            "deck_digest": row["deck_digest"], "evaluator_revision": row["evaluator_revision"],
        },
    )
    assert consumed["envelope_mapping"]["provenance"]["observation"]["profile_id"] == key
    store = QualificationStore(root=tmp_path / "state")
    result = store.import_report(
        report, source_revision=REPORT_REVISION,
        source_artifact_digest="sha256:" + "1" * 64,
        profile_key=key, executor="scripted", model_id="fixer", role="build",
        test_only=True, expected_revision=0, idempotency_key="price-only",
    )
    candidate = json.loads(
        (store.paths.candidates_root / f"{result['candidate_id']}.json").read_text(encoding="utf-8")
    )["payload"]
    assert candidate["profile_key"] == key
    assert candidate["source"]["report_digest"] == report["report_fingerprint"]


def test_q10_profile_key_and_permissions_remain_separate_from_qualification() -> None:
    identity = SimpleNamespace(
        executor="copilot", model_id="fixture-model", capabilities=("build",),
        independence_domain="builder-domain",
    )
    high = execution_adapters.resolve_profile(
        identity, "builder", effort="high", requirements={"role": "build"}
    )
    low = execution_adapters.resolve_profile(
        identity, "builder", effort="low", requirements={"role": "build"}
    )
    assert high.resolved_key != low.resolved_key
    assert high.resolved.conditions["permissions"] == low.resolved.conditions["permissions"]
    assert high.resolved.conditions["sandbox"] == low.resolved.conditions["sandbox"]

    payload = high.to_dict()
    for record in (payload["descriptor"], payload["requested"], payload["resolved"], payload["observed"]):
        metadata = record.setdefault("metadata", {})
        metadata["pricing"] = {"snapshot": "test-only-price"}
        metadata["timestamps"] = {"recorded_at": "2035-01-01T00:00:00Z"}
    metadata_binding = execution_adapters.load_profile_binding(payload)
    assert metadata_binding.resolved_key == high.resolved_key
    assert metadata_binding.actual_key == high.actual_key

    before = high.to_dict()
    execution_adapters.validate_dispatch_requirements(
        high, identity=identity, qualification_required=True,
        qualification={
            "state": "approved", "profile_key": high.resolved_key, "role": "build",
            "coverage": "complete", "receipt": "sha256:" + "a" * 64, "revoked": False,
        },
    )
    assert high.to_dict() == before


def test_q11_immutable_patchmud_fixtures_consume_without_runtime_import(
    tmp_path: Path, monkeypatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def deny_patchmud_runtime(name, *args, **kwargs):
        if name == "patchmud" or name.startswith("patchmud."):
            raise AssertionError("Cortex qualification consumer must not import PatchMUD runtime")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_patchmud_runtime)
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    records = {
        row["path"]: row
        for row in manifest["fixtures"]
        if row["path"] in {
            "report-v2/positive.json",
            "execution-profile-v1/positive.json",
        }
    }
    for relative, record in records.items():
        assert hashlib.sha256((FIXTURES / relative).read_bytes()).hexdigest() == record["sha256"]
    assert manifest["source_revision"] == REPORT_REVISION

    profile_payload = json.loads(
        (FIXTURES / "execution-profile-v1" / "positive.json").read_text(encoding="utf-8")
    )
    descriptor = execution_adapters.schema.parse_descriptor(profile_payload["descriptor"])
    resolved = execution_adapters.schema.parse_profile(profile_payload["resolved"], descriptor)
    observed = execution_adapters.schema.parse_profile(profile_payload["observed"], descriptor)
    assert execution_adapters.schema.profile_key(resolved) == profile_payload["resolved_key"]
    assert execution_adapters.schema.actual_condition_key(observed) is None
    report = _report()
    row = _row(report)
    context = {
        "executor": "scripted", "model_id": "fixer", "persona": "builder",
        "deck": {
            "deck_id": row["deck_id"],
            "content_sha256": row["deck_digest"].removeprefix("sha256:"),
            "encounter_count": len(row["coverage_expected_encounters"]),
            "measured_personas": [row["role"]],
        },
        "patchmud_version": report["producer"]["version"],
        "role": row["role"], "benchmark_type": row["benchmark_type"],
        "deck_digest": row["deck_digest"], "evaluator_revision": row["evaluator_revision"],
    }
    consumed = execution_adapters.profile_report_consumer(
        report, expected_profile_key=row["profile_id"],
        source_revision=manifest["source_revision"],
        source_digest=report["report_fingerprint"], envelope_context=context,
    )
    assert consumed["source_revision"] == manifest["source_revision"]
    assert consumed["envelope_mapping"]["provenance"]["registry_writable"] is False
    store = QualificationStore(root=tmp_path / "state")
    candidate = _import_fixture(store)
    assert candidate["source"]["artifact_digest"] == "sha256:" + records[
        "report-v2/positive.json"
    ]["sha256"]
    with pytest.raises(ValueError, match="execution profile binding"):
        store.import_report(
            report, source_revision=manifest["source_revision"],
            source_artifact_digest=candidate["source"]["artifact_digest"],
            profile_key=row["profile_id"], executor="scripted", model_id="fixer",
            role="build", profile_binding=profile_payload,
            profile_source_revision=manifest["source_revision"],
            profile_source_artifact_digest="sha256:" + records[
                "execution-profile-v1/positive.json"
            ]["sha256"],
            expected_revision=1, idempotency_key="profile-fixture-mismatch",
            test_only=True,
        )


def test_manager_reads_exact_roster_query_not_identity_attribute(monkeypatch) -> None:
    from types import SimpleNamespace

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator import qualification_lifecycle

    identity = SimpleNamespace(
        executor="copilot", model_id="fixture-model", capabilities=("build",),
        independence_domain="builder-a",
        execution_qualification={"state": "approved", "profile_key": "spoofed"},
    )
    launcher = SimpleNamespace(executor="copilot", model="fixture-model")
    run = SimpleNamespace(
        steps=[SimpleNamespace(phase="build", gate_result="passed", commit_policy="required", domain="builder-a")],
        model_chain_override={}, sizing_band="red",
    )
    step = SimpleNamespace(persona="builder")
    queried = []

    def lookup(selected_identity, binding, *, now=None):
        queried.append((selected_identity, binding.resolved_key))
        return {
            "state": "approved", "profile_key": binding.resolved_key,
            "role": "build", "coverage": "complete", "receipt": "sha256:" + "a" * 64,
            "revoked": False,
        }

    monkeypatch.setattr(qualification_lifecycle, "lookup_dispatch_qualification", lookup)
    binding, _ = manager._bind_workflow_execution_profile(
        run, step, identity, launcher, qualification_policy="enforce"
    )
    assert queried == [(identity, binding.resolved_key)]

    def no_roster_entry(selected_identity, binding, *, now=None):
        return None

    monkeypatch.setattr(qualification_lifecycle, "lookup_dispatch_qualification", no_roster_entry)
    with pytest.raises(execution_adapters.ExecutionAdapterError, match="qualification is unknown"):
        manager._bind_workflow_execution_profile(
            run, step, identity, launcher, qualification_policy="enforce"
        )
