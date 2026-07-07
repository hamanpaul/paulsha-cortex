from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from typing import Mapping

from paulsha_hippo.lib.lifecycle import schema as lifecycle_schema


PHASES = lifecycle_schema.PHASES
REQUIRED_ROLES = ("manager", "builder", "reviewer")
GATE_STATUSES = ("passed", "failed", "running", "skipped", "override")


@dataclass(frozen=True)
class PersonaContract:
    role: str
    version: str
    summary: str
    allowed_phases: tuple[str, ...]
    write_paths: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: tuple[str, ...]


_REQUIRED_PERSONA_FIELDS = (
    "role",
    "version",
    "summary",
    "allowed_phases",
    "write_paths",
    "allowed_tools",
)

_REQUIRED_HANDOFF_FIELDS = (
    "from_role",
    "to_role",
    "phase",
    "gate_status",
    "slice_id",
    "summary",
    "artifact_refs",
    "created_at",
)


def get_persona_contract(role: str, catalog: Mapping[str, PersonaContract] | None = None) -> PersonaContract | None:
    source = catalog or PERSONA_CATALOG
    return source.get(role)


def is_phase_allowed(role: str, phase: str, catalog: Mapping[str, PersonaContract] | None = None) -> bool:
    if phase not in PHASES:
        return False

    persona = get_persona_contract(role, catalog)
    if persona is None:
        return False

    return phase in persona.allowed_phases


def validate_persona_schema(
    catalog: Mapping[str, PersonaContract | Mapping[str, object]],
) -> ValidationResult:
    errors: list[str] = []

    for required_role in REQUIRED_ROLES:
        if required_role not in catalog:
            errors.append(f"missing required role: {required_role}")

    for role, persona in catalog.items():
        record = _persona_record(persona)

        for field in _REQUIRED_PERSONA_FIELDS:
            value = record.get(field)
            if value in (None, ""):
                errors.append(f"{role}: missing required field {field}")

        for str_field in ("role", "version", "summary"):
            value = record.get(str_field)
            if value is not None and not isinstance(value, str):
                errors.append(f"{role}: {str_field} must be a string")

        if record.get("role") != role:
            errors.append(f"{role}: role field must match catalog key")

        allowed_phases = record.get("allowed_phases")
        if not isinstance(allowed_phases, (list, tuple)) or not allowed_phases:
            errors.append(f"{role}: allowed_phases must be a non-empty list")
        elif any(phase not in PHASES for phase in allowed_phases):
            errors.append(f"{role}: allowed_phases must be subset of {PHASES}")

        write_paths = record.get("write_paths")
        if not isinstance(write_paths, (list, tuple)) or not write_paths:
            errors.append(f"{role}: write_paths must be a non-empty list")

        allowed_tools = record.get("allowed_tools")
        if not isinstance(allowed_tools, (list, tuple)) or not allowed_tools:
            errors.append(f"{role}: allowed_tools must be a non-empty list")

        skills = record.get("skills")
        if skills is not None and (
            not isinstance(skills, (list, tuple)) or any(not isinstance(skill, str) for skill in skills)
        ):
            errors.append(f"{role}: skills 必須是字串清單")

    return ValidationResult(ok=not errors, errors=tuple(errors))


def validate_handoff_message(
    payload: Mapping[str, object],
    catalog: Mapping[str, PersonaContract] | None = None,
) -> ValidationResult:
    source = catalog or PERSONA_CATALOG
    errors: list[str] = []

    for field in _REQUIRED_HANDOFF_FIELDS:
        value = payload.get(field)
        if value in (None, ""):
            errors.append(f"handoff required field missing: {field}")

    from_role = payload.get("from_role")
    to_role = payload.get("to_role")
    phase = payload.get("phase")
    gate_status = payload.get("gate_status")
    artifact_refs = payload.get("artifact_refs")
    created_at = payload.get("created_at")

    if isinstance(from_role, str) and from_role not in source:
        errors.append(f"unknown from_role: {from_role}")
    if isinstance(to_role, str) and to_role not in source:
        errors.append(f"unknown to_role: {to_role}")

    if isinstance(phase, str):
        if phase not in PHASES:
            errors.append(f"phase must be one of {PHASES}")
        elif isinstance(to_role, str) and to_role in source and not is_phase_allowed(to_role, phase, source):
            errors.append(f"to_role {to_role} cannot enter phase {phase}")

    if gate_status not in GATE_STATUSES:
        errors.append(f"gate_status must be one of {GATE_STATUSES}")

    if not isinstance(artifact_refs, list) or not artifact_refs:
        errors.append("artifact_refs must be a non-empty list")
    elif any(not isinstance(item, str) or not item.strip() for item in artifact_refs):
        errors.append("artifact_refs entries must be non-empty strings")

    if not _is_iso8601(created_at):
        errors.append("created_at must be ISO8601 timestamp")

    return ValidationResult(ok=not errors, errors=tuple(errors))


def _persona_record(persona: PersonaContract | Mapping[str, object]) -> dict[str, object]:
    if is_dataclass(persona):
        return asdict(persona)
    return dict(persona)


def _is_iso8601(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False

    normalized = value.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


from .loader import load_catalog  # noqa: E402  bottom-import 避免與 loader 循環相依


def _load_catalog_import_safe() -> dict[str, PersonaContract]:
    """import 期載入 catalog，但**絕不讓壞掉的 personas.yaml 在 import 期逃逸**。

    `loader.load_catalog` 維持嚴格 fail-closed（缺檔/壞 YAML/schema 不過 → raise），
    其例外語意由直接呼叫者（gate / scope_ci / loader 測試）負責處理。
    但 module-level 的 `PERSONA_CATALOG` 一旦在 import 期 raise，會讓整個 persona
    package 無法 import——包含設計 §8「shadow 恆 exit 0／絕不報錯」的 scope_ci
    entry point（`python -m paulsha_cortex.persona.scope_ci` 會先跑 __init__ → context
    → 此處）。為守住該硬安全保證，import 期改為 fail-closed 退到空 catalog：
    catalog 合法時照常填滿（既有行為不變）；壞掉時退空（deny-by-default），由各
    entry point 自行偵測並回報。
    """
    try:
        return load_catalog()
    except (FileNotFoundError, ValueError):
        return {}


PERSONA_CATALOG: dict[str, PersonaContract] = _load_catalog_import_safe()
