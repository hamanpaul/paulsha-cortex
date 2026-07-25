import re
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import dispatcher
from paulsha_cortex.config import paths
from paulsha_cortex.deploy.installer import render_units


def test_default_git_runner_prefixes_arguments_with_repo_root(monkeypatch, tmp_path):
    calls: list[tuple] = []

    def fake_run(argv, capture_output=True, text=True):
        calls.append((argv, capture_output, text))
        return SimpleNamespace(returncode=0, stdout="abc\n", stderr="")

    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(dispatcher.subprocess, "run", fake_run)

    out = dispatcher._default_git_runner(["rev-parse", "--show-toplevel"])
    assert out == "abc"
    assert calls == [(["git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"], True, True)]


def test_default_git_runner_failure_includes_repo_root_and_stderr(monkeypatch, tmp_path):
    def fake_run(argv, capture_output=True, text=True):
        return SimpleNamespace(returncode=128, stdout="", stderr="fatal: not a git repository")

    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(dispatcher.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match=re.escape("-C")) as exc:
        dispatcher._default_git_runner(["rev-parse", "--show-toplevel"])
    message = str(exc.value)
    assert str(tmp_path) in message
    assert "fatal: not a git repository" in message


def test_render_units_includes_working_directory_for_manager_and_monitor_service():
    units = render_units("cortex", 300)
    assert "WorkingDirectory=" in units["cortex-manager.service"]
    assert "WorkingDirectory=" in units["cortex-monitor.service"]
