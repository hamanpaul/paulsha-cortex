"""#1286：帳號 uid／gid 改在 plan 時決定，不再由 release config 寫死 991–995。

來源優先順序（逐帳號、逐欄位）：host overlay（或 install config 自己宣告）＞主機上已有的
同名帳號／群組＞確定性自動配號（uid 與 gid 都空、盡量 uid＝gid、system 範圍、避開
990–999）。plan 只讀注入的 passwd／group 快照；這裡的測試一律不讀主機的檔案。
"""
from __future__ import annotations

import hashlib
import json
import random
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paulsha_cortex.trust_root.install import (  # noqa: E402
    AccountCollisionError,
    InstallPlanError,
    apply_plan,
    bind_bundle_artifacts,
    build_install_plan,
    new_install_receipt,
    plan_sha256,
    validate_apply_plan,
    validate_preflight,
)
from paulsha_cortex.trust_root.install import cli as install_cli  # noqa: E402
from paulsha_cortex.trust_root.install import core as install_core  # noqa: E402
from paulsha_cortex.trust_root.install.core import HostAccounts  # noqa: E402
from test_trust_root_install_legacy_inventory import (  # noqa: E402
    _release_config,
    _write_bundle,
)
from test_trust_root_install_plan import _artifacts, _safe_config  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
CORTEX_ACCOUNTS = (
    "cortex-builder",
    "cortex-gate",
    "cortex-manager",
    "cortex-reviewer-planner",
    "cortex-egress",
)
#: Ubuntu 24.04 主機的 99x：system 帳號從 999 往下配，udev 群組也在這裡（#1286 實測）。
UBUNTU_USERS = (
    ("root", 0, 0),
    ("daemon", 1, 1),
    ("messagebus", 100, 101),
    ("syslog", 101, 102),
    ("systemd-network", 998, 998),
    ("systemd-timesync", 996, 996),
    ("systemd-resolve", 991, 991),
    ("polkitd", 990, 990),
    ("nobody", 65534, 65534),
)
UBUNTU_GROUPS = (
    ("root", 0),
    ("daemon", 1),
    ("messagebus", 101),
    ("syslog", 102),
    ("systemd-journal", 999),
    ("systemd-network", 998),
    ("crontab", 997),
    ("systemd-timesync", 996),
    ("input", 995),
    ("sgx", 994),
    ("kvm", 993),
    ("render", 992),
    ("systemd-resolve", 991),
    ("polkitd", 990),
    ("docker", 989),
    ("nogroup", 65534),
)
UBUNTU = HostAccounts(users=UBUNTU_USERS, groups=UBUNTU_GROUPS)


def _without_ids(config: dict) -> dict:
    stripped = deepcopy(config)
    for section in ("accounts", "service_accounts"):
        for row in stripped[section].values():
            row.pop("uid", None)
            row.pop("gid", None)
    return stripped


def _plan(tmp_path: Path, config: dict, host_accounts) -> dict:
    wheel, bundle = _artifacts(tmp_path)
    return build_install_plan(
        config=config,
        candidate_wheel=wheel,
        bundle=bundle,
        host_accounts=host_accounts,
    )


def _ids(plan: dict) -> dict[str, tuple[int, int]]:
    return {
        row["name"]: (row["uid"], row["gid"])
        for section in ("accounts", "service_accounts")
        for row in plan[section]
    }


def _account_steps(plan: dict) -> dict[str, dict]:
    return {
        step["name"]: step
        for step in plan["apply_order"]
        if step.get("kind") == "account"
    }


def _with(snapshot: HostAccounts, *, users=(), groups=()) -> HostAccounts:
    return HostAccounts(
        users=tuple(snapshot.users) + tuple(users),
        groups=tuple(snapshot.groups) + tuple(groups),
    )


# ---------------------------------------------------------------------------
# release config 不再寫死號碼
# ---------------------------------------------------------------------------


def test_release_install_config_declares_no_account_ids(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"candidate_sha": "a" * 40, "toolchain": []}), encoding="utf-8")
    output = tmp_path / "install-config.yaml"
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "qualification" / "write_install_config.py"),
            "--bundle",
            str(bundle),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    config = json.loads(output.read_text(encoding="utf-8"))

    for section in ("accounts", "service_accounts"):
        for name, row in config[section].items():
            assert set(row) == {"home", "shell"}, name


# ---------------------------------------------------------------------------
# 自動配號
# ---------------------------------------------------------------------------


