from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import job_workspace, owner_reclaim, worktree_reclaim


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _workspace(tmp_path: Path) -> tuple[Path, dict[str, object], Path, Path]:
    pool = tmp_path / "pool"
    pool.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    (source / "seed").write_text("seed\n", encoding="utf-8")
    _git(source, "add", "seed")
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "seed"],
        check=True,
    )
    workspace = pool / "job-1167"
    subprocess.run(["git", "clone", "-q", str(source), str(workspace)], check=True)
    marker = {
        "schema_version": job_workspace.MARKER_SCHEMA_VERSION,
        "model": job_workspace.WORKSPACE_MODEL,
        "branch": "feature/reclaim",
        "base": _git(source, "rev-parse", "HEAD"),
        "source_repo": str(source),
        "owner_identity": {"repo": "acme/repo", "work_id": "reclaim", "slice_id": "job-1167"},
        "attempt_id": "job-1167",
    }
    marker_path = job_workspace.marker_path(workspace)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    dirty = workspace / "new-dir" / "untracked.txt"
    dirty.parent.mkdir()
    dirty.write_text("preserve me\n", encoding="utf-8")
    preserve = tmp_path / "preserve"
    preserve.mkdir()
    return workspace, marker, pool, preserve


def test_owner_reclaim_scans_preserves_then_clears_only_marker_bound_workspace(tmp_path: Path) -> None:
    workspace, marker, pool, preserve = _workspace(tmp_path)
    (workspace / "committed.txt").write_text("keep the local commit\n", encoding="utf-8")
    _git(workspace, "add", "committed.txt")
    _git(workspace, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "local commit")

    result = owner_reclaim.reclaim_owned_workspace(
        workspace=workspace,
        pool_root=pool,
        preserve_root=preserve,
        expected_marker=marker,
    )

    assert result["status"] == "cleared"
    assert result["preserved_files"] == 1
    archive = Path(str(result["preserve_path"]))
    assert (archive / "new-dir" / "untracked.txt").read_text() == "preserve me\n"
    assert result["preserved_commit"] is True
    assert (archive / "workspace-head.bundle").is_file()
    assert list(workspace.iterdir()) == []
    assert workspace.is_dir()


def test_owner_reclaim_refuses_foreign_marker_without_mutating_workspace(tmp_path: Path) -> None:
    workspace, marker, pool, preserve = _workspace(tmp_path)
    expected = {**marker, "attempt_id": "another-job"}

    with pytest.raises(RuntimeError, match="marker does not match"):
        owner_reclaim.reclaim_owned_workspace(
            workspace=workspace,
            pool_root=pool,
            preserve_root=preserve,
            expected_marker=expected,
        )

    assert (workspace / "new-dir" / "untracked.txt").read_text() == "preserve me\n"
    assert list(preserve.iterdir()) == []


def test_owner_reclaim_refuses_non_pool_path_without_mutating_it(tmp_path: Path) -> None:
    workspace, marker, pool, preserve = _workspace(tmp_path)
    outside_pool = tmp_path / "outside"
    outside_pool.mkdir()
    with pytest.raises(RuntimeError, match="outside the configured job pool"):
        owner_reclaim.reclaim_owned_workspace(
            workspace=workspace,
            pool_root=outside_pool,
            preserve_root=preserve,
            expected_marker=marker,
        )
    assert (workspace / "new-dir" / "untracked.txt").exists()


def test_owner_reclaim_dirty_scan_failure_preserves_workspace(tmp_path: Path, monkeypatch) -> None:
    workspace, marker, pool, preserve = _workspace(tmp_path)
    original = owner_reclaim.subprocess.run

    def fail_scan(argv, **kwargs):
        if argv[0] == "git" and "status" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"permission denied")
        return original(argv, **kwargs)

    monkeypatch.setattr(owner_reclaim.subprocess, "run", fail_scan)
    with pytest.raises(RuntimeError, match="dirty scan failed"):
        owner_reclaim.reclaim_owned_workspace(
            workspace=workspace,
            pool_root=pool,
            preserve_root=preserve,
            expected_marker=marker,
        )
    assert (workspace / "new-dir" / "untracked.txt").exists()
    assert list(preserve.iterdir()) == []


def test_owner_reclaim_refuses_dirty_content_that_exceeds_preserve_limit(tmp_path: Path) -> None:
    workspace, marker, pool, preserve = _workspace(tmp_path)
    oversized = workspace / "too-large.bin"
    oversized.write_bytes(b"x" * (owner_reclaim.PRESERVE_FILE_MAX_BYTES + 1))

    with pytest.raises(RuntimeError, match="preserve size limit"):
        owner_reclaim.reclaim_owned_workspace(
            workspace=workspace,
            pool_root=pool,
            preserve_root=preserve,
            expected_marker=marker,
        )

    assert oversized.exists()
    assert (workspace / "new-dir" / "untracked.txt").exists()
    assert list(preserve.iterdir()) == []


def test_reclaim_routes_owner_bound_workspace_through_builder_helper(
    monkeypatch, tmp_path: Path
) -> None:
    workspace, marker, pool, preserve = _workspace(tmp_path)

    def run_builder_helper(*, workspace: str | Path, marker):
        return owner_reclaim.reclaim_owned_workspace(
            workspace=workspace,
            pool_root=pool,
            preserve_root=preserve,
            expected_marker=marker,
        )

    monkeypatch.setenv("PSC_JOB_RUNNER", "systemd-template")
    monkeypatch.setenv("PSC_WORKTREE_ROOT", str(pool))
    monkeypatch.setattr(owner_reclaim, "reclaim_through_builder_unit", run_builder_helper)
    source = Path(str(marker["source_repo"]))

    result = worktree_reclaim.reclaim_worktree(workspace, repo_root=source)

    assert result.ok
    assert result.directory_removed
    assert result.preserved_files == 1
    assert not workspace.exists()
    replay = worktree_reclaim.reclaim_worktree(workspace, repo_root=source)
    assert replay.status == worktree_reclaim.RECLAIM_ABSENT
