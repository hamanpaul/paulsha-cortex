from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import automatic_retry, manager, review, terminal_contract
from paulsha_cortex.coordinator.diagnostics import diagnostic_reason
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowRun, WorkflowStep


RUN_ID = "workflow-" + "a" * 20
CANDIDATE = "b" * 40
CLAIM_KEY = "claim:v1:" + "c" * 64


def _identities() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "claude",
                "model_id": "claude-primary",
                "independence_domain": "anthropic",
                "capabilities": ["build"],
            },
            {
                "executor": "google",
                "model_id": "gemini-review",
                "independence_domain": "google",
                "capabilities": ["review"],
            },
        ]
    )


def _steps(*, current_phase: str) -> tuple[WorkflowStep, ...]:
    build_passed = current_phase == "review"
    return (
        WorkflowStep(
            phase="build",
            persona="builder",
            card="implement",
            executor="codex" if build_passed else None,
            model="gpt-primary" if build_passed else None,
            domain="openai" if build_passed else None,
            inputs=(),
            outputs=(),
            gate_result="passed" if build_passed else "pending",
            commit_policy="required",
            test_policy="focused",
        ),
        WorkflowStep(
            phase="verify",
            persona="reviewer",
            card="verify",
            executor=None,
            model=None,
            domain=None,
            inputs=(),
            outputs=(),
            gate_result="passed" if current_phase == "review" else "pending",
        ),
        WorkflowStep(
            phase="review",
            persona="reviewer",
            card="review",
            executor=None,
            model=None,
            domain=None,
            inputs=(),
            outputs=(),
            gate_result="needs_human" if current_phase == "review" else "pending",
        ),
    )


def _make_run(
    registry: JobRegistry,
    tmp_path: Path,
    *,
    current_phase: str,
    reason: str | None,
    limit: int = 1,
    evidence_refs: tuple[str, ...] = (),
    candidate: str | None = CANDIDATE,
):
    facets = ("needs_human",) if reason is not None else ()
    reason_payload = (
        diagnostic_reason(
            reason,
            "reviewer found a correctness defect in the Candidate",
            source="tests.test_automatic_retry_1306",
            run_id=RUN_ID,
            work_id="auto-retry-fallback",
            card="review" if current_phase == "review" else "implement",
            candidate=CANDIDATE,
            evidence_refs=evidence_refs,
        )
        if reason is not None
        else None
    )
    return registry._manager_create_workflow_run(
        work_id="auto-retry-fallback",
        repo="hamanpaul/paulsha-cortex",
        claim_key=CLAIM_KEY,
        source_revision="d" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase=current_phase,
        steps=_steps(current_phase=current_phase),
        issue_refs=("hamanpaul/paulsha-cortex#1306",),
        openspec_refs=("auto-retry-fallback",),
        facets=facets,
        gate_status="running",
        attempts={"build": 1, "review": 1},
        candidate_head=candidate,
        model_chain_override={
            "builder": {"executor": "codex", "model_id": "gpt-primary"},
            "reviewer": {"executor": "google", "model_id": "gemini-review"},
        },
        auto_retry_limit=limit,
        needs_human_reason=reason_payload,
    )


def _create_job(
    registry: JobRegistry,
    *,
    run,
    tmp_path: Path,
    identity,
    status: str,
    runtime_diagnostic: dict | None = None,
) -> dict:
    job = registry.create_job(
        task=f"{run.run_id}-build-{len(registry.list_jobs())}",
        persona="builder",
        branch="feature/1306-auto-retry-fallback",
        pane="",
        worktree=str(tmp_path),
        dispatch_head="e" * 40,
        executor=identity.executor,
        model_id=identity.model_id,
        independence_domain=identity.independence_domain,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="implement",
        workflow_phase="build",
        workflow_repo_root=str(tmp_path),
        workflow_input_root=str(tmp_path),
        source_revision=run.source_revision,
    )
    if status in {"exited", "failed"}:
        diagnostic = runtime_diagnostic
        if diagnostic is not None:
            diagnostic = {
                **diagnostic,
                "source": "tests.test_automatic_retry_1306",
                "job_id": job["job_id"],
            }
        registry.update_headless_result(
            job["job_id"],
            status=status,
            exit_code=0 if status == "exited" else 1,
            runtime_diagnostic=diagnostic,
        )
    return registry.get_job(job["job_id"])


