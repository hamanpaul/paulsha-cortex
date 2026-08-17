"""#612 不變式：repo 根解析不出來時，production 動作必須**拒絕執行**而非落到 cwd。

背景（#610 → #611 → #612）：`paths.repo_root()` 舊實作未宣告 `PSC_REPO_ROOT` 時
退回 `Path.cwd()`，而 manager daemon 的 `WorkingDirectory` 正是 operator 的真實
cortex checkout。任何從 slice spec／config／payload 滲入的**相對**路徑，經
`autonomy._infer_repo_root` 的 `Path.resolve()` 之後都會把 repo 根解析成那個
checkout，於是 production 動作在錯的樹上「成功」執行——#610 實測到的形態是
`manager.complete_tick → _completion_candidate_ref` 對真實 repo 跑
`git fetch --no-tags origin main`（連帶打真實 github.com）。#611 只修了測試層，
production 面的預設值留到本檔對應的修復。

#623 讓這條更緊：trust-root Phase 2b 的 Manager unit 帶 `ProtectHome=yes`，repo
源碼樹要遷入 Manager-owned 樹，任何「落回 cwd 或 operator checkout」的解析都是
**無聲的錯誤目標**——不是失敗，是打在錯的樹上。

本檔的每個測試都遵循同一個形狀：
1. 把 cwd 設成一個**真的** git repo（重演 daemon 在 operator checkout 裡跑的形狀）；
2. 餵入相對路徑／不宣告 `PSC_REPO_ROOT`；
3. 斷言 production 動作 **fail-closed**，且**沒有任何 git 指令打向那個 cwd repo**。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from git_fixtures import make_fake_repo
from paulsha_cortex.config import paths
from paulsha_cortex.coordinator import autonomy, manager, seams
from paulsha_cortex.coordinator import dispatcher as dispatcher_mod
from paulsha_cortex.coordinator.autonomy import RepoRootResolutionError
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.registry import JobRegistry


class RecordingGitRunner:
    """記下每一條 git argv；永遠回成功，好讓「有沒有被呼叫」成為唯一的觀測點。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> SimpleNamespace:
        self.calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="a" * 40, stderr="")

    def targets(self) -> list[str]:
        """所有 `-C <path>` 的目標。"""
        return [
            argv[index + 1]
            for argv in self.calls
            for index, token in enumerate(argv)
            if token == "-C" and index + 1 < len(argv)
        ]


