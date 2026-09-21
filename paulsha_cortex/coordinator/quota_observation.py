"""Immutable quota observation wire-contract records.

This module is intentionally stdlib-only and side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
_TRUST_MARKER = object()
_MISSING_WIRE = object()
_DEFERRED_HELPER_RESULT = MappingProxyType({"state": "deferred"})

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
    state: str | None = None


def _build_record(cls: type[_RecordT], /, **attributes: object) -> _RecordT:
    record = object.__new__(cls)
    for name, value in attributes.items():
        object.__setattr__(record, name, value)
    object.__setattr__(record, "_trusted", None)
    return record


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
    _parse_binding_subject(snapshot.get("subject"), ("subject",))
    _parse_binding_constraints(snapshot.get("constraints"), ("constraints",))
    _parse_coverage(snapshot.get("coverage"), ("coverage",))
    return _seal_record(
        ProfilePoolBinding._from_parser(
            schema_version=schema_version,
            binding_id=binding_id,
            revision=revision,
            status=_DEFERRED_HELPER_RESULT,
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
    _parse_window_instance(snapshot.get("window_instance"), ("window_instance",))
    measurement = _parse_measurement(snapshot.get("measurement"), ("measurement",))
    _parse_known_unknown(
        snapshot.get("observed_at_ms"),
        ("observed_at_ms",),
        lambda value, locator: _parse_time(value, locator),
    )
    _parse_time(snapshot.get("received_at_ms"), ("received_at_ms",))
    _parse_known_unknown(
        snapshot.get("ttl_ms"),
        ("ttl_ms",),
        lambda value, locator: _parse_duration(value, locator),
    )
    _parse_known_unknown(
        snapshot.get("reset_at_ms"),
        ("reset_at_ms",),
        lambda value, locator: _parse_time(value, locator),
    )
    source_id = _parse_source(snapshot.get("source"), ("source",))
    _parse_coverage(snapshot.get("coverage"), ("coverage",))

    unit_map = _build_unit_catalog(unit_catalog)
    if scope.state == "known":
        raise QuotaContractError("unresolved_reference", ("scope",))
    if unit_ref.state == "known" and unit_ref.value not in unit_map:
        raise QuotaContractError("unresolved_reference", ("unit_ref",))
    if unit_ref.state == "unknown" and measurement.state in {"observed", "estimated"}:
        raise QuotaContractError("incompatible_semantics", ("measurement", "quantity"))

    return _seal_record(
        QuotaObservation._from_parser(
            schema_version=schema_version,
            observation_id=observation_id,
            source_id=source_id,
            event_identity_components=None,
            observed_at_ms=None,
            ttl_ms=None,
            reset_at_ms=None,
            window_end_ms=None,
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
    del now_utc_ms
    del allowed_clock_skew_ms
    if not _is_trusted_record(observation, QuotaObservation):
        raise QuotaContractError("invalid_type", ("observation",))
    return _DEFERRED_HELPER_RESULT


def event_identity(observation: QuotaObservation) -> Mapping[str, object]:
    if not _is_trusted_record(observation, QuotaObservation):
        raise QuotaContractError("invalid_type", ("observation",))
    return _DEFERRED_HELPER_RESULT


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
    for index, item in enumerate(items):
        item_locator = locator + (index,)
        payload = _expect_dict(item, item_locator)
        _ensure_exact_keys(payload, _UNIT_INLINE_KEYS, item_locator)
        units.append(_parse_inline_unit(payload, item_locator))
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
    unit_refs = frozenset(unit.ref for unit in units)
    windows: list[object] = []
    for index, item in enumerate(items):
        item_locator = locator + (index,)
        payload = _expect_dict(item, item_locator)
        windows.append(_parse_descriptor_window(payload, item_locator, unit_refs))
    return tuple(windows)


def _parse_descriptor_window(
    payload: dict[str, object],
    locator: tuple[str | int, ...],
    unit_refs: frozenset[tuple[str, str]],
) -> object:
    if "kind" not in payload:
        raise QuotaContractError("invalid_shape", locator + ("kind",))
    kind = _parse_identifier(payload.get("kind"), locator + ("kind",))
    if kind in _WINDOW_DURATION_KINDS:
        _ensure_exact_keys(payload, _WINDOW_DURATION_KEYS, locator)
        _parse_identifier(payload.get("window_id"), locator + ("window_id",))
        unit_ref = _parse_unit_ref_value(payload.get("unit_ref"), locator + ("unit_ref",))
        if unit_ref not in unit_refs:
            raise QuotaContractError("unresolved_reference", locator + ("unit_ref",))
        _parse_duration(payload.get("duration_ms"), locator + ("duration_ms",))
        return _freeze_wire(payload)
    if kind in _WINDOW_MIN_KINDS:
        _ensure_exact_keys(payload, _WINDOW_MIN_KEYS, locator)
        _parse_identifier(payload.get("window_id"), locator + ("window_id",))
        unit_ref = _parse_unit_ref_value(payload.get("unit_ref"), locator + ("unit_ref",))
        if unit_ref not in unit_refs:
            raise QuotaContractError("unresolved_reference", locator + ("unit_ref",))
        return _freeze_wire(payload)
    raise QuotaContractError("invalid_identifier", locator + ("kind",))


def _parse_ref_list(value: object, locator: tuple[str | int, ...]) -> tuple[str, ...]:
    items = _expect_list(value, locator)
    if not items:
        raise QuotaContractError("invalid_shape", locator)
    if len(items) > _MAX_CONTEXT_ITEMS:
        raise QuotaContractError("resource_limit", locator)
    refs: list[str] = []
    for index, item in enumerate(items):
        refs.append(_parse_ref(item, locator + (index,)))
    return tuple(refs)


def _parse_binding_subject(value: object, locator: tuple[str | int, ...]) -> None:
    payload = _expect_dict(value, locator)
    kind = payload.get("kind")
    if type(kind) is not str:
        raise QuotaContractError("invalid_type", locator + ("kind",))
    if kind != "profile":
        raise QuotaContractError("invalid_identifier", locator + ("kind",))
    _ensure_exact_keys(payload, ("kind", "profile_ref"), locator)
    _parse_known_unknown(
        payload.get("profile_ref"),
        locator + ("profile_ref",),
        _parse_profile_ref_value,
    )


def _parse_binding_constraints(
    value: object, locator: tuple[str | int, ...]
) -> tuple[_KnownValue, ...]:
    items = _expect_list(value, locator)
    if not items:
        raise QuotaContractError("invalid_shape", locator)
    if len(items) > _MAX_BINDING_CONSTRAINTS:
        raise QuotaContractError("resource_limit", locator)
    parsed: list[_KnownValue] = []
    for index, item in enumerate(items):
        parsed.append(
            _parse_known_unknown(
                item,
                locator + (index,),
                _parse_binding_constraint_value,
            )
        )
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


def _parse_coverage(value: object, locator: tuple[str | int, ...]) -> None:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("state", "gaps"), locator)
    state = _parse_enum_identifier(
        payload.get("state"), locator + ("state",), _COVERAGE_STATES
    )
    gaps = _expect_list(payload.get("gaps"), locator + ("gaps",))
    if len(gaps) > 32:
        raise QuotaContractError("resource_limit", locator + ("gaps",))
    if state == "complete" and gaps:
        raise QuotaContractError("invalid_shape", locator + ("gaps",))
    if state == "partial" and not gaps:
        raise QuotaContractError("invalid_shape", locator + ("gaps",))
    for index, item in enumerate(gaps):
        gap_locator = locator + ("gaps", index)
        gap = _expect_dict(item, gap_locator)
        _ensure_exact_keys(gap, ("scope", "reason"), gap_locator)
        _parse_identifier(gap.get("scope"), gap_locator + ("scope",))
        _parse_reason(gap.get("reason"), gap_locator + ("reason",))


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
        if unit.ref in mapping:
            raise QuotaContractError("duplicate_reference", ("unit_catalog", index))
        mapping[unit.ref] = unit
    return mapping


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
) -> object:
    payload = _expect_dict(value, locator)
    kind = payload.get("kind")
    if type(kind) is not str:
        raise QuotaContractError("invalid_type", locator + ("kind",))
    if kind == "unknown":
        _ensure_exact_keys(payload, _WINDOW_INSTANCE_UNKNOWN_KEYS, locator)
        _parse_reason(payload.get("reason"), locator + ("reason",))
        return _freeze_wire(payload)
    if kind == "instant":
        _ensure_exact_keys(payload, _WINDOW_INSTANCE_INSTANT_KEYS, locator)
        _parse_time(payload.get("at_ms"), locator + ("at_ms",))
        return _freeze_wire(payload)
    if kind == "interval":
        _ensure_exact_keys(payload, _WINDOW_INSTANCE_INTERVAL_KEYS, locator)
        _parse_time(payload.get("start_ms"), locator + ("start_ms",))
        _parse_time(payload.get("end_ms"), locator + ("end_ms",))
        _parse_known_unknown(
            payload.get("epoch"),
            locator + ("epoch",),
            lambda epoch_value, epoch_locator: _parse_identifier(epoch_value, epoch_locator),
        )
        return _freeze_wire(payload)
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
        return _QuantityInfo(state=quantity_state)
    if kind in _MEASUREMENT_SIGNAL_KINDS:
        _ensure_exact_keys(payload, ("kind", "metric_id", "signal"), locator)
        _parse_identifier(payload.get("metric_id"), locator + ("metric_id",))
        _parse_identifier(payload.get("signal"), locator + ("signal",))
        return _QuantityInfo()
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
    _ensure_exact_keys(payload, ("kind", "value"), locator)
    if _parse_identifier(payload.get("kind"), locator + ("kind",)) != "exact":
        raise QuotaContractError("invalid_identifier", locator + ("kind",))
    _parse_decimal_wire(payload.get("value"), locator + ("value",))


def _parse_decimal_wire(value: object, locator: tuple[str | int, ...]) -> str:
    if type(value) is not str:
        raise QuotaContractError("invalid_type", locator)
    if not _DECIMAL_WIRE_RE.fullmatch(value):
        raise QuotaContractError("invalid_decimal", locator)
    return value


def _parse_counter(value: object, locator: tuple[str | int, ...]) -> None:
    payload = _expect_dict(value, locator)
    _ensure_exact_keys(payload, ("scope_id", "epoch"), locator)
    _parse_identifier(payload.get("scope_id"), locator + ("scope_id",))
    _parse_known_unknown(
        payload.get("epoch"),
        locator + ("epoch",),
        lambda epoch_value, epoch_locator: _parse_identifier(epoch_value, epoch_locator),
    )


def _parse_source(value: object, locator: tuple[str | int, ...]) -> str:
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
    _parse_enum_identifier(
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
        return source_id
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
        return source_id
    raise QuotaContractError("invalid_shape", locator + ("event_identity", "state"))