def _wire_fake_dispatch(monkeypatch, registry, identities, tmp_path):
    seen = []

    def fake_dispatch(_dispatcher, *, run, **kwargs):
        candidates = manager._workflow_identity_candidates_for_persona(run, "builder", identities)
        identity = kwargs.get("forced_identity") or candidates[0]
        prior_jobs = [
            job
            for job in registry.list_jobs()
            if job.get("workflow_run_id") == run.run_id
            and job.get("workflow_phase") == "build"
            and job.get("workflow_card") == "implement"
        ]
        retry_context = manager._workflow_retry_context(prior_jobs, run=run, registry=registry)
        seen.append((run, identity, retry_context))
        return _create_job(
            registry,
            run=run,
            tmp_path=tmp_path,
            identity=identity,
            status="dispatched",
        )

    monkeypatch.setattr(manager, "dispatch_workflow_card", fake_dispatch)
    return seen


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _retry_failure(registry, run, *, reason: str, phase: str):
    return registry._manager_update_workflow_run(
        run.run_id,
        current_phase=phase,
        facets=("needs_human",),
        needs_human_reason=diagnostic_reason(
            reason,
            "reviewer found a correctness defect in the Candidate",
            source="tests.test_automatic_retry_1306",
            run_id=run.run_id,
            work_id=run.work_id,
            card="review" if phase == "review" else "implement",
            candidate=CANDIDATE,
        ),
    )


@pytest.mark.parametrize(
    ("phase", "reason"),
    [("build", "gate-contradiction"), ("review", "blocking-findings")],
)
def test_gate_and_review_failures_retry_exact_candidate_with_recorded_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, reason: str
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    identities = _identities()
    run = _make_run(registry, tmp_path, current_phase=phase, reason=reason)
    _create_job(
        registry,
        run=run,
        tmp_path=tmp_path,
        identity=identities.get("codex", "gpt-primary"),
        status="exited",
    )
    dispatcher = SimpleNamespace(_registry=registry)
    seen = _wire_fake_dispatch(monkeypatch, registry, identities, tmp_path)

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: object(),
        coordinator_root=tmp_path,
    )

    persisted = registry.get_workflow_run(run.run_id)
    assert result["reason"] == "automatic-retry-build", (result, persisted.auto_retry_history)
    assert persisted.current_phase == "build"
    assert persisted.candidate_head == CANDIDATE
    assert persisted.auto_retry_history[0]["candidate"] == CANDIDATE
    assert persisted.auto_retry_history[0]["reason"] == reason
    assert seen[0][2]["automatic_retry"]["reason"] == reason
    assert "correctness defect" in seen[0][2]["automatic_retry"]["feedback"]


def test_first_build_gate_contradiction_verifies_terminal_head_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    source = repo / "source.txt"
    source.write_text("base\n", encoding="utf-8")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    source.write_text("candidate\n", encoding="utf-8")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "-q", "-m", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    identities = _identities()
    run = _make_run(
        registry,
        tmp_path,
        current_phase="build",
        reason=None,
        candidate=None,
    )
    log_path = tmp_path / "builder.jsonl"
    log_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-card",
                "status": "passed",
                "run_id": run.run_id,
                "card_id": "implement",
                "candidate": candidate,
                "outputs": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    old_job = registry.create_job(
        task=f"{run.run_id}-build-initial",
        persona="builder",
        branch="feature/1306-auto-retry-fallback",
        pane="",
        worktree=str(repo),
        dispatch_head=base,
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        log_path=str(log_path),
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="implement",
        workflow_phase="build",
        workflow_repo_root=str(repo),
        workflow_input_root=str(repo),
        source_revision=run.source_revision,
        workflow_test_policy="focused",
    )
    registry.update_headless_result(old_job["job_id"], status="exited", exit_code=0)
    dispatcher = SimpleNamespace(_registry=registry, _git_runner=None)
    seen = _wire_fake_dispatch(monkeypatch, registry, identities, tmp_path)

    def reject_terminal(_registry, *, job_id, coordinator_root):
        raise terminal_contract.GateContradictionError(
            gate="pytest", expected="passed", actual="failed", detail="one test failed"
        )

    monkeypatch.setattr(manager, "terminalize_workflow_job", reject_terminal)
    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: object(),
        coordinator_root=tmp_path,
    )

    persisted = registry.get_workflow_run(run.run_id)
    assert result["reason"] == "automatic-retry-build", persisted.needs_human_reason
    assert persisted.candidate_head == candidate
    assert persisted.auto_retry_history[0]["candidate"] == candidate
    assert seen[0][2]["automatic_retry"]["reason"] == "gate-contradiction"


