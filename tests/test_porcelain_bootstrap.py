from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from paulsha_cortex.deploy import installer

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


def _write_symlink_tool(bin_dir: Path, name: str, target_name: str) -> Path:
    target = shutil.which(target_name)
    assert target is not None
    link = bin_dir / name
    link.symlink_to(target)
    return link


def _write_executor_stub(
    bin_dir: Path,
    *,
    name: str,
    auth_status_rc: int,
    argv_log: Path | None = None,
) -> Path:
    log_line = ""
    if argv_log is not None:
        log_line = f'printf "%s\\n" "$*" >> "{argv_log}"'
    commands = {
        "copilot": (
            'if [[ "${1:-}" == "-p" ]]; then',
            log_line,
            f'  [[ {auth_status_rc} -eq 0 ]] || echo "please run copilot login" >&2',
            f"  exit {auth_status_rc}",
            "fi",
            'if [[ "${1:-}" == "login" ]]; then',
            "  exit 0",
            "fi",
        ),
        "claude": (
            'if [[ "${1:-}" == "auth" && "${2:-}" == "status" ]]; then',
            f"  exit {auth_status_rc}",
            "fi",
            'if [[ "${1:-}" == "auth" && "${2:-}" == "login" ]]; then',
            "  exit 0",
            "fi",
        ),
        "codex": (
            'if [[ "${1:-}" == "doctor" && "${2:-}" == "--json" ]]; then',
            (
                "  printf '%s\\n' '{\"checks\":{\"auth.credentials\":{\"status\":\"ok\"}}}'"
                if auth_status_rc == 0
                else "  printf '%s\\n' '{\"checks\":{\"auth.credentials\":{\"status\":\"fail\"}}}'"
            ),
            f"  exit {auth_status_rc}",
            "fi",
            'if [[ "${1:-}" == "login" ]]; then',
            "  exit 0",
            "fi",
        ),
    }
    return _write_stub(bin_dir, name, "\n".join((*commands[name], "exit 0", "")))


def _configure_preflight_tools(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    executor: str = "claude",
    executor_auth_status_rc: int = 0,
    include_git: bool = True,
    runtime_executor: str | None = None,
    argv_log: Path | None = None,
) -> None:
    _write_symlink_tool(bootstrap_runtime["bin_dir"], "bash", "bash")
    if include_git:
        _write_symlink_tool(bootstrap_runtime["bin_dir"], "git", "git")
    _write_executor_stub(
        bootstrap_runtime["bin_dir"],
        name=executor,
        auth_status_rc=executor_auth_status_rc,
        argv_log=argv_log,
    )
    monkeypatch.setenv("PATH", str(bootstrap_runtime["bin_dir"]))
    if runtime_executor is None:
        monkeypatch.delenv("PSC_MANAGER_EXECUTOR", raising=False)
    else:
        monkeypatch.setenv("PSC_MANAGER_EXECUTOR", runtime_executor)


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
    ("executor", "login_hint"),
    [
        ("copilot", "copilot login"),
        ("claude", "claude auth login"),
        ("codex", "codex login"),
    ],
)
def test_bootstrap_preflight_reports_executor_login_fix(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    executor: str,
    login_hint: str,
) -> None:
    _reset_porcelain_modules()
    _configure_preflight_tools(
        bootstrap_runtime,
        monkeypatch,
        executor=executor,
        executor_auth_status_rc=1,
        runtime_executor=executor,
    )
    monkeypatch.chdir(bootstrap_runtime["repo_root"])

    assert _run_cli(["bootstrap"]) == 4

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert login_hint in combined
    assert "executor" in combined


def test_bootstrap_preflight_reports_actionable_repo_fix(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    _configure_preflight_tools(bootstrap_runtime, monkeypatch, runtime_executor="claude")
    monkeypatch.chdir(bootstrap_runtime["outside_root"])

    assert _run_cli(["bootstrap"]) == 4

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert "git" in combined
    assert "repo" in combined


def test_bootstrap_preflight_reports_missing_git_fix(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    _configure_preflight_tools(
        bootstrap_runtime,
        monkeypatch,
        include_git=False,
        runtime_executor="claude",
    )
    monkeypatch.chdir(bootstrap_runtime["repo_root"])

    assert _run_cli(["bootstrap"]) == 4

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert "git" in combined
    assert "install" in combined


def test_bootstrap_preflight_requires_effective_runtime_executor(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    _configure_preflight_tools(bootstrap_runtime, monkeypatch, executor="claude")
    monkeypatch.chdir(bootstrap_runtime["repo_root"])

    assert _run_cli(["bootstrap"]) == 4

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert "psc_manager_executor" in combined
    assert "copilot" in combined


def test_bootstrap_preflight_uses_installed_instance_executor_config(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    runtime_dir = bootstrap_runtime["home"] / ".agents" / "core" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "beta.env").write_text("PSC_MANAGER_EXECUTOR=claude\n", encoding="utf-8")
    _configure_preflight_tools(bootstrap_runtime, monkeypatch)
    monkeypatch.chdir(bootstrap_runtime["repo_root"])

    assert _run_cli(["bootstrap", "--dry-run"]) == 0

    captured = capsys.readouterr()
    assert "service install" in captured.out


def test_service_install_rejects_unsupported_executor_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PSC_MANAGER_EXECUTOR", "badexec")

    with pytest.raises(ValueError, match="PSC_MANAGER_EXECUTOR"):
        installer.install_service_result("beta", 300, repo_root)


def test_service_install_rejects_invalid_persisted_executor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    home = tmp_path / "home"
    runtime_dir = home / ".agents" / "core" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "beta-manager.env").write_text("PSC_MANAGER_EXECUTOR=badexec\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PSC_MANAGER_EXECUTOR", raising=False)

    with pytest.raises(ValueError, match="既有 PSC_MANAGER_EXECUTOR"):
        installer.install_service_result("beta", 300, repo_root)


def test_service_install_rejects_invalid_instance_env_executor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    home = tmp_path / "home"
    runtime_dir = home / ".agents" / "core" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "beta.env").write_text("PSC_MANAGER_EXECUTOR=badexec\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PSC_MANAGER_EXECUTOR", raising=False)

    with pytest.raises(ValueError, match="instance PSC_MANAGER_EXECUTOR"):
        installer.install_service_result("beta", 300, repo_root)


def test_service_install_rejects_invalid_instance_env_even_with_valid_manager_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    home = tmp_path / "home"
    runtime_dir = home / ".agents" / "core" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "beta.env").write_text("PSC_MANAGER_EXECUTOR=badexec\n", encoding="utf-8")
    (runtime_dir / "beta-manager.env").write_text("PSC_MANAGER_EXECUTOR=claude\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PSC_MANAGER_EXECUTOR", raising=False)

    with pytest.raises(ValueError, match="instance PSC_MANAGER_EXECUTOR"):
        installer.install_service_result("beta", 300, repo_root)


