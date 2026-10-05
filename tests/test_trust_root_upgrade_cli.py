"""#1263：`cortex upgrade` 的 CLI 介面與只限測試參數的閘門。"""
from __future__ import annotations

import pytest

from paulsha_cortex import cli as umbrella_cli
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import upgrade


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[upgrade.UpgradeOptions]:
    seen: list[upgrade.UpgradeOptions] = []

    def run_upgrade(options: upgrade.UpgradeOptions) -> int:
        seen.append(options)
        return 0

    monkeypatch.setattr(upgrade, "run_upgrade", run_upgrade)
    return seen


def test_non_root_is_refused_before_any_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], recorded
) -> None:
    monkeypatch.setattr(install_cli.os, "geteuid", lambda: 1000)

    assert install_cli.main(["upgrade", "0.1.13"]) == 1
    assert install_cli.upgrade_main(["0.1.13"]) == 1

    assert recorded == []
    assert capsys.readouterr().err.count("requires root") == 2


def test_test_only_flags_are_refused_without_the_qualification_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], recorded
) -> None:
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.delenv("PSC_UPGRADE_QUALIFICATION", raising=False)

    for flags in (
        ["--release-source", "/run/release"],
        ["--allow-same-version"],
        ["--prior-receipt", "/run/prior.json"],
    ):
        assert install_cli.upgrade_main(["0.1.13", *flags]) == 1

    assert recorded == []
    assert capsys.readouterr().err.count("PSC_UPGRADE_QUALIFICATION=1") == 3


def test_qualification_options_reach_the_coordinator(
    monkeypatch: pytest.MonkeyPatch, recorded
) -> None:
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setenv("PSC_UPGRADE_QUALIFICATION", "1")

    assert install_cli.main(
        [
            "upgrade",
            "0.1.13",
            "--release-source",
            "/run/release",
            "--allow-same-version",
            "--prior-receipt",
            "/run/prior.json",
            "--wait-idle",
            "30",
            "--json",
        ]
    ) == 0

    options = recorded[0]
    assert options.version == "0.1.13"
    assert options.wait_idle == 30
    assert options.json_output is True
    assert str(options.release_source) == "/run/release"
    assert options.allow_same_version is True
    assert str(options.prior_receipt) == "/run/prior.json"


def test_top_level_cortex_upgrade_is_an_alias(monkeypatch: pytest.MonkeyPatch, recorded) -> None:
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)

    assert umbrella_cli.main(["upgrade", "--status"]) == 0
    assert recorded[0].status is True


def test_recover_and_status_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc:
        install_cli.upgrade_main(["--recover", "--status"])
    assert exc.value.code == 2
