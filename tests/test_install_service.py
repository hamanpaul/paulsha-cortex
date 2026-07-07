import subprocess
import sys

import pytest

from paulsha_cortex.deploy.installer import render_units


def _init_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_render_substitutes_instance_and_script(tmp_path):
    units = render_units(instance="alpha", interval=120)
    service = units["alpha-manager.service"]
    assert "__INSTANCE__" not in service and "__SERVICE_SCRIPT__" not in service
    assert "alpha persona manager service" in service
    timer = units["alpha-manager.timer"]
    assert "OnUnitActiveSec=120" in timer


def test_install_is_idempotent(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert installer.main(["service", "--instance", "beta"]) == 0
    first = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert installer.main(["service", "--instance", "beta"]) == 0
    second = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert first == second


def test_install_writes_current_python_to_env_file(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert installer.main(["service", "--instance", "beta"]) == 0

    env_file = tmp_path / ".agents" / "core" / "runtime" / "beta-manager.env"
    env_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert f"PY={sys.executable}" in env_lines


def test_install_writes_git_repo_root_to_env_file(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    repo_root = _init_git_repo(tmp_path / "repo")
    work_dir = repo_root / "nested"
    work_dir.mkdir()

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(work_dir)

    assert installer.main(["service", "--instance", "beta"]) == 0

    env_file = tmp_path / "home" / ".agents" / "core" / "runtime" / "beta-manager.env"
    env_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert f"PSC_REPO_ROOT={repo_root.resolve()}" in env_lines


def test_install_rejects_non_git_repo_root(tmp_path, monkeypatch, capsys):
    from paulsha_cortex.deploy import installer

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    bad_root = tmp_path / "not-a-repo"
    bad_root.mkdir()

    with pytest.raises(SystemExit) as exc:
        installer.main(["service", "--repo-root", str(bad_root)])

    assert exc.value.code == 2
    assert f"{bad_root.resolve()} 不是 git repo" in capsys.readouterr().err
