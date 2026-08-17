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

成果回收一律是 **Manager 拉**，**不是 builder 推**。builder 永遠不 push 進 Manager
的樹；clone 完成後指向來源 repo 的暫時 remote 會被移除（見 :data:`SOURCE_REMOTE`），
工作區裡不留任何回寫路徑。

fetch 的 refspec 刻意**不帶 `+`**：非 fast-forward 一律被 git 拒絕，Manager 不會靜默
吸收被改寫過的歷史。

## 成果回收為什麼是 bundle ＋ append-only spool，而不是「對 clone fetch」

`git -C <來源樹> fetch <builder 的 clone>` 在 Phase 2b 三分下**結構性不成立**，
兩個獨立原因（operator 0817 實機驗證）：

1. **Manager 走不進 builder 的樹**——clone 是 builder-owned `0700`，
   `git -C <clone> …` 直接 `fatal: cannot change to '…': Permission denied`。
2. **per-job 路徑無法用一條設定涵蓋**——就算補了 traverse，Manager 對每個 job 的
   clone 還需要跨擁有者的 `safe.directory`，而實測 git 2.43 **不吃路徑 glob**，
   只認逐字相等或字面 `*`。

改成 builder 在自己的 clone 產出 bundle → 寫進 Manager-owned 的 append-only spool
（:func:`commit_spool_root`）→ Manager 從**那個檔案** fetch：

```
# builder 身分（wrapper script，見 build_bundle_command）
git -C <clone> bundle create <spool>/<key>/commits.bundle <branch> ^refs/cortex/base

# Manager 身分
git -C <來源樹> fetch --no-tags <spool>/<key>/commits.bundle <branch>:<branch>
```

關鍵在 Manager 讀的是一個**普通檔**而不是一個 repo——dubious-ownership 與 traverse
兩個問題同時消失，且 Manager **全程不需要、也不應該**存取 builder 的樹。

## bundle 不是證據，是搬運

#628 已把 gate ledger 與 exit sentinel 的作者收斂到 Manager，理由是「被驗方不得在
自己的進程裡產生自己的驗收證據」。bundle **不適用**那條：它不宣告任何結論，只把
commit 從一個 object store 搬到另一個。採信與否仍全部由 Manager 判斷——canonical
lane 在 `_verify_build_candidate_transition` 之後才回收，且回收後來源樹的 branch
**必須恰等於已採信的 candidate**，對不上即 fail-closed（沿用 #540 的 acceptance
chain：model 既不能自證成功、也不能自證失敗）。bundle 內容由 builder 掌控這件事
因此不新增任何採信面：它能做到的最壞情況就是讓回收失敗。
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from paulsha_cortex.config import paths

from . import spool_slot

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

#: provision 當下寫進 clone 的 base pin，bundle 以 `^<此 ref>` 收斂範圍。
#:
#: 為什麼要一個 ref、而不是把 base 寫死在 wrapper 裡：**產 bundle 的是 builder**，
#: 它讀得到自己的 clone，卻讀不到 spool（per-account ACL 是 `wx` 無 `r`），也讀不到
#: Manager 的任何狀態。base 因此必須落在 clone 內部。它與標記檔的 `base` 欄同源
#: （都取自 `seams` 解出的 `exact_base`），而 `exact_base` 是**來源 repo 自己**
#: `rev-parse --verify` 出來的 commit——所以「來源樹一定有 bundle 的 prerequisite」
#: 這條性質在**每一條 lane** 都由 provisioning 單一推導點保證。
#:
#: builder 動得了這個 ref（它是自己 clone 裡的一筆），但動了的後果只有一種：bundle
#: 產不出來、或 prerequisite 對不上而 fetch 失敗——一律 fail-closed，見
#: :func:`harvest_branch` 的錯誤分類。
BASE_REF = "refs/cortex/base"

#: per-job spool 裡那一份 bundle 的檔名（權威定義在 `spool_slot`，與
#: `review-verdict-spool` 的成果檔名並列在同一處）。
COMMIT_BUNDLE_FILENAME = spool_slot.COMMIT_BUNDLE_FILENAME

