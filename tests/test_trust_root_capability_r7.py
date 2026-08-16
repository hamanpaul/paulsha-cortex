"""R7（trust-root Phase 1）：capability 通道 + 降級運轉開關。

驗收：
- 敏感 action 在無 capability 時 100% 被拒（單元測試）。
- 降級運轉開關行為（逐案核可放行、無核可拒絕、完全停用一律拒絕）。
- capability action-bound／single-use／短效／不可經 durable state 重放。
"""
from __future__ import annotations

import pytest

from paulsha_cortex.trust_root import capability
from paulsha_cortex.trust_root.capability import (
    Capability,
    CapabilityBinding,
    CapabilityBroker,
    DEGRADED_MODE_ENV,
    DegradedMode,
    DegradedOperationGate,
    SensitiveAction,
    build_gate,
    find_capability_material,
    resolve_degraded_mode,
)


def _binding(action: SensitiveAction, work_id: str = "W1", run_id: str = "R1") -> CapabilityBinding:
    return CapabilityBinding(
        action=action,
        work_id=work_id,
        run_id=run_id,
        subject_hash="sha256:deadbeef",
        authority_revision="auth-rev-1",
    )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- 無 capability 一律被拒（100%）-----------------------------------------

@pytest.mark.parametrize("action", list(SensitiveAction))
def test_no_capability_denied_for_every_action(action: SensitiveAction) -> None:
    gate = DegradedOperationGate(
        mode=DegradedMode.PER_CASE_APPROVAL, broker=CapabilityBroker()
    )
    decision = gate.authorize(_binding(action), capability=None)
    assert decision.allowed is False
    assert decision.reason == "no-capability"
    assert decision.action is action


@pytest.mark.parametrize("action", list(SensitiveAction))
def test_disabled_mode_denies_even_with_valid_capability(action: SensitiveAction) -> None:
    broker = CapabilityBroker()
    binding = _binding(action)
    cap = broker.grant(binding)
    gate = DegradedOperationGate(mode=DegradedMode.DISABLED, broker=broker)
    decision = gate.authorize(binding, capability=cap)
    assert decision.allowed is False
    assert decision.reason == "degraded-operation-disabled"
    # 完全停用不得消費 capability（未放行）。
    assert broker.consumed_count() == 0


# --- 逐案核可放行 -----------------------------------------------------------

def test_per_case_approval_allows_with_valid_capability() -> None:
    clock = _FakeClock()
    broker = CapabilityBroker(clock=clock)
    binding = _binding(SensitiveAction.SHIP)
    cap = broker.grant(binding)
    gate = DegradedOperationGate(mode=DegradedMode.PER_CASE_APPROVAL, broker=broker)
    decision = gate.authorize(binding, capability=cap)
    assert decision.allowed is True
    assert decision.reason == "capability-consumed"
    assert decision.mode is DegradedMode.PER_CASE_APPROVAL


# --- single-use：重放被拒 ---------------------------------------------------

def test_single_use_replay_denied() -> None:
    broker = CapabilityBroker()
    binding = _binding(SensitiveAction.OUTBOX_MUTATION)
    cap = broker.grant(binding)
    gate = DegradedOperationGate(mode=DegradedMode.PER_CASE_APPROVAL, broker=broker)

    first = gate.authorize(binding, capability=cap)
    assert first.allowed is True

    replay = gate.authorize(binding, capability=cap)
    assert replay.allowed is False
    assert replay.reason == "nonce-already-consumed"


# --- action-bound：跨 action 轉用被拒 --------------------------------------

def test_action_bound_cross_action_denied() -> None:
    """一次核可 outbox-mutation 不得轉用去 ship（binding 不符）。"""
    broker = CapabilityBroker()
    granted_binding = _binding(SensitiveAction.OUTBOX_MUTATION)
    cap = broker.grant(granted_binding)
    gate = DegradedOperationGate(mode=DegradedMode.PER_CASE_APPROVAL, broker=broker)

    target_binding = _binding(SensitiveAction.SHIP)
    decision = gate.authorize(target_binding, capability=cap)
    assert decision.allowed is False
    assert decision.reason == "binding-mismatch"


