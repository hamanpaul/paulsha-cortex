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

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
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
_TEMPLATE_INSTANCE_UNSET = object()


#: per-job **具名片段**允許的字元。systemd unit 的 instance 名本身還允許更多
#: （`/` 需 escape），這裡刻意更窄：同一個字串會被 polkit 的 unit pattern 比對、
#: 被拼成 spec 檔名，也會成為 worktree pool 底下的一個目錄名。
JOB_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")


#: per-job 工作區授權用的外部程式（#710）。與
#: `trust_root.permgen.SYSTEM_PROGRAMS`／`RUN_EXTERNAL_DEPENDENCIES` 是**成對契約**
#: ——本模組刻意不 import `trust_root`，兩邊由測試釘住登記表真的有這一項。
SETFACL_PROGRAM = "setfacl"


class WorkspaceError(ValueError):
    """工作區 provision／回收的可操作錯誤。"""


# ---------------------------------------------------------------------------
# per-job 具名片段（#645：工作區目錄名 ＝ systemd 模板 instance 名，單一推導點）
# ---------------------------------------------------------------------------

def job_segment_valid(name: str) -> bool:
    """`name` 是否符合 :data:`JOB_SEGMENT_RE`（shim 端共用同一條判準）。"""

    return bool(name) and JOB_SEGMENT_RE.fullmatch(name) is not None


def job_segment(job_id: str) -> str:
    """job_id → 這個 job 的**具名片段**。全 repo 唯一的推導點。

    這一個字串同時是三個東西，而它們必須**逐字相等**：

    ==================================  ==========================================
    用途                                 由誰讀
    ==================================  ==========================================
    工作區目錄名 `<pool>/<segment>`      `seams.ScriptWorktreeCreator.create()`
    systemd 模板 instance 名             `job_runner.template_instance_id()`
    模板 unit 的 `%i`                    `permgen.build_job_unit()` 產的
                                        `ReadWritePaths=<pool>/%i`
    ==================================  ==========================================

    #645 的生產事故正是這三者曾經**各自推導**：provisioning 走 branch slug
    （`feature/<slice_id>` → `feature-<slice_id>`）、instance 走 job_id，兩者永遠差一個
    `feature-` 前綴，於是模板 unit 的 `ReadWritePaths` 指向一個不存在的路徑，systemd
    連 mount namespace 都建不起來（`226/NAMESPACE`）——降權派工因此從未經正式路徑
    成功啟動過任何 job。修法是把推導收斂成本函式；**呼叫端一律傳 job_id，不得自己
    拼名字**（兩邊各自算、剛好相等，撐不過下一次改名）。

    形狀：消毒後的可讀片段（保留可追蹤性）＋ job_id 的 sha256 前 8 碼（消毒後撞形
    也不會撞名）。與 #616 起既有的 instance 名推導**逐字相同**，因此既有部署的
    spec spool 檔名、polkit pattern 與 unit 名都不因本次變更而改變。
    """

    raw = str(job_id).strip()
    if not raw:
        raise WorkspaceError("job_id 為空，無法組出可追蹤的 per-job 片段")
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", raw).strip("-")[:48]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    if not slug or not slug[0].isalnum():
        slug = f"job{slug}" if slug else "job"
    segment = f"{slug}-{digest}"
    if not job_segment_valid(segment):
        raise WorkspaceError(
            f"推導出的 per-job 片段不合法: {segment!r}（job_id={raw!r}）"
        )
    return segment


def workspace_path(pool_root: str | Path, job_id: str) -> Path:
    """這個 job 的工作區絕對路徑：`<pool_root>/<job_segment(job_id)>`。"""

    return Path(pool_root) / job_segment(job_id)


# ---------------------------------------------------------------------------
# per-job 工作區的具名 ACL（#710）
# ---------------------------------------------------------------------------

#: `setfacl` 的 ACL spec 允許的 perms 形狀。會被逐字接進一條 `setfacl -R -m` 的引數，
#: 因此在**組命令之前**就驗——與 `job_segment_valid()` 同一條理由。
_ACL_PERMS_RE = re.compile(r"^[rwxX-]{1,4}$")

#: 帳號名的形狀。`setfacl` 的 spec 以 `:` 分段，帳號裡混進 `:`／`,` 會讓一條授權被
#: 解析成別的東西（或多出一條沒有人宣告過的）。
_ACL_ACCOUNT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")

#: `setfacl` 失敗時保留的 stderr 尾段長度（見 :func:`grant_workspace_acl`）。
_ACL_ERROR_TAIL = 2000