#: builder 產 bundle 時的暫存名。先寫 `<name>.part`、`chmod` 後 `mv` 成正式名，
#: 讓 spool 裡「存在」的那個檔恆為完整檔——中途被 kill 只會留下 `.part`。
COMMIT_BUNDLE_PART_SUFFIX = ".part"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: spool 的 per-job 目錄名（與 `coordinator/review.py` 的 `SAFE_SPOOL_KEY_RE` 同形）。
#: 這個字串會成為 Manager-owned 樹裡的一個目錄名，形狀守衛不得放寬。
_SPOOL_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


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

    return _is_regular_file(marker_path(workspace))


def _is_regular_file(path: Path) -> bool:
    """`path` 是不是一個**可 stat 的普通檔**；不可讀（含整棵樹不可進入）時回 False。

    三分部署下 Manager 對 builder-owned `0700` 的 clone 連 `stat` 都會拿到
    `PermissionError`。那個例外必須在這裡收斂成 False，而不是往上炸——所有呼叫端
    （`gc` 的掃描、`worktree_reclaim` 的安全閘）在「認不出這是什麼」時的正確行為
    都是**不動它**，而不是讓一個 tick 整個掛掉。
    """

    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def is_linked_worktree(workspace: str | Path) -> bool:
    """linked worktree 的根目錄帶的是 `.git` **檔案**（內容 `gitdir: ...`）。

    clone 模型上線後新工作區不會再是這個形狀，但升級前既存的 worktree 仍須能被
    回收——`gc` 與 `worktree_reclaim` 因此同時認得兩種形狀。
    """

    return _is_regular_file(Path(workspace) / ".git")


def read_marker(workspace: str | Path) -> dict[str, Any] | None:
    path = marker_path(workspace)
    if not _is_regular_file(path):
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
# 成果 spool（Manager-owned、append-only、per-job 一格）
# ---------------------------------------------------------------------------

def commit_spool_root(coordinator_root: str | Path | None = None) -> Path:
    """成果 bundle spool 的根：`<coordinator_root>/commit-spool/`。

    路徑契約的權威是 `config/paths.py:commit_spool_root()`（登記表資產
    `commit-spool`，#636）。本函式只多一件事：接受**顯式**的 coordinator root。
    回收與 dispatch 兩端都可能拿到呼叫端傳下來的 root（`manager` 與
    `verification` 都有這個參數），不能一律回頭讀 env——那會讓同一個 job 的
    dispatch 與 harvest 指到兩個不同的樹。未指定時逐字委派給 `paths`。
    """

    if coordinator_root is None:
        return paths.commit_spool_root()
    return Path(coordinator_root) / paths.COMMIT_SPOOL_DIRNAME


def commit_spool_dir(
    *,
    spool_key: str,
    coordinator_root: str | Path | None = None,
) -> Path:
    """單一 job 的 spool 目錄（唯一定址點）。"""

    if not isinstance(spool_key, str) or _SPOOL_KEY_RE.fullmatch(spool_key) is None:
        raise WorkspaceError(f"unsafe commit spool key: {spool_key!r}")
    return commit_spool_root(coordinator_root).resolve() / spool_key


def commit_bundle_path(
    *,
    spool_key: str,
    coordinator_root: str | Path | None = None,
) -> Path:
    """該 job 的 bundle 絕對路徑（builder 寫、Manager 讀的那一個檔）。"""

    return (
        commit_spool_dir(spool_key=spool_key, coordinator_root=coordinator_root)
        / COMMIT_BUNDLE_FILENAME
    )


def spool_key_for_job(job: Mapping[str, object]) -> str | None:
    """從 job 記錄推導出這個 job 在 dispatch 當下用的 spool key。

    **推導規則只有一條**：`Path(job["log_path"]).stem`。理由是那正是
    `launcher.launch()` 收到的 `slice_id`——它同時決定了 exit sentinel
    （`<log_dir>/<slice_id>.exit`）與 gate ledger（`terminal_contract.
    gate_ledger_path(log_path)`）的落點，本模組沿用同一條規則，spool 就不會與
    那兩者漂移。

    這件事必須是**單一規則**：canonical lane 的 launch key 是 job_id，slice lane
    的是 slice_id，兩條 lane 若各自在回收端「猜」自己的 key，任何一邊改名都會退化成
    「spool 找不到 → 靜默不回收」——那是最壞的失敗形態。改讀 `log_path` 之後兩條
    lane 共用同一個推導，且該欄位由 `registry.attach_launch_handle` 在 launch 當下
    寫入，與 spool 的建立點同源。

    job 還沒 launch（沒有 `log_path`）時回 None——沒有 spool，也沒有東西可回收。
    """

    log_path = job.get("log_path")
    if not isinstance(log_path, str) or not log_path.strip():
        return None
    stem = Path(log_path).stem
    if _SPOOL_KEY_RE.fullmatch(stem) is None:
        return None
    return stem


