"""版本化需求交付索引的可信 consumer 與 CAS 持久化。"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from paulsha_cortex import runtime_attestation

from . import claim, completion, delivery, github_delivery, review, verification


MANIFEST_SCHEMA = "cortex/requirement-manifest/v1"
SNAPSHOT_SCHEMA = "cortex/requirement-evidence-snapshot/v1"
REPORT_SCHEMA = "cortex/requirement-delivery-report/v1"
INDEX_SCHEMA = 1
INDEX_DOCUMENT_SCHEMA = "cortex/requirement-delivery-index"
VALIDATOR_VERSION = "requirement-delivery/v1"
INDEX_MAX_BYTES = 8 * 1024 * 1024
SOURCE_MAX_BYTES = 4 * 1024 * 1024
STAGES = ("source", "test", "review", "merge", "installed", "live")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PROFILE_KEY_RE = re.compile(r"^epk:v1:(?:request|resolved|observed):[0-9a-f]{64}$")
_UTC_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)$")


class IndexConflict(RuntimeError):
    """Durable index revision changed while a reconciliation was being evaluated."""


def _checkpoint(_name: str) -> None:
    """測試用 crash boundary；正式執行不做任何動作。"""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _nonempty(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _digest(value: object, *, field: str) -> str:
    value = _nonempty(value, field=field).lower()
    if _SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a 64-character SHA-256 digest")
    return value


def _timestamp(value: object, *, field: str) -> datetime:
    text = _nonempty(value, field=field)
    if _UTC_RE.fullmatch(text) is None:
        raise ValueError(f"{field} must be an RFC3339 UTC timestamp")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _locator(value: object, *, field: str) -> str:
    text = _nonempty(value, field=field)
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "\\" in text or not path.parts:
        raise ValueError(f"{field} must be a safe relative locator")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError(f"{field} must be a normalized relative locator")
    return path.as_posix()


def _accepted_plan_inventory(content: bytes, *, kind: str) -> dict[str, str]:
    """讀取已接受計畫中的 requirement id → 標題 inventory，作為 manifest 需求集合的鎖定基準。

    計畫表格逐需求一列，不逐條 acceptance criterion 拆列；驗收條件的具體措辭是 manifest
    自身的操作化定義，不強求與計畫散文逐字一致（否則會把任何合理改寫誤判為縮減範圍）。
    這裡只鎖定「需求識別碼與標題集合」不得在沿用同一 accepted revision 時被悄悄增減。"""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("accepted authority is not UTF-8") from exc
    frontmatter = re.match(r"\A---\r?\n(?P<body>.*?)\r?\n---(?:\r?\n|\Z)", text, re.DOTALL)
    if frontmatter is None:
        raise ValueError("accepted authority frontmatter is missing")
    status_values = re.findall(r"(?m)^status:\s*([^\s#]+)\s*$", frontmatter.group("body"))
    if status_values != ["accepted"]:
        raise ValueError("accepted authority status is not accepted")

    expected_headers = [
        "ID／問題",
        "本版明確納入的修正",
        "既有落點／需要補的範圍",
        "必須取得的驗收證據",
    ]
    lines = text.splitlines()

    def cells(line: str) -> list[str] | None:
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            return None
        return [cell.strip() for cell in stripped[1:-1].split("|")]

    headers = [index for index, line in enumerate(lines) if cells(line) == expected_headers]
    if len(headers) != 1:
        raise ValueError("accepted authority requirement inventory table is missing or ambiguous")
    inventory: dict[str, str] = {}
    for line in lines[headers[0] + 1:]:
        row = cells(line)
        if row is None:
            if inventory:
                break
            continue
        if len(row) != len(expected_headers):
            if row[0].startswith("R"):
                raise ValueError("accepted authority requirement row is malformed")
            continue
        match = re.fullmatch(r"(R[0-9]{2})\s+(.+)", row[0])
        if match is None:
            if row[0].startswith("R"):
                raise ValueError("accepted authority requirement identity is malformed")
            continue
        requirement_id, title = match.groups()
        if requirement_id in inventory:
            raise ValueError(f"accepted authority repeats requirement {requirement_id}")
        inventory[requirement_id] = title
    if not inventory:
        raise ValueError(f"accepted {kind} contains no supported requirement inventory")
    return inventory


def _validate_manifest_authority(manifest: Mapping[str, Any], *, authority_root: Path) -> None:
    """驗證 authority_ref 綁定到真正的 accepted 內容 digest，並鎖定 manifest 的需求識別碼
    與標題集合；同一 accepted revision 下不得悄悄增減需求，讓未完整交付的範圍逃過缺額判定。"""
    authority_ref = manifest["authority_ref"]
    locator = authority_ref["locator"]
    expected_digest = authority_ref["revision"]
    try:
        content = _read_beneath(authority_root, locator)
    except (OSError, ValueError) as exc:
        raise ValueError(f"accepted authority is unavailable: {type(exc).__name__}") from exc
    actual_digest = hashlib.sha256(content).hexdigest()
    if actual_digest != expected_digest:
        raise ValueError("accepted authority digest mismatch")
    inventory = _accepted_plan_inventory(content, kind=authority_ref["kind"])

    manifest_rows = {row["id"]: row for row in manifest["requirements"]}
    if set(manifest_rows) != set(inventory):
        missing = sorted(set(inventory) - set(manifest_rows))
        extra = sorted(set(manifest_rows) - set(inventory))
        raise ValueError(
            "accepted authority requirement inventory mismatch: "
            f"missing={missing}, extra={extra}"
        )
    for requirement_id, expected_title in inventory.items():
        row = manifest_rows[requirement_id]
        source_ref = row["source_ref"]
        if source_ref["locator"] != locator or source_ref["sha256"].lower() != expected_digest:
            raise ValueError(f"accepted authority source binding mismatch for {requirement_id}")
        if row["title"] != expected_title:
            raise ValueError(f"accepted authority requirement title mismatch for {requirement_id}")


def validate_manifest(
    payload: object,
    *,
    authority_root: str | Path | None = None,
) -> dict[str, Any]:
    """驗證版本化 requirement authority；空驗收或空 evidence policy 一律拒絕。"""
    if not isinstance(payload, dict) or payload.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("requirement manifest schema is unknown")
    required_root = {"schema", "manifest_id", "authority_ref", "requirements", "waiver_policy"}
    if set(payload) != required_root:
        raise ValueError("requirement manifest has missing or unexpected fields")
    _nonempty(payload.get("manifest_id"), field="manifest_id")
    authority_ref = payload.get("authority_ref")
    if not isinstance(authority_ref, dict) or set(authority_ref) != {"kind", "locator", "revision"}:
        raise ValueError("requirement manifest authority_ref must bind kind, locator, and revision digest")
    kind = _nonempty(authority_ref.get("kind"), field="authority_ref.kind")
    if kind not in {"accepted-plan", "accepted-spec"}:
        raise ValueError("requirement manifest authority_ref kind is unsupported")
    _locator(authority_ref.get("locator"), field="authority_ref.locator")
    _digest(authority_ref.get("revision"), field="authority_ref.revision")
    requirements = payload.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("requirement manifest must contain requirements")
    seen_ids: set[str] = set()
    normalized_requirements: list[dict[str, Any]] = []
    for index, row in enumerate(requirements):
        if isinstance(row, dict) and "acceptance_criteria" not in row:
            raise ValueError(f"requirements[{index}] acceptance criteria are required")
        if isinstance(row, dict) and "evidence_policy" not in row:
            raise ValueError(f"requirements[{index}] evidence policy is required")
        if not isinstance(row, dict) or set(row) != {
            "id", "revision", "title", "source_ref", "acceptance_criteria", "evidence_policy"
        }:
            raise ValueError(f"requirements[{index}] fields are invalid")
        requirement_id = _nonempty(row.get("id"), field=f"requirements[{index}].id")
        if requirement_id in seen_ids:
            raise ValueError(f"duplicate requirement id: {requirement_id}")
        seen_ids.add(requirement_id)
        revision = _nonempty(row.get("revision"), field=f"requirements[{index}].revision")
        source_ref = row.get("source_ref")
        if not isinstance(source_ref, dict) or set(source_ref) != {"locator", "sha256"}:
            raise ValueError(f"{requirement_id} source_ref is invalid")
        _locator(source_ref.get("locator"), field=f"{requirement_id}.source_ref.locator")
        _digest(source_ref.get("sha256"), field=f"{requirement_id}.source_ref.sha256")
        criteria = row.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria:
            # 空 acceptance_criteria 一定與 accepted authority 的 inventory 不符（後者至少有一條），
            # 訊息一併標明避免被誤讀成單純結構檢查而繞過縮減範圍偵測。
            raise ValueError(f"{requirement_id} acceptance criteria are required by accepted authority")
        criterion_ids: set[str] = set()
        for criterion in criteria:
            if not isinstance(criterion, dict) or set(criterion) != {"id", "description"}:
                raise ValueError(f"{requirement_id} acceptance criterion is invalid")
            criterion_id = _nonempty(criterion.get("id"), field="acceptance_criteria.id")
            _nonempty(criterion.get("description"), field="acceptance_criteria.description")
            if criterion_id in criterion_ids:
                raise ValueError(f"{requirement_id} has duplicate acceptance id")
            criterion_ids.add(criterion_id)
        policy = row.get("evidence_policy")
        if not isinstance(policy, dict) or set(policy) != {
            "required_stages", "waivable_stages", "max_age_seconds", "policy_version", "owner"
        }:
            raise ValueError(f"{requirement_id} evidence policy is required")
        _nonempty(policy.get("policy_version"), field=f"{requirement_id}.evidence_policy.policy_version")
        stages = policy.get("required_stages")
        if not isinstance(stages, list) or not stages or any(stage not in STAGES for stage in stages):
            raise ValueError(f"{requirement_id} evidence policy must require known stages")
        if len(stages) != len(set(stages)):
            raise ValueError(f"{requirement_id} evidence policy repeats a stage")
        waivable = policy.get("waivable_stages")
        if not isinstance(waivable, list) or any(stage not in stages for stage in waivable):
            raise ValueError(f"{requirement_id} waivable stages must be required stages")
        if set(waivable) & {"installed", "live"}:
            raise ValueError("cannot waive installed/live evidence")
        max_age = policy.get("max_age_seconds")
        if not isinstance(max_age, dict) or any(
            stage not in stages or type(seconds) is not int or seconds <= 0
            for stage, seconds in max_age.items()
        ):
            raise ValueError(f"{requirement_id} max_age_seconds is invalid")
        owner = policy.get("owner")
        if not isinstance(owner, dict) or set(owner) != {"id", "work_ids", "recovery"}:
            raise ValueError(f"{requirement_id} owner/recovery is required")
        _nonempty(owner.get("id"), field="evidence_policy.owner.id")
        work_ids = owner.get("work_ids")
        if not isinstance(work_ids, list) or not work_ids or any(not isinstance(item, str) or not item for item in work_ids):
            raise ValueError(f"{requirement_id} owner.work_ids is invalid")
        _nonempty(owner.get("recovery"), field="evidence_policy.owner.recovery")
        normalized_requirements.append(copy.deepcopy(row))
    waiver_policy = payload.get("waiver_policy")
    if not isinstance(waiver_policy, dict) or set(waiver_policy) != {"authorities"}:
        raise ValueError("waiver_policy must declare authorities")
    authorities = waiver_policy.get("authorities")
    if not isinstance(authorities, list):
        raise ValueError("waiver_policy.authorities must be a list")
    seen_authorities: set[tuple[str, str]] = set()
    for authority in authorities:
        if not isinstance(authority, dict) or set(authority) != {"id", "version"}:
            raise ValueError("waiver authority must have id and version")
        identity = (_nonempty(authority.get("id"), field="waiver authority id"), _nonempty(authority.get("version"), field="waiver authority version"))
        if identity in seen_authorities:
            raise ValueError("duplicate waiver authority")
        seen_authorities.add(identity)
    if authority_root is None:
        raise ValueError("accepted authority root is required")
    _validate_manifest_authority(payload, authority_root=Path(authority_root))
    return copy.deepcopy(payload)


def _validate_snapshot(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("requirement evidence snapshot schema is unknown")
    required = {"schema", "captured_at", "snapshot_revision", "mappings", "waivers"}
    if set(payload) != required:
        raise ValueError("requirement evidence snapshot fields are invalid")
    _timestamp(payload.get("captured_at"), field="captured_at")
    if type(payload.get("snapshot_revision")) is not int or payload["snapshot_revision"] < 0:
        raise ValueError("snapshot_revision must be a non-negative integer")
    if not isinstance(payload.get("mappings"), list) or not isinstance(payload.get("waivers"), list):
        raise ValueError("snapshot mappings and waivers must be lists")
    for index, row in enumerate(payload["mappings"]):
        if not isinstance(row, dict):
            raise ValueError(f"mappings[{index}] must be an object")
        for field in ("requirement_id", "requirement_revision", "repo", "work_id", "run_id", "policy_version"):
            _nonempty(row.get(field), field=f"mappings[{index}].{field}")
        if type(row.get("source_generation")) is not int or row["source_generation"] < 0:
            raise ValueError(f"mappings[{index}].source_generation must be non-negative")
        candidate = _nonempty(row.get("candidate_sha"), field=f"mappings[{index}].candidate_sha").lower()
        if _GIT_SHA_RE.fullmatch(candidate) is None:
            raise ValueError(f"mappings[{index}].candidate_sha is invalid")
        acceptance_ids = row.get("acceptance_ids")
        if not isinstance(acceptance_ids, list) or not acceptance_ids or any(
            not isinstance(item, str) or not item for item in acceptance_ids
        ) or len(acceptance_ids) != len(set(acceptance_ids)):
            raise ValueError(f"mappings[{index}].acceptance_ids is invalid")
        workflow_step_ids = row.get("workflow_step_ids")
        if not isinstance(workflow_step_ids, list) or not workflow_step_ids or any(
            not isinstance(item, str) or not item for item in workflow_step_ids
        ):
            raise ValueError(f"mappings[{index}].workflow_step_ids is invalid")
        for field in ("profile_key",):
            value = row.get(field)
            if value is not None and _PROFILE_KEY_RE.fullmatch(str(value)) is None:
                raise ValueError(f"mappings[{index}].{field} is invalid")
        for field in ("config_revision",):
            value = row.get(field)
            if value is not None:
                _digest(value, field=f"mappings[{index}].{field}")
        ref = row.get("completion_record")
        if ref is not None:
            if not isinstance(ref, dict) or set(ref) != {"locator", "sha256"}:
                raise ValueError(f"mappings[{index}].completion_record is invalid")
            _locator(ref.get("locator"), field=f"mappings[{index}].completion_record.locator")
            _digest(ref.get("sha256"), field=f"mappings[{index}].completion_record.sha256")
    for index, waiver in enumerate(payload["waivers"]):
        if not isinstance(waiver, dict):
            raise ValueError(f"waivers[{index}] must be an object")
    return copy.deepcopy(payload)


def _open_parent(path: Path, *, create: bool) -> int:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute():
        raise ValueError("index path must be absolute")
    descriptor = os.open("/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_at(directory_fd: int, name: str, *, limit: int = INDEX_MAX_BYTES) -> bytes | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError("index/source file is unsafe")
        chunks = bytearray()
        while len(chunks) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(descriptor)
        if len(chunks) > limit or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ValueError("index/source file changed while being read")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _read_beneath(root: Path, locator: str, *, limit: int = SOURCE_MAX_BYTES) -> bytes:
    safe = _locator(locator, field="evidence locator")
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("evidence root is unavailable or symlinked")
    directory_fd = _open_parent(root, create=False)
    parts = PurePosixPath(safe).parts
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
        content = _read_at(directory_fd, parts[-1], limit=limit)
        if content is None:
            raise ValueError(f"evidence missing: {safe}")
        return content
    finally:
        os.close(directory_fd)


def _load_json_beneath(root: Path, locator: str) -> tuple[object, bytes]:
    content = _read_beneath(root, locator)
    try:
        return json.loads(content.decode("utf-8")), content
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"evidence JSON invalid: {_locator(locator, field='locator')}") from exc


def _load_index_at(directory_fd: int, name: str) -> tuple[dict[str, Any], str | None]:
    raw = _read_at(directory_fd, name)
    if raw is None:
        return {
            "schema": INDEX_DOCUMENT_SCHEMA,
            "schema_version": INDEX_SCHEMA,
            "generation": 0,
            "manifest_id": None,
            "manifest_sha256": None,
            "snapshot_sha256": None,
            "mappings": [],
            "gaps": [],
            "reconcile_receipt": None,
            "extensions": {},
        }, None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("requirement delivery index is corrupt") from exc
    if not isinstance(value, dict) or value.get("schema") != INDEX_DOCUMENT_SCHEMA:
        raise ValueError("requirement delivery index schema is invalid")
    version = value.get("schema_version")
    if type(version) is not int or version != INDEX_SCHEMA:
        raise ValueError("unsupported index schema version")
    known = {
        "schema", "schema_version", "generation", "manifest_id", "manifest_sha256",
        "snapshot_sha256", "mappings", "gaps", "reconcile_receipt", "extensions",
    }
    extras = {key: value[key] for key in value.keys() - known}
    extensions = value.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ValueError("index extensions must be an object")
    normalized = {key: copy.deepcopy(value[key]) for key in known if key in value}
    normalized.setdefault("generation", 0)
    normalized.setdefault("manifest_id", None)
    normalized.setdefault("manifest_sha256", None)
    normalized.setdefault("snapshot_sha256", None)
    normalized.setdefault("mappings", [])
    # 舊版 reader 會把未知的 top-level gaps 收入 extensions；新 reader 可安全升級回投影欄位。
    extensions = copy.deepcopy(extensions)
    normalized.setdefault("gaps", extensions.pop("gaps", []))
    normalized.setdefault("reconcile_receipt", None)
    normalized["extensions"] = {**copy.deepcopy(extensions), **copy.deepcopy(extras)}
    if (
        type(normalized["generation"]) is not int
        or not isinstance(normalized["mappings"], list)
        or not isinstance(normalized["gaps"], list)
    ):
        raise ValueError("requirement delivery index fields are invalid")
    revision = hashlib.sha256(raw).hexdigest()
    return normalized, revision


def read_index(path: str | Path) -> dict[str, Any]:
    """安全讀取 sidecar；不碰 WorkflowRun registry，也不自動 migrate future schema。"""
    path = Path(path)
    if not path.is_absolute() or path.name in {"", ".", ".."} or "/" in path.name:
        raise ValueError("index path must be an absolute file path")
    try:
        directory_fd = _open_parent(path.parent, create=False)
    except FileNotFoundError:
        return {
            "schema": INDEX_DOCUMENT_SCHEMA,
            "schema_version": INDEX_SCHEMA,
            "generation": 0,
            "manifest_id": None,
            "manifest_sha256": None,
            "snapshot_sha256": None,
            "mappings": [],
            "gaps": [],
            "reconcile_receipt": None,
            "extensions": {},
            "revision": None,
        }
    try:
        index, revision = _load_index_at(directory_fd, path.name)
        return index | {"revision": revision}
    finally:
        os.close(directory_fd)


def _atomic_replace(path: Path, raw: bytes, *, expected_revision: str | None) -> str:
    if path.name in {"", ".", ".."} or "/" in path.name:
        raise ValueError("index file name is invalid")
    directory_fd = _open_parent(path.parent, create=True)
    lock_fd = -1
    temp_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        lock_fd = os.open(
            f"{path.name}.lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError("index lock is unsafe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        current = _read_at(directory_fd, path.name)
        current_revision = hashlib.sha256(current).hexdigest() if current is not None else None
        if current_revision != expected_revision:
            raise IndexConflict("requirement delivery index revision changed; re-evaluate sources")
        descriptor = os.open(
            temp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
        return hashlib.sha256(raw).hexdigest()
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def _stage(status: str, reason: str, *, locator: str | None = None, digest: str | None = None, validator: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"status": status, "reason": reason}
    if locator is not None:
        row["locator"] = locator
    if digest is not None:
        row["sha256"] = digest
    if validator is not None:
        row["validator"] = validator
    return row


def _completion_record(
    ref: object,
    *,
    evidence_root: Path,
    coordinator_root: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if ref is None:
        return None, None
    if not isinstance(ref, dict) or set(ref) != {"locator", "sha256"}:
        raise ValueError("completion_record reference is invalid")
    locator = _locator(ref.get("locator"), field="completion_record.locator")
    digest = _digest(ref.get("sha256"), field="completion_record.sha256")
    raw = _read_beneath(evidence_root, locator)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("completion record JSON is invalid") from exc
    normalized = completion.validate_completion_record(payload)
    # CompletionRecord 自己的 verify/review refs 也只能落在登記的 coordinator evidence 樹內；
    # 先用 descriptor-relative no-follow reader 預讀，才交給正式 domain validator。
    for name in ("verification_evidence_path", "review_evaluation_path"):
        value = normalized.get(name)
        if value is None:
            continue
        path = Path(str(value))
        try:
            relative = path.absolute().relative_to(Path(coordinator_root).absolute()).as_posix()
            _read_beneath(coordinator_root, relative)
        except (OSError, ValueError) as exc:
            raise ValueError(f"completion {name} escapes trusted evidence root") from exc
    record = completion.read_completion_record(evidence_root / locator, expected_hash=digest)
    return record, {"locator": locator, "sha256": digest, "payload": payload}


def _completion_child_json(record_path: object, *, coordinator_root: Path) -> tuple[str, dict[str, Any]]:
    path = Path(str(record_path))
    locator = path.absolute().relative_to(Path(coordinator_root).absolute()).as_posix()
    safe_locator = _locator(locator, field="CompletionRecord evidence locator")
    raw = _read_beneath(coordinator_root, safe_locator)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("CompletionRecord child evidence must be an object")
    return safe_locator, value


def _authority_matches(row: Mapping[str, Any], authority: object, record: Mapping[str, Any], *, now_epoch: float) -> None:
    if not isinstance(authority, claim.WorkAuthority):
        raise ValueError("confirmed WorkAuthority is unavailable")
    delivery._validate_work_authority(authority, now_epoch=now_epoch)
    if row.get("repo") != authority.repo or row.get("work_id") != authority.work_id:
        raise ValueError("mapping identity differs from fresh WorkAuthority")
    if row.get("pr_number") not in authority.mapped_prs or len(authority.mapped_prs) != 1:
        raise ValueError("mapping PR is not authorized by WorkAuthority")
    if row.get("change") not in authority.mapped_openspec:
        raise ValueError("mapping OpenSpec change is not authorized")
    # 對抗審查第六輪 MAJOR：多 todo 授權時，這裡曾以 tuple 逐序比對；同一組
    # 已授權路徑只因 producer 紀錄順序不同（例如 row 與 authority 排序不一致）
    # 就會被誤判 not-ready。改以集合比對——缺漏或多餘路徑（不論是否重複）
    # 仍會讓集合不相等而 fail closed，只是不再要求順序一致。
    if set(row.get("todo_paths", ())) != set(authority.mapped_todo_paths):
        raise ValueError("mapping Todo paths are not authorized")
    wire = record.get("work_authority")
    if not isinstance(wire, Mapping):
        raise ValueError("CompletionRecord has no WorkAuthority")
    expected = {
        "repo": authority.repo,
        "work_id": authority.work_id,
        "snapshot_hash": authority.snapshot_hash,
        "provider_id": authority.github_provider_id,
        "provider_revision": authority.github_provider_revision,
        "source_revisions": sorted(authority.source_revisions),
        "mapped_issues": sorted(authority.mapped_issues),
        "mapped_prs": sorted(authority.mapped_prs),
        "mapped_openspec": sorted(authority.mapped_openspec),
        "mapped_todo_paths": sorted(authority.mapped_todo_paths),
        "pr_number": row.get("pr_number"),
        "change": row.get("change"),
        "todo_paths": sorted(row.get("todo_paths", ())),
        "run_id": row.get("run_id"),
        "workflow_step_ids": sorted(row.get("workflow_step_ids", ())),
    }
    for key, value in expected.items():
        actual = wire.get(key)
        if key in {"source_revisions", "mapped_issues", "mapped_prs", "mapped_openspec", "mapped_todo_paths", "todo_paths", "workflow_step_ids"} and isinstance(actual, (list, tuple)):
            actual = sorted(actual)
        if actual != value:
            raise ValueError(f"CompletionRecord WorkAuthority {key} mismatch")


def _requirement_owner_matches(requirement: Mapping[str, Any], authority: object) -> bool:
    """將需求 owner 的 repo#issue 範圍綁到正式 WorkAuthority。"""
    if not isinstance(authority, claim.WorkAuthority):
        return False
    owner = requirement.get("evidence_policy", {}).get("owner")
    refs = owner.get("work_ids") if isinstance(owner, Mapping) else None
    if not isinstance(refs, list):
        return False
    for reference in refs:
        if not isinstance(reference, str):
            continue
        match = re.fullmatch(r"(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<issue>[1-9][0-9]*)", reference)
        if match is None:
            continue
        repo = match.group("repo")
        if int(match.group("issue")) in authority.mapped_issues and (repo is None or repo == authority.repo):
            return True
    return False


