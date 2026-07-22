from __future__ import annotations

import importlib
import sys
import types

import pytest


def _import_porcelain():
    sys.modules.pop("paulsha_cortex.porcelain", None)
    return importlib.import_module("paulsha_cortex.porcelain")


def test_register_rejects_duplicate_command_names() -> None:
    porcelain = _import_porcelain()
    porcelain.COMMANDS.clear()

    command = porcelain.PorcelainCommand(name="demo", help="demo command", run=lambda argv: 0)
    porcelain.register(command)

    with pytest.raises(ValueError, match="demo"):
        porcelain.register(command)


def test_load_commands_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    porcelain = _import_porcelain()
    porcelain.COMMANDS.clear()
    seen: list[str] = []

    fake_family = types.ModuleType("test_porcelain_family")

    def register_commands() -> None:
        seen.append("register")
        porcelain.register(
            porcelain.PorcelainCommand(name="demo", help="demo command", run=lambda argv: 0)
        )

    fake_family.register_commands = register_commands
    monkeypatch.setitem(sys.modules, "test_porcelain_family", fake_family)
    monkeypatch.setattr(porcelain, "_FAMILY_MODULES", ("test_porcelain_family",))

    porcelain.load_commands()
    porcelain.load_commands()

    assert seen == ["register"]
    assert list(porcelain.COMMANDS) == ["demo"]


def test_load_commands_fail_open_skips_broken_family_and_keeps_good_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    porcelain = _import_porcelain()
    porcelain.COMMANDS.clear()

    good_family = types.ModuleType("good_porcelain_family")

    def register_commands() -> None:
        porcelain.register(
            porcelain.PorcelainCommand(name="demo", help="demo command", run=lambda argv: 0)
        )

    good_family.register_commands = register_commands
    monkeypatch.setitem(sys.modules, "good_porcelain_family", good_family)
    monkeypatch.setattr(
        porcelain,
        "_FAMILY_MODULES",
        ("missing_porcelain_family", "good_porcelain_family"),
    )

    porcelain.load_commands()

    assert list(porcelain.COMMANDS) == ["demo"]
    assert "missing_porcelain_family" in capsys.readouterr().err


def test_load_commands_fail_open_reports_missing_register_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    porcelain = _import_porcelain()
    porcelain.COMMANDS.clear()

    broken_family = types.ModuleType("broken_porcelain_family")
    broken_family.register_commands = "not-callable"
    monkeypatch.setitem(sys.modules, "broken_porcelain_family", broken_family)
    monkeypatch.setattr(porcelain, "_FAMILY_MODULES", ("broken_porcelain_family",))

    porcelain.load_commands()

    assert porcelain.COMMANDS == {}
    assert "broken_porcelain_family" in capsys.readouterr().err
