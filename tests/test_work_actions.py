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
    MergeStatus,
    ReviewThread,
)
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult


HEAD = "a" * 40
TREE = "b" * 40


def _pr_metadata(path: Path, *, title="fix(work): 修正工作流程", body="Closes #12") -> Path:
    path.write_text(
        json.dumps({"title": title, "body": body, "labels": ["enhancement"]}),
        encoding="utf-8",
    )
    return path


def _snapshot(
    path: Path,
    *,
    issues=(12,),
    source_revisions=("issue:12@open", "openspec:demo@1"),
    provider_revision="gh-1",
    auto_label=True,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": provider_revision,
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "mapped_issues": list(issues),
                        "confirmed_todo": True,
                        "auto_label": auto_label,
                        "source_revisions": list(source_revisions),
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


def test_source_change_starts_new_run_and_terminal_run_is_not_resumed(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    first = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["runs"]["acme/demo/demo"]["status"] = "done"
    state.write_text(json.dumps(payload), encoding="utf-8")
    done = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert done["result"]["action"] == "done"

    _snapshot(
        snapshot,
        source_revisions=("issue:12@open", "openspec:demo@2"),
        provider_revision="gh-2",
    )
    changed = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert changed["result"]["action"] == "claim"
    assert changed["result"]["run"]["run_id"] != first["result"]["run"]["run_id"]


def test_periodic_auto_scan_claims_labeled_work_and_persists_missing_issue(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    claimed = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "cortex:auto-on-going"}]}),
            stderr="",
        ),
    )
    assert claimed[0]["action"] == "claim"
    assert claimed[0]["run"]["status"] == "ongoing"

    missing_snapshot = _snapshot(
        tmp_path / "missing.json",
        issues=(),
        source_revisions=("openspec:demo@2",),
    )
    missing_state = tmp_path / "missing-runs.json"
    attention = work_actions.run_auto_claim_scan(
        snapshot_path=missing_snapshot,
        state_path=missing_state,
        now=lambda: 200,
    )
    assert attention[0]["action"] == "needs_human"
    assert attention[0]["run"]["reason"] == "missing_issue"

    _snapshot(
        missing_snapshot,
        issues=(12,),
        source_revisions=("issue:12@open", "openspec:demo@3"),
        provider_revision="gh-3",
    )
    resumed = work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=missing_snapshot,
        state_path=missing_state,
        now=lambda: 200,
    )
    assert resumed["result"]["action"] == "claim"
    assert resumed["result"]["run"]["status"] == "ongoing"


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


def test_typed_path_and_openspec_links_and_exclusions_are_canonical(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    base = {"repo": "acme/demo", "work_id": "demo", "repo_root": str(repo_root)}
    for kind, ref in (
        ("path", "docs/superpowers/specs/demo.md"),
        ("openspec", "unified-work-lifecycle"),
    ):
        work_actions.execute_work_action(
            args={"action": "link", "kind": kind, "ref": ref, **base},
            requested_by="operator",
        )
    work_actions.execute_work_action(
        args={
            "action": "unlink",
            "kind": "path",
            "ref": "docs/superpowers/specs/demo.md",
            **base,
        },
        requested_by="operator",
    )
    payload = work_actions.safe_load(
        (repo_root / ".cortex" / "work-items.yaml").read_text(encoding="utf-8")
    )["work_items"]["demo"]
    assert {"kind": "openspec", "ref": "unified-work-lifecycle"} in payload["links"]
    assert {
        "kind": "path",
        "ref": "docs/superpowers/specs/demo.md",
    } in payload["excludes"]


@pytest.mark.parametrize(
    "extra",
    [
        {"kind": "path", "ref": "../escape"},
        {"kind": "openspec", "ref": "Bad Slug"},
        {"kind": "github_pr", "ref": "other/repo#2"},
        {"issue": 12, "kind": "github_issue", "ref": "acme/demo#12"},
    ],
)
def test_link_rejects_malformed_or_conflicting_source(tmp_path: Path, extra: dict) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    with pytest.raises(ValueError):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(repo_root),
                **extra,
            },
            requested_by="operator",
        )


