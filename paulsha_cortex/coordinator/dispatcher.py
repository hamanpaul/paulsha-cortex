from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Callable

from paulsha_cortex.config import paths

from . import terminal_contract
from .completion import classify_completion
from .provider_outcome import classify_provider_failure, read_log_tail
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
    repo_root = paths.repo_root()
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git -C {repo_root} {' '.join(args)} 失敗: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _branch_for_task(task: str) -> str:
    return f"feature/{task}"


def exit_sentinel_path(log_path: str) -> Path:
    """由 canonical job log 推導 Manager-only exit sentinel 路徑。

    SubprocessLauncher 在子進程結束時把 `$?` 寫入此檔；poll_headless_done 跨進程讀回，
    故完成判定不再依賴 os.waitpid（只有 spawn 子進程的進程能 reap）。確定性、零 I/O。

    #604：降權模式下寫者已改為 Manager 側的 exit 記帳 shell（見
    `job_runner.build_manager_exit_recorder_argv`）。#708 repair 之後 job log
    本身位於 per-principal writable spool；先投影回 Manager-only control-log
    anchor，避免把 sentinel 放進 job 可 rename 的目錄。
    """
    from .job_workspace import manager_control_log_path

    return manager_control_log_path(log_path).with_suffix(".exit")


def _read_exit_sentinel(log_path: str | None) -> int | None:
    """讀 exit sentinel；不存在／壞檔／**非 Manager 產生** → None。

    #604：sentinel 是 `poll_headless_done` 的第一判準，等於「這個 job 的終局是
    什麼」的權威來源。OS 隔離上線後 builder 是另一個 uid，一份由 job 帳號擁有的
    sentinel 就是「被隔離的一方自報 exit code」——不得採信。

    採「視同尚未寫下」而不是 raise：呼叫端 `poll_headless_done` 對「沒有 sentinel
    且行程已死」本來就有 fail-closed 分支（記為 `exit_code=1` → failed），沿用它
    比新增一條例外路徑安全，也不會讓一個被動過手腳的檔案把整個 tick 打斷。

    同時要求它是**普通檔**（`lstat` 不跟隨 symlink）：symlink 換掉的 sentinel 即使
    擁有者看起來對，指向的內容也不是 Manager 寫的。
    """
    if not log_path:
        return None
    p = exit_sentinel_path(log_path)
    try:
        stat_result = os.lstat(p)
    except OSError:
        return None
    if not stat.S_ISREG(stat_result.st_mode):
        return None
    if terminal_contract.foreign_evidence_author(p) is not None:
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
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
        #     #645：工作區目錄名由 job id 導出（本 lane 的 job id ＝ task），不再是
        #     branch slug；`job_id` 為必填，因此 TypeError 退路也必須帶著它。
        try:
            worktree = self._worktree_creator.create(branch, job_id=task, base_sha=base_sha)
        except TypeError:
            worktree = self._worktree_creator.create(branch, job_id=task)
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
        control_log_path = (
            job.get("control_log_path")
            if isinstance(job.get("control_log_path"), str)
            else log_path
        )
        if not isinstance(pid, int) or not log_path:
            return self._finalize_headless(job_id, exit_code=1, log_path=log_path)

        # 向後相容：注入 pid_waiter → 走舊「呼叫者直接給 exit code」路徑。
        if pid_waiter is not None:
            exit_code = pid_waiter(pid)
            if exit_code is None:
                return job
            return self._finalize_headless(job_id, exit_code, log_path)

        # 預設：跨進程 durable 機制。
        exit_code = _read_exit_sentinel(control_log_path)
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
        job = self._registry.get_job(job_id)
        # A downgraded Codex job publishes auth.json with a readable ACL/mode
        # before its Manager-authored exit sentinel.  The launcher records a
        # typed runtime surface; never infer a principal from workflow kind.
        # Missing runtime metadata/slot/authority is a durable runtime failure,
        # not a provider failure and never a silently skipped harvest.
        from . import job_runner, job_workspace, spool_slot
        runtime_diagnostic: dict[str, str] | None = None
        runtime_mode = job.get("runtime_mode")
        runtime_principal = job.get("runtime_principal")
        runtime_instance: str | None = None
        if runtime_mode == "systemd-template":
            runtime_instance = job_workspace.spool_key_for_job(job)
        elif runtime_mode == "systemd-run":
            runtime_instance = job_runner.template_instance_id(job_id)
        prompt_path = job.get("prompt_path")
        if prompt_path is not None:
            prompt = Path(prompt_path) if isinstance(prompt_path, str) else Path(".")
            expected_prompt: Path | None = None
            expected_prompt_dir: Path | None = None
            try:
                prompt_roles = {
                    config.log_spool_principal: role
                    for role, config in job_runner.JOB_ROLE_CONFIG.items()
                }
                prompt_role = prompt_roles.get(str(runtime_principal))
                if runtime_mode in {"systemd-run", "systemd-template"} and prompt_role in {
                    job_runner.JOB_ROLE_BUILDER,
                    job_runner.JOB_ROLE_REVIEW,
                }:
                    spec_spool = job_runner.resolve_prompt_spec_spool(
                        os.environ, role=prompt_role
                    )
                    expected_prompt_dir = Path(
                        job_runner.job_prompt_spool_path(
                            spec_spool,
                            principal=str(runtime_principal),
                            instance=runtime_instance,
                        )
                    )
                    expected_prompt = expected_prompt_dir / (
                        ".prompt-" + runtime_instance
                    )
            except (TypeError, ValueError):
                expected_prompt = None
                expected_prompt_dir = None
            if (
                not isinstance(prompt_path, str)
                or not prompt.is_absolute()
                or not prompt.name.startswith(".prompt-")
                or expected_prompt is None
                or prompt != expected_prompt
                or prompt.is_symlink()
            ):
                runtime_diagnostic = {
                    "reason": "runtime-prompt-cleanup-invalid",
                    "detail": (
                        f"private prompt path is malformed or outside its typed slot: "
                        f"{prompt_path!r}"
                    ),
                    "source": "dispatcher._finalize_headless",
                    "job_id": str(job_id),
                }
            else:
                try:
                    if expected_prompt_dir is None or not expected_prompt_dir.is_dir():
                        raise RuntimeError(
                            "private prompt parent is missing or not a directory"
                        )
                    if expected_prompt_dir.is_symlink():
                        raise RuntimeError("private prompt parent is a symlink")
                    if prompt.exists():
                        prompt.unlink()
                    leftovers: list[str] = []
                    for entry in expected_prompt_dir.iterdir():
                        try:
                            info = entry.lstat()
                        except OSError:
                            leftovers.append(str(entry))
                            continue
                        leftovers.append(str(entry))
                        # The parent is Manager-owned and non-writable by the
                        # job.  Remove only leaf/symlink leftovers; retain an
                        # unexpected directory or special node for an explicit
                        # durable diagnostic rather than following it.
                        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                            entry.unlink()
                    try:
                        expected_prompt_dir.rmdir()
                    except OSError as exc:
                        if not leftovers:
                            raise RuntimeError(
                                "private prompt parent could not be removed: "
                                f"{type(exc).__name__}: {exc}"
                            ) from exc
                    if leftovers:
                        raise RuntimeError(
                            "private prompt directory leaked entries: "
                            + ", ".join(leftovers)
                        )
                except OSError as exc:
                    runtime_diagnostic = runtime_diagnostic or {
                        "reason": "runtime-prompt-cleanup-failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "source": "dispatcher._finalize_headless",
                        "job_id": str(job_id),
                    }
                except RuntimeError as exc:
                    runtime_diagnostic = runtime_diagnostic or {
                        "reason": "runtime-prompt-leaked",
                        "detail": str(exc),
                        "source": "dispatcher._finalize_headless",
                        "job_id": str(job_id),
                    }
        isolated_codex = (
            job.get("executor") == "codex"
            and runtime_mode in {"systemd-run", "systemd-template"}
        )
        if isolated_codex and not job.get("credential_publish"):
            runtime_diagnostic = runtime_diagnostic or {
                "reason": "runtime-publisher-missing",
                "detail": "isolated Codex job has no credential publisher metadata",
                "source": "dispatcher._finalize_headless",
                "job_id": str(job_id),
            }
        elif job.get("credential_publish") and not isolated_codex:
            runtime_diagnostic = runtime_diagnostic or {
                "reason": "runtime-lane-invalid",
                "detail": "credential publisher is set outside an isolated Codex lane",
                "source": "dispatcher._finalize_headless",
                "job_id": str(job_id),
            }
        if job.get("credential_publish") and isolated_codex:
            principal = job.get("runtime_principal")
            surface_id = job.get("runtime_surface")
            if not isinstance(principal, str) or not isinstance(surface_id, str):
                runtime_diagnostic = runtime_diagnostic or {
                    "reason": "runtime-identity-missing",
                    "detail": (
                        f"credential publish metadata is inconsistent: "
                        f"principal={principal!r}, surface={surface_id!r}"
                    ),
                    "source": "dispatcher._finalize_headless",
                    "job_id": str(job_id),
                }
            else:
                try:
                    surface = spool_slot.codex_runtime_surface(
                        principal=principal, surface_id=surface_id
                    )
                    authority = spool_slot.credential_authority(principal)
                    if (
                        authority.is_symlink()
                        or authority.parent.is_symlink()
                        or not authority.is_file()
                    ):
                        raise spool_slot.SpoolSlotError(
                            "authority", f"credential authority is unavailable: {authority}"
                        )
                    if runtime_mode == "systemd-template":
                        if runtime_instance is None:
                            raise spool_slot.SpoolSlotError(
                                "authority",
                                "persisted template instance is missing or malformed",
                            )
                        spool_slot.commit_runtime_credential_for_instance(
                            principal=principal,
                            instance=runtime_instance,
                            surface_id=surface.surface_id,
                        )
                    else:
                        spool_slot.commit_runtime_credential(
                            principal=principal, job_id=job_id
                        )
                except Exception as exc:
                    runtime_diagnostic = runtime_diagnostic or {
                        "reason": "runtime-credential-harvest-failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "source": "dispatcher._finalize_headless",
                        "job_id": str(job_id),
                    }
        if runtime_diagnostic is not None:
            exit_code = 1
        last_jsonl_line = _last_nonempty_line(log_path)
        status = classify_completion(exit_code=exit_code, last_jsonl_line=last_jsonl_line)
        # #384：只在真的失敗時才分類——分類器本身也會拒絕 exit_code == 0
        # （防禦性），這裡額外用 status 把「exited 但非零 exit code 目前尚未走
        # classify_completion 的 failed 分支」這種邊界情況也排除掉，避免對
        # 明明成功的 job 做無意義的 log 讀取與分類。
        provider_outcome = None
        if status == "failed" and runtime_diagnostic is None:
            output = read_log_tail(log_path)
            provider_outcome = classify_provider_failure(exit_code=exit_code, output=output).to_dict()
        result_kwargs = {
            "status": status,
            "exit_code": exit_code,
            "provider_outcome": provider_outcome,
        }
        # Keep the legacy registry seam usable for pre-migration callers while
        # passing the durable runtime field whenever a real failure exists.
        if runtime_diagnostic is not None:
            result_kwargs["runtime_diagnostic"] = runtime_diagnostic
        return self._registry.update_headless_result(job_id, **result_kwargs)
