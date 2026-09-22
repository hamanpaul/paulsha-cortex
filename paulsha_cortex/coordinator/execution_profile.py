"""Pure execution-profile schema and canonical-key contract.

The module deliberately has no runtime, filesystem, environment, or registry
dependencies.  A descriptor describes the native effort grammar for one
adapter; a profile is one immutable requested, resolved, or observed record.
The public key functions operate only on parsed records.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import math
import re
import struct
from types import MappingProxyType
from typing import NoReturn

__all__ = [
    "MAX_DEPTH",
    "MAX_NODES",
    "MAX_SEMANTIC_BYTES",
    "MAX_TEXT_BYTES",
    "ExecutionProfileError",
    "ExecutionProfileContractError",
    "ProfileContractError",
    "ExecutionProfileDescriptor",
    "ExecutionProfile",
    "Descriptor",
    "Profile",
    "parse_descriptor",
    "parse_profile",
    "canonical_profile_bytes",
    "profile_key",
    "actual_condition_key",
    "actual_condition_missing_fields",
]


SCHEMA_VERSION = 1
MAX_DEPTH = 16
MAX_NODES = 4096
MAX_SEMANTIC_BYTES = 65536
MAX_TEXT_BYTES = 1048576
_MAX_DEPTH = MAX_DEPTH
_MAX_NODES = MAX_NODES
_MAX_SEMANTIC_BYTES = MAX_SEMANTIC_BYTES
_MAX_TEXT_BYTES = MAX_TEXT_BYTES

_DOMAIN_PREFIX = b"cortex.execution-profile"
_ID_FIELDS = frozenset(("id", "version", "revision"))
_PLANES = frozenset(("requested", "resolved", "observed"))
_CONDITION_FIELDS = (
    "adapter",
    "model",
    "effort",
    "loadout",
    "toolset",
    "sandbox",
    "permissions",
    "toolchain",
)
_SET_CONDITIONS = frozenset(("toolset", "permissions"))
_REQUIREMENT_FIELDS = ("role", "minimum_quality", "pin", "independence")
_METADATA_FIELDS = frozenset(
    (
        "pricing",
        "pricing_provenance",
        "timestamps",
        "evidence_refs",
        "approval_receipt_refs",
        "discovery",
    )
)
_DESCRIPTOR_FIELDS = (
    "schema_version",
    "id",
    "adapter",
    "model",
    "effort_grammar",
    "provenance",
    "metadata",
)
_PROFILE_FIELDS = (
    "schema_version",
    "plane",
    "conditions",
    "requirements",
    "provenance",
    "metadata",
)

_REF_NAMESPACE_RE = re.compile(r"[a-z][a-z0-9+.-]{1,31}\Z")
_REF_BODY_RE = re.compile(r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ExecutionProfileError(ValueError):
    """Machine-readable, value-redacting schema validation failure."""

    def __init__(
        self,
        code: str,
        locator: tuple[str | int, ...] = (),
    ) -> None:
        self.code = code
        self.locator = tuple(locator)
        super().__init__(self._message())

    def _message(self) -> str:
        if not self.locator:
            return self.code
        return f"{self.code} at " + ".".join(str(part) for part in self.locator)


# The shorter name is useful to callers that treat all schema modules alike.
ProfileContractError = ExecutionProfileError
ExecutionProfileContractError = ExecutionProfileError


class _Stats:
    __slots__ = ("depth", "nodes", "semantic_bytes")

    def __init__(self, depth: int, nodes: int, semantic_bytes: int) -> None:
        self.depth = depth
        self.nodes = nodes
        self.semantic_bytes = semantic_bytes


def _fail(code: str, locator: tuple[str | int, ...] = ()) -> NoReturn:
    raise ExecutionProfileError(code, locator)


def _format_locator(locator: tuple[str | int, ...]) -> tuple[str | int, ...]:
    return tuple(locator)


def _normalize_string(value: str, locator: tuple[str | int, ...]) -> str:
    """Reject lone surrogates and combine an explicitly encoded pair."""

    result: list[str] = []
    index = 0
    while index < len(value):
        codepoint = ord(value[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 >= len(value):
                _fail("invalid_unicode", locator)
            following = ord(value[index + 1])
            if not 0xDC00 <= following <= 0xDFFF:
                _fail("invalid_unicode", locator)
            result.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + following - 0xDC00))
            index += 2
            continue
        if 0xDC00 <= codepoint <= 0xDFFF:
            _fail("invalid_unicode", locator)
        result.append(value[index])
        index += 1
    return "".join(result)


def _clone_native(value: object, locator: tuple[str | int, ...] = ()) -> object:
    """Copy exact JSON-compatible native values without retaining aliases."""

    active: set[int] = set()

    def clone(current: object, current_locator: tuple[str | int, ...]) -> object:
        if type(current) is str:
            return _normalize_string(current, current_locator)
        if current is None or type(current) is bool:
            return current
        if type(current) is int:
            return current
        if type(current) is float:
            if not math.isfinite(current):
                _fail("non_finite_number", current_locator)
            return current
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in active:
                _fail("cyclic_value", current_locator)
            active.add(identity)
            copied: dict[str, object] = {}
            try:
                for key, item in current.items():
                    if type(key) is not str:
                        _fail("invalid_type", current_locator + ("<key>",))
                    normalized_key = _normalize_string(key, current_locator + ("<key>",))
                    if normalized_key in copied:
                        _fail("duplicate_key", current_locator + ("<duplicate>",))
                    copied[normalized_key] = clone(
                        item, current_locator + (normalized_key,)
                    )
            finally:
                active.remove(identity)
            return copied
        if type(current) is list:
            identity = id(current)
            if identity in active:
                _fail("cyclic_value", current_locator)
            active.add(identity)
            copied_list: list[object] = []
            try:
                for index, item in enumerate(current):
                    copied_list.append(clone(item, current_locator + (index,)))
            finally:
                active.remove(identity)
            return copied_list
        _fail("invalid_type", current_locator)

    return clone(value, locator)


def _json_constant(value: str) -> NoReturn:
    del value
    _fail("non_finite_number")


def _json_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        _fail("non_finite_number")
    return value


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        normalized = _normalize_string(key, ("<key>",))
        if normalized in result:
            _fail("duplicate_key", ("<duplicate>",))
        result[normalized] = value
    return result


def _decode_input(value: object, locator: tuple[str | int, ...]) -> object:
    if isinstance(value, str) or isinstance(value, (bytes, bytearray)):
        if isinstance(value, str):
            text = value
            try:
                raw = text.encode("utf-8")
            except UnicodeEncodeError:
                _fail("invalid_unicode", locator)
        else:
            raw = bytes(value)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                _fail("invalid_unicode", locator)
        if len(raw) > MAX_TEXT_BYTES:
            _fail("transport_too_large", locator)
        if text.startswith("\ufeff"):
            _fail("invalid_unicode", locator)
        try:
            decoded = json.loads(
                text,
                object_pairs_hook=_json_object,
                parse_int=int,
                parse_float=_json_float,
                parse_constant=_json_constant,
            )
        except ExecutionProfileError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            _fail("invalid_json", locator)
        return _clone_native(decoded, locator)
    return _clone_native(value, locator)


def _expect_mapping(value: object, locator: tuple[str | int, ...]) -> dict[str, object]:
    if type(value) is not dict:
        _fail("invalid_type", locator)
    return value


def _expect_list(value: object, locator: tuple[str | int, ...]) -> list[object]:
    if type(value) is not list:
        _fail("invalid_type", locator)
    return value


def _exact_keys(
    payload: dict[str, object],
    expected: tuple[str, ...] | frozenset[str],
    locator: tuple[str | int, ...],
) -> None:
    expected_set = set(expected)
    if set(payload) - expected_set:
        _fail("unknown_field", locator + ("<unknown>",))
    for key in expected:
        if key not in payload:
            _fail("missing_field", locator + (key,))


def _allowed_keys(
    payload: dict[str, object],
    allowed: tuple[str, ...] | frozenset[str],
    locator: tuple[str | int, ...],
) -> None:
    if set(payload) - set(allowed):
        _fail("unknown_field", locator + ("<unknown>",))


def _schema_version(value: object, locator: tuple[str | int, ...]) -> int:
    if type(value) is not int:
        _fail("invalid_type", locator)
    if value != SCHEMA_VERSION:
        _fail("unsupported_schema", locator)
    return value


def _id_string(value: object, locator: tuple[str | int, ...]) -> str:
    if type(value) is not str:
        _fail("invalid_type", locator)
    if not value or value != value.strip() or _CONTROL_RE.search(value):
        _fail("invalid_identifier", locator)
    return value


def _reason(value: object, locator: tuple[str | int, ...]) -> str:
    if type(value) is not str:
        _fail("invalid_type", locator)
    if not value or value != value.strip():
        _fail("invalid_reason", locator)
    return value


def _reference(value: object, locator: tuple[str | int, ...]) -> str:
    if type(value) is not str:
        _fail("invalid_type", locator)
    if (
        not value.isascii()
        or len(value.encode("ascii")) > 1024
        or ":" not in value
    ):
        _fail("invalid_reference", locator)
    namespace, _, body = value.partition(":")
    if (
        not _REF_NAMESPACE_RE.fullmatch(namespace)
        or not body
        or not _REF_BODY_RE.fullmatch(body)
    ):
        _fail("invalid_reference", locator)
    return value


def _parse_provenance(value: object, locator: tuple[str | int, ...]) -> list[object]:
    entries = _expect_list(value, locator)
    for index, entry in enumerate(entries):
        entry_locator = locator + (index,)
        payload = _expect_mapping(entry, entry_locator)
        _exact_keys(payload, ("kind", "ref"), entry_locator)
        _id_string(payload["kind"], entry_locator + ("kind",))
        _reference(payload["ref"], entry_locator + ("ref",))
    return entries


def _parse_metadata(value: object, locator: tuple[str | int, ...]) -> dict[str, object]:
    payload = _expect_mapping(value, locator)
    if set(payload) - _METADATA_FIELDS:
        _fail("unknown_field", locator + ("<unknown>",))
    return payload


def _parse_adapter(value: object, locator: tuple[str | int, ...]) -> dict[str, object]:
    payload = _expect_mapping(value, locator)
    expected = ("id", "protocol_id", "protocol_version", "runtime_version")
    _exact_keys(payload, expected, locator)
    for key in expected:
        _id_string(payload[key], locator + (key,))
    return payload


def _parse_model(value: object, locator: tuple[str | int, ...]) -> dict[str, object]:
    payload = _expect_mapping(value, locator)
    _exact_keys(payload, ("id", "revision"), locator)
    _id_string(payload["id"], locator + ("id",))
    _id_string(payload["revision"], locator + ("revision",))
    return payload


def _parse_versioned_ref(
    value: object, locator: tuple[str | int, ...]
) -> dict[str, object]:
    payload = _expect_mapping(value, locator)
    _exact_keys(payload, ("id", "version"), locator)
    _id_string(payload["id"], locator + ("id",))
    _id_string(payload["version"], locator + ("version",))
    return payload


def _validate_grammar(
    value: object,
    locator: tuple[str | int, ...],
    *,
    top_level: bool,
) -> dict[str, object]:
    grammar = _expect_mapping(value, locator)
    if "type" not in grammar:
        _fail("missing_field", locator + ("type",))
    grammar_type = grammar["type"]
    if type(grammar_type) is not str:
        _fail("invalid_type", locator + ("type",))

    if grammar_type in {"none", "boolean", "null"}:
        _exact_keys(grammar, ("type",), locator)
        if grammar_type == "none" and not top_level:
            _fail("invalid_grammar", locator + ("type",))
        if grammar_type in {"boolean", "null"} and top_level:
            _fail("invalid_grammar", locator + ("type",))
        return grammar

    if grammar_type == "string":
        _exact_keys(grammar, ("type", "enum"), locator) if "enum" in grammar else _exact_keys(grammar, ("type",), locator)
        if "enum" in grammar:
            enum = _expect_list(grammar["enum"], locator + ("enum",))
            if not enum:
                _fail("invalid_grammar", locator + ("enum",))
            seen: set[str] = set()
            for index, item in enumerate(enum):
                if type(item) is not str:
                    _fail("invalid_type", locator + ("enum", index))
                if item in seen:
                    _fail("duplicate_value", locator + ("enum", index))
                seen.add(item)
        return grammar

    if grammar_type == "integer":
        _allowed_keys(grammar, ("type", "min", "max"), locator)
        if "min" in grammar and type(grammar["min"]) is not int:
            _fail("invalid_type", locator + ("min",))
        if "max" in grammar and type(grammar["max"]) is not int:
            _fail("invalid_type", locator + ("max",))
        if "min" in grammar and "max" in grammar and grammar["min"] > grammar["max"]:
            _fail("invalid_grammar", locator)
        return grammar

    if grammar_type == "number":
        _allowed_keys(grammar, ("type", "min", "max"), locator)
        for bound in ("min", "max"):
            if bound in grammar:
                if type(grammar[bound]) is not float or not math.isfinite(grammar[bound]):
                    _fail("invalid_type", locator + (bound,))
        if "min" in grammar and "max" in grammar and grammar["min"] > grammar["max"]:
            _fail("invalid_grammar", locator)
        return grammar

    if grammar_type == "array":
        _exact_keys(grammar, ("type", "items"), locator)
        _validate_grammar(grammar["items"], locator + ("items",), top_level=False)
        return grammar

    if grammar_type == "object":
        _exact_keys(grammar, ("type", "properties", "required"), locator)
        properties = _expect_mapping(grammar["properties"], locator + ("properties",))
        for name, child in properties.items():
            if type(name) is not str or not name:
                _fail("invalid_identifier", locator + ("properties", "<unknown>"))
            _validate_grammar(
                child,
                locator + ("properties", name),
                top_level=False,
            )
        required = _expect_list(grammar["required"], locator + ("required",))
        seen_required: set[str] = set()
        for index, name in enumerate(required):
            if type(name) is not str:
                _fail("invalid_type", locator + ("required", index))
            if name in seen_required or name not in properties:
                _fail("invalid_grammar", locator + ("required", index))
            seen_required.add(name)
        return grammar

    _fail("invalid_grammar", locator + ("type",))


def _validate_grammar_value(
    value: object,
    grammar: dict[str, object],
    locator: tuple[str | int, ...],
) -> None:
    grammar_type = grammar["type"]
    if grammar_type == "string":
        if type(value) is not str:
            _fail("invalid_type", locator)
        enum = grammar.get("enum")
        if enum is not None and value not in enum:
            _fail("invalid_value", locator)
        return
    if grammar_type == "integer":
        if type(value) is not int:
            _fail("invalid_type", locator)
        if "min" in grammar and value < grammar["min"]:
            _fail("out_of_range", locator)
        if "max" in grammar and value > grammar["max"]:
            _fail("out_of_range", locator)
        return
    if grammar_type == "number":
        if type(value) is not float:
            _fail("invalid_type", locator)
        if not math.isfinite(value):
            _fail("non_finite_number", locator)
        if "min" in grammar and value < grammar["min"]:
            _fail("out_of_range", locator)
        if "max" in grammar and value > grammar["max"]:
            _fail("out_of_range", locator)
        return
    if grammar_type == "boolean":
        if type(value) is not bool:
            _fail("invalid_type", locator)
        return
    if grammar_type == "null":
        if value is not None:
            _fail("invalid_type", locator)
        return
    if grammar_type == "array":
        items = _expect_list(value, locator)
        child = grammar["items"]
        for index, item in enumerate(items):
            _validate_grammar_value(item, child, locator + (index,))
        return
    if grammar_type == "object":
        payload = _expect_mapping(value, locator)
        properties = grammar["properties"]
        if set(payload) - set(properties):
            _fail("unknown_field", locator + ("<unknown>",))
        for name in grammar["required"]:
            if name not in payload:
                _fail("missing_field", locator + (name,))
        for name, item in payload.items():
            _validate_grammar_value(item, properties[name], locator + (name,))
        return
    _fail("invalid_grammar", locator)


def _parse_tagged(
    value: object,
    locator: tuple[str | int, ...],
    *,
    parse_known=None,
    allow_not_applicable: bool = False,
    only_not_applicable: bool = False,
) -> None:
    payload = _expect_mapping(value, locator)
    if "state" not in payload:
        _fail("missing_field", locator + ("state",))
    state = payload["state"]
    if type(state) is not str:
        _fail("invalid_type", locator + ("state",))
    if state == "known":
        if only_not_applicable:
            _fail("invalid_value", locator + ("state",))
        _exact_keys(payload, ("state", "value"), locator)
        if parse_known is not None:
            parse_known(payload["value"], locator + ("value",))
        return
    if state == "unknown":
        if only_not_applicable:
            _fail("invalid_value", locator + ("state",))
        _exact_keys(payload, ("state", "reason"), locator)
        _reason(payload["reason"], locator + ("reason",))
        return
    if state == "not_applicable":
        _exact_keys(payload, ("state",), locator)
        if not allow_not_applicable and not only_not_applicable:
            _fail("invalid_value", locator + ("state",))
        return
    _fail("invalid_value", locator + ("state",))


def _parse_effort_tagged(
    value: object,
    locator: tuple[str | int, ...],
    grammar: dict[str, object],
) -> None:
    grammar_type = grammar["type"]
    if grammar_type == "none":
        _parse_tagged(
            value,
            locator,
            only_not_applicable=True,
            allow_not_applicable=True,
        )
        return
    _parse_tagged(
        value,
        locator,
        parse_known=lambda item, item_locator: _validate_grammar_value(
            item, grammar, item_locator
        ),
        allow_not_applicable=False,
    )


def _parse_condition_value(
    name: str,
    value: object,
    locator: tuple[str | int, ...],
) -> None:
    if name in {"adapter", "model", "loadout", "sandbox", "toolchain"}:
        parser = _parse_adapter if name == "adapter" else _parse_model if name == "model" else _parse_versioned_ref
        parser(value, locator)
        return
    if name in _SET_CONDITIONS:
        entries = _expect_list(value, locator)
        for index, entry in enumerate(entries):
            _parse_versioned_ref(entry, locator + (index,))
        return
    _fail("invalid_field", locator)


def _validate_descriptor(payload: dict[str, object]) -> dict[str, object]:
    _exact_keys(payload, _DESCRIPTOR_FIELDS, ())
    _schema_version(payload["schema_version"], ("schema_version",))
    _id_string(payload["id"], ("id",))
    _parse_adapter(payload["adapter"], ("adapter",))
    _parse_model(payload["model"], ("model",))
    _validate_grammar(payload["effort_grammar"], ("effort_grammar",), top_level=True)
    _parse_provenance(payload["provenance"], ("provenance",))
    _parse_metadata(payload["metadata"], ("metadata",))
    return payload


def _validate_requirements(payload: dict[str, object]) -> None:
    _exact_keys(payload, _REQUIREMENT_FIELDS, ("requirements",))

    def role(value: object, locator: tuple[str | int, ...]) -> None:
        if value is not None:
            _id_string(value, locator)

    _parse_tagged(payload["role"], ("requirements", "role"), parse_known=role)
    for name in _REQUIREMENT_FIELDS[1:]:
        _parse_tagged(payload[name], ("requirements", name))


def _validate_profile(
    payload: dict[str, object], descriptor: "ExecutionProfileDescriptor"
) -> dict[str, object]:
    _exact_keys(payload, _PROFILE_FIELDS, ())
    _schema_version(payload["schema_version"], ("schema_version",))
    plane = payload["plane"]
    if type(plane) is not str or plane not in _PLANES:
        _fail("invalid_value", ("plane",))

    conditions = _expect_mapping(payload["conditions"], ("conditions",))
    _exact_keys(conditions, _CONDITION_FIELDS, ("conditions",))
    for name in _CONDITION_FIELDS:
        locator = ("conditions", name)
        if name == "effort":
            _parse_effort_tagged(
                conditions[name], locator, descriptor._wire["effort_grammar"]
            )
            continue
        _parse_tagged(
            conditions[name],
            locator,
            parse_known=lambda item, item_locator, field=name: _parse_condition_value(
                field, item, item_locator
            ),
        )

    adapter_wrapper = conditions["adapter"]
    if adapter_wrapper.get("state") == "known":
        if adapter_wrapper["value"] != descriptor._wire["adapter"]:
            _fail("descriptor_mismatch", ("conditions", "adapter", "value"))
    model_wrapper = conditions["model"]
    if model_wrapper.get("state") == "known":
        if model_wrapper["value"] != descriptor._wire["model"]:
            _fail("descriptor_mismatch", ("conditions", "model", "value"))

    requirements = _expect_mapping(payload["requirements"], ("requirements",))
    _validate_requirements(requirements)
    _parse_provenance(payload["provenance"], ("provenance",))
    _parse_metadata(payload["metadata"], ("metadata",))
    return payload


def _measure(value: object, locator: tuple[str | int, ...]) -> _Stats:
    nodes = 0
    maximum_depth = 0
    active: set[int] = set()

    def visit(current: object, depth: int, current_locator: tuple[str | int, ...]) -> None:
        nonlocal nodes, maximum_depth
        if depth > MAX_DEPTH:
            _fail("depth_exceeded", current_locator)
        nodes += 1
        if nodes > MAX_NODES:
            _fail("node_count_exceeded", current_locator)
        maximum_depth = max(maximum_depth, depth)
        if isinstance(current, Mapping) or type(current) is list:
            identity = id(current)
            if identity in active:
                _fail("cyclic_value", current_locator)
            active.add(identity)
            try:
                if isinstance(current, Mapping):
                    for key, item in current.items():
                        visit(key, depth + 1, current_locator + ("<key>",))
                        visit(item, depth + 1, current_locator + (str(key),))
                else:
                    for index, item in enumerate(current):
                        visit(item, depth + 1, current_locator + (index,))
            finally:
                active.remove(identity)

    visit(value, 1, locator)
    try:
        semantic_bytes = len(_typed_json_bytes(value))
    except (UnicodeEncodeError, RecursionError):
        _fail("invalid_unicode", locator)
    if semantic_bytes > MAX_SEMANTIC_BYTES:
        _fail("semantic_size_exceeded", locator)
    return _Stats(maximum_depth, nodes, semantic_bytes)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw(item) for item in value]
    return value


def _typed(value: object) -> list[object]:
    if value is None:
        return ["n"]
    if type(value) is bool:
        return ["b", value]
    if type(value) is int:
        return ["i", str(value)]
    if type(value) is float:
        if not math.isfinite(value):
            _fail("non_finite_number")
        bits = struct.unpack(">Q", struct.pack(">d", value))[0]
        return ["f", f"{bits:016x}"]
    if type(value) is str:
        return ["s", value]
    if type(value) is list or type(value) is tuple:
        return ["a", [_typed(item) for item in value]]
    if isinstance(value, Mapping):
        members = []
        for key, item in value.items():
            if type(key) is not str:
                _fail("invalid_type", ("<key>",))
            members.append((key, _typed(item)))
        members.sort(key=lambda item: item[0].encode("utf-8"))
        return ["o", [[key, item] for key, item in members]]
    _fail("invalid_type")


def _render_string(value: str) -> str:
    pieces = ['"']
    for character in value:
        codepoint = ord(character)
        if character == '"':
            pieces.append('\\"')
        elif character == "\\":
            pieces.append("\\\\")
        elif codepoint <= 0x1F:
            pieces.append(f"\\u{codepoint:04x}")
        else:
            pieces.append(character)
    pieces.append('"')
    return "".join(pieces)


def _render_typed(value: object) -> str:
    if type(value) is str:
        return _render_string(value)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is list:
        return "[" + ",".join(_render_typed(item) for item in value) + "]"
    _fail("invalid_type")


def _typed_json_bytes(value: object) -> bytes:
    return _render_typed(_typed(value)).encode("utf-8")


class _ImmutableRecord:
    __slots__ = ("_wire", "_stats", "_sealed")

    def __init__(self, wire: object, stats: _Stats, token: object) -> None:
        if token is not _CONSTRUCTOR_TOKEN:
            raise TypeError("records are parser-sealed; use the parse function")
        object.__setattr__(self, "_wire", wire)
        object.__setattr__(self, "_stats", stats)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del value
        raise AttributeError(f"{type(self).__name__} is immutable")

    def to_dict(self) -> dict[str, object]:
        result = _thaw(self._wire)
        if type(result) is not dict:
            raise TypeError("record wire must be an object")
        return result

    @property
    def depth(self) -> int:
        return self._stats.depth

    @property
    def node_count(self) -> int:
        return self._stats.nodes

    @property
    def semantic_bytes(self) -> int:
        return self._stats.semantic_bytes

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and self._wire == other._wire

    def __hash__(self) -> int:
        return hash(_typed_json_bytes(self._wire))


_CONSTRUCTOR_TOKEN = object()


class ExecutionProfileDescriptor(_ImmutableRecord):
    __slots__ = ()

    @property
    def schema_version(self) -> int:
        return self._wire["schema_version"]

    @property
    def descriptor_id(self) -> str:
        return self._wire["id"]

    @property
    def id(self) -> str:
        return self._wire["id"]

    @property
    def adapter(self) -> Mapping[str, object]:
        return self._wire["adapter"]

    @property
    def model(self) -> Mapping[str, object]:
        return self._wire["model"]

    @property
    def effort_grammar(self) -> Mapping[str, object]:
        return self._wire["effort_grammar"]

    @property
    def provenance(self) -> tuple[object, ...]:
        return self._wire["provenance"]

    @property
    def metadata(self) -> Mapping[str, object]:
        return self._wire["metadata"]


class ExecutionProfile(_ImmutableRecord):
    __slots__ = ("_descriptor",)

    def __init__(
        self,
        wire: object,
        stats: _Stats,
        descriptor: ExecutionProfileDescriptor,
        token: object,
    ) -> None:
        super().__init__(wire, stats, token)
        object.__setattr__(self, "_descriptor", descriptor)

    @property
    def descriptor(self) -> ExecutionProfileDescriptor:
        return self._descriptor

    @property
    def schema_version(self) -> int:
        return self._wire["schema_version"]

    @property
    def plane(self) -> str:
        return self._wire["plane"]

    @property
    def conditions(self) -> Mapping[str, object]:
        return self._wire["conditions"]

    @property
    def requirements(self) -> Mapping[str, object]:
        return self._wire["requirements"]

    @property
    def provenance(self) -> tuple[object, ...]:
        return self._wire["provenance"]

    @property
    def metadata(self) -> Mapping[str, object]:
        return self._wire["metadata"]

    def __eq__(self, other: object) -> bool:
        return (
            type(self) is type(other)
            and self._wire == other._wire
            and self._descriptor == other._descriptor
        )


Descriptor = ExecutionProfileDescriptor
Profile = ExecutionProfile


def parse_descriptor(value: object) -> ExecutionProfileDescriptor:
    payload = _decode_input(value, ("descriptor",))
    mapping = _expect_mapping(payload, ())
    stats = _measure(mapping, ("descriptor",))
    _validate_descriptor(mapping)
    return ExecutionProfileDescriptor(_freeze(mapping), stats, _CONSTRUCTOR_TOKEN)


def parse_profile(
    value: object,
    descriptor: ExecutionProfileDescriptor | Mapping[str, object] | str | bytes,
) -> ExecutionProfile:
    if isinstance(descriptor, ExecutionProfileDescriptor):
        parsed_descriptor = descriptor
    else:
        parsed_descriptor = parse_descriptor(descriptor)
    payload = _decode_input(value, ("profile",))
    mapping = _expect_mapping(payload, ())
    stats = _measure(mapping, ("profile",))
    if parsed_descriptor.node_count + stats.nodes > MAX_NODES:
        _fail("node_count_exceeded", ("profile",))
    if parsed_descriptor.semantic_bytes + stats.semantic_bytes > MAX_SEMANTIC_BYTES:
        _fail("semantic_size_exceeded", ("profile",))
    _validate_profile(mapping, parsed_descriptor)
    combined = _Stats(
        max(parsed_descriptor.depth, stats.depth),
        parsed_descriptor.node_count + stats.nodes,
        parsed_descriptor.semantic_bytes + stats.semantic_bytes,
    )
    return ExecutionProfile(
        _freeze(mapping), combined, parsed_descriptor, _CONSTRUCTOR_TOKEN
    )


def _normalized_conditions(profile: ExecutionProfile) -> dict[str, object]:
    conditions = _thaw(profile._wire["conditions"])
    if type(conditions) is not dict:
        _fail("invalid_type", ("conditions",))
    for name in _SET_CONDITIONS:
        wrapper = conditions[name]
        if wrapper["state"] != "known":
            continue
        values = wrapper["value"]
        keyed = [(_typed_json_bytes(item), item) for item in values]
        keyed.sort(key=lambda item: item[0])
        unique: list[object] = []
        previous: bytes | None = None
        for encoded, item in keyed:
            if encoded != previous:
                unique.append(item)
                previous = encoded
        wrapper["value"] = unique
    return conditions


def _record_projection(profile: ExecutionProfile) -> dict[str, object]:
    return {
        "schema_version": profile._wire["schema_version"],
        "plane": profile._wire["plane"],
        "conditions": _normalized_conditions(profile),
        "requirements": _thaw(profile._wire["requirements"]),
    }


def _actual_projection(profile: ExecutionProfile) -> dict[str, object]:
    return {
        "schema_version": profile._wire["schema_version"],
        "conditions": _normalized_conditions(profile),
    }


def canonical_profile_bytes(profile: ExecutionProfile) -> bytes:
    if not isinstance(profile, ExecutionProfile):
        _fail("invalid_profile")
    return _typed_json_bytes(_record_projection(profile))


def _framed_key(domain: str, canonical: bytes) -> str:
    frame = (
        _DOMAIN_PREFIX
        + b"\0v1\0"
        + domain.encode("ascii")
        + b"\0"
        + struct.pack(">Q", len(canonical))
        + canonical
    )
    digest = sha256(frame).hexdigest()
    return f"epk:v1:{domain}:{digest}"


def profile_key(profile: ExecutionProfile) -> str:
    if not isinstance(profile, ExecutionProfile):
        _fail("invalid_profile")
    domain = "request" if profile.plane == "requested" else profile.plane
    return _framed_key(domain, canonical_profile_bytes(profile))


def actual_condition_missing_fields(
    profile: ExecutionProfile,
) -> tuple[tuple[str | int, ...], ...]:
    if not isinstance(profile, ExecutionProfile) or profile.plane != "observed":
        return (("plane",),)
    missing: list[tuple[str | int, ...]] = []
    conditions = profile._wire["conditions"]
    for name in _CONDITION_FIELDS:
        if conditions[name]["state"] == "known":
            continue
        if name == "effort" and conditions[name]["state"] == "not_applicable":
            continue
        missing.append(("conditions", name))
    missing.sort()
    return tuple(missing)


def actual_condition_key(profile: ExecutionProfile) -> str | None:
    if not isinstance(profile, ExecutionProfile):
        _fail("invalid_profile")
    if actual_condition_missing_fields(profile):
        return None
    return _framed_key("actual", _typed_json_bytes(_actual_projection(profile)))
