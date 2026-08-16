from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml


def _init_project(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".project-policy.yml").write_text(
        "policy_profile: flat\n",
        encoding="utf-8",
    )
    return path


def _legacy_instance_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repo_parent = tmp_path / "repos"
    target_repo = _init_project(repo_parent / "hippo")
    _init_project(repo_parent / "sibling")
    home = tmp_path / "home"
    agents_root = home / ".agents" / "instances" / "hippo-open-issues"
    env_file = home / ".agents" / "core" / "runtime" / "hippo-manager.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        f"PSC_AGENTS_ROOT={agents_root}\n"
        f"PSC_REPO_ROOT={target_repo}\n",
        encoding="utf-8",
    )
    return home, agents_root, target_repo, env_file


def _env_values(path: Path) -> dict[str, str]:
    return {
        key: value
        for key, value in (
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
        )
    }


def test_install_service_migrates_legacy_instance_to_one_exact_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#518 RED: legacy env adoption must materialize an isolated monitor config."""
    from paulsha_cortex.deploy import installer
    from paulsha_cortex.monitor.config import load_config
    from paulsha_cortex.monitor.scanner import scan_workspaces

    home, agents_root, target_repo, _env_file = _legacy_instance_fixture(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PSC_AGENTS_ROOT", raising=False)
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)

    assert installer.main(["service", "--instance", "hippo", "--repo-root", str(target_repo)]) == 0

    config_root = agents_root / "config" / "paulsha"
    project_config = config_root / "project-cortex.yaml"
    identities = config_root / "model-identities.yaml"
    assert _env_values(home / ".agents" / "core" / "runtime" / "hippo-manager.env")[
        "PSC_PROJECT_CONFIG_ROOT"
    ] == str(config_root)
    assert project_config.is_file()
    assert identities.is_file()
    project_payload = yaml.safe_load(project_config.read_text(encoding="utf-8"))
    assert isinstance(project_payload, dict)
    assert isinstance(project_payload.get("workspaces"), list)
    assert project_payload["workspaces"]
    assert isinstance(yaml.safe_load(identities.read_text(encoding="utf-8")), dict)

    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(config_root))
    monkeypatch.setenv("PSC_REPO_ROOT", str(target_repo))
    config = load_config()
    states = scan_workspaces(config)
    assert {Path(state.path).resolve() for state in states} == {target_repo.resolve()}

    from paulsha_cortex.doctor import run_doctor

    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_preflight_command",
        lambda environment: ("true",),
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_model_identities",
        lambda config_root: 1,
    )
    report = run_doctor(
        probe_live=False,
        instance="hippo",
        env={
            "HOME": str(home),
            "PSC_REPO_ROOT": str(target_repo),
            "PSC_PROJECT_CONFIG_ROOT": str(config_root),
        },
        home=home,
    )
    drift = next(probe for probe in report.probes if probe.name == "managed-path-drift")
    assert drift.status == "pass"


def test_install_service_rolls_back_legacy_adoption_after_managed_env_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#518 RED: a failed migration must restore env and generated config files."""
    from paulsha_cortex.deploy import installer

    home, agents_root, target_repo, env_file = _legacy_instance_fixture(tmp_path)
    config_root = agents_root / "config" / "paulsha"
    config_root.mkdir(parents=True)
    project_config = config_root / "project-cortex.yaml"
    identities = config_root / "model-identities.yaml"
    project_config.write_text("legacy: project-config\n", encoding="utf-8")
    identities.write_text("legacy: identities\n", encoding="utf-8")
    before_env = env_file.read_bytes()
    before_project = project_config.read_bytes()
    before_identities = identities.read_bytes()

    original_write_managed_env = installer._write_managed_env

    def fail_after_env_write(*args, **kwargs):
        original_write_managed_env(*args, **kwargs)
        raise RuntimeError("injected migration failure")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PSC_AGENTS_ROOT", raising=False)
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setattr(installer, "_write_managed_env", fail_after_env_write)

    with pytest.raises(RuntimeError, match="injected migration failure"):
        installer.main(["service", "--instance", "hippo", "--repo-root", str(target_repo)])

    assert env_file.read_bytes() == before_env
    assert project_config.read_bytes() == before_project
    assert identities.read_bytes() == before_identities


def test_doctor_warns_when_instances_share_project_config_root_and_repo_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#518 RED: one instance-local doctor run must expose shared-root impact."""
    from paulsha_cortex.doctor import run_doctor

    home, _agents_root, target_repo, _env_file = _legacy_instance_fixture(tmp_path)
    sibling_repo = target_repo.parent / "sibling"
    shared_root = home / ".agents" / "config" / "paulsha"
    shared_root.mkdir(parents=True)
    (shared_root / "project-cortex.yaml").write_text(
        "workspaces:\n"
        f"  - name: shared\n    path: {target_repo.parent}\n",
        encoding="utf-8",
    )
    (shared_root / "model-identities.yaml").write_text(
        "schema_version: 1\nidentities: []\n",
        encoding="utf-8",
    )
    runtime = home / ".agents" / "core" / "runtime"
    for instance in ("cortex", "hippo"):
        instance_agents = home / ".agents" / "instances" / instance
        (runtime / f"{instance}-manager.env").write_text(
            f"PSC_REPO_ROOT={target_repo}\n"
            f"PSC_AGENTS_ROOT={instance_agents}\n"
            f"PSC_RUN_ROOT={instance_agents / 'run' / instance}\n"
            f"PSC_MONITOR_STATE_ROOT={instance_agents / 'monitor'}\n"
            f"PSC_PROJECT_CONFIG_ROOT={shared_root}\n"
            f"PSC_CONTROL_ROOT={instance_agents / 'control' / instance}\n",
            encoding="utf-8",
        )
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for instance in ("cortex", "hippo"):
        for unit in (f"{instance}-manager.service", f"{instance}-monitor.service"):
            (unit_dir / unit).write_text(
                "[Unit]\n"
                f"EnvironmentFile=-%h/.agents/core/runtime/{instance}-manager.env\n",
                encoding="utf-8",
            )
        (unit_dir / f"{instance}-manager.timer").write_text("[Timer]\n", encoding="utf-8")

    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_preflight_command",
        lambda environment: ("true",),
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_model_identities",
        lambda config_root: 1,
    )
    report = run_doctor(
        probe_live=False,
        instance="hippo",
        env={
            "HOME": str(home),
            "PSC_REPO_ROOT": str(target_repo),
            "PSC_PROJECT_CONFIG_ROOT": str(shared_root),
        },
        home=home,
    )

    assert any(
        probe.status == "warn"
        and "hippo" in probe.detail
        and "cortex" in probe.detail
        and str(sibling_repo) in probe.detail
        and "2" in probe.detail
        for probe in report.probes
    )
