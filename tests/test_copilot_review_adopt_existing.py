"""#948: Tests for adopting existing exact-HEAD Copilot reviews during ship.

RED regression suite corresponding to spec R6 (a)-(f).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator import review as review_evidence
from paulsha_cortex.coordinator import work_bridge
from paulsha_cortex.coordinator.claim import load_work_authority, work_authority_digest
from paulsha_cortex.coordinator.delivery import (
    ReviewLoop,
    REVIEW_TIMEOUT_SECONDS,
)
from paulsha_cortex.coordinator.github_delivery import (
    COPILOT_REVIEWER_LOGIN,
    CopilotReview,
    DeliveryFacts,
    GitHubCheck,
    ReviewThread,
)
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import GateEvidenceRef, WorkflowStep


HEAD = "a" * 40
TREE = "b" * 40
REPO = "acme/demo"
WORK_ID = "copilot-adopt"
TODO_PATH = "docs/todo.md"


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
    issues: tuple[int, ...] = (12,),
    prs: tuple[int, ...] = (8,),
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
                        "source_revisions": ["issue:12@open"],
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
    title: str = "feat(ship): 採信既有 exact-HEAD Copilot review",
    body: str = "Closes #12",
) -> Path:
    path.write_text(
        json.dumps({"title": title, "body": body, "labels": ["enhancement"]}),
        encoding="utf-8",
    )
    return path


def _workflow_steps() -> tuple[WorkflowStep, ...]:
    personas = {
        "claim": "manager",
        "define": "planner",
        "plan": "planner",
        "build": "builder",
        "verify": "reviewer",
        "review": "reviewer",
        "ship": "manager",
    }
    return tuple(
        WorkflowStep(
            phase=phase,
            persona=persona,
            card=f"{phase}-card",
            executor=("codex" if phase == "build" else "claude"),
            model=("gpt" if phase == "build" else "sonnet"),
            domain=("openai" if phase == "build" else "anthropic"),
            inputs=(),
            outputs=(),
            gate_result="pending" if phase == "ship" else "passed",
        )
        for phase, persona in personas.items()
    )


class FakeGitHubDeliveryClient:
    def __init__(self, *, runner=None):
        self.runner = runner
        self.request_copilot_calls = 0
        self.reviews: tuple[CopilotReview, ...] = ()
        self.threads: tuple[ReviewThread, ...] = ()
        self.closing_issues: tuple[int, ...] = (12,)
        self.merged = False
        self.merge_commit = "c" * 40
        self.metadata_calls: list[dict[str, Any]] = []

    def ensure_pr_metadata(self, **kwargs):
        self.metadata_calls.append(kwargs)

    def fetch_delivery_facts(self, **kwargs):
        return DeliveryFacts(
            head=HEAD,
            mergeable=True,
            mergeable_state="clean",
            checks=(GitHubCheck("pytest", "completed", "success"),),
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
            pr_head=HEAD,
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
            facts=SimpleNamespace(merge_commit="c" * 40),
            completion_record={"path": "/evidence/completion.json", "hash": "d" * 64},
        )


def _setup_ship_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reviews: tuple[CopilotReview, ...] = (),
    threads: tuple[ReviewThread, ...] = (),
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
    github.reviews = reviews
    github.threads = threads
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
            head=HEAD,
            tree_hash=TREE,
        ),
    )
    foreign_normalized = {"state": "passed", "candidate": HEAD}
    monkeypatch.setattr(
        work_actions,
        "_validate_foreign_review",
        lambda *args, **kwargs: foreign_normalized,
    )
    foreign_path = tmp_path / "foreign-review.json"
    foreign_path.write_text(json.dumps(foreign_normalized), encoding="utf-8")

    return github, orchestrator_holder, snapshot, state, registry, run_id, authority


def _invoke_ship(
    tmp_path: Path,
    *,
    authority,
    state: Path,
    registry: JobRegistry,
    now: float,
) -> dict[str, Any]:
    foreign_path = tmp_path / "foreign-review.json"
    foreign_normalized = {"state": "passed", "candidate": HEAD}
    foreign_hash = hashlib.sha256(
        json.dumps(foreign_normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return work_actions._ship_action(
        args={
            "repo_root": str(tmp_path),
            "pr_number": 8,
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


def test_r6_a_pr_created_with_copilot_commented_review_manager_ships_40m_later_without_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) PR 建立即有 Copilot COMMENTED review（0 finding）、Manager 40 分鐘後才進 ship

    驗證：不呼叫 request_copilot、採信既有 review、同 tick 進入 merge-authorized（此處接 merge_if_ready）。
    """
    review_submitted_at = 1000.0
    ship_now = review_submitted_at + 40 * 60.0  # 40 分鐘後 (3400.0)
    existing_review = CopilotReview(
        review_id=42,
        commit_id=HEAD,
        state="COMMENTED",
        body="LGTM, no findings.",
        author=COPILOT_REVIEWER_LOGIN,
        submitted_at_epoch=review_submitted_at,
    )
    github, orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch, reviews=(existing_review,), threads=()
    )

    result = _invoke_ship(tmp_path, authority=authority, state=state, registry=registry, now=ship_now)

    # 必須未呼叫 request_copilot
    assert github.request_copilot_calls == 0
    # 同 tick 不得回 awaiting-copilot，應直接進入 merge-authorized / merged
    assert result.get("action") != "awaiting-copilot"
    row = _journal_row(state, run_id)
    ship_state = row["ship"]
    assert ship_state.get("adopted_review_id") == 42
    assert ship_state.get("adopted_at_epoch") == ship_now
    assert ship_state.get("requested_at_epoch") == review_submitted_at
    assert ship_state.get("phase") in {"merge-authorized", "merged"}
    if orch_holder:
        assert "merge-if-ready" in orch_holder[0].calls


