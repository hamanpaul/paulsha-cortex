from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from paulsha_cortex.config import paths

from . import review as foreign_review
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


def _normalize_work_authority(value: object) -> dict[str, Any]:
    required = {
        "repo",
        "work_id",
        "snapshot_hash",
        "provider_id",
        "provider_revision",
        "source_revisions",
        "mapped_issues",
        "mapped_prs",
        "mapped_openspec",
        "mapped_todo_paths",
        "pr_number",
        "change",
        "todo_paths",
        "merge_commit",
        "run_id",
        "workflow_step_ids",
        "trusted_evidence_refs",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("completion work_authority malformed")
    repo = _require_non_empty_string(value.get("repo"), field="work_authority.repo")
    if repo.count("/") != 1:
        raise ValueError("completion work_authority repo invalid")
    sources = value.get("source_revisions")
    issues = value.get("mapped_issues")
    mapped_prs = value.get("mapped_prs")
    mapped_openspec = value.get("mapped_openspec")
    mapped_todo_paths = value.get("mapped_todo_paths")
    todo_paths = value.get("todo_paths")
    pr_number = value.get("pr_number")
    step_ids = value.get("workflow_step_ids")
    evidence_refs = value.get("trusted_evidence_refs")
    change = value.get("change")
    if (
        not isinstance(sources, list)
        or not sources
        or any(not isinstance(item, str) or not item for item in sources)
        or not isinstance(issues, list)
        or not issues
        or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in issues)
        or not isinstance(mapped_prs, list)
        or not mapped_prs
        or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in mapped_prs)
        or not isinstance(mapped_openspec, list)
        or not mapped_openspec
        or any(not isinstance(item, str) or not item for item in mapped_openspec)
        or not isinstance(mapped_todo_paths, list)
        or not mapped_todo_paths
        or any(not isinstance(item, str) or not item for item in mapped_todo_paths)
        or not isinstance(todo_paths, list)
        or not todo_paths
        or any(not isinstance(item, str) or not item for item in todo_paths)
        or not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or pr_number <= 0
        or not isinstance(step_ids, list)
        or not step_ids
        or any(not isinstance(item, str) or not item for item in step_ids)
        or len(set(step_ids)) != len(step_ids)
        or not isinstance(evidence_refs, list)
        or len(mapped_prs) != 1
        or len(mapped_openspec) != 1
        or len(mapped_todo_paths) != 1
        or pr_number != mapped_prs[0]
        or not isinstance(change, str)
        or not change
        or change != mapped_openspec[0]
        or sorted(todo_paths) != sorted(mapped_todo_paths)
    ):
        raise ValueError("completion work_authority refs invalid")
    normalized_evidence: list[dict[str, str]] = []
    for item in evidence_refs:
        if not isinstance(item, dict) or set(item) != {"kind", "ref", "hash"}:
            raise ValueError("completion trusted evidence ref malformed")
        kind = item.get("kind")
        if kind not in {
            "preflight",
            "foreign_review",
            "copilot",
            "maintainer-review",
            "merge_authorization",
        }:
            raise ValueError("completion trusted evidence kind invalid")
        normalized_evidence.append(
            {
                "kind": kind,
                "ref": _require_non_empty_string(
                    item.get("ref"), field="work_authority.trusted_evidence.ref"
                ),
                "hash": _normalize_digest_hash(
                    item.get("hash"), field="work_authority.trusted_evidence.hash"
                ),
            }
        )
    evidence_kinds = {item["kind"] for item in normalized_evidence}
    if (
        not {"preflight", "foreign_review", "merge_authorization"}.issubset(evidence_kinds)
        or len(evidence_kinds & {"copilot", "maintainer-review"}) != 1
        or len(normalized_evidence) != 4
    ):
        raise ValueError("completion trusted evidence refs incomplete")
    return {
        "repo": repo,
        "work_id": _require_non_empty_string(value.get("work_id"), field="work_authority.work_id"),
        "snapshot_hash": _normalize_digest_hash(
            value.get("snapshot_hash"), field="work_authority.snapshot_hash"
        ),
        "provider_id": _require_non_empty_string(
            value.get("provider_id"), field="work_authority.provider_id"
        ),
        "provider_revision": _require_non_empty_string(
            value.get("provider_revision"), field="work_authority.provider_revision"
        ),
        "source_revisions": sorted(sources),
        "mapped_issues": sorted(issues),
        "mapped_prs": sorted(mapped_prs),
        "mapped_openspec": sorted(mapped_openspec),
        "mapped_todo_paths": sorted(mapped_todo_paths),
        "pr_number": pr_number,
        "change": change,
        "todo_paths": sorted(todo_paths),
        "merge_commit": _normalize_git_sha(
            value.get("merge_commit"), field="work_authority.merge_commit"
        ),
        "run_id": _require_non_empty_string(value.get("run_id"), field="work_authority.run_id"),
        "workflow_step_ids": sorted(step_ids),
        "trusted_evidence_refs": sorted(normalized_evidence, key=lambda item: item["kind"]),
    }


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
    extras = set(payload) - required - {"work_authority"}
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
    expected_review_policy = "required" if docs_class in {"normative", "code"} else "not-required"
    if review_policy != expected_review_policy:
        raise ValueError("completion review_policy does not match docs_class")

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
    if "work_authority" in payload:
        normalized["work_authority"] = _normalize_work_authority(payload["work_authority"])

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
    expected_target_ref = f"refs/remotes/{normalized['target_remote']}/{normalized['target_branch']}"
    if normalized["target_ref"] != expected_target_ref:
        raise ValueError("completion target_ref does not match target remote/branch")
    return normalized


def _load_json_file(path: str | Path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _validate_reference(
    *,
    path: object,
    expected_hash: object,
    field: str,
    validator,
) -> dict[str, Any]:
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
    return normalized


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
    verification_evidence = _validate_reference(
        path=normalized["verification_evidence_path"],
        expected_hash=normalized["verification_evidence_hash"],
        field="verification_evidence_path",
        validator=verification.validate_verification_evidence,
    )
    if verification_evidence["slice_id"] != normalized["slice_id"]:
        raise ValueError("verification evidence slice_id mismatch")
    if verification_evidence["candidate"] != normalized["candidate"]:
        raise ValueError("verification evidence candidate mismatch")
    expected_verification_status = "reviewing" if normalized["review_policy"] == "required" else "verified"
    if verification_evidence["status"] != expected_verification_status:
        raise ValueError("verification evidence status does not match review_policy")
    if normalized["review_policy"] == "required":
        review_evaluation = _validate_reference(
            path=normalized["review_evaluation_path"],
            expected_hash=normalized["review_evaluation_hash"],
            field="review_evaluation_path",
            validator=foreign_review.validate_gate_evaluation,
        )
        if review_evaluation["slice_id"] != normalized["slice_id"]:
            raise ValueError("review evaluation slice_id mismatch")
        if review_evaluation["candidate"] != normalized["candidate"]:
            raise ValueError("review evaluation candidate mismatch")
        if review_evaluation["builder_job_id"] != normalized["builder_job_id"]:
            raise ValueError("review evaluation builder_job_id mismatch")
        if review_evaluation["reviewer_job_id"] != normalized["reviewer_job_id"]:
            raise ValueError("review evaluation reviewer_job_id mismatch")
        if review_evaluation["state"] != "passed":
            raise ValueError("review evaluation state must be passed")
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
