from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SHA_PIN_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
USES_WITH_VERSION_COMMENT_RE = re.compile(
    r"^\s*uses:\s+[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}\s+#\s+v\d+\.\d+\.\d+(?:[-.A-Za-z0-9]+)?\s*$"
)


def _load_workflow(name: str) -> dict:
    path = WORKFLOWS / name
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} must exist"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(
        payload, dict
    ), f"{path.relative_to(REPO_ROOT)} must parse to a mapping"
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
            assert isinstance(
                uses, str
            ), f"{relpath}:{job_name} step uses must be a string"
            assert SHA_PIN_RE.match(
                uses
            ), f"{relpath}:{job_name} step `{uses}` must use a 40-hex SHA pin"


def _assert_all_uses_have_version_comments(name: str) -> None:
    path = WORKFLOWS / name
    uses_lines = [
        (line_no, line)
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if re.match(r"^\s*uses:\s+", line)
    ]
    assert (
        uses_lines
    ), f"{path.relative_to(REPO_ROOT)} must contain at least one uses step"
    for line_no, line in uses_lines:
        assert USES_WITH_VERSION_COMMENT_RE.match(
            line
        ), f"{path.relative_to(REPO_ROOT)}:{line_no} uses line must keep a 40-hex SHA pin and `# vX.Y.Z` comment"


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
    pytest_runs = _job_step_runs(pytest_job)
    assert "sudo apt-get install --yes --no-install-recommends acl" in pytest_runs
    assert "sudo useradd --system --user-group --create-home" in pytest_runs
    for account in (
        "cortex-builder",
        "cortex-reviewer-planner",
        "cortex-manager",
        "cortex-gate",
    ):
        assert account in pytest_runs
    assert "command -v setfacl" in pytest_runs

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
    _assert_all_uses_have_version_comments("tests.yml")


