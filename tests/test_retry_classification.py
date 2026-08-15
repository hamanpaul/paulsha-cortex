"""#215：retry 分類骨架（work_actions.RetryClassification／_classify_retry）。

驗收條件對應：
- 1. model_repair 與 orchestrator_retry 兩類的定義與判決條件落地
- 2.（真正的 CompletionRecord 寫入見 tests/test_completion_retry_classification.py；
     本檔涵蓋的是分類決策點本身，以及 execute_work_action 的 retry-build 回傳）
- 3. 不再只以 vN 世代數（attempts["build"] 之類的重試次數）判斷重試性質
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import subprocess

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowRun, WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason


HEAD = "b" * 40
NOW = "2026-07-27T00:00:00+00:00"

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


def _run(
    *,
    current_phase: str,
    steps: tuple[WorkflowStep, ...],
    run_id: str = "run-1",
    attempts: dict[str, int] | None = None,
) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        work_id="demo",
        repo="acme/demo",
        claim_key="claim:v1:abc",
        source_revision="a" * 40,
        workspace_root="/tmp/workspace",
        combo="feature-oneshot",
        current_phase=current_phase,
        steps=steps,
        issue_refs=("acme/demo#12",),
        openspec_refs=("demo",),
        pr_refs=(),
        attempts=attempts or {},
        evidence_refs=(),
        gate_refs=(),
        brainstorm_required=False,
        primary_domain=None,
        candidate_head=HEAD,
        verified_head=None,
        facets=("needs_human",),
        gate_status="running",
        created_at=NOW,
        updated_at=NOW,
    )


def _job_args(*, run: WorkflowRun, repair_card: str, tmp_path: Path) -> dict:
    return {
        "task": f"wf-{run.run_id}-{repair_card}",
        "persona": "builder",
        "branch": "feature/demo",
        "pane": "",
        "worktree": str(tmp_path),
        "workflow_run_id": run.run_id,
        "workflow_card": repair_card,
        "workflow_phase": "build",
    }


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


def _snapshot(path: Path) -> Path:
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
                        "mapped_issues": [12],
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


# ---------------------------------------------------------------------------
# enum 定案（後波不得改名）：五類齊全、值為固定字串。
# ---------------------------------------------------------------------------


def test_enum_declares_all_five_values() -> None:
    values = {member.value for member in work_actions.RetryClassification}
    assert values == {
        "model_repair",
        "orchestrator_retry",
        "authority_restart",
        "review_handoff_failure",
        "source_owner_repair",
    }


def test_enum_members_are_plain_strings() -> None:
    assert work_actions.RetryClassification.MODEL_REPAIR == "model_repair"
    assert work_actions.RetryClassification.ORCHESTRATOR_RETRY == "orchestrator_retry"
    assert isinstance(work_actions.RetryClassification.MODEL_REPAIR, str)


# ---------------------------------------------------------------------------
# _classify_retry：AC1（model_repair／orchestrator_retry 判準落地）
# ---------------------------------------------------------------------------


def test_verify_phase_needs_human_is_model_repair(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        current_phase="verify",
        steps=(_step("build", "subagent-build", gate_result="passed"),),
    )
    assert work_actions._classify_retry(run, registry) == (
        work_actions.RetryClassification.MODEL_REPAIR
    )


def test_review_phase_needs_human_is_model_repair(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        current_phase="review",
        steps=(_step("build", "subagent-build", gate_result="passed"),),
    )
    assert work_actions._classify_retry(run, registry) == (
        work_actions.RetryClassification.MODEL_REPAIR
    )


def test_build_phase_crashed_builder_is_orchestrator_retry(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        current_phase="build",
        steps=(_step("build", "subagent-build", gate_result="pending"),),
    )
    job = registry.create_job(
        **_job_args(run=run, repair_card="subagent-build", tmp_path=tmp_path)
    )
    registry.update_headless_result(job["job_id"], status="failed", exit_code=1)
    assert work_actions._classify_retry(run, registry) == (
        work_actions.RetryClassification.ORCHESTRATOR_RETRY
    )


def test_build_phase_clean_exit_without_evidence_is_orchestrator_retry(
    tmp_path: Path,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        current_phase="build",
        steps=(_step("build", "subagent-build", gate_result="pending"),),
    )
    job = registry.create_job(
        **_job_args(run=run, repair_card="subagent-build", tmp_path=tmp_path)
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    assert registry.get_job(job["job_id"])["workflow_evidence"] is None
    assert work_actions._classify_retry(run, registry) == (
        work_actions.RetryClassification.ORCHESTRATOR_RETRY
    )


def test_build_phase_clean_exit_with_bound_evidence_is_model_repair(
    tmp_path: Path,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        current_phase="build",
        steps=(_step("build", "subagent-build", gate_result="pending"),),
    )
    job = registry.create_job(
        **_job_args(run=run, repair_card="subagent-build", tmp_path=tmp_path)
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    registry.bind_workflow_evidence(
        job["job_id"],
        locator={"kind": "verification", "path": "/evidence/v.json", "hash": "f" * 64},
    )
    assert work_actions._classify_retry(run, registry) == (
        work_actions.RetryClassification.MODEL_REPAIR
    )


def test_build_phase_missing_job_is_orchestrator_retry(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        current_phase="build",
        steps=(_step("build", "subagent-build", gate_result="pending"),),
    )
    assert work_actions._classify_retry(run, registry) == (
        work_actions.RetryClassification.ORCHESTRATOR_RETRY
    )


def test_classification_ignores_build_attempt_generation_count(tmp_path: Path) -> None:
    """AC3：不再只以 vN 世代數（attempts["build"]）判斷重試性質——同一組
    phase/job 訊號下，不論 attempts 計到第幾代，分類結果都必須一致。"""
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    low = _run(
        current_phase="build",
        steps=(_step("build", "subagent-build", gate_result="pending"),),
        run_id="run-low",
        attempts={"build": 1},
    )
    high = _run(
        current_phase="build",
        steps=(_step("build", "subagent-build", gate_result="pending"),),
        run_id="run-low",
        attempts={"build": 41},
    )
    job = registry.create_job(
        **_job_args(run=low, repair_card="subagent-build", tmp_path=tmp_path)
    )
    registry.update_headless_result(job["job_id"], status="failed", exit_code=1)
    assert work_actions._classify_retry(low, registry) == work_actions._classify_retry(
        high, registry
    )


# ---------------------------------------------------------------------------
# execute_work_action(retry-build)：AC1 決策點在真正呼叫路徑上落地
# ---------------------------------------------------------------------------


def test_retry_build_result_carries_orchestrator_retry_for_unbound_terminalization(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    initial = work_actions._fallback_workflow_starter(
        registry, tmp_path / "runs.json"
    )(authority, work_actions._expected_claim_key(authority), None)
    repair_card = next(
        step.card for step in reversed(initial.steps) if step.phase == "build"
    )
    terminalization_failed = tuple(
        replace(step, gate_result="passed")
        if step.phase == "build" and step.card != repair_card
        else replace(step, gate_result="pending")
        if step.phase == "build"
        else step
        for step in initial.steps
    )
    for phase in ("plan", "build"):
        registry._manager_update_workflow_run(initial.run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        initial.run_id,
        steps=terminalization_failed,
        candidate_head=HEAD,
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=fixture_needs_human_reason(),
    )
    job_args = {
        "task": "wf-demo-subagent-build",
        "persona": "builder",
        "branch": "feature/demo",
        "pane": "",
        "worktree": str(tmp_path),
        "dispatch_head": HEAD,
        "workflow_run_id": initial.run_id,
        "workflow_claim_key": initial.claim_key,
        "workflow_repo": initial.repo,
        "workflow_card": repair_card,
        "workflow_phase": "build",
        "workflow_repo_root": str(tmp_path),
        "workflow_input_root": str(tmp_path),
        "source_revision": initial.source_revision,
    }
    successful_job = registry.create_job(**job_args)
    registry.update_headless_result(
        successful_job["job_id"], status="exited", exit_code=0
    )
    result = work_actions.execute_work_action(
        args={
            "action": "retry-build",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_candidate": HEAD,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )
    assert result["result"]["retry_classification"] == "orchestrator_retry"


def test_retry_build_result_carries_model_repair_after_review_needs_human(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    initial = work_actions._fallback_workflow_starter(
        registry, tmp_path / "runs.json"
    )(authority, work_actions._expected_claim_key(authority), None)
    passed = tuple(
        replace(step, gate_result="passed")
        if step.phase in {"build", "verify", "review"}
        else step
        for step in initial.steps
    )
    for phase in ("plan", "build", "verify"):
        registry._manager_update_workflow_run(initial.run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        initial.run_id,
        current_phase="review",
        steps=passed,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=fixture_needs_human_reason(),
    )
    result = work_actions.execute_work_action(
        args={
            "action": "retry-build",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_candidate": HEAD,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )
    assert result["result"]["retry_classification"] == "model_repair"


def test_enum_values_stay_in_sync_with_completion_schema() -> None:
    """completion.RETRY_CLASSIFICATION_VALUES 刻意不 import 本模組的 enum
    （避免 schema 層耦合 coordinator 高階模組），這條測試是兩處字串集合的
    唯一同步保證——#216 增修分類值時必須同時改兩處，否則在此示警。"""
    from paulsha_cortex.coordinator.completion import RETRY_CLASSIFICATION_VALUES

    assert RETRY_CLASSIFICATION_VALUES == frozenset(
        member.value for member in work_actions.RetryClassification
    )
