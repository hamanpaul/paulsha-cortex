"""#866：quota observation schema core 的 RED regression 測試。

這些 fixture 釘住 accepted plan / design 的 D1、D4a、D7 契約，先證明目前缺少
`paulsha_cortex.coordinator.quota_observation` 的 public API。profile key 只沿用
pin 588d7d8 的 #849 frozen framing fixture；本檔不 import 或重算 upstream
profile parser / fingerprint。
"""

from __future__ import annotations

import builtins
from copy import deepcopy
from importlib import import_module
import inspect
import json
import os
import socket
import subprocess

import pytest


_PROFILE_SHA = "a" * 64


def _profile_key(domain: str) -> str:
    return f"epk:v1:{domain}:{_PROFILE_SHA}"


def _unit_definition_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "unit_id": "fixture-native-token",
        "version": "1",
        "quantity_kind": "amount",
        "semantics_ref": "fixture:native-token/v1",
    }


def _pool_descriptor_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "authority_id": "fixture-authority",
        "account_id": "fixture-account-a",
        "pool_id": "fixture-pool-a",
        "revision": "fixture-revision-1",
        "authority_ref": "fixture:pool-authority/v1",
        "provenance_refs": ["fixture:pool-descriptor/1"],
        "units": [
            {
                "unit_id": "fixture-native-token",
                "version": "1",
                "quantity_kind": "amount",
                "semantics_ref": "fixture:native-token/v1",
            }
        ],
        "windows": [
            {
                "window_id": "fixture-window-short",
                "kind": "rolling",
                "unit_ref": {"unit_id": "fixture-native-token", "version": "1"},
                "duration_ms": 60000,
            }
        ],
    }


def _binding_constraint(window_id: str = "fixture-window-short") -> dict[str, object]:
    return {
        "state": "known",
        "value": {
            "pool_ref": {
                "authority_id": "fixture-authority",
                "account_id": "fixture-account-a",
                "pool_id": "fixture-pool-a",
                "revision": "fixture-revision-1",
            },
            "window_id": window_id,
        },
    }


def _binding_payload(*, domain: str = "request") -> dict[str, object]:
    return {
        "schema_version": 1,
        "binding_id": "fixture-binding-1",
        "revision": "fixture-binding-revision-1",
        "subject": {
            "kind": "profile",
            "profile_ref": {
                "state": "known",
                "value": {"schema_version": 1, "key": _profile_key(domain)},
            },
        },
        "constraints": [_binding_constraint()],
        "coverage": {"state": "complete", "gaps": []},
    }


def _group_binding_payload(*, members_state: str = "known") -> dict[str, object]:
    payload = _binding_payload()
    payload["subject"] = {
        "kind": "group",
        "group_ref": "fixture:group/v1",
        "revision": "fixture-group-revision-1",
        "members": {
            "state": members_state,
            "value": [{"schema_version": 1, "key": _profile_key("request")}],
        }
        if members_state == "known"
        else {"state": "unknown", "reason": "unresolved-group"},
    }
    return payload


def _known_scope_value(window_id: str = "fixture-window-short") -> dict[str, object]:
    return {
        "pool_ref": {
            "authority_id": "fixture-authority",
            "account_id": "fixture-account-a",
            "pool_id": "fixture-pool-a",
            "revision": "fixture-revision-1",
        },
        "window_id": window_id,
    }


def _cold_start_observation_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "observation_id": "fixture-receipt-1",
        "scope": {"state": "unknown", "reason": "unresolved-account"},
        "profile_ref": {"state": "unknown", "reason": "unresolved-profile"},
        "unit_ref": {
            "state": "known",
            "value": {"unit_id": "fixture-native-token", "version": "1"},
        },
        "window_instance": {"kind": "unknown", "reason": "unresolved-window"},
        "measurement": {
            "kind": "usage_delta",
            "metric_id": "fixture-token-usage",
            "quantity": {
                "state": "observed",
                "amount": {"kind": "exact", "value": "123"},
            },
        },
        "observed_at_ms": {"state": "known", "value": 1000},
        "received_at_ms": 1001,
        "ttl_ms": {"state": "known", "value": 60000},
        "reset_at_ms": {"state": "unknown", "reason": "missing-reset"},
        "source": {
            "source_id": "fixture-source",
            "source_schema": "fixture-event-v1",
            "adapter_version": "fixture-adapter-v1",
            "authority_ref": "fixture:source-contract/v1",
            "method": "executor_usage",
            "provenance_refs": ["fixture:source-event/123"],
            "event_identity": {"state": "unknown", "reason": "missing-event-id"},
        },
        "coverage": {"state": "unknown", "gaps": []},
    }


def _unknown_unit_observation_payload(kind: str) -> dict[str, object]:
    payload = _cold_start_observation_payload()
    payload["unit_ref"] = {"state": "unknown", "reason": "missing-unit"}
    payload["measurement"] = {
        "kind": kind,
        "metric_id": "fixture-token-usage",
        "quantity": {"state": "unknown", "reason": "missing-unit"},
    }
    if kind == "usage_total":
        payload["measurement"]["counter"] = {
            "scope_id": "fixture-counter",
            "epoch": {"state": "unknown", "reason": "missing-counter-epoch"},
        }
    return payload


def _known_scope_observation_payload() -> dict[str, object]:
    payload = _cold_start_observation_payload()
    payload["scope"] = {"state": "known", "value": _known_scope_value()}
    return payload


def _gauge_unit_definition_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "unit_id": "fixture-gauge-unit",
        "version": "1",
        "quantity_kind": "gauge",
        "semantics_ref": "fixture:gauge-unit/v1",
    }


def _gauge_pool_descriptor_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "authority_id": "fixture-authority",
        "account_id": "fixture-account-a",
        "pool_id": "fixture-gauge-pool-a",
        "revision": "fixture-revision-1",
        "authority_ref": "fixture:pool-authority/v1",
        "provenance_refs": ["fixture:pool-descriptor/1"],
        "units": [
            {
                "unit_id": "fixture-gauge-unit",
                "version": "1",
                "quantity_kind": "gauge",
                "semantics_ref": "fixture:gauge-unit/v1",
            }
        ],
        "windows": [
            {
                "window_id": "fixture-window-live",
                "kind": "instantaneous",
                "unit_ref": {"unit_id": "fixture-gauge-unit", "version": "1"},
            }
        ],
    }


def _known_event_observation_payload() -> dict[str, object]:
    payload = _cold_start_observation_payload()
    payload["source"]["event_identity"] = {
        "state": "known",
        "namespace": "fixture-provider",
        "epoch": "fixture-epoch-1",
        "event_id": "fixture-event-1",
    }
    return payload


