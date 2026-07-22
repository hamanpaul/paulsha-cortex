from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

import pytest

SERVICE_SCHEMA = "cortex-porcelain/service/v1"


def _load_cli():
    for module_name in (
        "paulsha_cortex.cli",
        "paulsha_cortex.porcelain",
        "paulsha_cortex.porcelain.service",
        "paulsha_cortex.porcelain._runtime_probe",
    ):
        sys.modules.pop(module_name, None)
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


def _write_systemctl_stub(bin_dir: Path, call_log: Path, show_payload: Path | None = None) -> Path:
    body = [
        f'printf "%s\\n" "$*" >> "{call_log}"',
        'if [[ "${1:-}" == "--user" && "${2:-}" == "show-environment" ]]; then',
        '  exit "${SYSTEMCTL_SHOW_ENV_RC:-0}"',
        "fi",
        'if [[ "${1:-}" == "--user" && "${2:-}" == "show" ]]; then',
    ]
    if show_payload is None:
        body.append("  exit 0")
    else:
        body.extend((f'  cat "{show_payload}"', "  exit 0"))
    body.extend(("fi", 'exit "${SYSTEMCTL_RC:-0}"'))
    return _write_stub(bin_dir, "systemctl", "\n".join(body) + "\n")


def _write_journalctl_stub(bin_dir: Path, call_log: Path, output_path: Path) -> Path:
    return _write_stub(
        bin_dir,
        "journalctl",
        f'printf "%s\\n" "$*" >> "{call_log}"\ncat "{output_path}"\n',
    )


def _write_units(unit_root: Path, instance: str, *, exec_path: Path, monitor_exec_path: Path) -> None:
    unit_root.mkdir(parents=True, exist_ok=True)
    (unit_root / f"{instance}-manager.service").write_text(
        "[Unit]\n"
        "[Service]\n"
        f"ExecStart={exec_path} -m paulsha_cortex.coordinator.manager_daemon\n",
        encoding="utf-8",
    )
    (unit_root / f"{instance}-manager.timer").write_text("[Timer]\nOnUnitActiveSec=300\n", encoding="utf-8")
    (unit_root / f"{instance}-monitor.service").write_text(
        "[Unit]\n"
        "[Service]\n"
        f"ExecStart={monitor_exec_path} -m paulsha_cortex.monitor\n",
        encoding="utf-8",
    )


def _systemctl_show(instance: str, *, manager_pid: int, timer_pid: int = 0, monitor_pid: int = 0) -> str:
    return (
        f"Id={instance}-manager.service\n"
        "LoadState=loaded\n"
        "ActiveState=active\n"
        "SubState=running\n"
        f"MainPID={manager_pid}\n\n"
        f"Id={instance}-manager.timer\n"
        "LoadState=loaded\n"
        "ActiveState=active\n"
        "SubState=waiting\n"
        f"MainPID={timer_pid}\n\n"
        f"Id={instance}-monitor.service\n"
        "LoadState=loaded\n"
        "ActiveState=active\n"
        "SubState=running\n"
        f"MainPID={monitor_pid}\n"
    )


@pytest.fixture
def service_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    unit_root = home / ".config" / "systemd" / "user"
    runtime_root = home / ".agents" / "core" / "runtime"
    log_root = home / ".agents" / "log"
    control_root = home / ".agents" / "control"
    repo_root = tmp_path / "repo"
    for path in (bin_dir, unit_root, runtime_root, log_root, control_root, repo_root):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PSC_INSTANCE", "beta")
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(control_root))
    monkeypatch.setenv("PSC_MANAGER_SPECS_DIR", str(tmp_path / "specs"))
    monkeypatch.setenv("PSC_MANAGER_INTERVAL_SECONDS", "120")

    return {
        "tmp_path": tmp_path,
        "home": home,
        "bin_dir": bin_dir,
        "unit_root": unit_root,
        "runtime_root": runtime_root,
        "log_root": log_root,
        "control_root": control_root,
        "repo_root": repo_root,
    }


