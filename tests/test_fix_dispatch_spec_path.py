"""`_infer_repo_root` 的推斷契約（#565 後 hermetic 化）。

本檔的推斷測試**不得**依賴 host `/tmp` 當下乾淨。#565 實測：agent sandbox
基礎設施會在 sandbox 存活期間於 `/tmp` 暫態 `mkdir` 一個空的 `.git`，舊判準
（`.git` 存在即 repo 根）會讓任何 `/tmp` 底下的 spec 路徑（含 pytest 的
`tmp_path`）被推斷成 `/tmp`，於是這裡的測試在「當下剛好有 sandbox 存活」時
必紅，Manager gate ledger 的全套 pytest 因此拒掉合格 candidate。

hermetic 手法有二，兩者都用：
1. 假 repo 一律用 `git_fixtures.make_fake_repo()` 建**完整**標記（`.git/HEAD`），
   不再只 `mkdir` 一個空 `.git`——空目錄現在依契約就不是 repo 根；
2. 污染由測試**自備**（`make_empty_git_dir()` / monkeypatch 搜尋上界），
   host `/tmp` 有沒有 `.git` 都不影響結果。

#612 起再加一條：推斷**不得**有任何 cwd 通道。舊實作有兩條——相對 spec 路徑的
`Path.resolve()`，以及 `paths.repo_root()` 未宣告時的 `Path.cwd()` 預設——兩條都
會把 repo 根解析成 daemon 的工作目錄（＝ operator 的真實 checkout）。因此本檔的
測試一律以**絕對** spec 路徑餵入，並在需要驗「未宣告」行為時顯式
`delenv("PSC_REPO_ROOT")`（conftest 預設把它指向 per-test 暫存路徑）。
"""

from pathlib import Path

import pytest

from git_fixtures import make_empty_git_dir, make_fake_repo
from paulsha_cortex.coordinator import autonomy
from paulsha_cortex.coordinator.autonomy import RepoRootResolutionError, _infer_repo_root


def _isolated_cwd(tmp_path: Path) -> Path:
    """驗「向上搜尋」分支時用的無關 cwd。

    #612 後 cwd 已不參與推斷，這個 helper 因此不再是必要條件，而是**反向**斷言的
    載體：把 cwd 換到一個與受測路徑無祖孫關係的空目錄，若哪天推斷又偷偷讀了 cwd，
    結果就會與斷言不符。
    """
    cwd = tmp_path / "unrelated-cwd"
    cwd.mkdir(exist_ok=True)
    return cwd


def test_infer_repo_root_prefers_configured_repo_root_for_external_spec(monkeypatch, tmp_path):
    repo_root = make_fake_repo(tmp_path / "repo")
    spec_dir = tmp_path / "agents" / "specs"
    spec_path = spec_dir / "foo-spec.md"
    spec_dir.mkdir(parents=True)
    spec_path.write_text("# foo\n", encoding="utf-8")
    # `~/.agents` 樹即使是**有效** repo 也不得被當成 spec 的 repo 根（名稱規則）。
    make_fake_repo(tmp_path / "agents")

    monkeypatch.setenv("PSC_REPO_ROOT", str(repo_root))

    assert _infer_repo_root(spec_path) == repo_root


def test_infer_repo_root_keeps_in_repo_path_for_repo_relative_spec(monkeypatch, tmp_path):
    repo_root = make_fake_repo(tmp_path / "repo")
    spec_path = repo_root / "specs" / "foo-spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# foo\n", encoding="utf-8")

    monkeypatch.setenv("PSC_REPO_ROOT", str(repo_root))

    assert _infer_repo_root(spec_path) == repo_root


def test_infer_repo_root_never_falls_back_to_cwd_repo(monkeypatch, tmp_path):
    """#612：spec 在任何 repo 之外、又沒宣告 `PSC_REPO_ROOT` → fail-closed。

    舊實作回 `spec_path.parent`，而 `paths.repo_root()` 的 cwd 預設更會讓「spec
    剛好在 cwd 底下」直接回 cwd。這裡把 cwd 設成一個**真的** repo（重演 daemon
    在 operator checkout 裡跑的形狀）並斷言推斷**不會**採用它。
    """
    repo_root = make_fake_repo(tmp_path / "repo")
    spec_path = tmp_path / "outside" / "specs" / "foo-spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# foo\n", encoding="utf-8")

    monkeypatch.delenv("PSC_REPO_ROOT", raising=False)
    monkeypatch.chdir(repo_root)

    with pytest.raises(RepoRootResolutionError) as excinfo:
        _infer_repo_root(spec_path)
    assert excinfo.value.diagnostic.reason == "repo-root-unresolved"


# --- #565 回歸：空 .git 目錄不得被當 repo 根 ---------------------------------