def _quota_api() -> dict[str, object]:
    try:
        module = import_module("paulsha_cortex.coordinator.quota_observation")
    except ModuleNotFoundError as exc:
        if exc.name == "paulsha_cortex.coordinator.quota_observation":
            pytest.fail(
                "quota observation contract module is not implemented yet",
                pytrace=False,
            )
        raise

    required = (
        "QuotaContractError",
        "UnitDefinition",
        "PoolDescriptor",
        "ProfilePoolBinding",
        "QuotaObservation",
        "parse_unit_definition",
        "parse_pool_descriptor",
        "parse_binding",
        "parse_observation",
        "binding_status",
        "freshness",
        "event_identity",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.fail(
            "quota observation contract module is missing public API: "
            + ", ".join(sorted(missing)),
            pytrace=False,
        )
    return {name: getattr(module, name) for name in required}


def test_public_api_exports_record_types_and_roundtrips_synthetic_fixtures() -> None:
    api = _quota_api()

    for name in (
        "QuotaContractError",
        "UnitDefinition",
        "PoolDescriptor",
        "ProfilePoolBinding",
        "QuotaObservation",
    ):
        assert inspect.isclass(api[name]), name
    for name in (
        "parse_unit_definition",
        "parse_pool_descriptor",
        "parse_binding",
        "parse_observation",
        "binding_status",
        "freshness",
        "event_identity",
    ):
        assert callable(api[name]), name

    unit_payload = _unit_definition_payload()
    descriptor_payload = _pool_descriptor_payload()
    binding_payload = _binding_payload()
    observation_payload = _cold_start_observation_payload()

    unit = api["parse_unit_definition"](deepcopy(unit_payload))
    descriptor = api["parse_pool_descriptor"](deepcopy(descriptor_payload))
    binding = api["parse_binding"](deepcopy(binding_payload), descriptors=(descriptor,))
    observation = api["parse_observation"](
        deepcopy(observation_payload),
        descriptors=(),
        unit_catalog=(unit,),
    )

    assert isinstance(unit, api["UnitDefinition"])
    assert unit.to_dict() == unit_payload
    assert isinstance(descriptor, api["PoolDescriptor"])
    assert descriptor.to_dict() == descriptor_payload
    assert isinstance(binding, api["ProfilePoolBinding"])
    assert binding.to_dict() == binding_payload
    assert isinstance(observation, api["QuotaObservation"])
    assert observation.to_dict() == observation_payload


def test_parse_observation_requires_explicit_unit_catalog_keyword() -> None:
    api = _quota_api()
    signature = inspect.signature(api["parse_observation"])

    assert tuple(signature.parameters) == ("payload", "descriptors", "unit_catalog")
    assert signature.parameters["descriptors"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["unit_catalog"].kind is inspect.Parameter.KEYWORD_ONLY

    with pytest.raises(TypeError):
        api["parse_observation"](_cold_start_observation_payload(), descriptors=())


def test_context_record_types_require_parse_entrypoints_for_construction() -> None:
    api = _quota_api()

    construction_cases = (
        (
            "UnitDefinition",
            {
                "schema_version": 1,
                "unit_id": "fixture-native-token",
                "version": "1",
                "quantity_kind": "amount",
                "semantics_ref": "fixture:native-token/v1",
            },
            "parse_unit_definition",
        ),
        (
            "PoolDescriptor",
            {
                "schema_version": 1,
                "authority_id": "fixture-authority",
                "account_id": "fixture-account-a",
                "pool_id": "fixture-pool-a",
                "revision": "fixture-revision-1",
                "units": (),
                "windows": (),
            },
            "parse_pool_descriptor",
        ),
        (
            "ProfilePoolBinding",
            {
                "schema_version": 1,
                "binding_id": "fixture-binding-1",
                "revision": "fixture-binding-revision-1",
            },
            "parse_binding",
        ),
        (
            "QuotaObservation",
            {
                "schema_version": 1,
                "observation_id": "fixture-observation-1",
                "source_id": "fixture-source",
            },
            "parse_observation",
        ),
    )

    for record_name, kwargs, parser_name in construction_cases:
        with pytest.raises(TypeError, match=parser_name):
            api[record_name](**kwargs)


def test_unit_definition_enforces_strict_shape_and_redacted_error_locator() -> None:
    api = _quota_api()
    payload = _unit_definition_payload()
    payload["secret-token"] = "should-not-leak"

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_unit_definition"](payload)

    error = excinfo.value
    assert error.code == "invalid_shape"
    assert "<unknown>" in error.locator

    rendered = str(error)
    assert "secret-token" not in rendered
    assert "should-not-leak" not in rendered


def test_unit_definition_rejects_non_v1_schema() -> None:
    api = _quota_api()
    payload = _unit_definition_payload()
    payload["schema_version"] = 2

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_unit_definition"](payload)

    assert excinfo.value.code == "unsupported_schema"


def test_binding_rejects_more_than_64_constraints() -> None:
    api = _quota_api()
    descriptor = api["parse_pool_descriptor"](_pool_descriptor_payload())
    payload = _binding_payload()
    payload["constraints"] = [
        _binding_constraint(window_id=f"fixture-window-{index}") for index in range(65)
    ]

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_binding"](payload, descriptors=(descriptor,))

    assert excinfo.value.code == "resource_limit"
    assert excinfo.value.locator == ("constraints",)


def test_parse_unit_and_observation_leave_exact_payloads_unchanged_and_snapshot_results() -> None:
    api = _quota_api()
    unit_payload = _unit_definition_payload()
    observation_payload = _cold_start_observation_payload()
    unit_source_before = deepcopy(unit_payload)
    observation_source_before = deepcopy(observation_payload)

    unit = api["parse_unit_definition"](unit_payload)
    observation = api["parse_observation"](
        observation_payload,
        descriptors=(),
        unit_catalog=(unit,),
    )

    assert unit_payload == unit_source_before
    assert observation_payload == observation_source_before

    unit_payload["semantics_ref"] = "fixture:mutated-source/v2"
    observation_payload["measurement"]["quantity"]["amount"]["value"] = "999"

    mutated_unit_dict = unit.to_dict()
    mutated_unit_dict["semantics_ref"] = "fixture:mutated-result/v3"
    mutated_observation_dict = observation.to_dict()
    mutated_observation_dict["measurement"]["quantity"]["amount"]["value"] = "777"

    assert unit.to_dict() == unit_source_before
    assert observation.to_dict() == observation_source_before


def test_unit_definition_uses_canonical_public_shape_even_for_inline_descriptor_units() -> None:
    api = _quota_api()
    unit_payload = _unit_definition_payload()
    descriptor_payload = _pool_descriptor_payload()

    standalone_unit = api["parse_unit_definition"](deepcopy(unit_payload))
    descriptor = api["parse_pool_descriptor"](deepcopy(descriptor_payload))
    inline_unit = descriptor.units[0]

    assert inline_unit == standalone_unit
    assert inline_unit.to_dict() == unit_payload
    assert standalone_unit.to_dict() == unit_payload
    assert descriptor.to_dict()["units"] == descriptor_payload["units"]


def test_public_records_compare_and_hash_by_full_wire_payload() -> None:
    api = _quota_api()

    descriptor_payload = _pool_descriptor_payload()
    descriptor_variant_payload = _pool_descriptor_payload()
    descriptor_variant_payload["authority_ref"] = "fixture:pool-authority/v2"

    descriptor = api["parse_pool_descriptor"](deepcopy(descriptor_payload))
    descriptor_variant = api["parse_pool_descriptor"](
        deepcopy(descriptor_variant_payload)
    )

    assert descriptor.to_dict() != descriptor_variant.to_dict()
    assert descriptor != descriptor_variant
    assert len({descriptor, descriptor_variant}) == 2

    binding_payload = _binding_payload(domain="request")
    binding_variant_payload = _binding_payload(domain="resolved")

    binding = api["parse_binding"](deepcopy(binding_payload), descriptors=(descriptor,))
    binding_variant = api["parse_binding"](
        deepcopy(binding_variant_payload),
        descriptors=(descriptor,),
    )

    assert binding.to_dict() != binding_variant.to_dict()
    assert binding != binding_variant
    assert len({binding, binding_variant}) == 2

    unit = api["parse_unit_definition"](deepcopy(_unit_definition_payload()))
    observation_payload = _cold_start_observation_payload()
    observation_variant_payload = _cold_start_observation_payload()
    observation_variant_payload["source"]["adapter_version"] = "fixture-adapter-v2"

    observation = api["parse_observation"](
        deepcopy(observation_payload),
        descriptors=(),
        unit_catalog=(unit,),
    )
    observation_variant = api["parse_observation"](
        deepcopy(observation_variant_payload),
        descriptors=(),
        unit_catalog=(unit,),
    )

    assert observation.to_dict() != observation_variant.to_dict()
    assert observation != observation_variant
    assert len({observation, observation_variant}) == 2


def test_context_records_reject_forged_untrusted_dataclasses() -> None:
    api = _quota_api()

    raw_unit = object.__new__(api["UnitDefinition"])
    raw_descriptor = object.__new__(api["PoolDescriptor"])

    with pytest.raises(api["QuotaContractError"]) as observation_error:
        api["parse_observation"](
            _cold_start_observation_payload(),
            descriptors=(),
            unit_catalog=(raw_unit,),
        )

    assert observation_error.value.code == "invalid_type"
    assert observation_error.value.locator == ("unit_catalog", 0)

    with pytest.raises(api["QuotaContractError"]) as binding_error:
        api["parse_binding"](_binding_payload(), descriptors=(raw_descriptor,))

    assert binding_error.value.code == "invalid_type"
    assert binding_error.value.locator == ("descriptors", 0)


def test_parse_binding_and_context_records_leave_exact_sources_unchanged() -> None:
    api = _quota_api()
    descriptor_payload = _pool_descriptor_payload()
    unit_payload = _unit_definition_payload()
    binding_payload = _binding_payload()
    observation_payload = _cold_start_observation_payload()
    descriptor_source_before = deepcopy(descriptor_payload)
    unit_source_before = deepcopy(unit_payload)
    binding_source_before = deepcopy(binding_payload)
    observation_source_before = deepcopy(observation_payload)

    descriptor = api["parse_pool_descriptor"](descriptor_payload)
    unit = api["parse_unit_definition"](unit_payload)
    descriptor_record_before = descriptor.to_dict()
    unit_record_before = unit.to_dict()
    binding = api["parse_binding"](binding_payload, descriptors=(descriptor,))
    observation = api["parse_observation"](
        observation_payload,
        descriptors=(descriptor,),
        unit_catalog=(unit,),
    )

    assert descriptor_payload == descriptor_source_before
    assert unit_payload == unit_source_before
    assert binding_payload == binding_source_before
    assert observation_payload == observation_source_before
    assert descriptor.to_dict() == descriptor_record_before
    assert unit.to_dict() == unit_record_before

    assert dict(api["binding_status"](binding)) == {
        "state": "complete",
        "reasons": (),
    }
    assert dict(
        api["freshness"](observation, now_utc_ms=1000, allowed_clock_skew_ms=0)
    ) == {"state": "fresh", "reason": "within-ttl"}
    assert dict(api["event_identity"](observation)) == {
        "state": "unavailable",
        "reason": "source-event-id-unavailable",
    }

    descriptor_payload["units"][0]["semantics_ref"] = "fixture:mutated-source/v2"
    unit_payload["semantics_ref"] = "fixture:mutated-source/v2"
    binding_payload["revision"] = "fixture-mutated-binding-revision"
    observation_payload["source"]["adapter_version"] = "fixture-mutated-adapter"

    mutated_descriptor_dict = descriptor.to_dict()
    mutated_descriptor_dict["units"][0]["semantics_ref"] = "fixture:mutated-result/v3"
    mutated_unit_dict = unit.to_dict()
    mutated_unit_dict["semantics_ref"] = "fixture:mutated-result/v3"
    mutated_binding_dict = binding.to_dict()
    mutated_binding_dict["revision"] = "fixture-mutated-binding-result"
    mutated_observation_dict = observation.to_dict()
    mutated_observation_dict["source"]["adapter_version"] = "fixture-mutated-result"

    assert descriptor.to_dict() == descriptor_source_before
    assert unit.to_dict() == unit_source_before
    assert binding.to_dict() == binding_source_before
    assert observation.to_dict() == observation_source_before


def test_helpers_compute_documented_results() -> None:
    api = _quota_api()
    descriptor = api["parse_pool_descriptor"](deepcopy(_pool_descriptor_payload()))
    unit = api["parse_unit_definition"](deepcopy(_unit_definition_payload()))
    binding = api["parse_binding"](deepcopy(_binding_payload()), descriptors=(descriptor,))
    observation = api["parse_observation"](
        deepcopy(_cold_start_observation_payload()),
        descriptors=(descriptor,),
        unit_catalog=(unit,),
    )

    assert dict(api["binding_status"](binding)) == {
        "state": "complete",
        "reasons": (),
    }
    assert dict(
        api["freshness"](observation, now_utc_ms=1000, allowed_clock_skew_ms=0)
    ) == {"state": "fresh", "reason": "within-ttl"}
    assert dict(api["event_identity"](observation)) == {
        "state": "unavailable",
        "reason": "source-event-id-unavailable",
    }


@pytest.mark.parametrize(
    ("payload_factory", "expected_reasons"),
    (
        (
            lambda: _group_binding_payload(members_state="unknown"),
            ("subject-unknown",),
        ),
        (
            lambda: {
                **_binding_payload(),
                "constraints": [{"state": "unknown", "reason": "unresolved-alias"}],
            },
            ("constraint-unknown",),
        ),
        (
            lambda: {
                **_binding_payload(),
                "coverage": {
                    "state": "partial",
                    "gaps": [{"scope": "fixture-gap", "reason": "missing-evidence"}],
                },
            },
            ("coverage-incomplete",),
        ),
        (
            lambda: {
                **_binding_payload(),
                "constraints": [
                    {
                        "state": "known",
                        "value": {
                            "pool_ref": _known_scope_value()["pool_ref"],
                            "window_id": "fixture-window-unknown",
                        },
                    }
                ],
            },
            ("window-unknown",),
        ),
    ),
)
def test_binding_status_reports_incomplete_reasons(
    payload_factory,
    expected_reasons: tuple[str, ...],
) -> None:
    api = _quota_api()
    descriptor_payload = _pool_descriptor_payload()
    descriptor_payload["windows"].append(
        {
            "window_id": "fixture-window-unknown",
            "kind": "unknown",
            "unit_ref": {"unit_id": "fixture-native-token", "version": "1"},
            "reason": "missing-window-shape",
        }
    )
    descriptor = api["parse_pool_descriptor"](descriptor_payload)
    binding = api["parse_binding"](payload_factory(), descriptors=(descriptor,))

    assert dict(api["binding_status"](binding)) == {
        "state": "incomplete",
        "reasons": expected_reasons,
    }


def test_parse_binding_accepts_group_subject_and_known_members() -> None:
    api = _quota_api()
    descriptor = api["parse_pool_descriptor"](_pool_descriptor_payload())

    binding = api["parse_binding"](
        _group_binding_payload(),
        descriptors=(descriptor,),
    )

    assert binding.to_dict()["subject"]["kind"] == "group"
    assert dict(api["binding_status"](binding)) == {
        "state": "complete",
        "reasons": (),
    }


def test_binding_preserves_same_pool_short_and_week_constraints() -> None:
    api = _quota_api()
    descriptor_payload = _pool_descriptor_payload()
    descriptor_payload["windows"].append(
        {
            "window_id": "fixture-window-week",
            "kind": "rolling",
            "unit_ref": {"unit_id": "fixture-native-token", "version": "1"},
            "duration_ms": 604800000,
        }
    )
    descriptor = api["parse_pool_descriptor"](descriptor_payload)
    payload = _binding_payload()
    payload["constraints"] = [
        {"state": "known", "value": _known_scope_value("fixture-window-short")},
        {"state": "known", "value": _known_scope_value("fixture-window-week")},
    ]

    binding = api["parse_binding"](payload, descriptors=(descriptor,))

    assert binding.to_dict()["constraints"] == payload["constraints"]
    assert dict(api["binding_status"](binding)) == {
        "state": "complete",
        "reasons": (),
    }


def test_parse_helpers_do_not_touch_io_env_subprocess_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _quota_api()
    descriptor = api["parse_pool_descriptor"](deepcopy(_pool_descriptor_payload()))
    unit = api["parse_unit_definition"](deepcopy(_unit_definition_payload()))
    binding = api["parse_binding"](deepcopy(_binding_payload()), descriptors=(descriptor,))
    observation = api["parse_observation"](
        deepcopy(_cold_start_observation_payload()),
        descriptors=(descriptor,),
        unit_catalog=(unit,),
    )

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected external access")

    class _BlockedEnviron(dict[str, str]):
        def __getitem__(self, _key: str) -> str:
            _blocked()
            raise AssertionError("unreachable")

        def get(self, _key: str, _default: object = None) -> object:
            _blocked()
            raise AssertionError("unreachable")

        def __contains__(self, _key: object) -> bool:
            _blocked()
            raise AssertionError("unreachable")

        def copy(self) -> dict[str, str]:
            _blocked()
            raise AssertionError("unreachable")

        def __iter__(self):
            _blocked()
            return iter(())

    monkeypatch.setattr(builtins, "open", _blocked)
    monkeypatch.setattr(os, "getenv", _blocked)
    monkeypatch.setattr(os, "environ", _BlockedEnviron(), raising=False)
    monkeypatch.setattr(subprocess, "Popen", _blocked)
    monkeypatch.setattr(subprocess, "run", _blocked)
    monkeypatch.setattr(subprocess, "call", _blocked)
    monkeypatch.setattr(subprocess, "check_output", _blocked)
    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)

    api["parse_unit_definition"](deepcopy(_unit_definition_payload()))
    api["parse_pool_descriptor"](deepcopy(_pool_descriptor_payload()))
    rebound_binding = api["parse_binding"](
        deepcopy(_binding_payload()),
        descriptors=(descriptor,),
    )
    rebound_observation = api["parse_observation"](
        deepcopy(_cold_start_observation_payload()),
        descriptors=(descriptor,),
        unit_catalog=(unit,),
    )

    assert "state" in dict(api["binding_status"](binding))
    assert "state" in dict(api["binding_status"](rebound_binding))
    assert "state" in dict(
        api["freshness"](observation, now_utc_ms=1000, allowed_clock_skew_ms=0)
    )
    assert "state" in dict(
        api["freshness"](rebound_observation, now_utc_ms=1000, allowed_clock_skew_ms=0)
    )
    assert "state" in dict(api["event_identity"](observation))
    assert "state" in dict(api["event_identity"](rebound_observation))


def test_known_unit_ref_requires_explicit_catalog_context() -> None:
    api = _quota_api()

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](
            _cold_start_observation_payload(),
            descriptors=(),
            unit_catalog=(),
        )

    assert excinfo.value.code == "unresolved_reference"


