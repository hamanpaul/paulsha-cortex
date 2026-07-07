from pathlib import Path

from paulsha_cortex.config import paths


def test_defaults_under_agents(monkeypatch):
    for var in ("PSC_AGENTS_ROOT", "PSC_CONTROL_ROOT", "PSC_COORDINATOR_ROOT", "PSC_SPECS_ROOT"):
        monkeypatch.delenv(var, raising=False)
    home = Path.home()
    assert paths.control_root() == home / ".agents" / "control"
    assert paths.coordinator_root() == home / ".agents" / "coordinator"
    assert paths.specs_root() == home / ".agents" / "specs"


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path / "ctl"))
    assert paths.control_root() == tmp_path / "ctl"


def test_repo_root_env_then_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path))
    assert paths.repo_root() == tmp_path
    monkeypatch.delenv("PSC_REPO_ROOT")
    assert paths.repo_root() == Path.cwd()


def test_worktree_root_is_repo_sibling(monkeypatch, tmp_path):
    monkeypatch.delenv("PSC_WORKTREE_ROOT", raising=False)
    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path / "myrepo"))
    assert paths.worktree_root() == tmp_path / "myrepo-worktrees"
