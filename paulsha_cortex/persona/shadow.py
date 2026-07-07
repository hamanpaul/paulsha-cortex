from __future__ import annotations

from typing import Mapping

from . import context, contract
from .guardrail import PersonaGuardrail


def run_shadow_validation(
    *,
    role: str,
    phase: str,
    gate_status: str,
    path: str,
    tool: str,
    handoff: Mapping[str, object],
    overlay: Mapping[str, object] | None = None,
) -> dict[str, object]:
    persona_context = context.build_persona_context(role=role, overlay=overlay)
    validator = PersonaGuardrail(contract.PERSONA_CATALOG)

    phase_allowed = contract.is_phase_allowed(role, phase)
    handoff_result = contract.validate_handoff_message(handoff)
    filesystem_decision = validator.evaluate_filesystem(role=role, path=path)
    tool_decision = validator.evaluate_tool(role=role, tool=tool)

    gate_status_valid = gate_status in contract.GATE_STATUSES

    overall_ok = all(
        (
            phase_allowed,
            gate_status_valid,
            handoff_result.ok,
            filesystem_decision.allowed,
            tool_decision.allowed,
        )
    )

    return {
        "role": role,
        "phase": phase,
        "gate_status": gate_status,
        "phase_allowed": phase_allowed,
        "gate_status_valid": gate_status_valid,
        "handoff": {
            "ok": handoff_result.ok,
            "errors": list(handoff_result.errors),
        },
        "filesystem": {
            "allowed": filesystem_decision.allowed,
            "rule_id": filesystem_decision.rule_id,
            "reason": filesystem_decision.reason,
        },
        "tool": {
            "allowed": tool_decision.allowed,
            "rule_id": tool_decision.rule_id,
            "reason": tool_decision.reason,
        },
        "context": persona_context,
        "overall_ok": overall_ok,
    }
