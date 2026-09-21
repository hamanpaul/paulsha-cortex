"""Immutable quota observation wire-contract records.

This module is intentionally stdlib-only and side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import re
from types import MappingProxyType
from typing import ClassVar, TypeVar

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
_MAX_BINDING_CONSTRAINTS = 64
_MAX_BINDING_CONTEXT_BYTES = 17 * _MAX_ROOT_BYTES
_MAX_OBSERVATION_CONTEXT_BYTES = _MAX_BINDING_CONTEXT_BYTES + (
    _MAX_CONTEXT_ITEMS * _MAX_UNIT_DEFINITION_BYTES
)
_TIME_MAX = 253402300799999
_DURATION_MAX = 31622400000
_MAX_CLOCK_SKEW_MS = 300000
_MAX_GROUP_MEMBERS = 64
_MAX_COVERAGE_GAPS = 32
_TRUST_MARKER = object()
_MISSING_WIRE = object()

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
_WINDOW_DURATION_KEYS = ("window_id", "kind", "unit_ref", "duration_ms")
_WINDOW_MIN_KEYS = ("window_id", "kind", "unit_ref")
_WINDOW_UNKNOWN_KEYS = ("window_id", "kind", "unit_ref", "reason")
_WINDOW_DURATION_KINDS = frozenset(("fixed", "rolling"))
_WINDOW_MIN_KINDS = frozenset(("instantaneous",))
_WINDOW_INSTANCE_INTERVAL_KEYS = ("kind", "start_ms", "end_ms", "epoch")
_WINDOW_INSTANCE_INSTANT_KEYS = ("kind", "at_ms")
_WINDOW_INSTANCE_UNKNOWN_KEYS = ("kind", "reason")
_MEASUREMENT_QUANTITY_KINDS = frozenset(
    ("remaining_snapshot", "usage_delta", "usage_total", "gauge_snapshot")
)
_MEASUREMENT_SIGNAL_KINDS = frozenset(("limit_signal",))
_SOURCE_METHOD_KINDS = frozenset(
    ("provider_status", "structured_event", "executor_usage", "estimate", "legacy")
)
_LIMIT_SIGNAL_SOURCE_METHODS = frozenset(("provider_status", "structured_event"))
_COVERAGE_STATES = frozenset(("complete", "partial", "unknown"))

_RecordT = TypeVar("_RecordT")


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


class _ParserSealedRecord:
    _parser_entrypoint: ClassVar[str]

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args
        del kwargs
        raise TypeError(
            f"{type(self).__name__} is parser-sealed; use {type(self)._parser_entrypoint}()"
        )

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if type(self) is not type(other):
            return False
        self_wire = getattr(self, "_wire", _MISSING_WIRE)
        other_wire = getattr(other, "_wire", _MISSING_WIRE)
        if self_wire is _MISSING_WIRE or other_wire is _MISSING_WIRE:
            return False
        return self_wire == other_wire

    def __hash__(self) -> int:
        wire = getattr(self, "_wire", _MISSING_WIRE)
        if wire is _MISSING_WIRE:
            return object.__hash__(self)
        return hash(_wire_hash_key(wire))


@dataclass(frozen=True, init=False)
class UnitDefinition(_ParserSealedRecord):
    _parser_entrypoint: ClassVar[str] = "parse_unit_definition"
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

    @classmethod
    def _from_parser(
        cls,
        *,
        schema_version: int,
        unit_id: str,
        version: str,
        quantity_kind: str,
        semantics_ref: str,
        wire: object,
        json_bytes: int,
    ) -> "UnitDefinition":
        return _build_record(
            cls,
            schema_version=schema_version,
            unit_id=unit_id,
            version=version,
            quantity_kind=quantity_kind,
            semantics_ref=semantics_ref,
            _wire=wire,
            _json_bytes=json_bytes,
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


@dataclass(frozen=True, init=False, eq=False)
class PoolDescriptor(_ParserSealedRecord):
    _parser_entrypoint: ClassVar[str] = "parse_pool_descriptor"
    schema_version: int
    authority_id: str
    account_id: str
    pool_id: str
    revision: str
    units: tuple[UnitDefinition, ...]
    windows: tuple[object, ...]
    _wire: object = field(repr=False, compare=False)
    _json_bytes: int = field(repr=False, compare=False)
    _trusted: object | None = field(
        default=None,
        repr=False,
        compare=False,
        init=False,
    )

    @classmethod
    def _from_parser(
        cls,
        *,
        schema_version: int,
        authority_id: str,
        account_id: str,
        pool_id: str,
        revision: str,
        units: tuple[UnitDefinition, ...],
        windows: tuple[object, ...],
        wire: object,
        json_bytes: int,
    ) -> "PoolDescriptor":
        return _build_record(
            cls,
            schema_version=schema_version,
            authority_id=authority_id,
            account_id=account_id,
            pool_id=pool_id,
            revision=revision,
            units=units,
            windows=windows,
            _wire=wire,
            _json_bytes=json_bytes,
        )

    @property
    def pool_ref(self) -> tuple[str, str, str, str]:
        return (self.authority_id, self.account_id, self.pool_id, self.revision)

    def to_dict(self) -> dict[str, object]:
        return _thaw_root_dict(self._wire)


@dataclass(frozen=True, init=False, eq=False)
class ProfilePoolBinding(_ParserSealedRecord):
    _parser_entrypoint: ClassVar[str] = "parse_binding"
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

    @classmethod
    def _from_parser(
        cls,
        *,
        schema_version: int,
        binding_id: str,
        revision: str,
        status: Mapping[str, object],
        wire: object,
        json_bytes: int,
    ) -> "ProfilePoolBinding":
        return _build_record(
            cls,
            schema_version=schema_version,
            binding_id=binding_id,
            revision=revision,
            _status=status,
            _wire=wire,
            _json_bytes=json_bytes,
        )

    def to_dict(self) -> dict[str, object]:
        return _thaw_root_dict(self._wire)


@dataclass(frozen=True, init=False, eq=False)
class QuotaObservation(_ParserSealedRecord):
    _parser_entrypoint: ClassVar[str] = "parse_observation"
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

    @classmethod
    def _from_parser(
        cls,
        *,
        schema_version: int,
        observation_id: str,
        source_id: str,
        event_identity_components: tuple[str, str, str] | None,
        observed_at_ms: int | None,
        ttl_ms: int | None,
        reset_at_ms: int | None,
        window_end_ms: int | None,
        wire: object,
        json_bytes: int,
    ) -> "QuotaObservation":
        return _build_record(
            cls,
            schema_version=schema_version,
            observation_id=observation_id,
            source_id=source_id,
            _event_identity_components=event_identity_components,
            _observed_at_ms=observed_at_ms,
            _ttl_ms=ttl_ms,
            _reset_at_ms=reset_at_ms,
            _window_end_ms=window_end_ms,
            _wire=wire,
            _json_bytes=json_bytes,
        )

    def to_dict(self) -> dict[str, object]:
        return _thaw_root_dict(self._wire)


@dataclass(frozen=True)
class _KnownValue:
    state: str
    value: object | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _QuantityInfo:
    kind: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class _ResolvedWindow:
    kind: str
    unit_ref: tuple[str, str]
    quantity_kind: str
    duration_ms: int | None = None


@dataclass(frozen=True)
class _WindowInstanceInfo:
    kind: str
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True)
class _SourceInfo:
    source_id: str
    method: str
    event_identity_components: tuple[str, str, str] | None = None


def _build_record(cls: type[_RecordT], /, **attributes: object) -> _RecordT:
    record = object.__new__(cls)
    for name, value in attributes.items():
        object.__setattr__(record, name, value)
    object.__setattr__(record, "_trusted", None)
    return record


def _helper_result(**payload: object) -> Mapping[str, object]:
    return MappingProxyType(dict(payload))


def _seal_record(record: object) -> object:
    object.__setattr__(record, "_trusted", _TRUST_MARKER)
    return record


def _is_trusted_record(value: object, expected_type: type[object]) -> bool:
    return type(value) is expected_type and getattr(value, "_trusted", None) is _TRUST_MARKER


def parse_unit_definition(payload: dict) -> UnitDefinition:
    snapshot, json_bytes = _snapshot_payload(
        payload,
        locator=(),
        byte_limit=_MAX_UNIT_DEFINITION_BYTES,
    )
    _ensure_exact_keys(snapshot, _UNIT_ROOT_KEYS, ())
    schema_version = _parse_schema_version(
        snapshot.get("schema_version"), ("schema_version",)
    )
    unit_id = _parse_identifier(snapshot.get("unit_id"), ("unit_id",))
    version = _parse_identifier(snapshot.get("version"), ("version",))
    quantity_kind = _parse_quantity_kind(
        snapshot.get("quantity_kind"), ("quantity_kind",)
    )
    semantics_ref = _parse_ref(snapshot.get("semantics_ref"), ("semantics_ref",))
    canonical_snapshot = _canonical_unit_snapshot(
        schema_version=schema_version,
        unit_id=unit_id,
        version=version,
        quantity_kind=quantity_kind,
        semantics_ref=semantics_ref,
    )
    return _seal_record(
        UnitDefinition._from_parser(
            schema_version=schema_version,
            unit_id=unit_id,
            version=version,
            quantity_kind=quantity_kind,
            semantics_ref=semantics_ref,
            wire=_freeze_wire(canonical_snapshot),
            json_bytes=json_bytes,
        )
    )


def parse_pool_descriptor(payload: dict) -> PoolDescriptor:
    snapshot, json_bytes = _snapshot_payload(
        payload,
        locator=(),
        byte_limit=_MAX_ROOT_BYTES,
    )
    _ensure_exact_keys(snapshot, _POOL_DESCRIPTOR_KEYS, ())
    schema_version = _parse_schema_version(
        snapshot.get("schema_version"), ("schema_version",)
    )
    authority_id = _parse_identifier(snapshot.get("authority_id"), ("authority_id",))
    account_id = _parse_identifier(snapshot.get("account_id"), ("account_id",))
    pool_id = _parse_identifier(snapshot.get("pool_id"), ("pool_id",))
    revision = _parse_identifier(snapshot.get("revision"), ("revision",))
    _parse_ref(snapshot.get("authority_ref"), ("authority_ref",))
    _parse_ref_list(snapshot.get("provenance_refs"), ("provenance_refs",))
    units = _parse_descriptor_units(snapshot.get("units"), ("units",))
    windows = _parse_descriptor_windows(snapshot.get("windows"), ("windows",), units)
    return _seal_record(
        PoolDescriptor._from_parser(
            schema_version=schema_version,
            authority_id=authority_id,
            account_id=account_id,
            pool_id=pool_id,
            revision=revision,
            units=units,
            windows=windows,
            wire=_freeze_wire(snapshot),
            json_bytes=json_bytes,
        )
    )


def parse_binding(
    payload: dict, *, descriptors: tuple[PoolDescriptor, ...]
) -> ProfilePoolBinding:
    descriptors = _validate_descriptors(descriptors)
    snapshot, json_bytes = _snapshot_payload(
        payload,
        locator=(),
        byte_limit=_MAX_ROOT_BYTES,
    )
    _ensure_total_bytes(
        json_bytes + sum(descriptor._json_bytes for descriptor in descriptors),
        _MAX_BINDING_CONTEXT_BYTES,
        ("descriptors",),
    )
    _ensure_exact_keys(snapshot, _BINDING_KEYS, ())
    schema_version = _parse_schema_version(
        snapshot.get("schema_version"), ("schema_version",)
    )
    binding_id = _parse_identifier(snapshot.get("binding_id"), ("binding_id",))
    revision = _parse_identifier(snapshot.get("revision"), ("revision",))
    subject_complete = _parse_binding_subject(snapshot.get("subject"), ("subject",))
    constraints = _parse_binding_constraints(snapshot.get("constraints"), ("constraints",))
    coverage_state = _parse_coverage(snapshot.get("coverage"), ("coverage",))
    has_unknown_window = _resolve_known_binding_constraints(constraints, descriptors)
    reasons: set[str] = set()
    if not subject_complete:
        reasons.add("subject-unknown")
    if any(constraint.state != "known" for constraint in constraints):
        reasons.add("constraint-unknown")
    if has_unknown_window:
        reasons.add("window-unknown")
    if coverage_state != "complete":
        reasons.add("coverage-incomplete")
    status = _helper_result(
        state="complete" if not reasons else "incomplete",
        reasons=tuple(sorted(reasons)),
    )
    return _seal_record(
        ProfilePoolBinding._from_parser(
            schema_version=schema_version,
            binding_id=binding_id,
            revision=revision,
            status=status,
            wire=_freeze_wire(snapshot),
            json_bytes=json_bytes,
        )
    )


def parse_observation(
    payload: dict,
    *,
    descriptors: tuple[PoolDescriptor, ...],
    unit_catalog: tuple[UnitDefinition, ...],
) -> QuotaObservation:
    descriptors = _validate_descriptors(descriptors)
    _validate_observation_descriptors(descriptors)
    unit_catalog = _validate_unit_catalog(unit_catalog)
    snapshot, json_bytes = _snapshot_payload(
        payload,
        locator=(),
        byte_limit=_MAX_ROOT_BYTES,
    )
    _ensure_total_bytes(
        json_bytes
        + sum(descriptor._json_bytes for descriptor in descriptors)
        + sum(unit._json_bytes for unit in unit_catalog),
        _MAX_OBSERVATION_CONTEXT_BYTES,
        ("unit_catalog",),
    )
    _ensure_exact_keys(snapshot, _OBSERVATION_KEYS, ())
    schema_version = _parse_schema_version(
        snapshot.get("schema_version"), ("schema_version",)
    )
    observation_id = _parse_identifier(
        snapshot.get("observation_id"), ("observation_id",)
    )
    scope = _parse_known_unknown(snapshot.get("scope"), ("scope",), _parse_scope_value)
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
    window_instance = _parse_window_instance(
        snapshot.get("window_instance"), ("window_instance",)
    )
    measurement = _parse_measurement(snapshot.get("measurement"), ("measurement",))
    observed_at_ms = _parse_known_unknown(
        snapshot.get("observed_at_ms"),
        ("observed_at_ms",),
        lambda value, locator: _parse_time(value, locator),
    )
    if observed_at_ms.state == "known" and type(observed_at_ms.value) is not int:
        raise QuotaContractError("invalid_type", ("observed_at_ms", "value"))
    _parse_time(snapshot.get("received_at_ms"), ("received_at_ms",))
    ttl_ms = _parse_known_unknown(
        snapshot.get("ttl_ms"),
        ("ttl_ms",),
        lambda value, locator: _parse_duration(value, locator),
    )
    if ttl_ms.state == "known" and type(ttl_ms.value) is not int:
        raise QuotaContractError("invalid_type", ("ttl_ms", "value"))
    reset_at_ms = _parse_known_unknown(
        snapshot.get("reset_at_ms"),
        ("reset_at_ms",),
        lambda value, locator: _parse_time(value, locator),
    )
    if reset_at_ms.state == "known" and type(reset_at_ms.value) is not int:
        raise QuotaContractError("invalid_type", ("reset_at_ms", "value"))
    source = _parse_source(snapshot.get("source"), ("source",))
    _parse_coverage(snapshot.get("coverage"), ("coverage",))

    if (
        observed_at_ms.state == "known"
        and ttl_ms.state == "known"
        and observed_at_ms.value + ttl_ms.value > _TIME_MAX
    ):
        raise QuotaContractError("invalid_time", ("ttl_ms", "value"))

    unit_map = _build_unit_catalog(descriptors, unit_catalog)
    resolved_scope = None
    if scope.state == "known":
        resolved_scope = _resolve_pool_window_constraint(
            scope.value,
            descriptors,
            ("scope",),
        )
    resolved_unit = None
    if unit_ref.state == "known":
        resolved_unit = unit_map.get(unit_ref.value)
        if resolved_unit is None:
            raise QuotaContractError("unresolved_reference", ("unit_ref",))

    _validate_window_instance_against_scope(window_instance, resolved_scope)
    _validate_measurement_semantics(
        measurement,
        source=source,
        unit_ref=unit_ref,
        resolved_unit=resolved_unit,
        resolved_scope=resolved_scope,
    )

    window_end_ms = (
        window_instance.end_ms if window_instance.kind == "interval" else None
    )
    event_identity_components = source.event_identity_components

    return _seal_record(
        QuotaObservation._from_parser(
            schema_version=schema_version,
            observation_id=observation_id,
            source_id=source.source_id,
            event_identity_components=event_identity_components,
            observed_at_ms=observed_at_ms.value
            if observed_at_ms.state == "known"
            else None,
            ttl_ms=ttl_ms.value if ttl_ms.state == "known" else None,
            reset_at_ms=reset_at_ms.value if reset_at_ms.state == "known" else None,
            window_end_ms=window_end_ms,
            wire=_freeze_wire(snapshot),
            json_bytes=json_bytes,
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
    current_time = _parse_time(now_utc_ms, ("now_utc_ms",))
    if type(allowed_clock_skew_ms) is not int:
        raise QuotaContractError("invalid_type", ("allowed_clock_skew_ms",))
    if allowed_clock_skew_ms < 0 or allowed_clock_skew_ms > _MAX_CLOCK_SKEW_MS:
        raise QuotaContractError("invalid_time", ("allowed_clock_skew_ms",))
    if observation._observed_at_ms is None or observation._ttl_ms is None:
        return _helper_result(state="unknown", reason="missing-freshness-input")
    if observation._observed_at_ms > current_time + allowed_clock_skew_ms:
        return _helper_result(state="unknown", reason="future-source-time")
    expiry = observation._observed_at_ms + observation._ttl_ms
    if observation._reset_at_ms is not None and observation._reset_at_ms < expiry:
        expiry = observation._reset_at_ms
    if observation._window_end_ms is not None and observation._window_end_ms < expiry:
        expiry = observation._window_end_ms
    if current_time >= expiry:
        return _helper_result(state="stale", reason="expired-observation")
    return _helper_result(state="fresh", reason="within-ttl")


def event_identity(observation: QuotaObservation) -> Mapping[str, object]:
    if not _is_trusted_record(observation, QuotaObservation):
        raise QuotaContractError("invalid_type", ("observation",))
    if observation._event_identity_components is None:
        return _helper_result(
            state="unavailable",
            reason="source-event-id-unavailable",
        )
    return _helper_result(
        state="available",
        key=("qev:v1", observation.source_id, *observation._event_identity_components),
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
    snapshot = _snapshot_builtin(
        payload,
        locator=locator,
        depth=1,
        active_ids=frozenset(),
        counter=[0],
    )
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
        return MappingProxyType({key: _freeze_wire(item) for key, item in value.items()})
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


def _wire_hash_key(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(
            (str(key), _wire_hash_key(item)) for key, item in sorted(value.items())
        )
    if type(value) is tuple:
        return tuple(_wire_hash_key(item) for item in value)
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
    return _parse_enum_identifier(value, locator, {"amount", "gauge"})


def _parse_enum_identifier(
    value: object,
    locator: tuple[str | int, ...],
    allowed: set[str] | frozenset[str],
) -> str:
    parsed = _parse_identifier(value, locator)
    if parsed not in allowed:
        raise QuotaContractError("invalid_identifier", locator)
    return parsed


def _parse_profile_ref_value(
    value: object, locator: tuple[str | int, ...]
) -> tuple[int, str]:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("schema_version", "key"), locator)
    schema_version = _parse_schema_version(
        payload.get("schema_version"),
        locator + ("schema_version",),
    )
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
    parse_known,
) -> _KnownValue:
    payload = _expect_dict(value, locator)
    state = payload.get("state")
    if type(state) is not str:
        raise QuotaContractError("invalid_type", locator + ("state",))
    if state == "known":
        _ensure_exact_keys(payload, ("state", "value"), locator)
        return _KnownValue(
            state="known",
            value=parse_known(payload.get("value"), locator + ("value",)),
        )
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
    quantity_kind = _parse_quantity_kind(
        payload.get("quantity_kind"),
        locator + ("quantity_kind",),
    )
    semantics_ref = _parse_ref(payload.get("semantics_ref"), locator + ("semantics_ref",))
    canonical_snapshot = _canonical_unit_snapshot(
        schema_version=_SCHEMA_VERSION,
        unit_id=unit_id,
        version=version,
        quantity_kind=quantity_kind,
        semantics_ref=semantics_ref,
    )
    return _seal_record(
        UnitDefinition._from_parser(
            schema_version=_SCHEMA_VERSION,
            unit_id=unit_id,
            version=version,
            quantity_kind=quantity_kind,
            semantics_ref=semantics_ref,
            wire=_freeze_wire(canonical_snapshot),
            json_bytes=_json_bytes(canonical_snapshot),
        )
    )


def _parse_descriptor_units(
    value: object, locator: tuple[str | int, ...]
) -> tuple[UnitDefinition, ...]:
    items = _expect_list(value, locator)
    if not items:
        raise QuotaContractError("invalid_shape", locator)
    if len(items) > _MAX_CONTEXT_ITEMS:
        raise QuotaContractError("resource_limit", locator)
    units: list[UnitDefinition] = []
    seen_refs: set[tuple[str, str]] = set()
    for index, item in enumerate(items):
        item_locator = locator + (index,)
        payload = _expect_dict(item, item_locator)
        _ensure_exact_keys(payload, _UNIT_INLINE_KEYS, item_locator)
        unit = _parse_inline_unit(payload, item_locator)
        if unit.ref in seen_refs:
            raise QuotaContractError("duplicate_reference", item_locator)
        seen_refs.add(unit.ref)
        units.append(unit)
    return tuple(units)


def _canonical_unit_snapshot(
    *,
    schema_version: int,
    unit_id: str,
    version: str,
    quantity_kind: str,
    semantics_ref: str,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "unit_id": unit_id,
        "version": version,
        "quantity_kind": quantity_kind,
        "semantics_ref": semantics_ref,
    }


def _parse_descriptor_windows(
    value: object,
    locator: tuple[str | int, ...],
    units: tuple[UnitDefinition, ...],
) -> tuple[object, ...]:
    items = _expect_list(value, locator)
    if not items:
        raise QuotaContractError("invalid_shape", locator)
    if len(items) > _MAX_CONTEXT_ITEMS:
        raise QuotaContractError("resource_limit", locator)
    unit_defs = {unit.ref: unit for unit in units}
    windows: list[object] = []
    seen_window_ids: set[str] = set()
    for index, item in enumerate(items):
        item_locator = locator + (index,)
        payload = _expect_dict(item, item_locator)
        window = _parse_descriptor_window(payload, item_locator, unit_defs)
        window_id = _parse_identifier(
            payload.get("window_id"),
            item_locator + ("window_id",),
        )
        if window_id in seen_window_ids:
            raise QuotaContractError("duplicate_reference", item_locator)
        seen_window_ids.add(window_id)
        windows.append(window)
    return tuple(windows)


def _parse_descriptor_window(
    payload: dict[str, object],
    locator: tuple[str | int, ...],
    unit_defs: dict[tuple[str, str], UnitDefinition],
) -> object:
    if "kind" not in payload:
        raise QuotaContractError("invalid_shape", locator + ("kind",))
    kind = _parse_identifier(payload.get("kind"), locator + ("kind",))
    if kind in _WINDOW_DURATION_KINDS:
        _ensure_exact_keys(payload, _WINDOW_DURATION_KEYS, locator)
        _parse_identifier(payload.get("window_id"), locator + ("window_id",))
        unit_ref = _parse_unit_ref_value(payload.get("unit_ref"), locator + ("unit_ref",))
        unit = unit_defs.get(unit_ref)
        if unit is None:
            raise QuotaContractError("unresolved_reference", locator + ("unit_ref",))
        if unit.quantity_kind != "amount":
            raise QuotaContractError("incompatible_semantics", locator + ("kind",))
        _parse_duration(payload.get("duration_ms"), locator + ("duration_ms",))
        return _freeze_wire(payload)
    if kind in _WINDOW_MIN_KINDS:
        _ensure_exact_keys(payload, _WINDOW_MIN_KEYS, locator)
        _parse_identifier(payload.get("window_id"), locator + ("window_id",))
        unit_ref = _parse_unit_ref_value(payload.get("unit_ref"), locator + ("unit_ref",))
        unit = unit_defs.get(unit_ref)
        if unit is None:
            raise QuotaContractError("unresolved_reference", locator + ("unit_ref",))
        if unit.quantity_kind != "gauge":
            raise QuotaContractError("incompatible_semantics", locator + ("kind",))
        return _freeze_wire(payload)
    if kind == "unknown":
        _ensure_exact_keys(payload, _WINDOW_UNKNOWN_KEYS, locator)
        _parse_identifier(payload.get("window_id"), locator + ("window_id",))
        unit_ref = _parse_unit_ref_value(payload.get("unit_ref"), locator + ("unit_ref",))
        if unit_ref not in unit_defs:
            raise QuotaContractError("unresolved_reference", locator + ("unit_ref",))
        _parse_reason(payload.get("reason"), locator + ("reason",))
        return _freeze_wire(payload)
    raise QuotaContractError("invalid_identifier", locator + ("kind",))


def _parse_ref_list(value: object, locator: tuple[str | int, ...]) -> tuple[str, ...]:
    items = _expect_list(value, locator)
    if not items:
        raise QuotaContractError("invalid_shape", locator)
    if len(items) > _MAX_CONTEXT_ITEMS:
        raise QuotaContractError("resource_limit", locator)
    refs: list[str] = []
    seen_refs: set[str] = set()
    for index, item in enumerate(items):
        ref = _parse_ref(item, locator + (index,))
        if ref in seen_refs:
            raise QuotaContractError("duplicate_reference", locator + (index,))
        seen_refs.add(ref)
        refs.append(ref)
    return tuple(refs)


def _parse_profile_ref_list(
    value: object, locator: tuple[str | int, ...]
) -> tuple[tuple[int, str], ...]:
    items = _expect_list(value, locator)
    if not items:
        raise QuotaContractError("invalid_shape", locator)
    if len(items) > _MAX_GROUP_MEMBERS:
        raise QuotaContractError("resource_limit", locator)
    profiles: list[tuple[int, str]] = []
    seen_profiles: set[tuple[int, str]] = set()
    for index, item in enumerate(items):
        parsed = _parse_profile_ref_value(item, locator + (index,))
        if parsed in seen_profiles:
            raise QuotaContractError("duplicate_reference", locator + (index,))
        seen_profiles.add(parsed)
        profiles.append(parsed)
    return tuple(profiles)


def _parse_binding_subject(value: object, locator: tuple[str | int, ...]) -> bool:
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


def _parse_binding_constraints(
    value: object, locator: tuple[str | int, ...]
) -> tuple[_KnownValue, ...]:
    items = _expect_list(value, locator)
    if not items:
        raise QuotaContractError("invalid_shape", locator)
    if len(items) > _MAX_BINDING_CONSTRAINTS:
        raise QuotaContractError("resource_limit", locator)
    parsed: list[_KnownValue] = []
    seen_constraints: set[tuple[tuple[str, str, str, str], str]] = set()
    for index, item in enumerate(items):
        constraint = _parse_known_unknown(
            item,
            locator + (index,),
            _parse_binding_constraint_value,
        )
        if constraint.state == "known":
            if constraint.value in seen_constraints:
                raise QuotaContractError("duplicate_reference", locator + (index,))
            seen_constraints.add(constraint.value)
        parsed.append(constraint)
    return tuple(parsed)


def _parse_binding_constraint_value(
    value: object, locator: tuple[str | int, ...]
) -> tuple[tuple[str, str, str, str], str]:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("pool_ref", "window_id"), locator)
    return (
        _parse_pool_ref_value(payload.get("pool_ref"), locator + ("pool_ref",)),
        _parse_identifier(payload.get("window_id"), locator + ("window_id",)),
    )


def _parse_coverage(value: object, locator: tuple[str | int, ...]) -> str:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("state", "gaps"), locator)
    state = _parse_enum_identifier(
        payload.get("state"), locator + ("state",), _COVERAGE_STATES
    )
    gaps = _expect_list(payload.get("gaps"), locator + ("gaps",))
    if len(gaps) > _MAX_COVERAGE_GAPS:
        raise QuotaContractError("resource_limit", locator + ("gaps",))
    if state == "complete" and gaps:
        raise QuotaContractError("invalid_shape", locator + ("gaps",))
    if state == "partial" and not gaps:
        raise QuotaContractError("invalid_shape", locator + ("gaps",))
    seen_gaps: set[tuple[str, str]] = set()
    for index, item in enumerate(gaps):
        gap_locator = locator + ("gaps", index)
        gap = _expect_dict(item, gap_locator)
        _ensure_exact_keys(gap, ("scope", "reason"), gap_locator)
        scope = _parse_identifier(gap.get("scope"), gap_locator + ("scope",))
        reason = _parse_reason(gap.get("reason"), gap_locator + ("reason",))
        pair = (scope, reason)
        if pair in seen_gaps:
            raise QuotaContractError("duplicate_reference", gap_locator)
        seen_gaps.add(pair)
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


def _validate_observation_descriptors(
    descriptors: tuple[PoolDescriptor, ...],
) -> None:
    seen_pool_refs: set[tuple[str, str, str, str]] = set()
    for index, descriptor in enumerate(descriptors):
        if descriptor.pool_ref in seen_pool_refs:
            raise QuotaContractError("duplicate_reference", ("descriptors", index))
        seen_pool_refs.add(descriptor.pool_ref)


def _ensure_total_bytes(
    observed: int, limit: int, locator: tuple[str | int, ...]
) -> None:
    if observed > limit:
        raise QuotaContractError("resource_limit", locator)


def _build_unit_catalog(
    descriptors: tuple[PoolDescriptor, ...],
    unit_catalog: tuple[UnitDefinition, ...],
) -> dict[tuple[str, str], UnitDefinition]:
    mapping: dict[tuple[str, str], UnitDefinition] = {}
    for index, unit in enumerate(unit_catalog):
        if unit.ref in mapping:
            raise QuotaContractError("duplicate_reference", ("unit_catalog", index))
        mapping[unit.ref] = unit
    for descriptor_index, descriptor in enumerate(descriptors):
        for unit_index, unit in enumerate(descriptor.units):
            existing = mapping.get(unit.ref)
            if existing is None:
                mapping[unit.ref] = unit
                continue
            if existing.definition_key != unit.definition_key:
                raise QuotaContractError(
                    "unit_conflict",
                    ("descriptors", descriptor_index, "units", unit_index),
                )
    return mapping


def _measurement_kind_matches_quantity_kind(
    measurement_kind: str | None, quantity_kind: str
) -> bool:
    if measurement_kind not in _MEASUREMENT_QUANTITY_KINDS:
        return True
    if quantity_kind == "gauge":
        return measurement_kind == "gauge_snapshot"
    return measurement_kind != "gauge_snapshot"


def _resolve_known_binding_constraints(
    constraints: tuple[_KnownValue, ...],
    descriptors: tuple[PoolDescriptor, ...],
) -> bool:
    has_unknown_window = False
    for index, constraint in enumerate(constraints):
        if constraint.state != "known":
            continue
        resolved_window = _resolve_pool_window_constraint(
            constraint.value,
            descriptors,
            ("constraints", index),
        )
        if resolved_window.kind == "unknown":
            has_unknown_window = True
    return has_unknown_window


def _resolve_pool_window_constraint(
    value: object,
    descriptors: tuple[PoolDescriptor, ...],
    locator: tuple[str | int, ...],
) -> _ResolvedWindow:
    pool_ref, window_id = value
    matching_descriptors = [descriptor for descriptor in descriptors if descriptor.pool_ref == pool_ref]
    if not matching_descriptors:
        raise QuotaContractError("unresolved_reference", locator)
    if len(matching_descriptors) > 1:
        raise QuotaContractError("duplicate_reference", locator)
    descriptor = matching_descriptors[0]
    descriptor_units = {unit.ref: unit for unit in descriptor.units}
    for window in descriptor.windows:
        if not isinstance(window, Mapping) or window.get("window_id") != window_id:
            continue
        unit_ref_payload = window.get("unit_ref")
        if not isinstance(unit_ref_payload, Mapping):
            raise QuotaContractError("invalid_type", locator + ("unit_ref",))
        unit_ref = (
            _parse_identifier(unit_ref_payload.get("unit_id"), locator + ("unit_ref", "unit_id")),
            _parse_identifier(unit_ref_payload.get("version"), locator + ("unit_ref", "version")),
        )
        unit = descriptor_units.get(unit_ref)
        if unit is None:
            raise QuotaContractError("unresolved_reference", locator + ("unit_ref",))
        duration_ms = window.get("duration_ms")
        if duration_ms is not None and type(duration_ms) is not int:
            raise QuotaContractError("invalid_type", locator + ("duration_ms",))
        return _ResolvedWindow(
            kind=str(window.get("kind")),
            unit_ref=unit_ref,
            quantity_kind=unit.quantity_kind,
            duration_ms=duration_ms,
        )
    raise QuotaContractError("unresolved_reference", locator)


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
    if kind == "unknown":
        _ensure_exact_keys(payload, _WINDOW_INSTANCE_UNKNOWN_KEYS, locator)
        _parse_reason(payload.get("reason"), locator + ("reason",))
        return _WindowInstanceInfo(kind="unknown")
    if kind == "instant":
        _ensure_exact_keys(payload, _WINDOW_INSTANCE_INSTANT_KEYS, locator)
        _parse_time(payload.get("at_ms"), locator + ("at_ms",))
        return _WindowInstanceInfo(kind="instant")
    if kind == "interval":
        _ensure_exact_keys(payload, _WINDOW_INSTANCE_INTERVAL_KEYS, locator)
        start_ms = _parse_time(payload.get("start_ms"), locator + ("start_ms",))
        end_ms = _parse_time(payload.get("end_ms"), locator + ("end_ms",))
        if start_ms >= end_ms:
            raise QuotaContractError("invalid_time", locator + ("end_ms",))
        _parse_known_unknown(
            payload.get("epoch"),
            locator + ("epoch",),
            lambda epoch_value, epoch_locator: _parse_identifier(epoch_value, epoch_locator),
        )
        return _WindowInstanceInfo(kind="interval", start_ms=start_ms, end_ms=end_ms)
    raise QuotaContractError("invalid_identifier", locator + ("kind",))


def _parse_measurement(
    value: object, locator: tuple[str | int, ...]
) -> _QuantityInfo:
    payload = _expect_dict(value, locator)
    if "kind" not in payload:
        raise QuotaContractError("invalid_shape", locator + ("kind",))
    kind = _parse_identifier(payload.get("kind"), locator + ("kind",))
    if kind in _MEASUREMENT_QUANTITY_KINDS - {"usage_total"}:
        _ensure_exact_keys(payload, ("kind", "metric_id", "quantity"), locator)
        _parse_identifier(payload.get("metric_id"), locator + ("metric_id",))
        return _QuantityInfo(
            kind=kind,
            state=_parse_quantity(payload.get("quantity"), locator + ("quantity",))
        )
    if kind == "usage_total":
        _ensure_exact_keys(
            payload,
            ("kind", "metric_id", "quantity", "counter"),
            locator,
        )
        _parse_identifier(payload.get("metric_id"), locator + ("metric_id",))
        quantity_state = _parse_quantity(
            payload.get("quantity"),
            locator + ("quantity",),
        )
        _parse_counter(payload.get("counter"), locator + ("counter",))
        return _QuantityInfo(kind=kind, state=quantity_state)
    if kind in _MEASUREMENT_SIGNAL_KINDS:
        _ensure_exact_keys(payload, ("kind", "metric_id", "signal"), locator)
        _parse_identifier(payload.get("metric_id"), locator + ("metric_id",))
        _parse_identifier(payload.get("signal"), locator + ("signal",))
        return _QuantityInfo(kind=kind)
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
    kind = _parse_identifier(payload.get("kind"), locator + ("kind",))
    if kind == "exact":
        _ensure_exact_keys(payload, ("kind", "value"), locator)
        _parse_decimal_wire(payload.get("value"), locator + ("value",))
        return
    if kind == "bounds":
        _ensure_exact_keys(payload, ("kind", "lower", "upper"), locator)
        lower = _parse_optional_decimal_wire(payload.get("lower"), locator + ("lower",))
        upper = _parse_optional_decimal_wire(payload.get("upper"), locator + ("upper",))
        if lower is None and upper is None:
            raise QuotaContractError("invalid_bounds", locator)
        if lower is not None and upper is not None:
            try:
                if Decimal(lower) > Decimal(upper):
                    raise QuotaContractError("invalid_bounds", locator)
            except InvalidOperation as exc:  # pragma: no cover - guarded by regex
                raise QuotaContractError("invalid_decimal", locator) from exc
        return
    raise QuotaContractError("invalid_identifier", locator + ("kind",))


def _parse_decimal_wire(value: object, locator: tuple[str | int, ...]) -> str:
    if type(value) is not str:
        raise QuotaContractError("invalid_type", locator)
    if not _DECIMAL_WIRE_RE.fullmatch(value):
        raise QuotaContractError("invalid_decimal", locator)
    return value


def _parse_optional_decimal_wire(
    value: object, locator: tuple[str | int, ...]
) -> str | None:
    if value is None:
        return None
    return _parse_decimal_wire(value, locator)


def _parse_counter(value: object, locator: tuple[str | int, ...]) -> None:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("scope_id", "epoch"), locator)
    _parse_identifier(payload.get("scope_id"), locator + ("scope_id",))
    _parse_known_unknown(
        payload.get("epoch"),
        locator + ("epoch",),
        lambda epoch_value, epoch_locator: _parse_identifier(epoch_value, epoch_locator),
    )


def _parse_source(value: object, locator: tuple[str | int, ...]) -> _SourceInfo:
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
    method = _parse_enum_identifier(
        payload.get("method"), locator + ("method",), _SOURCE_METHOD_KINDS
    )
    _parse_ref_list(payload.get("provenance_refs"), locator + ("provenance_refs",))
    event_identity = _expect_dict(
        payload.get("event_identity"),
        locator + ("event_identity",),
    )
    state = event_identity.get("state")
    if type(state) is not str:
        raise QuotaContractError("invalid_type", locator + ("event_identity", "state"))
    if state == "known":
        _ensure_exact_keys(
            event_identity,
            ("state", "namespace", "epoch", "event_id"),
            locator + ("event_identity",),
        )
        _parse_identifier(
            event_identity.get("namespace"),
            locator + ("event_identity", "namespace"),
        )
        _parse_identifier(
            event_identity.get("epoch"),
            locator + ("event_identity", "epoch"),
        )
        _parse_identifier(
            event_identity.get("event_id"),
            locator + ("event_identity", "event_id"),
        )
        return _SourceInfo(
            source_id=source_id,
            method=method,
            event_identity_components=(
                str(event_identity.get("namespace")),
                str(event_identity.get("epoch")),
                str(event_identity.get("event_id")),
            ),
        )
    if state == "unknown":
        _ensure_exact_keys(
            event_identity,
            ("state", "reason"),
            locator + ("event_identity",),
        )
        _parse_reason(
            event_identity.get("reason"),
            locator + ("event_identity", "reason"),
        )
        return _SourceInfo(source_id=source_id, method=method)
    raise QuotaContractError("invalid_shape", locator + ("event_identity", "state"))


def _validate_window_instance_against_scope(
    window_instance: _WindowInstanceInfo,
    resolved_scope: _ResolvedWindow | None,
) -> None:
    if resolved_scope is None:
        return
    if resolved_scope.kind == "unknown":
        if window_instance.kind != "unknown":
            raise QuotaContractError("incompatible_semantics", ("window_instance", "kind"))
        return
    if window_instance.kind == "unknown":
        return
    if resolved_scope.kind in _WINDOW_DURATION_KINDS:
        if window_instance.kind != "interval":
            raise QuotaContractError("incompatible_semantics", ("window_instance", "kind"))
        if (
            window_instance.start_ms is None
            or window_instance.end_ms is None
            or resolved_scope.duration_ms is None
            or window_instance.end_ms - window_instance.start_ms != resolved_scope.duration_ms
        ):
            raise QuotaContractError("incompatible_semantics", ("window_instance", "end_ms"))
        return
    if resolved_scope.kind == "instantaneous" and window_instance.kind != "instant":
        raise QuotaContractError("incompatible_semantics", ("window_instance", "kind"))


def _validate_measurement_semantics(
    measurement: _QuantityInfo,
    *,
    source: _SourceInfo,
    unit_ref: _KnownValue,
    resolved_unit: UnitDefinition | None,
    resolved_scope: _ResolvedWindow | None,
) -> None:
    if resolved_scope is not None and resolved_unit is not None:
        if resolved_scope.unit_ref != resolved_unit.ref:
            raise QuotaContractError("incompatible_semantics", ("unit_ref",))
    if measurement.kind in _MEASUREMENT_QUANTITY_KINDS:
        if (
            resolved_scope is not None
            and not _measurement_kind_matches_quantity_kind(
                measurement.kind,
                resolved_scope.quantity_kind,
            )
        ):
            raise QuotaContractError("incompatible_semantics", ("measurement", "kind"))
        if (
            resolved_unit is not None
            and not _measurement_kind_matches_quantity_kind(
                measurement.kind,
                resolved_unit.quantity_kind,
            )
        ):
            raise QuotaContractError("incompatible_semantics", ("measurement", "kind"))
        if unit_ref.state == "unknown" and measurement.state in {"observed", "estimated"}:
            raise QuotaContractError("incompatible_semantics", ("measurement", "quantity"))
        if source.method == "estimate" and measurement.state == "observed":
            raise QuotaContractError("incompatible_semantics", ("source", "method"))
        if source.method == "legacy" and measurement.state != "unknown":
            raise QuotaContractError("incompatible_semantics", ("source", "method"))
        return
    if measurement.kind == "limit_signal" and source.method not in _LIMIT_SIGNAL_SOURCE_METHODS:
        raise QuotaContractError("incompatible_semantics", ("source", "method"))
