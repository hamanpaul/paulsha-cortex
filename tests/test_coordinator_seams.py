from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator.seams import ScriptWorktreeCreator


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "init", "-b", "main"],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_worktree_creator_reuses_existing_branch_only_when_it_is_base_ancestor(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    branch = "feature/31-terminal-lifecycle-canary"
    _git(repo, "branch", branch)
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-am", "main advances")
    expected = _git(repo, "rev-parse", "main")

    target = Path(
        ScriptWorktreeCreator(repo=repo, wt_root=tmp_path / "worktrees").create(branch)
    )

    assert _git(repo, "rev-parse", branch) == expected
    assert _git(target, "rev-parse", "HEAD") == expected
    assert _git(target, "branch", "--show-current") == branch


def test_worktree_creator_rejects_diverged_existing_branch_without_moving_it(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    branch = "feature/31-terminal-lifecycle-canary"
    _git(repo, "switch", "-c", branch)
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature only")
    branch_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "main.txt")
    _git(repo, "commit", "-m", "main only")

    with pytest.raises(ValueError, match="commits outside requested base"):
        ScriptWorktreeCreator(repo=repo, wt_root=tmp_path / "worktrees").create(branch)

    assert _git(repo, "rev-parse", branch) == branch_head
    assert not (tmp_path / "worktrees" / "feature-31-terminal-lifecycle-canary").exists()
