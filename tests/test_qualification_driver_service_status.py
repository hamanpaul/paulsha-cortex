from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _driver():
    path = Path(__file__).parents[1] / "qualification" / "driver.py"
    spec = importlib.util.spec_from_file_location("qualification_driver_status_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _status_payload(*, manager_artifact: str = "match", monitor_trust: str = "verified"):
    reports = {}
    wheel_sha256 = "b" * 64
    candidate_commit = "c" * 40
    for name in ("manager", "monitor"):
        reports[name] = {
            "comparison": {
                "artifact_status": manager_artifact if name == "manager" else "match",
                "config_status": "match",
                "process_status": "match",
                "loaded_wheel_sha256": wheel_sha256,
            },
            "trust_root": {
                "status": monitor_trust if name == "monitor" else "verified",
                "wheel_sha256": wheel_sha256,
                "candidate_commit": candidate_commit,
            },
            "installed_artifact": {"wheel_sha256": wheel_sha256},
        }
    return {"service": {"loaded_runtime": reports}}


@pytest.mark.parametrize(
    ("manager_artifact", "monitor_trust"),
    [("drift", "verified"), ("match", "unknown")],
)
def test_installed_checks_fail_closed_on_system_runtime_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager_artifact: str,
    monitor_trust: str,
) -> None:
    driver = _driver()
    install_evidence = tmp_path / "install-evidence.json"
    install_evidence.write_text(
        json.dumps({"result": "pass", "attestation": {"ok": True}}),
        encoding="utf-8",
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    commands: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        commands.append(tuple(argv))
        if "service" in argv:
            stdout = json.dumps(
                _status_payload(
                    manager_artifact=manager_artifact,
                    monitor_trust=monitor_trust,
                )
            )
        elif "selfcheck" in argv:
            stdout = json.dumps({"ok": True, "job_writable_count": 0})
        else:
            stdout = json.dumps({"ok": True})
        return driver.CommandResult(tuple(argv), 0, stdout, "")

    monkeypatch.setattr(driver, "_run", run)
    monkeypatch.setattr(driver, "_installed_runtime_env", dict)
    monkeypatch.setattr(driver, "SYSTEM_STATUS_SETTLE_SECONDS", 0)

    expected = (
        "artifact_status=drift config_status=match process_status=match"
        if manager_artifact == "drift"
        else "Trust Root receipt is not verified"
    )
    with pytest.raises(driver.QualificationFailure, match=expected):
        driver._installed_checks(
            install_evidence=install_evidence,
            receipt={},
            evidence_dir=evidence_dir,
        )

    assert any("--system" in command and "status" in command for command in commands)


def test_installed_checks_capture_matching_system_runtime_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _driver()
    install_evidence = tmp_path / "install-evidence.json"
    install_evidence.write_text(
        json.dumps({"result": "pass", "attestation": {"ok": True}}),
        encoding="utf-8",
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    def run(argv, **_kwargs):
        if "service" in argv:
            stdout = json.dumps(_status_payload())
        elif "selfcheck" in argv:
            stdout = json.dumps({"ok": True, "job_writable_count": 0})
        else:
            stdout = json.dumps({"ok": True})
        return driver.CommandResult(tuple(argv), 0, stdout, "")

    monkeypatch.setattr(driver, "_run", run)
    monkeypatch.setattr(driver, "_installed_runtime_env", dict)

    checks = driver._installed_checks(
        install_evidence=install_evidence,
        receipt={},
        evidence_dir=evidence_dir,
    )

    assert {row["name"] for row in checks} >= {"system-loaded-runtime-attestation"}
    captured = json.loads(
        (evidence_dir / "system-loaded-runtime-status.json").read_text(encoding="utf-8")
    )
    assert captured["service"]["loaded_runtime"]["manager"]["comparison"]["process_status"] == "match"


def test_installed_checks_wait_for_the_loaded_receipt_after_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服務在 activate 時才啟動：第一次查詢 loaded receipt 可能尚未寫入
    （process 為 unknown），輪詢到 match 後才判定。"""

    driver = _driver()
    install_evidence = tmp_path / "install-evidence.json"
    install_evidence.write_text(
        json.dumps({"result": "pass", "attestation": {"ok": True}}),
        encoding="utf-8",
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    status_calls: list[int] = []

    def run(argv, **_kwargs):
        if "service" in argv:
            status_calls.append(1)
            payload = _status_payload()
            if len(status_calls) == 1:
                payload["service"]["loaded_runtime"]["manager"]["comparison"][
                    "process_status"
                ] = "unknown"
            stdout = json.dumps(payload)
        elif "selfcheck" in argv:
            stdout = json.dumps({"ok": True, "job_writable_count": 0})
        else:
            stdout = json.dumps({"ok": True})
        return driver.CommandResult(tuple(argv), 0, stdout, "")

    monkeypatch.setattr(driver, "_run", run)
    monkeypatch.setattr(driver, "_installed_runtime_env", dict)
    monkeypatch.setattr(driver.time, "sleep", lambda _seconds: None)

    tests = driver._installed_checks(
        install_evidence=install_evidence,
        receipt={},
        evidence_dir=evidence_dir,
    )

    assert len(status_calls) == 2
    assert {"name": "system-loaded-runtime-attestation", "status": "passed"} in tests


def test_installed_checks_pass_the_effective_install_receipt_to_system_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _driver()
    install_evidence = tmp_path / "install-evidence.json"
    install_evidence.write_text(
        json.dumps({"result": "pass", "attestation": {"ok": True}}),
        encoding="utf-8",
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    status_commands: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        if "service" in argv:
            status_commands.append(tuple(argv))
            stdout = json.dumps(_status_payload())
        elif "selfcheck" in argv:
            stdout = json.dumps({"ok": True, "job_writable_count": 0})
        else:
            stdout = json.dumps({"ok": True})
        return driver.CommandResult(tuple(argv), 0, stdout, "")

    monkeypatch.setattr(driver, "_run", run)
    monkeypatch.setattr(driver, "_installed_runtime_env", dict)
    receipt_path = Path("/run/cortex-install/install-receipt.json")

    driver._installed_checks(
        install_evidence=install_evidence,
        receipt={},
        evidence_dir=evidence_dir,
        receipt_path=receipt_path,
    )

    assert status_commands[0][-2:] == ("--install-receipt", str(receipt_path))
