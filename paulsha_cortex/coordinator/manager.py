from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from ..lib import idle
from ..persona import gate, handoff
from . import autonomy
from . import completion
from . import review as foreign_review
from . import verification
from .model_identities import CapabilityProbe, IdentityRegistry, load_model_identities
from .planning import (
    PlanningArtifact,
    PlanningScope,
    assess_planning_completeness,
    run_heterogeneous_brainstorm,
)
from .workflow import GateEvidenceRef, WorkflowManifest, validate_workflow_phase_transition

IN_FLIGHT_STATUSES = frozenset({"dispatched", "running"})
TERMINAL_STATUSES = frozenset({"exited", "failed"})
VERIFICATION_RESULT_STATES = frozenset({"needs_human", "reviewing", "verified"})
SLICE_ACTIONS = frozenset({"retry-build", "retry-verify", "retry-review", "abandon"})


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


def _load_planning_artifacts(args: Mapping[str, object]) -> tuple[PlanningArtifact, ...]:
    root = Path(_required_workflow_string(args, "artifact_root")).resolve()
    rows = args.get("planning_artifacts")
    if not isinstance(rows, list):
        raise ValueError("workflow-action planning_artifacts must be a list")
    artifacts: list[PlanningArtifact] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"kind", "ref"}:
            raise ValueError(f"workflow-action planning_artifacts[{index}] invalid")
        kind = row.get("kind")
        ref = row.get("ref")
        if not isinstance(kind, str) or not isinstance(ref, str) or not ref:
            raise ValueError(f"workflow-action planning_artifacts[{index}] invalid")
        relative = Path(ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"workflow-action planning_artifacts[{index}] escapes artifact_root")
        try:
            unresolved = root / relative
            if unresolved.is_symlink():
                raise ValueError("symlink planning artifact")
            resolved = unresolved.resolve()
            resolved.relative_to(root)
            text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"workflow planning artifact unreadable: {ref}") from exc
        artifacts.append(PlanningArtifact(kind=kind, ref=ref, text=text))
    return tuple(artifacts)


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
    worktree = job.get("worktree")
    if (
        not isinstance(candidate, str)
        or verification.SAFE_SHA_RE.fullmatch(candidate) is None
        or not isinstance(worktree, str)
    ):
        raise ValueError("workflow job candidate/worktree missing")
    runner = git_runner or subprocess.run
    common = {"capture_output": True, "text": True, "check": False}
    exists = runner(["git", "-C", worktree, "cat-file", "-e", f"{candidate}^{{commit}}"], **common)
    if getattr(exists, "returncode", 1) != 0:
        raise ValueError("workflow candidate does not exist")
    head = runner(["git", "-C", worktree, "rev-parse", "HEAD"], **common)
    if getattr(head, "returncode", 1) != 0 or getattr(head, "stdout", "").strip().lower() != candidate:
        raise ValueError("workflow candidate is not exact worktree HEAD")
    return candidate


def _review_builder_job(
    registry,
    *,
    run,
    builder_job_id: object,
    candidate: str,
    identities: IdentityRegistry,
) -> tuple[dict[str, object], object]:
    if not isinstance(builder_job_id, str) or not builder_job_id:
        raise ValueError("review evaluation builder job missing")
    builder = registry.get_job(builder_job_id)
    expected = {
        "workflow_run_id": run.run_id,
        "workflow_claim_key": run.claim_key,
        "workflow_repo": run.repo,
        "source_revision": run.source_revision,
        "persona": "builder",
        "subject_head": candidate,
        "status": "exited",
        "exit_code": 0,
    }
    for field, value in expected.items():
        if builder.get(field) != value:
            raise ValueError(f"review evaluation builder binding mismatch: {field}")
    card = builder.get("workflow_card")
    if not isinstance(card, str) or not any(
        step.phase == "build" and step.card == card and step.gate_result == "passed"
        for step in run.steps
    ):
        raise ValueError("review evaluation builder card is not passed")
    executor = builder.get("executor")
    model = builder.get("model_id")
    if not isinstance(executor, str) or not isinstance(model, str):
        raise ValueError("review evaluation builder identity missing")
    identity = identities.require(executor, model)
    if builder.get("independence_domain") != identity.independence_domain:
        raise ValueError("review evaluation builder identity/domain mismatch")
    return builder, identity


