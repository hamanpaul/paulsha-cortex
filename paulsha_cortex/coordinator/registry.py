from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from paulsha_cortex.config import paths

# job.status 合法值（設計 §7 / spec）
VALID_STATUSES = frozenset({"dispatched", "running", "done", "failed"})


def _default_state_path() -> Path:
    return paths.coordinator_root() / "jobs.json"


def _now_iso() -> str:
    # created_at 用於人讀/排序；job_id 不含時間（確定性由 _seq 保證）
    return datetime.now(timezone.utc).isoformat()


class JobRegistry:
    """Job 狀態的持久化 registry。

    - job_id 確定性：f"{task}-{seq}"，seq 為內部單調計數器（非時間/亂數）。
    - 狀態檔結構：{"seq": int, "jobs": [job, ...]}，JSON 持久化。
    - corrupt/不可解析狀態檔 → raise（fail-closed），MUST NOT 靜默清空。
    - 狀態檔不存在 → 空 registry（首次使用，非錯誤）。
    """

    def __init__(self, state_path: str | Path | None = None, seq_start: int = 0) -> None:
        self._state_path = Path(state_path) if state_path is not None else _default_state_path()
        self._jobs: list[dict[str, object]] = []
        self._seq = seq_start
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        if not self._state_path.is_file():
            return  # 不存在 → 空 registry
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"coordinator 狀態檔解析失敗（fail-closed）: {self._state_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError(f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}")
        seq = payload.get("seq", 0)
        if not isinstance(seq, int):
            raise ValueError(f"coordinator 狀態檔 seq 型別錯誤（fail-closed）: {self._state_path}")
        # 每筆 job MUST 為 dict 且含必要鍵（job_id/status）；否則 fail-closed。
        # 防 dict() 對非 dict（如 [["a","b"]]）靜默吞成畸形 job，事後在 get/update 才炸。
        for job in payload["jobs"]:
            if not isinstance(job, dict) or "job_id" not in job or "status" not in job:
                raise ValueError(
                    f"coordinator 狀態檔格式錯誤（fail-closed）: {self._state_path}"
                )
        self._jobs = [dict(job) for job in payload["jobs"]]
        # 重載後計數器續編：max(載入 seq, 現有 seq)，避免撞號
        self._seq = max(seq, self._seq)

    def _persist(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"seq": self._seq, "jobs": self._jobs}
        # 原子寫：先寫暫存再 replace，避免中斷留半檔
        fd, tmp = tempfile.mkstemp(dir=str(self._state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self._state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    # ---- CRUD ----
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
    ) -> dict[str, object]:
        self._seq += 1
        job: dict[str, object] = {
            "job_id": f"{task}-{self._seq}",
            "task": task,
            "persona": persona,
            "branch": branch,
            "pane": pane,
            "worktree": worktree,
            "status": "dispatched",
            # D5：dispatch 當下的 branch head（baseline），持久化於 job 上供
            # 跨進程的 poll_done 比對；取不到則為 None。
            "dispatch_head": dispatch_head,
            "executor": executor,
            "session_name": session_name,
            "pid": pid,
            "log_path": log_path,
            "exit_code": exit_code,
            "created_at": _now_iso(),
        }
        self._jobs.append(job)
        self._persist()
        return dict(job)

    def list_jobs(self) -> list[dict[str, object]]:
        return [dict(job) for job in self._jobs]

    def get_job(self, job_id: str) -> dict[str, object]:
        for job in self._jobs:
            if job["job_id"] == job_id:
                return dict(job)
        raise KeyError(f"job 不存在: {job_id}")

    def update_status(self, job_id: str, status: str) -> dict[str, object]:
        if status not in VALID_STATUSES:
            raise ValueError(f"非法 status: {status!r}（須為 {sorted(VALID_STATUSES)} 之一）")
        for job in self._jobs:
            if job["job_id"] == job_id:
                job["status"] = status
                self._persist()
                return dict(job)
        raise KeyError(f"job 不存在: {job_id}")

    def attach_launch_handle(
        self,
        job_id: str,
        *,
        executor: str | None = None,
        session_name: str | None = None,
        pid: int | None = None,
        log_path: str | None = None,
    ) -> dict[str, object]:
        """Fill in launch-handle fields on a row created before launch.

        Used so the registry row can be persisted *before* the agent process is
        started (crash-recovery), then updated with the handle once launch
        returns.
        """
        for job in self._jobs:
            if job["job_id"] == job_id:
                job["executor"] = executor
                job["session_name"] = session_name
                job["pid"] = pid
                job["log_path"] = log_path
                self._persist()
                return dict(job)
        raise KeyError(f"job 不存在: {job_id}")

    def update_headless_result(
        self,
        job_id: str,
        *,
        status: str,
        exit_code: int,
    ) -> dict[str, object]:
        if status not in {"done", "failed"}:
            raise ValueError(
                f"headless 完成結果 status 須為 'done' 或 'failed'，收到: {status!r}"
            )
        for job in self._jobs:
            if job["job_id"] == job_id:
                job["status"] = status
                job["exit_code"] = exit_code
                self._persist()
                return dict(job)
        raise KeyError(f"job 不存在: {job_id}")
