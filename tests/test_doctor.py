from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex import cli
from paulsha_cortex.doctor import DoctorReport, ProbeResult, run_doctor


class Result:
    def __init__(self, payload=None, *, returncode=0, stderr=""):
        self.returncode = returncode
        self.stdout = "" if payload is None else json.dumps(payload)
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
        (unit_dir / name).write_text("[Unit]\n", encoding="utf-8")
    runtime = agents / "core" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "cortex-manager.env").write_text("PSC_REPO_ROOT=/repo\n", encoding="utf-8")
    env = {
        "HOME": str(home),
        "PSC_AGENTS_ROOT": str(agents),
        "PSC_PREFLIGHT_CMD": str(preflight),
        "PSC_PROJECT_CONFIG_ROOT": str(identity.parent),
    }
    return home, env


def test_live_doctor_checks_gh_label_preflight_identity_agy_and_service_paths(
    tmp_path: Path,
) -> None:
    home, env = _layout(tmp_path)
    calls = []

    def runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        endpoint = " ".join(argv)
        if argv[:3] == ["gh", "auth", "status"]:
            return Result()
        if endpoint.endswith("repos/acme/demo"):
            return Result({"permissions": {"push": True}})
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
    assert "--probe-live" in capsys.readouterr().out
