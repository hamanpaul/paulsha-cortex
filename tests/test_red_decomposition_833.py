"""#833：Red 工作經拆分計畫審查後，透過標準 intake 接續一個子工作。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import manager, manager_daemon
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.planning import PlanReviewOutcome, PlanningArtifact
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority, WorkflowStep
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo

WORK_ID = "red-decomposition-833"
REPO = "example-org/paulsha-cortex"
DECOMPOSITION_CARD = "red-decomposition"


def _steps():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    compiled = compile_combo(combo, cards, "red decomposition", change=WORK_ID)
    assert compiled.workflow_manifest is not None
    return compiled.workflow_manifest.steps


def _write_planning_artifacts(root: Path) -> tuple[PlanningArtifactAuthority, ...]:
    proposal = root / f"openspec/changes/{WORK_ID}/proposal.md"
    proposal.parent.mkdir(parents=True, exist_ok=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    bodies = {
        "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nFixed.\n",
        "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nFixed.\n",
        "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nSplit.\n",
    }
    authority = []
    for kind, body in bodies.items():
        ref = f"docs/{kind}.md"
        path = root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        authority.append(
            PlanningArtifactAuthority(
                ref=ref,
                kind=kind,
                work_id=WORK_ID,
                baseline_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(authority)


def _make_run(tmp_path: Path, *, with_decomposition_step: bool = False):
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    authority = _write_planning_artifacts(repo)
    steps = list(_steps())
    if with_decomposition_step:
        steps = [
            replace(step, gate_result="passed") if step.phase == "plan" else step
            for step in steps
        ]
        steps.insert(
            next(index for index, step in enumerate(steps) if step.phase == "build"),
            WorkflowStep(
                phase="plan",
                persona="planner",
                card=DECOMPOSITION_CARD,
                executor=None,
                model=None,
                domain=None,
                inputs=tuple(item.ref for item in authority),
                outputs=(),
                skill_ref="superpowers:writing-plans",
                action="拆分 Red 工作並提出一個可進件子工作。",
            ),
        )
    run = registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="plan",
        steps=tuple(steps),
        issue_refs=(f"{REPO}#833",),
        openspec_refs=(WORK_ID,),
        pr_refs=(),
        attempts={"plan": 1},
        facets=("needs_decomposition",) if with_decomposition_step else (),
        gate_status="running",
        planning_authority=authority,
        sizing_score=8,
        sizing_band="red",
        decomposition_depth=0,
    )
    return registry, run


def _dispatcher(registry: JobRegistry):
    return type("D", (), {"_registry": registry, "_git_runner": None})()


def _no_launch(_identity):
    raise AssertionError("planner job must be reused")


def _create_terminal_planner_job(registry: JobRegistry, run) -> dict:
    step = next(item for item in run.steps if item.card == DECOMPOSITION_CARD)
    job = registry.create_job(
        task="red-decomposition-833-planner",
        persona="planner",
        branch="feature/red-decomposition-833",
        pane="",
        worktree=run.workspace_root,
        executor="codex",
        model_id="gpt-6-luna",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=step.card,
        workflow_phase=step.phase,
        workflow_repo_root=run.workspace_root,
        workflow_input_root=run.workspace_root,
        workflow_inputs=step.inputs,
        workflow_outputs=step.outputs,
        source_revision=run.source_revision,
    )
    log_path = Path(run.workspace_root).parent / "planner.jsonl"
    terminal = {
        "schema_version": 2,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": run.run_id,
        "card_id": step.card,
        "candidate": None,
        "outputs": [],
        "diagnostics": {},
        "gate_evidence": [],
    }
    log_path.write_text(json.dumps(terminal) + "\n", encoding="utf-8")
    registry.attach_launch_handle(
        job["job_id"],
        executor="codex",
        model_id="gpt-6-luna",
        session_name=job["job_id"],
        pid=1,
        log_path=str(log_path),
    )
    return registry.update_headless_result(
        job["job_id"], status="exited", exit_code=0, executor="codex", model_id="gpt-6-luna"
    )


def _reviewed_plan(job: dict) -> None:
    plan = (
        "---\n"
        "invariant_count: 1\n"
        "artifact_classes: [code, tests, documentation]\n"
        "child_work_id: accepted-child-833\n"
        "domain_breadth: 1\n"
        "state_consistency: 1\n"
        "---\n"
        "# Red decomposition\n\n"
        "## Tasks\n\n"
        "- [ ] 實作 accepted child 的 code 範圍。\n"
        "- [ ] 為 child scope 新增 tests／測試。\n"
        "- [ ] 更新 documentation／文件與 changelog。\n"
        "- [ ] 視需要補充 cli 行為說明。\n"
    )
    payload = json.loads(Path(job["log_path"]).read_text(encoding="utf-8"))
    payload["diagnostics"] = {"decomposition_plan": plan}
    Path(job["log_path"]).write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _ready_review(*_args, **_kwargs):
    return PlanReviewOutcome(
        ready=True,
        failed_check=None,
        reason=None,
        terminal=False,
        checks_run=("completeness", "contract_compatibility", "envelope"),
        observations={},
    )


def test_decomposition_plan_shape_passes_existing_plan_review_gate() -> None:
    plan = PlanningArtifact(
        kind="plan",
        ref="red-decomposition:plan-review",
        text=(
            "---\n"
            "invariant_count: 1\n"
            "artifact_classes: [code, tests, documentation]\n"
            "child_work_id: accepted-child-833\n"
            "---\n"
            "# Red decomposition\n\n"
            "## Tasks\n\n"
            "- 實作 accepted child 的 code 範圍。\n"
            "- 為 child scope 新增 tests／測試。\n"
            "- 更新 documentation／文件。\n"
            "- 記錄 changelog。\n"
            "- 視需要補充 cli 行為說明。\n"
        ),
    )

    outcome = manager._evaluate_yellow_plan_review(
        (plan,), envelope_lookup=lambda: None
    )

    assert outcome is not None and outcome.ready


def test_red_dispatch_starts_one_planner_and_reuses_it_on_resubmission(
    tmp_path: Path, monkeypatch
) -> None:
    registry, run = _make_run(tmp_path)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-5.6-luna",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            }
        ]
    )

    class _Launcher:
        def __init__(self):
            self.calls: list[dict] = []

        def as_read_only(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            self.calls.append({"slice_id": slice_id, "prompt": prompt})
            return LaunchHandle(
                executor="codex",
                model_id="gpt-5.6-luna",
                session_name=slice_id,
                pid=100,
                log_path=str(tmp_path / "planner.jsonl"),
            )

    launcher = _Launcher()
    monkeypatch.setattr(manager, "_runtime_preflight_gate", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        manager, "resolve_limiter", lambda _value: SimpleNamespace(admit=lambda _provider: None)
    )
    monkeypatch.setattr(manager, "resolve_provider", lambda **_kwargs: "codex")

    first = manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=run,
        identities=identities,
        launcher_factory=lambda _identity: launcher,
        coordinator_root=tmp_path / "coordinator",
    )

    persisted = registry.get_workflow_run(run.run_id)
    attempts = [step for step in persisted.steps if step.card == DECOMPOSITION_CARD]
    assert len(attempts) == 1, "Red 必須持久化唯一的拆分 planner step"
    assert attempts[0].persona == "planner"
    assert attempts[0].phase == "plan"
    assert attempts[0].gate_result == "pending"
    assert persisted.current_phase == "plan"
    assert persisted.facets == ("needs_decomposition",)
    assert first["job_id"]
    assert first["workflow_card"] == DECOMPOSITION_CARD
    assert len(registry.list_jobs()) == 1

    second = manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=registry.get_workflow_run(run.run_id),
        identities=identities,
        launcher_factory=lambda _identity: launcher,
        coordinator_root=tmp_path / "coordinator",
    )
    second_attempt = manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=registry.get_workflow_run(run.run_id),
        identities=identities,
        launcher_factory=lambda _identity: launcher,
        coordinator_root=tmp_path / "coordinator",
    )

    assert first["job_id"] == second["job_id"]
    assert len(launcher.calls) == 1
    assert len(registry.list_jobs()) == 1
    assert "decomposition_plan" in launcher.calls[0]["prompt"]
    assert "invariant_count" in launcher.calls[0]["prompt"]
    assert "changelog" in launcher.calls[0]["prompt"]


def test_decomposition_plan_review_failure_blocks_normal_child_intake(
    tmp_path: Path, monkeypatch
) -> None:
    registry, run = _make_run(tmp_path, with_decomposition_step=True)
    job = _create_terminal_planner_job(registry, run)
    step = next(item for item in run.steps if item.card == DECOMPOSITION_CARD)
    _reviewed_plan(job)
    monkeypatch.setattr(manager, "_malformed_workflow_card_terminal", lambda _job: False)
    monkeypatch.setattr(manager, "_retryable_nonpassing_workflow_terminal", lambda _job: False)
    monkeypatch.setattr(manager, "_explicit_stop_gate_terminal", lambda _job: None)
    monkeypatch.setattr(manager, "terminalize_workflow_job", lambda *_a, **_kw: job)
    monkeypatch.setattr(
        manager,
        "_evaluate_yellow_plan_review",
        lambda *_a, **_kw: PlanReviewOutcome(
            ready=False,
            failed_check="completeness",
            reason="missing task",
            terminal=False,
            checks_run=("completeness",),
            observations={},
        ),
    )
    intake_calls: list[str] = []

    result = manager.resume_workflow_run(
        _dispatcher(registry),
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_no_launch,
        coordinator_root=tmp_path / "coordinator",
        decomposition_intake=lambda work_id: intake_calls.append(work_id),
    )

    assert result["reason"] == "decomposition-plan-review-failed"
    assert intake_calls == []
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


def test_reviewed_child_uses_normal_intake_once_across_resume_retries(
    tmp_path: Path, monkeypatch
) -> None:
    registry, run = _make_run(tmp_path, with_decomposition_step=True)
    job = _create_terminal_planner_job(registry, run)
    step = next(item for item in run.steps if item.card == DECOMPOSITION_CARD)
    _reviewed_plan(job)
    monkeypatch.setattr(manager, "_malformed_workflow_card_terminal", lambda _job: False)
    monkeypatch.setattr(manager, "_retryable_nonpassing_workflow_terminal", lambda _job: False)
    monkeypatch.setattr(manager, "_explicit_stop_gate_terminal", lambda _job: None)
    monkeypatch.setattr(manager, "terminalize_workflow_job", lambda *_a, **_kw: job)
    monkeypatch.setattr(manager, "_evaluate_yellow_plan_review", _ready_review)
    intake_calls: list[str] = []

    def intake(work_id: str):
        intake_calls.append(work_id)
        return {"action": "claim", "run": {"run_id": "child-run-833"}}

    result = manager.resume_workflow_run(
        _dispatcher(registry),
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_no_launch,
        coordinator_root=tmp_path / "coordinator",
        decomposition_intake=intake,
    )

    assert result["reason"] == "decomposition-child-intake-started"
    assert result["child_work_id"] == "accepted-child-833"
    assert intake_calls == ["accepted-child-833"]
    assert registry.get_workflow_run(run.run_id).current_phase == "plan"
    assert registry.get_workflow_run(run.run_id).facets == ("needs_decomposition",)

    again = manager.resume_workflow_run(
        _dispatcher(registry),
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_no_launch,
        coordinator_root=tmp_path / "coordinator",
        decomposition_intake=intake,
    )
    assert again["reason"] == "no-pending-card"
    assert intake_calls == ["accepted-child-833"]


def test_manager_daemon_routes_reviewed_child_through_standard_work_intake(
    tmp_path: Path, monkeypatch
) -> None:
    registry, run = _make_run(tmp_path)
    intake_calls: list[dict] = []

    def standard_work_action(**kwargs):
        intake_calls.append(kwargs)
        return {"action": "claim", "run": {"run_id": "workflow-child-833"}}

    def resume_with_decomposition_intake(*_args, decomposition_intake=None, **_kwargs):
        assert decomposition_intake is not None
        return decomposition_intake("accepted-child-833")

    monkeypatch.setattr(manager, "resume_workflow_run", resume_with_decomposition_intake)
    monkeypatch.setattr(manager, "apply_work_action", standard_work_action)
    executor = manager_daemon.build_request_executor(
        dispatcher=_dispatcher(registry),
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
        launcher=_no_launch,
        workflow_ship_validator=lambda *_args, **_kwargs: None,
    )

    result = executor(
        build_request(
            req_type="workflow-action",
            args={"action": "resume", "run_id": run.run_id},
            requested_by="operator",
        )
    )

    assert result["run"]["run_id"] == "workflow-child-833"
    assert intake_calls == [{
        "args": {
            "action": "intake",
            "repo": run.repo,
            "work_id": "accepted-child-833",
        },
        "requested_by": "manager-daemon",
        "registry": registry,
        "runtime_factory": manager_daemon.planning_runtime.build_production_planning_runtime,
    }]
