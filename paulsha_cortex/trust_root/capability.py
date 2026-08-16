"""R7：敏感 action 的 capability 通道 + 降級運轉開關（Phase 1）。

spec §R7 要求 operator 逐案核可以 **capability** 形式表達，且滿足 action-bound＋
single-use＋短效＋不可經 durable state 重放。裁決 10-5 定：D6 join gate 未達成前，
降級運轉**預設為「逐案核可」**（非完全停用），並提供切到「完全停用」的 config。

**Phase 1（不需 root）交付**：capability 的資料形態、grant/consume 授權語意、
fail-closed（無 capability 一律拒絕）、逐案核可 vs 完全停用的降級開關、以及
「capability 本體不得落地 durable state」的常設掃描 helper。

**Phase 1 不提供（需 Phase 2 OS 邊界才完整）**：
- 通道的 OS 隔離：R7 要求通道為 Manager-owned `0700` 目錄下的 Unix socket，使
  headless UID 因目錄 traverse 被拒而無法 connect。Phase 1 尚無獨立 headless UID，
  socket 的 OS 隔離無意義，故本階段 capability 以 in-process broker 表達授權語意，
  **不**開 socket。
- 跨 process／重啟持久的 single-use nonce ledger：spec §R7 要求 nonce ledger 位於
  R2 的 OS 邊界內。Phase 1 的 nonce ledger 是 **in-process**（同一 broker 實例內
  single-use），足以示範語意；跨 process 去重與防重啟重放留待 Phase 2 落在受保護樹。

因此 Phase 1 的降級網在同 UID 下**不是**完整隔離——它是 join gate 未達成期間的
契約層安全網，把 fail-closed 語意先立起來，OS 強制由 Phase 2 承接。
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping

#: 逐案核可的預設 TTL（秒）；spec §R7 建議 ≤300。
DEFAULT_TTL_SECONDS = 300.0
#: 降級運轉開關的環境變數。
DEGRADED_MODE_ENV = "PSC_DEGRADED_OPERATION"


class SensitiveAction(Enum):
    """降級網涵蓋的敏感 action 封閉清單（spec §R10 Phase 1 第 6 條）。

    新增須改本 enum（比照 spec §R4 的封閉清單語意）——不接受任意字串。
    """

    HEADLESS_ACCEPTANCE = "headless-acceptance"
    OUTBOX_MUTATION = "outbox-mutation"
    SHIP = "ship"
    MERGE = "merge"


class DegradedMode(Enum):
    """降級運轉開關（裁決 10-5）。"""

    #: 預設：敏感 action 需 operator 逐案明示核可（有效 capability 才放行）。
    PER_CASE_APPROVAL = "per-case-approval"
    #: 完全停用：敏感 action 一律拒絕，capability 也不放行（供 operator 切換）。
    DISABLED = "disabled"


def resolve_degraded_mode(env: Mapping[str, str] | None = None) -> DegradedMode:
    """從環境解析降級模式；預設 PER_CASE_APPROVAL（裁決 10-5）。

    無法辨識的值一律回退到**最保守可用**的預設（逐案核可），而非靜默放行。
    """
    environ = os.environ if env is None else env
    raw = (environ.get(DEGRADED_MODE_ENV, "") or "").strip().lower()
    if raw == DegradedMode.DISABLED.value:
        return DegradedMode.DISABLED
    if raw == DegradedMode.PER_CASE_APPROVAL.value or raw == "":
        return DegradedMode.PER_CASE_APPROVAL
    return DegradedMode.PER_CASE_APPROVAL


@dataclass(frozen=True)
class CapabilityBinding:
    """capability 綁定的五元組（spec §R7 action-bound）。

    任一項不符即無效，MUST NOT 可轉用到其他 action 或 work。
    """

    action: SensitiveAction
    work_id: str
    run_id: str
    subject_hash: str
    authority_revision: str


@dataclass(frozen=True)
class Capability:
    """一次性授權憑證。

    **MUST NOT 落地任何 durable state**（spec §R7）——本物件只存在於記憶體與
    授權往返；落地的只有「已消耗 nonce」與不可還原的授權 attestation。
    `__str__`／`to_public_dict` 皆不吐 nonce，避免不慎寫進 log／journal。
    """

    nonce: str
    binding: CapabilityBinding
    issued_at: float          # broker clock（單調）
    ttl_seconds: float = DEFAULT_TTL_SECONDS

    def expires_at(self) -> float:
        return self.issued_at + self.ttl_seconds

    def to_public_dict(self) -> dict[str, object]:
        """不含 nonce 的展示投影（可安全記錄）。"""
        return {
            "action": self.binding.action.value,
            "work_id": self.binding.work_id,
            "run_id": self.binding.run_id,
            "ttl_seconds": self.ttl_seconds,
        }

    def __str__(self) -> str:  # pragma: no cover - 防呆
        return f"Capability(action={self.binding.action.value}, work_id={self.binding.work_id})"

    __repr__ = __str__


@dataclass(frozen=True)
class AuthorizationDecision:
    """授權判定結果（結構化，供 audit）。"""

    allowed: bool
    reason: str
    action: SensitiveAction
    mode: DegradedMode

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "action": self.action.value,
            "mode": self.mode.value,
        }


class CapabilityBroker:
    """發放與消費 capability；維護 in-process single-use nonce ledger。

    Phase 1：nonce ledger 是本實例的記憶體集合（同一 broker 內 single-use）。
    Phase 2 會把 ledger 移到 R2 OS 邊界內的 Manager-owned 樹以支援跨 process／
    防重啟重放（見模組 docstring）。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._consumed: set[str] = set()

    def grant(
        self,
        binding: CapabilityBinding,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> Capability:
        """operator 逐案核可：對指定 binding 鑄造一份短效、一次性 capability。"""
        return Capability(
            nonce=self._nonce_factory(),
            binding=binding,
            issued_at=self._clock(),
            ttl_seconds=ttl_seconds,
        )

    def consume(
        self,
        capability: Capability,
        binding: CapabilityBinding,
    ) -> AuthorizationDecision:
        """驗證並消費 capability。成功即作廢該 nonce（single-use）。

        依序檢查：nonce 未曾消費（single-use）→ 未過期（短效）→ binding 完全相符
        （action-bound）。任一不符即拒絕；**不**因失敗而消費 nonce（避免 DoS 把
        合法 capability 提前作廢——但相同 nonce 一旦成功消費即永久作廢）。
        """
        action = binding.action
        mode = DegradedMode.PER_CASE_APPROVAL
        if capability.nonce in self._consumed:
            return AuthorizationDecision(False, "nonce-already-consumed", action, mode)
        now = self._clock()
        if now >= capability.expires_at():
            return AuthorizationDecision(False, "capability-expired", action, mode)
        if capability.binding != binding:
            return AuthorizationDecision(False, "binding-mismatch", action, mode)
        # 通過：消費 nonce（single-use），放行。
        self._consumed.add(capability.nonce)
        return AuthorizationDecision(True, "capability-consumed", action, mode)

    def consumed_count(self) -> int:
        return len(self._consumed)


class DegradedOperationGate:
    """降級運轉閘：敏感 action 的唯一放行點（fail-closed）。

    - `DISABLED`：一律拒絕（reason=`degraded-operation-disabled`），連 capability
      也不放行。
    - `PER_CASE_APPROVAL`：需附一份對本 binding 有效的 capability；`None` 即拒絕
      （reason=`no-capability`），否則交由 broker consume 判定。
    """

    def __init__(self, *, mode: DegradedMode, broker: CapabilityBroker) -> None:
        self._mode = mode
        self._broker = broker

    @property
    def mode(self) -> DegradedMode:
        return self._mode

    def authorize(
        self,
        binding: CapabilityBinding,
        capability: Capability | None,
    ) -> AuthorizationDecision:
        action = binding.action
        if self._mode is DegradedMode.DISABLED:
            return AuthorizationDecision(
                False, "degraded-operation-disabled", action, self._mode
            )
        # PER_CASE_APPROVAL
        if capability is None:
            return AuthorizationDecision(False, "no-capability", action, self._mode)
        decision = self._broker.consume(capability, binding)
        # broker 以 PER_CASE_APPROVAL 記 mode；此處已知，直接沿用其 allowed/reason。
        return AuthorizationDecision(
            decision.allowed, decision.reason, action, self._mode
        )


def build_gate(
    *,
    env: Mapping[str, str] | None = None,
    broker: CapabilityBroker | None = None,
) -> DegradedOperationGate:
    """依環境建構降級閘（供 daemon／敏感 action 呼叫點取用）。"""
    return DegradedOperationGate(
        mode=resolve_degraded_mode(env),
        broker=broker or CapabilityBroker(),
    )


# ---------------------------------------------------------------------------
# spec §R7 Scenario「capability 不得出現在 durable state」——常設掃描 helper。
# ---------------------------------------------------------------------------

def find_capability_material(text: str, nonces: list[str]) -> list[str]:
    """在一段 durable-state 內容中找出任何 capability nonce 的可還原形式。

    回傳命中的 nonce 清單（空＝乾淨）。供常設測試對全部 Tier-0／Tier-1 資產內容
    掃描時使用；命中即代表 capability 本體外洩到 durable state（R7 違規）。
    """
    return [n for n in nonces if n and n in text]
