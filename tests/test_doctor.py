from __future__ import annotations

import json
import socket
import sys
import threading
import types
from pathlib import Path

import pytest

from paulsha_cortex import cli
from paulsha_cortex.doctor import (
    DoctorReport,
    ProbeResult,
    _identity_probe,
    _load_bootstrap_environment,
    _load_runtime_monitor_socket_path,
    _monitor_path_probes,
    _preflight_probe,
    _review_sandbox_probe,
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
    monitor_config = identity.parent / "project-cortex.yaml"
    monitor_config.write_text(
        "workspaces:\n"
        "  - name: test\n"
        f"    path: {tmp_path}\n",
        encoding="utf-8",
    )
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in ("cortex-manager.service", "cortex-manager.timer", "cortex-monitor.service"):
        content = "[Unit]\n"
        if name.endswith(".service"):
            content += "EnvironmentFile=-%h/.agents/core/runtime/cortex.env\n"
            content += "EnvironmentFile=-%h/.agents/core/runtime/cortex-manager.env\n"
        (unit_dir / name).write_text(content, encoding="utf-8")
    runtime = agents / "core" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "cortex-manager.env").write_text(
        "PSC_REPO_ROOT=/repo\n"
        f"PSC_AGENTS_ROOT={agents}\n"
        f"PSC_RUN_ROOT={agents / 'run' / 'cortex'}\n"
        f"PSC_MONITOR_STATE_ROOT={agents / 'monitor'}\n"
        f"PSC_PROJECT_CONFIG_ROOT={agents / 'config' / 'paulsha'}\n"
        f"PSC_PREFLIGHT_CMD={preflight}\n",
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
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_preflight_command",
        lambda environment: (environment["PSC_PREFLIGHT_CMD"],),
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_model_identities",
        lambda config_root: 2,
    )
    monitor_calls = []

    def monitor_request(socket_path, payload):
        monitor_calls.append((socket_path, payload))
        return {
            "ok": True,
            "data": {"schema": "cortex-work/v1", "items": [], "sequence": 0},
        }

    monkeypatch.setattr("paulsha_cortex.doctor._request_runtime_monitor", monitor_request)
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
    assert report.ok
    assert monitor_calls == [
        (
            home / ".agents" / "run" / "cortex" / "project-monitor.sock",
            {"kind": "list_work_items", "states": [], "include_done": False, "explain": False},
        )
    ]
    assert {probe.name for probe in report.probes} >= {
        "gh-auth",
        "gh-permissions",
        "auto-label",
        "preflight",
        "model-identities",
        "agy",
        "service-paths",
        "monitor-state",
        "monitor-socket",
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


def test_review_sandbox_probe_requires_dependencies_only_for_claude_reviewer(
    tmp_path: Path, monkeypatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    identity = config / "model-identities.yaml"
    identity.write_text(
        "schema_version: 2\n"
        "identities:\n"
        "  - executor: claude\n"
        "    model_id: sonnet\n"
        "    independence_domain: anthropic\n"
        "    capabilities: [review]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor.shutil.which",
        lambda name, path=None: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    result = _review_sandbox_probe(
        {"PSC_PROJECT_CONFIG_ROOT": str(config), "PATH": "/usr/bin"}, tmp_path
    )
    assert result.status == "fail"
    assert result.required is True
    assert "socat" in result.detail

    identity.write_text("schema_version: 2\nidentities: []\n", encoding="utf-8")
    optional = _review_sandbox_probe(
        {"PSC_PROJECT_CONFIG_ROOT": str(config), "PATH": "/usr/bin"}, tmp_path
    )
    assert optional.status == "warn"
    assert optional.required is False


def test_review_sandbox_probe_executes_supported_cli_and_native_smoke(
    tmp_path: Path, monkeypatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "model-identities.yaml").write_text(
        "schema_version: 2\n"
        "identities:\n"
        "  - executor: claude\n"
        "    model_id: sonnet\n"
        "    independence_domain: anthropic\n"
        "    capabilities: [review]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor.shutil.which",
        lambda name, path=None: f"/tools/{name}",
    )
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(list(argv))
        if argv == ["/tools/claude", "--version"]:
            return Result(raw="2.1.214 (Claude Code)\n")
        if argv == ["/tools/claude", "--help"]:
            return Result(
                raw=" ".join(
                    (
                        "--disable-slash-commands",
                        "--json-schema",
                        "--permission-mode",
                        "--safe-mode",
                        "--setting-sources",
                        "--settings",
                        "--tools",
                    )
                )
            )
        return Result()

    result = _review_sandbox_probe(
        {"PSC_PROJECT_CONFIG_ROOT": str(config), "PATH": "/tools"},
        tmp_path,
        runner=runner,
        live=True,
    )

    assert result.status == "pass"
    assert ["/tools/bwrap", "--version"] in calls
    assert ["/tools/socat", "-V"] in calls
    assert ["/tools/srt", "--version"] in calls
    assert any(argv[:2] == ["/tools/bwrap", "--ro-bind"] for argv in calls)
    assert any(argv[:3] == ["/tools/srt", "--", "/tools/python3"] for argv in calls)


def test_review_sandbox_probe_rejects_unsupported_claude_version(
    tmp_path: Path, monkeypatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "model-identities.yaml").write_text(
        "schema_version: 2\n"
        "identities:\n"
        "  - executor: claude\n"
        "    model_id: sonnet\n"
        "    independence_domain: anthropic\n"
        "    capabilities: [review]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor.shutil.which",
        lambda name, path=None: f"/tools/{name}",
    )

    result = _review_sandbox_probe(
        {"PSC_PROJECT_CONFIG_ROOT": str(config), "PATH": "/tools"},
        tmp_path,
        runner=lambda argv, **kwargs: Result(raw="2.1.186 (Claude Code)\n"),
    )

    assert result.status == "fail"
    assert "2.1.187" in result.detail


def test_review_sandbox_probe_rejects_degraded_unix_socket_filter(
    tmp_path: Path, monkeypatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "model-identities.yaml").write_text(
        "schema_version: 2\n"
        "identities:\n"
        "  - executor: claude\n"
        "    model_id: sonnet\n"
        "    independence_domain: anthropic\n"
        "    capabilities: [review]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor.shutil.which",
        lambda name, path=None: f"/tools/{name}",
    )

    def runner(argv, **_kwargs):
        if argv == ["/tools/claude", "--version"]:
            return Result(raw="2.1.214 (Claude Code)\n")
        if argv == ["/tools/claude", "--help"]:
            return Result(
                raw=" ".join(
                    (
                        "--disable-slash-commands",
                        "--json-schema",
                        "--permission-mode",
                        "--safe-mode",
                        "--setting-sources",
                        "--settings",
                        "--tools",
                    )
                )
            )
        if argv[:3] == ["/tools/srt", "--", "/tools/python3"]:
            return Result(returncode=1)
        return Result()

    result = _review_sandbox_probe(
        {"PSC_PROJECT_CONFIG_ROOT": str(config), "PATH": "/tools"},
        tmp_path,
        runner=runner,
        live=True,
    )

    assert result.status == "fail"
    assert "Unix socket seccomp" in result.detail


def test_monitor_path_probe_rejects_relative_socket_root(tmp_path: Path) -> None:
    state, monitor_socket = _monitor_path_probes(
        state_root=tmp_path / "state",
        socket_path=Path("relative/project-monitor.sock"),
        live=True,
    )
    assert state.status == "pass"
    assert monitor_socket.status == "fail"
    assert "absolute" in monitor_socket.detail


def test_monitor_live_probe_requires_connectable_unix_socket(tmp_path: Path) -> None:
    state, monitor_socket = _monitor_path_probes(
        state_root=tmp_path / "state",
        socket_path=tmp_path / "run" / "project-monitor.sock",
        live=True,
    )
    assert state.status == "pass"
    assert monitor_socket.status == "fail"
    assert "monitor socket" in monitor_socket.detail


def test_monitor_protocol_probe_rejects_transport_only_listener(tmp_path: Path, monkeypatch) -> None:
    socket_path = tmp_path / "run" / "project-monitor.sock"
    socket_path.parent.mkdir()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)

    def accept_then_close() -> None:
        connection, _address = listener.accept()
        with connection:
            connection.recv(4096)

    acceptor = threading.Thread(target=accept_then_close, daemon=True)
    acceptor.start()

    class FakeProductionClient:
        def __init__(self, socket_path, *, timeout):
            self.socket_path = socket_path
            self.timeout = timeout

        def request(self, _payload):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.socket_path))
                client.sendall(b'{"kind":"list_work_items"}\n')
                if not client.recv(4096):
                    raise RuntimeError("monitor socket returned no response")
            return {}

    module = types.ModuleType("paulsha_cortex.monitor.work_api")
    module.MonitorSocketClient = FakeProductionClient
    monkeypatch.setitem(sys.modules, "paulsha_cortex.monitor.work_api", module)
    try:
        _state, monitor_socket = _monitor_path_probes(
            state_root=tmp_path / "state",
            socket_path=socket_path,
            live=True,
        )
    finally:
        listener.close()
        acceptor.join(timeout=1)
    assert monitor_socket.status == "fail"
    assert "API probe failed" in monitor_socket.detail


