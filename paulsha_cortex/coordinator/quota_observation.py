"""Immutable quota observation wire-contract records.

This module is intentionally stdlib-only and side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import re
from types import MappingProxyType

__all__ = [
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
]

_SCHEMA_VERSION = 1
_MAX_DEPTH = 16
_MAX_NODES = 4096
_MAX_STRING_CODEPOINTS = 1024
_MAX_ROOT_BYTES = 65536
_MAX_UNIT_DEFINITION_BYTES = 2048
_MAX_CONTEXT_ITEMS = 16
_MAX_BINDING_CONTEXT_BYTES = 17 * _MAX_ROOT_BYTES
_MAX_OBSERVATION_CONTEXT_BYTES = _MAX_BINDING_CONTEXT_BYTES + (
    _MAX_CONTEXT_ITEMS * _MAX_UNIT_DEFINITION_BYTES
)
_TIME_MAX = 253402300799999
_DURATION_MAX = 31622400000
_ALLOWED_CLOCK_SKEW_MAX = 300000
_TRUST_MARKER = object()

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REASON_RE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_REF_NAMESPACE_RE = re.compile(r"[a-z][a-z0-9+.-]{1,31}\Z")
_REF_BODY_RE = re.compile(r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+\Z")
_PROFILE_KEY_RE = re.compile(
    r"epk:v1:(request|resolved|observed|actual):[0-9a-f]{64}\Z"
)
_DECIMAL_WIRE_RE = re.compile(r"(?:0|[1-9][0-9]{0,35})(?:\.[0-9]{0,17}[1-9])?\Z")

_UNIT_ROOT_KEYS = (
    "schema_version",
    "unit_id",
    "version",
    "quantity_kind",
    "semantics_ref",
)
_UNIT_INLINE_KEYS = ("unit_id", "version", "quantity_kind", "semantics_ref")
_POOL_DESCRIPTOR_KEYS = (
    "schema_version",
    "authority_id",
    "account_id",
    "pool_id",
    "revision",
    "authority_ref",
    "provenance_refs",
    "units",
    "windows",
)
_BINDING_KEYS = (
    "schema_version",
    "binding_id",
    "revision",
    "subject",
    "constraints",
    "coverage",
)
_OBSERVATION_KEYS = (
    "schema_version",
    "observation_id",
    "scope",
    "profile_ref",
    "unit_ref",
    "window_instance",
    "measurement",
    "observed_at_ms",
    "received_at_ms",
    "ttl_ms",
    "reset_at_ms",
    "source",
    "coverage",
)
_QUANTITY_KINDS = frozenset({"amount", "gauge"})
_SOURCE_METHODS = frozenset(
    {"provider_status", "structured_event", "executor_usage", "estimate", "legacy"}
)
_LIMIT_SIGNAL_METHODS = frozenset({"provider_status", "structured_event"})
_AMOUNT_MEASUREMENTS = frozenset({"remaining_snapshot", "usage_delta", "usage_total"})
_QUANTITY_MEASUREMENTS = frozenset(
    {"remaining_snapshot", "usage_delta", "usage_total", "gauge_snapshot"}
)


class QuotaContractError(ValueError):
    """Machine-readable validation failure."""

    def __init__(self, code: str, locator: tuple[str | int, ...] = ()) -> None:
        self.code = code
        self.locator = locator
        super().__init__(self._render_message())

    def _render_message(self) -> str:
        if not self.locator:
            return self.code
        return f"{self.code} at {_format_locator(self.locator)}"


@dataclass(frozen=True)
class UnitDefinition:
    schema_version: int
    unit_id: str
    version: str
    quantity_kind: str
    semantics_ref: str
    _wire: object = field(repr=False, compare=False)
    _json_bytes: int = field(repr=False, compare=False)
    _trusted: object | None = field(
        default=None,
        repr=False,
        compare=False,
        init=False,
    )

    @property
    def ref(self) -> tuple[str, str]:
        return (self.unit_id, self.version)

    @property
    def definition_key(self) -> tuple[int, str, str, str, str]:
        return (
            self.schema_version,
            self.unit_id,
            self.version,
            self.quantity_kind,
            self.semantics_ref,
        )

    def to_dict(self) -> dict[str, object]:
        return _thaw_root_dict(self._wire)


@dataclass(frozen=True)
class _WindowDefinition:
    window_id: str
    kind: str
    unit_ref: tuple[str, str]
    duration_ms: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PoolDescriptor:
    schema_version: int
    authority_id: str
    account_id: str
    pool_id: str
    revision: str
    units: tuple[UnitDefinition, ...]
    windows: tuple[_WindowDefinition, ...]
    _wire: object = field(repr=False, compare=False)
    _json_bytes: int = field(repr=False, compare=False)
    _trusted: object | None = field(
        default=None,
        repr=False,
        compare=False,
        init=False,
    )

    @property
    def pool_ref(self) -> tuple[str, str, str, str]:
        return (self.authority_id, self.account_id, self.pool_id, self.revision)

    def to_dict(self) -> dict[str, object]:
        return _thaw_root_dict(self._wire)


@dataclass(frozen=True)
class ProfilePoolBinding:
    schema_version: int
    binding_id: str
    revision: str
    _status: Mapping[str, object] = field(repr=False, compare=False)
    _wire: object = field(repr=False, compare=False)
    _json_bytes: int = field(repr=False, compare=False)
    _trusted: object | None = field(
        default=None,
        repr=False,
        compare=False,
        init=False,
    )

    def to_dict(self) -> dict[str, object]:
        return _thaw_root_dict(self._wire)


@dataclass(frozen=True)
class QuotaObservation:
    schema_version: int
    observation_id: str
    source_id: str
    _event_identity_components: tuple[str, str, str] | None = field(
        repr=False, compare=False
    )
    _observed_at_ms: int | None = field(repr=False, compare=False)
    _ttl_ms: int | None = field(repr=False, compare=False)
    _reset_at_ms: int | None = field(repr=False, compare=False)
    _window_end_ms: int | None = field(repr=False, compare=False)
    _wire: object = field(repr=False, compare=False)
    _json_bytes: int = field(repr=False, compare=False)
    _trusted: object | None = field(
        default=None,
        repr=False,
        compare=False,
        init=False,
    )

    def to_dict(self) -> dict[str, object]:
        return _thaw_root_dict(self._wire)


@dataclass(frozen=True)
class _KnownValue:
    state: str
    value: object | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _MeasurementInfo:
    kind: str
    quantity_state: str | None = None


@dataclass(frozen=True)
class _WindowInstanceInfo:
    kind: str
    start_ms: int | None = None
    end_ms: int | None = None


def _seal_record(record: object) -> object:
    object.__setattr__(record, "_trusted", _TRUST_MARKER)
    return record


def _is_trusted_record(value: object, expected_type: type[object]) -> bool:
    return type(value) is expected_type and getattr(value, "_trusted", None) is _TRUST_MARKER


def parse_unit_definition(payload: dict) -> UnitDefinition:
    snapshot, json_bytes = _snapshot_payload(payload, locator=(), byte_limit=_MAX_UNIT_DEFINITION_BYTES)
    _ensure_exact_keys(snapshot, _UNIT_ROOT_KEYS, ())
    schema_version = _parse_schema_version(snapshot.get("schema_version"), ("schema_version",))
    unit_id = _parse_identifier(snapshot.get("unit_id"), ("unit_id",))
    version = _parse_identifier(snapshot.get("version"), ("version",))
    quantity_kind = _parse_quantity_kind(snapshot.get("quantity_kind"), ("quantity_kind",))
    semantics_ref = _parse_ref(snapshot.get("semantics_ref"), ("semantics_ref",))
    return _seal_record(
        UnitDefinition(
            schema_version=schema_version,
            unit_id=unit_id,
            version=version,
            quantity_kind=quantity_kind,
            semantics_ref=semantics_ref,
            _wire=_freeze_wire(snapshot),
            _json_bytes=json_bytes,
        )
    )


def parse_pool_descriptor(payload: dict) -> PoolDescriptor:
    snapshot, json_bytes = _snapshot_payload(payload, locator=(), byte_limit=_MAX_ROOT_BYTES)
    _ensure_exact_keys(snapshot, _POOL_DESCRIPTOR_KEYS, ())
    schema_version = _parse_schema_version(snapshot.get("schema_version"), ("schema_version",))
    authority_id = _parse_identifier(snapshot.get("authority_id"), ("authority_id",))
    account_id = _parse_identifier(snapshot.get("account_id"), ("account_id",))
    pool_id = _parse_identifier(snapshot.get("pool_id"), ("pool_id",))
    revision = _parse_identifier(snapshot.get("revision"), ("revision",))
    _parse_ref(snapshot.get("authority_ref"), ("authority_ref",))
    _parse_ref_list(snapshot.get("provenance_refs"), ("provenance_refs",))
    units_raw = _expect_list(snapshot.get("units"), ("units",))
    if not units_raw:
        raise QuotaContractError("invalid_shape", ("units",))
    if len(units_raw) > _MAX_CONTEXT_ITEMS:
        raise QuotaContractError("resource_limit", ("units",))
    units: list[UnitDefinition] = []
    seen_unit_refs: set[tuple[str, str]] = set()
    for index, item in enumerate(units_raw):
        locator = ("units", index)
        unit_snapshot = _expect_dict(item, locator)
        _ensure_exact_keys(unit_snapshot, _UNIT_INLINE_KEYS, locator)
        unit = _parse_inline_unit(unit_snapshot, locator)
        if unit.ref in seen_unit_refs:
            raise QuotaContractError("duplicate_reference", locator)
        seen_unit_refs.add(unit.ref)
        units.append(unit)
    unit_map = {unit.ref: unit for unit in units}
    windows_raw = _expect_list(snapshot.get("windows"), ("windows",))
    if not windows_raw:
        raise QuotaContractError("invalid_shape", ("windows",))
    if len(windows_raw) > _MAX_CONTEXT_ITEMS:
        raise QuotaContractError("resource_limit", ("windows",))
    windows: list[_WindowDefinition] = []
    seen_window_ids: set[str] = set()
    for index, item in enumerate(windows_raw):
        locator = ("windows", index)
        window_snapshot = _expect_dict(item, locator)
        window = _parse_window_definition(window_snapshot, locator, unit_map)
        if window.window_id in seen_window_ids:
            raise QuotaContractError("duplicate_reference", locator)
        seen_window_ids.add(window.window_id)
        windows.append(window)
    return _seal_record(
        PoolDescriptor(
            schema_version=schema_version,
            authority_id=authority_id,
            account_id=account_id,
            pool_id=pool_id,
            revision=revision,
            units=tuple(units),
            windows=tuple(windows),
            _wire=_freeze_wire(snapshot),
            _json_bytes=json_bytes,
        )
    )


def parse_binding(
    payload: dict, *, descriptors: tuple[PoolDescriptor, ...]
) -> ProfilePoolBinding:
    descriptors = _validate_descriptors(descriptors)
    snapshot, json_bytes = _snapshot_payload(payload, locator=(), byte_limit=_MAX_ROOT_BYTES)
    _ensure_total_bytes(
        json_bytes + sum(descriptor._json_bytes for descriptor in descriptors),
        _MAX_BINDING_CONTEXT_BYTES,
        ("descriptors",),
    )
    _ensure_exact_keys(snapshot, _BINDING_KEYS, ())
    schema_version = _parse_schema_version(snapshot.get("schema_version"), ("schema_version",))
    binding_id = _parse_identifier(snapshot.get("binding_id"), ("binding_id",))
    revision = _parse_identifier(snapshot.get("revision"), ("revision",))
    subject_known = _parse_subject(snapshot.get("subject"), ("subject",))
    coverage_state = _parse_coverage(snapshot.get("coverage"), ("coverage",))
    constraints_raw = _expect_list(snapshot.get("constraints"), ("constraints",))
    if not constraints_raw:
        raise QuotaContractError("invalid_shape", ("constraints",))
    if len(constraints_raw) > 64:
        raise QuotaContractError("resource_limit", ("constraints",))
    known_constraints: set[tuple[tuple[str, str, str, str], str]] = set()
    reasons: list[str] = []
    for index, item in enumerate(constraints_raw):
        constraint = _parse_known_unknown(
            item,
            ("constraints", index),
            _parse_scope_value,
        )
        if constraint.state == "unknown":
            reasons.append("constraint-unknown")
            continue
        if constraint.value in known_constraints:
            raise QuotaContractError("duplicate_reference", ("constraints", index))
        known_constraints.add(constraint.value)
    if not subject_known:
        reasons.append("subject-unknown")
    if coverage_state != "complete":
        reasons.append("coverage-incomplete")
    unique_reasons = tuple(sorted(set(reasons)))
    status_state = "complete" if not unique_reasons else "incomplete"
    status = MappingProxyType({"state": status_state, "reasons": unique_reasons})
    return _seal_record(
        ProfilePoolBinding(
            schema_version=schema_version,
            binding_id=binding_id,
            revision=revision,
            _status=status,
            _wire=_freeze_wire(snapshot),
            _json_bytes=json_bytes,
        )
    )


def parse_observation(
    payload: dict,
    *,
    descriptors: tuple[PoolDescriptor, ...],
    unit_catalog: tuple[UnitDefinition, ...],
) -> QuotaObservation:
    descriptors = _validate_descriptors(descriptors)
    unit_catalog = _validate_unit_catalog(unit_catalog)
    snapshot, json_bytes = _snapshot_payload(payload, locator=(), byte_limit=_MAX_ROOT_BYTES)
    _ensure_total_bytes(
        json_bytes
        + sum(descriptor._json_bytes for descriptor in descriptors)
        + sum(unit._json_bytes for unit in unit_catalog),
        _MAX_OBSERVATION_CONTEXT_BYTES,
        ("unit_catalog",),
    )
    _ensure_exact_keys(snapshot, _OBSERVATION_KEYS, ())
    schema_version = _parse_schema_version(snapshot.get("schema_version"), ("schema_version",))
    observation_id = _parse_identifier(snapshot.get("observation_id"), ("observation_id",))
    _parse_known_unknown(
        snapshot.get("scope"),
        ("scope",),
        _parse_scope_value,
    )
    _parse_known_unknown(
        snapshot.get("profile_ref"),
        ("profile_ref",),
        _parse_profile_ref_value,
    )
    unit_ref = _parse_known_unknown(
        snapshot.get("unit_ref"),
        ("unit_ref",),
        _parse_unit_ref_value,
    )
    window_instance = _parse_window_instance(snapshot.get("window_instance"), ("window_instance",))
    measurement = _parse_measurement(snapshot.get("measurement"), ("measurement",))
    observed_at = _parse_known_unknown(
        snapshot.get("observed_at_ms"),
        ("observed_at_ms",),
        lambda value, locator: _parse_time(value, locator),
    )
    received_at_ms = _parse_time(snapshot.get("received_at_ms"), ("received_at_ms",))
    ttl_ms = _parse_known_unknown(
        snapshot.get("ttl_ms"),
        ("ttl_ms",),
        lambda value, locator: _parse_duration(value, locator),
    )
    reset_at_ms = _parse_known_unknown(
        snapshot.get("reset_at_ms"),
        ("reset_at_ms",),
        lambda value, locator: _parse_time(value, locator),
    )
    source_id, _source_method, event_components = _parse_source(
        snapshot.get("source"), ("source",)
    )
    _parse_coverage(snapshot.get("coverage"), ("coverage",))
    if observed_at.state == "known" and ttl_ms.state == "known":
        if observed_at.value + ttl_ms.value > _TIME_MAX:
            raise QuotaContractError("invalid_time", ("ttl_ms",))

    unit_map = _build_unit_catalog(unit_catalog)
    if unit_ref.state == "known":
        if unit_ref.value not in unit_map:
            raise QuotaContractError("unresolved_reference", ("unit_ref",))
    if unit_ref.state == "unknown" and measurement.quantity_state not in {None, "unknown"}:
        raise QuotaContractError("incompatible_semantics", ("measurement", "quantity"))

    return _seal_record(
        QuotaObservation(
            schema_version=schema_version,
            observation_id=observation_id,
            source_id=source_id,
            _event_identity_components=event_components,
            _observed_at_ms=observed_at.value if observed_at.state == "known" else None,
            _ttl_ms=ttl_ms.value if ttl_ms.state == "known" else None,
            _reset_at_ms=reset_at_ms.value if reset_at_ms.state == "known" else None,
            _window_end_ms=window_instance.end_ms if window_instance.kind == "interval" else None,
            _wire=_freeze_wire(snapshot),
            _json_bytes=json_bytes,
        )
    )


def binding_status(binding: ProfilePoolBinding) -> Mapping[str, object]:
    if not _is_trusted_record(binding, ProfilePoolBinding):
        raise QuotaContractError("invalid_type", ("binding",))
    return binding._status


def freshness(
    observation: QuotaObservation, *, now_utc_ms: int, allowed_clock_skew_ms: int
) -> Mapping[str, object]:
    if not _is_trusted_record(observation, QuotaObservation):
        raise QuotaContractError("invalid_type", ("observation",))
    now = _parse_time(now_utc_ms, ("now_utc_ms",))
    if type(allowed_clock_skew_ms) is not int or isinstance(allowed_clock_skew_ms, bool):
        raise QuotaContractError("invalid_type", ("allowed_clock_skew_ms",))
    if not 0 <= allowed_clock_skew_ms <= _ALLOWED_CLOCK_SKEW_MAX:
        raise QuotaContractError("invalid_time", ("allowed_clock_skew_ms",))
    if observation._observed_at_ms is None or observation._ttl_ms is None:
        return MappingProxyType(
            {"state": "unknown", "reason": "missing-freshness-input"}
        )
    if observation._observed_at_ms > now + allowed_clock_skew_ms:
        return MappingProxyType({"state": "unknown", "reason": "future-source-time"})
    expiry = observation._observed_at_ms + observation._ttl_ms
    if observation._reset_at_ms is not None and observation._reset_at_ms < expiry:
        expiry = observation._reset_at_ms
    if observation._window_end_ms is not None and observation._window_end_ms < expiry:
        expiry = observation._window_end_ms
    if now >= expiry:
        return MappingProxyType({"state": "stale", "reason": "expired-observation"})
    return MappingProxyType({"state": "fresh", "reason": "within-ttl"})


def event_identity(observation: QuotaObservation) -> Mapping[str, object]:
    if not _is_trusted_record(observation, QuotaObservation):
        raise QuotaContractError("invalid_type", ("observation",))
    if observation._event_identity_components is None:
        return MappingProxyType(
            {"state": "unavailable", "reason": "source-event-id-unavailable"}
        )
    namespace, epoch, event_id = observation._event_identity_components
    return MappingProxyType(
        {
            "state": "available",
            "key": ("qev:v1", observation.source_id, namespace, epoch, event_id),
        }
    )


def _format_locator(locator: tuple[str | int, ...]) -> str:
    rendered = "$"
    for part in locator:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += f".{part}"
    return rendered


def _snapshot_payload(
    payload: object, *, locator: tuple[str | int, ...], byte_limit: int
) -> tuple[dict[str, object], int]:
    snapshot = _snapshot_builtin(payload, locator=locator, depth=1, active_ids=frozenset(), counter=[0])
    if type(snapshot) is not dict:
        raise QuotaContractError("invalid_type", locator)
    json_bytes = _json_bytes(snapshot)
    if json_bytes > byte_limit:
        raise QuotaContractError("resource_limit", locator)
    return snapshot, json_bytes


def _snapshot_builtin(
    value: object,
    *,
    locator: tuple[str | int, ...],
    depth: int,
    active_ids: frozenset[int],
    counter: list[int],
) -> object:
    if depth > _MAX_DEPTH:
        raise QuotaContractError("resource_limit", locator)
    counter[0] += 1
    if counter[0] > _MAX_NODES:
        raise QuotaContractError("resource_limit", locator)
    value_type = type(value)
    if value_type is dict:
        object_id = id(value)
        if object_id in active_ids:
            raise QuotaContractError("cyclic_input", locator)
        next_ids = active_ids | {object_id}
        snapshot: dict[str, object] = {}
        for key, item in value.items():
            counter[0] += 1
            if counter[0] > _MAX_NODES:
                raise QuotaContractError("resource_limit", locator)
            if type(key) is not str:
                raise QuotaContractError("invalid_type", locator + ("<unknown>",))
            if len(key) > _MAX_STRING_CODEPOINTS:
                raise QuotaContractError("resource_limit", locator + ("<unknown>",))
            snapshot[key] = _snapshot_builtin(
                item,
                locator=locator + (key,),
                depth=depth + 1,
                active_ids=next_ids,
                counter=counter,
            )
        return snapshot
    if value_type is list:
        object_id = id(value)
        if object_id in active_ids:
            raise QuotaContractError("cyclic_input", locator)
        next_ids = active_ids | {object_id}
        return [
            _snapshot_builtin(
                item,
                locator=locator + (index,),
                depth=depth + 1,
                active_ids=next_ids,
                counter=counter,
            )
            for index, item in enumerate(value)
        ]
    if value_type is str:
        if len(value) > _MAX_STRING_CODEPOINTS:
            raise QuotaContractError("resource_limit", locator)
        return value
    if value_type is int:
        return value
    if value is None:
        return None
    raise QuotaContractError("invalid_type", locator)


def _json_bytes(payload: dict[str, object]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )


def _freeze_wire(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_wire(item) for key, item in value.items()}
        )
    if type(value) is list:
        return tuple(_freeze_wire(item) for item in value)
    return value


def _thaw_root_dict(value: object) -> dict[str, object]:
    thawed = _thaw_wire(value)
    if type(thawed) is not dict:
        raise QuotaContractError("invalid_type", ())
    return thawed


def _thaw_wire(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_wire(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_wire(item) for item in value]
    return value


def _ensure_exact_keys(
    payload: dict[str, object],
    expected: tuple[str, ...],
    locator: tuple[str | int, ...],
) -> None:
    expected_keys = set(expected)
    if set(payload) - expected_keys:
        raise QuotaContractError("invalid_shape", locator + ("<unknown>",))
    for key in expected:
        if key not in payload:
            raise QuotaContractError("invalid_shape", locator + (key,))


def _expect_dict(value: object, locator: tuple[str | int, ...]) -> dict[str, object]:
    if type(value) is not dict:
        raise QuotaContractError("invalid_type", locator)
    return value


def _expect_list(value: object, locator: tuple[str | int, ...]) -> list[object]:
    if type(value) is not list:
        raise QuotaContractError("invalid_type", locator)
    return value


def _parse_schema_version(value: object, locator: tuple[str | int, ...]) -> int:
    if type(value) is not int:
        raise QuotaContractError("invalid_type", locator)
    if value != _SCHEMA_VERSION:
        raise QuotaContractError("unsupported_schema", locator)
    return value


def _parse_identifier(value: object, locator: tuple[str | int, ...]) -> str:
    if type(value) is not str:
        raise QuotaContractError("invalid_type", locator)
    if not _IDENTIFIER_RE.fullmatch(value):
        raise QuotaContractError("invalid_identifier", locator)
    return value


def _parse_reason(value: object, locator: tuple[str | int, ...]) -> str:
    if type(value) is not str:
        raise QuotaContractError("invalid_type", locator)
    if not _REASON_RE.fullmatch(value):
        raise QuotaContractError("invalid_identifier", locator)
    return value


def _parse_ref(value: object, locator: tuple[str | int, ...]) -> str:
    if type(value) is not str:
        raise QuotaContractError("invalid_type", locator)
    namespace, separator, body = value.partition(":")
    if (
        separator != ":"
        or not _REF_NAMESPACE_RE.fullmatch(namespace)
        or not body
        or len(value.encode("utf-8")) > _MAX_STRING_CODEPOINTS
        or not _REF_BODY_RE.fullmatch(body)
        or not value.isascii()
    ):
        raise QuotaContractError("invalid_identifier", locator)
    return value


def _parse_time(value: object, locator: tuple[str | int, ...]) -> int:
    if type(value) is not int:
        raise QuotaContractError("invalid_type", locator)
    if value < 0 or value > _TIME_MAX:
        raise QuotaContractError("invalid_time", locator)
    return value


def _parse_duration(value: object, locator: tuple[str | int, ...]) -> int:
    if type(value) is not int:
        raise QuotaContractError("invalid_type", locator)
    if value < 1 or value > _DURATION_MAX:
        raise QuotaContractError("invalid_time", locator)
    return value


def _parse_quantity_kind(value: object, locator: tuple[str | int, ...]) -> str:
    identifier = _parse_identifier(value, locator)
    if identifier not in _QUANTITY_KINDS:
        raise QuotaContractError("invalid_identifier", locator)
    return identifier


def _parse_profile_ref_value(
    value: object, locator: tuple[str | int, ...]
) -> tuple[int, str]:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("schema_version", "key"), locator)
    schema_version = _parse_schema_version(payload.get("schema_version"), locator + ("schema_version",))
    key = payload.get("key")
    if type(key) is not str:
        raise QuotaContractError("invalid_type", locator + ("key",))
    if not _PROFILE_KEY_RE.fullmatch(key):
        raise QuotaContractError("invalid_identifier", locator + ("key",))
    return schema_version, key


def _parse_unit_ref_value(
    value: object, locator: tuple[str | int, ...]
) -> tuple[str, str]:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("unit_id", "version"), locator)
    return (
        _parse_identifier(payload.get("unit_id"), locator + ("unit_id",)),
        _parse_identifier(payload.get("version"), locator + ("version",)),
    )


def _parse_pool_ref_value(
    value: object, locator: tuple[str | int, ...]
) -> tuple[str, str, str, str]:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(
        payload,
        ("authority_id", "account_id", "pool_id", "revision"),
        locator,
    )
    return (
        _parse_identifier(payload.get("authority_id"), locator + ("authority_id",)),
        _parse_identifier(payload.get("account_id"), locator + ("account_id",)),
        _parse_identifier(payload.get("pool_id"), locator + ("pool_id",)),
        _parse_identifier(payload.get("revision"), locator + ("revision",)),
    )


def _parse_known_unknown(
    value: object,
    locator: tuple[str | int, ...],
    parse_known: callable,
) -> _KnownValue:
    payload = _expect_dict(value, locator)
    state = payload.get("state")
    if type(state) is not str:
        raise QuotaContractError("invalid_type", locator + ("state",))
    if state == "known":
        _ensure_exact_keys(payload, ("state", "value"), locator)
        return _KnownValue(state="known", value=parse_known(payload.get("value"), locator + ("value",)))
    if state == "unknown":
        _ensure_exact_keys(payload, ("state", "reason"), locator)
        return _KnownValue(
            state="unknown",
            reason=_parse_reason(payload.get("reason"), locator + ("reason",)),
        )
    raise QuotaContractError("invalid_shape", locator + ("state",))


def _parse_inline_unit(
    payload: dict[str, object], locator: tuple[str | int, ...]
) -> UnitDefinition:
    unit_id = _parse_identifier(payload.get("unit_id"), locator + ("unit_id",))
    version = _parse_identifier(payload.get("version"), locator + ("version",))
    quantity_kind = _parse_quantity_kind(payload.get("quantity_kind"), locator + ("quantity_kind",))
    semantics_ref = _parse_ref(payload.get("semantics_ref"), locator + ("semantics_ref",))
    return _seal_record(
        UnitDefinition(
            schema_version=_SCHEMA_VERSION,
            unit_id=unit_id,
            version=version,
            quantity_kind=quantity_kind,
            semantics_ref=semantics_ref,
            _wire=_freeze_wire(payload),
            _json_bytes=_json_bytes(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "unit_id": unit_id,
                    "version": version,
                    "quantity_kind": quantity_kind,
                    "semantics_ref": semantics_ref,
                }
            ),
        )
    )


def _parse_window_definition(
    payload: dict[str, object],
    locator: tuple[str | int, ...],
    unit_map: dict[tuple[str, str], UnitDefinition],
) -> _WindowDefinition:
    kind = payload.get("kind")
    if type(kind) is not str:
        raise QuotaContractError("invalid_type", locator + ("kind",))
    window_id = _parse_identifier(payload.get("window_id"), locator + ("window_id",))
    unit_ref = _parse_unit_ref_value(payload.get("unit_ref"), locator + ("unit_ref",))
    if unit_ref not in unit_map:
        raise QuotaContractError("unresolved_reference", locator + ("unit_ref",))
    if kind in {"fixed", "rolling"}:
        _ensure_exact_keys(payload, ("window_id", "kind", "unit_ref", "duration_ms"), locator)
        return _WindowDefinition(
            window_id=window_id,
            kind=kind,
            unit_ref=unit_ref,
            duration_ms=_parse_duration(payload.get("duration_ms"), locator + ("duration_ms",)),
        )
    if kind == "instantaneous":
        _ensure_exact_keys(payload, ("window_id", "kind", "unit_ref"), locator)
        return _WindowDefinition(window_id=window_id, kind=kind, unit_ref=unit_ref)
    if kind == "unknown":
        _ensure_exact_keys(payload, ("window_id", "kind", "unit_ref", "reason"), locator)
        return _WindowDefinition(
            window_id=window_id,
            kind=kind,
            unit_ref=unit_ref,
            reason=_parse_reason(payload.get("reason"), locator + ("reason",)),
        )
    raise QuotaContractError("invalid_identifier", locator + ("kind",))


def _parse_ref_list(value: object, locator: tuple[str | int, ...]) -> tuple[str, ...]:
    items = _expect_list(value, locator)
    if not items:
        raise QuotaContractError("invalid_shape", locator)
    if len(items) > _MAX_CONTEXT_ITEMS:
        raise QuotaContractError("resource_limit", locator)
    refs: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        ref = _parse_ref(item, locator + (index,))
        if ref in seen:
            raise QuotaContractError("duplicate_reference", locator + (index,))
        seen.add(ref)
        refs.append(ref)
    return tuple(refs)


def _descriptor_pool_map(
    descriptors: tuple[PoolDescriptor, ...]
) -> dict[tuple[str, str, str, str], list[PoolDescriptor]]:
    mapping: dict[tuple[str, str, str, str], list[PoolDescriptor]] = {}
    for descriptor in descriptors:
        mapping.setdefault(descriptor.pool_ref, []).append(descriptor)
    return mapping


def _resolve_pool_descriptor(
    descriptors: tuple[PoolDescriptor, ...],
    pool_ref: tuple[str, str, str, str],
    locator: tuple[str | int, ...],
) -> PoolDescriptor:
    matched = [descriptor for descriptor in descriptors if descriptor.pool_ref == pool_ref]
    if not matched:
        raise QuotaContractError("unresolved_reference", locator)
    if len(matched) > 1:
        raise QuotaContractError("duplicate_reference", locator)
    return matched[0]


def _descriptor_unit_map(descriptor: PoolDescriptor) -> dict[tuple[str, str], UnitDefinition]:
    return {unit.ref: unit for unit in descriptor.units}


def _resolve_window(
    descriptor: PoolDescriptor,
    window_id: str,
    locator: tuple[str | int, ...],
) -> _WindowDefinition:
    for window in descriptor.windows:
        if window.window_id == window_id:
            return window
    raise QuotaContractError("unresolved_reference", locator)


def _parse_subject(value: object, locator: tuple[str | int, ...]) -> bool:
    payload = _expect_dict(value, locator)
    kind = payload.get("kind")
    if type(kind) is not str:
        raise QuotaContractError("invalid_type", locator + ("kind",))
    if kind == "profile":
        _ensure_exact_keys(payload, ("kind", "profile_ref"), locator)
        profile_ref = _parse_known_unknown(
            payload.get("profile_ref"),
            locator + ("profile_ref",),
            _parse_profile_ref_value,
        )
        return profile_ref.state == "known"
    if kind == "group":
        _ensure_exact_keys(payload, ("kind", "group_ref", "revision", "members"), locator)
        _parse_ref(payload.get("group_ref"), locator + ("group_ref",))
        _parse_identifier(payload.get("revision"), locator + ("revision",))
        members = _parse_known_unknown(
            payload.get("members"),
            locator + ("members",),
            _parse_profile_ref_list,
        )
        return members.state == "known"
    raise QuotaContractError("invalid_identifier", locator + ("kind",))


def _parse_profile_ref_list(
    value: object, locator: tuple[str | int, ...]
) -> tuple[tuple[int, str], ...]:
    items = _expect_list(value, locator)
    if not items:
        raise QuotaContractError("invalid_shape", locator)
    if len(items) > 64:
        raise QuotaContractError("resource_limit", locator)
    parsed: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for index, item in enumerate(items):
        value_item = _parse_profile_ref_value(item, locator + (index,))
        if value_item in seen:
            raise QuotaContractError("duplicate_reference", locator + (index,))
        seen.add(value_item)
        parsed.append(value_item)
    return tuple(parsed)


def _parse_binding_constraint(
    value: object,
    locator: tuple[str | int, ...],
    descriptor_map: dict[tuple[str, str, str, str], list[PoolDescriptor]],
) -> tuple[tuple[str, str, str, str], str, _WindowDefinition]:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("pool_ref", "window_id"), locator)
    pool_ref = _parse_pool_ref_value(payload.get("pool_ref"), locator + ("pool_ref",))
    descriptors = descriptor_map.get(pool_ref)
    if descriptors is None:
        raise QuotaContractError("unresolved_reference", locator + ("pool_ref",))
    if len(descriptors) > 1:
        raise QuotaContractError("duplicate_reference", locator + ("pool_ref",))
    window_id = _parse_identifier(payload.get("window_id"), locator + ("window_id",))
    window = _resolve_window(descriptors[0], window_id, locator + ("window_id",))
    return pool_ref, window_id, window


def _parse_coverage(value: object, locator: tuple[str | int, ...]) -> str:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("state", "gaps"), locator)
    state = payload.get("state")
    if type(state) is not str:
        raise QuotaContractError("invalid_type", locator + ("state",))
    if state not in {"complete", "partial", "unknown"}:
        raise QuotaContractError("invalid_identifier", locator + ("state",))
    gaps = _expect_list(payload.get("gaps"), locator + ("gaps",))
    if len(gaps) > 32:
        raise QuotaContractError("resource_limit", locator + ("gaps",))
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(gaps):
        gap = _expect_dict(item, locator + ("gaps", index))
        _ensure_exact_keys(gap, ("scope", "reason"), locator + ("gaps", index))
        scope = _parse_identifier(gap.get("scope"), locator + ("gaps", index, "scope"))
        reason = _parse_reason(gap.get("reason"), locator + ("gaps", index, "reason"))
        key = (scope, reason)
        if key in seen:
            raise QuotaContractError("duplicate_reference", locator + ("gaps", index))
        seen.add(key)
    if state == "complete" and gaps:
        raise QuotaContractError("invalid_shape", locator + ("gaps",))
    if state == "partial" and not gaps:
        raise QuotaContractError("invalid_shape", locator + ("gaps",))
    return state


def _validate_descriptors(value: object) -> tuple[PoolDescriptor, ...]:
    if type(value) is not tuple:
        raise QuotaContractError("invalid_type", ("descriptors",))
    if len(value) > _MAX_CONTEXT_ITEMS:
        raise QuotaContractError("resource_limit", ("descriptors",))
    for index, item in enumerate(value):
        if not _is_trusted_record(item, PoolDescriptor):
            raise QuotaContractError("invalid_type", ("descriptors", index))
    return value


def _validate_unit_catalog(value: object) -> tuple[UnitDefinition, ...]:
    if type(value) is not tuple:
        raise QuotaContractError("invalid_type", ("unit_catalog",))
    if len(value) > _MAX_CONTEXT_ITEMS:
        raise QuotaContractError("resource_limit", ("unit_catalog",))
    for index, item in enumerate(value):
        if not _is_trusted_record(item, UnitDefinition):
            raise QuotaContractError("invalid_type", ("unit_catalog", index))
    return value


def _ensure_total_bytes(
    observed: int, limit: int, locator: tuple[str | int, ...]
) -> None:
    if observed > limit:
        raise QuotaContractError("resource_limit", locator)


def _build_unit_catalog(
    unit_catalog: tuple[UnitDefinition, ...],
) -> dict[tuple[str, str], UnitDefinition]:
    mapping: dict[tuple[str, str], UnitDefinition] = {}
    for index, unit in enumerate(unit_catalog):
        existing = mapping.get(unit.ref)
        if existing is None:
            mapping[unit.ref] = unit
            continue
        if existing.definition_key != unit.definition_key:
            raise QuotaContractError("unit_conflict", ("unit_catalog", index))
        raise QuotaContractError("duplicate_reference", ("unit_catalog", index))
    return mapping


def _build_unit_union(
    descriptors: tuple[PoolDescriptor, ...],
    unit_catalog: tuple[UnitDefinition, ...],
) -> dict[tuple[str, str], UnitDefinition]:
    unit_union: dict[tuple[str, str], UnitDefinition] = {}
    seen_catalog_refs: set[tuple[str, str]] = set()
    for index, unit in enumerate(unit_catalog):
        if unit.ref in seen_catalog_refs:
            raise QuotaContractError("duplicate_reference", ("unit_catalog", index))
        seen_catalog_refs.add(unit.ref)
        unit_union[unit.ref] = unit
    for descriptor_index, descriptor in enumerate(descriptors):
        for unit_index, unit in enumerate(descriptor.units):
            existing = unit_union.get(unit.ref)
            if existing is None:
                unit_union[unit.ref] = unit
                continue
            if existing.definition_key != unit.definition_key:
                raise QuotaContractError(
                    "unit_conflict", ("descriptors", descriptor_index, "units", unit_index)
                )
    return unit_union


def _parse_scope_value(
    value: object, locator: tuple[str | int, ...]
) -> tuple[tuple[str, str, str, str], str]:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("pool_ref", "window_id"), locator)
    return (
        _parse_pool_ref_value(payload.get("pool_ref"), locator + ("pool_ref",)),
        _parse_identifier(payload.get("window_id"), locator + ("window_id",)),
    )


def _parse_window_instance(
    value: object, locator: tuple[str | int, ...]
) -> _WindowInstanceInfo:
    payload = _expect_dict(value, locator)
    kind = payload.get("kind")
    if type(kind) is not str:
        raise QuotaContractError("invalid_type", locator + ("kind",))
    if kind == "interval":
        _ensure_exact_keys(payload, ("kind", "start_ms", "end_ms", "epoch"), locator)
        start_ms = _parse_time(payload.get("start_ms"), locator + ("start_ms",))
        end_ms = _parse_time(payload.get("end_ms"), locator + ("end_ms",))
        if start_ms >= end_ms:
            raise QuotaContractError("invalid_time", locator)
        _parse_known_unknown(
            payload.get("epoch"),
            locator + ("epoch",),
            lambda epoch_value, epoch_locator: _parse_identifier(epoch_value, epoch_locator),
        )
        return _WindowInstanceInfo(kind="interval", start_ms=start_ms, end_ms=end_ms)
    if kind == "instant":
        _ensure_exact_keys(payload, ("kind", "at_ms"), locator)
        _parse_time(payload.get("at_ms"), locator + ("at_ms",))
        return _WindowInstanceInfo(kind="instant")
    if kind == "unknown":
        _ensure_exact_keys(payload, ("kind", "reason"), locator)
        _parse_reason(payload.get("reason"), locator + ("reason",))
        return _WindowInstanceInfo(kind="unknown")
    raise QuotaContractError("invalid_identifier", locator + ("kind",))


def _parse_measurement(
    value: object, locator: tuple[str | int, ...]
) -> _MeasurementInfo:
    payload = _expect_dict(value, locator)
    kind = payload.get("kind")
    if type(kind) is not str:
        raise QuotaContractError("invalid_type", locator + ("kind",))
    if kind in {"remaining_snapshot", "usage_delta", "gauge_snapshot"}:
        _ensure_exact_keys(payload, ("kind", "metric_id", "quantity"), locator)
        _parse_identifier(payload.get("metric_id"), locator + ("metric_id",))
        quantity = _parse_quantity(payload.get("quantity"), locator + ("quantity",))
        return _MeasurementInfo(kind=kind, quantity_state=quantity)
    if kind == "usage_total":
        _ensure_exact_keys(payload, ("kind", "metric_id", "quantity", "counter"), locator)
        _parse_identifier(payload.get("metric_id"), locator + ("metric_id",))
        quantity = _parse_quantity(payload.get("quantity"), locator + ("quantity",))
        _parse_counter(payload.get("counter"), locator + ("counter",))
        return _MeasurementInfo(kind=kind, quantity_state=quantity)
    if kind == "limit_signal":
        _ensure_exact_keys(payload, ("kind", "metric_id", "signal"), locator)
        _parse_identifier(payload.get("metric_id"), locator + ("metric_id",))
        _parse_identifier(payload.get("signal"), locator + ("signal",))
        return _MeasurementInfo(kind=kind)
    raise QuotaContractError("invalid_identifier", locator + ("kind",))


def _parse_quantity(value: object, locator: tuple[str | int, ...]) -> str:
    payload = _expect_dict(value, locator)
    state = payload.get("state")
    if type(state) is not str:
        raise QuotaContractError("invalid_type", locator + ("state",))
    if state == "observed":
        _ensure_exact_keys(payload, ("state", "amount"), locator)
        _parse_amount(payload.get("amount"), locator + ("amount",))
        return state
    if state == "estimated":
        _ensure_exact_keys(payload, ("state", "amount", "method_ref"), locator)
        _parse_amount(payload.get("amount"), locator + ("amount",))
        _parse_ref(payload.get("method_ref"), locator + ("method_ref",))
        return state
    if state == "unknown":
        _ensure_exact_keys(payload, ("state", "reason"), locator)
        _parse_reason(payload.get("reason"), locator + ("reason",))
        return state
    raise QuotaContractError("invalid_shape", locator + ("state",))


def _parse_amount(value: object, locator: tuple[str | int, ...]) -> None:
    payload = _expect_dict(value, locator)
    kind = payload.get("kind")
    if type(kind) is not str:
        raise QuotaContractError("invalid_type", locator + ("kind",))
    if kind == "exact":
        _ensure_exact_keys(payload, ("kind", "value"), locator)
        _parse_decimal_wire(payload.get("value"), locator + ("value",))
        return
    if kind == "bounds":
        _ensure_exact_keys(payload, ("kind", "lower", "upper"), locator)
        lower = payload.get("lower")
        upper = payload.get("upper")
        if lower is None and upper is None:
            raise QuotaContractError("invalid_bounds", locator)
        lower_parts = _decimal_parts(lower, locator + ("lower",)) if lower is not None else None
        upper_parts = _decimal_parts(upper, locator + ("upper",)) if upper is not None else None
        if lower_parts is not None and upper_parts is not None and lower_parts > upper_parts:
            raise QuotaContractError("invalid_bounds", locator)
        return
    raise QuotaContractError("invalid_identifier", locator + ("kind",))


def _parse_decimal_wire(value: object, locator: tuple[str | int, ...]) -> str:
    if type(value) is not str:
        raise QuotaContractError("invalid_type", locator)
    if not _DECIMAL_WIRE_RE.fullmatch(value):
        raise QuotaContractError("invalid_decimal", locator)
    return value


def _decimal_parts(
    value: object, locator: tuple[str | int, ...]
) -> tuple[int, int]:
    decimal_text = _parse_decimal_wire(value, locator)
    if "." not in decimal_text:
        return int(decimal_text), 0
    whole, fraction = decimal_text.split(".", 1)
    return int(whole + fraction), len(fraction)


def _parse_counter(value: object, locator: tuple[str | int, ...]) -> None:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("scope_id", "epoch"), locator)
    _parse_identifier(payload.get("scope_id"), locator + ("scope_id",))
    _parse_known_unknown(
        payload.get("epoch"),
        locator + ("epoch",),
        lambda epoch_value, epoch_locator: _parse_identifier(epoch_value, epoch_locator),
    )


def _parse_source(
    value: object, locator: tuple[str | int, ...]
) -> tuple[str, str, tuple[str, str, str] | None]:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(
        payload,
        (
            "source_id",
            "source_schema",
            "adapter_version",
            "authority_ref",
            "method",
            "provenance_refs",
            "event_identity",
        ),
        locator,
    )
    source_id = _parse_identifier(payload.get("source_id"), locator + ("source_id",))
    _parse_identifier(payload.get("source_schema"), locator + ("source_schema",))
    _parse_identifier(payload.get("adapter_version"), locator + ("adapter_version",))
    _parse_ref(payload.get("authority_ref"), locator + ("authority_ref",))
    method = _parse_identifier(payload.get("method"), locator + ("method",))
    if method not in _SOURCE_METHODS:
        raise QuotaContractError("invalid_identifier", locator + ("method",))
    _parse_ref_list(payload.get("provenance_refs"), locator + ("provenance_refs",))
    event_payload = _expect_dict(payload.get("event_identity"), locator + ("event_identity",))
    state = event_payload.get("state")
    if type(state) is not str:
        raise QuotaContractError("invalid_type", locator + ("event_identity", "state"))
    if state == "known":
        _ensure_exact_keys(
            event_payload,
            ("state", "namespace", "epoch", "event_id"),
            locator + ("event_identity",),
        )
        return (
            source_id,
            method,
            (
                _parse_identifier(
                    event_payload.get("namespace"),
                    locator + ("event_identity", "namespace"),
                ),
                _parse_identifier(
                    event_payload.get("epoch"),
                    locator + ("event_identity", "epoch"),
                ),
                _parse_identifier(
                    event_payload.get("event_id"),
                    locator + ("event_identity", "event_id"),
                ),
            ),
        )
    if state == "unknown":
        _ensure_exact_keys(event_payload, ("state", "reason"), locator + ("event_identity",))
        _parse_reason(event_payload.get("reason"), locator + ("event_identity", "reason"))
        return source_id, method, None
    raise QuotaContractError("invalid_shape", locator + ("event_identity", "state"))


def _validate_window_instance_against_scope(
    instance: _WindowInstanceInfo,
    window: _WindowDefinition,
    locator: tuple[str | int, ...],
) -> None:
    if window.kind in {"fixed", "rolling"}:
        if instance.kind == "instant":
            raise QuotaContractError("incompatible_semantics", locator)
        if instance.kind == "interval" and window.duration_ms is not None:
            if instance.end_ms is None or instance.start_ms is None:
                raise QuotaContractError("invalid_time", locator)
            if instance.end_ms - instance.start_ms != window.duration_ms:
                raise QuotaContractError("incompatible_semantics", locator)
        return
    if window.kind == "instantaneous" and instance.kind == "interval":
        raise QuotaContractError("incompatible_semantics", locator)
    if window.kind == "unknown" and instance.kind != "unknown":
        raise QuotaContractError("incompatible_semantics", locator)
