"""issue #478：recover-pre-candidate 必須同時清掉目錄與 git worktree registry。

這裡刻意用**真實** temporary git repo 與真實 linked worktree，而不是普通暫存
目錄——#478 的既有測試（`tests/test_pre_candidate_recovery.py`）正是因為只驗
「目錄被刪掉」而看不見 registry 殘留，讓生產現場連續四次重現同一個缺陷。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from paulsha_cortex.coordinator import job_workspace, manager, work_actions, worktree_reclaim
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.seams import ScriptWorktreeCreator


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout


def _init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "reclaim@example.invalid")
    _git(repo, "config", "user.name", "reclaim-test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "init")
    return repo


def _registered_worktrees(repo: Path) -> set[str]:
    return {
        line[len("worktree ") :].strip()
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    }


def _runner_for(repo: Path):
    def _runner(args: list[str]) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip())
        return proc.stdout.strip()

    return _runner


# --------------------------------------------------------------------------
# reclaim_worktree 本身
# --------------------------------------------------------------------------


def test_reclaim_removes_directory_and_registry_entry(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    worktree = tmp_path / "pool" / "feature-slice-a"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature/slice-a")
    assert str(worktree) in _registered_worktrees(repo)

    result = worktree_reclaim.reclaim_worktree(
        worktree, git_runner=_runner_for(repo), preserve_root=tmp_path / "evidence"
    )

    assert result.status == worktree_reclaim.RECLAIM_RECLAIMED
    assert result.registry_entry_found is True
    assert result.registry_removed is True
    assert not worktree.exists()
    assert str(worktree) not in _registered_worktrees(repo)
    # #478 驗收條款：同一條 feature branch 必須能立刻重新掛上。
    _git(repo, "worktree", "add", "-q", str(worktree), "feature/slice-a")


def test_reclaim_self_heals_stale_registry_when_directory_already_gone(
    tmp_path: Path,
) -> None:
    """既存壞狀態（目錄不存在、registry 殘留 prunable 記錄）也要能收乾淨。"""

    repo = _init_repo(tmp_path)
    worktree = tmp_path / "pool" / "feature-slice-b"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature/slice-b")
    # 模擬舊版 recover-pre-candidate：只 rmtree 目錄、不動 registry。
    subprocess.run(["rm", "-rf", str(worktree)], check=True)
    assert str(worktree) in _registered_worktrees(repo)

    result = worktree_reclaim.reclaim_worktree(
        worktree, git_runner=_runner_for(repo), preserve_root=tmp_path / "evidence"
    )

    assert result.status == worktree_reclaim.RECLAIM_RECLAIMED
    assert result.registry_entry_found is True
    assert result.registry_removed is True
    assert str(worktree) not in _registered_worktrees(repo)
    _git(repo, "worktree", "add", "-q", str(worktree), "feature/slice-b")


def test_reclaim_reports_absent_when_nothing_to_reclaim(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    result = worktree_reclaim.reclaim_worktree(
        tmp_path / "pool" / "never-existed", git_runner=_runner_for(repo)
    )
    assert result.status == worktree_reclaim.RECLAIM_ABSENT
    assert result.ok is True


def test_reclaim_preserves_dirty_content_before_removing(tmp_path: Path) -> None:
    """#478 comment：未追蹤的 agent 產物不得被 rmtree 靜默丟掉。"""

    repo = _init_repo(tmp_path)
    worktree = tmp_path / "pool" / "feature-slice-c"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature/slice-c")
    (worktree / ".project-policy.yml").write_text("policy_version: 1.0.17\n", encoding="utf-8")
    (worktree / "README.md").write_text("seed\nmutated\n", encoding="utf-8")

    result = worktree_reclaim.reclaim_worktree(
        worktree, git_runner=_runner_for(repo), preserve_root=tmp_path / "evidence"
    )

    assert result.status == worktree_reclaim.RECLAIM_RECLAIMED
    assert result.preserved_ref is not None
    preserved = Path(result.preserved_ref)
    assert (preserved / ".project-policy.yml").read_text(encoding="utf-8") == (
        "policy_version: 1.0.17\n"
    )
    assert (preserved / "README.md").read_text(encoding="utf-8") == "seed\nmutated\n"
    assert not worktree.exists()


def test_reclaim_fails_closed_when_registry_entry_survives(tmp_path: Path) -> None:
    """`git worktree remove` 失敗時不得回報成功（#478 驗收條款第三條）。"""

    repo = _init_repo(tmp_path)
    worktree = tmp_path / "pool" / "feature-slice-d"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature/slice-d")
    real = _runner_for(repo)

    def stubborn(args: list[str]) -> str:
        if args[:2] == ["worktree", "remove"]:
            raise RuntimeError("fatal: validation failed, cannot remove working tree")
        if args[:2] == ["worktree", "prune"]:
            return ""
        return real(args)

    result = worktree_reclaim.reclaim_worktree(
        worktree, git_runner=stubborn, preserve_root=tmp_path / "evidence"
    )

    assert result.status == worktree_reclaim.RECLAIM_FAILED
    assert result.ok is False
    assert "worktree-registry-entry-remains" in (result.detail or "")
    # fail closed：目錄必須原封不動，不得先刪目錄再回報失敗。
    assert worktree.is_dir()


