"""#839 逐票驗收補齊：前兩輪（PR #1176）後仍為「部分」的條目。

全部走正式派工路徑（`manager.dispatch_workflow_card`／`resume_workflow_run`／
`manager_daemon` 的 request executor 與 periodic tick），不 monkeypatch
被測的 dispatch／launcher 決策：

- AC2：非額度類 negative（缺 qualification、unsupported effort、違 pin、違
  launcher 權限契約、唯一 reviewer 同 independence domain）在 quota admission
  **shadow 與 enforce** 兩種模式下都零 spawn、零 reservation、零 quota receipt，
  並回精確拒因——額度層不得替硬條件放行，也不得把非額度拒絕偽裝成額度等待。
- AC3：兩個 Manager process（各自的 registry／dispatcher、共享 reservation
  authority 與觀測 ledger）以 barrier 同時在 reserve 前對齊，一單位額度下只有
  取得 grant 的一方呼叫 launcher。
- AC4：spawn 時結構化 429 → `rate_limited`（structured）＋耐久 executor
  backoff；同一 attempt 的重送不重選、不再 reserve；安全新 attempt 才換候選；
  backoff 到期後原候選的排序與選中不受影響（不降品質分）。
- AC6：daemon start 與 forced retry（retry-card）遇到真的 quota wait 時的 #830
  分類；forced retry 不假稱成功。
"""

from __future__ import annotations

import hashlib
import json
import re
import multiprocessing
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import executor_backoff, manager, manager_daemon, quota_admission
from paulsha_cortex.coordinator import quota_observation as schema
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.quota_ledger import QuotaEventLedger
from paulsha_cortex.coordinator.quota_reservation import QuotaReservationAuthority
from paulsha_cortex.coordinator.quota_shadow import QuotaShadowService
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.runtime_preflight import ExecutorEnvironment
from paulsha_cortex.coordinator.workflow import WorkflowStep

from test_quota_admission_dispatch_wiring_839 import (
    _FakeWorktreeCreator,
    _init_worktree,
    _pool_descriptor,
    _pool_ref,
)

REPO = "hamanpaul/paulsha-cortex"
_OBSERVATION_PROFILE_KEY = "epk:v1:resolved:" + "f" * 64


# ---------------------------------------------------------------------------
# 共用 fixture
# ---------------------------------------------------------------------------


class _Structured429(RuntimeError):
    status_code = 429


class _RecordingLauncher:
    """真實派工路徑用的 launcher 替身：只記錄 `launch()` 是否被呼叫。

    ``effort`` 對應 `execution_adapters.make_launcher_profile` 讀的 `_effort`；
    ``commit_capable=False`` 模擬缺少 builder 寫入契約（`as_commit_required`）的
    launcher——#839 票面第 3 點「先硬濾…權限」的 launch 權限契約。
    """

    def __init__(
        self, executor: str, model_id: str, calls: list[str], *,
        effort: str | None = None, commit_capable: bool = True, fail_429: bool = False,
    ) -> None:
        self._executor = executor
        self._model_id = model_id
        self._calls = calls
        self._effort = effort
        self._fail_429 = fail_429
        if commit_capable:
            self.as_commit_required = lambda: self

    def as_read_only(self):
        return self

    def as_review_only(self, *, terminal_kind: str):
        self._review_only = True
        return self

    def executor_environment(self) -> ExecutorEnvironment:
        return ExecutorEnvironment(
            name=f"{self._executor}-workflow", interpreter=(sys.executable,),
            path=os.environ.get("PATH", ""), home=os.path.expanduser("~"),
            provider_identity=self._executor,
        )

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        self._calls.append(f"{self._executor}/{self._model_id}")
        if self._fail_429:
            raise _Structured429("fake-executor-429: rate limited at spawn time")
        return LaunchHandle(
            executor=self._executor, model_id=self._model_id, session_name=slice_id,
            pid=4242, log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


def _identity_binding(descriptor: schema.PoolDescriptor, *, executor: str, model_id: str):
    """#1116 identity subject binding：涵蓋該 executor＋model 的任何 resolved profile。"""
    return schema.parse_binding(
        {
            "schema_version": 1, "binding_id": f"binding-{executor}-{model_id}", "revision": "1",
            "subject": {"kind": "identity", "executor": executor, "model_id": model_id},
            "constraints": [
                {"state": "known", "value": {"pool_ref": _pool_ref(descriptor), "window_id": window["window_id"]}}
                for window in descriptor.to_dict()["windows"]
            ],
            "coverage": {"state": "complete", "gaps": []},
        },
        descriptors=(descriptor,),
    )


def _observation_payload(descriptor, window_id: str, value: str, *, now_ms: int, ttl_ms: int = 60_000) -> dict:
    return {
        "schema_version": 1,
        "observation_id": f"fixture-{descriptor.pool_id}-{window_id}-{now_ms}-{value}",
        "scope": {"state": "known", "value": {"pool_ref": _pool_ref(descriptor), "window_id": window_id}},
        "profile_ref": {"state": "known", "value": {"schema_version": 1, "key": _OBSERVATION_PROFILE_KEY}},
        "unit_ref": {"state": "known", "value": {"unit_id": "token", "version": "1"}},
        "window_instance": {"kind": "unknown", "reason": "missing-window-instance"},
        "measurement": {
            "kind": "remaining_snapshot", "metric_id": "remaining",
            "quantity": {"state": "observed", "amount": {"kind": "exact", "value": value}},
        },
        "observed_at_ms": {"state": "known", "value": now_ms},
        "received_at_ms": now_ms,
        "ttl_ms": {"state": "known", "value": ttl_ms},
        "reset_at_ms": {"state": "unknown", "reason": "missing-reset"},
        "source": {
            "source_id": "fixture-provider", "source_schema": "fixture-quota-v1",
            "adapter_version": "fixture-adapter-v1", "authority_ref": "fixture:provider-contract/v1",
            "method": "provider_status", "provenance_refs": ["fixture:source-document/v1"],
            "event_identity": {"state": "unknown", "reason": "provider-has-no-event-id"},
        },
        "coverage": {"state": "complete", "gaps": []},
    }


def _observe(
    shadow: QuotaShadowService, descriptor, value: str, *, now_ms: int, ttl_ms: int = 60_000,
    window_id: str = "short",
) -> None:
    result = shadow.record_observation(
        _observation_payload(descriptor, window_id, value, now_ms=now_ms, ttl_ms=ttl_ms),
        descriptors=(descriptor,), unit_catalog=(),
    )
    assert result.accepted == 1


def _freeze(monkeypatch: pytest.MonkeyPatch, now_ms: int) -> list[int]:
    clock = [now_ms]
    monkeypatch.setattr(time, "time", lambda: clock[0] / 1000)
    return clock


def _context(
    tmp_path: Path, *, shadow: QuotaShadowService, pools: dict[tuple[str, str], schema.PoolDescriptor],
    enforce: bool,
) -> quota_admission.DispatchContext:
    return quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow, descriptors=tuple(dict.fromkeys(pools.values())), unit_catalog=(),
        bindings=tuple(
            _identity_binding(descriptor, executor=executor, model_id=model_id)
            for (executor, model_id), descriptor in pools.items()
        ),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"} if enforce else {},
    )


