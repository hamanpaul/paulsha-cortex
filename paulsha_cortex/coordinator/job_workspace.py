"""#623：per-job **完整 clone** 的工作區模型（取代 `git worktree`）。

## 為什麼不是 `git worktree`

trust-root Phase 2b M1（#584）之後 builder job 以 `cortex-builder` 執行、Manager 以
`cortex-manager`，durable state 樹是 `0700 cortex-manager`。在這個模型下 `git worktree`
**結構性不成立**（#623 實測）：

1. linked worktree 的 `.git` 是一個指標檔，指向 `<來源 repo>/.git/worktrees/<name>`
   ——那在 Manager-owned 的樹裡。只把 worktree 目錄 chown 給 builder，`git status`
   直接 `fatal: not a git repository`。
2. 連 `.git/worktrees/<name>/` 一起 chown、父鏈補 `--x` 之後 `git status` 過了，但
   **`git add` 仍失敗**——寫 object 需要寫**共用 object store**。

推論：*只要 builder 要能 commit，它就必須能寫 object store；而能寫 object store，
「builder 不可竄改 Manager state」這條邊界就在 git 這一層漏掉。*共用 object store 與
三分隔離互斥。

per-job 完整 clone 沒有這個問題：clone 有**自己的** object store，整個目錄由該 job
帳號擁有，來源 repo 對它唯讀（實測 0.5 秒／35MB per job）。

## 本模組的職責

工作區「是什麼」的單一真相——標記、識別、列舉、刪除，以及**成果回收**
（Manager 從 job 的 clone `fetch` 回自己的樹）。三個呼叫端共用：

- `coordinator/seams.py`：provision（建 clone）
- `coordinator/gc.py`：`cortex work gc` 的掃描與回收
- `coordinator/worktree_reclaim.py`：#478／#544 的原子回收 helper

## 方向性（D2「git 讀」）

成果回收一律是 **Manager 拉**（`git -C <來源 repo> fetch <clone>`），**不是 builder
推**。builder 永遠不 push 進 Manager 的樹；clone 完成後指向來源 repo 的暫時 remote
會被移除（見 :data:`SOURCE_REMOTE`），工作區裡不留任何回寫路徑。

fetch 的 refspec 刻意**不帶 `+`**：非 fast-forward 一律被 git 拒絕，Manager 不會靜默
吸收被改寫過的歷史。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: clone 工作區的識別標記檔名，寫在 clone 自己的 `.git/` 底下。
#:
#: 放在 `.git/` 而非工作樹根，是因為工作樹根的任何檔案都會出現在
#: `git status --porcelain --untracked-files=all` 裡——那會讓每一個新工作區一出生就
#: 是 dirty，而 dirty 是 `verification` 與 `gc` 的 fail-closed 條件。
MARKER_NAME = "cortex-job-workspace.json"

MARKER_SCHEMA_VERSION = 1

#: 標記檔的 `model` 欄位值。將來若再換工作區模型，這個字面量就是分辨依據。
WORKSPACE_MODEL = "per-job-clone"

#: `git clone` 期間指向來源 repo 的 remote 名。clone 完成後**必定移除**——留著它
#: 等於在 builder 的工作區裡放一條可 push 回 Manager 樹的路徑（`git push
#: cortex-source`），與 D2 的單向性直接衝突。工作區最終看到的 `origin` 是**真正的
#: 上游**（來源 repo 的 `origin` URL），與 worktree 模型下逐字相同。
SOURCE_REMOTE = "cortex-source"

#: 回收 clone 前，把工作區 HEAD 封存到來源 repo 的這個 ref 命名空間。
#:
#: worktree 模型下「回收工作區」不會銷毀 commit——object 在共用 store 裡、branch 還在
#: 主 repo。clone 模型下 `rmtree` 會把**尚未回收的 commit 一併刪掉**，那與
#: `worktree_reclaim` 模組契約的「不銷毀證據」相牴觸。因此回收前先把 HEAD 拉進這個
#: 命名空間（不是 branch、不是 tag：不佔用 branch 名，`git push` 預設也不會帶出去）。
ARCHIVE_REF_PREFIX = "refs/cortex/reclaimed"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class WorkspaceError(ValueError):
    """工作區 provision／回收的可操作錯誤。"""


# ---------------------------------------------------------------------------
# git 執行（本模組刻意直接用 subprocess）
# ---------------------------------------------------------------------------

def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    """執行 `git <args>`；不 check，由呼叫端判讀。

    不引入 runner seam：本模組的行為**就是** git 的行為，注入假 runner 的測試只會
    驗到自己寫的 stub。相關測試一律開真 git repo（與 `tests/test_coordinator_seams.py`
    既有作法一致）。
    """

    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:  # pragma: no cover - 環境無 git
        raise WorkspaceError("git not found") from exc


def _git_ok(args: list[str], *, failure: str) -> str:
    proc = _git(args)
    if proc.returncode != 0:
        raise WorkspaceError(f"{failure}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# 標記與識別
# ---------------------------------------------------------------------------

def marker_path(workspace: str | Path) -> Path:
    return Path(workspace) / ".git" / MARKER_NAME


def is_job_clone(workspace: str | Path) -> bool:
    """這個路徑是不是 cortex provision 出來的 per-job clone。

    判準是標記檔存在，**不是**「`.git` 是目錄」——後者對任何主 checkout 都成立，
    包含 run 的 `workspace_root`（來源 repo 本身）。遞迴刪除的爆炸半徑不允許用
    這種寬鬆判準（見 `worktree_reclaim` 的安全閘與 #478 現場）。
    """

    marker = marker_path(workspace)
    return marker.is_file() and not marker.is_symlink()


def is_linked_worktree(workspace: str | Path) -> bool:
    """linked worktree 的根目錄帶的是 `.git` **檔案**（內容 `gitdir: ...`）。

    clone 模型上線後新工作區不會再是這個形狀，但升級前既存的 worktree 仍須能被
    回收——`gc` 與 `worktree_reclaim` 因此同時認得兩種形狀。
    """

    marker = Path(workspace) / ".git"
    return marker.is_file() and not marker.is_symlink()


def read_marker(workspace: str | Path) -> dict[str, Any] | None:
    path = marker_path(workspace)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_marker(
    workspace: str | Path,
    *,
    branch: str,
    base: str,
    source_repo: str | Path,
) -> Path:
    path = marker_path(workspace)
    payload = {
        "schema_version": MARKER_SCHEMA_VERSION,
        "model": WORKSPACE_MODEL,
        "branch": branch,
        "base": base,
        "source_repo": str(source_repo),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def list_clone_workspaces(pool_root: str | Path) -> list[Path]:
    """列出 pool 底下的 per-job clone（只掃**直接子項**，不遞迴）。

    不遞迴是刻意的：pool 的契約是 `<PSC_WORKTREE_ROOT>/<工作區名>`，遞迴只會把
    clone 內部的巢狀 repo（例如模型自己 clone 的第三方 repo）也掃進回收清單。
    """

    root = Path(pool_root)
    if not root.is_dir():
        return []
    found: list[Path] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        if is_job_clone(entry):
            found.append(entry)
    return found


def workspace_branch(workspace: str | Path) -> str | None:
    """工作區目前 checked-out 的 branch；detached 或不可讀時回 None。"""

    proc = _git(["-C", str(workspace), "symbolic-ref", "--short", "HEAD"])
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


# ---------------------------------------------------------------------------
# 成果回收（Manager 拉，builder 不推）
# ---------------------------------------------------------------------------

def harvest_branch(
    *,
    source_repo: str | Path,
    workspace: str | Path,
    branch: str,
) -> str:
    """把 job clone 的 `branch` fetch 回來源 repo，回傳回收後的 branch head。

    這是 clone 模型下**成果離開 job 帳號的唯一路徑**：Manager 以自己的身分對
    clone 執行 `git fetch`（單向讀），builder 永遠不 push 進 Manager 的樹。

    refspec 刻意不帶 `+`——非 fast-forward 由 git 拒絕，Manager 不會靜默吸收被
    改寫過的歷史（等價於 worktree 模型下 `branch -f` 前的 ancestry 守衛）。
    """

    source = Path(source_repo)
    target = Path(workspace)
    if not branch:
        raise WorkspaceError("harvest requires a branch name")
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    proc = _git(["-C", str(source), "fetch", "--no-tags", str(target), refspec])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        if "non-fast-forward" in detail or "rejected" in detail:
            raise WorkspaceError(
                f"job workspace branch is not a fast-forward of {branch}: {detail}"
            )
        raise WorkspaceError(f"job workspace harvest failed: {detail}")
    head = _git_ok(
        ["-C", str(source), "rev-parse", f"refs/heads/{branch}"],
        failure="job workspace harvest head unreadable",
    )
    if _SHA_RE.fullmatch(head) is None:
        raise WorkspaceError(f"job workspace harvest head invalid: {head}")
    return head


def harvest_if_job_clone(
    *,
    source_repo: str | Path,
    workspace: str | Path,
    branch: str,
) -> str | None:
    """工作區是 per-job clone 時做成果回收；否則回 None。

    worktree 模型下 branch 與 object 本來就在來源 repo 裡，沒有東西要 fetch；
    升級前既存的 worktree、以及測試用的假路徑因此**完全不受影響**——這是本次
    變更「既有部署零回歸」的掛點。
    """

    if not is_job_clone(workspace):
        return None
    return harvest_branch(source_repo=source_repo, workspace=workspace, branch=branch)


def archive_workspace_head(
    *,
    source_repo: str | Path,
    workspace: str | Path,
) -> str | None:
    """回收前把工作區 HEAD 封存進來源 repo 的 :data:`ARCHIVE_REF_PREFIX` 命名空間。

    回傳封存後的 ref 名；工作區沒有可讀 HEAD、或 commit 已在來源 repo 裡（沒有東西
    會被銷毀）時回 None。任何失敗都回 None——封存是**加分項**，不得讓回收本身失敗
    （回收失敗才是 #478／#601 的生產事故）。
    """

    source = Path(source_repo)
    target = Path(workspace)
    head_proc = _git(["-C", str(target), "rev-parse", "HEAD"])
    if head_proc.returncode != 0:
        return None
    head = head_proc.stdout.strip().lower()
    if _SHA_RE.fullmatch(head) is None:
        return None
    if _git(["-C", str(source), "cat-file", "-e", f"{head}^{{commit}}"]).returncode == 0:
        # commit 已在 Manager 的 object store 裡（多半剛做過成果回收），
        # 刪掉工作區不會銷毀任何東西。
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ref = f"{ARCHIVE_REF_PREFIX}/{target.name}/{stamp}-{head[:12]}"
    fetched = _git(["-C", str(source), "fetch", "--no-tags", str(target), f"{head}:{ref}"])
    if fetched.returncode != 0:
        return None
    return ref


# ---------------------------------------------------------------------------
# 刪除
# ---------------------------------------------------------------------------

def remove_clone(workspace: str | Path) -> None:
    """刪除 per-job clone 目錄，並驗證後置條件。

    clone 沒有 `git worktree` registry，回收因此退化成單純的目錄刪除——但後置條件
    仍必須被**驗證**（#478 的教訓：清理失敗被吞掉，下一個 tick 才炸）。
    """

    target = Path(workspace)
    if target.is_symlink():
        target.unlink()
        return
    if not target.exists():
        return
    shutil.rmtree(target, ignore_errors=False)
    if target.exists() or target.is_symlink():  # pragma: no cover - rmtree 失敗會先拋
        raise WorkspaceError(f"job workspace removal incomplete: {target}")


__all__ = [
    "ARCHIVE_REF_PREFIX",
    "MARKER_NAME",
    "MARKER_SCHEMA_VERSION",
    "SOURCE_REMOTE",
    "WORKSPACE_MODEL",
    "WorkspaceError",
    "archive_workspace_head",
    "harvest_branch",
    "harvest_if_job_clone",
    "is_job_clone",
    "is_linked_worktree",
    "list_clone_workspaces",
    "marker_path",
    "read_marker",
    "remove_clone",
    "workspace_branch",
    "write_marker",
]
