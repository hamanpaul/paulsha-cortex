from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from paulsha_cortex.config import paths

from .._yaml import YAMLError, safe_load
from ..lib import idle
from ..persona import gate, handoff
from . import autonomy
from . import completion
from . import planning_runtime
from . import review as foreign_review
from . import verification
from .model_identities import (
    AGY_DOMAIN,
    AGY_LIVE_PROBE,
    AGY_MODEL_ID,
    CapabilityProbe,
    IdentityRegistry,
    ModelIdentity,
    load_model_identities,
)
from .planning import (
    PlanningArtifact,
    PlanningScope,
    assess_planning_artifact,
    assess_planning_completeness,
    run_heterogeneous_brainstorm,
)
from .workflow import (
    WORKFLOW_PHASES,
    GateEvidenceRef,
    PlanningArtifactAuthority,
    WorkflowManifest,
    validate_workflow_phase_transition,
)

IN_FLIGHT_STATUSES = frozenset({"dispatched", "running"})
TERMINAL_STATUSES = frozenset({"exited", "failed"})
VERIFICATION_RESULT_STATES = frozenset({"needs_human", "reviewing", "verified"})
SLICE_ACTIONS = frozenset({"retry-build", "retry-verify", "retry-review", "abandon"})
WORKFLOW_REPORT_MAX_BYTES = 128 * 1024


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_safe_slice_id(slice_id) -> bool:
    """slice_id 用作單一檔名；拒絕路徑分隔/相對跳脫/絕對路徑（fail-closed 防越界寫）。"""
    return (
        isinstance(slice_id, str)
        and bool(slice_id)
        and slice_id not in (".", "..")
        and re.fullmatch(r"[A-Za-z0-9._-]+", slice_id) is not None
    )


class GateRunner(Protocol):
    def __call__(self, job: dict) -> dict | None: ...


def _default_gate_runner(job: dict) -> dict | None:
    """shadow diff gate（觀測用）。取不到 base/head 或 git 失敗 → None（不阻釋放）。"""
    branch = job.get("branch")
    base = job.get("dispatch_head")
    if not (isinstance(branch, str) and branch and isinstance(base, str) and base):
        return None
    role = job.get("persona") if isinstance(job.get("persona"), str) else "builder"
    # branch 為 ref 名（非 commit sha）是刻意的：git 在 eval 當下把 base...branch
    # 解析成該 branch 的 HEAD。shadow-only，任何失敗皆降級為 None（不阻釋放）。
    try:
        changed = gate.compute_changed_paths(base, branch)
    except Exception:
        return None
    return gate.build_verdict(role=role, changed_paths=changed, manifest_ok=False)


def _satisfied_pred(handoff_dir: str):
    # 委派單一真相源 default_is_satisfied（消費端零改，不 fork readiness 邏輯）。
    # try/except 僅做 error-hardening（壞檔/壞編碼 UnicodeDecodeError〔ValueError 子類〕/OSError
    # → False，不 crash tick），非 readiness 邏輯分岔。
    def _pred(slice_id: str) -> bool:
        try:
            return autonomy.default_is_satisfied(slice_id, handoff_dir=handoff_dir)
        except (OSError, ValueError):
            return False

    return _pred


