from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from paulsha_cortex.config import paths

from .._yaml import YAMLError, safe_load
from .launcher import build_agy_argv

MODEL_IDENTITY_SCHEMA_VERSION = 2
SUPPORTED_MODEL_IDENTITY_SCHEMAS = frozenset({1, 2})
AGY_MODEL_ID = "Gemini 3.1 Pro (High)"
AGY_DOMAIN = "google"
AGY_LIVE_PROBE = "agy-plan-sandbox"
PLANNER_PRIORITY = (
    ("agy", "google"),
    ("claude", "anthropic"),
    ("codex", "openai"),
)


def _assert_no_duplicate_yaml_keys(text: str) -> None:
    contexts: list[tuple[int, set[str]]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            if ":" not in item_text:
                while contexts and contexts[-1][0] > indent:
                    contexts.pop()
                continue
            key = item_text.split(":", 1)[0].strip()
            context_indent = indent + 2
            while contexts and contexts[-1][0] >= context_indent:
                contexts.pop()
            contexts.append((context_indent, set()))
            if key in contexts[-1][1]:
                raise ValueError(f"duplicate key '{key}' at line {lineno}")
            contexts[-1][1].add(key)
            continue
        if ":" not in stripped:
            continue
        key = stripped.split(":", 1)[0].strip()
        while contexts and contexts[-1][0] > indent:
            contexts.pop()
        if not contexts or contexts[-1][0] < indent:
            contexts.append((indent, set()))
        if key in contexts[-1][1]:
            raise ValueError(f"duplicate key '{key}' at line {lineno}")
        contexts[-1][1].add(key)


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class ModelIdentity:
    executor: str
    model_id: str
    independence_domain: str
    capabilities: tuple[str, ...] = ()
    live_probe: str | None = None

    def legacy_dict(self) -> dict[str, str]:
        return {
            "executor": self.executor,
            "model_id": self.model_id,
            "independence_domain": self.independence_domain,
        }

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = self.legacy_dict()
        payload["capabilities"] = list(self.capabilities)
        if self.live_probe is not None:
            payload["live_probe"] = self.live_probe
        return payload


@dataclass(frozen=True)
class IdentityRegistry:
    schema_version: int
    identities: tuple[ModelIdentity, ...]

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[Mapping[str, object]],
        *,
        schema_version: int = MODEL_IDENTITY_SCHEMA_VERSION,
    ) -> "IdentityRegistry":
        identities: list[ModelIdentity] = []
        seen: set[tuple[str, str]] = set()
        allowed = {
            "executor",
            "model_id",
            "independence_domain",
            "capabilities",
            "live_probe",
        }
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ValueError(f"model-identities[{index}] must be an object")
            extras = set(row) - allowed
            if extras:
                raise ValueError(f"model-identities[{index}].{sorted(extras)[0]} unexpected")
            executor = _nonempty(row.get("executor"), f"model-identities[{index}].executor")
            model_id = _nonempty(row.get("model_id"), f"model-identities[{index}].model_id")
            domain = _nonempty(
                row.get("independence_domain"),
                f"model-identities[{index}].independence_domain",
            )
            capabilities_raw = row.get("capabilities", [])
            if not isinstance(capabilities_raw, list) or any(
                not isinstance(item, str) or not item.strip() for item in capabilities_raw
            ):
                raise ValueError(f"model-identities[{index}].capabilities must be a string list")
            capabilities = tuple(item.strip() for item in capabilities_raw)
            if len(set(capabilities)) != len(capabilities):
                raise ValueError(f"model-identities[{index}].capabilities contains duplicates")
            live_probe_raw = row.get("live_probe")
            live_probe = (
                None
                if live_probe_raw is None
                else _nonempty(live_probe_raw, f"model-identities[{index}].live_probe")
            )
            if executor == "agy" and "planning" in capabilities:
                if domain != AGY_DOMAIN or live_probe != AGY_LIVE_PROBE:
                    raise ValueError(
                        f"model-identities[{index}] agy planning requires google and {AGY_LIVE_PROBE}"
                    )
            key = (executor, model_id)
            if key in seen:
                raise ValueError(f"model-identities duplicate identity: {executor}/{model_id}")
            seen.add(key)
            identities.append(
                ModelIdentity(
                    executor=executor,
                    model_id=model_id,
                    independence_domain=domain,
                    capabilities=capabilities,
                    live_probe=live_probe,
                )
            )
        return cls(schema_version=schema_version, identities=tuple(identities))

    def get(self, executor: str, model_id: str) -> ModelIdentity | None:
        for identity in self.identities:
            if (identity.executor, identity.model_id) == (executor, model_id):
                return identity
        return None

    def require(self, executor: str, model_id: str) -> ModelIdentity:
        identity = self.get(executor, model_id)
        if identity is None:
            raise ValueError(f"model identity unknown: {executor}/{model_id}")
        return identity

    def legacy_mapping(self) -> dict[tuple[str, str], dict[str, str]]:
        return {
            (identity.executor, identity.model_id): identity.legacy_dict()
            for identity in self.identities
        }