def test_parse_observation_rejects_known_unit_measurement_kind_mismatch() -> None:
    api = _quota_api()
    unit_payload = _unit_definition_payload()
    unit_payload["unit_id"] = "fixture-native-gauge"
    unit_payload["quantity_kind"] = "gauge"
    unit_payload["semantics_ref"] = "fixture:native-gauge/v1"
    unit = api["parse_unit_definition"](unit_payload)
    payload = _cold_start_observation_payload()
    payload["unit_ref"]["value"]["unit_id"] = "fixture-native-gauge"
    payload["measurement"]["quantity"] = {
        "state": "unknown",
        "reason": "missing-gauge-value",
    }

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](payload, descriptors=(), unit_catalog=(unit,))

    assert excinfo.value.code == "incompatible_semantics"
    assert excinfo.value.locator == ("measurement", "kind")


def test_parse_binding_rejects_known_constraints_without_unique_descriptor_match() -> None:
    api = _quota_api()
    payload = _binding_payload()

    with pytest.raises(api["QuotaContractError"]) as missing_excinfo:
        api["parse_binding"](deepcopy(payload), descriptors=())

    assert missing_excinfo.value.code == "unresolved_reference"
    assert missing_excinfo.value.locator == ("constraints", 0)

    descriptor = api["parse_pool_descriptor"](_pool_descriptor_payload())

    with pytest.raises(api["QuotaContractError"]) as ambiguous_excinfo:
        api["parse_binding"](
            deepcopy(payload),
            descriptors=(descriptor, api["parse_pool_descriptor"](_pool_descriptor_payload())),
        )

    assert ambiguous_excinfo.value.code == "duplicate_reference"
    assert ambiguous_excinfo.value.locator == ("constraints", 0)


