from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from paulsha_cortex.config import paths


@runtime_checkable
class PaneSender(Protocol):
    """把一行命令送進 tmux pane 的 seam。"""

    def send(self, pane_id: str, text: str) -> None: ...


@runtime_checkable
class WorktreeCreator(Protocol):
    """為某分支建立 git worktree、回傳其路徑的 seam。"""

    def create(self, branch: str, *, base_sha: str | None = None) -> str: ...


class TmuxPaneSender:
    """真實作：鏡射 daemon._send_to_pane。

    `tmux send-keys -t <pane> -l <text>`（literal，避免 shell 二次解讀）
    後 `tmux send-keys -t <pane> Enter`。失敗 → raise ValueError。
    單元測試 MUST 注入 fake，不實體化此類。
    """

    def send(self, pane_id: str, text: str) -> None:
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, "-l", text],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, "Enter"],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"tmux send-keys failed: {exc.stderr.decode().strip()}") from exc
        except FileNotFoundError as exc:
            raise ValueError("tmux not found") from exc


class ScriptWorktreeCreator:
    """真實作：鏡射 scripts/using-git-worktrees.sh 的新分支路徑。

    `git -C <repo> worktree add -b <branch> <wt_root>/<slug> <base>`，
    回傳 target 路徑。單元測試 MUST 注入 fake，不實體化此類。
    """

    def __init__(
        self,
        repo: str | Path | None = None,
        wt_root: str | Path | None = None,
        base: str = "main",
    ) -> None:
        self._repo = Path(paths.repo_root() if repo is None else repo)
        self._wt_root = Path(paths.worktree_root() if wt_root is None else wt_root)
        self._base = base

    def create(self, branch: str, *, base_sha: str | None = None) -> str:
        slug = branch.replace("/", "-")
        target = self._wt_root / slug
        self._wt_root.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["git", "-C", str(self._repo), "worktree", "add", "-b", branch,
                 str(target), base_sha or self._base],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"git worktree add failed: {exc.stderr.decode().strip()}") from exc
        except FileNotFoundError as exc:
            raise ValueError("git not found") from exc
        return str(target)
