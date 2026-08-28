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


def test_install_service_rejects_project_config_symlink_before_append(
    tmp_path: Path,
) -> None:
    """A project-config symlink is rejected before migration can replace its leaf."""
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo" / "target")
    other = _init_git_repo(tmp_path / "repo" / "other")
    config_root = tmp_path / "config"
    config_root.mkdir()
    real_config = tmp_path / "operator-owned" / "project-cortex.yaml"
    real_config.parent.mkdir()
    real_config.write_text(
        yaml.safe_dump(
            {"workspaces": [{"name": "other", "path": str(other)}]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    project_config = config_root / "project-cortex.yaml"
    project_config.symlink_to(real_config)
    (config_root / "model-identities.yaml").write_text(
        "schema_version: 3\nidentities: []\n", encoding="utf-8"
    )
    env_file = tmp_path / "instance.env"
    env_file.write_text(f"PSC_PROJECT_CONFIG_ROOT={config_root}\n", encoding="utf-8")
    before_real_config = real_config.read_bytes()
    before_env = env_file.read_bytes()

    with pytest.raises(ValueError, match="symlink") as exc_info:
        installer._migrate_instance_config(
            env_file=env_file,
            existing={"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
            config_root=config_root,
            repo_root=target,
            managed_env={"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
        )

    assert str(project_config) in str(exc_info.value)
    assert project_config.is_symlink()
    assert real_config.read_bytes() == before_real_config
    assert env_file.read_bytes() == before_env
    assert not list(config_root.glob("*.bak-*"))
    assert not (config_root / ".cortex-migration.lock").exists()


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


def test_install_service_backup_cleanup_failure_does_not_mask_migration_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cleanup failure must not replace the original migration exception."""
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

    def fail_env_write(*args, **kwargs):
        raise OSError("injected env write failure")

    real_unlink = Path.unlink

    def fail_backup_cleanup(path: Path, missing_ok: bool = False):
        if path.name.startswith("project-cortex.yaml.bak-"):
            raise OSError("injected backup cleanup failure")
        return real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(installer, "_write_managed_env", fail_env_write)
    monkeypatch.setattr(Path, "unlink", fail_backup_cleanup)

    with caplog.at_level("WARNING", logger="paulsha_cortex.deploy.installer"):
        with pytest.raises(OSError, match="injected env write failure"):
            installer._migrate_instance_config(
                env_file=env_file,
                existing={"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
                config_root=config_root,
                repo_root=target,
                managed_env={"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
            )

    assert any(
        "migration backup cleanup failed" in record.getMessage()
        and "project-cortex.yaml.bak-" in record.getMessage()
        for record in caplog.records
    )


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
    identities = config_root / "model-identities.yaml"
    identities.write_text(
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
    message = str(exc_info.value)
    assert (
        f"{project_config.resolve()}=有備份:{backups[0].resolve()}" in message
    )
    for path in (env_file, identities):
        assert (
            f"{path.resolve()}=無備份；"
            "pre-migration 內容只存在於記憶體中的 previous"
        ) in message
    assert isinstance(exc_info.value.__cause__, OSError)


def test_install_service_rollback_reports_unbacked_paths_when_restore_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rollback failure before backup creation must identify risky files."""
    from paulsha_cortex.deploy import installer

    target = _init_git_repo(tmp_path / "repo" / "target")
    config_root = tmp_path / "config"
    config_root.mkdir()
    env_file = tmp_path / "instance.env"
    project_config = config_root / "project-cortex.yaml"
    identities = config_root / "model-identities.yaml"

    def fail_env_write(*args, **kwargs):
        raise OSError("injected env write failure")

    def fail_restore(*args, **kwargs):
        raise OSError("injected restore failure")

    monkeypatch.setattr(installer, "_write_managed_env", fail_env_write)
    monkeypatch.setattr(installer, "_restore_file", fail_restore)

    with pytest.raises(ValueError, match="未取得備份") as exc_info:
        installer._migrate_instance_config(
            env_file=env_file,
            existing={},
            config_root=config_root,
            repo_root=target,
            managed_env={"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
        )

    message = str(exc_info.value)
    assert "pre-migration 內容只存在於記憶體中的 previous" in message
    for path in (env_file, project_config, identities):
        assert str(path.resolve()) in message
    assert not list(config_root.glob("*.bak-*"))
    assert isinstance(exc_info.value.__cause__, OSError)


def test_restore_file_replaces_atomically_and_preserves_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paulsha_cortex.deploy import installer

    path = tmp_path / "runtime.env"
    path.write_bytes(b"before\n")
    path.chmod(0o640)
    replacements: list[tuple[Path, Path]] = []
    real_replace = installer.os.replace

    def tracked_replace(source: Path, destination: Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(installer.os, "replace", tracked_replace)

    installer._restore_file(path, b"after\n")

    assert path.read_bytes() == b"after\n"
    assert path.stat().st_mode & 0o7777 == 0o640
    assert len(replacements) == 1
    source, destination = replacements[0]
    assert source.parent == path.parent
    assert source.name.startswith(f".{path.name}.restore-")
    assert destination == path
    assert not list(tmp_path.glob(f".{path.name}.restore-*"))


@pytest.mark.parametrize(
    "loader_error", [OSError("loader I/O"), ValueError("loader env")]
)
def test_load_project_config_payload_preserves_loader_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loader_error: Exception,
) -> None:
    from paulsha_cortex.deploy import installer
    from paulsha_cortex.monitor import config as monitor_config

    config_path = tmp_path / "project-cortex.yaml"
    config_path.write_text(
        "workspaces:\n  - name: demo\n    path: /tmp/demo\n", encoding="utf-8"
    )

    def fail_load(*, config_path: Path):
        raise loader_error

    monkeypatch.setattr(monitor_config, "load_config", fail_load)

    with pytest.raises(ValueError, match="project config 驗證失敗") as exc_info:
        installer._load_project_config_payload(config_path)

    assert str(loader_error) in str(exc_info.value)
    assert exc_info.value.__cause__ is loader_error


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


@pytest.mark.parametrize("source_mode", [0o600, 0o644])
def test_backup_file_preserves_source_mode_under_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_mode: int
) -> None:
    from paulsha_cortex.deploy import installer

    source = tmp_path / "project-cortex.yaml"
    source.write_bytes(b"workspaces: []\n")
    source.chmod(source_mode)
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

    previous_umask = os.umask(0o077)
    try:
        backup = installer._backup_file(source)
    finally:
        os.umask(previous_umask)

    assert len(open_calls) == 1
    flags, requested_mode = open_calls[0]
    assert flags & os.O_WRONLY
    assert flags & os.O_CREAT
    assert flags & os.O_EXCL
    assert requested_mode == source_mode
    assert source.stat().st_mode & 0o7777 == source_mode
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
