"""#1263：transaction 的步驟順序、sha／token 綁定，以及每一種失敗分支。"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import upgrade_fixtures as fx
from paulsha_cortex.trust_root.install import backend as install_backend
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
    in_flight = [(0, 0)]

    def counts(plan):
        assert plan is prior.plan or plan == prior.plan
        systemd.calls.append(("in-flight",))
        return in_flight.pop(0) if len(in_flight) > 1 else in_flight[0]

    monkeypatch.setattr(upgrade, "in_flight_counts", counts)
    return SimpleNamespace(
        systemd=systemd, cli=cli, sealed=sealed, bound=bound, prior=prior, in_flight=in_flight
    )


def _run_transaction(
    harness: SimpleNamespace, *, wait_idle: int = 0, steps: upgrade.StepLog | None = None
) -> tuple[int, dict[str, object]]:
    report: dict[str, object] = {"version": "0.1.13", "services_stopped": False}
    code = upgrade.run_transaction(
        harness.sealed,
        harness.bound,
        harness.prior,
        report,
        steps or upgrade.StepLog(),
        wait_idle=wait_idle,
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


def test_candidate_cli_calls_get_a_root_path_with_sbin_and_no_usr_local(harness) -> None:
    # #1263 RC qualification (run 37336228620): the activate-before drill's
    # `apply` hit `preflight failed: universal NOPASSWD is forbidden` because
    # `_candidate`'s PATH had no `/usr/sbin` for `shutil.which("visudo")` to
    # find -- even though a manual `install trust-root apply` with a normal
    # root PATH passed the same preflight. Every candidate-CLI call (apply,
    # credentials inherit, activate, verify, rollback) must get a PATH that
    # actually has the sealed venv first and the sbin dirs an installer needs
    # for `visudo`/`cvtsudoers`/`useradd`/`groupadd`.
    code, _report = _run_transaction(harness)
    assert code == 0

    apply_index = harness.cli.commands().index("apply")
    path = harness.cli.envs[apply_index]["PATH"]
    entries = path.split(":")

    assert entries[0] == f"{harness.sealed.venv}/bin"
    assert "/usr/sbin" in entries
    assert "/sbin" in entries
    assert "/usr/local" not in path


def test_a_tool_that_only_lives_in_sbin_resolves_under_the_candidate_path_shape(
    tmp_path: Path,
) -> None:
    """Hermetic: on Debian/Ubuntu `visudo`/`cvtsudoers`/`useradd`/`groupadd`
    live in `/usr/sbin`, not `/usr/bin`. Builds the candidate PATH's *shape*
    (venv/bin first, then sbin ahead of bin) out of tmp_path directories --
    never the host's real `/usr/sbin` -- and proves `shutil.which` only finds
    the sbin-only tool once the sbin segment is present.
    """

    root = tmp_path / "root"
    venv_bin = root / "venv" / "bin"
    usr_sbin = root / "usr" / "sbin"
    usr_bin = root / "usr" / "bin"
    sbin = root / "sbin"
    bin_dir = root / "bin"
    for directory in (venv_bin, usr_sbin, usr_bin, sbin, bin_dir):
        directory.mkdir(parents=True)

    tool = usr_sbin / "visudo"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)

    with_sbin = f"{venv_bin}:{usr_sbin}:{usr_bin}:{sbin}:{bin_dir}"
    assert shutil.which("visudo", path=with_sbin) == str(tool)

    # The regression this guards against: the old candidate PATH shape had no
    # sbin segment at all, and never finds an sbin-only tool.
    without_sbin = f"{venv_bin}:{usr_bin}:{bin_dir}"
    assert shutil.which("visudo", path=without_sbin) is None


def test_jobs_started_during_preparation_refuse_before_any_service_stops(harness) -> None:
    # Final review item 5: preflight ran before ingress, venv build and plan;
    # the lease re-checks just before stopping, and a refusal stops nothing.
    harness.in_flight[:] = [(1, 0)]
    steps = upgrade.StepLog()

    with pytest.raises(upgrade.UpgradeError, match="in-flight jobs block the upgrade"):
        _run_transaction(harness, wait_idle=0, steps=steps)

    assert harness.systemd.calls == [("in-flight",)]
    assert harness.cli.calls == []
    assert steps.rows[-1]["name"] == "idle-check"
    assert steps.rows[-1]["status"] == "failed"
    assert install_cli._read_maintenance_snapshot() is None
    assert _marker() is None


def test_the_in_lease_idle_check_waits_like_preflight(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(upgrade, "_monotonic", lambda: clock.now)
    monkeypatch.setattr(upgrade, "_sleep", sleep)
    harness.in_flight[:] = [(1, 0), (0, 0)]

    code, report = _run_transaction(harness, wait_idle=60)

    assert code == 0
    assert report["result"] == "upgraded"
    assert clock.sleeps == [5.0]
    events = harness.systemd.calls
    first_stop = min(index for index, call in enumerate(events) if call[0] == "stop")
    assert max(index for index, call in enumerate(events) if call == ("in-flight",)) < first_stop


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


def test_a_failed_rollback_child_surfaces_its_stderr_in_the_report(harness) -> None:
    # Fix round 1 / Finding 2: the real installer's rollback subcommand writes
    # only to stderr and exits non-zero with empty stdout when it raises
    # (`cli.py`'s top-level `except (InstallError, ...)` handler) -- unlike the
    # normal restore_safe=false path, which always emits a JSON payload first.
    # `harness.cli.fail[...]` reproduces exactly that shape (empty stdout,
    # `returncode=1`, stderr-only), for any candidate subcommand including
    # "rollback".
    harness.cli.fail["verify"] = "verify FAIL"
    harness.cli.fail["rollback"] = "backend lock is held by another process"

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["result"] == "halted"
    assert "backend lock is held by another process" in report["rollback"]["error"]
    assert report["rollback"]["restore_safe"] is False
    assert not [call for call in harness.systemd.calls if call[0] == "start"]
    snapshot = install_cli._read_maintenance_snapshot()
    assert snapshot is not None
    assert snapshot["receipt_path"] == str(harness.bound.receipt_path)


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


def test_a_verify_refusal_keeps_its_stderr_in_the_report(harness) -> None:
    # Final review item 2: refusals reach only the child's stderr.
    harness.cli.fail["verify"] = "maintenance token does not authorize the active plan"

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["failed_step"] == "verify"
    assert report["error"].startswith("verify did not PASS (exit 1)")
    assert "maintenance token does not authorize the active plan" in report["error"]


def test_a_verify_fail_keeps_its_stdout_detail_in_the_report(harness) -> None:
    # Final review item 2: a FAIL result is the JSON verify prints to stdout.
    harness.cli.verify_failure = {
        "ok": False,
        "checks": [{"name": "unit-drift", "status": "FAIL", "path": "cortex-manager.service"}],
    }

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["failed_step"] == "verify"
    assert '"name": "unit-drift"' in report["error"]


def test_the_verify_failure_detail_is_a_bounded_tail(harness) -> None:
    harness.cli.fail["verify"] = "x" * 20000 + " last line names the cause"

    _code, report = _run_transaction(harness)

    assert len(report["error"]) < 4200
    assert report["error"].rstrip().endswith("last line names the cause")


def test_an_unavailable_service_status_names_the_exit_code(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Final review item 2: "service_status=unavailable" alone hid the cause.
    monkeypatch.setattr(
        upgrade,
        "_run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 3, "", "service status failed: install receipt is unreadable\n"
        ),
    )

    mismatch, payload = upgrade.await_loaded_runtime(
        harness.prior.plan, harness.prior.path, {}, settle_seconds=0
    )

    assert payload is None
    assert mismatch.startswith("service_status=unavailable")
    assert "exit 3" in mismatch
    assert "install receipt is unreadable" in mismatch


def test_an_unavailable_service_status_names_the_exception_type(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(argv, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(upgrade, "_run", missing)

    mismatch, payload = upgrade.await_loaded_runtime(
        harness.prior.plan, harness.prior.path, {}, settle_seconds=0
    )

    assert payload is None
    assert mismatch.startswith("service_status=unavailable")
    assert "FileNotFoundError" in mismatch


def test_interrupt_after_the_apply_child_wrote_the_receipt_still_rolls_back(
    harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.cli.interrupt_after = "apply"

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["failed_step"] == "apply"
    assert report["error"] == "interrupted by SIGTERM"
    # Fix round 1 / Finding 1: the report must name the new receipt even though
    # apply never returned -- it is set as soon as the child starts, not only
    # on a successful return, so a halted/rolled-back report still tells the
    # operator which receipt holds any retained state.
    assert report["receipt"]["path"] == str(harness.bound.receipt_path)
    rollback = next(call for call in harness.cli.calls if call[0] == "rollback")
    assert rollback[rollback.index("--receipt") + 1] == str(harness.bound.receipt_path)
    assert harness.systemd.active == set(fx.SERVICES)
    assert report["result"] == "rolled-back"

    upgrade._print_outcome(report, json_output=False)
    assert f"new receipt:   {harness.bound.receipt_path}" in capsys.readouterr().out


@contextmanager
def _recording_handler(signum: int) -> Iterator[list[int]]:
    """Install a harmless handler so a stray signal never kills the test run."""

    received: list[int] = []
    previous = signal.signal(signum, lambda number, _frame: received.append(number))
    try:
        yield received
    finally:
        signal.signal(signum, previous)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_after_verification_cannot_undo_the_upgrade(
    harness, monkeypatch: pytest.MonkeyPatch, signum: int
) -> None:
    # Final review item 3/6: once `_advance` returns, the receipt is verified;
    # INT/TERM/HUP while clearing the snapshot must not turn it into "halted".
    clear = install_cli._clear_maintenance_snapshot

    def clear_with_signal(plan, *, receipt_path):
        os.kill(os.getpid(), signum)
        return clear(plan, receipt_path=receipt_path)

    monkeypatch.setattr(install_cli, "_clear_maintenance_snapshot", clear_with_signal)

    with _recording_handler(signum) as received:
        code, report = _run_transaction(harness)

    assert code == 0
    assert report["result"] == "upgraded"
    assert "rollback" not in harness.cli.commands()
    assert install_cli._read_maintenance_snapshot() is None
    assert _marker() is None
    # Ignored, not merely handled: the handler outside the window never ran.
    assert received == []


def test_sighup_inside_the_maintenance_window_rolls_back(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Final review item 6: a dropped SSH session sends SIGHUP; it must roll back
    # and report like SIGTERM instead of killing the coordinator.
    candidate = harness.cli

    def run(argv, **kwargs):
        result = candidate(argv, **kwargs)
        if tuple(argv)[3:4] == ("apply",):
            os.kill(os.getpid(), signal.SIGHUP)
        return result

    monkeypatch.setattr(upgrade, "_run", run)

    with _recording_handler(signal.SIGHUP) as received:
        code, report = _run_transaction(harness)

    assert received == []
    assert code == 1
    assert report["failed_step"] == "apply"
    assert report["error"] == "interrupted by SIGHUP"
    assert report["result"] == "rolled-back"
    assert "rollback" in harness.cli.commands()
    assert harness.systemd.active == set(fx.SERVICES)


def test_candidate_children_run_in_their_own_session_without_the_callers_stdin(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1270: the terminal's INT/HUP reach only the coordinator, which decides
    # how the running installer step ends; the child never reads root's stdin.
    seen: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(argv, **kwargs):
        seen.append((tuple(argv)[1:3], kwargs))
        return harness.cli(argv, **kwargs)

    monkeypatch.setattr(upgrade, "_run", run)

    code, _report = _run_transaction(harness)

    assert code == 0
    candidate = [kwargs for head, kwargs in seen if head == ("install", "trust-root")]
    assert len(candidate) == 4
    for kwargs in candidate:
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL


_SIGNAL_PARENT = (
    "import os, signal, sys, time\n"
    "for _ in range(int(sys.argv[2])):\n"
    "    os.kill(os.getppid(), signal.SIGTERM)\n"
    "    time.sleep(0.3)\n"
    "time.sleep(float(sys.argv[3]))\n"
    "open(sys.argv[1], 'w').write('finished')\n"
)


def _apply_child_signals_the_coordinator(
    harness, monkeypatch: pytest.MonkeyPatch, marker: Path, *, signals: int, linger: float
) -> None:
    """Run a real child for `apply` that sends SIGTERM to the coordinator mid-step."""

    candidate = harness.cli

    def run(argv, **kwargs):
        if tuple(argv)[3:4] == ("apply",):
            child = install_backend._run(
                (sys.executable, "-c", _SIGNAL_PARENT, str(marker), str(signals), str(linger)),
                **kwargs,
            )
            assert child.returncode == 0, child.stderr
        return candidate(argv, **kwargs)

    monkeypatch.setattr(upgrade, "_run", run)


def test_a_real_sigterm_lets_the_running_installer_step_finish_then_rolls_back(
    harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #1270: a signal while a root child mutates the host must not SIGKILL that
    # child; the coordinator waits for the step to end, then rolls back.
    marker = tmp_path / "apply-child"
    _apply_child_signals_the_coordinator(harness, monkeypatch, marker, signals=1, linger=0.5)

    with _recording_handler(signal.SIGTERM) as received:
        code, report = _run_transaction(harness)

    assert marker.read_text() == "finished"
    assert received == []
    assert code == 1
    assert report["failed_step"] == "apply"
    assert report["error"] == "interrupted by SIGTERM"
    assert report["result"] == "rolled-back"
    assert "rollback" in harness.cli.commands()
    assert "credentials inherit" not in harness.cli.commands()
    assert harness.systemd.active == set(fx.SERVICES)


def test_a_second_signal_stops_a_running_installer_step_at_once(
    harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An operator who signals again does not wait for a hung step: the child is
    # stopped (journal crash recovery covers it) and the upgrade rolls back.
    marker = tmp_path / "apply-child"

    def run(argv, **kwargs):
        if tuple(argv)[3:4] == ("apply",):
            install_backend._run(
                (sys.executable, "-c", _SIGNAL_PARENT, str(marker), "2", "60"), **kwargs
            )
        return harness.cli(argv, **kwargs)

    monkeypatch.setattr(upgrade, "_run", run)

    with _recording_handler(signal.SIGTERM) as received:
        code, report = _run_transaction(harness)

    assert not marker.exists()
    assert received == []
    assert code == 1
    assert report["failed_step"] == "apply"
    assert report["error"] == "interrupted by SIGTERM"
    assert report["result"] == "rolled-back"


def test_a_hung_service_status_probe_times_out_as_unavailable(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[object] = []

    def hung(argv, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(upgrade, "_run", hung)

    mismatch, payload = upgrade.await_loaded_runtime(
        harness.prior.plan, harness.prior.path, {}, settle_seconds=0
    )

    assert payload is None
    assert mismatch.startswith("service_status=unavailable (TimeoutExpired")
    assert timeouts == [upgrade._STATUS_TIMEOUT_SECONDS]


def test_a_silent_service_status_failure_names_its_exit_code_once(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1270: a child with no output used to read "exit 3: exit 3".
    monkeypatch.setattr(
        upgrade, "_run", lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 3, "", "")
    )

    mismatch, _payload = upgrade.await_loaded_runtime(
        harness.prior.plan, harness.prior.path, {}, settle_seconds=0
    )

    assert mismatch == "service_status=unavailable (exit 3 with no output)"


def test_a_silent_verify_failure_names_its_exit_code_once(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv, **kwargs):
        if tuple(argv)[3:4] == ("verify",):
            return subprocess.CompletedProcess(argv, 1, "", "")
        return harness.cli(argv, **kwargs)

    monkeypatch.setattr(upgrade, "_run", run)

    _code, report = _run_transaction(harness)

    assert report["failed_step"] == "verify"
    assert report["error"] == "verify did not PASS (exit 1) with no output"


def test_a_silent_rollback_child_names_its_exit_code_once(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.cli.fail["verify"] = "verify FAIL"

    def run(argv, **kwargs):
        if tuple(argv)[3:4] == ("rollback",):
            harness.cli.calls.append(("rollback",))
            return subprocess.CompletedProcess(argv, 1, "", "")
        return harness.cli(argv, **kwargs)

    monkeypatch.setattr(upgrade, "_run", run)

    code, report = _run_transaction(harness)

    assert code == 1
    assert report["result"] == "halted"
    assert report["rollback"]["error"] == "rollback exited 1 with no output"
    assert report["rollback"]["restore_safe"] is False
    assert install_cli._read_maintenance_snapshot() is not None


def test_a_sigint_right_after_the_window_closes_keeps_the_upgrade_recorded(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1270: the result used to be written after `_signals_raise` restored the
    # default handlers, so a SIGINT landing in between left `result` unset and
    # perform_upgrade reported a finished upgrade as "halted".
    real_signal = signal.signal
    saved = {sig: signal.getsignal(sig) for sig in upgrade._INTERRUPT_SIGNALS}
    last = upgrade._INTERRUPT_SIGNALS[-1]
    armed: list[bool] = []
    clear = install_cli._clear_maintenance_snapshot

    def clear_then_arm(plan, *, receipt_path):
        cleared = clear(plan, receipt_path=receipt_path)
        armed.append(True)
        return cleared

    def restore_then_interrupt(signum, handler):
        previous = real_signal(signum, handler)
        if armed and signum == last and handler is saved[last]:
            armed.clear()
            raise KeyboardInterrupt
        return previous

    monkeypatch.setattr(install_cli, "_clear_maintenance_snapshot", clear_then_arm)
    monkeypatch.setattr(signal, "signal", restore_then_interrupt)
    report: dict[str, object] = {"version": "0.1.13", "services_stopped": False}
    try:
        with pytest.raises(KeyboardInterrupt):
            upgrade.run_transaction(
                harness.sealed,
                harness.bound,
                harness.prior,
                report,
                upgrade.StepLog(),
                wait_idle=0,
            )
    finally:
        for sig, handler in saved.items():
            real_signal(sig, handler)

    assert report["result"] == "upgraded"
    assert "rollback" not in harness.cli.commands()


def test_perform_upgrade_keeps_an_upgrade_finished_before_a_late_interrupt(
    composed, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def transaction(_sealed, _bound, _prior, report, _steps, *, wait_idle):
        report["result"] = "upgraded"
        raise KeyboardInterrupt

    monkeypatch.setattr(upgrade, "run_transaction", transaction)

    assert upgrade.perform_upgrade(
        upgrade.UpgradeOptions(version="0.1.13", json_output=True)
    ) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "upgraded"
    assert report["error"] is None


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

    def transaction(_sealed, _bound, _prior, report, _steps, *, wait_idle):
        assert wait_idle == 0
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
        upgrade, "run_transaction", lambda *_args, **_kwargs: pytest.fail("must not reach the transaction")
    )

    assert upgrade.perform_upgrade(
        upgrade.UpgradeOptions(version="0.1.13", json_output=True)
    ) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "refused"
    assert report["failed_step"] == "ingress"
    assert "REST metadata" in report["error"]
    assert (composed.tmp_path / "installer" / "0.1.13" / "upgrade-report.json").exists()


def test_an_in_lease_idle_refusal_is_reported_as_refused(
    composed, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Final review item 5: same `--wait-idle` semantics as preflight; nothing
    # was stopped, so the report says refused and the lease is released.
    seen: list[tuple[object, int]] = []

    def busy(plan, *, wait_seconds):
        seen.append((plan, wait_seconds))
        raise upgrade.UpgradeError(
            "in-flight jobs block the upgrade: job processes=1, durable in-flight jobs=0; "
            "retry when idle or pass --wait-idle <seconds>"
        )

    monkeypatch.setattr(upgrade, "wait_until_idle", busy)
    monkeypatch.setattr(
        install_cli, "_systemctl", lambda *args: pytest.fail(f"nothing may stop: {args}")
    )

    assert upgrade.perform_upgrade(
        upgrade.UpgradeOptions(version="0.1.13", wait_idle=7, json_output=True)
    ) == 1

    report = json.loads(capsys.readouterr().out)
    assert seen == [(composed.prior.plan, 7)]
    assert report["result"] == "refused"
    assert report["failed_step"] == "idle-check"
    assert "in-flight jobs block the upgrade" in report["error"]
    assert report["services_stopped"] is False
    assert report["rollback"] is None
    assert install_cli._read_maintenance_snapshot() is None
    assert _marker() is None
