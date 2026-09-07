"""#828 RED coverage for the workflow execution-identity producer.

The status read model must expose the identity recorded on the registry job.
It must not derive an executor or model from the workflow phase/persona.
"""

from __future__ import annotations

import json
from pathlib import Path

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator import manager_daemon
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason


REPO = "hamanpaul/paulsha-cortex"


def _step(
    card: str,
    *,
    phase: str = "build",
    persona: str = "builder",
    executor: str | None = "planned-executor",
    model: str | None = "planned-model",
    gate_result: str = "pending",
) -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona=persona,
        card=card,
        executor=executor,
        model=model,
        domain="test-domain",
        inputs=(),
        outputs=(),
        gate_result=gate_result,
    )


def _run(
    registry: JobRegistry,
    tmp_path: Path,
    *,
    work_id: str = "workflow-execution-identity-828",
    repo: str = REPO,
    current_phase: str = "build",
    steps: tuple[WorkflowStep, ...],
    facets: tuple[str, ...] = ("needs_human",),
    gate_status: str = "failed",
):
    return registry._manager_create_workflow_run(
        work_id=work_id,
        repo=repo,
        claim_key=f"{repo}/{work_id}/{len(registry.list_workflow_runs())}",
        source_revision="a" * 64,
        workspace_root=str(tmp_path / "workspace"),
        combo="feature-oneshot",
        current_phase=current_phase,
        steps=steps,
        attempts={current_phase: 1},
        facets=facets,
        gate_status=gate_status,
        needs_human_reason=(fixture_needs_human_reason() if "needs_human" in facets else None),
    )


def _job(
    registry: JobRegistry,
    run,
    tmp_path: Path,
    *,
    card: str,
    status: str,
    executor: str | None = "codex",
    model_id: str | None = "gpt-5.3-codex",
    workflow_run_id: str | None = None,
    workflow_repo: str | None = None,
):
    job = registry.create_job(
        task=f"{run.run_id}-{card}",
        persona="builder",
        branch="feature/workflow-identity",
        pane="",
        worktree=str(tmp_path / f"worktree-{card}"),
        executor=executor,
        model_id=model_id,
        workflow_run_id=workflow_run_id or run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=workflow_repo or run.repo,
        workflow_card=card,
        workflow_phase=run.current_phase,
    )
    if status != "dispatched":
        registry.update_status(job["job_id"], status)
    return job


