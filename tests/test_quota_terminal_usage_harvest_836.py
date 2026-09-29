"""#836 補缺：受管 job 終局用量的合計扣減與 shadow 收割。

- G836-1（AC4）：controller／worker／reviewer 三個角色、各自不同的 resolved
  profile key、同一個 account/pool，經 production helper
  ``manager._quota_admission_record_terminal_usage`` 記終局 usage 後，投影的
  剩餘量恰為 snapshot 減去三個 job 的用量總和；原樣重播、重啟後重播、以另一個
  profile key 重播都不再扣。
- G836-2（owner 2026-09-29 裁決屬本票）：shadow 模式下 Cortex 自家受管 job 的
  終局用量也要寫進 quota ledger。production 接線是 periodic tick 的
  ``manager.reconcile_quota_admission_reservations``（第三步：
  ``harvest_quota_terminal_usage``）；job 與 admit receipt 的對應只認 job 事實
  （enforce 用 ``quota_decision_id`` 精確比對；shadow 用 attempt ordinal＋
  executor／model_id，對不上或有歧義就不記），重複 harvest、重啟、半途中斷都
  不重複記帳。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

import pytest

from paulsha_cortex.coordinator import manager, quota_admission
from paulsha_cortex.coordinator import quota_observation as schema
from paulsha_cortex.coordinator.quota_ledger import QuotaEventLedger
from paulsha_cortex.coordinator.quota_reservation import QuotaReservationAuthority
from paulsha_cortex.coordinator.quota_shadow import QuotaShadowService
from paulsha_cortex.coordinator.registry import JobRegistry

from test_quota_admission_dispatch_wiring_839 import (
    _FakeWorktreeCreator,
    _Launcher,
    _init_worktree,
    _make_run,
    _pool_ref,
    _resolved_profile_key,
    _two_builder_identities,
)


_NOW = 1_800_000_000_000
_PROFILE_CONTROLLER = "epk:v1:resolved:" + "c" * 64
_PROFILE_WORKER = "epk:v1:resolved:" + "d" * 64
_PROFILE_REVIEWER = "epk:v1:resolved:" + "e" * 64
_TOKEN_UNIT = {
    "schema_version": 1, "unit_id": "token", "version": "1",
    "quantity_kind": "amount", "semantics_ref": "fixture:native-token/v1",
}
_USAGE_UNIT_REFS = {"input_tokens": ("token", "1"), "output_tokens": ("token", "1")}


def _iso(ms: int) -> str:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)).isoformat()


def _descriptor(*, account: str = "acct-shared", pool: str = "pool-shared",
                windows=(("short", 300_000), ("week", 604_800_000))) -> schema.PoolDescriptor:
    return schema.parse_pool_descriptor({
        "schema_version": 1, "authority_id": "operator-budget-authority", "account_id": account,
        "pool_id": pool, "revision": "1", "authority_ref": "fixture:operator-pool-map/v1",
        "provenance_refs": ["fixture:pool-map/v1"],
        "units": [{k: v for k, v in _TOKEN_UNIT.items() if k != "schema_version"}],
        "windows": [
            {"window_id": window_id, "kind": "rolling",
             "unit_ref": {"unit_id": "token", "version": "1"}, "duration_ms": duration_ms}
            for window_id, duration_ms in windows
        ],
    })


def _profile_binding(descriptor, profile_key: str, *, binding_id: str) -> schema.ProfilePoolBinding:
    return schema.parse_binding({
        "schema_version": 1, "binding_id": binding_id, "revision": "1",
        "subject": {"kind": "profile",
                    "profile_ref": {"state": "known", "value": {"schema_version": 1, "key": profile_key}}},
        "constraints": [
            {"state": "known", "value": {"pool_ref": _pool_ref(descriptor), "window_id": window["window_id"]}}
            for window in descriptor.to_dict()["windows"]
        ],
        "coverage": {"state": "complete", "gaps": []},
    }, descriptors=(descriptor,))


def _group_binding(descriptor, keys, *, binding_id: str) -> schema.ProfilePoolBinding:
    return schema.parse_binding({
        "schema_version": 1, "binding_id": binding_id, "revision": "1",
        "subject": {"kind": "group", "group_ref": "fixture:all-roles/v1", "revision": "1",
                    "members": {"state": "known",
                                "value": [{"schema_version": 1, "key": key} for key in keys]}},
        "constraints": [
            {"state": "known", "value": {"pool_ref": _pool_ref(descriptor), "window_id": window["window_id"]}}
            for window in descriptor.to_dict()["windows"]
        ],
        "coverage": {"state": "complete", "gaps": []},
    }, descriptors=(descriptor,))


def _snapshot(descriptor, window_id: str, value: str, *, observed_at_ms: int, reset_at_ms: int,
              profile_key: str, ttl_ms: int = 3_600_000) -> dict:
    duration = next(w["duration_ms"] for w in descriptor.to_dict()["windows"] if w["window_id"] == window_id)
    return {
        "schema_version": 1,
        "observation_id": f"snapshot-{descriptor.pool_id}-{window_id}-{observed_at_ms}",
        "scope": {"state": "known", "value": {"pool_ref": _pool_ref(descriptor), "window_id": window_id}},
        "profile_ref": {"state": "known", "value": {"schema_version": 1, "key": profile_key}},
        "unit_ref": {"state": "known", "value": {"unit_id": "token", "version": "1"}},
        "window_instance": {
            "kind": "interval", "start_ms": reset_at_ms - duration, "end_ms": reset_at_ms,
            "epoch": {"state": "known", "value": f"reset-{reset_at_ms}"},
        },
        "measurement": {"kind": "remaining_snapshot", "metric_id": "remaining",
                        "quantity": {"state": "observed", "amount": {"kind": "exact", "value": value}}},
        "observed_at_ms": {"state": "known", "value": observed_at_ms},
        "received_at_ms": observed_at_ms,
        "ttl_ms": {"state": "known", "value": ttl_ms},
        "reset_at_ms": {"state": "known", "value": reset_at_ms},
        "source": {
            "source_id": "fixture-provider", "source_schema": "fixture-quota-v1",
            "adapter_version": "fixture-adapter-v1", "authority_ref": "fixture:provider-contract/v1",
            "method": "provider_status", "provenance_refs": ["fixture:source-document/v1"],
            "event_identity": {"state": "unknown", "reason": "provider-has-no-event-id"},
        },
        "coverage": {"state": "complete", "gaps": []},
    }


def _unit():
    return schema.parse_unit_definition(dict(_TOKEN_UNIT))


def _ctx(tmp_path: Path, *, descriptors, bindings, environment=None, ledger_name="events.jsonl"):
    return quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "quota" / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "quota" / "decisions.jsonl"),
        shadow=QuotaShadowService(QuotaEventLedger(tmp_path / "quota" / ledger_name)),
        descriptors=tuple(descriptors), unit_catalog=(_unit(),), bindings=tuple(bindings),
        environment={} if environment is None else environment,
        usage_unit_refs=dict(_USAGE_UNIT_REFS),
    )


def _row(report, pool_id: str, window_id: str):
    return next(r for r in report["pools"]
                if r["pool_ref"]["pool_id"] == pool_id and r["window_id"] == window_id)


def _usage_rows(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line]
    return [row for row in rows if row.get("kind") == "observation"
            and row["observation"]["source"]["source_id"] == "cortex-executor-usage"]


# ---------------------------------------------------------------------------
# G836-1（AC4）：三角色同帳號合計扣減的數值
# ---------------------------------------------------------------------------


def test_ac4_controller_worker_reviewer_same_account_total_deduction_is_exact(tmp_path: Path) -> None:
    """三個角色各自一個 resolved profile key（例如 live 的
    `codex-luna-builder／reviewer／planner` 三條 binding 綁同一個 pool），另有
    一條涵蓋三者的 group binding（profile alias）。經 production helper 記終局
    usage 後，short／week 兩個 window 的剩餘量恰為 snapshot 減去三個 job 用量總和
    （2+1+3+1=7），不因 binding 重疊、原樣重播、重啟、跨 profile 重播而多扣。"""
    descriptor = _descriptor()
    bindings = (
        _profile_binding(descriptor, _PROFILE_CONTROLLER, binding_id="controller"),
        _profile_binding(descriptor, _PROFILE_WORKER, binding_id="worker"),
        _profile_binding(descriptor, _PROFILE_REVIEWER, binding_id="reviewer"),
        _group_binding(descriptor, (_PROFILE_CONTROLLER, _PROFILE_WORKER, _PROFILE_REVIEWER),
                       binding_id="all-roles"),
    )
    ctx = _ctx(tmp_path, descriptors=(descriptor,), bindings=bindings)
    for window_id, value, duration in (("short", "20", 300_000), ("week", "50", 604_800_000)):
        result = ctx.shadow.record_observation(
            _snapshot(descriptor, window_id, value, observed_at_ms=_NOW,
                      reset_at_ms=_NOW + duration, profile_key=_PROFILE_CONTROLLER),
            descriptors=(descriptor,), unit_catalog=(_unit(),),
        )
        assert result.accepted == 1

    jobs = (
        (_PROFILE_CONTROLLER, {"job_id": "job-controller", "executor": "codex", "model_id": "gpt-x",
                               "usage": {"input_tokens": 2, "output_tokens": 1},
                               "started_at": _iso(_NOW + 1_000), "exited_at": _iso(_NOW + 2_000)}),
        (_PROFILE_WORKER, {"job_id": "job-worker", "executor": "codex", "model_id": "gpt-x",
                           "usage": {"input_tokens": 3, "output_tokens": 0},
                           "started_at": _iso(_NOW + 3_000), "exited_at": _iso(_NOW + 4_000)}),
        (_PROFILE_REVIEWER, {"job_id": "job-reviewer", "executor": "codex", "model_id": "gpt-x",
                             "usage": {"input_tokens": 1, "output_tokens": 0},
                             "started_at": _iso(_NOW + 5_000), "exited_at": _iso(_NOW + 6_000)}),
    )
    for profile_key, job in jobs:
        manager._quota_admission_record_terminal_usage(
            ctx, profile_key=profile_key, job=job, now_ms=_NOW + 10_000,
        )

    def _project(shadow: QuotaShadowService):
        return shadow.project(descriptors=(descriptor,), unit_catalog=(_unit(),), now_utc_ms=_NOW + 20_000)

    report = _project(ctx.shadow)
    assert _row(report, "pool-shared", "short")["remaining"] == {
        "state": "observed", "amount": {"kind": "exact", "value": "13"}}
    assert _row(report, "pool-shared", "week")["remaining"] == {
        "state": "observed", "amount": {"kind": "exact", "value": "43"}}
    # 每個 job、每個 metric、每個 window 恰好一筆 usage row：3 job × 2 metric
    # × 2 window = 12 筆，重疊的 group binding 不另生一筆。（有 mapping 的
    # metric 缺值會讓整個 pool 轉 unknown，見 #836 AC3 契約，所以三個 job 都
    # 帶齊兩個 metric。）
    ledger_path = tmp_path / "quota" / "events.jsonl"
    assert len(_usage_rows(ledger_path)) == 12

    # 原樣重播＋重啟後（新 ledger instance 開同一檔）以較晚的時間重播＋把 worker
    # 的 job 以 controller 的 profile key 重播（alias），都不能多扣。
    restarted = _ctx(tmp_path, descriptors=(descriptor,), bindings=bindings)
    for profile_key, job in jobs:
        manager._quota_admission_record_terminal_usage(
            ctx, profile_key=profile_key, job=dict(job), now_ms=_NOW + 11_000,
        )
        manager._quota_admission_record_terminal_usage(
            restarted, profile_key=profile_key, job=dict(job), now_ms=_NOW + 12_000,
        )
    manager._quota_admission_record_terminal_usage(
        restarted, profile_key=_PROFILE_CONTROLLER, job=dict(jobs[1][1]), now_ms=_NOW + 13_000,
    )
    assert len(_usage_rows(ledger_path)) == 12
    replayed = _project(restarted.shadow)
    assert _row(replayed, "pool-shared", "short")["remaining"]["amount"] == {"kind": "exact", "value": "13"}
    assert _row(replayed, "pool-shared", "week")["remaining"]["amount"] == {"kind": "exact", "value": "43"}


# ---------------------------------------------------------------------------
# G836-2：shadow 模式由 periodic tick 收割受管 job 的終局 usage
# ---------------------------------------------------------------------------


def _dispatch(registry, run, identities, worktree, coordinator_root, *, ctx):
    dispatcher = type(
        "D", (), {"_registry": registry, "_git_runner": None, "_worktree_creator": _FakeWorktreeCreator(worktree)}
    )()
    return manager.dispatch_workflow_card(
        dispatcher, run=run, identities=identities,
        launcher_factory=lambda identity: _Launcher(identity.executor, identity.model_id),
        coordinator_root=coordinator_root, quota_admission_context=ctx,
    )


def _finish_job_with_codex_usage(registry: JobRegistry, job_id: str, *, input_tokens: int,
                                 output_tokens: int, status: str = "exited") -> dict:
    """真 `registry.update_headless_result` 路徑：先在 job 的 log 寫一行 codex
    `turn.completed`，讓 #325 usage extractor 自己抽出 usage，不手填 job 欄位。"""
    job = registry.get_job(job_id)
    log_path = Path(job["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
    }}) + "\n", encoding="utf-8")
    return registry.update_headless_result(job_id, status=status, exit_code=0 if status == "exited" else 1)


