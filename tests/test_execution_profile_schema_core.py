"""Contract coverage for the accepted execution-profile schema-core plan."""

from __future__ import annotations

import builtins
from copy import deepcopy
import json
from importlib import import_module
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Callable

import pytest


_MODULE_NAME = "paulsha_cortex.coordinator.execution_profile"


def _api():
    try:
        module = import_module(_MODULE_NAME)
    except ModuleNotFoundError as exc:
        if exc.name == _MODULE_NAME:
            pytest.fail(
                "execution profile schema-core module is not implemented yet",
                pytrace=False,
            )
        raise

    required = (
        "parse_descriptor",
        "parse_profile",
        "canonical_profile_bytes",
        "profile_key",
        "actual_condition_key",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.fail(
            "execution profile schema-core module is missing public API: "
            + ", ".join(sorted(missing)),
            pytrace=False,
        )
    return module


def _descriptor_payload(grammar: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "fixture-descriptor",
        "adapter": {
            "id": "fixture-adapter",
            "protocol_id": "fixture-wire",
            "protocol_version": "1",
            "runtime_version": "1",
        },
        "model": {"id": "fixture-model", "revision": "r1"},
        "effort_grammar": grammar,
        "provenance": [],
        "metadata": {},
    }


def _profile_payload(
    *,
    plane: str,
    effort: object,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "plane": plane,
        "conditions": {
            "adapter": {
                "state": "known",
                "value": {
                    "id": "fixture-adapter",
                    "protocol_id": "fixture-wire",
                    "protocol_version": "1",
                    "runtime_version": "1",
                },
            },
            "model": {
                "state": "known",
                "value": {"id": "fixture-model", "revision": "r1"},
            },
            "effort": {"state": "known", "value": effort},
            "loadout": {
                "state": "known",
                "value": {"id": "fixture-loadout", "version": "1"},
            },
            "toolset": {"state": "known", "value": []},
            "sandbox": {
                "state": "known",
                "value": {"id": "fixture-sandbox", "version": "1"},
            },
            "permissions": {"state": "known", "value": []},
            "toolchain": {
                "state": "known",
                "value": {"id": "fixture-toolchain", "version": "1"},
            },
        },
        "requirements": {
            "role": {"state": "known", "value": "review"},
            "minimum_quality": {"state": "known", "value": None},
            "pin": {"state": "known", "value": None},
            "independence": {"state": "known", "value": None},
        },
        "provenance": [],
        "metadata": {} if metadata is None else metadata,
    }


def _parse_observed(api, descriptor_payload: dict[str, object], profile_payload):
    descriptor = api.parse_descriptor(descriptor_payload)
    return descriptor, api.parse_profile(profile_payload, descriptor)


def _assert_error(api, operation: Callable[[], object], code: str):
    with pytest.raises(api.ExecutionProfileError) as excinfo:
        operation()
    assert excinfo.value.code == code
    return excinfo.value


@pytest.mark.parametrize(
    ("grammar", "value"),
    [
        (
            {"type": "string", "enum": ["fixture-fast", "fixture-new"]},
            "fixture-new",
        ),
        ({"type": "integer", "min": 0, "max": 8}, 3),
        ({"type": "number", "min": 0.0, "max": 1.0}, 0.625),
        (
            {
                "type": "object",
                "properties": {
                    "temperature": {"type": "number", "min": 0.0, "max": 1.0},
                    "tools": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["temperature", "tools"],
            },
            {"temperature": 0.625, "tools": ["fixture-tool", "fixture-new-tool"]},
        ),
    ],
)
def test_descriptor_accepts_data_defined_effort_types_and_roundtrips(
    grammar: dict[str, object], value: object
) -> None:
    api = _api()
    descriptor_payload = _descriptor_payload(grammar)
    profile_payload = _profile_payload(plane="requested", effort=value)
    descriptor_before = deepcopy(descriptor_payload)
    profile_before = deepcopy(profile_payload)

    descriptor = api.parse_descriptor(descriptor_payload)
    profile = api.parse_profile(profile_payload, descriptor)

    assert descriptor_payload == descriptor_before
    assert profile_payload == profile_before
    assert descriptor.to_dict() == descriptor_before
    assert profile.to_dict() == profile_before

    # Returned snapshots are not aliases into the immutable parsed value.
    descriptor_copy = descriptor.to_dict()
    profile_copy = profile.to_dict()
    descriptor_copy["metadata"]["mutated"] = True
    profile_copy["conditions"]["toolset"]["value"].append(
        {"id": "fixture-mutated", "version": "1"}
    )
    assert descriptor.to_dict() == descriptor_before
    assert profile.to_dict() == profile_before


def test_requested_resolved_and_observed_are_independent_roundtrippable_planes() -> None:
    api = _api()
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))
    payloads = {
        "requested": _profile_payload(plane="requested", effort=0.25),
        "resolved": _profile_payload(plane="resolved", effort=0.5),
        "observed": _profile_payload(plane="observed", effort=0.75),
    }

    records = {
        plane: api.parse_profile(payload, descriptor)
        for plane, payload in payloads.items()
    }

    assert [records[plane].to_dict()["plane"] for plane in payloads] == [
        "requested",
        "resolved",
        "observed",
    ]
    assert [
        records[plane].to_dict()["conditions"]["effort"]["value"]
        for plane in payloads
    ] == [0.25, 0.5, 0.75]
    assert {api.profile_key(records[plane]).split(":")[2] for plane in payloads} == {
        "request",
        "resolved",
        "observed",
    }


