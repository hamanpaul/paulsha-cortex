from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import logging
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from paulsha_cortex.config import paths

from .._yaml import YAMLError, safe_load
from ..lib import idle
from ..persona import gate, handoff
from . import autonomy
from . import candidate_base
from . import completion
from . import coverage
from . import gate_ledger
from . import preflight
from . import job_runner
from . import job_workspace
from . import planning_runtime
from . import provider_backoff
from . import outcome_taxonomy
from . import provider_outcome
from . import seams
from . import review as foreign_review
from . import terminal_contract
from . import verification
from . import worktree_reclaim
from .spawn_admission import SpawnAdmissionLimiter, resolve_limiter, resolve_provider
from .registry import RETRY_CARD_PHASE_PERSONA, slice_repin_eligible
from ..config.paths import worktree_root_for
from .claim import (
    AuthorityValidationError,
    REASON_PROVIDER_RATE_LIMITED_CANONICAL,
    decomposition_route,
    needs_human_next_actions,
    needs_human_next_step_hint,
)
from . import model_resolution
from .diagnostics import DiagnosticReason, diagnostic_reason, summarize_exception
from .model_identities import (
    AGY_DOMAIN,
    AGY_LIVE_PROBE,
    AGY_MODEL_ID,
    CapabilityProbe,
    IdentityRegistry,
    ModelIdentity,
    is_environment_grade_rejection_reason,
    load_model_identities,
)
from .planning import (
    ACCEPTANCE_SURFACE_RULES,
    ArtifactAssessment,
    PlanningArtifact,
    PlanningScope,
    assess_planning_artifact,
    assess_planning_completeness,
    plan_review_gate,
    run_heterogeneous_brainstorm,
)
from .workflow import (
    BRAINSTORM_AUTHORITY_MISSING,
    WORKFLOW_PHASES,
    GateEvidenceRef,
    PlanningArtifactAuthority,
    WorkflowManifest,
    brainstorm_authority_bound,
    validate_workflow_phase_transition,
)

logger = logging.getLogger(__name__)

IN_FLIGHT_STATUSES = frozenset({"dispatched", "running"})
TERMINAL_STATUSES = frozenset({"exited", "failed"})

# workflow lane 的 phase job（issue #264）terminal manifest 語意：不查 slices
# 表，gate 依據就是 job 自身的 exit 結果——phase 的實際推進/gate 判定由
# workflow registry（_dispatch_workflow_card 重掃 list_jobs()）負責，跟這裡
# 寫的 handoff manifest 無關（manifest 只供 operator 視野／cockpit 展示用）。
# 用獨立的 gate_status/gate_reason，機械區分於 slice lane 的
# needs_human/missing-slice-proof，不得誤觸 needs_human 佇列。
WORKFLOW_LANE_GATE_STATUS = "workflow-tracked"
WORKFLOW_LANE_GATE_REASON = "workflow-lane-job"
VERIFICATION_RESULT_STATES = frozenset({"needs_human", "reviewing", "verified"})
SLICE_ACTIONS = frozenset({"retry-build", "retry-verify", "retry-review", "recover-pre-candidate", "abandon"})
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


def _supersede_handoff_manifest(
    *,
    handoff_dir: str,
    slice_id: str,
    action: str,
    actor: str,
    clock: Callable[[], str] = _utcnow,
) -> None:
    """操作者復原動作（recover-pre-candidate／abandon）後，替殘留 handoff manifest
    補上 superseded 稽核標記（issue #383）。

    `run_tick()`/`dispatch_gate_scan()` 的 fanout 放行判定改成與 registry 現況
    對帳（`_manifest_still_blocks_fanout`），本函式失敗與否都不影響「復原後
    下一輪 tick 能不能重派」這個驗收條件——這裡純粹補稽核可見性，讓直接檢視
    manifest 檔的人（非只看 tick 行為）也能看出這份終局紀錄已經過期，而不是
    誤以為它仍是這個 slice 的最新狀態。

    不刪檔（保留稽核紀錄）；只在既有 payload 補 `superseded_at`/`superseded_by`/
    `superseded_reason` 三欄後覆寫回同一路徑。manifest 不存在（尚未跑過任何
    job）／壞檔／symlink／已標記過，皆 best-effort no-op——復原是主動作，
    manifest 標記是次要動作，不得讓次要動作的失敗連帶讓復原本身 raise。
    """
    manifest_path = Path(handoff_dir) / f"{slice_id}.json"
    if manifest_path.is_symlink():
        return
    payload = _read_manifest_payload(manifest_path)
    if payload is None or payload.get("superseded_at") is not None:
        return
    payload = dict(payload)
    payload["superseded_at"] = clock()
    payload["superseded_by"] = actor
    payload["superseded_reason"] = action
    try:
        handoff.write_manifest(manifest_path, payload)
    except OSError:
        pass


def _reclaim_preserve_root(registry) -> Path:
    """#478：worktree 回收時 dirty 內容的封存落點（`<state 檔目錄>/evidence`）。

    與 `verification.write_verification_evidence`／`_recover_planning_record`
    同一個 coordinator 狀態根，operator 只要看同一棵 evidence 樹就找得到；
    registry 未帶 state path（測試 double）時退回 `paths.coordinator_root()`。
    """

    state_path = getattr(registry, "_state_path", None)
    if isinstance(state_path, (str, Path)):
        return Path(state_path).resolve().parent / "evidence"
    return paths.coordinator_root() / "evidence"


def _is_workflow_lane_job(job: dict) -> bool:
    """job 屬於 workflow lane 的判定（issue #264）。

    registry.create_job() 只有透過 workflow 派工路徑（_job_for_workflow_card /
    dispatch_workflow_card）才會帶 workflow_run_id；slice lane 的 job（經
    autonomy.dispatch_ready 派工）恆為 None。以此欄位機械區分兩條 lane，
    而不是靠查不到 slices 表就一律當成 slice lane 缺 proof。
    """
    return job.get("workflow_run_id") is not None


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


def _repo_root_for_slice_row(slice_row: Mapping | None) -> Path:
    """從 slice row 解析目標 repo 根——**沒有就 fail-closed**（#612）。

    舊實作在 `slice_row is None`（或推斷丟例外）時退回 `Path.cwd().resolve()`。
    daemon 的 `WorkingDirectory` 正是 operator 的真實 checkout，於是那條退路把
    「不知道目標」變成「打在 operator 的樹上」：`complete_tick` 的
    `_candidate_for_evidence`（`git rev-parse`）、`_completion_candidate_ref`
    （`git fetch --no-tags origin main`）、verification／review runner 全部跟著
    落在錯的 repo，而且一路「成功」。

    沒有 slice row 時退回 `paths.repo_root()`——它自 #612 起也是 fail-closed 的
    （`PSC_REPO_ROOT` 未宣告即拋 `RepoRootUnresolvedError`），因此這裡要嘛拿到
    operator 顯式宣告的目標，要嘛拋例外由呼叫端的錯誤通道記錄，不會再有第三種
    「靜默猜一個」的結局。
    """
    spec_path = None
    if isinstance(slice_row, Mapping):
        spec = slice_row.get("spec")
        if isinstance(spec, Mapping):
            spec_path = spec.get("path")
    if isinstance(spec_path, str) and spec_path:
        return autonomy._infer_repo_root(Path(spec_path))
    return paths.repo_root().resolve()


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
    registry.update_slice(
        slice_id,
        current_verification_evidence_hash=evidence["hash"],
        current_evidence_refs=refs,
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
    computed_hash = verification.canonical_json_hash(normalized)
    stored_hash = slice_row.get("current_verification_evidence_hash")
    if stored_hash is None:
        # Additive migration compatibility for callers holding an old detached
        # row: the registry writer will populate the durable field on the next
        # result application, while a valid legacy evidence ref remains usable.
        return path, computed_hash
    if not isinstance(stored_hash, str) or stored_hash != computed_hash:
        return path, None
    return path, stored_hash


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
    candidate = slice_row.get("candidate")
    valid_candidate = (
        isinstance(candidate, str)
        and verification.SAFE_SHA_RE.fullmatch(candidate) is not None
    )
    state = slice_row.get("state")
    if state == "failed":
        # retry-build 底層即 registry.repin_slice()。只有在 repin_slice()
        # 真的會接受目前 (state, gate_state) 組合時才宣告 retry-build——
        # slice_repin_eligible() 與 repin_slice() 共用同一張表／同一判準
        # （registry.REPINNABLE_SLICE_STATES / GATE_STATE_TRANSITIONS），
        # 讓「宣告的動作」與「mutation 端實際接受的動作」保持一致，不再宣告
        # 一個保證失敗的 retry-build（#382）。
        actions = ["recover-pre-candidate", "abandon"] if not valid_candidate else ["abandon"]
        if slice_repin_eligible(slice_row):
            actions = ["retry-build"] + actions
        return actions
    if state != "needs_human":
        return []
    if not valid_candidate:
        return ["recover-pre-candidate", "abandon"]
    actions = ["retry-build", "abandon"]
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
    try:
        repo_root = autonomy._infer_repo_root(Path(spec_path))
    except autonomy.RepoRootResolutionError:
        # #612：推不出目標 repo 就不跑 git——舊實作會落到 cwd（daemon 的
        # WorkingDirectory ＝ operator 的真實 checkout），ancestry 於是對錯的樹
        # 作答，而且答得「成功」。
        summary["status"] = "repo-unresolved"
        return summary
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


def _status_repo(*values: object) -> str | None:
    """Return an explicit ``owner/repo`` value, never infer one from a path."""
    for value in values:
        if not isinstance(value, str) or value.count("/") != 1:
            continue
        owner, repo = value.split("/", 1)
        if owner and repo:
            return value
    return None


def slice_status_entry(registry, slice_row: dict, *, handoff_dir: str, git_runner=None) -> dict[str, Any]:
    slice_id = str(slice_row.get("slice_id") or "")
    builder_job_id = slice_row.get("builder_job_id")
    reviewer_job_id = slice_row.get("reviewer_job_id")
    builder_job: dict[str, Any] | None = None
    reviewer_job: dict[str, Any] | None = None
    builder_job_state: str | None = None
    reviewer_job_state: str | None = None
    if hasattr(registry, "get_job"):
        try:
            if isinstance(builder_job_id, str):
                builder_job = registry.get_job(builder_job_id)
                builder_job_state = str(builder_job.get("status"))
        except Exception:
            builder_job = None
            builder_job_state = None
        try:
            if isinstance(reviewer_job_id, str):
                reviewer_job = registry.get_job(reviewer_job_id)
                reviewer_job_state = str(reviewer_job.get("status"))
        except Exception:
            reviewer_job = None
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
    # #384：manifest 上的 typed provider failure 分類（None 除非本輪終局是
    # build-phase failure 且分類得到結果，見上面 write_manifest 的呼叫端）。
    # 投影出來讓 `cortex inspect status` 不必自己解析 `reason` 字串。
    manifest_provider_outcome = (
        manifest.get("provider_outcome") if isinstance(manifest, dict) else None
    )
    if provider_outcome.ProviderFailureClassification.from_dict(manifest_provider_outcome) is None:
        manifest_provider_outcome = None
    authority = manifest.get("work_authority") if isinstance(manifest, dict) else None
    authority_repo = authority.get("repo") if isinstance(authority, dict) else None
    repo = _status_repo(
        slice_row.get("repo"),
        authority_repo,
        reviewer_job.get("workflow_repo") if reviewer_job else None,
        builder_job.get("workflow_repo") if builder_job else None,
    )
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
        "provider_outcome": manifest_provider_outcome,
        "repo": repo,
        "candidate": slice_row.get("candidate"),
        "target_remote": slice_row.get("target_remote"),
        "target_branch": slice_row.get("target_branch"),
        "ancestry": _resolve_ancestry_status(slice_row, git_runner=git_runner),
        "current_evidence_refs": list(slice_row.get("current_evidence_refs") or []),
        "current_evaluation_refs": list(slice_row.get("current_evaluation_refs") or []),
        "next_actions": allowed_slice_actions(registry, slice_row),
        # 診斷 invariant：slice lane 的 attention 條目沿用既有的 `reason`／
        # `provider_outcome`，沒有結構化理由可帶；`kind` 只是用來與同一份
        # attention 清單裡的 workflow run 條目區分（#527 之前這份清單只有
        # slice，run 完全不出現）。
        "kind": "slice",
        "blocking_reason": None,
    }