def test_ubuntu_host_allocates_free_ids_outside_the_distro_band(tmp_path: Path) -> None:
    plan = _plan(tmp_path, _without_ids(_safe_config(tmp_path)), UBUNTU)

    ids = _ids(plan)
    # docker 占了 gid 989：989 的 uid 雖空，uid＝gid 配不起來就跳過。
    assert ids == {
        "cortex-builder": (988, 988),
        "cortex-gate": (987, 987),
        "cortex-manager": (986, 986),
        "cortex-reviewer-planner": (985, 985),
        "cortex-egress": (984, 984),
    }
    assert plan["account_id_sources"] == {
        name: {"uid": "allocated", "gid": "allocated"} for name in CORTEX_ACCOUNTS
    }
    for uid, gid in ids.values():
        assert 100 <= uid <= 989 and 100 <= gid <= 989
    steps = _account_steps(plan)
    assert {name: (step["uid"], step["gid"]) for name, step in steps.items()} == ids
    bound = _bound(tmp_path, plan)
    assert validate_apply_plan(bound, confirm_sha256=plan_sha256(bound))


def test_same_snapshot_yields_the_same_ids_and_plan_sha(tmp_path: Path) -> None:
    config = _without_ids(_safe_config(tmp_path))
    first = _plan(tmp_path, config, UBUNTU)
    users = list(UBUNTU_USERS)
    groups = list(UBUNTU_GROUPS)
    random.Random(1286).shuffle(users)
    random.Random(6821).shuffle(groups)
    second = _plan(tmp_path, config, HostAccounts(users=tuple(users), groups=tuple(groups)))

    assert _ids(first) == _ids(second)
    assert plan_sha256(first) == plan_sha256(second)


def test_allocation_skips_ids_where_either_the_uid_or_the_gid_is_taken(
    tmp_path: Path,
) -> None:
    host = HostAccounts(
        users=(("root", 0, 0), ("uid-only", 989, 50)),
        groups=(("root", 0), ("gid-only", 988), ("primary", 50)),
    )
    plan = _plan(tmp_path, _without_ids(_safe_config(tmp_path)), host)

    ids = _ids(plan)
    assert ids["cortex-builder"] == (987, 987)
    taken = {989, 988}
    for uid, gid in ids.values():
        assert uid == gid and uid not in taken


def test_a_user_primary_gid_without_a_group_row_counts_as_taken(tmp_path: Path) -> None:
    host = HostAccounts(users=(("root", 0, 0), ("orphan-primary", 4000, 989)), groups=(("root", 0),))

    plan = _plan(tmp_path, _without_ids(_safe_config(tmp_path)), host)

    assert _ids(plan)["cortex-builder"] == (988, 988)


def test_missing_ids_without_a_host_snapshot_are_refused(tmp_path: Path) -> None:
    with pytest.raises(InstallPlanError, match="passwd/group snapshot"):
        _plan(tmp_path, _without_ids(_safe_config(tmp_path)), None)


def test_exhausted_system_range_is_refused(tmp_path: Path) -> None:
    host = HostAccounts(
        users=tuple((f"u{n}", n, n) for n in range(100, 990)),
        groups=tuple((f"g{n}", n) for n in range(100, 990)),
    )

    with pytest.raises(InstallPlanError, match="no free system"):
        _plan(tmp_path, _without_ids(_safe_config(tmp_path)), host)


def test_pinned_ids_never_read_the_snapshot(tmp_path: Path) -> None:
    def explode() -> HostAccounts:
        raise AssertionError("fully pinned config must not read passwd/group")

    plan = _plan(tmp_path, _safe_config(tmp_path), explode)

    assert _ids(plan)["cortex-manager"] == (991, 991)
    assert plan["account_id_sources"]["cortex-manager"] == {"uid": "config", "gid": "config"}


# ---------------------------------------------------------------------------
# 已有同名帳號／群組：沿用
# ---------------------------------------------------------------------------