@dataclass(frozen=True)
class WorkspaceAclGrant:
    """per-job 工作區上的一條具名授權（#710）。

    形狀的**唯一真相**是 `trust_root.registry.JOB_WORKSPACE_REACH`；本模組刻意不
    import `trust_root`（與 `job_runner.JobRoleConfig.log_spool_principal` 同一個既有
    模式：path／派工熱路徑對治理平面零依賴），兩邊是**成對契約**，由
    `tests/test_per_job_workspace_acl_710.py` 釘住逐列相等。
    """

    account: str
    #: `setfacl -m` 的右手邊。大寫 `X`＝只有目錄與**已經可執行**的檔拿到 `x`
    #: ——遞迴套用時不會把整棵樹的一般檔案變成可執行檔。
    access_perms: str
    #: `setfacl -d -m` 的右手邊（default ACL，只對目錄有意義；`setfacl` 對一般檔案
    #: 的 default 條目會靜默略過）。
    default_perms: str

    def spec(self) -> str:
        """本條授權在 `setfacl` ACL spec 裡的兩個片段。"""

        return f"u:{self.account}:{self.access_perms},d:u:{self.account}:{self.default_perms}"


def _validate_grants(grants: "tuple[WorkspaceAclGrant, ...]") -> None:
    if not grants:
        raise WorkspaceError(
            "工作區 ACL 授權清單為空——空清單與「這個形態不需要 ACL」在輸出上長得"
            "一樣，而後者的判準是 reach 形態，不是清單長度（#710）。"
        )
    for grant in grants:
        if _ACL_ACCOUNT_RE.fullmatch(grant.account) is None:
            raise WorkspaceError(f"工作區 ACL 帳號名不合法: {grant.account!r}（#710）")
        for perms in (grant.access_perms, grant.default_perms):
            if _ACL_PERMS_RE.fullmatch(perms) is None:
                raise WorkspaceError(
                    f"工作區 ACL perms 不合法: {perms!r}（{grant.account}，#710）"
                )


