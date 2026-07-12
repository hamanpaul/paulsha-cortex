from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from ..lib import idle
from ..persona import gate, handoff
from . import autonomy
from . import completion
from . import review as foreign_review
from . import verification

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
