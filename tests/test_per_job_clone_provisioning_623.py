"""#623：工作區模型由 `git worktree` 改為 per-job 完整 clone。

覆蓋四段，皆以**真 git repo** 驗證（注入假 runner 只會驗到 stub 自己）：

1. provision——守衛與 worktree 模型逐條等價，且失敗不留殘留
2. 隔離——工作區裡沒有任何回寫來源 repo 的路徑；未回收前來源 repo 不受影響
3. 成果回收——Manager 單向取回自己的樹（**搬運介面是 bundle**，見
   `tests/test_bundle_commit_harvest_623.py`：Manager 不得存取 builder 的 clone）
4. 回收——clone 走目錄刪除、封存 HEAD；linked worktree（升級前既存）仍走
   `git worktree remove`；主 checkout 一律拒絕
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import gc, job_workspace, manager, verification, worktree_reclaim
from paulsha_cortex.coordinator.seams import ScriptWorktreeCreator

_BRANCH = "feature/623-per-job-clone"
_JOB_ID = "623-per-job-clone"
#: #645：工作區目錄名由 job id 導出（不再是 branch slug）。
_SEGMENT = job_workspace.job_segment(_JOB_ID)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _try_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=False, capture_output=True, text=True
    )


def _source_repo(tmp_path: Path, *, with_upstream: bool = True) -> Path:
    """Manager-owned 的來源 working checkout（不是 bare——monitor 要掃工作樹）。"""

    repo = tmp_path / "repos" / "paulsha-cortex"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    _git(repo, "config", "user.email", "manager@example.invalid")
    _git(repo, "config", "user.name", "Cortex Manager")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    if with_upstream:
        upstream = tmp_path / "upstream.git"
        subprocess.run(
            ["git", "init", "-q", "-b", "main", "--bare", str(upstream)], check=True
        )
        _git(repo, "remote", "add", "origin", str(upstream))
        _git(repo, "push", "-q", "origin", "main")
        _git(repo, "fetch", "-q", "origin")
    return repo


def _creator(repo: Path, pool: Path) -> ScriptWorktreeCreator:
    return ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main")


def _builder_commit(workspace: Path, name: str = "builder.txt") -> str:
    """模擬 builder 在自己的 clone 裡做一次交付。"""

    (workspace / name).write_text("builder work\n", encoding="utf-8")
    _git(workspace, "add", name)
    _git(workspace, "commit", "-qm", f"builder: {name}")
    return _git(workspace, "rev-parse", "HEAD")


def _produce_bundle(workspace: Path, spool_root: Path, *, key: str = "job-1") -> Path:
    """以 **builder 身分**跑 production wrapper 裡那一段 bundle 命令，回傳落地的 bundle。

    刻意不在測試裡自己組 `git bundle create`——另寫一份只會驗到測試自己。
    """

    bundle = job_workspace.prepare_commit_spool(spool_key=key, coordinator_root=spool_root)
    subprocess.run(
        ["bash", "-c", job_workspace.build_bundle_command(workspace=workspace, bundle=bundle)],
        cwd=str(workspace), check=True, capture_output=True, text=True,
    )
    return bundle


def _harvest(
    repo: Path, workspace: Path, spool_root: Path, *, branch: str = _BRANCH, key: str = "job-1"
) -> str:
    """builder 產 bundle → Manager 從那個檔案回收（成果離開 job 帳號的唯一路徑）。"""

    bundle = _produce_bundle(workspace, spool_root, key=key)
    return job_workspace.harvest_branch(source_repo=repo, bundle=bundle, branch=branch)


# ---------------------------------------------------------------------------
# 1. provision
# ---------------------------------------------------------------------------

def test_provisioned_workspace_is_a_standalone_clone_not_a_linked_worktree(
    tmp_path: Path,
) -> None:
    """核心行為變更：工作區有**自己的** object store。

    linked worktree 的 `.git` 是指標檔、object store 共用——那正是 #623 證明與三分
    隔離互斥的結構。clone 之後 `.git` 是目錄，且來源 repo 的 worktree registry 裡
    完全沒有這一筆。
    """

    repo = _source_repo(tmp_path)
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))

    assert (workspace / ".git").is_dir()
    assert not (workspace / ".git").is_file()
    assert job_workspace.is_job_clone(workspace)
    assert str(workspace) not in _git(repo, "worktree", "list", "--porcelain")
    # 工作區一出生就必須是 clean——標記檔刻意放在 `.git/` 底下，不進工作樹。
    assert _git(workspace, "status", "--porcelain", "--untracked-files=all") == ""


def test_provision_reuses_existing_branch_only_when_it_is_base_ancestor(
    tmp_path: Path,
) -> None:
    """既有 branch 完全位於 base ancestry 時 fast-forward——與 worktree 模型等價。"""

    repo = _source_repo(tmp_path)
    _git(repo, "branch", _BRANCH)
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "main advances")
    expected = _git(repo, "rev-parse", "main")

    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))

    assert _git(repo, "rev-parse", _BRANCH) == expected
    assert _git(workspace, "rev-parse", "HEAD") == expected
    assert _git(workspace, "branch", "--show-current") == _BRANCH


def test_provision_rejects_diverged_existing_branch_without_moving_it(
    tmp_path: Path,
) -> None:
    """#613 的守衛在 clone 模型下必須逐字保留：不得靜默吸收前代的 commits。"""

    repo = _source_repo(tmp_path)
    _git(repo, "switch", "-qc", _BRANCH)
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-qm", "feature only")
    branch_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "main")
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "main.txt")
    _git(repo, "commit", "-qm", "main only")

    with pytest.raises(ValueError, match="commits outside requested base"):
        _creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID)

    assert _git(repo, "rev-parse", _BRANCH) == branch_head
    assert not (tmp_path / "pool" / _SEGMENT).exists()


