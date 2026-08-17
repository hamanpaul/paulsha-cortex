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
    """為某分支建立 per-job git 工作區、回傳其路徑的 seam。

    協定名沿用 ``WorktreeCreator``（以及 job 記錄的 ``worktree`` 欄位）：#623 之後
    實作已改為 per-job 完整 clone，但這兩個名字是**已持久化的契約**——job registry
    的既有列、以及注入 fake 的大量既有測試都靠它。改名不會讓任何東西更正確，只會
    製造一次不必要的資料遷移。實際的工作區模型見 `coordinator/job_workspace.py`。
    """

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

    單元測試 MUST 注入 fake，不實體化此類。
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

    @property
    def repo_root(self) -> Path:
        return self._repo

    # -- git 小工具（皆對來源 repo 或工作區執行，回傳 CompletedProcess） ---------

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], check=False, capture_output=True, text=True)

    def _source(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self._run(["-C", str(self._repo), *args])

    def create(self, branch: str, *, base_sha: str | None = None) -> str:
        slug = branch.replace("/", "-")
        target = self._wt_root / slug
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
