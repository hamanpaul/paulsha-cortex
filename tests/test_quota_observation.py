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
        "constraints": [
            {
                "state": "known",
                "value": {
                    "pool_ref": {
                        "authority_id": "fixture-authority",
                        "account_id": "fixture-account-a",
                        "pool_id": "fixture-pool-a",
                        "revision": "fixture-revision-1",
                    },
                    "window_id": "fixture-window-short",
                },
            }
        ],
        "coverage": {"state": "complete", "gaps": []},
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


def test_parsed_records_ignore_later_source_and_result_mutations() -> None:
    api = _quota_api()
    unit_payload = _unit_definition_payload()
    observation_payload = _cold_start_observation_payload()

    unit = api["parse_unit_definition"](deepcopy(unit_payload))
    observation = api["parse_observation"](
        deepcopy(observation_payload),
        descriptors=(),
        unit_catalog=(unit,),
    )

    unit_payload["semantics_ref"] = "fixture:mutated-source/v2"
    observation_payload["measurement"]["quantity"]["amount"]["value"] = "999"

    mutated_unit_dict = unit.to_dict()
    mutated_unit_dict["semantics_ref"] = "fixture:mutated-result/v3"
    mutated_observation_dict = observation.to_dict()
    mutated_observation_dict["measurement"]["quantity"]["amount"]["value"] = "777"

    assert unit.to_dict() == _unit_definition_payload()
    assert observation.to_dict() == _cold_start_observation_payload()


def test_context_records_reject_caller_constructed_dataclasses() -> None:
    api = _quota_api()

    raw_unit = api["UnitDefinition"](
        schema_version=1,
        unit_id="fixture-native-token",
        version="1",
        quantity_kind="amount",
        semantics_ref="fixture:native-token/v1",
        _wire={"schema_version": 1},
        _json_bytes=1,
    )
    raw_descriptor = api["PoolDescriptor"](
        schema_version=1,
        authority_id="fixture-authority",
        account_id="fixture-account-a",
        pool_id="fixture-pool-a",
        revision="fixture-revision-1",
        units=(),
        windows=(),
        _wire={"schema_version": 1},
        _json_bytes=1,
    )

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


def test_descriptor_and_catalog_records_stay_immutable_after_roundtrip() -> None:
    api = _quota_api()
    descriptor_payload = _pool_descriptor_payload()
    unit_payload = _unit_definition_payload()
    binding_payload = _binding_payload()
    observation_payload = _cold_start_observation_payload()

    descriptor = api["parse_pool_descriptor"](deepcopy(descriptor_payload))
    unit = api["parse_unit_definition"](deepcopy(unit_payload))
    binding = api["parse_binding"](deepcopy(binding_payload), descriptors=(descriptor,))
    observation = api["parse_observation"](
        deepcopy(observation_payload),
        descriptors=(descriptor,),
        unit_catalog=(unit,),
    )

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

    assert descriptor.to_dict() == _pool_descriptor_payload()
    assert unit.to_dict() == _unit_definition_payload()
    assert binding.to_dict() == _binding_payload()
    assert observation.to_dict() == _cold_start_observation_payload()


def test_helper_scaffolds_remain_deferred_in_t2() -> None:
    api = _quota_api()
    descriptor = api["parse_pool_descriptor"](deepcopy(_pool_descriptor_payload()))
    unit = api["parse_unit_definition"](deepcopy(_unit_definition_payload()))
    binding = api["parse_binding"](deepcopy(_binding_payload()), descriptors=(descriptor,))
    observation = api["parse_observation"](
        deepcopy(_cold_start_observation_payload()),
        descriptors=(descriptor,),
        unit_catalog=(unit,),
    )

    assert dict(api["binding_status"](binding)) == {"state": "deferred"}
    assert dict(
        api["freshness"](observation, now_utc_ms=1000, allowed_clock_skew_ms=0)
    ) == {"state": "deferred"}
    assert dict(api["event_identity"](observation)) == {"state": "deferred"}


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
