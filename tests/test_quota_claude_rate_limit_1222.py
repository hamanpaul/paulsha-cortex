"""Issue #1222：Claude Code rate_limit_event 額度觀測與終局 job 收割。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager, quota_admission, quota_observation as schema
from paulsha_cortex.coordinator.quota_ledger import QuotaEventLedger
from paulsha_cortex.coordinator.quota_reservation import QuotaReservationAuthority
from paulsha_cortex.coordinator.quota_shadow import QuotaShadowService
from paulsha_cortex.porcelain import quota as quota_cli
from paulsha_cortex.coordinator.quota_sources import ProviderQuotaTarget, capture_provider_quota


_NOW = 1_800_000_000_000
_PROFILE = "epk:v1:resolved:" + "a" * 64
_MODEL = "claude-sonnet-4"
_SEMANTICS = "provider:anthropic-claude-code/rate-limit-percent/v1"


def _descriptor():
    return schema.parse_pool_descriptor({
        "schema_version": 1, "authority_id": "operator-budget-authority",
        "account_id": "claude-account", "pool_id": "claude-pool", "revision": "1",
        "authority_ref": "fixture:operator-pool-map/v1", "provenance_refs": ["fixture:pool-map/v1"],
        "units": [{"unit_id": "percent", "version": "1", "quantity_kind": "amount",
                   "semantics_ref": _SEMANTICS}],
        "windows": [
            {"window_id": "five_hour", "kind": "rolling", "unit_ref": {"unit_id": "percent", "version": "1"},
             "duration_ms": 18_000_000},
            {"window_id": "seven_day", "kind": "rolling", "unit_ref": {"unit_id": "percent", "version": "1"},
             "duration_ms": 604_800_000},
        ],
    })


def _binding(descriptor):
    pool_ref = {"authority_id": descriptor.authority_id, "account_id": descriptor.account_id,
                "pool_id": descriptor.pool_id, "revision": descriptor.revision}
    return schema.parse_binding({
        "schema_version": 1, "binding_id": "claude-sonnet", "revision": "1",
        "subject": {"kind": "identity", "executor": "claude", "model_id": _MODEL},
        "constraints": [{"state": "known", "value": {"pool_ref": pool_ref, "window_id": window}}
                        for window in ("five_hour", "seven_day")],
        "coverage": {"state": "complete", "gaps": []},
    }, descriptors=(descriptor,))


def _event(five=0.17, week=0.88):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "rateLimitType": "seven_day",
        "unifiedWindows": {
            "five_hour": {"utilization": five, "resetsAt": (_NOW + 18_000_000) // 1000},
            "seven_day": {"utilization": week, "resetsAt": (_NOW + 604_800_000) // 1000},
        },
    }}


def _targets(descriptor):
    binding = _binding(descriptor)
    return tuple(ProviderQuotaTarget(
        resource_key=f"claude:{window}", binding=binding, descriptor=descriptor, window_id=window,
    ) for window in ("five_hour", "seven_day"))


def test_claude_contract_and_rate_limit_event_produce_percent_observations():
    from paulsha_cortex.coordinator import quota_sources

    descriptor = _descriptor()
    contract = quota_sources.provider_read_contract("claude")
    assert contract["state"] == "supported"
    assert contract["source_schema"] == "anthropic-claude-code-stream-json-rate-limit-event-v1"
    assert contract["unit_semantics_ref"] == _SEMANTICS
    capture = capture_provider_quota(
        "claude", _event(), profile_key=_PROFILE, model_id=_MODEL, targets=_targets(descriptor),
        descriptors=(descriptor,), unit_catalog=(), observed_at_ms=_NOW,
    )
    assert not capture.gaps
    observations = [row.to_dict() for row in capture.observations]
    assert {row["scope"]["value"]["window_id"]: row["measurement"]["quantity"]["amount"]["value"]
            for row in observations} == {"five_hour": "83", "seven_day": "12"}
    assert {row["reset_at_ms"]["value"] for row in observations} == {
        _NOW + 18_000_000, _NOW + 604_800_000,
    }


@pytest.mark.parametrize(("mutate", "unknown_keys"), [
    (lambda event: event["rate_limit_info"].pop("unifiedWindows"), {"claude:five_hour", "claude:seven_day"}),
    (lambda event: event["rate_limit_info"]["unifiedWindows"]["five_hour"].update(utilization="0.2"), {"claude:five_hour"}),
    (lambda event: event["rate_limit_info"]["unifiedWindows"]["five_hour"].update(resetsAt="later"), {"claude:five_hour"}),
    (lambda event: event["rate_limit_info"]["unifiedWindows"].update(five_hour=None), {"claude:five_hour"}),
])
def test_claude_malformed_or_missing_structured_fields_are_unknown(mutate, unknown_keys):
    descriptor = _descriptor()
    event = _event()
    mutate(event)
    capture = capture_provider_quota(
        "claude", event, profile_key=_PROFILE, model_id=_MODEL, targets=_targets(descriptor),
        descriptors=(descriptor,), unit_catalog=(), observed_at_ms=_NOW,
    )
    assert len(capture.observations) == 2
    rows = [row.to_dict() for row in capture.observations]
    unknown = {f"claude:{row['scope']['value']['window_id']}" for row in rows
               if row["measurement"]["quantity"]["state"] == "unknown"}
    assert unknown == unknown_keys
    assert {gap.resource_key for gap in capture.gaps} == unknown_keys


def test_claude_terminal_job_event_is_harvested_and_unknown_expires_to_unknown(tmp_path: Path, capsys):
    descriptor = _descriptor()
    binding = _binding(descriptor)
    ledger_path = tmp_path / "quota-observations" / "events.jsonl"
    shadow = QuotaShadowService(QuotaEventLedger(ledger_path))
    decision_store_path = tmp_path / "quota-admission-decisions" / "decisions.jsonl"
    decision_store = quota_admission.AdmissionDecisionStore(decision_store_path)
    context = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "quota-reservations" / "reservations.jsonl"),
        store=decision_store, shadow=shadow, bindings=(binding,), descriptors=(descriptor,),
        unit_catalog=(), lease_ms=300_000,
    )
    job_log = tmp_path / "job.jsonl"
    job_log.write_text(json.dumps(_event()) + "\n", encoding="utf-8")
    job = {"job_id": "job-claude", "workflow_run_id": "run-1", "workflow_card": "build",
           "status": "exited", "executor": "claude", "model_id": _MODEL,
           "log_path": str(job_log), "exited_at": "2027-01-15T08:00:00+00:00"}
    decision = {"decision_id": "decision-1", "run_id": "run-1", "card_id": "build",
                "attempt_id": "run-1:build:n0", "profile_key": _PROFILE, "outcome": "admit",
                "selected": {"executor": "claude", "model_id": _MODEL}}
    decision_model = quota_admission.AdmissionDecision(
        decision_id="decision-1", run_id="run-1", card_id="build",
        attempt_id="run-1:build:n0", profile_key=_PROFILE, mode="shadow", outcome="admit",
        policy_version=quota_admission.ADMISSION_POLICY_VERSION,
        observation_version="shadow-projection:fixture", demand_version=quota_admission.DEMAND_FIXTURE_VERSION,
        qualification_version="not-enforced", generated_at_ms=_NOW,
        selected={"executor": "claude", "model_id": _MODEL},
    )
    decision = decision_model.to_row()
    decision_store.record(decision_model)
    registry = SimpleNamespace(list_jobs=lambda: [job])

    before, _ = quota_admission.assess_candidate_quota(
        executor="claude", model_id=_MODEL, independence_domain="anthropic", profile_key=_PROFILE,
        bindings=(binding,), descriptors=(descriptor,), unit_catalog=(), shadow=shadow, now_ms=_NOW,
    )
    assert before.observation_state == "unknown"

    result = manager.reconcile_quota_admission_reservations(
        registry=registry, quota_admission_context=context, now_ms=_NOW,
    )
    assert result["terminal_usage"]["rate_limit_observations"]["recorded"] == 2
    assert quota_admission.unbound_profile_keys_report([decision], bindings=(binding,)) == ()
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text(json.dumps({
        "schema": "cortex/quota-pools/v1", "config_revision": "rev-1222",
        "descriptors": [descriptor.to_dict()], "unit_catalog": [],
        "bindings": [binding.to_dict()],
    }), encoding="utf-8")
    store_path = decision_store_path
    assert quota_cli.main([
        "bindings", "--report", "--config", str(config_path), "--store", str(store_path), "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["unbound"] == []
    second = manager.reconcile_quota_admission_reservations(
        registry=registry, quota_admission_context=context, now_ms=_NOW + 200_000,
    )
    assert second["terminal_usage"]["rate_limit_observations"]["recorded"] == 0
    assert len(ledger_path.read_text(encoding="utf-8").splitlines()) == 2
    after, _ = quota_admission.assess_candidate_quota(
        executor="claude", model_id=_MODEL, independence_domain="anthropic", profile_key=_PROFILE,
        bindings=(binding,), descriptors=(descriptor,), unit_catalog=(), shadow=shadow, now_ms=_NOW + 1,
    )
    assert after.observation_state == "known"

    expired, _ = quota_admission.assess_candidate_quota(
        executor="claude", model_id=_MODEL, independence_domain="anthropic", profile_key=_PROFILE,
        bindings=(binding,), descriptors=(descriptor,), unit_catalog=(), shadow=shadow,
        now_ms=_NOW + 300_001,
    )
    assert expired.observation_state == "unknown"


def test_claude_harvest_skips_jobs_whose_observation_already_expired(tmp_path: Path) -> None:
    """periodic reconcile 每拍走過全部 job：終局＋TTL 已過的 claude job 不讀 log、不寫 observation。"""

    descriptor = _descriptor()
    binding = _binding(descriptor)
    ledger_path = tmp_path / "quota-observations" / "events.jsonl"
    context = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "quota-reservations" / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(
            tmp_path / "quota-admission-decisions" / "decisions.jsonl"
        ),
        shadow=QuotaShadowService(QuotaEventLedger(ledger_path)),
        bindings=(binding,), descriptors=(descriptor,), unit_catalog=(), lease_ms=300_000,
    )
    job_log = tmp_path / "job.jsonl"
    job_log.write_text(json.dumps(_event()) + "\n", encoding="utf-8")
    job = {"job_id": "job-claude", "workflow_run_id": "run-1", "workflow_card": "build",
           "status": "exited", "executor": "claude", "model_id": _MODEL,
           "log_path": str(job_log), "exited_at": "2027-01-15T08:00:00+00:00"}

    result = manager._harvest_claude_rate_limit_event(
        job=job, profile_key=_PROFILE, quota_admission_context=context, now_ms=_NOW + 300_000,
    )

    assert result == {"recorded": 0, "gaps": 0, "reason": "observation-expired"}
    assert not ledger_path.exists() or ledger_path.read_text(encoding="utf-8") == ""