def grant_workspace_acl(
    workspace: str | Path, grants: "tuple[WorkspaceAclGrant, ...]"
) -> str:
    """對 **per-job 那一格**遞迴套上具名 ACL；回傳實際執行的命令字串（診斷用）。

    ## 為什麼是 ACL 而不是 `chown`

    per-job clone 是 **Manager** 用 `git clone` 建出來的，owner 因此是 Manager。
    把它交給 job 帳號的直覺作法是 `chown`——但那需要 `CAP_CHOWN`，而 Manager unit 帶
    `CapabilityBoundingSet=`（空）。#623／#648 的「整個 clone 由本 job 帳號擁有」因此
    **不是漏寫一行，是方案與降權模型衝突**：它在 unit 註解裡活了兩個月、零程式實作，
    直到 builder job 第一次由 daemon 經正規路徑派出來，shim 在 `os.chdir()` 當場
    `[Errno 13] Permission denied`（#710 實機）。

    `setfacl` 由**目錄 owner** 執行，不需要任何 capability ⇒ Manager 做得到。保留
    owner 另外買到一件事：`gc`／`worktree_reclaim` 仍 `rmtree` 得掉整棵樹（那需要樹
    **內**的寫入權，交出 owner 等於讓工作區回收不了——#478／#601 的生產事故面）。

    ## 為什麼必須遞迴

    樹裡每個 inode 都由 Manager 以 `UMask=0077` 建立（`0600`／`0700`）。只在樹根下
    一條 ACL 的話，job `chdir` 得進去，卻讀不到裡面**任何**東西——那是一個比原症狀
    更難查的形狀（`git status` 空、`ls` 空、沒有錯誤）。

    ## ⚠️ 只能下在 per-job 那一格

    pool 根是 builder／reviewer／planner 三個帳號共用的容器；在它身上下 default ACL
    會讓**每個** job 帳號進得去**每個** job 的目錄，裁決 10-2 的 per-job 隔離當場
    歸零。本函式因此**只**接受一個具體的工作區路徑，且由
    `permgen._assert_job_workspace_reach_matches_the_plan()` 在 import 當下擋掉
    「把授權宣告到 pool 根」那條路。

    ## ⚠️ mask 陷阱

    `setfacl` 會在每次修改 ACL 時**重算 mask**，因此本函式套完之後具名條目的有效
    權限就是宣告值；但任何後續的 `chmod` 都會把 mask 重寫回 mode 的 group 位
    （runbook 4e-2b）。**本函式之後不得再 `chmod`**，驗證也一律看 `getfacl` 的
    `mask::`／`#effective:`，不是「ACL 行存在」。

    symlink 一律拒絕：`setfacl` 對**命令列上**的 symlink 引數預設跟著走，一條指向
    別處的 `<pool>/<job-id>` 會讓整棵不相干的樹被授權出去。
    """

    _validate_grants(grants)
    target = Path(workspace)
    if target.is_symlink():
        raise WorkspaceError(
            f"工作區是一條 symlink，拒絕套用 ACL: {target}"
            "（`setfacl` 會跟著命令列上的 symlink 走，#710）"
        )
    if not target.is_dir():
        raise WorkspaceError(f"工作區不存在或不是目錄，無法套用 ACL: {target}（#710）")
    binary = shutil.which(SETFACL_PROGRAM)
    if binary is None:
        raise WorkspaceError(
            f"{SETFACL_PROGRAM} 不在 Manager 的 PATH 上——per-job 工作區的具名 ACL "
            "因此套不上去，**每一個** job 都會 `chdir` 不進自己的工作區（#710）。"
            "它是登記表登記過的執行期相依（`permgen.RUN_EXTERNAL_DEPENDENCIES`／"
            "`SYSTEM_PROGRAMS`），由發行版的 `acl` 套件提供；0818 的三個部署陷阱之一"
            "就是這個套件缺席。"
        )
    # Access ACL 與 default ACL 分兩次遞迴套用。把 `u:...` 與 `d:u:...` 混在
    # 同一個 `setfacl -R -m` 裡，在部分 acl 版本／檔案系統組合會把 default
    # 條目送到 regular inode，回 `Invalid argument`；permgen 本來就以兩條
    # 命令產生這個順序。`-d` 這一趟只處理目錄，並保留對既有子目錄的繼承契約。
    access_spec = ",".join(
        f"u:{grant.account}:{grant.access_perms}" for grant in grants
    )
    default_spec = ",".join(
        f"u:{grant.account}:{grant.default_perms}" for grant in grants
    )
    commands = (
        [binary, "-R", "-m", access_spec, str(target)],
        [binary, "-R", "-d", "-m", default_spec, str(target)],
    )
    rendered = " && ".join(shlex.join(command) for command in commands)
    for argv in commands:
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        if completed.returncode == 0:
            continue
        # `setfacl -R` 每個失敗的 inode 各印一行——一棵 35MB 的 clone 可以印出好幾 MB。
        # 只留尾段：失敗原因對整棵樹一律相同（`Operation not permitted`＝不是 owner、
        # `No such file or directory`＝樹在中途被回收），而完整清單只會把真正的錯誤
        # 從 operator 的畫面上推走。
        detail = (completed.stderr or completed.stdout).strip()
        if len(detail) > _ACL_ERROR_TAIL:
            detail = f"…（前 {len(detail) - _ACL_ERROR_TAIL} 字元省略）" + detail[-_ACL_ERROR_TAIL:]
        raise WorkspaceError(
            f"per-job 工作區的具名 ACL 套用失敗: {shlex.join(argv)}: {detail}（#710）"
        )
    return rendered


def legacy_branch_slug(branch: str) -> str:
    """#645 **之前**的工作區目錄名：`branch.replace("/", "-")`。

    只給**回收／診斷**路徑用，provisioning 一律不得再產生這個形狀。既有部署的磁碟上
    仍可能留著這種目錄（升級前 provision 的、或失敗殘留的），回收端必須認得它——
    但認得不等於刪得掉：實際刪除仍走 `worktree_reclaim`／`gc` 的形狀判準
    （標記檔或 `.git` 檔），認不出來的目錄一律 fail-closed，不刪。
    """

    return str(branch).replace("/", "-")