def test_empty_git_dir_on_path_chain_is_not_a_repo_root(monkeypatch, tmp_path):
    """路徑鏈上有 `mkdir` 出來的空 `.git` 時，推斷必須「穿過」它。

    這是 #565 的核心回歸：污染在 `tmp_path` 內自備，因此 host `/tmp` 的實際
    狀態（有無 sandbox 存活）完全不影響判定。
    """
    shared = tmp_path / "shared"
    real_repo = make_fake_repo(shared / "workspace" / "repo")
    make_empty_git_dir(shared)  # 污染：空 .git，與 sandbox 在 /tmp 造的形狀相同

    spec_path = real_repo / "specs" / "foo-spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# foo\n", encoding="utf-8")

    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path / "elsewhere"))

    assert _infer_repo_root(spec_path) == real_repo


def test_empty_git_dir_alone_does_not_anchor_repo_root(monkeypatch, tmp_path):
    """鏈上**只有**空 `.git`（沒有任何真 repo）時，不得落錨在污染點。

    #565 的原斷言是「落到既有 fallback（`spec.parent`）」；#612 之後那條 fallback
    本身也不存在了，所以斷言改成 fail-closed——重點不變：**污染點不是 repo 根**。
    """
    polluted = tmp_path / "polluted"
    make_empty_git_dir(polluted)
    spec_path = polluted / "nested" / "specs" / "foo-spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# foo\n", encoding="utf-8")

    monkeypatch.delenv("PSC_REPO_ROOT", raising=False)
    monkeypatch.chdir(_isolated_cwd(tmp_path))

    with pytest.raises(RepoRootResolutionError) as excinfo:
        _infer_repo_root(spec_path)
    assert excinfo.value.diagnostic.reason == "repo-root-unresolved"


def test_worktree_git_file_still_counts_as_repo_root(monkeypatch, tmp_path):
    """linked worktree 的 `.git` 是 `gitdir:` **檔案**，仍必須被認為是 repo 根。"""
    worktree = tmp_path / "repo-worktrees" / "feature"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(
        f"gitdir: {tmp_path / 'repo' / '.git' / 'worktrees' / 'feature'}\n",
        encoding="utf-8",
    )
    spec_path = worktree / "specs" / "foo-spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# foo\n", encoding="utf-8")

    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path / "elsewhere"))

    assert _infer_repo_root(spec_path) == worktree


@pytest.mark.parametrize("contents", ["", "not a gitdir pointer\n"])
def test_git_file_without_gitdir_pointer_is_not_a_repo_root(tmp_path, contents):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / ".git").write_text(contents, encoding="utf-8")

    assert autonomy._is_git_repo_root(candidate) is False


# --- #565 回歸：共享暫存根是搜尋上界 -----------------------------------------


def test_search_stops_at_shared_temp_root(monkeypatch, tmp_path):
    """即使共享根上是**有效** repo（有人在 `/tmp` 跑過 `git init`），也不落錨。

    以 monkeypatch 換掉搜尋上界，用 `tmp_path` 內的假共享根重演此情境，
    不依賴 host `/tmp` 真的被 `git init` 過。
    """
    shared = tmp_path / "fake-tmp"
    make_fake_repo(shared)  # 共享根本身是有效 repo，但仍不該被採用
    spec_path = shared / "job-1234" / "specs" / "foo-spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# foo\n", encoding="utf-8")

    monkeypatch.setattr(autonomy, "_repo_search_boundaries", lambda: frozenset({shared}))
    monkeypatch.delenv("PSC_REPO_ROOT", raising=False)
    monkeypatch.chdir(_isolated_cwd(tmp_path))

    with pytest.raises(RepoRootResolutionError) as excinfo:
        _infer_repo_root(spec_path)
    assert excinfo.value.diagnostic.reason == "repo-root-unresolved"


def test_repo_below_shared_temp_root_still_resolves(monkeypatch, tmp_path):
    """上界只擋共享根**自己**：其下的真 repo 照常命中。"""
    shared = tmp_path / "fake-tmp"
    real_repo = make_fake_repo(shared / "job-1234" / "repo")
    spec_path = real_repo / "specs" / "foo-spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# foo\n", encoding="utf-8")

    monkeypatch.setattr(autonomy, "_repo_search_boundaries", lambda: frozenset({shared}))
    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path / "elsewhere"))

    assert _infer_repo_root(spec_path) == real_repo


def test_real_tmp_is_a_search_boundary():
    """契約釘選：`/tmp` 與 `TMPDIR` 都在上界集合內（#565 的實際污染點）。"""
    boundaries = autonomy._repo_search_boundaries()
    assert Path("/tmp").resolve() in boundaries
    import tempfile

    assert Path(tempfile.gettempdir()).resolve() in boundaries
