"""#842 execution qualification 的候選、人工核可與 roster 生命週期。

本模組只消費版本化 report/profile 檔案；不載入 PatchMUD runtime，不把評測
pass、profile key 或 ``--apply`` 當成核可。CAS index 是唯一狀態權威；候選、
receipt 是不可變證據，approved roster 是可重建投影。
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable, Iterator, Mapping
import uuid

from ..config import paths as config_paths
from . import execution_adapters, execution_profile, model_resolution


QUALIFICATION_SCHEMA_VERSION = 1
QUALIFICATION_MAPPING_VERSION = "execution-qualification-report-mapping/v1"
_PROFILE_KEY_RE = re.compile(r"epk:v1:resolved:[0-9a-f]{64}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CANDIDATE_ID_RE = re.compile(r"qcan:v1:[0-9a-f]{64}\Z")
_RECEIPT_ID_RE = re.compile(r"qrcpt:v1:[0-9a-f]{64}\Z")
_OPERATOR_RECEIPT_ID_RE = re.compile(r"hqrcpt:v1:[0-9a-f]{64}\Z")
_MAX_STATE_BYTES = 64 * 1024 * 1024
_REPORT_ROLE_FOR_RUNTIME_ROLE = {
    "planning": "planner",
    "build": "builder",
    "review": "reviewer",
}
_RUNTIME_ROLES = frozenset(_REPORT_ROLE_FOR_RUNTIME_ROLE)
_INDEX_KEYS = frozenset(
    {
        "schema_version",
        "revision",
        "candidates",
        "receipts",
        "bindings",
        "idempotency",
        "legacy_migrations",
        "clock_watermarks",
    }
)
_OPERATOR_RECEIPT_INDEX_KEYS = frozenset({"schema_version", "receipts"})


class QualificationError(ValueError):
    """qualification evidence 或狀態不符合版本化契約。"""


class QualificationConflict(QualificationError):
    """expected revision 或 idempotency key 與目前狀態衝突。"""


@dataclass(frozen=True)
class QualificationStorePaths:
    """Trust Root 登記的 qualification state 與 operator receipt 落點。"""

    candidates_root: Path
    receipts_root: Path
    operator_receipts_root: Path
    operator_receipt_registry_path: Path
    roster_path: Path
    index_path: Path

    @property
    def root(self) -> Path:
        return self.index_path.parent


def _default_paths() -> QualificationStorePaths:
    return QualificationStorePaths(
        candidates_root=config_paths.execution_qualification_candidates_root(),
        receipts_root=config_paths.execution_qualification_receipts_root(),
        operator_receipts_root=config_paths.execution_qualification_operator_receipts_root(),
        operator_receipt_registry_path=config_paths.execution_qualification_operator_receipt_registry_path(),
        roster_path=config_paths.execution_qualification_roster_path(),
        index_path=config_paths.execution_qualification_index_path(),
    )


def _paths_under(root: Path) -> QualificationStorePaths:
    base = Path(root)
    return QualificationStorePaths(
        candidates_root=base / "candidates",
        receipts_root=base / "receipts",
        operator_receipts_root=base / "operator-receipts",
        operator_receipt_registry_path=base / "operator-receipt-index.json",
        roster_path=base / "approved-roster.json",
        index_path=base / "index.json",
    )


def _canonical_bytes(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise QualificationError("qualification payload must be finite JSON") from exc


def _sha256(payload: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualificationError(f"{field} must be a non-empty string")
    return value.strip()


def _timestamp(value: object, field: str) -> datetime:
    text = _nonempty(value, f"{field} timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise QualificationError(f"{field} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QualificationError(f"{field} timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_now(now: object | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise QualificationError("now timestamp must include a timezone")
        return now.astimezone(timezone.utc)
    return _timestamp(now, "now")


def _binding_key(executor: str, model_id: str, profile_key: str, role: str) -> str:
    return "qbind:v1:" + hashlib.sha256(
        _canonical_bytes([executor, model_id, profile_key, role])
    ).hexdigest()


def _empty_index() -> dict[str, object]:
    return {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "revision": 0,
        "candidates": {},
        "receipts": {},
        "bindings": {},
        "idempotency": {},
        "legacy_migrations": {},
        "clock_watermarks": {},
    }


def _safe_read_json(path: Path, *, label: str, allow_group_acl: bool = False) -> object:
    raw = _safe_read_bytes(path, label=label, allow_group_acl=allow_group_acl)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"{label} is unreadable") from exc


def _safe_read_bytes(path: Path, *, label: str, allow_group_acl: bool = False) -> bytes:
    """以 no-follow、單一 inode、bounded read 取回 manager-only state。"""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise QualificationError(f"{label} is unreadable") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise QualificationError(f"{label} must be a single-link regular file")
        exposed_bits = stat.S_IMODE(before.st_mode) & (0o007 if allow_group_acl else 0o077)
        if exposed_bits:
            raise QualificationError(f"{label} permissions are too broad")
        if before.st_size > _MAX_STATE_BYTES:
            raise QualificationError(f"{label} exceeds the bounded file size")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, _MAX_STATE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_STATE_BYTES:
                raise QualificationError(f"{label} exceeds the bounded file size")
        after = os.fstat(fd)
        before_identity = (
            before.st_dev, before.st_ino, before.st_nlink, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns, stat.S_IMODE(before.st_mode),
        )
        after_identity = (
            after.st_dev, after.st_ino, after.st_nlink, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns, stat.S_IMODE(after.st_mode),
        )
        if before_identity != after_identity or total != after.st_size:
            raise QualificationError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise QualificationError(f"qualification state directory is not a directory: {path.name}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise QualificationError(f"qualification state directory permissions are too broad: {path.name}")


def _ensure_governed_dir(path: Path) -> None:
    """Check a Trust Root provisioned directory without chmod that could erase ACLs."""
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise QualificationError(f"governed qualification directory is not provisioned: {path.name}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise QualificationError(f"governed qualification path is not a directory: {path.name}")
    if stat.S_IMODE(info.st_mode) & 0o007:
        raise QualificationError(f"governed qualification directory is other-accessible: {path.name}")


def _atomic_write_governed_json(path: Path, payload: object) -> None:
    """Atomically create a content-addressed operator receipt while preserving inherited ACLs."""
    data = _canonical_bytes(payload) + b"\n"
    if len(data) > _MAX_STATE_BYTES:
        raise QualificationError("qualification state exceeds the bounded file size")
    _ensure_governed_dir(path.parent)
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = -1
    try:
        parent_info = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_info.st_mode):
            raise QualificationError("operator receipt parent changed during write")
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o660,
            dir_fd=parent_fd,
        )
        os.fchmod(fd, 0o660)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(
                temporary,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = _safe_read_json(path, label="operator qualification receipt", allow_group_acl=True)
            if existing != payload:
                raise QualificationConflict("immutable operator receipt already exists with different bytes")
        finally:
            os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _atomic_write_json(path: Path, payload: object, *, governed_parent: bool = False) -> None:
    data = _canonical_bytes(payload) + b"\n"
    if len(data) > _MAX_STATE_BYTES:
        raise QualificationError("qualification state exceeds the bounded file size")
    if governed_parent:
        _ensure_governed_dir(path.parent)
    else:
        _ensure_private_dir(path.parent)
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _validate_profile_key(value: object) -> str:
    profile_key = _nonempty(value, "profile_key")
    if _PROFILE_KEY_RE.fullmatch(profile_key) is None:
        raise QualificationError("profile_key must be an epk:v1:resolved key")
    return profile_key


def _validate_sha256(value: object, field: str) -> str:
    digest = _nonempty(value, field)
    if _SHA256_RE.fullmatch(digest) is None:
        raise QualificationError(f"{field} must be sha256:<64 lowercase hex>")
    return digest


def _candidate_approval_blockers(candidate: Mapping[str, object]) -> list[str]:
    """只有技術證據完整的 candidate 才能由 receipt 發布。"""

    blockers: list[str] = []
    coverage = candidate.get("coverage", {})
    expected = coverage.get("expected_encounters", []) if isinstance(coverage, Mapping) else []
    observed = coverage.get("observed_encounters", []) if isinstance(coverage, Mapping) else []
    if (
        not isinstance(coverage, Mapping)
        or coverage.get("state") != "complete"
        or not isinstance(expected, list)
        or not expected
        or not isinstance(observed, list)
        or any(not isinstance(value, str) or not value for value in expected + observed)
        or len(set(expected)) != len(expected)
        or len(set(observed)) != len(observed)
        or set(expected) != set(observed)
    ):
        blockers.append("coverage-incomplete-or-unknown")
    profile = candidate.get("profile_observation", {})
    actual_key = profile.get("actual_key") if isinstance(profile, Mapping) else None
    if (
        not isinstance(profile, Mapping)
        or profile.get("state") != "complete"
        or profile.get("resolved_key") != candidate.get("profile_key")
        or not isinstance(actual_key, str)
        or re.fullmatch(r"epk:v1:actual:[0-9a-f]{64}", actual_key) is None
    ):
        blockers.append("observed-profile-incomplete")
    measurement = candidate.get("measurement", {})
    if not isinstance(measurement, Mapping) or measurement.get("verdict") != "pass":
        blockers.append("report-verdict-not-pass")
    unknown = measurement.get("unknown_dimensions", {}) if isinstance(measurement, Mapping) else {}
    subject = candidate.get("subject", {})
    report_role = subject.get("report_role") if isinstance(subject, Mapping) else None
    if isinstance(unknown, Mapping) and report_role in unknown:
        blockers.append("role-observed-dimensions-unknown")
    return blockers


class QualificationStore:
    """CAS-backed qualification lifecycle store.

    ``root`` is a test seam. Production uses four manager-only Trust Root paths.
    Failpoints are likewise an explicit deterministic crash-test seam.
    """

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._test_root = root is not None
        self.paths = _paths_under(Path(root)) if root is not None else _default_paths()
        self._failpoint = failpoint

    @property
    def revision(self) -> int:
        with self._locked():
            return int(self._load_index_unlocked()["revision"])

    def _hit(self, name: str) -> None:
        if self._failpoint is not None:
            self._failpoint(name)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self._test_root:
            _ensure_private_dir(self.paths.root)
        else:
            # The execution-qualification parent may carry an operator traverse ACL. Do not
            # chmod it on every manager transaction; that would reset the generated ACL mask.
            _ensure_governed_dir(self.paths.root)
        _ensure_private_dir(self.paths.candidates_root)
        _ensure_private_dir(self.paths.receipts_root)
        lock_path = self.paths.index_path.with_name(self.paths.index_path.name + ".lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise QualificationError("qualification lock must be a single-link regular file")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load_index_unlocked(self) -> dict[str, object]:
        if not self.paths.index_path.exists():
            index = _empty_index()
            _atomic_write_json(self.paths.index_path, index, governed_parent=not self._test_root)
            return index
        payload = _safe_read_json(self.paths.index_path, label="qualification index")
        if not isinstance(payload, dict) or set(payload) != _INDEX_KEYS:
            raise QualificationError("qualification index schema/keys are invalid")
        if (
            type(payload.get("schema_version")) is not int
            or payload["schema_version"] != QUALIFICATION_SCHEMA_VERSION
            or type(payload.get("revision")) is not int
            or payload["revision"] < 0
        ):
            raise QualificationError("qualification index version/revision is invalid")
        for field in (
            "candidates", "receipts", "bindings", "idempotency",
            "legacy_migrations", "clock_watermarks",
        ):
            if not isinstance(payload.get(field), dict):
                raise QualificationError(f"qualification index {field} must be an object")
        return payload

    def _load_operator_receipt_index_unlocked(self, *, create: bool = False) -> dict[str, object]:
        path = self.paths.operator_receipt_registry_path
        if not path.exists():
            index: dict[str, object] = {"schema_version": 1, "receipts": {}}
            if create:
                _atomic_write_json(path, index, governed_parent=not self._test_root)
            return index
        payload = _safe_read_json(path, label="operator receipt registry")
        if (
            not isinstance(payload, dict)
            or set(payload) != _OPERATOR_RECEIPT_INDEX_KEYS
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("receipts"), dict)
        ):
            raise QualificationError("operator receipt registry schema/keys are invalid")
        for receipt_id, row in payload["receipts"].items():
            if (
                not isinstance(receipt_id, str)
                or _OPERATOR_RECEIPT_ID_RE.fullmatch(receipt_id) is None
                or not isinstance(row, Mapping)
                or set(row) != {"digest", "candidate_id", "verdict"}
                or not isinstance(row.get("digest"), str)
                or _SHA256_RE.fullmatch(row["digest"]) is None
                or not isinstance(row.get("candidate_id"), str)
                or not isinstance(row.get("verdict"), str)
            ):
                raise QualificationError("operator receipt registry entry is invalid")
        return payload

    def _write_index_unlocked(self, index: dict[str, object]) -> None:
        _atomic_write_json(self.paths.index_path, index, governed_parent=not self._test_root)

    def _read_immutable(
        self,
        root: Path,
        item_id: str,
        *,
        id_pattern: re.Pattern[str],
        label: str,
        expected_digest: str | None = None,
    ) -> dict[str, object]:
        if id_pattern.fullmatch(item_id) is None:
            raise QualificationError(f"{label} id is invalid")
        path = root / f"{item_id}.json"
        payload = _safe_read_json(path, label=label)
        if not isinstance(payload, dict) or set(payload) != {"payload", "digest"}:
            raise QualificationError(f"{label} envelope is invalid")
        if not isinstance(payload["payload"], dict):
            raise QualificationError(f"{label} payload is invalid")
        digest = _sha256(payload["payload"])
        if payload.get("digest") != digest or (
            expected_digest is not None and digest != expected_digest
        ):
            raise QualificationError(f"{label} digest mismatch")
        return payload["payload"]

    def _write_immutable(
        self,
        root: Path,
        item_id: str,
        payload: dict[str, object],
        *,
        id_pattern: re.Pattern[str],
        label: str,
    ) -> str:
        if id_pattern.fullmatch(item_id) is None:
            raise QualificationError(f"{label} id is invalid")
        digest = _sha256(payload)
        path = root / f"{item_id}.json"
        envelope = {"payload": payload, "digest": digest}
        if path.exists():
            current = _safe_read_json(path, label=label)
            if current != envelope:
                raise QualificationConflict(f"immutable {label} already exists with different bytes")
            return digest
        _atomic_write_json(path, envelope)
        return digest

    def _candidate(self, index: Mapping[str, object], candidate_id: str) -> dict[str, object]:
        entries = index["candidates"]
        row = entries.get(candidate_id) if isinstance(entries, dict) else None
        if not isinstance(row, dict) or not isinstance(row.get("digest"), str):
            raise QualificationError("qualification candidate is unknown")
        return self._read_immutable(
            self.paths.candidates_root,
            candidate_id,
            id_pattern=_CANDIDATE_ID_RE,
            label="qualification candidate",
            expected_digest=row["digest"],
        )

    def _receipt(self, index: Mapping[str, object], receipt_id: str) -> dict[str, object]:
        entries = index["receipts"]
        row = entries.get(receipt_id) if isinstance(entries, dict) else None
        if not isinstance(row, dict) or not isinstance(row.get("digest"), str):
            raise QualificationError("qualification receipt is unknown")
        return self._read_immutable(
            self.paths.receipts_root,
            receipt_id,
            id_pattern=_RECEIPT_ID_RE,
            label="qualification receipt",
            expected_digest=row["digest"],
        )

    def _read_operator_receipt(
        self, receipt_id: str, *, expected_digest: str | None = None
    ) -> tuple[dict[str, object], str]:
        if not isinstance(receipt_id, str) or _OPERATOR_RECEIPT_ID_RE.fullmatch(receipt_id) is None:
            raise QualificationError("operator receipt id is invalid")
        _ensure_governed_dir(self.paths.operator_receipts_root)
        registry = self._load_operator_receipt_index_unlocked()
        registry_entry = registry["receipts"].get(receipt_id)
        if not isinstance(registry_entry, Mapping):
            raise QualificationError("operator receipt is not registered")
        trusted_digest = registry_entry.get("digest")
        if not isinstance(trusted_digest, str) or (
            expected_digest is not None and expected_digest != trusted_digest
        ):
            raise QualificationError("operator qualification receipt registry digest mismatch")
        envelope = _safe_read_json(
            self.paths.operator_receipts_root / f"{receipt_id}.json",
            label="operator qualification receipt",
            allow_group_acl=True,
        )
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "digest"}:
            raise QualificationError("operator qualification receipt envelope is invalid")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise QualificationError("operator qualification receipt payload is invalid")
        digest = _sha256(payload)
        if envelope.get("digest") != digest or trusted_digest != digest or (
            expected_digest is not None and expected_digest != digest
        ):
            raise QualificationError("operator qualification receipt digest mismatch")
        without_id = {key: value for key, value in payload.items() if key != "receipt_id"}
        expected_id = "hqrcpt:v1:" + hashlib.sha256(_canonical_bytes(without_id)).hexdigest()
        if payload.get("receipt_id") != receipt_id or expected_id != receipt_id:
            raise QualificationError("operator qualification receipt identity mismatch")
        required = {
            "schema_version", "kind", "candidate_id", "candidate_digest", "report_digest",
            "profile_digest", "profile_key", "role", "coverage", "verdict", "reviewer",
            "actor", "reason", "policy_revision", "reviewed_at", "expires_at", "test_only",
            "receipt_id", "binding_generation",
        }
        optional = {"approval_receipt_id", "approval_receipt_digest"}
        if not required <= set(payload) or set(payload) - required - optional:
            raise QualificationError("operator qualification receipt fields are invalid")
        if (
            type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1
            or payload.get("kind") != "qualification-operator-review"
            or payload.get("test_only") is not False
            or payload.get("verdict") not in {"approved", "revoked"}
            or payload.get("reviewer") != payload.get("actor")
            or registry_entry.get("candidate_id") != payload.get("candidate_id")
            or registry_entry.get("verdict") != payload.get("verdict")
            or type(payload.get("binding_generation")) is not int
            or payload.get("binding_generation") < 0
        ):
            raise QualificationError("operator qualification receipt identity is invalid")
        _nonempty(payload.get("reviewer"), "operator receipt reviewer")
        _nonempty(payload.get("reason"), "operator receipt reason")
        _nonempty(payload.get("policy_revision"), "operator receipt policy_revision")
        reviewed = _timestamp(payload.get("reviewed_at"), "operator receipt reviewed_at")
        expires = _timestamp(payload.get("expires_at"), "operator receipt expires_at")
        if expires <= reviewed:
            raise QualificationError("operator receipt expiry must follow reviewed_at")
        return payload, digest

    def _validate_operator_receipt(
        self,
        receipt_id: str,
        candidate: Mapping[str, object],
        candidate_digest: str,
        *,
        verdict: str,
        expected_digest: str | None = None,
        approval_receipt_id: str | None = None,
        approval_receipt_digest: str | None = None,
        expected_binding_generation: int | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, object], str]:
        payload, digest = self._read_operator_receipt(receipt_id, expected_digest=expected_digest)
        source = candidate.get("source")
        subject = candidate.get("subject")
        coverage = candidate.get("coverage")
        if (
            not isinstance(source, Mapping)
            or not isinstance(subject, Mapping)
            or not isinstance(coverage, Mapping)
            or candidate.get("test_only") is not False
            or payload.get("candidate_id") != candidate.get("candidate_id")
            or payload.get("candidate_digest") != candidate_digest
            or payload.get("report_digest") != source.get("report_digest")
            or payload.get("profile_digest") != _sha256(candidate.get("profile_observation"))
            or payload.get("profile_key") != candidate.get("profile_key")
            or payload.get("role") != subject.get("role")
            or payload.get("coverage") != coverage.get("state")
            or payload.get("verdict") != verdict
        ):
            raise QualificationError("operator receipt binding mismatch")
        if verdict == "revoked" and (
            payload.get("approval_receipt_id") != approval_receipt_id
            or payload.get("approval_receipt_digest") != approval_receipt_digest
        ):
            raise QualificationError("operator revocation receipt binding mismatch")
        if verdict == "approved" and ("approval_receipt_id" in payload or "approval_receipt_digest" in payload):
            raise QualificationError("operator approval receipt has unexpected revocation binding")
        if (
            expected_binding_generation is not None
            and payload.get("binding_generation") != expected_binding_generation
        ):
            # 這張人類 receipt 綁定的 binding generation 已被後續 approve／revoke 推進；
            # 撤銷或到期的世代不可再被舊 receipt 重播復活，必須換發綁新世代的 receipt。
            raise QualificationError(
                "operator receipt is bound to a superseded qualification generation"
            )
        if now is not None:
            reviewed = _timestamp(payload.get("reviewed_at"), "operator receipt reviewed_at")
            expires = _timestamp(payload.get("expires_at"), "operator receipt expires_at")
            if reviewed > now:
                raise QualificationError("operator receipt reviewed_at timestamp is in the future")
            if now >= expires:
                raise QualificationError("operator receipt is expired")
        return payload, digest

    def issue_operator_receipt(
        self,
        candidate_id: str,
        *,
        verdict: str,
        actor: str,
        reason: str,
        policy_revision: str,
        reviewed_at: str,
        expires_at: str,
        now: object | None = None,
    ) -> dict[str, object]:
        """Create an immutable live receipt for a confirmed operator CLI action."""
        actor = _nonempty(actor, "actor")
        reason = _nonempty(reason, "reason")
        policy_revision = _nonempty(policy_revision, "policy_revision")
        reviewed = _timestamp(reviewed_at, "reviewed_at")
        expires = _timestamp(expires_at, "expires_at")
        current_time = _parse_now(now)
        if verdict not in {"approved", "revoked"}:
            raise QualificationError("operator receipt verdict must be approved or revoked")
        if expires <= reviewed:
            raise QualificationError("expires_at must be after reviewed_at")
        if reviewed > current_time:
            raise QualificationError("reviewed_at timestamp is in the future")
        if expires <= current_time:
            raise QualificationError("expires_at timestamp is already expired")

        with self._locked():
            index = self._load_index_unlocked()
            candidate = self._candidate(index, candidate_id)
            candidate_row = index["candidates"].get(candidate_id)
            if not isinstance(candidate_row, Mapping) or not isinstance(candidate_row.get("digest"), str):
                raise QualificationError("qualification candidate digest is unavailable")
            if candidate.get("test_only") is not False:
                raise QualificationError("operator receipts cannot be issued for test-only candidates")
            if verdict == "approved":
                blockers = _candidate_approval_blockers(candidate)
                if blockers:
                    raise QualificationError(
                        "candidate is not eligible for approved roster: " + ", ".join(blockers)
                    )
            binding_id = _binding_key(
                candidate["subject"]["executor"], candidate["subject"]["model_id"],
                candidate["profile_key"], candidate["subject"]["role"],
            )
            binding = index["bindings"].get(binding_id)
            if not isinstance(binding, Mapping):
                raise QualificationError("qualification candidate has no lifecycle binding")
            # 綁定核發當下的 binding generation；之後任何 approve／revoke 都會推進世代，
            # 使這張人類 receipt 的雜湊與內容都與新世代不同，重跑相同參數不會拿回舊 receipt。
            binding_generation = int(binding.get("generation", 0))
            approval_receipt_id = None
            approval_receipt_digest = None
            if verdict == "revoked":
                if binding.get("candidate_id") != candidate_id or binding.get("state") != "approved":
                    raise QualificationConflict("candidate is not the current approved qualification")
                approval_receipt_id = binding.get("approval_receipt_id")
                approval_meta = index["receipts"].get(approval_receipt_id)
                if not isinstance(approval_receipt_id, str) or not isinstance(approval_meta, Mapping):
                    raise QualificationError("current approval receipt is unavailable")
                approval_receipt_digest = approval_meta.get("digest")
                if not isinstance(approval_receipt_digest, str):
                    raise QualificationError("current approval receipt digest is unavailable")
            draft: dict[str, object] = {
                "schema_version": 1,
                "kind": "qualification-operator-review",
                "candidate_id": candidate_id,
                "candidate_digest": candidate_row["digest"],
                "report_digest": candidate["source"]["report_digest"],
                "profile_digest": _sha256(candidate["profile_observation"]),
                "profile_key": candidate["profile_key"],
                "role": candidate["subject"]["role"],
                "coverage": candidate["coverage"]["state"],
                "verdict": verdict,
                "reviewer": actor,
                "actor": actor,
                "reason": reason,
                "policy_revision": policy_revision,
                "reviewed_at": _timestamp_text(reviewed),
                "expires_at": _timestamp_text(expires),
                "test_only": False,
                "binding_generation": binding_generation,
            }
            if verdict == "revoked":
                draft["approval_receipt_id"] = approval_receipt_id
                draft["approval_receipt_digest"] = approval_receipt_digest
            receipt_id = "hqrcpt:v1:" + hashlib.sha256(_canonical_bytes(draft)).hexdigest()
            payload = {**draft, "receipt_id": receipt_id}
            digest = _sha256(payload)
            if self._test_root:
                _ensure_private_dir(self.paths.operator_receipts_root)
            _atomic_write_governed_json(
                self.paths.operator_receipts_root / f"{receipt_id}.json",
                {"payload": payload, "digest": digest},
            )
            receipt_index = self._load_operator_receipt_index_unlocked(create=True)
            receipt_entries = receipt_index["receipts"]
            assert isinstance(receipt_entries, dict)
            registered = receipt_entries.get(receipt_id)
            registration = {
                "digest": digest,
                "candidate_id": candidate_id,
                "verdict": verdict,
            }
            if registered is not None and registered != registration:
                raise QualificationConflict("operator receipt id is already registered differently")
            if registered is None:
                receipt_entries[receipt_id] = registration
                _atomic_write_json(
                    self.paths.operator_receipt_registry_path,
                    receipt_index,
                    governed_parent=not self._test_root,
                )
            return {
                "candidate_id": candidate_id,
                "operator_receipt_id": receipt_id,
                "operator_receipt_digest": digest,
                "verdict": verdict,
                "reviewer": actor,
                "expires_at": payload["expires_at"],
            }

    def _replay(
        self,
        index: Mapping[str, object],
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, object] | None:
        entries = index["idempotency"]
        existing = entries.get(idempotency_key) if isinstance(entries, dict) else None
        if existing is None:
            return None
        if not isinstance(existing, dict) or existing.get("request_digest") != request_digest:
            raise QualificationConflict("idempotency key was already used for a different request")
        result = existing.get("result")
        if not isinstance(result, dict):
            raise QualificationError("idempotency result is invalid")
        return deepcopy(result)

    @staticmethod
    def _check_expected_revision(index: Mapping[str, object], expected_revision: object) -> int:
        if type(expected_revision) is not int or expected_revision < 0:
            raise QualificationError("expected_revision must be a non-negative integer")
        actual = index["revision"]
        if expected_revision != actual:
            raise QualificationConflict(
                f"qualification revision conflict: expected {expected_revision}, current {actual}"
            )
        return actual

    def _candidate_from_report(
        self,
        report: object,
        *,
        source_revision: str,
        source_artifact_digest: str,
        profile_key: str,
        executor: str,
        model_id: str,
        role: str,
        profile_binding: object | None,
        profile_source_revision: str | None,
        profile_source_artifact_digest: str | None,
        test_only: bool,
    ) -> dict[str, object]:
        if not isinstance(report, Mapping):
            raise QualificationError("PatchMUD report must be an object")
        source_revision = _nonempty(source_revision, "source_revision")
        if re.fullmatch(r"[0-9a-f]{7,64}", source_revision) is None:
            raise QualificationError("source_revision must be a lowercase git revision")
        artifact_digest = _validate_sha256(source_artifact_digest, "source_artifact_digest")
        profile_key = _validate_profile_key(profile_key)
        executor = _nonempty(executor, "executor")
        model_id = _nonempty(model_id, "model_id")
        role = _nonempty(role, "role")
        if role not in _RUNTIME_ROLES:
            raise QualificationError("role is not a supported execution capability")
        report_role = _REPORT_ROLE_FOR_RUNTIME_ROLE[role]
        producer = report.get("producer")
        if not isinstance(producer, Mapping):
            raise QualificationError("PatchMUD report producer provenance is missing")
        patchmud_version = _nonempty(producer.get("version"), "producer.version")
        leaderboards = report.get("leaderboards")
        clear_rate = leaderboards.get("clear_rate") if isinstance(leaderboards, Mapping) else None
        rows = clear_rate.get("rows") if isinstance(clear_rate, Mapping) else None
        if not isinstance(rows, list):
            raise QualificationError("PatchMUD report clear_rate rows are missing")
        matches = [
            row for row in rows
            if isinstance(row, Mapping)
            and row.get("profile_id") == profile_key
            and row.get("role") == report_role
        ]
        if len(matches) != 1:
            profile_rows = [
                row for row in rows
                if isinstance(row, Mapping) and row.get("profile_id") == profile_key
            ]
            if profile_rows and any(row.get("role") != report_role for row in profile_rows):
                raise QualificationError("report role does not match requested execution role")
            if not profile_rows:
                raise QualificationError("report does not match exact resolved profile")
            raise QualificationError("PatchMUD report must contain one exact profile/role row")
        row = matches[0]
        for field in ("benchmark_type", "deck_digest", "evaluator_revision", "model", "deck_id", "loadout"):
            _nonempty(row.get(field), f"report clear_rate.{field}")
        if row["model"] != f"{executor}:{model_id}":
            raise QualificationError("report model does not match exact executor/model identity")
        expected_encounters = row.get("coverage_expected_encounters")
        row_observed = row.get("coverage_observed_encounters")
        summary_coverage_valid = (
            isinstance(expected_encounters, list)
            and isinstance(row_observed, list)
            and all(isinstance(value, str) and value for value in expected_encounters)
            and all(isinstance(value, str) and value for value in row_observed)
            and len(set(expected_encounters)) == len(expected_encounters)
            and len(set(row_observed)) == len(row_observed)
        )
        if not summary_coverage_valid:
            expected_encounters, row_observed = [], []

        runs = report.get("runs")
        if not isinstance(runs, list):
            raise QualificationError("PatchMUD report runs are missing")
        exact_runs = [
            item for item in runs
            if isinstance(item, Mapping) and item.get("profile_id") == profile_key
        ]
        if not exact_runs:
            raise QualificationError("PatchMUD report has no run bound to the exact profile")
        expected_set = set(expected_encounters)
        observed_set = set(row_observed)
        run_expected: set[str] = set()
        run_observed: set[str] = set()
        per_run_complete = True
        unknown_dimensions: dict[str, object] = {}
        measured_dimensions: set[str] = set()
        for position, item in enumerate(exact_runs):
            for field in (
                "role", "benchmark_type", "deck_digest", "evaluator_revision", "model", "loadout"
            ):
                if item.get(field) != row.get(field):
                    raise QualificationError(f"report run {position} {field} does not match its cohort")
            coverage = item.get("deck_coverage")
            if not isinstance(coverage, Mapping):
                per_run_complete = False
                continue
            if coverage.get("complete") is not True:
                per_run_complete = False
            expected = coverage.get("expected_encounters")
            observed = coverage.get("observed_encounters")
            valid_coverage = (
                isinstance(expected, list)
                and isinstance(observed, list)
                and all(isinstance(value, str) and value for value in expected)
                and all(isinstance(value, str) and value for value in observed)
                and len(set(expected)) == len(expected)
                and len(set(observed)) == len(observed)
            )
            if not valid_coverage:
                per_run_complete = False
            else:
                if set(expected) != set(observed):
                    per_run_complete = False
                run_expected.update(value for value in expected if isinstance(value, str))
                run_observed.update(value for value in observed if isinstance(value, str))
            dims = item.get("measured_dimensions")
            if isinstance(dims, list):
                measured_dimensions.update(value for value in dims if isinstance(value, str))
            unmeasured = item.get("unmeasured_dimensions")
            if isinstance(unmeasured, Mapping):
                unknown_dimensions.update(unmeasured)
        if (
            row.get("coverage_complete") is True
            and summary_coverage_valid
            and expected_set
            and expected_set == observed_set == run_expected == run_observed
            and per_run_complete
        ):
            coverage_state = "complete"
        elif expected_set and (observed_set or run_observed):
            coverage_state = "incomplete"
        else:
            coverage_state = "unknown"

        deck = {
            "deck_id": row["deck_id"],
            "content_sha256": str(row["deck_digest"]).removeprefix("sha256:"),
            "encounter_count": len(expected_encounters),
            "measured_personas": [report_role] if report_role in measured_dimensions else [],
        }
        envelope_context = {
            "executor": executor,
            "model_id": model_id,
            "persona": report_role,
            "deck": deck,
            "patchmud_version": patchmud_version,
            "role": report_role,
            "benchmark_type": row["benchmark_type"],
            "deck_digest": row["deck_digest"],
            "evaluator_revision": row["evaluator_revision"],
        }
        try:
            consumed = execution_adapters.profile_report_consumer(
                report,
                expected_profile_key=profile_key,
                source_revision=source_revision,
                source_digest=report.get("report_fingerprint"),
                envelope_context=envelope_context,
            )
        except (TypeError, ValueError, execution_adapters.ExecutionAdapterError) as exc:
            raise QualificationError(f"profile report consumer rejected source: {exc}") from exc

        profile_record: dict[str, object]
        if profile_binding is None:
            profile_record = {
                "state": "unknown",
                "reason": "execution-profile-binding-missing",
                "requested": None,
                "resolved": None,
                "observed": None,
                "resolved_key": None,
                "actual_key": None,
                "source_revision": None,
                "source_artifact_digest": None,
            }
        elif isinstance(profile_binding, Mapping) and "profile_id" in profile_binding:
            # PatchMUD execution-profile/v1 producer payload。使用既有 #835 schema-core
            # 驗證 descriptor、三個 plane 與 key，不把 producer schema 改寫成 Cortex binding。
            try:
                if type(profile_binding.get("schema_version")) is not int or profile_binding["schema_version"] != 1:
                    raise ValueError("unsupported PatchMUD execution-profile schema")
                descriptor = execution_profile.parse_descriptor(profile_binding["descriptor"])
                requested = execution_profile.parse_profile(profile_binding["requested"], descriptor)
                resolved = execution_profile.parse_profile(profile_binding["resolved"], descriptor)
                observed = execution_profile.parse_profile(profile_binding["observed"], descriptor)
                request_key = execution_profile.profile_key(requested)
                resolved_key = execution_profile.profile_key(resolved)
                actual_key = execution_profile.actual_condition_key(observed)
            except (execution_profile.ExecutionProfileError, TypeError, ValueError) as exc:
                raise QualificationError("PatchMUD execution profile binding is invalid") from exc
            if (
                (requested.plane, resolved.plane, observed.plane) != ("requested", "resolved", "observed")
                or profile_binding.get("requested_key") != request_key
                or profile_binding.get("resolved_key") != resolved_key
                or profile_binding.get("actual_condition_key") != actual_key
                or profile_binding.get("profile_id") != resolved_key
            ):
                raise QualificationError("PatchMUD execution profile binding key/plane mismatch")
            if resolved_key != profile_key:
                raise QualificationError("execution profile binding does not match exact resolved profile")
            try:
                adapter = execution_adapters.adapter_for(executor)
            except execution_adapters.ExecutionAdapterError as exc:
                raise QualificationError("execution profile adapter has no registered executor") from exc
            if descriptor.adapter != adapter.descriptor_fields():
                raise QualificationError("execution profile adapter/version does not match executor")
            resolved_model = resolved.conditions.get("model", {})
            if (
                resolved_model.get("state") == "known"
                and resolved_model.get("value", {}).get("id") != model_id
            ):
                raise QualificationError("execution profile model does not match report identity")
            resolved_loadout = resolved.conditions.get("loadout", {})
            if (
                resolved_loadout.get("state") == "known"
                and resolved_loadout.get("value", {}).get("id") != row["loadout"]
            ):
                raise QualificationError("execution profile loadout does not match report cohort")
            role_requirement = resolved.requirements.get("role", {})
            if role_requirement.get("state") == "known" and role_requirement.get("value") != role:
                raise QualificationError("execution profile binding role does not match report")
            source_profile_revision = _nonempty(
                profile_source_revision or source_revision, "profile_source_revision"
            )
            profile_digest = _validate_sha256(
                profile_source_artifact_digest or _sha256(profile_binding),
                "profile_source_artifact_digest",
            )
            mismatches = [
                name for name in resolved.conditions
                if resolved.conditions.get(name) != observed.conditions.get(name)
            ]
            observation_state = "complete" if actual_key is not None and not mismatches else "unknown"
            profile_record = {
                "state": observation_state,
                "reason": None if observation_state == "complete" else (
                    "observed-conditions-unknown" if actual_key is None
                    else "observed-conditions-mismatch:" + ",".join(mismatches)
                ),
                "requested": requested.to_dict(),
                "resolved": resolved.to_dict(),
                "observed": observed.to_dict(),
                "resolved_key": resolved_key,
                "actual_key": actual_key,
                "source_revision": source_profile_revision,
                "source_artifact_digest": profile_digest,
            }
        else:
            try:
                binding = execution_adapters.load_profile_binding(profile_binding)
            except (TypeError, ValueError, execution_adapters.ExecutionAdapterError) as exc:
                raise QualificationError("execution profile binding is invalid") from exc
            if binding.resolved_key != profile_key:
                raise QualificationError("execution profile binding does not match exact resolved profile")
            try:
                adapter = execution_adapters.adapter_for(executor)
            except execution_adapters.ExecutionAdapterError as exc:
                raise QualificationError("execution profile adapter has no registered executor") from exc
            if binding.descriptor.adapter != adapter.descriptor_fields():
                raise QualificationError("execution profile adapter/version does not match executor")
            resolved_model = binding.resolved.conditions.get("model", {})
            if (
                resolved_model.get("state") == "known"
                and resolved_model.get("value", {}).get("id") != model_id
            ):
                raise QualificationError("execution profile model does not match report identity")
            resolved_loadout = binding.resolved.conditions.get("loadout", {})
            if (
                resolved_loadout.get("state") == "known"
                and resolved_loadout.get("value", {}).get("id") != row["loadout"]
            ):
                raise QualificationError("execution profile loadout does not match report cohort")
            role_requirement = binding.resolved.requirements.get("role", {})
            if role_requirement.get("state") == "known" and role_requirement.get("value") != role:
                raise QualificationError("execution profile binding role does not match report")
            resolved_conditions = binding.resolved.conditions
            observed_conditions = binding.observed.conditions
            condition_mismatches = [
                name for name in resolved_conditions
                if resolved_conditions.get(name) != observed_conditions.get(name)
            ]
            observation_state = (
                "complete"
                if binding.actual_key is not None and not condition_mismatches
                else "unknown"
            )
            profile_record = {
                "state": observation_state,
                "reason": None if observation_state == "complete" else (
                    "observed-conditions-unknown" if binding.actual_key is None
                    else "observed-conditions-mismatch:" + ",".join(condition_mismatches)
                ),
                "requested": binding.requested.to_dict(),
                "resolved": binding.resolved.to_dict(),
                "observed": binding.observed.to_dict(),
                "resolved_key": binding.resolved_key,
                "actual_key": binding.actual_key,
                "source_revision": _nonempty(profile_source_revision or source_revision, "profile_source_revision"),
                "source_artifact_digest": _validate_sha256(
                    profile_source_artifact_digest or _sha256(binding.to_dict()),
                    "profile_source_artifact_digest",
                ),
            }

        mapped = consumed.get("envelope_mapping")
        observation = mapped.get("provenance", {}).get("observation", {}) if isinstance(mapped, Mapping) else {}
        # ranked/pass 是報告的測量結果；coverage、實際條件與 receipt 仍各自硬閘，
        # 不因這個 verdict 直接產生 qualification。
        measurement_verdict = "pass" if row.get("ranked") is True else "pending"
        reasons: list[str] = []
        if coverage_state != "complete":
            reasons.append("coverage-incomplete" if coverage_state == "incomplete" else "coverage-unknown")
        if profile_record["state"] != "complete":
            reasons.append(str(profile_record["reason"]))
        if report_role in unknown_dimensions:
            reasons.append("role-observed-dimensions-unknown")
        return {
            "schema_version": 1,
            "test_only": bool(test_only),
            "subject": {
                "executor": executor,
                "model_id": model_id,
                "role": role,
                "report_role": report_role,
            },
            "profile_key": profile_key,
            "cohort": {
                "benchmark_type": row["benchmark_type"],
                "deck_digest": row["deck_digest"],
                "evaluator_revision": row["evaluator_revision"],
                "deck_id": row["deck_id"],
                "loadout": row["loadout"],
            },
            "coverage": {
                "state": coverage_state,
                "expected_encounters": sorted(expected_set),
                "observed_encounters": sorted(observed_set),
                "run_expected_encounters": sorted(run_expected),
                "run_observed_encounters": sorted(run_observed),
            },
            "source": {
                "producer": "paulsha-patchmud",
                "schema_version": consumed["schema_version"],
                "revision": consumed["source_revision"],
                "report_digest": consumed["report_fingerprint"],
                "artifact_digest": artifact_digest,
                "generated_at": report.get("generated_at"),
            },
            "profile_observation": profile_record,
            "measurement": {
                "verdict": measurement_verdict,
                "evaluated_at": report.get("generated_at") or "unknown",
                "row": {
                    "model": row["model"],
                    "runs": row.get("runs"),
                    "clears": row.get("clears"),
                    "ranked": row.get("ranked"),
                },
                "measured": mapped.get("envelope", {}) if isinstance(mapped, Mapping) else {},
                "provenance": mapped.get("provenance", {}) if isinstance(mapped, Mapping) else {},
                "measured_dimensions": sorted(measured_dimensions),
                "unknown_dimensions": unknown_dimensions,
                "reasons": reasons,
            },
            "mapping": {
                "version": QUALIFICATION_MAPPING_VERSION,
                "envelope_mapping": mapped,
            },
        }

    @staticmethod
    def _validate_candidate(candidate: object, *, test_only_required: bool) -> dict[str, object]:
        if not isinstance(candidate, dict):
            raise QualificationError("qualification candidate must be an object")
        required = {
            "schema_version", "test_only", "subject", "profile_key", "cohort",
            "coverage", "source", "profile_observation", "measurement", "mapping",
        }
        if set(candidate) != required:
            raise QualificationError("qualification candidate keys are invalid")
        if type(candidate.get("schema_version")) is not int or candidate["schema_version"] != 1:
            raise QualificationError("unsupported qualification candidate schema")
        if candidate.get("test_only") is not test_only_required:
            raise QualificationError("candidate test-only marker does not match import mode")
        subject = candidate.get("subject")
        if not isinstance(subject, Mapping):
            raise QualificationError("candidate subject is invalid")
        for name in ("executor", "model_id", "role", "report_role"):
            _nonempty(subject.get(name), f"candidate.subject.{name}")
        if subject["role"] not in _RUNTIME_ROLES or subject["report_role"] != _REPORT_ROLE_FOR_RUNTIME_ROLE[subject["role"]]:
            raise QualificationError("candidate runtime/report role binding is invalid")
        _validate_profile_key(candidate.get("profile_key"))
        cohort = candidate.get("cohort")
        if not isinstance(cohort, Mapping):
            raise QualificationError("candidate cohort is invalid")
        for name in ("benchmark_type", "deck_id", "loadout"):
            _nonempty(cohort.get(name), f"candidate.cohort.{name}")
        _validate_sha256(cohort.get("deck_digest"), "candidate.cohort.deck_digest")
        _validate_sha256(cohort.get("evaluator_revision"), "candidate.cohort.evaluator_revision")
        coverage = candidate.get("coverage")
        if not isinstance(coverage, Mapping) or coverage.get("state") not in {"complete", "incomplete", "unknown"}:
            raise QualificationError("candidate coverage state is invalid")
        for field in ("expected_encounters", "observed_encounters"):
            values = coverage.get(field)
            if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
                raise QualificationError(f"candidate coverage {field} is invalid")
            if len(values) != len(set(values)):
                raise QualificationError(f"candidate coverage {field} contains duplicates")
        source = candidate.get("source")
        if not isinstance(source, Mapping):
            raise QualificationError("candidate source is invalid")
        _nonempty(source.get("producer"), "candidate.source.producer")
        _nonempty(source.get("revision"), "candidate.source.revision")
        _validate_sha256(source.get("report_digest"), "candidate.source.report_digest")
        _validate_sha256(source.get("artifact_digest"), "candidate.source.artifact_digest")
        profile = candidate.get("profile_observation")
        if not isinstance(profile, Mapping) or profile.get("state") not in {"complete", "unknown"}:
            raise QualificationError("candidate profile observation is invalid")
        measurement = candidate.get("measurement")
        if not isinstance(measurement, Mapping) or measurement.get("verdict") not in {"pass", "pending", "fail"}:
            raise QualificationError("candidate measurement is invalid")
        _timestamp(measurement.get("evaluated_at"), "candidate evaluated_at")
        if not isinstance(candidate.get("mapping"), Mapping):
            raise QualificationError("candidate mapping is invalid")
        return deepcopy(candidate)

    def _import_candidate_unlocked(
        self,
        index: dict[str, object],
        candidate: dict[str, object],
        *,
        expected_revision: int,
        idempotency_key: str,
        operation: str,
    ) -> dict[str, object]:
        candidate_id = "qcan:v1:" + hashlib.sha256(_canonical_bytes(candidate)).hexdigest()
        candidate = {**candidate, "candidate_id": candidate_id}
        candidate_digest = _sha256(candidate)
        request = {
            "operation": operation,
            "candidate_digest": candidate_digest,
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
        }
        request_digest = _sha256(request)
        replay = self._replay(index, idempotency_key=idempotency_key, request_digest=request_digest)
        if replay is not None:
            return replay
        current_revision = self._check_expected_revision(index, expected_revision)
        self._write_immutable(
            self.paths.candidates_root,
            candidate_id,
            candidate,
            id_pattern=_CANDIDATE_ID_RE,
            label="qualification candidate",
        )
        self._hit("candidate_written")
        candidates = index["candidates"]
        assert isinstance(candidates, dict)
        candidates[candidate_id] = {"digest": candidate_digest, "state": "pending"}
        binding_id = _binding_key(
            candidate["subject"]["executor"],
            candidate["subject"]["model_id"],
            candidate["profile_key"],
            candidate["subject"]["role"],
        )
        bindings = index["bindings"]
        assert isinstance(bindings, dict)
        existing = bindings.get(binding_id)
        if existing is None:
            bindings[binding_id] = {
                "executor": candidate["subject"]["executor"],
                "model_id": candidate["subject"]["model_id"],
                "profile_key": candidate["profile_key"],
                "role": candidate["subject"]["role"],
                "candidate_id": candidate_id,
                "receipt_id": None,
                "approval_receipt_id": None,
                "state": "pending",
                "generation": 0,
                "history": [],
            }
        else:
            current = self._candidate(index, existing["candidate_id"])
            if (
                existing.get("state") == "pending"
                and _timestamp(candidate["measurement"]["evaluated_at"], "candidate evaluated_at")
                > _timestamp(current["measurement"]["evaluated_at"], "current candidate evaluated_at")
            ):
                existing["candidate_id"] = candidate_id
        index["revision"] = current_revision + 1
        result = {"candidate_id": candidate_id, "candidate_digest": candidate_digest, "revision": index["revision"]}
        idempotency = index["idempotency"]
        assert isinstance(idempotency, dict)
        idempotency[idempotency_key] = {"request_digest": request_digest, "result": result}
        self._write_index_unlocked(index)
        self._hit("index_written")
        self._sync_roster_unlocked(index)
        self._hit("roster_written")
        return deepcopy(result)

    def import_report(
        self,
        report: object,
        *,
        source_revision: str,
        source_artifact_digest: str,
        profile_key: str,
        executor: str,
        model_id: str,
        role: str,
        profile_binding: object | None = None,
        profile_source_revision: str | None = None,
        profile_source_artifact_digest: str | None = None,
        expected_revision: int,
        idempotency_key: str,
        test_only: bool = False,
    ) -> dict[str, object]:
        """以既有 v2 report consumer 建立 immutable candidate。"""

        candidate = self._candidate_from_report(
            report,
            source_revision=source_revision,
            source_artifact_digest=source_artifact_digest,
            profile_key=profile_key,
            executor=executor,
            model_id=model_id,
            role=role,
            profile_binding=profile_binding,
            profile_source_revision=profile_source_revision,
            profile_source_artifact_digest=profile_source_artifact_digest,
            test_only=test_only,
        )
        checked = self._validate_candidate(candidate, test_only_required=test_only)
        with self._locked():
            index = self._load_index_unlocked()
            return self._import_candidate_unlocked(
                index,
                checked,
                expected_revision=expected_revision,
                idempotency_key=_nonempty(idempotency_key, "idempotency_key"),
                operation="import-report",
            )

    def import_test_candidate(
        self,
        candidate: object,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        """匯入只能由 test-only receipt 消費的狀態機測試候選。"""

        checked = self._validate_candidate(candidate, test_only_required=True)
        with self._locked():
            index = self._load_index_unlocked()
            return self._import_candidate_unlocked(
                index,
                checked,
                expected_revision=expected_revision,
                idempotency_key=_nonempty(idempotency_key, "idempotency_key"),
                operation="import-test-candidate",
            )

    def review_candidate(
        self,
        candidate_id: str,
        *,
        verdict: str,
        reviewer: str | None = None,
        reviewer_authority: str | None = None,
        policy_revision: str | None = None,
        reviewed_at: str | None = None,
        expires_at: str | None = None,
        operator_receipt_id: str | None = None,
        test_only: bool,
        expected_revision: int,
        idempotency_key: str,
        now: object | None = None,
    ) -> dict[str, object]:
        """Publish a test review or one bound to a durable operator receipt."""

        current_time = _parse_now(now)
        if verdict not in {"approved", "rejected"}:
            raise QualificationError("verdict must be approved or rejected")
        operator_receipt: dict[str, object] | None = None
        operator_receipt_digest: str | None = None
        if test_only:
            if operator_receipt_id is not None:
                raise QualificationError("test-only review cannot use a live operator receipt")
            reviewer = _nonempty(reviewer, "reviewer")
            reviewer_authority = _nonempty(reviewer_authority, "reviewer_authority")
            policy_revision = _nonempty(policy_revision, "policy_revision")
            reviewed = _timestamp(reviewed_at, "reviewed_at")
            expires = _timestamp(expires_at, "expires_at")
            if reviewer_authority != "test-only":
                raise QualificationError("test-only review requires reviewer_authority=test-only")
        else:
            if not operator_receipt_id:
                raise QualificationError("live review requires a governed operator receipt")
            if any(value is not None for value in (reviewer, reviewer_authority, policy_revision, reviewed_at, expires_at)):
                raise QualificationError("live review details must come from the operator receipt")
            operator_receipt, operator_receipt_digest = self._read_operator_receipt(operator_receipt_id)
            reviewer = str(operator_receipt["reviewer"])
            reviewer_authority = "operator-receipt:" + operator_receipt_id
            policy_revision = str(operator_receipt["policy_revision"])
            reviewed = _timestamp(operator_receipt["reviewed_at"], "operator receipt reviewed_at")
            expires = _timestamp(operator_receipt["expires_at"], "operator receipt expires_at")
        assert reviewer is not None and reviewer_authority is not None and policy_revision is not None
        if expires <= reviewed:
            raise QualificationError("expires_at must be after reviewed_at")
        if reviewed > current_time:
            raise QualificationError("reviewed_at timestamp is in the future")
        if verdict == "approved" and expires <= current_time:
            raise QualificationError("expires_at timestamp is already expired")

        idem = _nonempty(idempotency_key, "idempotency_key")
        request = {
            "operation": "review",
            "candidate_id": candidate_id,
            "verdict": verdict,
            "operator_receipt_id": operator_receipt_id,
            "operator_receipt_digest": operator_receipt_digest,
            "reviewer": reviewer if test_only else None,
            "reviewer_authority": reviewer_authority if test_only else None,
            "policy_revision": policy_revision if test_only else None,
            "reviewed_at": _timestamp_text(reviewed) if test_only else None,
            "expires_at": _timestamp_text(expires) if test_only else None,
            "test_only": test_only,
            "expected_revision": expected_revision,
            "idempotency_key": idem,
        }
        request_digest = _sha256(request)
        with self._locked():
            index = self._load_index_unlocked()
            replay = self._replay(index, idempotency_key=idem, request_digest=request_digest)
            if replay is not None:
                return replay
            # 取得鎖後、寫入前重新量測時間：若他 process 持鎖到 receipt 過期後才放，
            # 這裡必須用鎖後的真實時間重新判斷，不能沿用取鎖前的舊快照放行過期核可。
            current_time = _parse_now(now)
            if reviewed > current_time:
                raise QualificationError("reviewed_at timestamp is in the future")
            if verdict == "approved" and expires <= current_time:
                raise QualificationError("expires_at timestamp is already expired")
            current_revision = self._check_expected_revision(index, expected_revision)
            candidate = self._candidate(index, candidate_id)
            if candidate["test_only"] is not test_only:
                raise QualificationError("candidate and receipt test-only markers must match")
            candidate_row = index["candidates"].get(candidate_id)
            if not isinstance(candidate_row, Mapping) or not isinstance(candidate_row.get("digest"), str):
                raise QualificationError("qualification candidate digest is unavailable")
            subject = candidate["subject"]
            binding_id = _binding_key(
                subject["executor"], subject["model_id"], candidate["profile_key"], subject["role"]
            )
            bindings = index["bindings"]
            assert isinstance(bindings, dict)
            binding = bindings.get(binding_id)
            if not isinstance(binding, dict):
                raise QualificationError("candidate has no lifecycle binding")
            if not test_only:
                assert operator_receipt_id is not None and operator_receipt_digest is not None
                operator_receipt, checked_operator_digest = self._validate_operator_receipt(
                    operator_receipt_id,
                    candidate,
                    candidate_row["digest"],
                    verdict=verdict,
                    expected_digest=operator_receipt_digest,
                    # 這張人類 receipt 必須綁在 binding 當前世代；一旦候選被撤銷／到期並推進
                    # 世代，舊 receipt 就不再匹配，不能重播復活已撤銷的資格（Q05）。
                    expected_binding_generation=binding.get("generation", 0),
                    now=current_time,
                )
                if checked_operator_digest != operator_receipt_digest:
                    raise QualificationError("operator receipt digest mismatch")
                reviewer = str(operator_receipt["reviewer"])
                policy_revision = str(operator_receipt["policy_revision"])
                reviewed = _timestamp(operator_receipt["reviewed_at"], "operator receipt reviewed_at")
                expires = _timestamp(operator_receipt["expires_at"], "operator receipt expires_at")
            current_candidate = self._candidate(index, binding["candidate_id"])
            if (
                binding.get("candidate_id") != candidate_id
                and _timestamp(candidate["measurement"]["evaluated_at"], "candidate evaluated_at")
                < _timestamp(current_candidate["measurement"]["evaluated_at"], "current candidate evaluated_at")
            ):
                raise QualificationConflict("late report cannot replace the current qualification generation")
            if verdict == "approved":
                blockers = _candidate_approval_blockers(candidate)
                if blockers:
                    raise QualificationError(
                        "candidate is not eligible for approved roster: " + ", ".join(blockers)
                    )

            scope = {
                "profile_key": candidate["profile_key"],
                "role": subject["role"],
                "report_role": subject["report_role"],
                "coverage": candidate["coverage"]["state"],
                "benchmark_type": candidate["cohort"]["benchmark_type"],
                "deck_digest": candidate["cohort"]["deck_digest"],
                "evaluator_revision": candidate["cohort"]["evaluator_revision"],
            }
            receipt_request = {
                **request,
                "candidate_digest": candidate_row["digest"],
                "scope": scope,
            }
            receipt_id = "qrcpt:v1:" + hashlib.sha256(_canonical_bytes(receipt_request)).hexdigest()
            receipt = {
                "schema_version": 1,
                "kind": "qualification-review",
                "receipt_id": receipt_id,
                "candidate_id": candidate_id,
                "candidate_digest": candidate_row["digest"],
                "report_digest": candidate["source"]["report_digest"],
                "profile_key": candidate["profile_key"],
                "profile_digest": _sha256(candidate["profile_observation"]),
                "role": subject["role"],
                "coverage": candidate["coverage"]["state"],
                "scope": scope,
                "verdict": verdict,
                "reviewer": reviewer,
                "reviewer_authority": {
                    "kind": "test-only" if test_only else "operator",
                    "reference": reviewer_authority,
                    "policy_revision": policy_revision,
                    **({
                        "receipt_id": operator_receipt_id,
                        "receipt_digest": operator_receipt_digest,
                    } if not test_only else {}),
                },
                "operator_receipt_id": operator_receipt_id,
                "operator_receipt_digest": operator_receipt_digest,
                "reason": operator_receipt.get("reason") if operator_receipt is not None else None,
                "policy_revision": policy_revision,
                "reviewed_at": _timestamp_text(reviewed),
                "expires_at": _timestamp_text(expires),
                "test_only": test_only,
                "expected_revision": expected_revision,
            }
            receipt_digest = self._write_immutable(
                self.paths.receipts_root,
                receipt_id,
                receipt,
                id_pattern=_RECEIPT_ID_RE,
                label="qualification receipt",
            )
            self._hit("receipt_written")
            receipts = index["receipts"]
            assert isinstance(receipts, dict)
            receipts[receipt_id] = {"digest": receipt_digest, "candidate_id": candidate_id, "kind": "review"}
            binding["candidate_id"] = candidate_id
            binding["state"] = verdict
            binding["receipt_id"] = receipt_id
            binding["approval_receipt_id"] = receipt_id if verdict == "approved" else None
            binding["generation"] = int(binding.get("generation", 0)) + 1
            history = binding.get("history")
            if not isinstance(history, list):
                history = []
            history.append(receipt_id)
            binding["history"] = history
            index["candidates"][candidate_id]["state"] = verdict
            index["revision"] = current_revision + 1
            result = {
                "candidate_id": candidate_id,
                "receipt_id": receipt_id,
                "receipt_digest": receipt_digest,
                "state": verdict,
                "generation": binding["generation"],
                "revision": index["revision"],
                "test_only": test_only,
                "operator_receipt_id": operator_receipt_id,
                "operator_receipt_digest": operator_receipt_digest,
            }
            index["idempotency"][idem] = {"request_digest": request_digest, "result": result}
            self._write_index_unlocked(index)
            self._hit("index_written")
            self._sync_roster_unlocked(index)
            self._hit("roster_written")
            return deepcopy(result)

    def revoke_qualification(
        self,
        candidate_id: str,
        *,
        reviewer: str | None = None,
        reviewer_authority: str | None = None,
        reason: str | None = None,
        revoked_at: str | None = None,
        operator_receipt_id: str | None = None,
        test_only: bool,
        expected_revision: int,
        idempotency_key: str,
        now: object | None = None,
    ) -> dict[str, object]:
        current_time = _parse_now(now)
        operator_receipt: dict[str, object] | None = None
        operator_receipt_digest: str | None = None
        if test_only:
            if operator_receipt_id is not None:
                raise QualificationError("test-only revoke cannot use a live operator receipt")
            reviewer = _nonempty(reviewer, "reviewer")
            reviewer_authority = _nonempty(reviewer_authority, "reviewer_authority")
            reason = _nonempty(reason, "reason")
            revoked = _timestamp(revoked_at, "revoked_at")
            if reviewer_authority != "test-only":
                raise QualificationError("test-only revoke requires reviewer_authority=test-only")
        else:
            if not operator_receipt_id:
                raise QualificationError("live revoke requires a governed operator receipt")
            if any(value is not None for value in (reviewer, reviewer_authority, reason, revoked_at)):
                raise QualificationError("live revoke details must come from the operator receipt")
            operator_receipt, operator_receipt_digest = self._read_operator_receipt(operator_receipt_id)
            reviewer = str(operator_receipt["reviewer"])
            reviewer_authority = "operator-receipt:" + operator_receipt_id
            reason = str(operator_receipt["reason"])
            revoked = _timestamp(operator_receipt["reviewed_at"], "operator receipt reviewed_at")
        assert reviewer is not None and reviewer_authority is not None and reason is not None
        if revoked > current_time:
            raise QualificationError("revoked_at timestamp is in the future")
        idem = _nonempty(idempotency_key, "idempotency_key")
        request = {
            "operation": "revoke",
            "candidate_id": candidate_id,
            "operator_receipt_id": operator_receipt_id,
            "operator_receipt_digest": operator_receipt_digest,
            "reviewer": reviewer if test_only else None,
            "reviewer_authority": reviewer_authority if test_only else None,
            "reason": reason if test_only else None,
            "revoked_at": _timestamp_text(revoked) if test_only else None,
            "test_only": test_only,
            "expected_revision": expected_revision,
            "idempotency_key": idem,
        }
        request_digest = _sha256(request)
        with self._locked():
            index = self._load_index_unlocked()
            replay = self._replay(index, idempotency_key=idem, request_digest=request_digest)
            if replay is not None:
                return replay
            # 取得鎖後、寫入前重新量測時間，理由同 review_candidate：不能沿用取鎖前的
            # 舊快照放行「revoked_at 在未來」這類判斷。
            current_time = _parse_now(now)
            if revoked > current_time:
                raise QualificationError("revoked_at timestamp is in the future")
            current_revision = self._check_expected_revision(index, expected_revision)
            candidate = self._candidate(index, candidate_id)
            if candidate["test_only"] is not test_only:
                raise QualificationError("candidate and receipt test-only markers must match")
            candidate_row = index["candidates"].get(candidate_id)
            if not isinstance(candidate_row, Mapping) or not isinstance(candidate_row.get("digest"), str):
                raise QualificationError("qualification candidate digest is unavailable")
            subject = candidate["subject"]
            binding_id = _binding_key(subject["executor"], subject["model_id"], candidate["profile_key"], subject["role"])
            binding = index["bindings"].get(binding_id)
            if not isinstance(binding, dict) or binding.get("candidate_id") != candidate_id:
                raise QualificationConflict("candidate is not the current qualification generation")
            if binding.get("state") != "approved" or not binding.get("approval_receipt_id"):
                raise QualificationConflict("only an approved qualification can be revoked")
            # 撤銷後這個世代必須永久結束；先固定住「被撤銷的世代」再遞增計數器，
            # 使日後任何綁在此世代（或更舊）的 receipt 都無法再核可回 approved（Q05）。
            revoked_generation = int(binding.get("generation", 0))
            approval = self._receipt(index, binding["approval_receipt_id"])
            approval_meta = index["receipts"].get(binding["approval_receipt_id"])
            if not isinstance(approval_meta, Mapping) or not isinstance(approval_meta.get("digest"), str):
                raise QualificationError("current approval receipt digest is unavailable")
            if not test_only:
                assert operator_receipt_id is not None and operator_receipt_digest is not None
                if approval.get("test_only") is not False:
                    raise QualificationError("live revoke cannot target a test-only approval")
                approval_operator_id = approval.get("operator_receipt_id")
                approval_operator_digest = approval.get("operator_receipt_digest")
                if not isinstance(approval_operator_id, str) or not isinstance(approval_operator_digest, str):
                    raise QualificationError("current approval lacks its operator receipt")
                self._validate_operator_receipt(
                    approval_operator_id,
                    candidate,
                    candidate_row["digest"],
                    verdict="approved",
                    expected_digest=approval_operator_digest,
                )
                operator_receipt, checked_operator_digest = self._validate_operator_receipt(
                    operator_receipt_id,
                    candidate,
                    candidate_row["digest"],
                    verdict="revoked",
                    expected_digest=operator_receipt_digest,
                    approval_receipt_id=binding["approval_receipt_id"],
                    approval_receipt_digest=approval_meta["digest"],
                    # 撤銷 receipt 本身也必須綁在 binding 當前世代，避免用陳舊撤銷 receipt
                    # 對已經前進到別的世代的候選重播。
                    expected_binding_generation=revoked_generation,
                    now=current_time,
                )
                if checked_operator_digest != operator_receipt_digest:
                    raise QualificationError("operator receipt digest mismatch")
                reviewer = str(operator_receipt["reviewer"])
                reason = str(operator_receipt["reason"])
                revoked = _timestamp(operator_receipt["reviewed_at"], "operator receipt reviewed_at")
            receipt_request = {
                **request,
                "candidate_digest": candidate_row["digest"],
                "approval_receipt_id": binding["approval_receipt_id"],
                "generation": revoked_generation,
            }
            receipt_id = "qrcpt:v1:" + hashlib.sha256(_canonical_bytes(receipt_request)).hexdigest()
            receipt = {
                "schema_version": 1,
                "kind": "qualification-revocation",
                "receipt_id": receipt_id,
                "candidate_id": candidate_id,
                "candidate_digest": candidate_row["digest"],
                "approval_receipt_id": binding["approval_receipt_id"],
                "approval_receipt_digest": index["receipts"][binding["approval_receipt_id"]]["digest"],
                "profile_key": candidate["profile_key"],
                "role": subject["role"],
                "reviewer": reviewer,
                "reviewer_authority": {
                    "kind": "test-only" if test_only else "operator",
                    "reference": reviewer_authority,
                    **({
                        "receipt_id": operator_receipt_id,
                        "receipt_digest": operator_receipt_digest,
                    } if not test_only else {}),
                },
                "operator_receipt_id": operator_receipt_id,
                "operator_receipt_digest": operator_receipt_digest,
                "reason": reason,
                "revoked_at": _timestamp_text(revoked),
                "test_only": test_only,
                "generation": revoked_generation,
            }
            # 撤銷 receipt 也必須可連回原核可 receipt，否則不能發布撤銷狀態。
            if approval.get("receipt_id") != binding["approval_receipt_id"]:
                raise QualificationError("approval receipt binding is invalid")
            receipt_digest = self._write_immutable(
                self.paths.receipts_root,
                receipt_id,
                receipt,
                id_pattern=_RECEIPT_ID_RE,
                label="qualification revocation receipt",
            )
            self._hit("receipt_written")
            index["receipts"][receipt_id] = {"digest": receipt_digest, "candidate_id": candidate_id, "kind": "revoke"}
            binding["state"] = "revoked"
            binding["receipt_id"] = receipt_id
            binding["revocation_receipt_id"] = receipt_id
            binding["revoked_at"] = _timestamp_text(revoked)
            binding["revocation_reason"] = reason
            binding["generation"] = revoked_generation + 1
            binding["history"].append(receipt_id)
            index["candidates"][candidate_id]["state"] = "revoked"
            index["revision"] = current_revision + 1
            result = {
                "candidate_id": candidate_id,
                "receipt_id": receipt_id,
                "receipt_digest": receipt_digest,
                "state": "revoked",
                "generation": binding["generation"],
                "revision": index["revision"],
                "test_only": test_only,
                "operator_receipt_id": operator_receipt_id,
                "operator_receipt_digest": operator_receipt_digest,
            }
            index["idempotency"][idem] = {"request_digest": request_digest, "result": result}
            self._write_index_unlocked(index)
            self._hit("revoke_index_written")
            self._sync_roster_unlocked(index)
            self._hit("roster_written")
            return deepcopy(result)

    def migrate_legacy_roster(
        self,
        payload: object,
        *,
        expected_revision: int,
        idempotency_key: str,
        source_ref: str,
    ) -> dict[str, object]:
        """保留舊 roster provenance 為 unknown，絕不升格為新資格。"""

        source_ref = _nonempty(source_ref, "source_ref")
        try:
            roster = model_resolution.parse_eval_roster(payload, path=source_ref)
        except (TypeError, ValueError) as exc:
            raise QualificationError("legacy model-eval-roster is invalid") from exc
        preserved = [entry.to_dict() for entry in roster.entries]
        legacy_digest = _sha256({"schema_version": roster.schema_version, "entries": preserved})
        migration_id = "qlegacy:v1:" + hashlib.sha256(
            _canonical_bytes([source_ref, legacy_digest])
        ).hexdigest()
        idem = _nonempty(idempotency_key, "idempotency_key")
        request = {
            "operation": "migrate-legacy-roster",
            "source_ref": source_ref,
            "legacy_digest": legacy_digest,
            "expected_revision": expected_revision,
            "idempotency_key": idem,
        }
        request_digest = _sha256(request)
        with self._locked():
            index = self._load_index_unlocked()
            replay = self._replay(index, idempotency_key=idem, request_digest=request_digest)
            if replay is not None:
                return replay
            current_revision = self._check_expected_revision(index, expected_revision)
            migrations = index["legacy_migrations"]
            migrations[migration_id] = {
                "state": "unknown",
                "source_ref": source_ref,
                "source_digest": legacy_digest,
                "schema_version": roster.schema_version,
                "entries": preserved,
            }
            index["revision"] = current_revision + 1
            result = {"migration_id": migration_id, "state": "unknown", "migrated_rows": len(preserved), "revision": index["revision"]}
            index["idempotency"][idem] = {"request_digest": request_digest, "result": result}
            self._write_index_unlocked(index)
            self._hit("index_written")
            self._sync_roster_unlocked(index)
            self._hit("roster_written")
            return deepcopy(result)

    def _eval_entry_for_binding(
        self,
        index: Mapping[str, object],
        binding: Mapping[str, object],
        candidate: Mapping[str, object],
    ) -> dict[str, object]:
        state = binding.get("state")
        receipt_id = binding.get("approval_receipt_id")
        receipt: Mapping[str, object] | None = None
        if isinstance(receipt_id, str):
            receipt = self._receipt(index, receipt_id)
        review_status = (
            "approved" if state == "approved"
            else "rejected" if state in {"rejected", "revoked"}
            else "pending"
        )
        entry: dict[str, object] = {
            "executor": candidate["subject"]["executor"],
            "model_id": candidate["subject"]["model_id"],
            "role": candidate["subject"]["report_role"],
            "execution_profile_key": candidate["profile_key"],
            "benchmark_type": candidate["cohort"]["benchmark_type"],
            "deck_digest": candidate["cohort"]["deck_digest"],
            "evaluator_revision": candidate["cohort"]["evaluator_revision"],
            "verdict": candidate["measurement"]["verdict"],
            "evaluated_at": candidate["measurement"]["evaluated_at"],
            "eval_source": candidate["source"]["revision"],
            "review_status": review_status,
            "eval_ref": candidate["source"]["report_digest"],
        }
        if receipt is not None:
            entry["reviewer"] = receipt.get("reviewer")
            entry["reviewed_at"] = receipt.get("reviewed_at")
        return entry

    def _roster_payload_unlocked(self, index: Mapping[str, object]) -> dict[str, object]:
        bindings = index["bindings"]
        assert isinstance(bindings, dict)
        entries: list[dict[str, object]] = []
        for binding_id in sorted(bindings):
            binding = bindings[binding_id]
            if not isinstance(binding, Mapping):
                continue
            try:
                candidate = self._candidate(index, binding["candidate_id"])
                if binding.get("state") == "approved" and candidate.get("test_only") is False:
                    approval_id = binding.get("approval_receipt_id")
                    if not isinstance(approval_id, str):
                        raise QualificationError("approved binding has no receipt id")
                    approval = self._receipt(index, approval_id)
                    operator_id = approval.get("operator_receipt_id")
                    operator_digest = approval.get("operator_receipt_digest")
                    candidate_meta = index["candidates"].get(binding["candidate_id"])
                    if (
                        not isinstance(operator_id, str)
                        or not isinstance(operator_digest, str)
                        or not isinstance(candidate_meta, Mapping)
                        or not isinstance(candidate_meta.get("digest"), str)
                    ):
                        raise QualificationError("approved binding has no operator receipt")
                    self._validate_operator_receipt(
                        operator_id,
                        candidate,
                        candidate_meta["digest"],
                        verdict="approved",
                        expected_digest=operator_digest,
                    )
                eval_entry = self._eval_entry_for_binding(index, binding, candidate)
            except (QualificationError, KeyError, TypeError):
                # 無法證明 index 指向的完整鏈時，投影不輸出該列；consumer 也會 fail closed。
                continue
            receipt_id = binding.get("approval_receipt_id")
            receipt_meta = index["receipts"].get(receipt_id) if isinstance(receipt_id, str) else None
            entries.append(
                {
                    "candidate_id": binding["candidate_id"],
                    "state": binding.get("state", "unknown"),
                    "generation": binding.get("generation", 0),
                    "profile_key": candidate["profile_key"],
                    "role": candidate["subject"]["role"],
                    "coverage": candidate["coverage"]["state"],
                    "candidate_digest": index["candidates"][binding["candidate_id"]]["digest"],
                    "receipt_id": receipt_id,
                    "receipt_digest": receipt_meta.get("digest") if isinstance(receipt_meta, Mapping) else None,
                    "test_only": candidate["test_only"],
                    "eval_entry": eval_entry,
                }
            )
        return {
            "schema_version": QUALIFICATION_SCHEMA_VERSION,
            "revision": index["revision"],
            "entries": entries,
        }

    @staticmethod
    def eval_roster_payload(
        roster: Mapping[str, object], *, include_test_only: bool = False
    ) -> dict[str, object]:
        """投影舊 EvalRosterEntry/parser 可理解的嚴格 v2 sibling view。"""

        entries = roster.get("entries")
        if not isinstance(entries, list):
            raise QualificationError("qualification roster entries are invalid")
        eval_entries = []
        for row in entries:
            if not isinstance(row, Mapping) or not isinstance(row.get("eval_entry"), Mapping):
                continue
            if row.get("test_only") is True and not include_test_only:
                continue
            eval_entries.append(dict(row["eval_entry"]))
        return {"schema_version": 2, "entries": eval_entries}

    def _sync_roster_unlocked(self, index: Mapping[str, object]) -> None:
        roster = self._roster_payload_unlocked(index)
        desired = _canonical_bytes(roster) + b"\n"
        try:
            current = _safe_read_bytes(self.paths.roster_path, label="approved qualification roster")
        except FileNotFoundError:
            current = None
        if current != desired:
            _atomic_write_json(self.paths.roster_path, roster, governed_parent=not self._test_root)

    def _prune_orphans_unlocked(self, index: Mapping[str, object]) -> None:
        indexed_candidates = index["candidates"]
        indexed_receipts = index["receipts"]
        for root, indexed in (
            (self.paths.candidates_root, indexed_candidates),
            (self.paths.receipts_root, indexed_receipts),
        ):
            assert isinstance(indexed, dict)
            referenced = set(indexed)
            for path in root.glob("*.json"):
                item_id = path.stem
                if item_id not in referenced:
                    # 尚未被 CAS index 引用的檔案不具授權效力，可安全清除。
                    if path.is_symlink() or not path.is_file():
                        raise QualificationError("orphan qualification artifact is not a regular file")
                    path.unlink()

    def _reconcile_unlocked(self, index: dict[str, object]) -> None:
        self._sync_roster_unlocked(index)
        self._prune_orphans_unlocked(index)

    def reconcile(self) -> dict[str, object]:
        with self._locked():
            index = self._load_index_unlocked()
            self._reconcile_unlocked(index)
            return {"revision": index["revision"], "roster_entries": len(self._roster_payload_unlocked(index)["entries"])}

    def read_roster(self) -> dict[str, object]:
        with self._locked():
            index = self._load_index_unlocked()
            self._reconcile_unlocked(index)
            payload = _safe_read_json(self.paths.roster_path, label="approved qualification roster")
            if (
                not isinstance(payload, dict)
                or set(payload) != {"schema_version", "revision", "entries"}
                or payload.get("schema_version") != QUALIFICATION_SCHEMA_VERSION
                or payload.get("revision") != index["revision"]
                or not isinstance(payload.get("entries"), list)
            ):
                raise QualificationError("approved qualification roster is invalid or stale")
            return payload

    def _find_legacy_match(
        self,
        index: Mapping[str, object],
        executor: str,
        model_id: str,
        profile_key: str,
        role: str,
    ) -> bool:
        migrations = index["legacy_migrations"]
        assert isinstance(migrations, Mapping)
        for migration in migrations.values():
            if not isinstance(migration, Mapping) or not isinstance(migration.get("entries"), list):
                continue
            try:
                parsed = model_resolution.parse_eval_roster(
                    {"schema_version": migration["schema_version"], "entries": migration["entries"]}
                )
            except (TypeError, ValueError, KeyError):
                continue
            if parsed.schema_version == 2 and any(
                entry.key == (executor, model_id)
                and entry.execution_profile_key == profile_key
                and entry.report_role == _REPORT_ROLE_FOR_RUNTIME_ROLE.get(role)
                and role in entry.roles
                for entry in parsed.entries
            ):
                return True
            # Legacy v1 pair-key evidence has no profile or receipt binding at all.
            if parsed.schema_version == 1 and any(entry.key == (executor, model_id) for entry in parsed.entries):
                return True
        return False

    def _qualification_status_unlocked(
        self,
        index: dict[str, object],
        roster: Mapping[str, object],
        executor: str,
        model_id: str,
        profile_key: str,
        role: str,
        now: datetime,
        *,
        allow_test_receipts: bool,
    ) -> dict[str, object]:
        binding_id = _binding_key(executor, model_id, profile_key, role)
        bindings = index["bindings"]
        assert isinstance(bindings, dict)
        binding = bindings.get(binding_id)
        if not isinstance(binding, dict):
            reason = "legacy-qualification-unbound" if self._find_legacy_match(
                index, executor, model_id, profile_key, role
            ) else "qualification-unknown"
            return {"state": "unknown", "reason": reason}
        if binding.get("state") == "revoked":
            return {"state": "revoked", "reason": binding.get("revocation_reason", "revoked"), "generation": binding.get("generation")}
        if binding.get("state") == "rejected":
            return {"state": "rejected", "reason": "human-review-rejected", "generation": binding.get("generation")}
        if binding.get("state") != "approved":
            return {"state": "unknown", "reason": "human-review-pending"}
        try:
            candidate = self._candidate(index, binding["candidate_id"])
            receipt = self._receipt(index, binding["approval_receipt_id"])
        except (QualificationError, KeyError, TypeError):
            return {"state": "unknown", "reason": "candidate-or-receipt-invalid"}
        if candidate.get("test_only") is True or receipt.get("test_only") is True:
            if not allow_test_receipts:
                return {"state": "unknown", "reason": "test-only-receipt"}
        if (
            candidate.get("profile_key") != profile_key
            or candidate.get("subject", {}).get("executor") != executor
            or candidate.get("subject", {}).get("model_id") != model_id
            or candidate.get("subject", {}).get("role") != role
            or receipt.get("candidate_id") != binding.get("candidate_id")
            or receipt.get("profile_key") != profile_key
            or receipt.get("role") != role
            or receipt.get("verdict") != "approved"
            or receipt.get("candidate_digest") != index["candidates"][binding["candidate_id"]]["digest"]
            or receipt.get("report_digest") != candidate.get("source", {}).get("report_digest")
            or receipt.get("profile_digest") != _sha256(candidate.get("profile_observation"))
            or receipt.get("scope", {}).get("coverage") != candidate.get("coverage", {}).get("state")
        ):
            return {"state": "unknown", "reason": "receipt-binding-mismatch"}
        if candidate.get("test_only") is False:
            operator_id = receipt.get("operator_receipt_id")
            operator_digest = receipt.get("operator_receipt_digest")
            candidate_meta = index["candidates"].get(binding["candidate_id"])
            if (
                receipt.get("reviewer_authority", {}).get("kind") != "operator"
                or not isinstance(operator_id, str)
                or not isinstance(operator_digest, str)
                or not isinstance(candidate_meta, Mapping)
                or not isinstance(candidate_meta.get("digest"), str)
            ):
                return {"state": "unknown", "reason": "operator-receipt-unavailable"}
            try:
                self._validate_operator_receipt(
                    operator_id,
                    candidate,
                    candidate_meta["digest"],
                    verdict="approved",
                    expected_digest=operator_digest,
                    now=now,
                )
            except (QualificationError, OSError, KeyError, TypeError):
                return {"state": "unknown", "reason": "operator-receipt-invalid"}
        coverage = candidate.get("coverage", {})
        if coverage.get("state") != "complete":
            return {"state": "unknown", "reason": "coverage-incomplete" if coverage.get("state") == "incomplete" else "coverage-unknown"}
        if set(coverage.get("expected_encounters", [])) != set(coverage.get("observed_encounters", [])):
            return {"state": "unknown", "reason": "coverage-set-mismatch"}
        observation = candidate.get("profile_observation", {})
        if observation.get("state") != "complete" or observation.get("resolved_key") != profile_key:
            return {"state": "unknown", "reason": "observed-profile-incomplete"}
        if candidate.get("measurement", {}).get("verdict") != "pass":
            return {"state": "unknown", "reason": "report-verdict-not-pass"}
        if receipt.get("reviewer_authority", {}).get("kind") != ("test-only" if receipt.get("test_only") else "operator"):
            return {"state": "unknown", "reason": "reviewer-authority-invalid"}

        watermarks = index["clock_watermarks"]
        assert isinstance(watermarks, dict)
        prior_text = watermarks.get(binding_id)
        if isinstance(prior_text, str):
            prior = _timestamp(prior_text, "clock watermark")
            if now < prior:
                return {"state": "unknown", "reason": "clock-regression"}
        reviewed = _timestamp(receipt.get("reviewed_at"), "receipt reviewed_at")
        expires = _timestamp(receipt.get("expires_at"), "receipt expires_at")
        if now < reviewed:
            return {"state": "unknown", "reason": "review-time-in-future"}
        watermarks[binding_id] = _timestamp_text(now)
        if now >= expires:
            return {"state": "expired", "reason": "receipt-expired", "generation": binding.get("generation")}

        entries = roster.get("entries")
        projected = next(
            (
                row for row in entries
                if isinstance(row, Mapping)
                and row.get("candidate_id") == binding.get("candidate_id")
                and row.get("state") == "approved"
                and row.get("receipt_id") == binding.get("approval_receipt_id")
            ),
            None,
        ) if isinstance(entries, list) else None
        if projected is None or projected.get("coverage") != "complete":
            return {"state": "unknown", "reason": "approved-roster-entry-missing"}
        try:
            parsed_roster = model_resolution.parse_eval_roster(
                self.eval_roster_payload(roster, include_test_only=allow_test_receipts)
            )
        except (TypeError, ValueError, QualificationError):
            return {"state": "unknown", "reason": "approved-roster-invalid"}
        cohort = {
            "role": candidate["subject"]["report_role"],
            "benchmark_type": candidate["cohort"]["benchmark_type"],
            "profile_id": profile_key,
            "deck_digest": candidate["cohort"]["deck_digest"],
            "evaluator_revision": candidate["cohort"]["evaluator_revision"],
        }
        entry = parsed_roster.entry_for(
            executor,
            model_id,
            role=role,
            execution_profile_key=profile_key,
            cohort_identity=cohort,
        )
        if entry is None or not entry.approves(role, execution_profile_key=profile_key, cohort_identity=cohort):
            return {"state": "unknown", "reason": "approved-roster-query-miss"}
        receipt_meta = index["receipts"][binding["approval_receipt_id"]]
        return {
            "state": "approved",
            "reason": None,
            "profile_key": profile_key,
            "role": role,
            "coverage": "complete",
            "receipt": receipt_meta["digest"],
            "revoked": False,
            "generation": binding["generation"],
            "expires_at": receipt["expires_at"],
            "test_only": receipt["test_only"],
        }

    def qualification_status(
        self,
        executor: str,
        model_id: str,
        profile_key: str,
        role: str,
        *,
        now: object | None = None,
        allow_test_receipts: bool = False,
    ) -> dict[str, object]:
        executor = _nonempty(executor, "executor")
        model_id = _nonempty(model_id, "model_id")
        profile_key = _validate_profile_key(profile_key)
        role = _nonempty(role, "role")
        if role not in _RUNTIME_ROLES:
            return {"state": "unknown", "reason": "unsupported-role"}
        current_time = _parse_now(now)
        with self._locked():
            index = self._load_index_unlocked()
            self._reconcile_unlocked(index)
            roster = _safe_read_json(self.paths.roster_path, label="approved qualification roster")
            if not isinstance(roster, Mapping):
                return {"state": "unknown", "reason": "approved-roster-invalid"}
            before = _canonical_bytes(index)
            result = self._qualification_status_unlocked(
                index, roster, executor, model_id, profile_key, role, current_time,
                allow_test_receipts=allow_test_receipts,
            )
            if _canonical_bytes(index) != before:
                self._write_index_unlocked(index)
            return result

    def query_qualification(
        self,
        executor: str,
        model_id: str,
        profile_key: str,
        role: str,
        *,
        now: object | None = None,
        allow_test_receipts: bool = False,
    ) -> dict[str, object] | None:
        executor = _nonempty(executor, "executor")
        model_id = _nonempty(model_id, "model_id")
        profile_key = _validate_profile_key(profile_key)
        role = _nonempty(role, "role")
        if role not in _RUNTIME_ROLES:
            return None
        current_time = _parse_now(now)
        with self._locked():
            index = self._load_index_unlocked()
            self._reconcile_unlocked(index)
            roster = _safe_read_json(self.paths.roster_path, label="approved qualification roster")
            if not isinstance(roster, Mapping):
                return None
            before = _canonical_bytes(index)
            result = self._qualification_status_unlocked(
                index,
                roster,
                executor,
                model_id,
                profile_key,
                role,
                current_time,
                allow_test_receipts=allow_test_receipts,
            )
            if _canonical_bytes(index) != before:
                self._write_index_unlocked(index)
            return result if result.get("state") == "approved" else None

    def verify_current_binding(self, executor: str, model_id: str, profile_key: str, role: str) -> bool:
        with self._locked():
            index = self._load_index_unlocked()
            binding = index["bindings"].get(_binding_key(executor, model_id, profile_key, role))
            if not isinstance(binding, Mapping) or not binding.get("candidate_id"):
                return False
            try:
                candidate = self._candidate(index, binding["candidate_id"])
                if binding.get("state") == "approved":
                    receipt = self._receipt(index, binding["approval_receipt_id"])
                    if receipt.get("candidate_id") != candidate.get("candidate_id"):
                        return False
                    if receipt.get("test_only") is False:
                        operator_id = receipt.get("operator_receipt_id")
                        operator_digest = receipt.get("operator_receipt_digest")
                        candidate_meta = index["candidates"].get(binding["candidate_id"])
                        if (
                            not isinstance(operator_id, str)
                            or not isinstance(operator_digest, str)
                            or not isinstance(candidate_meta, Mapping)
                            or not isinstance(candidate_meta.get("digest"), str)
                        ):
                            return False
                        self._validate_operator_receipt(
                            operator_id,
                            candidate,
                            candidate_meta["digest"],
                            verdict="approved",
                            expected_digest=operator_digest,
                        )
                    return True
                if binding.get("state") == "revoked":
                    receipt = self._receipt(index, binding["revocation_receipt_id"])
                    return receipt.get("approval_receipt_id") == binding.get("approval_receipt_id")
            except (QualificationError, KeyError, TypeError):
                return False
            return binding.get("state") in {"pending", "rejected"}


def lookup_dispatch_qualification(
    identity: object,
    binding: execution_adapters.ExecutionProfileBinding,
    *,
    now: object | None = None,
) -> dict[str, object] | None:
    """Manager 最終派工的 approved-roster query；舊 identity 屬性不具授權效力。"""

    role_requirement = binding.resolved.requirements.get("role", {})
    role = role_requirement.get("value") if role_requirement.get("state") == "known" else None
    if role not in _RUNTIME_ROLES:
        return None
    try:
        return QualificationStore().query_qualification(
            _nonempty(getattr(identity, "executor", None), "identity.executor"),
            _nonempty(getattr(identity, "model_id", None), "identity.model_id"),
            binding.resolved_key,
            role,
            now=now,
        )
    except (QualificationError, OSError):
        return None