def test_observed_profile_uses_frozen_actual_and_record_key_vectors() -> None:
    api = _api()
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))
    observed = api.parse_profile(
        _profile_payload(
            plane="observed",
            effort=1.0,
            metadata={"pricing": {"amount": 9.0, "unit": "fixture"}},
        ),
        descriptor,
    )

    assert (
        api._typed_json_bytes(api._actual_projection(observed))
        == (
            b'["o",[["conditions",["o",[["adapter",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixtur'
            b'e-adapter"]],["protocol_id",["s","fixture-wire"]],["protocol_version",["s","1"]],["runtime_version",'
            b'["s","1"]]]]]]]],["effort",["o",[["state",["s","known"]],["value",["f","3ff0000000000000"]]]]],["loa'
            b'dout",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-loadout"]],["version",["s","1'
            b'"]]]]]]]],["model",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-model"]],["revis'
            b'ion",["s","r1"]]]]]]]],["permissions",["o",[["state",["s","known"]],["value",["a",[]]]]]],["sandbox"'
            b',["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-sandbox"]],["version",["s","1"]]]]'
            b']]]],["toolchain",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-toolchain"]],["ve'
            b'rsion",["s","1"]]]]]]]],["toolset",["o",[["state",["s","known"]],["value",["a",[]]]]]]]]],["schema_v'
            b'ersion",["i","1"]]]]'
        )
    )
    assert api.canonical_profile_bytes(observed) == (
        b'["o",[["conditions",["o",[["adapter",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixtur'
        b'e-adapter"]],["protocol_id",["s","fixture-wire"]],["protocol_version",["s","1"]],["runtime_version",'
        b'["s","1"]]]]]]]],["effort",["o",[["state",["s","known"]],["value",["f","3ff0000000000000"]]]]],["loa'
        b'dout",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-loadout"]],["version",["s","1'
        b'"]]]]]]]],["model",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-model"]],["revis'
        b'ion",["s","r1"]]]]]]]],["permissions",["o",[["state",["s","known"]],["value",["a",[]]]]]],["sandbox"'
        b',["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-sandbox"]],["version",["s","1"]]]]'
        b']]]],["toolchain",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-toolchain"]],["ve'
        b'rsion",["s","1"]]]]]]]],["toolset",["o",[["state",["s","known"]],["value",["a",[]]]]]]]]],["plane",['
        b'"s","observed"]],["requirements",["o",[["independence",["o",[["state",["s","known"]],["value",["n"]]'
        b']]],["minimum_quality",["o",[["state",["s","known"]],["value",["n"]]]]],["pin",["o",[["state",["s",'
        b'"known"]],["value",["n"]]]]],["role",["o",[["state",["s","known"]],["value",["s","review"]]]]]]]],["s'
        b'chema_version",["i","1"]]]]'
    )
    assert (
        api.profile_key(observed)
        == "epk:v1:observed:e2750040b4f91c563760daa19bcaa808f66bab7f4c445c3765522f19609b841b"
    )
    assert (
        api.actual_condition_key(observed)
        == "epk:v1:actual:cccfc64a59aed4d4f3c14caa2468b18ff1ea067eeb84e3f376e0e722799e9ac2"
    )