def _dispatch_fixture(tmp_path: Path, *, environment=None):
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _make_run(registry, workspace_root=tmp_path / "workspace")
    identities = _two_builder_identities()
    step = manager._current_workflow_step(run)
    codex_identity = next(c for c in manager._workflow_identity_candidates(run, step, identities)
                          if c.executor == "codex")
    profile_key = _resolved_profile_key(run, step, codex_identity, "codex", "gpt-primary")
    descriptor = _descriptor(account="acct-codex", pool="pool-codex", windows=(("week", 604_800_000),))
    ctx = _ctx(tmp_path, descriptors=(descriptor,),
               bindings=(_profile_binding(descriptor, profile_key, binding_id="codex-builder"),),
               environment=environment)
    now_ms = int(time.time() * 1000)
    assert ctx.shadow.record_observation(
        _snapshot(descriptor, "week", "20", observed_at_ms=now_ms - 1_000,
                  reset_at_ms=now_ms + 600_000_000, profile_key=profile_key),
        descriptors=(descriptor,), unit_catalog=(_unit(),),
    ).accepted == 1
    return registry, run, identities, worktree, ctx, descriptor, profile_key


def _project_now(ctx, descriptor):
    return ctx.shadow.project(descriptors=(descriptor,), unit_catalog=(_unit(),),
                              now_utc_ms=int(time.time() * 1000) + 1_000)


