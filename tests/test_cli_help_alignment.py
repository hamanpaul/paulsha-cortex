from __future__ import annotations

import pytest

from paulsha_cortex import cli as umbrella_cli
from paulsha_cortex.coordinator.cli import _build_parser as build_coordinator_parser
from paulsha_cortex.deck.cli import _build_parser as build_deck_parser
from paulsha_cortex.deploy import installer
from paulsha_cortex.monitor.__main__ import build_parser as build_monitor_parser


def test_umbrella_help_lists_public_command_families(capsys) -> None:
    assert umbrella_cli.main(["--help"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "usage: cortex" in captured.out
    assert "install service" in captured.out
    assert "doctor" in captured.out
    assert "deck" in captured.out
    assert "monitor" in captured.out
    assert "tick" in captured.out
    assert "work             透過 Manager 單一 writer" in captured.out
    assert "dispatch         已停用" in captured.out


def test_coordinator_help_uses_cortex_and_describes_disabled_dispatch(capsys) -> None:
    parser = build_coordinator_parser()
    assert parser.prog == "cortex"
    assert "已停用的舊低階入口" in parser.format_help()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["dispatch", "--help"])
    assert exc.value.code == 0
    assert "固定拒絕執行" in capsys.readouterr().out


def test_fanout_help_uses_daemon_default_not_legacy_tmux(capsys) -> None:
    parser = build_coordinator_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["fanout", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "daemon 預設值" in output
    assert "舊 tmux pane" not in output
    assert "旁路 executor approval/sandbox" in output


def test_subcommand_help_uses_installed_cortex_invocations() -> None:
    assert build_deck_parser().prog == "cortex deck"
    assert build_monitor_parser().prog == "cortex monitor"


def test_install_help_explains_enable_start_and_interval_semantics(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        installer.main(["service", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "usage: cortex install service" in output
    assert "PSC_MANAGER_INTERVAL_SECONDS" in output
    assert "被治理的目標 git repo" in output
