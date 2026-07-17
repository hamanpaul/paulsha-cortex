from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from paulsha_cortex import cli
from paulsha_cortex.doctor import (
    DoctorReport,
    ProbeResult,
    _identity_probe,
    _monitor_path_probes,
    _preflight_probe,
    _valid_repo,
    run_doctor,
)


class Result:
    def __init__(self, payload=None, *, returncode=0, stderr="", raw=None):
        self.returncode = returncode
        self.stdout = raw if raw is not None else ("" if payload is None else json.dumps(payload))
        self.stderr = stderr


def _layout(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    agents = home / ".agents"
    preflight = tmp_path / "preflight"
    preflight.write_text("#!/bin/sh\n", encoding="utf-8")
    preflight.chmod(0o700)
    identity = agents / "config" / "paulsha" / "model-identities.yaml"
    identity.parent.mkdir(parents=True)
    identity.write_text("schema_version: 1\nidentities: []\n", encoding="utf-8")
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in ("cortex-manager.service", "cortex-manager.timer", "cortex-monitor.service"):
        content = "[Unit]\n"
        if name.endswith(".service"):
            content += "EnvironmentFile=-%h/.agents/core/runtime/cortex-manager.env\n"
        (unit_dir / name).write_text(content, encoding="utf-8")
    runtime = agents / "core" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "cortex-manager.env").write_text(
        "PSC_REPO_ROOT=/repo\n"
        f"PSC_RUN_ROOT={agents / 'run'}\n"
        f"PSC_MONITOR_STATE_ROOT={agents / 'monitor'}\n"
        f"PSC_PROJECT_CONFIG_ROOT={agents / 'config' / 'paulsha'}\n",
        encoding="utf-8",
    )
    env = {
        "HOME": str(home),
        "PSC_AGENTS_ROOT": str(agents),
        "PSC_PREFLIGHT_CMD": str(preflight),
        "PSC_PROJECT_CONFIG_ROOT": str(identity.parent),
    }
    return home, env


def test_live_doctor_checks_gh_label_preflight_identity_agy_and_service_paths(
    tmp_path: Path, monkeypatch,
) -> None:
    home, env = _layout(tmp_path)
    run_root = home / ".agents" / "run"
    run_root.mkdir(parents=True)
    socket_path = run_root / "project-monitor.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    acceptor = threading.Thread(target=lambda: listener.accept()[0].close(), daemon=True)
    acceptor.start()
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_preflight_command",
        lambda environment: (environment["PSC_PREFLIGHT_CMD"],),
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_model_identities",
        lambda config_root: 2,
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        endpoint = " ".join(argv)
        if argv[:3] == ["gh", "auth", "status"]:
            return Result()
        if endpoint.endswith("--include repos/acme/demo"):
            return Result(
                raw=(
                    "HTTP/2 200 OK\r\n"
                    "X-OAuth-Scopes: repo\r\n\r\n"
                    '{"private":true,"permissions":{"push":true}}'
                )
            )
        if "labels/cortex%3Aauto-on-going" in endpoint:
            return Result({"name": "cortex:auto-on-going"})
        raise AssertionError(argv)

    report = run_doctor(
        probe_live=True,
        repo="acme/demo",
        instance="cortex",
        env=env,
        home=home,
        runner=runner,
        agy_probe=lambda: (True, "Gemini 3.1 Pro (High) / google / ready"),
    )
    try:
        assert report.ok
    finally:
        listener.close()
        acceptor.join(timeout=1)
    assert {probe.name for probe in report.probes} >= {
        "gh-auth",
        "gh-permissions",
        "auto-label",
        "preflight",
        "model-identities",
        "agy",
        "service-paths",
        "monitor-state",
    }
    assert all(call[1]["shell"] is False for call in calls)


def test_doctor_does_not_echo_credentials_from_failed_command(tmp_path: Path) -> None:
    home, env = _layout(tmp_path)
    secret = "ghp_super_secret"

    def runner(argv, **kwargs):
        return Result(returncode=1, stderr=f"auth failed token={secret}")

    report = run_doctor(
        probe_live=True,
        repo="acme/demo",
        env=env,
        home=home,
        runner=runner,
        agy_probe=lambda: (False, secret),
    )
    rendered = json.dumps(report.to_dict())
    assert secret not in rendered
    assert not report.ok


