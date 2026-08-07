"""issue #203：`work-action intake` 必須觸發與 start/resume/retry-build 相同的
builder job 派工——manager_daemon 的 job-dispatch trigger
（`args.get("action") in {"start", "resume", "retry-build"}`）若漏加
``"intake"``，WorkflowRun 建立了，但永遠不會有實際的 builder job 被派出。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import manager, manager_daemon
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


def _claim_step() -> WorkflowStep:
    return WorkflowStep(
        phase="claim",
        persona="manager",
        card="claim",
        executor="cortex-manager",
        model="deterministic",
        domain="cortex",
        inputs=(),
        outputs=(),
        gate_result="passed",
    )


def _make_run(registry: JobRegistry, tmp_path: Path):
    return registry._manager_create_workflow_run(
        work_id="203-intake-dispatch",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="claim",
        steps=(_claim_step(),),
        issue_refs=("hamanpaul/paulsha-cortex#203",),
    )


def test_intake_dispatches_builder_job_like_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _make_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    def fake_work_action_fn(*, args, requested_by):
        assert args["action"] == "intake"
        return {"result": {"action": "claim", "run": run.to_dict()}}

    dispatch_calls: list[dict] = []

    def fake_dispatch(*args, **kwargs):
        dispatch_calls.append(kwargs)
        return {"job_id": "intake-builder-job"}

    monkeypatch.setattr(manager, "dispatch_workflow_card", fake_dispatch)

    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
        work_action_fn=fake_work_action_fn,
    )

    result = executor(
        build_request(
            req_type="work-action",
            args={
                "action": "intake",
                "repo": "hamanpaul/paulsha-cortex",
                "work_id": "203-intake-dispatch",
            },
            requested_by="operator",
        )
    )

    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["run"].run_id == run.run_id
    assert result["result"]["job_id"] == "intake-builder-job"


def test_link_and_unlink_do_not_spuriously_dispatch_a_builder_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反例：link/unlink 不是 claim 語意，不該觸發 job 派工（即使呼叫端
    result 形狀恰好帶有一個 ``run`` 鍵，也不能被 dispatch trigger 誤判）。
    """

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    def fake_work_action_fn(*, args, requested_by):
        return {"result": {"action": "link", "override_path": "/tmp/x", "source": {}}}

    def _must_not_dispatch(*args, **kwargs):
        raise AssertionError("link 不應觸發 builder job 派工")

    monkeypatch.setattr(manager, "dispatch_workflow_card", _must_not_dispatch)

    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
        work_action_fn=fake_work_action_fn,
    )

    result = executor(
        build_request(
            req_type="work-action",
            args={
                "action": "link",
                "repo": "hamanpaul/paulsha-cortex",
                "work_id": "203-intake-dispatch",
                "kind": "path",
                "ref": "docs/todo.md",
            },
            requested_by="operator",
        )
    )

    assert "job_id" not in result.get("result", {})
