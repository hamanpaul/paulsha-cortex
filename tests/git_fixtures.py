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

import subprocess
from pathlib import Path


def make_job_clone(source: Path, target: Path, *, branch: str) -> Path:
    """#649：造出一棵**真的** per-job clone（#623 之後工作區的實際形狀）。

    來源樹與工作區是兩個獨立的 object store，工作區 checkout 在 `<branch>` 上，
    來源樹留在自己的預設 branch——那正是成果回收（`job_workspace.harvest_branch()`）
    能成立的前提：`git fetch` **拒絕**寫入一條正被 checkout 的 branch。

    測試若把工作區與來源樹寫成同一個目錄（#623 之前 `git worktree` 共用 object
    store 時的殘留寫法），回收路徑會撞上那條拒絕，而那是 fixture 的問題不是產品的。
    """

    subprocess.run(
        ["git", "clone", "-q", "--branch", branch, str(source), str(target)], check=True
    )
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(target), "config", "user.name", "Test"], check=True)
    return target


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


class StubWorktreeCreator:
    """#648：canonical lane 的 build 卡改為 **per-job** provisioning 之後，每一次
    build dispatch 都會呼叫 `WorktreeCreator`（以前只有一個 run 的第一張 build 卡
    會呼叫，後續卡沿用 `builder_jobs[-1]["worktree"]`）。

    不在意工作區本身、只在意 job 生命週期的測試，注入這個 stub 即可；不注入時
    `manager._dispatch_workflow_card` 會自行建構真的 `ScriptWorktreeCreator`，而那
    要求 `run.workspace_root` 是一棵真的 git repo。

    `calls` 逐筆記下 `(branch, job_id, base_sha)`，讓需要驗「每張卡拿到自己的
    job_id」的測試不必再自己包一層。
    """

    def __init__(self, root: Path) -> None:
        self._root = str(root)
        self.calls: list[tuple[str, str, str | None]] = []

    def create(self, branch: str, *, job_id: str, base_sha: str | None = None) -> str:
        self.calls.append((branch, job_id, base_sha))
        return self._root