def test_provision_fails_closed_on_existing_target_without_deleting_it(
    tmp_path: Path,
) -> None:
    """#601／#527 的守衛：撞到既有 target 一律拒絕。

    clone 模型新增了「失敗時回滾」的能力，這條測的正是回滾**不得**擴張成
    「刪掉別人的工作區再說」——`target already exists` 的路徑上一個位元組都不動。
    """

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    stale = pool / _SEGMENT
    stale.mkdir(parents=True)
    (stale / "precious.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="worktree target already exists"):
        _creator(repo, pool).create(_BRANCH, job_id=_JOB_ID)

    assert (stale / "precious.json").is_file()


def test_provision_rolls_back_the_branch_when_the_base_is_invalid(
    tmp_path: Path,
) -> None:
    """base 無效時不得留下半套狀態：branch 不被建立、目錄不存在。"""

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"

    with pytest.raises(ValueError, match="git worktree base invalid"):
        ScriptWorktreeCreator(repo=repo, wt_root=pool, base="no-such-base").create(_BRANCH, job_id=_JOB_ID)

    assert _try_git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{_BRANCH}").returncode == 1
    assert not (pool / _SEGMENT).exists()


def test_provision_creates_the_branch_in_the_source_repo(tmp_path: Path) -> None:
    """branch 仍錨定在來源 repo：dispatch baseline 與 gc 都直接讀它。"""

    repo = _source_repo(tmp_path)
    base = _git(repo, "rev-parse", "main")

    _creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID)

    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == base


def test_provisioned_workspace_matches_the_worktree_model_git_environment(
    tmp_path: Path,
) -> None:
    """clone 不繼承來源 repo 的 local config，逐項補回才能與 worktree 模型等價。"""

    repo = _source_repo(tmp_path)
    upstream = _git(repo, "remote", "get-url", "origin")

    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))

    # `origin` 是**真正的上游**，不是來源 repo——否則 delivery 的
    # `git -C <工作區> push origin` 會把 candidate 推進 Manager 的樹。
    assert _git(workspace, "remote", "get-url", "origin") == upstream
    # 來源 repo 的 remote-tracking refs 有鏡射過來（模型常跑 `git log origin/main..`）
    assert _git(workspace, "rev-parse", "origin/main") == _git(repo, "rev-parse", "origin/main")
    # `worktree add -b` 不設 upstream，clone 也不得留下 `--branch` 帶進來的那組
    assert _try_git(workspace, "rev-parse", "--abbrev-ref", "@{u}").returncode != 0
    # identity 缺席時 builder 的 `git commit` 會直接失敗
    assert _git(workspace, "config", "user.email") == "manager@example.invalid"