def workflow_status_entry(
    registry, run, *, candidate_base_probe: "candidate_base.MirrorDistanceProbe | None" = None
) -> dict[str, Any]:
    """#527：把 `needs_human` 的 workflow run 投影成 attention 條目。

    現場（run ``workflow-6607ac1307feb02ffe06``）：run 停在 build 階段掛著
    `needs_human`，`cortex status` 的 `slices`／`ready`／`held`／`attention`
    四份清單卻**一份都不含它**——狀態快照 provider 只走 `list_slices()`，從來
    沒有呼叫過 `list_workflow_runs()`。`manager.reconcile_planning_transactions`
    裡「補 needs_human facet，讓 cortex status 的 attention 清單有話說」那句
    註解描述的投影，實際上並不存在。

    欄位刻意沿用 slice 條目既有的詞彙（`slice_state`／`reason`／`next_actions`
    ／`blocking_reason`），不另立一套平行欄位體系：既有的文字模式渲染與
    `entry.get("slice_state") == "needs_human"` 這類過濾器因此原樣可用。

    #731 (C) 追加 ``candidate_git_base``：這條 run 的候選 git base（真的那個
    40-hex commit SHA）與它落後 mirror 上 ``origin/main`` 幾個 commit。過去這個
    事實只存在於候選 worktree 的 `.git` 裡，attention 上唯一像版本的欄位是
    ``source_revision``（64-hex authority digest，**與 git base 無關**），
    operator 因此無從判斷「已 merge 的修法進不進得來」。``candidate_base_probe``
    由呼叫端傳入以共用同一次快照的 git 讀取（見 `manager_daemon`）；不傳時各自
    建一個唯讀 probe。**本路徑不 fetch、不寫任何 git 物件。**
    """

    reason_payload = getattr(run, "needs_human_reason", None)
    reason_code: str | None = None
    if isinstance(reason_payload, dict):
        value = reason_payload.get("reason")
        if isinstance(value, str) and value:
            reason_code = value
    # #728：`next_actions` 過去**只**由 `_phase_recovery_actions` 導出，而它只
    # 涵蓋 build／verify／review（`registry.RETRY_CARD_PHASE_PERSONA`）；`plan`
    # phase 的 needs_human run（現場：`planning-authority-reconciliation-failed`）
    # 因此永遠拿到 `[]`，CLI 面無路可走。基礎集合改由
    # `claim.needs_human_next_actions` 導出——與 `claim._resume_decision` 是**同一
    # 個函式**，且它永遠至少含 `abandon`，這就是「不得再出現 next_actions: []」
    # 的機械保證。`_phase_recovery_actions` 退回它原本的職責：**補**那些要看 job
    # 層事實才判定得出來的動作（regenerate-gates／retry-card）。
    hint_classification: str | None = None
    try:
        from .work_actions import _planning_failure_hint

        hint = _planning_failure_hint(run)
        if isinstance(hint, dict):
            hint_classification = hint.get("classification")
    except Exception:  # noqa: BLE001 - 呈現面不得因曝光計算失敗而讓 status 死掉
        hint_classification = None
    # 保底集合不依賴任何 registry／檔案讀取，因此上面的 except 分支也不會讓它變空。
    next_actions: tuple[str, ...] = needs_human_next_actions(
        phase=getattr(run, "current_phase", None),
        planning_failure_classification=hint_classification,
    )
    persisted_next_step_hint = None
    if isinstance(reason_payload, dict):
        value = reason_payload.get("next_step_hint")
        if isinstance(value, str) and value.strip():
            persisted_next_step_hint = value
    next_step_hint = persisted_next_step_hint or needs_human_next_step_hint(
        phase=getattr(run, "current_phase", None),
        planning_failure_classification=hint_classification,
        work_id=getattr(run, "work_id", None),
        repo=getattr(run, "repo", None),
        run_id=getattr(run, "run_id", None),
    )
    try:
        from .work_actions import _phase_recovery_actions

        next_actions = (
            *next_actions,
            *(
                item
                for item in _phase_recovery_actions(run, registry)
                if item not in next_actions
            ),
        )
    except Exception:  # noqa: BLE001 - 呈現面不得因曝光計算失敗而讓 status 死掉
        pass
    try:
        candidate_git_base = candidate_base.candidate_git_base_for_run(
            run, registry, probe=candidate_base_probe
        ).to_dict()
    except Exception:  # noqa: BLE001 - 呈現面不得因曝光計算失敗而讓 status 死掉
        candidate_git_base = None
    return {
        "kind": "workflow_run",
        "run_id": run.run_id,
        "work_id": run.work_id,
        "repo": run.repo,
        "current_phase": run.current_phase,
        "slice_state": "needs_human",
        "gate_state": run.gate_status,
        "reason": reason_code,
        # #731 (C)：候選 git base（40-hex commit SHA）與落後 mirror 上
        # origin/main 的 commit 數。與 `source_revision`（64-hex authority
        # digest）是**兩件事**，欄位名刻意寫死 `git_base` 以免再被混淆。
        "candidate_git_base": candidate_git_base,
        # 結構化理由整份帶出去（機器可讀 reason ＋ 人可讀 detail ＋ 來源位置
        # ＋ evidence 位置）。欄位名沿用 `claim.ClaimDecision.blocking_reason`
        # ——那是全庫既有的「為什麼卡住」欄位，不另發明。
        "blocking_reason": dict(reason_payload) if isinstance(reason_payload, dict) else None,
        "evidence_refs": list(run.evidence_refs),
        "next_actions": list(next_actions),
        "next_step_hint": next_step_hint,
        "updated_at": run.updated_at,
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
    """reviewer 的 evidence log 是否為純 JSONL（非純者一律 `invalid-process-output`）。

    #485：Codex CLI 0.147.0 的 `codex exec ... --json` 會先把
    `Reading additional input from stdin...` 印進同一份 evidence log，於是**每
    一次**成功的 Codex foreign review 都在讀 verdict 之前就被判
    `invalid-process-output`——process exit 0、`.psc-review-verdict.json` 也
    寫好了，卻永遠到不了 verdict 驗證。

    修法採 issue 列的第二條路：只在 parse 前剝離「精確、adapter 自有」的已知
    banner（`outcome_taxonomy.KNOWN_PROCESS_BANNERS`，且只認開頭連續的那幾
    行）。JSONL 純度檢查本身一格未放寬：不在該表上的任何非 JSON 文字仍舊
    fail closed。
    """

    if not isinstance(log_path, str) or not log_path:
        return True
    path = Path(log_path)
    if not path.is_file():
        return True
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    for line in outcome_taxonomy.strip_known_process_banners(lines):
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


def _slice_review_authority_inputs(
    *,
    slice_row: Mapping[str, object],
    repo_root: Path,
    coordinator_root: Path | None,
    candidate: str,
) -> tuple[dict[str, str] | None, tuple[dict[str, str], ...] | None]:
    if coordinator_root is None:
        return None, None
    identity = SimpleNamespace(
        run_id=str(slice_row["slice_id"]),
        work_id=str(slice_row["slice_id"]),
        repo=str(repo_root),
        source_revision=candidate,
    )
    authority: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    for key in ("spec", "plan"):
        payload = slice_row.get(key)
        if not isinstance(payload, Mapping):
            raise ValueError(f"slice review {key} payload invalid")
        raw_path = payload.get("path")
        digest = payload.get("hash")
        if not isinstance(raw_path, str) or not isinstance(digest, str):
            raise ValueError(f"slice review {key} payload invalid")
        path = Path(raw_path)
        # 對齊 _pinned_input_mismatches：legacy/回復情境可能留下相對 path（例如
        # 舊版 pin_dispatch_inputs 寫入的 plan path），一律以 repo_root 為 base
        # resolve，避免這裡誤用當前 cwd 解析而讀錯檔或拋例外。
        if not path.is_absolute():
            path = repo_root / path
        path = path.resolve()
        relative = path.relative_to(repo_root.resolve()).as_posix()
        data = path.read_bytes()
        actual = verification.sha256_bytes(data)
        if actual != digest:
            raise ValueError(f"slice review {key} input drift")
        authority[relative] = actual
        content_ref = _write_workflow_input_content(
            coordinator_root=coordinator_root,
            run=identity,
            ref=relative,
            digest=actual,
            content=data.decode("utf-8"),
        )
        rows.append(
            {
                "pattern": relative,
                "path": relative,
                "sha256": actual,
                "authority": "planning-authority",
                "content_ref": content_ref,
            }
        )
    return authority, tuple(rows)


def _spool_writable_launcher(review_launcher, spool_dir: Path):
    """讓 reviewer 寫得進自己那一格 verdict spool 的 launcher 特化。

    verdict 搬出 worktree 之後，executor 自己的 sandbox 會擋掉往
    `<coordinator_root>/review-verdicts/<job>/` 的寫入（codex `--sandbox
    workspace-write` 只放行 cwd；claude `acceptEdits` 只覆蓋工作目錄）。
    `SubprocessLauncher.as_verdict_spool_writer()` 產生一份只多放行**這一個
    目錄**的等價 launcher（走既有 `--add-dir` 機制）。

    以 `getattr` 取用而非硬性要求：`AgentLauncher` protocol 只約定 `launch()`，
    測試注入的 fake 與其他實作不必實作這個特化——沒有就照原樣用（fake 本來就
    不受 sandbox 限制）。
    """

    specialize = getattr(review_launcher, "as_verdict_spool_writer", None)
    if specialize is None:
        return review_launcher
    return specialize(str(spool_dir))


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
        # #469：reviewer 繼承 builder 的 repo 歸屬——reviewer terminal 時終局
        # manifest 讀的是 reviewer job，slice_status_entry 的 fallback 也
        # reviewer-first，不繼承會落 null。
        workflow_repo=builder_job.get("workflow_repo"),
        # trust-root Phase 2a：本 job 以 **verdict spool** 通道派工。這個標記是
        # harvest 端「可不可以回退讀 worktree verdict」的唯一判準——有標記即不可
        # （見 `_finalize_review_job`）。標記寫在 jobs.json（Manager-owned Tier-0
        # 樹）而不是靠「spool 目錄在不在」，否則 builder 只要刪掉 spool 目錄再寫一份
        # worktree verdict 就能把自己洗回 legacy 路徑。
        review_verdict_channel=foreign_review.REVIEW_VERDICT_CHANNEL_SPOOL,
    )
    try:
        authority_inputs = _slice_review_authority_inputs(
            slice_row=slice_row,
            repo_root=repo_root,
            coordinator_root=coordinator_root,
            candidate=candidate,
        )
        review_worktree = foreign_review.prepare_review_worktree(
            repo_root=repo_root,
            slice_id=slice_id,
            reviewer_job_id=reviewer_job["job_id"],
            candidate=candidate,
            authority=authority_inputs[0],
            input_snapshot=authority_inputs[1],
            source_revision=candidate if authority_inputs[0] else None,
            subprocess_runner=subprocess_runner,
            git_runner=git_runner,
        )
        registry.update_job(reviewer_job["job_id"], worktree=str(review_worktree))
        # Phase 2a：verdict 落點搬離 worktree。pre-seed 守衛（該 job 的 spool 位置
        # 必須不存在）也一併搬到這裡；`prepare_review_worktree()` 內對 worktree
        # verdict 的舊守衛保留為 defense-in-depth（legacy fallback 仍會讀它）。
        verdict_spool_path = foreign_review.prepare_review_verdict_spool(
            reviewer_job_id=reviewer_job["job_id"],
            coordinator_root=coordinator_root,
        )
        prompt = foreign_review.build_review_prompt(
            slice_id=slice_id,
            plan_path=slice_row["plan"]["path"],
            verdict_path=str(verdict_spool_path),
            builder_job_id=builder_job_id,
            reviewer_job_id=reviewer_job["job_id"],
            candidate=candidate,
            launch_identity=reviewer_identity,
        )
        handle = _spool_writable_launcher(review_launcher, verdict_spool_path.parent).launch(
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
            template_instance=handle.template_instance,
            runtime_principal=handle.runtime_principal,
            runtime_mode=handle.runtime_mode,
            runtime_surface=handle.runtime_surface,
            credential_publish=handle.credential_publish,
            prompt_path=handle.prompt_path,
            control_log_path=handle.control_log_path,
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


def _review_failure_gate_reason(review_job: Mapping[str, object]) -> str:
    """reviewer process 失敗時的 gate_reason（#499）。

    修法前一律 `foreign-review-absent`：一個 provider 講得清清楚楚的限流
    （Claude stream-json 帶 `rate_limit_event.status = rejected`、
    `rateLimitType = five_hour`、`resetsAt`，終局 `api_error_status = 429`）
    被壓平成「沒有 review 結論」，operator 得自己去翻 raw JSONL 才知道要等到
    什麼時候才值得 retry-review。

    分類已由 `Dispatcher._finalize_headless` 在 finalize 當下寫在 job 上，這裡
    只是把它顯露出來。**後續處置不變**：仍是 needs_human、仍只提供既有的
    手動 retry-review 出口——本修法只修「分錯類」，不動 recovery policy。
    未分類（legacy job）或分不出來（UNKNOWN）維持既有字面值，不偽造分類。
    """

    classification = provider_outcome.classification_from_job(review_job)
    if classification is None or classification.outcome is provider_outcome.ProviderOutcome.UNKNOWN:
        return "foreign-review-absent"
    return f"foreign-review-provider-{classification.outcome.value}"


def _resolve_review_verdict_source(
    *,
    review_job: Mapping[str, object],
    worktree: Path,
    coordinator_root: Path | None,
) -> tuple[Path, str] | None:
    """決定這個 reviewer job 的 verdict 從哪裡收（spool 優先，legacy 僅過渡期）。

    trust-root Phase 2a 之後派工的 reviewer job，其 job row 帶
    `review_verdict_channel == "spool"`；對這些 job **只**認 spool 落點——
    找不到就是 `verdict-missing`，**不回退**讀 worktree。這一點是本修法的核心：
    若允許回退，builder 只要刪掉 spool 目錄再往 worktree 寫一份「通過」的
    verdict，最短攻擊路徑就原封不動地回來了。

    只有本修法**之前**派工、job row 沒有這個標記的 in-flight reviewer job 才走
    legacy worktree 落點（採信時記 WARN ＋ DiagnosticReason，見
    `_warn_legacy_review_verdict_channel`）。這批 job 出清後即可移除本分支。
    """

    channel = review_job.get("review_verdict_channel")
    job_id = review_job.get("job_id")
    if isinstance(job_id, str) and job_id:
        try:
            spool_path = foreign_review.review_verdict_spool_path(
                reviewer_job_id=job_id,
                coordinator_root=coordinator_root,
            )
        except ValueError:
            spool_path = None
        if spool_path is not None and spool_path.is_file() and not spool_path.is_symlink():
            return spool_path, foreign_review.REVIEW_VERDICT_CHANNEL_SPOOL
    if channel == foreign_review.REVIEW_VERDICT_CHANNEL_SPOOL:
        return None
    legacy_path = foreign_review.review_verdict_path(worktree)
    if legacy_path.is_file():
        return legacy_path, foreign_review.REVIEW_VERDICT_CHANNEL_LEGACY_WORKTREE
    return None


def _warn_legacy_review_verdict_channel(
    registry,
    *,
    slice_id: str,
    review_job: Mapping[str, object],
    verdict_path: Path,
) -> DiagnosticReason:
    """採信 legacy worktree verdict 時的 WARN ＋ 結構化理由（過渡期可稽核）。

    處置一格未變（照樣採信、照樣走原本的 gate evaluation 路徑）——本函式只負責
    讓「這一份 verdict 來自 Phase 2a 之前的不受控落點」在 log 與 slice action 上
    留下痕跡，而不是靜默通過。
    """

    reason = diagnostic_reason(
        "review-verdict-legacy-worktree-source",
        (
            "採信 worktree 內的 legacy review verdict（Phase 2a 之前派工的 reviewer job）"
            "；該落點在同 UID 下可被 builder 代寫，僅為過渡期相容。"
        ),
        source="manager._finalize_review_job:legacy-verdict-channel",
        reviewer_job_id=review_job.get("job_id"),
        verdict_path=str(verdict_path),
    )
    logger.warning(
        "review verdict legacy channel: slice=%s reviewer_job=%s path=%s",
        slice_id,
        review_job.get("job_id"),
        verdict_path,
    )
    try:
        registry.record_action(
            slice_id,
            action="foreign-review-legacy-verdict-source",
            actor="manager",
        )
    except Exception:  # noqa: BLE001 - 稽核註記失敗不得讓合法 review 卡住
        pass
    return reason


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
        # #499：gate_reason 帶上 typed provider outcome（gate evaluation
        # artifact 的 `reason` 刻意維持 `reviewer-process-failed` 不變——那份
        # artifact 是 immutable 的，改字面值會讓升級前後同一個 reviewer_job_id
        # 的重新 finalize 撞 immutability）。
        return evaluation, "needs_human", _review_failure_gate_reason(review_job)
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
    verdict_source = _resolve_review_verdict_source(
        review_job=review_job,
        worktree=worktree,
        coordinator_root=coordinator_root,
    )
    if verdict_source is None:
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
    verdict_path, verdict_channel = verdict_source
    try:
        if verdict_channel == foreign_review.REVIEW_VERDICT_CHANNEL_SPOOL:
            # 綁定欄位（job id／candidate／reviewer identity）由 Manager 依 job
            # registry 推導，payload 自述一律忽略——見 read_spool_review_verdict()。
            verdict = foreign_review.read_spool_review_verdict(
                verdict_path,
                builder_job_id=builder_job["job_id"],
                reviewer_job_id=review_job["job_id"],
                candidate=candidate,
                launch_identity=reviewer_identity,
            )
        else:
            _warn_legacy_review_verdict_channel(
                registry,
                slice_id=slice_id,
                review_job=review_job,
                verdict_path=verdict_path,
            )
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
    if verdict_channel == foreign_review.REVIEW_VERDICT_CHANNEL_SPOOL:
        # 權威副本已落成 immutable gate evaluation；spool 那份轉唯讀。
        foreign_review.seal_review_verdict_spool(verdict_path)
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
        _supersede_handoff_manifest(
            handoff_dir=handoff_dir,
            slice_id=slice_id,
            action="operator-abandon",
            actor=actor,
            clock=clock,
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

    if action == "recover-pre-candidate":
        cand = slice_row.get("candidate")
        if isinstance(cand, str) and verification.SAFE_SHA_RE.fullmatch(cand) is not None:
            raise ValueError("action-not-allowed:recover-pre-candidate")

        if slice_row.get("state") == "pending" and slice_row.get("builder_job_id") is None:
            consumed_at = clock()
            return {
                "slice_id": slice_id,
                "action": action,
                "slice_state": "pending",
                "gate_state": "pending",
                "result": "ok",
                "reason": "already-recovered",
                "requested_at": requested_at,
                "consumed_at": consumed_at,
            }

        builder_job_id = slice_row.get("builder_job_id")
        wt_path = None
        if isinstance(builder_job_id, str):
            try:
                b_job = registry.get_job(builder_job_id)
                wt_path = b_job.get("worktree")
            except Exception:
                pass
        if not wt_path:
            wt_path = slice_row.get("worktree")

        # #478：舊碼在 `runner is None`（生產 dispatcher 的合法狀態）時整段跳過
        # git 清理只 rmtree 目錄，registry 記錄留下來讓下一 tick 的
        # `git worktree add` 必失敗；且清理只在「目錄還在」時觸發，既存的
        # 「目錄已消失、registry 殘留」壞狀態永遠自癒不了。改走單一回收函式：
        # 預設 runner 由 `worktree_reclaim` 自行 fallback，後置條件（目錄不存在
        # ＋ registry 無該筆）驗證不過就 fail closed，不再回報 ok。
        # #645：記錄沒有 worktree 時的反推改走共用 helper，新舊兩種目錄形狀都試。
        # pool root **只在真的要反推時才解析**——`paths.worktree_root()` 在
        # `PSC_REPO_ROOT` 未宣告時是 fail-closed 的（#612），記錄已有路徑卻因此炸掉
        # 會讓回收比 #645 之前更脆弱。
        recorded = wt_path if isinstance(wt_path, (str, Path)) and wt_path else None
        pool_root = None
        if recorded is None:
            try:
                pool_root = paths.worktree_root()
            except Exception:
                pool_root = None
        branch_hint = slice_row.get("branch")
        reclaim = worktree_reclaim.reclaim_recorded_or_derived(
            recorded_path=recorded,
            pool_root=pool_root,
            job_id=slice_id,
            branch=branch_hint if isinstance(branch_hint, str) else None,
            git_runner=runner,
            preserve_root=_reclaim_preserve_root(registry),
        )
        if reclaim is not None and not reclaim.ok:
            raise RuntimeError(
                "recover-pre-candidate worktree reclaim failed: "
                f"{reclaim.detail or reclaim.status} ({reclaim.path})"
            )

        consumed_at = clock()
        registry.record_action(
            slice_id,
            action="operator-recover-pre-candidate",
            actor=actor,
            state="pending",
            gate_state="pending",
            requested_at=requested_at,
            consumed_at=consumed_at,
            result="ok",
        )
        registry.update_slice(
            slice_id,
            state="pending",
            gate_state="pending",
            builder_job_id=None,
            candidate=None,
        )
        _supersede_handoff_manifest(
            handoff_dir=handoff_dir,
            slice_id=slice_id,
            action="operator-recover-pre-candidate",
            actor=actor,
            clock=clock,
        )
        latest = registry.get_slice(slice_id)
        payload = {
            "slice_id": slice_id,
            "action": action,
            "slice_state": latest.get("state"),
            "gate_state": latest.get("gate_state"),
            "result": "ok",
            "requested_at": requested_at,
            "consumed_at": consumed_at,
        }
        if reclaim is not None:
            payload["worktree_reclaim"] = reclaim.to_dict()
        return payload

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
    # completed 對應的 terminal job 全量快照（issue #204 skill_ledger 用）：
    # 與 completed 1:1 同步更新（含同輪同 slice 雙 terminal 的「後者勝」語意），
    # 讓 run_tick 的 ledger_recorder 注入點能拿到 workflow_card/job_id 等記帳
    # 所需欄位，不必重新 parse 精簡過的 completed 條目。
    completed_jobs: list[dict] = []
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
        try:
            return autonomy._infer_repo_root(Path(spec_path))
        except autonomy.RepoRootResolutionError:
            # #612：推不出目標 repo → 與「沒有 spec 路徑」同義（`None`），
            # `default_is_satisfied` 因此走它既有的「無 repo 可查」分支，
            # 而不是拿 cwd 當 repo 去問 handoff／ancestry。
            return None

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

    list_slices_fn = getattr(registry, "list_slices", None)
    slices = list_slices_fn() if callable(list_slices_fn) else []
    for slice_item in slices:
        if slice_item.get("state") == "needs_human":
            ev_data = _current_verification_payload(slice_item)
            if ev_data and ev_data.get("payload", {}).get("summary") in {
                "candidate-worktree-dirty",
                "candidate-worktree-dirty-after-verification",
            }:
                builder_job_id = slice_item.get("builder_job_id")
                if isinstance(builder_job_id, str):
                    try:
                        b_job = registry.get_job(builder_job_id)
                        if b_job.get("status") in TERMINAL_STATUSES:
                            # #612：舊實作在「slice 沒有 spec 路徑」與「推斷丟例外」
                            # 兩種情況都退回 `Path.cwd().resolve()`，於是 dirty
                            # worktree 的重驗會對 operator 的真實 checkout 跑
                            # verification（含 git 操作）。改成 fail-closed：解析
                            # 不出目標 repo 就不重驗，由底下的 except 收掉這一輪。
                            r_root = _repo_root_for_slice_row(slice_item)
                            st_path = getattr(registry, "_state_path", None)
                            coord_root = Path(st_path).parent if st_path is not None else None
                            re_ev = verification_runner(
                                slice_row=slice_item,
                                job=b_job,
                                repo_root=r_root,
                                coordinator_root=coord_root,
                                git_runner=git_runner,
                                subprocess_runner=subprocess_runner,
                            )
                            if re_ev and isinstance(re_ev, dict):
                                validated_ev = _validate_result_evidence(
                                    evidence=re_ev,
                                    slice_id=slice_item["slice_id"],
                                    coordinator_root=coord_root,
                                )
                                _apply_verification_result(registry, slice_item["slice_id"], validated_ev)
                    except Exception:
                        pass

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
            repo_root = _repo_root_for_slice_row(slice_row)
            state_path = getattr(registry, "_state_path", None)
            coordinator_root = Path(state_path).parent if state_path is not None else None
            evidence = None
            publish_evidence = False
            evaluation = None
            completion_record = None
            gate_status = "failed" if status == "failed" else "needs_human"
            gate_reason = None
            # #384：build-phase failure 的 typed 分類，供下面 manifest 寫入時
            # 投影給 `cortex inspect status` 讀（見 slice_status_entry）。只有
            # `elif status == "failed":` 分支會賦值；其餘終局（passed／
            # needs_human 但非 builder failure）維持 None。
            slice_provider_outcome_payload: dict[str, object] | None = None

            if job.get("kind") == "review":
                try:
                    identity_registry = _identity_registry()
                except Exception:
                    identity_registry = None
                if slice_row is None:
                    if _is_workflow_lane_job(job):
                        gate_status = "failed" if status == "failed" else WORKFLOW_LANE_GATE_STATUS
                        gate_reason = WORKFLOW_LANE_GATE_REASON
                    else:
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
                    # #499：review lane 過去完全不投影分類，`provider_outcome`
                    # 因此永遠是 null——一筆機器可讀的 429 被壓平成「沒有
                    # review 結論」。build lane 早已這麼做（見下面
                    # `elif status == "failed":`），這裡補上同一條線。
                    if status == "failed":
                        review_classification = provider_outcome.classification_from_job(job)
                        if review_classification is not None:
                            slice_provider_outcome_payload = review_classification.to_dict()
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
                    # #384：不再一律壓平成 "builder-failed"——`job` 已在
                    # `Dispatcher._finalize_headless` 分類過（見
                    # provider_outcome.py），有分類時把 outcome 併進 gate_reason，
                    # 供 operator／`cortex inspect status` 直接看出是 auth／
                    # rate_limited／transient／content／quota 哪一種，不必回頭
                    # 翻 log。slice lane 沒有 workflow lane 的 `run.attempts`
                    # 可持久化 retry 計數，故本 lane 只做分類與可觀測性，不做
                    # bounded auto-retry（沿用既有 operator `retry-build` 手動
                    # 復原路徑）。
                    classification = provider_outcome.classification_from_job(job)
                    if classification is not None:
                        gate_reason = f"builder-failed-{classification.outcome.value}"
                        slice_provider_outcome_payload = classification.to_dict()
                    else:
                        gate_reason = "builder-failed"
                    if slice_row is not None:
                        try:
                            registry.update_slice(slice_id, state="failed", gate_state="failed")
                        except Exception:
                            pass
                elif slice_row is None and _is_workflow_lane_job(job):
                    # workflow lane 的 build phase job 到這裡必為 status == "exited"
                    # （failed 已在上面 `elif status == "failed":` 分支處理掉），不查
                    # slices 表、不當成缺 slice proof。
                    gate_status = WORKFLOW_LANE_GATE_STATUS
                    gate_reason = WORKFLOW_LANE_GATE_REASON
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
                    # #465：workflow-lane job 派工時帶 workflow_repo（build 與
                    # review kind 皆有），寫進終局 manifest 讓讀取端
                    # `_repo_from_manifest` 投影 repo 歸屬；slice-lane job 自 spec
                    # frontmatter 的顯式 `repo:` 宣告帶入（#469，reviewer 繼承
                    # builder）；未宣告仍寫 null，依 #230 契約不從 branch 推斷。
                    "workflow_repo": job.get("workflow_repo"),
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
                    # #384：typed provider failure 分類（None 除非本輪終局是
                    # build-phase failure 且分類得到結果）。`slice_status_entry`
                    # 讀回這個欄位投影給 `cortex inspect status`。
                    "provider_outcome": slice_provider_outcome_payload,
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
                for job_entry in completed_jobs:
                    if job_entry.get("task") == slice_id:
                        job_entry.clear()
                        job_entry.update(job)
                        break
            else:
                completed.append({"slice_id": slice_id, "gate_status": gate_status})
                completed_jobs.append(dict(job))
            seen_slices[slice_id] = job_id
        except Exception as exc:
            errors.append({"job_id": job_id, "error": str(exc)})

    summary: dict = {
        "polled": polled,
        "completed": completed,
        "completed_jobs": completed_jobs,
        "errors": errors,
        "warnings": warnings,
    }
    if released_ok:
        try:
            summary["released"] = sorted(_ready_ids() - before_ready)
        except ValueError:
            pass
    return summary


def _manifest_still_blocks_fanout(registry, slice_id: str) -> bool:
    """判斷殘留 handoff 終局 manifest 是否仍該擋本輪 fanout（issue #383 提案 2）。

    manifest 只反映「某個 job 曾經跑到終局」，不代表「這個 slice 現在仍該被
    跳過」——`apply_slice_action` 的 `recover-pre-candidate`／`abandon` 等復原
    動作會把 registry state 撥回 `"pending"`（可重派），但沒有義務刪除舊
    manifest（保留稽核紀錄）。此處與 registry 現況對帳，而不是只看 handoff
    目錄有沒有檔案：

    - registry 查無此 slice（從未建過 slice row，例如純 handoff-only 情境）
      → 無法確認已復原，保守照舊擋。
    - `state == "pending"` → 已被復原到可重派狀態，manifest 過期，不擋。
    - 其餘狀態（needs_human/failed/building/verified/completed 等）→ 與
      manifest 描述的終局一致，照舊擋。
    """
    if registry is None:
        return True
    try:
        slice_row = registry.get_slice(slice_id)
    except KeyError:
        return True
    return str(slice_row.get("state")) != "pending"


def dispatch_gate_scan(
    metas: list[dict],
    *,
    handoff_dir: str,
    registry,
) -> tuple[list[dict], dict[str, dict], list[dict]]:
    """算出本輪允許進 `dispatch_ready()`/`ready_units()` 就緒判定的 meta 子集。

    `run_tick()` 與 `manager_daemon.py` 的 `request_type == "fanout"` 分支皆呼叫
    本函式，消除兩條路徑各自維護一份過濾邏輯導致的分歧（issue #383 複驗指出
    「fanout 路徑不只沒 already_terminal 過濾、連 in-flight active 過濾也沒
    有」）。兩層過濾疊加：

    1. in-flight 過濾：registry 中仍 dispatched/running 的 job 對應 slice 本趟
       不重派（同一 slice 一 job 不變量，review F-A）。
    2. handoff 終局過濾：已有 handoff 終局紀錄（needs_human/failed/passed/
       verified 皆算）的 slice 預設不重派（issue #339 冪等）——但終局是否仍
       該擋，改與 registry 現況對帳而非只看檔案存不存在（`_manifest_still_blocks_fanout`，
       issue #383 提案 2），避免復原後的 slice 被殘留 manifest 永久靜默跳過。

    回 `(fanout_metas, already_terminal, needs_human)`：
    - `fanout_metas`：真正該餵給 `dispatch_ready()` 的子集。
    - `already_terminal`：slice_id -> manifest payload，供呼叫端組 needs_human
      清單用途——不論是否阻擋 fanout 皆收，維持既有回報語意（不受本次對帳
      影響，複驗要求 needs_human 回傳語意不變）。
    - `needs_human`：manifest `gate_status == "needs_human"` 的清單（維持既有
      語意；即使已復原也照列，這是操作者可見的歷史軌跡，不是「靜默」問題）。
    """
    already_terminal: dict[str, dict] = {}
    needs_human: list = []
    blocking: set[str] = set()
    for meta in metas:
        slice_id = meta.get("slice_id") if isinstance(meta, dict) else None
        if not isinstance(slice_id, str) or not slice_id:
            continue
        manifest_path = Path(handoff_dir) / f"{slice_id}.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        already_terminal[slice_id] = payload
        if payload.get("gate_status") == "needs_human":
            needs_human.append(
                {
                    "slice_id": slice_id,
                    "gate_reason": payload.get("gate_reason"),
                    "handoff_path": str(manifest_path),
                }
            )
        if _manifest_still_blocks_fanout(registry, slice_id):
            blocking.add(slice_id)

    active: set[str] = set()
    if registry is not None:
        active = {j.get("task") for j in registry.list_jobs() if j.get("status") in IN_FLIGHT_STATUSES}
    fanout_metas = [m for m in metas if m.get("slice_id") not in active and m.get("slice_id") not in blocking]
    return fanout_metas, already_terminal, needs_human


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
    ledger_recorder: Callable[[list[dict]], list[dict]] | None = None,
    skill_janitor: Callable[[], dict] | None = None,
    review_executor: str | None = None,
    review_model: str | None = None,
    identity_registry=None,
    launcher_factory=None,
    spawn_admission: SpawnAdmissionLimiter | None = None,
) -> dict:
    """跑完整 manager tick：fanout（dispatch_ready）→ complete_tick →（可選）收尾 janitor。

    require_idle 時以 1-min load average gate（reuse memory.dream.idle，可注入 probe）——
    僅擋 fanout（新工作），complete_tick 一律跑。已有 dispatched/running job **或已有
    handoff 終局紀錄**的 slice 本趟不重派（冪等，issue #339）：`ready_units`/
    `default_is_satisfied` 只檢查「別人 depends_on 我」是否滿足，從未檢查「我自己是否
    已經跑過」——slice 一旦 exited 離開 IN_FLIGHT_STATUSES，若沒有這層過濾，下一趟
    tick 會把它判定為就緒、對同一 branch/worktree 重新 fanout，撞
    `ScriptWorktreeCreator.create` 的 "worktree target already exists"。
    fanout 例外（DispatchReadyError/RequiresLauncher/ValueError 環）收進 errors，不阻
    complete。

    reaper 為收尾 janitor（issue #161）：傳入時於 complete 後呼叫一次以回收孤兒 codex
    broker（多 worktree 派工殘留），其回傳放 summary["reaped"]；任何例外收進 errors（stage=reap），
    不破壞 tick。預設 None（不啟用）——避免單測誤觸真實行程回收；production 由 CLI 接上。

    ledger_recorder／skill_janitor 為 skill 治理收尾 hook（issue #204），與 reaper 同款
    「預設 None 不啟用、例外一律吸收不破壞 tick」注入模式：
    - ledger_recorder：若提供，於 complete_tick 之後以本輪 `complete["completed_jobs"]`
      （terminal job 全量快照清單）呼叫一次，預期回傳實際記錄的 usage event 清單，放進
      summary["skill_usage_events"]；典型注入值為
      `functools.partial(skill_ledger.record_usage_events, cards=cards)`。
    - skill_janitor：若提供，於 complete 後呼叫一次（zero-arg），只做 cold-skill 偵測與
      proposal 產生（不動 park state），回傳放 summary["skill_janitor"]；典型注入值為
      `functools.partial(skill_janitor.run_janitor_tick, cards=cards, ledger_path=..., proposals_dir=...)`。
    兩者任一丟例外都收進 errors（stage 分別為 skill_ledger／skill_janitor），不影響
    dispatch/complete/reap 任何一段的結果。
    回 {dispatch_skipped, dispatched, completed, errors, reaped, needs_human, skill_usage_events, skill_janitor}。
    """
    satisfied = is_satisfied if is_satisfied is not None else _satisfied_pred(handoff_dir)
    dispatched: list = []
    errors: list = []
    # 已有 handoff 終局紀錄（needs_human/failed/passed/verified 皆算）的 slice：
    # 不論 dispatch_skipped 與否都要掃描——這段刻意放在 idle 判斷之前、兩分支共用，
    # 因為「idle gate 擋不擋新工作」跟「有沒有 job 卡在 needs_human 待人工」是兩件事，
    # 高負載（not-idle）時操作者更需要看到 needs_human 清單，不能被 idle-skip 短路成空清單。
    # fanout_metas 已與 registry 現況對帳（dispatch_gate_scan，issue #383 提案 2）：
    # 復原動作（recover-pre-candidate 等）把 slice 撥回 pending 後，殘留的舊終局
    # manifest 不再讓它被永久跳過。
    registry = getattr(dispatcher, "_registry", None)
    fanout_metas, already_terminal, needs_human = dispatch_gate_scan(
        metas, handoff_dir=handoff_dir, registry=registry
    )
    # idle gate 只擋「派工側（新工作，會啟 agent，昂貴）」；完成側（poll→manifest，便宜的
    # 回收/記帳）一律跑，否則高負載時 job 完成/失敗狀態與下游釋放會被埋住（review F-C）。
    if require_idle and not idle.is_idle(max_load=max_load, probe=idle_probe):
        dispatch_skipped: str | bool = "not-idle"
    else:
        dispatch_skipped = False
        try:
            dispatched = autonomy.dispatch_ready(
                fanout_metas,
                satisfied,
                dispatcher,
                persona=persona,
                launcher=launcher,
                git_runner=getattr(dispatcher, "_git_runner", None),
                handoff_dir=handoff_dir,
                identity_registry=identity_registry,
                launcher_factory=launcher_factory,
                spawn_admission=spawn_admission,
            )
        except autonomy.DispatchReadyError as exc:
            dispatched = list(exc.jobs)
            errors.extend(
                {
                    "slice_id": slice_id,
                    "type": exc_value.__class__.__name__,
                    "message": str(exc_value),
                    "stage": "fanout",
                }
                for slice_id, exc_value in exc.errors
            )
        except (autonomy.DispatchReadyRequiresLauncherError, ValueError) as exc:
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
    # skill usage ledger 記錄（issue #204）：complete_tick 本輪產出的 terminal job
    # 全量快照過 ledger_recorder（若有注入）。跟 reaper 同款 try/except 隔離——記錄
    # 失敗不得讓 tick 中斷；結果放 summary["skill_usage_events"]，例外收進 errors
    # （stage=skill_ledger）。預設 None（不啟用）：避免單測誤寫真實 ledger 檔。
    skill_usage_events = None
    ledger_errors: list = []
    if ledger_recorder is not None:
        try:
            skill_usage_events = ledger_recorder(complete["completed_jobs"])
        except Exception as exc:
            ledger_errors.append({"stage": "skill_ledger", "error": str(exc)})

    # skill park janitor（issue #204）：proposal-first，只讀 ledger、只寫 proposal，
    # 絕不動 park state。例外一律吸收；結果放 summary["skill_janitor"]。
    skill_janitor_result = None
    janitor_errors: list = []
    if skill_janitor is not None:
        try:
            skill_janitor_result = skill_janitor()
        except Exception as exc:
            janitor_errors.append({"stage": "skill_janitor", "error": str(exc)})

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
        "errors": errors + complete["errors"] + ledger_errors + janitor_errors + reap_errors,
        "reaped": reaped,
        "needs_human": needs_human,
        "skill_usage_events": skill_usage_events,
        "skill_janitor": skill_janitor_result,
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


def _read_planning_artifact_content(root: Path, ref: str) -> tuple[bytes, str]:
    relative = Path(ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("planning artifact escapes artifact root")
    unresolved = root / relative
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("symlink planning artifact")
    resolved = unresolved.resolve()
    resolved.relative_to(root)
    content = resolved.read_bytes()
    return content, content.decode("utf-8")


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
        try:
            content, text = _read_planning_artifact_content(root, ref)
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


def _load_run_planning_artifacts(run) -> tuple[PlanningArtifact, ...] | None:
    root = Path(run.workspace_root).resolve()
    artifacts: list[PlanningArtifact] = []
    for authority in run.planning_authority:
        try:
            _content, text = _read_planning_artifact_content(root, authority.ref)
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        artifacts.append(PlanningArtifact(kind=authority.kind, ref=authority.ref, text=text))
    return tuple(artifacts)


# --- #414：deterministic pass plan 卡前的 declared-outputs 驗證 -------------
#
# 根因（生產實測 run workflow-e18785ac）：`assess_planning_completeness` 只看
# kind 覆蓋率——workstream todo（kind=plan、accepted）就足以讓 planning 判定
# complete，manager 因此把 plan 卡（如 writing-plans-light）deterministic
# pass，卻從未檢查卡片自己宣告的 `produces` glob（如
# `docs/superpowers/plans/*<task-slug>*.md`）是否真的命中檔案——todo 的
# ref 通常不落在該 pattern 內。下一棒 build 卡宣告同一 pattern 為
# declared input，`_workflow_input_snapshot`（見上方 `_safe_input_matches`
# 用法）找不到檔案便直接 raise，整個 run 卡死在 needs_human。
#
# 這裡補上與 build 端對稱的檢查：deterministic pass 之前，用同一套
# `_safe_input_matches` glob 語意驗證 outputs 是否已存在；缺席時嘗試把
# 已 accepted 的 kind=plan 內容 materialize 到卡片宣告的 canonical 路徑
# （`_materialize_plan_card_output`）。plan 卡目前沒有「planning_complete
# 為真時仍會走正常派工」的路徑（見 `_dispatch_workflow_card` 讀碼：一旦
# `planning_complete` 為真，本函式只會 needs_decomposition／needs_human／
# plan-review 重試／deterministic pass 四選一，從不落到下面建立真實 job
# 的路徑），materialize 因此是唯一可行的最小修法；不可 materialize 時
# （宣告了非單一 output pattern、或找不到已 accepted 的 kind=plan 內容）
# fail-closed：不跳過、不動 registry，交由下一輪 dispatch 重新判定。
def _plan_card_declared_outputs_present(root: Path, patterns: tuple[str, ...]) -> bool:
    """驗證 plan 卡宣告的 outputs glob patterns 是否皆已在 ``root``
    （run.workspace_root）命中至少一個實檔。空 patterns 視為已滿足。"""
    return all(_safe_input_matches(root, pattern) for pattern in patterns)


def _plan_card_canonical_output_path(pattern: str) -> str:
    """把單一 `*`-only 萬用字元 output glob pattern 收斂為 canonical 具體
    路徑（移除 `*`）。非此形狀（含 `?`／`[...]`，或移除萬用字元後路徑不
    合法）一律 raise，交由呼叫端視為不可 materialize。"""
    if "*" not in pattern or "?" in pattern or "[" in pattern:
        raise ValueError(f"plan output pattern 不支援 materialize：{pattern!r}")
    canonical = pattern.replace("*", "")
    relative = Path(canonical)
    if (
        not canonical
        or canonical.endswith("/")
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != canonical
    ):
        raise ValueError(f"plan output pattern materialize 路徑不合法：{pattern!r}")
    return canonical


def _materialize_plan_card_output(
    *,
    run,
    step,
    artifacts: tuple[PlanningArtifact, ...],
    workspace_root: Path,
):
    """把已 accepted 的 kind=plan 規劃內容原樣 materialize 到 ``step`` 宣告
    的（唯一）output pattern 之 canonical 路徑，透過既有 planning
    publication 機制（`_PlanningPublicationTransaction.publish`）做
    CAS-safe 的 atomic 寫入。成功時回傳
    ``(更新後的 artifacts, 新的 PlanningArtifactAuthority, transaction)``——
    呼叫端需把 authority 併入 ``run.planning_authority`` 一併提交（build
    worktree 是獨立 git worktree，declared input 檢查缺席時要靠
    authority 記錄從 workspace_root seed 進去，見 `_workflow_input_snapshot`
    的 authority_refs fallback），並在後續 registry 提交失敗時呼叫
    ``transaction.rollback()``。不可 materialize 時回傳 ``None``（不寫檔、
    不動 registry）。

    ``journal_root=None``：此次寫入與呼叫端的 registry 提交在同一次
    dispatch 呼叫內完成，不借用 brainstorm 那份跨 run 的
    crash-recoverable journal（避免與其 `reconcile()` 語意〔預期
    kind=evidence 的 brainstorm gate ref〕互相干擾）；殘餘風險是寫入成功
    後、registry 提交前這極窄的視窗內若 manager 崩潰，materialize 出的
    檔案會變成未登記的孤兒——下一輪 dispatch 會重新判定 outputs 是否存在
    並可能需要人工介入，但不劣於修復前 100% 必炸的現狀。
    """
    if len(step.outputs) != 1:
        return None
    try:
        canonical_relative = _plan_card_canonical_output_path(step.outputs[0])
    except ValueError:
        return None
    accepted_plan = next(
        (
            assessment.artifact
            for assessment in assess_planning_completeness(artifacts).assessments
            if assessment.artifact.kind == "plan" and assessment.accepted
        ),
        None,
    )
    if accepted_plan is None:
        return None
    destination = workspace_root / canonical_relative
    if destination.is_symlink():
        return None
    cursor = workspace_root
    for part in Path(canonical_relative).parent.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return None
    content = accepted_plan.text.encode("utf-8")
    transaction = _PlanningPublicationTransaction(
        root=workspace_root, run_id=run.run_id, journal_root=None,
    )
    try:
        transaction.publish(destination, content, baseline_hash=None, mode=0o644, kind="artifact")
    except ValueError:
        return None
    materialized = PlanningArtifact(kind="plan", ref=canonical_relative, text=accepted_plan.text)
    authority = PlanningArtifactAuthority(
        ref=canonical_relative,
        kind="plan",
        work_id=run.work_id,
        baseline_sha256=hashlib.sha256(content).hexdigest(),
    )
    return artifacts + (materialized,), authority, transaction


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
        # #728：這條前置條件是 recover 與 reconciliation 的**共用**斷言，判準
        # 只在 `workflow.brainstorm_authority_bound` 導出一次；
        # `work_actions._recover_planning_action` 讀同一個函式決定它的出口
        # phase，因此不會再造出一個「phase=plan 但沒有 brainstorm 背書」的
        # 狀態（那正是本 issue 的死結來源）。
        if not brainstorm_authority_bound(run):
            raise ValueError(BRAINSTORM_AUTHORITY_MISSING)
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
        raise ValueError(BRAINSTORM_AUTHORITY_MISSING)
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
    # #418：plan 卡 deterministic pass materialize 出的 canonical plan 檔
    # （`_materialize_plan_card_output`）與 brainstorm 實際發佈的 plan
    # artifact（例如 workstreams/<slug>/todo.md）路徑不同、卻是同一份內容
    # 的 byte-copy，僅為滿足 build 端 declared input pattern
    # （`docs/superpowers/plans/*<slug>*.md`）而落地。下面單獨算出 plan
    # phase 宣告的 output patterns，用來把這種合法副本從「omission」判定
    # 中排除——與 `declared_patterns`（含 define phase）分開，避免誤放行
    # 不相干的 define 階段路徑。
    plan_output_patterns = tuple(
        pattern
        for step in run.steps
        if step.persona == "planner" and step.phase == "plan"
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
        # #514：以下每一條 raise 過去都只有一句沒有主詞的英文。本函式在迴圈裡
        # 掃多個 ref，operator 拿到訊息時完全不知道是哪一個 artifact 出問題——
        # 而上游 `resume_workflow_run` 又把整個例外吞掉、只回一個籠統的
        # `planning-authority-reconciliation-failed`。訊息一律補上 `ref=`。
        if not target.is_file():
            raise ValueError(
                f"workflow brainstorm artifact hash drift: ref={ref} (檔案不存在或不是一般檔案)"
            )
        data = target.read_bytes()
        actual_digest = hashlib.sha256(data).hexdigest()
        if actual_digest != digest:
            # 0814 adversarial review 的修正：「artifact 在磁碟上被改動」這個
            # 情境**先在這裡**失敗，走不到下面的 assessment 分支。它才是
            # revalidation 最常見的現場，因此診斷必須做在這一條上。
            raise ValueError(
                f"workflow brainstorm artifact hash drift: ref={ref} "
                f"(evidence={digest[:12]}; disk={actual_digest[:12]})"
            )
        existing = persisted.get(ref)
        if existing is None:
            if not (
                any(fnmatch.fnmatch(ref, pattern) for pattern in declared_patterns)
                or planning_kind_bound(kind, ref, run.work_id)
            ):
                raise ValueError(
                    f"workflow brainstorm artifact outside planner outputs: ref={ref} "
                    f"(declared={','.join(declared_patterns) or '-'})"
                )
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"workflow brainstorm artifact unreadable: ref={ref} "
                    f"({type(exc).__name__}: {str(exc)[:80]})"
                ) from exc
            assessment = assess_planning_artifact(PlanningArtifact(kind=kind, ref=ref, text=text))
            if not assessment.accepted:
                # 沿用 #513 的 `(reasons=...; markers=Lnn:...; evidence=...)`
                # 格式與 `cortex-planning-artifact-rejection/v1` evidence 落檔，
                # 讓首次寫入拒收與重驗拒收對 operator 長得一模一樣。
                evidence_ref = _record_planning_artifact_rejection_evidence(
                    coordinator_root=Path(coordinator_root) if coordinator_root else None,
                    run_id=run.run_id,
                    work_id=run.work_id,
                    assessment=assessment,
                )
                message = _planning_artifact_rejection_message(
                    assessment, evidence_ref=evidence_ref
                )
                logger.error(
                    "workflow-brainstorm-artifact-rejected run_id=%s work_id=%s %s",
                    run.run_id,
                    run.work_id,
                    message,
                )
                raise ValueError(f"workflow brainstorm artifact is not accepted: {message}")
        elif (
            existing.kind != kind
            or existing.work_id != run.work_id
            or existing.baseline_sha256 != digest
        ):
            raise ValueError(
                f"workflow brainstorm artifact differs from persisted authority: ref={ref} "
                f"(kind={existing.kind}→{kind}; "
                f"baseline={existing.baseline_sha256[:12]}→{digest[:12]})"
            )
        scanned[ref] = PlanningArtifactAuthority(
            ref=ref,
            kind=kind,
            work_id=run.work_id,
            baseline_sha256=digest,
        )

    missing = set(persisted) - set(scanned)
    if missing:
        # #418：materialized plan 副本本身不在 brainstorm evidence 的
        # artifacts 列表裡（它是 plan 卡 deterministic pass 之後才產生
        # 的），因此天生落在 `persisted - scanned` 差集中。逐一檢查是否
        # 為「合法副本」：kind=plan、work_id 對得上這個 run、內容
        # （baseline_sha256）與某個已通過 brainstorm 驗證的 kind=plan
        # entry 完全一致（byte-copy）、且 ref 落在 plan phase 宣告的
        # output pattern 內（即 build 端會讀的那個 canonical 路徑）。四條
        # 全符合才視為合法副本、不計入 omission；其餘（真正的 omission）
        # 維持 raise，fail-closed 語意不變。
        scanned_plan_digests = {
            entry.baseline_sha256 for entry in scanned.values() if entry.kind == "plan"
        }
        unresolved = {
            ref
            for ref in missing
            if not (
                persisted[ref].kind == "plan"
                and persisted[ref].work_id == run.work_id
                and persisted[ref].baseline_sha256 in scanned_plan_digests
                and any(fnmatch.fnmatch(ref, pattern) for pattern in plan_output_patterns)
            )
        }
        if unresolved:
            raise ValueError(
                "workflow brainstorm evidence omits persisted authority: refs="
                + ",".join(sorted(unresolved)[:5])
            )
    # `ordered` 起始自 `run.planning_authority`（即 `persisted` 的來源），
    # 因此上面被判定為合法副本的 materialized plan entry 原樣留在回傳值
    # 裡——它必須繼續存在，好讓 build worktree 透過 `_workflow_input_snapshot`
    # 的 authority_refs fallback seed 到這份 canonical plan 檔。
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
            # #379 複驗發現：此函式過去重建每個 step 時漏傳
            # skill_ref/action/commit_policy/test_policy，導致 WorkflowStep 的
            # dataclass 預設值（None）覆寫掉「完全不在本次 (phase, card_id)
            # 範圍內」的其他 step 的既有值——包含尚未輪到的 build phase 卡片。
            # 實務上，run 通常在 claim phase 就先 advance 過一次，因此 build
            # phase 卡片的 test_policy 在真正被拿來跑之前就已經被抹成 None，
            # #307 的 red-required 語意反轉、以及 #379 由 test_policy 導出
            # 應驗 gate 名稱的機制都會因此變成 dead code。這四個欄位本就只
            # 該由 deck compile 決定、run 存續期間不變，故一律原樣帶過，不
            # 受 (phase, card_id) 命中與否影響。
            skill_ref=step.skill_ref,
            action=step.action,
            commit_policy=step.commit_policy,
            test_policy=step.test_policy,
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
            # #765 補遺：帶上 job 與兩側值——「只有欄位名」的版本讓 era 失配只能
            # 實機逐層猜（0820-21 實測兩小時）。值皆為系統雜湊／識別碼，非敏感內容。
            raise ValueError(
                f"workflow job binding mismatch: {field} "
                f"(job={job.get('job_id')!r} expected={value!r} actual={job.get(field)!r})"
            )
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


def _raise_if_worktree_read_blocked(result: object, *, what: str) -> None:
    """#641：這次 `git -C <job 樹>` 失敗若是「Manager 沒有讀取權」，換一個指得出下一步的錯誤。

    canonical lane 的 candidate 驗證與 slice lane 的 `verification` 是同一個問題的
    兩個實例：兩邊都以 Manager 身分伸手進 builder 的樹。#641 收掉 `repo-worktree`
    的唯讀 ACL 之後，三分部署下這裡必然 `Permission denied`。**照樣 fail-closed**
    （處置一格未變，仍然 raise），本函式只負責讓錯誤訊息指向 #629 的第三執行身分，
    而不是留下一句在那個部署形態下毫無意義的「candidate does not exist」。
    """

    if isinstance(result, str) or result is None:
        return
    stderr = getattr(result, "stderr", "")
    if not isinstance(stderr, str):
        return
    if not verification.worktree_read_blocked({"status": "non-zero", "stderr": stderr}):
        return
    raise ValueError(
        f"workflow candidate worktree unreadable by manager ({what}); "
        f"blocked on {verification.WORKTREE_READ_BLOCKED_ISSUE}: "
        f"{verification.WORKTREE_READ_BLOCKED_DETAIL}"
    )


def _job_control_log_path(job: Mapping[str, object], log_path: str) -> str:
    """Return the persisted Manager-only completion anchor for a job."""

    control = job.get("control_log_path")
    return control if isinstance(control, str) and control else log_path


def _record_candidate_full_suite_evidence(
    job: Mapping[str, object], *, run, candidate: str
) -> None:
    """#760：權威 gate 的全套綠 → tree-hash 定址的 FullSuiteEvidence。

    delivery 的 pr-preflight 用它請求 `--skip-tests`：manager 環境是第三個
    env-red 執行面（#723 第五例），在那裡第三跑全套只會把已由 gate 環境獨立驗過、
    CI 又會在 PR 上重驗的訊號變成結構性 block。判準取**未反轉**的 ledger outcome
    （`_ledger_outcomes`）——tdd-red 的 RED（pytest failed＝達成）因此天然排除，
    只有真正全綠的候選會留下 evidence。best-effort：記不下來不影響採信（fail-open
    僅及於「delivery 屆時老老實實再跑一次」）。
    """

    try:
        log_path = job.get("log_path")
        if not isinstance(log_path, str) or not log_path:
            return
        found = terminal_contract.read_gate_ledger(
            terminal_contract.gate_ledger_path(_job_control_log_path(job, log_path))
        )
        if found is None:
            return
        outcomes = terminal_contract._ledger_outcomes(found[0])
        pytest_outcome = outcomes.get(terminal_contract.RED_REQUIRED_TEST_GATE_NAME)
        if not isinstance(pytest_outcome, Mapping) or pytest_outcome.get("status") != "passed":
            return
        workspace_root = getattr(run, "workspace_root", None)
        if not isinstance(workspace_root, str) or not workspace_root:
            return
        tree = subprocess.run(
            ["git", "-C", workspace_root, "rev-parse", f"{candidate}^{{tree}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        tree_hash = tree.stdout.strip().lower()
        if tree.returncode != 0 or verification.SAFE_SHA_RE.fullmatch(tree_hash) is None:
            return
        specs = gate_ledger.load_gate_specs(os.environ)
        command: tuple[str, ...] = ()
        for spec in specs:
            if spec.name == terminal_contract.RED_REQUIRED_TEST_GATE_NAME:
                command = spec.argv
                break
        if not command:
            return
        preflight.record_external_full_suite_evidence(
            tree_hash=tree_hash,
            command=command,
            completed_at_epoch=time.time(),
        )
    except Exception:
        # 證據是加值：記錄失敗不得影響採信本身。
        return


def _job_gate_worktree_state(job: Mapping[str, object]) -> Mapping[str, object] | None:
    """這個 job 的權威 gate ledger 裡的 `worktree_state`（#738），讀不到即 None。

    權威 ledger（`<log>.gates.json`）由 Manager 自己落地（#628 的
    `foreign_evidence_author()` 檢查的就是它），內容則是 gate 執行身分在快照副本上
    收集的——這是 #629／#641 裁定的「第三執行身分在受控 checkout 執行」的落點。
    `probe != "ok"` 視同缺席：消費端一律退回既有 fail-closed 路徑，不對半套狀態
    做任何推論。
    """

    log_path = job.get("log_path")
    if not isinstance(log_path, str) or not log_path:
        return None
    path = terminal_contract.gate_ledger_path(_job_control_log_path(job, log_path))
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    state = payload.get(gate_ledger.WORKTREE_STATE_KEY)
    if not isinstance(state, Mapping) or state.get("probe") != "ok":
        return None
    return state


def _verify_exact_candidate(job: Mapping[str, object], *, git_runner=None) -> str:
    candidate = job.get("subject_head")
    # reviewer 走 `workflow_repo_root`，不讀 reviewer 的工作樹（那是 sandbox）。
    # #650 之後那個欄位是 **Manager 自己從來源樹 clone 出來的 candidate 樹**
    # （`_reviewer_candidate_workspace()`，HEAD 恰為 candidate），不再是前一張
    # build 卡的工作區——因此 #641 的同型問題在這條 lane 上結構性不存在。
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

    if job.get("persona") != "reviewer":
        # #738：builder 樹那條先消費 gate ledger 的 worktree_state——三分部署下
        # Manager 讀不進 builder 的樹（#641），HEAD == candidate 由 gate 執行身分
        # 在快照副本上量測。state 缺席／probe 非 ok 時退回下面的既有 git 路徑：
        # direct 模式（同 UID）照走、三分模式維持 #629 的 fail-closed。
        state = _job_gate_worktree_state(job)
        if state is not None:
            head = state.get("head")
            if not isinstance(head, str) or head != candidate.lower():
                raise ValueError(
                    "workflow candidate is not exact worktree HEAD "
                    f"(gate ledger head={head!r})"
                )
            return candidate

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
        _raise_if_worktree_read_blocked(exists, what="cat-file")
        raise ValueError("workflow candidate does not exist")
    head = run_git(["git", "-C", worktree, "rev-parse", "HEAD"])
    if isinstance(head, str):
        head_ok = True
        head_text = head
    else:
        head_ok = getattr(head, "returncode", 1) == 0
        head_text = getattr(head, "stdout", "")
    if not head_ok or not isinstance(head_text, str) or head_text.strip().lower() != candidate:
        _raise_if_worktree_read_blocked(head, what="rev-parse HEAD")
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

    # #738：ancestry 同樣先消費 gate ledger。ledger 記錄的 baseline 必須恰等於
    # Manager 當下算出的 baseline——不等（refreeze／換代造成的陳舊 ledger）視同
    # 缺席，退回既有路徑，不採信一份針對別的基線量出來的答案。
    state = _job_gate_worktree_state(job)
    if state is not None and state.get("ancestry_baseline") == baseline:
        ancestry_ok = state.get("ancestry_ok")
        if ancestry_ok is True:
            return candidate
        if ancestry_ok is False:
            raise ValueError("workflow build candidate is not a descendant")

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
        _raise_if_worktree_read_blocked(ancestry, what="merge-base --is-ancestor")
        raise ValueError("workflow build candidate ancestry unavailable")
    return candidate


def workflow_build_branch(run) -> str:
    """run 的 canonical build branch 名——**唯一**一條推導。

    #731：`_dispatch_workflow_card` 原本就地算這個名字（primary issue ＝ 最小的
    已授權 issue number，接 `run.work_id`）。`work refreeze-base` 必須在**改動
    候選基底之前**先問「這條 branch 現在在哪裡、會不會撞 #613 的
    `existing worktree branch has commits outside requested base`」，而那個問題
    只有拿到同一個 branch 名才問得準。抬成單一導出點，避免第二份會漂移的推導。
    """

    issue_numbers = [
        int(match.group(1))
        for ref in run.issue_refs
        if (match := re.fullmatch(rf"{re.escape(run.repo)}#([1-9][0-9]*)", ref))
    ]
    primary_issue = min(issue_numbers) if issue_numbers else None
    builder_work_id = (
        f"{primary_issue}-{run.work_id}" if primary_issue is not None else run.work_id
    )
    return f"feature/{builder_work_id}"


def _workflow_build_handoff_base(run, *, builder_jobs, card: str) -> str:
    """#648：中段／後續 build 卡的 clone base——**卡與卡交接的顯式通道**。

    canonical lane 的工作區改成 per-job 之後，「前一張卡的產出留在磁碟上給下一張
    用」這個隱含交接必須換成一個明講的通道。既有的那一條就是 #637 的 **bundle ＋
    append-only spool**：前一張卡的 commit 由 builder 產成 bundle → 寫進
    Manager-owned spool → `_harvest_build_candidate()` 把它 fetch 進來源樹的
    `refs/heads/<branch>`，且**強制**回收後的 branch head 恰等於被採信的 candidate
    （對不上即 fail-closed）。因此：

        來源樹的 `refs/heads/<branch>` == `run.candidate_head`

    在每一張 build 卡被採信之後成立，下一張卡只要以 `run.candidate_head` 為 base
    去 clone，拿到的就是前一張卡的成果——**完全不必讀前一張卡的工作區**。

    為什麼是 `run.candidate_head` 而不是「去讀來源樹的 branch tip」：candidate 是
    Manager 採信鏈（#540）的產物，branch tip 只是磁碟現況。以採信值為準，兩者一旦
    不一致就會在 `ScriptWorktreeCreator.create()` 的既有守衛上炸出來
    （`rev-parse --verify` 找不到 ⇒ harvest 沒完成；`merge-base --is-ancestor`
    失敗 ⇒ branch 上有 candidate 以外的 commit），而不是靜默沿用一個沒被採信的 tip。

    **中段卡重派（#545 retry-card）**：`_manager_reset_workflow_for_retry_card()`
    不動 `candidate_head`，只把那一張卡打回 pending。因此重派的中段卡在這裡拿到的
    base 仍是「最後一張**被採信**的 build 卡」的 candidate——不是 run 的原始 base，
    也不是那次失敗嘗試留在磁碟上的東西。失敗那次的工作區是另一個 job_id、另一個
    目錄，既不會撞名（#601 的 `worktree target already exists` 在這條 lane 結構上
    消失），也不會被下一次 provision 讀到。

    **`candidate_head` 尚未錨定時不走這條路**：那代表 run 還沒採信過任何 build
    成果（首張卡、或首張卡的 terminal 壞掉正在重派），呼叫端會退回「首張 build 卡」
    的凍結 base。判準寫在呼叫端而不是這裡，因為那是「有沒有東西要交接」的問題。

    推不出合法 SHA 時 raise：**不得**退回 creator 的預設 base（那是 `main`，等於把
    整個 run 已採信的成果 reset 掉）。
    """

    for value in (
        getattr(run, "candidate_head", None),
        builder_jobs[-1].get("subject_head") if builder_jobs else None,
    ):
        if isinstance(value, str) and verification.SAFE_SHA_RE.fullmatch(value) is not None:
            return value.lower()
    raise ValueError(
        "workflow build handoff base is unavailable: "
        f"card={card}；run 已有被採信的 build 卡，卻推不出可 clone 的 candidate。"
        "這代表上一張卡的成果回收（#637 bundle ＋ spool）沒有走完——不得以 run 的原始 "
        "base 重新 provision，那會丟掉已採信的 commit"
    )


def _harvest_build_candidate(
    job: Mapping[str, object],
    *,
    run,
    candidate: str,
    coordinator_root: str | Path | None = None,
) -> str | None:
    """#623：把 build card 的 candidate 從 job 的成果 bundle 取回 Manager 的樹。

    clone 模型下 builder 的 commit 只存在於 clone 自己的 object store。後續每一段
    都需要它在**來源 repo** 裡：review 卡的 `git worktree add --detach <candidate>`
    （`coordinator/review.py`）、`_completion_candidate_ref` 的 merge-base、
    `cortex work gc` 的 branch 分類，以及最基本的一件事——工作區被回收之後 commit
    還在不在。

    搬運介面是 **Manager-owned spool 裡的一個 bundle 檔**，不是 builder 的 clone
    ——三分部署下 Manager 走不進那棵樹（完整推導見 `job_workspace` 模組 docstring）。
    這個函式因此**完全不觸碰工作區**：它只讀 job 記錄、spool 與來源 repo。

    掛在 candidate 驗證**之後**：`_verify_build_candidate_transition` 已確認
    candidate 就是工作區的 HEAD 且單調延伸自基線，此處只負責把那個已被採信的
    commit 搬進來，不引入新的採信路徑。

    這個 job 沒有 spool 那一格（升級前既存的工作區、或測試裡的假 job 記錄）時回
    None，不做任何事——既有部署零回歸的掛點。
    """

    branch = job.get("branch")
    if not isinstance(branch, str) or not branch:
        return None
    bundle = job_workspace.commit_bundle_path_for_job(job, coordinator_root=coordinator_root)
    if bundle is None or bundle.parent.is_symlink() or not bundle.parent.is_dir():
        return None
    source_repo = getattr(run, "workspace_root", None)
    if not isinstance(source_repo, str) or not source_repo:
        raise ValueError("workflow build candidate harvest source repo missing")
    if not bundle.is_file():
        # 這張卡沒有產生新 commit（`git bundle create` 拒絕產生空 bundle）。candidate
        # 此時就是基線本身、來源樹早已有它——沒有東西要搬，也不該因此 fail。來源樹的
        # branch 若**不是** candidate，就落到 `harvest_branch` 的「bundle 缺席」
        # fail-closed，訊息會逐條列出成因。
        existing = job_workspace.source_branch_head(source_repo, branch)
        if existing is not None and existing == candidate.lower():
            job_workspace.seal_commit_spool(bundle)
            return candidate
    harvested = job_workspace.harvest_branch(
        source_repo=source_repo, bundle=bundle, branch=branch
    )
    if harvested.lower() != candidate.lower():
        # 回收後來源 repo 的 branch 必須恰好是被採信的 candidate。對不上代表
        # 工作區的 branch tip 與 HEAD 不同（模型在 detached HEAD 上 commit，
        # 或 provision 後有第三方動過 ref）——fail-closed，不得繼續。
        raise ValueError("workflow build candidate harvest head mismatch")
    job_workspace.seal_commit_spool(bundle)
    return harvested


#: #658：即時回收的結構化 log 事件名。operator 的稽核面就是這一行——
#: `journalctl -u cortex-manager | grep workflow-build-workspace-reclaim`。
#: 三種結果各一個字尾：`-reclaimed`（真的收掉）／`-skipped`（前置條件不成立，
#: 刻意不收）／`-failed`（試了但後置條件不成立，殘留交給 `cortex work gc`）。
WORKFLOW_BUILD_WORKSPACE_RECLAIM_EVENT = "workflow-build-workspace-reclaim"


def _trusted_build_workspace_target(
    job: Mapping[str, object], *, run, candidate: str
) -> tuple[Path | None, str | None]:
    """這張已被採信的 build 卡的工作區可不可以現在就回收？回 (路徑, 拒絕理由)。

    恰有一個非 None。**任何一條不成立都回拒絕理由，不 raise、不猜、不回收**——
    回收是清理，採信已經完成（#658 驗收：回收失敗不得擋住採信）。

    六條前置條件，前四條防「刪到不該刪的東西」，後兩條防「刪到還沒有第二份副本
    的東西」：

    1. **記錄真的有一條絕對路徑**，且是目錄、不是 symlink。
    2. **目錄名恰好是這個 job_id 經 `job_workspace.job_segment()` 導出的片段**。
       這是 #645 的單一推導點，也是本函式最重要的一條安全閘：它把「回收哪一棵樹」
       綁在 provisioning 的同一個推導上，而不是綁在一條可能陳舊的字串。#549 的
       資料語意地雷（實測 `job.worktree` 會等於 run 的 `workspace_root`，那是
       Manager 的 durable state）在這裡就被擋掉——來源樹的目錄名永遠不會是某個
       job_id 的片段。刻意**不**拿 `paths.worktree_root_for()` 當判準：那是
       config 解析出來的位置，會與磁碟上真正 provision 到哪裡漂開（注入 creator
       的呼叫端就是），而「以形狀判斷，不依環境推導」正是 #634 的原則。
    3. **它帶 `job_workspace` 的標記檔**（`is_job_clone`）。#646 的紅線：認不得的
       目錄一律不刪。三分部署下 Manager 讀不進 `0700` 的 clone，這條會回 False
       ⇒ 得到一個具名的 skip 理由，而不是一次注定失敗的 `rmtree`。
    4. **標記檔的 `branch` 與 `source_repo` 都對得上**——分別對 job 記錄的 branch
       與 run 的 `workspace_root`。標記檔是 provisioning 當下寫的，因此這一條驗的
       是「這棵樹真的是本 run、這條交付線 provision 出來的」，不只是路徑長得像。
    5. **來源樹裡真的有這顆 candidate**（`commit_present`）。
    6. **來源樹的 `refs/heads/<branch>` 恰等於 candidate**。

    5／6 是 `_harvest_build_candidate()` 的後置條件，這裡**當場對來源樹重驗一次**
    而不是依賴呼叫順序：`EVIDENCE_HARVESTED` 這個 evidence 模型（見
    `worktree_reclaim` 模組 docstring）的全部正當性就建立在這兩條上，而「呼叫端
    在正確的位置呼叫」是一條會隨重構漂掉的約定，不是不變式。
    """

    worktree = job.get("worktree")
    if not isinstance(worktree, str) or not worktree:
        return None, "workspace-path-missing"
    source_repo = getattr(run, "workspace_root", None)
    if not isinstance(source_repo, str) or not source_repo:
        return None, "source-repo-missing"
    branch = job.get("branch")
    if not isinstance(branch, str) or not branch:
        return None, "branch-missing"
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return None, "job-id-missing"
    if verification.SAFE_SHA_RE.fullmatch(candidate) is None:
        return None, "candidate-invalid"

    target = Path(worktree)
    if not target.is_absolute() or target.is_symlink() or not target.is_dir():
        return None, "workspace-path-not-a-directory"
    try:
        expected = job_workspace.workspace_path(target.parent, job_id)
    except job_workspace.WorkspaceError:
        return None, "job-id-not-a-workspace-segment"
    if target != expected:
        # #549：`job.worktree` 指到 run 的 `workspace_root`（主 checkout）時走到
        # 這裡。那是 Manager 的 durable state，遞迴刪除的爆炸半徑不可接受。
        return None, "workspace-name-not-derived-from-job-id"
    if not job_workspace.is_job_clone(target):
        return None, "workspace-not-a-job-clone"
    marker = job_workspace.read_marker(target) or {}
    if marker.get("branch") != branch:
        return None, "workspace-branch-mismatch"
    recorded_source = marker.get("source_repo")
    if not isinstance(recorded_source, str) or Path(recorded_source) != Path(source_repo):
        return None, "workspace-source-repo-mismatch"

    if not job_workspace.commit_present(source_repo, candidate):
        return None, "candidate-not-in-source-repo"
    if job_workspace.source_branch_head(source_repo, branch) != candidate.lower():
        return None, "source-branch-head-mismatch"
    return target, None


def _reclaim_trusted_build_workspace(
    job: Mapping[str, object], *, run, candidate: str
):
    """#658：build 卡被採信之後即時回收它的工作區。回傳 `WorktreeReclaim | None`。

    ## 為什麼現在可以收

    #648（後續 build 卡的 base 走 `run.candidate_head`，不讀前一張卡的工作區）、
    #649／#653（ship 段有自己的 Manager-owned 樹）、#650／#659（verify／review 的
    candidate 樹是 Manager 自己 clone 的）三票落地之後，**一張 build 卡被採信、
    `_harvest_build_candidate()` 走完之後，它的工作區已經沒有任何下游消費端**。
    per-job（#648）之後一個 run 會累積 N 棵這種樹（每棵約 35MB），即時回收因此是
    自然的下一步——#650 驗收第 3 條移到本票。

    ## 誰以什麼身分回收：**Manager，且不新增任何身分、不新增任何授權**

    這是 #658 的核心問題，答案由一條既有的不變式決定：**「Manager 進得去那棵樹」
    本來就是「這張卡被採信」的必要條件**。抵達本函式必須先走完
    `_verify_build_candidate_transition()`，而它的第一件事就是以 Manager 身分對
    **同一棵樹**跑 `git -C <worktree> cat-file` 與 `rev-parse HEAD`
    （`_verify_exact_candidate()`）。因此回收不需要比採信更大的授權面——它要的權限
    是採信路徑已經在用的那一份。

    四個候選身分各自為什麼不選（票上列的那四個）：

    - **(a) job wrapper 自刪**：被回收的對象自己決定回收，與 #540 的 acceptance
      chain 方向相反；更根本的是 job **不知道自己有沒有被採信**（採信發生在它退出
      之後、由 Manager 判定），自刪必然把未採信路徑的殘留一起銷毀——那正是 #601
      重派要用的東西。
    - **(b) #629 的 gate 執行身分**：登記表給 `GATE` 對 `repo-worktree` 的是 `rX`
      **無 `w`**。授它 `w` 等於讓一個專門用來跑不受信任程式碼的帳號能改 builder
      尚未 harvest 的交付樹，方向與 #629 自己的論證相反。
    - **(c) `ExecStopPost=`／`RuntimeDirectory`**：時機錯——unit 停止是 **job 退出**
      不是**被採信**，同 (a) 的失敗形態；而讓它跑得動 `rm -rf` 需要 `+` 前綴
      （root 執行），與「cortex 任何元件永不具 root」這條既有裁決相斥。
    - **(d) 依 `PSC_JOB_RUNNER` 分支（只在 `direct` 即時收）**：#634 的反模式。
      真正決定回收成不成立的是**磁碟上的 owner**，不是旗標；以旗標分支會在
      「旗標說降權、磁碟其實還是 Manager-owned」與其反面各錯一次。本函式改以
      **能力判定**：前置條件不成立就具名 skip，收不掉就回 `failed` ＋ 診斷。

    **三分／四分部署的誠實邊界**：#641 收掉 Manager 對 job 工作樹的 ACL 之後，
    `_verify_exact_candidate()` 在那些部署上會先 fail-closed（訊息明文
    `blocked on #629`）⇒ **那裡今天根本不存在「被採信卻沒回收的工作區」**，本函式
    連跑都不會跑到。等 #629 把 candidate 驗證搬到第三執行身分之後，「誰讀得到那棵
    樹」會跟著改變——屆時**回收身分必須與 candidate 驗證身分同進退**，這條依賴是
    刻意寫在這裡的，不是留給下一個人重新推導。

    ## 回收失敗不擋採信

    採信在呼叫本函式之前就已經 durable（`_manager_update_workflow_run()` 已落盤）。
    因此本函式**永不 raise**：失敗只留下 :data:`WORKFLOW_BUILD_WORKSPACE_RECLAIM_EVENT`
    的結構化 log，殘留由既有的 `cortex work gc` 掃除。

    ## evidence 模型

    走 `worktree_reclaim.EVIDENCE_HARVESTED`——完整論證在該模組的 docstring，前提由
    `_trusted_build_workspace_target()` 的第 5／6 條**當場對來源樹複驗**。
    """

    job_id = job.get("job_id")
    run_id = getattr(run, "run_id", None)
    card = job.get("workflow_card")
    target, refusal = _trusted_build_workspace_target(job, run=run, candidate=candidate)
    if target is None:
        logger.info(
            "%s-skipped run_id=%s job_id=%s card=%s reason=%s",
            WORKFLOW_BUILD_WORKSPACE_RECLAIM_EVENT, run_id, job_id, card, refusal,
        )
        return None
    try:
        result = worktree_reclaim.reclaim_worktree(
            target,
            repo_root=str(getattr(run, "workspace_root", "")) or None,
            evidence_model=worktree_reclaim.EVIDENCE_HARVESTED,
        )
    except Exception as exc:  # noqa: BLE001 - 清理不得讓已完成的採信反悔
        logger.warning(
            "%s-failed run_id=%s job_id=%s card=%s path=%s error=%s: %s",
            WORKFLOW_BUILD_WORKSPACE_RECLAIM_EVENT, run_id, job_id, card, target,
            type(exc).__name__, str(exc)[:200],
        )
        return None
    if result.ok:
        logger.info(
            "%s-reclaimed run_id=%s job_id=%s card=%s path=%s status=%s",
            WORKFLOW_BUILD_WORKSPACE_RECLAIM_EVENT, run_id, job_id, card,
            result.path, result.status,
        )
    else:
        logger.warning(
            "%s-failed run_id=%s job_id=%s card=%s path=%s detail=%s"
            "；採信不受影響，殘留工作區交給 `cortex work gc`",
            WORKFLOW_BUILD_WORKSPACE_RECLAIM_EVENT, run_id, job_id, card,
            result.path, result.detail,
        )
    return result


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
        "workflow_repo": run.repo,
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
            raise ValueError(
                f"review evaluation builder binding mismatch: {field} "
                f"(job={builder.get('job_id')!r} expected={value!r} actual={builder.get(field)!r})"
            )
    # #765：builder 引用**刻意不驗 claim era／source_revision 等值**——#216 AC5
    # 明言 authority restart 只 invalidate verify/review、build 產物的 Candidate
    # 跨 era 保留；builder job 因此**合法地**屬於較早的 era。真正把 builder 綁進
    # 本 run 的是 run_id＋repo＋`subject_head == candidate`（candidate 本身由
    # harvest 的 fast-forward 與 gate ledger 錨定）。era 等值檢查在此只會讓每一次
    # authority 前進（PR 建立、openspec link）把已採信的 build 產物變成孤兒。

    # ``fix-standard`` deliberately omits the Manager-only ship cards from
    # ``run.steps``.  The archive job is still the authoritative builder for a
    # post-archive review, and its canonical ship evidence is what proves that
    # Manager completed that card.  Requiring a matching step here therefore
    # rejects valid archive -> review handoffs (for example after an OpenSpec
    # archive advances the candidate).  Keep the normal build-card check
    # unchanged; only the typed, successful archive job gets this exception.
    if archive_author:
        evidence = builder.get("workflow_evidence")
        if not isinstance(evidence, dict) or evidence.get("kind") != "ship":
            raise ValueError("review evaluation archive evidence is not passed")
        return builder, archive_author

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


def _unwrap_structured_output(value: object) -> dict[str, object] | None:
    """#261 R3／D4：只剝一層白名單 wrapper，未知形狀一律回 ``None``。

    刻意不在這裡拋錯：``_extract_terminal_json`` 還會繼續掃描其他行，
    真正的「找不到 terminal evidence」由該函式統一終止。寬鬆解析（遞迴找出
    看起來像 canonical 的 dict）會把契約破口變成安靜的錯誤資料，故不採用。
    """

    if not isinstance(value, Mapping) or len(value) != 1:
        return None
    only_key = next(iter(value))
    if only_key not in terminal_contract.WRAPPER_KEYS:
        return None
    try:
        normalized = terminal_contract.normalize_structured_output(value)
    except terminal_contract.TerminalContractError:
        return None
    payload = normalized.payload
    return payload if _is_workflow_terminal_payload(payload) else None


def _schema_retry_attempt_key(card_id: str) -> str:
    """#261 D5：schema mismatch retry 計數在 run.attempts 上的鍵。

    刻意與 phase attempts 分開命名，避免和既有的 phase 重試次數混淆。

    #717：字面量收斂到 :func:`terminal_contract.schema_retry_attempt_key`——
    registry 的 `retry-card` 重置也要認得同一個鍵，而 registry 不 import manager。
    這裡保留既有名稱給既有呼叫端與測試。
    """

    return terminal_contract.schema_retry_attempt_key(card_id)


def _schema_mismatch_total_key(card_id: str) -> str:
    """#717：這張卡跨 `retry-card` 世代的累計 schema mismatch 觀測鍵。

    與 :func:`_schema_retry_attempt_key`（本輪額度，`retry-card` 會清）分家的理由
    見 :data:`terminal_contract.SCHEMA_MISMATCH_TOTAL_PREFIX` 的註解。
    """

    return terminal_contract.schema_mismatch_total_key(card_id)


def _provider_retry_attempt_key(card_id: str) -> str:
    """#384：provider 失敗（rate_limited／transient）bounded retry 計數在
    ``run.attempts`` 上的鍵。比照 :func:`_schema_retry_attempt_key` 的樣板——
    同一份 ``run.attempts`` 字典上以字首區分兩種完全不同性質的重試（schema
    mismatch 是模型輸出形狀問題；provider 失敗是 executor/服務層問題），
    互不影響、各自的 needs_human 原因也分開回報。
    """

    return f"provider-retry:{card_id}"


def _provider_failure_reroute(
    run,
    step,
    identities: IdentityRegistry,
    *,
    failed_job: Mapping[str, object],
    classification: "provider_outcome.ProviderFailureClassification",
):
    """#384：provider 失敗時，在既有 candidate 順序上 re-route，不放寬
    independence domain（forward-looking 約束：禁 policy-shopping，例如
    Codex builder 失敗後不得 re-route 成也用 Codex 的 reviewer）。

    複用 :func:`runtime_preflight.evaluate_dispatch_gate`（#369 已把 provider
    snapshot 接進生產的同一套機制）：把「剛剛觀察到的這次失敗」餵成一筆
    僅本次呼叫可見、in-memory 的 provider snapshot，讓 gate 依
    :func:`_workflow_identity_candidates`（既有 domain-filtered 順序，完全未
    改動）跳過剛失敗的 identity、選下一個候選。刻意不觸碰
    ``_EXECUTOR_AUTH_CACHE`` 或任何 durable snapshot——這只是這一次 retry
    決策的暫時性輸入，不影響其他 dispatch 熱路徑（`provider_prober=None`：
    不觸發任何真實 CLI 探測子行程）。

    只剩失敗的那個 identity 本身合格時，`evaluate_dispatch_gate` 必然又選回
    它——這不是 bug，是唯一合法選擇（沒有domain-合法的替代候選可換）。
    """

    from .runtime_preflight import (
        DEFAULT_PROVIDER_TTL_SECONDS,
        PROVIDER_EXECUTOR_SENTINEL,
        ProviderFreshness,
        RuntimeCapability,
        evaluate_dispatch_gate,
        host_environment,
    )

    candidates = _workflow_identity_candidates(run, step, identities)
    failed_executor = failed_job.get("executor")
    observed_at = time.time()

    def _snapshot_lookup(provider_id: str) -> ProviderFreshness | None:
        if provider_id != failed_executor:
            # 其餘候選：無快照 -> STALE_SNAPSHOT（non-blocking，直接放行，
            # 不多探測、不 spawn 任何子行程）。
            return None
        return ProviderFreshness(
            provider_id=provider_id,
            status="degraded",
            observed_at=observed_at,
            ttl_seconds=DEFAULT_PROVIDER_TTL_SECONDS,
            source="provider-failure-observed",
            reason=f"{classification.outcome.value}: {classification.reason}",
        )

    # `_workflow_identity_candidates` 在無合格候選時已 raise ValueError——與
    # `_select_workflow_identity` 完全相同的既有錯誤行為，這裡刻意不吞掉它
    # （沒有候選是設定錯誤，不是「這次 retry 剛好找不到人可換」）。
    decision = evaluate_dispatch_gate(
        card=step.card,
        requirements=(RuntimeCapability(kind="provider", name=PROVIDER_EXECUTOR_SENTINEL),),
        candidates=candidates,
        environment_for=lambda _identity: host_environment(),
        snapshot_lookup=_snapshot_lookup,
        provider_prober=None,
    )
    if decision.action == "needs_human":
        return None
    return decision.identity


def _canonicalize_card_terminal(raw: Mapping[str, object]) -> dict[str, object]:
    """#261 D1：把 canonical envelope 投影成既有 card 驗證路徑吃得下的形狀。

    canonical envelope 多帶 ``diagnostics``／``gate_evidence`` 兩個欄位；此處只保
    留 lifecycle 需要的欄位，避免新舊兩種讀法在下游並存。

    #717 更正：原註解宣稱這兩個欄位的語意「已經在
    :func:`_assert_terminal_gate_consistency` 消費完畢」——那對 ``gate_evidence``
    成立，對 ``diagnostics`` **不成立**。malformed／schema-retry 分支根本不會走到
    那個函式（見 :func:`_malformed_workflow_card_terminal` 的呼叫端），於是模型逐字
    寫下的病因整段被這個投影丟掉，operator 只看得到「不符契約」。診斷的落地點因此
    改由 :func:`_terminal_parse_diagnostics` 直接從**原始 envelope** 讀，不經過本
    投影——投影本身維持原狀（lifecycle 驗證路徑的形狀契約不變）。
    """

    projected = {
        key: value
        for key, value in raw.items()
        if key not in {"diagnostics", "gate_evidence"}
    }
    projected["schema_version"] = 1
    return projected


# #717：模型 ``diagnostics`` 帶進 attention 的字元預算。沿用 #606
# :data:`RETRY_CONTEXT_EVIDENCE_LIMIT` 的同一個數量級與理由——這段內容**完全來自
# 模型**，長度不受任何契約約束，一個亂寫的模型可以吐出數萬字把 attention 欄位
# 與狀態檔一起撐爆。預算是**全體**的（跨所有 key 共用一份），被截的項目以
# ``…`` 明示，不假裝完整。
TERMINAL_MODEL_DIAGNOSTICS_LIMIT = 2000

# 單一 diagnostics key 的長度上限；key 同樣是模型寫的，不能無界。
TERMINAL_MODEL_DIAGNOSTICS_KEY_LIMIT = 64


def _model_terminal_diagnostics(
    raw: Mapping[str, object] | None,
) -> tuple[tuple[str, str], ...]:
    """#717：把 envelope 上模型寫的 ``diagnostics`` 壓成有界的 (key, value) 序列。

    只做形狀正規化與截斷，**不做任何採信**：內容原封不動來自模型，呼叫端只把它
    當敘事帶給 operator，不得據以授權（見
    :class:`terminal_contract.TerminalDiagnostics` 的 ``authority_granted``）。

    非字串的值以 canonical JSON 落成字串（模型偶爾把 diagnostics 寫成巢狀物件），
    順序維持模型寫入順序，因此同一份 log 讀兩次結果逐字相同。
    """

    if not isinstance(raw, Mapping):
        return ()
    diagnostics = raw.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        return ()
    rows: list[tuple[str, str]] = []
    budget = TERMINAL_MODEL_DIAGNOSTICS_LIMIT
    for key, value in diagnostics.items():
        if budget <= 0:
            break
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(value, str):
            text = value.strip()
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                continue
        if not text:
            continue
        kept = text[:budget]
        budget -= len(kept)
        if len(kept) < len(text):
            kept = f"{kept}…"
        rows.append((key.strip()[:TERMINAL_MODEL_DIAGNOSTICS_KEY_LIMIT], kept))
    return tuple(rows)


def _terminal_parse_diagnostics(
    job: Mapping[str, object],
    *,
    error: BaseException | None = None,
) -> terminal_contract.TerminalDiagnostics:
    """#261 R4／D6：parse 失敗時保留唯讀診斷，但不授予 candidate authority。

    ``observed_head`` 取自 job 上「觀察到的」 subject/dispatch HEAD，與
    ``WorkflowRun.candidate_head``（已授權）分離；呼叫端不得用它去 bind。

    #717：另外把 envelope 上模型逐字寫的 ``diagnostics`` 一併帶出。讀的是
    :func:`_extract_terminal_json` 的**原始**結果，刻意不經
    :func:`_canonicalize_card_terminal`——那個投影正是把 ``diagnostics`` 丟掉的
    地方。取不到（log 讀不到、不是物件、模型沒寫）一律回空集合：診斷是加值，
    不得讓「讀不到診斷」再害死一次已經失敗的採信。
    """

    reason = str(error) if error is not None else ""
    raw: Mapping[str, object] | None = None
    try:
        raw = _extract_terminal_json(job.get("log_path"))
    except ValueError as exc:
        if not reason:
            reason = str(exc)
    except Exception:  # pragma: no cover - fail-soft：診斷組裝不得炸掉回報路徑
        raw = None
    if not reason:
        reason = "workflow terminal payload did not satisfy the result contract"
    observed = job.get("subject_head")
    if not isinstance(observed, str) or not observed:
        observed = job.get("dispatch_head")
    return terminal_contract.TerminalDiagnostics(
        job_id=str(job.get("job_id")),
        observed_head=observed if isinstance(observed, str) and observed else None,
        reason=reason,
        validation_path=getattr(error, "validation_path", "$"),
        model_diagnostics=_model_terminal_diagnostics(raw),
    )


# #261 R2：會實際跑確定性 gate 的 phase。這些 phase 的 `passed` 必須有 manager 獨立
# 產生的 gate ledger 背書；plan card 不改動 candidate、不跑 gate，故不在此列。
# #313：verify 亦不在此列——verification 卡以 review-only 沙箱啟動，
# `launcher._should_run_gates` 依設計不讓唯讀 reviewer 跑 gate（也不寫 ledger），
# 要求 ledger 等於讓 verification 卡結構性永不可過；verify 的獨立證據層是
# deterministic verification report 管線（schema／binding／report 重驗）。
GATE_LEDGER_REQUIRED_PHASES = frozenset({"build"})

# #629：降權模式下由**第四個帳號**（`cortex-gate`）重跑 gate 的 phase。
#
# 與 `GATE_LEDGER_REQUIRED_PHASES` **刻意是同一個集合**，而不是「所有會寫 candidate
# 的 phase」：跑 gate 的唯一理由就是有人要求那份 ledger，而要求它的只有上面那一條
# 規則。兩者若分家，就會出現「跑了 gate、產出的 ledger 沒有任何採信端會讀」的空轉，
# 或者反過來「要求 ledger 的 phase 沒人替它跑」——後者正是 #629 之前的現況。
GATE_EXECUTION_PHASES = GATE_LEDGER_REQUIRED_PHASES


def _workflow_step_test_policy(registry, job: Mapping[str, object]) -> str | None:
    """#307：從 job 綁定的 :class:`WorkflowRun` 撈出目前 card 的 ``execution.test_policy``。

    只用來餵給 :func:`terminal_contract.authorize_terminal` 做 red-required 語意
    反轉；找不到（``registry`` 未提供、run/card 不存在、欄位缺失等）一律回
    ``None``——維持既有 fail-closed 行為，不因為查找失敗而放寬任何一般卡的檢查。
    """

    if registry is None:
        return None
    run_id = job.get("workflow_run_id")
    card_id = job.get("workflow_card")
    if not isinstance(run_id, str) or not isinstance(card_id, str):
        return None
    phase = job.get("workflow_phase")
    try:
        run = registry.get_workflow_run(run_id)
    except Exception:
        return None
    for step in getattr(run, "steps", ()):
        if step.card == card_id and (phase is None or step.phase == phase):
            return step.test_policy
    return None


def _expected_gate_names_for_test_policy(test_policy: str | None) -> frozenset[str]:
    """#379 的判準；實作已於 #540 移到
    :func:`terminal_contract.expected_gate_names_for_test_policy`，讓 doctor 的
    gate 宣告前置檢查能共用同一份判準而不必 import 整個 manager。此處保留既有
    呼叫端與測試使用的名稱，不另立語意。
    """

    return terminal_contract.expected_gate_names_for_test_policy(test_policy)


def _workflow_acceptance_definition_drifted(
    job: Mapping[str, object], *, fresh_test_policy: str | None
) -> bool:
    """#379：驗收判準（test_policy）pinned-input drift 偵測，比照既有
    ``_pinned_input_mismatches``／``_review_inputs_drifted`` 的「派工當下 pin
    一份快照、harvest 時與現況比對」模式。

    ``job["workflow_test_policy"]`` 是 :func:`_dispatch_workflow_card` 派工當下
    寫入的快照（見該函式對 ``registry.create_job`` 的呼叫）；``fresh_test_policy``
    是 harvest 當下重新從 registry 現有 ``WorkflowRun.steps`` 讀到的值。兩者不
    一致代表這張卡的驗收判準在派工之後被動過——無論肇因是 operator／其他行程
    的合法變更，還是任何讓 builder 自報得以「改判準讓自報成真」的路徑，都必須
    fail closed，不得沿用新值靜默通過。

    ``job`` 沒有 ``workflow_test_policy`` 欄位（例如非經
    :func:`_dispatch_workflow_card` 真正派工路徑建立的 legacy／測試 job）時視為
    「未 pin」，不受本檢查約束——維持既有行為，不因為新增這道保護而誤殺舊資料。
    """

    pinned = job.get("workflow_test_policy")
    if pinned is None:
        return False
    return pinned != fresh_test_policy


def _gate_ancestry_baseline(registry, job: Mapping[str, object]) -> str | None:
    """auto 路徑 gate 量測 ancestry 的 baseline——**與採信端同一條導出**（#743）。

    採信端（`_verify_build_candidate_transition`）的 baseline 是
    `previous_candidate（run.candidate_head） or job["dispatch_head"]`；auto 路徑的
    gate 若各算一份（#738 首版直接拿 `dispatch_head`），中段 build 卡的 ledger 就會
    量在錯的基線上、被「baseline 不符視同缺席」的守衛正確拒絕，每張卡都得人工
    `regenerate-gates`。這裡回 run 的 `candidate_head`（合法 sha 時），否則 None
    ——`ensure_gate_ledger` 會退回 `dispatch_head`，首張卡兩者本就同值。
    """

    if registry is None:
        return None
    run_id = job.get("workflow_run_id")
    if not isinstance(run_id, str):
        return None
    try:
        run = registry.get_workflow_run(run_id)
    except Exception:
        return None
    candidate = getattr(run, "candidate_head", None)
    if isinstance(candidate, str) and verification.SAFE_SHA_RE.fullmatch(candidate):
        return candidate.lower()
    return None


def _run_gate_execution_identity(job: Mapping[str, object], *, registry=None) -> None:
    """#629：降權模式下補上缺席的 gate ledger（`direct` 模式為 no-op）。

    **失敗一律翻成 `TerminalContractError` 並 fail closed**，不吞、不降級。gate 跑
    不起來（polkit 拒絕、模板未安裝、快照失敗、spool 被塞了不合法內容）與「gate 沒
    通過」在**授權**這件事上是同一個結論：沒有獨立證據就不採信。差別只在訊息——
    這裡把 `GateRunnerError` 的診斷碼與 systemctl 的 exit code 原樣帶出來，否則
    operator 看到的只會是後面那個 `gate-ledger-missing`，指不出真正的原因（#643 的
    教訓：症狀是空輸出的失敗最難查）。
    """

    from . import gate_runner

    try:
        gate_runner.ensure_gate_ledger(
            job,
            phases=GATE_EXECUTION_PHASES,
            ancestry_baseline=_gate_ancestry_baseline(registry, job),
        )
    except (gate_runner.GateRunnerError, job_runner.JobRunnerError) as exc:
        reason = getattr(exc, "reason", None) or "gate-execution-failed"
        raise terminal_contract.TerminalContractError(
            f"gate 執行身分未能產生 ledger（{reason}）：{exc}",
            reason=str(reason),
            validation_path="$.gate_evidence",
        ) from exc


def _assert_terminal_gate_consistency(
    raw: Mapping[str, object],
    *,
    job: Mapping[str, object],
    registry=None,
) -> None:
    """#261 R2／D3：矛盾偵測優先於狀態採信。

    manager 讀的是 :mod:`paulsha_cortex.coordinator.gate_ledger` 在模型行程結束
    **之後**、於 manager 掌控的 wrapper script 內產生的 ledger——不是模型講的話。
    只要有任何確定性 gate 的實際結果不是 passed，terminal 自稱的 ``passed`` 一律
    fail closed，並把「哪一個 gate、期望值、實際值」保留在錯誤訊息裡。

    會跑 gate 的 phase（build／verify）若連 ledger 都不存在，代表 wrapper 的 gate
    階段沒跑完，同樣 fail closed：模型文字、exit code 為 0、無明確錯誤三者皆不構成
    成功授權。

    #307：``registry`` 為選填——提供時會用來解析目前 card 的 ``test_policy``，讓
    ``test_policy=red-required``（tdd-red）卡對測試 gate 的語意反轉在
    :func:`terminal_contract.authorize_terminal` 生效；未提供或解析不到時
    ``test_policy`` 視為 ``None``，行為與反轉前完全相同。

    #379：同一份 ``test_policy`` 另外餵給 :func:`_expected_gate_names_for_test_policy`
    導出這個 phase 應驗的 gate 名稱集合，交給 ``authorize_terminal`` 的
    ``expected_gate_names`` 參數把 #308 的空/局部 ledger 早退收斂成「只有 plan
    本就沒有應驗 gate 時才放行」。派工當下 pin 進 job 的 ``workflow_test_policy``
    與這裡重新解析出的現值不一致時（:func:`_workflow_acceptance_definition_drifted`），
    在呼叫 ``authorize_terminal`` 之前就先 fail closed，不讓「判準本身被動過」
    偽裝成一次乾淨的 gate 驗證。
    """

    try:
        envelope = terminal_contract.validate_envelope(raw)
    except terminal_contract.TerminalContractError:
        # 形狀問題交給既有的 per-phase schema 驗證發聲，這裡只負責矛盾偵測，
        # 不改寫既有錯誤訊息。
        return
    log_path = job.get("log_path")
    if not isinstance(log_path, str) or not log_path:
        return
    test_policy = _workflow_step_test_policy(registry, job)
    if _workflow_acceptance_definition_drifted(job, fresh_test_policy=test_policy):
        raise terminal_contract.TerminalContractError(
            "workflow card 的驗收判準（test_policy）自派工後已變動："
            f"pinned={job.get('workflow_test_policy')!r}，現值={test_policy!r}；"
            "不得沿用新值讓 builder 自報靜默通過",
            reason="workflow-acceptance-definition-drift",
            validation_path="$.gate_evidence",
            errors=(
                {
                    "pinned_test_policy": job.get("workflow_test_policy"),
                    "current_test_policy": test_policy,
                },
            ),
        )
    terminal_contract.authorize_terminal(
        envelope,
        ledger_path=terminal_contract.gate_ledger_path(_job_control_log_path(job, log_path)),
        require_ledger=job.get("workflow_phase") in GATE_LEDGER_REQUIRED_PHASES,
        test_policy=test_policy,
        expected_gate_names=_expected_gate_names_for_test_policy(test_policy),
    )


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
        # #261 R3：StructuredOutput 有時把 canonical payload 包在白名單外層鍵裡；
        # 只剝一層，未知形狀不處理（留給下方統一終止）。
        unwrapped = _unwrap_structured_output(value)
        if unwrapped is not None:
            return unwrapped
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


def _is_stale_terminalized_failed_job(job: Mapping[str, object]) -> bool:
    """#260 D6：把「exited 且 exit code 非 0」併入既有 ``status == 'failed'`` 的
    stale-terminal 判定。

    `resume_workflow_run` 的 replacement 選擇與 `_dispatch_workflow_card` 的
    `retryable_latest` 過去只認 ``status == "failed"``；repair job 以非 0 exit
    code 正常終止（``status == "exited"``）時不落在這條件內，導致第一次
    operator resume 只重新回報 stale job 的 `job-failed`，要再執行一次才
    dispatch replacement。收斂點刻意選在既有條件式旁新增一個並列分支，不動
    exited/0 的既有三條路徑（unbound terminal recovery、malformed schema
    retry、正常 terminalize）。
    """

    if job.get("status") == "failed":
        return True
    return job.get("status") == "exited" and type(job.get("exit_code")) is int and job.get("exit_code") != 0


def _retryable_nonpassing_workflow_terminal(job: Mapping[str, object]) -> bool:
    """Recognize an immutable, bound card terminal that explicitly requested a stop.

    #717 裁決 (1a)：非通過狀態（``failed``／``needs_human``）下 ``candidate``
    **不再**被要求是 40-hex SHA，``null`` 與任何字串都合法。

    理由是結構性的：本函式是「模型明示要求停止」的唯一入口，而過去 build phase 的
    入場券是「交得出 `git rev-parse HEAD`」。於是**唯一一種本契約無法表達的失敗
    模式，恰好是最需要被表達的那一種**——「我一條命令都跑不了」（#716）的模型結構
    上取不到 HEAD，只能填 contract 裡看得到的 64-hex `source_revision`，判準不過
    ⇒ 掉進 :func:`_malformed_workflow_card_terminal` ⇒ 被當成 schema 壞掉重派，
    直到 `card-terminal-schema-retry-exhausted`。

    放寬是安全的，因為**非通過狀態下沒有任何下游會讀這個 candidate**（沿用 #261
    R4／D6 的「唯讀診斷不授予 candidate authority」分離）。回 ``True`` 之後的三個
    消費點都只把它當布林旗標：

    1. :func:`_discard_failed_planner_sandbox` —— 只當 admission 判準；sandbox 路徑
       由 ``job`` 欄位與 ``sha256(f"{run_id}:{card}")`` 導出，不讀 terminal。
    2. :func:`_dispatch_workflow_card` 的 ``retryable_latest`` —— 只決定「不回傳舊
       job、改派新的」；新 job 的 ``dispatch_head``／``subject_head`` 取自 ``run``。
    3. :func:`resume_workflow_run` 的 ``retry_failed`` 分支 —— 同 2。

    唯一會把 terminal 的 ``candidate`` 寫進 run state 的是
    :func:`terminalize_workflow_job`，而它在讀 ``candidate`` **之前**就已經對
    ``status != "passed"`` 擲 ``ValueError``（見該函式 "did not pass" 那一行）；
    :func:`_is_exact_legacy_agy_recovery` 同樣要求 ``status == "passed"``。
    ``passed`` 的 40-hex 判準因此一個位元都沒動。

    plan phase 一併放寬（原本要求 ``candidate is None``）：判準的理由是「非通過狀態
    下 candidate 沒有授權語意」，與 phase 無關；只放寬 build 會讓 plan 卡踩到同一個
    catch-22 的另一半（模型多寫一個 candidate 就被判 schema 壞掉）。
    """

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
    if raw.get("schema_version") == terminal_contract.TERMINAL_SCHEMA_VERSION:
        raw = _canonicalize_card_terminal(raw)
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
        # #717：型別仍收斂（``None`` 或字串），只是不再驗 40-hex。非字串、非 null
        # 的值（數字、物件…）仍是真的 schema 壞掉，維持 fail closed。
        and (candidate is None or isinstance(candidate, str))
        and phase in {"plan", "build"}
        and isinstance(outputs, list)
        and all(isinstance(ref, str) for ref in outputs)
    )


def _declared_card_terminal_status(job: Mapping[str, object]) -> str | None:
    """#717：讀出 card terminal envelope 上模型自述的 ``status``。

    只在 :func:`_retryable_nonpassing_workflow_terminal` 已經認可的形狀上呼叫，
    因此這裡不重做形狀驗證；讀不到一律回 ``None``（敘事欄位，不得 fail closed）。
    """

    try:
        raw = _extract_terminal_json(job.get("log_path"))
    except Exception:
        return None
    status = raw.get("status")
    return status if isinstance(status, str) and status else None


def _malformed_workflow_card_terminal(job: Mapping[str, object]) -> bool:
    """Recognize plan/build terminals that cannot be bound as a passed workflow-card.

    #717 迴歸釘住：**合法的明示停止不得被判成 schema mismatch**。這條保證由下面
    第一行的早退提供——:func:`_retryable_nonpassing_workflow_terminal` 認得的形狀
    在此一律回 ``False``，因此不消耗 schema retry 額度（那份額度是給「模型寫壞
    JSON」的，不是給「執行環境壞掉」的）。下面 ``status != "passed"`` 那一行看似
    「任何非 passed 一律當 schema mismatch」，但它只會看到早退**沒有**接住的殘餘：
    真的形狀壞掉、或綁定對不上（run_id／card_id 不符）的 payload。
    """

    if _retryable_nonpassing_workflow_terminal(job):
        return False
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
        return True
    if raw.get("schema_version") == terminal_contract.TERMINAL_SCHEMA_VERSION:
        raw = _canonicalize_card_terminal(raw)
    required = {
        "schema_version", "kind", "status", "run_id", "card_id", "candidate", "outputs",
    }
    if (
        set(raw) != required
        or type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != 1
        or raw.get("kind") != "workflow-card"
    ):
        return True
    if raw.get("status") != "passed":
        return True
    candidate = raw.get("candidate")
    if job.get("workflow_phase") == "build":
        return (
            not isinstance(candidate, str)
            or verification.SAFE_SHA_RE.fullmatch(candidate) is None
        )
    return candidate is not None


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
    if raw.get("schema_version") == terminal_contract.TERMINAL_SCHEMA_VERSION:
        raw = _canonicalize_card_terminal(raw)
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
    # #650：candidate 樹的定錨從「等於 builder job 記錄的工作區」換成「等於本 run
    # ＋本 candidate 的**唯一推導點**算出來的那一棵 Manager-owned clone」
    # （`_reviewer_candidate_workspace_id`）。推導點比兄弟 job 的欄位穩定——後者正
    # 是本票要拆掉的耦合，而且那個目錄在成果回收之後隨時可能被回收掉。
    #
    # 舊形狀（`workflow_repo_root` == 那張 build 卡的工作區）**保留為容忍面**：升級
    # 當下正在進行、reviewer job 已派出去的 run 仍要走得完 operator recovery。兩者
    # 都是「這個 repo_root 真的是本 run 的 candidate 樹」的定錨，容忍不放寬語意。
    builder_worktree = builder.get("worktree")
    recorded_root = Path(repo_root_value).resolve()
    try:
        expected_root = job_workspace.workspace_path(
            worktree_root_for(Path(str(run.workspace_root))),
            _reviewer_candidate_workspace_id(run, str(run.candidate_head).lower()),
        ).resolve()
    except (ValueError, OSError):
        expected_root = None
    legacy_root = (
        Path(builder_worktree).resolve() if isinstance(builder_worktree, str) else None
    )
    if recorded_root not in {root for root in (expected_root, legacy_root) if root is not None}:
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


def _reviewer_input_patterns(run, effective_inputs: tuple[str, ...]) -> tuple[str, ...]:
    """Ensure every frozen planning authority ref is proven into the reviewer's
    input snapshot even when the review card itself declares no ``requires``
    (the deck default): #219 closes the gap where a reviewer job could
    dispatch and PASS a candidate without ever seeing the frozen plan.
    """
    missing = tuple(
        dict.fromkeys(
            item.ref
            for item in run.planning_authority
            if not any(fnmatch.fnmatch(item.ref, pattern) for pattern in effective_inputs)
        )
    )
    return effective_inputs + missing


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


_CHECKBOX_VOLATILE_PLAN_BASENAMES = frozenset({"tasks.md", "todo.md"})
_CHECKBOX_RE = re.compile(r"^(\s*(?:[-+*]|\d+[.)])\s+)\[[xX]\]", re.M)


def _checkbox_insensitive_equal(baseline: bytes, current: bytes) -> bool:
    """#310：卡片契約要求 builder 勾選 tasks/todo 的 checkbox；只有 checkbox
    狀態差異（``- [x]``→``- [ ]`` 正規化後相等）視為未漂移。任何其他 byte 差異
    仍屬 drift。"""
    try:
        base_text = baseline.decode("utf-8")
        cur_text = current.decode("utf-8")
    except UnicodeDecodeError:
        return False
    normalize = lambda text: _CHECKBOX_RE.sub(lambda m: m.group(1) + "[ ]", text)
    return normalize(base_text) == normalize(cur_text)


def _authority_map_with_checkbox_tolerance(run, *, candidate_root: Path) -> dict[str, str]:
    """#310 補遺：reviewer 的 frozen authority 驗證沿用 checkbox-insensitive 容忍。

    tasks/todo（kind=plan）在候選 worktree 的 checkbox 勾選不視為 drift；容忍
    成立時以候選內容的實際 hash 作為 pinned 期望值——reviewer 收到的 input
    snapshot 就是這份內容，hash 必須對得上實檔。其他差異維持 baseline，使
    `verify_authority_in_input_snapshot` 照舊 fail-closed。baseline bytes 取自
    operator_root 的同 ref 檔案，且必須先驗證其 hash 等於 authority baseline。
    """
    operator_root = Path(run.workspace_root).resolve()
    mapping: dict[str, str] = {}
    for item in run.planning_authority:
        expected = item.baseline_sha256
        if item.kind == "plan" and Path(item.ref).name in _CHECKBOX_VOLATILE_PLAN_BASENAMES:
            candidate_matches = _safe_input_matches(candidate_root, item.ref)
            baseline_matches = _safe_input_matches(operator_root, item.ref)
            if len(candidate_matches) == 1 and len(baseline_matches) == 1:
                candidate_data = candidate_matches[0].read_bytes()
                baseline_data = baseline_matches[0].read_bytes()
                digest = hashlib.sha256(candidate_data).hexdigest()
                if (
                    digest != expected
                    and hashlib.sha256(baseline_data).hexdigest() == expected
                    and _checkbox_insensitive_equal(baseline_data, candidate_data)
                ):
                    expected = digest
        mapping[item.ref] = expected
    return mapping


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
                archived_ref = _planning_artifact_relative_path_after_archive(
                    run,
                    workspace=operator_root,
                    ref=ref,
                    digest=authority[ref].baseline_sha256,
                ).as_posix()
                if archived_ref != ref:
                    source_matches = _safe_input_matches(operator_root, archived_ref)
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
                # #310：tasks/todo（kind=plan）的 checkbox 勾選是卡片契約的既定
                # 行為，不得視為 drift。baseline bytes 取自 operator_root 的同
                # ref 檔案，且必須先驗證其 hash 等於 authority baseline，才可
                # 作為 checkbox-insensitive 比對的基準；其餘任何差異 fail-closed。
                tolerated = False
                if (
                    bound.kind == "plan"
                    and Path(ref).name in _CHECKBOX_VOLATILE_PLAN_BASENAMES
                ):
                    baseline_matches = _safe_input_matches(operator_root, ref)
                    if len(baseline_matches) == 1:
                        baseline_data = baseline_matches[0].read_bytes()
                        if (
                            hashlib.sha256(baseline_data).hexdigest()
                            == bound.baseline_sha256
                            and _checkbox_insensitive_equal(baseline_data, data)
                        ):
                            tolerated = True
                if not tolerated:
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
        and not _malformed_workflow_card_terminal(job)
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


def _reviewer_candidate_workspace_id(run, candidate: str) -> str:
    """verify／review 卡那棵 candidate 樹的識別（#650）——**唯一推導點**。

    形狀與 `work_bridge._ship_workspace_id()` 對齊（`wf-<run 摘要>-<段>-<candidate
    前綴>`），只換中間那一段。穩定於 **(run, candidate)** 而**不是** per-job，這一點
    是被產品契約逼出來的，不是省一次 clone 的優化：

    `adversarial-review` 的 `requires` 是 `reports/review/*<task-slug>*.md`，也就是
    前一張 `code-review` 卡的產出；而 canonical report 是 Manager 在
    `terminalize_workflow_job()` 裡發佈到那張卡的 `workflow_repo_root` 的**未追蹤
    檔**（#653 之後不再被 ship 段清掉）。同一個 candidate 的 verify／review 卡因此
    必須共用同一棵樹，下一張卡的 `_workflow_input_snapshot()` 才 glob 得到它。
    per-job 一棵樹會讓那個 glob 落空 ⇒ `workflow declared input missing`。

    candidate 前進（retry-build、post-archive 重驗）就換一棵樹——那也正是對的：新
    candidate 的 review phase 從頭跑起，舊 candidate 的 report 不該被它讀到。舊的
    那一棵原地留著，回收交給 `cortex work gc`，與 build／ship 卡的 clone 同一套。
    """

    if verification.SAFE_SHA_RE.fullmatch(candidate) is None:
        raise ValueError("workflow reviewer candidate invalid")
    digest = hashlib.sha256(run.run_id.encode()).hexdigest()[:10]
    return f"wf-{digest}-review-{candidate[:12]}"


def _require_reviewer_candidate_workspace(
    worktree: Path, *, branch: str, candidate: str
) -> None:
    """candidate 樹開工前的三條不變式：branch 對、HEAD ＝ candidate、追蹤檔無漂移。

    - **branch**：job 記錄的 `branch` 與這棵樹實際 checkout 的 branch 必須一致
      （與 `work_bridge._require_pristine_ship_workspace()` 同一條理由）。
    - **HEAD ＝ candidate**：`_verify_exact_candidate()` 在採信時對 reviewer 的
      `workflow_repo_root` 跑的正是 `rev-parse HEAD == candidate`；這裡先驗一次，
      讓「樹不對」在派工當下就炸開，而不是等到一整個 session 跑完才發現。
      `_authority_map_with_checkbox_tolerance()` 讀的 candidate 內容也以這條為前提。
    - **追蹤檔無漂移**（`--untracked-files=no`）：**未追蹤檔刻意放行**——canonical
      report 就是未追蹤檔，而它們正是 `code-review` → `adversarial-review` 的交接
      載體（見 `_reviewer_candidate_workspace_id`）。這裡不能用 ship 段那條「完全
      乾淨」，否則第二張 review 卡永遠開不了工。

    這棵樹只有 Manager 寫（input seed ＋ canonical report 發佈），因此任何一條不成
    立都代表有第三方動過它——fail-closed，不刪、不自癒。
    """

    for argv, expected, failure in (
        (["symbolic-ref", "--quiet", "--short", "HEAD"], branch, "branch"),
        (["rev-parse", "HEAD"], candidate, "head"),
    ):
        probe = subprocess.run(
            ["git", "-C", str(worktree), *argv],
            shell=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0 or probe.stdout.strip().lower() != expected.lower():
            raise ValueError(f"workflow reviewer candidate workspace {failure} mismatch")
    tracked = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=no"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if tracked.returncode != 0 or tracked.stdout.strip():
        raise ValueError("workflow reviewer candidate workspace has tracked drift")


def _reviewer_candidate_workspace(
    *,
    run,
    branch: str,
    candidate: str,
    creator=None,
) -> Path:
    """#650：verify／review 卡的 candidate 樹——**Manager-owned**，不是 builder 的 clone。

    ## 換掉的是什麼

    在此之前 verify／review 卡以 `builder_jobs[-1]["worktree"]`（前一張 build 卡的
    工作區）為 candidate 樹，六個用途全掛在它身上。#648 把 build phase 的工作區改成
    per-job 之後，一個 run 會累積 N 棵這種樹（每棵約 35MB），而
    `_harvest_build_candidate()` 落地之後**被採信的卡的工作區已經沒有任何獨佔資訊**
    ——bundle 已封存、commit 已在來源樹裡。唯一還讓它回收不掉的就是這條引用。

    ## 形狀（沿用 #653 的 `work_bridge._manager_ship_workspace()`）

    以 `run.candidate_head` 為 base、用 `seams.ScriptWorktreeCreator` 在**來源樹**上
    clone 一份。來源樹是 `cortex-manager` 擁有且可寫，Manager 對自己 clone 出來的樹
    自然是 owner。creator 的兩道既有守衛在這條 lane 上剛好就是要的：

    - `rev-parse --verify <candidate>^{commit}`：來源樹必須已經有這個 commit——那正是
      `_harvest_build_candidate()`（#637 bundle ＋ append-only spool）保證的不變式
      （build 卡）與 `work_bridge._harvest_manager_ship_commit()`（#649，archive
      commit）保證的不變式。回收沒走完就 provision 不起來。
    - `merge-base --is-ancestor <branch> <candidate>`：delivery branch 不得帶著
      candidate 以外的 commit（#613 的形狀）。

    ## 順帶收掉的一個 #641 同型缺口

    舊模型下 Manager 在 reviewer 派工當下對 builder 的 clone 做的**不只是讀**：
    `_workflow_input_snapshot()` 會把缺席的 planning authority 檔案 seed 進去
    （`mkdir` ＋ `mkstemp` ＋ `os.replace`），`planning_runtime._tree_snapshot()` 會
    遞迴走完整棵樹。三分部署下那棵樹是 `0700 cortex-builder`、且 #641 已收掉 Manager
    的唯讀 ACL ⇒ 這兩步都是 `Permission denied`。換成 Manager 自己的樹之後，這條路徑
    不需要任何指向 job 工作樹的授權。

    ## 紅線

    沒有 `--reference`／`--shared`／任何把 object store 接回共用的優化（#623 判定共用
    object store 與三分隔離互斥），也沒有「Manager fetch 一棵 job 的 clone」。
    """

    source_repo = getattr(run, "workspace_root", None)
    if not isinstance(source_repo, str) or not source_repo:
        raise ValueError("workflow reviewer candidate workspace source repo missing")
    source = Path(source_repo)
    pool = worktree_root_for(source)
    workspace_id = _reviewer_candidate_workspace_id(run, candidate)
    target = job_workspace.workspace_path(pool, workspace_id)
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError("workflow reviewer candidate workspace path is not a directory")
    if target.is_dir():
        marker = job_workspace.read_marker(target)
        reusable = (
            job_workspace.is_job_clone(target)
            and isinstance(marker, dict)
            and marker.get("branch") == branch
            and str(marker.get("base", "")).lower() == candidate.lower()
        )
        if reusable:
            # 重用**不打回 pristine**：未追蹤的 canonical report 是同一個 candidate
            # 的下一張 review 卡的宣告輸入（見 `_reviewer_candidate_workspace_id`）。
            # ship 段的 `_reset_ship_workspace()` 之所以能 `clean -ffdx`，是因為它
            # 要 commit 一個 exact candidate；這裡的契約相反。
            _require_reviewer_candidate_workspace(
                target, branch=branch, candidate=candidate
            )
            return target
        if not job_workspace.is_job_clone(target):
            # 認不出這是什麼就**不刪**（#478 的爆炸半徑教訓）。
            raise ValueError("workflow reviewer candidate workspace path is occupied")
        job_workspace.remove_clone(target)
    if creator is None:
        creator = seams.ScriptWorktreeCreator(repo=source, wt_root=pool, base="main")
    created = Path(creator.create(branch, job_id=workspace_id, base_sha=candidate))
    _require_reviewer_candidate_workspace(created, branch=branch, candidate=candidate)
    return created


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


def _prepare_reviewer_sandbox_container(parent: Path) -> None:
    """建（並收斂）reviewer sandbox 的容器目錄（#742）。

    `0701`＝job 帳號可 traverse、不可列目錄——`dispatch-worktree-pool` 的既有先例
    （#641 記載）。容器上**只有** traverse：具名 rwX 一律落在 per-job 那一格
    （:func:`_grant_reviewer_sandbox_access`），#710 的 per-job 隔離裁決不變。
    """

    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, 0o701)


def _grant_reviewer_sandbox_access(sandbox: Path) -> str | None:
    """把建好的 per-job sandbox 交給 reviewer 執行帳號（#742；#710 的 reviewer 版）。

    sandbox 整棵由 Manager 以 `UMask=0077` 建立（clone、bind 目標、input seed），
    reviewer principal 一個 inode 都讀不到——`inherited-default-acl` 的 reach 模型
    對這個 pool 不成立：它不在部署清單上（verify 首走才被 mkdir 出來），且 default
    ACL 的繼承會被 Manager 的 umask 把 mask 歸零（#736 在 gate 快照上的同一個交互）。
    修法比照 #710：owner（Manager）以 `setfacl -R` 顯式授 per-job 那一格，mask 由
    setfacl 重算。帳號由 `resolve_job_account(role=review)` 解（#657 的單一導出）；
    **不在 passwd 時整支略過**——direct／單 UID 模式同 UID 本就可達，且與
    `ensure_workspace_reachable` 的既有處置一致，不是 fail-open（降權派工路徑在
    `prepare_systemd_template()` 對帳號存在性 fail-closed）。
    """

    account = job_runner.resolve_job_account(os.environ, role=job_runner.JOB_ROLE_REVIEW)
    try:
        pwd.getpwnam(account)
    except KeyError:
        return None
    return job_workspace.grant_workspace_acl(
        sandbox,
        (
            job_workspace.WorkspaceAclGrant(
                account=account, access_perms="rwX", default_perms="rwX"
            ),
        ),
    )


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
    _prepare_reviewer_sandbox_container(parent)
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
    try:
        _grant_reviewer_sandbox_access(sandbox)
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
    # #629：降權模式下 job wrapper 不跑 gate（跑了就是模型自證，見
    # `launcher._should_run_gates`），ledger 因此在這一刻還不存在。這裡以**第四個
    # 帳號**（`cortex-gate`）重跑 operator 宣告的命令，產出經 spool 回到 Manager
    # 手上、由 Manager 自己落地——`direct` 模式下本呼叫是 no-op（ledger 已由
    # wrapper 寫好），行為與 #629 之前逐字相同。
    #
    # 位置在 `_assert_terminal_gate_consistency` **正前方**：那裡是唯一會讀 ledger
    # 的採信點，證據必須在被讀之前就已經落地，且**不得**在採信開始之後才產生。
    _run_gate_execution_identity(job, registry=registry)
    # #261 R2／D3：矛盾偵測排在任何狀態採信之前。放在 per-phase schema 驗證之前，
    # 是為了避免「先按 passed 走一段流程、後面才發現不對」造成的部分副作用。
    # #307：帶入 registry 讓 red-required 卡的測試 gate 語意反轉生效。
    _assert_terminal_gate_consistency(raw, job=job, registry=registry)
    declared_outputs = job.get("workflow_outputs")
    if not isinstance(declared_outputs, list):
        raise ValueError("workflow job declared outputs missing")
    candidate: str | None = None
    inline_reports: tuple[tuple[str, str], ...] = ()
    if phase in {"plan", "build"}:
        if raw.get("schema_version") == terminal_contract.TERMINAL_SCHEMA_VERSION:
            # canonical envelope（#261 D1）：多帶 diagnostics 與 gate_evidence 兩個
            # 欄位；其餘欄位語意與 legacy 一致，往下沿用同一條驗證路徑。
            raw = _canonicalize_card_terminal(raw)
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
        # #261 R1：verifier 也必須能誠實回報 failed／needs_human，不得只有成功形狀
        # 合法。非通過狀態在此 fail closed 為可操作錯誤，而不是被誤判成 schema 壞掉。
        if raw.get("status") in terminal_contract.NON_PASSING_STATUSES:
            raise ValueError(
                f"workflow verification terminal reported non-passing status: {raw.get('status')}"
            )
        if (
            set(raw) != required
            or raw.get("schema_version") != 1
            or raw.get("kind") != "workflow-verification-result"
            or raw.get("status") not in {"verified", "passed"}
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
        expected_authority_hashes = {
            row["path"]: row["sha256"]
            for row in job.get("workflow_input_snapshot", [])
            if isinstance(row, dict) and row.get("authority") == "planning-authority"
        }
        required = {"schema_version", "kind", "reason", "findings", "reports"}
        if expected_authority_hashes:
            required = required | {"authority_hashes"}
        # #261 R1：review card 同樣必須能誠實回報 failed／needs_human。status 是
        # canonical envelope 的選填欄位（review verdict 本身由 findings 決定），
        # 在此先取出並攔截非通過狀態，再做既有的 exact key-set 驗證。
        if "status" in raw:
            declared_review_status = raw.get("status")
            if declared_review_status in terminal_contract.NON_PASSING_STATUSES:
                raise ValueError(
                    f"workflow review terminal reported non-passing status: {declared_review_status}"
                )
            if declared_review_status != "passed":
                raise ValueError("workflow review terminal schema invalid")
            raw = {key: value for key, value in raw.items() if key != "status"}
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
        verdict_payload = {
            "schema_version": foreign_review.REVIEW_SCHEMA_VERSION,
            "builder_job_id": builder_job_id,
            "reviewer_job_id": str(job["job_id"]),
            "candidate": candidate,
            "launch_identity": reviewer_identity,
            "findings": raw["findings"],
        }
        if expected_authority_hashes:
            verdict_payload["authority_hashes"] = raw.get("authority_hashes")
        verdict = foreign_review.validate_review_verdict(
            verdict_payload,
            builder_job_id=builder_job_id,
            reviewer_job_id=str(job["job_id"]),
            candidate=candidate,
            launch_identity=reviewer_identity,
            expected_authority_hashes=expected_authority_hashes or None,
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
    """已發佈的 canonical report 不在 `repo_root` 時，是否有 Manager 的刪除意圖佐證。

    #653 之後**產生**這種 evidence 的路徑已經沒有了：ship 段改在自己的 pristine
    clone 裡動手，不再需要（也不再有權）到 job 的工作樹裡刪 report，因此「報告缺席」
    不再是交付流程製造出來的正常狀態。本函式保留為**向後相容的容忍面**——升級當下
    正在進行、已經寫過 `report-cleanup` evidence 的 run 仍要走得完。沒有那份 evidence
    時一律回 False（缺席 ＝ artifact drift，fail-closed），語意與過去逐條相同。
    """

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


def _planning_conflict_hint(path: Path, *, run_id: str | None) -> str:
    """#535：no-clobber 衝突訊息附上殘留檔的歸屬與時間，供 operator 直接判讀。

    `owner=` 取自檔名帶的 run identity（`brainstorm-<run_id>-<hash>.json`，見
    `planning.brainstorm_evidence_filename`）；舊命名的殘留檔沒有這段，回
    `legacy-unscoped`——那正是「前代殘留」最典型的樣態。
    """

    owner = "legacy-unscoped"
    match = re.match(r"^[a-z-]+-(workflow-[0-9a-f]{20})-[0-9a-f]+\.json$", path.name)
    if match is not None:
        owner = match.group(1)
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        modified = "unknown"
    return f"existing owner={owner} mtime={modified} publishing run={run_id or 'unknown'}"


class PlanningPublicationDrift(RuntimeError):
    """A durable planning intent cannot be safely committed or rolled back."""


# #536：journal 的 schema 版本。v3 新增 `phase` 欄位（見
# `_PlanningPublicationTransaction.prepare_commit`），把「發佈 artifacts」與
# 「registry 提交 run 狀態」之間那條事務邊界寫成 durable 事實，恢復路徑才能
# 分辨「崩在發佈中途」與「崩在提交邊界」。v2 是升級前既有 journal 的格式，
# 恢復路徑必須繼續接受它——實際部署上就有 v2 的殘留（#536 現場的
# `workflow-7a430d31eff66ef13630`），自癒不能要求 operator 先手動搬遷。
_PLANNING_TRANSACTION_SCHEMA_VERSION = 3
_PLANNING_TRANSACTION_SCHEMA_VERSIONS = (2, _PLANNING_TRANSACTION_SCHEMA_VERSION)
_PLANNING_TRANSACTION_PHASES = ("publishing", "prepared")


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
        # #536：`publishing` = 檔案側還在發佈中，registry 提交尚未被嘗試；
        # `prepared` = 檔案側已全部落地且已 fsync，下一步就是唯一的 commit
        # point（registry 的 durable run row）。兩者都不改變 commit 判準
        # （判準永遠是 registry row 上有沒有這次的 brainstorm gate ref），
        # 只讓恢復路徑能誠實描述殘留是哪一種。
        self.phase = "publishing"
        self.journal_root = journal_root.resolve() if journal_root is not None else None
        self.journal_path = (
            self.journal_root / "planning-transactions" / f"{run_id}.json"
            if self.journal_root is not None
            else None
        )

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": _PLANNING_TRANSACTION_SCHEMA_VERSION,
            "kind": "planning-publication-intent",
            "run_id": self.run_id,
            "root": str(self.root),
            "phase": self.phase,
            "operations": self.operations,
            "expected_gate_ref": self.expected_gate_ref,
        }

    def prepare_commit(self) -> None:
        """封住檔案側、宣告下一步是 registry 這個唯一的 commit point。

        呼叫端必須在「所有 artifacts／evidence 都已發佈完成」與「registry
        提交 run 狀態」之間呼叫一次；之後不得再 `publish()`。這一筆
        durable 記錄本身不是 commit——commit 由 registry 的原子寫入決定——
        它只是把事務邊界寫進 journal，讓崩潰後的恢復路徑（見
        `reconcile_planning_transactions`）能區分兩種殘留並如實回報。
        """

        if self.phase != "prepared":
            self.phase = "prepared"
            self._persist()

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
        if self.phase != "publishing":
            # #536：事務邊界一旦封住（prepare_commit），檔案側就不得再變動——
            # 否則 journal 記載的「已全部落地」與磁碟事實脫節，恢復路徑的
            # 前滾驗證會拿不到一致的 after_hash 集合。
            raise ValueError("planning publication is already prepared for commit")
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
                # #535：舊訊息只有相對路徑，operator 得自己挖 mtime 對時間軸才
                # 知道那份殘留檔屬於哪個 run、是不是已 superseded 的前代產物。
                # 這裡直接附上落點歸屬（檔名帶的 run identity）與 mtime。
                raise ValueError(
                    f"planning artifact no-clobber conflict: {relative}"
                    f" ({_planning_conflict_hint(path, run_id=self.run_id)})"
                )
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

    def rollback(self, *, adopted: Callable[[Path], bool] | None = None) -> tuple[str, ...]:
        """回退本事務所有已落地的變動；回傳被 ``adopted`` 判定為「已被採納」
        而**刻意跳過**的路徑。

        ``adopted``（只有崩潰後的 sweep 會傳）用來擋住「這份未提交的發佈殘留
        後來已被 operator 納入 git 追蹤」的情形——比照
        `work_actions._gc_one_abandoned_planning_artifact` 的既有紀律：已被
        git 追蹤的檔案不是未提交殘留，刪掉等同銷毀 operator 的工作（#507 的
        教訓）。in-process 的回退一律不傳，語意與修法前逐位元組相同。
        """

        skipped: list[str] = []
        for operation in reversed(self.operations):
            path = Path(str(operation["path"]))
            if adopted is not None and adopted(path):
                skipped.append(str(path))
                continue
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
        return tuple(skipped)

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
    def reconcile(
        cls,
        *,
        root: Path,
        journal_root: Path,
        run,
        adopted: Callable[[Path], bool] | None = None,
    ) -> dict[str, object] | None:
        """把一份 durable 發佈意圖收斂到與 registry 權威狀態一致。

        判準只有一條：**registry 的 run row 上有沒有這次的 brainstorm gate
        ref**。有 → 前滾（驗證每個已提交產物仍逐位元組吻合，然後退役
        journal）；沒有 → 回退（把檔案側還原成發佈前的樣子）。沒有 journal
        時回傳 ``None``；其餘回傳 ``{"outcome", "phase", "skipped"}``。
        """

        path = journal_root.resolve() / "planning-transactions" / f"{run.run_id}.json"
        if path.is_symlink():
            raise PlanningPublicationDrift("planning transaction journal symlink rejected")
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanningPublicationDrift("planning transaction journal is unreadable") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") not in _PLANNING_TRANSACTION_SCHEMA_VERSIONS
            or payload.get("kind") != "planning-publication-intent"
            or payload.get("run_id") != run.run_id
            or payload.get("root") != str(root.resolve())
            or not isinstance(payload.get("operations"), list)
            or payload.get("expected_gate_ref") is not None
            and not isinstance(payload.get("expected_gate_ref"), dict)
        ):
            raise PlanningPublicationDrift("planning transaction journal is invalid")
        # #536：v2 沒有 phase 欄位，一律視為 `publishing`（升級前的 journal
        # 從未宣告過事務邊界）；v3 必須帶合法 phase。phase 純粹是診斷用的
        # durable 事實，**不參與** commit 判準——判準永遠是 registry row。
        if payload["schema_version"] == 2:
            if "phase" in payload:
                raise PlanningPublicationDrift("planning transaction journal is invalid")
            phase = "publishing"
        else:
            phase = payload.get("phase")
            if phase not in _PLANNING_TRANSACTION_PHASES:
                raise PlanningPublicationDrift("planning transaction phase is invalid")
        transaction = cls(root=root, run_id=run.run_id, journal_root=journal_root)
        transaction.phase = phase
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
            return {"outcome": "committed", "phase": phase, "skipped": ()}
        try:
            skipped = transaction.rollback(adopted=adopted)
        except RuntimeError as exc:
            raise PlanningPublicationDrift("planning uncommitted rollback drift") from exc
        return {
            "outcome": "adopted" if skipped else "rolled-back",
            "phase": phase,
            "skipped": skipped,
        }


# #536：崩潰殘留的 journal 要多久之後才被視為「不可能還在飛」。發佈側最後
# 一次 fsync 到 registry 提交之間正常是次秒級（`prepare_commit` 之後只剩
# `_validated_brainstorm_planning_authority` 與一次原子寫入），這裡留 5 分鐘
# 的極寬裕餘裕，純粹是為了讓 sweep 絕不可能誤傷一個仍在進行中的發佈——
# 即使它是由 daemon 之外的行程（operator 的前景 `cortex work start`）發起。
_PLANNING_TRANSACTION_GRACE_SECONDS = 300.0


def _planning_artifact_adopted_by_git(path: Path, *, workspace_root: Path) -> bool:
    """殘留檔是否已被 operator 納入 git 追蹤（＝不再是「未提交的發佈殘留」）。

    與 `work_actions._gc_one_abandoned_planning_artifact` 同一條判準與同一個
    理由：git 已追蹤代表 operator 已經採納這份產出，刪掉就是銷毀他人工作
    （#507）。無法判定時（workspace 不是 git repo、git 不可用）回傳 False，
    讓回退照常進行——回退本身還有逐位元組的 CAS 護欄（`before_exists=False`
    ＋ `after_hash` 必須完全吻合），不依賴這層。
    """

    try:
        relative = path.resolve().relative_to(workspace_root.resolve())
    except (ValueError, OSError):
        return False
    try:
        tracked = subprocess.run(
            [
                "git", "-C", str(workspace_root), "ls-files", "--error-unmatch",
                "--", str(relative),
            ],
            shell=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return tracked.returncode == 0


def reconcile_planning_transactions(
    *,
    registry,
    coordinator_root: str | Path,
    now: float | None = None,
    grace_seconds: float = _PLANNING_TRANSACTION_GRACE_SECONDS,
) -> list[dict[str, object]]:
    """#536：把 planning-transactions journal 目錄整個掃過，逐份收斂。

    根因：「發佈 planning artifacts」與「更新 run 狀態」是兩次分離的 durable
    寫入，中間崩潰會留下「artifacts 已落地、run 狀態停在原地」的中間態。
    journal 早就記了 before/after hash，但 `reconcile()` **只能由持有該 run
    的呼叫端逐 run 觸發**（define 起始、`resume_workflow_run`）——一旦那個
    run 離開 `ongoing`（superseded／done），或 run 從沒被任何迴圈再訪，
    journal 與它描述的殘留檔就永遠沒有人看，正是 #536 說的「對所有恢復迴圈
    隱形」。實測 coordinator root 上就躺著兩份這種孤兒 journal（其中一份正是
    #536 現場的 `workflow-7a430d31eff66ef13630`，run 已 superseded）。

    這個 sweep 是**唯一**的恢復路徑：不管 run 是 ongoing、superseded 還是
    done，只要 journal 還在就把它收斂掉，因此既有殘留與未來崩潰走同一條
    程式路徑自癒。每一份 journal 的結果都會回報並落 log，不得靜默。
    """

    journal_root = Path(coordinator_root).resolve()
    directory = journal_root / "planning-transactions"
    if directory.is_symlink() or not directory.is_dir():
        return []
    runs = {run.run_id: run for run in registry.list_workflow_runs()}
    moment = time.time() if now is None else now
    report: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        run_id = path.name[: -len(".json")]
        record: dict[str, object] = {"run_id": run_id}
        if path.is_symlink() or not path.is_file():
            record.update(outcome="invalid", detail="journal is not a regular file")
            logger.error(
                "planning-transaction-invalid run_id=%s detail=%s", run_id, record["detail"]
            )
            report.append(record)
            continue
        try:
            age = moment - path.stat().st_mtime
        except OSError as exc:
            record.update(outcome="invalid", detail=f"{type(exc).__name__}: {str(exc)[:120]}")
            logger.error(
                "planning-transaction-invalid run_id=%s detail=%s", run_id, record["detail"]
            )
            report.append(record)
            continue
        if age < grace_seconds:
            # 仍可能在飛：不碰，下一輪再看。
            record.update(outcome="in-flight", age_seconds=round(age, 3))
            report.append(record)
            continue
        run = runs.get(run_id)
        if run is None:
            # fail-closed：沒有 run row 就無法驗證 journal 宣稱的 workspace
            # root，不能拿 journal 自報的路徑去刪檔。留檔＋落 log，讓它可見。
            record.update(outcome="unknown-run", detail="no workflow run owns this journal")
            logger.error(
                "planning-transaction-orphan run_id=%s detail=%s", run_id, record["detail"]
            )
            report.append(record)
            continue
        workspace_root = Path(run.workspace_root)
        try:
            outcome = _PlanningPublicationTransaction.reconcile(
                root=workspace_root,
                journal_root=journal_root,
                run=run,
                adopted=lambda target: _planning_artifact_adopted_by_git(
                    target, workspace_root=workspace_root
                ),
            )
        except PlanningPublicationDrift as exc:
            record.update(outcome="drift", detail=str(exc)[:200], status=run.status)
            logger.error(
                "planning-transaction-drift run_id=%s status=%s detail=%s",
                run_id,
                run.status,
                str(exc)[:200],
            )
            if run.status == "ongoing" and "needs_human" not in run.facets:
                # 無法自動收斂的殘留必須有人接手——補 needs_human facet，
                # 讓 `cortex status` 的 attention 清單與 next_actions 有話說。
                try:
                    registry._manager_update_workflow_run(
                        run.run_id,
                        facets=tuple(dict.fromkeys((*run.facets, "needs_human"))),
                        gate_status="running",
                        needs_human_reason=diagnostic_reason(
                            "planning-publication-drift",
                            f"planning 發佈事務無法自動收斂：{str(exc)[:200]}",
                            source="manager.reconcile_planning_transactions",
                            run_id=run.run_id,
                            # sweep 走的是 `list_workflow_runs()` 的任意 row，
                            # 注入型測試會給不完整的替身；診斷欄位缺席不得讓
                            # 「呈現 needs_human」這件事本身失敗。
                            work_id=getattr(run, "work_id", None),
                            journal=str(path),
                        ),
                    )
                    record["surfaced"] = True
                except Exception as surface_exc:  # noqa: BLE001 - 呈現失敗不得吃掉診斷
                    logger.error(
                        "planning-transaction-surface-failed run_id=%s error=%s: %s",
                        run_id,
                        type(surface_exc).__name__,
                        str(surface_exc)[:200],
                    )
            report.append(record)
            continue
        if outcome is None:
            continue
        record.update(outcome, status=run.status)
        record["skipped"] = list(outcome.get("skipped", ()))
        logger.info(
            "planning-transaction-reconciled run_id=%s outcome=%s phase=%s status=%s skipped=%d",
            run_id,
            record["outcome"],
            record["phase"],
            run.status,
            len(record["skipped"]),
        )
        report.append(record)
    return report


# #416：`_publish_planning_artifacts` 對「檔案已存在但無/與目前 authority
# 不符」一律 fail-closed（見下方兩處 raise），這正是 abandon 未回滾已發佈
# 殘留檔（`work_actions._gc_abandoned_planning_artifacts` 修復前的缺口）撞見
# 下一世代重新發佈同一 destinations 時的典型死鎖地雷特徵——屬環境／狀態
# 殘留，不是模型內容缺陷。這兩個訊息子字串必須與下方兩個 raise 的字面文字
# 完全一致，`_is_planning_authority_residue_failure` 才能正確辨識。
_PLANNING_AUTHORITY_RESIDUE_MARKERS = (
    "planning artifact lacks current planning authority",
    "planning artifact current authority drift",
)


def _is_planning_authority_residue_failure(reason: str | None) -> bool:
    """判斷 brainstorm not-ready 的 reason 是否為 #416 的 authority 殘留死鎖。

    只窄判斷 `run_heterogeneous_brainstorm` 對 `artifact_writer` 例外包出的
    `primary-artifact-write-rejected: ...` 前綴、且訊息命中
    `_PLANNING_AUTHORITY_RESIDUE_MARKERS` 兩個明確子字串之一。
    `_publish_planning_artifacts` 其餘的內容型驗證錯誤（schema 不合法、路徑
    逃出 governed roots、artifact 未通過驗收……）刻意不在此範圍內，維持
    #393 既有的 `content` 分類與 fail-closed 意圖，不擴大分類映射。
    """

    return (
        reason is not None
        and reason.startswith("primary-artifact-write-rejected:")
        and any(marker in reason for marker in _PLANNING_AUTHORITY_RESIDUE_MARKERS)
    )


# --- planner launcher 的暫時性服務失敗 ----------------------------------------
#
# #533 先行實作，2026-08-15 隨 #499／#500／#487／#485 收編進
# `outcome_taxonomy`：三個 lane 共用同一張 markers 表，避免第七次同型漂移。
# 這裡保留別名與判準函式，因為 planning lane 的呼叫端與其測試以此為名。
_PLANNING_TRANSIENT_SERVICE_MARKERS = outcome_taxonomy.TRANSIENT_SERVICE_MARKERS


def _is_planning_transient_service_failure(reason: str | None) -> bool:
    """判斷 planning 失敗的 reason 是否為 launcher/service 層的暫時性失敗。

    命中者分類改落 `environment`（與 #416 的殘留例外同路），讓
    `_resume_decision` 浮現 `recover-planning`——暫時性服務錯誤等它過去重跑
    即可，不該燒掉一個世代或落入 abandon 死路。

    判準刻意窄：只認 CLI/service 層的暫時性錯誤樣態（服務不可用、限流、逾時、
    連線層失敗）。模型「內容不從」（回散文不回 JSON、schema 不合）不在此列，
    維持 `content` 分類與 fail-closed 意圖——分辨依據是這些字樣出自 launcher
    轉印的服務錯誤，不會出現在合法的規劃輸出裡。
    """

    return outcome_taxonomy.matches_transient_service_markers(reason)


# --- issue #554：operator worktree drift 是環境事件，不是內容缺陷 -------------
#
# #507 前，drift 的處置是把 operator worktree 整棵抹除再從 baseline 還原——
# 那確實會銷毀資料，把它歸 `content`（fail-closed、不給 recover-planning）
# 在當時是合理的保守。#543 之後處置已改為「一個位元組都不動、只備份與報告」，
# drift 於是變成一個純粹的環境事件：operator／其他 agent／編輯器在 planning
# 視窗內動了樹，本次 planning 結果不可信，但**沒有任何東西被破壞**，重跑
# planning 就好。維持 `content` 只會讓唯一出口是 abandon（燒一個世代），
# 這是 #507 comment 2 記錄、#543 明文留待後續的死鎖。
#
# 判準只認 `planning_runtime` 匯出的穩定前綴：訊息尾段已在 #543 改過一次
# （`changes rolled back` → `operator content preserved`），計數與 evidence 路徑
# 每次都不同，任何依賴尾段字面的判準都會再壞一次。
_PLANNING_WORKTREE_DRIFT_MARKER = planning_runtime.PLANNING_WORKTREE_DRIFT_MESSAGE_PREFIX


def _is_planning_worktree_drift_failure(reason: str | None) -> bool:
    """判斷 planning 失敗的 reason 是否為 operator worktree drift（#507／#554）。

    reason 的實際樣貌是 `run_heterogeneous_brainstorm` 對 launcher 例外包出的
    `<stage>-<kind>: ValueError: <drift message>`，因此比對用 `in` 而非
    `startswith`——前綴指的是「drift 訊息自己的前綴」，不是整個 reason 的前綴。

    判準刻意窄：只認 operator worktree 這一族。同一段 finally 另有
    `planning launcher modified disposable read-only sandbox`（launcher 寫壞了
    拋棄式沙箱）——那是 launcher 行為異常而非環境並行編輯，不在此列，維持
    既有 `content` 分類。
    """

    return reason is not None and _PLANNING_WORKTREE_DRIFT_MARKER in reason


# --- issue #682（#672 票 A）：拒因表裡的 environment 級拒因 -------------------
#
# `no-heterogeneous-planner` 今天一律落 `content`，而 `content` 在
# `_resume_decision` 一律不浮現 `recover-planning` ⇒ 死路。但那個 reason 底下
# 其實混著三類完全不同的失敗：拓撲問題（roster 真的沒有異質 planner）、輸出
# 不合約（#670 的 code fence／`agy models` 兩欄漂移），以及 executor 根本起不
# 來（#672 實測的沙箱／憑證缺口）。後者是環境事件，重跑或修環境就好，不該被
# 判成模型內容缺陷。
#
# 判準刻意**不**對整串 reason 做 substring-search：拒因表的 diagnostic 帶的是
# 模型 stdout 節錄，一個回「planning-executor-failed」的模型就能把 content
# 失敗偽裝成 environment。改成讀渲染端算好、且**錨在字串開頭**的 `grade=`
# 欄位（`model_identities.render_secondary_rejection_reason`）。
def _is_planning_candidate_rejection_environment_failure(reason: str | None) -> bool:
    """判斷 planning 失敗的 reason 是否為帶 environment 級拒因的逐候選拒因表。"""

    return is_environment_grade_rejection_reason(reason)


def _classify_planning_failure(reason: str | None) -> str:
    """brainstorm not-ready 的 reason → `environment` / `content` 的**單一判準**。

    #393 的預設是 `content`（fail-closed，`_resume_decision` 不浮現
    `recover-planning`）。四個具名例外改歸 `environment`：

    1. `_is_planning_authority_residue_failure`（#416）——abandon 未回滾的發佈
       殘留撞見 authority fail-closed，是狀態殘留而非模型內容缺陷。
    2. `_is_planning_transient_service_failure`（#533）——launcher/service 層
       的暫時性錯誤（503／限流／逾時），幾分鐘後自癒。
    3. `_is_planning_worktree_drift_failure`（#507／#554）——operator worktree
       在 planning 視窗內被動過；#543 之後不再銷毀資料，是環境事件。
    4. `_is_planning_candidate_rejection_environment_failure`（#682／#672 票 A）
       ——`no-heterogeneous-planner` 的逐候選拒因表裡有 environment 級拒因
       （job 起不來、executor 異常退出）。

    四個判準合成一個具名函式，是為了讓「reason → classification」這條映射有
    單一可測的入口（過去它只以三元表達式活在 `_run_define_stage` 中段，測不到
    也看不見）。
    """

    if (
        _is_planning_authority_residue_failure(reason)
        or _is_planning_transient_service_failure(reason)
        or _is_planning_worktree_drift_failure(reason)
        or _is_planning_candidate_rejection_environment_failure(reason)
    ):
        return "environment"
    return "content"


# --- issue #511：planning artifact 拒收的診斷面 -------------------------------
#
# 修法前拒收只丟一句 `planning artifact is not accepted: <path>`：
# `assess_planning_artifact` 算出來的 `reasons`（status-not-accepted／
# required-section-missing／blocking-decision）與 `blocking_markers`（行號＋文字）
# 全被布林化丟棄，被拒的 artifact 內容又只活在 planning launcher 的
# `TemporaryDirectory`（`planning_runtime.py` 的 `last.json`），context 一結束就
# 沒了。operator 因此完全看不到 planner 寫了什麼、卡在哪一條驗收條件，只能盲目
# 重試（實測 abandon→重新 claim→同樣失敗，四次全同）。下面補兩件事：
#   A. reasons／markers 進錯誤訊息（單行、有長度上限）
#   B. 完整內容落 `cortex-planning-artifact-rejection/v1` evidence
PLANNING_ARTIFACT_REJECTION_SCHEMA = "cortex-planning-artifact-rejection/v1"
# 目錄與既有的 `planning-recovery`（`cortex-planning-failure/v1`）並列於
# `<coordinator_root>/evidence/` 下，刻意不混進同一個目錄——`work_actions.
# _read_planning_failure_record` 用 `path.parent.name == "planning-recovery"`
# 當作 recover-planning 的收容判準，塞進去會讓它多出一筆「無法解析」的候選、
# 撞上 `planning failure evidence ambiguous` 的 fail-closed。
PLANNING_ARTIFACT_REJECTION_DIRNAME = "planning-artifacts"
# 訊息上限：比照 `manager_daemon.TICK_ERROR_REASON_MAX_LENGTH = 200` 的作法
# （截斷後補 `…`），但這裡放寬到 400——本訊息除了 reason 本身還要塞下 artifact
# 路徑、markers 與 evidence 絕對路徑，200 會把 markers 整段吃掉。上游
# `run_heterogeneous_brainstorm` 對 artifact-write 例外另有 `str(exc)[:160]`
# 的截斷（planning.py），故欄位順序刻意排成 reasons → markers → evidence：
# 最關鍵、最短的 reasons 先寫，即使被上游截斷也還看得到；完整訊息則同時以
# `logger.error` 落 log，evidence 檔本身也在固定目錄用 run_id 可查。
PLANNING_ARTIFACT_REJECTION_MESSAGE_MAX_LENGTH = 400
# 單一 marker 文字的顯示上限：未決問題常是整句 zh-tw 敘述，不設限的話兩三條就
# 把整則訊息吃滿；完整文字一律以 evidence 為準，訊息只負責指路。
_PLANNING_ARTIFACT_REJECTION_MARKER_TEXT_MAX_LENGTH = 48
# 訊息裡最多列幾條 marker（其餘以 `+N` 帶過），理由同上。
_PLANNING_ARTIFACT_REJECTION_MESSAGE_MARKER_LIMIT = 3
# evidence 內容上限（字元數）：planning artifact 是 markdown 文件，正常只有數 KB；
# 64K 足以完整收下真實案例，又能擋住失控輸出把 evidence 目錄灌爆。超過時截斷並
# 標記 `truncated: true` 與原始長度 `content_length`。
PLANNING_ARTIFACT_REJECTION_CONTENT_MAX_CHARS = 64_000


def _single_line(text: str) -> str:
    """把任意文字壓成單行：evidence 的 `reason` 欄位與 `recover-planning` 的
    `failure_reason` 都明確拒收換行（見 `work_actions`／`control.contract` 對
    `"\\n" in failure_reason` 的檢查），故拒收訊息不得帶任何換行或定位字元。"""

    return " ".join(text.split())


def _planning_artifact_rejection_message(
    assessment: ArtifactAssessment, *, evidence_ref: str | None
) -> str:
    """組出單行、有長度上限的拒收訊息（issue #511 A）。

    格式：``planning artifact is not accepted: <path> (reasons=...; markers=Lnn:...; evidence=...)``
    ——`planning artifact is not accepted: ` 前綴與路徑必須保持在最前面，既有
    測試與 operator 的 grep 習慣都以它為錨點。
    """

    details: list[str] = [f"reasons={','.join(assessment.reasons)}"]
    markers = assessment.blocking_markers
    if markers:
        shown = markers[:_PLANNING_ARTIFACT_REJECTION_MESSAGE_MARKER_LIMIT]
        rendered = [
            f"L{marker.line}:"
            + _single_line(marker.text)[:_PLANNING_ARTIFACT_REJECTION_MARKER_TEXT_MAX_LENGTH]
            for marker in shown
        ]
        if len(markers) > len(shown):
            rendered.append(f"+{len(markers) - len(shown)}")
        details.append("markers=" + ", ".join(rendered))
    if evidence_ref is not None:
        details.append(f"evidence={evidence_ref}")
    message = _single_line(
        f"planning artifact is not accepted: {assessment.artifact.ref} ({'; '.join(details)})"
    )
    if len(message) > PLANNING_ARTIFACT_REJECTION_MESSAGE_MAX_LENGTH:
        message = message[:PLANNING_ARTIFACT_REJECTION_MESSAGE_MAX_LENGTH].rstrip() + "…"
    return message


def _write_planning_artifact_rejection_evidence(
    *,
    coordinator_root: Path,
    run_id: str,
    work_id: str,
    assessment: ArtifactAssessment,
) -> str:
    """把被拒 artifact 的完整內容落 evidence（issue #511 B）。

    原子寫入手法與檔名慣例（canonical json hash、`{run_id}-{digest}.json`、
    tmp→fsync→rename→0400、內容衝突 raise）一律比照
    `_write_planning_failure_evidence`／`work_actions._recover_planning_record`，
    不另創風格。
    """

    content = assessment.artifact.text
    truncated = len(content) > PLANNING_ARTIFACT_REJECTION_CONTENT_MAX_CHARS
    body = {
        "schema": PLANNING_ARTIFACT_REJECTION_SCHEMA,
        "run_id": run_id,
        "work_id": work_id,
        "kind": assessment.artifact.kind,
        "path": assessment.artifact.ref,
        "reasons": list(assessment.reasons),
        "markers": [
            {"kind": marker.kind, "line": marker.line, "text": marker.text}
            for marker in assessment.blocking_markers
        ],
        "content": content[:PLANNING_ARTIFACT_REJECTION_CONTENT_MAX_CHARS] if truncated else content,
        "content_length": len(content),
        "truncated": truncated,
        "created_at": _utcnow(),
    }
    digest = verification.canonical_json_hash(body)
    directory = coordinator_root.resolve() / "evidence" / PLANNING_ARTIFACT_REJECTION_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{run_id}-{digest}.json"
    payload = (json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if target.exists():
        if target.is_symlink() or target.read_bytes() != payload:
            raise RuntimeError("planning artifact rejection evidence conflict")
        return str(target)
    temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o400)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


def _record_planning_artifact_rejection_evidence(
    *,
    coordinator_root: Path | None,
    run_id: str,
    work_id: str,
    assessment: ArtifactAssessment,
) -> str | None:
    """呼叫端 wrapper：evidence 記錄本身 fail-open，比照
    `_record_planning_failure_evidence`——記不下診斷不得掩蓋真正的拒收原因，
    否則 operator 拿到的會是 IO 錯誤而不是「artifact 為何不被接受」。
    未帶 `coordinator_root`（既有直呼叫端／測試）時直接不落檔。
    """

    if coordinator_root is None:
        return None
    try:
        return _write_planning_artifact_rejection_evidence(
            coordinator_root=Path(coordinator_root),
            run_id=run_id,
            work_id=work_id,
            assessment=assessment,
        )
    except Exception as exc:  # noqa: BLE001 - evidence 記錄本身 fail-open
        logger.error(
            "planning-artifact-rejection-evidence-write-failed run_id=%s path=%s error=%s: %s",
            run_id,
            assessment.artifact.ref,
            type(exc).__name__,
            str(exc)[:200],
        )
        return None


def planning_kind_bound(kind: object, path_value: object, work_id: object) -> bool:
    """Whether a planning artifact uses its canonical work-item destination.

    The planning runtime always materializes the accepted spec/design/plan
    triplet, even when a combo's manifest omits the optional brainstorming card
    (for example ``fix-standard``).  Keep that runtime contract independent of
    the combo's flattened output list while still binding each kind to its own
    destination family and work item.
    """

    if (
        kind not in {"spec", "design", "plan"}
        or not isinstance(path_value, str)
        or not path_value
        or not isinstance(work_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None
    ):
        return False
    relative = Path(path_value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != path_value
        or len(relative.parts) != 4
    ):
        return False
    if kind in {"spec", "design"}:
        if relative.parts[:3] != ("docs", "superpowers", "specs"):
            return False
        pattern = f"docs/superpowers/specs/*{work_id}*-{kind}.md"
    else:
        if relative.parts[:3] != ("docs", "superpowers", "plans"):
            return False
        pattern = f"docs/superpowers/plans/*{work_id}*.md"
    if relative.suffix != ".md":
        return False
    # The accepted planning contract intentionally permits any basename slug
    # containing the work item.  The directory, four-part relative path and
    # normalized-path guards above keep this basename glob from crossing into
    # another governed root or escaping the workspace.
    return fnmatch.fnmatch(path_value, pattern)


def _publish_planning_artifacts(
    root_value: str,
    rows: object,
    *,
    work_id: str,
    allowed_refs: tuple[str, ...],
    authorities: tuple[PlanningArtifactAuthority, ...] = (),
    transaction: _PlanningPublicationTransaction | None = None,
    coordinator_root: str | Path | None = None,
) -> Callable[[], None]:
    if not isinstance(rows, list):
        raise ValueError("planning artifacts must be a list")
    root = Path(root_value).resolve()
    # #511：拒收 evidence 的 run_id 沿用發佈交易的 run_id（define 路徑一定帶
    # transaction），operator 才能用同一組 run_id 同時撈 planning-recovery 與
    # planning-artifacts 兩份 evidence；沒有交易的直呼叫端沿用下方 ephemeral 交易
    # 的同一個字面值，命名語意一致。
    publication_run_id = transaction.run_id if transaction is not None else "ephemeral"
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
        kind_bound = planning_kind_bound(row.get("kind"), path_value, work_id)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not (docs_bound or openspec_bound)
            or not (manifest_bound or kind_bound)
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
        assessment = assess_planning_artifact(artifact)
        if not assessment.accepted:
            # #511：先落 evidence 再組訊息，好讓訊息帶得上 evidence 路徑。
            evidence_ref = _record_planning_artifact_rejection_evidence(
                coordinator_root=coordinator_root,
                run_id=publication_run_id,
                work_id=work_id,
                assessment=assessment,
            )
            message = _planning_artifact_rejection_message(assessment, evidence_ref=evidence_ref)
            # 上游 `run_heterogeneous_brainstorm` 會把本例外壓成
            # `primary-artifact-write-rejected: ValueError: {str(exc)[:160]}`，
            # evidence 路徑可能被截掉；完整訊息在此另落一筆 log（比照 #391 對
            # needs_human reason 的處理），確保診斷至少有一條完整軌跡。
            logger.error(
                "planning-artifact-rejected run_id=%s work_id=%s %s",
                publication_run_id,
                work_id,
                message,
            )
            raise ValueError(message)
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


_MODEL_CHAIN_CAPABILITY_BY_PERSONA = {
    "planner": "planning",
    "reviewer": "review",
    "builder": "build",
}


def _identity_candidates_for_persona(persona: str, identities: IdentityRegistry, builder_domains: set):
    """依 persona 過濾出合法 candidate 清單（capability + independence domain）。

    #205 的 run-scoped 覆寫用它做約束檢查，與共享 registry 選擇共用同一份過濾
    條件，避免兩處判準漂移。**刻意不含 `primary_domain` 偏好**——那是「同 domain
    優先」的排序偏好，不是合法性限制；拿它驗證覆寫會把偏好升級成硬約束，讓
    operator 明確指定的 identity 被誤判為違規。
    """
    # 非 planner／reviewer 一律視為 builder（比照 #205 之前既有的 else 分支
    # catch-all 行為，deck 目前只會派出 planner/build/reviewer 三種 persona）。
    capability = _MODEL_CHAIN_CAPABILITY_BY_PERSONA.get(persona, "build")
    candidates = [item for item in identities.identities if capability in item.capabilities]
    if persona == "reviewer":
        candidates = [item for item in candidates if item.independence_domain not in builder_domains]
    return candidates


def _measured_profile_partition(
    persona: str, sizing_band, candidates: list
) -> tuple[list, list[tuple[object, str]]]:
    """#452 C：measured 側寫的解析優先序與 capable() 判準 1 過濾。

    - 帶該 persona 實測側寫（accepts_bands source=measured）的身分排前
      （解析優先序：override > measured 側寫 > registry/預設；同層維持既有
      registry 順序，stable partition 不重排）。
    - 實測 accepts_bands 排除本 run 的 sizing_band 的身分被剔除，理由隨
      excluded 回傳給呼叫端（#209 R1：全滅時進 fail-closed 錯誤訊息，部分
      剔除時由呼叫端落 manager log，兩者皆可觀測）。band 未知（planning 尚未
      產出）或封套來自預設（#453 零過濾不變量）時一律不過濾。
    """

    from .model_identities import ENVELOPE_SOURCE_MEASURED, project_envelope

    measured: list = []
    defaults: list = []
    excluded: list[tuple[object, str]] = []
    for identity in candidates:
        projection = project_envelope(identity, persona)
        if projection.source.get("accepts_bands") != ENVELOPE_SOURCE_MEASURED:
            defaults.append(identity)
            continue
        bands = tuple(projection.envelope["accepts_bands"])
        if sizing_band is not None and sizing_band not in bands:
            excluded.append(
                (
                    identity,
                    f"measured accepts_bands={list(bands)} 排除 sizing_band={sizing_band}",
                )
            )
            continue
        measured.append(identity)
    return measured + defaults, excluded


def _workflow_identity_candidates_for_persona(
    run, persona: str, identities: IdentityRegistry
) -> list:
    """依既有規則產生合格 identity 清單，順序即優先序。

    #262：re-route 需要「次佳但合法」的候選，因此把原本只回傳 candidates[0]
    的邏輯抽成清單。過濾條件（persona capability、independence domain）完全
    不變——spec 明列「不改 identity 選擇順序與 independence domain 規則」。

    #205：run-scoped 覆寫先於共享 registry 選擇被檢查；命中時回傳**單元素**
    清單，使 #262 的 preflight re-route 不會把 operator 明確指定的 identity
    換成別的——覆寫的意義就是「用這一個」，被 re-route 掉等同靜默失效。
    #452 C：覆寫優先序高於 measured 側寫，measured band 過濾不套用在覆寫上。
    """

    builder_domains = {
        item.domain
        for item in run.steps
        if item.phase == "build" and item.gate_result == "passed" and item.domain is not None
    }
    # #205 R1/D1/D3/D4：run-scoped 覆寫先於共享 registry 選擇；三段各自獨立，
    # 未覆寫的段落回退既有共享 registry 選擇邏輯（下方 fallback 完全不動）。
    # 覆寫仍須通過與共享路徑相同的 capability／independence domain 約束，違反
    # 時 fail closed 並列出可用 candidates，MUST NOT 靜默退回共享預設。
    override = getattr(run, "model_chain_override", None)
    persona_override = override.get(persona) if isinstance(override, dict) else None
    if persona_override is not None:
        eligible = _identity_candidates_for_persona(persona, identities, builder_domains)
        executor = persona_override.get("executor")
        model_id = persona_override.get("model_id")
        identity = identities.get(executor, model_id)
        available = ", ".join(f"{item.executor}/{item.model_id}" for item in eligible) or "(none)"
        if identity is None:
            raise ValueError(
                "model chain override 指定的 identity 不存在於 registry: "
                f"{persona}={executor}/{model_id}（可用 candidates: {available}）"
            )
        if identity not in eligible:
            if persona == "reviewer" and identity.independence_domain in builder_domains:
                reason = "independence_domain 與 builder 相同"
            else:
                capability = _MODEL_CHAIN_CAPABILITY_BY_PERSONA.get(persona, "build")
                reason = f"不具備 {capability} capability"
            raise ValueError(
                "model chain override 指定的 identity 不符既有約束: "
                f"{persona}={executor}/{model_id}（{reason}；可用 candidates: {available}）"
            )
        return [identity]
    candidates = _identity_candidates_for_persona(persona, identities, builder_domains)
    if persona == "builder" and run.primary_domain is not None:
        # #452 對抗審查修正：primary_domain 是「同 domain 優先」的**排序偏好**，
        # 不是合法性限制（見 _identity_candidates_for_persona docstring）。舊實
        # 作「preferred 非空即整組收窄」在 packaged roster 只有 agy 時無害，但
        # roster 擴充 build 候選（#456 R3）後，僅為候選宣告的 packaged 身分會把
        # host overlay 的可跑 builder 整個擠出候選清單，#262 preflight re-route
        # 因此失去 fallback——packaged 登錄不隱含本機可用（比照 doctor #456 R6
        # 的候選宣告語意）。改為 preferred 排前、其餘保留在後：首選不變，
        # fallback 不丟。
        preferred = [item for item in candidates if item.independence_domain == run.primary_domain]
        if preferred:
            rest = [
                item for item in candidates if item.independence_domain != run.primary_domain
            ]
            candidates = preferred + rest
    if not candidates:
        raise ValueError(f"no configured identity for workflow persona: {persona}")
    # #452 C：measured 側寫優先＋band 過濾（三段 persona 之外的 catch-all
    # persona 沒有封套語意，維持原清單）。
    from .model_identities import DEFAULT_ENVELOPE

    if persona in DEFAULT_ENVELOPE:
        ordered, excluded = _measured_profile_partition(
            persona, getattr(run, "sizing_band", None), candidates
        )
        if excluded and ordered:
            # #452 對抗審查修正（#209 R1）：部分剔除（仍有存活候選）時排除理由
            # 也要可觀測——落 manager log，不再靜默丟棄；全滅時走下方
            # fail-closed 錯誤訊息。
            logger.info(
                "workflow run=%s persona=%s measured 側寫剔除候選：%s（存活候選 %d）",
                getattr(run, "run_id", None),
                persona,
                "; ".join(
                    f"{identity.executor}/{identity.model_id}: {reason}"
                    for identity, reason in excluded
                ),
                len(ordered),
            )
        if not ordered:
            detail = "; ".join(
                f"{identity.executor}/{identity.model_id}: {reason}"
                for identity, reason in excluded
            )
            raise ValueError(
                f"no capable identity for workflow persona: {persona}（{detail}）"
            )
        candidates = ordered
    return _rank_candidates_by_resolution_layer(run, persona, candidates, identities)


def _rank_candidates_by_resolution_layer(
    run, persona: str, candidates: list, identities: IdentityRegistry
) -> list:
    """#534：把候選清單重排成三層解析鏈的順序，並套用 packaged fallback 政策。

    層級是排序主鍵、**stable**：同層內完全維持上游既有順序，因此 #452 的
    measured 側寫優先與 #262 的 primary_domain 偏好都原封不動地降級為同層內的
    次要偏好。這正是本 issue 的核心修正——packaged roster 的內建列序（「agy 維持
    首位」）不再有機會壓過 operator 在 host overlay 的人工指定。
    """

    role = model_resolution.role_for_persona(persona)
    ranked = model_resolution.rank_candidates(
        candidates, role=role, context=identities.resolution_context
    )
    for warning in ranked.warnings:
        logger.warning(
            "workflow run=%s persona=%s %s", getattr(run, "run_id", None), persona, warning
        )
    if ranked.excluded:
        logger.info(
            "workflow run=%s persona=%s 解析層剔除候選：%s（存活候選 %d）",
            getattr(run, "run_id", None),
            persona,
            ranked.exclusion_detail(),
            len(ranked.ordered),
        )
    if not ranked.ordered:
        raise ValueError(
            f"no resolvable identity for workflow persona: {persona}"
            f"（{ranked.exclusion_detail()}）——第 1 層請於 host overlay 宣告身分，"
            "第 2 層請以 patchmud 評估合格並人工複核後加入 model-eval-roster.yaml"
        )
    return list(ranked.ordered)


def _resolution_layer_for(
    identity, persona: str, identities: IdentityRegistry | None = None
) -> str:
    """本次解析結果落在哪一層（provenance 用；被 park 的身分不會走到這裡）。

    `identities` 缺席（舊呼叫端／測試替身）時退回空的評估清單：overlay 來源
    仍記第 1 層，packaged 來源記第 3 層——保守方向，不會把未評估的身分誤標成
    已評估合格。
    """

    context = (
        identities.resolution_context
        if identities is not None
        else model_resolution.DEFAULT_CONTEXT
    )
    layer = model_resolution.identity_layer(
        identity,
        role=model_resolution.role_for_persona(persona),
        eval_roster=context.eval_roster,
    )
    return layer or model_resolution.RESOLUTION_LAYER_PACKAGED


def _workflow_identity_candidates(run, step, identities: IdentityRegistry) -> list:
    return _workflow_identity_candidates_for_persona(run, step.persona, identities)


def _select_workflow_identity(run, step, identities: IdentityRegistry):
    return _workflow_identity_candidates(run, step, identities)[0]


def _specialize_workflow_launcher(launcher, step):
    """依 persona／commit policy 套用 launcher 的執行契約。

    抽成函式是為了讓 #262 的 preflight 能取得「與正式 job 完全相同」的
    launcher（進而是相同的 PATH／HOME／sandbox policy）。若 preflight 用未
    specialize 的 launcher，reviewer 的最小 env 就不會被檢查到——那正是
    design D2 說的安慰劑。

    **#716（選項 F）：`commit_policy` 從這裡開始也決定 sandbox mode。** 在此之前它
    只在 `required` 那一支被消費（`as_commit_required()`），`forbidden` 完全是 prompt
    契約——於是一張 `commit_policy=forbidden` 且 `declared_outputs` 為空的唯讀 build
    卡照樣拿到 `--sandbox workspace-write`。判準由
    `registry.card_contract_forbids_workspace_write()` 機械算出，**不由 persona 一刀
    切**：builder persona 底下同時有唯讀卡與寫入卡。
    """

    # lazy import 的理由與 `launcher._codex_inner_sandbox_argv()`／`planning_job` 逐字
    # 相同：`trust_root` 是產生器面，不該進 `coordinator` 的模組載入圖，但**規則的內容
    # 必須只有一份**。
    from ..trust_root import registry as trust_registry

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
    elif trust_registry.card_contract_forbids_workspace_write(
        commit_policy=effective_commit_policy,
        # `getattr` 而不是 `step.outputs`：本函式也被 preflight 那條路以更窄的 step
        # 形狀呼叫。缺欄回 `None` ⇒ 判準回 `False` ⇒ **維持寫入卡契約**
        # （`BUILDER_WORKSPACE_WRITE`；argv 上發什麼 mode 由
        # `registry.SANDBOX_MODE_DERIVATION` 那一列決定，#716 B 後半起是
        # `danger-full-access`），與「契約缺欄不猜」逐字一致（真實 `WorkflowStep`
        # 恆有這一欄）。
        declared_outputs=getattr(step, "outputs", None),
    ):
        # #716：**兩個條件都要明確成立才降**（`commit_policy=forbidden` **且**
        # `declared_outputs` 為空），其餘一律維持寫入卡契約——判準本體與
        # 「解不出來就不猜」的理由都住在那支函式與 `registry.SANDBOX_MODE_DERIVATION`。
        #
        # **capability 缺席時保持寫入卡契約，不是 fail-open**：那正是
        # 今天的行為，而本票的保守方向逐字就是「不確定 ⇒ 維持寫入卡契約」。
        # 真實 launcher 一定有這支（`SubprocessLauncher.as_write_forbidden`），
        # 由 `tests/test_card_contract_sandbox_mode_716.py` 釘住。
        write_forbidden_factory = getattr(launcher, "as_write_forbidden", None)
        if callable(write_forbidden_factory):
            launcher = write_forbidden_factory()
    return launcher


# #369：provider capability 探測的死碼修復。修復前 `_runtime_preflight_gate`
# 呼叫 `evaluate_dispatch_gate` 時從未傳入 `snapshot_lookup`／`provider_prober`
# （兩者預設 None）——`_resolve_provider_freshness` 在 `snapshot_lookup is None`
# 時直接回 STALE_SNAPSHOT，而 STALE_SNAPSHOT 不在 `_BLOCKING_OUTCOMES`（只有
# CAPABILITY_MISSING／PROVIDER_UNAVAILABLE 會擋），所以任何 `provider:` 宣告
# 在生產環境永遠放行，等於整條路徑是死碼。以下兩個 factory 把它接上真正的
# 資料源：GitHub 走既有 monitor durable snapshot（唯讀，無快照時安全回退成
# STALE_SNAPSHOT，不變更行為的保守面）；executor 走 dispatch-time 的登入態
# 探測（`coordinator.executor_auth`），以 process-level 快取避免每次 dispatch
# 都重新 spawn CLI 子行程（見 `_EXECUTOR_AUTH_CACHE`）。
#
# #442：`provider:executor` sentinel 已（小範圍）接上 cards——openspec-archive
# ／policy-commit 兩張 ship-phase 卡宣告了它（與 #369 先行接上的
# `provider:github:<repo>` 併存），啟用前已在部署環境驗證 claude／codex／
# copilot CLI 皆在場且登入態探測可用（見 #442 PR 的驗證紀錄）。其餘卡仍
# 維持 hold：待 ship-phase 觀測無誤後再擴大，避免 dispatch 熱路徑對更多
# combo 意外 spawn 探測子行程。探測成本由 `_EXECUTOR_AUTH_CACHE`（process-
# level、TTL 900s）與 ProbeBudget 上限共同約束。
_EXECUTOR_AUTH_CACHE: dict[str, object] = {}


def _monitor_provider_snapshot_lookup(provider_id: str, *, snapshot_store) -> object | None:
    from paulsha_cortex.monitor.work_models import parse_timestamp

    from .runtime_preflight import DEFAULT_PROVIDER_TTL_SECONDS, ProviderFreshness

    try:
        snapshot = snapshot_store.load()
    except Exception:  # noqa: BLE001 - monitor 快照壞掉不得拖垮 dispatch
        return None
    if snapshot is None:
        return None
    provider = snapshot.providers.get(provider_id)
    if provider is None:
        return None
    try:
        observed_at = parse_timestamp(provider.last_attempt_at).timestamp()
    except ValueError:
        return None
    reason = "; ".join(provider.diagnostics) if provider.diagnostics else None
    return ProviderFreshness(
        provider_id=provider.provider_id,
        status=provider.status,
        observed_at=observed_at,
        ttl_seconds=DEFAULT_PROVIDER_TTL_SECONDS,
        source="monitor-snapshot",
        reason=reason,
    )


def _executor_auth_snapshot_lookup(provider_id: str) -> object | None:
    """#369：executor 登入態的「快照」層——實際是 process-level 快取。

    沒有既有的 durable executor-auth snapshot 基礎設施（GitHub 有，monitor
    daemon 會定期掃描並落盤；executor CLI 登入態沒有），因此第一次查詢時合成
    一筆「已過期」的紀錄，讓 `_resolve_provider_freshness` 的 stale 分支去
    呼叫 `provider_prober` 做一次真正探測；探測結果回寫這個 cache，後續在
    TTL 內的查詢會直接命中 `is_fresh()`、不再重新 spawn 子行程。
    """

    from .runtime_preflight import ProviderFreshness

    cached = _EXECUTOR_AUTH_CACHE.get(provider_id)
    if cached is not None:
        return cached
    from .executor_auth import EXECUTOR_AUTH_TTL_SECONDS

    return ProviderFreshness(
        provider_id=provider_id,
        status="degraded",
        observed_at=0.0,
        ttl_seconds=EXECUTOR_AUTH_TTL_SECONDS,
        source="cold-start",
        reason="no prior executor auth probe",
    )


def _executor_auth_prober(provider_id: str) -> object | None:
    from .executor_auth import check_executor_auth

    result = check_executor_auth(provider_id)
    _EXECUTOR_AUTH_CACHE[provider_id] = result
    return result


def _combined_provider_snapshot_lookup(*, snapshot_store) -> Callable[[str], object | None]:
    from .executor_auth import EXECUTOR_CANDIDATES

    def _lookup(provider_id: str) -> object | None:
        if provider_id in EXECUTOR_CANDIDATES:
            return _executor_auth_snapshot_lookup(provider_id)
        return _monitor_provider_snapshot_lookup(provider_id, snapshot_store=snapshot_store)

    return _lookup


def _combined_provider_prober(provider_id: str) -> object | None:
    from .executor_auth import EXECUTOR_CANDIDATES

    if provider_id in EXECUTOR_CANDIDATES:
        return _executor_auth_prober(provider_id)
    # GitHub 目前只用 monitor snapshot，不做額外 live probe：monitor daemon
    # 本身已定期刷新該快照，再疊一層 live probe 只是重複付網路成本
    # （#369 範圍界定，見 changelog）。
    return None


def _runtime_preflight_gate(
    run, step, *, identities: IdentityRegistry, launcher_factory, snapshot_store=None
):
    """#262：dispatch 前的 runtime capability／provider 新鮮度 gate。

    回傳 None 代表這張 card 未宣告任何 capability，呼叫端照原路徑走；否則回傳
    `DispatchGateDecision`，其中 `launcher` 只在通過 preflight 的 identity 上建立
    ——被擋下的 identity 不會產生任何 model session。

    `snapshot_store` 預設 None 時延後到真正需要時才建立
    `monitor.work_snapshot.WorkSnapshotStore()`（讀既有 monitor durable
    snapshot）；測試可注入指向 tmp_path 的 store，不觸碰真實安裝路徑。
    """

    from .runtime_preflight import card_runtime_requirements, evaluate_dispatch_gate

    try:
        requirements = card_runtime_requirements(step.card)
    except Exception:  # noqa: BLE001 - deck 載入問題不得把 dispatch 一起拖垮
        return None
    if not requirements:
        return None

    candidates = _workflow_identity_candidates(run, step, identities)

    # 每個 identity 只 specialize 一次並記憶：preflight 與最終 dispatch 共用同一
    # 個 launcher 實例，因此（a）檢查的環境就是 job 的環境，（b）通過的 identity
    # 不會被重複套用 as_commit_required／as_review_only 等契約工廠。
    specialized: dict[int, object] = {}

    def _launcher_for(identity):
        key = id(identity)
        if key not in specialized:
            specialized[key] = _specialize_workflow_launcher(launcher_factory(identity), step)
            if identities.resolution is not None:
                model_resolution.validate_identity_compatibility(
                    step.persona, identity, launcher=specialized[key]
                )
        return specialized[key]

    def _environment_for(identity):
        launcher = _launcher_for(identity)
        describe = getattr(launcher, "executor_environment", None)
        if callable(describe):
            return describe()
        # launcher 未描述自身環境時退回 host 描述：仍會檢查，只是無法保證與正式
        # job 完全一致；這是 fail-soft，不比 #262 之前更差。
        from .runtime_preflight import host_environment

        return host_environment()

    active_store = snapshot_store
    if active_store is None:
        from paulsha_cortex.monitor.work_snapshot import WorkSnapshotStore

        active_store = WorkSnapshotStore()

    return evaluate_dispatch_gate(
        card=step.card,
        requirements=requirements,
        candidates=candidates,
        environment_for=_environment_for,
        launcher_factory=_launcher_for,
        snapshot_lookup=_combined_provider_snapshot_lookup(snapshot_store=active_store),
        provider_prober=_combined_provider_prober,
    )


def _record_resolved_model_chain(
    registry, run, step, identity, identities: IdentityRegistry | None = None
) -> None:
    """#205 R4/D5：把本次 dispatch 實際解析到的 executor/model/domain 與來源
    寫入 run，供事後稽核。純 provenance 寫入，逐段覆蓋合併既有紀錄，不影響
    既有 workflow 語意或推進邏輯。

    #534：``source`` 改記**解析層**——``run-override``（run-scoped 覆寫）／
    ``operator-overlay``／``evaluated-roster``／``packaged-fallback``。舊值
    ``default-envelope``／``patchmud-profile`` 只說得出「封套來自實測或預設」，
    說不出「這顆模型憑什麼進熱路徑」，operator 看到 `source: default-envelope`
    根本無從得知跑的是未經核可的 packaged 候選。封套來源改記在獨立的
    ``envelope_source`` 欄位，資訊不減。
    """
    from .model_identities import (
        DEFAULT_ENVELOPE,
        ENVELOPE_SOURCE_DEFAULT,
        ENVELOPE_SOURCE_MEASURED,
        project_envelope,
    )

    override = getattr(run, "model_chain_override", None)
    if isinstance(override, dict) and step.persona in override:
        source = "run-override"
    else:
        source = _resolution_layer_for(identity, step.persona, identities)
    envelope_source = ENVELOPE_SOURCE_DEFAULT
    if step.persona in DEFAULT_ENVELOPE:
        projection = project_envelope(identity, step.persona)
        if ENVELOPE_SOURCE_MEASURED in projection.source.values():
            envelope_source = ENVELOPE_SOURCE_MEASURED
    resolved = dict(getattr(run, "resolved_model_chain", None) or {})
    resolved[step.persona] = {
        "executor": identity.executor,
        "model_id": identity.model_id,
        "independence_domain": identity.independence_domain,
        "source": source,
        "envelope_source": envelope_source,
    }
    registry._manager_update_workflow_run(run.run_id, resolved_model_chain=resolved)


_LEGACY_CARD_EXECUTION = {
    "worktree-isolation": (
        "superpowers:using-git-worktrees",
        "Confirm the Manager-provisioned worktree; do not create a second worktree.",
        "forbidden",
        "none",
    ),
    "tdd-red": (
        "superpowers:test-driven-development",
        "Use the accepted plan to add and commit a reproducible RED regression test. "
        "In pinned planning files (tasks.md / todo.md) you may ONLY toggle checkbox "
        "state; never edit, rephrase, or annotate their text — any wording change "
        "is planning-input drift and fails the run.",
        "required",
        "red-required",
    ),
    "subagent-build": (
        "superpowers:subagent-driven-development",
        "Implement the accepted plan with the minimum diff and commit a tested candidate HEAD. "
        "In pinned planning files (tasks.md / todo.md) you may ONLY toggle checkbox "
        "state; never edit, rephrase, or annotate their text — any wording change "
        "is planning-input drift and fails the run.",
        "required",
        "focused",
    ),
}


def _repair_findings_prompt_suffix(run, *, coordinator_root: str | Path) -> str:
    journal_path = Path(coordinator_root) / "delivery-journal.json"
    if journal_path.is_symlink() or not journal_path.is_file():
        return ""
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    runs = journal.get("runs") if isinstance(journal, dict) else None
    row = runs.get(run.run_id) if isinstance(runs, dict) else None
    ship = row.get("ship") if isinstance(row, dict) else None
    findings = ship.get("findings") if isinstance(ship, dict) else None
    if not (
        isinstance(ship, dict)
        and ship.get("phase") == "needs-fix"
        and isinstance(findings, list)
        and findings
    ):
        return ""
    compact: list[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        path = finding.get("path")
        line = finding.get("line")
        body = finding.get("body")
        if path is not None and not isinstance(path, str):
            continue
        if line is not None and (not isinstance(line, int) or isinstance(line, bool)):
            continue
        if not isinstance(body, str) or not body:
            continue
        compact.append(f"[{path or '?'}:{line if line is not None else '?'}] {body}")
    if not compact:
        return ""
    suffix = " Reviewer findings to fix in this repair (address each, then commit): " + "; ".join(
        compact
    )
    return suffix[:2000]


# #606：重派 prompt 內 retry-context 的截斷上限。retry-context 的內容全部來自
# manager 自產證據（gate ledger 的 detail 是 gate 命令的 stderr/stdout 尾段，
# 見 `gate_ledger.run_gates`），長度不受控——一個失敗的全套 pytest 可以吐出數萬
# 字。沒有上限的話「附上證據」會直接把 dispatch prompt 撐爆，反而讓重派更糟。
RETRY_CONTEXT_EVIDENCE_LIMIT = 2000
RETRY_CONTEXT_MESSAGE_LIMIT = 600


def _retry_context_error_row(exc: BaseException) -> dict[str, object]:
    """把採信錯誤壓成 prompt 可帶的機械欄位（類別＋canonical 訊息＋reason）。

    刻意只取例外物件自己的欄位：``TerminalContractError``／
    ``GateContradictionError`` 的訊息是 manager 側判準產生的 canonical 文字
    （見 :mod:`.terminal_contract`），不是模型講的話。
    """

    message = str(exc)
    row: dict[str, object] = {
        "error_class": type(exc).__name__,
        "message": message[:RETRY_CONTEXT_MESSAGE_LIMIT],
    }
    if len(message) > RETRY_CONTEXT_MESSAGE_LIMIT:
        row["message_truncated"] = True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        row["reason"] = reason
    validation_path = getattr(exc, "validation_path", None)
    if isinstance(validation_path, str) and validation_path:
        row["validation_path"] = validation_path
    return row


def _prior_card_acceptance_error(
    job: Mapping[str, object], *, registry=None
) -> dict[str, object] | None:
    """重新導出「上一次這張卡為什麼沒被採信」的 canonical 錯誤。

    不讀任何既存的敘事欄位（``run.needs_human_reason`` 在 ``retry-card`` 重置時
    依診斷 invariant 已被清空，見 ``registry._manager_reset_workflow_for_retry_card``），
    而是對舊 job 重跑既有採信路徑的前兩段——同一份 log、同一份 gate ledger、
    同一組判準函式，因此拿到的錯誤與當初 harvest 擲出的逐字相同：

    - log 裡沒有可用的 terminal JSON（#569 現場）→ :func:`_extract_terminal_json`
      的 ``ValueError`` 文字；
    - 有 envelope 但與 manager 自產 gate ledger 矛盾（#606 現場）→
      :func:`_assert_terminal_gate_consistency` 擲出的
      ``GateContradictionError``／``TerminalContractError``。

    取不到就回 ``None``——證據是加值，不得讓「讀不到舊證據」害死一次合法重派。
    """

    log_path = job.get("log_path")
    if not isinstance(log_path, str) or not log_path:
        return None
    try:
        raw = _extract_terminal_json(log_path)
    except ValueError as exc:
        return _retry_context_error_row(exc)
    except Exception:  # pragma: no cover - fail-soft：prompt 組裝不得炸掉 dispatch
        return None
    try:
        _assert_terminal_gate_consistency(raw, job=job, registry=registry)
    except terminal_contract.TerminalContractError as exc:
        return _retry_context_error_row(exc)
    except Exception:  # pragma: no cover - 同上
        return None
    return None


def _prior_card_failed_gates(job: Mapping[str, object]) -> list[dict[str, object]]:
    """從舊 job 的 gate ledger 機械讀出 failed gate（名稱＋exit code＋截尾輸出）。

    「哪些算 failed」刻意複用 :func:`terminal_contract._ledger_outcomes`——採信端
    判定矛盾用的就是它（exit_code 非 0 覆寫自述 status），retry-context 不得另立
    第二份判準，否則 prompt 會告訴 builder 一組跟採信不同的失敗集合。

    ``detail`` 依 :data:`RETRY_CONTEXT_EVIDENCE_LIMIT` 做**全體**預算截斷（保留
    尾段，與 ``gate_ledger.run_gates`` 自己的 ``[-2000:]`` 同向：pytest 的 short
    summary 在尾巴），被截的項目帶 ``detail_truncated`` 明示，不假裝完整。
    """

    log_path = job.get("log_path")
    if not isinstance(log_path, str) or not log_path:
        return []
    try:
        found = terminal_contract.read_gate_ledger(
            terminal_contract.gate_ledger_path(_job_control_log_path(job, log_path))
        )
        if found is None:
            return []
        outcomes = terminal_contract._ledger_outcomes(found[0])
    except Exception:
        # ledger 缺席／壞掉／是 symlink：沒有可附的獨立證據，回空集合即可。
        return []
    rows: list[dict[str, object]] = []
    budget = RETRY_CONTEXT_EVIDENCE_LIMIT
    for name, outcome in outcomes.items():
        if outcome.get("status") == "passed":
            continue
        row: dict[str, object] = {"name": name, "status": "failed"}
        exit_code = outcome.get("exit_code")
        if type(exit_code) is int:
            row["exit_code"] = exit_code
        detail = outcome.get("detail")
        if isinstance(detail, str) and detail:
            kept = detail[-budget:] if budget > 0 else ""
            budget -= len(kept)
            row["detail"] = kept
            if len(kept) < len(detail):
                row["detail_truncated"] = True
        rows.append(row)
    return rows


def _prior_review_rejection(run, registry) -> dict[str, object] | None:
    """#750：repair 回合的跨卡回饋——本 run 最近一顆 verify／review 非通過 terminal。

    #606 的 retry_context 只看**同一張卡**的前次 job；把 candidate 打回來的那份
    verification 判定在另一張卡上、且因 harvest fail-closed（verify terminal 只認
    verified/passed）沒有綁進 run——`retry-build` 的 repair 文案要求 builder
    「fix only real Candidate failures identified by the current verification/review
    evidence」，卻沒有任何通道把那份 evidence 交到它手上，盲修不收斂是確定性的
    （實機：verification-22 failed → repair -23 加了測試 → verification-24 同因再
    failed）。這裡經 harvest 同一支 :func:`_extract_terminal_json` 讀回判定，
    **誠實標注 `source: "reviewer-terminal"`**——它是 reviewer 產物，不是
    manager-independent；它只進 prompt 供 repair 消費，不進任何採信路徑。
    取不到一律回 ``None``（證據是加值，不得害死一次合法重派）。
    """

    if registry is None:
        return None
    try:
        jobs = registry.list_jobs()
    except Exception:
        return None
    rejected: tuple[Mapping[str, object], Mapping[str, object]] | None = None
    for job in jobs:
        if job.get("workflow_run_id") != getattr(run, "run_id", None):
            continue
        if job.get("workflow_phase") not in {"verify", "review"}:
            continue
        if job.get("status") != "exited":
            continue
        log_path = job.get("log_path")
        if not isinstance(log_path, str) or not log_path:
            continue
        try:
            raw = _extract_terminal_json(log_path)
        except Exception:
            continue
        if raw.get("status") not in terminal_contract.NON_PASSING_STATUSES:
            continue
        rejected = (job, raw)
    if rejected is None:
        return None
    job, raw = rejected
    details = raw.get("details") if isinstance(raw.get("details"), Mapping) else {}
    budget = RETRY_CONTEXT_EVIDENCE_LIMIT
    context: dict[str, object] = {
        "source": "reviewer-terminal",
        "phase": str(job.get("workflow_phase")),
        "job_id": str(job.get("job_id")),
        "status": str(raw.get("status")),
        "summary": str(raw.get("summary") or "")[:budget],
    }
    findings = details.get("findings")
    if isinstance(findings, list) and findings:
        rows: list[str] = []
        remaining = budget
        for item in findings[:8]:
            text = (
                json.dumps(item, ensure_ascii=False)
                if isinstance(item, (dict, list))
                else str(item)
            )
            kept = text[: max(0, remaining)]
            if not kept:
                break
            remaining -= len(kept)
            rows.append(kept)
        if rows:
            context["findings"] = rows
    conformance = details.get("conformance")
    if isinstance(conformance, Mapping) and conformance:
        context["conformance"] = {
            str(key): str(value)[:300]
            for key, value in list(conformance.items())[:12]
        }
    return context


def _operator_adjudications(
    run, coordinator_root: str | Path | None
) -> list[dict[str, object]] | None:
    """#752：本 run 的 operator 裁決紀錄（`cortex-operator-adjudication/v1`）。

    verify 階段的人裁通道：design/todo 矛盾這類「reviewer 只能 needs_human」的判定，
    operator 經 `retry-card --reason` 落成 Manager-owned immutable evidence，這裡讀回
    最近 ≤3 筆（reason 有界）進 retry_context——builder 與 reviewer 卡都吃。內容經
    bounded CLI＋Manager 落地，不是 builder 可偽造的 candidate 內容（#540／#628 的
    作者歸屬不變）。取不到一律回 None。
    """

    if coordinator_root is None:
        return None
    root = Path(coordinator_root) / "evidence" / "operator-adjudication"
    try:
        # 檔名是 content-addressed（run_id-digest），不含時序——以 mtime 排序。
        entries = sorted(
            root.glob(f"{run.run_id}-*.json"), key=lambda item: item.stat().st_mtime
        )
    except Exception:
        return None
    rows: list[dict[str, object]] = []
    for path in entries:
        if path.is_symlink():
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(body, Mapping) or body.get("run_id") != run.run_id:
            continue
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            continue
        rows.append(
            {
                "source": "operator-adjudication",
                "actor": str(body.get("actor") or "operator"),
                "card": str(body.get("card") or ""),
                "created_at": str(body.get("created_at") or ""),
                "reason": reason[:RETRY_CONTEXT_EVIDENCE_LIMIT],
            }
        )
    if not rows:
        return None
    return rows[-3:]


def _workflow_retry_context(
    prior_jobs: Sequence[Mapping[str, object]],
    *,
    registry=None,
    review_rejection: Mapping[str, object] | None = None,
    operator_adjudications: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object] | None:
    """#606：重派這張卡時要機械附進 prompt 的「前次採信失敗證據」。

    現場（run ``workflow-7812abefede9d9b5d601`` 的 subagent-build，job 492／493）：
    builder 兩次自稱 ``pytest: passed``，manager ledger 兩次獨立重跑抓到**同一個**
    失敗，``GateContradictionError`` 逐字相同。``retry-card``（#545／#569）重派用
    的是原卡 prompt——契約不可竄改，這是對的——但 prompt 裡沒有任何通道讓 builder
    知道「上次為什麼被拒」，於是無回饋的重試就是決定論的重複，只是燒 job。

    ``prior_jobs`` 就是 :func:`_dispatch_workflow_card` 算出的 ``matching``（同一
    張卡、同一個 phase，verify／review 另以 candidate 定錨），因此：

    - **首派必然是空集合 → 回 ``None`` → prompt 逐字不變**（#606 要求 2，有測試釘住）；
    - 內容全部來自 manager 自產證據（自己的 gate ledger、自己的採信判準），一個
      字都不取自模型輸出，與 #540 的不可竄改性一致。

    ``attempt``／``redispatch_count`` 由這張卡已燒掉的 job 數機械導出，是 #555
    （per-card 熔斷）要的計數鉤子：本 PR 不實作熔斷，只把計數落到 prompt 與
    retry-context 上，讓熔斷判準之後有一個既有的、機械的來源可接。
    """

    if not prior_jobs:
        # #750：理論上 rejection 只出現在 repair 回合（該卡必有前次 job）；防禦性
        # 地維持「首派 prompt 逐字不變」的 #606 要求。
        return None
    latest = prior_jobs[-1]
    context: dict[str, object] = {
        # 本次是這張卡的第 N 次派工；首派為 1，故 retry-context 恆有 attempt >= 2。
        "attempt": len(prior_jobs) + 1,
        # #555 的鉤子：已重派過幾次（首次重派為 1）。
        "redispatch_count": len(prior_jobs),
        "previous_job_id": str(latest.get("job_id")),
        "previous_job_ids": [str(job.get("job_id")) for job in prior_jobs],
        "evidence_source": "manager-independent",
        "evidence_char_limit": RETRY_CONTEXT_EVIDENCE_LIMIT,
        "failed_gates": _prior_card_failed_gates(latest),
    }
    error = _prior_card_acceptance_error(latest, registry=registry)
    if error is not None:
        context["acceptance_error"] = error
    if review_rejection is not None:
        # #750：repair 回合的跨卡回饋。鍵名明示它是「打回 candidate 的那份判定」。
        context["review_rejection"] = dict(review_rejection)
    if operator_adjudications:
        # #752：operator 的人裁紀錄——needs_human 判定的權威答覆，優先於文件間的
        # 表面矛盾（例：design 與 todo 不一致時，以裁決指定的那一邊為準）。
        context["operator_adjudications"] = [dict(row) for row in operator_adjudications]
    return context


def _workflow_job_prompt(
    run,
    step,
    *,
    builder_job_id: str | None,
    coordinator_root: str | Path,
    input_snapshot: tuple[dict[str, str], ...] = (),
    candidate_checkout: str | None = None,
    env: Mapping[str, str] | None = None,
    retry_context: Mapping[str, object] | None = None,
    operator_adjudications: Sequence[Mapping[str, object]] | None = None,
) -> str:
    """組出單張 workflow card 的派工 prompt。

    ``env``（#540）：canonical gate 名稱由這份 env 的 ``PSC_GATE_CMD_*`` 宣告
    機械導出（預設 ``os.environ``——與 launcher 交給 wrapper 的 env 同源，見
    ``launcher._git_scope_env``），因此 prompt 告訴模型的 gate 名稱集合，與
    job 結束後 manager 自己寫出的 ledger 必為同一組。

    ``retry_context``（#606）：由 :func:`_workflow_retry_context` 從**這張卡的
    前次 job** 機械導出的採信失敗證據。``None``（首派、或呼叫端未提供）時
    prompt 逐字不變——重派回饋只加在真的有前次失敗的那條路徑上。
    """
    fallback = _LEGACY_CARD_EXECUTION.get(step.card, (None, None, None, None))
    effective_test_policy = step.test_policy or fallback[3]
    # #540：與 launcher 交給 gate ledger writer 的是同一份 env，因此這裡導出的
    # 名稱就是 ledger 之後會有的名稱。
    #
    # #721：**適用範圍**同樣機械導出，不在 prompt 端另寫一份。契約不要求模型交出
    # gate 結果的卡（`test_policy` 為 `None`／`"none"`）拿到空集合，dispatch 端與
    # harvest 端因此對「這張卡要模型驗哪些 gate」給出同一個答案——判準是
    # `terminal_contract.expected_gate_names_for_test_policy`（經
    # `gate_ledger.card_requires_gate_evidence` 轉呼叫），與 harvest 端
    # `_assert_terminal_gate_consistency` 用的是同一支函式。
    card_requires_gate_evidence = gate_ledger.card_requires_gate_evidence(
        effective_test_policy
    )
    card_gate_names = gate_ledger.card_gate_names(env, test_policy=effective_test_policy)
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
            "fixed": {"schema_version": 1, "kind": "workflow-verification-result"},
            # #261 R1：成功、失敗與需人工介入三者對等可達。gate 失敗時必須誠實回
            # failed／needs_human，不得為了讓 card 收斂而輸出 verified。
            "status": ["verified", "failed", "needs_human"],
            "status_policy": (
                "Report verified only when every deterministic gate you ran actually passed. "
                "If any gate failed, report failed; if the decision needs a human, report "
                "needs_human. A non-passing terminal is a valid, expected outcome."
            ),
            "reports": [{"path": "concrete repo-relative path matching declared_outputs", "body": "Markdown body without frontmatter"}],
        }
    elif step.phase == "review":
        authority_hashes_expected = {
            row["path"]: row["sha256"]
            for row in input_snapshot
            if row.get("authority") == "planning-authority"
        }
        terminal_schema = {
            "kind": "workflow-review-result",
            "schema_version": 1,
            "required": [
                "schema_version", "kind", "reason", "findings", "reports",
                *(["authority_hashes"] if authority_hashes_expected else []),
            ],
            "fixed": {
                "schema_version": 1,
                "kind": "workflow-review-result",
                # #315 補遺 2：sonnet reviewer 對「actually opened」措辭的條件性
                # 解讀會整組省略 authority_hashes（實測 2/2）。expected 值由
                # manager 原樣提供，列入 fixed 要求逐字照抄——照抄本身即攻證
                # 「收到的 frozen authority 與 pinned hash 一致」；harvest 端
                # 的精確比對不變。
                **(
                    {"authority_hashes": dict(authority_hashes_expected)}
                    if authority_hashes_expected
                    else {}
                ),
            },
            # #261 R1：選填欄位；review verdict 仍由 findings 決定，status 只用來
            # 誠實表達「這張 review card 自己沒能完成」。
            "optional": ["status"],
            "status": ["passed", "failed", "needs_human"],
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
        if authority_hashes_expected:
            terminal_schema["authority_hashes"] = {
                "description": (
                    "MANDATORY: copy the expected mapping below verbatim into the "
                    "authority_hashes field of your terminal JSON. It attests the frozen "
                    "planning authority you received; the verdict is rejected if the field "
                    "is missing or differs in any way."
                ),
                "expected": dict(authority_hashes_expected),
            }
    else:
        fixed_terminal_fields: dict[str, object] = {
            "schema_version": terminal_contract.TERMINAL_SCHEMA_VERSION,
            "kind": "workflow-card",
            "run_id": run.run_id,
            "card_id": step.card,
        }
        if not step.outputs:
            fixed_terminal_fields["outputs"] = []
        # #721：兩段文字的**適用範圍**由 `card_gate_names`（＝上面那支機械導出）決定。
        # 契約要求模型交出 gate 結果時逐字沿用 #261／#540／#606 的既有文字；不要求時
        # 連泛用前言都不能沿用——它逐字點名 pytest（"every deterministic gate you ran
        # (OpenSpec / pytest / policy)"）並宣告「Manager 會重讀 ledger 判你的 passed」，
        # 讀起來就是「去跑 pytest」，而那正是 job wf-6c37c77ca1-worktree-isolation-8
        # 在 `-s read-only` 沙箱下撞死、Manager 自動重派、形成確定性迴圈的那句話。
        if card_requires_gate_evidence:
            status_policy = (
                "Report passed only when every deterministic gate you ran (OpenSpec / pytest / "
                "policy) actually passed. Natural-language confidence, an exit code of 0, and "
                "the absence of an explicit error do NOT authorize passed. If a gate failed "
                "because of your change, report failed; the Manager re-reads the gate ledger "
                "and fails closed on any contradiction, so a dishonest passed only costs you "
                "a retry. "
                + gate_ledger.gate_scope_honesty_hint(env, test_policy=effective_test_policy)
            )
            gate_evidence_description = (
                "Declare every deterministic gate you actually ran and its real result. "
                "The Manager independently re-runs the declared gate commands after your "
                "process exits and compares; claiming a gate you did not run, or claiming "
                "passed for a gate that failed, fails the card closed. "
                + gate_ledger.gate_evidence_name_hint(env, test_policy=effective_test_policy)
            )
        else:
            status_policy = (
                "Report passed only when this card's own action is genuinely complete. "
                "Natural-language confidence, an exit code of 0, and the absence of an "
                "explicit error do NOT authorize passed; if the action is not complete, or "
                "the decision needs a human, report failed or needs_human instead. "
                + gate_ledger.gate_scope_honesty_hint(env, test_policy=effective_test_policy)
            )
            gate_evidence_description = gate_ledger.gate_evidence_name_hint(
                env, test_policy=effective_test_policy
            )
        terminal_schema = {
            "kind": "workflow-card",
            # #261 D1：canonical envelope。舊的 schema_version 1 形狀仍可被 harvest
            # 讀取（相容路徑＋legacy 標記），但新派工一律要求 canonical 版本。
            "schema_version": terminal_contract.TERMINAL_SCHEMA_VERSION,
            "required": [
                "schema_version", "kind", "status", "run_id", "card_id", "candidate",
                "outputs", "diagnostics", "gate_evidence",
            ],
            "fixed": fixed_terminal_fields,
            "status": ["passed", "failed", "needs_human"],
            # #261 R2：成功必須由 gate evidence 證明。模型自述、exit code 為 0、
            # 「沒看到錯誤」三者皆不構成成功授權；manager 會重讀 gate ledger 做
            # 確定性 cross-check，矛盾即 fail closed。
            #
            # #606：末段的範圍紀律（「focused 綠不得推定宣告的 gate 綠」）與
            # 下面 gate_evidence 的 allowed_names 說明同一條機械生成紀律——具體
            # 的 gate 名稱與命令由 operator 的 PSC_GATE_CMD_* 宣告導出，不手寫。
            #
            # #721：整段（含泛用前言）在上面依 `card_requires_gate_evidence` 分岔，
            # 適用範圍與 gate_evidence 那段同源。
            "status_policy": status_policy,
            "outputs": {
                "type": "array",
                "items": "repo-relative artifact path string matching declared_outputs",
                "must_match_every_declared_output": True,
                "descriptive_objects_forbidden": True,
            },
            # #261 R1：結構化 diagnostics，讓失敗與需人工介入有對等的表達位置。
            "diagnostics": {
                "type": "object",
                "description": (
                    "Structured, machine-readable context for this terminal. Required for "
                    "every status; put the concrete failure detail here when reporting "
                    "failed or needs_human instead of burying it in prose."
                ),
            },
            # #261 R2：模型自述跑了哪些 gate。Manager 會用它自己在你結束之後獨立
            # 產生的 gate ledger 對照這份宣告，任何不一致都會 fail closed。
            #
            # #540：canonical gate 名稱集合（`allowed_names`）與說明文字皆由
            # operator 的 `PSC_GATE_CMD_*` 宣告機械產生（gate_ledger 與寫 ledger
            # 用的是同一條導出路徑），不在此手寫第二份真實來源——舊 prompt 只寫
            # 「gate name」，模型只能自己造名字，採信必然撞
            # `gate-evidence-unknown-gate`。
            #
            # #721：名字機械化之後，**適用範圍**也機械化——契約不要求模型交出 gate
            # 結果的卡，`allowed_names` 為空且說明逐字要求 `gate_evidence: []`。
            "gate_evidence": {
                "type": "array",
                "items": {
                    "name": "one of allowed_names below",
                    "status": "passed | failed",
                },
                "allowed_names": list(card_gate_names),
                "description": gate_evidence_description,
            },
        }
        if effective_test_policy == "red-required":
            # #540：#307 的反轉判準過去只存在於 manager 側；泛用 status_policy
            # 對 tdd-red 卡字面上要求回 failed，與實際採信規則相反。說明文字由
            # terminal_contract 依同一組判準常數產生。
            terminal_schema["red_required_policy"] = (
                terminal_contract.red_required_status_hint()
            )
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
        "test_policy": effective_test_policy,
        "terminal_schema": terminal_schema,
    }
    if run.openspec_refs:
        contract["openspec_ref"] = run.openspec_refs[0]
    if builder_job_id is not None:
        contract["builder_job_id"] = builder_job_id
    if candidate_checkout is not None:
        contract["candidate_checkout"] = candidate_checkout
    if retry_context is not None:
        # #606：首派沒有這個鍵，prompt 因此逐字不變（見 `_workflow_retry_context`）。
        contract["retry_context"] = dict(retry_context)
    if operator_adjudications:
        # #757：operator 裁決是 **run 級**的權威答覆，不是某卡的重試歷史——一旦
        # 存在就隨每一次派工出現（builder／reviewer 皆然），不因 candidate 換新、
        # 卡片首派而消失。無裁決時本鍵缺席，prompt 逐字不變。
        contract["operator_adjudications"] = [
            dict(row) for row in operator_adjudications
        ]
    effective_commit_policy = step.commit_policy or fallback[2]
    tasks_path = (
        f"openspec/changes/{contract['openspec_ref']}/tasks.md"
        if isinstance(contract.get("openspec_ref"), str)
        else "openspec/changes/<change>/tasks.md"
    )
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
    commit_required_contract = (
        f" Before the final commit, update {tasks_path} checkboxes for work completed by this card, "
        "and never modify pinned input files such as the plan document."
        if effective_commit_policy == "required"
        else ""
    )
    repair_findings_contract = (
        _repair_findings_prompt_suffix(run, coordinator_root=coordinator_root)
        if effective_commit_policy == "required"
        else ""
    )
    # #606：明示語句與 retry_context 區塊成對出現。文字固定、數字機械帶入——
    # 「上一次為什麼被拒」的內容一律留在 retry_context 的結構化欄位裡。
    retry_context_contract = (
        (
            f" This card is being redispatched: attempt {int(retry_context.get('attempt', 2)) - 1} "
            "was rejected by the Manager's own independent evidence, reproduced verbatim in "
            "the retry_context block of the contract below. That block is Manager-generated "
            "(its own gate ledger and its own acceptance error), not the previous attempt's "
            "self-report, and it is not negotiable. Reproduce that failure first, fix it, and "
            "only then complete this card; repeating the previous attempt without addressing "
            "it will be rejected identically."
        )
        if retry_context is not None
        else ""
    )
    generic_preamble = (
        "Execute exactly one workflow card. End with one JSON object only; do not supply an evidence "
        "path or hash because Manager will canonicalize it. For workflow-card outputs, return only "
        "repo-relative artifact path strings matching declared_outputs; when declared_outputs is "
        "empty, outputs must be exactly []. Never put action, summary, or other descriptive objects "
        "in outputs. For build cards, candidate must be the full 40-hex commit SHA of the worktree "
        "HEAD after the card completes (use `/usr/bin/git rev-parse HEAD` as one command, with "
        "no pipe, boolean fallback, redirection, or suffix); commit-forbidden cards must "
        "report the current HEAD, build candidates must never be null, and plan candidates must be "
        "null."
        + planner_contract
        + reviewer_contract
        + commit_required_contract
        + repair_findings_contract
        + retry_context_contract
    )
    preamble = (
        job_runner.WORKTREE_ISOLATION_AUTONOMOUS_PREAMBLE
        if step.phase == "build"
        and step.card == "worktree-isolation"
        and step.persona == "builder"
        and retry_context is None
        and not operator_adjudications
        else generic_preamble
    )
    return preamble + " Contract: " + json.dumps(
        contract, ensure_ascii=False, sort_keys=True
    )


def _plan_frontmatter_artifact_classes(text: str) -> frozenset[str] | None:
    """讀 plan 自己宣告的 ``artifact_classes``（複用 ``_report_binding`` 的 frontmatter
    抽取手法），供 ``planning.plan_review_gate()`` 的 ``acceptance_surfaces`` 輸入
    ——與 ``_plan_review_envelope`` 消費同一個宣告欄位語意一致：plan 說它覆蓋哪些
    artifact classes，completeness 檢查 Tasks 是否真的每個都有對應項目。缺席或
    格式錯誤一律回傳 ``None``（fail-soft，呼叫端據此判斷 gate 是否可跑）。
    """

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        closing = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration:
        return None
    try:
        payload = safe_load("\n".join(lines[1:closing]))
    except YAMLError:
        return None
    if not isinstance(payload, dict):
        return None
    classes = payload.get("artifact_classes")
    if (
        not isinstance(classes, list)
        or not classes
        or any(not isinstance(item, str) or not item.strip() for item in classes)
    ):
        return None
    return frozenset(item.strip() for item in classes)


def _plan_review_envelope_lookup(run, identities: IdentityRegistry):
    """#452 C：`planning._plan_review_envelope` 的 envelope_lookup provider。

    投影對象是「本 run 將解析到的 builder 身分」（#205 覆寫優先，與 dispatch
    同一條解析路徑）。#453 R5／#454 R5：投影所需兩鍵任一來源為 default 即回
    ``None``——v1 映射不量測這兩欄，現況恆回 None，seam 證據字節與
    ``envelope_lookup=None`` 逐位元相同；實測值落地後自動開始真值過濾。
    """

    def _lookup():
        try:
            candidates = _workflow_identity_candidates_for_persona(run, "builder", identities)
        except ValueError:
            return None
        from .model_identities import plan_review_envelope_projection

        return plan_review_envelope_projection(candidates[0], persona="builder")

    return _lookup


def _evaluate_yellow_plan_review(
    artifacts: tuple[PlanningArtifact, ...] | None,
    envelope_lookup=None,
):
    """#208 收口 wiring 2：Yellow band 推進 build 前的機械 plan review 判定。

    輸入不可得（沒有 plan artifact、或它缺少 ``artifact_classes`` 宣告）一律
    fail-soft 回傳 ``None``——呼叫端據此維持現行為（正常推進），不得讓既有
    測試變紅。``applicable_contract_rules`` 固定餵 ``ACCEPTANCE_SURFACE_RULES``
    全集，理由同 ``work_bridge.current_sizing_snapshot``。

    ``envelope_lookup``（#452 C）由呼叫端以 :func:`_plan_review_envelope_lookup`
    注入；預設 ``None`` 維持 #209 未接線時的既有 bypass 語意。
    """

    if artifacts is None:
        return None
    plan_artifact = next((item for item in artifacts if item.kind == "plan"), None)
    if plan_artifact is None:
        return None
    acceptance_surfaces = _plan_frontmatter_artifact_classes(plan_artifact.text)
    if acceptance_surfaces is None:
        return None
    try:
        return plan_review_gate(
            plan_artifact=plan_artifact,
            acceptance_surfaces=acceptance_surfaces,
            applicable_contract_rules=ACCEPTANCE_SURFACE_RULES,
            envelope_lookup=envelope_lookup,
        )
    except ValueError:
        return None


def _dispatch_workflow_card(
    dispatcher,
    *,
    run,
    identities: IdentityRegistry,
    launcher_factory: Callable[[object], object],
    coordinator_root: str | Path,
    retry_failed: bool = False,
    operator_recovery_job_id: str | None = None,
    force_new_card: bool = False,
    forced_identity: object | None = None,
    spawn_admission: SpawnAdmissionLimiter | None = None,
) -> dict[str, object] | None:
    """#381：workflow lane 的實際 spawn 點。spawn_admission 未注入時解析為
    零間隔 no-op（見 spawn_admission.resolve_limiter）——只有 resume_workflow_run
    /manager_daemon periodic tick 顯式注入同一個 instance 時，兩條 lane 才會
    對同一 provider 共用節流時間軸。

    ``force_new_card``（#545 的 ``force_new_build`` 於 #569 一般化）：operator
    已透過 ``retry-build``／``retry-card`` 明確授權重派當前這張卡，因此即使該卡
    最新的 job 已經終止（``exited``／``failed``）也要派出**新** job，而不是把舊
    的那顆回傳給呼叫端再讀一次。受理的卡與 ``registry.RETRY_CARD_PHASE_PERSONA``
    同一份判準：build phase 的 builder 卡、verify／review phase 的 reviewer 卡。
    """
    registry = getattr(dispatcher, "_registry", None)
    if registry is None:
        raise RuntimeError("workflow dispatch requires dispatcher registry")
    step = _current_workflow_step(run)
    if step is None or run.current_phase not in {"plan", "build", "verify", "review"}:
        return None
    if force_new_card and RETRY_CARD_PHASE_PERSONA.get(step.phase) != step.persona:
        raise ValueError("forced workflow retry requires builder or reviewer card")
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
    # #765：reuse／retry 判定只認**本 claim era** 的 job（None 容忍比照 #766/#768）。
    # `matching` 本身維持全 era——retry-context 與 sandbox 清理需要完整歷史；但
    # 「這張卡已有 job、直接回傳供 harvest」的 reuse 決策若拿到前代 era 的
    # terminal（authority restart 之後），advance 的 binding 對現 era 必炸且每
    # tick 重炸（實機：verification-38 經此路徑被無限重放，era 過濾了 resume 與
    # retry-card 兩處後仍炸的最後出口）。
    reusable = [
        job
        for job in matching
        if job.get("workflow_claim_key") in (None, run.claim_key)
    ]
    retryable_latest = bool(
        reusable
        and (
            (
                retry_failed
                and (
                    _is_stale_terminalized_failed_job(reusable[-1])
                    or _retryable_nonpassing_workflow_terminal(reusable[-1])
                    or _malformed_workflow_card_terminal(reusable[-1])
                    or _is_rejected_workflow_review_evidence(
                        reusable[-1],
                        run=run,
                        coordinator_root=coordinator_root,
                    )
                )
            )
            or (
                operator_recovery_job_id == reusable[-1].get("job_id")
                and (
                    _is_exact_legacy_agy_recovery(
                        reusable[-1], run=run, step=step, identities=identities
                    )
                    or _is_exact_reviewer_terminal_recovery(
                        registry,
                        reusable[-1],
                        run=run,
                        step=step,
                        identities=identities,
                        coordinator_root=coordinator_root,
                    )
                )
            )
            or (force_new_card and reusable[-1].get("status") in TERMINAL_STATUSES)
        )
    )
    if reusable and not retryable_latest:
        return reusable[-1]
    # #569：reviewer 卡的強制重派要先回收被取代 job 的 sandbox。sandbox 目錄名是
    # `sha256(run_id:card:candidate)`（見 `_create_reviewer_sandbox`），重派同一
    # 張卡＋同一個 candidate 必然撞上「stale reviewer sandbox requires
    # reconciliation」而派不出去。`require_candidate_unchanged=True` 讓「reviewer
    # 動過 candidate」fail closed——重派不得成為蓋掉這個事實的名義。刻意只掛在
    # forced 路徑上：其餘既有路徑的 sandbox 已由 terminalize／resume 的既有回收
    # 點處理，行為一個字節都不動。
    if force_new_card and matching and step.persona == "reviewer":
        _discard_reviewer_sandbox(
            matching[-1],
            coordinator_root=coordinator_root,
            require_candidate_unchanged=True,
        )
    if matching and step.persona == "planner":
        _discard_failed_planner_sandbox(
            matching[-1],
            run_id=run.run_id,
            card=step.card,
            coordinator_root=coordinator_root,
        )
    if step.persona == "planner" and step.phase == "plan":
        artifacts = _load_run_planning_artifacts(run)
        try:
            planning_complete = artifacts is not None and assess_planning_completeness(artifacts).complete
        except ValueError:
            planning_complete = False
        if planning_complete:
            pending_phase_steps = [
                item
                for item in run.steps
                if item.phase == run.current_phase and item.gate_result != "passed"
            ]
            is_last_pending = bool(pending_phase_steps) and step.card == pending_phase_steps[-1].card
            if is_last_pending and run.current_phase == "plan" and run.sizing_band == "red":
                # #223（design #208 H.3）：Red band 收斂到 needs_decomposition，
                # 不推進到 build；current_phase 刻意保持在 plan
                # （validate_workflow_phase_transition 只允許單調 +1，Red 決策
                # 不是合法的 phase transition，見 #223 讀碼地圖）。拆分深度逾限
                # （decomposition_depth 已達上限）改轉 needs_human（驗收條件
                # 3）。每次 dispatch 都會重新檢查 sizing_band，band 跨帶上升
                # 至 red 時同樣在此攔下、不會以原身分繼續推進（驗收條件 4）。
                route = decomposition_route(decomposition_depth=run.decomposition_depth)
                route_reason = (
                    "decomposition-depth-exceeded"
                    if route == "needs_human"
                    else "needs-decomposition"
                )
                updated = registry._manager_update_workflow_run(
                    run.run_id,
                    facets=tuple(dict.fromkeys((*run.facets, route))),
                    # route 為 `needs_decomposition` 時不帶理由（那不是本 invariant
                    # 的管轄範圍，且它自己就是可讀的路由結論）；只有轉入
                    # `needs_human`（拆分深度已達 #223 的上限、不能再拆）才落理由。
                    needs_human_reason=(
                        diagnostic_reason(
                            route_reason,
                            f"sizing_band=red 但 decomposition_depth 已達上限"
                            f"（depth={run.decomposition_depth}），不得再拆一層",
                            source="manager._dispatch_workflow_card:decomposition-route",
                            run_id=run.run_id,
                            work_id=run.work_id,
                            sizing_band=run.sizing_band,
                            decomposition_depth=str(run.decomposition_depth),
                        )
                        if route == "needs_human"
                        else None
                    ),
                )
                return {
                    "run_id": updated.run_id,
                    "current_phase": updated.current_phase,
                    "reason": (
                        "decomposition-depth-exceeded"
                        if route == "needs_human"
                        else "needs-decomposition"
                    ),
                }
            plan_review_passed_now = False
            if is_last_pending and run.current_phase == "plan" and run.sizing_band == "yellow":
                # #208 收口 wiring 2：Yellow 先機械 plan review 再放行進 build
                # （#212 的判定機制，這裡只接線）。Green／Red／None band 不呼叫
                # gate（Red 已在上面攔下；Green/None 維持現行為，#223 已定案的
                # fail-soft 慣例）。
                gate_outcome = _evaluate_yellow_plan_review(
                    artifacts,
                    envelope_lookup=_plan_review_envelope_lookup(run, identities),
                )
                if gate_outcome is not None and not gate_outcome.ready:
                    if gate_outcome.terminal:
                        updated = registry._manager_update_workflow_run(
                            run.run_id,
                            facets=tuple(dict.fromkeys((*run.facets, "needs_human"))),
                            needs_human_reason=diagnostic_reason(
                                f"plan-review-{gate_outcome.failed_check}",
                                "Yellow band 的機械 plan review 判定為終局不通過"
                                f"（failed_check={gate_outcome.failed_check}）",
                                source="manager._dispatch_workflow_card:plan-review",
                                run_id=run.run_id,
                                work_id=run.work_id,
                                card=step.card,
                                sizing_band=run.sizing_band,
                            ),
                        )
                        return {
                            "run_id": updated.run_id,
                            "current_phase": updated.current_phase,
                            "reason": f"plan-review-{gate_outcome.failed_check}",
                        }
                    # non-terminal：不推進，記錄可重試原因；不動 run（下次
                    # dispatch 對同一份 plan 重跑同一個判定，plan 修訂後即可
                    # 通過——與 Red 的 needs_decomposition 不同，這裡不是終局
                    # 路由，run 狀態原樣保留）。
                    return {
                        "run_id": run.run_id,
                        "current_phase": run.current_phase,
                        "reason": f"plan-review-retry-{gate_outcome.failed_check}",
                    }
                if gate_outcome is not None and gate_outcome.ready:
                    plan_review_passed_now = True
                # gate_outcome is None：輸入不可得（fail-soft），落到下面正常推進，
                # 維持現行為，不掛 plan_review_passed。
            # #414：deterministic pass 這張 plan 卡之前，先驗證卡片宣告的
            # outputs glob 是否已在 workspace_root 命中實檔——不對稱地只驗
            # build 端 declared input、卻放過 plan 端 declared output，正是
            # 生產事故（run workflow-e18785ac）的根因。缺席時嘗試
            # materialize；不可 materialize 時 fail-closed 不跳過（詳見
            # `_materialize_plan_card_output` docstring）。
            workspace_root = Path(run.workspace_root).resolve()
            new_authority: PlanningArtifactAuthority | None = None
            publication: _PlanningPublicationTransaction | None = None
            if not _plan_card_declared_outputs_present(workspace_root, step.outputs):
                materialize_result = _materialize_plan_card_output(
                    run=run, step=step, artifacts=artifacts, workspace_root=workspace_root,
                )
                if materialize_result is None:
                    return {
                        "run_id": run.run_id,
                        "current_phase": run.current_phase,
                        "reason": "plan-outputs-missing",
                    }
                artifacts, new_authority, publication = materialize_result
            next_phase = run.current_phase
            attempts = run.attempts
            if is_last_pending:
                next_phase = WORKFLOW_PHASES[WORKFLOW_PHASES.index(run.current_phase) + 1]
                attempts = {
                    **run.attempts,
                    next_phase: run.attempts.get(next_phase, 0) + 1,
                }
            try:
                registry._manager_update_workflow_run(
                    run.run_id,
                    current_phase=next_phase,
                    steps=_audit_phase_steps(
                        run.steps,
                        phase=run.current_phase,
                        executor="cortex-manager",
                        model="deterministic",
                        domain="cortex",
                        outputs=tuple(artifact.ref for artifact in artifacts),
                        card_id=step.card,
                    ),
                    attempts=attempts,
                    **({"plan_review_passed": True} if plan_review_passed_now else {}),
                    **(
                        {"planning_authority": run.planning_authority + (new_authority,)}
                        if new_authority is not None
                        else {}
                    ),
                )
            except BaseException:
                if publication is not None:
                    publication.rollback()
                raise
            return None
    # #262 runtime preflight gate：在建立 worktree／sandbox／job row／model session
    # 之前，於實際將被使用的 executor 環境驗證 card 宣告的 capability 與 provider
    # 新鮮度。未宣告 capability 的 card 完全走原路徑（gate 為 no-op）。
    # #384：`forced_identity` 提供時（provider 失敗 bounded retry 的 re-route
    # 決策，見 `resume_workflow_run`／`_provider_failure_reroute`）代表呼叫端
    # 已經跑過一次針對「剛剛觀察到的失敗」的 evaluate_dispatch_gate 決策，這裡
    # 不再重跑一次通用 preflight（重跑不會產生更好的答案，只是多付一次探測
    # 成本）。
    gate = (
        None
        if forced_identity is not None
        else _runtime_preflight_gate(
            run,
            step,
            identities=identities,
            launcher_factory=launcher_factory,
        )
    )
    if gate is not None and gate.action == "needs_human":
        updated = registry._manager_update_workflow_run(
            run.run_id,
            facets=tuple(dict.fromkeys((*run.facets, "needs_human"))),
            needs_human_reason=diagnostic_reason(
                f"runtime-preflight-{gate.result.outcome.value}",
                "runtime preflight 在建立 worktree／job 之前判定不可派工："
                f"{gate.reason or gate.result.blocking_reason() or gate.result.outcome.value}",
                source="manager._dispatch_workflow_card:runtime-preflight",
                run_id=run.run_id,
                work_id=run.work_id,
                card=step.card,
                outcome=gate.result.outcome.value,
            ),
        )
        return {
            "run_id": updated.run_id,
            "current_phase": updated.current_phase,
            "reason": f"runtime-preflight-{gate.result.outcome.value}",
            "runtime_preflight": gate.to_dict(),
        }
    if gate is not None:
        # gate.launcher 已由 _runtime_preflight_gate 套過執行契約，不再 specialize。
        identity = gate.identity
        launcher = gate.launcher
        if launcher is None:
            raise ValueError("workflow launcher unavailable")
    elif forced_identity is not None:
        identity = forced_identity
        launcher = launcher_factory(identity)
        if launcher is None:
            raise ValueError("workflow launcher unavailable")
        launcher = _specialize_workflow_launcher(launcher, step)
    else:
        identity = _select_workflow_identity(run, step, identities)
        launcher = launcher_factory(identity)
        if launcher is None:
            raise ValueError("workflow launcher unavailable")
        launcher = _specialize_workflow_launcher(launcher, step)
    if identities.resolution is not None and identity is not None:
        # The candidate path already checked the static registry contract.  A
        # final check against the specialized launcher closes the remaining
        # dependency seam before any job/worktree launch side effect.
        model_resolution.validate_identity_compatibility(
            step.persona, identity, launcher=launcher
        )
    # #205 R4/D5：稽核實際解析到的模型鏈。接在兩條路徑之後，因此 #262 preflight
    # re-route 換掉的 identity 也會被如實記錄（記的是真正要跑的那個，不是原選擇）。
    _record_resolved_model_chain(registry, run, step, identity, identities)
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
    task = f"wf-{hashlib.sha256(run.run_id.encode()).hexdigest()[:10]}-{step.card}"
    # #648：job_id 必須在 **provision 之前**就定案——per-job 工作區的目錄名就是
    # `job_workspace.job_segment(job_id)`，而 `launcher.launch(slice_id=job_id)`
    # 之後交給 `job_runner.prepare_systemd_template(job_id=…)` 算 instance 名的也是
    # 同一個字串。順序不能反過來：`create_job()` 的 `workflow_input_snapshot` /
    # `workflow_output_baseline` 都是從工作區的檔案算出來的。
    # 配發即消耗（見 `registry.reserve_job_id`），因此 provision 失敗只是燒掉一個
    # 序號，不會有兩個 job 共用同一個 id、進而共用同一個目錄。
    reserved_job_id = registry.reserve_job_id(task)
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
    elif step.phase == "build":
        creator = getattr(dispatcher, "_worktree_creator", None)
        workspace_root = Path(run.workspace_root)
        if creator is None:
            try:
                creator = seams.ScriptWorktreeCreator(
                    repo=workspace_root,
                    wt_root=worktree_root_for(workspace_root),
                    base="main",
                )
            except BaseException as exc:
                raise ValueError("workflow builder worktree creator unavailable") from exc
        #: #633：改問 `anchored_at()` 而不是自己比較 `creator.repo_root`——lazy 化
        #: 之後「repo 尚未解析且環境沒宣告」是 dispatcher 上一個合法的 creator 狀態，
        #: 直接讀 `repo_root` 會讓 `RepoRootUnresolvedError` 從一句比較裡漏出去。
        #: 語意不變：錨定的不是本 run 的 workspace_root 就換一個錨定正確的。
        elif isinstance(creator, seams.ScriptWorktreeCreator) and not creator.anchored_at(
            workspace_root
        ):
            try:
                creator = seams.ScriptWorktreeCreator(
                    repo=workspace_root,
                    wt_root=worktree_root_for(workspace_root),
                    base="main",
                )
            except BaseException as exc:
                raise ValueError("workflow builder worktree creator unavailable") from exc
        # #731：branch 名的推導抬成 `workflow_build_branch()`（模組級單一導出點），
        # 讓 `work refreeze-base` 的 #613 前置檢查問到的是**同一條** branch。
        builder_branch = workflow_build_branch(run)
        # #648：canonical lane 的工作區改為 **per-job**——每一張 build 卡自己 clone
        # 一份，目錄名 ＝ 這張卡的 job_id 經 `job_workspace.job_segment()` 導出的
        # 片段，也就是 `job_runner.template_instance_id()` 算出來的 instance 名。
        # #645／#646 之後 canonical lane 傳的是 run 層級的 build 身分，一個工作區
        # 對多個 job_id，`ReadWritePaths=<pool>/%i` 對第二張卡起必然指向不存在的
        # 路徑（`226/NAMESPACE`）；改 per-job 之後那個一對多消失，不變式成立。
        # 這裡刻意與 slice lane（`autonomy._launcher_worktree`）用**同一個推導點**。
        build_branch = (
            str(builder_jobs[-1]["branch"])
            if builder_jobs and isinstance(builder_jobs[-1].get("branch"), str)
            else builder_branch
        )
        accepted_candidate = (
            run.candidate_head
            if isinstance(run.candidate_head, str)
            and verification.SAFE_SHA_RE.fullmatch(run.candidate_head) is not None
            else None
        )
        if builder_jobs and accepted_candidate is not None:
            # 中段／後續 build 卡：base 是**來源樹上這條 branch 現在的位置**，也就是
            # 前一張卡 harvest 回來（#637 bundle ＋ append-only spool）之後被採信的
            # candidate。交接因此完全走 Manager 自己的 object store，不依賴前一張卡的
            # 工作區還留在磁碟上——那個目錄可以已經被回收掉。
            # `run.candidate_head` 是 Manager 採信的權威值；推不出時 fail-closed，
            # **絕不**讓 base 落回 creator 的預設（那會是 `main`，等於把整個 run 的
            # 成果 reset 掉）。base 對不上來源樹的實況時，creator 既有的兩道守衛會擋：
            # `rev-parse --verify <base>` 找不到 commit ⇒ harvest 沒完成；
            # `merge-base --is-ancestor <branch> <base>` ⇒ branch 上有 base 以外的
            # commit（#613 的形狀），一律拒絕 provision。
            build_base_sha = _workflow_build_handoff_base(
                run, builder_jobs=builder_jobs, card=step.card
            )
        else:
            # 首張 build 卡：#208 收口 wiring 5（#211 閉環）——凍結集存在時必須以
            # frozen_readiness["base_sha"] 為基底，不得讓 dispatch 自行重新推導一個
            # 可能更新鮮（或更陳舊）的 base（hippo #18 #2／#41 v2 的 stale-base 缺陷）。
            build_base_sha = None
            if isinstance(run.frozen_readiness, dict):
                candidate_base_sha = run.frozen_readiness.get("base_sha")
                if isinstance(candidate_base_sha, str) and candidate_base_sha:
                    build_base_sha = candidate_base_sha
        # 無凍結集且為首張卡時完全不傳 base_sha 引數，維持現行為（呼叫端保有舊
        # WorktreeCreator 實作 without base_sha 亦不受影響）。
        if build_base_sha is not None:
            worktree = str(
                creator.create(
                    build_branch, job_id=reserved_job_id, base_sha=build_base_sha
                )
            )
        else:
            worktree = str(creator.create(build_branch, job_id=reserved_job_id))
    elif step.persona == "reviewer":
        # #650：verify／review 卡的 candidate 樹改為 **Manager 自己在來源樹上 clone
        # 出來的一棵**，不再是 `builder_jobs[-1]["worktree"]`。
        #
        # 為什麼在這裡（而不是等到 reviewer 分支）provision：`_workflow_input_snapshot()`
        # 是 `_create_reviewer_sandbox()` 的**輸入**，算它時 sandbox 還不存在——票上點名
        # 的順序問題。解法沿用 #653 對 `archive-applied-needs-commit` 的處置：**同一次
        # 派工內結構性共用同一個 provisioning**。candidate 樹在這裡建好一次，
        # authority map／input snapshot／output baseline／sandbox clone 源／tree
        # snapshot 五個用途全部拿到同一棵樹，順序問題因此不是被「解決」而是**不存在**。
        #
        # branch 與底下 job 記錄用的那一個是同一條推導（前一張 build 卡的 branch；
        # post-archive 時是 `_record_manager_ship_job()` 記在 archive 卡上的那一條）。
        reviewer_branch = (
            str(builder_jobs[-1]["branch"])
            if builder_jobs and isinstance(builder_jobs[-1].get("branch"), str)
            else f"feature/{run.work_id}"
        )
        reviewer_candidate = run.candidate_head
        if (
            not isinstance(reviewer_candidate, str)
            or verification.SAFE_SHA_RE.fullmatch(reviewer_candidate) is None
        ):
            # 與 `_create_reviewer_sandbox()` 逐字相同的訊息：candidate 推不出來時
            # 這條 lane 本來就走不下去，只是現在擋在建樹之前。
            raise ValueError("workflow reviewer candidate invalid")
        worktree = str(
            _reviewer_candidate_workspace(
                run=run,
                branch=reviewer_branch,
                candidate=reviewer_candidate.lower(),
            )
        )
    elif builder_jobs:
        worktree = str(builder_jobs[-1]["worktree"])
    else:
        worktree = run.workspace_root
    effective_repo_root = Path(worktree).resolve()
    effective_inputs = _effective_workflow_inputs(run, step)
    if step.persona == "reviewer":
        reviewer_target = effective_repo_root
        # #310 補遺：checkbox 容忍成立的 tasks/todo 以候選實際 hash 為 pinned 期望值。
        authority_map = _authority_map_with_checkbox_tolerance(
            run, candidate_root=reviewer_target
        )
        effective_inputs = _reviewer_input_patterns(run, effective_inputs)
        input_snapshot = _workflow_input_snapshot(
            run=run,
            repo_root=reviewer_target,
            patterns=effective_inputs,
            coordinator_root=coordinator_root,
        )
        foreign_review.verify_authority_in_input_snapshot(
            authority=authority_map,
            input_snapshot=input_snapshot,
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
    try:
        branch = (
            # #648：build 卡的 branch 已在 provisioning 當下定案（`build_branch`
            # 就是傳給 `creator.create()` 的那一個）。在這裡重算一次等於再開一個
            # 會漂移的來源——job 記錄的 branch 與工作區實際 checkout 的 branch 對不
            # 上，harvest 的 refspec 就會指到別條 ref。
            build_branch
            if step.phase == "build"
            else (
                str(builder_jobs[-1]["branch"])
                if builder_jobs and isinstance(builder_jobs[-1].get("branch"), str)
                else f"feature/{run.work_id}"
            )
        )
        job = registry.create_job(
            task=task,
            job_id=reserved_job_id,
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
            # #379：派工當下 pin 住這張卡的驗收判準（test_policy），供 harvest 時
            # 與 registry 現有 WorkflowRun.steps 的現值比對出 drift（見
            # manager._workflow_acceptance_definition_drifted）。
            workflow_test_policy=step.test_policy,
        )
    except BaseException:
        if planner_sandbox is not None:
            shutil.rmtree(planner_sandbox, ignore_errors=True)
        if reviewer_sandbox is not None:
            shutil.rmtree(reviewer_sandbox, ignore_errors=True)
        raise
    try:
        # #381：真正 spawn 前才 admit，不佔住這張卡接下來的整個執行期。
        resolve_limiter(spawn_admission).admit(resolve_provider(identity=identity, launcher=launcher))
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
                    else "."
                    if step.persona == "reviewer"
                    else None
                ),
                # #606：`matching` 就是這張卡先前燒掉的 job（首派為空 →
                # retry_context 為 None → prompt 逐字不變）。retry-card 的重派與
                # daemon 的 forced retry 都走這唯一一條組裝路徑，因此兩者同時
                # 拿到回饋，不需要第二份實作。
                retry_context=_workflow_retry_context(
                    matching,
                    registry=registry,
                    # #750：只有 builder 的 build 卡吃跨卡回饋——repair 回合的
                    # 消費者。reviewer 卡維持既有語意（它自己的前次失敗已由
                    # #606 覆蓋）。
                    review_rejection=(
                        _prior_review_rejection(run, registry)
                        if step.phase == "build" and step.persona == "builder"
                        else None
                    ),
                ),
                # #757：run 級裁決獨立於 retry_context——verify/review 的 matching
                # 以 candidate 定錨，candidate 換新即空，掛在底下會讓裁決消失。
                operator_adjudications=_operator_adjudications(run, coordinator_root),
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
            template_instance=handle.template_instance,
            runtime_principal=handle.runtime_principal,
            runtime_mode=handle.runtime_mode,
            runtime_surface=handle.runtime_surface,
            credential_publish=handle.credential_publish,
            prompt_path=handle.prompt_path,
            control_log_path=handle.control_log_path,
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
    force_new_card: bool = False,
    forced_identity: object | None = None,
    spawn_admission: SpawnAdmissionLimiter | None = None,
) -> dict[str, object] | None:
    """Dispatch a normal workflow card; legacy recovery is operator-resume internal only."""

    return _dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=launcher_factory,
        coordinator_root=coordinator_root,
        retry_failed=retry_failed,
        force_new_card=force_new_card,
        forced_identity=forced_identity,
        spawn_admission=spawn_admission,
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


def _provider_rate_limit_result(
    exc: BaseException, *, run_id: str, current_phase: str, coordinator_root: str | Path
) -> dict[str, object] | None:
    """#370: translate a canonical-authority rate-limit failure into a soft,
    non-raising resume result instead of the generic needs_human+re-raise
    path used for every other ship_validator/authority failure.

    Returns ``None`` (meaning: "not this case, handle it the old way") for
    every exception except an :class:`AuthorityValidationError` carrying
    :data:`REASON_PROVIDER_RATE_LIMITED_CANONICAL` -- a real authority
    defect (malformed row, missing provider, genuine auth failure upstream,
    ...) still needs_human+raises exactly as before.

    Records a durable backoff deadline (survives daemon restart -- see
    ``provider_backoff.py``) so the *next* resume attempt, operator-driven
    or periodic-tick, can short-circuit before the window has passed
    instead of re-hitting the same rate limit immediately.
    """
    if not (isinstance(exc, AuthorityValidationError) and exc.reason_code == REASON_PROVIDER_RATE_LIMITED_CANONICAL):
        return None
    provider_id = exc.provider_id or "github"
    backoff = provider_backoff.record_backoff(coordinator_root, provider_id, now=time.time())
    return {
        "run_id": run_id,
        "current_phase": current_phase,
        "reason": "provider-rate-limited",
        "retry_after_epoch": backoff.deadline_epoch,
    }


def resume_workflow_run(
    dispatcher,
    *,
    run_id: str,
    identities: IdentityRegistry,
    launcher_factory: Callable[[object], object],
    coordinator_root: str | Path,
    ship_validator: Callable[..., object] | None = None,
    operator_resume: bool = False,
    spawn_admission: SpawnAdmissionLimiter | None = None,
) -> dict[str, object]:
    registry = getattr(dispatcher, "_registry", None)
    if registry is None:
        raise RuntimeError("workflow resume requires dispatcher registry")
    run = registry.get_workflow_run(run_id)
    if ship_validator is not None:
        # #370: a prior resume already recorded a durable rate-limit
        # backoff for this run's GitHub provider (see
        # `_provider_rate_limit_result` below) -- short-circuit *before*
        # doing any work, so an operator retrying early (or a periodic
        # tick landing before the window clears) gets an explicit "still
        # rate limited, retry after <time>" instead of walking the whole
        # resume flow only to hit ship_validator and the same wall again.
        # Gated on `ship_validator is not None` because that's the only
        # thing in this function that can ever touch GitHub provider
        # authority; canonical claim:v1 runs are wired with one uniformly
        # regardless of phase, so this deliberately pauses all phases for
        # a rate-limited repo, not just the review->ship transition.
        active = provider_backoff.active_backoff(
            coordinator_root, f"github:{run.repo}", now=time.time()
        )
        if active is not None:
            return {
                "run_id": run.run_id,
                "current_phase": run.current_phase,
                "reason": "provider-rate-limited",
                "retry_after_epoch": active.deadline_epoch,
            }
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
                # #765：recovery 同樣只認本 claim era（None 容忍比照 #766/#768）。
                # 這是最後一個 era-blind 選擇器：operator_resume 的 recovery 分支
                # 會把前代 era 的已綁 evidence job 抓回重放，advance 的 binding
                # 對現 era 必炸（實機：verification-38 每次 resume 被重放）。
                and job.get("workflow_claim_key") in (None, run.claim_key)
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
    except PlanningPublicationDrift as exc:
        updated = registry._manager_update_workflow_run(
            run.run_id,
            facets=("needs_human",),
            gate_status="running",
            needs_human_reason=diagnostic_reason(
                "planning-publication-drift",
                f"resume 前 reconcile planning 發佈事務偵測到 drift：{str(exc)[:200]}",
                source="manager.resume_workflow_run:planning-publication-drift",
                run_id=run.run_id,
                work_id=run.work_id,
                phase=run.current_phase,
            ),
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
        except ValueError as exc:
            current = registry.get_workflow_run(run.run_id)
            updated = registry._manager_update_workflow_run(
                run.run_id,
                facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
                gate_status=pre_resume_gate_status,
                # #514：這條路徑過去把例外整個丟掉，run 上只留下
                # `planning-authority-reconciliation-failed` 這個籠統的回傳值
                # （而回傳值在 daemon periodic tick 根本沒人消費）。
                # `_validated_brainstorm_planning_authority` 現在會把 ref、
                # assessment reasons 與 blocking marker 行號寫進例外訊息，
                # 這裡原樣透傳進 run 的結構化理由。
                needs_human_reason=diagnostic_reason(
                    "planning-authority-reconciliation-failed",
                    f"已發佈 planning artifact 的重驗失敗：{summarize_exception(exc, limit=300)}",
                    source="manager.resume_workflow_run:planning-authority",
                    run_id=run.run_id,
                    work_id=run.work_id,
                    phase=run.current_phase,
                ),
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
                    spawn_admission=spawn_admission,
                )
            return dispatch_workflow_card(
                dispatcher,
                run=bound_run,
                identities=identities,
                launcher_factory=launcher_factory,
                coordinator_root=coordinator_root,
                retry_failed=retry,
                spawn_admission=spawn_admission,
            )
        except Exception as exc:
            current = registry.get_workflow_run(bound_run.run_id)
            registry._manager_update_workflow_run(
                bound_run.run_id,
                facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
                gate_status="running",
                needs_human_reason=diagnostic_reason(
                    "workflow-card-dispatch-failed",
                    f"派工卡片時擲出例外：{summarize_exception(exc)}",
                    source="manager.resume_workflow_run:dispatch_or_stop",
                    run_id=bound_run.run_id,
                    work_id=bound_run.work_id,
                    phase=bound_run.current_phase,
                    retry="true" if retry else "false",
                ),
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
            except Exception as exc:
                rate_limited = _provider_rate_limit_result(
                    exc,
                    run_id=run.run_id,
                    current_phase=run.current_phase,
                    coordinator_root=coordinator_root,
                )
                if rate_limited is not None:
                    return rate_limited
                current = registry.get_workflow_run(run.run_id)
                registry._manager_update_workflow_run(
                    run.run_id,
                    facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
                    gate_status="failed",
                    needs_human_reason=diagnostic_reason(
                        "review-advance-failed",
                        f"review→ship 的 advance 擲出例外：{summarize_exception(exc)}",
                        source="manager.resume_workflow_run:review-advance",
                        run_id=run.run_id,
                        work_id=run.work_id,
                        card=last.card,
                    ),
                )
                raise
        if run.current_phase == "ship" and run.status == "done" and post_merge_closure:
            if ship_validator is None:
                return {
                    "run_id": run.run_id,
                    "current_phase": run.current_phase,
                    "reason": "ship-validator-unavailable",
                }
            try:
                return apply_workflow_action(
                    registry,
                    args={"action": "refresh-completion", "run_id": run.run_id},
                    identity_registry=identities,
                    ship_validator=ship_validator,
                    git_runner=getattr(dispatcher, "_git_runner", None),
                    coordinator_root=coordinator_root,
                    trusted_terminal=True,
                )
            except Exception as exc:
                rate_limited = _provider_rate_limit_result(
                    exc,
                    run_id=run.run_id,
                    current_phase=run.current_phase,
                    coordinator_root=coordinator_root,
                )
                if rate_limited is not None:
                    return rate_limited
                raise
        return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": "no-pending-card"}
    jobs = [
        job
        for job in registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_card") == step.card
        and job.get("workflow_phase") == step.phase
        # #765：advance 只認**本 claim era** 的 terminal job。authority restart
        # （#373：operator link／PR 建立等 authority 前進）會重算 claim_key 並把
        # verify/review 打回 pending——設計語意是「在新 era 下重驗」；前代 era 的
        # terminal job 若仍被撿起，`_job_for_workflow_card` 的 claim_key 綁定必炸，
        # 而且每 tick 重炸（#373 docstring 記載的那個永動迴圈的另一半）。era 不符
        # 的 job 是前代稽核列，不是本 era 的候選——跳過之後 `dispatch_or_stop`
        # 會為新 era 重新派工。
        # 缺欄位視為未 pin（legacy／測試 fixture，#379 同型容忍）；帶欄位者必須同 era。
        and job.get("workflow_claim_key") in (None, run.claim_key)
        and (
            step.phase not in {"verify", "review"}
            or job.get("subject_head") == run.candidate_head
        )
    ]
    job = jobs[-1] if jobs else dispatch_or_stop(run, retry=retry_failed)
    if job is not None and recovery_job_id == job.get("job_id"):
        job = dispatch_or_stop(run, retry_recovery_job_id=recovery_job_id)
    elif retry_failed and job is not None and (
        _is_stale_terminalized_failed_job(job)
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
        runtime_diagnostic = job.get("runtime_diagnostic")
        if isinstance(runtime_diagnostic, dict):
            failure_reason = "runtime-contract-failed"
        sandbox_ok = True
        try:
            _discard_reviewer_sandbox(
                job,
                coordinator_root=coordinator_root,
                require_candidate_unchanged=True,
            )
        except ValueError:
            failure_reason = "reviewer-candidate-drift"
            sandbox_ok = False
        # #260 R6：失敗回報附帶唯讀 terminal 診斷（observed HEAD／job id／失敗
        # 原因），沿用 #261 的 `_terminal_parse_diagnostics`；診斷與授權分離，
        # 不得因此授予任何 candidate authority。
        diagnostics = _terminal_parse_diagnostics(job)
        # #384：不再一律壓平成 "job-failed"。`job` 已在
        # `Dispatcher._finalize_headless` 分類過（provider_outcome.py）；沙箱
        # drift 時（sandbox_ok is False）刻意不看分類、不重試——candidate 已被
        # reviewer 動過，跟 provider 是哪一種失敗無關，必須 fail closed。
        classification = (
            provider_outcome.classification_from_job(job) if sandbox_ok else None
        )
        if isinstance(runtime_diagnostic, dict):
            # A runtime contract failure is already a durable Manager-side
            # diagnosis.  It must not be reclassified as a retryable provider
            # outage or fed back into provider routing.
            classification = None
        status_fields: dict[str, object] = {}
        if classification is not None:
            status_fields["provider_outcome"] = classification.outcome.value
            status_fields["provider_outcome_authority"] = classification.authority.value
            failure_reason = f"job-failed-{classification.outcome.value}"
        if sandbox_ok and classification is not None and classification.retryable:
            retry_key = _provider_retry_attempt_key(step.card)
            attempts = dict(run.attempts)
            seen = attempts.get(retry_key, 0)
            status_fields["provider_retry_count"] = seen
            status_fields["provider_retry_limit"] = terminal_contract.MAX_PROVIDER_RETRIES
            if seen < terminal_contract.MAX_PROVIDER_RETRIES:
                rerouted_identity = _provider_failure_reroute(
                    run, step, identities, failed_job=job, classification=classification,
                )
                replacement = dispatch_workflow_card(
                    dispatcher,
                    run=run,
                    identities=identities,
                    launcher_factory=launcher_factory,
                    coordinator_root=coordinator_root,
                    retry_failed=True,
                    forced_identity=rerouted_identity,
                )
                if replacement is None:
                    return {
                        "run_id": run.run_id,
                        "current_phase": run.current_phase,
                        "reason": "not-dispatchable",
                    }
                current = registry.get_workflow_run(run.run_id)
                attempts = dict(current.attempts)
                attempts[retry_key] = seen + 1
                registry._manager_update_workflow_run(run.run_id, attempts=attempts)
                return {
                    "run_id": run.run_id,
                    "current_phase": run.current_phase,
                    "job_id": replacement["job_id"],
                    "reason": "provider-failure-retry",
                    **{**status_fields, "provider_retry_count": seen + 1},
                    "terminal_diagnostics": diagnostics.as_dict(),
                }
            failure_reason = "provider-retry-exhausted"
        updated = registry._manager_update_workflow_run(
            run.run_id,
            facets=("needs_human",),
            gate_status="running",
            needs_human_reason=diagnostic_reason(
                failure_reason,
                (
                    "isolated runtime contract failed: "
                    f"{runtime_diagnostic.get('detail', failure_reason)}"
                    if isinstance(runtime_diagnostic, dict)
                    else "builder/reviewer job 以 provider 層失敗終局，"
                    f"bounded retry 已耗盡或不可重試：{diagnostics.reason or failure_reason}"
                ),
                source=(
                    "manager._poll_workflow_job:runtime-contract"
                    if isinstance(runtime_diagnostic, dict)
                    else "manager._poll_workflow_job:provider-failure"
                ),
                run_id=run.run_id,
                work_id=run.work_id,
                job_id=str(job["job_id"]),
                card=step.card,
            ),
        )
        return {
            "run_id": run.run_id,
            "current_phase": updated.current_phase,
            "job_id": job["job_id"],
            "reason": failure_reason,
            **status_fields,
            "terminal_diagnostics": diagnostics.as_dict(),
        }
    if _retryable_nonpassing_workflow_terminal(job):
        # #717：模型**明示要求停止**（`failed`／`needs_human`），而且 envelope 形狀
        # 與綁定都合法。這既不是 schema 壞掉，也不是 provider 失敗，因此：
        #
        # - 不消耗 schema retry 額度（那份額度是給「模型寫壞 JSON」的）；
        # - 不自動回派——模型已經講清楚它要人，再派一次只是把同一句話再買一次；
        # - **模型逐字寫的 `diagnostics` 直接落進 attention 的 detail**。
        #
        # 這條分支之前，這一組 job 會一路走到 `terminalize_workflow_job` 才被
        # "workflow card terminal evidence did not pass" 擋下，operator 收到的是
        # `terminalize-workflow-job-failed` 這個離病因兩層遠的例外摘要（而且會把
        # 例外往上擲）。病因就在同一份 envelope 上，沒有理由讓它繞這一圈。
        diagnostics = _terminal_parse_diagnostics(job)
        declared_status = _declared_card_terminal_status(job)
        model_text = diagnostics.model_diagnostics_text()
        current = registry.get_workflow_run(run.run_id)
        updated = registry._manager_update_workflow_run(
            run.run_id,
            facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
            gate_status="running",
            needs_human_reason=diagnostic_reason(
                "card-terminal-explicit-stop",
                f"卡片 terminal 明示要求停止（status={declared_status}）："
                + (model_text or "模型未在 envelope 的 diagnostics 欄位寫下任何病因"),
                source="manager._poll_workflow_job:explicit-stop",
                run_id=run.run_id,
                work_id=run.work_id,
                job_id=str(job["job_id"]),
                card=step.card,
                declared_status=declared_status,
            ),
        )
        return {
            "run_id": run.run_id,
            "current_phase": updated.current_phase,
            "job_id": job["job_id"],
            "reason": "card-terminal-explicit-stop",
            "declared_status": declared_status,
            # #261 R4／D6：唯讀診斷與授權欄位分離——模型講的話同樣不授權。
            "terminal_diagnostics": diagnostics.as_dict(),
        }
    if _malformed_workflow_card_terminal(job):
        # #261 R3／D5：同一個確定性 schema mismatch 不得無限回派模型。計數持久化在
        # run.attempts（既有的可觀測欄位），逾限即停止並讓 operator 接手。
        diagnostics = _terminal_parse_diagnostics(job)
        retry_key = _schema_retry_attempt_key(step.card)
        total_key = _schema_mismatch_total_key(step.card)
        attempts = dict(run.attempts)
        seen = attempts.get(retry_key, 0)
        # #717（ii）：`seen` 是**本輪**（上一次 `retry-card` 之後）用掉的額度，
        # `observed` 是這張卡**累計**撞過幾次。兩者過去共用同一個 `(n/N)`，於是
        # 「這一輪一次都沒重試就 exhausted」被 operator 讀成「它剛剛試了兩次」。
        observed = attempts.get(total_key, 0) + seen
        status_fields = {
            "schema_retry_count": seen,
            "schema_retry_limit": terminal_contract.MAX_SCHEMA_RETRIES,
            "schema_mismatch_observed": observed,
            "last_validation_path": diagnostics.validation_path,
            "last_validation_reason": diagnostics.reason,
        }
        if seen >= terminal_contract.MAX_SCHEMA_RETRIES:
            current = registry.get_workflow_run(run.run_id)
            model_text = diagnostics.model_diagnostics_text()
            registry._manager_update_workflow_run(
                run.run_id,
                facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
                gate_status="running",
                needs_human_reason=diagnostic_reason(
                    "card-terminal-schema-retry-exhausted",
                    "同一張卡的 terminal envelope 連續 schema mismatch 已達上限"
                    f"（本輪 {seen}/{terminal_contract.MAX_SCHEMA_RETRIES}，"
                    f"該卡累計 {observed} 次）："
                    f"{diagnostics.reason or 'terminal envelope 不符契約'}"
                    # #717：模型若在 envelope 上寫了 diagnostics，逐字帶進 attention。
                    + (f"；模型 diagnostics：{model_text}" if model_text else ""),
                    source="manager._poll_workflow_job:schema-retry",
                    run_id=run.run_id,
                    work_id=run.work_id,
                    job_id=str(job["job_id"]),
                    card=step.card,
                    validation_path=diagnostics.validation_path,
                    schema_mismatch_observed=observed,
                ),
            )
            return {
                "run_id": run.run_id,
                "current_phase": run.current_phase,
                "job_id": job["job_id"],
                "reason": "card-terminal-schema-retry-exhausted",
                **status_fields,
                # #261 R4／D6：唯讀診斷與授權欄位分離；operator 看得到 observed
                # HEAD／job id／失敗原因，但 candidate 並未因此取得 authority。
                "terminal_diagnostics": diagnostics.as_dict(),
            }
        replacement = dispatch_workflow_card(
            dispatcher,
            run=run,
            identities=identities,
            launcher_factory=launcher_factory,
            coordinator_root=coordinator_root,
            retry_failed=True,
        )
        if replacement is None:
            return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": "not-dispatchable"}
        current = registry.get_workflow_run(run.run_id)
        attempts = dict(current.attempts)
        attempts[retry_key] = seen + 1
        registry._manager_update_workflow_run(run.run_id, attempts=attempts)
        return {
            "run_id": run.run_id,
            "current_phase": run.current_phase,
            "job_id": replacement["job_id"],
            "reason": "card-terminal-malformed-retry",
            **{
                **status_fields,
                "schema_retry_count": seen + 1,
                "schema_mismatch_observed": observed + 1,
            },
            "terminal_diagnostics": diagnostics.as_dict(),
        }
    try:
        job = terminalize_workflow_job(
            registry,
            job_id=str(job["job_id"]),
            coordinator_root=coordinator_root,
        )
    except Exception as exc:
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
            needs_human_reason=diagnostic_reason(
                "terminalize-workflow-job-failed",
                f"採信 terminal envelope 時擲出例外：{summarize_exception(exc)}",
                source="manager._poll_workflow_job:terminalize",
                run_id=run.run_id,
                work_id=run.work_id,
                job_id=str(job["job_id"]),
                card=step.card,
            ),
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
    except Exception as exc:
        rate_limited = _provider_rate_limit_result(
            exc,
            run_id=run.run_id,
            current_phase=run.current_phase,
            coordinator_root=coordinator_root,
        )
        if rate_limited is not None:
            return rate_limited
        current = registry.get_workflow_run(run.run_id)
        registry._manager_update_workflow_run(
            run.run_id,
            facets=tuple(dict.fromkeys((*current.facets, "needs_human"))),
            gate_status="running",
            needs_human_reason=diagnostic_reason(
                "workflow-advance-failed",
                f"卡片通過後推進 workflow 擲出例外：{summarize_exception(exc)}",
                source="manager._poll_workflow_job:advance",
                run_id=run.run_id,
                work_id=run.work_id,
                job_id=str(job["job_id"]),
                card=step.card,
                next_phase=next_phase,
            ),
        )
        raise
    updated = registry.get_workflow_run(run.run_id)
    if "needs_human" in updated.facets:
        return result
    next_job = dispatch_or_stop(updated)
    if next_job is not None:
        result["job_id"] = next_job["job_id"]
    return result


def _write_planning_failure_evidence(
    *,
    coordinator_root: Path,
    run_id: str,
    classification: str,
    reason: str,
) -> str:
    """#393：define needs_human 三條靜默失敗路徑落 `cortex-planning-failure/v1`
    evidence，供 `work_actions._read_planning_failure_record`／
    `_planning_failure_hint` 消費，讓 `recover-planning` 對 define 失敗結構性
    可用（過去全庫只有 reader、沒有任何 producer，recover-planning 對這個
    它最該覆蓋的場景永遠 fail-closed）。

    原子寫入模式與 `work_bridge._write_json_evidence` 一致（tmp、fsync、
    rename、0400）；body 直接落 reader 要求的頂層欄位（不套 envelope），
    因為 reader 直接讀 `schema`/`run_id`/`classification`/`reason`，
    `_write_json_evidence` 的 `{"payload": ..., "hash": ...}` envelope 與
    content-hash 檔名不符這裡的讀取契約，故不直接共用、改用同樣手法的
    小工廠避免跨模組奇怪依賴。目錄名必須是 `planning-recovery`——
    reader 用 `path.parent.name` 判斷。
    """

    body = {
        "schema": "cortex-planning-failure/v1",
        "run_id": run_id,
        "classification": classification,
        "reason": reason,
        "created_at": _utcnow(),
    }
    digest = verification.canonical_json_hash(body)
    directory = coordinator_root.resolve() / "evidence" / "planning-recovery"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{run_id}-{digest}.json"
    content = (
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if target.exists():
        if target.is_symlink() or target.read_bytes() != content:
            raise RuntimeError("workflow planning failure evidence conflict")
        return str(target)
    temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o400)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


def _record_planning_failure_evidence(
    run,
    *,
    coordinator_root: Path,
    classification: str,
    reason: str,
) -> tuple[str, ...]:
    """呼叫端 wrapper：evidence 寫入失敗不得讓 define 路徑爆炸（fail-open
    僅限 evidence 記錄本身），失敗時只 log 註記、`run.evidence_refs` 原樣
    帶回，needs_human 仍照舊落地——不因為記不下證據就讓整個 define
    請求跟著炸。
    """

    try:
        evidence_path = _write_planning_failure_evidence(
            coordinator_root=coordinator_root,
            run_id=run.run_id,
            classification=classification,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 - evidence 記錄本身 fail-open
        logger.error(
            "planning-failure-evidence-write-failed run_id=%s classification=%s error=%s: %s",
            run.run_id,
            classification,
            type(exc).__name__,
            str(exc)[:200],
        )
        return run.evidence_refs
    return (*run.evidence_refs, evidence_path)


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
                        run_id,
                        facets=("needs_human",),
                        gate_status="running",
                        needs_human_reason=diagnostic_reason(
                            "ship-validator-unavailable",
                            "review→ship 需要 ship validator 判定交付狀態，"
                            "但呼叫端沒有提供（無法自行採信本地 Copilot 證據）",
                            source="manager.apply_workflow_action:advance-ship",
                            run_id=run_id,
                            work_id=current.work_id,
                            card=card_id,
                        ),
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
                    delivery_reason = trusted.get("reason") or "delivery-needs-human"
                    updated = registry._manager_update_workflow_run(
                        run_id,
                        facets=("needs_human",),
                        gate_status="running",
                        needs_human_reason=diagnostic_reason(
                            "delivery-needs-human",
                            "ship validator 判定交付需要人工介入："
                            f"{delivery_reason}",
                            source="manager.apply_workflow_action:advance-ship",
                            run_id=run_id,
                            work_id=current.work_id,
                            card=card_id,
                            candidate=current.candidate_head,
                            delivery_reason=str(delivery_reason),
                        ),
                    )
                    return {
                        "run_id": updated.run_id,
                        "current_phase": updated.current_phase,
                        "reason": delivery_reason,
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
            _harvest_build_candidate(
                job, run=current, candidate=candidate, coordinator_root=coordinator_root
            )
            _record_candidate_full_suite_evidence(
                job, run=current, candidate=candidate
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
                needs_human_reason=diagnostic_reason(
                    "blocking-findings",
                    "reviewer 對 candidate 提出阻擋性 findings"
                    f"（{len(evaluation.get('findings') or ())} 條），review gate 判定 rejected",
                    source="manager.apply_workflow_action:advance-review",
                    run_id=run_id,
                    work_id=current.work_id,
                    card=card_id,
                    candidate=candidate,
                    evidence_refs=(evidence_path,) if evidence_path else (),
                ),
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
        # 診斷 invariant：下面兩條會把 run 推進 needs_human 的分支各自帶上理由，
        # 交給同一個 `_manager_update_workflow_run` 呼叫寫入（見結尾）。
        facets_reason = None
        if current.current_phase == "review" and phase_done and next_phase == "ship":
            if ship_validator is None:
                next_phase = "review"
                facets = ("needs_human",)
                gate_status = "running"
                facets_reason = diagnostic_reason(
                    "ship-validator-unavailable",
                    "review 全數通過但呼叫端未提供 ship validator，"
                    "無法判定交付狀態（本地 Copilot 證據一律不採信）",
                    source="manager.apply_workflow_action:advance-phase",
                    run_id=run_id,
                    work_id=current.work_id,
                    card=card_id,
                )
            else:
                # The review evidence is staged in ``by_kind`` above but is
                # not persisted until the phase transition succeeds.  The
                # production ship validator nevertheless needs to inspect the
                # canonical foreign-review ref while deciding whether the
                # transition is admissible.  Give it the exact prospective
                # run view; keep the durable update below atomic and manager-
                # owned.
                validation_run = replace(
                    current,
                    gate_refs=tuple(
                        by_kind[kind]
                        for kind in (
                            "brainstorm", "foreign-review", "copilot", "maintainer-review"
                        )
                        if kind in by_kind
                    ),
                )
                status, trusted = validate_ship_result(
                    ship_validator(run=validation_run, candidate=candidate),
                    candidate=candidate,
                )
                if status in {"pending", "needs_human"}:
                    persisted = registry.get_workflow_run(run_id)
                    if (
                        persisted.current_phase != current.current_phase
                        or persisted.candidate_head != current.candidate_head
                    ):
                        return {
                            "run_id": persisted.run_id,
                            "current_phase": persisted.current_phase,
                            "reason": trusted.get("reason")
                            or ("delivery-in-progress" if status == "pending" else "delivery-needs-human"),
                        }
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
                    facets_reason = diagnostic_reason(
                        "delivery-needs-human",
                        "ship validator 判定交付需要人工介入："
                        f"{trusted.get('reason') or 'delivery-needs-human'}",
                        source="manager.apply_workflow_action:advance-phase",
                        run_id=run_id,
                        work_id=current.work_id,
                        card=card_id,
                        candidate=candidate,
                    )
        updated = registry._manager_update_workflow_run(
            run_id,
            current_phase=next_phase,
            needs_human_reason=facets_reason,
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
        if current.current_phase == "build":
            # #658：這張卡的採信此刻已經 durable（上面那次 `_manager_update_workflow_run`
            # 已落盤），它的工作區從此沒有任何下游消費端 ⇒ 即時回收。
            #
            # **位置刻意在狀態更新之後**：若掛在 `_harvest_build_candidate()` 旁邊，
            # 中間任何一條 raise（宣告了 outputs 卻沒有 artifact、`_audit_phase_steps`、
            # phase transition 驗證……）都會讓 run 停在「工作區已刪、卡仍 pending」，
            # 下一個 tick 的 `_verify_exact_candidate()` 會以 `git -C <已刪的路徑>`
            # 失敗收場——一條由清理製造出來的死路。狀態落盤之後再收，重入的是
            # `workflow card evidence replay rejected`（已採信的卡不再讀工作區）。
            _reclaim_trusted_build_workspace(job, run=updated, candidate=str(candidate))
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
    # v4 R1（方案 A）coverage validator **shadow**：在 production topology validator
    # 呼叫點旁並行跑 coverage validator 並記 telemetry。**零行為變更**——
    # run_coverage_shadow 全程 try/except、永不 raise，且受
    # PSC_RESPONSIBILITY_COVERAGE 閘控（off 連比對都不跑）；下面的
    # validate_manager_spine() 仍是唯一 production 真相源。coverage validator 的判定
    # 只進 telemetry，絕不影響此處 gate。放在 validate 之前，是為了連 topology 即將
    # raise 的案例也能被 shadow 觀測到（disagreement 資料才完整）。
    coverage.run_coverage_shadow(
        manifest,
        callsite="manager.start",
        context={
            "work_id": args.get("work_id"),
            "repo": args.get("repo"),
            "claim_key": args.get("claim_key"),
            "combo": manifest.combo,
        },
    )
    manifest.validate_manager_spine()
    # #208 收口 wiring 1：work_bridge.start_canonical_workflow 在呼叫端已算好
    # （fail-soft，可能是 None）的 claim-time sizing 快照透過 args 帶進來，這裡
    # 只負責原樣轉交給 _manager_create_workflow_run；不在 manager.py 重算。
    start_sizing_score = args.get("sizing_score")
    start_sizing_band = args.get("sizing_band")
    start_combo_selection = args.get("combo_selection")
    # #205 R1/R2：run-scoped 模型鏈覆寫在 claim 當下（本次 `_manager_create_
    # workflow_run` 呼叫，若同 claim_key 已存在 ongoing run 則此呼叫是
    # no-op，覆寫沿用既有 run，見 registry.py 的 idempotent 短路）一併凍結。
    start_model_chain_override = args.get("model_chain_override")
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
        sizing_score=start_sizing_score,
        sizing_band=start_sizing_band,
        model_chain_override=start_model_chain_override,
        combo_selection=start_combo_selection,
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
    except PlanningPublicationDrift as exc:
        run = registry._manager_update_workflow_run(
            run.run_id,
            facets=("needs_human",),
            gate_status="running",
            needs_human_reason=diagnostic_reason(
                "planning-publication-drift",
                f"start 前 reconcile planning 發佈事務偵測到 drift：{str(exc)[:200]}",
                source="manager.apply_workflow_action:start",
                run_id=run.run_id,
                work_id=run.work_id,
                artifact_root=str(artifact_root),
            ),
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
                # #507：planning launcher 偵測到 operator worktree drift 時
                # 不再整棵還原，改為把受影響檔案完整備份進 run-scoped evidence
                # 並落一份結構化 diff 報告；這裡把 evidence 根與 run_id 交給
                # runtime，operator 才能用同一組 run_id 同時撈
                # `planning-recovery`、`planning-artifacts` 與
                # `planning-worktree-drift` 三份 evidence。
                evidence_root=transaction_root,
                run_id=run.run_id,
            )
        except Exception as exc:
            # #393：recover-planning 靠 `cortex-planning-failure/v1` evidence
            # 判定能不能恢復——過去全庫只有 reader（work_actions.
            # _read_planning_failure_record）、這三條 needs_human 路徑沒有任何
            # 一條寫 evidence，導致 recover-planning 對 define 靜默失敗結構性
            # 不可用。這裡歸類 `environment`（runtime 初始化本身出問題，非
            # 內容缺陷），reason 沿用 #392 已組好的字串再併上例外摘要。
            failure_reason = (
                f"planning-runtime-initialization-failed: {type(exc).__name__}: {str(exc)[:200]}"
            )
            evidence_refs = _record_planning_failure_evidence(
                run,
                coordinator_root=transaction_root,
                classification="environment",
                reason=failure_reason,
            )
            run = registry._manager_update_workflow_run(
                run.run_id,
                facets=("needs_human",),
                brainstorm_required=True,
                evidence_refs=evidence_refs,
                needs_human_reason=diagnostic_reason(
                    "planning-runtime-initialization-failed",
                    f"planning runtime 初始化失敗：{summarize_exception(exc)}",
                    source="manager.apply_workflow_action:start-planning-runtime",
                    evidence_refs=evidence_refs,
                    run_id=run.run_id,
                    work_id=run.work_id,
                    classification="environment",
                ),
            )
            # #391：needs_human 的 reason 過去只塞進回傳值——daemon periodic
            # tick 觸發時（未經活人在旁的 request/response）沒人消費回傳值，
            # reason 隨呼叫堆疊蒸發，run row 只留下一個查不出原因的
            # needs_human facet。這裡結構化落一筆 log（run_id／reason／底層
            # exception 型別與訊息前 200 字），至少留下可追查的軌跡；exception
            # 本身仍整段吞掉不重拋，維持既有 fail-soft 行為不變。
            logger.error(
                "planning-runtime-initialization-failed run_id=%s reason=%s error=%s: %s",
                run.run_id,
                "planning-runtime-initialization-failed",
                type(exc).__name__,
                str(exc)[:200],
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
        # #393：同上一條路徑，補 evidence 才能讓 recover-planning 對這條
        # needs_human 出口可用；沒有底層 exception 可附，classification 仍歸
        # `environment`（runtime 元件缺失，非內容缺陷）。
        evidence_refs = _record_planning_failure_evidence(
            run,
            coordinator_root=transaction_root,
            classification="environment",
            reason="planning-runtime-unavailable",
        )
        run = registry._manager_update_workflow_run(
            run.run_id,
            facets=("needs_human",),
            brainstorm_required=True,
            evidence_refs=evidence_refs,
            needs_human_reason=diagnostic_reason(
                "planning-runtime-unavailable",
                "planning runtime 三個角色（primary_questioner／secondary_planner／"
                "primary_integrator）未齊備，brainstorm 無法啟動",
                source="manager.apply_workflow_action:start-planning-runtime",
                evidence_refs=evidence_refs,
                run_id=run.run_id,
                work_id=run.work_id,
                classification="environment",
            ),
        )
        # #391：runtime_factory 沒被呼叫（或呼叫成功但沒補齊三個角色）時同樣
        # 落一筆 log，理由同上——reason 不能只活在回傳值裡。
        logger.error(
            "planning-runtime-unavailable run_id=%s reason=%s",
            run.run_id,
            "planning-runtime-unavailable",
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
            # #511：拒收 evidence 必須落在 coordinator_root，不能落在
            # `artifact_root`——後者是被 cortex daemon 監控的 operator worktree，
            # planning 失敗時會被整棵樹抹除再從 baseline 還原（#507），診斷會跟著
            # 一起消失。與 `_record_planning_failure_evidence` 用同一個 root。
            coordinator_root=transaction_root,
        ),
        evidence_writer=publication.write_evidence,
        # #515：post-integration 檢查判定 artifact 不合驗收條件時，被拒內容會在
        # 緊接著的 `rollback_publication()` 被撤下——不先存一份，operator 就再也
        # 看不到 planner 到底寫了什麼（#511 的同一個教訓）。沿用 #513 已建立的
        # `cortex-planning-artifact-rejection/v1` evidence 落檔，不另創格式。
        rejection_recorder=lambda assessment: _record_planning_artifact_rejection_evidence(
            coordinator_root=transaction_root,
            run_id=run.run_id,
            work_id=run.work_id,
            assessment=assessment,
        ),
        # #535：brainstorm evidence 的 content-addressed 命名納入 run identity，
        # 前一世代（已 abandon）的殘留檔才不會佔住新世代的落點。前代檔案原位
        # 保留、原路徑仍可稽核——evidence 不搬不刪。
        run_id=run.run_id,
    )
    if result.state != "ready" or result.gate_refs.brainstorm_peer is None:
        publication.rollback()
        # #393：brainstorm 未收斂預設歸內容缺陷（非 runtime 環境問題），
        # classification 落 `content`——`_read_planning_failure_record` 仍接受
        # 此值，但 `_resume_decision` 對 `content` 一律不浮現 recover-planning
        # （見 claim.py 的 fail-closed 判準），行為與 issue 393 的 fail-closed
        # 意圖一致。`result.reason` 理論上不應為空，仍防禦性 fallback 避免
        # evidence 的 reason 欄位落空字串。
        # #416：例外一——reason 命中 `_is_planning_authority_residue_failure`
        # 判準時（abandon 未回滾的發佈殘留撞見 `_publish_planning_artifacts`
        # 的 authority fail-closed），這其實是環境／狀態殘留而非模型內容
        # 缺陷，改歸 `environment`，讓 `_resume_decision` 可以浮現
        # recover-planning，不必再燒一個世代改名重識別才能繞過。
        # 例外二——`_is_planning_transient_service_failure`：launcher/service 層
        # 的暫時性錯誤（503/限流/逾時；agy 實測會印錯誤文字但 exit 0），同歸
        # `environment`。一個幾分鐘後自癒的服務錯誤不得被判成 `content` 死路。
        # 例外三（#554）——`_is_planning_worktree_drift_failure`：operator
        # worktree 在 planning 視窗內被動過。#543 之後 drift 不再銷毀任何資料
        # （只備份與報告），語意上就是環境事件，同歸 `environment`。
        brainstorm_not_ready_reason = result.reason or "brainstorm-not-ready"
        # #554／PR #560：分類收斂進具名的 `_classify_planning_failure`（reason →
        # classification 的單一可測入口，含 worktree drift 的 environment 例外）。
        # 這裡把它 hoist 成區域變數，好讓 evidence 與 needs_human_reason 兩者
        # 引用**同一個**判定結果，不各算一次。
        brainstorm_classification = _classify_planning_failure(brainstorm_not_ready_reason)
        brainstorm_next_step_hint = needs_human_next_step_hint(
            phase=run.current_phase,
            planning_failure_classification=brainstorm_classification,
            work_id=run.work_id,
            repo=run.repo,
            run_id=run.run_id,
        )
        brainstorm_evidence_refs = _record_planning_failure_evidence(
            run,
            coordinator_root=transaction_root,
            classification=brainstorm_classification,
            reason=brainstorm_not_ready_reason,
        )
        run = registry._manager_update_workflow_run(
            run.run_id,
            facets=("needs_human",),
            brainstorm_required=True,
            evidence_refs=brainstorm_evidence_refs,
            # #515／#511：`result.reason` 現在帶得動實際的拒收原因（哪一個
            # artifact、哪一條驗收條件、還是環境類的路徑／編碼問題），不再是
            # 一個籠統的 `primary-artifact-invalid`。這裡把它原樣落進 run，
            # operator 不必再從 `~/.agents/log` 反推。
            needs_human_reason=diagnostic_reason(
                "brainstorm-not-ready",
                f"brainstorm 未收斂（state={result.state}）：{brainstorm_not_ready_reason}",
                source="manager.apply_workflow_action:start-brainstorm",
                evidence_refs=brainstorm_evidence_refs,
                next_step_hint=brainstorm_next_step_hint,
                run_id=run.run_id,
                work_id=run.work_id,
                classification=brainstorm_classification,
            ),
        )
        # #391：run_heterogeneous_brainstorm 沒能收斂到 ready 狀態時，同樣的
        # reason-只活在回傳值裡問題——比照上面兩條 runtime 缺失路徑補一筆 log。
        logger.error(
            "brainstorm-not-ready run_id=%s state=%s reason=%s",
            run.run_id,
            result.state,
            result.reason,
        )
        return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": result.reason}
    # #536：檔案側到此為止全部落地且已 fsync，接下來 registry 的那一次原子
    # 寫入就是本事務唯一的 commit point。把這條邊界寫成 durable 事實，崩潰
    # 後的 sweep（`reconcile_planning_transactions`）才能誠實區分「崩在發佈
    # 中途」與「崩在提交邊界」，而不是只看到一坨無主檔案。
    publication.prepare_commit()
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
    from .work_bridge import extract_model_chain_override, start_canonical_workflow

    active_registry = registry or JobRegistry()
    state_path = getattr(active_registry, "_state_path", None)
    coordinator_root = (
        Path(state_path).resolve().parent if state_path is not None else paths.coordinator_root().resolve()
    )
    # #205 R1：operator 在 `cortex run work start/resume/...` 帶入的 run-scoped
    # 模型鏈覆寫語法層抽取；是否合法留給 dispatch 時 fail closed（D4）。
    work_action_model_chain_override = extract_model_chain_override(args)
    # combo 只在 start／intake action 有效（issue #203：intake 內部等價於
    # start）——resume 等動作即使夾帶 combo（不論來自上游疏漏或 --payload
    # 繞過）也不得轉交 start_canonical_workflow，避免已在 define phase 之外
    # resume 時被未預期的 combo override 影響（contract.validate_request 已
    # fail-closed 擋掉此請求，這裡是 manager 層的縱深防禦，行為不依賴上游
    # 有沒有先擋下）。
    work_action_combo_override = (
        args.get("combo") if args.get("action") in {"start", "intake"} else None
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
            model_chain_override=work_action_model_chain_override,
            combo_override=work_action_combo_override,
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
