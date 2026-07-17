from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import completion, review, verification
from paulsha_cortex.coordinator.claim import load_work_authority
from paulsha_cortex.coordinator.delivery import (
    ArchiveGateFacts,
    PullRequestMetadata,
    ReviewLoop,
    ForeignReviewEvidence,
    ShipOrchestrator,
    build_openspec_archive_argv,
    validate_archive_gate,
    validate_pr_metadata,
)
from paulsha_cortex.coordinator.github_delivery import RemoteClosureFacts
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult


HEAD1 = "a" * 40
HEAD2 = "b" * 40
HEAD3 = "c" * 40


def _authority(root: Path, *, last_success: float = 150, issues=(14,)):
    payload = {
        "schema": "work-items-snapshot/v1",
        "providers": {
            "github": {
                "provider_id": "github",
                "revision": "github-rev-1",
                "last_success_epoch": last_success,
                "degraded": False,
            }
        },
        "work_items": [
            {
                "repo": "acme/demo",
                "work_id": "work",
                "mapped_issues": list(issues),
                "mapped_prs": [7],
                "mapped_openspec": ["work"],
                "mapped_todo_paths": ["docs/todo.md"],
                "confirmed_todo": True,
                "source_revisions": ["issue:14@open", "openspec:work@abc"],
            }
        ],
    }
    path = root / f"authority-{last_success}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_work_authority(repo="acme/demo", work_id="work", snapshot_path=path)


def test_archive_gate_requires_tasks_specs_docs_and_changelog() -> None:
    facts = ArchiveGateFacts(
        tasks_complete=True,
        canonical_specs_valid=True,
        doc_references_valid=True,
        changelog_present=True,
    )
    assert validate_archive_gate(facts).allowed
    for field in (
        "tasks_complete",
        "canonical_specs_valid",
        "doc_references_valid",
        "changelog_present",
    ):
        result = validate_archive_gate(replace(facts, **{field: False}))
        assert not result.allowed


def test_official_openspec_archive_argv_is_non_interactive_and_typed() -> None:
    assert build_openspec_archive_argv("unified-work-lifecycle") == [
        "openspec",
        "archive",
        "-y",
        "unified-work-lifecycle",
    ]
    with pytest.raises(ValueError, match="safe slug"):
        build_openspec_archive_argv("../escape")


def test_pr_metadata_requires_zh_tw_conventional_title_and_closing_keywords() -> None:
    metadata = PullRequestMetadata(
        title="feat(workflow): 建立統一工作生命週期",
        body="## 摘要\n\n完成工作流程。\n\nCloses #14\nFixes #5\n",
        labels=("enhancement",),
    )
    assert validate_pr_metadata(metadata=metadata, required_issues=(14, 5)).allowed
    assert not validate_pr_metadata(
        metadata=replace(metadata, title="feat(workflow): unified lifecycle"),
        required_issues=(14, 5),
    ).allowed
    assert not validate_pr_metadata(
        metadata=replace(metadata, body="Relates to #14\nFixes #5"),
        required_issues=(14, 5),
    ).allowed


def test_review_loop_invalidates_every_push_and_allows_two_fix_rounds() -> None:
    loop = ReviewLoop.start(head=HEAD1, now_epoch=100)
    assert loop.needs_request
    loop = loop.mark_requested(head=HEAD1, now_epoch=100)
    first = loop.record_review(
        head=HEAD1, now_epoch=110, finding_count=1, review_id=1, submitted_at_epoch=110
    )
    assert first.action == "fix_required"
    loop = first.loop.advance_after_fix(head=HEAD2, now_epoch=120)
    assert loop.needs_request
    second = loop.mark_requested(head=HEAD2, now_epoch=120).record_review(
        head=HEAD2,
        now_epoch=130,
        finding_count=1,
        review_id=2,
        submitted_at_epoch=130,
    )
    assert second.action == "fix_required"
    loop = second.loop.advance_after_fix(head=HEAD3, now_epoch=140)
    third = loop.mark_requested(head=HEAD3, now_epoch=140).record_review(
        head=HEAD3,
        now_epoch=150,
        finding_count=1,
        review_id=3,
        submitted_at_epoch=150,
    )
    assert third.action == "needs_human"
    assert third.reason == "copilot-finding-budget-exhausted"