def test_shadow_dispatch_terminal_usage_is_harvested_once_by_periodic_reconcile(tmp_path: Path) -> None:
    registry, run, identities, worktree, ctx, descriptor, _ = _dispatch_fixture(tmp_path)
    dispatched = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", ctx=ctx)
    job_id = dispatched["job_id"]
    job = registry.get_job(job_id)
    assert job["executor"] == "codex"
    assert job.get("quota_decision_id") is None  # shadow：job 形狀與沒接線時相同
    ledger_path = tmp_path / "quota" / "events.jsonl"

    # job 還在跑：不記任何 usage，也沒有 reservation 可以收斂。
    running = manager.reconcile_quota_admission_reservations(registry=registry, quota_admission_context=ctx)
    assert running["wired"] is True
    assert running["bound"] == [] and running["reserved"] == []
    assert _usage_rows(ledger_path) == []

    _finish_job_with_codex_usage(registry, job_id, input_tokens=3, output_tokens=2)
    first = manager.reconcile_quota_admission_reservations(registry=registry, quota_admission_context=ctx)
    harvested = first["terminal_usage"]
    assert harvested["wired"] is True
    assert [item["job_id"] for item in harvested["recorded"]] == [job_id]
    assert harvested["recorded"][0]["accepted"] == 2  # input／output 兩個 metric × 一個 window
    rows = _usage_rows(ledger_path)
    assert len(rows) == 2
    assert {row["observation"]["measurement"]["metric_id"] for row in rows} == {"input_tokens", "output_tokens"}
    assert all(row["idempotency_key"].startswith(f"caller:terminal-usage:v1:{job_id}:") for row in rows)
    remaining = _row(_project_now(ctx, descriptor), "pool-codex", "week")["remaining"]
    assert remaining == {"state": "observed", "amount": {"kind": "exact", "value": "15"}}

    # 重複 harvest（下一輪 periodic tick）與重啟（新 ledger／store instance）都
    # 不新增 row、不再扣。
    second = manager.reconcile_quota_admission_reservations(registry=registry, quota_admission_context=ctx)
    assert second["terminal_usage"]["recorded"] == []
    restarted = _ctx(tmp_path, descriptors=ctx.descriptors, bindings=ctx.bindings)
    third = manager.reconcile_quota_admission_reservations(registry=registry, quota_admission_context=restarted)
    assert third["terminal_usage"]["recorded"] == []
    assert len(_usage_rows(ledger_path)) == 2
    assert _row(_project_now(restarted, descriptor), "pool-codex", "week")["remaining"]["amount"] == {
        "kind": "exact", "value": "15"}


