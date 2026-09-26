"""Trusted adapters joining execution profiles to existing launcher contracts.

Descriptors are data only.  Runtime code is registered explicitly by this
module; a descriptor can never import or execute provider supplied code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Callable

from . import execution_profile as schema


PROFILE_BINDING_SCHEMA = 1
_PERSONA_ROLE = {"planner": "planning", "builder": "build", "reviewer": "review"}
_ROLE_CAPABILITY = dict(_PERSONA_ROLE)


class ExecutionAdapterError(ValueError):
    """A selected execution profile cannot be consumed safely."""


@dataclass(frozen=True)
class ExecutionAdapter:
    """One trusted implementation of an executor protocol.

    The launcher remains the owner of process isolation, terminal lifecycle,
    and cancellation.  These fields describe how the profile is expressed and
    which existing producer is responsible for each observation.
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

    def build_argv(self, kwargs: Mapping[str, object]) -> list[str]:
        """Delegate to the existing trusted argv builder for this executor."""

        from .launcher import _ARGV_BUILDERS

        builder = _ARGV_BUILDERS.get(self.executor)
        if builder is None:
            raise ExecutionAdapterError(f"unsupported adapter: {self.executor}")
        return builder(**dict(kwargs))

    def parse_terminal(self, payload: object) -> object:
        """Consume the shared terminal envelope without provider-specific rules."""

        from .terminal_contract import validate_envelope

        return validate_envelope(payload)

    def parse_usage(self, log_path: str | None) -> dict[str, Any]:
        from .usage_extractors import extract_usage

        return extract_usage(self.executor, log_path)

    def quota_capability(self) -> dict[str, str]:
        # Usage is not remaining quota.  Until a trusted source is bound, keep
        # this explicitly unknown and let quota consumers decide admission.
        return {"state": self.quota_state, "source": "not-bound"}

    def cancel_contract(self) -> str:
        return self.cancellation_contract

    def timeout_contract_name(self) -> str:
        return self.timeout_contract


_ADAPTERS: dict[str, ExecutionAdapter] = {
    "copilot": ExecutionAdapter(
        "copilot", "github-copilot-cli", "1", "cortex-adapter-v1",
        efforts=("low", "medium", "high", "xhigh"), default_effort="xhigh",
        usage_source="copilot-jsonl",
    ),
    "claude": ExecutionAdapter(
        "claude", "claude-code-cli", "1", "cortex-adapter-v1",
        usage_source="claude-jsonl",
    ),
    "codex": ExecutionAdapter(
        "codex", "openai-codex-cli", "1", "cortex-adapter-v1",
        efforts=("low", "medium", "high", "xhigh", "max"),
        usage_source="codex-jsonl",
    ),
    "agy": ExecutionAdapter(
        "agy", "antigravity-cli", "1", "cortex-adapter-v1",
        usage_source="agy-jsonl",
    ),
    "cg": ExecutionAdapter(
        "cg", "copilot-gateway-cli", "1", "cortex-adapter-v1",
        efforts=("low", "medium", "high", "xhigh"), default_effort="medium",
        usage_source="cg-jsonl",
    ),
}


def adapter_for(executor: str) -> ExecutionAdapter:
    adapter = _ADAPTERS.get(executor)
    if adapter is None:
        raise ExecutionAdapterError(f"unsupported adapter: {executor}")
    return adapter


def register_adapter(adapter: ExecutionAdapter, *, replace: bool = False) -> None:
    """Register trusted adapter code; descriptors alone cannot extend this map."""

    if not isinstance(adapter, ExecutionAdapter) or not adapter.executor:
        raise ExecutionAdapterError("invalid trusted execution adapter")
    if adapter.executor in _ADAPTERS and not replace:
        raise ExecutionAdapterError(f"adapter already registered: {adapter.executor}")
    if adapter.quota_state not in {"supported", "unsupported", "unknown"}:
        raise ExecutionAdapterError("adapter quota capability state is invalid")
    _ADAPTERS[adapter.executor] = adapter


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
    resolved_effort = effort or adapter.default_effort
    if executor == "codex" and effort is None:
        # Keep the existing Codex mapping as the single source of truth; do
        # not let profile reporting and argv generation drift apart.
        from .launcher import _codex_default_effort

        resolved_effort = _codex_default_effort(model_id)
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


def validate_dispatch_requirements(
    binding: ExecutionProfileBinding,
    *,
    identity: object,
    qualification: Mapping[str, object] | None = None,
    qualification_required: bool = False,
    builder_domains: Sequence[str] = (),
    trust_root_valid: bool = True,
    quota: Mapping[str, object] | None = None,
) -> None:
    """Recheck profile-bound hard gates at the final pre-spawn boundary.

    Quota is accepted only for observability; it is intentionally excluded from
    the permission calculation.
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
    if role not in getattr(identity, "capabilities", ()):
        raise ExecutionAdapterError(f"identity lacks required role capability: {role}")
    pin = binding.resolved.requirements["pin"]
    if pin.get("state") == "known" and pin.get("value") is not None:
        expected = pin["value"]
        if not isinstance(expected, Mapping) or (expected.get("executor"), expected.get("model_id")) != (executor, model_id):
            raise ExecutionAdapterError("explicit model pin does not match resolved identity")
    if role == "review" and getattr(identity, "independence_domain", None) in set(builder_domains):
        raise ExecutionAdapterError("reviewer independence domain matches a builder")
    if not trust_root_valid:
        raise ExecutionAdapterError("Trust Root profile is not valid")
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
    """Validate a profile-aware file and optionally map its legacy envelope view.

    Report acceptance is provenance only.  The optional existing envelope
    mapping is a compatibility projection and never creates qualification.
    """

    if not isinstance(payload, Mapping):
        raise ExecutionAdapterError("profile report must be an object")
    if payload.get("schema_version") != 1:
        raise ExecutionAdapterError("unsupported profile report schema")
    canonical = json.dumps(
        dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if (
        payload.get("source_revision") != source_revision
        or not isinstance(source_digest, str)
        or sha256(canonical).hexdigest() != source_digest
    ):
        raise ExecutionAdapterError("profile report source revision/digest mismatch")
    if payload.get("profile_key") != expected_profile_key:
        raise ExecutionAdapterError("profile report does not match exact resolved profile")
    if payload.get("pricing") is not None and not isinstance(payload.get("pricing"), Mapping):
        raise ExecutionAdapterError("profile report pricing metadata must be an object")
    result = dict(payload)
    if envelope_context is not None:
        expected_context = {
            "executor", "model_id", "persona", "deck", "patchmud_version",
            "report_model", "report_loadout",
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
                report_model=envelope_context["report_model"],
                report_loadout=envelope_context["report_loadout"],
            )
        except (EnvelopeMappingError, TypeError, ValueError) as exc:
            raise ExecutionAdapterError("profile report envelope mapping failed") from exc
    return result
