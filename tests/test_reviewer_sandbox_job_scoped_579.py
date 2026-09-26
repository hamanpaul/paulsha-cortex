"""#579：reviewer sandbox 名稱必須綁 job，authority restart 重派前要回收前代 era 孤兒 sandbox。"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager, planning_runtime

from test_reviewer_card_retry_569 import (  # noqa: E402
    _ReviewLauncher,
    _reviewer_identities,
    _stuck_reviewer_run,
)


def _legacy_sandbox_name(*, run_id: str, card: str, candidate: str) -> str:
    return hashlib.sha256(f"{run_id}:{card}:{candidate}".encode()).hexdigest()[:32]


def _v2_sandbox_name(*, run_id: str, card: str, candidate: str, job_id: str) -> str:
    return hashlib.sha256(
        f"{run_id}:{card}:{candidate}:{job_id}".encode()
    ).hexdigest()[:32]


def _reviewer_job(
    run,
    *,
    candidate_root: Path,
    worktree: Path,
    job_id: str,
    claim_key: str | None,
    status: str = "exited",
    card: str = "verification",
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "persona": "reviewer",
        "status": status,
        "worktree": str(worktree),
        "workflow_run_id": run.run_id,
        "workflow_claim_key": claim_key,
        "workflow_card": card,
        "workflow_phase": "verify",
        "workflow_repo_root": str(candidate_root.resolve()),
        "workflow_input_root": str(worktree),
        "workflow_sandbox_hash": planning_runtime._tree_snapshot(candidate_root.resolve()),
        "subject_head": run.candidate_head,
        "workflow_evidence": None,
    }


def _dispatcher(registry):
    return type("D", (), {"_registry": registry, "_git_runner": None})()


def test_create_reviewer_sandbox_uses_job_scoped_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同 run／card／candidate 的 reviewer job，要靠 job_id 拿到兩個不同 sandbox。"""

    monkeypatch.setattr(manager, "_grant_reviewer_sandbox_access", lambda _sandbox: None)
    _snapshot, _registry, run, _job_id = _stuck_reviewer_run(tmp_path, with_git=True)
    step = SimpleNamespace(card="verification")
    candidate_root = Path(run.workspace_root)
    coordinator_root = tmp_path / "coordinator"
    sandboxes: list[Path] = []

    try:
        first, first_checkout = manager._create_reviewer_sandbox(
            run=run,
            step=step,
            executor="codex",
            candidate_root=candidate_root,
            coordinator_root=coordinator_root,
            input_snapshot=(),
            job_id="wf-reviewer-1",
        )
        sandboxes.append(first)
        second, second_checkout = manager._create_reviewer_sandbox(
            run=run,
            step=step,
            executor="codex",
            candidate_root=candidate_root,
            coordinator_root=coordinator_root,
            input_snapshot=(),
            job_id="wf-reviewer-2",
        )
        sandboxes.append(second)

        assert first != second
        assert first_checkout == first
        assert second_checkout == second
        assert first.is_dir()
        assert second.is_dir()
        assert first.name == manager._reviewer_sandbox_name(
            run_id=run.run_id,
            card=step.card,
            candidate=run.candidate_head,
            job_id="wf-reviewer-1",
        )
        assert second.name == manager._reviewer_sandbox_name(
            run_id=run.run_id,
            card=step.card,
            candidate=run.candidate_head,
            job_id="wf-reviewer-2",
        )
    finally:
        for sandbox in sandboxes:
            shutil.rmtree(sandbox, ignore_errors=True)