def test_review_loop_accepts_clean_current_head_and_rejects_old_or_error_review() -> None:
    loop = ReviewLoop.start(head=HEAD1, now_epoch=100).mark_requested(
        head=HEAD1,
        now_epoch=100,
    )
    assert loop.record_review(
        head=HEAD1,
        now_epoch=110,
        finding_count=0,
        review_id=1,
        submitted_at_epoch=110,
    ).action == "passed"
    assert loop.record_review(
        head=HEAD2,
        now_epoch=110,
        finding_count=0,
        review_id=2,
        submitted_at_epoch=110,
    ).action == "needs_human"
    assert loop.record_review(
        head=HEAD1,
        now_epoch=110,
        finding_count=0,
        review_id=3,
        submitted_at_epoch=110,
        error=True,
    ).action == "needs_human"


def test_review_loop_times_out_at_fifteen_minutes() -> None:
    loop = ReviewLoop.start(head=HEAD1, now_epoch=100).mark_requested(
        head=HEAD1,
        now_epoch=100,
    )
    result = loop.record_review(
        head=HEAD1,
        now_epoch=1001,
        finding_count=0,
        review_id=1,
        submitted_at_epoch=1001,
    )
    assert result.action == "needs_human"
    assert result.reason == "copilot-review-timeout"


def test_review_loop_requires_request_before_review() -> None:
    with pytest.raises(ValueError, match="not requested"):
        ReviewLoop.start(head=HEAD1, now_epoch=100).record_review(
            head=HEAD1,
            now_epoch=101,
            finding_count=0,
            review_id=1,
            submitted_at_epoch=101,
        )


def test_repeated_request_does_not_extend_current_head_deadline() -> None:
    loop = ReviewLoop.start(head=HEAD1, now_epoch=100).mark_requested(
        head=HEAD1,
        now_epoch=100,
    )
    assert loop.mark_requested(head=HEAD1, now_epoch=800).requested_at == 100


def test_review_id_must_belong_to_current_request_epoch() -> None:
    loop = ReviewLoop.start(head=HEAD1, now_epoch=100).mark_requested(
        head=HEAD1,
        now_epoch=100,
    )
    decision = loop.record_review(
        head=HEAD1,
        now_epoch=110,
        finding_count=0,
        review_id=7,
        submitted_at_epoch=99,
    )
    assert decision.action == "needs_human"
    assert decision.reason == "copilot-review-outside-request-epoch"


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), float("-inf")])
def test_review_epochs_require_finite_timestamps(timestamp: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        ReviewLoop.start(head=HEAD1, now_epoch=timestamp)


def _foreign_review(root: Path, *, head: str = HEAD1) -> ForeignReviewEvidence:
    payload = review.build_gate_evaluation(
        slice_id="ship-review",
        state="passed",
        reason="accepted",
        builder_job_id="builder-1",
        reviewer_job_id="reviewer-1",
        candidate=head,
        launch_identity={
            "builder": {
                "executor": "codex",
                "model_id": "builder",
                "independence_domain": "openai",
            },
            "reviewer": {
                "executor": "claude",
                "model_id": "reviewer",
                "independence_domain": "anthropic",
            },
        },
    )
    path = root / "foreign-review.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return ForeignReviewEvidence(
        path=str(path),
        expected_hash=verification.canonical_json_hash(payload),
    )


def _preflight(*, head: str = HEAD1, tree: str = HEAD2) -> PreflightResult:
    command = CommandResult(argv=("ok",), returncode=0, stdout="", stderr="")
    return PreflightResult(
        passed=True,
        failed_stage=None,
        policy=command,
        ci_parity=command,
        head=head,
        tree_hash=tree,
    )


def _copilot_decision(*, head: str = HEAD1):
    return ReviewLoop.start(head=head, now_epoch=100).mark_requested(
        head=head,
        now_epoch=100,
    ).record_review(
        head=head,
        now_epoch=110,
        finding_count=0,
        review_id=77,
        submitted_at_epoch=110,
    )


def test_ship_orchestrator_is_single_exact_evidence_merge_admission(tmp_path: Path) -> None:
    class GitHub:
        def __init__(self):
            self.policy = None

        def merge_if_ready(self, **kwargs):
            self.policy = kwargs["policy"]
            return object()

    github = GitHub()
    orchestrator = ShipOrchestrator(github=github, now=lambda: 200)
    result = orchestrator.merge_if_ready(
        repo="acme/demo",
        pr_number=7,
        change="work",
        expected_head=HEAD1,
        expected_tree_hash=HEAD2,
        authority=_authority(tmp_path),
        preflight=_preflight(),
        copilot=_copilot_decision(),
        foreign_review=_foreign_review(tmp_path),
    )
    assert result.expected_head == HEAD1
    assert github.policy.copilot_review_id == 77
    assert github.policy.copilot_requested_at_epoch == 100


