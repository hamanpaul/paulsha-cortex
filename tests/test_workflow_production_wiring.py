from __future__ import annotations

from dataclasses import replace
import json
import hashlib
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from paulsha_cortex.control import constants, contract
from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import (
    manager, manager_daemon, planning_runtime, registry as registry_module, review, verification,
    work_bridge,
)
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import (
    AGY_LIVE_PROBE,
    AGY_MODEL_ID,
    CapabilityProbe,
    IdentityRegistry,
)
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import (
    GateEvidenceRef,
    PlanningArtifactAuthority,
    WorkflowManifest,
    WorkflowRun,
    WorkflowStep,
)
from paulsha_cortex.deck.compile import compile_combo, emit
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo


def _manifest() -> WorkflowManifest:
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "production wiring", change="production-wiring")
    assert result.workflow_manifest is not None
    return result.workflow_manifest


def test_feature_oneshot_manifest_has_monotonic_complete_spine_and_foreign_review_before_ship() -> None:
    manifest = _manifest()
    phases = [step.phase for step in manifest.steps]
    order = {phase: index for index, phase in enumerate(("claim", "define", "plan", "build", "verify", "review", "ship"))}

    assert phases[0] == "claim"
    assert set(phases) == set(order)
    assert [order[phase] for phase in phases] == sorted(order[phase] for phase in phases)
    first_ship = phases.index("ship")
    assert any(step.phase == "review" and step.persona == "reviewer" for step in manifest.steps[:first_ship])

    without_reviewer = WorkflowManifest(
        combo=manifest.combo,
        task_slug=manifest.task_slug,
        steps=tuple(step for step in manifest.steps if step.phase != "review"),
    )
    with pytest.raises(ValueError, match="完整 phase spine|reviewer"):
        without_reviewer.validate_manager_spine()


def test_deck_emit_persists_round_trippable_workflow_manifest(tmp_path: Path) -> None:
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "durable manifest", change="durable-manifest")

    written = emit(result, tmp_path)
    manifest_path = tmp_path / "durable-manifest.workflow.json"

    assert manifest_path in written
    assert WorkflowManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8"))) == result.workflow_manifest


def _workflow_args(manifest_path: Path, artifact_root: Path) -> dict[str, object]:
    return {
        "action": "start",
        "manifest_path": str(manifest_path),
        "work_id": "production-wiring",
        "repo": "hamanpaul/paulsha-cortex",
        "claim_key": "hamanpaul/paulsha-cortex/production-wiring/rev-a",
        "source_revision": "rev-a",
        "artifact_root": str(artifact_root),
        "planning_artifacts": [],
        "primary_executor": "codex",
        "primary_model": "gpt-primary",
        "evidence_dir": str(artifact_root / "evidence"),
    }


def test_control_queue_workflow_action_is_the_production_mutation_path(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    manifest = _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
    )

    result = executor(build_request(req_type="workflow-action", args=_workflow_args(manifest_path, tmp_path), requested_by="operator"))

    persisted = registry.get_workflow_run(result["run_id"])
    assert persisted.current_phase == "define"
    assert persisted.facets == ("needs_human",)
    assert result["reason"] == "planning-runtime-unavailable"
    assert not hasattr(registry, "create_workflow_run")
    assert not hasattr(registry, "update_workflow_run")


def test_public_work_resume_routes_through_phase_aware_poll_terminalize_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    calls: list[tuple[str, bool]] = []

    def phase_aware_resume(*args, **kwargs):
        calls.append((kwargs["run_id"], kwargs["operator_resume"]))
        return {
            "run_id": kwargs["run_id"],
            "current_phase": "build",
            "job_id": "new-build-job",
            "reason": "advanced",
        }

    monkeypatch.setattr(manager, "resume_workflow_run", phase_aware_resume)
    monkeypatch.setattr(
        manager,
        "dispatch_workflow_card",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("public resume must not dispatch without polling terminal state")
        ),
    )
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        work_action_fn=lambda **_: {
            "work_id": run.work_id,
            "repo": run.repo,
            "result": {"action": "resume", "run": run.to_dict()},
        },
    )

    result = executor(
        build_request(
            req_type="work-action",
            args={"action": "resume", "repo": run.repo, "work_id": run.work_id},
            requested_by="operator",
        )
    )

    assert calls == [(run.run_id, True)]
    assert result["result"]["current_phase"] == "build"
    assert result["result"]["job_id"] == "new-build-job"


def test_public_work_resume_preserves_define_retry_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="define",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"define": 1},
        facets=("needs_human",),
        brainstorm_required=True,
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    monkeypatch.setattr(
        manager,
        "resume_workflow_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("define retry is completed by the canonical work starter")
        ),
    )
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        work_action_fn=lambda **_: {
            "work_id": run.work_id,
            "repo": run.repo,
            "result": {
                "action": "needs_human",
                "reason": "planning-runtime-initialization-failed",
                "run": run.to_dict(),
            },
        },
    )

    result = executor(
        build_request(
            req_type="work-action",
            args={"action": "resume", "repo": run.repo, "work_id": run.work_id},
            requested_by="operator",
        )
    )

    assert result["result"]["reason"] == "planning-runtime-initialization-failed"
    assert result["result"]["run"]["current_phase"] == "define"
    assert result["result"]["run"]["facets"] == ["needs_human"]


def test_public_work_retry_build_forces_one_new_manager_dispatched_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 2},
        candidate_head="a" * 40,
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    calls: list[bool] = []

    def forced_dispatch(*args, **kwargs):
        calls.append(kwargs.get("force_new_build"))
        return {"job_id": "repair-builder"}

    monkeypatch.setattr(manager, "dispatch_workflow_card", forced_dispatch)
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
        work_action_fn=lambda **_: {
            "work_id": run.work_id,
            "repo": run.repo,
            "result": {"action": "retry-build", "run": run.to_dict()},
        },
    )

    result = executor(
        build_request(
            req_type="work-action",
            args={
                "action": "retry-build",
                "repo": run.repo,
                "work_id": run.work_id,
                "expected_candidate": "a" * 40,
            },
            requested_by="operator",
        )
    )

    assert calls == [True]
    assert result["result"]["job_id"] == "repair-builder"


def test_forced_retry_build_dispatches_new_job_after_prior_success(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "docs/superpowers/plans/production-wiring.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Repair plan\n", encoding="utf-8")
    steps = tuple(
        replace(
            step,
            gate_result=(
                "pending"
                if step.phase == "build" and step.card == "subagent-build"
                else "passed" if step.phase == "build" else step.gate_result
            ),
            action=(
                "Repair exact Candidate and commit a tested descendant."
                if step.phase == "build" and step.card == "subagent-build"
                else step.action
            ),
        )
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 2},
        candidate_head="a" * 40,
        gate_status="running",
    )
    old = registry.create_job(
        task="wf-old-subagent-build",
        persona="builder",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(tmp_path),
        dispatch_head="b" * 40,
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        subject_head="a" * 40,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        workflow_repo_root=str(tmp_path),
        workflow_input_root=str(tmp_path),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(old["job_id"], status="exited", exit_code=0)
    launched: list[str] = []

    class Launcher:
        def as_commit_required(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            launched.append(prompt)
            return LaunchHandle(
                executor="codex",
                model_id="gpt-primary",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    replacement = manager.dispatch_workflow_card(
        type("D", (), {"_registry": registry, "_git_runner": None})(),
        run=run,
        identities=IdentityRegistry.from_rows(
            [{
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            }]
        ),
        launcher_factory=lambda _: Launcher(),
        coordinator_root=tmp_path / "coordinator",
        force_new_build=True,
    )

    assert replacement["job_id"] != old["job_id"]
    assert replacement["dispatch_head"] == old["dispatch_head"]
    assert "Repair exact Candidate" in launched[0]


def test_public_work_retry_build_restores_needs_human_when_dispatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 2},
        candidate_head="a" * 40,
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    monkeypatch.setattr(
        manager,
        "dispatch_workflow_card",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("launch failed")),
    )
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
        work_action_fn=lambda **_: {
            "work_id": run.work_id,
            "repo": run.repo,
            "result": {"action": "retry-build", "run": run.to_dict()},
        },
    )

    with pytest.raises(RuntimeError, match="launch failed"):
        executor(
            build_request(
                req_type="work-action",
                args={
                    "action": "retry-build",
                    "repo": run.repo,
                    "work_id": run.work_id,
                    "expected_candidate": "a" * 40,
                },
                requested_by="operator",
            )
        )

    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)


def test_periodic_resume_does_not_clear_needs_human_or_retry(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 1},
        facets=("needs_human",),
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: (_ for _ in ()).throw(AssertionError("must not launch")),
        coordinator_root=tmp_path,
    )

    assert result["reason"] == "operator-resume-required"
    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)
    assert registry.list_jobs() == []


def test_ship_validator_failure_persists_needs_human_on_review_complete_run(
    tmp_path: Path,
) -> None:
    steps = tuple(
        WorkflowStep.from_dict({**step.to_dict(), "gate_result": "passed"})
        if step.phase != "ship"
        else step
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"review": 1},
        candidate_head="a" * 40,
        verified_head="a" * 40,
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    with pytest.raises(RuntimeError, match="preflight failed"):
        manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=IdentityRegistry.from_rows([]),
            launcher_factory=lambda _: None,
            coordinator_root=tmp_path,
            operator_resume=True,
            ship_validator=lambda **_: (_ for _ in ()).throw(
                RuntimeError("preflight failed")
            ),
        )

    stopped = registry.get_workflow_run(run.run_id)
    assert stopped.current_phase == "review"
    assert stopped.facets == ("needs_human",)
    assert stopped.gate_status == "failed"


@pytest.mark.parametrize("terminal_phase", ["merged", "done"])
def test_post_merge_closure_skips_active_planning_path_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_phase: str,
) -> None:
    steps = tuple(
        WorkflowStep.from_dict({**step.to_dict(), "gate_result": "passed"})
        if step.phase != "ship"
        else step
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    candidate = "a" * 40
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=("hamanpaul/paulsha-cortex#17",),
        attempts={"review": 1},
        candidate_head=candidate,
        verified_head=candidate,
        facets=("needs_human",),
        gate_status="failed",
        brainstorm_required=True,
    )
    (tmp_path / "delivery-journal.json").write_text(
        json.dumps(
            {
                "schema": "cortex-delivery-journal/v1",
                "runs": {
                    run.run_id: {
                        "run_id": run.run_id,
                        "repo": run.repo,
                        "work_id": run.work_id,
                        "ship": {
                            "phase": terminal_phase,
                            "head": candidate,
                            "merge_commit": "b" * 40,
                            "merge_authorization": {
                                "path": "/evidence/authorization.json",
                                "hash": "d" * 64,
                                "payload": {
                                    "run_id": run.run_id,
                                    "repo": run.repo,
                                    "work_id": run.work_id,
                                    "head": candidate,
                                },
                            },
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        manager,
        "_validated_brainstorm_planning_authority",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("post-merge closure must not require active planning paths")
        ),
    )
    calls: list[str] = []
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=tmp_path,
        operator_resume=True,
        ship_validator=lambda **_: calls.append("ship")
        or {
            "trusted": True,
            "status": "pending",
            "head": candidate,
            "commit_id": candidate,
            "ref": "delivery:merged",
            "hash": "c" * 64,
        },
    )

    assert result["reason"] == "delivery-in-progress"
    assert calls == ["ship"]
    assert registry.get_workflow_run(run.run_id).facets == ()


def test_post_merge_closure_routing_rejects_incomplete_authorization(tmp_path: Path) -> None:
    run = SimpleNamespace(
        run_id="workflow-" + "1" * 20,
        repo="hamanpaul/paulsha-cortex",
        work_id="production-wiring",
        current_phase="review",
        candidate_head="a" * 40,
    )
    (tmp_path / "delivery-journal.json").write_text(
        json.dumps(
            {
                "schema": "cortex-delivery-journal/v1",
                "runs": {
                    run.run_id: {
                        "run_id": run.run_id,
                        "repo": run.repo,
                        "work_id": run.work_id,
                        "ship": {
                            "phase": "merged",
                            "head": run.candidate_head,
                            "merge_commit": "b" * 40,
                            "merge_authorization": {
                                "path": "/evidence/authorization.json",
                                "hash": "c" * 64,
                                "payload": {
                                    "run_id": run.run_id,
                                    "repo": run.repo,
                                    "work_id": run.work_id,
                                },
                            },
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert not manager._merged_delivery_reconciliation_pending(
        run, coordinator_root=tmp_path
    )


def test_operator_resume_retries_bound_needs_human_terminal_without_rewriting_old_job(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "docs/superpowers/plans/production-wiring-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Plan\n", encoding="utf-8")
    steps = tuple(
        WorkflowStep.from_dict({
            **step.to_dict(),
            "gate_result": "passed" if step.card == "worktree-isolation" else step.gate_result,
        })
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 1},
        facets=("needs_human",),
        gate_status="running",
    )
    log = tmp_path / "needs-human.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "needs_human",
        "run_id": run.run_id,
        "card_id": "tdd-red",
        "candidate": "a" * 40,
        "outputs": [],
    }) + "\n", encoding="utf-8")
    old_job = registry.create_job(
        task="wf-tdd-red",
        persona="builder",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(tmp_path),
        dispatch_head="b" * 40,
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="tdd-red",
        workflow_phase="build",
        workflow_repo_root=str(tmp_path),
        workflow_input_root=str(tmp_path),
        source_revision=run.source_revision,
    )
    registry.attach_launch_handle(old_job["job_id"], log_path=str(log))
    registry.update_headless_result(old_job["job_id"], status="exited", exit_code=0)

    class Launcher:
        def as_commit_required(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            return LaunchHandle(
                executor="codex",
                model_id="gpt-primary",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    class ResumeDispatcher:
        _registry = registry
        _git_runner = None

        def poll_headless_done(self, job_id):
            return registry.get_job(job_id)

    identities = IdentityRegistry.from_rows([{
        "executor": "codex",
        "model_id": "gpt-primary",
        "independence_domain": "openai",
        "capabilities": [],
    }])
    stopped = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _: (_ for _ in ()).throw(AssertionError("must not launch")),
        coordinator_root=tmp_path / "coordinator",
    )
    assert stopped["reason"] == "operator-resume-required"
    assert len(registry.list_jobs()) == 1

    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "needs_human",
        "run_id": "wrong-run",
        "card_id": "tdd-red",
        "candidate": "a" * 40,
        "outputs": [],
    }) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="did not pass"):
        manager.resume_workflow_run(
            ResumeDispatcher(),
            run_id=run.run_id,
            identities=identities,
            launcher_factory=lambda _: Launcher(),
            coordinator_root=tmp_path / "coordinator",
            operator_resume=True,
        )
    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)
    assert len(registry.list_jobs()) == 1
    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "needs_human",
        "run_id": run.run_id,
        "card_id": "tdd-red",
        "candidate": "a" * 40,
        "outputs": [],
    }) + "\n", encoding="utf-8")

    result = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _: Launcher(),
        coordinator_root=tmp_path / "coordinator",
        operator_resume=True,
    )

    assert result["reason"] == "in-flight"
    assert result["job_id"] != old_job["job_id"]
    assert registry.get_job(old_job["job_id"])["status"] == "exited"
    assert registry.get_job(old_job["job_id"])["workflow_evidence"] is None
    assert registry.get_workflow_run(run.run_id).facets == ()


