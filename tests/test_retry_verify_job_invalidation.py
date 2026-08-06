"""#315：retry-verify 重置 verify step 時必須失效舊 exited verification job。

否則 dispatch 路徑對 exited+sentinel 的最新 job 先 terminalize——reviewer
sandbox 依設計已清除，input snapshot 實檔不可重驗，run 永遠卡在
`workflow input snapshot file missing`，到不了 fresh dispatch。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

CANDIDATE = "c" * 40


def _steps() -> tuple[WorkflowStep, ...]:
    build = WorkflowStep(
        phase="build",
        persona="builder",
        card="subagent-build",
        executor="copilot",
        model="gpt-5.4",
        domain="github",
        inputs=(),
        outputs=(),
        gate_result="passed",
    )
    verify = WorkflowStep(
        phase="verify",
        persona="reviewer",
        card="verification",
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        gate_result="needs_human",
    )
    return (build, verify)


def _run_with_exited_verify_job(tmp_path: Path):
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id="retry-verify-work",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "e" * 64,
        source_revision="rev-e",
        workspace_root="/tmp/workspace",
        combo="feature-oneshot",
        current_phase="verify",
        steps=_steps(),
        issue_refs=("hamanpaul/paulsha-cortex#315",),
        attempts={"claim": 1, "verify": 1},
        facets=("needs_human",),
        candidate_head=CANDIDATE,
    )
    job = registry.create_job(
        task="wf-demo-verification",
        persona="reviewer",
        branch="feature/315-demo",
        pane="",
        worktree=str(tmp_path / "sandbox"),
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="verification",
        workflow_phase="verify",
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    return registry, run, job["job_id"]


def test_reset_marks_exited_verify_job_failed(tmp_path: Path) -> None:
    registry, run, job_id = _run_with_exited_verify_job(tmp_path)
    registry._manager_reset_workflow_for_retry_verify(
        run.run_id, expected_candidate=CANDIDATE
    )
    row = registry.get_job(job_id)
    assert row["status"] == "failed"


def test_reset_leaves_build_phase_jobs_untouched(tmp_path: Path) -> None:
    registry, run, _ = _run_with_exited_verify_job(tmp_path)
    build_job = registry.create_job(
        task="wf-demo-build",
        persona="builder",
        branch="feature/315-demo",
        pane="",
        worktree=str(tmp_path / "wt"),
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
    )
    registry.update_headless_result(build_job["job_id"], status="exited", exit_code=0)
    registry._manager_reset_workflow_for_retry_verify(
        run.run_id, expected_candidate=CANDIDATE
    )
    assert registry.get_job(build_job["job_id"])["status"] == "exited"


def test_reset_still_refuses_active_verify_job(tmp_path: Path) -> None:
    registry, run, _ = _run_with_exited_verify_job(tmp_path)
    registry.create_job(
        task="wf-demo-verification-2",
        persona="reviewer",
        branch="feature/315-demo",
        pane="",
        worktree=str(tmp_path / "sandbox2"),
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="verification",
        workflow_phase="verify",
    )
    with pytest.raises(ValueError, match="active workflow job"):
        registry._manager_reset_workflow_for_retry_verify(
            run.run_id, expected_candidate=CANDIDATE
        )


def test_retry_review_reset_marks_exited_review_job_failed(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    build = WorkflowStep(
        phase="build", persona="builder", card="subagent-build",
        executor="copilot", model="gpt-5.4", domain="github",
        inputs=(), outputs=(), gate_result="passed",
    )
    verify = WorkflowStep(
        phase="verify", persona="reviewer", card="verification",
        executor=None, model=None, domain=None,
        inputs=(), outputs=(), gate_result="passed",
    )
    review = WorkflowStep(
        phase="review", persona="reviewer", card="code-review",
        executor=None, model=None, domain=None,
        inputs=(), outputs=(), gate_result="needs_human",
    )
    run = registry._manager_create_workflow_run(
        work_id="retry-review-work",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "f" * 64,
        source_revision="rev-f",
        workspace_root="/tmp/workspace",
        combo="feature-oneshot",
        current_phase="review",
        steps=(build, verify, review),
        issue_refs=("hamanpaul/paulsha-cortex#315",),
        attempts={"claim": 1, "review": 1},
        facets=("needs_human",),
        candidate_head=CANDIDATE,
        verified_head=CANDIDATE,
    )
    job = registry.create_job(
        task="wf-demo-code-review",
        persona="reviewer",
        branch="feature/315-demo",
        pane="",
        worktree=str(tmp_path / "sandbox"),
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="code-review",
        workflow_phase="review",
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    registry._manager_reset_workflow_for_retry_review(
        run.run_id, expected_candidate=CANDIDATE
    )
    assert registry.get_job(job["job_id"])["status"] == "failed"


def test_review_tool_schema_allows_authority_hashes() -> None:
    """#315 補遺 3：StructuredOutput 工具 schema 必須開放 authority_hashes 屬性，
    否則 additionalProperties:false 下模型無法交出 manager 驗證器要求的攻證欄位。"""
    import json as _json

    from paulsha_cortex.coordinator.launcher import _claude_review_json_schema

    schema = _json.loads(_claude_review_json_schema("workflow-review-result"))
    assert "authority_hashes" in schema["properties"]
    assert schema["properties"]["authority_hashes"]["type"] == "object"