def _dispatcher(registry: JobRegistry, worktree: Path):
    """dispatcher 替身：`poll_headless_done` 回 registry 原樣 row（fake pid 的 job
    視為仍在執行），供 periodic resume 的 harvest 輪詢；派工決策不經過它。"""
    return type(
        "D", (), {
            "_registry": registry, "_git_runner": None, "_worktree_creator": _FakeWorktreeCreator(worktree),
            "poll_headless_done": lambda self, job_id, **_kwargs: registry.get_job(job_id),
        },
    )()


def _builder_run(registry: JobRegistry, tmp_path: Path, *, sized: bool = False, override=None, work_id: str = "acceptance-839"):
    return registry._manager_create_workflow_run(
        work_id=work_id, repo=REPO,
        claim_key="claim:v1:" + hashlib.sha256(work_id.encode()).hexdigest(), source_revision="4" * 64,
        workspace_root=str(tmp_path), combo="feature-oneshot", current_phase="build",
        steps=(WorkflowStep(
            phase="build", persona="builder", card="subagent-build", executor=None, model=None,
            domain=None, inputs=(), outputs=(), gate_result="pending",
        ),),
        issue_refs=(), openspec_refs=(), facets=(), gate_status="running",
        sizing_score=5 if sized else None, sizing_band="yellow" if sized else None,
        model_chain_override=override,
    )


def _reviewer_run(registry: JobRegistry, tmp_path: Path, *, override=None):
    candidate = "b" * 40
    return registry._manager_create_workflow_run(
        work_id="acceptance-839-review", repo=REPO,
        claim_key="claim:v1:" + "5" * 64, source_revision="6" * 64,
        workspace_root=str(tmp_path), combo="feature-oneshot", current_phase="verify",
        steps=(
            WorkflowStep(
                phase="build", persona="builder", card="subagent-build", executor="codex",
                model="gpt-primary", domain="openai", inputs=(), outputs=(), gate_result="passed",
            ),
            WorkflowStep(
                phase="verify", persona="reviewer", card="verification", executor=None, model=None,
                domain=None, inputs=(), outputs=(), gate_result="pending",
            ),
        ),
        issue_refs=(), openspec_refs=(), facets=(), gate_status="running",
        candidate_head=candidate, model_chain_override=override,
    )


# ---------------------------------------------------------------------------
# AC2：非額度 negative 在 quota admission shadow／enforce 下零 spawn＋精確拒因
# ---------------------------------------------------------------------------


_AC2_CASES = (
    "missing-qualification",
    "unsupported-effort",
    "pin-violation",
    "launch-permission-contract",
    "sole-reviewer-same-domain",
)


