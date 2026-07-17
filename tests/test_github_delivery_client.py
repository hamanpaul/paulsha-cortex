from __future__ import annotations

import json

import pytest

from paulsha_cortex.coordinator.github_delivery import (
    DeliveryPolicy,
    GitHubDeliveryClient,
)


HEAD = "a" * 40
MERGE = "b" * 40
HEAD_TREE = "c" * 40
MAIN_TREE = "d" * 40


class Result:
    def __init__(self, payload, returncode=0):
        self.returncode = returncode
        self.stdout = json.dumps(payload)
        self.stderr = "" if returncode == 0 else "failed"


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        endpoint = " ".join(argv)
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
            return Result(
                {
                    "check_runs": [
                        {"name": "pytest", "status": "completed", "conclusion": "success"}
                    ]
                }
            )
        if f"commits/{HEAD}/status" in endpoint:
            return Result(
                {"statuses": [{"context": "legacy/lint", "state": "success"}]}
            )
        if "pulls/7/reviews" in endpoint:
            return Result(
                [
                    {
                        "id": 9,
                        "user": {"login": "copilot-pull-request-reviewer[bot]"},
                        "commit_id": HEAD,
                        "state": "COMMENTED",
                        "body": "clean",
                    }
                ]
            )
        if " api graphql " in f" {endpoint} ":
            return Result(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "closingIssuesReferences": {"nodes": [{"number": 14}]},
                                "reviewThreads": {
                                    "nodes": [
                                        {"id": "T1", "isResolved": False, "isOutdated": True}
                                    ]
                                },
                            }
                        }
                    }
                }
            )
        if f"git/commits/{HEAD}" in endpoint:
            return Result({"tree": {"sha": HEAD_TREE}})
        if "git/commits/main" in endpoint:
            return Result({"tree": {"sha": MAIN_TREE}})
        if f"git/trees/{HEAD_TREE}?recursive=1" in endpoint or f"git/trees/{MAIN_TREE}?recursive=1" in endpoint:
            return Result(
                {
                    "tree": [
                        {
                            "path": "openspec/changes/archive/2026-07-17-unified-work-lifecycle/tasks.md"
                        }
                    ]
                }
            )
        if endpoint.endswith("repos/acme/demo"):
            return Result({"default_branch": "main"})
        if f"compare/{MERGE}...main" in endpoint:
            return Result({"status": "ahead"})
        if f"git/commits/{MERGE}" in endpoint:
            return Result({"parents": [{"sha": "1" * 40}, {"sha": "2" * 40}]})
        if "repos/acme/demo/issues/14" in endpoint:
            return Result({"state": "closed"})
        if argv[:4] == ["gh", "pr", "merge", "7"]:
            return Result({})
        raise AssertionError(f"unexpected argv: {argv}")


def test_fetch_delivery_facts_uses_authenticated_typed_gh_api() -> None:
    runner = FakeRunner()
    facts = GitHubDeliveryClient(runner=runner).fetch_delivery_facts(
        repo="acme/demo",
        pr_number=7,
        change="unified-work-lifecycle",
    )
    assert facts.head == HEAD
    assert facts.closing_issues == (14,)
    assert facts.active_openspec_absent
    assert facts.archive_present
    assert facts.review_threads[0].outdated
    assert any(
        f"commits/{HEAD}/status" in " ".join(call[0]) for call in runner.calls
    )
    assert all(call[1]["shell"] is False for call in runner.calls)


def test_fetch_remote_closure_verifies_merge_ancestor_issues_and_archive() -> None:
    runner = FakeRunner()
    facts = GitHubDeliveryClient(runner=runner).fetch_remote_closure(
        repo="acme/demo",
        pr_number=7,
        change="unified-work-lifecycle",
        required_issues=(14,),
        todo_complete=True,
        completion_record_valid=True,
    )
    assert facts.merge_commit == MERGE
    assert facts.merge_is_ancestor
    assert facts.merge_is_merge_commit
    assert facts.issue_states == {14: "closed"}
    assert facts.archive_present


def test_fetch_fails_closed_on_non_json_or_gh_error() -> None:
    class BrokenRunner:
        def __call__(self, argv, **kwargs):
            result = Result({}, returncode=1)
            result.stdout = "not-json"
            return result

    with pytest.raises(RuntimeError, match="gh command failed"):
        GitHubDeliveryClient(runner=BrokenRunner()).fetch_delivery_facts(
            repo="acme/demo",
            pr_number=7,
            change="x",
        )


def test_merge_and_request_commands_remain_shell_free() -> None:
    calls = []

    class RawRunner:
        def __call__(self, argv, **kwargs):
            calls.append((list(argv), kwargs))
            return Result({})

    client = GitHubDeliveryClient(runner=RawRunner())
    client.request_copilot(repo="acme/demo", pr_number=7)
    client.merge(pr_number=7, expected_head=HEAD)
    assert calls[0][0][-1] == "reviewers[]=copilot-pull-request-reviewer[bot]"
    assert calls[1][0] == [
        "gh",
        "pr",
        "merge",
        "7",
        "--merge",
        "--match-head-commit",
        HEAD,
    ]
    assert all(call[1]["shell"] is False for call in calls)


def test_merge_if_ready_rereads_and_matches_exact_head() -> None:
    runner = FakeRunner()
    client = GitHubDeliveryClient(runner=runner)
    client.merge_if_ready(
        repo="acme/demo",
        pr_number=7,
        change="unified-work-lifecycle",
        policy=DeliveryPolicy(expected_head=HEAD, required_closing_issues=(14,)),
    )
    merge_calls = [call for call in runner.calls if call[0][:4] == ["gh", "pr", "merge", "7"]]
    assert len(merge_calls) == 1
    assert merge_calls[0][0][-1] == HEAD


def test_merge_if_ready_does_not_merge_when_final_reread_blocks() -> None:
    runner = FakeRunner()
    client = GitHubDeliveryClient(runner=runner)
    with pytest.raises(RuntimeError, match="head-race"):
        client.merge_if_ready(
            repo="acme/demo",
            pr_number=7,
            change="unified-work-lifecycle",
            policy=DeliveryPolicy(
                expected_head="f" * 40,
                required_closing_issues=(14,),
            ),
        )
    assert not any(call[0][:4] == ["gh", "pr", "merge", "7"] for call in runner.calls)
