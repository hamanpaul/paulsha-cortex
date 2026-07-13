from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from hashlib import sha256


def _write_model_identities(root: Path, body: str) -> Path:
    path = root / "model-identities.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class ModelIdentityRegistryTests(unittest.TestCase):
    def test_foreign_reviewer_requires_explicit_known_different_domain_identity(self) -> None:
        from paulsha_cortex.coordinator import review

        with tempfile.TemporaryDirectory() as d:
            config_root = Path(d)
            _write_model_identities(
                config_root,
                (
                    "schema_version: 1\n"
                    "identities:\n"
                    "  - executor: copilot\n"
                    "    model_id: claude-haiku-4.5\n"
                    "    independence_domain: anthropic\n"
                    "  - executor: claude\n"
                    "    model_id: claude-sonnet-4.5\n"
                    "    independence_domain: anthropic\n"
                    "  - executor: codex\n"
                    "    model_id: gpt-5.4\n"
                    "    independence_domain: openai\n"
                ),
            )

            registry = review.load_model_identity_registry(config_root)

            missing_builder = review.select_foreign_reviewer(
                registry=registry,
                builder_executor="copilot",
                builder_model_id=None,
                review_executor="codex",
                review_model_id="gpt-5.4",
                tier="shareable",
            )
            self.assertEqual(missing_builder["state"], "absent")

            missing_reviewer = review.select_foreign_reviewer(
                registry=registry,
                builder_executor="copilot",
                builder_model_id="claude-haiku-4.5",
                review_executor="codex",
                review_model_id=None,
                tier="shareable",
            )
            self.assertEqual(missing_reviewer["state"], "absent")

            unknown_identity = review.select_foreign_reviewer(
                registry=registry,
                builder_executor="copilot",
                builder_model_id="claude-haiku-4.5",
                review_executor="copilot",
                review_model_id="unknown",
                tier="shareable",
            )
            self.assertEqual(unknown_identity["state"], "absent")

            same_domain = review.select_foreign_reviewer(
                registry=registry,
                builder_executor="copilot",
                builder_model_id="claude-haiku-4.5",
                review_executor="claude",
                review_model_id="claude-sonnet-4.5",
                tier="shareable",
            )
            self.assertEqual(same_domain["state"], "absent")

            ready = review.select_foreign_reviewer(
                registry=registry,
                builder_executor="copilot",
                builder_model_id="claude-haiku-4.5",
                review_executor="codex",
                review_model_id="gpt-5.4",
                tier="shareable",
            )
            self.assertEqual(ready["state"], "ready")
            self.assertEqual(ready["reviewer"]["independence_domain"], "openai")

    def test_non_shareable_tier_goes_directly_needs_human(self) -> None:
        from paulsha_cortex.coordinator import review

        with tempfile.TemporaryDirectory() as d:
            config_root = Path(d)
            _write_model_identities(
                config_root,
                (
                    "schema_version: 1\n"
                    "identities:\n"
                    "  - executor: copilot\n"
                    "    model_id: claude-haiku-4.5\n"
                    "    independence_domain: anthropic\n"
                    "  - executor: codex\n"
                    "    model_id: gpt-5.4\n"
                    "    independence_domain: openai\n"
                ),
            )
            registry = review.load_model_identity_registry(config_root)

            decision = review.select_foreign_reviewer(
                registry=registry,
                builder_executor="copilot",
                builder_model_id="claude-haiku-4.5",
                review_executor="codex",
                review_model_id="gpt-5.4",
                tier="work",
            )

            self.assertEqual(decision["state"], "needs_human")

    def test_model_identity_file_is_strict_and_fail_closed(self) -> None:
        from paulsha_cortex.coordinator import review

        with tempfile.TemporaryDirectory() as d:
            config_root = Path(d)

            with self.assertRaisesRegex(ValueError, "model-identities"):
                review.load_model_identity_registry(config_root)

            _write_model_identities(
                config_root,
                (
                    "schema_version: 1\n"
                    "identities:\n"
                    "  - executor: codex\n"
                    "    model_id: gpt-5.4\n"
                    "    independence_domain: openai\n"
                    "  - executor: codex\n"
                    "    model_id: gpt-5.4\n"
                    "    independence_domain: openai\n"
                ),
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                review.load_model_identity_registry(config_root)

            _write_model_identities(
                config_root,
                (
                    "schema_version: 1\n"
                    "identities:\n"
                    "  - executor: codex\n"
                    "    model_id: gpt-5.4\n"
                    "    independence_domain: openai\n"
                    "    unexpected: boom\n"
                ),
            )
            with self.assertRaisesRegex(ValueError, "unexpected"):
                review.load_model_identity_registry(config_root)

            _write_model_identities(
                config_root,
                (
                    "schema_version: 1\n"
                    "schema_version: 1\n"
                    "identities:\n"
                    "  - executor: codex\n"
                    "    model_id: gpt-5.4\n"
                    "    model_id: gpt-5.4\n"
                    "    independence_domain: openai\n"
                ),
            )
            with self.assertRaisesRegex(ValueError, "duplicate key"):
                review.load_model_identity_registry(config_root)

    def test_prepare_review_worktree_rejects_preseeded_verdict_file(self) -> None:
        from paulsha_cortex.coordinator import review

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree_path = review.review_worktree_path(
                repo_root=root,
                slice_id="slice-a",
                reviewer_job_id="slice-a-2",
            )

            def fake_runner(argv, **kwargs):
                worktree_path.mkdir(parents=True, exist_ok=True)
                (worktree_path / ".psc-review-verdict.json").write_text("{}", encoding="utf-8")
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with self.assertRaisesRegex(RuntimeError, "preseeded"):
                review.prepare_review_worktree(
                    repo_root=root,
                    slice_id="slice-a",
                    reviewer_job_id="slice-a-2",
                    candidate="a" * 40,
                    subprocess_runner=fake_runner,
                    git_runner=lambda args: type("R", (), {"returncode": 0, "stdout": "a" * 40, "stderr": ""})(),
                )

    def test_prepare_review_worktree_rejects_preseeded_verdict_symlink(self) -> None:
        from paulsha_cortex.coordinator import review

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree_path = review.review_worktree_path(
                repo_root=root,
                slice_id="slice-a",
                reviewer_job_id="slice-a-2",
            )

            def fake_runner(argv, **kwargs):
                worktree_path.mkdir(parents=True, exist_ok=True)
                (worktree_path / ".psc-review-verdict.json").symlink_to(worktree_path / "missing.json")
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with self.assertRaisesRegex(RuntimeError, "preseeded"):
                review.prepare_review_worktree(
                    repo_root=root,
                    slice_id="slice-a",
                    reviewer_job_id="slice-a-2",
                    candidate="a" * 40,
                    subprocess_runner=fake_runner,
                    git_runner=lambda args: type("R", (), {"returncode": 0, "stdout": "a" * 40, "stderr": ""})(),
                )


class ReviewVerdictValidationTests(unittest.TestCase):
    def test_verdict_validation_enforces_schema_and_stable_finding_ids(self) -> None:
        from paulsha_cortex.coordinator import review

        verdict = review.validate_review_verdict(
            {
                "schema_version": 1,
                "builder_job_id": "slice-a-1",
                "reviewer_job_id": "slice-a-2",
                "candidate": "b" * 40,
                "launch_identity": {
                    "executor": "codex",
                    "model_id": "gpt-5.4",
                    "independence_domain": "openai",
                },
                "findings": [
                    {
                        "category": "correctness",
                        "severity": "critical",
                        "summary": "wrong exit status",
                        "evidence": [
                            {"path": "paulsha_cortex/x.py", "line": 9, "detail": "returns 1"},
                            {"path": "tests/test_x.py", "line": 33, "detail": "expects 0"},
                        ],
                        "recommendation": "fix return value",
                    }
                ],
            },
            builder_job_id="slice-a-1",
            reviewer_job_id="slice-a-2",
            candidate="b" * 40,
            launch_identity={
                "executor": "codex",
                "model_id": "gpt-5.4",
                "independence_domain": "openai",
            },
        )

        finding = verdict["findings"][0]
        expected_id = sha256(
            json.dumps(
                {
                    "category": "correctness",
                    "summary": "wrong exit status",
                    "evidence": [
                        {"path": "paulsha_cortex/x.py", "line": 9, "detail": "returns 1"},
                        {"path": "tests/test_x.py", "line": 33, "detail": "expects 0"},
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(finding["finding_id"], expected_id)
        self.assertEqual(verdict["state"], "rejected")

        same_id = review.validate_review_verdict(
            {
                "schema_version": 1,
                "builder_job_id": "slice-a-1",
                "reviewer_job_id": "slice-a-2",
                "candidate": "b" * 40,
                "launch_identity": {
                    "executor": "codex",
                    "model_id": "gpt-5.4",
                    "independence_domain": "openai",
                },
                "findings": [
                    {
                        "category": "correctness",
                        "severity": "minor",
                        "summary": "wrong exit status",
                        "evidence": [
                            {"path": "tests/test_x.py", "line": 33, "detail": "expects 0"},
                            {"path": "paulsha_cortex/x.py", "line": 9, "detail": "returns 1"},
                        ],
                        "recommendation": "different wording should not matter",
                    }
                ],
            },
            builder_job_id="slice-a-1",
            reviewer_job_id="slice-a-2",
            candidate="b" * 40,
            launch_identity={
                "executor": "codex",
                "model_id": "gpt-5.4",
                "independence_domain": "openai",
            },
        )["findings"][0]["finding_id"]
        self.assertEqual(finding["finding_id"], same_id)

    def test_verdict_rejects_duplicate_finding_id_in_single_verdict(self) -> None:
        from paulsha_cortex.coordinator import review

        with self.assertRaisesRegex(ValueError, "duplicate finding_id"):
            review.validate_review_verdict(
                {
                    "schema_version": 1,
                    "builder_job_id": "slice-a-1",
                    "reviewer_job_id": "slice-a-2",
                    "candidate": "b" * 40,
                    "launch_identity": {
                        "executor": "codex",
                        "model_id": "gpt-5.4",
                        "independence_domain": "openai",
                    },
                    "findings": [
                        {
                            "category": "correctness",
                            "severity": "critical",
                            "summary": "wrong exit status",
                            "evidence": [
                                {"path": "paulsha_cortex/x.py", "line": 9, "detail": "returns 1"},
                            ],
                            "recommendation": "fix return value",
                        },
                        {
                            "category": "correctness",
                            "severity": "minor",
                            "summary": "wrong exit status",
                            "evidence": [
                                {"path": "paulsha_cortex/x.py", "line": 9, "detail": "returns 1"},
                            ],
                            "recommendation": "different recommendation should still be duplicate",
                        },
                    ],
                },
                builder_job_id="slice-a-1",
                reviewer_job_id="slice-a-2",
                candidate="b" * 40,
                launch_identity={
                    "executor": "codex",
                    "model_id": "gpt-5.4",
                    "independence_domain": "openai",
                },
            )

    def test_style_only_verdict_is_non_blocking(self) -> None:
        from paulsha_cortex.coordinator import review

        verdict = review.validate_review_verdict(
            {
                "schema_version": 1,
                "builder_job_id": "slice-a-1",
                "reviewer_job_id": "slice-a-2",
                "candidate": "b" * 40,
                "launch_identity": {
                    "executor": "codex",
                    "model_id": "gpt-5.4",
                    "independence_domain": "openai",
                },
                "findings": [
                    {
                        "category": "style",
                        "severity": "minor",
                        "summary": "naming could be clearer",
                        "evidence": [{"path": "x.py", "line": 1, "detail": "short variable"}],
                        "recommendation": "rename variable",
                    }
                ],
            },
            builder_job_id="slice-a-1",
            reviewer_job_id="slice-a-2",
            candidate="b" * 40,
            launch_identity={
                "executor": "codex",
                "model_id": "gpt-5.4",
                "independence_domain": "openai",
            },
        )

        self.assertEqual(verdict["state"], "passed")

    def test_verdict_evidence_accepts_null_line_and_rejects_absolute_path(self) -> None:
        from paulsha_cortex.coordinator import review

        verdict = review.validate_review_verdict(
            {
                "schema_version": 1,
                "builder_job_id": "slice-a-1",
                "reviewer_job_id": "slice-a-2",
                "candidate": "b" * 40,
                "launch_identity": {
                    "executor": "codex",
                    "model_id": "gpt-5.4",
                    "independence_domain": "openai",
                },
                "findings": [
                    {
                        "category": "style",
                        "severity": "minor",
                        "summary": "naming could be clearer",
                        "evidence": [
                            {"path": "x.py", "line": None, "detail": "module level"},
                            {"path": "x.py", "line": 7, "detail": "second line"},
                        ],
                        "recommendation": "rename variable",
                    }
                ],
            },
            builder_job_id="slice-a-1",
            reviewer_job_id="slice-a-2",
            candidate="b" * 40,
            launch_identity={
                "executor": "codex",
                "model_id": "gpt-5.4",
                "independence_domain": "openai",
            },
        )
        self.assertIsNone(verdict["findings"][0]["evidence"][0]["line"])

        with self.assertRaisesRegex(ValueError, "repo-relative"):
            review.validate_review_verdict(
                {
                    "schema_version": 1,
                    "builder_job_id": "slice-a-1",
                    "reviewer_job_id": "slice-a-2",
                    "candidate": "b" * 40,
                    "launch_identity": {
                        "executor": "codex",
                        "model_id": "gpt-5.4",
                        "independence_domain": "openai",
                    },
                    "findings": [
                        {
                            "category": "style",
                            "severity": "minor",
                            "summary": "naming could be clearer",
                            "evidence": [{"path": "/etc/passwd", "line": 1, "detail": "bad"}],
                            "recommendation": "rename variable",
                        }
                    ],
                },
                builder_job_id="slice-a-1",
                reviewer_job_id="slice-a-2",
                candidate="b" * 40,
                launch_identity={
                    "executor": "codex",
                    "model_id": "gpt-5.4",
                    "independence_domain": "openai",
                },
            )

    def test_verdict_missing_provenance_or_malformed_json_is_rejected(self) -> None:
        from paulsha_cortex.coordinator import review

        with self.assertRaisesRegex(ValueError, "builder_job_id"):
            review.validate_review_verdict(
                {
                    "schema_version": 1,
                    "reviewer_job_id": "slice-a-2",
                    "candidate": "b" * 40,
                    "launch_identity": {
                        "executor": "codex",
                        "model_id": "gpt-5.4",
                        "independence_domain": "openai",
                    },
                    "findings": [],
                },
                builder_job_id="slice-a-1",
                reviewer_job_id="slice-a-2",
                candidate="b" * 40,
                launch_identity={
                    "executor": "codex",
                    "model_id": "gpt-5.4",
                    "independence_domain": "openai",
                },
            )

        with tempfile.TemporaryDirectory() as d:
            verdict_path = Path(d) / ".psc-review-verdict.json"
            verdict_path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON"):
                review.read_review_verdict_file(
                    verdict_path,
                    builder_job_id="slice-a-1",
                    reviewer_job_id="slice-a-2",
                    candidate="b" * 40,
                    launch_identity={
                        "executor": "codex",
                        "model_id": "gpt-5.4",
                        "independence_domain": "openai",
                    },
                )

    def test_absent_gate_evaluations_are_versioned_by_builder_and_candidate(self) -> None:
        from paulsha_cortex.coordinator import review

        with tempfile.TemporaryDirectory() as d:
            first = review.write_gate_evaluation(
                review.build_gate_evaluation(
                    slice_id="slice-a",
                    state="absent",
                    reason="same-independence-domain",
                    builder_job_id="slice-a-1",
                    reviewer_job_id=None,
                    candidate="a" * 40,
                    launch_identity={"builder": None, "reviewer": None},
                    findings=[],
                ),
                coordinator_root=d,
            )
            second = review.write_gate_evaluation(
                review.build_gate_evaluation(
                    slice_id="slice-a",
                    state="absent",
                    reason="same-independence-domain",
                    builder_job_id="slice-a-2",
                    reviewer_job_id=None,
                    candidate="b" * 40,
                    launch_identity={"builder": None, "reviewer": None},
                    findings=[],
                ),
                coordinator_root=d,
            )

            self.assertNotEqual(first["path"], second["path"])