def test_preflight_probe_uses_runtime_validator_and_fails_closed_when_unavailable(monkeypatch) -> None:
    def reject(_env):
        raise ValueError("PSC_PREFLIGHT_CMD shell wrapper is not allowed")

    monkeypatch.setattr("paulsha_cortex.doctor._load_runtime_preflight_command", reject)
    result = _preflight_probe({"PSC_PREFLIGHT_CMD": "/usr/bin/env bash -c true"})
    assert result.status == "fail"
    assert "runtime validator" in result.detail


@pytest.mark.parametrize("repo", ["../demo", "acme/..", "acme/demo/extra", "acme demo/repo"])
def test_repo_validation_rejects_non_owner_name(repo: str) -> None:
    assert not _valid_repo(repo)


def test_identity_probe_uses_runtime_schema_validator(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "model-identities.yaml").write_text(
        "schema_version: 2\nidentities:\n  - executor: agy\n",
        encoding="utf-8",
    )

    def reject(_root):
        raise ValueError("model_id must be a non-empty string")

    monkeypatch.setattr("paulsha_cortex.doctor._load_runtime_model_identities", reject)
    result = _identity_probe({"PSC_PROJECT_CONFIG_ROOT": str(config)}, tmp_path)
    assert result.status == "fail"
    assert "runtime validator" in result.detail


def test_monitor_path_probe_rejects_relative_socket_root(tmp_path: Path) -> None:
    state, monitor_socket = _monitor_path_probes(
        {"PSC_MONITOR_STATE_ROOT": str(tmp_path / "state"), "PSC_RUN_ROOT": "relative"},
        tmp_path,
        live=True,
    )
    assert state.status == "pass"
    assert monitor_socket.status == "fail"
    assert "absolute" in monitor_socket.detail


def test_monitor_live_probe_requires_connectable_unix_socket(tmp_path: Path) -> None:
    state, monitor_socket = _monitor_path_probes(
        {
            "PSC_MONITOR_STATE_ROOT": str(tmp_path / "state"),
            "PSC_RUN_ROOT": str(tmp_path / "run"),
        },
        tmp_path,
        live=True,
    )
    assert state.status == "pass"
    assert monitor_socket.status == "fail"
    assert "not listening" in monitor_socket.detail


def test_github_permission_probe_fails_without_token_scope_proof(tmp_path: Path, monkeypatch) -> None:
    home, env = _layout(tmp_path)
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_preflight_command",
        lambda environment: (environment["PSC_PREFLIGHT_CMD"],),
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_model_identities",
        lambda config_root: 2,
    )

    def runner(argv, **kwargs):
        if argv[:3] == ["gh", "auth", "status"]:
            return Result()
        if "repos/acme/demo" in argv:
            return Result(
                raw=(
                    "HTTP/2 200 OK\r\n\r\n"
                    '{"private":true,"permissions":{"push":true}}'
                )
            )
        return Result({"name": "cortex:auto-on-going"})

    report = run_doctor(
        probe_live=True,
        repo="acme/demo",
        env=env,
        home=home,
        runner=runner,
        agy_probe=lambda: (False, "unavailable"),
    )
    permission = next(item for item in report.probes if item.name == "gh-permissions")
    assert permission.status == "fail"
    assert "not proven" in permission.detail


def test_service_probe_rejects_unit_that_does_not_load_bootstrap_env(tmp_path: Path) -> None:
    from paulsha_cortex.doctor import _service_paths_probe

    home, _env = _layout(tmp_path)
    (home / ".config" / "systemd" / "user" / "cortex-monitor.service").write_text(
        "[Unit]\n",
        encoding="utf-8",
    )
    result = _service_paths_probe(home=home, instance="cortex", live=True)
    assert result.status == "fail"
    assert "inconsistent" in result.detail


def test_doctor_cli_json_and_help(monkeypatch, capsys) -> None:
    report = DoctorReport(
        probes=(ProbeResult("unit", "pass", "ready", True),),
    )
    monkeypatch.setattr("paulsha_cortex.doctor.run_doctor", lambda **kwargs: report)
    assert cli.main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "cortex-doctor/v1"
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["doctor", "--help"])
    assert exit_info.value.code == 0
    help_output = capsys.readouterr().out
    assert "--probe-live" in help_output
    assert "Monitor socket" in help_output
