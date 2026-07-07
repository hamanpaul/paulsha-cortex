from __future__ import annotations

from typing import Mapping

from .contract import PERSONA_CATALOG, PersonaContract, get_persona_contract


def load_user_overlay(overlay: Mapping[str, object] | None = None) -> dict[str, object]:
    base = {
        "instruction_append": [],
        "tool_allowlist_additions": [],
        "memory_loadout": [],
    }
    if overlay is None:
        return base

    for key in tuple(base.keys()):
        value = overlay.get(key)
        if isinstance(value, list):
            base[key] = [item for item in value if isinstance(item, str) and item.strip()]

    return base


def build_persona_context(
    *,
    role: str,
    catalog: Mapping[str, PersonaContract] | None = None,
    overlay: Mapping[str, object] | None = None,
) -> dict[str, object]:
    source = catalog or PERSONA_CATALOG
    persona = get_persona_contract(role, source)
    if persona is None:
        raise ValueError(f"unknown persona role: {role}")

    normalized_overlay = load_user_overlay(overlay)
    effective_tools = sorted(
        set(persona.allowed_tools).union(normalized_overlay["tool_allowlist_additions"])
    )

    return {
        "role": persona.role,
        "version": persona.version,
        "allowed_phases": list(persona.allowed_phases),
        "write_paths": list(persona.write_paths),
        "effective_tools": effective_tools,
        "overlay": normalized_overlay,
    }
