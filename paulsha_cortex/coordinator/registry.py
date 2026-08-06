from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from paulsha_cortex.config import paths
from . import verification
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
    "needs_human": frozenset({"needs_human", "pending", "building", "reviewing", "verified", "failed", "completed"}),
    "failed": frozenset({"failed", "pending", "needs_human"}),
}
GATE_STATE_TRANSITIONS = {
    "pending": frozenset({"pending", "passed", "failed", "needs_human"}),
    "passed": frozenset({"passed"}),
    "failed": frozenset({"failed", "needs_human"}),
    "needs_human": frozenset({"needs_human", "pending", "passed", "failed"}),
}

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
        self._state_mtime_ns: int | None = None
        self._state_size: int | None = None
        self._load()

    def _record_state_file_metadata(self) -> None:
        try:
            stat = self._state_path.stat()
        except OSError:
            self._state_mtime_ns = None
            self._state_size = None
            return
        self._state_mtime_ns = stat.st_mtime_ns
        self._state_size = stat.st_size

    def _reload_if_changed(self) -> None:
        try:
            stat = self._state_path.stat()
        except OSError:
            return
        if self._state_mtime_ns == stat.st_mtime_ns and self._state_size == stat.st_size:
            return
        self._load()

    def _load(self) -> None:
        if not self._state_path.is_file():
            self._state_mtime_ns = None
            self._state_size = None
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
            self._record_state_file_metadata()
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
        self._jobs = jobs
        self._slices = slices
        self._workflows = validated_workflows
        self._legacy_records = _deepcopy_json(legacy_records)
        self._seq = max(seq, self._seq)
        self._record_state_file_metadata()

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
            self._record_state_file_metadata()
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
        for field in (
            "executor", "session_name", "log_path", "model_id", "independence_domain",
            "workflow_run_id", "workflow_claim_key", "workflow_repo", "workflow_card",
            "workflow_phase", "workflow_repo_root", "workflow_input_root", "source_revision",
            "workflow_sandbox_hash", "workflow_builder_job_id", "workflow_stage_execution_key",
        ):
            value = job.get(field)
            if value is not None and not isinstance(value, str):
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
        self._reload_if_changed()
        for job in self._jobs:
            if job["job_id"] == job_id:
                return job
        raise KeyError(f"job 不存在: {job_id}")

    def _find_slice(self, slice_id: str) -> dict[str, Any]:
        self._reload_if_changed()
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
        workflow_run_id: str | None = None,
        workflow_claim_key: str | None = None,
        workflow_repo: str | None = None,
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
        self._validate_existing_job_ref("workflow_builder_job_id", workflow_builder_job_id)
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
            "workflow_run_id": workflow_run_id,
            "workflow_claim_key": workflow_claim_key,
            "workflow_repo": workflow_repo,
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
            "workflow_evidence": None,
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
        return _deepcopy_json(job)

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
        self._reload_if_changed()
        return [self._copy_slice(slice_row) for slice_row in self._slices]

    def get_slice(self, slice_id: str) -> dict[str, Any]:
        self._reload_if_changed()
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
            facets=current.facets if facets is None else tuple(facets),
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
            frozen_readiness=(
                current.frozen_readiness if frozen_readiness is None else frozen_readiness
            ),
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
    ) -> WorkflowRun:
        """Atomically reopen only the final builder card after an explicit human stop."""

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

    def _manager_reset_workflow_for_retry_verify(
        self,
        run_id: str,
        *,
        expected_candidate: str,
        retry_classification: str | None = None,
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
        # #315：operator 已以 CAS 顯式授權重跑 verification；舊 exited verify job
        # 的 reviewer sandbox 依設計已清除、terminal 證據不可重驗，維持 "exited"
        # 會讓 dispatch 先 terminalize 舊 job 而永遠卡在 input-snapshot-missing。
        # 標記 failed 讓 explicit resume 走 replacement dispatch；build phase job
        # 與 active job（前面 admission 已擋）不受影響。
        for job in self._jobs:
            if (
                job.get("workflow_run_id") == current.run_id
                and job.get("workflow_phase") == "verify"
                and job.get("status") == "exited"
            ):
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
        # #315（review 版）：operator 已以 CAS 顯式授權重跑 review；舊 exited
        # review job 的 reviewer sandbox 已清、terminal 不可重驗，維持 "exited"
        # 會讓 resume 先 terminalize 舊 job 而卡死。標記 failed 讓 explicit
        # resume 走 replacement dispatch；verify／build job 不受影響。
        for job in self._jobs:
            if (
                job.get("workflow_run_id") == current.run_id
                and job.get("workflow_phase") == "review"
                and job.get("status") == "exited"
            ):
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
            source_revision=authority_digest,
            verified_head=None,
            facets=tuple(facet for facet in current.facets if facet != "needs_human"),
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
