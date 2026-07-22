from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BOOTSTRAP_SCHEMA = "cortex-porcelain/bootstrap/v1"


def _reset_porcelain_modules() -> None:
    for module_name in (
        "paulsha_cortex.cli",
        "paulsha_cortex.porcelain",
        "paulsha_cortex.porcelain.bootstrap",
        "paulsha_cortex.porcelain.inspect",
        "paulsha_cortex.porcelain.service",
    ):
        sys.modules.pop(module_name, None)


def _load_cli():
    sys.modules.pop("paulsha_cortex.cli", None)
    return importlib.import_module("paulsha_cortex.cli")


def _run_cli(argv: list[str]) -> int:
    cli = _load_cli()
    try:
        return cli.main(argv)
    except SystemExit as error:
        code = error.code
        return code if isinstance(code, int) else 1


def _write_stub(path: Path, name: str, body: str) -> Path:
    script = path / name
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _write_gh_stub(bin_dir: Path, *, auth_status_rc: int) -> Path:
    return _write_stub(
        bin_dir,
        "gh",
        "\n".join(
            (
                'if [[ "${1:-}" == "auth" && "${2:-}" == "status" ]]; then',
                f"  exit {auth_status_rc}",
                "fi",
                'if [[ "${1:-}" == "auth" && "${2:-}" == "login" ]]; then',
                "  exit 0",
                "fi",
                "exit 0",
                "",
            )
        ),
    )


@pytest.fixture
def bootstrap_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    repo_root = tmp_path / "repo"
    outside_root = tmp_path / "outside"
    for path in (home, bin_dir, repo_root, outside_root):
        path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "README.md").write_text("# demo\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PSC_INSTANCE", "beta")
    monkeypatch.chdir(repo_root)

    return {
        "tmp_path": tmp_path,
        "home": home,
        "bin_dir": bin_dir,
        "repo_root": repo_root,
        "outside_root": outside_root,
    }


def _patch_bootstrap_backends(
    monkeypatch: pytest.MonkeyPatch,
    *,
    service_calls: list[tuple[str, tuple[object, ...], dict[str, object]]],
    inspect_calls: list[tuple[str, tuple[object, ...], dict[str, object]]],
) -> None:
    importlib.import_module("paulsha_cortex.porcelain")
    service = importlib.import_module("paulsha_cortex.porcelain.service")
    inspect = importlib.import_module("paulsha_cortex.porcelain.inspect")

    def fake_install(*args, **kwargs):
        service_calls.append(("install", args, kwargs))
        return {
            "command": "install",
            "instance": "beta",
            "message": "installed",
            "mode": "systemd",
            "result": {"exit_code": 0},
        }

    def fake_start(*args, **kwargs):
        service_calls.append(("start", args, kwargs))
        return {
            "command": "start",
            "instance": "beta",
            "mode": "systemd",
            "result": {"exit_code": 0},
        }

    def fake_status_summary(*args, **kwargs):
        inspect_calls.append(("status_summary", args, kwargs))
        return {
            "ready": ["porcelain-bootstrap"],
            "held": [],
            "in_flight": [],
            "recent_done": [],
            "degraded": False,
            "updated_at": "2026-07-22T14:30:32Z",
        }

    def fake_doctor_summary(*args, **kwargs):
        inspect_calls.append(("doctor_summary", args, kwargs))
        return {
            "schema": "cortex-doctor/v1",
            "ok": True,
            "probes": [{"name": "gh-auth", "status": "pass", "detail": "ready", "required": True}],
        }

    monkeypatch.setattr(service, "install", fake_install, raising=False)
    monkeypatch.setattr(service, "start", fake_start, raising=False)
    monkeypatch.setattr(inspect, "status_summary", fake_status_summary, raising=False)
    monkeypatch.setattr(inspect, "doctor_summary", fake_doctor_summary, raising=False)


def _patch_sample_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        bootstrap = importlib.import_module("paulsha_cortex.porcelain.bootstrap")
    except ModuleNotFoundError:
        return

    def fake_init_sample(*args, **kwargs):
        raise RuntimeError("sample seed failed")

    monkeypatch.setattr(bootstrap, "init_sample", fake_init_sample, raising=False)


@pytest.mark.parametrize(
    ("cwd_key", "path_entries", "auth_status_rc", "needles"),
    [
        ("repo_root", "with-gh", 1, ("gh auth login", "exit code 4")),
        ("outside_root", "with-gh", 0, ("git", "repo")),
        ("repo_root", "missing-git", 0, ("git", "install")),
    ],
)
def test_bootstrap_preflight_reports_actionable_fixes(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cwd_key: str,
    path_entries: str,
    auth_status_rc: int,
    needles: tuple[str, str],
) -> None:
    _reset_porcelain_modules()
    _write_gh_stub(bootstrap_runtime["bin_dir"], auth_status_rc=auth_status_rc)
    if path_entries == "missing-git":
        monkeypatch.setenv("PATH", str(bootstrap_runtime["bin_dir"]))
    else:
        monkeypatch.setenv("PATH", f"{bootstrap_runtime['bin_dir']}:{os.environ['PATH']}")
    monkeypatch.chdir(bootstrap_runtime[cwd_key])

    assert _run_cli(["bootstrap"]) == 4

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    for needle in needles:
        assert needle.lower() in combined


def test_bootstrap_dry_run_only_previews_service_calls(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    _write_gh_stub(bootstrap_runtime["bin_dir"], auth_status_rc=0)
    monkeypatch.setenv("PATH", f"{bootstrap_runtime['bin_dir']}:{os.environ['PATH']}")
    service_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    inspect_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    _patch_bootstrap_backends(monkeypatch, service_calls=service_calls, inspect_calls=inspect_calls)

    assert _run_cli(["bootstrap", "--dry-run"]) == 0

    captured = capsys.readouterr()
    assert "service install" in captured.out
    assert "service start" in captured.out
    assert service_calls == []
    assert inspect_calls == []


def test_bootstrap_json_runs_service_then_inspect_summaries(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    _write_gh_stub(bootstrap_runtime["bin_dir"], auth_status_rc=0)
    monkeypatch.setenv("PATH", f"{bootstrap_runtime['bin_dir']}:{os.environ['PATH']}")
    service_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    inspect_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    _patch_bootstrap_backends(monkeypatch, service_calls=service_calls, inspect_calls=inspect_calls)

    assert _run_cli(["bootstrap", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == BOOTSTRAP_SCHEMA
    assert [name for name, _args, _kwargs in service_calls] == ["install", "start"]
    assert [name for name, _args, _kwargs in inspect_calls] == ["status_summary", "doctor_summary"]
    assert payload["doctor"]["ok"] is True
    assert payload["status"]["degraded"] is False


def test_bootstrap_sample_failure_is_degraded_but_not_fatal(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    _write_gh_stub(bootstrap_runtime["bin_dir"], auth_status_rc=0)
    monkeypatch.setenv("PATH", f"{bootstrap_runtime['bin_dir']}:{os.environ['PATH']}")
    service_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    inspect_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    _patch_bootstrap_backends(monkeypatch, service_calls=service_calls, inspect_calls=inspect_calls)
    _patch_sample_failure(monkeypatch)

    assert _run_cli(["bootstrap", "--sample", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == BOOTSTRAP_SCHEMA
    assert payload["sample"]["ok"] is False
    assert "sample seed failed" in payload["sample"]["error"]
