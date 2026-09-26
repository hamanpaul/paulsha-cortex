"""#600：overlay 指定模型的 dispatch 前可用性探測。"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from paulsha_cortex.coordinator.model_identities import IdentityRegistry, ModelIdentity
from paulsha_cortex.coordinator.runtime_preflight import PreflightOutcome


def test_copilot_model_probe_uses_requested_identity_and_classifies_only_known_unavailable():
    from paulsha_cortex.coordinator import executor_auth

    probe = getattr(executor_auth, "probe_copilot_model_availability", None)
    assert callable(probe), "缺少 Copilot model availability probe"

    calls: list[tuple[tuple[str, ...], float]] = []

    def runner(argv, *, timeout):
        calls.append((tuple(argv), timeout))
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr='Error: Model "MAI-Code-1.1-Flash" from --model flag is not available.',
        )

    status, reason = probe(
        "MAI-Code-1.1-Flash", runner=runner, timeout_seconds=7.0
    )

    assert status == "unavailable"
    assert "模型不可用" in reason
    assert len(calls) == 1
    argv, timeout = calls[0]
    assert argv[0] == "copilot"
    assert argv[argv.index("--model") + 1] == "MAI-Code-1.1-Flash"
    assert "--remote" not in argv
    assert "--name" not in argv
    assert "--log-dir" not in argv
    assert timeout == 7.0


def test_copilot_model_probe_treats_other_errors_and_timeout_as_unverifiable():
    from paulsha_cortex.coordinator import executor_auth

    probe = getattr(executor_auth, "probe_copilot_model_availability", None)
    assert callable(probe), "缺少 Copilot model availability probe"

    def failed_runner(argv, *, timeout):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="temporary network failure"
        )

    status, reason = probe("candidate", runner=failed_runner)
    assert status == "unknown"
    assert "無法驗證" in reason

    def timeout_runner(argv, *, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    status, reason = probe("candidate", runner=timeout_runner)
    assert status == "unknown"
    assert "無法驗證" in reason


def test_copilot_model_probe_accepts_success_for_the_requested_model():
    from paulsha_cortex.coordinator import executor_auth

    probe = getattr(executor_auth, "probe_copilot_model_availability", None)
    assert callable(probe), "缺少 Copilot model availability probe"

    status, reason = probe(
        "candidate",
        runner=lambda argv, *, timeout: subprocess.CompletedProcess(
            argv, 0, stdout='{"content":"OK"}', stderr=""
        ),
    )

    assert status == "available"
    assert "探測成功" in reason


def _manager_gate(tmp_path, monkeypatch, probe_result, *, packaged=False):
    from paulsha_cortex.coordinator import executor_auth, manager, model_resolution
    from paulsha_cortex.coordinator.runtime_preflight import host_environment

    copilot = ModelIdentity(
        executor="copilot",
        model_id="MAI-Code-1.1-Flash",
        independence_domain="microsoft",
        capabilities=("build",),
        origin=(
            model_resolution.IDENTITY_ORIGIN_PACKAGED
            if packaged
            else model_resolution.IDENTITY_ORIGIN_OVERLAY
        ),
    )
    codex = ModelIdentity(
        executor="codex",
        model_id="gpt-5.6-luna",
        independence_domain="openai",
        capabilities=("build",),
        origin=model_resolution.IDENTITY_ORIGIN_OVERLAY,
    )
    probe_calls: list[str] = []

    def probe(model_id, **kwargs):
        probe_calls.append(model_id)
        return probe_result

    monkeypatch.setattr(executor_auth, "probe_copilot_model_availability", probe)
    launcher_calls: list[str] = []

    class Launcher:
        def __init__(self, identity):
            launcher_calls.append(identity.executor)
            self.identity = identity

        def executor_environment(self):
            return host_environment(name=f"{self.identity.executor}-env")

    class Step:
        card = "model-probe-test-card"  # no runtime capability declaration
        persona = "builder"
        phase = "build"
        commit_policy = None

    run = SimpleNamespace(
        steps=(),
        primary_domain=None,
        sizing_band=None,
        model_chain_override=None,
    )
    registry = IdentityRegistry(schema_version=3, identities=(copilot, codex))
    decision = manager._runtime_preflight_gate(
        run,
        Step(),
        identities=registry,
        launcher_factory=Launcher,
    )
    return decision, probe_calls, launcher_calls, copilot, codex


def test_unavailable_overlay_copilot_model_reroutes_before_dispatch(tmp_path, monkeypatch):
    decision, probe_calls, launcher_calls, copilot, codex = _manager_gate(
        tmp_path,
        monkeypatch,
        ("unavailable", "Copilot CLI 明確回報指定模型不可用"),
    )

    assert probe_calls == [copilot.model_id]
    assert decision is not None
    assert decision.action == "reroute"
    assert decision.identity.executor == codex.executor
    assert launcher_calls == [codex.executor]
    blocked = decision.attempts[0]
    assert blocked.identity_token == "copilot/MAI-Code-1.1-Flash"
    assert blocked.outcome is PreflightOutcome.PROVIDER_UNAVAILABLE
    assert "模型不可用" in (blocked.blocking_reason() or "")


def test_unverifiable_overlay_model_is_diagnostic_only(tmp_path, monkeypatch, caplog):
    decision, probe_calls, launcher_calls, copilot, _codex = _manager_gate(
        tmp_path,
        monkeypatch,
        ("unknown", "模型可用性無法驗證：Copilot CLI 探測逾時"),
    )

    assert probe_calls == [copilot.model_id]
    assert decision is not None
    assert decision.action == "dispatch"
    assert decision.identity is copilot
    assert launcher_calls == [copilot.executor]
    assert any(
        finding.outcome is PreflightOutcome.PROBE_INCONCLUSIVE
        and "無法驗證" in (finding.reason or "")
        for finding in decision.result.findings
    )
    assert "模型可用性無法驗證" in caplog.text


def test_packaged_copilot_identity_does_not_receive_overlay_model_probe(tmp_path, monkeypatch):
    decision, probe_calls, launcher_calls, _copilot, _codex = _manager_gate(
        tmp_path,
        monkeypatch,
        ("unavailable", "Copilot CLI 明確回報指定模型不可用"),
        packaged=True,
    )

    assert decision is None
    assert probe_calls == []
    assert launcher_calls == []


def test_availability_probe_is_cached_within_ttl_and_unknown_is_not_cached(monkeypatch) -> None:
    from paulsha_cortex.coordinator import executor_auth

    """每次派工都探測會消耗 Copilot 請求；明確結果在 TTL 內重用，unknown 不快取。"""
    calls: list[str] = []
    results = iter([("available", "ok"), ("unknown", "timeout"), ("unknown", "timeout")])

    def probe(model_id):
        calls.append(model_id)
        return next(results)

    monkeypatch.setattr(executor_auth, "probe_copilot_model_availability", probe)
    clock = iter([0.0, 10.0, 1000.0, 1001.0])
    now = lambda: next(clock)

    assert executor_auth.cached_copilot_model_availability("m", ttl_seconds=900, clock=now)[0] == "available"
    assert executor_auth.cached_copilot_model_availability("m", ttl_seconds=900, clock=now)[0] == "available"
    assert calls == ["m"]
    assert executor_auth.cached_copilot_model_availability("m", ttl_seconds=900, clock=now)[0] == "unknown"
    assert executor_auth.cached_copilot_model_availability("m", ttl_seconds=900, clock=now)[0] == "unknown"
    assert calls == ["m", "m", "m"]