def reclaim_candidate_paths(
    pool_root: str | Path,
    *,
    job_id: str | None = None,
    branch: str | None = None,
) -> list[Path]:
    """某個 job 的工作區**可能**落在哪些路徑（新形狀優先、保序去重）。

    job／slice 記錄沒有 `worktree` 欄位時（舊列、或 provision 途中就炸掉），回收端只能
    由 id／branch 反推。#645 換名之後「反推」必須同時涵蓋兩種形狀，否則升級當下磁碟上
    的 `feature-<id>` 殘留會被回收端當成「不存在」而靜默略過，下一次 provision 就撞
    `worktree target already exists`（#601 的生產現場）。

    只回傳**候選路徑**，不做任何刪除判斷：呼叫端把每一條交給
    `worktree_reclaim.reclaim_worktree()`，那裡的安全閘負責「不認得就不刪」。
    """

    root = Path(pool_root)
    candidates: list[Path] = []
    if job_id:
        try:
            candidates.append(workspace_path(root, job_id))
        except WorkspaceError:
            pass
    if branch:
        candidates.append(root / legacy_branch_slug(branch))
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


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
    return spool_slot.canonical_job_slot(
        "commit-spool", "placeholder", coordinator_root=coordinator_root
    ).parent


def commit_spool_dir(
    *,
    spool_key: str,
    coordinator_root: str | Path | None = None,
) -> Path:
    """單一 job 的 spool 目錄（唯一定址點）。"""

    if not isinstance(spool_key, str) or _SPOOL_KEY_RE.fullmatch(spool_key) is None:
        raise WorkspaceError(f"unsafe commit spool key: {spool_key!r}")
    return spool_slot.canonical_job_slot(
        "commit-spool", spool_key, coordinator_root=coordinator_root
    )


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

    **推導規則只有一條**：優先使用 launch 當下持久化的 ``template_instance``；
    只有 genuinely legacy/direct rows 才回退到 Manager registry 的 ``job_id``。
    log path、session name、worktree 名、payload text 與 caller 自述都不是 slot
    authority，不能反向決定 systemd ``%i`` 所指的 owned slot。

    這件事必須是**單一規則**：template lane 若把 authority 掉了，就寧可 fail
    closed，也不能再去猜別的 sibling/foreign slot；direct 與舊 state rows 才
    允許沿用 ``job_id`` fallback。

    job 還沒取得 registry identity 時回 None——沒有可採信的 spool authority。
    """

    template_instance = _template_instance_for_job(job)
    if template_instance is _TEMPLATE_INSTANCE_UNSET:
        pass
    elif template_instance is None:
        return None
    else:
        return template_instance

    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        return None
    if not job_segment_valid(job_id):
        return None
    return job_id


def _template_instance_for_job(job: Mapping[str, object]) -> str | None | object:
    if "template_instance" not in job:
        if job.get("runtime_mode") == "systemd-template":
            return None
        return _TEMPLATE_INSTANCE_UNSET
    template_instance = job.get("template_instance")
    if template_instance is None:
        if job.get("runtime_mode") == "systemd-template":
            return None
        return _TEMPLATE_INSTANCE_UNSET
    if isinstance(template_instance, str) and job_segment_valid(template_instance):
        return template_instance
    return None


def commit_bundle_path_for_job(
    job: Mapping[str, object],
    *,
    coordinator_root: str | Path | None = None,
) -> Path | None:
    """該 job 的 bundle 路徑；推導不出 spool key 時回 None。"""

    template_instance = _template_instance_for_job(job)
    if template_instance is _TEMPLATE_INSTANCE_UNSET:
        pass
    elif template_instance is None:
        return None
    else:
        return (
            spool_slot.exact_job_slot(
                "commit-spool", template_instance, coordinator_root=coordinator_root
            )
            / COMMIT_BUNDLE_FILENAME
        )
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


#: launcher 派出的 job 在自己那一格 log spool 裡的檔名。副檔名刻意仍是 `.jsonl`
#: ——Manager 直接讀這個 canonical surface（見 :func:`prepare_job_log_spool`），而
#: harvest 端的 `usage_extractors` 讀的就是 JSONL。
JOB_LOG_FILENAME = "job.jsonl"

#: 降權 planning job 在自己那一格 log spool 裡的檔名（#686 起的既有字面量，
#: #727 起收斂到這裡）。理由與 :data:`JOB_LOG_FILENAME` 同一條：`-o` 的落點由
#: 它機械導出（:func:`job_last_message_path`），檔名散成兩份就會漂移。
PLANNING_JOB_LOG_FILENAME = "planning.log"

#: executor 的「最後一則訊息」落點副檔名（codex 的 `--output-last-message`／`-o`，#714）。
JOB_LAST_MESSAGE_SUFFIX = ".last.json"

# A template job writes its canonical log below one of the per-principal
# writable spools.  Manager-owned completion controls deliberately stay in the
# dispatch log directory: the job must be able to append its log, but it must
# never be able to create or replace the exit sentinel or gate ledger.
_ISOLATED_JOB_LOG_LAYOUTS = frozenset(
    {
        (paths.COMMIT_SPOOL_DIRNAME, paths.BUILD_JOB_LOG_SPOOL_DIRNAME, JOB_LOG_FILENAME),
        (
            paths.REVIEW_VERDICT_SPOOL_DIRNAME,
            paths.PLANNING_JOB_LOG_SPOOL_DIRNAME,
            PLANNING_JOB_LOG_FILENAME,
        ),
        (
            paths.GATE_LEDGER_SPOOL_DIRNAME,
            paths.GATE_JOB_LOG_SPOOL_DIRNAME,
            "gate.log",
        ),
    }
)


def job_last_message_path(job_log_path: str | Path) -> Path:
    """該 job 的 `-o last message` 落點：**它自己那份 log 的兄弟檔**（#714）。

    ## 修的是什麼

    `#708`／PR #709 把 job log 搬進 `<commit-spool>/build-logs/<job>/`，但 codex 的
    `--output-last-message` 仍指著**舊的** `<coordinator_root>/logs/workflow/`——那一格
    `0700 cortex-manager`、零具名 ACL，正是 #708 的原症狀。實機 0819 逐字：

        Failed to write last message file
        "/var/lib/cortex/coordinator/logs/workflow/last.json": Permission denied (os error 13)

    **而且它是共用路徑**（`last.json`，不帶 job id）⇒ 就算補了授權，並行的兩個 job
    也會互相蓋掉。

    ## 為什麼是「log 的兄弟檔」這一條規則，而不是第二個落點決定

    因為那正是 #708 已經替每個角色回答過的問題：「這個 job 寫得進去的那一格在哪」。
    再決定一次就會再錯一次（#708 的破口逐字是「三個 principal 的 log 落點**各自**被
    決定」）。由 job log 路徑機械導出之後，兩種派工模式都自動落在對的地方，且**都帶
    job id**：

    - 降權（模板 unit）：`<build-logs>/<job>/job.jsonl` ⇒ `…/<job>/job.last.json`
      ——那一格是 `registry.JOB_LOG_SPOOLS` 導出的、該 principal 已經有 `wx` 的資產，
      掛在既有通道底下 ⇒ 模板 unit 的 `ReadWritePaths=` **逐字不變、零部署動作**；
    - direct：`<log_dir>/<slice>.jsonl` ⇒ `<log_dir>/<slice>.last.json`
      ——同一個目錄（direct 模式下 Manager 就是寫者），但檔名帶了 slice id，共用路徑
      的並行覆寫一併解掉。

    ## #727：planning probe 這條路徑也回到同一條規則

    #714 只替 **builder** lane 修好（`launcher.launch()` 那一條）。`planning_runtime.
    _planning_argv()` 當時仍自己組 `Path(temp_dir)/"last.json"`——**第二份落點決定**，
    於是 planning job 的 `-o` 落在 unit 的 `PrivateTmp=yes` 私有 `/tmp`：job 寫得進去，
    **Manager 讀不到**（`planning_job` 的 D-j／R-2 退步）。`_extract_json` 因此退成單
    候選，而它當時解不了 codex 的 `--json` 串流 ⇒ `ValueError: planning launcher
    returned no JSON object` ⇒ 唯一有憑證、剖面也對的 planner 候選永遠 not-ready。

    #727 起 planning 那一條也由本函式導出：

    - 降權（模板 unit）：`<planning-logs>/<instance>/planning.log`
      ⇒ `…/<instance>/planning.last.json`——同一格 log spool，job 已有 `wx`，
      模板 unit 的 `ReadWritePaths=` 逐字不變、零部署動作；且 Manager 是那一格的
      owner ⇒ 預建（`spool_slot.preseed_job_writable_file`，mode `0620`）之後**讀得回來**；
    - direct：`<tempdir>/planning.log` ⇒ `<tempdir>/planning.last.json`——同一個
      一次性 tempdir，行為與修法前等價（只有檔名換了）。

    **它不是證據面**：沒有任何採信路徑讀它（`gate_ledger`／exit sentinel／harvest 讀的
    都是別的檔），因此把它放進 job 寫得到的那一格不會動到 #604 的作者性保證。
    planning 那一條也一樣——`_extract_json` 讀它只是為了拿模型輸出本體，而模型輸出
    本來就是 job 產的。
    """

    log = Path(job_log_path)
    return log.with_name(log.stem + JOB_LAST_MESSAGE_SUFFIX)


def manager_control_log_path(job_log_path: str | Path) -> Path:
    """Return the Manager-only control-log anchor for a canonical job log.

    The JSONL log itself is intentionally Manager-readable and job-writable,
    so it lives in the job's existing writable spool.  Completion controls are
    different: ``.exit`` and ``.gates.json`` are Manager-authored and must stay
    outside that directory.  The old implementation used a hard link between
    these two surfaces.  ``ProtectSystem=strict`` gives separate
    ``ReadWritePaths`` bind mounts separate mount identities, so that design
    fails with ``EXDEV`` in the live Manager namespace.

    New template launches persist the raw Manager control anchor separately
    (`LaunchHandle.control_log_path` / registry `control_log_path`) because the
    canonical job-writable slot now follows systemd `%i`.  Callers that care
    about Manager-authored completion controls must consume that explicit field
    rather than try to reconstruct it from the canonical spool path.  This
    helper therefore keeps the historical projection for the registered legacy
    layouts while arbitrary direct / already-explicit paths retain their
    sibling behavior.
    """

    path = Path(job_log_path)
    layout = (path.parents[2].name, path.parents[1].name, path.name) if len(path.parents) >= 3 else None
    if layout not in _ISOLATED_JOB_LOG_LAYOUTS:
        return path
    key = path.parent.name
    if _SPOOL_KEY_RE.fullmatch(key) is None:
        return path
    # parents[3] is the shared ``coordinator`` root for all three registered
    # layouts.  Do not resolve or follow anything here: this is compatibility
    # projection only, and the actual Manager-authored controls still undergo
    # their existing regular-file/owner checks.  New template launches persist
    # the raw Manager control anchor explicitly because `%i` may be a hashed
    # instance name and cannot be reversed to the original slice id here.
    coordinator_root = path.parents[3]
    return coordinator_root.parent / "runtime" / "dispatch" / f"{key}.jsonl"


def job_log_spool_dir(*, principal_id: str, spool_key: str) -> Path:
    """該 principal 那一格 job log spool 目錄（唯一定址點，#708）。

    `principal_id` 由派工端的**角色**決定（`job_runner.JobRoleConfig.
    log_spool_principal`），不是這裡猜的：launcher 同時派 builder 與 reviewer 兩種
    job，兩者走的是不同的模板 unit、不同的帳號，因此也是不同的一條既有輸出通道。

    `spool_key` 是呼叫端已經決定好的 slot 名：模板 job 傳進來的是 unit `%i`
    （`template_plan.instance`），而 raw `slice_id` 留在另外那條 explicit
    `manager_log_path` / `control_log_path`。這裡唯一承重的是「key 的形狀守衛與
    `gate_runner._validate_spool_key` 同一條」——兩邊用不同判準就會出現「這一格建得
    起來、那一格建不起來」的錯位。
    """

    if not isinstance(spool_key, str) or _SPOOL_KEY_RE.fullmatch(spool_key) is None:
        raise WorkspaceError(f"unsafe job log spool key: {spool_key!r}")
    try:
        root = paths.job_log_spool_root(principal_id)
    except ValueError as exc:
        raise WorkspaceError(str(exc)) from exc
    return root.resolve() / spool_key


def prepare_job_log_spool(
    *,
    principal_id: str,
    spool_key: str,
    manager_log_path: str | Path,
) -> Path:
    """建出該 job 的 Manager-readable canonical log 一格。

    回傳的是 **job 端**的路徑——它就是要寫進 spec `log_path` 的那一個值，shim 在降權
    之後以 `O_NOFOLLOW` 開的也是它。

    ## 為什麼不再建立 Manager 端 hard link

    `<log_dir>/<slice>.jsonl` 不只是一個 log：舊實作還把 **exit sentinel
    （`<slice>.exit`）、gate ledger（`<slice>.gates.json`）與 spool key 全部由它
    逐字推導**（`dispatcher.exit_sentinel_path`／
    `terminal_contract.gate_ledger_path`／:func:`spool_key_for_job`）。現在只有
    control paths 經 :func:`manager_control_log_path` 投影；兩個檔的全部保證仍是
    「由 Manager 寫、採信端以 `foreign_evidence_author()` 檢查擁有者」。

    反過來把 builder 加進 Manager 的 dispatch log 目錄也不行，理由同一個：那一層
    住著 gate ledger 與 sentinel。

    先前以 hard link 讓兩件事同時成立，但 live `ProtectSystem=strict` namespace
    會把兩個 `ReadWritePaths` 做成不同 mount identity，故不能再依賴這個橋：

    - **job 側**只看得到自己 spool 裡的那一格（`ProtectSystem=strict` ＋ 登記表 ACL
      導出的可寫面，Manager 的 log 目錄它連 traverse 都進不去）；
    - **Manager 側**直接讀 job spool 的 canonical `log_path`；沒有複本，也沒有
      「job 還在跑但 Manager 只看得到舊內容」的同步競態。sentinel／ledger 另走
      Manager-only control surface。

    ## 為什麼不是 symlink

    shim 一律以 `O_NOFOLLOW` 開 log（那是它對「spool 目錄被埋 symlink」的既有防線），
    symlink 會讓它當場失敗。job 若替換自己的 log 名稱，只能造成 Manager 看到缺失
    或不完整的診斷；它不能替換 Manager-only 的 completion controls。

    現在不再跨 mount 建 link。`job.jsonl` 是 Manager 預建的 regular file（mode
    `0620`），所以 Manager 可以直接讀同一個 canonical surface，而 job 只能以
    `O_APPEND` 寫入它；sentinel／ledger 不由這條路徑推導，仍由
    :func:`manager_control_log_path` 投影到 Manager-only control surface。若 job
    unlink／replace 自己的 log 名稱，Manager 只會得到缺失或不完整的診斷，不能因此
    產生一份可採信的 completion control。

    ## 誠實邊界：本函式**不**封口，另外兩個 spool 會

    `commit-spool`／`gate-ledger-spool` 在成果落地後 `seal_slot()`（收掉目錄的 `w`
    ⇒ ACL mask 連帶失效），log 這一格**刻意沒有**對應的封口點：builder 的收割是非
    同步的（harvest 由 daemon 的另一個 tick 做，失敗的 job 根本不 harvest），而 gate
    是同步的、所以它自己封（`gate_runner._run_as_gate_identity`）。殘餘風險是「job
    在自己的 log 被判讀之後繼續追寫」——**自報噪音，不是提權**（它寫的還是自己那一格，
    Manager 也不會回頭重讀已判讀的 log）。要收掉它得有一個「這個 job 結束了」的單一
    收斂點，那不在本票範圍內；把一個沒有人呼叫的 `seal_job_log_spool()` 放在這裡，
    只會讓治理面看起來比實際多一條。下一輪同 key 派工會整格重建（`reset=True`），
    因此它也不會無限長大。
    """

    slot = job_log_spool_dir(principal_id=principal_id, spool_key=spool_key)
    # ``manager_log_path`` remains an input for source/API compatibility and
    # for callers' explicit control-surface bookkeeping.  It is intentionally
    # not opened or linked here: the two paths may be different systemd bind
    # mounts even when they share a host filesystem device.
    _ = Path(manager_log_path)
    try:
        job_log = spool_slot.prepare_job_log(slot, slot / JOB_LOG_FILENAME)
    except spool_slot.SpoolSlotError as exc:
        if exc.kind == "symlink":
            raise WorkspaceError(f"job log spool directory is a symlink: {slot}") from exc
        raise WorkspaceError(f"job log spool directory unavailable: {slot}: {exc}") from exc
    except OSError as exc:
        raise WorkspaceError(f"job log spool unavailable: {slot}: {exc}") from exc
    return job_log


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


def publish_commit_bundle(
    *,
    workspace: str | Path,
    bundle: str | Path,
    branch: str,
    exclude: str | None,
) -> Path:
    """#649：**in-process 版**的 :func:`build_bundle_command`——producer 是 Manager
    自己，不是 wrapper script 驅動的 builder。

    存在的理由：ship phase 的 `openspec-archive` commit 由 **Manager 親手**做出來
    （`work_bridge._commit_archive_and_require_reverification` 直接 `git commit`），
    不是任何 job 的模型產物，因此沒有 wrapper script 可以掛那段 shell。但**回收
    通道必須是同一條**——`harvest_branch()` 是「commit 進來源樹」的唯一實作，繞過
    它另寫一次 `git fetch <某棵樹>` 會同時複製一份 fail-closed 分類，而且會把
    「Manager fetch 一棵 job 的樹」這個模組 docstring 明文否決的形狀寫回程式碼。

    因此這裡只補 producer 那一半：把 bundle 產出來，交給既有的 consumer 那一半。

    - **正向 ref 寫死 `refs/heads/<branch>`**（不是 `symbolic-ref HEAD`）：呼叫端
      是 Manager，branch 是它自己記錄的權威值；用 HEAD 反而會在 detached 時發表
      一個 ref 名對不上的 bundle。
    - **負向 ref 由呼叫端決定**（`exclude`），而不是寫死 :data:`BASE_REF`：Manager
      要排除的是「來源樹**已經有**的那個 commit」（＝已被採信的 candidate），而
      `refs/cortex/base` 是那棵 clone 自己 provision 當下的 pin，兩者不同。
      `exclude=None` ⇒ 不排除任何東西，bundle 帶完整歷史、無 prerequisite——留給
      「來源樹沒有那個 commit」的情形（升級前的既有 run、沒有走過 build harvest
      的測試路徑），否則 `harvest_branch()` 會因缺 prerequisite 而 fail-closed。
    - `.part` → 放寬 → `mv`：與 wrapper 那條逐條相同，spool 裡看得見的
      `commits.bundle` 恆為完整檔。

    `git bundle create` 對「沒有任何 commit 可帶」會拒絕產出（空 bundle）——那在
    這條路徑上是真的出事（Manager 剛做完一個 commit），因此一律 raise，不回 None。
    """

    if not branch:
        raise WorkspaceError("commit bundle publication requires a branch name")
    bundle_path = Path(bundle)
    if bundle_path.is_symlink() or bundle_path.parent.is_symlink():
        raise WorkspaceError(f"job workspace commit bundle is a symlink: {bundle_path}")
    part = bundle_path.with_name(bundle_path.name + COMMIT_BUNDLE_PART_SUFFIX)
    part.unlink(missing_ok=True)
    argv = ["-C", str(workspace), "bundle", "create", str(part), f"refs/heads/{branch}"]
    if exclude is not None:
        if _SHA_RE.fullmatch(exclude.lower()) is None:
            raise WorkspaceError(f"commit bundle exclusion is not a commit sha: {exclude!r}")
        argv.append(f"^{exclude.lower()}")
    proc = _git(argv)
    if proc.returncode != 0 or not part.is_file():
        part.unlink(missing_ok=True)
        raise WorkspaceError(
            f"commit bundle creation failed: {(proc.stderr or proc.stdout).strip()}"
            f"；工作區 {workspace} 的 refs/heads/{branch} 產不出 bundle。"
            "常見原因：branch 不存在於該工作區、或排除範圍已涵蓋全部 commit"
            "（`git bundle create` 拒絕產生空 bundle）"
        )
    spool_slot.publish_file(part)
    part.replace(bundle_path)
    return bundle_path


# ---------------------------------------------------------------------------
# 成果回收（Manager 拉，builder 不推）
# ---------------------------------------------------------------------------

def commit_present(repo: str | Path, sha: str) -> bool:
    """`repo` 的 object store 裡有沒有這個 commit（#649）。

    用途只有一個：決定 :func:`publish_commit_bundle` 的 `exclude` 能不能用——
    bundle 的 prerequisite 必須是**來源樹已經有**的 commit，否則 `harvest_branch()`
    會在 fetch 那一步以「prerequisite 缺席」fail-closed。
    """

    if not isinstance(sha, str) or _SHA_RE.fullmatch(sha.lower()) is None:
        return False
    return _git(["-C", str(repo), "rev-parse", "--verify", "--quiet", f"{sha.lower()}^{{commit}}"]).returncode == 0

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
    "JOB_SEGMENT_RE",
    "MARKER_NAME",
    "MARKER_SCHEMA_VERSION",
    "SETFACL_PROGRAM",
    "SOURCE_REMOTE",
    "WORKSPACE_MODEL",
    "WorkspaceAclGrant",
    "WorkspaceError",
    "archive_workspace_head",
    "grant_workspace_acl",
    "build_bundle_command",
    "commit_bundle_path",
    "commit_bundle_path_for_job",
    "commit_spool_dir",
    "commit_spool_root",
    "harvest_branch",
    "harvest_if_spooled",
    "is_job_clone",
    "is_linked_worktree",
    "job_segment",
    "job_segment_valid",
    "legacy_branch_slug",
    "list_clone_workspaces",
    "marker_path",
    "prepare_commit_spool",
    "read_marker",
    "reclaim_candidate_paths",
    "remove_clone",
    "job_log_spool_dir",
    "manager_control_log_path",
    "prepare_job_log_spool",
    "seal_commit_spool",
    "source_branch_head",
    "spool_key_for_job",
    "workspace_branch",
    "workspace_path",
    "write_marker",
]
