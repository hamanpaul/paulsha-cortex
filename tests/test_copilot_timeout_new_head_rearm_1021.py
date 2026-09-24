"""#1021 RED: exact-head rearm after a timed-out Copilot stop."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.github_delivery import (
    COPILOT_REVIEWER_LOGIN,
    CopilotReview,
    DeliveryFacts,
    GitHubCheck,
    ReviewThread,
)
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult
from paulsha_cortex.coordinator.registry import JobRegistry


OLD_HEAD = "a" * 40
NEW_HEAD = "c" * 40
RACE_HEAD = "e" * 40
OLD_TREE = "b" * 40
NEW_TREE = "d" * 40
REPO = "acme/demo"
WORK_ID = "copilot-timeout-new-head-review-rearm"
TODO_PATH = f"docs/superpowers/workstreams/{WORK_ID}/todo.md"


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", f"git@github.com:{REPO}.git"],
            check=True,
        )


def _snapshot(
    path: Path,
    *,
    issues: tuple[int, ...] = (1021,),
    prs: tuple[int, ...] = (954,),
    changes: tuple[str, ...] = (),
    todo_paths: tuple[str, ...] = (TODO_PATH,),
) -> Path:
    _init_repo(path.parent)
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
                        "repo": REPO,
                        "work_id": WORK_ID,
                        "mapped_issues": list(issues),
                        "mapped_prs": list(prs),
                        "mapped_openspec": list(changes),
                        "mapped_todo_paths": list(todo_paths),
                        "confirmed_todo": True,
                        "auto_label": False,
                        "source_revisions": ["issue:1021@open"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _start_run(*, snapshot: Path, state: Path, registry: JobRegistry) -> str:
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": REPO, "work_id": WORK_ID},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    return started["result"]["run"]["run_id"]


def _journal_row(path: Path, run_id: str) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))["runs"][run_id]


def _write_journal_row(path: Path, run_id: str, row: dict[str, Any]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runs"][run_id] = row
    path.write_text(json.dumps(payload), encoding="utf-8")


def _pr_metadata(
    path: Path,
    *,
    title: str = "fix(ship): 允許新 HEAD 明示重啟 Copilot review",
    body: str = "Closes #1021",
) -> Path:
    path.write_text(
        json.dumps({"title": title, "body": body, "labels": ["bug"]}),
        encoding="utf-8",
    )
    return path


class FakeGitHubDeliveryClient:
    def __init__(self) -> None:
        self.remote_head = OLD_HEAD
        self.reviews: tuple[CopilotReview, ...] = ()
        self.threads: tuple[ReviewThread, ...] = ()
        self.closing_issues: tuple[int, ...] = (1021,)
        self.checks: tuple[GitHubCheck, ...] = (
            GitHubCheck("pytest", "completed", "success"),
        )
        self.mergeable = True
        self.mergeable_state = "clean"
        self.request_copilot_calls = 0
        self.merged = False
        self.merge_commit = "f" * 40
        self.metadata_calls: list[dict[str, Any]] = []

    def ensure_pr_metadata(self, **kwargs):
        self.metadata_calls.append(kwargs)

    def fetch_delivery_facts(self, **kwargs):
        return DeliveryFacts(
            head=self.remote_head,
            mergeable=self.mergeable,
            mergeable_state=self.mergeable_state,
            checks=self.checks,
            copilot_reviews=self.reviews,
            review_threads=self.threads,
            closing_issues=self.closing_issues,
            active_openspec_absent=True,
            archive_present=True,
            openspec_required=False,
        )

    def fetch_merge_status(self, **kwargs):
        return SimpleNamespace(
            merged=self.merged,
            pr_head=self.remote_head,
            merge_commit=self.merge_commit if self.merged else None,
        )

    def request_copilot(self, **kwargs):
        self.request_copilot_calls += 1


class FakeShipOrchestrator:
    def __init__(self, *, github, now):
        self._github = github
        self._now = now
        self.calls: list[str] = []

    def merge_if_ready(self, **kwargs):
        self.calls.append("merge-if-ready")
        self._github.merged = True
        return SimpleNamespace(
            expected_head=kwargs["expected_head"],
            expected_tree_hash=kwargs["expected_tree_hash"],
        )

    def verify_remote_closure(self, **kwargs):
        self.calls.append("verify-remote-closure")
        return SimpleNamespace(
            facts=SimpleNamespace(merge_commit="f" * 40),
            completion_record={"path": "/evidence/completion.json", "hash": "d" * 64},
        )


def _setup_ship_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeGitHubDeliveryClient, list[FakeShipOrchestrator], Path, Path, JobRegistry, str, Any]:
    snapshot = _snapshot(tmp_path / "snapshot.json", changes=())
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run_id = _start_run(snapshot=snapshot, state=state, registry=registry)
    authority = work_actions.load_work_authority(
        repo=REPO,
        work_id=WORK_ID,
        snapshot_path=snapshot,
    )
    github = FakeGitHubDeliveryClient()
    orchestrator_holder: list[FakeShipOrchestrator] = []

    def orchestrator_factory(*args, **kwargs):
        orch = FakeShipOrchestrator(*args, **kwargs)
        orchestrator_holder.append(orch)
        return orch

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", lambda **kwargs: github)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", orchestrator_factory)
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: PreflightResult(
            passed=True,
            failed_stage=None,
            policy=CommandResult(("policy",), 0, "", ""),
            ci_parity=CommandResult(("preflight",), 0, "", ""),
            head=OLD_HEAD,
            tree_hash=OLD_TREE,
        ),
    )
    monkeypatch.setattr(
        work_actions,
        "_validate_foreign_review",
        lambda *args, **kwargs: {"state": "passed", "candidate": OLD_HEAD},
    )

    return github, orchestrator_holder, snapshot, state, registry, run_id, authority


def _set_preflight(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head: str,
    tree_hash: str,
) -> None:
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: PreflightResult(
            passed=True,
            failed_stage=None,
            policy=CommandResult(("policy",), 0, "", ""),
            ci_parity=CommandResult(("preflight",), 0, "", ""),
            head=head,
            tree_hash=tree_hash,
        ),
    )
    monkeypatch.setattr(
        work_actions,
        "_validate_foreign_review",
        lambda *args, **kwargs: {"state": "passed", "candidate": head},
    )


def _invoke_ship(
    tmp_path: Path,
    *,
    authority,
    state: Path,
    registry: JobRegistry,
    head: str,
    now: float,
    extra_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    foreign_normalized = {"state": "passed", "candidate": head}
    foreign_path = tmp_path / f"foreign-review-{head}.json"
    foreign_path.write_text(json.dumps(foreign_normalized), encoding="utf-8")
    foreign_hash = hashlib.sha256(
        json.dumps(foreign_normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    args = {
        "repo_root": str(tmp_path),
        "pr_number": 954,
        "change": None,
        "todo_paths": [TODO_PATH],
        "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        "foreign_review_path": str(foreign_path),
        "foreign_review_hash": foreign_hash,
    }
    if extra_args:
        args.update(extra_args)
    return work_actions._ship_action(
        args=args,
        authority=authority,
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        now=lambda: now,
        state_path=state,
        workflow_registry=registry,
    )


def _advance_run_to_ship(registry: JobRegistry, run_id: str, *, candidate_head: str) -> None:
    for phase in ("plan", "build", "verify", "review"):
        registry._manager_update_workflow_run(run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=candidate_head,
        verified_head=candidate_head,
        gate_status="running",
    )


def _create_timeout_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    authority,
    state: Path,
    registry: JobRegistry,
    github: FakeGitHubDeliveryClient,
) -> None:
    github.remote_head = OLD_HEAD
    github.reviews = ()
    github.threads = ()
    _set_preflight(monkeypatch, head=OLD_HEAD, tree_hash=OLD_TREE)
    first = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=OLD_HEAD,
        now=1_000.0,
    )
    assert first == {"action": "awaiting-copilot", "head": OLD_HEAD}
    timed_out = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=OLD_HEAD,
        now=1_000.0 + 15 * 60.0 + 1.0,
    )
    assert timed_out["action"] == "needs_human"
    assert timed_out["reason"] == "copilot-review-timeout"


def _resume(
    *,
    snapshot: Path,
    state: Path,
    registry: JobRegistry,
    at: float,
) -> dict[str, Any]:
    return work_actions.execute_work_action(
        args={"action": "resume", "repo": REPO, "work_id": WORK_ID},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: at,
        workflow_registry=registry,
    )


def test_explicit_resume_writes_a_manager_owned_idempotent_rearm_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )

    resumed = _resume(snapshot=snapshot, state=state, registry=registry, at=4_000.0)
    first_row = _journal_row(state, run_id)
    permit = first_row["copilot_review_rearm_permit"]
    assert resumed["result"]["run"]["copilot_review_rearm_permit"] == permit
    assert permit["run_id"] == run_id
    assert permit["old_head"] == OLD_HEAD
    assert permit["candidate_head"] == NEW_HEAD
    assert permit["authority_digest"] == work_actions.work_authority_digest(authority)
    assert permit["delivery_binding_hash"] == work_actions._delivery_binding_hash(
        first_row["delivery_binding"]
    )
    assert permit["history_prefix_hash"] == work_actions.verification.canonical_json_hash([])
    assert permit["requested_by"] == "operator"
    assert permit["transition_id"].startswith("manager-transition-")

    resumed_again = _resume(snapshot=snapshot, state=state, registry=registry, at=4_001.0)
    second_row = _journal_row(state, run_id)

    assert resumed_again["result"]["run"]["copilot_review_rearm_permit"] == permit
    assert second_row["copilot_review_rearm_permit"] == permit


def test_resume_rejects_caller_supplied_rearm_authority_inputs(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", changes=())
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    with pytest.raises(ValueError, match="resume rejects caller evidence/input: head"):
        work_actions.execute_work_action(
            args={"action": "resume", "repo": REPO, "work_id": WORK_ID, "head": NEW_HEAD},
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
            workflow_registry=registry,
        )


def test_explicit_resume_is_required_to_rearm_a_timed_out_old_head_on_new_exact_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = NEW_HEAD
    github.reviews = ()
    _set_preflight(monkeypatch, head=NEW_HEAD, tree_hash=NEW_TREE)

    without_resume = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=NEW_HEAD,
        now=4_000.0,
    )
    assert without_resume["action"] == "needs_human"
    assert without_resume["reason"] == "copilot-review-timeout"

    _resume(snapshot=snapshot, state=state, registry=registry, at=4_001.0)

    result = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=NEW_HEAD,
        now=4_002.0,
    )

    assert result == {"action": "awaiting-copilot", "head": NEW_HEAD}
    assert github.request_copilot_calls == 2
    row = _journal_row(state, run_id)
    assert row["ship"]["phase"] == "review-requested"
    assert row["ship"]["head"] == NEW_HEAD
    assert row["ship"]["tree_hash"] == NEW_TREE
    assert row["ship"]["rearm"] == {
        "run_id": run_id,
        "from_head": OLD_HEAD,
        "authority_digest": work_actions.work_authority_digest(authority),
        "delivery_binding_hash": work_actions._delivery_binding_hash(row["delivery_binding"]),
        "requested_by": "operator",
        "transition_id": row["ship"]["rearm"]["transition_id"],
    }
    assert "adopted_review_id" not in row["ship"]
    assert "merge_authorization" not in row["ship"]
    assert "copilot_review_rearm_permit" not in row
    assert len(row["delivery_review_epochs"]) == 1
    archived_epoch = row["delivery_review_epochs"][0]
    assert archived_epoch["schema"] == work_actions._DELIVERY_REVIEW_EPOCH_SCHEMA
    assert archived_epoch["ship"]["reason"] == "copilot-review-timeout"
    assert archived_epoch["ship"]["head"] == OLD_HEAD
    assert archived_epoch["ship_hash"] == work_actions._ship_state_hash(archived_epoch["ship"])
    assert archived_epoch["rearm"]["transition_id"] == row["ship"]["rearm"]["transition_id"]


def test_explicit_resume_adopts_an_existing_new_head_copilot_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = NEW_HEAD
    github.reviews = (
        CopilotReview(
            review_id=41,
            commit_id=OLD_HEAD,
            state="COMMENTED",
            body="Looks good on the timed-out head.",
            author=COPILOT_REVIEWER_LOGIN,
            submitted_at_epoch=1_050.0,
        ),
        CopilotReview(
            review_id=99,
            commit_id=NEW_HEAD,
            state="COMMENTED",
            body="LGTM on the exact new head.",
            author=COPILOT_REVIEWER_LOGIN,
            submitted_at_epoch=4_500.0,
        ),
    )
    _set_preflight(monkeypatch, head=NEW_HEAD, tree_hash=NEW_TREE)

    _resume(snapshot=snapshot, state=state, registry=registry, at=4_600.0)

    result = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=NEW_HEAD,
        now=4_601.0,
    )

    assert github.request_copilot_calls == 1
    assert result["action"] in {"merge-authorized", "merged-awaiting-closure"}
    row = _journal_row(state, run_id)
    assert row["ship"]["head"] == NEW_HEAD
    assert row["ship"]["adopted_review_id"] == 99
    assert row["ship"]["requested_at_epoch"] == 4_500.0
    assert row["ship"]["phase"] in {"merge-authorized", "merged"}
    if orch_holder:
        assert any("merge-if-ready" in orch.calls for orch in orch_holder)


@pytest.mark.parametrize(
    ("review", "case_id"),
    (
        (
            CopilotReview(
                review_id=51,
                commit_id=OLD_HEAD,
                state="COMMENTED",
                body="Old-head approval must not authorize the new epoch.",
                author=COPILOT_REVIEWER_LOGIN,
                submitted_at_epoch=2_000.0,
            ),
            "old-head",
        ),
        (
            CopilotReview(
                review_id=52,
                commit_id=NEW_HEAD,
                state="COMMENTED",
                body="Copilot encountered an error while reviewing changes.",
                author=COPILOT_REVIEWER_LOGIN,
                submitted_at_epoch=2_000.0,
            ),
            "error-body",
        ),
        (
            CopilotReview(
                review_id=53,
                commit_id=NEW_HEAD,
                state="COMMENTED",
                body="Human review is not a Copilot epoch.",
                author="octocat",
                submitted_at_epoch=2_000.0,
            ),
            "foreign-author",
        ),
        (
            CopilotReview(
                review_id=54,
                commit_id=NEW_HEAD,
                state="CHANGES_REQUESTED",
                body="Unsupported state must not be adopted.",
                author=COPILOT_REVIEWER_LOGIN,
                submitted_at_epoch=2_000.0,
            ),
            "unsupported-state",
        ),
    ),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_explicit_resume_does_not_carry_invalid_or_old_reviews_into_the_new_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    review: CopilotReview,
    case_id: str,
) -> None:
    del case_id
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = NEW_HEAD
    github.reviews = (review,)
    _set_preflight(monkeypatch, head=NEW_HEAD, tree_hash=NEW_TREE)

    _resume(snapshot=snapshot, state=state, registry=registry, at=4_700.0)

    result = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=NEW_HEAD,
        now=4_701.0,
    )

    assert result == {"action": "awaiting-copilot", "head": NEW_HEAD}
    assert github.request_copilot_calls == 2
    row = _journal_row(state, run_id)
    assert row["ship"]["phase"] == "review-requested"
    assert row["ship"]["head"] == NEW_HEAD
    assert "adopted_review_id" not in row["ship"]


def test_same_new_head_timeout_does_not_request_again_after_a_second_explicit_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = NEW_HEAD
    github.reviews = ()
    _set_preflight(monkeypatch, head=NEW_HEAD, tree_hash=NEW_TREE)

    _resume(snapshot=snapshot, state=state, registry=registry, at=4_800.0)
    first = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=NEW_HEAD,
        now=4_801.0,
    )
    assert first == {"action": "awaiting-copilot", "head": NEW_HEAD}
    assert github.request_copilot_calls == 2

    timed_out = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=NEW_HEAD,
        now=4_801.0 + 15 * 60.0 + 1.0,
    )
    assert timed_out["action"] == "needs_human"
    assert timed_out["reason"] == "copilot-review-timeout"

    _resume(snapshot=snapshot, state=state, registry=registry, at=5_800.0)
    replay = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=NEW_HEAD,
        now=5_801.0,
    )

    assert replay["action"] == "needs_human"
    assert replay["reason"] == "copilot-review-timeout"
    assert github.request_copilot_calls == 2
    assert "copilot_review_rearm_permit" not in _journal_row(state, run_id)


def test_rearm_fails_closed_when_delivery_review_history_was_rewritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = NEW_HEAD
    github.reviews = ()
    _set_preflight(monkeypatch, head=NEW_HEAD, tree_hash=NEW_TREE)

    _resume(snapshot=snapshot, state=state, registry=registry, at=4_850.0)
    row = _journal_row(state, run_id)
    fake_ship = {
        "phase": "needs_human",
        "reason": "copilot-review-timeout",
        "head": "f" * 40,
        "tree_hash": "1" * 40,
        "requested_at_epoch": 123.0,
        "epoch_started_at": 123.0,
        "fix_rounds": 0,
        "pr_number": 954,
        "change": None,
        "todo_paths": [TODO_PATH],
    }
    row["delivery_review_epochs"] = [
        {
            "schema": work_actions._DELIVERY_REVIEW_EPOCH_SCHEMA,
            "ship": fake_ship,
            "ship_hash": work_actions._ship_state_hash(fake_ship),
            "archived_at_epoch": 124.0,
            "rearm": row["copilot_review_rearm_permit"],
        }
    ]
    _write_journal_row(state, run_id, row)

    with pytest.raises(RuntimeError, match="copilot timeout rearm permit conflicts with current state"):
        _invoke_ship(
            tmp_path,
            authority=authority,
            state=state,
            registry=registry,
            head=NEW_HEAD,
            now=4_851.0,
        )

    persisted = _journal_row(state, run_id)
    assert persisted["ship"]["reason"] == "copilot-review-timeout"
    assert "copilot_review_rearm_permit" not in persisted


def test_resume_invalidates_a_conflicting_persisted_rearm_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )

    _resume(snapshot=snapshot, state=state, registry=registry, at=4_860.0)
    row = _journal_row(state, run_id)
    row["copilot_review_rearm_permit"]["history_prefix_hash"] = "0" * 64
    _write_journal_row(state, run_id, row)

    with pytest.raises(RuntimeError, match="copilot timeout rearm permit conflicts with current state"):
        _resume(snapshot=snapshot, state=state, registry=registry, at=4_861.0)

    persisted = _journal_row(state, run_id)
    assert persisted["ship"]["reason"] == "copilot-review-timeout"
    assert "copilot_review_rearm_permit" not in persisted


def test_binding_drift_invalidates_the_old_head_rearm_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = NEW_HEAD
    github.reviews = ()
    _set_preflight(monkeypatch, head=NEW_HEAD, tree_hash=NEW_TREE)

    _resume(snapshot=snapshot, state=state, registry=registry, at=4_875.0)
    row = _journal_row(state, run_id)
    row["delivery_binding"] = {
        **row["delivery_binding"],
        "todo_paths": ["docs/superpowers/workstreams/other/todo.md"],
    }
    _write_journal_row(state, run_id, row)

    with pytest.raises(
        RuntimeError,
        match="ship delivery binding differs from persisted PR/OpenSpec/Todo refs",
    ):
        _invoke_ship(
            tmp_path,
            authority=authority,
            state=state,
            registry=registry,
            head=NEW_HEAD,
            now=4_876.0,
        )

    persisted = _journal_row(state, run_id)
    assert persisted["ship"]["reason"] == "copilot-review-timeout"
    assert "copilot_review_rearm_permit" not in persisted
    assert github.request_copilot_calls == 1


def test_explicit_resume_stays_fail_closed_when_the_exact_pr_head_has_raced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = RACE_HEAD
    github.reviews = ()
    _set_preflight(monkeypatch, head=NEW_HEAD, tree_hash=NEW_TREE)

    _resume(snapshot=snapshot, state=state, registry=registry, at=4_900.0)

    with pytest.raises(RuntimeError, match="ship HEAD differs from authenticated GitHub PR"):
        _invoke_ship(
            tmp_path,
            authority=authority,
            state=state,
            registry=registry,
            head=NEW_HEAD,
            now=4_901.0,
        )
    assert github.request_copilot_calls == 1
    assert "copilot_review_rearm_permit" not in _journal_row(state, run_id)


def test_explicit_resume_invalidates_the_old_head_rearm_permit_when_preflight_head_drifted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = RACE_HEAD
    github.reviews = ()
    _set_preflight(monkeypatch, head=RACE_HEAD, tree_hash=NEW_TREE)

    _resume(snapshot=snapshot, state=state, registry=registry, at=4_925.0)

    with pytest.raises(RuntimeError, match="ship preflight HEAD differs from explicit resume permit"):
        _invoke_ship(
            tmp_path,
            authority=authority,
            state=state,
            registry=registry,
            head=RACE_HEAD,
            now=4_926.0,
        )

    persisted = _journal_row(state, run_id)
    assert persisted["ship"]["reason"] == "copilot-review-timeout"
    assert "copilot_review_rearm_permit" not in persisted
    assert github.request_copilot_calls == 1


def test_explicit_resume_timeout_rearm_rejects_maintainer_merge_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = NEW_HEAD
    github.reviews = ()
    _set_preflight(monkeypatch, head=NEW_HEAD, tree_hash=NEW_TREE)
    _resume(snapshot=snapshot, state=state, registry=registry, at=4_930.0)

    def fail_if_called(**kwargs):
        raise AssertionError("_ship_with_maintainer_review must not run for timeout rearm")

    monkeypatch.setattr(work_actions, "_ship_with_maintainer_review", fail_if_called)

    with pytest.raises(RuntimeError, match="copilot timeout rearm requires exact-HEAD Copilot review"):
        _invoke_ship(
            tmp_path,
            authority=authority,
            state=state,
            registry=registry,
            head=NEW_HEAD,
            now=4_931.0,
            extra_args={
                "maintainer_review_path": str(tmp_path / "maintainer-review.json"),
                "maintainer_review_hash": "a" * 64,
            },
        )

    persisted = _journal_row(state, run_id)
    assert persisted["ship"]["reason"] == "copilot-review-timeout"
    assert persisted["copilot_review_rearm_permit"]["candidate_head"] == NEW_HEAD
    assert github.request_copilot_calls == 1


@pytest.mark.parametrize(
    ("reviews", "case_id"),
    (
        ((), "request"),
        (
            (
                CopilotReview(
                    review_id=123,
                    commit_id=NEW_HEAD,
                    state="COMMENTED",
                    body="LGTM on the exact new head.",
                    author=COPILOT_REVIEWER_LOGIN,
                    submitted_at_epoch=4_950.0,
                ),
            ),
            "adopt",
        ),
    ),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_explicit_resume_rereads_exact_head_foreign_review_before_consuming_the_rearm_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reviews: tuple[CopilotReview, ...],
    case_id: str,
) -> None:
    del case_id
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = NEW_HEAD
    github.reviews = reviews
    _set_preflight(monkeypatch, head=NEW_HEAD, tree_hash=NEW_TREE)

    _resume(snapshot=snapshot, state=state, registry=registry, at=4_940.0)

    calls = 0

    def reject_foreign_review(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("foreign review does not authorize exact HEAD")

    monkeypatch.setattr(work_actions, "_validate_foreign_review", reject_foreign_review)

    with pytest.raises(RuntimeError, match="foreign review does not authorize exact HEAD"):
        _invoke_ship(
            tmp_path,
            authority=authority,
            state=state,
            registry=registry,
            head=NEW_HEAD,
            now=4_941.0,
        )

    assert calls == 1
    assert github.request_copilot_calls == 1
    persisted = _journal_row(state, run_id)
    assert persisted["ship"]["reason"] == "copilot-review-timeout"
    assert persisted["ship"]["head"] == OLD_HEAD
    assert "copilot_review_rearm_permit" in persisted
    assert "delivery_review_epochs" not in persisted


def test_review_requesting_replay_without_exact_head_review_fails_closed_without_resending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = NEW_HEAD
    github.reviews = ()
    _set_preflight(monkeypatch, head=NEW_HEAD, tree_hash=NEW_TREE)

    _resume(snapshot=snapshot, state=state, registry=registry, at=4_950.0)
    row = _journal_row(state, run_id)
    permit = row["copilot_review_rearm_permit"]
    old_ship = dict(row["ship"])
    row["delivery_review_epochs"] = [
        {
            "schema": work_actions._DELIVERY_REVIEW_EPOCH_SCHEMA,
            "ship": old_ship,
            "ship_hash": work_actions._ship_state_hash(old_ship),
            "archived_at_epoch": 4_951.0,
            "rearm": permit,
        }
    ]
    row.pop("copilot_review_rearm_permit")
    row["ship"] = {
        "phase": "review-requesting",
        "head": NEW_HEAD,
        "tree_hash": NEW_TREE,
        "requested_at_epoch": 4_951.0,
        "epoch_started_at": 4_951.0,
        "fix_rounds": 1,
        "pr_number": 954,
        "change": None,
        "todo_paths": [TODO_PATH],
        "rearm": work_actions._copilot_epoch_rearm_record(permit),
    }
    _write_journal_row(state, run_id, row)

    result = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=NEW_HEAD,
        now=4_952.0,
    )

    assert result["action"] == "needs_human"
    assert result["reason"] == "copilot-review-request-outcome-unknown"
    assert github.request_copilot_calls == 1
    persisted = _journal_row(state, run_id)
    assert persisted["ship"]["phase"] == "needs_human"
    assert persisted["ship"]["reason"] == "copilot-review-request-outcome-unknown"


def test_explicit_resume_request_error_becomes_outcome_unknown_and_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch
    )
    _advance_run_to_ship(registry, run_id, candidate_head=OLD_HEAD)
    _create_timeout_stop(
        tmp_path,
        monkeypatch,
        authority=authority,
        state=state,
        registry=registry,
        github=github,
    )
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=NEW_HEAD,
        verified_head=NEW_HEAD,
    )
    github.remote_head = NEW_HEAD
    github.reviews = ()
    _set_preflight(monkeypatch, head=NEW_HEAD, tree_hash=NEW_TREE)

    def raising_request(**kwargs):
        github.request_copilot_calls += 1
        raise RuntimeError("transient gh failure")

    github.request_copilot = raising_request

    _resume(snapshot=snapshot, state=state, registry=registry, at=5_000.0)

    first = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=NEW_HEAD,
        now=5_001.0,
    )

    assert first["action"] == "needs_human"
    assert first["reason"] == "copilot-review-request-outcome-unknown"
    assert github.request_copilot_calls == 2

    second = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        head=NEW_HEAD,
        now=5_002.0,
    )

    assert second["action"] == "needs_human"
    assert second["reason"] == "copilot-review-request-outcome-unknown"
    assert github.request_copilot_calls == 2
