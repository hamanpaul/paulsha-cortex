"""#836 的 manager-owned、append-only quota event ledger。"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from . import quota_observation as schema


_MAX_LEDGER_BYTES = 32 * 1024 * 1024
_MAX_LEDGER_EVENTS = 100_000
_MAX_IDEMPOTENCY_CHARS = 512
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TIMESTAMP_MS = 253402300799999
_TERMINAL_USAGE_SOURCE_SCHEMA = "cortex-terminal-usage-v1"


class LedgerCorrupt(ValueError):
    """ledger 不完整、超限、版本未知或權限形狀不可信。"""


@dataclass(frozen=True)
class LedgerAppendResult:
    status: str
    accepted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    idempotency_key: str = ""


@dataclass(frozen=True)
class LedgerSnapshot:
    events: tuple[dict[str, Any], ...]


class QuotaEventLedger:
    """以固定版本 JSONL append-only 儲存 observations 與衝突收據。

    檔案使用 no-follow、flock、0600 與 fsync。未知／損毀內容一律不視為空 ledger。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from paulsha_cortex.config.paths import quota_observation_root

            path = quota_observation_root() / "events.jsonl"
        self.path = Path(path)
        if ".." in self.path.parts:
            raise ValueError("ledger path cannot contain parent traversal")

    def append_observation(
        self,
        observation: schema.QuotaObservation,
        *,
        idempotency_key: str | None = None,
        associations: tuple[dict[str, Any], ...] = (),
        terminal_job_started_at_ms: int | None = None,
        terminal_job_finished_at_ms: int | None = None,
    ) -> LedgerAppendResult:
        if not isinstance(observation, schema.QuotaObservation):
            raise TypeError("observation must be parser-sealed")
        wire = observation.to_dict()
        normalized_associations = _validate_associations(associations)
        # 只保存原始事實（job 起訖時間），不保存任何依 ledger 現況推導出的
        # 衍生值：observation identity／digest 只由這些原始事實決定，重播時
        # 不論 ledger 內容是否變動都會得到相同 digest（見 #836 對抗審查第
        # 五輪 MAJOR）。
        terminal_metadata = _terminal_usage_metadata(
            wire, terminal_job_started_at_ms, terminal_job_finished_at_ms
        )
        key = _idempotency_key(observation, wire, idempotency_key)
        payload_bytes = _canonical_bytes(
            _observation_digest_payload(wire, normalized_associations, terminal_metadata)
        )
        digest = hashlib.sha256(payload_bytes).hexdigest()
        entry = {
            "schema_version": 1,
            "kind": "observation",
            "idempotency_key": key,
            "payload_sha256": digest,
            "observation": wire,
        }
        entry.update(terminal_metadata)
        if normalized_associations:
            entry["associations"] = normalized_associations
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_parent()
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise LedgerCorrupt("ledger-open-failed") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            info = os.fstat(fd)
            self._check_file(info)
            records = self._read_fd(fd, info.st_size)
            if len(records) >= _MAX_LEDGER_EVENTS:
                raise LedgerCorrupt("ledger-event-limit")
            previous = [row for row in records if row.get("idempotency_key") == key
                        and row.get("kind") == "observation"]
            conflicts = [row for row in records if row.get("idempotency_key") == key
                         and row.get("kind") == "conflict"]
            if conflicts:
                return LedgerAppendResult("conflict", conflicts=1, idempotency_key=key)
            if previous:
                if any(row.get("payload_sha256") == digest for row in previous):
                    return LedgerAppendResult("duplicate", duplicates=1, idempotency_key=key)
                same_value = [
                    row for row in previous
                    if _same_snapshot_value(row.get("observation"), wire)
                ]
                if same_value:
                    if (_snapshot_has_known_window_epoch(wire)
                            and not any(_snapshot_has_known_window_epoch(
                                row.get("observation")
                            ) for row in same_value)):
                        self._append_fd(fd, entry, info.st_size)
                        return LedgerAppendResult("accepted", accepted=1, idempotency_key=key)
                    return LedgerAppendResult("duplicate", duplicates=1, idempotency_key=key)
                prior_digest = previous[-1].get("payload_sha256")
                conflict = {
                    "schema_version": 1,
                    "kind": "conflict",
                    "idempotency_key": key,
                    "existing_sha256": prior_digest,
                    "incoming_sha256": digest,
                    "scope": _scope_summary(wire),
                    "observed_at_ms": _known_value(wire.get("observed_at_ms")),
                    "window_id": _scope_summary(wire).get("window_id"),
                }
                if normalized_associations:
                    conflict["associations"] = normalized_associations
                self._append_fd(fd, conflict, info.st_size)
                return LedgerAppendResult("conflict", conflicts=1, idempotency_key=key)
            # 就算這筆的 idempotency key（caller 指定或外部 event_identity）
            # 與既有事件都不同，仍必須以語意範圍（pool／window／observed_at／
            # metric）比對是否撞上同一次觀測：key 本身可能因呼叫端／事件來源
            # 不同而永遠不會撞在一起，但那不代表語意上不是同一筆衝突觀測
            # （見 #836 對抗審查第七輪 MAJOR quota_ledger.py:274）。
            cross_digest = _cross_identity_conflict_digest(records, wire, digest)
            if cross_digest is not None:
                conflict = {
                    "schema_version": 1,
                    "kind": "conflict",
                    "idempotency_key": key,
                    "existing_sha256": cross_digest,
                    "incoming_sha256": digest,
                    "scope": _scope_summary(wire),
                    "observed_at_ms": _known_value(wire.get("observed_at_ms")),
                    "window_id": _scope_summary(wire).get("window_id"),
                }
                if normalized_associations:
                    conflict["associations"] = normalized_associations
                self._append_fd(fd, conflict, info.st_size)
                return LedgerAppendResult("conflict", conflicts=1, idempotency_key=key)
            self._append_fd(fd, entry, info.st_size)
            return LedgerAppendResult("accepted", accepted=1, idempotency_key=key)
        finally:
            os.close(fd)

    def read(self) -> LedgerSnapshot:
        self._check_parent(allow_missing=True)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return LedgerSnapshot(())
        except OSError as exc:
            raise LedgerCorrupt("ledger-open-failed") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            info = os.fstat(fd)
            self._check_file(info)
            return LedgerSnapshot(tuple(self._read_fd(fd, info.st_size)))
        finally:
            os.close(fd)

    def _check_parent(self, *, allow_missing: bool = False) -> None:
        try:
            info = self.path.parent.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise LedgerCorrupt("ledger-parent-missing")
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise LedgerCorrupt("ledger-parent-permissions-invalid")

    @staticmethod
    def _check_file(info: os.stat_result) -> None:
        if (not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_LEDGER_BYTES
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise LedgerCorrupt("ledger-file-shape-invalid")

    @staticmethod
    def _read_fd(fd: int, size: int) -> list[dict[str, Any]]:
        if size > _MAX_LEDGER_BYTES:
            raise LedgerCorrupt("ledger-size-limit")
        raw = os.pread(fd, size, 0)
        if len(raw) != size:
            raise LedgerCorrupt("ledger-short-read")
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise LedgerCorrupt("ledger-partial-tail")
        lines = raw.splitlines()
        if len(lines) > _MAX_LEDGER_EVENTS:
            raise LedgerCorrupt("ledger-event-limit")
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise LedgerCorrupt("ledger-invalid-json") from exc
            if (not isinstance(row, dict) or type(row.get("schema_version")) is not int
                    or row.get("schema_version") != 1):
                raise LedgerCorrupt("ledger-unknown-record-version")
            if row.get("kind") == "observation":
                required = {"schema_version", "kind", "idempotency_key", "payload_sha256", "observation"}
                optional = {
                    "associations", "terminal_job_started_at_ms", "terminal_job_finished_at_ms",
                }
                if (not required.issubset(row)
                        or not set(row).issubset(required | optional)
                        or not isinstance(row.get("idempotency_key"), str)
                        or not row["idempotency_key"]
                        or len(row["idempotency_key"]) > _MAX_IDEMPOTENCY_CHARS + len("caller:")
                        or not isinstance(row.get("payload_sha256"), str)
                        or not _SHA256_RE.fullmatch(row["payload_sha256"])
                        or not isinstance(row.get("observation"), dict)):
                    raise LedgerCorrupt("ledger-invalid-observation-record")
                has_associations = "associations" in row
                associations = _validate_associations(tuple(row.get("associations", [])))
                terminal_metadata = {}
                has_terminal_started = "terminal_job_started_at_ms" in row
                has_terminal_finished = "terminal_job_finished_at_ms" in row
                if has_terminal_started or has_terminal_finished:
                    # 起訖兩個原始事實欄位一律成對出現；只有其中一個代表
                    # ledger 本身結構已不完整。
                    if not (has_terminal_started and has_terminal_finished):
                        raise LedgerCorrupt("ledger-invalid-terminal-job-timestamps")
                    try:
                        if not _is_terminal_usage_observation(row["observation"]):
                            raise ValueError("terminal job timestamps on non-terminal usage")
                        terminal_metadata = _terminal_usage_metadata(
                            row["observation"],
                            row["terminal_job_started_at_ms"],
                            row["terminal_job_finished_at_ms"],
                        )
                    except (TypeError, ValueError) as exc:
                        raise LedgerCorrupt("ledger-invalid-terminal-job-timestamps") from exc
                digest_payload = _observation_digest_payload(
                    row["observation"], associations if has_associations else (), terminal_metadata
                )
                digest = hashlib.sha256(_canonical_bytes(digest_payload)).hexdigest()
                if digest != row["payload_sha256"]:
                    raise LedgerCorrupt("ledger-digest-mismatch")
                row["associations"] = associations
            elif row.get("kind") == "conflict":
                allowed = {"schema_version", "kind", "idempotency_key", "existing_sha256",
                           "incoming_sha256", "scope", "observed_at_ms", "window_id", "associations"}
                if (set(row) not in (allowed, allowed - {"associations"})
                        or not isinstance(row.get("idempotency_key"), str)
                        or not row["idempotency_key"]
                        or len(row["idempotency_key"]) > _MAX_IDEMPOTENCY_CHARS + len("caller:")
                        or not isinstance(row.get("existing_sha256"), str)
                        or not _SHA256_RE.fullmatch(row["existing_sha256"])
                        or not isinstance(row.get("incoming_sha256"), str)
                        or not _SHA256_RE.fullmatch(row["incoming_sha256"])
                        or not isinstance(row.get("scope"), dict)
                        or row.get("observed_at_ms") is not None
                        and type(row.get("observed_at_ms")) is not int
                        or row.get("window_id") is not None
                        and not isinstance(row.get("window_id"), str)):
                    raise LedgerCorrupt("ledger-invalid-conflict-record")
                row.setdefault("associations", [])
                _validate_associations(tuple(row["associations"]))
            else:
                raise LedgerCorrupt("ledger-unknown-record-kind")
            records.append(row)
        return records

    @staticmethod
    def _append_fd(fd: int, row: dict[str, Any], old_size: int) -> None:
        raw = _canonical_bytes(row) + b"\n"
        if old_size + len(raw) > _MAX_LEDGER_BYTES:
            raise LedgerCorrupt("ledger-size-limit")
        cursor = 0
        while cursor < len(raw):
            written = os.write(fd, raw[cursor:])
            if written <= 0:
                raise OSError("ledger append made no progress")
            cursor += written
        os.fsync(fd)


def _idempotency_key(observation, wire, caller_key):
    if caller_key is not None:
        if (not isinstance(caller_key, str) or not caller_key
                or len(caller_key) > _MAX_IDEMPOTENCY_CHARS or "\x00" in caller_key):
            raise ValueError("invalid idempotency key")
        return "caller:" + caller_key
    identity = schema.event_identity(observation)
    if identity.get("state") == "available":
        measurement = wire.get("measurement", {})
        return "source:" + hashlib.sha256(_canonical_bytes({
            "event": list(identity.get("key", ())),
            "scope": _scope_summary(wire),
            "metric_id": measurement.get("metric_id"),
            "kind": measurement.get("kind"),
        })).hexdigest()
    # Without a provider event ID, use stable observation coordinates rather
    # than content: a changed quantity at the same scope/time must collide
    # and create a conflict receipt. The key intentionally excludes
    # source_id/source_schema/method: conflict detection must compare
    # across sources on semantic scope (pool/window/observed_at), not only
    # within one source. Keying on source fields let a manager provider
    # read and an imported external read (or the same source before/after
    # an adapter-schema rollback) report different remaining values for
    # the same pool/window/observed_at without ever colliding, so
    # project() silently picked one arbitrarily instead of raising a
    # conflict receipt (see #836 對抗審查第六輪 MAJOR quota_ledger.py:296).
    derived_identity = _semantic_scope_identity(wire)
    if derived_identity is not None:
        return "derived:" + hashlib.sha256(_canonical_bytes(derived_identity)).hexdigest()
    # Unknown coordinates cannot safely be coalesced across receipts. Keep
    # replay detection stable by using the caller observation ID, not payload.
    return "receipt:" + hashlib.sha256(_canonical_bytes({
        "source_id": observation.source_id,
        "observation_id": observation.observation_id,
    })).hexdigest()


def _semantic_scope_identity(wire):
    """觀測的語意範圍身分：pool／window／observed_at／metric／measurement
    kind。與 idempotency key 使用哪一種機制（caller-supplied、外部
    event_identity 或本模組推導的 derived key）完全無關，只用來判斷「這是
    不是同一次觀測」，不論 idempotency key 是否相同（見 #836 對抗審查第七輪
    MAJOR quota_ledger.py:274）。scope 或 observed_at_ms 未知時回傳 None，
    代表這筆事件的座標不足以安全比對。"""
    scope = _scope_summary(wire)
    if scope.get("state") != "known":
        return None
    observed_at_ms = _known_value(wire.get("observed_at_ms"))
    if type(observed_at_ms) is not int:
        return None
    measurement = wire.get("measurement")
    if not isinstance(measurement, dict):
        return None
    return {
        "scope": scope,
        "metric_id": measurement.get("metric_id"),
        "kind": measurement.get("kind"),
        "observed_at_ms": observed_at_ms,
    }


_SNAPSHOT_LIKE_MEASUREMENT_KINDS = frozenset(("remaining_snapshot", "gauge_snapshot"))


def _same_snapshot_value(prior_wire, wire):
    """同一語意 key 下兩筆 snapshot 類量測（remaining／gauge）是否觀測到同一個
    量值。同一輪輪詢由不同 profile／來源對同一共享 pool 在同一時點讀到相同值
    時，payload digest 會因 profile_ref 等欄位不同而不同，但那是一致的觀測，
    不是衝突（#836 對抗審查第八輪 BLOCKER quota_ledger.py:109）；量值不同才是
    衝突。usage_delta 等累加型事件不走這條（仍以完整 digest 判定）。"""
    if not isinstance(prior_wire, dict) or not isinstance(wire, dict):
        return False
    prior_measurement = prior_wire.get("measurement")
    measurement = wire.get("measurement")
    if not isinstance(prior_measurement, dict) or not isinstance(measurement, dict):
        return False
    if (
        prior_measurement.get("kind") not in _SNAPSHOT_LIKE_MEASUREMENT_KINDS
        or measurement.get("kind") != prior_measurement.get("kind")
    ):
        return False
    prior_id = _semantic_scope_identity(prior_wire)
    current_id = _semantic_scope_identity(wire)
    if prior_id is None or current_id is None or _canonical_bytes(prior_id) != _canonical_bytes(current_id):
        return False
    return _canonical_bytes(prior_measurement.get("quantity")) == _canonical_bytes(
        measurement.get("quantity")
    )


def _snapshot_has_known_window_epoch(wire):
    window_instance = wire.get("window_instance") if isinstance(wire, dict) else None
    if not isinstance(window_instance, dict) or window_instance.get("kind") != "interval":
        return False
    epoch = window_instance.get("epoch")
    return isinstance(epoch, dict) and epoch.get("state") == "known"


def _cross_identity_conflict_digest(records, wire, digest):
    """在既有事件中尋找與這筆觀測語意範圍相同、但觀測到的量測值不同的既有
    observation 記錄——不論兩者當初各自使用哪一種 idempotency key。找到即
    回傳既有的 payload digest（用於寫入 conflict receipt）；範圍不足以比
    對、或範圍相同但量測值一致都回傳 None（視為一致，不算衝突：不同
    profile／來源各自獨立讀到同一個真值時，profile_ref／source 等欄位本來
    就可能不同，不能因此誤判成衝突）。

    只比對「同一時間點應該只有一個真值」的 snapshot 類量測
    （remaining_snapshot／gauge_snapshot）。usage_delta 等累加型事件本質上
    允許多筆事件共享同一個 observed_at_ms（例如多個不同 job 在同一毫秒各自
    完成、各自扣減同一個 window），不屬於這裡要抓的語意衝突；那一類事件的
    去重／衝突判定另有專屬 identity（見 record_terminal_usage 的 replay
    key），不能被這裡的通用比對誤傷。"""
    measurement = wire.get("measurement")
    if not isinstance(measurement, dict) or measurement.get("kind") not in _SNAPSHOT_LIKE_MEASUREMENT_KINDS:
        return None
    semantic_id = _semantic_scope_identity(wire)
    if semantic_id is None:
        return None
    target = _canonical_bytes(semantic_id)
    value = _canonical_bytes(measurement.get("quantity"))
    for row in records:
        if row.get("kind") != "observation":
            continue
        other_wire = row.get("observation")
        if not isinstance(other_wire, dict):
            continue
        other_measurement = other_wire.get("measurement")
        if (not isinstance(other_measurement, dict)
                or other_measurement.get("kind") not in _SNAPSHOT_LIKE_MEASUREMENT_KINDS):
            continue
        other_id = _semantic_scope_identity(other_wire)
        if other_id is None or _canonical_bytes(other_id) != target:
            continue
        if _canonical_bytes(other_measurement.get("quantity")) == value:
            continue
        return row.get("payload_sha256")
    return None


def _is_terminal_usage_observation(wire):
    source = wire.get("source", {}) if isinstance(wire, dict) else {}
    return (
        isinstance(source, dict)
        and source.get("source_schema") == _TERMINAL_USAGE_SOURCE_SCHEMA
        and source.get("method") == "executor_usage"
    )


def _terminal_usage_metadata(wire, started_at_ms, finished_at_ms=None):
    """終局 usage 的原始事實中繼資料：只保存 job 起訖時間本身，不保存任何
    依 ledger 現況推導出的衍生值（例如 window instance）。任一時間缺失即
    以 None 記為 unknown，仍照樣寫入（見 #836 對抗審查第五輪 MAJOR）。"""
    if not _is_terminal_usage_observation(wire):
        if started_at_ms is not None or finished_at_ms is not None:
            raise ValueError("terminal job timestamps require terminal usage observation")
        return {}
    for value in (started_at_ms, finished_at_ms):
        if (value is not None
                and (type(value) is not int or value < 0 or value > _MAX_TIMESTAMP_MS)):
            raise ValueError("invalid terminal job timestamp")
    return {
        "terminal_job_started_at_ms": started_at_ms,
        "terminal_job_finished_at_ms": finished_at_ms,
    }


_TERMINAL_USAGE_REPLAY_TIME_EXCLUDED = "terminal-usage-replay-time-excluded-from-identity"
_TERMINAL_USAGE_REPLAY_PROFILE_EXCLUDED = "terminal-usage-replay-profile-excluded-from-identity"


def _observation_digest_payload(wire, associations, terminal_metadata):
    if _is_terminal_usage_observation(wire):
        # 終局 usage 的身分／衝突判定只綁 usage 內容與 job 終局事實
        # （job_id 已在 idempotency key、job 開始時間在 terminal_metadata），
        # 不綁重播當下的 observed_at_ms/received_at_ms：同一個已結束的 job
        # 若在重啟後以較晚時間重播相同 usage，內容不變就必須判定為
        # duplicate，不能因為記錄時間變了而被誤判成 conflict。
        wire = dict(wire)
        wire["observed_at_ms"] = _TERMINAL_USAGE_REPLAY_TIME_EXCLUDED
        wire["received_at_ms"] = _TERMINAL_USAGE_REPLAY_TIME_EXCLUDED
        # 同樣不綁記錄當下使用的 profile_key／observation_id：同一 terminal
        # job 若因 group binding／profile alias replay 先後以不同
        # profile_key 記到同一個 shared pool/window，identity 只認
        # (job_id, pool identity, metric, window 範圍)，profile_key 不應
        # 參與比對——否則數值相同的重播仍會因 profile_ref/observation_id
        # 不同而被誤判成 conflict，而不是 duplicate（見 #836 對抗審查第七輪
        # MAJOR quota_shadow.py:307）。
        wire["profile_ref"] = _TERMINAL_USAGE_REPLAY_PROFILE_EXCLUDED
        wire["observation_id"] = _TERMINAL_USAGE_REPLAY_PROFILE_EXCLUDED
    if associations:
        payload = {"observation": wire, "associations": associations}
    else:
        payload = wire
    if terminal_metadata:
        payload = dict(payload)
        payload.update(terminal_metadata)
    return payload


def _scope_summary(wire):
    scope = wire.get("scope", {})
    value = scope.get("value") if isinstance(scope, dict) else None
    if not isinstance(value, dict):
        return {"state": "unknown"}
    pool = value.get("pool_ref")
    if not isinstance(pool, dict):
        return {"state": "unknown"}
    return {
        "state": "known",
        "pool_ref": {key: pool.get(key) for key in ("authority_id", "account_id", "pool_id", "revision")},
        "window_id": value.get("window_id"),
    }


def _validate_associations(value):
    if type(value) is not tuple and type(value) is not list:
        raise ValueError("associations must be a tuple or list")
    if len(value) > 64:
        raise ValueError("too many quota associations")
    result: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str, str, str], str]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid quota association")
        pool = item.get("pool_ref")
        window_id = item.get("window_id")
        binding_id = item.get("binding_id")
        if (not isinstance(pool, dict) or set(pool) != {"authority_id", "account_id", "pool_id", "revision"}
                or any(not isinstance(pool[key], str) or not pool[key] for key in pool)
                or not isinstance(window_id, str) or not window_id
                or not isinstance(binding_id, str) or not binding_id):
            raise ValueError("invalid quota association")
        pool_key = tuple(pool[key] for key in ("authority_id", "account_id", "pool_id", "revision"))
        identity = (pool_key, window_id)
        if identity in seen:
            raise ValueError("duplicate quota association")
        seen.add(identity)
        result.append({"pool_ref": dict(pool), "window_id": window_id, "binding_id": binding_id})
    result.sort(key=lambda item: (
        item["pool_ref"]["authority_id"], item["pool_ref"]["account_id"],
        item["pool_ref"]["pool_id"], item["pool_ref"]["revision"], item["window_id"],
    ))
    return result


def _known_value(value):
    if not isinstance(value, dict) or value.get("state") != "known":
        return None
    return value.get("value")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
