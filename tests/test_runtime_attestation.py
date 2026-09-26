from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from paulsha_cortex.runtime_attestation import (
    RUNTIME_ATTESTATION_SCHEMA,
    artifact_identity,
    artifact_identity_from_package_root,
    compare_runtime_state,
    configuration_revision,
    manager_configuration_snapshot,
    inspect_runtime_state,
    record_config_reload,
    record_runtime_startup,
    service_declaration_projection,
    trust_root_receipt_summary,
)


def _artifact(digest: str, *, kind: str = "installed-wheel") -> dict[str, object]:
    return {
        "kind": kind,
        "package": "paulsha-cortex",
        "package_version": "0.1.10",
        "source_revision": "unknown",
        "sha256": digest,
    }


def _write_fake_install(site: Path, marker: str) -> Path:
    package_root = site / "paulsha_cortex"
    (package_root / "scripts").mkdir(parents=True)
    (package_root / "coordinator").mkdir()
    (package_root / "monitor").mkdir()
    (package_root / "__init__.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    (package_root / "scripts" / "service-manager.sh").write_text(
        f"# {marker}\n", encoding="utf-8"
    )
    (package_root / "coordinator" / "manager_daemon.py").write_text(
        f"MARKER = {marker!r}\n", encoding="utf-8"
    )
    (package_root / "monitor" / "__init__.py").write_text(
        f"MARKER = {marker!r}\n", encoding="utf-8"
    )
    metadata = site / "paulsha_cortex-0.1.10.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: paulsha-cortex\nVersion: 0.1.10\n",
        encoding="utf-8",
    )
    return package_root


def _service_units_with_dropins(
    tmp_path: Path, *, manager_main: str, monitor_main: str
) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for service, command in (("manager", manager_main), ("monitor", monitor_main)):
        unit_name = f"test-{service}.service"
        unit_path = tmp_path / "user" / unit_name
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(f"[Service]\nExecStart={command}\n", encoding="utf-8")
        rows[unit_name] = {
            "path": str(unit_path),
            "status": "active/running",
            "pid": 321 if service == "manager" else 322,
            "exec_path": "/usr/bin/env",
            "stale": False,
        }
    return rows


