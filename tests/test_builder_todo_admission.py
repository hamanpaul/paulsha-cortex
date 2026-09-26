"""Builder 首次派工前必須以目前 WorkAuthority 核對 Todo cardinality。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


_SOURCE_REVISION = "d" * 64


class _RecordingCreator:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.calls: list[str] = []

    def create(self, branch: str, *, job_id: str | None = None, base_sha: str | None = None) -> str:
        self.calls.append(branch)
        return str(self.repo)


class _RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def as_commit_required(self):
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        self.calls.append({"slice_id": slice_id, "worktree": worktree, "log_dir": log_dir})
        return LaunchHandle(
            executor="copilot",
            model_id="gpt",
            session_name=slice_id,
            pid=100,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def _fixture(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
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
    )
    run = registry._manager_create_workflow_run(
        work_id="todo-admission",
        repo="acme/demo",
        claim_key="claim:v1:" + _SOURCE_REVISION,
        source_revision=_SOURCE_REVISION,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="build",
        steps=(step,),
        issue_refs=("acme/demo#12",),
        openspec_refs=(),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
    )
    creator = _RecordingCreator(repo)
    launcher = _RecordingLauncher()
    dispatcher = type(
        "Dispatcher",
        (),
        {"_registry": registry, "_worktree_creator": creator, "_git_runner": None},
    )()
    identities = IdentityRegistry.from_rows(
        [{
            "executor": "copilot",
            "model_id": "gpt",
            "independence_domain": "openai",
            "capabilities": ["build"],
        }]
    )
    return registry, run, dispatcher, identities, creator, launcher


def _dispatch(tmp_path: Path, *, todo_paths: tuple[str, ...], authority_revision: str = _SOURCE_REVISION):
    registry, run, dispatcher, identities, creator, launcher = _fixture(tmp_path)
    admission = manager.BuilderTodoAdmission(
        authority_revision=authority_revision,
        mapped_todo_paths=todo_paths,
        error=None,
    )
    result = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=lambda _identity: launcher,
        coordinator_root=tmp_path / "coordinator",
        builder_todo_admission=admission,
    )
    return registry, run, creator, launcher, result


def test_zero_todo_stops_before_builder_job_worktree_or_launch(tmp_path: Path) -> None:
    registry, run, creator, launcher, result = _dispatch(tmp_path, todo_paths=())

    assert result == {
        "run_id": run.run_id,
        "current_phase": "build",
        "reason": "builder-todo-missing",
    }
    assert registry.list_jobs() == []
    assert creator.calls == []
    assert launcher.calls == []
    stopped = registry.get_workflow_run(run.run_id)
    assert "needs_human" in stopped.facets
    assert stopped.needs_human_reason["reason"] == "builder-todo-missing"
    assert "發布 canonical Todo" in stopped.needs_human_reason["detail"]
    assert "link path" in stopped.needs_human_reason["detail"]
    assert "Monitor" in stopped.needs_human_reason["detail"]


def test_one_current_todo_allows_exactly_one_builder_dispatch(tmp_path: Path) -> None:
    registry, _run, creator, launcher, result = _dispatch(
        tmp_path,
        todo_paths=("docs/superpowers/workstreams/todo-admission/todo.md",),
    )

    assert isinstance(result, dict) and result.get("job_id")
    assert len(registry.list_jobs()) == 1
    assert len(creator.calls) == 1
    assert len(launcher.calls) == 1


def test_multiple_todos_stop_with_unlink_guidance_before_side_effects(tmp_path: Path) -> None:
    registry, run, creator, launcher, result = _dispatch(
        tmp_path,
        todo_paths=("docs/one/todo.md", "docs/two/todo.md"),
    )

    assert result == {
        "run_id": run.run_id,
        "current_phase": "build",
        "reason": "builder-todo-ambiguous",
    }
    assert registry.list_jobs() == []
    assert creator.calls == []
    assert launcher.calls == []
    detail = registry.get_workflow_run(run.run_id).needs_human_reason["detail"]
    assert "cortex work unlink" in detail
    assert "docs/one/todo.md" in detail
    assert "docs/two/todo.md" in detail


def test_changed_authority_claim_cannot_dispatch_with_old_run(tmp_path: Path) -> None:
    registry, run, creator, launcher, result = _dispatch(
        tmp_path,
        todo_paths=("docs/current/todo.md",),
        authority_revision="e" * 64,
    )

    assert result == {
        "run_id": run.run_id,
        "current_phase": "build",
        "reason": "builder-todo-authority-changed",
    }
    assert registry.list_jobs() == []
    assert creator.calls == []
    assert launcher.calls == []
    detail = registry.get_workflow_run(run.run_id).needs_human_reason["detail"]
    assert "目前 WorkAuthority" in detail
    assert "既有正式重啟流程" in detail


def test_resume_reloads_todo_admission_before_builder_dispatch(tmp_path: Path) -> None:
    registry, run, dispatcher, identities, _creator, launcher = _fixture(tmp_path)
    admissions = []

    def load_admission(bound_run):
        admissions.append(bound_run.run_id)
        return SimpleNamespace(
            authority_revision=bound_run.source_revision,
            mapped_todo_paths=(),
            error=None,
        )

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: launcher,
        coordinator_root=tmp_path / "coordinator",
        operator_resume=True,
        builder_todo_admission_loader=load_admission,
    )

    assert admissions and set(admissions) == {run.run_id}
    assert result["reason"] == "builder-todo-missing"
    assert registry.list_jobs() == []
