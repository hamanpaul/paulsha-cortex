"""issue #478：build worktree 的原子化回收（目錄 ＋ git worktree registry）。

回收 build worktree 一直分散在兩處手寫片段（`manager.apply_slice_action` 的
`recover-pre-candidate` 分支與 `work_actions._recover_pre_candidate_action`），
兩處都只在「目錄還在」時才嘗試 git 清理、失敗一律吞掉，於是產生 #478 的生產
現場：目錄刪了、`git worktree list --porcelain` 仍留著同一條 registry 記錄
（`prunable gitdir file points to non-existent location`），下一個 tick 的
`git worktree add` 立刻以 `cannot force update the branch ... used by worktree
at ...` 失敗，slice 被打回 `needs_human`。

本模組把回收收斂成單一函式，契約如下：

- **原子性**：「目錄不存在」與「registry 無該筆記錄」兩個後置條件必須同時成立，
  否則回收判定為 ``failed``——呼叫端據此 fail closed，不得回報成功。
- **自癒**：既存壞狀態（目錄已不存在、registry 殘留）也要能收乾淨；因此
  registry 探測先於目錄探測，不以目錄存在與否作為要不要清 registry 的條件。
- **不銷毀證據**：見下節。回收永遠不得讓「還沒有第二份副本的東西」消失。

## 「不銷毀證據」的兩種模型（#658）

原始契約只有一種做法：目錄若帶未提交／未追蹤內容，先複製到 ``preserve_root``
底下的 reclaim 封存再刪；封存失敗即 ``failed``，一個位元組都不刪（#478 的
`.project-policy.yml` 資料遺失回報）。#658 把它拆成**兩個具名模型**，呼叫端必須
明講自己屬於哪一種——**不是靜默跳過**：

:data:`EVIDENCE_PRESERVE`（預設，語意與 #478 逐字相同）
    「這棵樹裡可能有還沒有第二份副本的東西」。所有**未採信**路徑一律走這條：
    `recover-pre-candidate`（#478／#547）、`abandon` 的回收（#527）、#601 的
    retry 前殘留。這些路徑的共同性質是**成果沒有被 harvest 過**——工作區裡的
    commit 只存在於它自己的 object store，未提交內容更是沒有任何其他副本。

:data:`EVIDENCE_HARVESTED`（#658 新增）
    「這棵樹裡的每一樣**受治理**的東西都已經有第二份副本，且那份副本在
    Manager-owned 的樹裡」。呼叫端 MUST 在傳入這個值之前證明該前提；本模組
    **不**代為證明（它只拿得到一個路徑）。目前唯一的合法呼叫端是
    `manager._reclaim_trusted_build_workspace()`——它在
    `_harvest_build_candidate()` 之後、且**當場對來源樹複驗** candidate 已經
    在裡面、`refs/heads/<branch>` 恰等於它，才會用這個模型。

### 為什麼 `harvested` 底下可以不做 preserve（#658 的論證，不是省略）

被回收的是 canonical lane 一張**已被採信**的 build 卡的工作區。逐條盤點它當下
還持有什麼，以及那樣東西的第二份副本在哪：

===============================  ===================================================
工作區裡的東西                    第二份副本
===============================  ===================================================
被採信的 commit                   來源樹的 ``refs/heads/<branch>``（#637 bundle ＋
                                 append-only spool；`_harvest_build_candidate()`
                                 強制「回收後 branch head 恰等於 candidate」）
bundle 本身                       Manager-owned 的 `commit-spool`（封口後仍在）
gate ledger                       `gate-ledger-spool` → Manager 重寫的 `gate-ledger`
                                 （#628／#629，作者一律是 Manager）
exit sentinel                     Manager 自己寫（#604／#628）
JSONL log                         `log_dir`（Manager-owned），本來就不在工作區裡
宣告的 outputs                    採信當下已由 `_read_job_workflow_evidence()` 逐檔
                                 hash 進 immutable canonical evidence
canonical report                  #650／#659 之後發佈在 reviewer 的 candidate 樹，
                                 不在 build 卡的工作區
===============================  ===================================================

**剩下的就只有「模型做了、但既沒 commit、也沒宣告為 output」的未追蹤殘渣。**
它在採信面上的地位是零——#540 的 acceptance chain 採信的是 candidate commit，
未提交內容從定義上就不在採信面內；#628 更明講「被驗方不得在自己的進程裡產生
自己的驗收證據」，而這些檔案正是 builder 完全掌控的內容。把它們複製進
Manager-owned 的 ``evidence/`` 樹，實際效果是**把不受信任的內容搬進受信任的
樹**，並且把本次要回收的位元組原地搬個家（一張卡就可能是 512 檔 × 4MB）——
那與 #658 要解的「工作區佔用隨 build 卡數線性成長」是同一個問題換個目錄。

**#478 的現場不適用**：那次遺失的是 operator 自己 worktree 裡的真實工作，
發生在**未採信**路徑上。那條路徑仍然、也必須走 :data:`EVIDENCE_PRESERVE`。

**兩個模型共用的保險絲**：`job_workspace.archive_workspace_head()` 在**兩種模型
下都跑**。它把工作區 HEAD 拉進來源樹的封存命名空間，且「commit 已在來源樹裡」
時直接回 None。因此在 `harvested` 模型下它正常是 no-op；一旦呼叫端的前提其實
不成立（commit 沒進來源樹），它就會把那顆 commit 救回來——這是模型選錯時的
安全網，不是可有可無的加分項。

git_runner seam 沿用 `dispatcher.GitRunner` 契約：收 **git 子命令參數**（不含
前導 ``git``、不含 ``-C``），對 repo root 執行，失敗時 raise。#478 驗收條款
「do not prepend `git` if the seam accepts Git subcommand arguments only」指的
就是舊碼在此處多塞一個 ``git`` 造成 `git -C <repo> git worktree remove ...`。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from paulsha_cortex.config import paths

from . import job_workspace
from .dispatcher import _default_git_runner

logger = logging.getLogger(__name__)

GitRunner = Callable[[list[str]], Any]

# 回收結果三態。`absent` 與 `reclaimed` 都算成功（後置條件成立）；只有
# `failed` 代表後置條件無法證實，呼叫端必須 fail closed。
RECLAIM_RECLAIMED = "reclaimed"
RECLAIM_ABSENT = "absent"
RECLAIM_FAILED = "failed"

# 封存單檔上限；超過者只記名不複製（builder worktree 偶有大型 build 產物，
# 全複製會讓回收變成不可預期的 I/O）。
PRESERVE_FILE_MAX_BYTES = 4 * 1024 * 1024
# 封存檔數上限，理由同上。
PRESERVE_FILE_MAX_COUNT = 512

# 「不銷毀證據」的兩種模型（完整論證見模組 docstring）。**預設一律是
# `EVIDENCE_PRESERVE`**：新呼叫端忘了表態時得到的是保守的那一個。
EVIDENCE_PRESERVE = "preserve"
EVIDENCE_HARVESTED = "harvested"
_EVIDENCE_MODELS = frozenset({EVIDENCE_PRESERVE, EVIDENCE_HARVESTED})


@dataclass(frozen=True)
class WorktreeReclaim:
    """單一 worktree 的回收結果（機器可讀，供 action record／診斷帶出）。"""

    status: str
    path: str
    registry_entry_found: bool = False
    registry_removed: bool = False
    directory_removed: bool = False
    preserved_ref: str | None = None
    preserved_files: int = 0
    #: #623：per-job clone 被刪除前，其 HEAD 被封存到來源 repo 的哪一條 ref
    #: （`job_workspace.ARCHIVE_REF_PREFIX` 底下）。worktree 模型、或 commit 已在
    #: 來源 repo 裡（成果已回收）時為 None。
    archived_ref: str | None = None
    #: #658：本次回收採用的「不銷毀證據」模型（見模組 docstring）。帶進 action
    #: record／診斷是刻意的——operator 看得到某一次回收**為什麼**沒有 preserve 封存。
    evidence_model: str = EVIDENCE_PRESERVE
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {RECLAIM_RECLAIMED, RECLAIM_ABSENT}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "path": self.path,
            "registry_entry_found": self.registry_entry_found,
            "registry_removed": self.registry_removed,
            "directory_removed": self.directory_removed,
            "evidence_model": self.evidence_model,
        }
        if self.preserved_ref is not None:
            payload["preserved_ref"] = self.preserved_ref
            payload["preserved_files"] = self.preserved_files
        if self.archived_ref is not None:
            payload["archived_ref"] = self.archived_ref
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


def _pinned_git_runner(repo_root: Path) -> GitRunner:
    def _runner(args: list[str]) -> str:
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

    return _runner


def resolve_git_runner(
    git_runner: GitRunner | None = None,
    *,
    repo_root: str | Path | None = None,
) -> GitRunner:
    """#478 驗收條款：未注入 runner 時必須退回 production-safe 的預設實作。

    舊碼是 `runner = git_runner or getattr(dispatcher, "_git_runner", None)`，
    而生產 dispatcher 的 `_git_runner` 合法為 ``None``（它自己在 dispatch 時
    才 fallback 到預設），於是 recovery 整段 git 清理被跳過。
    """

    if git_runner is not None:
        return git_runner
    if repo_root is not None:
        return _pinned_git_runner(Path(repo_root))
    return _default_git_runner


def _run(runner: GitRunner, args: list[str]) -> tuple[bool, str, str]:
    """執行 git 子命令；回傳 (成功, stdout, 錯誤摘要)。

    同時吃兩種 runner 回傳型別：dispatcher 風格（回 stdout 字串、失敗 raise）
    與 `subprocess.CompletedProcess` 風格，避免呼叫端被迫改造既有 seam。
    """

    try:
        raw = runner(list(args))
    except Exception as exc:  # noqa: BLE001 - seam 失敗一律轉成結構化結果
        return False, "", f"{type(exc).__name__}: {str(exc)[:400]}"
    if isinstance(raw, str):
        return True, raw, ""
    returncode = getattr(raw, "returncode", None)
    stdout = getattr(raw, "stdout", "") or ""
    stderr = getattr(raw, "stderr", "") or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if not isinstance(returncode, int):
        # 無法判定回傳碼的 seam（多半是測試 double）視為成功但無輸出。
        return True, str(stdout), ""
    if returncode != 0:
        return False, str(stdout), str(stderr).strip()[:400]
    return True, str(stdout), ""


def _identity_keys(path: str | Path) -> set[str]:
    text = str(path)
    keys = {text, os.path.normpath(text)}
    try:
        keys.add(os.path.realpath(text))
    except OSError:  # pragma: no cover - realpath 幾乎不拋
        pass
    return {key.rstrip("/") or "/" for key in keys}


def list_registered_worktrees(runner: GitRunner) -> tuple[list[str], str | None]:
    """`git worktree list --porcelain` 的 worktree 路徑清單。

    回傳 ``(paths, error)``；``error`` 非 None 代表無法取得清單——呼叫端必須
    視為「後置條件不可證實」而 fail closed，不得當成「沒有殘留」。
    """

    ok, stdout, stderr = _run(runner, ["worktree", "list", "--porcelain"])
    if not ok:
        return [], stderr or "git worktree list failed"
    entries = [
        line[len("worktree ") :].strip()
        for line in stdout.splitlines()
        if line.startswith("worktree ")
    ]
    return entries, None


def _registry_contains(runner: GitRunner, target: Path) -> tuple[bool, str | None]:
    entries, error = list_registered_worktrees(runner)
    if error is not None:
        return False, error
    wanted = _identity_keys(target)
    for entry in entries:
        if _identity_keys(entry) & wanted:
            return True, None
    return False, None


def _looks_like_job_workspace(target: Path) -> bool:
    """這個路徑是不是「本模組該遞迴刪除的 build 工作區」。

    兩種形狀都算：

    - **per-job clone**（#623 之後的模型）：`.git` 是目錄，但帶
      `job_workspace` 的標記檔。
    - **linked worktree**（升級前既存）：`.git` 是**檔案**（內容 `gitdir: ...`）。

    `.git` 是目錄且**沒有**標記檔，代表那是一個主 checkout（獨立 repo，例如 run 的
    `workspace_root`）——那絕不是本模組該遞迴刪除的東西，一律回 False。標記檔而非
    「`.git` 是目錄」當判準，正是為了守住這條邊界：clone 模型下若改用寬鬆判準，
    一筆陳舊的 `job.worktree` 就足以刪掉整個來源 repo。
    """

    return job_workspace.is_job_clone(target) or _looks_like_linked_worktree(target)


def _looks_like_linked_worktree(target: Path) -> bool:
    """linked worktree 的根目錄帶的是 `.git` **檔案**（內容 `gitdir: ...`）。"""

    marker = target / ".git"
    return marker.is_file() and not marker.is_symlink()


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _dirty_entries(runner: GitRunner, target: Path) -> tuple[list[str], str | None]:
    """列出 worktree 內未提交／未追蹤（不含 ignored）的相對路徑。

    透過 ``git -C <worktree>`` 走同一個 seam：git 允許重複 ``-C``，後者為絕對
    路徑時直接生效，因此不必為了讀 worktree 狀態另開一條 runner 契約。

    刻意**不用** ``git status --porcelain``：seam 契約回傳的是已 ``strip()`` 的
    字串，porcelain 的兩字狀態碼首欄可能是空白（`` M README.md``），首筆記錄會
    被 strip 掉一個字元、路徑跟著錯位。改用兩個只吐裸路徑的命令，對 strip 免疫。
    """

    entries: list[str] = []
    ok, stdout, stderr = _run(
        runner, ["-C", str(target), "diff", "--name-only", "-z", "HEAD"]
    )
    if not ok:
        return [], stderr or "git diff failed"
    entries.extend(field for field in stdout.split("\0") if field)
    ok, stdout, stderr = _run(
        runner,
        ["-C", str(target), "ls-files", "--others", "--exclude-standard", "-z"],
    )
    if not ok:
        return [], stderr or "git ls-files failed"
    entries.extend(field for field in stdout.split("\0") if field)
    seen: set[str] = set()
    unique: list[str] = []
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        unique.append(entry)
    return unique, None


def _preserve_dirty_content(
    target: Path,
    entries: list[str],
    *,
    preserve_root: Path,
) -> tuple[str, int]:
    """把 dirty 內容複製到 reclaim 封存目錄，回傳 (封存路徑, 檔數)。"""

    destination = (
        Path(preserve_root)
        / "worktree-reclaim"
        / f"{target.name}-{_timestamp_slug()}-{uuid4().hex[:8]}"
    )
    destination.mkdir(parents=True, exist_ok=False)
    copied = 0
    skipped: list[str] = []
    for relative in entries[:PRESERVE_FILE_MAX_COUNT]:
        source = target / relative
        if source.is_symlink() or not source.is_file():
            continue
        if source.stat().st_size > PRESERVE_FILE_MAX_BYTES:
            skipped.append(relative)
            continue
        copy_to = destination / relative
        copy_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copy_to)
        copied += 1
    overflow = entries[PRESERVE_FILE_MAX_COUNT:]
    if skipped or overflow:
        manifest = destination / "_reclaim-skipped.txt"
        manifest.write_text(
            "\n".join([*skipped, *overflow]) + "\n", encoding="utf-8"
        )
    return str(destination), copied


def reclaim_worktree(
    path: str | Path,
    *,
    git_runner: GitRunner | None = None,
    repo_root: str | Path | None = None,
    preserve_root: str | Path | None = None,
    evidence_model: str = EVIDENCE_PRESERVE,
) -> WorktreeReclaim:
    """原子回收單一 build worktree（目錄 ＋ registry），並驗證後置條件。

    後置條件（兩者皆須成立才回報成功）：

    1. ``git worktree list --porcelain`` 不再含該路徑；
    2. 該路徑在檔案系統上不存在。

    任一條無法證實（含 registry 清單本身讀不到）一律回 ``failed``；呼叫端
    MUST NOT 在 ``failed`` 上回報 recovery 成功——這正是 #478 的核心缺陷。

    ``evidence_model``（#658）決定「不銷毀證據」怎麼落實，兩個合法值的語意與
    適用條件見模組 docstring。**不接受未知值**：那代表呼叫端沒有真的表態，而
    靜默退回預設會讓一次本該 preserve 的回收看起來像是刻意跳過。
    """

    if evidence_model not in _EVIDENCE_MODELS:
        raise ValueError(f"unknown worktree reclaim evidence model: {evidence_model!r}")
    runner = resolve_git_runner(git_runner, repo_root=repo_root)
    target = Path(path)
    text = str(target)

    registered, list_error = _registry_contains(runner, target)
    if list_error is not None:
        return WorktreeReclaim(
            RECLAIM_FAILED,
            text,
            evidence_model=evidence_model,
            detail=f"worktree-registry-unreadable: {list_error}",
        )

    exists = target.exists() or target.is_symlink()
    if not registered and not exists:
        return WorktreeReclaim(RECLAIM_ABSENT, text, evidence_model=evidence_model)

    # 安全閘（先於任何寫入／掃描）：registry 沒這筆、目錄本身也沒有
    # linked-worktree 標記，代表這個路徑不是（也不曾是）build worktree——
    # job／slice 記錄可能陳舊或指錯（實測 `job.worktree` 會等於 run 的
    # `workspace_root`，那是主 checkout），遞迴刪除的爆炸半徑不可接受。
    # 回報 failed 讓 operator 看見異常，而不是靜默刪掉別人的目錄，也不是
    # 靜默留下會擋住下一次 `worktree add` 的殘骸。
    if (
        exists
        and not registered
        and not target.is_symlink()
        and target.is_dir()
        and not _looks_like_job_workspace(target)
    ):
        return WorktreeReclaim(
            RECLAIM_FAILED,
            text,
            evidence_model=evidence_model,
            detail="worktree-path-not-a-worktree",
        )

    # #623：clone 模型下 `rmtree` 會連 object store 一起刪掉——worktree 模型下這些
    # commit 在共用 store 裡、branch 也還在主 repo，回收不銷毀任何東西。為了維持本
    # 模組契約的「不銷毀證據」，刪除前先把工作區 HEAD 拉進來源 repo 的封存命名空間
    # （已在來源 repo 裡的 commit 不重複封存）。封存本身是加分項，失敗不阻斷回收
    # ——回收失敗才是 #478／#601 的生產事故。
    #
    # #658：這一段在**兩種 evidence 模型下都跑**。`harvested` 模型下它正常是 no-op
    # （commit 已在來源樹裡 ⇒ 直接回 None），但呼叫端的前提萬一不成立，它就是把那顆
    # commit 救回來的安全網——正因為如此，`harvested` 略過的只有 preserve 那一段。
    archived_ref: str | None = None
    if exists and not target.is_symlink() and job_workspace.is_job_clone(target):
        # 來源 repo 優先取呼叫端給的值；沒給時取標記檔記錄的 provision 來源
        # ——既有呼叫端（`manager.apply_slice_action` 的 recover-pre-candidate、
        # `work_actions` 的 abandon 回收）都不傳 `repo_root`，硬要求它會讓封存
        # 在生產路徑上永遠不觸發。
        marker = job_workspace.read_marker(target) or {}
        source = repo_root or marker.get("source_repo")
        if isinstance(source, (str, Path)) and str(source):
            try:
                archived_ref = job_workspace.archive_workspace_head(
                    source_repo=source, workspace=target
                )
            except Exception as exc:  # noqa: BLE001 - 封存失敗只記錄，不阻斷回收
                logger.warning(
                    "worktree-reclaim-archive-unavailable path=%s error=%s", text, exc
                )

    preserved_ref: str | None = None
    preserved_files = 0
    if (
        evidence_model == EVIDENCE_PRESERVE
        and exists
        and not target.is_symlink()
        and target.is_dir()
    ):
        entries, dirty_error = _dirty_entries(runner, target)
        if dirty_error is not None:
            # gitdir 已壞（正是 #478 的殘留態）時 status 讀不到；此時目錄要嘛
            # 不存在、要嘛只剩無主檔案，記錄後照常回收，不阻斷自癒。
            logger.warning(
                "worktree-reclaim-dirty-scan-unavailable path=%s error=%s",
                text,
                dirty_error,
            )
        elif entries:
            root = Path(preserve_root) if preserve_root is not None else (
                paths.coordinator_root() / "evidence"
            )
            try:
                preserved_ref, preserved_files = _preserve_dirty_content(
                    target, entries, preserve_root=root
                )
            except OSError as exc:
                # 證據沒保住就一個位元組都不刪——#478 的 `.project-policy.yml`
                # 資料遺失回報要求「preserve or fail closed」，不得兩者皆非。
                return WorktreeReclaim(
                    RECLAIM_FAILED,
                    text,
                    registry_entry_found=registered,
                    evidence_model=evidence_model,
                    detail=(
                        "worktree-dirty-preserve-failed: "
                        f"{type(exc).__name__}: {str(exc)[:200]}"
                    ),
                )

    registry_removed = False
    remove_error: str | None = None
    if registered:
        ok, _, stderr = _run(runner, ["worktree", "remove", "--force", text])
        if not ok:
            remove_error = stderr or "git worktree remove failed"
            # `--force` 拒收時（例如 gitdir 檔已被外力刪除）再試 prune，
            # 這是 native `git worktree prune` 的等價操作。
            _run(runner, ["worktree", "prune"])
        still_registered, list_error = _registry_contains(runner, target)
        if list_error is not None:
            return WorktreeReclaim(
                RECLAIM_FAILED,
                text,
                registry_entry_found=True,
                preserved_ref=preserved_ref,
                preserved_files=preserved_files,
                evidence_model=evidence_model,
                detail=f"worktree-registry-unreadable: {list_error}",
            )
        if still_registered:
            return WorktreeReclaim(
                RECLAIM_FAILED,
                text,
                registry_entry_found=True,
                preserved_ref=preserved_ref,
                preserved_files=preserved_files,
                evidence_model=evidence_model,
                detail=(
                    "worktree-registry-entry-remains: "
                    f"{remove_error or 'git worktree remove reported success'}"
                ),
            )
        registry_removed = True

    directory_removed = False
    if target.exists() or target.is_symlink():
        is_link_or_file = target.is_symlink() or target.is_file()
        try:
            if is_link_or_file:
                target.unlink()
            else:
                shutil.rmtree(target)
        except OSError as exc:
            return WorktreeReclaim(
                RECLAIM_FAILED,
                text,
                registry_entry_found=registered,
                registry_removed=registry_removed,
                preserved_ref=preserved_ref,
                preserved_files=preserved_files,
                evidence_model=evidence_model,
                detail=f"worktree-directory-remove-failed: {type(exc).__name__}: {str(exc)[:200]}",
            )
        directory_removed = True

    if target.exists() or target.is_symlink():
        return WorktreeReclaim(
            RECLAIM_FAILED,
            text,
            registry_entry_found=registered,
            registry_removed=registry_removed,
            preserved_ref=preserved_ref,
            preserved_files=preserved_files,
            evidence_model=evidence_model,
            detail="worktree-directory-remains",
        )

    return WorktreeReclaim(
        RECLAIM_RECLAIMED,
        text,
        registry_entry_found=registered,
        registry_removed=registry_removed,
        directory_removed=directory_removed,
        preserved_ref=preserved_ref,
        preserved_files=preserved_files,
        archived_ref=archived_ref,
        evidence_model=evidence_model,
    )


def reclaim_recorded_or_derived(
    *,
    recorded_path: str | Path | None = None,
    pool_root: str | Path | None = None,
    job_id: str | None = None,
    branch: str | None = None,
    git_runner: GitRunner | None = None,
    repo_root: str | Path | None = None,
    preserve_root: str | Path | None = None,
    evidence_model: str = EVIDENCE_PRESERVE,
) -> WorktreeReclaim | None:
    """回收某個 slice 的 build 工作區——記錄有路徑就用它，沒有才反推。

    `recover-pre-candidate` 有兩處實作（`manager.apply_slice_action` 與
    `work_actions`），兩處都得回答同一個問題：「這條 slice 的工作區在哪」。收斂成
    本函式，兩邊就不會在 #645 換名之後各自更新一半。

    - **記錄有 `worktree`** → 逐字回收那一條，行為與 #645 之前完全相同。
    - **記錄沒有** → 由 `job_workspace.reclaim_candidate_paths()` 反推候選：#645 之後
      的 `<pool>/<job_segment(job_id)>` 與 #645 之前的 `<pool>/<branch slug>`。
      **兩種形狀都要試**，否則升級當下磁碟上的舊目錄會被當成「不存在」而略過，
      下一次 provision 直接撞 `worktree target already exists`（#601 的現場）。

    回傳挑選規則：任一筆 `failed` → 回那一筆（呼叫端據此 fail closed）；否則有
    實際回收到的 → 回那一筆；否則回第一筆（`absent`）；完全沒有候選 → None。
    **不認得的目錄一律由 `reclaim_worktree()` 的安全閘擋下（回 failed），本函式不
    會、也不得靜默刪除任何形狀不明的目錄。**
    """

    targets: list[Path] = []
    if recorded_path and isinstance(recorded_path, (str, Path)):
        targets = [Path(recorded_path)]
    elif pool_root is not None:
        targets = job_workspace.reclaim_candidate_paths(
            pool_root, job_id=job_id, branch=branch
        )
    if not targets:
        return None
    results = reclaim_worktrees(
        list(targets),
        git_runner=git_runner,
        repo_root=repo_root,
        preserve_root=preserve_root,
        evidence_model=evidence_model,
    )
    if not results:
        return None
    for result in results:
        if result.status == RECLAIM_FAILED:
            return result
    for result in results:
        if result.status == RECLAIM_RECLAIMED:
            return result
    return results[0]


def reclaim_worktrees(
    worktrees: list[str | Path],
    *,
    git_runner: GitRunner | None = None,
    repo_root: str | Path | None = None,
    preserve_root: str | Path | None = None,
    evidence_model: str = EVIDENCE_PRESERVE,
) -> list[WorktreeReclaim]:
    """對多個 worktree 逐一回收（去重、保序）；不因單筆失敗而中止。"""

    seen: set[str] = set()
    results: list[WorktreeReclaim] = []
    for item in worktrees:
        text = str(item)
        if not text or text in seen:
            continue
        seen.add(text)
        results.append(
            reclaim_worktree(
                text,
                git_runner=git_runner,
                repo_root=repo_root,
                preserve_root=preserve_root,
                evidence_model=evidence_model,
            )
        )
    return results