def test_parse_observation_rejects_duplicate_descriptor_pool_refs_even_when_scope_unknown() -> None:
    api = _quota_api()
    descriptor = api["parse_pool_descriptor"](_pool_descriptor_payload())
    unit = api["parse_unit_definition"](_unit_definition_payload())

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](
            _cold_start_observation_payload(),
            descriptors=(descriptor, api["parse_pool_descriptor"](_pool_descriptor_payload())),
            unit_catalog=(unit,),
        )

    assert excinfo.value.code == "duplicate_reference"
    assert excinfo.value.locator == ("descriptors", 1)


def test_parse_observation_accepts_descriptor_backed_known_scope_with_explicit_context() -> None:
    api = _quota_api()
    descriptor = api["parse_pool_descriptor"](_pool_descriptor_payload())
    payload = _known_scope_observation_payload()

    observation = api["parse_observation"](
        payload,
        descriptors=(descriptor,),
        unit_catalog=(),
    )

    assert observation.to_dict() == payload


def test_scope_resolution_keeps_different_accounts_distinct() -> None:
    api = _quota_api()
    descriptor_a = api["parse_pool_descriptor"](_pool_descriptor_payload())
    descriptor_b_payload = _pool_descriptor_payload()
    descriptor_b_payload["account_id"] = "fixture-account-b"
    descriptor_b_payload["pool_id"] = "fixture-pool-b"
    descriptor_b_payload["windows"][0]["window_id"] = "fixture-window-week"
    descriptor_b = api["parse_pool_descriptor"](descriptor_b_payload)
    payload = _cold_start_observation_payload()
    payload["scope"] = {
        "state": "known",
        "value": {
            "pool_ref": {
                "authority_id": "fixture-authority",
                "account_id": "fixture-account-b",
                "pool_id": "fixture-pool-b",
                "revision": "fixture-revision-1",
            },
            "window_id": "fixture-window-week",
        },
    }
    payload["window_instance"] = {
        "kind": "interval",
        "start_ms": 1000,
        "end_ms": 61000,
        "epoch": {"state": "unknown", "reason": "missing-counter-epoch"},
    }

    observation = api["parse_observation"](
        payload,
        descriptors=(descriptor_a, descriptor_b),
        unit_catalog=(),
    )

    assert observation.to_dict()["scope"]["value"]["pool_ref"]["account_id"] == "fixture-account-b"


