from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from paulsha_cortex.config import paths
from . import terminal_contract, verification
from .claim import claim_key_for_authority_digest
from .diagnostics import (
    DiagnosticInvariantError,
    DiagnosticReason,
    coerce_diagnostic_reason,
    diagnostic_reason,
)
from .usage_extractors import extract_usage
from .workflow import (
    GateEvidenceRef,
    PlanningArtifactAuthority,
    WorkflowRun,
    WorkflowStep,
    validate_workflow_phase_transition,
)

COORDINATOR_STATE_SCHEMA_VERSION = 2

VALID_JOB_STATUSES = frozenset({"dispatched", "running", "exited", "failed"})
ACTIVE_JOB_STATUSES = frozenset({"dispatched", "running"})
TERMINAL_JOB_STATUSES = frozenset({"exited", "failed"})

# #545／#569：`retry-card` 受理的 phase 與該 phase 唯一合法的重派 persona。
# build → builder（#545 的中段 builder 卡），verify／review → reviewer（#569 的
# verification／code-review／adversarial-review 卡）。以 mapping 而非兩份散落的
# 條件式表述，是為了讓 work action 層與 registry 層的 admission 共用同一份判準
# ——兩邊漂移正是 #382 付過學費的教訓。
RETRY_CARD_PHASE_PERSONA = {
    "build": "builder",
    "verify": "reviewer",
    "review": "reviewer",
}

# #555：同一張卡最多接受三次 operator 明示 retry-card 重派；此計數跨重派世代保留。
MAX_RETRY_CARD_REDISPATCHES = 3


def _retry_card_attempt_key(card_id: str) -> str:
    """同一張卡跨 retry-card 世代累計的 operator 重派次數。"""

    return f"retry-card:{card_id}"


def _retry_card_redispatch_count(
    attempts: Mapping[str, int], *, matching_job_count: int, card_id: str
) -> int:
    """只計 operator 明示 retry-card 的持久計數。

    不以卡片 job 數回推：同卡的 schema 自動重派也會產生 job，回推會把自動重派
    誤算成 operator 重派而提早耗盡額度。舊 run 沒有這個鍵時從 0 起算。
    """

    del matching_job_count
    return attempts.get(_retry_card_attempt_key(card_id), 0)


def _retry_card_budget_message(card_id: str, count: int) -> str:
    return (
        f"retry-card per-card limit reached for card {card_id} "
        f"({count}/{MAX_RETRY_CARD_REDISPATCHES}); use abandon or retry-build when eligible"
    )


def _retry_card_budget_reason(
    card_id: str, count: int, *, source: str
) -> DiagnosticReason:
    return diagnostic_reason(
        "retry-card-budget-exhausted",
        f"卡片 {card_id} 的 retry-card 重派已達上限（{count}/{MAX_RETRY_CARD_REDISPATCHES}）。",
        source=source,
        next_step_hint=(
            "請執行 abandon；若 retry-build 的前置條件成立，可先修復 Candidate 後重派。"
        ),
        card=card_id,
        redispatch_count=count,
        retry_limit=MAX_RETRY_CARD_REDISPATCHES,
    )

VALID_SLICE_STATES = frozenset(
    {
        "pending",
        "building",
        "dispatched",
        "running",
        "exited",
        "reviewing",
        "verified",
        "completed",
        "needs_human",
        "failed",
        "superseded",
    }
)
VALID_GATE_STATES = frozenset({"pending", "passed", "failed", "needs_human"})

JOB_STATUS_TRANSITIONS = {
    "dispatched": frozenset({"dispatched", "running", "exited", "failed"}),
    "running": frozenset({"running", "exited", "failed"}),
    "exited": frozenset({"exited"}),
    "failed": frozenset({"failed"}),
}
SLICE_STATE_TRANSITIONS = {
    "pending": frozenset({"pending", "building", "dispatched", "running", "needs_human", "failed"}),
    "building": frozenset({"building", "needs_human", "failed", "reviewing", "verified", "completed", "exited"}),
    "dispatched": frozenset({"dispatched", "running", "exited", "failed", "needs_human"}),
    "running": frozenset({"running", "exited", "failed"}),
    "exited": frozenset({"exited"}),
    "reviewing": frozenset({"reviewing", "needs_human", "verified", "failed"}),
    "verified": frozenset({"verified", "completed", "needs_human"}),
    "completed": frozenset({"completed"}),
    "needs_human": frozenset({"needs_human", "pending", "building", "reviewing", "verified", "failed", "completed", "superseded"}),
    # "building" 併入 failed 的合法離開路徑（#382）：repin_slice() 刻意保留
    # slice state（同 needs_human 的既有行為，見
    # test_repin_slice_preserves_needs_human_until_explicit_retry_transition），
    # 只重置 gate_state；retry-build 的實際派工路徑
    # （autonomy.dispatch_ready -> _mark_slice_building）緊接著會把 state 從
    # repin 前的原值直接轉成 "building"。needs_human 早就允許這條直接跳轉，
    # failed 原本沒有，導致 repin 成功後下一步 _mark_slice_building 仍會
    # raise，retry-build 整條路徑還是走不完。
    "failed": frozenset({"failed", "pending", "needs_human", "building", "superseded"}),
    "superseded": frozenset({"superseded"}),
}
GATE_STATE_TRANSITIONS = {
    "pending": frozenset({"pending", "passed", "failed", "needs_human"}),
    "passed": frozenset({"passed"}),
    # "failed" -> "pending" 對齊 SLICE_STATE_TRANSITIONS["failed"]（同樣允許
    # failed -> pending）：兩張表原本不對稱是 #382 死鎖根因之一——slice state
    # 可以復原、gate_state 卻永久卡死，讓 repin_slice()／retry-build 對
    # failed/failed slice 保證失敗。
    "failed": frozenset({"failed", "needs_human", "pending"}),
    "needs_human": frozenset({"needs_human", "pending", "passed", "failed"}),
}

# repin_slice() 接受重派的 slice state 集合。"failed" 併入此集合（#382）：
# SLICE_STATE_TRANSITIONS 早就允許 failed -> pending，但 repin_slice() 自己
# 這道硬編閘門沒跟上，導致宣告的 retry-build 對 failed slice 保證被拒。
# 仍然刻意排除進行中／終局狀態（building/dispatched/running/exited/
# reviewing/verified/completed）——repin 不該蓋過一個 active 中的 job。
REPINNABLE_SLICE_STATES = frozenset({"pending", "needs_human", "failed"})


def slice_repin_eligible(slice_row: dict[str, Any]) -> bool:
    """判斷 `repin_slice()` 目前是否會接受這個 slice 的 `(state, gate_state)`。

    `repin_slice()` 與 `manager.allowed_slice_actions()` 共用同一套判準
    （同一份 REPINNABLE_SLICE_STATES／GATE_STATE_TRANSITIONS），確保
    `allowed_slice_actions()` 宣告的 `retry-build` 一定是 `repin_slice()`
    真的會接受的動作，不會宣告一個保證失敗的操作（#382）。
    """
    if str(slice_row.get("state")) not in REPINNABLE_SLICE_STATES:
        return False
    gate_state = str(slice_row.get("gate_state"))
    return "pending" in GATE_STATE_TRANSITIONS.get(gate_state, frozenset())


# StageExecutionKey 涵蓋的內容定址欄位（#214）：repo/work_id/card/phase/executor/
# model/base_sha/candidate_sha/frozen_input_hashes/action/test_policy 任一改變都必須
# 產生不同 key，讓 authority／candidate／model 任一變更精準 invalidate reuse 判定。
STAGE_EXECUTION_KEY_STRING_FIELDS = (
    "repo",
    "work_id",
    "card",
    "phase",
    "executor",
    "model",
    "base_sha",
    "candidate_sha",
    "action",
    "test_policy",
)


def compute_stage_execution_key(
    *,
    repo: str,
    work_id: str,
    card: str,
    phase: str,
    executor: str,
    model: str,
    base_sha: str,
    candidate_sha: str,
    frozen_input_hashes: tuple[str, ...] | list[str],
    action: str,
    test_policy: str,
) -> str:
    """把 stage 執行的內容定址欄位收斂成單一雜湊 key（建立在既有 phase 級
    checkpoint／claim key 之上，只是把顆粒度從 phase 降到 stage）。

    涵蓋 repo/work_id/card/phase/executor/model/base_sha/candidate_sha/
    frozen_input_hashes/action/test_policy；任一欄位變更都會產生不同的
    64-hex key，讓 authority／candidate／model 任一變更即精準 invalidate
    既有 reuse 判定，不需要額外的比對邏輯。
    """
    values = {
        "repo": repo,
        "work_id": work_id,
        "card": card,
        "phase": phase,
        "executor": executor,
        "model": model,
        "base_sha": base_sha,
        "candidate_sha": candidate_sha,
        "action": action,
        "test_policy": test_policy,
    }
    for field_name in STAGE_EXECUTION_KEY_STRING_FIELDS:
        value = values[field_name]
        if not isinstance(value, str) or not value:
            raise ValueError(f"stage execution key {field_name} 必須為非空字串")
    if not isinstance(frozen_input_hashes, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in frozen_input_hashes
    ):
        raise ValueError("stage execution key frozen_input_hashes 必須為非空字串的 list/tuple")
    payload = dict(values)
    payload["frozen_input_hashes"] = sorted(frozen_input_hashes)
    return verification.canonical_json_hash(payload)


def _default_state_path() -> Path:
    return paths.coordinator_root() / "jobs.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_ref_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _copy_ref_list(value: list[str]) -> list[str]:
    return [str(item) for item in value]


def _copy_json_object(value: dict[str, Any]) -> dict[str, Any]:
    copied = dict(value)
    for key, nested in value.items():
        if _is_ref_list(nested):
            copied[key] = _copy_ref_list(nested)
    return copied


def _copy_json_list(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_copy_json_object(item) for item in value]


def _deepcopy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


_MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
_SLICE_BINDING_VERSION = "cortex/slice-binding/v1"
_RECOVERY_REQUEST_VERSION = "cortex/recovery-registry-request/v1"
_RECOVERY_RECEIPT_VERSION = "cortex/recovery-registry-receipt/v1"
_JOB_SUPERSESSION_VERSION = "cortex/job-supersession/v1"
_JOB_CONSUMPTION_VERSION = "cortex/job-consumption/v1"
_CHECKPOINT_REQUEST_VERSION = "cortex/legacy-binding-checkpoint-request/v1"
_CHECKPOINT_SNAPSHOT_VERSION = "cortex/legacy-binding-snapshot/v1"
_CHECKPOINT_RECEIPT_VERSION = "cortex/legacy-binding-checkpoint-receipt/v1"
_BOUND_BINDING_FIELD = "bound_binding"
_RECOVERY_REQUIRED_STEP_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_RECOVERY_REQUEST_FIELDS = frozenset({"version", "request_id", "payload", "request_digest"})
_RECOVERY_REQUEST_PAYLOAD_FIELDS = frozenset(
    {
        "request_type",
        "action",
        "target",
        "expected_binding",
        "required_steps",
        "actor",
        "requested_by",
        "created_at",
    }
)
_RECOVERY_TARGET_FIELDS = frozenset({"repo", "work_id", "slice_id"})
_SLICE_BINDING_FIELDS = frozenset(
    {
        "binding_version",
        "binding_revision",
        "builder_job_id",
        "reviewer_job_id",
        "candidate",
        "state",
        "gate_state",
        "spec",
        "plan",
        "verification_hash",
        "target_branch",
        "target_remote",
        "dispatch_base",
    }
)
_LEGACY_BINDING_FIELDS = frozenset(field for field in _SLICE_BINDING_FIELDS if not field.startswith("binding_"))
_RECOVERY_RECEIPT_FIELDS = frozenset(
    {
        "version",
        "request_id",
        "request_digest",
        "payload",
        "phase",
        "prepared_at",
        "completed_at",
        "applied_binding",
        "affected_job_ids",
        "required_steps",
        "step_receipts",
        "result",
    }
)
_RECOVERY_STEP_RECEIPT_FIELDS = frozenset({"step_id", "ref", "sha256"})
_JOB_SUPERSESSION_FIELDS = frozenset(
    {
        "version",
        "job_id",
        "slice_id",
        "binding_revision",
        "actor",
        "at",
        "reason",
        "superseding_identity",
        _BOUND_BINDING_FIELD,
    }
)
_JOB_SUPERSESSION_REQUIRED_FIELDS = _JOB_SUPERSESSION_FIELDS - {_BOUND_BINDING_FIELD}
_JOB_CONSUMPTION_FIELDS = frozenset(
    {
        "version",
        "job_id",
        "slice_id",
        "binding_revision",
        "actor",
        "at",
        "completion_identity",
        "proof_refs",
        _BOUND_BINDING_FIELD,
    }
)
_JOB_CONSUMPTION_REQUIRED_FIELDS = _JOB_CONSUMPTION_FIELDS - {_BOUND_BINDING_FIELD}
_CHECKPOINT_REQUEST_FIELDS = frozenset({"version", "request_id", "payload", "request_digest"})
_CHECKPOINT_REQUEST_PAYLOAD_FIELDS = frozenset(
    {
        "operation",
        "target",
        "expected_legacy_row",
        "expected_legacy_binding",
        "expected_job_refs",
        "legacy_snapshot_fingerprint",
        "actor",
        "requested_by",
        "created_at",
        "provenance",
    }
)
_CHECKPOINT_JOB_REF_FIELDS = frozenset({"builder_job_id", "reviewer_job_id"})
_CHECKPOINT_PROVENANCE_FIELDS = frozenset(
    {"owner_action_ref", "observed_at", "authority_ref", "proof_refs"}
)
_PROOF_REF_FIELDS = frozenset({"ref", "sha256"})
_REQUEST_IDENTITY_FIELDS = frozenset({"request_id", "request_digest"})
_BINDING_IDENTITY_FIELDS = frozenset(
    {"binding_version", "binding_revision", "builder_job_id", "reviewer_job_id", "candidate"}
)
_CHECKPOINT_RECEIPT_FIELDS = frozenset(
    {
        "version",
        "request_id",
        "request_digest",
        "payload",
        "phase",
        "checkpointed_at",
        "applied_binding",
        "provenance",
        "result",
    }
)


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _prefixed_canonical_json_digest(prefix: str, payload: Any) -> str:
    return hashlib.sha256(prefix.encode("utf-8") + b"\n" + _canonical_json_bytes(payload)).hexdigest()


def _contract_error(
    code: str,
    *,
    state_path: Path,
    detail: str | None = None,
) -> ValueError:
    suffix = f": {detail}" if detail else ""
    return ValueError(f"{code}{suffix}（fail-closed）: {state_path}")


def _same_disposition_except_at(existing: object, proposed: Mapping[str, Any]) -> bool:
    if not isinstance(existing, Mapping) or "at" not in existing or "at" not in proposed:
        return False
    return (
        {key: value for key, value in existing.items() if key != "at"}
        == {key: value for key, value in proposed.items() if key != "at"}
    )


def _require_non_empty_string(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> str:
    if not isinstance(value, str) or not value:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label) from exc
    return value


def _normalize_owner_identity(
    value: Mapping[str, str] | None,
    *,
    state_path: Path,
) -> dict[str, str] | None:
    """驗證持久 repo／Work Item／slice 身分，不推測或回填 legacy row。"""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"repo", "work_id", "slice_id"}:
        raise _contract_error("malformed-owner-identity", state_path=state_path)
    repo = _require_non_empty_string(value.get("repo"), label="owner_identity.repo", state_path=state_path)
    work_id = _require_non_empty_string(value.get("work_id"), label="owner_identity.work_id", state_path=state_path)
    slice_id = _require_non_empty_string(value.get("slice_id"), label="owner_identity.slice_id", state_path=state_path)
    repo_parts = repo.split("/")
    if len(repo_parts) != 2 or any(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) is None for part in repo_parts
    ):
        raise _contract_error("malformed-owner-identity", state_path=state_path, detail="repo")
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None:
        raise _contract_error("malformed-owner-identity", state_path=state_path, detail="work_id")
    return {"repo": repo, "work_id": work_id, "slice_id": slice_id}


