from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from sandbox_support import requires_af_unix_bind
from socket_fixtures import make_short_socket_dir

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


#: #608：`_layout` 在短固定根下造出來的假 home，於本模組跑完後統一清掉。
#: 用 module-scope fixture 而非改 `_layout` 的簽章：後者要動 15 個呼叫端，而這裡
#: 真正要保證的只有「socket 的家夠短」與「跑完不留垃圾」兩件事。
_SHORT_HOME_ROOTS: list[Path] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_short_home_roots():
    yield
    for path in _SHORT_HOME_ROOTS:
        shutil.rmtree(path, ignore_errors=True)
    _SHORT_HOME_ROOTS.clear()


def _layout(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    # #608：這個假 home 底下住著 monitor 的 AF_UNIX socket
    # （`<home>/.agents/run/cortex/project-monitor.sock`，光固定段就 39 bytes），
    # 吃 `sun_path` 的 107 bytes 上限。`tmp_path` 掛在 `TMPDIR` 下、長度由環境
    # 決定，實測連預設 `/tmp` 都已經因為測試名夠長而超限（doctor 過去不量長度，
    # 所以沒人發現）。home 因此改掛短固定根；`preflight` 與 workspace 沒有長度
    # 上限，照舊留在 `tmp_path`。
    home = make_short_socket_dir(prefix="doctor") / "home"
    _SHORT_HOME_ROOTS.append(home.parent)
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
        f"PSC_PREFLIGHT_CMD={preflight}\n"
        # #540：deck 的 build 卡宣告了 test_policy，harvest 端因此要求 ledger 有
        # `pytest`；沒有這行宣告的部署，builder 交付的合格成果會在採信階段被
        # `gate-ledger-missing-expected-gate` 拒絕，doctor 現在會事前擋下。
        "PSC_GATE_CMD_PYTEST=python3 -m pytest -q\n",
        encoding="utf-8",
    )
    env = {
        "HOME": str(home),
        "PSC_AGENTS_ROOT": str(agents),
        "PSC_PREFLIGHT_CMD": str(preflight),
        "PSC_PROJECT_CONFIG_ROOT": str(identity.parent),
    }
    return home, env


def _init_git_repo_with_origin(path: Path, url: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)
    return path


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
    lowered = result.detail.lower()
    assert "shell wrapper" in lowered
    assert "typed" in lowered


@pytest.mark.parametrize(
    ("exc", "needles"),
    [
        ("PSC_PREFLIGHT_CMD is required", ("required", "set")),
        ("PSC_PREFLIGHT_CMD is malformed", ("malformed", "typed")),
        ("PSC_PREFLIGHT_CMD shell wrapper is not allowed", ("shell-wrapper-not-allowed", "typed")),
        (
            "PSC_PREFLIGHT_CMD executable unavailable: /private/operator/bin/preflight",
            ("executable-unavailable",),
        ),
    ],
)
def test_preflight_probe_returns_actionable_categories_for_known_errors(
    monkeypatch, exc: str, needles: tuple[str, ...]
) -> None:
    def reject(_env):
        raise ValueError(exc)

    monkeypatch.setattr("paulsha_cortex.doctor._load_runtime_preflight_command", reject)
    detail = _preflight_probe({"PSC_PREFLIGHT_CMD": "/usr/bin/env true"}).detail
    lowered = detail.lower()
    assert "PSC_PREFLIGHT_CMD" in detail
    assert all(needle in lowered for needle in needles)
    assert "/private/operator" not in detail


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
    lowered = result.detail.lower()
    assert "validation" in lowered


@pytest.mark.parametrize(
    ("exc", "needles"),
    [
        ("model-identities missing", ("missing", "model-identities", "registry-missing")),
        (
            "model-identities unreadable: /private/operator/config/model-identities.yaml",
            ("unreadable", "model-identities", "registry-unreadable"),
        ),
        (
            "model-identities schema_version must be one of [1, 2], got 99",
            ("schema", "contract", "registry-invalid", "model-identities"),
        ),
        ("canonical agy planning identity missing", ("canonical", "agy", "planning", "registry-invalid")),
    ],
)
def test_identity_probe_returns_actionable_categories_for_known_errors(
    monkeypatch, tmp_path: Path, exc: str, needles: tuple[str, ...]
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "model-identities.yaml").write_text("schema_version: 1\nidentities: []\n", encoding="utf-8")

    def reject(_root):
        raise ValueError(exc)

    monkeypatch.setattr("paulsha_cortex.doctor._load_runtime_model_identities", reject)
    detail = _identity_probe({"PSC_PROJECT_CONFIG_ROOT": str(config)}, tmp_path).detail
    lowered = detail.lower()
    assert all(needle in lowered for needle in needles)
    assert "/private/operator" not in detail


