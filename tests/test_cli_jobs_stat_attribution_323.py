"""#323：``cortex jobs``／``stat`` 對 workflow lane job 補 work_id／primary
issue 歸屬欄——operator 原本得手動 join ``jobs.json`` 的 job row
（``workflow_run_id``／``worktree``／``branch``）與 run row（``work_id``／
``issue_refs``）才能判讀某個 ``wf-xxxxxxxx-<card>-<n>`` job 屬於哪個 work
item、對應哪張 issue。card 已由既有 ``workflow_card`` 欄位提供，本次只補
``workflow_work_id``／``workflow_primary_issue`` 兩欄，皆為輸出端 join，零
額外持久化狀態；既有欄位一律保留不變。

比照 #223 ``test_cli_stat_decomposition_depths_223.py`` 的 ``_manager_create_
workflow_run`` 測試模式。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from paulsha_cortex.coordinator import cli as coordinator_cli
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


def _step() -> WorkflowStep:
    return WorkflowStep(
        phase="build",
        persona="builder",
        card="subagent-build",
        executor="agy",
        model="gemini-3.1-pro-high",
        domain="google",
        inputs=(),
        outputs=(),
        gate_result="pending",
    )


def _create_run(registry: JobRegistry, *, work_id: str, claim_seed: str, issue_refs=()):
    return registry._manager_create_workflow_run(
        repo="hamanpaul/paulsha-cortex",
        work_id=work_id,
        claim_key=f"claim:v1:{claim_seed * 64}",
        source_revision="rev-323",
        workspace_root="/tmp/workspace",
        combo="feature-oneshot",
        current_phase="build",
        steps=(_step(),),
        issue_refs=tuple(issue_refs),
    )


def test_jobs_workflow_lane_job_gets_work_id_and_primary_issue(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _create_run(
        registry,
        work_id="323-jobs-stat-attribution",
        claim_seed="1",
        issue_refs=("hamanpaul/paulsha-cortex#323",),
    )
    registry.create_job(
        task="wf-18dfaa134b-subagent-build",
        persona="builder",
        branch="feature/323-323-jobs-stat-attribution",
        pane="",
        worktree="/wt/323",
        workflow_run_id=run.run_id,
        workflow_card="subagent-build",
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = coordinator_cli.main(["jobs"], registry=registry)
    assert exit_code == 0
    jobs = json.loads(buffer.getvalue())
    assert len(jobs) == 1
    job = jobs[0]

    # 既有欄位不變（只增不改不刪）。
    assert job["job_id"] == "wf-18dfaa134b-subagent-build-1"
    assert job["workflow_run_id"] == run.run_id
    assert job["workflow_card"] == "subagent-build"
    assert job["worktree"] == "/wt/323"
    assert job["branch"] == "feature/323-323-jobs-stat-attribution"

    # 新欄位：join registry 的 workflow run 得出。
    assert job["workflow_work_id"] == "323-jobs-stat-attribution"
    assert job["workflow_primary_issue"] == "hamanpaul/paulsha-cortex#323"


def test_jobs_non_workflow_job_new_fields_are_null(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    registry.create_job(
        task="add-cortex-version-flag-build",
        persona="builder",
        branch="feature/add-cortex-version-flag-build",
        pane="",
        worktree="/wt/legacy",
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = coordinator_cli.main(["jobs"], registry=registry)
    assert exit_code == 0
    jobs = json.loads(buffer.getvalue())
    assert len(jobs) == 1
    job = jobs[0]

    assert job["workflow_run_id"] is None
    assert job["workflow_card"] is None
    # 非 workflow lane job 不受影響：新欄位存在但為 null（與既有 workflow_*
    # 欄位「一律出現、N/A 時為 null」慣例一致），不是「不出現」。
    assert "workflow_work_id" in job
    assert job["workflow_work_id"] is None
    assert "workflow_primary_issue" in job
    assert job["workflow_primary_issue"] is None


def test_jobs_workflow_run_with_no_issue_refs_primary_issue_is_null(tmp_path: Path) -> None:
    # openspec-only work item：run 存在但 issue_refs 空，primary issue 應為 null
    # 而非拋錯或省略欄位。
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _create_run(
        registry,
        work_id="feat-work-gc-v2",
        claim_seed="2",
        issue_refs=(),
    )
    registry.create_job(
        task="wf-abc123-subagent-build",
        persona="builder",
        branch="feature/feat-work-gc-v2",
        pane="",
        worktree="/wt/gc",
        workflow_run_id=run.run_id,
        workflow_card="subagent-build",
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = coordinator_cli.main(["jobs"], registry=registry)
    assert exit_code == 0
    job = json.loads(buffer.getvalue())[0]
    assert job["workflow_work_id"] == "feat-work-gc-v2"
    assert job["workflow_primary_issue"] is None


def test_jobs_dangling_workflow_run_id_does_not_crash(tmp_path: Path) -> None:
    # 理論上不該發生，但輸出端 join 對查無 run 的 workflow_run_id 必須
    # fail-soft（兩欄位皆 null），不得拋例外讓整個 jobs 輸出失敗。
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    registry.create_job(
        task="wf-dangling-subagent-build",
        persona="builder",
        branch="feature/dangling",
        pane="",
        worktree="/wt/dangling",
        workflow_run_id="workflow-does-not-exist",
        workflow_card="subagent-build",
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = coordinator_cli.main(["jobs"], registry=registry)
    assert exit_code == 0
    job = json.loads(buffer.getvalue())[0]
    assert job["workflow_work_id"] is None
    assert job["workflow_primary_issue"] is None


def test_stat_single_job_workflow_lane_gets_attribution(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _create_run(
        registry,
        work_id="323-jobs-stat-attribution",
        claim_seed="3",
        issue_refs=("hamanpaul/paulsha-cortex#323",),
    )
    registry.create_job(
        task="wf-18dfaa134b-subagent-build",
        persona="builder",
        branch="feature/323-323-jobs-stat-attribution",
        pane="",
        worktree="/wt/323",
        workflow_run_id=run.run_id,
        workflow_card="subagent-build",
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = coordinator_cli.main(
            ["stat", "wf-18dfaa134b-subagent-build-1"], registry=registry
        )
    assert exit_code == 0
    job = json.loads(buffer.getvalue())
    assert job["job_id"] == "wf-18dfaa134b-subagent-build-1"
    assert job["workflow_work_id"] == "323-jobs-stat-attribution"
    assert job["workflow_primary_issue"] == "hamanpaul/paulsha-cortex#323"


def test_stat_single_job_non_workflow_fields_are_null(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    registry.create_job(
        task="legacy",
        persona="builder",
        branch="feature/legacy",
        pane="",
        worktree="/wt/legacy",
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = coordinator_cli.main(["stat", "legacy-1"], registry=registry)
    assert exit_code == 0
    job = json.loads(buffer.getvalue())
    assert job["workflow_work_id"] is None
    assert job["workflow_primary_issue"] is None


def test_stat_unknown_job_id_error_path_unchanged(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    err = io.StringIO()
    with redirect_stderr(err):
        exit_code = coordinator_cli.main(["stat", "nope-9"], registry=registry)
    assert exit_code == 1
    assert "nope-9" in err.getvalue()
