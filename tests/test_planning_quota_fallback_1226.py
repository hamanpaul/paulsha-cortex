from __future__ import annotations

from types import SimpleNamespace

from paulsha_cortex.coordinator import execution_adapters, manager, model_identities, planning


def _fixture():
    primary = model_identities.ModelIdentity("claude", "p", "anthropic", ("planning",))
    secondary = model_identities.ModelIdentity("agy", "s", "google", ("planning",))
    alternate = model_identities.ModelIdentity("codex", "a", "openai", ("planning",))
    identities = model_identities.IdentityRegistry(
        schema_version=4, identities=(primary, secondary, alternate)
    )
    probes = {
        (item.executor, item.model_id): model_identities.CapabilityProbe.ready_for(
            item.executor, item.model_id, item.independence_domain
        )
        for item in identities.identities
    }
    return identities, probes, primary, secondary, alternate


def test_secondary_admissible_skips_quota_infeasible_identity():
    identities, probes, _primary, _secondary, alternate = _fixture()
    selected = planning.select_secondary_planner(
        registry=identities,
        primary=("claude", "p"),
        probes=probes,
        admissible=lambda item: item is alternate,
    )
    assert selected.state == "ready"
    assert selected.identity == alternate


def test_secondary_admissible_waits_when_no_candidate_is_feasible():
    identities, probes, *_ = _fixture()
    selected = planning.select_secondary_planner(
        registry=identities,
        primary=("claude", "p"),
        probes=probes,
        admissible=lambda _item: False,
    )
    assert selected.state == "needs_human"
    assert any(
        rejection.reason == "quota-admission-insufficient"
        for rejection in selected.rejections
    )


def test_fallback_primary_can_pair_with_feasible_different_domain_secondary():
    identities, probes, _primary, secondary, alternate = _fixture()
    selection = planning.select_secondary_planner(
        registry=identities,
        primary=(alternate.executor, alternate.model_id),
        probes=probes,
        admissible=lambda item: item is secondary,
    )
    assert selection.state == "ready"
    assert selection.identity == secondary
    assert selection.identity.independence_domain != alternate.independence_domain


def test_fallback_waits_when_only_feasible_secondary_shares_primary_domain():
    secondary = model_identities.ModelIdentity("agy", "s", "google", ("planning",))
    fallback = model_identities.ModelIdentity("codex", "a", "google", ("planning",))
    identities = model_identities.IdentityRegistry(
        schema_version=4, identities=(fallback, secondary)
    )
    probes = {
        (item.executor, item.model_id): model_identities.CapabilityProbe.ready_for(
            item.executor, item.model_id, item.independence_domain
        )
        for item in identities.identities
    }
    selection = planning.select_secondary_planner(
        registry=identities,
        primary=(fallback.executor, fallback.model_id),
        probes=probes,
        admissible=lambda item: item is secondary,
    )
    assert selection.state == "needs_human"
    assert selection.reason == "no-heterogeneous-planner"


def test_secondary_default_selection_remains_unchanged():
    identities, probes, _primary, secondary, _alternate = _fixture()
    selected = planning.select_secondary_planner(
        registry=identities, primary=("claude", "p"), probes=probes
    )
    assert selected.state == "ready"
    assert selected.identity == secondary


def test_gate_preflight_exclusion_is_attached_to_selected_admission(monkeypatch):
    identities, _probes, primary, _secondary, alternate = _fixture()

    class Store:
        def __init__(self):
            self.rows = {}

        def get(self, decision_id):
            return self.rows.get(decision_id)

        def record(self, decision):
            self.rows[decision.decision_id] = decision

    class Registry:
        def __init__(self):
            self.run = SimpleNamespace(run_id="run", quota_admission={})

        def list_jobs(self):
            return []

        def _manager_update_workflow_run(self, _run_id, **kwargs):
            for key, value in kwargs.items():
                setattr(self.run, key, value)
            return self.run

    store = Store()
    registry = Registry()
    context = SimpleNamespace(
        store=store,
        authority=SimpleNamespace(),
        shadow=object(),
        bindings=(),
        descriptors=(),
        unit_catalog=(),
        usage_unit_refs={},
        lease_ms=1000,
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
        config_revision="cfg",
    )
    monkeypatch.setattr(
        execution_adapters, "resolve_profile",
        lambda *_args, **_kwargs: SimpleNamespace(resolved_key="profile"),
    )
    monkeypatch.setattr(
        manager, "_evaluate_quota_admission_candidate",
        lambda _ctx, *, identity, **_kwargs: (
            SimpleNamespace(
                feasible=identity == alternate,
                exclusion_reason="quota-admission-insufficient",
                pools=(), observation_state="known", binding_kind="exact",
            ),
            "demand",
        ),
    )
    from paulsha_cortex.coordinator import quota_admission

    monkeypatch.setattr(quota_admission, "observation_fingerprint", lambda _value: "obs")
    run = registry.run
    gate = manager._PlanningQuotaGate(
        registry=registry, run=run, identities=identities, quota_context=context
    )
    assert gate.feasible(primary) is False
    assert gate.feasible(alternate) is True
    gate.acquire(SimpleNamespace(purpose="questioner", identity=alternate))

    receipt = next(iter(store.rows.values()))
    assert receipt.selected == {
        "executor": alternate.executor,
        "model_id": alternate.model_id,
        "independence_domain": alternate.independence_domain,
    }
    assert [(item["executor"], item["model_id"], item["exclusion_reason"])
            for item in receipt.excluded] == [
        (primary.executor, primary.model_id, "quota-admission-insufficient")
    ]


