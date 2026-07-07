from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paulsha_cortex.persona import contract, guardrail
from paulsha_cortex.persona.loader import load_catalog


class LoadCatalogTests(unittest.TestCase):
    def test_default_catalog_loads_three_roles(self) -> None:
        catalog = load_catalog()
        self.assertEqual(set(catalog), {"manager", "builder", "reviewer"})
        self.assertTrue(contract.validate_persona_schema(catalog).ok)

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
            set(contract.PERSONA_CATALOG["builder"].write_paths),
            {"paulsha_cortex/**", "tests/**", "openspec/changes/archive/**"},
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
