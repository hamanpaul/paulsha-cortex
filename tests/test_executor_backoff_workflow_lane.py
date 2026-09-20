"""#928：workflow lane 尚未消費 durable executor backoff 的 RED 測試。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, runtime_preflight
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.runtime_preflight import ExecutorEnvironment, RuntimeCapability
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
        _step("build", "subagent-build", gate_result="pending"),
        _step("verify", "reviewer-verify", gate_result="pending"),
        _step("review", "reviewer-review", gate_result="pending"),
        _step("ship", "manager-ship", gate_result="pending"),
    )


def _host_executor_env(*, name: str, provider_identity: str | None = None) -> ExecutorEnvironment:
    return ExecutorEnvironment(
        name=name,
        interpreter=(sys.executable,),
        path=os.environ.get("PATH", ""),
        home=os.path.expanduser("~"),
        provider_identity=provider_identity,
    )


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


def _init_worktree(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "a.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "base"], path)
    return _git(["rev-parse", "HEAD"], path).stdout.strip().lower()


def _make_run(registry: JobRegistry, *, workspace_root: Path, steps):
    return registry._manager_create_workflow_run(
        work_id="executor-backoff-928",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace_root),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#928",),
        openspec_refs=("executor-backoff-terminal-admission",),
        facets=(),
        gate_status="running",
    )


class _FakeWorktreeCreator:
    def __init__(self, path: Path):
        self._path = path

    def create(self, branch: str, base_sha: str | None = None, *, job_id: str | None = None) -> Path:
        return self._path


class _Launcher:
    def __init__(self, executor: str, model_id: str) -> None:
        self._executor = executor
        self._model_id = model_id

    def as_commit_required(self):
        return self

    def as_read_only(self):
        return self

    def as_review_only(self, *, terminal_kind: str):
        return self

    def executor_environment(self) -> ExecutorEnvironment:
        return _host_executor_env(
            name=f"{self._executor}-workflow",
            provider_identity=self._executor,
        )

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        return LaunchHandle(
            executor=self._executor,
            model_id=self._model_id,
            session_name=slice_id,
            pid=4242,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


class _ResumeDispatcher:
    def __init__(self, registry: JobRegistry, worktree: Path) -> None:
        self._registry = registry
        self._git_runner = None
        self._worktree_creator = _FakeWorktreeCreator(worktree)

    def poll_headless_done(self, job_id: str) -> dict:
        return self._registry.get_job(job_id)


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


def _launcher_factory(identity):
    return _Launcher(identity.executor, identity.model_id)


def _outcome_input(outcome: str, reason: str, reset_at: float | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "outcome": outcome,
        "authority": "structured",
        "reason": reason,
        "retryable": outcome == "rate_limited",
    }
    if reset_at is not None:
        payload["reset_at"] = reset_at
    payload_bytes = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "outcome": outcome,
        "authority": "structured",
        "payload": payload,
        "payload_fingerprint": hashlib.sha256(payload_bytes).hexdigest(),
        "reason": reason,
        "policy_revision": "executor-backoff/v1",
        "rate_limited_base_seconds": 10.0,
        "quota_base_seconds": 40.0,
        "backoff_multiplier_base": 2.0,
        "backoff_max_exponent": 4,
        "reset_margin_seconds": 5.0,
        "reset_provenance": "structured" if reset_at is not None else "absent",
        "reset_parser": None,
        "evidence_ref": "fixture://executor-backoff-terminal-admission",
    }


def _record_backoff(
    coordinator_root: Path,
    *,
    executor: str,
    model_id: str,
    now: float,
    reset_at: float,
    job_id: str,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    result = executor_backoff.record_backoff(
        coordinator_root,
        executor,
        model_id,
        now=now,
        outcome=_outcome_input("rate_limited", "synthetic-rate-limit", reset_at),
        reset_at=reset_at,
        reason="synthetic-rate-limit",
        job_id=job_id,
        event_epoch=now,
    )
    assert result.observation.value == "valid"


def _seed_failed_job(
    registry: JobRegistry,
    *,
    run,
    worktree: Path,
    executor: str,
    model_id: str,
    domain: str,
) -> dict[str, object]:
    base_head = _git(["rev-parse", "HEAD"], worktree).stdout.strip().lower()
    job = registry.create_job(
        task=f"seed-{run.run_id}-{executor}",
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
    registry.update_headless_result(
        job["job_id"],
        status="failed",
        exit_code=1,
        provider_outcome={
            "outcome": "rate_limited",
            "authority": "text_signal",
            "reason": "synthetic-rate-limit",
            "retryable": True,
        },
    )
    return registry.get_job(job["job_id"])


def test_active_executor_backoff_skips_the_cooled_down_first_candidate_on_resume(
    tmp_path: Path,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path, steps=_build_only_steps())
    identities = _two_builder_identities()
    dispatcher = _ResumeDispatcher(registry, worktree)
    coordinator_root = tmp_path / "coordinator"
    current = time.time()

    _record_backoff(
        coordinator_root,
        executor="codex",
        model_id="gpt-primary",
        now=current,
        reset_at=current + 600.0,
        job_id="job-codex-rate-limited",
    )

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=coordinator_root,
    )

    dispatched = registry.get_job(result["job_id"])
    assert dispatched["executor"] == "claude"
    assert dispatched["model_id"] == "claude-primary"
    assert dispatched["dispatch_reroute"]["source"] == "executor-backoff"
    assert dispatched["dispatch_reroute"]["skipped"][0]["executor"] == "codex"
    assert dispatched["dispatch_reroute"]["skipped"][0]["model_id"] == "gpt-primary"
    assert dispatched["dispatch_reroute"]["skipped"][0]["retry_after_epoch"] == pytest.approx(
        current + 605.0, rel=0, abs=5.0
    )


def test_expired_executor_backoff_restores_first_candidate_without_stale_reroute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    identities = _two_builder_identities()
    coordinator_root = tmp_path / "coordinator"
    base = 1_786_000_000.0
    observed_now = {"value": base + 1.0}
    monkeypatch.setattr(manager.time, "time", lambda: observed_now["value"])

    _record_backoff(
        coordinator_root,
        executor="codex",
        model_id="gpt-primary",
        now=base,
        reset_at=base + 60.0,
        job_id="job-codex-rate-limited",
    )

    first_registry = JobRegistry(state_path=tmp_path / "jobs-first.json")
    first_run = _make_run(first_registry, workspace_root=tmp_path, steps=_build_only_steps())
    first_dispatcher = _ResumeDispatcher(first_registry, worktree)

    first_result = manager.resume_workflow_run(
        first_dispatcher,
        run_id=first_run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=coordinator_root,
    )

    first_job = first_registry.get_job(first_result["job_id"])
    assert first_job["executor"] == "claude"
    assert first_job["dispatch_reroute"] is not None

    observed_now["value"] = base + 120.0
    second_registry = JobRegistry(state_path=tmp_path / "jobs-second.json")
    second_run = _make_run(second_registry, workspace_root=tmp_path, steps=_build_only_steps())
    second_dispatcher = _ResumeDispatcher(second_registry, worktree)

    second_result = manager.resume_workflow_run(
        second_dispatcher,
        run_id=second_run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=coordinator_root,
    )

    second_job = second_registry.get_job(second_result["job_id"])
    assert second_job["executor"] == "codex"
    assert second_job["model_id"] == "gpt-primary"
    assert second_job["dispatch_reroute"] is None


def test_runtime_preflight_still_projects_cooled_candidate_during_workflow_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path, steps=_build_only_steps())
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    assert step is not None
    coordinator_root = tmp_path / "coordinator"
    current = time.time()

    _record_backoff(
        coordinator_root,
        executor="codex",
        model_id="gpt-primary",
        now=current,
        reset_at=current + 600.0,
        job_id="job-codex-rate-limited",
    )

    candidates = manager._workflow_identity_candidates(run, step, identities)
    eligible, skipped = manager._executor_backoff_filter(
        candidates,
        coordinator_root=coordinator_root,
        now=current + 1.0,
        registry=registry,
    )
    skipped_by_identity = {
        (item["executor"], item["model_id"]): item for item in skipped
    }
    monkeypatch.setattr(
        runtime_preflight,
        "card_runtime_requirements",
        lambda _card: (RuntimeCapability("provider", "executor"),),
    )

    gate = manager._runtime_preflight_gate(
        run,
        step,
        identities=identities,
        launcher_factory=_launcher_factory,
        candidates=eligible,
        projected_backoff_candidates=tuple(
            (
                candidate,
                skipped_by_identity[(candidate.executor, candidate.model_id)],
            )
            for candidate in candidates
            if (candidate.executor, candidate.model_id) in skipped_by_identity
        ),
    )

    assert gate is not None
    assert gate.identity is not None
    assert gate.identity.executor == "claude"
    assert gate.attempts[0].identity_token == "codex/gpt-primary"
    freshness = gate.attempts[0].provider_freshness[0]
    assert freshness.status == "degraded"
    assert freshness.source == "executor-backoff"


def test_all_candidates_on_backoff_return_executor_backoff_decision_without_dispatch(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path, steps=_build_only_steps())
    identities = _two_builder_identities()
    dispatcher = _ResumeDispatcher(registry, worktree)
    coordinator_root = tmp_path / "coordinator"
    current = time.time()

    _record_backoff(
        coordinator_root,
        executor="codex",
        model_id="gpt-primary",
        now=current,
        reset_at=current + 600.0,
        job_id="job-codex-rate-limited",
    )
    _record_backoff(
        coordinator_root,
        executor="claude",
        model_id="claude-primary",
        now=current,
        reset_at=current + 900.0,
        job_id="job-claude-rate-limited",
    )

    codex_backoff = executor_backoff.active_backoff(
        coordinator_root, "codex", "gpt-primary", now=current + 1.0
    )
    claude_backoff = executor_backoff.active_backoff(
        coordinator_root, "claude", "claude-primary", now=current + 1.0
    )
    assert codex_backoff.backoff is not None
    assert claude_backoff.backoff is not None

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=coordinator_root,
    )

    assert result["reason"] == "executor-backoff"
    assert result["retry_after_epoch"] == codex_backoff.backoff.deadline_epoch
    assert result["skipped"] == [
        {
            "executor": "codex",
            "model_id": "gpt-primary",
            "retry_after_epoch": codex_backoff.backoff.deadline_epoch,
        },
        {
            "executor": "claude",
            "model_id": "claude-primary",
            "retry_after_epoch": claude_backoff.backoff.deadline_epoch,
        },
    ]
    assert registry.list_jobs() == []


def test_executor_backoff_unknown_returns_decision_without_fake_deadline(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import executor_backoff

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path, steps=_build_only_steps())
    identities = _two_builder_identities()
    dispatcher = _ResumeDispatcher(registry, worktree)
    coordinator_root = tmp_path / "coordinator"
    coordinator_root.mkdir(parents=True, exist_ok=True)
    (coordinator_root / executor_backoff.STATE_FILENAME).write_text(
        "{not-json}\n",
        encoding="utf-8",
    )

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=coordinator_root,
    )

    assert result["reason"] == "executor-backoff-unknown"
    assert "retry_after_epoch" not in result
    assert "job_id" not in result
    assert result["diagnostics"][0]["executor"] == "codex"
    assert result["diagnostics"][0]["reconciliation"] == "unknown"
    assert registry.list_jobs() == []
