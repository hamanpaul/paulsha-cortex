"""#1263：`cortex upgrade` 的前置檢查不改動主機任何東西。"""
from __future__ import annotations

import ast
import sys
from argparse import Namespace
from pathlib import Path

import pytest

import upgrade_fixtures as fx
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import release_ingress, upgrade
from paulsha_cortex.trust_root.install.core import InstallError
from paulsha_cortex.trust_root.install.release_ingress import IngressError


def _args(**changes: object) -> Namespace:
    values: dict[str, object] = {
        "version": "0.1.13",
        "wait_idle": 0,
        "json_output": False,
        "recover": False,
        "status": False,
        "release_source": None,
        "allow_same_version": False,
        "prior_receipt": None,
    }
    values.update(changes)
    return Namespace(**values)


@pytest.mark.parametrize(
    "flag",
    [
        {"release_source": "/run/release"},
        {"allow_same_version": True},
        {"prior_receipt": "/run/prior.json"},
    ],
)
def test_test_only_flags_need_the_qualification_environment(flag) -> None:
    with pytest.raises(upgrade.UpgradeError, match="PSC_UPGRADE_QUALIFICATION=1"):
        upgrade.options_from_args(_args(**flag), environ={})

    options = upgrade.options_from_args(
        _args(**flag), environ={"PSC_UPGRADE_QUALIFICATION": "1"}
    )
    assert options.version == "0.1.13"


def test_qualification_paths_must_be_absolute() -> None:
    with pytest.raises(upgrade.UpgradeError, match="absolute"):
        upgrade.options_from_args(
            _args(release_source="relative/dir"),
            environ={"PSC_UPGRADE_QUALIFICATION": "1"},
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"version": None}, "target version is required"),
        ({"recover": True}, "take no version"),
        ({"version": None, "status": True, "wait_idle": 5}, "take no version"),
        ({"wait_idle": -1}, "zero or more"),
    ],
)
def test_option_combinations_are_validated(changes, message) -> None:
    with pytest.raises(upgrade.UpgradeError, match=message):
        upgrade.options_from_args(_args(**changes), environ={})


def test_malformed_version_is_refused_before_any_work() -> None:
    with pytest.raises(IngressError, match="MAJOR.MINOR.PATCH"):
        upgrade.options_from_args(_args(version="0.1.13-rc1"), environ={})


def test_status_takes_no_version() -> None:
    options = upgrade.options_from_args(_args(version=None, status=True), environ={})

    assert options.status is True
    assert options.version is None


@pytest.mark.parametrize(
    ("target", "allow", "ok"),
    [
        ("0.1.13", False, True),
        ("0.1.12", False, False),
        ("0.1.11", False, False),
        ("0.1.12", True, True),
        ("0.1.11", True, False),
    ],
)
def test_upgrade_only_moves_forward(target: str, allow: bool, ok: bool) -> None:
    if ok:
        assert upgrade.check_target_version(
            target, (0, 1, 12), allow_same_version=allow
        ) == release_ingress.parse_version(target)
    else:
        with pytest.raises(upgrade.UpgradeError, match="only moves forward"):
            upgrade.check_target_version(target, (0, 1, 12), allow_same_version=allow)


@pytest.fixture
def rootless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _o, _p: None)
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(upgrade, "_STATE_ROOT", (tmp_path / "var/lib/cortex").absolute())
    monkeypatch.setattr(upgrade, "_STATUS_SETTLE_SECONDS", 0)
    return tmp_path


def test_production_locates_the_prior_through_the_receipt_chain(rootless: Path) -> None:
    prior = fx.durable_prior(rootless)

    located = upgrade.locate_prior(upgrade.UpgradeOptions(version="0.1.13"))

    assert located.path == prior.path
    assert located.version == (0, 1, 12)


def test_qualification_prior_receipt_is_loaded_explicitly(rootless: Path) -> None:
    prior = fx.durable_prior(rootless)

    located = upgrade.locate_prior(
        upgrade.UpgradeOptions(version="0.1.13", prior_receipt=prior.path)
    )

    assert located.document["receipt_id"] == prior.to_dict()["receipt_id"]


