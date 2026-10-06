"""Helpers for isolating child Python processes from inherited runtime paths."""

from __future__ import annotations

import os
from typing import Mapping


PYTHON_PATH_ENVIRONMENT = frozenset(
    {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"}
)


def clean_python_path_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy an environment without Python startup or import-path overrides."""

    source = os.environ if environment is None else environment
    return {
        name: value
        for name, value in source.items()
        if name not in PYTHON_PATH_ENVIRONMENT
    }
