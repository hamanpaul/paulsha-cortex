"""RED coverage for the accepted execution-profile schema-core plan.

The production module is intentionally absent at this stage.  Importing it inside
the tests keeps the RED result as a normal pytest failure (exit status 1), rather
than a collection error, while making the missing public contract explicit.
"""

from __future__ import annotations

from copy import deepcopy
from importlib import import_module

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

    assert isinstance(api.canonical_profile_bytes(observed), bytes)
    assert (
        api.profile_key(observed)
        == "epk:v1:observed:e2750040b4f91c563760daa19bcaa808f66bab7f4c445c3765522f19609b841b"
    )
    assert (
        api.actual_condition_key(observed)
        == "epk:v1:actual:cccfc64a59aed4d4f3c14caa2468b18ff1ea067eeb84e3f376e0e722799e9ac2"
    )
