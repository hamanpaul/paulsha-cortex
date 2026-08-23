"""RED contract tests for #716 real agent-loop qualification.

The existing inner-sandbox probe proves a scripted surface. The missing
qualification harness must instead exercise the production-shaped Codex
template-dispatch seam and fail closed on every degraded outcome named by the
accepted plan.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from paulsha_cortex.trust_root import agent_loop_probe, permgen
from paulsha_cortex.trust_root.__main__ import main


def test_the_probe_uses_real_dispatch_and_fail_closed_qualification_contract() -> None:
    lines = permgen.build_agent_loop_probe(permgen.DEFAULT_SCHEME)
    assert permgen.path_probe_env_injections(lines) == ()
    executable = [
        line for line in lines if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("--property=" in line for line in executable)
    assert not any("--setenv=" in line for line in executable)

    text = "\n".join(lines)
    for token in (
        "build_codex_argv",
        "prepare_systemd_template",
        "build_job_env",
        "build_job_spec",
        "write_job_spec",
        "systemctl start --wait",
        "repository command",
        "child process",
        "forbidden path",
        "forbidden host",
        "no-unsafe-fallback",
        "danger-full-access",
        "--dangerously-bypass-approvals-and-sandbox",
        "executor/model",
        "unit hash",
        "candidate SHA",
        "artifact hash",
        "child tree",
        "exit reason",
        "SKIP",
        "fallback",
        "quota",
        "model mismatch",
    ):
        assert token in text


def test_the_probe_runner_executes_the_generated_harness() -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    env = {"PATH": "/usr/bin", "PSC_REPO_ROOT": "/tmp/repo"}
    assert (
        agent_loop_probe.run_agent_loop_probe(
            permgen.DEFAULT_SCHEME,
            runner=fake_run,
            env=env,
        )
        == 0
    )
    assert captured["argv"][:2] == ["bash", "-c"]
    script = captured["argv"][2]
    assert script.startswith("set -euo pipefail\n# === #716 real agent-loop qualification")
    assert "build_codex_argv" in script
    assert "systemctl start --wait" in script
    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["text"] is True
    assert kwargs["env"] == env


def test_the_probe_cli_is_wired() -> None:
    with patch("paulsha_cortex.trust_root.__main__.run_agent_loop_probe", return_value=0) as mocked:
        assert main(["agent-loop-probe", "four-way"]) == 0
    mocked.assert_called_once_with(permgen.DEFAULT_SCHEME)
    assert main(["agent-loop-probe", "nonsense"]) == 2
