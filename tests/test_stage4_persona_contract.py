from __future__ import annotations

import unittest

from paulsha_cortex.persona import context, contract, guardrail, shadow


def make_handoff_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "from_role": "builder",
        "to_role": "reviewer",
        "phase": "review",
        "gate_status": "passed",
        "slice_id": "slice-stage4-1",
        "summary": "build slice ready for review",
        "artifact_refs": ["reports/verify/slice-stage4-1.md"],
        "created_at": "2026-04-21T00:00:00Z",
    }
    payload.update(overrides)
    return payload


class PersonaSchemaTests(unittest.TestCase):
    def test_persona_schema_accepts_baseline_catalog(self) -> None:
        result = contract.validate_persona_schema(contract.PERSONA_CATALOG)

        self.assertTrue(result.ok)
        self.assertEqual(result.errors, ())

    def test_persona_schema_rejects_missing_required_field(self) -> None:
        broken_catalog = {
            "manager": {
                "role": "manager",
                "summary": "missing version",
                "allowed_phases": ["research"],
                "write_paths": ["docs/**"],
                "allowed_tools": ["coordinator.dispatch"],
            }
        }

        result = contract.validate_persona_schema(broken_catalog)

        self.assertFalse(result.ok)
        self.assertIn("version", "\n".join(result.errors))


class RoleBaselineTests(unittest.TestCase):
    def test_required_roles_present(self) -> None:
        catalog = contract.PERSONA_CATALOG

        self.assertIn("manager", catalog)
        self.assertIn("builder", catalog)
        self.assertIn("reviewer", catalog)


class AllowedPhasesTests(unittest.TestCase):
    def test_allowed_phases_subset_of_stage3_vocabulary(self) -> None:
        stage3_vocab = set(contract.PHASES)
        catalog = contract.PERSONA_CATALOG

        for role, persona in catalog.items():
            with self.subTest(role=role):
                self.assertTrue(set(persona.allowed_phases).issubset(stage3_vocab))


class HandoffSchemaTests(unittest.TestCase):
    def test_stage3_phase_gate_compatibility(self) -> None:
        result = contract.validate_handoff_message(make_handoff_payload())

        self.assertTrue(result.ok)

    def test_gate_status_is_required_and_validated(self) -> None:
        payload = make_handoff_payload()
        payload.pop("gate_status")

        missing_result = contract.validate_handoff_message(payload)
        self.assertFalse(missing_result.ok)
        self.assertIn("gate_status", "\n".join(missing_result.errors))

        invalid_result = contract.validate_handoff_message(make_handoff_payload(gate_status="unknown"))
        self.assertFalse(invalid_result.ok)
        self.assertIn("gate_status", "\n".join(invalid_result.errors))


class GuardrailTests(unittest.TestCase):
    def test_disallow_tool_outside_allowlist(self) -> None:
        persona_guardrail = guardrail.PersonaGuardrail(contract.PERSONA_CATALOG)

        decision = persona_guardrail.evaluate_tool(role="reviewer", tool="git push")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule_id, "tool-allowlist")

    def test_disallow_path_outside_scope(self) -> None:
        persona_guardrail = guardrail.PersonaGuardrail(contract.PERSONA_CATALOG)

        decision = persona_guardrail.evaluate_filesystem(
            role="reviewer",
            path="paulsha_cortex/lifecycle/schema.py",
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule_id, "filesystem-scope")


class ShadowRunTests(unittest.TestCase):
    def test_user_overlay_load_and_context_build(self) -> None:
        overlay = context.load_user_overlay(
            {
                "instruction_append": ["follow user playbook"],
                "tool_allowlist_additions": ["review-helper"],
            }
        )

        persona_context = context.build_persona_context(
            role="reviewer",
            overlay=overlay,
        )

        self.assertIn("follow user playbook", persona_context["overlay"]["instruction_append"])
        self.assertIn("review-helper", persona_context["effective_tools"])

    def test_shadow_run_summary_contains_gate_and_guardrail_decisions(self) -> None:
        summary = shadow.run_shadow_validation(
            role="reviewer",
            phase="review",
            gate_status="passed",
            path="paulsha_cortex/lifecycle/schema.py",
            tool="git push",
            handoff=make_handoff_payload(),
            overlay={"instruction_append": ["enforce strict review"]},
        )

        self.assertEqual(summary["role"], "reviewer")
        self.assertEqual(summary["phase"], "review")
        self.assertEqual(summary["gate_status"], "passed")
        self.assertIn("filesystem", summary)
        self.assertIn("tool", summary)
        self.assertFalse(summary["filesystem"]["allowed"])
        self.assertFalse(summary["tool"]["allowed"])


if __name__ == "__main__":
    unittest.main()
