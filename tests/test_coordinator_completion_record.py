from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.coordinator import autonomy, completion, review, verification


def _git_ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _git_nonzero(returncode: int = 1) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="", stderr="")


def _write_review_eval(root: Path, *, slice_id: str, candidate: str) -> dict:
    payload = review.build_gate_evaluation(
        slice_id=slice_id,
        state="passed",
        reason="accepted",
        builder_job_id="builder-1",
        reviewer_job_id="reviewer-1",
        candidate=candidate,
        launch_identity={
            "builder": {
                "executor": "copilot",
                "model_id": "builder-model",
                "independence_domain": "builder-domain",
            },
            "reviewer": {
                "executor": "codex",
                "model_id": "reviewer-model",
                "independence_domain": "reviewer-domain",
            },
        },
    )
    return review.write_gate_evaluation(payload, coordinator_root=root)


def _write_verification(root: Path, *, slice_id: str, candidate: str, status: str = "verified") -> dict:
    return verification.write_verification_evidence(
        {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": slice_id,
            "candidate": candidate,
            "status": status,
            "summary": "verification-succeeded",
            "details": {"ok": True},
        },
        coordinator_root=root,
    )


def _completion_payload(
    *,
    slice_id: str,
    candidate: str,
    target_sha: str,
    verification_ref: dict,
    review_policy: str,
    docs_class: str,
    reviewer_job_id: str | None,
    review_eval_ref: dict | None,
) -> dict:
    return {
        "schema_version": completion.COMPLETION_SCHEMA_VERSION,
        "slice_id": slice_id,
        "spec_hash": "1" * 64,
        "plan_hash": "2" * 64,
        "verification_hash": "3" * 64,
        "builder_job_id": "builder-1",
        "reviewer_job_id": reviewer_job_id,
        "dispatch_base": "a" * 40,
        "candidate": candidate,
        "target_branch": "main",
        "target_remote": "origin",
        "target_ref": "refs/remotes/origin/main",
        "target_ref_sha": target_sha,
        "verification_evidence_path": verification_ref["path"],
        "verification_evidence_hash": verification_ref["hash"],
        "review_policy": review_policy,
        "docs_class": docs_class,
        "review_evaluation_path": None if review_eval_ref is None else review_eval_ref["path"],
        "review_evaluation_hash": None if review_eval_ref is None else review_eval_ref["hash"],
        "completed_at": "2026-07-12T00:00:00+00:00",
    }


def _write_manifest(
    handoff_dir: Path,
    *,
    slice_id: str,
    record_ref: dict,
    slice_state: str,
    spec_hash: str = "1" * 64,
    plan_hash: str = "2" * 64,
    verification_hash: str = "3" * 64,
) -> Path:
    path = handoff_dir / f"{slice_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "slice_id": slice_id,
                "job_id": "builder-1",
                "gate_status": "passed",
                "completion": "exited",
                "exit_code": 0,
                "branch": f"feature/{slice_id}",
                "gate_reason": "candidate-merged",
                "gate_verdict": None,
                "verification_evidence_path": None,
                "verification_evidence_hash": None,
                "review_evaluation_path": None,
                "review_evaluation_hash": None,
                "completion_record_path": record_ref["path"],
                "completion_record_hash": record_ref["hash"],
                "slice_state": slice_state,
                "spec_hash": spec_hash,
                "plan_hash": plan_hash,
                "verification_hash": verification_hash,
                "completed_at": "2026-07-12T00:00:00+00:00",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