def test_override_writer_rejects_symlink_escape(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo_root.mkdir()
    outside.mkdir()
    (repo_root / ".cortex").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(repo_root),
                "kind": "path",
                "ref": "docs/demo.md",
            },
            requested_by="operator",
        )


def test_override_writer_rejects_malformed_existing_schema(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    override = repo_root / ".cortex" / "work-items.yaml"
    override.parent.mkdir(parents=True)
    override.write_text(
        "version: 1\nwork_items:\n  demo:\n    title: demo\n    links:\n      - kind: path\n        ref: ../escape\n    excludes: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="canonical repo-relative"):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(repo_root),
                "kind": "openspec",
                "ref": "demo",
            },
            requested_by="operator",
        )


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
    merged_state = {"value": False}
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

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(
                merged=merged_state["value"],
                pr_head=HEAD,
                merge_commit="c" * 40 if merged_state["value"] else None,
            )

    class Orchestrator:
        def __init__(self, *, github, now):
            calls.append("orchestrator-init")

        def merge_if_ready(self, **kwargs):
            calls.append("merge-if-ready")
            assert kwargs["authority"].mapped_issues == (12,)
            assert kwargs["preflight"].head == HEAD
            assert kwargs["copilot"].review_id == 9
            merged_state["value"] = True
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
        "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
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
    (tmp_path / "openspec" / "changes" / "demo" / "tasks.md").write_text(
        "- [x] complete\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n- **demo**: done\n", encoding="utf-8"
    )
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
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=runner,
    )
    assert result["result"]["action"] == "archive-applied-needs-commit"
    assert calls[-1][0] == ["openspec", "archive", "-y", "demo"]
    assert calls[0][0] == ["openspec", "validate", "demo", "--strict"]


def test_review_findings_persist_across_heads_and_third_round_needs_human(
    monkeypatch, tmp_path: Path
) -> None:
    heads = ["a" * 40, "b" * 40, "c" * 40]
    current = {"head": heads[0], "review": False, "submitted": 201.0, "now": 200.0}
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: current["now"],
    )

    class GitHub:
        def __init__(self, *, runner):
            pass

        def fetch_delivery_facts(self, **kwargs):
            reviews = ()
            if current["review"]:
                reviews = (
                    CopilotReview(
                        review_id=heads.index(current["head"]) + 1,
                        commit_id=current["head"],
                        state="COMMENTED",
                        body="finding",
                        author=COPILOT_REVIEWER_LOGIN,
                        submitted_at_epoch=current["submitted"],
                    ),
                )
            return DeliveryFacts(
                head=current["head"],
                mergeable=True,
                mergeable_state="clean",
                checks=(),
                copilot_reviews=reviews,
                review_threads=(ReviewThread("thread", False, False),),
                closing_issues=(12,),
                active_openspec_absent=True,
                archive_present=True,
            )

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(False, current["head"], None)

        def request_copilot(self, **kwargs):
            pass

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: PreflightResult(
            True,
            None,
            CommandResult(("policy",), 0, "", ""),
            CommandResult(("preflight",), 0, "", ""),
            current["head"],
            TREE,
        ),
    )
    base = {
        "action": "ship",
        "repo": "acme/demo",
        "work_id": "demo",
        "repo_root": str(tmp_path),
        "pr_number": 8,
        "change": "demo",
        "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
    }
    for index, head in enumerate(heads):
        current.update(head=head, review=False, now=200.0 + index * 20, submitted=201.0 + index * 20)
        requested = work_actions.execute_work_action(
            args=base,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: current["now"],
        )
        assert requested["result"]["action"] == "awaiting-copilot"
        current["review"] = True
        current["now"] += 2
        reviewed = work_actions.execute_work_action(
            args=base,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: current["now"],
        )
        if index < 2:
            assert reviewed["result"]["action"] == "fix-required"
        else:
            assert reviewed["result"] == {
                "action": "needs_human",
                "reason": "copilot-finding-budget-exhausted",
            }
    persisted = json.loads(state.read_text(encoding="utf-8"))["runs"]["acme/demo/demo"]
    assert persisted["status"] == "needs_human"
    assert persisted["ship"]["fix_rounds"] == 2