@pytest.mark.parametrize(
    ("grammar", "effort", "expected_key"),
    [
        (
            {"type": "integer"},
            1,
            "epk:v1:actual:a5b73772473ec7f2926cc56d0117cf8b5e6ffa68431090d5be3fc552b91e729f",
        ),
        (
            {"type": "string"},
            "1",
            "epk:v1:actual:0bbfb460e7bd4e09122ac9737d14f9efb3ecba7387a562078a0496ce395a9e30",
        ),
        (
            {"type": "number"},
            0.0,
            "epk:v1:actual:987c15ceb122f1e723247646330890502786b63a53a13bf01311b7e06a53cbd6",
        ),
        (
            {"type": "number"},
            -0.0,
            "epk:v1:actual:a637e42ea4416af2af0e34e869c48ef3682dceec152f7124b0855d5550731f26",
        ),
    ],
)
def test_actual_condition_key_matches_remaining_d44_golden_vectors(
    grammar: dict[str, object], effort: object, expected_key: str
) -> None:
    api = _api()
    descriptor = api.parse_descriptor(_descriptor_payload(grammar))
    observed = api.parse_profile(
        _profile_payload(plane="observed", effort=effort), descriptor
    )

    assert api.actual_condition_key(observed) == expected_key


@pytest.mark.parametrize(
    ("label", "descriptor_section", "condition_section", "key", "value"),
    [
        ("effort", None, "effort", None, 0.75),
        ("adapter id", "adapter", "adapter", "id", "fixture-adapter-v2"),
        (
            "adapter protocol id",
            "adapter",
            "adapter",
            "protocol_id",
            "fixture-wire-v2",
        ),
        (
            "adapter protocol version",
            "adapter",
            "adapter",
            "protocol_version",
            "2",
        ),
        (
            "adapter runtime version",
            "adapter",
            "adapter",
            "runtime_version",
            "2",
        ),
        ("model id", "model", "model", "id", "fixture-model-v2"),
        ("model revision", "model", "model", "revision", "r2"),
        ("loadout", None, "loadout", "version", "2"),
        (
            "toolset",
            None,
            "toolset",
            None,
            [{"id": "fixture-tool", "version": "1"}],
        ),
        ("sandbox", None, "sandbox", "id", "fixture-sandbox-v2"),
        (
            "permissions",
            None,
            "permissions",
            None,
            [{"id": "fixture-permission", "version": "1"}],
        ),
        ("toolchain", None, "toolchain", "version", "2"),
    ],
)
def test_each_execution_condition_perturbation_changes_actual_key(
    label: str,
    descriptor_section: str | None,
    condition_section: str,
    key: str | None,
    value: object,
) -> None:
    del label
    api = _api()
    descriptor_payload = _descriptor_payload({"type": "number"})
    baseline_payload = _profile_payload(plane="observed", effort=0.5)
    descriptor, baseline = _parse_observed(api, descriptor_payload, baseline_payload)
    baseline_key = api.actual_condition_key(baseline)
    assert baseline_key is not None

    changed_descriptor = deepcopy(descriptor_payload)
    changed_payload = deepcopy(baseline_payload)
    if descriptor_section is not None:
        changed_descriptor[descriptor_section][key] = value  # type: ignore[index]
        changed_payload["conditions"][condition_section]["value"][key] = value  # type: ignore[index]
    elif key is None:
        changed_payload["conditions"][condition_section]["value"] = deepcopy(value)  # type: ignore[index]
    else:
        changed_payload["conditions"][condition_section]["value"][key] = value  # type: ignore[index]

    changed_descriptor_record, changed = _parse_observed(
        api, changed_descriptor, changed_payload
    )
    if descriptor_section is not None:
        assert changed_descriptor_record != descriptor
    else:
        assert changed_descriptor_record == descriptor
    changed_key = api.actual_condition_key(changed)
    assert changed_key is not None
    assert changed_key != baseline_key