def _read_completion_stages(
    ref: object,
    *,
    row: Mapping[str, Any],
    context: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any], dict[str, Any] | None, object | None]:
    """回傳 row 層級的 test/review 階段結果；test_result 為 "verified" 時仍須由呼叫端
    以 verification_doc 逐條 acceptance criterion 判定明確綁定的通過測試，不可直接採信。"""
    locator = ref.get("locator") if isinstance(ref, Mapping) else None
    digest = ref.get("sha256") if isinstance(ref, Mapping) else None
    if ref is None:
        missing = _stage("missing", "completion-record-missing")
        return missing, None, missing.copy(), None, None
    try:
        record, _ref_doc = _completion_record(
            ref,
            evidence_root=Path(context["evidence_root"]),
            coordinator_root=Path(context["coordinator_root"]),
        )
        if record is None:
            raise ValueError("completion record missing")
        if record["candidate"] != str(row.get("candidate_sha", "")).lower():
            raise ValueError("CompletionRecord candidate mismatch")
        if record.get("work_authority") is None:
            raise ValueError("CompletionRecord WorkAuthority missing")
        authority = context["authority_loader"](row["repo"], row["work_id"])
        _authority_matches(row, authority, record, now_epoch=float(context["now_epoch"]))
        verification_locator, verification_payload = _completion_child_json(
            record["verification_evidence_path"], coordinator_root=Path(context["coordinator_root"])
        )
        verification_doc = verification.validate_verification_evidence(verification_payload)
        if verification_doc["candidate"] != row.get("candidate_sha"):
            raise ValueError("verification evidence candidate mismatch")
        if verification_doc["status"] not in {"reviewing", "verified"}:
            raise ValueError("verification evidence is not passed")
        # 只表示 run 層級的驗證通過；是否覆蓋某 acceptance criterion 交給呼叫端
        # 依 verification_doc 的逐條測試綁定重新判定，不可用此 run 狀態直接推定通過。
        test_result = _stage("verified", "completion-verification-passed", locator=verification_locator, digest=str(record["verification_evidence_hash"]), validator="completion/v1")
        review_result = _stage("missing", "review-evidence-missing")
        review_payload: object | None = None
        if record.get("review_policy") == "required":
            review_locator, review_payload = _completion_child_json(
                record["review_evaluation_path"], coordinator_root=Path(context["coordinator_root"])
            )
            review_doc = review.validate_gate_evaluation(review_payload)
            if review_doc["candidate"] != row.get("candidate_sha") or review_doc["state"] != "passed":
                raise ValueError("review evidence does not authorize exact candidate")
            identities = review_doc.get("launch_identity")
            builder = identities.get("builder") if isinstance(identities, Mapping) else None
            reviewer = identities.get("reviewer") if isinstance(identities, Mapping) else None
            if not isinstance(builder, Mapping) or not isinstance(reviewer, Mapping):
                raise ValueError("review independence identity is unknown")
            builder_domain = builder.get("independence_domain")
            reviewer_domain = reviewer.get("independence_domain")
            if not isinstance(builder_domain, str) or not isinstance(reviewer_domain, str) or builder_domain == reviewer_domain:
                raise ValueError("review independence domains are not distinct")
            review_result = _stage("verified", "independent-review-passed", locator=review_locator, digest=str(record["review_evaluation_hash"]), validator="foreign-review/v1")
        return test_result, verification_doc, review_result, record, authority
    except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RuntimeError, TypeError) as exc:
        message = str(exc).casefold()
        status = "unknown" if "fresh" in message or "stale" in message else (
            "stale" if "candidate mismatch" in message or "revision mismatch" in message else "failed"
        )
        result = _stage(status, f"completion-invalid:{type(exc).__name__}", locator=str(locator) if locator else None, digest=str(digest) if digest else None)
        return result, None, result.copy(), None, None