def test_existing_same_name_accounts_keep_their_ids(tmp_path: Path) -> None:
    host = _with(
        UBUNTU,
        users=(("cortex-manager", 1234, 1235), ("cortex-egress", 970, 971)),
        groups=(("cortex-manager", 1235), ("cortex-egress", 971)),
    )
    plan = _plan(tmp_path, _without_ids(_safe_config(tmp_path)), host)

    ids = _ids(plan)
    assert ids["cortex-manager"] == (1234, 1235)
    assert ids["cortex-egress"] == (970, 971)
    assert plan["account_id_sources"]["cortex-manager"] == {"uid": "existing", "gid": "existing"}
    assert plan["account_id_sources"]["cortex-egress"] == {"uid": "existing", "gid": "existing"}
    # 其餘帳號照舊配號，也不會搶到沿用中的號碼。
    assert plan["account_id_sources"]["cortex-builder"] == {
        "uid": "allocated",
        "gid": "allocated",
    }
    assert ids["cortex-builder"] == (988, 988)


def test_an_existing_group_alone_keeps_its_gid_and_the_uid_prefers_it(tmp_path: Path) -> None:
    host = _with(UBUNTU, groups=(("cortex-gate", 970),))

    plan = _plan(tmp_path, _without_ids(_safe_config(tmp_path)), host)

    assert _ids(plan)["cortex-gate"] == (970, 970)
    assert plan["account_id_sources"]["cortex-gate"] == {"uid": "allocated", "gid": "existing"}


# ---------------------------------------------------------------------------
# overlay 優先
# ---------------------------------------------------------------------------


def _release_effective(tmp_path: Path) -> tuple[dict, Path]:
    bundle = _write_bundle(tmp_path)
    config = json.loads(_release_config(tmp_path, bundle).read_text(encoding="utf-8"))
    return config, bundle


def test_overlay_ids_win_over_existing_accounts_and_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, bundle = _release_effective(tmp_path)
    host = _with(
        UBUNTU,
        users=(("cortex-builder", 977, 977),),
        groups=(("cortex-builder", 977),),
    )
    monkeypatch.setattr(install_cli, "_host_account_snapshot", lambda: host)
    overlay = {"accounts": {"cortex-builder": {"uid": 1500, "gid": 1501}}}

    plan = install_cli._plan_document(config, bundle, overlay=overlay)

    assert _ids(plan)["cortex-builder"] == (1500, 1501)
    assert plan["account_id_sources"]["cortex-builder"] == {"uid": "overlay", "gid": "overlay"}
    assert plan["account_id_sources"]["cortex-gate"] == {"uid": "allocated", "gid": "allocated"}
    assert validate_apply_plan(plan, confirm_sha256=plan_sha256(plan))


def test_overlay_may_pin_only_the_uid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, bundle = _release_effective(tmp_path)
    monkeypatch.setattr(install_cli, "_host_account_snapshot", lambda: UBUNTU)

    plan = install_cli._plan_document(
        config, bundle, overlay={"accounts": {"cortex-gate": {"uid": 975}}}
    )

    assert _ids(plan)["cortex-gate"] == (975, 975)
    assert plan["account_id_sources"]["cortex-gate"] == {"uid": "overlay", "gid": "allocated"}


def test_an_occupied_overlay_id_still_fails_apply_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, bundle = _release_effective(tmp_path)
    monkeypatch.setattr(install_cli, "_host_account_snapshot", lambda: UBUNTU)
    # overlay 指定 991：Ubuntu 的 systemd-resolve 已占用，照舊在 apply preflight 報錯。
    plan = install_cli._plan_document(
        config, bundle, overlay={"accounts": {"cortex-manager": {"uid": 991, "gid": 991}}}
    )
    assert _ids(plan)["cortex-manager"] == (991, 991)

    with pytest.raises(AccountCollisionError, match="991 is already owned by systemd-resolve"):
        validate_preflight(plan, _facts(plan, UBUNTU))


# ---------------------------------------------------------------------------
# plan 審核摘要
# ---------------------------------------------------------------------------


def test_plan_command_reports_every_account_id_and_its_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _write_bundle(tmp_path)
    config = _release_config(tmp_path, bundle)
    host = _with(UBUNTU, users=(("cortex-manager", 1234, 1235),), groups=(("cortex-manager", 1235),))
    monkeypatch.setattr(install_cli, "_host_account_snapshot", lambda: host)
    overlay = tmp_path / "host-overlay.yaml"
    overlay.write_text(json.dumps({"accounts": {"cortex-gate": {"uid": 1500, "gid": 1500}}}), encoding="utf-8")
    output = tmp_path / "plan.json"

    assert install_cli.main(
        [
            "plan",
            "--config",
            str(config),
            "--host-overlay",
            str(overlay),
            "--bundle",
            str(bundle),
            "--output",
            str(output),
        ]
    ) == 0

    emitted = json.loads(capsys.readouterr().out)
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert emitted["plan_sha256"] == plan_sha256(plan)
    summary = emitted["account_ids"]
    assert set(summary) == set(CORTEX_ACCOUNTS)
    assert summary["cortex-manager"] == {
        "uid": 1234,
        "gid": 1235,
        "uid_source": "existing",
        "gid_source": "existing",
    }
    assert summary["cortex-gate"] == {
        "uid": 1500,
        "gid": 1500,
        "uid_source": "overlay",
        "gid_source": "overlay",
    }
    assert summary["cortex-builder"]["uid_source"] == "allocated"
    assert summary["cortex-builder"]["uid"] == _ids(plan)["cortex-builder"][0]


