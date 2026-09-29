from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from test_coordinator_manager import (
    _HeadlessDispatcher,
    _RecordingLauncher,
    _dispatch_meta,
)
from test_quota_admission_839 import _PROFILE_A, _binding, _observation, _pool_descriptor
from paulsha_cortex.coordinator import autonomy, manager, quota_admission
from paulsha_cortex.coordinator.quota_ledger import QuotaEventLedger
from paulsha_cortex.coordinator.quota_reservation import QuotaReservationAuthority
from paulsha_cortex.coordinator.quota_shadow import QuotaShadowService
from paulsha_cortex.coordinator.registry import JobRegistry


class IdentityRegistry:
    def __init__(self):
        self.identity = SimpleNamespace(
            executor="copilot", model_id="model-1", independence_domain="copilot"
        )
        self.identities = (self.identity,)

    def get(self, executor, model_id):
        return self.identity if (executor, model_id) == ("copilot", "model-1") else None


class QuotaLauncher(_RecordingLauncher):
    executor = "copilot"
    model = "model-1"

    def __init__(self, *, fail=False):
        super().__init__()
        self.fail = fail

    def launch(self, **kwargs):
        if self.fail:
            raise RuntimeError("spawn failed")
        return super().launch(**kwargs)


def _context(tmp_path: Path, *, enforce: bool, remaining: str = "10"):
    descriptor = _pool_descriptor(windows=(("short", 300_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    shadow = QuotaShadowService.in_memory()
    observation = _observation(
        descriptor, "short", value=remaining, observed_at_ms=int(__import__("time").time() * 1000),
        profile_key=_PROFILE_A,
    )
    shadow.record_observation(observation.to_dict(), descriptors=(descriptor,), unit_catalog=())
    context = quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(tmp_path / "reservations.jsonl"),
        store=quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl"),
        shadow=shadow,
        descriptors=(descriptor,),
        unit_catalog=(),
        bindings=(binding,),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on" if enforce else "off"},
        usage_unit_refs={"input_tokens": ("token", "1")},
    )
    return context, descriptor, binding


def _run_dispatch(tmp_path, context, *, launcher=None, limiter=None):
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    dispatcher = _HeadlessDispatcher(registry)
    active_launcher = launcher or QuotaLauncher()
    meta = _dispatch_meta("slice-1153")
    meta.update({"executor": "copilot", "model_id": "model-1"})
    with pytest.MonkeyPatch.context() as mp:
        def bind_profile(target, _identity, _persona, **_kwargs):
            target._execution_profile_binding = SimpleNamespace(resolved_key=_PROFILE_A)
            return target

        mp.setattr(autonomy, "_bind_dispatch_execution_profile", bind_profile)
        waits = []
        result = autonomy.dispatch_ready(
            [meta], lambda _dep: True, dispatcher, launcher=active_launcher,
            git_runner=lambda args: (
                "e" * 40 if any("refs/remotes/origin/main" in str(arg) for arg in args)
                else "f" * 40 if "rev-parse" in args else ""
            ),
            identity_registry=IdentityRegistry(), launcher_factory=lambda _identity: active_launcher,
            spawn_admission=limiter, quota_admission_context=context, quota_waits=waits,
        )
    return result, waits, registry, active_launcher


def test_shadow_fanout_receipt_keeps_dispatch_fields_unchanged(tmp_path):
    context, _descriptor, _binding = _context(tmp_path / "shadow", enforce=False, remaining="0")
    jobs, waits, registry, launcher = _run_dispatch(tmp_path / "shadow-run", context)
    baseline_jobs, _baseline_waits, _baseline_registry, _baseline_launcher = _run_dispatch(
        tmp_path / "shadow-baseline", None
    )

    assert len(jobs) == 1
    assert len(baseline_jobs) == 1
    assert waits == []
    assert len(launcher.calls) == 1
    assert registry.list_jobs()[0]["quota_decision_id"] == context.store.all_rows()[0]["decision_id"]
    assert context.store.all_rows()[0]["mode"] == "shadow"
    assert context.store.all_rows()[0]["outcome"] == "admit"
    assert context.authority.list_by_state("reserved", now_ms=1_800_000_000_100) == ()
    behavioral_fields = ("task", "persona", "kind", "branch", "status", "worktree", "dispatch_head")
    assert {key: jobs[0].get(key) for key in behavioral_fields} == {
        key: baseline_jobs[0].get(key) for key in behavioral_fields
    }


def test_enforced_insufficient_fanout_returns_wait_without_job_or_spawn(tmp_path):
    context, _descriptor, _binding = _context(tmp_path / "enforce", enforce=True, remaining="0")
    jobs, waits, registry, launcher = _run_dispatch(tmp_path / "enforce-run", context)

    assert jobs == []
    assert len(launcher.calls) == 0
    assert registry.list_jobs() == []
    assert waits[0]["reason"] == "quota-admission-insufficient"
    assert waits[0]["retry_eligible"] is True
    assert context.store.all_rows()[0]["outcome"] == "wait"
    assert context.authority.list_by_state("reserved", now_ms=1_800_000_000_100) == ()


def test_enforced_sufficient_fanout_binds_reservation_to_job(tmp_path):
    context, _descriptor, _binding = _context(tmp_path / "bound", enforce=True)
    jobs, waits, registry, launcher = _run_dispatch(tmp_path / "bound-run", context)

    assert len(jobs) == 1
    assert waits == []
    assert len(launcher.calls) == 1
    decision = context.store.all_rows()[0]
    assert decision["outcome"] == "admit"
    assert decision["reservation_id"]
    bound = context.authority.list_by_state("bound", now_ms=1_800_000_000_100)
    assert len(bound) == 1
    assert bound[0].job_id == registry.list_jobs()[0]["job_id"]


def test_limiter_rejection_happens_before_quota_reservation(tmp_path):
    context, _descriptor, _binding = _context(tmp_path / "limiter", enforce=True)

    class RejectingLimiter:
        def admit(self, _provider):
            raise RuntimeError("limiter rejected")

    with pytest.raises(autonomy.DispatchReadyError):
        _run_dispatch(tmp_path / "limiter-run", context, limiter=RejectingLimiter())
    assert context.authority.list_by_state("reserved", now_ms=1_800_000_000_100) == ()
    assert context.store.all_rows() == []


def test_spawn_failure_settles_bound_fanout_reservation(tmp_path):
    context, _descriptor, _binding = _context(tmp_path / "spawn", enforce=True)
    with pytest.raises(autonomy.DispatchReadyError):
        _run_dispatch(tmp_path / "spawn-run", context, launcher=QuotaLauncher(fail=True))

    rows = context.authority.list_by_state("settled", now_ms=1_800_000_000_100)
    assert len(rows) == 1
    assert rows[0].state == "settled"


def test_same_fanout_attempt_replay_does_not_reserve_twice(tmp_path):
    context, descriptor, _binding = _context(tmp_path / "replay", enforce=True)
    identity = IdentityRegistry().identity
    launcher = SimpleNamespace(_execution_profile_binding=SimpleNamespace(resolved_key=_PROFILE_A))
    dispatcher = SimpleNamespace(_registry=SimpleNamespace(list_jobs=lambda: []))
    waits = []

    first = autonomy._fanout_quota_admission(
        context=context, dispatcher=dispatcher, slice_id="slice-replay", persona="builder",
        identity=identity, launcher=launcher, quota_waits=waits,
    )
    second = autonomy._fanout_quota_admission(
        context=context, dispatcher=dispatcher, slice_id="slice-replay", persona="builder",
        identity=identity, launcher=launcher, quota_waits=waits,
    )

    assert first[0] is not None
    assert second[0] is None
    assert waits[0]["reason"] == "quota-admission-attempt-held-elsewhere"
    reserved = context.authority.list_by_state("reserved", now_ms=1_800_000_000_100)
    assert len(reserved) == 1


def test_fresh_quota_observation_resumes_wait_on_new_attempt_generation(tmp_path):
    context, descriptor, _binding = _context(tmp_path / "resume", enforce=True, remaining="0")
    identity = IdentityRegistry().identity
    launcher = SimpleNamespace(_execution_profile_binding=SimpleNamespace(resolved_key=_PROFILE_A))
    dispatcher = SimpleNamespace(_registry=SimpleNamespace(list_jobs=lambda: []))
    waits = []
    denied = autonomy._fanout_quota_admission(
        context=context, dispatcher=dispatcher, slice_id="slice-resume", persona="builder",
        identity=identity, launcher=launcher, quota_waits=waits,
    )
    now_ms = int(__import__("time").time() * 1000)
    refreshed = _observation(
        descriptor, "short", value="10", observed_at_ms=now_ms, profile_key=_PROFILE_A
    )
    context.shadow.record_observation(refreshed.to_dict(), descriptors=(descriptor,), unit_catalog=())

    resumed = autonomy._fanout_quota_admission(
        context=context, dispatcher=dispatcher, slice_id="slice-resume", persona="builder",
        identity=identity, launcher=launcher, quota_waits=waits,
    )

    assert denied[2].outcome == "wait"
    assert resumed[0] is not None
    assert resumed[2].outcome == "admit"
    assert resumed[2].attempt_id.endswith(":g1")


def test_terminal_usage_harvest_pairs_fanout_job_by_decision_id(tmp_path):
    context, _descriptor, _binding = _context(tmp_path, enforce=False)
    run_id, card_id, attempt_id = "fanout:slice-harvest", "slice-harvest", "fanout:slice-harvest:slice-harvest:n0"
    decision_id = quota_admission.decision_id_for(
        run_id=run_id, card_id=card_id, attempt_id=attempt_id,
        profile_key=_PROFILE_A, mode="shadow",
    )
    context.store.record(quota_admission.AdmissionDecision(
        decision_id=decision_id, run_id=run_id, card_id=card_id, attempt_id=attempt_id,
        profile_key=_PROFILE_A, mode="shadow", outcome="admit",
        policy_version=quota_admission.ADMISSION_POLICY_VERSION,
        observation_version="observation-v1", demand_version="demand-v1",
        qualification_version="not-applicable", generated_at_ms=1_800_000_000_000,
        selected={"executor": "copilot", "model_id": "model-1"},
    ))
    job = {
        "job_id": "fanout-job-1", "task": card_id, "status": "exited",
        "quota_decision_id": decision_id, "executor": "copilot", "model_id": "model-1",
        "usage": {"input_tokens": 5}, "started_at": "2027-01-15T00:00:00Z",
        "exited_at": "2027-01-15T00:00:01Z",
    }
    registry = SimpleNamespace(list_jobs=lambda: [job])

    result = manager.harvest_quota_terminal_usage(
        registry=registry, quota_admission_context=context, now_ms=1_800_000_000_100
    )

    assert result["recorded"][0]["job_id"] == "fanout-job-1"
    events = context.shadow.ledger.read().events
    assert any(event.get("kind") == "observation" and "terminal-usage" in event.get("idempotency_key", "") for event in events)


def test_reserve_race_loss_after_feasible_wait_still_records_an_admit_receipt(
    tmp_path, monkeypatch
):
    """#1153 審查：tick1 額度不足（wait@g0）；tick2 觀測可行但 reserve 被 workflow
    搶先（wait@g1）；tick3 容量釋出時必須跳過所有已有 wait receipt 的世代，讓 job
    指向 admit receipt，harvest 才配得到終局用量。"""

    context, descriptor, _binding = _context(tmp_path / "race", enforce=True, remaining="0")
    identity = IdentityRegistry().identity
    launcher = SimpleNamespace(_execution_profile_binding=SimpleNamespace(resolved_key=_PROFILE_A))
    dispatcher = SimpleNamespace(_registry=SimpleNamespace(list_jobs=lambda: []))
    waits = []

    def admit():
        return autonomy._fanout_quota_admission(
            context=context, dispatcher=dispatcher, slice_id="slice-race", persona="builder",
            identity=identity, launcher=launcher, quota_waits=waits,
        )

    first = admit()
    assert first[2].outcome == "wait"
    refreshed = _observation(
        descriptor, "short", value="10",
        observed_at_ms=int(__import__("time").time() * 1000), profile_key=_PROFILE_A,
    )
    context.shadow.record_observation(refreshed.to_dict(), descriptors=(descriptor,), unit_catalog=())

    real_reserve = quota_admission.reserve_for_candidate_with_generation_fallback

    def lose_race(authority, **kwargs):
        attempt_id = kwargs["base_attempt_id"]
        decision_id = quota_admission.decision_id_for(
            run_id=kwargs["run_id"], card_id=kwargs["card_id"], attempt_id=attempt_id,
            profile_key=kwargs["profile_key"], mode="enforced",
        )
        return attempt_id, decision_id, SimpleNamespace(status="denied", reason="held by workflow")

    monkeypatch.setattr(quota_admission, "reserve_for_candidate_with_generation_fallback", lose_race)
    second = admit()
    assert second[0] is None
    monkeypatch.setattr(quota_admission, "reserve_for_candidate_with_generation_fallback", real_reserve)

    third = admit()

    assert third[0] is not None
    assert third[2].outcome == "admit"
    stored = context.store.get(third[2].decision_id)
    assert stored is not None and stored.outcome == "admit"
    wait_ids = {row["decision_id"] for row in context.store.all_rows() if row["outcome"] == "wait"}
    assert third[2].decision_id not in wait_ids
