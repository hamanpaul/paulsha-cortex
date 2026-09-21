"""#946：admission reconciliation replay 與 workflow decision consumer 的 RED tests。"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import executor_backoff, manager
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


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


def _build_only_steps() -> tuple[WorkflowStep, ...]:
    return (
        _step("claim", "manager-claim", gate_result="passed"),
        _step("define", "planner-define", gate_result="passed"),
        _step("plan", "planner-plan", gate_result="passed"),
        _step("build", "subagent-build"),
        _step("verify", "reviewer-verify"),
        _step("review", "reviewer-review"),
        _step("ship", "manager-ship"),
    )


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_worktree(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "a.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "base"], path)


def _make_run(registry: JobRegistry, *, workspace_root: Path):
    return registry._manager_create_workflow_run(
        work_id="executor-backoff-reconcile-replay",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace_root),
        combo="feature-oneshot",
        current_phase="build",
        steps=_build_only_steps(),
        issue_refs=("hamanpaul/paulsha-cortex#946",),
        openspec_refs=("executor-backoff-reconcile-replay",),
        facets=(),
        gate_status="running",
    )


def _two_builder_identities() -> IdentityRegistry:
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
        ]
    )


def _seed_job(
    registry: JobRegistry,
    *,
    run,
    worktree: Path,
    executor: str,
    model_id: str,
    domain: str,
    status: str,
    exit_code: int,
    outcome: str | None = None,
    reason: str = "synthetic-rate-limit",
    reset_at: float | None = None,
) -> dict[str, object]:
    base_head = _git(["rev-parse", "HEAD"], worktree).stdout.strip().lower()
    job = registry.create_job(
        task=f"seed-{run.run_id}-{executor}-{len(registry.list_jobs())}",
        persona="builder",
        branch=f"feature/{run.work_id}",
        pane="",
        worktree=str(worktree),
        dispatch_head=base_head,
        executor=executor,
        model_id=model_id,
        independence_domain=domain,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        workflow_repo_root=str(worktree),
        workflow_input_root=str(worktree),
        source_revision=run.source_revision,
    )
    provider_outcome = None
    if outcome is not None:
        provider_outcome = {
            "outcome": outcome,
            "authority": "structured",
            "reason": reason,
            "retryable": outcome == "rate_limited",
        }
        if reset_at is not None:
            provider_outcome["reset_at"] = reset_at
    registry.update_headless_result(
        job["job_id"],
        status=status,
        exit_code=exit_code,
        provider_outcome=provider_outcome,
    )
    return registry.get_job(job["job_id"])


def _seed_terminal_job(
    registry: JobRegistry,
    *,
    run,
    worktree: Path,
    executor: str = "codex",
    model_id: str = "gpt-primary",
    domain: str = "openai",
    outcome: str = "rate_limited",
    reason: str = "synthetic-rate-limit",
    reset_at: float | None = None,
) -> dict[str, object]:
    return _seed_job(
        registry,
        run=run,
        worktree=worktree,
        executor=executor,
        model_id=model_id,
        domain=domain,
        status="failed",
        exit_code=1,
        outcome=outcome,
        reason=reason,
        reset_at=reset_at,
    )


class _ResumeDispatcher:
    def __init__(self, registry: JobRegistry, worktree: Path) -> None:
        self._registry = registry
        self._git_runner = None
        self._worktree_creator = None

    def poll_headless_done(self, job_id: str) -> dict[str, object]:
        return self._registry.get_job(job_id)


@pytest.mark.parametrize("outcome", ["rate_limited", "quota"])
def test_admission_replays_historical_terminal_inventory_and_is_idempotent(
    tmp_path: Path,
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5(a): an empty store must be repaired from durable terminal inventory."""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    candidate = _two_builder_identities().identities[0]
    terminal = _seed_terminal_job(
        registry,
        run=run,
        worktree=worktree,
        outcome=outcome,
        reason=f"synthetic-{outcome}",
    )
    coordinator_root = tmp_path / "coordinator"
    admission_now = time.time() + 3600.0
    replayed_jobs: list[str] = []
    original_record_backoff = executor_backoff.record_backoff

    def record_replay(*args, **kwargs):
        replayed_jobs.append(str(kwargs["job_id"]))
        return original_record_backoff(*args, **kwargs)

    monkeypatch.setattr(executor_backoff, "record_backoff", record_replay)

    first = manager._executor_backoff_admission_report(
        [candidate],
        coordinator_root=coordinator_root,
        now=admission_now,
        registry=registry,
    )

    assert first["eligible"] == [candidate]
    assert first["skipped"] == []
    assert first["unknown"] == []
    stored = executor_backoff.read_store(
        coordinator_root / executor_backoff.STATE_FILENAME
    )
    assert stored.payload is not None
    assert set(stored.payload["events"]) == {terminal["job_id"]}
    assert set(stored.payload["acks"]) == {terminal["job_id"]}

    second = manager._executor_backoff_admission_report(
        [candidate],
        coordinator_root=coordinator_root,
        now=admission_now,
        registry=registry,
    )

    assert second["eligible"] == [candidate]
    assert second["skipped"] == []
    assert second["unknown"] == []
    assert replayed_jobs == [terminal["job_id"]]
    reread = executor_backoff.read_store(
        coordinator_root / executor_backoff.STATE_FILENAME
    )
    assert reread.payload == stored.payload


