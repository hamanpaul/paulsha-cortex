"""#1167 RC `owner-bound-reclaim` check: wiring that CI can exercise.

The installed check itself needs root, systemd, polkit and the three accounts, so
it only runs in the release qualification container.  These tests pin the parts
that broke there (run 36568739107: the check resolved the pool in the driver
process, which has no PSC_REPO_ROOT) and execute every code string the check
hands to the installed runtime, in a single-UID simulation, so a wiring or
syntax error cannot first surface inside the container.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import job_workspace, owner_reclaim

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "qualification" / "driver.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location("cortex_qualification_driver_1167", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _python(code: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        env={**env, "PYTHONPATH": str(REPO_ROOT)},
        timeout=120,
        check=False,
    )


def _runtime_env(tmp_path: Path) -> dict[str, str]:
    pool = tmp_path / "worktree"
    pool.mkdir()
    repos = tmp_path / "repos"
    repos.mkdir()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PSC_AGENTS_ROOT": str(tmp_path / "agents"),
        "PSC_REPO_ROOT": str(repos / "installed"),
        "PSC_WORKTREE_ROOT": str(pool),
        # No such account on a CI host: the per-job ACL step is then a no-op,
        # exactly as `ensure_workspace_reachable` documents for single-UID hosts.
        "PSC_BUILDER_ACCOUNT": "cortex-builder-absent-1167",
        "PSC_GATE_ACCOUNT": "cortex-gate-absent-1167",
    }


def test_manager_steps_run_as_the_installed_manager_runtime_not_the_driver(
    monkeypatch, tmp_path: Path
) -> None:
    driver = _load_driver()
    # The RC driver process has no PSC_REPO_ROOT; nothing may resolve paths here.
    monkeypatch.delenv("PSC_REPO_ROOT", raising=False)
    monkeypatch.delenv("PSC_WORKTREE_ROOT", raising=False)
    installed = {
        "HOME": "/root",
        "PATH": "/opt/cortex/venv/bin:/usr/bin:/bin",
        "PSC_REPO_ROOT": "/var/lib/cortex/repos/installed",
        "PSC_WORKTREE_ROOT": "/var/lib/cortex/worktree",
        "PSC_JOB_RUNNER": "systemd-template",
    }
    monkeypatch.setattr(driver, "_installed_runtime_env", lambda: dict(installed))
    monkeypatch.setattr(
        driver,
        "_account_env",
        lambda account: {"HOME": f"/var/lib/{account}", "PATH": "/usr/bin:/bin"},
    )
    calls: list[dict[str, object]] = []

    def fake_run(argv, *, user=None, env=None, timeout=120):
        calls.append({"argv": tuple(argv), "user": user, "env": dict(env or {}), "timeout": timeout})
        return driver.CommandResult(tuple(argv), 0, 'noise\n{"pool": "/var/lib/cortex/worktree"}\n', "")

    monkeypatch.setattr(driver, "_run", fake_run)

    result = driver._owner_reclaim_manager_json("print(1)", "arg", label="probe")

    assert result == {"pool": "/var/lib/cortex/worktree"}
    [call] = calls
    assert call["user"] == "cortex-manager"
    assert call["argv"][:3] == ("/opt/cortex/venv/bin/python", "-c", "print(1)")
    assert call["env"]["HOME"] == "/var/lib/cortex-manager"
    for key in ("PSC_REPO_ROOT", "PSC_WORKTREE_ROOT", "PSC_JOB_RUNNER"):
        assert call["env"][key] == installed[key]


def test_manager_step_without_a_json_result_fails_closed(monkeypatch) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_installed_runtime_env", dict)
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    monkeypatch.setattr(
        driver,
        "_run",
        lambda argv, **_kwargs: driver.CommandResult(tuple(argv), 0, "not json\n", ""),
    )
    with pytest.raises(driver.QualificationFailure, match="returned no JSON result"):
        driver._owner_reclaim_manager_json("print(1)", label="probe")


def test_installed_check_requires_root_before_touching_anything(monkeypatch) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        driver, "_installed_runtime_env", lambda: pytest.fail("must not load runtime as non-root")
    )
    with pytest.raises(driver.QualificationFailure, match="requires root"):
        driver._installed_owner_bound_reclaim({}, Path("/nonexistent"))


def test_manager_setup_code_provisions_production_shaped_pool_slots(tmp_path: Path) -> None:
    driver = _load_driver()
    env = _runtime_env(tmp_path)
    source = tmp_path / "repos" / "rc-owner-reclaim-fixture"
    jobs = ["rc-owner-reclaim-abc", "rc-owner-reclaim-abc-foreign"]

    completed = _python(driver._OWNER_RECLAIM_SETUP_CODE, str(source), json.dumps(jobs), env=env)

    assert completed.returncode == 0, completed.stderr
    fixture = json.loads(completed.stdout.strip().splitlines()[-1])
    pool = Path(env["PSC_WORKTREE_ROOT"]).resolve()
    assert fixture["pool"] == str(pool)
    assert fixture["evidence_root"].endswith("/evidence/worktree-reclaim")
    assert set(fixture["workspaces"]) == set(jobs)
    for job_id, row in fixture["workspaces"].items():
        workspace = Path(row["path"])
        # Same derivation as dispatch: the slot name is the template instance.
        assert workspace == pool / job_workspace.job_segment(job_id)
        marker = job_workspace.read_marker(workspace)
        assert marker["attempt_id"] == job_id
        assert marker["owner_identity"]["slice_id"] == job_id
        assert marker["source_repo"] == str(source)
        assert row["marker_sha256"] == owner_reclaim.marker_digest(marker)
        assert row["approval"].endswith(f"/{workspace.name}.json")
        assert row["surfaces"] and all(
            Path(surface).name == workspace.name for surface in row["surfaces"]
        )
        # Born under the Manager unit's UMask=0077.
        assert stat.S_IMODE(workspace.stat().st_mode) & 0o077 == 0
        assert stat.S_IMODE(job_workspace.marker_path(workspace).stat().st_mode) & 0o077 == 0


def test_builder_code_commits_and_leaves_untracked_content(tmp_path: Path) -> None:
    driver = _load_driver()
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    (source / "seed").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "seed"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=f", "-c", "user.email=f@example.invalid", "commit", "-qm", "seed"],
        check=True,
    )
    workspace = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(source), str(workspace)], check=True)

    completed = _python(
        driver._OWNER_RECLAIM_BUILDER_CODE,
        str(workspace),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
    )

    assert completed.returncode == 0, completed.stderr
    head = completed.stdout.strip().splitlines()[-1]
    assert driver.SHA40.fullmatch(head)
    assert (workspace / "untracked" / "nested" / "payload.txt").read_text() == "preserve\n"
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status.strip() == "?? untracked/nested/payload.txt"


def test_manager_reclaim_code_reports_the_reclaim_record(tmp_path: Path) -> None:
    driver = _load_driver()
    env = _runtime_env(tmp_path)
    source = Path(env["PSC_REPO_ROOT"])
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)

    completed = _python(
        driver._OWNER_RECLAIM_RECLAIM_CODE,
        str(Path(env["PSC_WORKTREE_ROOT"]) / "already-gone"),
        str(source),
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    record = json.loads(completed.stdout.strip().splitlines()[-1])
    assert record["status"] == "absent"
    assert record["directory_removed"] is False