def _ac2_fixture(case: str, registry: JobRegistry, tmp_path: Path, launch_calls: list[str]):
    """回 ``(run, identities, launcher_factory, pools, expected)``。

    ``expected`` 為 ``("decision", reason, detail_fragment)`` 或
    ``("raises", ValueError, match)``：前者是 #262 runtime preflight 在建立
    worktree／job 前的結構化非 Job 決策（候選的 profile／launch 契約被拒時帶
    精確 finding），後者是既有 identity 解析層的 fail-closed 例外（production
    的 `resume_workflow_run.dispatch_or_stop` 會把它持久化成 needs_human）。
    """
    codex = {"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}
    claude_builder = {"executor": "claude", "model_id": "claude-primary", "independence_domain": "anthropic", "capabilities": ["build"]}
    claude_reviewer_only = {"executor": "claude", "model_id": "claude-primary", "independence_domain": "anthropic", "capabilities": ["review"]}
    same_domain_reviewer = {"executor": "copilot", "model_id": "gpt-review", "independence_domain": "openai", "capabilities": ["review"]}
    descriptor = _pool_descriptor(account="acct-ac2", pool="pool-ac2")

    def factory(**launcher_kwargs):
        return lambda identity: _RecordingLauncher(identity.executor, identity.model_id, launch_calls, **launcher_kwargs)

    if case == "missing-qualification":
        identities = replace(IdentityRegistry.from_rows([codex]), qualification_policy="enforce")
        run = _builder_run(registry, tmp_path, sized=True)
        return run, identities, factory(), {("codex", "gpt-primary"): descriptor}, (
            "decision", "runtime-preflight-capability_missing",
            "ExecutionAdapterError: exact-profile qualification is unknown",
        )
    if case == "unsupported-effort":
        identities = IdentityRegistry.from_rows([codex])
        run = _builder_run(registry, tmp_path)
        return run, identities, factory(effort="warp"), {("codex", "gpt-primary"): descriptor}, (
            "decision", "runtime-preflight-capability_missing",
            "ExecutionAdapterError: unsupported effort for selected descriptor",
        )
    if case == "pin-violation":
        # 凍結 pin 指向不具 build capability 的 identity：額度再充足也不得改派
        # 其他候選（#205 覆寫 fail closed，不靜默退回共享預設）。
        identities = IdentityRegistry.from_rows([codex, claude_reviewer_only])
        run = _builder_run(
            registry, tmp_path, override={"builder": {"executor": "claude", "model_id": "claude-primary"}},
        )
        return run, identities, factory(), {
            ("codex", "gpt-primary"): descriptor, ("claude", "claude-primary"): descriptor,
        }, ("raises", ValueError, "model chain override 指定的 identity 不符既有約束: builder=claude/claude-primary（不具備 build capability")
    if case == "launch-permission-contract":
        identities = IdentityRegistry.from_rows([codex, claude_builder])
        run = _builder_run(registry, tmp_path)
        return run, identities, factory(commit_capable=False), {
            ("codex", "gpt-primary"): descriptor, ("claude", "claude-primary"): descriptor,
        }, (
            "decision", "runtime-preflight-capability_missing",
            "ValueError: builder launcher lacks explicit commit-required capability",
        )
    if case == "sole-reviewer-same-domain":
        identities = IdentityRegistry.from_rows([codex, same_domain_reviewer])
        run = _reviewer_run(registry, tmp_path)
        return run, identities, factory(), {
            ("copilot", "gpt-review"): descriptor,
        }, (
            "raises", ValueError,
            r"no configured identity for workflow persona: reviewer（reviewer independence_domain 與 builder 相同：openai；"
            r"被排除 candidates: copilot/gpt-review）",
        )
    raise AssertionError(case)


@pytest.mark.parametrize("mode", ("shadow", "enforce"))
@pytest.mark.parametrize("case", _AC2_CASES)
def test_ac2_non_quota_hard_filters_zero_spawn_with_precise_reason_under_quota_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, mode: str,
) -> None:
    now_ms = int(time.time() * 1000)
    _freeze(monkeypatch, now_ms)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    launch_calls: list[str] = []
    run, identities, launcher_factory, pools, expected = _ac2_fixture(case, registry, tmp_path, launch_calls)
    shadow = QuotaShadowService(QuotaEventLedger(tmp_path / "quota-events.jsonl"))
    for descriptor in dict.fromkeys(pools.values()):
        # 額度充足：若額度層會替硬條件放行，這裡就一定會派出去。
        _observe(shadow, descriptor, "50", now_ms=now_ms)
    context = _context(tmp_path, shadow=shadow, pools=pools, enforce=(mode == "enforce"))
    jobs_before = registry.list_jobs()

    def dispatch():
        return manager.dispatch_workflow_card(
            _dispatcher(registry, worktree), run=registry.get_workflow_run(run.run_id),
            identities=identities, launcher_factory=launcher_factory,
            coordinator_root=tmp_path / "coordinator", quota_admission_context=context,
        )

    if expected[0] == "decision":
        _, reason, detail = expected
        result = dispatch()
        projection = manager.classify_dispatch_result(
            result, registry=registry, run_id=run.run_id, before_phase=run.current_phase,
        )
        assert projection == {
            "kind": "decision", "run_id": run.run_id, "current_phase": run.current_phase, "reason": reason,
        }
        assert detail in result["runtime_preflight"]["reason"]
        assert result["runtime_preflight"]["model_invocations"] == 0
        persisted = registry.get_workflow_run(run.run_id)
        assert "needs_human" in persisted.facets
        assert persisted.needs_human_reason["reason"] == reason
        assert detail in persisted.needs_human_reason["detail"]
    else:
        _, exc_type, match = expected
        with pytest.raises(exc_type, match=match):
            dispatch()
        # 同一個拒絕經 production periodic resume（`dispatch_or_stop`）持久化成
        # needs_human，理由逐字帶著精確原因，不是空洞的『派工失敗』。
        with pytest.raises(exc_type, match=match):
            manager.resume_workflow_run(
                _dispatcher(registry, worktree), run_id=run.run_id, identities=identities,
                launcher_factory=launcher_factory, coordinator_root=tmp_path / "coordinator",
                quota_admission_context=context,
            )
        persisted = registry.get_workflow_run(run.run_id)
        assert "needs_human" in persisted.facets
        assert persisted.needs_human_reason["reason"] == "workflow-card-dispatch-failed"
        assert re.search(match, persisted.needs_human_reason["detail"])

    # 零 spawn：沒有 launcher 呼叫、沒有新 job。
    assert launch_calls == []
    assert registry.list_jobs() == jobs_before
    # 額度層完全沒有介入：沒有 reservation、沒有 admit／wait receipt，也沒有
    # 把非額度拒絕寫成 quota 等待（needs_human 理由不是 quota-*）。
    assert context.authority.list_by_state("reserved", now_ms=now_ms) == ()
    assert context.authority.list_by_state("bound", now_ms=now_ms) == ()
    assert context.authority.committed(now_ms=now_ms) == {}
    assert context.store.all_rows() == []
    persisted = registry.get_workflow_run(run.run_id)
    assert persisted.quota_admission is None
    assert not manager.quota_wait_retry_is_eligible(run=persisted, quota_admission_context=context)