def test_record_and_actual_key_domains_are_separate() -> None:
    api = _api()
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))
    records = {
        plane: api.parse_profile(
            _profile_payload(plane=plane, effort=effort), descriptor
        )
        for plane, effort in (
            ("requested", 0.25),
            ("resolved", 0.5),
            ("observed", 0.75),
        )
    }

    record_keys = {api.profile_key(record) for record in records.values()}
    assert {key.split(":")[2] for key in record_keys} == {
        "request",
        "resolved",
        "observed",
    }
    actual_key = api.actual_condition_key(records["observed"])
    assert actual_key is not None
    assert actual_key.split(":")[2] == "actual"
    assert actual_key not in record_keys


@pytest.mark.parametrize(
    "field",
    [
        "adapter",
        "model",
        "effort",
        "loadout",
        "toolset",
        "sandbox",
        "permissions",
        "toolchain",
    ],
)
def test_unknown_observed_condition_blocks_actual_key_with_exact_reason(
    field: str,
) -> None:
    api = _api()
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))
    payload = _profile_payload(plane="observed", effort=0.5)
    payload["conditions"][field] = {  # type: ignore[index]
        "state": "unknown",
        "reason": f"fixture-{field}-unavailable",
    }
    profile = api.parse_profile(payload, descriptor)

    assert api.actual_condition_key(profile) is None
    assert api.actual_condition_missing_fields(profile) == (("conditions", field),)


def test_observed_actual_key_does_not_grant_approval_or_hash_provenance() -> None:
    api = _api()
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))
    base_payload = _profile_payload(plane="observed", effort=0.5)
    base = api.parse_profile(base_payload, descriptor)
    fake_evidence_payload = deepcopy(base_payload)
    fake_evidence_payload["provenance"] = [  # type: ignore[index]
        {"kind": "fake-review", "ref": "artifact:fixture/approval.json"}
    ]
    fake_evidence_payload["metadata"] = {  # type: ignore[index]
        "approval_receipt_refs": ["artifact:fixture/approval.json"],
        "pricing": {"amount": 1.0, "unit": "fixture"},
    }
    fake_evidence = api.parse_profile(fake_evidence_payload, descriptor)

    assert api.actual_condition_key(base) == api.actual_condition_key(fake_evidence)
    wire = fake_evidence.to_dict()
    assert all(name not in wire for name in ("approved", "qualified", "permission_grant"))
    assert not hasattr(fake_evidence, "approved")
    assert not hasattr(fake_evidence, "qualified")