def test_in_flight_status_projects_the_bound_job_identity(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    job = registry.create_job(
        task="wf-identity-828-build",
        persona="builder",
        branch="feature/workflow-identity",
        pane="",
        worktree=str(tmp_path / "worktree"),
        executor="codex",
        model_id="gpt-5.3-codex",
        workflow_run_id="workflow-82800000000000000000",
        workflow_repo=REPO,
        workflow_card="build-primary",
        workflow_phase="build",
    )
    registry.update_status(job["job_id"], "running")

    rows = manager_daemon._in_flight_status(registry)

    assert rows[0]["executor"] == "codex"
    assert rows[0]["model"] == "gpt-5.3-codex"
    assert rows[0]["job_id"] == job["job_id"]
    assert rows[0]["card"] == "build-primary"
    assert rows[0]["identity_source"] == "in-flight"
    assert rows[0]["execution_state"] == "running"


def test_attention_prefers_current_card_in_flight_over_other_same_phase_cards(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        registry,
        tmp_path,
        steps=(
            _step("build-setup", gate_result="passed", executor="agy", model="planner-model"),
            _step("build-primary", executor="planned-executor", model="planned-model"),
        ),
    )
    previous = _job(
        registry, run, tmp_path, card="build-primary", status="exited",
        executor="claude", model_id="old-model",
    )
    current = _job(
        registry, run, tmp_path, card="build-primary", status="running",
        executor="codex", model_id="retry-model",
    )
    other_card = _job(
        registry, run, tmp_path, card="build-setup", status="running",
        executor="wrong-executor", model_id="wrong-model",
    )

    entry = manager.workflow_status_entry(registry, run)

    assert entry["executor"] == "codex"
    assert entry["model"] == "retry-model"
    assert entry["job_id"] == current["job_id"]
    assert entry["card"] == "build-primary"
    assert entry["identity_source"] == "in-flight"
    assert entry["execution_state"] == "running"
    assert entry["job_id"] != previous["job_id"]
    assert entry["job_id"] != other_card["job_id"]


def test_attention_uses_last_execution_after_current_card_retry_exits(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        registry,
        tmp_path,
        steps=(
            _step("build-setup", gate_result="passed"),
            _step("build-primary"),
        ),
    )
    previous = _job(
        registry, run, tmp_path, card="build-primary", status="exited",
        executor="claude", model_id="last-model",
    )

    entry = manager.workflow_status_entry(registry, run)

    assert entry["executor"] == "claude"
    assert entry["model"] == "last-model"
    assert entry["job_id"] == previous["job_id"]
    assert entry["card"] == "build-primary"
    assert entry["identity_source"] == "last-execution"
    assert entry["execution_state"] == "exited"
    assert entry["slice_state"] == "needs_human"


def test_status_does_not_fill_missing_job_identity_from_planned_step(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        registry,
        tmp_path,
        steps=(_step("build-primary", executor="planned-executor", model="planned-model"),),
    )
    job = _job(
        registry,
        run,
        tmp_path,
        card="build-primary",
        status="running",
        executor=None,
        model_id=None,
    )

    entry = manager.workflow_status_entry(registry, run)

    assert entry["job_id"] == job["job_id"]
    assert entry["card"] == "build-primary"
    assert entry["executor"] is None
    assert entry["model"] is None
    assert entry["identity_source"] == "in-flight"
    assert entry["execution_state"] == "running"


def test_unassigned_deterministic_step_is_explicitly_planned(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        registry,
        tmp_path,
        current_phase="plan",
        steps=(
            _step(
                "manager-plan",
                phase="plan",
                persona="planner",
                executor="cortex-manager",
                model="deterministic",
            ),
        ),
    )

    entry = manager.workflow_status_entry(registry, run)

    assert entry["executor"] == "cortex-manager"
    assert entry["model"] == "deterministic"
    assert entry["job_id"] is None
    assert entry["card"] == "manager-plan"
    assert entry["identity_source"] == "planned"
    assert entry["execution_state"] == "not-dispatched"


def test_no_job_and_no_planned_identity_is_unknown_not_persona_derived(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        registry,
        tmp_path,
        current_phase="claim",
        steps=(
            _step(
                "claim-work",
                phase="claim",
                persona="planner",
                executor=None,
                model=None,
            ),
        ),
    )

    entry = manager.workflow_status_entry(registry, run)

    assert entry["executor"] is None
    assert entry["model"] is None
    assert entry["job_id"] is None
    assert entry["card"] == "claim-work"
    assert entry["identity_source"] == "unknown"
    assert entry["execution_state"] == "not-dispatched"


def test_status_does_not_borrow_same_card_from_another_run_or_repo(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    target = _run(
        registry,
        tmp_path,
        work_id="same-work-id",
        repo=REPO,
        steps=(_step("build-primary", executor=None, model=None),),
    )
    foreign = _run(
        registry,
        tmp_path,
        work_id="same-work-id",
        repo="other-owner/other-repo",
        steps=(_step("build-primary", executor="foreign-plan", model="foreign-plan"),),
    )
    foreign_job = _job(
        registry,
        foreign,
        tmp_path,
        card="build-primary",
        status="running",
        executor="foreign-executor",
        model_id="foreign-model",
    )

    entry = manager.workflow_status_entry(registry, target)

    assert entry["executor"] is None
    assert entry["model"] is None
    assert entry["job_id"] is None
    assert entry["card"] == "build-primary"
    assert entry["identity_source"] == "unknown"
    assert entry["execution_state"] == "not-dispatched"
    assert entry["job_id"] != foreign_job["job_id"]


def test_runtime_status_keeps_identity_fields_across_workflow_sections(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        registry,
        tmp_path,
        steps=(_step("build-primary"),),
    )
    job = _job(
        registry,
        run,
        tmp_path,
        card="build-primary",
        status="running",
        executor="codex",
        model_id="gpt-5.3-codex",
    )
    provider = manager_daemon.build_runtime_status_provider(
        registry=registry,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        scan_specs_fn=lambda _path: [],
        ready_units_fn=lambda _metas, _predicate: [],
        now_fn=lambda: "2026-09-07T00:00:00+00:00",
    )

    status = provider()
    in_flight = next(row for row in status["in_flight"] if row["job_id"] == job["job_id"])
    attention = next(row for row in status["attention"] if row["run_id"] == run.run_id)

    for row in (in_flight, attention):
        assert row["executor"] == "codex"
        assert row["model"] == "gpt-5.3-codex"
        assert row["job_id"] == job["job_id"]
        assert row["card"] == "build-primary"
        assert row["identity_source"] == "in-flight"
        assert row["execution_state"] == "running"


def test_recent_done_projects_identity_from_the_completed_registry_job(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(
        registry,
        tmp_path,
        facets=(),
        gate_status="pending",
        steps=(_step("build-primary"),),
    )
    job = _job(
        registry,
        run,
        tmp_path,
        card="build-primary",
        status="exited",
        executor="claude",
        model_id="completed-model",
    )
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    (handoff_dir / "completed.json").write_text(
        json.dumps(
            {
                "slice_id": f"{run.run_id}-build-primary",
                "gate_status": "passed",
                "completed_at": "2026-09-07T00:00:00+00:00",
                "job_id": job["job_id"],
                "repo": REPO,
                "workflow_run_id": run.run_id,
                "workflow_card": "build-primary",
            }
        ),
        encoding="utf-8",
    )
    provider = manager_daemon.build_runtime_status_provider(
        registry=registry,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(handoff_dir),
        scan_specs_fn=lambda _path: [],
        ready_units_fn=lambda _metas, _predicate: [],
        now_fn=lambda: "2026-09-07T00:00:00+00:00",
    )

    row = provider()["recent_done"][0]

    assert row["executor"] == "claude"
    assert row["model"] == "completed-model"
    assert row["job_id"] == job["job_id"]
    assert row["card"] == "build-primary"
    assert row["identity_source"] == "last-execution"
    assert row["execution_state"] == "exited"
