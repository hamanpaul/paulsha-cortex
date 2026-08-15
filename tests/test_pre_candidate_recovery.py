from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from paulsha_cortex.coordinator import manager, verification
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.registry import JobRegistry


def test_allowed_slice_actions_when_candidate_is_null(tmp_path: Path) -> None:
    state_path = tmp_path / "jobs.json"
    reg = JobRegistry(state_path=state_path)
    builder_job = reg.create_job(
        task="slice-null-cand",
        persona="builder",
        branch="feature/slice-null-cand",
        pane="",
        worktree=str(tmp_path / "wt" / "feature-slice-null-cand"),
    )
    reg.update_headless_result(builder_job["job_id"], status="failed", exit_code=1)
    reg.create_slice(
        slice_id="slice-null-cand",
        spec_path="specs/slice-null-cand.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-null-cand.md",
        plan_hash="plan-sha",
        target_branch="main",
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=None,
        candidate=None,
    )
    reg.update_slice("slice-null-cand", state="needs_human", gate_state="needs_human")
    slice_row = reg.get_slice("slice-null-cand")

    actions = manager.allowed_slice_actions(reg, slice_row)
    assert "recover-pre-candidate" in actions
    assert "retry-build" not in actions


def _git(repo: Path, *args: str) -> None:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"


