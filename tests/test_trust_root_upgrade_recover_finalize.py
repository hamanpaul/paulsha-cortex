"""#1270：`--recover` 對已 verify、服務在跑且 loaded runtime 一致的 receipt 改為收尾。

owner 裁決（2026-10-06）：升級在 verify PASS 之後、清 snapshot／marker 之前被打斷時，
`--recover` 不再 rollback 一個已驗證、正在服務的 receipt；三個條件任何一個不成立或
無法證明，就照舊走 `_recover_command` 的 rollback。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import upgrade_fixtures as fx
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import upgrade
from paulsha_cortex.trust_root.install.core import (
    InstallError,
    InstallReceipt,
    new_install_receipt,
    plan_sha256,
)

_ALL = list(fx.SERVICES)


@pytest.fixture
def interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A host where `cortex upgrade` died after verify PASS, before cleanup."""

    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _o, _p: None)
    monkeypatch.setattr(upgrade, "_OWNER_UID", os.getuid())
    monkeypatch.setattr(upgrade, "_pin_import_paths", lambda: None)
    monkeypatch.setattr(upgrade, "_STATE_ROOT", (tmp_path / "var/lib/cortex").absolute())
    monkeypatch.setattr(upgrade, "_STATUS_SETTLE_SECONDS", 0)
    monkeypatch.setattr(upgrade, "installed_runtime_env", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        upgrade,
        "ingest_release",
        lambda *_args, **_kwargs: pytest.fail("recover must not fetch a release"),
    )
    systemd = fx.FakeSystemd()
    monkeypatch.setattr(install_cli, "_systemctl", systemd)
    cli = fx.FakeCandidateCli(systemd)
    monkeypatch.setattr(upgrade, "_run", cli)
    (tmp_path / "installer").mkdir()

    prior = fx.durable_prior(tmp_path)
    bound = fx.make_bound(tmp_path)
    upgrade.publish_durable_plan(
        install_core.canonical_plan_bytes(bound.plan),
        bound.sha256,
        plans_root=tmp_path / "installer" / "plans",
    )
    receipt = new_install_receipt(bound.plan, path=bound.receipt_path)
    prior_document = prior.to_dict()
    receipt._document.update(
        state="applied",
        qualified=True,
        activated=True,
        services_started=True,
        parent_receipt={
            "path": str(prior.path),
            "receipt_id": prior_document["receipt_id"],
            "plan_sha256": prior_document["plan_sha256"],
        },
    )
    receipt._persist()
    receipt_id = str(receipt.to_dict()["receipt_id"])
    cli.loaded[str(bound.receipt_path)] = (receipt_id, fx.NEW_WHEEL, fx.NEW_COMMIT)
    cli.loaded[str(prior.path)] = (
        str(prior_document["receipt_id"]),
        fx.PRIOR_WHEEL,
        fx.PRIOR_COMMIT,
    )
    install_cli._write_maintenance_snapshot(
        {
            "schema_version": 1,
            "plan_sha256": bound.sha256,
            "receipt_path": str(bound.receipt_path),
            "present_services": _ALL,
            "previously_active": _ALL,
        }
    )
    _write_marker(bound.sha256)
    upgrade._publish_report(
        {
            "version": "0.1.13",
            "result": "halted",
            "failed_step": "loaded-runtime",
            "error": "interrupted by SIGTERM",
            "next_action": "the upgrade stopped inside the maintenance window; "
            "run `cortex upgrade --recover`",
            "plan": {"sha256": bound.sha256},
            "receipt": None,
            "finished_at": "2026-10-06T00:00:00Z",
            "report_path": str(tmp_path / "installer" / "0.1.13" / "upgrade-report.json"),
        }
    )

    rollbacks: list[dict[str, object]] = []
    real_recover = install_cli._recover_command

    def recover_command(args, *, emit=None):
        rollbacks.append(
            {
                "args": args,
                "snapshot": install_cli._read_maintenance_snapshot(),
                "marker": _marker(),
            }
        )
        (emit or install_cli._emit)(
            {
                "maintenance_recovered": True,
                "plan_sha256": args.confirm_sha256,
                "receipt_path": str(bound.receipt_path),
                "restore_safe": True,
                "services_restored": _ALL,
                "services_stopped": _ALL,
            }
        )
        return 0

    monkeypatch.setattr(install_cli, "_recover_command", recover_command)
    return SimpleNamespace(
        tmp_path=tmp_path,
        systemd=systemd,
        cli=cli,
        prior=prior,
        bound=bound,
        receipt_id=receipt_id,
        rollbacks=rollbacks,
        real_recover=real_recover,
    )


