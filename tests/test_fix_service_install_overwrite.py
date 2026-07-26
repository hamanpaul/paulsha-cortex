import subprocess
from pathlib import Path

import pytest


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_install_service_refuses_or_preserves_conflicting_existing_python_config(
    tmp_path, monkeypatch
):
    from paulsha_cortex.deploy import installer

    existing_python = "/tmp/venv-a/bin/python"
    repo_root = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    runtime_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text(
        f"PY={existing_python}\nPSC_AGENTS_ROOT=/tmp/agents-a\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer.sys, "executable", "/tmp/venv-b/bin/python")
    monkeypatch.chdir(repo_root)

    before = runtime_file.read_text(encoding="utf-8")
    try:
        installer.install_service("beta", 300, repo_root)
    except ValueError:
        assert runtime_file.read_text(encoding="utf-8") == before
    else:
        assert runtime_file.read_text(encoding="utf-8") == before


def test_install_service_allows_existing_config_with_same_python(
    tmp_path, monkeypatch
):
    from paulsha_cortex.deploy import installer

    same_python = "/tmp/venv-a/bin/python"
    repo_root = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    runtime_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text(
        f"PY={same_python}\nPSC_AGENTS_ROOT=/tmp/agents-a\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer.sys, "executable", same_python)
    monkeypatch.chdir(repo_root)

    assert installer.install_service("beta", 300, repo_root) == 0
    assert runtime_file.read_text(encoding="utf-8").splitlines()[0].startswith(f"PY={same_python}")


def test_install_service_repairs_invalid_existing_manager_env(
    tmp_path, monkeypatch
):
    from paulsha_cortex.deploy import installer

    repo_root = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    runtime_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text("PSC_AGENTS_ROOT=./relative-path\n", encoding="utf-8")
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo_root)

    assert installer.install_service("beta", 300, repo_root) == 0
    assert "PSC_AGENTS_ROOT=./relative-path" not in runtime_file.read_text(encoding="utf-8")


def test_install_service_first_install_creates_managed_env(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    repo_root = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    runtime_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo_root)

    assert not runtime_file.exists()
    assert installer.install_service("beta", 300, repo_root) == 0
    assert runtime_file.exists()
    assert f"PSC_REPO_ROOT={repo_root}" in runtime_file.read_text(encoding="utf-8")
