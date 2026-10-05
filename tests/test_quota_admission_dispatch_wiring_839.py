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
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, quota_admission
from paulsha_cortex.coordinator import executor_backoff
from paulsha_cortex.coordinator import quota_observation as schema
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.quota_reservation import QuotaReservationAuthority
from paulsha_cortex.coordinator.quota_shadow import QuotaShadowService
from paulsha_cortex.coordinator.registry import JobRegistry, RegistryRevisionConflict
from paulsha_cortex.coordinator.runtime_preflight import ExecutorEnvironment
from paulsha_cortex.coordinator.workflow import WorkflowStep


class _Structured429(RuntimeError):
    status_code = 429


def _step(phase: str, card: str, *, gate_result: str = "pending") -> WorkflowStep:
    persona = {"build": "builder", "verify": "reviewer", "review": "reviewer"}[phase]
    return WorkflowStep(
        phase=phase, persona=persona, card=card, executor=None, model=None, domain=None,
        inputs=(), outputs=(), gate_result=gate_result,
    )


def _make_run(registry: JobRegistry, *, workspace_root: Path, sized: bool = False):
    return registry._manager_create_workflow_run(
        work_id="quota-admission-839", repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "3" * 64, source_revision="4" * 64,
        workspace_root=str(workspace_root), combo="feature-oneshot", current_phase="build",
        steps=(_step("build", "subagent-build", gate_result="pending"),),
        issue_refs=(), openspec_refs=(), facets=(), gate_status="running",
        sizing_score=5 if sized else None,
        sizing_band="yellow" if sized else None,
    )


class _FakeWorktreeCreator:
    def __init__(self, path: Path):
        self._path = path

    def create(
        self,
        branch: str,
        base_sha: str | None = None,
        *,
        job_id: str | None = None,
        owner_identity: dict[str, str] | None = None,
        attempt_id: str | None = None,
    ) -> Path:
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
            raise _Structured429("fake-executor-429: rate limited at spawn time")
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


def _observe(
    shadow: QuotaShadowService, descriptor, window_id: str, value: str, *,
    profile_key: str, now_ms: int, reset_at_ms: int | None = None,
) -> None:
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
        "reset_at_ms": (
            {"state": "known", "value": reset_at_ms}
            if reset_at_ms is not None
            else {"state": "unknown", "reason": "missing-reset"}
        ),
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


def _freeze_wall_clock(monkeypatch: pytest.MonkeyPatch, *, now_ms: int) -> None:
    """凍結 `time.time()` 在 `now_ms`（毫秒）這一刻，讓整個測試裡的每一次取樣
    ——測試自己傳給 `_observe()` 的 `now_ms`，以及 `manager._dispatch_workflow_card`
    內部各處各自重新呼叫的 `int(time.time() * 1000)`（assess／reserve／bind／
    settle／release 都各自取樣一次，不是共用同一個值）——都讀到同一個時間點。

    沒有這個凍結時，CPU 滿載會拖長 `_init_worktree()` 的 subprocess／worktree
    建立與 dispatch 本身的耗時，讓 `_observe()` 記錄觀測的那一刻與 dispatch
    內部真正讀取 remaining 的那一刻之間的牆鐘落差變大；一旦落差超過 fixture
    觀測的 60 秒 TTL（見 `_observe` 的 `ttl_ms`），原本 sufficient 的額度就會
    被 `quota_shadow.project()` 判定成 stale／unknown（exclusion_reason
    `unknown-remaining-quota`），造成間歇性失敗（見 #839 dogfood：CPU 滿載下
    約一半回合失敗，無負載時逐次都通過）。凍結後這條路徑完全不受牆鐘影響，
    不論環境多慢都逐字可重現。
    """
    monkeypatch.setattr(time, "time", lambda: now_ms / 1000)


def _dispatch(
    registry, run, identities, worktree, coordinator_root, *,
    quota_admission_context=None, force_new_card=False,
):
    dispatcher = type(
        "D", (), {"_registry": registry, "_git_runner": None, "_worktree_creator": _FakeWorktreeCreator(worktree)}
    )()
    return manager.dispatch_workflow_card(
        dispatcher, run=run, identities=identities, launcher_factory=_launcher_factory,
        coordinator_root=coordinator_root, quota_admission_context=quota_admission_context,
        force_new_card=force_new_card,
    )


def _init_worktree(path: Path) -> None:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "a.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    # 固定 author/committer 時間戳──commit SHA 內含秒級時間，沒有固定時間時
    # 兩個獨立 worktree 只要初始化跨過牆鐘秒界就會產生不同的 `dispatch_head`
    # （CPU 滿載下 subprocess 變慢，更容易跨秒），讓比較兩個 worktree 派工
    # 結果的測試間歇性失敗；固定時間讓初始 commit 逐字可重現，不受牆鐘影響。
    commit_env = dict(os.environ)
    commit_env["GIT_AUTHOR_DATE"] = "2000-01-01T00:00:00Z"
    commit_env["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00Z"
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "base"], check=True, env=commit_env,
    )


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


def test_shadow_mode_records_decision_but_never_blocks_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    profile_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")

    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    descriptor = _pool_descriptor(account="acct-codex", pool="pool-codex")
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "0", profile_key=profile_key, now_ms=now_ms)
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
        mode="shadow",
    )
    decision = ctx.store.get(decision_id)
    assert decision is not None
    assert decision.mode == "shadow"
    assert decision.outcome == "admit"
    updated_run = registry.get_workflow_run(run.run_id)
    assert updated_run.quota_admission["builder"]["mode"] == "shadow"


def test_shadow_mode_store_record_failure_never_changes_dispatch_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """對抗審查第三輪 MAJOR（manager.py:14056）：shadow 模式下 decision store
    任何錯誤（含內容衝突／IO／損毀）都不得改變派工結果——舊實作 admit
    receipt 寫入前沒有先讀舊值，直接無條件 `store.record()`；同一個
    decision_id 的重送（retry）因為 `generated_at_ms` 每次都不同，會被
    `AdmissionDecisionStore` 判定成內容衝突而整段往外丟例外，讓 shadow
    （本該是完全旁觀、絕不改變既有派工結果的模式）也會被這個純診斷寫入
    炸掉整條 dispatch。這裡直接讓 `store.record()` 恆定丟例外（模擬任何
    store 寫入失敗），驗證：派工結果（是否派工、選中的 executor、run 的
    phase）與完全沒有 `quota_admission_context` 時逐字相同，
    `WorkflowRun.quota_admission` 也一樣維持 `None`（不是半吊子的診斷
    投影）。
    """
    identities = _two_builder_identities()

    registry_baseline = JobRegistry(state_path=tmp_path / "jobs-baseline.json")
    worktree_baseline = tmp_path / "wt-baseline"
    _init_worktree(worktree_baseline)
    run_baseline = _make_run(registry_baseline, workspace_root=tmp_path / "workspace-baseline")
    baseline_result = _dispatch(
        registry_baseline, run_baseline, identities, worktree_baseline, tmp_path / "coordinator-baseline",
    )
    assert baseline_result is not None
    assert "job_id" in baseline_result
    baseline_job = registry_baseline.get_job(baseline_result["job_id"])
    baseline_run_after = registry_baseline.get_workflow_run(run_baseline.run_id)

    registry_shadow = JobRegistry(state_path=tmp_path / "jobs-shadow.json")
    worktree_shadow = tmp_path / "wt-shadow"
    _init_worktree(worktree_shadow)
    run_shadow = _make_run(registry_shadow, workspace_root=tmp_path / "workspace-shadow")
    step = manager._current_workflow_step(run_shadow)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run_shadow, step, identities) if c.executor == "codex")
    profile_key = _resolved_profile_key(run_shadow, step, codex_identity, "codex", "gpt-primary")
    descriptor = _pool_descriptor(account="acct-codex-store-fail", pool="pool-codex-store-fail")
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=profile_key, now_ms=int(time.time() * 1000))
    ctx = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "reservations-store-fail.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions-store-fail.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, profile_key),), environment={},  # PSC_QUOTA_ADMISSION_ENFORCE 未設 -> shadow
    )

    def _boom(self, decision):
        raise RuntimeError("simulated decision store write failure")

    monkeypatch.setattr(quota_admission.AdmissionDecisionStore, "record", _boom)

    shadow_result = _dispatch(
        registry_shadow, run_shadow, identities, worktree_shadow, tmp_path / "coordinator-shadow",
        quota_admission_context=ctx,
    )
    assert shadow_result is not None
    assert "job_id" in shadow_result  # store 全程失敗仍照常派工——shadow 絕不擋派
    shadow_job = registry_shadow.get_job(shadow_result["job_id"])
    shadow_run_after = registry_shadow.get_workflow_run(run_shadow.run_id)

    # 派工結果逐字相同——只允許每個獨立 registry／run 自身內在就會不同的
    # 識別碼／路徑／時間戳差異，其餘欄位（executor／status／facets 等）必須
    # 完全一致。`attempt_id`（#1261）是每張 build job 各自的 owner attempt。
    allowed_volatile = {
        "job_id", "pid", "log_path", "session_name", "worktree", "prompt_path",
        "workflow_run_id", "workflow_repo_root", "workflow_input_root",
        "control_log_path", "created_at", "started_at", "attempt_id",
    }
    volatile_job_keys = {
        key for key in set(baseline_job) | set(shadow_job)
        if baseline_job.get(key) != shadow_job.get(key)
    }
    assert volatile_job_keys.issubset(allowed_volatile), volatile_job_keys
    assert baseline_job["executor"] == shadow_job["executor"] == "codex"
    assert baseline_job["model_id"] == shadow_job["model_id"]
    assert baseline_job["status"] == shadow_job["status"]

    assert set(baseline_result.keys()) == set(shadow_result.keys())

    # store 寫入從頭到尾都失敗——shadow 的診斷投影完全沒有落地，
    # `WorkflowRun.quota_admission` 和 baseline（完全沒有 context）一樣維持
    # `None`，不是半吊子的『寫了一半』狀態。
    assert baseline_run_after.quota_admission is None
    assert shadow_run_after.quota_admission is None


