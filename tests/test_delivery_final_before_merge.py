"""Issue #220: final attestation 必須先於 merge mutation。

驗收條件：
1. ship/merge transition 要求 exact candidate final verdict 已存在。
2. verdict 的 authority／candidate／reviewer identity 與 current run 完全相符。
3. remote PR head、current-head CI、review threads 與 attestation 皆綠。
4. 不得先 merge 再補 attestation（hippo #18 倒置實案）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import review, verification
from paulsha_cortex.coordinator.claim import load_work_authority, work_authority_digest
from paulsha_cortex.coordinator.delivery import (
    ForeignReviewEvidence,
    ReviewLoop,
    ShipOrchestrator,
)
from paulsha_cortex.coordinator.github_delivery import (
    DeliveryPolicy,
    FinalGateVerdict,
    GitHubDeliveryClient,
    _SHIP_CAPABILITY,
)
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult


HEAD = "a" * 40
MERGE = "b" * 40
HEAD_TREE = "c" * 40


class Result:
    def __init__(self, payload, returncode=0):
        self.returncode = returncode
        self.stdout = json.dumps(payload)
        self.stderr = "" if returncode == 0 else "failed"


class PaginatedResult(Result):
    def __init__(self, pages):
        super().__init__({})
        self.stdout = "\n".join(json.dumps(page) for page in pages)


class FakeRunner:
    """Golden-path remote fixture: PR HEAD green, one closing issue, no blocking thread."""

    def __init__(self, *, review_submitted_at: str = "1970-01-01T00:01:40Z"):
        self.review_submitted_at = review_submitted_at
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        endpoint = " ".join(argv)
        if argv[:4] == ["gh", "pr", "merge", "7"]:
            return Result("")
        if "repos/acme/demo/pulls/7" in endpoint and "reviews" not in endpoint:
            return Result(
                {
                    "head": {"sha": HEAD},
                    "base": {"ref": "main"},
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged_at": "2026-07-17T00:00:00Z",
                    "merge_commit_sha": MERGE,
                }
            )
        if f"commits/{HEAD}/check-runs" in endpoint:
            return PaginatedResult(
                [
                    {
                        "total_count": 1,
                        "check_runs": [
                            {"name": "pytest", "status": "completed", "conclusion": "success"}
                        ],
                    }
                ]
            )
        if f"commits/{HEAD}/statuses" in endpoint:
            return PaginatedResult([[{"context": "legacy/lint", "state": "success"}]])
        if "pulls/7/reviews" in endpoint:
            return PaginatedResult(
                [
                    [
                        {
                            "id": 9,
                            "user": {"login": "copilot-pull-request-reviewer[bot]"},
                            "commit_id": HEAD,
                            "state": "COMMENTED",
                            "body": "clean",
                            "submitted_at": self.review_submitted_at,
                        }
                    ]
                ]
            )
        if " api graphql " in f" {endpoint} ":
            return Result(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "closingIssuesReferences": {
                                    "nodes": [
                                        {"number": 14, "repository": {"nameWithOwner": "acme/demo"}}
                                    ],
                                    "pageInfo": {"hasNextPage": False, "endCursor": "I1"},
                                },
                                "reviewThreads": {
                                    "nodes": [],
                                    "pageInfo": {"hasNextPage": False, "endCursor": "T1"},
                                },
                            }
                        }
                    }
                }
            )
        if f"git/commits/{HEAD}" in endpoint:
            return Result({"tree": {"sha": HEAD_TREE}})
        if f"git/trees/{HEAD_TREE}?recursive=1" in endpoint:
            return Result(
                {
                    "truncated": False,
                    "tree": [
                        {
                            "path": "openspec/changes/archive/2026-07-17-unified-work-lifecycle/tasks.md"
                        }
                    ],
                }
            )
        raise AssertionError(f"unexpected gh call: {endpoint}")


def _policy(expected_head: str = HEAD) -> DeliveryPolicy:
    return DeliveryPolicy(
        expected_head=expected_head,
        required_closing_issues=(14,),
        copilot_review_id=9,
        copilot_requested_at_epoch=1,
    )


# --- GitHubDeliveryClient: evaluate_final_gate / commit_merge phase split ---


def test_commit_merge_rejects_a_verdict_that_was_not_produced_by_evaluate_final_gate() -> None:
    runner = FakeRunner()
    client = GitHubDeliveryClient(runner=runner)
    with pytest.raises(RuntimeError, match="does not authorize"):
        client.commit_merge(
            verdict="not-a-verdict",
            repo="acme/demo",
            pr_number=7,
            change="unified-work-lifecycle",
            expected_head=HEAD,
            authority_digest="digest-1",
            _capability=_SHIP_CAPABILITY,
        )
    assert not any(call[0][:4] == ["gh", "pr", "merge", "7"] for call in runner.calls)


def test_commit_merge_rejects_verdict_bound_to_a_different_authority_digest() -> None:
    runner = FakeRunner()
    client = GitHubDeliveryClient(runner=runner)
    verdict = client.evaluate_final_gate(
        repo="acme/demo",
        pr_number=7,
        change="unified-work-lifecycle",
        policy=_policy(),
        authority_digest="digest-1",
        _capability=_SHIP_CAPABILITY,
    )
    with pytest.raises(RuntimeError, match="does not authorize"):
        client.commit_merge(
            verdict=verdict,
            repo="acme/demo",
            pr_number=7,
            change="unified-work-lifecycle",
            expected_head=HEAD,
            authority_digest="digest-2",
            _capability=_SHIP_CAPABILITY,
        )
    assert not any(call[0][:4] == ["gh", "pr", "merge", "7"] for call in runner.calls)


def test_evaluate_final_gate_then_commit_merge_succeeds_for_matching_identity() -> None:
    runner = FakeRunner()
    client = GitHubDeliveryClient(runner=runner)
    verdict = client.evaluate_final_gate(
        repo="acme/demo",
        pr_number=7,
        change="unified-work-lifecycle",
        policy=_policy(),
        authority_digest="digest-1",
        _capability=_SHIP_CAPABILITY,
    )
    assert isinstance(verdict, FinalGateVerdict)
    facts = client.commit_merge(
        verdict=verdict,
        repo="acme/demo",
        pr_number=7,
        change="unified-work-lifecycle",
        expected_head=HEAD,
        authority_digest="digest-1",
        _capability=_SHIP_CAPABILITY,
    )
    merge_calls = [call for call in runner.calls if call[0][:4] == ["gh", "pr", "merge", "7"]]
    assert len(merge_calls) == 1
    assert facts.head == HEAD


def test_evaluate_final_gate_blocks_and_never_merges_when_remote_head_is_stale() -> None:
    runner = FakeRunner()
    client = GitHubDeliveryClient(runner=runner)
    with pytest.raises(RuntimeError, match="head-race"):
        client.evaluate_final_gate(
            repo="acme/demo",
            pr_number=7,
            change="unified-work-lifecycle",
            policy=_policy(expected_head="f" * 40),
            authority_digest="digest-1",
            _capability=_SHIP_CAPABILITY,
        )
    assert not any(call[0][:4] == ["gh", "pr", "merge", "7"] for call in runner.calls)


# --- ShipOrchestrator: verdict must exist before the merge mutation fires ---

HEAD1 = "1" * 40
HEAD2 = "2" * 40


def _authority(root: Path):
    payload = {
        "schema": "work-items-snapshot/v1",
        "providers": {
            "github": {
                "provider_id": "github",
                "revision": "github-rev-1",
                "last_success_epoch": 150,
                "degraded": False,
            }
        },
        "work_items": [
            {
                "repo": "acme/demo",
                "work_id": "work",
                "mapped_issues": [14],
                "mapped_prs": [7],
                "mapped_openspec": ["work"],
                "mapped_todo_paths": ["docs/todo.md"],
                "confirmed_todo": True,
                "source_revisions": ["issue:14@open", "openspec:work@abc"],
            }
        ],
    }
    path = root / "authority.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_work_authority(repo="acme/demo", work_id="work", snapshot_path=path)


def _preflight() -> PreflightResult:
    command = CommandResult(argv=("ok",), returncode=0, stdout="", stderr="")
    return PreflightResult(
        passed=True,
        failed_stage=None,
        policy=command,
        ci_parity=command,
        head=HEAD1,
        tree_hash=HEAD2,
    )


def _foreign_review(root: Path) -> ForeignReviewEvidence:
    payload = review.build_gate_evaluation(
        slice_id="ship-review",
        state="passed",
        reason="accepted",
        builder_job_id="builder-1",
        reviewer_job_id="reviewer-1",
        candidate=HEAD1,
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


def _copilot_decision():
    return (
        ReviewLoop.start(head=HEAD1, now_epoch=100)
        .mark_requested(head=HEAD1, now_epoch=100)
        .record_review(
            head=HEAD1,
            now_epoch=110,
            finding_count=0,
            review_id=77,
            submitted_at_epoch=110,
        )
    )


def test_ship_orchestrator_produces_final_verdict_before_committing_merge(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    authority = _authority(tmp_path)
    expected_digest = work_authority_digest(authority)

    class GitHub:
        def evaluate_final_gate(self, **kwargs):
            calls.append("evaluate_final_gate")
            assert kwargs["authority_digest"] == expected_digest
            return {"stage": "verdict", "authority_digest": kwargs["authority_digest"]}

        def commit_merge(self, **kwargs):
            calls.append("commit_merge")
            assert kwargs["verdict"] == {
                "stage": "verdict",
                "authority_digest": expected_digest,
            }
            assert kwargs["authority_digest"] == expected_digest
            return object()

    orchestrator = ShipOrchestrator(github=GitHub(), now=lambda: 200)
    result = orchestrator.merge_if_ready(
        repo="acme/demo",
        pr_number=7,
        change="work",
        expected_head=HEAD1,
        expected_tree_hash=HEAD2,
        authority=authority,
        preflight=_preflight(),
        copilot=_copilot_decision(),
        foreign_review=_foreign_review(tmp_path),
    )
    assert calls == ["evaluate_final_gate", "commit_merge"]
    assert result.expected_head == HEAD1


def test_ship_orchestrator_never_commits_merge_when_final_gate_evaluation_fails(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)

    class GitHub:
        def evaluate_final_gate(self, **kwargs):
            raise RuntimeError("delivery gate blocked: checks-not-terminal-green")

        def commit_merge(self, **kwargs):
            raise AssertionError("commit_merge must not be reached when the final gate fails")

    orchestrator = ShipOrchestrator(github=GitHub(), now=lambda: 200)
    with pytest.raises(RuntimeError, match="checks-not-terminal-green"):
        orchestrator.merge_if_ready(
            repo="acme/demo",
            pr_number=7,
            change="work",
            expected_head=HEAD1,
            expected_tree_hash=HEAD2,
            authority=authority,
            preflight=_preflight(),
            copilot=_copilot_decision(),
            foreign_review=_foreign_review(tmp_path),
        )
