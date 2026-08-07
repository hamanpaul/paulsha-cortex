"""#260：repair commit 缺 terminal evidence 時的窄化 adoption 出口，
以及 resume/retry-build 不再重選 stale failed job。

驗收條件對應：
- R1/R2/R3：`recover-repair-commit` 雙 CAS、判準取自系統事實、fail-closed、冪等
- R4：`retry-build` 既有 CAS 與窄化入口不放寬（regression lock）
- R6：`resume_workflow_run` 第一次即 dispatch replacement，不重選 stale failed job；
  重送不產生第二個 replacement
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, work_actions
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

ACTION = "recover-repair-commit"

_PERSONA_BY_PHASE = {
    "claim": "manager",
    "define": "planner",
    "plan": "planner",
    "build": "builder",
    "verify": "reviewer",
    "review": "reviewer",
    "ship": "manager",
}


def _step(phase: str, card: str, *, gate_result: str = "pending") -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona=_PERSONA_BY_PHASE[phase],
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        gate_result=gate_result,
    )


def _repair_steps() -> tuple[WorkflowStep, ...]:
    return (
        _step("claim", "manager-claim", gate_result="passed"),
        _step("define", "planner-define", gate_result="passed"),
        _step("plan", "planner-plan", gate_result="passed"),
        _step("build", "subagent-build", gate_result="pending"),
        _step("verify", "reviewer-verify", gate_result="pending"),
        _step("review", "reviewer-review", gate_result="pending"),
        _step("ship", "manager-ship", gate_result="pending"),
    )


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


def _snapshot(path: Path, *, issues=(12,)) -> Path:
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
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "mapped_issues": list(issues),
                        "mapped_prs": [8],
                        "mapped_openspec": ["demo"],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": ["issue:12@open", "openspec:demo@1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _authority(tmp_path: Path):
    snapshot = _snapshot(tmp_path / "snapshot.json")
    return work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    ), snapshot


def _make_run(
    registry: JobRegistry,
    *,
    authority,
    claim_key: str,
    current_phase: str,
    steps: tuple[WorkflowStep, ...],
    candidate_head: str | None = None,
    facets: tuple[str, ...] = (),
):
    return registry._manager_create_workflow_run(
        work_id=authority.work_id,
        repo=authority.repo,
        claim_key=claim_key,
        source_revision=work_actions.work_authority_digest(authority),
        workspace_root="/tmp/workspace",
        combo="feature-oneshot",
        current_phase=current_phase,
        steps=steps,
        issue_refs=tuple(f"{authority.repo}#{n}" for n in authority.mapped_issues),
        openspec_refs=authority.mapped_openspec,
        candidate_head=candidate_head,
        facets=facets,
        gate_status="running",
    )


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )


def _init_repair_worktree(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "a.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "base"], path)
    return _git(["rev-parse", "HEAD"], path).stdout.strip().lower()


def _commit_repair(path: Path, *, message: str = "repair") -> str:
    (path / "fix.txt").write_text(message + "\n", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", message], path)
    return _git(["rev-parse", "HEAD"], path).stdout.strip().lower()


def _seed_failed_builder_job(
    registry: JobRegistry,
    *,
    run,
    worktree: Path,
    base_head: str,
    status: str = "exited",
    exit_code: int = 1,
) -> dict:
    job = registry.create_job(
        task=f"seed-{run.run_id}",
        persona="builder",
        branch=f"feature/{run.work_id}",
        pane="",
        worktree=str(worktree),
        dispatch_head=base_head,
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        workflow_repo_root=str(worktree),
        workflow_input_root=str(worktree),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(job["job_id"], status=status, exit_code=exit_code)
    return registry.get_job(job["job_id"])


def _run_action(
    *,
    snapshot: Path,
    state: Path,
    registry: JobRegistry,
    expected_run_id: str,
    expected_candidate: str,
    issue: int | None = 12,
) -> dict:
    args: dict[str, object] = {
        "action": ACTION,
        "repo": "acme/demo",
        "work_id": "demo",
        "actor": "operator",
        "expected_run_id": expected_run_id,
        "expected_candidate": expected_candidate,
    }
    if issue is not None:
        args["issue"] = issue
    return work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )


# ---------------------------------------------------------------------------
# recover-repair-commit
# ---------------------------------------------------------------------------


def test_recover_repair_commit_adopts_descendant_head_without_terminal_evidence(
    tmp_path: Path,
) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    worktree = tmp_path / "wt"
    base_head = _init_repair_worktree(worktree)
    repaired_head = _commit_repair(worktree)

    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="build",
        steps=_repair_steps(),
        candidate_head=base_head,
        facets=("needs_human",),
    )
    failed_job = _seed_failed_builder_job(
        registry, run=run, worktree=worktree, base_head=base_head
    )

    result = _run_action(
        snapshot=snapshot,
        state=tmp_path / "runs.json",
        registry=registry,
        expected_run_id=run.run_id,
        expected_candidate=repaired_head,
    )

    payload = result["result"]
    assert payload["reason"] == "repair-commit-adopted"
    updated_run = payload["run"]
    assert updated_run["candidate_head"] == repaired_head
    assert updated_run["current_phase"] == "verify"
    assert "needs_human" not in updated_run["facets"]
    by_phase = {step["phase"]: step for step in updated_run["steps"]}
    assert by_phase["build"]["gate_result"] == "passed"
    assert by_phase["verify"]["gate_result"] == "pending"

    evidence_ref = Path(payload["evidence"]["ref"])
    assert evidence_ref.is_file()
    record = json.loads(evidence_ref.read_text(encoding="utf-8"))
    assert record["schema"] == "cortex-work-repair-adoption/v1"
    assert record["run_id"] == run.run_id
    assert record["adopted_candidate"] == repaired_head
    assert record["previous_candidate"] == base_head
    assert record["failed_job_id"] == failed_job["job_id"]

    adoption_job = registry.get_job(payload["adoption_job_id"])
    assert adoption_job["status"] == "exited"
    assert adoption_job["exit_code"] == 0
    assert adoption_job["subject_head"] == repaired_head
    assert adoption_job["workflow_evidence"]["path"] == str(evidence_ref)
    assert adoption_job["executor"] == failed_job["executor"]
    assert adoption_job["model_id"] == failed_job["model_id"]
    assert adoption_job["independence_domain"] == failed_job["independence_domain"]

    # failed job row 原樣保留，不得被改寫。
    unchanged_failed = registry.get_job(failed_job["job_id"])
    assert unchanged_failed == failed_job

    # 沒有任何新啟動的 model session（無 dispatched/running job）。
    assert not any(
        job["status"] in {"dispatched", "running"} for job in registry.list_jobs()
    )
    assert len(registry.list_jobs()) == 2


def test_recover_repair_commit_rejects_non_descendant_head(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    worktree = tmp_path / "wt"
    base_head = _init_repair_worktree(worktree)
    # 產生與 base_head 無共同祖先的 unrelated history。
    _git(["checkout", "-q", "--orphan", "unrelated"], worktree)
    (worktree / "other.txt").write_text("other\n", encoding="utf-8")
    _git(["add", "."], worktree)
    _git(["commit", "-q", "-m", "unrelated"], worktree)
    unrelated_head = _git(["rev-parse", "HEAD"], worktree).stdout.strip().lower()

    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="build",
        steps=_repair_steps(),
        candidate_head=base_head,
        facets=("needs_human",),
    )
    _seed_failed_builder_job(registry, run=run, worktree=worktree, base_head=base_head)

    with pytest.raises(RuntimeError, match="descendant"):
        _run_action(
            snapshot=snapshot,
            state=tmp_path / "runs.json",
            registry=registry,
            expected_run_id=run.run_id,
            expected_candidate=unrelated_head,
        )

    unchanged = registry.get_workflow_run(run.run_id)
    assert unchanged.candidate_head == base_head
    assert unchanged.current_phase == "build"
    assert "needs_human" in unchanged.facets
    assert len(registry.list_jobs()) == 1


def test_recover_repair_commit_rejects_dirty_worktree(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    worktree = tmp_path / "wt"
    base_head = _init_repair_worktree(worktree)
    repaired_head = _commit_repair(worktree)
    # worktree 有未 commit 的變更。
    (worktree / "fix.txt").write_text("dirty\n", encoding="utf-8")

    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="build",
        steps=_repair_steps(),
        candidate_head=base_head,
        facets=("needs_human",),
    )
    _seed_failed_builder_job(registry, run=run, worktree=worktree, base_head=base_head)

    with pytest.raises(RuntimeError, match="clean worktree"):
        _run_action(
            snapshot=snapshot,
            state=tmp_path / "runs.json",
            registry=registry,
            expected_run_id=run.run_id,
            expected_candidate=repaired_head,
        )

    unchanged = registry.get_workflow_run(run.run_id)
    assert unchanged.candidate_head == base_head
    assert unchanged.current_phase == "build"
    assert len(registry.list_jobs()) == 1


def test_recover_repair_commit_fail_closed_when_terminal_evidence_bound(
    tmp_path: Path,
) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    worktree = tmp_path / "wt"
    base_head = _init_repair_worktree(worktree)
    repaired_head = _commit_repair(worktree)

    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="build",
        steps=_repair_steps(),
        candidate_head=base_head,
        facets=("needs_human",),
    )
    bound_job = _seed_failed_builder_job(
        registry,
        run=run,
        worktree=worktree,
        base_head=base_head,
        status="exited",
        exit_code=0,
    )
    registry.bind_workflow_evidence(
        bound_job["job_id"],
        locator={"kind": "workflow-card", "path": str(tmp_path / "ev.json"), "hash": "a" * 64},
        subject_head=repaired_head,
    )

    with pytest.raises(RuntimeError, match="bound workflow evidence"):
        _run_action(
            snapshot=snapshot,
            state=tmp_path / "runs.json",
            registry=registry,
            expected_run_id=run.run_id,
            expected_candidate=repaired_head,
        )

    unchanged = registry.get_workflow_run(run.run_id)
    assert unchanged.candidate_head == base_head
    assert unchanged.current_phase == "build"
    assert len(registry.list_jobs()) == 1


def test_recover_repair_commit_replay_already_recovered_no_new_job(
    tmp_path: Path,
) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    worktree = tmp_path / "wt"
    base_head = _init_repair_worktree(worktree)
    repaired_head = _commit_repair(worktree)

    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="build",
        steps=_repair_steps(),
        candidate_head=base_head,
        facets=("needs_human",),
    )
    _seed_failed_builder_job(registry, run=run, worktree=worktree, base_head=base_head)

    first = _run_action(
        snapshot=snapshot,
        state=tmp_path / "runs.json",
        registry=registry,
        expected_run_id=run.run_id,
        expected_candidate=repaired_head,
    )
    assert first["result"]["reason"] == "repair-commit-adopted"
    jobs_after_first = registry.list_jobs()
    evidence_dir = tmp_path / "evidence" / "work-repair-adoption"
    records_after_first = list(evidence_dir.glob("*.json"))
    assert len(records_after_first) == 1

    second = _run_action(
        snapshot=snapshot,
        state=tmp_path / "runs.json",
        registry=registry,
        expected_run_id=run.run_id,
        expected_candidate=repaired_head,
    )
    assert second["result"]["reason"] == "already-recovered"
    assert registry.list_jobs() == jobs_after_first
    assert list(evidence_dir.glob("*.json")) == records_after_first


def test_recover_repair_commit_rejects_unauthorized_issue(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    worktree = tmp_path / "wt"
    base_head = _init_repair_worktree(worktree)
    repaired_head = _commit_repair(worktree)

    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="build",
        steps=_repair_steps(),
        candidate_head=base_head,
        facets=("needs_human",),
    )
    _seed_failed_builder_job(registry, run=run, worktree=worktree, base_head=base_head)

    with pytest.raises(RuntimeError, match="not authorized"):
        _run_action(
            snapshot=snapshot,
            state=tmp_path / "runs.json",
            registry=registry,
            expected_run_id=run.run_id,
            expected_candidate=repaired_head,
            issue=999,
        )

    unchanged = registry.get_workflow_run(run.run_id)
    assert unchanged.candidate_head == base_head
    assert len(registry.list_jobs()) == 1


# ---------------------------------------------------------------------------
# retry-build CAS 不放寬（regression lock）
# ---------------------------------------------------------------------------


def test_retry_build_expected_candidate_cas_unchanged(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    head = "b" * 40
    _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="build",
        steps=_repair_steps(),
        candidate_head=head,
        facets=("needs_human",),
    )

    with pytest.raises(ValueError, match="retry-build requires exact expected_candidate"):
        work_actions.execute_work_action(
            args={
                "action": "retry-build",
                "repo": "acme/demo",
                "work_id": "demo",
                "issue": 12,
                "actor": "operator",
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
        )

    with pytest.raises(RuntimeError, match="retry-build expected Candidate CAS mismatch"):
        work_actions.execute_work_action(
            args={
                "action": "retry-build",
                "repo": "acme/demo",
                "work_id": "demo",
                "issue": 12,
                "actor": "operator",
                "expected_candidate": "c" * 40,
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
        )


# ---------------------------------------------------------------------------
# resume 不重選 stale failed job
# ---------------------------------------------------------------------------


class _FakeWorktreeCreator:
    def __init__(self, path: Path) -> None:
        self._path = path

    def create(self, branch: str, base_sha: str | None = None) -> Path:
        return self._path


class _FakeLauncher:
    def as_commit_required(self):
        return self

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        from paulsha_cortex.coordinator.launcher import LaunchHandle

        return LaunchHandle(
            executor="codex",
            model_id="gpt-primary",
            session_name=slice_id,
            pid=4242,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


class _ResumeDispatcher:
    def __init__(self, registry: JobRegistry, worktree: Path) -> None:
        self._registry = registry
        self._git_runner = None
        self._worktree_creator = _FakeWorktreeCreator(worktree)

    def poll_headless_done(self, job_id: str) -> dict:
        return self._registry.get_job(job_id)


def _resume_fixture(tmp_path: Path):
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    worktree = tmp_path / "wt"
    _init_repair_worktree(worktree)

    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="build",
        steps=_repair_steps(),
        candidate_head=None,
        facets=("needs_human",),
    )
    failed_job = _seed_failed_builder_job(
        registry,
        run=run,
        worktree=worktree,
        base_head="0" * 40,
        status="exited",
        exit_code=17,
    )
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            }
        ]
    )
    dispatcher = _ResumeDispatcher(registry, worktree)
    return registry, run, failed_job, identities, dispatcher


def test_resume_first_call_dispatches_replacement_not_stale_failed_job(
    tmp_path: Path,
) -> None:
    registry, run, failed_job, identities, dispatcher = _resume_fixture(tmp_path)

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: _FakeLauncher(),
        coordinator_root=tmp_path / "coordinator",
        operator_resume=True,
    )

    assert result["reason"] != "job-failed"
    assert result["job_id"] != failed_job["job_id"]
    all_jobs = registry.list_jobs()
    assert len(all_jobs) == 2
    replacement = registry.get_job(result["job_id"])
    assert replacement["workflow_card"] == "subagent-build"
    assert replacement["job_id"] != failed_job["job_id"]


def test_resume_replay_keeps_single_replacement_job(tmp_path: Path) -> None:
    registry, run, failed_job, identities, dispatcher = _resume_fixture(tmp_path)

    first = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: _FakeLauncher(),
        coordinator_root=tmp_path / "coordinator",
        operator_resume=True,
    )
    assert len(registry.list_jobs()) == 2

    second = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: _FakeLauncher(),
        coordinator_root=tmp_path / "coordinator",
        operator_resume=True,
    )

    assert second["job_id"] == first["job_id"]
    assert len(registry.list_jobs()) == 2
