from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYTHON_VERSIONS = ["3.10", "3.11", "3.12", "3.13"]
SHA_PIN_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
POLICY_ENGINE_PIN = (
    "hamanpaul/paulsha-conventions/.github/workflows/"
    "reusable-policy-check.yml@58290153a400926851afa0f1794236e7669847c6  # v1.0.12"
)


def _read_required(relpath: str) -> str:
    path = REPO_ROOT / relpath
    assert path.is_file(), f"{relpath} is missing"
    return path.read_text(encoding="utf-8")


def _load_workflow(relpath: str) -> tuple[dict[object, object], str]:
    text = _read_required(relpath)
    payload = yaml.safe_load(text) or {}
    assert isinstance(payload, dict), f"{relpath} must decode to a mapping"
    return payload, text


def _workflow_on(payload: dict[object, object]) -> dict[object, object]:
    raw = payload.get("on", payload.get(True))
    assert isinstance(raw, dict), "workflow `on` block must be a mapping"
    return raw


def _job_steps(job: object) -> list[dict[object, object]]:
    assert isinstance(job, dict), "job payload must be a mapping"
    steps = job.get("steps")
    assert isinstance(steps, list), "job.steps must be a list"
    typed: list[dict[object, object]] = []
    for step in steps:
        assert isinstance(step, dict), "each workflow step must be a mapping"
        typed.append(step)
    return typed


def _all_uses(payload: dict[object, object]) -> list[str]:
    jobs = payload.get("jobs")
    assert isinstance(jobs, dict), "workflow jobs must be a mapping"
    uses: list[str] = []
    for job in jobs.values():
        for step in _job_steps(job):
            value = step.get("uses")
            if isinstance(value, str):
                uses.append(value)
    return uses


def _job_run_script(job: dict[object, object]) -> str:
    lines: list[str] = []
    for step in _job_steps(job):
        run = step.get("run")
        if isinstance(run, str):
            lines.append(run)
    return "\n".join(lines)


def _step_uses_prefix(step: dict[object, object], prefix: str) -> bool:
    value = step.get("uses")
    return isinstance(value, str) and value.startswith(prefix)


def test_tests_workflow_expands_release_matrix_and_build_smoke_jobs() -> None:
    payload, _ = _load_workflow(".github/workflows/tests.yml")
    jobs = payload.get("jobs")
    assert isinstance(jobs, dict), "tests.yml jobs must be a mapping"
    assert {"pytest", "build", "smoke-install"} <= set(jobs), (
        "tests.yml must add build and smoke-install jobs alongside pytest"
    )

    pytest_job = jobs["pytest"]
    assert isinstance(pytest_job, dict), "pytest job must be a mapping"
    strategy = pytest_job.get("strategy")
    assert isinstance(strategy, dict), "pytest job must declare strategy matrix"
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), "pytest strategy.matrix must be a mapping"
    assert matrix.get("python-version") == EXPECTED_PYTHON_VERSIONS

    setup_steps = [step for step in _job_steps(pytest_job) if _step_uses_prefix(step, "actions/setup-python@")]
    assert setup_steps, "pytest job must install the matrix-selected Python version"
    setup_with = setup_steps[0].get("with")
    assert isinstance(setup_with, dict), "setup-python step must provide inputs"
    assert setup_with.get("python-version") == "${{ matrix.python-version }}"

    build_job = jobs["build"]
    assert isinstance(build_job, dict), "build job must be a mapping"
    build_run = _job_run_script(build_job)
    assert "python -m build" in build_run
    assert "twine check --strict" in build_run
    assert any(_step_uses_prefix(step, "actions/upload-artifact@") for step in _job_steps(build_job)), (
        "build job must upload wheel/sdist artifacts"
    )

    smoke_job = jobs["smoke-install"]
    assert isinstance(smoke_job, dict), "smoke-install job must be a mapping"
    needs = smoke_job.get("needs")
    normalized_needs = [needs] if isinstance(needs, str) else list(needs or [])
    assert "build" in normalized_needs, "smoke-install must consume build artifacts"
    smoke_run = _job_run_script(smoke_job)
    assert "cortex --version" in smoke_run
    assert "cortex --help" in smoke_run


def test_release_workflow_is_tag_only_and_attaches_built_artifacts() -> None:
    payload, text = _load_workflow(".github/workflows/release.yml")
    triggers = _workflow_on(payload)
    push = triggers.get("push")
    assert isinstance(push, dict), "release.yml must trigger on push tags"
    assert push.get("tags") == ["v*"]

    jobs = payload.get("jobs")
    assert isinstance(jobs, dict), "release.yml jobs must be a mapping"
    assert jobs, "release.yml must define at least one job"
    assert "python -m build" in text
    assert "twine check --strict" in text
    assert any(token in text for token in ("action-gh-release", "gh release create", "gh release upload")), (
        "release.yml must publish to GitHub Release, not just build locally"
    )
    lowered = text.lower()
    assert "pypi" not in lowered
    assert "twine upload" not in lowered


def test_release_pipeline_workflows_pin_uses_and_preserve_policy_engine_pin() -> None:
    policy_text = _read_required(".github/workflows/policy-check.yml")
    assert POLICY_ENGINE_PIN in policy_text

    for relpath in (".github/workflows/tests.yml", ".github/workflows/release.yml"):
        payload, _ = _load_workflow(relpath)
        uses = _all_uses(payload)
        assert uses, f"{relpath} must declare at least one uses step"
        for use in uses:
            assert SHA_PIN_RE.match(use), f"{relpath} uses entry must be SHA pinned: {use}"
