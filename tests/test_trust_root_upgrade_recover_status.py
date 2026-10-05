"""#1263：`--recover` 只讀 snapshot 推出 durable plan；`--status` 唯讀。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import upgrade_fixtures as fx
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import upgrade
from paulsha_cortex.trust_root.install.core import InstallError


@pytest.fixture
def recovering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(upgrade, "_OWNER_UID", os.getuid())
    monkeypatch.setattr(upgrade, "_pin_import_paths", lambda: None)
    monkeypatch.setattr(
        upgrade,
        "ingest_release",
        lambda *_args, **_kwargs: pytest.fail("recover must not fetch a release"),
    )
    (tmp_path / "installer").mkdir()
    captured: list[object] = []

    def recover_command(args) -> int:
        captured.append(args)
        return 0

    monkeypatch.setattr(install_cli, "_recover_command", recover_command)
    return captured


def _publish(tmp_path: Path, payload: bytes = b'{"plan":"x"}\n') -> str:
    sha = hashlib.sha256(payload).hexdigest()
    upgrade.publish_durable_plan(payload, sha, plans_root=tmp_path / "installer" / "plans")
    return sha


def _snapshot(tmp_path: Path, sha: str) -> None:
    install_cli._write_maintenance_snapshot(
        {
            "schema_version": 1,
            "plan_sha256": sha,
            "receipt_path": str(tmp_path / "receipts" / "next.run-1.json"),
            "present_services": ["cortex-manager.service"],
            "previously_active": ["cortex-manager.service"],
        }
    )


def test_recover_derives_the_durable_plan_from_the_snapshot_only(
    tmp_path: Path, recovering
) -> None:
    sha = _publish(tmp_path)
    _snapshot(tmp_path, sha)

    assert upgrade.recover_upgrade() == 0

    assert recovering[0].plan == str(tmp_path / "installer" / "plans" / f"{sha}.json")
    assert recovering[0].confirm_sha256 == sha


def test_recover_falls_back_to_a_stale_lease_marker(tmp_path: Path, recovering) -> None:
    sha = _publish(tmp_path)
    with install_cli._host_lock(leaf="maintenance.lock", conflict="unexpected") as lock_fd:
        install_cli._write_lock_payload(lock_fd, {"plan_sha256": sha, "token_sha256": "b" * 64})

    assert upgrade.recover_upgrade() == 0
    assert recovering[0].confirm_sha256 == sha


def test_recover_with_nothing_interrupted_does_nothing(
    tmp_path: Path, recovering, capsys: pytest.CaptureFixture[str]
) -> None:
    assert upgrade.recover_upgrade() == 0

    assert recovering == []
    assert json.loads(capsys.readouterr().out) == {
        "maintenance_recovered": False,
        "reason": "nothing-to-recover",
    }


def test_recover_refuses_a_tampered_durable_plan(tmp_path: Path, recovering) -> None:
    sha = _publish(tmp_path)
    _snapshot(tmp_path, sha)
    (tmp_path / "installer" / "plans" / f"{sha}.json").write_bytes(b'{"plan":"y"}\n')

    with pytest.raises(upgrade.UpgradeError, match="durable reviewed plan digest mismatch"):
        upgrade.recover_upgrade()
    assert recovering == []


def test_recover_refuses_while_the_maintenance_window_is_held(
    tmp_path: Path, recovering
) -> None:
    plan = fx.plan_document(
        tmp_path, version="0.1.13", wheel_sha256=fx.NEW_WHEEL, commit=fx.NEW_COMMIT
    )

    with install_cli._maintenance_lease(plan):
        with pytest.raises(upgrade.UpgradeError, match="still held by a live process"):
            upgrade.recover_upgrade()
    assert recovering == []


def test_recover_marks_the_last_report_recovered(tmp_path: Path, recovering) -> None:
    sha = _publish(tmp_path)
    _snapshot(tmp_path, sha)
    upgrade._publish_report({"version": "0.1.13", "result": "in-progress", "plan": {"sha256": sha}})

    assert upgrade.recover_upgrade() == 0

    report = json.loads((tmp_path / "installer" / "last-upgrade-report.json").read_text())
    assert report["result"] == "recovered"


def test_status_is_read_only_and_reports_receipt_runtime_and_last_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    (tmp_path / "installer").mkdir()
    upgrade._publish_report(
        {
            "version": "0.1.14",
            "result": "rolled-back",
            "failed_step": "credentials",
            "error": "new plan requires credentials the prior receipt never recorded",
            "finished_at": "2026-10-05T00:00:00Z",
            "report_path": "/var/lib/cortex-installer/0.1.14/upgrade-report.json",
        }
    )
    prior = fx.make_prior(tmp_path)
    monkeypatch.setattr(upgrade, "effective_receipt", lambda _state_root: prior.receipt)
    monkeypatch.setattr(
        upgrade, "await_loaded_runtime", lambda plan, path, expected, **_kwargs: ("", {})
    )
    before = sorted(str(path) for path in tmp_path.rglob("*"))

    status = upgrade.upgrade_status()

    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    assert status["effective_receipt"] == {
        "path": str(prior.path),
        "receipt_id": "prior-receipt",
        "version": "0.1.12",
    }
    assert status["loaded_runtime"] == "match"
    assert status["last_upgrade"]["result"] == "rolled-back"
    assert status["maintenance_pending"] is False
    assert upgrade.run_upgrade(upgrade.UpgradeOptions(status=True, json_output=True)) == 0
    assert json.loads(capsys.readouterr().out)["effective_receipt"]["version"] == "0.1.12"


def test_status_is_non_zero_when_the_receipt_cannot_be_decided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")

    def undecided(_state_root):
        raise InstallError(
            "cannot decide the effective receipt: 2 applied and qualified receipts "
            "have no qualified successor (a.json, b.json)"
        )

    monkeypatch.setattr(upgrade, "effective_receipt", undecided)

    assert upgrade.run_upgrade(upgrade.UpgradeOptions(status=True)) == 1
    assert "cannot decide the effective receipt" in capsys.readouterr().out


def test_status_reports_a_stale_maintenance_marker_as_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Final review item 9: a crashed lease leaves only the marker (no snapshot
    # yet); the next upgrade demands --recover, so --status must not say idle.
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    (tmp_path / "installer").mkdir()
    with install_cli._host_lock(leaf="maintenance.lock", conflict="unexpected") as lock_fd:
        install_cli._write_lock_payload(lock_fd, {"plan_sha256": "a" * 64, "token_sha256": "b" * 64})
    prior = fx.make_prior(tmp_path)
    monkeypatch.setattr(upgrade, "effective_receipt", lambda _state_root: prior.receipt)
    monkeypatch.setattr(
        upgrade, "await_loaded_runtime", lambda plan, path, expected, **_kwargs: ("", {})
    )
    before = sorted(str(path) for path in tmp_path.rglob("*"))

    status = upgrade.upgrade_status()

    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    assert status["maintenance_pending"] is True
    assert upgrade.run_upgrade(upgrade.UpgradeOptions(status=True)) == 1
    assert "cortex upgrade --recover" in capsys.readouterr().out

    # The empty marker every completed lease leaves behind is idle.
    with install_cli._host_lock(leaf="maintenance.lock", conflict="unexpected") as lock_fd:
        install_cli._write_lock_payload(lock_fd, None)
    assert upgrade.upgrade_status()["maintenance_pending"] is False
