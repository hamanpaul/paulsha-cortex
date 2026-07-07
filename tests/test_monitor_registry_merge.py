from pathlib import Path

import pytest

from paulsha_cortex.monitor import registry
from paulsha_cortex.monitor.config import load_config


def _entry(p, name, source):
    return registry.ProjectEntry(path=p.resolve(), name=name, source=source)


def test_merge_dedupes_by_realpath_manual_wins(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    manual = _entry(p, "manual-name", "manual")
    hippo = _entry(p, "hippo-slug", "hippo")
    merged = registry.merge_projects([manual], [hippo])
    assert len(merged) == 1
    assert merged[0].name == "manual-name"


def test_merge_union_order_manual_first(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    out = registry.merge_projects([_entry(a, "a", "manual")], [_entry(b, "b", "hippo")])
    assert [e.path for e in out] == [a.resolve(), b.resolve()]


def test_load_hippo_projects_missing_returns_empty(tmp_path):
    assert registry.load_hippo_projects(tmp_path / "nope.yaml") == []


def test_load_hippo_projects_reads_slug_roots(tmp_path):
    src = tmp_path / "project-hippo.yaml"
    src.write_text(f"projects:\n  - slug: proj-x\n    roots: [{tmp_path}]\n", encoding="utf-8")
    entries = registry.load_hippo_projects(src)
    assert entries[0].source == "hippo" and entries[0].name == "proj-x"
    assert entries[0].path == tmp_path.resolve()


def test_load_hippo_projects_invalid_yaml_raises_value_error(tmp_path):
    src = tmp_path / "project-hippo.yaml"
    src.write_text("projects: [", encoding="utf-8")
    with pytest.raises(ValueError, match="project-hippo.yaml"):
        registry.load_hippo_projects(src)


def test_load_hippo_projects_rejects_non_list_roots(tmp_path):
    src = tmp_path / "project-hippo.yaml"
    src.write_text("projects:\n  - slug: proj-x\n    roots: /tmp/demo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="roots"):
        registry.load_hippo_projects(src)


def test_load_hippo_projects_rejects_non_mapping_top_level(tmp_path):
    src = tmp_path / "project-hippo.yaml"
    src.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="頂層"):
        registry.load_hippo_projects(src)


def test_load_hippo_projects_rejects_blank_root(tmp_path):
    src = tmp_path / "project-hippo.yaml"
    src.write_text("projects:\n  - slug: proj-x\n    roots: ['']\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不可為空字串"):
        registry.load_hippo_projects(src)


def test_load_hippo_projects_wraps_read_os_error(tmp_path, monkeypatch):
    src = tmp_path / "project-hippo.yaml"
    src.write_text("projects: []\n", encoding="utf-8")
    original = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == src:
            raise PermissionError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    with pytest.raises(ValueError, match="讀取或解析失敗"):
        registry.load_hippo_projects(src)


def test_load_config_missing_hippo_graceful(monkeypatch, tmp_path):
    monkeypatch.delenv("PSC_MONITOR_CONFIG", raising=False)
    monkeypatch.delenv("PAULSHACLAW_CONFIG", raising=False)
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(tmp_path))
    (tmp_path / "project-cortex.yaml").write_text(
        f"workspaces:\n  - {{name: a, path: {tmp_path}}}\n",
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.hippo_projects == ()


def test_load_config_both_missing_fails(monkeypatch, tmp_path):
    monkeypatch.delenv("PSC_MONITOR_CONFIG", raising=False)
    monkeypatch.delenv("PAULSHACLAW_CONFIG", raising=False)
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("PSC_CONFIG_ROOT", str(tmp_path / "legacy"))
    with pytest.raises(FileNotFoundError, match="無 project 設定"):
        load_config()


def test_scan_dedupes_manual_and_hippo_to_one_state(monkeypatch, tmp_path):
    from paulsha_cortex.monitor.config import MonitorConfig, WorkspaceConfig
    from paulsha_cortex.monitor.scanner import scan_workspaces

    ws_root = tmp_path / "ws"
    (ws_root / "projX").mkdir(parents=True)
    cfg = MonitorConfig(
        workspaces=(WorkspaceConfig(path=ws_root, name="curated"),),
        hippo_projects=(_entry(ws_root / "projX", "hippo-slug", "hippo"),),
    )
    states = scan_workspaces(cfg)
    matches = [s for s in states if Path(s.path).resolve() == (ws_root / "projX").resolve()]
    assert len(matches) == 1
    assert matches[0].workspace == "curated"