def test_nonpassing_terminal_retry_authority_requires_exact_schema_and_binding(
    tmp_path: Path,
) -> None:
    log = tmp_path / "terminal.jsonl"
    payload = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "needs_human",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }
    job = {
        "workflow_evidence": None,
        "status": "exited",
        "exit_code": 0,
        "workflow_phase": "build",
        "workflow_run_id": "run",
        "workflow_card": "card",
        "log_path": str(log),
    }
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    assert manager._retryable_nonpassing_workflow_terminal(job) is True

    for key, value in (
        ("schema_version", True),
        ("status", "passed"),
        ("run_id", "other-run"),
        ("card_id", "other-card"),
        ("candidate", "not-a-sha"),
        ("outputs", "not-a-list"),
    ):
        invalid = {**payload, key: value}
        log.write_text(json.dumps(invalid) + "\n", encoding="utf-8")
        assert manager._retryable_nonpassing_workflow_terminal(job) is False

    log.write_text("not-json\n", encoding="utf-8")
    assert manager._retryable_nonpassing_workflow_terminal(job) is False

    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    assert manager._retryable_nonpassing_workflow_terminal({**job, "exit_code": False}) is False


def test_operator_resume_recovers_only_exact_legacy_agy_reviewer_terminal(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "canary@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Canary"], check=True)
    (tmp_path / "README.md").write_text("legacy recovery\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    steps = tuple(
        WorkflowStep.from_dict({
            **step.to_dict(),
            "gate_result": "passed" if step.phase in {"claim", "define", "plan", "build"} else "pending",
        })
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="verify",
        steps=steps,
        candidate_head=candidate,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"verify": 1},
        facets=("needs_human",),
        gate_status="running",
    )
    builder = registry.create_job(
        task="wf-builder",
        persona="builder",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(tmp_path),
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        workflow_repo_root=str(tmp_path),
        workflow_input_root=str(tmp_path),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(builder["job_id"], status="exited", exit_code=0)
    legacy_log = tmp_path / "legacy-agy.jsonl"
    legacy_log.write_text(
        "```json\n"
        + json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-card",
                "status": "passed",
                "run_id": run.run_id,
                "card_id": "verification",
                "candidate": candidate,
                "outputs": ["reports/verify/production-wiring.md"],
            },
            indent=2,
        )
        + "\n```\n",
        encoding="utf-8",
    )
    legacy = registry.create_job(
        task="wf-verification",
        persona="reviewer",
        kind="review",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(tmp_path),
        executor="agy",
        model_id=AGY_MODEL_ID,
        independence_domain="google",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="verification",
        workflow_phase="verify",
        workflow_repo_root=str(tmp_path),
        workflow_input_root=str(tmp_path),
        workflow_outputs=("reports/verify/*production-wiring*.md",),
        source_revision=run.source_revision,
        workflow_output_baseline=(),
    )
    registry.attach_launch_handle(
        legacy["job_id"],
        executor="agy",
        model_id=AGY_MODEL_ID,
        session_name=legacy["job_id"],
        log_path=str(legacy_log),
    )
    registry.update_headless_result(legacy["job_id"], status="exited", exit_code=0)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "agy",
                "model_id": AGY_MODEL_ID,
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": AGY_LIVE_PROBE,
            },
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "claude",
                "model_id": "sonnet",
                "independence_domain": "anthropic",
                "capabilities": ["review"],
            },
        ]
    )
    launched: list[str] = []

    class Launcher:
        def as_review_only(self, *, terminal_kind):
            assert terminal_kind == "workflow-verification-result"
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            launched.append(slice_id)
            return LaunchHandle(
                executor="claude",
                model_id="sonnet",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    class ResumeDispatcher:
        _registry = registry
        _git_runner = None

        def poll_headless_done(self, job_id):
            return registry.get_job(job_id)

    verify_step = next(step for step in run.steps if step.card == "verification")
    assert manager._is_exact_legacy_agy_recovery(
        registry.get_job(legacy["job_id"]),
        run=run,
        step=verify_step,
        identities=identities,
    ), (registry.get_job(legacy["job_id"]), run.to_dict(), verify_step.to_dict())
    assert not manager._is_exact_legacy_agy_recovery(
        {
            **registry.get_job(legacy["job_id"]),
            "workflow_outputs": ["reports/verify/other.md"],
        },
        run=run,
        step=verify_step,
        identities=identities,
    )

    stopped = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: Launcher(),
        coordinator_root=tmp_path / "coordinator",
    )
    assert stopped["reason"] == "operator-resume-required"
    assert launched == []

    resumed = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: Launcher(),
        coordinator_root=tmp_path / "coordinator",
        operator_resume=True,
    )

    assert resumed["reason"] == "in-flight"
    assert resumed["job_id"] != legacy["job_id"]
    assert registry.get_job(legacy["job_id"])["workflow_evidence"] is None
    replacement = registry.get_job(resumed["job_id"])
    assert replacement["executor"] == "claude"
    assert replacement["workflow_builder_job_id"] == builder["job_id"]
    assert launched == [replacement["job_id"]]


def test_build_card_advances_candidate_only_to_exact_descendant_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "canary@example.invalid")
    git("config", "user.name", "Canary")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "tests").mkdir()
    (repo / "tests/red.py").write_text("assert False\n", encoding="utf-8")
    git("add", "tests/red.py")
    git("commit", "-qm", "red")
    candidate = git("rev-parse", "HEAD")

    steps = tuple(
        WorkflowStep.from_dict({
            **step.to_dict(),
            "gate_result": "passed" if step.card == "worktree-isolation" else step.gate_result,
        })
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
        candidate_head=base,
    )
    log = tmp_path / "tdd-red.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": run.run_id,
        "card_id": "tdd-red",
        "candidate": candidate,
        "outputs": [],
    }) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="wf-tdd-red",
        persona="builder",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(repo),
        dispatch_head=base,
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="tdd-red",
        workflow_phase="build",
        workflow_repo_root=str(repo),
        source_revision=run.source_revision,
    )
    registry.attach_launch_handle(
        job["job_id"],
        executor="codex",
        model_id="gpt-primary",
        session_name="wf-tdd-red",
        log_path=str(log),
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    terminal = manager.terminalize_workflow_job(
        registry,
        job_id=str(job["job_id"]),
        coordinator_root=tmp_path / "coordinator",
    )
    assert manager._verify_build_candidate_transition(
        terminal,
        previous_candidate=None,
    ) == candidate
    with pytest.raises(ValueError, match="baseline missing"):
        manager._verify_build_candidate_transition(
            {**terminal, "dispatch_head": None},
            previous_candidate=None,
        )
    identities = IdentityRegistry.from_rows([{
        "executor": "codex",
        "model_id": "gpt-primary",
        "independence_domain": "openai",
        "capabilities": [],
    }])

    result = manager.apply_workflow_action(
        registry,
        args={
            "action": "advance",
            "run_id": run.run_id,
            "card_id": "tdd-red",
            "job_id": terminal["job_id"],
            "current_phase": "build",
        },
        identity_registry=identities,
        coordinator_root=tmp_path / "coordinator",
        trusted_terminal=True,
    )

    updated = registry.get_workflow_run(run.run_id)
    assert result["current_phase"] == "build"
    assert updated.candidate_head == candidate
    assert next(step for step in updated.steps if step.card == "tdd-red").gate_result == "passed"

    git("checkout", "-q", "--detach", base)
    (repo / "sibling.txt").write_text("sibling\n", encoding="utf-8")
    git("add", "sibling.txt")
    git("commit", "-qm", "sibling")
    sibling = git("rev-parse", "HEAD")
    with pytest.raises(ValueError, match="not a descendant"):
        manager._verify_build_candidate_transition(
            {**terminal, "subject_head": sibling},
            previous_candidate=candidate,
        )


def test_operator_resume_reconciles_brainstorm_artifact_authority_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    coordinator_root = tmp_path / "coordinator"
    rows = {
        "spec": "docs/superpowers/specs/production-wiring-spec.md",
        "design": "docs/superpowers/specs/production-wiring-design.md",
        "plan": "docs/superpowers/plans/production-wiring-plan.md",
    }
    bodies = {
        "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
        "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nBound.\n",
        "plan": "---\nstatus: accepted\n---\n# Plan\n## Tasks\n- Build.\n",
    }
    artifact_rows = []
    for kind, ref in rows.items():
        path = workspace / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(bodies[kind], encoding="utf-8")
        artifact_rows.append(
            {"kind": kind, "ref": ref, "sha256": manager._sha256_path(path)}
        )
    evidence = coordinator_root / "evidence" / "planning" / "brainstorm.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "brainstorm-peer",
                "scope": {
                    "repo": "hamanpaul/paulsha-cortex",
                    "work_id": "production-wiring",
                    "source_revision": "2" * 64,
                },
                "artifacts": artifact_rows,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    state_path = coordinator_root / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 1},
        facets=("needs_human",),
        gate_refs=(
            GateEvidenceRef("brainstorm", str(evidence), manager._sha256_path(evidence)),
        ),
        gate_status="running",
    )
    legacy_state = json.loads(state_path.read_text(encoding="utf-8"))
    legacy_state["workflows"][0].pop("planning_source_revision")
    state_path.write_text(json.dumps(legacy_state), encoding="utf-8")
    registry = JobRegistry(state_path=state_path)
    run = registry.get_workflow_run(run.run_id)
    assert run.planning_source_revision is None
    seen: list[tuple[PlanningArtifactAuthority, ...]] = []

    def no_dispatch(_dispatcher, *, run, **_kwargs):
        seen.append(run.planning_authority)
        return None

    monkeypatch.setattr(manager, "dispatch_workflow_card", no_dispatch)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=coordinator_root,
        operator_resume=True,
    )

    assert result["reason"] == "not-dispatchable"
    assert {item.ref for item in seen[0]} == set(rows.values())
    reconciled = registry.get_workflow_run(run.run_id)
    assert reconciled.planning_authority == seen[0]
    assert reconciled.planning_source_revision == "2" * 64

    rebased = registry._manager_update_workflow_run(
        run.run_id,
        source_revision="3" * 64,
    )
    assert rebased.planning_source_revision == "2" * 64
    periodic = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=coordinator_root,
    )
    assert periodic["reason"] == "not-dispatchable"
    assert registry.get_workflow_run(run.run_id).facets == ()

    evidence.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence hash drift"):
        manager._validated_brainstorm_planning_authority(
            registry.get_workflow_run(run.run_id),
            coordinator_root=coordinator_root,
        )