def test_provision_works_without_an_upstream_remote(tmp_path: Path) -> None:
    """來源 repo 沒有 `origin`（測試環境常見）時工作區同樣沒有——與 worktree 等價。"""

    repo = _source_repo(tmp_path, with_upstream=False)
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))

    assert _git(workspace, "remote") == ""
    assert _builder_commit(workspace)


# ---------------------------------------------------------------------------
# 2. 隔離
# ---------------------------------------------------------------------------

def test_workspace_has_no_git_path_back_into_the_source_repo(tmp_path: Path) -> None:
    """D2 方向性：builder 永遠不 push 進 Manager 的樹。

    clone 期間的來源 remote 必須被移除——留著它，`git push cortex-source` 就是一條
    現成的回寫路徑（權限層擋得住，但不該在工作區裡放這個把手）。
    """

    repo = _source_repo(tmp_path)
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))

    remotes = _git(workspace, "remote").splitlines()
    assert job_workspace.SOURCE_REMOTE not in remotes
    urls = [_git(workspace, "remote", "get-url", name) for name in remotes if name]
    assert all(str(repo) != url for url in urls)


def test_builder_commits_do_not_reach_the_source_repo_before_harvest(
    tmp_path: Path,
) -> None:
    """clone 有自己的 object store：未回收前來源 repo 看不到 builder 的 commit。

    這正是與 worktree 模型的實質差異，也是「必須有回收這一段」的理由。
    """

    repo = _source_repo(tmp_path)
    base = _git(repo, "rev-parse", "main")
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))

    candidate = _builder_commit(workspace)

    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == base
    assert _try_git(repo, "cat-file", "-e", f"{candidate}^{{commit}}").returncode != 0


# ---------------------------------------------------------------------------
# 3. 成果回收
# ---------------------------------------------------------------------------

def test_manager_harvests_the_candidate_out_of_the_builder_clone(
    tmp_path: Path,
) -> None:
    repo = _source_repo(tmp_path)
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))
    candidate = _builder_commit(workspace)

    harvested = _harvest(repo, workspace, tmp_path / "coordinator")

    assert harvested == candidate
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == candidate
    # object 也真的進來了——review 卡的 `worktree add --detach <candidate>` 靠這個
    assert _try_git(repo, "cat-file", "-e", f"{candidate}^{{commit}}").returncode == 0


def test_harvest_rejects_a_rewritten_history_instead_of_absorbing_it(
    tmp_path: Path,
) -> None:
    """refspec 刻意不帶 `+`：Manager 不會靜默吸收被改寫過的歷史。"""

    repo = _source_repo(tmp_path)
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))
    first = _builder_commit(workspace)
    _harvest(repo, workspace, tmp_path / "coordinator")

    _git(workspace, "reset", "-q", "--hard", "HEAD~1")
    _builder_commit(workspace, "rewritten.txt")

    with pytest.raises(job_workspace.WorkspaceError, match="not a fast-forward"):
        _harvest(repo, workspace, tmp_path / "coordinator", key="job-2")

    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == first


def test_harvest_is_a_noop_for_a_job_dispatched_before_the_spool_existed(
    tmp_path: Path,
) -> None:
    """零回歸掛點：worktree 模型與測試裡的假 job 記錄完全不受影響。"""

    repo = _source_repo(tmp_path)
    legacy = tmp_path / "legacy-worktree"
    _git(repo, "worktree", "add", "-q", "-b", "feature/legacy", str(legacy), "main")

    assert (
        job_workspace.harvest_if_spooled(
            source_repo=repo,
            job={"worktree": str(legacy), "log_path": str(tmp_path / "logs" / "legacy.jsonl")},
            branch="feature/legacy",
            coordinator_root=tmp_path / "coordinator",
        )
        is None
    )
    assert (
        job_workspace.harvest_if_spooled(
            source_repo=repo,
            job={"worktree": str(tmp_path / "does-not-exist")},
            branch="feature/x",
            coordinator_root=tmp_path / "coordinator",
        )
        is None
    )


# ---------------------------------------------------------------------------
# 4. 回收
# ---------------------------------------------------------------------------