def test_release_workflow_is_manual_no_pypi_and_sha_pinned() -> None:
    payload = _load_workflow("release.yml")
    on_block = _workflow_on(payload)
    assert set(on_block) == {
        "workflow_dispatch"
    }, "release.yml must validate an untagged exact SHA before publishing"
    dispatch = on_block["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    inputs = dispatch.get("inputs")
    assert isinstance(inputs, dict)
    version = inputs.get("version")
    assert isinstance(version, dict)
    assert version.get("required") is True
    assert version.get("type") == "string"

    raw = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "policy_version" not in raw
    lowered = raw.lower()
    assert "pypi" not in lowered
    assert "twine upload" not in lowered

    jobs = payload.get("jobs", {})
    assert isinstance(jobs, dict) and jobs, "release.yml must define jobs"
    _assert_all_uses_are_sha_pinned(payload, relpath=".github/workflows/release.yml")
    _assert_all_uses_have_version_comments("release.yml")


def _job_needs(job: dict) -> set[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return {needs}
    assert isinstance(needs, list), "job needs must be a string or list"
    return set(needs)


def test_rc_qualification_is_manual_protected_and_sha_pinned() -> None:
    payload = _load_workflow("rc-qualification.yml")
    on_block = _workflow_on(payload)
    assert set(on_block) == {
        "workflow_dispatch"
    }, "RC qualification is privileged and must never run on push, pull_request, or schedule"

    jobs = payload.get("jobs", {})
    assert isinstance(jobs, dict) and jobs
    qualification_jobs = [
        job
        for job in jobs.values()
        if isinstance(job, dict) and "qualification/run.sh" in _job_step_runs(job)
    ]
    assert qualification_jobs, "RC workflow must execute qualification/run.sh"
    assert all(
        job.get("environment") == "rc-qualification" for job in qualification_jobs
    ), "every job running privileged qualification must use the protected rc-qualification environment"

    raw = (WORKFLOWS / "rc-qualification.yml").read_text(encoding="utf-8")
    lowered = raw.lower()
    assert "python -m build" in lowered
    assert "qualification/run.sh" in raw
    assert "qualification.json" in raw
    assert "upload-artifact" in lowered
    assert "github.sha" in lowered or "github.event.inputs" in lowered
    assert "timeout-minutes" in lowered

    _assert_all_uses_are_sha_pinned(
        payload, relpath=".github/workflows/rc-qualification.yml"
    )
    _assert_all_uses_have_version_comments("rc-qualification.yml")


def test_release_requires_exact_sha_qualification_and_wheel_hash_before_publish() -> (
    None
):
    payload = _load_workflow("release.yml")
    jobs = payload.get("jobs", {})
    assert isinstance(jobs, dict)

    preflight = jobs.get("release-preflight")
    assert isinstance(preflight, dict), "release.yml must define an untagged preflight"
    preflight_runs = _job_step_runs(preflight)
    assert "default-branch head" in preflight_runs
    assert "release dispatch ref is not the default branch" in preflight_runs
    assert "actions/workflows/tests.yml/runs" in preflight_runs
    assert "openspec/changes/phase2-install-docker-qualification" in preflight_runs
    assert (
        "phase2-install-docker-qualification" in preflight_runs
        and "archive" in preflight_runs
    )
    assert "merge_commit_sha" in preflight_runs
    assert 'state == "APPROVED"' in preflight_runs
    assert "check-runs" in preflight_runs
    assert "check-runs?per_page=100" in preflight_runs
    assert "--paginate" in preflight_runs
    assert "jq -sc" in preflight_runs
    assert "openspec validate --specs --no-interactive" in preflight_runs
    assert (
        "openspec validate phase2-install-docker-qualification" in preflight_runs
        and "--strict --no-interactive" in preflight_runs
    )
    assert "archived Phase 2 tasks are incomplete" in preflight_runs
    assert "rc-qualification.yml" in preflight_runs
    assert "head_sha" in preflight_runs
    assert "matching-refs/tags" in preflight_runs
    assert "tag target mismatch" in preflight_runs
    assert "tag already exists" in preflight_runs
    outputs = preflight.get("outputs")
    assert isinstance(outputs, dict)
    assert {"release_sha", "tag_name", "rc_run_id"} <= set(outputs)
    permissions = payload.get("permissions")
    assert isinstance(permissions, dict)
    assert permissions.get("contents") == "read"

    build = jobs.get("build")
    assert isinstance(build, dict)
    assert "release-preflight" in _job_needs(build)
    assert "needs['release-preflight'].outputs.release_sha" in repr(build)

    gate = jobs.get("qualification-gate")
    assert isinstance(gate, dict), "release.yml must define qualification-gate"
    assert {"release-preflight", "build"} <= _job_needs(gate)

    gate_runs = _job_step_runs(gate)
    gate_text = repr(gate)
    gate_lower = gate_runs.lower()
    assert "qualification.json" in gate_text
    assert "qualification/validate.py" in gate_runs
    assert "--candidate-sha" in gate_runs
    assert "--wheel-sha256" in gate_runs
    assert "sha256sum" in gate_lower
    assert "needs['release-preflight'].outputs.release_sha" in gate_text
    assert "needs['release-preflight'].outputs.rc_run_id" in gate_text

    release = jobs.get("release")
    assert isinstance(release, dict)
    assert {"release-preflight", "qualification-gate"} <= _job_needs(release)
    release_permissions = release.get("permissions")
    assert isinstance(release_permissions, dict)
    assert release_permissions.get("contents") == "write"

    release_runs = _job_step_runs(release)
    release_uses = "\n".join(
        str(step.get("uses", ""))
        for step in release.get("steps", [])
        if isinstance(step, dict)
    ).lower()
    assert "action-gh-release" not in release_uses
    assert "default-branch head changed" in release_runs
    assert "matching-refs/tags" in release_runs
    assert "tag target mismatch" in release_runs
    assert "tag already exists" in release_runs
    assert "repos/${GITHUB_REPOSITORY}/git/tags" in release_runs
    assert "repos/${GITHUB_REPOSITORY}/git/refs" in release_runs
    assert "refs/tags/$tag_name" in release_runs
    assert "gh release create" in release_runs
    assert "--verify-tag" in release_runs
    assert "--method DELETE" in release_runs
    assert "--draft" in release_runs
    assert "draft: false" in release_runs
    assert "release_transaction_marker" in release_runs
    assert "repos/${GITHUB_REPOSITORY}/releases/$release_id" in release_runs
    assert "release cleanup ownership mismatch" in release_runs
    for job_name, job in jobs.items():
        if job_name == "release" or not isinstance(job, dict):
            continue
        non_release_runs = _job_step_runs(job)
        assert "--method POST" not in non_release_runs
        assert "gh release create" not in non_release_runs
    assert (
        "qualification/validate.py" not in release_runs
    ), "evidence validation belongs in the required predecessor gate, never after publication"


def test_release_asset_failure_removes_owned_release_before_tag(tmp_path: Path) -> None:
    payload = _load_workflow("release.yml")
    release = payload["jobs"]["release"]
    release_runs = _job_step_runs(release)
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    dist = tmp_path / "dist"
    fake_bin.mkdir()
    state.mkdir()
    dist.mkdir()
    (dist / "candidate.whl").write_bytes(b"qualified wheel")
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["FAKE_GH_STATE"])
events = state / "events"

def record(value):
    with events.open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")

if args[:2] == ["api", "repos/example/cortex"]:
    print("main")
elif args[:2] == ["api", "repos/example/cortex/git/ref/heads/main"]:
    print(os.environ["RELEASE_SHA"])
elif "matching-refs/tags" in " ".join(args):
    print("[]")
elif args[:3] == ["api", "--method", "POST"] and args[3].endswith("/git/tags"):
    sys.stdin.read()
    print(json.dumps({"sha": "b" * 40}))
elif args[:3] == ["api", "--method", "POST"] and args[3].endswith("/git/refs"):
    sys.stdin.read()
    (state / "tag").write_text("b" * 40, encoding="utf-8")
elif args[:2] == ["api", "repos/example/cortex/git/ref/tags/v1.2.3"]:
    if not (state / "tag").exists():
        raise SystemExit(1)
    if "--jq" in args:
        print("b" * 40)
    else:
        print(json.dumps({
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "tag", "sha": "b" * 40},
        }))
