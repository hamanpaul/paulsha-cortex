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