def test_not_applicable_is_only_valid_for_a_none_effort_grammar() -> None:
    api = _api()
    none_descriptor = api.parse_descriptor(_descriptor_payload({"type": "none"}))
    none_payload = _profile_payload(plane="observed", effort=None)
    none_payload["conditions"]["effort"] = {"state": "not_applicable"}  # type: ignore[index]
    none_profile = api.parse_profile(none_payload, none_descriptor)
    assert none_profile.to_dict()["conditions"]["effort"] == {
        "state": "not_applicable"
    }
    assert api.actual_condition_key(none_profile) is not None

    number_descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))
    invalid_payload = _profile_payload(plane="observed", effort=0.5)
    invalid_payload["conditions"]["effort"] = {  # type: ignore[index]
        "state": "not_applicable"
    }
    error = _assert_error(
        api,
        lambda: api.parse_profile(invalid_payload, number_descriptor),
        "invalid_value",
    )
    assert "not_applicable" not in str(error)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing_version", "missing_field"),
        ("bool_version", "invalid_type"),
        ("future_version", "unsupported_schema"),
        ("unknown_field", "unknown_field"),
    ],
)
def test_descriptor_strict_version_and_field_contract(
    mutation: str, code: str
) -> None:
    api = _api()
    payload = _descriptor_payload({"type": "string"})
    if mutation == "missing_version":
        del payload["schema_version"]
    elif mutation == "bool_version":
        payload["schema_version"] = True
    elif mutation == "future_version":
        payload["schema_version"] = 2
    else:
        payload["extra"] = "fixture-secret-value"

    error = _assert_error(api, lambda: api.parse_descriptor(payload), code)
    assert "fixture-secret-value" not in str(error)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing_conditions", "missing_field"),
        ("unknown_field", "unknown_field"),
        ("bool_version", "invalid_type"),
        ("future_version", "unsupported_schema"),
    ],
)
def test_profile_strict_version_and_field_contract(
    mutation: str, code: str
) -> None:
    api = _api()
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))
    payload = _profile_payload(plane="observed", effort=0.5)
    if mutation == "missing_conditions":
        del payload["conditions"]
    elif mutation == "unknown_field":
        payload["extra"] = "fixture-secret-value"
    elif mutation == "bool_version":
        payload["schema_version"] = False
    else:
        payload["schema_version"] = 2

    error = _assert_error(api, lambda: api.parse_profile(payload, descriptor), code)
    assert "fixture-secret-value" not in str(error)


def test_missing_tagged_state_is_reported_as_missing_field() -> None:
    api = _api()
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))
    payload = _profile_payload(plane="observed", effort=0.5)
    del payload["conditions"]["effort"]["state"]  # type: ignore[index]

    _assert_error(
        api,
        lambda: api.parse_profile(payload, descriptor),
        "missing_field",
    )


def test_json_escape_decoded_duplicate_key_is_rejected() -> None:
    api = _api()
    payload = _descriptor_payload({"type": "string"})
    raw = json.dumps(payload, separators=(",", ":"))[:-1]
    duplicate_metadata = raw + r',"\u006detadata":{}}'

    error = _assert_error(
        api,
        lambda: api.parse_descriptor(duplicate_metadata),
        "duplicate_key",
    )
    assert "metadata" not in str(error)


@pytest.mark.parametrize(
    ("grammar", "value", "code"),
    [
        ({"type": "number"}, True, "invalid_type"),
        ({"type": "number"}, 1, "invalid_type"),
        ({"type": "integer"}, 1.0, "invalid_type"),
        ({"type": "string"}, 1, "invalid_type"),
    ],
)
def test_effort_native_types_are_not_coerced(
    grammar: dict[str, object], value: object, code: str
) -> None:
    api = _api()
    descriptor = api.parse_descriptor(_descriptor_payload(grammar))
    payload = _profile_payload(plane="observed", effort=value)
    _assert_error(api, lambda: api.parse_profile(payload, descriptor), code)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_json_numbers_are_rejected_before_profile_validation(
    token: str,
) -> None:
    api = _api()
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))
    payload = json.dumps(
        _profile_payload(plane="observed", effort=0.5),
        separators=(",", ":"),
    )
    assert payload.count("0.5") == 1
    payload = payload.replace("0.5", token)
    _assert_error(
        api,
        lambda: api.parse_profile(payload, descriptor),
        "non_finite_number",
    )


