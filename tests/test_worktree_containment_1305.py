from __future__ import annotations

import os
import shutil
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.launcher import (
    _bubblewrap_worktree_argv,
    _direct_builder_state_dir,
)
from paulsha_cortex.coordinator.registry import JobRegistry


@pytest.mark.parametrize("executor_name", ("claude", "codex", "copilot", "agy"))
def test_installed_executor_version_runs_under_direct_builder_containment(
    executor_name: str,
    tmp_path: Path,
) -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is not installed")
    executable = shutil.which(executor_name)
    if executable is None:
        pytest.skip(f"{executor_name} is not installed")

    worktree = tmp_path / "candidate"
    (worktree / ".git").mkdir(parents=True)
    env = {
        **os.environ,
        "PSC_REPO_ROOT": str(worktree),
        "PSC_JOB_ID": f"containment-smoke-{executor_name}",
    }
    result = subprocess.run(
        _bubblewrap_worktree_argv([executable, "--version"], str(worktree)),
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    if (
        executor_name == "copilot"
        and result.returncode == 77
        and "copilot is disabled by the owner" in result.stderr
    ):
        pytest.skip("Copilot is explicitly disabled by the local owner wrapper")
    assert result.returncode == 0, result.stderr
    assert (result.stdout or result.stderr).strip()


def test_direct_builder_can_write_its_worktree_but_not_operator_checkout(
    tmp_path: Path,
) -> None:
    assert shutil.which("bwrap") is not None, "direct builder containment requires bubblewrap"
    operator_file = tmp_path / "operator-todo.md"
    operator_file.write_text("operator baseline\n", encoding="utf-8")
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    (worktree / ".git").mkdir()

    candidate_file = worktree / "candidate.txt"
    command = [
        "/bin/sh",
        "-c",
        'printf "candidate\n" > "$1"; printf "changed\n" > "$2"',
        "containment-probe",
        str(candidate_file),
        str(operator_file),
    ]
    result = subprocess.run(
        _bubblewrap_worktree_argv(command, str(worktree)),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert candidate_file.read_text(encoding="utf-8") == "candidate\n"
    assert operator_file.read_text(encoding="utf-8") == "operator baseline\n"
    assert "Read-only file system" in result.stderr or "Permission denied" in result.stderr


def test_linked_builder_can_write_its_gitdir_but_not_git_common_config(
    tmp_path: Path,
) -> None:
    assert shutil.which("bwrap") is not None, "direct builder containment requires bubblewrap"
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "builder@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "Builder Test"], check=True
    )
    (source / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "seed"], check=True)
    worktree = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "-b", "feature/probe", str(worktree)],
        check=True,
        capture_output=True,
    )
    gitdir = Path((worktree / ".git").read_text(encoding="utf-8").strip().split(": ", 1)[1])
    common_config = source / ".git" / "config"
    common_config_before = common_config.read_bytes()
    candidate_file = worktree / "candidate.txt"
    gitdir_probe = gitdir / "containment-probe"
    command = [
        "/bin/sh",
        "-c",
        'printf "candidate\n" > "$1"; printf "linked gitdir\n" > "$2"; printf "changed\n" > "$3"',
        "containment-probe",
        str(candidate_file),
        str(gitdir_probe),
        str(common_config),
    ]
    result = subprocess.run(
        _bubblewrap_worktree_argv(command, str(worktree)),
        check=False,
        capture_output=True,
        text=True,
    )

    try:
        assert result.returncode != 0
        assert candidate_file.read_text(encoding="utf-8") == "candidate\n"
        assert gitdir_probe.read_text(encoding="utf-8") == "linked gitdir\n"
        assert common_config.read_bytes() == common_config_before
        assert "Read-only file system" in result.stderr or "Permission denied" in result.stderr
    finally:
        gitdir_probe.unlink(missing_ok=True)


def test_executor_state_setup_rejects_symlink_before_writing_outside(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "candidate"
    (worktree / ".git").mkdir(parents=True)
    outside = tmp_path / "operator"
    outside.mkdir()
    (worktree / ".git" / "cortex-executor").symlink_to(outside, target_is_directory=True)

    try:
        _direct_builder_state_dir(str(worktree), "job-a")
    except ValueError as exc:
        assert "symlink" in str(exc) or "regular directory" in str(exc)
    else:
        raise AssertionError("symlinked executor state root must be rejected")

    assert list(outside.iterdir()) == []


def test_operator_checkout_drift_is_recorded_on_job_and_workflow(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "operator"
    checkout.mkdir()
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    authority_file = checkout / "todo.md"
    authority_file.write_text("- [ ] keep\n", encoding="utf-8")
    baseline = manager._operator_checkout_snapshot(checkout, ["todo.md"])

    authority_file.write_text("- [x] changed\n", encoding="utf-8")
    (checkout / "unexpected.txt").write_text("outside write\n", encoding="utf-8")
    event = manager._operator_checkout_violation(baseline)

    assert event is not None
    assert event["changed_fields"] == ["git_status", "planning_authority"]
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    job = registry.create_job(
        task="work-item",
        persona="builder",
        branch="feature/work-item",
        pane="",
        worktree=str(checkout),
        workflow_operator_checkout_baseline=baseline,
    )
    registry.update_job(
        job["job_id"], workflow_operator_checkout_violation=event
    )
    persisted = JobRegistry(state_path=tmp_path / "registry.json").get_job(job["job_id"])
    assert persisted["workflow_operator_checkout_violation"] == event

    run = SimpleNamespace(
        run_id="run-1",
        work_id="work-item",
        current_phase="build",
        facets=(),
        needs_human_reason={
            "reason": "operator-checkout-mutated",
            "context": {"job_id": "work-item-1", "changed_fields": "git_status,planning_authority"},
        },
    )

    class _Registry:
        def update_job(self, job_id, *, workflow_operator_checkout_violation):
            assert job_id == "work-item-1"
            job["workflow_operator_checkout_violation"] = workflow_operator_checkout_violation
            return job

        def get_workflow_run(self, run_id):
            assert run_id == run.run_id
            return run

        def _manager_update_workflow_run(self, run_id, *, facets, needs_human_reason):
            assert run_id == run.run_id
            run.facets = facets
            run.needs_human_reason = needs_human_reason.to_dict()

    violation_job = {
        "job_id": "work-item-1",
        "workflow_card": "build",
        "workflow_operator_checkout_baseline": baseline,
    }
    result = manager._record_operator_checkout_violation(_Registry(), run, violation_job)

    assert result == {
        "run_id": "run-1",
        "current_phase": "build",
        "job_id": "work-item-1",
        "reason": "operator-checkout-mutated",
    }
    assert "needs_human" in run.facets
    assert run.needs_human_reason["reason"] == "operator-checkout-mutated"
    context = run.needs_human_reason["context"]
    assert context["job_id"] == "work-item-1"
    assert context["changed_fields"] == "git_status,planning_authority"
    assert context["operator_checkout_git_status_before"] == baseline["git_status_sha256"]
    assert context["operator_checkout_git_status_after"] == event["current"]["git_status_sha256"]
    assert context["operator_checkout_planning_authority_refs"] == "todo.md"
