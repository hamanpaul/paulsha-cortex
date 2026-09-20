"""#928：workflow lane 尚未消費 durable executor backoff 的 RED 測試。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.runtime_preflight import ExecutorEnvironment
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
