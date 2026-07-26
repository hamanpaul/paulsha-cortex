from __future__ import annotations

from paulsha_cortex.persona import contract, guardrail


def _builder_guardrail() -> guardrail.PersonaGuardrail:
    return guardrail.PersonaGuardrail(contract.PERSONA_CATALOG)


def test_cross_repo_dispatch_path_is_out_of_builder_scope_before_fix() -> None:
    decision = _builder_guardrail().evaluate_filesystem(
        role="builder",
        path="external-repo/src/feature/agent/main.py",
    )

    assert not decision.allowed
    assert decision.rule_id == "filesystem-scope"


def test_cortex_repo_dispatch_keeps_in_scope() -> None:
    decision = _builder_guardrail().evaluate_filesystem(
        role="builder",
        path="paulsha_cortex/persona/contract.py",
    )

    assert decision.allowed