def test_large_integer_tokens_are_not_converted_through_float() -> None:
    api = _api()
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "integer"}))
    value = 9007199254740993
    payload = json.dumps(
        _profile_payload(plane="observed", effort=value),
        separators=(",", ":"),
    )
    profile = api.parse_profile(payload, descriptor)
    assert profile.to_dict()["conditions"]["effort"]["value"] == value


def test_float_exponent_and_surrogate_pair_json_tokens_are_valid() -> None:
    api = _api()
    number_descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))
    number_payload = json.dumps(
        _profile_payload(plane="observed", effort=0.5),
        separators=(",", ":"),
    ).replace("0.5", "1e0")
    number_profile = api.parse_profile(number_payload, number_descriptor)
    assert number_profile.to_dict()["conditions"]["effort"]["value"] == 1.0

    string_descriptor = api.parse_descriptor(_descriptor_payload({"type": "string"}))
    string_payload = json.dumps(
        _profile_payload(plane="observed", effort="fixture"),
        separators=(",", ":"),
    ).replace('"fixture"', r'"\ud83d\ude00"', 1)
    string_profile = api.parse_profile(string_payload, string_descriptor)
    assert string_profile.to_dict()["conditions"]["effort"]["value"] == "😀"


def test_invalid_unicode_and_native_cycles_are_rejected_without_echoing_values() -> None:
    api = _api()
    invalid_unicode = _descriptor_payload({"type": "string"})
    invalid_unicode["id"] = "fixture-secret-\ud800"
    error = _assert_error(
        api,
        lambda: api.parse_descriptor(invalid_unicode),
        "invalid_unicode",
    )
    assert "fixture-secret" not in str(error)

    cyclic = _descriptor_payload({"type": "string"})
    cyclic["metadata"]["cycle"] = cyclic["metadata"]  # type: ignore[index]
    _assert_error(api, lambda: api.parse_descriptor(cyclic), "cyclic_value")


@pytest.mark.parametrize(
    "reference",
    [
        "urn:fixture:observation-7",
        "artifact:reports/r7.json",
        "https://example.invalid/r/7",
        "future.namespace+v1:opaque~value?x=1&y=2",
    ],
)
def test_d1_reference_grammar_accepts_portable_opaque_positive_fixtures(
    reference: str,
) -> None:
    api = _api()
    payload = _descriptor_payload({"type": "string"})
    payload["provenance"] = [{"kind": "fixture", "ref": reference}]
    descriptor = api.parse_descriptor(payload)
    assert descriptor.to_dict()["provenance"] == [
        {"kind": "fixture", "ref": reference}
    ]


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "urn:",
        "/tmp/report.json",
        r"C:\temp\r.json",
        "urn:one two",
        " urn:fixture:item",
        "urn:fixture:item ",
        "urn:fixture:item\n",
        "URN:fixture:item",
        "a:body",
        "urn:é",
        "urn:" + ("x" * 1022),
    ],
)
def test_d1_reference_grammar_rejects_negative_fixtures_without_echo(
    reference: str,
) -> None:
    api = _api()
    payload = _descriptor_payload({"type": "string"})
    payload["provenance"] = [{"kind": "fixture", "ref": reference}]
    error = _assert_error(
        api,
        lambda: api.parse_descriptor(payload),
        "invalid_reference",
    )
    if reference:
        assert reference not in str(error)


def _nested_list_chain(count: int) -> list[object]:
    assert count >= 1
    value: list[object] = []
    for _ in range(count - 1):
        value = [value]
    return value


