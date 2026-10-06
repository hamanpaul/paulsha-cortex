from __future__ import annotations

from . import COMMANDS, PorcelainCommand, register
from ..command_policy_hook import main


def register_commands() -> None:
    if "command-policy-hook" in COMMANDS:
        return
    register(
        PorcelainCommand(
            name="command-policy-hook",
            help="執行 headless executor 的 command-policy PreToolUse hook",
            run=main,
        )
    )
