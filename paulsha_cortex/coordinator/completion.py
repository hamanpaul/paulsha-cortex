from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from paulsha_cortex.config import paths

from . import verification

COMPLETION_SCHEMA_VERSION = 1
VALID_REVIEW_POLICIES = frozenset({"required", "not-required"})


def classify_completion(*, exit_code: int, last_jsonl_line: str | None) -> str:
    """exit code + 末筆 JSONL → 'exited'/'failed'。JSONL 不可解則 fallback exit code。"""
    if last_jsonl_line:
        try:
            obj = json.loads(last_jsonl_line)
            if isinstance(obj, dict) and obj.get("ok") is False:
                return "failed"
        except (json.JSONDecodeError, TypeError):
            pass  # fallback 到 exit code
    return "exited" if exit_code == 0 else "failed"


def completion_record_path(
    *,
    slice_id: str,
    candidate: str,
    coordinator_root: str | Path | None = None,
) -> Path:
    if verification.SAFE_SLICE_ID_RE.fullmatch(slice_id) is None:
        raise ValueError(f"unsafe slice_id for completion record path: {slice_id!r}")
    if verification.SAFE_SHA_RE.fullmatch(candidate) is None:
        raise ValueError(f"unsafe candidate for completion record path: {candidate!r}")
    root = Path(coordinator_root) if coordinator_root is not None else paths.coordinator_root()
    return root.resolve() / "evidence" / "completion" / f"{slice_id}-{candidate.lower()}.json"


def _require_non_empty_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _normalize_git_sha(value: object, *, field: str) -> str:
    sha = _require_non_empty_string(value, field=field)
    if verification.SAFE_SHA_RE.fullmatch(sha) is None:
        raise ValueError(f"{field} must be a 40-char hex SHA")
    return sha.lower()


def _normalize_digest_hash(value: object, *, field: str) -> str:
    digest = _require_non_empty_string(value, field=field)
    if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in digest):
        raise ValueError(f"{field} must be a 64-char hex digest")
    return digest.lower()