elif args[:2] == ["api", "repos/example/cortex/git/tags/" + "b" * 40]:
    print(json.dumps({
        "tag": "v1.2.3",
        "object": {"type": "commit", "sha": os.environ["RELEASE_SHA"]},
    }))
elif args[:2] == ["release", "create"]:
    marker = args[args.index("--notes") + 1]
    (state / "release.json").write_text(json.dumps({
        "id": 17,
        "tag_name": "v1.2.3",
        "body": marker,
        "draft": True,
        "assets": [],
    }), encoding="utf-8")
    record("partial-release-created")
    raise SystemExit(23)
elif "releases?per_page=100" in " ".join(args):
    release_path = state / "release.json"
    print("[" + release_path.read_text(encoding="utf-8") + "]" if release_path.exists() else "[]")
elif args[:3] == ["api", "--method", "DELETE"] and args[3].endswith("/releases/17"):
    (state / "release.json").unlink()
    record("release-deleted")
elif args[:2] == ["api", "repos/example/cortex/releases/17"]:
    raise SystemExit(1)
elif args[:3] == ["api", "--method", "DELETE"] and args[3].endswith("/git/refs/tags/v1.2.3"):
    (state / "tag").unlink()
    record("tag-deleted")
else:
    print("unexpected fake gh argv: " + repr(args), file=sys.stderr)
    raise SystemExit(97)
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    release_sha = "a" * 40
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_GH_STATE": str(state),
        "GITHUB_REPOSITORY": "example/cortex",
        "GITHUB_RUN_ID": "1234",
        "GITHUB_RUN_ATTEMPT": "1",
        "RELEASE_SHA": release_sha,
        "TAG_NAME": "v1.2.3",
    }

    result = subprocess.run(
        ["bash", "-c", release_runs],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 23, result.stderr
    assert not (state / "release.json").exists()
    assert not (state / "tag").exists()
    assert (state / "events").read_text(encoding="utf-8").splitlines() == [
        "partial-release-created",
        "release-deleted",
        "tag-deleted",
    ]
