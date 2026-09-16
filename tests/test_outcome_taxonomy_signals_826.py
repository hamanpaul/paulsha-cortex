"""#826：Outcome taxonomy 與 launch failure 訊號保真回歸測試。

accepted plan 要求新的 failure taxonomy 從 producer（dispatcher／autonomy／
workflow launch）一路穿到 persisted job、slice gate_reason 與 workflow
consumer。本檔覆蓋三條 producer 寫入路徑與 workflow consumer 的實際接線。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import autonomy, manager, outcome_taxonomy
from paulsha_cortex.coordinator.dispatcher import Dispatcher, exit_sentinel_path
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.provider_outcome import classify_provider_failure
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

_COPILOT_EFFORT_UNSUPPORTED = (
    'Reasoning effort "xhigh" is not supported for model '
    '"mai-code-1-flash-picker"'
)
_STRUCTURED_RATE_LIMIT_LOG = "\n".join(
    [
        json.dumps(
            {
                "type": "system",
                "subtype": "rate_limit_event",
                "rate_limit_event": {
                    "status": "rejected",
                    "rateLimitType": "five_hour",
                    "resetsAt": 1786554000,
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "api_error_status": 429,
                "terminal_reason": "api_error",
            }
        ),
    ]
)
_INTERRUPTED_LOG = json.dumps(
    {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "[Request interrupted by user]",
        "terminal_reason": "aborted_streaming",
    }
)
_PERSONA_BY_PHASE = {
    "claim": "manager",
    "define": "planner",
    "plan": "planner",
    "build": "builder",
    "verify": "reviewer",
    "review": "reviewer",
    "ship": "manager",
}


class _NoopDispatcher:
    def __init__(self, registry: JobRegistry) -> None:
        self._registry = registry

    def poll_headless_done(self, job_id: str) -> dict:
        return self._registry.get_job(job_id)


class _FixedWorktreeCreator:
    def __init__(self, path: Path) -> None:
        self._path = path

    def create(
        self, branch: str, base_sha: str | None = None, *, job_id: str | None = None
    ) -> Path:
        return self._path


class _WorkflowLauncher:
    def __init__(
        self, executor: str, model_id: str, *, exc: BaseException | None = None
    ) -> None:
        self._executor = executor
        self._model_id = model_id
        self._exc = exc

    def as_commit_required(self):
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str):
        if self._exc is not None:
            raise self._exc
        return LaunchHandle(
            executor=self._executor,
            model_id=self._model_id,
            session_name=slice_id,
            pid=4242,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


class _WorkflowDispatcher:
    def __init__(self, registry: JobRegistry, worktree: Path) -> None:
        self._registry = registry
        self._git_runner = None
        self._worktree_creator = _FixedWorktreeCreator(worktree)

    def poll_headless_done(self, job_id: str) -> dict:
        return self._registry.get_job(job_id)


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


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_worktree(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "base"], path)
    return _git(["rev-parse", "HEAD"], path).lower()


def _make_run(registry: JobRegistry, *, workspace_root: Path):
    return registry._manager_create_workflow_run(
        work_id="outcome-taxonomy-signals",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace_root),
        combo="feature-oneshot",
        current_phase="build",
        steps=_build_only_steps(),
        issue_refs=("hamanpaul/paulsha-cortex#826",),
        openspec_refs=("outcome-taxonomy-signals",),
        facets=(),
        gate_status="running",
    )


def _seed_slice_job(
    state: Path,
    *,
    slice_id: str,
    executor: str = "copilot",
    model_id: str = "mai-code-1-flash-picker",
    pid: int | None = 2_000_000_000,
    log_text: str | None,
    exit_code: int | None = 1,
) -> str:
    registry = JobRegistry(state_path=state)
    log_path = state.parent / f"{slice_id}.jsonl" if log_text is not None else None
    if log_path is not None:
        log_path.write_text(log_text, encoding="utf-8")
        assert exit_code is not None
        exit_sentinel_path(str(log_path)).write_text(str(exit_code), encoding="utf-8")
    job = registry.create_job(
        task=slice_id,
        persona="builder",
        branch=f"feature/{slice_id}",
        pane="",
        worktree=f"/wt/{slice_id}",
        executor=executor,
        model_id=model_id,
        session_name=slice_id if pid is not None else None,
        pid=pid,
        log_path=str(log_path) if log_path is not None else None,
    )
    return str(job["job_id"])


def _poll_and_render_slice(
    tmp_path: Path,
    *,
    slice_id: str,
    executor: str = "copilot",
    model_id: str = "mai-code-1-flash-picker",
    pid: int | None = 2_000_000_000,
    log_text: str | None,
    exit_code: int | None = 1,
) -> tuple[dict, dict]:
    state = tmp_path / "jobs.json"
    job_id = _seed_slice_job(
        state,
        slice_id=slice_id,
        executor=executor,
        model_id=model_id,
        pid=pid,
        log_text=log_text,
        exit_code=exit_code,
    )
    fresh = JobRegistry(state_path=state)
    updated = Dispatcher(fresh, pane_sender=None, worktree_creator=None).poll_headless_done(
        job_id, pid_alive=lambda _pid: False
    )
    handoff = tmp_path / "handoff"
    manager.complete_tick(_NoopDispatcher(fresh), handoff_dir=str(handoff), clock=lambda: "T0")
    manifest = json.loads((handoff / f"{slice_id}.json").read_text(encoding="utf-8"))
    return updated, manifest


def _seed_workflow_job_from_log(
    registry: JobRegistry,
    *,
    run,
    worktree: Path,
    base_head: str,
    executor: str,
    model_id: str,
    domain: str,
    log_text: str,
    exit_code: int,
) -> dict:
    log_path = worktree.parent / f"{executor}-{model_id}.jsonl"
    log_path.write_text(log_text, encoding="utf-8")
    exit_sentinel_path(str(log_path)).write_text(str(exit_code), encoding="utf-8")
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
        session_name=f"seed-{executor}",
        pid=2_000_000_000,
        log_path=str(log_path),
    )
    Dispatcher(registry, pane_sender=None, worktree_creator=None).poll_headless_done(
        str(job["job_id"]), pid_alive=lambda _pid: False
    )
    return registry.get_job(str(job["job_id"]))


def _two_builder_identities() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {
                "executor": "copilot",
                "model_id": "mai-code-1-flash-picker",
                "independence_domain": "github",
                "capabilities": ["build"],
            },
            {
                "executor": "claude",
                "model_id": "claude-sonnet-4.5",
                "independence_domain": "anthropic",
                "capabilities": ["build"],
            },
        ]
    )


def _launcher_factory(identity):
    return _WorkflowLauncher(identity.executor, identity.model_id)


def test_effort_signal_extracts_effort_and_model_from_provider_text() -> None:
    result = outcome_taxonomy.classify_text(
        exit_code=1,
        provider_text=_COPILOT_EFFORT_UNSUPPORTED,
        model_text="",
    )

    assert result.signal.value == "effort_not_supported"
    assert "xhigh" in result.detail
    assert "mai-code-1-flash-picker" in result.detail


def test_exit_127_without_output_classifies_as_executable_not_found_signal() -> None:
    result = outcome_taxonomy.classify_text(
        exit_code=127,
        provider_text="",
        model_text="",
    )

    assert result.signal.value == "executable_not_found"


def test_shell_command_not_found_classifies_as_executable_not_found_signal() -> None:
    result = outcome_taxonomy.classify_text(
        exit_code=1,
        provider_text="bash: copilot: command not found",
        model_text="",
    )

    assert result.signal.value == "executable_not_found"
    assert "copilot" in result.detail


def test_bash_line_command_not_found_classifies_as_executable_not_found_signal() -> None:
    result = outcome_taxonomy.classify_text(
        exit_code=1,
        provider_text="bash: line 1: copilot: command not found",
        model_text="",
    )

    assert result.signal.value == "executable_not_found"


@pytest.mark.parametrize(
    "provider_text",
    [
        "exec: No such file or directory",
        "execvpe: No such file or directory",
        "execve: No such file or directory",
        "spawn: No such file or directory",
        "Popen: No such file or directory",
        "os.execvpe(...): [Errno 2] No such file or directory: 'copilot'",
        "subprocess.Popen(...): [Errno 2] No such file or directory: 'copilot'",
    ],
)
def test_exec_spawn_popen_enoent_classifies_as_executable_not_found_signal(
    provider_text: str,
) -> None:
    result = outcome_taxonomy.classify_text(
        exit_code=1,
        provider_text=provider_text,
        model_text="",
    )

    assert result.signal.value == "executable_not_found"
    assert "missing executable" in result.detail


def test_general_file_open_enoent_stays_none_signal() -> None:
    result = outcome_taxonomy.classify_text(
        exit_code=1,
        provider_text="open /tmp/work/spec.json: No such file or directory",
        model_text="",
    )

    assert result.signal.value == "none"


@pytest.mark.parametrize(
    "provider_text",
    [
        (
            "subprocess.Popen(..., cwd='/tmp/missing'): "
            "[Errno 2] No such file or directory: '/tmp/missing'"
        ),
        (
            "subprocess.Popen(..., cwd='missing-cwd'): "
            "[Errno 2] No such file or directory: 'missing-cwd'"
        ),
    ],
)
def test_launch_call_enoent_for_missing_cwd_stays_none_signal(provider_text: str) -> None:
    result = outcome_taxonomy.classify_text(
        exit_code=1,
        provider_text=provider_text,
        model_text="",
    )

    assert result.signal.value == "none"


@pytest.mark.parametrize(
    "shared_binary", ["bash", "sh", "git", "systemctl", "systemd-run"]
)
def test_launch_call_enoent_for_shared_launch_infrastructure_stays_none_signal(
    shared_binary: str,
) -> None:
    result = outcome_taxonomy.classify_text(
        exit_code=1,
        provider_text=(
            "subprocess.Popen(...): "
            f"[Errno 2] No such file or directory: '{shared_binary}'"
        ),
        model_text="",
    )

    assert result.signal.value == "none"


def test_spawn_log_open_enoent_stays_none_signal() -> None:
    result = outcome_taxonomy.classify_text(
        exit_code=1,
        provider_text="open /tmp/spawn.log: No such file or directory",
        model_text="",
    )

    assert result.signal.value == "none"


def test_structured_signals_still_win_before_exit_127_text_classification() -> None:
    rate_limited = classify_provider_failure(
        exit_code=127, output=_STRUCTURED_RATE_LIMIT_LOG
    )
    interrupted = classify_provider_failure(exit_code=127, output=_INTERRUPTED_LOG)

    assert rate_limited.outcome.value == "rate_limited"
    assert rate_limited.authority.value == "structured"
    assert interrupted.authority.value == "structured"
    assert interrupted.outcome.value == "unknown"


@pytest.mark.parametrize(
    ("provider_text", "model_text"),
    [
        ("HTTP 404 Not Found", ""),
        ("model 'mai-code-1-flash-picker' not found", ""),
        ("open build/output.log: No such file or directory", ""),
        ("", "bash: copilot: command not found"),
        ("bash: pytest: command not found", ""),
        ("bash: line 1: pytest: command not found", ""),
    ],
)
def test_executable_not_found_requires_provider_side_launch_evidence(
    provider_text: str, model_text: str
) -> None:
    result = outcome_taxonomy.classify_text(
        exit_code=1,
        provider_text=provider_text,
        model_text=model_text,
    )

    assert result.signal.value != "executable_not_found"


def test_poll_headless_done_projects_effort_not_supported_into_slice_gate_reason(
    tmp_path: Path,
) -> None:
    updated, manifest = _poll_and_render_slice(
        tmp_path,
        slice_id="slice-effort",
        log_text=_COPILOT_EFFORT_UNSUPPORTED,
        exit_code=1,
    )

    assert updated["provider_outcome"]["outcome"] == "effort_not_supported"
    assert updated["provider_outcome"]["authority"] != "hint"
    assert manifest["gate_reason"] == "builder-failed-effort_not_supported"
    assert manifest["provider_outcome"]["outcome"] == "effort_not_supported"


def test_poll_headless_done_projects_exit_127_empty_log_as_executable_not_found(
    tmp_path: Path,
) -> None:
    updated, manifest = _poll_and_render_slice(
        tmp_path,
        slice_id="slice-exec-127",
        log_text="",
        exit_code=127,
    )

    assert updated["provider_outcome"]["outcome"] == "executable_not_found"
    assert updated["provider_outcome"]["authority"] != "hint"
    assert manifest["gate_reason"] == "builder-failed-executable_not_found"
    assert manifest["provider_outcome"]["outcome"] == "executable_not_found"


def test_poll_headless_done_projects_missing_launch_handle_as_launch_failed(
    tmp_path: Path,
) -> None:
    updated, manifest = _poll_and_render_slice(
        tmp_path,
        slice_id="slice-missing-handle",
        log_text=None,
        exit_code=None,
        pid=None,
    )

    assert updated["provider_outcome"]["outcome"] == "launch_failed"
    assert updated["provider_outcome"]["authority"] != "hint"
    assert (
        updated["provider_outcome"]["reason"]
        == "launch handle missing: pid=None, log_path=None"
    )
    assert updated["runtime_diagnostic"] == {
        "reason": "launch-failed",
        "detail": "launch handle missing: pid=None, log_path=None",
        "source": "dispatcher.poll_headless_done:launch",
        "job_id": str(updated["job_id"]),
    }
    assert manifest["gate_reason"] == "builder-failed-launch_failed"
    assert manifest["provider_outcome"]["outcome"] == "launch_failed"


def test_autonomy_fail_launching_job_persists_launch_failed_runtime_diagnostic(
    tmp_path: Path,
) -> None:
    class Registry:
        def __init__(self) -> None:
            self.updated = None

        def update_status(self, job_id: str, status: str) -> dict:
            raise AssertionError("legacy update_status path should not be used")

        def update_headless_result(self, job_id: str, **kwargs) -> dict:
            self.updated = {"job_id": job_id, **kwargs}
            return self.updated

    registry = Registry()
    dispatcher = type("D", (), {"_registry": registry})()
    missing_cwd = tmp_path / "missing-cwd"

    autonomy._fail_launching_job(
        dispatcher,
        {"job_id": "slice-launch-1"},
        executor="copilot",
        model_id="mai-code-1-flash-picker",
        exc=FileNotFoundError(2, "No such file or directory", str(missing_cwd)),
    )

    assert registry.updated == {
        "job_id": "slice-launch-1",
        "status": "failed",
        "exit_code": 1,
        "executor": "copilot",
        "model_id": "mai-code-1-flash-picker",
        "provider_outcome": {
            "outcome": "launch_failed",
            "authority": "structured",
            "reason": (
                "launch failed before attach_launch_handle: FileNotFoundError: "
                f"[Errno 2] No such file or directory: '{missing_cwd}'"
            ),
            "retryable": False,
        },
        "runtime_diagnostic": {
            "reason": "launch-failed",
            "detail": (
                "FileNotFoundError: "
                f"[Errno 2] No such file or directory: '{missing_cwd}'"
            ),
            "source": "autonomy.dispatch_ready:launch",
            "job_id": "slice-launch-1",
        },
    }


def test_autonomy_fail_launching_job_without_exception_still_marks_job_failed() -> None:
    class Registry:
        def __init__(self) -> None:
            self.updated = None

        def update_headless_result(self, job_id: str, **kwargs) -> dict:
            self.updated = {"job_id": job_id, **kwargs}
            return self.updated

    registry = Registry()
    dispatcher = type("D", (), {"_registry": registry})()

    autonomy._fail_launching_job(
        dispatcher,
        {"job_id": "slice-launch-0"},
        executor="copilot",
        model_id="mai-code-1-flash-picker",
    )

    assert registry.updated is not None
    assert registry.updated["status"] == "failed"
    assert registry.updated["provider_outcome"]["outcome"] == "launch_failed"
    assert registry.updated["provider_outcome"]["authority"] == "structured"
    assert (
        registry.updated["provider_outcome"]["reason"]
        == "launch failed before attach_launch_handle"
    )
    assert (
        registry.updated["runtime_diagnostic"]["detail"]
        == "launch failed before attach_launch_handle"
    )


def test_autonomy_fail_launching_job_classifies_missing_executable_exception_as_executable_not_found() -> None:
    class Registry:
        def __init__(self) -> None:
            self.updated = None

        def update_headless_result(self, job_id: str, **kwargs) -> dict:
            self.updated = {"job_id": job_id, **kwargs}
            return self.updated

    registry = Registry()
    dispatcher = type("D", (), {"_registry": registry})()

    autonomy._fail_launching_job(
        dispatcher,
        {"job_id": "slice-launch-2", "worktree": "/tmp/worktree"},
        executor="copilot",
        model_id="mai-code-1-flash-picker",
        exc=FileNotFoundError(2, "No such file or directory", "copilot"),
    )

    assert registry.updated["provider_outcome"]["outcome"] == "executable_not_found"
    assert registry.updated["provider_outcome"]["authority"] == "structured"
    assert registry.updated["runtime_diagnostic"]["reason"] == "launch-failed"


def test_workflow_launch_exception_persists_launch_failed_through_resume(
    tmp_path: Path,
) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    dispatcher = _WorkflowDispatcher(registry, worktree)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "copilot",
                "model_id": "mai-code-1-flash-picker",
                "independence_domain": "github",
                "capabilities": ["build"],
            }
        ]
    )
    missing_cwd = tmp_path / "missing-cwd"

    with pytest.raises(FileNotFoundError):
        manager.dispatch_workflow_card(
            dispatcher,
            run=run,
            identities=identities,
            launcher_factory=lambda _identity: _WorkflowLauncher(
                "copilot",
                "mai-code-1-flash-picker",
                exc=FileNotFoundError(2, "No such file or directory", str(missing_cwd)),
            ),
            coordinator_root=tmp_path / "coordinator",
        )

    reloaded = JobRegistry(state_path=state)
    job = reloaded.list_jobs()[-1]
    assert job["provider_outcome"]["outcome"] == "launch_failed"
    assert job["runtime_diagnostic"] == {
        "reason": "launch-failed",
        "detail": (
            "FileNotFoundError: "
            f"[Errno 2] No such file or directory: '{missing_cwd}'"
        ),
        "source": "manager._dispatch_workflow_card:launch",
        "job_id": str(job["job_id"]),
    }

    resumed = manager.resume_workflow_run(
        _WorkflowDispatcher(reloaded, worktree),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=tmp_path / "coordinator",
    )

    assert resumed["reason"] == "job-failed-launch_failed"
    assert resumed["provider_outcome"] == "launch_failed"
    assert resumed["provider_outcome_authority"] == "structured"
    persisted = reloaded.get_workflow_run(run.run_id)
    assert persisted.needs_human_reason["context"]["executor"] == "copilot"
    assert persisted.needs_human_reason["context"]["model"] == "mai-code-1-flash-picker"
    assert persisted.needs_human_reason["context"]["effort"] == "unknown"


def test_workflow_launch_exception_missing_shared_launch_infrastructure_stays_launch_failed_without_reroute(
    tmp_path: Path,
) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    dispatcher = _WorkflowDispatcher(registry, worktree)
    identities = _two_builder_identities()

    with pytest.raises(FileNotFoundError):
        manager.dispatch_workflow_card(
            dispatcher,
            run=run,
            identities=identities,
            launcher_factory=lambda identity: _WorkflowLauncher(
                identity.executor,
                identity.model_id,
                exc=FileNotFoundError(2, "No such file or directory", "bash"),
            ),
            coordinator_root=tmp_path / "coordinator",
        )

    reloaded = JobRegistry(state_path=state)
    assert len(reloaded.list_jobs()) == 1

    resumed = manager.resume_workflow_run(
        _WorkflowDispatcher(reloaded, worktree),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=tmp_path / "coordinator",
    )

    assert resumed["reason"] == "job-failed-launch_failed"
    assert resumed["provider_outcome"] == "launch_failed"
    assert resumed["provider_outcome_authority"] == "structured"
    assert len(reloaded.list_jobs()) == 1


def test_effort_not_supported_reroutes_after_real_poll_classification(
    tmp_path: Path,
) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    worktree = tmp_path / "wt"
    base_head = _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    failed = _seed_workflow_job_from_log(
        registry,
        run=run,
        worktree=worktree,
        base_head=base_head,
        executor="copilot",
        model_id="mai-code-1-flash-picker",
        domain="github",
        log_text=_COPILOT_EFFORT_UNSUPPORTED,
        exit_code=1,
    )

    assert failed["provider_outcome"]["outcome"] == "effort_not_supported"

    reloaded = JobRegistry(state_path=state)
    dispatcher = _WorkflowDispatcher(reloaded, worktree)
    identities = _two_builder_identities()

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=tmp_path / "coordinator",
    )

    assert result["reason"] == "provider-failure-retry"
    assert result["provider_outcome"] == "effort_not_supported"
    assert result["provider_retry_count"] == 1

    replacement = reloaded.get_job(result["job_id"])
    assert replacement["executor"] == "claude"
    assert replacement["independence_domain"] == "anthropic"


def test_effort_not_supported_without_alternative_candidate_stops_in_needs_human(
    tmp_path: Path,
) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    worktree = tmp_path / "wt"
    base_head = _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    _seed_workflow_job_from_log(
        registry,
        run=run,
        worktree=worktree,
        base_head=base_head,
        executor="copilot",
        model_id="mai-code-1-flash-picker",
        domain="github",
        log_text=_COPILOT_EFFORT_UNSUPPORTED,
        exit_code=1,
    )

    reloaded = JobRegistry(state_path=state)
    dispatcher = _WorkflowDispatcher(reloaded, worktree)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "copilot",
                "model_id": "mai-code-1-flash-picker",
                "independence_domain": "github",
                "capabilities": ["build"],
            }
        ]
    )
    jobs_before = len(reloaded.list_jobs())

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=tmp_path / "coordinator",
    )

    assert result["reason"] == "job-failed-effort_not_supported"
    assert result["provider_outcome"] == "effort_not_supported"
    assert len(reloaded.list_jobs()) == jobs_before
    persisted = reloaded.get_workflow_run(run.run_id)
    assert persisted.needs_human_reason["context"]["executor"] == "copilot"
    assert persisted.needs_human_reason["context"]["model"] == "mai-code-1-flash-picker"
    assert persisted.needs_human_reason["context"]["effort"] == "xhigh"


def test_effort_not_supported_can_reroute_to_same_executor_different_model(
    tmp_path: Path,
) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    worktree = tmp_path / "wt"
    base_head = _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    _seed_workflow_job_from_log(
        registry,
        run=run,
        worktree=worktree,
        base_head=base_head,
        executor="copilot",
        model_id="mai-code-1-flash-picker",
        domain="github",
        log_text=_COPILOT_EFFORT_UNSUPPORTED,
        exit_code=1,
    )

    reloaded = JobRegistry(state_path=state)
    dispatcher = _WorkflowDispatcher(reloaded, worktree)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "copilot",
                "model_id": "mai-code-1-flash-picker",
                "independence_domain": "github",
                "capabilities": ["build"],
            },
            {
                "executor": "copilot",
                "model_id": "gpt-5.4",
                "independence_domain": "github",
                "capabilities": ["build"],
            },
        ]
    )

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=tmp_path / "coordinator",
    )

    assert result["reason"] == "provider-failure-retry"
    replacement = reloaded.get_job(result["job_id"])
    assert replacement["executor"] == "copilot"
    assert replacement["model_id"] == "gpt-5.4"


def test_runtime_contract_failures_still_do_not_reroute_retryable_outages(
    tmp_path: Path,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    base_head = _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    job = registry.create_job(
        task=f"seed-{run.run_id}-copilot",
        persona="builder",
        branch=f"feature/{run.work_id}",
        pane="",
        worktree=str(worktree),
        dispatch_head=base_head,
        executor="copilot",
        model_id="mai-code-1-flash-picker",
        independence_domain="github",
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
        str(job["job_id"]),
        status="failed",
        exit_code=1,
        provider_outcome={
            "outcome": "rate_limited",
            "authority": "text_signal",
            "reason": "structured rate limit already observed",
            "retryable": True,
        },
        runtime_diagnostic={
            "reason": "runtime-prompt-cleanup-invalid",
            "detail": "private prompt path is malformed",
            "source": "dispatcher._finalize_headless",
            "job_id": str(job["job_id"]),
        },
    )
    identities = _two_builder_identities()

    result = manager.resume_workflow_run(
        _WorkflowDispatcher(registry, worktree),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=tmp_path / "coordinator",
    )

    assert result["reason"] == "runtime-contract-failed"
    assert result.get("provider_outcome") is None
    assert len(registry.list_jobs()) == 1
