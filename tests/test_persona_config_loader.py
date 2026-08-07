from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paulsha_cortex.persona import contract, guardrail
from paulsha_cortex.persona.loader import load_catalog


class LoadCatalogTests(unittest.TestCase):
    def test_default_catalog_loads_required_roles_including_planner(self) -> None:
        catalog = load_catalog()
        self.assertEqual(set(catalog), {"manager", "planner", "builder", "reviewer"})
        self.assertTrue(contract.validate_persona_schema(catalog).ok)

    def test_planner_is_limited_to_define_and_plan(self) -> None:
        planner = load_catalog()["planner"]
        self.assertEqual(planner.allowed_phases, ("define", "plan"))

    def test_modern_catalog_without_required_planner_is_rejected(self) -> None:
        catalog = load_catalog()
        catalog.pop("planner")
        result = contract.validate_persona_schema(catalog)
        self.assertFalse(result.ok)
        self.assertIn("missing required role: planner", result.errors)

    def test_legacy_v1_catalog_without_planner_remains_compatible(self) -> None:
        catalog = load_catalog()
        catalog.pop("planner")
        legacy = {
            role: contract.PersonaContract(
                role=persona.role,
                version="v1",
                summary=persona.summary,
                allowed_phases=persona.allowed_phases,
                write_paths=persona.write_paths,
                allowed_tools=persona.allowed_tools,
                skills=persona.skills,
            )
            for role, persona in catalog.items()
        }
        self.assertTrue(contract.validate_persona_schema(legacy).ok)

    def test_missing_file_fails_closed(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_catalog("/nonexistent/personas.yaml")

    def test_invalid_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "personas.yaml"
            bad.write_text("roles:\n  manager:\n    role: manager\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_catalog(bad)

    def test_contract_catalog_sourced_from_yaml(self) -> None:
        self.assertEqual(
            list(contract.PERSONA_CATALOG["builder"].write_paths),
            [
                "**",
            ],
        )

    def test_malformed_yaml_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "personas.yaml"
            bad.write_text("roles: [unclosed\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_catalog(bad)

    def test_missing_roles_key_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "personas.yaml"
            bad.write_text("version: 1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_catalog(bad)

    def test_builder_completion_obligations_require_commit(self) -> None:
        obligations = " ".join(contract.PERSONA_CATALOG["builder"].completion_obligations)
        self.assertTrue(obligations)
        self.assertIn("commit", obligations)
        self.assertTrue("worktree" in obligations or "乾淨" in obligations)

    def test_completion_obligations_non_list_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "personas.yaml"
            bad.write_text(
                "version: 1\n"
                "enforcement: shadow\n"
                "roles:\n"
                "  manager:\n"
                "    role: manager\n"
                "    version: \"1.0.0\"\n"
                "    summary: manager\n"
                "    allowed_phases: [claim]\n"
                "    write_paths: [\"docs/**\"]\n"
                "    allowed_tools: [\"git\"]\n"
                "  planner:\n"
                "    role: planner\n"
                "    version: \"1.0.0\"\n"
                "    summary: planner\n"
                "    allowed_phases: [plan]\n"
                "    write_paths: [\"docs/**\"]\n"
                "    allowed_tools: [\"rg\"]\n"
                "  builder:\n"
                "    role: builder\n"
                "    version: \"1.0.0\"\n"
                "    summary: builder\n"
                "    allowed_phases: [build]\n"
                "    write_paths: [\"**\"]\n"
                "    allowed_tools: [\"git\"]\n"
                "    completion_obligations: \"not a list\"\n"
                "  reviewer:\n"
                "    role: reviewer\n"
                "    version: \"1.0.0\"\n"
                "    summary: reviewer\n"
                "    allowed_phases: [review]\n"
                "    write_paths: [\"reports/**\"]\n"
                "    allowed_tools: [\"rg\"]\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_catalog(bad)


class RoleV2ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rail = guardrail.PersonaGuardrail(load_catalog())

    def test_manager_can_write_openspec(self) -> None:
        self.assertTrue(
            self.rail.evaluate_filesystem(role="manager", path="openspec/changes/x/proposal.md").allowed
        )

    def test_builder_archive_yes_push_no_commit_yes(self) -> None:
        self.assertTrue(
            self.rail.evaluate_filesystem(role="builder", path="openspec/changes/archive/x/spec.md").allowed
        )
        self.assertTrue(
            self.rail.evaluate_filesystem(role="builder", path="openspec/changes/x/tasks.md").allowed
        )
        self.assertTrue(
            self.rail.evaluate_filesystem(role="builder", path="changelog.d/116-builder-deliverables.md").allowed
        )
        self.assertFalse(self.rail.evaluate_tool(role="builder", tool="git push").allowed)
        self.assertTrue(self.rail.evaluate_tool(role="builder", tool="git commit").allowed)

    def test_reviewer_only_reports(self) -> None:
        self.assertTrue(
            self.rail.evaluate_filesystem(role="reviewer", path="reports/review/r.md").allowed
        )
        self.assertFalse(
            self.rail.evaluate_filesystem(role="reviewer", path="paulsha_cortex/x.py").allowed
        )


if __name__ == "__main__":
    unittest.main()