def test_enforced_settle_and_harvest_record_the_same_job_only_once(tmp_path: Path) -> None:
    """enforce：periodic reconcile 先把 bound reservation 收斂成終局並經
    `on_settled` 記 usage，同一輪的 harvest 看到同一個 job 時不得再記一次；
    reservation 釋放後容量只剩 usage 這一份扣減（不是 usage＋reservation）。"""
    registry, run, identities, worktree, ctx, descriptor, _ = _dispatch_fixture(
        tmp_path, environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
    )
    dispatched = _dispatch(registry, run, identities, worktree, tmp_path / "coordinator", ctx=ctx)
    job_id = dispatched["job_id"]
    assert isinstance(registry.get_job(job_id).get("quota_decision_id"), str)
    [bound] = ctx.authority.list_by_state("bound", now_ms=int(time.time() * 1000))
    assert bound.job_id == job_id

    _finish_job_with_codex_usage(registry, job_id, input_tokens=4, output_tokens=1)
    result = manager.reconcile_quota_admission_reservations(registry=registry, quota_admission_context=ctx)
    assert [item["action"] for item in result["bound"]] == ["settled"]
    assert result["terminal_usage"]["recorded"] == []  # on_settled 已經記過
    assert len(_usage_rows(tmp_path / "quota" / "events.jsonl")) == 2
    assert ctx.authority.committed(now_ms=int(time.time() * 1000)) == {}
    assert _row(_project_now(ctx, descriptor), "pool-codex", "week")["remaining"]["amount"] == {
        "kind": "exact", "value": "15"}