def test_identity_probe_unknown_error_uses_safe_boundary_message(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "model-identities.yaml").write_text("schema_version: 1\nidentities: []\n", encoding="utf-8")

    def reject(_root):
        raise RuntimeError("TOP_SECRET_MARKER runtime crash")

    monkeypatch.setattr("paulsha_cortex.doctor._load_runtime_model_identities", reject)
    detail = _identity_probe({"PSC_PROJECT_CONFIG_ROOT": str(config)}, tmp_path).detail
    lower = detail.lower()
    assert "runtime validator rejected" not in lower
    assert "top_secret_marker" not in detail
    assert "top secret marker" not in lower
    assert "TOP_SECRET_MARKER" not in detail
    assert "/private/operator" not in detail


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
        # model_id 刻意避開 packaged roster 的 claude/sonnet（#452 B），
        # 否則 overlay 覆蓋同鍵身分會觸發 shadow fail-closed。
        "    model_id: sonnet-reviewer\n"
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
        # model_id 刻意避開 packaged roster 的 claude/sonnet（#452 B），
        # 否則 overlay 覆蓋同鍵身分會觸發 shadow fail-closed。
        "    model_id: sonnet-reviewer\n"
        "    independence_domain: anthropic\n"
        "    capabilities: [review]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor.shutil.which",
        lambda name, path=None: f"/tools/{name}",
    )
    calls: list[list[str]] = []
    configured_sandbox = {}

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
        if argv[:2] == ["/tools/srt", "--settings"]:
            configured_sandbox.update(
                json.loads(Path(argv[2]).read_text(encoding="utf-8"))
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
    assert any(
        argv[0] == "/tools/srt" and argv[3:5] == ["--", "/tools/python3"]
        for argv in calls
    )
    assert configured_sandbox["filesystem"]["denyRead"][-1] == "/run/docker.sock"
    assert "/var/run/docker.sock" not in configured_sandbox["filesystem"]["denyRead"]


def test_review_sandbox_probe_rejects_unsupported_claude_version(
    tmp_path: Path, monkeypatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "model-identities.yaml").write_text(
        "schema_version: 2\n"
        "identities:\n"
        "  - executor: claude\n"
        # model_id 刻意避開 packaged roster 的 claude/sonnet（#452 B），
        # 否則 overlay 覆蓋同鍵身分會觸發 shadow fail-closed。
        "    model_id: sonnet-reviewer\n"
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
        # model_id 刻意避開 packaged roster 的 claude/sonnet（#452 B），
        # 否則 overlay 覆蓋同鍵身分會觸發 shadow fail-closed。
        "    model_id: sonnet-reviewer\n"
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
        if argv[0] == "/tools/srt" and "--settings" in argv:
            return Result(returncode=1)
        return Result()

    result = _review_sandbox_probe(
        {"PSC_PROJECT_CONFIG_ROOT": str(config), "PATH": "/tools"},
        tmp_path,
        runner=runner,
        live=True,
    )

    assert result.status == "fail"
    assert "configured reviewer sandbox" in result.detail


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


@requires_af_unix_bind
def test_monitor_protocol_probe_rejects_transport_only_listener(
    tmp_path: Path, socket_dir: Path, monkeypatch
) -> None:
    # #608：socket 走短固定根（`tmp_path` 吃 TMPDIR 長度會撞 sun_path）；
    # state root 不 bind，照舊留在 `tmp_path`。
    socket_path = socket_dir / "run" / "project-monitor.sock"
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


# --- #370：gh-auth probe 必須區分 rate limit（暫時性）與真憑證失效 ----------


def test_gh_auth_probe_reports_rate_limit_as_warn_not_authentication_failed(
    tmp_path: Path, monkeypatch,
) -> None:
    """`gh auth status` 撞到 rate limit 時 exit code 也是非零，但這不是
    憑證失效——doctor 誤報 authentication failed 會誤導排障方向（見 #370
    的 runtime 證據：secondary rate limit 被當成 token invalid 排查）。"""
    home, env = _layout(tmp_path)
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_preflight_command",
        lambda environment: (environment["PSC_PREFLIGHT_CMD"],),
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_model_identities",
        lambda config_root: 2,
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor._request_runtime_monitor",
        lambda socket_path, payload: {
            "ok": True,
            "data": {"schema": "cortex-work/v1", "items": [], "sequence": 0},
        },
    )

    def runner(argv, **kwargs):
        if argv[:3] == ["gh", "auth", "status"]:
            return Result(
                returncode=1,
                stderr=(
                    "You have exceeded a secondary rate limit for the OAuth "
                    "App associated with this personal access token."
                ),
            )
        if "repos/acme/demo" in argv:
            return Result(
                raw=(
                    "HTTP/2 200 OK\r\nX-OAuth-Scopes: repo\r\n\r\n"
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
        agy_probe=lambda: (True, "ready"),
    )
    gh_auth = next(item for item in report.probes if item.name == "gh-auth")
    assert gh_auth.status == "warn"
    assert "authentication failed" not in gh_auth.detail
    assert "rate limit" in gh_auth.detail.lower()
    # A rate-limit warn on a required-in-spirit probe must not itself flip
    # the whole report to not-ok (other probes still gate that).
    assert report.ok


def test_gh_auth_probe_still_reports_real_credential_failure_as_fail(
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

    def runner(argv, **kwargs):
        if argv[:3] == ["gh", "auth", "status"]:
            return Result(returncode=1, stderr="The token in ~/.config/gh/hosts.yml is invalid.")
        if "repos/acme/demo" in argv:
            return Result(returncode=1)
        return Result(returncode=1)

    report = run_doctor(
        probe_live=True,
        repo="acme/demo",
        env=env,
        home=home,
        runner=runner,
        agy_probe=lambda: (False, "unavailable"),
    )
    gh_auth = next(item for item in report.probes if item.name == "gh-auth")
    assert gh_auth.status == "fail"
    assert gh_auth.detail == "authentication failed"
    assert not report.ok


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


# --- #366：repo-identity probe（PSC_REPO_ROOT 與 PSC_REPO_IDENTITY 漂移偵測） --


def test_repo_identity_probe_passes_when_stamp_matches_actual_origin(tmp_path: Path) -> None:
    from paulsha_cortex.doctor import _repo_identity_probe

    repo = tmp_path / "repo"
    _init_git_repo_with_origin(repo, "https://github.com/hamanpaul/paulsha-cortex")
    effective = {
        "PSC_REPO_ROOT": str(repo),
        "PSC_REPO_IDENTITY": "git:github.com/hamanpaul/paulsha-cortex",
    }

    result = _repo_identity_probe(effective)

    assert result.status == "pass"


def test_repo_identity_probe_fails_when_stamp_diverges_from_actual_origin(tmp_path: Path) -> None:
    from paulsha_cortex.doctor import _repo_identity_probe

    repo = tmp_path / "repo"
    _init_git_repo_with_origin(repo, "https://github.com/hamanpaul/paulsha-cortex")
    effective = {
        "PSC_REPO_ROOT": str(repo),
        "PSC_REPO_IDENTITY": "git:github.com/other-owner/other-repo",
    }

    result = _repo_identity_probe(effective)

    assert result.status == "fail"
    assert result.required is True
    assert "other-owner/other-repo" in result.detail
    assert "hamanpaul/paulsha-cortex" in result.detail


def test_repo_identity_probe_warns_when_stamp_missing_on_legacy_env(tmp_path: Path) -> None:
    """#366 修復前的舊安裝，env 只有 PSC_REPO_ROOT、沒有 PSC_REPO_IDENTITY，
    屬於預期過渡態，不得把 doctor 拖成 fail。"""
    from paulsha_cortex.doctor import _repo_identity_probe

    repo = tmp_path / "repo"
    repo.mkdir()
    effective = {"PSC_REPO_ROOT": str(repo)}

    result = _repo_identity_probe(effective)

    assert result.status == "warn"
    assert result.required is False


def test_run_doctor_reports_repo_identity_drift_as_overall_failure(
    tmp_path: Path, monkeypatch
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
    repo = tmp_path / "governed-repo"
    _init_git_repo_with_origin(repo, "https://github.com/hamanpaul/paulsha-cortex")
    env_file = home / ".agents" / "core" / "runtime" / "cortex-manager.env"
    content = env_file.read_text(encoding="utf-8").replace(
        "PSC_REPO_ROOT=/repo\n",
        f"PSC_REPO_ROOT={repo}\nPSC_REPO_IDENTITY=git:github.com/other-owner/other-repo\n",
    )
    env_file.write_text(content, encoding="utf-8")

    report = run_doctor(probe_live=False, instance="cortex", env=env, home=home)

    assert report.ok is False
    assert {probe.name for probe in report.probes} >= {
        "repo-identity",
        "service-paths",
        "monitor-state",
        "monitor-socket",
        "preflight",
        "model-identities",
        "agy",
    }
    repo_identity = next(p for p in report.probes if p.name == "repo-identity")
    assert repo_identity.status == "fail"
    assert repo_identity.required is True


# --- #371／#375：managed-path drift probe（潛伏期偵測） -----------------------
#
# preserve_existing 曾把 PSC_PROJECT_CONFIG_ROOT 鎖死成一個早期殘留的錯誤值，
# `cortex install service` 重裝也修不好（#371）；PSC_CONTROL_ROOT 是 #375 新收進
# managed_env 的鍵，同一種 bug class 也可能發生。installer 修好之後，這個 probe
# 是給「還沒重跑 install service 的既有安裝」用的獨立診斷：只要 env 檔裡的值
# 跟目前 PSC_AGENTS_ROOT／instance 會推導出的值對不上，就回報 drift。


def test_managed_path_drift_probe_passes_when_values_match_derivation(tmp_path: Path) -> None:
    from paulsha_cortex.doctor import _managed_path_drift_probe

    agents_root = tmp_path / "agents"
    effective = {
        "PSC_PROJECT_CONFIG_ROOT": str(agents_root / "config" / "paulsha"),
        "PSC_CONTROL_ROOT": str(agents_root / "control" / "cortex"),
    }

    result = _managed_path_drift_probe(effective, agents_root=agents_root, instance="cortex")

    assert result.status == "pass"
    assert result.required is False


def test_managed_path_drift_probe_warns_when_control_root_absent_legacy_env(
    tmp_path: Path,
) -> None:
    """#375 修復前的舊安裝：env 只有 PSC_PROJECT_CONFIG_ROOT，PSC_CONTROL_ROOT
    整個沒被 installer 寫過，屬預期過渡態，不得把 doctor 拖成 fail。"""
    from paulsha_cortex.doctor import _managed_path_drift_probe

    agents_root = tmp_path / "agents"
    effective = {"PSC_PROJECT_CONFIG_ROOT": str(agents_root / "config" / "paulsha")}

    result = _managed_path_drift_probe(effective, agents_root=agents_root, instance="cortex")

    assert result.status == "warn"
    assert result.required is False
    assert "PSC_CONTROL_ROOT" in result.detail


def test_managed_path_drift_probe_fails_when_project_config_root_diverges(
    tmp_path: Path,
) -> None:
    """issue #371 的精確重現：PSC_PROJECT_CONFIG_ROOT 已存在，但值不是目前
    agents_root 會推導出的值——這正是 preserve_existing 鎖死掉的那個錯誤值。"""
    from paulsha_cortex.doctor import _managed_path_drift_probe

    agents_root = tmp_path / "agents" / "instances" / "hippo-open-issues"
    stale_shared_root = tmp_path / "agents" / "config" / "paulsha"
    effective = {
        "PSC_PROJECT_CONFIG_ROOT": str(stale_shared_root),
        "PSC_CONTROL_ROOT": str(agents_root / "control" / "hippo"),
    }

    result = _managed_path_drift_probe(effective, agents_root=agents_root, instance="hippo")

    assert result.status == "fail"
    assert result.required is True
    assert "PSC_PROJECT_CONFIG_ROOT" in result.detail
    assert str(stale_shared_root) in result.detail
    assert str(agents_root / "config" / "paulsha") in result.detail
    assert "cortex install service" in result.detail


def test_managed_path_drift_probe_fails_when_control_root_diverges(tmp_path: Path) -> None:
    from paulsha_cortex.doctor import _managed_path_drift_probe

    agents_root = tmp_path / "agents"
    effective = {
        "PSC_PROJECT_CONFIG_ROOT": str(agents_root / "config" / "paulsha"),
        "PSC_CONTROL_ROOT": str(tmp_path / "shared-control"),
    }

    result = _managed_path_drift_probe(effective, agents_root=agents_root, instance="cortex")

    assert result.status == "fail"
    assert result.required is True
    assert "PSC_CONTROL_ROOT" in result.detail


def test_run_doctor_includes_managed_path_drift_probe_and_stays_ok_when_matching(
    tmp_path: Path, monkeypatch
) -> None:
    """happy path（`_layout()` 目前寫的 PSC_PROJECT_CONFIG_ROOT 是正確推導值、
    PSC_CONTROL_ROOT 尚未寫入即為 legacy warn）不得讓整體 doctor 變 fail。"""
    home, env = _layout(tmp_path)
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_preflight_command",
        lambda environment: (environment["PSC_PREFLIGHT_CMD"],),
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_model_identities",
        lambda config_root: 2,
    )

    report = run_doctor(probe_live=False, instance="cortex", env=env, home=home)

    assert report.ok
    drift = next(p for p in report.probes if p.name == "managed-path-drift")
    assert drift.status == "warn"


def test_run_doctor_reports_managed_path_drift_as_overall_failure(
    tmp_path: Path, monkeypatch
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
    env_file = home / ".agents" / "core" / "runtime" / "cortex-manager.env"
    stale_shared_root = home / ".agents" / "config" / "paulsha"
    isolated_agents_root = home / ".agents" / "instances" / "hippo-open-issues"
    content = env_file.read_text(encoding="utf-8")
    content = content.replace(
        f"PSC_AGENTS_ROOT={home / '.agents'}\n",
        f"PSC_AGENTS_ROOT={isolated_agents_root}\n",
    )
    content = content.replace(
        f"PSC_PROJECT_CONFIG_ROOT={home / '.agents' / 'config' / 'paulsha'}\n",
        f"PSC_PROJECT_CONFIG_ROOT={stale_shared_root}\n",
    )
    env_file.write_text(content, encoding="utf-8")
    (isolated_agents_root / "config" / "paulsha").mkdir(parents=True, exist_ok=True)
    (isolated_agents_root / "config" / "paulsha" / "model-identities.yaml").write_text(
        "schema_version: 1\nidentities: []\n", encoding="utf-8"
    )

    report = run_doctor(probe_live=False, instance="cortex", env=env, home=home)

    assert report.ok is False
    drift = next(p for p in report.probes if p.name == "managed-path-drift")
    assert drift.status == "fail"
    assert drift.required is True
    assert "PSC_PROJECT_CONFIG_ROOT" in drift.detail


def test_run_doctor_reports_missing_gate_declaration_as_overall_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """#540：manager env 缺 `PSC_GATE_CMD_*` 宣告時，doctor 必須在開工前就紅。

    現場（run `workflow-084f75e2178cf7547476`）是 builder 跑完、交付了合格的 RED
    commit 之後才在採信階段撞 `gate-ledger-missing-expected-gate`，而錯誤只進
    `manager.log`。
    """

    home, env = _layout(tmp_path)
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_preflight_command",
        lambda environment: (environment["PSC_PREFLIGHT_CMD"],),
    )
    monkeypatch.setattr(
        "paulsha_cortex.doctor._load_runtime_model_identities",
        lambda config_root: 2,
    )
    env_file = home / ".agents" / "core" / "runtime" / "cortex-manager.env"
    env_file.write_text(
        "".join(
            line + "\n"
            for line in env_file.read_text(encoding="utf-8").splitlines()
            if not line.startswith("PSC_GATE_CMD_")
        ),
        encoding="utf-8",
    )

    report = run_doctor(probe_live=False, instance="cortex", env=env, home=home)

    assert report.ok is False
    gates = next(p for p in report.probes if p.name == "gate-declarations")
    assert gates.status == "fail"
    assert gates.required is True
    assert "pytest" in gates.detail


def test_run_doctor_passes_gate_declaration_probe_when_declared(
    tmp_path: Path, monkeypatch
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

    report = run_doctor(probe_live=False, instance="cortex", env=env, home=home)

    assert report.ok
    gates = next(p for p in report.probes if p.name == "gate-declarations")
    assert gates.status == "pass"


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