def _require_exact_dict_keys(
    value: object,
    *,
    expected: frozenset[str],
    label: str,
    state_path: Path,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    normalized = dict(value)
    if set(normalized) != expected:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    return normalized


def _require_dict_keys_with_optional(
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str],
    label: str,
    state_path: Path,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    normalized = dict(value)
    keys = set(normalized)
    if not required.issubset(keys) or keys - required - optional:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    return normalized


def _require_optional_string(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> str | None:
    if value is None:
        return None
    return _require_non_empty_string(value, label=label, state_path=state_path)


def _require_sha256_hex(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> str:
    normalized = _require_non_empty_string(value, label=label, state_path=state_path)
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    return normalized


def _require_safe_integer(
    value: object,
    *,
    label: str,
    state_path: Path,
    minimum: int = 1,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > _MAX_SAFE_JSON_INTEGER:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    return value


def _validate_json_tree(
    value: Any,
    *,
    allow_bool: bool,
    allow_float: bool,
    label: str,
    state_path: Path,
    _stack: set[int] | None = None,
) -> Any:
    stack = set() if _stack is None else _stack
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label) from exc
        return value
    if isinstance(value, bool):
        if not allow_bool:
            raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not allow_float or not math.isfinite(value):
            raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
        return value
    if isinstance(value, list):
        object_id = id(value)
        if object_id in stack:
            raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
        stack.add(object_id)
        try:
            return [
                _validate_json_tree(
                    item,
                    allow_bool=allow_bool,
                    allow_float=allow_float,
                    label=label,
                    state_path=state_path,
                    _stack=stack,
                )
                for item in value
            ]
        finally:
            stack.remove(object_id)
    if isinstance(value, Mapping):
        object_id = id(value)
        if object_id in stack:
            raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
        stack.add(object_id)
        normalized: dict[str, Any] = {}
        try:
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label) from exc
                normalized[key] = _validate_json_tree(
                    nested,
                    allow_bool=allow_bool,
                    allow_float=allow_float,
                    label=label,
                    state_path=state_path,
                    _stack=stack,
                )
            return normalized
        finally:
            stack.remove(object_id)
    raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)


def _validate_spec_meta(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> dict[str, str]:
    meta = _require_exact_dict_keys(
        value,
        expected=frozenset({"path", "hash"}),
        label=label,
        state_path=state_path,
    )
    return {
        "path": _require_non_empty_string(meta.get("path"), label=f"{label}.path", state_path=state_path),
        "hash": _require_non_empty_string(meta.get("hash"), label=f"{label}.hash", state_path=state_path),
    }


def _validate_job_ref(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> str | None:
    return _require_optional_string(value, label=label, state_path=state_path)


def _validate_candidate(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> str | None:
    return _require_optional_string(value, label=label, state_path=state_path)


def _validate_target(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> dict[str, str | None]:
    target = _require_exact_dict_keys(
        value,
        expected=_RECOVERY_TARGET_FIELDS,
        label=label,
        state_path=state_path,
    )
    return {
        "repo": _require_non_empty_string(target.get("repo"), label=f"{label}.repo", state_path=state_path),
        "work_id": _require_optional_string(
            target.get("work_id"),
            label=f"{label}.work_id",
            state_path=state_path,
        ),
        "slice_id": _require_non_empty_string(
            target.get("slice_id"),
            label=f"{label}.slice_id",
            state_path=state_path,
        ),
    }


def _validate_required_steps(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> list[str]:
    if not isinstance(value, list) or not value:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    normalized = [
        _require_non_empty_string(item, label=f"{label}[]", state_path=state_path)
        for item in value
    ]
    if any(_RECOVERY_REQUIRED_STEP_RE.fullmatch(item) is None for item in normalized):
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    if normalized != sorted(normalized) or len(set(normalized)) != len(normalized):
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    return normalized


def _validate_proof_refs(
    value: object,
    *,
    label: str,
    state_path: Path,
    required: bool,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or (required and not value):
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for item in value:
        proof = _require_exact_dict_keys(
            item,
            expected=_PROOF_REF_FIELDS,
            label=f"{label}[]",
            state_path=state_path,
        )
        ref = _require_non_empty_string(proof.get("ref"), label=f"{label}[].ref", state_path=state_path)
        sha256 = _require_sha256_hex(
            proof.get("sha256"),
            label=f"{label}[].sha256",
            state_path=state_path,
        )
        if ref in seen:
            raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
        seen.add(ref)
        normalized.append({"ref": ref, "sha256": sha256})
    return normalized


def _validate_disposition_identity(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    normalized = dict(value)
    keys = frozenset(normalized)
    if keys == _REQUEST_IDENTITY_FIELDS:
        return {
            "request_id": _require_non_empty_string(
                normalized.get("request_id"),
                label=f"{label}.request_id",
                state_path=state_path,
            ),
            "request_digest": _require_sha256_hex(
                normalized.get("request_digest"),
                label=f"{label}.request_digest",
                state_path=state_path,
            ),
        }
    if keys == _BINDING_IDENTITY_FIELDS:
        binding_version = _require_non_empty_string(
            normalized.get("binding_version"),
            label=f"{label}.binding_version",
            state_path=state_path,
        )
        if binding_version != _SLICE_BINDING_VERSION:
            raise _contract_error("unsupported-recovery-version", state_path=state_path, detail=label)
        return {
            "binding_version": binding_version,
            "binding_revision": _require_safe_integer(
                normalized.get("binding_revision"),
                label=f"{label}.binding_revision",
                state_path=state_path,
            ),
            "builder_job_id": _validate_job_ref(
                normalized.get("builder_job_id"),
                label=f"{label}.builder_job_id",
                state_path=state_path,
            ),
            "reviewer_job_id": _validate_job_ref(
                normalized.get("reviewer_job_id"),
                label=f"{label}.reviewer_job_id",
                state_path=state_path,
            ),
            "candidate": _validate_candidate(
                normalized.get("candidate"),
                label=f"{label}.candidate",
                state_path=state_path,
            ),
        }
    raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)


def _validate_slice_binding_snapshot(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> dict[str, Any]:
    binding = _require_exact_dict_keys(
        value,
        expected=_SLICE_BINDING_FIELDS,
        label=label,
        state_path=state_path,
    )
    state = _require_non_empty_string(binding.get("state"), label=f"{label}.state", state_path=state_path)
    gate_state = _require_non_empty_string(
        binding.get("gate_state"),
        label=f"{label}.gate_state",
        state_path=state_path,
    )
    if state not in VALID_SLICE_STATES or gate_state not in VALID_GATE_STATES:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    binding_version = _require_non_empty_string(
        binding.get("binding_version"),
        label=f"{label}.binding_version",
        state_path=state_path,
    )
    if binding_version != _SLICE_BINDING_VERSION:
        raise _contract_error("unsupported-recovery-version", state_path=state_path, detail=label)
    return {
        "binding_version": binding_version,
        "binding_revision": _require_safe_integer(
            binding.get("binding_revision"),
            label=f"{label}.binding_revision",
            state_path=state_path,
        ),
        "builder_job_id": _validate_job_ref(
            binding.get("builder_job_id"),
            label=f"{label}.builder_job_id",
            state_path=state_path,
        ),
        "reviewer_job_id": _validate_job_ref(
            binding.get("reviewer_job_id"),
            label=f"{label}.reviewer_job_id",
            state_path=state_path,
        ),
        "candidate": _validate_candidate(
            binding.get("candidate"),
            label=f"{label}.candidate",
            state_path=state_path,
        ),
        "state": state,
        "gate_state": gate_state,
        "spec": _validate_spec_meta(binding.get("spec"), label=f"{label}.spec", state_path=state_path),
        "plan": _validate_spec_meta(binding.get("plan"), label=f"{label}.plan", state_path=state_path),
        "verification_hash": _require_non_empty_string(
            binding.get("verification_hash"),
            label=f"{label}.verification_hash",
            state_path=state_path,
        ),
        "target_branch": _require_non_empty_string(
            binding.get("target_branch"),
            label=f"{label}.target_branch",
            state_path=state_path,
        ),
        "target_remote": _require_non_empty_string(
            binding.get("target_remote"),
            label=f"{label}.target_remote",
            state_path=state_path,
        ),
        "dispatch_base": _require_optional_string(
            binding.get("dispatch_base"),
            label=f"{label}.dispatch_base",
            state_path=state_path,
        ),
    }


def _validate_legacy_binding_snapshot(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> dict[str, Any]:
    binding = _require_exact_dict_keys(
        value,
        expected=_LEGACY_BINDING_FIELDS,
        label=label,
        state_path=state_path,
    )
    state = _require_non_empty_string(binding.get("state"), label=f"{label}.state", state_path=state_path)
    gate_state = _require_non_empty_string(
        binding.get("gate_state"),
        label=f"{label}.gate_state",
        state_path=state_path,
    )
    if state not in VALID_SLICE_STATES or gate_state not in VALID_GATE_STATES:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    return {
        "builder_job_id": _validate_job_ref(
            binding.get("builder_job_id"),
            label=f"{label}.builder_job_id",
            state_path=state_path,
        ),
        "reviewer_job_id": _validate_job_ref(
            binding.get("reviewer_job_id"),
            label=f"{label}.reviewer_job_id",
            state_path=state_path,
        ),
        "candidate": _validate_candidate(
            binding.get("candidate"),
            label=f"{label}.candidate",
            state_path=state_path,
        ),
        "state": state,
        "gate_state": gate_state,
        "spec": _validate_spec_meta(binding.get("spec"), label=f"{label}.spec", state_path=state_path),
        "plan": _validate_spec_meta(binding.get("plan"), label=f"{label}.plan", state_path=state_path),
        "verification_hash": _require_non_empty_string(
            binding.get("verification_hash"),
            label=f"{label}.verification_hash",
            state_path=state_path,
        ),
        "target_branch": _require_non_empty_string(
            binding.get("target_branch"),
            label=f"{label}.target_branch",
            state_path=state_path,
        ),
        "target_remote": _require_non_empty_string(
            binding.get("target_remote"),
            label=f"{label}.target_remote",
            state_path=state_path,
        ),
        "dispatch_base": _require_optional_string(
            binding.get("dispatch_base"),
            label=f"{label}.dispatch_base",
            state_path=state_path,
        ),
    }


def _validate_job_refs_snapshot(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> dict[str, str | None]:
    refs = _require_exact_dict_keys(
        value,
        expected=_CHECKPOINT_JOB_REF_FIELDS,
        label=label,
        state_path=state_path,
    )
    return {
        "builder_job_id": _validate_job_ref(
            refs.get("builder_job_id"),
            label=f"{label}.builder_job_id",
            state_path=state_path,
        ),
        "reviewer_job_id": _validate_job_ref(
            refs.get("reviewer_job_id"),
            label=f"{label}.reviewer_job_id",
            state_path=state_path,
        ),
    }


def _validate_recovery_request_payload(
    value: object,
    *,
    expected_slice_id: str | None,
    state_path: Path,
) -> dict[str, Any]:
    payload = _require_exact_dict_keys(
        value,
        expected=_RECOVERY_REQUEST_PAYLOAD_FIELDS,
        label="recovery payload",
        state_path=state_path,
    )
    target = _validate_target(payload.get("target"), label="recovery payload.target", state_path=state_path)
    if expected_slice_id is not None and target["slice_id"] != expected_slice_id:
        raise _contract_error("request-content-conflict", state_path=state_path, detail="slice_id")
    expected_binding = _validate_slice_binding_snapshot(
        payload.get("expected_binding"),
        label="recovery payload.expected_binding",
        state_path=state_path,
    )
    if expected_binding["candidate"] is not None or expected_binding["state"] not in {"needs_human", "failed"}:
        raise _contract_error("recovery-binding-required", state_path=state_path)
    if (
        expected_binding["builder_job_id"] is None
        and expected_binding["reviewer_job_id"] is None
    ):
        raise _contract_error("recovery-binding-required", state_path=state_path)
    action = _require_non_empty_string(payload.get("action"), label="recovery payload.action", state_path=state_path)
    if action != "recover-pre-candidate":
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail="action")
    request_type = _require_non_empty_string(
        payload.get("request_type"),
        label="recovery payload.request_type",
        state_path=state_path,
    )
    if request_type not in {"slice-action", "work-action"}:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail="request_type")
    return {
        "request_type": request_type,
        "action": action,
        "target": target,
        "expected_binding": expected_binding,
        "required_steps": _validate_required_steps(
            payload.get("required_steps"),
            label="recovery payload.required_steps",
            state_path=state_path,
        ),
        "actor": _require_non_empty_string(
            payload.get("actor"),
            label="recovery payload.actor",
            state_path=state_path,
        ),
        "requested_by": _require_non_empty_string(
            payload.get("requested_by"),
            label="recovery payload.requested_by",
            state_path=state_path,
        ),
        "created_at": _require_non_empty_string(
            payload.get("created_at"),
            label="recovery payload.created_at",
            state_path=state_path,
        ),
    }


def _validate_checkpoint_provenance(
    value: object,
    *,
    label: str,
    state_path: Path,
) -> dict[str, Any]:
    provenance = _require_exact_dict_keys(
        value,
        expected=_CHECKPOINT_PROVENANCE_FIELDS,
        label=label,
        state_path=state_path,
    )
    return {
        "owner_action_ref": _require_non_empty_string(
            provenance.get("owner_action_ref"),
            label=f"{label}.owner_action_ref",
            state_path=state_path,
        ),
        "observed_at": _require_non_empty_string(
            provenance.get("observed_at"),
            label=f"{label}.observed_at",
            state_path=state_path,
        ),
        "authority_ref": _require_non_empty_string(
            provenance.get("authority_ref"),
            label=f"{label}.authority_ref",
            state_path=state_path,
        ),
        "proof_refs": _validate_proof_refs(
            provenance.get("proof_refs"),
            label=f"{label}.proof_refs",
            state_path=state_path,
            required=True,
        ),
    }


def _checkpoint_fingerprint_payload(
    *,
    expected_legacy_row: Mapping[str, Any],
    expected_legacy_binding: Mapping[str, Any],
    expected_job_refs: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "version": _CHECKPOINT_SNAPSHOT_VERSION,
        "slice": dict(expected_legacy_row),
        "binding": dict(expected_legacy_binding),
        "job_refs": dict(expected_job_refs),
    }


def _compute_checkpoint_fingerprint(
    *,
    expected_legacy_row: Mapping[str, Any],
    expected_legacy_binding: Mapping[str, Any],
    expected_job_refs: Mapping[str, Any],
) -> str:
    return _prefixed_canonical_json_digest(
        _CHECKPOINT_SNAPSHOT_VERSION,
        _checkpoint_fingerprint_payload(
            expected_legacy_row=expected_legacy_row,
            expected_legacy_binding=expected_legacy_binding,
            expected_job_refs=expected_job_refs,
        ),
    )


def _validate_checkpoint_request_payload(
    value: object,
    *,
    expected_slice_id: str | None,
    state_path: Path,
) -> dict[str, Any]:
    payload = _require_exact_dict_keys(
        value,
        expected=_CHECKPOINT_REQUEST_PAYLOAD_FIELDS,
        label="checkpoint payload",
        state_path=state_path,
    )
    operation = _require_non_empty_string(
        payload.get("operation"),
        label="checkpoint payload.operation",
        state_path=state_path,
    )
    if operation != "checkpoint-legacy-binding":
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail="operation")
    target = _validate_target(payload.get("target"), label="checkpoint payload.target", state_path=state_path)
    if expected_slice_id is not None and target["slice_id"] != expected_slice_id:
        raise _contract_error("request-content-conflict", state_path=state_path, detail="slice_id")
    expected_legacy_row = _validate_json_tree(
        payload.get("expected_legacy_row"),
        allow_bool=True,
        allow_float=True,
        label="checkpoint payload.expected_legacy_row",
        state_path=state_path,
    )
    if not isinstance(expected_legacy_row, dict):
        raise _contract_error(
            "malformed-recovery-context",
            state_path=state_path,
            detail="checkpoint payload.expected_legacy_row",
        )
    if any(field in expected_legacy_row for field in ("binding_version", "binding_revision", "binding_checkpoint_receipt")):
        raise _contract_error("legacy-checkpoint-not-applicable", state_path=state_path)
    expected_legacy_binding = _validate_legacy_binding_snapshot(
        payload.get("expected_legacy_binding"),
        label="checkpoint payload.expected_legacy_binding",
        state_path=state_path,
    )
    expected_job_refs = _validate_job_refs_snapshot(
        payload.get("expected_job_refs"),
        label="checkpoint payload.expected_job_refs",
        state_path=state_path,
    )
    try:
        row_binding_source = {
            "builder_job_id": expected_legacy_row["builder_job_id"],
            "reviewer_job_id": expected_legacy_row["reviewer_job_id"],
            "candidate": expected_legacy_row["candidate"],
            "state": expected_legacy_row["state"],
            "gate_state": expected_legacy_row["gate_state"],
            "spec": expected_legacy_row["spec"],
            "plan": expected_legacy_row["plan"],
            "verification_hash": expected_legacy_row["verification"]["hash"],
            "target_branch": expected_legacy_row["target_branch"],
            "target_remote": expected_legacy_row["target_remote"],
            "dispatch_base": expected_legacy_row["dispatch_base"],
        }
    except (KeyError, TypeError) as exc:
        raise _contract_error(
            "malformed-recovery-context",
            state_path=state_path,
            detail="expected_legacy_row",
        ) from exc
    row_binding = _validate_legacy_binding_snapshot(
        row_binding_source,
        label="checkpoint payload.expected_legacy_row.binding",
        state_path=state_path,
    )
    if row_binding != expected_legacy_binding:
        raise _contract_error("request-content-conflict", state_path=state_path, detail="expected_legacy_binding")
    row_job_refs = _validate_job_refs_snapshot(
        {field: expected_legacy_row.get(field) for field in _CHECKPOINT_JOB_REF_FIELDS},
        label="checkpoint payload.expected_legacy_row.refs",
        state_path=state_path,
    )
    if row_job_refs != expected_job_refs or row_job_refs != {
        "builder_job_id": expected_legacy_binding["builder_job_id"],
        "reviewer_job_id": expected_legacy_binding["reviewer_job_id"],
    }:
        raise _contract_error("request-content-conflict", state_path=state_path, detail="expected_job_refs")
    legacy_snapshot_fingerprint = _require_sha256_hex(
        payload.get("legacy_snapshot_fingerprint"),
        label="checkpoint payload.legacy_snapshot_fingerprint",
        state_path=state_path,
    )
    expected_fingerprint = _compute_checkpoint_fingerprint(
        expected_legacy_row=expected_legacy_row,
        expected_legacy_binding=expected_legacy_binding,
        expected_job_refs=expected_job_refs,
    )
    if legacy_snapshot_fingerprint != expected_fingerprint:
        raise _contract_error("request-content-conflict", state_path=state_path, detail="legacy_snapshot_fingerprint")
    return {
        "operation": operation,
        "target": target,
        "expected_legacy_row": expected_legacy_row,
        "expected_legacy_binding": expected_legacy_binding,
        "expected_job_refs": expected_job_refs,
        "legacy_snapshot_fingerprint": legacy_snapshot_fingerprint,
        "actor": _require_non_empty_string(
            payload.get("actor"),
            label="checkpoint payload.actor",
            state_path=state_path,
        ),
        "requested_by": _require_non_empty_string(
            payload.get("requested_by"),
            label="checkpoint payload.requested_by",
            state_path=state_path,
        ),
        "created_at": _require_non_empty_string(
            payload.get("created_at"),
            label="checkpoint payload.created_at",
            state_path=state_path,
        ),
        "provenance": _validate_checkpoint_provenance(
            payload.get("provenance"),
            label="checkpoint payload.provenance",
            state_path=state_path,
        ),
    }


def _validate_recovery_request(
    value: object,
    *,
    expected_slice_id: str | None,
    state_path: Path,
) -> dict[str, Any]:
    request = _require_exact_dict_keys(
        value,
        expected=_RECOVERY_REQUEST_FIELDS,
        label="recovery request",
        state_path=state_path,
    )
    version = _require_non_empty_string(
        request.get("version"),
        label="recovery request.version",
        state_path=state_path,
    )
    if version != _RECOVERY_REQUEST_VERSION:
        raise _contract_error("unsupported-recovery-version", state_path=state_path)
    request_id = _require_non_empty_string(
        request.get("request_id"),
        label="recovery request.request_id",
        state_path=state_path,
    )
    payload = _validate_recovery_request_payload(
        request.get("payload"),
        expected_slice_id=expected_slice_id,
        state_path=state_path,
    )
    request_digest = _require_sha256_hex(
        request.get("request_digest"),
        label="recovery request.request_digest",
        state_path=state_path,
    )
    expected_digest = _prefixed_canonical_json_digest(
        _RECOVERY_REQUEST_VERSION,
        {"version": version, "request_id": request_id, "payload": payload},
    )
    if request_digest != expected_digest:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail="request_digest")
    return {
        "version": version,
        "request_id": request_id,
        "payload": payload,
        "request_digest": request_digest,
    }


def _validate_checkpoint_request(
    value: object,
    *,
    expected_slice_id: str | None,
    state_path: Path,
) -> dict[str, Any]:
    request = _require_exact_dict_keys(
        value,
        expected=_CHECKPOINT_REQUEST_FIELDS,
        label="checkpoint request",
        state_path=state_path,
    )
    version = _require_non_empty_string(
        request.get("version"),
        label="checkpoint request.version",
        state_path=state_path,
    )
    if version != _CHECKPOINT_REQUEST_VERSION:
        raise _contract_error("unsupported-recovery-version", state_path=state_path)
    request_id = _require_non_empty_string(
        request.get("request_id"),
        label="checkpoint request.request_id",
        state_path=state_path,
    )
    payload = _validate_checkpoint_request_payload(
        request.get("payload"),
        expected_slice_id=expected_slice_id,
        state_path=state_path,
    )
    request_digest = _require_sha256_hex(
        request.get("request_digest"),
        label="checkpoint request.request_digest",
        state_path=state_path,
    )
    expected_digest = _prefixed_canonical_json_digest(
        _CHECKPOINT_REQUEST_VERSION,
        {"version": version, "request_id": request_id, "payload": payload},
    )
    if request_digest != expected_digest:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail="request_digest")
    return {
        "version": version,
        "request_id": request_id,
        "payload": payload,
        "request_digest": request_digest,
    }


def _expected_affected_job_ids(expected_binding: Mapping[str, Any]) -> list[str]:
    return [
        job_id
        for job_id in (
            expected_binding.get("builder_job_id"),
            expected_binding.get("reviewer_job_id"),
        )
        if isinstance(job_id, str)
    ]


def _expected_recovery_applied_binding(expected_binding: Mapping[str, Any]) -> dict[str, Any]:
    applied = _deepcopy_json(expected_binding)
    applied["binding_revision"] = int(expected_binding["binding_revision"]) + 1
    applied["builder_job_id"] = None
    applied["reviewer_job_id"] = None
    applied["candidate"] = None
    applied["state"] = "pending"
    applied["gate_state"] = "pending"
    return applied


def _expected_checkpoint_applied_binding(expected_legacy_binding: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "binding_version": _SLICE_BINDING_VERSION,
        "binding_revision": 1,
        **_deepcopy_json(expected_legacy_binding),
    }


def _validate_affected_job_ids(
    value: object,
    *,
    expected_binding: Mapping[str, Any],
    label: str,
    state_path: Path,
) -> list[str]:
    if not isinstance(value, list):
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail=label)
    normalized = [
        _require_non_empty_string(item, label=f"{label}[]", state_path=state_path)
        for item in value
    ]
    if normalized != _expected_affected_job_ids(expected_binding):
        raise _contract_error("request-content-conflict", state_path=state_path, detail=label)
    return normalized


def _validate_step_receipts(
    value: object,
    *,
    required_steps: list[str],
    label: str,
    state_path: Path,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise _contract_error("incomplete-recovery-proof", state_path=state_path)
    normalized: list[dict[str, str]] = []
    seen_steps: set[str] = set()
    for item in value:
        receipt = _require_exact_dict_keys(
            item,
            expected=_RECOVERY_STEP_RECEIPT_FIELDS,
            label=f"{label}[]",
            state_path=state_path,
        )
        step_id = _require_non_empty_string(
            receipt.get("step_id"),
            label=f"{label}[].step_id",
            state_path=state_path,
        )
        if step_id not in required_steps or step_id in seen_steps:
            raise _contract_error("incomplete-recovery-proof", state_path=state_path)
        seen_steps.add(step_id)
        normalized.append(
            {
                "step_id": step_id,
                "ref": _require_non_empty_string(
                    receipt.get("ref"),
                    label=f"{label}[].ref",
                    state_path=state_path,
                ),
                "sha256": _require_sha256_hex(
                    receipt.get("sha256"),
                    label=f"{label}[].sha256",
                    state_path=state_path,
                ),
            }
        )
    if seen_steps != set(required_steps):
        raise _contract_error("incomplete-recovery-proof", state_path=state_path)
    return normalized


def _validate_recovery_receipt(
    value: object,
    *,
    slice_row: Mapping[str, Any],
    state_path: Path,
) -> dict[str, Any]:
    receipt = _require_exact_dict_keys(
        value,
        expected=_RECOVERY_RECEIPT_FIELDS,
        label="recovery receipt",
        state_path=state_path,
    )
    version = _require_non_empty_string(
        receipt.get("version"),
        label="recovery receipt.version",
        state_path=state_path,
    )
    if version != _RECOVERY_RECEIPT_VERSION:
        raise _contract_error("unsupported-recovery-version", state_path=state_path)
    request_id = _require_non_empty_string(
        receipt.get("request_id"),
        label="recovery receipt.request_id",
        state_path=state_path,
    )
    payload = _validate_recovery_request_payload(
        receipt.get("payload"),
        expected_slice_id=str(slice_row["slice_id"]),
        state_path=state_path,
    )
    request_digest = _require_sha256_hex(
        receipt.get("request_digest"),
        label="recovery receipt.request_digest",
        state_path=state_path,
    )
    expected_digest = _prefixed_canonical_json_digest(
        _RECOVERY_REQUEST_VERSION,
        {"version": _RECOVERY_REQUEST_VERSION, "request_id": request_id, "payload": payload},
    )
    if request_digest != expected_digest:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail="request_digest")
    phase = _require_non_empty_string(
        receipt.get("phase"),
        label="recovery receipt.phase",
        state_path=state_path,
    )
    if phase not in {"prepared", "complete"}:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail="phase")
    normalized = {
        "version": version,
        "request_id": request_id,
        "request_digest": request_digest,
        "payload": payload,
        "phase": phase,
        "prepared_at": _require_non_empty_string(
            receipt.get("prepared_at"),
            label="recovery receipt.prepared_at",
            state_path=state_path,
        ),
        "completed_at": None,
        "applied_binding": None,
        "affected_job_ids": _validate_affected_job_ids(
            receipt.get("affected_job_ids"),
            expected_binding=payload["expected_binding"],
            label="recovery receipt.affected_job_ids",
            state_path=state_path,
        ),
        "required_steps": _validate_required_steps(
            receipt.get("required_steps"),
            label="recovery receipt.required_steps",
            state_path=state_path,
        ),
        "step_receipts": [],
        "result": None,
    }
    if normalized["required_steps"] != payload["required_steps"]:
        raise _contract_error("request-content-conflict", state_path=state_path, detail="required_steps")
    if phase == "prepared":
        if (
            receipt.get("completed_at") is not None
            or receipt.get("applied_binding") is not None
            or receipt.get("result") is not None
            or receipt.get("step_receipts") != []
        ):
            raise _contract_error("malformed-recovery-context", state_path=state_path, detail="prepared-phase")
        return normalized
    normalized["completed_at"] = _require_non_empty_string(
        receipt.get("completed_at"),
        label="recovery receipt.completed_at",
        state_path=state_path,
    )
    normalized["applied_binding"] = _validate_slice_binding_snapshot(
        receipt.get("applied_binding"),
        label="recovery receipt.applied_binding",
        state_path=state_path,
    )
    if normalized["applied_binding"] != _expected_recovery_applied_binding(payload["expected_binding"]):
        raise _contract_error("request-content-conflict", state_path=state_path, detail="applied_binding")
    normalized["step_receipts"] = _validate_step_receipts(
        receipt.get("step_receipts"),
        required_steps=normalized["required_steps"],
        label="recovery receipt.step_receipts",
        state_path=state_path,
    )
    normalized["result"] = _require_non_empty_string(
        receipt.get("result"),
        label="recovery receipt.result",
        state_path=state_path,
    )
    return normalized


def _validate_checkpoint_receipt(
    value: object,
    *,
    slice_row: Mapping[str, Any],
    state_path: Path,
) -> dict[str, Any]:
    receipt = _require_exact_dict_keys(
        value,
        expected=_CHECKPOINT_RECEIPT_FIELDS,
        label="checkpoint receipt",
        state_path=state_path,
    )
    version = _require_non_empty_string(
        receipt.get("version"),
        label="checkpoint receipt.version",
        state_path=state_path,
    )
    if version != _CHECKPOINT_RECEIPT_VERSION:
        raise _contract_error("unsupported-recovery-version", state_path=state_path)
    request_id = _require_non_empty_string(
        receipt.get("request_id"),
        label="checkpoint receipt.request_id",
        state_path=state_path,
    )
    payload = _validate_checkpoint_request_payload(
        receipt.get("payload"),
        expected_slice_id=str(slice_row["slice_id"]),
        state_path=state_path,
    )
    request_digest = _require_sha256_hex(
        receipt.get("request_digest"),
        label="checkpoint receipt.request_digest",
        state_path=state_path,
    )
    expected_digest = _prefixed_canonical_json_digest(
        _CHECKPOINT_REQUEST_VERSION,
        {"version": _CHECKPOINT_REQUEST_VERSION, "request_id": request_id, "payload": payload},
    )
    if request_digest != expected_digest:
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail="request_digest")
    phase = _require_non_empty_string(
        receipt.get("phase"),
        label="checkpoint receipt.phase",
        state_path=state_path,
    )
    if phase != "complete":
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail="phase")
    applied_binding = _validate_slice_binding_snapshot(
        receipt.get("applied_binding"),
        label="checkpoint receipt.applied_binding",
        state_path=state_path,
    )
    if applied_binding != _expected_checkpoint_applied_binding(payload["expected_legacy_binding"]):
        raise _contract_error("request-content-conflict", state_path=state_path, detail="applied_binding")
    provenance = _validate_checkpoint_provenance(
        receipt.get("provenance"),
        label="checkpoint receipt.provenance",
        state_path=state_path,
    )
    if provenance != payload["provenance"]:
        raise _contract_error("request-content-conflict", state_path=state_path, detail="provenance")
    result = _require_non_empty_string(
        receipt.get("result"),
        label="checkpoint receipt.result",
        state_path=state_path,
    )
    if result != "checkpoint-created":
        raise _contract_error("request-content-conflict", state_path=state_path, detail="result")
    return {
        "version": version,
        "request_id": request_id,
        "request_digest": request_digest,
        "payload": payload,
        "phase": phase,
        "checkpointed_at": _require_non_empty_string(
            receipt.get("checkpointed_at"),
            label="checkpoint receipt.checkpointed_at",
            state_path=state_path,
        ),
        "applied_binding": applied_binding,
        "provenance": provenance,
        "result": result,
    }


def _validate_job_supersession(
    value: object,
    *,
    state_path: Path,
) -> dict[str, Any]:
    supersession = _require_dict_keys_with_optional(
        value,
        required=_JOB_SUPERSESSION_REQUIRED_FIELDS,
        optional=frozenset({_BOUND_BINDING_FIELD}),
        label="job supersession",
        state_path=state_path,
    )
    version = _require_non_empty_string(
        supersession.get("version"),
        label="job supersession.version",
        state_path=state_path,
    )
    if version != _JOB_SUPERSESSION_VERSION:
        raise _contract_error("unsupported-recovery-version", state_path=state_path)
    job_id = _require_non_empty_string(
        supersession.get("job_id"),
        label="job supersession.job_id",
        state_path=state_path,
    )
    binding_revision = _require_safe_integer(
        supersession.get("binding_revision"),
        label="job supersession.binding_revision",
        state_path=state_path,
    )
    normalized = {
        "version": version,
        "job_id": job_id,
        "slice_id": _require_non_empty_string(
            supersession.get("slice_id"),
            label="job supersession.slice_id",
            state_path=state_path,
        ),
        "binding_revision": binding_revision,
        "actor": _require_non_empty_string(
            supersession.get("actor"),
            label="job supersession.actor",
            state_path=state_path,
        ),
        "at": _require_non_empty_string(
            supersession.get("at"),
            label="job supersession.at",
            state_path=state_path,
        ),
        "reason": _require_non_empty_string(
            supersession.get("reason"),
            label="job supersession.reason",
            state_path=state_path,
        ),
        "superseding_identity": _validate_disposition_identity(
            supersession.get("superseding_identity"),
            label="job supersession.superseding_identity",
            state_path=state_path,
        ),
    }
    if _BOUND_BINDING_FIELD in supersession:
        normalized[_BOUND_BINDING_FIELD] = _validate_slice_binding_snapshot(
            supersession.get(_BOUND_BINDING_FIELD),
            label=f"job supersession.{_BOUND_BINDING_FIELD}",
            state_path=state_path,
        )
        if (
            int(normalized[_BOUND_BINDING_FIELD]["binding_revision"]) != binding_revision
            or not _job_id_in_binding_snapshot(job_id, normalized[_BOUND_BINDING_FIELD])
        ):
            raise _contract_error(
                "request-content-conflict",
                state_path=state_path,
                detail=f"job supersession.{_BOUND_BINDING_FIELD}",
            )
    return normalized


def _validate_job_consumption(
    value: object,
    *,
    state_path: Path,
) -> dict[str, Any]:
    consumption = _require_dict_keys_with_optional(
        value,
        required=_JOB_CONSUMPTION_REQUIRED_FIELDS,
        optional=frozenset({_BOUND_BINDING_FIELD}),
        label="job consumption",
        state_path=state_path,
    )
    version = _require_non_empty_string(
        consumption.get("version"),
        label="job consumption.version",
        state_path=state_path,
    )
    if version != _JOB_CONSUMPTION_VERSION:
        raise _contract_error("unsupported-recovery-version", state_path=state_path)
    job_id = _require_non_empty_string(
        consumption.get("job_id"),
        label="job consumption.job_id",
        state_path=state_path,
    )
    binding_revision = _require_safe_integer(
        consumption.get("binding_revision"),
        label="job consumption.binding_revision",
        state_path=state_path,
    )
    normalized = {
        "version": version,
        "job_id": job_id,
        "slice_id": _require_non_empty_string(
            consumption.get("slice_id"),
            label="job consumption.slice_id",
            state_path=state_path,
        ),
        "binding_revision": binding_revision,
        "actor": _require_non_empty_string(
            consumption.get("actor"),
            label="job consumption.actor",
            state_path=state_path,
        ),
        "at": _require_non_empty_string(
            consumption.get("at"),
            label="job consumption.at",
            state_path=state_path,
        ),
        "completion_identity": _validate_disposition_identity(
            consumption.get("completion_identity"),
            label="job consumption.completion_identity",
            state_path=state_path,
        ),
        "proof_refs": _validate_proof_refs(
            consumption.get("proof_refs"),
            label="job consumption.proof_refs",
            state_path=state_path,
            required=True,
        ),
    }
    if _BOUND_BINDING_FIELD in consumption:
        normalized[_BOUND_BINDING_FIELD] = _validate_slice_binding_snapshot(
            consumption.get(_BOUND_BINDING_FIELD),
            label=f"job consumption.{_BOUND_BINDING_FIELD}",
            state_path=state_path,
        )
        if (
            int(normalized[_BOUND_BINDING_FIELD]["binding_revision"]) != binding_revision
            or not _job_id_in_binding_snapshot(job_id, normalized[_BOUND_BINDING_FIELD])
        ):
            raise _contract_error(
                "request-content-conflict",
                state_path=state_path,
                detail=f"job consumption.{_BOUND_BINDING_FIELD}",
            )
    return normalized


def _slice_binding_from_row(slice_row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "binding_version": slice_row["binding_version"],
        "binding_revision": slice_row["binding_revision"],
        "builder_job_id": slice_row["builder_job_id"],
        "reviewer_job_id": slice_row["reviewer_job_id"],
        "candidate": slice_row["candidate"],
        "state": slice_row["state"],
        "gate_state": slice_row["gate_state"],
        "spec": dict(slice_row["spec"]),
        "plan": dict(slice_row["plan"]),
        "verification_hash": slice_row["verification"]["hash"],
        "target_branch": slice_row["target_branch"],
        "target_remote": slice_row["target_remote"],
        "dispatch_base": slice_row["dispatch_base"],
    }


def _legacy_binding_from_row(slice_row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "builder_job_id": slice_row["builder_job_id"],
        "reviewer_job_id": slice_row["reviewer_job_id"],
        "candidate": slice_row["candidate"],
        "state": slice_row["state"],
        "gate_state": slice_row["gate_state"],
        "spec": dict(slice_row["spec"]),
        "plan": dict(slice_row["plan"]),
        "verification_hash": slice_row["verification"]["hash"],
        "target_branch": slice_row["target_branch"],
        "target_remote": slice_row["target_remote"],
        "dispatch_base": slice_row["dispatch_base"],
    }


def _job_id_in_binding_snapshot(job_id: str, binding: Mapping[str, Any]) -> bool:
    return job_id in {
        binding.get("builder_job_id"),
        binding.get("reviewer_job_id"),
    }


def _validate_optional_recovery_slice_fields(
    slice_row: Mapping[str, Any],
    *,
    state_path: Path,
) -> dict[str, Any]:
    if "_binding_history" in slice_row:
        raise _contract_error(
            "malformed-recovery-context",
            state_path=state_path,
            detail="_binding_history",
        )
    has_binding_version = "binding_version" in slice_row
    has_binding_revision = "binding_revision" in slice_row
    has_recovery_receipts = "recovery_receipts" in slice_row
    has_checkpoint_receipt = "binding_checkpoint_receipt" in slice_row
    if has_binding_version != has_binding_revision:
        raise _contract_error("legacy-binding-unversioned", state_path=state_path)
    if not has_binding_version:
        if has_recovery_receipts or has_checkpoint_receipt:
            raise _contract_error("legacy-binding-unversioned", state_path=state_path)
        return {}
    binding_version = _require_non_empty_string(
        slice_row.get("binding_version"),
        label="slice.binding_version",
        state_path=state_path,
    )
    if binding_version != _SLICE_BINDING_VERSION:
        raise _contract_error("unsupported-recovery-version", state_path=state_path)
    binding_revision = _require_safe_integer(
        slice_row.get("binding_revision"),
        label="slice.binding_revision",
        state_path=state_path,
    )
    recovery_receipts = slice_row.get("recovery_receipts")
    if not isinstance(recovery_receipts, list):
        raise _contract_error("malformed-recovery-context", state_path=state_path, detail="recovery_receipts")
    validated: dict[str, Any] = {
        "binding_version": binding_version,
        "binding_revision": binding_revision,
        "recovery_receipts": [
            _validate_recovery_receipt(item, slice_row=slice_row, state_path=state_path)
            for item in recovery_receipts
        ],
    }
    if has_checkpoint_receipt:
        validated["binding_checkpoint_receipt"] = _validate_checkpoint_receipt(
            slice_row.get("binding_checkpoint_receipt"),
            slice_row=slice_row,
            state_path=state_path,
        )
    return validated


def _iter_registry_request_receipts(
    slice_row: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    receipts: list[tuple[str, dict[str, Any]]] = []
    if isinstance(slice_row.get("recovery_receipts"), list):
        for receipt in slice_row["recovery_receipts"]:
            if isinstance(receipt, dict):
                receipts.append(("recovery", receipt))
    checkpoint = slice_row.get("binding_checkpoint_receipt")
    if isinstance(checkpoint, dict):
        receipts.append(("checkpoint", checkpoint))
    return receipts


def _slice_binding_witnesses(slice_row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    witnesses: list[Mapping[str, Any]] = []
    if "binding_version" in slice_row and "binding_revision" in slice_row:
        witnesses.append(_slice_binding_from_row(slice_row))
    for kind, receipt in _iter_registry_request_receipts(slice_row):
        if kind == "recovery":
            payload = receipt.get("payload")
            if isinstance(payload, Mapping):
                expected_binding = payload.get("expected_binding")
                if isinstance(expected_binding, Mapping):
                    witnesses.append(expected_binding)
        applied_binding = receipt.get("applied_binding")
        if isinstance(applied_binding, Mapping):
            witnesses.append(applied_binding)
    return witnesses


def _validate_recovery_request_id_collisions(
    slices: list[Mapping[str, Any]],
    *,
    state_path: Path,
) -> None:
    seen: set[str] = set()
    for slice_row in slices:
        for _kind, receipt in _iter_registry_request_receipts(slice_row):
            request_id = str(receipt["request_id"])
            if request_id in seen:
                raise _contract_error("request-content-conflict", state_path=state_path, detail="request_id")
            seen.add(request_id)


def _empty_legacy_records() -> dict[str, Any]:
    return {"source_schema_version": 1, "seq": 0, "jobs": [], "slices": []}


_CURRENT_VERIFICATION_EVIDENCE_HASH = "current_verification_evidence_hash"


@dataclass(frozen=True)
class _DurableStateSnapshot:
    raw_bytes: bytes | None
    revision: str | None
    mtime_ns: int | None
    size: int | None


def canonical_state_path(state_path: str | Path) -> Path:
    state_path = Path(state_path).expanduser()
    return state_path.parent.resolve(strict=False) / state_path.name


def state_transaction_lock_path(state_path: str | Path) -> Path:
    return Path(f"{canonical_state_path(state_path)}.transaction.lock")


def _state_revision(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def _serialize_state_payload(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


class RegistryRevisionConflict(RuntimeError):
    def __init__(
        self,
        *,
        expected_revision: str | None,
        actual_revision: str | None,
        state_path: Path,
    ) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        self.state_path = Path(state_path)
        super().__init__(
            "registry revision conflict: "
            f"expected_revision={expected_revision}, "
            f"actual_revision={actual_revision}, "
            f"state_path={self.state_path}"
        )


def _is_sha256_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value.lower())
    )


def _read_current_verification_evidence_hash(slice_row: Mapping[str, Any]) -> str | None:
    refs = slice_row.get("current_evidence_refs")
    if not isinstance(refs, list) or not refs or not isinstance(refs[0], str):
        return None
    try:
        payload = json.loads(Path(refs[0]).read_text(encoding="utf-8"))
        normalized = verification.validate_verification_evidence(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return verification.canonical_json_hash(normalized)


def _normalize_loaded_slice_verification(slice_row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the additive evidence-hash field and repair #501 legacy rows.

    Before #501, ``verification.hash`` was overwritten with the hash of the
    current evidence payload. A pre-fix row has no
    ``current_verification_evidence_hash`` field, but its persisted contract is
    enough to distinguish that shape: when the old value is a SHA-256 digest
    different from the canonical contract hash, it is the legacy evidence
    hash. Preserve arbitrary/non-digest test and operator values rather than
    guessing across an otherwise unclassifiable row.
    """

    normalized = dict(slice_row)
    verification_meta = dict(slice_row["verification"])
    field_present = _CURRENT_VERIFICATION_EVIDENCE_HASH in slice_row
    evidence_hash = slice_row.get(_CURRENT_VERIFICATION_EVIDENCE_HASH)
    if evidence_hash is not None and (
        not isinstance(evidence_hash, str) or not evidence_hash
    ):
        raise ValueError("coordinator 狀態檔 current verification evidence hash 格式錯誤（fail-closed）")

    contract = verification_meta.get("contract")
    stored_hash = verification_meta["hash"]
    contract_hash = (
        verification.canonical_json_hash(contract) if isinstance(contract, dict) else None
    )
    derived_evidence_hash = (
        _read_current_verification_evidence_hash(slice_row)
        if evidence_hash is None
        else None
    )
    if evidence_hash is None and derived_evidence_hash is not None:
        evidence_hash = derived_evidence_hash

    # Only rows written before the additive field existed are eligible for the
    # deterministic repair. A present field belongs to the new schema and a
    # mismatching contract hash must remain fail-closed for operator recovery.
    if (
        not field_present
        and isinstance(contract_hash, str)
        and _is_sha256_digest(stored_hash)
        and stored_hash != contract_hash
    ):
        verification_meta["hash"] = contract_hash
        if evidence_hash is None:
            # The old writer stored the evidence hash exactly here. Keeping it
            # in the new field makes the current evidence independently
            # addressable even if the evidence file is no longer readable.
            evidence_hash = stored_hash

    normalized["verification"] = verification_meta
    normalized[_CURRENT_VERIFICATION_EVIDENCE_HASH] = evidence_hash
    return normalized


# #519：semantic-reclaim 世代熔斷（work_actions.SEMANTIC_RECLAIM_LIMIT）的重置
# 水位欄位集合。水位是 append-only 的「赦免名單」——記錄某次 operator 明示重置
# 當下已存在的 superseded run_id，之後熔斷只計不在任何赦免名單裡的世代。刻意
# 不改寫（更不刪除）任何既有 WorkflowRun row：run 歷史是稽核來源，重置是新增
# 一筆授權事實，不是抹掉失敗紀錄。
_RECLAIM_RESET_STRING_FIELDS = (
    "repo", "work_id", "actor", "reason", "evidence_ref", "evidence_hash", "created_at",
)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _migration_error(path: Path, reason: str) -> ValueError:
    return ValueError(
        f"coordinator 狀態檔需要人工 clean start: {path} ({reason}); "
        "請先 archive/remove 舊檔後再重試。"
    )


def _validate_transition(
    *,
    field: str,
    current: str,
    new: str,
    allowed: dict[str, frozenset[str]],
) -> None:
    legal = allowed.get(current)
    if legal is None or new not in legal:
        raise ValueError(f"非法 {field} transition: {current!r} -> {new!r}")


def _validate_slice_job_ref_in_state(
    *,
    field: str,
    job_id: object,
    job_ids: set[str],
    state_path: Path,
) -> None:
    if job_id is None:
        return
    if not isinstance(job_id, str) or not job_id:
        raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {state_path}")
    if job_id not in job_ids:
        raise ValueError(
            f"coordinator 狀態檔 {field} 指向不存在 job（fail-closed）: {state_path}: {job_id}"
        )


class JobRegistry:
    """Versioned coordinator state with atomic single-file persistence."""

    def __init__(self, state_path: str | Path | None = None, seq_start: int = 0) -> None:
        self._state_path = (
            Path(state_path).expanduser() if state_path is not None else _default_state_path()
        )
        self.canonical_state_path = canonical_state_path(self._state_path)
        self.state_transaction_lock_path = state_transaction_lock_path(self._state_path)
        self._seq_start = seq_start
        self._jobs: list[dict[str, Any]] = []
        self._slices: list[dict[str, Any]] = []
        self._workflows: list[WorkflowRun] = []
        self._legacy_records: dict[str, Any] = _empty_legacy_records()
        self._reclaim_resets: list[dict[str, Any]] = []
        self._seq = seq_start
        self._state_mtime_ns: int | None = None
        self._state_size: int | None = None
        self._loaded_revision: str | None = None
        self._loaded_source_schema_version: int | None = None
        self._pending_previous_raw_bytes: bytes | None = None
        self._load()

    def _record_loaded_snapshot(
        self,
        snapshot: _DurableStateSnapshot,
        *,
        source_schema_version: int | None,
    ) -> None:
        self._loaded_revision = snapshot.revision
        self._loaded_source_schema_version = source_schema_version
        self._state_mtime_ns = snapshot.mtime_ns
        self._state_size = snapshot.size

    def _restore_absent_snapshot(self, snapshot: _DurableStateSnapshot) -> None:
        self._jobs = []
        self._slices = []
        self._workflows = []
        self._legacy_records = _empty_legacy_records()
        self._reclaim_resets = []
        self._seq = self._seq_start
        self._record_loaded_snapshot(snapshot, source_schema_version=None)

    def _read_durable_snapshot(self) -> _DurableStateSnapshot:
        try:
            fd = os.open(self._state_path, os.O_RDONLY)
        except FileNotFoundError:
            return _DurableStateSnapshot(
                raw_bytes=None,
                revision=None,
                mtime_ns=None,
                size=None,
            )
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            stat = os.fstat(fd)
        finally:
            os.close(fd)
        raw_bytes = b"".join(chunks)
        return _DurableStateSnapshot(
            raw_bytes=raw_bytes,
            revision=_state_revision(raw_bytes),
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
        )

    @contextmanager
    def _hold_state_transaction_lock(self):
        lock_path = self.state_transaction_lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        created = not lock_path.exists()
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if created:
                _fsync_directory(lock_path.parent)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _reload_if_changed(self) -> None:
        snapshot = self._read_durable_snapshot()
        if snapshot.revision == self._loaded_revision:
            if (
                snapshot.mtime_ns != self._state_mtime_ns
                or snapshot.size != self._state_size
            ):
                self._record_loaded_snapshot(
                    snapshot,
                    source_schema_version=self._loaded_source_schema_version,
                )
            return
        self._load(snapshot=snapshot)

    def _load(
        self,
        *,
        snapshot: _DurableStateSnapshot | None = None,
        allow_repairs: bool = True,
    ) -> None:
        pending = snapshot
        while True:
            active_snapshot = pending if pending is not None else self._read_durable_snapshot()
            try:
                self._restore_from_snapshot(active_snapshot, allow_repairs=allow_repairs)
                return
            except RegistryRevisionConflict:
                if not allow_repairs:
                    raise
                pending = None

    def _restore_from_snapshot(
        self,
        snapshot: _DurableStateSnapshot,
        *,
        allow_repairs: bool,
    ) -> None:
        if snapshot.revision is None:
            self._restore_absent_snapshot(snapshot)
            return
        assert snapshot.raw_bytes is not None
        try:
            payload = json.loads(snapshot.raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"coordinator 狀態檔解析失敗（fail-closed）: {self._state_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        schema_version = payload.get("schema_version")
        if schema_version == 1:
            _, _, seq = self._validate_state_records(payload)
            legacy_records = {
                "source_schema_version": 1,
                "seq": seq,
                # Validation may normalize fields used by the live v2 model.
                # The quarantined v1 records are immutable migration evidence,
                # so preserve the validated source rows byte-for-byte in shape.
                "jobs": _deepcopy_json(payload["jobs"]),
                "slices": _deepcopy_json(payload["slices"]),
            }
            self._jobs = []
            self._slices = []
            self._workflows = []
            self._legacy_records = _deepcopy_json(legacy_records)
            self._reclaim_resets = []
            self._seq = seq
            self._record_loaded_snapshot(snapshot, source_schema_version=1)
            if allow_repairs:
                self._persist()
            return
        if schema_version != COORDINATOR_STATE_SCHEMA_VERSION:
            if schema_version is None:
                raise _migration_error(self._state_path, "缺少 schema_version（legacy jobs-only state）")
            raise _migration_error(
                self._state_path,
                f"不支援的 schema_version={schema_version!r}",
            )
        jobs, slices, seq = self._validate_state_records(payload)
        missing_v2_roots = [key for key in ("workflows", "legacy_records") if key not in payload]
        if missing_v2_roots:
            raise ValueError(
                "coordinator 狀態檔v2缺必要根欄位（fail-closed）: "
                + ", ".join(missing_v2_roots)
            )
        workflows = payload["workflows"]
        legacy_records = payload["legacy_records"]
        if not isinstance(workflows, list):
            raise ValueError(f"coordinator 狀態檔 workflow 格式錯誤（fail-closed）: {self._state_path}")
        try:
            validated_workflows = [WorkflowRun.from_dict(run) for run in workflows]
        except ValueError as exc:
            raise ValueError(
                f"coordinator 狀態檔 workflow 格式錯誤（fail-closed）: {self._state_path}: {exc}"
            ) from exc
        # claim_key 唯一性只約束 ongoing runs：abandon→reclaim（#256 D4／#299）會讓
        # released（superseded＋planning_released）歷史 row 與新 ongoing run 合法共用同
        # 一 claim_key（_manager_create_workflow_run 以 attempt 鹽化 run_id）。全域唯一
        # 性會讓重 claim persist 後的狀態檔無法重新載入（manager 重啟即 brick）。
        # run_id 唯一性維持全域 fail-closed。
        ongoing_claim_keys = [
            run.claim_key for run in validated_workflows if run.status == "ongoing"
        ]
        run_ids = [run.run_id for run in validated_workflows]
        if (
            len(set(ongoing_claim_keys)) != len(ongoing_claim_keys)
            or len(set(run_ids)) != len(run_ids)
        ):
            raise ValueError(f"coordinator 狀態檔 workflow 重複識別（fail-closed）: {self._state_path}")
        self._validate_legacy_records(legacy_records)
        # #519：`reclaim_resets` 刻意是「加法相容」的可選根欄位，不列入上面的
        # `missing_v2_roots` fail-closed 清單，也不 bump schema_version——本欄位
        # 出現前寫下的狀態檔（既有部署裡的每一份）都必須照常載入，否則升級即
        # brick manager。缺欄位一律視為「沒有任何重置授權」，也就是熔斷維持最
        # 嚴格的既有行為（fail-closed 方向）。
        reclaim_resets = self._validate_reclaim_resets(payload.get("reclaim_resets", []))
        self._jobs = jobs
        self._slices = slices
        self._workflows = validated_workflows
        self._legacy_records = _deepcopy_json(legacy_records)
        self._reclaim_resets = reclaim_resets
        self._seq = seq
        self._record_loaded_snapshot(
            snapshot,
            source_schema_version=COORDINATOR_STATE_SCHEMA_VERSION,
        )
        if slices != payload["slices"] and allow_repairs:
            # #501：the additive evidence-hash field and deterministic repair
            # must survive a restart, otherwise the same legacy row would be
            # reclassified on every load.
            self._persist()

    def _validate_reclaim_resets(self, value: object) -> list[dict[str, Any]]:
        """#519：semantic-reclaim 重置水位的載入驗證（malformed 一律 fail-closed）。"""

        if not isinstance(value, list):
            raise ValueError(
                f"coordinator 狀態檔 reclaim_resets 格式錯誤（fail-closed）: {self._state_path}"
            )
        validated: list[dict[str, Any]] = []
        for entry in value:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"coordinator 狀態檔 reclaim_resets 格式錯誤（fail-closed）: {self._state_path}"
                )
            for field in _RECLAIM_RESET_STRING_FIELDS:
                if not isinstance(entry.get(field), str) or not entry[field]:
                    raise ValueError(
                        "coordinator 狀態檔 reclaim_resets 欄位缺漏（fail-closed）: "
                        f"{self._state_path}: {field}"
                    )
            cleared = entry.get("cleared_run_ids")
            if not _is_ref_list(cleared) or not cleared:
                raise ValueError(
                    "coordinator 狀態檔 reclaim_resets cleared_run_ids 格式錯誤"
                    f"（fail-closed）: {self._state_path}"
                )
            validated.append(_deepcopy_json(entry))
        return validated

    def _validate_state_records(
        self, payload: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        jobs = payload.get("jobs")
        slices = payload.get("slices")
        seq = payload.get("seq", 0)
        if not isinstance(jobs, list) or not isinstance(slices, list) or not isinstance(seq, int):
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        validated_jobs = [self._validate_loaded_job(job) for job in jobs]
        job_ids = {str(job["job_id"]) for job in validated_jobs}
        for job in validated_jobs:
            builder_job_id = job.get("workflow_builder_job_id")
            if builder_job_id is not None and (
                builder_job_id not in job_ids or builder_job_id == job.get("job_id")
            ):
                raise ValueError(
                    "coordinator 狀態檔 workflow_builder_job_id 格式錯誤（fail-closed）: "
                    f"{self._state_path}"
                )
        validated_slices = [self._validate_loaded_slice(slice_row, job_ids) for slice_row in slices]
        _validate_recovery_request_id_collisions(validated_slices, state_path=self._state_path)
        slices_by_id = {str(slice_row["slice_id"]): slice_row for slice_row in validated_slices}
        for job in validated_jobs:
            for field_name in ("supersession", "consumption"):
                entry = job.get(field_name)
                if not isinstance(entry, dict):
                    continue
                slice_row = slices_by_id.get(str(entry["slice_id"]))
                if slice_row is None:
                    raise _contract_error(
                        "request-content-conflict",
                        state_path=self._state_path,
                        detail=f"{field_name}.binding",
                    )
                self._validate_loaded_job_disposition_binding(
                    job,
                    field_name=field_name,
                    entry=entry,
                    slice_row=slice_row,
                )
        return validated_jobs, validated_slices, seq

    def _validate_legacy_records(self, value: object) -> None:
        if not isinstance(value, dict):
            raise ValueError(f"coordinator 狀態檔 legacy_records 格式錯誤: {self._state_path}")
        if value.get("source_schema_version") != 1 or not isinstance(value.get("seq"), int):
            raise ValueError(f"coordinator 狀態檔 legacy_records 格式錯誤: {self._state_path}")
        jobs = value.get("jobs")
        slices = value.get("slices")
        if not isinstance(jobs, list) or not isinstance(slices, list):
            raise ValueError(f"coordinator 狀態檔 legacy_records 格式錯誤: {self._state_path}")
        validated_jobs = [self._validate_loaded_job(job) for job in jobs]
        job_ids = {str(job["job_id"]) for job in validated_jobs}
        for slice_row in slices:
            self._validate_loaded_slice(slice_row, job_ids)

    def _write_v1_backup(self, original: bytes) -> Path:
        directory = self._state_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(original).hexdigest()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = directory / f"{self._state_path.name}.v1.{timestamp}.{digest}.bak"
        fd, tmp_name = tempfile.mkstemp(dir=str(directory), suffix=".backup.tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o400)
            os.link(tmp, backup)
            _fsync_directory(directory)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.unlink(missing_ok=True)
        return backup

    def _write_payload_atomically(
        self,
        payload: dict[str, Any],
    ) -> _DurableStateSnapshot:
        raw_bytes = _serialize_state_payload(payload)
        previous_raw_bytes = self._pending_previous_raw_bytes
        directory = self._state_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
        tmp = Path(tmp_name)
        backup: Path | None = None
        had_original = previous_raw_bytes is not None
        replaced = False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            if had_original:
                backup_fd, backup_name = tempfile.mkstemp(
                    dir=str(directory), suffix=".rollback.bak"
                )
                backup = Path(backup_name)
                with os.fdopen(backup_fd, "wb") as handle:
                    assert previous_raw_bytes is not None
                    handle.write(previous_raw_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(tmp, self._state_path)
            replaced = True
            _fsync_directory(directory)
            stat = self._state_path.stat()
            return _DurableStateSnapshot(
                raw_bytes=raw_bytes,
                revision=_state_revision(raw_bytes),
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
            )
        except BaseException as original_error:
            tmp.unlink(missing_ok=True)
            if replaced:
                try:
                    if had_original and backup is not None:
                        os.replace(backup, self._state_path)
                        backup = None
                    else:
                        self._state_path.unlink(missing_ok=True)
                    _fsync_directory(directory)
                except BaseException as rollback_error:
                    raise RuntimeError(
                        "coordinator state rollback failed after durability fault"
                    ) from rollback_error
            raise original_error
        finally:
            tmp.unlink(missing_ok=True)
            if backup is not None:
                backup.unlink(missing_ok=True)

    def _build_payload(self) -> dict[str, Any]:
        return {
            "schema_version": COORDINATOR_STATE_SCHEMA_VERSION,
            "seq": self._seq,
            "jobs": self._jobs,
            "slices": self._slices,
            "workflows": [run.to_dict() for run in self._workflows],
            "legacy_records": self._legacy_records,
            "reclaim_resets": self._reclaim_resets,
        }

    def _persist(self) -> None:
        payload = self._build_payload()
        try:
            with self._hold_state_transaction_lock():
                current_snapshot = self._read_durable_snapshot()
                expected_revision = self._loaded_revision
                if current_snapshot.revision != expected_revision:
                    self._restore_from_snapshot(current_snapshot, allow_repairs=False)
                    raise RegistryRevisionConflict(
                        expected_revision=expected_revision,
                        actual_revision=current_snapshot.revision,
                        state_path=self.canonical_state_path,
                    )
                if self._loaded_source_schema_version == 1 and current_snapshot.raw_bytes is not None:
                    self._write_v1_backup(current_snapshot.raw_bytes)
                self._pending_previous_raw_bytes = current_snapshot.raw_bytes
                try:
                    written_snapshot = self._write_payload_atomically(payload)
                finally:
                    self._pending_previous_raw_bytes = None
                if not isinstance(written_snapshot, _DurableStateSnapshot):
                    written_snapshot = self._read_durable_snapshot()
                self._record_loaded_snapshot(
                    written_snapshot,
                    source_schema_version=COORDINATOR_STATE_SCHEMA_VERSION,
                )
        except RegistryRevisionConflict:
            raise
        except BaseException:
            # _write_payload_atomically restores the previous durable file on
            # replace/fsync faults. Always reload the exact current durable
            # snapshot here without taking the transaction lock again.
            self._restore_from_snapshot(self._read_durable_snapshot(), allow_repairs=False)
            raise

    def _validate_loaded_job(self, job: object) -> dict[str, Any]:
        if not isinstance(job, dict) or "job_id" not in job or "status" not in job:
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        status = job.get("status")
        if status == "done":
            raise _migration_error(self._state_path, "legacy job status 'done' 已停用")
        if status not in VALID_JOB_STATUSES:
            raise ValueError(
                f"coordinator 狀態檔 job status 非法（fail-closed）: {self._state_path}: {status!r}"
            )
        if "kind" in job and job.get("kind") not in {None, "build", "review"}:
            raise ValueError(f"coordinator 狀態檔 job kind 非法（fail-closed）: {self._state_path}")
        for field in (
            "executor", "session_name", "log_path", "model_id", "independence_domain",
            "workflow_run_id", "workflow_claim_key", "workflow_repo", "workflow_card",
            "workflow_phase", "workflow_repo_root", "workflow_input_root", "source_revision",
            "workflow_sandbox_hash", "workflow_builder_job_id", "workflow_stage_execution_key",
            "workflow_test_policy", "usage_reason", "started_at", "exited_at",
            "review_verdict_channel",
            "runtime_principal", "runtime_mode", "runtime_surface",
            "prompt_path",
            "template_instance",
        ):
            value = job.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"coordinator 狀態檔 {field} 格式錯誤（fail-closed）: {self._state_path}")
        runtime_principal = job.get("runtime_principal")
        if runtime_principal is not None and runtime_principal not in {
            "builder", "reviewer", "gate"
        }:
            raise ValueError(
                f"coordinator 狀態檔 runtime_principal 非法（fail-closed）: {self._state_path}"
            )
        runtime_mode = job.get("runtime_mode")
        if runtime_mode is not None and runtime_mode not in {
            "direct", "systemd-run", "systemd-template"
        }:
            raise ValueError(
                f"coordinator 狀態檔 runtime_mode 非法（fail-closed）: {self._state_path}"
            )
        runtime_surface = job.get("runtime_surface")
        if runtime_surface is not None and runtime_surface not in {
            "builder-codex-home", "reviewer-codex-home"
        }:
            raise ValueError(
                f"coordinator 狀態檔 runtime_surface 非法（fail-closed）: {self._state_path}"
            )
        credential_publish = job.get("credential_publish", False)
        if not isinstance(credential_publish, bool):
            raise ValueError(
                f"coordinator 狀態檔 credential_publish 格式錯誤（fail-closed）: {self._state_path}"
            )
        if credential_publish:
            expected_principal = {
                "builder-codex-home": "builder",
                "reviewer-codex-home": "reviewer",
            }.get(runtime_surface)
            if (
                job.get("executor") != "codex"
                or runtime_mode not in {"systemd-run", "systemd-template"}
                or expected_principal is None
                or runtime_principal != expected_principal
            ):
                raise ValueError(
                    f"coordinator 狀態檔 credential publisher lane 不一致（fail-closed）: {self._state_path}"
                )
        prompt_path = job.get("prompt_path")
        if prompt_path is not None and (
            not isinstance(prompt_path, str)
            or not prompt_path.startswith("/")
            or not Path(prompt_path).name.startswith(".prompt-")
        ):
            raise ValueError(
                f"coordinator 狀態檔 prompt_path 格式錯誤（fail-closed）: {self._state_path}"
            )
        template_instance = job.get("template_instance")
        if template_instance is not None:
            from . import job_workspace

            if (
                not isinstance(template_instance, str)
                or not job_workspace.job_segment_valid(template_instance)
            ):
                raise ValueError(
                    f"coordinator 狀態檔 template_instance 格式錯誤（fail-closed）: {self._state_path}"
                )
        runtime_diagnostic = job.get("runtime_diagnostic")
        if runtime_diagnostic is not None and (
            not isinstance(runtime_diagnostic, dict)
            or set(runtime_diagnostic) != {"reason", "detail", "source", "job_id"}
            or any(
                not isinstance(runtime_diagnostic.get(key), str)
                or not runtime_diagnostic[key]
                for key in runtime_diagnostic
            )
        ):
            raise ValueError(
                f"coordinator 狀態檔 runtime_diagnostic 格式錯誤（fail-closed）: {self._state_path}"
            )
        dispatch_reroute = job.get("dispatch_reroute")
        if dispatch_reroute is not None and (
            not isinstance(dispatch_reroute, dict)
            or set(dispatch_reroute) != {"source", "skipped"}
            or dispatch_reroute.get("source") != "executor-backoff"
            or not isinstance(dispatch_reroute.get("skipped"), list)
            or any(
                not isinstance(item, dict)
                or set(item) != {"executor", "model_id", "retry_after_epoch"}
                or not isinstance(item.get("executor"), str)
                or not item["executor"]
                or not isinstance(item.get("model_id"), str)
                or not item["model_id"]
                or not isinstance(item.get("retry_after_epoch"), (int, float))
                or isinstance(item.get("retry_after_epoch"), bool)
                for item in dispatch_reroute["skipped"]
            )
        ):
            raise ValueError(
                f"coordinator 狀態檔 dispatch_reroute 格式錯誤（fail-closed）: {self._state_path}"
            )
        # trust-root Phase 2a：通道標記只有一個合法字面值。任何其他值都可能是被
        # 改過的狀態檔（想把新 job 偽裝成 legacy 以打開 worktree fallback）→ fail-closed。
        verdict_channel = job.get("review_verdict_channel")
        if verdict_channel is not None and verdict_channel != "spool":
            raise ValueError(
                f"coordinator 狀態檔 review_verdict_channel 非法（fail-closed）: {self._state_path}"
            )
        for field in ("usage", "usage_raw"):
            value = job.get(field)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"coordinator 狀態檔 {field} 格式錯誤（fail-closed）: {self._state_path}")
        sandbox_hash = job.get("workflow_sandbox_hash")
        if sandbox_hash is not None and (
            len(sandbox_hash) != 64
            or any(char not in "0123456789abcdef" for char in sandbox_hash)
        ):
            raise ValueError(
                f"coordinator 狀態檔 workflow_sandbox_hash 格式錯誤（fail-closed）: {self._state_path}"
            )
        stage_execution_key = job.get("workflow_stage_execution_key")
        if stage_execution_key is not None and (
            len(stage_execution_key) != 64
            or any(char not in "0123456789abcdef" for char in stage_execution_key)
        ):
            raise ValueError(
                f"coordinator 狀態檔 workflow_stage_execution_key 格式錯誤（fail-closed）: {self._state_path}"
            )
        for field in ("pid", "exit_code"):
            value = job.get(field)
            if value is not None and not isinstance(value, int):
                raise ValueError(f"coordinator 狀態檔 {field} 格式錯誤（fail-closed）: {self._state_path}")
        for field in ("subject_head",):
            value = job.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"coordinator 狀態檔 {field} 格式錯誤（fail-closed）: {self._state_path}")
        evidence = job.get("workflow_evidence")
        if evidence is not None and (
            not isinstance(evidence, dict)
            or set(evidence) != {"kind", "path", "hash"}
            or any(not isinstance(evidence.get(key), str) or not evidence[key] for key in evidence)
            or len(str(evidence.get("hash", ""))) != 64
            or any(char not in "0123456789abcdef" for char in str(evidence.get("hash", "")))
        ):
            raise ValueError(
                f"coordinator 狀態檔 workflow_evidence 格式錯誤（fail-closed）: {self._state_path}"
            )
        # #384：executor 失敗的 typed 分類（provider_outcome.py）。舊狀態檔沒有
        # 這個欄位（None）；有的話必須帶齊四個必要鍵，鍵值型別比照
        # ProviderFailureClassification.to_dict()。
        # #499：`reset_at`（provider 給的權威限流重置時刻，epoch 秒）為可選第五
        # 鍵——只有結構化限流證據帶得到，故四鍵形狀的舊狀態檔仍原樣合法。
        provider_outcome = job.get("provider_outcome")
        if provider_outcome is not None and (
            not isinstance(provider_outcome, dict)
            or not {"outcome", "authority", "reason", "retryable"} <= set(provider_outcome)
            or set(provider_outcome) - {"outcome", "authority", "reason", "retryable", "reset_at"}
            or not isinstance(provider_outcome.get("outcome"), str)
            or not provider_outcome["outcome"]
            or not isinstance(provider_outcome.get("authority"), str)
            or not provider_outcome["authority"]
            or not isinstance(provider_outcome.get("reason"), str)
            or not provider_outcome["reason"]
            or not isinstance(provider_outcome.get("retryable"), bool)
            or (
                provider_outcome.get("reset_at") is not None
                and (
                    not isinstance(provider_outcome.get("reset_at"), int)
                    or isinstance(provider_outcome.get("reset_at"), bool)
                )
            )
        ):
            raise ValueError(
                f"coordinator 狀態檔 provider_outcome 格式錯誤（fail-closed）: {self._state_path}"
            )
        provider_outcome_reset_parser = job.get("provider_outcome_reset_parser")
        if provider_outcome_reset_parser is not None and (
            provider_outcome is None
            or provider_outcome.get("reset_at") is None
            or not isinstance(provider_outcome_reset_parser, dict)
            or set(provider_outcome_reset_parser)
            != {"rule_version", "timezone", "base_year", "base_reference_epoch"}
            or not isinstance(provider_outcome_reset_parser.get("rule_version"), str)
            or not provider_outcome_reset_parser["rule_version"]
            or not isinstance(provider_outcome_reset_parser.get("timezone"), str)
            or not provider_outcome_reset_parser["timezone"]
            or not isinstance(provider_outcome_reset_parser.get("base_year"), int)
            or isinstance(provider_outcome_reset_parser.get("base_year"), bool)
            or not isinstance(
                provider_outcome_reset_parser.get("base_reference_epoch"), (int, float)
            )
            or isinstance(provider_outcome_reset_parser.get("base_reference_epoch"), bool)
        ):
            raise ValueError(
                f"coordinator 狀態檔 provider_outcome_reset_parser 格式錯誤（fail-closed）: {self._state_path}"
            )
        for field in ("workflow_inputs", "workflow_outputs"):
            value = job.get(field)
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, str) or not item for item in value)
            ):
                raise ValueError(
                    f"coordinator 狀態檔 {field} 格式錯誤（fail-closed）: {self._state_path}"
                )
        input_snapshot = job.get("workflow_input_snapshot", [])
        if not isinstance(input_snapshot, list):
            raise ValueError(
                f"coordinator 狀態檔 workflow_input_snapshot 格式錯誤（fail-closed）: {self._state_path}"
            )
        snapshot_keys: set[tuple[str, str]] = set()
        for row in input_snapshot:
            if (
                not isinstance(row, dict)
                or set(row) != {"pattern", "path", "sha256", "authority", "content_ref"}
                or any(not isinstance(row.get(key), str) or not row[key] for key in row)
                or Path(row["path"]).is_absolute()
                or ".." in Path(row["path"]).parts
                or Path(row["path"]).as_posix() != row["path"]
                or len(row["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in row["sha256"])
                or row["authority"] not in {"planning-authority", "worktree"}
                or not Path(row["content_ref"]).is_absolute()
                or (row["pattern"], row["path"]) in snapshot_keys
            ):
                raise ValueError(
                    f"coordinator 狀態檔 workflow_input_snapshot 格式錯誤（fail-closed）: {self._state_path}"
                )
            snapshot_keys.add((row["pattern"], row["path"]))
        output_baseline = job.get("workflow_output_baseline", [])
        if not isinstance(output_baseline, list):
            raise ValueError(
                f"coordinator 狀態檔 workflow_output_baseline 格式錯誤（fail-closed）: {self._state_path}"
            )
        baseline_paths: set[str] = set()
        for row in output_baseline:
            if (
                not isinstance(row, dict)
                or set(row) != {"path", "sha256"}
                or not isinstance(row.get("path"), str)
                or not row["path"]
                or Path(row["path"]).is_absolute()
                or ".." in Path(row["path"]).parts
                or Path(row["path"]).as_posix() != row["path"]
                or not isinstance(row.get("sha256"), str)
                or len(row["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in row["sha256"])
                or row["path"] in baseline_paths
            ):
                raise ValueError(
                    f"coordinator 狀態檔 workflow_output_baseline 格式錯誤（fail-closed）: {self._state_path}"
                )
            baseline_paths.add(row["path"])
        validated_job = dict(job)
        if "supersession" in job:
            validated_job["supersession"] = _validate_job_supersession(
                job.get("supersession"),
                state_path=self._state_path,
            )
            if validated_job["supersession"]["job_id"] != validated_job["job_id"]:
                raise _contract_error("request-content-conflict", state_path=self._state_path, detail="supersession.job_id")
        if "consumption" in job:
            validated_job["consumption"] = _validate_job_consumption(
                job.get("consumption"),
                state_path=self._state_path,
            )
            if validated_job["consumption"]["job_id"] != validated_job["job_id"]:
                raise _contract_error("request-content-conflict", state_path=self._state_path, detail="consumption.job_id")
        return validated_job

    def _validate_loaded_slice(self, slice_row: object, job_ids: set[str]) -> dict[str, Any]:
        if not isinstance(slice_row, dict):
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        required = {
            "slice_id",
            "spec",
            "plan",
            "target_branch",
            "target_remote",
            "dispatch_base",
            "builder_job_id",
            "reviewer_job_id",
            "candidate",
            "state",
            "gate_state",
            "verification",
            "current_evidence_refs",
            "current_evaluation_refs",
            "evidence_history",
            "evaluation_history",
            "actions",
            "created_at",
            "updated_at",
        }
        if not required.issubset(slice_row.keys()):
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        if not isinstance(slice_row["slice_id"], str) or not slice_row["slice_id"]:
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        for key in ("spec", "plan"):
            meta = slice_row[key]
            if not (
                isinstance(meta, dict)
                and isinstance(meta.get("path"), str)
                and meta["path"]
                and isinstance(meta.get("hash"), str)
                and meta["hash"]
            ):
                raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        if slice_row["state"] not in VALID_SLICE_STATES:
            raise ValueError(f"coordinator 狀態檔 slice state 非法（fail-closed）: {self._state_path}")
        if slice_row["gate_state"] not in VALID_GATE_STATES:
            raise ValueError(f"coordinator 狀態檔 gate_state 非法（fail-closed）: {self._state_path}")
        if not isinstance(slice_row["target_branch"], str) or not slice_row["target_branch"]:
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        if not isinstance(slice_row["target_remote"], str) or not slice_row["target_remote"]:
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        if slice_row.get("dispatch_base") is not None and (
            not isinstance(slice_row.get("dispatch_base"), str) or not slice_row["dispatch_base"]
        ):
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        if slice_row.get("candidate") is not None and (
            not isinstance(slice_row.get("candidate"), str) or not slice_row["candidate"]
        ):
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        verification_meta = slice_row["verification"]
        if not (
            isinstance(verification_meta, dict)
            and isinstance(verification_meta.get("hash"), str)
            and verification_meta["hash"]
        ):
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        _validate_slice_job_ref_in_state(
            field="builder_job_id",
            job_id=slice_row["builder_job_id"],
            job_ids=job_ids,
            state_path=self._state_path,
        )
        _validate_slice_job_ref_in_state(
            field="reviewer_job_id",
            job_id=slice_row["reviewer_job_id"],
            job_ids=job_ids,
            state_path=self._state_path,
        )
        if not _is_ref_list(slice_row["current_evidence_refs"]) or not _is_ref_list(
            slice_row["current_evaluation_refs"]
        ):
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        for key in ("evidence_history", "evaluation_history", "actions"):
            if not isinstance(slice_row[key], list) or not all(
                isinstance(item, dict) for item in slice_row[key]
            ):
                raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        normalized_slice = _normalize_loaded_slice_verification(slice_row)
        additive_fields = _validate_optional_recovery_slice_fields(
            normalized_slice,
            state_path=self._state_path,
        )
        return {
            **normalized_slice,
            "spec": dict(slice_row["spec"]),
            "plan": dict(slice_row["plan"]),
            "verification": dict(normalized_slice["verification"]),
            "current_evidence_refs": list(slice_row["current_evidence_refs"]),
            "current_evaluation_refs": list(slice_row["current_evaluation_refs"]),
            "evidence_history": _copy_json_list(slice_row["evidence_history"]),
            "evaluation_history": _copy_json_list(slice_row["evaluation_history"]),
            "actions": _copy_json_list(slice_row["actions"]),
            **additive_fields,
        }

    def _lookup_with_cross_instance_visibility(self, lookup: Callable[[], Any]) -> Any:
        expected_revision = self._loaded_revision
        try:
            return lookup()
        except KeyError as missing:
            snapshot = self._read_durable_snapshot()
            if (
                snapshot.revision == self._loaded_revision
                and snapshot.mtime_ns == self._state_mtime_ns
                and snapshot.size == self._state_size
            ):
                raise missing
            self._restore_from_snapshot(snapshot, allow_repairs=False)
            try:
                visible = lookup()
            except KeyError:
                raise missing
            if snapshot.revision != expected_revision:
                raise RegistryRevisionConflict(
                    expected_revision=expected_revision,
                    actual_revision=snapshot.revision,
                    state_path=self.canonical_state_path,
                )
            return visible

    def _find_job_in_memory(self, job_id: str) -> dict[str, Any]:
        for job in self._jobs:
            if job["job_id"] == job_id:
                return job
        raise KeyError(f"job 不存在: {job_id}")

    def _find_job(self, job_id: str) -> dict[str, Any]:
        return self._lookup_with_cross_instance_visibility(
            lambda: self._find_job_in_memory(job_id)
        )

    def _find_slice_in_memory(self, slice_id: str) -> dict[str, Any]:
        for slice_row in self._slices:
            if slice_row["slice_id"] == slice_id:
                return slice_row
        raise KeyError(f"slice 不存在: {slice_id}")

    def _find_slice(self, slice_id: str) -> dict[str, Any]:
        return self._lookup_with_cross_instance_visibility(
            lambda: self._find_slice_in_memory(slice_id)
        )

    def _copy_slice(self, slice_row: dict[str, Any]) -> dict[str, Any]:
        return _deepcopy_json(slice_row)

    def _slice_is_versioned(self, slice_row: Mapping[str, Any]) -> bool:
        return "binding_version" in slice_row and "binding_revision" in slice_row

    def _next_binding_revision(self, current_revision: int) -> int:
        if current_revision >= _MAX_SAFE_JSON_INTEGER:
            raise ValueError("binding_revision exhausted")
        return current_revision + 1

    def _require_versioned_slice_binding(self, slice_row: Mapping[str, Any]) -> dict[str, Any]:
        if not self._slice_is_versioned(slice_row):
            raise _contract_error("legacy-binding-unversioned", state_path=self._state_path)
        return _slice_binding_from_row(slice_row)

    def _maybe_bump_binding_revision(
        self,
        slice_row: dict[str, Any],
        *,
        previous_binding: dict[str, Any] | None,
        force: bool = False,
    ) -> None:
        if not self._slice_is_versioned(slice_row):
            return
        current_binding = _slice_binding_from_row(slice_row)
        if force or previous_binding != current_binding:
            slice_row["binding_revision"] = self._next_binding_revision(int(slice_row["binding_revision"]))

    def _locate_registry_request_receipt(
        self,
        request_id: str,
    ) -> tuple[dict[str, Any], str, dict[str, Any], int | None] | None:
        for slice_row in self._slices:
            receipts = slice_row.get("recovery_receipts")
            if isinstance(receipts, list):
                for index, receipt in enumerate(receipts):
                    if isinstance(receipt, dict) and receipt.get("request_id") == request_id:
                        return slice_row, "recovery", receipt, index
            checkpoint = slice_row.get("binding_checkpoint_receipt")
            if isinstance(checkpoint, dict) and checkpoint.get("request_id") == request_id:
                return slice_row, "checkpoint", checkpoint, None
        return None

    def _ensure_matching_request_receipt(
        self,
        located: tuple[dict[str, Any], str, dict[str, Any], int | None],
        *,
        expected_kind: str,
        request: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], int | None]:
        slice_row, kind, receipt, index = located
        if (
            kind != expected_kind
            or receipt.get("request_digest") != request["request_digest"]
            or receipt.get("payload") != request["payload"]
        ):
            raise _contract_error("request-content-conflict", state_path=self._state_path, detail="request_id")
        return slice_row, receipt, index

    def _revalidate_recovery_binding(
        self,
        slice_row: dict[str, Any],
        *,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        current_binding = self._require_versioned_slice_binding(slice_row)
        expected_binding = request["payload"]["expected_binding"]
        if current_binding != expected_binding:
            raise _contract_error("stale-binding", state_path=self._state_path)
        for field_name in ("builder_job_id", "reviewer_job_id"):
            job_id = expected_binding[field_name]
            if job_id is not None:
                self._validate_existing_job_ref(field_name, job_id)
        return current_binding

    def _revalidate_legacy_checkpoint_snapshot(
        self,
        slice_row: dict[str, Any],
        *,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self._slice_is_versioned(slice_row):
            raise _contract_error("legacy-checkpoint-not-applicable", state_path=self._state_path)
        expected_row = request["payload"]["expected_legacy_row"]
        current_row = self._copy_slice(slice_row)
        if _canonical_json_bytes(current_row) != _canonical_json_bytes(expected_row):
            raise _contract_error("stale-binding", state_path=self._state_path)
        current_binding = _legacy_binding_from_row(slice_row)
        if current_binding != request["payload"]["expected_legacy_binding"]:
            raise _contract_error("stale-binding", state_path=self._state_path)
        current_job_refs = {
            "builder_job_id": slice_row["builder_job_id"],
            "reviewer_job_id": slice_row["reviewer_job_id"],
        }
        if current_job_refs != request["payload"]["expected_job_refs"]:
            raise _contract_error("stale-binding", state_path=self._state_path)
        current_fingerprint = _compute_checkpoint_fingerprint(
            expected_legacy_row=current_row,
            expected_legacy_binding=current_binding,
            expected_job_refs=current_job_refs,
        )
        if current_fingerprint != request["payload"]["legacy_snapshot_fingerprint"]:
            raise _contract_error("stale-binding", state_path=self._state_path)
        for field_name, job_id in current_job_refs.items():
            if job_id is not None:
                self._validate_existing_job_ref(field_name, job_id)
        return current_binding

    def _legacy_job_disposition_binding_witness(
        self,
        slice_row: Mapping[str, Any],
        *,
        job_id: str,
        binding_revision: int,
        detail: str,
    ) -> dict[str, Any] | None:
        witness: dict[str, Any] | None = None
        for candidate in _slice_binding_witnesses(slice_row):
            if int(candidate["binding_revision"]) != binding_revision or not _job_id_in_binding_snapshot(job_id, candidate):
                continue
            normalized = _deepcopy_json(candidate)
            if witness is None:
                witness = normalized
            elif witness != normalized:
                raise _contract_error("request-content-conflict", state_path=self._state_path, detail=detail)
        return witness

    def _recorded_job_bound_binding_details(
        self,
        job: Mapping[str, Any],
        *,
        slice_row: Mapping[str, Any],
        slice_id: str,
        binding_revision: int,
        mutate_missing: bool,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        job_id = str(job["job_id"])
        explicit_witness: dict[str, Any] | None = None
        missing_entries: list[dict[str, Any]] = []
        for field_name in ("supersession", "consumption"):
            entry = job.get(field_name)
            if not isinstance(entry, Mapping):
                continue
            if entry.get("slice_id") != slice_id or int(entry["binding_revision"]) != binding_revision:
                continue
            if _BOUND_BINDING_FIELD not in entry:
                if isinstance(entry, dict):
                    missing_entries.append(entry)
                continue
            bound_binding = entry.get(_BOUND_BINDING_FIELD)
            if not isinstance(bound_binding, Mapping):
                raise _contract_error(
                    "request-content-conflict",
                    state_path=self._state_path,
                    detail=f"{field_name}.{_BOUND_BINDING_FIELD}",
                )
            normalized = _deepcopy_json(bound_binding)
            if (
                int(normalized["binding_revision"]) != binding_revision
                or not _job_id_in_binding_snapshot(job_id, normalized)
            ):
                raise _contract_error(
                    "request-content-conflict",
                    state_path=self._state_path,
                    detail=f"{field_name}.{_BOUND_BINDING_FIELD}",
                )
            if explicit_witness is None:
                explicit_witness = normalized
            elif explicit_witness != normalized:
                raise _contract_error(
                    "request-content-conflict",
                    state_path=self._state_path,
                    detail=f"{field_name}.{_BOUND_BINDING_FIELD}",
                )
        witness = explicit_witness
        if witness is None and missing_entries:
            witness = self._legacy_job_disposition_binding_witness(
                slice_row,
                job_id=job_id,
                binding_revision=binding_revision,
                detail="binding",
            )
        if witness is None:
            return None, missing_entries
        if mutate_missing:
            for entry in missing_entries:
                entry[_BOUND_BINDING_FIELD] = _deepcopy_json(witness)
        return witness, missing_entries

    def _recorded_job_bound_binding(
        self,
        job: Mapping[str, Any],
        *,
        slice_row: Mapping[str, Any],
        slice_id: str,
        binding_revision: int,
    ) -> dict[str, Any] | None:
        witness, _missing_entries = self._recorded_job_bound_binding_details(
            job,
            slice_row=slice_row,
            slice_id=slice_id,
            binding_revision=binding_revision,
            mutate_missing=True,
        )
        return witness

    def _stage_current_binding_disposition_backfills(
        self,
        *,
        slice_row: Mapping[str, Any],
        current_binding: Mapping[str, Any],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        slice_id = str(slice_row["slice_id"])
        binding_revision = int(current_binding["binding_revision"])
        staged: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for job in self._jobs:
            witness, missing_entries = self._recorded_job_bound_binding_details(
                job,
                slice_row=slice_row,
                slice_id=slice_id,
                binding_revision=binding_revision,
                mutate_missing=False,
            )
            if witness is None:
                if missing_entries:
                    raise _contract_error(
                        "request-content-conflict",
                        state_path=self._state_path,
                        detail="binding",
                    )
                continue
            if witness != current_binding:
                raise _contract_error(
                    "request-content-conflict",
                    state_path=self._state_path,
                    detail="binding",
                )
            for entry in missing_entries:
                staged.append((entry, witness))
        return staged

    def _resolve_job_disposition_binding(
        self,
        job: Mapping[str, Any],
        *,
        slice_row: Mapping[str, Any],
        binding_revision: int,
    ) -> dict[str, Any]:
        if not self._slice_is_versioned(slice_row):
            raise _contract_error("legacy-binding-unversioned", state_path=self._state_path)
        current_binding = _slice_binding_from_row(slice_row)
        current_revision = int(current_binding["binding_revision"])
        if binding_revision == current_revision:
            if not _job_id_in_binding_snapshot(str(job["job_id"]), current_binding):
                raise _contract_error(
                    "request-content-conflict",
                    state_path=self._state_path,
                    detail="job_id",
                )
            return current_binding
        if binding_revision > current_revision:
            raise _contract_error("stale-binding", state_path=self._state_path)
        witness = self._recorded_job_bound_binding(
            job,
            slice_row=slice_row,
            slice_id=str(slice_row["slice_id"]),
            binding_revision=binding_revision,
        )
        if witness is None:
            raise _contract_error("stale-binding", state_path=self._state_path)
        return witness

    def _validate_loaded_job_disposition_binding(
        self,
        job: Mapping[str, Any],
        *,
        field_name: str,
        entry: Mapping[str, Any],
        slice_row: Mapping[str, Any],
    ) -> None:
        if not self._slice_is_versioned(slice_row):
            raise _contract_error("legacy-binding-unversioned", state_path=self._state_path)
        current_binding = _slice_binding_from_row(slice_row)
        entry_revision = int(entry["binding_revision"])
        if entry_revision > int(current_binding["binding_revision"]):
            raise _contract_error(
                "request-content-conflict",
                state_path=self._state_path,
                detail=f"{field_name}.binding",
            )
        bound_binding = self._recorded_job_bound_binding(
            job,
            slice_row=slice_row,
            slice_id=str(entry["slice_id"]),
            binding_revision=entry_revision,
        )
        if bound_binding is None:
            raise _contract_error(
                "request-content-conflict",
                state_path=self._state_path,
                detail=f"{field_name}.binding",
            )
        if entry_revision == int(current_binding["binding_revision"]) and bound_binding != current_binding:
            raise _contract_error(
                "request-content-conflict",
                state_path=self._state_path,
                detail=f"{field_name}.{_BOUND_BINDING_FIELD}",
            )

    def _persist_recovery_change(self) -> None:
        try:
            self._persist()
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"recovery-persistence-failed: {exc}") from exc

    def _build_job_supersession(
        self,
        job: Mapping[str, Any],
        *,
        slice_id: str,
        binding_revision: int,
        bound_binding: Mapping[str, Any],
        actor: str,
        reason: str,
        superseding_identity: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]:
        return {
            "version": _JOB_SUPERSESSION_VERSION,
            "job_id": job["job_id"],
            "slice_id": slice_id,
            "binding_revision": binding_revision,
            "actor": actor,
            "at": at,
            "reason": reason,
            "superseding_identity": _validate_disposition_identity(
                superseding_identity,
                label="job supersession.superseding_identity",
                state_path=self._state_path,
            ),
            _BOUND_BINDING_FIELD: _validate_slice_binding_snapshot(
                bound_binding,
                label=f"job supersession.{_BOUND_BINDING_FIELD}",
                state_path=self._state_path,
            ),
        }

    def _build_job_consumption(
        self,
        job: Mapping[str, Any],
        *,
        slice_id: str,
        binding_revision: int,
        bound_binding: Mapping[str, Any],
        actor: str,
        completion_identity: Mapping[str, Any],
        proof_refs: list[Mapping[str, Any]],
        at: str,
    ) -> dict[str, Any]:
        return {
            "version": _JOB_CONSUMPTION_VERSION,
            "job_id": job["job_id"],
            "slice_id": slice_id,
            "binding_revision": binding_revision,
            "actor": actor,
            "at": at,
            "completion_identity": _validate_disposition_identity(
                completion_identity,
                label="job consumption.completion_identity",
                state_path=self._state_path,
            ),
            "proof_refs": _validate_proof_refs(
                proof_refs,
                label="job consumption.proof_refs",
                state_path=self._state_path,
                required=True,
            ),
            _BOUND_BINDING_FIELD: _validate_slice_binding_snapshot(
                bound_binding,
                label=f"job consumption.{_BOUND_BINDING_FIELD}",
                state_path=self._state_path,
            ),
        }

    def _record_job_supersession_inplace(
        self,
        job: dict[str, Any],
        *,
        slice_id: str,
        binding_revision: int,
        bound_binding: Mapping[str, Any],
        actor: str,
        reason: str,
        superseding_identity: Mapping[str, Any],
        at: str,
        at_was_omitted: bool = False,
    ) -> bool:
        supersession = self._build_job_supersession(
            job,
            slice_id=slice_id,
            binding_revision=binding_revision,
            bound_binding=bound_binding,
            actor=actor,
            reason=reason,
            superseding_identity=superseding_identity,
            at=at,
        )
        existing = job.get("supersession")
        if existing is not None:
            if existing == supersession or (at_was_omitted and _same_disposition_except_at(existing, supersession)):
                return False
            raise _contract_error("request-content-conflict", state_path=self._state_path, detail="supersession")
        job["supersession"] = supersession
        return True

    def _record_job_consumption_inplace(
        self,
        job: dict[str, Any],
        *,
        slice_id: str,
        binding_revision: int,
        bound_binding: Mapping[str, Any],
        actor: str,
        completion_identity: Mapping[str, Any],
        proof_refs: list[Mapping[str, Any]],
        at: str,
        at_was_omitted: bool = False,
    ) -> bool:
        consumption = self._build_job_consumption(
            job,
            slice_id=slice_id,
            binding_revision=binding_revision,
            bound_binding=bound_binding,
            actor=actor,
            completion_identity=completion_identity,
            proof_refs=proof_refs,
            at=at,
        )
        existing = job.get("consumption")
        if existing is not None:
            if existing == consumption or (at_was_omitted and _same_disposition_except_at(existing, consumption)):
                return False
            raise _contract_error("request-content-conflict", state_path=self._state_path, detail="consumption")
        job["consumption"] = consumption
        return True

    def _build_recovery_receipt(
        self,
        request: Mapping[str, Any],
        *,
        phase: str,
        prepared_at: str,
        completed_at: str | None = None,
        applied_binding: Mapping[str, Any] | None = None,
        step_receipts: list[Mapping[str, Any]] | None = None,
        result: str | None = None,
    ) -> dict[str, Any]:
        required_steps = list(request["payload"]["required_steps"])
        return {
            "version": _RECOVERY_RECEIPT_VERSION,
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "payload": _deepcopy_json(request["payload"]),
            "phase": phase,
            "prepared_at": prepared_at,
            "completed_at": completed_at,
            "applied_binding": None if applied_binding is None else _deepcopy_json(applied_binding),
            "affected_job_ids": _expected_affected_job_ids(request["payload"]["expected_binding"]),
            "required_steps": required_steps,
            "step_receipts": [] if step_receipts is None else _deepcopy_json(step_receipts),
            "result": result,
        }

    def _build_checkpoint_receipt(
        self,
        request: Mapping[str, Any],
        *,
        checkpointed_at: str,
        applied_binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "version": _CHECKPOINT_RECEIPT_VERSION,
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "payload": _deepcopy_json(request["payload"]),
            "phase": "complete",
            "checkpointed_at": checkpointed_at,
            "applied_binding": _deepcopy_json(applied_binding),
            "provenance": _deepcopy_json(request["payload"]["provenance"]),
            "result": "checkpoint-created",
        }

    def _validate_existing_job_ref(self, field: str, job_id: str | None) -> None:
        if job_id is None:
            return
        try:
            self._find_job(job_id)
        except KeyError as exc:
            raise ValueError(f"{field} 指向不存在 job: {job_id}") from exc

    def _allocate_job_id(self, task: str) -> str:
        """配發下一個 job_id。**全 repo 唯一**的 job_id 產生點。

        `create_job()` 與 `reserve_job_id()` 都走這裡：兩邊各自拼一次
        `f"{task}-{seq}"` 就是兩個會漂移的來源，而那正是 #645 的形狀。
        """

        self._seq += 1
        return f"{task}-{self._seq}"

    def reserve_job_id(self, task: str) -> str:
        """先把 job_id 配發出來（並持久化水位），稍後再以它建立 job（#648）。

        存在的理由只有一個：**per-job 工作區的目錄名就是 job_id**
        （`job_workspace.job_segment()`），而工作區必須在 `create_job()`
        **之前**存在——canonical lane 的 `workflow_input_snapshot` /
        `workflow_output_baseline` 都是從工作區的檔案算出來的，是 `create_job()`
        的必填參數。順序因此只能是「先配 id → 建工作區 → 建 job」。

        配發即消耗：`self._seq` 前進並落盤，因此即使後續 provision 失敗、這個 id
        永遠不會被第二個 job 取用（只是燒掉一個序號，無害）。呼叫端必須把拿到的
        id 原樣交回 `create_job(job_id=…)`；那裡會驗證它確實屬於同一個 `task`、
        且尚未被使用。

        **不是**「預測下一個 id」——預測會在任何一次插入之後漂掉。這裡是真的把
        序號取走。
        """

        if not isinstance(task, str) or not task:
            raise ValueError("reserve_job_id 需要非空 task")
        job_id = self._allocate_job_id(task)
        self._persist()
        return job_id

    def create_job(
        self,
        *,
        task: str,
        persona: str,
        branch: str,
        pane: str,
        worktree: str,
        job_id: str | None = None,
        dispatch_head: str | None = None,
        executor: str | None = None,
        session_name: str | None = None,
        pid: int | None = None,
        log_path: str | None = None,
        exit_code: int | None = None,
        kind: str = "build",
        model_id: str | None = None,
        independence_domain: str | None = None,
        subject_head: str | None = None,
        spec_hash: str | None = None,
        plan_hash: str | None = None,
        verification_hash: str | None = None,
        workflow_run_id: str | None = None,
        workflow_claim_key: str | None = None,
        workflow_repo: str | None = None,
        owner_identity: Mapping[str, str] | None = None,
        attempt_id: str | None = None,
        workflow_card: str | None = None,
        workflow_phase: str | None = None,
        workflow_repo_root: str | None = None,
        workflow_input_root: str | None = None,
        workflow_inputs: tuple[str, ...] = (),
        workflow_input_snapshot: tuple[dict[str, str], ...] = (),
        workflow_outputs: tuple[str, ...] = (),
        source_revision: str | None = None,
        workflow_sandbox_hash: str | None = None,
        workflow_output_baseline: tuple[dict[str, str], ...] = (),
        workflow_builder_job_id: str | None = None,
        workflow_stage_execution_key: str | None = None,
        workflow_test_policy: str | None = None,
        review_verdict_channel: str | None = None,
        runtime_principal: str | None = None,
        runtime_mode: str | None = None,
        runtime_surface: str | None = None,
        credential_publish: bool = False,
        prompt_path: str | None = None,
        dispatch_reroute: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if persona == "builder" and any(
            job.get("task") == task
            and job.get("persona") == "builder"
            and job.get("status") in ACTIVE_JOB_STATUSES
            for job in self._jobs
        ):
            raise ValueError(f"slice 已有 active builder，不可重複派工: {task}")
        if kind not in {"build", "review"}:
            raise ValueError(f"非法 kind: {kind!r}")
        normalized_owner = _normalize_owner_identity(owner_identity, state_path=self._state_path)
        if normalized_owner is not None and normalized_owner["slice_id"] != task:
            raise ValueError("owner_identity.slice_id 必須符合 job task")
        if attempt_id is not None and (not isinstance(attempt_id, str) or not attempt_id.strip()):
            raise ValueError("attempt_id 必須為非空字串")
        self._validate_existing_job_ref("workflow_builder_job_id", workflow_builder_job_id)
        if job_id is None:
            allocated = self._allocate_job_id(task)
        else:
            # #648：只接受**本 registry 配發過**的 id。放寬成「任意字串」等於讓
            # 呼叫端自己造 job_id，那既繞過序號單調性，也讓工作區目錄名與 registry
            # 身分脫鉤（#645 的復發面）。
            prefix = f"{task}-"
            suffix = job_id[len(prefix):] if job_id.startswith(prefix) else ""
            if not suffix.isdigit() or int(suffix) > self._seq:
                raise ValueError(f"create_job 收到未經配發的 job_id: {job_id!r}（task={task!r}）")
            if any(existing.get("job_id") == job_id for existing in self._jobs):
                raise ValueError(f"create_job 收到已被使用的 job_id: {job_id!r}")
            allocated = job_id
        job: dict[str, Any] = {
            "job_id": allocated,
            "task": task,
            "persona": persona,
            "kind": kind,
            "branch": branch,
            "pane": pane,
            "worktree": worktree,
            "status": "dispatched",
            "dispatch_head": dispatch_head,
            "executor": executor,
            "model_id": model_id,
            "independence_domain": independence_domain,
            "session_name": session_name,
            "pid": pid,
            "log_path": log_path,
            # Template launches persist the exact Manager-issued `%i` here so
            # harvest never has to guess from job_id/session/log payload text.
            "template_instance": None,
            # Template launches persist a separate Manager-only completion
            # anchor because their canonical JSONL log lives in a job-writable
            # spool.  Legacy/direct rows leave this unset and use log_path.
            "control_log_path": None,
            "exit_code": exit_code,
            "subject_head": subject_head,
            "spec_hash": spec_hash,
            "plan_hash": plan_hash,
            "verification_hash": verification_hash,
            "workflow_run_id": workflow_run_id,
            "workflow_claim_key": workflow_claim_key,
            "workflow_repo": workflow_repo,
            "owner_identity": normalized_owner,
            "attempt_id": attempt_id,
            "workflow_card": workflow_card,
            "workflow_phase": workflow_phase,
            "workflow_repo_root": workflow_repo_root,
            "workflow_input_root": workflow_input_root,
            "workflow_inputs": list(workflow_inputs),
            "workflow_input_snapshot": [dict(row) for row in workflow_input_snapshot],
            "workflow_outputs": list(workflow_outputs),
            "source_revision": source_revision,
            "workflow_sandbox_hash": workflow_sandbox_hash,
            "workflow_output_baseline": [dict(row) for row in workflow_output_baseline],
            "workflow_builder_job_id": workflow_builder_job_id,
            "workflow_stage_execution_key": workflow_stage_execution_key,
            # #379：派工當下 pin 住的驗收判準快照（deck 卡片的 test_policy），
            # harvest 時與 registry 現有 WorkflowRun.steps 的現值比對，drift 一律
            # fail closed（見 manager._workflow_acceptance_definition_drifted）。
            "workflow_test_policy": workflow_test_policy,
            # trust-root Phase 2a：reviewer job 的 verdict 交付通道。
            # `"spool"` ＝ 以 per-job verdict spool 派工（`<coordinator_root>/
            # review-verdicts/<job_id>/verdict.json`），harvest 端**只**認該落點；
            # `None` ＝ 本修法之前派工的 in-flight job，harvest 端才允許回退讀
            # worktree 內的 legacy verdict（並記 WARN）。舊狀態檔沒有這個欄位，
            # 因此 legacy 判定天然正確，不需要遷移。
            "review_verdict_channel": review_verdict_channel,
            "runtime_principal": runtime_principal,
            "runtime_mode": runtime_mode,
            "runtime_surface": runtime_surface,
            "credential_publish": credential_publish,
            "prompt_path": prompt_path,
            "dispatch_reroute": None if dispatch_reroute is None else dict(dispatch_reroute),
            "runtime_diagnostic": None,
            "workflow_evidence": None,
            # #384：executor 失敗的 typed 分類（見 provider_outcome.py），只在
            # `update_headless_result` 收到失敗結果且能分類時才會被寫入。
            "provider_outcome": None,
            "provider_outcome_reset_parser": None,
            "usage": None,
            "usage_raw": None,
            "usage_reason": None,
            "started_at": None,
            "exited_at": None,
            "created_at": _now_iso(),
        }
        job = self._validate_loaded_job(job)
        self._jobs.append(job)
        self._persist()
        return _deepcopy_json(job)

    def list_jobs(self) -> list[dict[str, Any]]:
        self._reload_if_changed()
        return [_deepcopy_json(job) for job in self._jobs]

    def get_job(self, job_id: str) -> dict[str, Any]:
        self._reload_if_changed()
        return _deepcopy_json(self._find_job(job_id))

    def update_job(
        self,
        job_id: str,
        *,
        worktree: str | None = None,
    ) -> dict[str, Any]:
        job = self._find_job(job_id)
        if worktree is None:
            raise ValueError("update_job 至少需要一個欄位")
        if not isinstance(worktree, str) or not worktree.strip():
            raise ValueError("worktree 必須為非空字串")
        job["worktree"] = worktree
        self._persist()
        return _deepcopy_json(job)

    def bind_workflow_evidence(
        self,
        job_id: str,
        *,
        locator: dict[str, str],
        subject_head: str | None = None,
    ) -> dict[str, Any]:
        job = self._find_job(job_id)
        if job.get("status") != "exited" or job.get("exit_code") != 0:
            raise ValueError("workflow evidence只能綁定successful terminal job")
        if (
            not isinstance(locator, dict)
            or set(locator) != {"kind", "path", "hash"}
            or any(not isinstance(locator.get(key), str) or not locator[key] for key in locator)
            or len(locator.get("hash", "")) != 64
            or any(char not in "0123456789abcdef" for char in locator.get("hash", ""))
        ):
            raise ValueError("workflow evidence locator格式錯誤")
        existing = job.get("workflow_evidence")
        if existing is not None:
            if existing != locator or (subject_head is not None and job.get("subject_head") != subject_head):
                raise ValueError("workflow evidence已綁定且內容衝突")
            return _deepcopy_json(job)
        job["workflow_evidence"] = dict(locator)
        if subject_head is not None:
            job["subject_head"] = subject_head
        self._persist()
        return _deepcopy_json(job)

    def find_reusable_stage_evidence(
        self,
        stage_execution_key: str,
        *,
        is_evidence_still_valid: Callable[[dict[str, str]], bool] | None = None,
    ) -> dict[str, Any] | None:
        """依 StageExecutionKey 查找可重用的既有 workflow evidence（#214）。

        只有『同一個 stage_execution_key』、成功結束（exited/exit_code==0）且
        已綁定 canonical evidence 的既有 job 才是候選；`is_evidence_still_valid`
        是呼叫端注入的驗證 callback（例如確認 evidence 完整、未撤銷、通過
        ResultVerification），本方法不預設任何 evidence 語意。fail-closed：
        key 格式錯誤、找不到候選、或未提供 callback（無法確認 evidence 仍然
        有效）時一律回傳 None，交由呼叫端照原路徑重新派工，不會誤判可以
        reuse。找到多筆候選時取最近一筆（列表尾端）。
        """
        if (
            not isinstance(stage_execution_key, str)
            or len(stage_execution_key) != 64
            or any(char not in "0123456789abcdef" for char in stage_execution_key)
        ):
            return None
        if is_evidence_still_valid is None:
            return None
        self._reload_if_changed()
        for job in reversed(self._jobs):
            if job.get("workflow_stage_execution_key") != stage_execution_key:
                continue
            if job.get("status") != "exited" or job.get("exit_code") != 0:
                continue
            evidence = job.get("workflow_evidence")
            if not isinstance(evidence, dict):
                continue
            try:
                still_valid = is_evidence_still_valid(dict(evidence))
            except Exception:
                continue
            if not still_valid:
                continue
            evidence_hash = evidence.get("hash")
            if (
                not isinstance(evidence_hash, str)
                or len(evidence_hash) != 64
                or any(char not in "0123456789abcdef" for char in evidence_hash)
            ):
                continue
            # evidence_hash 直接對齊 completion.py reused_from schema 的
            # {run_id, job_id, evidence_hash}，消費端不需再自行拆 locator。
            return {
                "run_id": job.get("workflow_run_id"),
                "job_id": job.get("job_id"),
                "evidence": dict(evidence),
                "evidence_hash": evidence_hash,
            }
        return None

    def update_status(self, job_id: str, status: str) -> dict[str, Any]:
        if status not in VALID_JOB_STATUSES:
            raise ValueError(f"非法 status: {status!r}（須為 {sorted(VALID_JOB_STATUSES)} 之一）")
        job = self._find_job(job_id)
        _validate_transition(
            field="job status",
            current=str(job["status"]),
            new=status,
            allowed=JOB_STATUS_TRANSITIONS,
        )
        job["status"] = status
        self._persist()
        return _deepcopy_json(job)

    def attach_launch_handle(
        self,
        job_id: str,
        *,
        executor: str | None = None,
        model_id: str | None = None,
        session_name: str | None = None,
        pid: int | None = None,
        log_path: str | None = None,
        template_instance: str | None = None,
        runtime_principal: str | None = None,
        runtime_mode: str | None = None,
        runtime_surface: str | None = None,
        credential_publish: bool = False,
        prompt_path: str | None = None,
        control_log_path: str | None = None,
    ) -> dict[str, Any]:
        job = self._find_job(job_id)
        if job["status"] not in ACTIVE_JOB_STATUSES:
            raise ValueError(f"僅能為 in-flight job 附加 launch handle: {job_id}")
        job["executor"] = executor
        if model_id is not None:
            job["model_id"] = model_id
        job["session_name"] = session_name
        job["pid"] = pid
        job["log_path"] = log_path
        job["template_instance"] = template_instance
        job["runtime_principal"] = runtime_principal
        job["runtime_mode"] = runtime_mode
        job["runtime_surface"] = runtime_surface
        job["credential_publish"] = credential_publish
        job["prompt_path"] = prompt_path
        job["control_log_path"] = control_log_path
        job["started_at"] = _now_iso()
        self._persist()
        return _deepcopy_json(job)

    def update_headless_result(
        self,
        job_id: str,
        *,
        status: str,
        exit_code: int,
        executor: str | None = None,
        model_id: str | None = None,
        provider_outcome: Mapping[str, Any] | None = None,
        provider_outcome_reset_parser: Mapping[str, Any] | None = None,
        runtime_diagnostic: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError(
                f"headless 完成結果 status 須為 'exited' 或 'failed'，收到: {status!r}"
            )
        # #499：`reset_at` 為可選第五鍵（見 _validate_state 的同一份判準）。
        if provider_outcome is not None and (
            not isinstance(provider_outcome, Mapping)
            or not {"outcome", "authority", "reason", "retryable"} <= set(provider_outcome)
            or set(provider_outcome) - {"outcome", "authority", "reason", "retryable", "reset_at"}
        ):
            raise ValueError("provider_outcome 格式錯誤（fail-closed）")
        if provider_outcome_reset_parser is not None and (
            provider_outcome is None
            or provider_outcome.get("reset_at") is None
            or not isinstance(provider_outcome_reset_parser, Mapping)
            or set(provider_outcome_reset_parser)
            != {"rule_version", "timezone", "base_year", "base_reference_epoch"}
            or not isinstance(provider_outcome_reset_parser.get("rule_version"), str)
            or not provider_outcome_reset_parser["rule_version"]
            or not isinstance(provider_outcome_reset_parser.get("timezone"), str)
            or not provider_outcome_reset_parser["timezone"]
            or not isinstance(provider_outcome_reset_parser.get("base_year"), int)
            or isinstance(provider_outcome_reset_parser.get("base_year"), bool)
            or not isinstance(
                provider_outcome_reset_parser.get("base_reference_epoch"), (int, float)
            )
            or isinstance(provider_outcome_reset_parser.get("base_reference_epoch"), bool)
        ):
            raise ValueError("provider_outcome_reset_parser 格式錯誤（fail-closed）")
        if runtime_diagnostic is not None and (
            not isinstance(runtime_diagnostic, Mapping)
            or set(runtime_diagnostic) != {"reason", "detail", "source", "job_id"}
            or any(
                not isinstance(runtime_diagnostic.get(key), str)
                or not runtime_diagnostic[key]
                for key in runtime_diagnostic
            )
        ):
            raise ValueError("runtime_diagnostic 格式錯誤（fail-closed）")
        job = self._find_job(job_id)
        _validate_transition(
            field="job status",
            current=str(job["status"]),
            new=status,
            allowed=JOB_STATUS_TRANSITIONS,
        )
        job["status"] = status
        job["exit_code"] = exit_code
        if executor is not None:
            job["executor"] = executor
        if model_id is not None:
            job["model_id"] = model_id
        job["exited_at"] = _now_iso()
        # #384：executor 失敗的 typed 分類（見 provider_outcome.py）。只在呼叫端
        # 傳入時才寫入——`status == "exited"` 或呼叫端未提供分類（例如 launch
        # 本身失敗、根本沒有 executor 輸出可分類）時保持 None，不偽造分類。
        job["provider_outcome"] = dict(provider_outcome) if provider_outcome is not None else None
        job["provider_outcome_reset_parser"] = (
            dict(provider_outcome_reset_parser)
            if provider_outcome_reset_parser is not None
            else None
        )
        job["runtime_diagnostic"] = (
            dict(runtime_diagnostic) if runtime_diagnostic is not None else None
        )
        # #325：usage 抽取是盡力而為的附加資訊，任何失敗都不得影響上面已判定
        # 好的 status/exit_code/exited_at——extract_usage 本身已 fail-soft，
        # 這裡再包一層防禦性雙保險。
        try:
            extraction = extract_usage(job.get("executor"), job.get("log_path"))
            job["usage"] = extraction.get("usage")
            job["usage_raw"] = extraction.get("usage_raw")
            job["usage_reason"] = extraction.get("usage_reason")
        except BaseException as exc:  # noqa: BLE001 - 防禦性雙保險，絕不上拋
            job["usage"] = None
            job["usage_raw"] = None
            job["usage_reason"] = f"extractor crashed: {exc}"
        self._persist()
        return _deepcopy_json(job)

    def create_slice(
        self,
        *,
        slice_id: str,
        spec_path: str,
        spec_hash: str,
        plan_path: str,
        plan_hash: str,
        target_branch: str,
        target_remote: str = "origin",
        verification_hash: str | None = None,
        verification: dict[str, Any] | None = None,
        dispatch_base: str | None = None,
        builder_job_id: str | None = None,
        reviewer_job_id: str | None = None,
        candidate: str | None = None,
        owner_identity: Mapping[str, str] | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        if any(row["slice_id"] == slice_id for row in self._slices):
            raise ValueError(f"slice 已存在: {slice_id}")
        self._validate_existing_job_ref("builder_job_id", builder_job_id)
        self._validate_existing_job_ref("reviewer_job_id", reviewer_job_id)
        normalized_owner = _normalize_owner_identity(owner_identity, state_path=self._state_path)
        if normalized_owner is not None and normalized_owner["slice_id"] != slice_id:
            raise ValueError("owner_identity.slice_id 必須符合 slice_id")
        if attempt_id is not None and (not isinstance(attempt_id, str) or not attempt_id.strip()):
            raise ValueError("attempt_id 必須為非空字串")
        now = _now_iso()
        slice_row = {
            "slice_id": slice_id,
            "owner_identity": normalized_owner,
            "attempt_id": attempt_id,
            "spec": {"path": spec_path, "hash": spec_hash},
            "plan": {"path": plan_path, "hash": plan_hash},
            "target_branch": target_branch,
            "target_remote": target_remote,
            "binding_version": _SLICE_BINDING_VERSION,
            "binding_revision": 1,
            "verification": {
                "hash": verification_hash or ("0" * 64),
                "contract": dict(verification) if isinstance(verification, dict) else None,
            },
            "dispatch_base": dispatch_base,
            "builder_job_id": builder_job_id,
            "reviewer_job_id": reviewer_job_id,
            "candidate": candidate,
            "state": "pending",
            "gate_state": "pending",
            _CURRENT_VERIFICATION_EVIDENCE_HASH: None,
            "current_evidence_refs": [],
            "current_evaluation_refs": [],
            "evidence_history": [],
            "evaluation_history": [],
            "actions": [],
            "recovery_receipts": [],
            "created_at": now,
            "updated_at": now,
        }
        self._slices.append(slice_row)
        self._persist()
        return self._copy_slice(slice_row)

    def repin_slice(
        self,
        slice_id: str,
        *,
        spec_path: str,
        spec_hash: str,
        plan_path: str,
        plan_hash: str,
        target_branch: str,
        target_remote: str,
        verification_hash: str,
        verification: dict[str, Any] | None,
        dispatch_base: str | None,
        owner_identity: Mapping[str, str] | None = None,
        attempt_id: str | None = None,
        replace_owner_identity: bool = False,
    ) -> dict[str, Any]:
        slice_row = self._find_slice(slice_id)
        normalized_owner = _normalize_owner_identity(owner_identity, state_path=self._state_path)
        if replace_owner_identity and normalized_owner is not None and normalized_owner["slice_id"] != slice_id:
            raise ValueError("owner_identity.slice_id 必須符合 slice_id")
        if replace_owner_identity:
            previous_owner = _normalize_owner_identity(
                slice_row.get("owner_identity"), state_path=self._state_path
            )
            if previous_owner != normalized_owner:
                if previous_owner is None:
                    raise ValueError("cannot migrate legacy owner identity")
                raise ValueError("cannot replace an established owner identity")
        if attempt_id is not None and (not isinstance(attempt_id, str) or not attempt_id.strip()):
            raise ValueError("attempt_id 必須為非空字串")
        previous_binding = _slice_binding_from_row(slice_row) if self._slice_is_versioned(slice_row) else None
        staged_binding_backfills: list[tuple[dict[str, Any], dict[str, Any]]] = []
        if previous_binding is not None:
            self._next_binding_revision(int(slice_row["binding_revision"]))
            staged_binding_backfills = self._stage_current_binding_disposition_backfills(
                slice_row=slice_row,
                current_binding=previous_binding,
            )
        if str(slice_row["state"]) not in REPINNABLE_SLICE_STATES:
            raise ValueError(
                f"非法 slice state repin: {slice_row['state']!r}"
                "（只允許 pending/needs_human/failed 重派）"
            )
        _validate_transition(
            field="gate_state",
            current=str(slice_row["gate_state"]),
            new="pending",
            allowed=GATE_STATE_TRANSITIONS,
        )
        for entry, witness in staged_binding_backfills:
            entry[_BOUND_BINDING_FIELD] = _deepcopy_json(witness)
        slice_row["spec"] = {"path": spec_path, "hash": spec_hash}
        slice_row["plan"] = {"path": plan_path, "hash": plan_hash}
        slice_row["target_branch"] = target_branch
        slice_row["target_remote"] = target_remote
        slice_row["verification"] = {
            "hash": verification_hash,
            "contract": dict(verification) if isinstance(verification, dict) else None,
        }
        slice_row["dispatch_base"] = dispatch_base
        if replace_owner_identity:
            slice_row["owner_identity"] = normalized_owner
        if attempt_id is not None:
            slice_row["attempt_id"] = attempt_id
        slice_row["builder_job_id"] = None
        slice_row["reviewer_job_id"] = None
        slice_row["candidate"] = None
        slice_row["gate_state"] = "pending"
        slice_row[_CURRENT_VERIFICATION_EVIDENCE_HASH] = None
        slice_row["current_evidence_refs"] = []
        slice_row["current_evaluation_refs"] = []
        self._maybe_bump_binding_revision(slice_row, previous_binding=previous_binding, force=True)
        slice_row["updated_at"] = _now_iso()
        self._persist()
        return self._copy_slice(slice_row)

    def list_slices(self) -> list[dict[str, Any]]:
        self._reload_if_changed()
        return [self._copy_slice(slice_row) for slice_row in self._slices]

    def list_slices_by_owner(self, *, repo: str, work_id: str) -> list[dict[str, Any]]:
        """依明示、持久的 repo／Work Item 身分列出 slice；不推測或回填舊 row。"""
        self._reload_if_changed()
        return [
            self._copy_slice(row)
            for row in self._slices
            if isinstance(row.get("owner_identity"), dict)
            and row["owner_identity"].get("repo") == repo
            and row["owner_identity"].get("work_id") == work_id
        ]

    def get_slice(self, slice_id: str) -> dict[str, Any]:
        self._reload_if_changed()
        return self._copy_slice(self._find_slice(slice_id))

    def lookup_registry_request_receipt(self, request_id: str) -> dict[str, Any] | None:
        request_key = _require_non_empty_string(
            request_id,
            label="lookup_registry_request_receipt.request_id",
            state_path=self._state_path,
        )
        self._reload_if_changed()
        located = self._locate_registry_request_receipt(request_key)
        if located is None:
            return None
        slice_row, kind, receipt, _index = located
        return {
            "slice_id": slice_row["slice_id"],
            "kind": kind,
            "receipt": _deepcopy_json(receipt),
        }

    def prepare_recovery(
        self,
        slice_id: str,
        *,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized_request = _validate_recovery_request(
            request,
            expected_slice_id=slice_id,
            state_path=self._state_path,
        )
        self._reload_if_changed()
        located = self._locate_registry_request_receipt(normalized_request["request_id"])
        if located is not None:
            slice_row, receipt, _index = self._ensure_matching_request_receipt(
                located,
                expected_kind="recovery",
                request=normalized_request,
            )
            if slice_row["slice_id"] != slice_id:
                raise _contract_error("request-content-conflict", state_path=self._state_path, detail="slice_id")
            if receipt["phase"] == "complete":
                return _deepcopy_json(receipt)
            self._revalidate_recovery_binding(slice_row, request=normalized_request)
            return _deepcopy_json(receipt)
        slice_row = self._find_slice(slice_id)
        self._revalidate_recovery_binding(slice_row, request=normalized_request)
        receipt = self._build_recovery_receipt(
            normalized_request,
            phase="prepared",
            prepared_at=_now_iso(),
        )
        slice_row["recovery_receipts"].append(receipt)
        self._persist_recovery_change()
        return _deepcopy_json(receipt)

    def commit_pre_candidate_recovery(
        self,
        slice_id: str,
        *,
        request: Mapping[str, Any],
        step_receipts: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        normalized_request = _validate_recovery_request(
            request,
            expected_slice_id=slice_id,
            state_path=self._state_path,
        )
        self._reload_if_changed()
        located = self._locate_registry_request_receipt(normalized_request["request_id"])
        if located is None:
            raise _contract_error("invalid-recovery-phase", state_path=self._state_path)
        slice_row, receipt, receipt_index = self._ensure_matching_request_receipt(
            located,
            expected_kind="recovery",
            request=normalized_request,
        )
        if slice_row["slice_id"] != slice_id or receipt_index is None:
            raise _contract_error("request-content-conflict", state_path=self._state_path, detail="slice_id")
        if receipt["phase"] == "complete":
            normalized_step_receipts = _validate_step_receipts(
                step_receipts,
                required_steps=list(normalized_request["payload"]["required_steps"]),
                label="commit_pre_candidate_recovery.step_receipts",
                state_path=self._state_path,
            )
            if normalized_step_receipts != receipt["step_receipts"]:
                raise _contract_error("request-content-conflict", state_path=self._state_path, detail="step_receipts")
            return _deepcopy_json(receipt)
        if receipt["phase"] != "prepared":
            raise _contract_error("invalid-recovery-phase", state_path=self._state_path)
        current_binding = self._revalidate_recovery_binding(slice_row, request=normalized_request)
        normalized_step_receipts = _validate_step_receipts(
            step_receipts,
            required_steps=list(normalized_request["payload"]["required_steps"]),
            label="commit_pre_candidate_recovery.step_receipts",
            state_path=self._state_path,
        )
        old_jobs = [
            self._find_job(job_id)
            for job_id in (
                current_binding["builder_job_id"],
                current_binding["reviewer_job_id"],
            )
            if job_id is not None
        ]
        completed_at = _now_iso()
        superseding_identity = {
            "request_id": normalized_request["request_id"],
            "request_digest": normalized_request["request_digest"],
        }
        old_revision = int(current_binding["binding_revision"])
        next_binding_revision = self._next_binding_revision(old_revision)
        staged_supersessions: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for job in old_jobs:
            supersession = self._build_job_supersession(
                job,
                slice_id=slice_id,
                binding_revision=old_revision,
                bound_binding=current_binding,
                actor=normalized_request["payload"]["actor"],
                reason="recover-pre-candidate",
                superseding_identity=superseding_identity,
                at=completed_at,
            )
            existing = job.get("supersession")
            if existing is not None and existing != supersession:
                raise _contract_error("request-content-conflict", state_path=self._state_path, detail="supersession")
            staged_supersessions.append((job, supersession))
        for job, supersession in staged_supersessions:
            job["supersession"] = supersession
        slice_row["state"] = "pending"
        slice_row["gate_state"] = "pending"
        slice_row["builder_job_id"] = None
        slice_row["reviewer_job_id"] = None
        slice_row["candidate"] = None
        slice_row["binding_revision"] = next_binding_revision
        applied_binding = _slice_binding_from_row(slice_row)
        slice_row["actions"].append(
            {
                "action": "recover-pre-candidate",
                "actor": normalized_request["payload"]["actor"],
                "state": slice_row["state"],
                "gate_state": slice_row["gate_state"],
                "requested_at": normalized_request["payload"]["created_at"],
                "at": completed_at,
                "result": "recovery-complete",
            }
        )
        slice_row["updated_at"] = completed_at
        completed_receipt = self._build_recovery_receipt(
            normalized_request,
            phase="complete",
            prepared_at=receipt["prepared_at"],
            completed_at=completed_at,
            applied_binding=applied_binding,
            step_receipts=normalized_step_receipts,
            result="recovery-complete",
        )
        slice_row["recovery_receipts"][receipt_index] = completed_receipt
        self._persist_recovery_change()
        return _deepcopy_json(completed_receipt)

    def record_job_supersession(
        self,
        job_id: str,
        *,
        slice_id: str,
        binding_revision: int,
        actor: str,
        reason: str,
        superseding_identity: Mapping[str, Any],
        at: str | None = None,
    ) -> dict[str, Any]:
        job = self._find_job(job_id)
        slice_row = self._find_slice(slice_id)
        normalized_binding_revision = _require_safe_integer(
            binding_revision,
            label="record_job_supersession.binding_revision",
            state_path=self._state_path,
        )
        bound_binding = self._resolve_job_disposition_binding(
            job,
            slice_row=slice_row,
            binding_revision=normalized_binding_revision,
        )
        at_was_omitted = at is None
        changed = self._record_job_supersession_inplace(
            job,
            slice_id=slice_id,
            binding_revision=normalized_binding_revision,
            bound_binding=bound_binding,
            actor=_require_non_empty_string(actor, label="record_job_supersession.actor", state_path=self._state_path),
            reason=_require_non_empty_string(reason, label="record_job_supersession.reason", state_path=self._state_path),
            superseding_identity=superseding_identity,
            at=_now_iso() if at_was_omitted else _require_non_empty_string(
                at,
                label="record_job_supersession.at",
                state_path=self._state_path,
            ),
            at_was_omitted=at_was_omitted,
        )
        if changed:
            self._persist_recovery_change()
        return _deepcopy_json(job)

    def record_job_consumption(
        self,
        job_id: str,
        *,
        slice_id: str,
        binding_revision: int,
        actor: str,
        completion_identity: Mapping[str, Any],
        proof_refs: list[Mapping[str, Any]],
        at: str | None = None,
    ) -> dict[str, Any]:
        job = self._find_job(job_id)
        slice_row = self._find_slice(slice_id)
        normalized_binding_revision = _require_safe_integer(
            binding_revision,
            label="record_job_consumption.binding_revision",
            state_path=self._state_path,
        )
        bound_binding = self._resolve_job_disposition_binding(
            job,
            slice_row=slice_row,
            binding_revision=normalized_binding_revision,
        )
        at_was_omitted = at is None
        changed = self._record_job_consumption_inplace(
            job,
            slice_id=slice_id,
            binding_revision=normalized_binding_revision,
            bound_binding=bound_binding,
            actor=_require_non_empty_string(actor, label="record_job_consumption.actor", state_path=self._state_path),
            completion_identity=completion_identity,
            proof_refs=proof_refs,
            at=_now_iso() if at_was_omitted else _require_non_empty_string(
                at,
                label="record_job_consumption.at",
                state_path=self._state_path,
            ),
            at_was_omitted=at_was_omitted,
        )
        if changed:
            self._persist_recovery_change()
        return _deepcopy_json(job)

    def checkpoint_legacy_binding(
        self,
        slice_id: str,
        *,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized_request = _validate_checkpoint_request(
            request,
            expected_slice_id=slice_id,
            state_path=self._state_path,
        )
        self._reload_if_changed()
        located = self._locate_registry_request_receipt(normalized_request["request_id"])
        if located is not None:
            slice_row, receipt, _index = self._ensure_matching_request_receipt(
                located,
                expected_kind="checkpoint",
                request=normalized_request,
            )
            if slice_row["slice_id"] != slice_id:
                raise _contract_error("request-content-conflict", state_path=self._state_path, detail="slice_id")
            return _deepcopy_json(receipt)
        slice_row = self._find_slice(slice_id)
        legacy_binding = self._revalidate_legacy_checkpoint_snapshot(
            slice_row,
            request=normalized_request,
        )
        slice_row["binding_version"] = _SLICE_BINDING_VERSION
        slice_row["binding_revision"] = 1
        slice_row["recovery_receipts"] = []
        applied_binding = {
            "binding_version": _SLICE_BINDING_VERSION,
            "binding_revision": 1,
            **legacy_binding,
        }
        checkpoint_receipt = self._build_checkpoint_receipt(
            normalized_request,
            checkpointed_at=_now_iso(),
            applied_binding=applied_binding,
        )
        slice_row["binding_checkpoint_receipt"] = checkpoint_receipt
        self._persist_recovery_change()
        return _deepcopy_json(checkpoint_receipt)

    def update_slice(
        self,
        slice_id: str,
        *,
        state: str | None = None,
        gate_state: str | None = None,
        current_evidence_refs: list[str] | None = None,
        current_evaluation_refs: list[str] | None = None,
        builder_job_id: str | None = None,
        reviewer_job_id: str | None = None,
        candidate: str | None = None,
        dispatch_base: str | None = None,
        target_remote: str | None = None,
        current_verification_evidence_hash: str | None = None,
    ) -> dict[str, Any]:
        slice_row = self._find_slice(slice_id)
        previous_binding = _slice_binding_from_row(slice_row) if self._slice_is_versioned(slice_row) else None
        staged_binding_backfills: list[tuple[dict[str, Any], dict[str, Any]]] = []

        # Phase 1 — validate every provided field against the live row
        # *without* mutating anything. #382: the previous validate-then-write
        # ordering was interleaved per field, so a later field (e.g.
        # gate_state) failing validation could raise *after* an earlier field
        # (e.g. state) had already been written into the live `slice_row`
        # object. That partial write was never persisted (the raise happens
        # before `_persist()`), but it also never got corrected — the next
        # *unrelated* `_persist()` call (from any other job/slice mutation)
        # would flush the tainted in-memory row to disk. Validating
        # everything up front means a rejected call cannot leave any trace,
        # in memory or on disk.
        new_current_evidence_refs = None
        if current_evidence_refs is not None:
            if not _is_ref_list(current_evidence_refs):
                raise ValueError("current_evidence_refs 必須為字串陣列")
            new_current_evidence_refs = _copy_ref_list(current_evidence_refs)
        new_current_evaluation_refs = None
        if current_evaluation_refs is not None:
            if not _is_ref_list(current_evaluation_refs):
                raise ValueError("current_evaluation_refs 必須為字串陣列")
            new_current_evaluation_refs = _copy_ref_list(current_evaluation_refs)
        if current_verification_evidence_hash is not None and (
            not isinstance(current_verification_evidence_hash, str)
            or not current_verification_evidence_hash
        ):
            raise ValueError("current_verification_evidence_hash 必須為非空字串")
        if state is not None:
            if state not in VALID_SLICE_STATES:
                raise ValueError(f"非法 slice state: {state!r}")
            _validate_transition(
                field="slice state",
                current=str(slice_row["state"]),
                new=state,
                allowed=SLICE_STATE_TRANSITIONS,
            )
        if gate_state is not None:
            if gate_state not in VALID_GATE_STATES:
                raise ValueError(f"非法 gate_state: {gate_state!r}")
            _validate_transition(
                field="gate_state",
                current=str(slice_row["gate_state"]),
                new=gate_state,
                allowed=GATE_STATE_TRANSITIONS,
            )
        if builder_job_id is not None:
            self._validate_existing_job_ref("builder_job_id", builder_job_id)
        if reviewer_job_id is not None:
            self._validate_existing_job_ref("reviewer_job_id", reviewer_job_id)
        if previous_binding is not None:
            will_bump_binding = (
                (state is not None and state != slice_row["state"])
                or (gate_state is not None and gate_state != slice_row["gate_state"])
                or (builder_job_id is not None and builder_job_id != slice_row["builder_job_id"])
                or (reviewer_job_id is not None and reviewer_job_id != slice_row["reviewer_job_id"])
                or (candidate is not None and candidate != slice_row["candidate"])
                or (dispatch_base is not None and dispatch_base != slice_row["dispatch_base"])
                or (target_remote is not None and target_remote != slice_row["target_remote"])
            )
            if will_bump_binding:
                self._next_binding_revision(int(slice_row["binding_revision"]))
                staged_binding_backfills = self._stage_current_binding_disposition_backfills(
                    slice_row=slice_row,
                    current_binding=previous_binding,
                )

        # Phase 2 — everything validated; apply every field together.
        for entry, witness in staged_binding_backfills:
            entry[_BOUND_BINDING_FIELD] = _deepcopy_json(witness)
        if state is not None:
            slice_row["state"] = state
        if gate_state is not None:
            slice_row["gate_state"] = gate_state
        if new_current_evidence_refs is not None:
            slice_row["current_evidence_refs"] = new_current_evidence_refs
        if new_current_evaluation_refs is not None:
            slice_row["current_evaluation_refs"] = new_current_evaluation_refs
        if builder_job_id is not None:
            slice_row["builder_job_id"] = builder_job_id
        if reviewer_job_id is not None:
            slice_row["reviewer_job_id"] = reviewer_job_id
        if candidate is not None:
            slice_row["candidate"] = candidate
        if dispatch_base is not None:
            slice_row["dispatch_base"] = dispatch_base
        if target_remote is not None:
            slice_row["target_remote"] = target_remote
        if current_verification_evidence_hash is not None:
            slice_row[_CURRENT_VERIFICATION_EVIDENCE_HASH] = current_verification_evidence_hash
        self._maybe_bump_binding_revision(slice_row, previous_binding=previous_binding)
        slice_row["updated_at"] = _now_iso()
        self._persist()
        return self._copy_slice(slice_row)

    def record_action(
        self,
        slice_id: str,
        *,
        action: str,
        actor: str,
        state: str | None = None,
        gate_state: str | None = None,
        evidence_refs: list[str] | None = None,
        evaluation_refs: list[str] | None = None,
        candidate: str | None = None,
        clear_builder_binding: bool = False,
        clear_candidate: bool = False,
        requested_at: str | None = None,
        consumed_at: str | None = None,
        result: str | None = None,
        reason: str | None = None,
        expected_binding_revision: int | None = None,
        diagnostic_reason: DiagnosticReason | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        slice_row = self._find_slice(slice_id)
        if not isinstance(clear_builder_binding, bool) or not isinstance(clear_candidate, bool):
            raise ValueError("clear binding flags 必須為布林值")
        normalized_diagnostic_reason = coerce_diagnostic_reason(diagnostic_reason)
        if diagnostic_reason is not None and normalized_diagnostic_reason is None:
            raise ValueError("diagnostic_reason 必須符合 DiagnosticReason 契約")
        normalized_expected_binding_revision = None
        if expected_binding_revision is not None:
            normalized_expected_binding_revision = _require_safe_integer(
                expected_binding_revision,
                label="record_action.expected_binding_revision",
                state_path=self._state_path,
            )
            if slice_row.get("binding_revision") != normalized_expected_binding_revision:
                raise ValueError(
                    "record_action expected_binding_revision mismatch: "
                    f"expected={normalized_expected_binding_revision}, "
                    f"actual={slice_row.get('binding_revision')}"
                )
        if reason is not None and (
            not isinstance(reason, str)
            or reason != reason.strip()
            or not 1 <= len(reason) <= 500
            or not reason.isprintable()
        ):
            raise ValueError("record_action reason 必須為 1–500 字可列印單行文字")
        previous_binding = _slice_binding_from_row(slice_row) if self._slice_is_versioned(slice_row) else None
        staged_binding_backfills: list[tuple[dict[str, Any], dict[str, Any]]] = []

        # Phase 1 — validate every provided field before mutating anything
        # (#382, same rationale as update_slice above): a rejected multi-field
        # transition must never leave a half-applied write sitting in the
        # live `slice_row`, or an unrelated later `_persist()` call flushes
        # the tainted row to disk.
        new_evidence_refs = None
        if evidence_refs is not None:
            if not _is_ref_list(evidence_refs):
                raise ValueError("evidence_refs 必須為字串陣列")
            new_evidence_refs = _copy_ref_list(evidence_refs)
        new_evaluation_refs = None
        if evaluation_refs is not None:
            if not _is_ref_list(evaluation_refs):
                raise ValueError("evaluation_refs 必須為字串陣列")
            new_evaluation_refs = _copy_ref_list(evaluation_refs)
        if state is not None:
            if state not in VALID_SLICE_STATES:
                raise ValueError(f"非法 slice state: {state!r}")
            _validate_transition(
                field="slice state",
                current=str(slice_row["state"]),
                new=state,
                allowed=SLICE_STATE_TRANSITIONS,
            )
        if gate_state is not None:
            if gate_state not in VALID_GATE_STATES:
                raise ValueError(f"非法 gate_state: {gate_state!r}")
            _validate_transition(
                field="gate_state",
                current=str(slice_row["gate_state"]),
                new=gate_state,
                allowed=GATE_STATE_TRANSITIONS,
            )
        if previous_binding is not None:
            will_bump_binding = (
                (state is not None and state != slice_row["state"])
                or (gate_state is not None and gate_state != slice_row["gate_state"])
                or (candidate is not None and candidate != slice_row["candidate"])
                or (clear_builder_binding and slice_row["builder_job_id"] is not None)
                or (clear_candidate and slice_row["candidate"] is not None)
            )
            if will_bump_binding:
                self._next_binding_revision(int(slice_row["binding_revision"]))
                staged_binding_backfills = self._stage_current_binding_disposition_backfills(
                    slice_row=slice_row,
                    current_binding=previous_binding,
                )

        # Phase 2 — everything validated; apply every field together, then
        # persist exactly once.
        for entry, witness in staged_binding_backfills:
            entry[_BOUND_BINDING_FIELD] = _deepcopy_json(witness)
        if state is not None:
            slice_row["state"] = state
        if gate_state is not None:
            slice_row["gate_state"] = gate_state
        if new_evidence_refs is not None:
            slice_row["current_evidence_refs"] = new_evidence_refs
            slice_row["evidence_history"].append(
                {"action": action, "actor": actor, "refs": new_evidence_refs, "at": _now_iso()}
            )
        if new_evaluation_refs is not None:
            slice_row["current_evaluation_refs"] = new_evaluation_refs
            slice_row["evaluation_history"].append(
                {"action": action, "actor": actor, "refs": new_evaluation_refs, "at": _now_iso()}
            )
        if candidate is not None:
            slice_row["candidate"] = candidate
        if clear_builder_binding:
            slice_row["builder_job_id"] = None
        if clear_candidate:
            slice_row["candidate"] = None
        self._maybe_bump_binding_revision(slice_row, previous_binding=previous_binding)
        action_entry: dict[str, Any] = {
            "action": action,
            "actor": actor,
            "state": slice_row["state"],
            "gate_state": slice_row["gate_state"],
            "at": _now_iso(),
        }
        if requested_at is not None:
            action_entry["requested_at"] = requested_at
        if consumed_at is not None:
            action_entry["consumed_at"] = consumed_at
        if result is not None:
            action_entry["result"] = result
        if reason is not None:
            action_entry["reason"] = reason
        if normalized_expected_binding_revision is not None:
            action_entry["expected_binding_revision"] = normalized_expected_binding_revision
        if normalized_diagnostic_reason is not None:
            action_entry["diagnostic_reason"] = normalized_diagnostic_reason.to_dict()
        slice_row["actions"].append(action_entry)
        slice_row["updated_at"] = _now_iso()
        self._persist()
        return self._copy_slice(slice_row)

    def _find_workflow_run_index_in_memory(self, run_id: str) -> int:
        for index, run in enumerate(self._workflows):
            if run.run_id == run_id:
                return index
        raise KeyError(f"workflow run 不存在: {run_id}")

    def _find_workflow_run_index(self, run_id: str) -> int:
        return self._lookup_with_cross_instance_visibility(
            lambda: self._find_workflow_run_index_in_memory(run_id)
        )

    def _copy_workflow_run(self, run: WorkflowRun) -> WorkflowRun:
        return WorkflowRun.from_dict(run.to_dict())

    def list_legacy_records(self) -> dict[str, Any]:
        self._reload_if_changed()
        return _deepcopy_json(self._legacy_records)

    def list_workflow_runs(self) -> list[WorkflowRun]:
        self._reload_if_changed()
        return [self._copy_workflow_run(run) for run in self._workflows]

    def get_workflow_run(self, run_id: str) -> WorkflowRun:
        self._reload_if_changed()
        return self._copy_workflow_run(self._workflows[self._find_workflow_run_index(run_id)])

    def list_reclaim_resets(self) -> list[dict[str, Any]]:
        """#519：唯讀列出所有 semantic-reclaim 熔斷重置授權（append-only 稽核列）。"""

        self._reload_if_changed()
        return _deepcopy_json(self._reclaim_resets)

    def reclaim_reset_cleared_run_ids(self, *, repo: str, work_id: str) -> frozenset[str]:
        """#519：某 (repo, work_id) 已被明示重置赦免的 superseded run_id 聯集。

        熔斷計數改以「superseded 世代扣掉這個集合」為準，因此重置只赦免當下已
        存在的世代——重置之後新產生的 superseded 世代照常累加，熔斷會再次上膛。
        """

        self._reload_if_changed()
        cleared: set[str] = set()
        for entry in self._reclaim_resets:
            if entry.get("repo") == repo and entry.get("work_id") == work_id:
                cleared.update(str(item) for item in entry.get("cleared_run_ids", ()))
        return frozenset(cleared)

    def _manager_record_reclaim_reset(
        self,
        *,
        repo: str,
        work_id: str,
        actor: str,
        reason: str,
        evidence_ref: str,
        evidence_hash: str,
        cleared_run_ids: list[str],
        created_at: str,
    ) -> dict[str, Any]:
        """#519：登錄一筆 semantic-reclaim 熔斷重置授權（唯一 writer 走 manager）。

        純 append：既有 WorkflowRun row 一個位元組都不動。被赦免的 run_id 必須
        確實存在且為 superseded，否則 fail closed——重置只能赦免「已經發生的
        世代」，不得預先授權未來的失敗。同一 evidence_ref 重入視為冪等（比照
        abandon 的 crash-window 契約，#275），不產生第二筆授權。
        """

        if not cleared_run_ids:
            raise ValueError("workflow reclaim reset requires cleared run ids")
        for existing in self._reclaim_resets:
            if existing.get("evidence_ref") == evidence_ref:
                return _deepcopy_json(existing)
        superseded = {
            run.run_id
            for run in self._workflows
            if run.repo == repo and run.work_id == work_id and run.status == "superseded"
        }
        unknown = sorted(set(cleared_run_ids) - superseded)
        if unknown:
            raise ValueError(
                f"workflow reclaim reset 指向非 superseded run（fail-closed）: {unknown[0]}"
            )
        entry = {
            "repo": repo,
            "work_id": work_id,
            "actor": actor,
            "reason": reason,
            "evidence_ref": evidence_ref,
            "evidence_hash": evidence_hash,
            "cleared_run_ids": sorted(set(cleared_run_ids)),
            "created_at": created_at,
        }
        self._reclaim_resets.append(entry)
        self._persist()
        return _deepcopy_json(entry)

    # --- 診斷 invariant 的強制點（#527／#514／#515／#511／#482）-----------------
    #
    # 這是全庫**唯一**能把 `needs_human` facet 寫進 run row 的兩個入口
    # （`_manager_create_workflow_run`／`_manager_update_workflow_run`）。invariant
    # 因此在這裡強制，而不是逐個呼叫端人工檢查——五次逐案補洞已證明後者無效。
    #
    # 規則只有三條：
    #   1. 這次更新把 `needs_human` **加進**facets（先前沒有）→ 必須帶
    #      `needs_human_reason`，否則 `DiagnosticInvariantError`。
    #   2. 這次更新把 `needs_human` **移出**facets → 理由一併清掉（陳舊理由比沒
    #      理由更糟；見 `WorkflowRun.__post_init__`）。
    #   3. facet 已經在、這次沒帶新理由 → 沿用既有理由（大量呼叫端會在同一個
    #      run 上重複寫 `facets=("needs_human",)`，不該因此把第一次的理由洗掉）。
    #
    # **範圍**：本層只管理由有沒有落地，不改任何呼叫端的後續處置。
    @staticmethod
    def _resolve_needs_human_reason(
        *,
        run_id: str,
        current_facets: tuple[str, ...],
        next_facets: tuple[str, ...],
        current_reason: dict[str, Any] | None,
        supplied: DiagnosticReason | Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        reason = coerce_diagnostic_reason(supplied)
        blocked = "needs_human" in next_facets
        if reason is not None and not blocked:
            raise DiagnosticInvariantError(
                "workflow run 帶了 needs_human_reason 卻沒有設 needs_human facet"
                f"（fail-closed）: {run_id}"
            )
        if not blocked:
            return None
        if reason is not None:
            return reason.to_dict()
        if "needs_human" not in current_facets:
            raise DiagnosticInvariantError(
                "把 workflow run 轉入 needs_human 必須同時提供結構化理由"
                "（diagnostics.DiagnosticReason：機器可讀 reason ＋ 人可讀 detail "
                f"＋ 來源位置）: {run_id}"
            )
        return dict(current_reason) if current_reason is not None else None

    def _manager_create_workflow_run(
        self,
        *,
        work_id: str,
        repo: str,
        claim_key: str,
        source_revision: str,
        workspace_root: str,
        combo: str,
        current_phase: str,
        steps: tuple[WorkflowStep, ...],
        issue_refs: tuple[str, ...] = (),
        openspec_refs: tuple[str, ...] = (),
        pr_refs: tuple[str, ...] = (),
        attempts: dict[str, int] | None = None,
        evidence_refs: tuple[str, ...] = (),
        gate_refs: tuple[GateEvidenceRef, ...] = (),
        brainstorm_required: bool = False,
        primary_domain: str | None = None,
        candidate_head: str | None = None,
        verified_head: str | None = None,
        facets: tuple[str, ...] = (),
        gate_status: str = "pending",
        planning_authority: tuple[PlanningArtifactAuthority, ...] = (),
        sizing_score: int | None = None,
        sizing_band: str | None = None,
        decomposition_depth: int = 0,
        plan_review_passed: bool = False,
        frozen_readiness: dict[str, Any] | None = None,
        model_chain_override: dict[str, dict[str, str]] | None = None,
        combo_selection: dict[str, Any] | None = None,
        needs_human_reason: DiagnosticReason | Mapping[str, Any] | None = None,
    ) -> WorkflowRun:
        matches = [
            existing
            for existing in self._workflows
            if existing.claim_key == claim_key
            and existing.work_id == work_id
            and existing.repo == repo
        ]
        if any(existing.status == "ongoing" for existing in matches):
            return self._copy_workflow_run(
                next(existing for existing in matches if existing.status == "ongoing")
            )
        if any(existing.work_id != work_id or existing.repo != repo for existing in matches):
            raise ValueError(f"claim_key 已屬於其他 work item: {claim_key}")
        if len(matches) != len(set(run.run_id for run in matches)):
            raise ValueError(f"workflow run id duplicated for claim_key: {claim_key}")
        attempt = len(matches) + 1
        while True:
            seed = f"{claim_key}:{attempt}"
            run_id = f"workflow-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"
            if any(run.run_id == run_id for run in self._workflows):
                attempt += 1
                if attempt > 10_000:
                    raise ValueError(f"workflow run id collision: {run_id}")
                continue
            break
        if any(run.run_id == run_id for run in self._workflows):
            raise ValueError(f"workflow run id collision: {run_id}")
        now = _now_iso()
        resolved_needs_human_reason = self._resolve_needs_human_reason(
            run_id=run_id,
            current_facets=(),
            next_facets=tuple(facets),
            current_reason=None,
            supplied=needs_human_reason,
        )
        run = WorkflowRun(
            run_id=run_id,
            work_id=work_id,
            repo=repo,
            claim_key=claim_key,
            source_revision=source_revision,
            workspace_root=workspace_root,
            combo=combo,
            current_phase=current_phase,
            steps=tuple(steps),
            issue_refs=tuple(issue_refs),
            openspec_refs=tuple(openspec_refs),
            pr_refs=tuple(pr_refs),
            attempts=dict(attempts or {}),
            evidence_refs=tuple(evidence_refs),
            gate_refs=tuple(gate_refs),
            brainstorm_required=brainstorm_required,
            primary_domain=primary_domain,
            candidate_head=candidate_head,
            verified_head=verified_head,
            facets=tuple(facets),
            gate_status=gate_status,
            created_at=now,
            updated_at=now,
            planning_authority=tuple(planning_authority),
            planning_source_revision=source_revision,
            sizing_score=sizing_score,
            sizing_band=sizing_band,
            decomposition_depth=decomposition_depth,
            plan_review_passed=plan_review_passed,
            frozen_readiness=frozen_readiness,
            model_chain_override=model_chain_override,
            combo_selection=combo_selection,
            needs_human_reason=resolved_needs_human_reason,
        )
        superseded_at = _now_iso()
        next_workflows = [
            replace(
                existing,
                status="superseded",
                facets=tuple(sorted(set(existing.facets) | {"blocked"})),
                updated_at=superseded_at,
            )
            if existing.repo == repo
            and existing.work_id == work_id
            and existing.status == "ongoing"
            else existing
            for existing in self._workflows
        ]
        self._workflows = [*next_workflows, run]
        self._persist()
        return self._copy_workflow_run(run)

    def _manager_update_workflow_run(
        self,
        run_id: str,
        *,
        current_phase: str | None = None,
        source_revision: str | None = None,
        steps: tuple[WorkflowStep, ...] | None = None,
        issue_refs: tuple[str, ...] | None = None,
        openspec_refs: tuple[str, ...] | None = None,
        pr_refs: tuple[str, ...] | None = None,
        attempts: dict[str, int] | None = None,
        evidence_refs: tuple[str, ...] | None = None,
        gate_refs: tuple[GateEvidenceRef, ...] | None = None,
        brainstorm_required: bool | None = None,
        primary_domain: str | None = None,
        candidate_head: str | None = None,
        verified_head: str | None = None,
        facets: tuple[str, ...] | None = None,
        gate_status: str | None = None,
        planning_authority: tuple[PlanningArtifactAuthority, ...] | None = None,
        planning_source_revision: str | None = None,
        status: str | None = None,
        completion_record_path: str | None = None,
        completion_record_hash: str | None = None,
        completion_record_revision: str | None = None,
        completion_source_revisions: dict[str, str] | None = None,
        pr_candidate: str | None = None,
        merge_revision: str | None = None,
        retry_classification: str | None = None,
        sizing_score: int | None = None,
        sizing_band: str | None = None,
        decomposition_depth: int | None = None,
        plan_review_passed: bool | None = None,
        frozen_readiness: dict[str, Any] | None = None,
        model_chain_override: dict[str, dict[str, str]] | None = None,
        resolved_model_chain: dict[str, dict[str, str]] | None = None,
        combo_selection: dict[str, Any] | None = None,
        needs_human_reason: DiagnosticReason | Mapping[str, Any] | None = None,
    ) -> WorkflowRun:
        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        next_phase = current.current_phase if current_phase is None else current_phase
        validate_workflow_phase_transition(current.current_phase, next_phase)
        next_facets = current.facets if facets is None else tuple(facets)
        resolved_needs_human_reason = self._resolve_needs_human_reason(
            run_id=run_id,
            current_facets=current.facets,
            next_facets=next_facets,
            current_reason=current.needs_human_reason,
            supplied=needs_human_reason,
        )
        updated = WorkflowRun(
            run_id=current.run_id,
            work_id=current.work_id,
            repo=current.repo,
            claim_key=current.claim_key,
            source_revision=(
                current.source_revision if source_revision is None else source_revision
            ),
            workspace_root=current.workspace_root,
            combo=current.combo,
            current_phase=next_phase,
            steps=current.steps if steps is None else tuple(steps),
            issue_refs=current.issue_refs if issue_refs is None else tuple(issue_refs),
            openspec_refs=current.openspec_refs if openspec_refs is None else tuple(openspec_refs),
            pr_refs=current.pr_refs if pr_refs is None else tuple(pr_refs),
            attempts=dict(current.attempts if attempts is None else attempts),
            evidence_refs=current.evidence_refs if evidence_refs is None else tuple(evidence_refs),
            gate_refs=current.gate_refs if gate_refs is None else tuple(gate_refs),
            brainstorm_required=(
                current.brainstorm_required if brainstorm_required is None else brainstorm_required
            ),
            primary_domain=current.primary_domain if primary_domain is None else primary_domain,
            candidate_head=current.candidate_head if candidate_head is None else candidate_head,
            verified_head=current.verified_head if verified_head is None else verified_head,
            facets=next_facets,
            gate_status=current.gate_status if gate_status is None else gate_status,
            created_at=current.created_at,
            updated_at=_now_iso(),
            planning_authority=(
                current.planning_authority
                if planning_authority is None
                else tuple(planning_authority)
            ),
            planning_source_revision=(
                current.planning_source_revision
                if planning_source_revision is None
                else planning_source_revision
            ),
            status=current.status if status is None else status,
            completion_record_path=(
                current.completion_record_path
                if completion_record_path is None
                else completion_record_path
            ),
            completion_record_hash=(
                current.completion_record_hash
                if completion_record_hash is None
                else completion_record_hash
            ),
            completion_record_revision=(
                current.completion_record_revision
                if completion_record_revision is None
                else completion_record_revision
            ),
            completion_source_revisions=(
                dict(current.completion_source_revisions)
                if completion_source_revisions is None
                else dict(completion_source_revisions)
            ),
            pr_candidate=current.pr_candidate if pr_candidate is None else pr_candidate,
            merge_revision=current.merge_revision if merge_revision is None else merge_revision,
            retry_classification=(
                current.retry_classification
                if retry_classification is None
                else retry_classification
            ),
            sizing_score=current.sizing_score if sizing_score is None else sizing_score,
            sizing_band=current.sizing_band if sizing_band is None else sizing_band,
            decomposition_depth=(
                current.decomposition_depth
                if decomposition_depth is None
                else decomposition_depth
            ),
            plan_review_passed=(
                current.plan_review_passed
                if plan_review_passed is None
                else plan_review_passed
            ),
            model_chain_override=(
                current.model_chain_override
                if model_chain_override is None
                else model_chain_override
            ),
            resolved_model_chain=(
                current.resolved_model_chain
                if resolved_model_chain is None
                else resolved_model_chain
            ),
            combo_selection=(
                current.combo_selection if combo_selection is None else combo_selection
            ),
            frozen_readiness=(
                current.frozen_readiness if frozen_readiness is None else frozen_readiness
            ),
            needs_human_reason=resolved_needs_human_reason,
        )
        self._workflows[index] = updated
        self._persist()
        return self._copy_workflow_run(updated)

    def _manager_reset_workflow_after_archive(
        self,
        run_id: str,
        *,
        candidate_head: str,
    ) -> WorkflowRun:
        """Atomically invalidate old Candidate gates after Manager archive commit."""

        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        if current.current_phase != "review" or current.status != "ongoing":
            raise ValueError("archive candidate reset requires active review workflow")
        steps = []
        for step in current.steps:
            if step.phase in {"verify", "review"}:
                steps.append(replace(step, gate_result="pending"))
            elif step.phase == "ship" and step.card == "openspec-archive":
                steps.append(
                    replace(
                        step,
                        executor="cortex-manager",
                        model="deterministic",
                        domain="cortex",
                        gate_result="passed",
                    )
                )
            else:
                steps.append(step)
        updated = replace(
            current,
            current_phase="verify",
            steps=tuple(steps),
            attempts={**current.attempts, "verify": current.attempts.get("verify", 0) + 1},
            gate_refs=tuple(ref for ref in current.gate_refs if ref.kind == "brainstorm"),
            candidate_head=candidate_head,
            verified_head=None,
            facets=(),
            # 診斷 invariant：清掉 needs_human facet 就必須清掉理由——
            # 陳舊理由會讓 operator 讀到上一輪的原因（見 WorkflowRun.__post_init__）。
            needs_human_reason=None,
            gate_status="running",
            updated_at=_now_iso(),
        )
        self._workflows[index] = updated
        self._persist()
        return self._copy_workflow_run(updated)

    def _manager_reset_workflow_for_retry_build(
        self,
        run_id: str,
        *,
        expected_candidate: str,
        repair_action: str,
        retry_classification: str | None = None,
        model_chain_override: dict[str, dict[str, str]] | None = None,
    ) -> WorkflowRun:
        """Atomically reopen only the final builder card after an explicit human stop.

        ``model_chain_override`` is an optional, explicit recovery-time refinement.
        It is merged onto the run-scoped claim override (rather than replacing the
        planner/reviewer entries), and the selected identity is still validated by
        the normal dispatch path before a job is created.
        """

        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        if (
            current.status != "ongoing"
            or current.current_phase not in {"build", "verify", "review"}
            or "needs_human" not in current.facets
        ):
            raise ValueError(
                "retry-build reset requires active needs_human build/verify/review workflow"
            )
        if current.candidate_head != expected_candidate:
            raise ValueError("retry-build reset Candidate CAS mismatch")
        if not isinstance(repair_action, str) or not repair_action:
            raise ValueError("retry-build reset action missing")
        if any(
            job.get("workflow_run_id") == current.run_id
            and job.get("status") in ACTIVE_JOB_STATUSES
            for job in self._jobs
        ):
            raise ValueError("retry-build reset refuses active workflow job")
        build_steps = [step for step in current.steps if step.phase == "build"]
        if not build_steps:
            raise ValueError("retry-build reset requires build phase")
        repair_card = build_steps[-1].card
        if current.current_phase == "build":
            if (
                any(step.gate_result != "passed" for step in build_steps[:-1])
                or build_steps[-1].gate_result != "pending"
            ):
                raise ValueError(
                    "retry-build reset requires only the final builder card pending"
                )
            terminal_repairs = [
                job
                for job in self._jobs
                if job.get("workflow_run_id") == current.run_id
                and job.get("workflow_phase") == "build"
                and job.get("workflow_card") == repair_card
                and job.get("status") == "exited"
                and job.get("exit_code") == 0
            ]
            if (
                not terminal_repairs
                or terminal_repairs[-1].get("workflow_evidence") is not None
            ):
                raise ValueError(
                    "retry-build reset requires unbound terminal builder evidence"
                )
        elif any(step.gate_result != "passed" for step in build_steps):
            raise ValueError("retry-build reset requires completed build phase")
        passed_ship_steps = [
            step
            for step in current.steps
            if step.phase == "ship" and step.gate_result == "passed"
        ]
        if len(passed_ship_steps) > 1 or any(
            step.card != "openspec-archive"
            or step.executor != "cortex-manager"
            or step.model != "deterministic"
            or step.domain != "cortex"
            for step in passed_ship_steps
        ):
            raise ValueError(
                "retry-build reset only permits Manager-owned archive authority"
            )
        steps = tuple(
            replace(
                step,
                executor=None,
                model=None,
                domain=None,
                gate_result="pending",
                action=(
                    repair_action
                    if step.phase == "build" and step.card == repair_card
                    else step.action
                ),
            )
            if (step.phase == "build" and step.card == repair_card)
            or step.phase in {"verify", "review"}
            or (step.phase == "ship" and step.gate_result != "passed")
            else step
            for step in current.steps
        )
        effective_model_chain_override = current.model_chain_override
        if model_chain_override is not None:
            effective_model_chain_override = {
                **dict(current.model_chain_override or {}),
                **{persona: dict(row) for persona, row in model_chain_override.items()},
            }
        updated = replace(
            current,
            current_phase="build",
            steps=steps,
            attempts={
                **current.attempts,
                "build": current.attempts.get("build", 0) + 1,
            },
            gate_refs=tuple(ref for ref in current.gate_refs if ref.kind == "brainstorm"),
            verified_head=None,
            facets=tuple(
                facet for facet in current.facets if facet != "needs_human"
            ),
            # 診斷 invariant：清掉 needs_human facet 就必須清掉理由——
            # 陳舊理由會讓 operator 讀到上一輪的原因（見 WorkflowRun.__post_init__）。
            needs_human_reason=None,
            gate_status="running",
            retry_classification=(
                current.retry_classification
                if retry_classification is None
                else retry_classification
            ),
            model_chain_override=effective_model_chain_override,
            updated_at=_now_iso(),
        )
        self._workflows[index] = updated
        self._persist()
        return self._copy_workflow_run(updated)

    def _manager_reset_workflow_for_retry_card(
        self,
        run_id: str,
        *,
        expected_run_id: str,
        card: str,
        retry_classification: str | None = None,
        model_chain_override: dict[str, dict[str, str]] | None = None,
    ) -> WorkflowRun:
        """#545／#569：原子重開「當前 phase 內最早一張尚未採信的卡」。

        受理範圍：build phase 的 builder 卡（#545 的中段卡）與 verify／review
        phase 的 reviewer 卡（#569 的 verification／code-review／
        adversarial-review）。兩者是同一個形狀的死結——卡片最新的終止 job 輸出
        損壞、evidence 綁不上，harvest 每次重讀同一顆壞 job，而契約內沒有任何
        路徑能產生新的 envelope。

        與 `_manager_reset_workflow_for_retry_build` 的差別是刻意的，不是重複：

        - retry-build 是 **candidate 修復**語意，只受理最後一張 builder 卡，並
          把該卡的 `action` 覆寫成 repair 指示；中段卡（tdd-red）若走那條路，
          卡片本身的指示（「寫一個 RED regression test」）會被 repair 文案抹掉。
          本方法**完全不動 `step.action`**——重派的就是原卡，prompt 由既有的
          `_workflow_job_prompt` 依原卡重組（含 #541 的 canonical gate 名注入）。
        - 已採信（`workflow_evidence` 已綁定）的卡一律拒絕：舊 job 與舊 envelope
          是稽核紀錄，重派只允許產生**新** job 與**新** envelope，既有紀錄原樣保留。

        與 `_manager_reset_workflow_for_retry_verify`／`..._retry_review` 的差別
        同樣是刻意的：那兩者是 **phase 級**重置（整個 phase 的 step 全打回
        pending、清 gate_refs，且把舊 exited job 改標 failed），本方法只動**指名
        的那一張卡**，舊 job 一個位元組都不動。

        `card` 必須精確等於「當前 phase 內最早一張 gate_result 非 passed 的
        卡」——那也正是 `manager._current_workflow_step` 之後會派的那一張，兩邊
        同一判準，避免宣告可行的重派實際落到另一張卡上（#382 的教訓）。
        """

        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        if current.run_id != expected_run_id:
            raise ValueError("retry-card reset expected WorkflowRun CAS mismatch")
        phase = current.current_phase
        if (
            current.status != "ongoing"
            or phase not in RETRY_CARD_PHASE_PERSONA
            or "needs_human" not in current.facets
        ):
            raise ValueError(
                "retry-card reset requires active needs_human "
                "build/verify/review workflow"
            )
        expected_persona = RETRY_CARD_PHASE_PERSONA[phase]
        if any(
            job.get("workflow_run_id") == current.run_id
            and job.get("status") in ACTIVE_JOB_STATUSES
            for job in self._jobs
        ):
            raise ValueError("retry-card reset refuses active workflow job")
        pending = [
            step
            for step in current.steps
            if step.phase == phase and step.gate_result != "passed"
        ]
        if not pending or pending[0].card != card:
            raise ValueError(
                f"retry-card reset requires the earliest un-accepted {expected_persona} card"
            )
        if pending[0].persona != expected_persona:
            raise ValueError(f"retry-card reset requires a {expected_persona} card")
        # Keep the registry-side atomic guard aligned with work_actions: an
        # accepted earlier builder attempt may be followed by a newer terminal
        # provider failure before any envelope is bound.  That failure
        # supersedes only the current attempt; it never mutates old evidence.
        matching_card_jobs = [
            job
            for job in self._jobs
            if job.get("workflow_run_id") == current.run_id
            and job.get("workflow_phase") == phase
            and job.get("workflow_card") == card
            and (phase == "build" or job.get("subject_head") == current.candidate_head)
            and job.get("workflow_claim_key") in (None, current.claim_key)
        ]
        latest_card_job = matching_card_jobs[-1] if matching_card_jobs else None
        accepted_evidence = any(
            job.get("workflow_evidence") is not None for job in matching_card_jobs
        )
        superseded_by_failed_builder = bool(
            phase == "build"
            and latest_card_job is not None
            and latest_card_job.get("workflow_evidence") is None
            and latest_card_job.get("status") in TERMINAL_JOB_STATUSES
            and (
                latest_card_job.get("status") == "failed"
                or latest_card_job.get("exit_code") not in (None, 0)
            )
        )
        if accepted_evidence and not superseded_by_failed_builder:
            raise ValueError("retry-card reset refuses a card with accepted evidence")
        retry_card_count = _retry_card_redispatch_count(
            current.attempts,
            matching_job_count=len(matching_card_jobs),
            card_id=card,
        )
        if retry_card_count >= MAX_RETRY_CARD_REDISPATCHES:
            self._manager_update_workflow_run(
                current.run_id,
                needs_human_reason=_retry_card_budget_reason(
                    card,
                    retry_card_count,
                    source="coordinator.registry._manager_reset_workflow_for_retry_card",
                ),
            )
            raise ValueError(_retry_card_budget_message(card, retry_card_count))
        steps = tuple(
            # 只清掉「上一次是誰跑的」這類解析結果，讓下一次 dispatch 重新解析
            # identity；`action`／`inputs`／`outputs`／`test_policy` 等卡片契約
            # 原樣保留。#568 的 reviewer fail-over 正是依賴這個重新解析——複製舊
            # job 的 executor／model 等於把壞掉的身分再派一次。
            replace(step, executor=None, model=None, domain=None, gate_result="pending")
            if step.phase == phase and step.card == card
            else step
            for step in current.steps
        )
        # #555：operator 的顯式重派次數跨世代累計，不得被下方 schema retry reset 清掉。
        # 舊 run 若尚無這個 attempts key，從 0 起算（不以 job 數回推，見 _retry_card_redispatch_count）。
        # #717：operator 的顯式重派＝**重新給一輪 schema retry 額度**。
        #
        # 過去只 bump `attempts[phase]`，同一個 dict 上的 `schema-mismatch:<card>`
        # 原封不動留著。08-19 實機逐字後果：計數在前兩次派工就累到 2 → operator 下
        # `retry-card` 重派 → 新 job 只產生**一份**不合契約的 terminal 就
        # `seen >= MAX_SCHEMA_RETRIES` 判 exhausted，這一輪一次自動重試都沒有，
        # attention 卻寫「已達上限（2/2）」——operator 讀成「它剛剛試了兩次」。
        #
        # 本輪額度清零，但**累計觀測不清**：值搬到 `schema-mismatch-total:<card>`
        # 累加供成本診斷；operator 重派的 per-card 上限由獨立的
        # `retry-card:<card>` attempts 鍵計數，避免混淆兩種重派來源。
        card_retry_key = terminal_contract.schema_retry_attempt_key(card)
        card_total_key = terminal_contract.schema_mismatch_total_key(card)
        attempts = {
            key: value
            for key, value in current.attempts.items()
            if key != card_retry_key
        }
        attempts[phase] = current.attempts.get(phase, 0) + 1
        attempts[_retry_card_attempt_key(card)] = retry_card_count + 1
        carried = current.attempts.get(card_retry_key, 0)
        if carried:
            attempts[card_total_key] = current.attempts.get(card_total_key, 0) + carried
        effective_model_chain_override = current.model_chain_override
        if model_chain_override is not None:
            effective_model_chain_override = {
                **dict(current.model_chain_override or {}),
                **{persona: dict(row) for persona, row in model_chain_override.items()},
            }
        updated = replace(
            current,
            steps=steps,
            attempts=attempts,
            facets=tuple(facet for facet in current.facets if facet != "needs_human"),
            # 診斷 invariant：清掉 needs_human facet 就必須清掉理由——
            # 陳舊理由會讓 operator 讀到上一輪的原因（見 WorkflowRun.__post_init__）。
            needs_human_reason=None,
            gate_status="running",
            retry_classification=(
                current.retry_classification
                if retry_classification is None
                else retry_classification
            ),
            model_chain_override=effective_model_chain_override,
            updated_at=_now_iso(),
        )
        self._workflows[index] = updated
        self._persist()
        return self._copy_workflow_run(updated)

    def _manager_adopt_repair_candidate(
        self,
        run_id: str,
        *,
        expected_candidate: str,
        adopted_candidate: str,
        adoption_job_id: str,
        evidence_ref: str,
    ) -> WorkflowRun:
        """Atomically bind an adopted repair commit as the new build Candidate (#260).

        比照 `_manager_reset_workflow_for_retry_build` 的 CAS／admission 風格：驗
        run 狀態、final build card pending、無 active job 後，在單一 persist 內完成
        candidate 換綁、final build step 標 passed（identity 取自 adoption job）、
        phase 前進 verify。``expected_candidate`` 是原（舊）candidate 的 CAS，
        ``adopted_candidate`` 才是要換綁的新 SHA——呼叫端（work_actions）已完成
        git 事實驗證，這裡只再次原子重驗 run 狀態未在期間漂移。
        """

        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        if (
            current.status != "ongoing"
            or current.current_phase != "build"
            or "needs_human" not in current.facets
        ):
            raise ValueError(
                "repair-commit adoption requires active needs_human build workflow"
            )
        if current.candidate_head != expected_candidate:
            raise ValueError("repair-commit adoption expected Candidate CAS mismatch")
        if (
            not isinstance(adopted_candidate, str)
            or verification.SAFE_SHA_RE.fullmatch(adopted_candidate) is None
        ):
            raise ValueError("repair-commit adoption requires exact adopted candidate")
        if not isinstance(evidence_ref, str) or not evidence_ref:
            raise ValueError("repair-commit adoption evidence ref missing")
        if any(
            job.get("workflow_run_id") == current.run_id
            and job.get("status") in ACTIVE_JOB_STATUSES
            for job in self._jobs
        ):
            raise ValueError("repair-commit adoption refuses active workflow job")
        build_steps = [step for step in current.steps if step.phase == "build"]
        if not build_steps:
            raise ValueError("repair-commit adoption requires build phase")
        if (
            any(step.gate_result != "passed" for step in build_steps[:-1])
            or build_steps[-1].gate_result != "pending"
        ):
            raise ValueError(
                "repair-commit adoption requires only the final builder card pending"
            )
        repair_card = build_steps[-1].card
        adoption_job = self._find_job(adoption_job_id)
        expected_job_fields = {
            "workflow_run_id": current.run_id,
            "workflow_claim_key": current.claim_key,
            "workflow_repo": current.repo,
            "workflow_card": repair_card,
            "workflow_phase": "build",
            "persona": "builder",
            "source_revision": current.source_revision,
            "subject_head": adopted_candidate,
            "status": "exited",
            "exit_code": 0,
        }
        for field, value in expected_job_fields.items():
            if adoption_job.get(field) != value:
                raise ValueError(f"repair-commit adoption job binding mismatch: {field}")
        if adoption_job.get("workflow_evidence") is None:
            raise ValueError("repair-commit adoption job requires bound evidence")
        identity_executor = adoption_job.get("executor")
        identity_model = adoption_job.get("model_id")
        identity_domain = adoption_job.get("independence_domain")
        if (
            not isinstance(identity_executor, str)
            or not isinstance(identity_model, str)
            or not isinstance(identity_domain, str)
        ):
            raise ValueError("repair-commit adoption job identity missing")
        steps = tuple(
            replace(
                step,
                executor=identity_executor,
                model=identity_model,
                domain=identity_domain,
                gate_result="passed",
            )
            if step.phase == "build" and step.card == repair_card
            else step
            for step in current.steps
        )
        evidence_refs = current.evidence_refs
        if evidence_ref not in evidence_refs:
            evidence_refs = (*evidence_refs, evidence_ref)
        updated = replace(
            current,
            current_phase="verify",
            steps=steps,
            attempts={
                **current.attempts,
                "verify": current.attempts.get("verify", 0) + 1,
            },
            gate_refs=tuple(ref for ref in current.gate_refs if ref.kind == "brainstorm"),
            candidate_head=adopted_candidate,
            verified_head=None,
            facets=tuple(facet for facet in current.facets if facet != "needs_human"),
            # 診斷 invariant：清掉 needs_human facet 就必須清掉理由——
            # 陳舊理由會讓 operator 讀到上一輪的原因（見 WorkflowRun.__post_init__）。
            needs_human_reason=None,
            gate_status="running",
            evidence_refs=evidence_refs,
            updated_at=_now_iso(),
        )
        self._workflows[index] = updated
        self._persist()
        return self._copy_workflow_run(updated)

    def _manager_reset_workflow_for_retry_verify(
        self,
        run_id: str,
        *,
        expected_candidate: str,
        retry_classification: str | None = None,
        reviewer_recovery_checker: (
            Callable[[Mapping[str, Any], WorkflowRun], bool] | None
        ) = None,
    ) -> WorkflowRun:
        """Atomically rerun verification only, keeping the exact unchanged Candidate (#216).

        比照 `_manager_reset_workflow_for_retry_build` 的 CAS／admission 風格，但只
        把 verify step 打回 pending——build phase（已產出的 Candidate）完全不動，
        不重建 candidate、不消耗 build attempts。
        """

        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        if (
            current.status != "ongoing"
            or current.current_phase != "verify"
            or "needs_human" not in current.facets
        ):
            raise ValueError(
                "retry-verify reset requires active needs_human verify workflow"
            )
        if current.candidate_head != expected_candidate:
            raise ValueError("retry-verify reset Candidate CAS mismatch")
        if any(
            job.get("workflow_run_id") == current.run_id
            and job.get("status") in ACTIVE_JOB_STATUSES
            for job in self._jobs
        ):
            raise ValueError("retry-verify reset refuses active workflow job")
        build_steps = [step for step in current.steps if step.phase == "build"]
        if not build_steps or any(step.gate_result != "passed" for step in build_steps):
            raise ValueError("retry-verify reset requires completed build phase")
        # #315：一般舊 exited verify job 標記 failed，讓 explicit resume 走 replacement
        # dispatch。若 Manager 的完整判準確認 job 仍可供精準 reviewer terminal
        # recovery 採用，則保留 exited，避免關閉免費復原路徑。
        for job in self._jobs:
            if (
                job.get("workflow_run_id") == current.run_id
                and job.get("workflow_phase") == "verify"
                and job.get("status") == "exited"
            ):
                if reviewer_recovery_checker is not None and reviewer_recovery_checker(
                    job, current
                ):
                    continue
                job["status"] = "failed"
        steps = tuple(
            replace(step, gate_result="pending") if step.phase == "verify" else step
            for step in current.steps
        )
        updated = replace(
            current,
            current_phase="verify",
            steps=steps,
            attempts={
                **current.attempts,
                "verify": current.attempts.get("verify", 0) + 1,
            },
            gate_refs=tuple(ref for ref in current.gate_refs if ref.kind == "brainstorm"),
            verified_head=None,
            facets=tuple(facet for facet in current.facets if facet != "needs_human"),
            # 診斷 invariant：清掉 needs_human facet 就必須清掉理由——
            # 陳舊理由會讓 operator 讀到上一輪的原因（見 WorkflowRun.__post_init__）。
            needs_human_reason=None,
            gate_status="running",
            retry_classification=(
                current.retry_classification
                if retry_classification is None
                else retry_classification
            ),
            updated_at=_now_iso(),
        )
        self._workflows[index] = updated
        self._persist()
        return self._copy_workflow_run(updated)

    def _manager_reset_workflow_for_retry_review(
        self,
        run_id: str,
        *,
        expected_candidate: str,
        retry_classification: str | None = None,
        reviewer_recovery_checker: (
            Callable[[Mapping[str, Any], WorkflowRun], bool] | None
        ) = None,
    ) -> WorkflowRun:
        """Atomically relaunch foreign review only, keeping the verified Candidate (#216).

        build／verify phase 保持不動（verified_head 也保留）——只重開 review
        step，不重跑 builder、不重建 candidate。
        """

        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        if (
            current.status != "ongoing"
            or current.current_phase != "review"
            or "needs_human" not in current.facets
        ):
            raise ValueError(
                "retry-review reset requires active needs_human review workflow"
            )
        if (
            current.candidate_head != expected_candidate
            or current.verified_head != expected_candidate
        ):
            raise ValueError("retry-review reset Candidate CAS mismatch")
        if any(
            job.get("workflow_run_id") == current.run_id
            and job.get("status") in ACTIVE_JOB_STATUSES
            for job in self._jobs
        ):
            raise ValueError("retry-review reset refuses active workflow job")
        verify_steps = [step for step in current.steps if step.phase == "verify"]
        if not verify_steps or any(step.gate_result != "passed" for step in verify_steps):
            raise ValueError("retry-review reset requires completed verify phase")
        # #315：一般舊 exited review job 標記 failed，讓 explicit resume 走 replacement
        # dispatch。若 Manager 的完整判準確認 job 仍可供精準 reviewer terminal
        # recovery 採用，則保留 exited，避免關閉免費復原路徑。
        for job in self._jobs:
            if (
                job.get("workflow_run_id") == current.run_id
                and job.get("workflow_phase") == "review"
                and job.get("status") == "exited"
            ):
                if reviewer_recovery_checker is not None and reviewer_recovery_checker(
                    job, current
                ):
                    continue
                job["status"] = "failed"
        steps = tuple(
            replace(step, gate_result="pending") if step.phase == "review" else step
            for step in current.steps
        )
        updated = replace(
            current,
            current_phase="review",
            steps=steps,
            attempts={
                **current.attempts,
                "review": current.attempts.get("review", 0) + 1,
            },
            gate_refs=tuple(ref for ref in current.gate_refs if ref.kind == "brainstorm"),
            facets=tuple(facet for facet in current.facets if facet != "needs_human"),
            # 診斷 invariant：清掉 needs_human facet 就必須清掉理由——
            # 陳舊理由會讓 operator 讀到上一輪的原因（見 WorkflowRun.__post_init__）。
            needs_human_reason=None,
            gate_status="running",
            retry_classification=(
                current.retry_classification
                if retry_classification is None
                else retry_classification
            ),
            updated_at=_now_iso(),
        )
        self._workflows[index] = updated
        self._persist()
        return self._copy_workflow_run(updated)

    def _manager_reset_workflow_for_authority_restart(
        self,
        run_id: str,
        *,
        authority_digest: str,
    ) -> WorkflowRun:
        """Atomically invalidate stale verify/review gates after a bound WorkAuthority
        declaration changes (#216).

        只精準 invalidate 依賴 authority 內容（issue/PR/OpenSpec 宣告）的 verify/
        review gate——比照 `_manager_reset_workflow_after_archive` 的『只清 verify/
        review』模式，build phase 已產出的 Candidate 維持不變，不是
        `_manager_reset_workflow_for_retry_build` 那種整個 build phase 級重置。

        #373：`claim_key` 必須跟著 `authority_digest` 同步重算，否則
        `work_actions._claim_action` 的觸發條件
        （`canonical_run.claim_key != _expected_claim_key(authority)`）在 reset
        之後仍然為真——同一個未再變動的 authority digest 會讓每一次 automatic
        scan（每個 daemon tick）都重新判定為「authority 已變更」，重跑本函式：
        剝除 needs_human、attempts["verify"] 無界累加，並讓
        `manager.resume_workflow_run` 在同一 tick 內撞上已改寫的
        `source_revision` 造成 workflow job binding mismatch，形成永久重觸發
        迴圈。`claim_key` 同步後，下一次 automatic scan 只要 authority 沒有
        再變，觸發條件即為假，reset 只在 authority 真的前進時才會再次發生。
        """

        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        if current.status != "ongoing" or current.current_phase not in {"verify", "review"}:
            raise ValueError(
                "authority-restart reset requires ongoing verify/review workflow"
            )
        if (
            not isinstance(authority_digest, str)
            or len(authority_digest) != 64
            or any(char not in "0123456789abcdef" for char in authority_digest)
        ):
            raise ValueError("authority-restart reset requires exact authority digest")
        if any(
            job.get("workflow_run_id") == current.run_id
            and job.get("status") in ACTIVE_JOB_STATUSES
            for job in self._jobs
        ):
            raise ValueError("authority-restart reset refuses active workflow job")
        steps = tuple(
            replace(step, gate_result="pending")
            if step.phase in {"verify", "review"}
            else step
            for step in current.steps
        )
        updated = replace(
            current,
            current_phase="verify",
            steps=steps,
            attempts={
                **current.attempts,
                "verify": current.attempts.get("verify", 0) + 1,
            },
            gate_refs=tuple(ref for ref in current.gate_refs if ref.kind == "brainstorm"),
            # #373 根治：claim_key 必須與 authority_digest 同步重算（見上方
            # docstring），否則觸發條件永久為真，形成每 tick 重新 reset 的迴圈。
            claim_key=claim_key_for_authority_digest(
                repo=current.repo, work_id=current.work_id, authority_digest=authority_digest
            ),
            source_revision=authority_digest,
            verified_head=None,
            facets=tuple(facet for facet in current.facets if facet != "needs_human"),
            # 診斷 invariant：清掉 needs_human facet 就必須清掉理由——
            # 陳舊理由會讓 operator 讀到上一輪的原因（見 WorkflowRun.__post_init__）。
            needs_human_reason=None,
            gate_status="running",
            retry_classification="authority_restart",
            updated_at=_now_iso(),
        )
        self._workflows[index] = updated
        self._persist()
        return self._copy_workflow_run(updated)

    def _manager_validate_workflow_abandon(
        self,
        run_id: str,
        *,
        evidence_ref: str,
    ) -> WorkflowRun:
        """Read-only admission check for one exact pre-delivery abandon action."""

        if not isinstance(evidence_ref, str) or not evidence_ref:
            raise ValueError("workflow abandon evidence ref missing")
        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        if current.status == "superseded":
            if evidence_ref not in current.evidence_refs:
                raise ValueError("workflow already superseded by different authority")
            return self._copy_workflow_run(current)
        if current.status != "ongoing":
            raise ValueError("workflow abandon requires ongoing run")
        if any(
            job.get("workflow_run_id") == current.run_id
            and job.get("status") in ACTIVE_JOB_STATUSES
            for job in self._jobs
        ):
            raise ValueError("workflow abandon refuses active workflow job")
        if (
            current.current_phase == "ship"
            or current.pr_refs
            or any(
                step.phase == "ship" and step.gate_result == "passed"
                for step in current.steps
            )
            or current.completion_record_path is not None
        ):
            raise ValueError("workflow abandon only permits pre-delivery run")
        return self._copy_workflow_run(current)

    def _manager_abandon_workflow_run(
        self,
        run_id: str,
        *,
        evidence_ref: str,
    ) -> WorkflowRun:
        """Supersede one exact pre-delivery run after an explicit operator action."""

        self._manager_validate_workflow_abandon(
            run_id,
            evidence_ref=evidence_ref,
        )
        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        if current.status == "superseded":
            return self._copy_workflow_run(current)
        updated = replace(
            current,
            status="superseded",
            facets=tuple(sorted(set(current.facets) | {"blocked", "planning_released"})),
            evidence_refs=(*current.evidence_refs, evidence_ref),
            updated_at=_now_iso(),
        )
        self._workflows[index] = updated
        self._persist()
        return self._copy_workflow_run(updated)

    def _manager_validate_workflow_retire_delivered(
        self,
        run_id: str,
        *,
        evidence_ref: str,
    ) -> WorkflowRun:
        """Read-only admission check for retiring one *delivered* orphan run.

        ``_manager_validate_workflow_abandon`` only admits a *pre-delivery*
        run (no ``pr_refs``, not shipping, no completion record) — so a run
        whose delivery already happened outside the cortex pipeline (a
        fallback subagent built and merged the PR directly) is stuck: it
        carries terminal ``pr_refs`` yet can neither ship (a dirty candidate)
        nor abandon (the pre-delivery gate). This sibling admits exactly that
        orphan: an ``ongoing`` run that REQUIRES ``pr_refs`` (the delivered
        signature), trusting the work-action layer to have proven every one of
        those PRs is terminal (merged/closed) *before* calling. The registry
        stays pure and never touches GitHub itself; existing ``abandon`` is
        left untouched — pre-delivery runs must still use the strict path.
        """

        if not isinstance(evidence_ref, str) or not evidence_ref:
            raise ValueError("workflow retire-delivered evidence ref missing")
        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        if current.status == "superseded":
            if evidence_ref not in current.evidence_refs:
                raise ValueError("workflow already superseded by different authority")
            return self._copy_workflow_run(current)
        if current.status != "ongoing":
            raise ValueError("workflow retire-delivered requires ongoing run")
        if any(
            job.get("workflow_run_id") == current.run_id
            and job.get("status") in ACTIVE_JOB_STATUSES
            for job in self._jobs
        ):
            raise ValueError("workflow retire-delivered refuses active workflow job")
        if not current.pr_refs:
            raise ValueError(
                "workflow retire-delivered requires a delivered run with pr refs"
            )
        return self._copy_workflow_run(current)

    def _manager_retire_delivered_workflow_run(
        self,
        run_id: str,
        *,
        evidence_ref: str,
    ) -> WorkflowRun:
        """Supersede one delivered orphan run whose PRs are all terminal.

        The state transition is byte-identical to
        ``_manager_abandon_workflow_run`` — the only difference is the
        admission gate above. Idempotent re-entry (run already ``superseded``
        by this same evidence) returns the current copy, matching the abandon
        crash-window contract (#275).
        """

        self._manager_validate_workflow_retire_delivered(
            run_id,
            evidence_ref=evidence_ref,
        )
        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        if current.status == "superseded":
            return self._copy_workflow_run(current)
        updated = replace(
            current,
            status="superseded",
            facets=tuple(sorted(set(current.facets) | {"blocked", "planning_released"})),
            evidence_refs=(*current.evidence_refs, evidence_ref),
            updated_at=_now_iso(),
        )
        self._workflows[index] = updated
        self._persist()
        return self._copy_workflow_run(updated)
