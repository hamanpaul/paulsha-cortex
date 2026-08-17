"""#633：`ScriptWorktreeCreator` 的 repo 解析改 lazy——起得來，但派不了工。

## 生產現場

#612／#630 把 `paths.repo_root()` 改成未宣告 `PSC_REPO_ROOT` 即拋
`RepoRootUnresolvedError`。方向正確，但它命中的位置在**啟動路徑**上：

```
manager_daemon.run_loop → ensure_dispatcher()
                        → Dispatcher(…, ScriptWorktreeCreator())
seams.ScriptWorktreeCreator.__init__ → paths.repo_root()
paths.repo_root                      → raise RepoRootUnresolvedError
```

於是 `PSC_REPO_ROOT` 未設時 Manager **啟動即崩**，`Restart=on-failure` 把它變成
crash-loop（實機 `NRestarts` 連跳 7 次）——一台什麼都做不了、也什麼都不說的機器。

## 本檔釘死的性質（時機改了，性質沒改）

1. **起得來**：未宣告 `PSC_REPO_ROOT` 時，建構子不拋、`Dispatcher` 組得起來、
   `run_loop` 走完一輪並回 `True`。
2. **派工時 fail-closed**：第一次 `create()` 仍拋 `RepoRootUnresolvedError`，訊息
   逐字沿用 `paths` 那條（可操作：說得出缺哪個變數、以及為什麼不猜）。
3. **沒有放寬**：沒有任何 cwd 退路——cwd 是一棵真 repo 時也不得被採用，磁碟上不
   得留下任何 worktree pool。
4. **顯式路徑不受影響**：`repo=` / `wt_root=` 給定時完全不碰 `paths`。
5. **memoize**：解析只發生一次；一個 creator 在存活期間永遠對同一棵樹動手。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from git_fixtures import make_fake_repo
from paulsha_cortex.config import paths
from paulsha_cortex.coordinator import manager_daemon, seams
from paulsha_cortex.coordinator.dispatcher import Dispatcher


@pytest.fixture
def undeclared_repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """重演事故形狀：cwd 是 operator 的真 checkout，目標 repo 沒有被宣告。"""
    repo = make_fake_repo(tmp_path / "operator-checkout")
    monkeypatch.delenv("PSC_REPO_ROOT", raising=False)
    monkeypatch.delenv("PSC_WORKTREE_ROOT", raising=False)
    monkeypatch.chdir(repo)
    return repo


# --------------------------------------------------------------------------- #
# 1) 起得來：建構子不再是 fail-closed 的觸發點
# --------------------------------------------------------------------------- #
def test_creator_constructs_without_declared_repo_root(undeclared_repo_root: Path) -> None:
    """#633 的核心：實體化本身不得解析 repo。"""
    creator = seams.ScriptWorktreeCreator()
    assert isinstance(creator, seams.ScriptWorktreeCreator)


def test_dispatcher_constructs_without_declared_repo_root(undeclared_repo_root: Path) -> None:
    """`manager_daemon.ensure_dispatcher()` 那一行的形狀，逐字重演。"""
    dispatcher = Dispatcher(MagicMock(), MagicMock(), seams.ScriptWorktreeCreator())
    assert dispatcher is not None


def test_run_loop_starts_without_declared_repo_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, undeclared_repo_root: Path
) -> None:
    """整條啟動路徑：不注入 dispatcher／request_executor，逼 `ensure_dispatcher()` 跑。

    這正是 crash-loop 的來源。`run_loop` 回 `True`（＝取得鎖、跑完一輪）就是
    「Manager 起得來」的觀測點；#633 之前這裡是 `RepoRootUnresolvedError`。
    """
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path / "control"))
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(tmp_path / "coordinator"))
    monkeypatch.setenv("PSC_RUN_ROOT", str(tmp_path / "run"))
    monkeypatch.setenv("PSC_TRUST_ROOT_SELFCHECK", "off")

    started = manager_daemon.run_loop(
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": True},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-08-17T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
    )
    assert started is True


