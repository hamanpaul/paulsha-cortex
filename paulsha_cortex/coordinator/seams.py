from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from paulsha_cortex.config import paths

from . import job_workspace


@runtime_checkable
class PaneSender(Protocol):
    """把一行命令送進 tmux pane 的 seam。"""

    def send(self, pane_id: str, text: str) -> None: ...


@runtime_checkable
class WorktreeCreator(Protocol):
    """為某 job 建立 per-job git 工作區、回傳其路徑的 seam。

    協定名沿用 ``WorktreeCreator``（以及 job 記錄的 ``worktree`` 欄位）：#623 之後
    實作已改為 per-job 完整 clone，但這兩個名字是**已持久化的契約**——job registry
    的既有列、以及注入 fake 的大量既有測試都靠它。改名不會讓任何東西更正確，只會
    製造一次不必要的資料遷移。實際的工作區模型見 `coordinator/job_workspace.py`。

    ``job_id`` 是**必填**（#645）：目錄名由它經 `job_workspace.job_segment()` 導出，
    而那同時是 systemd 模板的 instance 名。留一個預設值等於留一條「忘了傳就退回舊
    命名」的路，那正是 #645 的復發面。``branch`` 仍是 branch——它決定 clone 出來
    checkout 哪一條，**不再**決定目錄叫什麼。
    """

    def create(self, branch: str, *, job_id: str, base_sha: str | None = None) -> str: ...


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
    """真實作：per-job **完整 clone** 的工作區 provisioning（#623）。

    #623 之前這裡是 `git worktree add`。trust-root Phase 2b 三分 UID 上線後那個模型
    結構性不成立——builder 要 commit 就必須寫**共用** object store，而能寫共用
    object store，隔離邊界就在 git 這一層漏掉（完整推導見
    `coordinator/job_workspace.py` 模組 docstring）。改為每個 job 一份自己的 clone。

    **守衛與 worktree 模型逐條等價**（錯誤訊息刻意逐字保留，既有診斷／測試不因
    實作換代而失效）：

    ================================  ==========================================
    守衛                              clone 模型下的作法
    ================================  ==========================================
    target 已存在                     同——先探測後 fail-closed
    base 必須是既有 commit            同——`rev-parse --verify <base>^{commit}`
    既有 branch 必須位於 base          同——`merge-base --is-ancestor`，非祖先即拒
      ancestry
    既有 branch fast-forward 後重掛    在**來源 repo** `branch -f` 後 clone 出來
    新 branch                         在**來源 repo** 建 branch 後 clone 出來
    ================================  ==========================================

    branch 仍錨定在來源 repo（而非只存在於 clone 裡）的理由有三：`gc` 與
    `dispatcher`／`autonomy` 的 dispatch baseline 都直接讀來源 repo 的
    `refs/heads/<branch>`；ancestry 守衛需要一個跨世代穩定的比較對象（#613）；而
    成果回收本來就會把 branch fetch 回同一個位置。

    clone 完成後的工作區狀態，與 worktree 模型下逐字相同：`origin` 指向**真正的
    上游**（來源 repo 的 `origin` URL），指向來源 repo 的暫時 remote 一律移除，
    `<branch>` 沒有 upstream（`worktree add -b` 也不設），來源 repo 的
    `refs/remotes/origin/*` 與本地 `user.name`／`user.email` 一併複製過去
    （clone 不繼承來源的 local config，少了它 builder 的 `git commit` 會直接失敗）。

    任何一步失敗都會把**已做的變更全部還原**（部分 clone 目錄刪除、branch 回到
    provision 前的位置或刪除）——殘留正是 #601／#613 的生產現場。

    **目錄名（#645）**：`<worktree_root>/<job_workspace.job_segment(job_id)>`。
    #645 之前是 `<worktree_root>/<branch.replace("/", "-")>`，與模板 unit 期望的
    `<worktree_root>/%i` 永遠差一個 `feature-` 前綴，systemd 建 mount namespace 直接
    失敗（`226/NAMESPACE`）。branch 名不變，只有磁碟上的目錄名改。

    **#633：解析時機（repo／worktree pool 一律 lazy）**。#612／#630 讓
    `paths.repo_root()` 在未宣告 `PSC_REPO_ROOT` 時 fail-closed——方向正確，但這裡
    舊實作在 `__init__` 就解析，而 `manager_daemon.run_loop → ensure_dispatcher()`
    會在**建 dispatcher 時**實體化本類。於是「env 少一個變數」的後果不是「派不了
    工」而是 **Manager 啟動即崩**，`Restart=on-failure` 再把它變成 crash-loop
    （實機 `NRestarts` 連跳 7 次）——一台什麼都做不了、也什麼都不告訴你的機器。

    改法只動**時機**、不動**性質**：repo 與 worktree pool 改為第一次真正要用時才
    解析（`_repo` / `_wt_root` 兩個 property，解析結果 memoize）。因此

    * 沒有宣告目標 repo 的 Manager **起得來**，其餘職責（tick、monitor、狀態回報、
      降級運轉）照常——這與 `PSC_DEGRADED_OPERATION` 的精神一致：能做的繼續做。
    * 第一次 `create()`（＝真的要在磁碟上開一棵樹）仍然 `RepoRootUnresolvedError`
      **原樣**拋出，訊息逐字不變，只是出現在**派工當下**而不是啟動當下——那也正是
      operator 看得懂它的時刻。
    * fail-closed 一個位元組都沒放寬：沒有新增任何 cwd 退路，也沒有把例外吞掉。
      唯一「不拋」的新入口是 :meth:`anchored_at`，而它回的是 `False`（＝不能用），
      不是一個猜出來的路徑。

    單元測試 MUST 注入 fake，不實體化此類。
    """

    def __init__(
        self,
        repo: str | Path | None = None,
        wt_root: str | Path | None = None,
        base: str = "main",
    ) -> None:
        #: #633：**未給定時不在建構子解析**——只記下「沒有顯式值」，第一次真正要
        #: 用時才問 `paths`。理由見類別 docstring 的「#633：解析時機」段。
        self._repo_resolved: Path | None = None if repo is None else Path(repo)
        self._wt_root_resolved: Path | None = None if wt_root is None else Path(wt_root)
        self._base = base

    # -- #633：lazy 解析（解析結果 memoize，一個 creator 一棵樹） -----------------

    @property
    def _repo(self) -> Path:
        """來源 repo 根。未顯式給定時第一次存取才解析，且**解析失敗即拋**。

        memoize 是刻意的：舊實作在建構子解析一次、其後這個值就是凍結的，全部既有
        呼叫端（含 `_source()` 每一次 git 呼叫）都建立在「同一個 creator 永遠對同一
        棵樹動手」之上。lazy 只改**第一次**解析發生的時機，不讓 env 在 creator 存活
        期間改變它指向哪裡。
        """
        if self._repo_resolved is None:
            self._repo_resolved = Path(paths.repo_root())
        return self._repo_resolved

    @property
    def _wt_root(self) -> Path:
        """worktree pool 根。同上——未顯式給定時第一次存取才解析並 memoize。

        `paths.worktree_root()` 內部也會走 `repo_root()`，因此它與 `_repo` 是同一條
        fail-closed；分開 memoize 只是為了讓「顯式給 wt_root、repo 交給 env」這個
        既有組合（`__init__` 兩個參數本來就各自獨立）維持原語意。
        """
        if self._wt_root_resolved is None:
            self._wt_root_resolved = Path(paths.worktree_root())
        return self._wt_root_resolved

    @property
    def repo_root(self) -> Path:
        return self._repo

    def anchored_at(self, root: str | Path) -> bool:
        """本 creator 是否錨定在 `root` 這棵樹上。

        #633：repo 解析改 lazy 之後，「尚未解析且環境沒有宣告」是一個**合法狀態**
        （Manager 在未宣告 `PSC_REPO_ROOT` 時仍起得來）。呼叫端問的是「這個 creator
        能不能拿來對 `root` 派工」，而一個解析不出來的 creator 對任何具體的 `root`
        都答不出「是」——因此回 `False`，讓呼叫端走它既有的「換一個錨定正確的
        creator」分支，而不是把 `RepoRootUnresolvedError` 從一句比較裡漏出去。
        fail-closed 沒有被放寬：真的要動手時 `create()` 仍會解析、仍會拋。
        """
        try:
            mine = self._repo
        except paths.RepoRootUnresolvedError:
            return False
        return str(mine.resolve()) == str(Path(root).resolve())

    # -- git 小工具（皆對來源 repo 或工作區執行，回傳 CompletedProcess） ---------

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], check=False, capture_output=True, text=True)

    def _source(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self._run(["-C", str(self._repo), *args])

    def create(self, branch: str, *, job_id: str, base_sha: str | None = None) -> str:
        #: #645：目錄名由 **job id** 導出，不再是 branch slug。降權派工的模板 unit
        #: 只有 `%i` 可用（`ReadWritePaths=<pool>/%i`），推不出 branch slug，因此
        #: 兩個名字要對齊，只能讓目錄名這一側讓步。推導點只有
        #: `job_workspace.job_segment()` 一個——`job_runner.template_instance_id()`
        #: 走的是同一個函式，兩者因此不是「剛好相等」而是**同一個字串**。
        target = job_workspace.workspace_path(self._wt_root, job_id)
        self._wt_root.mkdir(parents=True, exist_ok=True)
        base = base_sha or self._base
        #: provision 前的 branch 位置：None＝當時不存在。失敗時據此還原。
        previous_branch_sha: str | None = None
        branch_touched = False
        #: 只有**本次呼叫親手建立**的 target 才可以在還原時刪除。撞到既有目錄時
        #: 這個旗標必為 False——否則 "target already exists" 這條守衛會從
        #: fail-closed 變成「刪掉別人的工作區再說」。
        target_created = False
        try:
            if target.exists() or target.is_symlink():
                raise ValueError("worktree target already exists")
            base_probe = self._source(["rev-parse", "--verify", f"{base}^{{commit}}"])
            if base_probe.returncode != 0:
                raise ValueError(f"git worktree base invalid: {base_probe.stderr.strip()}")
            exact_base = base_probe.stdout.strip()
            branch_probe = self._source(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
            )
            if branch_probe.returncode not in {0, 1}:
                raise ValueError(f"git branch probe failed: {branch_probe.stderr.strip()}")
            if branch_probe.returncode == 0:
                ancestor = self._source(
                    ["merge-base", "--is-ancestor", branch, exact_base]
                )
                if ancestor.returncode == 1:
                    raise ValueError("existing worktree branch has commits outside requested base")
                if ancestor.returncode != 0:
                    raise ValueError(
                        f"git branch ancestry check failed: {ancestor.stderr.strip()}"
                    )
                previous_branch_sha = self._source(
                    ["rev-parse", f"refs/heads/{branch}"]
                ).stdout.strip() or None
            branch_touched = True
            moved = self._source(["branch", "-f", branch, exact_base])
            if moved.returncode != 0:
                branch_touched = False
                raise ValueError(f"git worktree add failed: {moved.stderr.strip()}")
            target_created = True
            self._clone(branch=branch, target=target, exact_base=exact_base)
        except ValueError:
            self._rollback(
                branch=branch,
                target=target,
                previous_branch_sha=previous_branch_sha,
                branch_touched=branch_touched,
                target_created=target_created,
            )
            raise
        except FileNotFoundError as exc:
            self._rollback(
                branch=branch,
                target=target,
                previous_branch_sha=previous_branch_sha,
                branch_touched=branch_touched,
                target_created=target_created,
            )
            raise ValueError("git not found") from exc
        return str(target)

    # -- clone 與善後 -----------------------------------------------------------

    def _clone(self, *, branch: str, target: Path, exact_base: str) -> None:
        cloned = self._run(
            [
                "clone",
                "--quiet",
                # hardlink 會讓 clone 的 object 與來源 repo 共用 inode——那正是本次
                # 變更要消滅的共用面（也是 operator 實測採用的旗標）。
                "--no-hardlinks",
                "--origin",
                job_workspace.SOURCE_REMOTE,
                "--branch",
                branch,
                "--",
                str(self._repo),
                str(target),
            ]
        )
        if cloned.returncode != 0:
            raise ValueError(f"git worktree add failed: {cloned.stderr.strip()}")

        origin_url = self._source(["remote", "get-url", "origin"])
        upstream = origin_url.stdout.strip() if origin_url.returncode == 0 else ""

        if upstream:
            # 來源 repo 的 remote-tracking refs 一併鏡射過去：worktree 模型下
            # `git -C <工作區> rev-parse origin/main` 是共用 ref，直接可讀；clone
            # 只會拿到 `refs/heads/*`，少了這一步模型端的 `git log origin/main..`
            # 之類命令會突然失效。
            mirrored = self._run(
                [
                    "-C",
                    str(target),
                    "fetch",
                    "--no-tags",
                    job_workspace.SOURCE_REMOTE,
                    "+refs/remotes/origin/*:refs/remotes/origin/*",
                ]
            )
            if mirrored.returncode != 0:
                raise ValueError(
                    f"git worktree add failed: {mirrored.stderr.strip()}"
                )

        removed = self._run(["-C", str(target), "remote", "remove", job_workspace.SOURCE_REMOTE])
        if removed.returncode != 0:
            raise ValueError(f"git worktree add failed: {removed.stderr.strip()}")

        if upstream:
            added = self._run(["-C", str(target), "remote", "add", "origin", upstream])
            if added.returncode != 0:
                raise ValueError(f"git worktree add failed: {added.stderr.strip()}")

        # `--branch` 會把 upstream 設到暫時 remote 上；`remote remove` 之後那組
        # config 可能仍在。worktree 模型下 `worktree add -b` 不設 upstream，這裡
        # 清掉以維持等價（unset 不存在的 key 回 5，非錯誤）。
        for key in (f"branch.{branch}.remote", f"branch.{branch}.merge"):
            self._run(["-C", str(target), "config", "--unset-all", key])

        # clone **不繼承**來源 repo 的 local config。identity 缺席時 builder 的
        # `git commit` 會直接失敗，而 worktree 模型下它是共用的——複製過去。
        for key in ("user.name", "user.email"):
            probe = self._source(["config", "--local", "--get", key])
            value = probe.stdout.strip() if probe.returncode == 0 else ""
            if value:
                self._run(["-C", str(target), "config", key, value])

        # 成果 bundle 的 `^<base>` 錨點。bundle 要能被來源樹 fetch，它的
        # prerequisite 就必須是**來源樹已有**的 commit——`exact_base` 正是來源 repo
        # 自己 `rev-parse --verify` 出來的，所以這條性質由 provisioning 這個單一
        # 推導點對**每一條 lane** 一致成立。pin 成 clone 內的一個 ref 是因為產
        # bundle 的是 builder：它讀得到自己的 clone，讀不到 spool 也讀不到 Manager
        # 的任何狀態（見 `job_workspace.BASE_REF`）。
        pinned = self._run(
            ["-C", str(target), "update-ref", job_workspace.BASE_REF, exact_base]
        )
        if pinned.returncode != 0:
            raise ValueError(f"git worktree add failed: {pinned.stderr.strip()}")

        job_workspace.write_marker(
            target, branch=branch, base=exact_base, source_repo=self._repo
        )

    def _rollback(
        self,
        *,
        branch: str,
        target: Path,
        previous_branch_sha: str | None,
        branch_touched: bool,
        target_created: bool,
    ) -> None:
        """把 provision 途中已做的變更全部還原（best-effort，不覆蓋原始錯誤）。"""

        if target_created:
            try:
                if target.is_dir() and not target.is_symlink():
                    job_workspace.remove_clone(target)
            except Exception:  # noqa: BLE001 - 還原失敗不得蓋掉原始 provision 錯誤
                pass
        if not branch_touched:
            return
        try:
            if previous_branch_sha:
                self._source(["branch", "-f", branch, previous_branch_sha])
            else:
                self._source(["branch", "-D", branch])
        except Exception:  # noqa: BLE001 - 同上
            pass
