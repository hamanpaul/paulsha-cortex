from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from paulsha_cortex.control import constants, contract
from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import manager_daemon, planning_runtime, registry as registry_module
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


def _write_evidence(root: Path, name: str, payload: dict[str, object]) -> dict[str, str]:
    path = root / name
    content = (json.dumps(payload, sort_keys=True) + "\n").encode()
    path.write_bytes(content)
    return {"ref": name, "sha256": hashlib.sha256(content).hexdigest()}


def _phase_payload(
    *, work_id: str, phase: str, persona: str, kind: str, candidate: str | None,
    domain: str, outputs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": kind,
        "status": "passed",
        "work_id": work_id,
        "phase": phase,
        "persona": persona,
        "executor": "codex" if domain == "openai" else "claude",
        "model": "test-model",
        "independence_domain": domain,
        "candidate_head": candidate,
        "outputs": outputs or [],
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
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
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
        for row in pack["questions"]:
            kind = row["kind"].removeprefix("missing-")
            ref = f"docs/{kind}.md"
            path = tmp_path / ref
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(bodies[kind], encoding="utf-8")
            resolutions.append(
                {"question_id": row["question_id"], "decision": "accepted", "artifact_kind": kind, "artifact_refs": [ref]}
            )
        return {
            "schema_version": 1,
            "question_pack_id": pack["pack_id"],
            "secondary_evidence_hash": evidence["evidence_hash"],
            "resolutions": resolutions,
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
    )

    result = executor(build_request(req_type="workflow-action", args=_workflow_args(manifest_path, tmp_path), requested_by="operator"))
    run = registry.get_workflow_run(result["run_id"])

    assert calls == ["questioner", "secondary:anthropic", "integrator"]
    assert run.current_phase == "plan"
    assert [ref.kind for ref in run.gate_refs] == ["brainstorm"]
    assert Path(run.gate_refs[0].ref).is_file()

    skip = build_request(
        req_type="workflow-action",
        args={"action": "advance", "run_id": run.run_id, "current_phase": "verify"},
        requested_by="operator",
    )
    with pytest.raises(ValueError, match="phase transition"):
        executor(skip)
    forged = build_request(
        req_type="workflow-action",
        args={
            "action": "advance",
            "run_id": run.run_id,
            "current_phase": "build",
            "evidence_root": str(tmp_path),
            "phase_evidence": {"ref": "does-not-exist.json", "sha256": "0" * 64},
        },
        requested_by="operator",
    )
    with pytest.raises(ValueError, match="evidence locator"):
        executor(forged)

    plan_evidence = _write_evidence(
        tmp_path,
        "plan-gate.json",
        _phase_payload(
            work_id=run.work_id, phase="plan", persona="planner", kind="phase-gate",
            candidate=None, domain="openai", outputs=["docs/plan.md"],
        ),
    )
    advance = build_request(
        req_type="workflow-action",
        args={
            "action": "advance", "run_id": run.run_id, "current_phase": "build",
            "evidence_root": str(tmp_path), "phase_evidence": plan_evidence,
        },
        requested_by="operator",
    )
    assert executor(advance)["current_phase"] == "build"
    audited = registry.get_workflow_run(run.run_id)
    assert all(
        step.executor == "codex" and step.domain == "openai" and step.gate_result == "passed"
        for step in audited.steps if step.phase == "plan"
    )
    candidate = "a" * 40
    for current_phase, next_phase, kind, persona, domain in (
        ("build", "verify", "phase-gate", "builder", "openai"),
        ("verify", "review", "verification", "reviewer", "anthropic"),
    ):
        locator = _write_evidence(
            tmp_path,
            f"{current_phase}-gate.json",
            _phase_payload(
                work_id=run.work_id, phase=current_phase, persona=persona, kind=kind,
                candidate=candidate, domain=domain,
            ),
        )
        request = build_request(
            req_type="workflow-action",
            args={
                "action": "advance", "run_id": run.run_id, "current_phase": next_phase,
                "evidence_root": str(tmp_path), "phase_evidence": locator,
            },
            requested_by="operator",
        )
        assert executor(request)["current_phase"] == next_phase

    foreign = _write_evidence(
        tmp_path,
        "foreign-review.json",
        _phase_payload(
            work_id=run.work_id, phase="review", persona="reviewer", kind="foreign-review",
            candidate=candidate, domain="anthropic",
        ),
    )
    stale_copilot = _write_evidence(
        tmp_path,
        "stale-copilot.json",
        {
            "schema_version": 1, "kind": "copilot", "status": "passed",
            "work_id": run.work_id, "phase": "ship", "head": "b" * 40, "commit_id": "b" * 40,
        },
    )
    bad_ship = build_request(
        req_type="workflow-action",
        args={
            "action": "advance", "run_id": run.run_id, "current_phase": "ship",
            "evidence_root": str(tmp_path), "phase_evidence": foreign,
            "gate_refs": [{"kind": "copilot", **stale_copilot}], "gate_status": "passed",
        },
        requested_by="operator",
    )
    with pytest.raises(ValueError, match="exact-HEAD"):
        executor(bad_ship)
    copilot = _write_evidence(
        tmp_path,
        "copilot.json",
        {
            "schema_version": 1, "kind": "copilot", "status": "passed",
            "work_id": run.work_id, "phase": "ship", "head": candidate, "commit_id": candidate,
        },
    )
    ship = build_request(
        req_type="workflow-action",
        args={
            "action": "advance", "run_id": run.run_id, "current_phase": "ship",
            "evidence_root": str(tmp_path), "phase_evidence": foreign,
            "gate_refs": [{"kind": "copilot", **copilot}], "gate_status": "passed",
        },
        requested_by="operator",
    )
    assert executor(ship)["current_phase"] == "ship"
    shipped = registry.get_workflow_run(run.run_id)
    assert shipped.verified_head == shipped.candidate_head == candidate
    assert {ref.kind for ref in shipped.gate_refs} == {"brainstorm", "foreign-review", "copilot"}
    assert all(
        step.executor is not None and step.domain is not None and step.gate_result == "passed"
        for step in shipped.steps if step.phase in {"claim", "define", "plan", "build", "verify", "review"}
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