def commit_bundle_path_for_job(
    job: Mapping[str, object],
    *,
    coordinator_root: str | Path | None = None,
) -> Path | None:
    """該 job 的 bundle 路徑；推導不出 spool key 時回 None。"""

    key = spool_key_for_job(job)
    if key is None:
        return None
    return commit_bundle_path(spool_key=key, coordinator_root=coordinator_root)


def prepare_commit_spool(
    *,
    spool_key: str,
    coordinator_root: str | Path | None = None,
) -> Path:
    """dispatch 當下建立 per-job 那一格，回傳 bundle 應該落地的路徑。

    生命週期本身走 :mod:`spool_slot`（與 `review-verdict-spool` 共用同一份實作，
    #638）；本函式只負責 commit-spool 專屬的部分：路徑推導、symlink 守衛，以及把
    共用層的錯誤翻成 :class:`WorkspaceError`。

    守衛與慣例：

    - spool 目錄或 bundle 是 **symlink** → 一律拒絕。Manager 之後會直接
      `git fetch <那個檔案>`，讓它指向別處等於把回收路徑外包出去。
    - 那一格以 `reset=True` 建立：同一個 key 會被重跑（retry 用同一個 slice_id／
      同一張卡重派），上一輪 harvest 之後的封存（見 :func:`seal_commit_spool`）
      必須重新開封。這與 `launcher.launch()` 對 exit sentinel 與 gate ledger 的
      處置逐條一致。`spool_slot.create_slot()` 的解封做法是**整格重建**而不是
      `chmod` 回去——理由見該函式（`chmod` 只能猜一個 mask，正確的 mask 由
      default ACL 重新繼承才拿得到，#638 缺陷 1）。
    - 重建同時涵蓋了「殘留的 bundle（含 `.part`）在起跑前清掉」。**這比「已存在
      即拒絕」更強**：預埋一份 bundle 的人得到的不是拒絕派工，而是自己的檔案被
      刪掉；而 Manager 是這一格的 owner，刪得掉 builder 寫的檔。

    **不再傳明確 mode**（#638 缺陷 1）：在帶 default ACL 的樹上，`mkdir(mode=…)`
    會把 mask 一起重設，把 builder 繼承來的具名條目壓成 `#effective:---`，實機
    後果是 builder 連 `commits.bundle.part.lock` 都建不出來。初始權限交給 default
    ACL，事後只**檢查**並收窄 `other`（見 `spool_slot.narrow_inherited_mode()`）。

    真正的 owner／ACL 由 Phase 2b 的 permgen 依 R1 登記表套用（資產由 #636
    定義）；本函式只負責「這一格存在、而且是乾淨的」。
    """

    spool_dir = commit_spool_dir(spool_key=spool_key, coordinator_root=coordinator_root)
    try:
        spool_slot.create_slot(spool_dir, reset=True)
    except spool_slot.SpoolSlotError as exc:
        if exc.kind == "symlink":
            raise WorkspaceError(f"commit spool directory is a symlink: {spool_dir}") from exc
        raise WorkspaceError(f"commit spool directory unavailable: {spool_dir}: {exc}") from exc
    return spool_dir / COMMIT_BUNDLE_FILENAME


def seal_commit_spool(bundle: str | Path) -> None:
    """成果落地後把該 job 那一格轉唯讀（append-only spool 的封口）。

    封的是**目錄**而不是檔案：bundle 由 builder 的 uid 建立，Manager 不是它的
    owner、`chmod` 不了它（#638 缺陷 3）；但 Manager 是目錄的 owner，收掉目錄的
    `w` 之後該格就再也建不了、改不了名、刪不掉任何檔——而 POSIX ACL 的 mask 同時
    被 `chmod` 收窄，producer 具名條目的 `wx` 授權一併失效。實作與
    `review-verdict-spool` 共用 `spool_slot.seal_slot()`。

    best-effort：封存失敗不得讓一次**已經成功**的回收反而失敗（回收失敗才是
    #478／#601 的生產事故）。權威副本此時已經在來源樹的 `refs/heads/<branch>` 裡。
    """

    spool_slot.seal_slot(Path(bundle).parent)