def test_r6_b_existing_copilot_review_with_one_finding_routes_to_fix_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) 同情境 1 finding → fix-required（既有路徑）。"""
    review_submitted_at = 1000.0
    ship_now = review_submitted_at + 40 * 60.0
    existing_review = CopilotReview(
        review_id=43,
        commit_id=HEAD,
        state="COMMENTED",
        body="Found an issue.",
        author=COPILOT_REVIEWER_LOGIN,
        submitted_at_epoch=review_submitted_at,
    )
    blocking_thread = ReviewThread(
        thread_id="thread-1",
        resolved=False,
        outdated=False,
        path="src/app.py",
        line=42,
        body_excerpt="Possible NoneType dereference.",
    )
    github, orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch, reviews=(existing_review,), threads=(blocking_thread,)
    )

    result = _invoke_ship(tmp_path, authority=authority, state=state, registry=registry, now=ship_now)

    assert github.request_copilot_calls == 0
    assert result.get("action") == "fix-required"
    row = _journal_row(state, run_id)
    ship_state = row["ship"]
    assert ship_state.get("phase") == "needs-fix"
    assert ship_state.get("review_id") == 43
    assert ship_state.get("finding_count") == 1


def test_r6_c_existing_copilot_error_review_is_not_adopted_and_falls_back_to_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) 既有 review 為 error → 不採信、走 request。"""
    review_submitted_at = 1000.0
    ship_now = review_submitted_at + 40 * 60.0
    error_review = CopilotReview(
        review_id=44,
        commit_id=HEAD,
        state="COMMENTED",
        body="Copilot encountered an error while reviewing changes.",
        author=COPILOT_REVIEWER_LOGIN,
        submitted_at_epoch=review_submitted_at,
    )
    assert error_review.is_error is True

    github, orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch, reviews=(error_review,), threads=()
    )

    result = _invoke_ship(tmp_path, authority=authority, state=state, registry=registry, now=ship_now)

    assert github.request_copilot_calls == 1
    assert result.get("action") == "awaiting-copilot"
    row = _journal_row(state, run_id)
    ship_state = row["ship"]
    assert ship_state.get("phase") == "review-requested"
    assert "adopted_review_id" not in ship_state


def test_r6_d_existing_copilot_review_for_old_head_is_not_adopted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) 既有 review 是舊 head → 不採信。"""
    old_head = "f" * 40
    review_submitted_at = 1000.0
    ship_now = review_submitted_at + 40 * 60.0
    old_review = CopilotReview(
        review_id=45,
        commit_id=old_head,
        state="COMMENTED",
        body="Looks good on old commit.",
        author=COPILOT_REVIEWER_LOGIN,
        submitted_at_epoch=review_submitted_at,
    )
    github, orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch, reviews=(old_review,), threads=()
    )

    result = _invoke_ship(tmp_path, authority=authority, state=state, registry=registry, now=ship_now)

    assert github.request_copilot_calls == 1
    assert result.get("action") == "awaiting-copilot"
    row = _journal_row(state, run_id)
    ship_state = row["ship"]
    assert ship_state.get("phase") == "review-requested"
    assert "adopted_review_id" not in ship_state


def test_r6_e_no_existing_copilot_review_requests_copilot_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(e) 無既有 review → 呼叫 request_copilot 一次（現行）。"""
    github, orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch, reviews=(), threads=()
    )

    result = _invoke_ship(tmp_path, authority=authority, state=state, registry=registry, now=200.0)

    assert github.request_copilot_calls == 1
    assert result.get("action") == "awaiting-copilot"
    row = _journal_row(state, run_id)
    ship_state = row["ship"]
    assert ship_state.get("phase") == "review-requested"
    assert "adopted_review_id" not in ship_state


