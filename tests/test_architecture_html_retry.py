from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO_ROOT / "tests" / "architecture_html_visual_check_retry.py"
TIMEOUT = "viewer/visual-check-runtime: Target.getTargets: timed out after 15000ms"


def _load_helper():
    assert HELPER_PATH.is_file(), "Architecture HTML visual-check retry helper is missing"
    spec = importlib.util.spec_from_file_location("architecture_html_visual_check_retry", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed(returncode: int, stdout: str, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["node", "archify.mjs", "visual-check"],
        returncode=returncode,
        stdout=stdout.encode(),
        stderr=stderr.encode(),
    )


def test_retries_only_target_get_targets_timeout_and_keeps_attempt_diagnostics(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    helper = _load_helper()
    results = [
        _completed(1, '{"ok":false,"error":"' + TIMEOUT + '"}', "CDP target discovery failed"),
        _completed(0, '{"ok":true,"runtime":"ready"}'),
    ]
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return results[len(calls) - 1]

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    output = tmp_path / "review"
    result = helper.run_visual_check(
        html=tmp_path / "architecture.html",
        archify=tmp_path / "archify.mjs",
        output=output,
    )

    assert result == 0
    assert len(calls) == 2
    assert (output / "visual-check-attempt-1.json").read_text() == results[0].stdout.decode()
    assert TIMEOUT in (output / "visual-check-attempt-1.json").read_text()
    assert "CDP target discovery failed" in (output / "visual-check-attempt-1.log").read_text()
    assert (output / "visual-check-attempt-2.json").read_text() == results[1].stdout.decode()
    assert (output / "visual-check.json").read_text() == results[1].stdout.decode()
    assert "重試" in capsys.readouterr().err


def test_repeated_target_get_targets_timeout_stops_after_two_retries(
    tmp_path: Path, monkeypatch
) -> None:
    helper = _load_helper()
    result_data = _completed(1, '{"error":"' + TIMEOUT + '"}', "CDP timeout")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return result_data

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    output = tmp_path / "review"
    result = helper.run_visual_check(
        html=tmp_path / "architecture.html",
        archify=tmp_path / "archify.mjs",
        output=output,
    )

    assert result == 1
    assert len(calls) == 3
    assert all((output / f"visual-check-attempt-{n}.json").is_file() for n in (1, 2, 3))
    assert "3 次執行上限" in (output / "visual-check.log").read_text()


def test_non_cdp_failure_fails_immediately_and_preserves_log(
    tmp_path: Path, monkeypatch
) -> None:
    helper = _load_helper()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(7, '{"error":"native graph assertion failed"}', "AssertionError: graph")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    output = tmp_path / "review"
    result = helper.run_visual_check(
        html=tmp_path / "architecture.html",
        archify=tmp_path / "archify.mjs",
        output=output,
    )

    assert result == 7
    assert len(calls) == 1
    assert not (output / "visual-check-attempt-2.json").exists()
    assert "AssertionError: graph" in (output / "visual-check-attempt-1.log").read_text()


def test_architecture_html_workflow_runs_retry_helper_and_always_uploads_diagnostics() -> None:
    workflow = yaml.load(
        (REPO_ROOT / ".github/workflows/architecture-html.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["review"]["steps"]
    visual_step = next(
        step for step in steps
        if step.get("name") == "Original Archify file navigation and native graph interaction"
    )
    artifact_step = next(step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact@"))

    assert "architecture_html_visual_check_retry.py" in visual_step["run"]
    assert artifact_step["if"] == "always()"
    assert "/tmp/architecture-review" in artifact_step["with"]["path"]