def test_opt_in_independent_pool_alternative_is_selected_same_pool_alternative_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
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
        mode="enforced",
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


@pytest.mark.parametrize("observed_remaining", ["0", None], ids=["insufficient", "unknown"])
def test_opt_in_all_candidates_infeasible_returns_zero_job_with_precise_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_remaining: str | None,
) -> None:
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
    clock_ms = [now_ms]
    monkeypatch.setattr(time, "time", lambda: clock_ms[0] / 1000)
    shadow = QuotaShadowService.in_memory()
    reset_at_ms = now_ms + 90_000
    if observed_remaining is not None:
        _observe(shadow, descriptor_a, "short", observed_remaining, profile_key=codex_key, now_ms=now_ms, reset_at_ms=reset_at_ms)
        _observe(shadow, descriptor_b, "short", observed_remaining, profile_key=claude_key, now_ms=now_ms, reset_at_ms=reset_at_ms)
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

    # 對抗審查第二輪 MAJOR（manager.py:11413）：全部候選被拒也要留一筆
    # decision receipt，並更新 WorkflowRun.quota_admission[persona] 診斷投影
    # ——不能只標 needs_human，讓額度層自己完全沒有留下任何證據。
    assert updated_run.quota_admission is not None
    projection = updated_run.quota_admission["builder"]
    assert projection["mode"] == "enforced"
    assert projection["outcome"] == "wait"
    decision = ctx.store.get(projection["decision_id"])
    assert decision is not None
    assert decision.mode == "enforced"
    assert decision.outcome == "wait"
    assert decision.reason == "quota-admission-insufficient"
    assert decision.retry_eligible is True
    assert decision.reset_at_ms == (reset_at_ms if observed_remaining is not None else None)
    assert {item["executor"] for item in decision.excluded} == {"codex", "claude"}
    assert manager.quota_wait_retry_is_eligible(run=updated_run, quota_admission_context=ctx)
    assert not manager._quota_wait_has_recovered_candidate(
        run=updated_run, identities=identities, launcher_factory=_launcher_factory,
        quota_admission_context=ctx,
    )

    # A retry-eligible quota wait exposes the same resume action accepted by the
    # formal operator resume path. When quota is still insufficient, resume must
    # retain the original wait receipt instead of appending a duplicate decision.
    from paulsha_cortex.coordinator import work_actions

    recovery_actions = work_actions._phase_recovery_actions(
        updated_run, registry, quota_decision_store=ctx.store,
    )
    assert "resume" in recovery_actions
    status_entry = manager.workflow_status_entry(registry, updated_run, quota_decision_store=ctx.store)
    assert "resume" in status_entry["next_actions"]
    assert "resume" in status_entry["next_step_hint"]
    from paulsha_cortex.coordinator import claim

    claim_decision = claim._resume_decision(claim.ClaimCandidate(
        authority=None, repo=updated_run.repo, work_id=updated_run.work_id,
        source_revisions=(updated_run.source_revision,), confirmed_todo=False,
        confirmed_issue=None, auto_label=False, active_run_id=updated_run.run_id,
        active_claim_key=updated_run.claim_key, active_status="needs_human",
        active_phase=updated_run.current_phase,
        active_recovery_actions=recovery_actions, active_pre_delivery=True,
    ))
    assert {"abandon", "resume"} <= set(claim_decision.next_actions)
    assert "resume" in claim_decision.next_step_hint
    from paulsha_cortex.monitor import providers

    monkeypatch.setattr(
        work_actions, "work_authority_projection_state", lambda **_kwargs: "available",
    )
    monitor_actions = providers._workflow_next_actions_projection(
        [updated_run.to_dict()], repo=updated_run.repo,
        job_rows=registry.list_jobs(), slice_rows=[], state_path=registry._state_path,
        quota_decision_store=ctx.store,
    )
    assert "resume" in monitor_actions[updated_run.work_id]["actions"]
    decisions_before = ctx.store.all_rows()
    dispatcher = type(
        "D", (), {"_registry": registry, "_git_runner": None,
                  "_worktree_creator": _FakeWorktreeCreator(worktree)}
    )()
    resumed = manager.resume_workflow_run(
        dispatcher, run_id=updated_run.run_id, identities=identities,
        launcher_factory=_launcher_factory, coordinator_root=tmp_path / "coordinator",
        operator_resume=True, quota_admission_context=ctx,
    )
    assert resumed["reason"] == "quota-admission-insufficient"
    still_waiting = registry.get_workflow_run(updated_run.run_id)
    assert still_waiting.quota_admission["builder"]["decision_id"] == projection["decision_id"]
    assert len(ctx.store.all_rows()) == len(decisions_before) == 1

    # A reset timestamp alone never releases the wait. A fresh sufficient
    # observation is required, after which the periodic resume path may retry.
    clock_ms[0] += 1_000
    _observe(shadow, descriptor_a, "short", "5", profile_key=codex_key,
             now_ms=clock_ms[0], reset_at_ms=clock_ms[0] + 90_000)
    _observe(shadow, descriptor_b, "short", "5", profile_key=claude_key,
             now_ms=clock_ms[0], reset_at_ms=clock_ms[0] + 90_000)
    assert manager._quota_wait_has_recovered_candidate(
        run=updated_run, identities=identities, launcher_factory=_launcher_factory,
        quota_admission_context=ctx,
    )


