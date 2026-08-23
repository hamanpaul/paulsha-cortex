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


def test_service_environment_probe_accepts_generated_trust_root_install_layout(
    tmp_path: Path,
) -> None:
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
    repo = _init_git_repo_with_origin(
        tmp_path / "repo",
        "https://github.com/hamanpaul/paulsha-cortex",
    )
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

    manager_unit = permgen.build_manager_unit(permgen.FOUR_WAY_SCHEME, layout)
    monitor_unit = permgen.build_monitor_unit(permgen.FOUR_WAY_SCHEME, layout)
    # The probe currently accepts an injected HOME but not a unit-root override,
    # so the fixture preserves the generated trust-root bytes while keeping the
    # test isolated from real system paths.
    (unit_root / manager_unit.unit_name).write_text(manager_unit.content, encoding="utf-8")
    (unit_root / monitor_unit.unit_name).write_text(monitor_unit.content, encoding="utf-8")

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
