from __future__ import annotations

import fcntl
import os
import re
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
                ],
                "monitor": {
                    "poll_interval_seconds": 17,
                    "ignore_dirs": [".git", ".pytest_cache"],
                },
                "hippo": {
                    "projects": [
                        {"slug": "shared-memory", "roots": [str(existing_a)]}
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    project_config.chmod(0o600)
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
    assert payload["monitor"] == {
        "poll_interval_seconds": 17,
        "ignore_dirs": [".git", ".pytest_cache"],
    }
    assert payload["hippo"] == {
        "projects": [{"slug": "shared-memory", "roots": [str(existing_a)]}]
    }
    backups = sorted(config_root.glob("project-cortex.yaml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before_project
    assert project_config.stat().st_mode & 0o777 == 0o600
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert re.fullmatch(
        r"project-cortex\.yaml\.bak-\d{8}T\d{12}Z", backups[0].name
    )
    assert env_file.read_bytes() != before_env


def test_install_service_preserves_existing_non_exact_workspace_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A matching operator entry is never rewritten or promoted by install."""
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo" / "target")
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
                        "name": "operator-target",
                        "path": str(target),
                        "exact_project": False,
                    }
                ],
                "monitor": {"ignore_dirs": [".cache"]},
                "hippo": {"projects": [{"slug": "ambient", "roots": [str(target)]}]},
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
    assert project_config.read_bytes() == before_project
    assert not list(config_root.glob("project-cortex.yaml.bak-*"))
    assert yaml.safe_load(project_config.read_text(encoding="utf-8"))["workspaces"][0][
        "exact_project"
    ] is False
    assert env_file.read_bytes() != before_env


def test_install_service_rollback_removes_migration_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed migration restores files and removes only its new backup."""
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo" / "target")
    other = _init_git_repo(tmp_path / "repo" / "other")
    config_root = tmp_path / "config"
    config_root.mkdir()
    project_config = config_root / "project-cortex.yaml"
    project_config.write_text(
        yaml.safe_dump(
            {"workspaces": [{"name": "other", "path": str(other)}]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_root / "model-identities.yaml").write_text(
        "schema_version: 3\nidentities: []\n", encoding="utf-8"
    )
    env_file = tmp_path / "instance.env"
    env_file.write_text(f"PSC_PROJECT_CONFIG_ROOT={config_root}\n", encoding="utf-8")
    before_project = project_config.read_bytes()
    before_env = env_file.read_bytes()

    def fail_env_write(*args, **kwargs):
        raise OSError("injected env write failure")

    monkeypatch.setattr(installer, "_write_managed_env", fail_env_write)

    with pytest.raises(OSError, match="injected env write failure"):
        installer._migrate_instance_config(
            env_file=env_file,
            existing={"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
            config_root=config_root,
            repo_root=target,
            managed_env={"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
        )

    assert project_config.read_bytes() == before_project
    assert env_file.read_bytes() == before_env
    assert not list(config_root.glob("project-cortex.yaml.bak-*"))


def test_install_service_rollback_retains_backup_when_restore_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore failure must leave the pre-migration bytes recoverable."""
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo" / "target")
    other = _init_git_repo(tmp_path / "repo" / "other")
    config_root = tmp_path / "config"
    config_root.mkdir()
    project_config = config_root / "project-cortex.yaml"
    project_config.write_text(
        yaml.safe_dump(
            {"workspaces": [{"name": "other", "path": str(other)}]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_root / "model-identities.yaml").write_text(
        "schema_version: 3\nidentities: []\n", encoding="utf-8"
    )
    env_file = tmp_path / "instance.env"
    env_file.write_text(f"PSC_PROJECT_CONFIG_ROOT={config_root}\n", encoding="utf-8")
    before_project = project_config.read_bytes()

    def fail_env_write(*args, **kwargs):
        raise OSError("injected env write failure")

    def fail_restore(*args, **kwargs):
        raise OSError("injected restore failure")

    monkeypatch.setattr(installer, "_write_managed_env", fail_env_write)
    monkeypatch.setattr(installer, "_restore_file", fail_restore)

    with pytest.raises(ValueError, match="migration rollback 失敗") as exc_info:
        installer._migrate_instance_config(
            env_file=env_file,
            existing={"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
            config_root=config_root,
            repo_root=target,
            managed_env={"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
        )

    backups = sorted(config_root.glob("project-cortex.yaml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before_project
    assert str(backups[0].resolve()) in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, OSError)


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
    assert f"psc_agents_root={foreign_agents_root}" in message
    assert f"home={home}" in message
    assert env_file.read_bytes() == before_env
    assert not foreign_agents_root.exists()


def test_install_service_rejects_process_agents_root_from_different_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An implicit process env root must obey HOME regardless of its basename."""
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "current-home"
    foreign_agents_root = tmp_path / "foreign-home" / "external-root"

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
    assert not (config_root / ".cortex-migration.lock").exists()


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
    assert not (config_root / ".cortex-migration.lock").exists()


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

    with pytest.raises(ValueError, match="修復或移走"):
        installer.install_service_result("hippo", 300, target)

    assert project_config.read_bytes() == before_project
    assert not list(config_root.glob("project-cortex.yaml.bak-*"))
    assert not (config_root / ".cortex-migration.lock").exists()


def test_backup_file_uses_exclusive_source_mode_when_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paulsha_cortex.deploy import installer

    source = tmp_path / "project-cortex.yaml"
    source.write_bytes(b"workspaces: []\n")
    source.chmod(0o600)
    source_mode = source.stat().st_mode & 0o7777
    open_calls: list[tuple[int, int]] = []
    real_open = installer.os.open

    def tracked_open(path, flags, mode=0o777, *args, **kwargs):
        candidate = Path(path)
        if candidate.parent == source.parent and candidate.name.startswith(
            "project-cortex.yaml.bak-"
        ):
            open_calls.append((flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(installer.os, "open", tracked_open)

    backup = installer._backup_file(source)

    assert len(open_calls) == 1
    flags, requested_mode = open_calls[0]
    assert flags & os.O_WRONLY
    assert flags & os.O_CREAT
    assert flags & os.O_EXCL
    assert requested_mode == source_mode
    assert backup.stat().st_mode & 0o7777 == source_mode


def test_instance_config_migration_locks_read_through_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo" / "target")
    other = _init_git_repo(tmp_path / "repo" / "other")
    config_root = tmp_path / "config"
    config_root.mkdir()
    project_config = config_root / "project-cortex.yaml"
    project_config.write_text(
        yaml.safe_dump(
            {"workspaces": [{"name": "other", "path": str(other)}]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_root / "model-identities.yaml").write_text(
        "schema_version: 3\nidentities: []\n", encoding="utf-8"
    )
    env_file = tmp_path / "instance.env"
    events: list[str] = []
    lock_held = False
    real_flock = fcntl.flock
    real_load = installer._load_project_config_payload
    real_replace = installer.os.replace

    def tracked_flock(fd, operation):
        nonlocal lock_held
        if operation == fcntl.LOCK_EX:
            assert not lock_held
            lock_held = True
            events.append("lock")
        elif operation == fcntl.LOCK_UN:
            assert lock_held
            events.append("unlock")
            lock_held = False
        return real_flock(fd, operation)

    def tracked_load(config_path: Path):
        events.append("read")
        return real_load(config_path)

    def tracked_replace(source: Path, destination: Path):
        assert lock_held
        events.append("replace")
        return real_replace(source, destination)

    monkeypatch.setattr(installer.fcntl, "flock", tracked_flock)
    monkeypatch.setattr(installer, "_load_project_config_payload", tracked_load)
    monkeypatch.setattr(installer.os, "replace", tracked_replace)

    installer._migrate_instance_config(
        env_file=env_file,
        existing={"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
        config_root=config_root,
        repo_root=target,
        managed_env={"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
    )

    assert events == ["read", "lock", "read", "replace", "unlock"]
    assert not lock_held