def test_bootstrap_environment_and_monitor_config_select_custom_socket(tmp_path: Path) -> None:
    home, env = _layout(tmp_path)
    runtime = home / ".agents" / "core" / "runtime"
    (runtime / "cortex.env").write_text(
        f"PSC_RUN_ROOT={tmp_path / 'base-run'}\n",
        encoding="utf-8",
    )
    custom_socket = tmp_path / "custom" / "monitor.sock"
    config_root = home / ".agents" / "config" / "paulsha"
    (config_root / "project-cortex.yaml").write_text(
        "workspaces:\n"
        "  - name: test\n"
        f"    path: {tmp_path}\n"
        "monitor:\n"
        f"  socket_path: {custom_socket}\n",
        encoding="utf-8",
    )

    effective = _load_bootstrap_environment(home=home, instance="cortex", base_env=env)
    assert effective["PSC_RUN_ROOT"] == str(home / ".agents" / "run" / "cortex")
    assert _load_runtime_monitor_socket_path(effective) == custom_socket


def test_default_monitor_socket_is_scoped_to_installed_instance(tmp_path: Path) -> None:
    home, env = _layout(tmp_path)
    effective = _load_bootstrap_environment(home=home, instance="cortex", base_env=env)
    assert _load_runtime_monitor_socket_path(effective) == (
        home / ".agents" / "run" / "cortex" / "project-monitor.sock"
    )


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
    assert "bootstrap environment is invalid" in result.detail


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
