"""測試用的假 git repo fixture（#565）。

`_infer_repo_root`（`coordinator/autonomy.py`）自 #565 起不再把「`.git` 存在」
當作 repo 根，而要求 `.git` 是**有效** repo 標記：目錄含 `HEAD`，或是內含
`gitdir:` 的檔案（linked worktree）。理由見該函式 docstring——agent sandbox
基礎設施會在 `/tmp` 暫態 `mkdir` 一個空 `.git`，舊判準會把 repo 根全域劫持
到 `/tmp`。

測試若只 `(<root> / ".git").mkdir()`，造出的正是那種被判為「非 repo」的空目錄。
需要一個「看起來像 repo 根」的目錄時一律用 `make_fake_repo()`：它產生的形狀與
真 `git init` 的最小交集一致，`.git` 目錄本身也不需要真的跑 git。
"""

from __future__ import annotations

from pathlib import Path


def make_fake_repo(root: Path, *, branch: str = "main") -> Path:
    """在 `root` 建出被 `_infer_repo_root` 認可的最小 repo 標記，回傳 `root`。"""
    git_dir = root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    return root


def make_empty_git_dir(root: Path) -> Path:
    """建出 #565 實測到的**污染形狀**：`mkdir` 出來、沒有 `HEAD` 的空 `.git`。

    回傳該 `.git` 路徑。專供「路徑鏈上有空 `.git` 時不得被當 repo 根」的回歸
    測試使用——它讓測試自備污染，不必依賴 host `/tmp` 當下是否真的被污染。
    """
    git_dir = root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    return git_dir