def test_periodic_tick_retries_quota_wait_only_after_fresh_recovery_and_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real daemon tick -> resume -> dispatch path for a durable quota wait."""
    from paulsha_cortex.coordinator import manager_daemon

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    candidates = manager._workflow_identity_candidates(run, step, identities)
    codex = next(row for row in candidates if row.executor == "codex")
    claude = next(row for row in candidates if row.executor == "claude")
    codex_key = _resolved_profile_key(run, step, codex, "codex", "gpt-primary")
    claude_key = _resolved_profile_key(run, step, claude, "claude", "claude-primary")
    codex_pool = _pool_descriptor(account="acct-codex", pool="pool-codex")
    claude_pool = _pool_descriptor(account="acct-claude", pool="pool-claude")
    clock_ms = [int(time.time() * 1000)]
    monkeypatch.setattr(time, "time", lambda: clock_ms[0] / 1000)
    shadow = QuotaShadowService.in_memory()
    reset_at_ms = clock_ms[0] + 1_000
    _observe(shadow, codex_pool, "short", "0", profile_key=codex_key,
             now_ms=clock_ms[0], reset_at_ms=reset_at_ms)
    _observe(shadow, claude_pool, "short", "0", profile_key=claude_key,
             now_ms=clock_ms[0], reset_at_ms=reset_at_ms)
    context = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(codex_pool, claude_pool), unit_catalog=(),
        bindings=(_binding(codex_pool, codex_key), _binding(claude_pool, claude_key)),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )
    # `poll_headless_done` 回 registry 原樣 row（fake pid 的 job 視為仍在執行）：
    # 缺這個方法時第二個 tick 的 resume 會以 AttributeError 失敗、把 run 標成
    # `resume-workflow-failed`，下面「連續 tick 冪等」的斷言就會因為錯誤的理由
    # 通過。
    dispatcher = type(
        "D", (), {"_registry": registry, "_git_runner": None,
                  "_worktree_creator": _FakeWorktreeCreator(worktree),
                  "poll_headless_done": lambda self, job_id, **_kwargs: registry.get_job(job_id)}
    )()
    monkeypatch.setattr(manager, "_runtime_preflight_gate", lambda *args, **kwargs: None)
    initial = manager.dispatch_workflow_card(
        dispatcher, run=run, identities=identities, launcher_factory=_launcher_factory,
        coordinator_root=tmp_path / "coordinator", quota_admission_context=context,
    )
    assert initial["reason"] == "quota-admission-insufficient"
    waiting = registry.get_workflow_run(run.run_id)
    assert waiting.needs_human_reason["reason"] == "quota-admission-insufficient"
    assert waiting.quota_admission["builder"]["outcome"] == "wait"
    assert manager.quota_wait_retry_is_eligible(run=waiting, quota_admission_context=context)
    assert registry.list_jobs() == []

    monkeypatch.setattr(manager_daemon, "_quota_admission_context_for", lambda: context)
    monkeypatch.setattr(
        manager_daemon, "_resolve_launcher_compat",
        lambda executor, *a, identity=None, model=None, **kw: _Launcher(
            identity.executor if identity is not None else executor,
            identity.model_id if identity is not None else (model or "gpt-primary"),
        ),
    )
    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher, specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"), workflow_identity_registry=identities,
        scan_specs_fn=lambda _dir: [], auto_claim_fn=lambda: [],
        run_tick_fn=lambda *args, **kwargs: {"dispatched": []},
    )

    runner()  # still empty: an eligible receipt alone does not admit a candidate
    assert registry.list_jobs() == []
    clock_ms[0] = reset_at_ms + 61_000
    runner()  # reset has passed, but the old observation is stale; do not dispatch
    assert registry.list_jobs() == []

    _observe(shadow, codex_pool, "short", "5", profile_key=codex_key,
             now_ms=clock_ms[0], reset_at_ms=clock_ms[0] + 90_000)
    _observe(shadow, claude_pool, "short", "5", profile_key=claude_key,
             now_ms=clock_ms[0], reset_at_ms=clock_ms[0] + 90_000)
    runner()
    launched = registry.list_jobs()
    assert len(launched) == 1
    assert launched[0]["workflow_run_id"] == run.run_id
    assert launched[0]["workflow_card"] == step.card
    runner()  # the same attempt is idempotent across consecutive daemon ticks
    assert len(registry.list_jobs()) == 1
    resumed = registry.get_workflow_run(run.run_id)
    assert "needs_human" not in resumed.facets
    assert resumed.needs_human_reason is None


def test_quota_context_cannot_override_missing_exact_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep the unrelated runtime capability/provider snapshot out of this case;
    # the real dispatch path and execution-profile qualification gate still run.
    monkeypatch.setattr(manager, "_runtime_preflight_gate", lambda *args, **kwargs: None)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path, sized=True)
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    identity = manager._workflow_identity_candidates(run, step, identities)[0]
    profile_key = _resolved_profile_key(run, step, identity, "codex", "gpt-primary")
    descriptor = _pool_descriptor(account="acct-qualification", pool="pool-qualification")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=profile_key, now_ms=now_ms)
    context = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, profile_key),),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    result = _dispatch(
        registry, run, replace(identities, qualification_policy="enforce"),
        worktree, tmp_path / "coordinator", quota_admission_context=context,
    )

    assert result["reason"] == "execution-profile-blocked"
    assert "exact-profile qualification is unknown" in result["detail"]
    assert registry.list_jobs() == []
    assert context.store.all_rows() == []
    assert context.authority.list_by_state("reserved", now_ms=now_ms) == ()
    assert context.authority.list_by_state("bound", now_ms=now_ms) == ()
    assert registry.get_workflow_run(run.run_id).needs_human_reason["reason"] == "execution-profile-blocked"


def test_two_manager_dispatches_racing_for_one_unit_launch_exactly_one_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    step = manager._current_workflow_step(run)
    identity = manager._workflow_identity_candidates(run, step, identities)[0]
    profile_key = _resolved_profile_key(run, step, identity, "codex", "gpt-primary")
    descriptor = _pool_descriptor(account="acct-race", pool="pool-race")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=profile_key, now_ms=now_ms)
    authority_path = tmp_path / "reservations.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    bindings = (_binding(descriptor, profile_key),)
    contexts = [
        quota_admission.DispatchContext(
            authority=QuotaReservationAuthority(authority_path),
            store=quota_admission.AdmissionDecisionStore(decisions_path),
            shadow=shadow, descriptors=(descriptor,), unit_catalog=(), bindings=bindings,
            environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
        )
        for _ in range(2)
    ]
    barrier = Barrier(2)
    launched: list[str] = []

    def dispatch(context):
        local_registry = JobRegistry(state_path=state_path)
        local_run = local_registry.get_workflow_run(run.run_id)
        dispatcher = type(
            "D", (), {"_registry": local_registry, "_git_runner": None,
                      "_worktree_creator": _FakeWorktreeCreator(worktree)}
        )()
        barrier.wait(timeout=5)
        def launcher_factory(candidate):
            launcher = _launcher_factory(candidate)
            original_launch = launcher.launch

            def tracked_launch(**kwargs):
                launched.append(candidate.executor)
                return original_launch(**kwargs)

            launcher.launch = tracked_launch
            return launcher

        try:
            return manager.dispatch_workflow_card(
                dispatcher, run=local_run, identities=identities,
                launcher_factory=launcher_factory, coordinator_root=tmp_path / "coordinator",
                quota_admission_context=context,
            )
        except RegistryRevisionConflict as exc:
            # Two Managers may race to publish their run projection; losing
            # registry CAS must still leave the quota winner as the only launcher.
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(dispatch, contexts))

    final_registry = JobRegistry(state_path=state_path)
    jobs = final_registry.list_jobs()
    # 安全不變量：最多一個 launch，且每個 launch 都有 job 紀錄。
    assert len(jobs) <= 1
    assert len(launched) == len(jobs)
    assert sum(isinstance(result, RegistryRevisionConflict) for result in results) <= 1
    if jobs:
        assert sum(isinstance(result, dict) and "job_id" in result for result in results) == 1
        assert launched == ["codex"]
        bound = contexts[0].authority.list_by_state("bound", now_ms=now_ms)
        assert len(bound) == 1
        assert bound[0].job_id == jobs[0]["job_id"]
    else:
        # 真實競態下 grant 方可能在 launch 前輸掉 registry CAS，而另一方當下因
        # 額度被佔拿到 quota wait：當輪零派工（由下一輪自動續派接手），但不得
        # 殘留 reservation，也不得有任何 launch（CI 偶發，#1187）。
        assert any(isinstance(result, RegistryRevisionConflict) for result in results)
        assert contexts[0].authority.list_by_state("reserved", now_ms=now_ms + 1) == ()
        assert contexts[0].authority.list_by_state("bound", now_ms=now_ms + 1) == ()


def test_manager_restart_reuses_live_bound_job_without_new_reservation_or_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    step = manager._current_workflow_step(run)
    identity = manager._workflow_identity_candidates(run, step, identities)[0]
    profile_key = _resolved_profile_key(run, step, identity, "codex", "gpt-primary")
    descriptor = _pool_descriptor(account="acct-restart", pool="pool-restart")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=profile_key, now_ms=now_ms)
    authority_path = tmp_path / "reservations.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    context = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(authority_path),
        store=quota_admission.AdmissionDecisionStore(decisions_path),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, profile_key),),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )
    first_job = _dispatch(
        registry, run, identities, worktree, tmp_path / "coordinator",
        quota_admission_context=context,
    )
    first_decisions = context.store.all_rows()
    reservation_states = context.authority.list_by_state("bound", now_ms=now_ms)

    restarted_registry = JobRegistry(state_path=state_path)
    restarted_run = restarted_registry.get_workflow_run(run.run_id)
    restarted_job = _dispatch(
        restarted_registry, restarted_run, identities, worktree, tmp_path / "coordinator",
        quota_admission_context=context,
    )

    assert restarted_job["job_id"] == first_job["job_id"]
    assert len(restarted_registry.list_jobs()) == 1
    assert context.store.all_rows() == first_decisions
    assert context.authority.list_by_state("bound", now_ms=now_ms) == reservation_states


def test_spawn_time_429_after_bind_settles_failed_and_frees_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=codex_key, now_ms=now_ms)
    ctx = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, codex_key),), environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    # 對抗審查第二輪 BLOCKER（manager.py:14379）：spawn 時 429／infra 失敗的
    # 即時 settle 路徑必須和 reconcile 的 on_settled 共用同一個
    # record_terminal_usage helper——用 spy 包一層原始實作，既驗證『真的被
    # 呼叫』又不改變原有副作用。
    terminal_usage_calls: list[dict[str, object]] = []
    original_record_terminal_usage = QuotaShadowService.record_terminal_usage

    def _spy_record_terminal_usage(self, job, **kwargs):
        terminal_usage_calls.append({"job": dict(job), **kwargs})
        return original_record_terminal_usage(self, job, **kwargs)

    monkeypatch.setattr(QuotaShadowService, "record_terminal_usage", _spy_record_terminal_usage)

    qualification_before = registry.get_workflow_run(run.run_id).model_qualification
    _FAILING_EXECUTORS.add("codex")
    try:
        with pytest.raises(RuntimeError, match="fake-executor-429"):
            _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)
    finally:
        _FAILING_EXECUTORS.discard("codex")

    decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=f"{run.run_id}:{step.card}:n0", profile_key=codex_key,
        mode="enforced",
    )
    decision = ctx.store.get(decision_id)
    assert decision is not None
    assert decision.reservation_id is not None
    status = ctx.authority.status(decision.reservation_id, now_ms=now_ms + 1)
    assert status.state == "settled"
    committed = ctx.authority.committed(now_ms=now_ms + 1)
    key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    assert committed.get(key, "0") == "0"
    # job 本身仍如實記為 failed——quota admission 不覆寫既有失敗分類（#826）。
    job = registry.list_jobs()[0]
    assert job["status"] == "failed"
    assert job["provider_outcome"]["outcome"] == "rate_limited"
    assert job["provider_outcome"]["authority"] == "structured"
    assert registry.get_workflow_run(run.run_id).model_qualification == qualification_before
    persisted_backoff = executor_backoff.active_backoff(
        tmp_path / "coordinator", "codex", "gpt-primary", now=now_ms / 1000 + 1,
    )
    assert persisted_backoff.backoff is not None
    assert persisted_backoff.backoff.executor == "codex"

    retry = _dispatch(
        registry, run, identities, worktree, tmp_path / "coordinator",
        quota_admission_context=ctx, force_new_card=True,
    )
    assert retry["executor"] == "claude"
    claude_identity = next(
        candidate for candidate in manager._workflow_identity_candidates(run, step, identities)
        if candidate.executor == "claude"
    )
    claude_key = _resolved_profile_key(run, step, claude_identity, "claude", "claude-primary")
    retry_decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card,
        attempt_id=f"{run.run_id}:{step.card}:n1", profile_key=claude_key,
        mode="enforced",
    )
    assert retry_decision_id != decision_id
    retry_decision = ctx.store.get(retry_decision_id)
    assert retry_decision is not None
    assert retry_decision.selected["executor"] == "claude"

    # settle 之後這個 attempt 的終局 usage 已經記過——不必等 restart 後的
    # periodic reconcile 才補記。
    assert len(terminal_usage_calls) == 1
    assert terminal_usage_calls[0]["profile_key"] == codex_key
    assert terminal_usage_calls[0]["job"]["id"] == job["job_id"]


def test_ac3_two_instances_race_for_last_unit_only_one_gets_bound_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
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


def test_race_loss_on_top_ranked_candidate_falls_back_to_independent_pool_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
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
        mode="enforced",
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


def test_duplicate_reservation_from_concurrent_manager_is_never_released_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """對抗審查第二輪 MAJOR（manager.py:13848）：`reserve_for_candidate()` 的
    冪等回放（`status == "duplicate"`）代表這個 decision_id 的 reservation
    在這次呼叫**之前**就已經存在——可能是另一個 Manager instance 剛剛才
    贏得的 grant，不是這次呼叫自己剛建立的。舊實作把 `duplicate` 和
    `granted` 同等對待，若這個 instance 隨後在 `create_job()`／provisioning
    失敗，會用這個借來的 owner_token 呼叫 `release()`，誤釋放另一個仍在
    使用中的 instance 的 grant。

    這裡直接模擬「另一個 Manager instance」（比照既有 AC3／race-fallback 兩個
    測試『直接呼叫 authority 模擬第二個 instance』的既有寫法）：在 dispatch
    呼叫前，用同一份 reservation authority 檔案，替 dispatch 即將算出的那個
    decision_id 先 reserve 一次（`granted`，取得它自己的 owner_token）——這
    就是它「已經 reserve、還沒 bind」的窗口。dispatch（模擬第二個 instance）
    唯一候選撞上這個既有 reservation 只會拿到 `duplicate`；把
    `registry.create_job` 換成一定失敗的版本，驗證即使走到『原本會觸發
    release』的失敗路徑，這個既有 reservation 仍完全沒被動過。
    """
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")

    descriptor = _pool_descriptor(account="acct-codex", pool="pool-codex")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "10", profile_key=codex_key, now_ms=now_ms)
    authority_path = tmp_path / "reservations.jsonl"
    authority = QuotaReservationAuthority(authority_path)
    ctx = quota_admission.DispatchContext(
        authority=authority,
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, codex_key),),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    # 「另一個 Manager instance」：先替 dispatch 即將算出的同一個 decision_id
    # reserve（registry 目前是空的，因此 attempt_id 一定是 n0）。
    from paulsha_cortex.coordinator.quota_reservation import PoolDemand

    attempt_id = f"{run.run_id}:{step.card}:n0"
    other_decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=attempt_id, profile_key=codex_key,
        mode="enforced",
    )
    pool_key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    other_result = authority.reserve(
        run_id=run.run_id, card_id=step.card, decision_id=other_decision_id, attempt_id=attempt_id,
        pools=(PoolDemand(pool_ref=_pool_ref(descriptor), window_id="short", amount="1"),),
        capacity_by_pool={pool_key: "10"},
        observation_version="obs-other", demand_version="demand-other", lease_ms=900_000, now_ms=now_ms,
    )
    assert other_result.status == "granted"

    # 這次 dispatch（模擬第二個 instance）在 create_job() 這一步失敗——模擬
    # provisioning 在拿到 job_id 之後、真正 spawn 之前發生的錯誤。若這個候選
    # 被誤當成『這次新建立的 grant』，就會走到 release_reservation_before_spawn，
    # 用借來的 owner_token 誤釋放另一個 instance 仍在用的 reservation。
    original_create_job = registry.create_job

    def _failing_create_job(*args, **kwargs):
        raise RuntimeError("simulated provisioning failure before spawn")

    registry.create_job = _failing_create_job
    try:
        result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)
    finally:
        registry.create_job = original_create_job

    assert result is not None
    assert "job_id" not in result
    assert result["reason"] == "quota-admission-insufficient"
    assert registry.list_jobs() == []
    excluded_reasons = {item["exclusion_reason"] for item in result.get("attempts", [])}
    assert excluded_reasons == {"quota-admission-attempt-held-elsewhere"}

    # 核心斷言：另一個 instance 的 reservation 完全沒被動過（仍是 reserved，
    # 不是被誤 release 成 released；sequence 停在 0，代表連 bind 都沒發生）。
    status = authority.status(other_result.reservation_id, now_ms=now_ms + 1)
    assert status is not None
    assert status.state == "reserved"
    assert status.sequence == 0
    assert status.job_id is None
    committed = authority.committed(now_ms=now_ms + 1)
    assert committed.get(pool_key) == "1"


def test_duplicate_reservation_already_terminated_advances_to_next_generation_and_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """對抗審查第三輪 MAJOR（manager.py:13961）：上一次 retry 在
    `create_job()` 之前失敗，已經把這個 attempt 的 reservation
    `release_reservation_before_spawn` 掉（或已被 reconcile 收斂終結）——
    但因為從未真正建出 job，`_quota_admission_attempt_id` 依 job 數推算出的
    attempt_id 完全不變，下一次 retry 用同一個 attempt_id／decision_id 重新
    reserve 只會撞到這筆『已經終結』的舊 reservation。舊實作把所有
    `duplicate` 一律當成『別人持有』，會讓這張卡永久卡在
    held-elsewhere，即使容量其實完全空著；驗證新實作改用下一個世代的
    attempt_id 重新 reserve，成功建出 job。
    """
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")

    descriptor = _pool_descriptor(account="acct-codex", pool="pool-codex")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "10", profile_key=codex_key, now_ms=now_ms)
    authority_path = tmp_path / "reservations.jsonl"
    authority = QuotaReservationAuthority(authority_path)
    ctx = quota_admission.DispatchContext(
        authority=authority,
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, codex_key),),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    # 模擬「上一次 retry」：替這個 attempt（一定是 n0，registry 目前是空的）
    # reserve 之後、在 create_job() 之前就已經失敗並 release 掉——這正是
    # `release_reservation_before_spawn` 的既有用法
    # （reason="fail-before-spawn"）。
    from paulsha_cortex.coordinator.quota_reservation import PoolDemand

    base_attempt_id = f"{run.run_id}:{step.card}:n0"
    base_decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=base_attempt_id, profile_key=codex_key,
        mode="enforced",
    )
    pool_key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    prior_result = authority.reserve(
        run_id=run.run_id, card_id=step.card, decision_id=base_decision_id, attempt_id=base_attempt_id,
        pools=(PoolDemand(pool_ref=_pool_ref(descriptor), window_id="short", amount="1"),),
        capacity_by_pool={pool_key: "10"},
        observation_version="obs-prior", demand_version="demand-prior", lease_ms=900_000, now_ms=now_ms,
    )
    assert prior_result.status == "granted"
    released = authority.release(
        reservation_id=prior_result.reservation_id, owner_token=prior_result.owner_token,
        attempt_id=base_attempt_id, reason="fail-before-spawn",
        expected_sequence=prior_result.sequence, now_ms=now_ms,
    )
    assert released.status == "ok"

    result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)
    assert result is not None
    assert "job_id" in result  # 舊 reservation 已終結，改用下一個世代成功派工
    job = registry.get_job(result["job_id"])
    assert job["executor"] == "codex"

    generation_attempt_id = f"{base_attempt_id}:g1"
    decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=generation_attempt_id, profile_key=codex_key,
        mode="enforced",
    )
    decision = ctx.store.get(decision_id)
    assert decision is not None
    assert decision.attempt_id == generation_attempt_id
    assert decision.reservation_id is not None
    status = ctx.authority.status(decision.reservation_id, now_ms=now_ms + 1)
    assert status.state == "bound"
    assert status.job_id == result["job_id"]

    # 上一個世代的 reservation 維持它原本的終局狀態，沒有被誤動。
    prior_status = ctx.authority.status(prior_result.reservation_id, now_ms=now_ms + 1)
    assert prior_status.state == "released"


def test_switching_shadow_to_enforce_mid_attempt_writes_distinct_enforced_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """對抗審查第四輪 MAJOR（manager.py:11491）：同一個 attempt（job 從未真正
    建立，ordinal 不動）先在 shadow 模式下寫過一筆 admit receipt——舊實作
    `decision_id_for` 不納入 mode，之後 operator 把
    `PSC_QUOTA_ADMISSION_ENFORCE` 開成 on 重試同一張卡時，算出**同一個**
    decision_id；enforce 那次真正 reserve／bind 出的 reservation 想寫入
    admit receipt 時會被 shadow 的舊記錄擋下（`_quota_admission_record_admit_decision`
    的『先讀舊值、有就沿用』語意會直接沿用 shadow 那筆），
    `WorkflowRun.quota_admission` 永遠卡在 shadow，真正的 bound reservation
    對『依 receipt 反查』的收斂掃描也永遠不可見。

    重現「shadow 觀察過但從未真正建出 job」的現場：把 `registry.create_job`
    換成只在第一次呼叫失敗的版本——admit receipt 的寫入在
    `_dispatch_workflow_card` 裡發生在 `create_job()` 之前，因此 shadow
    receipt 會確實落地，但 job 從未建立、ordinal 不前進；第二次呼叫（開了
    enforce）算出同一個 attempt_id，驗證 enforce 那次的 receipt 用
    **不同** decision_id 落地、與 shadow 那筆並存，且
    `WorkflowRun.quota_admission` 更新成 enforced。
    """
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")

    descriptor = _pool_descriptor(account="acct-codex", pool="pool-codex")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=codex_key, now_ms=now_ms)
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    bindings = (_binding(descriptor, codex_key),)

    shadow_ctx = quota_admission.DispatchContext(
        authority=authority, store=store, shadow=shadow, descriptors=(descriptor,),
        unit_catalog=(), bindings=bindings, environment={},  # PSC_QUOTA_ADMISSION_ENFORCE 未設 -> shadow
    )

    original_create_job = registry.create_job
    call_count = {"n": 0}

    def _fail_once_create_job(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated provisioning failure before spawn")
        return original_create_job(*args, **kwargs)

    registry.create_job = _fail_once_create_job
    try:
        with pytest.raises(RuntimeError, match="simulated provisioning failure"):
            _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=shadow_ctx)
    finally:
        registry.create_job = original_create_job

    assert registry.list_jobs() == []  # shadow 那次從未真正建出 job，ordinal 不動

    attempt_id = f"{run.run_id}:{step.card}:n0"
    shadow_decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=attempt_id, profile_key=codex_key, mode="shadow",
    )
    shadow_decision = store.get(shadow_decision_id)
    assert shadow_decision is not None
    assert shadow_decision.mode == "shadow"
    assert shadow_decision.reservation_id is None

    # operator 開 enforce 重試同一張卡（同一個 attempt_id，因為從未真正建出
    # job）。
    enforce_ctx = quota_admission.DispatchContext(
        authority=authority, store=store, shadow=shadow, descriptors=(descriptor,),
        unit_catalog=(), bindings=bindings, environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )
    result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=enforce_ctx)
    assert result is not None
    assert "job_id" in result
    job = registry.get_job(result["job_id"])
    assert job["executor"] == "codex"

    enforced_decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=attempt_id, profile_key=codex_key, mode="enforced",
    )
    assert enforced_decision_id != shadow_decision_id  # mode 隔離：兩者不撞
    enforced_decision = store.get(enforced_decision_id)
    assert enforced_decision is not None
    assert enforced_decision.mode == "enforced"
    assert enforced_decision.reservation_id is not None

    # shadow 那筆舊記錄完全沒被動過（仍是它自己、獨立的一筆）。
    shadow_decision_after = store.get(shadow_decision_id)
    assert shadow_decision_after == shadow_decision

    # WorkflowRun.quota_admission 投影更新成 enforced——不再卡在 shadow。
    updated_run = registry.get_workflow_run(run.run_id)
    assert updated_run.quota_admission["builder"]["mode"] == "enforced"
    assert updated_run.quota_admission["builder"]["decision_id"] == enforced_decision_id

    # 這筆真正的 bound reservation 對 authority-based 的收斂掃描可見。
    bound_outcomes = quota_admission.reconcile_bound_reservations(
        authority=authority, store=store,
        job_lookup=lambda job_id: registry.get_job(job_id),
        job_outcome=lambda job: "succeeded" if job.get("status") == "exited" else None,
        now_ms=now_ms + 1,
    )
    assert len(bound_outcomes) == 1
    assert bound_outcomes[0].reservation_id == enforced_decision.reservation_id


def test_rollback_to_shadow_preserves_enforce_evidence_and_frozen_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    step = manager._current_workflow_step(run)
    pinned_step = WorkflowStep(
        phase=step.phase, persona=step.persona, card=step.card,
        executor="codex", model="gpt-primary", domain="openai",
        inputs=step.inputs, outputs=step.outputs, gate_result=step.gate_result,
    )
    run = registry._manager_update_workflow_run(run.run_id, steps=(pinned_step,))
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    identity = manager._workflow_identity_candidates(run, pinned_step, identities)[0]
    profile_key = _resolved_profile_key(run, pinned_step, identity, "codex", "gpt-primary")
    descriptor = _pool_descriptor(account="acct-rollback", pool="pool-rollback")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=profile_key, now_ms=now_ms)
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    context = quota_admission.DispatchContext(
        authority=authority, store=store, shadow=shadow, descriptors=(descriptor,),
        unit_catalog=(), bindings=(_binding(descriptor, profile_key),),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )
    original_create_job = registry.create_job

    def fail_before_create(*args, **kwargs):
        raise RuntimeError("provisioning failed before job creation")

    registry.create_job = fail_before_create
    try:
        with pytest.raises(RuntimeError, match="before job creation"):
            _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=context)
    finally:
        registry.create_job = original_create_job

    enforce_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=pinned_step.card,
        attempt_id=f"{run.run_id}:{pinned_step.card}:n0", profile_key=profile_key,
        mode="enforced",
    )
    enforced = store.get(enforce_id)
    assert enforced is not None and enforced.reservation_id is not None
    assert authority.status(enforced.reservation_id, now_ms=now_ms + 1).state == "released"

    shadow_context = quota_admission.DispatchContext(
        authority=authority, store=store, shadow=shadow, descriptors=(descriptor,),
        unit_catalog=(), bindings=(_binding(descriptor, profile_key),), environment={},
    )
    retry = _dispatch(
        registry, run, identities, worktree, tmp_path / "coordinator",
        quota_admission_context=shadow_context,
    )
    assert retry["executor"] == "codex"
    shadow_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=pinned_step.card,
        attempt_id=f"{run.run_id}:{pinned_step.card}:n0", profile_key=profile_key,
        mode="shadow",
    )
    shadow_decision = store.get(shadow_id)
    assert shadow_decision is not None and shadow_decision.mode == "shadow"
    assert store.get(enforce_id) == enforced
    assert authority.status(enforced.reservation_id, now_ms=now_ms + 1).state == "released"
    assert registry.get_workflow_run(run.run_id).steps[0].executor == "codex"
    assert registry.get_workflow_run(run.run_id).steps[0].model == "gpt-primary"


def test_reserved_sweep_job_lookup_does_not_misattribute_unrelated_candidates_job(tmp_path: Path) -> None:
    """對抗審查第四輪 MAJOR（manager.py:11603）：Manager A 對候選 A 的
    attempt n0 `reserve()` 後 crash（`create_job()` 從未發生）；Manager B
    （另一個 instance，或稍後的 retry）改派候選 B（不同 pool／profile）也在
    attempt n0 建出它自己的 job——這個 job 剛好是該 run/card 第一個
    （ordinal 0）。舊實作 `_quota_admission_job_lookup_by_attempt` 純依
    run/card + ordinal 反查，會把 B 的 job 誤當成 A 的證據，用它的 liveness
    決定要不要 renew／release A 的 reservation，即使兩者完全無關。新實作依
    job 建立時記錄的 `quota_decision_id` 精確比對，B 的 job 永遠不會被誤配
    到 A。"""
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run_id = "run-x"
    card_id = "card-x"

    decision_id_a = quota_admission.decision_id_for(
        run_id=run_id, card_id=card_id, attempt_id=f"{run_id}:{card_id}:n0",
        profile_key="epk:v1:resolved:" + "a" * 64, mode="enforced",
    )
    decision_id_b = quota_admission.decision_id_for(
        run_id=run_id, card_id=card_id, attempt_id=f"{run_id}:{card_id}:n0",
        profile_key="epk:v1:resolved:" + "b" * 64, mode="enforced",
    )
    assert decision_id_a != decision_id_b

    # Manager B 派候選 B 建出它自己的 job（該 run/card 目前唯一、ordinal 0）。
    registry.create_job(
        task="wf-x", persona="builder", branch="feature/x", pane="",
        worktree=str(tmp_path / "wt"),
        workflow_run_id=run_id, workflow_card=card_id, workflow_phase="build",
        quota_decision_id=decision_id_b,
    )

    # 舊實作（ordinal 反查）會把這個 job 當成 A 的證據；新實作精確比對
    # decision_id，A 查不到任何對應的 job。
    found_for_a = manager._quota_admission_job_lookup_by_decision(registry, run_id, card_id, decision_id_a)
    assert found_for_a is None

    found_for_b = manager._quota_admission_job_lookup_by_decision(registry, run_id, card_id, decision_id_b)
    assert found_for_b is not None
    assert found_for_b["quota_decision_id"] == decision_id_b


def test_reserved_sweep_ignores_legacy_job_missing_quota_decision_id_field(tmp_path: Path) -> None:
    """job 缺少可比對欄位（本欄位新增之前建立的舊版 job，或非額度管理路徑
    建立的 job：`quota_decision_id` 為 `None`）——一律視為『查無此 job』，
    交給呼叫端依 lease／in-flight 判定，不 bind、不 release（見
    `reconcile_reserved_reservations` 對 `job is None` 的既有保守分支），不
    得因為欄位缺失就誤配到任何 decision_id。"""
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run_id = "run-y"
    card_id = "card-y"
    decision_id = quota_admission.decision_id_for(
        run_id=run_id, card_id=card_id, attempt_id=f"{run_id}:{card_id}:n0",
        profile_key="epk:v1:resolved:" + "c" * 64, mode="enforced",
    )
    registry.create_job(
        task="wf-y", persona="builder", branch="feature/y", pane="",
        worktree=str(tmp_path / "wt"),
        workflow_run_id=run_id, workflow_card=card_id, workflow_phase="build",
        # quota_decision_id 未傳——預設 None，模擬舊版 job。
    )
    found = manager._quota_admission_job_lookup_by_decision(registry, run_id, card_id, decision_id)
    assert found is None


# ---------------------------------------------------------------------------
# 對抗審查第四輪 MAJOR（quota_admission.py:1119）：dispatch 側 provisioning
# 續租、in-flight 標記與清除、bind() 撞上已終結 reservation 時 fail closed。
# ---------------------------------------------------------------------------


def test_successful_dispatch_renews_lease_and_clears_in_flight_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功派工時：(1) reservation 的 lease 在 bind 之前就已經被續租過一次
    （對抗審查第四輪 MAJOR quota_admission.py:1119 的續租呼叫，見
    `_dispatch_workflow_card` 拿到 grant 之後、開始 provisioning 之前）；
    (2) `IN_FLIGHT_DISPATCHES` 在派工結束後不再標記這筆 reservation——不是
    永久卡在 in-flight 集合裡（見 finally 保證）。"""
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")

    descriptor = _pool_descriptor(account="acct-codex", pool="pool-codex")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=codex_key, now_ms=now_ms)
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    # 刻意用一個很短（但合法，>= 60s 下限）的 lease_ms——若沒有 provisioning
    # 續租，_freeze_wall_clock 讓牆鐘完全不動，所以這裡不是要重現真的過期，
    # 而是要驗證 renew() 事件確實發生過（sequence 前進、lease 被展延）。
    ctx = quota_admission.DispatchContext(
        authority=authority, store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, codex_key),),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"}, lease_ms=60_000,
    )

    result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)
    assert result is not None
    assert "job_id" in result

    status_after = authority.status(
        [item for item in authority.list_by_state("bound", now_ms=now_ms + 1)][0].reservation_id,
        now_ms=now_ms + 1,
    )
    # reserve() 給的原始 lease 是 now_ms + 60_000；renew()（sequence 0→1）
    # 加上 bind()（sequence 1→2）之後 sequence 至少前進了兩次。
    assert status_after.sequence >= 2

    # in-flight 集合已經清空——不是永久卡住。
    assert quota_admission.IN_FLIGHT_DISPATCHES.is_in_flight(status_after.reservation_id) is False