def test_known_scope_requires_window_consistent_with_measurement_even_if_unit_unknown() -> None:
    api = _quota_api()
    descriptor = api["parse_pool_descriptor"](_gauge_pool_descriptor_payload())
    payload = _cold_start_observation_payload()
    payload["scope"] = {
        "state": "known",
        "value": {
            "pool_ref": {
                "authority_id": "fixture-authority",
                "account_id": "fixture-account-a",
                "pool_id": "fixture-gauge-pool-a",
                "revision": "fixture-revision-1",
            },
            "window_id": "fixture-window-live",
        },
    }
    payload["unit_ref"] = {"state": "unknown", "reason": "missing-unit"}
    payload["measurement"] = {
        "kind": "usage_delta",
        "metric_id": "fixture-gauge",
        "quantity": {"state": "unknown", "reason": "missing-unit"},
    }

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](payload, descriptors=(descriptor,), unit_catalog=())

    assert excinfo.value.code == "incompatible_semantics"
    assert excinfo.value.locator == ("measurement", "kind")


def test_known_scope_gauge_requires_instant_window_instance() -> None:
    api = _quota_api()
    descriptor = api["parse_pool_descriptor"](_gauge_pool_descriptor_payload())
    gauge_unit = api["parse_unit_definition"](_gauge_unit_definition_payload())
    payload = _cold_start_observation_payload()
    payload["scope"] = {
        "state": "known",
        "value": {
            "pool_ref": {
                "authority_id": "fixture-authority",
                "account_id": "fixture-account-a",
                "pool_id": "fixture-gauge-pool-a",
                "revision": "fixture-revision-1",
            },
            "window_id": "fixture-window-live",
        },
    }
    payload["unit_ref"] = {
        "state": "known",
        "value": {"unit_id": "fixture-gauge-unit", "version": "1"},
    }
    payload["measurement"] = {
        "kind": "gauge_snapshot",
        "metric_id": "fixture-gauge",
        "quantity": {"state": "unknown", "reason": "missing-gauge-value"},
    }
    payload["window_instance"] = {"kind": "instant", "at_ms": 1000}

    observation = api["parse_observation"](
        deepcopy(payload),
        descriptors=(descriptor,),
        unit_catalog=(gauge_unit,),
    )
    assert observation.to_dict() == payload

    payload["window_instance"] = {
        "kind": "interval",
        "start_ms": 1000,
        "end_ms": 2000,
        "epoch": {"state": "unknown", "reason": "missing-counter-epoch"},
    }
    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](
            payload,
            descriptors=(descriptor,),
            unit_catalog=(gauge_unit,),
        )

    assert excinfo.value.code == "incompatible_semantics"
    assert excinfo.value.locator == ("window_instance", "kind")


def test_known_scope_rolling_window_requires_exact_interval_width() -> None:
    api = _quota_api()
    descriptor = api["parse_pool_descriptor"](_pool_descriptor_payload())
    payload = _known_scope_observation_payload()
    payload["window_instance"] = {
        "kind": "interval",
        "start_ms": 1000,
        "end_ms": 61000,
        "epoch": {"state": "unknown", "reason": "missing-counter-epoch"},
    }

    observation = api["parse_observation"](
        deepcopy(payload),
        descriptors=(descriptor,),
        unit_catalog=(),
    )
    assert observation.to_dict() == payload

    payload["window_instance"]["end_ms"] = 62000
    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](payload, descriptors=(descriptor,), unit_catalog=())

    assert excinfo.value.code == "incompatible_semantics"
    assert excinfo.value.locator == ("window_instance", "end_ms")


def test_duplicate_standalone_unit_catalog_ref_is_rejected() -> None:
    api = _quota_api()
    unit_one = api["parse_unit_definition"](_unit_definition_payload())
    unit_two = api["parse_unit_definition"](_unit_definition_payload())

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](
            _cold_start_observation_payload(),
            descriptors=(),
            unit_catalog=(unit_one, unit_two),
        )

    assert excinfo.value.code == "duplicate_reference"


def test_unit_catalog_rejects_more_than_16_entries() -> None:
    api = _quota_api()
    payloads = []
    for index in range(17):
        payload = _unit_definition_payload()
        payload["unit_id"] = f"fixture-native-token-{index}"
        payload["semantics_ref"] = f"fixture:native-token/{index}"
        payloads.append(api["parse_unit_definition"](payload))

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](
            _cold_start_observation_payload(),
            descriptors=(),
            unit_catalog=tuple(payloads),
        )

    assert excinfo.value.code == "resource_limit"
    assert excinfo.value.locator == ("unit_catalog",)


def test_unit_catalog_accepts_exact_16_entries() -> None:
    api = _quota_api()
    units = [api["parse_unit_definition"](_unit_definition_payload())]
    for index in range(1, 16):
        payload = _unit_definition_payload()
        payload["unit_id"] = f"fixture-native-token-{index}"
        payload["semantics_ref"] = f"fixture:native-token/{index}"
        units.append(api["parse_unit_definition"](payload))

    observation = api["parse_observation"](
        _cold_start_observation_payload(),
        descriptors=(),
        unit_catalog=tuple(units),
    )

    assert observation.to_dict()["unit_ref"] == {
        "state": "known",
        "value": {"unit_id": "fixture-native-token", "version": "1"},
    }


def test_bounded_walker_rejects_cycles_depth_and_node_overflow() -> None:
    api = _quota_api()

    cyclic_payload = _unit_definition_payload()
    cyclic_payload["extra"] = cyclic_payload
    with pytest.raises(api["QuotaContractError"]) as cycle_excinfo:
        api["parse_unit_definition"](cyclic_payload)
    assert cycle_excinfo.value.code == "cyclic_input"

    deep_payload = _unit_definition_payload()
    current: dict[str, object] = {}
    deep_payload["extra"] = current
    for index in range(17):
        current[str(index)] = {}
        current = current[str(index)]  # type: ignore[assignment]
    with pytest.raises(api["QuotaContractError"]) as depth_excinfo:
        api["parse_unit_definition"](deep_payload)
    assert depth_excinfo.value.code == "resource_limit"

    node_payload = _unit_definition_payload()
    node_payload["extra"] = list(range(5000))
    with pytest.raises(api["QuotaContractError"]) as node_excinfo:
        api["parse_unit_definition"](node_payload)
    assert node_excinfo.value.code == "resource_limit"