def test_checkout_source_override_has_digest_but_is_not_an_installed_artifact(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "paulsha_cortex"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

    identity = artifact_identity(package_root=package_root, source_override=True)

    assert identity["kind"] == "source-override"
    assert identity["sha256"] and len(identity["sha256"]) == 64
    assert identity["source_revision"] == "unknown"
    comparison = compare_runtime_state(
        {
            "status": "attested",
            "latest": {"artifact": identity, "config": {"effective_revision": "a"}},
        },
        current_artifact=identity,
        declared_config_revision="a",
    )
    assert comparison["artifact_status"] == "unknown"
    assert comparison["reason"] == "source-override"


def test_runtime_startup_and_config_reload_append_immutable_receipts(tmp_path: Path) -> None:
    state_root = tmp_path / "runtime"
    first = record_runtime_startup(
        service="monitor",
        instance="test",
        state_root=state_root,
        configuration={"poll_interval_seconds": 30},
        artifact=_artifact("1" * 64),
        started_at="2026-09-26T00:00:00Z",
        pid=100,
    )
    first_bytes = first.read_bytes()

    second = record_runtime_startup(
        service="monitor",
        instance="test",
        state_root=state_root,
        configuration={"poll_interval_seconds": 45},
        artifact=_artifact("1" * 64),
        started_at="2026-09-26T01:00:00Z",
        pid=101,
    )
    reload = record_config_reload(
        service="monitor",
        instance="test",
        state_root=state_root,
        previous_receipt=second,
        configuration={"poll_interval_seconds": 60},
        recorded_at="2026-09-26T01:30:00Z",
    )

    assert first.read_bytes() == first_bytes
    assert second != first
    state = inspect_runtime_state(state_root, service="monitor", instance="test")
    assert state["status"] == "attested"
    assert state["latest"]["event_type"] == "config-reload"
    assert state["latest"]["previous_receipt_id"]
    assert state["initial_config_revision"] == state["process_start"]["config"]["initial_revision"]
    assert state["effective_config_revision"] == configuration_revision(
        {"poll_interval_seconds": 60}
    )
    assert reload.exists()


def test_subsecond_restart_selects_the_new_process_without_rewriting_prior_receipt(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "runtime"
    prior = record_runtime_startup(
        service="manager",
        instance="test",
        state_root=state_root,
        configuration={"poll_interval": 30},
        artifact=_artifact("1" * 64),
        started_at="2026-09-26T00:00:00.000001Z",
        pid=115,
    )
    prior_bytes = prior.read_bytes()
    current = record_runtime_startup(
        service="manager",
        instance="test",
        state_root=state_root,
        configuration={"poll_interval": 45},
        artifact=_artifact("2" * 64),
        started_at="2026-09-26T00:00:00.000002Z",
        pid=116,
    )

    state = inspect_runtime_state(state_root, service="manager", instance="test")

    assert state["latest"]["pid"] == 116
    assert state["latest"]["artifact"]["sha256"] == "2" * 64
    assert state["previous_process_start"]["pid"] == 115
    assert current.read_bytes() != prior_bytes
    assert prior.read_bytes() == prior_bytes


@pytest.mark.parametrize("bad_receipt", ["truncated", "unknown-schema"])
def test_missing_corrupt_and_unknown_receipts_remain_unknown(
    tmp_path: Path, bad_receipt: str
) -> None:
    state_root = tmp_path / "runtime"
    absent = inspect_runtime_state(state_root, service="manager", instance="test")
    assert absent["status"] == "unknown"
    assert absent["reason"] == "receipt-missing"

    receipt_dir = state_root / "runtime-attestations"
    receipt_dir.mkdir(parents=True)
    receipt_dir.chmod(0o700)
    path = receipt_dir / "manager-invalid.json"
    if bad_receipt == "truncated":
        path.write_text('{"schema":', encoding="utf-8")
    else:
        path.write_text(
            json.dumps({"schema": "cortex/loaded-runtime-attestation/v99"}),
            encoding="utf-8",
        )
    path.chmod(0o600)

    result = inspect_runtime_state(state_root, service="manager", instance="test")
    assert result["status"] == "unknown"
    assert result["reason"] == (
        "schema-unknown" if bad_receipt == "unknown-schema" else "receipt-corrupt"
    )


def test_disk_artifact_or_config_drift_is_visible_without_rewriting_loaded_receipt(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "runtime"
    original = record_runtime_startup(
        service="manager",
        instance="test",
        state_root=state_root,
        configuration={"interval": 10},
        artifact=_artifact("1" * 64),
        started_at="2026-09-26T00:00:00Z",
        pid=111,
    )
    original_bytes = original.read_bytes()
    state = inspect_runtime_state(state_root, service="manager", instance="test")

    artifact_drift = compare_runtime_state(
        state,
        current_artifact=_artifact("2" * 64),
        declared_config_revision=configuration_revision({"interval": 10}),
    )
    config_drift = compare_runtime_state(
        state,
        current_artifact=_artifact("1" * 64),
        declared_config_revision=configuration_revision({"interval": 20}),
    )

    assert artifact_drift["status"] == "drift"
    assert artifact_drift["artifact_status"] == "drift"
    assert config_drift["status"] == "drift"
    assert config_drift["config_status"] == "drift"
    assert original.read_bytes() == original_bytes


def test_restart_keeps_prior_artifact_and_rollback_receipt_chain(tmp_path: Path) -> None:
    state_root = tmp_path / "runtime"
    prior = record_runtime_startup(
        service="manager",
        instance="test",
        state_root=state_root,
        configuration={"interval": 10},
        artifact=_artifact("1" * 64),
        started_at="2026-09-26T00:00:00Z",
        pid=120,
        trust_root={"status": "verified", "receipt_id": "b73e4da7-ef66-423e-bad0-162075ee6d55"},
    )
    prior_bytes = prior.read_bytes()
    record_runtime_startup(
        service="manager",
        instance="test",
        state_root=state_root,
        configuration={"interval": 10},
        artifact=_artifact("2" * 64),
        started_at="2026-09-26T01:00:00Z",
        pid=121,
        trust_root={
            "status": "rolled-back",
            "receipt_id": "09be5652-d5d3-4fe4-a1bc-84ff45773c67",
            "rollback_revision": "3" * 64,
            "in_flight_jobs": 0,
        },
    )

    state = inspect_runtime_state(state_root, service="manager", instance="test")
    assert state["previous_process_start"]["artifact"]["sha256"] == "1" * 64
    assert state["latest"]["artifact"]["sha256"] == "2" * 64
    assert state["previous_process_start"]["trust_root"]["status"] == "verified"
    assert state["latest"]["trust_root"]["status"] == "rolled-back"
    assert prior.read_bytes() == prior_bytes


def test_trust_root_receipt_summary_binds_install_activation_verify_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paulsha_cortex.trust_root.install import core as install_core

    receipt_path = tmp_path / "install-receipt.json"
    receipt_path.write_text("existing receipt bytes\n", encoding="utf-8")
    document = {
        "receipt_id": "b73e4da7-ef66-423e-bad0-162075ee6d55",
        "plan_sha256": "1" * 64,
        "qualified": True,
        "activation_journal": [
            {"service": "manager", "status": "completed"},
            {"service": "monitor", "status": "completed"},
        ],
        "verification_evidence": {"result": "pass", "sha256": "2" * 64},
        "rollback": {"result": "applied", "prior_artifact": "3" * 64},
    }

    class Receipt:
        _checkpoint_sha256 = "4" * 64

        def to_dict(self):
            return document

    monkeypatch.setattr(install_core.InstallReceipt, "load", lambda _path: Receipt())
    summary = trust_root_receipt_summary(receipt_path)

    assert summary["status"] == "rolled-back"
    assert summary["receipt_id"] == document["receipt_id"]
    assert summary["plan_sha256"] == "1" * 64
    assert len(summary["receipt_sha256"]) == 64
    assert summary["verification_sha256"] == "2" * 64
    assert "inventory_sha256" not in summary
    assert summary["rollback_revision"]
    assert str(receipt_path) not in json.dumps(summary)


def test_config_reload_cannot_branch_from_superseded_process_receipt(tmp_path: Path) -> None:
    from paulsha_cortex.runtime_attestation import RuntimeAttestationError

    state_root = tmp_path / "runtime"
    first = record_runtime_startup(
        service="monitor",
        instance="test",
        state_root=state_root,
        configuration={"poll_interval_seconds": 30},
        artifact=_artifact("1" * 64),
        started_at="2026-09-26T00:00:00Z",
        pid=130,
    )
    record_runtime_startup(
        service="monitor",
        instance="test",
        state_root=state_root,
        configuration={"poll_interval_seconds": 45},
        artifact=_artifact("1" * 64),
        started_at="2026-09-26T01:00:00Z",
        pid=131,
    )

    with pytest.raises(RuntimeAttestationError, match="stale"):
        record_config_reload(
            service="monitor",
            instance="test",
            state_root=state_root,
            previous_receipt=first,
            configuration={"poll_interval_seconds": 60},
        )


def test_config_revision_excludes_secret_values_and_runtime_report_is_allowlisted(
    tmp_path: Path,
) -> None:
    assert configuration_revision(
        {"poll_interval": 10, "api_token": "first-secret"}
    ) == configuration_revision(
        {"poll_interval": 10, "api_token": "second-secret"}
    )
    state_root = tmp_path / "runtime"
    record_runtime_startup(
        service="manager",
        instance="test",
        state_root=state_root,
        configuration={"poll_interval": 10, "api_token": "never-persist-this"},
        artifact=_artifact("1" * 64),
        started_at="2026-09-26T00:00:00Z",
        pid=112,
    )
    state = inspect_runtime_state(state_root, service="manager", instance="test")
    serialized = json.dumps(state, sort_keys=True)
    assert "never-persist-this" not in serialized
    assert "api_token" not in serialized
    assert state["latest"]["artifact"]["sha256"] == "1" * 64


def test_future_additive_fields_are_ignored_by_the_current_receipt_reader(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    receipt = record_runtime_startup(
        service="monitor",
        instance="test",
        state_root=root,
        configuration={"poll_interval": 30},
        artifact=_artifact("1" * 64),
        started_at="2026-09-26T00:00:00Z",
        pid=117,
    )
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["future_optional_field"] = {"version": 2}
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    receipt.chmod(0o600)

    state = inspect_runtime_state(root, service="monitor", instance="test")

    assert state["status"] == "attested"
    assert "future_optional_field" not in state["latest"]


def test_service_declaration_projection_hashes_paths_and_unit_contents_without_echoing(
    tmp_path: Path,
) -> None:
    unit_path = tmp_path / "manager.service"
    unit_path.write_text(
        "[Service]\nExecStart=/opt/cortex/bin/python\n"
        "Environment=API_TOKEN=declaration-secret\n",
        encoding="utf-8",
    )
    projected = service_declaration_projection(
        {
            "test-manager.service": {
                "path": str(unit_path),
                "status": "active/running",
                "pid": 321,
                "exec_path": "/opt/cortex/bin/python",
                "stale": False,
            }
        },
        instance="test",
    )

    assert projected["manager"]["status"] == "active/running"
    assert projected["manager"]["pid"] == 321
    assert projected["manager"]["exec_path_sha256"]
    assert projected["manager"]["disk_unit_sha256"]
    assert projected["manager"]["artifact"]["kind"] == "unknown"
    assert "declaration-secret" not in json.dumps(projected)
    assert projected["monitor"]["disk_unit_sha256"] is None


def test_service_dropins_project_pinned_manager_and_monitor_artifacts_against_receipts(
    tmp_path: Path,
) -> None:
    checkout_root = _write_fake_install(tmp_path / "checkout-a", "checkout-a")
    pin_site = tmp_path / "pin-b"
    pin_root = _write_fake_install(pin_site, "pin-b")
    checkout_python = "/opt/checkout-a/bin/python"
    manager_script = checkout_root / "scripts" / "service-manager.sh"
    units = _service_units_with_dropins(
        tmp_path,
        manager_main=f"/usr/bin/env bash {manager_script}",
        monitor_main=f"{checkout_python} -m paulsha_cortex.monitor",
    )
    units["test-monitor.service"]["exec_path"] = checkout_python
    manager_unit = Path(str(units["test-manager.service"]["path"]))
    monitor_unit = Path(str(units["test-monitor.service"]["path"]))
    manager_dropin = manager_unit.with_name(manager_unit.name + ".d")
    monitor_dropin = monitor_unit.with_name(monitor_unit.name + ".d")
    manager_dropin.mkdir()
    monitor_dropin.mkdir()
    manager_command = (
        "/usr/bin/env PYTHONDONTWRITEBYTECODE=1 "
        f"PYTHONPATH={pin_site} /usr/bin/python3 -m "
        "paulsha_cortex.coordinator.manager_daemon --specs-dir /srv/specs "
        "--no-require-idle"
    )
    monitor_command = (
        "/usr/bin/env PYTHONDONTWRITEBYTECODE=1 "
        f"PYTHONPATH={pin_site} /usr/bin/python3 -m paulsha_cortex.monitor"
    )
    for directory, command in (
        (manager_dropin, manager_command),
        (monitor_dropin, monitor_command),
    ):
        (directory / "zz-refine-runtime-pin.conf").write_text(
            "[Service]\nWorkingDirectory=/srv/pin\nEnvironment=DECLARATION_SECRET=hidden\n"
            "ExecStart=\n"
            f"ExecStart={command}\n",
            encoding="utf-8",
        )

    projected = service_declaration_projection(units, instance="test")
    pin_artifact = artifact_identity_from_package_root(pin_root)
    for service, pid in (("manager", 321), ("monitor", 322)):
        declared = projected[service]["artifact"]
        assert declared["sha256"] == pin_artifact["sha256"]
        assert declared["kind"] == "installed-wheel"
        state_root = tmp_path / f"{service}-runtime"
        record_runtime_startup(
            service=service,
            instance="test",
            state_root=state_root,
            configuration={"revision": "same"},
            artifact=pin_artifact,
            started_at="2026-09-26T00:00:00Z",
            pid=pid,
        )
        receipt_state = inspect_runtime_state(
            state_root, service=service, instance="test"
        )
        comparison = compare_runtime_state(
            receipt_state,
            current_artifact=declared,
            declared_config_revision=configuration_revision({"revision": "same"}),
            expected_pid=pid,
            require_process_match=True,
        )
        assert comparison["status"] == "match"

    encoded = json.dumps(projected)
    assert str(pin_site) not in encoded
    assert "DECLARATION_SECRET" not in encoded
    assert projected["monitor"]["exec_path_sha256"] == hashlib.sha256(
        os.fsencode("/usr/bin/env")
    ).hexdigest()


def test_service_dropin_empty_execstart_clears_main_command_and_is_unknown(
    tmp_path: Path,
) -> None:
    checkout_root = _write_fake_install(tmp_path / "checkout-a", "checkout-a")
    units = _service_units_with_dropins(
        tmp_path,
        manager_main=(
            f"/usr/bin/env bash {checkout_root / 'scripts' / 'service-manager.sh'}"
        ),
        monitor_main="/usr/bin/python3 -m paulsha_cortex.monitor",
    )
    manager_unit = Path(str(units["test-manager.service"]["path"]))
    dropin_dir = manager_unit.with_name(manager_unit.name + ".d")
    dropin_dir.mkdir()
    (dropin_dir / "20-clear.conf").write_text(
        "[Service]\nExecStart=\n", encoding="utf-8"
    )

    projected = service_declaration_projection(units, instance="test")

    assert projected["manager"]["artifact"]["kind"] == "unknown"
    assert projected["manager"]["artifact"]["sha256"] is None


def test_service_dropins_apply_in_filename_order_and_change_unit_digest(
    tmp_path: Path,
) -> None:
    first_site = tmp_path / "pin-a"
    second_site = tmp_path / "pin-b"
    _write_fake_install(first_site, "pin-a")
    second_root = _write_fake_install(second_site, "pin-b")
    units = _service_units_with_dropins(
        tmp_path,
        manager_main="/usr/bin/true",
        monitor_main="/usr/bin/true",
    )
    unit_path = Path(str(units["test-manager.service"]["path"]))
    dropin_dir = unit_path.with_name(unit_path.name + ".d")
    dropin_dir.mkdir()
    for name, site in (("10-first.conf", first_site), ("20-second.conf", second_site)):
        (dropin_dir / name).write_text(
            "[Service]\nExecStart=\nExecStart=/usr/bin/env PYTHONPATH="
            f"{site} /usr/bin/python3 -m "
            "paulsha_cortex.coordinator.manager_daemon\n",
            encoding="utf-8",
        )

    before = service_declaration_projection(units, instance="test")["manager"]
    second_artifact = artifact_identity_from_package_root(second_root)
    assert before["artifact"]["sha256"] == second_artifact["sha256"]
    original_digest = before["disk_unit_sha256"]
    (dropin_dir / "20-second.conf").write_text(
        (dropin_dir / "20-second.conf").read_text(encoding="utf-8")
        + "Environment=RUNTIME_PIN_REVISION=two\n",
        encoding="utf-8",
    )
    after = service_declaration_projection(units, instance="test")["manager"]

    assert after["disk_unit_sha256"] != original_digest


def test_service_env_python_module_without_pythonpath_uses_interpreter_prefix(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "venv"
    package_root = _write_fake_install(
        prefix / "lib" / "python3.12" / "site-packages", "venv"
    )
    python = prefix / "bin" / "python3.12"
    units = _service_units_with_dropins(
        tmp_path,
        manager_main="/usr/bin/true",
        monitor_main=(
            f"/usr/bin/env PYTHONDONTWRITEBYTECODE=1 {python} "
            "-m paulsha_cortex.monitor"
        ),
    )

    projected = service_declaration_projection(units, instance="test")

    assert projected["monitor"]["artifact"]["sha256"] == (
        artifact_identity_from_package_root(package_root)["sha256"]
    )


def test_service_declaration_with_multiple_effective_execstarts_is_unknown(
    tmp_path: Path,
) -> None:
    checkout_root = _write_fake_install(tmp_path / "checkout-a", "checkout-a")
    units = _service_units_with_dropins(
        tmp_path,
        manager_main=(
            f"/usr/bin/env bash {checkout_root / 'scripts' / 'service-manager.sh'}"
        ),
        monitor_main="/usr/bin/true",
    )
    unit_path = Path(str(units["test-manager.service"]["path"]))
    dropin_dir = unit_path.with_name(unit_path.name + ".d")
    dropin_dir.mkdir()
    (dropin_dir / "30-second-command.conf").write_text(
        "[Service]\nExecStart=/usr/bin/false\n", encoding="utf-8"
    )

    projected = service_declaration_projection(units, instance="test")["manager"]

    assert projected["artifact"]["kind"] == "unknown"
    assert projected["artifact"]["sha256"] is None
    assert projected["exec_path_sha256"] is None


def test_service_declaration_with_unparseable_execstart_is_unknown(
    tmp_path: Path,
) -> None:
    checkout_root = _write_fake_install(tmp_path / "checkout-a", "checkout-a")
    units = _service_units_with_dropins(
        tmp_path,
        manager_main=(
            f"/usr/bin/env bash {checkout_root / 'scripts' / 'service-manager.sh'}"
        ),
        monitor_main="/usr/bin/true",
    )
    unit_path = Path(str(units["test-manager.service"]["path"]))
    dropin_dir = unit_path.with_name(unit_path.name + ".d")
    dropin_dir.mkdir()
    (dropin_dir / "30-invalid-command.conf").write_text(
        "[Service]\nExecStart /usr/bin/false\n", encoding="utf-8"
    )

    projected = service_declaration_projection(units, instance="test")["manager"]

    assert projected["artifact"]["kind"] == "unknown"
    assert projected["artifact"]["sha256"] is None


@pytest.mark.parametrize(
    "unsafe_file", ["symlink", "symlink-directory", "oversized"]
)
def test_service_dropin_read_failure_fails_closed(
    tmp_path: Path, unsafe_file: str
) -> None:
    checkout_root = _write_fake_install(tmp_path / "checkout-a", "checkout-a")
    units = _service_units_with_dropins(
        tmp_path,
        manager_main=(
            f"/usr/bin/env bash {checkout_root / 'scripts' / 'service-manager.sh'}"
        ),
        monitor_main="/usr/bin/python3 -m paulsha_cortex.monitor",
    )
    manager_unit = Path(str(units["test-manager.service"]["path"]))
    dropin_dir = manager_unit.with_name(manager_unit.name + ".d")
    if unsafe_file == "symlink-directory":
        target_dir = tmp_path / "dropins-target"
        target_dir.mkdir()
        dropin_dir.symlink_to(target_dir, target_is_directory=True)
    else:
        dropin_dir.mkdir()
    dropin_path = dropin_dir / "50-unsafe.conf"
    if unsafe_file == "symlink":
        target = tmp_path / "outside.conf"
        target.write_text("[Service]\nExecStart=/usr/bin/false\n", encoding="utf-8")
        dropin_path.symlink_to(target)
    elif unsafe_file == "oversized":
        dropin_path.write_bytes(b"[Service]\n" + b"X" * (300 * 1024))

    projected = service_declaration_projection(units, instance="test")["manager"]

    assert projected["artifact"]["kind"] == "unknown"
    assert projected["artifact"]["sha256"] is None
    assert projected["disk_unit_sha256"] is None


def test_service_status_requires_loaded_receipt_pid_to_match_unit_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paulsha_cortex.porcelain import service as service_porcelain

    coordinator_root = tmp_path / "coordinator"
    monitor_root = tmp_path / "monitor"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(coordinator_root))
    monkeypatch.setenv("PSC_MONITOR_STATE_ROOT", str(monitor_root))
    environment = service_porcelain._fallback_environment("test")
    configuration, components = manager_configuration_snapshot({}, environment)
    record_runtime_startup(
        service="manager",
        instance="test",
        state_root=coordinator_root,
        configuration=configuration,
        config_components=components,
        artifact=artifact_identity(),
        started_at="2026-09-26T00:00:00Z",
        pid=321,
    )
    unit_path = tmp_path / "test-manager.service"
    unit_path.write_text("[Service]\nExecStart=/usr/bin/python\n", encoding="utf-8")
    units = {
        "test-manager.service": {
            "path": str(unit_path),
            "status": "active/running",
            "pid": 321,
            "exec_path": "/usr/bin/python",
            "stale": False,
        },
        "test-monitor.service": {
            "path": str(tmp_path / "test-monitor.service"),
            "status": "inactive/dead",
            "pid": None,
            "exec_path": None,
            "stale": False,
        },
    }
    monkeypatch.setattr(
        service_porcelain,
        "probe_service_runtime",
        lambda _instance: {"mode": "systemd", "version": "0.1.10", "units": units},
    )

    report = service_porcelain._status_payload("test")["loaded_runtime"]["manager"]

    assert report["loaded"]["pid"] == 321
    assert report["comparison"]["process_status"] == "match"
    assert report["status"] == "unknown"
    assert report["reason"] == "invocation-declaration-unknown"


def test_runtime_state_compares_declared_revision_and_never_calls_inflight_safe(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "runtime"
    record_runtime_startup(
        service="manager",
        instance="test",
        state_root=state_root,
        configuration={"tick_interval": 300},
        artifact=_artifact("1" * 64),
        started_at="2026-09-26T00:00:00Z",
        pid=113,
        trust_root={"status": "verified", "in_flight_jobs": 2},
    )
    state = inspect_runtime_state(state_root, service="manager", instance="test")
    comparison = compare_runtime_state(
        state,
        current_artifact=_artifact("1" * 64),
        declared_config_revision=configuration_revision({"tick_interval": 300}),
        in_flight_jobs=state["latest"]["trust_root"]["in_flight_jobs"],
    )
    assert comparison["status"] == "match"
    assert comparison["transition_disposition"] == "blocked-in-flight"
    assert comparison["transition_safe"] is False
    without_live_pid = compare_runtime_state(
        state,
        current_artifact=_artifact("1" * 64),
        declared_config_revision=configuration_revision({"tick_interval": 300}),
        expected_pid=None,
        require_process_match=True,
        in_flight_jobs=2,
    )
    assert without_live_pid["status"] == "unknown"
    assert without_live_pid["process_status"] == "unknown"


def test_runtime_receipt_is_bound_to_its_instance_root(tmp_path: Path) -> None:
    source_root = tmp_path / "source-runtime"
    receipt = record_runtime_startup(
        service="manager",
        instance="test",
        state_root=source_root,
        configuration={"tick_interval": 300},
        artifact=_artifact("1" * 64),
        started_at="2026-09-26T00:00:00Z",
        pid=114,
    )
    destination_root = tmp_path / "copied-runtime"
    shutil.copytree(source_root / "runtime-attestations", destination_root / "runtime-attestations")
    (destination_root / "runtime-attestations").chmod(0o700)

    result = inspect_runtime_state(
        destination_root, service="manager", instance="test"
    )

    assert receipt.exists()
    assert result["status"] == "unknown"
    assert result["reason"] == "instance-root-mismatch"


def test_isolated_installed_cli_reports_installed_artifact_from_outside_checkout(
    tmp_path: Path,
) -> None:
    import yaml

    project_root = Path(__file__).resolve().parents[1]
    prefix = tmp_path / "isolated-venv"
    home = tmp_path / "home"
    home.mkdir()
    venv = subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(prefix)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert venv.returncode == 0, venv.stderr
    venv_python = prefix / "bin" / "python"
    # Python 3.12 起 venv（含 --system-site-packages 的基底）不再內建 setuptools；
    # 新 venv 看得到 build backend 才離線 --no-build-isolation，否則交給 pip 的
    # build isolation 取得 backend（CI 有網路）。兩條路都是真的從 checkout 外安裝。
    backend_probe = subprocess.run(
        [str(venv_python), "-c", "import setuptools.build_meta"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    isolation_args = ["--no-build-isolation"] if backend_probe.returncode == 0 else []
    install = subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            *isolation_args,
            str(project_root),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PIP_CACHE_DIR": str(tmp_path / "pip-cache"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert install.returncode == 0, install.stderr

    dependency_root = tmp_path / "dependencies"
    shutil.copytree(
        Path(yaml.__file__).resolve().parent,
        dependency_root / "yaml",
    )
    environment = {
        **os.environ,
        "HOME": str(home),
        "PYTHONPATH": str(dependency_root),
        "PSC_COORDINATOR_ROOT": str(tmp_path / "coordinator"),
        "PSC_MONITOR_STATE_ROOT": str(tmp_path / "monitor"),
    }
    cli = prefix / "bin" / "cortex"
    help_result = subprocess.run(
        [str(cli), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert help_result.returncode == 0
    assert "service" in help_result.stdout

    status_result = subprocess.run(
        [str(cli), "service", "status", "--json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert status_result.returncode == 0, status_result.stderr
    payload = json.loads(status_result.stdout)
    runtime = payload["service"]["loaded_runtime"]
    assert payload["service"]["instance"] == "cortex"
    assert runtime["operator_cli"]["artifact"]["kind"] == "installed-wheel"
    assert runtime["operator_cli"]["instance"] == "cortex"
    assert runtime["operator_cli"]["environment_revision"]
    assert runtime["manager"]["installed_artifact"]["kind"] == "unknown"
    assert runtime["manager"]["status"] == "unknown"

    package_root = next(prefix.glob("lib/python*/site-packages/paulsha_cortex"))
    manager_script = package_root / "scripts" / "service-manager.sh"
    manager_unit = tmp_path / "cortex-manager.service"
    manager_unit.write_text(
        f"[Service]\nExecStart=/usr/bin/env bash {manager_script}\n",
        encoding="utf-8",
    )
    monitor_unit = tmp_path / "cortex-monitor.service"
    monitor_unit.write_text(
        f"[Service]\nExecStart={cli.parent / 'python'} -m paulsha_cortex.monitor\n",
        encoding="utf-8",
    )
    declarations = service_declaration_projection(
        {
            "cortex-manager.service": {
                "path": str(manager_unit),
                "status": "active/running",
                "pid": 111,
                "exec_path": "/usr/bin/env",
                "stale": False,
            },
            "cortex-monitor.service": {
                "path": str(monitor_unit),
                "status": "active/running",
                "pid": 112,
                "exec_path": str(cli.parent / "python"),
                "stale": False,
            },
        },
        instance="cortex",
    )
    for service_name in ("manager", "monitor"):
        assert declarations[service_name]["artifact"]["kind"] == "installed-wheel"
        assert declarations[service_name]["artifact"]["sha256"] == runtime[
            "operator_cli"
        ]["artifact"]["sha256"]


def test_manager_and_monitor_startup_write_separate_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paulsha_cortex.coordinator import manager_daemon
    from paulsha_cortex.monitor import __main__ as monitor_main

    coordinator_root = tmp_path / "coordinator"
    monitor_root = tmp_path / "monitor"
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(coordinator_root))
    monkeypatch.setenv("PSC_MONITOR_STATE_ROOT", str(monitor_root))
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path / "control"))
    monkeypatch.setenv("PSC_RUN_ROOT", str(tmp_path / "run"))
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("PSC_INSTANCE", "test")

    config_path = tmp_path / "config" / "project-cortex.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "workspaces:\n"
        "  - name: test\n"
        f"    path: {tmp_path}\n"
        "monitor:\n  poll_interval_seconds: 30\n",
        encoding="utf-8",
    )
    called: dict[str, object] = {}

    def fake_run_loop(**kwargs):
        callback = kwargs.get("on_started")
        assert callable(callback)
        callback()
        return True

    monkeypatch.setattr(manager_daemon, "run_loop", fake_run_loop)
    assert manager_daemon.main(["--max-rounds", "0"]) == 0

    class StopAfterStartup:
        def __init__(self, *, config):
            called["config"] = config

        def run_forever(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(monitor_main, "ProjectMonitorService", StopAfterStartup)
    assert monitor_main.main(["--config", str(config_path)]) == 0

    manager_state = inspect_runtime_state(
        coordinator_root, service="manager", instance="test"
    )
    monitor_state = inspect_runtime_state(monitor_root, service="monitor", instance="test")
    assert manager_state["status"] == "attested"
    assert monitor_state["status"] == "attested"
    assert manager_state["latest"]["artifact"]["sha256"]
    assert monitor_state["latest"]["config"]["effective_revision"]


def test_doctor_exposes_the_same_safe_runtime_identity_projection(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.doctor import run_doctor

    home = tmp_path / "home"
    agents = home / ".agents"
    config_root = agents / "config" / "paulsha"
    config_root.mkdir(parents=True)
    (config_root / "project-cortex.yaml").write_text(
        "workspaces:\n"
        "  - name: test\n"
        f"    path: {tmp_path}\n",
        encoding="utf-8",
    )
    environment = {
        "HOME": str(home),
        "PSC_AGENTS_ROOT": str(agents),
        "PSC_COORDINATOR_ROOT": str(agents / "coordinator"),
        "PSC_MONITOR_STATE_ROOT": str(agents / "monitor"),
        "PSC_PROJECT_CONFIG_ROOT": str(config_root),
        "PSC_RUN_ROOT": str(agents / "run" / "test"),
        "PSC_MANAGER_API_TOKEN": "doctor-secret-must-not-appear",
    }

    report = run_doctor(
        probe_live=False,
        instance="test",
        env=environment,
        home=home,
    )
    probe = next(row for row in report.probes if row.name == "loaded-runtime")
    serialized = json.dumps(probe.to_dict(), sort_keys=True)

    assert probe.status == "warn"
    assert probe.context["manager"]["status"] == "unknown"
    assert probe.context["monitor"]["status"] == "unknown"
    assert probe.context["operator_cli"]["artifact"]["sha256"]
    assert probe.context["service_declaration"]["manager"]["pid"] is None
    assert "doctor-secret-must-not-appear" not in serialized
