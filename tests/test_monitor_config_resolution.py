from pathlib import Path

import pytest

from paulsha_cortex.monitor.config import _resolve_config_source, load_config


def _write(p: Path, text="workspaces:\n  - {name: a, path: /tmp/a}\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_prefers_new_project_cortex_over_legacy(monkeypatch, tmp_path):
    monkeypatch.delenv("PSC_MONITOR_CONFIG", raising=False)
    monkeypatch.delenv("PAULSHACLAW_CONFIG", raising=False)
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("PSC_CONFIG_ROOT", str(tmp_path / "legacy"))
    new = _write(tmp_path / "agents" / "project-cortex.yaml")
    _write(tmp_path / "legacy" / "paulshaclaw.yaml")
    assert _resolve_config_source(None) == new


def test_legacy_only_transition(monkeypatch, tmp_path, recwarn):
    monkeypatch.delenv("PSC_MONITOR_CONFIG", raising=False)
    monkeypatch.delenv("PAULSHACLAW_CONFIG", raising=False)
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("PSC_CONFIG_ROOT", str(tmp_path / "legacy"))
    legacy = _write(tmp_path / "legacy" / "paulshaclaw.yaml")
    assert _resolve_config_source(None) == legacy
    assert any("deprecated" in str(w.message).lower() for w in recwarn.list)


def test_none_when_no_manual(monkeypatch, tmp_path):
    monkeypatch.delenv("PSC_MONITOR_CONFIG", raising=False)
    monkeypatch.delenv("PAULSHACLAW_CONFIG", raising=False)
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("PSC_CONFIG_ROOT", str(tmp_path / "legacy"))
    assert _resolve_config_source(None) is None


def test_load_config_wraps_read_os_error(monkeypatch, tmp_path):
    config_path = _write(tmp_path / "agents" / "project-cortex.yaml")
    original = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == config_path:
            raise PermissionError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    with pytest.raises(ValueError, match="讀取或解析失敗"):
        load_config(config_path=config_path)