def test_an_unqualified_prior_is_refused(rootless: Path) -> None:
    prior = fx.durable_prior(rootless, qualified=False)

    with pytest.raises(upgrade.UpgradeError, match="not applied and qualified"):
        upgrade.locate_prior(
            upgrade.UpgradeOptions(version="0.1.13", prior_receipt=prior.path)
        )


def test_preflight_refuses_when_running_services_do_not_match_the_prior(
    rootless: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.durable_prior(rootless)
    monkeypatch.setattr(
        upgrade,
        "await_loaded_runtime",
        lambda plan, path, expected, **_kwargs: ("manager_receipt=mismatch", None),
    )
    monkeypatch.setattr(
        upgrade,
        "in_flight_counts",
        lambda _plan: pytest.fail("must stop before the idle check"),
    )

    with pytest.raises(upgrade.UpgradeError, match="manager_receipt=mismatch"):
        upgrade.preflight(upgrade.UpgradeOptions(version="0.1.13"))


def test_preflight_passes_on_an_idle_matching_host(
    rootless: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior = fx.durable_prior(rootless)
    seen: list[tuple[Path, dict[str, object]]] = []

    def matched(plan, path, expected, **_kwargs):
        seen.append((path, dict(expected)))
        return "", {}

    monkeypatch.setattr(upgrade, "await_loaded_runtime", matched)
    monkeypatch.setattr(upgrade, "in_flight_counts", lambda _plan: (0, 0))

    checked = upgrade.preflight(upgrade.UpgradeOptions(version="0.1.13"))

    assert checked.target == (0, 1, 13)
    assert seen[0][0] == prior.path
    assert seen[0][1]["receipt_id"] == prior.to_dict()["receipt_id"]


def test_installer_idle_check_refuses_lease_marker_and_snapshot(rootless: Path) -> None:
    plan = fx.plan_document(
        rootless, version="0.1.12", wheel_sha256=fx.PRIOR_WHEEL, commit=fx.PRIOR_COMMIT
    )
    upgrade.assert_installer_idle()

    with install_cli._maintenance_lease(plan):
        with pytest.raises(InstallError, match="maintenance window is active"):
            upgrade.assert_installer_idle()
    with install_cli._host_lock(leaf="maintenance.lock", conflict="unexpected") as lock_fd:
        install_cli._write_lock_payload(
            lock_fd, {"plan_sha256": "a" * 64, "token_sha256": "b" * 64}
        )
    with pytest.raises(upgrade.UpgradeError, match="stale trust-root maintenance marker"):
        upgrade.assert_installer_idle()
    with install_cli._host_lock(leaf="maintenance.lock", conflict="unexpected") as lock_fd:
        install_cli._write_lock_payload(lock_fd, None)
    install_cli._write_maintenance_snapshot(
        {
            "schema_version": 1,
            "plan_sha256": "a" * 64,
            "receipt_path": str(rootless / "receipt.json"),
            "present_services": [],
            "previously_active": [],
        }
    )
    with pytest.raises(upgrade.UpgradeError, match="unfinished maintenance snapshot"):
        upgrade.assert_installer_idle()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_wait_idle_polls_until_jobs_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(upgrade, "_monotonic", clock.monotonic)
    monkeypatch.setattr(upgrade, "_sleep", clock.sleep)
    observations = iter([(1, 0), (0, 1), (0, 0)])
    monkeypatch.setattr(upgrade, "in_flight_counts", lambda _plan: next(observations))

    upgrade.wait_until_idle({}, wait_seconds=60)

    assert clock.sleeps == [5.0, 5.0]


def test_wait_idle_zero_refuses_at_once_with_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade, "in_flight_counts", lambda _plan: (2, 1))

    with pytest.raises(
        upgrade.UpgradeError, match="job processes=2, durable in-flight jobs=1"
    ):
        upgrade.wait_until_idle({}, wait_seconds=0)


def test_an_unreadable_durable_registry_counts_as_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(upgrade, "_monotonic", clock.monotonic)
    monkeypatch.setattr(upgrade, "_sleep", clock.sleep)
    monkeypatch.setattr(upgrade, "in_flight_counts", lambda _plan: (0, None))

    with pytest.raises(upgrade.UpgradeError, match="durable in-flight jobs=unknown"):
        upgrade.wait_until_idle({}, wait_seconds=10)
    assert sum(clock.sleeps) == 10.0


@pytest.mark.parametrize(
    ("leftover", "version"),
    [("snapshot", "0.1.13"), ("marker", "0.1.13"), ("marker", "0.1.11")],
)
def test_preflight_names_recover_before_any_other_check(
    rootless: Path, monkeypatch: pytest.MonkeyPatch, leftover: str, version: str
) -> None:
    # Final review item 4: after a crashed upgrade the services are stopped, so
    # the runtime check would wait 60s and then blame the services, and an older
    # target would hear "only moves forward"; the operator must hear --recover.
    fx.durable_prior(rootless)
    if leftover == "snapshot":
        install_cli._write_maintenance_snapshot(
            {
                "schema_version": 1,
                "plan_sha256": "a" * 64,
                "receipt_path": str(rootless / "receipt.json"),
                "present_services": [],
                "previously_active": [],
            }
        )
    else:
        with install_cli._host_lock(leaf="maintenance.lock", conflict="unexpected") as lock_fd:
            install_cli._write_lock_payload(
                lock_fd, {"plan_sha256": "a" * 64, "token_sha256": "b" * 64}
            )
    clock = _Clock()
    monkeypatch.setattr(upgrade, "_monotonic", clock.monotonic)
    monkeypatch.setattr(upgrade, "_sleep", clock.sleep)
    monkeypatch.setattr(upgrade, "_STATUS_SETTLE_SECONDS", 60)
    monkeypatch.setattr(
        upgrade,
        "_service_status",
        lambda _plan, _path: upgrade._StatusUnavailable("exit 3: services are stopped"),
    )
    monkeypatch.setattr(
        upgrade, "in_flight_counts", lambda _plan: pytest.fail("must stop first")
    )

    with pytest.raises(upgrade.UpgradeError, match=r"cortex upgrade --recover"):
        upgrade.preflight(upgrade.UpgradeOptions(version=version))
    assert clock.sleeps == []


def test_in_flight_counts_reads_job_accounts_from_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Host:
        def in_flight(self, job_uids, plan):
            seen["job_uids"] = dict(job_uids)
            return {"job_processes": 0, "durable_jobs": None}

    monkeypatch.setattr(upgrade, "LocalLegacyHostBackend", lambda: Host())
    plan = {
        "accounts": [
            {"name": "cortex-builder", "uid": 993},
            {"name": "cortex-manager", "uid": 991},
            {"name": "cortex-gate", "uid": 994},
        ]
    }

    assert upgrade.in_flight_counts(plan) == (0, None)
    assert seen["job_uids"] == {"cortex-builder": 993, "cortex-gate": 994}


def test_import_paths_are_pinned_to_the_resolved_venv_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path.resolve()
    slot = base / "venvs" / "slot-a"
    (slot / "lib").mkdir(parents=True)
    link = base / "venv"
    link.symlink_to(slot)
    monkeypatch.setattr(sys, "prefix", str(link))
    monkeypatch.setattr(sys, "path", [f"{link}/lib/python3/site-packages", "/usr/lib/python3"])

    upgrade._pin_import_paths()

    assert sys.path == [f"{slot}/lib/python3/site-packages", "/usr/lib/python3"]


def test_import_paths_are_left_alone_without_a_venv_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path.resolve() / "venv"
    real.mkdir()
    monkeypatch.setattr(sys, "prefix", str(real))
    monkeypatch.setattr(sys, "path", [f"{real}/lib/site-packages"])

    upgrade._pin_import_paths()

    assert sys.path == [f"{real}/lib/site-packages"]


def test_upgrade_modules_import_everything_at_module_level() -> None:
    for module in (upgrade, release_ingress):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        nested = [
            node.lineno
            for function in ast.walk(tree)
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(function)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        assert nested == [], f"{module.__name__} imports inside functions at {nested}"
