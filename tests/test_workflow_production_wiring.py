from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.control import constants, contract
from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import (
    manager, manager_daemon, planning_runtime, registry as registry_module, review, verification,
)
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import CapabilityProbe, IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import (
    GateEvidenceRef,
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


def test_control_queue_manager_executes_heterogeneous_brainstorm_before_plan(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    candidate = "a" * 40

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
                "capabilities": ["planning"],
            },
        ]
    )
    calls: list[str] = []

    class WorkflowLauncher:
        def launch(self, *, slice_id, prompt, worktree, log_dir):
            contract_payload = json.loads(prompt.split("Contract: ", 1)[1])
            job = registry.get_job(slice_id)
            phase = contract_payload["phase"]
            card = contract_payload["card_id"]
            if phase == "plan":
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
                    "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
                    "slice_id": f"{contract_payload['run_id']}-{card}",
                    "candidate": candidate, "status": "verified", "summary": "ok",
                    "details": {"card": card},
                }
            else:
                evidence = review.build_gate_evaluation(
                    slice_id=f"{contract_payload['run_id']}-{card}",
                    state="passed", reason="accepted",
                    builder_job_id=contract_payload["builder_job_id"],
                    reviewer_job_id=slice_id, candidate=candidate,
                    launch_identity={
                        "builder": identities.require("codex", "gpt-primary").legacy_dict(),
                        "reviewer": identities.require("claude", "claude-secondary").legacy_dict(),
                    },
                )
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

    result = executor(build_request(req_type="workflow-action", args=_workflow_args(manifest_path, tmp_path), requested_by="operator"))
    run = registry.get_workflow_run(result["run_id"])

    assert calls == ["questioner", "secondary:anthropic", "integrator"]
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
    registry = JobRegistry(state_path=tmp_path / "registry.json")
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
    assert result["reason"] == "ship-validator-unavailable"

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
    trusted_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=identities,
        launcher=workflow_launcher,
        workflow_ship_validator=lambda **_: {
            "trusted": True, "status": "passed", "head": candidate, "commit_id": candidate,
            "ref": "github:copilot/current-head", "hash": "f" * 64,
        },
    )
    trusted_ship = build_request(
        req_type="workflow-action",
        args={"action": "resume", "run_id": run.run_id},
        requested_by="operator",
    )
    assert trusted_executor(trusted_ship)["current_phase"] == "ship"

    shipped = registry.get_workflow_run(run.run_id)
    assert shipped.verified_head == shipped.candidate_head == candidate
    assert {ref.kind for ref in shipped.gate_refs} == {"brainstorm", "foreign-review", "copilot"}
    assert all(
        step.executor is not None and step.domain is not None and step.gate_result == "passed"
        for step in shipped.steps if step.phase in {"claim", "define", "plan", "build", "verify", "review"}
    )
    workflow_jobs = [job for job in registry.list_jobs() if job.get("workflow_run_id") == run.run_id]
    assert len(workflow_jobs) == 7
    assert created_branches == ["feature/production-wiring"]
    assert all(job.get("workflow_evidence") for job in workflow_jobs)
    assert all(job.get("workflow_claim_key") == run.claim_key for job in workflow_jobs)
    assert all(isinstance(job.get("workflow_inputs"), list) for job in workflow_jobs)
    assert all(isinstance(job.get("workflow_outputs"), list) for job in workflow_jobs)
    assert all(not Path(job["workflow_evidence"]["path"]).is_absolute() for job in workflow_jobs)
    assert all(
        (tmp_path / job["workflow_evidence"]["path"]).is_file()
        for job in workflow_jobs
    )


def test_workflow_candidate_must_exist_at_exact_worktree_head(tmp_path: Path) -> None:
    candidate = "a" * 40
    job = {"subject_head": candidate, "worktree": str(tmp_path)}

    def missing_runner(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    with pytest.raises(ValueError, match="does not exist"):
        manager._verify_exact_candidate(job, git_runner=missing_runner)


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
            {"executor": "claude", "model_id": "reviewer", "independence_domain": "openai", "capabilities": []},
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
            task_slug="production-wiring",
            allowed_refs=("docs/superpowers/plans/*production-wiring*.md",),
        )


def test_planning_artifact_publish_is_scoped_no_clobber_and_transactional(tmp_path: Path) -> None:
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
        str(tmp_path), [plan, spec], work_id="production-wiring", task_slug="production-wiring",
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
    with pytest.raises(ValueError, match="no-clobber"):
        manager._publish_planning_artifacts(
            str(tmp_path), [plan, spec], work_id="production-wiring", task_slug="production-wiring",
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
            str(tmp_path), [other_work], work_id="production-wiring", task_slug="production-wiring",
            allowed_refs=("docs/superpowers/plans/*production-wiring*.md",),
        )


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
                gate_result="passed" if step.phase in {"verify", "review"} else step.gate_result,
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

    assert _run(phase="review", status="passed", refs=(brainstorm, foreign)).gate_status == "passed"
    assert _run(phase="ship", status="passed", refs=(brainstorm, foreign, copilot)).current_phase == "ship"
    with pytest.raises(ValueError, match="foreign-review"):
        _run(phase="review", status="passed", refs=(brainstorm,))
    with pytest.raises(ValueError, match="copilot"):
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


def test_complete_plan_does_not_require_or_launch_brainstorm(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
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
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_runtime_factory=lambda **_: (_ for _ in ()).throw(AssertionError("must not launch")),
    )

    result = executor(build_request(req_type="workflow-action", args=args, requested_by="operator"))
    run = registry.get_workflow_run(result["run_id"])
    assert result["reason"] == "planning-complete"
    assert run.brainstorm_required is False
    assert run.gate_refs == ()


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