def test_brainstorm_required_without_evidence_stays_stopped_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={"build": 1},
        facets=("needs_human",),
        gate_status="running",
        brainstorm_required=True,
    )
    dispatched: list[str] = []
    monkeypatch.setattr(
        manager,
        "dispatch_workflow_card",
        lambda *_args, **_kwargs: dispatched.append("called"),
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=tmp_path,
        operator_resume=True,
    )

    assert result["reason"] == "planning-authority-reconciliation-failed"
    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)
    assert dispatched == []


def test_brainstorm_authority_resolves_exact_manager_archive_after_active_path_moves(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    coordinator_root = tmp_path / "coordinator"
    ref = "openspec/changes/production-wiring/proposal.md"
    body = "---\nstatus: accepted\n---\n# Proposal\n## Requirements\nBound.\n"
    active = workspace / ref
    active.parent.mkdir(parents=True)
    active.write_text(body, encoding="utf-8")
    digest = manager._sha256_path(active)
    evidence = coordinator_root / "evidence" / "planning" / "brainstorm.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "brainstorm-peer",
                "scope": {
                    "repo": "hamanpaul/paulsha-cortex",
                    "work_id": "production-wiring",
                    "source_revision": "2" * 64,
                },
                "artifacts": [{"kind": "spec", "ref": ref, "sha256": digest}],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    archive = (
        workspace
        / "openspec"
        / "changes"
        / "archive"
        / "2026-07-19-production-wiring"
        / "proposal.md"
    )
    archive.parent.mkdir(parents=True)
    active.replace(archive)
    archive_step = replace(
        next(step for step in _manifest().steps if step.card == "openspec-archive"),
        executor="cortex-manager",
        model="deterministic",
        domain="cortex",
        gate_result="passed",
    )
    run = SimpleNamespace(
        repo="hamanpaul/paulsha-cortex",
        work_id="production-wiring",
        workspace_root=str(workspace),
        steps=(archive_step,),
        openspec_refs=("production-wiring",),
        brainstorm_required=True,
        planning_source_revision="2" * 64,
        planning_authority=(
            PlanningArtifactAuthority(
                ref=ref,
                kind="spec",
                work_id="production-wiring",
                baseline_sha256=digest,
            ),
        ),
        gate_refs=(
            GateEvidenceRef("brainstorm", str(evidence), manager._sha256_path(evidence)),
        ),
    )

    authority, source_revision = manager._validated_brainstorm_planning_authority(
        run,
        coordinator_root=coordinator_root,
    )

    assert authority == run.planning_authority
    assert source_revision == "2" * 64

    untrusted = SimpleNamespace(
        **{
            **run.__dict__,
            "steps": (replace(archive_step, executor="operator"),),
        }
    )
    with pytest.raises(ValueError, match="artifact hash drift"):
        manager._validated_brainstorm_planning_authority(
            untrusted,
            coordinator_root=coordinator_root,
        )


def test_operator_resume_dispatch_error_restores_needs_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={"build": 1},
        facets=("needs_human", "degraded"),
        gate_status="running",
    )
    monkeypatch.setattr(
        manager,
        "_dispatch_workflow_card",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("input gate failed")),
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    with pytest.raises(ValueError, match="input gate failed"):
        manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=IdentityRegistry.from_rows([]),
            launcher_factory=lambda _: None,
            coordinator_root=tmp_path,
            operator_resume=True,
        )

    assert registry.get_workflow_run(run.run_id).facets == ("degraded", "needs_human")


def test_build_input_snapshot_seeds_hash_bound_plan_and_versions_prompt(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    builder_root = tmp_path / "builder"
    plan_ref = "docs/superpowers/plans/production-wiring-plan.md"
    plan = operator_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan_bytes = b"# Accepted plan\n\nBuild the contract.\n"
    plan.write_bytes(plan_bytes)
    builder_root.mkdir()
    digest = hashlib.sha256(plan_bytes).hexdigest()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(operator_root),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
        planning_authority=(
            PlanningArtifactAuthority(
                ref=plan_ref,
                kind="plan",
                work_id="production-wiring",
                baseline_sha256=digest,
            ),
        ),
    )
    red = next(step for step in run.steps if step.card == "tdd-red")

    patterns = manager._effective_workflow_inputs(run, red)
    snapshot = manager._workflow_input_snapshot(
        run=run,
        repo_root=builder_root,
        patterns=patterns,
        coordinator_root=tmp_path / "coordinator",
    )
    payload = json.loads(
        manager._workflow_job_prompt(
            run,
            red,
            builder_job_id=None,
            coordinator_root=tmp_path / "coordinator",
            input_snapshot=snapshot,
        ).split("Contract: ", 1)[1]
    )

    assert patterns == ("docs/superpowers/plans/*production-wiring*.md",)
    assert (builder_root / plan_ref).read_bytes() == plan_bytes
    assert snapshot == (
        {
            "pattern": patterns[0],
            "path": plan_ref,
            "sha256": digest,
            "authority": "planning-authority",
            "content_ref": snapshot[0]["content_ref"],
        },
    )
    assert payload["kind"] == "workflow-card-prompt"
    assert payload["schema_version"] == 1
    assert payload["skill_ref"] == "superpowers:test-driven-development"
    assert payload["source_material"][0]["content"] == plan_bytes.decode()
    assert payload["terminal_schema"]["required"]
    assert payload["terminal_schema"]["fixed"]["run_id"] == run.run_id
    assert payload["terminal_schema"]["fixed"]["card_id"] == red.card
    assert payload["terminal_schema"]["fixed"]["outputs"] == []
    assert payload["terminal_schema"]["outputs"]["descriptive_objects_forbidden"] is True
    assert Path(snapshot[0]["content_ref"]).stat().st_mode & 0o222 == 0


def test_input_content_tamper_is_rejected_by_prompt_and_terminal_validation(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    builder_root = tmp_path / "builder"
    plan_ref = "docs/superpowers/plans/production-wiring-plan.md"
    plan = operator_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan.write_text("# Accepted\n", encoding="utf-8")
    builder_root.mkdir()
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring", repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64, source_revision="2" * 64,
        workspace_root=str(operator_root), combo="feature-oneshot", current_phase="build",
        steps=_manifest().steps, issue_refs=(), openspec_refs=("production-wiring",), pr_refs=(),
        attempts={"build": 1}, gate_status="running",
        planning_authority=(PlanningArtifactAuthority(
            ref=plan_ref, kind="plan", work_id="production-wiring", baseline_sha256=digest,
        ),),
    )
    red = next(step for step in run.steps if step.card == "tdd-red")
    coordinator_root = tmp_path / "coordinator"
    snapshot = manager._workflow_input_snapshot(
        run=run,
        repo_root=builder_root,
        patterns=manager._effective_workflow_inputs(run, red),
        coordinator_root=coordinator_root,
    )
    content_ref = Path(snapshot[0]["content_ref"])
    envelope = json.loads(content_ref.read_text(encoding="utf-8"))
    envelope["content"] = "# Tampered\n"
    content_ref.chmod(0o600)
    content_ref.write_text(json.dumps(envelope), encoding="utf-8")
    content_ref.chmod(0o444)

    with pytest.raises(ValueError, match="locator drift"):
        manager._workflow_job_prompt(
            run,
            red,
            builder_job_id=None,
            coordinator_root=coordinator_root,
            input_snapshot=snapshot,
        )
    with pytest.raises(ValueError, match="locator drift"):
        manager._validate_workflow_input_snapshot(
            builder_root,
            list(snapshot),
            coordinator_root=coordinator_root,
        )


def test_build_input_snapshot_rejects_mutable_operator_drift(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    builder_root = tmp_path / "builder"
    plan_ref = "docs/superpowers/plans/production-wiring-plan.md"
    plan = operator_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan.write_text("changed after acceptance\n", encoding="utf-8")
    builder_root.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring", repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64, source_revision="2" * 64,
        workspace_root=str(operator_root), combo="feature-oneshot", current_phase="build",
        steps=_manifest().steps, issue_refs=(), openspec_refs=("production-wiring",), pr_refs=(),
        attempts={"build": 1}, gate_status="running",
        planning_authority=(PlanningArtifactAuthority(
            ref=plan_ref, kind="plan", work_id="production-wiring", baseline_sha256="0" * 64,
        ),),
    )
    red = next(step for step in run.steps if step.card == "tdd-red")

    with pytest.raises(ValueError, match="planning input drift"):
        manager._workflow_input_snapshot(
            run=run,
            repo_root=builder_root,
            patterns=manager._effective_workflow_inputs(run, red),
            coordinator_root=tmp_path / "coordinator",
        )
    assert list(builder_root.rglob("*")) == []


def test_build_input_seed_rejects_symlinked_parent_without_outside_write(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    builder_root = tmp_path / "builder"
    outside = tmp_path / "outside"
    plan_ref = "docs/superpowers/plans/production-wiring-plan.md"
    plan = operator_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan.write_text("# Accepted\n", encoding="utf-8")
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    builder_root.mkdir()
    outside.mkdir()
    (builder_root / "docs").symlink_to(outside, target_is_directory=True)
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring", repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64, source_revision="2" * 64,
        workspace_root=str(operator_root), combo="feature-oneshot", current_phase="build",
        steps=_manifest().steps, issue_refs=(), openspec_refs=("production-wiring",), pr_refs=(),
        attempts={"build": 1}, gate_status="running",
        planning_authority=(PlanningArtifactAuthority(
            ref=plan_ref, kind="plan", work_id="production-wiring", baseline_sha256=digest,
        ),),
    )
    red = next(step for step in run.steps if step.card == "tdd-red")

    with pytest.raises(ValueError, match="symlink"):
        manager._workflow_input_snapshot(
            run=run, repo_root=builder_root,
            patterns=manager._effective_workflow_inputs(run, red),
            coordinator_root=tmp_path / "coordinator",
        )
    assert not (outside / "superpowers/plans/production-wiring-plan.md").exists()


def test_same_input_content_isolated_across_workflow_runs(tmp_path: Path) -> None:
    refs: list[str] = []
    for index in (1, 2):
        operator_root = tmp_path / f"operator-{index}"
        builder_root = tmp_path / f"builder-{index}"
        plan_ref = f"docs/superpowers/plans/work-{index}-plan.md"
        plan = operator_root / plan_ref
        plan.parent.mkdir(parents=True)
        plan.write_text("# Identical accepted content\n", encoding="utf-8")
        builder_root.mkdir()
        digest = hashlib.sha256(plan.read_bytes()).hexdigest()
        registry = JobRegistry(state_path=tmp_path / f"registry-{index}.json")
        manifest = compile_combo(
            load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", load_cards(DEFAULT_CARDS_PATH)),
            load_cards(DEFAULT_CARDS_PATH), f"work {index}", change=f"work-{index}",
        ).workflow_manifest
        run = registry._manager_create_workflow_run(
            work_id=f"work-{index}", repo="hamanpaul/paulsha-cortex",
            claim_key=f"claim:v1:{index}" + "1" * 63, source_revision=str(index) * 64,
            workspace_root=str(operator_root), combo="feature-oneshot", current_phase="build",
            steps=manifest.steps, issue_refs=(), openspec_refs=(f"work-{index}",), pr_refs=(),
            attempts={"build": 1}, gate_status="running",
            planning_authority=(PlanningArtifactAuthority(
                ref=plan_ref, kind="plan", work_id=f"work-{index}", baseline_sha256=digest,
            ),),
        )
        red = next(step for step in run.steps if step.card == "tdd-red")
        snapshot = manager._workflow_input_snapshot(
            run=run, repo_root=builder_root,
            patterns=manager._effective_workflow_inputs(run, red),
            coordinator_root=tmp_path / "coordinator",
        )
        refs.append(snapshot[0]["content_ref"])

    assert refs[0] != refs[1]
    assert all(Path(ref).is_file() for ref in refs)


