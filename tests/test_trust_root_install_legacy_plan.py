"""Legacy adoption PR-3: host overlay, legacy_policy, disposition, quarantine steps (#1122).

Planning binds a reviewed legacy inventory: it re-derives the inventory scope
from the same config, overlay and bundle, checks the host binding and the
self digest, and derives one disposition per inventoried object (adopt,
adopt-in-place, quarantine-then-create, quarantine, or planning failure).
Apply accepts such a plan only together with the inventory it binds; the apply
side (PR-4) is covered by ``test_trust_root_install_legacy_adoption.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import legacy
from paulsha_cortex.trust_root.install.core import InstallPlanError

from test_trust_root_install_legacy_inventory import (  # noqa: E402
    LEGACY_IDS,
    MACHINE_ID,
    RELEASE_PLAN_GOLDEN_SHA256,
    FakeLegacyHost,
    _collect,
    _host_config,
    _legacy_overlay,
    _normalized_plan_sha256,
    _release_config,
    _seed_legacy_host,
    _write_bundle,
)


QUARANTINE_ROOT = "host/var/lib/cortex-installer/legacy-quarantine"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _setup(tmp_path: Path, overlay: dict | None = None, **config_options):
    tmp_path.mkdir(parents=True, exist_ok=True)
    overlay = _legacy_overlay(tmp_path) if overlay is None else overlay
    bundle = _write_bundle(tmp_path)
    config = _host_config(tmp_path, bundle, **config_options)
    base = install_cli._plan_document(config, bundle, overlay=overlay)
    seeded = _seed_legacy_host(tmp_path, base)
    return config, bundle, overlay, FakeLegacyHost(base, seeded), seeded, base


def _capture(tmp_path: Path, base: dict, overlay: dict, backend, *, mutate=None) -> Path:
    document = _collect(base, overlay, backend)
    if mutate is not None:
        mutate(document)
        document["inventory_sha256"] = legacy.inventory_stable_sha256(document)
    path = tmp_path / "inventories" / f"{document['inventory_sha256']}.json"
    path.parent.mkdir(exist_ok=True)
    legacy.publish_inventory(path, document)
    return path


def _request(tmp_path: Path, inventory: Path, **extra) -> dict:
    return {
        "inventory_sha256": legacy.LegacyInventory.load(inventory).inventory_sha256,
        "quarantine_root": str(tmp_path / QUARANTINE_ROOT),
        **extra,
    }


def _adopt(
    tmp_path: Path,
    *,
    overlay: dict | None = None,
    host=None,
    capture=None,
    request: dict | None = None,
    machine: str = MACHINE_ID,
):
    config, bundle, overlay, backend, seeded, base = _setup(tmp_path, overlay)
    if host is not None:
        host(seeded, backend, base)
    inventory = _capture(tmp_path, base, overlay, backend, mutate=capture)
    block = _request(tmp_path, inventory, **(request or {}))
    plan = install_cli._plan_document(
        config,
        bundle,
        overlay={**overlay, "legacy_adoption": block},
        legacy_inventory=inventory,
        machine_id=machine,
    )
    return plan, seeded, base


def _failures(tmp_path: Path, **options) -> list[str]:
    with pytest.raises(legacy.LegacyAdoptionPlanError) as caught:
        _adopt(tmp_path, **options)
    return list(caught.value.failures)


def _quarantine(plan: dict) -> dict[str, dict]:
    return {row["path"]: row for row in plan["legacy_adoption"]["quarantine"]}


def _quarantine_steps(plan: dict) -> dict[str, dict]:
    return {
        step["path"]: step
        for step in plan["apply_order"]
        if step["kind"] == "legacy-quarantine"
    }


def _position(plan: dict, step_id: str) -> int:
    return next(
        index for index, step in enumerate(plan["apply_order"]) if step["step_id"] == step_id
    )


def _step(plan: dict, step_id: str) -> dict:
    return plan["apply_order"][_position(plan, step_id)]


def _state(base: dict) -> Path:
    return Path(base["roots"]["state"])


def _deploy(base: dict) -> Path:
    return Path(base["roots"]["deploy"])


def _home(tmp_path: Path, account: str) -> Path:
    return tmp_path / "host/var/lib" / account


def _replace_user(backend, name: str, **changes) -> None:
    backend.users = [
        row
        if row.name != name
        else legacy.PasswdEntry(
            name,
            changes.get("uid", row.uid),
            changes.get("gid", row.gid),
            changes.get("home", row.home),
            changes.get("shell", row.shell),
        )
        for row in backend.users
    ]


def _replace_group(backend, name: str, **changes) -> None:
    backend.groups = [
        row
        if row.name != name
        else legacy.GroupEntry(
            name, changes.get("gid", row.gid), tuple(changes.get("members", row.members))
        )
        for row in backend.groups
    ]


# ---------------------------------------------------------------------------
# host overlay
# ---------------------------------------------------------------------------


def test_release_plan_without_overlay_or_inventory_matches_the_golden(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    config = _release_config(tmp_path, bundle)
    output = tmp_path / "plan.json"

    assert install_cli.main(
        ["plan", "--config", str(config), "--bundle", str(bundle), "--output", str(output)]
    ) == 0

    plan = json.loads(output.read_text(encoding="utf-8"))
    assert "legacy_adoption" not in plan
    assert "host_overlay_sha256" not in plan
    assert not any(step["kind"] == "legacy-quarantine" for step in plan["apply_order"])
    assert _normalized_plan_sha256(plan, tmp_path) == RELEASE_PLAN_GOLDEN_SHA256


def test_plan_applies_the_host_overlay_and_records_its_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _write_bundle(tmp_path)
    config = _host_config(tmp_path, bundle)
    config_path = tmp_path / "install-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    overlay = _legacy_overlay(tmp_path)
    overlay_path = tmp_path / "host-overlay.yaml"
    overlay_path.write_text(json.dumps(overlay), encoding="utf-8")
    output = tmp_path / "plan.json"

    assert install_cli.main(
        [
            "plan",
            "--config",
            str(config_path),
            "--host-overlay",
            str(overlay_path),
            "--bundle",
            str(bundle),
            "--output",
            str(output),
        ]
    ) == 0

    plan = json.loads(output.read_text(encoding="utf-8"))
    emitted = json.loads(capsys.readouterr().out)
    record = legacy.host_overlay_record(overlay)
    assert plan["host_overlay_sha256"] == record["sha256"]
    assert emitted["host_overlay_sha256"] == record["sha256"]
    assert emitted["plan_sha256"] == install_core.plan_sha256(plan)
    assert "legacy_adoption" not in plan
    assert _step(plan, "account:cortex-builder")["uid"] == LEGACY_IDS["cortex-builder"][0]
    assert _step(plan, "account:cortex-egress")["home"] == str(tmp_path / "host/srv/cortex-egress")
    assert plan["operator_account"] == "legacy-operator"
    # The overlay changes the effective config and adds exactly one key.
    merged = install_cli._bound_plan_from_config(legacy.apply_host_overlay(config, overlay), bundle)
    bound = dict(plan)
    bound.pop("host_overlay_sha256")
    bound.pop("receipt_path")
    merged.pop("receipt_path")
    assert bound == merged
    assert plan["receipt_path"] == str(install_core.canonical_receipt_path(plan))


def test_overlay_plan_remains_appliable(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    plan = install_cli._plan_document(
        _host_config(tmp_path, bundle), bundle, overlay=_legacy_overlay(tmp_path)
    )

    steps = install_core.validate_apply_plan(plan, confirm_sha256=install_core.plan_sha256(plan))

    assert any(step["step_id"] == "account:cortex-builder" for step in steps)
    forged = deepcopy(plan)
    forged["host_overlay_sha256"] = "not-a-digest"
    forged["receipt_path"] = str(install_core.canonical_receipt_path(forged))
    with pytest.raises(InstallPlanError, match="host_overlay_sha256"):
        install_core.validate_apply_plan(forged, confirm_sha256=install_core.plan_sha256(forged))


def test_plan_rejects_a_disallowed_overlay_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _write_bundle(tmp_path)
    config_path = tmp_path / "install-config.json"
    config_path.write_text(json.dumps(_host_config(tmp_path, bundle)), encoding="utf-8")
    overlay_path = tmp_path / "host-overlay.yaml"
    overlay_path.write_text(json.dumps({"roots": {"state": "/srv/cortex"}}), encoding="utf-8")
    output = tmp_path / "plan.json"

    assert install_cli.main(
        [
            "plan",
            "--config",
            str(config_path),
            "--host-overlay",
            str(overlay_path),
            "--bundle",
            str(bundle),
            "--output",
            str(output),
        ]
    ) == 1

    assert "roots.state" in capsys.readouterr().err
    assert not output.exists()


# ---------------------------------------------------------------------------
# legacy_policy semantics
# ---------------------------------------------------------------------------


def _write_plan_inputs(tmp_path: Path, config: dict, overlay: dict) -> tuple[Path, Path]:
    config_path = tmp_path / "install-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    overlay_path = tmp_path / "host-overlay.yaml"
    overlay_path.write_text(json.dumps(overlay), encoding="utf-8")
    return config_path, overlay_path


def test_reject_policy_refuses_a_legacy_adoption_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, bundle, overlay, backend, _seeded, base = _setup(tmp_path)
    inventory = _capture(tmp_path, base, overlay, backend)
    config["legacy_policy"] = "reject"
    config_path, overlay_path = _write_plan_inputs(
        tmp_path, config, {**overlay, "legacy_adoption": _request(tmp_path, inventory)}
    )
    output = tmp_path / "plan.json"

    for extra in ([], ["--legacy-inventory", str(inventory)]):
        assert install_cli.main(
            [
                "plan",
                "--config",
                str(config_path),
                "--host-overlay",
                str(overlay_path),
                "--bundle",
                str(bundle),
                "--output",
                str(output),
                *extra,
            ]
        ) == 1
        assert "legacy_policy" in capsys.readouterr().err
        assert not output.exists()

    with pytest.raises(InstallPlanError, match="reject"):
        legacy.legacy_adoption_request(
            config, {"legacy_adoption": _request(tmp_path, inventory)}, inventory_given=True
        )


def test_quarantine_policy_without_a_legacy_block_plans_as_before(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    config = _host_config(tmp_path, bundle)
    overlay = _legacy_overlay(tmp_path)

    assert config["legacy_policy"] == "quarantine"
    assert legacy.legacy_adoption_request(config, overlay, inventory_given=False) is None
    assert legacy.legacy_adoption_request(config, None, inventory_given=False) is None
    plain = install_cli._plan_document(config, bundle)
    assert plain == install_cli._bound_plan_from_config(config, bundle)
    with_overlay = install_cli._plan_document(config, bundle, overlay=overlay)
    assert "legacy_adoption" not in with_overlay
    assert not any(step["kind"] == "legacy-quarantine" for step in with_overlay["apply_order"])


def test_legacy_block_requires_a_legacy_inventory_and_vice_versa(tmp_path: Path) -> None:
    config, bundle, overlay, backend, _seeded, base = _setup(tmp_path)
    inventory = _capture(tmp_path, base, overlay, backend)
    block = _request(tmp_path, inventory)

    with pytest.raises(InstallPlanError, match="--legacy-inventory"):
        install_cli._plan_document(
            config, bundle, overlay={**overlay, "legacy_adoption": block}
        )
    with pytest.raises(InstallPlanError, match="legacy_adoption"):
        install_cli._plan_document(
            config, bundle, overlay=overlay, legacy_inventory=inventory, machine_id=MACHINE_ID
        )
    with pytest.raises(InstallPlanError, match="legacy_adoption"):
        install_cli._plan_document(
            config, bundle, legacy_inventory=inventory, machine_id=MACHINE_ID
        )


@pytest.mark.parametrize(
    ("block", "named"),
    [
        ({"quarantine_root": "/q"}, "inventory_sha256"),
        ({"inventory_sha256": "0" * 64}, "quarantine_root"),
        ({"inventory_sha256": "0" * 64, "quarantine_root": "relative/q"}, "quarantine_root"),
        ({"inventory_sha256": "0" * 64, "quarantine_root": "/q/../etc"}, "quarantine_root"),
        ({"inventory_sha256": "short", "quarantine_root": "/q"}, "inventory_sha256"),
        ({"inventory_sha256": "0" * 64, "quarantine_root": "/q", "extra": 1}, "extra"),
        (
            {
                "inventory_sha256": "0" * 64,
                "quarantine_root": "/q",
                "census_exceptions": [{"path": "/x"}],
            },
            "census_exceptions",
        ),
        (
            {
                "inventory_sha256": "0" * 64,
                "quarantine_root": "/q",
                "quarantine_paths": ["relative"],
            },
            "quarantine_paths",
        ),
    ],
)
def test_legacy_adoption_block_schema(tmp_path: Path, block: dict, named: str) -> None:
    config = _host_config(tmp_path, _write_bundle(tmp_path))
    with pytest.raises(InstallPlanError, match=re.escape(named)):
        legacy.legacy_adoption_request(
            config, {"legacy_adoption": block}, inventory_given=True
        )


# ---------------------------------------------------------------------------
# binding: digest, scope, overlay, host
# ---------------------------------------------------------------------------


def test_legacy_plan_binds_inventory_scope_host_and_overlay(tmp_path: Path) -> None:
    plan, _seeded, base = _adopt(tmp_path)
    inventory = legacy.LegacyInventory.load(
        next((tmp_path / "inventories").iterdir())
    )
    block = plan["legacy_adoption"]

    assert plan["legacy_policy"] == "quarantine"
    assert set(block) == {
        "schema_version",
        "inventory_sha256",
        "scope_sha256",
        "host_binding_sha256",
        "host_overlay_sha256",
        "quarantine_root",
        "adopted",
        "quarantine",
        "census_exceptions",
        "summary",
    }
    assert block["schema_version"] == 1
    assert block["inventory_sha256"] == inventory.inventory_sha256
    assert block["scope_sha256"] == inventory.scope_sha256
    assert block["host_binding_sha256"] == legacy.host_binding_sha256(MACHINE_ID)
    assert block["host_overlay_sha256"] == plan["host_overlay_sha256"]
    assert block["quarantine_root"] == str(tmp_path / QUARANTINE_ROOT)
    # Quarantine steps do not change what the inventory scope covers.
    assert legacy.legacy_scope(plan) == legacy.legacy_scope(base)
    assert legacy.scope_sha256(legacy.legacy_scope(plan)) == inventory.scope_sha256
    # The block is part of the confirmed digest and the receipt identity.
    assert plan["receipt_path"] == str(install_core.canonical_receipt_path(plan))
    assert install_core.plan_sha256(plan) != install_core.plan_sha256(base)
    summary = block["summary"]
    assert set(summary) == {
        "adopt",
        "adopt-in-place",
        "quarantine-then-create",
        "quarantine",
        "create",
        "covered",
    }
    assert summary["adopt"] >= 5 and summary["quarantine"] > 0

    again, _seeded, _base = _adopt(tmp_path / "again")
    assert len(again["apply_order"]) == len(plan["apply_order"])
    assert [row["reason"] for row in again["legacy_adoption"]["quarantine"]] == [
        row["reason"] for row in block["quarantine"]
    ]


def test_legacy_plan_is_deterministic(tmp_path: Path) -> None:
    config, bundle, overlay, backend, _seeded, base = _setup(tmp_path)
    inventory = _capture(tmp_path, base, overlay, backend)
    full = {**overlay, "legacy_adoption": _request(tmp_path, inventory)}

    first = install_cli._plan_document(
        config, bundle, overlay=full, legacy_inventory=inventory, machine_id=MACHINE_ID
    )
    second = install_cli._plan_document(
        config, bundle, overlay=full, legacy_inventory=inventory, machine_id=MACHINE_ID
    )

    assert install_core.plan_sha256(first) == install_core.plan_sha256(second)


def test_legacy_plan_rejects_an_inventory_digest_mismatch(tmp_path: Path) -> None:
    failures = _failures(tmp_path, request={"inventory_sha256": "a" * 64})
    assert any("inventory_sha256" in failure for failure in failures)


def test_legacy_plan_rejects_a_tampered_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, bundle, overlay, backend, _seeded, base = _setup(tmp_path)
    inventory = _capture(tmp_path, base, overlay, backend)
    block = _request(tmp_path, inventory)
    raw = inventory.read_bytes()
    inventory.write_bytes(raw.replace(b'"mode":"0755"', b'"mode":"0777"', 1))
    config_path, overlay_path = _write_plan_inputs(
        tmp_path, config, {**overlay, "legacy_adoption": block}
    )
    monkeypatch.setattr(install_cli, "_host_machine_id", lambda: MACHINE_ID)
    output = tmp_path / "plan.json"

    assert install_cli.main(
        [
            "plan",
            "--config",
            str(config_path),
            "--host-overlay",
            str(overlay_path),
            "--bundle",
            str(bundle),
            "--legacy-inventory",
            str(inventory),
            "--output",
            str(output),
        ]
    ) == 1

    assert "legacy inventory is invalid" in capsys.readouterr().err
    assert not output.exists()


def test_legacy_plan_rejects_an_inventory_captured_for_another_scope(tmp_path: Path) -> None:
    config, bundle, overlay, backend, _seeded, base = _setup(tmp_path)
    inventory = _capture(tmp_path, base, overlay, backend)
    other = _host_config(tmp_path, bundle, state="var/lib/cortex-alt")

    with pytest.raises(legacy.LegacyAdoptionPlanError, match="scope"):
        install_cli._plan_document(
            other,
            bundle,
            overlay={**overlay, "legacy_adoption": _request(tmp_path, inventory)},
            legacy_inventory=inventory,
            machine_id=MACHINE_ID,
        )


def test_legacy_plan_rejects_an_inventory_captured_with_another_overlay(
    tmp_path: Path,
) -> None:
    config, bundle, overlay, backend, _seeded, base = _setup(tmp_path)
    inventory = _capture(tmp_path, base, overlay, backend)
    renumbered = deepcopy(overlay)
    renumbered["accounts"]["cortex-gate"] = {"uid": 1994, "gid": 1984}
    # Account ids are outside the scope digest; the overlay record binds them.
    assert legacy.legacy_scope(install_cli._plan_document(config, bundle, overlay=renumbered)) == (
        legacy.legacy_scope(base)
    )

    with pytest.raises(legacy.LegacyAdoptionPlanError, match="host overlay"):
        install_cli._plan_document(
            config,
            bundle,
            overlay={**renumbered, "legacy_adoption": _request(tmp_path, inventory)},
            legacy_inventory=inventory,
            machine_id=MACHINE_ID,
        )


def test_legacy_plan_rejects_another_host(tmp_path: Path) -> None:
    failures = _failures(tmp_path, machine="f" * 32)
    assert any("host binding" in failure for failure in failures)


# ---------------------------------------------------------------------------
# dispositions: accounts
# ---------------------------------------------------------------------------


def test_accounts_matching_the_overlay_are_adopted(tmp_path: Path) -> None:
    plan, _seeded, _base = _adopt(tmp_path)
    inventory = legacy.LegacyInventory.load(next((tmp_path / "inventories").iterdir()))
    rows = {row["name"]: row for row in inventory.document["accounts"]}
    adopted = plan["legacy_adoption"]["adopted"]

    for name in LEGACY_IDS:
        assert adopted[f"account:{name}"] == legacy.inventory_row_sha256("accounts", rows[name])
        # The account step keeps the host's ids: nothing is remapped.
        assert _step(plan, f"account:{name}")["uid"] == LEGACY_IDS[name][0]


@pytest.mark.parametrize(
    ("mutate", "detail"),
    [
        (lambda backend: _replace_user(backend, "cortex-builder", uid=1995), "uid"),
        (lambda backend: _replace_user(backend, "cortex-builder", gid=1985), "gid"),
        (lambda backend: _replace_user(backend, "cortex-builder", home="/srv/builder"), "home"),
        (lambda backend: _replace_user(backend, "cortex-builder", shell="/bin/bash"), "shell"),
        (lambda backend: _replace_group(backend, "kvm", members=("cortex-builder",)), "supplementary"),
        (
            lambda backend: _replace_group(backend, "cortex-builder", members=("legacy-operator",)),
            "member",
        ),
        (lambda backend: _replace_group(backend, "cortex-builder", gid=1985), "group"),
        (
            lambda backend: setattr(
                backend, "password_locked", lambda name: name != "cortex-builder"
            ),
            "password",
        ),
        (
            lambda backend: backend.users.append(
                legacy.PasswdEntry("squatter", 995, 4343, "/", "/bin/sh")
            ),
            "squatter",
        ),
        (
            lambda backend: backend.groups.append(legacy.GroupEntry("squatters", 985, ())),
            "squatters",
        ),
    ],
)
def test_account_mismatch_or_foreign_holder_fails_planning(
    tmp_path: Path, mutate, detail: str
) -> None:
    failures = _failures(tmp_path, host=lambda _seeded, backend, _base: mutate(backend))

    assert any(
        "account cortex-builder" in failure and detail in failure for failure in failures
    ), failures


def test_release_ids_held_by_other_identities_are_never_remapped(tmp_path: Path) -> None:
    # Planned with the release config's ids, the host's system identities
    # already hold them; planning fails and names every foreign holder.
    failures = _failures(tmp_path, overlay={})
    text = "\n".join(failures)

    assert "account cortex-manager" in text and "systemd-resolve" in text
    assert "account cortex-builder" in text and "kvm" in text
    assert "account cortex-egress" in text


def test_overlay_with_only_a_legacy_block_binds_no_overlay_digest(tmp_path: Path) -> None:
    release_ids = {
        "cortex-manager": (991, 991),
        "cortex-reviewer-planner": (992, 992),
        "cortex-builder": (993, 993),
        "cortex-gate": (994, 994),
        "cortex-egress": (995, 995),
    }

    def release_host(_seeded, backend, _base) -> None:
        homes = {row.name: row.home for row in backend.users}
        backend.users = [
            legacy.PasswdEntry(name, uid, gid, homes[name], "/usr/sbin/nologin")
            for name, (uid, gid) in release_ids.items()
        ] + [row for row in backend.users if row.name == "fixture-owner"]
        backend.groups = [
            legacy.GroupEntry(name, gid, ()) for name, (_uid, gid) in release_ids.items()
        ] + [row for row in backend.groups if row.name == "fixture-owner"]

    plan, _seeded, _base = _adopt(tmp_path, overlay={}, host=release_host)

    assert "host_overlay_sha256" not in plan
    assert plan["legacy_adoption"]["host_overlay_sha256"] is None
    assert _step(plan, "account:cortex-builder")["uid"] == 993
    assert "account:cortex-builder" in plan["legacy_adoption"]["adopted"]
    assert install_core._validate_apply_plan_schema(plan)


def test_absent_account_is_created_unless_its_ids_are_held(tmp_path: Path) -> None:
    def remove_gate(_seeded, backend, _base) -> None:
        backend.users = [row for row in backend.users if row.name != "cortex-gate"]
        backend.groups = [row for row in backend.groups if row.name != "cortex-gate"]

    plan, _seeded, _base = _adopt(tmp_path, host=remove_gate)
    assert "account:cortex-gate" not in plan["legacy_adoption"]["adopted"]
    assert plan["legacy_adoption"]["summary"]["create"] >= 1

    def squat_gate(seeded, backend, base) -> None:
        remove_gate(seeded, backend, base)
        backend.users.append(legacy.PasswdEntry("squatter", 994, 4343, "/", "/bin/sh"))

    failures = _failures(tmp_path / "squatted", host=squat_gate)
    assert any("account cortex-gate" in failure and "squatter" in failure for failure in failures)


# ---------------------------------------------------------------------------
# dispositions: managed paths
# ---------------------------------------------------------------------------


def test_managed_directories_of_the_right_type_are_adopted_in_place(tmp_path: Path) -> None:
    plan, seeded, base = _adopt(tmp_path)
    adopted = plan["legacy_adoption"]["adopted"]
    quarantined = _quarantine(plan)
    state = _state(base)

    for step_id, path in (
        ("asset:runtime-agents-tree", state),
        ("asset:coordinator-root-tree", state / "coordinator"),
        ("asset:commit-spool", seeded["commit_spool"]),
        ("asset:builder-codex-state", _home(tmp_path, "cortex-builder") / ".codex"),
        (f"scaffold:{_deploy(base)}", _deploy(base)),
        (f"scaffold:{_deploy(base)}/toolchain/lib", _deploy(base) / "toolchain/lib"),
    ):
        assert step_id in adopted, step_id
        assert str(path) not in quarantined
    # Existing contents of an adopted directory are adopted with it.
    assert str(seeded["jobs"]) not in quarantined
    # Absent managed directories are simply created.
    assert f"scaffold:{_deploy(base)}/bin" not in adopted


@pytest.mark.parametrize(
    ("relative", "make"),
    [
        ("var/lib/cortex/specs", lambda path: path.write_text("not a directory\n")),
        ("etc/polkit-1/rules.d/49-cortex-downgrade.rules", None),
    ],
)
def test_managed_path_of_the_wrong_type_fails_planning(
    tmp_path: Path, relative: str, make
) -> None:
    def host(_seeded, _backend, _base) -> None:
        path = tmp_path / "host" / relative
        if make is None:
            path.unlink()
            path.mkdir()
        else:
            make(path)

    failures = _failures(tmp_path, host=host)
    assert any(
        str(tmp_path / "host" / relative) in failure and "type" in failure for failure in failures
    ), failures


def test_managed_generated_files_are_quarantined_then_created(tmp_path: Path) -> None:
    plan, seeded, _base = _adopt(tmp_path)
    quarantined = _quarantine(plan)
    steps = _quarantine_steps(plan)

    for name, reason in (
        ("unit", "managed-generated"),
        ("polkit", "managed-generated"),
        # Generated, but credential class: its content is never read.
        ("env", "credential"),
    ):
        row = quarantined[str(seeded[name])]
        assert row["disposition"] == "quarantine-then-create"
        assert row["reason"] == reason
    unit_step = steps[str(seeded["unit"])]
    assert unit_step["expected"]["sha256"] == hashlib.sha256(
        seeded["unit"].read_bytes()
    ).hexdigest()
    # The credential-class env file is bound by metadata only.
    env_step = steps[str(seeded["env"])]
    assert set(env_step["expected"]) == {"type", "uid", "gid", "mode", "dev", "ino"}
    assert env_step["expected"]["ino"] == seeded["env"].lstat().st_ino


def test_matching_managed_symlink_is_adopted_and_a_mismatch_is_quarantined(
    tmp_path: Path,
) -> None:
    link_ids = ("asset:reviewer-planner-agy-state", "asset:reviewer-planner-claude-state")

    def host(_seeded, _backend, base) -> None:
        for step_id in link_ids:
            step = _step(base, step_id)
            target = step["target"] if step_id.endswith("agy-state") else "/elsewhere"
            Path(step["path"]).symlink_to(target)

    def root_owned(document) -> None:
        for row in document["managed_paths"]:
            if row["step_id"] in link_ids:
                row["lstat"]["uid"] = 0
                row["lstat"]["gid"] = 0

    plan, _seeded, base = _adopt(tmp_path, host=host, capture=root_owned)
    agy = _step(base, link_ids[0])["path"]
    claude = _step(base, link_ids[1])["path"]

    assert link_ids[0] in plan["legacy_adoption"]["adopted"]
    assert agy not in _quarantine(plan)
    assert _quarantine(plan)[claude]["disposition"] == "quarantine-then-create"
    assert _quarantine(plan)[claude]["reason"] == "managed-symlink-mismatch"
    assert _quarantine_steps(plan)[claude]["expected"]["link_target"] == "/elsewhere"

    # Owned by someone other than root, even the right target is replaced.
    plan, _seeded, base = _adopt(tmp_path / "unowned", host=host)
    agy = _step(base, link_ids[0])["path"]
    assert link_ids[0] not in plan["legacy_adoption"]["adopted"]
    assert _quarantine(plan)[agy]["reason"] == "managed-symlink-mismatch"


def test_managed_symlink_location_holding_a_directory_fails(tmp_path: Path) -> None:
    def host(_seeded, _backend, base) -> None:
        Path(_step(base, "asset:reviewer-planner-agy-state")["path"]).mkdir()

    failures = _failures(tmp_path, host=host)
    assert any(".gemini" in failure and "type" in failure for failure in failures)


def test_toolchain_bin_is_quarantined_as_one_directory(tmp_path: Path) -> None:
    plan, seeded, base = _adopt(tmp_path)
    toolchain_bin = str(_deploy(base) / "toolchain/bin")
    row = _quarantine(plan)[toolchain_bin]

    assert row["disposition"] == "quarantine-then-create"
    assert row["reason"] == "toolchain-bin"
    # Large binaries and wrapper symlinks move with the directory, unread.
    for name in ("claude", "codex", "copilot"):
        assert str(seeded[name]) in row["covers"]
        assert str(seeded[name]) not in _quarantine(plan)
    assert f"generated:toolchain_wrappers/codex" not in plan["legacy_adoption"]["adopted"]


def test_venv_active_directory_is_quarantined_and_a_symlink_fails(tmp_path: Path) -> None:
    plan, _seeded, base = _adopt(tmp_path)
    active = str(_deploy(base) / "venv")
    row = _quarantine(plan)[active]

    assert row["disposition"] == "quarantine"
    assert row["reason"] == "venv-active-directory"
    quarantine_at = _position(plan, f"legacy-quarantine:{active}")
    assert _position(plan, f"scaffold:{_deploy(base)}") < quarantine_at
    assert quarantine_at < _position(plan, "candidate-venv")

    def symlinked(seeded, _backend, base) -> None:
        venv = _deploy(base) / "venv"
        venv.rename(_deploy(base) / "venv.real")
        venv.symlink_to("venv.real")

    failures = _failures(tmp_path / "symlinked", host=symlinked)
    assert any("venv" in failure and "symlink" in failure for failure in failures)


def test_source_repository_is_quarantined_then_cloned(tmp_path: Path) -> None:
    def host(_seeded, _backend, base) -> None:
        (_state(base) / "repos/paulsha-cortex/.git").mkdir(parents=True)

    plan, _seeded, base = _adopt(tmp_path, host=host)
    repository = str(_state(base) / "repos/paulsha-cortex")
    row = _quarantine(plan)[repository]

    assert row["disposition"] == "quarantine-then-create"
    assert row["reason"] == "source-repository"
    assert "asset:repo-source-tree" in plan["legacy_adoption"]["adopted"]
    at = _position(plan, f"legacy-quarantine:{repository}")
    assert _position(plan, "asset:repo-source-tree") < at < _position(
        plan, "repository:paulsha-cortex"
    )


def test_job_worktree_pools_are_quarantined_then_created(tmp_path: Path) -> None:
    def host(_seeded, _backend, base) -> None:
        (_state(base) / "gate-worktree/slot-1").mkdir(parents=True)

    plan, seeded, base = _adopt(tmp_path, host=host)
    adopted = plan["legacy_adoption"]["adopted"]

    for step_id, relative in (
        ("asset:dispatch-worktree-pool", "worktree"),
        ("asset:gate-worktree-pool", "gate-worktree"),
    ):
        row = _quarantine(plan)[str(_state(base) / relative)]
        assert row["disposition"] == "quarantine-then-create"
        assert row["reason"] == "job-worktree-pool"
        assert step_id not in adopted
    assert str(seeded["worktree_slot"]) in _quarantine(plan)[str(_state(base) / "worktree")]["covers"]


@pytest.mark.parametrize("which", ["toolchain", "venv-slot"])
def test_existing_versioned_install_path_fails_planning(tmp_path: Path, which: str) -> None:
    def host(_seeded, _backend, base) -> None:
        if which == "toolchain":
            (_deploy(base) / "toolchain/lib/agy-1.2.11").mkdir()
        else:
            Path(_step(base, "candidate-venv")["path"]).mkdir(parents=True)

    failures = _failures(tmp_path, host=host)
    fragment = "agy-1.2.11" if which == "toolchain" else "venvs/"
    assert any(fragment in failure and "receipt" in failure for failure in failures)


def test_mountpoint_cannot_be_quarantined(tmp_path: Path) -> None:
    def mounted(document) -> None:
        for row in document["managed_paths"]:
            if row["step_id"] == "asset:dispatch-worktree-pool":
                row["content"] = {"mountpoint": True}

    failures = _failures(tmp_path, capture=mounted)
    assert any("worktree" in failure and "mountpoint" in failure for failure in failures)


# ---------------------------------------------------------------------------
# dispositions: discovered objects, credentials, services
# ---------------------------------------------------------------------------


def test_unmanaged_state_top_level_entries_are_quarantined(tmp_path: Path) -> None:
    plan, seeded, _base = _adopt(tmp_path)
    quarantined = _quarantine(plan)

    for name in ("legacy_imported", "specs", "stray"):
        assert quarantined[str(seeded[name])]["disposition"] == "quarantine"
        assert quarantined[str(seeded[name])]["reason"] == "state-top"
    assert str(seeded["legacy_imported_child"]) not in quarantined


def test_unmanaged_subdirectories_and_residue_in_managed_directories_are_quarantined(
    tmp_path: Path,
) -> None:
    plan, seeded, base = _adopt(
        tmp_path,
        request={
            "census_exceptions": [],
        },
    )
    quarantined = _quarantine(plan)

    assert quarantined[str(seeded["sandboxes"])]["reason"] == "managed-subdir"
    assert quarantined[str(_deploy(base) / "toolchain/lib/codex")]["reason"] == "managed-subdir"
    assert quarantined[str(seeded["residue_tmp"])]["reason"] == "managed-residue"
    assert quarantined[str(seeded["residue_bak"])]["reason"] == "managed-residue"


def test_authority_objects_the_installer_did_not_generate_are_quarantined(
    tmp_path: Path,
) -> None:
    plan, seeded, _base = _adopt(tmp_path)
    quarantined = _quarantine(plan)
    adopted = plan["legacy_adoption"]["adopted"]

    dropin_dir = quarantined[str(seeded["dropin_dir"])]
    assert dropin_dir["disposition"] == "quarantine"
    assert dropin_dir["reason"] == "authority"
    assert str(seeded["dropin"]) in dropin_dir["covers"]
    assert quarantined[str(seeded["env_backup"])]["disposition"] == "quarantine"
    inventory = legacy.LegacyInventory.load(next((tmp_path / "inventories").iterdir()))
    authority_rows = {
        legacy.inventory_row_sha256("discovered", row)
        for row in inventory.document["discovered"]
        if row["class"] == "authority" and row["path"] != str(seeded["wants"])
    }
    assert not authority_rows & set(adopted.values())


def test_superseded_reviewer_settings_are_quarantined(tmp_path: Path) -> None:
    plan, seeded, _base = _adopt(tmp_path)
    row = _quarantine(plan)[str(seeded["agy_settings"])]

    assert row["disposition"] == "quarantine"
    assert row["reason"] == "superseded-by-launcher"


def test_credential_objects_are_quarantined_by_metadata(tmp_path: Path) -> None:
    plan, seeded, _base = _adopt(tmp_path)
    quarantined = _quarantine(plan)
    steps = _quarantine_steps(plan)

    for name in ("builder_auth", "reviewer_auth", "manager_hosts", "oauth", "builder_copilot"):
        row = quarantined[str(seeded[name])]
        assert row["disposition"] == "quarantine"
        assert row["reason"] == "credential"
        assert "sha256" not in steps[str(seeded[name])]["expected"]
    # The agy token moves with the unmanaged directory that holds it.
    token_dir = str(seeded["reviewer_token"].parent)
    assert str(seeded["reviewer_token"]) in quarantined[token_dir]["covers"]
    assert str(seeded["reviewer_token"]) not in quarantined


def test_credential_class_managed_directory_is_quarantined_then_created(
    tmp_path: Path,
) -> None:
    def host(_seeded, _backend, base) -> None:
        (_state(base) / "config/codex-credentials/builder").mkdir(parents=True)

    plan, _seeded, base = _adopt(tmp_path, host=host)
    credentials = str(_state(base) / "config/codex-credentials")
    row = _quarantine(plan)[credentials]

    # A plan-managed state directory, but credential class: never adopted.
    assert row["disposition"] == "quarantine-then-create"
    assert row["reason"] == "credential"
    assert "asset:codex-credential-root" not in plan["legacy_adoption"]["adopted"]
    assert f"scaffold:{_state(base)}/config" in plan["legacy_adoption"]["adopted"]
    assert set(_quarantine_steps(plan)[credentials]["expected"]) == {
        "type",
        "uid",
        "gid",
        "mode",
        "dev",
        "ino",
    }


def test_deploy_backups_are_quarantined_and_unknown_deploy_entries_are_unclassified(
    tmp_path: Path,
) -> None:
    plan, _seeded, base = _adopt(tmp_path)
    quarantined = _quarantine(plan)
    for name in ("venv.bak-20260821", "operator-backups"):
        assert quarantined[str(_deploy(base) / name)]["reason"] == "deploy-backup"

    def mystery(_seeded, _backend, base) -> None:
        (_deploy(base) / "mystery.txt").write_text("?\n", encoding="utf-8")

    failures = _failures(tmp_path / "mystery", host=mystery)
    assert any(
        failure.startswith("unclassified") and "mystery.txt" in failure for failure in failures
    ), failures


def test_unclassified_objects_fail_and_are_all_listed(tmp_path: Path) -> None:
    def host(_seeded, _backend, base) -> None:
        (_home(tmp_path, "cortex-builder") / ".bash_history").write_text("ls\n")
        os.mkfifo(_state(base) / "legacy.fifo")

    failures = _failures(tmp_path, host=host)
    unclassified = [failure for failure in failures if failure.startswith("unclassified")]

    assert any(".bash_history" in failure for failure in unclassified)
    assert any("legacy.fifo" in failure for failure in unclassified)


def test_operator_quarantine_paths_resolve_unclassified_objects(tmp_path: Path) -> None:
    history = _home(tmp_path, "cortex-builder") / ".bash_history"

    def host(_seeded, _backend, _base) -> None:
        history.write_text("ls\n")

    plan, _seeded, _base = _adopt(
        tmp_path, host=host, request={"quarantine_paths": [str(history)]}
    )
    assert _quarantine(plan)[str(history)]["reason"] == "operator"

    failures = _failures(
        tmp_path / "unknown",
        request={"quarantine_paths": [str(tmp_path / "unknown/host/var/lib/cortex/nothing")]},
    )
    assert any("quarantine_paths" in failure and "nothing" in failure for failure in failures)


def test_enablement_links_for_planned_units_are_adopted(tmp_path: Path) -> None:
    plan, seeded, _base = _adopt(tmp_path)
    inventory = legacy.LegacyInventory.load(next((tmp_path / "inventories").iterdir()))
    wants = next(row for row in inventory.document["discovered"] if row["path"] == str(seeded["wants"]))

    assert plan["legacy_adoption"]["adopted"]["systemd:enable:cortex-manager.service"] == (
        legacy.inventory_row_sha256("discovered", wants)
    )
    assert str(seeded["wants"]) not in _quarantine(plan)

    def retarget(seeded, _backend, _base) -> None:
        seeded["wants"].unlink()
        seeded["wants"].symlink_to("/lib/systemd/system/cortex-manager.service")

    plan, seeded, _base = _adopt(tmp_path / "retargeted", host=retarget)
    assert _quarantine(plan)[str(seeded["wants"])]["reason"] == "authority"
    assert "systemd:enable:cortex-manager.service" not in plan["legacy_adoption"]["adopted"]


def test_service_loaded_or_dropped_in_outside_the_quarantine_fails(tmp_path: Path) -> None:
    def host(_seeded, backend, _base) -> None:
        backend.services["cortex-manager.service"]["drop_in_paths"] = [
            "/run/systemd/system/cortex-manager.service.d/override.conf",
            # A distribution-wide drop-in for every service is not cortex state.
            "/usr/lib/systemd/system/service.d/10-timeout-abort.conf",
        ]
        backend.services["cortex-monitor.service"]["fragment_path"] = (
            "/usr/lib/systemd/system/cortex-monitor.service"
        )

    failures = _failures(tmp_path, host=host)
    text = "\n".join(failures)

    assert "cortex-manager.service.d/override.conf" in text
    assert "10-timeout-abort.conf" not in text
    assert "cortex-monitor.service" in text and "/usr/lib/systemd/system" in text


# ---------------------------------------------------------------------------
# census
# ---------------------------------------------------------------------------


def _sandbox_writable(seeded, backend, _base) -> None:
    backend.writable = {
        LEGACY_IDS["cortex-reviewer-planner"][0]: {str(seeded["sandboxes"])}
    }


def test_census_path_outside_declared_assets_needs_an_exception(tmp_path: Path) -> None:
    failures = _failures(tmp_path, host=_sandbox_writable)
    assert any(
        "census" in failure and "cortex-reviewer-planner" in failure and "review-sandboxes" in failure
        for failure in failures
    ), failures

    sandboxes = str(tmp_path / "admitted/host/var/lib/cortex/coordinator/review-sandboxes")
    exception = {"path": sandboxes, "principal": "cortex-reviewer-planner"}
    plan, _seeded, _base = _adopt(
        tmp_path / "admitted", host=_sandbox_writable, request={"census_exceptions": [exception]}
    )
    assert plan["legacy_adoption"]["census_exceptions"] == [exception]

    wrong = {"path": str(tmp_path / "wrong/host/var/lib/cortex/coordinator/review-sandboxes"), "principal": "cortex-gate"}
    failures = _failures(tmp_path / "wrong", host=_sandbox_writable, request={"census_exceptions": [wrong]})
    assert any("census" in failure for failure in failures)


def test_census_exception_principal_must_be_a_job_account(tmp_path: Path) -> None:
    failures = _failures(
        tmp_path,
        request={"census_exceptions": [{"path": "/x", "principal": "cortex-manager"}]},
    )
    assert any("census_exceptions" in failure for failure in failures)


def test_unstable_census_fails_planning(tmp_path: Path) -> None:
    def unstable(seeded, backend, _base) -> None:
        backend.unstable = {LEGACY_IDS["cortex-gate"][0]: {str(seeded["stray"])}}

    failures = _failures(tmp_path, host=unstable)
    assert any("unstable" in failure and "cortex-gate" in failure for failure in failures)


# ---------------------------------------------------------------------------
# the legacy-quarantine step
# ---------------------------------------------------------------------------


def test_quarantine_steps_carry_the_bound_move(tmp_path: Path) -> None:
    plan, seeded, _base = _adopt(tmp_path)
    inventory = legacy.LegacyInventory.load(next((tmp_path / "inventories").iterdir()))
    prefix = inventory.inventory_sha256[:16]
    step = _quarantine_steps(plan)[str(seeded["stray"])]
    row = next(row for row in inventory.document["discovered"] if row["path"] == str(seeded["stray"]))

    assert set(step) == {
        "step_id",
        "kind",
        "path",
        "destination",
        "expected",
        "row_sha256",
        "operations",
        "rollback_policy",
        "desired_sha256",
    }
    assert step["step_id"] == f"legacy-quarantine:{seeded['stray']}"
    assert step["destination"] == (
        f"{tmp_path / QUARANTINE_ROOT}/{prefix}/root{seeded['stray']}"
    )
    assert step["destination"] == legacy.quarantine_destination(
        str(tmp_path / QUARANTINE_ROOT), inventory.inventory_sha256, str(seeded["stray"])
    )
    observed = seeded["stray"].lstat()
    assert step["expected"] == {
        "type": "file",
        "uid": observed.st_uid,
        "gid": observed.st_gid,
        "mode": format(observed.st_mode & 0o7777, "04o"),
        "dev": observed.st_dev,
        "ino": observed.st_ino,
        "sha256": hashlib.sha256(seeded["stray"].read_bytes()).hexdigest(),
    }
    assert step["row_sha256"] == legacy.inventory_row_sha256("discovered", row)
    assert step["row_sha256"] == _quarantine(plan)[str(seeded["stray"])]["row_sha256"]
    assert step["operations"] == ["snapshot", "rename-noreplace"]
    assert step["rollback_policy"] == "restore"
    assert install_core._valid_sha256(step["desired_sha256"])
    # Nested objects move once, with their outermost quarantined ancestor.
    paths = sorted(_quarantine_steps(plan))
    assert not any(
        other.startswith(path + "/") for path in paths for other in paths if other != path
    )


def test_quarantine_steps_follow_their_parent_and_precede_their_path(tmp_path: Path) -> None:
    plan, seeded, base = _adopt(tmp_path)
    order = plan["apply_order"]
    accounts_end = max(i for i, step in enumerate(order) if step["kind"] == "account")
    directories = {
        step["path"]: index
        for index, step in enumerate(order)
        if step["kind"] == "asset" and step["asset_type"] == "directory"
    }

    for index, step in enumerate(order):
        if step["kind"] != "legacy-quarantine":
            continue
        path = step["path"]
        parent = os.path.dirname(path)
        assert index > accounts_end
        if parent in directories:
            assert directories[parent] < index, path
        for later, other in enumerate(order):
            for candidate in (other.get("path"), other.get("active_link")):
                if (
                    other["kind"] != "legacy-quarantine"
                    and isinstance(candidate, str)
                    and (candidate == path or candidate.startswith(path + "/"))
                ):
                    assert index < later, (path, other["step_id"])
    worktree = str(_state(base) / "worktree")
    assert _position(plan, f"legacy-quarantine:{worktree}") < _position(
        plan, "asset:dispatch-worktree-pool"
    )
    assert _position(plan, f"legacy-quarantine:{seeded['unit']}") < _position(
        plan, "generated:units/cortex-manager.service"
    )


def test_topology_rejects_a_misplaced_quarantine_step(tmp_path: Path) -> None:
    plan, _seeded, base = _adopt(tmp_path)
    install_core._assert_managed_parent_topology(plan)
    worktree = str(_state(base) / "worktree")
    step_id = f"legacy-quarantine:{worktree}"

    late = deepcopy(plan)
    step = late["apply_order"].pop(_position(late, step_id))
    late["apply_order"].insert(_position(late, "asset:dispatch-worktree-pool") + 1, step)
    with pytest.raises(InstallPlanError, match="must precede"):
        install_core._assert_managed_parent_topology(late)

    early = deepcopy(plan)
    step = early["apply_order"].pop(_position(early, step_id))
    early["apply_order"].insert(_position(early, "asset:runtime-agents-tree"), step)
    with pytest.raises(InstallPlanError, match="parent"):
        install_core._assert_managed_parent_topology(early)


def test_apply_schema_recognizes_legacy_quarantine_steps(tmp_path: Path) -> None:
    plan, seeded, _base = _adopt(tmp_path)

    steps = install_core._validate_apply_plan_schema(plan)
    assert sum(step["kind"] == "legacy-quarantine" for step in steps) == len(
        plan["legacy_adoption"]["quarantine"]
    )

    def rebound(document: dict) -> dict:
        document["receipt_path"] = str(install_core.canonical_receipt_path(document))
        return document

    moved = deepcopy(plan)
    _quarantine_steps(moved)[str(seeded["stray"])]["destination"] = "/tmp/elsewhere"
    with pytest.raises(InstallPlanError, match="destination"):
        install_core._validate_apply_plan_schema(rebound(moved))

    dropped = deepcopy(plan)
    dropped["apply_order"] = [
        step
        for step in dropped["apply_order"]
        if step.get("path") != str(seeded["stray"]) or step["kind"] != "legacy-quarantine"
    ]
    with pytest.raises(InstallPlanError, match="quarantine"):
        install_core._validate_apply_plan_schema(rebound(dropped))

    forged = deepcopy(plan)
    forged["legacy_adoption"]["adopted"]["asset:forged"] = "0" * 64
    with pytest.raises(InstallPlanError, match="adopted"):
        install_core._validate_apply_plan_schema(rebound(forged))

    orphan = deepcopy(plan)
    del orphan["legacy_adoption"]
    with pytest.raises(InstallPlanError, match="legacy_adoption"):
        install_core._validate_apply_plan_schema(rebound(orphan))

    rejecting = deepcopy(plan)
    rejecting["legacy_policy"] = "reject"
    with pytest.raises(InstallPlanError, match="legacy_policy"):
        install_core._validate_apply_plan_schema(rebound(rejecting))


def test_quarantine_root_must_stay_outside_the_inventoried_scope(tmp_path: Path) -> None:
    inside = str(tmp_path / "host/var/lib/cortex/quarantine")
    failures = _failures(tmp_path, request={"quarantine_root": inside})
    assert any("quarantine_root" in failure for failure in failures)

    around = str(tmp_path / "around/host/var/lib")
    failures = _failures(tmp_path / "around", request={"quarantine_root": around})
    assert any("quarantine_root" in failure for failure in failures)


# ---------------------------------------------------------------------------
# apply accepts a legacy plan only with the inventory it binds (PR-4)
# ---------------------------------------------------------------------------


def test_apply_validates_a_legacy_plan_and_requires_its_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Adopted rows are keyed by step id; a scaffold id carries its path, and
    # here that path contains "auth".  The secret-field screen must still let
    # the plan through validation.
    tmp_path = tmp_path / "auth-bearing"
    plan, _seeded, _base = _adopt(tmp_path)
    digest = install_core.plan_sha256(plan)
    assert any("auth" in step_id for step_id in plan["legacy_adoption"]["adopted"])

    steps = install_core.validate_apply_plan(plan, confirm_sha256=digest)
    assert sum(step["kind"] == "legacy-quarantine" for step in steps) == len(
        plan["legacy_adoption"]["quarantine"]
    )
    smuggled = deepcopy(plan)
    smuggled["legacy_adoption"]["summary"]["github_token"] = 0
    smuggled["receipt_path"] = str(install_core.canonical_receipt_path(smuggled))
    with pytest.raises(InstallPlanError, match="forbidden"):
        install_core.validate_apply_plan(
            smuggled, confirm_sha256=install_core.plan_sha256(smuggled)
        )

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    receipt = tmp_path / "receipts" / "receipt.json"
    assert install_cli.main(
        ["apply", "--plan", str(plan_path), "--confirm-sha256", digest, "--receipt", str(receipt)]
    ) == 1
    assert "requires --legacy-inventory" in capsys.readouterr().err
    assert not receipt.parent.exists()
    assert not Path(plan["receipt_path"]).exists()


def test_cli_plan_binds_a_legacy_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, bundle, overlay, backend, seeded, base = _setup(tmp_path)
    backend.writable = {LEGACY_IDS["cortex-reviewer-planner"][0]: {str(seeded["sandboxes"])}}
    inventory = _capture(tmp_path, base, overlay, backend)
    block = _request(
        tmp_path,
        inventory,
        census_exceptions=[
            {"path": str(seeded["sandboxes"]), "principal": "cortex-reviewer-planner"}
        ],
    )
    config_path, overlay_path = _write_plan_inputs(
        tmp_path, config, {**overlay, "legacy_adoption": block}
    )
    monkeypatch.setattr(install_cli, "_host_machine_id", lambda: MACHINE_ID)
    output = tmp_path / "plan.json"

    assert install_cli.main(
        [
            "plan",
            "--config",
            str(config_path),
            "--host-overlay",
            str(overlay_path),
            "--bundle",
            str(bundle),
            "--legacy-inventory",
            str(inventory),
            "--output",
            str(output),
        ]
    ) == 0

    plan = json.loads(output.read_text(encoding="utf-8"))
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["plan_sha256"] == install_core.plan_sha256(plan)
    assert emitted["legacy_adoption"]["inventory_sha256"] == block["inventory_sha256"]
    assert emitted["legacy_adoption"]["summary"] == plan["legacy_adoption"]["summary"]
    assert emitted["legacy_adoption"]["quarantine"] == len(plan["legacy_adoption"]["quarantine"])
    assert plan["legacy_adoption"]["census_exceptions"] == block["census_exceptions"]
    assert install_core._validate_apply_plan_schema(plan)