def test_d5_depth_boundary_has_accepting_and_rejecting_fixtures() -> None:
    api = _api()
    assert api.MAX_DEPTH == 16
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))

    accepting_payload = _profile_payload(
        plane="observed",
        effort=0.5,
        metadata={"discovery": _nested_list_chain(14)},
    )
    accepting = api.parse_profile(accepting_payload, descriptor)
    assert accepting.depth == 16

    rejecting_payload = _profile_payload(
        plane="observed",
        effort=0.5,
        metadata={"discovery": _nested_list_chain(15)},
    )
    _assert_error(
        api,
        lambda: api.parse_profile(rejecting_payload, descriptor),
        "depth_exceeded",
    )


def test_d5_node_count_boundary_has_accepting_and_rejecting_fixtures() -> None:
    api = _api()
    assert api.MAX_NODES == 4096
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))

    accepting_payload = _profile_payload(
        plane="observed",
        effort=0.5,
        metadata={"discovery": [0] * 3956},
    )
    accepting = api.parse_profile(accepting_payload, descriptor)
    assert accepting.node_count == 4096

    rejecting_payload = _profile_payload(
        plane="observed",
        effort=0.5,
        metadata={"discovery": [0] * 3957},
    )
    _assert_error(
        api,
        lambda: api.parse_profile(rejecting_payload, descriptor),
        "node_count_exceeded",
    )


def test_d5_semantic_byte_boundary_has_accepting_and_rejecting_fixtures() -> None:
    api = _api()
    assert api.MAX_SEMANTIC_BYTES == 65536
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))

    accepting_payload = _profile_payload(
        plane="observed",
        effort=0.5,
        metadata={"discovery": "x" * 63854},
    )
    accepting = api.parse_profile(accepting_payload, descriptor)
    assert accepting.semantic_bytes == 65536

    rejecting_payload = _profile_payload(
        plane="observed",
        effort=0.5,
        metadata={"discovery": "x" * 63855},
    )
    _assert_error(
        api,
        lambda: api.parse_profile(rejecting_payload, descriptor),
        "semantic_size_exceeded",
    )


def _profile_json_of_exact_size(target: int) -> str:
    compact = json.dumps(
        _profile_payload(plane="observed", effort=0.5),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert len(compact.encode("utf-8")) < target
    return " " * (target - len(compact.encode("utf-8"))) + compact


def test_d5_transport_boundary_has_accepting_and_rejecting_fixtures() -> None:
    api = _api()
    assert api.MAX_TEXT_BYTES == 1048576
    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))

    accepting_text = _profile_json_of_exact_size(1048576)
    assert len(accepting_text.encode("utf-8")) == 1048576
    accepting = api.parse_profile(accepting_text, descriptor)
    assert accepting.semantic_bytes < api.MAX_SEMANTIC_BYTES

    rejecting_text = _profile_json_of_exact_size(1048577)
    assert len(rejecting_text.encode("utf-8")) == 1048577
    _assert_error(
        api,
        lambda: api.parse_profile(rejecting_text, descriptor),
        "transport_too_large",
    )