def test_bind_conflict_reservation_already_terminal_fails_closed_with_wait_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """對抗審查第四輪 MAJOR（quota_admission.py:1119）：bind() 時發現這筆
    reservation 已經被結束掉（`conflict`／`reservation-already-terminal`，
    即使已經有 renew／grace／in-flight 排除，極端情況仍可能發生）——job 記錄
    已經建立，不能假稱成功；必須標記失敗（不記 provider_outcome，避免誤觸
    #825/#826 executor backoff）、留一筆 wait decision receipt，並讓例外
    以既有『job 已建立、spawn 前失敗』的 fail-closed 路徑傳播（不 spawn）。"""
    from paulsha_cortex.coordinator.quota_reservation import TransitionResult

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")

    descriptor = _pool_descriptor(account="acct-codex", pool="pool-codex")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=codex_key, now_ms=now_ms)
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    store = quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    ctx = quota_admission.DispatchContext(
        authority=authority, store=store, shadow=shadow, descriptors=(descriptor,),
        unit_catalog=(), bindings=(_binding(descriptor, codex_key),),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    monkeypatch.setattr(
        QuotaReservationAuthority, "bind",
        lambda self, **kwargs: TransitionResult(
            status="conflict", state="released", reason="reservation-already-terminal",
        ),
    )

    with pytest.raises(ValueError, match="reservation-already-terminal"):
        _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)

    # job 記錄已建立（create_job() 早於 bind()），但標記為失敗——不假稱成功。
    jobs = registry.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["status"] == "failed"
    assert job["provider_outcome"] is None  # 不誤觸 #825/#826 executor backoff
    assert job["runtime_diagnostic"]["reason"] == "quota-admission-reservation-lost"

    # wait decision receipt 已留下，供 #840 觀測。
    updated_run = registry.get_workflow_run(run.run_id)
    assert updated_run.quota_admission is not None
    projection = updated_run.quota_admission["builder"]
    assert projection["mode"] == "enforced"
    assert projection["outcome"] == "wait"
    decision = store.get(projection["decision_id"])
    assert decision is not None
    assert decision.reason == "quota-admission-reservation-lost"

    # in-flight 集合已經清除——不因為這條失敗路徑而永久卡住。
    # bind() 被 monkeypatch 直接回傳假結果、從未真正寫入任何轉移事件，這筆
    # reservation 在 authority 裡仍是 reserved；用它反查 reservation_id。
    still_reserved = authority.list_by_state("reserved", now_ms=now_ms + 1)
    assert len(still_reserved) == 1
    assert quota_admission.IN_FLIGHT_DISPATCHES.is_in_flight(still_reserved[0].reservation_id) is False