def test_r6_f_copilot_review_timeout_next_actions_includes_review_attest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(f) copilot-review-timeout / resume 的 next_actions 都維持 list[str]。"""
    github, orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch, reviews=(), threads=()
    )
    # 第一步：觸發 request，進入 review-requested 狀態
    t0 = 1000.0
    first = _invoke_ship(tmp_path, authority=authority, state=state, registry=registry, now=t0)
    assert first.get("action") == "awaiting-copilot"

    # 第二步：超過 15 分鐘無 review 回覆，觸發 copilot-review-timeout
    t_timeout = t0 + 15 * 60.0 + 1.0
    result = _invoke_ship(tmp_path, authority=authority, state=state, registry=registry, now=t_timeout)

    assert result.get("action") == "needs_human"
    assert result.get("reason") == "copilot-review-timeout"
    next_actions = result.get("next_actions", ())
    assert isinstance(next_actions, list)
    assert "review-attest" in next_actions
    assert "abandon" in next_actions
    # 既有值在前，補充在後
    assert list(next_actions).index("abandon") < list(next_actions).index("review-attest")

    # 同時驗證透過 execute_work_action resume 該 run 時，返回的 next_actions 亦含 review-attest
    resume_resp = work_actions.execute_work_action(
        args={"action": "resume", "repo": REPO, "work_id": WORK_ID},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: t_timeout + 1.0,
        workflow_registry=registry,
    )
    resume_actions = resume_resp["result"].get("next_actions", ())
    assert isinstance(resume_actions, list)
    assert "review-attest" in resume_actions


def test_adopt_existing_copilot_review_selects_latest_by_epoch_and_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1: 多筆 exact-HEAD review 取 (submitted_at_epoch, review_id) 最大者。"""
    review_early = CopilotReview(
        review_id=10,
        commit_id=HEAD,
        state="COMMENTED",
        body="Early review.",
        author=COPILOT_REVIEWER_LOGIN,
        submitted_at_epoch=1000.0,
    )
    review_late = CopilotReview(
        review_id=20,
        commit_id=HEAD,
        state="COMMENTED",
        body="Later review.",
        author=COPILOT_REVIEWER_LOGIN,
        submitted_at_epoch=1200.0,
    )
    github, orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch, reviews=(review_early, review_late), threads=()
    )

    result = _invoke_ship(tmp_path, authority=authority, state=state, registry=registry, now=3400.0)

    assert github.request_copilot_calls == 0
    row = _journal_row(state, run_id)
    ship_state = row["ship"]
    assert ship_state.get("adopted_review_id") == 20
    assert ship_state.get("requested_at_epoch") == 1200.0


def test_review_loop_adopted_at_epoch_preserves_valid_window_for_earlier_submission() -> None:
    """R2 / D2: ReviewLoop 支援 adopted_at，timeout 判定以 adopted_at 為基準。"""
    review_submitted_at = 1000.0
    adopted_at = 3400.0  # 40 分鐘後採信
    now_epoch = 3405.0   # 採信後 5 秒記錄

    loop = ReviewLoop(
        head=HEAD,
        fix_rounds=0,
        epoch_started_at=review_submitted_at,
        requested_at=review_submitted_at,
        adopted_at=adopted_at,
    )
    decision = loop.record_review(
        head=HEAD,
        now_epoch=now_epoch,
        finding_count=0,
        review_id=42,
        submitted_at_epoch=review_submitted_at,
    )
    assert decision.action == "passed"
    assert decision.reason is None


def test_copilot_error_review_needs_human_next_actions_are_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch, reviews=(), threads=()
    )
    requested_at = 1000.0
    first = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        now=requested_at,
    )
    assert first.get("action") == "awaiting-copilot"

    github.reviews = (
        CopilotReview(
            review_id=46,
            commit_id=HEAD,
            state="COMMENTED",
            body="Copilot encountered an error while reviewing changes.",
            author=COPILOT_REVIEWER_LOGIN,
            submitted_at_epoch=requested_at + 1.0,
        ),
    )

    result = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        now=requested_at + 2.0,
    )

    assert result.get("action") == "needs_human"
    assert result.get("reason") == "copilot-error-review"
    assert result.get("next_actions") == ["abandon", "review-attest"]