# --------------------------------------------------------------------------- #
# 2) 但派不了工：fail-closed 的性質原封不動，只是搬到派工當下
# --------------------------------------------------------------------------- #
def test_create_still_fails_closed_and_message_stays_actionable(
    undeclared_repo_root: Path,
) -> None:
    """訊息必須留在**可操作**的那一版：說得出缺什麼、以及為什麼不猜 cwd。"""
    creator = seams.ScriptWorktreeCreator()
    with pytest.raises(paths.RepoRootUnresolvedError) as excinfo:
        creator.create("feature/633-probe", job_id="job-633")
    message = str(excinfo.value)
    assert "PSC_REPO_ROOT" in message
    assert "cwd" in message
    assert "allow_cwd" in message


def test_repo_root_property_still_fails_closed(undeclared_repo_root: Path) -> None:
    """`repo_root` 是公開的讀取面（`manager` 的錨定比較用過它），同樣不得猜。"""
    creator = seams.ScriptWorktreeCreator()
    with pytest.raises(paths.RepoRootUnresolvedError):
        _ = creator.repo_root


def test_create_leaves_no_worktree_pool_behind(undeclared_repo_root: Path) -> None:
    """fail-closed 必須發生在**任何磁碟動作之前**——不得先 mkdir 再後悔。"""
    creator = seams.ScriptWorktreeCreator()
    with pytest.raises(paths.RepoRootUnresolvedError):
        creator.create("feature/633-probe", job_id="job-633")
    pool = undeclared_repo_root.parent / f"{undeclared_repo_root.name}-worktrees"
    assert not pool.exists()
    assert list(undeclared_repo_root.parent.iterdir()) == [undeclared_repo_root]


def test_anchored_at_reports_false_instead_of_raising(undeclared_repo_root: Path) -> None:
    """`anchored_at()` 是唯一不拋的新入口——但它回的是「不能用」，不是猜出來的路徑。"""
    creator = seams.ScriptWorktreeCreator()
    assert creator.anchored_at(undeclared_repo_root) is False
    assert creator.anchored_at(Path("/nonexistent")) is False
    # 且問過之後仍然沒有把任何值 memoize 進去：真的要動手時照樣拋。
    with pytest.raises(paths.RepoRootUnresolvedError):
        _ = creator.repo_root


# --------------------------------------------------------------------------- #
# 3) 顯式路徑不受影響；解析只發生一次
# --------------------------------------------------------------------------- #
def test_explicit_repo_never_consults_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, undeclared_repo_root: Path
) -> None:
    """顯式給定時完全不碰 `paths.repo_root()`——連 lazy 的那次都不該發生。"""
    calls: list[str] = []

    def _boom(**_kwargs: object) -> Path:
        calls.append("repo_root")
        raise AssertionError("顯式 repo 不得回頭問 paths.repo_root()")

    monkeypatch.setattr(paths, "repo_root", _boom)
    explicit = make_fake_repo(tmp_path / "declared")
    creator = seams.ScriptWorktreeCreator(repo=explicit, wt_root=tmp_path / "pool")

    assert creator.repo_root == explicit
    assert creator.anchored_at(explicit) is True
    assert calls == []


def test_declared_env_resolves_lazily_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """宣告了就照常解析，但只解析一次（memoize＝一個 creator 一棵樹）。"""
    declared = make_fake_repo(tmp_path / "declared")
    monkeypatch.setenv("PSC_REPO_ROOT", str(declared))
    monkeypatch.setenv("PSC_WORKTREE_ROOT", str(tmp_path / "pool"))

    calls: list[str] = []
    real_repo_root = paths.repo_root

    def _counting(**kwargs: object) -> Path:
        calls.append("repo_root")
        return real_repo_root(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(paths, "repo_root", _counting)
    creator = seams.ScriptWorktreeCreator()
    assert calls == []  # 建構子不解析

    assert creator.repo_root == declared
    assert creator.repo_root == declared
    assert calls == ["repo_root"]

    # env 在 creator 存活期間被改掉也不會讓它換一棵樹（凍結語意與舊實作一致）。
    monkeypatch.setenv("PSC_REPO_ROOT", str(make_fake_repo(tmp_path / "moved")))
    assert creator.repo_root == declared