class _FakeRegistry:
    def __init__(self, jobs):
        self._jobs = [dict(job) for job in jobs]

    def list_jobs(self):
        return [dict(job) for job in self._jobs]


def _admit(store, *, run_id: str, card_id: str, ordinal: int, profile_key: str, executor: str,
           model_id: str, mode: str = "shadow", generated_at_ms: int = _NOW):
    attempt_id = f"{run_id}:{card_id}:n{ordinal}"
    decision = quota_admission.AdmissionDecision(
        decision_id=quota_admission.decision_id_for(
            run_id=run_id, card_id=card_id, attempt_id=attempt_id, profile_key=profile_key, mode=mode,
        ),
        run_id=run_id, card_id=card_id, attempt_id=attempt_id, profile_key=profile_key,
        mode=mode, outcome="admit", policy_version=quota_admission.ADMISSION_POLICY_VERSION,
        observation_version="shadow-projection:fixture", demand_version=quota_admission.DEMAND_FIXTURE_VERSION,
        qualification_version="not-applicable", generated_at_ms=generated_at_ms,
        selected={"executor": executor, "model_id": model_id, "independence_domain": "openai"},
    )
    store.record(decision)
    return decision


def _terminal_job(job_id: str, *, run_id: str = "run-1", card: str = "subagent-build",
                  executor: str = "codex", model_id: str = "gpt-x", status: str = "exited",
                  input_tokens: int = 2, **extra) -> dict:
    return {
        "job_id": job_id, "workflow_run_id": run_id, "workflow_card": card, "status": status,
        "exit_code": 0, "executor": executor, "model_id": model_id,
        # 兩個有 mapping 的 metric 都帶值：有 mapping 卻缺值的 metric 依 #836
        # AC3 契約記成 unknown quantity（見下方 unusable-usage 測試）。
        "usage": {"input_tokens": input_tokens, "output_tokens": 0}, "started_at": _iso(_NOW + 1_000),
        "exited_at": _iso(_NOW + 2_000), **extra,
    }


