from __future__ import annotations

import json
import base64

import pytest

from paulsha_cortex.coordinator.github_delivery import (
    DeliveryPolicy,
    GitHubDeliveryClient,
    _SHIP_CAPABILITY,
)


HEAD = "a" * 40
MERGE = "b" * 40
HEAD_TREE = "c" * 40
MAIN_TREE = "d" * 40
DEFAULT_HEAD = "e" * 40


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
                [{
                    "total_count": 1,
                    "check_runs": [
                        {"name": "pytest", "status": "completed", "conclusion": "success"}
                    ]
                }]
            )
        if f"commits/{HEAD}/statuses" in endpoint:
            return Result([[{"context": "legacy/lint", "state": "success"}]])
        if "pulls/7/reviews" in endpoint:
            return Result(
                [[
                    {
                        "id": 9,
                        "user": {"login": "copilot-pull-request-reviewer[bot]"},
                        "commit_id": HEAD,
                        "state": "COMMENTED",
                        "body": "clean",
                        "submitted_at": "2026-07-17T00:00:00Z",
                    }
                ]]
            )
        if " api graphql " in f" {endpoint} ":
            return Result(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "closingIssuesReferences": {
                                    "nodes": [
                                        {
                                            "number": 14,
                                            "repository": {"nameWithOwner": "acme/demo"},
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": "I1",
                                    },
                                },
                                "reviewThreads": {
                                    "nodes": [
                                        {"id": "T1", "isResolved": False, "isOutdated": True}
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": "T1",
                                    },
                                },
                            }
                        }
                    }
                }
            )
        if f"git/commits/{HEAD}" in endpoint:
            return Result({"tree": {"sha": HEAD_TREE}})
        if "git/ref/heads/main" in endpoint:
            return Result({"object": {"sha": DEFAULT_HEAD}})
        if f"git/commits/{DEFAULT_HEAD}" in endpoint:
            return Result({"tree": {"sha": MAIN_TREE}})
        if f"git/trees/{HEAD_TREE}?recursive=1" in endpoint or f"git/trees/{MAIN_TREE}?recursive=1" in endpoint:
            return Result(
                {
                    "truncated": False,
                    "tree": [
                        {
                            "path": "openspec/changes/archive/2026-07-17-unified-work-lifecycle/tasks.md"
                        }
                    ]
                }
            )
        if endpoint.endswith("repos/acme/demo"):
            return Result({"default_branch": "main"})
        if f"compare/{MERGE}...{DEFAULT_HEAD}" in endpoint:
            return Result({"status": "ahead"})
        if f"git/commits/{MERGE}" in endpoint:
            return Result({"parents": [{"sha": "1" * 40}, {"sha": HEAD}]})
        if "repos/acme/demo/issues/14" in endpoint:
            return Result({"state": "closed"})
        if f"repos/acme/demo/contents/docs/todo.md?ref={DEFAULT_HEAD}" in endpoint:
            return Result(
                {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(b"- [x] complete\n").decode(),
                    "sha": "f" * 40,
                }
            )
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
        f"commits/{HEAD}/statuses" in " ".join(call[0]) for call in runner.calls
    )
    assert all(call[1]["shell"] is False for call in runner.calls)


def test_fetch_delivery_facts_uses_latest_legacy_status_per_context() -> None:
    class StatusHistory(FakeRunner):
        def __call__(self, argv, **kwargs):
            if f"commits/{HEAD}/statuses" in " ".join(argv):
                return Result(
                    [[
                        {"context": "legacy/lint", "state": "success"},
                        {"context": "legacy/lint", "state": "failure"},
                    ]]
                )
            return super().__call__(argv, **kwargs)

    facts = GitHubDeliveryClient(runner=StatusHistory()).fetch_delivery_facts(
        repo="acme/demo",
        pr_number=7,
        change="unified-work-lifecycle",
    )

    legacy_checks = tuple(check for check in facts.checks if check.name == "legacy/lint")
    assert len(legacy_checks) == 1
    assert legacy_checks[0].terminal_green