# ---------------------------------------------------------------------------
# apply preflight 重新確認
# ---------------------------------------------------------------------------


def _facts(plan: dict, host: HostAccounts) -> dict:
    """Preflight facts derived from a passwd/group snapshot, like ``preflight_facts``."""

    desired = {row["name"] for section in ("accounts", "service_accounts") for row in plan[section]}
    group_rows = {name: gid for name, gid in host.groups}
    members_by_gid: dict[int, list[str]] = {}
    for name, gid in host.groups:
        members_by_gid.setdefault(gid, []).append(name)
    primary: dict[int, list[str]] = {}
    for name, _uid, gid in host.users:
        primary.setdefault(gid, []).append(name)
    return {
        "systemd": True,
        "polkit": True,
        "cgroup_v2": True,
        "acl": True,
        "disk_free_bytes": 2 * 1024 * 1024 * 1024,
        "universal_nopasswd": False,
        "in_flight_jobs": 0,
        "services": {
            "cortex-egress-proxy.service": "inactive",
            "cortex-manager.service": "inactive",
            "cortex-monitor.service": "inactive",
        },
        "accounts": {
            name: {
                "name": name,
                "uid": uid,
                "gid": gid,
                "home": next(
                    row["home"]
                    for section in ("accounts", "service_accounts")
                    for row in plan[section]
                    if row["name"] == name
                ),
                "shell": "/usr/sbin/nologin",
                "supplementary_groups": [],
                "password_locked": True,
            }
            for name, uid, gid in host.users
            if name in desired
        },
        "account_uids": {uid: name for name, uid, _gid in host.users},
        "group_gids": {gid: name for name, gid in host.groups},
        "groups": {
            name: {"name": name, "gid": gid, "members": []}
            for name, gid in group_rows.items()
            if name in desired
        },
        "primary_gid_users": {gid: sorted(names) for gid, names in primary.items()},
        "group_names_by_gid": {gid: sorted(names) for gid, names in members_by_gid.items()},
        "paths": {
            step["path"]: {"exists": False, "is_symlink": False}
            for step in plan["apply_order"]
            if isinstance(step.get("path"), str)
        },
    }


def _bound(tmp_path: Path, plan: dict) -> dict:
    tools = [
        {
            "name": name,
            "version": configured["version"],
            "shape": "file",
            "resolved_path": str(tmp_path / f"{name}.locked"),
            "sha256": configured["sha256"],
        }
        for name, configured in plan["toolchain_manifest"].items()
    ]
    repository = tmp_path / "paulsha-cortex.bundle"
    repository.write_bytes(b"exact source bundle\n")
    return bind_bundle_artifacts(
        plan,
        {
            "toolchain": tools,
            "source_repositories": [
                {
                    "slug": "paulsha-cortex",
                    "commit": "a" * 40,
                    "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
                    "resolved_path": str(repository),
                    "sha256": hashlib.sha256(repository.read_bytes()).hexdigest(),
                }
            ],
        },
    )


def test_unchanged_snapshot_passes_the_account_preflight(tmp_path: Path) -> None:
    plan = _plan(tmp_path, _without_ids(_safe_config(tmp_path)), UBUNTU)

    report = validate_preflight(plan, _facts(plan, UBUNTU))

    assert report.ok


