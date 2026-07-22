from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

from paulsha_cortex.control import constants, contract
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.doctor import DoctorReport, ProbeResult

INSPECT_SCHEMA = "cortex-porcelain/inspect/v1"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_cli():
    for module_name in (
        "paulsha_cortex.cli",
        "paulsha_cortex.porcelain",
        "paulsha_cortex.porcelain.inspect",
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


@pytest.fixture
def inspect_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    agents_root = home / ".agents"
    control_root = tmp_path / "control"
    coordinator_root = tmp_path / "coordinator"
    specs_root = tmp_path / "specs"
    project_config_root = agents_root / "config" / "paulsha"
    run_root = agents_root / "run" / "cortex"
    monitor_state_root = agents_root / "monitor"
    runtime_root = agents_root / "core" / "runtime"
    unit_root = home / ".config" / "systemd" / "user"

    for path in (
        control_root,
        coordinator_root,
        specs_root,
        project_config_root,
        run_root,
        monitor_state_root,
        runtime_root,
        unit_root,
    ):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PSC_INSTANCE", "cortex")
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(agents_root))
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(control_root))
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(coordinator_root))
    monkeypatch.setenv("PSC_SPECS_ROOT", str(specs_root))
    monkeypatch.setenv("PSC_RUN_ROOT", str(run_root))
    monkeypatch.setenv("PSC_MONITOR_STATE_ROOT", str(monitor_state_root))
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(project_config_root))
    monkeypatch.setenv("PSC_REPO_ROOT", str(REPO_ROOT))

    (project_config_root / "model-identities.yaml").write_text(
        "schema_version: 2\nidentities: []\n",
        encoding="utf-8",
    )
    (project_config_root / "project-cortex.yaml").write_text(
        "workspaces:\n"
        f"  - name: demo\n    path: {tmp_path}\n",
        encoding="utf-8",
    )
    (runtime_root / "cortex-manager.env").write_text(
        "\n".join(
            (
                f"PSC_AGENTS_ROOT={agents_root}",
                f"PSC_CONTROL_ROOT={control_root}",
                f"PSC_COORDINATOR_ROOT={coordinator_root}",
                f"PSC_SPECS_ROOT={specs_root}",
                f"PSC_RUN_ROOT={run_root}",
                f"PSC_MONITOR_STATE_ROOT={monitor_state_root}",
                f"PSC_PROJECT_CONFIG_ROOT={project_config_root}",
                f"PSC_REPO_ROOT={REPO_ROOT}",
                "",
            )
        ),
        encoding="utf-8",
    )
    manager_script = REPO_ROOT / "paulsha_cortex" / "scripts" / "service-manager.sh"
    (unit_root / "cortex-manager.service").write_text(
        "[Unit]\n"
        "[Service]\n"
        "EnvironmentFile=-%h/.agents/core/runtime/cortex.env\n"
        "EnvironmentFile=-%h/.agents/core/runtime/cortex-manager.env\n"
        f"ExecStart=/usr/bin/env bash {manager_script}\n",
        encoding="utf-8",
    )
    (unit_root / "cortex-manager.timer").write_text("[Timer]\nOnUnitActiveSec=60\n", encoding="utf-8")
    (unit_root / "cortex-monitor.service").write_text(
        "[Unit]\n"
        "[Service]\n"
        "EnvironmentFile=-%h/.agents/core/runtime/cortex.env\n"
        "EnvironmentFile=-%h/.agents/core/runtime/cortex-manager.env\n"
        f"ExecStart={sys.executable} -m paulsha_cortex.monitor\n",
        encoding="utf-8",
    )

    return {
        "home": home,
        "agents_root": agents_root,
        "control_root": control_root,
        "coordinator_root": coordinator_root,
        "specs_root": specs_root,
        "project_config_root": project_config_root,
        "run_root": run_root,
        "monitor_state_root": monitor_state_root,
        "runtime_root": runtime_root,
        "unit_root": unit_root,
    }