@pytest.fixture
def cwd_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把 cwd 換成一個真 repo，並清掉 `PSC_REPO_ROOT`。

    這正是事故現場的形狀：daemon 的工作目錄是 operator 的 checkout，而目標 repo
    沒有被顯式宣告。任何「落回 cwd」的實作都會在這個 fixture 底下被抓到。
    """
    repo = make_fake_repo(tmp_path / "operator-checkout")
    monkeypatch.delenv("PSC_REPO_ROOT", raising=False)
    monkeypatch.chdir(repo)
    return repo


# --------------------------------------------------------------------------- #
# 1) 路徑推斷層：相對路徑一律 fail-closed
# --------------------------------------------------------------------------- #
def test_infer_repo_root_rejects_relative_spec_path(cwd_repo: Path) -> None:
    """相對 spec 路徑不得被 `resolve()` 接到 cwd 上。"""
    with pytest.raises(RepoRootResolutionError) as excinfo:
        autonomy._infer_repo_root(Path("specs/slice-relative.md"))
    diagnostic = excinfo.value.diagnostic
    assert diagnostic.reason == "spec-path-not-absolute"
    assert diagnostic.source.startswith("autonomy._infer_repo_root")
    # 診斷必須帶得走：source ＋ context 讓 operator 不必反推是哪條路徑寫的。
    assert diagnostic.context["spec_path"] == "specs/slice-relative.md"


def test_infer_repo_root_rejects_relative_spec_even_with_configured_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """有宣告 `PSC_REPO_ROOT` 也不放行相對路徑。

    宣告了目標 repo 並不代表相對路徑就該相對它解析——`Path.resolve()` 相對的
    始終是 cwd，不是 `PSC_REPO_ROOT`。正規化必須在進件邊界完成。
    """
    declared = make_fake_repo(tmp_path / "declared-repo")
    monkeypatch.setenv("PSC_REPO_ROOT", str(declared))
    monkeypatch.chdir(make_fake_repo(tmp_path / "operator-checkout"))

    with pytest.raises(RepoRootResolutionError) as excinfo:
        autonomy._infer_repo_root(Path("specs/slice-relative.md"))
    assert excinfo.value.diagnostic.reason == "spec-path-not-absolute"


def test_parse_spec_frontmatter_holds_spec_with_unresolvable_repo_root(
    cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """掃描不炸，但這份 spec 永遠停在 `hold`＋帶 `parse_error`。

    `dispatch: auto` 的 spec 只要 repo 根推不出來就不得被派工——但整輪 scan 也
    不該因為一份壞 spec 而中斷，因此理由落成 `parse_error`。
    """
    spec = cwd_repo / "specs" / "slice-relative.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(
        "---\ndispatch: auto\nslice_id: slice-relative\nplan: docs/plan.md\n"
        "target_branch: main\n---\n\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(cwd_repo)

    meta = autonomy.parse_spec_frontmatter("specs/slice-relative.md")

    assert meta["dispatch"] == "hold"
    assert meta["parse_error"]["code"] == "spec-path-not-absolute"
    assert meta["parse_error"]["field"] == "path"


# --------------------------------------------------------------------------- #
# 2) production 動作層：拒絕執行，且不得對 cwd repo 下任何 git 指令
# --------------------------------------------------------------------------- #
def test_resolve_target_base_sha_refuses_relative_spec_without_fetching(cwd_repo: Path) -> None:
    """派工前的 `git fetch --no-tags <remote> <branch>` 必須先解析出目標 repo。"""
    runner = RecordingGitRunner()
    pinned_inputs = {
        "spec_path": "specs/slice-relative.md",
        "target_branch": "main",
        "target_remote": "origin",
    }

    with pytest.raises(RepoRootResolutionError):
        autonomy._resolve_target_base_sha(
            meta={"depends_on": []},
            pinned_inputs=pinned_inputs,
            handoff_dir=str(cwd_repo / "handoff"),
            git_runner=runner,
        )

    assert runner.calls == [], "解析不出 repo 根就不得執行任何 git 指令"


def test_pin_dispatch_inputs_refuses_relative_spec_path(cwd_repo: Path) -> None:
    """派工 pin 也走同一條推斷，同樣不得落到 cwd。"""
    with pytest.raises(RepoRootResolutionError):
        autonomy.pin_dispatch_inputs(
            {
                "slice_id": "slice-relative",
                "path": "specs/slice-relative.md",
                "plan": "docs/plan.md",
                "verification": None,
            }
        )


def test_ancestry_status_reports_repo_unresolved_without_touching_cwd_repo(
    cwd_repo: Path,
) -> None:
    """`rev-parse` / `merge-base --is-ancestor` 這條 ancestry 判準也一併 fail-closed。"""
    runner = RecordingGitRunner()
    slice_row = {
        "slice_id": "slice-relative",
        "candidate": "b" * 40,
        "target_branch": "main",
        "target_remote": "origin",
        "spec": {"path": "specs/slice-relative.md"},
    }

    summary = manager._resolve_ancestry_status(slice_row, git_runner=runner)

    assert summary["status"] == "repo-unresolved"
    assert runner.calls == []


def test_complete_tick_relative_spec_never_fetches_from_cwd_repo(
    cwd_repo: Path, tmp_path: Path
) -> None:
    """#610 事故路徑的直接回歸：相對 spec path 不得讓 tick 對 cwd repo 跑 fetch。

    舊行為：`_infer_repo_root("specs/slice-x.md")` → cwd（＝ operator 的真實
    checkout）→ `_completion_candidate_ref` 對它跑
    `git -C <真實 checkout> fetch --no-tags origin main`，而該 repo 的 origin
    是真的 github.com。
    """
    runner = RecordingGitRunner()
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    builder_job = registry.create_job(
        task="slice-relative",
        persona="builder",
        branch="feature/slice-relative",
        pane="",
        worktree=str(tmp_path / "wt" / "feature-slice-relative"),
    )
    registry.update_headless_result(builder_job["job_id"], status="exited", exit_code=0)
    registry.create_slice(
        slice_id="slice-relative",
        spec_path="specs/slice-relative.md",  # ← 相對路徑：事故的進件形狀
        spec_hash="0" * 64,
        plan_path="plans/slice-relative.md",
        plan_hash="1" * 64,
        target_branch="main",
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=None,
        candidate=None,
    )

    handoff_dir = tmp_path / "handoff"
    dispatcher = Dispatcher(registry, pane_sender=MagicMock(), worktree_creator=MagicMock())

    summary = manager.complete_tick(
        dispatcher,
        handoff_dir=str(handoff_dir),
        git_runner=runner,
        clock=lambda: "T0",
    )

    # 一條 git 都不該打出去，更不可能打到 cwd 的 repo。
    assert str(cwd_repo) not in runner.targets()
    assert all("fetch" not in argv for argv in runner.calls)
    # 理由要留在 tick 的錯誤通道上，而不是靜默完成。
    assert summary["errors"], "repo 根解析失敗必須進 errors，而不是靜默略過"
    assert any("spec-path-not-absolute" in str(entry) for entry in summary["errors"])
    # 也不得寫出任何終局 manifest——那份 manifest 的內容會是對錯 repo 的觀測。
    assert not (handoff_dir / "slice-relative.json").exists()


# --------------------------------------------------------------------------- #
# 3) 路徑契約層：未宣告 `PSC_REPO_ROOT` 時，git seam 與 worktree 建立都 fail-closed
# --------------------------------------------------------------------------- #
def test_default_git_runner_fails_closed_without_declared_repo_root(cwd_repo: Path) -> None:
    """dispatcher 的預設 git seam 是 `git -C paths.repo_root() ...`。

    未宣告目標 repo 時它以前會在 cwd 上執行——這裡釘死它必須拒絕。
    """
    with pytest.raises(paths.RepoRootUnresolvedError):
        dispatcher_mod._default_git_runner(["rev-parse", "HEAD"])


def test_worktree_creator_fails_closed_without_declared_repo_root(cwd_repo: Path) -> None:
    """worktree 建立是**寫入**動作，落在錯的 repo 就是事故而不只是誤讀。"""
    with pytest.raises(paths.RepoRootUnresolvedError):
        seams.ScriptWorktreeCreator()


def test_trusted_repo_root_resolution_does_not_fall_back_to_cwd(cwd_repo: Path) -> None:
    """`work` lane 的 owner/name → repo 根解析同樣不得把 cwd 當候選。"""
    from paulsha_cortex.coordinator import work_bridge

    with pytest.raises(ValueError, match="trusted repo registry"):
        work_bridge.resolve_trusted_repo_root("example/acme")


# --------------------------------------------------------------------------- #
# 4) 顯式路徑仍照常運作（fail-closed 不得誤殺正常流程）
# --------------------------------------------------------------------------- #
def test_absolute_spec_inside_declared_repo_still_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    declared = make_fake_repo(tmp_path / "declared-repo")
    spec = declared / "specs" / "slice-ok.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# ok\n", encoding="utf-8")
    monkeypatch.setenv("PSC_REPO_ROOT", str(declared))
    monkeypatch.chdir(make_fake_repo(tmp_path / "operator-checkout"))

    assert autonomy._infer_repo_root(spec) == declared


def test_absolute_spec_in_foreign_repo_resolves_to_that_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """跨 repo 派工（#469）不受影響：spec 所屬的 repo 仍優先於宣告值。"""
    foreign = make_fake_repo(tmp_path / "foreign-repo")
    spec = foreign / "specs" / "slice-foreign.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# foreign\n", encoding="utf-8")
    monkeypatch.setenv("PSC_REPO_ROOT", str(make_fake_repo(tmp_path / "declared-repo")))
    monkeypatch.chdir(make_fake_repo(tmp_path / "operator-checkout"))

    assert autonomy._infer_repo_root(spec) == foreign