def test_admission_replay_preserves_a_real_future_cooldown(
    tmp_path: Path,
) -> None:
    """A replay must fold the immutable reset time, not admission time."""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    admission_now = time.time() + 3600.0
    reset_at = int(admission_now + 600.0)
    _seed_terminal_job(
        registry,
        run=run,
        worktree=worktree,
        reset_at=reset_at,
    )

    report = manager._executor_backoff_admission_report(
        [_two_builder_identities().identities[0]],
        coordinator_root=tmp_path / "coordinator",
        now=admission_now,
        registry=registry,
    )

    assert report["eligible"] == []
    assert report["skipped"] == [
        {
            "executor": "codex",
            "model_id": "gpt-primary",
            "retry_after_epoch": pytest.approx(reset_at + 5.0, rel=0, abs=0.01),
        }
    ]


def test_admission_replays_only_the_terminal_event_missing_after_a_store_write(
    tmp_path: Path,
) -> None:
    """R5(b): a crash between inventory observation and store write is recoverable."""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    first = _seed_terminal_job(registry, run=run, worktree=worktree)
    second = _seed_terminal_job(registry, run=run, worktree=worktree)
    classification = manager.provider_outcome.classification_from_job(first)
    assert classification is not None
    coordinator_root = tmp_path / "coordinator"
    manager.record_executor_backoff_from_job(coordinator_root, first, classification)
    before = executor_backoff.read_store(
        coordinator_root / executor_backoff.STATE_FILENAME
    )
    assert before.payload is not None
    assert set(before.payload["events"]) == {first["job_id"]}

    report = manager._executor_backoff_admission_report(
        [_two_builder_identities().identities[0]],
        coordinator_root=coordinator_root,
        now=time.time() + 3600.0,
        registry=registry,
    )

    assert report["eligible"] == [_two_builder_identities().identities[0]]
    assert report["skipped"] == []
    assert report["unknown"] == []
    after = executor_backoff.read_store(
        coordinator_root / executor_backoff.STATE_FILENAME
    )
    assert after.payload is not None
    assert set(after.payload["events"]) == {first["job_id"], second["job_id"]}
    assert set(after.payload["acks"]) == {first["job_id"], second["job_id"]}


