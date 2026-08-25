"""#623：generated trust-root install state must stay visible to Manager discovery."""

from __future__ import annotations

import subprocess
from pathlib import Path

from paulsha_cortex.doctor import _service_environment_probe
from paulsha_cortex.trust_root import permgen


def _init_git_repo_with_origin(path: Path, url: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)
    return path


def _trust_root_layout(tmp_path: Path) -> tuple[Path, Path, permgen.PathLayout]:
    home = tmp_path / "operator-home"
    unit_root = home / ".config" / "systemd" / "user"
    unit_root.mkdir(parents=True, exist_ok=True)
    layout = permgen.PathLayout(
        agents_root=str(tmp_path / "var" / "lib" / "cortex"),
        worktree_root=str(tmp_path / "var" / "lib" / "cortex" / "worktree"),
        deploy_root=str(tmp_path / "opt" / "cortex"),
        home_root=str(tmp_path / "var" / "lib"),
        source_repo_slugs=("paulsha-cortex",),
    )
    return home, unit_root, layout


def _write_trust_root_env(layout: permgen.PathLayout, *, repo: Path) -> Path:
    env_file = Path(layout.env_file)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "\n".join(
            (
                f"PSC_AGENTS_ROOT={layout.agents_root}",
                f"PSC_RUN_ROOT={layout.run_root}",
                f"PSC_MONITOR_STATE_ROOT={layout.monitor_state_root}",
                f"PSC_PROJECT_CONFIG_ROOT={layout.project_config_root}",
                f"PSC_REPO_ROOT={repo}",
                "PSC_REPO_IDENTITY=git:github.com/hamanpaul/paulsha-cortex",
                "",
            )
        ),
        encoding="utf-8",
    )
    return env_file


def _write_generated_units(
    unit_root: Path,
    layout: permgen.PathLayout,
    *,
    manager_env_file: str | None = None,
    monitor_env_file: str | None = None,
) -> None:
    manager_unit = permgen.build_manager_unit(permgen.FOUR_WAY_SCHEME, layout)
    monitor_unit = permgen.build_monitor_unit(permgen.FOUR_WAY_SCHEME, layout)
    manager_content = manager_unit.content
    monitor_content = monitor_unit.content
    if manager_env_file is not None:
        manager_content = manager_content.replace(
            f"EnvironmentFile={layout.env_file}",
            f"EnvironmentFile={manager_env_file}",
            1,
        )
    if monitor_env_file is not None:
        monitor_content = monitor_content.replace(
            f"EnvironmentFile={layout.env_file}",
            f"EnvironmentFile={monitor_env_file}",
            1,
        )
    # The probe currently accepts an injected HOME but not a unit-root override,
    # so the fixture preserves the generated trust-root bytes while keeping the
    # test isolated from real system paths.
    (unit_root / manager_unit.unit_name).write_text(manager_content, encoding="utf-8")
    (unit_root / monitor_unit.unit_name).write_text(monitor_content, encoding="utf-8")
    (unit_root / f"{layout.instance}-manager.timer").write_text("[Timer]\n", encoding="utf-8")


def test_service_environment_probe_accepts_generated_trust_root_install_layout(
    tmp_path: Path,
) -> None:
    home, unit_root, layout = _trust_root_layout(tmp_path)
    repo = _init_git_repo_with_origin(
        tmp_path / "repo",
        "https://github.com/hamanpaul/paulsha-cortex",
    )
    _write_trust_root_env(layout, repo=repo)
    _write_generated_units(unit_root, layout)

    result, effective = _service_environment_probe(
        home=home,
        instance=layout.instance,
        live=False,
        base_env={"HOME": str(home)},
    )

    assert result.status == "pass", result.detail
    assert effective["PSC_REPO_IDENTITY"] == "git:github.com/hamanpaul/paulsha-cortex"
    assert effective["PSC_AGENTS_ROOT"] == layout.agents_root
    assert effective["PSC_RUN_ROOT"] == layout.run_root
    assert effective["PSC_MONITOR_STATE_ROOT"] == layout.monitor_state_root


def test_service_environment_probe_rejects_generated_trust_root_layout_when_env_file_missing(
    tmp_path: Path,
) -> None:
    home, unit_root, layout = _trust_root_layout(tmp_path)
    _write_generated_units(unit_root, layout)

    result, _effective = _service_environment_probe(
        home=home,
        instance=layout.instance,
        live=False,
        base_env={"HOME": str(home)},
    )

    assert result.status == "fail"
    assert "bootstrap environment is invalid" in result.detail


def test_service_environment_probe_rejects_generated_trust_root_layout_when_units_disagree(
    tmp_path: Path,
) -> None:
    home, unit_root, layout = _trust_root_layout(tmp_path)
    repo = _init_git_repo_with_origin(
        tmp_path / "repo",
        "https://github.com/hamanpaul/paulsha-cortex",
    )
    env_file = _write_trust_root_env(layout, repo=repo)
    _write_generated_units(
        unit_root,
        layout,
        monitor_env_file=str(env_file.with_name("other-manager.env")),
    )

    result, _effective = _service_environment_probe(
        home=home,
        instance=layout.instance,
        live=False,
        base_env={"HOME": str(home)},
    )

    assert result.status == "fail"
    assert "bootstrap environment is invalid" in result.detail


def test_service_environment_probe_rejects_generated_trust_root_layout_when_env_file_is_symlink(
    tmp_path: Path,
) -> None:
    home, unit_root, layout = _trust_root_layout(tmp_path)
    repo = _init_git_repo_with_origin(
        tmp_path / "repo",
        "https://github.com/hamanpaul/paulsha-cortex",
    )
    env_file = _write_trust_root_env(layout, repo=repo)
    real_env_file = env_file.with_name("real-manager.env")
    real_env_file.write_text(env_file.read_text(encoding="utf-8"), encoding="utf-8")
    env_file.unlink()
    env_file.symlink_to(real_env_file)
    _write_generated_units(unit_root, layout)

    result, _effective = _service_environment_probe(
        home=home,
        instance=layout.instance,
        live=False,
        base_env={"HOME": str(home)},
    )

    assert result.status == "fail"
    assert "bootstrap environment is invalid" in result.detail
