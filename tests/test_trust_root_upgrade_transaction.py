"""#1263：transaction 的步驟順序、sha／token 綁定，以及每一種失敗分支。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import upgrade_fixtures as fx
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import upgrade
from paulsha_cortex.trust_root.install.release_ingress import IngressError


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(upgrade, "_STATUS_SETTLE_SECONDS", 0)
    monkeypatch.setattr(upgrade, "installed_runtime_env", lambda *_args, **_kwargs: {})
    systemd = fx.FakeSystemd()
    monkeypatch.setattr(install_cli, "_systemctl", systemd)
    cli = fx.FakeCandidateCli(systemd)
    monkeypatch.setattr(upgrade, "_run", cli)
    sealed = fx.make_sealed(tmp_path)
    bound = fx.make_bound(tmp_path)
    prior = fx.make_prior(tmp_path)
    cli.loaded[str(bound.receipt_path)] = ("new-receipt", fx.NEW_WHEEL, fx.NEW_COMMIT)
    cli.loaded[str(prior.path)] = ("prior-receipt", fx.PRIOR_WHEEL, fx.PRIOR_COMMIT)
    return SimpleNamespace(systemd=systemd, cli=cli, sealed=sealed, bound=bound, prior=prior)


def _run_transaction(harness: SimpleNamespace) -> tuple[int, dict[str, object]]:
    report: dict[str, object] = {"version": "0.1.13", "services_stopped": False}
    code = upgrade.run_transaction(
        harness.sealed, harness.bound, harness.prior, report, upgrade.StepLog()
    )
    return code, report


def _marker() -> object:
    with install_cli._host_lock_file(leaf="maintenance.lock") as lock_fd:
        return install_cli._maintenance_lock_payload(lock_fd, allow_absent=True)


def test_upgrade_runs_every_candidate_step_with_the_bound_sha_and_lease_token(harness) -> None:
    code, report = _run_transaction(harness)

    assert code == 0
    assert report["result"] == "upgraded"
    assert [c for c in harness.cli.commands() if c != "status"] == [
        "apply",
        "credentials inherit",
        "activate",
        "verify",
    ]
    apply = harness.cli.calls[0]
    assert apply[apply.index("--plan") + 1] == str(harness.bound.durable_path)
    assert apply[apply.index("--confirm-sha256") + 1] == harness.bound.sha256
    assert apply[apply.index("--receipt") + 1] == str(harness.bound.receipt_path)
    assert apply[apply.index("--prior-receipt") + 1] == str(harness.prior.path)
    tokens = {
        call[call.index("--maintenance-token") + 1]
        for call in harness.cli.calls
        if "--maintenance-token" in call
    }
    assert len(tokens) == 1
    assert re.fullmatch(r"[0-9a-f]{64}", tokens.pop())
    events = harness.systemd.calls
    last_stop = max(index for index, call in enumerate(events) if call[0] == "stop")
    assert last_stop < events.index(("candidate", "apply"))
    assert ("status", str(harness.bound.receipt_path)) in harness.cli.calls
    assert report["inherited_credentials"] == [
        {"principal": "builder", "provider": "codex", "inherited_from": "prior-receipt"}
    ]
    assert report["receipt"] == {
        "path": str(harness.bound.receipt_path),
        "receipt_id": "new-receipt",
    }
    assert install_cli._read_maintenance_snapshot() is None
    assert _marker() is None


def test_credential_handoff_failure_rolls_back_and_restores_services(harness) -> None:
    harness.cli.fail["credentials inherit"] = (
        "new plan requires credentials the prior receipt never recorded: "
        "reviewer-planner/copilot; roll back and import them per "
        "trust-root-transactional-install.md §4"
    )

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["result"] == "rolled-back"
    assert report["failed_step"] == "credentials"
    assert report["phase"] == "pre-activate"
    assert "§4" in report["error"]
    assert "rollback" in harness.cli.commands()
    assert "activate" not in harness.cli.commands()
    assert report["rollback"]["services_restored"] == list(fx.SERVICES)
    assert harness.systemd.active == set(fx.SERVICES)
    assert report["rollback"]["prior_loaded_runtime"]["mismatch"] == ""
    assert install_cli._read_maintenance_snapshot() is None
    assert _marker() is None


def test_post_activate_failure_with_retained_state_stays_put(harness) -> None:
    harness.cli.fail["verify"] = "verify FAIL"
    harness.cli.rollback_result = {
        "restore_safe": False,
        "retained_unknown": ["/var/lib/cortex/coordinator/jobs.json"],
        "retained_drift": [],
        "systemd_daemon_reload": "completed",
    }

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["result"] == "halted"
    assert report["phase"] == "post-activate"
    assert report["failed_step"] == "verify"
    assert report["rollback"]["retained_unknown"] == ["/var/lib/cortex/coordinator/jobs.json"]
    assert "cortex upgrade --recover" in report["next_action"]
    assert not [call for call in harness.systemd.calls if call[0] == "start"]
    snapshot = install_cli._read_maintenance_snapshot()
    assert snapshot is not None
    assert snapshot["receipt_path"] == str(harness.bound.receipt_path)
    assert _marker()["plan_sha256"] == harness.bound.sha256


def test_loaded_runtime_mismatch_after_verify_rolls_back(harness) -> None:
    # This is the "typical" post-activate failure, where the installer's
    # `rollback` call proves `restore_safe=true` (the fixture's default
    # `rollback_result`) — so `_abort` auto-restores the prior services per
    # the accepted design (spec §6/§7, `--recover`'s read of the same
    # contract). The other post-activate shape, `restore_safe=false` →
    # `halted` with services left stopped, is pinned separately by
    # `test_post_activate_failure_with_retained_state_stays_put` above.
    harness.cli.loaded[str(harness.bound.receipt_path)] = (
        "prior-receipt",
        fx.PRIOR_WHEEL,
        fx.PRIOR_COMMIT,
    )

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["failed_step"] == "loaded-runtime"
    assert "manager_receipt=mismatch" in report["error"]
    assert "rollback" in harness.cli.commands()
    assert report["result"] == "rolled-back"


def test_interrupt_after_the_apply_child_wrote_the_receipt_still_rolls_back(harness) -> None:
    harness.cli.interrupt_after = "apply"

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["failed_step"] == "apply"
    assert report["error"] == "interrupted by SIGTERM"
    rollback = next(call for call in harness.cli.calls if call[0] == "rollback")
    assert rollback[rollback.index("--receipt") + 1] == str(harness.bound.receipt_path)
    assert harness.systemd.active == set(fx.SERVICES)
    assert report["result"] == "rolled-back"


def test_failure_before_apply_restores_services_without_a_rollback(harness) -> None:
    harness.systemd.fail_stop.add("cortex-monitor.service")

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["failed_step"] == "stop-services"
    assert "apply" not in harness.cli.commands()
    assert "rollback" not in harness.cli.commands()
    assert report["rollback"]["attempted"] is False
    assert report["result"] == "rolled-back"
    assert harness.systemd.active == set(fx.SERVICES)


def test_a_changed_sealed_cli_stops_before_apply(harness) -> None:
    (harness.sealed.venv / "bin" / "cortex").write_text("#!/bin/sh\necho changed\n")

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["failed_step"] == "apply"
    assert "changed since ingress" in report["error"]
    assert "apply" not in harness.cli.commands()
    assert report["result"] == "rolled-back"


@pytest.fixture
def composed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(upgrade, "_pin_import_paths", lambda: None)
    prior = fx.make_prior(tmp_path)
    sealed = fx.make_sealed(tmp_path)
    bound = fx.make_bound(tmp_path)
    monkeypatch.setattr(
        upgrade, "preflight", lambda options: upgrade.Preflight(prior=prior, target=(0, 1, 13))
    )
    monkeypatch.setattr(upgrade, "ingest_release", lambda version, **_kwargs: sealed)
    monkeypatch.setattr(upgrade, "produce_plan", lambda *_args, **_kwargs: bound)
    return SimpleNamespace(tmp_path=tmp_path, prior=prior, sealed=sealed, bound=bound)


def test_perform_upgrade_publishes_the_report_and_prints_a_summary(
    composed, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    last = composed.tmp_path / "installer" / "last-upgrade-report.json"

    def transaction(_sealed, _bound, _prior, report, _steps):
        assert json.loads(last.read_text())["result"] == "in-progress"
        report["receipt"] = {
            "path": str(composed.bound.receipt_path),
            "receipt_id": "new-receipt",
        }
        report["result"] = "upgraded"
        return 0

    monkeypatch.setattr(upgrade, "run_transaction", transaction)

    assert upgrade.perform_upgrade(upgrade.UpgradeOptions(version="0.1.13")) == 0

    report = json.loads(
        (composed.tmp_path / "installer" / "0.1.13" / "upgrade-report.json").read_text()
    )
    metadata = composed.sealed.metadata
    assert report["result"] == "upgraded"
    assert report["plan"]["sha256"] == composed.bound.sha256
    assert report["candidate"]["assets"] == {
        asset.name: asset.sha256
        for asset in (metadata.wheel, metadata.install_input, metadata.qualification)
    }
    assert report["prior_receipt"] == {
        "path": str(composed.prior.path),
        "receipt_id": "prior-receipt",
        "version": "0.1.12",
    }
    assert [row["name"] for row in report["steps"]] == ["ingress", "plan"]
    assert json.loads(last.read_text()) == report
    out = capsys.readouterr().out
    assert out.startswith("cortex upgrade 0.1.13: upgraded\n")
    assert f"new receipt:   {composed.bound.receipt_path}" in out


def test_an_ingress_failure_is_reported_as_refused(
    composed, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(version, **_kwargs):
        raise IngressError("release asset digest does not match its REST metadata: x")

    monkeypatch.setattr(upgrade, "ingest_release", refuse)
    monkeypatch.setattr(
        upgrade, "run_transaction", lambda *_args: pytest.fail("must not reach the transaction")
    )

    assert upgrade.perform_upgrade(
        upgrade.UpgradeOptions(version="0.1.13", json_output=True)
    ) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "refused"
    assert report["failed_step"] == "ingress"
    assert "REST metadata" in report["error"]
    assert (composed.tmp_path / "installer" / "0.1.13" / "upgrade-report.json").exists()