def test_builder_switch_waits_for_limit_then_preserves_reviewer_independence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    identities = _identities()
    run = _make_run(registry, tmp_path, current_phase="build", reason="gate-contradiction", limit=1)
    first_failed = _create_job(
        registry,
        run=run,
        tmp_path=tmp_path,
        identity=identities.get("codex", "gpt-primary"),
        status="exited",
    )
    dispatcher = SimpleNamespace(_registry=registry)
    seen = _wire_fake_dispatch(monkeypatch, registry, identities, tmp_path)

    first = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: object(),
        coordinator_root=tmp_path,
    )
    assert first["reason"] == "automatic-retry-build", first
    retry_job = registry.get_job(first["job_id"])
    registry.update_headless_result(retry_job["job_id"], status="exited", exit_code=0)
    run = _retry_failure(
        registry,
        registry.get_workflow_run(run.run_id),
        reason="gate-contradiction",
        phase="build",
    )

    second = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: object(),
        coordinator_root=tmp_path,
    )
    persisted = registry.get_workflow_run(run.run_id)
    assert second["reason"] == "automatic-retry-build"
    assert seen[0][1].executor == "codex"
    assert seen[1][1].executor == "claude"
    assert persisted.auto_retry_history[-1]["decision"] == "switch-builder"
    assert persisted.model_chain_override["builder"] == {
        "executor": "claude",
        "model_id": "claude-primary",
    }
    assert persisted.auto_retry_history[-1]["builder_before"] == {
        "executor": "codex",
        "model_id": "gpt-primary",
    }
    assert persisted.auto_retry_history[-1]["builder_after"] == {
        "executor": "claude",
        "model_id": "claude-primary",
    }
    assert first_failed["job_id"] != retry_job["job_id"]

    second_job = registry.get_job(second["job_id"])
    registry.update_headless_result(second_job["job_id"], status="exited", exit_code=0)
    exhausted_input = _retry_failure(
        registry,
        registry.get_workflow_run(run.run_id),
        reason="gate-contradiction",
        phase="build",
    )
    exhausted = manager.resume_workflow_run(
        dispatcher,
        run_id=exhausted_input.run_id,
        identities=identities,
        launcher_factory=lambda _identity: object(),
        coordinator_root=tmp_path,
    )
    exhausted_run = registry.get_workflow_run(run.run_id)
    assert exhausted["reason"] == "automatic-retry-exhausted"
    assert exhausted_run.needs_human_reason["reason"] == "automatic-retry-exhausted"
    assert [row["decision"] for row in exhausted_run.auto_retry_history] == [
        "retry",
        "switch-builder",
        "exhausted",
    ]
    assert exhausted["automatic_retries"]["remaining_for_current_builder"] == 0


def test_known_executor_environment_error_switches_builder_and_is_observable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    identities = _identities()
    run = _make_run(registry, tmp_path, current_phase="build", reason=None, limit=2)
    failed = _create_job(
        registry,
        run=run,
        tmp_path=tmp_path,
        identity=identities.get("codex", "gpt-primary"),
        status="failed",
        runtime_diagnostic={"reason": "runtime-failure", "detail": "sandbox panic"},
    )
    dispatcher = SimpleNamespace(_registry=registry)
    seen = _wire_fake_dispatch(monkeypatch, registry, identities, tmp_path)

    result = manager._auto_reroute_environment_failure(
        dispatcher,
        registry=registry,
        run=run,
        step=run.steps[0],
        job=failed,
        identities=identities,
        launcher_factory=lambda _identity: object(),
        coordinator_root=tmp_path,
    )
    persisted = registry.get_workflow_run(run.run_id)
    assert result["reason"] == "automatic-builder-fallback"
    assert seen[0][1].executor == "claude"
    assert persisted.auto_retry_history[0]["reason"] == "sandbox-panic"
    assert persisted.auto_retry_history[0]["decision"] == "switch-builder"
    assert manager.workflow_status_entry(registry, persisted)["automatic_retries"]["count"] == 1


def test_review_findings_are_selected_from_exact_candidate_evaluation(
    tmp_path: Path,
) -> None:
    finding = review._normalize_finding(
        {
            "category": "correctness",
            "severity": "critical",
            "summary": "fix the null dereference",
            "evidence": [],
            "recommendation": "guard the optional value before use",
        },
        field="findings[0]",
    )
    evaluation_path = tmp_path / "review.json"
    evaluation_path.write_text(
        json.dumps(
            {
                "schema_version": review.REVIEW_SCHEMA_VERSION,
                "slice_id": "implement",
                "state": "rejected",
                "reason": "blocking-findings",
                "builder_job_id": "builder-1",
                "reviewer_job_id": "reviewer-1",
                "candidate": CANDIDATE,
                "launch_identity": {"builder": None, "reviewer": None},
                "findings": [finding],
            }
        ),
        encoding="utf-8",
    )
    run = SimpleNamespace(candidate_head=CANDIDATE)
    feedback = manager._auto_retry_feedback(
        run,
        {
            "context": {"evidence_refs": [str(evaluation_path)]},
            "detail": "fallback detail",
        },
        tmp_path,
    )
    assert "fix the null dereference" in feedback
    assert "fallback detail" not in feedback


def test_run_retry_limit_and_history_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSC_WORKFLOW_AUTO_RETRY_LIMIT", "3")
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _make_run(registry, tmp_path, current_phase="build", reason=None, limit=None)
    row = run.to_dict()
    restored = WorkflowRun.from_dict(row)
    assert restored.auto_retry_limit == 3
    assert automatic_retry.automatic_retry_summary(restored) == {
        "count": 0,
        "limit_per_builder": 3,
        "remaining_for_current_builder": 3,
        "builder_switches": [],
        "attempts": [],
    }
