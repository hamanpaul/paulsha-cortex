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
    ) -> LedgerAppendResult:
        if not isinstance(observation, schema.QuotaObservation):
            raise TypeError("observation must be parser-sealed")
        wire = observation.to_dict()
        normalized_associations = _validate_associations(associations)
        key = _idempotency_key(observation, wire, idempotency_key)
        payload_bytes = (
            _canonical_bytes({"observation": wire, "associations": normalized_associations})
            if normalized_associations else _canonical_bytes(wire)
        )
        digest = hashlib.sha256(payload_bytes).hexdigest()
        entry = {
            "schema_version": 1,
            "kind": "observation",
            "idempotency_key": key,
            "payload_sha256": digest,
            "observation": wire,
        }
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
                prior_digest = previous[0].get("payload_sha256")
                if prior_digest == digest:
                    return LedgerAppendResult("duplicate", duplicates=1, idempotency_key=key)
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
                allowed = {"schema_version", "kind", "idempotency_key", "payload_sha256", "observation", "associations"}
                if (set(row) not in (allowed, allowed - {"associations"})
                        or not isinstance(row.get("idempotency_key"), str)
                        or not row["idempotency_key"]
                        or len(row["idempotency_key"]) > _MAX_IDEMPOTENCY_CHARS + len("caller:")
                        or not isinstance(row.get("payload_sha256"), str)
                        or not _SHA256_RE.fullmatch(row["payload_sha256"])
                        or not isinstance(row.get("observation"), dict)):
                    raise LedgerCorrupt("ledger-invalid-observation-record")
                has_associations = "associations" in row
                associations = _validate_associations(tuple(row.get("associations", [])))
                digest_payload = (
                    {"observation": row["observation"], "associations": associations}
                    if has_associations else row["observation"]
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
    return "content:" + hashlib.sha256(_canonical_bytes(wire)).hexdigest()


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
