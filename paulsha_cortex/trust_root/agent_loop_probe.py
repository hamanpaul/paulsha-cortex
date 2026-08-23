"""Runtime executor for the #716 real agent-loop qualification harness."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Callable, Mapping

from paulsha_cortex.coordinator import job_runner, job_workspace
from paulsha_cortex.coordinator.launcher import SubprocessLauncher

from . import permgen

Runner = Callable[..., object]


def _validated_probe_command(command: object) -> list[str]:
    if not isinstance(command, list) or len(command) < 3 or command[:2] != ["bash", "-c"]:
        raise RuntimeError("production launch drift: expected template wrapper command bash -c")
    if not all(isinstance(item, str) for item in command):
        raise RuntimeError("production launch drift: command must stay a string argv list")
    return command


def run_production_agent_loop_probe(
    *,
    job_id: str,
    prompt: str,
    repo_root: str,
    artifact_root: str,
) -> int:
    os.environ[job_runner.JOB_RUNNER_ENV] = job_runner.RUNNER_SYSTEMD_TEMPLATE
    plan = job_runner.prepare_systemd_template(os.environ, job_id=job_id, executor="codex")
    unit_cat = subprocess.run(
        [plan.binary, "cat", plan.unit],
        check=False,
        capture_output=True,
        text=True,
    )
    if unit_cat.returncode != 0:
        detail = unit_cat.stderr.strip() or str(unit_cat.returncode)
        raise RuntimeError(f"unable to read generated unit {plan.unit}: {detail}")
    unit_text = unit_cat.stdout
    unit_props = permgen.unit_replica_properties(unit_text, instance=plan.instance)
    unit_hash = hashlib.sha256(unit_text.encode("utf-8")).hexdigest()

    handle = SubprocessLauncher(executor="codex").launch(
        slice_id=job_id,
        prompt=prompt,
        worktree=repo_root,
        log_dir=artifact_root,
    )
    if handle.template_instance != plan.instance:
        raise RuntimeError(
            f"template instance drift: launch={handle.template_instance!r} plan={plan.instance!r}"
        )
    if handle.control_log_path is None:
        raise RuntimeError("control log path missing for degraded launch")

    spec = json.loads(Path(plan.spec_path).read_text(encoding="utf-8"))
    command = _validated_probe_command(spec.get("command"))
    command_text = command[2]
    for token in ("codex exec", "--json", "--sandbox danger-full-access"):
        if token not in command_text:
            raise RuntimeError(f"production launch drift: wrapper command lost {token!r}")
    for forbidden in (
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "use_legacy_landlock",
    ):
        if forbidden in command_text:
            raise RuntimeError(
                f"no-unsafe-fallback: wrapper command unexpectedly contains {forbidden!r}"
            )
    if spec.get("log_path") != handle.log_path:
        raise RuntimeError(
            f"job log drift: spec={spec.get('log_path')!r} handle={handle.log_path!r}"
        )

    last_message = str(job_workspace.job_last_message_path(handle.log_path))
    main_pid_proc = subprocess.run(
        [plan.binary, "show", "-p", "MainPID", "--value", plan.unit],
        check=False,
        capture_output=True,
        text=True,
    )
    main_pid = main_pid_proc.stdout.strip()
    child_tree = ""
    if main_pid and main_pid != "0":
        child = subprocess.run(
            ["ps", "-o", "pid,ppid,stat,cmd", "--forest", "-p", main_pid],
            check=False,
            capture_output=True,
            text=True,
        )
        child_tree = child.stdout.strip()

    pid, status = os.waitpid(handle.pid, 0)
    if pid != handle.pid:
        raise RuntimeError(f"unexpected waitpid result: {pid!r} != {handle.pid!r}")
    exit_code = os.waitstatus_to_exitcode(status)

    exit_reason = subprocess.run(
        [
            plan.binary,
            "show",
            "-p",
            "Result",
            "-p",
            "ExecMainCode",
            "-p",
            "ExecMainStatus",
            plan.unit,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    job_log = Path(handle.log_path).read_text(encoding="utf-8", errors="replace")
    for forbidden in ("SKIP", "fallback", "quota", "model mismatch"):
        if forbidden in job_log:
            raise RuntimeError(
                f"qualification failed: {forbidden} is forbidden in the job log"
            )

    candidate_sha = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    artifact_hashes: list[str] = []
    for path in (plan.spec_path, handle.log_path, last_message, handle.control_log_path):
        artifact_hashes.append(
            subprocess.run(
                ["sha256sum", path],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )

    print(f"unit\t{plan.unit}")
    print(f"executor/model\tcodex\t{os.environ.get('PSC_MODEL_ID', '<configured>')}")
    print(f"sandbox profile\t{plan.hardening_profile}")
    print(f"candidate SHA\t{candidate_sha}")
    print(f"unit hash\t{unit_hash}")
    print(f"command\t{shlex.join(command)}")
    print(f"egress pair\t{len(unit_props)} properties")
    print("child tree")
    print(child_tree or "<not captured>")
    print("exit reason")
    print(exit_reason.stdout, end="")
    print(f"artifact hash\t{' | '.join(artifact_hashes)}")
    return exit_code


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