def test_control_queue_manager_executes_heterogeneous_brainstorm_before_plan(tmp_path: Path) -> None:
    coordinator_dir = tmp_path.parent / f".{tmp_path.name}-coordinator"
    state_path = coordinator_dir / "registry.json"
    registry = JobRegistry(state_path=state_path)
    proposal = tmp_path / "openspec/changes/production-wiring/proposal.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "canary@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Canary"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "openspec"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def git_runner(argv, **kwargs):
        if "cat-file" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "rev-parse" in argv:
            return SimpleNamespace(returncode=0, stdout=candidate + "\n", stderr="")
        raise AssertionError(argv)

    created_branches: list[str] = []

    class WorktreeCreator:
        def create(self, branch, base_sha=None):
            created_branches.append(branch)
            return str(tmp_path)

    dispatcher = Dispatcher(
        registry, pane_sender=None, worktree_creator=WorktreeCreator(), git_runner=git_runner
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "claude",
                "model_id": "claude-secondary",
                "independence_domain": "anthropic",
                "capabilities": ["planning", "review"],
            },
        ]
    )
    calls: list[str] = []
    plan_launch_roots: list[Path] = []
    commit_capability_requests: list[str] = []
    review_capability_requests: list[str] = []
    adversarial_launches: list[str] = []

    class WorkflowLauncher:
        def as_read_only(self):
            return self

        def as_commit_required(self):
            commit_capability_requests.append("required")
            return self

        def as_review_only(self, *, terminal_kind):
            review_capability_requests.append(terminal_kind)
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            contract_payload = json.loads(prompt.split("Contract: ", 1)[1])
            job = registry.get_job(slice_id)
            phase = contract_payload["phase"]
            card = contract_payload["card_id"]
            if phase == "plan":
                plan_launch_roots.append(Path(worktree))
                evidence = {
                    "schema_version": 1, "kind": "workflow-card", "status": "passed",
                    "run_id": contract_payload["run_id"], "card_id": card,
                    "candidate": None,
                    "outputs": ["docs/superpowers/plans/production-wiring-plan.md"],
                }
            elif phase == "build":
                evidence = {
                    "schema_version": 1, "kind": "workflow-card", "status": "passed",
                    "run_id": contract_payload["run_id"], "card_id": card,
                    "candidate": candidate, "outputs": [],
                }
            elif phase == "verify":
                evidence = {
                    "schema_version": 1, "kind": "workflow-verification-result",
                    "status": "verified", "summary": "ok",
                    "details": {"card": card},
                    "reports": [{
                        "path": "reports/verify/production-wiring.md",
                        "body": "# Verification\n\nPassed.",
                    }],
                }
            else:
                suffix = "-adversarial" if card == "adversarial-review" else ""
                report_ref = f"reports/review/production-wiring{suffix}.md"
                findings = []
                if card == "adversarial-review":
                    adversarial_launches.append(slice_id)
                    if len(adversarial_launches) == 1:
                        findings = [{
                            "category": "correctness",
                            "severity": "minor",
                            "summary": "prior report omitted one sandbox-only failure file",
                            "evidence": [{
                                "path": "reports/review/production-wiring.md",
                                "line": None,
                                "detail": "the Candidate verdict is unchanged",
                            }],
                            "recommendation": "correct the enumeration in a fresh report",
                        }]
                evidence = {
                    "schema_version": 1,
                    "kind": "workflow-review-result",
                    "reason": "blocking findings" if findings else "accepted",
                    "findings": findings,
                    "reports": [{"path": report_ref, "body": "# Review\n\nPassed."}],
                }
            log_path = Path(log_dir) / f"{slice_id}.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
            log_path.with_suffix(".exit").write_text("0", encoding="utf-8")
            return LaunchHandle(
                executor=str(job["executor"]), model_id=str(job["model_id"]),
                session_name=slice_id, pid=100, log_path=str(log_path),
            )

    workflow_launcher = WorkflowLauncher()

    def questioner(report):
        calls.append("questioner")
        from paulsha_cortex.coordinator.planning import assess_planning_completeness

        return assess_planning_completeness([]).default_question_pack.to_dict()

    def secondary(pack, identity):
        calls.append(f"secondary:{identity.independence_domain}")
        return {
            "schema_version": 1,
            "question_pack_id": pack["pack_id"],
            "evidence": [
                {"question_id": row["question_id"], "claims": ["missing"], "source_refs": ["index:1"]}
                for row in pack["questions"]
            ],
        }

    def integrator(pack, evidence):
        calls.append("integrator")
        bodies = {
            "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nFixed.\n",
            "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nFixed.\n",
            "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
        }
        resolutions = []
        artifacts = []
        for row in pack["questions"]:
            kind = row["kind"].removeprefix("missing-")
            ref = (
                "docs/superpowers/plans/production-wiring-plan.md"
                if kind == "plan"
                else f"docs/superpowers/specs/production-wiring-{kind}.md"
            )
            resolutions.append(
                {"question_id": row["question_id"], "decision": "accepted", "artifact_kind": kind, "artifact_refs": [ref]}
            )
            artifacts.append({"kind": kind, "path": ref, "content": bodies[kind]})
        return {
            "schema_version": 1,
            "question_pack_id": pack["pack_id"],
            "secondary_evidence_hash": evidence["evidence_hash"],
            "resolutions": resolutions,
            "artifacts": artifacts,
        }

    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=identities,
        workflow_probes={
            ("claude", "claude-secondary"): CapabilityProbe.ready_for(
                "claude", "claude-secondary", "anthropic"
            )
        },
        workflow_primary_questioner=questioner,
        workflow_secondary_planner=secondary,
        workflow_primary_integrator=integrator,
        launcher=workflow_launcher,
    )

    workflow_args = _workflow_args(manifest_path, tmp_path)
    workflow_args["evidence_dir"] = str(coordinator_dir / "evidence")
    result = executor(build_request(
        req_type="workflow-action", args=workflow_args, requested_by="operator"
    ))
    run = registry.get_workflow_run(result["run_id"])

    assert calls == ["questioner", "secondary:anthropic", "integrator"]
    assert commit_capability_requests == []
    assert review_capability_requests == []
    assert plan_launch_roots and all(path != tmp_path for path in plan_launch_roots)
    assert run.current_phase == "plan"
    assert [ref.kind for ref in run.gate_refs] == ["brainstorm"]
    assert Path(run.gate_refs[0].ref).is_file()

    with pytest.raises(ValueError, match="rejects caller evidence"):
        executor(build_request(
            req_type="workflow-action",
            args={
                "action": "resume", "run_id": run.run_id,
                "verification_ref": {"path": "/tmp/forged", "hash": "0" * 64},
            },
            requested_by="operator",
        ))

    # Simulate daemon restart: only durable registry + job log/sentinel survive.
    registry = JobRegistry(state_path=state_path)
    dispatcher = Dispatcher(
        registry, pane_sender=None, worktree_creator=WorktreeCreator(), git_runner=git_runner
    )
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=identities,
        launcher=workflow_launcher,
    )

    periodic = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=workflow_launcher,
        workflow_identity_registry=identities,
        scan_specs_fn=lambda _: [],
        run_tick_fn=lambda *args, **kwargs: {"dispatch_skipped": False},
        auto_claim_fn=lambda: [],
    )
    periodic()
    assert registry.get_workflow_run(run.run_id).current_phase == "build"

    seen_phases = ["build"]
    for _ in range(6):
        result = executor(build_request(
            req_type="workflow-action",
            args={"action": "resume", "run_id": run.run_id},
            requested_by="operator",
        ))
        seen_phases.append(result["current_phase"])
    assert seen_phases == ["build", "build", "build", "verify", "review", "review", "review"]
    assert commit_capability_requests == ["required", "required"]
    assert review_capability_requests == [
        "workflow-verification-result",
        "workflow-review-result",
        "workflow-review-result",
    ]
    assert result["reason"] == "blocking-findings"
    blocked = registry.get_workflow_run(run.run_id)
    assert blocked.facets == ("needs_human",)
    assert blocked.gate_status == "failed"
    assert next(
        step for step in blocked.steps if step.card == "adversarial-review"
    ).gate_result == "needs_human"
    rejected_job = next(
        job
        for job in reversed(registry.list_jobs())
        if job.get("workflow_card") == "adversarial-review"
    )
    forged_job = dict(rejected_job)
    forged_job["workflow_evidence"] = {
        **rejected_job["workflow_evidence"],
        "hash": "0" * 64,
    }
    assert not manager._is_rejected_workflow_review_evidence(
        forged_job, run=blocked, coordinator_root=coordinator_dir
    )
    stale_job = {**rejected_job, "subject_head": "f" * 40}
    assert not manager._is_rejected_workflow_review_evidence(
        stale_job, run=blocked, coordinator_root=coordinator_dir
    )
    rejected_index = next(
        index
        for index, job in enumerate(registry._jobs)
        if job.get("job_id") == rejected_job["job_id"]
    )
    jobs_before_mismatch = len(registry.list_jobs())
    rejected_subject = registry._jobs[rejected_index]["subject_head"]
    registry._jobs[rejected_index]["subject_head"] = "f" * 40
    registry._persist()
    mismatch = executor(build_request(
        req_type="workflow-action",
        args={"action": "resume", "run_id": run.run_id},
        requested_by="operator",
    ))
    assert mismatch["reason"] == "rejected-review-recovery-mismatch"
    assert len(registry.list_jobs()) == jobs_before_mismatch
    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)
    assert registry.get_workflow_run(run.run_id).gate_status == "failed"
    registry._jobs[rejected_index]["subject_head"] = rejected_subject
    registry._persist()
    rejected_hash = registry._jobs[rejected_index]["workflow_evidence"]["hash"]
    registry._jobs[rejected_index]["workflow_evidence"]["hash"] = "0" * 64
    registry._persist()
    mismatch = executor(build_request(
        req_type="workflow-action",
        args={"action": "resume", "run_id": run.run_id},
        requested_by="operator",
    ))
    assert mismatch["reason"] == "rejected-review-recovery-mismatch"
    assert len(registry.list_jobs()) == jobs_before_mismatch
    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)
    assert registry.get_workflow_run(run.run_id).gate_status == "failed"
    registry._jobs[rejected_index]["workflow_evidence"]["hash"] = rejected_hash
    registry._persist()
    jobs_before_periodic = len(registry.list_jobs())
    periodic()
    assert len(registry.list_jobs()) == jobs_before_periodic
    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)

    result = executor(build_request(
        req_type="workflow-action",
        args={"action": "resume", "run_id": run.run_id},
        requested_by="operator",
    ))
    assert result["reason"] == "ship-validator-unavailable"
    assert len(adversarial_launches) == 2
    assert review_capability_requests == [
        "workflow-verification-result",
        "workflow-review-result",
        "workflow-review-result",
        "workflow-review-result",
    ]

    passed = registry.get_workflow_run(run.run_id)
    replay_steps = tuple(
        replace(step, gate_result="pending")
        if step.card == "adversarial-review"
        else step
        for step in passed.steps
    )
    registry._manager_update_workflow_run(
        run.run_id,
        steps=replay_steps,
        facets=("needs_human",),
        gate_status="running",
    )
    jobs_before_replay = len(registry.list_jobs())
    replayed = executor(build_request(
        req_type="workflow-action",
        args={"action": "resume", "run_id": run.run_id},
        requested_by="operator",
    ))
    assert replayed["reason"] == "ship-validator-unavailable"
    assert len(registry.list_jobs()) == jobs_before_replay
    assert next(
        step
        for step in registry.get_workflow_run(run.run_id).steps
        if step.card == "adversarial-review"
    ).gate_result == "passed"

    fake_ship = build_request(
        req_type="workflow-action",
        args={
            "action": "advance", "run_id": run.run_id, "card_id": "adversarial-review",
            "current_phase": "ship", "gate_refs": [{"kind": "copilot", "ref": "fake"}],
        },
        requested_by="operator",
    )
    with pytest.raises(ValueError, match="internal"):
        executor(fake_ship)
    current = registry.get_workflow_run(run.run_id)
    for card in ("openspec-archive", "policy-commit"):
        work_bridge._record_manager_ship_job(
            registry=registry,
            state_root=coordinator_dir,
            run=current,
            worktree=tmp_path,
            branch="feature/production-wiring",
            card=card,
            old_head=candidate,
            new_head=candidate,
        )
    trusted_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=identities,
        launcher=workflow_launcher,
        workflow_ship_validator=lambda **_: {
            "trusted": True, "status": "passed", "head": candidate, "commit_id": candidate,
            "ref": "github:copilot/current-head", "hash": "f" * 64,
            "completion": {
                "record_path": str(tmp_path / "evidence/completion.json"),
                "record_hash": "e" * 64,
                "record_revision": candidate,
                "source_revisions": {"openspec:production-wiring": "rev-a"},
                "pr_candidate": candidate,
                "merge_revision": "d" * 40,
            },
        },
    )
    trusted_ship = build_request(
        req_type="workflow-action",
        args={"action": "resume", "run_id": run.run_id},
        requested_by="operator",
    )
    assert trusted_executor(trusted_ship)["current_phase"] == "ship"

    shipped = registry.get_workflow_run(run.run_id)
    assert shipped.status == "done"
    assert shipped.completion_record_revision == candidate
    assert shipped.merge_revision == "d" * 40
    assert shipped.verified_head == shipped.candidate_head == candidate
    assert {ref.kind for ref in shipped.gate_refs} == {"brainstorm", "foreign-review", "copilot"}
    assert all(
        step.executor is not None and step.domain is not None and step.gate_result == "passed"
        for step in shipped.steps if step.phase in {"claim", "define", "plan", "build", "verify", "review"}
    )
    workflow_jobs = [job for job in registry.list_jobs() if job.get("workflow_run_id") == run.run_id]
    assert len(workflow_jobs) == 10
    assert {
        job.get("workflow_card")
        for job in workflow_jobs
        if job.get("workflow_phase") == "ship"
    } == {"openspec-archive", "policy-commit"}
    assert created_branches == ["feature/production-wiring"]
    assert all(job.get("workflow_evidence") for job in workflow_jobs)
    assert all(job.get("workflow_claim_key") == run.claim_key for job in workflow_jobs)
    assert all(isinstance(job.get("workflow_inputs"), list) for job in workflow_jobs)
    assert all(isinstance(job.get("workflow_outputs"), list) for job in workflow_jobs)
    assert all(isinstance(job.get("workflow_output_baseline"), list) for job in workflow_jobs)
    adversarial_jobs = [
        job for job in workflow_jobs if job.get("workflow_card") == "adversarial-review"
    ]
    assert len(adversarial_jobs) == 2
    assert any(
        row["path"] == "reports/review/production-wiring.md"
        for row in adversarial_jobs[0]["workflow_output_baseline"]
    )
    assert any(
        row["path"] == "reports/review/production-wiring-adversarial.md"
        for row in adversarial_jobs[1]["workflow_output_baseline"]
    )
    assert all(not Path(job["workflow_evidence"]["path"]).is_absolute() for job in workflow_jobs)
    assert all(
        (coordinator_dir / job["workflow_evidence"]["path"]).is_file()
        for job in workflow_jobs
    )


