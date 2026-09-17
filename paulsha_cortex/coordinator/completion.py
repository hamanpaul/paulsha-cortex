from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from paulsha_cortex.config import paths
from paulsha_cortex.deck.schema import BAND_LEVELS

from . import claim
from . import review as foreign_review
from . import verification

COMPLETION_SCHEMA_VERSION = 1
VALID_REVIEW_POLICIES = frozenset({"required", "not-required"})
# source_revisions（openspec/todo/spec 綁 default-branch tree SHA）隨 main 前進合法漂移，
# 對 completion 語意（run/candidate/verdict/mapped refs/merge_commit）無影響，故納入揮發集，
# 讓長期 in-flight run 的 ship work_authority 比對不因此漂移誤判 mismatch。
VOLATILE_WORK_AUTHORITY_FIELDS = frozenset(
    {"snapshot_hash", "provider_revision", "source_revisions"}
)
# reused_from（#214 StageExecutionKey reuse）只是 provenance：記錄本次 stage
# 是 reuse 既有 run/job/evidence 而非重新呼叫 model，不影響 completion 語意，
# 故整個欄位排除在 semantic match 之外——避免良性 reuse 被誤判為衝突而觸發
# quarantine（比照 VOLATILE_WORK_AUTHORITY_FIELDS 的做法）。
REUSED_FROM_FIELDS = frozenset({"run_id", "job_id", "evidence_hash"})
# retry_classification（#215 retry 分類骨架）同樣只是 provenance：記錄一次
# retry-build 的性質判斷（model_repair／orchestrator_retry／…），不影響 completion
# 語意，故排除在 semantic match 之外——避免同一份 candidate 因重試分類不同而被
# 誤判衝突（比照 REUSED_FROM_FIELDS 的做法）。enum 值須與
# work_actions.RetryClassification 同步（enum 定案，後波不得改名）；本模組刻意
# 不 import 該型別以避免 schema 層耦合 coordinator 高階模組。
RETRY_CLASSIFICATION_VALUES = frozenset(
    {
        "model_repair",
        "orchestrator_retry",
        "authority_restart",
        "review_handoff_failure",
        "source_owner_repair",
    }
)
# #212：final 發現「plan 錯而非 candidate 錯」的訊號欄位，供 #137 度量 plan review
# 漏檢（plan_review_gate 判定通過，但 final 才發現問題其實出在 plan）。純
# provenance，不影響既有 completion 語意，semantic match 排除比照 reused_from。
VALID_FINAL_DEFECT_LOCI = frozenset({"plan", "candidate"})
# #222（design #208 H.2）：五維 sizing 總分／band／宣告-實際模組數落差記錄。
# sizing_score/sizing_band 是 work item 狀態（WorkflowRun，見 workflow.py）在
# completion 當下的快照，band 門檻判定委派給 claim.sizing_band()（claim.py／
# registry.py／completion.py 三處共用同一份純函式，避免各自硬編碼漂移）；
# sizing_declaration_drift 記錄宣告模組數 vs candidate 實際變更數（供 #210
# 後驗）。比照 #215 的 reused_from/retry_classification/final_defect_locus：
# 可選欄位＋_normalize_*＋extras 白名單聯集；三者皆屬 work item 狀態快照／
# provenance，不影響既有 completion 語意，semantic match 排除。
SIZING_DECLARATION_DRIFT_FIELDS = frozenset({"declared_modules", "actual_modules"})


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
        or len(mapped_openspec) > 1
        or len(mapped_todo_paths) != 1
        or pr_number != mapped_prs[0]
        or sorted(todo_paths) != sorted(mapped_todo_paths)
    ):
        raise ValueError("completion work_authority refs invalid")
    if len(mapped_openspec) == 0:
        if change is not None:
            raise ValueError("completion work_authority refs invalid")
    elif not isinstance(change, str) or not change or change != mapped_openspec[0]:
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
        "change": change if change is None else str(change),
        "todo_paths": sorted(todo_paths),
        "merge_commit": _normalize_git_sha(
            value.get("merge_commit"), field="work_authority.merge_commit"
        ),
        "run_id": _require_non_empty_string(value.get("run_id"), field="work_authority.run_id"),
        "workflow_step_ids": sorted(step_ids),
        "trusted_evidence_refs": sorted(normalized_evidence, key=lambda item: item["kind"]),
    }


