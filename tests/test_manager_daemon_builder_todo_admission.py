from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import manager, manager_daemon
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


def _run(registry: JobRegistry, root: Path):
    step = WorkflowStep(
        phase="build",
        persona="builder",
        card="build",
        executor="copilot",
        model="gpt",
        domain="openai",
        inputs=(),
        outputs=(),
    )
    return registry._manager_create_workflow_run(
        work_id="builder-todo-admission",
        repo="acme/demo",
        claim_key="claim:v1:" + "d" * 64,
        source_revision="d" * 64,
        workspace_root=str(root),
        combo="feature-oneshot",
        current_phase="build",
        steps=(step,),
        issue_refs=("acme/demo#12",),
        openspec_refs=(),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
    )


def test_daemon_loads_current_authority_and_passes_admission_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(registry, tmp_path)
    dispatcher = type("Dispatcher", (), {"_registry": registry, "_git_runner": None})()
    authority = SimpleNamespace(
        repo=run.repo,
        work_id=run.work_id,
        mapped_todo_paths=("docs/superpowers/workstreams/demo/todo.md",),
    )
    monkeypatch.setattr(manager_daemon, "load_work_authority", lambda **_kwargs: authority, raising=False)
    monkeypatch.setattr(manager_daemon, "work_authority_digest", lambda _authority: run.source_revision, raising=False)
    monkeypatch.setattr(manager, "apply_workflow_action", lambda *args, **kwargs: {"run_id": run.run_id})
    dispatched: list[object] = []

    def fake_dispatch(*_args, **kwargs):
        dispatched.append(kwargs["builder_todo_admission"])
        return registry.create_job(
            task="builder",
            persona="builder",
            branch="feature/builder",
            pane="",
            worktree=str(tmp_path / "builder"),
            workflow_run_id=run.run_id,
            workflow_card="build",
        )

    monkeypatch.setattr(manager, "dispatch_workflow_card", fake_dispatch)
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
    )

    executor(
        build_request(req_type="workflow-action", args={"action": "start"}, requested_by="operator")
    )

    assert len(dispatched) == 1
    assert dispatched[0].authority_revision == run.source_revision
    assert dispatched[0].mapped_todo_paths == authority.mapped_todo_paths
    assert dispatched[0].error is None


def test_daemon_converts_authority_load_failure_to_fail_closed_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = SimpleNamespace(
        repo="acme/demo",
        work_id="builder-todo-admission",
        current_phase="build",
        claim_key="claim:v1:" + "d" * 64,
    )

    def fail_load(**_kwargs):
        raise ValueError("snapshot unavailable")

    monkeypatch.setattr(manager_daemon, "load_work_authority", fail_load)
    admission = manager_daemon._builder_todo_admission_for_run(run)

    assert admission.authority_revision is None
    assert admission.mapped_todo_paths is None
    assert admission.error == "current-work-authority-unavailable"