def build_bundle_command(*, workspace: str | Path, bundle: str | Path) -> str:
    """builder 在自己的 clone 產出 bundle 的那一段 shell（由 wrapper script 執行）。

    形狀：

    ```
    git -C <clone> bundle create <bundle>.part "$(git -C <clone> symbolic-ref HEAD)" \
        ^refs/cortex/base && chmod 0644 <bundle>.part && mv -f <bundle>.part <bundle>
    ```

    三個決定：

    - **正向 ref 用 `symbolic-ref HEAD` 而不是寫死的 branch 名**——`launch()` 這一層
      拿不到 branch（它只收 `slice_id`／`worktree`／`log_dir`），而 bundle 必須帶
      **完整 ref 名**（`refs/heads/<branch>`），Manager 端才能用既有的
      `refs/heads/<b>:refs/heads/<b>` refspec 取回。builder 若把 HEAD 弄成 detached，
      這一步失敗、bundle 不存在 → 回收 fail-closed，正是想要的結果。
    - **負向 ref 是 `^refs/cortex/base`**（provision 當下 pin 的來源樹 commit，見
      :data:`BASE_REF`），讓 bundle 只帶這一輪的增量而不是整部歷史。
    - **`.part` → `chmod` → `mv`**：spool 裡看得見的 `commits.bundle` 恆為完整檔；
      `chmod` 到 `spool_slot.PUBLISHED_FILE_MODE` 是 #638 缺陷 2 的修法（producer
      自己放寬給 consumer）——檔由 builder 的 umask 建立（降權 unit 常帶
      `UMask=0077`），Manager 讀不到自己就沒東西可回收。放寬不擴張暴露面：那一格的
      容器是 `0700 cortex-manager` ＋ per-account `wx`，別的帳號連 traverse 都進不來。
      `review-verdict-spool` 走同一個常數（那邊的 producer 是模型，因此改由 wrapper
      script 的 `spool_slot.publish_file_command()` 段執行）。

    整段用 `&&` 串接：任何一步失敗都不會發表一個半成品 bundle。
    """

    workspace_arg = shlex.quote(str(workspace))
    final = shlex.quote(str(bundle))
    part = shlex.quote(str(bundle) + COMMIT_BUNDLE_PART_SUFFIX)
    return (
        f"git -C {workspace_arg} bundle create {part} "
        f'"$(git -C {workspace_arg} symbolic-ref HEAD)" ^{shlex.quote(BASE_REF)} '
        f"&& chmod {spool_slot.PUBLISHED_FILE_MODE:04o} {part} && mv -f {part} {final}"
    )


# ---------------------------------------------------------------------------
# 成果回收（Manager 拉，builder 不推）
# ---------------------------------------------------------------------------

def source_branch_head(source_repo: str | Path, branch: str) -> str | None:
    """來源樹上 `refs/heads/<branch>` 現在指到哪；不存在／不可讀時回 None。"""

    if not branch:
        return None
    proc = _git(["-C", str(source_repo), "rev-parse", f"refs/heads/{branch}"])
    if proc.returncode != 0:
        return None
    head = proc.stdout.strip().lower()
    return head if _SHA_RE.fullmatch(head) else None


