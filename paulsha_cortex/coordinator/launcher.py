from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class LaunchHandle:
    executor: str
    model_id: str | None
    session_name: str
    pid: int
    log_path: str


def build_copilot_argv(
    *,
    prompt: str,
    slice_id: str,
    log_dir: str,
    worktree: str | None = None,
    remote: str | None = None,
    allow_unsafe: bool = False,
    model: str | None = None,
) -> list[str]:
    # allow_unsafe（明確 opt-in）才放開 copilot 的全自動授權 --allow-all；
    # 預設關閉 → 由 executor 自身的互動授權把關（manager 自主派工請設 allow_unsafe=True）。
    argv = [
        "copilot",
        "-p",
        prompt,
        "--remote",
        "--name",
        slice_id,
        "--log-dir",
        log_dir,
        "--output-format",
        "json",
    ]
    if model is not None:
        argv += ["--model", model]
    if allow_unsafe:
        argv.append("--allow-all")
    return argv


def build_claude_argv(
    *,
    prompt: str,
    slice_id: str,
    log_dir: str,
    worktree: str | None = None,
    remote: str | None = None,
    allow_unsafe: bool = False,
    model: str | None = None,
) -> list[str]:
    # allow_unsafe（明確 opt-in）→ bypassPermissions（不再逐筆授權）；
    # 預設用 acceptEdits（仍受權限模式把關，最小放權）。
    argv = [
        "claude",
        "-p",
        prompt,
        "--remote-control",
        "--output-format",
        "stream-json",
        "--verbose",  # smoke 實證：claude -p + --output-format stream-json 必須帶 --verbose
        "--name",
        slice_id,
        "--permission-mode",
        "bypassPermissions" if allow_unsafe else "acceptEdits",
    ]
    if model is not None:
        argv += ["--model", model]
    if worktree is not None:
        argv.extend(["--add-dir", worktree])
    return argv


def build_codex_argv(
    *,
    prompt: str,
    slice_id: str,
    log_dir: str,
    worktree: str | None = None,
    remote: str | None = "psc",
    allow_unsafe: bool = False,
    model: str | None = None,
) -> list[str]:
    # smoke 實證：`codex exec` 不接受 `--remote`（unexpected argument）。codex 的 remote
    # 是獨立的 `remote-control` 子命令/app-server，非 exec 旗標；故 headless exec 不帶 remote。
    argv = [
        "codex",
        "exec",
        prompt,
        "--json",
    ]
    # 高風險：--dangerously-bypass-approvals-and-sandbox 同時關掉核可「與」沙箱。
    # 僅在明確 opt-in（allow_unsafe=True，例如 manager 自主全自動派工）時才加入；
    # 預設關閉，讓 codex 自身的核可/沙箱機制把關。
    if allow_unsafe:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
        # smoke 實證：headless codex exec 帶（未持久信任的）relay hook 時，會卡在 hook
        # 信任閘等待輸入 → timeout。autonomous 派工須一併 bypass hook trust 才不會掛住。
        argv.append("--dangerously-bypass-hook-trust")
    if model is not None:
        argv += ["--model", model]
    argv.extend(["-o", str(Path(log_dir) / "last.json")])
    if worktree is not None:
        argv.extend(["-C", worktree])
    return argv


def build_agy_argv(
    *,
    prompt: str,
    slice_id: str,
    log_dir: str,
    worktree: str | None = None,
    remote: str | None = None,
    allow_unsafe: bool = False,
    model: str | None = None,
) -> list[str]:
    """Build the only supported Antigravity invocation: headless plan+sandbox.

    Antigravity exposes ``--dangerously-skip-permissions`` but cortex never
    emits it.  The planner peer is evidence-only, so it has no reason to run
    with write permissions even when another executor was explicitly granted
    unsafe mode.
    """
    if allow_unsafe:
        raise ValueError("agy executor does not support unsafe mode")
    argv = ["agy", "--print", prompt, "--mode", "plan", "--sandbox"]
    if model is not None:
        argv.extend(["--model", model])
    return argv