def _packaged_registry_path() -> Path:
    return Path(__file__).with_name("data") / "model-identities.yaml"


def _load_model_identity_file(path: Path) -> IdentityRegistry:
    if not path.is_file():
        raise ValueError(f"model-identities missing: {path}")
    try:
        text = path.read_text(encoding="utf-8")
        _assert_no_duplicate_yaml_keys(text)
        payload = safe_load(text)
    except (OSError, UnicodeDecodeError, YAMLError, ValueError) as exc:
        raise ValueError(f"model-identities unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"model-identities invalid root: {path}")
    extras = set(payload) - {"schema_version", "identities"}
    if extras:
        raise ValueError(f"model-identities unexpected top-level key: {sorted(extras)[0]}")
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version not in SUPPORTED_MODEL_IDENTITY_SCHEMAS:
        raise ValueError(
            "model-identities schema_version must be one of "
            f"{sorted(SUPPORTED_MODEL_IDENTITY_SCHEMAS)}, got {schema_version!r}"
        )
    rows = payload.get("identities")
    if not isinstance(rows, list):
        raise ValueError("model-identities identities must be a list")
    if schema_version == 1:
        normalized_rows = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"model-identities[{index}] must be an object")
            extras = set(row) - {"executor", "model_id", "independence_domain"}
            if extras:
                raise ValueError(f"model-identities[{index}].{sorted(extras)[0]} unexpected")
            normalized_rows.append(dict(row))
        rows = normalized_rows
    return IdentityRegistry.from_rows(rows, schema_version=int(schema_version))


def load_model_identities(
    config_root: str | Path | None = None,
    *,
    use_packaged_default: bool = False,
) -> IdentityRegistry:
    root = Path(config_root) if config_root is not None else paths.project_config_root()
    custom_path = root / "model-identities.yaml"
    if not use_packaged_default:
        return _load_model_identity_file(custom_path)

    packaged = _load_model_identity_file(_packaged_registry_path())
    if not custom_path.is_file():
        return packaged
    custom = _load_model_identity_file(custom_path)
    packaged_keys = {(item.executor, item.model_id) for item in packaged.identities}
    for identity in custom.identities:
        key = (identity.executor, identity.model_id)
        if key in packaged_keys:
            raise ValueError(
                f"model-identities custom identity shadows packaged default: {key[0]}/{key[1]}"
            )
    return IdentityRegistry(
        schema_version=MODEL_IDENTITY_SCHEMA_VERSION,
        identities=packaged.identities + custom.identities,
    )


