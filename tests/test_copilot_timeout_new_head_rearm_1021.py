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
) -> dict[str, Any]:
    foreign_normalized = {"state": "passed", "candidate": head}
    foreign_path = tmp_path / f"foreign-review-{head}.json"
    foreign_path.write_text(json.dumps(foreign_normalized), encoding="utf-8")
    foreign_hash = hashlib.sha256(
        json.dumps(foreign_normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return work_actions._ship_action(
        args={
            "repo_root": str(tmp_path),
            "pr_number": 954,
            "change": None,
            "todo_paths": [TODO_PATH],
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
            "foreign_review_path": str(foreign_path),
            "foreign_review_hash": foreign_hash,
        },
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
    assert "adopted_review_id" not in row["ship"]
    assert "merge_authorization" not in row["ship"]


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
        assert "merge-if-ready" in orch_holder[0].calls


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