@runtime_checkable
class AgentLauncher(Protocol):
    def launch(
        self,
        *,
        slice_id: str,
        prompt: str,
        worktree: str,
        log_dir: str,
    ) -> LaunchHandle: ...


_ARGV_BUILDERS = {
    "copilot": build_copilot_argv,
    "claude": build_claude_argv,
    "codex": build_codex_argv,
    "agy": build_agy_argv,
}


class SubprocessLauncher:
    """真實作：headless subprocess 啟動。測試 MUST 注入 fake，不實體化。"""

    def __init__(
        self,
        executor: str = "copilot",
        *,
        relay_target: str | None = None,
        codex_remote: str = "psc",
        allow_unsafe: bool = False,
        model: str | None = None,
    ) -> None:
        if executor not in _ARGV_BUILDERS:
            raise ValueError(f"unknown executor: {executor}")
        if executor == "agy" and allow_unsafe:
            raise ValueError("agy executor refuses unsafe mode")
        self._executor = executor
        self._relay_target = relay_target
        self._codex_remote = codex_remote
        # allow_unsafe（明確 opt-in）：放開各 executor 的全自動授權/沙箱旁路旗標
        # （codex --dangerously-bypass-approvals-and-sandbox、copilot --allow-all、
        # claude bypassPermissions）。預設 False，採最小放權，避免無意間關掉沙箱。
        self._allow_unsafe = allow_unsafe
        self._model = model

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        # log_dir resolve 成絕對：sentinel 由子進程的 bash wrapper 以 cwd=worktree 寫入，
        # 相對路徑會落到 worktree（poller 在他處找不到）→ 完成偵測對 worktree dispatch 失效。
        # 絕對化後 JSONL / sentinel / 回傳 log_path 皆與 cwd 無關，跨進程 poll 一致。
        log_dir = str(Path(log_dir).resolve())
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        inner_argv = _ARGV_BUILDERS[self._executor](
            prompt=prompt,
            slice_id=slice_id,
            log_dir=log_dir,
            worktree=worktree,
            remote=self._codex_remote,
            allow_unsafe=self._allow_unsafe,
            model=self._model,
        )
        # PSC_REPO_ROOT 讓已安裝 hook 的 `${PSC_REPO_ROOT}/scripts/coordinator/psc-relay-hook.sh`
        # 在 cwd=worktree（≠repo）時仍可解（worktree 雖是 repo checkout，但 hook 為全域安裝、
        # 不可依賴相對 cwd；互動 session 亦不應因相對路徑找不到 script 而報錯）。
        env = {
            **os.environ,
            "PSC_SLICE_ID": slice_id,
            "PSC_REPO_ROOT": str(Path(__file__).resolve().parents[2]),
        }
        if self._relay_target is not None:
            env["PSC_RELAY_TARGET"] = self._relay_target
        log_path = str(Path(log_dir) / f"{slice_id}.jsonl")
        # 跨進程 durable 完成判定：以 bash -lc 包裝，子進程結束時把 $? 寫入 exit sentinel。
        # 用 shlex.join 安全嵌入內層 argv（prompt 含換行/空白仍為單一 token），
        # sentinel 路徑亦 shlex.quote。poll_headless_done 讀此 sentinel，不再靠 os.waitpid。
        sentinel = str(Path(log_dir) / f"{slice_id}.exit")
        # 重跑同一 slice_id 前先清掉上一輪殘留：移除舊 exit sentinel、log 以 wb 截斷。
        # 否則 poll_headless_done 會讀到上一輪的 sentinel / 末筆 JSONL，
        # 誤判「還沒開始就已完成」（fail-closed：每輪從乾淨狀態起跑）。
        Path(sentinel).unlink(missing_ok=True)
        script = f'{shlex.join(inner_argv)}; printf %s "$?" > {shlex.quote(sentinel)}'
        argv = ["bash", "-lc", script]
        with open(log_path, "wb") as logf:
            proc = subprocess.Popen(
                argv,
                cwd=worktree,
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        return LaunchHandle(
            executor=self._executor,
            model_id=self._model,
            session_name=slice_id,
            pid=proc.pid,
            log_path=log_path,
        )