# ---------------------------------------------------------------------------
# AC1（dispatch 層補強）：同池換 model 不算恢復、短窗夠週窗不足也拒，獨立池可選
# ---------------------------------------------------------------------------


def test_ac1_dispatch_rejects_same_pool_model_swap_and_weekly_exhaustion_then_selects_independent_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = int(time.time() * 1000)
    _freeze(monkeypatch, now_ms)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _builder_run(registry, tmp_path)
    identities = IdentityRegistry.from_rows([
        _CODEX_ROW,
        {"executor": "codex", "model_id": "gpt-alt", "independence_domain": "openai", "capabilities": ["build"]},
        _CLAUDE_BUILDER_ROW,
    ])
    shared_pool = _pool_descriptor(
        account="acct-shared", pool="pool-shared", windows=(("short", 300_000), ("week", 604_800_000)),
    )
    independent_pool = _pool_descriptor(account="acct-independent", pool="pool-independent")
    shadow = QuotaShadowService(QuotaEventLedger(tmp_path / "quota-events.jsonl"))
    # 共享池：短窗還有 50、週窗已耗盡——兩個 codex model 綁同一個池。
    _observe(shadow, shared_pool, "50", now_ms=now_ms, window_id="short")
    _observe(shadow, shared_pool, "0", now_ms=now_ms, window_id="week")
    _observe(shadow, independent_pool, "5", now_ms=now_ms)
    context = _context(
        tmp_path, shadow=shadow, enforce=True,
        pools={
            ("codex", "gpt-primary"): shared_pool, ("codex", "gpt-alt"): shared_pool,
            ("claude", "claude-primary"): independent_pool,
        },
    )
    launch_calls: list[str] = []

    result = manager.dispatch_workflow_card(
        _dispatcher(registry, worktree), run=run, identities=identities,
        launcher_factory=lambda identity: _RecordingLauncher(identity.executor, identity.model_id, launch_calls),
        coordinator_root=tmp_path / "coordinator", quota_admission_context=context,
    )

    assert result["executor"] == "claude"
    assert launch_calls == ["claude/claude-primary"]
    decision = context.store.get(registry.get_workflow_run(run.run_id).quota_admission["builder"]["decision_id"])
    assert decision.selected["executor"] == "claude"
    assert [(item["executor"], item["model_id"], item["exclusion_reason"]) for item in decision.excluded] == [
        ("codex", "gpt-primary", "insufficient-quota"),
        ("codex", "gpt-alt", "insufficient-quota"),
    ]
    # 只替選中的獨立池原子預留；被拒的共享池（兩個窗）一單位都沒扣。
    independent_key = (
        tuple(_pool_ref(independent_pool)[k] for k in ("authority_id", "account_id", "pool_id", "revision")),
        "short",
    )
    assert context.authority.committed(now_ms=now_ms) == {independent_key: "1"}


# ---------------------------------------------------------------------------
# AC3：兩個 Manager process 真並行，barrier 對齊在 reserve 前，只有 grant 者 launch
# ---------------------------------------------------------------------------


_CODEX_ROW = {"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai", "capabilities": ["build"]}


def _ac3_manager_process(
    label: str, root: str, shared: str, descriptor_payload: dict, now_ms: int, barrier, result_queue,
) -> None:
    """一個獨立的 Manager process：自己的 registry／dispatcher／coordinator root，
    與另一個 process 只共享 reservation authority、觀測 ledger 與 decision store
    （同一 host 多 instance 的部署形狀，見 #838 AC1）。

    barrier 只插在**真正的** `QuotaReservationAuthority.reserve` 之前：兩邊都已
    經以同一份觀測判定 feasible（剩一單位），才同時搶 reserve——race 由 #838 的
    flock authority 仲裁，不是由測試排序。launcher／dispatch 決策完全是
    production 程式碼。"""
    time.time = lambda: now_ms / 1000
    root_path = Path(root)
    shared_path = Path(shared)
    launches: list[str] = []
    try:
        descriptor = schema.parse_pool_descriptor(descriptor_payload)
        registry = JobRegistry(state_path=root_path / "coordinator" / "jobs.json")
        run = registry.list_workflow_runs()[0]
        original_reserve = QuotaReservationAuthority.reserve
        waited: list[bool] = []

        def reserve_after_barrier(self, *args, **kwargs):
            if not waited:
                waited.append(True)
                barrier.wait(timeout=60)
            return original_reserve(self, *args, **kwargs)

        QuotaReservationAuthority.reserve = reserve_after_barrier
        context = quota_admission.DispatchContext(
            authority=QuotaReservationAuthority(shared_path / "reservations.jsonl"),
            store=quota_admission.AdmissionDecisionStore(shared_path / "decisions.jsonl"),
            shadow=QuotaShadowService(QuotaEventLedger(shared_path / "quota-events.jsonl")),
            descriptors=(descriptor,), unit_catalog=(),
            bindings=(_identity_binding(descriptor, executor="codex", model_id="gpt-primary"),),
            environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
        )
        result = manager.dispatch_workflow_card(
            _dispatcher(registry, root_path / "wt"), run=run,
            identities=IdentityRegistry.from_rows([_CODEX_ROW]),
            launcher_factory=lambda identity: _RecordingLauncher(identity.executor, identity.model_id, launches),
            coordinator_root=root_path / "coordinator", quota_admission_context=context,
        )
        result_queue.put((label, "ok", {"job_id": result.get("job_id"), "reason": result.get("reason")}, launches))
    except BaseException as exc:  # noqa: BLE001 - 回報給父 process
        result_queue.put((label, "error", repr(exc), launches))