@dataclass(frozen=True)
class CapabilityProbe:
    ready: bool
    executor: str
    model_id: str
    independence_domain: str
    reason: str | None = None
    diagnostic: str | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.executor, self.model_id, self.independence_domain)

    @classmethod
    def ready_for(cls, executor: str, model_id: str, domain: str) -> "CapabilityProbe":
        return cls(True, executor, model_id, domain)


ProcessRunner = Callable[..., object]


def _process_fields(raw: object) -> tuple[int, str, str]:
    returncode = getattr(raw, "returncode", None)
    stdout = getattr(raw, "stdout", None)
    stderr = getattr(raw, "stderr", None)
    if not isinstance(returncode, int) or not isinstance(stdout, str) or not isinstance(stderr, str):
        raise ValueError("malformed process result")
    return returncode, stdout, stderr


def _failed_agy(reason: str, diagnostic: str | None = None) -> CapabilityProbe:
    return CapabilityProbe(False, "agy", AGY_MODEL_ID, AGY_DOMAIN, reason, diagnostic)


def probe_agy_capability(
    *,
    runner: ProcessRunner | None = None,
    timeout_seconds: int = 45,
) -> CapabilityProbe:
    """Probe both model identity discovery and the exact safe planning mode."""
    process_runner = runner or subprocess.run
    common = {
        "shell": False,
        "capture_output": True,
        "text": True,
        "timeout": timeout_seconds,
    }
    try:
        listed_raw = process_runner(["agy", "models"], **common)
        listed_rc, listed_stdout, _listed_stderr = _process_fields(listed_raw)
    except Exception as exc:
        return _failed_agy("models-probe-failed", type(exc).__name__)
    if listed_rc != 0:
        return _failed_agy("models-probe-failed", f"exit-code:{listed_rc}")
    if AGY_MODEL_ID not in {line.strip() for line in listed_stdout.splitlines()}:
        return _failed_agy("model-not-listed")

    expected = {"capability": "cortex-plan-sandbox", "model": AGY_MODEL_ID}
    prompt = (
        "Return only this compact JSON object and perform no tool calls: "
        + json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    )
    argv = build_agy_argv(
        prompt=prompt,
        slice_id="cortex-capability-probe",
        log_dir=".",
        model=AGY_MODEL_ID,
    )
    try:
        smoke_raw = process_runner(argv, **common)
        smoke_rc, smoke_stdout, _smoke_stderr = _process_fields(smoke_raw)
    except Exception as exc:
        return _failed_agy("smoke-failed", type(exc).__name__)
    if smoke_rc != 0:
        return _failed_agy("smoke-failed", f"exit-code:{smoke_rc}")
    try:
        payload = json.loads(smoke_stdout.strip())
    except (json.JSONDecodeError, TypeError):
        return _failed_agy("malformed-output")
    if payload != expected:
        return _failed_agy("identity-mismatch")
    return CapabilityProbe.ready_for("agy", AGY_MODEL_ID, AGY_DOMAIN)


@dataclass(frozen=True)
class SecondarySelection:
    state: str
    reason: str | None
    identity: ModelIdentity | None


def select_secondary_planner(
    *,
    registry: IdentityRegistry,
    primary: tuple[str, str],
    probes: Mapping[tuple[str, str], CapabilityProbe],
) -> SecondarySelection:
    primary_identity = registry.get(*primary)
    if primary_identity is None:
        return SecondarySelection("needs_human", "primary-identity-unknown", None)
    for executor, domain in PLANNER_PRIORITY:
        for identity in registry.identities:
            if identity.executor != executor or identity.independence_domain != domain:
                continue
            if "planning" not in identity.capabilities:
                continue
            if identity.independence_domain == primary_identity.independence_domain:
                continue
            probe = probes.get((identity.executor, identity.model_id))
            if probe is None or not probe.ready:
                continue
            if probe.identity != (
                identity.executor,
                identity.model_id,
                identity.independence_domain,
            ):
                continue
            return SecondarySelection("ready", None, identity)
    return SecondarySelection("needs_human", "no-heterogeneous-planner", None)
