"""Trusted adapters joining execution profiles to existing launcher contracts.

Descriptors are data only.  Adapter capabilities (protocol/runtime versions,
native effort grammar and defaults, usage/quota labels) are loaded from the
packaged ``data/execution-adapters.yaml`` plus an optional operator overlay of
the same schema in the project config root.  Runtime code — the argv builder,
usage extractor, terminal/cancel/timeout contracts — is registered explicitly
by trusted Python; a descriptor can never import or execute provider supplied
code and can only describe adapters that already have trusted code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace as _replace
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable

from . import execution_profile as schema


PROFILE_BINDING_SCHEMA = 1
_PERSONA_ROLE = {"planner": "planning", "builder": "build", "reviewer": "review"}
_ROLE_CAPABILITY = dict(_PERSONA_ROLE)

#: Descriptor 檔的 wire version 與檔名（packaged 內建預設與 config root overlay 共用）。
ADAPTER_CATALOG_SCHEMA_VERSION = 1
ADAPTER_CATALOG_FILENAME = "execution-adapters.yaml"
_CATALOG_KEYS = frozenset({"schema_version", "adapters"})
_ADAPTER_DESCRIPTOR_KEYS = frozenset(
    {"protocol_id", "protocol_version", "runtime_version", "usage_source", "quota_state", "effort"}
)
_EFFORT_REQUIRED_KEYS = frozenset({"values", "default"})
_EFFORT_OPTIONAL_KEYS = frozenset({"model_defaults"})
_QUOTA_STATES = frozenset({"supported", "unsupported", "unknown"})
#: launcher 實際實作的生命週期契約；新 adapter 不能宣告 launcher 沒有的契約。
SUPPORTED_TERMINAL_CONTRACTS = frozenset({"shared-terminal-contract-v1"})
SUPPORTED_CANCELLATION_CONTRACTS = frozenset({"launcher-process-group-v1"})
SUPPORTED_TIMEOUT_CONTRACTS = frozenset({"job-watchdog-v1"})


class ExecutionAdapterError(ValueError):
    """A selected execution profile cannot be consumed safely."""


@dataclass(frozen=True)
class ExecutionAdapter:
    """One trusted implementation of an executor protocol.

    The launcher remains the owner of process isolation, terminal lifecycle,
    and cancellation.  These fields describe how the profile is expressed and
    which existing producer is responsible for each observation.

    Built-in adapters get every data field from the descriptor catalog and use
    the launcher's registered argv builder.  A new runtime supplies its own
    trusted ``argv_builder`` (and optionally ``usage_extractor``) through
    :func:`register_adapter`; descriptors cannot set either hook.
    """

    executor: str
    protocol_id: str
    protocol_version: str
    runtime_version: str
    efforts: tuple[str, ...] = ()
    default_effort: str | None = None
    usage_source: str = "unknown"
    terminal_contract: str = "shared-terminal-contract-v1"
    cancellation_contract: str = "launcher-process-group-v1"
    timeout_contract: str = "job-watchdog-v1"
    quota_state: str = "unknown"
    #: descriptor 宣告的 model → 原生 effort 預設（未列的 model 用 ``default_effort``）。
    model_default_efforts: tuple[tuple[str, str], ...] = ()
    argv_builder: Callable[..., list[str]] | None = field(default=None, compare=False, repr=False)
    usage_extractor: Callable[[str], dict[str, Any]] | None = field(
        default=None, compare=False, repr=False
    )
    #: 資料來源（packaged 檔／overlay 檔路徑，或 ``code``）；只供診斷，不進 key。
    descriptor_source: str = field(default="code", compare=False)

    @property
    def adapter_id(self) -> str:
        return f"{self.executor}-cli"

    def descriptor_fields(self) -> dict[str, str]:
        return {
            "id": self.adapter_id,
            "protocol_id": self.protocol_id,
            "protocol_version": self.protocol_version,
            "runtime_version": self.runtime_version,
        }

    def effort_grammar(self) -> dict[str, object]:
        if not self.efforts:
            return {"type": "none"}
        return {"type": "string", "enum": list(self.efforts)}

    def default_effort_for(self, model_id: str | None) -> str | None:
        """Descriptor-declared native effort default for ``model_id``."""

        if model_id is not None:
            for model, effort in self.model_default_efforts:
                if model == model_id:
                    return effort
        return self.default_effort

    def launch_effort(self, model: str | None, effort: str | None) -> str | None:
        """Resolve the native effort an argv builder must emit (``None`` = no flag).

        Resolver 與 argv builder 共用這一個函式，profile 上的 effort 與實際
        argv 因此不會各自寫死一份而漂移；未宣告的值在 spawn 前拒絕。
        """

        resolved = effort or self.default_effort_for(model)
        if resolved is None:
            return None
        if resolved not in self.efforts:
            raise ExecutionAdapterError(
                f"{self.executor} executor effort must be one of {sorted(self.efforts)}, "
                f"got {resolved!r}"
            )
        return resolved

    def trusted_argv_builder(self) -> Callable[..., list[str]] | None:
        if self.argv_builder is not None:
            return self.argv_builder
        from .launcher import _ARGV_BUILDERS

        return _ARGV_BUILDERS.get(self.executor)

    def accepts(self, parameter: str) -> bool:
        """Whether the trusted argv builder can express ``parameter``."""

        builder = self.trusted_argv_builder()
        if builder is None:
            return False
        try:
            parameters = inspect.signature(builder).parameters
        except (TypeError, ValueError):
            return False
        return parameter in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )

    def build_argv(self, kwargs: Mapping[str, object]) -> list[str]:
        """Delegate to the trusted argv builder for this executor."""

        builder = self.trusted_argv_builder()
        if builder is None:
            raise ExecutionAdapterError(f"unsupported adapter: {self.executor}")
        return builder(**dict(kwargs))

    def parse_terminal(self, payload: object) -> object:
        """Consume the shared terminal envelope without provider-specific rules."""

        from .terminal_contract import validate_envelope

        return validate_envelope(payload)

    def parse_usage(self, log_path: str | None) -> dict[str, Any]:
        from .usage_extractors import extract_usage

        if self.usage_extractor is None:
            return extract_usage(self.executor, log_path)
        if not log_path:
            return {"usage": None, "usage_raw": None, "usage_reason": "missing log_path"}
        try:
            return self.usage_extractor(log_path)
        except BaseException as exc:  # noqa: BLE001 - 與 extract_usage 同一 fail-soft 邊界
            return {"usage": None, "usage_raw": None, "usage_reason": str(exc)}

    def quota_capability(self) -> dict[str, str]:
        # Usage is not remaining quota.  Until a trusted source is bound, keep
        # this explicitly unknown and let quota consumers decide admission.
        return {"state": self.quota_state, "source": "not-bound"}

    def cancel_contract(self) -> str:
        return self.cancellation_contract

    def timeout_contract_name(self) -> str:
        return self.timeout_contract


# ---------------------------------------------------------------------------
# Descriptor catalog（#835 AC1）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterDescriptor:
    """One validated descriptor entry (data only, no code hooks)."""

    executor: str
    protocol_id: str
    protocol_version: str
    runtime_version: str
    usage_source: str
    quota_state: str
    efforts: tuple[str, ...]
    default_effort: str | None
    model_default_efforts: tuple[tuple[str, str], ...]
    source: str

    def adapter_fields(self) -> dict[str, object]:
        return {
            "protocol_id": self.protocol_id,
            "protocol_version": self.protocol_version,
            "runtime_version": self.runtime_version,
            "usage_source": self.usage_source,
            "quota_state": self.quota_state,
            "efforts": self.efforts,
            "default_effort": self.default_effort,
            "model_default_efforts": self.model_default_efforts,
            "descriptor_source": self.source,
        }


class ExecutionAdapterDescriptorError(ExecutionAdapterError):
    """The descriptor catalog itself is invalid (deployment configuration error).

    與「這次派工的 profile 不合格」不同：它不屬於任何一個 run，Manager 不把它寫成
    per-run needs_human，而是整次派工拒絕並在 descriptor 修正後自然恢復。
    """


def _descriptor_error(source: str, message: str) -> ExecutionAdapterDescriptorError:
    return ExecutionAdapterDescriptorError(f"execution adapter descriptor {source}: {message}")


def _require_text(value: object, *, source: str, locator: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise _descriptor_error(source, f"{locator} must be a non-empty string")
    return value


def _check_keys(
    value: Mapping[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    source: str,
    locator: str,
) -> None:
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise _descriptor_error(source, f"{locator} has unknown keys {unknown}")
    missing = sorted(required - set(value))
    if missing:
        raise _descriptor_error(source, f"{locator} is missing fields {missing}")


def _parse_effort(
    value: object, *, source: str, locator: str
) -> tuple[tuple[str, ...], str | None, tuple[tuple[str, str], ...]]:
    if value is None:
        return (), None, ()
    if not isinstance(value, Mapping):
        raise _descriptor_error(source, f"{locator} must be null or a mapping")
    _check_keys(
        value,
        required=_EFFORT_REQUIRED_KEYS,
        optional=_EFFORT_OPTIONAL_KEYS,
        source=source,
        locator=locator,
    )
    values = value["values"]
    if not isinstance(values, list) or not values:
        raise _descriptor_error(source, f"{locator}.values must be a non-empty list")
    efforts = tuple(
        _require_text(item, source=source, locator=f"{locator}.values[{index}]")
        for index, item in enumerate(values)
    )
    if len(set(efforts)) != len(efforts):
        raise _descriptor_error(source, f"{locator}.values has duplicate effort values")
    default = value["default"]
    if default is not None:
        _require_text(default, source=source, locator=f"{locator}.default")
        if default not in efforts:
            raise _descriptor_error(source, f"{locator}.default {default!r} is not a declared value")
    model_defaults_raw = value.get("model_defaults", {})
    if model_defaults_raw is None:
        model_defaults_raw = {}
    if not isinstance(model_defaults_raw, Mapping):
        raise _descriptor_error(source, f"{locator}.model_defaults must be a mapping")
    model_defaults: list[tuple[str, str]] = []
    for model, effort in model_defaults_raw.items():
        _require_text(model, source=source, locator=f"{locator}.model_defaults key")
        _require_text(effort, source=source, locator=f"{locator}.model_defaults[{model!r}]")
        if effort not in efforts:
            raise _descriptor_error(
                source, f"{locator}.model_defaults[{model!r}] model default {effort!r} is not a declared value"
            )
        model_defaults.append((model, effort))
    return efforts, default, tuple(model_defaults)


def parse_adapter_catalog(payload: object, *, source: str) -> dict[str, AdapterDescriptor]:
    """Strictly parse one descriptor document (packaged or overlay)."""

    if not isinstance(payload, Mapping):
        raise _descriptor_error(source, "document must be a mapping")
    if "schema_version" not in payload:
        raise _descriptor_error(source, "schema_version is missing")
    version = payload["schema_version"]
    if type(version) is not int or version != ADAPTER_CATALOG_SCHEMA_VERSION:
        raise _descriptor_error(source, f"unsupported schema_version {version!r}")
    _check_keys(payload, required=_CATALOG_KEYS, source=source, locator="document")
    adapters = payload["adapters"]
    if not isinstance(adapters, Mapping) or not adapters:
        raise _descriptor_error(source, "adapters must be a non-empty mapping")
    parsed: dict[str, AdapterDescriptor] = {}
    for executor, entry in adapters.items():
        _require_text(executor, source=source, locator="adapter name")
        locator = f"adapters.{executor}"
        if not isinstance(entry, Mapping):
            raise _descriptor_error(source, f"{locator} must be a mapping")
        _check_keys(entry, required=_ADAPTER_DESCRIPTOR_KEYS, source=source, locator=locator)
        quota_state = _require_text(entry["quota_state"], source=source, locator=f"{locator}.quota_state")
        if quota_state not in _QUOTA_STATES:
            raise _descriptor_error(source, f"{locator}.quota_state {quota_state!r} is invalid")
        efforts, default, model_defaults = _parse_effort(
            entry["effort"], source=source, locator=f"{locator}.effort"
        )
        parsed[executor] = AdapterDescriptor(
            executor=executor,
            protocol_id=_require_text(entry["protocol_id"], source=source, locator=f"{locator}.protocol_id"),
            protocol_version=_require_text(
                entry["protocol_version"], source=source, locator=f"{locator}.protocol_version"
            ),
            runtime_version=_require_text(
                entry["runtime_version"], source=source, locator=f"{locator}.runtime_version"
            ),
            usage_source=_require_text(entry["usage_source"], source=source, locator=f"{locator}.usage_source"),
            quota_state=quota_state,
            efforts=efforts,
            default_effort=default,
            model_default_efforts=model_defaults,
            source=source,
        )
    return parsed


def packaged_catalog_path() -> Path:
    return Path(__file__).with_name("data") / ADAPTER_CATALOG_FILENAME


def overlay_catalog_path(config_root: str | Path | None = None) -> Path:
    if config_root is None:
        from paulsha_cortex.config import paths

        config_root = paths.project_config_root()
    return Path(config_root) / ADAPTER_CATALOG_FILENAME


def _read_catalog(path: Path) -> dict[str, AdapterDescriptor]:
    from .._yaml import YAMLError, safe_load

    source = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _descriptor_error(source, f"unreadable: {exc}") from exc
    try:
        payload = safe_load(text)
    except YAMLError as exc:
        raise _descriptor_error(source, f"malformed YAML: {exc}") from exc
    return parse_adapter_catalog(payload, source=source)


def _validate_adapter(adapter: ExecutionAdapter) -> None:
    """Check that data and trusted code agree before the adapter is usable."""

    if adapter.trusted_argv_builder() is None:
        raise ExecutionAdapterError(
            f"adapter {adapter.executor} has no trusted argv builder; "
            "descriptors cannot introduce executable runtimes"
        )
    if adapter.quota_state not in _QUOTA_STATES:
        raise ExecutionAdapterError("adapter quota capability state is invalid")
    if adapter.terminal_contract not in SUPPORTED_TERMINAL_CONTRACTS:
        raise ExecutionAdapterError(
            f"adapter {adapter.executor} terminal contract is not implemented: {adapter.terminal_contract}"
        )
    if adapter.cancellation_contract not in SUPPORTED_CANCELLATION_CONTRACTS:
        raise ExecutionAdapterError(
            f"adapter {adapter.executor} cancel contract is not implemented: {adapter.cancellation_contract}"
        )
    if adapter.timeout_contract not in SUPPORTED_TIMEOUT_CONTRACTS:
        raise ExecutionAdapterError(
            f"adapter {adapter.executor} timeout contract is not implemented: {adapter.timeout_contract}"
        )
    if len(set(adapter.efforts)) != len(adapter.efforts):
        raise ExecutionAdapterError(f"adapter {adapter.executor} has duplicate effort values")
    for effort in (adapter.default_effort, *(value for _, value in adapter.model_default_efforts)):
        if effort is not None and effort not in adapter.efforts:
            raise ExecutionAdapterError(
                f"adapter {adapter.executor} default effort {effort!r} is not declared"
            )
    if adapter.efforts and not adapter.accepts("effort"):
        raise ExecutionAdapterError(
            f"adapter {adapter.executor} cannot express native effort: its trusted argv "
            "builder has no effort parameter"
        )


#: 程式碼登記的 adapter（新 runtime 或受信任替換）。descriptor 只能描述已在此或
#: launcher argv builder 表中的 executor。
_REGISTERED_ADAPTERS: dict[str, ExecutionAdapter] = {}
_PACKAGED_DESCRIPTORS: dict[str, AdapterDescriptor] | None = None
_CATALOG_CACHE: tuple[tuple[object, ...], dict[str, ExecutionAdapter]] | None = None
_CATALOG_LOCK = threading.Lock()


def _packaged_descriptors() -> dict[str, AdapterDescriptor]:
    global _PACKAGED_DESCRIPTORS
    if _PACKAGED_DESCRIPTORS is None:
        _PACKAGED_DESCRIPTORS = _read_catalog(packaged_catalog_path())
    return _PACKAGED_DESCRIPTORS


def _file_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _descriptor_error(str(path), f"unreadable: {exc}") from exc
    return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)


def load_adapter_catalog(config_root: str | Path | None = None) -> dict[str, ExecutionAdapter]:
    """Build the adapter catalog: packaged defaults ← code registrations ← overlay.

    - packaged ``data/execution-adapters.yaml`` 是內建預設；
    - :func:`register_adapter` 登記的受信任程式碼可新增 runtime 或替換條目；
    - config root 的 ``execution-adapters.yaml`` overlay 以同名條目整筆取代資料欄位
      （保留程式碼 hook），不能新增沒有受信任程式碼的 adapter。

    任何一層不合法都整體拒收（不退回 packaged 預設）。
    """

    adapters: dict[str, ExecutionAdapter] = {}
    for executor, descriptor in _packaged_descriptors().items():
        adapters[executor] = ExecutionAdapter(executor, **descriptor.adapter_fields())
    adapters.update(_REGISTERED_ADAPTERS)
    overlay_path = overlay_catalog_path(config_root)
    if _file_signature(overlay_path) is not None:
        for executor, descriptor in _read_catalog(overlay_path).items():
            base = adapters.get(executor)
            if base is None:
                raise _descriptor_error(
                    str(overlay_path),
                    f"adapter {executor!r} has no trusted code; descriptors cannot "
                    "introduce executable runtimes",
                )
            adapters[executor] = _replace(base, **descriptor.adapter_fields())
    for adapter in adapters.values():
        try:
            _validate_adapter(adapter)
        except ExecutionAdapterError as exc:
            raise _descriptor_error(adapter.descriptor_source, str(exc)) from exc
    return adapters


def reload_adapter_catalog() -> None:
    """Drop cached catalog state (process restart semantics; tests use it)."""

    global _CATALOG_CACHE, _PACKAGED_DESCRIPTORS
    with _CATALOG_LOCK:
        _CATALOG_CACHE = None
        _PACKAGED_DESCRIPTORS = None


def adapter_catalog() -> Mapping[str, ExecutionAdapter]:
    """Active catalog; re-read when the overlay file or code registrations change."""

    global _CATALOG_CACHE
    try:
        overlay_path = overlay_catalog_path()
    except ValueError as exc:
        raise _descriptor_error("config root", f"unresolvable: {exc}") from exc
    key = (
        str(overlay_path),
        _file_signature(overlay_path),
        tuple(sorted((name, id(adapter)) for name, adapter in _REGISTERED_ADAPTERS.items())),
        id(_REGISTERED_ADAPTERS),
    )
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE
        if cached is not None and cached[0] == key:
            return cached[1]
    catalog = load_adapter_catalog(overlay_path.parent)
    with _CATALOG_LOCK:
        _CATALOG_CACHE = (key, catalog)
    return catalog


def adapter_for(executor: str) -> ExecutionAdapter:
    adapter = adapter_catalog().get(executor)
    if adapter is None:
        raise ExecutionAdapterError(f"unsupported adapter: {executor}")
    return adapter


def register_adapter(adapter: ExecutionAdapter, *, replace: bool = False) -> None:
    """Register trusted adapter code; descriptors alone cannot extend this map."""

    if not isinstance(adapter, ExecutionAdapter) or not adapter.executor:
        raise ExecutionAdapterError("invalid trusted execution adapter")
    known = set(_REGISTERED_ADAPTERS) | set(_packaged_descriptors())
    if adapter.executor in known and not replace:
        raise ExecutionAdapterError(f"adapter already registered: {adapter.executor}")
    _validate_adapter(adapter)
    _REGISTERED_ADAPTERS[adapter.executor] = adapter


def _tagged(value: object, *, unknown_reason: str) -> dict[str, object]:
    if value is _UNKNOWN:
        return {"state": "unknown", "reason": unknown_reason}
    return {"state": "known", "value": value}


_UNKNOWN = object()


def _profile_payload(
    *,
    plane: str,
    descriptor: schema.ExecutionProfileDescriptor,
    effort: object,
    loadout: object,
    toolset: object,
    sandbox: object,
    permissions: object,
    toolchain: object,
    requirements: Mapping[str, object],
    metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    role = requirements.get("role", _UNKNOWN)
    minimum_quality = requirements.get("minimum_quality", _UNKNOWN)
    pin = requirements.get("pin", _UNKNOWN)
    independence = requirements.get("independence", _UNKNOWN)
    return {
        "schema_version": 1,
        "plane": plane,
        "conditions": {
            "adapter": _tagged(descriptor.adapter, unknown_reason="adapter-not-observed"),
            "model": _tagged(descriptor.model, unknown_reason="model-not-observed"),
            "effort": effort,
            "loadout": _tagged(loadout, unknown_reason="loadout-not-observed"),
            "toolset": _tagged(toolset, unknown_reason="toolset-not-observed"),
            "sandbox": _tagged(sandbox, unknown_reason="sandbox-not-observed"),
            "permissions": _tagged(permissions, unknown_reason="permissions-not-observed"),
            "toolchain": _tagged(toolchain, unknown_reason="toolchain-not-observed"),
        },
        "requirements": {
            "role": _tagged(role, unknown_reason="role-not-resolved"),
            "minimum_quality": _tagged(minimum_quality, unknown_reason="quality-not-specified"),
            "pin": _tagged(pin, unknown_reason="pin-not-specified"),
            "independence": _tagged(independence, unknown_reason="independence-not-resolved"),
        },
        "provenance": [],
        "metadata": dict(metadata or {}),
    }


def _unknown_effort(adapter: ExecutionAdapter) -> dict[str, str]:
    if not adapter.efforts:
        return {"state": "not_applicable"}
    return {"state": "unknown", "reason": "native-effort-not-observed"}


@dataclass(frozen=True)
class ExecutionProfileBinding:
    """Versioned sibling binding for requested/resolved/observed profile rows."""

    descriptor: schema.ExecutionProfileDescriptor
    requested: schema.ExecutionProfile
    resolved: schema.ExecutionProfile
    observed: schema.ExecutionProfile
    request_key: str
    resolved_key: str
    actual_key: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PROFILE_BINDING_SCHEMA,
            "descriptor": self.descriptor.to_dict(),
            "requested": self.requested.to_dict(),
            "resolved": self.resolved.to_dict(),
            "observed": self.observed.to_dict(),
            "request_key": self.request_key,
            "resolved_key": self.resolved_key,
            "actual_key": self.actual_key,
        }


def _binding(
    descriptor: schema.ExecutionProfileDescriptor,
    requested_payload: Mapping[str, object],
    resolved_payload: Mapping[str, object],
    observed_payload: Mapping[str, object],
) -> ExecutionProfileBinding:
    requested = schema.parse_profile(requested_payload, descriptor)
    resolved = schema.parse_profile(resolved_payload, descriptor)
    observed = schema.parse_profile(observed_payload, descriptor)
    if (requested.plane, resolved.plane, observed.plane) != (
        "requested", "resolved", "observed"
    ):
        raise ExecutionAdapterError("profile binding planes are inconsistent")
    return ExecutionProfileBinding(
        descriptor=descriptor,
        requested=requested,
        resolved=resolved,
        observed=observed,
        request_key=schema.profile_key(requested),
        resolved_key=schema.profile_key(resolved),
        actual_key=schema.actual_condition_key(observed),
    )


def _versioned_refs(values: object) -> list[dict[str, str]]:
    if values is _UNKNOWN:
        return []
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ExecutionAdapterError("tool and permission conditions must be sequences")
    return sorted(
        (dict(item) for item in values), key=lambda item: (item["id"], item["version"])
    )


def _safe_tool_ids(values: object) -> list[dict[str, str]]:
    """Project grants to capability names; never put host paths in shared keys."""

    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    safe: set[str] = set()
    for value in values:
        text = str(value)
        if text.startswith("write("):
            safe.add("write-scoped")
        elif text.startswith("shell("):
            safe.add("shell-scoped")
        elif text in {"Bash", "Read", "Edit", "Write", "Glob", "Grep"}:
            safe.add(text.lower())
        elif text and "/" not in text and "\\" not in text:
            safe.add(text.lower())
    return [{"id": value, "version": "1"} for value in sorted(safe)]


def _launcher_conditions(
    adapter: ExecutionAdapter,
    persona: str,
    effort: object,
    launch_contract: Mapping[str, object],
    descriptor: schema.ExecutionProfileDescriptor,
) -> dict[str, object]:
    mode = launch_contract.get("sandbox")
    if mode is None:
        if persona == "planner":
            mode = "read-only"
        elif persona == "reviewer":
            mode = "review-only"
        else:
            mode = "workspace-write"
    if not isinstance(mode, str) or not mode:
        raise ExecutionAdapterError("resolved sandbox mode is invalid")
    tools = _safe_tool_ids(launch_contract.get("tools", ()))
    permission_names = set()
    if mode in {"read-only", "review-only", "write-forbidden"}:
        permission_names.add(mode)
    else:
        permission_names.add("workspace-write")
    if launch_contract.get("allow_unsafe") is True:
        permission_names.add("unsafe-explicit-opt-in")
    permissions = [{"id": name, "version": "1"} for name in sorted(permission_names)]
    conditions = {
        "effort": effort,
        "loadout": {"id": persona, "version": str(launch_contract.get("loadout_version", "1"))},
        "toolset": tools,
        "sandbox": {"id": mode, "version": str(launch_contract.get("sandbox_version", "1"))},
        "permissions": permissions,
        "toolchain": {
            "id": adapter.executor,
            "version": str(launch_contract.get("toolchain_version", adapter.runtime_version)),
        },
    }
    return conditions


def resolve_profile(
    identity: object,
    persona: str,
    *,
    effort: str | None = None,
    descriptor: object | None = None,
    launch_contract: Mapping[str, object] | None = None,
    requirements: Mapping[str, object] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> ExecutionProfileBinding:
    """Resolve one identity into requested, resolved, and unknown observed rows."""

    role = _PERSONA_ROLE.get(persona)
    if role is None:
        raise ExecutionAdapterError(f"unknown workflow persona: {persona}")
    executor = getattr(identity, "executor", None)
    model_id = getattr(identity, "model_id", None)
    if not isinstance(executor, str) or not executor or not isinstance(model_id, str) or not model_id:
        raise ExecutionAdapterError("execution identity lacks executor/model")
    adapter = adapter_for(executor)
    if descriptor is None:
        descriptor_payload: object = {
            "schema_version": 1,
            "id": f"{adapter.adapter_id}:{model_id}",
            "adapter": adapter.descriptor_fields(),
            "model": {"id": model_id, "revision": "unreported"},
            "effort_grammar": adapter.effort_grammar(),
            "provenance": [],
            "metadata": {},
        }
    else:
        descriptor_payload = descriptor
    parsed_descriptor = schema.parse_descriptor(descriptor_payload)
    if parsed_descriptor.adapter != adapter.descriptor_fields():
        raise ExecutionAdapterError("descriptor adapter does not match selected identity")
    if parsed_descriptor.model.get("id") != model_id:
        raise ExecutionAdapterError("descriptor model does not match selected identity")
    if effort is not None and not adapter.efforts:
        raise ExecutionAdapterError(f"unsupported effort for adapter {executor}")

    explicit_effort: object
    if effort is None:
        explicit_effort = _UNKNOWN
    else:
        explicit_effort = effort
    # Descriptor 宣告的 model 預設優先、其次 adapter 預設；argv builder 以同一個
    # `default_effort_for()` 決定實際發出的 effort，兩者不會各寫一份而漂移。
    resolved_effort = effort or adapter.default_effort_for(model_id)
    effort_known = (
        {"state": "known", "value": resolved_effort}
        if resolved_effort is not None
        else _unknown_effort(adapter)
    )
    if not adapter.efforts:
        effort_known = {"state": "not_applicable"}
    requested_effort = (
        {"state": "known", "value": explicit_effort}
        if explicit_effort is not _UNKNOWN
        else _unknown_effort(adapter)
    )
    requested_conditions = {
        "effort": requested_effort,
        "loadout": _UNKNOWN,
        "toolset": _UNKNOWN,
        "sandbox": _UNKNOWN,
        "permissions": _UNKNOWN,
        "toolchain": _UNKNOWN,
    }
    requested_adapter = _UNKNOWN
    requested_model = _UNKNOWN
    supplied_requirements = dict(requirements or {})
    explicit_pin = supplied_requirements.get("pin")
    if explicit_pin is not None:
        requested_adapter = parsed_descriptor.adapter
        requested_model = parsed_descriptor.model
    requested_payload = _profile_payload(
        plane="requested",
        descriptor=parsed_descriptor,
        effort=requested_conditions["effort"],
        loadout=requested_conditions["loadout"],
        toolset=requested_conditions["toolset"],
        sandbox=requested_conditions["sandbox"],
        permissions=requested_conditions["permissions"],
        toolchain=requested_conditions["toolchain"],
        requirements={
            "role": role,
            "minimum_quality": supplied_requirements.get("minimum_quality", _UNKNOWN),
            "pin": explicit_pin,
            "independence": supplied_requirements.get("independence", _UNKNOWN),
        },
        metadata=metadata,
    )
    requested_payload["conditions"]["adapter"] = _tagged(
        requested_adapter, unknown_reason="adapter-preference-not-pinned"
    )
    requested_payload["conditions"]["model"] = _tagged(
        requested_model, unknown_reason="model-preference-not-pinned"
    )

    contract = dict(launch_contract or {})
    conditions = _launcher_conditions(adapter, persona, effort_known, contract, parsed_descriptor)
    if parsed_descriptor.effort_grammar["type"] == "none":
        conditions["effort"] = {"state": "not_applicable"}
    requirements_payload = {
        "role": role,
        "minimum_quality": supplied_requirements.get("minimum_quality", _UNKNOWN),
        "pin": explicit_pin,
        "independence": supplied_requirements.get("independence", getattr(identity, "independence_domain", _UNKNOWN)),
    }
    resolved_payload = _profile_payload(
        plane="resolved",
        descriptor=parsed_descriptor,
        effort=conditions["effort"],
        loadout=conditions["loadout"],
        toolset=conditions["toolset"],
        sandbox=conditions["sandbox"],
        permissions=conditions["permissions"],
        toolchain=conditions["toolchain"],
        requirements=requirements_payload,
        metadata=metadata,
    )

    unknown = {"state": "unknown", "reason": "no-trusted-runtime-observation"}
    observed_payload = {
        "schema_version": 1,
        "plane": "observed",
        "conditions": {name: (dict(unknown) if name != "effort" or adapter.efforts else {"state": "not_applicable"}) for name in (
            "adapter", "model", "effort", "loadout", "toolset", "sandbox", "permissions", "toolchain"
        )},
        "requirements": resolved_payload["requirements"],
        "provenance": [],
        "metadata": dict(metadata or {}),
    }
    try:
        return _binding(parsed_descriptor, requested_payload, resolved_payload, observed_payload)
    except schema.ExecutionProfileError as exc:
        if effort is not None:
            raise ExecutionAdapterError("unsupported effort for selected descriptor") from exc
        raise


def load_profile_binding(payload: object) -> ExecutionProfileBinding:
    """Strictly load a binding; legacy absence is handled by the containing record."""

    if not isinstance(payload, Mapping):
        raise ExecutionAdapterError("profile binding must be an object")
    expected = {
        "schema_version", "descriptor", "requested", "resolved", "observed",
        "request_key", "resolved_key", "actual_key",
    }
    if set(payload) != expected:
        raise ExecutionAdapterError("profile binding descriptor/fields are incomplete")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != PROFILE_BINDING_SCHEMA:
        raise ExecutionAdapterError("unsupported profile binding schema")
    try:
        descriptor = schema.parse_descriptor(payload["descriptor"])
    except schema.ExecutionProfileError as exc:
        raise ExecutionAdapterError("profile binding descriptor is invalid") from exc
    requested = schema.parse_profile(payload["requested"], descriptor)
    resolved = schema.parse_profile(payload["resolved"], descriptor)
    observed = schema.parse_profile(payload["observed"], descriptor)
    if (requested.plane, resolved.plane, observed.plane) != ("requested", "resolved", "observed"):
        raise ExecutionAdapterError("profile binding planes are inconsistent")
    request_key = schema.profile_key(requested)
    resolved_key = schema.profile_key(resolved)
    actual_key = schema.actual_condition_key(observed)
    if payload.get("request_key") != request_key or payload.get("resolved_key") != resolved_key:
        raise ExecutionAdapterError("profile binding key mismatch")
    if payload.get("actual_key") != actual_key:
        raise ExecutionAdapterError("profile binding actual key mismatch")
    return ExecutionProfileBinding(descriptor, requested, resolved, observed, request_key, resolved_key, actual_key)


def record_observed(
    binding: ExecutionProfileBinding,
    observation: Mapping[str, object],
) -> ExecutionProfileBinding:
    """Accept observations only when a trusted producer binds exact resolved key."""

    if (
        observation.get("verified") is not True
        or observation.get("profile_key") != binding.resolved_key
        or not isinstance(observation.get("source"), str)
        or not observation.get("source")
        or not isinstance(observation.get("conditions"), Mapping)
    ):
        return binding
    payload = {
        **binding.observed.to_dict(),
        "conditions": dict(observation["conditions"]),
        "provenance": [{"kind": "observer", "ref": str(observation["source"])}],
    }
    observed = schema.parse_profile(payload, binding.descriptor)
    return ExecutionProfileBinding(
        binding.descriptor,
        binding.requested,
        binding.resolved,
        observed,
        binding.request_key,
        binding.resolved_key,
        schema.actual_condition_key(observed),
    )


def require_workflow_role(persona: str) -> str:
    """Return the execution role for ``persona``; unknown roles never default to build."""

    role = _PERSONA_ROLE.get(persona)
    if role is None:
        raise ExecutionAdapterError(f"unknown workflow persona: {persona}")
    return role


@dataclass(frozen=True)
class _CapabilityWaivedIdentity:
    """Identity view for lanes whose contract does not filter by capability."""

    executor: object
    model_id: object
    capabilities: tuple[str, ...]


def trust_root_compatibility(
    persona: str,
    identity: object,
    launcher: object | None,
    *,
    require_role_capability: bool = True,
) -> tuple[bool, str | None]:
    """Evaluate the Trust Root launch contract for one selected identity.

    The authority is the existing :mod:`model_resolution` compatibility
    predicate (launcher profile + Trust Root toolchain grant + credential grant
    for the persona's principal).  It is active only when the Trust Root runner
    contract is deployed (``PSC_JOB_RUNNER`` not ``direct``); direct mode keeps
    the historical operator-overlay path and has no Trust Root contract to
    violate.  An unresolvable runner configuration fails closed.

    ``require_role_capability=False`` waives only the capability layer for
    lanes that never filtered by capability (slice lane, #381); launcher,
    toolchain and credential layers are still enforced.
    """

    from . import model_resolution

    try:
        checker = model_resolution.compatibility_checker_for(persona)
    except ValueError as exc:
        return False, f"Trust Root runner contract is unresolvable: {exc}"
    if checker is None:
        return True, None
    subject = identity
    if not require_role_capability:
        subject = _CapabilityWaivedIdentity(
            executor=getattr(identity, "executor", None),
            model_id=getattr(identity, "model_id", None),
            capabilities=(model_resolution.role_for_persona(persona),),
        )
    try:
        model_resolution.validate_identity_compatibility(persona, subject, launcher=launcher)
    except ValueError as exc:
        return False, str(exc)
    return True, None


def validate_dispatch_requirements(
    binding: ExecutionProfileBinding,
    *,
    identity: object,
    trust_root_valid: bool,
    trust_root_reason: str | None = None,
    qualification: Mapping[str, object] | None = None,
    qualification_required: bool = False,
    builder_domains: Sequence[str] = (),
    quota: Mapping[str, object] | None = None,
    require_role_capability: bool = True,
) -> None:
    """Recheck profile-bound hard gates at the final pre-spawn boundary.

    Quota is accepted only for observability; it is intentionally excluded from
    the permission calculation.

    ``trust_root_valid`` is mandatory: production callers obtain it from
    :func:`trust_root_compatibility` (the shared Trust Root predicate) instead
    of relying on an always-true default.

    ``require_role_capability=False`` 只給既有契約本來就不看 capability 宣告的
    入口（slice lane 的 spec 明示 executor/model_id）；unknown role、pin、
    independence、Trust Root 與 qualification 仍照常硬擋。
    """

    del quota
    executor = getattr(identity, "executor", None)
    model_id = getattr(identity, "model_id", None)
    if (executor, model_id) != (
        binding.resolved.conditions["adapter"]["value"]["id"].removesuffix("-cli"),
        binding.resolved.conditions["model"]["value"]["id"],
    ):
        raise ExecutionAdapterError("selected identity does not match resolved profile")
    role_value = binding.resolved.requirements["role"]
    role = role_value.get("value") if role_value.get("state") == "known" else None
    if role not in _ROLE_CAPABILITY.values():
        raise ExecutionAdapterError("unknown role cannot enter the execution candidate pool")
    if require_role_capability and role not in getattr(identity, "capabilities", ()):
        raise ExecutionAdapterError(f"identity lacks required role capability: {role}")
    pin = binding.resolved.requirements["pin"]
    if pin.get("state") == "known" and pin.get("value") is not None:
        expected = pin["value"]
        if not isinstance(expected, Mapping) or (expected.get("executor"), expected.get("model_id")) != (executor, model_id):
            raise ExecutionAdapterError("explicit model pin does not match resolved identity")
    if role == "review" and getattr(identity, "independence_domain", None) in set(builder_domains):
        raise ExecutionAdapterError("reviewer independence domain matches a builder")
    if trust_root_valid is not True:
        detail = f": {trust_root_reason}" if trust_root_reason else ""
        raise ExecutionAdapterError(f"Trust Root profile is not valid{detail}")
    if qualification_required:
        if not isinstance(qualification, Mapping):
            raise ExecutionAdapterError("exact-profile qualification is unknown")
        if (
            qualification.get("state") != "approved"
            or qualification.get("profile_key") != binding.resolved_key
            or qualification.get("role") != role
            or qualification.get("coverage") != "complete"
            or not isinstance(qualification.get("receipt"), str)
            or not qualification.get("receipt")
            or qualification.get("revoked") is True
        ):
            raise ExecutionAdapterError("exact-profile qualification is insufficient")


def validate_profile_for_launch(
    binding: ExecutionProfileBinding,
    *,
    executor: str,
    model: str | None,
    effort: str | None,
    sandbox_mode: str | None = None,
) -> None:
    """Fail before argv/Popen when launcher inputs contradict the resolved profile."""

    resolved = binding.resolved
    adapter_value = resolved.conditions["adapter"]
    model_value = resolved.conditions["model"]
    if adapter_value.get("state") != "known" or model_value.get("state") != "known":
        raise ExecutionAdapterError("resolved adapter/model must be known before launch")
    if adapter_value["value"]["id"] != adapter_for(executor).adapter_id:
        raise ExecutionAdapterError("launcher adapter does not match profile")
    if model_value["value"]["id"] != model:
        raise ExecutionAdapterError("launcher model does not match profile")
    adapter = adapter_for(executor)
    effort_value = resolved.conditions["effort"]
    if effort is not None and not adapter.efforts:
        raise ExecutionAdapterError(f"unsupported effort for adapter {executor}")
    if effort_value.get("state") == "known" and effort is not None and effort_value.get("value") != effort:
        raise ExecutionAdapterError("launcher effort does not match resolved profile")
    # argv builder 依目前 descriptor 發出的 effort 必須就是 profile 解析出的那個；
    # descriptor 在 resolve 與 launch 之間被改動（overlay 熱讀）時在 spawn 前拒絕。
    emitted = adapter.launch_effort(model, effort) if adapter.efforts else None
    if effort_value.get("state") == "known":
        if emitted != effort_value.get("value"):
            raise ExecutionAdapterError("launcher effort does not match resolved profile")
    elif emitted is not None:
        raise ExecutionAdapterError("launcher effort does not match resolved profile")
    if sandbox_mode is not None:
        sandbox_value = resolved.conditions["sandbox"]
        if sandbox_value.get("state") != "known" or sandbox_value.get("value", {}).get("id") != sandbox_mode:
            raise ExecutionAdapterError("launcher sandbox does not match resolved profile")


def make_launcher_profile(
    launcher: object,
    identity: object,
    persona: str,
    *,
    requirements: Mapping[str, object] | None = None,
) -> ExecutionProfileBinding:
    """Build profile facts from the already-specialized production launcher."""

    contract = {
        "read_only": bool(getattr(launcher, "_read_only", False)),
        "review_only": bool(getattr(launcher, "_review_only", False)),
        "commit_required": bool(getattr(launcher, "_commit_required", False)),
        "write_forbidden": bool(getattr(launcher, "_write_forbidden", False)),
        "allow_unsafe": bool(getattr(launcher, "_allow_unsafe", False)),
        "effort": getattr(launcher, "_effort", None),
        "tools": getattr(launcher, "_effective_tools", ()) or (),
    }
    if contract["review_only"]:
        contract["sandbox"] = "review-only"
    elif contract["read_only"]:
        contract["sandbox"] = "read-only"
    elif contract["write_forbidden"]:
        contract["sandbox"] = "write-forbidden"
    elif contract["allow_unsafe"]:
        contract["sandbox"] = "unsafe-opt-in"
    else:
        contract["sandbox"] = "workspace-write"
    if contract["commit_required"]:
        contract["tools"] = tuple(contract["tools"]) + ("git-commit",)
    effort = contract["effort"]
    return resolve_profile(
        identity,
        persona,
        effort=effort,
        launch_contract=contract,
        requirements=requirements,
    )


def bind_launcher_profile(launcher: object, binding: ExecutionProfileBinding) -> object:
    binder = getattr(launcher, "with_execution_profile", None)
    if callable(binder):
        return binder(binding)
    # Injected test/remote launchers are not executable adapters.  Keep them
    # usable while the workflow still persists the binding for audit.
    return launcher


def profile_report_consumer(
    payload: object,
    *,
    expected_profile_key: str,
    source_revision: str,
    source_digest: str,
    envelope_context: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """Validate a profile-aware PatchMUD report and optionally map its envelope view.

    Report acceptance is provenance only.  The optional envelope mapping is a
    projection and never creates qualification.

    PatchMUD report v2 本身不帶 producer revision；內容完整性由 producer 寫入的
    ``report_fingerprint``（去掉 ``generated_at`` 與自身後的 canonical JSON
    SHA-256，``ensure_ascii=False``）承擔。呼叫端以 ``source_digest`` 釘住該
    fingerprint、以 ``source_revision`` 記錄產出報表的 PatchMUD revision（只作
    provenance，報表內無可比對欄位）。
    """

    if not isinstance(payload, Mapping):
        raise ExecutionAdapterError("profile report must be an object")
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 2
    ):
        raise ExecutionAdapterError(
            "unsupported profile report schema; only PatchMUD v2 is accepted"
        )
    fingerprint = payload.get("report_fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(
        r"sha256:[0-9a-f]{64}", fingerprint
    ) is None:
        raise ExecutionAdapterError("profile report fingerprint is missing or malformed")
    stable = {
        key: value
        for key, value in payload.items()
        if key not in ("generated_at", "report_fingerprint")
    }
    try:
        canonical = json.dumps(
            stable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExecutionAdapterError("profile report is not finite JSON") from exc
    if "sha256:" + sha256(canonical).hexdigest() != fingerprint:
        raise ExecutionAdapterError("profile report fingerprint does not match content")
    if (
        not isinstance(source_revision, str)
        or not source_revision.strip()
        or not isinstance(source_digest, str)
        or source_digest.removeprefix("sha256:") != fingerprint.removeprefix("sha256:")
    ):
        raise ExecutionAdapterError("profile report source revision/digest mismatch")
    leaderboards = payload.get("leaderboards")
    matching_profile_row = False
    if isinstance(leaderboards, Mapping):
        for board in leaderboards.values():
            if not isinstance(board, Mapping):
                continue
            rows = board.get("rows")
            if not isinstance(rows, list):
                continue
            if any(
                isinstance(row, Mapping)
                and row.get("profile_id") == expected_profile_key
                for row in rows
            ):
                matching_profile_row = True
                break
    if not matching_profile_row:
        raise ExecutionAdapterError("profile report does not match exact resolved profile")
    if payload.get("pricing") is not None and not isinstance(payload.get("pricing"), Mapping):
        raise ExecutionAdapterError("profile report pricing metadata must be an object")
    result = dict(payload)
    result["source_revision"] = source_revision
    if envelope_context is not None:
        expected_context = {
            "executor", "model_id", "persona", "deck", "patchmud_version",
            "role", "benchmark_type", "deck_digest", "evaluator_revision",
        }
        if set(envelope_context) != expected_context:
            raise ExecutionAdapterError("profile report envelope context is incomplete")
        from .envelope_mapping import EnvelopeMappingError, map_report_to_envelope

        try:
            result["envelope_mapping"] = map_report_to_envelope(
                payload,
                executor=envelope_context["executor"],
                model_id=envelope_context["model_id"],
                persona=envelope_context["persona"],
                deck=envelope_context["deck"],
                patchmud_version=envelope_context["patchmud_version"],
                execution_profile_key=expected_profile_key,
                role=envelope_context["role"],
                benchmark_type=envelope_context["benchmark_type"],
                deck_digest=envelope_context["deck_digest"],
                evaluator_revision=envelope_context["evaluator_revision"],
            )
        except (EnvelopeMappingError, TypeError, ValueError) as exc:
            raise ExecutionAdapterError("profile report envelope mapping failed") from exc
    return result
