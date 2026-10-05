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


def _rollback_status_payload(*, manager_wheel: str = "b" * 64, monitor_receipt: str = "prior"):
    reports = {}
    for name in ("manager", "monitor"):
        reports[name] = {
            "comparison": {
                "artifact_status": "match",
                "config_status": "match",
                "process_status": "match",
                "loaded_wheel_sha256": manager_wheel if name == "manager" else "b" * 64,
            },
            "trust_root": {
                "status": "verified",
                "receipt_id": "prior" if name == "manager" else monitor_receipt,
                "wheel_sha256": "b" * 64,
                "candidate_commit": "c" * 40,
            },
            "installed_artifact": {"wheel_sha256": "b" * 64},
        }
    return {"service": {"loaded_runtime": reports}}


def test_rollback_loaded_runtime_parser_accepts_prior_artifact_and_receipt() -> None:
    driver = _driver()

    assert driver._rollback_loaded_runtime_mismatch(
        _rollback_status_payload(),
        {"receipt_id": "prior", "wheel_sha256": "b" * 64, "candidate_commit": "c" * 40},
    ) == ""


def test_rollback_loaded_runtime_parser_rejects_artifact_or_receipt_mismatch() -> None:
    driver = _driver()

    mismatch = driver._rollback_loaded_runtime_mismatch(
        _rollback_status_payload(manager_wheel="d" * 64, monitor_receipt="other"),
        {"receipt_id": "prior", "wheel_sha256": "b" * 64, "candidate_commit": "c" * 40},
    )

    assert "wheel=mismatch" in mismatch
    assert "receipt=mismatch" in mismatch


def test_rollback_loaded_runtime_parser_rejects_unknown_receipt_evidence() -> None:
    driver = _driver()
    payload = _rollback_status_payload()
    del payload["service"]["loaded_runtime"]["manager"]["trust_root"]

    mismatch = driver._rollback_loaded_runtime_mismatch(
        payload,
        {"receipt_id": "prior", "wheel_sha256": "b" * 64, "candidate_commit": "c" * 40},
    )

    assert "manager=unknown" in mismatch


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
    # 本檔只驗 system-scope status；#1167 的 root-only reclaim check 另有測試。
    monkeypatch.setattr(driver, "_installed_owner_bound_reclaim", lambda _receipt, _evidence_dir: None)
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
    # 本檔只驗 system-scope status；#1167 的 root-only reclaim check 另有測試。
    monkeypatch.setattr(driver, "_installed_owner_bound_reclaim", lambda _receipt, _evidence_dir: None)

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
    # 本檔只驗 system-scope status；#1167 的 root-only reclaim check 另有測試。
    monkeypatch.setattr(driver, "_installed_owner_bound_reclaim", lambda _receipt, _evidence_dir: None)
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
    # 本檔只驗 system-scope status；#1167 的 root-only reclaim check 另有測試。
    monkeypatch.setattr(driver, "_installed_owner_bound_reclaim", lambda _receipt, _evidence_dir: None)
    receipt_path = Path("/run/cortex-install/install-receipt.json")

    driver._installed_checks(
        install_evidence=install_evidence,
        receipt={},
        evidence_dir=evidence_dir,
        receipt_path=receipt_path,
    )

    assert status_commands[0][-2:] == ("--install-receipt", str(receipt_path))


def _upgrade_inputs(
    tmp_path: Path, *, drill_result: str = "rolled-back", inherited_from: str = "prior"
):
    drill_receipt = tmp_path / "drill-receipt.json"
    parent = {
        "path": "/run/cortex-install/install-receipt.json",
        "receipt_id": "prior",
        "plan_sha256": "e" * 64,
    }
    drill_receipt.write_text(
        json.dumps({"receipt_id": "drill", "state": "rolled-back", "parent_receipt": parent}),
        encoding="utf-8",
    )
    plan = {"candidate": {"wheel_sha256": "b" * 64}, "repo_identity": {"commit": "c" * 40}}
    prior = {"receipt_id": "prior", "plan": plan}
    upgraded = {
        "receipt_id": "upgraded",
        "state": "applied",
        "qualified": True,
        "parent_receipt": parent,
        "plan": plan,
        "credentials": [
            {
                "principal": "builder",
                "provider": "codex",
                "mode": "0600",
                "sha256": "f" * 64,
                "inherited_from": inherited_from,
            }
        ],
    }
    drill = {
        "result": drill_result,
        "failed_step": "credentials",
        "receipt": {"path": str(drill_receipt), "receipt_id": "drill"},
        "rollback": {
            "restore_safe": True,
            "prior_loaded_runtime": {"mismatch": "", "service_status": _rollback_status_payload()},
        },
    }
    report = {
        "result": "upgraded",
        "receipt": {"path": "/var/lib/cortex-install-receipts/next.json", "receipt_id": "upgraded"},
        "plan": {"sha256": "d" * 64},
    }
    return prior, upgraded, drill, report


def _upgraded_status_payload():
    payload = _rollback_status_payload()
    for report in payload["service"]["loaded_runtime"].values():
        report["trust_root"]["receipt_id"] = "upgraded"
    return payload


def _capture(driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **changes):
    prior, upgraded, drill, report = _upgrade_inputs(tmp_path, **changes)
    commands: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        commands.append(tuple(argv))
        return driver.CommandResult(tuple(argv), 0, json.dumps(_upgraded_status_payload()), "")

    monkeypatch.setattr(driver, "_run", run)
    monkeypatch.setattr(driver, "_installed_runtime_env", dict)
    monkeypatch.setattr(driver, "SYSTEM_STATUS_SETTLE_SECONDS", 0)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    driver._capture_one_command_upgrade(
        prior_receipt=prior,
        upgrade_receipt=upgraded,
        receipt_path=Path(report["receipt"]["path"]),
        drill_report=drill,
        upgrade_report=report,
        evidence_dir=evidence,
    )
    return evidence, commands


def test_one_command_upgrade_evidence_binds_the_drill_and_the_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver()

    evidence, commands = _capture(driver, tmp_path, monkeypatch)

    rollback = json.loads((evidence / "rollback-loaded-runtime-status.json").read_text())
    assert rollback["scenario"] == "same-artifact-qualified-prior-to-candidate-rollback"
    assert rollback["rollback_receipt"] == {
        "receipt_id": "drill",
        "state": "rolled-back",
        "parent_receipt_id": "prior",
    }
    upgrade = json.loads((evidence / "one-command-upgrade.json").read_text())
    assert upgrade["scenario"] == "one-command-upgrade-same-artifact"
    assert upgrade["upgrade"]["inherited_credentials"] == [
        {"principal": "builder", "provider": "codex", "inherited_from": "prior"}
    ]
    assert upgrade["expected"]["receipt_id"] == "upgraded"
    assert commands[-1][-2:] == ("--install-receipt", "/var/lib/cortex-install-receipts/next.json")


def test_one_command_upgrade_evidence_refuses_a_drill_that_did_not_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver()

    with pytest.raises(driver.QualificationFailure, match="did not roll back"):
        _capture(driver, tmp_path, monkeypatch, drill_result="halted")


def test_one_command_upgrade_evidence_refuses_credentials_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver()

    with pytest.raises(driver.QualificationFailure, match="inherited the prior credentials"):
        _capture(driver, tmp_path, monkeypatch, inherited_from="another")
