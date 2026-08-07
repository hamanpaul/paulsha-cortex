import subprocess
from pathlib import Path

import pytest


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_install_service_migrates_legacy_env_without_identity_stamp(tmp_path, monkeypatch):
    """#366：既有 env 是 #366 修復前的舊安裝，只有 PY、沒有 PSC_REPO_ROOT／
    PSC_REPO_IDENTITY。此時 PY／repo_root 不一致必須放行並補寫新值（遷移路徑），
    不可 fail-closed——否則本機現役四個 instance 下次 install 全部會壞。"""
    from paulsha_cortex.deploy import installer

    existing_python = "/tmp/venv-a/bin/python"
    new_python = "/tmp/venv-b/bin/python"
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
    monkeypatch.setattr(installer.sys, "executable", new_python)
    monkeypatch.chdir(repo_root)

    assert installer.install_service("beta", 300, repo_root) == 0

    content = runtime_file.read_text(encoding="utf-8")
    assert f"PY={new_python}" in content
    assert f"PSC_REPO_ROOT={repo_root}" in content
    assert "PSC_REPO_IDENTITY=" in content


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
