from __future__ import annotations

import contextlib
import grp
import json
import os
import pwd
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import (
    job_runner,
    job_workspace,
    owner_reclaim,
    spool_slot,
    worktree_reclaim,
)


NONCE = "a" * 32


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    (source / "seed").write_text("seed\n", encoding="utf-8")
    _git(source, "add", "seed")
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "seed"],
        check=True,
    )
    return source


def _clone(source: Path, pool: Path, name: str, *, attempt_id: str) -> tuple[Path, dict[str, object]]:
    workspace = pool / name
    subprocess.run(["git", "clone", "-q", str(source), str(workspace)], check=True)
    marker = {
        "schema_version": job_workspace.MARKER_SCHEMA_VERSION,
        "model": job_workspace.WORKSPACE_MODEL,
        "branch": "feature/reclaim",
        "base": _git(source, "rev-parse", "HEAD"),
        "source_repo": str(source),
        "owner_identity": {"repo": "acme/repo", "work_id": "reclaim", "slice_id": name},
        "attempt_id": attempt_id,
    }
    marker_path = job_workspace.marker_path(workspace)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    dirty = workspace / "new-dir" / "untracked.txt"
    dirty.parent.mkdir()
    dirty.write_text("preserve me\n", encoding="utf-8")
    return workspace, marker


def _workspace(tmp_path: Path) -> tuple[Path, dict[str, object], Path, Path]:
    pool = tmp_path / "pool"
    pool.mkdir()
    source = _source(tmp_path)
    workspace, marker = _clone(source, pool, "job-1167", attempt_id="job-1167")
    preserve = tmp_path / "preserve"
    preserve.mkdir()
    return workspace, marker, pool, preserve


def _arguments(workspace: Path, marker: dict[str, object], pool: Path, preserve: Path) -> list[str]:
    return owner_reclaim.reclaim_arguments(
        workspace=workspace.resolve(),
        pool_root=pool.resolve(),
        preserve_root=preserve,
        marker_sha256=owner_reclaim.marker_digest(marker),
        nonce=NONCE,
    )


def _approve(spool: Path, workspace: Path, command: list[str]) -> Path:
    """Write the approval exactly as the Manager does: a job spec in its spool."""

    spool.mkdir(mode=0o700, exist_ok=True)
    spec = job_runner.build_job_spec(
        job_id=workspace.name,
        instance=workspace.name,
        unit=f"cortex-job@{workspace.name}.service",
        command=command,
        working_directory=str(workspace.resolve()),
        log_path=str(spool.parent / "reclaim-job.jsonl"),
        env={"HOME": "/nonexistent-home", "PATH": "/usr/bin:/bin"},
    )
    path = spool / f"{workspace.name}.json"
    job_runner.write_job_spec(str(path), spec)
    return path


def _reclaim_command(arguments: list[str]) -> list[str]:
    return [owner_reclaim.RECLAIM_PYTHON, "-m", owner_reclaim.MODULE, *arguments]


def _tree(path: Path) -> dict[str, bytes]:
    return {
        str(item.relative_to(path)): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file() and ".git" not in item.relative_to(path).parts
    }


