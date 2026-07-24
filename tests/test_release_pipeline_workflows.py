from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SHA_PIN_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


def _load_workflow(name: str) -> dict:
    path = WORKFLOWS / name
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} must exist"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"{path.relative_to(REPO_ROOT)} must parse to a mapping"
    return payload


def _workflow_on(payload: dict) -> dict:
    on_block = payload.get("on", payload.get(True))
    assert isinstance(on_block, dict), "workflow `on` must be a mapping"
    return on_block


def _job_step_runs(job: dict) -> str:
    return "\n".join(
        step.get("run", "")
        for step in job.get("steps", [])
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    )


def _assert_all_uses_are_sha_pinned(payload: dict, *, relpath: str) -> None:
    for job_name, job in payload.get("jobs", {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps", []):
            if not isinstance(step, dict) or "uses" not in step:
                continue
            uses = step["uses"]
            assert isinstance(uses, str), f"{relpath}:{job_name} step uses must be a string"
            assert SHA_PIN_RE.match(uses), f"{relpath}:{job_name} step `{uses}` must use a 40-hex SHA pin"


def test_tests_workflow_matches_release_pipeline_contract() -> None:
    payload = _load_workflow("tests.yml")
    jobs = payload.get("jobs", {})
    assert isinstance(jobs, dict), "tests.yml jobs must be a mapping"

    pytest_job = jobs.get("pytest")
    assert isinstance(pytest_job, dict), "tests.yml must keep the pytest job"

    strategy = pytest_job.get("strategy")
    assert isinstance(strategy, dict), "pytest job must define a Python matrix strategy"
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), "pytest job matrix must be a mapping"
    assert matrix.get("python-version") == ["3.10", "3.11", "3.12", "3.13"]

    build_job = jobs.get("build")
    assert isinstance(build_job, dict), "tests.yml must add a build job"
    build_runs = _job_step_runs(build_job)
    assert "python -m build" in build_runs
    assert "twine check --strict" in build_runs

    smoke_job = jobs.get("smoke-install")
    assert isinstance(smoke_job, dict), "tests.yml must add a smoke-install job"
    assert smoke_job.get("needs") == "build" or smoke_job.get("needs") == ["build"]
    smoke_runs = _job_step_runs(smoke_job)
    assert "pip install" in smoke_runs
    assert "cortex --version" in smoke_runs
    assert "cortex --help" in smoke_runs

    _assert_all_uses_are_sha_pinned(payload, relpath=".github/workflows/tests.yml")


def test_release_workflow_is_tag_only_no_pypi_and_sha_pinned() -> None:
    payload = _load_workflow("release.yml")
    on_block = _workflow_on(payload)
    push = on_block.get("push")
    assert isinstance(push, dict), "release.yml must trigger from push"
    assert push.get("tags") == ["v*"]
    assert "pull_request" not in on_block, "release.yml must not trigger from pull_request"

    raw = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "policy_version" not in raw
    lowered = raw.lower()
    assert "pypi" not in lowered
    assert "twine upload" not in lowered

    jobs = payload.get("jobs", {})
    assert isinstance(jobs, dict) and jobs, "release.yml must define jobs"
    _assert_all_uses_are_sha_pinned(payload, relpath=".github/workflows/release.yml")