def test_ac3_two_manager_processes_race_for_one_unit_only_the_grant_holder_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = int(time.time() * 1000)
    _freeze(monkeypatch, now_ms)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    descriptor = _pool_descriptor(account="acct-ac3", pool="pool-ac3")
    _observe(QuotaShadowService(QuotaEventLedger(shared / "quota-events.jsonl")), descriptor, "1", now_ms=now_ms)
    runs = {}
    for label in ("manager-a", "manager-b"):
        root = tmp_path / label
        _init_worktree(root / "wt")
        registry = JobRegistry(state_path=root / "coordinator" / "jobs.json")
        runs[label] = _builder_run(registry, root, work_id=f"acceptance-839-{label}").run_id

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_ac3_manager_process,
            args=(label, str(tmp_path / label), str(shared), descriptor.to_dict(), now_ms, barrier, result_queue),
        )
        for label in runs
    ]
    for process in processes:
        process.start()
    results = {}
    for _ in processes:
        label, status, payload, launches = result_queue.get(timeout=120)
        results[label] = (status, payload, launches)
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0

    assert {status for status, _payload, _launches in results.values()} == {"ok"}, results
    winners = [label for label, (_s, payload, _l) in results.items() if payload["job_id"]]
    losers = [label for label, (_s, payload, _l) in results.items() if not payload["job_id"]]
    assert len(winners) == 1 and len(losers) == 1, results
    winner, loser = winners[0], losers[0]
    # 只有取得 grant 的 Manager 呼叫 launcher；落敗者連 launcher 都沒碰。
    assert results[winner][2] == ["codex/gpt-primary"]
    assert results[loser][2] == []
    assert results[loser][1]["reason"] == "quota-admission-insufficient"

    authority = QuotaReservationAuthority(shared / "reservations.jsonl")
    bound = authority.list_by_state("bound", now_ms=now_ms)
    assert len(bound) == 1
    assert bound[0].job_id == results[winner][1]["job_id"]
    assert authority.list_by_state("reserved", now_ms=now_ms) == ()
    pool_key = (tuple(_pool_ref(descriptor)[k] for k in ("authority_id", "account_id", "pool_id", "revision")), "short")
    # 沒有半額度保留：整個 pool 只被 grant 者佔用一單位。
    assert authority.committed(now_ms=now_ms) == {pool_key: "1"}

    winner_registry = JobRegistry(state_path=tmp_path / winner / "coordinator" / "jobs.json")
    loser_registry = JobRegistry(state_path=tmp_path / loser / "coordinator" / "jobs.json")
    assert [job["job_id"] for job in winner_registry.list_jobs()] == [results[winner][1]["job_id"]]
    assert loser_registry.list_jobs() == []
    loser_run = loser_registry.get_workflow_run(runs[loser])
    assert loser_run.needs_human_reason["reason"] == "quota-admission-insufficient"
    store = quota_admission.AdmissionDecisionStore(shared / "decisions.jsonl")
    wait = store.get(loser_run.quota_admission["builder"]["decision_id"])
    assert wait is not None and wait.outcome == "wait" and wait.reservation_id is None
    assert [item["exclusion_reason"] for item in wait.excluded] == ["reservation-denied"]
    admit = store.get(winner_registry.get_workflow_run(runs[winner]).quota_admission["builder"]["decision_id"])
    assert admit is not None and admit.outcome == "admit" and admit.reservation_id == bound[0].reservation_id


# ---------------------------------------------------------------------------
# AC4：spawn 時 429 → 結構化 rate-limit＋耐久退避；新 attempt 才重選；不降品質分
# ---------------------------------------------------------------------------


_CLAUDE_BUILDER_ROW = {
    "executor": "claude", "model_id": "claude-primary", "independence_domain": "anthropic", "capabilities": ["build"],
}


