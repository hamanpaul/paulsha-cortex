from __future__ import annotations

from dataclasses import dataclass
import importlib
from typing import Callable, Sequence


@dataclass(frozen=True)
class PorcelainCommand:
    name: str
    help: str
    run: Callable[[Sequence[str]], int]


COMMANDS: dict[str, PorcelainCommand] = {}
_FAMILY_MODULES: tuple[str, ...] = ()
_LOADED_MODULES: set[str] = set()


def register(command: PorcelainCommand) -> None:
    if command.name in COMMANDS:
        raise ValueError(f"porcelain command already registered: {command.name}")
    COMMANDS[command.name] = command


def load_commands() -> None:
    for module_name in _FAMILY_MODULES:
        if module_name in _LOADED_MODULES:
            continue
        module = importlib.import_module(module_name)
        register_commands = getattr(module, "register_commands")
        register_commands()
        _LOADED_MODULES.add(module_name)