def test_reviewer_sandbox_path_accepts_own_v2_name_and_rejects_foreign_or_missing_job_id(
    tmp_path: Path,
) -> None:
    _snapshot, _registry, run, _job_id = _stuck_reviewer_run(tmp_path, with_git=True)
    candidate_root = Path(run.workspace_root)
    coordinator_root = tmp_path / "coordinator"
    parent = manager._reviewer_sandbox_parent(
        coordinator_root=coordinator_root,
        candidate_root=candidate_root,
    )
    own = _reviewer_job(
        run,
        candidate_root=candidate_root,
        worktree=parent
        / _v2_sandbox_name(
            run_id=run.run_id,
            card="verification",
            candidate=run.candidate_head,
            job_id="wf-reviewer-1",
        ),
        job_id="wf-reviewer-1",
        claim_key=run.claim_key,
    )

    assert manager._reviewer_sandbox_path(own, coordinator_root) == Path(own["worktree"])

    foreign = {
        **own,
        "worktree": str(
            parent
            / _v2_sandbox_name(
                run_id=run.run_id,
                card="verification",
                candidate=run.candidate_head,
                job_id="wf-reviewer-2",
            )
        ),
    }
    with pytest.raises(ValueError, match="reviewer sandbox path invalid"):
        manager._reviewer_sandbox_path(foreign, coordinator_root)

    missing = dict(own)
    missing.pop("job_id")
    with pytest.raises(ValueError, match="reviewer sandbox identity missing"):
        manager._reviewer_sandbox_path(missing, coordinator_root)


def test_legacy_reviewer_sandbox_name_still_validates_and_cleans_up(tmp_path: Path) -> None:
    _snapshot, _registry, run, _job_id = _stuck_reviewer_run(tmp_path, with_git=True)
    candidate_root = Path(run.workspace_root)
    coordinator_root = tmp_path / "coordinator"
    parent = manager._reviewer_sandbox_parent(
        coordinator_root=coordinator_root,
        candidate_root=candidate_root,
    )
    sandbox = parent / _legacy_sandbox_name(
        run_id=run.run_id,
        card="verification",
        candidate=run.candidate_head,
    )
    sandbox.mkdir(parents=True, exist_ok=True)
    job = _reviewer_job(
        run,
        candidate_root=candidate_root,
        worktree=sandbox,
        job_id="wf-reviewer-legacy",
        claim_key=run.claim_key,
    )

    try:
        assert manager._reviewer_sandbox_path(job, coordinator_root) == sandbox
        manager._discard_reviewer_sandbox(
            job,
            coordinator_root=coordinator_root,
            require_candidate_unchanged=False,
        )
        assert not sandbox.exists()
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_authority_restart_dispatch_reclaims_a_legacy_reviewer_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager, "_grant_reviewer_sandbox_access", lambda _sandbox: None)
    _snapshot, registry, run, old_job_id = _stuck_reviewer_run(tmp_path, with_git=True)
    candidate_root = Path(run.workspace_root)
    coordinator_root = tmp_path / "coordinator"
    sandbox = manager._reviewer_sandbox_parent(
        coordinator_root=coordinator_root,
        candidate_root=candidate_root,
    ) / _legacy_sandbox_name(
        run_id=run.run_id,
        card="verification",
        candidate=run.candidate_head,
    )
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "stale-marker").write_text("x", encoding="utf-8")
    registry._find_job(old_job_id).update(
        {
            "worktree": str(sandbox),
            "workflow_repo_root": str(candidate_root.resolve()),
            "workflow_input_root": str(sandbox),
            "workflow_sandbox_hash": planning_runtime._tree_snapshot(
                candidate_root.resolve()
            ),
        }
    )
    registry._persist()

    restarted = registry._manager_reset_workflow_for_authority_restart(
        run.run_id, authority_digest="e" * 64
    )
    launched: list[tuple[str, str]] = []

    replacement = manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=restarted,
        identities=_reviewer_identities(),
        launcher_factory=lambda _identity: _ReviewLauncher(launched),
        coordinator_root=coordinator_root,
    )

    assert replacement is not None
    assert replacement["job_id"] != old_job_id
    assert Path(replacement["worktree"]).name == manager._reviewer_sandbox_name(
        run_id=restarted.run_id,
        card="verification",
        candidate=restarted.candidate_head,
        job_id=str(replacement["job_id"]),
    )
    assert Path(replacement["worktree"]).is_dir()
    assert not sandbox.exists()
    assert registry.get_job(old_job_id)["status"] == "exited"
    assert registry.get_job(old_job_id)["workflow_evidence"] is None


