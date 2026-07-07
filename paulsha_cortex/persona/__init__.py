"""Stage4 persona contract and guardrail primitives."""

from importlib import import_module

from . import context, contract, gate, guardrail, handoff, render, shadow

__all__ = [
    "contract",
    "guardrail",
    "context",
    "shadow",
    "handoff",
    "render",
    "gate",
    "scope_ci",
]


def __getattr__(name: str):
    if name == "scope_ci":
        return import_module(".scope_ci", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
