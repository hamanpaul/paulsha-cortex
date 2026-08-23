"""RED contract tests for #716 real agent-loop qualification.

The existing inner-sandbox probe proves a scripted surface. The missing
qualification harness must instead exercise the production-shaped Codex
template-dispatch seam and fail closed on every degraded outcome named by the
accepted plan.
"""

from __future__ import annotations

from paulsha_cortex.trust_root import permgen
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


def test_the_probe_cli_is_wired() -> None:
    assert main(["agent-loop-probe", "four-way"]) == 0
    assert main(["agent-loop-probe", "nonsense"]) == 2
