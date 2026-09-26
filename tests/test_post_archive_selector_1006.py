"""#1006：post-archive Builder 的 reuse 與 dispatch provenance 綁定 exact candidate。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.coordinator import manager, work_bridge
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


REPO = "hamanpaul/paulsha-cortex"
WORK_ID = "post-archive-selector-1006"
OLD_CANDIDATE = "b" * 40
ARCHIVE_CANDIDATE = "c" * 40


def _run(registry: JobRegistry, workspace: Path, *, candidate: str):
    step = WorkflowStep(
        phase="build",
        persona="builder",
        card="tdd-red",
        executor="copilot",
        model="gpt",
        domain="openai",
        inputs=(),
        outputs=(),
        commit_policy="required",
    )
    return registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace),
        combo="feature-oneshot",
        current_phase="build",
        steps=(step,),
        issue_refs=(f"{REPO}#1006",),
        openspec_refs=(WORK_ID,),
        pr_refs=(),
        attempts={"build": 1},
        candidate_head=candidate,
        gate_status="running",
    )


def _builder_job(
    registry: JobRegistry,
    run,
    *,
    dispatch_head: str,
    status: str = "dispatched",
    task: str | None = None,
    subject_head: str | None = None,
) -> dict[str, object]:
    job = registry.create_job(
        task=task or f"wf-{WORK_ID}-tdd-red",
        persona="builder",
        branch=f"feature/{WORK_ID}",
        pane="",
        worktree=run.workspace_root,
        dispatch_head=dispatch_head,
        subject_head=subject_head,
        executor="copilot",
        model_id="gpt",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="tdd-red",
        workflow_phase="build",
        source_revision=run.source_revision,
    )
    if status == "exited":
        return registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    return job


def _record_archive(registry: JobRegistry, run, *, state_root: Path) -> dict[str, object]:
    return work_bridge._record_manager_ship_job(
        registry=registry,
        state_root=state_root,
        run=run,
        worktree=Path(run.workspace_root),
        branch=f"feature/{WORK_ID}",
        card="openspec-archive",
        old_head=OLD_CANDIDATE,
        new_head=ARCHIVE_CANDIDATE,
    )


class _WorktreeCreator:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.calls: list[tuple[str, str, str | None]] = []

    def create(self, branch: str, *, job_id: str, base_sha: str | None = None) -> str:
        self.calls.append((branch, job_id, base_sha))
        return str(self.workspace)


class _Launcher:
    def as_commit_required(self):
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str):
        return LaunchHandle(
            executor="copilot",
            model_id="gpt",
            session_name=slice_id,
            pid=100,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


def _identities() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [{
            "executor": "copilot",
            "model_id": "gpt",
            "independence_domain": "openai",
            "capabilities": ["build"],
        }]
    )


def test_dispatch_rejects_same_card_builder_from_pre_archive_candidate(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, workspace, candidate=ARCHIVE_CANDIDATE)
    old_job = _builder_job(
        registry,
        run,
        dispatch_head=OLD_CANDIDATE,
        status="exited",
        subject_head=OLD_CANDIDATE,
    )
    archive = _record_archive(registry, run, state_root=tmp_path / "coordinator")
    creator = _WorktreeCreator(workspace)
    dispatcher = SimpleNamespace(
        _registry=registry,
        _git_runner=None,
        _worktree_creator=creator,
    )

    dispatched = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=_identities(),
        launcher_factory=lambda _identity: _Launcher(),
        coordinator_root=tmp_path / "coordinator",
    )

    assert dispatched is not None
    assert dispatched["job_id"] != old_job["job_id"]
    assert creator.calls[0][2] == ARCHIVE_CANDIDATE
    assert dispatched["dispatch_head"] == ARCHIVE_CANDIDATE
    assert archive["subject_head"] == ARCHIVE_CANDIDATE


def test_resume_selects_builder_dispatched_from_current_candidate(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, workspace, candidate=ARCHIVE_CANDIDATE)
    _record_archive(registry, run, state_root=tmp_path / "coordinator")
    current = _builder_job(
        registry,
        run,
        dispatch_head=ARCHIVE_CANDIDATE,
        task=f"wf-{WORK_ID}-current",
    )
    stale = _builder_job(
        registry,
        run,
        dispatch_head=OLD_CANDIDATE,
        task=f"wf-{WORK_ID}-stale",
    )
    polled: list[str] = []

    def poll_headless_done(job_id: str) -> dict[str, object]:
        polled.append(job_id)
        return registry.get_job(job_id)

    dispatcher = SimpleNamespace(
        _registry=registry,
        _git_runner=None,
        poll_headless_done=poll_headless_done,
    )

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _identity: None,
        coordinator_root=tmp_path / "coordinator",
    )

    assert result["reason"] == "in-flight"
    assert result["job_id"] == current["job_id"]
    assert polled == [current["job_id"]]
    assert stale["dispatch_head"] == OLD_CANDIDATE
