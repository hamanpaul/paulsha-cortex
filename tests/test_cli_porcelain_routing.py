from __future__ import annotations

import importlib
import sys
import types

import pytest


def _load_cli():
    sys.modules.pop("paulsha_cortex.cli", None)
    return importlib.import_module("paulsha_cortex.cli")


def _install_fake_porcelain(monkeypatch: pytest.MonkeyPatch, *, with_command: bool):
    seen: dict[str, object] = {"load_calls": 0}
    module = types.ModuleType("paulsha_cortex.porcelain")
    module.COMMANDS = {}

    def run(argv):
        seen["argv"] = list(argv)
        return 7

    def load_commands() -> None:
        seen["load_calls"] = int(seen["load_calls"]) + 1
        module.COMMANDS.clear()
        if with_command:
            module.COMMANDS["demo"] = types.SimpleNamespace(
                name="demo",
                help="demo porcelain command",
                run=run,
            )

    module.load_commands = load_commands
    monkeypatch.setitem(sys.modules, "paulsha_cortex.porcelain", module)
    return seen


def test_help_includes_version_and_hides_empty_porcelain_section(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_porcelain(monkeypatch, with_command=False)
    cli = _load_cli()

    assert cli.main(["--help"]) == 0

    captured = capsys.readouterr()
    assert "--version" in captured.out
    assert "porcelain commands:" not in captured.out


def test_help_appends_porcelain_commands_when_registry_is_non_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_porcelain(monkeypatch, with_command=True)
    cli = _load_cli()

    assert cli.main(["--help"]) == 0

    captured = capsys.readouterr()
    assert "porcelain commands:" in captured.out
    assert "demo            demo porcelain command" in captured.out


def test_porcelain_command_routes_before_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_fake_porcelain(monkeypatch, with_command=True)
    cli = _load_cli()

    def fake_coordinator_main(argv=None):
        raise AssertionError(f"coordinator should not handle porcelain argv: {argv}")

    monkeypatch.setattr("paulsha_cortex.coordinator.cli.main", fake_coordinator_main)

    assert cli.main(["demo", "--flag"]) == 7
    assert seen["argv"] == ["--flag"]
    assert seen["load_calls"] == 1
