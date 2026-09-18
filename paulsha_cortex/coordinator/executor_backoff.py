"""Durable, bounded executor/model backoff store.

#850 requires a single-module store that preserves exact executor/model
cooldowns across process restarts without pretending that an unreadable or
resource-exceeded store means "all clear". The module therefore keeps a
strict three-state read surface, an immutable event ledger keyed by terminal
job identity, and an atomic read-modify-write path guarded by a root-scoped
``flock``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Mapping

from .backoff import tick_backoff_seconds

STATE_FILENAME = "executor-backoff.json"
LOCK_FILENAME = "executor-backoff.lock"
SCHEMA = "executor-backoff/v1"
CAPACITY_PROFILE = "bounded-ledger/v1"
POLICY_REVISION = "executor-backoff/v1"
POLICY_BACKOFF_MULTIPLIER_BASE = 2.0
POLICY_BACKOFF_MAX_EXPONENT = 4

RESET_MARGIN_SECONDS = 5.0
RATE_LIMITED_BASE_SECONDS = 10.0
QUOTA_BASE_SECONDS = 40.0

LOCK_TIMEOUT_SECONDS = 1.0
LOCK_POLL_INTERVAL_SECONDS = 0.01

MAX_STORE_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 1 * 1024 * 1024
MAX_RETAINED_EVENTS = 1024
MAX_RETAINED_IDENTITIES = 128
MAX_EVENT_CANONICAL_BYTES = 8 * 1024
MAX_JSON_DEPTH = 12
MAX_STRING_BYTES = 4 * 1024
MAX_KEY_BYTES = 512
MAX_NUMERIC_TOKEN_CHARS = 64
MAX_STRUCTURAL_NODES = 131072
MAX_SORT_COMPARISONS = 65536
MAX_FOLD_EVENTS = 2048
MAX_BYTE_WORK = 16 * 1024 * 1024
COMPUTE_DEADLINE_SECONDS = 1.0

_monotonic: Callable[[], float] = time.monotonic
_time_now: Callable[[], float] = time.time
_TEST_HOOK: Callable[[str], None] | None = None

__all__ = [
    "STATE_FILENAME",
    "SCHEMA",
    "RESET_MARGIN_SECONDS",
    "RATE_LIMITED_BASE_SECONDS",
    "QUOTA_BASE_SECONDS",
    "StoreObservation",
    "ReconciliationStatus",
    "BackoffOutcome",
    "StoreRead",
    "ExecutorBackoff",
    "BackoffStatus",
    "BackoffMutationResult",
    "read_store",
    "active_backoff",
    "reconcile_backoff",
    "record_backoff",
    "clear_backoff",
]


class StoreObservation(str, Enum):
    MISSING = "missing"
    VALID = "valid"
    UNKNOWN = "unknown"


class ReconciliationStatus(str, Enum):
    UNVERIFIED = "unverified"
    PENDING = "pending"
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class BackoffOutcome(str, Enum):
    RATE_LIMITED = "rate_limited"
    QUOTA = "quota"


@dataclass(frozen=True)
class StoreRead:
    observation: StoreObservation
    payload: dict[str, object] | None = None
    diagnostics: tuple[str, ...] = ()
    last_good: dict[str, object] | None = None


@dataclass(frozen=True)
class ExecutorBackoff:
    executor: str
    model_id: str
    deadline_epoch: float
    consecutive_hits: int
    last_event_epoch: float
    last_terminal_key: str
    outcome: BackoffOutcome
    reason: str
    event_count: int


@dataclass(frozen=True)
class BackoffStatus:
    observation: StoreObservation
    backoff: ExecutorBackoff | None = None
    diagnostics: tuple[str, ...] = ()
    last_good: ExecutorBackoff | None = None
    reconciliation: ReconciliationStatus = ReconciliationStatus.UNVERIFIED
    missing_terminal_keys: tuple[str, ...] = ()
    conflicting_terminal_keys: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackoffMutationResult(BackoffStatus):
    changed: bool = False


@dataclass(frozen=True)
class _OutcomeEnvelope:
    outcome: BackoffOutcome
    authority: str
    payload: dict[str, object]
    payload_fingerprint: str
    reason: str
    policy_revision: str
    rate_limited_base_seconds: float
    quota_base_seconds: float
    backoff_multiplier_base: float
    backoff_max_exponent: int
    reset_margin_seconds: float
    reset_provenance: str
    reset_parser: dict[str, object] | None
    evidence_ref: str


class _StoreProblem(Exception):
    def __init__(self, *diagnostics: str, snapshot: "_StoreSnapshot | None" = None):
        self.diagnostics = tuple(diagnostics) or ("executor-backoff-store-unknown",)
        self.snapshot = snapshot
        super().__init__(self.diagnostics[0])


@dataclass(frozen=True)
class _StoredEvent:
    terminal_key: str
    event_epoch: float
    identity_key: str
    executor: str
    model_id: str
    outcome: BackoffOutcome
    authority: str
    payload: dict[str, object]
    payload_fingerprint: str
    reason: str
    reset_at: float | None
    policy_revision: str
    rate_limited_base_seconds: float
    quota_base_seconds: float
    backoff_multiplier_base: float
    backoff_max_exponent: int
    reset_margin_seconds: float
    reset_provenance: str
    reset_parser: dict[str, object] | None
    evidence_ref: str
    fingerprint: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            "terminal_key": self.terminal_key,
            "event_epoch": self.event_epoch,
            "identity_key": self.identity_key,
            "executor": self.executor,
            "model_id": self.model_id,
            "outcome": self.outcome.value,
            "authority": self.authority,
            "payload": self.payload,
            "payload_fingerprint": self.payload_fingerprint,
            "reason": self.reason,
            "reset_at": self.reset_at,
            "policy_revision": self.policy_revision,
            "rate_limited_base_seconds": self.rate_limited_base_seconds,
            "quota_base_seconds": self.quota_base_seconds,
            "backoff_multiplier_base": self.backoff_multiplier_base,
            "backoff_max_exponent": self.backoff_max_exponent,
            "reset_margin_seconds": self.reset_margin_seconds,
            "reset_provenance": self.reset_provenance,
            "reset_parser": self.reset_parser,
            "evidence_ref": self.evidence_ref,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.canonical_payload()
        payload["fingerprint"] = self.fingerprint
        return payload


@dataclass(frozen=True)
class _StoredAck:
    terminal_key: str
    event_epoch: float
    identity_key: str
    fingerprint: str

    def to_payload(self) -> dict[str, object]:
        return {
            "terminal_key": self.terminal_key,
            "event_epoch": self.event_epoch,
            "identity_key": self.identity_key,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class _StoredEntry:
    identity_key: str
    executor: str
    model_id: str
    deadline_epoch: float
    consecutive_hits: int
    last_event_epoch: float
    last_terminal_key: str
    outcome: BackoffOutcome
    reason: str
    event_count: int

    def to_payload(self) -> dict[str, object]:
        return {
            "executor": self.executor,
            "model_id": self.model_id,
            "deadline_epoch": self.deadline_epoch,
            "consecutive_hits": self.consecutive_hits,
            "last_event_epoch": self.last_event_epoch,
            "last_terminal_key": self.last_terminal_key,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "event_count": self.event_count,
        }

    def to_public(self) -> ExecutorBackoff:
        return ExecutorBackoff(
            executor=self.executor,
            model_id=self.model_id,
            deadline_epoch=self.deadline_epoch,
            consecutive_hits=self.consecutive_hits,
            last_event_epoch=self.last_event_epoch,
            last_terminal_key=self.last_terminal_key,
            outcome=self.outcome,
            reason=self.reason,
            event_count=self.event_count,
        )


@dataclass(frozen=True)
class _StoreSnapshot:
    scope: str
    entries: dict[str, _StoredEntry]
    events: dict[str, _StoredEvent]
    acks: dict[str, _StoredAck]

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "capacity_profile": CAPACITY_PROFILE,
            "scope": self.scope,
            "entries": {
                key: value.to_payload() for key, value in sorted(self.entries.items())
            },
            "events": {
                key: value.to_payload() for key, value in sorted(self.events.items())
            },
            "acks": {key: value.to_payload() for key, value in sorted(self.acks.items())},
        }

    def active_for(self, identity_key: str, *, now: float) -> ExecutorBackoff | None:
        entry = self.entries.get(identity_key)
        if entry is None or entry.deadline_epoch <= now:
            return None
        return entry.to_public()

    def entry_for(self, identity_key: str) -> _StoredEntry | None:
        return self.entries.get(identity_key)


@dataclass(frozen=True)
class _SnapshotRead:
    read: StoreRead
    snapshot: _StoreSnapshot | None = None


class _ComputeBudget:
    def __init__(self) -> None:
        self._started = _monotonic()
        self._byte_work = 0

    def checkpoint(self) -> None:
        if (_monotonic() - self._started) > COMPUTE_DEADLINE_SECONDS:
            raise _StoreProblem("computation-budget-exceeded")

    def note_bytes(self, count: int) -> None:
        self._byte_work += count
        if self._byte_work > MAX_BYTE_WORK:
            raise _StoreProblem("computation-budget-exceeded")
        self.checkpoint()


def _state_path(coordinator_root: str | Path) -> Path:
    return Path(coordinator_root) / STATE_FILENAME


def _resolve_path(path: str | Path, *, diagnostic_name: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise _StoreProblem(f"invalid-{diagnostic_name}") from error
    # Python 3.13 起 ``Path.resolve(strict=False)`` 遇 symlink 迴圈不再拋例外
    # （≤3.12 拋 ``RuntimeError``），錯誤會晚到 ``lstat`` 才以 ELOOP 冒出，診斷
    # 標籤因此隨版本漂移。這裡以 ``realpath(strict=True)`` 顯式偵測迴圈與其他
    # 走訪錯誤（3.10+ 語意一致），缺檔元件維持 strict=False 的放行語意。
    try:
        os.path.realpath(path, strict=True)
    except FileNotFoundError:
        pass
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise _StoreProblem(f"invalid-{diagnostic_name}") from error
    return resolved


def _lock_path(coordinator_root: str | Path) -> Path:
    root = _resolve_path(coordinator_root, diagnostic_name="coordinator-root")
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    filename = f".{digest}.{LOCK_FILENAME}"
    return _stable_lock_base(root) / filename


def _stable_lock_base(coordinator_root: Path) -> Path:
    parent = coordinator_root.parent
    ancestors = (parent, *parent.parents)
    for ancestor in reversed(ancestors):
        if ancestor.exists() and os.access(ancestor, os.W_OK | os.X_OK):
            return ancestor
    for ancestor in reversed(ancestors):
        if ancestor.exists():
            return ancestor
    return _resolve_path(Path.cwd(), diagnostic_name="cwd")


def _unknown_store(*diagnostics: str, last_good: dict[str, object] | None = None) -> StoreRead:
    return StoreRead(
        observation=StoreObservation.UNKNOWN,
        diagnostics=tuple(diagnostics),
        last_good=last_good,
    )


def _unknown_status(
    *diagnostics: str,
    last_good: ExecutorBackoff | None = None,
) -> BackoffStatus:
    return BackoffStatus(
        observation=StoreObservation.UNKNOWN,
        diagnostics=tuple(diagnostics),
        last_good=last_good,
        reconciliation=ReconciliationStatus.UNVERIFIED,
    )


def _unknown_mutation(
    *diagnostics: str,
    last_good: ExecutorBackoff | None = None,
) -> BackoffMutationResult:
    return BackoffMutationResult(
        observation=StoreObservation.UNKNOWN,
        diagnostics=tuple(diagnostics),
        last_good=last_good,
        reconciliation=ReconciliationStatus.UNVERIFIED,
        changed=False,
    )


def _identity_key(executor: str, model_id: str) -> str:
    _require_non_empty_string(executor, "executor")
    _require_non_empty_string(model_id, "model_id")
    if "/" in executor or "/" in model_id:
        raise _StoreProblem("invalid-identity")
    identity_key = f"{executor}/{model_id}"
    if len(identity_key.encode("utf-8")) > MAX_KEY_BYTES:
        raise _StoreProblem("identity-key-too-large")
    return identity_key


def _require_non_empty_string(value: object, name: str, *, max_bytes: int = MAX_STRING_BYTES) -> str:
    if type(value) is not str or not value:
        raise _StoreProblem(f"invalid-{name}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _StoreProblem(f"invalid-{name}") from error
    if len(encoded) > max_bytes:
        raise _StoreProblem(f"{name}-too-large")
    return value


def _require_key_string(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise _StoreProblem(f"invalid-{name}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _StoreProblem(f"invalid-{name}") from error
    if len(encoded) > MAX_KEY_BYTES:
        raise _StoreProblem(f"{name}-too-large")
    return value


def _require_epoch(value: object, name: str) -> float:
    if type(value) not in (int, float):
        raise _StoreProblem(f"invalid-{name}")
    try:
        candidate = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise _StoreProblem(f"invalid-{name}") from error
    if not math.isfinite(candidate):
        raise _StoreProblem(f"invalid-{name}")
    return candidate


def _require_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise _StoreProblem(f"invalid-{name}")
    return value


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if type(value) is not dict:
        raise _StoreProblem(f"invalid-{name}")
    keys = value.keys()
    if not all(isinstance(key, str) for key in keys):
        raise _StoreProblem(f"invalid-{name}")
    return value


def _require_fingerprint(value: object, name: str) -> str:
    fingerprint = _require_non_empty_string(value, name, max_bytes=128)
    if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        raise _StoreProblem(f"invalid-{name}")
    return fingerprint


def _normalize_authority(value: object) -> str:
    authority = _require_non_empty_string(value, "authority")
    if authority not in {"structured", "text_signal"}:
        raise _StoreProblem("invalid-authority")
    return authority


def _validate_input_json_value(
    value: object,
    *,
    budget: _ComputeBudget,
    depth: int = 0,
    active: set[int] | None = None,
    node_counter: list[int] | None = None,
) -> object:
    if depth > MAX_JSON_DEPTH:
        raise _StoreProblem("json-depth-exceeded")
    budget.checkpoint()
    if node_counter is None:
        node_counter = [0]
    node_counter[0] += 1
    if node_counter[0] > MAX_STRUCTURAL_NODES:
        raise _StoreProblem("json-node-limit-exceeded")
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise _StoreProblem("invalid-payload") from error
        if len(encoded) > MAX_STRING_BYTES:
            raise _StoreProblem("json-string-too-large")
        return value
    if type(value) in (int, float):
        numeric = _require_epoch(value, "json-number")
        if len(str(value)) > MAX_NUMERIC_TOKEN_CHARS:
            raise _StoreProblem("json-number-token-too-large")
        return int(numeric) if isinstance(value, int) else numeric
    if active is None:
        active = set()
    if type(value) is dict:
        marker = id(value)
        if marker in active:
            raise _StoreProblem("invalid-payload")
        active.add(marker)
        try:
            for key, child in value.items():
                _require_key_string(key, "json-key")
                _validate_input_json_value(
                    child,
                    budget=budget,
                    depth=depth + 1,
                    active=active,
                    node_counter=node_counter,
                )
            return value
        finally:
            active.remove(marker)
    if type(value) is list:
        marker = id(value)
        if marker in active:
            raise _StoreProblem("invalid-payload")
        active.add(marker)
        try:
            for child in value:
                _validate_input_json_value(
                    child,
                    budget=budget,
                    depth=depth + 1,
                    active=active,
                    node_counter=node_counter,
                )
            return value
        finally:
            active.remove(marker)
    raise _StoreProblem("invalid-payload")


def _normalize_reset_parser(value: object, *, provenance: str) -> dict[str, object] | None:
    if provenance != "parsed":
        if value is not None:
            raise _StoreProblem("invalid-reset_parser")
        return None
    parser = _require_mapping(value, "reset_parser")
    if set(parser) != {"rule_version", "timezone", "base_year", "base_reference_epoch"}:
        raise _StoreProblem("invalid-reset_parser")
    base_year = _require_int(parser.get("base_year"), "base_year")
    if len(str(base_year)) > MAX_NUMERIC_TOKEN_CHARS:
        raise _StoreProblem("json-number-token-too-large")
    return {
        "rule_version": _require_non_empty_string(parser.get("rule_version"), "rule_version"),
        "timezone": _require_non_empty_string(parser.get("timezone"), "timezone"),
        "base_year": base_year,
        "base_reference_epoch": _require_epoch(
            parser.get("base_reference_epoch"), "base_reference_epoch"
        ),
    }


def _normalize_outcome(value: object) -> BackoffOutcome:
    if isinstance(value, BackoffOutcome):
        return value
    if isinstance(value, str):
        try:
            return BackoffOutcome(value)
        except ValueError as error:
            raise _StoreProblem("unsupported-outcome") from error
    try:
        raw = getattr(value, "value", None)
    except Exception as error:
        raise _StoreProblem("unsupported-outcome") from error
    if isinstance(raw, str):
        return _normalize_outcome(raw)
    raise _StoreProblem("unsupported-outcome")


def _normalize_reset_provenance(value: object) -> str:
    provenance = _require_non_empty_string(value, "reset_provenance")
    if provenance not in {"structured", "parsed", "absent"}:
        raise _StoreProblem("invalid-reset-provenance")
    return provenance


def _expected_retryable(outcome: BackoffOutcome, authority: str) -> bool:
    return authority != "hint" and outcome is BackoffOutcome.RATE_LIMITED


def _normalize_outcome_envelope(
    value: object,
    *,
    reason: str,
    reset_at: float | None,
    budget: _ComputeBudget,
) -> _OutcomeEnvelope:
    mapping = _require_mapping(value, "outcome-envelope")
    expected_keys = {
        "outcome",
        "authority",
        "payload",
        "payload_fingerprint",
        "reason",
        "policy_revision",
        "rate_limited_base_seconds",
        "quota_base_seconds",
        "backoff_multiplier_base",
        "backoff_max_exponent",
        "reset_margin_seconds",
        "reset_provenance",
        "reset_parser",
        "evidence_ref",
    }
    if set(mapping) != expected_keys:
        raise _StoreProblem("invalid-outcome-envelope")
    normalized_outcome = _normalize_outcome(mapping.get("outcome"))
    authority = _normalize_authority(mapping.get("authority"))
    payload = _validate_input_json_value(
        _require_mapping(mapping.get("payload"), "payload"),
        budget=budget,
    )
    payload_keys = set(payload)
    if payload_keys not in (
        {"outcome", "authority", "reason", "retryable"},
        {"outcome", "authority", "reason", "retryable", "reset_at"},
    ):
        raise _StoreProblem("invalid-payload")
    payload_body = _bounded_canonical_json_bytes(
        payload,
        budget=budget,
        max_bytes=MAX_EVENT_CANONICAL_BYTES,
        overflow_diagnostic="payload-too-large",
    )
    payload_fingerprint = _require_fingerprint(
        mapping.get("payload_fingerprint"), "payload_fingerprint"
    )
    expected_fingerprint = hashlib.sha256(payload_body).hexdigest()
    if payload_fingerprint != expected_fingerprint:
        raise _StoreProblem("payload-fingerprint-mismatch")
    immutable_payload = json.loads(
        payload_body.decode("utf-8"),
        parse_constant=_reject_constants,
        object_pairs_hook=_reject_duplicate_object_pairs,
    )
    envelope_reason = _require_non_empty_string(mapping.get("reason"), "reason")
    if envelope_reason != reason:
        raise _StoreProblem("reason-mismatch")
    policy_revision = _require_non_empty_string(mapping.get("policy_revision"), "policy_revision")
    rate_limited_base_seconds = _require_epoch(
        mapping.get("rate_limited_base_seconds"), "rate_limited_base_seconds"
    )
    quota_base_seconds = _require_epoch(
        mapping.get("quota_base_seconds"), "quota_base_seconds"
    )
    backoff_multiplier_base = _require_epoch(
        mapping.get("backoff_multiplier_base"), "backoff_multiplier_base"
    )
    backoff_max_exponent = _require_int(
        mapping.get("backoff_max_exponent"), "backoff_max_exponent"
    )
    reset_margin_seconds = _require_epoch(
        mapping.get("reset_margin_seconds"), "reset_margin_seconds"
    )
    if (
        policy_revision != POLICY_REVISION
        or rate_limited_base_seconds != RATE_LIMITED_BASE_SECONDS
        or quota_base_seconds != QUOTA_BASE_SECONDS
        or backoff_multiplier_base != POLICY_BACKOFF_MULTIPLIER_BASE
        or backoff_max_exponent != POLICY_BACKOFF_MAX_EXPONENT
        or reset_margin_seconds != RESET_MARGIN_SECONDS
        or quota_base_seconds <= rate_limited_base_seconds
    ):
        raise _StoreProblem("unknown-policy")
    envelope_reset_value = payload.get("reset_at")
    if reset_at is None:
        if envelope_reset_value is not None:
            raise _StoreProblem("reset-at-mismatch")
    else:
        if _require_epoch(envelope_reset_value, "reset_at") != reset_at:
            raise _StoreProblem("reset-at-mismatch")
    payload_outcome = payload.get("outcome")
    if payload_outcome != normalized_outcome.value:
        raise _StoreProblem("payload-outcome-mismatch")
    payload_authority = payload.get("authority")
    if payload_authority != authority:
        raise _StoreProblem("payload-authority-mismatch")
    payload_reason = payload.get("reason")
    if payload_reason != reason:
        raise _StoreProblem("payload-reason-mismatch")
    payload_retryable = payload.get("retryable")
    if not isinstance(payload_retryable, bool):
        raise _StoreProblem("invalid-payload")
    if payload_retryable is not _expected_retryable(normalized_outcome, authority):
        raise _StoreProblem("payload-retryable-mismatch")
    reset_provenance = _normalize_reset_provenance(mapping.get("reset_provenance"))
    if reset_at is None and reset_provenance != "absent":
        raise _StoreProblem("invalid-reset-provenance")
    if reset_at is not None and reset_provenance == "absent":
        raise _StoreProblem("invalid-reset-provenance")
    reset_parser = _normalize_reset_parser(mapping.get("reset_parser"), provenance=reset_provenance)
    if (
        reset_provenance == "parsed"
        and reset_at is not None
        and reset_parser is not None
        and reset_at < float(reset_parser["base_reference_epoch"])
    ):
        raise _StoreProblem("reset-at-before-base-reference")
    evidence_ref = _require_non_empty_string(mapping.get("evidence_ref"), "evidence_ref")
    return _OutcomeEnvelope(
        outcome=normalized_outcome,
        authority=authority,
        payload=immutable_payload,
        payload_fingerprint=payload_fingerprint,
        reason=envelope_reason,
        policy_revision=policy_revision,
        rate_limited_base_seconds=rate_limited_base_seconds,
        quota_base_seconds=quota_base_seconds,
        backoff_multiplier_base=backoff_multiplier_base,
        backoff_max_exponent=backoff_max_exponent,
        reset_margin_seconds=reset_margin_seconds,
        reset_provenance=reset_provenance,
        reset_parser=reset_parser,
        evidence_ref=evidence_ref,
    )


def _base_seconds(outcome: BackoffOutcome, *, event: _StoredEvent | None = None) -> float:
    if event is not None:
        if outcome is BackoffOutcome.RATE_LIMITED:
            return event.rate_limited_base_seconds
        return event.quota_base_seconds
    if outcome is BackoffOutcome.RATE_LIMITED:
        return RATE_LIMITED_BASE_SECONDS
    return QUOTA_BASE_SECONDS


def _canonical_json_bytes(payload: object, *, budget: _ComputeBudget | None = None) -> bytes:
    return _bounded_canonical_json_bytes(payload, budget=budget)


def _bounded_canonical_json_bytes(
    payload: object,
    *,
    budget: _ComputeBudget | None = None,
    max_bytes: int | None = None,
    overflow_diagnostic: str = "capacity-exceeded",
) -> bytes:
    encoder = json.JSONEncoder(
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    parts: list[bytes] = []
    total = 0
    for chunk in encoder.iterencode(payload):
        encoded = chunk.encode("utf-8")
        total += len(encoded)
        if max_bytes is not None and total > max_bytes:
            raise _StoreProblem(overflow_diagnostic)
        if budget is not None:
            budget.note_bytes(len(encoded))
        parts.append(encoded)
    return b"".join(parts)


def _event_from_inputs(
    *,
    executor: str,
    model_id: str,
    outcome: object,
    reset_at: float | None,
    reason: str,
    job_id: str,
    event_epoch: float,
    budget: _ComputeBudget,
) -> _StoredEvent:
    identity_key = _identity_key(executor, model_id)
    terminal_key = _require_key_string(job_id, "job_id")
    canonical_event_epoch = _require_epoch(event_epoch, "event_epoch")
    canonical_reason = _require_non_empty_string(reason, "reason")
    canonical_reset = None if reset_at is None else _require_epoch(reset_at, "reset_at")
    if canonical_reset is not None and (canonical_reset + RESET_MARGIN_SECONDS) <= canonical_event_epoch:
        raise _StoreProblem("stale-reset-at")
    envelope = _normalize_outcome_envelope(
        outcome,
        reason=canonical_reason,
        reset_at=canonical_reset,
        budget=budget,
    )
    policy_payload = {
        "policy_revision": envelope.policy_revision,
        "rate_limited_base_seconds": envelope.rate_limited_base_seconds,
        "quota_base_seconds": envelope.quota_base_seconds,
        "backoff_multiplier_base": envelope.backoff_multiplier_base,
        "backoff_max_exponent": envelope.backoff_max_exponent,
        "reset_margin_seconds": envelope.reset_margin_seconds,
        "reset_provenance": envelope.reset_provenance,
    }
    payload = {
        "terminal_key": terminal_key,
        "event_epoch": canonical_event_epoch,
        "identity_key": identity_key,
        "executor": executor,
        "model_id": model_id,
        "outcome": envelope.outcome.value,
        "authority": envelope.authority,
        "payload": envelope.payload,
        "payload_fingerprint": envelope.payload_fingerprint,
        "reason": canonical_reason,
        "reset_at": canonical_reset,
        "reset_parser": envelope.reset_parser,
        "evidence_ref": envelope.evidence_ref,
        **policy_payload,
    }
    body = _bounded_canonical_json_bytes(
        payload,
        budget=budget,
        max_bytes=MAX_EVENT_CANONICAL_BYTES,
        overflow_diagnostic="event-too-large",
    )
    fingerprint = hashlib.sha256(body).hexdigest()
    event = _StoredEvent(
        terminal_key=terminal_key,
        event_epoch=canonical_event_epoch,
        identity_key=identity_key,
        executor=executor,
        model_id=model_id,
        outcome=envelope.outcome,
        authority=envelope.authority,
        payload=envelope.payload,
        payload_fingerprint=envelope.payload_fingerprint,
        reason=envelope.reason,
        reset_at=canonical_reset,
        policy_revision=envelope.policy_revision,
        rate_limited_base_seconds=envelope.rate_limited_base_seconds,
        quota_base_seconds=envelope.quota_base_seconds,
        backoff_multiplier_base=envelope.backoff_multiplier_base,
        backoff_max_exponent=envelope.backoff_max_exponent,
        reset_margin_seconds=envelope.reset_margin_seconds,
        reset_provenance=envelope.reset_provenance,
        reset_parser=envelope.reset_parser,
        evidence_ref=envelope.evidence_ref,
        fingerprint=fingerprint,
    )
    _bounded_canonical_json_bytes(
        event.to_payload(),
        budget=budget,
        max_bytes=MAX_EVENT_CANONICAL_BYTES,
        overflow_diagnostic="event-too-large",
    )
    return event


def _verified_tick_backoff_seconds(base_interval: float, consecutive_hits: int) -> float:
    observed = tick_backoff_seconds(base_interval, consecutive_hits)
    if consecutive_hits <= 0:
        expected = base_interval
    else:
        exponent = min(consecutive_hits, POLICY_BACKOFF_MAX_EXPONENT)
        expected = base_interval * (POLICY_BACKOFF_MULTIPLIER_BASE**exponent)
    if not math.isfinite(observed) or not math.isclose(observed, expected, rel_tol=0.0, abs_tol=0.0):
        raise _StoreProblem("unknown-policy")
    return observed


def _read_bounded_bytes(path: Path, *, budget: _ComputeBudget) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise _StoreProblem(f"unable to open executor backoff store: {error}") from error
    try:
        try:
            info = os.fstat(fd)
        except OSError as error:
            raise _StoreProblem(f"unable to stat executor backoff store fd: {error}") from error
        if not stat.S_ISREG(info.st_mode):
            raise _StoreProblem("executor backoff store must be a regular file")
        expected_size = info.st_size
        if expected_size > MAX_STORE_BYTES:
            raise _StoreProblem("store-too-large")
        chunks: list[bytes] = []
        total = 0
        limit = MAX_STORE_BYTES + 1
        while total < limit:
            budget.checkpoint()
            try:
                chunk = os.read(fd, min(65536, limit - total))
            except OSError as error:
                raise _StoreProblem(f"unable to read executor backoff store: {error}") from error
            if not chunk:
                break
            total += len(chunk)
            budget.note_bytes(len(chunk))
            chunks.append(chunk)
        try:
            final_info = os.fstat(fd)
        except OSError as error:
            raise _StoreProblem(f"unable to stat executor backoff store fd: {error}") from error
        if final_info.st_size != expected_size or total != expected_size:
            raise _StoreProblem("store-changed-during-read")
        if total > MAX_STORE_BYTES:
            raise _StoreProblem("store-too-large")
        return b"".join(chunks)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _lexically_validate_json(raw: bytes, *, budget: _ComputeBudget) -> None:
    whitespace = {9, 10, 13, 32}
    delimiters = whitespace | {44, 58, 93, 125}
    depth = 0
    nodes = 0
    in_string = False
    escaped = False
    string_bytes = 0
    index = 0
    while index < len(raw):
        budget.checkpoint()
        value = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                in_string = False
            else:
                string_bytes += 1
                if string_bytes > MAX_STRING_BYTES:
                    raise _StoreProblem("json-string-too-large")
            index += 1
            continue
        if value in whitespace:
            index += 1
            continue
        nodes += 1
        if nodes > MAX_STRUCTURAL_NODES:
            raise _StoreProblem("json-node-limit-exceeded")
        if value == 34:
            in_string = True
            string_bytes = 0
            index += 1
            continue
        if value in (123, 91):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise _StoreProblem("json-depth-exceeded")
            index += 1
            continue
        if value in (125, 93):
            depth -= 1
            if depth < 0:
                raise _StoreProblem("executor backoff store has unbalanced JSON structure")
            index += 1
            continue
        if value in (44, 58):
            index += 1
            continue
        if value == 45 or 48 <= value <= 57:
            number_chars = 1
            index += 1
            while index < len(raw) and raw[index] not in delimiters:
                number_chars += 1
                if number_chars > MAX_NUMERIC_TOKEN_CHARS:
                    raise _StoreProblem("json-number-token-too-large")
                index += 1
            continue
        if value in (116, 102, 110):
            index += 1
            while index < len(raw) and 97 <= raw[index] <= 122:
                index += 1
            continue
        index += 1
    if in_string:
        raise _StoreProblem("executor backoff store contains an unterminated string")
    if depth != 0:
        raise _StoreProblem("executor backoff store has unbalanced JSON structure")


def _reject_constants(value: str) -> object:
    raise _StoreProblem(f"invalid-json-constant:{value}")


def _reject_duplicate_object_pairs(pairs: list[tuple[object, object]]) -> dict[object, object]:
    payload: dict[object, object] = {}
    for key, value in pairs:
        if key in payload:
            raise _StoreProblem(f"duplicate-json-key:{key}")
        payload[key] = value
    return payload


def _strict_json_loads(raw: bytes, *, budget: _ComputeBudget) -> object:
    _lexically_validate_json(raw, budget=budget)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _StoreProblem(f"executor backoff store is not valid UTF-8: {error}") from error
    budget.note_bytes(len(raw))
    try:
        return json.loads(
            text,
            parse_constant=_reject_constants,
            object_pairs_hook=_reject_duplicate_object_pairs,
        )
    except _StoreProblem:
        raise
    except json.JSONDecodeError as error:
        raise _StoreProblem(f"executor backoff store is not valid JSON: {error}") from error


def _count_nodes(value: object, *, budget: _ComputeBudget) -> int:
    stack = [value]
    total = 0
    while stack:
        budget.checkpoint()
        current = stack.pop()
        total += 1
        if total > MAX_STRUCTURAL_NODES:
            raise _StoreProblem("json-node-limit-exceeded")
        if isinstance(current, Mapping):
            for key, child in current.items():
                _require_key_string(key, "json-key")
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)
    return total


def _group_events_by_identity(
    events: Mapping[str, _StoredEvent] | list[_StoredEvent],
    *,
    budget: _ComputeBudget,
) -> dict[str, list[_StoredEvent]]:
    grouped: dict[str, list[_StoredEvent]] = {}
    iterable = events.values() if isinstance(events, Mapping) else events
    for event in iterable:
        budget.checkpoint()
        grouped.setdefault(event.identity_key, []).append(event)
    return grouped


def _sorted_identity_events(events: list[_StoredEvent], *, budget: _ComputeBudget) -> list[_StoredEvent]:
    if len(events) > MAX_FOLD_EVENTS:
        raise _StoreProblem("capacity-exceeded")
    comparisons = 0 if len(events) < 2 else len(events) * math.ceil(math.log2(len(events)))
    if comparisons > MAX_SORT_COMPARISONS:
        raise _StoreProblem("computation-budget-exceeded")
    budget.checkpoint()
    return sorted(events, key=lambda event: (event.event_epoch, event.terminal_key))


def _fold_events(
    identity_key: str,
    events: list[_StoredEvent],
    *,
    budget: _ComputeBudget,
) -> _StoredEntry:
    ordered = _sorted_identity_events(events, budget=budget)
    if not ordered:
        raise _StoreProblem("missing-events-for-identity")
    deadline: float | None = None
    hits = 0
    for event in ordered:
        budget.checkpoint()
        if deadline is None or event.event_epoch >= deadline:
            hits = 1
        else:
            hits += 1
        if event.reset_at is not None:
            candidate_deadline = event.reset_at + event.reset_margin_seconds
        else:
            candidate_deadline = event.event_epoch + _verified_tick_backoff_seconds(
                _base_seconds(event.outcome, event=event),
                hits,
            )
        if not math.isfinite(candidate_deadline):
            raise _StoreProblem("invalid-deadline")
        deadline = candidate_deadline if deadline is None else max(deadline, candidate_deadline)
    last_event = ordered[-1]
    assert deadline is not None
    return _StoredEntry(
        identity_key=identity_key,
        executor=last_event.executor,
        model_id=last_event.model_id,
        deadline_epoch=deadline,
        consecutive_hits=hits,
        last_event_epoch=last_event.event_epoch,
        last_terminal_key=last_event.terminal_key,
        outcome=last_event.outcome,
        reason=last_event.reason,
        event_count=len(ordered),
    )


def _fold_entry(identity_key: str, events: dict[str, _StoredEvent], *, budget: _ComputeBudget) -> _StoredEntry:
    grouped = _group_events_by_identity(events, budget=budget)
    return _fold_events(identity_key, grouped.get(identity_key, []), budget=budget)


def _conservative_entry_floor(
    persisted: _StoredEntry,
    recomputed: _StoredEntry,
) -> _StoredEntry:
    if persisted.deadline_epoch <= recomputed.deadline_epoch:
        return recomputed
    return _StoredEntry(
        identity_key=recomputed.identity_key,
        executor=recomputed.executor,
        model_id=recomputed.model_id,
        deadline_epoch=persisted.deadline_epoch,
        consecutive_hits=recomputed.consecutive_hits,
        last_event_epoch=recomputed.last_event_epoch,
        last_terminal_key=recomputed.last_terminal_key,
        outcome=recomputed.outcome,
        reason=recomputed.reason,
        event_count=recomputed.event_count,
    )


def _snapshot_from_payload(
    payload: object,
    *,
    budget: _ComputeBudget,
    expected_scope: str,
) -> _StoreSnapshot:
    _count_nodes(payload, budget=budget)
    if not isinstance(payload, dict):
        raise _StoreProblem(
            "executor backoff store payload must be a JSON object, "
            f"got {type(payload).__name__}"
        )
    expected_top_level = {"schema", "capacity_profile", "scope", "entries", "events", "acks"}
    if set(payload) != expected_top_level:
        raise _StoreProblem("invalid-store-shape")
    schema = payload.get("schema")
    if schema != SCHEMA:
        raise _StoreProblem(f"unknown-schema:{schema}")
    capacity_profile = payload.get("capacity_profile")
    if capacity_profile != CAPACITY_PROFILE:
        raise _StoreProblem(f"unknown-capacity-profile:{capacity_profile}")
    scope = _require_non_empty_string(payload.get("scope"), "scope")
    if scope != expected_scope:
        raise _StoreProblem("scope-mismatch")
    raw_entries = payload.get("entries")
    raw_events = payload.get("events")
    raw_acks = payload.get("acks")
    if not isinstance(raw_entries, dict) or not isinstance(raw_events, dict) or not isinstance(raw_acks, dict):
        raise _StoreProblem("invalid-store-shape")
    if len(raw_events) > MAX_RETAINED_EVENTS or len(raw_acks) > MAX_RETAINED_EVENTS:
        raise _StoreProblem("capacity-exceeded")

    events: dict[str, _StoredEvent] = {}
    for raw_terminal_key, raw_event in raw_events.items():
        terminal_key = _require_key_string(raw_terminal_key, "terminal-key")
        if not isinstance(raw_event, dict):
            raise _StoreProblem("invalid-event-payload")
        expected_event_keys = {
            "terminal_key",
            "event_epoch",
            "identity_key",
            "executor",
            "model_id",
            "outcome",
            "authority",
            "payload",
            "payload_fingerprint",
            "reason",
            "reset_at",
            "policy_revision",
            "rate_limited_base_seconds",
            "quota_base_seconds",
            "backoff_multiplier_base",
            "backoff_max_exponent",
            "reset_margin_seconds",
            "reset_provenance",
            "reset_parser",
            "evidence_ref",
            "fingerprint",
        }
        if set(raw_event) != expected_event_keys:
            raise _StoreProblem("invalid-event-payload")
        event = _StoredEvent(
            terminal_key=_require_key_string(raw_event.get("terminal_key"), "terminal-key"),
            event_epoch=_require_epoch(raw_event.get("event_epoch"), "event_epoch"),
            identity_key=_require_key_string(raw_event.get("identity_key"), "identity_key"),
            executor=_require_non_empty_string(raw_event.get("executor"), "executor"),
            model_id=_require_non_empty_string(raw_event.get("model_id"), "model_id"),
            outcome=_normalize_outcome(raw_event.get("outcome")),
            authority=_normalize_authority(raw_event.get("authority")),
            payload=_validate_input_json_value(
                _require_mapping(raw_event.get("payload"), "payload"),
                budget=budget,
            ),
            payload_fingerprint=_require_fingerprint(
                raw_event.get("payload_fingerprint"), "payload_fingerprint"
            ),
            reason=_require_non_empty_string(raw_event.get("reason"), "reason"),
            reset_at=(
                None
                if raw_event.get("reset_at") is None
                else _require_epoch(raw_event.get("reset_at"), "reset_at")
            ),
            policy_revision=_require_non_empty_string(
                raw_event.get("policy_revision"), "policy_revision"
            ),
            rate_limited_base_seconds=_require_epoch(
                raw_event.get("rate_limited_base_seconds"), "rate_limited_base_seconds"
            ),
            quota_base_seconds=_require_epoch(
                raw_event.get("quota_base_seconds"), "quota_base_seconds"
            ),
            backoff_multiplier_base=_require_epoch(
                raw_event.get("backoff_multiplier_base"), "backoff_multiplier_base"
            ),
            backoff_max_exponent=_require_int(
                raw_event.get("backoff_max_exponent"), "backoff_max_exponent"
            ),
            reset_margin_seconds=_require_epoch(
                raw_event.get("reset_margin_seconds"), "reset_margin_seconds"
            ),
            reset_provenance=_normalize_reset_provenance(
                raw_event.get("reset_provenance")
            ),
            reset_parser=_normalize_reset_parser(
                raw_event.get("reset_parser"),
                provenance=_normalize_reset_provenance(raw_event.get("reset_provenance")),
            ),
            evidence_ref=_require_non_empty_string(raw_event.get("evidence_ref"), "evidence_ref"),
            fingerprint=_require_non_empty_string(
                raw_event.get("fingerprint"),
                "fingerprint",
                max_bytes=128,
            ),
        )
        if event.terminal_key != terminal_key:
            raise _StoreProblem("event-terminal-key-mismatch")
        if event.identity_key != _identity_key(event.executor, event.model_id):
            raise _StoreProblem("event-identity-mismatch")
        if (
            event.policy_revision != POLICY_REVISION
            or event.rate_limited_base_seconds != RATE_LIMITED_BASE_SECONDS
            or event.quota_base_seconds != QUOTA_BASE_SECONDS
            or event.backoff_multiplier_base != POLICY_BACKOFF_MULTIPLIER_BASE
            or event.backoff_max_exponent != POLICY_BACKOFF_MAX_EXPONENT
            or event.reset_margin_seconds != RESET_MARGIN_SECONDS
            or event.quota_base_seconds <= event.rate_limited_base_seconds
        ):
            raise _StoreProblem("unknown-policy")
        if event.reset_at is not None and event.reset_provenance == "absent":
            raise _StoreProblem("invalid-reset-provenance")
        if event.reset_at is None and event.reset_provenance != "absent":
            raise _StoreProblem("invalid-reset-provenance")
        payload_body = _bounded_canonical_json_bytes(
            event.payload,
            budget=budget,
            max_bytes=MAX_EVENT_CANONICAL_BYTES,
            overflow_diagnostic="payload-too-large",
        )
        payload_keys = set(event.payload)
        if payload_keys not in (
            {"outcome", "authority", "reason", "retryable"},
            {"outcome", "authority", "reason", "retryable", "reset_at"},
        ):
            raise _StoreProblem("invalid-payload")
        if event.payload_fingerprint != hashlib.sha256(payload_body).hexdigest():
            raise _StoreProblem("payload-fingerprint-mismatch")
        if event.payload.get("outcome") != event.outcome.value:
            raise _StoreProblem("payload-outcome-mismatch")
        if event.payload.get("authority") != event.authority:
            raise _StoreProblem("payload-authority-mismatch")
        if event.payload.get("reason") != event.reason:
            raise _StoreProblem("payload-reason-mismatch")
        payload_retryable = event.payload.get("retryable")
        if not isinstance(payload_retryable, bool):
            raise _StoreProblem("invalid-payload")
        if payload_retryable is not _expected_retryable(event.outcome, event.authority):
            raise _StoreProblem("payload-retryable-mismatch")
        if event.reset_at is None:
            if event.payload.get("reset_at") is not None:
                raise _StoreProblem("reset-at-mismatch")
        elif _require_epoch(event.payload.get("reset_at"), "reset_at") != event.reset_at:
            raise _StoreProblem("reset-at-mismatch")
        if (
            event.reset_provenance == "parsed"
            and event.reset_parser is not None
            and event.reset_at is not None
            and event.reset_at < float(event.reset_parser["base_reference_epoch"])
        ):
            raise _StoreProblem("reset-at-before-base-reference")
        if event.reset_at is not None and (
            event.reset_at + event.reset_margin_seconds
        ) <= event.event_epoch:
            raise _StoreProblem("stale-reset-at")
        expected_fingerprint = hashlib.sha256(
            _bounded_canonical_json_bytes(
                event.canonical_payload(),
                budget=budget,
                max_bytes=MAX_EVENT_CANONICAL_BYTES,
                overflow_diagnostic="event-too-large",
            )
        ).hexdigest()
        if event.fingerprint != expected_fingerprint:
            raise _StoreProblem("event-fingerprint-mismatch")
        _bounded_canonical_json_bytes(
            event.to_payload(),
            budget=budget,
            max_bytes=MAX_EVENT_CANONICAL_BYTES,
            overflow_diagnostic="event-too-large",
        )
        events[terminal_key] = event

    acks: dict[str, _StoredAck] = {}
    for raw_terminal_key, raw_ack in raw_acks.items():
        terminal_key = _require_key_string(raw_terminal_key, "ack-terminal-key")
        if not isinstance(raw_ack, dict):
            raise _StoreProblem("invalid-ack-payload")
        if set(raw_ack) != {"terminal_key", "event_epoch", "identity_key", "fingerprint"}:
            raise _StoreProblem("invalid-ack-payload")
        ack = _StoredAck(
            terminal_key=_require_key_string(raw_ack.get("terminal_key"), "terminal-key"),
            event_epoch=_require_epoch(raw_ack.get("event_epoch"), "event_epoch"),
            identity_key=_require_key_string(raw_ack.get("identity_key"), "identity_key"),
            fingerprint=_require_non_empty_string(
                raw_ack.get("fingerprint"),
                "fingerprint",
                max_bytes=128,
            ),
        )
        event = events.get(terminal_key)
        if event is None:
            raise _StoreProblem("ack-without-event")
        if (
            ack.terminal_key != terminal_key
            or ack.event_epoch != event.event_epoch
            or ack.identity_key != event.identity_key
            or ack.fingerprint != event.fingerprint
        ):
            raise _StoreProblem("ack-event-mismatch")
        acks[terminal_key] = ack
    if set(acks) != set(events):
        raise _StoreProblem("incomplete-ack-ledger")

    unique_identity_keys = {event.identity_key for event in events.values()}
    if len(unique_identity_keys) > MAX_RETAINED_IDENTITIES:
        raise _StoreProblem("capacity-exceeded")
    events_by_identity = _group_events_by_identity(events, budget=budget)

    parsed_entries: dict[str, _StoredEntry] = {}
    for raw_identity_key, raw_entry in raw_entries.items():
        identity_key = _require_key_string(raw_identity_key, "entry-identity-key")
        if not isinstance(raw_entry, dict):
            raise _StoreProblem("invalid-entry-payload")
        expected_entry_keys = {
            "executor",
            "model_id",
            "deadline_epoch",
            "consecutive_hits",
            "last_event_epoch",
            "last_terminal_key",
            "outcome",
            "reason",
            "event_count",
        }
        if set(raw_entry) != expected_entry_keys:
            raise _StoreProblem("invalid-entry-payload")
        consecutive_hits = raw_entry.get("consecutive_hits")
        event_count = raw_entry.get("event_count")
        if (
            not isinstance(consecutive_hits, int)
            or isinstance(consecutive_hits, bool)
            or consecutive_hits < 1
        ):
            raise _StoreProblem("invalid-consecutive-hits")
        if not isinstance(event_count, int) or isinstance(event_count, bool) or event_count < 1:
            raise _StoreProblem("invalid-event-count")
        entry = _StoredEntry(
            identity_key=identity_key,
            executor=_require_non_empty_string(raw_entry.get("executor"), "executor"),
            model_id=_require_non_empty_string(raw_entry.get("model_id"), "model_id"),
            deadline_epoch=_require_epoch(raw_entry.get("deadline_epoch"), "deadline_epoch"),
            consecutive_hits=consecutive_hits,
            last_event_epoch=_require_epoch(raw_entry.get("last_event_epoch"), "last_event_epoch"),
            last_terminal_key=_require_key_string(
                raw_entry.get("last_terminal_key"), "last_terminal_key"
            ),
            outcome=_normalize_outcome(raw_entry.get("outcome")),
            reason=_require_non_empty_string(raw_entry.get("reason"), "reason"),
            event_count=event_count,
        )
        if entry.identity_key != _identity_key(entry.executor, entry.model_id):
            raise _StoreProblem("entry-identity-mismatch")
        parsed_entries[identity_key] = entry

    entries: dict[str, _StoredEntry] = {}
    for identity_key, entry in parsed_entries.items():
        expected_entry = _fold_events(
            identity_key,
            events_by_identity.get(identity_key, []),
            budget=budget,
        )
        if entry != expected_entry:
            persisted_entries = dict(parsed_entries)
            persisted_entries[identity_key] = _conservative_entry_floor(entry, expected_entry)
            for missing_identity_key, grouped_events in events_by_identity.items():
                if missing_identity_key in persisted_entries:
                    continue
                persisted_entries[missing_identity_key] = _fold_events(
                    missing_identity_key,
                    grouped_events,
                    budget=budget,
                )
            raise _StoreProblem(
                "aggregate-mismatch",
                snapshot=_StoreSnapshot(scope=scope, entries=persisted_entries, events=events, acks=acks),
            )
        entries[identity_key] = entry

    if set(entries) != unique_identity_keys:
        salvaged_entries = dict(parsed_entries)
        for missing_identity_key, grouped_events in events_by_identity.items():
            if missing_identity_key in salvaged_entries:
                continue
            salvaged_entries[missing_identity_key] = _fold_events(
                missing_identity_key,
                grouped_events,
                budget=budget,
            )
        raise _StoreProblem(
            "entry-ledger-mismatch",
            snapshot=_StoreSnapshot(scope=scope, entries=salvaged_entries, events=events, acks=acks),
        )
    return _StoreSnapshot(scope=scope, entries=entries, events=events, acks=acks)


def _status_last_good(
    snapshot: _StoreSnapshot | None,
    identity_key: str,
    *,
    now: float,
) -> ExecutorBackoff | None:
    if snapshot is None:
        return None
    active = snapshot.active_for(identity_key, now=now)
    if active is not None:
        return active
    entry = snapshot.entry_for(identity_key)
    return None if entry is None else entry.to_public()


def _payload_last_good(snapshot: _StoreSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return snapshot.to_payload()


def _fsync_directory(path: Path) -> None:
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        dir_fd = os.open(path, dir_flags)
    except OSError as error:
        raise _StoreProblem(f"unable to fsync executor backoff store directory: {error}") from error
    try:
        try:
            os.fsync(dir_fd)
        except OSError as error:
            raise _StoreProblem(f"unable to fsync executor backoff store directory: {error}") from error
    finally:
        try:
            os.close(dir_fd)
        except OSError as error:
            raise _StoreProblem(
                f"unable to close executor backoff store directory fd: {error}"
            ) from error


def _fsync_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise _StoreProblem(f"unable to fsync executor backoff store data: {error}") from error
    try:
        try:
            os.fsync(fd)
        except OSError as error:
            raise _StoreProblem(f"unable to fsync executor backoff store data: {error}") from error
    finally:
        try:
            os.close(fd)
        except OSError as error:
            raise _StoreProblem(f"unable to close executor backoff store data fd: {error}") from error


def _invoke_test_hook(stage: str) -> None:
    if _TEST_HOOK is None:
        return
    _TEST_HOOK(stage)


@contextmanager
def _root_lock(coordinator_root: Path, *, exclusive: bool) -> Iterator[None]:
    lock_path = _lock_path(coordinator_root)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if exclusive:
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise _StoreProblem(f"unable to open executor backoff lock: {error}") from error
    else:
        try:
            fd = os.open(
                lock_path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            try:
                fd = os.open(lock_path, flags, 0o600)
            except OSError as error:
                raise _StoreProblem(f"unable to open executor backoff lock: {error}") from error
        except OSError as error:
            raise _StoreProblem(f"unable to open executor backoff lock: {error}") from error
    try:
        try:
            info = os.fstat(fd)
        except OSError as error:
            raise _StoreProblem(f"unable to stat executor backoff lock: {error}") from error
        if not stat.S_ISREG(info.st_mode):
            raise _StoreProblem("executor backoff lock must be a regular file")
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = _monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if _monotonic() >= deadline:
                    raise _StoreProblem("lock-timeout") from error
                time.sleep(LOCK_POLL_INTERVAL_SECONDS)
            except OSError as error:
                raise _StoreProblem(f"unable to acquire executor backoff lock: {error}") from error
        _invoke_test_hook("lock-acquired")
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError as error:
            raise _StoreProblem(f"unable to close executor backoff lock: {error}") from error


def _read_snapshot_unlocked(
    path: Path,
    *,
    verify_durability: bool,
    expected_scope: str,
    budget: _ComputeBudget | None = None,
) -> _SnapshotRead:
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        return _SnapshotRead(read=StoreRead(observation=StoreObservation.MISSING))
    except OSError as error:
        return _SnapshotRead(read=_unknown_store(f"unable to stat executor backoff store: {error}"))
    if not stat.S_ISREG(path_info.st_mode):
        return _SnapshotRead(read=_unknown_store("executor backoff store must be a regular file"))

    budget = _ComputeBudget() if budget is None else budget
    try:
        raw = _read_bounded_bytes(path, budget=budget)
    except FileNotFoundError:
        return _SnapshotRead(read=StoreRead(observation=StoreObservation.MISSING))
    except _StoreProblem as error:
        return _SnapshotRead(read=_unknown_store(*error.diagnostics))
    try:
        payload = _strict_json_loads(raw, budget=budget)
        snapshot = _snapshot_from_payload(payload, budget=budget, expected_scope=expected_scope)
    except _StoreProblem as error:
        snapshot = error.snapshot
        return _SnapshotRead(
            read=_unknown_store(*error.diagnostics, last_good=_payload_last_good(snapshot)),
            snapshot=snapshot,
        )

    if verify_durability:
        try:
            _fsync_file(path)
            _fsync_directory(path.parent)
        except _StoreProblem as error:
            return _SnapshotRead(
                read=_unknown_store(*error.diagnostics, last_good=snapshot.to_payload()),
                snapshot=snapshot,
            )
    return _SnapshotRead(
        read=StoreRead(observation=StoreObservation.VALID, payload=snapshot.to_payload()),
        snapshot=snapshot,
    )


def read_store(store_path: str | Path) -> StoreRead:
    snapshot_read: _SnapshotRead | None = None
    try:
        path = Path(store_path)
        if path.name != STATE_FILENAME:
            return _unknown_store("invalid-store-path")
        resolved_root = _resolve_path(path.parent, diagnostic_name="coordinator-root")
        expected_scope = str(resolved_root)
        path = _state_path(resolved_root)
        with _root_lock(resolved_root, exclusive=False):
            snapshot_read = _read_snapshot_unlocked(
                path,
                verify_durability=True,
                expected_scope=expected_scope,
            )
        return snapshot_read.read
    except (TypeError, ValueError):
        return _unknown_store("invalid-store-path")
    except _StoreProblem as error:
        if snapshot_read is not None:
            return _unknown_store(
                *error.diagnostics,
                last_good=_payload_last_good(snapshot_read.snapshot),
            )
        return _unknown_store(*error.diagnostics)


def active_backoff(
    coordinator_root: str | Path,
    executor: str,
    model_id: str,
    *,
    now: float,
) -> BackoffStatus:
    snapshot_read: _SnapshotRead | None = None
    try:
        identity_key = _identity_key(executor, model_id)
        current_now = _require_epoch(now, "now")
        resolved_root = _resolve_path(coordinator_root, diagnostic_name="coordinator-root")
        expected_scope = str(resolved_root)
    except _StoreProblem as error:
        return _unknown_status(*error.diagnostics)
    path = _state_path(resolved_root)
    try:
        with _root_lock(resolved_root, exclusive=False):
            snapshot_read = _read_snapshot_unlocked(
                path,
                verify_durability=True,
                expected_scope=expected_scope,
            )
    except _StoreProblem as error:
        if snapshot_read is not None:
            return _unknown_status(
                *error.diagnostics,
                last_good=_status_last_good(snapshot_read.snapshot, identity_key, now=current_now),
            )
        return _unknown_status(*error.diagnostics)

    if snapshot_read.read.observation is StoreObservation.MISSING:
        return BackoffStatus(
            observation=StoreObservation.MISSING,
            reconciliation=ReconciliationStatus.UNVERIFIED,
        )
    if snapshot_read.read.observation is StoreObservation.UNKNOWN:
        return _unknown_status(
            *snapshot_read.read.diagnostics,
            last_good=_status_last_good(snapshot_read.snapshot, identity_key, now=current_now),
        )
    assert snapshot_read.snapshot is not None
    return BackoffStatus(
        observation=StoreObservation.VALID,
        backoff=snapshot_read.snapshot.active_for(identity_key, now=current_now),
        reconciliation=ReconciliationStatus.UNVERIFIED,
    )


def reconcile_backoff(
    coordinator_root: str | Path,
    executor: str,
    model_id: str,
    *,
    now: float,
    inventory: dict[str, object] | None,
) -> BackoffStatus:
    observation: StoreObservation | None = None
    backoff: ExecutorBackoff | None = None
    last_good: ExecutorBackoff | None = None
    try:
        identity_key = _identity_key(executor, model_id)
        current_now = _require_epoch(now, "now")
        expected_scope = str(_resolve_path(coordinator_root, diagnostic_name="coordinator-root"))
        root = Path(expected_scope)
        path = _state_path(root)
        with _root_lock(root, exclusive=False):
            budget = _ComputeBudget()
            snapshot_read = _read_snapshot_unlocked(
                path,
                verify_durability=True,
                expected_scope=expected_scope,
                budget=budget,
            )
            if snapshot_read.read.observation is StoreObservation.UNKNOWN:
                return BackoffStatus(
                    observation=StoreObservation.UNKNOWN,
                    diagnostics=snapshot_read.read.diagnostics,
                    last_good=_status_last_good(
                        snapshot_read.snapshot,
                        identity_key,
                        now=current_now,
                    ),
                    reconciliation=ReconciliationStatus.UNKNOWN,
                )
            if snapshot_read.read.observation is StoreObservation.MISSING:
                snapshot = _StoreSnapshot(scope=expected_scope, entries={}, events={}, acks={})
                observation = StoreObservation.MISSING
            else:
                assert snapshot_read.snapshot is not None
                snapshot = snapshot_read.snapshot
                observation = StoreObservation.VALID
            backoff = snapshot.active_for(identity_key, now=current_now)
            last_good = _status_last_good(snapshot, identity_key, now=current_now)
            if inventory is None:
                return BackoffStatus(
                    observation=observation,
                    backoff=backoff,
                    last_good=last_good,
                    reconciliation=ReconciliationStatus.UNVERIFIED,
                )
            inventory_mapping = _require_mapping(inventory, "inventory")
            if set(inventory_mapping) != {"complete", "readable", "events", "evidence_ref"}:
                raise _StoreProblem("invalid-inventory")
            normalized_inventory = _validate_input_json_value(inventory_mapping, budget=budget)
            inventory_body = _bounded_canonical_json_bytes(
                normalized_inventory,
                budget=budget,
                max_bytes=MAX_INPUT_BYTES,
                overflow_diagnostic="capacity-exceeded",
            )
            immutable_inventory = json.loads(
                inventory_body.decode("utf-8"),
                parse_constant=_reject_constants,
                object_pairs_hook=_reject_duplicate_object_pairs,
            )
            inventory_mapping = _require_mapping(immutable_inventory, "inventory")
            readable = inventory_mapping.get("readable")
            complete = inventory_mapping.get("complete")
            if not isinstance(readable, bool) or not isinstance(complete, bool):
                raise _StoreProblem("invalid-inventory")
            evidence_ref = _require_non_empty_string(
                inventory_mapping.get("evidence_ref"), "inventory_evidence_ref"
            )
            if not readable or not complete:
                return BackoffStatus(
                    observation=observation,
                    backoff=backoff,
                    diagnostics=("inventory-unavailable",),
                    last_good=last_good,
                    reconciliation=ReconciliationStatus.UNKNOWN,
                    evidence_refs=(evidence_ref,),
                )
            raw_events = inventory_mapping.get("events")
            if not isinstance(raw_events, list):
                raise _StoreProblem("invalid-inventory")
            if len(raw_events) > MAX_RETAINED_EVENTS:
                raise _StoreProblem("capacity-exceeded")
            missing: list[str] = []
            conflicting: list[str] = []
            seen_terminal_keys: set[str] = set()
            for raw_event in raw_events:
                event_mapping = _require_mapping(raw_event, "inventory-event")
                if set(event_mapping) != {
                    "executor",
                    "model_id",
                    "job_id",
                    "event_epoch",
                    "outcome",
                    "reset_at",
                    "reason",
                }:
                    raise _StoreProblem("invalid-inventory-event")
                if (
                    event_mapping.get("executor") != executor
                    or event_mapping.get("model_id") != model_id
                ):
                    continue
                event = _event_from_inputs(
                    executor=executor,
                    model_id=model_id,
                    outcome=event_mapping.get("outcome"),
                    reset_at=(
                        None
                        if event_mapping.get("reset_at") is None
                        else _require_epoch(event_mapping.get("reset_at"), "reset_at")
                    ),
                    reason=_require_non_empty_string(event_mapping.get("reason"), "reason"),
                    job_id=_require_key_string(event_mapping.get("job_id"), "job_id"),
                    event_epoch=_require_epoch(event_mapping.get("event_epoch"), "event_epoch"),
                    budget=budget,
                )
                seen_terminal_keys.add(event.terminal_key)
                stored = snapshot.events.get(event.terminal_key)
                if stored is None or event.terminal_key not in snapshot.acks:
                    missing.append(event.terminal_key)
                    continue
                if stored != event:
                    conflicting.append(event.terminal_key)
            unexpected_store_keys = sorted(
                key
                for key, stored in snapshot.events.items()
                if stored.identity_key == identity_key and key not in seen_terminal_keys
            )
            if unexpected_store_keys:
                conflicting.extend(unexpected_store_keys)
            if conflicting:
                return BackoffStatus(
                    observation=observation,
                    backoff=backoff,
                    diagnostics=("inventory-conflict",),
                    last_good=last_good,
                    reconciliation=ReconciliationStatus.UNKNOWN,
                    missing_terminal_keys=tuple(sorted(missing)),
                    conflicting_terminal_keys=tuple(sorted(conflicting)),
                    evidence_refs=(evidence_ref,),
                )
            if missing:
                return BackoffStatus(
                    observation=observation,
                    backoff=backoff,
                    diagnostics=("inventory-pending",),
                    last_good=last_good,
                    reconciliation=ReconciliationStatus.PENDING,
                    missing_terminal_keys=tuple(sorted(missing)),
                    evidence_refs=(evidence_ref,),
                )
            return BackoffStatus(
                observation=observation,
                backoff=backoff,
                diagnostics=(),
                last_good=last_good,
                reconciliation=ReconciliationStatus.COMPLETE,
                evidence_refs=(evidence_ref,),
            )
    except _StoreProblem as error:
        return BackoffStatus(
            observation=StoreObservation.UNKNOWN if observation is None else observation,
            backoff=backoff,
            diagnostics=error.diagnostics,
            last_good=last_good,
            reconciliation=ReconciliationStatus.UNKNOWN,
        )


def _write_snapshot(path: Path, snapshot: _StoreSnapshot, *, budget: _ComputeBudget) -> None:
    payload = snapshot.to_payload()
    body = _bounded_canonical_json_bytes(
        payload,
        budget=budget,
        max_bytes=MAX_STORE_BYTES,
        overflow_diagnostic="capacity-exceeded",
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _StoreProblem(f"unable to create executor backoff root: {error}") from error
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    except OSError as error:
        raise _StoreProblem(f"unable to allocate executor backoff temp file: {error}") from error
    temp = Path(name)
    temp_fd = fd
    try:
        try:
            os.fchmod(temp_fd, 0o600)
        except OSError as error:
            raise _StoreProblem(f"unable to chmod executor backoff temp file: {error}") from error
        handle = os.fdopen(temp_fd, "wb")
        try:
            temp_fd = -1
            try:
                handle.write(body)
                handle.flush()
            except OSError as error:
                raise _StoreProblem(f"unable to write executor backoff temp file: {error}") from error
            try:
                os.fsync(handle.fileno())
            except OSError as error:
                raise _StoreProblem(f"unable to fsync executor backoff temp file: {error}") from error
        finally:
            try:
                handle.close()
            except OSError as error:
                raise _StoreProblem(f"unable to close executor backoff temp file: {error}") from error
        _invoke_test_hook("before-replace")
        try:
            os.replace(temp, path)
        except OSError as error:
            raise _StoreProblem(f"unable to replace executor backoff store: {error}") from error
        _invoke_test_hook("after-replace")
        try:
            _fsync_directory(path.parent)
        except _StoreProblem as error:
            raise _StoreProblem(
                "commit-durability-unknown",
                *error.diagnostics,
                snapshot=snapshot,
            ) from error
        _invoke_test_hook("after-directory-fsync")
    except BaseException:
        if temp_fd != -1:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        temp.unlink(missing_ok=True)
        raise


def _merge_event(snapshot: _StoreSnapshot, event: _StoredEvent, *, budget: _ComputeBudget) -> tuple[_StoreSnapshot, bool]:
    existing = snapshot.events.get(event.terminal_key)
    if existing is not None:
        if existing == event:
            return snapshot, False
        raise _StoreProblem("integrity-conflict")
    if len(snapshot.events) + 1 > MAX_RETAINED_EVENTS:
        raise _StoreProblem("capacity-exceeded")
    identity_keys = {stored.identity_key for stored in snapshot.events.values()}
    if event.identity_key not in identity_keys and len(identity_keys) + 1 > MAX_RETAINED_IDENTITIES:
        raise _StoreProblem("capacity-exceeded")

    merged_events = dict(snapshot.events)
    merged_events[event.terminal_key] = event
    ack = _StoredAck(
        terminal_key=event.terminal_key,
        event_epoch=event.event_epoch,
        identity_key=event.identity_key,
        fingerprint=event.fingerprint,
    )
    merged_acks = dict(snapshot.acks)
    merged_acks[event.terminal_key] = ack
    merged_entries = dict(snapshot.entries)
    merged_entries[event.identity_key] = _fold_entry(event.identity_key, merged_events, budget=budget)
    candidate = _StoreSnapshot(
        scope=snapshot.scope,
        entries=merged_entries,
        events=merged_events,
        acks=merged_acks,
    )
    _bounded_canonical_json_bytes(
        candidate.to_payload(),
        budget=budget,
        max_bytes=MAX_STORE_BYTES,
        overflow_diagnostic="capacity-exceeded",
    )
    return candidate, True


def record_backoff(
    coordinator_root: str | Path,
    executor: str,
    model_id: str,
    *,
    now: float,
    outcome: object,
    reset_at: float | None = None,
    reason: str,
    job_id: str,
    event_epoch: float,
) -> BackoffMutationResult:
    event: _StoredEvent | None = None
    last_known_snapshot: _StoreSnapshot | None = None
    committed = False
    try:
        current_now = _require_epoch(now, "now")
        expected_scope = str(_resolve_path(coordinator_root, diagnostic_name="coordinator-root"))
    except _StoreProblem as error:
        return _unknown_mutation(*error.diagnostics)

    root = Path(expected_scope)
    path = _state_path(root)
    try:
        with _root_lock(root, exclusive=True):
            budget = _ComputeBudget()
            event = _event_from_inputs(
                executor=executor,
                model_id=model_id,
                outcome=outcome,
                reset_at=reset_at,
                reason=reason,
                job_id=job_id,
                event_epoch=event_epoch,
                budget=budget,
            )
            snapshot_read = _read_snapshot_unlocked(
                path,
                verify_durability=True,
                expected_scope=expected_scope,
                budget=budget,
            )
            if snapshot_read.read.observation is StoreObservation.UNKNOWN:
                return _unknown_mutation(
                    *snapshot_read.read.diagnostics,
                    last_good=_status_last_good(snapshot_read.snapshot, event.identity_key, now=current_now),
                )
            if snapshot_read.read.observation is StoreObservation.MISSING:
                snapshot = _StoreSnapshot(
                    scope=expected_scope,
                    entries={},
                    events={},
                    acks={},
                )
            else:
                assert snapshot_read.snapshot is not None
                snapshot = snapshot_read.snapshot
            last_known_snapshot = snapshot
            try:
                candidate, changed = _merge_event(snapshot, event, budget=budget)
            except _StoreProblem as error:
                last_good = _status_last_good(snapshot, event.identity_key, now=current_now)
                if error.diagnostics == ("integrity-conflict",):
                    return _unknown_mutation(*error.diagnostics, last_good=last_good)
                return _unknown_mutation(*error.diagnostics, last_good=last_good)
            if not changed:
                existing = snapshot.active_for(event.identity_key, now=current_now)
                return BackoffMutationResult(
                    observation=StoreObservation.VALID,
                    backoff=existing,
                    reconciliation=ReconciliationStatus.UNVERIFIED,
                    changed=False,
                )
            try:
                _write_snapshot(path, candidate, budget=budget)
                committed = True
                last_known_snapshot = candidate
            except _StoreProblem as error:
                last_good_snapshot = snapshot if error.snapshot is None else error.snapshot
                return BackoffMutationResult(
                    observation=StoreObservation.UNKNOWN,
                    diagnostics=error.diagnostics,
                    last_good=_status_last_good(
                        last_good_snapshot,
                        event.identity_key,
                        now=current_now,
                    ),
                    reconciliation=ReconciliationStatus.UNVERIFIED,
                    changed=error.diagnostics[:1] == ("commit-durability-unknown",),
                )
    except _StoreProblem as error:
        if event is not None and last_known_snapshot is not None:
            return BackoffMutationResult(
                observation=StoreObservation.UNKNOWN,
                diagnostics=error.diagnostics,
                last_good=_status_last_good(
                    last_known_snapshot,
                    event.identity_key,
                    now=current_now,
                ),
                reconciliation=ReconciliationStatus.UNVERIFIED,
                changed=committed,
            )
        return _unknown_mutation(*error.diagnostics)

    return BackoffMutationResult(
        observation=StoreObservation.VALID,
        backoff=candidate.active_for(event.identity_key, now=current_now),
        reconciliation=ReconciliationStatus.UNVERIFIED,
        changed=True,
    )


def clear_backoff(
    coordinator_root: str | Path,
    executor: str,
    model_id: str,
) -> BackoffMutationResult:
    snapshot_read: _SnapshotRead | None = None
    try:
        identity_key = _identity_key(executor, model_id)
        current_now = _require_epoch(_time_now(), "now")
        expected_scope = str(_resolve_path(coordinator_root, diagnostic_name="coordinator-root"))
    except _StoreProblem as error:
        return _unknown_mutation(*error.diagnostics)
    root = Path(expected_scope)
    path = _state_path(root)
    try:
        with _root_lock(root, exclusive=True):
            snapshot_read = _read_snapshot_unlocked(
                path,
                verify_durability=True,
                expected_scope=expected_scope,
            )
            if snapshot_read.read.observation is StoreObservation.MISSING:
                return BackoffMutationResult(
                    observation=StoreObservation.MISSING,
                    reconciliation=ReconciliationStatus.UNVERIFIED,
                    changed=False,
                )
            if snapshot_read.read.observation is StoreObservation.UNKNOWN:
                return _unknown_mutation(
                    *snapshot_read.read.diagnostics,
                    last_good=_status_last_good(snapshot_read.snapshot, identity_key, now=current_now),
                )
            assert snapshot_read.snapshot is not None
            snapshot = snapshot_read.snapshot
            active = snapshot.active_for(identity_key, now=current_now)
            if active is not None:
                return BackoffMutationResult(
                    observation=StoreObservation.VALID,
                    backoff=active,
                    diagnostics=("active-cooldown",),
                    reconciliation=ReconciliationStatus.UNVERIFIED,
                    changed=False,
                )
            return BackoffMutationResult(
                observation=StoreObservation.VALID,
                backoff=None,
                last_good=snapshot.entry_for(identity_key).to_public()
                if snapshot.entry_for(identity_key) is not None
                else None,
                reconciliation=ReconciliationStatus.UNVERIFIED,
                changed=False,
            )
    except _StoreProblem as error:
        if snapshot_read is not None:
            return BackoffMutationResult(
                observation=StoreObservation.UNKNOWN,
                diagnostics=error.diagnostics,
                last_good=_status_last_good(snapshot_read.snapshot, identity_key, now=current_now),
                reconciliation=ReconciliationStatus.UNVERIFIED,
                changed=False,
            )
        return _unknown_mutation(*error.diagnostics)
