from __future__ import annotations

from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import (
    execution_adapters, manager, planning, planning_runtime, quota_admission,
)
from paulsha_cortex.coordinator.quota_reservation import QuotaReservationAuthority


class _Store:
    def __init__(self):
        self.rows = {}

    def get(self, decision_id):
        return self.rows.get(decision_id)

    def record(self, decision):
        self.rows[decision.decision_id] = decision


class _Registry:
    def __init__(self, run):
        self.run = run
        self.jobs = []

    def list_jobs(self):
        return self.jobs

    def get_workflow_run(self, _run_id):
        return self.run

    def _manager_update_workflow_run(self, _run_id, **kwargs):
        for key, value in kwargs.items():
            setattr(self.run, key, value)
        return self.run


class _Invoker:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def run(self, _invocation):
        self.calls += 1
        return self.outcome

    def capability_probe_runner(self):
        return "probe-runner"


class _Gate:
    def __init__(self):
        self.events = []

    def acquire(self, invocation):
        self.events.append(("acquire", invocation.purpose))
        return "lease"

    def settle(self, lease, invocation, outcome):
        self.events.append(("settle", lease, invocation.purpose, outcome))


def _invocation(purpose="questioner"):
    return planning_runtime.PlanningInvocation(
        identity=SimpleNamespace(executor="claude", model_id="sonnet", independence_domain="a"),
        prompt="prompt", purpose=purpose, timeout_seconds=1, worktree=__import__("pathlib").Path("."),
        evidence_root=None, run_id="run-1", execution_profile=object(),
    )


def _assessment(*, feasible=True):
    return SimpleNamespace(
        feasible=feasible, exclusion_reason=None if feasible else "insufficient-quota",
        pools=(), observation_state="known", binding_kind="exact",
    )


def _context(*, enforce=False):
    store = _Store()
    context = SimpleNamespace(
        store=store, authority=SimpleNamespace(), shadow=object(), bindings=(),
        descriptors=(), unit_catalog=(), usage_unit_refs={}, lease_ms=1000,
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on" if enforce else "off"},
        config_revision="cfg-1",
    )
    return context, store


def _run():
    return SimpleNamespace(
        run_id="run-1", work_id="work-1", sizing_band=None, quota_admission={}, facets=(),
        current_phase="define", status="ongoing", steps=(),
    )


def _patch_assessment(monkeypatch, assessment):
    monkeypatch.setattr(
        execution_adapters, "resolve_profile",
        lambda *_a, **_kw: SimpleNamespace(resolved_key="profile-1"),
    )
    monkeypatch.setattr(manager, "_evaluate_quota_admission_candidate", lambda *_a, **_kw: (assessment, "demand-1"))
    monkeypatch.setattr(quota_admission, "observation_fingerprint", lambda _assessment: "obs-1")


def test_shadow_records_admit_and_wrapper_preserves_outcome(monkeypatch):
    context, store = _context()
    run = _run()
    registry = _Registry(run)
    _patch_assessment(monkeypatch, _assessment(feasible=False))
    gate = manager._PlanningQuotaGate(
        registry=registry, run=run, identities=SimpleNamespace(qualification_policy="disabled"),
        quota_context=context,
    )
    outcome = planning_runtime.PlanningOutcome(returncode=0, stdout='{}')
    inner = _Invoker(outcome)
    wrapped = planning_runtime.QuotaGatedPlanningInvoker(inner, gate)

    assert wrapped.run(_invocation()) is outcome
    assert inner.calls == 1
    receipt = next(iter(store.rows.values()))
    assert receipt.mode == "shadow"
    assert receipt.outcome == "admit"
    assert receipt.selected_feasible is False


def test_enforce_insufficient_blocks_inner_and_projects_retry_resume(monkeypatch):
    context, store = _context(enforce=True)
    run = _run()
    registry = _Registry(run)
    _patch_assessment(monkeypatch, _assessment(feasible=False))
    gate = manager._PlanningQuotaGate(
        registry=registry, run=run, identities=SimpleNamespace(qualification_policy="disabled"),
        quota_context=context,
    )
    inner = _Invoker(planning_runtime.PlanningOutcome(returncode=0, stdout="{}"))
    wrapped = planning_runtime.QuotaGatedPlanningInvoker(inner, gate)

    with pytest.raises(planning_runtime.PlanningQuotaWait) as blocked:
        wrapped.run(_invocation())
    assert inner.calls == 0
    receipt = next(iter(store.rows.values()))
    assert receipt.outcome == "wait"
    assert receipt.retry_eligible is True
    manager._planning_quota_wait_stop(
        registry=registry, run=run, excluded=blocked.value.excluded, card_id="define-card",
    )
    assert "needs_human" in run.facets
    assert run.needs_human_reason.reason == "quota-admission-insufficient"
    run.needs_human_reason = {"reason": "quota-admission-insufficient"}
    run.facets = ("needs_human",)
    run.steps = (SimpleNamespace(phase="define", card="define-card", persona="planner", gate_result=None),)
    from paulsha_cortex.coordinator.work_actions import _phase_recovery_actions

    assert "resume" in _phase_recovery_actions(
        run, registry, quota_decision_store=store,
    )
    monkeypatch.setattr(
        manager, "_evaluate_quota_admission_candidate",
        lambda *_a, **_kw: (_assessment(feasible=True), "demand-1"),
    )
    assert wrapped.run(_invocation()) == planning_runtime.PlanningOutcome(returncode=0, stdout="{}")
    assert inner.calls == 1
    assert {row.outcome for row in store.rows.values()} == {"wait", "admit"}


