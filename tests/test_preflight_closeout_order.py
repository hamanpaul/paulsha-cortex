from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager, review, work_actions, work_bridge
from paulsha_cortex.coordinator.claim import load_work_authority, work_authority_digest
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import (
    GateEvidenceRef,
    PlanningArtifactAuthority,
    WorkflowStep,
)


@dataclass
class CallOutcome:
    result: object | None
    exception: BaseException | None


@dataclass
class RunnerResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class ShipHarness:
    repo: Path
    candidate: str
    snapshot: Path
    state_root: Path
    registry: JobRegistry
    run_id: str
    validator: object
    runner: "SpyRunner"

    @property
    def run(self):
        return self.registry.get_workflow_run(self.run_id)


class SpyRunner:
    def __init__(
        self,
        *,
        repo: Path,
        metadata_preflight_returncode: int = 0,
        pr_preflight_returncode: int = 0,
    ) -> None:
        self.repo = repo
        self.calls: list[list[str]] = []
        self.remote_head: str | None = None
        self.metadata_preflight_returncode = metadata_preflight_returncode
        self.pr_preflight_returncode = pr_preflight_returncode

    def __call__(self, argv, **kwargs):
        command = [str(value) for value in argv]
        self.calls.append(command)
        if command[:3] == ["python3", "-m", "policy_check"]:
            return RunnerResult(0)
        if command[:2] == ["openspec", "validate"]:
            return RunnerResult(0)
        if command[:2] == ["openspec", "archive"]:
            self._apply_archive(command[-1])
            return RunnerResult(0)
        if command[:2] == ["git", "-C"] and "ls-remote" in command:
            if self.remote_head is None:
                return RunnerResult(2)
            ref = command[-1]
            return RunnerResult(0, stdout=f"{self.remote_head}\t{ref}\n")
        if command[:2] == ["git", "-C"] and "push" in command:
            head = subprocess.run(
                ["git", "-C", command[2], "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.remote_head = head
            return RunnerResult(0)
        if command and command[0] == "gh":
            return RunnerResult(0, stdout="17\n")
        if command and command[0] == "preflight":
            if "--metadata" in command:
                rc = self.metadata_preflight_returncode
                return RunnerResult(
                    rc,
                    stdout="metadata ok" if rc == 0 else "metadata blocked",
                    stderr="" if rc == 0 else "metadata blocked",
                )
            if "--pr" in command:
                rc = self.pr_preflight_returncode
                return RunnerResult(
                    rc,
                    stdout="pr ok" if rc == 0 else "pr blocked",
                    stderr="" if rc == 0 else "pr blocked",
                )
        return subprocess.run(command, **kwargs)

    def _apply_archive(self, change: str) -> None:
        active = self.repo / "openspec" / "changes" / change
        archived = self.repo / "openspec" / "changes" / "archive" / change
        if active.exists():
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(active), str(archived))

    def saw(self, *parts: str) -> bool:
        return any(command[: len(parts)] == list(parts) for command in self.calls)

    def saw_push(self) -> bool:
        return any("push" in command for command in self.calls)

    def saw_gh(self) -> bool:
        return any(command and command[0] == "gh" for command in self.calls)

    def first_index(self, predicate) -> int | None:
        for index, command in enumerate(self.calls):
            if predicate(command):
                return index
        return None


class FakeGitHubDeliveryClient:
    def __init__(self, *, runner) -> None:
        self._runner = runner

    def create_or_get_pull_request(self, **kwargs) -> int:
        self._runner(
            ["gh", "pr", "create", "--head", str(kwargs["branch"])],
            shell=False,
            capture_output=True,
            text=True,
        )
        return 17

    def ensure_pr_metadata(self, **kwargs) -> None:
        self._runner(
            ["gh", "pr", "edit", str(kwargs["pr_number"])],
            shell=False,
            capture_output=True,
            text=True,
        )

    def fetch_default_branch(self, **kwargs) -> str:
        return "main"

    def fetch_remote_closure(self, **kwargs):
        return SimpleNamespace(default_head="d" * 40, merge_commit="e" * 40)

    def fetch_delivery_facts(self, **kwargs):
        return SimpleNamespace(
            head="a" * 40,
            active_openspec_absent=True,
            archive_present=True,
        )

    def fetch_merge_status(self, **kwargs):
        return SimpleNamespace(merged=False, pr_head=None, merge_commit=None)


def _capture(callable_obj, /, *args, **kwargs) -> CallOutcome:
    try:
        return CallOutcome(callable_obj(*args, **kwargs), None)
    except BaseException as exc:  # pragma: no cover - exercised by RED assertions
        return CallOutcome(None, exc)


def _repo(root: Path, *, active_change: bool, archived_change: bool) -> tuple[Path, str]:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", "git@github.com:acme/demo.git"],
        check=True,
    )
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("## [Unreleased]\n\n- work\n", encoding="utf-8")
    changelog = root / "changelog.d" / "work.md"
    changelog.parent.mkdir(parents=True, exist_ok=True)
    changelog.write_text("work\n", encoding="utf-8")
    todo = root / "docs" / "todo.md"
    todo.parent.mkdir(parents=True, exist_ok=True)
    todo.write_text("- [x] ready\n", encoding="utf-8")
    change_root = root / "openspec" / "changes"
    if active_change:
        target = change_root / "work"
    elif archived_change:
        target = change_root / "archive" / "work"
    else:
        target = None
    if target is not None:
        target.mkdir(parents=True, exist_ok=True)
        (target / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
        (target / "design.md").write_text("# Design\n", encoding="utf-8")
        (target / "tasks.md").write_text("- [x] ready\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, head


def _snapshot(path: Path, *, mapped_prs: tuple[int, ...] = ()) -> Path:
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
                        "source_revisions": [
                            "github_issue:acme/demo#14@issue-open",
                            "openspec:acme/demo:work@spec-1",
                            "todo:acme/demo:docs/todo.md@todo-1",
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _steps(*, review_passed: bool) -> tuple[WorkflowStep, ...]:
    phases = ("claim", "define", "plan", "build", "verify", "review", "ship")
    personas = {
        "claim": "manager",
        "define": "planner",
        "plan": "planner",
        "build": "builder",
        "verify": "reviewer",
        "review": "reviewer",
        "ship": "manager",
    }
    steps = []
    for phase in phases:
        gate = "passed" if phase in {"claim", "define", "plan", "build", "verify"} else "pending"
        if phase == "review" and review_passed:
            gate = "passed"
        steps.append(
            WorkflowStep(
                phase=phase,
                persona=personas[phase],
                card=f"{phase}-card",
                executor=None,
                model=None,
                domain=None,
                inputs=(),
                outputs=(),
                gate_result=gate,
            )
        )
    return tuple(steps)


def _seed_foreign_review(*, registry: JobRegistry, run, repo: Path, candidate: str, state_root: Path) -> None:
    builder = registry.create_job(
        task="wf-build",
        persona="builder",
        kind="build",
        branch="feature/14-work",
        pane="",
        worktree=str(repo),
        executor="codex",
        model_id="gpt",
        independence_domain="openai",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="build-card",
        workflow_phase="build",
        workflow_repo_root=str(repo),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(builder["job_id"], status="exited", exit_code=0)
    report_ref = "reports/review/work-review.md"
    review_job = registry.create_job(
        task="wf-review",
        persona="reviewer",
        kind="review",
        branch="feature/14-work",
        pane="",
        worktree=str(repo),
        executor="claude",
        model_id="sonnet",
        independence_domain="anthropic",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="review-card",
        workflow_phase="review",
        workflow_repo_root=str(repo),
        workflow_outputs=(report_ref,),
        workflow_output_baseline=(),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)
    evaluation = review.build_gate_evaluation(
        slice_id=f"{run.run_id}-review-card",
        state="passed",
        reason="accepted",
        builder_job_id=builder["job_id"],
        reviewer_job_id=review_job["job_id"],
        candidate=candidate,
        launch_identity={
            "builder": {
                "executor": "codex",
                "model_id": "gpt",
                "independence_domain": "openai",
            },
            "reviewer": {
                "executor": "claude",
                "model_id": "sonnet",
                "independence_domain": "anthropic",
            },
        },
    )
    report = repo / report_ref
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Canonical review\n", encoding="utf-8")
    report_hash = hashlib.sha256(report.read_bytes()).hexdigest()
    evaluation["outputs"] = [{"path": report_ref, "sha256": report_hash}]
    envelope = {
        "schema_version": 1,
        "kind": "review",
        "job": {
            "job_id": review_job["job_id"],
            "run_id": run.run_id,
            "claim_key": run.claim_key,
            "repo": run.repo,
            "source_revision": run.source_revision,
            "card_id": "review-card",
            "phase": "review",
            "inputs": [],
            "outputs": [report_ref],
            "output_baseline": [],
        },
        "payload": evaluation,
        "artifacts": [
            {"path": report_ref, "sha256": report_hash, "baseline_sha256": None}
        ],
    }
    content = (
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    foreign = state_root / "evidence" / "workflow" / "foreign.json"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_bytes(content)
    foreign_hash = hashlib.sha256(content).hexdigest()
    registry.bind_workflow_evidence(
        review_job["job_id"],
        locator={
            "kind": "review",
            "path": "evidence/workflow/foreign.json",
            "hash": foreign_hash,
        },
        subject_head=candidate,
    )
    registry._manager_update_workflow_run(
        run.run_id,
        gate_refs=(GateEvidenceRef("foreign-review", str(foreign), foreign_hash),),
    )


def _ship_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    active_change: bool,
    archived_change: bool,
    metadata_preflight_returncode: int = 0,
) -> ShipHarness:
    repo, candidate = _repo(
        tmp_path / "repo",
        active_change=active_change,
        archived_change=archived_change,
    )
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = load_work_authority(repo="acme/demo", work_id="work", snapshot_path=snapshot)
    state_root = tmp_path / "state"
    registry = JobRegistry(state_path=state_root / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id="work",
        repo="acme/demo",
        claim_key="claim:v1:" + "1" * 64,
        source_revision=work_authority_digest(authority),
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="review",
        steps=_steps(review_passed=True),
        issue_refs=("acme/demo#14",),
        openspec_refs=("work",),
        pr_refs=(),
        attempts={"review": 1},
        candidate_head=candidate,
        verified_head=candidate,
        gate_status="running",
    )
    _seed_foreign_review(
        registry=registry,
        run=run,
        repo=repo,
        candidate=candidate,
        state_root=state_root,
    )
    runner = SpyRunner(
        repo=repo,
        metadata_preflight_returncode=metadata_preflight_returncode,
    )
    monkeypatch.setattr(work_bridge, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(work_bridge, "GitHubDeliveryClient", FakeGitHubDeliveryClient)
    validator = work_bridge.build_production_ship_validator(
        registry=registry,
        coordinator_root=state_root,
        snapshot_path=snapshot,
        runner=runner,
    )
    return ShipHarness(
        repo=repo,
        candidate=candidate,
        snapshot=snapshot,
        state_root=state_root,
        registry=registry,
        run_id=run.run_id,
        validator=validator,
        runner=runner,
    )


def _review_authority_fixture(tmp_path: Path) -> tuple[Path, str, object, dict[str, str], tuple[dict[str, str], ...]]:
    repo, candidate = _repo(tmp_path / "review-repo", active_change=False, archived_change=False)
    plan_ref = "docs/superpowers/plans/review-plan.md"
    plan = repo / plan_ref
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan_bytes = b"# Accepted plan\n\nFrozen.\n"
    plan.write_bytes(plan_bytes)
    subprocess.run(["git", "-C", str(repo), "add", plan_ref], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add plan"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    digest = hashlib.sha256(plan_bytes).hexdigest()
    registry = JobRegistry(state_path=tmp_path / "review-registry.json")
    run = registry._manager_create_workflow_run(
        work_id="review-work",
        repo="acme/demo",
        claim_key="claim:v1:" + "2" * 64,
        source_revision="3" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="review",
        steps=_steps(review_passed=False),
        candidate_head=candidate,
        verified_head=candidate,
        gate_status="running",
        planning_authority=(
            PlanningArtifactAuthority(
                ref=plan_ref,
                kind="plan",
                work_id="review-work",
                baseline_sha256=digest,
            ),
        ),
    )
    input_snapshot = manager._workflow_input_snapshot(
        run=run,
        repo_root=repo,
        patterns=(plan_ref,),
        coordinator_root=tmp_path / "coordinator",
    )
    authority = {plan_ref: digest}
    return repo, candidate, run, authority, input_snapshot


def test_slice_review_authority_inputs_resolves_relative_plan_path_against_repo_root(
    tmp_path: Path,
) -> None:
    """``_pinned_input_mismatches`` explicitly supports a legacy/recovered slice
    row whose ``plan.path`` is repo-relative, by resolving it against the
    inferred ``repo_root``. ``_slice_review_authority_inputs`` operates on the
    same slice rows to materialize foreign-review authority and must resolve
    relative ``spec``/``plan`` paths the same way -- resolving against the
    process cwd instead would read the wrong file (or raise) whenever pytest
    (or the coordinator daemon) is not started from inside the target repo.
    """
    repo, candidate = _repo(
        tmp_path / "authority-relative-repo", active_change=False, archived_change=False
    )
    plan_ref = "docs/superpowers/plans/relative-plan.md"
    plan = repo / plan_ref
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan_bytes = b"# Relative plan\n\nFrozen.\n"
    plan.write_bytes(plan_bytes)
    spec_ref = "specs/relative-spec.md"
    spec = repo / spec_ref
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec_bytes = b"# Relative spec\n"
    spec.write_bytes(spec_bytes)
    subprocess.run(["git", "-C", str(repo), "add", plan_ref, spec_ref], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add relative inputs"], check=True)

    slice_row = {
        "slice_id": "slice-relative",
        "spec": {"path": spec_ref, "hash": hashlib.sha256(spec_bytes).hexdigest()},
        "plan": {"path": plan_ref, "hash": hashlib.sha256(plan_bytes).hexdigest()},
    }

    authority, rows = manager._slice_review_authority_inputs(
        slice_row=slice_row,
        repo_root=repo,
        coordinator_root=tmp_path / "coordinator-relative",
        candidate=candidate,
    )

    assert authority == {
        plan_ref: hashlib.sha256(plan_bytes).hexdigest(),
        spec_ref: hashlib.sha256(spec_bytes).hexdigest(),
    }
    assert {row["path"] for row in rows} == {plan_ref, spec_ref}


def test_ship_validate_completes_local_archive_closeout_without_pr_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=True,
        archived_change=False,
    )

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)
    updated = harness.registry.get_workflow_run(harness.run_id)

    assert outcome.exception is None
    assert updated.current_phase == "verify"
    assert updated.candidate_head is not None and updated.candidate_head != harness.candidate
    assert updated.pr_refs == ()
    assert not (harness.repo / "openspec" / "changes" / "work").exists()
    assert not harness.runner.saw_push()
    assert not harness.runner.saw_gh()


def test_pr_metadata_preflight_failure_keeps_local_closeout_and_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        metadata_preflight_returncode=1,
    )
    dispatcher = SimpleNamespace(_registry=harness.registry, _git_runner=None)

    first = _capture(
        manager.resume_workflow_run,
        dispatcher,
        run_id=harness.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=harness.state_root,
        ship_validator=harness.validator,
        operator_resume=True,
    )
    stopped = harness.registry.get_workflow_run(harness.run_id)
    second = _capture(
        manager.resume_workflow_run,
        dispatcher,
        run_id=harness.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=harness.state_root,
        ship_validator=harness.validator,
        operator_resume=True,
    )

    assert first.exception is None
    assert isinstance(first.result, dict)
    assert "pr-preflight-blocked" in str(first.result.get("reason"))
    assert "needs_human" in stopped.facets
    assert stopped.gate_status != "failed"
    assert second.exception is None
    assert sum("--metadata" in call for call in harness.runner.calls) >= 2


def test_stage_order_closeout_then_preflight_then_ship(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=True,
        archived_change=False,
    )

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)
    archive_index = harness.runner.first_index(lambda call: call[:2] == ["openspec", "archive"])
    preflight_index = harness.runner.first_index(lambda call: call and call[0] == "preflight")
    mutation_index = harness.runner.first_index(
        lambda call: (call and call[0] == "gh") or "push" in call
    )

    assert outcome.exception is None
    assert archive_index is not None
    if preflight_index is not None:
        assert archive_index < preflight_index
    if preflight_index is not None and mutation_index is not None:
        assert preflight_index < mutation_index


def test_pr_created_automatically_after_closeout_and_preflight_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once local closeout has already completed (active change archived)
    and metadata preflight passes, ship validate MUST proceed to push and
    create the PR in the same call -- spec R3's 驗收面: "本地 archive 完成
    後建立 PR，仍銜接 current-head CI、foreign review、final attestation與
    ship". Plan task 3 explicitly keeps `_pr_metadata`/preflight/push/PR
    creation "保持在 local closeout 段之後執行" (kept, just reordered), not
    removed behind a manual "awaiting-pr-authorization" stop.
    """
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
    )

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)

    assert outcome.exception is None
    assert isinstance(outcome.result, dict)
    assert outcome.result["status"] == "pending"
    assert harness.runner.saw_push()
    assert harness.runner.saw_gh()
    updated = harness.registry.get_workflow_run(harness.run_id)
    assert updated.pr_refs == ("acme/demo#17",)
    assert (harness.state_root / "delivery-journal.json").exists()


def test_archive_commit_does_not_push(tmp_path: Path) -> None:
    repo, _initial = _repo(tmp_path / "archive-repo", active_change=True, archived_change=False)
    active = repo / "openspec" / "changes" / "work"
    archived = repo / "openspec" / "changes" / "archive" / "work"
    archived.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(active), str(archived))
    candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    snapshot = _snapshot(tmp_path / "archive-snapshot.json")
    authority = load_work_authority(repo="acme/demo", work_id="work", snapshot_path=snapshot)
    state_root = tmp_path / "archive-state"
    registry = JobRegistry(state_path=state_root / "jobs.json")
    ship_steps = (
        WorkflowStep("ship", "manager", "openspec-archive", None, None, None, (), ()),
        WorkflowStep("ship", "manager", "policy-commit", None, None, None, (), ()),
    )
    run = registry._manager_create_workflow_run(
        work_id="work",
        repo="acme/demo",
        claim_key="claim:v1:" + "4" * 64,
        source_revision=work_authority_digest(authority),
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="review",
        steps=tuple(step for step in _steps(review_passed=True) if step.phase != "ship")
        + ship_steps,
        issue_refs=("acme/demo#14",),
        openspec_refs=("work",),
        pr_refs=(),
        attempts={"verify": 1, "review": 1},
        gate_refs=(GateEvidenceRef("foreign-review", "review.json", "1" * 64),),
        candidate_head=candidate,
        verified_head=candidate,
        gate_status="running",
    )
    runner = SpyRunner(repo=repo)

    reset = work_bridge._commit_archive_and_require_reverification(
        registry=registry,
        state_root=state_root,
        run=run,
        authority=authority,
        worktree=repo,
        branch="feature/14-work",
        candidate=candidate,
        runner=runner,
    )

    assert reset.current_phase == "verify"
    assert reset.candidate_head != candidate
    assert not runner.saw_push()


def test_reviewer_dispatch_fail_closed_on_frozen_hash_drift(tmp_path: Path) -> None:
    repo, _candidate, _run, authority, input_snapshot = _review_authority_fixture(tmp_path)
    plan = repo / next(iter(authority))
    plan.write_text("# Drifted plan\n", encoding="utf-8")

    outcome = _capture(
        review.verify_authority_in_input_snapshot,
        authority=authority,
        input_snapshot=input_snapshot,
        workspace_root=repo,
    )

    assert isinstance(outcome.exception, ValueError)
    assert "authority hash drift" in str(outcome.exception)


def test_review_worktree_materializes_frozen_authority_with_attestation(
    tmp_path: Path,
) -> None:
    repo, candidate, _run, authority, input_snapshot = _review_authority_fixture(tmp_path)

    prepared = _capture(
        review.prepare_review_worktree,
        repo_root=repo,
        slice_id="slice-a",
        reviewer_job_id="slice-a-2",
        candidate=candidate,
        authority=authority,
        input_snapshot=input_snapshot,
        source_revision="3" * 64,
        subprocess_runner=None,
        git_runner=None,
    )

    assert prepared.exception is None
    worktree = prepared.result
    assert isinstance(worktree, Path)
    for ref, digest in authority.items():
        assert hashlib.sha256((worktree / ref).read_bytes()).hexdigest() == digest
    verify = _capture(
        review.verify_authority_in_input_snapshot,
        authority=authority,
        input_snapshot=input_snapshot,
        workspace_root=worktree,
    )
    assert verify.exception is None
    missing = _capture(
        review.prepare_review_worktree,
        repo_root=repo,
        slice_id="slice-b",
        reviewer_job_id="slice-b-2",
        candidate=candidate,
        input_snapshot=input_snapshot,
        subprocess_runner=None,
        git_runner=None,
    )
    assert isinstance(missing.exception, ValueError)
    assert "authority" in str(missing.exception)


def _malicious_authority_row(
    *,
    coordinator_root: Path,
    malicious_path: str,
    content: str = "malicious content\n",
    run_id: str = "review-work",
    work_id: str = "review-work",
    repo: str = "acme/demo",
    source_revision: str = "3" * 64,
) -> tuple[dict[str, str], str]:
    """Craft a workflow-input content blob whose *envelope* path is malicious.

    Both real writers of these blobs (``_slice_review_authority_inputs`` and
    ``_workflow_input_snapshot``) derive ``ref`` via ``Path.relative_to(root)``,
    so a ``..``/absolute ``path`` can never occur through them. This directly
    exercises ``manager._write_workflow_input_content`` instead, standing in
    for a corrupted/forged content-ref blob, so that
    ``prepare_review_worktree`` remains the last line of defense regardless
    of how the input snapshot was produced.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    identity = SimpleNamespace(
        run_id=run_id,
        work_id=work_id,
        repo=repo,
        source_revision=source_revision,
    )
    content_ref = manager._write_workflow_input_content(
        coordinator_root=coordinator_root,
        run=identity,
        ref=malicious_path,
        digest=digest,
        content=content,
    )
    row = {
        "pattern": malicious_path,
        "path": malicious_path,
        "sha256": digest,
        "authority": "planning-authority",
        "content_ref": content_ref,
    }
    return row, digest


def _tree_snapshot(base: Path) -> set[str]:
    entries: set[str] = set()
    if not base.exists():
        return entries
    for dirpath, dirnames, filenames in os.walk(base):
        for name in dirnames:
            entries.add(str(Path(dirpath) / name))
        for name in filenames:
            entries.add(str(Path(dirpath) / name))
    return entries


@pytest.mark.parametrize(
    "malicious_path",
    [
        "../escape-dir/evil.txt",
        "../../../escape-dir/evil.txt",
        "safe/../../escape-dir/evil.txt",
    ],
)
def test_review_worktree_materialize_rejects_dotdot_ref_before_mkdir(
    tmp_path: Path, malicious_path: str
) -> None:
    repo, candidate = _repo(
        tmp_path / "review-repo-dotdot", active_change=False, archived_change=False
    )
    coordinator_root = tmp_path / "coordinator-dotdot"
    row, digest = _malicious_authority_row(
        coordinator_root=coordinator_root, malicious_path=malicious_path
    )
    authority = {malicious_path: digest}

    before = _tree_snapshot(tmp_path)

    outcome = _capture(
        review.prepare_review_worktree,
        repo_root=repo,
        slice_id="slice-evil",
        reviewer_job_id="evil-1",
        candidate=candidate,
        authority=authority,
        input_snapshot=(row,),
        source_revision=candidate,
        subprocess_runner=None,
        git_runner=None,
    )

    assert isinstance(outcome.exception, ValueError)
    assert "ref path invalid" in str(outcome.exception)

    after = _tree_snapshot(tmp_path)
    # The only permissible new filesystem entries are git's own worktree
    # bookkeeping (repo/.git/worktrees/...) and the sandboxed review worktree
    # directory itself, created *before* authority materialization begins.
    # Nothing named after the escape target may appear anywhere.
    for entry in after - before:
        assert "escape-dir" not in entry
        assert "evil.txt" not in entry


@pytest.mark.parametrize("escape_name", ["outside-repo-abs", "outside-repo-abs-2"])
def test_review_worktree_materialize_rejects_absolute_ref_before_mkdir(
    tmp_path: Path, escape_name: str
) -> None:
    repo, candidate = _repo(
        tmp_path / "review-repo-abs", active_change=False, archived_change=False
    )
    coordinator_root = tmp_path / "coordinator-abs"
    escape_target = tmp_path / escape_name / "evil.txt"
    malicious_path = str(escape_target)
    row, digest = _malicious_authority_row(
        coordinator_root=coordinator_root, malicious_path=malicious_path
    )
    authority = {malicious_path: digest}

    outcome = _capture(
        review.prepare_review_worktree,
        repo_root=repo,
        slice_id="slice-evil-abs",
        reviewer_job_id="evil-2",
        candidate=candidate,
        authority=authority,
        input_snapshot=(row,),
        source_revision=candidate,
        subprocess_runner=None,
        git_runner=None,
    )

    assert isinstance(outcome.exception, ValueError)
    assert "ref path invalid" in str(outcome.exception)
    assert not escape_target.exists()
    assert not escape_target.parent.exists()


def test_unexpected_exception_still_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=True,
        archived_change=False,
    )
    dispatcher = SimpleNamespace(_registry=harness.registry, _git_runner=None)
    monkeypatch.setattr(
        work_bridge,
        "_commit_archive_and_require_reverification",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    outcome = _capture(
        manager.resume_workflow_run,
        dispatcher,
        run_id=harness.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=harness.state_root,
        ship_validator=harness.validator,
        operator_resume=True,
    )
    stopped = harness.registry.get_workflow_run(harness.run_id)

    assert isinstance(outcome.exception, RuntimeError)
    assert "boom" in str(outcome.exception)
    assert stopped.facets == ("needs_human",)
    assert stopped.gate_status == "failed"
