"""Release preflight: the conditional `legacy-adoption` requirement (#1122, PR-5).

A release whose diff against the previous release tag touches
`paulsha_cortex/trust_root/install/` or `qualification/` needs a successful
`legacy-adoption` rc-qualification run on the exact release SHA (within three
days); other releases do not.  The decision and the run selection live in
`qualification/release_gate.py` so they can be tested here; release.yml only
wires them to the GitHub API.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "qualification" / "release_gate.py"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SHA = "a" * 40
OTHER = "b" * 40
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
LEGACY_TITLE = "RC qualification (legacy-adoption)"
RELEASE_TITLE = "RC qualification (release)"


def _gate():
    spec = importlib.util.spec_from_file_location("cortex_release_gate", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _gate()


# ---------------------------------------------------------------------------
# which releases need the legacy-adoption profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "required"),
    [
        (["paulsha_cortex/trust_root/install/core.py"], True),
        (["paulsha_cortex/trust_root/install/legacy.py", "README.md"], True),
        (["qualification/run.sh"], True),
        (["qualification/legacy_fixture.json"], True),
        (["paulsha_cortex/trust_root/permgen.py", "docs/runbook.md"], False),
        (["paulsha_cortex/trust_root/installer.py"], False),
        (["qualification-notes/run.sh", "tests/qualification/x.py"], False),
        ([], False),
    ],
)
def test_legacy_trigger_paths_match_installer_and_qualification_trees(
    paths: list[str], required: bool
) -> None:
    matched = gate.legacy_trigger_paths(paths)
    assert bool(matched) is required
    assert set(matched) <= set(paths)


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "gate test",
            "GIT_AUTHOR_EMAIL": "gate@example.invalid",
            "GIT_COMMITTER_NAME": "gate test",
            "GIT_COMMITTER_EMAIL": "gate@example.invalid",
        },
    )
    return completed.stdout.strip()


def _commit(repo: Path, path: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(message + "\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "-c", "init.defaultBranch=main", "init", "--quiet")
    _commit(root, "README.md", "initial")
    _git(root, "tag", "--annotate", "--message", "Release v0.1.11", "v0.1.11")
    return root


def _requirement(repo: Path, head: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(GATE), "legacy-requirement", "--repo", str(repo), "--head", head],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_release_touching_the_installer_requires_legacy_adoption(repo: Path) -> None:
    _commit(repo, "docs/notes.md", "docs only")
    head = _commit(repo, "paulsha_cortex/trust_root/install/core.py", "installer change")

    result = _requirement(repo, head)

    assert result["required"] is True
    assert result["base_tag"] == "v0.1.11"
    assert result["paths"] == ["paulsha_cortex/trust_root/install/core.py"]


def test_release_touching_qualification_requires_legacy_adoption(repo: Path) -> None:
    head = _commit(repo, "qualification/run.sh", "harness change")
    result = _requirement(repo, head)
    assert result["required"] is True and result["paths"] == ["qualification/run.sh"]


def test_release_without_installer_or_qualification_changes_does_not(repo: Path) -> None:
    _commit(repo, "paulsha_cortex/coordinator/manager.py", "coordinator change")
    head = _commit(repo, "docs/notes.md", "docs")

    result = _requirement(repo, head)

    assert result == {
        "required": False,
        "base_tag": "v0.1.11",
        "paths": [],
        "reason": "no change under paulsha_cortex/trust_root/install/ or qualification/ since v0.1.11",
    }


def test_only_changes_after_the_previous_release_tag_count(repo: Path) -> None:
    _commit(repo, "qualification/run.sh", "harness change released in v0.1.12")
    _git(repo, "tag", "--annotate", "--message", "Release v0.1.12", "v0.1.12")
    head = _commit(repo, "paulsha_cortex/coordinator/manager.py", "coordinator change")

    result = _requirement(repo, head)

    assert result["required"] is False
    assert result["base_tag"] == "v0.1.12"


def test_a_tag_on_the_release_commit_itself_is_not_the_previous_release(repo: Path) -> None:
    head = _commit(repo, "qualification/run.sh", "harness change")
    _git(repo, "tag", "--annotate", "--message", "Release v0.1.12", "v0.1.12")

    result = _requirement(repo, head)

    assert result["base_tag"] == "v0.1.11"
    assert result["required"] is True


def test_without_a_previous_release_tag_the_requirement_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "-c", "init.defaultBranch=main", "init", "--quiet")
    _commit(root, "README.md", "initial")
    head = _commit(root, "docs/notes.md", "docs")

    result = _requirement(root, head)

    assert result["required"] is True
    assert result["base_tag"] is None
    assert "no previous release tag" in result["reason"]


def test_legacy_requirement_rejects_a_non_sha_head(repo: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(GATE), "legacy-requirement", "--repo", str(repo), "--head", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0


# ---------------------------------------------------------------------------
# which rc-qualification run satisfies each profile
# ---------------------------------------------------------------------------


def _run(
    run_id: int,
    *,
    title: str,
    sha: str = SHA,
    age: timedelta = timedelta(hours=1),
    conclusion: str = "success",
    status: str = "completed",
) -> dict:
    return {
        "id": run_id,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "display_title": title,
        "created_at": (NOW - age).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _select(runs: list[dict], profile: str) -> dict:
    return gate.select_run(
        {"workflow_runs": runs}, sha=SHA, profile=profile, now=NOW.timestamp()
    )


def test_release_selection_ignores_newer_legacy_runs() -> None:
    runs = [
        _run(1, title=RELEASE_TITLE, age=timedelta(hours=5)),
        _run(2, title=LEGACY_TITLE, age=timedelta(hours=1)),
    ]
    assert _select(runs, "release")["id"] == 1
    assert _select(runs, "legacy-adoption")["id"] == 2


def test_release_selection_accepts_runs_from_before_the_profile_input() -> None:
    runs = [_run(7, title="RC qualification")]
    assert _select(runs, "release")["id"] == 7
    with pytest.raises(gate.GateError, match="legacy-adoption"):
        _select(runs, "legacy-adoption")


def test_selection_takes_the_newest_successful_exact_sha_run() -> None:
    runs = [
        _run(1, title=LEGACY_TITLE, age=timedelta(hours=10)),
        _run(2, title=LEGACY_TITLE, age=timedelta(hours=2)),
        _run(3, title=LEGACY_TITLE, age=timedelta(hours=1), conclusion="failure"),
        _run(4, title=LEGACY_TITLE, age=timedelta(minutes=5), status="in_progress", conclusion=None),
        _run(5, title=LEGACY_TITLE, sha=OTHER, age=timedelta(minutes=1)),
    ]
    selected = _select(runs, "legacy-adoption")
    assert selected["id"] == 2
    assert selected["artifact_name"] == f"rc-qualification-legacy-adoption-{SHA}"
    assert _select([_run(9, title=RELEASE_TITLE)], "release")["artifact_name"] == (
        f"rc-qualification-{SHA}"
    )


@pytest.mark.parametrize(
    ("runs", "message"),
    [
        ([], "no successful legacy-adoption"),
        ([_run(1, title=LEGACY_TITLE, conclusion="failure")], "no successful legacy-adoption"),
        ([_run(1, title=LEGACY_TITLE, sha=OTHER)], "no successful legacy-adoption"),
        ([_run(1, title=LEGACY_TITLE, age=timedelta(days=3, minutes=1))], "older than 3 days"),
        ([_run(1, title=LEGACY_TITLE, age=timedelta(hours=-1))], "future-dated"),
    ],
    ids=["none", "failed", "other-sha", "stale", "future"],
)
def test_legacy_selection_fails_closed_and_names_the_profile(runs, message: str) -> None:
    with pytest.raises(gate.GateError, match=message):
        _select(runs, "legacy-adoption")


def test_select_run_cli_prints_the_run_or_fails(tmp_path: Path) -> None:
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps({"workflow_runs": [_run(4, title=LEGACY_TITLE)]}), encoding="utf-8")
    base = [
        sys.executable,
        str(GATE),
        "select-run",
        "--runs",
        str(runs),
        "--sha",
        SHA,
        "--now",
        str(int(NOW.timestamp())),
    ]
    found = subprocess.run([*base, "--profile", "legacy-adoption"], capture_output=True, text=True)
    assert found.returncode == 0, found.stderr
    assert json.loads(found.stdout)["id"] == 4
    missing = subprocess.run([*base, "--profile", "release"], capture_output=True, text=True)
    assert missing.returncode == 1
    assert "no successful release" in missing.stderr


# ---------------------------------------------------------------------------
# workflow wiring
# ---------------------------------------------------------------------------


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_rc_qualification_dispatch_selects_the_profile_and_defaults_to_release() -> None:
    payload = _workflow("rc-qualification.yml")
    raw = (WORKFLOWS / "rc-qualification.yml").read_text(encoding="utf-8")
    on_block = payload.get("on", payload.get(True))
    profile = on_block["workflow_dispatch"]["inputs"]["profile"]
    assert profile["type"] == "choice"
    assert profile["options"] == ["release", "legacy-adoption"]
    assert profile["default"] == "release"
    # Run titles carry the profile so release preflight can tell runs apart.
    assert payload["run-name"] == "RC qualification (${{ inputs.profile || 'release' }})"

    steps = payload["jobs"]["qualification"]["steps"]
    run_step = next(step for step in steps if step.get("name") == "Run privileged systemd qualification")
    assert run_step["env"]["QUALIFICATION_PROFILE"] == "${{ inputs.profile || 'release' }}"
    assert "release) profile_args=(--profile release) ;;" in run_step["run"]
    assert "legacy-adoption) profile_args=(--profile legacy-adoption) ;;" in run_step["run"]
    assert '"${profile_args[@]}"' in run_step["run"]
    redaction = next(
        step for step in steps if step.get("name") == "Fail closed on credential material in evidence"
    )
    assert '--profile "$QUALIFICATION_PROFILE"' in redaction["run"]
    upload = next(step for step in steps if "upload-artifact" in str(step.get("uses", "")))
    name = upload["with"]["name"]
    assert "rc-qualification-legacy-adoption-{0}" in name
    assert "rc-qualification-{0}" in name
    assert "inputs.profile == 'legacy-adoption'" in name
    assert "secrets." not in raw


def test_release_preflight_requires_legacy_adoption_only_for_installer_changes() -> None:
    payload = _workflow("release.yml")
    preflight = payload["jobs"]["release-preflight"]
    checkout = preflight["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0, "the previous release tag needs full history"
    runs = "\n".join(step.get("run", "") for step in preflight["steps"])

    assert "python3 qualification/release_gate.py legacy-requirement" in runs
    assert "--profile release" in runs and "--profile legacy-adoption" in runs
    requirement = runs.index("legacy-requirement")
    legacy_select = runs.index("--profile legacy-adoption")
    assert requirement < legacy_select
    # The legacy selection only runs when the requirement says so.
    assert "if jq -e '.required == true'" in runs[requirement:legacy_select]
    assert "rc-qualification-legacy-adoption-$release_sha" in runs
    assert "actions/runs/$legacy_rc_run_id/artifacts" in runs
    assert ".expired == false" in runs
    assert "legacy-adoption" in runs and "::error::" in runs
    assert "dispatch rc-qualification.yml with profile=legacy-adoption" in runs
    assert "legacy_rc_run_id" in preflight["outputs"]
    # The PR-head rc-qualification check keeps meaning the release profile.
    assert f'!= "{LEGACY_TITLE}"' in runs