def _normalize_reused_from(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != REUSED_FROM_FIELDS:
        raise ValueError("completion reused_from malformed")
    return {
        "run_id": _require_non_empty_string(value.get("run_id"), field="reused_from.run_id"),
        "job_id": _require_non_empty_string(value.get("job_id"), field="reused_from.job_id"),
        "evidence_hash": _normalize_digest_hash(
            value.get("evidence_hash"), field="reused_from.evidence_hash"
        ),
    }


def _normalize_retry_classification(value: object) -> str:
    if not isinstance(value, str) or value not in RETRY_CLASSIFICATION_VALUES:
        raise ValueError("completion retry_classification invalid")
    return value


def _normalize_final_defect_locus(value: object) -> str:
    if value not in VALID_FINAL_DEFECT_LOCI:
        raise ValueError(f"completion final_defect_locus must be one of {sorted(VALID_FINAL_DEFECT_LOCI)}")
    return value


def _normalize_sizing_score(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 10):
        raise ValueError("completion sizing_score must be an int in [0, 10]")
    return value


def _normalize_sizing_band(value: object) -> str:
    if value not in BAND_LEVELS:
        raise ValueError(f"completion sizing_band must be one of {list(BAND_LEVELS)}")
    return value


def _normalize_sizing_declaration_drift(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != SIZING_DECLARATION_DRIFT_FIELDS:
        raise ValueError("completion sizing_declaration_drift malformed")
    declared = value.get("declared_modules")
    actual = value.get("actual_modules")
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < 0:
        raise ValueError("completion sizing_declaration_drift.declared_modules invalid")
    if not isinstance(actual, int) or isinstance(actual, bool) or actual < 0:
        raise ValueError("completion sizing_declaration_drift.actual_modules invalid")
    return {"declared_modules": declared, "actual_modules": actual}


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
    extras = set(payload) - required - {
        "work_authority", "reused_from", "retry_classification", "final_defect_locus",
        "sizing_score", "sizing_band", "sizing_declaration_drift",
    }
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
    if "reused_from" in payload:
        normalized["reused_from"] = _normalize_reused_from(payload["reused_from"])
    if "retry_classification" in payload:
        normalized["retry_classification"] = _normalize_retry_classification(
            payload["retry_classification"]
        )
    if "final_defect_locus" in payload:
        normalized["final_defect_locus"] = _normalize_final_defect_locus(payload["final_defect_locus"])
    if ("sizing_score" in payload) != ("sizing_band" in payload):
        raise ValueError("completion sizing_score/sizing_band must be supplied together")
    if "sizing_score" in payload:
        normalized["sizing_score"] = _normalize_sizing_score(payload["sizing_score"])
        normalized["sizing_band"] = _normalize_sizing_band(payload["sizing_band"])
        expected_band = claim.sizing_band(normalized["sizing_score"])
        if normalized["sizing_band"] != expected_band:
            raise ValueError(
                "completion sizing_band does not match sizing_score threshold "
                f"(#222 H.2): expected {expected_band!r}, got {normalized['sizing_band']!r}"
            )
    if "sizing_declaration_drift" in payload:
        normalized["sizing_declaration_drift"] = _normalize_sizing_declaration_drift(
            payload["sizing_declaration_drift"]
        )

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


def _semantic_work_authority(authority: object) -> dict[str, Any] | None:
    if authority is None:
        return None
    normalized = _normalize_work_authority(authority)
    return {
        key: copy.deepcopy(value)
        for key, value in normalized.items()
        if key not in VOLATILE_WORK_AUTHORITY_FIELDS
    }


def completion_records_semantically_match(existing: object, incoming: object) -> bool:
    try:
        existing_normalized = validate_completion_record(existing)
        incoming_normalized = validate_completion_record(incoming)
    except ValueError:
        return False
    existing_authority = _semantic_work_authority(existing_normalized.pop("work_authority", None))
    incoming_authority = _semantic_work_authority(incoming_normalized.pop("work_authority", None))
    existing_normalized.pop("reused_from", None)
    incoming_normalized.pop("reused_from", None)
    existing_normalized.pop("retry_classification", None)
    incoming_normalized.pop("retry_classification", None)
    existing_normalized.pop("final_defect_locus", None)
    incoming_normalized.pop("final_defect_locus", None)
    existing_normalized.pop("sizing_score", None)
    incoming_normalized.pop("sizing_score", None)
    existing_normalized.pop("sizing_band", None)
    incoming_normalized.pop("sizing_band", None)
    existing_normalized.pop("sizing_declaration_drift", None)
    incoming_normalized.pop("sizing_declaration_drift", None)
    return existing_normalized == incoming_normalized and existing_authority == incoming_authority


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


def _existing_record_or_raise(
    path: Path,
    content_hash: str,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    reason = "existing completion record unreadable"
    try:
        existing = read_completion_record(path)
    except ValueError:
        existing = None
    if existing is not None:
        existing_hash = verification.canonical_json_hash(existing)
        return {"path": str(path), "hash": existing_hash, "payload": copy.deepcopy(existing)}
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
        return _existing_record_or_raise(path, content_hash, normalized)
    try:
        verification.atomic_write_json(path, normalized)
    except verification.AtomicWriteConflictError:
        return _existing_record_or_raise(path, content_hash, normalized)
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
