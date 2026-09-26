"""#833：Red 工作經拆分計畫審查後，透過標準 intake 接續一個子工作。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    assert "新的 child work item" in launcher.calls[0]["prompt"]
    assert "Current Sprint" in launcher.calls[0]["prompt"]


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


def test_reviewed_child_is_published_then_waits_for_monitor_authority(
    tmp_path: Path, monkeypatch
) -> None:
    registry, run = _make_run(tmp_path, with_decomposition_step=True)
    job = _create_terminal_planner_job(registry, run)
    _reviewed_plan(job)
    monkeypatch.setattr(manager, "_malformed_workflow_card_terminal", lambda _job: False)
    monkeypatch.setattr(manager, "_retryable_nonpassing_workflow_terminal", lambda _job: False)
    monkeypatch.setattr(manager, "_explicit_stop_gate_terminal", lambda _job: None)
    monkeypatch.setattr(manager, "terminalize_workflow_job", lambda *_a, **_kw: job)
    monkeypatch.setattr(manager, "_evaluate_yellow_plan_review", _ready_review)

    repo_root = Path(run.workspace_root)
    manifest = repo_root / ".cortex" / "work-items.yaml"
    manifest.parent.mkdir(parents=True)
    original_manifest = (
        "version: 1\n"
        "work_items:\n"
        "  unrelated-work:\n"
        "    title: 'Unrelated work'\n"
        "    links: []\n"
        "    excludes: []\n"
    )
    manifest.write_text(original_manifest, encoding="utf-8")

    monitor_confirmed = False
    intake_calls: list[str] = []
    todo_ref = "docs/superpowers/workstreams/accepted-child-833/todo.md"
    todo_path = repo_root / todo_ref

    def intake(work_id: str):
        nonlocal monitor_confirmed
        intake_calls.append(work_id)
        assert work_id == "accepted-child-833"
        payload = manager.safe_load(manifest.read_text(encoding="utf-8"))
        assert payload["work_items"]["unrelated-work"] == {
            "title": "Unrelated work",
            "links": [],
            "excludes": [],
        }
        assert payload["work_items"][work_id]["links"] == [
            {"kind": "path", "ref": todo_ref}
        ]
        assert todo_path.is_file()
        todo_text = todo_path.read_text(encoding="utf-8")
        assert "## Current Sprint" in todo_text
        assert "- [ ] 實作 accepted child 的 code 範圍。" in todo_text
        if not monitor_confirmed:
            raise ValueError("confirmed work authority missing or ambiguous")
        return {"action": "claim", "run": {"run_id": "child-run-833"}}

    kwargs = {
        "identities": IdentityRegistry.from_rows([]),
        "launcher_factory": _no_launch,
        "coordinator_root": tmp_path / "coordinator",
        "decomposition_intake": intake,
    }
    waiting = manager.resume_workflow_run(
        _dispatcher(registry), run_id=run.run_id, **kwargs
    )

    assert waiting["reason"] == "decomposition-child-awaiting-monitor"
    waiting_manifest = manifest.read_text(encoding="utf-8")
    assert waiting_manifest.startswith(original_manifest)
    assert "  accepted-child-833:" in waiting_manifest
    assert "needs_human" not in registry.get_workflow_run(run.run_id).facets
    assert registry.get_workflow_run(run.run_id).facets == ("needs_decomposition",)
    assert intake_calls == ["accepted-child-833"]

    monitor_confirmed = True
    accepted = manager.resume_workflow_run(
        _dispatcher(registry), run_id=run.run_id, **kwargs
    )

    assert accepted["reason"] == "decomposition-child-intake-started"
    assert accepted["child_work_id"] == "accepted-child-833"
    assert manifest.read_text(encoding="utf-8") == waiting_manifest
    assert intake_calls == ["accepted-child-833", "accepted-child-833"]


def test_red_child_publication_rejects_non_exact_destinations(tmp_path: Path) -> None:
    manifest_content = (
        "version: 1\n"
        "work_items:\n"
        "  child-833:\n"
        "    title: 'child-833'\n"
        "    links:\n"
        "      - kind: path\n"
        "        ref: 'docs/superpowers/workstreams/child-833/todo.md'\n"
        "    excludes: []\n"
    )
    todo_content = "# child-833\n\n## Current Sprint\n\n- [ ] Complete child work.\n"

    rollback = manager._publish_planning_artifacts(
        str(tmp_path),
        [
            {"kind": "work-item", "path": ".cortex/work-items.yaml", "content": manifest_content},
            {
                "kind": "workstream-todo",
                "path": "docs/superpowers/workstreams/child-833/todo.md",
                "content": todo_content,
            },
        ],
        work_id="child-833",
        allowed_refs=(),
    )
    assert (tmp_path / ".cortex" / "work-items.yaml").read_text(encoding="utf-8") == manifest_content
    assert (
        tmp_path / "docs/superpowers/workstreams/child-833/todo.md"
    ).read_text(encoding="utf-8") == todo_content
    rollback()

    expanded_manifest = manifest_content.replace(
        "  child-833:\n",
        "  unrelated-work:\n    title: 'unrelated-work'\n    links: []\n    excludes: []\n"
        "  child-833:\n",
    )
    with pytest.raises(ValueError, match="only add its work item"):
        manager._publish_planning_artifacts(
            str(tmp_path),
            [{"kind": "work-item", "path": ".cortex/work-items.yaml", "content": expanded_manifest}],
            work_id="child-833",
            allowed_refs=(),
        )

    with pytest.raises(ValueError, match="outside governed roots"):
        manager._publish_planning_artifacts(
            str(tmp_path),
            [
                {
                    "kind": "workstream-todo",
                    "path": "docs/superpowers/workstreams/other-child/todo.md",
                    "content": todo_content,
                }
            ],
            work_id="child-833",
            allowed_refs=(),
        )


def test_periodic_resume_supplies_standard_red_child_intake(
    tmp_path: Path, monkeypatch
) -> None:
    workflow = SimpleNamespace(
        run_id="workflow-periodic-red-child",
        work_id="red-parent",
        repo=REPO,
        status="ongoing",
        facets=("needs_decomposition",),
        current_phase="plan",
        claim_key="claim:legacy:red-parent",
        source_revision="",
    )
    registry = SimpleNamespace(
        _state_path=str(tmp_path / "jobs.json"),
        list_workflow_runs=lambda: [workflow],
    )
    dispatcher = SimpleNamespace(_registry=registry, _git_runner=lambda _args: "")
    intake_calls: list[dict] = []

    def standard_work_action(**kwargs):
        intake_calls.append(kwargs)
        return {"action": "claim", "run": {"run_id": "child-run"}}

    resume_calls: list[str] = []

    def resume_workflow(_dispatcher, **kwargs):
        resume_calls.append(kwargs["run_id"])
        assert kwargs["decomposition_intake"] is not None
        return kwargs["decomposition_intake"]("child-833")

    monkeypatch.setattr(manager_daemon.manager, "apply_work_action", standard_work_action)
    monkeypatch.setattr(manager_daemon.manager, "resume_workflow_run", resume_workflow)
    monkeypatch.setattr(
        manager_daemon.manager,
        "reconcile_planning_transactions",
        lambda **_kwargs: [],
    )
    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        run_tick_fn=lambda *_args, **_kwargs: {
            "dispatch_skipped": False,
            "dispatched": [],
            "completed": [],
            "errors": [],
            "reaped": None,
        },
        scan_specs_fn=lambda _specs_dir: [],
        auto_claim_fn=lambda: [],
        workflow_identity_registry=IdentityRegistry.from_rows([]),
    )

    runner()

    assert resume_calls == [workflow.run_id]
    assert intake_calls == [{
        "args": {"action": "intake", "repo": REPO, "work_id": "child-833"},
        "requested_by": "manager-daemon",
        "registry": registry,
        "runtime_factory": manager_daemon.planning_runtime.build_production_planning_runtime,
    }]