class CompletionRecordValidationTests(unittest.TestCase):
    @staticmethod
    def _work_authority(*, review_kind: str = "copilot") -> dict:
        return {
            "repo": "acme/demo",
            "work_id": "work",
            "snapshot_hash": "0" * 64,
            "provider_id": "github",
            "provider_revision": "github-rev-1",
            "source_revisions": ["issue:14@closed"],
            "mapped_issues": [14],
            "mapped_prs": [7],
            "mapped_openspec": ["work"],
            "mapped_todo_paths": ["docs/todo.md"],
            "pr_number": 7,
            "change": "work",
            "todo_paths": ["docs/todo.md"],
            "merge_commit": "a" * 40,
            "run_id": "run-1",
            "workflow_step_ids": ["step-ship"],
            "trusted_evidence_refs": [
                {"kind": "preflight", "ref": "tree:" + "b" * 40, "hash": "1" * 64},
                {"kind": "foreign_review", "ref": "/review.json", "hash": "2" * 64},
                {"kind": review_kind, "ref": "/delivery-review.json", "hash": "3" * 64},
                {"kind": "merge_authorization", "ref": "/authorization.json", "hash": "4" * 64},
            ],
        }

    def test_work_authority_accepts_maintainer_review_as_delivery_authority(self) -> None:
        normalized = completion._normalize_work_authority(
            self._work_authority(review_kind="maintainer-review")
        )

        self.assertEqual(
            {item["kind"] for item in normalized["trusted_evidence_refs"]},
            {"preflight", "foreign_review", "maintainer-review", "merge_authorization"},
        )

    def test_work_authority_rejects_both_delivery_review_authorities(self) -> None:
        authority = self._work_authority(review_kind="maintainer-review")
        authority["trusted_evidence_refs"].append(
            {"kind": "copilot", "ref": "github-review:9", "hash": "5" * 64}
        )

        with self.assertRaisesRegex(ValueError, "refs incomplete"):
            completion._normalize_work_authority(authority)

    def test_work_authority_rejects_unclosed_multi_target_refs(self) -> None:
        authority = {
            "repo": "acme/demo",
            "work_id": "work",
            "snapshot_hash": "0" * 64,
            "provider_id": "github",
            "provider_revision": "github-rev-1",
            "source_revisions": ["issue:14@closed"],
            "mapped_issues": [14],
            "mapped_prs": [7, 8],
            "mapped_openspec": ["work"],
            "mapped_todo_paths": ["docs/todo.md"],
            "pr_number": 7,
            "change": "work",
            "todo_paths": ["docs/todo.md"],
            "merge_commit": "a" * 40,
            "run_id": "run-1",
            "workflow_step_ids": ["step-ship"],
            "trusted_evidence_refs": [],
        }
        with self.assertRaisesRegex(ValueError, "refs invalid"):
            completion._normalize_work_authority(authority)

    def test_required_policy_requires_reviewer_identity_and_eval_refs(self) -> None:
        payload = _completion_payload(
            slice_id="slice-required",
            candidate="b" * 40,
            target_sha="c" * 40,
            verification_ref={"path": "/tmp/verify.json", "hash": "4" * 64},
            review_policy="required",
            docs_class="code",
            reviewer_job_id=None,
            review_eval_ref=None,
        )
        with self.assertRaisesRegex(ValueError, "reviewer_job_id"):
            completion.validate_completion_record(payload)

    def test_not_required_policy_rejects_non_null_reviewer_refs(self) -> None:
        payload = _completion_payload(
            slice_id="slice-informational",
            candidate="b" * 40,
            target_sha="c" * 40,
            verification_ref={"path": "/tmp/verify.json", "hash": "4" * 64},
            review_policy="not-required",
            docs_class="informational",
            reviewer_job_id="reviewer-1",
            review_eval_ref={"path": "/tmp/review.json", "hash": "5" * 64},
        )
        with self.assertRaisesRegex(ValueError, "not-required"):
            completion.validate_completion_record(payload)