def _write_marker(sha: str) -> None:
    with install_cli._host_lock(leaf="maintenance.lock", conflict="unexpected") as lock_fd:
        install_cli._write_lock_payload(lock_fd, {"plan_sha256": sha, "token_sha256": "b" * 64})


def _clear_marker() -> None:
    with install_cli._host_lock(leaf="maintenance.lock", conflict="unexpected") as lock_fd:
        install_cli._write_lock_payload(lock_fd, None)


def _marker() -> object:
    with install_cli._host_lock_file(leaf="maintenance.lock") as lock_fd:
        return install_cli._maintenance_lock_payload(lock_fd, allow_absent=True)


def _drop_snapshot(env: SimpleNamespace) -> None:
    assert install_cli._clear_maintenance_snapshot(
        env.bound.plan, receipt_path=env.bound.receipt_path
    )


def _rewrite_receipt(env: SimpleNamespace, **fields: object) -> None:
    receipt = InstallReceipt.load(env.bound.receipt_path)
    receipt._document.update(fields)
    receipt._persist()


def _last_report(env: SimpleNamespace) -> dict[str, object]:
    return json.loads((env.tmp_path / "installer" / "last-upgrade-report.json").read_text())


def _service_changes(env: SimpleNamespace) -> list[tuple[str, ...]]:
    return [call for call in env.systemd.calls if call[0] in {"stop", "start"}]


def _assert_rolled_back(
    env: SimpleNamespace, capsys: pytest.CaptureFixture[str], reason: str
) -> dict[str, object]:
    assert upgrade.recover_upgrade() == 0

    assert len(env.rollbacks) == 1
    call = env.rollbacks[0]
    assert call["args"].confirm_sha256 == env.bound.sha256
    assert call["args"].plan == str(env.bound.durable_path)
    # Finalize left the recovery state for `_recover_command` untouched.
    assert call["snapshot"] is not None
    assert call["marker"] is not None
    assert call["marker"]["plan_sha256"] == env.bound.sha256
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["maintenance_recovered"] is True
    assert emitted["action"] == "rolled-back"
    assert emitted["reason"].startswith(reason), emitted["reason"]
    report = _last_report(env)
    assert report["result"] == "recovered"
    assert report["recovery"]["action"] == "rolled-back"
    assert report["recovery"]["reason"] == emitted["reason"]
    return emitted


@pytest.mark.parametrize("marker", [True, False], ids=["marker", "marker-lost-on-reboot"])
def test_recover_finalizes_a_verified_serving_receipt_instead_of_rolling_back(
    interrupted, capsys: pytest.CaptureFixture[str], marker: bool
) -> None:
    if not marker:
        _clear_marker()

    assert upgrade.recover_upgrade() == 0

    assert interrupted.rollbacks == []
    assert _service_changes(interrupted) == []
    assert "rollback" not in interrupted.cli.commands()
    assert json.loads(capsys.readouterr().out) == {
        "maintenance_recovered": True,
        "action": "finalized",
        "plan_sha256": interrupted.bound.sha256,
        "receipt_path": str(interrupted.bound.receipt_path),
        "receipt_id": interrupted.receipt_id,
        "snapshot_cleared": True,
        "services_restored": [],
        "services_stopped": [],
    }
    assert install_cli._read_maintenance_snapshot() is None
    assert _marker() is None
    receipt = InstallReceipt.load(interrupted.bound.receipt_path).to_dict()
    assert receipt["state"] == "applied" and receipt["qualified"] is True
    report = _last_report(interrupted)
    assert report["result"] == "finalized"
    assert report["next_action"] is None
    assert report["recovery"] == {
        "action": "finalized",
        "receipt": {
            "path": str(interrupted.bound.receipt_path),
            "receipt_id": interrupted.receipt_id,
        },
    }
    assert report["recovered_at"]


