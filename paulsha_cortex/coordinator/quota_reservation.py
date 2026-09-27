"""#838：跨 instance 共享的原子 quota reservation authority。

同一台機器上可能有多個 Manager instance（同帳號）各自嘗試針對同一個共享
pool/window 發起 spawn；本模組提供 ``reserve → bind → settle／release``
的原子生命週期，確保「兩個 instance 同時搶最後一單位配額時只有一個成功」，
並在 crash／restart 後可安全 reconcile，不誤放已消耗的配額、也不誤判活 job
為可釋放。

契約邊界（本票**不**做的事，留給 #839）：

- 不做候選排序、fallback、forecast——capacity 由呼叫端傳入（來自 #836 的
  觀測投影，或測試用固定 fixture），本模組只保證「同一時刻、同一
  pool/window 上，所有仍生效的 reservation 總量不超過呼叫端當次提供的
  capacity」；
- 不判斷某個 pool 是否『足夠』、不做 model 層的候選挑選——``pool_ref`` 沿用
  #836 的契約（``authority_id``／``account_id``／``pool_id``／``revision``），
  刻意不含 model／profile 維度，換 model 名字無法偷換到不同的 authority；
- 不接線到任何實際 spawn path。本模組本身保持 dormant／shadow，直到有呼叫
  端明確 import 並呼叫；:func:`reservation_authority_enabled` 是保留給未來
  整合者（#839）的 opt-in 開關，本模組的正確性完全不依賴這個開關的值——
  把它調回 off／整條移除即是 rollback。
- 不查詢任何 job registry／liveness 來源——:meth:`QuotaReservationAuthority.reconcile`
  的 evidence／resolution 完全由呼叫端提供與裁決，本模組只保證『裁決一旦
  給定，如何原子且冪等地套用』，不自行猜測 job 是否還活著。

儲存形態沿用 #836 ``quota_ledger.QuotaEventLedger`` 的硬化模式（``flock``、
``O_NOFOLLOW``、固定權限、fsync、大小上限、損毀一律 fail-closed），但事件
schema 完全不同（reservation 生命週期 vs. quota observation），因此獨立成
自己的 append-only JSONL 檔，不與 #836 的 ledger 共用檔案或 import 其
schema-core。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Mapping, Sequence

from .quota_observation import _DECIMAL_WIRE_RE

__all__ = [
    "ReservationCorrupt",
    "PoolDemand",
    "ReservationResult",
    "TransitionResult",
    "ReservationStatus",
    "QuotaReservationAuthority",
    "reservation_authority_enabled",
    "RESERVATION_LOGICAL_STATES",
]

#: :meth:`QuotaReservationAuthority.list_by_state` 接受的邏輯狀態——與折疊後
#: ``current["state"]`` 的可能值逐字相同（見 ``_fold``）。
RESERVATION_LOGICAL_STATES = frozenset({"reserved", "bound", "settled", "released"})

_MAX_STORE_BYTES = 32 * 1024 * 1024
_MAX_EVENTS = 200_000
_MAX_POOLS_PER_RESERVATION = 32
_MAX_ID_CHARS = 128
_MAX_NOTE_CHARS = 512
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_POOL_REF_KEYS = ("authority_id", "account_id", "pool_id", "revision")
_MAX_TIMESTAMP_MS = 253402300799999
_MAX_LEASE_MS = 31622400000  # 一年——只是防呆上限，不是業務語意
_SETTLE_OUTCOMES = frozenset({"succeeded", "failed", "cancelled"})
_RECONCILE_RESOLUTIONS = frozenset(
    {"confirmed-terminated", "confirmed-alive", "inconclusive"}
)
_RELEASE_REASONS = frozenset({"fail-before-spawn", "cancelled", "superseded"})

_ENV_ENFORCE_FLAG = "PSC_QUOTA_RESERVATION_ENFORCE"


class ReservationCorrupt(ValueError):
    """reservation store 不完整、超限、版本未知或權限形狀不可信——fail closed。"""


def reservation_authority_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """#839（未來整合者）用的 opt-in 開關；本模組的正確性不依賴它的值。

    只讀 ``PSC_QUOTA_RESERVATION_ENFORCE``；大小寫不敏感的 ``on`` 才是
    True，其餘（含未設定、任何拼錯的值）一律 False——預設 shadow、不阻擋
    既有派工。rollback 只需把環境變數改回非 ``on`` 或整條移除，不需要改
    任何程式碼。
    """
    env = os.environ if environment is None else environment
    return env.get(_ENV_ENFORCE_FLAG, "").strip().lower() == "on"


def _validate_pool_ref(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(_POOL_REF_KEYS):
        raise ValueError("invalid pool_ref shape")
    result: dict[str, str] = {}
    for key in _POOL_REF_KEYS:
        item = value[key]
        if not isinstance(item, str) or not item or len(item) > _MAX_ID_CHARS:
            raise ValueError(f"invalid pool_ref.{key}")
        result[key] = item
    return result


def _validate_amount(value: object) -> str:
    if not isinstance(value, str) or not _DECIMAL_WIRE_RE.match(value):
        raise ValueError("invalid amount: must be a non-negative finite decimal wire string")
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid amount: not decimal") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError("invalid amount: must be finite and non-negative")
    return value


def _validate_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {field_name}")
    return value


def _validate_timestamp(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0 or value > _MAX_TIMESTAMP_MS:
        raise ValueError(f"invalid {field_name}")
    return value


@dataclass(frozen=True)
class PoolDemand:
    """一個 reservation 內、單一 pool/window 上的需求量。"""

    pool_ref: Mapping[str, str]
    window_id: str
    amount: str  # decimal wire string（>=0、finite；沿用 #836 契約）

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_ref", _validate_pool_ref(self.pool_ref))
        object.__setattr__(self, "window_id", _validate_identifier(self.window_id, "window_id"))
        object.__setattr__(self, "amount", _validate_amount(self.amount))

    def key(self) -> tuple[tuple[str, str, str, str], str]:
        return (tuple(self.pool_ref[k] for k in _POOL_REF_KEYS), self.window_id)

    def to_dict(self) -> dict[str, Any]:
        return {"pool_ref": dict(self.pool_ref), "window_id": self.window_id, "amount": self.amount}


@dataclass(frozen=True)
class ReservationResult:
    """:meth:`QuotaReservationAuthority.reserve` 的回傳值。"""

    status: str  # granted | duplicate | conflict | denied | invalid
    reservation_id: str | None = None
    owner_token: str | None = None
    sequence: int = -1
    state: str | None = None
    lease_expires_at_ms: int | None = None
    denied_pools: tuple[dict[str, Any], ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class TransitionResult:
    """bind／settle／release／reconcile 的共同回傳型。"""

    status: str  # ok | duplicate | conflict | invalid | not-found
    state: str | None = None
    display_state: str | None = None
    sequence: int = -1
    reason: str | None = None


@dataclass(frozen=True)
class ReservationStatus:
    """一個 reservation 目前的完整投影（唯讀）。"""

    reservation_id: str
    state: str  # reserved | bound | settled | released（folded 後的邏輯終態）
    display_state: str  # 同 state，除非 lease 過期且尚未 confirmed-alive/terminated → uncertain
    sequence: int
    run_id: str
    card_id: str
    decision_id: str
    attempt_id: str
    job_id: str | None
    pools: tuple[dict[str, Any], ...]
    observation_version: str
    demand_version: str
    lease_expires_at_ms: int
    created_at_ms: int
    last_event_at_ms: int


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _pools_signature(pools: Sequence[PoolDemand]) -> Any:
    return sorted((demand.to_dict() for demand in pools), key=lambda d: _canonical_bytes(d))


class QuotaReservationAuthority:
    """跨 instance 共享的原子 reservation authority（單一 host、單一帳號）。

    以 ``flock`` 序列化同一份檔案的讀—決策—寫入三步，讓「檢查 capacity 是否
    足夠」與「記錄這次授予」在鎖內原子完成——這是兩個 process 競爭同一個
    pool 時「恰好一個成功」的唯一保證來源，不依賴呼叫端自己做互斥。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        if path is None:
            from paulsha_cortex.config.paths import quota_reservation_root

            path = quota_reservation_root() / "reservations.jsonl"
        self.path = Path(path)
        if ".." in self.path.parts:
            raise ValueError("reservation store path cannot contain parent traversal")
        self._failpoint = failpoint or (lambda stage: None)

    # -- 對外 API：reserve ------------------------------------------------

    def reserve(
        self,
        *,
        run_id: str,
        card_id: str,
        decision_id: str,
        attempt_id: str,
        pools: Sequence[PoolDemand],
        capacity_by_pool: Mapping[tuple[tuple[str, str, str, str], str], str],
        observation_version: str,
        demand_version: str,
        lease_ms: int,
        now_ms: int,
    ) -> ReservationResult:
        try:
            run_id = _validate_identifier(run_id, "run_id")
            card_id = _validate_identifier(card_id, "card_id")
            decision_id = _validate_identifier(decision_id, "decision_id")
            attempt_id = _validate_identifier(attempt_id, "attempt_id")
            observation_version = _validate_identifier(observation_version, "observation_version")
            demand_version = _validate_identifier(demand_version, "demand_version")
            now_ms = _validate_timestamp(now_ms, "now_ms")
            if type(lease_ms) is not int or lease_ms < 0 or lease_ms > _MAX_LEASE_MS:
                raise ValueError("invalid lease_ms")
            pools = tuple(pools)
            if not pools or len(pools) > _MAX_POOLS_PER_RESERVATION:
                raise ValueError("invalid pools: must be 1..32 demands")
            if not all(isinstance(item, PoolDemand) for item in pools):
                raise ValueError("invalid pools: must be PoolDemand instances")
            keys = [demand.key() for demand in pools]
            if len(keys) != len(set(keys)):
                raise ValueError("invalid pools: duplicate pool/window within one reservation")
            capacity: dict[tuple[tuple[str, str, str, str], str], str] = {}
            for demand in pools:
                key = demand.key()
                if key not in capacity_by_pool:
                    raise ValueError("missing capacity for a requested pool/window")
                capacity[key] = _validate_amount(capacity_by_pool[key])
        except (TypeError, ValueError) as exc:
            return ReservationResult(status="invalid", reason=str(exc))

        reservation_id = "resv:v1:" + hashlib.sha256(
            f"{run_id}\0{card_id}\0{decision_id}\0{attempt_id}".encode()
        ).hexdigest()
        pools_signature = _pools_signature(pools)

        fd = self._open_for_append()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            info = os.fstat(fd)
            self._check_file(info)
            records = self._read_fd(fd, info.st_size)
            folded = _fold(records)
            existing = folded.get(reservation_id)
            if existing is not None:
                if existing["pools_signature"] == pools_signature:
                    return ReservationResult(
                        status="duplicate",
                        reservation_id=reservation_id,
                        owner_token=existing["owner_token"],
                        sequence=existing["sequence"],
                        state=existing["state"],
                        lease_expires_at_ms=existing["lease_expires_at_ms"],
                    )
                return ReservationResult(
                    status="conflict",
                    reservation_id=reservation_id,
                    reason="decision-reused-with-different-composition",
                )
            if len(records) >= _MAX_EVENTS:
                raise ReservationCorrupt("reservation-store-event-limit")
            committed = _committed_totals(folded, now_ms)
            denied: list[dict[str, Any]] = []
            for demand in pools:
                key = demand.key()
                current = committed.get(key, Decimal(0))
                requested = Decimal(demand.amount)
                cap = Decimal(capacity[key])
                if current + requested > cap:
                    denied.append(
                        {
                            "pool_ref": dict(demand.pool_ref),
                            "window_id": demand.window_id,
                            "requested": demand.amount,
                            "committed": _decimal_wire(current),
                            "capacity": capacity[key],
                        }
                    )
            if denied:
                # all-or-none：任一 pool 不足即整份拒絕，不寫入任何事件、不持有
                # 任何一格容量（見模組 docstring／#838 AC2）。
                return ReservationResult(status="denied", denied_pools=tuple(denied))

            owner_token = os.urandom(16).hex()
            lease_expires_at_ms = min(now_ms + lease_ms, _MAX_TIMESTAMP_MS)
            entry = {
                "schema_version": 1,
                "kind": "reserve",
                "reservation_id": reservation_id,
                "sequence": 0,
                "run_id": run_id,
                "card_id": card_id,
                "decision_id": decision_id,
                "attempt_id": attempt_id,
                "owner_token": owner_token,
                "pools": [demand.to_dict() for demand in pools],
                "capacity_by_pool": [
                    {"pool_ref": dict(demand.pool_ref), "window_id": demand.window_id, "capacity": capacity[demand.key()]}
                    for demand in pools
                ],
                "observation_version": observation_version,
                "demand_version": demand_version,
                "lease_expires_at_ms": lease_expires_at_ms,
                "created_at_ms": now_ms,
                "event_at_ms": now_ms,
            }
            self._failpoint("reserve-before-append")
            self._append_fd(fd, entry, info.st_size)
            return ReservationResult(
                status="granted",
                reservation_id=reservation_id,
                owner_token=owner_token,
                sequence=0,
                state="reserved",
                lease_expires_at_ms=lease_expires_at_ms,
            )
        finally:
            os.close(fd)

    # -- 對外 API：狀態轉換 ------------------------------------------------

    def bind(
        self,
        *,
        reservation_id: str,
        owner_token: str,
        attempt_id: str,
        job_id: str,
        expected_sequence: int,
        now_ms: int,
    ) -> TransitionResult:
        try:
            job_id = _validate_identifier(job_id, "job_id")
        except ValueError:
            return TransitionResult(status="invalid", reason="invalid-job-id")

        def apply(current: dict[str, Any]) -> tuple[dict[str, Any] | None, TransitionResult]:
            if current["state"] == "bound" and current["job_id"] == job_id:
                return None, TransitionResult(
                    status="duplicate", state=current["state"],
                    display_state=_display_state(current, now_ms), sequence=current["sequence"],
                )
            if current["state"] in ("settled", "released"):
                return None, TransitionResult(status="conflict", state=current["state"], reason="reservation-already-terminal")
            if current["state"] == "bound" and current["job_id"] != job_id:
                return None, TransitionResult(status="conflict", state=current["state"], reason="job-id-mismatch")
            if current["sequence"] != expected_sequence:
                return None, TransitionResult(status="conflict", state=current["state"], sequence=current["sequence"], reason="sequence-mismatch")
            entry = {
                "schema_version": 1,
                "kind": "bind",
                "reservation_id": reservation_id,
                "sequence": current["sequence"] + 1,
                "job_id": job_id,
                "event_at_ms": now_ms,
            }
            return entry, TransitionResult(status="ok", state="bound", display_state="bound", sequence=current["sequence"] + 1)

        return self._transition(
            reservation_id=reservation_id, owner_token=owner_token, attempt_id=attempt_id,
            now_ms=now_ms, failpoint_stage="bind-before-append", apply=apply,
        )

    def settle(
        self,
        *,
        reservation_id: str,
        owner_token: str,
        attempt_id: str,
        outcome: str,
        expected_sequence: int,
        now_ms: int,
        note: str | None = None,
    ) -> TransitionResult:
        if outcome not in _SETTLE_OUTCOMES:
            return TransitionResult(status="invalid", reason="unknown-outcome")
        if note is not None and (not isinstance(note, str) or len(note) > _MAX_NOTE_CHARS):
            return TransitionResult(status="invalid", reason="invalid-note")

        def apply(current: dict[str, Any]) -> tuple[dict[str, Any] | None, TransitionResult]:
            if current["state"] == "settled":
                if current["settle_outcome"] == outcome:
                    return None, TransitionResult(
                        status="duplicate", state=current["state"],
                        display_state=_display_state(current, now_ms), sequence=current["sequence"],
                    )
                return None, TransitionResult(status="conflict", state=current["state"], reason="settle-outcome-mismatch")
            if current["state"] == "released":
                return None, TransitionResult(status="conflict", state=current["state"], reason="reservation-already-terminal")
            if current["state"] != "bound":
                # 協定：reserve → 建 job 記錄 → bind(job_id) → 才 spawn。reserved
                # 恆表示「尚未 spawn」，因此 settle（job 終局，含 spawn 失敗）只接受
                # bound；reserved 只能 release（bind 前放棄）或由 reconcile 處理。
                return None, TransitionResult(status="conflict", state=current["state"], reason="settle-requires-bound")
            if current["sequence"] != expected_sequence:
                return None, TransitionResult(status="conflict", state=current["state"], sequence=current["sequence"], reason="sequence-mismatch")
            entry = {
                "schema_version": 1,
                "kind": "settle",
                "reservation_id": reservation_id,
                "sequence": current["sequence"] + 1,
                "outcome": outcome,
                "note": note,
                "event_at_ms": now_ms,
            }
            return entry, TransitionResult(status="ok", state="settled", display_state="settled", sequence=current["sequence"] + 1)

        return self._transition(
            reservation_id=reservation_id, owner_token=owner_token, attempt_id=attempt_id,
            now_ms=now_ms, failpoint_stage="settle-before-append", apply=apply,
        )

    def release(
        self,
        *,
        reservation_id: str,
        owner_token: str,
        attempt_id: str,
        reason: str,
        expected_sequence: int,
        now_ms: int,
    ) -> TransitionResult:
        if reason not in _RELEASE_REASONS:
            return TransitionResult(status="invalid", reason="unknown-release-reason")

        def apply(current: dict[str, Any]) -> tuple[dict[str, Any] | None, TransitionResult]:
            if current["state"] == "released":
                if current["release_reason"] == reason:
                    return None, TransitionResult(
                        status="duplicate", state=current["state"],
                        display_state=_display_state(current, now_ms), sequence=current["sequence"],
                    )
                return None, TransitionResult(status="conflict", state=current["state"], reason="release-reason-mismatch")
            if current["state"] == "settled":
                return None, TransitionResult(status="conflict", state=current["state"], reason="reservation-already-terminal")
            if current["state"] == "bound":
                # 已 spawn（bound）的 reservation 只能經 settle（job 終局）或
                # reconcile（可信 liveness 證據）結束；release 只給 spawn 前使用。
                # reserve() 冪等回放會交回 owner_token，若允許 release bound，
                # 重放者就能在活 job 仍占用時釋放 lease 並讓容量被重新 grant。
                return None, TransitionResult(status="conflict", state=current["state"], reason="reservation-bound-requires-settle-or-reconcile")
            if current["sequence"] != expected_sequence:
                return None, TransitionResult(status="conflict", state=current["state"], sequence=current["sequence"], reason="sequence-mismatch")
            entry = {
                "schema_version": 1,
                "kind": "release",
                "reservation_id": reservation_id,
                "sequence": current["sequence"] + 1,
                "reason": reason,
                "event_at_ms": now_ms,
            }
            return entry, TransitionResult(status="ok", state="released", display_state="released", sequence=current["sequence"] + 1)

        return self._transition(
            reservation_id=reservation_id, owner_token=owner_token, attempt_id=attempt_id,
            now_ms=now_ms, failpoint_stage="release-before-append", apply=apply,
        )

    def reconcile(
        self,
        *,
        reservation_id: str,
        evidence: Mapping[str, Any],
        resolution: str,
        expected_sequence: int,
        now_ms: int,
        renew_lease_ms: int | None = None,
    ) -> TransitionResult:
        """crash／restart 後的復原路徑——**刻意不驗證 owner_token／attempt_id**：

        原本的 owner 可能已隨行程崩潰消失，這條路徑就是為了讓「另一個知道如何
        取得可信 liveness 證據」的呼叫端（例如 job registry 查詢、耐久 job
        audit）能安全地把懸而未決的 reservation 導向終局。安全性不靠身分驗證，
        靠 ``resolution`` 只有三個值、且 ``inconclusive`` 永遠不釋放容量。
        """
        if resolution not in _RECONCILE_RESOLUTIONS:
            return TransitionResult(status="invalid", reason="unknown-resolution")
        if not isinstance(evidence, Mapping) or not isinstance(evidence.get("kind"), str) or not evidence.get("kind"):
            return TransitionResult(status="invalid", reason="invalid-evidence")
        try:
            evidence_wire = json.loads(_canonical_bytes(dict(evidence)))
        except (TypeError, ValueError):
            return TransitionResult(status="invalid", reason="invalid-evidence")
        if len(_canonical_bytes(evidence_wire)) > 4096:
            return TransitionResult(status="invalid", reason="evidence-too-large")
        if renew_lease_ms is not None and (
            type(renew_lease_ms) is not int or renew_lease_ms < 0 or renew_lease_ms > _MAX_LEASE_MS
        ):
            return TransitionResult(status="invalid", reason="invalid-renew-lease-ms")

        def apply(current: dict[str, Any]) -> tuple[dict[str, Any] | None, TransitionResult]:
            if current["state"] in ("settled", "released"):
                return None, TransitionResult(status="conflict", state=current["state"], reason="reservation-already-terminal")
            if current["sequence"] != expected_sequence:
                return None, TransitionResult(status="conflict", state=current["state"], sequence=current["sequence"], reason="sequence-mismatch")
            entry = {
                "schema_version": 1,
                "kind": "reconcile",
                "reservation_id": reservation_id,
                "sequence": current["sequence"] + 1,
                "resolution": resolution,
                "evidence": evidence_wire,
                "renew_lease_ms": renew_lease_ms,
                "event_at_ms": now_ms,
            }
            if resolution == "confirmed-terminated":
                new_state = "released"
                display = "released"
            else:
                new_state = current["state"]
                display = "uncertain" if resolution == "inconclusive" else current["state"]
            return entry, TransitionResult(status="ok", state=new_state, display_state=display, sequence=current["sequence"] + 1)

        return self._transition_no_auth(
            reservation_id=reservation_id, now_ms=now_ms,
            failpoint_stage="reconcile-before-append", apply=apply,
        )

    # -- 對外 API：唯讀 ------------------------------------------------------

    def status(self, reservation_id: str, *, now_ms: int) -> ReservationStatus | None:
        records = self._read()
        folded = _fold(records)
        current = folded.get(reservation_id)
        if current is None:
            return None
        return self._status_from_entry(current, now_ms)

    def list_by_state(self, state: str, *, now_ms: int) -> tuple[ReservationStatus, ...]:
        """唯讀：列出目前邏輯狀態恰為 ``state`` 的所有 reservation（依讀取時
        的檔案快照；含 lease 已過期者——本方法不代為篩選是否過期，呼叫端依
        回傳的 ``lease_expires_at_ms``／``display_state`` 自行判斷）。

        對抗審查第三輪 MAJOR（quota_admission.py:1008）新增：#839 原本的
        ``reserved`` 收斂掃描（``quota_admission.reconcile_reserved_reservations``）
        只能透過 ``AdmissionDecisionStore.enforced_admitted()`` 反查『曾經
        寫過 admit receipt 的決策』，再用 :meth:`status` 查它的 reservation
        狀態——如果合法持有者在 :meth:`reserve` 成功後、寫入那筆 receipt
        之前 crash（或 receipt 寫入本身失敗），這筆 reservation 就永遠不會
        出現在 store 的列舉裡，等於對收斂掃描完全隱形，容量永久卡住。這支
        方法讓收斂掃描改以本 authority（reservation 的唯一真相來源）為出發
        點，不必經過任何下游 receipt 是否成功寫入。

        純唯讀查詢：不修改任何狀態、不新增事件種類，#838 既有狀態機與
        reserve／bind／settle／release／reconcile 的寫入協定完全不變。"""
        if state not in RESERVATION_LOGICAL_STATES:
            raise ValueError(f"invalid reservation state: {state!r}")
        records = self._read()
        folded = _fold(records)
        return tuple(
            self._status_from_entry(entry, now_ms)
            for entry in folded.values()
            if entry["state"] == state
        )

    @staticmethod
    def _status_from_entry(entry: Mapping[str, Any], now_ms: int) -> ReservationStatus:
        return ReservationStatus(
            reservation_id=entry["reservation_id"],
            state=entry["state"],
            display_state=_display_state(entry, now_ms),
            sequence=entry["sequence"],
            run_id=entry["run_id"],
            card_id=entry["card_id"],
            decision_id=entry["decision_id"],
            attempt_id=entry["attempt_id"],
            job_id=entry["job_id"],
            pools=tuple(entry["pools"]),
            observation_version=entry["observation_version"],
            demand_version=entry["demand_version"],
            lease_expires_at_ms=entry["lease_expires_at_ms"],
            created_at_ms=entry["created_at_ms"],
            last_event_at_ms=entry["last_event_at_ms"],
        )

    def committed(self, *, now_ms: int) -> dict[tuple[tuple[str, str, str, str], str], str]:
        """目前仍生效（``reserved``／``bound``，含 ``uncertain``）的每 pool/window 總量。

        唯讀、供監控／測試稽核；本模組內部的容量檢查也用同一支函式，因此
        對外回報與實際 gating 邏輯保證一致，不會出現「看到的」與「用到的」
        分歧。
        """
        records = self._read()
        folded = _fold(records)
        totals = _committed_totals(folded, now_ms)
        return {key: _decimal_wire(value) for key, value in totals.items()}

    # -- 內部：共用轉換骨架 --------------------------------------------------

    def _transition(
        self,
        *,
        reservation_id: str,
        owner_token: str,
        attempt_id: str,
        now_ms: int,
        failpoint_stage: str,
        apply: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, TransitionResult]],
    ) -> TransitionResult:
        try:
            reservation_id = _validate_identifier(reservation_id, "reservation_id")
            if not isinstance(owner_token, str) or not owner_token:
                raise ValueError("invalid owner_token")
            attempt_id = _validate_identifier(attempt_id, "attempt_id")
            now_ms = _validate_timestamp(now_ms, "now_ms")
        except ValueError as exc:
            return TransitionResult(status="invalid", reason=str(exc))

        return self._with_lock(reservation_id, lambda current: self._authenticated_apply(
            current, owner_token=owner_token, attempt_id=attempt_id, apply=apply,
        ), now_ms=now_ms, failpoint_stage=failpoint_stage)

    def _transition_no_auth(
        self,
        *,
        reservation_id: str,
        now_ms: int,
        failpoint_stage: str,
        apply: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, TransitionResult]],
    ) -> TransitionResult:
        try:
            reservation_id = _validate_identifier(reservation_id, "reservation_id")
            now_ms = _validate_timestamp(now_ms, "now_ms")
        except ValueError as exc:
            return TransitionResult(status="invalid", reason=str(exc))

        def guarded(current: dict[str, Any] | None) -> tuple[dict[str, Any] | None, TransitionResult]:
            if current is None:
                return None, TransitionResult(status="not-found")
            return apply(current)

        return self._with_lock(reservation_id, guarded, now_ms=now_ms, failpoint_stage=failpoint_stage)

    @staticmethod
    def _authenticated_apply(
        current: dict[str, Any] | None,
        *,
        owner_token: str,
        attempt_id: str,
        apply: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, TransitionResult]],
    ) -> tuple[dict[str, Any] | None, TransitionResult]:
        if current is None:
            return None, TransitionResult(status="not-found")
        if owner_token != current["owner_token"]:
            return None, TransitionResult(status="invalid", state=current["state"], reason="owner-mismatch")
        if attempt_id != current["attempt_id"]:
            return None, TransitionResult(status="invalid", state=current["state"], reason="attempt-mismatch")
        return apply(current)

    def _with_lock(
        self,
        reservation_id: str,
        step: Callable[[dict[str, Any] | None], tuple[dict[str, Any] | None, TransitionResult]],
        *,
        now_ms: int,
        failpoint_stage: str,
    ) -> TransitionResult:
        fd = self._open_for_append()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            info = os.fstat(fd)
            self._check_file(info)
            records = self._read_fd(fd, info.st_size)
            folded = _fold(records)
            current = folded.get(reservation_id)
            entry, result = step(current)
            if entry is None:
                return result
            if len(records) >= _MAX_EVENTS:
                raise ReservationCorrupt("reservation-store-event-limit")
            self._failpoint(failpoint_stage)
            self._append_fd(fd, entry, info.st_size)
            return result
        finally:
            os.close(fd)

    def _open_for_append(self) -> int:
        """開啟（必要時建立）store 供寫入。首次建立目錄或檔案時一併 fsync 其
        父目錄，確保主機崩潰後第一筆 grant 的 dirent 仍在，重啟不會看到空
        ledger 而把同一容量再 grant 一次。"""
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_parent()
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise ReservationCorrupt("reservation-store-open-failed") from exc
        # 不以 exists() 判斷「是不是我建立的」：前一個建立者可能在 O_CREAT 後、
        # 目錄 fsync 前崩潰，後續呼叫者看到檔案已存在就不會補 fsync。每次寫入前
        # 都 fsync 父目錄與其上層，確保 dirent 耐久後才可能回 granted。
        try:
            _fsync_directory(parent.parent)
            _fsync_directory(parent)
        except BaseException:
            os.close(fd)
            raise
        return fd

    # -- 內部：檔案安全（比照 #836 quota_ledger 的硬化模式） ------------------

    def _read(self) -> list[dict[str, Any]]:
        self._check_parent(allow_missing=True)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ReservationCorrupt("reservation-store-open-failed") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            info = os.fstat(fd)
            self._check_file(info)
            return self._read_fd(fd, info.st_size)
        finally:
            os.close(fd)

    def _check_parent(self, *, allow_missing: bool = False) -> None:
        try:
            info = self.path.parent.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise ReservationCorrupt("reservation-store-parent-missing")
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ReservationCorrupt("reservation-store-parent-permissions-invalid")

    @staticmethod
    def _check_file(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > _MAX_STORE_BYTES
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ReservationCorrupt("reservation-store-file-shape-invalid")

    @staticmethod
    def _read_fd(fd: int, size: int) -> list[dict[str, Any]]:
        if size > _MAX_STORE_BYTES:
            raise ReservationCorrupt("reservation-store-size-limit")
        raw = os.pread(fd, size, 0)
        if len(raw) != size:
            raise ReservationCorrupt("reservation-store-short-read")
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise ReservationCorrupt("reservation-store-partial-tail")
        lines = raw.splitlines()
        if len(lines) > _MAX_EVENTS:
            raise ReservationCorrupt("reservation-store-event-limit")
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ReservationCorrupt("reservation-store-invalid-json") from exc
            if (
                not isinstance(row, dict)
                or type(row.get("schema_version")) is not int
                or row.get("schema_version") != 1
                or not isinstance(row.get("kind"), str)
                or row.get("kind") not in _EVENT_VALIDATORS
                or not isinstance(row.get("reservation_id"), str)
                or not row["reservation_id"]
                or type(row.get("sequence")) is not int
                or row.get("sequence") < 0
            ):
                raise ReservationCorrupt("reservation-store-invalid-record")
            _EVENT_VALIDATORS[row["kind"]](row)
            records.append(row)
        return records

    @staticmethod
    def _append_fd(fd: int, row: dict[str, Any], old_size: int) -> None:
        raw = _canonical_bytes(row) + b"\n"
        if old_size + len(raw) > _MAX_STORE_BYTES:
            raise ReservationCorrupt("reservation-store-size-limit")
        cursor = 0
        while cursor < len(raw):
            written = os.write(fd, raw[cursor:])
            if written <= 0:
                raise OSError("reservation store append made no progress")
            cursor += written
        os.fsync(fd)


# ---------------------------------------------------------------------------
# 事件形狀驗證（讀取時強制封閉——未知欄位／版本一律 fail closed）
# ---------------------------------------------------------------------------


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        dir_fd = os.open(path, flags)
    except OSError as exc:
        raise ReservationCorrupt("reservation-store-dir-fsync-failed") from exc
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        raise ReservationCorrupt("reservation-store-dir-fsync-failed") from exc
    finally:
        os.close(dir_fd)


def _require_keys(row: dict[str, Any], required: frozenset[str], optional: frozenset[str] = frozenset()) -> None:
    keys = set(row)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise ReservationCorrupt("reservation-store-invalid-record-shape")


_RESERVE_REQUIRED = frozenset(
    {
        "schema_version", "kind", "reservation_id", "sequence", "run_id", "card_id",
        "decision_id", "attempt_id", "owner_token", "pools", "capacity_by_pool",
        "observation_version", "demand_version", "lease_expires_at_ms", "created_at_ms",
        "event_at_ms",
    }
)
_BIND_REQUIRED = frozenset({"schema_version", "kind", "reservation_id", "sequence", "job_id", "event_at_ms"})
_SETTLE_REQUIRED = frozenset(
    {"schema_version", "kind", "reservation_id", "sequence", "outcome", "note", "event_at_ms"}
)
_RELEASE_REQUIRED = frozenset(
    {"schema_version", "kind", "reservation_id", "sequence", "reason", "event_at_ms"}
)
_RECONCILE_REQUIRED = frozenset(
    {
        "schema_version", "kind", "reservation_id", "sequence", "resolution", "evidence",
        "renew_lease_ms", "event_at_ms",
    }
)


def _validate_reserve_row(row: dict[str, Any]) -> None:
    _require_keys(row, _RESERVE_REQUIRED)
    if row["sequence"] != 0:
        raise ReservationCorrupt("reservation-store-reserve-sequence-nonzero")
    for field_name in ("run_id", "card_id", "decision_id", "attempt_id", "observation_version", "demand_version"):
        if not isinstance(row[field_name], str) or not row[field_name]:
            raise ReservationCorrupt("reservation-store-invalid-record")
    if not isinstance(row["owner_token"], str) or len(row["owner_token"]) != 32:
        raise ReservationCorrupt("reservation-store-invalid-record")
    if not isinstance(row["pools"], list) or not row["pools"]:
        raise ReservationCorrupt("reservation-store-invalid-record")
    for item in row["pools"]:
        if not isinstance(item, dict) or set(item) != {"pool_ref", "window_id", "amount"}:
            raise ReservationCorrupt("reservation-store-invalid-record")
        try:
            _validate_pool_ref(item["pool_ref"])
            _validate_amount(item["amount"])
        except ValueError as exc:
            raise ReservationCorrupt("reservation-store-invalid-record") from exc
        if not isinstance(item["window_id"], str) or not _ID_RE.fullmatch(item["window_id"]):
            raise ReservationCorrupt("reservation-store-invalid-record")
    capacity_rows = row["capacity_by_pool"]
    if not isinstance(capacity_rows, list) or len(capacity_rows) != len(row["pools"]):
        raise ReservationCorrupt("reservation-store-invalid-record")
    pool_keys = []
    for item in row["pools"]:
        pool_keys.append((tuple(item["pool_ref"][k] for k in _POOL_REF_KEYS), item["window_id"]))
    capacity_keys = []
    for item in capacity_rows:
        if not isinstance(item, dict) or set(item) != {"pool_ref", "window_id", "capacity"}:
            raise ReservationCorrupt("reservation-store-invalid-record")
        try:
            _validate_pool_ref(item["pool_ref"])
            _validate_amount(item["capacity"])
        except ValueError as exc:
            raise ReservationCorrupt("reservation-store-invalid-record") from exc
        if not isinstance(item["window_id"], str) or not _ID_RE.fullmatch(item["window_id"]):
            raise ReservationCorrupt("reservation-store-invalid-record")
        capacity_keys.append((tuple(item["pool_ref"][k] for k in _POOL_REF_KEYS), item["window_id"]))
    # pools 與 capacity_by_pool 必須逐一對應（同一組 pool／window、無重複）；
    # 損毀導致 pools 少一格時，遺失的容量不得被靜默重新 grant。
    if len(set(pool_keys)) != len(pool_keys) or sorted(pool_keys) != sorted(capacity_keys):
        raise ReservationCorrupt("reservation-store-invalid-record")
    for field_name in ("lease_expires_at_ms", "created_at_ms", "event_at_ms"):
        value = row[field_name]
        if type(value) is not int or value < 0 or value > _MAX_TIMESTAMP_MS:
            raise ReservationCorrupt("reservation-store-invalid-record")
    if row["lease_expires_at_ms"] < row["created_at_ms"]:
        raise ReservationCorrupt("reservation-store-invalid-record")


def _validate_bind_row(row: dict[str, Any]) -> None:
    _require_keys(row, _BIND_REQUIRED)
    if not isinstance(row["job_id"], str) or not row["job_id"]:
        raise ReservationCorrupt("reservation-store-invalid-record")


def _validate_settle_row(row: dict[str, Any]) -> None:
    _require_keys(row, _SETTLE_REQUIRED)
    if row["outcome"] not in _SETTLE_OUTCOMES:
        raise ReservationCorrupt("reservation-store-invalid-record")
    if row["note"] is not None and not isinstance(row["note"], str):
        raise ReservationCorrupt("reservation-store-invalid-record")


def _validate_release_row(row: dict[str, Any]) -> None:
    _require_keys(row, _RELEASE_REQUIRED)
    if row["reason"] not in _RELEASE_REASONS:
        raise ReservationCorrupt("reservation-store-invalid-record")


def _validate_reconcile_row(row: dict[str, Any]) -> None:
    _require_keys(row, _RECONCILE_REQUIRED)
    if row["resolution"] not in _RECONCILE_RESOLUTIONS:
        raise ReservationCorrupt("reservation-store-invalid-record")
    evidence = row["evidence"]
    # 與 reconcile() API 相同的 evidence 契約：必須有非空字串 kind 且有界。
    if (
        not isinstance(evidence, dict)
        or not isinstance(evidence.get("kind"), str)
        or not evidence.get("kind")
        or len(_canonical_bytes(evidence)) > 4096
    ):
        raise ReservationCorrupt("reservation-store-invalid-record")
    renew = row["renew_lease_ms"]
    if renew is not None and (type(renew) is not int or renew < 0 or renew > _MAX_LEASE_MS):
        raise ReservationCorrupt("reservation-store-invalid-record")


def _validate_transition_time(row: dict[str, Any]) -> None:
    """所有轉移事件的 event_at_ms／sequence 都必須是合法整數；壞檔案不得在
    折疊時噴裸 TypeError，而是一致地 ReservationCorrupt。"""
    event_at_ms = row.get("event_at_ms")
    if type(event_at_ms) is not int or event_at_ms < 0 or event_at_ms > _MAX_TIMESTAMP_MS:
        raise ReservationCorrupt("reservation-store-invalid-record")
    sequence = row.get("sequence")
    if type(sequence) is not int or sequence < 0:
        raise ReservationCorrupt("reservation-store-invalid-record")


# 折疊時允許的狀態轉移（與公開 API 的前置條件一致）：終局（settled／
# released）之後不得再有任何轉移；bind 只能從 reserved 發生。損毀但 shape
# 合法的歷史（例如 reserve→settle→bind）必須 fail-closed，不得復活已終局的
# reservation 並重新占用容量。
_ALLOWED_FROM_STATE: dict[str, frozenset[str]] = {
    "bind": frozenset({"reserved"}),
    "settle": frozenset({"bound"}),
    "release": frozenset({"reserved"}),
    "reconcile": frozenset({"reserved", "bound"}),
}


_EVENT_VALIDATORS: dict[str, Callable[[dict[str, Any]], None]] = {
    "reserve": _validate_reserve_row,
    "bind": _validate_bind_row,
    "settle": _validate_settle_row,
    "release": _validate_release_row,
    "reconcile": _validate_reconcile_row,
}


# ---------------------------------------------------------------------------
# 事件折疊（純函式：records → {reservation_id: 折疊後的邏輯狀態}）
# ---------------------------------------------------------------------------


def _fold(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    folded: dict[str, dict[str, Any]] = {}
    for row in records:
        reservation_id = row["reservation_id"]
        if row["kind"] == "reserve":
            if reservation_id in folded:
                raise ReservationCorrupt("reservation-store-duplicate-reservation-id")
            folded[reservation_id] = {
                "reservation_id": reservation_id,
                "sequence": 0,
                "state": "reserved",
                "run_id": row["run_id"],
                "card_id": row["card_id"],
                "decision_id": row["decision_id"],
                "attempt_id": row["attempt_id"],
                "owner_token": row["owner_token"],
                "job_id": None,
                "pools": row["pools"],
                "pools_signature": sorted((dict(item) for item in row["pools"]), key=_canonical_bytes),
                "observation_version": row["observation_version"],
                "demand_version": row["demand_version"],
                "lease_expires_at_ms": row["lease_expires_at_ms"],
                "created_at_ms": row["created_at_ms"],
                "last_event_at_ms": row["event_at_ms"],
                "settle_outcome": None,
                "release_reason": None,
                "last_reconcile_resolution": None,
            }
            continue
        current = folded.get(reservation_id)
        if current is None:
            raise ReservationCorrupt("reservation-store-orphan-transition")
        _validate_transition_time(row)
        if row["sequence"] != current["sequence"] + 1:
            raise ReservationCorrupt("reservation-store-sequence-gap")
        allowed = _ALLOWED_FROM_STATE.get(row["kind"])
        if allowed is None or current["state"] not in allowed:
            raise ReservationCorrupt("reservation-store-illegal-transition")
        current["sequence"] = row["sequence"]
        current["last_event_at_ms"] = row["event_at_ms"]
        if row["kind"] == "bind":
            current["state"] = "bound"
            current["job_id"] = row["job_id"]
        elif row["kind"] == "settle":
            current["state"] = "settled"
            current["settle_outcome"] = row["outcome"]
        elif row["kind"] == "release":
            current["state"] = "released"
            current["release_reason"] = row["reason"]
        elif row["kind"] == "reconcile":
            current["last_reconcile_resolution"] = row["resolution"]
            if row["resolution"] == "confirmed-terminated":
                current["state"] = "released"
                current["release_reason"] = "reconcile-confirmed-terminated"
            elif row["resolution"] == "confirmed-alive" and row.get("renew_lease_ms") is not None:
                current["lease_expires_at_ms"] = min(
                    row["event_at_ms"] + row["renew_lease_ms"], _MAX_TIMESTAMP_MS
                )
        else:  # pragma: no cover - _read_fd 已擋在更早的位置
            raise ReservationCorrupt("reservation-store-unknown-kind")
    return folded


def _display_state(entry: Mapping[str, Any], now_ms: int) -> str:
    """對外報告用的顯示狀態：邏輯狀態（``state``，決定是否持有容量）不受這裡
    影響——``committed()``／容量檢查一律只看 ``state``，``display_state`` 純粹
    是給人看的『這筆是否該被關注』標記。

    只要目前的 ``lease_expires_at_ms``（reconcile 帶 ``confirmed-alive`` ＋
    ``renew_lease_ms`` 可以把它延到未來）還沒過期，就照實回報邏輯狀態；一旦
    過期，一律回報 ``uncertain``——不論歷史上是否曾經 reconcile 過、給過什麼
    resolution。刻意不讓『曾經 confirmed-alive』這件事本身無限期壓下
    uncertain 標記：沒有配一次新的、真的延到未來的 lease，舊的確認不能背書
    『現在』還活著（見模組 docstring／#838 AC4：過期本身永不證明可以釋放，
    也不該讓一次陳舊的確認永久假裝『現在沒問題』）。"""
    state = entry["state"]
    if state in ("settled", "released"):
        return state
    if now_ms <= entry["lease_expires_at_ms"]:
        return state
    return "uncertain"


def _committed_totals(
    folded: Mapping[str, Mapping[str, Any]], now_ms: int
) -> dict[tuple[tuple[str, str, str, str], str], Decimal]:
    totals: dict[tuple[tuple[str, str, str, str], str], Decimal] = {}
    for entry in folded.values():
        if entry["state"] not in ("reserved", "bound"):
            continue
        for item in entry["pools"]:
            key = (tuple(item["pool_ref"][k] for k in _POOL_REF_KEYS), item["window_id"])
            totals[key] = totals.get(key, Decimal(0)) + Decimal(item["amount"])
    return totals


def _decimal_wire(value: Decimal) -> str:
    if value == 0:
        return "0"
    normalized = format(value.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
