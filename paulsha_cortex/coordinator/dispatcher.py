from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from .completion import classify_completion
from .registry import JobRegistry
from .seams import PaneSender, WorktreeCreator

# git_runner seam：收 git 參數、回 stdout 文字。預設真實作呼 git。
GitRunner = Callable[[list[str]], str]
# pid_waiter seam（向後相容）：收 pid，回該子進程的 exit code；仍在跑回 None。
# 注入此 seam 時走「呼叫者直接判定 exit code」舊路徑（單元測試用）。
PidWaiter = Callable[[int], int | None]
# pid_alive seam：收 pid，回該進程是否仍存活。預設 os.kill(pid, 0)。
# 跨進程安全：不依賴 os.waitpid（只有 spawn 該子進程的進程能 reap）。
PidAlive = Callable[[int], bool]


def _default_git_runner(args: list[str]) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _branch_for_task(task: str) -> str:
    return f"feature/{task}"


def exit_sentinel_path(log_path: str) -> Path:
    """由 job 的 log_path 推導 exit sentinel 檔路徑（<...>.jsonl → <...>.exit）。

    SubprocessLauncher 在子進程結束時把 `$?` 寫入此檔；poll_headless_done 跨進程讀回，
    故完成判定不再依賴 os.waitpid（只有 spawn 子進程的進程能 reap）。確定性、零 I/O。
    """
    return Path(log_path).with_suffix(".exit")


def _read_exit_sentinel(log_path: str | None) -> int | None:
    """讀 exit sentinel；不存在/壞檔 → None（視為尚未寫下 exit code）。"""
    if not log_path:
        return None
    p = exit_sentinel_path(log_path)
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8").strip()
    try:
        return int(text)
    except ValueError:
        return None


def _default_pid_alive(pid: int) -> bool:
    """os.kill(pid, 0)：無錯=存活；ProcessLookupError=已死；PermissionError=存活（非本人但在）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _last_nonempty_line(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    last_line = None
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            last_line = line
    return last_line


class Dispatcher:
    """派工原語：建 worktree → 送命令 → 記 job；poll_done 以 branch 新 commit 標 exited。

    所有副作用經注入 seam（PaneSender / WorktreeCreator / git_runner）；
    單元測試注入 fake，不啟動真 tmux/worktree/copilot。
    """

    def __init__(
        self,
        registry: JobRegistry,
        pane_sender: PaneSender,
        worktree_creator: WorktreeCreator,
        git_runner: GitRunner | None = None,
    ) -> None:
        self._registry = registry
        self._pane_sender = pane_sender
        self._worktree_creator = worktree_creator
        self._git_runner = git_runner

    def dispatch(
        self,
        *,
        task: str,
        persona: str,
        pane_id: str,
        command: str,
        base_sha: str | None = None,
        git_runner: GitRunner | None = None,
    ) -> dict[str, object]:
        branch = _branch_for_task(task)
        # (1) 先建 worktree；失敗則 raise（不送命令、不記 job — fail-closed）
        try:
            worktree = self._worktree_creator.create(branch, base_sha=base_sha)
        except TypeError:
            worktree = self._worktree_creator.create(branch)
        # (2) 忠實轉送呼叫者給的完整 command（本 change 不組裝 copilot 指令）
        self._pane_sender.send(pane_id, command)
        # (3) 取 dispatch 當下的 branch head（baseline）；取不到記 None。
        #     D5：baseline 持久化於 job 上（非實例 dict），故 poll_done 可跨進程比對。
        runner = git_runner or self._git_runner or _default_git_runner
        try:
            dispatch_head: str | None = runner(["rev-parse", branch])
        except Exception:
            dispatch_head = None
        # (4) registry 記一筆 job（status=dispatched，含 dispatch_head baseline）
        job = self._registry.create_job(
            task=task, persona=persona, branch=branch,
            pane=pane_id, worktree=worktree, dispatch_head=dispatch_head,
        )
        return job

    def poll_done(
        self,
        job_id: str,
        git_runner: GitRunner | None = None,
    ) -> dict[str, object]:
        """branch 出現新 commit（head 異於 dispatch_head baseline）→ 標 exited；否則維持原 status。

        baseline 從 job 記錄（`dispatch_head`）讀，故跨進程（CLI 多次獨立呼叫）仍可比對。
        baseline 為 None（dispatch 時取不到 head）時不自動完成——無 baseline 即無法判定有無新 commit。
        """
        job = self._registry.get_job(job_id)
        baseline = job.get("dispatch_head")
        if baseline is None:
            return job  # baseline 不明 → 不自動完成
        runner = git_runner or self._git_runner or _default_git_runner
        try:
            current = runner(["rev-parse", job["branch"]])
        except Exception:
            return job  # 取不到 head → 無法判定，維持原狀
        if current != baseline:
            return self._registry.update_status(job_id, "exited")
        return job

    def poll_headless_done(
        self,
        job_id: str,
        pid_waiter: PidWaiter | None = None,
        pid_alive: PidAlive | None = None,
    ) -> dict[str, object]:
        """跨進程安全的 headless 完成輪詢。

        判定順序（不依賴 os.waitpid，故 systemd oneshot / 分離 tick 進程亦正確）：
          1. exit sentinel 檔存在 → 讀 exit code、配末筆 JSONL → classify → exited/failed。
          2. 否則進程仍存活（os.kill(pid,0)）→ 維持 dispatched（仍在跑）。
          3. 否則（進程已死、卻無 sentinel，或 crash 留下無 handle pre-launch row）→ failed。

        pid_waiter（向後相容 seam）：注入時沿用舊路徑——直接由 waiter(pid) 取 exit code
        （None=仍在跑），不讀 sentinel。單元測試用以模擬已知 exit code。
        """
        job = self._registry.get_job(job_id)
        pid = job.get("pid")
        log_path = job.get("log_path") if isinstance(job.get("log_path"), str) else None
        if not isinstance(pid, int) or not log_path:
            return self._finalize_headless(job_id, exit_code=1, log_path=log_path)

        # 向後相容：注入 pid_waiter → 走舊「呼叫者直接給 exit code」路徑。
        if pid_waiter is not None:
            exit_code = pid_waiter(pid)
            if exit_code is None:
                return job
            return self._finalize_headless(job_id, exit_code, log_path)

        # 預設：跨進程 durable 機制。
        exit_code = _read_exit_sentinel(log_path)
        if exit_code is not None:
            return self._finalize_headless(job_id, exit_code, log_path)

        alive = (pid_alive or _default_pid_alive)(pid)
        if alive:
            return job  # 仍在跑 → 不動

        # 進程已死、無 sentinel → fail-closed（避免永遠卡 dispatched 遮蔽失敗）。
        return self._finalize_headless(job_id, exit_code=1, log_path=log_path)

    def _finalize_headless(
        self, job_id: str, exit_code: int, log_path: str | None
    ) -> dict[str, object]:
        last_jsonl_line = _last_nonempty_line(log_path)
        status = classify_completion(exit_code=exit_code, last_jsonl_line=last_jsonl_line)
        return self._registry.update_headless_result(
            job_id,
            status=status,
            exit_code=exit_code,
        )