def test_public_consumer_fixture_uses_only_pure_module_api(monkeypatch) -> None:
    api = _api()

    def forbidden(*args, **kwargs):
        del args, kwargs
        raise AssertionError("execution-profile core attempted external I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(time, "time", forbidden)

    descriptor = api.parse_descriptor(_descriptor_payload({"type": "number"}))
    profile = api.parse_profile(
        _profile_payload(plane="observed", effort=0.625), descriptor
    )
    record_bytes = api.canonical_profile_bytes(profile)
    assert isinstance(record_bytes, bytes)
    assert api.profile_key(profile).startswith("epk:v1:observed:")
    assert api.actual_condition_key(profile).startswith("epk:v1:actual:")
    assert profile.to_dict()["schema_version"] == 1


_CHILD_PROCESS_CODE = r'''
import hashlib
import json
import sys

from paulsha_cortex.coordinator.execution_profile import (
    actual_condition_key,
    canonical_profile_bytes,
    parse_descriptor,
    parse_profile,
    profile_key,
)

payload = json.load(sys.stdin)
descriptor = parse_descriptor(payload["descriptor"])
profile = parse_profile(payload["profile"], descriptor)
print(json.dumps({
    "canonical_sha256": hashlib.sha256(canonical_profile_bytes(profile)).hexdigest(),
    "profile_key": profile_key(profile),
    "actual_key": actual_condition_key(profile),
    "roundtrip": profile.to_dict(),
}, ensure_ascii=False, sort_keys=True))
'''


def test_public_api_roundtrips_in_fresh_process_across_hash_seeds(tmp_path: Path) -> None:
    descriptor = _descriptor_payload({"type": "number"})
    profile = _profile_payload(plane="observed", effort=0.625)
    input_text = json.dumps(
        {"descriptor": descriptor, "profile": profile},
        ensure_ascii=False,
        sort_keys=True,
    )
    repo_root = Path(__file__).resolve().parents[1]
    results = []
    for seed in ("1", "random"):
        child_environment = os.environ.copy()
        child_environment["PYTHONHASHSEED"] = seed
        child_environment["PYTHONPATH"] = str(repo_root)
        result = subprocess.run(
            [sys.executable, "-c", _CHILD_PROCESS_CODE],
            cwd=tmp_path,
            env=child_environment,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        results.append(json.loads(result.stdout))

    assert results[0] == results[1]
    assert results[0]["roundtrip"] == profile
    assert results[0]["profile_key"].startswith("epk:v1:observed:")
    assert results[0]["actual_key"].startswith("epk:v1:actual:")


def test_legacy_and_future_versions_are_rejected_without_mutating_old_bytes() -> None:
    api = _api()
    legacy = _descriptor_payload({"type": "string"})
    del legacy["schema_version"]
    legacy_bytes_before = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
    _assert_error(api, lambda: api.parse_descriptor(legacy), "missing_field")
    assert json.dumps(legacy, sort_keys=True, separators=(",", ":")) == legacy_bytes_before

    future = _descriptor_payload({"type": "string"})
    future["schema_version"] = 2
    _assert_error(api, lambda: api.parse_descriptor(future), "unsupported_schema")

    descriptor = api.parse_descriptor(_descriptor_payload({"type": "string"}))
    legacy_profile = _profile_payload(plane="observed", effort="fixture")
    del legacy_profile["schema_version"]
    legacy_profile_bytes_before = json.dumps(
        legacy_profile, sort_keys=True, separators=(",", ":")
    )
    _assert_error(
        api,
        lambda: api.parse_profile(legacy_profile, descriptor),
        "missing_field",
    )
    assert (
        json.dumps(legacy_profile, sort_keys=True, separators=(",", ":"))
        == legacy_profile_bytes_before
    )


def test_new_adapter_protocol_value_is_v1_descriptor_extension_and_changes_key() -> None:
    api = _api()
    base_descriptor_payload = _descriptor_payload({"type": "number"})
    base_profile_payload = _profile_payload(plane="observed", effort=0.5)
    base_descriptor, base_profile = _parse_observed(
        api, base_descriptor_payload, base_profile_payload
    )

    extended_descriptor_payload = deepcopy(base_descriptor_payload)
    extended_descriptor_payload["adapter"]["protocol_version"] = "v2"  # type: ignore[index]
    extended_profile_payload = deepcopy(base_profile_payload)
    extended_profile_payload["conditions"]["adapter"]["value"]["protocol_version"] = "v2"  # type: ignore[index]
    extended_descriptor, extended_profile = _parse_observed(
        api, extended_descriptor_payload, extended_profile_payload
    )

    assert base_descriptor.schema_version == extended_descriptor.schema_version == 1
    assert base_profile.schema_version == extended_profile.schema_version == 1
    assert api.actual_condition_key(base_profile) != api.actual_condition_key(
        extended_profile
    )
