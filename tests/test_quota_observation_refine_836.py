"""Issue #836 B/C/D：來源 adapter、耐久 ledger 與 shadow 投影契約。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import import_module
import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import quota_observation as schema


_PROFILE_A = "epk:v1:resolved:" + "a" * 64
_PROFILE_B = "epk:v1:resolved:" + "b" * 64
_NOW = 1_800_000_000_000


def _feature_api():
    try:
        sources = import_module("paulsha_cortex.coordinator.quota_sources")
        ledger = import_module("paulsha_cortex.coordinator.quota_ledger")
        shadow = import_module("paulsha_cortex.coordinator.quota_shadow")
    except ImportError as exc:
        pytest.fail(f"RED: #836 B/C/D public API is not implemented: {exc}")
    required = (
        (sources, "ProviderQuotaTarget"),
        (sources, "capture_provider_quota"),
        (sources, "provider_read_contract"),
        (ledger, "QuotaEventLedger"),
        (shadow, "QuotaShadowService"),
    )
    missing = [name for module, name in required if not hasattr(module, name)]
    assert not missing, f"RED: #836 B/C/D API missing: {missing}"
    return sources, ledger, shadow


def _profile_ref(key: str) -> dict[str, object]:
    return {"state": "known", "value": {"schema_version": 1, "key": key}}


def _pool_descriptor(
    *,
    account: str = "account-shared",
    pool: str = "pool-shared",
    unit_id: str = "token",
    semantics_ref: str = "fixture:native-token/v1",
    quantity_kind: str = "amount",
    windows: tuple[tuple[str, int], ...] = (("short", 300_000), ("week", 604_800_000)),
):
    unit = {
        "unit_id": unit_id,
        "version": "1",
        "quantity_kind": quantity_kind,
        "semantics_ref": semantics_ref,
    }
    return schema.parse_pool_descriptor(
        {
            "schema_version": 1,
            "authority_id": "operator-budget-authority",
            "account_id": account,
            "pool_id": pool,
            "revision": "1",
            "authority_ref": "fixture:operator-pool-map/v1",
            "provenance_refs": ["fixture:pool-map/v1"],
            "units": [unit],
            "windows": [
                {
                    "window_id": window_id,
                    "kind": "instantaneous" if quantity_kind == "gauge" else "rolling",
                    "unit_ref": {"unit_id": unit_id, "version": "1"},
                    **({} if quantity_kind == "gauge" else {"duration_ms": duration_ms}),
                }
                for window_id, duration_ms in windows
            ],
        }
    )


def _binding(descriptors, key: str, constraints=None, *, binding_id="binding"):
    if constraints is None:
        constraints = [
            {
                "state": "known",
                "value": {
                    "pool_ref": {
                        "authority_id": descriptor.authority_id,
                        "account_id": descriptor.account_id,
                        "pool_id": descriptor.pool_id,
                        "revision": descriptor.revision,
                    },
                    "window_id": window_id,
                },
            }
            for descriptor, window_id in constraints_from(descriptors)
        ]
    return schema.parse_binding(
        {
            "schema_version": 1,
            "binding_id": binding_id,
            "revision": "1",
            "subject": {"kind": "profile", "profile_ref": _profile_ref(key)},
            "constraints": constraints,
            "coverage": {"state": "complete", "gaps": []},
        },
        descriptors=tuple(descriptors),
    )


def constraints_from(descriptors):
    for descriptor in descriptors:
        for window in descriptor.to_dict()["windows"]:
            yield descriptor, window["window_id"]


def _observation(
    descriptor,
    window_id: str,
    *,
    value: str | None,
    observed_at_ms: int,
    reset_at_ms: int | None = None,
    profile_key: str = _PROFILE_A,
    unit_id: str = "token",
    metric_id: str = "remaining",
    measurement_kind: str = "remaining_snapshot",
    source_method: str = "provider_status",
    event_id: str | None = None,
    ttl_ms: int = 60_000,
):
    descriptor_wire = descriptor.to_dict()
    window = next(item for item in descriptor_wire["windows"] if item["window_id"] == window_id)
    duration_ms = window.get("duration_ms")
    if reset_at_ms is not None and duration_ms is not None:
        window_instance = {
            "kind": "interval",
            "start_ms": reset_at_ms - duration_ms,
            "end_ms": reset_at_ms,
            "epoch": {"state": "known", "value": f"reset-{reset_at_ms}"},
        }
    else:
        window_instance = {"kind": "unknown", "reason": "missing-window-instance"}
    quantity = (
        {"state": "unknown", "reason": "missing-remaining"}
        if value is None
        else {"state": "observed", "amount": {"kind": "exact", "value": value}}
    )
    source_event = (
        {"state": "unknown", "reason": "provider-has-no-event-id"}
        if event_id is None
        else {
            "state": "known",
            "namespace": "fixture-provider",
            "epoch": "1",
            "event_id": event_id,
        }
    )
    payload = {
        "schema_version": 1,
        "observation_id": f"fixture-{metric_id}-{observed_at_ms}",
        "scope": {
            "state": "known",
            "value": {
                "pool_ref": {
                    "authority_id": descriptor.authority_id,
                    "account_id": descriptor.account_id,
                    "pool_id": descriptor.pool_id,
                    "revision": descriptor.revision,
                },
                "window_id": window_id,
            },
        },
        "profile_ref": _profile_ref(profile_key),
        "unit_ref": {"state": "known", "value": {"unit_id": unit_id, "version": "1"}},
        "window_instance": window_instance,
        "measurement": {
            "kind": measurement_kind,
            "metric_id": metric_id,
            "quantity": quantity,
        },
        "observed_at_ms": {"state": "known", "value": observed_at_ms},
        "received_at_ms": observed_at_ms,
        "ttl_ms": {"state": "known", "value": ttl_ms},
        "reset_at_ms": (
            {"state": "unknown", "reason": "missing-reset"}
            if reset_at_ms is None
            else {"state": "known", "value": reset_at_ms}
        ),
        "source": {
            "source_id": "fixture-provider",
            "source_schema": "fixture-quota-v1",
            "adapter_version": "fixture-adapter-v1",
            "authority_ref": "fixture:provider-contract/v1",
            "method": source_method,
            "provenance_refs": ["fixture:source-document/v1"],
            "event_identity": source_event,
        },
        "coverage": {"state": "complete", "gaps": []},
    }
    return schema.parse_observation(
        payload,
        descriptors=(descriptor,),
        unit_catalog=(),
    )


def _pool_row(report, pool_id: str, window_id: str):
    return next(
        row for row in report["pools"]
        if row["pool_ref"]["pool_id"] == pool_id and row["window_id"] == window_id
    )


def _iso_utc_ms(value: int) -> str:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (epoch + timedelta(milliseconds=value)).isoformat()


def test_ac2_codex_app_server_v2_camel_case_fields_are_observed():
    sources, _, _ = _feature_api()
    semantics = "provider:openai-codex-app-server/rate-limit-percent/v2"
    descriptor = _pool_descriptor(
        unit_id="codex-percent", semantics_ref=semantics, windows=(("short", 300_000),)
    )
    target = sources.ProviderQuotaTarget(
        resource_key="codex:shared:primary",
        binding=_binding((descriptor,), _PROFILE_A),
        descriptor=descriptor,
        window_id="short",
    )
    capture = sources.capture_provider_quota(
        "codex",
        {"result": {"rateLimits": {
            "limitId": "shared",
            "primary": {
                "usedPercent": 40,
                "windowDurationMins": 5,
                "resetsAt": 1_800_001_000,
            },
        }}},
        profile_key=_PROFILE_A,
        targets=(target,),
        descriptors=(descriptor,),
        unit_catalog=(),
        observed_at_ms=_NOW,
    )
    assert not capture.gaps
    wire = capture.observations[0].to_dict()
    assert wire["measurement"]["quantity"]["amount"] == {
        "kind": "exact", "value": "60"
    }
    assert wire["reset_at_ms"] == {"state": "known", "value": 1_800_001_000_000}

    reset_unavailable = sources.capture_provider_quota(
        "codex",
        {"result": {"rateLimits": {
            "limitId": "shared",
            "primary": {
                "usedPercent": 40,
                "windowDurationMins": None,
                "resetsAt": None,
            },
        }}},
        profile_key=_PROFILE_A,
        targets=(target,),
        descriptors=(descriptor,),
        unit_catalog=(),
        observed_at_ms=_NOW,
    )
    assert not reset_unavailable.gaps
    reset_wire = reset_unavailable.observations[0].to_dict()
    assert reset_wire["measurement"]["quantity"]["amount"] == {
        "kind": "exact", "value": "60"
    }
    assert reset_wire["reset_at_ms"] == {
        "state": "unknown", "reason": "provider-reset-unavailable"
    }

    unknown_shape = sources.capture_provider_quota(
        "codex",
        {"result": {"rateLimits": {
            "limitId": "shared",
            "primary": {"used_pct": 40, "window_mins": 5, "resets": 1_800_001_000},
        }}},
        profile_key=_PROFILE_A,
        targets=(target,),
        descriptors=(descriptor,),
        unit_catalog=(),
        observed_at_ms=_NOW,
    )
    assert unknown_shape.observations[0].to_dict()["measurement"]["quantity"]["state"] == "unknown"


def test_ac5_missing_provider_event_derived_identity_conflict_is_unknown(tmp_path):
    _, ledger_module, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    service = shadow_module.QuotaShadowService(
        ledger_module.QuotaEventLedger(tmp_path / "quota-events.jsonl")
    )
    first = _observation(
        descriptor, "short", value="18", observed_at_ms=_NOW, event_id=None,
    )
    conflicting = deepcopy(first.to_dict())
    conflicting["measurement"]["quantity"]["amount"]["value"] = "7"

    assert first.observation_id == schema.parse_observation(
        conflicting, descriptors=(descriptor,), unit_catalog=()
    ).observation_id
    assert service.record_observation(
        first.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).accepted == 1
    result = service.record_observation(
        conflicting, descriptors=(descriptor,), unit_catalog=()
    )
    assert result.status == "conflict"
    records = ledger_module.QuotaEventLedger(tmp_path / "quota-events.jsonl").read().events
    assert any(row.get("kind") == "conflict" for row in records)

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 1
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "unknown"
    assert "source-conflict" in row["coverage_gaps"]


def test_ac5_reset_expiration_makes_snapshot_unknown_before_ttl():
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    service = shadow_module.QuotaShadowService.in_memory()
    snapshot = _observation(
        descriptor,
        "short",
        value="0",
        observed_at_ms=_NOW,
        reset_at_ms=_NOW + 100,
        ttl_ms=60_000,
    )
    assert service.record_observation(
        snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).accepted == 1

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 101
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "unknown"
    assert "stale-snapshot" in row["coverage_gaps"]


def test_ac5_unknown_snapshot_epoch_does_not_combine_usage_after_window_switch():
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    service = shadow_module.QuotaShadowService.in_memory()
    snapshot = _observation(
        descriptor,
        "short",
        value="20",
        observed_at_ms=_NOW,
        ttl_ms=300_000,
    )
    assert service.record_observation(
        snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).accepted == 1

    # The terminal usage belongs to a new five-minute window one millisecond
    # after the snapshot. The snapshot has no window instance or reset anchor.
    terminal_usage = _observation(
        descriptor,
        "short",
        value="2",
        observed_at_ms=_NOW + 1,
        reset_at_ms=_NOW + 300_001,
        ttl_ms=300_000,
        measurement_kind="usage_delta",
        metric_id="input_tokens",
        source_method="executor_usage",
        event_id="terminal-usage-after-window-switch",
    )
    assert service.record_observation(
        terminal_usage.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).accepted == 1

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 2
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "unknown"
    assert "window-epoch-unknown" in row["coverage_gaps"]


def test_ac5_known_snapshot_epoch_still_projects_matching_usage():
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    service = shadow_module.QuotaShadowService.in_memory()
    snapshot = _observation(
        descriptor,
        "short",
        value="20",
        observed_at_ms=_NOW,
        reset_at_ms=_NOW + 300_000,
        ttl_ms=300_000,
    )
    usage = _observation(
        descriptor,
        "short",
        value="2",
        observed_at_ms=_NOW + 1,
        reset_at_ms=_NOW + 300_000,
        ttl_ms=300_000,
        measurement_kind="usage_delta",
        metric_id="input_tokens",
        source_method="executor_usage",
        event_id="terminal-usage-same-window",
    )
    for observation in (snapshot, usage):
        assert service.record_observation(
            observation.to_dict(), descriptors=(descriptor,), unit_catalog=()
        ).accepted == 1

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 2
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "observed"
    assert row["remaining"]["amount"] == {"kind": "exact", "value": "18"}


def test_ac5_terminal_job_straddling_snapshot_is_not_deducted_as_a_whole():
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    service = shadow_module.QuotaShadowService.in_memory()
    snapshot = _observation(
        descriptor, "short", value="20", observed_at_ms=_NOW,
        reset_at_ms=_NOW + 300_000,
    )
    assert service.record_observation(
        snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).accepted == 1
    assert service.record_terminal_usage(
        {
            "id": "job-straddles-snapshot",
            "executor": "codex",
            "started_at": _iso_utc_ms(_NOW - 1),
            "usage": {"input_tokens": 3},
        },
        profile_key=_PROFILE_A,
        binding=binding,
        descriptors=(descriptor,),
        unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 10,
    ).accepted == 1

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 20
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "unknown"
    assert "straddling-usage" in row["coverage_gaps"]


def test_ac1_multimodel_shared_pool_independent_pool_and_all_windows_are_separate():
    _, _, shadow_module = _feature_api()
    descriptor_shared = _pool_descriptor()
    descriptor_other = _pool_descriptor(account="account-other", pool="pool-independent")
    descriptor_concurrency = _pool_descriptor(
        account="account-concurrency", pool="pool-concurrency", unit_id="active-jobs",
        quantity_kind="gauge", windows=(("active", 1_000),),
    )
    service = shadow_module.QuotaShadowService.in_memory()
    observations = (
        _observation(descriptor_shared, "short", value="8", observed_at_ms=_NOW),
        _observation(descriptor_shared, "week", value="3", observed_at_ms=_NOW),
        _observation(descriptor_shared, "short", value="8", observed_at_ms=_NOW,
                     profile_key=_PROFILE_B),
        _observation(descriptor_other, "short", value="20", observed_at_ms=_NOW, profile_key=_PROFILE_B),
        _observation(descriptor_concurrency, "active", value="2", observed_at_ms=_NOW,
                     unit_id="active-jobs", measurement_kind="gauge_snapshot"),
    )
    for index, observation in enumerate(observations):
        service.record_observation(
            observation.to_dict(),
            descriptors=(descriptor_shared, descriptor_other, descriptor_concurrency),
            unit_catalog=(),
            idempotency_key=f"fake-provider-{index}",
        )
    report = service.project(
        descriptors=(descriptor_shared, descriptor_other, descriptor_concurrency),
        unit_catalog=(),
        now_utc_ms=_NOW + 1,
        demand_by_window={
            (("operator-budget-authority", "account-shared", "pool-shared", "1"), "short"): "5",
            (("operator-budget-authority", "account-shared", "pool-shared", "1"), "week"): "5",
        },
    )
    assert report["mode"] == "shadow"
    assert report["dispatch_effect"] == "none"
    assert _pool_row(report, "pool-shared", "short")["assessment"] == "sufficient"
    assert _pool_row(report, "pool-shared", "week")["assessment"] == "insufficient"
    assert sum(row["pool_ref"]["pool_id"] == "pool-shared"
               and row["window_id"] == "short" for row in report["pools"]) == 1
    assert _pool_row(report, "pool-independent", "short")["remaining"]["amount"] == {
        "kind": "exact",
        "value": "20",
    }
    concurrency = _pool_row(report, "pool-concurrency", "active")
    assert concurrency["gauge"]["state"] == "observed"
    assert concurrency["gauge"]["amount"] == {"kind": "exact", "value": "2"}
    assert concurrency["assessment"] == "unknown"


def test_ac2_provider_missing_stale_corrupt_and_invalid_values_remain_unknown(tmp_path):
    sources, _, shadow_module = _feature_api()
    percent_unit = "provider:openai-codex-app-server/rate-limit-percent/v2"
    descriptor = _pool_descriptor(
        unit_id="codex-percent",
        semantics_ref=percent_unit,
        windows=(("short", 300_000),),
    )
    profile_binding = _binding((descriptor,), _PROFILE_A)
    target = sources.ProviderQuotaTarget(
        resource_key="codex:shared:primary",
        binding=profile_binding,
        descriptor=descriptor,
        window_id="short",
    )
    missing = sources.capture_provider_quota(
        "codex",
        None,
        profile_key=_PROFILE_A,
        targets=(target,),
        descriptors=(descriptor,),
        unit_catalog=(),
        observed_at_ms=_NOW,
    )
    assert missing.observations
    assert missing.gaps
    assert missing.observations[0].to_dict()["measurement"]["quantity"]["state"] == "unknown"

    codex = sources.capture_provider_quota(
        "codex",
        {
            "limit_id": "shared",
            "primary": {"used_percent": 40, "window_duration_mins": 5,
                        "resets_at": 1_800_001_000},
        },
        profile_key=_PROFILE_A, targets=(target,), descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW,
    )
    assert not codex.gaps
    codex_observation = codex.observations[0].to_dict()
    assert codex_observation["measurement"]["quantity"]["amount"] == {
        "kind": "exact", "value": "60"
    }
    assert codex_observation["unit_ref"]["value"]["unit_id"] == "codex-percent"

    invalid = sources.capture_provider_quota(
        "codex",
        {
            "limit_id": "shared",
            "primary": {"used_percent": 140, "window_duration_mins": 5, "resets_at": 1_800_001_000},
        },
        profile_key=_PROFILE_A,
        targets=(target,),
        descriptors=(descriptor,),
        unit_catalog=(),
        observed_at_ms=_NOW,
    )
    assert invalid.observations[0].to_dict()["measurement"]["quantity"]["state"] == "unknown"
    assert "invalid-provider-value" in {gap.reason for gap in invalid.gaps}
    nonfinite = sources.capture_provider_quota(
        "codex",
        {
            "limit_id": "shared",
            "primary": {"used_percent": float("nan"), "window_duration_mins": 5,
                        "resets_at": 1_800_001_000},
        },
        profile_key=_PROFILE_A, targets=(target,), descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW,
    )
    assert nonfinite.observations[0].to_dict()["measurement"]["quantity"]["state"] == "unknown"
    negative = sources.capture_provider_quota(
        "codex",
        {
            "limit_id": "shared",
            "primary": {"used_percent": -1, "window_duration_mins": 5,
                        "resets_at": 1_800_001_000},
        },
        profile_key=_PROFILE_A, targets=(target,), descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW,
    )
    assert negative.observations[0].to_dict()["measurement"]["quantity"]["state"] == "unknown"
    infinity = sources.capture_provider_quota(
        "codex",
        {
            "limit_id": "shared",
            "primary": {"used_percent": float("inf"), "window_duration_mins": 5,
                        "resets_at": 1_800_001_000},
        },
        profile_key=_PROFILE_A, targets=(target,), descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW,
    )
    assert infinity.observations[0].to_dict()["measurement"]["quantity"]["state"] == "unknown"
    unsupported = sources.capture_provider_quota(
        "claude", {"quota": "not-a-supported-interface"}, profile_key=_PROFILE_A,
        targets=(target,), descriptors=(descriptor,), unit_catalog=(), observed_at_ms=_NOW,
    )
    assert unsupported.observations[0].to_dict()["measurement"]["quantity"]["state"] == "unknown"
    assert "no-documented-machine-readable-quota-remaining-interface" in {
        gap.reason for gap in unsupported.gaps
    }

    bad_unit = _observation(
        descriptor, "short", value="8", observed_at_ms=_NOW, unit_id="codex-percent"
    ).to_dict()
    bad_unit["unit_ref"]["value"]["unit_id"] = "unregistered-unit"
    with pytest.raises(schema.QuotaContractError) as unknown_unit_error:
        schema.parse_observation(bad_unit, descriptors=(descriptor,), unit_catalog=())
    assert unknown_unit_error.value.code == "unresolved_reference"
    reversed_bounds = _observation(
        descriptor, "short", value="8", observed_at_ms=_NOW, unit_id="codex-percent"
    ).to_dict()
    reversed_bounds["measurement"]["quantity"]["amount"] = {
        "kind": "bounds", "lower": "9", "upper": "2"
    }
    with pytest.raises(schema.QuotaContractError) as bounds_error:
        schema.parse_observation(reversed_bounds, descriptors=(descriptor,), unit_catalog=())
    assert bounds_error.value.code == "invalid_bounds"

    expired = _observation(
        descriptor, "short", value="80", observed_at_ms=_NOW - 120_000,
        unit_id="codex-percent",
    )
    service = shadow_module.QuotaShadowService.in_memory()
    service.record_observation(expired.to_dict(), descriptors=(descriptor,), unit_catalog=())
    stale_report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW
    )
    assert _pool_row(stale_report, "pool-shared", "short")["remaining"]["state"] == "unknown"
    assert "stale-snapshot" in _pool_row(stale_report, "pool-shared", "short")["coverage_gaps"]

    corrupt_file = tmp_path / "quota-events.jsonl"
    corrupt_file.write_text("{broken\n", encoding="utf-8")
    corrupt = shadow_module.QuotaShadowService(
        __import__("paulsha_cortex.coordinator.quota_ledger", fromlist=["QuotaEventLedger"])
        .QuotaEventLedger(corrupt_file)
    ).project(descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW)
    assert corrupt["state"] == "unknown"
    assert "ledger-corrupt" in corrupt["coverage_gaps"]


def test_ac2_copilot_and_antigravity_official_quota_payloads_keep_native_units():
    sources, _, _ = _feature_api()
    copilot_unit = "provider:github-copilot-sdk/requests/v1"
    copilot_descriptor = _pool_descriptor(
        account="copilot-account", pool="premium-requests", unit_id="requests",
        semantics_ref=copilot_unit, windows=(("month", 2_678_400_000),),
    )
    copilot_target = sources.ProviderQuotaTarget(
        resource_key="copilot:premium_interactions",
        binding=_binding((copilot_descriptor,), _PROFILE_A),
        descriptor=copilot_descriptor,
        window_id="month",
    )
    copilot = sources.capture_provider_quota(
        "copilot",
        {"quotaSnapshots": {"premium_interactions": {
            "entitlementRequests": 12, "usedRequests": 3,
            "remainingPercentage": 75, "resetDate": "2027-01-15T08:13:20Z",
        }}},
        profile_key=_PROFILE_A, targets=(copilot_target,),
        descriptors=(copilot_descriptor,), unit_catalog=(), observed_at_ms=_NOW,
    )
    assert not copilot.gaps
    assert copilot.observations[0].to_dict()["measurement"]["quantity"]["amount"] == {
        "kind": "exact", "value": "9"
    }

    agy_unit = "provider:google-antigravity-cli/quota-fraction/v1"
    agy_descriptor = _pool_descriptor(
        account="agy-account", pool="gemini-window", unit_id="fraction",
        semantics_ref=agy_unit, windows=(("rolling", 300_000),),
    )
    agy_target = sources.ProviderQuotaTarget(
        resource_key="agy:gemini-pro", binding=_binding((agy_descriptor,), _PROFILE_A),
        descriptor=agy_descriptor, window_id="rolling",
    )
    agy = sources.capture_provider_quota(
        "agy", {"quota": {"gemini-pro": {
            "remaining_fraction": 0.25, "reset_time": "2027-01-15T08:13:20Z",
        }}},
        profile_key=_PROFILE_A, targets=(agy_target,), descriptors=(agy_descriptor,),
        unit_catalog=(), observed_at_ms=_NOW,
    )
    assert not agy.gaps
    assert agy.observations[0].to_dict()["measurement"]["quantity"]["amount"] == {
        "kind": "exact", "value": "0.25"
    }


def test_ac3_usage_never_converts_tokens_to_subscription_credits_and_provenance_stays_typed():
    _, _, shadow_module = _feature_api()
    credit_descriptor = _pool_descriptor(
        unit_id="premium-credit",
        semantics_ref="provider:github-copilot-sdk/premium-interactions/v1",
        windows=(("month", 2_678_400_000),),
    )
    token_unit = schema.parse_unit_definition(
        {
            "schema_version": 1,
            "unit_id": "executor-token",
            "version": "1",
            "quantity_kind": "amount",
            "semantics_ref": "cortex:executor-usage/token/v1",
        }
    )
    binding = _binding((credit_descriptor,), _PROFILE_A)
    service = shadow_module.QuotaShadowService.in_memory()
    result = service.record_terminal_usage(
        {
            "id": "job-builder-1",
            "executor": "codex",
            "usage": {"input_tokens": 120, "output_tokens": 30},
        },
        profile_key=_PROFILE_A,
        binding=binding,
        descriptors=(credit_descriptor,),
        unit_catalog=(token_unit,),
        unit_ref_by_metric={
            "input_tokens": ("executor-token", "1"),
            "output_tokens": ("executor-token", "1"),
        },
        observed_at_ms=_NOW,
    )
    assert result.gaps
    report = service.project(
        descriptors=(credit_descriptor,), unit_catalog=(token_unit,), now_utc_ms=_NOW + 1
    )
    row = _pool_row(report, "pool-shared", "month")
    assert row["remaining"]["state"] == "unknown"
    assert "usage-unit-not-comparable" in row["coverage_gaps"]
    usage_events = [event for event in report["events"] if event["measurement_kind"] == "usage_delta"]
    assert {event["unit_ref"]["unit_id"] for event in usage_events} == {"executor-token"}
    assert all(event["source_method"] == "executor_usage" for event in usage_events)
    assert all(event["source_schema"] == "cortex-terminal-usage-v1" for event in usage_events)

    provenance_service = shadow_module.QuotaShadowService.in_memory()
    observed = _observation(
        credit_descriptor, "month", value="10", observed_at_ms=_NOW - 4,
        unit_id="premium-credit",
    )
    provenance_service.record_observation(
        observed.to_dict(), descriptors=(credit_descriptor,), unit_catalog=()
    )
    estimated = _observation(
        credit_descriptor, "month", value="9", observed_at_ms=_NOW - 3,
        unit_id="premium-credit",
    ).to_dict()
    estimated["measurement"]["quantity"] = {
        "state": "estimated", "amount": {"kind": "exact", "value": "9"},
        "method_ref": "fixture:budget-estimate/v1",
    }
    estimated["source"]["method"] = "estimate"
    estimated["source"]["source_schema"] = "fixture-estimate-v1"
    estimated["source"]["adapter_version"] = "fixture-estimate-adapter-v1"
    provenance_service.record_observation(
        estimated, descriptors=(credit_descriptor,), unit_catalog=()
    )
    unknown = _observation(
        credit_descriptor, "month", value=None, observed_at_ms=_NOW - 2,
        unit_id="premium-credit",
    )
    provenance_service.record_observation(
        unknown.to_dict(), descriptors=(credit_descriptor,), unit_catalog=()
    )
    provenance = provenance_service.project(
        descriptors=(credit_descriptor,), unit_catalog=(), now_utc_ms=_NOW + 1
    )
    quantity_states = {event["quantity"]["state"] for event in provenance["events"]}
    assert quantity_states == {"observed", "estimated", "unknown"}
    assert any(event["source_schema"] == "fixture-estimate-v1" for event in provenance["events"])


def test_ac4_manager_worker_reviewer_replays_deduplicate_and_external_gap_is_visible():
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000), ("week", 604_800_000)))
    unit = schema.parse_unit_definition(
        {
            "schema_version": 1,
            "unit_id": "token",
            "version": "1",
            "quantity_kind": "amount",
            "semantics_ref": "fixture:native-token/v1",
        }
    )
    binding = _binding((descriptor,), _PROFILE_A)
    service = shadow_module.QuotaShadowService.in_memory()
    service.record_observation(
        _observation(descriptor, "short", value="20", observed_at_ms=_NOW).to_dict(),
        descriptors=(descriptor,),
        unit_catalog=(unit,),
        idempotency_key="provider-snapshot-1",
    )
    service.record_observation(
        _observation(descriptor, "week", value="50", observed_at_ms=_NOW).to_dict(),
        descriptors=(descriptor,), unit_catalog=(unit,), idempotency_key="provider-snapshot-week-1",
    )
    jobs = [
        {"id": "job-controller", "executor": "codex", "usage": {"input_tokens": 2}},
        {"id": "job-worker", "executor": "codex", "usage": {"input_tokens": 3}},
        {"id": "job-reviewer", "executor": "codex", "usage": {"input_tokens": 1}},
    ]
    for job in jobs:
        first = service.record_terminal_usage(
            job,
            profile_key=_PROFILE_A,
            binding=binding,
            descriptors=(descriptor,),
            unit_catalog=(unit,),
            unit_ref_by_metric={"input_tokens": ("token", "1")},
            observed_at_ms=_NOW + 10,
        )
        replay = service.record_terminal_usage(
            deepcopy(job),
            profile_key=_PROFILE_A,
            binding=binding,
            descriptors=(descriptor,),
            unit_catalog=(unit,),
            unit_ref_by_metric={"input_tokens": ("token", "1")},
            observed_at_ms=_NOW + 10,
        )
        assert first.accepted == 2
        assert replay.duplicates == 2
    report = service.project(
        descriptors=(descriptor,), unit_catalog=(unit,), now_utc_ms=_NOW + 20
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "unknown"
    assert "window-epoch-unknown" in row["coverage_gaps"]
    assert "external-session-unobserved" in row["coverage_gaps"]
    week = _pool_row(report, "pool-shared", "week")
    assert week["remaining"]["state"] == "unknown"
    assert "window-epoch-unknown" in week["coverage_gaps"]
    assert "external-session-unobserved" in week["coverage_gaps"]
    assert report["dispatch_effect"] == "none"

    bounded_service = shadow_module.QuotaShadowService.in_memory()
    bounded_payload = _observation(
        descriptor, "short", value="15", observed_at_ms=_NOW
    ).to_dict()
    bounded_payload["measurement"]["quantity"]["amount"] = {
        "kind": "bounds", "lower": "10", "upper": "20"
    }
    bounded_observation = schema.parse_observation(
        bounded_payload, descriptors=(descriptor,), unit_catalog=(unit,)
    )
    bounded_service.record_observation(
        bounded_observation, descriptors=(descriptor,), unit_catalog=(unit,),
        idempotency_key="bounded-provider-snapshot",
    )
    bounded_service.record_terminal_usage(
        {"id": "job-bounded", "executor": "codex", "usage": {"input_tokens": 3}},
        profile_key=_PROFILE_A, binding=binding, descriptors=(descriptor,),
        unit_catalog=(unit,), unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 10,
    )
    bounded_report = bounded_service.project(
        descriptors=(descriptor,), unit_catalog=(unit,), now_utc_ms=_NOW + 20
    )
    bounded_row = _pool_row(bounded_report, "pool-shared", "short")
    assert bounded_row["remaining"]["state"] == "unknown"
    assert "window-epoch-unknown" in bounded_row["coverage_gaps"]

    external_service = shadow_module.QuotaShadowService.in_memory()
    external_payload = _observation(
        descriptor, "short", value="16", observed_at_ms=_NOW
    ).to_dict()
    external_payload["source"].update({
        "source_id": "external-read-host",
        "source_schema": "fixture-external-read-v1",
        "adapter_version": "external-reader-v1",
        "authority_ref": "fixture:external-read-authority/v1",
        "method": "structured_event",
        "provenance_refs": ["fixture:external-host-read/v1"],
    })
    external = external_service.record_external_observation(
        external_payload, descriptors=(descriptor,), unit_catalog=(unit,),
        idempotency_key="external-read-1",
    )
    assert external.accepted == 1
    external_report = external_service.project(
        descriptors=(descriptor,), unit_catalog=(unit,), now_utc_ms=_NOW + 1
    )
    assert any(event["source_schema"] == "fixture-external-read-v1"
               for event in external_report["events"])
    assert "external-session-unobserved" in _pool_row(
        external_report, "pool-shared", "short"
    )["coverage_gaps"]


def test_ac5_restart_replay_reset_clock_rollback_and_conflicts_do_not_wash_out_usage(tmp_path):
    _, ledger_module, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    state_path = tmp_path / "quota-events.jsonl"
    first = shadow_module.QuotaShadowService(ledger_module.QuotaEventLedger(state_path))
    snapshot = _observation(
        descriptor,
        "short",
        value="18",
        observed_at_ms=_NOW,
        reset_at_ms=_NOW + 300_000,
        event_id="snapshot-1",
    )
    assert first.record_observation(
        snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).accepted == 1
    assert first.record_observation(
        snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).duplicates == 1

    restarted = shadow_module.QuotaShadowService(ledger_module.QuotaEventLedger(state_path))
    clock_rollback = restarted.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW - 1
    )
    assert _pool_row(clock_rollback, "pool-shared", "short")["remaining"]["state"] == "unknown"
    assert "clock-rollback" in _pool_row(clock_rollback, "pool-shared", "short")["coverage_gaps"]
    older = _observation(
        descriptor,
        "short",
        value="99",
        observed_at_ms=_NOW - 10,
        reset_at_ms=_NOW + 300_000,
        event_id="snapshot-older",
    )
    restarted.record_observation(older.to_dict(), descriptors=(descriptor,), unit_catalog=())
    rollback = restarted.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 1
    )
    assert _pool_row(rollback, "pool-shared", "short")["remaining"]["amount"]["value"] == "18"

    conflict = deepcopy(snapshot.to_dict())
    conflict["measurement"]["quantity"]["amount"]["value"] = "7"
    conflict_record = schema.parse_observation(
        conflict, descriptors=(descriptor,), unit_catalog=()
    )
    assert restarted.record_observation(
        conflict_record.to_dict(),
        descriptors=(descriptor,),
        unit_catalog=(),
    ).status == "conflict"
    after_conflict = restarted.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 2
    )
    assert _pool_row(after_conflict, "pool-shared", "short")["remaining"]["state"] == "unknown"
    assert "source-conflict" in _pool_row(after_conflict, "pool-shared", "short")["coverage_gaps"]

    reset_snapshot = _observation(
        descriptor,
        "short",
        value="25",
        observed_at_ms=_NOW + 300_001,
        reset_at_ms=_NOW + 600_000,
        event_id="snapshot-reset-2",
    )
    restarted.record_observation(reset_snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=())
    reset_report = restarted.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 300_002
    )
    assert _pool_row(reset_report, "pool-shared", "short")["remaining"]["amount"]["value"] == "25"


def test_ac6_provider_read_interfaces_and_shadow_fixture_do_not_touch_dispatch_or_credentials():
    sources, _, shadow_module = _feature_api()
    assert sources.provider_read_contract("agy")["argv"] == (
        "agy", "-p", "/usage", "--output-format", "json"
    )
    assert sources.provider_read_contract("codex")["method"] == "account/rateLimits/read"
    assert sources.provider_read_contract("copilot")["method"] == "account.getQuota"
    assert sources.provider_read_contract("claude")["state"] == "unsupported"
    assert sources.provider_read_contract("cg")["state"] == "unknown"

    positive = json.loads(
        Path("tests/fixtures/patchmud/usage-provenance-v1/positive.json").read_text(
            encoding="utf-8"
        )
    )
    assert positive["fields"]["billed_input_total"]["state"] == "observed"
    assert positive["fields"]["reasoning"]["state"] == "unknown"
    service = shadow_module.QuotaShadowService.in_memory()
    assert service.project(descriptors=(), unit_catalog=(), now_utc_ms=_NOW)["dispatch_effect"] == "none"


# #836 對抗審查第四輪：以下四個測試分別對應審查稿逐條列出的
# BLOCKER/MAJOR，皆走正常 producer 路徑（record_terminal_usage / project），
# 不用手工 record_observation 灌假資料覆蓋。


def test_review4_blocker1_terminal_usage_deducts_via_real_producer_path():
    """BLOCKER quota_shadow.py:596 — record_terminal_usage() 之前永遠把
    window_instance 標 unknown，即使已有含 reset 的 fresh snapshot、且 job
    完整落在該窗口內，投影仍只會得到 usage-window-unresolved 而不會扣減。"""
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    service = shadow_module.QuotaShadowService.in_memory()
    snapshot = _observation(
        descriptor, "short", value="20", observed_at_ms=_NOW,
        reset_at_ms=_NOW + 300_000,
    )
    assert service.record_observation(
        snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).accepted == 1

    result = service.record_terminal_usage(
        {
            "id": "job-fresh-within-window",
            "executor": "codex",
            "started_at": _iso_utc_ms(_NOW + 1),
            "usage": {"input_tokens": 2},
        },
        profile_key=_PROFILE_A,
        binding=binding,
        descriptors=(descriptor,),
        unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 5,
    )
    assert result.accepted == 1
    assert not result.gaps

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 10
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "observed"
    assert row["remaining"]["amount"] == {"kind": "exact", "value": "18"}


def test_review4_major2_same_window_id_across_two_pools_does_not_collide():
    """MAJOR quota_shadow.py:273 — replay key 之前只含
    job_id/profile_key/metric/window_id；同一 binding 把同 metric/unit 映到
    兩個 pool、且 window_id 恰好同名（shared-account 多角色都叫 month）時，
    第二筆會撞到第一筆而被誤判成 conflict、錯拆池。"""
    _, _, shadow_module = _feature_api()
    pool_a = _pool_descriptor(
        account="acct-role-a", pool="pool-role-a", windows=(("month", 2_678_400_000),),
    )
    pool_b = _pool_descriptor(
        account="acct-role-b", pool="pool-role-b", windows=(("month", 2_678_400_000),),
    )
    binding = _binding((pool_a, pool_b), _PROFILE_A)
    service = shadow_module.QuotaShadowService.in_memory()

    result = service.record_terminal_usage(
        {"id": "job-shared-account", "executor": "codex", "usage": {"input_tokens": 5}},
        profile_key=_PROFILE_A,
        binding=binding,
        descriptors=(pool_a, pool_b),
        unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW,
    )
    assert result.accepted == 2
    assert result.conflicts == 0

    report = service.project(
        descriptors=(pool_a, pool_b), unit_catalog=(), now_utc_ms=_NOW + 1
    )
    usage_events = [e for e in report["events"] if e["measurement_kind"] == "usage_delta"]
    assert len(usage_events) == 2
    assert {e["quantity"]["amount"]["value"] for e in usage_events} == {"5"}


def test_review4_major3_terminal_usage_replay_with_later_recording_time_is_duplicate(tmp_path):
    """MAJOR quota_shadow.py:590 — terminal usage payload 把 observed_at_ms
    烘進 identity/digest，重啟後同一個已結束的 job 若以較晚時間重播相同
    usage，會被誤判成 conflict 而不是 duplicate。真的改內容時仍必須是
    conflict。"""
    _, ledger_module, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    service = shadow_module.QuotaShadowService(
        ledger_module.QuotaEventLedger(tmp_path / "quota-events.jsonl")
    )
    job = {
        "id": "job-restart-replay",
        "executor": "codex",
        "started_at": _iso_utc_ms(_NOW + 1),
        "usage": {"input_tokens": 4},
    }
    first = service.record_terminal_usage(
        job, profile_key=_PROFILE_A, binding=binding, descriptors=(descriptor,),
        unit_catalog=(), unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 5,
    )
    assert first.accepted == 1

    # 模擬 manager 重啟後，以遠晚於原始記錄時間重播同一筆終局 usage。
    replay = service.record_terminal_usage(
        deepcopy(job), profile_key=_PROFILE_A, binding=binding, descriptors=(descriptor,),
        unit_catalog=(), unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 500_000,
    )
    assert replay.duplicates == 1
    assert replay.conflicts == 0

    changed_job = deepcopy(job)
    changed_job["usage"]["input_tokens"] = 9
    changed = service.record_terminal_usage(
        changed_job, profile_key=_PROFILE_A, binding=binding, descriptors=(descriptor,),
        unit_catalog=(), unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 600_000,
    )
    assert changed.conflicts == 1


def test_review4_major4_stale_pool_revision_does_not_poison_other_pools(tmp_path):
    """MAJOR quota_shadow.py:314 — project() 之前用當前傳入的
    descriptors/unit_catalog 重 parse 整份 append-only ledger；pool 升版後
    caller 只帶新 revision，ledger 內舊 revision 的事件會被當
    ledger-corrupt、毒化整份 shadow projection（連帶未升版的其他 pool也遭殃）。"""
    _, ledger_module, shadow_module = _feature_api()
    ledger_path = tmp_path / "quota-events.jsonl"
    service = shadow_module.QuotaShadowService(ledger_module.QuotaEventLedger(ledger_path))

    rotated_v1 = _pool_descriptor(pool="pool-rotates")
    rotated_snapshot = _observation(rotated_v1, "short", value="20", observed_at_ms=_NOW)
    assert service.record_observation(
        rotated_snapshot.to_dict(), descriptors=(rotated_v1,), unit_catalog=(),
    ).accepted == 1

    untouched = _pool_descriptor(account="account-untouched", pool="pool-untouched")
    untouched_snapshot = _observation(untouched, "short", value="30", observed_at_ms=_NOW)
    assert service.record_observation(
        untouched_snapshot.to_dict(), descriptors=(untouched,), unit_catalog=(),
    ).accepted == 1

    # pool-rotates 升版到 revision 2：caller 之後只帶新 revision 的描述集，
    # ledger 內 revision 1 的事件對不上任何目前的 descriptor。
    rotated_v2 = schema.parse_pool_descriptor({**rotated_v1.to_dict(), "revision": "2"})

    report = service.project(
        descriptors=(rotated_v2, untouched), unit_catalog=(), now_utc_ms=_NOW + 1
    )
    assert "ledger-corrupt" not in report["coverage_gaps"]

    rotated_row = _pool_row(report, "pool-rotates", "short")
    assert rotated_row["remaining"]["state"] == "unknown"
    assert "stale-pool-revision" in rotated_row["coverage_gaps"]

    untouched_row = _pool_row(report, "pool-untouched", "short")
    assert untouched_row["remaining"]["state"] == "observed"
    assert untouched_row["remaining"]["amount"] == {"kind": "exact", "value": "30"}


# #836 對抗審查第五輪：以下三個測試對應審查稿指出的兩條 MAJOR（同根因）——
# record_terminal_usage() 之前依記錄當下的 ledger 內容一次性推導並持久化
# window_instance，且只保存 terminal_job_started_at_ms、不保存 finished_at。


def test_review5_major1_usage_recorded_before_late_snapshot_converges_to_deduction():
    """MAJOR quota_shadow.py:273 — usage 先落 ledger、涵蓋同一 window 的
    較舊 snapshot 之後才補進來時，記錄當下推導的 window_instance 永遠是
    unknown（因為當時 ledger 內還沒有那張 snapshot），shadow 永不收斂。
    改為 project() 時依當下 ledger 動態推導後，晚到的 snapshot 補進來即可
    讓先記的 usage 正確扣減。"""
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    service = shadow_module.QuotaShadowService.in_memory()

    # usage 先記：此時 ledger 內完全沒有任何 remaining_snapshot。
    result = service.record_terminal_usage(
        {
            "id": "job-usage-before-snapshot",
            "executor": "codex",
            "started_at": _iso_utc_ms(_NOW + 1),
            "finished_at": _iso_utc_ms(_NOW + 2),
            "usage": {"input_tokens": 2},
        },
        profile_key=_PROFILE_A,
        binding=binding,
        descriptors=(descriptor,),
        unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 5,
    )
    assert result.accepted == 1

    early_report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 6
    )
    early_row = _pool_row(early_report, "pool-shared", "short")
    assert early_row["remaining"]["state"] == "unknown"
    assert "missing-snapshot" in early_row["coverage_gaps"]

    # 較舊、涵蓋同一 window 的 snapshot 之後才補進 ledger（append-only：
    # 補進順序不影響任何一筆事件本身的內容或 identity）。
    snapshot = _observation(
        descriptor, "short", value="20", observed_at_ms=_NOW,
        reset_at_ms=_NOW + 300_000,
    )
    assert service.record_observation(
        snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).accepted == 1

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 10
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "observed"
    assert row["remaining"]["amount"] == {"kind": "exact", "value": "18"}


def test_review5_major1_terminal_usage_replay_across_ledger_changes_stays_duplicate():
    """MAJOR quota_shadow.py:273 — 舊實作在記錄當下依 ledger 現況推導
    window_instance 並直接烘進 observation payload；同一個 job 重播時，若
    兩次呼叫之間 ledger 內容已經不同（例如中途補進了 snapshot），第二次推導
    出的 window_instance 就會與第一次不同，payload 隨之改變、被誤判成
    conflict。改為只保存原始事實、window 歸屬全部移到 project() 時動態推導
    後，重播不論 ledger 內容是否變動都必須是 duplicate，且只扣減一次。"""
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    service = shadow_module.QuotaShadowService.in_memory()
    job = {
        "id": "job-replay-across-ledger-change",
        "executor": "codex",
        "started_at": _iso_utc_ms(_NOW + 1),
        "finished_at": _iso_utc_ms(_NOW + 2),
        "usage": {"input_tokens": 4},
    }
    first = service.record_terminal_usage(
        deepcopy(job), profile_key=_PROFILE_A, binding=binding,
        descriptors=(descriptor,), unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 5,
    )
    assert first.accepted == 1

    # ledger 內容在兩次呼叫之間改變：補進一張涵蓋同一 window 的 snapshot。
    snapshot = _observation(
        descriptor, "short", value="20", observed_at_ms=_NOW,
        reset_at_ms=_NOW + 300_000,
    )
    assert service.record_observation(
        snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).accepted == 1

    replay = service.record_terminal_usage(
        deepcopy(job), profile_key=_PROFILE_A, binding=binding,
        descriptors=(descriptor,), unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 500_000,
    )
    assert replay.duplicates == 1
    assert replay.conflicts == 0

    # 重播嘗試的 observed_at_ms 遠晚於首次記錄，但因為是 duplicate、沒有寫入
    # 新事件，投影仍以首次記錄時的 ledger 內容為準，snapshot 也仍在 fresh
    # 範圍內；只需確認扣減只發生一次（16 = 20 - 4，不是 20 - 4 - 4）。
    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 20
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "observed"
    assert row["remaining"]["amount"] == {"kind": "exact", "value": "16"}


def test_review5_major2_pre_snapshot_completed_job_replay_is_ignored_not_poisoned():
    """MAJOR quota_shadow.py:567 — 舊實作只持久化
    terminal_job_started_at_ms，project() 看不到 finished_at；job 整段
    （started_at 與 finished_at）都早於 snapshot.observed_at、只是因為
    manager 重啟才在 snapshot 之後被重播寫入 ledger 時，只看
    started_at < observed_at 就會誤判為 straddling-usage，把本應忽略、
    早已反映於 snapshot 的 pre-snapshot usage 變成毒化餘額的 unknown。"""
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    service = shadow_module.QuotaShadowService.in_memory()

    snapshot = _observation(
        descriptor, "short", value="20", observed_at_ms=_NOW,
        reset_at_ms=_NOW + 300_000,
    )
    assert service.record_observation(
        snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).accepted == 1

    # job 在 snapshot 之前就已整段完整結束（started_at 與 finished_at 都早
    # 於 snapshot.observed_at），只是因為 manager 重啟才在 snapshot 之後被
    # 重播寫入 ledger（observed_at_ms 遠晚於實際結束時間）。
    result = service.record_terminal_usage(
        {
            "id": "job-finished-before-snapshot",
            "executor": "codex",
            "started_at": _iso_utc_ms(_NOW - 20),
            "finished_at": _iso_utc_ms(_NOW - 10),
            "usage": {"input_tokens": 9},
        },
        profile_key=_PROFILE_A,
        binding=binding,
        descriptors=(descriptor,),
        unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 100,
    )
    assert result.accepted == 1

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 200
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "observed"
    assert row["remaining"]["amount"] == {"kind": "exact", "value": "20"}
    assert "straddling-usage" not in row["coverage_gaps"]


# #836 對抗審查第六輪：以下三個測試分別對應審查稿逐條列出的
# BLOCKER/MAJOR。BLOCKER1 全程走真實 producer 路徑
# （record_provider_read → record_terminal_usage → project()），不用手工
# _observation() 假資料覆蓋 window_instance。


def test_review6_blocker1_provider_snapshot_window_epoch_enables_terminal_usage_deduction():
    """BLOCKER quota_sources.py:431 — record_provider_read() 產生的真實
    provider snapshot 之前一律 window_instance=unknown，即使收到含 reset 的
    fresh snapshot、且終局 usage 完整落在該窗口內，project() 仍只會報
    window-epoch-unknown 而不會扣減，AC 的 matching-usage 仍只能靠手工
    _observation() 假資料覆蓋才會通過。"""
    sources, _, shadow_module = _feature_api()
    percent_unit = "provider:openai-codex-app-server/rate-limit-percent/v2"
    descriptor = _pool_descriptor(
        unit_id="codex-percent", semantics_ref=percent_unit, windows=(("short", 300_000),)
    )
    binding = _binding((descriptor,), _PROFILE_A)
    target = sources.ProviderQuotaTarget(
        resource_key="codex:shared:primary", binding=binding,
        descriptor=descriptor, window_id="short",
    )
    service = shadow_module.QuotaShadowService.in_memory()
    reset_seconds = _NOW // 1000 + 300  # 5 分鐘後 reset，恰等於 window duration。
    capture = service.record_provider_read(
        "codex",
        {"result": {"rateLimits": {
            "limitId": "shared",
            "primary": {
                "usedPercent": 40,
                "windowDurationMins": 5,
                "resetsAt": reset_seconds,
            },
        }}},
        profile_key=_PROFILE_A,
        targets=(target,),
        descriptors=(descriptor,),
        unit_catalog=(),
        observed_at_ms=_NOW,
    )
    assert not capture.gaps
    snapshot_wire = capture.observations[0].to_dict()
    assert snapshot_wire["window_instance"]["kind"] == "interval"
    assert snapshot_wire["measurement"]["quantity"]["amount"] == {"kind": "exact", "value": "60"}

    result = service.record_terminal_usage(
        {
            "id": "job-real-producer-path",
            "executor": "codex",
            "started_at": _iso_utc_ms(_NOW + 10),
            "finished_at": _iso_utc_ms(_NOW + 20),
            "usage": {"input_tokens": 5},
        },
        profile_key=_PROFILE_A,
        binding=binding,
        descriptors=(descriptor,),
        unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("codex-percent", "1")},
        observed_at_ms=_NOW + 50,
    )
    assert result.accepted == 1
    assert not result.gaps

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 100
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "observed"
    assert row["remaining"]["amount"] == {"kind": "exact", "value": "55"}


def test_review6_major2_mixed_comparable_and_incomparable_pool_flags_gap_and_invalidates_snapshot():
    """MAJOR quota_shadow.py:251 — 同一 binding 同時含可比對 unit 的 pool
    （tokens）與不可換算 unit 的 pool（premium credit）時，
    record_terminal_usage() 之前只要有任一 constraint 命中 same_unit 就整批
    視為「已比對」，導致不可換算的那個 pool 完全拿不到 usage-unit-not-
    comparable 的證據，既有 snapshot 的餘額被當確定值。"""
    _, _, shadow_module = _feature_api()
    token_descriptor = _pool_descriptor(
        account="acct-tokens", pool="pool-tokens", unit_id="token",
        windows=(("month", 2_678_400_000),),
    )
    credit_descriptor = _pool_descriptor(
        account="acct-credit", pool="pool-credit", unit_id="premium-credit",
        semantics_ref="provider:github-copilot-sdk/premium-interactions/v1",
        windows=(("month", 2_678_400_000),),
    )
    binding = _binding((token_descriptor, credit_descriptor), _PROFILE_A)
    service = shadow_module.QuotaShadowService.in_memory()

    # 不可換算的 pool 先有一張確定的 remaining snapshot。
    credit_snapshot = _observation(
        credit_descriptor, "month", value="10", observed_at_ms=_NOW, unit_id="premium-credit",
    )
    assert service.record_observation(
        credit_snapshot.to_dict(), descriptors=(credit_descriptor,), unit_catalog=()
    ).accepted == 1

    result = service.record_terminal_usage(
        {"id": "job-mixed-pools", "executor": "codex", "usage": {"input_tokens": 3}},
        profile_key=_PROFILE_A,
        binding=binding,
        descriptors=(token_descriptor, credit_descriptor),
        unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 10,
    )
    # token pool 記到一筆可比對 usage_delta，credit pool 記到一筆「有耗用但
    # 不可換算」的證據——兩筆都是新事件。
    assert result.accepted == 2
    assert any(gap.reason == "usage-unit-not-comparable" for gap in result.gaps)

    report = service.project(
        descriptors=(token_descriptor, credit_descriptor), unit_catalog=(), now_utc_ms=_NOW + 20
    )
    credit_row = _pool_row(report, "pool-credit", "month")
    assert credit_row["remaining"]["state"] == "unknown"
    assert "usage-unit-not-comparable" in credit_row["coverage_gaps"]


def test_review6_major3_cross_source_snapshot_conflict_at_same_scope_and_time_is_detected():
    """MAJOR quota_ledger.py:296 — derived identity 之前把 source_id／
    source_schema 綁進 key，manager provider read 與匯入的外部 read（或同一
    來源 rollback 前後不同 adapter schema）在同一 pool／window／observed_at
    給出不同 remaining 時，因為 source 欄位不同永遠不會撞成同一個
    idempotency key；project() 因此任選一筆，看不到衝突。"""
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    service = shadow_module.QuotaShadowService.in_memory()

    manager_read = _observation(descriptor, "short", value="18", observed_at_ms=_NOW)
    assert service.record_observation(
        manager_read.to_dict(), descriptors=(descriptor,), unit_catalog=()
    ).accepted == 1

    # 同一 pool／window／observed_at，但由匯入的外部 read 給出不同 remaining，
    # 且來源身分（source_id/source_schema/method）與 manager provider read
    # 完全不同——語意上仍是同一時點的衝突觀測。
    external_read_payload = deepcopy(manager_read.to_dict())
    external_read_payload["observation_id"] = "fixture-external-conflicting-read"
    external_read_payload["measurement"]["quantity"]["amount"]["value"] = "9"
    external_read_payload["source"].update({
        "source_id": "external-read-host",
        "source_schema": "fixture-external-read-v1",
        "adapter_version": "external-reader-v1",
        "authority_ref": "fixture:external-read-authority/v1",
        "method": "structured_event",
        "provenance_refs": ["fixture:external-host-read/v1"],
    })
    result = service.record_observation(
        external_read_payload, descriptors=(descriptor,), unit_catalog=()
    )
    assert result.status == "conflict"

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 1
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "unknown"
    assert "source-conflict" in row["coverage_gaps"]


# #836 對抗審查第七輪：以下測試對應審查稿指出的三條 MAJOR。


def _group_binding(descriptors, keys, *, binding_id="group-binding"):
    constraints = [
        {
            "state": "known",
            "value": {
                "pool_ref": {
                    "authority_id": descriptor.authority_id,
                    "account_id": descriptor.account_id,
                    "pool_id": descriptor.pool_id,
                    "revision": descriptor.revision,
                },
                "window_id": window_id,
            },
        }
        for descriptor, window_id in constraints_from(descriptors)
    ]
    return schema.parse_binding(
        {
            "schema_version": 1,
            "binding_id": binding_id,
            "revision": "1",
            "subject": {
                "kind": "group",
                "group_ref": "fixture:group/v1",
                "revision": "1",
                "members": {
                    "state": "known",
                    "value": [{"schema_version": 1, "key": key} for key in keys],
                },
            },
            "constraints": constraints,
            "coverage": {"state": "complete", "gaps": []},
        },
        descriptors=tuple(descriptors),
    )


def test_review7_major1_conflicting_snapshots_with_different_caller_idempotency_keys_are_detected():
    """MAJOR quota_ledger.py:274 — 同一 pool/window/observed_at 的兩筆衝突
    snapshot，若呼叫端分別帶不同的 caller idempotency key，兩者的
    idempotency key 永遠不會撞在一起，ledger 不會產生 conflict receipt，
    project() 依 observation_id 任選一筆 remaining 當確定值。衝突偵測必須
    以語意範圍（pool／window／observed_at／metric）為準，不論 idempotency
    key 是否不同。"""
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    service = shadow_module.QuotaShadowService.in_memory()

    first = _observation(descriptor, "short", value="18", observed_at_ms=_NOW)
    result_a = service.record_observation(
        first.to_dict(), descriptors=(descriptor,), unit_catalog=(),
        idempotency_key="caller-key-a",
    )
    assert result_a.accepted == 1

    second_payload = deepcopy(first.to_dict())
    second_payload["observation_id"] = "fixture-remaining-conflicting-caller-key"
    second_payload["measurement"]["quantity"]["amount"]["value"] = "9"
    result_b = service.record_observation(
        second_payload, descriptors=(descriptor,), unit_catalog=(),
        idempotency_key="caller-key-b",
    )
    assert result_b.status == "conflict"

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 1
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "unknown"
    assert "source-conflict" in row["coverage_gaps"]


def test_review7_major1_conflicting_snapshots_with_different_event_identity_are_detected():
    """MAJOR quota_ledger.py:274 — 同一 pool/window/observed_at 的兩筆衝突
    snapshot，若分別帶不同的外部 event_identity（不同 provider event id），
    「source:」key 因 event id 不同而永遠不會撞在一起，同樣看不到衝突。"""
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    service = shadow_module.QuotaShadowService.in_memory()

    first = _observation(
        descriptor, "short", value="18", observed_at_ms=_NOW, event_id="evt-a",
    )
    assert service.record_observation(
        first.to_dict(), descriptors=(descriptor,), unit_catalog=(),
    ).accepted == 1

    second_payload = deepcopy(first.to_dict())
    second_payload["observation_id"] = "fixture-remaining-conflicting-event-id"
    second_payload["measurement"]["quantity"]["amount"]["value"] = "9"
    second_payload["source"]["event_identity"]["event_id"] = "evt-b"
    result = service.record_observation(
        second_payload, descriptors=(descriptor,), unit_catalog=(),
    )
    assert result.status == "conflict"

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 1
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "unknown"
    assert "source-conflict" in row["coverage_gaps"]


def test_review7_major2_group_binding_replay_with_different_profile_keys_does_not_double_deduct():
    """MAJOR quota_shadow.py:307 — 同一 terminal job 若因 group binding／
    profile alias replay 先後以兩個 profile_key 記到同一 shared pool／
    window，replay key 含 profile_key，兩筆 usage 都被接受而雙重扣減。
    終局 usage 的去重 identity 必須是 (job_id, pool identity, metric,
    window 範圍)，不含 profile_key；數值相同時第二筆須判定為 duplicate。"""
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    binding = _group_binding((descriptor,), (_PROFILE_A, _PROFILE_B))
    service = shadow_module.QuotaShadowService.in_memory()

    snapshot = _observation(
        descriptor, "short", value="20", observed_at_ms=_NOW,
        reset_at_ms=_NOW + 300_000,
    )
    assert service.record_observation(
        snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=(),
    ).accepted == 1

    job = {
        "id": "job-group-replay", "executor": "codex",
        "started_at": _iso_utc_ms(_NOW + 1), "finished_at": _iso_utc_ms(_NOW + 2),
        "usage": {"input_tokens": 4},
    }
    first = service.record_terminal_usage(
        job, profile_key=_PROFILE_A, binding=binding, descriptors=(descriptor,),
        unit_catalog=(), unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 5,
    )
    assert first.accepted == 1

    # 同一 job 因 profile alias replay，改以另一個 group member 的
    # profile_key 記到同一個 shared pool/window，用量數值不變。
    replay = service.record_terminal_usage(
        deepcopy(job), profile_key=_PROFILE_B, binding=binding, descriptors=(descriptor,),
        unit_catalog=(), unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 6,
    )
    assert replay.accepted == 0
    assert replay.duplicates == 1
    assert replay.conflicts == 0

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 10
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "observed"
    assert row["remaining"]["amount"] == {"kind": "exact", "value": "16"}


def test_review7_major2_group_binding_replay_with_different_usage_value_is_conflict():
    """同上情境，但 replay 的用量數值不同時必須是 conflict、該 pool
    unknown，不能被靜默接受成第二筆獨立扣減。"""
    _, _, shadow_module = _feature_api()
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    binding = _group_binding((descriptor,), (_PROFILE_A, _PROFILE_B))
    service = shadow_module.QuotaShadowService.in_memory()

    snapshot = _observation(
        descriptor, "short", value="20", observed_at_ms=_NOW,
        reset_at_ms=_NOW + 300_000,
    )
    assert service.record_observation(
        snapshot.to_dict(), descriptors=(descriptor,), unit_catalog=(),
    ).accepted == 1

    job = {
        "id": "job-group-replay-conflict", "executor": "codex",
        "started_at": _iso_utc_ms(_NOW + 1), "finished_at": _iso_utc_ms(_NOW + 2),
        "usage": {"input_tokens": 4},
    }
    first = service.record_terminal_usage(
        job, profile_key=_PROFILE_A, binding=binding, descriptors=(descriptor,),
        unit_catalog=(), unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 5,
    )
    assert first.accepted == 1

    changed_job = deepcopy(job)
    changed_job["usage"]["input_tokens"] = 9
    changed = service.record_terminal_usage(
        changed_job, profile_key=_PROFILE_B, binding=binding, descriptors=(descriptor,),
        unit_catalog=(), unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 6,
    )
    assert changed.accepted == 0
    assert changed.conflicts == 1

    report = service.project(
        descriptors=(descriptor,), unit_catalog=(), now_utc_ms=_NOW + 10
    )
    row = _pool_row(report, "pool-shared", "short")
    assert row["remaining"]["state"] == "unknown"


def test_review7_major3_unresolvable_usage_event_forces_associated_pool_unknown_instead_of_being_dropped():
    """MAJOR quota_shadow.py:791 — 先記一筆透過 associations 指向 credit
    pool 的不可換算 token usage，project() 若因為缺少該 usage 自身引用的
    unit（例如舊版／rollback descriptor）而無法解析，scope=unknown 的事件
    會被 _mark_unresolved_record 直接丟掉：credit pool 的 row 仍顯示舊
    snapshot 餘額，等同放行一筆看不懂的耗用事件。無法解讀的 usage 事件不得
    丟棄；受影響 pool 必須轉 unknown 並附上明確 gap（例如
    usage-unresolvable）。"""
    _, _, shadow_module = _feature_api()
    token_descriptor = _pool_descriptor(
        account="acct-tokens", pool="pool-tokens", unit_id="token",
        windows=(("month", 2_678_400_000),),
    )
    credit_descriptor = _pool_descriptor(
        account="acct-credit", pool="pool-credit", unit_id="premium-credit",
        semantics_ref="provider:github-copilot-sdk/premium-interactions/v1",
        windows=(("month", 2_678_400_000),),
    )
    binding = _binding((token_descriptor, credit_descriptor), _PROFILE_A)
    service = shadow_module.QuotaShadowService.in_memory()

    credit_snapshot = _observation(
        credit_descriptor, "month", value="10", observed_at_ms=_NOW, unit_id="premium-credit",
    )
    assert service.record_observation(
        credit_snapshot.to_dict(), descriptors=(credit_descriptor,), unit_catalog=()
    ).accepted == 1

    result = service.record_terminal_usage(
        {"id": "job-unresolvable-usage", "executor": "codex", "usage": {"input_tokens": 3}},
        profile_key=_PROFILE_A,
        binding=binding,
        descriptors=(token_descriptor, credit_descriptor),
        unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("token", "1")},
        observed_at_ms=_NOW + 10,
    )
    assert result.accepted == 2

    # project() 只帶 credit_descriptor：credit pool 那筆不可換算 usage 自身
    # 引用的 token unit 因而缺失——模擬缺少該 usage unit 的舊版／rollback
    # descriptor。
    report = service.project(
        descriptors=(credit_descriptor,), unit_catalog=(), now_utc_ms=_NOW + 20
    )
    credit_row = _pool_row(report, "pool-credit", "month")
    assert credit_row["remaining"]["state"] == "unknown"
    assert "usage-unresolvable" in credit_row["coverage_gaps"]
