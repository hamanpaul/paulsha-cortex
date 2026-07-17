from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paulsha_cortex.config import paths
from .workflow import GateEvidenceRef, WorkflowRun, WorkflowStep, validate_workflow_phase_transition

COORDINATOR_STATE_SCHEMA_VERSION = 2

VALID_JOB_STATUSES = frozenset({"dispatched", "running", "exited", "failed"})
ACTIVE_JOB_STATUSES = frozenset({"dispatched", "running"})
TERMINAL_JOB_STATUSES = frozenset({"exited", "failed"})

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
    "needs_human": frozenset({"needs_human", "building", "reviewing", "verified", "failed", "completed"}),
    "failed": frozenset({"failed"}),
}
GATE_STATE_TRANSITIONS = {
    "pending": frozenset({"pending", "passed", "failed", "needs_human"}),
    "passed": frozenset({"passed"}),
    "failed": frozenset({"failed"}),
    "needs_human": frozenset({"needs_human", "pending", "passed", "failed"}),
}


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


def _empty_legacy_records() -> dict[str, Any]:
    return {"source_schema_version": 1, "seq": 0, "jobs": [], "slices": []}


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
        self._state_path = Path(state_path) if state_path is not None else _default_state_path()
        self._jobs: list[dict[str, Any]] = []
        self._slices: list[dict[str, Any]] = []
        self._workflows: list[WorkflowRun] = []
        self._legacy_records: dict[str, Any] = _empty_legacy_records()
        self._seq = seq_start
        self._load()

    def _load(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            original = self._state_path.read_bytes()
            payload = json.loads(original.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"coordinator 狀態檔解析失敗（fail-closed）: {self._state_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        schema_version = payload.get("schema_version")
        if schema_version == 1:
            jobs, slices, seq = self._validate_state_records(payload)
            legacy_records = {
                "source_schema_version": 1,
                "seq": seq,
                "jobs": jobs,
                "slices": slices,
            }
            migrated = {
                "schema_version": COORDINATOR_STATE_SCHEMA_VERSION,
                "seq": seq,
                "jobs": [],
                "slices": [],
                "workflows": [],
                "legacy_records": legacy_records,
            }
            self._write_v1_backup(original)
            self._write_payload_atomically(migrated)
            self._legacy_records = _deepcopy_json(legacy_records)
            self._seq = max(seq, self._seq)
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
        claim_keys = [run.claim_key for run in validated_workflows]
        run_ids = [run.run_id for run in validated_workflows]
        if len(set(claim_keys)) != len(claim_keys) or len(set(run_ids)) != len(run_ids):
            raise ValueError(f"coordinator 狀態檔 workflow 重複識別（fail-closed）: {self._state_path}")
        self._validate_legacy_records(legacy_records)
        self._jobs = jobs
        self._slices = slices
        self._workflows = validated_workflows
        self._legacy_records = _deepcopy_json(legacy_records)
        self._seq = max(seq, self._seq)

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
        validated_slices = [self._validate_loaded_slice(slice_row, job_ids) for slice_row in slices]
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

    def _write_payload_atomically(self, payload: dict[str, Any]) -> None:
        directory = self._state_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
        tmp = Path(tmp_name)
        backup: Path | None = None
        had_original = self._state_path.is_file()
        replaced = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            if had_original:
                backup_fd, backup_name = tempfile.mkstemp(
                    dir=str(directory), suffix=".rollback.bak"
                )
                backup = Path(backup_name)
                with os.fdopen(backup_fd, "wb") as handle:
                    handle.write(self._state_path.read_bytes())
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(tmp, self._state_path)
            replaced = True
            _fsync_directory(directory)
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

    def _persist(self) -> None:
        payload = {
            "schema_version": COORDINATOR_STATE_SCHEMA_VERSION,
            "seq": self._seq,
            "jobs": self._jobs,
            "slices": self._slices,
            "workflows": [run.to_dict() for run in self._workflows],
            "legacy_records": self._legacy_records,
        }
        try:
            self._write_payload_atomically(payload)
        except BaseException:
            # _write_payload_atomically restores the previous durable file.
            # Reload that exact snapshot so every mutation site, including
            # legacy job/slice methods, rolls memory back consistently too.
            self._jobs = []
            self._slices = []
            self._workflows = []
            self._legacy_records = _empty_legacy_records()
            self._seq = 0
            self._load()
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
        for field in ("executor", "session_name", "log_path", "model_id", "independence_domain"):
            value = job.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"coordinator 狀態檔 {field} 格式錯誤（fail-closed）: {self._state_path}")
        for field in ("pid", "exit_code"):
            value = job.get(field)
            if value is not None and not isinstance(value, int):
                raise ValueError(f"coordinator 狀態檔 {field} 格式錯誤（fail-closed）: {self._state_path}")
        for field in ("subject_head",):
            value = job.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"coordinator 狀態檔 {field} 格式錯誤（fail-closed）: {self._state_path}")
        return dict(job)

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
        return {
            **dict(slice_row),
            "spec": dict(slice_row["spec"]),
            "plan": dict(slice_row["plan"]),
            "verification": dict(slice_row["verification"]),
            "current_evidence_refs": list(slice_row["current_evidence_refs"]),
            "current_evaluation_refs": list(slice_row["current_evaluation_refs"]),
            "evidence_history": _copy_json_list(slice_row["evidence_history"]),
            "evaluation_history": _copy_json_list(slice_row["evaluation_history"]),
            "actions": _copy_json_list(slice_row["actions"]),
        }

    def _find_job(self, job_id: str) -> dict[str, Any]:
        for job in self._jobs:
            if job["job_id"] == job_id:
                return job
        raise KeyError(f"job 不存在: {job_id}")

    def _find_slice(self, slice_id: str) -> dict[str, Any]:
        for slice_row in self._slices:
            if slice_row["slice_id"] == slice_id:
                return slice_row
        raise KeyError(f"slice 不存在: {slice_id}")

    def _copy_slice(self, slice_row: dict[str, Any]) -> dict[str, Any]:
        return {
            **dict(slice_row),
            "spec": dict(slice_row["spec"]),
            "plan": dict(slice_row["plan"]),
            "verification": dict(slice_row["verification"]),
            "current_evidence_refs": list(slice_row["current_evidence_refs"]),
            "current_evaluation_refs": list(slice_row["current_evaluation_refs"]),
            "evidence_history": _copy_json_list(slice_row["evidence_history"]),
            "evaluation_history": _copy_json_list(slice_row["evaluation_history"]),
            "actions": _copy_json_list(slice_row["actions"]),
        }

    def _validate_existing_job_ref(self, field: str, job_id: str | None) -> None:
        if job_id is None:
            return
        try:
            self._find_job(job_id)
        except KeyError as exc:
            raise ValueError(f"{field} 指向不存在 job: {job_id}") from exc

    def create_job(
        self,
        *,
        task: str,
        persona: str,
        branch: str,
        pane: str,
        worktree: str,
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
        self._seq += 1
        job: dict[str, Any] = {
            "job_id": f"{task}-{self._seq}",
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
            "exit_code": exit_code,
            "subject_head": subject_head,
            "spec_hash": spec_hash,
            "plan_hash": plan_hash,
            "verification_hash": verification_hash,
            "created_at": _now_iso(),
        }
        self._jobs.append(job)
        self._persist()
        return dict(job)

    def list_jobs(self) -> list[dict[str, Any]]:
        return [dict(job) for job in self._jobs]

    def get_job(self, job_id: str) -> dict[str, Any]:
        return dict(self._find_job(job_id))

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
        return dict(job)

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
        return dict(job)

    def attach_launch_handle(
        self,
        job_id: str,
        *,
        executor: str | None = None,
        model_id: str | None = None,
        session_name: str | None = None,
        pid: int | None = None,
        log_path: str | None = None,
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
        self._persist()
        return dict(job)

    def update_headless_result(
        self,
        job_id: str,
        *,
        status: str,
        exit_code: int,
    ) -> dict[str, Any]:
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError(
                f"headless 完成結果 status 須為 'exited' 或 'failed'，收到: {status!r}"
            )
        job = self._find_job(job_id)
        _validate_transition(
            field="job status",
            current=str(job["status"]),
            new=status,
            allowed=JOB_STATUS_TRANSITIONS,
        )
        job["status"] = status
        job["exit_code"] = exit_code
        self._persist()
        return dict(job)

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
    ) -> dict[str, Any]:
        if any(row["slice_id"] == slice_id for row in self._slices):
            raise ValueError(f"slice 已存在: {slice_id}")
        self._validate_existing_job_ref("builder_job_id", builder_job_id)
        self._validate_existing_job_ref("reviewer_job_id", reviewer_job_id)
        now = _now_iso()
        slice_row = {
            "slice_id": slice_id,
            "spec": {"path": spec_path, "hash": spec_hash},
            "plan": {"path": plan_path, "hash": plan_hash},
            "target_branch": target_branch,
            "target_remote": target_remote,
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
            "current_evidence_refs": [],
            "current_evaluation_refs": [],
            "evidence_history": [],
            "evaluation_history": [],
            "actions": [],
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
    ) -> dict[str, Any]:
        slice_row = self._find_slice(slice_id)
        if str(slice_row["state"]) not in {"pending", "needs_human"}:
            raise ValueError(
                f"非法 slice state repin: {slice_row['state']!r}（只允許 pending/needs_human 重派）"
            )
        _validate_transition(
            field="gate_state",
            current=str(slice_row["gate_state"]),
            new="pending",
            allowed=GATE_STATE_TRANSITIONS,
        )
        slice_row["spec"] = {"path": spec_path, "hash": spec_hash}
        slice_row["plan"] = {"path": plan_path, "hash": plan_hash}
        slice_row["target_branch"] = target_branch
        slice_row["target_remote"] = target_remote
        slice_row["verification"] = {
            "hash": verification_hash,
            "contract": dict(verification) if isinstance(verification, dict) else None,
        }
        slice_row["dispatch_base"] = dispatch_base
        slice_row["builder_job_id"] = None
        slice_row["reviewer_job_id"] = None
        slice_row["candidate"] = None
        slice_row["gate_state"] = "pending"
        slice_row["current_evidence_refs"] = []
        slice_row["current_evaluation_refs"] = []
        slice_row["updated_at"] = _now_iso()
        self._persist()
        return self._copy_slice(slice_row)

    def list_slices(self) -> list[dict[str, Any]]:
        return [self._copy_slice(slice_row) for slice_row in self._slices]

    def get_slice(self, slice_id: str) -> dict[str, Any]:
        return self._copy_slice(self._find_slice(slice_id))

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
        verification_hash: str | None = None,
    ) -> dict[str, Any]:
        slice_row = self._find_slice(slice_id)
        if state is not None:
            if state not in VALID_SLICE_STATES:
                raise ValueError(f"非法 slice state: {state!r}")
            _validate_transition(
                field="slice state",
                current=str(slice_row["state"]),
                new=state,
                allowed=SLICE_STATE_TRANSITIONS,
            )
            slice_row["state"] = state
        if gate_state is not None:
            if gate_state not in VALID_GATE_STATES:
                raise ValueError(f"非法 gate_state: {gate_state!r}")
            _validate_transition(
                field="gate_state",
                current=str(slice_row["gate_state"]),
                new=gate_state,
                allowed=GATE_STATE_TRANSITIONS,
            )
            slice_row["gate_state"] = gate_state
        if current_evidence_refs is not None:
            if not _is_ref_list(current_evidence_refs):
                raise ValueError("current_evidence_refs 必須為字串陣列")
            slice_row["current_evidence_refs"] = _copy_ref_list(current_evidence_refs)
        if current_evaluation_refs is not None:
            if not _is_ref_list(current_evaluation_refs):
                raise ValueError("current_evaluation_refs 必須為字串陣列")
            slice_row["current_evaluation_refs"] = _copy_ref_list(current_evaluation_refs)
        if builder_job_id is not None:
            self._validate_existing_job_ref("builder_job_id", builder_job_id)
            slice_row["builder_job_id"] = builder_job_id
        if reviewer_job_id is not None:
            self._validate_existing_job_ref("reviewer_job_id", reviewer_job_id)
            slice_row["reviewer_job_id"] = reviewer_job_id
        if candidate is not None:
            slice_row["candidate"] = candidate
        if dispatch_base is not None:
            slice_row["dispatch_base"] = dispatch_base
        if target_remote is not None:
            slice_row["target_remote"] = target_remote
        if verification_hash is not None:
            slice_row["verification"]["hash"] = verification_hash
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
        requested_at: str | None = None,
        consumed_at: str | None = None,
        result: str | None = None,
    ) -> dict[str, Any]:
        slice_row = self._find_slice(slice_id)
        if state is not None:
            if state not in VALID_SLICE_STATES:
                raise ValueError(f"非法 slice state: {state!r}")
            _validate_transition(
                field="slice state",
                current=str(slice_row["state"]),
                new=state,
                allowed=SLICE_STATE_TRANSITIONS,
            )
            slice_row["state"] = state
        if gate_state is not None:
            if gate_state not in VALID_GATE_STATES:
                raise ValueError(f"非法 gate_state: {gate_state!r}")
            _validate_transition(
                field="gate_state",
                current=str(slice_row["gate_state"]),
                new=gate_state,
                allowed=GATE_STATE_TRANSITIONS,
            )
            slice_row["gate_state"] = gate_state
        if evidence_refs is not None:
            if not _is_ref_list(evidence_refs):
                raise ValueError("evidence_refs 必須為字串陣列")
            refs = _copy_ref_list(evidence_refs)
            slice_row["current_evidence_refs"] = refs
            slice_row["evidence_history"].append(
                {"action": action, "actor": actor, "refs": refs, "at": _now_iso()}
            )
        if evaluation_refs is not None:
            if not _is_ref_list(evaluation_refs):
                raise ValueError("evaluation_refs 必須為字串陣列")
            refs = _copy_ref_list(evaluation_refs)
            slice_row["current_evaluation_refs"] = refs
            slice_row["evaluation_history"].append(
                {"action": action, "actor": actor, "refs": refs, "at": _now_iso()}
            )
        if candidate is not None:
            slice_row["candidate"] = candidate
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
        slice_row["actions"].append(action_entry)
        slice_row["updated_at"] = _now_iso()
        self._persist()
        return self._copy_slice(slice_row)

    def _find_workflow_run_index(self, run_id: str) -> int:
        for index, run in enumerate(self._workflows):
            if run.run_id == run_id:
                return index
        raise KeyError(f"workflow run 不存在: {run_id}")

    def _copy_workflow_run(self, run: WorkflowRun) -> WorkflowRun:
        return WorkflowRun.from_dict(run.to_dict())

    def list_legacy_records(self) -> dict[str, Any]:
        return _deepcopy_json(self._legacy_records)

    def list_workflow_runs(self) -> list[WorkflowRun]:
        return [self._copy_workflow_run(run) for run in self._workflows]

    def get_workflow_run(self, run_id: str) -> WorkflowRun:
        return self._copy_workflow_run(self._workflows[self._find_workflow_run_index(run_id)])

    def _manager_create_workflow_run(
        self,
        *,
        work_id: str,
        repo: str,
        claim_key: str,
        combo: str,
        current_phase: str,
        steps: tuple[WorkflowStep, ...],
        issue_refs: tuple[str, ...] = (),
        openspec_refs: tuple[str, ...] = (),
        pr_refs: tuple[str, ...] = (),
        attempts: dict[str, int] | None = None,
        evidence_refs: tuple[str, ...] = (),
        gate_refs: tuple[GateEvidenceRef, ...] = (),
        facets: tuple[str, ...] = (),
        gate_status: str = "pending",
    ) -> WorkflowRun:
        for existing in self._workflows:
            if existing.claim_key != claim_key:
                continue
            if existing.work_id != work_id or existing.repo != repo:
                raise ValueError(f"claim_key 已屬於其他 work item: {claim_key}")
            return self._copy_workflow_run(existing)

        run_id = f"workflow-{hashlib.sha256(claim_key.encode('utf-8')).hexdigest()[:20]}"
        if any(run.run_id == run_id for run in self._workflows):
            raise ValueError(f"workflow run id collision: {run_id}")
        now = _now_iso()
        run = WorkflowRun(
            run_id=run_id,
            work_id=work_id,
            repo=repo,
            claim_key=claim_key,
            combo=combo,
            current_phase=current_phase,
            steps=tuple(steps),
            issue_refs=tuple(issue_refs),
            openspec_refs=tuple(openspec_refs),
            pr_refs=tuple(pr_refs),
            attempts=dict(attempts or {}),
            evidence_refs=tuple(evidence_refs),
            gate_refs=tuple(gate_refs),
            facets=tuple(facets),
            gate_status=gate_status,
            created_at=now,
            updated_at=now,
        )
        self._workflows.append(run)
        self._persist()
        return self._copy_workflow_run(run)

    def _manager_update_workflow_run(
        self,
        run_id: str,
        *,
        current_phase: str | None = None,
        steps: tuple[WorkflowStep, ...] | None = None,
        issue_refs: tuple[str, ...] | None = None,
        openspec_refs: tuple[str, ...] | None = None,
        pr_refs: tuple[str, ...] | None = None,
        attempts: dict[str, int] | None = None,
        evidence_refs: tuple[str, ...] | None = None,
        gate_refs: tuple[GateEvidenceRef, ...] | None = None,
        facets: tuple[str, ...] | None = None,
        gate_status: str | None = None,
    ) -> WorkflowRun:
        index = self._find_workflow_run_index(run_id)
        current = self._workflows[index]
        next_phase = current.current_phase if current_phase is None else current_phase
        validate_workflow_phase_transition(current.current_phase, next_phase)
        updated = WorkflowRun(
            run_id=current.run_id,
            work_id=current.work_id,
            repo=current.repo,
            claim_key=current.claim_key,
            combo=current.combo,
            current_phase=next_phase,
            steps=current.steps if steps is None else tuple(steps),
            issue_refs=current.issue_refs if issue_refs is None else tuple(issue_refs),
            openspec_refs=current.openspec_refs if openspec_refs is None else tuple(openspec_refs),
            pr_refs=current.pr_refs if pr_refs is None else tuple(pr_refs),
            attempts=dict(current.attempts if attempts is None else attempts),
            evidence_refs=current.evidence_refs if evidence_refs is None else tuple(evidence_refs),
            gate_refs=current.gate_refs if gate_refs is None else tuple(gate_refs),
            facets=current.facets if facets is None else tuple(facets),
            gate_status=current.gate_status if gate_status is None else gate_status,
            created_at=current.created_at,
            updated_at=_now_iso(),
        )
        self._workflows[index] = updated
        self._persist()
        return self._copy_workflow_run(updated)