def _harvest_fixture(tmp_path: Path):
    descriptor = _descriptor(windows=(("week", 604_800_000),))
    bindings = (
        _profile_binding(descriptor, _PROFILE_WORKER, binding_id="worker"),
        _profile_binding(descriptor, _PROFILE_REVIEWER, binding_id="reviewer"),
    )
    ctx = _ctx(tmp_path, descriptors=(descriptor,), bindings=bindings)
    return ctx, descriptor


def test_harvest_maps_shadow_receipt_by_attempt_ordinal_and_identity_only(tmp_path: Path) -> None:
    ctx, _ = _harvest_fixture(tmp_path)
    # run-1／card 的 job 依建立順序：n0（codex，對得上 receipt）、n1（claude，
    # receipt 的 selected 是 codex → 身分不符不記）、n2（沒有任何 receipt）、
    # n3（receipt 在，但 job 仍在跑）。
    _admit(ctx.store, run_id="run-1", card_id="subagent-build", ordinal=0,
           profile_key=_PROFILE_WORKER, executor="codex", model_id="gpt-x")
    _admit(ctx.store, run_id="run-1", card_id="subagent-build", ordinal=1,
           profile_key=_PROFILE_WORKER, executor="codex", model_id="gpt-x")
    _admit(ctx.store, run_id="run-1", card_id="subagent-build", ordinal=3,
           profile_key=_PROFILE_WORKER, executor="codex", model_id="gpt-x")
    registry = _FakeRegistry([
        _terminal_job("job-n0"),
        _terminal_job("job-n1", executor="claude", model_id="claude-x"),
        _terminal_job("job-n2"),
        _terminal_job("job-n3", status="dispatched"),
        # 另一個 run／card 的 job 不影響 run-1 的 ordinal。
        _terminal_job("job-other", run_id="run-2"),
    ])
    result = manager.harvest_quota_terminal_usage(
        registry=registry, quota_admission_context=ctx, now_ms=_NOW + 10_000,
    )
    assert [item["job_id"] for item in result["recorded"]] == ["job-n0"]
    assert result["skipped"] == {"no-admit-decision": 3}  # n1（身分不符）、n2、run-2
    rows = _usage_rows(tmp_path / "quota" / "events.jsonl")
    assert sorted(row["idempotency_key"].split(":")[3] for row in rows) == ["job-n0", "job-n0"]