def _read_trusted_artifact_ref(locator: object, validator) -> tuple[dict[str, object], str, str]:
    if not isinstance(locator, dict):
        raise ValueError("trusted evidence ref must be an object")
    ref = locator.get("path")
    expected_hash = locator.get("hash")
    if not isinstance(ref, str) or not isinstance(expected_hash, str):
        raise ValueError("trusted evidence ref requires path/hash")
    try:
        payload = json.loads(Path(ref).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("trusted evidence unreadable") from exc
    normalized = validator(payload)
    actual_hash = verification.canonical_json_hash(normalized)
    if actual_hash != expected_hash:
        raise ValueError("trusted evidence hash mismatch")
    return normalized, ref, actual_hash


def _verified_output_refs(args: Mapping[str, object], *, repo_root: Path) -> tuple[str, ...]:
    rows = args.get("artifacts", [])
    if not isinstance(rows, list):
        raise ValueError("workflow card artifacts must be a list")
    refs: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ValueError("workflow card artifact locator invalid")
        ref = row.get("path")
        digest = row.get("sha256")
        if not isinstance(ref, str) or not isinstance(digest, str):
            raise ValueError("workflow card artifact locator invalid")
        relative = Path(ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("workflow card artifact escapes repo")
        unresolved = repo_root / relative
        if unresolved.is_symlink():
            raise ValueError("workflow card artifact symlink rejected")
        path = unresolved.resolve()
        path.relative_to(repo_root)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("workflow card artifact hash mismatch")
        refs.append(ref)
    return tuple(refs)


def _write_planning_artifacts(root_value: str, rows: object) -> None:
    if not isinstance(rows, list):
        raise ValueError("planning artifacts must be a list")
    root = Path(root_value).resolve()
    prepared: list[tuple[Path, bytes]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"kind", "path", "content"}:
            raise ValueError("planning artifact schema invalid")
        path_value = row.get("path")
        content = row.get("content")
        if not isinstance(path_value, str) or not isinstance(content, str):
            raise ValueError("planning artifact path/content invalid")
        relative = Path(path_value)
        allowed = (
            relative.parts[:2] == ("docs", "superpowers")
            or relative.parts[:2] == ("openspec", "changes")
        )
        if relative.is_absolute() or ".." in relative.parts or not allowed or relative.suffix != ".md":
            raise ValueError("planning artifact path outside governed roots")
        unresolved = root / relative
        if unresolved.is_symlink():
            raise ValueError("planning artifact symlink rejected")
        path = unresolved.resolve()
        path.relative_to(root)
        prepared.append((path, content.encode("utf-8")))
    for path, content in prepared:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".planning.tmp")
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp.unlink(missing_ok=True)


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
) -> dict[str, object]:
    """Apply the sole production mutation API for Manager-owned workflows.

    Callers reach this function through the durable control queue. Registry
    mutation methods are intentionally private so CLI/socket clients cannot
    bypass Manager orchestration.
    """

    action = _required_workflow_string(args, "action")
    if action == "advance":
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
                trusted = ship_validator(run=current, candidate=current.candidate_head)
                if not isinstance(trusted, dict) or trusted.get("trusted") is not True:
                    raise ValueError("ship validator returned no trusted result")
                if (
                    trusted.get("status") != "passed"
                    or trusted.get("head") != current.candidate_head
                    or trusted.get("commit_id") != current.candidate_head
                    or not isinstance(trusted.get("ref"), str)
                    or not isinstance(trusted.get("hash"), str)
                ):
                    raise ValueError("ship validator current-HEAD result invalid")
                refs = {item.kind: item for item in current.gate_refs}
                refs["copilot"] = GateEvidenceRef("copilot", trusted["ref"], trusted["hash"])
                updated = registry._manager_update_workflow_run(
                    run_id,
                    current_phase="ship",
                    gate_refs=tuple(
                        refs[kind] for kind in ("brainstorm", "foreign-review", "copilot") if kind in refs
                    ),
                    gate_status="passed",
                    facets=(),
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
        candidate = current.candidate_head
        if current.current_phase in {"build", "verify", "review"}:
            job_candidate = _verify_exact_candidate(job, git_runner=git_runner)
            if candidate is not None and candidate != job_candidate:
                raise ValueError("workflow card candidate mismatch")
            candidate = job_candidate
        builder_domains = {
            item.domain
            for item in current.steps
            if item.phase == "build" and item.gate_result == "passed" and item.domain is not None
        }
        if current.current_phase in {"verify", "review"} and identity.independence_domain in builder_domains:
            raise ValueError("workflow reviewer must use a foreign independence domain")
        outputs = _verified_output_refs(
            args, repo_root=Path(_required_workflow_string(args, "repo_root")).resolve()
        )
        by_kind = {item.kind: item for item in current.gate_refs}
        verified = current.verified_head
        if current.current_phase == "verify":
            evidence, evidence_path, evidence_hash = _read_trusted_artifact_ref(
                args.get("verification_ref"), verification.validate_verification_evidence
            )
            if (
                evidence.get("slice_id") != f"{current.run_id}-{card_id}"
                or evidence.get("candidate") != candidate
                or evidence.get("status") not in {"verified", "reviewing"}
            ):
                raise ValueError("verification evidence workflow/card/candidate mismatch")
            outputs = outputs + (evidence_path,)
            verified = candidate
        if current.current_phase == "review":
            evaluation, evidence_path, evidence_hash = _read_trusted_artifact_ref(
                args.get("review_ref"), foreign_review.validate_gate_evaluation
            )
            if (
                evaluation.get("slice_id") != f"{current.run_id}-{card_id}"
                or evaluation.get("candidate") != candidate
                or evaluation.get("state") != "passed"
                or evaluation.get("reviewer_job_id") != job.get("job_id")
            ):
                raise ValueError("review evaluation workflow/card/candidate mismatch")
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
            outputs = outputs + (evidence_path,)
        if step.outputs and not outputs:
            raise ValueError("workflow card declares outputs but no verified artifact was supplied")
        updated_steps = _audit_phase_steps(
            current.steps,
            phase=current.current_phase,
            executor=identity.executor,
            model=identity.model_id,
            domain=identity.independence_domain,
            outputs=outputs,
            card_id=card_id,
        )
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
                trusted = ship_validator(run=current, candidate=candidate)
                if not isinstance(trusted, dict) or trusted.get("trusted") is not True:
                    raise ValueError("ship validator returned no trusted result")
                if (
                    trusted.get("status") != "passed"
                    or trusted.get("head") != candidate
                    or trusted.get("commit_id") != candidate
                    or not isinstance(trusted.get("ref"), str)
                    or not isinstance(trusted.get("hash"), str)
                ):
                    raise ValueError("ship validator current-HEAD result invalid")
                by_kind["copilot"] = GateEvidenceRef(
                    "copilot", trusted["ref"], trusted["hash"]
                )
                gate_status = "passed"
                facets = ()
        updated = registry._manager_update_workflow_run(
            run_id,
            current_phase=next_phase,
            steps=updated_steps,
            gate_refs=tuple(by_kind[kind] for kind in ("brainstorm", "foreign-review", "copilot") if kind in by_kind),
            gate_status=gate_status,
            candidate_head=candidate,
            verified_head=verified,
            facets=facets,
        )
        reason = (
            "ship-validator-unavailable"
            if current.current_phase == "review" and phase_done and requested_phase == "ship" and ship_validator is None
            else None
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
    if run.current_phase != "claim":
        return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": "already-claimed"}
    run = registry._manager_update_workflow_run(
        run.run_id,
        current_phase="define",
        attempts={**run.attempts, "define": 1},
    )
    artifacts = _load_planning_artifacts(args)
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
        artifact_writer=lambda rows: _write_planning_artifacts(
            _required_workflow_string(args, "artifact_root"), rows
        ),
    )
    if result.state != "ready" or result.gate_refs.brainstorm_peer is None:
        run = registry._manager_update_workflow_run(
            run.run_id, facets=("needs_human",), brainstorm_required=True
        )
        return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": result.reason}
    run = registry._manager_update_workflow_run(
        run.run_id,
        current_phase="plan",
        attempts={**run.attempts, "plan": 1},
        gate_refs=result.gate_refs.as_tuple(),
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
    return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": "brainstorm-complete"}


def apply_work_action(*, args, requested_by):
    """唯一 production mutation seam；daemon control request 之外不直接呼叫。"""
    from .work_actions import execute_work_action

    return execute_work_action(args=args, requested_by=requested_by)
