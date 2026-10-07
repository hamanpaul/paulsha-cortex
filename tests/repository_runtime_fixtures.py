"""#1124：「跑過 Manager job 的來源樹」真 git fixture。

installer 的 repository step（`trust_root/install/backend.py`）以 prior receipt 升級時，
來源樹已經帶著 Manager 執行期寫進去的狀態。本模組只重現**程式碼實際會寫**的那幾種
（出處逐條列在 backend 的 `_RUNTIME_*` allowlist docstring），讓 backend 與 transaction
兩層測試共用同一份形狀，而不是各自手拼、各自漂移。

每一條 git 命令都跑在 `_REPOSITORY_GIT_ENV` 底下（無 global／system config、無 HOME），
測試機的 gitconfig 因此影響不到 fixture。
"""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from paulsha_cortex.trust_root.install import backend as backend_module
from paulsha_cortex.trust_root.install.backend import LocalInstallBackend
from paulsha_cortex.trust_root.install.core import _desired_digest

REMOTE = "https://github.com/hamanpaul/paulsha-cortex.git"
#: `coordinator/review.py:227`：`<repo>/.psc-review-worktrees/<slice_id>-<reviewer job>`。
REVIEW_WORKTREE = ".psc-review-worktrees/s1124-review-1124"
#: `coordinator/seams.py:249` 以 `branch -f` 在來源樹 provision 的 build branch。
BUILD_BRANCH = "feature/1124-repository-runtime"

_GIT_ENV = {
    **backend_module._REPOSITORY_GIT_ENV,
    "GIT_AUTHOR_NAME": "Cortex Test",
    "GIT_AUTHOR_EMAIL": "cortex@example.invalid",
    "GIT_COMMITTER_NAME": "Cortex Test",
    "GIT_COMMITTER_EMAIL": "cortex@example.invalid",
}


def git(*argv: str) -> str:
    return subprocess.run(
        ["git", *argv],
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    ).stdout.strip()


def refs(repository: Path) -> list[str]:
    output = git(
        "-C", str(repository), "for-each-ref", "--format=%(refname) %(objectname)"
    )
    return output.splitlines()


@dataclass
class RepositoryUpgrade:
    source: Path
    bundle: Path
    repository: Path
    old_commit: str
    new_commit: str
    owner: str
    group: str

    def step(self, commit: str) -> dict[str, object]:
        step: dict[str, object] = {
            "step_id": "repository:paulsha-cortex",
            "kind": "repository",
            "slug": "paulsha-cortex",
            "source": str(self.bundle),
            "source_sha256": backend_module._sha256_file(self.bundle),
            "commit": commit,
            "remote": REMOTE,
            "path": str(self.repository),
            "owner": self.owner,
            "group": self.group,
            "mode": "0755",
            "durable": True,
            "operations": ["snapshot", "clone-bundle", "checkout", "chown"],
        }
        step["desired_sha256"] = _desired_digest(step)
        return step

    @property
    def old_step(self) -> dict[str, object]:
        return self.step(self.old_commit)

    @property
    def new_step(self) -> dict[str, object]:
        return self.step(self.new_commit)


def repository_upgrade(
    tmp_path: Path, *, candidate_files: dict[str, str] | None = None
) -> RepositoryUpgrade:
    """兩個 commit 的 bundle，並以真 backend 安裝舊的那一個（prior receipt 的狀態）。"""

    source = tmp_path / "source"
    source.mkdir()
    git("init", "--quiet", str(source))
    readme = source / "README.md"
    readme.write_text("old\n", encoding="utf-8")
    git("-C", str(source), "add", "README.md")
    git("-C", str(source), "commit", "--quiet", "-m", "old")
    old_commit = git("-C", str(source), "rev-parse", "HEAD")
    readme.write_text("new\n", encoding="utf-8")
    for relative, content in (candidate_files or {}).items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git("-C", str(source), "add", "--all")
    git("-C", str(source), "commit", "--quiet", "-m", "new")
    new_commit = git("-C", str(source), "rev-parse", "HEAD")
    bundle = tmp_path / "source.bundle"
    git("-C", str(source), "bundle", "create", str(bundle), "HEAD")
    repository = tmp_path / "repos" / "paulsha-cortex"
    repository.parent.mkdir()
    upgrade = RepositoryUpgrade(
        source=source,
        bundle=bundle,
        repository=repository,
        old_commit=old_commit,
        new_commit=new_commit,
        owner=pwd.getpwuid(os.getuid()).pw_name,
        group=grp.getgrgid(os.getgid()).gr_name,
    )
    LocalInstallBackend(require_root=False).apply_step(upgrade.old_step)
    return upgrade


def simulate_manager_runtime(upgrade: RepositoryUpgrade) -> None:
    """重現 Manager 執行期對來源樹的寫入（每一條對應 production 出處）。"""

    repository = str(upgrade.repository)
    old = upgrade.old_commit
    # seams.py:351-355 從來源樹 local config 讀 builder 的 commit identity；
    # qualification/driver.py:3126-3142 以 Manager 身分把它寫進來源 checkout。
    git("-C", repository, "config", "--local", "user.name", "Cortex Test")
    git("-C", repository, "config", "--local", "user.email", "cortex@example.invalid")
    # seams.py:249：provision 在來源樹 `branch -f <branch> <exact base>`。
    git("-C", repository, "branch", "-f", BUILD_BRANCH, old)
    # job_workspace.py:1224-1225：回收前把工作區 HEAD fetch 進封存命名空間
    # （同一條 fetch 也寫 FETCH_HEAD 與新 object）。
    git(
        "-C",
        repository,
        "fetch",
        "--no-tags",
        str(upgrade.bundle),
        f"HEAD:refs/cortex/reclaimed/job-1124-abcdef01/20260929T000000Z-{upgrade.new_commit[:12]}",
    )
    # work_bridge.py:1397-1404 retry pin；manager.py:1854 等 `fetch origin <branch>`
    # 以 canonical refspec 順帶更新的 remote-tracking ref。
    git("-C", repository, "update-ref", "refs/cortex/main-sync/run-1124", old)
    git("-C", repository, "update-ref", "refs/remotes/origin/main", old)
    # work_actions.py：回收 build branch 前的 refs/archive 保存 ref。
    git("-C", repository, "update-ref", f"refs/archive/repository-runtime-{old[:8]}", old)
    # Legacy tags stay accepted for already-installed source trees.
    git("-C", repository, "tag", f"archive/repository-runtime-{old[:8]}", old)
    # review.py:510：foreign review 的 linked worktree 開在來源樹內。
    git(
        "-C",
        repository,
        "worktree",
        "add",
        "--detach",
        str(upgrade.repository / REVIEW_WORKTREE),
        old,
    )
    # verification.py:1131／1201 與 review.py:505-508：清理中途崩潰留下的
    # prunable registry（目錄已刪、`.git/worktrees/<id>` 還在）。
    stale = upgrade.repository / ".psc-verification-worktrees" / f"s1124-{old[:12]}"
    git("-C", repository, "worktree", "add", "--detach", str(stale), old)
    shutil.rmtree(stale)