def test_admission_conflict_stays_unknown_without_overwriting_the_store(
    tmp_path: Path,
) -> None:
    """R5(c): a same-key, different-payload event is an integrity conflict."""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    terminal = _seed_terminal_job(
        registry,
        run=run,
        worktree=worktree,
        reason="inventory-reason",
    )
    coordinator_root = tmp_path / "coordinator"
    stored = manager.record_executor_backoff_from_job(
        coordinator_root,
        terminal,
        manager.provider_outcome.ProviderFailureClassification(
            outcome=manager.provider_outcome.ProviderOutcome.RATE_LIMITED,
            authority=manager.provider_outcome.SignalAuthority.STRUCTURED,
            reason="store-reason",
        ),
    )
    assert stored is not None
    before = executor_backoff.read_store(
        coordinator_root / executor_backoff.STATE_FILENAME
    )
    assert before.payload is not None

    report = manager._executor_backoff_admission_report(
        [_two_builder_identities().identities[0]],
        coordinator_root=coordinator_root,
        now=time.time() + 3600.0,
        registry=registry,
    )

    assert report["eligible"] == []
    assert report["skipped"] == []
    assert len(report["unknown"]) == 1
    detail = report["unknown"][0]
    assert "inventory-conflict" in detail["diagnostics"]
    assert detail["pending_count"] == 0
    assert detail["earliest_event_epoch"] in (None, 0)
    assert detail["latest_event_epoch"] in (None, 0)
    assert detail["replayed_count"] == 0
    assert detail["replay_diagnostics"] == []
    after = executor_backoff.read_store(
        coordinator_root / executor_backoff.STATE_FILENAME
    )
    assert after.payload == before.payload


def test_admission_reports_store_capacity_rejection_during_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5(d): a replay mutation rejected by capacity remains unknown with evidence."""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    terminal = _seed_terminal_job(registry, run=run, worktree=worktree)
    coordinator_root = tmp_path / "coordinator"
    calls: list[str] = []

    def reject_replay(*args, **kwargs):
        calls.append(str(kwargs["job_id"]))
        return executor_backoff.BackoffMutationResult(
            observation=executor_backoff.StoreObservation.UNKNOWN,
            diagnostics=("capacity-exceeded",),
            reconciliation=executor_backoff.ReconciliationStatus.UNKNOWN,
        )

    monkeypatch.setattr(executor_backoff, "record_backoff", reject_replay)
    report = manager._executor_backoff_admission_report(
        [_two_builder_identities().identities[0]],
        coordinator_root=coordinator_root,
        now=time.time() + 3600.0,
        registry=registry,
    )

    assert calls == [terminal["job_id"]]
    assert report["eligible"] == []
    assert report["skipped"] == []
    assert len(report["unknown"]) == 1
    detail = report["unknown"][0]
    assert detail["pending_count"] == 1
    assert detail["earliest_event_epoch"] is not None
    assert detail["latest_event_epoch"] is not None
    assert detail["replayed_count"] == 0
    assert any(
        "capacity-exceeded" in str(item) for item in detail["replay_diagnostics"]
    )


def test_resume_consumes_decision_after_advance_without_job_id_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5(e): the post-advance decision path must preserve the decision payload."""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    _seed_job(
        registry,
        run=run,
        worktree=worktree,
        executor="codex",
        model_id="gpt-primary",
        domain="openai",
        status="exited",
        exit_code=0,
    )
    dispatcher = _ResumeDispatcher(registry, worktree)
    decision = {
        "run_id": run.run_id,
        "current_phase": run.current_phase,
        "reason": "executor-backoff-unknown",
        "diagnostics": [{"executor": "codex", "model_id": "gpt-primary"}],
    }
    before_attempts = registry.get_workflow_run(run.run_id).attempts

    monkeypatch.setattr(
        manager,
        "terminalize_workflow_job",
        lambda registry, *, job_id, **kwargs: registry.get_job(job_id),
    )
    monkeypatch.setattr(manager, "_malformed_workflow_card_terminal", lambda _job: False)
    monkeypatch.setattr(
        manager,
        "apply_workflow_action",
        lambda *args, **kwargs: {
            "run_id": run.run_id,
            "current_phase": "verify",
            "reason": "advanced",
        },
    )
    monkeypatch.setattr(
        manager,
        "dispatch_workflow_card",
        lambda *args, **kwargs: decision,
    )

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=_two_builder_identities(),
        launcher_factory=lambda _identity: None,
        coordinator_root=tmp_path / "coordinator",
    )

    assert result["dispatch_decision"] == decision
    assert "job_id" not in result
    assert registry.get_workflow_run(run.run_id).attempts == before_attempts