# ---------------------------------------------------------------------------
# 對抗審查第五輪 MAJOR：mark_started／try-finally 必須緊鄰，provisioning
# 失敗（含 creator.create()）也要清 in-flight 並 release(fail-before-spawn)；
# periodic sweep 對本 process in-flight 的 reservation 必須在 job 反查之前
# 就跳過；bind() 撞上並行續租造成的 sequence-mismatch 要重試一次。
# ---------------------------------------------------------------------------


class _FailingOnceWorktreeCreator:
    """第一次呼叫（模擬 creator.create() 的 provisioning 失敗）拋例外，第二次
    起成功——用來驗證失敗後的 retry 能乾淨重新 reserve，不撞 held-elsewhere。"""

    def __init__(self, path: Path):
        self._path = path
        self.calls = 0

    def create(
        self,
        branch: str,
        base_sha: str | None = None,
        *,
        job_id: str | None = None,
        owner_identity: dict[str, str] | None = None,
        attempt_id: str | None = None,
    ) -> Path:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("fake-worktree-provisioning-failure")
        return self._path


def _dispatch_with_creator(registry, run, identities, creator, coordinator_root, *, quota_admission_context=None):
    dispatcher = type(
        "D", (), {"_registry": registry, "_git_runner": None, "_worktree_creator": creator}
    )()
    return manager.dispatch_workflow_card(
        dispatcher, run=run, identities=identities, launcher_factory=_launcher_factory,
        coordinator_root=coordinator_root, quota_admission_context=quota_admission_context,
    )