def test_string_and_root_byte_limits_fail_closed_before_shape_validation() -> None:
    api = _quota_api()

    max_ref = "aa:" + ("b" * 1021)
    payload = _unit_definition_payload()
    payload["semantics_ref"] = max_ref
    unit = api["parse_unit_definition"](payload)
    rendered = json.dumps(
        unit.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert len(rendered) < 2048

    overflow_string_payload = _unit_definition_payload()
    overflow_string_payload["semantics_ref"] = "aa:" + ("b" * 1022)
    with pytest.raises(api["QuotaContractError"]) as string_excinfo:
        api["parse_unit_definition"](overflow_string_payload)
    assert string_excinfo.value.code == "resource_limit"

    overflow_root_payload = _cold_start_observation_payload()
    overflow_root_payload["extra"] = ["x" * 1024] * 70
    with pytest.raises(api["QuotaContractError"]) as root_excinfo:
        api["parse_observation"](overflow_root_payload, descriptors=(), unit_catalog=())
    assert root_excinfo.value.code == "resource_limit"


def test_equal_descriptor_and_standalone_unit_refs_share_context_without_conflict() -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    descriptor = api["parse_pool_descriptor"](_pool_descriptor_payload())

    observation = api["parse_observation"](
        _known_scope_observation_payload(),
        descriptors=(descriptor,),
        unit_catalog=(unit,),
    )

    assert observation.to_dict()["unit_ref"] == {
        "state": "known",
        "value": {"unit_id": "fixture-native-token", "version": "1"},
    }


def test_conflicting_unit_definition_across_descriptor_and_catalog_is_rejected() -> None:
    api = _quota_api()
    conflicting_payload = _unit_definition_payload()
    conflicting_payload["semantics_ref"] = "fixture:native-token/v2"
    conflicting_unit = api["parse_unit_definition"](conflicting_payload)
    descriptor = api["parse_pool_descriptor"](_pool_descriptor_payload())

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](
            _known_scope_observation_payload(),
            descriptors=(descriptor,),
            unit_catalog=(conflicting_unit,),
        )

    assert excinfo.value.code == "unit_conflict"


@pytest.mark.parametrize(
    "domain",
    ("request", "resolved", "observed", "actual"),
)
def test_profile_ref_domain_roundtrips_without_upgrading(domain: str) -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()
    payload["profile_ref"] = {
        "state": "known",
        "value": {"schema_version": 1, "key": _profile_key(domain)},
    }

    observation = api["parse_observation"](
        payload,
        descriptors=(),
        unit_catalog=(unit,),
    )

    assert observation.to_dict()["profile_ref"] == payload["profile_ref"]


def test_profile_ref_shape_does_not_create_qualification_or_actual_state() -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()
    payload["profile_ref"] = {
        "state": "known",
        "value": {"schema_version": 1, "key": _profile_key("actual")},
    }

    observation = api["parse_observation"](
        payload,
        descriptors=(),
        unit_catalog=(unit,),
    )

    assert observation.to_dict()["profile_ref"]["value"]["key"] == _profile_key("actual")
    assert set(dict(api["event_identity"](observation))) <= {"state", "reason", "key"}


@pytest.mark.parametrize(
    "measurement_kind",
    ("remaining_snapshot", "usage_delta", "usage_total", "gauge_snapshot"),
)
def test_unknown_unit_and_unknown_quantity_variants_are_allowed(
    measurement_kind: str,
) -> None:
    api = _quota_api()
    payload = _unknown_unit_observation_payload(measurement_kind)

    observation = api["parse_observation"](payload, descriptors=(), unit_catalog=())

    assert observation.to_dict()["measurement"]["kind"] == measurement_kind


@pytest.mark.parametrize(
    "quantity",
    (
        {"state": "observed", "amount": {"kind": "exact", "value": "123"}},
        {
            "state": "estimated",
            "amount": {"kind": "exact", "value": "123"},
            "method_ref": "fixture:estimator/v1",
        },
    ),
)
def test_unknown_unit_ref_cannot_carry_numeric_quantity(
    quantity: dict[str, object],
) -> None:
    api = _quota_api()
    payload = _unknown_unit_observation_payload("usage_delta")
    payload["measurement"]["quantity"] = quantity

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](payload, descriptors=(), unit_catalog=())

    assert excinfo.value.code == "incompatible_semantics"


def test_amount_bounds_roundtrip_is_supported_for_known_units() -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()
    payload["measurement"]["quantity"]["amount"] = {
        "kind": "bounds",
        "lower": "1",
        "upper": "2.5",
    }

    observation = api["parse_observation"](
        payload,
        descriptors=(),
        unit_catalog=(unit,),
    )

    assert (
        observation.to_dict()["measurement"]["quantity"]["amount"]
        == payload["measurement"]["quantity"]["amount"]
    )


@pytest.mark.parametrize(
    ("amount", "expected_code"),
    (
        ({"kind": "bounds", "lower": None, "upper": None}, "invalid_bounds"),
        ({"kind": "bounds", "lower": "3", "upper": "2"}, "invalid_bounds"),
        ({"kind": "bounds", "lower": "01", "upper": "2"}, "invalid_decimal"),
        ({"kind": "exact", "value": "1.0"}, "invalid_decimal"),
        ({"kind": "exact", "value": "-0"}, "invalid_decimal"),
        ({"kind": "exact", "value": "1e3"}, "invalid_decimal"),
        ({"kind": "exact", "value": "NaN"}, "invalid_decimal"),
        ({"kind": "exact", "value": "Infinity"}, "invalid_decimal"),
        ({"kind": "exact", "value": True}, "invalid_type"),
        ({"kind": "exact", "value": 0.5}, "invalid_type"),
    ),
)
def test_amount_variants_enforce_bounds_and_decimal_grammar(
    amount: dict[str, object],
    expected_code: str,
) -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()
    payload["measurement"]["quantity"]["amount"] = amount

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](payload, descriptors=(), unit_catalog=(unit,))

    assert excinfo.value.code == expected_code


@pytest.mark.parametrize(
    ("measurement", "expected_code", "expected_locator"),
    (
        (
            {
                "kind": "usage_total",
                "metric_id": "fixture-token-usage",
                "quantity": {
                    "state": "observed",
                    "amount": {"kind": "exact", "value": "123"},
                },
            },
            "invalid_shape",
            ("measurement", "counter"),
        ),
        (
            {
                "kind": "usage_delta",
                "metric_id": "fixture-token-usage",
                "quantity": {
                    "state": "observed",
                    "amount": {"kind": "exact", "value": "123"},
                },
                "counter": {
                    "scope_id": "fixture-counter",
                    "epoch": {"state": "unknown", "reason": "missing-counter-epoch"},
                },
            },
            "invalid_shape",
            ("measurement", "<unknown>"),
        ),
        (
            {
                "kind": "collector_guess",
                "metric_id": "fixture-token-usage",
                "quantity": {
                    "state": "observed",
                    "amount": {"kind": "exact", "value": "123"},
                },
            },
            "invalid_identifier",
            ("measurement", "kind"),
        ),
    ),
)
def test_parse_observation_rejects_undervalidated_measurement_variants(
    measurement: dict[str, object],
    expected_code: str,
    expected_locator: tuple[str | int, ...],
) -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()
    payload["measurement"] = measurement

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](payload, descriptors=(), unit_catalog=(unit,))

    assert excinfo.value.code == expected_code
    assert excinfo.value.locator == expected_locator


