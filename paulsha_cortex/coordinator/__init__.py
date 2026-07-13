"""Stage4 persona Phase 2 minimal coordinator CLI package."""

from . import autonomy, cli, dispatcher, registry, seams, verification

__all__ = [
    "registry",
    "seams",
    "dispatcher",
    "cli",
    "autonomy",
    "verification",
]