def test_provisioning_failure_before_create_job_clears_in_flight_and_releases_for_clean_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#839 對抗審查修復第五輪 MAJOR（manager.py:14098）：mark_started 之後、
    真正進入涵蓋 provisioning 全程的 try 之前不能夾雜任何可能拋例外的程式碼
    ——`creator.create()`（worktree provisioning）在 `create_job()` 之前失敗
    時，這筆 reservation 必須被 `release(reason="fail-before-spawn")`，
    `IN_FLIGHT_DISPATCHES` 也必須清掉；下一次 retry 要能正常重新 reserve
    （撞到已終結的舊 reservation 時自動換下一個世代），不會卡在
    held-elsewhere。"""
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")

    descriptor = _pool_descriptor(account="acct-codex", pool="pool-codex")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=codex_key, now_ms=now_ms)
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    ctx = quota_admission.DispatchContext(
        authority=authority, store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, codex_key),),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    creator = _FailingOnceWorktreeCreator(worktree)

    with pytest.raises(RuntimeError, match="fake-worktree-provisioning-failure"):
        _dispatch_with_creator(
            registry, run, identities, creator, tmp_path / "coordinator", quota_admission_context=ctx,
        )

    # 第一次失敗：從未建立任何 job；reservation 已經被 release
    # (fail-before-spawn)；in-flight 集合也已經清空——不是永久卡住。
    assert registry.list_jobs() == []
    released = authority.list_by_state("released", now_ms=now_ms + 1)
    assert len(released) == 1
    assert released[0].reservation_id != ""
    assert quota_admission.IN_FLIGHT_DISPATCHES.is_in_flight(released[0].reservation_id) is False
    assert authority.list_by_state("reserved", now_ms=now_ms + 1) == ()

    # 下一次 retry（同一個 run/card，job 數未變 ⇒ 同一個 attempt_id ordinal）
    # ——撞到上面這筆已終結的 reservation 時自動換算下一個世代，正常拿到新
    # grant 並成功派工，不撞 held-elsewhere。
    result = _dispatch_with_creator(
        registry, run, identities, creator, tmp_path / "coordinator", quota_admission_context=ctx,
    )
    assert result is not None
    assert "job_id" in result
    jobs = registry.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["status"] != "failed"
    bound = authority.list_by_state("bound", now_ms=now_ms + 1)
    assert len(bound) == 1
    assert bound[0].reservation_id != released[0].reservation_id
    assert quota_admission.IN_FLIGHT_DISPATCHES.is_in_flight(bound[0].reservation_id) is False


def test_reserved_sweep_skips_this_process_in_flight_reservation_before_job_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#839 對抗審查修復第五輪 MAJOR（quota_admission.py 週期性 sweep）：在
    `create_job()` 與 `bind()` 之間插入一次本 process 自己的
    `reconcile_reserved_reservations` 掃描——這個 reservation 已經被
    `mark_started` 標記為 in-flight，掃描必須在反查 job 之前就跳過（不續租、
    不寫入任何事件），不能因為這時候 job 已經存在但未終局就對它 renew，
    否則會讓 dispatch 手上快取的 `expected_sequence` 過期。"""
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")

    descriptor = _pool_descriptor(account="acct-codex", pool="pool-codex")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=codex_key, now_ms=now_ms)
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    ctx = quota_admission.DispatchContext(
        authority=authority, store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, codex_key),),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    captured_outcomes: list = []
    sequence_before_sweep_holder: list[int] = []
    original_create_job = JobRegistry.create_job

    def _create_job_then_sweep(self, **kwargs):
        job = original_create_job(self, **kwargs)

        # job 已建立、bind() 尚未呼叫——此刻這筆 reservation 恰好處於
        # create_job() 與 bind() 之間，且已被 mark_started 標記為 in-flight。
        reserved_now = authority.list_by_state("reserved", now_ms=int(time.time() * 1000))
        assert len(reserved_now) == 1
        sequence_before_sweep_holder.append(reserved_now[0].sequence)

        def _job_lookup_by_decision(run_id, card_id, decision_id):
            return next(
                (
                    j for j in self.list_jobs()
                    if j.get("workflow_run_id") == run_id
                    and j.get("workflow_card") == card_id
                    and j.get("quota_decision_id") == decision_id
                ),
                None,
            )

        outcomes = quota_admission.reconcile_reserved_reservations(
            authority=authority,
            job_lookup_by_decision=_job_lookup_by_decision,
            job_outcome=lambda j: None,
            now_ms=int(time.time() * 1000),
            in_flight=quota_admission.IN_FLIGHT_DISPATCHES,
        )
        captured_outcomes.extend(outcomes)
        return job

    monkeypatch.setattr(JobRegistry, "create_job", _create_job_then_sweep)

    result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)
    assert result is not None
    assert "job_id" in result

    # 本 process 自己的 sweep 對這筆 in-flight reservation 一律 skipped——
    # 沒有續租、沒有寫入任何事件。
    assert len(captured_outcomes) == 1
    assert captured_outcomes[0].action == "skipped"
    assert "in-flight" in captured_outcomes[0].detail

    bound = authority.list_by_state("bound", now_ms=now_ms + 1)
    assert len(bound) == 1
    # sweep 完全沒有介入：sequence 只由 dispatch 自己的 renew()→bind() 前進，
    # 沒有夾雜任何額外的 sweep 寫入事件。
    assert bound[0].sequence == sequence_before_sweep_holder[0] + 1