@pytest.mark.parametrize(
    ("users", "groups", "detail"),
    [
        ((("intruder", 988, 50),), (), "uid 988"),
        ((), (("late-group", 988),), "gid 988"),
        ((("late-primary", 4000, 988),), (), "gid 988"),
    ],
)
def test_ids_taken_between_plan_and_apply_fail_closed_before_any_mutation(
    tmp_path: Path, users, groups, detail: str
) -> None:
    plan = _bound(tmp_path, _plan(tmp_path, _without_ids(_safe_config(tmp_path)), UBUNTU))
    assert _ids(plan)["cortex-builder"] == (988, 988)
    later = _with(UBUNTU, users=users, groups=groups)

    class PreflightOnlyBackend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def preflight_facts(self, candidate):
            self.calls.append("preflight_facts")
            return _facts(candidate, later)

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            self.calls.append(name)
            raise AttributeError(name)

    backend = PreflightOnlyBackend()
    receipt = new_install_receipt(plan)

    with pytest.raises(AccountCollisionError) as raised:
        apply_plan(plan, confirm_sha256=plan_sha256(plan), receipt=receipt, backend=backend)

    message = str(raised.value)
    assert "cortex-builder" in message and detail in message
    assert "generate a new plan" in message
    assert backend.calls == ["preflight_facts"]
    assert receipt.to_dict()["state"] != "applying"


def test_a_reused_account_that_disappeared_fails_closed(tmp_path: Path) -> None:
    host = _with(
        UBUNTU,
        users=(("cortex-manager", 1234, 1235),),
        groups=(("cortex-manager", 1235),),
    )
    plan = _plan(tmp_path, _without_ids(_safe_config(tmp_path)), host)

    with pytest.raises(AccountCollisionError, match="cortex-manager.*generate a new plan"):
        validate_preflight(plan, _facts(plan, UBUNTU))


def test_a_reused_account_whose_ids_changed_fails_closed(tmp_path: Path) -> None:
    host = _with(
        UBUNTU,
        users=(("cortex-manager", 1234, 1235),),
        groups=(("cortex-manager", 1235),),
    )
    plan = _plan(tmp_path, _without_ids(_safe_config(tmp_path)), host)
    later = _with(
        UBUNTU,
        users=(("cortex-manager", 1300, 1235),),
        groups=(("cortex-manager", 1235),),
    )

    with pytest.raises(AccountCollisionError, match="cortex-manager.*generate a new plan"):
        validate_preflight(plan, _facts(plan, later))


def test_allocated_ids_now_held_by_the_same_name_account_defer_to_provenance(
    tmp_path: Path,
) -> None:
    # 首次 apply 已建好帳號、再 apply 同一份 plan（重跑／rollback 後重裝）：號碼屬於同名
    # 帳號不是撞號，交給既有的 receipt provenance 判斷，而不是要求重新產生 plan。
    plan = _plan(tmp_path, _without_ids(_safe_config(tmp_path)), UBUNTU)
    created = _with(
        UBUNTU,
        users=tuple((name, uid, gid) for name, (uid, gid) in _ids(plan).items()),
        groups=tuple((name, gid) for name, (_uid, gid) in _ids(plan).items()),
    )

    with pytest.raises(AccountCollisionError) as raised:
        validate_preflight(plan, _facts(plan, created))

    assert "lacks trusted prior receipt provenance" in str(raised.value)
    assert "generate a new plan" not in str(raised.value)


# ---------------------------------------------------------------------------
# 升級：沒有 overlay 檔也得出與 prior receipt 相同的帳號 step
# ---------------------------------------------------------------------------


def test_upgrade_plan_without_overlay_reproduces_v0113_account_steps(tmp_path: Path) -> None:
    # `_safe_config` 就是 v0.1.13 release config 的形狀：五個帳號寫死 991–995。
    prior = _plan(tmp_path, _safe_config(tmp_path), None)
    assert _ids(prior)["cortex-manager"] == (991, 991)
    # v0.1.13 的 plan／receipt 沒有 account_id_sources，照樣讀得進來。
    prior.pop("account_id_sources")
    prior["receipt_path"] = str(install_core.canonical_receipt_path(prior))
    bound_prior = _bound(tmp_path, prior)
    assert validate_apply_plan(bound_prior, confirm_sha256=plan_sha256(bound_prior))
    installed = HostAccounts(
        users=(("root", 0, 0),)
        + tuple((name, uid, gid) for name, (uid, gid) in _ids(prior).items()),
        groups=(("root", 0),)
        + tuple((name, gid) for name, (_uid, gid) in _ids(prior).items()),
    )

    upgrade = _plan(tmp_path, _without_ids(_safe_config(tmp_path)), installed)

    assert _account_steps(upgrade) == _account_steps(prior)
    assert all(
        row == {"uid": "existing", "gid": "existing"}
        for row in upgrade["account_id_sources"].values()
    )