def harvest_branch(
    *,
    source_repo: str | Path,
    bundle: str | Path,
    branch: str,
) -> str:
    """從 **bundle 檔**把 `branch` fetch 回來源 repo，回傳回收後的 branch head。

    這是三分模型下**成果離開 job 帳號的唯一路徑**。Manager 的 fetch 對象是一個
    普通檔案，**不是** builder 的 clone——這正是本次變更的全部價值，見模組 docstring。

    refspec 刻意不帶 `+`——非 fast-forward 由 git 拒絕，Manager 不會靜默吸收被
    改寫過的歷史（等價於 worktree 模型下 `branch -f` 前的 ancestry 守衛）。

    `git fetch` 對「bundle 不完整」的訊息（`error: Repository lacks these
    prerequisite commits:` ＋ 一串裸 SHA）看不出該怎麼辦，因此這裡逐類包一層可操作
    的說明。四類全部 fail-closed，沒有任何一條退回讀 clone。
    """

    source = Path(source_repo)
    bundle_path = Path(bundle)
    if not branch:
        raise WorkspaceError("harvest requires a branch name")
    if bundle_path.is_symlink():
        raise WorkspaceError(f"job workspace commit bundle is a symlink: {bundle_path}")
    if not bundle_path.is_file():
        raise WorkspaceError(
            f"job workspace commit bundle missing: {bundle_path}"
            "；builder 沒有產出 bundle。常見原因：(1) 這一輪工作區內沒有任何新 commit"
            f"（`git bundle create` 拒絕產生空 bundle）；(2) clone 內的 {BASE_REF} 被"
            "動過或 HEAD 是 detached，產 bundle 那一步失敗。逐字原因在該 job 的 JSONL "
            "log 末段（bundle 步驟與模型輸出寫同一份 log）"
        )
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    proc = _git(["-C", str(source), "fetch", "--no-tags", str(bundle_path), refspec])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        if "prerequisite" in detail:
            raise WorkspaceError(
                f"job workspace commit bundle is incomplete: {bundle_path}: {detail}"
                f"；bundle 以 `^{BASE_REF}` 收斂範圍，來源樹卻沒有那個 prerequisite "
                "commit。這代表 provision 當下 pin 的 base 與來源樹已經對不上"
                f"（工作區內的 {BASE_REF} 被改寫，或來源樹被 reset 掉了那段歷史）。"
                "處置：不要放寬 refspec——重新 provision 這張卡的工作區，讓 base 重新"
                "錨定在來源樹現有的 commit 上"
            )
        if "couldn't find remote ref" in detail or "find remote ref" in detail:
            raise WorkspaceError(
                f"job workspace commit bundle does not carry {branch}: {bundle_path}: {detail}"
                "；bundle 帶的是工作區 `HEAD` 當下所指的 branch，與 Manager 記錄的 "
                "branch 不同即代表 builder 換過 branch（或 HEAD detached 後另建）。"
                "處置：以 `git bundle list-heads <bundle>` 確認實際帶的 ref，"
                "不得改用其他 ref 回收"
            )
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


def harvest_if_spooled(
    *,
    source_repo: str | Path,
    job: Mapping[str, object],
    branch: str,
    coordinator_root: str | Path | None = None,
) -> str | None:
    """這個 job 有 spool 授權時做成果回收；否則回 None。

    判準是 **Manager-owned 的 spool 那一格在不在**，不是「工作區是不是 clone」——
    後者要讀 `<clone>/.git/` 底下的標記檔，而在三分部署下 Manager 讀不到，判準會
    恆為 False，退化成**靜默不回收**（最壞的失敗形態：成果沒進來，錯誤訊息卻出現
    在很遠的地方）。spool 那一格由 Manager 自己在 dispatch 當下建立，因此永遠讀得到。

    這也維持了 #634 的原則：**以工作區自己的形狀判斷，不依 `PSC_JOB_RUNNER` 分支**。
    spool 授權是 dispatch 當下就決定的形狀，`direct` 與降權模式走完全相同的路徑。

    回 None 的兩種情形（都是零回歸掛點）：job 還沒 launch（無 `log_path`）、或這個
    job 是升級前／測試用的假路徑（沒有 spool 那一格）。**spool 存在但 bundle 缺席
    不在此列**——那是真的出事了，一律 raise。
    """

    bundle = commit_bundle_path_for_job(job, coordinator_root=coordinator_root)
    if bundle is None:
        return None
    spool_dir = bundle.parent
    if spool_dir.is_symlink() or not spool_dir.is_dir():
        return None
    head = harvest_branch(source_repo=source_repo, bundle=bundle, branch=branch)
    seal_commit_spool(bundle)
    return head


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
    "BASE_REF",
    "COMMIT_BUNDLE_FILENAME",
    "COMMIT_BUNDLE_PART_SUFFIX",
    "MARKER_NAME",
    "MARKER_SCHEMA_VERSION",
    "SOURCE_REMOTE",
    "WORKSPACE_MODEL",
    "WorkspaceError",
    "archive_workspace_head",
    "build_bundle_command",
    "commit_bundle_path",
    "commit_bundle_path_for_job",
    "commit_spool_dir",
    "commit_spool_root",
    "harvest_branch",
    "harvest_if_spooled",
    "is_job_clone",
    "is_linked_worktree",
    "list_clone_workspaces",
    "marker_path",
    "prepare_commit_spool",
    "read_marker",
    "remove_clone",
    "seal_commit_spool",
    "source_branch_head",
    "spool_key_for_job",
    "workspace_branch",
    "write_marker",
]