def test_ship_orchestrator_blocks_stale_provider_or_non_exact_preflight(tmp_path: Path) -> None:
    class GitHub:
        def merge_if_ready(self, **kwargs):
            raise AssertionError("merge must not be reached")

    orchestrator = ShipOrchestrator(github=GitHub(), now=lambda: 2_000)
    base = dict(
        repo="acme/demo",
        pr_number=7,
        change="work",
        expected_head=HEAD1,
        expected_tree_hash=HEAD2,
        authority=_authority(tmp_path, last_success=100),
        preflight=_preflight(),
        copilot=_copilot_decision(),
        foreign_review=_foreign_review(tmp_path),
    )
    with pytest.raises(RuntimeError, match="stale"):
        orchestrator.merge_if_ready(**base)
    base["authority"] = _authority(tmp_path, last_success=1_500)
    base["preflight"] = _preflight(head=HEAD3)
    with pytest.raises(RuntimeError, match="exact HEAD/tree"):
        orchestrator.merge_if_ready(**base)


def test_remote_closure_reads_and_validates_completion_record(tmp_path: Path) -> None:
    authority = _authority(tmp_path, last_success=0)
    verification_ref = verification.write_verification_evidence(
        {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": "ship-review",
            "candidate": HEAD1,
            "status": "reviewing",
            "summary": "ok",
            "details": {"ok": True},
        },
        coordinator_root=tmp_path,
    )
    foreign_payload = json.loads(Path(_foreign_review(tmp_path).path).read_text())
    review_ref = review.write_gate_evaluation(foreign_payload, coordinator_root=tmp_path)
    trusted_evidence_refs = [
        {"kind": "preflight", "ref": "tree:" + HEAD2, "hash": "5" * 64},
        {"kind": "foreign_review", "ref": review_ref["path"], "hash": review_ref["hash"]},
        {"kind": "copilot", "ref": "github-review:9", "hash": "6" * 64},
        {"kind": "merge_authorization", "ref": "run:run-1", "hash": "7" * 64},
    ]
    completion_payload = {
        "schema_version": completion.COMPLETION_SCHEMA_VERSION,
        "slice_id": "ship-review",
        "spec_hash": "1" * 64,
        "plan_hash": "2" * 64,
        "verification_hash": "3" * 64,
        "builder_job_id": "builder-1",
        "reviewer_job_id": "reviewer-1",
        "dispatch_base": HEAD3,
        "candidate": HEAD1,
        "target_branch": "main",
        "target_remote": "origin",
        "target_ref": "refs/remotes/origin/main",
        "target_ref_sha": HEAD2,
        "verification_evidence_path": verification_ref["path"],
        "verification_evidence_hash": verification_ref["hash"],
        "review_policy": "required",
        "docs_class": "code",
        "review_evaluation_path": review_ref["path"],
        "review_evaluation_hash": review_ref["hash"],
        "completed_at": "2026-07-17T00:00:00+00:00",
        "work_authority": {
            "repo": authority.repo,
            "work_id": authority.work_id,
            "snapshot_hash": authority.snapshot_hash,
            "provider_id": authority.github_provider_id,
            "provider_revision": authority.github_provider_revision,
            "source_revisions": list(authority.source_revisions),
            "mapped_issues": list(authority.mapped_issues),
            "mapped_prs": list(authority.mapped_prs),
            "mapped_openspec": list(authority.mapped_openspec),
            "mapped_todo_paths": list(authority.mapped_todo_paths),
            "pr_number": 7,
            "change": "work",
            "todo_paths": ["docs/todo.md"],
            "merge_commit": HEAD2,
            "run_id": "run-1",
            "workflow_step_ids": ["step-build", "step-review", "step-ship"],
            "trusted_evidence_refs": trusted_evidence_refs,
        },
    }

    class GitHub:
        def fetch_remote_closure(self, **kwargs):
            return RemoteClosureFacts(
                merge_commit=HEAD2,
                pr_head=HEAD1,
                merge_parents=(HEAD3, HEAD1),
                default_head=HEAD2,
                merge_is_ancestor=True,
                merge_is_merge_commit=True,
                issue_states={14: "closed"},
                active_openspec_absent=True,
                archive_present=True,
                todo_complete=True,
                todo_revisions={"docs/todo.md": HEAD3},
                completion_record_valid=False,
            )

    result = ShipOrchestrator(github=GitHub(), now=lambda: 0).verify_remote_closure(
        repo="acme/demo",
        pr_number=7,
        change="work",
        authority=authority,
        todo_paths=("docs/todo.md",),
        expected_head=HEAD1,
        completion_payload=completion_payload,
        run_id="run-1",
        workflow_step_ids=("step-build", "step-review", "step-ship"),
        trusted_evidence_refs=tuple(trusted_evidence_refs),
        coordinator_root=tmp_path,
    )
    assert result.facts.completion_record_valid
    assert completion.read_completion_record(
        result.completion_record["path"],
        expected_hash=result.completion_record["hash"],
    )["candidate"] == HEAD1