def test_workflow_candidate_must_exist_at_exact_worktree_head(tmp_path: Path) -> None:
    candidate = "a" * 40
    job = {"subject_head": candidate, "worktree": str(tmp_path)}

    def missing_runner(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    with pytest.raises(ValueError, match="does not exist"):
        manager._verify_exact_candidate(job, git_runner=missing_runner)


def test_ship_audit_accepts_manager_archive_ancestor_after_retry_build(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    coordinator = tmp_path / "coordinator"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "archive.txt").write_text("archived\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "archive.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "archive"], check=True)
    archive_candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "repair.txt").write_text("repair\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "repair.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "repair"], check=True)
    final_candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    registry = JobRegistry(state_path=coordinator / "jobs.json")
    steps = tuple(
        replace(
            step,
            executor="cortex-manager",
            model="deterministic",
            domain="cortex",
            gate_result="passed",
        )
        if step.phase == "ship" and step.card == "openspec-archive"
        else step
        for step in _manifest().steps
    )
    run = registry._manager_create_workflow_run(
        work_id="archive-repair",
        repo="owner/repo",
        claim_key="claim:v1:" + "a" * 64,
        source_revision="b" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        candidate_head=final_candidate,
        verified_head=final_candidate,
        gate_status="running",
    )
    work_bridge._record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        worktree=repo,
        branch="feature/archive-repair",
        card="openspec-archive",
        old_head=archive_candidate,
        new_head=archive_candidate,
    )
    run = registry._manager_update_workflow_run(
        run.run_id,
        source_revision="e" * 64,
    )
    work_bridge._record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        worktree=repo,
        branch="feature/archive-repair",
        card="policy-commit",
        old_head=final_candidate,
        new_head=final_candidate,
    )

    audited = manager._validated_ship_steps(
        registry,
        run=run,
        candidate=final_candidate,
        coordinator_root=coordinator,
    )
    assert all(
        step.gate_result == "passed"
        for step in audited
        if step.phase == "ship"
    )

    archive_tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{archive_candidate}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    sibling_candidate = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", archive_tree, "-p", archive_candidate],
        check=True,
        capture_output=True,
        text=True,
        input="unrelated sibling\n",
    ).stdout.strip()
    unrelated_root = tmp_path / "unrelated-coordinator"
    unrelated_registry = JobRegistry(state_path=unrelated_root / "jobs.json")
    unrelated_run = unrelated_registry._manager_create_workflow_run(
        work_id="unrelated-archive",
        repo="owner/repo",
        claim_key="claim:v1:" + "c" * 64,
        source_revision="d" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        candidate_head=final_candidate,
        verified_head=final_candidate,
        gate_status="running",
    )
    work_bridge._record_manager_ship_job(
        registry=unrelated_registry,
        state_root=unrelated_root,
        run=unrelated_run,
        worktree=repo,
        branch="feature/unrelated-archive",
        card="openspec-archive",
        old_head=archive_candidate,
        new_head=sibling_candidate,
    )
    work_bridge._record_manager_ship_job(
        registry=unrelated_registry,
        state_root=unrelated_root,
        run=unrelated_run,
        worktree=repo,
        branch="feature/unrelated-archive",
        card="policy-commit",
        old_head=final_candidate,
        new_head=final_candidate,
    )
    with pytest.raises(ValueError, match="openspec-archive"):
        manager._validated_ship_steps(
            unrelated_registry,
            run=unrelated_run,
            candidate=final_candidate,
            coordinator_root=unrelated_root,
        )


def test_manager_rejects_same_domain_reviewer_before_dispatch(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    candidate = "b" * 40
    steps = tuple(
        WorkflowStep(
            phase=step.phase,
            persona=step.persona,
            card=step.card,
            executor="codex" if step.phase == "build" else step.executor,
            model="builder" if step.phase == "build" else step.model,
            domain="openai" if step.phase == "build" else step.domain,
            inputs=step.inputs,
            outputs=step.outputs,
            gate_result="passed" if step.phase == "build" else step.gate_result,
        )
        for step in _manifest().steps
    )
    run = registry._manager_create_workflow_run(
        work_id="same-domain", repo="owner/repo", claim_key="owner/repo/same-domain/rev-a",
        source_revision="rev-a", workspace_root=str(tmp_path), combo="feature-oneshot", current_phase="review",
        steps=steps, candidate_head=candidate, verified_head=candidate, gate_status="running",
    )
    identities = IdentityRegistry.from_rows(
        [
            {"executor": "codex", "model_id": "builder", "independence_domain": "openai", "capabilities": []},
            {"executor": "claude", "model_id": "reviewer", "independence_domain": "openai", "capabilities": ["review"]},
        ]
    )
    review_step = next(step for step in run.steps if step.phase == "review")
    with pytest.raises(ValueError, match="no configured identity"):
        manager._select_workflow_identity(run, review_step, identities)


def test_manager_rejects_planner_artifacts_outside_governed_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside governed roots"):
        manager._publish_planning_artifacts(
            str(tmp_path),
            [{"kind": "plan", "path": "README.md", "content": "not allowed"}],
            work_id="production-wiring",
            allowed_refs=("docs/superpowers/plans/*production-wiring*.md",),
        )


def test_planning_artifact_publish_is_scoped_cas_and_transactional(tmp_path: Path) -> None:
    plan = {
        "kind": "plan",
        "path": "docs/superpowers/plans/production-wiring-plan.md",
        "content": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
    }
    spec = {
        "kind": "spec",
        "path": "docs/superpowers/specs/production-wiring-spec.md",
        "content": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
    }
    rollback = manager._publish_planning_artifacts(
        str(tmp_path), [plan, spec], work_id="production-wiring",
        allowed_refs=(
            "docs/superpowers/plans/*production-wiring*.md",
            "docs/superpowers/specs/*production-wiring*-spec.md",
        ),
    )
    assert (tmp_path / plan["path"]).is_file()
    assert (tmp_path / spec["path"]).is_file()
    rollback()
    assert not (tmp_path / plan["path"]).exists()
    assert not (tmp_path / spec["path"]).exists()

    conflict = tmp_path / spec["path"]
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_text("owned by another transaction\n", encoding="utf-8")
    with pytest.raises(ValueError, match="current planning authority"):
        manager._publish_planning_artifacts(
            str(tmp_path), [plan, spec], work_id="production-wiring",
            allowed_refs=(
                "docs/superpowers/plans/*production-wiring*.md",
                "docs/superpowers/specs/*production-wiring*-spec.md",
            ),
        )
    assert not (tmp_path / plan["path"]).exists()
    assert conflict.read_text(encoding="utf-8") == "owned by another transaction\n"

    other_work = dict(plan, path="docs/superpowers/plans/other-work-plan.md")
    with pytest.raises(ValueError, match="outside governed roots"):
        manager._publish_planning_artifacts(
            str(tmp_path), [other_work], work_id="production-wiring",
            allowed_refs=("docs/superpowers/plans/*production-wiring*.md",),
        )


def test_planning_artifact_publish_replaces_only_exact_baseline_and_rolls_back_group(
    tmp_path: Path,
) -> None:
    spec_ref = "docs/superpowers/specs/production-wiring-spec.md"
    plan_ref = "docs/superpowers/plans/production-wiring-plan.md"
    old_spec = "---\nstatus: draft\n---\n# Spec\n## Requirements\nTBD\n"
    spec_path = tmp_path / spec_ref
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(old_spec, encoding="utf-8")
    baseline = manager._sha256_path(spec_path)
    rows = [
        {
            "kind": "spec", "path": spec_ref,
            "content": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
        },
        {
            "kind": "plan", "path": plan_ref,
            "content": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
        },
    ]
    rollback = manager._publish_planning_artifacts(
        str(tmp_path), rows, work_id="production-wiring",
        allowed_refs=(
            "docs/superpowers/specs/*production-wiring*-spec.md",
            "docs/superpowers/plans/*production-wiring*.md",
        ),
        authorities=(PlanningArtifactAuthority(
            ref=spec_ref, kind="spec", work_id="production-wiring",
            baseline_sha256=baseline,
        ),),
    )
    assert "Bound." in spec_path.read_text(encoding="utf-8")
    assert (tmp_path / plan_ref).is_file()
    rollback()
    assert spec_path.read_text(encoding="utf-8") == old_spec
    assert not (tmp_path / plan_ref).exists()

    spec_path.write_text("operator changed after scan\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authority drift"):
        manager._publish_planning_artifacts(
            str(tmp_path), rows, work_id="production-wiring",
            allowed_refs=(
                "docs/superpowers/specs/*production-wiring*-spec.md",
                "docs/superpowers/plans/*production-wiring*.md",
            ),
            authorities=(PlanningArtifactAuthority(
                ref=spec_ref, kind="spec", work_id="production-wiring",
                baseline_sha256=baseline,
            ),),
        )
    assert spec_path.read_text(encoding="utf-8") == "operator changed after scan\n"
    assert not (tmp_path / plan_ref).exists()


def test_verify_terminal_evidence_cannot_substitute_for_declared_report(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    log = tmp_path / "verify.jsonl"
    payload = {
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "verified",
        "summary": "ok",
        "details": {},
        "reports": [],
    }
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="verify", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="card", workflow_phase="verify", workflow_repo_root=str(tmp_path),
        workflow_outputs=("reports/verify/work.md",), source_revision="rev",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)

    with pytest.raises(ValueError, match="non-empty list"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path
        )
    assert not (tmp_path / "evidence/workflow").exists()


def test_planner_terminalization_rejects_disposable_sandbox_pollution(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_ref = "docs/superpowers/plans/work-plan.md"
    plan = repo / plan_ref
    plan.parent.mkdir(parents=True)
    plan.write_text("# Plan\n", encoding="utf-8")
    sandbox = tmp_path / "planning-sandboxes" / ("a" * 32)
    sandbox.parent.mkdir()
    planning_runtime._copy_planning_sandbox(repo, sandbox)
    sandbox_hash = planning_runtime._tree_snapshot(sandbox)
    log = tmp_path / "plan.jsonl"
    payload = {
        "schema_version": 1, "kind": "workflow-card", "status": "passed",
        "run_id": "run", "card_id": "card", "candidate": None,
        "outputs": [plan_ref],
    }
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="plan", persona="planner", branch="feature/work", pane="",
        worktree=str(sandbox), executor="codex", model_id="planner",
        independence_domain="openai", workflow_run_id="run",
        workflow_claim_key="claim", workflow_repo="owner/repo", workflow_card="card",
        workflow_phase="plan", workflow_repo_root=str(repo), workflow_outputs=(plan_ref,),
        source_revision="rev", workflow_sandbox_hash=sandbox_hash,
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    (sandbox / "empty-pollution").mkdir()

    with pytest.raises(ValueError, match="modified disposable read-only sandbox"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path
        )
    assert not sandbox.exists()
    assert registry.get_job(job["job_id"])["workflow_evidence"] is None


def test_terminal_json_uses_codex_final_agent_message_not_turn_envelope(tmp_path: Path) -> None:
    evidence = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": None,
        "outputs": ["docs/superpowers/plans/work.md"],
    }
    log = tmp_path / "codex.jsonl"
    log.write_text(
        "\n".join(
            (
                json.dumps({
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "aggregated_output": json.dumps({"status": "fake"}),
                    },
                }),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(evidence)},
                }),
                json.dumps({"type": "turn.completed", "usage": {"output_tokens": 10}}),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert manager._extract_terminal_json(str(log)) == evidence


