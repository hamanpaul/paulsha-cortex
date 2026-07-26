from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from paulsha_cortex.coordinator import manager, work_bridge
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


_REPO = "hamanpaul/paulsha-cortex"


class _CommitLauncher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def as_commit_required(self):
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        self.calls.append(
            {
                "slice_id": slice_id,
                "worktree": worktree,
                "prompt": prompt,
                "log_dir": log_dir,
            },
        )
        return LaunchHandle(
            executor="copilot",
            model_id="gpt",
            session_name=slice_id,
            pid=100,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


class _RecordingCreator:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.calls: list[str] = []

    def create(self, branch: str, *, base_sha: str | None = None) -> str:
        self.calls.append(branch)
        return str(self.repo_root)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test User"], check=True)
    (root / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)


def _build_run(
    registry: JobRegistry,
    workspace_root: Path,
    issue_refs: tuple[str, ...],
) -> Any:
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
        test_policy="red-required",
        gate_result="pending",
    )
    return registry._manager_create_workflow_run(
        work_id="multi-issue-worktree",
        repo=_REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace_root),
        combo="feature-oneshot",
        current_phase="build",
        steps=(step,),
        issue_refs=issue_refs,
        openspec_refs=("2026-07-26-multi-issue-worktree",),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
    )


def _dispatch(
    run,
    registry: JobRegistry,
    creator: _RecordingCreator,
    tmp_path: Path,
) -> tuple[dict[str, object], _CommitLauncher]:
    launcher = _CommitLauncher()
    dispatcher = type(
        "D",
        (),
        {
            "_registry": registry,
            "_worktree_creator": creator,
            "_git_runner": None,
        },
    )()
    job = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=IdentityRegistry.from_rows(
            [{
                "executor": "copilot",
                "model_id": "gpt",
                "independence_domain": "openai",
                "capabilities": ["build"],
            }]
        ),
        launcher_factory=lambda _: launcher,
        coordinator_root=tmp_path / "coordinator",
    )
    assert job is not None
    return registry.get_job(job["job_id"]), launcher


def test_build_branch_uses_canonical_primary_issue_for_multi_issue_run(tmp_path: Path) -> None:
    workspace = tmp_path / "run-repo"
    _init_repo(workspace)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _build_run(
        registry,
        workspace_root=workspace,
        issue_refs=(f"{_REPO}#39", f"{_REPO}#34"),
    )
    creator = _RecordingCreator(workspace)
    job, launcher = _dispatch(run, registry, creator, tmp_path)

    assert creator.calls == ["feature/34-multi-issue-worktree"]
    assert job["branch"] == "feature/34-multi-issue-worktree"
    assert launcher.calls[0]["worktree"] == str(workspace)


def test_single_issue_build_branch_unchanged(tmp_path: Path) -> None:
    workspace = tmp_path / "run-repo"
    _init_repo(workspace)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _build_run(registry, workspace_root=workspace, issue_refs=(f"{_REPO}#39",))
    creator = _RecordingCreator(workspace)
    job, launcher = _dispatch(run, registry, creator, tmp_path)

    assert creator.calls == ["feature/39-multi-issue-worktree"]
    assert job["branch"] == "feature/39-multi-issue-worktree"
    assert launcher.calls[0]["worktree"] == str(workspace)


def test_build_worktree_uses_run_workspace_root_not_manager_repo(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from paulsha_cortex.config.paths import worktree_root_for

    manager_repo = tmp_path / "manager-repo"
    run_workspace = tmp_path / "run-repo"
    _init_repo(manager_repo)
    _init_repo(run_workspace)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _build_run(
        registry,
        workspace_root=run_workspace,
        issue_refs=(f"{_REPO}#34",),
    )
    # Production daemon injects a real ScriptWorktreeCreator anchored at the
    # Manager repo; build phase must re-anchor it to the run's workspace_root.
    manager_pool = manager_repo.parent / f"{manager_repo.name}-worktrees"
    manager_creator = manager.seams.ScriptWorktreeCreator(
        repo=manager_repo, wt_root=manager_pool, base="main"
    )
    launcher = _CommitLauncher()
    dispatcher = type(
        "D",
        (),
        {
            "_registry": registry,
            "_worktree_creator": manager_creator,
            "_git_runner": None,
        },
    )()
    job = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=IdentityRegistry.from_rows(
            [{
                "executor": "copilot",
                "model_id": "gpt",
                "independence_domain": "openai",
                "capabilities": ["build"],
            }]
        ),
        launcher_factory=lambda _: launcher,
        coordinator_root=tmp_path / "coordinator",
    )
    assert job is not None
    persisted = registry.get_job(job["job_id"])

    expected_pool = worktree_root_for(run_workspace)
    expected_worktree = expected_pool / "feature-34-multi-issue-worktree"
    assert persisted["worktree"] == str(expected_worktree)
    assert expected_worktree.is_dir()
    # Manager-anchored pool must NOT receive the build worktree.
    assert not (manager_pool / "feature-34-multi-issue-worktree").exists()


def test_pr_metadata_closes_all_mapped_issues(tmp_path: Path) -> None:
    workspace = tmp_path / "run-repo"
    _init_repo(workspace)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _build_run(
        registry,
        workspace_root=workspace,
        issue_refs=(f"{_REPO}#39", f"{_REPO}#34"),
    )
    metadata = work_bridge._pr_metadata(run)
    closes = [line for line in metadata["body"].splitlines() if line.startswith("Closes #")]

    assert closes == ["Closes #34", "Closes #39"]