def test_action_bound_revision_mismatch_denied() -> None:
    broker = CapabilityBroker()
    binding = _binding(SensitiveAction.SHIP)
    cap = broker.grant(binding)
    gate = DegradedOperationGate(mode=DegradedMode.PER_CASE_APPROVAL, broker=broker)

    stale = CapabilityBinding(
        action=binding.action,
        work_id=binding.work_id,
        run_id=binding.run_id,
        subject_hash=binding.subject_hash,
        authority_revision="auth-rev-2",  # authority_revision 前進了
    )
    decision = gate.authorize(stale, capability=cap)
    assert decision.allowed is False
    assert decision.reason == "binding-mismatch"


# --- 短效：逾時被拒 ---------------------------------------------------------

def test_expired_capability_denied() -> None:
    clock = _FakeClock()
    broker = CapabilityBroker(clock=clock)
    binding = _binding(SensitiveAction.MERGE)
    cap = broker.grant(binding, ttl_seconds=60.0)
    gate = DegradedOperationGate(mode=DegradedMode.PER_CASE_APPROVAL, broker=broker)

    clock.now += 61.0  # 逾 TTL
    decision = gate.authorize(binding, capability=cap)
    assert decision.allowed is False
    assert decision.reason == "capability-expired"


# --- 不可經 durable state 重放：capability 本體不得落地 ---------------------

def test_capability_material_absent_from_durable_state() -> None:
    broker = CapabilityBroker()
    cap = broker.grant(_binding(SensitiveAction.SHIP))
    # 模擬 durable state 內容（jobs.json / journal / done 結果的公開投影）。
    public = cap.to_public_dict()
    serialized = repr(public) + str(cap)
    assert cap.nonce not in serialized
    assert find_capability_material(serialized, [cap.nonce]) == []


def test_public_dict_and_str_never_leak_nonce() -> None:
    cap = CapabilityBroker().grant(_binding(SensitiveAction.SHIP))
    assert cap.nonce not in str(cap)
    assert cap.nonce not in repr(cap)
    assert "nonce" not in cap.to_public_dict()


def test_find_capability_material_detects_leak() -> None:
    cap = CapabilityBroker().grant(_binding(SensitiveAction.SHIP))
    leaked = f'{{"note": "{cap.nonce}"}}'  # 若 nonce 不慎寫進 durable state
    assert find_capability_material(leaked, [cap.nonce]) == [cap.nonce]


# --- 降級開關解析（裁決 10-5：預設逐案核可）--------------------------------

def test_default_mode_is_per_case_approval() -> None:
    assert resolve_degraded_mode({}) is DegradedMode.PER_CASE_APPROVAL


def test_disabled_mode_from_env() -> None:
    assert resolve_degraded_mode({DEGRADED_MODE_ENV: "disabled"}) is DegradedMode.DISABLED


def test_unknown_mode_falls_back_to_per_case_approval() -> None:
    """無法辨識的值回退到最保守可用預設，而非靜默放行。"""
    assert resolve_degraded_mode({DEGRADED_MODE_ENV: "wide-open"}) is DegradedMode.PER_CASE_APPROVAL


def test_build_gate_wires_env_mode() -> None:
    gate = build_gate(env={DEGRADED_MODE_ENV: "disabled"})
    assert gate.mode is DegradedMode.DISABLED
    gate2 = build_gate(env={})
    assert gate2.mode is DegradedMode.PER_CASE_APPROVAL


def test_sensitive_action_closed_list() -> None:
    """封閉清單：涵蓋 headless acceptance／outbox mutation／ship／merge。"""
    values = {a.value for a in SensitiveAction}
    assert values == {"headless-acceptance", "outbox-mutation", "ship", "merge"}