def test_reclaim_refuses_a_path_that_is_not_a_worktree(tmp_path: Path) -> None:
    """陳舊／指錯的 job 記錄不得讓回收遞迴刪掉任意目錄。"""

    repo = _init_repo(tmp_path)
    innocent = tmp_path / "not-a-worktree"
    innocent.mkdir()
    (innocent / "precious.json").write_text("{}", encoding="utf-8")

    result = worktree_reclaim.reclaim_worktree(innocent, git_runner=_runner_for(repo))

    assert result.status == worktree_reclaim.RECLAIM_FAILED
    assert result.detail == "worktree-path-not-a-worktree"
    assert (innocent / "precious.json").is_file()


def test_reclaim_refuses_a_main_checkout(tmp_path: Path) -> None:
    """job.worktree 實測會等於 run 的 `workspace_root`（主 checkout，`.git` 為
    目錄）——回收絕不能把整個 repo 刪掉。"""

    repo = _init_repo(tmp_path)
    other = _init_repo(tmp_path / "elsewhere")

    result = worktree_reclaim.reclaim_worktree(other, git_runner=_runner_for(repo))

    assert result.status == worktree_reclaim.RECLAIM_FAILED
    assert result.detail == "worktree-path-not-a-worktree"
    assert (other / "README.md").is_file()


def test_reclaim_fails_closed_when_registry_is_unreadable(tmp_path: Path) -> None:
    def broken(_args: list[str]) -> str:
        raise RuntimeError("fatal: not a git repository")

    result = worktree_reclaim.reclaim_worktree(tmp_path / "pool" / "x", git_runner=broken)
    assert result.status == worktree_reclaim.RECLAIM_FAILED
    assert "worktree-registry-unreadable" in (result.detail or "")


def test_resolve_git_runner_falls_back_to_production_default(tmp_path: Path) -> None:
    """#478 驗收條款第一條：未注入 runner 時退回 production-safe 預設實作。"""

    from paulsha_cortex.coordinator import dispatcher

    assert worktree_reclaim.resolve_git_runner(None) is dispatcher._default_git_runner


# --------------------------------------------------------------------------
# recover-pre-candidate（slice lane，manager.apply_slice_action）
# --------------------------------------------------------------------------


def _seed_recoverable_slice(
    registry: JobRegistry, *, slice_id: str, branch: str, worktree: Path
) -> None:
    builder_job = registry.create_job(
        task=slice_id, persona="builder", branch=branch, pane="", worktree=str(worktree)
    )
    registry.update_headless_result(builder_job["job_id"], status="failed", exit_code=1)
    registry.create_slice(
        slice_id=slice_id,
        spec_path=f"specs/{slice_id}.md",
        spec_hash="spec-sha",
        plan_path=f"plans/{slice_id}.md",
        plan_hash="plan-sha",
        target_branch="main",
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=None,
        candidate=None,
    )
    registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")


def test_recover_pre_candidate_clears_registry_and_allows_redispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生產現場重現：dispatcher._git_runner is None 時仍必須清掉 registry。

    舊碼 `runner = git_runner or getattr(dispatcher, "_git_runner", None)` 在
    runner 為 None 時整段跳過 git 清理，只 rmtree 目錄——registry 留著，下一輪
    `git worktree add` 立刻以 `cannot force update the branch ...` 失敗。
    """

    repo = _init_repo(tmp_path)
    pool = tmp_path / "repo-worktrees"
    worktree = pool / "feature-slice-e"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature/slice-e")
    monkeypatch.setenv("PSC_REPO_ROOT", str(repo))
    monkeypatch.setenv("PSC_WORKTREE_ROOT", str(pool))

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    _seed_recoverable_slice(
        registry, slice_id="slice-e", branch="feature/slice-e", worktree=worktree
    )
    dispatcher = Dispatcher(
        registry, pane_sender=MagicMock(), worktree_creator=MagicMock()
    )
    assert getattr(dispatcher, "_git_runner", "missing") is None

    result = manager.apply_slice_action(
        dispatcher=dispatcher,
        slice_id="slice-e",
        action="recover-pre-candidate",
        actor="test-operator",
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
    )

    assert result["result"] == "ok"
    assert result["worktree_reclaim"]["status"] == "reclaimed"
    assert not worktree.exists()
    assert str(worktree) not in _registered_worktrees(repo)
    assert registry.get_slice("slice-e")["state"] == "pending"
    # 下一 tick 的實際動作：dispatch 會呼叫 ScriptWorktreeCreator.create()。
    # #645：目錄名改由 job id 導出（branch 名不變），因此重建的位置是新形狀那一個。
    recreated = ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main").create(
        "feature/slice-e", job_id="slice-e"
    )
    assert Path(recreated) == pool / job_workspace.job_segment("slice-e")


def test_recover_pre_candidate_self_heals_orphan_registry_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目錄已被前一次（壞掉的）回收刪掉，registry 殘留——本次必須自癒。"""

    repo = _init_repo(tmp_path)
    pool = tmp_path / "repo-worktrees"
    worktree = pool / "feature-slice-f"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature/slice-f")
    subprocess.run(["rm", "-rf", str(worktree)], check=True)
    monkeypatch.setenv("PSC_REPO_ROOT", str(repo))
    monkeypatch.setenv("PSC_WORKTREE_ROOT", str(pool))

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    _seed_recoverable_slice(
        registry, slice_id="slice-f", branch="feature/slice-f", worktree=worktree
    )
    dispatcher = Dispatcher(
        registry, pane_sender=MagicMock(), worktree_creator=MagicMock()
    )

    result = manager.apply_slice_action(
        dispatcher=dispatcher,
        slice_id="slice-f",
        action="recover-pre-candidate",
        actor="test-operator",
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
    )

    assert result["result"] == "ok"
    assert str(worktree) not in _registered_worktrees(repo)
    ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main").create(
        "feature/slice-f", job_id="slice-f"
    )