def test_authority_restart_dispatch_warns_but_keeps_going_when_reclaim_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager, "_grant_reviewer_sandbox_access", lambda _sandbox: None)
    _snapshot, registry, run, old_job_id = _stuck_reviewer_run(tmp_path, with_git=True)
    candidate_root = Path(run.workspace_root)
    coordinator_root = tmp_path / "coordinator"
    sandbox = manager._reviewer_sandbox_parent(
        coordinator_root=coordinator_root,
        candidate_root=candidate_root,
    ) / _legacy_sandbox_name(
        run_id=run.run_id,
        card="verification",
        candidate=run.candidate_head,
    )
    sandbox.mkdir(parents=True, exist_ok=True)
    registry._find_job(old_job_id).update(
        {
            "worktree": str(sandbox),
            "workflow_input_root": str(sandbox),
            "workflow_sandbox_hash": planning_runtime._tree_snapshot(
                candidate_root.resolve()
            ),
        }
    )
    registry._find_job(old_job_id).pop("workflow_repo_root", None)
    registry._persist()

    restarted = registry._manager_reset_workflow_for_authority_restart(
        run.run_id, authority_digest="e" * 64
    )

    with caplog.at_level(logging.WARNING, logger=manager.logger.name):
        replacement = manager.dispatch_workflow_card(
            _dispatcher(registry),
            run=restarted,
            identities=_reviewer_identities(),
            launcher_factory=lambda _identity: _ReviewLauncher([]),
            coordinator_root=coordinator_root,
        )

    assert replacement is not None
    assert Path(str(replacement["worktree"])).name == manager._reviewer_sandbox_name(
        run_id=restarted.run_id,
        card="verification",
        candidate=restarted.candidate_head,
        job_id=str(replacement["job_id"]),
    )
    assert sandbox.is_dir()
    assert any(
        record.levelno == logging.WARNING and old_job_id in record.getMessage()
        for record in caplog.records
    )


def test_reclaim_superseded_era_reviewer_sandboxes_only_drops_terminal_unique_paths(
    tmp_path: Path,
) -> None:
    _snapshot, _registry, run, _job_id = _stuck_reviewer_run(tmp_path, with_git=True)
    candidate_root = Path(run.workspace_root)
    coordinator_root = tmp_path / "coordinator"
    parent = manager._reviewer_sandbox_parent(
        coordinator_root=coordinator_root,
        candidate_root=candidate_root,
    )

    shared = parent / _legacy_sandbox_name(
        run_id=run.run_id,
        card="verification",
        candidate=run.candidate_head,
    )
    running = parent / _v2_sandbox_name(
        run_id=run.run_id,
        card="verification",
        candidate=run.candidate_head,
        job_id="wf-running",
    )
    no_claim = parent / _v2_sandbox_name(
        run_id=run.run_id,
        card="verification",
        candidate=run.candidate_head,
        job_id="wf-no-claim",
    )
    failed = parent / _v2_sandbox_name(
        run_id=run.run_id,
        card="verification",
        candidate=run.candidate_head,
        job_id="wf-failed",
    )
    for sandbox in (shared, running, no_claim, failed):
        sandbox.mkdir(parents=True, exist_ok=True)

    jobs = [
        _reviewer_job(
            run,
            candidate_root=candidate_root,
            worktree=running,
            job_id="wf-running",
            claim_key="claim:v1:" + "1" * 64,
            status="running",
        ),
        _reviewer_job(
            run,
            candidate_root=candidate_root,
            worktree=shared,
            job_id="wf-current",
            claim_key=run.claim_key,
        ),
        _reviewer_job(
            run,
            candidate_root=candidate_root,
            worktree=no_claim,
            job_id="wf-no-claim",
            claim_key=None,
        ),
        _reviewer_job(
            run,
            candidate_root=candidate_root,
            worktree=shared,
            job_id="wf-shared-old",
            claim_key="claim:v1:" + "2" * 64,
        ),
        _reviewer_job(
            run,
            candidate_root=candidate_root,
            worktree=failed,
            job_id="wf-failed",
            claim_key="claim:v1:" + "3" * 64,
            status="failed",
        ),
    ]

    manager._reclaim_superseded_era_reviewer_sandboxes(
        jobs,
        run=run,
        coordinator_root=coordinator_root,
    )

    assert shared.is_dir()
    assert running.is_dir()
    assert no_claim.is_dir()
    assert not failed.exists()