def test_reclaim_removes_a_job_clone_and_leaves_no_residue(tmp_path: Path) -> None:
    """#601／#613 關心的正是殘留：目錄與 registry 都不得留下任何一筆。"""

    repo = _source_repo(tmp_path)
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))
    _builder_commit(workspace)
    _harvest(repo, workspace, tmp_path / "coordinator")

    result = worktree_reclaim.reclaim_worktree(workspace, repo_root=repo)

    assert result.status == worktree_reclaim.RECLAIM_RECLAIMED
    assert result.ok
    assert not workspace.exists()
    assert str(workspace) not in _git(repo, "worktree", "list", "--porcelain")
    # 成果已回收 → 沒有東西會被銷毀 → 不重複封存
    assert result.archived_ref is None


def test_reclaim_archives_unharvested_commits_before_deleting_the_clone(
    tmp_path: Path,
) -> None:
    """clone 模型下 `rmtree` 會連 object store 一起刪掉。

    worktree 模型回收工作區不銷毀任何 commit（object 在共用 store、branch 還在），
    本模組契約也明文「不銷毀證據」。未回收的 commit 因此必須先進封存命名空間。
    """

    repo = _source_repo(tmp_path)
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))
    orphan = _builder_commit(workspace)

    result = worktree_reclaim.reclaim_worktree(workspace, repo_root=repo)

    assert result.status == worktree_reclaim.RECLAIM_RECLAIMED
    assert not workspace.exists()
    assert result.archived_ref is not None
    assert result.archived_ref.startswith(job_workspace.ARCHIVE_REF_PREFIX)
    assert _git(repo, "rev-parse", result.archived_ref) == orphan
    assert result.to_dict()["archived_ref"] == result.archived_ref


def test_reclaim_derives_the_source_repo_from_the_workspace_marker(
    tmp_path: Path,
) -> None:
    """既有呼叫端都不傳 `repo_root`——封存必須靠標記檔自己找得到來源 repo。"""

    repo = _source_repo(tmp_path)
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))
    orphan = _builder_commit(workspace)

    result = worktree_reclaim.reclaim_worktree(
        workspace, git_runner=lambda args: subprocess.run(
            ["git", "-C", str(repo), *args], check=False, capture_output=True, text=True
        )
    )

    assert result.status == worktree_reclaim.RECLAIM_RECLAIMED
    assert result.archived_ref is not None
    assert _git(repo, "rev-parse", result.archived_ref) == orphan


def test_reclaim_preserves_dirty_content_inside_a_job_clone(tmp_path: Path) -> None:
    """#478 的 `.project-policy.yml` 資料遺失回報：未提交內容先封存再刪。

    `_dirty_entries` 走的是 repo-pinned runner 再疊一層 `-C <工作區>`；clone 是獨立
    repo（不是共用 gitdir 的 linked worktree），這條路徑必須照樣掃得到 dirty 內容。
    """

    repo = _source_repo(tmp_path)
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))
    (workspace / "uncommitted.txt").write_text("work in progress\n", encoding="utf-8")
    preserve_root = tmp_path / "evidence"

    result = worktree_reclaim.reclaim_worktree(
        workspace, repo_root=repo, preserve_root=preserve_root
    )

    assert result.status == worktree_reclaim.RECLAIM_RECLAIMED
    assert result.preserved_files == 1
    assert result.preserved_ref is not None
    assert (Path(result.preserved_ref) / "uncommitted.txt").read_text(
        encoding="utf-8"
    ) == "work in progress\n"
    assert not workspace.exists()


def test_reclaim_still_refuses_a_main_checkout_under_the_clone_model(
    tmp_path: Path,
) -> None:
    """判準是標記檔，不是「`.git` 是目錄」——否則陳舊的 job 記錄能刪掉整個 repo。"""

    repo = _source_repo(tmp_path)
    other = _source_repo(tmp_path / "elsewhere")

    result = worktree_reclaim.reclaim_worktree(
        other,
        git_runner=lambda args: subprocess.run(
            ["git", "-C", str(repo), *args], check=False, capture_output=True, text=True
        ),
    )

    assert result.status == worktree_reclaim.RECLAIM_FAILED
    assert result.detail == "worktree-path-not-a-worktree"
    assert (other / "tracked.txt").is_file()