def test_adopted_host_upgrade_without_the_overlay_file_keeps_the_account_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, bundle = _release_effective(tmp_path)
    legacy_ids = {
        "cortex-manager": (1999, 1987),
        "cortex-reviewer-planner": (1997, 1986),
        "cortex-builder": (1995, 1985),
        "cortex-gate": (1994, 1984),
        "cortex-egress": (1993, 1983),
    }
    overlay = {
        "accounts": {
            name: {"uid": uid, "gid": gid}
            for name, (uid, gid) in legacy_ids.items()
            if name != "cortex-egress"
        },
        "service_accounts": {"cortex-egress": {"uid": 1993, "gid": 1983}},
    }
    host = _with(
        UBUNTU,
        users=tuple((name, uid, gid) for name, (uid, gid) in legacy_ids.items()),
        groups=tuple((name, gid) for name, (_uid, gid) in legacy_ids.items()),
    )
    monkeypatch.setattr(install_cli, "_host_account_snapshot", lambda: host)
    adopted = install_cli._plan_document(config, bundle, overlay=overlay)

    upgraded = install_cli._plan_document(config, bundle, overlay=None)

    assert _account_steps(upgraded) == _account_steps(adopted)
    assert _ids(upgraded) == legacy_ids
    assert all(
        row == {"uid": "existing", "gid": "existing"}
        for row in upgraded["account_id_sources"].values()
    )


# ---------------------------------------------------------------------------
# plan schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda sources: sources.update({"cortex-builder": {"uid": "guessed", "gid": "allocated"}}),
        lambda sources: sources.pop("cortex-gate"),
        lambda sources: sources["cortex-manager"].pop("gid"),
        lambda sources: sources.update({"intruder": {"uid": "allocated", "gid": "allocated"}}),
    ],
)
def test_apply_rejects_a_malformed_account_id_source_record(tmp_path: Path, mutate) -> None:
    plan = _bound(tmp_path, _plan(tmp_path, _without_ids(_safe_config(tmp_path)), UBUNTU))
    forged = deepcopy(plan)
    mutate(forged["account_id_sources"])
    forged["receipt_path"] = str(install_core.canonical_receipt_path(forged))

    with pytest.raises(InstallPlanError, match="account_id_sources"):
        validate_apply_plan(forged, confirm_sha256=plan_sha256(forged))


def test_an_existing_root_owned_name_is_refused_at_plan_time(tmp_path: Path) -> None:
    host = _with(UBUNTU, users=(("cortex-gate", 0, 0),))

    with pytest.raises(InstallPlanError, match="cortex-gate has an invalid uid"):
        _plan(tmp_path, _without_ids(_safe_config(tmp_path)), host)


def test_two_accounts_reusing_one_existing_uid_are_refused(tmp_path: Path) -> None:
    host = _with(
        UBUNTU,
        users=(("cortex-builder", 977, 977), ("cortex-gate", 977, 976)),
        groups=(("cortex-builder", 977), ("cortex-gate", 976)),
    )

    with pytest.raises(InstallPlanError, match="duplicate uid"):
        _plan(tmp_path, _without_ids(_safe_config(tmp_path)), host)


# ---------------------------------------------------------------------------
# runbook
# ---------------------------------------------------------------------------


def test_transactional_install_runbook_documents_the_id_source_rules() -> None:
    runbook = (
        REPO_ROOT / "docs/superpowers/runbooks/trust-root-transactional-install.md"
    ).read_text(encoding="utf-8")
    plan_section = runbook.split("## 2. 產生並三方確認 plan", 1)[1].split("## 3.", 1)[0]
    rules = plan_section[plan_section.index("### 帳號 uid／gid 的來源（#1286）") :]

    for token in ("`overlay`", "`config`", "`existing`", "`allocated`", "990–999", "100–989"):
        assert token in rules, token
    assert "只在要**指定**號碼時才需要" in rules
    assert "重新產生 plan" in rules
    assert "`account_ids`" in plan_section


def test_legacy_adoption_runbook_keeps_overlay_ids_without_remap() -> None:
    runbook = (
        REPO_ROOT / "docs/superpowers/runbooks/trust-root-legacy-adoption.md"
    ).read_text(encoding="utf-8")

    assert "**不 remap uid／gid**" in runbook
    assert "#1286" in runbook
    assert "沒有 overlay 檔也得出與 prior receipt 相同的帳號 step" in runbook