@pytest.mark.parametrize("adopted_at_epoch", [float("nan"), float("inf")])
def test_ship_rejects_non_finite_adopted_at_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adopted_at_epoch: float,
) -> None:
    github, orch_holder, snapshot, state, registry, run_id, authority = _setup_ship_env(
        tmp_path, monkeypatch, reviews=(), threads=()
    )
    requested_at = 1000.0
    first = _invoke_ship(
        tmp_path,
        authority=authority,
        state=state,
        registry=registry,
        now=requested_at,
    )
    assert first.get("action") == "awaiting-copilot"

    row = _journal_row(state, run_id)
    row["ship"]["adopted_at_epoch"] = adopted_at_epoch
    _write_journal_row(state, run_id, row)

    with pytest.raises(ValueError, match="ship review request state malformed"):
        _invoke_ship(
            tmp_path,
            authority=authority,
            state=state,
            registry=registry,
            now=requested_at + REVIEW_TIMEOUT_SECONDS + 1.0,
        )


@pytest.mark.parametrize(
    ("ship_state", "action", "expects_adoption"),
    [
        (
            {
                "phase": "needs-fix",
                "requested_at_epoch": 1000.0,
                "adopted_review_id": 42,
                "adopted_at_epoch": 3400.0,
            },
            {"action": "fix-required", "reason": "copilot-review-findings"},
            True,
        ),
        (
            {
                "phase": "review-requested",
                "requested_at_epoch": 1000.0,
            },
            {"action": "awaiting-copilot"},
            False,
        ),
    ],
)
def test_delivery_adapter_evidence_reflects_adopted_review_only_when_ship_state_adopts_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ship_state: dict[str, Any],
    action: dict[str, str],
    expects_adoption: bool,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", changes=())
    authority = load_work_authority(repo=REPO, work_id=WORK_ID, snapshot_path=snapshot)
    state_root = tmp_path / "state"
    registry = JobRegistry(state_path=state_root / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision=work_authority_digest(authority),
        workspace_root=str(tmp_path / "repo"),
        combo="feature-oneshot",
        current_phase="review",
        steps=_workflow_steps(),
        issue_refs=(f"{REPO}#12",),
        openspec_refs=(),
        pr_refs=(f"{REPO}#8",),
        attempts={"review": 1},
        gate_refs=(GateEvidenceRef("foreign-review", str(tmp_path / "foreign.json"), "f" * 64),),
        candidate_head=HEAD,
        verified_head=HEAD,
        gate_status="running",
    )
    work_actions._load_work_run(
        state_path=state_root / "delivery-journal.json",
        workflow_registry=registry,
        authority=authority,
    )

    monkeypatch.setattr(work_bridge, "_builder_binding", lambda *args, **kwargs: "feature/12-work")
    monkeypatch.setattr(work_bridge, "_manager_ship_workspace", lambda **kwargs: tmp_path)
    monkeypatch.setattr(work_bridge, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_bridge,
        "_run_exact_candidate_preflight",
        lambda **kwargs: PreflightResult(
            True,
            None,
            CommandResult(("policy",), 0, "", ""),
            CommandResult(("preflight",), 0, "", ""),
            HEAD,
            TREE,
        ),
    )
    monkeypatch.setattr(work_bridge, "_push_exact_candidate", lambda **kwargs: None)
    monkeypatch.setattr(work_bridge, "_workflow_evidence_payload", lambda **kwargs: ({}, {}))
    monkeypatch.setattr(
        review_evidence,
        "write_gate_evaluation",
        lambda payload, *, coordinator_root=None: {
            "path": str(tmp_path / "foreign-review.json"),
            "hash": "f" * 64,
            "payload": payload,
        },
    )

    def fake_ship_action(*, state_path: Path, **kwargs) -> dict[str, str]:
        journal = json.loads(state_path.read_text(encoding="utf-8"))
        journal["runs"][run.run_id]["ship"] = dict(ship_state)
        state_path.write_text(json.dumps(journal), encoding="utf-8")
        return dict(action)

    monkeypatch.setattr(work_actions, "_ship_action", fake_ship_action)

    validator = work_bridge.build_production_ship_validator(
        registry=registry,
        coordinator_root=state_root,
        snapshot_path=snapshot,
    )

    result = validator(run=run, candidate=HEAD)

    evidence = json.loads(Path(str(result["ref"])).read_text(encoding="utf-8"))
    payload = evidence["payload"]
    if expects_adoption:
        assert payload["adopted_review_id"] == 42
        assert payload["adopted_at_epoch"] == 3400.0
    else:
        assert "adopted_review_id" not in payload
        assert "adopted_at_epoch" not in payload


def test_delivery_adapter_adoption_fields_wrap_invalid_journal_json(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)
    (state_root / "delivery-journal.json").write_text("{\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="delivery journal payload malformed") as excinfo:
        work_bridge._delivery_adapter_adoption_fields(
            state_root=state_root,
            run_id="run-1",
        )

    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)