def _verify_remote_merge(
    row: Mapping[str, Any],
    record: Mapping[str, Any] | None,
    authority: object | None,
    *,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    if record is None or not isinstance(authority, claim.WorkAuthority):
        return _stage("unknown", "completion-or-authority-unverified")
    try:
        checkout = context["checkout_resolver"](row["repo"])
        facts = context["github_client"].fetch_remote_closure(
            repo=row["repo"],
            pr_number=authority.mapped_prs[0],
            change=row.get("change"),
            required_issues=authority.mapped_issues,
            todo_paths=authority.mapped_todo_paths,
            canonical_checkout=checkout,
            include_closing_issues=True,
            # 對抗審查第六輪 BLOCKER：`todo_complete` 若讀「目前」default head，
            # PR 合併當下 mapped todo 尚未全勾、之後別的無關 commit 才補勾，重跑
            # reconcile 會把這筆交付從 blocked 誤判成 covered——todo 完成度必須
            # 綁這筆 merge 當下（merge commit 的 tree）的內容，不是「現在」。
            todo_at_merge_commit=True,
        )
        if not isinstance(facts, github_delivery.RemoteClosureFacts):
            raise ValueError("remote closure facts have an unknown shape")
        gate = github_delivery.evaluate_remote_closure(
            facts=replace(facts, completion_record_valid=True),
            required_issues=authority.mapped_issues,
            expected_head=str(row.get("candidate_sha", "")).lower(),
        )
        wire = record.get("work_authority")
        if not isinstance(wire, Mapping):
            raise ValueError("CompletionRecord WorkAuthority missing")
        if wire.get("merge_commit") != facts.merge_commit.lower():
            return _stage("failed", "remote-merge-commit-mismatch")
        # 對抗審查第四輪 BLOCKER：`record.target_ref_sha` 是合併當下捕捉、不可變
        # 的歷史快照；其他 PR 之後推進 default branch 是正常事件，會讓「目前」
        # `facts.default_head` 不再等於它。已完成的交付只要求這條 PR 的
        # merge commit 仍是「目前」default head 的祖先（merge ancestry）——這正是
        # `evaluate_remote_closure` 透過 `facts.merge_is_ancestor`（對目前 default
        # head 即時計算）已經驗證的條件，不需要、也不應該再對歷史快照要求相等，
        # 否則會讓已 covered 的需求因為之後的正常推進而退化。這裡不改
        # `evaluate_remote_closure` 本身的語意（其他呼叫者仍照舊），只是不在
        # consumer 端疊加這條錯誤的額外相等檢查。
        if facts.pr_head.lower() != str(row.get("candidate_sha", "")).lower():
            return _stage("failed", "remote-pr-head-mismatch")
        target = row.get("target")
        if isinstance(target, Mapping) and target.get("source_revision", "").lower() != facts.merge_commit.lower():
            return _stage("failed", "installed-source-revision-does-not-match-merge")
        # `evaluate_remote_closure` 只看 issue 目前是否 closed，不驗證是「這個」PR
        # 關閉的——issue 可能被人工或另一個 PR 關閉，mapped todo 也可能仍有未勾選項。
        # 這兩條是本 consumer（requirement-delivery）對 merge 階段加嚴的必要條件，
        # 不能改共用 gate 的語意，因此在這裡另外檢查。
        unreferenced_issues = set(authority.mapped_issues) - set(facts.closing_issues)
        if unreferenced_issues:
            return _stage("failed", "remote-closing-issue-reference-missing")
        if not facts.todo_complete:
            return _stage("failed", "remote-todo-incomplete")
        if not gate.allowed:
            return _stage("failed", "remote-closure-blocked:" + ",".join(gate.reasons))
        # 對抗審查第五輪 MAJOR：這個 digest／mapping 身分只能綁這筆交付不可變
        # 的事實（merge commit、PR head、mapped issues、todo 狀態）；「目前」
        # default head 只是驗證輸入（見上方 merge ancestry 檢查），不能入身分
        # ——否則其他 PR 之後正常推進 default branch 就會讓同一筆已驗證的
        # merge 證據被判定成內容改變，把 main 正常前進誤判成
        # same-source-generation-content-drift。
        result = _stage(
            "verified", "exact-remote-closure-passed",
            locator=f"pull/{authority.mapped_prs[0]}/merge/{facts.merge_commit.lower()}",
            digest=canonical_json_sha256({
                "merge": facts.merge_commit.lower(),
                "head": facts.pr_head.lower(),
                "pr_number": authority.mapped_prs[0],
                "mapped_issues": sorted(authority.mapped_issues),
                "todo_complete": facts.todo_complete,
            }),
            validator="remote-closure/v1",
        )
        result.update({
            "merge_sha": facts.merge_commit.lower(),
            "head_sha": facts.pr_head.lower(),
            # `target_sha` 只是驗證當下觀察到的「目前」default head，供報告
            # 參考；不是不可變事實，因此不進上面的 digest，且持久化到索引
            # 前會由 `_stable_evidence` 剔除，不列入同 generation 內容比對。
            "target_sha": facts.default_head.lower(),
        })
        return result
    except (KeyError, OSError, ValueError, RuntimeError, TypeError) as exc:
        return _stage("unknown", f"remote-closure-unverified:{type(exc).__name__}")


def _verify_source(requirement: Mapping[str, Any], *, source_root: Path) -> dict[str, Any]:
    source_ref = requirement["source_ref"]
    try:
        content = _read_beneath(source_root, source_ref["locator"])
        digest = hashlib.sha256(content).hexdigest()
        if digest != source_ref["sha256"].lower():
            return _stage("failed", "source-authority-hash-mismatch", locator=source_ref["locator"], digest=digest)
        return _stage("verified", "versioned-source-authority-matched", locator=source_ref["locator"], digest=digest, validator="manifest-source/v1")
    except (KeyError, OSError, ValueError) as exc:
        return _stage("unknown", f"source-authority-unavailable:{type(exc).__name__}")


def _verify_installed(row: Mapping[str, Any], *, context: Mapping[str, Any]) -> dict[str, Any]:
    installed = row.get("installed_runtime")
    target = row.get("target")
    if not isinstance(installed, Mapping) or not isinstance(target, Mapping):
        return _stage("missing", "installed-runtime-receipt-missing")
    try:
        resolver = context.get("runtime_state_resolver")
        if not callable(resolver):
            return _stage("unknown", "loaded-runtime-state-root-resolver-unavailable", validator="loaded-runtime-attestation/v1")
        state_root = Path(resolver(
            str(installed.get("service")),
            str(installed.get("instance")),
            installed.get("state_root"),
        ))
        if not state_root.is_absolute():
            return _stage("unknown", "loaded-runtime-state-root-not-canonical", validator="loaded-runtime-attestation/v1")
        expected_target = _target_identity(row)
        if installed.get("target") != expected_target or target != expected_target:
            return _stage("failed", "installed-acceptance-target-mismatch")
        config_revision = _digest(installed.get("declared_config_revision"), field="installed declared config revision")
        if config_revision != expected_target["config_revision"]:
            return _stage("stale", "installed-config-revision-stale")
        status_resolver = context.get("runtime_status_resolver")
        if callable(status_resolver):
            report = status_resolver(str(installed.get("service")), str(installed.get("instance")))
        else:
            report = runtime_attestation.runtime_status_report(
                state_root,
                service=str(installed.get("service")),
                instance=str(installed.get("instance")),
                declared_config_revision=config_revision,
                expected_pid=installed.get("expected_pid"),
                require_process_match=True,
                current_artifact=context.get("current_artifact"),
            )
        if not isinstance(report, Mapping):
            return _stage("unknown", "loaded-runtime-status-projection-unavailable", validator="loaded-runtime-attestation/v1")
        latest = report.get("loaded")
        if report.get("status") != "match" or not isinstance(latest, Mapping):
            if report.get("comparison", {}).get("artifact_status") == "drift" or report.get("comparison", {}).get("config_status") == "drift":
                return _stage("stale", f"loaded-runtime-{report.get('reason') or 'drift'}", validator="loaded-runtime-attestation/v1")
            return _stage("unknown", f"loaded-runtime-{report.get('reason') or 'unknown'}", validator="loaded-runtime-attestation/v1")
        artifact = latest.get("artifact")
        config = latest.get("config")
        components = config.get("components") if isinstance(config, Mapping) else None
        if (
            not isinstance(artifact, Mapping)
            or artifact.get("kind") != "installed-wheel"
            or artifact.get("sha256") != expected_target["artifact_sha256"]
            or artifact.get("source_revision") != expected_target["source_revision"]
            or latest.get("service") != expected_target["service"]
            or latest.get("instance") != expected_target["instance"]
            or latest.get("pid") != installed.get("expected_pid")
            or not isinstance(components, Mapping)
            or components.get("profile_key") != expected_target["profile_key"].rsplit(":", 1)[-1]
            or config.get("effective_revision") != expected_target["config_revision"]
            or report.get("trust_root", {}).get("status") != "verified"
        ):
            if isinstance(components, Mapping) and components.get("profile_key") != expected_target["profile_key"].rsplit(":", 1)[-1]:
                return _stage("stale", "loaded-runtime-profile-key-stale", validator="loaded-runtime-attestation/v1")
            if isinstance(config, Mapping) and config.get("effective_revision") != expected_target["config_revision"]:
                return _stage("stale", "loaded-runtime-config-revision-stale", validator="loaded-runtime-attestation/v1")
            return _stage("failed", "loaded-runtime-identity-or-target-mismatch", validator="loaded-runtime-attestation/v1")
        return _stage("verified", "installed-artifact-and-loaded-runtime-matched", locator=str(latest.get("receipt_id")), digest=canonical_json_sha256(latest), validator="loaded-runtime-attestation/v1")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return _stage("unknown", f"loaded-runtime-unverified:{type(exc).__name__}")


def _target_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("target")
    if not isinstance(value, Mapping):
        raise ValueError("acceptance target is missing")
    fields = ("repo", "candidate_sha", "artifact_sha256", "source_revision", "service", "instance", "profile_key", "config_revision")
    if any(not isinstance(value.get(field), str) or not value.get(field) for field in fields):
        raise ValueError("acceptance target is incomplete")
    if _GIT_SHA_RE.fullmatch(value["candidate_sha"].lower()) is None or _GIT_SHA_RE.fullmatch(value["source_revision"].lower()) is None:
        raise ValueError("acceptance target source/candidate revision is invalid")
    _digest(value["artifact_sha256"], field="target artifact sha256")
    _digest(value["config_revision"], field="target config revision")
    if _PROFILE_KEY_RE.fullmatch(value["profile_key"]) is None:
        raise ValueError("acceptance target profile_key is invalid")
    if value["candidate_sha"].lower() != str(row.get("candidate_sha", "")).lower():
        raise ValueError("acceptance target candidate differs from mapping")
    if value["repo"] != row.get("repo"):
        raise ValueError("acceptance target repository differs from mapping")
    if row.get("profile_key") is not None and value["profile_key"] != row.get("profile_key"):
        raise ValueError("acceptance target profile_key differs from mapping")
    if row.get("config_revision") is not None and value["config_revision"] != row.get("config_revision"):
        raise ValueError("acceptance target config revision differs from mapping")
    return {field: value[field] for field in fields}


def _verify_live(
    row: Mapping[str, Any],
    requirement: Mapping[str, Any],
    acceptance_id: str,
    *,
    evidence_root: Path,
    now: datetime,
    validator: Callable[[Mapping[str, Any]], bool] | None,
    review_document: Mapping[str, Any] | None,
    domain_deriver: Callable[[Mapping[str, Any]], str | None] | None,
) -> dict[str, Any]:
    ref = row.get("live_receipt")
    if ref is None:
        return _stage("missing", "live-canary-receipt-missing")
    if not isinstance(ref, Mapping) or set(ref) != {"locator", "sha256"}:
        return _stage("failed", "live-canary-reference-invalid")
    try:
        locator = _locator(ref.get("locator"), field="live_receipt.locator")
        expected_digest = _digest(ref.get("sha256"), field="live_receipt.sha256")
        receipt, raw = _load_json_beneath(evidence_root, locator)
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            return _stage("failed", "live-canary-hash-mismatch", locator=locator)
        if not isinstance(receipt, Mapping) or receipt.get("schema") != "cortex/live-canary-receipt/v1":
            return _stage("unknown", "live-canary-validator-schema-unavailable", locator=locator, digest=expected_digest)
        target = _target_identity(row)
        if (
            receipt.get("result") != "passed"
            or receipt.get("requirement_id") != requirement["id"]
            or receipt.get("requirement_revision") != requirement["revision"]
            or receipt.get("acceptance_id") != acceptance_id
            or receipt.get("target") != target
        ):
            return _stage("failed", "live-canary-acceptance-target-mismatch", locator=locator, digest=expected_digest)
        observed_at = _timestamp(receipt.get("observed_at"), field="live receipt observed_at")
        max_age = requirement["evidence_policy"]["max_age_seconds"].get("live")
        if observed_at > now or (max_age is not None and (now - observed_at).total_seconds() > max_age):
            return _stage("stale", "live-canary-receipt-expired", locator=locator, digest=expected_digest)
        authority = receipt.get("authority")
        independence = receipt.get("independence")
        if not isinstance(authority, Mapping) or not all(isinstance(authority.get(key), str) and authority.get(key) for key in ("id", "version", "receipt")):
            return _stage("unknown", "live-canary-approval-authority-unknown", locator=locator, digest=expected_digest)
        if not isinstance(independence, Mapping):
            return _stage("unknown", "live-canary-independence-unknown", locator=locator, digest=expected_digest)
        reviewer = review_document.get("launch_identity", {}).get("reviewer") if isinstance(review_document, Mapping) else None
        reviewer_domain = reviewer.get("independence_domain") if isinstance(reviewer, Mapping) else None
        review_domain = independence.get("review_domain")
        if not isinstance(review_domain, str) or (reviewer_domain is not None and review_domain != reviewer_domain):
            return _stage("failed", "live-canary-independence-mismatch", locator=locator, digest=expected_digest)
        # 對抗審查第九輪 BLOCKER：canary_domain 不得只信 receipt 自報字串——
        # 由 `domain_deriver`（production 為 `live_receipt_validators.
        # derive_canary_domain`）依 receipt kind 與已驗證的 evidence 導出；
        # independence 檢查只用導出值。receipt 若仍自報 canary_domain，該值
        # 必須與導出值完全相同，否則拒絕；無法導出（kind 未登記、evidence
        # 缺對應欄位）一律 fail closed，不得代入預設值放行。
        if domain_deriver is None:
            return _stage("unknown", "governed-live-receipt-domain-deriver-unavailable", locator=locator, digest=expected_digest)
        try:
            canary_domain = domain_deriver(receipt)
        except Exception:
            canary_domain = None
        reported_canary_domain = independence.get("canary_domain")
        if (
            not isinstance(canary_domain, str)
            or not canary_domain
            or canary_domain == review_domain
            or (reported_canary_domain is not None and reported_canary_domain != canary_domain)
        ):
            return _stage("failed", "live-canary-independence-mismatch", locator=locator, digest=expected_digest)
        if validator is None:
            return _stage("unknown", "governed-live-receipt-validator-unavailable", locator=locator, digest=expected_digest)
        if validator(receipt) is not True:
            return _stage("failed", "governed-live-receipt-validator-rejected", locator=locator, digest=expected_digest)
        return _stage("verified", "fresh-live-canary-matched-exact-target", locator=locator, digest=expected_digest, validator="live-canary-domain/v1")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _stage("unknown", f"live-canary-unverified:{type(exc).__name__}")


def _test_stage_for_criterion(
    verification_doc: Mapping[str, Any] | None,
    acceptance_id: str,
    *,
    locator: str | None,
    digest: str | None,
) -> dict[str, Any]:
    """只採信 CompletionRecord 驗證證據中明確綁定該 acceptance criterion 且通過的測試；
    verification run 的整體狀態（reviewing／ongoing 等）不可用來推定該 criterion 已通過。"""
    tests: object = None
    if isinstance(verification_doc, Mapping):
        details = verification_doc.get("details")
        if isinstance(details, Mapping):
            tests = details.get("tests")
    if not isinstance(tests, list):
        return _stage("missing", "verification-evidence-has-no-criterion-bound-tests")
    bound = [
        entry
        for entry in tests
        if isinstance(entry, Mapping)
        and isinstance(entry.get("acceptance_ids"), list)
        and acceptance_id in entry["acceptance_ids"]
    ]
    if not bound:
        return _stage("missing", "no-test-bound-to-acceptance-criterion")
    if any(entry.get("status") == "passed" for entry in bound):
        return _stage(
            "verified",
            "criterion-bound-test-passed",
            locator=locator,
            digest=digest,
            validator="completion/v1",
        )
    return _stage("failed", "criterion-bound-test-not-passed")


def _verify_waiver(
    waiver: Mapping[str, Any],
    *,
    requirement: Mapping[str, Any],
    acceptance_id: str,
    stage_name: str,
    now: datetime,
    validator: Callable[[Mapping[str, Any]], bool] | None,
) -> dict[str, Any] | None:
    if waiver.get("requirement_id") != requirement["id"] or waiver.get("requirement_revision") != requirement["revision"] or waiver.get("acceptance_id") != acceptance_id or waiver.get("stage") != stage_name:
        return None
    if stage_name in {"installed", "live"}:
        raise ValueError("cannot waive installed/live evidence")
    if stage_name not in requirement["evidence_policy"]["waivable_stages"]:
        return _stage("failed", "waiver-not-allowed-by-requirement-policy")
    try:
        if set(waiver) != {"requirement_id", "requirement_revision", "acceptance_id", "stage", "reason", "authority", "expires_at", "receipt"}:
            raise ValueError("waiver scope is incomplete")
        _nonempty(waiver.get("reason"), field="waiver.reason")
        _nonempty(waiver.get("receipt"), field="waiver.receipt")
        authority = waiver.get("authority")
        if not isinstance(authority, Mapping) or set(authority) != {"id", "version"}:
            raise ValueError("waiver approval authority is missing")
        allowed = requirement.get("_waiver_authorities", [])
        if not any(row.get("id") == authority.get("id") and row.get("version") == authority.get("version") for row in allowed):
            raise ValueError("waiver authority/version is not policy-approved")
        if _timestamp(waiver.get("expires_at"), field="waiver.expires_at") <= now:
            raise ValueError("waiver has expired")
        if validator is None or validator(waiver) is not True:
            return _stage("unknown", "waiver-approval-validator-unavailable")
        return _stage("not-applicable", "scoped-policy-approved-waiver")
    except (ValueError, TypeError) as exc:
        return _stage("failed", f"waiver-invalid:{type(exc).__name__}")


def _current_generation_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """對抗審查第五輪 BLOCKER：同一 requirement／criterion 下，若同一 repo／
    work／run 邏輯範圍在單次 source_snapshot 內同時出現多個 source_generation
    的 mapping row（例如較舊 covered 與較新 blocked 同時存在、producer 尚未
    清理掉舊紀錄），判定 covered／blocked／missing 前必須先依這個邏輯範圍
    只留最高 generation 的 row 決定——與 reconcile_delivery 跨 mapping
    generation 守門同一套「以最高 generation 為單一真相」規則，不能只要看到
    任一 covered row 就通過，讓已被取代的較舊 covered row 蓋過較新的
    blocked／missing 事實。"""
    best_generation: dict[tuple[str, str, str], int] = {}
    best_rows: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in matches:
        scope = (row["repo"], row["work_id"], row["run_id"])
        generation = row.get("source_generation")
        generation = generation if isinstance(generation, int) else -1
        current_best = best_generation.get(scope)
        if current_best is None or generation > current_best:
            best_generation[scope] = generation
            best_rows[scope] = [row]
        elif generation == current_best:
            best_rows[scope].append(row)
    effective: list[dict[str, Any]] = []
    for scope_rows in best_rows.values():
        effective.extend(scope_rows)
    return effective


def inspect_delivery(
    manifest: object,
    source_snapshot: object,
    *,
    source_root: str | Path,
    evidence_root: str | Path,
    coordinator_root: str | Path,
    authority_loader: Callable[[str, str], object],
    github_client: object,
    checkout_resolver: Callable[[str], str | Path],
    current_artifact: Mapping[str, object] | None,
    now_epoch: int | float,
    runtime_state_resolver: Callable[[str, str, object], str | Path] | None = None,
    runtime_status_resolver: Callable[[str, str], Mapping[str, Any]] | None = None,
    live_receipt_validator: Callable[[Mapping[str, Any]], bool] | None = None,
    live_receipt_domain_deriver: Callable[[Mapping[str, Any]], str | None] | None = None,
    waiver_validator: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """唯讀重驗 requirement/evidence；永不派工、merge、部署或改 issue。"""
    manifest_doc = validate_manifest(manifest, authority_root=source_root)
    snapshot = _validate_snapshot(source_snapshot)
    if isinstance(now_epoch, bool) or not isinstance(now_epoch, (int, float)):
        raise ValueError("now_epoch must be finite numeric")
    now_epoch = float(now_epoch)
    if not (now_epoch == now_epoch and abs(now_epoch) != float("inf")):
        raise ValueError("now_epoch must be finite")
    now = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
    context = {
        "evidence_root": Path(evidence_root),
        "coordinator_root": Path(coordinator_root),
        "authority_loader": authority_loader,
        "github_client": github_client,
        "checkout_resolver": checkout_resolver,
        "current_artifact": current_artifact,
        "now_epoch": now_epoch,
        "runtime_state_resolver": runtime_state_resolver,
        "runtime_status_resolver": runtime_status_resolver,
    }
    requirements = {row["id"]: copy.deepcopy(row) for row in manifest_doc["requirements"]}
    for requirement in requirements.values():
        requirement["_waiver_authorities"] = manifest_doc["waiver_policy"]["authorities"]
    for waiver in snapshot["waivers"]:
        if isinstance(waiver, Mapping) and waiver.get("stage") in {"installed", "live"}:
            raise ValueError("cannot waive installed/live evidence")
    mapping_results: dict[tuple[str, str], list[dict[str, Any]]] = {}
    mapping_rows: list[dict[str, Any]] = []
    orphan_mappings: list[dict[str, str]] = []
    for row in snapshot["mappings"]:
        if not isinstance(row, dict):
            orphan_mappings.append({"reason": "mapping-not-object"})
            continue
        requirement = requirements.get(row.get("requirement_id"))
        if requirement is None:
            orphan_mappings.append({"requirement_id": str(row.get("requirement_id", "")), "reason": "unknown-requirement"})
            continue
        if row.get("requirement_revision") != requirement["revision"]:
            orphan_mappings.append({"requirement_id": requirement["id"], "reason": "requirement-revision-mismatch"})
            continue
        criteria = {item["id"] for item in requirement["acceptance_criteria"]}
        acceptance_ids = row.get("acceptance_ids")
        if not isinstance(acceptance_ids, list) or not acceptance_ids or any(item not in criteria for item in acceptance_ids):
            orphan_mappings.append({"requirement_id": requirement["id"], "reason": "acceptance-mapping-invalid"})
            continue
        logical_keys = {
            key: row.get(key) for key in ("repo", "work_id", "run_id")
        }
        if any(not isinstance(value, str) or not value for value in logical_keys.values()):
            orphan_mappings.append({"requirement_id": requirement["id"], "reason": "work-identity-missing"})
            continue
        source_result = _verify_source(requirement, source_root=Path(source_root))
        verification_doc: Mapping[str, Any] | None = None
        if row.get("policy_version") != requirement["evidence_policy"]["policy_version"]:
            policy_drift = _stage("stale", "requirement-evidence-policy-revision-changed")
            test_result = review_result = merge_result = installed_result = policy_drift
            record = authority = None
        else:
            test_result, verification_doc, review_result, record, authority = _read_completion_stages(
                row.get("completion_record"), row=row, context=context
            )
            owner_bound = authority is None or _requirement_owner_matches(requirement, authority)
            if not owner_bound:
                test_result = _stage("failed", "work-authority-not-listed-for-requirement-owner")
                review_result = _stage("failed", "work-authority-not-listed-for-requirement-owner")
                merge_result = _stage("failed", "work-authority-not-listed-for-requirement-owner")
            else:
                merge_result = _verify_remote_merge(row, record, authority, context=context)
            installed_result = _verify_installed(row, context=context)
        live_results: dict[str, dict[str, Any]] = {}
        review_doc = None
        if record is not None and record.get("review_evaluation_path"):
            try:
                _review_locator, review_doc = _completion_child_json(
                    record["review_evaluation_path"], coordinator_root=Path(coordinator_root)
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                pass
        for acceptance_id in acceptance_ids:
            live_results[acceptance_id] = _verify_live(
                row,
                requirement,
                acceptance_id,
                evidence_root=Path(evidence_root),
                now=now,
                validator=live_receipt_validator,
                review_document=review_doc,
                domain_deriver=live_receipt_domain_deriver,
            )
            # test_result 為 "verified" 只表示 run 層級驗證通過；仍須逐條 acceptance
            # criterion 找到明確綁定且通過的測試才算 covered，不可用 run 狀態推定。
            if test_result["status"] == "verified":
                acceptance_test_result = _test_stage_for_criterion(
                    verification_doc,
                    acceptance_id,
                    locator=test_result.get("locator"),
                    digest=test_result.get("sha256"),
                )
            else:
                acceptance_test_result = test_result
            stages = {
                "source": source_result,
                "test": acceptance_test_result,
                "review": review_result,
                "merge": merge_result,
                "installed": installed_result,
                "live": live_results[acceptance_id],
            }
            stage_bindings = {
                name: copy.deepcopy(value)
                for name, value in stages.items()
                if name in requirement["evidence_policy"]["required_stages"]
            }
            waivers = snapshot["waivers"]
            for stage_name in requirement["evidence_policy"]["required_stages"]:
                for waiver in waivers:
                    if isinstance(waiver, Mapping) and waiver.get("requirement_id") == requirement["id"] and waiver.get("acceptance_id") == acceptance_id and waiver.get("stage") == stage_name:
                        waiver_result = _verify_waiver(
                            waiver,
                            requirement=requirement,
                            acceptance_id=acceptance_id,
                            stage_name=stage_name,
                            now=now,
                            validator=waiver_validator,
                        )
                        if waiver_result is not None:
                            stage_bindings[stage_name] = waiver_result
                            break
            satisfied = all(result["status"] in {"verified", "not-applicable"} for result in stage_bindings.values()) and set(stage_bindings) == set(requirement["evidence_policy"]["required_stages"])
            stage_statuses = {value["status"] for value in stage_bindings.values()}
            mapping_status = "covered" if satisfied else ("blocked" if stage_statuses & {"failed", "stale", "unknown"} else "missing")
            try:
                target_identity = _target_identity(row)
            except (TypeError, ValueError):
                target_identity = None
            source_revision_sha = (
                canonical_json_sha256(sorted(authority.source_revisions))
                if isinstance(authority, claim.WorkAuthority) else None
            )
            authority_sha = (
                claim.work_authority_digest(authority)
                if isinstance(authority, claim.WorkAuthority) else None
            )
            mapping_identity = {
                "requirement_id": requirement["id"],
                "requirement_revision": requirement["revision"],
                "acceptance_id": acceptance_id,
                "repo": row["repo"],
                "work_id": row["work_id"],
                "run_id": row["run_id"],
                "workflow_step_ids": sorted(row["workflow_step_ids"]),
                "pr_number": row.get("pr_number"),
                "change": row.get("change"),
                "todo_paths": sorted(row.get("todo_paths", ())),
                "candidate_sha": row.get("candidate_sha"),
                "merge_sha": merge_result.get("merge_sha"),
                "profile_key": row.get("profile_key"),
                "config_revision": row.get("config_revision"),
                "policy_version": row.get("policy_version"),
                "source_revision_sha256": source_revision_sha,
                "authority_sha256": authority_sha,
                "completion_record": copy.deepcopy(row.get("completion_record")),
                "target": target_identity,
            }
            mapping_id = canonical_json_sha256(mapping_identity)
            result = {
                "mapping_id": mapping_id,
                **mapping_identity,
                "source_generation": row.get("source_generation"),
                "status": mapping_status,
                "evidence": stage_bindings,
                "observed_at": snapshot["captured_at"],
            }
            mapping_rows.append(result)
            mapping_results.setdefault((requirement["id"], acceptance_id), []).append(result)

    requirement_results: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for requirement in manifest_doc["requirements"]:
        criterion_results = []
        owner = requirement["evidence_policy"]["owner"]
        for criterion in requirement["acceptance_criteria"]:
            matches = mapping_results.get((requirement["id"], criterion["id"]), [])
            # 判定 covered 與否只信該邏輯範圍內最高 generation 的 row（見
            # `_current_generation_matches`），較舊 generation 不得蓋過較新事實。
            effective_matches = _current_generation_matches(matches)
            # 同一邏輯範圍（repo／work／run）在最高 generation 內若 covered 與非
            # covered 並存，代表事實互相矛盾（例如同代弱化重讀與舊結果並存），
            # 該範圍不得算 covered；只有某範圍的每一筆 row 都 covered 才成立。
            scope_rows: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
            for row in effective_matches:
                scope_rows.setdefault(
                    (row["repo"], row["work_id"], row["run_id"]), []
                ).append(row)
            criterion_covered = any(
                all(row["status"] == "covered" for row in rows)
                for rows in scope_rows.values()
            )
            criterion_status = "covered" if criterion_covered else ("blocked" if effective_matches else "missing")
            criterion_results.append({"acceptance_id": criterion["id"], "status": criterion_status, "mapping_ids": [row["mapping_id"] for row in matches]})
            if not criterion_covered:
                # 回報缺額時優先取非 covered 的 row，讓 gap 指向實際失敗的階段。
                gap_rows = [row for row in effective_matches if row["status"] != "covered"] or effective_matches
                candidate_stages = gap_rows[0]["evidence"] if gap_rows else {}
                for stage_name in requirement["evidence_policy"]["required_stages"]:
                    evidence = candidate_stages.get(stage_name)
                    if evidence is None:
                        status, reason = "missing", "no-mapping-for-acceptance-criterion"
                    else:
                        status, reason = evidence["status"], evidence["reason"]
                    if status not in {"verified", "not-applicable"}:
                        gaps.append({
                            "requirement_id": requirement["id"],
                            "requirement_revision": requirement["revision"],
                            "acceptance_id": criterion["id"],
                            "stage": stage_name,
                            "status": status,
                            "reason": reason,
                            "owner": copy.deepcopy(owner),
                            "recovery": owner["recovery"],
                            "mapping_id": effective_matches[0]["mapping_id"] if effective_matches else None,
                        })
        status = "covered" if all(row["status"] == "covered" for row in criterion_results) else ("blocked" if any(row["status"] == "blocked" for row in criterion_results) else "missing")
        requirement_results.append({
            "requirement_id": requirement["id"],
            "requirement_revision": requirement["revision"],
            "status": status,
            "acceptance_criteria": criterion_results,
            "owner": copy.deepcopy(owner),
        })
    return {
        "schema": REPORT_SCHEMA,
        "manifest_id": manifest_doc["manifest_id"],
        "manifest_sha256": canonical_json_sha256(manifest_doc),
        "snapshot_revision": snapshot["snapshot_revision"],
        "snapshot_sha256": canonical_json_sha256(snapshot),
        "closure_readiness": "ready" if requirement_results and all(row["status"] == "covered" for row in requirement_results) else "not-ready",
        "requirements": requirement_results,
        "mappings": mapping_rows,
        "gaps": gaps,
        "orphan_mappings": orphan_mappings,
    }


# 對抗審查第五輪 MAJOR：各階段 evidence 中，哪些欄位只是驗證當下觀察到的
# 「目前」事實（會隨時間正常變動、不是這筆交付的不可變身分），持久化與同
# generation 內容比對前必須先剔除——目前只有 merge 階段的 `target_sha`
# （目前 default head）。
_VOLATILE_EVIDENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "merge": ("target_sha",),
}


def _stable_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """剔除 `_VOLATILE_EVIDENCE_FIELDS` 列出的驗證當下觀察值，只留下可作為
    這筆交付身分依據的不可變事實，供索引持久化與同 generation 內容比對
    使用；即時查詢（`inspect_delivery` 的回傳值）仍保留完整欄位供人工/報告
    參考。"""
    stable: dict[str, Any] = {}
    for stage_name, stage in evidence.items():
        drop = _VOLATILE_EVIDENCE_FIELDS.get(stage_name)
        if drop and isinstance(stage, Mapping):
            stage = {key: value for key, value in stage.items() if key not in drop}
        stable[stage_name] = stage
    return stable


def _read_completion_review_for_history(mapping: Mapping[str, Any], *, evidence_root: Path) -> dict[str, Any]:
    # 僅保留 opaque-safe provenance；stage verdict 已由 inspect_delivery 正式驗證。
    projected = {key: mapping.get(key) for key in ("mapping_id", "requirement_id", "requirement_revision", "acceptance_id", "repo", "work_id", "run_id", "workflow_step_ids", "pr_number", "change", "todo_paths", "candidate_sha", "merge_sha", "profile_key", "config_revision", "policy_version", "completion_record", "source_generation", "source_revision_sha256", "authority_sha256", "status", "evidence", "target", "observed_at")}
    evidence = projected.get("evidence")
    if isinstance(evidence, Mapping):
        projected["evidence"] = _stable_evidence(evidence)
    return projected


def _evidence_weakened(previous_evidence: object, incoming_evidence: object) -> bool:
    """對抗審查第四輪 MAJOR：同一 generation 重讀時，任何原本 ``verified`` 的
    階段（例如 merge／installed）在新讀取中不再是 ``verified``，即視為弱化——
    這通常是暫時性讀取失敗（網路抖動、API 短暫不可用），不是真的失效，不可
    用來取代既有已驗證結果。"""
    if not isinstance(previous_evidence, Mapping):
        return False
    if not isinstance(incoming_evidence, Mapping):
        return any(
            isinstance(stage, Mapping) and stage.get("status") == "verified"
            for stage in previous_evidence.values()
        )
    for stage_name, previous_stage in previous_evidence.items():
        if not (isinstance(previous_stage, Mapping) and previous_stage.get("status") == "verified"):
            continue
        incoming_stage = incoming_evidence.get(stage_name)
        if not (isinstance(incoming_stage, Mapping) and incoming_stage.get("status") == "verified"):
            return True
    return False


def reconcile_delivery(
    manifest: object,
    source_snapshot: object,
    *,
    index_path: str | Path,
    expected_index_revision: str | None = None,
    source_root: str | Path,
    evidence_root: str | Path,
    coordinator_root: str | Path,
    authority_loader: Callable[[str, str], object],
    github_client: object,
    checkout_resolver: Callable[[str], str | Path],
    current_artifact: Mapping[str, object] | None,
    now_epoch: int | float,
    runtime_state_resolver: Callable[[str, str, object], str | Path] | None = None,
    runtime_status_resolver: Callable[[str, str], Mapping[str, Any]] | None = None,
    live_receipt_validator: Callable[[Mapping[str, Any]], bool] | None = None,
    live_receipt_domain_deriver: Callable[[Mapping[str, Any]], str | None] | None = None,
    waiver_validator: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """重新驗證可信來源後以單檔 CAS 更新衍生索引。"""
    path = Path(index_path)
    base = read_index(path)
    base_revision = base.get("revision")
    if expected_index_revision is not None and base_revision != expected_index_revision:
        raise IndexConflict("expected index revision is stale")
    _checkpoint("before-validation")
    authority_digests: dict[tuple[str, str], str] = {}
    def observed_authority_loader(repo: str, work_id: str) -> object:
        authority = authority_loader(repo, work_id)
        if not isinstance(authority, claim.WorkAuthority):
            raise ValueError("confirmed WorkAuthority is unavailable")
        authority_digests[(repo, work_id)] = claim.work_authority_digest(authority)
        return authority

    context = {
        "source_root": source_root,
        "evidence_root": evidence_root,
        "coordinator_root": coordinator_root,
        "authority_loader": observed_authority_loader,
        "github_client": github_client,
        "checkout_resolver": checkout_resolver,
        "current_artifact": current_artifact,
        "now_epoch": now_epoch,
        "runtime_state_resolver": runtime_state_resolver,
        "runtime_status_resolver": runtime_status_resolver,
        "live_receipt_validator": live_receipt_validator,
        "live_receipt_domain_deriver": live_receipt_domain_deriver,
        "waiver_validator": waiver_validator,
    }
    report = inspect_delivery(manifest, source_snapshot, **context)
    manifest_doc = validate_manifest(manifest, authority_root=source_root)
    snapshot = _validate_snapshot(source_snapshot)
    _checkpoint("after-validation")
    for (repo, work_id), before_digest in authority_digests.items():
        try:
            current_authority = authority_loader(repo, work_id)
            if not isinstance(current_authority, claim.WorkAuthority) or claim.work_authority_digest(current_authority) != before_digest:
                report["closure_readiness"] = "pending"
                report["authority_drift"] = True
                return {"report": report, "index": base, "changed": False, "index_revision": base_revision, "pending_reason": "work-authority-changed-during-reconcile"}
        except Exception:
            report["closure_readiness"] = "pending"
            report["authority_drift"] = True
            return {"report": report, "index": base, "changed": False, "index_revision": base_revision, "pending_reason": "work-authority-unavailable-before-commit"}
    new_by_id = {row["mapping_id"]: _read_completion_review_for_history(row, evidence_root=Path(evidence_root)) for row in report["mappings"]}
    old_rows = {row.get("mapping_id"): copy.deepcopy(row) for row in base["mappings"] if isinstance(row, dict) and isinstance(row.get("mapping_id"), str)}
    # 依「同一 requirement／criterion／repo／work／run」的 logical 範圍索引既有
    # rows（不論目前狀態），供下面的 generation 守門查詢用；mapping_id 會因
    # candidate/completion_record 等內容變動而改變，不能只用 mapping_id 相同來
    # 判斷是否為同一份需求交付。
    old_rows_by_logical: dict[tuple[Any, ...], list[str]] = {}
    for old_id, old_row in old_rows.items():
        old_logical = tuple(old_row.get(key) for key in ("requirement_id", "acceptance_id", "repo", "work_id", "run_id"))
        old_rows_by_logical.setdefault(old_logical, []).append(old_id)
    for new_row in new_by_id.values():
        logical = tuple(new_row.get(key) for key in ("requirement_id", "acceptance_id", "repo", "work_id", "run_id"))
        new_generation = new_row.get("source_generation")
        for old_id in old_rows_by_logical.get(logical, ()):
            if old_id == new_row["mapping_id"]:
                continue
            old_row = old_rows[old_id]
            old_generation = old_row.get("source_generation")
            # 對抗審查第四輪 MAJOR（跨 mapping generation 守門）：晚到的舊
            # generation（例如從未入索引過的 gen1）若先被當成 new_row 走到這
            # 裡，不論該 logical 範圍內既有 row 目前是 covered／blocked／stale
            # 哪個狀態，都是已見過的較新事實之前的舊輸入，不得插入或讓 index
            # 的 closure_readiness 翻回 ready——generation 比較必須跨 mapping_id
            # 且不論既有 row 狀態，以此 logical 範圍內已見過的最高 generation
            # 為準；只看 covered rows 會讓已 blocked／stale 的較新 generation
            # 被遲到的較舊 generation 繞過。
            if (
                type(old_generation) is int
                and type(new_generation) is int
                and new_generation < old_generation
            ):
                report["closure_readiness"] = "pending"
                report["source_generation_drift"] = True
                return {"report": report, "index": base, "changed": False, "index_revision": base_revision, "pending_reason": "late-source-generation-ignored"}
            if old_row.get("status") != "covered":
                continue
            # 對抗審查第四輪 MAJOR（同 generation 弱化重讀）：mapping_id 不同但
            # logical 範圍、generation 都相同，代表這是同一份交付的重新讀取，
            # 不是新的一代——若重讀結果比既有 covered 弱（任何原本 verified 的
            # 階段變成非 verified，例如 merge／installed 證據因暫時性讀取失敗
            # 而遺失），必須保留既有 covered、記 pending／drift，不得讓暫時性
            # 弱讀取靜默取代已驗證結果。
            if (
                type(old_generation) is int
                and type(new_generation) is int
                and new_generation == old_generation
                and _evidence_weakened(old_row.get("evidence"), new_row.get("evidence"))
            ):
                report["closure_readiness"] = "pending"
                report["source_generation_drift"] = True
                return {"report": report, "index": base, "changed": False, "index_revision": base_revision, "pending_reason": "same-generation-weaker-evidence-ignored"}
            old_row["status"] = "stale"
            old_row["stale_reason"] = "mapping-context-updated"
            old_row["superseded_by"] = new_row["mapping_id"]
    for mapping_id, incoming in new_by_id.items():
        previous = old_rows.get(mapping_id)
        if previous is not None:
            old_generation = previous.get("source_generation")
            new_generation = incoming.get("source_generation")
            if type(old_generation) is int and type(new_generation) is int and new_generation < old_generation:
                report["closure_readiness"] = "pending"
                report["source_generation_drift"] = True
                return {"report": report, "index": base, "changed": False, "index_revision": base_revision, "pending_reason": "late-source-generation-ignored"}
            if type(old_generation) is int and type(new_generation) is int and new_generation == old_generation:
                previous_comparable = {key: value for key, value in previous.items() if key != "observed_at"}
                incoming_comparable = {key: value for key, value in incoming.items() if key != "observed_at"}
                if previous_comparable != incoming_comparable:
                    # 對抗審查第六輪 MAJOR：這條「同代重讀內容不得漂移」的保護，
                    # 原意是擋暫時性弱讀取（見 `_evidence_weakened`）靜默取代已
                    # 驗證結果；但若無差別地把「任何內容差異」都當漂移拒絕，會
                    # 連「先缺 live receipt 記為 blocked、之後補上合法 live
                    # receipt 重跑」這種證據變強也一起擋掉，讓 gap 永遠清不掉。
                    # 只有真的變弱（原本 verified 的階段退回非 verified）才 pending；
                    # 其餘階段不弱化的變強／持平內容差異必須接受更新。
                    if _evidence_weakened(previous.get("evidence"), incoming.get("evidence")):
                        report["closure_readiness"] = "pending"
                        report["source_generation_drift"] = True
                        return {"report": report, "index": base, "changed": False, "index_revision": base_revision, "pending_reason": "same-source-generation-content-drift"}
                else:
                    incoming = copy.deepcopy(previous)
        old_rows[mapping_id] = incoming
    current_rows = list(old_rows.values())
    current_revisions = {row["id"]: row["revision"] for row in manifest_doc["requirements"]}
    for old_row in current_rows:
        requirement_id = old_row.get("requirement_id")
        current_revision = current_revisions.get(requirement_id)
        if old_row.get("status") == "covered" and current_revision != old_row.get("requirement_revision"):
            old_row["status"] = "stale"
            old_row["stale_reason"] = (
                "requirement-removed-from-manifest"
                if current_revision is None
                else "requirement-revision-updated"
            )
    receipt = {
        "validator": VALIDATOR_VERSION,
        "manifest_sha256": report["manifest_sha256"],
        "snapshot_sha256": report["snapshot_sha256"],
        "snapshot_revision": snapshot["snapshot_revision"],
        "validated_at": snapshot["captured_at"],
        "source_mapping_count": len(snapshot["mappings"]),
        "coverage": report["closure_readiness"],
        "gap_count": len(report["gaps"]),
    }
    candidate = {
        "schema": INDEX_DOCUMENT_SCHEMA,
        "schema_version": INDEX_SCHEMA,
        "generation": base["generation"],
        "manifest_id": manifest_doc["manifest_id"],
        "manifest_sha256": report["manifest_sha256"],
        "snapshot_sha256": report["snapshot_sha256"],
        "mappings": sorted(current_rows, key=lambda row: str(row.get("mapping_id", ""))),
        "gaps": copy.deepcopy(report["gaps"]),
        "reconcile_receipt": receipt,
        "extensions": copy.deepcopy(base.get("extensions", {})),
    }
    current_semantic = {key: base.get(key) for key in candidate if key != "generation"}
    candidate_semantic = {key: candidate[key] for key in candidate if key != "generation"}
    changed = current_semantic != candidate_semantic
    if changed:
        candidate["generation"] = base["generation"] + 1
    raw = canonical_json_bytes(candidate) + b"\n"
    if changed:
        _checkpoint("before-index-write")
        _atomic_replace(path, raw, expected_revision=base_revision)
        _checkpoint("after-receipt-write")
    index = read_index(path)
    _checkpoint("before-return")
    return {"report": report, "index": index, "changed": changed, "index_revision": index.get("revision")}