def test_enforce_reservation_is_bound_settled_and_usage_is_harvested(monkeypatch, tmp_path):
    context, store = _context(enforce=True)
    run = _run()
    registry = _Registry(run)
    pool_ref = {
        "authority_id": "fixture-authority", "account_id": "fixture-account",
        "pool_id": "fixture-pool", "revision": "1",
    }
    assessment = quota_admission.CandidateAssessment(
        executor="claude", model_id="sonnet", independence_domain="a",
        profile_key="profile-1", feasible=True, exclusion_reason=None,
        pools=(quota_admission.PoolAssessment(
            pool_ref=pool_ref, window_id="daily",
            remaining={"amount": {"kind": "exact", "value": "5"}},
            demand="1", assessment="sufficient", coverage_gaps=(),
        ),),
        observation_state="known", coverage_gaps=(), binding_kind="exact",
    )
    _patch_assessment(monkeypatch, assessment)
    context.authority = QuotaReservationAuthority(tmp_path / "reservations.jsonl")
    usage_rows = []
    monkeypatch.setattr(manager, "_quota_admission_record_terminal_usage", lambda *_a, **kw: usage_rows.append(kw))
    gate = manager._PlanningQuotaGate(
        registry=registry, run=run, identities=SimpleNamespace(qualification_policy="disabled"),
        quota_context=context,
    )
    invocation = _invocation()
    lease = gate.acquire(invocation)
    assert len(context.authority.list_by_state("bound", now_ms=10**15)) == 1
    gate.settle(lease, invocation, planning_runtime.PlanningOutcome(
        returncode=0,
        stdout='{"type":"result","usage":{"input_tokens":12,"output_tokens":8,"cache_read_input_tokens":2}}\n',
    ))

    terminal = context.authority.list_by_state("settled", now_ms=10**15)
    assert len(terminal) == 1
    assert terminal[0].job_id.startswith("planning:")
    assert usage_rows[0]["job"]["usage"]["input_tokens"] == 12
    assert usage_rows[0]["job"]["started_at"]
    assert usage_rows[0]["job"]["exited_at"]
    assert next(iter(store.rows.values())).reservation_id == terminal[0].reservation_id


def test_probe_bypasses_quota_gate():
    gate = _Gate()
    outcome = planning_runtime.PlanningOutcome(returncode=0, stdout="{}")
    inner = _Invoker(outcome)
    wrapped = planning_runtime.QuotaGatedPlanningInvoker(inner, gate)

    assert wrapped.capability_probe_runner() == "probe-runner"
    assert wrapped.run(_invocation("probe")) is outcome
    assert inner.calls == 1
    assert gate.events == []


def test_planning_pipeline_preserves_quota_wait(monkeypatch):
    identity = SimpleNamespace(independence_domain="secondary")
    monkeypatch.setattr(
        planning, "select_secondary_planner",
        lambda **_kwargs: SimpleNamespace(state="ready", identity=identity, reason=None, rejections=()),
    )
    report = SimpleNamespace(
        complete=False,
        to_dict=lambda: {},
        default_question_pack=SimpleNamespace(to_dict=lambda: {}),
    )

    def wait(_payload):
        raise planning_runtime.PlanningQuotaWait(excluded=({"model_id": "sonnet"},))

    with pytest.raises(planning_runtime.PlanningQuotaWait):
        planning.run_heterogeneous_brainstorm(
            report=report, primary=("claude", "sonnet"), registry=object(), probes={},
            evidence_dir=".", artifact_root=".", scope=object(),
            primary_questioner=wait, secondary_planner=lambda *_args: {},
            primary_integrator=lambda *_args: {},
        )


def test_production_runtime_wraps_configured_invoker_with_gate(monkeypatch, tmp_path):
    identity = SimpleNamespace(executor="claude", model_id="sonnet", independence_domain="a")
    registry = SimpleNamespace(identities=(), get=lambda *_args: identity)

    class Cache:
        def flush(self, *, keep):
            assert keep == []

    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: registry)
    monkeypatch.setattr(planning_runtime.planning_probe_cache.ProbeCache, "open", lambda *_a, **_kw: Cache())
    monkeypatch.setattr(planning_runtime.planning_probe_cache, "roster_digest", lambda _registry: "roster")
    monkeypatch.setattr(
        execution_adapters, "resolve_profile",
        lambda *_a, **_kw: SimpleNamespace(resolved_key="profile-1"),
    )
    gate = _Gate()
    runtime = planning_runtime.build_production_planning_runtime(
        primary=("claude", "sonnet"), worktree=tmp_path,
        invoker=_Invoker(planning_runtime.PlanningOutcome(returncode=0, stdout="{}")),
        quota_gate=gate, probe_cache_path=tmp_path / "probe-cache.json",
    )

    assert runtime.primary_questioner({}) == {}
    assert [event[0] for event in gate.events] == ["acquire", "settle"]


def test_synthetic_planning_job_is_reconcilable_after_manager_restart(monkeypatch):
    class MissingRegistryJob:
        def get_job(self, _job_id):
            raise KeyError(_job_id)

    monkeypatch.setattr(manager, "_planning_process_start_ticks", lambda _pid: "777")
    active = manager._quota_admission_job_lookup(
        MissingRegistryJob(), "planning:123:777:attempt-digest",
    )
    exited = manager._quota_admission_job_lookup(
        MissingRegistryJob(), "planning:123:778:attempt-digest",
    )

    assert active["status"] == "dispatched"
    assert manager._quota_admission_job_terminal_outcome(active) is None
    assert exited["status"] == "failed"
    assert manager._quota_admission_job_terminal_outcome(exited) == "failed"