# ---------------------------------------------------------------------------
# apply_workflow_action 整合：fallback 真的改到 brainstorm 的 primary；整組不可行
# 時 run 轉成 `quota-admission-insufficient`，不讓 PlanningQuotaWait 往外漏。
# ---------------------------------------------------------------------------

import json as _json
from types import SimpleNamespace as _NS

from paulsha_cortex.coordinator import manager as _manager
from paulsha_cortex.coordinator import planning_runtime as _planning_runtime
from paulsha_cortex.coordinator import quota_admission as _quota_admission
from paulsha_cortex.coordinator.model_identities import (
    CapabilityProbe as _Probe,
    IdentityRegistry as _IdentityRegistry,
)
from paulsha_cortex.coordinator.planning import BrainstormResult as _BrainstormResult
from paulsha_cortex.coordinator.planning import PlanningGateRefs as _GateRefs
from paulsha_cortex.coordinator.registry import JobRegistry as _JobRegistry
from test_workflow_production_wiring import _manifest, _workflow_args

_ROWS = (
    ("codex", "gpt-primary", "openai"),
    ("claude", "sonnet", "anthropic"),
    ("agy", "gemini", "google"),
)


def _start_with_quota(tmp_path, monkeypatch, *, infeasible):
    registry = _JobRegistry(state_path=tmp_path / "registry.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(_json.dumps(_manifest().to_dict()), encoding="utf-8")
    args = _workflow_args(manifest_path, tmp_path)
    identities = _IdentityRegistry.from_rows([
        {
            "executor": e, "model_id": m, "independence_domain": d, "capabilities": ["planning"],
            **({"live_probe": "agy-plan-sandbox"} if e == "agy" else {}),
        }
        for e, m, d in _ROWS
    ])
    probes = {(e, m): _Probe.ready_for(e, m, d) for e, m, d in _ROWS}
    factory_primaries = []

    def factory(*, primary, worktree, evidence_root, run_id, quota_gate=None):
        factory_primaries.append(primary)
        return _planning_runtime.ProductionPlanningRuntime(
            identities, probes, lambda report: {}, lambda pack, identity: {},
            lambda pack, evidence: {},
        )

    brainstorm_primaries = []

    def fake_brainstorm(**kwargs):
        brainstorm_primaries.append(kwargs["primary"])
        return _BrainstormResult(
            state="needs_human", reason="captured-for-test", secondary_domain=None,
            gate_refs=_GateRefs(),
        )

    monkeypatch.setattr(_manager, "run_heterogeneous_brainstorm", fake_brainstorm)

    def evaluate(ctx, *, identity, profile_binding, now_ms):
        feasible = identity.executor not in infeasible
        return (_NS(
            feasible=feasible,
            exclusion_reason=None if feasible else "quota-admission-insufficient",
            pools=(),
        ), "demand-fixture")

    monkeypatch.setattr(_manager, "_evaluate_quota_admission_candidate", evaluate)
    context = _NS(
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"},
        store=_quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        config_revision="rev-1226",
    )
    result = _manager.apply_workflow_action(
        registry, args=args, runtime_factory=factory, coordinator_root=tmp_path,
        quota_admission_context=context,
    )
    return registry, result, factory_primaries, brainstorm_primaries


def test_apply_workflow_action_falls_back_to_feasible_primary(tmp_path, monkeypatch):
    _registry, _result, factory_primaries, brainstorm_primaries = _start_with_quota(
        tmp_path, monkeypatch, infeasible={"codex"},
    )

    assert brainstorm_primaries == [("claude", "sonnet")]
    assert factory_primaries[-1] == ("claude", "sonnet")


def test_apply_workflow_action_waits_when_no_heterogeneous_pair_is_feasible(
    tmp_path, monkeypatch,
):
    registry, result, _factory_primaries, brainstorm_primaries = _start_with_quota(
        tmp_path, monkeypatch, infeasible={"codex", "claude"},
    )

    assert brainstorm_primaries == []
    assert result["reason"] == "quota-admission-insufficient"
    run = registry.get_workflow_run(result["run_id"])
    assert "needs_human" in run.facets
    assert run.needs_human_reason["reason"] == "quota-admission-insufficient"