def test_concurrent_instance_sweep_renew_between_create_job_and_bind_retries_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#839 對抗審查修復第五輪 MAJOR（manager.py:14511 bind 重試）：模擬
    *另一個* Manager instance 的 periodic sweep——它沒有本 process 的
    `IN_FLIGHT_DISPATCHES` 記憶（不同 process 的記憶體集合），在
    `create_job()` 之後、原 dispatch `bind()` 之前，對這筆同一個 owner 的
    reservation 做了一次合法的 `reconcile(confirmed-alive)` 續租（`reserved`
    →`reserved`，sequence 前進）。原 dispatch 手上快取的 `expected_sequence`
    因此過期；`bind()` 第一次呼叫必然撞上 `conflict`／`sequence-mismatch`，
    但 owner_token／attempt_id 都對得上、且 reservation 仍是 `reserved`——
    重新讀取目前 sequence 後重試一次應該成功，派工乾淨完成，不會永久卡住。"""
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path)
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities) if c.executor == "codex")
    codex_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")

    descriptor = _pool_descriptor(account="acct-codex", pool="pool-codex")
    now_ms = int(time.time() * 1000)
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=codex_key, now_ms=now_ms)
    authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    ctx = quota_admission.DispatchContext(
        authority=authority, store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, codex_key),),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    original_create_job = JobRegistry.create_job

    def _create_job_then_foreign_sweep_renew(self, **kwargs):
        job = original_create_job(self, **kwargs)
        # 模擬另一個 instance 的 sweep：直接對 authority 操作，完全不知道
        # 本 process 的 IN_FLIGHT_DISPATCHES（它是不同 process 的記憶體）。
        reserved = authority.list_by_state("reserved", now_ms=int(time.time() * 1000))
        assert len(reserved) == 1
        status = reserved[0]
        renew_result = authority.reconcile(
            reservation_id=status.reservation_id,
            evidence={"kind": "job-registry-lookup-alive-unbound"},
            resolution="confirmed-alive",
            expected_sequence=status.sequence,
            now_ms=int(time.time() * 1000),
            renew_lease_ms=300_000,
        )
        assert renew_result.status == "ok"
        return job

    monkeypatch.setattr(JobRegistry, "create_job", _create_job_then_foreign_sweep_renew)

    result = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)
    assert result is not None
    assert "job_id" in result
    job = registry.get_job(result["job_id"])
    assert job["status"] != "failed"

    bound = authority.list_by_state("bound", now_ms=now_ms + 1)
    assert len(bound) == 1
    # sequence 前進次序：reserve()=0 → dispatch 自己的 renew()=1 →
    # 「另一個 instance」的 foreign sweep renew=2 → bind() 重試成功=3。
    assert bound[0].sequence == 3
    assert quota_admission.IN_FLIGHT_DISPATCHES.is_in_flight(bound[0].reservation_id) is False


def test_failure_before_sandbox_provisioning_surfaces_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1183 CI（兩 Manager 競態）：provisioning try 區塊在 sandbox 變數賦值前就失敗時，
    except 區塊曾因引用未賦值的 ``planner_sandbox`` 擲 UnboundLocalError、蓋掉原始
    例外。原始錯誤必須原樣傳出，reservation 也要在 spawn 前釋放。"""

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
    _freeze_wall_clock(monkeypatch, now_ms=now_ms)
    shadow = QuotaShadowService.in_memory()
    _observe(shadow, descriptor, "short", "5", profile_key=codex_key, now_ms=now_ms)
    ctx = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_binding(descriptor, codex_key),), environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )

    def fail_before_provisioning(**_kwargs):
        raise RuntimeError("stage context failed before provisioning")

    monkeypatch.setattr(manager, "_workflow_stage_execution_context", fail_before_provisioning)

    with pytest.raises(RuntimeError, match="stage context failed before provisioning"):
        _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", quota_admission_context=ctx)

    assert registry.list_jobs() == []
    assert ctx.authority.list_by_state("reserved", now_ms=now_ms + 1) == ()
