"""#214：manager_daemon.py 的 workflow-action/start 觸發處消費 StageExecutionKey

reuse 判定——相同 key 第二次執行時短路掉 dispatch_workflow_card（不增加 model
invocation count），沒帶 key 或找不到可 reuse 的 evidence時行為與過去完全相同。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import manager, manager_daemon
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry, compute_stage_execution_key
from paulsha_cortex.coordinator.workflow import WorkflowStep


def _build_step() -> WorkflowStep:
    return WorkflowStep(
        phase="build",
        persona="builder",
        card="build",
        executor="codex",
        model="gpt-5",
        domain="openai",
        inputs=(),
        outputs=(),
    )


def _make_run(registry: JobRegistry, tmp_path: Path):
    return registry._manager_create_workflow_run(
        work_id="214-stage-execution-key",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=(_build_step(),),
        issue_refs=("hamanpaul/paulsha-cortex#214",),
        openspec_refs=("214-stage-execution-key",),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
    )


def _executor(
    *,
    dispatcher,
    tmp_path: Path,
    stage_evidence_validator=None,
):
    return manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
        stage_evidence_validator=stage_evidence_validator,
    )


def test_start_reuses_stage_evidence_and_does_not_dispatch_a_new_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _make_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    stage_execution_key = compute_stage_execution_key(
        repo="hamanpaul/paulsha-cortex",
        work_id="214-stage-execution-key",
        card="build",
        phase="build",
        executor="codex",
        model="gpt-5",
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        frozen_input_hashes=("1" * 64,),
        action="propose-diff",
        test_policy="focused",
    )
    prior_job = registry.create_job(
        task="prior-build",
        persona="review",
        branch="feature/prior-build",
        pane="pane-1",
        worktree=str(tmp_path / "prior-build"),
        workflow_run_id="prior-run",
        workflow_stage_execution_key=stage_execution_key,
    )
    registry.update_headless_result(prior_job["job_id"], status="exited", exit_code=0)
    registry.bind_workflow_evidence(
        prior_job["job_id"],
        locator={"kind": "gate", "path": "evidence/workflow/prior-build.json", "hash": "e" * 64},
    )
    jobs_before = registry.list_jobs()

    monkeypatch.setattr(
        manager,
        "apply_workflow_action",
        lambda *a, **k: {"run_id": run.run_id, "current_phase": "build"},
    )

    def _must_not_dispatch(*args, **kwargs):
        raise AssertionError("相同 stage_execution_key 找到可重用 evidence 時不應再派新 job")

    monkeypatch.setattr(manager, "dispatch_workflow_card", _must_not_dispatch)

    executor = _executor(
        dispatcher=dispatcher,
        tmp_path=tmp_path,
        stage_evidence_validator=lambda _evidence: True,
    )
    result = executor(
        build_request(
            req_type="workflow-action",
            args={"action": "start", "stage_execution_key": stage_execution_key},
            requested_by="operator",
        )
    )

    assert result["reused_from"] == {
        "run_id": "prior-run",
        "job_id": prior_job["job_id"],
        "evidence": {"kind": "gate", "path": "evidence/workflow/prior-build.json", "hash": "e" * 64},
        "evidence_hash": "e" * 64,
    }
    assert "job_id" not in result
    # model invocation count 不增加：沒有因為這次「reuse」而多出任何 job。
    assert registry.list_jobs() == jobs_before


def test_start_dispatches_normally_when_no_reusable_stage_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _make_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    monkeypatch.setattr(
        manager,
        "apply_workflow_action",
        lambda *a, **k: {"run_id": run.run_id, "current_phase": "build"},
    )
    dispatch_calls = []

    def _fake_dispatch(*args, **kwargs):
        dispatch_calls.append(kwargs)
        # #830：daemon 消費端現在驗 registry 綁定，fake 必須登記綁到本 run 的真 Job。
        return registry.create_job(
            task="new-build-job", persona="builder", branch="feature/new-build-job", pane="",
            worktree=str(tmp_path / "new-build-job"), workflow_run_id=run.run_id, workflow_card="build",
        )

    monkeypatch.setattr(manager, "dispatch_workflow_card", _fake_dispatch)

    executor = _executor(
        dispatcher=dispatcher,
        tmp_path=tmp_path,
        stage_evidence_validator=lambda _evidence: True,
    )
    unmatched_key = compute_stage_execution_key(
        repo="hamanpaul/paulsha-cortex",
        work_id="214-stage-execution-key",
        card="build",
        phase="build",
        executor="codex",
        model="a-different-model",
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        frozen_input_hashes=("1" * 64,),
        action="propose-diff",
        test_policy="focused",
    )
    result = executor(
        build_request(
            req_type="workflow-action",
            args={"action": "start", "stage_execution_key": unmatched_key},
            requested_by="operator",
        )
    )

    assert len(dispatch_calls) == 1
    assert result["job_id"] == registry.list_jobs()[-1]["job_id"]
    assert "reused_from" not in result


def test_start_without_stage_execution_key_keeps_previous_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _make_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    monkeypatch.setattr(
        manager,
        "apply_workflow_action",
        lambda *a, **k: {"run_id": run.run_id, "current_phase": "build"},
    )
    dispatch_calls = []

    def _fake_dispatch(*args, **kwargs):
        dispatch_calls.append(kwargs)
        # #830：daemon 消費端現在驗 registry 綁定，fake 必須登記綁到本 run 的真 Job。
        return registry.create_job(
            task="new-build-job", persona="builder", branch="feature/new-build-job", pane="",
            worktree=str(tmp_path / "new-build-job"), workflow_run_id=run.run_id, workflow_card="build",
        )

    monkeypatch.setattr(manager, "dispatch_workflow_card", _fake_dispatch)

    # 沒帶 stage_evidence_validator、也沒帶 stage_execution_key：backward
    # compatible，行為必須與 #214 之前完全相同。
    executor = _executor(dispatcher=dispatcher, tmp_path=tmp_path)
    result = executor(
        build_request(req_type="workflow-action", args={"action": "start"}, requested_by="operator")
    )

    assert len(dispatch_calls) == 1
    assert result["job_id"] == registry.list_jobs()[-1]["job_id"]
    assert "reused_from" not in result
