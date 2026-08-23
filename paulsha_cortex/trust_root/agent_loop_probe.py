"""Runtime executor for the #716 real agent-loop qualification harness."""

from __future__ import annotations

import subprocess
from typing import Callable, Mapping

from . import permgen

Runner = Callable[..., object]


def render_agent_loop_probe_script(scheme: permgen.UidScheme) -> str:
    return "set -euo pipefail\n" + "\n".join(permgen.build_agent_loop_probe(scheme)) + "\n"


def run_agent_loop_probe(
    scheme: permgen.UidScheme,
    *,
    runner: Runner | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    actual_runner = subprocess.run if runner is None else runner
    kwargs: dict[str, object] = {
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "text": True,
    }
    if env is not None:
        kwargs["env"] = dict(env)
    raw = actual_runner(
        ["bash", "-c", render_agent_loop_probe_script(scheme)],
        **kwargs,
    )
    returncode = getattr(raw, "returncode", None)
    if not isinstance(returncode, int):
        raise RuntimeError("agent-loop-probe runner returned no integer returncode")
    return returncode
