from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _instance_env_file(home: Path, instance: str) -> Path:
    return home / ".agents" / "core" / "runtime" / f"{instance}-manager.env"


def _write_instance_env(
    home: Path,
    agents_root: Path,
    config_root: Path,
    *,
    instance: str = "hippo",
) -> Path:
    env_file = _instance_env_file(home, instance)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        f"PSC_AGENTS_ROOT={agents_root}\n"
        f"PSC_PROJECT_CONFIG_ROOT={config_root}\n",
        encoding="utf-8",
    )
    return env_file


def _prepare_installer(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    from paulsha_cortex.deploy import installer

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PSC_AGENTS_ROOT", raising=False)
    monkeypatch.delenv("PSC_PROJECT_CONFIG_ROOT", raising=False)
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)


def test_install_service_appends_workspace_and_backs_up_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: adopting a shared config must append the target and retain its history."""
    from paulsha_cortex.deploy import installer

    repo_root = tmp_path / "repos"
    existing_a = _init_git_repo(repo_root / "existing-a")
    existing_b = _init_git_repo(repo_root / "existing-b")
    target = _init_git_repo(repo_root / "target")
    home = tmp_path / "home"
    agents_root = home / ".agents"
    config_root = agents_root / "config" / "paulsha"
    config_root.mkdir(parents=True)
    project_config = config_root / "project-cortex.yaml"
    project_config.write_text(
        yaml.safe_dump(
            {
                "workspaces": [
                    {"name": "existing-a", "path": str(existing_a)},
                    {"name": "existing-b", "path": str(existing_b)},
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_root / "model-identities.yaml").write_text(
        "schema_version: 3\nidentities: []\n", encoding="utf-8"
    )
    env_file = _write_instance_env(home, agents_root, config_root)
    before_project = project_config.read_bytes()
    before_env = env_file.read_bytes()

    _prepare_installer(monkeypatch, home)

    result = installer.install_service_result("hippo", 300, target)

    assert result.exit_code == 0
    payload = yaml.safe_load(project_config.read_text(encoding="utf-8"))
    assert [
        (row["name"], Path(row["path"]).resolve(), row.get("exact_project", False))
        for row in payload["workspaces"]
    ] == [
        ("existing-a", existing_a.resolve(), False),
        ("existing-b", existing_b.resolve(), False),
        ("target", target.resolve(), True),
    ]
    backups = sorted(config_root.glob("project-cortex.yaml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before_project
    assert env_file.read_bytes() != before_env


def test_install_service_rejects_agents_root_from_different_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: a persisted default agents root from another HOME must fail closed."""
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "current-home"
    foreign_agents_root = tmp_path / "foreign-home" / ".agents"
    env_file = _write_instance_env(
        home,
        foreign_agents_root,
        foreign_agents_root / "config" / "paulsha",
    )
    before_env = env_file.read_bytes()

    _prepare_installer(monkeypatch, home)

    with pytest.raises(ValueError) as exc_info:
        installer.install_service_result("hippo", 300, target)

    message = str(exc_info.value).lower()
    assert "home" in message
    assert "agents" in message
    assert env_file.read_bytes() == before_env
    assert not foreign_agents_root.exists()


def test_install_service_rejects_process_agents_root_from_different_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An implicit process env default root must obey the HOME boundary too."""
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "current-home"
    foreign_agents_root = tmp_path / "foreign-home" / ".agents"

    _prepare_installer(monkeypatch, home)
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(foreign_agents_root))

    with pytest.raises(ValueError, match="HOME"):
        installer.install_service_result("hippo", 300, target)

    assert not foreign_agents_root.exists()


def test_install_service_raises_when_existing_model_identities_are_unloadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: an unloadable identity registry must not be silently replaced."""
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    agents_root = home / ".agents"
    config_root = agents_root / "config" / "paulsha"
    config_root.mkdir(parents=True)
    project_config = config_root / "project-cortex.yaml"
    project_config.write_text(
        yaml.safe_dump(
            {
                "workspaces": [
                    {
                        "name": target.name,
                        "path": str(target),
                        "exact_project": True,
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    identities = config_root / "model-identities.yaml"
    identities.write_text("schema_version: 99\nidentities: []\n", encoding="utf-8")
    env_file = _write_instance_env(home, agents_root, config_root)
    before_project = project_config.read_bytes()
    before_identities = identities.read_bytes()
    before_env = env_file.read_bytes()

    _prepare_installer(monkeypatch, home)

    with pytest.raises(ValueError) as exc_info:
        installer.install_service_result("hippo", 300, target)

    assert "identit" in str(exc_info.value).lower()
    assert project_config.read_bytes() == before_project
    assert identities.read_bytes() == before_identities
    assert env_file.read_bytes() == before_env


def test_install_service_rejects_unloadable_identities_with_invalid_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither invalid shared config may be replaced by a migration scaffold."""
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    agents_root = home / ".agents"
    config_root = agents_root / "config" / "paulsha"
    config_root.mkdir(parents=True)
    project_config = config_root / "project-cortex.yaml"
    identities = config_root / "model-identities.yaml"
    project_config.write_text("workspaces: [\n", encoding="utf-8")
    identities.write_text("schema_version: 99\nidentities: []\n", encoding="utf-8")
    before_project = project_config.read_bytes()
    before_identities = identities.read_bytes()

    _prepare_installer(monkeypatch, home)

    with pytest.raises(ValueError, match="model-identities"):
        installer.install_service_result("hippo", 300, target)

    assert project_config.read_bytes() == before_project
    assert identities.read_bytes() == before_identities
    assert not list(config_root.glob("project-cortex.yaml.bak-*"))
    assert not list(config_root.glob("model-identities.yaml.bak-*"))


def test_install_service_rejects_existing_unparseable_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing project config must not be scaffold-replaced when unreadable."""
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    agents_root = home / ".agents"
    config_root = agents_root / "config" / "paulsha"
    config_root.mkdir(parents=True)
    project_config = config_root / "project-cortex.yaml"
    project_config.write_text("workspaces: [\n", encoding="utf-8")
    (config_root / "model-identities.yaml").write_text(
        "schema_version: 3\nidentities: []\n", encoding="utf-8"
    )
    before_project = project_config.read_bytes()

    _prepare_installer(monkeypatch, home)

    with pytest.raises(ValueError, match="project config"):
        installer.install_service_result("hippo", 300, target)

    assert project_config.read_bytes() == before_project
    assert not list(config_root.glob("project-cortex.yaml.bak-*"))
