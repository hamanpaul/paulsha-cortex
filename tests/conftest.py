from __future__ import annotations

from pathlib import Path
import os
import shutil

import pytest


@pytest.fixture(autouse=True)
def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests hermetic against operator shell/runtime bootstrap variables."""
    for name in tuple(os.environ):
        if name.startswith("PSC_") or name == "PAULSHACLAW_CONFIG":
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _prefer_local_openspec(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    wrapper = repo_root / "scripts" / "openspec"
    if not wrapper.exists():
        return

    real_openspec = shutil.which("openspec")
    if real_openspec:
        monkeypatch.setenv("PAULSHA_REAL_OPENSPEC", real_openspec)

    original_path = os.environ.get("PATH", "")
    wrapper_parent = str(wrapper.parent.resolve())
    if wrapper_parent not in original_path.split(os.pathsep):
        monkeypatch.setenv("PATH", f"{wrapper_parent}{os.pathsep}{original_path}")
