from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paulsha_cortex.persona.loader import load_enforcement


class LoadEnforcementTests(unittest.TestCase):
    def test_default_yaml_is_shadow(self) -> None:
        self.assertEqual(load_enforcement(), "shadow")

    def test_explicit_enforce_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "personas.yaml"
            p.write_text("enforcement: enforce\nroles: {}\n", encoding="utf-8")
            self.assertEqual(load_enforcement(p), "enforce")

    def test_absent_key_defaults_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "personas.yaml"
            p.write_text("version: 1\nroles: {}\n", encoding="utf-8")
            self.assertEqual(load_enforcement(p), "shadow")

    def test_bogus_value_defaults_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "personas.yaml"
            p.write_text("enforcement: yolo\nroles: {}\n", encoding="utf-8")
            self.assertEqual(load_enforcement(p), "shadow")

    def test_missing_file_defaults_shadow(self) -> None:
        self.assertEqual(load_enforcement("/nonexistent/personas.yaml"), "shadow")

    def test_malformed_yaml_defaults_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "personas.yaml"
            p.write_text("enforcement: [unclosed\n", encoding="utf-8")
            self.assertEqual(load_enforcement(p), "shadow")

    def test_unreadable_file_defaults_shadow(self) -> None:
        # read_text 觸發 OSError（權限/IO）時也須 fail-safe 退 shadow，不可整個崩潰。
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "personas.yaml"
            p.write_text("enforcement: enforce\nroles: {}\n", encoding="utf-8")
            with mock.patch.object(
                Path, "read_text", side_effect=PermissionError("permission denied")
            ):
                self.assertEqual(load_enforcement(p), "shadow")


if __name__ == "__main__":
    unittest.main()
