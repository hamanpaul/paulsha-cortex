from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.github_delivery import (
    COPILOT_REVIEWER_LOGIN,
    CopilotReview,
    DeliveryFacts,
    GitHubCheck,
    MergeStatus,
    ReviewThread,
)
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult
from paulsha_cortex.coordinator.registry import JobRegistry


HEAD = "a" * 40
TREE = "b" * 40


def _only_journal_row(state: Path) -> dict:
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["schema"] == "cortex-delivery-journal/v1"
    assert len(payload["runs"]) == 1
    return next(iter(payload["runs"].values()))


def _initialize_delivery_journal(*, snapshot: Path, state: Path) -> dict:
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=state.parent / "jobs.json")
    work_actions._load_work_run(
        state_path=state,
        workflow_registry=registry,
        authority=authority,
    )
    return _only_journal_row(state)


def _init_repo(root: Path, repo: str = "acme/demo") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", f"git@github.com:{repo}.git"],
            check=True,
        )
    return root


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
    prs=(8,),
    changes=("demo",),
    todo_paths=("docs/todo.md",),
) -> Path:
    _init_repo(path.parent)
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
                        "mapped_prs": list(prs),
                        "mapped_openspec": list(changes),
                        "mapped_todo_paths": list(todo_paths),
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


def test_auto_without_issue_mutates_every_mapped_issue(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", issues=(12, 13))
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    result = work_actions.execute_work_action(
        args={
            "action": "auto",
            "repo": "acme/demo",
            "work_id": "demo",
            "enabled": True,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        now=lambda: 200,
        runner=runner,
    )
    assert result["result"] == {"action": "auto", "enabled": True, "issues": [12, 13]}
    assert [call[4] for call in calls] == [
        "repos/acme/demo/issues/12/labels",
        "repos/acme/demo/issues/13/labels",
    ]


def test_auto_without_issue_fails_closed_if_any_label_mutation_fails(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", issues=(12, 13))
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        failed = "issues/13/" in " ".join(argv)
        return SimpleNamespace(returncode=1 if failed else 0, stdout="{}", stderr="boom" if failed else "")

    with pytest.raises(RuntimeError, match="auto-label mutation failed"):
        work_actions.execute_work_action(
            args={
                "action": "auto",
                "repo": "acme/demo",
                "work_id": "demo",
                "enabled": False,
            },
            requested_by="operator",
            snapshot_path=snapshot,
            now=lambda: 200,
            runner=runner,
        )
    assert len(calls) == 2


def test_preflight_authorization_hash_excludes_drifting_command_output() -> None:
    first = PreflightResult(
        True,
        None,
        CommandResult(("policy",), 0, "first stdout", "first stderr"),
        CommandResult(("preflight",), 0, "duration=1", ""),
        HEAD,
        TREE,
    )
    second = PreflightResult(
        True,
        None,
        CommandResult(("policy",), 0, "different stdout", "different stderr"),
        CommandResult(("preflight",), 0, "duration=999", "warning text"),
        HEAD,
        TREE,
    )
    assert work_actions._preflight_hash(first) == work_actions._preflight_hash(second)


def test_source_change_starts_new_canonical_run(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    first = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
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
    runs = JobRegistry(state_path=state.parent / "jobs.json").list_workflow_runs()
    assert len([run for run in runs if run.status == "ongoing"]) == 1
    old = next(run for run in runs if run.run_id == first["result"]["run"]["run_id"])
    assert old.status == "superseded"
    assert "blocked" in old.facets


def test_canonical_claim_excludes_derived_sources_and_volatile_github_revisions(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "canonical.json"

    def write(
        *,
        provider_revision: str,
        issue_revision: str,
        pr_revision: str,
        issue_status: str = "open",
        openspec_status: str = "active",
    ) -> None:
        snapshot.write_text(
            json.dumps(
                {
                    "schema": "work-items-snapshot/v1",
                    "providers": {
                        "github:acme/demo": {
                            "provider_id": "github:acme/demo",
                            "status": "ok",
                            "last_attempt_at": "2026-07-17T00:00:00Z",
                            "last_success_at": "2026-07-17T00:00:00Z",
                            "revision": provider_revision,
                            "diagnostics": [],
                            "sources": [],
                            "observations": {},
                        }
                    },
                    "work_items": [
                        {
                            "repo": "acme/demo",
                            "work_id": "demo",
                            "sources": [
                                {
                                    "source_id": "github_issue:acme/demo#12",
                                    "kind": "github_issue",
                                    "ref": "acme/demo#12",
                                    "revision": issue_revision,
                                    "status": issue_status,
                                    "confidence": "confirmed",
                                    "provider": "github:acme/demo",
                                },
                                {
                                    "source_id": "github_pr:acme/demo#8",
                                    "kind": "github_pr",
                                    "ref": "acme/demo#8",
                                    "revision": pr_revision,
                                    "status": "open",
                                    "confidence": "confirmed",
                                    "provider": "github:acme/demo",
                                },
                                {
                                    "source_id": "openspec:acme/demo:demo",
                                    "kind": "openspec",
                                    "ref": "demo",
                                    "revision": "spec-content",
                                    "status": openspec_status,
                                    "confidence": "confirmed",
                                    "provider": "repo:acme/demo",
                                },
                                {
                                    "source_id": "workflow_run:acme/demo:run-1",
                                    "kind": "workflow_run",
                                    "ref": "run-1",
                                    "revision": "registry:9",
                                    "status": "ongoing",
                                    "confidence": "confirmed",
                                    "provider": "workflow:acme/demo",
                                },
                                {
                                    "source_id": "completion_record:acme/demo:run-1",
                                    "kind": "completion_record",
                                    "ref": "run-1",
                                    "revision": "completion:9",
                                    "status": "valid",
                                    "confidence": "confirmed",
                                    "provider": "workflow:acme/demo",
                                },
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    write(provider_revision="gh-1", issue_revision="updated:1", pr_revision="updated:1")
    first = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    first_digest = work_actions.work_authority_digest(first)
    assert all("workflow_run" not in row and "completion_record" not in row for row in first.source_revisions)

    write(provider_revision="gh-2", issue_revision="updated:2", pr_revision="updated:2")
    refreshed = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    assert refreshed.source_revisions == first.source_revisions
    assert work_actions.work_authority_digest(refreshed) == first_digest

    write(
        provider_revision="gh-3",
        issue_revision="updated:3",
        pr_revision="updated:3",
        issue_status="closed",
        openspec_status="archived",
    )
    completed = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    completed_candidate = work_actions.ClaimCandidate(
        authority=completed,
        repo=completed.repo,
        work_id=completed.work_id,
        source_revisions=completed.source_revisions,
        confirmed_todo=completed.confirmed_todo,
        confirmed_issue=12,
        auto_label=False,
        active_run_id=None,
        active_claim_key=None,
    )
    completed_key = work_actions.build_claim_key(completed_candidate)

    write(
        provider_revision="gh-4",
        issue_revision="updated:4",
        pr_revision="updated:4",
        issue_status="open",
        openspec_status="archived",
    )
    reopened = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    assert work_actions.work_authority_digest(reopened) != work_actions.work_authority_digest(
        completed
    )

    write(
        provider_revision="gh-5",
        issue_revision="updated:5",
        pr_revision="updated:5",
        issue_status="closed",
        openspec_status="active",
    )
    reactivated = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    assert work_actions.work_authority_digest(reactivated) != work_actions.work_authority_digest(
        completed
    )
    decision = work_actions.decide_manual_start(
        work_actions.ClaimCandidate(
            authority=reactivated,
            repo=reactivated.repo,
            work_id=reactivated.work_id,
            source_revisions=reactivated.source_revisions,
            confirmed_todo=reactivated.confirmed_todo,
            confirmed_issue=12,
            auto_label=False,
            active_run_id="done-run",
            active_claim_key=completed_key,
            active_status="done",
            active_snapshot_hash=completed.snapshot_hash,
            active_source_revisions=completed.source_revisions,
            active_provider_revision=completed.github_provider_revision,
            active_authority_digest=work_actions.work_authority_digest(completed),
        ),
        now_epoch=reactivated.github_last_success_epoch + 1,
    )
    assert decision.action == "claim"
    assert decision.reason is None


def test_snapshot_refresh_noise_keeps_same_semantic_run(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    first = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    first_snapshot_hash = first["result"]["run"]["snapshot_hash"]
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload.update(
        {
            "sequence": 99,
            "written_at": "2026-07-17T12:00:00Z",
            "fleet_noise": {"other/repo": "changed"},
        }
    )
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    refreshed = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert refreshed["result"]["action"] == "resume"
    assert refreshed["result"]["run"]["run_id"] == first["result"]["run"]["run_id"]
    assert refreshed["result"]["run"]["snapshot_hash"] != first_snapshot_hash


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


def test_periodic_auto_scan_reads_every_issue_and_any_label_claims(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", issues=(12, 13))
    calls: list[str] = []

    def runner(argv, **kwargs):
        calls.append(argv[-1])
        labels = [{"name": "cortex:auto-on-going"}] if argv[-1].endswith("/13") else []
        return SimpleNamespace(returncode=0, stdout=json.dumps({"labels": labels}), stderr="")

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        now=lambda: 200,
        runner=runner,
    )
    assert calls == ["repos/acme/demo/issues/12", "repos/acme/demo/issues/13"]
    assert result[0]["action"] == "claim"


def test_periodic_auto_scan_fails_closed_if_any_mapped_issue_read_fails(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", issues=(12, 13))

    def runner(argv, **kwargs):
        if argv[-1].endswith("/13"):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "cortex:auto-on-going"}]}),
            stderr="",
        )

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        now=lambda: 200,
        runner=runner,
    )
    assert result == [{
        "repo": "acme/demo",
        "work_id": "demo",
        "action": "blocked",
        "reason": "github-label-read-failed",
    }]
    assert not (tmp_path / "runs.json").exists()


def test_unlink_persists_exclusion_and_link_removes_it(tmp_path: Path) -> None:
    repo_root = _init_repo(tmp_path / "repo")
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
    repo_root = _init_repo(tmp_path / "repo")
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
    repo_root = _init_repo(tmp_path / "repo")
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
    _init_repo(repo_root)
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
    repo_root = _init_repo(tmp_path / "repo")
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


def test_all_actions_reject_malformed_repo_and_repo_root_remote_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        work_actions.execute_work_action(
            args={"action": "start", "repo": "bad/repo/extra", "work_id": "demo"},
            requested_by="operator",
            snapshot_path=tmp_path / "missing",
        )


def test_repo_root_must_be_exact_git_toplevel(tmp_path: Path) -> None:
    root = _init_repo(tmp_path / "repo")
    nested = root / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="top-level"):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(nested),
                "kind": "openspec",
                "ref": "demo",
            },
            requested_by="operator",
        )
    root = _init_repo(tmp_path / "other", repo="acme/other")
    with pytest.raises(ValueError, match="remote.*match"):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(root),
                "kind": "openspec",
                "ref": "demo",
            },
            requested_by="operator",
        )


def test_path_link_rejects_repo_internal_symlink(tmp_path: Path) -> None:
    root = _init_repo(tmp_path / "repo")
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    (root / "linked.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(root),
                "kind": "path",
                "ref": "linked.md",
            },
            requested_by="operator",
        )


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"pr_number": 9}, "PR.*authorized"),
        ({"change": "other"}, "OpenSpec.*authorized"),
        ({"todo_paths": ["docs/other.md"]}, "Todo.*authorized"),
    ],
)
def test_ship_payload_refs_must_exactly_match_work_authority(
    tmp_path: Path, override: dict, reason: str
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
    args = {
        "action": "ship",
        "repo": "acme/demo",
        "work_id": "demo",
        "repo_root": str(tmp_path),
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
        "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        **override,
    }
    with pytest.raises(RuntimeError, match=reason):
        work_actions.execute_work_action(
            args=args,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
        )


@pytest.mark.parametrize(
    "snapshot_overrides",
    [
        {"prs": (8, 9)},
        {"changes": ("demo", "other")},
        {"todo_paths": ("docs/todo.md", "docs/other.md")},
    ],
)
def test_ship_needs_human_when_authority_has_multiple_delivery_targets(
    tmp_path: Path, snapshot_overrides: dict
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", **snapshot_overrides)
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    result = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": ["docs/todo.md"],
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert result["result"] == {
        "action": "needs_human",
        "reason": "multiple-delivery-targets-unsupported",
    }
    persisted = _only_journal_row(state)
    assert "status" not in persisted
    assert JobRegistry(state_path=state.parent / "jobs.json").list_workflow_runs()[0].facets == (
        "needs_human",
    )


def test_ship_rejects_old_run_after_current_authority_changes(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    _snapshot(
        snapshot,
        source_revisions=("issue:12@open", "openspec:demo@2"),
        provider_revision="gh-2",
    )
    with pytest.raises(RuntimeError, match="current WorkAuthority"):
        work_actions.execute_work_action(
            args={
                "action": "ship",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(tmp_path),
                "pr_number": 8,
                "change": "demo",
                "todo_paths": ["docs/todo.md"],
                "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
        )


def test_ship_rejects_authorized_repo_path_symlink(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    outside = tmp_path / "outside-change"
    outside.mkdir()
    active_root = tmp_path / "openspec" / "changes"
    active_root.mkdir(parents=True)
    (active_root / "demo").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        work_actions.execute_work_action(
            args={
                "action": "ship",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(tmp_path),
                "pr_number": 8,
                "change": "demo",
                "todo_paths": ["docs/todo.md"],
                "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
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

        def ensure_pr_metadata(self, **kwargs):
            calls.append("ensure-pr-metadata")

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
                checks=(GitHubCheck("pytest", "completed", "success"),),
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
            durable = _only_journal_row(state)
            assert durable["ship"]["phase"] == "merge-authorized"
            assert durable["ship"]["merge_authorization"]["hash"]
            authorization_path = Path(durable["ship"]["merge_authorization"]["path"])
            assert authorization_path.is_file()
            assert authorization_path.stat().st_mode & 0o222 == 0
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
    foreign_normalized = {"state": "passed", "candidate": HEAD}
    monkeypatch.setattr(
        work_actions,
        "_validate_foreign_review",
        lambda *args, **kwargs: foreign_normalized,
    )
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
        "todo_paths": ["docs/todo.md"],
        "foreign_review_path": str(foreign),
        "foreign_review_hash": work_actions.verification.canonical_json_hash(foreign_normalized),
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
        args={**base, "completion_record_path": str(completion)},
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
            "todo_paths": ["docs/todo.md"],
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

        def ensure_pr_metadata(self, **kwargs):
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
        "todo_paths": ["docs/todo.md"],
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
    persisted = _only_journal_row(state)
    assert "status" not in persisted
    assert JobRegistry(state_path=state.parent / "jobs.json").list_workflow_runs()[0].facets == (
        "needs_human",
    )
    assert persisted["ship"]["fix_rounds"] == 2


def test_external_merge_without_durable_authorization_needs_human(monkeypatch, tmp_path: Path) -> None:
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

        def ensure_pr_metadata(self, **kwargs):
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
                "todo_paths": ["docs/todo.md"],
                "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert result["result"] == {
        "action": "needs_human",
        "reason": "external-merge-without-authorization",
    }
    assert merge_calls == []


def test_crash_reconcile_uses_stable_authorization_without_rerunning_preflight(
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
    _initialize_delivery_journal(snapshot=snapshot, state=state)
    persisted = json.loads(state.read_text(encoding="utf-8"))
    run = next(iter(persisted["runs"].values()))
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    authorization = work_actions._authorization_record(
        {
            "schema": "cortex-merge-authorization/v1",
            "run_id": run["run_id"],
            "workflow_step_ids": run["workflow_step_ids"],
            "repo": "acme/demo",
            "work_id": "demo",
            "authority_digest": work_actions.work_authority_digest(authority),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": ["docs/todo.md"],
            "head": HEAD,
            "tree_hash": TREE,
            "copilot_requested_at_epoch": 200.0,
            "copilot_review_id": 9,
            "copilot_hash": "1" * 64,
            "foreign_review_path": "/evidence/review.json",
            "foreign_review_hash": "2" * 64,
            "preflight_hash": "3" * 64,
            "checks_hash": "4" * 64,
        },
        state_path=state,
    )
    run["delivery_binding"] = {
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
    }
    run["ship"] = {
        "phase": "merge-authorized",
        "head": HEAD,
        "tree_hash": TREE,
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
        "merge_authorization": authorization,
    }
    state.write_text(json.dumps(persisted), encoding="utf-8")

    class GitHub:
        def __init__(self, *, runner):
            pass

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(True, HEAD, "c" * 40)

        def ensure_pr_metadata(self, **kwargs):
            raise AssertionError("crash reconciliation must not rewrite PR metadata")

    class Orchestrator:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", Orchestrator)
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("crash reconciliation must not rerun preflight")
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
            "todo_paths": ["docs/todo.md"],
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 220,
    )
    assert result["result"]["action"] == "merged-awaiting-closure"


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
    _initialize_delivery_journal(snapshot=snapshot, state=state)
    persisted = json.loads(state.read_text(encoding="utf-8"))
    run = next(iter(persisted["runs"].values()))
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    authorization = work_actions._authorization_record(
        {
            "schema": "cortex-merge-authorization/v1",
            "run_id": run["run_id"],
            "workflow_step_ids": run["workflow_step_ids"],
            "repo": "acme/demo",
            "work_id": "demo",
            "authority_digest": work_actions.work_authority_digest(authority),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": ["docs/todo.md"],
            "head": HEAD,
            "tree_hash": TREE,
            "copilot_requested_at_epoch": 200.0,
            "copilot_review_id": 9,
            "copilot_hash": "1" * 64,
            "foreign_review_path": "/evidence/review.json",
            "foreign_review_hash": "2" * 64,
            "preflight_hash": "3" * 64,
            "checks_hash": "4" * 64,
        },
        state_path=state,
    )
    run["delivery_binding"] = {
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
    }
    run["ship"] = {
        "phase": "done",
        "head": HEAD,
        "tree_hash": TREE,
        "todo_paths": ["docs/todo.md"],
        "merge_authorization": authorization,
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
            "todo_paths": ["docs/todo.md"],
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
                "todo_paths": ["docs/todo.md"],
                "pr_metadata_path": str(metadata),
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
            runner=runner,
        )
    assert ["openspec", "archive", "-y", "demo"] not in calls


@pytest.mark.parametrize(
    ("changelog", "policy_stdout", "reason"),
    [
        ("## [Unreleased]\n- **other**: done\n", "", "changelog-missing"),
        (
            "## [Unreleased]\n- **demo**: done\n",
            "WARN R-22 doc-reference stale link",
            "doc-reference-invalid",
        ),
    ],
)
def test_archive_requires_change_specific_changelog_and_no_doc_reference_warning(
    tmp_path: Path, changelog: str, policy_stdout: str, reason: str
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
    change_dir = tmp_path / "openspec" / "changes" / "demo"
    change_dir.mkdir(parents=True)
    (change_dir / "tasks.md").write_text("- [x] complete\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")

    def runner(argv, **kwargs):
        stdout = policy_stdout if argv[:3] == ["python3", "-m", "policy_check"] else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    with pytest.raises(RuntimeError, match=reason):
        work_actions.execute_work_action(
            args={
                "action": "ship",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(tmp_path),
                "pr_number": 8,
                "change": "demo",
                "todo_paths": ["docs/todo.md"],
                "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
            runner=runner,
        )