def test_complete_tick_with_absolute_spec_still_writes_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """對照組：同一條 tick，spec 路徑改成絕對就照常走完並落 manifest。"""
    repo = make_fake_repo(tmp_path / "declared-repo")
    monkeypatch.setenv("PSC_REPO_ROOT", str(repo))
    runner = RecordingGitRunner()
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    builder_job = registry.create_job(
        task="slice-abs",
        persona="builder",
        branch="feature/slice-abs",
        pane="",
        worktree=str(tmp_path / "wt" / "feature-slice-abs"),
    )
    registry.update_headless_result(builder_job["job_id"], status="failed", exit_code=1)
    registry.create_slice(
        slice_id="slice-abs",
        spec_path=str(repo / "specs" / "slice-abs.md"),
        spec_hash="0" * 64,
        plan_path=str(repo / "plans" / "slice-abs.md"),
        plan_hash="1" * 64,
        target_branch="main",
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=None,
        candidate=None,
    )

    handoff_dir = tmp_path / "handoff"
    dispatcher = Dispatcher(registry, pane_sender=MagicMock(), worktree_creator=MagicMock())

    summary = manager.complete_tick(
        dispatcher,
        handoff_dir=str(handoff_dir),
        git_runner=runner,
        clock=lambda: "T0",
    )

    assert summary["errors"] == []
    manifest = json.loads((handoff_dir / "slice-abs.json").read_text(encoding="utf-8"))
    assert manifest["slice_id"] == "slice-abs"
    # 任何 git 目標都必須是宣告的 repo（或 job 自己的 worktree），不得是 cwd。
    for target in runner.targets():
        assert target.startswith(str(tmp_path)), target