def test_recover_pre_candidate_removes_worktree_and_resets_slice(
    tmp_path: Path, monkeypatch
) -> None:
    # issue #478：這個 fixture 原本只是一個普通暫存目錄，因此只證明得了「檔案被
    # 刪掉」、看不見 git worktree registry 殘留（正是 #478 連續四次在生產重現卻
    # 測試全綠的原因）。改用真實 repo ＋ 真實 linked worktree。
    state_path = tmp_path / "jobs.json"
    reg = JobRegistry(state_path=state_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "recover@example.invalid")
    _git(repo, "config", "user.name", "recover-test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "init")
    monkeypatch.setenv("PSC_REPO_ROOT", str(repo))
    wt_dir = tmp_path / "wt" / "feature-slice-3a"
    _git(repo, "worktree", "add", "-q", str(wt_dir), "-b", "feature/slice-3a")
    (wt_dir / "leftover.txt").write_text("residual", encoding="utf-8")

    builder_job = reg.create_job(
        task="slice-3a",
        persona="builder",
        branch="feature/slice-3a",
        pane="",
        worktree=str(wt_dir),
    )
    reg.update_headless_result(builder_job["job_id"], status="failed", exit_code=1)

    reg.create_slice(
        slice_id="slice-3a",
        spec_path="specs/slice-3a.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-3a.md",
        plan_hash="plan-sha",
        target_branch="main",
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=None,
        candidate=None,
    )
    reg.update_slice("slice-3a", state="needs_human", gate_state="needs_human")

    pane_sender = MagicMock()
    wt_creator = MagicMock()
    dispatcher = Dispatcher(reg, pane_sender=pane_sender, worktree_creator=wt_creator)

    res = manager.apply_slice_action(
        dispatcher=dispatcher,
        slice_id="slice-3a",
        action="recover-pre-candidate",
        actor="test-operator",
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
    )

    assert res["result"] == "ok"
    assert not wt_dir.exists()
    listing = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout
    assert str(wt_dir) not in listing
    latest_slice = reg.get_slice("slice-3a")
    assert latest_slice["state"] == "pending"
    assert latest_slice["gate_state"] == "pending"


def test_recover_pre_candidate_supersedes_stale_handoff_manifest(tmp_path: Path) -> None:
    # issue #383：recover-pre-candidate 撥回 pending 之後，殘留的舊終局 handoff
    # manifest 應被標記 superseded（稽核可見性）——run_tick 的放行判定本身已改成
    # 跟 registry 現況對帳（不依賴這個標記，見 dispatch_gate_scan/
    # _manifest_still_blocks_fanout），這裡單獨驗證標記本身確實落地。
    state_path = tmp_path / "jobs.json"
    reg = JobRegistry(state_path=state_path)
    builder_job = reg.create_job(
        task="slice-super",
        persona="builder",
        branch="feature/slice-super",
        pane="",
        worktree=str(tmp_path / "wt" / "feature-slice-super"),
    )
    reg.update_headless_result(builder_job["job_id"], status="failed", exit_code=1)
    reg.create_slice(
        slice_id="slice-super",
        spec_path="specs/slice-super.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-super.md",
        plan_hash="plan-sha",
        target_branch="main",
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=None,
        candidate=None,
    )
    reg.update_slice("slice-super", state="needs_human", gate_state="needs_human")

    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = handoff_dir / "slice-super.json"
    manifest_path.write_text(
        json.dumps({"slice_id": "slice-super", "job_id": builder_job["job_id"], "gate_status": "failed"}),
        encoding="utf-8",
    )

    pane_sender = MagicMock()
    wt_creator = MagicMock()
    dispatcher = Dispatcher(reg, pane_sender=pane_sender, worktree_creator=wt_creator)

    res = manager.apply_slice_action(
        dispatcher=dispatcher,
        slice_id="slice-super",
        action="recover-pre-candidate",
        actor="test-operator",
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(handoff_dir),
    )

    assert res["result"] == "ok"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_status"] == "failed"
    assert manifest["superseded_by"] == "test-operator"
    assert manifest["superseded_reason"] == "operator-recover-pre-candidate"
    assert isinstance(manifest["superseded_at"], str) and manifest["superseded_at"]


def test_recover_pre_candidate_fail_closed_when_candidate_exists(tmp_path: Path) -> None:
    state_path = tmp_path / "jobs.json"
    reg = JobRegistry(state_path=state_path)
    valid_candidate = "a" * 40
    builder_job = reg.create_job(
        task="slice-with-cand",
        persona="builder",
        branch="feature/slice-with-cand",
        pane="",
        worktree=str(tmp_path / "wt" / "feature-slice-with-cand"),
    )
    reg.create_slice(
        slice_id="slice-with-cand",
        spec_path="specs/slice-with-cand.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-with-cand.md",
        plan_hash="plan-sha",
        target_branch="main",
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=None,
        candidate=valid_candidate,
    )
    reg.update_slice("slice-with-cand", state="needs_human", gate_state="needs_human")
    slice_row = reg.get_slice("slice-with-cand")

    actions = manager.allowed_slice_actions(reg, slice_row)
    assert "recover-pre-candidate" not in actions

    pane_sender = MagicMock()
    wt_creator = MagicMock()
    dispatcher = Dispatcher(reg, pane_sender=pane_sender, worktree_creator=wt_creator)

    with pytest.raises(ValueError, match="action-not-allowed:recover-pre-candidate"):
        manager.apply_slice_action(
            dispatcher=dispatcher,
            slice_id="slice-with-cand",
            action="recover-pre-candidate",
            actor="test-operator",
            specs_dir=str(tmp_path / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
        )


def test_candidate_worktree_dirty_reevaluation_on_tick(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(manager, "_pinned_input_mismatches", lambda _slice: [])

    state_path = tmp_path / "jobs.json"
    reg = JobRegistry(state_path=state_path)
    stale_candidate = "0" * 40
    new_candidate = "1" * 40

    builder_job = reg.create_job(
        task="slice-3b",
        persona="builder",
        branch="feature/slice-3b",
        pane="",
        worktree=str(tmp_path / "wt" / "feature-slice-3b"),
    )
    reg.update_headless_result(builder_job["job_id"], status="exited", exit_code=0)

    reg.create_slice(
        slice_id="slice-3b",
        spec_path="specs/slice-3b.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-3b.md",
        plan_hash="plan-sha",
        target_branch="main",
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=None,
        candidate=stale_candidate,
        verification={"hash": "ver-hash", "contract": {"docs_class": "trivial", "review_policy": "not-required"}},
    )

    dirty_payload = {
        "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
        "slice_id": "slice-3b",
        "candidate": stale_candidate,
        "status": "needs_human",
        "summary": "candidate-worktree-dirty",
        "details": {},
    }
    dirty_evidence = verification.write_verification_evidence(
        dirty_payload,
        coordinator_root=tmp_path,
    )
    reg.update_slice(
        "slice-3b",
        state="needs_human",
        gate_state="needs_human",
        verification_hash=dirty_evidence["hash"],
        current_evidence_refs=[dirty_evidence["path"]],
    )

    def fake_verification_runner(*_args, **_kwargs):
        succeeded_payload = {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": "slice-3b",
            "candidate": new_candidate,
            "status": "verified",
            "summary": "verification-succeeded",
            "details": {},
        }
        return verification.write_verification_evidence(
            succeeded_payload,
            coordinator_root=tmp_path,
        )

    pane_sender = MagicMock()
    wt_creator = MagicMock()
    dispatcher = Dispatcher(reg, pane_sender=pane_sender, worktree_creator=wt_creator)

    manager.complete_tick(
        dispatcher,
        handoff_dir=str(tmp_path / "handoff"),
        verification_runner=fake_verification_runner,
    )

    updated_slice = reg.get_slice("slice-3b")
    assert updated_slice["candidate"] == new_candidate
    assert updated_slice["state"] in {"verified", "completed"}
