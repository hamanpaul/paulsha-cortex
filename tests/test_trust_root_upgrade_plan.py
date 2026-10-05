"""#1263：plan 由封存的 candidate CLI 以非 root 產生，工具自己綁定 plan sha。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

import pytest

import upgrade_fixtures as fx
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import upgrade
from paulsha_cortex.trust_root.install.core import plan_sha256
from paulsha_cortex.trust_root.install.legacy import host_overlay_record

OVERLAY = {"operator_account": "cortex-ops", "providers": {"builder": ["codex"]}}


@pytest.fixture
def planning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, object]]:
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(upgrade, "_OWNER_UID", os.getuid())
    monkeypatch.setattr(upgrade, "_chown", lambda *_args: None)
    handoffs: list[tuple[object, object]] = []
    monkeypatch.setattr(
        upgrade,
        "validate_prior_receipt_handoff",
        lambda plan, prior: handoffs.append((plan, prior)),
    )
    return handoffs


def _overlay(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / "installer" / "host-overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o644)
    return path


def _new_plan(tmp_path: Path, *, overlay_sha: str | None = None) -> dict[str, object]:
    return fx.plan_document(
        tmp_path,
        version="0.1.13",
        wheel_sha256=fx.NEW_WHEEL,
        commit=fx.NEW_COMMIT,
        overlay_sha=overlay_sha,
    )


def test_plan_runs_the_sealed_cli_unprivileged_and_binds_its_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning
) -> None:
    _overlay(tmp_path, {**OVERLAY, "legacy_adoption": {"inventory_sha256": "d" * 64}})
    overlay_sha = host_overlay_record(OVERLAY)["sha256"]
    sealed = fx.make_sealed(tmp_path)
    prior = fx.make_prior(tmp_path, overlay_sha=overlay_sha)
    plan = _new_plan(tmp_path, overlay_sha=overlay_sha)
    cli = fx.FakePlanCli(plan)
    monkeypatch.setattr(upgrade, "_run", cli)
    monkeypatch.setattr(
        upgrade, "_lookup_account", lambda name: {"cortex-ops": fx.account(4242, 4243)}[name]
    )

    bound = upgrade.produce_plan(sealed, prior, options=upgrade.UpgradeOptions(version="0.1.13"))

    call = cli.calls[0]
    assert (call["uid"], call["gid"]) == (4242, 4243)
    assert call["argv"][:4] == (str(sealed.cli), "install", "trust-root", "plan")
    assert set(call["env"]) == {"HOME", "PATH", "LANG", "LC_ALL", "PYTHONNOUSERSITE"}
    assert call["env"]["HOME"] == str(sealed.attempt_dir / "plan" / "home")
    overlay_arg = Path(call["argv"][call["argv"].index("--host-overlay") + 1])
    assert "legacy_adoption" not in json.loads(overlay_arg.read_text())
    assert bound.sha256 == plan_sha256(plan)
    assert bound.overlay_sha256 == overlay_sha
    assert bound.durable_path == tmp_path / "installer" / "plans" / f"{bound.sha256}.json"
    assert bound.durable_path.read_bytes() == install_core.canonical_plan_bytes(plan)
    assert stat.S_IMODE(bound.durable_path.stat().st_mode) == 0o600
    canonical = Path(str(plan["receipt_path"]))
    assert bound.receipt_path.parent == canonical.parent
    assert re.fullmatch(rf"{canonical.stem}\.run-[0-9a-f]{{32}}\.json", bound.receipt_path.name)
    assert planning == [(bound.plan, prior.receipt)]


def test_plan_refuses_a_reported_sha_that_is_not_the_file_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning
) -> None:
    sealed = fx.make_sealed(tmp_path)
    monkeypatch.setattr(upgrade, "_run", fx.FakePlanCli(_new_plan(tmp_path), reported_sha="0" * 64))
    monkeypatch.setattr(upgrade, "_lookup_account", lambda name: fx.account(65534, 65534))

    with pytest.raises(upgrade.UpgradeError, match="does not match the plan file"):
        upgrade.produce_plan(
            sealed, fx.make_prior(tmp_path), options=upgrade.UpgradeOptions(version="0.1.13")
        )
    assert not (tmp_path / "installer" / "plans").exists()


def test_a_changed_host_overlay_is_refused_outside_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning
) -> None:
    _overlay(tmp_path, OVERLAY)
    overlay_sha = host_overlay_record(OVERLAY)["sha256"]
    sealed = fx.make_sealed(tmp_path)
    prior = fx.make_prior(tmp_path)
    cli = fx.FakePlanCli(_new_plan(tmp_path, overlay_sha=overlay_sha))
    monkeypatch.setattr(upgrade, "_run", cli)
    monkeypatch.setattr(upgrade, "_lookup_account", lambda name: fx.account(4242, 4243))

    with pytest.raises(upgrade.UpgradeError, match="host overlay differs"):
        upgrade.produce_plan(sealed, prior, options=upgrade.UpgradeOptions(version="0.1.13"))
    assert cli.calls == []

    bound = upgrade.produce_plan(
        sealed,
        prior,
        options=upgrade.UpgradeOptions(version="0.1.13", allow_same_version=True),
    )
    assert bound.overlay_sha256 == overlay_sha


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("candidate_sha", "f" * 40, "not the release tag target"),
        ("wheel_sha256", "9" * 64, "not the release wheel"),
    ],
)
def test_plan_must_name_the_release_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning, field, value, message
) -> None:
    plan = _new_plan(tmp_path)
    if field == "candidate_sha":
        plan["candidate"]["candidate_sha"] = value  # type: ignore[index]
    else:
        plan["candidate"]["wheel"]["sha256"] = value  # type: ignore[index]
    plan["receipt_path"] = str(install_core.canonical_receipt_path(plan))
    monkeypatch.setattr(upgrade, "_run", fx.FakePlanCli(plan))
    monkeypatch.setattr(upgrade, "_lookup_account", lambda name: fx.account(65534, 65534))

    with pytest.raises(upgrade.UpgradeError, match=message):
        upgrade.produce_plan(
            fx.make_sealed(tmp_path),
            fx.make_prior(tmp_path),
            options=upgrade.UpgradeOptions(version="0.1.13"),
        )
    assert not (tmp_path / "installer" / "plans").exists()


@pytest.mark.parametrize("overlay", [None, {"operator_account": "root"}])
def test_plan_never_runs_as_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning, overlay
) -> None:
    if overlay is not None:
        _overlay(tmp_path, overlay)
    overlay_sha = host_overlay_record(overlay)["sha256"] if overlay is not None else None
    accounts = {"root": fx.account(0, 0), "nobody": fx.account(65534, 65534)}
    monkeypatch.setattr(upgrade, "_lookup_account", lambda name: accounts[name])
    cli = fx.FakePlanCli(_new_plan(tmp_path, overlay_sha=overlay_sha))
    monkeypatch.setattr(upgrade, "_run", cli)

    upgrade.produce_plan(
        fx.make_sealed(tmp_path),
        fx.make_prior(tmp_path, overlay_sha=overlay_sha),
        options=upgrade.UpgradeOptions(version="0.1.13"),
    )

    assert (cli.calls[0]["uid"], cli.calls[0]["gid"]) == (65534, 65534)


def test_a_named_operator_account_missing_on_the_host_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planning
) -> None:
    _overlay(tmp_path, OVERLAY)

    def missing(name: str):
        raise KeyError(name)

    monkeypatch.setattr(upgrade, "_lookup_account", missing)

    with pytest.raises(upgrade.UpgradeError, match="operator_account does not exist"):
        upgrade.produce_plan(
            fx.make_sealed(tmp_path),
            fx.make_prior(tmp_path, overlay_sha=host_overlay_record(OVERLAY)["sha256"]),
            options=upgrade.UpgradeOptions(version="0.1.13"),
        )


def test_host_overlay_must_be_a_private_regular_file(tmp_path: Path, planning) -> None:
    path = _overlay(tmp_path, OVERLAY)
    path.chmod(0o666)
    with pytest.raises(upgrade.UpgradeError, match="without group/other write"):
        upgrade.read_host_overlay(tmp_path / "installer")

    path.unlink()
    real = tmp_path / "elsewhere.yaml"
    real.write_text("{}", encoding="utf-8")
    path.symlink_to(real)
    with pytest.raises(upgrade.UpgradeError, match="regular file"):
        upgrade.read_host_overlay(tmp_path / "installer")


def test_missing_host_overlay_means_no_overlay(tmp_path: Path, planning) -> None:
    assert upgrade.read_host_overlay(tmp_path / "installer") is None


def test_durable_plan_publication_reuses_identical_bytes_and_never_overwrites(
    tmp_path: Path, planning
) -> None:
    plans = tmp_path / "installer" / "plans"
    (tmp_path / "installer").mkdir()
    payload = b'{"plan": 1}\n'
    sha = hashlib.sha256(payload).hexdigest()

    first = upgrade.publish_durable_plan(payload, sha, plans_root=plans)
    assert upgrade.publish_durable_plan(payload, sha, plans_root=plans) == first

    first.write_bytes(b'{"plan": 2}\n')
    with pytest.raises(upgrade.UpgradeError, match="does not match reviewed bytes"):
        upgrade.publish_durable_plan(payload, sha, plans_root=plans)
    with pytest.raises(upgrade.UpgradeError, match="changed before durable publication"):
        upgrade.publish_durable_plan(b"other", sha, plans_root=plans)


def test_plan_refuses_a_replay_of_the_same_plan_via_the_real_handoff_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling 1: drive the *real* `core.validate_prior_receipt_handoff`, not the
    `planning` fixture's recorder.  A new plan byte-identical to the prior's
    (same version, same candidate, no overlay) is the rejection it actually
    makes: "prior receipt must identify a different plan; replay the current
    receipt instead".  No durable plan may be published when that fires.
    """

    monkeypatch.setattr(install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "installer")
    monkeypatch.setattr(upgrade, "_OWNER_UID", os.getuid())
    monkeypatch.setattr(upgrade, "_chown", lambda *_args: None)

    prior = fx.make_prior(tmp_path, version="0.1.12")
    sealed = fx.make_sealed(
        tmp_path, version="0.1.12", commit=fx.PRIOR_COMMIT, wheel_sha256=fx.PRIOR_WHEEL
    )
    # Byte-identical to the plan `fx.make_prior` embedded in the prior receipt.
    replay = fx.plan_document(
        tmp_path, version="0.1.12", wheel_sha256=fx.PRIOR_WHEEL, commit=fx.PRIOR_COMMIT
    )
    assert plan_sha256(replay) == prior.document["plan_sha256"]
    monkeypatch.setattr(upgrade, "_run", fx.FakePlanCli(replay))
    monkeypatch.setattr(upgrade, "_lookup_account", lambda name: fx.account(os.getuid(), os.getgid()))

    with pytest.raises(
        install_core.InstallError, match="must identify a different plan"
    ):
        upgrade.produce_plan(sealed, prior, options=upgrade.UpgradeOptions(version="0.1.12"))
    assert not (tmp_path / "installer" / "plans").exists()
