from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import work_bridge
from paulsha_cortex.coordinator.claim import load_work_authority
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.monitor.providers import GitHubTerminalProvider


def _repo(root: Path) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "remote",
            "add",
            "origin",
            "git@github.com:acme/demo.git",
        ],
        check=True,
    )
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    return root


def _snapshot(
    path: Path,
    *,
    mapped_prs: tuple[int, ...] = (),
    source_revisions: tuple[str, ...] = (
        "github_issue:acme/demo#14@issue-open",
        "openspec:acme/demo:work@spec-1",
    ),
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-1",
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "work",
                        "mapped_issues": [14],
                        "mapped_prs": list(mapped_prs),
                        "mapped_openspec": ["work"],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": False,
                        "source_revisions": list(source_revisions),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _completed(payload, *, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=("gh",),
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


class _FakeRunner:
    def __init__(self, results):
        self.results = iter(results)

    def run(self, argv, *, timeout):
        return next(self.results)


def test_new_claim_run_starts_with_empty_pr_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _repo(tmp_path / "workspace")
    snapshot = _snapshot(tmp_path / "snapshot.json", mapped_prs=(42,))
    authority = load_work_authority(
        repo="acme/demo", work_id="work", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    needs_human_run = work_bridge.start_canonical_workflow(
        registry=registry,
        authority=authority,
        claim_key="claim:v1:" + "1" * 64,
        coordinator_root=tmp_path / "coordinator",
        explicit_repo_root=workspace,
        needs_human_reason="missing-issue",
    )
    assert needs_human_run.pr_refs == ()

    manifest = work_bridge.default_workflow_manifest("work", change="work")
    captured: dict[str, object] = {}

    def fake_apply_workflow_action(
        _registry,
        *,
        args,
        **_kwargs,
    ):
        captured["args"] = args
        run = _registry._manager_create_workflow_run(
            work_id=args["work_id"],
            repo=args["repo"],
            claim_key=args["claim_key"],
            source_revision=args["source_revision"],
            workspace_root=args["artifact_root"],
            combo=manifest.combo,
            current_phase="define",
            steps=manifest.steps,
            issue_refs=tuple(args["issue_refs"]),
            openspec_refs=tuple(args["openspec_refs"]),
            pr_refs=tuple(args["pr_refs"]),
            attempts={"define": 1},
        )
        return {"run_id": run.run_id}

    monkeypatch.setattr(
        "paulsha_cortex.coordinator.manager.apply_workflow_action",
        fake_apply_workflow_action,
    )
    identity_registry = IdentityRegistry.from_rows(
        (
            {
                "executor": "codex",
                "model_id": "gpt",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
        )
    )

    start_run = work_bridge.start_canonical_workflow(
        registry=registry,
        authority=authority,
        claim_key="claim:v1:" + "2" * 64,
        coordinator_root=tmp_path / "coordinator",
        explicit_repo_root=workspace,
        identity_registry=identity_registry,
    )
    assert captured["args"]["pr_refs"] == []
    assert start_run.pr_refs == ()


def test_terminal_provider_skips_closed_unmerged_pr_closing_links():
    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {"name": "main", "target": {"oid": "d" * 40}},
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "number": 9,
                            "body": "",
                            "headRefOid": "e" * 40,
                            "state": "OPEN",
                            "mergedAt": None,
                            "mergeCommit": None,
                            "closingIssuesReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"number": 70, "state": "OPEN"}],
                            },
                        },
                        {
                            "number": 10,
                            "body": "",
                            "headRefOid": "f" * 40,
                            "state": "CLOSED",
                            "mergedAt": None,
                            "mergeCommit": None,
                            "closingIssuesReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"number": 71, "state": "CLOSED"}],
                            },
                        },
                        {
                            "number": 11,
                            "body": "",
                            "headRefOid": "g" * 40,
                            "state": "MERGED",
                            "mergedAt": "2026-07-17T10:00:00Z",
                            "mergeCommit": {
                                "oid": "a" * 40,
                                "parents": {"totalCount": 2},
                            },
                            "closingIssuesReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"number": 72, "state": "MERGED"}],
                            },
                        },
                    ],
                },
            }
        }
    }
    tree = {"truncated": False, "tree": []}
    runner = _FakeRunner([_completed(graph), _completed(tree), _completed({"status": "ahead"})])
    result = GitHubTerminalProvider("example/acme", runner=runner).scan()

    assert result.status == "ok"
    assert result.observations["closing_links"] == {
        "github_pr:example/acme#9": "github_issue:example/acme#70",
        "github_pr:example/acme#11": "github_issue:example/acme#72",
    }


def test_terminal_provider_keeps_open_pr_closing_links():
    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {"name": "main", "target": {"oid": "d" * 40}},
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "number": 9,
                            "body": "",
                            "headRefOid": "e" * 40,
                            "state": "OPEN",
                            "mergedAt": None,
                            "mergeCommit": None,
                            "closingIssuesReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"number": 7, "state": "OPEN"}],
                            },
                        }
                    ],
                },
            }
        }
    }
    tree = {"truncated": False, "tree": []}
    runner = _FakeRunner([_completed(graph), _completed(tree)])
    result = GitHubTerminalProvider("example/acme", runner=runner).scan()

    assert result.status == "ok"
    assert result.observations["closing_links"] == {
        "github_pr:example/acme#9": "github_issue:example/acme#7",
    }