def test_bootstrap_preflight_rejects_symlinked_runtime_env(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    runtime_dir = bootstrap_runtime["home"] / ".agents" / "core" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "real.env").write_text("PSC_MANAGER_EXECUTOR=claude\n", encoding="utf-8")
    (runtime_dir / "beta.env").symlink_to(runtime_dir / "real.env")
    _configure_preflight_tools(bootstrap_runtime, monkeypatch, runtime_executor="claude")
    monkeypatch.delenv("PSC_MANAGER_EXECUTOR", raising=False)
    monkeypatch.chdir(bootstrap_runtime["repo_root"])

    assert _run_cli(["bootstrap"]) == 4

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert "runtime executor" in combined
    assert "symlink" in combined


def test_bootstrap_preflight_rejects_invalid_runtime_env_even_with_process_override(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    runtime_dir = bootstrap_runtime["home"] / ".agents" / "core" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "beta.env").write_text("PSC_MANAGER_EXECUTOR=badexec\n", encoding="utf-8")
    _configure_preflight_tools(bootstrap_runtime, monkeypatch, runtime_executor="claude")
    monkeypatch.chdir(bootstrap_runtime["repo_root"])

    assert _run_cli(["bootstrap"]) == 4

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert "runtime executor" in combined
    assert "badexec" in combined


def test_bootstrap_preflight_accepts_quoted_runtime_executor(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    runtime_dir = bootstrap_runtime["home"] / ".agents" / "core" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "beta-manager.env").write_text('PSC_MANAGER_EXECUTOR="claude"\n', encoding="utf-8")
    _configure_preflight_tools(bootstrap_runtime, monkeypatch)
    monkeypatch.chdir(bootstrap_runtime["repo_root"])

    assert _run_cli(["bootstrap", "--dry-run"]) == 0

    captured = capsys.readouterr()
    assert "service install" in captured.out


def test_bootstrap_dry_run_only_previews_service_calls(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    _configure_preflight_tools(bootstrap_runtime, monkeypatch, runtime_executor="claude")
    service_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    inspect_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    _patch_bootstrap_backends(monkeypatch, service_calls=service_calls, inspect_calls=inspect_calls)

    assert _run_cli(["bootstrap", "--dry-run"]) == 0

    captured = capsys.readouterr()
    assert "service install" in captured.out
    assert "service start" in captured.out
    assert service_calls == []
    assert inspect_calls == []


def test_bootstrap_planned_commands_quote_copy_paste_values() -> None:
    _reset_porcelain_modules()
    bootstrap = importlib.import_module("paulsha_cortex.porcelain.bootstrap")

    commands = bootstrap._planned_commands(
        instance="beta team",
        repo_root="/tmp/demo repo",
        interval=300,
        start=True,
        sample="feature oneshot",
        task="demo feature",
        change="demo change",
    )

    assert commands == [
        "cortex service install --instance 'beta team' --repo-root '/tmp/demo repo' --interval 300",
        "cortex service start --instance 'beta team'",
        "cortex init-sample --combo 'feature oneshot' --task 'demo feature' --change 'demo change'",
    ]


def test_bootstrap_copilot_preflight_avoids_allow_all_tools(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_porcelain_modules()
    argv_log = bootstrap_runtime["tmp_path"] / "copilot-argv.log"
    _configure_preflight_tools(
        bootstrap_runtime,
        monkeypatch,
        executor="copilot",
        runtime_executor="copilot",
        argv_log=argv_log,
    )

    assert _run_cli(["bootstrap", "--dry-run"]) == 0

    logged_argv = argv_log.read_text(encoding="utf-8").strip()
    assert "--allow-all-tools" not in logged_argv
    assert "--disable-builtin-mcps" in logged_argv
    assert "--no-custom-instructions" in logged_argv


def test_bootstrap_json_runs_service_then_inspect_summaries(
    bootstrap_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_porcelain_modules()
    _configure_preflight_tools(bootstrap_runtime, monkeypatch, runtime_executor="claude")
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
    _configure_preflight_tools(bootstrap_runtime, monkeypatch, runtime_executor="claude")
    service_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    inspect_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    _patch_bootstrap_backends(monkeypatch, service_calls=service_calls, inspect_calls=inspect_calls)
    _patch_sample_failure(monkeypatch)

    assert _run_cli(["bootstrap", "--sample", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == BOOTSTRAP_SCHEMA
    assert payload["sample"]["ok"] is False
    assert "sample seed failed" in payload["sample"]["error"]