def validate_completion_record(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("completion record must be an object")
    required = {
        "schema_version",
        "slice_id",
        "spec_hash",
        "plan_hash",
        "verification_hash",
        "builder_job_id",
        "reviewer_job_id",
        "dispatch_base",
        "candidate",
        "target_branch",
        "target_remote",
        "target_ref",
        "target_ref_sha",
        "verification_evidence_path",
        "verification_evidence_hash",
        "review_policy",
        "docs_class",
        "review_evaluation_path",
        "review_evaluation_hash",
        "completed_at",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"completion record missing keys: {', '.join(missing)}")
    extras = set(payload) - required
    if extras:
        raise ValueError(f"completion record unexpected key: {sorted(extras)[0]}")
    if payload.get("schema_version") != COMPLETION_SCHEMA_VERSION:
        raise ValueError(f"completion record schema_version must be {COMPLETION_SCHEMA_VERSION}")

    review_policy = payload.get("review_policy")
    if review_policy not in VALID_REVIEW_POLICIES:
        raise ValueError(f"invalid completion review_policy: {review_policy!r}")
    docs_class = payload.get("docs_class")
    if docs_class not in verification.VALID_DOCS_CLASSES:
        raise ValueError(f"invalid completion docs_class: {docs_class!r}")

    normalized = {
        "schema_version": COMPLETION_SCHEMA_VERSION,
        "slice_id": _require_non_empty_string(payload.get("slice_id"), field="slice_id"),
        "spec_hash": _normalize_digest_hash(payload.get("spec_hash"), field="spec_hash"),
        "plan_hash": _normalize_digest_hash(payload.get("plan_hash"), field="plan_hash"),
        "verification_hash": _normalize_digest_hash(payload.get("verification_hash"), field="verification_hash"),
        "builder_job_id": _require_non_empty_string(payload.get("builder_job_id"), field="builder_job_id"),
        "reviewer_job_id": payload.get("reviewer_job_id"),
        "dispatch_base": _normalize_git_sha(payload.get("dispatch_base"), field="dispatch_base"),
        "candidate": _normalize_git_sha(payload.get("candidate"), field="candidate"),
        "target_branch": _require_non_empty_string(payload.get("target_branch"), field="target_branch"),
        "target_remote": _require_non_empty_string(payload.get("target_remote"), field="target_remote"),
        "target_ref": _require_non_empty_string(payload.get("target_ref"), field="target_ref"),
        "target_ref_sha": _normalize_git_sha(payload.get("target_ref_sha"), field="target_ref_sha"),
        "verification_evidence_path": _require_non_empty_string(
            payload.get("verification_evidence_path"),
            field="verification_evidence_path",
        ),
        "verification_evidence_hash": _normalize_digest_hash(
            payload.get("verification_evidence_hash"),
            field="verification_evidence_hash",
        ),
        "review_policy": review_policy,
        "docs_class": docs_class,
        "review_evaluation_path": payload.get("review_evaluation_path"),
        "review_evaluation_hash": payload.get("review_evaluation_hash"),
        "completed_at": _require_non_empty_string(payload.get("completed_at"), field="completed_at"),
    }

    reviewer_job_id = normalized["reviewer_job_id"]
    review_path = normalized["review_evaluation_path"]
    review_hash = normalized["review_evaluation_hash"]
    if review_policy == "required":
        normalized["reviewer_job_id"] = _require_non_empty_string(
            reviewer_job_id,
            field="reviewer_job_id",
        )
        normalized["review_evaluation_path"] = _require_non_empty_string(
            review_path,
            field="review_evaluation_path",
        )
        normalized["review_evaluation_hash"] = _normalize_digest_hash(
            review_hash,
            field="review_evaluation_hash",
        )
    else:
        if reviewer_job_id is not None or review_path is not None or review_hash is not None:
            raise ValueError("review_policy=not-required must keep reviewer/evaluation refs null")
        normalized["reviewer_job_id"] = None
        normalized["review_evaluation_path"] = None
        normalized["review_evaluation_hash"] = None
    return normalized


def _load_json_file(path: str | Path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _validate_reference(
    *,
    path: object,
    expected_hash: object,
    field: str,
    validator,
) -> None:
    path_str = _require_non_empty_string(path, field=field)
    if Path(path_str).is_symlink():
        raise ValueError(f"{field} must not be a symlink path")
    expected = _normalize_digest_hash(expected_hash, field=f"{field}_hash")
    try:
        payload = _load_json_file(path_str)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} unreadable: {path_str}") from exc
    if validator is None:
        if not isinstance(payload, dict):
            raise ValueError(f"{field} must point to a JSON object")
        normalized = payload
    else:
        normalized = validator(payload)
    if verification.canonical_json_hash(normalized) != expected:
        raise ValueError(f"{field} hash mismatch: {path_str}")


def read_completion_record(
    path: str | Path,
    *,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    record_path = Path(path)
    if record_path.is_symlink():
        raise ValueError(f"completion record path must not be symlink: {record_path}")
    try:
        payload = _load_json_file(record_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"completion record unreadable: {record_path}") from exc
    normalized = validate_completion_record(payload)
    if expected_hash is not None:
        expected = _normalize_digest_hash(expected_hash, field="completion_record_hash")
        if verification.canonical_json_hash(normalized) != expected:
            raise ValueError(f"completion record hash mismatch: {record_path}")
    _validate_reference(
        path=normalized["verification_evidence_path"],
        expected_hash=normalized["verification_evidence_hash"],
        field="verification_evidence_path",
        validator=verification.validate_verification_evidence,
    )
    if normalized["review_policy"] == "required":
        _validate_reference(
            path=normalized["review_evaluation_path"],
            expected_hash=normalized["review_evaluation_hash"],
            field="review_evaluation_path",
            validator=None,
        )
    return normalized


def _existing_record_or_raise(path: Path, content_hash: str) -> dict[str, Any]:
    reason = "existing completion record unreadable"
    try:
        existing = read_completion_record(path)
    except ValueError:
        existing = None
    if existing is not None:
        if verification.canonical_json_hash(existing) == content_hash:
            return {"path": str(path), "hash": content_hash, "payload": copy.deepcopy(existing)}
        reason = "content mismatch"
    quarantine_dir = path.parent / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    quarantine_path = quarantine_dir / f"{path.stem}-{uuid4().hex}.json"
    os.replace(path, quarantine_path)
    raise RuntimeError(f"conflicting completion record: {path} ({reason})")


def write_completion_record(
    payload: object,
    *,
    coordinator_root: str | Path | None = None,
) -> dict[str, Any]:
    normalized = validate_completion_record(payload)
    path = completion_record_path(
        slice_id=normalized["slice_id"],
        candidate=normalized["candidate"],
        coordinator_root=coordinator_root,
    )
    content_hash = verification.canonical_json_hash(normalized)
    if path.exists():
        return _existing_record_or_raise(path, content_hash)
    try:
        verification.atomic_write_json(path, normalized)
    except verification.AtomicWriteConflictError:
        return _existing_record_or_raise(path, content_hash)
    return {"path": str(path), "hash": content_hash, "payload": copy.deepcopy(normalized)}


def load_completion_from_handoff(
    slice_id: str,
    *,
    handoff_dir: str,
    repo_root: str | Path | None = None,
    git_runner=None,
) -> dict[str, Any] | None:
    manifest_path = Path(handoff_dir) / f"{slice_id}.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    if manifest.get("gate_status") not in {"passed", "verified"}:
        return None
    if manifest.get("slice_state") != "completed":
        return None
    path = manifest.get("completion_record_path")
    expected_hash = manifest.get("completion_record_hash")
    if not isinstance(path, str) or not path or not isinstance(expected_hash, str) or not expected_hash:
        return None
    try:
        record = read_completion_record(path, expected_hash=expected_hash)
    except ValueError:
        return None
    if record["slice_id"] != slice_id:
        return None
    if manifest.get("spec_hash") != record["spec_hash"]:
        return None
    if manifest.get("plan_hash") != record["plan_hash"]:
        return None
    if manifest.get("verification_hash") != record["verification_hash"]:
        return None
    resolved_repo = verification.normalize_repo_root(repo_root)
    current_ref = verification._run_git(
        ["-C", str(resolved_repo), "rev-parse", record["target_ref"]],
        git_runner,
    )
    current_sha = current_ref["stdout"].strip().lower()
    if current_ref["status"] != "ok" or verification.SAFE_SHA_RE.fullmatch(current_sha) is None:
        return None
    ancestry = verification._run_git(
        ["-C", str(resolved_repo), "merge-base", "--is-ancestor", record["candidate"], current_sha],
        git_runner,
    )
    if ancestry["status"] != "ok":
        return None
    return record