def test_external_merge_is_reconciled_without_second_merge(monkeypatch, tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    merge_calls = []

    class GitHub:
        def __init__(self, *, runner):
            pass

        def fetch_delivery_facts(self, **kwargs):
            return DeliveryFacts(
                head=HEAD,
                mergeable=False,
                mergeable_state="unknown",
                checks=(),
                copilot_reviews=(),
                review_threads=(),
                closing_issues=(12,),
                active_openspec_absent=True,
                archive_present=True,
            )

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(True, HEAD, "c" * 40)

        def request_copilot(self, **kwargs):
            raise AssertionError("merged PR must not request review")

    class Orchestrator:
        def __init__(self, **kwargs):
            pass

        def merge_if_ready(self, **kwargs):
            merge_calls.append(kwargs)
            raise AssertionError("merged PR must not merge again")

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", Orchestrator)
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: PreflightResult(
            True,
            None,
            CommandResult(("policy",), 0, "", ""),
            CommandResult(("preflight",), 0, "", ""),
            HEAD,
            TREE,
        ),
    )
    result = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": "demo",
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert result["result"]["action"] == "merged-awaiting-closure"
    assert merge_calls == []


def test_cached_done_replays_remote_closure_instead_of_trusting_local_state(
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
    persisted = json.loads(state.read_text(encoding="utf-8"))
    run = persisted["runs"]["acme/demo/demo"]
    run["status"] = "done"
    run["ship"] = {
        "phase": "done",
        "head": HEAD,
        "todo_paths": ["docs/todo.md"],
        "completion_record": {"path": "/evidence/record.json", "hash": "d" * 64},
    }
    state.write_text(json.dumps(persisted), encoding="utf-8")
    closure_calls = []

    class GitHub:
        def __init__(self, *, runner):
            pass

    class Orchestrator:
        def __init__(self, **kwargs):
            pass

        def verify_remote_closure(self, **kwargs):
            closure_calls.append(kwargs)
            return SimpleNamespace(
                facts=SimpleNamespace(merge_commit="c" * 40),
                completion_record={"path": "/evidence/record.json", "hash": "d" * 64},
            )

    from paulsha_cortex.coordinator import completion

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", Orchestrator)
    monkeypatch.setattr(completion, "read_completion_record", lambda *args, **kwargs: {"record": True})
    result = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": "demo",
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert result["result"]["action"] == "done"
    assert len(closure_calls) == 1
    assert closure_calls[0]["authority"].snapshot_hash == run["snapshot_hash"]


@pytest.mark.parametrize(
    ("tasks", "title", "body", "reason"),
    [
        ("- [ ] pending\n", "fix(work): 修正工作流程", "Closes #12", "tasks-incomplete"),
        ("- [x] done\n", "fix(work): invalid", "Closes #12", "PR metadata blocked"),
        ("- [x] done\n", "fix(work): 修正工作流程", "Relates #12", "PR metadata blocked"),
    ],
)
def test_archive_and_pr_metadata_fail_closed_before_mutation(
    tmp_path: Path, tasks: str, title: str, body: str, reason: str
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
    change = tmp_path / "openspec" / "changes" / "demo"
    change.mkdir(parents=True)
    (change / "tasks.md").write_text(tasks, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n- **demo**: done\n", encoding="utf-8"
    )
    metadata = _pr_metadata(tmp_path / "pr.json", title=title, body=body)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match=reason):
        work_actions.execute_work_action(
            args={
                "action": "ship",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(tmp_path),
                "pr_number": 8,
                "change": "demo",
                "pr_metadata_path": str(metadata),
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
            runner=runner,
        )
    assert ["openspec", "archive", "-y", "demo"] not in calls
