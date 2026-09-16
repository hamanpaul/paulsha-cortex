"""#262 dispatch 前的 runtime capability 與 provider 新鮮度驗證。

設計依據 docs/superpowers/specs/dispatch-runtime-preflight-design.md：

- D1 capability 以資料宣告，本模組是通用執行器，不為個別 card 寫分支。
- D2 探測一律在「即將實際被使用的 executor environment」內進行；module 檢查
  透過該環境的 interpreter 以子行程執行，executable 檢查只看該環境的 PATH。
  絕不使用 manager 這側的 import 或 host PATH，否則會產生假通過（#262 的
  pytest 在 host 有、在 Spark 隔離環境沒有，正是這個缺口）。
- D3 provider 健康採「快照 + TTL + 有界 probe」三層。
- D4 四種結果分開表達，不折疊為布林。
- D5 preflight 失敗優先 re-route，其次 needs_human。
- D6 probe 預算以 provider identity 為鍵，沿用 claim_readiness 的 TTL 快取模式。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "CAPABILITY_KINDS",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER_TTL_SECONDS",
    "CapabilityFinding",
    "DispatchGateDecision",
    "card_runtime_requirements",
    "ExecutorEnvironment",
    "PreflightOutcome",
    "PROVIDER_EXECUTOR_SENTINEL",
    "ProbeBudget",
    "ProviderFreshness",
    "RuntimeCapability",
    "RuntimePreflightResult",
    "evaluate_dispatch_gate",
    "render_preflight_status",
    "run_runtime_preflight",
]


# capability 宣告的 kind 白名單，對應 spec R1 列舉的四類。
CAPABILITY_KINDS: tuple[str, ...] = ("module", "executable", "bridge", "provider")

# provider snapshot 的預設 TTL；與 claim.PROVIDER_MAX_AGE_SECONDS 的 900 秒對齊。
DEFAULT_PROVIDER_TTL_SECONDS = 900.0

# 單次 capability 探測的 timeout（R4：探測必須有界）。
DEFAULT_PROBE_TIMEOUT_SECONDS = 10.0

# #369：provider capability 的動態 sentinel。card 宣告 `provider:executor` 時
# （而非寫死的 `provider:<literal>`），preflight 逐一嘗試 identity candidate
# 時把它解析成「這次嘗試的 identity 實際使用的 executor」，而不是固定字串。
# builder／manager persona 的 identity 本就會在 claude／codex 之間輪替
# （見 manager._identity_candidates_for_persona）；若卡片寫死某個 executor
# 名稱，換候選時就會驗錯 executor。sentinel 讓同一張卡對不同 candidate
# 各自驗證其真正會用到的 executor 登入態。
PROVIDER_EXECUTOR_SENTINEL = "executor"

# bridge 宣告名稱到「可用性如何判定」的資料表。bridge 依賴可能是一個
# executable（socat／script），也可能是 interpreter 內的 module（pty／fcntl）。
# 這仍是資料而非分支：新增 bridge 只需在此加一列。
_BRIDGE_PROBES: Mapping[str, tuple[str, str]] = {
    "pty": ("module", "pty"),
    "fcntl": ("module", "fcntl"),
    "termios": ("module", "termios"),
    "socat": ("executable", "socat"),
    "script": ("executable", "script"),
    "tmux": ("executable", "tmux"),
}


class PreflightOutcome(str, Enum):
    """D4：四種結果各自獨立，不折疊成「可用／不可用」布林。

    四者的正確處置不同——capability missing 要修環境或 re-route；provider
    unavailable 可等待或換 identity；stale snapshot 要刷新；probe inconclusive
    不應被當成失敗。
    """

    OK = "ok"
    CAPABILITY_MISSING = "capability_missing"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    STALE_SNAPSHOT = "stale_snapshot"
    PROBE_INCONCLUSIVE = "probe_inconclusive"


# 只有這兩種是 hard block；stale snapshot 要刷新、probe inconclusive 無定論，
# 皆不得擋住 dispatch（見 spec R3 與 design D4）。
_BLOCKING_OUTCOMES = frozenset(
    {PreflightOutcome.CAPABILITY_MISSING, PreflightOutcome.PROVIDER_UNAVAILABLE}
)

# 彙總多筆 finding 時的嚴重度排序：越前面越優先成為整體 outcome。
_OUTCOME_SEVERITY: tuple[PreflightOutcome, ...] = (
    PreflightOutcome.CAPABILITY_MISSING,
    PreflightOutcome.PROVIDER_UNAVAILABLE,
    PreflightOutcome.STALE_SNAPSHOT,
    PreflightOutcome.PROBE_INCONCLUSIVE,
    PreflightOutcome.OK,
)


@dataclass(frozen=True)
class RuntimeCapability:
    """一項 capability 宣告（R1）。純資料，preflight 依 kind 逐項檢查。"""

    kind: str
    name: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in CAPABILITY_KINDS:
            raise ValueError(
                f"unknown runtime capability kind: {self.kind!r} "
                f"(expected one of {', '.join(CAPABILITY_KINDS)})"
            )
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("runtime capability name must be a non-empty string")

    @property
    def token(self) -> str:
        """穩定的機械可比對識別字串，例如 `module:pytest`。"""

        return f"{self.kind}:{self.name}"

    @classmethod
    def parse(cls, spec: str) -> "RuntimeCapability":
        """由 `<kind>:<name>` 字串解析宣告；card YAML 直接寫這個形式。"""

        if not isinstance(spec, str) or ":" not in spec:
            raise ValueError(f"runtime capability must look like '<kind>:<name>': {spec!r}")
        kind, _, name = spec.partition(":")
        return cls(kind.strip(), name.strip())

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"kind": self.kind, "name": self.name, "token": self.token}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class ExecutorEnvironment:
    """D2：即將實際被使用的 executor 環境。

    preflight 的探測必須經由這裡描述的 interpreter／PATH／HOME 進行，才能與
    正式 job 一致；只檢查 host PATH 會產生假通過。
    """

    name: str
    interpreter: tuple[str, ...]
    path: str
    home: str
    provider_identity: str | None = None
    extra_env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.interpreter:
            raise ValueError("executor environment requires a non-empty interpreter argv")

    def environ(self) -> dict[str, str]:
        """正式 job 會看到的環境變數（sandbox policy 的具體展現）。"""

        env = {"PATH": self.path, "HOME": self.home}
        env.update(self.extra_env)
        return env

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "interpreter": list(self.interpreter),
            "path": self.path,
            "home": self.home,
            "provider_identity": self.provider_identity,
        }


@dataclass(frozen=True)
class ProviderFreshness:
    """R3：provider snapshot 的新鮮度語意（observed_at／TTL／source／reason）。"""

    provider_id: str
    status: str
    observed_at: float
    ttl_seconds: float = DEFAULT_PROVIDER_TTL_SECONDS
    source: str = "snapshot"
    reason: str | None = None

    def age_seconds(self, *, now: float) -> float:
        return float(now) - float(self.observed_at)

    def is_fresh(self, *, now: float) -> bool:
        age = self.age_seconds(now=now)
        return 0.0 <= age <= float(self.ttl_seconds)

    def to_dict(self, *, now: float | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "provider_id": self.provider_id,
            "status": self.status,
            "observed_at": self.observed_at,
            "ttl_seconds": self.ttl_seconds,
            "source": self.source,
            "reason": self.reason,
        }
        if now is not None:
            payload["age_seconds"] = self.age_seconds(now=now)
            payload["fresh"] = self.is_fresh(now=now)
        return payload


@dataclass
class ProbeBudget:
    """R4／D6：live probe 的 timeout／快取／rate-limit 預算，以 provider identity 為鍵。

    沿用 `claim_readiness.LiveProbeCache` 的 TTL 快取模式，不另立一套：同一批次
    內多張 card 需要同一 provider 時只探測一次，避免形成另一個昂貴 retry loop。
    """

    ttl_seconds: float = 300.0
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS
    max_probes: int = 4
    now: Callable[[], float] = time.monotonic
    _entries: dict[str, tuple[float, ProviderFreshness]] = field(default_factory=dict, repr=False)
    _consumed: int = field(default=0, repr=False)

    @property
    def consumed(self) -> int:
        return self._consumed

    @property
    def remaining(self) -> int:
        return max(0, self.max_probes - self._consumed)

    def peek(self, provider_id: str) -> ProviderFreshness | None:
        entry = self._entries.get(provider_id)
        if entry is None:
            return None
        stored_at, value = entry
        if self.now() - stored_at > self.ttl_seconds:
            self._entries.pop(provider_id, None)
            return None
        return value

    def store(self, provider_id: str, value: ProviderFreshness) -> None:
        self._entries[provider_id] = (self.now(), value)

    def consume(self) -> bool:
        """扣一次 rate-limit 額度；額度耗盡時回傳 False（不再探測）。"""

        if self._consumed >= self.max_probes:
            return False
        self._consumed += 1
        return True


@dataclass(frozen=True)
class CapabilityFinding:
    """單一 capability 宣告的檢查結果。"""

    capability: RuntimeCapability
    outcome: PreflightOutcome
    reason: str
    freshness: ProviderFreshness | None = None

    @property
    def satisfied(self) -> bool:
        return self.outcome is PreflightOutcome.OK

    def to_dict(self, *, now: float | None = None) -> dict[str, object]:
        return {
            "capability": self.capability.to_dict(),
            "token": self.capability.token,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "freshness": None if self.freshness is None else self.freshness.to_dict(now=now),
        }


@dataclass(frozen=True)
class RuntimePreflightResult:
    """一個 (card, identity, environment) 組合的 preflight 結果。"""

    card: str
    identity_token: str
    environment: ExecutorEnvironment
    findings: tuple[CapabilityFinding, ...]
    checked_at: float

    @property
    def outcome(self) -> PreflightOutcome:
        """彙總為單一 outcome，但四種結果仍各自可辨識（D4）。"""

        present = {finding.outcome for finding in self.findings}
        for candidate in _OUTCOME_SEVERITY:
            if candidate in present:
                return candidate
        return PreflightOutcome.OK

    @property
    def outcomes(self) -> tuple[PreflightOutcome, ...]:
        return tuple(dict.fromkeys(finding.outcome for finding in self.findings))

    @property
    def blocking(self) -> bool:
        return any(finding.outcome in _BLOCKING_OUTCOMES for finding in self.findings)

    @property
    def missing_capabilities(self) -> tuple[str, ...]:
        return tuple(
            finding.capability.token
            for finding in self.findings
            if finding.outcome is PreflightOutcome.CAPABILITY_MISSING
        )

    @property
    def unavailable_providers(self) -> tuple[str, ...]:
        return tuple(
            finding.capability.name
            for finding in self.findings
            if finding.outcome is PreflightOutcome.PROVIDER_UNAVAILABLE
        )

    @property
    def provider_freshness(self) -> tuple[ProviderFreshness, ...]:
        return tuple(f.freshness for f in self.findings if f.freshness is not None)

    def blocking_reason(self) -> str | None:
        """R2：可操作的具體原因，指出缺哪些 capability／哪個 provider 不可用。"""

        if not self.blocking:
            return None
        parts: list[str] = []
        if self.missing_capabilities:
            parts.append("missing capabilities: " + ", ".join(self.missing_capabilities))
        if self.unavailable_providers:
            parts.append("providers unavailable: " + ", ".join(self.unavailable_providers))
        return f"[{self.identity_token} @ {self.environment.name}] " + "; ".join(parts)

    def to_dict(self) -> dict[str, object]:
        return {
            "card": self.card,
            "identity": self.identity_token,
            "outcome": self.outcome.value,
            "outcomes": [item.value for item in self.outcomes],
            "blocking": self.blocking,
            "checked_at": self.checked_at,
            "executor_environment": self.environment.to_dict(),
            "missing_capabilities": list(self.missing_capabilities),
            "unavailable_providers": list(self.unavailable_providers),
            "findings": [f.to_dict(now=self.checked_at) for f in self.findings],
            "provider_freshness": [
                item.to_dict(now=self.checked_at) for item in self.provider_freshness
            ],
        }


# --------------------------------------------------------------- 探測基本動作


def _probe_module(
    environment: ExecutorEnvironment, module: str, *, timeout_seconds: float
) -> tuple[bool, str]:
    """在 executor 的 interpreter 內以子行程 import；不得用 manager 這側的 import。"""

    argv = [*environment.interpreter, "-c", f"import importlib; importlib.import_module({module!r})"]
    try:
        completed = subprocess.run(  # noqa: S603 - argv 為內部組裝，非 shell
            argv,
            env=environment.environ(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return False, f"executor interpreter not found: {environment.interpreter[0]}"
    except subprocess.TimeoutExpired:
        return False, f"module probe timed out after {timeout_seconds}s"
    if completed.returncode == 0:
        return True, "importable in executor interpreter"
    detail = (completed.stderr or "").strip().splitlines()
    tail = detail[-1] if detail else f"exit {completed.returncode}"
    return False, f"not importable in executor interpreter: {tail}"


def _probe_executable(environment: ExecutorEnvironment, name: str) -> tuple[bool, str]:
    """只查 executor 環境的 PATH；host PATH 不參與判定（spec R2）。"""

    resolved = shutil.which(name, path=environment.path)
    if resolved:
        return True, f"resolved on executor PATH: {resolved}"
    return False, f"not on executor PATH ({environment.path or '<empty>'})"


def _probe_bridge(
    environment: ExecutorEnvironment, name: str, *, timeout_seconds: float
) -> tuple[bool, str]:
    kind, target = _BRIDGE_PROBES.get(name, ("executable", name))
    if kind == "module":
        return _probe_module(environment, target, timeout_seconds=timeout_seconds)
    return _probe_executable(environment, target)


def _resolve_provider_freshness(
    provider_id: str,
    *,
    snapshot_lookup: Callable[[str], ProviderFreshness | None] | None,
    provider_prober: Callable[[str], ProviderFreshness | None] | None,
    budget: ProbeBudget | None,
    now: float,
) -> tuple[PreflightOutcome, str, ProviderFreshness | None]:
    """D3：快照 + TTL + 有界 probe 三層。"""

    snapshot = snapshot_lookup(provider_id) if snapshot_lookup is not None else None
    if snapshot is None:
        return (
            PreflightOutcome.STALE_SNAPSHOT,
            "no provider snapshot available",
            None,
        )

    if snapshot.is_fresh(now=now):
        # TTL 內的快照即當前事實，直接採信，不付出探測成本。
        if snapshot.status == "ok":
            return PreflightOutcome.OK, "provider snapshot fresh and ok", snapshot
        return (
            PreflightOutcome.PROVIDER_UNAVAILABLE,
            f"provider {snapshot.status}: {snapshot.reason or 'no reason recorded'}",
            snapshot,
        )

    # 逾期。#262 的教訓：stale 的 degraded 判斷不得被當成當前事實。
    age = snapshot.age_seconds(now=now)
    stale_reason = (
        f"snapshot stale ({age:.0f}s > ttl {snapshot.ttl_seconds:.0f}s); "
        f"last recorded status={snapshot.status} reason={snapshot.reason or 'none'}"
    )
    if provider_prober is None:
        return PreflightOutcome.STALE_SNAPSHOT, stale_reason, snapshot

    active_budget = budget if budget is not None else ProbeBudget()
    cached = active_budget.peek(provider_id)
    if cached is not None:
        # 同批次同 provider：走快取，不重複探測（D6）。
        if cached.status == "ok":
            return PreflightOutcome.OK, "provider live probe ok (cached)", cached
        return (
            PreflightOutcome.PROVIDER_UNAVAILABLE,
            f"provider live probe {cached.status} (cached): {cached.reason or 'no reason'}",
            cached,
        )

    if not active_budget.consume():
        return (
            PreflightOutcome.PROBE_INCONCLUSIVE,
            f"live probe budget exhausted ({active_budget.max_probes} probes); {stale_reason}",
            snapshot,
        )

    try:
        probed = provider_prober(provider_id)
    except Exception as exc:  # noqa: BLE001 - 探測失敗一律歸為無定論，不當成失敗
        return (
            PreflightOutcome.PROBE_INCONCLUSIVE,
            f"live probe inconclusive: {type(exc).__name__}: {exc}",
            snapshot,
        )
    if probed is None:
        return (
            PreflightOutcome.PROBE_INCONCLUSIVE,
            f"live probe returned no verdict; {stale_reason}",
            snapshot,
        )

    active_budget.store(provider_id, probed)
    if probed.status == "ok":
        return PreflightOutcome.OK, "provider live probe ok", probed
    return (
        PreflightOutcome.PROVIDER_UNAVAILABLE,
        f"provider live probe {probed.status}: {probed.reason or 'no reason'}",
        probed,
    )


# ---------------------------------------------------------------- card 宣告讀取


def card_runtime_requirements(
    card_id: str, *, cards: Mapping[str, object] | None = None
) -> tuple[RuntimeCapability, ...]:
    """讀出某張 card 的 capability 宣告（R1：資料驅動）。

    未宣告或查無此 card 時回傳空 tuple——preflight 對它是 no-op，行為與
    #262 之前完全相同，因此加宣告是純增量、不會回頭影響既有 card。
    """

    if cards is None:
        from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, load_cards

        cards = load_cards(DEFAULT_CARDS_PATH)
    card = cards.get(card_id)
    if card is None:
        return ()
    declared = getattr(card, "runtime_capabilities", ())
    return tuple(RuntimeCapability.parse(token) for token in declared)


# ------------------------------------------------------------------ 通用執行器


def run_runtime_preflight(
    *,
    card: str,
    requirements: Sequence[RuntimeCapability],
    identity: object,
    environment: ExecutorEnvironment,
    snapshot_lookup: Callable[[str], ProviderFreshness | None] | None = None,
    provider_prober: Callable[[str], ProviderFreshness | None] | None = None,
    budget: ProbeBudget | None = None,
    now: float | None = None,
) -> RuntimePreflightResult:
    """依 card 的 capability 宣告逐項檢查（R1 資料驅動，R2 在 executor 環境內）。"""

    checked_at = time.time() if now is None else float(now)
    timeout_seconds = budget.timeout_seconds if budget is not None else DEFAULT_PROBE_TIMEOUT_SECONDS
    findings: list[CapabilityFinding] = []

    for capability in requirements:
        if capability.kind == "module":
            ok, reason = _probe_module(environment, capability.name, timeout_seconds=timeout_seconds)
            outcome = PreflightOutcome.OK if ok else PreflightOutcome.CAPABILITY_MISSING
            findings.append(CapabilityFinding(capability, outcome, reason))
        elif capability.kind == "executable":
            ok, reason = _probe_executable(environment, capability.name)
            outcome = PreflightOutcome.OK if ok else PreflightOutcome.CAPABILITY_MISSING
            findings.append(CapabilityFinding(capability, outcome, reason))
        elif capability.kind == "bridge":
            ok, reason = _probe_bridge(environment, capability.name, timeout_seconds=timeout_seconds)
            outcome = PreflightOutcome.OK if ok else PreflightOutcome.CAPABILITY_MISSING
            findings.append(CapabilityFinding(capability, outcome, reason))
        else:  # provider —— kind 白名單已由 RuntimeCapability 保證
            provider_id = capability.name
            if provider_id == PROVIDER_EXECUTOR_SENTINEL:
                # #369：解析成這次嘗試的 identity 實際會用的 executor，而非
                # sentinel 字面值本身——sentinel 只在 card 宣告時出現。
                resolved_executor = getattr(identity, "executor", None)
                if resolved_executor:
                    provider_id = resolved_executor
            outcome, reason, freshness = _resolve_provider_freshness(
                provider_id,
                snapshot_lookup=snapshot_lookup,
                provider_prober=provider_prober,
                budget=budget,
                now=checked_at,
            )
            findings.append(CapabilityFinding(capability, outcome, reason, freshness))

    return RuntimePreflightResult(
        card=card,
        identity_token=_identity_token(identity),
        environment=environment,
        findings=tuple(findings),
        checked_at=checked_at,
    )


def _identity_token(identity: object) -> str:
    executor = getattr(identity, "executor", None)
    model_id = getattr(identity, "model_id", None)
    if executor and model_id:
        return f"{executor}/{model_id}"
    return str(identity)


def _candidate_contract_failure_result(
    *,
    card: str,
    identity: object,
    error: BaseException,
    now: float | None,
    environment: ExecutorEnvironment | None = None,
) -> RuntimePreflightResult:
    """Represent a per-candidate launcher-contract failure as a blocked attempt.

    A candidate can fail while its launcher is being specialized, before an
    executor environment can be described.  That failure is candidate-local:
    the dispatch gate must record it and continue to the next ordered
    candidate instead of aborting the whole re-route.  The fallback host
    description is only a serialization anchor; the finding explicitly says
    that no runtime probe was performed against it.
    """

    checked_at = time.time() if now is None else float(now)
    executor = getattr(identity, "executor", None)
    environment = environment or host_environment(
        name=f"{executor or 'candidate'}:compatibility-error",
        provider_identity=executor if isinstance(executor, str) else None,
    )
    capability = RuntimeCapability(kind="bridge", name="dispatch-contract")
    finding = CapabilityFinding(
        capability=capability,
        outcome=PreflightOutcome.CAPABILITY_MISSING,
        reason=(
            "candidate compatibility rejected before executor preflight: "
            f"{type(error).__name__}: {error}"
        ),
    )
    return RuntimePreflightResult(
        card=card,
        identity_token=_identity_token(identity),
        environment=environment,
        findings=(finding,),
        checked_at=checked_at,
    )


# ------------------------------------------------------------------ dispatch gate


@dataclass(frozen=True)
class DispatchGateDecision:
    """D5：preflight 失敗優先 re-route，其次 needs_human。

    `model_invocations` 恆為 0——這個 gate 位於 model session 建立之前，
    本身永遠不會啟動任何模型。
    """

    action: str
    identity: object | None
    reason: str | None
    result: RuntimePreflightResult
    attempts: tuple[RuntimePreflightResult, ...]
    launcher: object | None = None
    model_invocations: int = 0

    @property
    def blocked(self) -> bool:
        return self.action == "needs_human"

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "identity": None if self.identity is None else _identity_token(self.identity),
            "reason": self.reason,
            "model_invocations": self.model_invocations,
            "missing_capabilities": list(
                dict.fromkeys(
                    token for attempt in self.attempts for token in attempt.missing_capabilities
                )
            ),
            "result": self.result.to_dict(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


def evaluate_dispatch_gate(
    *,
    card: str,
    requirements: Sequence[RuntimeCapability],
    candidates: Sequence[object],
    environment_for: Callable[[object], ExecutorEnvironment],
    launcher_factory: Callable[[object], object] | None = None,
    snapshot_lookup: Callable[[str], ProviderFreshness | None] | None = None,
    provider_prober: Callable[[str], ProviderFreshness | None] | None = None,
    budget: ProbeBudget | None = None,
    now: float | None = None,
) -> DispatchGateDecision:
    """在建立 model session 之前決定 dispatch／re-route／needs_human。

    `candidates` 由呼叫端以既有的 identity 選擇規則產生並保持原順序——本 gate
    不改選擇順序，也不放寬 independence domain 規則（spec 非目標），只是在
    既有順序上略過 preflight 失敗者。
    """

    if not candidates:
        raise ValueError("dispatch gate requires at least one identity candidate")

    attempts: list[RuntimePreflightResult] = []
    active_budget = budget if budget is not None else ProbeBudget()

    for index, identity in enumerate(candidates):
        try:
            environment = environment_for(identity)
        except Exception as exc:  # noqa: BLE001 - one candidate must not abort reroute
            result = _candidate_contract_failure_result(
                card=card, identity=identity, error=exc, now=now
            )
        else:
            result = run_runtime_preflight(
                card=card,
                requirements=requirements,
                identity=identity,
                environment=environment,
                snapshot_lookup=snapshot_lookup,
                provider_prober=provider_prober,
                budget=active_budget,
                now=now,
            )
        attempts.append(result)
        if result.blocking:
            continue
        # 通過：到這裡才允許建立 launcher／model session。
        try:
            launcher = None if launcher_factory is None else launcher_factory(identity)
        except Exception as exc:  # noqa: BLE001 - one candidate must not abort reroute
            attempts[-1] = _candidate_contract_failure_result(
                card=card,
                identity=identity,
                error=exc,
                now=now,
                environment=environment,
            )
            continue
        return DispatchGateDecision(
            action="dispatch" if index == 0 else "reroute",
            identity=identity,
            reason=None if index == 0 else "capability-missing-rerouted",
            result=result,
            attempts=tuple(attempts),
            launcher=launcher,
        )

    # 全部 candidate 都被擋下 → needs_human，reason 必須具體可操作。
    reasons = [attempt.blocking_reason() for attempt in attempts]
    reason = "runtime preflight blocked all candidates -- " + " | ".join(
        item for item in reasons if item
    )
    return DispatchGateDecision(
        action="needs_human",
        identity=None,
        reason=reason,
        result=attempts[-1],
        attempts=tuple(attempts),
    )


# ------------------------------------------------------------------ R5 可觀測


def render_preflight_status(payload: Mapping[str, object]) -> list[str]:
    """把 gate decision 的 payload 轉成 status／inspect 可印的文字行。

    顯示缺少的 capability、使用中的 executor environment 與 snapshot 新鮮度。
    """

    lines = [f"runtime-preflight: {payload.get('action', 'unknown')}"]
    reason = payload.get("reason")
    if reason:
        lines.append(f"  reason: {reason}")
    missing = payload.get("missing_capabilities") or []
    if missing:
        lines.append(f"  missing capabilities: {', '.join(str(item) for item in missing)}")
    attempts = payload.get("attempts") or []
    for attempt in attempts:  # type: ignore[union-attr]
        env = attempt.get("executor_environment", {})
        lines.append(
            f"  [{attempt.get('identity')}] env={env.get('name')} "
            f"interpreter={' '.join(env.get('interpreter', []))} "
            f"PATH={env.get('path')} HOME={env.get('home')} -> {attempt.get('outcome')}"
        )
        for freshness in attempt.get("provider_freshness", []) or []:
            lines.append(
                f"    provider {freshness.get('provider_id')}: status={freshness.get('status')} "
                f"source={freshness.get('source')} age={freshness.get('age_seconds')}s "
                f"ttl={freshness.get('ttl_seconds')}s fresh={freshness.get('fresh')} "
                f"reason={freshness.get('reason')}"
            )
    return lines


def host_environment(*, name: str = "host", provider_identity: str | None = None) -> ExecutorEnvironment:
    """僅供 fallback：當 card 未宣告專屬 executor 環境時的 host 描述。"""

    import sys

    return ExecutorEnvironment(
        name=name,
        interpreter=(sys.executable,),
        path=os.environ.get("PATH", ""),
        home=os.path.expanduser("~"),
        provider_identity=provider_identity,
    )
