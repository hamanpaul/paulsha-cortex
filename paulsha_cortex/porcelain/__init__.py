from __future__ import annotations

from dataclasses import dataclass
import importlib
import sys
from typing import Callable, Sequence


@dataclass(frozen=True)
class PorcelainCommand:
    name: str
    help: str
    run: Callable[[Sequence[str]], int]


COMMANDS: dict[str, PorcelainCommand] = {}
_FAMILY_MODULES: tuple[str, ...] = ("paulsha_cortex.porcelain.request", "paulsha_cortex.porcelain.inspect", "paulsha_cortex.porcelain.service", "paulsha_cortex.porcelain.bootstrap")
_LOADED_MODULES: set[str] = set()


def register(command: PorcelainCommand) -> None:
    if command.name in COMMANDS:
        raise ValueError(f"porcelain command already registered: {command.name}")
    COMMANDS[command.name] = command


def load_commands() -> None:
    for module_name in _FAMILY_MODULES:
        if module_name in _LOADED_MODULES:
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            sys.stderr.write(
                f"warning: porcelain family {module_name} skipped: import failed ({type(exc).__name__}: {exc})\n"
            )
            continue
        register_commands = getattr(module, "register_commands", None)
        if not callable(register_commands):
            sys.stderr.write(
                f"warning: porcelain family {module_name} skipped: register_commands missing or not callable\n"
            )
            continue
        snapshot = dict(COMMANDS)
        try:
            register_commands()
        except Exception as exc:
            COMMANDS.clear()
            COMMANDS.update(snapshot)
            sys.stderr.write(
                f"warning: porcelain family {module_name} skipped: register_commands failed "
                f"({type(exc).__name__}: {exc})\n"
            )
            continue
        _LOADED_MODULES.add(module_name)