def _read_manifest_payload(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _existing_manifest_job_id(path: Path) -> str | None:
    """既存 manifest 的 job_id（缺檔/壞檔/缺欄 → None，觸發 overwrite）。"""
    payload = _read_manifest_payload(path)
    if payload is None:
        return None
    if payload.get("gate_status") in {"passed", "verified"}:
        return None
    if payload.get("gate_status") == "needs_human" and payload.get("verification_evidence_path") is None and (
        payload.get("gate_reason") in {"pinned-input-mismatch", "verification-runner-error", "verification-state-update-error"}
    ):
        return None
    job_id = payload.get("job_id")
    return job_id if isinstance(job_id, str) else None


def _slice_for_job(registry, slice_id: str, job_id: str) -> dict | None:
    if registry is None:
        return None
    try:
        slice_row = registry.get_slice(slice_id)
    except KeyError:
        return None
    if slice_row.get("builder_job_id") != job_id:
        return None
    return slice_row


def _slice_for_reviewer_job(registry, slice_id: str, job_id: str) -> dict | None:
    if registry is None:
        return None
    try:
        slice_row = registry.get_slice(slice_id)
    except KeyError:
        return None
    if slice_row.get("reviewer_job_id") != job_id:
        return None
    return slice_row


def _pinned_input_mismatches(slice_row: dict) -> list[str]:
    repo_root = autonomy._infer_repo_root(Path(slice_row["spec"]["path"]))
    mismatches: list[str] = []
    spec_path = Path(slice_row["spec"]["path"])
    plan_path = Path(slice_row["plan"]["path"])
    if not plan_path.is_absolute():
        plan_path = (repo_root / plan_path).resolve()
    try:
        current_spec_hash = verification.sha256_bytes(spec_path.read_bytes())
    except OSError:
        return ["spec-unreadable"]
    if current_spec_hash != slice_row["spec"]["hash"]:
        mismatches.append("spec-hash")
    try:
        current_plan_hash = verification.sha256_bytes(plan_path.read_bytes())
    except OSError:
        return mismatches + ["plan-unreadable"]
    if current_plan_hash != slice_row["plan"]["hash"]:
        mismatches.append("plan-hash")
    try:
        current_meta = autonomy.parse_spec_frontmatter(spec_path)
    except (OSError, UnicodeDecodeError):
        return mismatches + ["spec-frontmatter-unreadable"]
    if current_meta.get("parse_error") is not None:
        return mismatches + ["spec-frontmatter-invalid"]
    if current_meta.get("target_branch") != slice_row.get("target_branch"):
        mismatches.append("target-branch")
    current_verification = current_meta.get("verification")
    current_verification_hash = verification.canonical_json_hash(current_verification)
    if current_verification_hash != slice_row["verification"]["hash"]:
        mismatches.append("verification-hash")
    return mismatches


def _candidate_for_evidence(
    *,
    slice_row: dict | None,
    job: dict,
    repo_root: Path,
    git_runner,
) -> str:
    fallback = None
    if slice_row is not None:
        dispatch_base = slice_row.get("dispatch_base")
        if isinstance(dispatch_base, str) and verification.SAFE_SHA_RE.fullmatch(dispatch_base):
            fallback = dispatch_base.lower()
    branch = job.get("branch")
    if isinstance(branch, str) and branch:
        branch_head = verification._run_git(["-C", str(repo_root), "rev-parse", branch], git_runner)
        stdout = branch_head["stdout"].strip()
        if branch_head["status"] == "ok" and verification.SAFE_SHA_RE.fullmatch(stdout):
            return stdout.lower()
    worktree = job.get("worktree")
    if isinstance(worktree, str) and worktree:
        worktree_head = verification._run_git(["-C", worktree, "rev-parse", "HEAD"], git_runner)
        stdout = worktree_head["stdout"].strip()
        if worktree_head["status"] == "ok" and verification.SAFE_SHA_RE.fullmatch(stdout):
            return stdout.lower()
    return fallback or ("0" * 40)


def _write_status_evidence(
    *,
    slice_row: dict | None,
    job: dict,
    repo_root: Path,
    coordinator_root: Path | None,
    git_runner,
    status: str,
    summary: str,
    details: dict,
) -> dict | None:
    slice_id = job.get("task")
    if not isinstance(slice_id, str) or not slice_id:
        return None
    payload = {
        "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
        "slice_id": slice_id,
        "candidate": _candidate_for_evidence(
            slice_row=slice_row,
            job=job,
            repo_root=repo_root,
            git_runner=git_runner,
        ),
        "status": status,
        "summary": summary,
        "details": details,
    }
    return verification.write_verification_evidence(payload, coordinator_root=coordinator_root)


def _discard_unpublished_evidence(evidence: dict | None) -> None:
    if not isinstance(evidence, dict):
        return
    path = evidence.get("path")
    if not isinstance(path, str) or not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _validate_result_evidence(
    *,
    evidence: object,
    slice_id: str,
    coordinator_root: Path | None,
) -> dict:
    if not isinstance(evidence, dict):
        raise ValueError("verification runner must return an evidence object")
    normalized = verification.validate_verification_evidence(evidence.get("payload"))
    if normalized["slice_id"] != slice_id:
        raise ValueError("verification evidence slice_id mismatch")
    if normalized["status"] not in VERIFICATION_RESULT_STATES:
        raise ValueError(f"unsupported verification evidence status: {normalized['status']!r}")
    expected_path = verification.evidence_path(
        slice_id=slice_id,
        candidate=normalized["candidate"],
        coordinator_root=coordinator_root,
    )
    if evidence.get("path") != str(expected_path):
        raise ValueError("verification evidence path mismatch")
    expected_hash = verification.canonical_json_hash(normalized)
    if evidence.get("hash") != expected_hash:
        raise ValueError("verification evidence hash mismatch")
    stored_payload = _read_manifest_payload(expected_path)
    if stored_payload is None:
        raise ValueError("verification evidence file unreadable")
    stored_normalized = verification.validate_verification_evidence(stored_payload)
    if stored_normalized != normalized:
        raise ValueError("verification evidence payload mismatch")
    return {"path": str(expected_path), "hash": expected_hash, "payload": normalized}


def _apply_verification_result(registry, slice_id: str, evidence: dict) -> None:
    payload = evidence["payload"]
    refs = [evidence["path"]]
    state = payload["status"]
    gate_state = "pending" if state == "reviewing" else ("passed" if state == "verified" else "needs_human")
    action = {
        "reviewing": "verification-passed-await-review",
        "verified": "verification-passed",
    }.get(state, "verification-failed")
    registry.record_action(
        slice_id,
        action=action,
        actor="manager",
        state=state,
        gate_state=gate_state,
        evidence_refs=refs,
        candidate=payload["candidate"],
    )


def _identity_registry() -> dict[tuple[str, str], dict[str, str]]:
    return foreign_review.load_model_identity_registry()


def _builder_launch_identity(job: dict, identity_registry: dict[tuple[str, str], dict[str, str]] | None = None) -> dict | None:
    executor = job.get("executor")
    model_id = job.get("model_id")
    domain = job.get("independence_domain")
    if isinstance(executor, str) and isinstance(model_id, str) and isinstance(domain, str) and domain:
        return {"executor": executor, "model_id": model_id, "independence_domain": domain}
    if identity_registry is None:
        return None
    if not isinstance(executor, str) or not isinstance(model_id, str):
        return None
    return identity_registry.get((executor, model_id))


def _reviewer_launch_identity(job: dict) -> dict | None:
    executor = job.get("executor")
    model_id = job.get("model_id")
    domain = job.get("independence_domain")
    if not (isinstance(executor, str) and isinstance(model_id, str) and isinstance(domain, str) and domain):
        return None
    return {"executor": executor, "model_id": model_id, "independence_domain": domain}


def _current_verification_ref(slice_row: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(slice_row, dict):
        return None, None
    refs = slice_row.get("current_evidence_refs")
    if not isinstance(refs, list) or not refs:
        return None, None
    path = refs[0]
    if not isinstance(path, str):
        return None, None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        normalized = verification.validate_verification_evidence(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return path, None
    return path, verification.canonical_json_hash(normalized)


def _current_review_ref(slice_row: dict | None) -> tuple[str | None, str | None, dict | None]:
    if not isinstance(slice_row, dict):
        return None, None, None
    refs = slice_row.get("current_evaluation_refs")
    if not isinstance(refs, list) or not refs:
        return None, None, None
    path = refs[0]
    if not isinstance(path, str):
        return None, None, None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return path, None, None
    if not isinstance(payload, dict):
        return path, None, None
    return path, verification.canonical_json_hash(payload), payload


def _review_policy_for_slice(slice_row: dict) -> str:
    contract = slice_row.get("verification", {}).get("contract")
    if isinstance(contract, dict):
        policy = contract.get("review_policy")
        if policy in {"required", "not-required"}:
            return str(policy)
        docs_class = contract.get("docs_class")
        if docs_class in {"informational", "trivial"}:
            return "not-required"
    return "required"


def _current_verification_payload(slice_row: dict | None) -> dict | None:
    if not isinstance(slice_row, dict):
        return None
    refs = slice_row.get("current_evidence_refs")
    if not isinstance(refs, list) or not refs:
        return None
    path = refs[0]
    if not isinstance(path, str) or not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        normalized = verification.validate_verification_evidence(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return {
        "path": path,
        "hash": verification.canonical_json_hash(normalized),
        "payload": normalized,
    }


def allowed_slice_actions(registry, slice_row: dict | None) -> list[str]:
    if not isinstance(slice_row, dict):
        return []
    if slice_row.get("state") != "needs_human":
        return []
    actions = ["retry-build", "abandon"]
    candidate = slice_row.get("candidate")
    if not (
        isinstance(candidate, str)
        and verification.SAFE_SHA_RE.fullmatch(candidate) is not None
    ):
        return actions
    builder_job_id = slice_row.get("builder_job_id")
    if not isinstance(builder_job_id, str):
        return actions
    try:
        builder_job = registry.get_job(builder_job_id)
    except Exception:
        return actions
    if builder_job.get("status") != "exited":
        return actions
    evidence = _current_verification_payload(slice_row)
    if evidence is None:
        return actions
    if evidence["payload"].get("candidate", "").lower() != candidate.lower():
        return actions
    actions.append("retry-verify")
    if (
        _review_policy_for_slice(slice_row) == "required"
        and evidence["payload"].get("status") in {"reviewing", "verified"}
    ):
        actions.append("retry-review")
    return actions


def _resolve_ancestry_status(slice_row: dict, *, git_runner) -> dict[str, Any]:
    target_remote = str(slice_row.get("target_remote") or "origin")
    target_branch = str(slice_row.get("target_branch") or "main")
    target_ref = f"refs/remotes/{target_remote}/{target_branch}"
    summary: dict[str, Any] = {
        "target_ref": target_ref,
        "target_head": None,
        "status": "unknown",
    }
    candidate = slice_row.get("candidate")
    if not (
        isinstance(candidate, str)
        and verification.SAFE_SHA_RE.fullmatch(candidate) is not None
    ):
        summary["status"] = "candidate-missing"
        return summary
    spec_path = slice_row.get("spec", {}).get("path")
    if not isinstance(spec_path, str) or not spec_path:
        summary["status"] = "repo-unresolved"
        return summary
    runner = git_runner or verification._default_git_runner
    repo_root = autonomy._infer_repo_root(Path(spec_path))
    target_head = verification._run_git(["-C", str(repo_root), "rev-parse", target_ref], runner)
    target_sha = target_head["stdout"].strip().lower()
    if target_head["status"] != "ok" or verification.SAFE_SHA_RE.fullmatch(target_sha) is None:
        summary["status"] = "target-unresolved"
        return summary
    summary["target_head"] = target_sha
    ancestor = verification._run_git(
        ["-C", str(repo_root), "merge-base", "--is-ancestor", candidate.lower(), target_sha],
        runner,
    )
    if ancestor["status"] == "ok":
        summary["status"] = "ancestor"
    elif ancestor["status"] == "non-zero" and ancestor["returncode"] == 1:
        summary["status"] = "not-ancestor"
    else:
        summary["status"] = "error"
    return summary


def slice_status_entry(registry, slice_row: dict, *, handoff_dir: str, git_runner=None) -> dict[str, Any]:
    slice_id = str(slice_row.get("slice_id") or "")
    builder_job_id = slice_row.get("builder_job_id")
    reviewer_job_id = slice_row.get("reviewer_job_id")
    builder_job_state: str | None = None
    reviewer_job_state: str | None = None
    if hasattr(registry, "get_job"):
        try:
            if isinstance(builder_job_id, str):
                builder_job_state = str(registry.get_job(builder_job_id).get("status"))
        except Exception:
            builder_job_state = None
        try:
            if isinstance(reviewer_job_id, str):
                reviewer_job_state = str(registry.get_job(reviewer_job_id).get("status"))
        except Exception:
            reviewer_job_state = None
    reason = None
    manifest = _read_manifest_payload(Path(handoff_dir) / f"{slice_id}.json")
    if isinstance(manifest, dict):
        gate_reason = manifest.get("gate_reason")
        if isinstance(gate_reason, str) and gate_reason:
            reason = gate_reason
    if reason is None:
        actions = slice_row.get("actions")
        if isinstance(actions, list) and actions:
            latest = actions[-1]
            if isinstance(latest, dict):
                latest_action = latest.get("action")
                if isinstance(latest_action, str) and latest_action:
                    reason = latest_action
    return {
        "slice_id": slice_id,
        "slice_state": slice_row.get("state"),
        "gate_state": slice_row.get("gate_state"),
        "job_state": reviewer_job_state or builder_job_state,
        "builder_job_id": builder_job_id,
        "builder_job_state": builder_job_state,
        "reviewer_job_id": reviewer_job_id,
        "reviewer_job_state": reviewer_job_state,
        "reason": reason,
        "candidate": slice_row.get("candidate"),
        "target_remote": slice_row.get("target_remote"),
        "target_branch": slice_row.get("target_branch"),
        "ancestry": _resolve_ancestry_status(slice_row, git_runner=git_runner),
        "current_evidence_refs": list(slice_row.get("current_evidence_refs") or []),
        "current_evaluation_refs": list(slice_row.get("current_evaluation_refs") or []),
        "next_actions": allowed_slice_actions(registry, slice_row),
    }


def _completion_candidate_ref(
    *,
    registry,
    slice_row: dict,
    repo_root: Path,
    coordinator_root: Path | None,
    gate_status: str,
    gate_reason: str | None,
    clock: Callable[[], str],
    git_runner,
) -> tuple[str, str | None, dict | None]:
    if gate_status not in {"verified", "passed"}:
        return gate_status, gate_reason, None
    slice_id = str(slice_row["slice_id"])
    candidate = slice_row.get("candidate")
    if not isinstance(candidate, str) or verification.SAFE_SHA_RE.fullmatch(candidate) is None:
        registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")
        registry.record_action(
            slice_id,
            action="completion-candidate-invalid",
            actor="manager",
            state="needs_human",
            gate_state="needs_human",
        )
        return "needs_human", "completion-candidate-invalid", None
    target_remote = str(slice_row.get("target_remote") or "origin")
    target_branch = str(slice_row.get("target_branch") or "main")
    target_ref = f"refs/remotes/{target_remote}/{target_branch}"
    if slice_row.get("state") == "completed" and slice_row.get("gate_state") == "passed":
        try:
            record_path = completion.completion_record_path(
                slice_id=slice_id,
                candidate=candidate.lower(),
                coordinator_root=coordinator_root,
            )
            payload = completion.read_completion_record(record_path)
            return "passed", "candidate-merged", {
                "path": str(record_path),
                "hash": verification.canonical_json_hash(payload),
                "payload": payload,
            }
        except Exception:
            return "needs_human", "completion-record-missing", None
    fetch_result = verification._run_git(
        ["-C", str(repo_root), "fetch", "--no-tags", target_remote, target_branch],
        git_runner,
    )
    if fetch_result["status"] != "ok":
        return "verified", "target-fetch-failed", None
    target_head = verification._run_git(["-C", str(repo_root), "rev-parse", target_ref], git_runner)
    target_sha = target_head["stdout"].strip().lower()
    if target_head["status"] != "ok" or verification.SAFE_SHA_RE.fullmatch(target_sha) is None:
        return "verified", "target-ref-unreadable", None
    ancestor = verification._run_git(
        ["-C", str(repo_root), "merge-base", "--is-ancestor", candidate.lower(), target_sha],
        git_runner,
    )
    if ancestor["status"] != "ok":
        if ancestor["status"] == "non-zero" and ancestor["returncode"] == 1:
            return "verified", "candidate-not-merged", None
        return "verified", "target-ancestry-error", None

    verification_path, verification_hash = _current_verification_ref(slice_row)
    review_path, review_hash, _ = _current_review_ref(slice_row)
    contract = slice_row.get("verification", {}).get("contract")
    docs_class = (
        contract.get("docs_class")
        if isinstance(contract, dict) and isinstance(contract.get("docs_class"), str)
        else "code"
    )
    review_policy = (
        contract.get("review_policy")
        if isinstance(contract, dict) and contract.get("review_policy") in {"required", "not-required"}
        else ("required" if docs_class in {"normative", "code"} else "not-required")
    )
    if verification_path is None or verification_hash is None:
        return "verified", "completion-missing-verification-evidence", None
    if review_policy == "required" and (
        not isinstance(slice_row.get("reviewer_job_id"), str)
        or review_path is None
        or review_hash is None
    ):
        try:
            registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")
            registry.record_action(
                slice_id,
                action="completion-missing-review-evaluation",
                actor="manager",
                state="needs_human",
                gate_state="needs_human",
            )
        except Exception:
            pass
        return "needs_human", "completion-missing-review-evaluation", None
    payload = {
        "schema_version": completion.COMPLETION_SCHEMA_VERSION,
        "slice_id": slice_id,
        "spec_hash": str(slice_row["spec"]["hash"]),
        "plan_hash": str(slice_row["plan"]["hash"]),
        "verification_hash": str(slice_row["verification"]["hash"]),
        "builder_job_id": str(slice_row["builder_job_id"]),
        "reviewer_job_id": slice_row.get("reviewer_job_id"),
        "dispatch_base": str(slice_row["dispatch_base"]),
        "candidate": candidate.lower(),
        "target_branch": target_branch,
        "target_remote": target_remote,
        "target_ref": target_ref,
        "target_ref_sha": target_sha,
        "verification_evidence_path": verification_path,
        "verification_evidence_hash": verification_hash,
        "review_policy": review_policy,
        "docs_class": docs_class,
        "review_evaluation_path": review_path,
        "review_evaluation_hash": review_hash,
        "completed_at": clock(),
    }
    try:
        record = completion.write_completion_record(payload, coordinator_root=coordinator_root)
    except Exception:
        try:
            registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")
            registry.record_action(
                slice_id,
                action="completion-record-write-failed",
                actor="manager",
                state="needs_human",
                gate_state="needs_human",
            )
        except Exception:
            pass
        return "needs_human", "completion-record-write-failed", None
    try:
        registry.update_slice(slice_id, state="completed", gate_state="passed")
    except Exception:
        return "verified", "completion-state-update-failed", record
    record_action_kwargs: dict[str, Any] = {
        "action": "completion-recorded",
        "actor": "manager",
        "state": "completed",
        "gate_state": "passed",
        "candidate": candidate.lower(),
        "evidence_refs": [verification_path],
    }
    if review_path is not None:
        record_action_kwargs["evaluation_refs"] = [review_path]
    try:
        registry.record_action(slice_id, **record_action_kwargs)
    except Exception:
        return "verified", "completion-action-record-failed", record
    return "passed", "candidate-merged", record


def _review_log_has_only_json_lines(log_path: object) -> bool:
    if not isinstance(log_path, str) or not log_path:
        return True
    path = Path(log_path)
    if not path.is_file():
        return True
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    for line in lines:
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return False
    return True


def _apply_review_evaluation(registry, slice_id: str, evaluation: dict) -> None:
    payload = evaluation["payload"]
    state = payload["state"]
    gate_state = {"passed": "passed", "rejected": "failed", "absent": "needs_human"}[state]
    slice_state = "verified" if state == "passed" else "needs_human"
    action = {
        "passed": "foreign-review-passed",
        "rejected": "foreign-review-rejected",
        "absent": "foreign-review-absent",
    }[state]
    registry.record_action(
        slice_id,
        action=action,
        actor="manager",
        state=slice_state,
        gate_state=gate_state,
        evaluation_refs=[evaluation["path"]],
        candidate=payload["candidate"],
    )


def _write_gate_evaluation(
    *,
    slice_id: str,
    state: str,
    reason: str,
    builder_job_id: str,
    reviewer_job_id: str | None,
    candidate: str,
    builder_identity: dict | None,
    reviewer_identity: dict | None,
    findings: list[dict] | None,
    coordinator_root: Path | None,
) -> dict:
    payload = foreign_review.build_gate_evaluation(
        slice_id=slice_id,
        state=state,
        reason=reason,
        builder_job_id=builder_job_id,
        reviewer_job_id=reviewer_job_id,
        candidate=candidate,
        launch_identity={"builder": builder_identity, "reviewer": reviewer_identity},
        findings=findings,
    )
    return foreign_review.write_gate_evaluation(payload, coordinator_root=coordinator_root)


def _review_inputs_drifted(slice_row: dict, review_job: dict) -> bool:
    if slice_row.get("candidate") != review_job.get("subject_head"):
        return True
    return any(
        slice_row[key]["hash"] != review_job.get(f"{key}_hash")
        for key in ("spec", "plan")
    ) or slice_row["verification"]["hash"] != review_job.get("verification_hash")


def _launch_foreign_review(
    *,
    registry,
    slice_row: dict,
    builder_job: dict,
    repo_root: Path,
    coordinator_root: Path | None,
    candidate: str,
    subprocess_runner,
    git_runner,
    review_launcher,
    review_executor: str | None,
    review_model: str | None,
) -> dict[str, Any]:
    builder_job_id = str(builder_job["job_id"])
    slice_id = str(slice_row["slice_id"])
    builder_identity = None
    try:
        tier = foreign_review.read_repo_tier(repo_root)
        identity_registry = _identity_registry()
    except Exception as exc:
        builder_identity = _builder_launch_identity(builder_job)
        evaluation = _write_gate_evaluation(
            slice_id=slice_id,
            state="absent",
            reason="config-error",
            builder_job_id=builder_job_id,
            reviewer_job_id=None,
            candidate=candidate,
            builder_identity=builder_identity,
            reviewer_identity=None,
            findings=[],
            coordinator_root=coordinator_root,
        )
        _apply_review_evaluation(registry, slice_id, evaluation)
        return {
            "launched": False,
            "gate_status": "needs_human",
            "gate_reason": f"foreign-review-config-error:{exc}",
            "evaluation": evaluation,
        }
    decision = foreign_review.select_foreign_reviewer(
        registry=identity_registry,
        builder_executor=builder_job.get("executor"),
        builder_model_id=builder_job.get("model_id"),
        review_executor=review_executor,
        review_model_id=review_model,
        tier=tier,
    )
    builder_identity = decision.get("builder") or _builder_launch_identity(builder_job, identity_registry)
    reviewer_identity = decision.get("reviewer")
    if decision["state"] == "needs_human":
        evaluation = _write_gate_evaluation(
            slice_id=slice_id,
            state="absent",
            reason=str(decision["reason"]),
            builder_job_id=builder_job_id,
            reviewer_job_id=None,
            candidate=candidate,
            builder_identity=builder_identity,
            reviewer_identity=reviewer_identity,
            findings=[],
            coordinator_root=coordinator_root,
        )
        _apply_review_evaluation(registry, slice_id, evaluation)
        return {
            "launched": False,
            "gate_status": "needs_human",
            "gate_reason": str(decision["reason"]),
            "evaluation": evaluation,
        }
    if decision["state"] == "absent":
        evaluation = _write_gate_evaluation(
            slice_id=slice_id,
            state="absent",
            reason=str(decision["reason"]),
            builder_job_id=builder_job_id,
            reviewer_job_id=None,
            candidate=candidate,
            builder_identity=builder_identity,
            reviewer_identity=reviewer_identity,
            findings=[],
            coordinator_root=coordinator_root,
        )
        _apply_review_evaluation(registry, slice_id, evaluation)
        return {
            "launched": False,
            "gate_status": "needs_human",
            "gate_reason": "foreign-review-absent",
            "evaluation": evaluation,
        }
    if review_launcher is None:
        evaluation = _write_gate_evaluation(
            slice_id=slice_id,
            state="absent",
            reason="launcher-missing",
            builder_job_id=builder_job_id,
            reviewer_job_id=None,
            candidate=candidate,
            builder_identity=builder_identity,
            reviewer_identity=reviewer_identity,
            findings=[],
            coordinator_root=coordinator_root,
        )
        _apply_review_evaluation(registry, slice_id, evaluation)
        return {
            "launched": False,
            "gate_status": "needs_human",
            "gate_reason": "foreign-review-launcher-missing",
            "evaluation": evaluation,
        }
    reviewer_job = registry.create_job(
        task=slice_id,
        persona="reviewer",
        kind="review",
        branch=str(builder_job.get("branch") or f"feature/{slice_id}"),
        pane="",
        worktree="",
        dispatch_head=slice_row.get("dispatch_base"),
        executor=review_executor,
        model_id=review_model,
        independence_domain=reviewer_identity["independence_domain"] if reviewer_identity else None,
        subject_head=candidate,
        spec_hash=slice_row["spec"]["hash"],
        plan_hash=slice_row["plan"]["hash"],
        verification_hash=slice_row["verification"]["hash"],
    )
    try:
        review_worktree = foreign_review.prepare_review_worktree(
            repo_root=repo_root,
            slice_id=slice_id,
            reviewer_job_id=reviewer_job["job_id"],
            candidate=candidate,
            subprocess_runner=subprocess_runner,
            git_runner=git_runner,
        )
        registry.update_job(reviewer_job["job_id"], worktree=str(review_worktree))
        prompt = foreign_review.build_review_prompt(
            slice_id=slice_id,
            plan_path=slice_row["plan"]["path"],
            verdict_path=str(foreign_review.review_verdict_path(review_worktree)),
            builder_job_id=builder_job_id,
            reviewer_job_id=reviewer_job["job_id"],
            candidate=candidate,
            launch_identity=reviewer_identity,
        )
        handle = review_launcher.launch(
            slice_id=reviewer_job["job_id"],
            prompt=prompt,
            worktree=str(review_worktree),
            log_dir=str(Path("runtime/review") / slice_id),
        )
        registry.attach_launch_handle(
            reviewer_job["job_id"],
            executor=handle.executor,
            model_id=handle.model_id,
            session_name=handle.session_name,
            pid=handle.pid,
            log_path=handle.log_path,
        )
        registry.update_slice(slice_id, reviewer_job_id=reviewer_job["job_id"], candidate=candidate)
        registry.record_action(
            slice_id,
            action="foreign-review-dispatched",
            actor="manager",
            state="reviewing",
            gate_state="pending",
            candidate=candidate,
        )
        return {"launched": True, "reviewer_job_id": reviewer_job["job_id"]}
    except Exception as exc:
        try:
            registry.update_status(reviewer_job["job_id"], "failed")
        except Exception:
            pass
        evaluation = _write_gate_evaluation(
            slice_id=slice_id,
            state="absent",
            reason="launch-error",
            builder_job_id=builder_job_id,
            reviewer_job_id=reviewer_job["job_id"],
            candidate=candidate,
            builder_identity=builder_identity,
            reviewer_identity=reviewer_identity,
            findings=[],
            coordinator_root=coordinator_root,
        )
        _apply_review_evaluation(registry, slice_id, evaluation)
        return {
            "launched": False,
            "gate_status": "needs_human",
            "gate_reason": f"foreign-review-launch-error:{exc}",
            "evaluation": evaluation,
        }


def _finalize_review_job(
    *,
    registry,
    slice_row: dict,
    review_job: dict,
    coordinator_root: Path | None,
    identity_registry: dict[tuple[str, str], dict[str, str]] | None,
    git_runner,
) -> tuple[dict | None, str, str]:
    slice_id = str(slice_row["slice_id"])
    builder_job = registry.get_job(slice_row["builder_job_id"])
    candidate = str(review_job.get("subject_head") or slice_row.get("candidate") or "")
    builder_identity = _builder_launch_identity(builder_job, identity_registry)
    reviewer_identity = _reviewer_launch_identity(review_job)
    if _review_inputs_drifted(slice_row, review_job):
        evaluation = _write_gate_evaluation(
            slice_id=slice_id,
            state="absent",
            reason="stale-input",
            builder_job_id=builder_job["job_id"],
            reviewer_job_id=review_job["job_id"],
            candidate=candidate,
            builder_identity=builder_identity,
            reviewer_identity=reviewer_identity,
            findings=[],
            coordinator_root=coordinator_root,
        )
        registry.record_action(
            slice_id,
            action="foreign-review-stale-input",
            actor="manager",
            state="needs_human",
            gate_state="needs_human",
            evaluation_refs=[evaluation["path"]],
            candidate=slice_row.get("candidate"),
        )
        registry.update_slice(slice_id, current_evaluation_refs=[], state="needs_human", gate_state="needs_human")
        return evaluation, "needs_human", "stale-input"
    if review_job.get("status") == "failed":
        evaluation = _write_gate_evaluation(
            slice_id=slice_id,
            state="absent",
            reason="reviewer-process-failed",
            builder_job_id=builder_job["job_id"],
            reviewer_job_id=review_job["job_id"],
            candidate=candidate,
            builder_identity=builder_identity,
            reviewer_identity=reviewer_identity,
            findings=[],
            coordinator_root=coordinator_root,
        )
        _apply_review_evaluation(registry, slice_id, evaluation)
        return evaluation, "needs_human", "foreign-review-absent"
    worktree = Path(str(review_job["worktree"]))
    review_head = verification._run_git(["-C", str(worktree), "rev-parse", "HEAD"], git_runner)
    if review_head["status"] != "ok" or review_head["stdout"].strip().lower() != candidate.lower():
        evaluation = _write_gate_evaluation(
            slice_id=slice_id,
            state="absent",
            reason="stale-head",
            builder_job_id=builder_job["job_id"],
            reviewer_job_id=review_job["job_id"],
            candidate=candidate,
            builder_identity=builder_identity,
            reviewer_identity=reviewer_identity,
            findings=[],
            coordinator_root=coordinator_root,
        )
        _apply_review_evaluation(registry, slice_id, evaluation)
        return evaluation, "needs_human", "foreign-review-absent"
    if not _review_log_has_only_json_lines(review_job.get("log_path")):
        evaluation = _write_gate_evaluation(
            slice_id=slice_id,
            state="absent",
            reason="invalid-process-output",
            builder_job_id=builder_job["job_id"],
            reviewer_job_id=review_job["job_id"],
            candidate=candidate,
            builder_identity=builder_identity,
            reviewer_identity=reviewer_identity,
            findings=[],
            coordinator_root=coordinator_root,
        )
        _apply_review_evaluation(registry, slice_id, evaluation)
        return evaluation, "needs_human", "foreign-review-absent"
    verdict_path = foreign_review.review_verdict_path(worktree)
    if not verdict_path.is_file():
        evaluation = _write_gate_evaluation(
            slice_id=slice_id,
            state="absent",
            reason="verdict-missing",
            builder_job_id=builder_job["job_id"],
            reviewer_job_id=review_job["job_id"],
            candidate=candidate,
            builder_identity=builder_identity,
            reviewer_identity=reviewer_identity,
            findings=[],
            coordinator_root=coordinator_root,
        )
        _apply_review_evaluation(registry, slice_id, evaluation)
        return evaluation, "needs_human", "foreign-review-absent"
    try:
        verdict = foreign_review.read_review_verdict_file(
            verdict_path,
            builder_job_id=builder_job["job_id"],
            reviewer_job_id=review_job["job_id"],
            candidate=candidate,
            launch_identity=reviewer_identity,
        )
    except Exception:
        evaluation = _write_gate_evaluation(
            slice_id=slice_id,
            state="absent",
            reason="invalid-verdict",
            builder_job_id=builder_job["job_id"],
            reviewer_job_id=review_job["job_id"],
            candidate=candidate,
            builder_identity=builder_identity,
            reviewer_identity=reviewer_identity,
            findings=[],
            coordinator_root=coordinator_root,
        )
        _apply_review_evaluation(registry, slice_id, evaluation)
        return evaluation, "needs_human", "foreign-review-absent"
    reason = "blocking-findings" if verdict["state"] == "rejected" else "accepted"
    evaluation = _write_gate_evaluation(
        slice_id=slice_id,
        state=verdict["state"],
        reason=reason,
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=review_job["job_id"],
        candidate=candidate,
        builder_identity=builder_identity,
        reviewer_identity=reviewer_identity,
        findings=verdict["findings"],
        coordinator_root=coordinator_root,
    )
    _apply_review_evaluation(registry, slice_id, evaluation)
    gate_status = "passed" if verdict["state"] == "passed" else "failed"
    return evaluation, gate_status, reason


def apply_slice_action(
    dispatcher,
    *,
    slice_id: str,
    action: str,
    actor: str,
    specs_dir: str,
    handoff_dir: str = autonomy.DEFAULT_HANDOFF_DIR,
    launcher=None,
    review_launcher=None,
    persona: str = "builder",
    review_executor: str | None = None,
    review_model: str | None = None,
    clock: Callable[[], str] = _utcnow,
    git_runner=None,
    subprocess_runner=None,
    verification_runner=None,
    scan_specs_fn: Callable[[str], list[dict[str, Any]]] = autonomy.scan_specs,
    dispatch_ready_fn: Callable[..., list[dict[str, Any]]] = autonomy.dispatch_ready,
) -> dict[str, Any]:
    registry = getattr(dispatcher, "_registry", None)
    if registry is None:
        raise RuntimeError("slice-action requires dispatcher._registry")
    if action not in SLICE_ACTIONS:
        raise ValueError(f"unsupported-slice-action:{action}")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("slice-action actor must be a non-empty string")
    try:
        slice_row = registry.get_slice(slice_id)
    except KeyError as exc:
        raise ValueError("unknown-slice") from exc
    if action not in allowed_slice_actions(registry, slice_row):
        raise ValueError(f"action-not-allowed:{action}")

    requested_at = clock()
    runner = git_runner or getattr(dispatcher, "_git_runner", None)
    verification_runner = verification_runner or verification.run_result_verification

    if action == "abandon":
        consumed_at = clock()
        registry.record_action(
            slice_id,
            action="operator-abandon",
            actor=actor,
            state="failed",
            gate_state="failed",
            requested_at=requested_at,
            consumed_at=consumed_at,
            result="ok",
        )
        latest = registry.get_slice(slice_id)
        return {
            "slice_id": slice_id,
            "action": action,
            "slice_state": latest.get("state"),
            "gate_state": latest.get("gate_state"),
            "result": "ok",
            "requested_at": requested_at,
            "consumed_at": consumed_at,
        }

    if action == "retry-build":
        metas = scan_specs_fn(specs_dir)
        target = next((meta for meta in metas if meta.get("slice_id") == slice_id), None)
        if target is None:
            raise ValueError("unknown-slice")
        if isinstance(target.get("parse_error"), dict):
            raise ValueError(f"invalid-spec:{target['parse_error'].get('field')}")
        if not (isinstance(target.get("plan"), str) and target["plan"]):
            raise ValueError("no-plan")
        dispatched = dispatch_ready_fn(
            [{**target, "dispatch": "auto"}],
            lambda sid: autonomy.default_is_satisfied(
                sid,
                handoff_dir=handoff_dir,
                git_runner=runner,
            ),
            dispatcher,
            persona=persona,
            launcher=launcher,
            handoff_dir=handoff_dir,
            git_runner=runner,
        )
        if not dispatched:
            raise RuntimeError("retry-build-dispatch-failed")
        latest = registry.get_slice(slice_id)
        outcome = {
            "slice_id": slice_id,
            "action": action,
            "job_id": dispatched[0].get("job_id"),
            "slice_state": latest.get("state"),
            "gate_state": latest.get("gate_state"),
        }
    elif action == "retry-verify":
        builder_job_id = slice_row.get("builder_job_id")
        if not isinstance(builder_job_id, str):
            raise ValueError("retry-verify-missing-builder")
        builder_job = registry.get_job(builder_job_id)
        if builder_job.get("status") != "exited":
            raise ValueError("retry-verify-builder-not-exited")
        repo_root = autonomy._infer_repo_root(Path(slice_row["spec"]["path"]))
        state_path = getattr(registry, "_state_path", None)
        coordinator_root = Path(state_path).parent if state_path is not None else None
        try:
            evidence = verification_runner(
                slice_row=slice_row,
                job=builder_job,
                repo_root=repo_root,
                coordinator_root=coordinator_root,
                git_runner=runner,
                subprocess_runner=subprocess_runner,
            )
            evidence = _validate_result_evidence(
                evidence=evidence,
                slice_id=slice_id,
                coordinator_root=coordinator_root,
            )
            _apply_verification_result(registry, slice_id, evidence)
            gate_status = str(evidence["payload"]["status"])
            gate_reason = str(evidence["payload"]["summary"])
        except Exception as exc:
            evidence = _write_status_evidence(
                slice_row=slice_row,
                job=builder_job,
                repo_root=repo_root,
                coordinator_root=coordinator_root,
                git_runner=runner,
                status="needs_human",
                summary="verification-runner-error",
                details={"error": str(exc)},
            )
            if evidence is not None:
                _apply_verification_result(registry, slice_id, evidence)
            else:
                registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")
            gate_status = "needs_human"
            gate_reason = "verification-runner-error"
        launch_result: dict[str, Any] | None = None
        if gate_status == "reviewing":
            launch_result = _launch_foreign_review(
                registry=registry,
                slice_row=registry.get_slice(slice_id),
                builder_job=builder_job,
                repo_root=repo_root,
                coordinator_root=coordinator_root,
                candidate=str(evidence["payload"]["candidate"]),
                subprocess_runner=subprocess_runner,
                git_runner=runner,
                review_launcher=review_launcher,
                review_executor=review_executor,
                review_model=review_model,
            )
            if not launch_result.get("launched"):
                gate_status = str(launch_result.get("gate_status") or "needs_human")
                gate_reason = str(launch_result.get("gate_reason") or "foreign-review-absent")
        latest = registry.get_slice(slice_id)
        refs = latest.get("current_evidence_refs") or []
        outcome = {
            "slice_id": slice_id,
            "action": action,
            "gate_status": gate_status,
            "gate_reason": gate_reason,
            "verification_evidence_path": refs[0] if refs else None,
            "slice_state": latest.get("state"),
            "gate_state": latest.get("gate_state"),
        }
        if launch_result is not None:
            outcome["review_launched"] = bool(launch_result.get("launched"))
            if launch_result.get("reviewer_job_id") is not None:
                outcome["reviewer_job_id"] = launch_result.get("reviewer_job_id")
    else:  # retry-review
        builder_job_id = slice_row.get("builder_job_id")
        candidate = slice_row.get("candidate")
        if not isinstance(builder_job_id, str):
            raise ValueError("retry-review-missing-builder")
        if not (
            isinstance(candidate, str)
            and verification.SAFE_SHA_RE.fullmatch(candidate) is not None
        ):
            raise ValueError("retry-review-candidate-invalid")
        builder_job = registry.get_job(builder_job_id)
        repo_root = autonomy._infer_repo_root(Path(slice_row["spec"]["path"]))
        state_path = getattr(registry, "_state_path", None)
        coordinator_root = Path(state_path).parent if state_path is not None else None
        launch_result = _launch_foreign_review(
            registry=registry,
            slice_row=registry.get_slice(slice_id),
            builder_job=builder_job,
            repo_root=repo_root,
            coordinator_root=coordinator_root,
            candidate=candidate.lower(),
            subprocess_runner=subprocess_runner,
            git_runner=runner,
            review_launcher=review_launcher,
            review_executor=review_executor,
            review_model=review_model,
        )
        latest = registry.get_slice(slice_id)
        outcome = {
            "slice_id": slice_id,
            "action": action,
            "slice_state": latest.get("state"),
            "gate_state": latest.get("gate_state"),
        }
        if launch_result.get("launched"):
            outcome["launched"] = True
            outcome["reviewer_job_id"] = launch_result.get("reviewer_job_id")
        else:
            outcome["launched"] = False
            outcome["gate_status"] = launch_result.get("gate_status")
            outcome["gate_reason"] = launch_result.get("gate_reason")
            evaluation = launch_result.get("evaluation")
            if isinstance(evaluation, dict):
                outcome["review_evaluation_path"] = evaluation.get("path")

    consumed_at = clock()
    registry.record_action(
        slice_id,
        action=f"operator-{action}",
        actor=actor,
        requested_at=requested_at,
        consumed_at=consumed_at,
        result="ok",
    )
    outcome["result"] = "ok"
    outcome["requested_at"] = requested_at
    outcome["consumed_at"] = consumed_at
    return outcome


def complete_tick(
    dispatcher,
    *,
    gate_runner: GateRunner | None = None,
    handoff_dir: str = autonomy.DEFAULT_HANDOFF_DIR,
    metas: list[dict] | None = None,
    clock: Callable[[], str] = _utcnow,
    git_runner=None,
    subprocess_runner=None,
    verification_runner=None,
    review_launcher=None,
    review_executor: str | None = None,
    review_model: str | None = None,
) -> dict:
    registry = getattr(dispatcher, "_registry", None)
    if registry is None:
        raise RuntimeError("complete_tick 需 dispatcher._registry（fail-closed）")
    hdir = Path(handoff_dir)
    git_runner = git_runner or getattr(dispatcher, "_git_runner", None)
    verification_runner = verification_runner or verification.run_result_verification

    polled: list[str] = []
    completed: list[dict] = []
    errors: list[dict] = []
    warnings: list[dict] = []
    seen_slices: dict[str, str] = {}  # slice_id → 本輪已寫盤的 job_id（偵測同輪同 slice 雙 terminal）

    meta_by_slice: dict[str, dict] = {}
    if isinstance(metas, list):
        for meta in metas:
            if not isinstance(meta, dict):
                continue
            sid = meta.get("slice_id")
            if isinstance(sid, str):
                meta_by_slice[sid] = meta

    def _repo_root_for_slice(slice_id: str) -> Path | None:
        spec_path = None
        meta = meta_by_slice.get(slice_id)
        if isinstance(meta, dict):
            spec_path = meta.get("spec_path")
        if not isinstance(spec_path, str) or not spec_path:
            try:
                spec_path = registry.get_slice(slice_id).get("spec", {}).get("path")
            except Exception:
                spec_path = None
        if not isinstance(spec_path, str) or not spec_path:
            return None
        return autonomy._infer_repo_root(Path(spec_path))

    def _ready_ids() -> set[str]:
        return {
            m["slice_id"]
            for m in autonomy.ready_units(
                metas,
                lambda sid: autonomy.default_is_satisfied(
                    sid,
                    handoff_dir=handoff_dir,
                    repo_root=_repo_root_for_slice(sid),
                    git_runner=git_runner,
                ),
            )
        }

    released_ok = metas is not None
    before_ready: set[str] = set()
    if released_ok:
        try:
            before_ready = _ready_ids()
        except ValueError:
            released_ok = False  # metas 有環/重複 → released 觀測停用，不擋完成側

    for snapshot in registry.list_jobs():
        job_id = snapshot["job_id"]
        try:
            job = snapshot
            status = job.get("status")
            if status in IN_FLIGHT_STATUSES:
                job = dispatcher.poll_headless_done(job_id)
                polled.append(job_id)
                status = job.get("status")

            if status not in TERMINAL_STATUSES:
                continue

            slice_id = job.get("task")
            if not _is_safe_slice_id(slice_id):
                errors.append({"job_id": job_id, "error": f"job 缺合法/安全 task/slice_id: {slice_id!r}"})
                continue
            manifest_path = hdir / f"{slice_id}.json"
            if manifest_path.is_symlink():
                # 單檔 symlink 檢查：防預置 symlink 讓 write_manifest 寫出界（不誤殺部署上層 symlink）。
                errors.append(
                    {"job_id": job_id, "error": f"handoff manifest path 拒絕 symlink: {manifest_path}"}
                )
                continue
            if _existing_manifest_job_id(manifest_path) == job_id:
                continue  # 真冪等：同一個 terminal job 已落盤（同 job_id → skip；異 job_id/壞檔 → overwrite）

            if job.get("kind") == "review":
                slice_row = _slice_for_reviewer_job(registry, slice_id, job_id)
            else:
                slice_row = _slice_for_job(registry, slice_id, job_id)
                if slice_row is not None and slice_row.get("reviewer_job_id"):
                    continue
            repo_root = (
                autonomy._infer_repo_root(Path(slice_row["spec"]["path"]))
                if slice_row is not None
                else Path.cwd().resolve()
            )
            state_path = getattr(registry, "_state_path", None)
            coordinator_root = Path(state_path).parent if state_path is not None else None
            evidence = None
            publish_evidence = False
            evaluation = None
            completion_record = None
            gate_status = "failed" if status == "failed" else "needs_human"
            gate_reason = None

            if job.get("kind") == "review":
                try:
                    identity_registry = _identity_registry()
                except Exception:
                    identity_registry = None
                if slice_row is None:
                    gate_status = "needs_human"
                    gate_reason = "missing-slice-proof"
                elif slice_row.get("state") in {"verified", "completed"} and slice_row.get("gate_state") == "passed":
                    review_path, review_hash, review_payload = _current_review_ref(slice_row)
                    if review_payload is not None:
                        evaluation = {"path": review_path, "hash": review_hash, "payload": review_payload}
                    gate_status = "passed" if slice_row.get("state") == "completed" else "verified"
                    gate_reason = "accepted"
                else:
                    evaluation, gate_status, gate_reason = _finalize_review_job(
                        registry=registry,
                        slice_row=slice_row,
                        review_job=job,
                        coordinator_root=coordinator_root,
                        identity_registry=identity_registry,
                        git_runner=git_runner,
                    )
            else:
                mismatches = _pinned_input_mismatches(slice_row) if slice_row is not None else []

                if mismatches:
                    gate_status = "needs_human"
                    gate_reason = "pinned-input-mismatch"
                    try:
                        evidence = _write_status_evidence(
                            slice_row=slice_row,
                            job=job,
                            repo_root=repo_root,
                            coordinator_root=coordinator_root,
                            git_runner=git_runner,
                            status="needs_human",
                            summary="pinned-input-mismatch",
                            details={"mismatches": mismatches},
                        )
                        if evidence is not None:
                            _apply_verification_result(registry, slice_id, evidence)
                            publish_evidence = True
                        else:
                            registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")
                    except Exception:
                        try:
                            registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")
                        except Exception:
                            pass
                elif status == "failed":
                    gate_status = "failed"
                    gate_reason = "builder-failed"
                    if slice_row is not None:
                        try:
                            registry.update_slice(slice_id, state="failed", gate_state="failed")
                        except Exception:
                            pass
                elif slice_row is None:
                    evidence = _write_status_evidence(
                        slice_row=None,
                        job=job,
                        repo_root=repo_root,
                        coordinator_root=coordinator_root,
                        git_runner=git_runner,
                        status="needs_human",
                        summary="missing-slice-proof",
                        details={"reason": "builder exited without pinned slice verification contract"},
                    )
                    gate_status = "needs_human"
                    gate_reason = "missing-slice-proof"
                    publish_evidence = evidence is not None
                elif slice_row.get("state") in {"verified", "completed"} and slice_row.get("gate_state") == "passed":
                    gate_status = "passed" if slice_row.get("state") == "completed" else "verified"
                    gate_reason = "accepted"
                else:
                    try:
                        evidence = verification_runner(
                            slice_row=slice_row,
                            job=job,
                            repo_root=repo_root,
                            coordinator_root=coordinator_root,
                            git_runner=git_runner,
                            subprocess_runner=subprocess_runner,
                        )
                        evidence = _validate_result_evidence(
                            evidence=evidence,
                            slice_id=slice_id,
                            coordinator_root=coordinator_root,
                        )
                        gate_status = evidence["payload"]["status"]
                        gate_reason = evidence["payload"]["summary"]
                    except Exception as exc:
                        gate_status = "needs_human"
                        gate_reason = "verification-runner-error"
                        try:
                            evidence = _write_status_evidence(
                                slice_row=slice_row,
                                job=job,
                                repo_root=repo_root,
                                coordinator_root=coordinator_root,
                                git_runner=git_runner,
                                status="needs_human",
                                summary="verification-runner-error",
                                details={"error": str(exc)},
                            )
                            if evidence is not None:
                                _apply_verification_result(registry, slice_id, evidence)
                                publish_evidence = True
                            else:
                                registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")
                        except Exception:
                            try:
                                registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")
                            except Exception:
                                pass
                    else:
                        try:
                            _apply_verification_result(registry, slice_id, evidence)
                            publish_evidence = True
                        except Exception:
                            gate_status = "needs_human"
                            gate_reason = "verification-state-update-error"
                            publish_evidence = False

                    if gate_status == "reviewing" and slice_row is not None:
                        launch_result = _launch_foreign_review(
                            registry=registry,
                            slice_row=registry.get_slice(slice_id),
                            builder_job=registry.get_job(job_id),
                            repo_root=repo_root,
                            coordinator_root=coordinator_root,
                            candidate=evidence["payload"]["candidate"],
                            subprocess_runner=subprocess_runner,
                            git_runner=git_runner,
                            review_launcher=review_launcher,
                            review_executor=review_executor,
                            review_model=review_model,
                        )
                        if launch_result.get("launched"):
                            continue
                        gate_status = str(launch_result["gate_status"])
                        gate_reason = str(launch_result["gate_reason"])
                        evaluation = launch_result.get("evaluation")

            if slice_row is not None:
                gate_status, gate_reason, completion_record = _completion_candidate_ref(
                    registry=registry,
                    slice_row=registry.get_slice(slice_id),
                    repo_root=repo_root,
                    coordinator_root=coordinator_root,
                    gate_status=gate_status,
                    gate_reason=gate_reason,
                    clock=clock,
                    git_runner=git_runner,
                )
                slice_row = registry.get_slice(slice_id)
            verification_path, verification_hash = _current_verification_ref(slice_row)
            review_path, review_hash, review_payload = _current_review_ref(slice_row)
            if evaluation is None and review_payload is not None:
                evaluation = {"path": review_path, "hash": review_hash, "payload": review_payload}
            handoff.write_manifest(
                manifest_path,
                {
                    "slice_id": slice_id,
                    "job_id": job_id,
                    "gate_status": gate_status,
                    "completion": status,
                    "exit_code": job.get("exit_code"),
                    "branch": job.get("branch"),
                    "gate_reason": gate_reason,
                    "gate_verdict": (
                        evaluation["payload"]
                        if evaluation is not None
                        else (evidence["payload"] if publish_evidence and evidence is not None else None)
                    ),
                    "verification_evidence_path": (
                        evidence["path"] if publish_evidence and evidence is not None else verification_path
                    ),
                    "verification_evidence_hash": (
                        evidence["hash"] if publish_evidence and evidence is not None else verification_hash
                    ),
                    "review_evaluation_path": evaluation["path"] if evaluation is not None else None,
                    "review_evaluation_hash": evaluation["hash"] if evaluation is not None else None,
                    "completion_record_path": (
                        completion_record["path"] if completion_record is not None else None
                    ),
                    "completion_record_hash": (
                        completion_record["hash"] if completion_record is not None else None
                    ),
                    "slice_state": slice_row.get("state") if isinstance(slice_row, dict) else None,
                    "spec_hash": (
                        slice_row.get("spec", {}).get("hash") if isinstance(slice_row, dict) else None
                    ),
                    "plan_hash": (
                        slice_row.get("plan", {}).get("hash") if isinstance(slice_row, dict) else None
                    ),
                    "verification_hash": (
                        slice_row.get("verification", {}).get("hash")
                        if isinstance(slice_row, dict)
                        else None
                    ),
                    "completed_at": clock(),
                },
            )
            if not publish_evidence and gate_reason in {
                "pinned-input-mismatch",
                "verification-runner-error",
                "verification-state-update-error",
            }:
                _discard_unpublished_evidence(evidence)
            if slice_id in seen_slices:
                # 同輪同 slice 第二個 terminal job：後者勝（manifest 已覆寫）→ 記 warning、completed 去重更新。
                warnings.append({"slice_id": slice_id, "warning": "same-slice concurrent terminals"})
                for entry in completed:
                    if entry["slice_id"] == slice_id:
                        entry["gate_status"] = gate_status
                        break
            else:
                completed.append({"slice_id": slice_id, "gate_status": gate_status})
            seen_slices[slice_id] = job_id
        except Exception as exc:
            errors.append({"job_id": job_id, "error": str(exc)})

    summary: dict = {"polled": polled, "completed": completed, "errors": errors, "warnings": warnings}
    if released_ok:
        try:
            summary["released"] = sorted(_ready_ids() - before_ready)
        except ValueError:
            pass
    return summary


def run_tick(
    dispatcher,
    *,
    metas: list[dict],
    launcher=None,
    review_launcher=None,
    persona: str = "builder",
    is_satisfied=None,
    gate_runner: GateRunner | None = None,
    handoff_dir: str = autonomy.DEFAULT_HANDOFF_DIR,
    require_idle: bool = False,
    max_load: float = 1.0,
    idle_probe: Callable[[], tuple] = os.getloadavg,
    clock: Callable[[], str] = _utcnow,
    reaper: Callable[[], dict] | None = None,
    review_executor: str | None = None,
    review_model: str | None = None,
) -> dict:
    """跑完整 manager tick：fanout（dispatch_ready）→ complete_tick →（可選）收尾 janitor。

    require_idle 時以 1-min load average gate（reuse memory.dream.idle，可注入 probe）——
    僅擋 fanout（新工作），complete_tick 一律跑。已有 dispatched/running job 的 slice
    本趟不重派（冪等）。fanout 例外（DispatchReadyError/RequiresLauncher/ValueError 環）
    收進 errors，不阻 complete。

    reaper 為收尾 janitor（issue #161）：傳入時於 complete 後呼叫一次以回收孤兒 codex
    broker（多 worktree 派工殘留），其回傳放 summary["reaped"]；任何例外收進 errors（stage=reap），
    不破壞 tick。預設 None（不啟用）——避免單測誤觸真實行程回收；production 由 CLI 接上。
    回 {dispatch_skipped, dispatched, completed, errors, reaped}。
    """
    satisfied = is_satisfied if is_satisfied is not None else _satisfied_pred(handoff_dir)
    dispatched: list = []
    errors: list = []
    # idle gate 只擋「派工側（新工作，會啟 agent，昂貴）」；完成側（poll→manifest，便宜的
    # 回收/記帳）一律跑，否則高負載時 job 完成/失敗狀態與下游釋放會被埋住（review F-C）。
    if require_idle and not idle.is_idle(max_load=max_load, probe=idle_probe):
        dispatch_skipped: str | bool = "not-idle"
    else:
        dispatch_skipped = False
        # 冪等：跳過 registry 中已有 dispatched/running job 的 slice，避免 oneshot+timer
        # 反覆對同一 slice 重派（review F-A：一 slice 一 job 不變量）。
        registry = getattr(dispatcher, "_registry", None)
        active = (
            {j.get("task") for j in registry.list_jobs() if j.get("status") in IN_FLIGHT_STATUSES}
            if registry is not None
            else set()
        )
        fanout_metas = [m for m in metas if m.get("slice_id") not in active]
        try:
            dispatched = autonomy.dispatch_ready(
                fanout_metas,
                satisfied,
                dispatcher,
                persona=persona,
                launcher=launcher,
                git_runner=getattr(dispatcher, "_git_runner", None),
                handoff_dir=handoff_dir,
            )
        except (
            autonomy.DispatchReadyError,
            autonomy.DispatchReadyRequiresLauncherError,
            ValueError,
        ) as exc:
            errors.append({"stage": "fanout", "error": str(exc)})
    complete = complete_tick(
        dispatcher,
        gate_runner=gate_runner,
        handoff_dir=handoff_dir,
        metas=metas,
        clock=clock,
        review_launcher=review_launcher,
        review_executor=review_executor,
        review_model=review_model,
    )
    # 收尾 janitor（issue #161）：回收孤兒 codex broker。失敗一律不破壞 tick——
    # 收進 errors（stage=reap），狀態放 summary["reaped"]。
    reaped = None
    reap_errors: list = []
    if reaper is not None:
        try:
            reaped = reaper()
        except Exception as exc:
            reap_errors.append({"stage": "reap", "error": str(exc)})
    return {
        "dispatch_skipped": dispatch_skipped,
        "dispatched": dispatched,
        "completed": complete["completed"],
        "errors": errors + complete["errors"] + reap_errors,
        "reaped": reaped,
    }


def _required_workflow_string(args: Mapping[str, object], field: str) -> str:
    value = args.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"workflow-action requires {field}")
    return value.strip()


def _load_workflow_manifest(path_value: str) -> WorkflowManifest:
    path = Path(path_value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"workflow manifest unreadable: {path}") from exc
    return WorkflowManifest.from_dict(payload)


def _load_planning_artifacts(
    args: Mapping[str, object],
    *,
    work_id: str,
    persisted: tuple[PlanningArtifactAuthority, ...] = (),
) -> tuple[tuple[PlanningArtifact, ...], tuple[PlanningArtifactAuthority, ...]]:
    root = Path(_required_workflow_string(args, "artifact_root")).resolve()
    rows = args.get("planning_artifacts")
    if not isinstance(rows, list):
        raise ValueError("workflow-action planning_artifacts must be a list")
    requested: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"kind", "ref"}:
            raise ValueError(f"workflow-action planning_artifacts[{index}] invalid")
        kind = row.get("kind")
        ref = row.get("ref")
        if not isinstance(kind, str) or not isinstance(ref, str) or not ref:
            raise ValueError(f"workflow-action planning_artifacts[{index}] invalid")
        requested.append((kind, ref))
    if persisted:
        expected = [(item.kind, item.ref) for item in persisted]
        if requested != expected:
            raise ValueError("workflow planning artifact scan differs from persisted authority")
        authority = persisted
    else:
        authority = ()
    artifacts: list[PlanningArtifact] = []
    scanned: list[PlanningArtifactAuthority] = []
    for index, (kind, ref) in enumerate(requested):
        relative = Path(ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"workflow-action planning_artifacts[{index}] escapes artifact_root")
        try:
            unresolved = root / relative
            cursor = root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError("symlink planning artifact")
            resolved = unresolved.resolve()
            resolved.relative_to(root)
            content = resolved.read_bytes()
            text = content.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"workflow planning artifact unreadable: {ref}") from exc
        artifacts.append(PlanningArtifact(kind=kind, ref=ref, text=text))
        scanned.append(
            PlanningArtifactAuthority(
                ref=ref,
                kind=kind,
                work_id=work_id,
                baseline_sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    if authority and tuple(scanned) != authority:
        raise ValueError("workflow planning artifact current authority drift")
    return tuple(artifacts), tuple(scanned)


def _manager_archive_applied(run) -> bool:
    archives = [
        step
        for step in run.steps
        if step.phase == "ship"
        and step.card == "openspec-archive"
        and step.gate_result == "passed"
    ]
    return len(archives) == 1 and (
        archives[0].executor,
        archives[0].model,
        archives[0].domain,
    ) == ("cortex-manager", "deterministic", "cortex")


def _planning_artifact_relative_path_after_archive(
    run,
    *,
    workspace: Path,
    ref: str,
    digest: str,
) -> Path:
    relative = Path(ref)
    cursor = workspace
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("workflow brainstorm artifact symlink rejected")
    direct = workspace / relative
    if direct.is_file() or not _manager_archive_applied(run):
        return relative
    parts = relative.parts
    if (
        len(parts) < 4
        or parts[:2] != ("openspec", "changes")
        or parts[2] not in run.openspec_refs
    ):
        return relative
    archive_root = workspace / "openspec" / "changes" / "archive"
    if archive_root.is_symlink() or not archive_root.is_dir():
        return relative
    suffix = f"-{parts[2]}"
    matches: list[Path] = []
    for archived_change in archive_root.iterdir():
        if archived_change.is_symlink() or not archived_change.name.endswith(suffix):
            continue
        candidate = archived_change.joinpath(*parts[3:])
        cursor = workspace
        for part in candidate.relative_to(workspace).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("workflow brainstorm archived artifact symlink rejected")
        if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
            matches.append(candidate)
    if len(matches) > 1:
        raise ValueError("workflow brainstorm archived artifact authority ambiguous")
    return matches[0].relative_to(workspace) if matches else relative


def _validated_brainstorm_planning_authority(
    run,
    *,
    coordinator_root: str | Path,
    brainstorm_ref: GateEvidenceRef | None = None,
) -> tuple[tuple[PlanningArtifactAuthority, ...], str | None]:
    """Bind published planning artifacts from canonical brainstorm evidence."""
    refs = (
        [brainstorm_ref]
        if brainstorm_ref is not None
        else [ref for ref in run.gate_refs if ref.kind == "brainstorm"]
    )
    if not refs:
        if run.brainstorm_required:
            raise ValueError("workflow brainstorm evidence missing")
        return run.planning_authority, run.planning_source_revision
    if len(refs) != 1 or refs[0] is None:
        raise ValueError("workflow brainstorm authority must be unique")
    gate_ref = refs[0]
    evidence_path = Path(gate_ref.ref)
    evidence_root = Path(coordinator_root).resolve() / "evidence"
    if (
        not evidence_path.is_absolute()
        or evidence_path.is_symlink()
        or not evidence_path.is_file()
    ):
        raise ValueError("workflow brainstorm evidence missing")
    resolved_evidence = evidence_path.resolve()
    try:
        resolved_evidence.relative_to(evidence_root)
    except ValueError as exc:
        raise ValueError("workflow brainstorm evidence outside coordinator root") from exc
    encoded = resolved_evidence.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != gate_ref.sha256:
        raise ValueError("workflow brainstorm evidence hash drift")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("workflow brainstorm evidence invalid") from exc
    scope = payload.get("scope") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("kind") != "brainstorm-peer"
        or not isinstance(scope, dict)
        or set(scope) != {"repo", "work_id", "source_revision"}
        or scope.get("repo") != run.repo
        or scope.get("work_id") != run.work_id
        or not isinstance(scope.get("source_revision"), str)
        or not scope["source_revision"]
        or not isinstance(payload.get("artifacts"), list)
    ):
        raise ValueError("workflow brainstorm evidence binding invalid")
    evidence_source_revision = scope["source_revision"]
    if (
        run.planning_source_revision is not None
        and run.planning_source_revision != evidence_source_revision
    ):
        raise ValueError("workflow brainstorm evidence source revision drift")
    rows = payload["artifacts"]

    declared_patterns = tuple(
        pattern
        for step in run.steps
        if step.persona == "planner" and step.phase in {"define", "plan"}
        for pattern in step.outputs
    )
    persisted = {item.ref: item for item in run.planning_authority}
    scanned: dict[str, PlanningArtifactAuthority] = {}
    workspace = Path(run.workspace_root).resolve()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"kind", "ref", "sha256"}:
            raise ValueError(f"workflow brainstorm artifact[{index}] invalid")
        kind = row.get("kind")
        ref = row.get("ref")
        digest = row.get("sha256")
        if (
            kind not in {"spec", "design", "plan"}
            or not isinstance(ref, str)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or ref in scanned
        ):
            raise ValueError(f"workflow brainstorm artifact[{index}] invalid")
        relative = Path(ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("workflow brainstorm artifact escapes workspace")
        target_relative = _planning_artifact_relative_path_after_archive(
            run,
            workspace=workspace,
            ref=ref,
            digest=digest,
        )
        cursor = workspace
        for part in target_relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("workflow brainstorm artifact symlink rejected")
        target = (workspace / target_relative).resolve()
        try:
            target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("workflow brainstorm artifact escapes workspace") from exc
        if not target.is_file():
            raise ValueError("workflow brainstorm artifact hash drift")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("workflow brainstorm artifact hash drift")
        existing = persisted.get(ref)
        if existing is None:
            if not any(fnmatch.fnmatch(ref, pattern) for pattern in declared_patterns):
                raise ValueError("workflow brainstorm artifact outside planner outputs")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("workflow brainstorm artifact unreadable") from exc
            if not assess_planning_artifact(PlanningArtifact(kind=kind, ref=ref, text=text)).accepted:
                raise ValueError("workflow brainstorm artifact is not accepted")
        elif (
            existing.kind != kind
            or existing.work_id != run.work_id
            or existing.baseline_sha256 != digest
        ):
            raise ValueError("workflow brainstorm artifact differs from persisted authority")
        scanned[ref] = PlanningArtifactAuthority(
            ref=ref,
            kind=kind,
            work_id=run.work_id,
            baseline_sha256=digest,
        )

    if set(persisted) - set(scanned):
        raise ValueError("workflow brainstorm evidence omits persisted authority")
    ordered = list(run.planning_authority)
    ordered.extend(scanned[ref] for ref in scanned if ref not in persisted)
    return tuple(ordered), evidence_source_revision


def _audit_phase_steps(
    steps,
    *,
    phase: str,
    executor: str,
    model: str,
    domain: str,
    outputs: tuple[str, ...],
    gate_result: str = "passed",
    card_id: str | None = None,
):
    from .workflow import WorkflowStep

    return tuple(
        WorkflowStep(
            phase=step.phase,
            persona=step.persona,
            card=step.card,
            executor=executor if step.phase == phase and (card_id is None or step.card == card_id) else step.executor,
            model=model if step.phase == phase and (card_id is None or step.card == card_id) else step.model,
            domain=domain if step.phase == phase and (card_id is None or step.card == card_id) else step.domain,
            inputs=step.inputs,
            outputs=outputs if step.phase == phase and (card_id is None or step.card == card_id) else step.outputs,
            gate_result=gate_result if step.phase == phase and (card_id is None or step.card == card_id) else step.gate_result,
        )
        for step in steps
    )


def _job_for_workflow_card(
    registry,
    *,
    run,
    card_id: str,
    job_id: object,
    expected_persona: str,
    identities: IdentityRegistry,
) -> tuple[dict[str, object], object]:
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("workflow card evidence requires registry job_id")
    job = registry.get_job(job_id)
    expected = {
        "workflow_run_id": run.run_id,
        "workflow_claim_key": run.claim_key,
        "workflow_repo": run.repo,
        "workflow_card": card_id,
        "workflow_phase": run.current_phase,
        "source_revision": run.source_revision,
        "persona": expected_persona,
    }
    for field, value in expected.items():
        if job.get(field) != value:
            raise ValueError(f"workflow job binding mismatch: {field}")
    if job.get("status") != "exited" or job.get("exit_code") != 0:
        raise ValueError("workflow job has no successful terminal result")
    executor = job.get("executor")
    model = job.get("model_id")
    if not isinstance(executor, str) or not isinstance(model, str):
        raise ValueError("workflow job identity missing")
    identity = identities.require(executor, model)
    if job.get("independence_domain") != identity.independence_domain:
        raise ValueError("workflow job identity/domain mismatch")
    return job, identity


def _verify_exact_candidate(job: Mapping[str, object], *, git_runner=None) -> str:
    candidate = job.get("subject_head")
    worktree = (
        job.get("workflow_repo_root")
        if job.get("persona") == "reviewer"
        else job.get("worktree")
    )
    if (
        not isinstance(candidate, str)
        or verification.SAFE_SHA_RE.fullmatch(candidate) is None
        or not isinstance(worktree, str)
    ):
        raise ValueError("workflow job candidate/worktree missing")

    def run_git(argv: list[str]):
        if git_runner is None:
            return subprocess.run(argv, capture_output=True, text=True, check=False)
        try:
            return git_runner(argv, capture_output=True, text=True, check=False)
        except TypeError:
            return git_runner(argv[1:] if argv and argv[0] == "git" else argv)

    exists = run_git(["git", "-C", worktree, "cat-file", "-e", f"{candidate}^{{commit}}"])
    if isinstance(exists, str):
        exists_ok = True
    else:
        exists_ok = getattr(exists, "returncode", 1) == 0
    if not exists_ok:
        raise ValueError("workflow candidate does not exist")
    head = run_git(["git", "-C", worktree, "rev-parse", "HEAD"])
    if isinstance(head, str):
        head_ok = True
        head_text = head
    else:
        head_ok = getattr(head, "returncode", 1) == 0
        head_text = getattr(head, "stdout", "")
    if not head_ok or not isinstance(head_text, str) or head_text.strip().lower() != candidate:
        raise ValueError("workflow candidate is not exact worktree HEAD")
    return candidate


def _verify_build_candidate_transition(
    job: Mapping[str, object],
    *,
    previous_candidate: object,
    git_runner=None,
) -> str:
    """Accept an exact build HEAD only when it monotonically extends its trusted baseline."""

    candidate = _verify_exact_candidate(job, git_runner=git_runner)
    baseline = previous_candidate if previous_candidate is not None else job.get("dispatch_head")
    worktree = job.get("worktree")
    if (
        not isinstance(baseline, str)
        or verification.SAFE_SHA_RE.fullmatch(baseline) is None
        or not isinstance(worktree, str)
    ):
        raise ValueError("workflow build candidate baseline missing")
    if baseline == candidate:
        return candidate

    argv = ["git", "-C", worktree, "merge-base", "--is-ancestor", baseline, candidate]
    if git_runner is None:
        ancestry = subprocess.run(argv, capture_output=True, text=True, check=False)
    else:
        try:
            ancestry = git_runner(argv, capture_output=True, text=True, check=False)
        except TypeError:
            ancestry = git_runner(argv[1:])
    if isinstance(ancestry, str):
        return candidate
    returncode = getattr(ancestry, "returncode", 1)
    if returncode == 1:
        raise ValueError("workflow build candidate is not a descendant")
    if returncode != 0:
        raise ValueError("workflow build candidate ancestry unavailable")
    return candidate


def _review_builder_job_binding(
    registry,
    *,
    run,
    builder_job_id: object,
    candidate: str,
) -> tuple[dict[str, object], bool]:
    if not isinstance(builder_job_id, str) or not builder_job_id:
        raise ValueError("review evaluation builder job missing")
    builder = registry.get_job(builder_job_id)
    archive_author = (
        builder.get("workflow_phase") == "ship"
        and builder.get("workflow_card") == "openspec-archive"
        and builder.get("persona") == "manager"
    )
    expected = {
        "workflow_run_id": run.run_id,
        "workflow_claim_key": run.claim_key,
        "workflow_repo": run.repo,
        "source_revision": run.source_revision,
        "subject_head": candidate,
        "status": "exited",
        "exit_code": 0,
    }
    expected.update(
        {
            "workflow_phase": "ship" if archive_author else "build",
            "persona": "manager" if archive_author else "builder",
        }
    )
    for field, value in expected.items():
        if builder.get(field) != value:
            raise ValueError(f"review evaluation builder binding mismatch: {field}")
    card = builder.get("workflow_card")
    if not isinstance(card, str) or not any(
        step.card == card
        and step.gate_result == "passed"
        and (
            (step.phase == "build" and not archive_author)
            or (step.phase == "ship" and archive_author)
        )
        for step in run.steps
    ):
        raise ValueError("review evaluation builder card is not passed")
    return builder, archive_author


def _review_builder_job(
    registry,
    *,
    run,
    builder_job_id: object,
    candidate: str,
    identities: IdentityRegistry,
) -> tuple[dict[str, object], object]:
    builder, archive_author = _review_builder_job_binding(
        registry,
        run=run,
        builder_job_id=builder_job_id,
        candidate=candidate,
    )
    executor = builder.get("executor")
    model = builder.get("model_id")
    if not isinstance(executor, str) or not isinstance(model, str):
        raise ValueError("review evaluation builder identity missing")
    identity = (
        ModelIdentity(
            executor=executor,
            model_id=model,
            independence_domain=str(builder.get("independence_domain")),
        )
        if archive_author
        else identities.require(executor, model)
    )
    if builder.get("independence_domain") != identity.independence_domain:
        raise ValueError("review evaluation builder identity/domain mismatch")
    return builder, identity


def _extract_terminal_json(log_path: object) -> dict[str, object]:
    if not isinstance(log_path, str) or not log_path:
        raise ValueError("workflow terminal log missing")
    try:
        content = Path(log_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("workflow terminal log unreadable") from exc
    lines = content.splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        nested = value.get("workflow_evidence")
        if _is_workflow_terminal_payload(nested):
            return nested
        item = value.get("item")
        if (
            value.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
        ):
            parsed = _parse_terminal_json_text(item.get("text"))
            if parsed is not None:
                return parsed
        data = value.get("data")
        if value.get("type") == "assistant.message" and isinstance(data, dict):
            parsed = _parse_terminal_json_text(data.get("content"))
            if parsed is not None:
                return parsed
        for key in ("result", "content", "message", "text"):
            parsed = _parse_terminal_json_text(value.get(key))
            if parsed is not None:
                return parsed
        if _is_workflow_terminal_payload(value):
            return value
    fenced = re.fullmatch(r"```json\r?\n(?P<body>[\s\S]+)\r?\n```\r?\n?", content)
    if fenced is not None:
        parsed = _parse_terminal_json_text(fenced.group("body"))
        if parsed is not None:
            return parsed
    raise ValueError("workflow terminal log has no JSON evidence")


def _parse_terminal_json_text(value: object) -> dict[str, object] | None:
    if not isinstance(value, str):
        return None
    fenced = re.fullmatch(r"```json\r?\n(?P<body>[\s\S]+)\r?\n```", value)
    if fenced is not None:
        value = fenced.group("body")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if _is_workflow_terminal_payload(parsed) else None


def _is_workflow_terminal_payload(value: object) -> bool:
    if isinstance(value, dict) and value.get("kind") in {
        "workflow-verification-result",
        "workflow-review-result",
    }:
        return type(value.get("schema_version")) is int and isinstance(value.get("reports"), list)
    return (
        isinstance(value, dict)
        and type(value.get("schema_version")) is int
        and ("status" in value or "state" in value)
        and "candidate" in value
        and "outputs" in value
        and (value.get("kind") == "workflow-card" or "slice_id" in value)
    )


def _retryable_nonpassing_workflow_terminal(job: Mapping[str, object]) -> bool:
    """Recognize an immutable, bound card terminal that explicitly requested a stop."""

    if (
        job.get("workflow_evidence") is not None
        or job.get("status") != "exited"
        or type(job.get("exit_code")) is not int
        or job.get("exit_code") != 0
        or job.get("workflow_phase") not in {"plan", "build"}
    ):
        return False
    try:
        raw = _extract_terminal_json(job.get("log_path"))
    except ValueError:
        return False
    required = {
        "schema_version", "kind", "status", "run_id", "card_id", "candidate", "outputs",
    }
    phase = job.get("workflow_phase")
    candidate = raw.get("candidate")
    outputs = raw.get("outputs")
    return (
        set(raw) == required
        and type(raw.get("schema_version")) is int
        and raw.get("schema_version") == 1
        and raw.get("kind") == "workflow-card"
        and raw.get("status") in {"failed", "needs_human"}
        and raw.get("run_id") == job.get("workflow_run_id")
        and raw.get("card_id") == job.get("workflow_card")
        and (
            (phase == "plan" and candidate is None)
            or (
                phase == "build"
                and isinstance(candidate, str)
                and verification.SAFE_SHA_RE.fullmatch(candidate) is not None
            )
        )
        and isinstance(outputs, list)
        and all(isinstance(ref, str) for ref in outputs)
    )


def _workflow_review_evidence_state(
    job: Mapping[str, object],
    *,
    run,
    coordinator_root: str | Path,
) -> str | None:
    """Return the exact immutable review state eligible for operator recovery."""

    card = job.get("workflow_card")
    if (
        job.get("workflow_run_id") != run.run_id
        or job.get("workflow_claim_key") != run.claim_key
        or job.get("workflow_repo") != run.repo
        or job.get("source_revision") != run.source_revision
        or not isinstance(card, str)
        or not any(
            step.phase == "review"
            and step.card == card
            and step.gate_result != "passed"
            for step in run.steps
        )
        or job.get("workflow_phase") != "review"
        or job.get("persona") != "reviewer"
        or job.get("kind") != "review"
        or job.get("subject_head") != run.candidate_head
        or job.get("workflow_evidence") is None
        or job.get("status") != "exited"
        or job.get("exit_code") != 0
    ):
        return None
    try:
        evidence, _outputs, _path, _digest = _read_job_workflow_evidence(
            job,
            run=run,
            coordinator_root=coordinator_root,
        )
        payload = dict(evidence)
        payload.pop("outputs", None)
        evaluation = foreign_review.validate_gate_evaluation(payload)
    except (OSError, ValueError):
        return None
    state = evaluation.get("state")
    if (
        state not in {"passed", "rejected"}
        or evaluation.get("slice_id") != f"{run.run_id}-{card}"
        or evaluation.get("candidate") != run.candidate_head
        or evaluation.get("reviewer_job_id") != job.get("job_id")
    ):
        return None
    return str(state)


def _is_rejected_workflow_review_evidence(
    job: Mapping[str, object],
    *,
    run,
    coordinator_root: str | Path,
) -> bool:
    """Recognize an exact immutable rejected review for explicit fresh review only."""

    return _workflow_review_evidence_state(
        job,
        run=run,
        coordinator_root=coordinator_root,
    ) == "rejected"


def _is_exact_legacy_agy_recovery(
    job: Mapping[str, object],
    *,
    run,
    step,
    identities: IdentityRegistry,
) -> bool:
    """Classify the one legacy planning-only Agy reviewer terminal eligible for operator recovery."""

    if (
        job.get("workflow_evidence") is not None
        or job.get("status") != "exited"
        or type(job.get("exit_code")) is not int
        or job.get("exit_code") != 0
        or step.persona != "reviewer"
        or step.phase not in {"verify", "review"}
        or job.get("persona") != "reviewer"
        or job.get("kind") != "review"
        or job.get("workflow_run_id") != run.run_id
        or job.get("workflow_claim_key") != run.claim_key
        or job.get("workflow_repo") != run.repo
        or job.get("source_revision") != run.source_revision
        or job.get("workflow_card") != step.card
        or job.get("workflow_phase") != step.phase
        or job.get("subject_head") != run.candidate_head
        or job.get("executor") != "agy"
        or job.get("model_id") != AGY_MODEL_ID
        or job.get("independence_domain") != AGY_DOMAIN
    ):
        return False
    worktree = job.get("worktree")
    repo_root = job.get("workflow_repo_root")
    input_root = job.get("workflow_input_root")
    if (
        not isinstance(worktree, str)
        or not Path(worktree).is_absolute()
        or Path(worktree).resolve(strict=False) != Path(worktree)
        or repo_root != worktree
        or input_root != worktree
    ):
        return False
    identity = identities.get("agy", AGY_MODEL_ID)
    if (
        identity is None
        or identity.independence_domain != AGY_DOMAIN
        or identity.capabilities != ("planning",)
        or identity.live_probe != AGY_LIVE_PROBE
    ):
        return False
    try:
        raw = _extract_terminal_json(job.get("log_path"))
    except ValueError:
        return False
    required = {
        "schema_version", "kind", "status", "run_id", "card_id", "candidate", "outputs",
    }
    outputs = raw.get("outputs")
    declared_outputs = job.get("workflow_outputs")
    return (
        set(raw) == required
        and raw.get("schema_version") == 1
        and raw.get("kind") == "workflow-card"
        and raw.get("status") == "passed"
        and raw.get("run_id") == run.run_id
        and raw.get("card_id") == step.card
        and raw.get("candidate") == run.candidate_head
        and isinstance(run.candidate_head, str)
        and verification.SAFE_SHA_RE.fullmatch(run.candidate_head) is not None
        and isinstance(outputs, list)
        and all(
            isinstance(ref, str)
            and ref
            and not Path(ref).is_absolute()
            and ".." not in Path(ref).parts
            and Path(ref).as_posix() == ref
            for ref in outputs
        )
        and isinstance(declared_outputs, list)
        and declared_outputs == list(step.outputs)
        and all(
            any(fnmatch.fnmatch(ref, pattern) for pattern in declared_outputs)
            for ref in outputs
        )
        and all(
            any(fnmatch.fnmatch(ref, pattern) for ref in outputs)
            for pattern in declared_outputs
        )
    )


def _is_exact_reviewer_terminal_recovery(
    registry,
    job: Mapping[str, object],
    *,
    run,
    step,
    identities: IdentityRegistry,
    coordinator_root: str | Path,
) -> bool:
    """Classify an exact reviewer with no payload for explicit operator retry only."""

    repo_root_value = job.get("workflow_repo_root")
    if (
        job.get("workflow_evidence") is not None
        or job.get("status") != "exited"
        or type(job.get("exit_code")) is not int
        or job.get("exit_code") != 0
        or step.persona != "reviewer"
        or step.phase not in {"verify", "review"}
        or job.get("persona") != "reviewer"
        or job.get("kind") != "review"
        or job.get("workflow_run_id") != run.run_id
        or job.get("workflow_claim_key") != run.claim_key
        or job.get("workflow_repo") != run.repo
        or job.get("source_revision") != run.source_revision
        or job.get("workflow_card") != step.card
        or job.get("workflow_phase") != step.phase
        or job.get("subject_head") != run.candidate_head
        or job.get("workflow_outputs") != list(step.outputs)
        or not isinstance(repo_root_value, str)
    ):
        return False
    executor = job.get("executor")
    model_id = job.get("model_id")
    if not isinstance(executor, str) or not isinstance(model_id, str):
        return False
    identity = identities.get(executor, model_id)
    if (
        identity is None
        or "review" not in identity.capabilities
        or identity.independence_domain != job.get("independence_domain")
    ):
        return False
    try:
        builder, _ = _review_builder_job(
            registry,
            run=run,
            builder_job_id=job.get("workflow_builder_job_id"),
            candidate=str(run.candidate_head),
            identities=identities,
        )
        sandbox = _reviewer_sandbox_path(job, coordinator_root)
        checkout = _reviewer_checkout_path(
            job,
            coordinator_root,
            allow_legacy_claude_layout=True,
        )
    except ValueError:
        return False
    builder_worktree = builder.get("worktree")
    if (
        not isinstance(builder_worktree, str)
        or Path(repo_root_value).resolve() != Path(builder_worktree).resolve()
    ):
        return False
    expected = job.get("workflow_sandbox_hash")
    candidate_root = Path(repo_root_value)
    try:
        candidate_unchanged = (
            isinstance(expected, str)
            and len(expected) == 64
            and not candidate_root.is_symlink()
            and candidate_root.is_dir()
            and planning_runtime._tree_snapshot(candidate_root) == expected
        )
    except (OSError, PermissionError):
        return False
    if (
        not candidate_unchanged
        or sandbox == candidate_root
        or checkout == candidate_root
        or builder.get("independence_domain") == identity.independence_domain
    ):
        return False
    log_value = job.get("log_path")
    job_id = job.get("job_id")
    if not isinstance(log_value, str) or not isinstance(job_id, str):
        return False
    log = Path(log_value)
    expected_log_root = Path(coordinator_root).resolve() / "logs" / "workflow"
    if (
        log.is_symlink()
        or not log.is_file()
        or log.name != f"{job_id}.jsonl"
        or log.resolve().parent != expected_log_root
    ):
        return False
    try:
        log.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    try:
        _extract_terminal_json(str(log))
    except ValueError:
        return True
    return False


def _canonical_workflow_artifacts(
    rows: object,
    *,
    repo_root: Path,
    baseline_by_ref: Mapping[str, str],
) -> list[dict[str, str | None]]:
    if not isinstance(rows, list):
        raise ValueError("workflow terminal outputs must be a list")
    artifacts: list[dict[str, str]] = []
    for ref in rows:
        if not isinstance(ref, str) or not ref:
            raise ValueError("workflow terminal output path invalid")
        relative = Path(ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("workflow terminal output escapes repo")
        unresolved = repo_root / relative
        cursor = repo_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("workflow terminal output symlink rejected")
        resolved = unresolved.resolve()
        resolved.relative_to(repo_root)
        if not resolved.is_file():
            raise ValueError("workflow terminal output missing")
        artifacts.append(
            {
                "path": ref,
                "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
                "baseline_sha256": baseline_by_ref.get(ref),
            }
        )
    return artifacts


def _workflow_output_baseline(repo_root: Path, patterns: tuple[str, ...]) -> tuple[dict[str, str], ...]:
    rows: dict[str, str] = {}
    for pattern in patterns:
        relative_pattern = Path(pattern)
        if relative_pattern.is_absolute() or ".." in relative_pattern.parts:
            raise ValueError("workflow manifest output pattern escapes repo")
        static_parts: list[str] = []
        for part in relative_pattern.parts:
            if any(marker in part for marker in ("*", "?", "[")):
                break
            static_parts.append(part)
        static_root = repo_root.joinpath(*static_parts)
        cursor = repo_root
        for part in static_parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("workflow output baseline symlink rejected")
        if len(static_parts) == len(relative_pattern.parts):
            candidates = (static_root,)
        elif static_root.is_dir():
            candidates = tuple(static_root.rglob("*"))
        else:
            candidates = ()
        for unresolved in candidates:
            relative = unresolved.relative_to(repo_root).as_posix()
            cursor = repo_root
            for part in Path(relative).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError("workflow output baseline symlink rejected")
            resolved = unresolved.resolve()
            resolved.relative_to(repo_root)
            if resolved.is_file():
                rows[relative] = _sha256_path(resolved)
    return tuple({"path": path, "sha256": rows[path]} for path in sorted(rows))


def _effective_workflow_inputs(run, step) -> tuple[str, ...]:
    """Include earlier same-phase inputs so legacy pending cards retain bounded context."""
    patterns: list[str] = []
    for item in run.steps:
        if item.phase == step.phase:
            for pattern in item.inputs:
                if pattern not in patterns:
                    patterns.append(pattern)
        if item is step or item.card == step.card:
            break
    return tuple(patterns)


def _safe_input_matches(root: Path, pattern: str) -> tuple[Path, ...]:
    relative = Path(pattern)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("workflow manifest input pattern escapes repo")
    matches: list[Path] = []
    for unresolved in root.glob(pattern):
        ref = unresolved.relative_to(root)
        cursor = root
        for part in ref.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("workflow input symlink rejected")
        resolved = unresolved.resolve()
        resolved.relative_to(root)
        if resolved.is_file():
            matches.append(resolved)
    return tuple(sorted(matches, key=lambda item: item.relative_to(root).as_posix()))


def _write_workflow_input_content(
    *,
    coordinator_root: Path,
    run,
    ref: str,
    digest: str,
    content: str,
) -> str:
    envelope = {
        "schema_version": 1,
        "kind": "workflow-input-content",
        "run_id": run.run_id,
        "work_id": run.work_id,
        "repo": run.repo,
        "source_revision": run.source_revision,
        "path": ref,
        "sha256": digest,
        "content": content,
    }
    encoded = (
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    locator_digest = hashlib.sha256(encoded).hexdigest()
    path = coordinator_root.resolve() / "evidence" / "workflow-inputs" / f"{locator_digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != encoded or path.stat().st_mode & 0o222:
            raise ValueError("workflow input content-address conflict")
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o444)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return str(path)


def _workflow_input_snapshot(
    *,
    run,
    repo_root: Path,
    patterns: tuple[str, ...],
    coordinator_root: str | Path,
) -> tuple[dict[str, str], ...]:
    root = repo_root.resolve()
    operator_root = Path(run.workspace_root).resolve()
    authority = {item.ref: item for item in run.planning_authority}
    seeds: dict[str, bytes] = {}

    for pattern in patterns:
        if _safe_input_matches(root, pattern):
            continue
        authority_refs = sorted(ref for ref in authority if fnmatch.fnmatch(ref, pattern))
        if not authority_refs:
            raise ValueError(f"workflow declared input missing: {pattern}")
        for ref in authority_refs:
            source_matches = _safe_input_matches(operator_root, ref)
            if len(source_matches) != 1:
                raise ValueError("workflow planning input missing")
            source = source_matches[0]
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != authority[ref].baseline_sha256:
                raise ValueError("workflow planning input drift")
            seeds[ref] = data

    for ref, data in seeds.items():
        destination = root / ref
        parent = root
        for part in Path(ref).parent.parts:
            child = parent / part
            if child.is_symlink():
                raise ValueError("workflow input seed parent symlink rejected")
            child.mkdir(exist_ok=True)
            if child.is_symlink() or not child.is_dir():
                raise ValueError("workflow input seed parent invalid")
            child.resolve().relative_to(root)
            parent = child
        if destination.is_symlink():
            raise ValueError("workflow input seed symlink rejected")
        if destination.exists():
            if not destination.is_file() or destination.read_bytes() != data:
                raise ValueError("workflow input seed conflict")
            continue
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    rows: list[dict[str, str]] = []
    total_bytes = 0
    for pattern in patterns:
        matches = _safe_input_matches(root, pattern)
        if not matches:
            raise ValueError(f"workflow declared input missing: {pattern}")
        for resolved in matches:
            ref = resolved.relative_to(root).as_posix()
            data = resolved.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            bound = authority.get(ref)
            if bound is not None and digest != bound.baseline_sha256:
                raise ValueError("workflow planning input drift")
            pattern_has_authority = any(
                fnmatch.fnmatch(candidate_ref, pattern) for candidate_ref in authority
            )
            if pattern_has_authority and bound is None:
                raise ValueError("workflow planning input lacks authority")
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("workflow input must be UTF-8") from exc
            total_bytes += len(data)
            if total_bytes > 131072:
                raise ValueError("workflow input envelope exceeds bound")
            content_ref = _write_workflow_input_content(
                coordinator_root=Path(coordinator_root),
                run=run,
                ref=ref,
                digest=digest,
                content=content,
            )
            rows.append(
                {
                    "pattern": pattern,
                    "path": ref,
                    "sha256": digest,
                    "authority": "planning-authority" if bound is not None else "worktree",
                    "content_ref": content_ref,
                }
            )
    return tuple(rows)


def _read_workflow_input_content(
    row: Mapping[str, object],
    *,
    run=None,
    coordinator_root: str | Path | None = None,
) -> dict[str, object]:
    raw_ref = row.get("content_ref")
    if not isinstance(raw_ref, str):
        raise ValueError("workflow input content reference invalid")
    content_path = Path(raw_ref)
    if not content_path.is_absolute() or content_path.is_symlink() or not content_path.is_file():
        raise ValueError("workflow input content reference missing")
    if content_path.stat().st_mode & 0o222:
        raise ValueError("workflow input content reference mutable")
    resolved = content_path.resolve()
    if coordinator_root is not None:
        expected_root = Path(coordinator_root).resolve() / "evidence" / "workflow-inputs"
        if resolved.parent != expected_root:
            raise ValueError("workflow input content reference outside evidence root")
    encoded = resolved.read_bytes()
    if resolved.suffix != ".json" or hashlib.sha256(encoded).hexdigest() != resolved.stem:
        raise ValueError("workflow input content locator drift")
    try:
        envelope = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("workflow input content reference invalid") from exc
    required = {
        "schema_version", "kind", "run_id", "work_id", "repo", "source_revision",
        "path", "sha256", "content",
    }
    if (
        not isinstance(envelope, dict)
        or set(envelope) != required
        or envelope.get("schema_version") != 1
        or envelope.get("kind") != "workflow-input-content"
        or envelope.get("path") != row.get("path")
        or envelope.get("sha256") != row.get("sha256")
        or not isinstance(envelope.get("content"), str)
        or hashlib.sha256(envelope["content"].encode("utf-8")).hexdigest() != row.get("sha256")
    ):
        raise ValueError("workflow input content reference drift")
    if run is not None and (
        envelope.get("run_id") != run.run_id
        or envelope.get("work_id") != run.work_id
        or envelope.get("repo") != run.repo
        or envelope.get("source_revision") != run.source_revision
    ):
        raise ValueError("workflow input content authority drift")
    return envelope


def _validate_workflow_input_snapshot(
    repo_root: Path,
    rows: object,
    *,
    coordinator_root: str | Path | None = None,
) -> None:
    if not isinstance(rows, list):
        raise ValueError("workflow input snapshot missing")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "pattern", "path", "sha256", "authority", "content_ref"
        }:
            raise ValueError("workflow input snapshot invalid")
        ref = Path(str(row["path"]))
        if ref.is_absolute() or ".." in ref.parts:
            raise ValueError("workflow input snapshot path invalid")
        target = repo_root / ref
        if target.is_symlink() or not target.is_file():
            raise ValueError("workflow input snapshot file missing")
        if hashlib.sha256(target.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("workflow input snapshot hash drift")
        _read_workflow_input_content(row, coordinator_root=coordinator_root)


def _report_binding(content: bytes) -> Mapping[str, object]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("workflow report must be UTF-8") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("workflow report binding frontmatter missing")
    try:
        closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("workflow report binding frontmatter missing") from exc
    try:
        payload = safe_load("\n".join(lines[1:closing]))
    except YAMLError as exc:
        raise ValueError("workflow report binding frontmatter invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("workflow report binding frontmatter invalid")
    return payload


def _inline_terminal_reports(
    value: object,
    *,
    phase: str,
    declared_outputs: list[str],
) -> tuple[tuple[str, str], ...]:
    governed_root = {
        "verify": ("reports", "verify"),
        "review": ("reports", "review"),
    }.get(phase)
    if governed_root is None:
        raise ValueError("workflow terminal report phase invalid")
    for pattern in declared_outputs:
        relative_pattern = Path(pattern)
        if (
            relative_pattern.is_absolute()
            or ".." in relative_pattern.parts
            or relative_pattern.parts[:2] != governed_root
            or relative_pattern.suffix != ".md"
        ):
            raise ValueError("workflow terminal report manifest root invalid")
    if not isinstance(value, list) or not value:
        raise ValueError("workflow terminal reports must be a non-empty list")
    reports: list[tuple[str, str]] = []
    refs: set[str] = set()
    total = 0
    for index, row in enumerate(value):
        if not isinstance(row, dict) or set(row) != {"path", "body"}:
            raise ValueError(f"workflow terminal reports[{index}] schema invalid")
        ref = row.get("path")
        body = row.get("body")
        relative = Path(ref) if isinstance(ref, str) else Path()
        if (
            not isinstance(ref, str)
            or not ref
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != ref
            or relative.parts[:2] != governed_root
            or relative.suffix != ".md"
            or ref in refs
            or not isinstance(body, str)
            or not body.strip()
        ):
            raise ValueError(f"workflow terminal reports[{index}] invalid")
        encoded = body.encode("utf-8")
        total += len(encoded)
        if total > WORKFLOW_REPORT_MAX_BYTES:
            raise ValueError("workflow terminal report content exceeds bound")
        refs.add(ref)
        reports.append((ref, body))
    if any(
        not any(fnmatch.fnmatch(ref, pattern) for pattern in declared_outputs)
        for ref, _body in reports
    ):
        raise ValueError("workflow terminal report is outside manifest refs")
    if any(
        not any(fnmatch.fnmatch(ref, pattern) for ref, _body in reports)
        for pattern in declared_outputs
    ):
        raise ValueError("workflow terminal report is incomplete for manifest refs")
    return tuple(reports)


def _manager_report_content(
    *,
    job: Mapping[str, object],
    candidate: str,
    body: str,
) -> bytes:
    binding = {
        "workflow_run_id": job.get("workflow_run_id"),
        "workflow_card_id": job.get("workflow_card"),
        "workflow_job_id": job.get("job_id"),
        "candidate": candidate,
    }
    frontmatter = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in binding.items()
    )
    normalized_body = body.rstrip() + "\n"
    return f"---\n{frontmatter}\n---\n{normalized_body}".encode("utf-8")


class WorkflowReportPublicationDrift(RuntimeError):
    """A report publication journal cannot be safely committed or rolled back."""


class _WorkflowReportPublicationTransaction:
    """Crash-consistent report publication around canonical evidence binding."""

    def __init__(
        self,
        *,
        repo_root: Path,
        coordinator_root: Path,
        job_id: str,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.coordinator_root = coordinator_root.resolve()
        self.job_id = job_id
        name = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        self.journal_path = self.coordinator_root / "workflow-report-transactions" / f"{name}.json"
        self.operations: list[dict[str, object]] = []
        self.expected_evidence: dict[str, str] | None = None

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "workflow-report-publication-intent",
            "job_id": self.job_id,
            "repo_root": str(self.repo_root),
            "operations": self.operations,
            "expected_evidence": self.expected_evidence,
        }

    def _persist(self) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        content = (json.dumps(self._payload(), ensure_ascii=False, sort_keys=True) + "\n").encode()
        fd, tmp_name = tempfile.mkstemp(dir=self.journal_path.parent, suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.journal_path)
            directory_fd = os.open(self.journal_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _content(operation: Mapping[str, object], field: str) -> bytes:
        encoded = operation.get(field)
        if not isinstance(encoded, str):
            raise WorkflowReportPublicationDrift("workflow report transaction content invalid")
        try:
            return base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError) as exc:
            raise WorkflowReportPublicationDrift(
                "workflow report transaction content invalid"
            ) from exc

    @staticmethod
    def _guard_path(path: Path, root: Path) -> None:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise WorkflowReportPublicationDrift(
                "workflow report transaction path escapes repo"
            ) from exc
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise WorkflowReportPublicationDrift(
                    "workflow report transaction symlink rejected"
                )

    def publish(
        self,
        reports: tuple[tuple[str, str], ...],
        *,
        job: Mapping[str, object],
        candidate: str,
    ) -> None:
        baseline_rows = job.get("workflow_output_baseline")
        if not isinstance(baseline_rows, list):
            raise ValueError("workflow job output baseline missing")
        baseline_by_ref = {
            str(row["path"]): str(row["sha256"])
            for row in baseline_rows
            if isinstance(row, dict)
            and set(row) == {"path", "sha256"}
            and isinstance(row.get("path"), str)
            and isinstance(row.get("sha256"), str)
        }
        if len(baseline_by_ref) != len(baseline_rows):
            raise ValueError("workflow job output baseline invalid")
        operations: list[dict[str, object]] = []
        for ref, body in reports:
            path = self.repo_root / ref
            self._guard_path(path, self.repo_root)
            existed = path.is_file()
            before = path.read_bytes() if existed else None
            if before is not None and len(before) > WORKFLOW_REPORT_MAX_BYTES:
                raise ValueError(f"workflow report baseline exceeds bound: {ref}")
            before_hash = hashlib.sha256(before).hexdigest() if before is not None else None
            baseline_hash = baseline_by_ref.get(ref)
            if (baseline_hash is None and existed) or (
                baseline_hash is not None and before_hash != baseline_hash
            ):
                raise ValueError(f"workflow report baseline CAS conflict: {ref}")
            after = _manager_report_content(job=job, candidate=candidate, body=body)
            operations.append(
                {
                    "path": str(path),
                    "before_exists": existed,
                    "before_hash": before_hash,
                    "before_content": base64.b64encode(before).decode("ascii") if before is not None else None,
                    "before_mode": path.stat().st_mode & 0o7777 if existed else None,
                    "after_hash": hashlib.sha256(after).hexdigest(),
                    "after_content": base64.b64encode(after).decode("ascii"),
                    "after_mode": 0o644,
                }
            )
        self.operations = operations
        self._persist()
        try:
            self._apply(forward=True)
        except BaseException:
            self.rollback()
            raise

    def bind_expected_evidence(self, locator: Mapping[str, object]) -> None:
        if (
            set(locator) != {"kind", "path", "hash"}
            or not all(isinstance(locator.get(key), str) for key in ("kind", "path", "hash"))
        ):
            raise ValueError("workflow report expected evidence invalid")
        self.expected_evidence = {key: str(locator[key]) for key in ("kind", "path", "hash")}
        self._persist()

    def _apply(self, *, forward: bool) -> None:
        rows = self.operations if forward else list(reversed(self.operations))
        for operation in rows:
            path = Path(str(operation["path"]))
            self._guard_path(path, self.repo_root)
            current_hash = _sha256_path(path) if path.is_file() else None
            before_hash = operation.get("before_hash")
            after_hash = operation.get("after_hash")
            wanted_hash = after_hash if forward else before_hash
            tolerated_hash = before_hash if forward else after_hash
            if current_hash == wanted_hash:
                continue
            if current_hash != tolerated_hash:
                raise WorkflowReportPublicationDrift(
                    f"workflow report publication drift: {path}"
                )
            if not forward and not bool(operation["before_exists"]):
                path.unlink(missing_ok=True)
                if path.parent.exists():
                    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                continue
            content_field = "after_content" if forward else "before_content"
            mode_field = "after_mode" if forward else "before_mode"
            content = self._content(operation, content_field)
            mode = operation.get(mode_field)
            if not isinstance(mode, int):
                raise WorkflowReportPublicationDrift(
                    "workflow report transaction mode invalid"
                )
            _PlanningPublicationTransaction._write_atomic(
                path,
                content,
                mode,
                expect_absent=current_hash is None,
                expected_hash=current_hash,
            )

    def rollback(self) -> None:
        if not self.journal_path.exists():
            return
        self._apply(forward=False)
        self.commit()

    def commit(self) -> None:
        self.journal_path.unlink(missing_ok=True)
        if self.journal_path.parent.exists():
            directory_fd = os.open(self.journal_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    @classmethod
    def reconcile(
        cls,
        *,
        registry,
        job: Mapping[str, object],
        coordinator_root: Path,
    ) -> None:
        repo_root_value = job.get("workflow_repo_root")
        job_id = job.get("job_id")
        if not isinstance(repo_root_value, str) or not isinstance(job_id, str):
            return
        transaction = cls(
            repo_root=Path(repo_root_value),
            coordinator_root=coordinator_root,
            job_id=job_id,
        )
        path = transaction.journal_path
        if path.is_symlink():
            raise WorkflowReportPublicationDrift("workflow report transaction symlink rejected")
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowReportPublicationDrift(
                "workflow report transaction unreadable"
            ) from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {
                "schema_version", "kind", "job_id", "repo_root", "operations", "expected_evidence",
            }
            or payload.get("schema_version") != 1
            or payload.get("kind") != "workflow-report-publication-intent"
            or payload.get("job_id") != job_id
            or payload.get("repo_root") != str(Path(repo_root_value).resolve())
            or not isinstance(payload.get("operations"), list)
        ):
            raise WorkflowReportPublicationDrift("workflow report transaction invalid")
        phase_root = {
            "verify": ("reports", "verify"),
            "review": ("reports", "review"),
        }.get(job.get("workflow_phase"))
        required_operation = {
            "path", "before_exists", "before_hash", "before_content", "before_mode",
            "after_hash", "after_content", "after_mode",
        }
        operations: list[dict[str, object]] = []
        operation_paths: set[Path] = set()
        for row in payload["operations"]:
            if not isinstance(row, dict) or set(row) != required_operation or phase_root is None:
                raise WorkflowReportPublicationDrift(
                    "workflow report transaction operation invalid"
                )
            operation = dict(row)
            operation_path = Path(str(operation["path"]))
            if (
                not operation_path.is_absolute()
                or operation_path != operation_path.resolve(strict=False)
            ):
                raise WorkflowReportPublicationDrift(
                    "workflow report transaction operation invalid"
                )
            try:
                relative = operation_path.relative_to(transaction.repo_root)
            except ValueError as exc:
                raise WorkflowReportPublicationDrift(
                    "workflow report transaction operation invalid"
                ) from exc
            if (
                ".." in relative.parts
                or relative.parts[:2] != phase_root
                or relative.suffix != ".md"
                or operation_path in operation_paths
            ):
                raise WorkflowReportPublicationDrift(
                    "workflow report transaction operation invalid"
                )
            operation_paths.add(operation_path)
            if not isinstance(operation.get("before_exists"), bool):
                raise WorkflowReportPublicationDrift(
                    "workflow report transaction operation invalid"
                )
            before = operation.get("before_content")
            before_hash = operation.get("before_hash")
            before_mode = operation.get("before_mode")
            if operation["before_exists"]:
                before_bytes = transaction._content(operation, "before_content")
                if (
                    not isinstance(before_hash, str)
                    or hashlib.sha256(before_bytes).hexdigest() != before_hash
                    or not isinstance(before_mode, int)
                ):
                    raise WorkflowReportPublicationDrift(
                        "workflow report transaction baseline invalid"
                    )
            elif any(value is not None for value in (before, before_hash, before_mode)):
                raise WorkflowReportPublicationDrift(
                    "workflow report transaction baseline invalid"
                )
            after_bytes = transaction._content(operation, "after_content")
            if (
                not isinstance(operation.get("after_hash"), str)
                or hashlib.sha256(after_bytes).hexdigest() != operation["after_hash"]
                or not isinstance(operation.get("after_mode"), int)
            ):
                raise WorkflowReportPublicationDrift(
                    "workflow report transaction target invalid"
                )
            operations.append(operation)
        transaction.operations = operations
        expected = payload.get("expected_evidence")
        expected_path = Path(str(expected.get("path"))) if isinstance(expected, dict) else None
        if expected is not None and (
            not isinstance(expected, dict)
            or set(expected) != {"kind", "path", "hash"}
            or expected.get("kind") != job.get("workflow_phase")
            or not isinstance(expected.get("path"), str)
            or expected_path is None
            or expected_path.is_absolute()
            or ".." in expected_path.parts
            or expected_path.as_posix() != expected.get("path")
            or expected_path.parts[:2] != ("evidence", "workflow")
            or not isinstance(expected.get("hash"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(expected["hash"])) is None
        ):
            raise WorkflowReportPublicationDrift(
                "workflow report transaction expected evidence invalid"
            )
        transaction.expected_evidence = dict(expected) if isinstance(expected, dict) else None
        persisted = registry.get_job(job_id).get("workflow_evidence")
        if persisted is not None:
            if persisted != transaction.expected_evidence:
                raise WorkflowReportPublicationDrift(
                    "workflow report transaction evidence binding drift"
                )
            transaction._apply(forward=True)
            transaction.commit()
        else:
            transaction.rollback()


def _persisted_job_identity(job: Mapping[str, object], *, field: str) -> dict[str, str]:
    identity = {
        "executor": job.get("executor"),
        "model_id": job.get("model_id"),
        "independence_domain": job.get("independence_domain"),
    }
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise ValueError(f"workflow {field} identity missing")
    return {key: str(value) for key, value in identity.items()}


def _validate_terminal_reports(
    refs: list[str],
    *,
    repo_root: Path,
    job: Mapping[str, object],
    candidate: str | None,
) -> None:
    if job.get("workflow_phase") not in {"verify", "review"}:
        return
    baseline = {
        row["path"]: row["sha256"]
        for row in job.get("workflow_output_baseline", [])
        if isinstance(row, dict)
    }
    for ref in refs:
        path = (repo_root / ref).resolve()
        content = path.read_bytes()
        current_hash = hashlib.sha256(content).hexdigest()
        if baseline.get(ref) == current_hash:
            raise ValueError(f"workflow stale preexisting report rejected: {ref}")
        binding = _report_binding(content)
        expected = {
            "workflow_run_id": job.get("workflow_run_id"),
            "workflow_card_id": job.get("workflow_card"),
            "workflow_job_id": job.get("job_id"),
            "candidate": candidate,
        }
        if any(binding.get(key) != value for key, value in expected.items()):
            raise ValueError(f"workflow report binding mismatch: {ref}")


def _planner_sandbox_path(job: Mapping[str, object], coordinator_root: str | Path) -> Path:
    raw = job.get("worktree")
    if not isinstance(raw, str) or not raw:
        raise ValueError("planner sandbox path missing")
    path = Path(raw)
    if (
        not path.is_absolute()
        or path.is_symlink()
        or path != path.resolve(strict=False)
    ):
        raise ValueError("planner sandbox path invalid")
    root = Path(coordinator_root).resolve()
    allowed_parents = {
        root / "planning-sandboxes",
        root.parent / f".{root.name}-planning-sandboxes",
    }
    if path.parent not in allowed_parents or re.fullmatch(r"[0-9a-f]{32}", path.name) is None:
        raise ValueError("planner sandbox path outside coordinator boundary")
    return path


def _discard_failed_planner_sandbox(
    job: Mapping[str, object],
    *,
    run_id: str,
    card: str,
    coordinator_root: str | Path,
) -> None:
    if job.get("persona") != "planner" or (
        job.get("status") != "failed"
        and not _retryable_nonpassing_workflow_terminal(job)
    ):
        raise ValueError("planner sandbox retry requires failed planner job")
    path = _planner_sandbox_path(job, coordinator_root)
    expected_name = hashlib.sha256(f"{run_id}:{card}".encode()).hexdigest()[:32]
    if path.name != expected_name:
        raise ValueError("planner sandbox retry identity mismatch")
    if path.exists():
        if not path.is_dir():
            raise ValueError("planner sandbox retry target is not a directory")
        shutil.rmtree(path)
    if path.exists() or path.is_symlink():
        raise ValueError("planner sandbox retry cleanup incomplete")


def _reviewer_sandbox_parent(
    *,
    coordinator_root: str | Path,
    candidate_root: Path,
) -> Path:
    parent = Path(coordinator_root).resolve() / "review-sandboxes"
    try:
        parent.relative_to(candidate_root.resolve())
    except ValueError:
        return parent
    return Path(coordinator_root).resolve().parent / f".{Path(coordinator_root).name}-review-sandboxes"


_CLAUDE_REVIEW_PROTECTED_FILES = (
    ".bash_profile",
    ".bashrc",
    ".claude.json",
    ".gitconfig",
    ".gitmodules",
    ".mcp.json",
    ".profile",
    ".ripgreprc",
    ".zprofile",
    ".zshrc",
)
_CLAUDE_REVIEW_PROTECTED_DIRS = (
    ".claude",
    ".claude/agents",
    ".claude/commands",
    ".claude/skills",
    ".claude/worktrees",
    ".husky",
    ".idea",
    ".vscode",
)


def _prepare_claude_review_sandbox(sandbox: Path) -> None:
    """Create only the bind targets required by Claude's strict Bash sandbox."""

    for ref in _CLAUDE_REVIEW_PROTECTED_DIRS:
        target = sandbox / ref
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError("workflow reviewer protected directory invalid")
        if not target.exists():
            target.mkdir(parents=True)
    for ref in _CLAUDE_REVIEW_PROTECTED_FILES:
        target = sandbox / ref
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError("workflow reviewer protected file invalid")
        if not target.exists():
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.flush()
                os.fsync(handle.fileno())


def _create_reviewer_sandbox(
    *,
    run,
    step,
    executor: str,
    candidate_root: Path,
    coordinator_root: str | Path,
    input_snapshot: tuple[dict[str, str], ...],
) -> tuple[Path, Path]:
    candidate = run.candidate_head
    if not isinstance(candidate, str) or verification.SAFE_SHA_RE.fullmatch(candidate) is None:
        raise ValueError("workflow reviewer candidate invalid")
    parent = _reviewer_sandbox_parent(
        coordinator_root=coordinator_root,
        candidate_root=candidate_root,
    )
    parent.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(f"{run.run_id}:{step.card}:{candidate}".encode()).hexdigest()[:32]
    sandbox = parent / name
    if sandbox.exists() or sandbox.is_symlink():
        raise ValueError("stale reviewer sandbox requires reconciliation")
    checkout_root = sandbox / "candidate" if executor == "claude" else sandbox
    if executor == "claude":
        sandbox.mkdir()
        _prepare_claude_review_sandbox(sandbox)
    clone = subprocess.run(
        [
            "git", "clone", "--quiet", "--no-hardlinks", "--no-local", "--no-checkout",
            str(candidate_root.resolve()), str(checkout_root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if clone.returncode != 0:
        shutil.rmtree(sandbox, ignore_errors=True)
        raise ValueError("workflow reviewer sandbox clone failed")
    checkout = subprocess.run(
        ["git", "-C", str(checkout_root), "checkout", "--quiet", "--detach", candidate],
        capture_output=True,
        text=True,
        check=False,
    )
    if checkout.returncode != 0:
        shutil.rmtree(sandbox, ignore_errors=True)
        raise ValueError("workflow reviewer sandbox checkout failed")
    remove_origin = subprocess.run(
        ["git", "-C", str(checkout_root), "remote", "remove", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    remotes = subprocess.run(
        ["git", "-C", str(checkout_root), "remote"],
        capture_output=True,
        text=True,
        check=False,
    )
    if remove_origin.returncode != 0 or remotes.returncode != 0 or remotes.stdout.strip():
        shutil.rmtree(sandbox, ignore_errors=True)
        raise ValueError("workflow reviewer sandbox remote isolation failed")
    head = subprocess.run(
        ["git", "-C", str(checkout_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode != 0 or head.stdout.strip().lower() != candidate.lower():
        shutil.rmtree(sandbox, ignore_errors=True)
        raise ValueError("workflow reviewer sandbox head mismatch")
    for link in checkout_root.rglob("*"):
        if not link.is_symlink():
            continue
        try:
            link.resolve(strict=False).relative_to(checkout_root.resolve())
        except ValueError as exc:
            shutil.rmtree(sandbox, ignore_errors=True)
            raise ValueError("workflow reviewer sandbox external symlink rejected") from exc
    try:
        for row in input_snapshot:
            envelope = _read_workflow_input_content(
                row,
                run=run,
                coordinator_root=coordinator_root,
            )
            ref = str(envelope["path"])
            target = checkout_root / ref
            target.parent.mkdir(parents=True, exist_ok=True)
            content = str(envelope["content"]).encode("utf-8")
            if target.is_symlink():
                raise ValueError("workflow reviewer input symlink rejected")
            if target.exists() and (not target.is_file() or target.read_bytes() != content):
                raise ValueError("workflow reviewer input seed conflict")
            if not target.exists():
                _PlanningPublicationTransaction._write_atomic(
                    target,
                    content,
                    0o600,
                    expect_absent=True,
                )
    except BaseException:
        shutil.rmtree(sandbox, ignore_errors=True)
        raise
    return sandbox, checkout_root


def _reviewer_sandbox_path(job: Mapping[str, object], coordinator_root: str | Path) -> Path:
    raw = job.get("worktree")
    repo_root = job.get("workflow_repo_root")
    if not isinstance(raw, str) or not isinstance(repo_root, str):
        raise ValueError("reviewer sandbox path missing")
    path = Path(raw)
    allowed = _reviewer_sandbox_parent(
        coordinator_root=coordinator_root,
        candidate_root=Path(repo_root),
    )
    run_id = job.get("workflow_run_id")
    card = job.get("workflow_card")
    candidate = job.get("subject_head")
    if (
        not isinstance(run_id, str)
        or not isinstance(card, str)
        or not isinstance(candidate, str)
        or verification.SAFE_SHA_RE.fullmatch(candidate) is None
    ):
        raise ValueError("reviewer sandbox identity missing")
    expected_name = hashlib.sha256(f"{run_id}:{card}:{candidate}".encode()).hexdigest()[:32]
    if (
        not path.is_absolute()
        or path.is_symlink()
        or path.parent != allowed
        or path.name != expected_name
    ):
        raise ValueError("reviewer sandbox path invalid")
    return path


def _reviewer_checkout_path(
    job: Mapping[str, object],
    coordinator_root: str | Path,
    *,
    allow_legacy_claude_layout: bool = False,
) -> Path:
    """Resolve the exact disposable checkout nested under a reviewer session root."""

    sandbox = _reviewer_sandbox_path(job, coordinator_root)
    input_root = job.get("workflow_input_root")
    executor = job.get("executor")
    if not isinstance(input_root, str) or not isinstance(executor, str):
        raise ValueError("reviewer checkout path missing")
    checkout = Path(input_root)
    expected = sandbox / "candidate" if executor == "claude" else sandbox
    legacy = executor == "claude" and allow_legacy_claude_layout and checkout == sandbox
    if (
        not checkout.is_absolute()
        or checkout.is_symlink()
        or (checkout != expected and not legacy)
    ):
        raise ValueError("reviewer checkout path invalid")
    return checkout


def _discard_reviewer_sandbox(
    job: Mapping[str, object],
    *,
    coordinator_root: str | Path,
    require_candidate_unchanged: bool,
) -> None:
    if job.get("persona") != "reviewer" or not isinstance(job.get("workflow_sandbox_hash"), str):
        return
    repo_root = job.get("workflow_repo_root")
    if not isinstance(repo_root, str):
        raise ValueError("reviewer candidate root missing")
    candidate_root = Path(repo_root).resolve()
    expected = str(job["workflow_sandbox_hash"])
    sandbox = _reviewer_sandbox_path(job, coordinator_root)
    if not sandbox.exists() and not sandbox.is_symlink():
        return
    unchanged = candidate_root.is_dir() and planning_runtime._tree_snapshot(candidate_root) == expected
    shutil.rmtree(sandbox, ignore_errors=True)
    if sandbox.exists() or sandbox.is_symlink():
        raise ValueError("reviewer sandbox cleanup incomplete")
    if require_candidate_unchanged and not unchanged:
        raise ValueError("workflow reviewer modified Candidate checkout")


def terminalize_workflow_job(
    registry,
    *,
    job_id: str,
    coordinator_root: str | Path,
) -> dict[str, object]:
    """Create and atomically bind canonical evidence for one terminal workflow job."""

    job = registry.get_job(job_id)
    _WorkflowReportPublicationTransaction.reconcile(
        registry=registry,
        job=job,
        coordinator_root=Path(coordinator_root),
    )
    job = registry.get_job(job_id)
    sandbox_path: Path | None = None
    if job.get("workflow_evidence") is not None:
        if job.get("persona") == "planner":
            sandbox_path = _planner_sandbox_path(job, coordinator_root)
            shutil.rmtree(sandbox_path, ignore_errors=True)
        elif job.get("persona") == "reviewer":
            _discard_reviewer_sandbox(
                job,
                coordinator_root=coordinator_root,
                require_candidate_unchanged=True,
            )
        return job
    if job.get("persona") == "planner":
        expected_sandbox_hash = job.get("workflow_sandbox_hash")
        if not isinstance(expected_sandbox_hash, str) or len(expected_sandbox_hash) != 64:
            raise ValueError("planner job sandbox baseline missing")
        sandbox_path = _planner_sandbox_path(job, coordinator_root)
        if not sandbox_path.is_dir() or planning_runtime._tree_snapshot(sandbox_path) != expected_sandbox_hash:
            shutil.rmtree(sandbox_path, ignore_errors=True)
            raise ValueError("planner modified disposable read-only sandbox")
    if job.get("status") != "exited" or job.get("exit_code") != 0:
        raise ValueError("workflow job is not successful terminal")
    phase = job.get("workflow_phase")
    if phase not in {"plan", "build", "verify", "review"}:
        raise ValueError("workflow job phase is not terminalizable")
    raw = _extract_terminal_json(job.get("log_path"))
    declared_outputs = job.get("workflow_outputs")
    if not isinstance(declared_outputs, list):
        raise ValueError("workflow job declared outputs missing")
    candidate: str | None = None
    inline_reports: tuple[tuple[str, str], ...] = ()
    if phase in {"plan", "build"}:
        required = {"schema_version", "kind", "status", "run_id", "card_id", "candidate", "outputs"}
        if set(raw) != required or raw.get("schema_version") != 1 or raw.get("kind") != "workflow-card":
            raise ValueError("workflow card terminal evidence schema invalid")
        if raw.get("status") != "passed":
            raise ValueError("workflow card terminal evidence did not pass")
        candidate_value = raw.get("candidate")
        if phase == "build":
            if not isinstance(candidate_value, str) or verification.SAFE_SHA_RE.fullmatch(candidate_value) is None:
                raise ValueError("workflow build candidate invalid")
            candidate = candidate_value.lower()
        elif candidate_value is not None:
            raise ValueError("workflow plan candidate must be null")
        normalized_payload: dict[str, object] = dict(raw)
    elif phase == "verify":
        required = {"schema_version", "kind", "status", "summary", "details", "reports"}
        if (
            set(raw) != required
            or raw.get("schema_version") != 1
            or raw.get("kind") != "workflow-verification-result"
            or raw.get("status") != "verified"
            or not isinstance(raw.get("summary"), str)
            or not str(raw["summary"]).strip()
            or not isinstance(raw.get("details"), dict)
        ):
            raise ValueError("workflow verification terminal schema invalid")
        if not isinstance(job.get("subject_head"), str):
            raise ValueError("workflow verification candidate missing")
        candidate = str(job["subject_head"])
        inline_reports = _inline_terminal_reports(
            raw.get("reports"), phase="verify", declared_outputs=declared_outputs
        )
        normalized_payload = verification.validate_verification_evidence(
            {
                "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
                "slice_id": f"{job['workflow_run_id']}-{job['workflow_card']}",
                "candidate": candidate,
                "status": "verified",
                "summary": str(raw["summary"]).strip(),
                "details": raw["details"],
            }
        )
        normalized_payload["outputs"] = [ref for ref, _body in inline_reports]
    else:
        required = {"schema_version", "kind", "reason", "findings", "reports"}
        if (
            set(raw) != required
            or raw.get("schema_version") != 1
            or raw.get("kind") != "workflow-review-result"
            or not isinstance(raw.get("reason"), str)
            or not str(raw["reason"]).strip()
            or not isinstance(raw.get("findings"), list)
            or not isinstance(job.get("subject_head"), str)
        ):
            raise ValueError("workflow review terminal schema invalid")
        candidate = str(job["subject_head"])
        builder_job_id = job.get("workflow_builder_job_id")
        if not isinstance(builder_job_id, str) or not builder_job_id:
            raise ValueError("workflow review builder job binding missing")
        run_id = job.get("workflow_run_id")
        if not isinstance(run_id, str):
            raise ValueError("workflow review run binding missing")
        run = registry.get_workflow_run(run_id)
        builder_job, _archive_author = _review_builder_job_binding(
            registry,
            run=run,
            builder_job_id=builder_job_id,
            candidate=candidate,
        )
        reviewer_identity = _persisted_job_identity(job, field="reviewer")
        builder_identity = _persisted_job_identity(builder_job, field="builder")
        verdict = foreign_review.validate_review_verdict(
            {
                "schema_version": foreign_review.REVIEW_SCHEMA_VERSION,
                "builder_job_id": builder_job_id,
                "reviewer_job_id": str(job["job_id"]),
                "candidate": candidate,
                "launch_identity": reviewer_identity,
                "findings": raw["findings"],
            },
            builder_job_id=builder_job_id,
            reviewer_job_id=str(job["job_id"]),
            candidate=candidate,
            launch_identity=reviewer_identity,
        )
        inline_reports = _inline_terminal_reports(
            raw.get("reports"), phase="review", declared_outputs=declared_outputs
        )
        normalized_payload = foreign_review.build_gate_evaluation(
            slice_id=f"{job['workflow_run_id']}-{job['workflow_card']}",
            state=str(verdict["state"]),
            reason=str(raw["reason"]).strip(),
            builder_job_id=builder_job_id,
            reviewer_job_id=str(job["job_id"]),
            candidate=candidate,
            launch_identity={"builder": builder_identity, "reviewer": reviewer_identity},
            findings=verdict["findings"],
        )
        normalized_payload = foreign_review.validate_gate_evaluation(normalized_payload)
        normalized_payload["outputs"] = [ref for ref, _body in inline_reports]
    if (
        normalized_payload.get("run_id", job.get("workflow_run_id")) != job.get("workflow_run_id")
        or normalized_payload.get("card_id", job.get("workflow_card")) != job.get("workflow_card")
    ):
        raise ValueError("workflow terminal evidence run/card mismatch")
    output_refs = normalized_payload.get("outputs", [])
    if not isinstance(output_refs, list):
        raise ValueError("workflow terminal outputs invalid")
    if any(
        not isinstance(ref, str)
        or not any(fnmatch.fnmatch(ref, pattern) for pattern in declared_outputs)
        for ref in output_refs
    ):
        raise ValueError("workflow terminal output is outside manifest refs")
    if any(
        not any(fnmatch.fnmatch(str(ref), pattern) for ref in output_refs)
        for pattern in declared_outputs
    ):
        raise ValueError("workflow terminal output is incomplete for manifest refs")
    repo_root_value = job.get("workflow_repo_root")
    if not isinstance(repo_root_value, str) or not repo_root_value:
        raise ValueError("workflow job repo root missing")
    repo_root = Path(repo_root_value).resolve()
    input_root_value = job.get("workflow_input_root") or repo_root_value
    if not isinstance(input_root_value, str) or not input_root_value:
        raise ValueError("workflow job input root missing")
    input_root = Path(input_root_value).resolve()
    input_snapshot = job.get("workflow_input_snapshot", [])
    _validate_workflow_input_snapshot(
        input_root,
        input_snapshot,
        coordinator_root=coordinator_root,
    )
    if job.get("persona") == "reviewer":
        _discard_reviewer_sandbox(
            job,
            coordinator_root=coordinator_root,
            require_candidate_unchanged=True,
        )
    baseline_rows = job.get("workflow_output_baseline")
    if not isinstance(baseline_rows, list):
        raise ValueError("workflow job output baseline missing")
    baseline_by_ref = {
        str(row["path"]): str(row["sha256"])
        for row in baseline_rows
        if isinstance(row, dict)
        and set(row) == {"path", "sha256"}
        and isinstance(row.get("path"), str)
        and isinstance(row.get("sha256"), str)
    }
    if len(baseline_by_ref) != len(baseline_rows):
        raise ValueError("workflow job output baseline invalid")
    report_transaction: _WorkflowReportPublicationTransaction | None = None
    created_evidence = False
    path: Path | None = None
    try:
        if inline_reports:
            if candidate is None:
                raise ValueError("workflow report candidate missing")
            report_transaction = _WorkflowReportPublicationTransaction(
                repo_root=repo_root,
                coordinator_root=Path(coordinator_root),
                job_id=job_id,
            )
            report_transaction.publish(inline_reports, job=job, candidate=candidate)
        _validate_terminal_reports(
            output_refs,
            repo_root=repo_root,
            job=job,
            candidate=candidate,
        )
        artifacts = _canonical_workflow_artifacts(
            normalized_payload.get("outputs", []),
            repo_root=repo_root,
            baseline_by_ref=baseline_by_ref,
        )
        job_binding = {
            "job_id": job["job_id"],
            "run_id": job["workflow_run_id"],
            "claim_key": job["workflow_claim_key"],
            "repo": job["workflow_repo"],
            "source_revision": job["source_revision"],
            "card_id": job["workflow_card"],
            "phase": phase,
            "inputs": job.get("workflow_inputs", []),
            "outputs": declared_outputs,
            "output_baseline": baseline_rows,
        }
        if "workflow_input_snapshot" in job:
            job_binding["input_snapshot"] = input_snapshot
        envelope = {
            "schema_version": 1,
            "kind": str(phase),
            "job": job_binding,
            "payload": normalized_payload,
            "artifacts": artifacts,
        }
        content = (
            json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        root = Path(coordinator_root).resolve()
        relative = Path("evidence") / "workflow" / f"{hashlib.sha256(job_id.encode()).hexdigest()}.json"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        locator = {
            "kind": str(phase),
            "path": relative.as_posix(),
            "hash": hashlib.sha256(content).hexdigest(),
        }
        if report_transaction is not None:
            report_transaction.bind_expected_evidence(locator)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise ValueError("workflow canonical evidence conflict")
        else:
            created_evidence = True
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        bound = registry.bind_workflow_evidence(job_id, locator=locator, subject_head=candidate)
        if report_transaction is not None:
            report_transaction.commit()
    except BaseException:
        persisted = registry.get_job(job_id).get("workflow_evidence")
        if persisted is None:
            if created_evidence and path is not None:
                path.unlink(missing_ok=True)
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            if report_transaction is not None:
                report_transaction.rollback()
        elif report_transaction is not None:
            _WorkflowReportPublicationTransaction.reconcile(
                registry=registry,
                job=registry.get_job(job_id),
                coordinator_root=Path(coordinator_root),
            )
        raise
    if sandbox_path is not None:
        shutil.rmtree(sandbox_path, ignore_errors=True)
    return bound


def _read_job_workflow_evidence(
    job: Mapping[str, object],
    *,
    run,
    coordinator_root: str | Path,
) -> tuple[dict[str, object], tuple[str, ...], str, str]:
    locator = job.get("workflow_evidence")
    if not isinstance(locator, dict) or set(locator) != {"kind", "path", "hash"}:
        raise ValueError("workflow job has no canonical evidence locator")
    relative = Path(str(locator["path"]))
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:2] != ("evidence", "workflow"):
        raise ValueError("workflow canonical evidence path invalid")
    root = Path(coordinator_root).resolve()
    unresolved = root / relative
    if unresolved.is_symlink():
        raise ValueError("workflow canonical evidence symlink rejected")
    path = unresolved.resolve()
    path.relative_to(root)
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != locator["hash"]:
        raise ValueError("workflow canonical evidence hash mismatch")
    payload = json.loads(content.decode("utf-8"))
    expected_job = {
        "job_id": job["job_id"],
        "run_id": run.run_id,
        "claim_key": run.claim_key,
        "repo": run.repo,
        "source_revision": job["source_revision"],
        "card_id": job["workflow_card"],
        "phase": job["workflow_phase"],
        "inputs": job.get("workflow_inputs", []),
        "outputs": job.get("workflow_outputs", []),
        "output_baseline": job.get("workflow_output_baseline", []),
    }
    payload_job = payload.get("job") if isinstance(payload, dict) else None
    if job.get("workflow_input_snapshot") or (
        isinstance(payload_job, dict) and "input_snapshot" in payload_job
    ):
        expected_job["input_snapshot"] = job.get("workflow_input_snapshot", [])
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("kind") != locator["kind"]
        or payload.get("job") != expected_job
        or not isinstance(payload.get("payload"), dict)
        or not isinstance(payload.get("artifacts"), list)
    ):
        raise ValueError("workflow canonical evidence binding invalid")
    repo_root_value = job.get("workflow_repo_root")
    if not isinstance(repo_root_value, str) or not repo_root_value:
        raise ValueError("workflow evidence repo root missing")
    repo_root = Path(repo_root_value).resolve()
    refs: list[str] = []
    for row in payload["artifacts"]:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "baseline_sha256"}:
            raise ValueError("workflow canonical artifact locator invalid")
        ref = row.get("path")
        expected_hash = row.get("sha256")
        baseline_hash = row.get("baseline_sha256")
        if (
            not isinstance(ref, str)
            or not isinstance(expected_hash, str)
            or baseline_hash is not None and not isinstance(baseline_hash, str)
        ):
            raise ValueError("workflow canonical artifact locator invalid")
        expected_baseline = {
            str(item["path"]): str(item["sha256"])
            for item in job.get("workflow_output_baseline", [])
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("sha256"), str)
        }.get(ref)
        if baseline_hash != expected_baseline:
            raise ValueError("workflow canonical artifact baseline mismatch")
        relative_artifact = Path(ref)
        if relative_artifact.is_absolute() or ".." in relative_artifact.parts:
            raise ValueError("workflow canonical artifact escapes repo")
        unresolved_artifact = repo_root / relative_artifact
        cursor = repo_root
        for part in relative_artifact.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("workflow canonical artifact symlink rejected")
        artifact_path = unresolved_artifact.resolve()
        artifact_path.relative_to(repo_root)
        artifact_present = artifact_path.is_file()
        artifact_bytes = None
        if artifact_present:
            try:
                artifact_bytes = artifact_path.read_bytes()
            except FileNotFoundError:
                artifact_present = False
        if not artifact_present:
            if not _workflow_report_cleanup_allows_missing(
                coordinator_root=root,
                run=run,
                ref=ref,
                expected_hash=expected_hash,
            ):
                raise ValueError("workflow canonical artifact drift")
        elif hashlib.sha256(artifact_bytes).hexdigest() != expected_hash:
            raise ValueError("workflow canonical artifact drift")
        refs.append(ref)
    return payload["payload"], tuple(refs), str(path), digest


def _workflow_report_cleanup_allows_missing(
    *,
    coordinator_root: Path,
    run,
    ref: str,
    expected_hash: str,
) -> bool:
    directory = coordinator_root / "evidence" / "report-cleanup"
    if directory.is_symlink() or not directory.is_dir():
        return False
    matched = False
    try:
        markers = directory.iterdir()
        for count, marker in enumerate(markers, start=1):
            if count > 2048:
                return False
            try:
                invalid_marker = (
                    marker.is_symlink()
                    or not marker.is_file()
                    or marker.stat().st_mode & 0o222
                    or re.fullmatch(r"[0-9a-f]{64}\.json", marker.name) is None
                )
            except OSError:
                continue
            if invalid_marker:
                continue
            envelope = json.loads(marker.read_text(encoding="utf-8"))
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
            reports = payload.get("reports") if isinstance(payload, dict) else None
            digest = marker.stem
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"payload", "hash"}
                or not isinstance(payload, dict)
                or envelope.get("hash") != digest
                or verification.canonical_json_hash(payload) != digest
                or set(payload) != {"schema", "run_id", "candidate", "reports"}
                or payload.get("schema") != "cortex-workflow-report-cleanup/v1"
                or payload.get("run_id") != run.run_id
                or payload.get("candidate") != run.candidate_head
                or not isinstance(reports, list)
                or not reports
            ):
                continue
            normalized: dict[str, str] = {}
            valid = True
            for row in reports:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"path", "sha256"}
                    or not isinstance(row.get("path"), str)
                    or Path(str(row["path"])).is_absolute()
                    or ".." in Path(str(row["path"])).parts
                    or Path(str(row["path"])).as_posix() != str(row["path"])
                    or not str(row["path"]).startswith(("reports/verify/", "reports/review/"))
                    or not isinstance(row.get("sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"])) is None
                    or str(row["path"]) in normalized
                ):
                    valid = False
                    break
                normalized[str(row["path"])] = str(row["sha256"])
            if valid and normalized.get(ref) == expected_hash:
                matched = True
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return matched


def _validated_ship_steps(registry, *, run, candidate: str, coordinator_root: str | Path):
    def matches_candidate(card: str, job: Mapping[str, object]) -> bool:
        subject = job.get("subject_head")
        if subject == candidate:
            return True
        if (
            card != "openspec-archive"
            or not _manager_archive_applied(run)
            or not isinstance(subject, str)
            or verification.SAFE_SHA_RE.fullmatch(subject) is None
            or verification.SAFE_SHA_RE.fullmatch(candidate) is None
        ):
            return False
        try:
            ancestry = subprocess.run(
                [
                    "git", "-C", str(run.workspace_root), "merge-base", "--is-ancestor",
                    subject, candidate,
                ],
                shell=False,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return False
        return ancestry.returncode == 0

    steps = run.steps
    for card in ("openspec-archive", "policy-commit"):
        jobs = [
            job
            for job in registry.list_jobs()
            if job.get("workflow_run_id") == run.run_id
            and job.get("workflow_phase") == "ship"
            and job.get("workflow_card") == card
            and job.get("persona") == "manager"
            and matches_candidate(card, job)
            and job.get("status") == "exited"
            and job.get("exit_code") == 0
        ]
        if len(jobs) != 1:
            raise ValueError(f"workflow ship card audit missing or ambiguous: {card}")
        payload, _outputs, _path, _digest = _read_job_workflow_evidence(
            jobs[0], run=run, coordinator_root=coordinator_root
        )
        if (
            payload.get("kind") != "workflow-card"
            or payload.get("status") != "passed"
            or payload.get("run_id") != run.run_id
            or payload.get("card_id") != card
            or payload.get("candidate") != jobs[0].get("subject_head")
        ):
            raise ValueError(f"workflow ship card evidence invalid: {card}")
        steps = _audit_phase_steps(
            steps,
            phase="ship",
            executor="cortex-manager",
            model="deterministic",
            domain="cortex",
            outputs=(),
            card_id=card,
        )
    return steps


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PlanningPublicationDrift(RuntimeError):
    """A durable planning intent cannot be safely committed or rolled back."""


class _PlanningPublicationTransaction:
    """Recoverable filesystem side of the brainstorm -> registry commit.

    Every intended mutation is durably journaled before it is applied.  A
    registry save fault can roll the group back immediately; after a crash,
    Manager reconciles the journal against the persisted brainstorm gate.
    """

    def __init__(
        self,
        *,
        root: Path,
        run_id: str,
        journal_root: Path | None,
    ) -> None:
        self.root = root.resolve()
        self.run_id = run_id
        self.operations: list[dict[str, object]] = []
        self.expected_gate_ref: dict[str, str] | None = None
        self.journal_root = journal_root.resolve() if journal_root is not None else None
        self.journal_path = (
            self.journal_root / "planning-transactions" / f"{run_id}.json"
            if self.journal_root is not None
            else None
        )

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "kind": "planning-publication-intent",
            "run_id": self.run_id,
            "root": str(self.root),
            "operations": self.operations,
            "expected_gate_ref": self.expected_gate_ref,
        }

    def _persist(self) -> None:
        if self.journal_path is None:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        content = (json.dumps(self._payload(), ensure_ascii=False, sort_keys=True) + "\n").encode()
        fd, tmp_name = tempfile.mkstemp(dir=self.journal_path.parent, suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.journal_path)
            directory_fd = os.open(self.journal_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _write_atomic(
        path: Path,
        content: bytes,
        mode: int,
        *,
        expect_absent: bool = False,
        expected_hash: str | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".planning.tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, mode)
            if expect_absent:
                os.link(tmp, path)
                tmp.unlink()
            else:
                if expected_hash is not None and (
                    not path.is_file() or _sha256_path(path) != expected_hash
                ):
                    raise ValueError(f"planning artifact baseline CAS conflict: {path}")
                os.replace(tmp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            tmp.unlink(missing_ok=True)

    def publish(
        self,
        path: Path,
        content: bytes,
        *,
        baseline_hash: str | None,
        mode: int = 0o644,
        kind: str = "artifact",
    ) -> None:
        path = Path(os.path.abspath(path))
        boundary = self.root
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            if kind != "evidence" or self.journal_root is None:
                raise
            relative = path.relative_to(self.journal_root)
            boundary = self.journal_root
        if path.is_symlink():
            raise ValueError("planning publication symlink rejected")
        cursor = path.parent
        while cursor != boundary:
            if cursor.is_symlink():
                raise ValueError("planning publication parent symlink rejected")
            parent = cursor.parent
            if parent == cursor:
                raise ValueError("planning publication boundary invalid")
            cursor = parent
        existed = path.is_file()
        before = path.read_bytes() if existed else None
        before_hash = hashlib.sha256(before).hexdigest() if before is not None else None
        idempotent_evidence = existed and before == content and kind == "evidence"
        if idempotent_evidence and path.stat().st_mode & 0o7777 != mode:
            raise ValueError(f"planning immutable evidence mode conflict: {relative}")
        if existed and baseline_hash is None:
            if not idempotent_evidence:
                raise ValueError(f"planning artifact no-clobber conflict: {relative}")
        if baseline_hash is not None and (not existed or before_hash != baseline_hash):
            raise ValueError(f"planning artifact baseline CAS conflict: {relative}")
        missing_dirs: list[str] = []
        parent = path.parent
        while parent != boundary and not parent.exists():
            missing_dirs.append(str(parent))
            parent = parent.parent
        after_mode = (path.stat().st_mode & 0o7777) if existed else mode
        operation: dict[str, object] = {
            "kind": kind,
            "path": str(path),
            "before_exists": existed,
            "before_hash": before_hash,
            "before_content": (
                base64.b64encode(before).decode("ascii") if before is not None else None
            ),
            "before_mode": (path.stat().st_mode & 0o7777) if existed else None,
            "after_hash": hashlib.sha256(content).hexdigest(),
            "after_mode": after_mode,
            "created_dirs": list(reversed(missing_dirs)),
            "mutation": not idempotent_evidence,
        }
        self.operations.append(operation)
        self._persist()
        if idempotent_evidence:
            return
        self._write_atomic(
            path,
            content,
            after_mode,
            expect_absent=not existed,
            expected_hash=before_hash if existed else None,
        )

    def write_evidence(self, path: Path, payload: object) -> None:
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("kind") != "brainstorm-peer"
        ):
            raise ValueError("brainstorm evidence payload is invalid")
        content = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        self.expected_gate_ref = {
            "kind": "brainstorm",
            "ref": str(Path(os.path.abspath(path))),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        self.publish(path, content, baseline_hash=None, mode=0o600, kind="evidence")

    def rollback(self) -> None:
        for operation in reversed(self.operations):
            path = Path(str(operation["path"]))
            after_hash = str(operation["after_hash"])
            boundary = (
                self.journal_root
                if operation.get("kind") == "evidence"
                and self.journal_root is not None
                and not path.is_relative_to(self.root)
                else self.root
            )
            cursor = path.parent
            while cursor != boundary:
                if cursor.is_symlink():
                    raise RuntimeError(f"planning rollback parent became symlink: {cursor}")
                parent = cursor.parent
                if parent == cursor:
                    raise RuntimeError("planning rollback boundary invalid")
                cursor = parent
            if path.is_symlink():
                raise RuntimeError(f"planning rollback path became symlink: {path}")
            current_hash = _sha256_path(path) if path.is_file() else None
            before_hash = operation.get("before_hash")
            if current_hash == before_hash:
                pass
            elif current_hash != after_hash:
                raise RuntimeError(f"planning rollback refused operator drift: {path}")
            elif bool(operation["before_exists"]):
                encoded = operation.get("before_content")
                if not isinstance(encoded, str):
                    raise RuntimeError("planning rollback baseline missing")
                self._write_atomic(
                    path,
                    base64.b64decode(encoded),
                    int(operation["before_mode"]),
                )
            else:
                path.unlink(missing_ok=True)
                if path.parent.exists():
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            for directory in reversed(list(operation.get("created_dirs", []))):
                try:
                    Path(str(directory)).rmdir()
                except OSError:
                    pass
        self.operations.clear()
        self.commit()

    def commit(self) -> None:
        if self.journal_path is not None:
            self.journal_path.unlink(missing_ok=True)
            if self.journal_path.parent.exists():
                directory_fd = os.open(self.journal_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)

    def _validate_loaded_operation(self, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise RuntimeError("planning transaction operation is invalid")
        required = {
            "kind", "path", "before_exists", "before_hash", "before_content",
            "before_mode", "after_hash", "after_mode", "created_dirs", "mutation",
        }
        if set(value) != required or value.get("kind") not in {"artifact", "evidence"}:
            raise RuntimeError("planning transaction operation is invalid")
        raw_path = value.get("path")
        if (
            not isinstance(raw_path, str)
            or not Path(raw_path).is_absolute()
            or ".." in Path(raw_path).parts
        ):
            raise RuntimeError("planning transaction operation path is invalid")
        path = Path(raw_path)
        boundary = self.root
        try:
            path.relative_to(boundary)
        except ValueError:
            if value.get("kind") != "evidence" or self.journal_root is None:
                raise RuntimeError("planning transaction operation escapes boundary")
            boundary = self.journal_root
            try:
                path.relative_to(boundary)
            except ValueError as exc:
                raise RuntimeError("planning transaction operation escapes boundary") from exc
        for field in ("before_hash", "after_hash"):
            digest = value.get(field)
            if digest is not None and (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise RuntimeError("planning transaction hash is invalid")
        if not isinstance(value.get("before_exists"), bool):
            raise RuntimeError("planning transaction baseline is invalid")
        if not isinstance(value.get("mutation"), bool):
            raise RuntimeError("planning transaction mutation flag is invalid")
        if not value["mutation"] and (
            value.get("kind") != "evidence"
            or value.get("before_hash") != value.get("after_hash")
        ):
            raise RuntimeError("planning transaction immutable operation is invalid")
        if not isinstance(value.get("after_hash"), str) or not isinstance(value.get("after_mode"), int):
            raise RuntimeError("planning transaction target is invalid")
        if value["before_exists"]:
            if (
                not isinstance(value.get("before_hash"), str)
                or not isinstance(value.get("before_content"), str)
                or not isinstance(value.get("before_mode"), int)
            ):
                raise RuntimeError("planning transaction baseline is invalid")
            try:
                base64.b64decode(value["before_content"], validate=True)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("planning transaction baseline is invalid") from exc
        elif any(value.get(field) is not None for field in ("before_hash", "before_content", "before_mode")):
            raise RuntimeError("planning transaction absent baseline is invalid")
        created_dirs = value.get("created_dirs")
        if not isinstance(created_dirs, list):
            raise RuntimeError("planning transaction created_dirs is invalid")
        parents = set(path.parents)
        for raw_dir in created_dirs:
            directory = Path(str(raw_dir))
            if not directory.is_absolute() or directory not in parents or directory == boundary:
                raise RuntimeError("planning transaction created_dir escapes boundary")
            try:
                directory.relative_to(boundary)
            except ValueError as exc:
                raise RuntimeError("planning transaction created_dir escapes boundary") from exc
        return dict(value)

    def _validate_committed_operation(self, operation: Mapping[str, object]) -> None:
        path = Path(str(operation["path"]))
        boundary = self.root
        try:
            path.relative_to(boundary)
        except ValueError:
            if operation.get("kind") != "evidence" or self.journal_root is None:
                raise PlanningPublicationDrift("planning committed artifact escaped boundary")
            boundary = self.journal_root
            try:
                path.relative_to(boundary)
            except ValueError as exc:
                raise PlanningPublicationDrift(
                    "planning committed artifact escaped boundary"
                ) from exc
        cursor = path.parent
        while cursor != boundary:
            if cursor.is_symlink():
                raise PlanningPublicationDrift(
                    f"planning committed artifact parent type drift: {cursor}"
                )
            parent = cursor.parent
            if parent == cursor:
                raise PlanningPublicationDrift("planning committed artifact boundary drift")
            cursor = parent
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise PlanningPublicationDrift(f"planning committed artifact drift: {path}") from exc
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise PlanningPublicationDrift(f"planning committed artifact type drift: {path}")
        if _sha256_path(path) != operation["after_hash"]:
            raise PlanningPublicationDrift(f"planning committed artifact hash drift: {path}")
        if metadata.st_mode & 0o7777 != operation["after_mode"]:
            raise PlanningPublicationDrift(f"planning committed artifact mode drift: {path}")
        if operation.get("kind") == "evidence":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PlanningPublicationDrift("planning committed evidence drift") from exc
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != 1
                or payload.get("kind") != "brainstorm-peer"
                or self.expected_gate_ref is None
                or self.expected_gate_ref.get("ref") != str(path)
                or self.expected_gate_ref.get("sha256") != operation["after_hash"]
            ):
                raise PlanningPublicationDrift("planning committed evidence binding drift")

    @classmethod
    def reconcile(cls, *, root: Path, journal_root: Path, run) -> None:
        path = journal_root.resolve() / "planning-transactions" / f"{run.run_id}.json"
        if path.is_symlink():
            raise PlanningPublicationDrift("planning transaction journal symlink rejected")
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanningPublicationDrift("planning transaction journal is unreadable") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 2
            or payload.get("kind") != "planning-publication-intent"
            or payload.get("run_id") != run.run_id
            or payload.get("root") != str(root.resolve())
            or not isinstance(payload.get("operations"), list)
            or payload.get("expected_gate_ref") is not None
            and not isinstance(payload.get("expected_gate_ref"), dict)
        ):
            raise PlanningPublicationDrift("planning transaction journal is invalid")
        transaction = cls(root=root, run_id=run.run_id, journal_root=journal_root)
        expected_gate_ref = payload["expected_gate_ref"]
        expected: GateEvidenceRef | None = None
        if expected_gate_ref is not None:
            try:
                expected = GateEvidenceRef.from_dict(expected_gate_ref)
            except ValueError as exc:
                raise PlanningPublicationDrift("planning expected gate ref is invalid") from exc
            if expected.kind != "brainstorm" or expected.sha256 is None:
                raise PlanningPublicationDrift("planning expected gate ref is invalid")
            transaction.expected_gate_ref = expected.to_dict()
        try:
            transaction.operations = [
                transaction._validate_loaded_operation(operation)
                for operation in payload["operations"]
            ]
        except RuntimeError as exc:
            raise PlanningPublicationDrift("planning transaction operation drift") from exc
        evidence_operations = [
            operation
            for operation in transaction.operations
            if operation.get("kind") == "evidence"
        ]
        if expected is not None and len(evidence_operations) != 1:
            raise PlanningPublicationDrift("planning committed evidence operation is invalid")
        committed = expected is not None and any(ref == expected for ref in run.gate_refs)
        if committed:
            for operation in transaction.operations:
                transaction._validate_committed_operation(operation)
            transaction.commit()
        else:
            try:
                transaction.rollback()
            except RuntimeError as exc:
                raise PlanningPublicationDrift("planning uncommitted rollback drift") from exc


def _publish_planning_artifacts(
    root_value: str,
    rows: object,
    *,
    work_id: str,
    allowed_refs: tuple[str, ...],
    authorities: tuple[PlanningArtifactAuthority, ...] = (),
    transaction: _PlanningPublicationTransaction | None = None,
) -> Callable[[], None]:
    if not isinstance(rows, list):
        raise ValueError("planning artifacts must be a list")
    root = Path(root_value).resolve()
    authority_by_ref = {item.ref: item for item in authorities}
    if len(authority_by_ref) != len(authorities):
        raise ValueError("duplicate planning authority ref")
    prepared: list[tuple[Path, bytes, str | None]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"kind", "path", "content"}:
            raise ValueError("planning artifact schema invalid")
        path_value = row.get("path")
        content = row.get("content")
        if not isinstance(path_value, str) or not isinstance(content, str):
            raise ValueError("planning artifact path/content invalid")
        relative = Path(path_value)
        docs_bound = (
            relative.parts[:3] in {
                ("docs", "superpowers", "specs"),
                ("docs", "superpowers", "plans"),
            }
        )
        openspec_bound = (
            len(relative.parts) >= 4
            and relative.parts[:2] == ("openspec", "changes")
            and relative.parts[2] == work_id
            and relative.parts[2] != "archive"
        )
        manifest_bound = any(fnmatch.fnmatch(path_value, pattern) for pattern in allowed_refs)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not (docs_bound or openspec_bound)
            or not manifest_bound
            or relative.suffix != ".md"
        ):
            raise ValueError("planning artifact path outside governed roots")
        unresolved = root / relative
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("planning artifact symlink rejected")
        path = unresolved.resolve()
        path.relative_to(root)
        artifact = PlanningArtifact(kind=str(row["kind"]), ref=path_value, text=content)
        if not assess_planning_artifact(artifact).accepted:
            raise ValueError(f"planning artifact is not accepted: {path_value}")
        owner = authority_by_ref.get(path_value)
        baseline_hash: str | None = None
        if path.exists():
            if (
                owner is None
                or owner.ref != path_value
                or owner.kind != row["kind"]
                or owner.work_id != work_id
            ):
                raise ValueError(f"planning artifact lacks current planning authority: {path_value}")
            baseline_hash = owner.baseline_sha256
            if not path.is_file() or _sha256_path(path) != baseline_hash:
                raise ValueError(f"planning artifact current authority drift: {path_value}")
        elif owner is not None:
            raise ValueError(f"planning artifact current authority drift: {path_value}")
        prepared.append((path, content.encode("utf-8"), baseline_hash))

    publication = transaction or _PlanningPublicationTransaction(
        root=root, run_id="ephemeral", journal_root=None
    )
    try:
        for target, content, baseline_hash in prepared:
            publication.publish(target, content, baseline_hash=baseline_hash)
    except BaseException:
        publication.rollback()
        raise
    return publication.rollback


def _current_workflow_step(run):
    pending = [
        step
        for step in run.steps
        if step.phase == run.current_phase and step.gate_result != "passed"
    ]
    return pending[0] if pending else None


def _select_workflow_identity(run, step, identities: IdentityRegistry):
    builder_domains = {
        item.domain
        for item in run.steps
        if item.phase == "build" and item.gate_result == "passed" and item.domain is not None
    }
    candidates = list(identities.identities)
    if step.persona == "planner":
        candidates = [item for item in candidates if "planning" in item.capabilities]
    if step.persona == "reviewer":
        candidates = [
            item
            for item in candidates
            if "review" in item.capabilities
            and item.independence_domain not in builder_domains
        ]
    elif run.primary_domain is not None:
        preferred = [item for item in candidates if item.independence_domain == run.primary_domain]
        if preferred:
            candidates = preferred
    if not candidates:
        raise ValueError(f"no configured identity for workflow persona: {step.persona}")
    return candidates[0]


_LEGACY_CARD_EXECUTION = {
    "worktree-isolation": (
        "superpowers:using-git-worktrees",
        "Confirm the Manager-provisioned worktree; do not create a second worktree.",
        "forbidden",
        "none",
    ),
    "tdd-red": (
        "superpowers:test-driven-development",
        "Use the accepted plan to add and commit a reproducible RED regression test.",
        "required",
        "red-required",
    ),
    "subagent-build": (
        "superpowers:subagent-driven-development",
        "Implement the accepted plan with the minimum diff and commit a tested candidate HEAD.",
        "required",
        "focused",
    ),
}


def _workflow_job_prompt(
    run,
    step,
    *,
    builder_job_id: str | None,
    coordinator_root: str | Path,
    input_snapshot: tuple[dict[str, str], ...] = (),
    candidate_checkout: str | None = None,
) -> str:
    fallback = _LEGACY_CARD_EXECUTION.get(step.card, (None, None, None, None))
    source_material: list[dict[str, object]] = []
    for row in input_snapshot:
        envelope = _read_workflow_input_content(
            row,
            run=run,
            coordinator_root=coordinator_root,
        )
        source_material.append({**row, "content": envelope["content"]})
    if step.phase == "verify":
        terminal_schema: dict[str, object] = {
            "kind": "workflow-verification-result",
            "schema_version": 1,
            "required": ["schema_version", "kind", "status", "summary", "details", "reports"],
            "fixed": {"schema_version": 1, "kind": "workflow-verification-result", "status": "verified"},
            "reports": [{"path": "concrete repo-relative path matching declared_outputs", "body": "Markdown body without frontmatter"}],
        }
    elif step.phase == "review":
        terminal_schema = {
            "kind": "workflow-review-result",
            "schema_version": 1,
            "required": ["schema_version", "kind", "reason", "findings", "reports"],
            "fixed": {"schema_version": 1, "kind": "workflow-review-result"},
            "finding_keys": ["category", "severity", "summary", "evidence", "recommendation"],
            "finding_evidence_keys": ["path", "line", "detail"],
            "finding_categories": sorted(foreign_review.VALID_FINDING_CATEGORIES),
            "finding_severities": sorted(foreign_review.VALID_SEVERITIES),
            "finding_category_policy": {
                "blocking": (
                    "Use correctness/acceptance/security/data-loss/race/scope-bypass/"
                    "verification-bypass only for Candidate or acceptance defects."
                ),
                "report_only": (
                    "Use style for prior-report wording or enumeration inaccuracies that do not "
                    "change the Candidate verdict, and correct the record in this report."
                ),
            },
            "reports": [{"path": "concrete repo-relative path matching declared_outputs", "body": "Markdown body without frontmatter"}],
        }
    else:
        fixed_terminal_fields: dict[str, object] = {
            "schema_version": 1,
            "kind": "workflow-card",
            "run_id": run.run_id,
            "card_id": step.card,
        }
        if not step.outputs:
            fixed_terminal_fields["outputs"] = []
        terminal_schema = {
            "kind": "workflow-card",
            "schema_version": 1,
            "required": ["schema_version", "kind", "status", "run_id", "card_id", "candidate", "outputs"],
            "fixed": fixed_terminal_fields,
            "status": ["passed", "failed", "needs_human"],
            "outputs": {
                "type": "array",
                "items": "repo-relative artifact path string matching declared_outputs",
                "must_match_every_declared_output": True,
                "descriptive_objects_forbidden": True,
            },
        }
    contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "workflow-card-prompt",
        "run_id": run.run_id,
        "work_id": run.work_id,
        "repo": run.repo,
        "source_revision": run.source_revision,
        "phase": step.phase,
        "card_id": step.card,
        "persona": step.persona,
        "inputs": list(dict.fromkeys(row["pattern"] for row in input_snapshot)),
        "source_material": source_material,
        "declared_outputs": list(step.outputs),
        "candidate": run.candidate_head,
        "skill_ref": step.skill_ref or fallback[0],
        "action": step.action or fallback[1],
        "commit_policy": step.commit_policy or fallback[2],
        "test_policy": step.test_policy or fallback[3],
        "terminal_schema": terminal_schema,
    }
    if builder_job_id is not None:
        contract["builder_job_id"] = builder_job_id
    if candidate_checkout is not None:
        contract["candidate_checkout"] = candidate_checkout
    planner_contract = (
        " This planner card is read-only: use the disposable checkout only, do not edit files, and "
        "return only existing manifest-declared artifacts."
        if step.persona == "planner"
        else ""
    )
    reviewer_contract = (
        " This reviewer card is read-only: inspect and run only non-mutating commands in the "
        "Candidate checkout. If candidate_checkout is present, change into that relative directory "
        "before every repository command. Execute the verification or review now; do not create a plan or ask "
        "for approval. Return report bodies inline; Manager alone writes report files, binding "
        "frontmatter, job IDs, Candidate and launch identities."
        if step.persona == "reviewer"
        else ""
    )
    return (
        "Execute exactly one workflow card. End with one JSON object only; do not supply an evidence "
        "path or hash because Manager will canonicalize it. For workflow-card outputs, return only "
        "repo-relative artifact path strings matching declared_outputs; when declared_outputs is "
        "empty, outputs must be exactly []. Never put action, summary, or other descriptive objects "
        "in outputs."
        + planner_contract
        + reviewer_contract
        + " Contract: "
        + json.dumps(contract, ensure_ascii=False, sort_keys=True)
    )


def _dispatch_workflow_card(
    dispatcher,
    *,
    run,
    identities: IdentityRegistry,
    launcher_factory: Callable[[object], object],
    coordinator_root: str | Path,
    retry_failed: bool = False,
    operator_recovery_job_id: str | None = None,
    force_new_build: bool = False,
) -> dict[str, object] | None:
    registry = getattr(dispatcher, "_registry", None)
    if registry is None:
        raise RuntimeError("workflow dispatch requires dispatcher registry")
    step = _current_workflow_step(run)
    if step is None or run.current_phase not in {"plan", "build", "verify", "review"}:
        return None
    if force_new_build and (step.phase != "build" or step.persona != "builder"):
        raise ValueError("forced workflow retry requires builder card")
    matching = [
        job
        for job in registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_card") == step.card
        and job.get("workflow_phase") == step.phase
        and (
            step.phase not in {"verify", "review"}
            or job.get("subject_head") == run.candidate_head
        )
    ]
    retryable_latest = bool(
        matching
        and (
            (
                retry_failed
                and (
                    matching[-1].get("status") == "failed"
                    or _retryable_nonpassing_workflow_terminal(matching[-1])
                    or _is_rejected_workflow_review_evidence(
                        matching[-1],
                        run=run,
                        coordinator_root=coordinator_root,
                    )
                )
            )
            or (
                operator_recovery_job_id == matching[-1].get("job_id")
                and (
                    _is_exact_legacy_agy_recovery(
                        matching[-1], run=run, step=step, identities=identities
                    )
                    or _is_exact_reviewer_terminal_recovery(
                        registry,
                        matching[-1],
                        run=run,
                        step=step,
                        identities=identities,
                        coordinator_root=coordinator_root,
                    )
                )
            )
            or (force_new_build and matching[-1].get("status") in TERMINAL_STATUSES)
        )
    )
    if matching and not retryable_latest:
        return matching[-1]
    if matching and step.persona == "planner":
        _discard_failed_planner_sandbox(
            matching[-1],
            run_id=run.run_id,
            card=step.card,
            coordinator_root=coordinator_root,
        )
    identity = _select_workflow_identity(run, step, identities)
    launcher = launcher_factory(identity)
    if launcher is None:
        raise ValueError("workflow launcher unavailable")
    if step.persona == "planner":
        read_only_factory = getattr(launcher, "as_read_only", None)
        if not callable(read_only_factory):
            raise ValueError("planner launcher lacks enforced read-only contract")
        launcher = read_only_factory()
    elif step.persona == "reviewer":
        review_only_factory = getattr(launcher, "as_review_only", None)
        if not callable(review_only_factory):
            raise ValueError("reviewer launcher lacks enforced read-only contract")
        terminal_kind = (
            "workflow-verification-result"
            if step.phase == "verify"
            else "workflow-review-result"
        )
        launcher = review_only_factory(terminal_kind=terminal_kind)
    effective_commit_policy = step.commit_policy or _LEGACY_CARD_EXECUTION.get(
        step.card, (None, None, None, None)
    )[2]
    if effective_commit_policy == "required":
        if step.persona != "builder":
            raise ValueError("commit-required workflow card must use builder persona")
        commit_required_factory = getattr(launcher, "as_commit_required", None)
        if not callable(commit_required_factory):
            raise ValueError("builder launcher lacks explicit commit-required capability")
        launcher = commit_required_factory()
    builder_jobs = [
        job
        for job in registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
        and (
            job.get("persona") == "builder"
            or (
                job.get("persona") == "manager"
                and job.get("workflow_phase") == "ship"
                and job.get("workflow_card") == "openspec-archive"
            )
        )
        and job.get("status") == "exited"
        and job.get("exit_code") == 0
        and (
            run.candidate_head is None
            or job.get("subject_head") == run.candidate_head
        )
    ]
    builder_job_id = str(builder_jobs[-1]["job_id"]) if builder_jobs else None
    if step.persona == "reviewer" and builder_job_id is None:
        raise ValueError("workflow reviewer builder job unavailable")
    planner_sandbox: Path | None = None
    reviewer_sandbox: Path | None = None
    sandbox_hash: str | None = None
    repo_root = run.workspace_root
    if step.persona == "planner":
        sandbox_parent = Path(coordinator_root).resolve() / "planning-sandboxes"
        try:
            sandbox_parent.relative_to(Path(run.workspace_root).resolve())
        except ValueError:
            pass
        else:
            sandbox_parent = (
                Path(coordinator_root).resolve().parent
                / f".{Path(coordinator_root).resolve().name}-planning-sandboxes"
            )
        sandbox_parent.mkdir(parents=True, exist_ok=True)
        sandbox_name = hashlib.sha256(f"{run.run_id}:{step.card}".encode()).hexdigest()[:32]
        planner_sandbox = sandbox_parent / sandbox_name
        if planner_sandbox.exists() or planner_sandbox.is_symlink():
            raise ValueError("stale planner sandbox requires reconciliation")
        planning_runtime._copy_planning_sandbox(Path(run.workspace_root), planner_sandbox)
        sandbox_hash = planning_runtime._tree_snapshot(planner_sandbox)
        worktree = str(planner_sandbox)
    elif builder_jobs:
        worktree = str(builder_jobs[-1]["worktree"])
    elif step.phase == "build":
        creator = getattr(dispatcher, "_worktree_creator", None)
        if creator is None:
            raise ValueError("workflow builder worktree creator unavailable")
        issue_numbers = [
            match.group(1)
            for ref in run.issue_refs
            if (match := re.fullmatch(rf"{re.escape(run.repo)}#([1-9][0-9]*)", ref))
        ]
        if len(issue_numbers) > 1:
            raise ValueError("workflow builder requires exactly one confirmed issue")
        builder_branch = (
            f"feature/{issue_numbers[0]}-{run.work_id}"
            if issue_numbers
            else f"feature/{run.work_id}"
        )
        worktree = str(creator.create(builder_branch))
    else:
        worktree = run.workspace_root
    effective_repo_root = Path(worktree).resolve()
    effective_inputs = _effective_workflow_inputs(run, step)
    if step.persona == "reviewer":
        reviewer_target = effective_repo_root
        input_snapshot = _workflow_input_snapshot(
            run=run,
            repo_root=reviewer_target,
            patterns=effective_inputs,
            coordinator_root=coordinator_root,
        )
        output_baseline = _workflow_output_baseline(reviewer_target, step.outputs)
        try:
            reviewer_sandbox, reviewer_checkout = _create_reviewer_sandbox(
                run=run,
                step=step,
                executor=identity.executor,
                candidate_root=reviewer_target,
                coordinator_root=coordinator_root,
                input_snapshot=input_snapshot,
            )
            sandbox_hash = planning_runtime._tree_snapshot(reviewer_target)
            repo_root = str(reviewer_target)
            worktree = str(reviewer_sandbox)
            effective_repo_root = reviewer_checkout
            _validate_workflow_input_snapshot(
                effective_repo_root,
                list(input_snapshot),
                coordinator_root=coordinator_root,
            )
        except BaseException:
            if reviewer_sandbox is not None:
                shutil.rmtree(reviewer_sandbox, ignore_errors=True)
            raise
    else:
        input_snapshot = _workflow_input_snapshot(
            run=run,
            repo_root=effective_repo_root,
            patterns=effective_inputs,
            coordinator_root=coordinator_root,
        )
        output_baseline = _workflow_output_baseline(effective_repo_root, step.outputs)
    dispatch_base: str | None = None
    if step.phase == "build":
        if builder_jobs:
            persisted_base = builder_jobs[0].get("dispatch_head")
            if (
                not isinstance(persisted_base, str)
                or verification.SAFE_SHA_RE.fullmatch(persisted_base) is None
            ):
                raise ValueError("workflow build phase base is unavailable")
            dispatch_base = persisted_base
        else:
            base_result = verification._run_git(
                ["-C", str(effective_repo_root), "rev-parse", "HEAD"],
                getattr(dispatcher, "_git_runner", None),
            )
            base_value = str(base_result.get("stdout", "")).strip().lower()
            if (
                base_result.get("status") != "ok"
                or verification.SAFE_SHA_RE.fullmatch(base_value) is None
            ):
                raise ValueError("workflow build phase base is unavailable")
            dispatch_base = base_value
    task = f"wf-{hashlib.sha256(run.run_id.encode()).hexdigest()[:10]}-{step.card}"
    try:
        branch = (
            (
                str(builder_jobs[-1]["branch"])
                if builder_jobs and isinstance(builder_jobs[-1].get("branch"), str)
                else builder_branch
            )
            if step.phase == "build"
            else (
                str(builder_jobs[-1]["branch"])
                if builder_jobs and isinstance(builder_jobs[-1].get("branch"), str)
                else f"feature/{run.work_id}"
            )
        )
        job = registry.create_job(
            task=task,
            persona=step.persona,
            kind="review" if step.persona == "reviewer" else "build",
            branch=branch,
            pane="",
            worktree=worktree,
            dispatch_head=dispatch_base,
            executor=identity.executor,
            model_id=identity.model_id,
            independence_domain=identity.independence_domain,
            subject_head=run.candidate_head if step.phase in {"verify", "review"} else None,
            workflow_run_id=run.run_id,
            workflow_claim_key=run.claim_key,
            workflow_repo=run.repo,
            workflow_card=step.card,
            workflow_phase=step.phase,
            workflow_repo_root=(
                repo_root if step.persona in {"planner", "reviewer"} else worktree
            ),
            workflow_input_root=str(effective_repo_root),
            workflow_inputs=effective_inputs,
            workflow_input_snapshot=input_snapshot,
            workflow_outputs=step.outputs,
            source_revision=run.source_revision,
            workflow_sandbox_hash=sandbox_hash,
            workflow_output_baseline=output_baseline,
            workflow_builder_job_id=builder_job_id if step.persona == "reviewer" else None,
        )
    except BaseException:
        if planner_sandbox is not None:
            shutil.rmtree(planner_sandbox, ignore_errors=True)
        if reviewer_sandbox is not None:
            shutil.rmtree(reviewer_sandbox, ignore_errors=True)
        raise
    try:
        handle = launcher.launch(
            slice_id=str(job["job_id"]),
            prompt=_workflow_job_prompt(
                run,
                step,
                builder_job_id=builder_job_id,
                coordinator_root=coordinator_root,
                input_snapshot=input_snapshot,
                candidate_checkout=(
                    "candidate"
                    if step.persona == "reviewer" and identity.executor == "claude"
                    else None
                ),
            ),
            worktree=worktree,
            log_dir=str(Path(coordinator_root).resolve() / "logs" / "workflow"),
        )
        return registry.attach_launch_handle(
            str(job["job_id"]),
            executor=identity.executor,
            model_id=identity.model_id,
            session_name=handle.session_name,
            pid=handle.pid,
            log_path=handle.log_path,
        )
    except BaseException as launch_exc:
        registry.update_headless_result(str(job["job_id"]), status="failed", exit_code=1)
        if planner_sandbox is not None:
            shutil.rmtree(planner_sandbox, ignore_errors=True)
        if reviewer_sandbox is not None:
            try:
                _discard_reviewer_sandbox(
                    registry.get_job(str(job["job_id"])),
                    coordinator_root=coordinator_root,
                    require_candidate_unchanged=True,
                )
            except Exception as cleanup_exc:
                raise cleanup_exc from launch_exc
        raise


def dispatch_workflow_card(
    dispatcher,
    *,
    run,
    identities: IdentityRegistry,
    launcher_factory: Callable[[object], object],
    coordinator_root: str | Path,
    retry_failed: bool = False,
    force_new_build: bool = False,
) -> dict[str, object] | None:
    """Dispatch a normal workflow card; legacy recovery is operator-resume internal only."""

    return _dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=launcher_factory,
        coordinator_root=coordinator_root,
        retry_failed=retry_failed,
        force_new_build=force_new_build,
    )


def _merged_delivery_reconciliation_pending(run, *, coordinator_root: str | Path) -> bool:
    """Detect the narrow terminal closure path without granting ship authority."""
    terminal_refresh = (
        run.current_phase == "ship" and getattr(run, "status", None) == "done"
    )
    if (
        run.current_phase != "review" and not terminal_refresh
    ) or not isinstance(run.candidate_head, str):
        return False
    journal_path = Path(coordinator_root) / "delivery-journal.json"
    if journal_path.is_symlink() or not journal_path.is_file():
        return False
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    rows = journal.get("runs") if isinstance(journal, dict) else None
    row = rows.get(run.run_id) if isinstance(rows, dict) else None
    ship = row.get("ship") if isinstance(row, dict) else None
    authorization = ship.get("merge_authorization") if isinstance(ship, dict) else None
    authorization_body = (
        authorization.get("payload") if isinstance(authorization, dict) else None
    )
    return bool(
        isinstance(journal, dict)
        and journal.get("schema") == "cortex-delivery-journal/v1"
        and isinstance(row, dict)
        and row.get("run_id") == run.run_id
        and row.get("repo") == run.repo
        and row.get("work_id") == run.work_id
        and isinstance(ship, dict)
        and ship.get("phase") in {"merged", "done"}
        and ship.get("head") == run.candidate_head
        and isinstance(ship.get("merge_commit"), str)
        and verification.SAFE_SHA_RE.fullmatch(ship["merge_commit"]) is not None
        and isinstance(authorization, dict)
        and isinstance(authorization.get("path"), str)
        and Path(authorization["path"]).is_absolute()
        and isinstance(authorization.get("hash"), str)
        and re.fullmatch(r"[0-9a-f]{64}", authorization["hash"]) is not None
        and isinstance(authorization_body, dict)
        and authorization_body.get("run_id") == run.run_id
        and authorization_body.get("repo") == run.repo
        and authorization_body.get("work_id") == run.work_id
        and authorization_body.get("head") == run.candidate_head
    )


def resume_workflow_run(
    dispatcher,
    *,
    run_id: str,
    identities: IdentityRegistry,
    launcher_factory: Callable[[object], object],
    coordinator_root: str | Path,
    ship_validator: Callable[..., object] | None = None,
    operator_resume: bool = False,
) -> dict[str, object]:
    registry = getattr(dispatcher, "_registry", None)
    if registry is None:
        raise RuntimeError("workflow resume requires dispatcher registry")
    run = registry.get_workflow_run(run_id)
    pre_resume_gate_status = run.gate_status
    retry_failed = False
    recovery_job_id: str | None = None
    if "needs_human" in run.facets and run.status == "ongoing":
        if not operator_resume:
            return {
                "run_id": run.run_id,
                "current_phase": run.current_phase,
                "reason": "operator-resume-required",
            }
        recovery_step = _current_workflow_step(run)
        if recovery_step is not None:
            recovery_jobs = [
                job
                for job in registry.list_jobs()
                if job.get("workflow_run_id") == run.run_id
                and job.get("workflow_card") == recovery_step.card
                and job.get("workflow_phase") == recovery_step.phase
            ]
            latest_recovery = recovery_jobs[-1] if recovery_jobs else None
            if (
                latest_recovery is not None
                and recovery_step.phase == "review"
                and latest_recovery.get("workflow_evidence") is not None
                and _workflow_review_evidence_state(
                    latest_recovery,
                    run=run,
                    coordinator_root=coordinator_root,
                ) is None
            ):
                return {
                    "run_id": run.run_id,
                    "current_phase": run.current_phase,
                    "job_id": latest_recovery.get("job_id"),
                    "reason": "rejected-review-recovery-mismatch",
                }
            if latest_recovery is not None and (
                _is_exact_legacy_agy_recovery(
                    latest_recovery, run=run, step=recovery_step, identities=identities
                )
                or _is_exact_reviewer_terminal_recovery(
                    registry,
                    latest_recovery,
                    run=run,
                    step=recovery_step,
                    identities=identities,
                    coordinator_root=coordinator_root,
                )
            ):
                recovery_job_id = str(latest_recovery["job_id"])
            if recovery_job_id is not None:
                try:
                    _discard_reviewer_sandbox(
                        latest_recovery,
                        coordinator_root=coordinator_root,
                        require_candidate_unchanged=True,
                    )
                except ValueError:
                    return {
                        "run_id": run.run_id,
                        "current_phase": run.current_phase,
                        "job_id": recovery_jobs[-1].get("job_id"),
                        "reason": "reviewer-candidate-drift",
                    }
        run = registry._manager_update_workflow_run(
            run.run_id,
            facets=tuple(facet for facet in run.facets if facet != "needs_human"),
            gate_status="running",
        )
        retry_failed = True
    try:
        _PlanningPublicationTransaction.reconcile(
            root=Path(run.workspace_root),
            journal_root=Path(coordinator_root),
            run=run,
        )
    except PlanningPublicationDrift:
        updated = registry._manager_update_workflow_run(
            run.run_id, facets=("needs_human",), gate_status="running"
        )
        return {
            "run_id": updated.run_id,
            "current_phase": updated.current_phase,
            "reason": "planning-publication-drift",
        }
    post_merge_closure = _merged_delivery_reconciliation_pending(
        run, coordinator_root=coordinator_root
    )
    if not post_merge_closure:
        try:
            planning_authority, planning_source_revision = (
                _validated_brainstorm_planning_authority(
                    run,
                    coordinator_root=coordinator_root,
                )
            )
        except ValueError:
            current = registry.get_workflow_run(run.run_id)
            updated = registry._manager_update_workflow_run(
                run.run_id,
                facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
                gate_status=pre_resume_gate_status,
            )
            return {
                "run_id": updated.run_id,
                "current_phase": updated.current_phase,
                "reason": "planning-authority-reconciliation-failed",
            }
        if (
            planning_authority != run.planning_authority
            or planning_source_revision != run.planning_source_revision
        ):
            run = registry._manager_update_workflow_run(
                run.run_id,
                planning_authority=planning_authority,
                planning_source_revision=planning_source_revision,
            )

    def dispatch_or_stop(
        bound_run,
        *,
        retry: bool = False,
        retry_recovery_job_id: str | None = None,
    ):
        try:
            if retry_recovery_job_id is not None:
                return _dispatch_workflow_card(
                    dispatcher,
                    run=bound_run,
                    identities=identities,
                    launcher_factory=launcher_factory,
                    coordinator_root=coordinator_root,
                    retry_failed=retry,
                    operator_recovery_job_id=retry_recovery_job_id,
                )
            return dispatch_workflow_card(
                dispatcher,
                run=bound_run,
                identities=identities,
                launcher_factory=launcher_factory,
                coordinator_root=coordinator_root,
                retry_failed=retry,
            )
        except Exception:
            current = registry.get_workflow_run(bound_run.run_id)
            registry._manager_update_workflow_run(
                bound_run.run_id,
                facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
                gate_status="running",
            )
            raise

    step = _current_workflow_step(run)
    if step is None:
        if run.current_phase == "review" and post_merge_closure and ship_validator is None:
            return {
                "run_id": run.run_id,
                "current_phase": run.current_phase,
                "reason": "ship-validator-unavailable",
            }
        if run.current_phase == "review" and ship_validator is not None:
            last = [item for item in run.steps if item.phase == "review"][-1]
            try:
                return apply_workflow_action(
                    registry,
                    args={
                        "action": "advance",
                        "run_id": run.run_id,
                        "card_id": last.card,
                        "current_phase": "ship",
                    },
                    identity_registry=identities,
                    ship_validator=ship_validator,
                    git_runner=getattr(dispatcher, "_git_runner", None),
                    coordinator_root=coordinator_root,
                    trusted_terminal=True,
                )
            except Exception:
                current = registry.get_workflow_run(run.run_id)
                registry._manager_update_workflow_run(
                    run.run_id,
                    facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
                    gate_status="failed",
                )
                raise
        if run.current_phase == "ship" and run.status == "done" and post_merge_closure:
            if ship_validator is None:
                return {
                    "run_id": run.run_id,
                    "current_phase": run.current_phase,
                    "reason": "ship-validator-unavailable",
                }
            return apply_workflow_action(
                registry,
                args={"action": "refresh-completion", "run_id": run.run_id},
                identity_registry=identities,
                ship_validator=ship_validator,
                git_runner=getattr(dispatcher, "_git_runner", None),
                coordinator_root=coordinator_root,
                trusted_terminal=True,
            )
        return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": "no-pending-card"}
    jobs = [
        job
        for job in registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_card") == step.card
        and job.get("workflow_phase") == step.phase
        and (
            step.phase not in {"verify", "review"}
            or job.get("subject_head") == run.candidate_head
        )
    ]
    job = jobs[-1] if jobs else dispatch_or_stop(run, retry=retry_failed)
    if job is not None and recovery_job_id == job.get("job_id"):
        job = dispatch_or_stop(run, retry_recovery_job_id=recovery_job_id)
    elif retry_failed and job is not None and (
        job.get("status") == "failed"
        or _retryable_nonpassing_workflow_terminal(job)
        or _is_rejected_workflow_review_evidence(
            job,
            run=run,
            coordinator_root=coordinator_root,
        )
    ):
        job = dispatch_or_stop(run, retry=True)
    if job is None:
        return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": "not-dispatchable"}
    if job.get("status") in IN_FLIGHT_STATUSES:
        job = dispatcher.poll_headless_done(str(job["job_id"]))
    if job.get("status") in IN_FLIGHT_STATUSES:
        return {"run_id": run.run_id, "current_phase": run.current_phase, "job_id": job["job_id"], "reason": "in-flight"}
    if job.get("status") != "exited" or job.get("exit_code") != 0:
        failure_reason = "job-failed"
        try:
            _discard_reviewer_sandbox(
                job,
                coordinator_root=coordinator_root,
                require_candidate_unchanged=True,
            )
        except ValueError:
            failure_reason = "reviewer-candidate-drift"
        updated = registry._manager_update_workflow_run(
            run.run_id, facets=("needs_human",), gate_status="running"
        )
        return {
            "run_id": run.run_id,
            "current_phase": updated.current_phase,
            "job_id": job["job_id"],
            "reason": failure_reason,
        }
    try:
        job = terminalize_workflow_job(
            registry,
            job_id=str(job["job_id"]),
            coordinator_root=coordinator_root,
        )
    except Exception:
        _discard_reviewer_sandbox(
            registry.get_job(str(job["job_id"])),
            coordinator_root=coordinator_root,
            require_candidate_unchanged=True,
        )
        current = registry.get_workflow_run(run.run_id)
        registry._manager_update_workflow_run(
            run.run_id,
            facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
            gate_status="running",
        )
        raise
    phase_steps = [item for item in run.steps if item.phase == run.current_phase]
    is_last = step.card == phase_steps[-1].card
    next_phase = (
        WORKFLOW_PHASES[WORKFLOW_PHASES.index(run.current_phase) + 1]
        if is_last
        else run.current_phase
    )
    try:
        result = apply_workflow_action(
            registry,
            args={
                "action": "advance",
                "run_id": run.run_id,
                "card_id": step.card,
                "job_id": job["job_id"],
                "current_phase": next_phase,
            },
            identity_registry=identities,
            ship_validator=ship_validator,
            git_runner=getattr(dispatcher, "_git_runner", None),
            coordinator_root=coordinator_root,
            trusted_terminal=True,
        )
    except Exception:
        current = registry.get_workflow_run(run.run_id)
        registry._manager_update_workflow_run(
            run.run_id,
            facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
            gate_status="running",
        )
        raise
    updated = registry.get_workflow_run(run.run_id)
    if "needs_human" in updated.facets:
        return result
    next_job = dispatch_or_stop(updated)
    if next_job is not None:
        result["job_id"] = next_job["job_id"]
    return result


def apply_workflow_action(
    registry,
    *,
    args: Mapping[str, object],
    identity_registry: IdentityRegistry | None = None,
    probes: Mapping[tuple[str, str], CapabilityProbe] | None = None,
    primary_questioner: Callable[[Mapping[str, object]], object] | None = None,
    secondary_planner: Callable[[Mapping[str, object], object], object] | None = None,
    primary_integrator: Callable[[Mapping[str, object], Mapping[str, object]], object] | None = None,
    runtime_factory: Callable[..., object] | None = None,
    ship_validator: Callable[..., object] | None = None,
    git_runner=None,
    coordinator_root: str | Path | None = None,
    trusted_terminal: bool = False,
) -> dict[str, object]:
    """Apply the sole production mutation API for Manager-owned workflows.

    Callers reach this function through the durable control queue. Registry
    mutation methods are intentionally private so CLI/socket clients cannot
    bypass Manager orchestration.
    """

    action = _required_workflow_string(args, "action")

    def validate_ship_result(value: object, *, candidate: str | None) -> tuple[str, dict]:
        if not isinstance(value, dict) or value.get("trusted") is not True:
            raise ValueError("ship validator returned no trusted result")
        status = value.get("status")
        if (
            status not in {"pending", "passed", "needs_human"}
            or not isinstance(candidate, str)
            or value.get("head") != candidate
            or value.get("commit_id") != candidate
            or not isinstance(value.get("ref"), str)
            or not value["ref"]
            or not isinstance(value.get("hash"), str)
            or len(value["hash"]) != 64
        ):
            raise ValueError("ship validator current-HEAD result invalid")
        normalized = dict(value)
        normalized.setdefault("review_kind", "copilot")
        normalized.setdefault("review_ref", value.get("ref"))
        normalized.setdefault("review_hash", value.get("hash"))
        if (
            normalized["review_kind"] not in {"copilot", "maintainer-review"}
            or not isinstance(normalized["review_ref"], str)
            or not normalized["review_ref"]
            or not isinstance(normalized["review_hash"], str)
            or len(normalized["review_hash"]) != 64
        ):
            raise ValueError("ship validator delivery review result invalid")
        completion = normalized.get("completion")
        if status == "passed" and completion is not None:
            if (
                not isinstance(completion, dict)
                or set(completion)
                != {
                    "record_path",
                    "record_hash",
                    "record_revision",
                    "source_revisions",
                    "pr_candidate",
                    "merge_revision",
                }
                or completion.get("record_revision") != candidate
                or completion.get("pr_candidate") != candidate
                or not isinstance(completion.get("record_path"), str)
                or not isinstance(completion.get("record_hash"), str)
                or len(completion["record_hash"]) != 64
                or not isinstance(completion.get("source_revisions"), dict)
                or not completion["source_revisions"]
                or not isinstance(completion.get("merge_revision"), str)
                or verification.SAFE_SHA_RE.fullmatch(completion["merge_revision"])
                is None
            ):
                raise ValueError("ship validator completion binding invalid")
        return str(status), normalized

    if action == "refresh-completion":
        if not trusted_terminal:
            raise ValueError("workflow completion refresh is internal to terminal polling")
        run_id = _required_workflow_string(args, "run_id")
        current = registry.get_workflow_run(run_id)
        if current.current_phase != "ship" or current.status != "done":
            raise ValueError("workflow completion refresh requires a done ship run")
        if ship_validator is None:
            return {
                "run_id": current.run_id,
                "current_phase": current.current_phase,
                "reason": "ship-validator-unavailable",
            }
        status, trusted = validate_ship_result(
            ship_validator(run=current, candidate=current.candidate_head),
            candidate=current.candidate_head,
        )
        if status != "passed":
            return {
                "run_id": current.run_id,
                "current_phase": current.current_phase,
                "reason": trusted.get("reason")
                or ("delivery-in-progress" if status == "pending" else "delivery-needs-human"),
            }
        completion_binding = trusted.get("completion")
        if completion_binding is None:
            raise ValueError("workflow completion refresh requires completion binding")
        if coordinator_root is None:
            raise ValueError("workflow ship audit root unavailable")
        refs = {item.kind: item for item in current.gate_refs}
        review_kind = trusted["review_kind"]
        refs.pop("maintainer-review" if review_kind == "copilot" else "copilot", None)
        refs[review_kind] = GateEvidenceRef(
            review_kind, trusted["review_ref"], trusted["review_hash"]
        )
        updated = registry._manager_update_workflow_run(
            run_id,
            steps=_validated_ship_steps(
                registry,
                run=current,
                candidate=str(current.candidate_head),
                coordinator_root=coordinator_root,
            ),
            gate_refs=tuple(
                refs[kind]
                for kind in ("brainstorm", "foreign-review", "copilot", "maintainer-review")
                if kind in refs
            ),
            gate_status="passed",
            facets=(),
            status="done",
            completion_record_path=completion_binding["record_path"],
            completion_record_hash=completion_binding["record_hash"],
            completion_record_revision=completion_binding["record_revision"],
            completion_source_revisions=completion_binding["source_revisions"],
            pr_candidate=completion_binding["pr_candidate"],
            merge_revision=completion_binding["merge_revision"],
        )
        return {
            "run_id": updated.run_id,
            "current_phase": updated.current_phase,
            "reason": "completion-refreshed",
        }

    if action == "advance":
        if not trusted_terminal:
            raise ValueError("workflow advance is internal to terminal polling")
        run_id = _required_workflow_string(args, "run_id")
        current = registry.get_workflow_run(run_id)
        card_id = _required_workflow_string(args, "card_id")
        matches = [step for step in current.steps if step.card == card_id]
        if len(matches) != 1 or matches[0].phase != current.current_phase:
            raise ValueError("workflow card is not in current phase")
        step = matches[0]
        if step.gate_result == "passed":
            if current.current_phase == "review" and args.get("current_phase") == "ship":
                if args.get("gate_refs"):
                    raise ValueError("local Copilot evidence is never trusted")
                if ship_validator is None:
                    updated = registry._manager_update_workflow_run(
                        run_id, facets=("needs_human",), gate_status="running"
                    )
                    return {
                        "run_id": updated.run_id,
                        "current_phase": updated.current_phase,
                        "reason": "ship-validator-unavailable",
                    }
                status, trusted = validate_ship_result(
                    ship_validator(run=current, candidate=current.candidate_head),
                    candidate=current.candidate_head,
                )
                if status == "pending":
                    persisted = registry.get_workflow_run(run_id)
                    if (
                        persisted.current_phase != current.current_phase
                        or persisted.candidate_head != current.candidate_head
                    ):
                        return {
                            "run_id": persisted.run_id,
                            "current_phase": persisted.current_phase,
                            "reason": trusted.get("reason") or "delivery-in-progress",
                        }
                    registry._manager_update_workflow_run(
                        run_id, facets=(), gate_status="running"
                    )
                    return {
                        "run_id": current.run_id,
                        "current_phase": "review",
                        "reason": "delivery-in-progress",
                    }
                if status == "needs_human":
                    updated = registry._manager_update_workflow_run(
                        run_id, facets=("needs_human",), gate_status="running"
                    )
                    return {
                        "run_id": updated.run_id,
                        "current_phase": updated.current_phase,
                        "reason": trusted.get("reason") or "delivery-needs-human",
                    }
                refs = {item.kind: item for item in current.gate_refs}
                review_kind = trusted["review_kind"]
                refs.pop("maintainer-review" if review_kind == "copilot" else "copilot", None)
                refs[review_kind] = GateEvidenceRef(
                    review_kind, trusted["review_ref"], trusted["review_hash"]
                )
                ship_steps = current.steps
                if trusted.get("completion") is not None:
                    if coordinator_root is None:
                        raise ValueError("workflow ship audit root unavailable")
                    ship_steps = _validated_ship_steps(
                        registry,
                        run=current,
                        candidate=str(current.candidate_head),
                        coordinator_root=coordinator_root,
                    )
                updated = registry._manager_update_workflow_run(
                    run_id,
                    current_phase="ship",
                    steps=ship_steps,
                    gate_refs=tuple(
                        refs[kind]
                        for kind in ("brainstorm", "foreign-review", "copilot", "maintainer-review")
                        if kind in refs
                    ),
                    gate_status="passed",
                    facets=(),
                    status=("done" if trusted.get("completion") is not None else None),
                    completion_record_path=(
                        trusted["completion"]["record_path"]
                        if trusted.get("completion") is not None
                        else None
                    ),
                    completion_record_hash=(
                        trusted["completion"]["record_hash"]
                        if trusted.get("completion") is not None
                        else None
                    ),
                    completion_record_revision=(
                        trusted["completion"]["record_revision"]
                        if trusted.get("completion") is not None
                        else None
                    ),
                    completion_source_revisions=(
                        trusted["completion"]["source_revisions"]
                        if trusted.get("completion") is not None
                        else None
                    ),
                    pr_candidate=(
                        trusted["completion"]["pr_candidate"]
                        if trusted.get("completion") is not None
                        else None
                    ),
                    merge_revision=(
                        trusted["completion"]["merge_revision"]
                        if trusted.get("completion") is not None
                        else None
                    ),
                )
                return {"run_id": updated.run_id, "current_phase": "ship", "reason": None}
            raise ValueError("workflow card evidence replay rejected")
        identities = identity_registry or load_model_identities()
        job, identity = _job_for_workflow_card(
            registry,
            run=current,
            card_id=card_id,
            job_id=args.get("job_id"),
            expected_persona=step.persona,
            identities=identities,
        )
        if coordinator_root is None:
            raise ValueError("workflow canonical coordinator root unavailable")
        evidence, outputs, evidence_path, evidence_hash = _read_job_workflow_evidence(
            job,
            run=current,
            coordinator_root=coordinator_root,
        )
        candidate = current.candidate_head
        if current.current_phase == "build":
            candidate = _verify_build_candidate_transition(
                job,
                previous_candidate=candidate,
                git_runner=git_runner,
            )
        elif current.current_phase in {"verify", "review"}:
            job_candidate = _verify_exact_candidate(job, git_runner=git_runner)
            if candidate != job_candidate:
                raise ValueError("workflow card candidate mismatch")
            candidate = job_candidate
        builder_domains = {
            item.domain
            for item in current.steps
            if item.phase == "build" and item.gate_result == "passed" and item.domain is not None
        }
        if current.current_phase in {"verify", "review"} and identity.independence_domain in builder_domains:
            raise ValueError("workflow reviewer must use a foreign independence domain")
        by_kind = {item.kind: item for item in current.gate_refs}
        verified = current.verified_head
        review_state: str | None = None
        if current.current_phase == "verify":
            report_outputs = evidence.get("outputs")
            evidence_payload = dict(evidence)
            evidence_payload.pop("outputs", None)
            evidence = verification.validate_verification_evidence(evidence_payload)
            evidence["outputs"] = report_outputs
            if (
                evidence.get("slice_id") != f"{current.run_id}-{card_id}"
                or evidence.get("candidate") != candidate
                or evidence.get("status") not in {"verified", "reviewing"}
            ):
                raise ValueError("verification evidence workflow/card/candidate mismatch")
            verified = candidate
        if current.current_phase == "review":
            evidence_payload = dict(evidence)
            evidence_payload.pop("outputs", None)
            evaluation = foreign_review.validate_gate_evaluation(evidence_payload)
            if (
                evaluation.get("slice_id") != f"{current.run_id}-{card_id}"
                or evaluation.get("candidate") != candidate
                or evaluation.get("state") not in {"passed", "rejected"}
                or evaluation.get("reviewer_job_id") != job.get("job_id")
            ):
                raise ValueError("review evaluation workflow/card/candidate mismatch")
            review_state = str(evaluation["state"])
            builder_job_id = evaluation.get("builder_job_id")
            _builder_job, builder_identity = _review_builder_job(
                registry,
                run=current,
                builder_job_id=builder_job_id,
                candidate=str(candidate),
                identities=identities,
            )
            launch_identity = evaluation.get("launch_identity", {})
            if (
                launch_identity.get("builder") != builder_identity.legacy_dict()
                or launch_identity.get("reviewer") != identity.legacy_dict()
                or builder_identity.independence_domain == identity.independence_domain
            ):
                raise ValueError("review evaluation identity/domain mismatch")
            foreign = GateEvidenceRef("foreign-review", evidence_path, evidence_hash)
            by_kind[foreign.kind] = foreign
        if step.outputs and not outputs:
            raise ValueError("workflow card declares outputs but no verified artifact was supplied")
        updated_steps = _audit_phase_steps(
            current.steps,
            phase=current.current_phase,
            executor=identity.executor,
            model=identity.model_id,
            domain=identity.independence_domain,
            outputs=outputs,
            gate_result=("needs_human" if review_state == "rejected" else "passed"),
            card_id=card_id,
        )
        if review_state == "rejected":
            updated = registry._manager_update_workflow_run(
                run_id,
                current_phase=current.current_phase,
                steps=updated_steps,
                gate_refs=tuple(
                    by_kind[kind]
                    for kind in (
                        "brainstorm", "foreign-review", "copilot", "maintainer-review",
                    )
                    if kind in by_kind
                ),
                gate_status="failed",
                candidate_head=candidate,
                verified_head=verified,
                facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
            )
            return {
                "run_id": updated.run_id,
                "current_phase": updated.current_phase,
                "reason": "blocking-findings",
            }
        phase_done = all(
            item.gate_result == "passed"
            for item in updated_steps
            if item.phase == current.current_phase
        )
        requested_phase = _required_workflow_string(args, "current_phase")
        if not phase_done:
            if requested_phase != current.current_phase:
                raise ValueError("workflow phase still has incomplete cards")
            next_phase = current.current_phase
        else:
            next_phase = requested_phase
            validate_workflow_phase_transition(current.current_phase, next_phase)
        facets = current.facets
        gate_status = current.gate_status
        if current.current_phase == "review" and phase_done and next_phase == "ship":
            if ship_validator is None:
                next_phase = "review"
                facets = ("needs_human",)
                gate_status = "running"
            else:
                status, trusted = validate_ship_result(
                    ship_validator(run=current, candidate=candidate),
                    candidate=candidate,
                )
                if status == "passed":
                    review_kind = trusted["review_kind"]
                    by_kind.pop("maintainer-review" if review_kind == "copilot" else "copilot", None)
                    by_kind[review_kind] = GateEvidenceRef(
                        review_kind, trusted["review_ref"], trusted["review_hash"]
                    )
                    gate_status = "passed"
                    facets = ()
                elif status == "pending":
                    next_phase = "review"
                    gate_status = "running"
                    facets = ()
                else:
                    next_phase = "review"
                    gate_status = "running"
                    facets = ("needs_human",)
        updated = registry._manager_update_workflow_run(
            run_id,
            current_phase=next_phase,
            steps=(
                _validated_ship_steps(
                    registry,
                    run=current,
                    candidate=str(candidate),
                    coordinator_root=coordinator_root,
                )
                if current.current_phase == "review"
                and phase_done
                and next_phase == "ship"
                and trusted.get("completion") is not None
                else updated_steps
            ),
            gate_refs=tuple(
                by_kind[kind]
                for kind in ("brainstorm", "foreign-review", "copilot", "maintainer-review")
                if kind in by_kind
            ),
            gate_status=gate_status,
            candidate_head=candidate,
            verified_head=verified,
            facets=facets,
            status=(
                "done"
                if current.current_phase == "review"
                and phase_done
                and next_phase == "ship"
                and trusted.get("completion") is not None
                else None
            ),
            completion_record_path=(
                trusted["completion"]["record_path"]
                if current.current_phase == "review"
                and phase_done
                and next_phase == "ship"
                and trusted.get("completion") is not None
                else None
            ),
            completion_record_hash=(
                trusted["completion"]["record_hash"]
                if current.current_phase == "review"
                and phase_done
                and next_phase == "ship"
                and trusted.get("completion") is not None
                else None
            ),
            completion_record_revision=(
                trusted["completion"]["record_revision"]
                if current.current_phase == "review"
                and phase_done
                and next_phase == "ship"
                and trusted.get("completion") is not None
                else None
            ),
            completion_source_revisions=(
                trusted["completion"]["source_revisions"]
                if current.current_phase == "review"
                and phase_done
                and next_phase == "ship"
                and trusted.get("completion") is not None
                else None
            ),
            pr_candidate=(
                trusted["completion"]["pr_candidate"]
                if current.current_phase == "review"
                and phase_done
                and next_phase == "ship"
                and trusted.get("completion") is not None
                else None
            ),
            merge_revision=(
                trusted["completion"]["merge_revision"]
                if current.current_phase == "review"
                and phase_done
                and next_phase == "ship"
                and trusted.get("completion") is not None
                else None
            ),
        )
        reason = (
            "ship-validator-unavailable"
            if current.current_phase == "review" and phase_done and requested_phase == "ship" and ship_validator is None
            else (
                "delivery-in-progress"
                if current.current_phase == "review"
                and phase_done
                and requested_phase == "ship"
                and ship_validator is not None
                and next_phase == "review"
                and not facets
                else (
                    str(trusted.get("reason") or "delivery-needs-human")
                    if current.current_phase == "review"
                    and phase_done
                    and requested_phase == "ship"
                    and ship_validator is not None
                    and facets == ("needs_human",)
                    else None
                )
            )
        )
        return {"run_id": updated.run_id, "current_phase": updated.current_phase, "reason": reason}
    if action != "start":
        raise ValueError(f"unsupported workflow action: {action}")

    manifest = _load_workflow_manifest(_required_workflow_string(args, "manifest_path"))
    manifest.validate_manager_spine()
    run = registry._manager_create_workflow_run(
        work_id=_required_workflow_string(args, "work_id"),
        repo=_required_workflow_string(args, "repo"),
        claim_key=_required_workflow_string(args, "claim_key"),
        source_revision=_required_workflow_string(args, "source_revision"),
        workspace_root=str(Path(_required_workflow_string(args, "artifact_root")).resolve()),
        combo=manifest.combo,
        current_phase="claim",
        issue_refs=tuple(args.get("issue_refs", ())),
        openspec_refs=tuple(args.get("openspec_refs", ())),
        pr_refs=tuple(args.get("pr_refs", ())),
        attempts={"claim": 1},
        steps=_audit_phase_steps(
            manifest.steps,
            phase="claim",
            executor="cortex-manager",
            model="deterministic",
            domain="cortex",
            outputs=(),
        ),
        gate_status="running",
    )
    artifact_root = Path(_required_workflow_string(args, "artifact_root")).resolve()
    transaction_root = (
        Path(coordinator_root).resolve()
        if coordinator_root is not None
        else Path(_required_workflow_string(args, "evidence_dir")).resolve().parent
    )
    try:
        _PlanningPublicationTransaction.reconcile(
            root=artifact_root,
            journal_root=transaction_root,
            run=run,
        )
    except PlanningPublicationDrift:
        run = registry._manager_update_workflow_run(
            run.run_id, facets=("needs_human",), gate_status="running"
        )
        return {
            "run_id": run.run_id,
            "current_phase": run.current_phase,
            "reason": "planning-publication-drift",
        }
    if run.current_phase not in {"claim", "define"}:
        return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": "already-claimed"}
    if run.current_phase == "claim":
        run = registry._manager_update_workflow_run(
            run.run_id,
            current_phase="define",
            attempts={**run.attempts, "define": 1},
        )
    artifacts, authority = _load_planning_artifacts(
        args,
        work_id=run.work_id,
        persisted=run.planning_authority,
    )
    if not run.planning_authority and authority:
        run = registry._manager_update_workflow_run(
            run.run_id,
            planning_authority=authority,
        )
    report = assess_planning_completeness(artifacts)
    if report.complete:
        primary_executor = str(args.get("primary_executor") or "cortex-manager")
        primary_model = str(args.get("primary_model") or "deterministic")
        primary_domain = str(args.get("primary_domain") or "cortex")
        run = registry._manager_update_workflow_run(
            run.run_id,
            current_phase="plan",
            attempts={**run.attempts, "plan": 1},
            steps=_audit_phase_steps(
                run.steps,
                phase="define",
                executor=primary_executor,
                model=primary_model,
                domain=primary_domain,
                outputs=tuple(artifact.ref for artifact in artifacts),
            ),
            brainstorm_required=False,
            primary_domain=primary_domain,
            facets=(),
        )
        return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": "planning-complete"}

    primary = (
        _required_workflow_string(args, "primary_executor"),
        _required_workflow_string(args, "primary_model"),
    )
    if (
        runtime_factory is not None
        and (primary_questioner is None or secondary_planner is None or primary_integrator is None)
    ):
        try:
            runtime = runtime_factory(
                primary=primary,
                worktree=_required_workflow_string(args, "artifact_root"),
            )
        except Exception:
            run = registry._manager_update_workflow_run(
                run.run_id, facets=("needs_human",), brainstorm_required=True
            )
            return {
                "run_id": run.run_id,
                "current_phase": run.current_phase,
                "reason": "planning-runtime-initialization-failed",
            }
        identity_registry = runtime.identity_registry
        probes = runtime.probes
        primary_questioner = runtime.primary_questioner
        secondary_planner = runtime.secondary_planner
        primary_integrator = runtime.primary_integrator
    if primary_questioner is None or secondary_planner is None or primary_integrator is None:
        run = registry._manager_update_workflow_run(
            run.run_id, facets=("needs_human",), brainstorm_required=True
        )
        return {
            "run_id": run.run_id,
            "current_phase": run.current_phase,
            "reason": "planning-runtime-unavailable",
        }

    identities = identity_registry or load_model_identities()
    publication = _PlanningPublicationTransaction(
        root=artifact_root,
        run_id=run.run_id,
        journal_root=transaction_root,
    )
    result = run_heterogeneous_brainstorm(
        report=report,
        primary=primary,
        registry=identities,
        probes=probes or {},
        evidence_dir=_required_workflow_string(args, "evidence_dir"),
        artifact_root=_required_workflow_string(args, "artifact_root"),
        scope=PlanningScope(
            repo=run.repo,
            work_id=run.work_id,
            source_revision=_required_workflow_string(args, "source_revision"),
        ),
        primary_questioner=primary_questioner,
        secondary_planner=secondary_planner,
        primary_integrator=primary_integrator,
        artifact_writer=lambda rows: _publish_planning_artifacts(
            _required_workflow_string(args, "artifact_root"),
            rows,
            work_id=run.work_id,
            allowed_refs=tuple(
                ref for step in manifest.steps for ref in step.outputs
            ),
            authorities=run.planning_authority,
            transaction=publication,
        ),
        evidence_writer=publication.write_evidence,
    )
    if result.state != "ready" or result.gate_refs.brainstorm_peer is None:
        publication.rollback()
        run = registry._manager_update_workflow_run(
            run.run_id, facets=("needs_human",), brainstorm_required=True
        )
        return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": result.reason}
    try:
        planning_authority, planning_source_revision = _validated_brainstorm_planning_authority(
            run,
            coordinator_root=transaction_root,
            brainstorm_ref=result.gate_refs.brainstorm_peer,
        )
        run = registry._manager_update_workflow_run(
            run.run_id,
            current_phase="plan",
            attempts={**run.attempts, "plan": 1},
            gate_refs=result.gate_refs.as_tuple(),
            planning_authority=planning_authority,
            planning_source_revision=planning_source_revision,
            brainstorm_required=True,
            primary_domain=identities.require(*primary).independence_domain,
            steps=_audit_phase_steps(
                run.steps,
                phase="define",
                executor=primary[0],
                model=primary[1],
                domain=identities.require(*primary).independence_domain,
                outputs=tuple(
                    ref
                    for resolution in (result.integration or {}).get("resolutions", [])
                    if isinstance(resolution, dict)
                    for ref in resolution.get("artifact_refs", [])
                    if isinstance(ref, str)
                ),
            ),
            facets=(),
        )
    except BaseException:
        persisted = registry.get_workflow_run(run.run_id)
        expected = publication.expected_gate_ref
        committed = False
        if expected is not None:
            try:
                expected_ref = GateEvidenceRef.from_dict(expected)
            except ValueError:
                expected_ref = None
            committed = expected_ref is not None and any(
                ref == expected_ref for ref in persisted.gate_refs
            )
        if committed:
            _PlanningPublicationTransaction.reconcile(
                root=artifact_root,
                journal_root=transaction_root,
                run=persisted,
            )
        else:
            publication.rollback()
        raise
    publication.commit()
    return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": "brainstorm-complete"}


def apply_work_action(*, args, requested_by, registry=None, runtime_factory=None):
    """唯一 production mutation seam；daemon control request 之外不直接呼叫。"""
    from .work_actions import execute_work_action
    from .registry import JobRegistry
    from .work_bridge import start_canonical_workflow

    active_registry = registry or JobRegistry()
    state_path = getattr(active_registry, "_state_path", None)
    coordinator_root = (
        Path(state_path).resolve().parent if state_path is not None else paths.coordinator_root().resolve()
    )

    def starter(authority, claim_key, reason):
        return start_canonical_workflow(
            registry=active_registry,
            authority=authority,
            claim_key=claim_key,
            coordinator_root=coordinator_root,
            explicit_repo_root=args.get("repo_root"),
            runtime_factory=runtime_factory or planning_runtime.build_production_planning_runtime,
            needs_human_reason=reason,
        )

    return execute_work_action(
        args=args,
        requested_by=requested_by,
        workflow_registry=active_registry,
        workflow_starter=starter,
    )


def run_auto_claim_scan(*, registry=None, runtime_factory=None):
    """Periodic Manager-owned durable work claim projection."""
    from .work_actions import run_auto_claim_scan as scan
    from .registry import JobRegistry
    from .work_bridge import start_canonical_workflow

    active_registry = registry or JobRegistry()
    state_path = getattr(active_registry, "_state_path", None)
    coordinator_root = (
        Path(state_path).resolve().parent if state_path is not None else paths.coordinator_root().resolve()
    )

    def starter(authority, claim_key, reason):
        return start_canonical_workflow(
            registry=active_registry,
            authority=authority,
            claim_key=claim_key,
            coordinator_root=coordinator_root,
            runtime_factory=runtime_factory or planning_runtime.build_production_planning_runtime,
            needs_human_reason=reason,
        )

    return scan(workflow_registry=active_registry, workflow_starter=starter)
