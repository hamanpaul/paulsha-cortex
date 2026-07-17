from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.github_delivery import (
    COPILOT_REVIEWER_LOGIN,
    CopilotReview,
    DeliveryFacts,
)
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult


HEAD = "a" * 40
TREE = "b" * 40


def _snapshot(path: Path) -> Path:
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
                        "work_id": "demo",
                        "mapped_issues": [12],
                        "confirmed_todo": True,
                        "source_revisions": ["issue:12@open", "openspec:demo@1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_start_is_restart_idempotent_and_auto_uses_typed_label_command(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    first = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    second = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert first["result"]["run"]["run_id"] == second["result"]["run"]["run_id"]

    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    work_actions.execute_work_action(
        args={
            "action": "auto",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "enabled": True,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=runner,
    )
    assert calls[0][0][:4] == ["gh", "api", "--method", "POST"]
    assert calls[0][1]["shell"] is False
    with pytest.raises(ValueError, match="strict boolean"):
        work_actions.execute_work_action(
            args={
                "action": "auto",
                "repo": "acme/demo",
                "work_id": "demo",
                "issue": 12,
                "enabled": 1,
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
        )


def test_unlink_persists_exclusion_and_link_removes_it(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    common = {
        "repo": "acme/demo",
        "work_id": "demo",
        "issue": 12,
        "repo_root": str(repo_root),
    }
    work_actions.execute_work_action(
        args={"action": "unlink", **common},
        requested_by="operator",
    )
    text = (repo_root / ".cortex" / "work-items.yaml").read_text(encoding="utf-8")
    assert "excludes:" in text and "acme/demo#12" in text
    work_actions.execute_work_action(
        args={"action": "link", **common},
        requested_by="operator",
    )
    payload = work_actions.safe_load(
        (repo_root / ".cortex" / "work-items.yaml").read_text(encoding="utf-8")
    )
    assert payload["work_items"]["demo"]["excludes"] == []
    assert payload["work_items"]["demo"]["links"][0]["ref"] == "acme/demo#12"


def test_default_ship_runtime_is_resumable_and_connects_all_delivery_gates(
    monkeypatch, tmp_path: Path
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    foreign = tmp_path / "foreign.json"
    foreign.write_text("{}", encoding="utf-8")
    completion = tmp_path / "completion.json"
    completion.write_text("{}", encoding="utf-8")
    review_available = {"value": False}
    calls = []

    class GitHub:
        def __init__(self, *, runner):
            calls.append("github-init")

        def fetch_delivery_facts(self, **kwargs):
            calls.append("remote-archive-gate")
            reviews = ()
            if review_available["value"]:
                reviews = (
                    CopilotReview(
                        review_id=9,
                        commit_id=HEAD,
                        state="COMMENTED",
                        body="ok",
                        author=COPILOT_REVIEWER_LOGIN,
                        submitted_at_epoch=205,
                    ),
                )
            return DeliveryFacts(
                head=HEAD,
                mergeable=True,
                mergeable_state="clean",
                checks=(),
                copilot_reviews=reviews,
                review_threads=(),
                closing_issues=(12,),
                active_openspec_absent=True,
                archive_present=True,
            )

        def request_copilot(self, **kwargs):
            calls.append("request-copilot")

    class Orchestrator:
        def __init__(self, *, github, now):
            calls.append("orchestrator-init")

        def merge_if_ready(self, **kwargs):
            calls.append("merge-if-ready")
            assert kwargs["authority"].mapped_issues == (12,)
            assert kwargs["preflight"].head == HEAD
            assert kwargs["copilot"].review_id == 9
            return SimpleNamespace(expected_head=HEAD, expected_tree_hash=TREE)

        def verify_remote_closure(self, **kwargs):
            calls.append("remote-closure")
            assert kwargs["expected_head"] == HEAD
            return SimpleNamespace(
                facts=SimpleNamespace(merge_commit="c" * 40),
                completion_record={"path": "/evidence/completion.json", "hash": "d" * 64},
            )

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", Orchestrator)
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: PreflightResult(
            passed=True,
            failed_stage=None,
            policy=CommandResult(("policy",), 0, "", ""),
            ci_parity=CommandResult(("preflight",), 0, "", ""),
            head=HEAD,
            tree_hash=TREE,
        ),
    )
    base = {
        "action": "ship",
        "repo": "acme/demo",
        "work_id": "demo",
        "repo_root": str(tmp_path),
        "pr_number": 8,
        "change": "demo",
        "foreign_review_path": str(foreign),
        "foreign_review_hash": "e" * 64,
    }
    first = work_actions.execute_work_action(
        args=base,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert first["result"]["action"] == "awaiting-copilot"
    assert "request-copilot" in calls

    review_available["value"] = True
    second = work_actions.execute_work_action(
        args=base,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
    )
    assert second["result"]["action"] == "merged-awaiting-closure"
    assert "merge-if-ready" in calls

    third = work_actions.execute_work_action(
        args={**base, "todo_paths": ["todo.md"], "completion_record_path": str(completion)},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 220,
    )
    assert third["result"]["action"] == "done"
    assert "remote-closure" in calls


def test_ship_runs_official_archive_before_preflight(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    (tmp_path / "openspec" / "changes" / "demo").mkdir(parents=True)
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": "demo",
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=runner,
    )
    assert result["result"]["action"] == "archive-applied-needs-commit"
    assert calls == [
        (
            ["openspec", "archive", "-y", "demo"],
            {
                "cwd": str(tmp_path),
                "shell": False,
                "capture_output": True,
                "text": True,
            },
        )
    ]