def test_status_after_finalize_shows_no_pending_maintenance(
    interrupted, capsys: pytest.CaptureFixture[str]
) -> None:
    assert upgrade.upgrade_status()["maintenance_pending"] is True

    assert upgrade.recover_upgrade() == 0
    capsys.readouterr()

    status = upgrade.upgrade_status()
    assert status["maintenance_pending"] is False
    assert status["effective_receipt"]["path"] == str(interrupted.bound.receipt_path)
    assert status["loaded_runtime"] == "match"
    assert status["last_upgrade"]["result"] == "finalized"
    assert upgrade.run_upgrade(upgrade.UpgradeOptions(status=True)) == 0
    assert "maintenance:       idle" in capsys.readouterr().out
    # The next upgrade no longer demands `--recover`.
    upgrade.assert_installer_idle()


def test_recover_checks_the_receipt_under_the_lease_and_transaction_lock(
    interrupted, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    held: list[str] = []
    probe = upgrade.await_loaded_runtime

    def locked_probe(plan, receipt_path, expected, **kwargs):
        for leaf in ("maintenance.lock", "transaction.lock"):
            with pytest.raises(InstallError):
                with install_cli._host_lock(leaf=leaf, conflict=f"{leaf} held"):
                    pass
            held.append(leaf)
        return probe(plan, receipt_path, expected, **kwargs)

    monkeypatch.setattr(upgrade, "await_loaded_runtime", locked_probe)

    assert upgrade.recover_upgrade() == 0

    assert held == ["maintenance.lock", "transaction.lock"]
    assert json.loads(capsys.readouterr().out)["action"] == "finalized"


def test_a_stale_marker_alone_finalizes_when_the_chain_proves_the_receipt(
    interrupted, capsys: pytest.CaptureFixture[str]
) -> None:
    # The snapshot was cleared; only the lease marker survived (killed before
    # the lease released it).  The marker names only the plan: the receipt
    # chain's effective receipt must come from that same plan.
    _drop_snapshot(interrupted)

    assert upgrade.recover_upgrade() == 0

    assert interrupted.rollbacks == []
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["action"] == "finalized"
    assert emitted["receipt_path"] == str(interrupted.bound.receipt_path)
    assert emitted["snapshot_cleared"] is False
    assert _marker() is None
    assert _last_report(interrupted)["result"] == "finalized"


def test_a_stale_marker_whose_plan_is_not_in_force_is_only_cleared_as_today(
    interrupted, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Killed before the snapshot (or after an abort finished): the receipt in
    # force is the prior one.  Today's `_recover_command` clears the marker and
    # touches nothing else; the result says so instead of "rolled-back".
    _drop_snapshot(interrupted)
    _rewrite_receipt(interrupted, qualified=False)
    monkeypatch.setattr(install_cli, "_recover_command", interrupted.real_recover)
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "validate_apply_plan", lambda _plan, **_kwargs: ())

    assert upgrade.recover_upgrade() == 0

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["maintenance_recovered"] is True
    assert emitted["receipt_path"] is None
    assert emitted["action"] == "marker-cleared"
    assert emitted["reason"] == "receipt-not-from-interrupted-plan"
    assert _service_changes(interrupted) == []
    assert _marker() is None
    report = _last_report(interrupted)
    assert report["result"] == "recovered"
    assert report["recovery"] == {
        "action": "marker-cleared",
        "reason": "receipt-not-from-interrupted-plan",
    }


def test_recover_rolls_back_a_receipt_that_is_not_qualified(
    interrupted, capsys: pytest.CaptureFixture[str]
) -> None:
    _rewrite_receipt(interrupted, qualified=False)

    _assert_rolled_back(interrupted, capsys, "receipt-not-qualified")


def test_recover_rolls_back_a_receipt_that_is_no_longer_applied(
    interrupted, capsys: pytest.CaptureFixture[str]
) -> None:
    _rewrite_receipt(interrupted, state="rolling-back")

    emitted = _assert_rolled_back(interrupted, capsys, "receipt-not-qualified")
    assert "state=rolling-back" in emitted["reason"]


def test_recover_rolls_back_when_the_loaded_runtime_does_not_match(
    interrupted, capsys: pytest.CaptureFixture[str]
) -> None:
    interrupted.cli.loaded[str(interrupted.bound.receipt_path)] = (
        "prior-receipt",
        fx.PRIOR_WHEEL,
        fx.PRIOR_COMMIT,
    )

    emitted = _assert_rolled_back(interrupted, capsys, "loaded-runtime-mismatch")
    assert "manager_receipt=mismatch" in emitted["reason"]


def test_recover_rolls_back_when_the_service_status_is_unavailable(
    interrupted, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(upgrade, "_run", unavailable)

    emitted = _assert_rolled_back(interrupted, capsys, "loaded-runtime-mismatch")
    assert "service_status=unavailable" in emitted["reason"]


def test_recover_rolls_back_when_a_service_is_inactive(
    interrupted, capsys: pytest.CaptureFixture[str]
) -> None:
    interrupted.systemd.active.discard("cortex-monitor.service")

    emitted = _assert_rolled_back(interrupted, capsys, "services-inactive")
    assert emitted["reason"] == "services-inactive: cortex-monitor.service"
    # Cheap checks first: a stopped host is never probed for its runtime.
    assert ("status", str(interrupted.bound.receipt_path)) not in interrupted.cli.calls


def test_recover_rolls_back_when_the_receipt_cannot_be_read(
    interrupted, capsys: pytest.CaptureFixture[str]
) -> None:
    interrupted.bound.receipt_path.write_text("{not json\n", encoding="utf-8")

    _assert_rolled_back(interrupted, capsys, "receipt-unreadable")


def test_recover_rolls_back_a_serving_receipt_the_snapshot_did_not_bind(
    interrupted, capsys: pytest.CaptureFixture[str]
) -> None:
    # The snapshot points at the prior receipt: applied, qualified and serving,
    # but not the interrupted plan's receipt.  Never finalize it.
    _drop_snapshot(interrupted)
    install_cli._write_maintenance_snapshot(
        {
            "schema_version": 1,
            "plan_sha256": interrupted.bound.sha256,
            "receipt_path": str(interrupted.prior.path),
            "present_services": _ALL,
            "previously_active": _ALL,
        }
    )

    emitted = _assert_rolled_back(interrupted, capsys, "receipt-not-from-interrupted-plan")
    assert emitted["reason"] == "receipt-not-from-interrupted-plan"


def test_recover_rolls_back_through_the_real_recover_command_when_apply_never_landed(
    interrupted, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Killed before apply wrote the receipt: services are stopped and there is
    # nothing to finalize; today's recovery restores the previous services.
    interrupted.bound.receipt_path.unlink()
    interrupted.systemd.active.clear()
    monkeypatch.setattr(install_cli, "_recover_command", interrupted.real_recover)
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "validate_apply_plan", lambda _plan, **_kwargs: ())

    assert upgrade.recover_upgrade() == 0

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["action"] == "rolled-back"
    assert emitted["reason"] == "receipt-absent"
    assert emitted["restore_safe"] is True
    assert emitted["services_restored"] == _ALL
    assert interrupted.systemd.active == set(_ALL)
    assert install_cli._read_maintenance_snapshot() is None
    assert _marker() is None
    assert _last_report(interrupted)["result"] == "recovered"


def test_a_failed_rollback_after_declining_keeps_the_reason_in_the_report(
    interrupted, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rewrite_receipt(interrupted, qualified=False)

    def refused(_args, *, emit=None):
        raise InstallError(
            "recovery rollback retained unknown or drifted state; services remain stopped"
        )

    monkeypatch.setattr(install_cli, "_recover_command", refused)

    with pytest.raises(InstallError, match="retained unknown"):
        upgrade.recover_upgrade()

    report = _last_report(interrupted)
    assert report["result"] == "halted"
    assert report["recovery"]["action"] == "rolled-back"
    assert report["recovery"]["reason"].startswith("receipt-not-qualified")


def test_a_durable_plan_that_is_not_canonical_is_never_finalized(
    interrupted, capsys: pytest.CaptureFixture[str]
) -> None:
    # The recorded sha names the plan file's bytes; finalize also needs the
    # parsed plan to hash to it, or it cannot bind the lease and snapshot.
    payload = b'{"plan": "x"}\n'
    sha = upgrade.hashlib.sha256(payload).hexdigest()
    assert plan_sha256({"plan": "x"}) != sha
    upgrade.publish_durable_plan(
        payload, sha, plans_root=interrupted.tmp_path / "installer" / "plans"
    )
    _drop_snapshot(interrupted)
    _write_marker(sha)

    assert upgrade.recover_upgrade() == 0

    assert len(interrupted.rollbacks) == 1
    assert interrupted.rollbacks[0]["args"].confirm_sha256 == sha
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["action"] == "rolled-back"
    assert emitted["reason"] == "plan-mismatch"
