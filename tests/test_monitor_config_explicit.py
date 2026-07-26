from pathlib import Path

from paulsha_cortex.monitor.config import load_config


def _write_yaml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_load_config_with_explicit_path_does_not_merge_ambient_hippo_projects(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(tmp_path / "ambient"))
    config_dir = tmp_path / "explicit"
    _write_yaml(
        config_dir / "project-cortex.yaml",
        """
        workspaces:
          - name: explicit
            path: /tmp/explicit-workspace
        """,
    )
    _write_yaml(
        config_dir / "project-hippo.yaml",
        """
        projects:
          - slug: explicit-ambient
            roots: [/tmp/explicit-root]
        """,
    )

    cfg = load_config(config_path=config_dir / "project-cortex.yaml")

    assert cfg.hippo_projects == ()


def test_load_config_without_path_merges_ambient_hippo_projects(monkeypatch, tmp_path):
    monkeypatch.delenv("PSC_MONITOR_CONFIG", raising=False)
    monkeypatch.delenv("PAULSHACLAW_CONFIG", raising=False)
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(tmp_path / "ambient"))
    config_root = tmp_path / "ambient"
    _write_yaml(
        config_root / "project-cortex.yaml",
        """
        workspaces:
          - name: default
            path: /tmp/default-workspace
        """,
    )
    _write_yaml(
        config_root / "project-hippo.yaml",
        """
        projects:
          - slug: default-ambient
            roots: [/tmp/default-root]
        """,
    )

    cfg = load_config()

    assert len(cfg.hippo_projects) == 1
    assert cfg.hippo_projects[0].name == "default-ambient"