def test_harvest_skips_ambiguous_shadow_receipts_instead_of_guessing(tmp_path: Path) -> None:
    """同一個 attempt ordinal 有兩筆 shadow admit receipt、executor／model 相同
    但 resolved profile key 不同（例如 create_job 前失敗後換了 effort 重試）：
    無法證明是哪一個 profile 的消耗，不記，不猜。"""
    ctx, _ = _harvest_fixture(tmp_path)
    _admit(ctx.store, run_id="run-1", card_id="subagent-build", ordinal=0,
           profile_key=_PROFILE_WORKER, executor="codex", model_id="gpt-x")
    _admit(ctx.store, run_id="run-1", card_id="subagent-build", ordinal=0,
           profile_key=_PROFILE_REVIEWER, executor="codex", model_id="gpt-x", generated_at_ms=_NOW + 5)
    result = manager.harvest_quota_terminal_usage(
        registry=_FakeRegistry([_terminal_job("job-n0")]), quota_admission_context=ctx, now_ms=_NOW + 10_000,
    )
    assert result["recorded"] == []
    assert result["skipped"] == {"ambiguous-admit-decision": 1}
    assert _usage_rows(tmp_path / "quota" / "events.jsonl") == []


def test_harvest_uses_exact_quota_decision_id_for_enforced_jobs(tmp_path: Path) -> None:
    ctx, _ = _harvest_fixture(tmp_path)
    decision = _admit(ctx.store, run_id="run-1", card_id="subagent-build", ordinal=0,
                      profile_key=_PROFILE_REVIEWER, executor="codex", model_id="gpt-x", mode="enforced")
    # 同一個 ordinal 另有一筆 shadow receipt（先 shadow 後切 enforce）：enforce
    # job 帶著 quota_decision_id，精確比對，不受 ordinal 歧義影響。
    _admit(ctx.store, run_id="run-1", card_id="subagent-build", ordinal=0,
           profile_key=_PROFILE_WORKER, executor="codex", model_id="gpt-x")
    registry = _FakeRegistry([_terminal_job("job-n0", quota_decision_id=decision.decision_id)])
    result = manager.harvest_quota_terminal_usage(
        registry=registry, quota_admission_context=ctx, now_ms=_NOW + 10_000,
    )
    assert [item["job_id"] for item in result["recorded"]] == ["job-n0"]
    rows = _usage_rows(tmp_path / "quota" / "events.jsonl")
    assert len(rows) == 2
    assert {row["observation"]["profile_ref"]["value"]["key"] for row in rows} == {_PROFILE_REVIEWER}