def test_terminal_json_reads_copilot_assistant_message_data_content(tmp_path: Path) -> None:
    evidence = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }
    log = tmp_path / "copilot.jsonl"
    log.write_text(
        "\n".join(
            (
                json.dumps({
                    "type": "assistant.message",
                    "data": {"content": json.dumps(evidence)},
                }),
                json.dumps({"type": "result", "exitCode": 0}),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert manager._extract_terminal_json(str(log)) == evidence


def test_terminal_json_rejects_copilot_non_message_data_content(tmp_path: Path) -> None:
    fake = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }
    log = tmp_path / "copilot-tool.jsonl"
    log.write_text(
        json.dumps({
            "type": "tool.execution_complete",
            "data": {"content": json.dumps(fake)},
        })
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no JSON evidence"):
        manager._extract_terminal_json(str(log))


def test_failed_planner_retry_replaces_only_its_disposable_sandbox(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.md").write_text("source\n", encoding="utf-8")
    proposal = repo / "openspec/changes/production-wiring/proposal.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator_root / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
    )
    identities = IdentityRegistry.from_rows(
        [{
            "executor": "codex",
            "model_id": "gpt-primary",
            "independence_domain": "openai",
            "capabilities": ["planning"],
        }]
    )

    class Launcher:
        def as_read_only(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            return LaunchHandle(
                executor="codex",
                model_id="gpt-primary",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    first = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=lambda _: Launcher(),
        coordinator_root=coordinator_root,
    )
    assert first["workflow_input_root"] == first["worktree"]
    first_sandbox = Path(first["worktree"])
    (first_sandbox / "failed-attempt-marker").write_text("stale\n", encoding="utf-8")
    registry.update_headless_result(first["job_id"], status="failed", exit_code=1)

    retried = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=lambda _: Launcher(),
        coordinator_root=coordinator_root,
        retry_failed=True,
    )

    assert retried["job_id"] != first["job_id"]
    assert Path(retried["worktree"]) == first_sandbox
    assert not (first_sandbox / "failed-attempt-marker").exists()
    assert (first_sandbox / "source.md").read_text(encoding="utf-8") == "source\n"


def _run(
    *, phase: str, status: str, refs: tuple[GateEvidenceRef, ...],
    brainstorm_required: bool = True,
) -> WorkflowRun:
    now = "2026-07-17T00:00:00+00:00"
    steps = _manifest().steps
    if phase == "ship":
        steps = tuple(
            WorkflowStep(
                phase=step.phase,
                persona=step.persona,
                card=step.card,
                executor="test" if step.phase in {"build", "verify", "review"} else step.executor,
                model="test-model" if step.phase in {"build", "verify", "review"} else step.model,
                domain=(
                    "openai" if step.phase == "build"
                    else "anthropic" if step.phase in {"verify", "review"}
                    else step.domain
                ),
                inputs=step.inputs,
                outputs=step.outputs,
                gate_result=(
                    "passed" if step.phase in {"verify", "review", "ship"}
                    else step.gate_result
                ),
            )
            for step in steps
        )
    return WorkflowRun(
        run_id="workflow-1",
        work_id="work-1",
        repo="owner/repo",
        claim_key="owner/repo/work-1/rev-a",
        source_revision="rev-a",
        workspace_root="/tmp/work-1",
        combo="feature-oneshot",
        current_phase=phase,
        steps=steps,
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={},
        evidence_refs=(),
        gate_refs=refs,
        brainstorm_required=brainstorm_required,
        primary_domain="openai",
        candidate_head="a" * 40 if phase == "ship" else None,
        verified_head="a" * 40 if phase == "ship" else None,
        facets=(),
        gate_status=status,
        created_at=now,
        updated_at=now,
    )


def test_workflow_gate_refs_are_typed_distinct_and_ship_requires_all_three() -> None:
    brainstorm = GateEvidenceRef("brainstorm", "evidence/brainstorm.json")
    foreign = GateEvidenceRef("foreign-review", "evidence/foreign.json")
    copilot = GateEvidenceRef("copilot", "evidence/copilot.json")
    maintainer = GateEvidenceRef("maintainer-review", "evidence/maintainer.json")

    assert _run(phase="review", status="passed", refs=(brainstorm, foreign)).gate_status == "passed"
    assert _run(phase="ship", status="passed", refs=(brainstorm, foreign, copilot)).current_phase == "ship"
    assert _run(phase="ship", status="passed", refs=(brainstorm, foreign, maintainer)).current_phase == "ship"
    with pytest.raises(ValueError, match="foreign-review"):
        _run(phase="review", status="passed", refs=(brainstorm,))
    with pytest.raises(ValueError, match="delivery review"):
        _run(phase="ship", status="passed", refs=(brainstorm, foreign))
    with pytest.raises(ValueError, match="gate_status.*passed"):
        _run(phase="ship", status="running", refs=(brainstorm, foreign, copilot))
    with pytest.raises(ValueError, match="distinct"):
        _run(
            phase="ship",
            status="passed",
            refs=(brainstorm, GateEvidenceRef("foreign-review", brainstorm.ref), copilot),
        )
    no_brainstorm = _run(
        phase="review", status="passed", refs=(foreign,), brainstorm_required=False
    )
    assert no_brainstorm.gate_status == "passed"


def test_restart_reconcile_keeps_publication_when_registry_gate_is_committed(
    tmp_path: Path,
) -> None:
    transaction = manager._PlanningPublicationTransaction(
        root=tmp_path, run_id="workflow-1", journal_root=tmp_path
    )
    artifact = tmp_path / "docs/superpowers/plans/work-1.md"
    transaction.publish(
        artifact, b"# Plan\n", baseline_hash=None, kind="artifact"
    )
    evidence = tmp_path / "evidence/brainstorm.json"
    transaction.write_evidence(
        evidence, {"schema_version": 1, "kind": "brainstorm-peer"}
    )
    digest = manager._sha256_path(evidence)
    run = _run(
        phase="plan",
        status="running",
        refs=(GateEvidenceRef("brainstorm", str(evidence), digest),),
    )

    manager._PlanningPublicationTransaction.reconcile(
        root=tmp_path, journal_root=tmp_path, run=run
    )

    assert artifact.is_file()
    assert evidence.is_file()
    assert not (tmp_path / "planning-transactions/workflow-1.json").exists()


def test_idempotent_existing_evidence_records_expected_gate_before_registry_commit(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence/brainstorm.json"
    evidence.parent.mkdir()
    evidence_payload = {"schema_version": 1, "kind": "brainstorm-peer"}
    evidence.write_text(
        json.dumps(evidence_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    evidence.chmod(0o600)
    transaction = manager._PlanningPublicationTransaction(
        root=tmp_path, run_id="workflow-1", journal_root=tmp_path
    )
    artifact = tmp_path / "docs/superpowers/plans/work-1.md"
    transaction.publish(artifact, b"# Plan\n", baseline_hash=None, kind="artifact")
    transaction.write_evidence(evidence, evidence_payload)
    journal = json.loads(
        (tmp_path / "planning-transactions/workflow-1.json").read_text(encoding="utf-8")
    )
    expected = {
        "kind": "brainstorm",
        "ref": str(evidence),
        "sha256": manager._sha256_path(evidence),
    }
    assert journal["expected_gate_ref"] == expected
    assert [row["kind"] for row in journal["operations"]] == ["artifact", "evidence"]

    run = _run(
        phase="plan", status="running",
        refs=(GateEvidenceRef("brainstorm", expected["ref"], expected["sha256"]),),
    )
    manager._PlanningPublicationTransaction.reconcile(
        root=tmp_path, journal_root=tmp_path, run=run
    )
    assert artifact.is_file()
    assert evidence.is_file()
    assert not (tmp_path / "planning-transactions/workflow-1.json").exists()


def test_idempotent_existing_evidence_rejects_noncanonical_mode(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence/brainstorm.json"
    evidence.parent.mkdir()
    payload = {"schema_version": 1, "kind": "brainstorm-peer"}
    evidence.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    evidence.chmod(0o644)
    transaction = manager._PlanningPublicationTransaction(
        root=tmp_path, run_id="workflow-1", journal_root=tmp_path
    )

    with pytest.raises(ValueError, match="immutable evidence mode conflict"):
        transaction.write_evidence(evidence, payload)

    assert not (tmp_path / "planning-transactions/workflow-1.json").exists()


def test_committed_reconcile_detects_artifact_drift_and_preserves_intent(
    tmp_path: Path,
) -> None:
    transaction = manager._PlanningPublicationTransaction(
        root=tmp_path, run_id="workflow-1", journal_root=tmp_path
    )
    artifact = tmp_path / "docs/superpowers/plans/work-1.md"
    transaction.publish(artifact, b"# Plan\n", baseline_hash=None, kind="artifact")
    evidence = tmp_path / "evidence/brainstorm.json"
    transaction.write_evidence(
        evidence, {"schema_version": 1, "kind": "brainstorm-peer"}
    )
    run = _run(
        phase="plan", status="running",
        refs=(GateEvidenceRef("brainstorm", str(evidence), manager._sha256_path(evidence)),),
    )
    artifact.write_text("operator drift\n", encoding="utf-8")

    with pytest.raises(manager.PlanningPublicationDrift, match="drift"):
        manager._PlanningPublicationTransaction.reconcile(
            root=tmp_path, journal_root=tmp_path, run=run
        )

    assert artifact.read_text(encoding="utf-8") == "operator drift\n"
    assert (tmp_path / "planning-transactions/workflow-1.json").is_file()


def test_existing_report_requires_baseline_change_and_embedded_workflow_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    report_ref = "reports/verify/work.md"
    report = tmp_path / report_ref
    report.parent.mkdir(parents=True)
    stale = (
        "---\nworkflow_run_id: run\nworkflow_card_id: card\n"
        f"candidate: {'a' * 40}\n---\n# Verification\n\nPassed.\n"
    )
    report.write_text(stale, encoding="utf-8")
    baseline = manager._sha256_path(report)
    log = tmp_path / "verify.jsonl"
    payload = {
        "schema_version": 1, "kind": "workflow-verification-result",
        "status": "verified", "summary": "ok", "details": {},
        "reports": [{"path": report_ref, "body": "# Verification\n\nPassed after this job."}],
    }
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="verify", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="card", workflow_phase="verify", workflow_repo_root=str(tmp_path),
        workflow_outputs=(report_ref,), source_revision="rev",
        workflow_output_baseline=({"path": report_ref, "sha256": baseline},),
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)

    report.write_text(stale.replace("Passed.", "Operator drift."), encoding="utf-8")
    with pytest.raises(ValueError, match="baseline CAS conflict"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path
        )

    report.write_text(stale, encoding="utf-8")
    terminal = manager.terminalize_workflow_job(
        registry, job_id=job["job_id"], coordinator_root=tmp_path
    )
    assert terminal["workflow_evidence"] is not None
    binding = manager._report_binding(report.read_bytes())
    assert binding == {
        "workflow_run_id": "run",
        "workflow_card_id": "card",
        "workflow_job_id": job["job_id"],
        "candidate": "a" * 40,
    }
    assert "Passed after this job." in report.read_text(encoding="utf-8")

    run = SimpleNamespace(
        run_id="run",
        claim_key="claim",
        repo="owner/repo",
        source_revision="rev",
        candidate_head="a" * 40,
    )
    report_bytes = report.read_bytes()
    report_hash = manager._sha256_path(report)
    report.unlink()
    with pytest.raises(ValueError, match="artifact drift"):
        manager._read_job_workflow_evidence(
            terminal,
            run=run,
            coordinator_root=tmp_path,
        )

    work_bridge._write_json_evidence(
        tmp_path,
        "report-cleanup",
        {
            "schema": "cortex-workflow-report-cleanup/v1",
            "run_id": run.run_id,
            "candidate": run.candidate_head,
            "reports": [{"path": report_ref, "sha256": report_hash}],
        },
    )
    payload, outputs, _path, _digest = manager._read_job_workflow_evidence(
        terminal,
        run=run,
        coordinator_root=tmp_path,
    )
    assert payload["status"] == "verified"
    assert outputs == (report_ref,)

    report.write_bytes(report_bytes)
    original_read_bytes = Path.read_bytes
    raced = False

    def disappear_before_read(path: Path) -> bytes:
        nonlocal raced
        if path == report and not raced:
            raced = True
            report.unlink()
            raise FileNotFoundError(report)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", disappear_before_read)
    payload, outputs, _path, _digest = manager._read_job_workflow_evidence(
        terminal,
        run=run,
        coordinator_root=tmp_path,
    )
    assert payload["status"] == "verified"
    assert outputs == (report_ref,)


def test_report_cleanup_evidence_enumeration_is_bounded_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "evidence" / "report-cleanup"
    directory.mkdir(parents=True)
    run = SimpleNamespace(run_id="run", candidate_head="a" * 40)
    original_iterdir = Path.iterdir

    def too_many_markers(path: Path):
        if path == directory:
            for index in range(2049):
                yield directory / f"{index:064x}.json"
            return
        yield from original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", too_many_markers)
    assert manager._workflow_report_cleanup_allows_missing(
        coordinator_root=tmp_path,
        run=run,
        ref="reports/verify/work.md",
        expected_hash="b" * 64,
    ) is False

    def failed_enumeration(path: Path):
        if path == directory:
            raise OSError("directory enumeration failed")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", failed_enumeration)
    assert manager._workflow_report_cleanup_allows_missing(
        coordinator_root=tmp_path,
        run=run,
        ref="reports/verify/work.md",
        expected_hash="b" * 64,
    ) is False


def test_terminal_report_manifest_cannot_authorize_arbitrary_markdown_overwrite(
    tmp_path: Path,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "state.json")
    readme = tmp_path / "README.md"
    readme.write_text("operator content\n", encoding="utf-8")
    log = tmp_path / "verify.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "verified",
        "summary": "ok",
        "details": {},
        "reports": [{"path": "README.md", "body": "replaced"}],
    }) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="verify-wide", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="verification", workflow_phase="verify",
        workflow_repo_root=str(tmp_path), workflow_outputs=("**/*.md",),
        source_revision="rev",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)

    with pytest.raises(ValueError, match="manifest root invalid"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path / "coordinator"
        )
    assert readme.read_text(encoding="utf-8") == "operator content\n"


def test_report_publication_rolls_back_when_registry_bind_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    report_ref = "reports/verify/work.md"
    log = tmp_path / "verify.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "verified",
        "summary": "ok",
        "details": {},
        "reports": [{"path": report_ref, "body": "# Verification\n\nPassed."}],
    }) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="verify-bind-fault", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="verification", workflow_phase="verify",
        workflow_repo_root=str(tmp_path), workflow_outputs=(report_ref,), source_revision="rev",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    monkeypatch.setattr(
        registry,
        "bind_workflow_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("save fault")),
    )

    with pytest.raises(OSError, match="save fault"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=coordinator
        )
    assert not (tmp_path / report_ref).exists()
    assert not list((coordinator / "workflow-report-transactions").glob("*.json"))
    assert registry.get_job(job["job_id"])["workflow_evidence"] is None


def test_multi_report_partial_write_is_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    refs = ("reports/verify/work-a.md", "reports/verify/work-b.md")
    log = tmp_path / "verify.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "verified",
        "summary": "ok",
        "details": {},
        "reports": [
            {"path": refs[0], "body": "# A"},
            {"path": refs[1], "body": "# B"},
        ],
    }) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="verify-partial", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="verification", workflow_phase="verify",
        workflow_repo_root=str(tmp_path), workflow_outputs=("reports/verify/*.md",),
        source_revision="rev",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    original = manager._PlanningPublicationTransaction._write_atomic
    failed = False

    def flaky(path, content, mode, **kwargs):
        nonlocal failed
        if path.name == "work-b.md" and not failed:
            failed = True
            raise OSError("second write fault")
        return original(path, content, mode, **kwargs)

    monkeypatch.setattr(
        manager._PlanningPublicationTransaction,
        "_write_atomic",
        staticmethod(flaky),
    )
    with pytest.raises(OSError, match="second write fault"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=coordinator
        )
    assert all(not (tmp_path / ref).exists() for ref in refs)
    assert not list((coordinator / "workflow-report-transactions").glob("*.json"))