def test_ac4_spawn_time_429_is_structured_durable_and_reselects_only_at_a_new_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = int(time.time() * 1000)
    clock = _freeze(monkeypatch, now_ms)
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _builder_run(registry, tmp_path, sized=True)
    step = manager._current_workflow_step(run)
    identities = IdentityRegistry.from_rows([_CODEX_ROW, _CLAUDE_BUILDER_ROW])
    launch_calls: list[str] = []
    failing = {"codex"}

    def launcher_factory(identity):
        return _RecordingLauncher(
            identity.executor, identity.model_id, launch_calls, fail_429=identity.executor in failing,
        )

    # 觀測 ledger、reservation authority、decision store 全部 file-backed：下面
    # 以全新 handle 重讀，證明消耗／退避是耐久紀錄，不是記憶體狀態。
    shadow = QuotaShadowService(QuotaEventLedger(tmp_path / "quota-events.jsonl"))
    codex_pool = _pool_descriptor(account="acct-codex", pool="pool-codex")
    claude_pool = _pool_descriptor(account="acct-claude", pool="pool-claude")
    _observe(shadow, codex_pool, "5", now_ms=now_ms)
    _observe(shadow, claude_pool, "5", now_ms=now_ms)
    context = _context(
        tmp_path, shadow=shadow,
        pools={("codex", "gpt-primary"): codex_pool, ("claude", "claude-primary"): claude_pool}, enforce=True,
    )
    dispatcher = _dispatcher(registry, worktree)
    ranking_before = [
        (candidate.executor, candidate.model_id)
        for candidate in manager._workflow_identity_candidates(run, step, identities)
    ]
    assert ranking_before == [("codex", "gpt-primary"), ("claude", "claude-primary")]

    # --- attempt n0：派前額度可行、reserve＋bind 成功，spawn 當下 429。
    with pytest.raises(_Structured429):
        manager.dispatch_workflow_card(
            dispatcher, run=registry.get_workflow_run(run.run_id), identities=identities,
            launcher_factory=launcher_factory, coordinator_root=coordinator, quota_admission_context=context,
        )
    [failed_job] = registry.list_jobs()
    assert failed_job["status"] == "failed"
    assert failed_job["executor"] == "codex"
    # #825／#826 結構化分類：HTTP 429 是 structured rate_limited、可 bounded
    # retry——不是 LAUNCH_FAILED，也不是 content（模型品質）類失敗。
    assert {key: failed_job["provider_outcome"][key] for key in ("outcome", "authority", "retryable")} == {
        "outcome": "rate_limited", "authority": "structured", "retryable": True,
    }
    n0_decision = context.store.get(quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=f"{run.run_id}:{step.card}:n0",
        profile_key=registry.get_workflow_run(run.run_id).execution_profile_bindings["builder"]["resolved_key"],
        mode="enforced",
    ))
    assert n0_decision is not None and n0_decision.selected["executor"] == "codex"
    fresh_authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    assert fresh_authority.status(n0_decision.reservation_id, now_ms=clock[0]).state == "settled"
    assert fresh_authority.committed(now_ms=clock[0]) == {}
    backoff = executor_backoff.active_backoff(coordinator, "codex", "gpt-primary", now=clock[0] / 1000)
    assert backoff.backoff is not None
    assert backoff.backoff.outcome.value == "rate_limited"
    assert backoff.backoff.last_terminal_key == failed_job["job_id"]
    qualification_after_429 = registry.get_workflow_run(run.run_id).model_qualification
    assert qualification_after_429 == {"builder": "not-enforced"}

    # --- 同一 attempt 重送（restart／start replay，沒有新 attempt 邊界）：回原本
    # 那顆 failed job，不在同一 attempt 內偷換候選、不再 reserve、不寫新 receipt。
    receipts_before_replay = context.store.all_rows()
    replay = manager.dispatch_workflow_card(
        _dispatcher(JobRegistry(state_path=tmp_path / "jobs.json"), worktree),
        run=registry.get_workflow_run(run.run_id), identities=identities,
        launcher_factory=launcher_factory, coordinator_root=coordinator, quota_admission_context=context,
    )
    assert replay["job_id"] == failed_job["job_id"]
    assert launch_calls == ["codex/gpt-primary"]
    assert context.store.all_rows() == receipts_before_replay
    assert fresh_authority.list_by_state("reserved", now_ms=clock[0]) == ()
    assert fresh_authority.list_by_state("bound", now_ms=clock[0]) == ()

    # --- 安全新 attempt：production periodic resume 依 structured rate_limited 做
    # bounded provider-failure retry，於 attempt n1 重新准入；codex 仍在耐久
    # backoff 中被跳過，改選 claude。
    resumed = manager.resume_workflow_run(
        dispatcher, run_id=run.run_id, identities=identities, launcher_factory=launcher_factory,
        coordinator_root=coordinator, quota_admission_context=context,
    )
    assert resumed["reason"] == "provider-failure-retry"
    assert resumed["provider_outcome"] == "rate_limited"
    assert launch_calls == ["codex/gpt-primary", "claude/claude-primary"]
    retry_job = registry.get_job(resumed["job_id"])
    assert retry_job["executor"] == "claude"
    attempts = {
        row["attempt_id"]: (row["outcome"], (row.get("selected") or {}).get("executor"))
        for row in context.store.all_rows()
    }
    assert attempts == {
        f"{run.run_id}:{step.card}:n0": ("admit", "codex"),
        f"{run.run_id}:{step.card}:n1": ("admit", "claude"),
    }
    assert registry.get_workflow_run(run.run_id).model_qualification == qualification_after_429

    # --- 不降品質分：429 只留下有期限的 backoff。期滿後候選排序原封不動，下一個
    # 全新 attempt（另一張卡片的第一次派工）仍選回原本排第一的 codex，且 codex
    # 不在 excluded 內——infra 失敗沒有被記成永久的品質／資格降級。
    clock[0] = int(backoff.backoff.deadline_epoch * 1000) + 1_000
    _observe(shadow, codex_pool, "5", now_ms=clock[0])
    assert executor_backoff.active_backoff(coordinator, "codex", "gpt-primary", now=clock[0] / 1000).backoff is None
    assert [
        (candidate.executor, candidate.model_id)
        for candidate in manager._workflow_identity_candidates(run, step, identities)
    ] == ranking_before
    failing.clear()
    fresh_run = _builder_run(registry, tmp_path, sized=True, work_id="acceptance-839-after-backoff")
    fresh = manager.dispatch_workflow_card(
        dispatcher, run=fresh_run, identities=identities, launcher_factory=launcher_factory,
        coordinator_root=coordinator, quota_admission_context=context,
    )
    assert fresh["executor"] == "codex"
    fresh_decision = context.store.get(
        registry.get_workflow_run(fresh_run.run_id).quota_admission["builder"]["decision_id"]
    )
    assert fresh_decision.selected["executor"] == "codex"
    assert fresh_decision.excluded == ()
    assert registry.get_workflow_run(fresh_run.run_id).model_qualification == {"builder": "not-enforced"}


