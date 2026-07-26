from pathlib import Path

from paulsha_cortex.coordinator.autonomy import _infer_repo_root


def test_infer_repo_root_prefers_configured_repo_root_for_external_spec(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    spec_dir = tmp_path / "agents" / "specs"
    spec_path = spec_dir / "foo-spec.md"
    spec_dir.mkdir(parents=True)
    spec_path.write_text("# foo\n", encoding="utf-8")
    (tmp_path / "agents" / ".git").mkdir()

    monkeypatch.setenv("PSC_REPO_ROOT", str(repo_root))

    assert _infer_repo_root(spec_path) == repo_root


def test_infer_repo_root_keeps_in_repo_path_for_repo_relative_spec(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    spec_path = repo_root / "specs" / "foo-spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# foo\n", encoding="utf-8")

    monkeypatch.setenv("PSC_REPO_ROOT", str(repo_root))

    assert _infer_repo_root(spec_path) == repo_root


def test_infer_repo_root_fallback_unchanged_without_configured_repo_root(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    spec_path = tmp_path / "outside" / "specs" / "foo-spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# foo\n", encoding="utf-8")

    monkeypatch.delenv("PSC_REPO_ROOT", raising=False)
    monkeypatch.chdir(repo_root)

    assert _infer_repo_root(spec_path) == spec_path.parent