def test_forged_report_journal_traversal_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    job = registry.create_job(
        task="verify-journal", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(repo), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="verification", workflow_phase="verify",
        workflow_repo_root=str(repo), workflow_outputs=("reports/verify/*.md",),
        source_revision="rev",
    )
    transaction = manager._WorkflowReportPublicationTransaction(
        repo_root=repo,
        coordinator_root=coordinator,
        job_id=job["job_id"],
    )
    transaction.publish(
        (("reports/verify/work.md", "# Verification"),),
        job=job,
        candidate="a" * 40,
    )
    payload = json.loads(transaction.journal_path.read_text(encoding="utf-8"))
    payload["operations"][0]["path"] = str(
        repo / "reports" / "verify" / ".." / ".." / ".." / "outside.md"
    )
    transaction.journal_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(manager.WorkflowReportPublicationDrift, match="operation invalid"):
        manager._WorkflowReportPublicationTransaction.reconcile(
            registry=registry,
            job=job,
            coordinator_root=coordinator,
        )
    assert not (tmp_path / "outside.md").exists()
    assert transaction.journal_path.is_file()


def test_reviewer_disposable_checkout_detects_candidate_mutation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "canary@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Canary"], check=True)
    readme = repo / "README.md"
    readme.write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    run = SimpleNamespace(run_id="workflow-review", candidate_head=candidate)
    step = SimpleNamespace(card="verification")
    coordinator = tmp_path / "coordinator"
    sandbox, checkout = manager._create_reviewer_sandbox(
        run=run,
        step=step,
        executor="claude",
        candidate_root=repo,
        coordinator_root=coordinator,
        input_snapshot=(),
    )
    assert sandbox != repo
    assert subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip() == candidate
    assert subprocess.run(
        ["git", "-C", str(checkout), "remote"], check=True,
        capture_output=True, text=True,
    ).stdout.strip() == ""
    assert all((sandbox / ref).is_file() for ref in manager._CLAUDE_REVIEW_PROTECTED_FILES)
    assert all((sandbox / ref).is_dir() for ref in manager._CLAUDE_REVIEW_PROTECTED_DIRS)
    assert subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain"], check=True,
        capture_output=True, text=True,
    ).stdout == ""
    assert subprocess.run(
        ["git", "-C", str(checkout), "push", "origin", "HEAD:refs/heads/forbidden"],
        capture_output=True, text=True, check=False,
    ).returncode != 0
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    job = registry.create_job(
        task="review-mutation", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(sandbox), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head=candidate,
        workflow_run_id="workflow-review", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="verification", workflow_phase="verify",
        workflow_repo_root=str(repo), workflow_input_root=str(checkout),
        workflow_sandbox_hash=manager.planning_runtime._tree_snapshot(repo),
        source_revision="rev",
    )
    readme.write_text("reviewer mutation\n", encoding="utf-8")

    with pytest.raises(ValueError, match="modified Candidate"):
        manager._discard_reviewer_sandbox(
            job,
            coordinator_root=coordinator,
            require_candidate_unchanged=True,
        )
    assert not sandbox.exists()


def test_operator_resume_replaces_exact_bound_reviewer_without_terminal_json(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "canary@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Canary"], check=True
    )
    (repo / "README.md").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    steps = tuple(
        WorkflowStep.from_dict(
            {
                **step.to_dict(),
                "gate_result": (
                    "passed"
                    if step.phase in {"claim", "define", "plan", "build"}
                    else "pending"
                ),
            }
        )
        for step in _manifest().steps
    )
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace),
        combo="feature-oneshot",
        current_phase="verify",
        steps=steps,
        candidate_head=candidate,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"verify": 1},
        facets=("needs_human",),
        gate_status="running",
    )
    builder = registry.create_job(
        task="wf-builder",
        persona="builder",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(repo),
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        workflow_repo_root=str(repo),
        workflow_input_root=str(repo),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(builder["job_id"], status="exited", exit_code=0)
    verify_step = next(step for step in run.steps if step.card == "verification")
    sandbox, checkout = manager._create_reviewer_sandbox(
        run=run,
        step=verify_step,
        executor="codex",
        candidate_root=repo,
        coordinator_root=coordinator,
        input_snapshot=(),
    )
    log_root = coordinator / "logs" / "workflow"
    log_root.mkdir(parents=True)
    legacy = registry.create_job(
        task="wf-verification",
        persona="reviewer",
        kind="review",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(sandbox),
        executor="claude",
        model_id="sonnet",
        independence_domain="anthropic",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=verify_step.card,
        workflow_phase=verify_step.phase,
        workflow_repo_root=str(repo),
        workflow_input_root=str(checkout),
        workflow_outputs=verify_step.outputs,
        workflow_output_baseline=(),
        workflow_sandbox_hash=manager.planning_runtime._tree_snapshot(repo),
        workflow_builder_job_id=builder["job_id"],
        source_revision=run.source_revision,
    )
    log = log_root / f"{legacy['job_id']}.jsonl"
    log.write_text(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Plan Mode prevented tests; no terminal JSON was produced.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry.attach_launch_handle(
        legacy["job_id"],
        executor="claude",
        model_id="sonnet",
        session_name=legacy["job_id"],
        log_path=str(log),
    )
    registry.update_headless_result(legacy["job_id"], status="exited", exit_code=0)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "claude",
                "model_id": "sonnet",
                "independence_domain": "anthropic",
                "capabilities": ["review"],
            },
        ]
    )
    bound = registry.get_job(legacy["job_id"])
    assert manager._is_exact_reviewer_terminal_recovery(
        registry,
        bound,
        run=run,
        step=verify_step,
        identities=identities,
        coordinator_root=coordinator,
    )
    assert not manager._is_exact_reviewer_terminal_recovery(
        registry,
        {**bound, "subject_head": "f" * 40},
        run=run,
        step=verify_step,
        identities=identities,
        coordinator_root=coordinator,
    )
    assert not manager._is_exact_reviewer_terminal_recovery(
        registry,
        {**bound, "worktree": str(sandbox.with_name("0" * 32))},
        run=run,
        step=verify_step,
        identities=identities,
        coordinator_root=coordinator,
    )
    assert not manager._is_exact_reviewer_terminal_recovery(
        registry,
        {**bound, "workflow_input_root": str(repo)},
        run=run,
        step=verify_step,
        identities=identities,
        coordinator_root=coordinator,
    )
    assert not manager._is_exact_reviewer_terminal_recovery(
        registry,
        {**bound, "workflow_repo_root": str(workspace)},
        run=run,
        step=verify_step,
        identities=identities,
        coordinator_root=coordinator,
    )

    launched: list[tuple[str, str, str]] = []

    class Launcher:
        def as_review_only(self, *, terminal_kind):
            assert terminal_kind == "workflow-verification-result"
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            launched.append((slice_id, prompt, worktree))
            return LaunchHandle(
                executor="claude",
                model_id="sonnet",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    class ResumeDispatcher:
        _registry = registry
        _git_runner = None

        def poll_headless_done(self, job_id):
            return registry.get_job(job_id)

    stopped = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: Launcher(),
        coordinator_root=coordinator,
    )
    assert stopped["reason"] == "operator-resume-required"
    assert launched == []

    resumed = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: Launcher(),
        coordinator_root=coordinator,
        operator_resume=True,
    )
    assert resumed["reason"] == "in-flight"
    assert resumed["job_id"] != legacy["job_id"]
    assert [row[0] for row in launched] == [resumed["job_id"]]
    assert '"candidate_checkout": "candidate"' in launched[0][1]
    assert launched[0][2] == str(sandbox)
    replacement = registry.get_job(resumed["job_id"])
    assert Path(replacement["worktree"]) == sandbox
    assert Path(replacement["workflow_input_root"]) == sandbox / "candidate"
    assert (sandbox / "candidate").is_dir()
    assert registry.get_job(legacy["job_id"])["workflow_evidence"] is None