def test_fetch_remote_closure_verifies_merge_ancestor_issues_and_archive() -> None:
    runner = FakeRunner()
    facts = GitHubDeliveryClient(runner=runner).fetch_remote_closure(
        repo="acme/demo",
        pr_number=7,
        change="unified-work-lifecycle",
        required_issues=(14,),
        todo_paths=("docs/todo.md",),
    )
    assert facts.merge_commit == MERGE
    assert facts.merge_is_ancestor
    assert facts.merge_is_merge_commit
    assert facts.issue_states == {14: "closed"}
    assert facts.archive_present
    assert facts.todo_complete
    assert facts.default_head == DEFAULT_HEAD
    assert facts.todo_revisions == {"docs/todo.md": "f" * 40}
    assert not facts.completion_record_valid


def test_fetch_merge_status_binds_merged_side_effect_to_exact_pr_head() -> None:
    status = GitHubDeliveryClient(runner=FakeRunner()).fetch_merge_status(
        repo="acme/demo", pr_number=7
    )
    assert status.merged
    assert status.pr_head == HEAD
    assert status.merge_commit == MERGE


def test_ensure_pr_metadata_updates_and_rereads_exact_remote_fields() -> None:
    calls: list[list[str]] = []

    class MetadataRunner:
        def __call__(self, argv, **kwargs):
            calls.append(list(argv))
            endpoint = " ".join(argv)
            if "--method PATCH" in endpoint:
                return Result({"title": "fix(work): 修正工作流程", "body": "Closes #14"})
            if "--method PUT" in endpoint:
                return Result({"labels": [{"name": "enhancement"}]})
            if endpoint.endswith("repos/acme/demo/pulls/7"):
                return Result({"title": "fix(work): 修正工作流程", "body": "Closes #14"})
            if endpoint.endswith("repos/acme/demo/issues/7"):
                return Result({"labels": [{"name": "enhancement"}]})
            raise AssertionError(argv)

    GitHubDeliveryClient(runner=MetadataRunner()).ensure_pr_metadata(
        repo="acme/demo",
        pr_number=7,
        title="fix(work): 修正工作流程",
        body="Closes #14",
        labels=("enhancement",),
    )
    assert any("--method" in call and "PATCH" in call for call in calls)
    assert any("--method" in call and "PUT" in call for call in calls)


def test_ensure_pr_metadata_rejects_remote_reread_drift() -> None:
    class DriftRunner:
        def __call__(self, argv, **kwargs):
            endpoint = " ".join(argv)
            if "--method" in argv:
                return Result({})
            if endpoint.endswith("repos/acme/demo/pulls/7"):
                return Result({"title": "wrong", "body": "Closes #14"})
            if endpoint.endswith("repos/acme/demo/issues/7"):
                return Result({"labels": [{"name": "enhancement"}]})
            raise AssertionError(argv)

    with pytest.raises(RuntimeError, match="metadata reread mismatch"):
        GitHubDeliveryClient(runner=DriftRunner()).ensure_pr_metadata(
            repo="acme/demo",
            pr_number=7,
            title="fix(work): 修正工作流程",
            body="Closes #14",
            labels=("enhancement",),
        )


def test_create_or_get_pull_request_creates_exact_head_then_rereads_metadata() -> None:
    calls: list[list[str]] = []

    class CreateRunner:
        def __call__(self, argv, **kwargs):
            calls.append(list(argv))
            endpoint = " ".join(argv)
            if endpoint.endswith("repos/acme/demo"):
                return Result({"default_branch": "main"})
            if "pulls?state=open&head=acme%3Afeature%2F14-work" in endpoint:
                return Result([])
            if "--method POST repos/acme/demo/pulls" in endpoint:
                return Result(
                    {
                        "number": 17,
                        "head": {"ref": "feature/14-work", "sha": HEAD},
                        "base": {"ref": "main"},
                    }
                )
            if "--method PATCH" in endpoint or "--method PUT" in endpoint:
                return Result({})
            if endpoint.endswith("repos/acme/demo/pulls/17"):
                return Result({"title": "feat(workflow): 完成 work", "body": "Closes #14"})
            if endpoint.endswith("repos/acme/demo/issues/17"):
                return Result({"labels": [{"name": "enhancement"}]})
            raise AssertionError(argv)

    number = GitHubDeliveryClient(runner=CreateRunner()).create_or_get_pull_request(
        repo="acme/demo",
        branch="feature/14-work",
        expected_head=HEAD,
        title="feat(workflow): 完成 work",
        body="Closes #14",
        labels=("enhancement",),
    )

    assert number == 17
    assert sum("--method POST repos/acme/demo/pulls" in " ".join(call) for call in calls) == 1
    assert all(call[0] == "gh" for call in calls)


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
    with pytest.raises(PermissionError, match="ShipOrchestrator"):
        client.merge(repo="acme/demo", pr_number=7, expected_head=HEAD)
    client.merge(
        repo="acme/demo",
        pr_number=7,
        expected_head=HEAD,
        _capability=_SHIP_CAPABILITY,
    )
    assert calls[0][0][-1] == "reviewers[]=copilot-pull-request-reviewer[bot]"
    assert calls[1][0] == [
        "gh",
        "pr",
        "merge",
        "7",
        "--repo",
        "acme/demo",
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
        policy=DeliveryPolicy(
            expected_head=HEAD,
            required_closing_issues=(14,),
            copilot_review_id=9,
            copilot_requested_at_epoch=1,
        ),
        _capability=_SHIP_CAPABILITY,
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
                copilot_review_id=9,
                copilot_requested_at_epoch=1,
            ),
            _capability=_SHIP_CAPABILITY,
        )
    assert not any(call[0][:4] == ["gh", "pr", "merge", "7"] for call in runner.calls)


