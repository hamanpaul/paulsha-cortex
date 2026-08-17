"""#384：LLM executor/provider 失敗的 typed semantics、bounded recovery 與
policy-aware fallback 在 workflow lane 的接線測試。

Root cause：`resume_workflow_run` 的 job-failed 分支（`manager.py`）過去把任何
executor 失敗一律壓平成寫死的 ``"job-failed"``＋``needs_human``，無分類、無
retry、無 backoff。本檔驗證：

- rate_limited／transient → bounded retry（`run.attempts` 持久化計數、逾限
  needs_human＋專屬 reason）。
- content／auth → 不盲目 retry，直接 needs_human 並帶上分類。
- retry 時在既有 candidate 順序上 re-route（`_provider_failure_reroute`），且
  不放寬 independence domain（policy-shopping 被擋）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, terminal_contract
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.provider_outcome import ProviderOutcome, SignalAuthority
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


def _step(phase: str, card: str, *, gate_result: str = "pending", domain: str | None = None) -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona=_PERSONA_BY_PHASE[phase],
        card=card,
        executor=None,
        model=None,
        domain=domain,
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


def _make_run(registry: JobRegistry, *, workspace_root: Path, steps, claim_key="claim:v1:" + "1" * 64):
    return registry._manager_create_workflow_run(
        work_id="demo",
        repo="acme/demo",
        claim_key=claim_key,
        source_revision="2" * 64,
        workspace_root=str(workspace_root),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=("acme/demo#1",),
        openspec_refs=("demo",),
        facets=(),
        gate_status="running",
    )


def _seed_builder_job(
    registry: JobRegistry,
    *,
    run,
    worktree: Path,
    base_head: str,
    executor: str,
    model_id: str,
    domain: str,
    outcome: ProviderOutcome | None,
    reason: str = "synthetic",
) -> dict:
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
    provider_outcome_payload = None
    if outcome is not None:
        retryable = outcome in {ProviderOutcome.RATE_LIMITED, ProviderOutcome.TRANSIENT}
        provider_outcome_payload = {
            "outcome": outcome.value,
            "authority": SignalAuthority.TEXT_SIGNAL.value,
            "reason": reason,
            "retryable": retryable,
        }
    registry.update_headless_result(
        job["job_id"], status="failed", exit_code=1, provider_outcome=provider_outcome_payload
    )
    return registry.get_job(job["job_id"])


class _FakeWorktreeCreator:
    def __init__(self, path: Path):
        self._path = path

    def create(self, branch: str, base_sha: str | None = None, *, job_id: str | None = None) -> Path:
        return self._path


class _Launcher:
    def __init__(self, executor: str, model_id: str):
        self._executor = executor
        self._model_id = model_id

    def as_commit_required(self):
        return self

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


# --------------------------------------------------------------- bounded retry + re-route


def test_rate_limited_failure_triggers_bounded_retry_and_reroutes_to_next_candidate(
    tmp_path: Path,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    base_head = _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path, steps=_build_only_steps())
    _seed_builder_job(
        registry,
        run=run,
        worktree=worktree,
        base_head=base_head,
        executor="codex",
        model_id="gpt-primary",
        domain="openai",
        outcome=ProviderOutcome.RATE_LIMITED,
    )
    identities = _two_builder_identities()
    dispatcher = _ResumeDispatcher(registry, worktree)

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=tmp_path / "coordinator",
    )

    assert result["reason"] == "provider-failure-retry"
    assert result["provider_outcome"] == "rate_limited"
    assert result["provider_retry_count"] == 1
    # 不得進 needs_human——bounded retry 尚未耗盡，run 仍是可繼續推進的狀態。
    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" not in persisted.facets
    assert persisted.attempts[manager._provider_retry_attempt_key("subagent-build")] == 1

    # re-route：新 job 換成候選清單裡的下一個 identity（claude），不是原本
    # 剛失敗的 codex——在既有 candidate 順序上換人，而不是盲目用同一個失敗中
    # 的 identity 重打。
    new_job = registry.get_job(result["job_id"])
    assert new_job["executor"] == "claude"
    assert new_job["independence_domain"] == "anthropic"


def test_provider_retry_bounded_and_exhaustion_reaches_needs_human(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    base_head = _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path, steps=_build_only_steps())
    _seed_builder_job(
        registry,
        run=run,
        worktree=worktree,
        base_head=base_head,
        executor="codex",
        model_id="gpt-primary",
        domain="openai",
        outcome=ProviderOutcome.TRANSIENT,
    )
    identities = _two_builder_identities()
    dispatcher = _ResumeDispatcher(registry, worktree)

    reasons: list[str] = []
    counts: list[int | None] = []
    for _ in range(terminal_contract.MAX_PROVIDER_RETRIES + 2):
        result = manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=identities,
            launcher_factory=_launcher_factory,
            coordinator_root=tmp_path / "coordinator",
        )
        reasons.append(result["reason"])
        counts.append(result.get("provider_retry_count"))
        if result["reason"] != "provider-failure-retry":
            break
        # 讓最新那個 replacement job 也失敗，逼近上限。
        latest_job_id = result["job_id"]
        job = registry.get_job(latest_job_id)
        registry.update_headless_result(
            latest_job_id,
            status="failed",
            exit_code=1,
            provider_outcome={
                "outcome": "transient",
                "authority": "text_signal",
                "reason": "synthetic",
                "retryable": True,
            },
        )

    assert reasons.count("provider-failure-retry") == terminal_contract.MAX_PROVIDER_RETRIES
    assert reasons[-1] == "provider-retry-exhausted"
    assert counts[: terminal_contract.MAX_PROVIDER_RETRIES] == list(
        range(1, terminal_contract.MAX_PROVIDER_RETRIES + 1)
    )

    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" in persisted.facets
    assert (
        persisted.attempts[manager._provider_retry_attempt_key("subagent-build")]
        == terminal_contract.MAX_PROVIDER_RETRIES
    )

    # 逾限後（未 operator_resume）再打一次：facets 已含 needs_human，非
    # operator_resume 的呼叫不得靜默繼續派工，必須擋下等人工介入。
    guarded = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=tmp_path / "coordinator",
    )
    assert guarded["reason"] == "operator-resume-required"


@pytest.mark.parametrize("outcome", [ProviderOutcome.AUTH, ProviderOutcome.CONTENT])
def test_non_retryable_outcomes_go_straight_to_needs_human_without_dispatching_replacement(
    tmp_path: Path, outcome: ProviderOutcome
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    base_head = _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path, steps=_build_only_steps())
    _seed_builder_job(
        registry,
        run=run,
        worktree=worktree,
        base_head=base_head,
        executor="codex",
        model_id="gpt-primary",
        domain="openai",
        outcome=outcome,
    )
    identities = _two_builder_identities()
    dispatcher = _ResumeDispatcher(registry, worktree)
    jobs_before = len(registry.list_jobs())

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=tmp_path / "coordinator",
    )

    assert result["reason"] == f"job-failed-{outcome.value}"
    assert result["provider_outcome"] == outcome.value
    # 不盲目 retry：沒有新 job 被 dispatch。
    assert len(registry.list_jobs()) == jobs_before
    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" in persisted.facets


def test_unclassified_failure_keeps_legacy_job_failed_reason(tmp_path: Path) -> None:
    """Job 沒有走過 dispatcher 分類管線（例如舊狀態檔／直接呼叫 registry）時，
    `classification_from_job` 回 None，行為必須與 #384 之前完全一致——不偽造
    一個 outcome、不改變既有 reason 字面值，保護所有既有呼叫端。
    """

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    base_head = _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path, steps=_build_only_steps())
    _seed_builder_job(
        registry,
        run=run,
        worktree=worktree,
        base_head=base_head,
        executor="codex",
        model_id="gpt-primary",
        domain="openai",
        outcome=None,
    )
    identities = _two_builder_identities()
    dispatcher = _ResumeDispatcher(registry, worktree)

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=identities,
        launcher_factory=_launcher_factory,
        coordinator_root=tmp_path / "coordinator",
    )

    assert result["reason"] == "job-failed"
    assert "provider_outcome" not in result


# --------------------------------------------------------------- domain preservation (no policy-shopping)


def test_reroute_never_returns_identity_outside_existing_candidate_list(tmp_path: Path) -> None:
    """`_provider_failure_reroute` 只能在 `_workflow_identity_candidates` 既有
    （domain-filtered）順序上選人，不能生出一個不在清單裡的 identity——這是
    「不放寬 independence domain」的直接斷言：無論回傳什麼，都必須是既有合法
    候選之一。
    """

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _make_run(registry, workspace_root=tmp_path, steps=_build_only_steps())
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    candidates = manager._workflow_identity_candidates(run, step, identities)

    classification = manager.provider_outcome.ProviderFailureClassification(
        outcome=ProviderOutcome.RATE_LIMITED,
        authority=SignalAuthority.TEXT_SIGNAL,
        reason="synthetic",
    )
    rerouted = manager._provider_failure_reroute(
        run, step, identities, failed_job={"executor": "codex"}, classification=classification
    )
    assert rerouted is not None
    assert rerouted in candidates
    assert rerouted.executor != "codex"


def test_reroute_with_single_candidate_returns_none_not_a_policy_shopped_identity(tmp_path: Path) -> None:
    """只有一個合格候選（剛好就是失敗中的那個）時，reroute 找不到「合法的另一
    個」，必須回 None（呼叫端退回既有 `_select_workflow_identity`，仍然選中
    同一個 identity——這是唯一合法選擇，不是靠 reroute 生出一個域外身分）。
    """

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _make_run(registry, workspace_root=tmp_path, steps=_build_only_steps())
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            }
        ]
    )
    step = manager._current_workflow_step(run)
    classification = manager.provider_outcome.ProviderFailureClassification(
        outcome=ProviderOutcome.RATE_LIMITED,
        authority=SignalAuthority.TEXT_SIGNAL,
        reason="synthetic",
    )
    rerouted = manager._provider_failure_reroute(
        run, step, identities, failed_job={"executor": "codex"}, classification=classification
    )
    assert rerouted is None


def test_reroute_for_reviewer_never_crosses_into_builder_domain(tmp_path: Path) -> None:
    """Reviewer 的候選清單本就排除跟 builder 同 domain 的 identity
    （independence domain 規則）；`_provider_failure_reroute` 完全複用這份既有
    清單，因此即使 reviewer 唯一候選失敗，也絕不會 re-route 回 builder 用過的
    domain——這正是 plan 提到的 policy-shopping 防線（例如 Codex builder 失敗
    後，不得讓 Codex 又跑去當 reviewer）。
    """

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    steps = (
        _step("claim", "manager-claim", gate_result="passed"),
        _step("define", "planner-define", gate_result="passed"),
        _step("plan", "planner-plan", gate_result="passed"),
        _step("build", "subagent-build", gate_result="passed", domain="openai"),
        _step("verify", "reviewer-verify", gate_result="pending"),
        _step("review", "reviewer-review", gate_result="pending"),
        _step("ship", "manager-ship", gate_result="pending"),
    )
    run = registry._manager_create_workflow_run(
        work_id="demo",
        repo="acme/demo",
        claim_key="claim:v1:" + "2" * 64,
        source_revision="3" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        issue_refs=("acme/demo#1",),
        openspec_refs=("demo",),
        facets=(),
        gate_status="running",
    )
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build", "review"],
            },
            {
                "executor": "claude",
                "model_id": "claude-primary",
                "independence_domain": "anthropic",
                "capabilities": ["review"],
            },
        ]
    )
    step = manager._current_workflow_step(run)
    assert step.persona == "reviewer"
    candidates = manager._workflow_identity_candidates(run, step, identities)
    # 前提斷言：既有規則已經排除掉跟 builder 同 domain 的 codex。
    assert [c.executor for c in candidates] == ["claude"]

    classification = manager.provider_outcome.ProviderFailureClassification(
        outcome=ProviderOutcome.RATE_LIMITED,
        authority=SignalAuthority.TEXT_SIGNAL,
        reason="synthetic",
    )
    rerouted = manager._provider_failure_reroute(
        run, step, identities, failed_job={"executor": "claude"}, classification=classification
    )
    # 唯一候選（claude）本身就是失敗中的那個 -> None（回退 _select_workflow_identity
    # 仍選 claude，不會、也不能 re-route 回 codex/openai）。
    assert rerouted is None