# ---------------------------------------------------------------------------
# AC6：daemon start／forced retry 遇真 quota wait 的 #830 分類
# ---------------------------------------------------------------------------


def _install_quota_pools_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, descriptor) -> None:
    """寫一份真的 operator quota-pools 設定檔並開 enforce——daemon 經
    `_quota_admission_context_for()` 以預設路徑的 file-backed ledger／authority／
    decision store 建 context，不注入測試替身。"""
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text(json.dumps({
        "schema": quota_admission.QUOTA_POOLS_CONFIG_SCHEMA,
        "config_revision": "acceptance-839-v1",
        "descriptors": [descriptor.to_dict()],
        "unit_catalog": [],
        "bindings": [_identity_binding(descriptor, executor="codex", model_id="gpt-primary").to_dict()],
    }), encoding="utf-8")
    monkeypatch.setenv("PSC_QUOTA_POOLS_CONFIG", str(config_path))
    monkeypatch.setenv("PSC_QUOTA_ADMISSION_ENFORCE", "on")
    manager_daemon._QUOTA_POOLS_CONFIG_CACHE.clear()


def _canonical_authority(tmp_path: Path):
    """把 WorkAuthority snapshot 寫到 Manager 讀取的 canonical 路徑，讓 daemon 的
    Builder Todo admission（#847）照 production 判準放行，而不是被 monkeypatch 掉。"""
    from paulsha_cortex.coordinator import work_actions
    from paulsha_cortex.coordinator.claim import canonical_work_snapshot_path

    from test_midchain_builder_retry_545 import REPO as AUTHORITY_REPO, WORK_ID as AUTHORITY_WORK_ID, _snapshot

    canonical = canonical_work_snapshot_path()
    canonical.parent.mkdir(parents=True, exist_ok=True)
    _snapshot(canonical)
    return work_actions.load_work_authority(repo=AUTHORITY_REPO, work_id=AUTHORITY_WORK_ID, snapshot_path=canonical)


def test_ac6_daemon_start_classifies_real_quota_wait_as_decision_without_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paulsha_cortex.coordinator import work_actions

    now_ms = int(time.time() * 1000)
    clock = _freeze(monkeypatch, now_ms)
    descriptor = _pool_descriptor(account="acct-start", pool="pool-start")
    _install_quota_pools_config(tmp_path, monkeypatch, descriptor)
    default_shadow = QuotaShadowService(QuotaEventLedger())
    _observe(default_shadow, descriptor, "0", now_ms=now_ms)
    authority = _canonical_authority(tmp_path)
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id=authority.work_id, repo=authority.repo,
        claim_key=work_actions._expected_claim_key(authority),
        source_revision=work_actions.work_authority_digest(authority),
        workspace_root=str(tmp_path), combo="feature-oneshot", current_phase="build",
        steps=(WorkflowStep(
            phase="build", persona="builder", card="subagent-build", executor=None, model=None,
            domain=None, inputs=(), outputs=(), gate_result="pending",
        ),),
        issue_refs=tuple(f"{authority.repo}#{n}" for n in authority.mapped_issues),
        openspec_refs=authority.mapped_openspec, gate_status="running",
    )
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    launch_calls: list[str] = []
    launcher = _RecordingLauncher("codex", "gpt-primary", launch_calls)
    dispatcher = _dispatcher(registry, worktree)
    # start 的 run 建立（manifest／planning 載入）不是本條驗收對象；派工、
    # quota context、builder todo admission 與 #830 分類全走 production。
    monkeypatch.setattr(
        manager, "apply_workflow_action",
        lambda *args, **kwargs: {"run_id": run.run_id, "current_phase": run.current_phase},
    )
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher, specs_dir=str(tmp_path / "specs"), handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([_CODEX_ROW]), launcher=launcher,
    )

    result = executor(build_request(req_type="workflow-action", args={"action": "start"}, requested_by="operator"))

    assert result["dispatch"] == {
        "kind": "decision", "run_id": run.run_id, "current_phase": "build",
        "reason": "quota-admission-insufficient",
    }
    assert "job_id" not in result
    assert launch_calls == []
    assert registry.list_jobs() == []
    waiting = registry.get_workflow_run(run.run_id)
    assert waiting.needs_human_reason["reason"] == "quota-admission-insufficient"
    store = quota_admission.AdmissionDecisionStore()
    wait = store.get(waiting.quota_admission["builder"]["decision_id"])
    assert wait is not None
    assert (wait.mode, wait.outcome, wait.retry_eligible) == ("enforced", "wait", True)
    assert [item["exclusion_reason"] for item in wait.excluded] == ["insufficient-quota"]
    assert QuotaReservationAuthority().committed(now_ms=now_ms) == {}

    # 同一個 production daemon：額度恢復後 periodic tick 自動續派（不需 operator），
    # 且只派一次。
    clock[0] += 1_000
    _observe(default_shadow, descriptor, "5", now_ms=clock[0])
    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher, specs_dir=str(tmp_path / "specs"), handoff_dir=str(tmp_path / "handoff"),
        launcher=launcher, workflow_identity_registry=IdentityRegistry.from_rows([_CODEX_ROW]),
        scan_specs_fn=lambda _dir: [], auto_claim_fn=lambda: [],
        run_tick_fn=lambda *args, **kwargs: {"dispatched": []},
    )
    runner()
    runner()
    assert launch_calls == ["codex/gpt-primary"]
    [job] = registry.list_jobs()
    assert job["workflow_run_id"] == run.run_id
    resumed = registry.get_workflow_run(run.run_id)
    assert "needs_human" not in resumed.facets
    admit = store.get(resumed.quota_admission["builder"]["decision_id"])
    assert (admit.outcome, admit.selected["executor"]) == ("admit", "codex")