def test_check_run_pagination_must_match_total_count() -> None:
    class Incomplete(FakeRunner):
        def __call__(self, argv, **kwargs):
            if f"commits/{HEAD}/check-runs" in " ".join(argv):
                return Result(
                    [
                        {
                            "total_count": 2,
                            "check_runs": [
                                {
                                    "name": "pytest",
                                    "status": "completed",
                                    "conclusion": "success",
                                }
                            ],
                        }
                    ]
                )
            return super().__call__(argv, **kwargs)

    with pytest.raises(RuntimeError, match="pagination incomplete"):
        GitHubDeliveryClient(runner=Incomplete()).fetch_delivery_facts(
            repo="acme/demo",
            pr_number=7,
            change="unified-work-lifecycle",
        )


@pytest.mark.parametrize("truncated", [None, "false", 0, 1, True])
def test_tree_requires_explicit_boolean_false(truncated) -> None:
    class BadTree(FakeRunner):
        def __call__(self, argv, **kwargs):
            if f"git/trees/{HEAD_TREE}?recursive=1" in " ".join(argv):
                payload = {"tree": []}
                if truncated is not None:
                    payload["truncated"] = truncated
                return Result(payload)
            return super().__call__(argv, **kwargs)

    with pytest.raises(RuntimeError, match="unavailable or truncated"):
        GitHubDeliveryClient(runner=BadTree()).fetch_delivery_facts(
            repo="acme/demo",
            pr_number=7,
            change="unified-work-lifecycle",
        )


def test_graphql_connections_are_fully_paginated_and_booleans_are_strict() -> None:
    class Paged(FakeRunner):
        def __init__(self, malformed=False):
            super().__init__()
            self.malformed = malformed

        def __call__(self, argv, **kwargs):
            endpoint = " ".join(argv)
            if " api graphql " not in f" {endpoint} ":
                return super().__call__(argv, **kwargs)
            second = "threadCursor=T1" in endpoint
            resolved = "false" if self.malformed else False
            issue = 15 if second else 14
            thread = "T2" if second else "T1"
            return Result(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "closingIssuesReferences": {
                                    "nodes": [
                                        {
                                            "number": issue,
                                            "repository": {"nameWithOwner": "acme/demo"},
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": not second,
                                        "endCursor": "I2" if second else "I1",
                                    },
                                },
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": thread,
                                            "isResolved": resolved,
                                            "isOutdated": False,
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": not second,
                                        "endCursor": "T2" if second else "T1",
                                    },
                                },
                            }
                        }
                    }
                }
            )

    facts = GitHubDeliveryClient(runner=Paged()).fetch_delivery_facts(
        repo="acme/demo",
        pr_number=7,
        change="unified-work-lifecycle",
    )
    assert facts.closing_issues == (14, 15)
    assert tuple(thread.thread_id for thread in facts.review_threads) == ("T1", "T2")
    with pytest.raises(RuntimeError, match="association facts malformed"):
        GitHubDeliveryClient(runner=Paged(malformed=True)).fetch_delivery_facts(
            repo="acme/demo",
            pr_number=7,
            change="unified-work-lifecycle",
        )