# ---------------------------------------------------------------------------
# 5. `cortex work gc`
# ---------------------------------------------------------------------------

def test_gc_scan_sees_job_clones_that_git_worktree_list_cannot(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    workspace = Path(_creator(repo, pool).create(_BRANCH, job_id=_JOB_ID))

    artifacts = gc.scan(repo, worktree_root=pool)
    rows = {
        (item.kind, item.identifier): item
        for item in artifacts
        if item.kind == "worktree"
    }

    assert ("worktree", str(workspace.resolve())) in rows


def test_gc_apply_reclaims_a_merged_job_clone_by_directory_removal(
    tmp_path: Path,
) -> None:
    """clone 沒有 worktree registry，`git worktree remove` 對它必然失敗。"""

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    workspace = Path(_creator(repo, pool).create(_BRANCH, job_id=_JOB_ID))

    artifacts = gc.scan(repo, worktree_root=pool)
    applied = gc.apply_gc(repo, artifacts, default_branch="main")
    reclaimed = [
        item
        for item in applied
        if item.kind == "worktree" and item.action == gc.ACTION_RECLAIM
    ]

    assert [item.identifier for item in reclaimed] == [str(workspace.resolve())]
    assert reclaimed[0].detail == "removed"
    assert not workspace.exists()


def test_gc_protects_the_branch_of_a_live_job_clone(tmp_path: Path) -> None:
    """未 merge 的工作區仍在用時，它的 branch 不得被歸類為可回收。

    worktree 模型靠 `git worktree list` 找出「還掛著」的 branch；clone 沒有那份
    registry，少了 clone 掃描這一步 gc 會在 job 還在跑的時候把 branch 刪掉。
    """

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    workspace = Path(_creator(repo, pool).create(_BRANCH, job_id=_JOB_ID))
    _builder_commit(workspace)
    _harvest(repo, workspace, tmp_path / "coordinator")

    artifacts = gc.scan(repo, worktree_root=pool)
    branch_rows = [item for item in artifacts if item.kind == "branch" and item.branch == _BRANCH]

    assert branch_rows and all(item.action == gc.ACTION_KEEP for item in branch_rows)


def test_gc_still_reclaims_a_pre_upgrade_linked_worktree(tmp_path: Path) -> None:
    """既有部署零回歸：升級前建立的 linked worktree 仍走 `git worktree remove`。"""

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    pool.mkdir()
    legacy = pool / "feature-legacy"
    _git(repo, "worktree", "add", "-q", "-b", "feature/legacy", str(legacy), "main")

    artifacts = gc.scan(repo, worktree_root=pool)
    applied = gc.apply_gc(repo, artifacts, default_branch="main")
    reclaimed = [
        item
        for item in applied
        if item.kind == "worktree" and item.action == gc.ACTION_RECLAIM
    ]

    assert [item.identifier for item in reclaimed] == [str(legacy.resolve())]
    assert not legacy.exists()
    assert str(legacy) not in _git(repo, "worktree", "list", "--porcelain")


# ---------------------------------------------------------------------------
# 6. 端到端
# ---------------------------------------------------------------------------

def test_workflow_lane_harvests_the_candidate_when_the_card_is_accepted(
    tmp_path: Path,
) -> None:
    """canonical lane 的掛點：build card 的 candidate 被採信後立刻取回 Manager 的樹。

    掛在 `_verify_build_candidate_transition` **之後**——那一步已確認 candidate 就是
    工作區的 HEAD 且單調延伸自基線，回收只負責搬運，不新增採信路徑。
    """

    repo = _source_repo(tmp_path)
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))
    candidate = _builder_commit(workspace)
    coordinator_root = tmp_path / "coordinator"
    bundle = _produce_bundle(workspace, coordinator_root)
    job = {
        "worktree": str(workspace),
        "branch": _BRANCH,
        "log_path": str(tmp_path / "logs" / f"{bundle.parent.name}.jsonl"),
    }
    run = SimpleNamespace(workspace_root=str(repo))

    harvested = manager._harvest_build_candidate(
        job, run=run, candidate=candidate, coordinator_root=coordinator_root
    )

    assert harvested == candidate
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == candidate