def test_service_install_json_wraps_existing_installer(
    service_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from paulsha_cortex.deploy import installer

    seen: dict[str, list[str]] = {}

    def fake_install(argv):
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(installer, "main", fake_install)

    argv = [
        "service",
        "install",
        "--instance",
        "beta",
        "--repo-root",
        str(service_runtime["repo_root"]),
        "--interval",
        "60",
        "--json",
    ]
    assert _run_cli(argv) == 0

    payload = json.loads(capsys.readouterr().out)
    assert seen["argv"] == ["service", "--instance", "beta", "--repo-root", str(service_runtime["repo_root"]), "--interval", "60"]
    assert payload["schema"] == SERVICE_SCHEMA
    assert payload["command"] == "install"
    assert payload["instance"] == "beta"
    assert payload["mode"] == "systemd"
    assert payload["result"]["exit_code"] == 0


@pytest.mark.parametrize(("command", "verb"), [("start", "start"), ("stop", "stop"), ("restart", "restart")])
def test_service_lifecycle_commands_operate_manager_service_and_timer_together(
    service_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    verb: str,
) -> None:
    call_log = service_runtime["tmp_path"] / f"systemctl-{command}.log"
    show_payload = service_runtime["tmp_path"] / f"systemctl-{command}.txt"
    manager_exec = service_runtime["tmp_path"] / "venv" / "bin" / "python"
    manager_exec.parent.mkdir(parents=True, exist_ok=True)
    manager_exec.write_text("", encoding="utf-8")
    missing_monitor_exec = service_runtime["tmp_path"] / "missing-venv" / "bin" / "python"
    _write_units(service_runtime["unit_root"], "beta", exec_path=manager_exec, monitor_exec_path=missing_monitor_exec)
    show_payload.write_text(_systemctl_show("beta", manager_pid=4321), encoding="utf-8")
    _write_systemctl_stub(service_runtime["bin_dir"], call_log, show_payload)
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "9.9.9")

    assert _run_cli(["service", command, "--instance", "beta", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    calls = call_log.read_text(encoding="utf-8")
    assert payload["schema"] == SERVICE_SCHEMA
    assert payload["command"] == command
    assert payload["mode"] == "systemd"
    assert f"--user {verb}" in calls
    assert "beta-manager.service" in calls
    assert "beta-manager.timer" in calls


def test_service_status_reports_systemd_runtime_and_env_summary(
    service_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    call_log = service_runtime["tmp_path"] / "systemctl-status.log"
    show_payload = service_runtime["tmp_path"] / "systemctl-status.txt"
    manager_exec = service_runtime["tmp_path"] / "venv" / "bin" / "python"
    manager_exec.parent.mkdir(parents=True, exist_ok=True)
    manager_exec.write_text("", encoding="utf-8")
    missing_monitor_exec = service_runtime["tmp_path"] / "gone" / "bin" / "python"
    _write_units(service_runtime["unit_root"], "beta", exec_path=manager_exec, monitor_exec_path=missing_monitor_exec)
    (service_runtime["runtime_root"] / "beta-manager.env").write_text(
        "PY=/custom/python\n"
        "PSC_MANAGER_INTERVAL_SECONDS=120\n"
        f"PSC_MANAGER_SPECS_DIR={service_runtime['tmp_path'] / 'specs'}\n",
        encoding="utf-8",
    )
    show_payload.write_text(_systemctl_show("beta", manager_pid=4321, monitor_pid=8765), encoding="utf-8")
    _write_systemctl_stub(service_runtime["bin_dir"], call_log, show_payload)
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "9.9.9")

    assert _run_cli(["service", "status", "--instance", "beta", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == SERVICE_SCHEMA
    assert payload["command"] == "status"
    assert payload["service"]["mode"] == "systemd"
    assert payload["service"]["version"] == "9.9.9"
    assert payload["service"]["pid"] == 4321
    assert payload["service"]["env"]["executor"] == "/custom/python"
    assert payload["service"]["env"]["interval_seconds"] == 120
    assert payload["service"]["env"]["specs_dir"] == str(service_runtime["tmp_path"] / "specs")
    assert payload["service"]["units"]["beta-monitor.service"]["stale"] is True

    assert _run_cli(["service", "status", "--instance", "beta"]) == 0
    human = capsys.readouterr().out
    assert "mode: systemd" in human
    assert "version: 9.9.9" in human
    assert "beta-monitor.service" in human
    assert "stale" in human.lower()


def test_service_status_reports_fallback_mode_from_live_manager_lock(
    service_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    (service_runtime["control_root"] / "manager.lock").write_text(
        json.dumps({"pid": os.getpid()}),
        encoding="utf-8",
    )
    (service_runtime["log_root"] / "manager.log").write_text("fallback line 1\nfallback line 2\n", encoding="utf-8")

    assert _run_cli(["service", "status", "--instance", "beta", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == SERVICE_SCHEMA
    assert payload["command"] == "status"
    assert payload["service"]["mode"] == "fallback"
    assert payload["service"]["pid"] == os.getpid()
    assert payload["service"]["log_path"] == str(service_runtime["log_root"] / "manager.log")


def test_service_status_reports_none_mode_with_install_hint(
    service_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["service", "status", "--instance", "beta", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == SERVICE_SCHEMA
    assert payload["command"] == "status"
    assert payload["service"]["mode"] == "none"
    assert payload["service"]["suggested_commands"] == ["cortex service install --instance beta"]


def test_service_logs_uses_journalctl_when_systemd_units_exist(
    service_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    show_payload = service_runtime["tmp_path"] / "systemctl-logs.txt"
    systemctl_calls = service_runtime["tmp_path"] / "systemctl-logs.calls"
    journalctl_calls = service_runtime["tmp_path"] / "journalctl.calls"
    journal_output = service_runtime["tmp_path"] / "journalctl.out"
    manager_exec = service_runtime["tmp_path"] / "venv" / "bin" / "python"
    manager_exec.parent.mkdir(parents=True, exist_ok=True)
    manager_exec.write_text("", encoding="utf-8")
    _write_units(service_runtime["unit_root"], "beta", exec_path=manager_exec, monitor_exec_path=manager_exec)
    show_payload.write_text(_systemctl_show("beta", manager_pid=4321, monitor_pid=8765), encoding="utf-8")
    journal_output.write_text("journal line 1\njournal line 2\n", encoding="utf-8")
    _write_systemctl_stub(service_runtime["bin_dir"], systemctl_calls, show_payload)
    _write_journalctl_stub(service_runtime["bin_dir"], journalctl_calls, journal_output)

    assert _run_cli(["service", "logs", "--instance", "beta", "--follow", "-n", "5"]) == 0

    captured = capsys.readouterr()
    assert "journal line 1" in captured.out
    calls = journalctl_calls.read_text(encoding="utf-8")
    assert "--user" in calls
    assert "-n 5" in calls
    assert "beta-manager.service" in calls


def test_service_logs_reads_fallback_log_when_systemd_is_unavailable(
    service_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = service_runtime["log_root"] / "manager.log"
    log_path.write_text("old line\nrecent line\nnewest line\n", encoding="utf-8")
    (service_runtime["control_root"] / "manager.lock").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    assert _run_cli(["service", "logs", "--instance", "beta", "-n", "2"]) == 0

    captured = capsys.readouterr()
    assert "recent line" in captured.out
    assert "newest line" in captured.out
    assert "old line" not in captured.out


@pytest.mark.parametrize(("purge", "env_exists"), [(False, True), (True, False)])
def test_service_uninstall_only_purges_runtime_env_with_flag(
    service_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    purge: bool,
    env_exists: bool,
) -> None:
    show_payload = service_runtime["tmp_path"] / f"systemctl-uninstall-{purge}.txt"
    systemctl_calls = service_runtime["tmp_path"] / f"systemctl-uninstall-{purge}.calls"
    manager_exec = service_runtime["tmp_path"] / "venv" / "bin" / "python"
    manager_exec.parent.mkdir(parents=True, exist_ok=True)
    manager_exec.write_text("", encoding="utf-8")
    _write_units(service_runtime["unit_root"], "beta", exec_path=manager_exec, monitor_exec_path=manager_exec)
    env_file = service_runtime["runtime_root"] / "beta-manager.env"
    env_file.write_text("PY=/custom/python\n", encoding="utf-8")
    show_payload.write_text(_systemctl_show("beta", manager_pid=4321, monitor_pid=8765), encoding="utf-8")
    _write_systemctl_stub(service_runtime["bin_dir"], systemctl_calls, show_payload)

    argv = ["service", "uninstall", "--instance", "beta", "--json"]
    if purge:
        argv.insert(3, "--purge")
    assert _run_cli(argv) == 0

    payload = json.loads(capsys.readouterr().out)
    calls = systemctl_calls.read_text(encoding="utf-8")
    assert payload["schema"] == SERVICE_SCHEMA
    assert payload["command"] == "uninstall"
    assert payload["mode"] == "systemd"
    assert not (service_runtime["unit_root"] / "beta-manager.service").exists()
    assert not (service_runtime["unit_root"] / "beta-manager.timer").exists()
    assert not (service_runtime["unit_root"] / "beta-monitor.service").exists()
    assert env_file.exists() is env_exists
    assert "--user stop" in calls
    assert "--user disable" in calls