def test_harvest_after_partial_crash_writes_only_the_missing_rows(tmp_path: Path, monkeypatch) -> None:
    """記到一半 crash（第一個 metric 已落 ledger、第二個沒有）：下一輪 harvest
    只補缺的那一筆，已落地的那一筆不重寫、也不被當成『這個 job 已經記過』而
    整個略過。"""
    ctx, descriptor = _harvest_fixture(tmp_path)
    _admit(ctx.store, run_id="run-1", card_id="subagent-build", ordinal=0,
           profile_key=_PROFILE_WORKER, executor="codex", model_id="gpt-x")
    job = _terminal_job("job-n0")
    job["usage"] = {"input_tokens": 2, "output_tokens": 5}
    registry = _FakeRegistry([job])

    original_append = QuotaEventLedger.append_observation
    calls = {"n": 0}

    def _crash_after_first(self, observation, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated crash between metrics")
        return original_append(self, observation, **kwargs)

    monkeypatch.setattr(QuotaEventLedger, "append_observation", _crash_after_first)
    crashed = manager.harvest_quota_terminal_usage(
        registry=registry, quota_admission_context=ctx, now_ms=_NOW + 10_000,
    )
    assert [item["job_id"] for item in crashed["failed"]] == ["job-n0"]
    assert crashed["recorded"] == []
    assert len(_usage_rows(tmp_path / "quota" / "events.jsonl")) == 1
    monkeypatch.setattr(QuotaEventLedger, "append_observation", original_append)

    restarted = _ctx(tmp_path, descriptors=ctx.descriptors, bindings=ctx.bindings)
    healed = manager.harvest_quota_terminal_usage(
        registry=registry, quota_admission_context=restarted, now_ms=_NOW + 20_000,
    )
    assert [(item["job_id"], item["accepted"]) for item in healed["recorded"]] == [("job-n0", 1)]
    rows = _usage_rows(tmp_path / "quota" / "events.jsonl")
    assert sorted(row["observation"]["measurement"]["metric_id"] for row in rows) == [
        "input_tokens", "output_tokens"]


def test_harvest_is_a_no_op_without_quota_context(tmp_path: Path) -> None:
    assert manager.harvest_quota_terminal_usage(
        registry=_FakeRegistry([_terminal_job("job-n0")]), quota_admission_context=None,
    ) == {"wired": False}
    assert manager.harvest_quota_terminal_usage(
        registry=_FakeRegistry([_terminal_job("job-n0")]),
        quota_admission_context=quota_admission.QuotaConfigInvalid(reason="bad"),
    ) == {"wired": False}


@pytest.mark.parametrize("usage", [None, {"input_tokens": None, "output_tokens": 0}])
def test_harvest_reports_unusable_usage_without_writing(tmp_path: Path, usage) -> None:
    ctx, _ = _harvest_fixture(tmp_path)
    _admit(ctx.store, run_id="run-1", card_id="subagent-build", ordinal=0,
           profile_key=_PROFILE_WORKER, executor="codex", model_id="gpt-x")
    job = _terminal_job("job-n0")
    job["usage"] = usage
    result = manager.harvest_quota_terminal_usage(
        registry=_FakeRegistry([job]), quota_admission_context=ctx, now_ms=_NOW + 10_000,
    )
    if usage is None:
        assert result["recorded"] == []
        assert result["skipped"] == {"usage-unavailable": 1}
        assert _usage_rows(tmp_path / "quota" / "events.jsonl") == []
    else:
        # 值缺但 metric 有 mapping：照 #836 契約記成 unknown quantity（保守），
        # 不是跳過——那筆耗用確實發生過、只是金額不明。
        assert [item["job_id"] for item in result["recorded"]] == ["job-n0"]
        rows = {row["observation"]["measurement"]["metric_id"]: row
                for row in _usage_rows(tmp_path / "quota" / "events.jsonl")}
        assert rows["input_tokens"]["observation"]["measurement"]["quantity"] == {
            "state": "unknown", "reason": "missing-usage-value"}
        assert rows["output_tokens"]["observation"]["measurement"]["quantity"]["state"] == "observed"


def test_harvest_reports_missing_usage_unit_mapping_as_skip_reason(tmp_path: Path) -> None:
    """quota-pools.json 沒有 `usage_unit_refs`（或沒涵蓋 job 的 metric）時，
    收割不寫 ledger，但 tick summary 用機讀理由讓 operator 看得出缺的是設定，
    不是『沒有受管 job』。"""
    descriptor = _descriptor(windows=(("week", 604_800_000),))
    ctx = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "quota" / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "quota" / "decisions.jsonl"),
        shadow=QuotaShadowService(QuotaEventLedger(tmp_path / "quota" / "events.jsonl")),
        descriptors=(descriptor,), unit_catalog=(_unit(),),
        bindings=(_profile_binding(descriptor, _PROFILE_WORKER, binding_id="worker"),),
        environment={},
    )
    _admit(ctx.store, run_id="run-1", card_id="subagent-build", ordinal=0,
           profile_key=_PROFILE_WORKER, executor="codex", model_id="gpt-x")
    result = manager.harvest_quota_terminal_usage(
        registry=_FakeRegistry([_terminal_job("job-n0")]), quota_admission_context=ctx, now_ms=_NOW + 10_000,
    )
    assert result["recorded"] == []
    assert result["skipped"] == {"usage-unit-mapping-missing": 1}
    assert _usage_rows(tmp_path / "quota" / "events.jsonl") == []