def test_review_terminal_rejects_non_builder_job_binding_before_publication(
    tmp_path: Path,
) -> None:
    candidate = "a" * 40
    steps = tuple(
        WorkflowStep.from_dict({
            **step.to_dict(),
            "gate_result": "passed" if step.phase in {"claim", "define", "plan", "build", "verify"} else "pending",
        })
        for step in _manifest().steps
    )
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring", repo="owner/repo",
        claim_key="claim:v1:" + "1" * 64, source_revision="2" * 64,
        workspace_root=str(tmp_path), combo="feature-oneshot", current_phase="review",
        steps=steps, issue_refs=(), openspec_refs=(), pr_refs=(),
        attempts={"review": 1}, candidate_head=candidate, verified_head=candidate,
        gate_status="running",
    )
    invalid_builder = registry.create_job(
        task="invalid-builder", persona="manager", kind="build", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="codex", model_id="builder",
        independence_domain="openai", subject_head=candidate,
        workflow_run_id=run.run_id, workflow_claim_key=run.claim_key,
        workflow_repo=run.repo, workflow_card="subagent-build", workflow_phase="build",
        workflow_repo_root=str(tmp_path), source_revision=run.source_revision,
    )
    registry.update_headless_result(invalid_builder["job_id"], status="exited", exit_code=0)
    report_ref = "reports/review/production-wiring.md"
    log = tmp_path / "review.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1, "kind": "workflow-review-result", "reason": "accepted",
        "findings": [], "reports": [{"path": report_ref, "body": "# Review"}],
    }) + "\n", encoding="utf-8")
    review_job = registry.create_job(
        task="review-invalid-builder", persona="reviewer", kind="review",
        branch="feature/work", pane="", worktree=str(tmp_path), executor="claude",
        model_id="reviewer", independence_domain="anthropic", subject_head=candidate,
        workflow_run_id=run.run_id, workflow_claim_key=run.claim_key,
        workflow_repo=run.repo, workflow_card="code-review", workflow_phase="review",
        workflow_repo_root=str(tmp_path), workflow_outputs=(report_ref,),
        workflow_builder_job_id=invalid_builder["job_id"], source_revision=run.source_revision,
    )
    registry.attach_launch_handle(review_job["job_id"], log_path=str(log))
    registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)

    with pytest.raises(ValueError, match="builder binding mismatch: persona"):
        manager.terminalize_workflow_job(
            registry, job_id=review_job["job_id"], coordinator_root=coordinator
        )
    assert not (tmp_path / report_ref).exists()


def test_planning_replacement_requires_persisted_authority_not_caller_hash(
    tmp_path: Path,
) -> None:
    ref = "docs/superpowers/specs/production-wiring-spec.md"
    path = tmp_path / ref
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nstatus: draft\n---\n# Spec\n## Requirements\nTBD\n",
        encoding="utf-8",
    )
    authority = PlanningArtifactAuthority(
        ref=ref, kind="spec", work_id="production-wiring",
        baseline_sha256=manager._sha256_path(path),
    )
    replacement = {
        "kind": "spec", "path": ref,
        "content": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
    }
    rollback = manager._publish_planning_artifacts(
        str(tmp_path), [replacement], work_id="production-wiring",
        allowed_refs=("docs/superpowers/specs/*production-wiring*-spec.md",),
        authorities=(authority,),
    )
    rollback()

    forged = PlanningArtifactAuthority(
        ref=ref, kind="design", work_id="production-wiring",
        baseline_sha256=authority.baseline_sha256,
    )
    with pytest.raises(ValueError, match="current planning authority"):
        manager._publish_planning_artifacts(
            str(tmp_path), [replacement], work_id="production-wiring",
            allowed_refs=("docs/superpowers/specs/*production-wiring*-spec.md",),
            authorities=(forged,),
        )


def test_complete_plan_does_not_require_or_launch_brainstorm(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
    proposal = tmp_path / "openspec/changes/production-wiring/proposal.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    bodies = {
        "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nFixed.\n",
        "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nFixed.\n",
        "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
    }
    rows = []
    for kind, body in bodies.items():
        ref = f"docs/{kind}.md"
        path = tmp_path / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        rows.append({"kind": kind, "ref": ref})
    args = _workflow_args(manifest_path, tmp_path)
    args["planning_artifacts"] = rows
    args["primary_domain"] = "openai"
    launched: list[str] = []

    class Launcher:
        def as_read_only(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            launched.append(slice_id)
            return LaunchHandle(
                executor="test",
                model_id="test",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=Launcher(),
        workflow_runtime_factory=lambda **_: (_ for _ in ()).throw(AssertionError("must not launch")),
    )

    result = executor(build_request(req_type="workflow-action", args=args, requested_by="operator"))
    run = registry.get_workflow_run(result["run_id"])
    assert result["reason"] == "planning-complete"
    assert launched == [result["job_id"]]
    assert run.brainstorm_required is False
    assert run.gate_refs == ()
    assert {
        (authority.ref, authority.kind, authority.work_id, authority.baseline_sha256)
        for authority in run.planning_authority
    } == {
        (row["ref"], row["kind"], "production-wiring", manager._sha256_path(tmp_path / row["ref"]))
        for row in rows
    }


@pytest.mark.parametrize("commit_before_error", [False, True])
def test_brainstorm_publication_reconciles_registry_commit_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit_before_error: bool,
) -> None:
    state_path = tmp_path / "registry.json"
    registry = JobRegistry(state_path=state_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "primary",
                "independence_domain": "openai", "capabilities": ["planning"],
            },
            {
                "executor": "claude", "model_id": "secondary",
                "independence_domain": "anthropic", "capabilities": ["planning"],
            },
        ]
    )

    def questioner(report):
        from paulsha_cortex.coordinator.planning import assess_planning_completeness

        return assess_planning_completeness([]).default_question_pack.to_dict()

    def secondary(pack, identity):
        return {
            "schema_version": 1,
            "question_pack_id": pack["pack_id"],
            "evidence": [
                {"question_id": row["question_id"], "claims": ["missing"], "source_refs": ["scan:1"]}
                for row in pack["questions"]
            ],
        }

    def integrator(pack, evidence):
        bodies = {
            "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
            "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nBound.\n",
            "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
        }
        refs = {
            "spec": "docs/superpowers/specs/production-wiring-spec.md",
            "design": "docs/superpowers/specs/production-wiring-design.md",
            "plan": "docs/superpowers/plans/production-wiring-plan.md",
        }
        resolutions = []
        artifacts = []
        for row in pack["questions"]:
            kind = row["kind"].removeprefix("missing-")
            resolutions.append(
                {
                    "question_id": row["question_id"], "decision": "accepted",
                    "artifact_kind": kind, "artifact_refs": [refs[kind]],
                }
            )
            artifacts.append({"kind": kind, "path": refs[kind], "content": bodies[kind]})
        return {
            "schema_version": 1, "question_pack_id": pack["pack_id"],
            "secondary_evidence_hash": evidence["evidence_hash"],
            "resolutions": resolutions, "artifacts": artifacts,
        }

    args = _workflow_args(manifest_path, tmp_path)
    args.update({"primary_model": "primary"})
    real_write = registry._write_payload_atomically
    failed = False

    def fail_plan_transition(payload):
        nonlocal failed
        if not failed and any(
            row.get("current_phase") == "plan" and row.get("gate_refs")
            for row in payload.get("workflows", [])
        ):
            failed = True
            if commit_before_error:
                real_write(payload)
            raise OSError("registry save fault")
        real_write(payload)

    monkeypatch.setattr(registry, "_write_payload_atomically", fail_plan_transition)
    with pytest.raises(OSError, match="registry save fault"):
        manager.apply_workflow_action(
            registry, args=args, identity_registry=identities,
            probes={
                ("claude", "secondary"): CapabilityProbe.ready_for(
                    "claude", "secondary", "anthropic"
                )
            },
            primary_questioner=questioner, secondary_planner=secondary,
            primary_integrator=integrator, coordinator_root=tmp_path,
        )

    if commit_before_error:
        assert registry.list_workflow_runs()[0].current_phase == "plan"
        assert (tmp_path / "docs/superpowers").is_dir()
        assert list((tmp_path / "evidence").glob("brainstorm-*.json"))
        assert not list((tmp_path / "planning-transactions").glob("*.json"))
    else:
        assert registry.list_workflow_runs()[0].current_phase == "define"
        assert not (tmp_path / "docs/superpowers").exists()
        assert not list((tmp_path / "evidence").glob("brainstorm-*.json"))

    restarted = JobRegistry(state_path=state_path)
    result = manager.apply_workflow_action(
        restarted, args=args, identity_registry=identities,
        probes={
            ("claude", "secondary"): CapabilityProbe.ready_for(
                "claude", "secondary", "anthropic"
            )
        },
        primary_questioner=questioner, secondary_planner=secondary,
        primary_integrator=integrator, coordinator_root=tmp_path,
    )
    assert result["reason"] == (
        "already-claimed" if commit_before_error else "brainstorm-complete"
    )
    assert restarted.get_workflow_run(result["run_id"]).current_phase == "plan"
    assert {
        authority.ref
        for authority in restarted.get_workflow_run(result["run_id"]).planning_authority
    } == {
        "docs/superpowers/specs/production-wiring-spec.md",
        "docs/superpowers/specs/production-wiring-design.md",
        "docs/superpowers/plans/production-wiring-plan.md",
    }
    assert not list((tmp_path / "planning-transactions").glob("*.json"))


def test_manager_rejects_forged_persona_spine(tmp_path: Path) -> None:
    manifest = _manifest()
    bad_steps = tuple(
        WorkflowStep(
            phase=step.phase,
            persona="builder" if step.phase == "review" else step.persona,
            card=step.card,
            executor=step.executor,
            model=step.model,
            domain=step.domain,
            inputs=step.inputs,
            outputs=step.outputs,
            gate_result=step.gate_result,
        )
        for step in manifest.steps
    )
    forged = WorkflowManifest(combo=manifest.combo, task_slug=manifest.task_slug, steps=bad_steps)
    with pytest.raises(ValueError, match="review.*reviewer"):
        forged.validate_manager_spine()


def test_run_loop_workflow_request_calls_production_runtime_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    control_root = tmp_path / "control"
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(control_root))
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
    args = _workflow_args(manifest_path, tmp_path)
    request = build_request(req_type="workflow-action", args=args, requested_by="operator")
    contract.atomic_write_json(constants.requests_dir() / f"{request['req_id']}.json", request)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "gpt-primary",
                "independence_domain": "openai", "capabilities": ["planning"],
            }
        ]
    )
    calls: list[tuple[tuple[str, str], Path]] = []

    def factory(*, primary, worktree):
        calls.append((primary, Path(worktree)))
        return planning_runtime.ProductionPlanningRuntime(
            identities,
            {},
            lambda report: {},
            lambda pack, identity: {},
            lambda pack, evidence: {},
        )

    monkeypatch.setattr(planning_runtime, "build_production_planning_runtime", factory)
    started = manager_daemon.run_loop(
        poll_interval=0,
        tick_interval=300,
        monotonic_fn=lambda: 0,
        sleep_fn=lambda _: None,
        max_rounds=1,
        registry=registry,
        dispatcher=dispatcher,
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
    )
    done = contract.read_json(constants.done_dir() / f"{request['req_id']}.json")

    assert started is True
    assert calls == [(('codex', 'gpt-primary'), tmp_path)]
    assert done and done["status"] == "ok"
    assert done["result"]["reason"] == "no-heterogeneous-planner"


def test_registry_restores_file_and_memory_when_directory_fsync_fails_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "registry.json"
    registry = JobRegistry(state_path=state)
    registry.create_job(task="baseline", persona="builder", branch="feature/base", pane="%0", worktree="/wt/base")
    original = state.read_bytes()
    calls = 0
    real_fsync = registry_module._fsync_directory

    def fail_once(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("directory fsync fault")
        real_fsync(path)

    monkeypatch.setattr(registry_module, "_fsync_directory", fail_once)
    with pytest.raises(OSError, match="directory fsync fault"):
        registry.create_job(task="new", persona="builder", branch="feature/new", pane="%1", worktree="/wt/new")

    assert state.read_bytes() == original
    assert [job["task"] for job in registry.list_jobs()] == ["baseline"]
    assert calls >= 2