def test_owner_reclaim_scans_preserves_then_clears_only_marker_bound_workspace(tmp_path: Path) -> None:
    workspace, marker, pool, preserve = _workspace(tmp_path)
    (workspace / "committed.txt").write_text("keep the local commit\n", encoding="utf-8")
    _git(workspace, "add", "committed.txt")
    _git(workspace, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "local commit")

    result = owner_reclaim.reclaim_owned_workspace(
        workspace=workspace,
        pool_root=pool,
        preserve_root=preserve,
        expected_marker_sha256=owner_reclaim.marker_digest(marker),
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
            expected_marker_sha256=owner_reclaim.marker_digest(expected),
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
            expected_marker_sha256=owner_reclaim.marker_digest(marker),
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
            expected_marker_sha256=owner_reclaim.marker_digest(marker),
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
            expected_marker_sha256=owner_reclaim.marker_digest(marker),
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
            expected_marker_sha256=owner_reclaim.marker_digest(marker),
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


# ---------------------------------------------------------------------------
# Manager approval (#1167 review MAJOR): the helper acts only on a Manager-authored
# job spec that names exactly this invocation.  The builder can read the marker and
# can call the CLI itself, so neither may stand in for Manager intent.
# ---------------------------------------------------------------------------


def test_helper_cli_runs_only_with_a_matching_manager_approval(tmp_path: Path, capsys) -> None:
    workspace, marker, pool, preserve = _workspace(tmp_path)
    spool = tmp_path / "approvals"
    arguments = _arguments(workspace, marker, pool, preserve)
    _approve(spool, workspace, _reclaim_command(arguments))

    rc = owner_reclaim.main(arguments, approval_spool=spool, manager_uid=os.getuid())

    assert rc == 0
    completion = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert completion["status"] == "cleared"
    assert completion["nonce"] == NONCE
    assert completion["workspace_name"] == workspace.name
    assert list(workspace.iterdir()) == []


def test_helper_cli_refuses_a_builder_invocation_without_any_approval(tmp_path: Path, capsys) -> None:
    workspace, marker, pool, preserve = _workspace(tmp_path)
    spool = tmp_path / "approvals"
    spool.mkdir(mode=0o700)
    before = _tree(workspace)

    rc = owner_reclaim.main(
        _arguments(workspace, marker, pool, preserve),
        approval_spool=spool,
        manager_uid=os.getuid(),
    )

    assert rc == 1
    assert "no Manager approval" in capsys.readouterr().err
    assert _tree(workspace) == before
    assert list(preserve.iterdir()) == []


@pytest.mark.parametrize(
    "forgery",
    ["not-manager-owner", "approval-group-writable", "spool-group-writable", "approval-symlink"],
)
def test_helper_cli_refuses_a_forged_approval(tmp_path: Path, capsys, forgery: str) -> None:
    workspace, marker, pool, preserve = _workspace(tmp_path)
    spool = tmp_path / "approvals"
    arguments = _arguments(workspace, marker, pool, preserve)
    approval = _approve(spool, workspace, _reclaim_command(arguments))
    manager_uid = os.getuid()
    if forgery == "not-manager-owner":
        # The builder-authored copy has the right bytes but the wrong author.
        manager_uid = os.getuid() + 1
    elif forgery == "approval-group-writable":
        approval.chmod(0o660)
    elif forgery == "spool-group-writable":
        spool.chmod(0o770)
    else:
        real = tmp_path / "elsewhere.json"
        real.write_bytes(approval.read_bytes())
        approval.unlink()
        approval.symlink_to(real)
    before = _tree(workspace)

    rc = owner_reclaim.main(arguments, approval_spool=spool, manager_uid=manager_uid)

    assert rc == 1
    assert "approval" in capsys.readouterr().err
    assert _tree(workspace) == before
    assert list(preserve.iterdir()) == []


def test_helper_cli_refuses_to_clear_another_pool_workspace(tmp_path: Path, capsys) -> None:
    owned, owned_marker, pool, preserve = _workspace(tmp_path)
    source = Path(str(owned_marker["source_repo"]))
    foreign, foreign_marker = _clone(source, pool, "job-foreign", attempt_id="job-foreign")
    spool = tmp_path / "approvals"
    _approve(spool, owned, _reclaim_command(_arguments(owned, owned_marker, pool, preserve)))
    foreign_arguments = _arguments(foreign, foreign_marker, pool, preserve)
    before = _tree(foreign)

    # No approval names the foreign slot at all.
    assert owner_reclaim.main(foreign_arguments, approval_spool=spool, manager_uid=os.getuid()) == 1
    assert "no Manager approval" in capsys.readouterr().err
    # The foreign slot's own Manager spec is a model job, not a reclaim approval.
    _approve(spool, foreign, ["codex", "exec", "--json"])
    assert owner_reclaim.main(foreign_arguments, approval_spool=spool, manager_uid=os.getuid()) == 1
    assert "does not authorize" in capsys.readouterr().err
    # Re-targeting the owned approval at the foreign slot changes the argv.
    retargeted = _arguments(owned, owned_marker, pool, preserve)
    retargeted[retargeted.index("--workspace") + 1] = str(foreign.resolve())
    assert owner_reclaim.main(retargeted, approval_spool=spool, manager_uid=os.getuid()) == 1
    assert _tree(foreign) == before
    assert (owned / "new-dir" / "untracked.txt").exists()
    assert list(preserve.iterdir()) == []


def test_helper_cli_refuses_a_replayed_approval(tmp_path: Path, capsys) -> None:
    workspace, marker, pool, preserve = _workspace(tmp_path)
    source = Path(str(marker["source_repo"]))
    spool = tmp_path / "approvals"
    arguments = _arguments(workspace, marker, pool, preserve)
    approval = _approve(spool, workspace, _reclaim_command(arguments))
    stale = approval.read_bytes()
    assert owner_reclaim.main(arguments, approval_spool=spool, manager_uid=os.getuid()) == 0
    capsys.readouterr()

    # Consumed: the Manager deletes the approval once the unit has finished.
    approval.unlink()
    assert owner_reclaim.main(arguments, approval_spool=spool, manager_uid=os.getuid()) == 1
    assert "no Manager approval" in capsys.readouterr().err

    # A later attempt provisions the same slot again; a leftover copy of the old
    # approval must not clear the new attempt's work.
    workspace.rmdir()
    workspace, _new_marker = _clone(source, pool, "job-1167", attempt_id="job-1167-retry")
    approval.write_bytes(stale)
    approval.chmod(0o640)
    before = _tree(workspace)
    assert owner_reclaim.main(arguments, approval_spool=spool, manager_uid=os.getuid()) == 1
    assert "marker does not match" in capsys.readouterr().err
    assert _tree(workspace) == before


# ---------------------------------------------------------------------------
# Manager half: one systemd template instance per existing pool slot, with a
# single-use approval.  systemd is replaced by a fake that executes the approved
# helper in-process, the way the root-owned shim would.
# ---------------------------------------------------------------------------


@pytest.fixture
def manager_runtime(tmp_path: Path, monkeypatch):
    agents = tmp_path / "agents"
    pool = tmp_path / "worktree"
    pool.mkdir()
    spool = tmp_path / "job-specs" / "builder"
    spool.mkdir(parents=True, mode=0o700)
    home = tmp_path / "builder-home"
    home.mkdir()
    controls = tmp_path / "codex-controls" / "builder"
    (controls / "plugins").mkdir(parents=True)
    (controls / "skills").mkdir()
    (controls / "config.toml").write_text("model = 'fixture'\n", encoding="utf-8")
    (controls / "hooks.json").write_text("{}\n", encoding="utf-8")
    source = _source(tmp_path)
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    for key, value in {
        "PSC_AGENTS_ROOT": str(agents),
        "PSC_REPO_ROOT": str(source),
        "PSC_WORKTREE_ROOT": str(pool),
        "PSC_JOB_RUNNER": "systemd-template",
        "PSC_MANAGER_EXECUTOR": "codex",
        "PSC_BUILDER_ACCOUNT": account,
        "PSC_BUILDER_GROUP": group,
        "PSC_BUILDER_HOME": str(home),
        "PSC_BUILDER_PATH": "/usr/bin:/bin",
        "PSC_JOB_SPEC_SPOOL": str(spool),
        "PSC_CODEX_CONTROL_ROOT": str(controls.parent),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(owner_reclaim, "APPROVAL_SPEC_SPOOL", str(spool))
    monkeypatch.setattr(job_runner, "preflight_systemd_template", lambda **_kwargs: "/usr/bin/systemctl")
    monkeypatch.setattr(job_runner, "_unit_is_active", lambda *_args: False)
    monkeypatch.setattr(spool_slot, "_apply_slot_acl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(owner_reclaim, "_grant_archive_acl", lambda *_args, **_kwargs: None)
    started: list[dict[str, object]] = []

    # What else runs with this ``%i`` while the unit is up (a forger sharing the
    # instance's writable surfaces).  It can read the spec, so it knows the nonce.
    unit_behaviour: dict[str, object] = {"run_helper": True, "forge": None}

    def fake_unit(argv):
        unit = argv[-1]
        instance = unit.split("@", 1)[1].removesuffix(".service")
        spec = json.loads((spool / f"{instance}.json").read_text(encoding="utf-8"))
        started.append({"argv": list(argv), "spec": spec})
        rc = 0
        if unit_behaviour["run_helper"]:
            with open(spec["log_path"], "a", encoding="utf-8") as log:
                with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    rc = owner_reclaim.main(
                        spec["command"][3:], approval_spool=spool, manager_uid=os.getuid()
                    )
        forge = unit_behaviour["forge"]
        if forge is not None:
            forge(spec)
        return subprocess.CompletedProcess(list(argv), rc, "", "")

    monkeypatch.setattr(owner_reclaim, "_start_builder_unit", fake_unit)
    workspace, marker = _clone(
        source, pool, job_workspace.job_segment("slice-1167"), attempt_id="slice-1167"
    )
    return {
        "workspace": workspace,
        "marker": marker,
        "spool": spool,
        "pool": pool,
        "started": started,
        "unit": unit_behaviour,
        "source": source,
    }


def test_manager_half_runs_the_existing_slot_instance_with_a_single_use_approval(
    manager_runtime,
) -> None:
    workspace = manager_runtime["workspace"]
    spool = manager_runtime["spool"]
    # The log of the builder job that used this slot is still diagnostic evidence.
    build_log = spool_slot.exact_job_slot("builder-job-log", workspace.name) / "job.jsonl"
    build_log.parent.mkdir(parents=True)
    build_log.write_text('{"event": "builder job output"}\n', encoding="utf-8")

    result = owner_reclaim.reclaim_through_builder_unit(
        workspace=workspace, marker=manager_runtime["marker"]
    )

    [started] = manager_runtime["started"]
    spec = started["spec"]
    # The pool slot name *is* the unit instance: no second job_segment() hash.
    # (the template stem follows the Manager executor's hardening profile.)
    assert started["argv"][-1].startswith("cortex-job")
    assert started["argv"][-1].endswith(f"@{workspace.name}.service")
    assert spec["unit"] == started["argv"][-1]
    assert spec["instance"] == workspace.name
    assert spec["working_directory"] == str(workspace.resolve())
    assert spec["command"][1:3] == ["-m", owner_reclaim.MODULE]
    assert spec["env"]["CODEX_HOME"].endswith(f"/codex-home/builder/{workspace.name}")
    # Single use: the approval is gone once the unit has finished.
    assert not (spool / f"{workspace.name}.json").exists()
    assert list(workspace.iterdir()) == []
    # The preserved evidence has left the builder-writable spool slot.
    archive = Path(str(result["preserve_path"]))
    assert archive.parent == owner_reclaim.reclaim_evidence_root()
    assert (archive / "new-dir" / "untracked.txt").read_text() == "preserve me\n"
    slot = spool_slot.exact_job_slot("commit-spool", workspace.name)
    assert list((slot / owner_reclaim.RECLAIM_ARCHIVE_DIRNAME).iterdir()) == []
    codex_home = spool_slot.exact_job_slot("builder-codex-home", workspace.name)
    assert codex_home.is_dir() and not (codex_home / "auth.json").exists()
    assert build_log.read_text(encoding="utf-8") == '{"event": "builder job output"}\n'
    assert spec["log_path"] == str(slot / owner_reclaim.RECLAIM_LOG_FILENAME)


def _nothing_preserved(spec, *, nonce: str | None = None) -> dict[str, object]:
    """A completion record claiming a clean workspace, as a forger would write it."""

    command = spec["command"]
    return {
        "schema_version": 1,
        "status": "cleared",
        "workspace_name": spec["instance"],
        "preserved_files": 0,
        "preserved_commit": False,
        "preserve_path": None,
        "nonce": nonce if nonce is not None else command[command.index("--nonce") + 1],
    }


def _evidence_archives(workspace: Path) -> list[Path]:
    root = owner_reclaim.reclaim_evidence_root()
    return sorted(root.glob(f"{workspace.name}-*")) if root.is_dir() else []


def test_manager_half_ignores_a_completion_from_another_invocation(manager_runtime) -> None:
    workspace = manager_runtime["workspace"]

    def replace_log(spec):
        Path(spec["log_path"]).write_text(
            json.dumps(_nothing_preserved(spec, nonce="b" * 32)) + "\n", encoding="utf-8"
        )

    manager_runtime["unit"]["forge"] = replace_log
    with pytest.raises(RuntimeError, match="no completion record for this invocation"):
        owner_reclaim.reclaim_through_builder_unit(
            workspace=workspace, marker=manager_runtime["marker"]
        )
    # The evidence the helper really preserved was still taken out of builder reach.
    [archive] = _evidence_archives(workspace)
    assert (archive / "new-dir" / "untracked.txt").read_text() == "preserve me\n"


@pytest.mark.parametrize("forgery", ["appended", "replaced"])
def test_forged_completion_cannot_skip_the_evidence_handover_or_delete_the_slot(
    manager_runtime, forgery: str
) -> None:
    workspace = manager_runtime["workspace"]
    spool = manager_runtime["spool"]
    slot = spool_slot.exact_job_slot("commit-spool", workspace.name)

    def forge(spec):
        record = json.dumps(_nothing_preserved(spec)) + "\n"
        log = Path(spec["log_path"])
        if forgery == "appended":
            with log.open("a", encoding="utf-8") as stream:
                stream.write(record)
        else:  # the log is builder-writable: truncate and keep only the forgery
            log.write_text(record, encoding="utf-8")

    manager_runtime["unit"]["forge"] = forge
    outcome = worktree_reclaim.reclaim_worktree(
        workspace, repo_root=manager_runtime["source"]
    )

    assert outcome.status == worktree_reclaim.RECLAIM_FAILED
    assert "completion" in str(outcome.detail)
    # The Manager did not remove the slot on the strength of the forged record.
    assert workspace.is_dir()
    # It moved the real archive into Manager-only evidence anyway ...
    [archive] = _evidence_archives(workspace)
    assert (archive / "new-dir" / "untracked.txt").read_text() == "preserve me\n"
    # ... nothing is left in the builder-reachable preserve area, and the slot is sealed.
    assert list((slot / owner_reclaim.RECLAIM_ARCHIVE_DIRNAME).iterdir()) == []
    assert slot.stat().st_mode & 0o222 == 0
    assert not (spool / f"{workspace.name}.json").exists()


def test_retry_after_a_rejected_completion_converges_on_the_emptied_slot(
    manager_runtime,
) -> None:
    """helper 已清空（marker 一併清掉）但完成紀錄被拒：重送不得卡在空 slot。"""

    workspace = manager_runtime["workspace"]

    def forge(spec):
        Path(spec["log_path"]).write_text(
            json.dumps(_nothing_preserved(spec)) + "\n", encoding="utf-8"
        )

    manager_runtime["unit"]["forge"] = forge
    first = worktree_reclaim.reclaim_worktree(workspace, repo_root=manager_runtime["source"])
    assert first.status == worktree_reclaim.RECLAIM_FAILED
    assert workspace.is_dir() and list(workspace.iterdir()) == []
    [archive] = _evidence_archives(workspace)

    manager_runtime["unit"]["forge"] = None
    retry = worktree_reclaim.reclaim_worktree(workspace, repo_root=manager_runtime["source"])

    assert retry.status == worktree_reclaim.RECLAIM_RECLAIMED, retry.detail
    assert retry.directory_removed
    assert not workspace.exists()
    # 空目錄的移除不起第二次 unit，也不動已移交的證據。
    assert len(manager_runtime["started"]) == 1
    assert (archive / "new-dir" / "untracked.txt").read_text() == "preserve me\n"
    again = worktree_reclaim.reclaim_worktree(workspace, repo_root=manager_runtime["source"])
    assert again.status == worktree_reclaim.RECLAIM_ABSENT


def test_empty_directory_outside_the_pool_is_still_not_a_worktree(tmp_path: Path, monkeypatch) -> None:
    pool = tmp_path / "pool"
    pool.mkdir()
    monkeypatch.setenv("PSC_WORKTREE_ROOT", str(pool))
    source = _source(tmp_path)
    stray = tmp_path / "not-in-pool"
    stray.mkdir()

    outcome = worktree_reclaim.reclaim_worktree(stray, repo_root=source)

    assert outcome.status == worktree_reclaim.RECLAIM_FAILED
    assert outcome.detail == "worktree-path-not-a-worktree"
    assert stray.is_dir()


def test_forged_completion_cannot_remove_a_workspace_the_helper_never_cleared(
    manager_runtime,
) -> None:
    workspace = manager_runtime["workspace"]
    before = _tree(workspace)

    def forge_only(spec):
        with Path(spec["log_path"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(_nothing_preserved(spec)) + "\n")

    manager_runtime["unit"].update({"run_helper": False, "forge": forge_only})
    outcome = worktree_reclaim.reclaim_worktree(
        workspace, repo_root=manager_runtime["source"]
    )

    assert outcome.status == worktree_reclaim.RECLAIM_FAILED
    assert "workspace is not empty" in str(outcome.detail)
    assert _tree(workspace) == before
    assert job_workspace.read_marker(workspace) == manager_runtime["marker"]


def test_manager_half_leaves_planted_entries_and_fails_closed(manager_runtime, tmp_path: Path) -> None:
    workspace = manager_runtime["workspace"]
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (outside / "keep.txt").write_text("not evidence\n", encoding="utf-8")

    def plant(spec):
        preserve_root = Path(spec["command"][spec["command"].index("--preserve-root") + 1])
        (preserve_root / "planted").symlink_to(outside, target_is_directory=True)

    manager_runtime["unit"]["forge"] = plant
    with pytest.raises(RuntimeError, match="entries the helper does not create"):
        owner_reclaim.reclaim_through_builder_unit(
            workspace=workspace, marker=manager_runtime["marker"]
        )
    [archive] = _evidence_archives(workspace)
    assert (archive / "new-dir" / "untracked.txt").read_text() == "preserve me\n"
    assert (outside / "keep.txt").read_text() == "not evidence\n"
    assert not (owner_reclaim.reclaim_evidence_root() / "planted").exists()


def test_manager_half_validates_the_job_environment_before_any_side_effect(
    manager_runtime, monkeypatch, tmp_path: Path
) -> None:
    workspace = manager_runtime["workspace"]
    monkeypatch.setenv("PSC_BUILDER_HOME", str(tmp_path / "missing-builder-home"))

    with pytest.raises(job_runner.JobRunnerError, match="job-runner-home-missing"):
        owner_reclaim.reclaim_through_builder_unit(
            workspace=workspace, marker=manager_runtime["marker"]
        )

    assert manager_runtime["started"] == []
    for surface in ("commit-spool", "builder-codex-home", "builder-job-log"):
        assert not os.path.lexists(spool_slot.exact_job_slot(surface, workspace.name))
    assert (workspace / "new-dir" / "untracked.txt").exists()


def test_manager_half_refuses_while_a_sibling_builder_unit_holds_the_slot(
    manager_runtime, monkeypatch
) -> None:
    workspace = manager_runtime["workspace"]
    spool = manager_runtime["spool"]
    sibling = f"cortex-job-ro@{workspace.name}.service"
    monkeypatch.setattr(job_runner, "_unit_is_active", lambda _systemctl, unit: unit == sibling)

    with pytest.raises(RuntimeError, match="another builder unit for this workspace is still active"):
        owner_reclaim.reclaim_through_builder_unit(
            workspace=workspace, marker=manager_runtime["marker"]
        )

    assert manager_runtime["started"] == []
    assert not (spool / f"{workspace.name}.json").exists()
    assert (workspace / "new-dir" / "untracked.txt").exists()


def test_manager_half_surfaces_the_helper_refusal_and_keeps_the_workspace(
    manager_runtime,
) -> None:
    workspace = manager_runtime["workspace"]
    spool = manager_runtime["spool"]
    stale_snapshot = {**manager_runtime["marker"], "attempt_id": "an-earlier-attempt"}

    with pytest.raises(RuntimeError, match="marker does not match the Manager-approved digest"):
        owner_reclaim.reclaim_through_builder_unit(workspace=workspace, marker=stale_snapshot)

    assert not (spool / f"{workspace.name}.json").exists()
    assert (workspace / "new-dir" / "untracked.txt").read_text() == "preserve me\n"
    assert job_workspace.read_marker(workspace) == manager_runtime["marker"]


def test_manager_half_refuses_to_reset_an_unreviewed_reclaim_archive(manager_runtime) -> None:
    workspace = manager_runtime["workspace"]
    slot = spool_slot.exact_job_slot("commit-spool", workspace.name)
    leftover = slot / owner_reclaim.RECLAIM_ARCHIVE_DIRNAME / f"{workspace.name}-earlier"
    leftover.mkdir(parents=True)
    (leftover / "evidence.txt").write_text("from an earlier failed reclaim\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="earlier reclaim archive"):
        owner_reclaim.reclaim_through_builder_unit(
            workspace=workspace, marker=manager_runtime["marker"]
        )

    assert (leftover / "evidence.txt").read_text() == "from an earlier failed reclaim\n"
    assert manager_runtime["started"] == []
    assert (workspace / "new-dir" / "untracked.txt").exists()


def test_prepare_systemd_template_accepts_an_existing_slot_instance_verbatim(
    manager_runtime,
) -> None:
    workspace = manager_runtime["workspace"]
    plan = job_runner.prepare_systemd_template(
        os.environ,
        job_id=workspace.name,
        instance=workspace.name,
        executor="codex",
    )
    assert plan.instance == workspace.name
    assert plan.unit.endswith(f"@{workspace.name}.service")
    assert plan.spec_path.endswith(f"/{workspace.name}.json")
    derived = job_runner.prepare_systemd_template(os.environ, job_id=workspace.name, executor="codex")
    assert derived.instance != workspace.name
    with pytest.raises(job_runner.JobRunnerError):
        job_runner.prepare_systemd_template(
            os.environ, job_id=workspace.name, instance="../escape", executor="codex"
        )