@pytest.mark.parametrize(
    ("mutator", "expected_locator"),
    (
        (
            lambda payload: payload["source"].__setitem__("method", "estimate"),
            ("source", "method"),
        ),
        (
            lambda payload: payload["source"].__setitem__("method", "legacy"),
            ("source", "method"),
        ),
    ),
)
def test_source_method_restricts_quantity_state(
    mutator,
    expected_locator: tuple[str | int, ...],
) -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()
    mutator(payload)

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](payload, descriptors=(), unit_catalog=(unit,))

    assert excinfo.value.code == "incompatible_semantics"
    assert excinfo.value.locator == expected_locator


def test_limit_signal_requires_status_or_structured_source_method() -> None:
    api = _quota_api()
    payload = _cold_start_observation_payload()
    payload["unit_ref"] = {"state": "unknown", "reason": "missing-unit"}
    payload["measurement"] = {
        "kind": "limit_signal",
        "metric_id": "fixture-limit",
        "signal": "quota-exhausted",
    }
    payload["source"]["method"] = "executor_usage"

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](payload, descriptors=(), unit_catalog=())

    assert excinfo.value.code == "incompatible_semantics"
    assert excinfo.value.locator == ("source", "method")


def test_parse_observation_rejects_unknown_source_method() -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()
    payload["source"]["method"] = "collector-guess"

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](payload, descriptors=(), unit_catalog=(unit,))

    assert excinfo.value.code == "invalid_identifier"
    assert excinfo.value.locator == ("source", "method")


@pytest.mark.parametrize(
    ("coverage", "expected_code", "expected_locator"),
    (
        (
            {"state": "incomplete", "gaps": []},
            "invalid_identifier",
            ("coverage", "state"),
        ),
        (
            {
                "state": "complete",
                "gaps": [{"scope": "fixture-gap", "reason": "missing-evidence"}],
            },
            "invalid_shape",
            ("coverage", "gaps"),
        ),
        (
            {"state": "partial", "gaps": []},
            "invalid_shape",
            ("coverage", "gaps"),
        ),
    ),
)
def test_parse_observation_rejects_invalid_coverage_states_and_gap_shapes(
    coverage: dict[str, object],
    expected_code: str,
    expected_locator: tuple[str | int, ...],
) -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()
    payload["coverage"] = coverage

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](payload, descriptors=(), unit_catalog=(unit,))

    assert excinfo.value.code == expected_code
    assert excinfo.value.locator == expected_locator


def test_freshness_states_follow_ttl_reset_and_window_end_precedence() -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()

    fresh = api["parse_observation"](deepcopy(payload), descriptors=(), unit_catalog=(unit,))
    assert dict(api["freshness"](fresh, now_utc_ms=1000, allowed_clock_skew_ms=0)) == {
        "state": "fresh",
        "reason": "within-ttl",
    }

    at_ttl = api["parse_observation"](deepcopy(payload), descriptors=(), unit_catalog=(unit,))
    assert dict(api["freshness"](at_ttl, now_utc_ms=61000, allowed_clock_skew_ms=0)) == {
        "state": "stale",
        "reason": "expired-observation",
    }

    reset_payload = deepcopy(payload)
    reset_payload["reset_at_ms"] = {"state": "known", "value": 5000}
    reset_obs = api["parse_observation"](reset_payload, descriptors=(), unit_catalog=(unit,))
    assert dict(api["freshness"](reset_obs, now_utc_ms=5000, allowed_clock_skew_ms=0)) == {
        "state": "stale",
        "reason": "expired-observation",
    }

    window_payload = deepcopy(payload)
    window_payload["window_instance"] = {
        "kind": "interval",
        "start_ms": 1000,
        "end_ms": 4000,
        "epoch": {"state": "unknown", "reason": "missing-counter-epoch"},
    }
    window_obs = api["parse_observation"](window_payload, descriptors=(), unit_catalog=(unit,))
    assert dict(api["freshness"](window_obs, now_utc_ms=4000, allowed_clock_skew_ms=0)) == {
        "state": "stale",
        "reason": "expired-observation",
    }


def test_freshness_reports_missing_inputs_and_future_source_time() -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()
    payload["ttl_ms"] = {"state": "unknown", "reason": "missing-ttl"}
    missing_ttl = api["parse_observation"](payload, descriptors=(), unit_catalog=(unit,))

    assert dict(api["freshness"](missing_ttl, now_utc_ms=1000, allowed_clock_skew_ms=0)) == {
        "state": "unknown",
        "reason": "missing-freshness-input",
    }

    future_payload = _cold_start_observation_payload()
    future_payload["observed_at_ms"] = {"state": "known", "value": 2000}
    future_obs = api["parse_observation"](
        future_payload,
        descriptors=(),
        unit_catalog=(unit,),
    )

    assert dict(api["freshness"](future_obs, now_utc_ms=1000, allowed_clock_skew_ms=0)) == {
        "state": "unknown",
        "reason": "future-source-time",
    }
    assert dict(api["freshness"](future_obs, now_utc_ms=1000, allowed_clock_skew_ms=1000)) == {
        "state": "fresh",
        "reason": "within-ttl",
    }


def test_time_and_duration_caps_are_enforced() -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()
    payload["observed_at_ms"] = {"state": "known", "value": 253402300799999}
    payload["ttl_ms"] = {"state": "unknown", "reason": "missing-ttl"}
    observation = api["parse_observation"](payload, descriptors=(), unit_catalog=(unit,))
    assert observation.to_dict()["observed_at_ms"]["value"] == 253402300799999

    invalid_time_payload = _cold_start_observation_payload()
    invalid_time_payload["observed_at_ms"] = {"state": "known", "value": 253402300800000}
    with pytest.raises(api["QuotaContractError"]) as time_excinfo:
        api["parse_observation"](invalid_time_payload, descriptors=(), unit_catalog=(unit,))
    assert time_excinfo.value.code == "invalid_time"
    assert time_excinfo.value.locator == ("observed_at_ms", "value")

    invalid_duration_payload = _cold_start_observation_payload()
    invalid_duration_payload["ttl_ms"] = {"state": "known", "value": 31622400001}
    with pytest.raises(api["QuotaContractError"]) as duration_excinfo:
        api["parse_observation"](
            invalid_duration_payload,
            descriptors=(),
            unit_catalog=(unit,),
        )
    assert duration_excinfo.value.code == "invalid_time"
    assert duration_excinfo.value.locator == ("ttl_ms", "value")


