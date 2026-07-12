from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _write_spec(dirpath: Path, name: str, frontmatter: str, body: str = "body") -> Path:
    path = dirpath / name
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path


class VerificationContractFrontmatterTests(unittest.TestCase):
    def test_parser_rejects_non_string_target_branch_even_for_hold_specs(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        with tempfile.TemporaryDirectory() as d:
            meta = parse_spec_frontmatter(
                _write_spec(
                    Path(d),
                    "hold-invalid-target.md",
                    "dispatch: hold\n"
                    "slice_id: hold-invalid-target\n"
                    "target_branch:\n"
                    "  - main",
                )
            )

            self.assertEqual(meta["dispatch"], "hold")
            self.assertIsNone(meta["target_branch"])
            self.assertEqual(meta["parse_error"]["field"], "target_branch")
            self.assertIn("non-empty string", meta["parse_error"]["message"])

    def test_auto_dispatch_requires_exactly_one_persona_scope_and_policy_command(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        with tempfile.TemporaryDirectory() as d:
            meta = parse_spec_frontmatter(
                _write_spec(
                    Path(d),
                    "missing-policy.md",
                    "dispatch: auto\n"
                    "slice_id: missing-policy\n"
                    "plan: docs/superpowers/plans/missing-policy.md\n"
                    "target_branch: main\n"
                    "verification:\n"
                    "  docs_class: code\n"
                    "  required_artifacts: []\n"
                    "  checks:\n"
                    "    - kind: persona-scope\n"
                    "    - kind: command\n"
                    "      name: docs\n"
                    "      argv: [python3, -m, pytest, -q]\n"
                    "      cwd: .\n"
                    "      timeout_seconds: 30\n"
                    "  tests: []\n"
                    "  full_suite:\n"
                    "    argv: [python3, -m, pytest, -q]\n"
                    "    cwd: .\n"
                    "    timeout_seconds: 30\n"
                    "    baseline: no-regression",
                )
            )

            self.assertEqual(meta["dispatch"], "hold")
            self.assertEqual(meta["parse_error"]["field"], "verification.checks")
            self.assertIn("policy", meta["parse_error"]["message"])

    def test_parser_rejects_unknown_v1_keys_instead_of_silently_dropping_them(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        with tempfile.TemporaryDirectory() as d:
            meta = parse_spec_frontmatter(
                _write_spec(
                    Path(d),
                    "unknown-key.md",
                    "dispatch: auto\n"
                    "slice_id: unknown-key\n"
                    "plan: docs/superpowers/plans/unknown-key.md\n"
                    "target_branch: main\n"
                    "unexpected: true\n"
                    "verification:\n"
                    "  docs_class: code\n"
                    "  required_artifacts: []\n"
                    "  checks:\n"
                    "    - kind: persona-scope\n"
                    "    - kind: command\n"
                    "      name: policy\n"
                    "      argv: [python3, -m, pytest, -q]\n"
                    "      cwd: .\n"
                    "      timeout_seconds: 30\n"
                    "  tests: []\n"
                    "  full_suite:\n"
                    "    argv: [python3, -m, pytest, -q]\n"
                    "    cwd: .\n"
                    "    timeout_seconds: 30\n"
                    "    baseline: no-regression",
                )
            )

            self.assertEqual(meta["dispatch"], "hold")
            self.assertEqual(meta["parse_error"]["field"], "unexpected")
            self.assertIn("unexpected", meta["parse_error"]["message"])

    def test_parser_rejects_path_escape_in_verification_contract(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        with tempfile.TemporaryDirectory() as d:
            meta = parse_spec_frontmatter(
                _write_spec(
                    Path(d),
                    "escape.md",
                    "dispatch: auto\n"
                    "slice_id: escape\n"
                    "plan: docs/superpowers/plans/escape.md\n"
                    "target_branch: main\n"
                    "verification:\n"
                    "  docs_class: code\n"
                    "  required_artifacts:\n"
                    "    - path: ../outside.py\n"
                    "      must_change: true\n"
                    "  checks:\n"
                    "    - kind: persona-scope\n"
                    "    - kind: command\n"
                    "      name: policy\n"
                    "      argv: [python3, -m, pytest, -q]\n"
                    "      cwd: ../\n"
                    "      timeout_seconds: 30\n"
                    "  tests: []\n"
                    "  full_suite:\n"
                    "    argv: [python3, -m, pytest, -q]\n"
                    "    cwd: .\n"
                    "    timeout_seconds: 30\n"
                    "    baseline: no-regression",
                )
            )

            self.assertEqual(meta["dispatch"], "hold")
            self.assertIn(meta["parse_error"]["field"], {"verification.required_artifacts[0].path", "verification.checks[1].cwd"})

    def test_parser_rejects_non_boolean_must_change(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        with tempfile.TemporaryDirectory() as d:
            meta = parse_spec_frontmatter(
                _write_spec(
                    Path(d),
                    "must-change-string.md",
                    "dispatch: auto\n"
                    "slice_id: must-change-string\n"
                    "plan: docs/superpowers/plans/must-change-string.md\n"
                    "target_branch: main\n"
                    "verification:\n"
                    "  docs_class: code\n"
                    "  required_artifacts:\n"
                    "    - path: docs/spec.py\n"
                    "      must_change: 'false'\n"
                    "  checks:\n"
                    "    - kind: persona-scope\n"
                    "    - kind: command\n"
                    "      name: policy\n"
                    "      argv: [python3, -m, pytest, -q]\n"
                    "      cwd: .\n"
                    "      timeout_seconds: 30\n"
                    "  tests: []\n"
                    "  full_suite:\n"
                    "    argv: [python3, -m, pytest, -q]\n"
                    "    cwd: .\n"
                    "    timeout_seconds: 30\n"
                    "    baseline: no-regression",
                )
            )

            self.assertEqual(meta["dispatch"], "hold")
            self.assertEqual(meta["parse_error"]["field"], "verification.required_artifacts[0].must_change")
            self.assertIn("boolean", meta["parse_error"]["message"])


class VerificationEvidenceWriterTests(unittest.TestCase):
    def test_evidence_path_requires_full_candidate_sha(self) -> None:
        from paulsha_cortex.coordinator import verification

        with tempfile.TemporaryDirectory() as d:
            for candidate in ("abc1234", "d" * 64):
                with self.subTest(candidate=candidate):
                    with self.assertRaisesRegex(ValueError, "unsafe candidate sha"):
                        verification.evidence_path(
                            slice_id="slice-a",
                            candidate=candidate,
                            coordinator_root=Path(d),
                        )

    def test_validate_verification_evidence_requires_full_candidate_sha(self) -> None:
        from paulsha_cortex.coordinator import verification

        for candidate in ("abc1234", "d" * 64):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, "invalid verification evidence candidate"):
                    verification.validate_verification_evidence(
                        {
                            "schema_version": 1,
                            "slice_id": "slice-a",
                            "candidate": candidate,
                            "status": "needs_human",
                            "summary": "spec-hash-mismatch",
                            "details": {"expected": "old", "actual": "new"},
                        }
                    )

    def test_write_verification_evidence_is_idempotent_for_identical_content(self) -> None:
        from paulsha_cortex.coordinator import verification

        with tempfile.TemporaryDirectory() as d:
            payload = {
                "schema_version": 1,
                "slice_id": "slice-a",
                "candidate": "a" * 40,
                "status": "needs_human",
                "summary": "spec-hash-mismatch",
                "details": {"expected": "spec-old", "actual": "spec-new"},
            }

            first = verification.write_verification_evidence(payload, coordinator_root=Path(d))
            second = verification.write_verification_evidence(dict(payload), coordinator_root=Path(d))

            self.assertEqual(first["path"], second["path"])
            self.assertEqual(first["hash"], second["hash"])
            self.assertEqual(first["payload"], second["payload"])

    def test_write_verification_evidence_quarantines_conflicts(self) -> None:
        from paulsha_cortex.coordinator import verification

        with tempfile.TemporaryDirectory() as d:
            coordinator_root = Path(d)
            payload = {
                "schema_version": 1,
                "slice_id": "slice-a",
                "candidate": "b" * 40,
                "status": "needs_human",
                "summary": "spec-hash-mismatch",
                "details": {"expected": "old", "actual": "new"},
            }
            verification.write_verification_evidence(payload, coordinator_root=coordinator_root)

            conflicting = dict(payload)
            conflicting["summary"] = "different-content"

            with self.assertRaisesRegex(RuntimeError, "conflicting verification evidence"):
                verification.write_verification_evidence(conflicting, coordinator_root=coordinator_root)

            quarantine_dir = coordinator_root / "evidence" / "verification" / "quarantine"
            quarantined = list(quarantine_dir.glob("slice-a-*.json"))
            self.assertEqual(len(quarantined), 1)

    def test_write_verification_evidence_quarantines_raced_conflicts(self) -> None:
        from paulsha_cortex.coordinator import verification

        with tempfile.TemporaryDirectory() as d:
            coordinator_root = Path(d)
            payload = {
                "schema_version": 1,
                "slice_id": "slice-a",
                "candidate": "d" * 40,
                "status": "needs_human",
                "summary": "spec-hash-mismatch",
                "details": {"expected": "old", "actual": "new"},
            }
            conflicting = dict(payload)
            conflicting["summary"] = "different-content"
            original_atomic_write_json = verification.atomic_write_json

            def racing_atomic_write_json(path: Path, normalized: dict) -> None:
                original_atomic_write_json(path, conflicting)
                original_atomic_write_json(path, normalized)

            with mock.patch.object(
                verification,
                "atomic_write_json",
                side_effect=racing_atomic_write_json,
            ):
                with self.assertRaisesRegex(RuntimeError, "conflicting verification evidence"):
                    verification.write_verification_evidence(payload, coordinator_root=coordinator_root)

            evidence_path = verification.evidence_path(
                slice_id=payload["slice_id"],
                candidate=payload["candidate"],
                coordinator_root=coordinator_root,
            )
            self.assertFalse(evidence_path.exists())
            quarantine_dir = coordinator_root / "evidence" / "verification" / "quarantine"
            quarantined = list(quarantine_dir.glob("slice-a-*.json"))
            self.assertEqual(len(quarantined), 1)

    def test_write_verification_evidence_quarantines_invalid_existing_json(self) -> None:
        from paulsha_cortex.coordinator import verification

        with tempfile.TemporaryDirectory() as d:
            coordinator_root = Path(d)
            payload = {
                "schema_version": 1,
                "slice_id": "slice-a",
                "candidate": "c" * 40,
                "status": "needs_human",
                "summary": "spec-hash-mismatch",
                "details": {"expected": "old", "actual": "new"},
            }
            path = verification.evidence_path(
                slice_id=payload["slice_id"],
                candidate=payload["candidate"],
                coordinator_root=coordinator_root,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"schema_version": 1, "slice_id": "slice-a", "candidate": "cccccccccccccccccccccccccccccccccccccccc"}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "invalid schema"):
                verification.write_verification_evidence(payload, coordinator_root=coordinator_root)

            self.assertFalse(path.exists())
            quarantine_dir = coordinator_root / "evidence" / "verification" / "quarantine"
            quarantined = list(quarantine_dir.glob("slice-a-*.json"))
            self.assertEqual(len(quarantined), 1)


class DispatchPinningTests(unittest.TestCase):
    def test_pin_dispatch_inputs_requires_readable_spec_and_plan(self) -> None:
        from paulsha_cortex.coordinator.autonomy import pin_dispatch_inputs

        with tempfile.TemporaryDirectory() as d:
            repo_root = Path(d)
            spec_path = repo_root / "specs" / "slice-a.md"
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text("---\n---\n", encoding="utf-8")
            meta = {
                "path": str(spec_path),
                "slice_id": "slice-a",
                "plan": "docs/superpowers/plans/missing.md",
                "target_branch": "main",
                "verification": {
                    "docs_class": "code",
                    "review_policy": "required",
                    "required_artifacts": [],
                    "checks": [{"kind": "persona-scope"}],
                    "tests": [],
                    "full_suite": {
                        "argv": ["python3", "-m", "pytest", "-q"],
                        "cwd": ".",
                        "timeout_seconds": 30,
                        "baseline": "no-regression",
                    },
                },
            }

            with self.assertRaisesRegex(ValueError, "plan"):
                pin_dispatch_inputs(meta)


if __name__ == "__main__":
    unittest.main()