class CompletionRecordReadAndSatisfactionTests(unittest.TestCase):
    def test_read_completion_record_rejects_cross_candidate_verification_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            candidate = "b" * 40
            verification_ref = _write_verification(
                root,
                slice_id="slice-a",
                candidate="c" * 40,
                status="verified",
            )
            normalized = completion.validate_completion_record(
                _completion_payload(
                    slice_id="slice-a",
                    candidate=candidate,
                    target_sha="d" * 40,
                    verification_ref=verification_ref,
                    review_policy="not-required",
                    docs_class="informational",
                    reviewer_job_id=None,
                    review_eval_ref=None,
                )
            )
            record_path = root / "completion.json"
            verification.atomic_write_json(record_path, normalized)

            with self.assertRaisesRegex(ValueError, "verification evidence candidate mismatch"):
                completion.read_completion_record(record_path)

    def test_read_completion_record_rejects_cross_slice_review_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            candidate = "b" * 40
            verification_ref = _write_verification(
                root,
                slice_id="slice-a",
                candidate=candidate,
                status="reviewing",
            )
            review_eval_ref = _write_review_eval(root, slice_id="slice-b", candidate=candidate)
            normalized = completion.validate_completion_record(
                _completion_payload(
                    slice_id="slice-a",
                    candidate=candidate,
                    target_sha="d" * 40,
                    verification_ref=verification_ref,
                    review_policy="required",
                    docs_class="code",
                    reviewer_job_id="reviewer-1",
                    review_eval_ref=review_eval_ref,
                )
            )
            record_path = root / "completion.json"
            verification.atomic_write_json(record_path, normalized)

            with self.assertRaisesRegex(ValueError, "review evaluation slice_id mismatch"):
                completion.read_completion_record(record_path)

    def test_read_completion_record_rejects_symlink_record_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            candidate = "b" * 40
            verification_ref = _write_verification(root, slice_id="slice-a", candidate=candidate, status="reviewing")
            review_eval_ref = _write_review_eval(root, slice_id="slice-a", candidate=candidate)
            record = completion.write_completion_record(
                _completion_payload(
                    slice_id="slice-a",
                    candidate=candidate,
                    target_sha="c" * 40,
                    verification_ref=verification_ref,
                    review_policy="required",
                    docs_class="code",
                    reviewer_job_id="reviewer-1",
                    review_eval_ref=review_eval_ref,
                ),
                coordinator_root=root,
            )
            link_path = root / "record-link.json"
            link_path.symlink_to(Path(record["path"]))
            with self.assertRaisesRegex(ValueError, "must not be symlink"):
                completion.read_completion_record(link_path, expected_hash=record["hash"])

    def test_read_completion_record_rejects_symlink_reference_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            candidate = "b" * 40
            verification_ref = _write_verification(root, slice_id="slice-a", candidate=candidate, status="verified")
            verification_link = root / "verification-link.json"
            verification_link.symlink_to(Path(verification_ref["path"]))
            payload = _completion_payload(
                slice_id="slice-a",
                candidate=candidate,
                target_sha="c" * 40,
                verification_ref={"path": str(verification_link), "hash": verification_ref["hash"]},
                review_policy="not-required",
                docs_class="informational",
                reviewer_job_id=None,
                review_eval_ref=None,
            )
            normalized = completion.validate_completion_record(payload)
            record_hash = verification.canonical_json_hash(normalized)
            record_path = root / "evidence" / "completion" / "slice-a-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json"
            verification.atomic_write_json(record_path, normalized)
            with self.assertRaisesRegex(ValueError, "verification_evidence_path"):
                completion.read_completion_record(record_path, expected_hash=record_hash)

    def test_load_completion_requires_completed_slice_state_and_matching_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            handoff_dir = root / "handoff"
            slice_id = "slice-a"
            candidate = "b" * 40
            target_sha = "c" * 40
            verification_ref = _write_verification(root, slice_id=slice_id, candidate=candidate, status="verified")
            record = completion.write_completion_record(
                _completion_payload(
                    slice_id=slice_id,
                    candidate=candidate,
                    target_sha=target_sha,
                    verification_ref=verification_ref,
                    review_policy="not-required",
                    docs_class="informational",
                    reviewer_job_id=None,
                    review_eval_ref=None,
                ),
                coordinator_root=root,
            )
            _write_manifest(handoff_dir, slice_id=slice_id, record_ref=record, slice_state="verified")

            def git_runner(args: list[str]):
                key = tuple(args)
                mapping = {
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/main"): _git_ok(target_sha),
                    ("-C", str(root), "merge-base", "--is-ancestor", candidate, target_sha): _git_ok(""),
                }
                return mapping[key]

            self.assertIsNone(
                completion.load_completion_from_handoff(
                    slice_id,
                    handoff_dir=str(handoff_dir),
                    repo_root=root,
                    git_runner=git_runner,
                )
            )

            _write_manifest(
                handoff_dir,
                slice_id=slice_id,
                record_ref=record,
                slice_state="completed",
                spec_hash="f" * 64,
            )
            self.assertIsNone(
                completion.load_completion_from_handoff(
                    slice_id,
                    handoff_dir=str(handoff_dir),
                    repo_root=root,
                    git_runner=git_runner,
                )
            )

            _write_manifest(handoff_dir, slice_id=slice_id, record_ref=record, slice_state="completed")
            loaded = completion.load_completion_from_handoff(
                slice_id,
                handoff_dir=str(handoff_dir),
                repo_root=root,
                git_runner=git_runner,
            )
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["candidate"], candidate)

    def test_default_is_satisfied_tracks_completion_record_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            handoff_dir = root / "handoff"
            slice_id = "slice-a"
            candidate = "b" * 40
            target_sha = "c" * 40
            verification_ref = _write_verification(root, slice_id=slice_id, candidate=candidate, status="verified")
            record = completion.write_completion_record(
                _completion_payload(
                    slice_id=slice_id,
                    candidate=candidate,
                    target_sha=target_sha,
                    verification_ref=verification_ref,
                    review_policy="not-required",
                    docs_class="informational",
                    reviewer_job_id=None,
                    review_eval_ref=None,
                ),
                coordinator_root=root,
            )
            _write_manifest(handoff_dir, slice_id=slice_id, record_ref=record, slice_state="completed")

            def merged_runner(args: list[str]):
                key = tuple(args)
                mapping = {
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/main"): _git_ok(target_sha),
                    ("-C", str(root), "merge-base", "--is-ancestor", candidate, target_sha): _git_ok(""),
                }
                return mapping[key]

            def stale_runner(args: list[str]):
                key = tuple(args)
                mapping = {
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/main"): _git_ok(target_sha),
                    ("-C", str(root), "merge-base", "--is-ancestor", candidate, target_sha): _git_nonzero(1),
                }
                return mapping[key]

            self.assertTrue(
                autonomy.default_is_satisfied(
                    slice_id,
                    handoff_dir=str(handoff_dir),
                    repo_root=root,
                    git_runner=merged_runner,
                )
            )
            self.assertFalse(
                autonomy.default_is_satisfied(
                    slice_id,
                    handoff_dir=str(handoff_dir),
                    repo_root=root,
                    git_runner=stale_runner,
                )
            )


if __name__ == "__main__":
    unittest.main()