def test_workflow_lane_harvest_is_a_noop_outside_the_clone_model(
    tmp_path: Path,
) -> None:
    """既有部署零回歸：worktree 模型與測試裡的假 job 記錄完全不觸發回收。"""

    repo = _source_repo(tmp_path)
    run = SimpleNamespace(workspace_root=str(repo))
    coordinator_root = tmp_path / "coordinator"

    assert (
        manager._harvest_build_candidate(
            {"worktree": str(repo), "branch": "main"},
            run=run, candidate="0" * 40, coordinator_root=coordinator_root,
        )
        is None
    )
    assert (
        manager._harvest_build_candidate(
            {"worktree": "", "branch": _BRANCH},
            run=run, candidate="0" * 40, coordinator_root=coordinator_root,
        )
        is None
    )


def test_workflow_lane_harvest_fails_closed_when_the_head_does_not_match(
    tmp_path: Path,
) -> None:
    repo = _source_repo(tmp_path)
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))
    _builder_commit(workspace)
    coordinator_root = tmp_path / "coordinator"
    bundle = _produce_bundle(workspace, coordinator_root)
    job = {
        "worktree": str(workspace),
        "branch": _BRANCH,
        "log_path": str(tmp_path / "logs" / f"{bundle.parent.name}.jsonl"),
    }
    run = SimpleNamespace(workspace_root=str(repo))

    with pytest.raises(ValueError, match="harvest head mismatch"):
        manager._harvest_build_candidate(
            job, run=run, candidate="0" * 40, coordinator_root=coordinator_root
        )


def test_slice_lane_verification_harvests_before_reading_the_branch(
    tmp_path: Path,
) -> None:
    """`verification` 以來源 repo 為根判讀 candidate／ancestry／diff。

    clone 模型下那些 commit 只在工作區裡——少了回收這一步，整段會以
    `candidate-unreadable` 或 `candidate-worktree-mismatch` 收場。
    """

    repo = _source_repo(tmp_path)
    base = _git(repo, "rev-parse", "main")
    workspace = Path(_creator(repo, tmp_path / "pool").create(_BRANCH, job_id=_JOB_ID))
    candidate = _builder_commit(workspace)
    bundle = _produce_bundle(workspace, tmp_path / "coordinator")
    contract = {
        "docs_class": "trivial",
        "review_policy": "not-required",
        "required_artifacts": [],
        "checks": [],
        "tests": [],
        "full_suite": {
            "argv": ["true"],
            "cwd": ".",
            "timeout_seconds": 5,
            "baseline": "no-regression",
        },
    }

    verification.run_result_verification(
        slice_row={
            "slice_id": "clone-623",
            "dispatch_base": base,
            "verification": {"contract": contract},
        },
        job={
            "task": "clone-623",
            "branch": _BRANCH,
            "worktree": str(workspace),
            "log_path": str(tmp_path / "logs" / f"{bundle.parent.name}.jsonl"),
        },
        repo_root=repo,
        coordinator_root=tmp_path / "coordinator",
    )

    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == candidate


def test_end_to_end_provision_commit_harvest_reclaim(tmp_path: Path) -> None:
    """一整條：provision → builder commit → Manager 回收成果 → 回收工作區。"""

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    workspace = Path(_creator(repo, pool).create(_BRANCH, job_id=_JOB_ID))

    candidate = _builder_commit(workspace)
    # builder 寫不到來源 repo 的 ref（此處以「來源 repo 沒有這條路徑」驗證方向性；
    # 檔案權限那一半屬 trust-root 的 chown／ACL，不在本層）
    assert job_workspace.SOURCE_REMOTE not in _git(workspace, "remote").splitlines()

    harvested = _harvest(repo, workspace, tmp_path / "coordinator")
    assert harvested == candidate
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == candidate

    result = worktree_reclaim.reclaim_worktree(workspace, repo_root=repo)

    assert result.ok
    assert not workspace.exists()
    # 成果留在 Manager 的樹裡，工作區消失也不會遺失
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == candidate
    assert _try_git(repo, "cat-file", "-e", f"{candidate}^{{commit}}").returncode == 0