def test_recover_pre_candidate_fails_closed_when_reclaim_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回收失敗時 recovery 必須 raise，不得回報 pending/ok。"""

    repo = _init_repo(tmp_path)
    pool = tmp_path / "repo-worktrees"
    worktree = pool / "feature-slice-g"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature/slice-g")
    monkeypatch.setenv("PSC_REPO_ROOT", str(repo))
    monkeypatch.setenv("PSC_WORKTREE_ROOT", str(pool))

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    _seed_recoverable_slice(
        registry, slice_id="slice-g", branch="feature/slice-g", worktree=worktree
    )
    dispatcher = Dispatcher(
        registry, pane_sender=MagicMock(), worktree_creator=MagicMock()
    )
    real = _runner_for(repo)

    def stubborn(args: list[str]) -> str:
        if args[:2] in (["worktree", "remove"], ["worktree", "prune"]):
            raise RuntimeError("fatal: refusing to remove")
        return real(args)

    with pytest.raises(RuntimeError, match="worktree reclaim failed"):
        manager.apply_slice_action(
            dispatcher=dispatcher,
            slice_id="slice-g",
            action="recover-pre-candidate",
            actor="test-operator",
            specs_dir=str(tmp_path / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            git_runner=stubborn,
        )

    # slice 必須停在原狀態（不是假的 pending），operator 才知道要介入。
    assert registry.get_slice("slice-g")["state"] == "needs_human"
    assert str(worktree) in _registered_worktrees(repo)


# --------------------------------------------------------------------------
# supersede（abandon）路徑共用同一回收函式
# --------------------------------------------------------------------------


def test_abandon_reclaims_build_worktree_of_the_superseded_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#527 root cause 之一：supersede 不回收 build worktree。"""

    repo = _init_repo(tmp_path)
    pool = tmp_path / "repo-worktrees"
    mine = pool / "feature-mine"
    other = pool / "feature-other"
    _git(repo, "worktree", "add", "-q", str(mine), "-b", "feature/mine")
    _git(repo, "worktree", "add", "-q", str(other), "-b", "feature/other")
    monkeypatch.setenv("PSC_REPO_ROOT", str(repo))

    run = SimpleNamespace(run_id="workflow-" + "a" * 20)
    workflow_registry = SimpleNamespace(
        list_jobs=lambda: [
            {"workflow_run_id": run.run_id, "worktree": str(mine)},
            {"workflow_run_id": "workflow-" + "b" * 20, "worktree": str(other)},
            {"workflow_run_id": run.run_id, "worktree": None},
        ]
    )

    work_actions._reclaim_abandoned_build_worktrees(
        run, workflow_registry, state_path=tmp_path / "jobs.json"
    )

    registered = _registered_worktrees(repo)
    assert str(mine) not in registered
    assert not mine.exists()
    # 別的 run 的 worktree 一律不碰。
    assert str(other) in registered
    assert other.is_dir()


def test_abandon_worktree_reclaim_never_raises(tmp_path: Path) -> None:
    """回收是 abandon 的附帶效果，失敗只落 diagnostics、不得讓 abandon 反悔。"""

    def explode() -> list[dict[str, object]]:
        raise RuntimeError("registry unavailable")

    work_actions._reclaim_abandoned_build_worktrees(
        SimpleNamespace(run_id="workflow-" + "c" * 20),
        SimpleNamespace(list_jobs=explode),
        state_path=tmp_path / "jobs.json",
    )