def test_inspect_status_human_and_json_report_same_snapshot(
    inspect_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    updated_at = "2026-07-22T08:00:00+00:00"
    payload = contract.build_status(
        ready=["slice-a"],
        in_flight=[{"job_id": "slice-a-1", "slice_id": "slice-a", "state": "running"}],
        recent_done=[{"slice_id": "slice-z", "gate_status": "passed", "at": updated_at}],
        daemon={"pid": os.getpid(), "last_tick_at": updated_at, "idle": False},
        updated_at=updated_at,
    )
    payload["held"] = [{"slice_id": "slice-held", "reasons": ["dispatch-hold"]}]
    contract.atomic_write_json(constants.status_path(), payload)

    assert _run_cli(["inspect", "status", "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["schema"] == INSPECT_SCHEMA
    assert rendered["command"] == "status"
    assert rendered["status"]["ready"] == ["slice-a"]
    assert rendered["status"]["held"] == [{"slice_id": "slice-held", "reasons": ["dispatch-hold"]}]
    assert rendered["status"]["degraded"] is False

    assert _run_cli(["inspect", "status"]) == 0
    human = capsys.readouterr().out
    assert "slice-a" in human
    assert "slice-held" in human
    assert "degraded" in human


def test_inspect_job_human_and_json_report_same_job_record(
    inspect_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = JobRegistry(state_path=inspect_runtime["coordinator_root"] / "jobs.json")
    job = registry.create_job(
        task="porcelain-inspect-build",
        persona="builder",
        branch="feature/89-porcelain-inspect",
        pane="builder:1",
        worktree=str(inspect_runtime["control_root"] / "wt"),
        workflow_run_id="workflow-" + "a" * 20,
        workflow_repo="hamanpaul/paulsha-cortex",
        workflow_card="tdd-red",
        workflow_phase="build",
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=1)

    assert _run_cli(["inspect", "job", job["job_id"], "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["schema"] == INSPECT_SCHEMA
    assert rendered["command"] == "job"
    assert rendered["job"]["job_id"] == job["job_id"]
    assert rendered["job"]["status"] == "exited"
    assert rendered["job"]["workflow_card"] == "tdd-red"

    assert _run_cli(["inspect", "job", job["job_id"]]) == 0
    human = capsys.readouterr().out
    assert job["job_id"] in human
    assert "tdd-red" in human
    assert "exited" in human


def test_inspect_ready_human_and_json_report_same_ready_slice(
    inspect_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from paulsha_cortex.coordinator import autonomy

    ready = [
        {
            "slice_id": "porcelain-inspect-build",
            "path": str(inspect_runtime["specs_root"] / "porcelain-inspect-build.md"),
            "plan": "docs/superpowers/plans/porcelain-inspect.md",
        }
    ]
    monkeypatch.setattr(autonomy, "scan_specs", lambda _specs_dir: ready)
    monkeypatch.setattr(autonomy, "ready_units", lambda metas, predicate: metas)

    assert _run_cli(["inspect", "ready", "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["schema"] == INSPECT_SCHEMA
    assert rendered["command"] == "ready"
    assert rendered["ready"][0]["slice_id"] == "porcelain-inspect-build"

    assert _run_cli(["inspect", "ready"]) == 0
    human = capsys.readouterr().out
    assert "porcelain-inspect-build" in human
    assert "porcelain-inspect.md" in human


def test_inspect_work_human_and_json_report_same_work_item(
    inspect_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from paulsha_cortex.monitor import work_api

    def fake_request(self, payload):
        if payload.get("kind") not in {"get_work_item", "explain_work_item"}:
            raise AssertionError(payload)
        return {
            "ok": True,
            "data": {
                "schema": "cortex-work/v1",
                "sequence": 7,
                "item": {
                    "repo": "example/acme",
                    "work_id": "porcelain-inspect",
                    "title": "porcelain inspect",
                    "state": "todo",
                    "phase": "plan",
                    "facets": ["openspec"],
                },
            },
        }

    monkeypatch.setattr(work_api.MonitorSocketClient, "request", fake_request)

    argv = ["inspect", "work", "porcelain-inspect", "--repo", "example/acme", "--json"]
    assert _run_cli(argv) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["schema"] == INSPECT_SCHEMA
    assert rendered["command"] == "work"
    assert rendered["item"]["work_id"] == "porcelain-inspect"
    assert rendered["item"]["repo"] == "example/acme"
    assert rendered["item"]["state"] == "todo"

    assert _run_cli(["inspect", "work", "porcelain-inspect", "--repo", "example/acme"]) == 0
    human = capsys.readouterr().out
    assert "example/acme" in human
    assert "porcelain-inspect" in human
    assert "todo" in human


def test_inspect_doctor_human_and_json_report_same_probe_summary(
    inspect_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from paulsha_cortex import doctor as doctor_module

    report = DoctorReport(
        (
            ProbeResult("service-paths", "pass", "effective service environment is valid", True),
            ProbeResult("gh-auth", "warn", "live probe skipped", False),
        )
    )
    monkeypatch.setattr(doctor_module, "run_doctor", lambda **_kwargs: report)

    assert _run_cli(["inspect", "doctor", "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["schema"] == INSPECT_SCHEMA
    assert rendered["command"] == "doctor"
    assert rendered["doctor"]["schema"] == "cortex-doctor/v1"
    assert rendered["doctor"]["ok"] is True
    assert rendered["doctor"]["probes"][0]["name"] == "service-paths"

    assert _run_cli(["inspect", "doctor"]) == 0
    human = capsys.readouterr().out
    assert "service-paths" in human
    assert "gh-auth" in human
    assert "PASS" in human


def test_inspect_service_flags_unit_pointing_to_missing_venv(
    inspect_runtime: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_python = inspect_runtime["home"] / "venvs" / "missing" / "bin" / "python"
    (inspect_runtime["unit_root"] / "cortex-monitor.service").write_text(
        "[Unit]\n"
        "[Service]\n"
        "EnvironmentFile=-%h/.agents/core/runtime/cortex.env\n"
        "EnvironmentFile=-%h/.agents/core/runtime/cortex-manager.env\n"
        f"ExecStart={missing_python} -m paulsha_cortex.monitor\n",
        encoding="utf-8",
    )

    assert _run_cli(["inspect", "service", "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["schema"] == INSPECT_SCHEMA
    assert rendered["command"] == "service"
    assert rendered["service"]["instance"] == "cortex"
    assert rendered["service"]["mode"] == "systemd"
    assert rendered["service"]["units"]["cortex-monitor.service"]["exec_path"] == str(missing_python)
    assert rendered["service"]["units"]["cortex-monitor.service"]["stale"] is True

    assert _run_cli(["inspect", "service"]) == 0
    human = capsys.readouterr().out
    assert "cortex-monitor.service" in human
    assert str(missing_python) in human
    assert "stale" in human.lower()


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (["inspect", "job", "missing-job"], "missing-job"),
        (["inspect", "work", "missing-work", "--repo", "example/acme"], "missing-work"),
    ],
)
def test_inspect_missing_targets_exit_one(
    argv: list[str],
    needle: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from paulsha_cortex.monitor import work_api

    monkeypatch.setattr(
        work_api.MonitorSocketClient,
        "request",
        lambda self, payload: {"ok": False, "error": f"work item not found: {payload['work_id']}"},
    )

    assert _run_cli(argv) == 1
    captured = capsys.readouterr()
    assert needle in (captured.out + captured.err)