def test_ac6_forced_retry_card_hitting_real_quota_wait_fails_loudly_without_replacement_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paulsha_cortex.coordinator import work_actions

    from test_midchain_builder_retry_545 import _stuck_run

    now_ms = int(time.time() * 1000)
    clock = _freeze(monkeypatch, now_ms)
    monkeypatch.setenv("PSC_GATE_CMD_PYTEST", "python3 -m pytest -q")
    descriptor = _pool_descriptor(account="acct-retry", pool="pool-retry")
    _install_quota_pools_config(tmp_path, monkeypatch, descriptor)
    default_shadow = QuotaShadowService(QuotaEventLedger())
    _observe(default_shadow, descriptor, "0", now_ms=now_ms)
    snapshot, registry, run, stuck_job_id = _stuck_run(tmp_path)
    _canonical_authority(tmp_path)
    jobs_before = [dict(row) for row in registry.list_jobs()]
    launch_calls: list[str] = []
    launcher = _RecordingLauncher("codex", "gpt-primary", launch_calls)
    from git_fixtures import StubWorktreeCreator

    dispatcher = type(
        "D", (), {"_registry": registry, "_git_runner": None, "_worktree_creator": StubWorktreeCreator(tmp_path)},
    )()

    def real_retry_card(*, args, requested_by):
        # 正式 work action：exact run CAS＋卡名、清 needs_human、重置卡片。
        return work_actions.execute_work_action(
            args=args, requested_by=requested_by, snapshot_path=snapshot,
            state_path=tmp_path / "runs.json", workflow_registry=registry,
        )

    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher, specs_dir=str(tmp_path / "specs"), handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([_CODEX_ROW]), launcher=launcher,
        work_action_fn=real_retry_card,
    )

    def retry_card_request():
        current = registry.get_workflow_run(run.run_id)
        return build_request(
            req_type="work-action",
            args={
                "action": "retry-card", "repo": current.repo, "work_id": current.work_id,
                "expected_run_id": current.run_id, "card": "tdd-red",
            },
            requested_by="operator",
        )

    with pytest.raises(RuntimeError, match=r"retry-card produced no builder Job（dispatch decision: quota-admission-insufficient）"):
        executor(retry_card_request())

    # 不假稱成功：沒有 replacement job、launcher 沒被呼叫、舊 job 原封不動。
    assert launch_calls == []
    assert registry.list_jobs() == jobs_before
    failed = registry.get_workflow_run(run.run_id)
    assert "needs_human" in failed.facets
    assert failed.needs_human_reason["reason"] == "forced-card-retry-failed"
    assert "quota-admission-insufficient" in failed.needs_human_reason["detail"]
    # 額度層自己的證據仍在：這個 forced attempt 的 wait receipt 耐久存在。
    store = quota_admission.AdmissionDecisionStore()
    wait = store.get(failed.quota_admission["builder"]["decision_id"])
    assert wait is not None and (wait.outcome, wait.reason) == ("wait", "quota-admission-insufficient")
    assert wait.attempt_id == f"{run.run_id}:tdd-red:n1"
    assert QuotaReservationAuthority().committed(now_ms=now_ms) == {}
    # operator 明確要求的重派沒有被 periodic tick 當成一般 quota wait 靜默改派：
    # forced 意圖不耐久，仍須 operator 在額度恢復後重下 retry-card。
    assert not manager.quota_wait_retry_is_eligible(
        run=failed, quota_admission_context=manager_daemon._quota_admission_context_for(),
    )

    # 額度恢復後同一個 operator 動作才真的派出新 job（同一個 attempt n1）。
    clock[0] += 1_000
    _observe(default_shadow, descriptor, "5", now_ms=clock[0])
    result = executor(retry_card_request())
    assert launch_calls == ["codex/gpt-primary"]
    assert result["result"]["dispatch"]["kind"] == "job"
    replacement = registry.get_job(result["result"]["job_id"])
    assert replacement["job_id"] != stuck_job_id and replacement["workflow_card"] == "tdd-red"
    admit = store.get(registry.get_workflow_run(run.run_id).quota_admission["builder"]["decision_id"])
    assert (admit.outcome, admit.attempt_id) == ("admit", f"{run.run_id}:tdd-red:n1")
