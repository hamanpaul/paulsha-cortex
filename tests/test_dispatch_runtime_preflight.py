"""#262 dispatch runtime preflight 的行為測試。

每個測試對應 docs/superpowers/specs/dispatch-runtime-preflight-spec.md 的
Requirement（R1~R5），斷言 Requirement 的語意而非欄位存在性。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import sys

import pytest

from paulsha_cortex.coordinator.model_identities import ModelIdentity
from paulsha_cortex.coordinator.runtime_preflight import (
    CAPABILITY_KINDS,
    DispatchGateDecision,
    ExecutorEnvironment,
    PreflightOutcome,
    ProbeBudget,
    ProviderFreshness,
    RuntimeCapability,
    evaluate_dispatch_gate,
    run_runtime_preflight,
)


# ---------------------------------------------------------------- 共用 fixture


def _executable(directory, name: str) -> str:
    """在 directory 內造一個可執行的 stub，回傳其路徑。"""

    target = directory / name
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(target)


def _host_env(tmp_path, **overrides) -> ExecutorEnvironment:
    """與 host 一致的 executor 環境（模組確實可 import、PATH 為 host PATH）。"""

    params = {
        "name": "host-parity",
        "interpreter": (sys.executable,),
        "path": os.environ.get("PATH", ""),
        # 必須沿用真實 HOME：pytest 裝在 user-site（$HOME/.local），換掉 HOME
        # 就等於換了一個 executor 環境，那是 _isolated_env 的工作。
        "home": os.path.expanduser("~"),
        "provider_identity": None,
    }
    params.update(overrides)
    return ExecutorEnvironment(**params)


def _isolated_env(tmp_path, **overrides) -> ExecutorEnvironment:
    """模擬 Spark 隔離環境：真實 interpreter 但看不到 host site-packages。

    `python3 -I -S` 會停用 site 模組，因此 host site-packages 內的套件
    （例如 pytest）在此 interpreter 下無法 import——這正是 #262 的情境。
    """

    params = {
        "name": "isolated-executor",
        "interpreter": (sys.executable, "-I", "-S"),
        "path": str(tmp_path / "empty-bin"),
        "home": str(tmp_path / "sandbox-home"),
        "provider_identity": None,
    }
    params.update(overrides)
    (tmp_path / "empty-bin").mkdir(exist_ok=True)
    return ExecutorEnvironment(**params)


class _CountingLauncherFactory:
    """記錄 model session 建立次數；preflight 攔截時必須維持 0。"""

    def __init__(self) -> None:
        self.model_invocations = 0

    def __call__(self, identity):  # pragma: no cover - 被呼叫即代表測試失敗
        self.model_invocations += 1
        return object()


def _fresh_executor_auth_cache(*executors: str) -> dict[str, ProviderFreshness]:
    """#442：預填 manager 的 process-level executor auth 快取為「剛探測過、ok」。

    manager wiring 測試若不預填，`provider:executor` 宣告會讓
    `_combined_provider_prober` 真的 spawn CLI 子行程——測試必須維持 hermetic。
    """

    import time as _time

    now = _time.time()
    return {
        executor: ProviderFreshness(
            provider_id=executor,
            status="ok",
            observed_at=now,
            ttl_seconds=900.0,
            source="live-probe",
        )
        for executor in executors
    }


_BUILDER = ModelIdentity(
    executor="claude",
    model_id="opus-5",
    independence_domain="anthropic",
    capabilities=("build",),
)
_ALT_BUILDER = ModelIdentity(
    executor="codex",
    model_id="gpt-5.4",
    independence_domain="openai",
    capabilities=("build",),
)


# ------------------------------------------------------------------ R1 / R2


def test_missing_module_blocks_dispatch_with_zero_model_calls(tmp_path):
    """R2：executor 環境缺 module 時在 dispatch 前攔截，model invocation 維持 0。"""

    factory = _CountingLauncherFactory()
    decision = evaluate_dispatch_gate(
        card="tdd-red",
        requirements=(RuntimeCapability("module", "pytest"),),
        candidates=(_BUILDER,),
        environment_for=lambda identity: _isolated_env(tmp_path),
        launcher_factory=factory,
    )

    assert isinstance(decision, DispatchGateDecision)
    assert decision.action == "needs_human"
    assert decision.identity is None
    # 語意斷言：確實判為 capability missing，而非泛用失敗。
    assert decision.result.outcome is PreflightOutcome.CAPABILITY_MISSING
    assert "module:pytest" in decision.result.missing_capabilities
    # 核心驗收：完全沒有建立 model session。
    assert factory.model_invocations == 0
    assert decision.model_invocations == 0


def test_missing_executable_blocks_dispatch(tmp_path):
    """R2：reviewer sandbox 缺 socat 時同樣在 dispatch 前攔截。"""

    assert shutil.which("sh") is not None, "host 必須有 sh 才能驗證 PATH 隔離語意"

    factory = _CountingLauncherFactory()
    decision = evaluate_dispatch_gate(
        card="requesting-code-review",
        requirements=(RuntimeCapability("executable", "socat"),),
        candidates=(_BUILDER,),
        environment_for=lambda identity: _isolated_env(tmp_path),
        launcher_factory=factory,
    )

    assert decision.action == "needs_human"
    assert decision.result.outcome is PreflightOutcome.CAPABILITY_MISSING
    assert "executable:socat" in decision.result.missing_capabilities
    assert factory.model_invocations == 0

    # 對照組：把 socat stub 放進 executor 環境的 PATH 後必須放行，
    # 證明判定依據是 executor PATH 而非「一律失敗」。
    bindir = tmp_path / "with-socat"
    bindir.mkdir()
    _executable(bindir, "socat")
    ok_factory = _CountingLauncherFactory()
    ok_decision = evaluate_dispatch_gate(
        card="requesting-code-review",
        requirements=(RuntimeCapability("executable", "socat"),),
        candidates=(_BUILDER,),
        environment_for=lambda identity: _isolated_env(tmp_path, path=str(bindir)),
        launcher_factory=ok_factory,
    )
    assert ok_decision.action == "dispatch"
    assert ok_decision.result.outcome is PreflightOutcome.OK
    assert ok_factory.model_invocations == 1


def test_candidate_contract_failure_reroutes_to_next_identity(tmp_path):
    """A launcher-contract exception is local to the candidate being tried."""

    def environment_for(identity):
        if identity is _BUILDER:
            raise ValueError("missing builder credential grant")
        return _host_env(tmp_path, name="codex-env")

    decision = evaluate_dispatch_gate(
        card="tdd-red",
        requirements=(RuntimeCapability("executable", "sh"),),
        candidates=(_BUILDER, _ALT_BUILDER),
        environment_for=environment_for,
    )

    assert decision.action == "reroute"
    assert decision.identity is _ALT_BUILDER
    assert decision.attempts[0].outcome is PreflightOutcome.CAPABILITY_MISSING
    assert decision.attempts[0].missing_capabilities == ("bridge:dispatch-contract",)
    assert "missing builder credential grant" in decision.attempts[0].findings[0].reason


def test_preflight_uses_executor_environment_not_host(tmp_path):
    """R2：host 有而 executor 環境沒有的 module，必須判為缺失。

    這是 #262 的核心——不得因為 manager 這側 import 得到就放行。
    """

    # 前提：host（跑測試的這個 interpreter）確實有 pytest。
    assert importlib.util.find_spec("pytest") is not None

    host_result = run_runtime_preflight(
        card="tdd-red",
        requirements=(RuntimeCapability("module", "pytest"),),
        identity=_BUILDER,
        environment=_host_env(tmp_path),
    )
    assert host_result.outcome is PreflightOutcome.OK, "host 環境應該通過"

    # 同一個宣告，改在隔離 executor 環境檢查 → 必須判為缺失。
    isolated_result = run_runtime_preflight(
        card="tdd-red",
        requirements=(RuntimeCapability("module", "pytest"),),
        identity=_BUILDER,
        environment=_isolated_env(tmp_path),
    )
    assert isolated_result.outcome is PreflightOutcome.CAPABILITY_MISSING
    assert isolated_result.missing_capabilities == ("module:pytest",)

    # 進一步隔離變因：只改 interpreter（HOME／PATH 與 host 相同）也必須判為缺失，
    # 證明判定確實經由 executor 的 interpreter，而非 manager 這側的 import。
    interpreter_only = run_runtime_preflight(
        card="tdd-red",
        requirements=(RuntimeCapability("module", "pytest"),),
        identity=_BUILDER,
        environment=_host_env(
            tmp_path, name="interpreter-only", interpreter=(sys.executable, "-I", "-S")
        ),
    )
    assert interpreter_only.outcome is PreflightOutcome.CAPABILITY_MISSING

    # R5：結果必須指出實際使用的 executor environment，而非 host。
    described = isolated_result.to_dict()["executor_environment"]
    assert described["name"] == "isolated-executor"
    assert described["interpreter"] == [sys.executable, "-I", "-S"]
    assert described["path"] == str(tmp_path / "empty-bin")
    assert described["home"] == str(tmp_path / "sandbox-home")


def test_card_capability_declaration_is_data_driven(tmp_path):
    """R1：新增 card 的宣告只是資料，不需修改 preflight 實作。"""

    # 四種 kind 都必須被通用執行器涵蓋。
    assert set(CAPABILITY_KINDS) == {"module", "executable", "bridge", "provider"}

    # 一張「新 card」用資料宣告兩項需求，preflight 逐項檢查。
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir, "socat")
    requirements = tuple(
        RuntimeCapability.parse(token)
        for token in ("module:json", "executable:socat")
    )
    result = run_runtime_preflight(
        card="a-brand-new-card",
        requirements=requirements,
        identity=_BUILDER,
        environment=_host_env(tmp_path, path=str(bindir)),
    )
    assert result.outcome is PreflightOutcome.OK
    # 逐項檢查：兩項宣告各自產生一筆 finding。
    assert tuple(f.capability.token for f in result.findings) == (
        "module:json",
        "executable:socat",
    )

    # 換一個 kind 也不需改實作：bridge 宣告同樣被通用執行器涵蓋。
    empty = tmp_path / "nothing"
    empty.mkdir()
    bridge_missing = run_runtime_preflight(
        card="a-brand-new-card",
        requirements=(RuntimeCapability("bridge", "socat"),),
        identity=_BUILDER,
        environment=_host_env(tmp_path, path=str(empty)),
    )
    assert bridge_missing.outcome is PreflightOutcome.CAPABILITY_MISSING
    assert bridge_missing.missing_capabilities == ("bridge:socat",)

    # bridge 也可以由 interpreter 內的 module 支撐（pty 是 stdlib）。
    bridge_ok = run_runtime_preflight(
        card="a-brand-new-card",
        requirements=(RuntimeCapability("bridge", "pty"),),
        identity=_BUILDER,
        environment=_host_env(tmp_path, path=str(empty)),
    )
    assert bridge_ok.outcome is PreflightOutcome.OK


# ------------------------------------------------------------------------ R3


def test_stale_degraded_snapshot_is_not_hard_block(tmp_path):
    """R3：超過 TTL 的 degraded 快照不得被當成當前事實。"""

    stale = ProviderFreshness(
        provider_id="github:hamanpaul/paulsha-cortex",
        status="degraded",
        observed_at=1000.0,
        ttl_seconds=900.0,
        source="snapshot",
        reason="github rate limit exceeded",
    )
    now = 1000.0 + 5000.0  # 遠超 TTL
    assert stale.is_fresh(now=now) is False

    probe_calls: list[str] = []

    def _probe(provider_id: str) -> ProviderFreshness:
        probe_calls.append(provider_id)
        return ProviderFreshness(
            provider_id=provider_id,
            status="ok",
            observed_at=now,
            ttl_seconds=900.0,
            source="live-probe",
            reason=None,
        )

    result = run_runtime_preflight(
        card="workflow-claim",
        requirements=(RuntimeCapability("provider", "github:hamanpaul/paulsha-cortex"),),
        identity=_BUILDER,
        environment=_host_env(tmp_path),
        snapshot_lookup=lambda pid: stale,
        provider_prober=_probe,
        budget=ProbeBudget(now=lambda: now),
        now=now,
    )

    # 語意：stale 的 degraded 被 live probe 覆蓋，最終判定為可用。
    assert result.outcome is PreflightOutcome.OK
    assert probe_calls == ["github:hamanpaul/paulsha-cortex"]
    # 且必須留下新鮮度證據，來源是 live probe 而非 stale snapshot。
    freshness = result.to_dict()["provider_freshness"]
    assert freshness[0]["source"] == "live-probe"
    assert freshness[0]["status"] == "ok"

    # 對照：同樣是 degraded 但仍在 TTL 內 → 才是當前事實，硬擋且不 probe。
    fresh_calls: list[str] = []
    fresh = ProviderFreshness(
        provider_id="github:hamanpaul/paulsha-cortex",
        status="degraded",
        observed_at=now - 10.0,
        ttl_seconds=900.0,
        source="snapshot",
        reason="github rate limit exceeded",
    )
    fresh_result = run_runtime_preflight(
        card="workflow-claim",
        requirements=(RuntimeCapability("provider", "github:hamanpaul/paulsha-cortex"),),
        identity=_BUILDER,
        environment=_host_env(tmp_path),
        snapshot_lookup=lambda pid: fresh,
        provider_prober=lambda pid: fresh_calls.append(pid),
        budget=ProbeBudget(now=lambda: now),
        now=now,
    )
    assert fresh_result.outcome is PreflightOutcome.PROVIDER_UNAVAILABLE
    assert fresh_calls == [], "TTL 內的快照應直接採信，不付出探測成本"


def test_four_outcomes_are_distinguishable(tmp_path):
    """R3/D4：四種結果各自獨立表達，不折疊成布林。"""

    now = 5000.0
    provider = RuntimeCapability("provider", "github:acme/repo")

    def _snapshot(status: str, age: float) -> ProviderFreshness:
        return ProviderFreshness(
            provider_id="github:acme/repo",
            status=status,
            observed_at=now - age,
            ttl_seconds=900.0,
            source="snapshot",
            reason="stale diagnostics" if status == "degraded" else None,
        )

    # 1. capability missing
    missing = run_runtime_preflight(
        card="c",
        requirements=(RuntimeCapability("module", "pytest"),),
        identity=_BUILDER,
        environment=_isolated_env(tmp_path),
        now=now,
    )

    # 2. provider unavailable（fresh degraded）
    unavailable = run_runtime_preflight(
        card="c",
        requirements=(provider,),
        identity=_BUILDER,
        environment=_host_env(tmp_path),
        snapshot_lookup=lambda pid: _snapshot("degraded", 10.0),
        now=now,
    )

    # 3. stale snapshot（逾期且無 prober 可用）
    stale = run_runtime_preflight(
        card="c",
        requirements=(provider,),
        identity=_BUILDER,
        environment=_host_env(tmp_path),
        snapshot_lookup=lambda pid: _snapshot("degraded", 5000.0),
        provider_prober=None,
        now=now,
    )

    # 4. probe inconclusive（逾期、有 prober 但探測無定論）
    def _inconclusive(pid: str):
        raise TimeoutError("probe timed out")

    inconclusive = run_runtime_preflight(
        card="c",
        requirements=(provider,),
        identity=_BUILDER,
        environment=_host_env(tmp_path),
        snapshot_lookup=lambda pid: _snapshot("degraded", 5000.0),
        provider_prober=_inconclusive,
        budget=ProbeBudget(now=lambda: now),
        now=now,
    )

    outcomes = [
        missing.outcome,
        unavailable.outcome,
        stale.outcome,
        inconclusive.outcome,
    ]
    assert outcomes == [
        PreflightOutcome.CAPABILITY_MISSING,
        PreflightOutcome.PROVIDER_UNAVAILABLE,
        PreflightOutcome.STALE_SNAPSHOT,
        PreflightOutcome.PROBE_INCONCLUSIVE,
    ]
    # 四者互異，且沒有被折疊成同一個布林。
    assert len(set(outcomes)) == 4

    # 處置也必須不同：只有前兩者是 hard block。
    assert missing.blocking is True
    assert unavailable.blocking is True
    assert stale.blocking is False, "stale 需要刷新，不是硬擋"
    assert inconclusive.blocking is False, "probe inconclusive 不應被當成失敗"


def test_live_probe_respects_budget(tmp_path):
    """R4/D6：live probe 有 timeout／快取／rate-limit 預算，同批次不重複探測。"""

    now = 7000.0
    provider = RuntimeCapability("provider", "github:acme/repo")
    stale = ProviderFreshness(
        provider_id="github:acme/repo",
        status="degraded",
        observed_at=now - 5000.0,
        ttl_seconds=900.0,
        source="snapshot",
        reason="stale",
    )

    calls: list[str] = []

    def _probe(pid: str) -> ProviderFreshness:
        calls.append(pid)
        return ProviderFreshness(
            provider_id=pid,
            status="ok",
            observed_at=now,
            ttl_seconds=900.0,
            source="live-probe",
            reason=None,
        )

    budget = ProbeBudget(ttl_seconds=300.0, timeout_seconds=5.0, max_probes=2, now=lambda: now)

    # 同一批次五張 card 都需要同一個 provider → 只能探測一次（快取共用）。
    for _ in range(5):
        result = run_runtime_preflight(
            card="c",
            requirements=(provider,),
            identity=_BUILDER,
            environment=_host_env(tmp_path),
            snapshot_lookup=lambda pid: stale,
            provider_prober=_probe,
            budget=budget,
            now=now,
        )
        assert result.outcome is PreflightOutcome.OK
    assert calls == ["github:acme/repo"], "同 provider identity 必須共用快取，不重複探測"
    assert budget.timeout_seconds == 5.0

    # rate-limit 預算：不同 provider 耗盡額度後不得再探測。
    other_calls: list[str] = []

    def _counting(pid: str) -> ProviderFreshness:
        other_calls.append(pid)
        return ProviderFreshness(
            provider_id=pid,
            status="ok",
            observed_at=now,
            ttl_seconds=900.0,
            source="live-probe",
            reason=None,
        )

    exhausted: list[PreflightOutcome] = []
    for index in range(4):
        pid = f"github:acme/repo-{index}"
        result = run_runtime_preflight(
            card="c",
            requirements=(RuntimeCapability("provider", pid),),
            identity=_BUILDER,
            environment=_host_env(tmp_path),
            snapshot_lookup=lambda _pid: ProviderFreshness(
                provider_id=_pid,
                status="degraded",
                observed_at=now - 5000.0,
                ttl_seconds=900.0,
                source="snapshot",
                reason="stale",
            ),
            provider_prober=_counting,
            budget=budget,
            now=now,
        )
        exhausted.append(result.outcome)

    # budget max_probes=2，第一次探測已在上面用掉 1 → 只剩 1 次。
    assert len(other_calls) == 1
    assert exhausted[0] is PreflightOutcome.OK
    # 額度耗盡後不再探測，退為 probe inconclusive（而非假裝失敗）。
    assert exhausted[1:] == [PreflightOutcome.PROBE_INCONCLUSIVE] * 3


# ------------------------------------------------------------------ R2 / D5


def test_reroute_when_alternative_identity_available(tmp_path):
    """D5：有合法替代 identity 時自動 re-route，仍滿足 capability 與 domain 規則。"""

    bindir = tmp_path / "alt-bin"
    bindir.mkdir()
    _executable(bindir, "socat")

    def _environment_for(identity: ModelIdentity) -> ExecutorEnvironment:
        if identity.executor == "claude":
            return _isolated_env(tmp_path, name="claude-sandbox")
        return _isolated_env(tmp_path, name="codex-sandbox", path=str(bindir))

    factory = _CountingLauncherFactory()
    decision = evaluate_dispatch_gate(
        card="requesting-code-review",
        requirements=(RuntimeCapability("executable", "socat"),),
        candidates=(_BUILDER, _ALT_BUILDER),
        environment_for=_environment_for,
        launcher_factory=factory,
    )

    assert decision.action == "reroute"
    assert decision.identity == _ALT_BUILDER
    # 語意：確實換了 identity，且沿用既有 candidate 順序與 domain 規則。
    assert decision.identity.independence_domain == "openai"
    assert decision.reason == "capability-missing-rerouted"
    # 被否決的 primary 必須留下具體證據。
    assert len(decision.attempts) == 2
    assert decision.attempts[0].identity_token == "claude/opus-5"
    assert decision.attempts[0].outcome is PreflightOutcome.CAPABILITY_MISSING
    assert decision.attempts[0].missing_capabilities == ("executable:socat",)
    assert decision.attempts[1].outcome is PreflightOutcome.OK
    # 只有最終被選中的 identity 建立 launcher，被 preflight 否決的不建立。
    assert factory.model_invocations == 1
    assert decision.model_invocations == 0


def test_needs_human_carries_specific_reason(tmp_path):
    """D5/R5：無替代時進入 needs_human 且帶具體 reason 與可觀測欄位。"""

    factory = _CountingLauncherFactory()
    decision = evaluate_dispatch_gate(
        card="requesting-code-review",
        requirements=(
            RuntimeCapability("executable", "socat"),
            RuntimeCapability("module", "pytest"),
        ),
        candidates=(_BUILDER, _ALT_BUILDER),
        environment_for=lambda identity: _isolated_env(
            tmp_path, name=f"{identity.executor}-sandbox"
        ),
        launcher_factory=factory,
    )

    assert decision.action == "needs_human"
    assert decision.identity is None
    assert factory.model_invocations == 0

    # reason 必須具體：指出缺哪些 capability，不能只是 "preflight failed"。
    assert "executable:socat" in decision.reason
    assert "module:pytest" in decision.reason

    payload = decision.to_dict()
    assert payload["action"] == "needs_human"
    # R5：顯示缺少的 capability、使用中的 executor environment、snapshot 新鮮度。
    assert set(payload["missing_capabilities"]) == {"executable:socat", "module:pytest"}
    environments = [attempt["executor_environment"]["name"] for attempt in payload["attempts"]]
    assert environments == ["claude-sandbox", "codex-sandbox"]
    assert "provider_freshness" in payload["attempts"][0]
    # 兩個 candidate 都被實際檢查過，且都失敗。
    assert len(payload["attempts"]) == 2
    assert all(
        attempt["outcome"] == PreflightOutcome.CAPABILITY_MISSING.value
        for attempt in payload["attempts"]
    )


def test_card_contract_declares_capabilities_in_shipped_deck():
    """R1：出貨的 card 契約真的用資料宣告了 capability。"""

    from paulsha_cortex.coordinator.runtime_preflight import card_runtime_requirements
    from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, load_cards

    cards = load_cards(DEFAULT_CARDS_PATH)

    # 跑測試的三張 card 宣告了 pytest —— 正是 #262 撞到的缺口。
    for card_id in ("tdd-red", "subagent-build", "verification"):
        requirements = card_runtime_requirements(card_id, cards=cards)
        assert RuntimeCapability("module", "pytest") in requirements, card_id

    # #369：ship phase 的兩張卡宣告了 provider 需求——這是修復死碼路徑後第一批
    # 真正的輸入，否則接線了也不會被觸發（cards.yaml 修復前只有 module:pytest
    # 宣告，從無 provider: 宣告）。
    # #442：同兩張卡再宣告 `provider:executor` 動態 sentinel——小範圍試啟用
    # dispatch 前的 executor 登入態閘門（其餘卡待 ship-phase 觀測無誤再擴大）。
    from paulsha_cortex.coordinator.runtime_preflight import PROVIDER_EXECUTOR_SENTINEL

    for card_id in ("openspec-archive", "policy-commit"):
        requirements = card_runtime_requirements(card_id, cards=cards)
        assert (
            RuntimeCapability("provider", "github:hamanpaul/paulsha-cortex") in requirements
        ), card_id
        assert (
            RuntimeCapability("provider", PROVIDER_EXECUTOR_SENTINEL) in requirements
        ), card_id

    # 未宣告的 card 是 no-op，行為與 #262 之前相同。
    assert card_runtime_requirements("workflow-claim", cards=cards) == ()
    assert card_runtime_requirements("no-such-card", cards=cards) == ()


def test_card_contract_rejects_malformed_capability_declaration(tmp_path):
    """R1：宣告是資料，但 fail-closed —— 非法 kind 在載入時就擋下，避免無聲漏檢。"""

    from paulsha_cortex.deck.schema import DeckSchemaError, load_cards

    bad = tmp_path / "cards.yaml"
    bad.write_text(
        "version: 0\n"
        "cards:\n"
        "  - id: broken\n"
        "    kind: skill\n"
        "    type: headless\n"
        "    class: core\n"
        '    skill_ref: "x:y"\n'
        '    runtime_capabilities: ["nonsense:thing"]\n',
        encoding="utf-8",
    )
    with pytest.raises(DeckSchemaError) as excinfo:
        load_cards(bad)
    assert "runtime_capabilities" in str(excinfo.value)


def test_manager_gate_blocks_dispatch_before_model_session(tmp_path):
    """R2：manager 的 dispatch seam 在建立 model session 前就攔下缺 capability 的 card。"""

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator.model_identities import IdentityRegistry

    class _FakeStep:
        card = "tdd-red"  # 出貨契約宣告 module:pytest
        persona = "builder"
        phase = "build"
        commit_policy = "required"

    class _FakeRun:
        steps = ()
        primary_domain = None

    launched: list[str] = []
    specialized: list[str] = []

    class _Launcher:
        """只描述環境；一旦 launch 被呼叫就代表 gate 沒擋住。"""

        def __init__(self, identity, *, commit_required: bool = False):
            self._identity = identity
            self._commit_required = commit_required

        def as_commit_required(self):
            specialized.append(self._identity.executor)
            return _Launcher(self._identity, commit_required=True)

        def executor_environment(self):
            # 只有 specialize 後的 launcher 才回報正式 job 的環境，
            # 藉此證明 preflight 檢查的是 job 真正會用的環境。
            assert self._commit_required, "preflight 必須使用與正式 job 相同的 launcher 契約"
            return _isolated_env(tmp_path, name=f"{self._identity.executor}-sandbox")

        def launch(self, **kwargs):  # pragma: no cover - 被呼叫即測試失敗
            launched.append(self._identity.executor)
            raise AssertionError("preflight 失敗時不得建立 model session")

    registry = IdentityRegistry(schema_version=2, identities=(_BUILDER, _ALT_BUILDER))
    decision = manager._runtime_preflight_gate(
        _FakeRun(),
        _FakeStep(),
        identities=registry,
        launcher_factory=_Launcher,
    )

    assert decision is not None
    assert decision.action == "needs_human"
    assert decision.result.outcome is PreflightOutcome.CAPABILITY_MISSING
    assert decision.result.missing_capabilities == ("module:pytest",)
    # 兩個 candidate 都被實際檢查，且都沒有啟動 model session。
    assert len(decision.attempts) == 2
    assert launched == []
    # 兩個 candidate 都經過與正式 job 相同的 launcher specialize。
    assert specialized == ["claude", "codex"]

    # 對照：未宣告 capability 的 card → gate 是 no-op，完全走原路徑。
    class _PlainStep:
        card = "workflow-claim"
        persona = "builder"
        phase = "claim"
        commit_policy = None

    assert (
        manager._runtime_preflight_gate(
            _FakeRun(), _PlainStep(), identities=registry, launcher_factory=_Launcher
        )
        is None
    )


# ------------------------------------------------------------------------ #369


def test_provider_executor_sentinel_resolves_dynamically_per_candidate(tmp_path):
    """#369：`provider:executor` 是動態 sentinel，preflight 逐一嘗試 identity
    candidate 時把它解析成該 candidate 實際使用的 executor，而非寫死的字面值
    ——builder／manager persona 的 identity 本就會在 claude／codex 之間輪替
    （見 manager._identity_candidates_for_persona），寫死會驗錯 executor。
    """

    now = 2000.0
    seen: list[str] = []

    def _snapshot(provider_id: str) -> ProviderFreshness:
        seen.append(provider_id)
        return ProviderFreshness(
            provider_id=provider_id,
            status="degraded",
            observed_at=now - 10.0,
            ttl_seconds=900.0,
            source="snapshot",
            reason="not logged in",
        )

    decision = evaluate_dispatch_gate(
        card="c",
        requirements=(RuntimeCapability("provider", "executor"),),
        candidates=(_BUILDER, _ALT_BUILDER),
        environment_for=lambda identity: _host_env(tmp_path, name=f"{identity.executor}-env"),
        snapshot_lookup=_snapshot,
        now=now,
    )

    assert decision.action == "needs_human"
    # sentinel 從未原樣傳給 snapshot_lookup——兩個 candidate 各自被以「自己的
    # executor」查詢（claude 對應 _BUILDER，codex 對應 _ALT_BUILDER）。
    assert seen == ["claude", "codex"]
    assert "executor" not in seen


def test_provider_executor_sentinel_passes_when_resolved_executor_is_ok(tmp_path):
    """對照組：解析出的 executor 新鮮度為 ok 時放行，證明 sentinel 確實接上
    真正的判定邏輯，而不是一律擋下。"""

    now = 3000.0

    def _snapshot(provider_id: str) -> ProviderFreshness:
        return ProviderFreshness(
            provider_id=provider_id,
            status="ok",
            observed_at=now - 5.0,
            ttl_seconds=900.0,
            source="snapshot",
        )

    factory = _CountingLauncherFactory()
    decision = evaluate_dispatch_gate(
        card="c",
        requirements=(RuntimeCapability("provider", "executor"),),
        candidates=(_BUILDER,),
        environment_for=lambda identity: _host_env(tmp_path),
        launcher_factory=factory,
        snapshot_lookup=_snapshot,
        now=now,
    )

    assert decision.action == "dispatch"
    assert decision.result.outcome is PreflightOutcome.OK
    assert factory.model_invocations == 1


def test_manager_wiring_blocks_dispatch_on_fresh_degraded_provider_snapshot(
    tmp_path, monkeypatch
):
    """#369 RED→GREEN 的核心斷言：manager._runtime_preflight_gate 過去從未把
    snapshot_lookup／provider_prober 傳給 evaluate_dispatch_gate（兩者預設
    None），因此 provider capability 探測在生產環境是死碼——即使 monitor
    snapshot 顯示某個 provider degraded，dispatch 仍會放行（#369 root cause）。

    修復前這個測試會 FAIL：`decision.action == "dispatch"` 而非
    `"needs_human"`，因為 `_runtime_preflight_gate` 沒有接上任何 snapshot 來源
    ——`evaluate_dispatch_gate` 收到的 `snapshot_lookup=None` 讓
    `_resolve_provider_freshness` 直接判 STALE_SNAPSHOT（non-blocking）。
    """

    from datetime import datetime, timezone

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator.model_identities import IdentityRegistry
    from paulsha_cortex.monitor.work_models import ProviderSnapshot
    from paulsha_cortex.monitor.work_snapshot import WorkSnapshot, WorkSnapshotStore

    # #442：policy-commit 現在同時宣告 `provider:executor`；預填 process-level
    # 快取為 fresh ok，讓本測試聚焦 github provider 的 degraded 硬擋語意，
    # 且不 spawn 任何真實 CLI 探測子行程。
    monkeypatch.setattr(
        manager, "_EXECUTOR_AUTH_CACHE", _fresh_executor_auth_cache("claude", "codex")
    )

    provider_id = "github:hamanpaul/paulsha-cortex"
    # 用「現在」當 last_attempt_at，確保相對於 gate 實際呼叫時的 wall clock
    # 一定落在 TTL(900s) 內——測試斷言的是「fresh degraded 必須硬擋」，不是
    # stale 的次要分支。
    fresh_now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    store = WorkSnapshotStore(path=tmp_path / "work-items.snapshot.json")
    store.write(
        WorkSnapshot(
            sequence=1,
            written_at=fresh_now,
            providers={
                provider_id: ProviderSnapshot(
                    provider_id=provider_id,
                    status="degraded",
                    last_attempt_at=fresh_now,
                    last_success_at=None,
                    revision=None,
                    diagnostics=("github rate limit exceeded",),
                    sources=(),
                )
            },
            work_items=(),
            source_owners={},
            exclusions=(),
        )
    )

    class _FakeStep:
        card = "policy-commit"  # 出貨契約宣告 provider:github:hamanpaul/paulsha-cortex
        persona = "manager"
        phase = "ship"
        commit_policy = None

    class _FakeRun:
        steps = ()
        primary_domain = None

    class _Launcher:
        def __init__(self, identity):
            self._identity = identity

        def executor_environment(self):
            return _host_env(tmp_path, name=f"{self._identity.executor}-sandbox")

        def launch(self, **kwargs):  # pragma: no cover - 被呼叫即測試失敗
            raise AssertionError("provider degraded 時不得建立 model session")

    registry = IdentityRegistry(schema_version=2, identities=(_BUILDER, _ALT_BUILDER))
    decision = manager._runtime_preflight_gate(
        _FakeRun(),
        _FakeStep(),
        identities=registry,
        launcher_factory=_Launcher,
        snapshot_store=store,
    )

    assert decision is not None
    assert decision.action == "needs_human"
    assert decision.result.outcome is PreflightOutcome.PROVIDER_UNAVAILABLE
    assert f"providers unavailable: {provider_id}" in (decision.reason or "")


def test_manager_wiring_dispatches_when_provider_snapshot_absent(tmp_path, monkeypatch):
    """對照組：monitor 完全沒有這個 provider 的快照時（例如 daemon 剛啟動、
    尚未跑過第一輪掃描）不得硬擋——回退成 STALE_SNAPSHOT，non-blocking，行為
    與 #369 修復前對「無 snapshot」情境的保守面一致，只是現在死碼真的活了。
    """

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator.model_identities import IdentityRegistry
    from paulsha_cortex.monitor.work_snapshot import WorkSnapshotStore

    # #442：同上——預填 executor auth 快取，維持 hermetic。
    monkeypatch.setattr(
        manager, "_EXECUTOR_AUTH_CACHE", _fresh_executor_auth_cache("claude", "codex")
    )

    class _FakeStep:
        card = "policy-commit"
        persona = "manager"
        phase = "ship"
        commit_policy = None

    class _FakeRun:
        steps = ()
        primary_domain = None

    class _Launcher:
        def __init__(self, identity):
            self._identity = identity

        def executor_environment(self):
            return _host_env(tmp_path, name=f"{self._identity.executor}-sandbox")

        def launch(self, **kwargs):  # pragma: no cover
            raise AssertionError("測試不預期真的 launch")

    registry = IdentityRegistry(schema_version=2, identities=(_BUILDER, _ALT_BUILDER))
    empty_store = WorkSnapshotStore(path=tmp_path / "absent-snapshot.json")
    decision = manager._runtime_preflight_gate(
        _FakeRun(),
        _FakeStep(),
        identities=registry,
        launcher_factory=_Launcher,
        snapshot_store=empty_store,
    )

    assert decision is not None
    assert decision.action == "dispatch"
    assert decision.result.outcome is PreflightOutcome.STALE_SNAPSHOT


def test_manager_wiring_probes_executor_auth_via_fake_runner(tmp_path, monkeypatch):
    """#442 啟用 regression：ship-phase 卡宣告 `provider:executor` 後，manager
    的 dispatch gate 真的走 executor_auth 探測路徑——冷啟動（process 快取空）
    時以正確 argv 對候選 identity 的 executor 探測一次、結果回寫快取。runner
    為注入的 fake，全程不 spawn 實體 CLI。
    """

    import functools
    import subprocess as _subprocess

    from paulsha_cortex.coordinator import executor_auth, manager
    from paulsha_cortex.coordinator.model_identities import IdentityRegistry
    from paulsha_cortex.monitor.work_snapshot import WorkSnapshotStore

    calls: list[tuple[str, ...]] = []

    def _fake_runner(argv, *, timeout):
        calls.append(tuple(argv))
        return _subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(manager, "_EXECUTOR_AUTH_CACHE", {})
    monkeypatch.setattr(
        executor_auth,
        "check_executor_auth",
        functools.partial(executor_auth.check_executor_auth, runner=_fake_runner),
    )

    class _FakeStep:
        card = "policy-commit"  # #442 起宣告 provider:executor（與 provider:github 併存）
        persona = "manager"
        phase = "ship"
        commit_policy = None

    class _FakeRun:
        steps = ()
        primary_domain = None

    class _Launcher:
        def __init__(self, identity):
            self._identity = identity

        def executor_environment(self):
            return _host_env(tmp_path, name=f"{self._identity.executor}-sandbox")

        def launch(self, **kwargs):  # pragma: no cover
            raise AssertionError("測試不預期真的 launch")

    registry = IdentityRegistry(schema_version=2, identities=(_BUILDER, _ALT_BUILDER))
    empty_store = WorkSnapshotStore(path=tmp_path / "absent-snapshot.json")
    decision = manager._runtime_preflight_gate(
        _FakeRun(),
        _FakeStep(),
        identities=registry,
        launcher_factory=_Launcher,
        snapshot_store=empty_store,
    )

    assert decision is not None
    assert decision.action == "dispatch"
    # 首位 candidate（claude）探測 ok 即放行：sentinel 解析成該 candidate 實際
    # 的 executor，argv 正是 executor_auth 對 claude 定義的登入態指令，且只
    # 探測一次（codex 不需被打擾）。
    assert calls == [("claude", "auth", "status")]
    cached = manager._EXECUTOR_AUTH_CACHE.get("claude")
    assert cached is not None and cached.status == "ok"
    outcomes = {
        finding.capability.token: finding.outcome
        for finding in decision.result.findings
    }
    # github provider 無 monitor 快照 → STALE_SNAPSHOT（non-blocking）；
    # executor 登入態探測則真的跑了並回報 OK。
    assert outcomes["provider:executor"] is PreflightOutcome.OK
    assert (
        outcomes["provider:github:hamanpaul/paulsha-cortex"]
        is PreflightOutcome.STALE_SNAPSHOT
    )


def test_manager_wiring_reroutes_when_executor_rate_limited(tmp_path, monkeypatch):
    """#442：首位 candidate 的 executor 探測回報限流（degraded）時，gate 依
    既有 candidate 順序 re-route 到下一位（codex），而非直接 needs_human——
    「限流／登出擋在 model session spawn 之前」正是本閘門啟用的目的。
    """

    import functools
    import subprocess as _subprocess

    from paulsha_cortex.coordinator import executor_auth, manager
    from paulsha_cortex.coordinator.model_identities import IdentityRegistry
    from paulsha_cortex.monitor.work_snapshot import WorkSnapshotStore

    calls: list[tuple[str, ...]] = []

    def _fake_runner(argv, *, timeout):
        calls.append(tuple(argv))
        if argv[0] == "claude":
            # 比照 #369 實際案例：限流訊息同時帶 authenticate 字樣，分類必須
            # 是 rate_limited 而非 logged_out（executor_auth 的判定順序）。
            return _subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="API rate limit exceeded; please re-authenticate for higher quota",
            )
        return _subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(manager, "_EXECUTOR_AUTH_CACHE", {})
    monkeypatch.setattr(
        executor_auth,
        "check_executor_auth",
        functools.partial(executor_auth.check_executor_auth, runner=_fake_runner),
    )

    class _FakeStep:
        card = "policy-commit"
        persona = "manager"
        phase = "ship"
        commit_policy = None

    class _FakeRun:
        steps = ()
        primary_domain = None

    class _Launcher:
        def __init__(self, identity):
            self._identity = identity

        def executor_environment(self):
            return _host_env(tmp_path, name=f"{self._identity.executor}-sandbox")

        def launch(self, **kwargs):  # pragma: no cover
            raise AssertionError("測試不預期真的 launch")

    registry = IdentityRegistry(schema_version=2, identities=(_BUILDER, _ALT_BUILDER))
    empty_store = WorkSnapshotStore(path=tmp_path / "absent-snapshot.json")
    decision = manager._runtime_preflight_gate(
        _FakeRun(),
        _FakeStep(),
        identities=registry,
        launcher_factory=_Launcher,
        snapshot_store=empty_store,
    )

    assert decision is not None
    assert decision.action == "reroute"
    assert decision.identity is _ALT_BUILDER
    assert calls == [("claude", "auth", "status"), ("codex", "doctor", "--json")]
    # 被擋下的 claude 以 PROVIDER_UNAVAILABLE 記錄於 attempts，freshness 指名
    # 解析後的 executor（claude）與限流原因——sentinel 字面值不外洩。
    first = decision.attempts[0]
    assert first.blocking
    finding = next(
        item for item in first.findings if item.capability.token == "provider:executor"
    )
    assert finding.outcome is PreflightOutcome.PROVIDER_UNAVAILABLE
    assert finding.freshness is not None
    assert finding.freshness.provider_id == "claude"
    assert "rate limit" in (finding.freshness.reason or "")


def test_launcher_reports_same_environment_as_real_job():
    """D2：preflight 用的環境與正式 job 是同一份 env，不是另建一套。"""

    from paulsha_cortex.coordinator import launcher as launcher_mod

    subprocess_launcher = launcher_mod.SubprocessLauncher(executor="claude", model="opus-5")
    env = subprocess_launcher.executor_environment()

    # 與 launch() 內部用的 _git_scope_env() 完全一致的 PATH/HOME。
    expected = launcher_mod._git_scope_env()
    assert env.path == expected.get("PATH", "")
    assert env.home == expected.get("HOME", "")
    assert env.name == "claude:workspace-write"

    # reviewer 走 review scope（最小 env），且模式可辨識。
    review = subprocess_launcher.as_review_only(
        terminal_kind="workflow-review-result"
    ).executor_environment()
    assert review.name == "claude:review-only"
    assert review.path == launcher_mod._review_scope_env().get("PATH", "")


def test_status_renders_missing_capability_and_freshness(tmp_path, capsys):
    """R5：status/inspect 顯示缺少的 capability、executor environment 與 snapshot 新鮮度。"""

    from paulsha_cortex.porcelain import inspect as inspect_mod

    now = 9000.0
    stale = ProviderFreshness(
        provider_id="github:acme/repo",
        status="degraded",
        observed_at=now - 5000.0,
        ttl_seconds=900.0,
        source="snapshot",
        reason="github rate limit exceeded",
    )
    decision = evaluate_dispatch_gate(
        card="verification",
        requirements=(
            RuntimeCapability("module", "pytest"),
            RuntimeCapability("provider", "github:acme/repo"),
        ),
        candidates=(_BUILDER,),
        environment_for=lambda identity: _isolated_env(tmp_path, name="spark-sandbox"),
        snapshot_lookup=lambda pid: stale,
        provider_prober=None,
        now=now,
    )

    inspect_mod._print_status(
        {"updated_at": "now", "degraded": False, "runtime_preflight": decision.to_dict()}
    )
    out = capsys.readouterr().out

    assert "runtime-preflight: needs_human" in out
    # 缺少的 capability
    assert "module:pytest" in out
    # 使用中的 executor environment
    assert "env=spark-sandbox" in out
    assert str(tmp_path / "sandbox-home") in out
    # snapshot 新鮮度：來源、TTL、是否過期都看得到
    assert "provider github:acme/repo" in out
    assert "source=snapshot" in out
    assert "ttl=900.0s" in out
    assert "fresh=False" in out


def test_capability_parse_rejects_unknown_kind():
    """R1：宣告是資料，但 kind 必須在白名單內，避免無聲漏檢。"""

    with pytest.raises(ValueError):
        RuntimeCapability.parse("nonsense:thing")
    with pytest.raises(ValueError):
        RuntimeCapability.parse("module:")
    assert RuntimeCapability.parse("module:pytest") == RuntimeCapability("module", "pytest")
