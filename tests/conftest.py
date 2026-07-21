from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests hermetic against operator shell/runtime bootstrap variables."""
    for name in tuple(os.environ):
        if name.startswith("PSC_") or name == "PAULSHACLAW_CONFIG":
            monkeypatch.delenv(name, raising=False)