def test_remote_closure_rejects_completion_bound_to_another_work(tmp_path: Path) -> None:
    authority = _authority(tmp_path, last_success=0)

    class GitHub:
        def fetch_remote_closure(self, **kwargs):
            return RemoteClosureFacts(
                merge_commit=HEAD2,
                pr_head=HEAD1,
                merge_parents=(HEAD3, HEAD1),
                default_head=HEAD2,
                merge_is_ancestor=True,
                merge_is_merge_commit=True,
                issue_states={14: "closed"},
                active_openspec_absent=True,
                archive_present=True,
                todo_complete=True,
                todo_revisions={"docs/todo.md": HEAD3},
                completion_record_valid=False,
            )

    # Validation reaches authority binding before evidence references are read.
    payload = {
        "schema_version": completion.COMPLETION_SCHEMA_VERSION,
        "slice_id": "ship-review",
        "spec_hash": "1" * 64,
        "plan_hash": "2" * 64,
        "verification_hash": "3" * 64,
        "builder_job_id": "builder-1",
        "reviewer_job_id": None,
        "dispatch_base": HEAD3,
        "candidate": HEAD1,
        "target_branch": "main",
        "target_remote": "origin",
        "target_ref": "refs/remotes/origin/main",
        "target_ref_sha": HEAD2,
        "verification_evidence_path": "/missing",
        "verification_evidence_hash": "4" * 64,
        "review_policy": "not-required",
        "docs_class": "informational",
        "review_evaluation_path": None,
        "review_evaluation_hash": None,
        "completed_at": "2026-07-17T00:00:00+00:00",
        "work_authority": {
            "repo": "acme/other",
            "work_id": authority.work_id,
            "snapshot_hash": authority.snapshot_hash,
            "provider_id": authority.github_provider_id,
            "provider_revision": authority.github_provider_revision,
            "source_revisions": list(authority.source_revisions),
            "mapped_issues": list(authority.mapped_issues),
            "mapped_prs": list(authority.mapped_prs),
            "mapped_openspec": list(authority.mapped_openspec),
            "mapped_todo_paths": list(authority.mapped_todo_paths),
            "pr_number": 7,
            "change": "work",
            "todo_paths": ["docs/todo.md"],
            "merge_commit": HEAD2,
            "run_id": "run-1",
            "workflow_step_ids": ["step-build", "step-review", "step-ship"],
            "trusted_evidence_refs": [
                {"kind": "preflight", "ref": "tree:" + HEAD2, "hash": "5" * 64},
                {"kind": "foreign_review", "ref": "/missing-review", "hash": "6" * 64},
                {"kind": "copilot", "ref": "github-review:9", "hash": "7" * 64},
                {"kind": "merge_authorization", "ref": "run:run-1", "hash": "8" * 64},
            ],
        },
    }
    with pytest.raises(RuntimeError, match="WorkAuthority"):
        ShipOrchestrator(github=GitHub(), now=lambda: 0).verify_remote_closure(
            repo="acme/demo",
            pr_number=7,
            change="work",
            authority=authority,
            todo_paths=("docs/todo.md",),
            expected_head=HEAD1,
            completion_payload=payload,
            run_id="run-1",
            workflow_step_ids=("step-build", "step-review", "step-ship"),
            trusted_evidence_refs=tuple(payload["work_authority"]["trusted_evidence_refs"]),
            coordinator_root=tmp_path,
        )
