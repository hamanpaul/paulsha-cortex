"""#839：quota-aware admission 接線到 `manager._dispatch_workflow_card`。

沿用 `test_executor_backoff_workflow_lane.py` 的 dispatch harness
（`JobRegistry`＋fake launcher／worktree creator），驗證：

- baseline／shadow：`quota_admission_context` 缺席，或給了但未 opt-in，派工
  結果逐字不變（不因為額度不足就擋派）。
- opt-in、獨立池合格替代可選（AC1）：top-ranked 候選的 pool 用盡，同池換
  model 一樣被拒；rank 第二、獨立池合格的候選改為選中並拿到 reservation。
- opt-in、全部候選不可行：zero job，回精確 wait 理由，不造假 job_id。
- 兩 Manager barrier 共 reservation authority：race 落敗不建 job、不留半額度
  （AC3，透過直接呼叫 authority 模擬第二個 instance 搶先）。
- fake executor 派前可用、spawn 時 429：settle(failed) 記消耗、釋回容量。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, quota_admission
from paulsha_cortex.coordinator import quota_observation as schema
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.quota_reservation import QuotaReservationAuthority
from paulsha_cortex.coordinator.quota_shadow import QuotaShadowService
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.runtime_preflight import ExecutorEnvironment
from paulsha_cortex.coordinator.workflow import WorkflowStep


def _step(phase: str, card: str, *, gate_result: str = "pending") -> WorkflowStep:
    persona = {"build": "builder", "verify": "reviewer", "review": "reviewer"}[phase]
    return WorkflowStep(
        phase=phase, persona=persona, card=card, executor=None, model=None, domain=None,
        inputs=(), outputs=(), gate_result=gate_result,
    )


def _make_run(registry: JobRegistry, *, workspace_root: Path):
    return registry._manager_create_workflow_run(
        work_id="quota-admission-839", repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "3" * 64, source_revision="4" * 64,
        workspace_root=str(workspace_root), combo="feature-oneshot", current_phase="build",
        steps=(_step("build", "subagent-build", gate_result="pending"),),
        issue_refs=(), openspec_refs=(), facets=(), gate_status="running",
    )


class _FakeWorktreeCreator:
    def __init__(self, path: Path):
        self._path = path

    def create(self, branch: str, base_sha: str | None = None, *, job_id: str | None = None) -> Path:
        return self._path


class _Launcher:
    def __init__(self, executor: str, model_id: str, *, fail_on_launch: bool = False) -> None:
        self._executor = executor
        self._model_id = model_id
        self._fail_on_launch = fail_on_launch

    def as_commit_required(self):
        return self

    def as_read_only(self):
        return self

    def as_review_only(self, *, terminal_kind: str):
        return self

    def executor_environment(self) -> ExecutorEnvironment:
        return ExecutorEnvironment(
            name=f"{self._executor}-workflow", interpreter=(sys.executable,),
            path=os.environ.get("PATH", ""), home=os.path.expanduser("~"),
            provider_identity=self._executor,
        )

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        if self._fail_on_launch:
            raise RuntimeError("fake-executor-429: rate limited at spawn time")
        return LaunchHandle(
            executor=self._executor, model_id=self._model_id, session_name=slice_id,
            pid=4242, log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


_FAILING_EXECUTORS: set[str] = set()


def _launcher_factory(identity):
    return _Launcher(identity.executor, identity.model_id, fail_on_launch=identity.executor in _FAILING_EXECUTORS)


def _two_builder_identities() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]},
            {"executor": "claude", "model_id": "claude-primary", "independence_domain": "anthropic", "capabilities": ["build"]},
        ]
    )


def _resolved_profile_key(run, step, identity, executor: str, model_id: str) -> str:
    binding, _ = manager._bind_workflow_execution_profile(run, step, identity, _Launcher(executor, model_id))
    return binding.resolved_key


def _pool_descriptor(*, account: str, pool: str, windows=(("short", 300_000),)) -> schema.PoolDescriptor:
    unit = {"unit_id": "token", "version": "1", "quantity_kind": "amount", "semantics_ref": "fixture:native-token/v1"}
    return schema.parse_pool_descriptor(
        {
            "schema_version": 1, "authority_id": "operator-budget-authority", "account_id": account,
            "pool_id": pool, "revision": "1", "authority_ref": "fixture:operator-pool-map/v1",
            "provenance_refs": ["fixture:pool-map/v1"], "units": [unit],
            "windows": [
                {"window_id": window_id, "kind": "rolling", "unit_ref": {"unit_id": "token", "version": "1"}, "duration_ms": duration_ms}
                for window_id, duration_ms in windows
            ],
        }
    )


def _pool_ref(descriptor: schema.PoolDescriptor) -> dict[str, str]:
    return {
        "authority_id": descriptor.authority_id, "account_id": descriptor.account_id,
        "pool_id": descriptor.pool_id, "revision": descriptor.revision,
    }


def _binding(descriptor: schema.PoolDescriptor, profile_key: str) -> schema.ProfilePoolBinding:
    return schema.parse_binding(
        {
            "schema_version": 1, "binding_id": f"binding-{profile_key[-8:]}", "revision": "1",
            "subject": {"kind": "profile", "profile_ref": {"state": "known", "value": {"schema_version": 1, "key": profile_key}}},
            "constraints": [
                {"state": "known", "value": {"pool_ref": _pool_ref(descriptor), "window_id": window["window_id"]}}
                for window in descriptor.to_dict()["windows"]
            ],
            "coverage": {"state": "complete", "gaps": []},
        },
        descriptors=(descriptor,),
    )


def _observe(shadow: QuotaShadowService, descriptor, window_id: str, value: str, *, profile_key: str, now_ms: int) -> None:
    payload = {
        "schema_version": 1,
        "observation_id": f"fixture-{descriptor.pool_id}-{window_id}-{profile_key[-8:]}-{now_ms}",
        "scope": {"state": "known", "value": {"pool_ref": _pool_ref(descriptor), "window_id": window_id}},
        "profile_ref": {"state": "known", "value": {"schema_version": 1, "key": profile_key}},
        "unit_ref": {"state": "known", "value": {"unit_id": "token", "version": "1"}},
        "window_instance": {"kind": "unknown", "reason": "missing-window-instance"},
        "measurement": {
            "kind": "remaining_snapshot", "metric_id": "remaining",
            "quantity": {"state": "observed", "amount": {"kind": "exact", "value": value}},
        },
        "observed_at_ms": {"state": "known", "value": now_ms},
        "received_at_ms": now_ms,
        "ttl_ms": {"state": "known", "value": 60_000},
        "reset_at_ms": {"state": "unknown", "reason": "missing-reset"},
        "source": {
            "source_id": "fixture-provider", "source_schema": "fixture-quota-v1",
            "adapter_version": "fixture-adapter-v1", "authority_ref": "fixture:provider-contract/v1",
            "method": "provider_status", "provenance_refs": ["fixture:source-document/v1"],
            "event_identity": {"state": "unknown", "reason": "provider-has-no-event-id"},
        },
        "coverage": {"state": "complete", "gaps": []},
    }
    observation = schema.parse_observation(payload, descriptors=(descriptor,), unit_catalog=())
    result = shadow.record_observation(observation.to_dict(), descriptors=(descriptor,), unit_catalog=())
    assert result.accepted == 1


def _dispatch(registry, run, identities, worktree, coordinator_root, *, quota_admission_context=None):
    dispatcher = type(
        "D", (), {"_registry": registry, "_git_runner": None, "_worktree_creator": _FakeWorktreeCreator(worktree)}
    )()
    return manager.dispatch_workflow_card(
        dispatcher, run=run, identities=identities, launcher_factory=_launcher_factory,
        coordinator_root=coordinator_root, quota_admission_context=quota_admission_context,
    )


def _init_worktree(path: Path) -> None:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "a.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "base"], check=True)


@pytest.fixture(autouse=True)
def _clear_failing_executors():
    _FAILING_EXECUTORS.clear()
    yield
    _FAILING_EXECUTORS.clear()


def test_baseline_without_quota_context_is_fully_unaffected(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = _two_builder_identities()
    result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator")
    assert result is not None
    assert "job_id" in result
    job = registry.get_job(result["job_id"])
    assert job["executor"] == "codex"


def test_shadow_mode_records_decision_but_never_blocks_dispatch(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    profile_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")

    descriptor = _pool_descriptor(account="acct-codex", pool="pool-codex")
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "0", profile_key=profile_key, now_ms=int(time.time() * 1000))
    ctx = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, profile_key),), environment={},  # PSC_QUOTA_ADMISSION_ENFORCE 未設 -> shadow
    )

    result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)
    assert result is not None
    assert "job_id" in result  # 額度為 0（不可行）也照常派工——shadow 不擋
    job = registry.get_job(result["job_id"])
    assert job["executor"] == "codex"

    decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=f"{run.run_id}:{step.card}:n0", profile_key=profile_key,
    )
    decision = ctx.store.get(decision_id)
    assert decision is not None
    assert decision.mode == "shadow"
    assert decision.outcome == "admit"
    updated_run = registry.get_workflow_run(run.run_id)
    assert updated_run.quota_admission["builder"]["mode"] == "shadow"


def test_opt_in_independent_pool_alternative_is_selected_same_pool_alternative_rejected(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    candidates = manager._workflow_identity_candidates(run, step, identities)
    codex_identity = next(c for c in candidates if c.executor == "codex")
    claude_identity = next(c for c in candidates if c.executor == "claude")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")
    claude_key = _resolved_profile_key(run, step, claude_identity, "claude", "claude-primary")

    exhausted = _pool_descriptor(account="acct-codex", pool="pool-codex")
    independent = _pool_descriptor(account="acct-claude", pool="pool-claude")
    now_ms = int(time.time() * 1000)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, exhausted, "short", "0", profile_key=codex_key, now_ms=now_ms)
    _observe(shadow, independent, "short", "50", profile_key=claude_key, now_ms=now_ms)
    ctx = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(exhausted, independent), unit_catalog=(),
        bindings=(_binding(exhausted, codex_key), _binding(independent, claude_key)),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)
    assert result is not None
    assert "job_id" in result
    job = registry.get_job(result["job_id"])
    assert job["executor"] == "claude"  # 第一名 codex 池用盡被跳過，改選獨立池的 claude

    decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=f"{run.run_id}:{step.card}:n0", profile_key=claude_key,
    )
    decision = ctx.store.get(decision_id)
    assert decision is not None
    assert decision.mode == "enforced"
    assert decision.reservation_id is not None
    assert len(decision.excluded) == 1
    assert decision.excluded[0]["executor"] == "codex"
    status = ctx.authority.status(decision.reservation_id, now_ms=now_ms + 1)
    assert status.state == "bound"
    assert status.job_id == result["job_id"]


def test_opt_in_all_candidates_infeasible_returns_zero_job_with_precise_reason(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    candidates = manager._workflow_identity_candidates(run, step, identities)
    codex_identity = next(c for c in candidates if c.executor == "codex")
    claude_identity = next(c for c in candidates if c.executor == "claude")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")
    claude_key = _resolved_profile_key(run, step, claude_identity, "claude", "claude-primary")

    descriptor_a = _pool_descriptor(account="acct-codex", pool="pool-codex")
    descriptor_b = _pool_descriptor(account="acct-claude", pool="pool-claude")
    now_ms = int(time.time() * 1000)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor_a, "short", "0", profile_key=codex_key, now_ms=now_ms)
    _observe(shadow, descriptor_b, "short", "0", profile_key=claude_key, now_ms=now_ms)
    ctx = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor_a, descriptor_b), unit_catalog=(),
        bindings=(_binding(descriptor_a, codex_key), _binding(descriptor_b, claude_key)),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)
    assert result is not None
    assert "job_id" not in result
    assert result["reason"] == "quota-admission-insufficient"
    assert registry.list_jobs() == []
    updated_run = registry.get_workflow_run(run.run_id)
    assert "needs_human" in updated_run.facets


def test_spawn_time_429_after_bind_settles_failed_and_frees_capacity(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")

    descriptor = _pool_descriptor(account="acct-codex", pool="pool-codex")
    now_ms = int(time.time() * 1000)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=codex_key, now_ms=now_ms)
    ctx = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, codex_key),), environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    _FAILING_EXECUTORS.add("codex")
    with pytest.raises(RuntimeError, match="fake-executor-429"):
        _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)

    decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=f"{run.run_id}:{step.card}:n0", profile_key=codex_key,
    )
    decision = ctx.store.get(decision_id)
    assert decision is not None
    assert decision.reservation_id is not None
    status = ctx.authority.status(decision.reservation_id, now_ms=now_ms + 1)
    assert status.state == "settled"
    committed = ctx.authority.committed(now_ms=now_ms + 1)
    key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    assert committed.get(key, "0") == "0"
    # job 本身仍如實記為 failed——quota admission 不覆寫既有失敗分類。
    job = registry.list_jobs()[0]
    assert job["status"] == "failed"


def test_ac3_two_instances_race_for_last_unit_only_one_gets_bound_job(tmp_path: Path) -> None:
    """兩個 Manager instance 共用同一份 reservation authority 檔案；模擬第二個
    instance 在第一個 instance 完成 reserve() 之後，搶先把剩餘容量吃光——第一
    個 instance 隨後嘗試 bind 前，其實已經在 reserve() 當下就該被擋下（見
    #838 all-or-none 語意）。這裡改用直接呼叫 authority 模擬第二個 instance
    的搶先 reserve，驗證 dispatch 端在 race 落敗時不建立 job、不留半額度。
    """
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    claude_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "claude")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")
    claude_key = _resolved_profile_key(run, step, claude_identity, "claude", "claude-primary")

    descriptor = _pool_descriptor(account="acct-shared", pool="pool-shared")
    now_ms = int(time.time() * 1000)
    shadow = QuotaShadowService.in_memory()
    # 同一個共享 pool 只有一個 remaining 值——兩個候選各自的 binding 都指向
    # 這同一個 pool/window，不需要（也不該）為每個 profile 各記一筆觀測。
    _observe(shadow, descriptor, "short", "1", profile_key=codex_key, now_ms=now_ms)
    authority_path = tmp_path / "reservations.jsonl"
    ctx = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(authority_path),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, codex_key), _binding(descriptor, claude_key)),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    # 另一個 instance 先搶走這個 pool/window 僅剩的 1 單位（不同 run/card/attempt，
    # 但同一個 pool_ref/window_id——正是「共享額度」的核心情境）。
    from paulsha_cortex.coordinator.quota_reservation import PoolDemand

    other_authority = QuotaReservationAuthority(authority_path)
    other_result = other_authority.reserve(
        run_id="other-run", card_id="other-card", decision_id="adm:v1:" + "f" * 64,
        attempt_id="other-attempt",
        pools=(PoolDemand(pool_ref=_pool_ref(descriptor), window_id="short", amount="1"),),
        capacity_by_pool={(tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short"): "1"},
        observation_version="obs-other", demand_version="demand-other", lease_ms=60_000, now_ms=now_ms,
    )
    assert other_result.status == "granted"

    result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)
    assert result is not None
    assert "job_id" not in result  # 兩個候選都撞到同一個已被搶走的 pool，race 落敗不建 job
    assert result["reason"] == "quota-admission-insufficient"
    assert registry.list_jobs() == []


def test_race_loss_on_top_ranked_candidate_falls_back_to_independent_pool_candidate(tmp_path: Path) -> None:
    """對抗審查 MAJOR（manager.py:13643）：排名第一的可行候選在原子預留階段
    race 落敗時（另一個 instance 搶先吃光它自己那個池），不得直接回
    quota-admission-insufficient——必須排除該候選、依既有排序對下一個候選
    （這裡是獨立池的 claude）重試；claude 的池完全沒被動過，重試應該成功
    並 bind 出一個真正的 job。與 AC3（`test_ac3_two_instances_race_for_last_unit_only_one_gets_bound_job`）
    的差別：AC3 兩個候選共用同一個已耗盡的池，全部落敗；這裡只有排名第一的
    候選撞到 race，第二名的池是獨立、未受影響的。
    """
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    claude_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "claude")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")
    claude_key = _resolved_profile_key(run, step, claude_identity, "claude", "claude-primary")

    codex_pool = _pool_descriptor(account="acct-codex", pool="pool-codex")
    claude_pool = _pool_descriptor(account="acct-claude", pool="pool-claude")
    now_ms = int(time.time() * 1000)
    shadow = QuotaShadowService.in_memory()
    # 兩個候選各自的池在 shadow 投影裡都還很充裕——assess 階段兩者都可行；
    # race 只發生在原子預留這一層，shadow 的唯讀投影看不到它。
    _observe(shadow, codex_pool, "short", "10", profile_key=codex_key, now_ms=now_ms)
    _observe(shadow, claude_pool, "short", "10", profile_key=claude_key, now_ms=now_ms)
    authority_path = tmp_path / "reservations.jsonl"
    ctx = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(authority_path),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(codex_pool, claude_pool), unit_catalog=(),
        bindings=(_binding(codex_pool, codex_key), _binding(claude_pool, claude_key)),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    # 另一個 instance 先把 codex 自己的池吃光——claude 的池完全沒被動過。
    from paulsha_cortex.coordinator.quota_reservation import PoolDemand

    other_authority = QuotaReservationAuthority(authority_path)
    other_result = other_authority.reserve(
        run_id="other-run", card_id="other-card", decision_id="adm:v1:" + "e" * 64,
        attempt_id="other-attempt",
        pools=(PoolDemand(pool_ref=_pool_ref(codex_pool), window_id="short", amount="10"),),
        capacity_by_pool={(tuple(_pool_ref(codex_pool)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short"): "10"},
        observation_version="obs-other", demand_version="demand-other", lease_ms=60_000, now_ms=now_ms,
    )
    assert other_result.status == "granted"

    result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)
    assert result is not None
    assert "job_id" in result  # race 落敗換掉 codex，claude 的獨立池成功 bind
    job = registry.get_job(result["job_id"])
    assert job["executor"] == "claude"

    decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=f"{run.run_id}:{step.card}:n0", profile_key=claude_key,
    )
    decision = ctx.store.get(decision_id)
    assert decision is not None
    assert decision.mode == "enforced"
    assert decision.reservation_id is not None
    assert len(decision.excluded) == 1
    assert decision.excluded[0]["executor"] == "codex"
    assert decision.excluded[0]["exclusion_reason"] == "reservation-denied"
    status = ctx.authority.status(decision.reservation_id, now_ms=now_ms + 1)
    assert status.state == "bound"
    assert status.job_id == result["job_id"]