def test_event_identity_available_and_stable_across_receipt_changes() -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    observation_payload = _known_event_observation_payload()
    observation = api["parse_observation"](
        deepcopy(observation_payload),
        descriptors=(),
        unit_catalog=(unit,),
    )
    variant_payload = _known_event_observation_payload()
    variant_payload["observation_id"] = "fixture-receipt-2"
    variant_payload["received_at_ms"] = 2000
    variant_payload["source"]["adapter_version"] = "fixture-adapter-v2"
    variant = api["parse_observation"](
        variant_payload,
        descriptors=(),
        unit_catalog=(unit,),
    )

    expected_key = (
        "qev:v1",
        "fixture-source",
        "fixture-provider",
        "fixture-epoch-1",
        "fixture-event-1",
    )
    assert dict(api["event_identity"](observation)) == {
        "state": "available",
        "key": expected_key,
    }
    assert dict(api["event_identity"](variant)) == {
        "state": "available",
        "key": expected_key,
    }


def test_event_identity_collides_intentionally_for_same_source_event() -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    base_payload = _known_event_observation_payload()
    first = api["parse_observation"](
        deepcopy(base_payload),
        descriptors=(),
        unit_catalog=(unit,),
    )

    second_payload = _known_event_observation_payload()
    second_payload["measurement"] = {
        "kind": "remaining_snapshot",
        "metric_id": "fixture-remaining",
        "quantity": {"state": "observed", "amount": {"kind": "exact", "value": "456"}},
    }
    second = api["parse_observation"](
        second_payload,
        descriptors=(),
        unit_catalog=(unit,),
    )

    assert first != second
    assert dict(api["event_identity"](first)) == dict(api["event_identity"](second))


def test_freshness_rejects_invalid_clock_inputs() -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    observation = api["parse_observation"](
        _cold_start_observation_payload(),
        descriptors=(),
        unit_catalog=(unit,),
    )

    with pytest.raises(api["QuotaContractError"]) as now_excinfo:
        api["freshness"](observation, now_utc_ms=True, allowed_clock_skew_ms=0)

    assert now_excinfo.value.code == "invalid_type"
    assert now_excinfo.value.locator == ("now_utc_ms",)

    with pytest.raises(api["QuotaContractError"]) as skew_excinfo:
        api["freshness"](observation, now_utc_ms=1000, allowed_clock_skew_ms=300001)

    assert skew_excinfo.value.code == "invalid_time"
    assert skew_excinfo.value.locator == ("allowed_clock_skew_ms",)


def test_existing_usage_pipeline_trace_stays_registry_to_extract_usage() -> None:
    from paulsha_cortex.coordinator import registry as registry_module
    from paulsha_cortex.coordinator import usage_extractors

    assert registry_module.extract_usage is usage_extractors.extract_usage


def test_parse_pool_descriptor_rejects_window_unit_ref_missing_from_inline_units() -> None:
    api = _quota_api()
    payload = _pool_descriptor_payload()
    payload["windows"][0]["unit_ref"] = {
        "unit_id": "fixture-missing-unit",
        "version": "1",
    }

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_pool_descriptor"](payload)

    assert excinfo.value.code == "unresolved_reference"
    assert excinfo.value.locator == ("windows", 0, "unit_ref")


def test_parse_pool_descriptor_rejects_duplicate_inline_unit_refs() -> None:
    api = _quota_api()
    payload = _pool_descriptor_payload()
    payload["units"].append(deepcopy(payload["units"][0]))

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_pool_descriptor"](payload)

    assert excinfo.value.code == "duplicate_reference"
    assert excinfo.value.locator == ("units", 1)


def test_parse_pool_descriptor_rejects_duplicate_window_ids() -> None:
    api = _quota_api()
    payload = _pool_descriptor_payload()
    duplicate_window = deepcopy(payload["windows"][0])
    duplicate_window["duration_ms"] = 120000
    payload["windows"].append(duplicate_window)

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_pool_descriptor"](payload)

    assert excinfo.value.code == "duplicate_reference"
    assert excinfo.value.locator == ("windows", 1)

def test_parse_pool_descriptor_enforces_window_quantity_kind_matrix() -> None:
    api = _quota_api()
    amount_payload = _pool_descriptor_payload()
    amount_payload["windows"] = [
        {
            "window_id": "fixture-window-live",
            "kind": "instantaneous",
            "unit_ref": {"unit_id": "fixture-native-token", "version": "1"},
        }
    ]

    with pytest.raises(api["QuotaContractError"]) as amount_excinfo:
        api["parse_pool_descriptor"](amount_payload)

    assert amount_excinfo.value.code == "incompatible_semantics"
    assert amount_excinfo.value.locator == ("windows", 0, "kind")

    gauge_payload = _gauge_pool_descriptor_payload()
    gauge_payload["windows"] = [
        {
            "window_id": "fixture-window-short",
            "kind": "rolling",
            "unit_ref": {"unit_id": "fixture-gauge-unit", "version": "1"},
            "duration_ms": 60000,
        }
    ]
    with pytest.raises(api["QuotaContractError"]) as gauge_excinfo:
        api["parse_pool_descriptor"](gauge_payload)

    assert gauge_excinfo.value.code == "incompatible_semantics"
    assert gauge_excinfo.value.locator == ("windows", 0, "kind")


@pytest.mark.parametrize(
    ("window_instance", "expected_code", "expected_locator"),
    (
        (
            {"kind": "rolling", "at_ms": 1000},
            "invalid_identifier",
            ("window_instance", "kind"),
        ),
        (
            {"kind": "interval", "at_ms": 1000},
            "invalid_shape",
            ("window_instance", "<unknown>"),
        ),
    ),
)
def test_parse_observation_rejects_window_instance_kinds_without_documented_shapes(
    window_instance: dict[str, object],
    expected_code: str,
    expected_locator: tuple[str | int, ...],
) -> None:
    api = _quota_api()
    unit = api["parse_unit_definition"](_unit_definition_payload())
    payload = _cold_start_observation_payload()
    payload["window_instance"] = window_instance

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_observation"](payload, descriptors=(), unit_catalog=(unit,))

    assert excinfo.value.code == expected_code
    assert excinfo.value.locator == expected_locator


@pytest.mark.parametrize(
    ("window", "expected_code", "expected_locator"),
    (
        (
            {
                "window_id": "fixture-window-short",
                "kind": "calendar",
                "unit_ref": {"unit_id": "fixture-native-token", "version": "1"},
                "duration_ms": 60000,
            },
            "invalid_identifier",
            ("windows", 0, "kind"),
        ),
        (
            {
                "window_id": "fixture-window-short",
                "kind": "instantaneous",
                "unit_ref": {"unit_id": "fixture-native-token", "version": "1"},
                "duration_ms": 60000,
            },
            "invalid_shape",
            ("windows", 0, "<unknown>"),
        ),
    ),
)
def test_parse_pool_descriptor_rejects_undervalidated_window_variants(
    window: dict[str, object],
    expected_code: str,
    expected_locator: tuple[str | int, ...],
) -> None:
    api = _quota_api()
    payload = _pool_descriptor_payload()
    payload["windows"] = [window]

    with pytest.raises(api["QuotaContractError"]) as excinfo:
        api["parse_pool_descriptor"](payload)

    assert excinfo.value.code == expected_code
    assert excinfo.value.locator == expected_locator
