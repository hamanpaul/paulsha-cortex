"""#216：retry 分類補齊（authority_restart／review_handoff_failure／
source_owner_repair）與精準 invalidation（retry-build／retry-verify／
retry-review／authority restart）。

驗收條件對應：
- AC1：retry-build 只重跑 builder，invalidate candidate 相依的 verify/review
       （regression：既有 #215 行為，本檔補一條聚焦測試鎖住）
- AC2：retry-verify candidate 不變時只重跑 verification，不重建 candidate
- AC3：retry-review 不重跑 builder；缺 frozen plan 時 pre-dispatch fail
- AC4：source-owner／claim sequencing repair 不觸發 builder
- AC5：authority restart 只 invalidate 依賴已變更 authority hash 的 stage
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority, WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason


HEAD = "b" * 40
NOW = "2026-07-27T00:00:00+00:00"

_PERSONA_BY_PHASE = {
    "claim": "manager",
    "define": "planner",
    "plan": "planner",
    "build": "builder",
    "verify": "reviewer",
    "review": "reviewer",
    "ship": "manager",
}


def _step(phase: str, card: str, *, gate_result: str = "pending") -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona=_PERSONA_BY_PHASE[phase],
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        gate_result=gate_result,
    )


def _init_repo(root: Path, repo: str = "acme/demo") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", f"git@github.com:{repo}.git"],
            check=True,
        )
    return root


def _snapshot(path: Path, *, source_revisions: list[str] | None = None) -> Path:
    _init_repo(path.parent)
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-1",
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "mapped_issues": [12],
                        "mapped_prs": [8],
                        "mapped_openspec": ["demo"],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": source_revisions
                        or ["issue:12@open", "openspec:demo@1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _authority(tmp_path: Path, *, source_revisions: list[str] | None = None):
    snapshot = _snapshot(tmp_path / "snapshot.json", source_revisions=source_revisions)
    return work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    ), snapshot


def _make_run(
    registry: JobRegistry,
    *,
    authority,
    claim_key: str,
    current_phase: str,
    steps: tuple[WorkflowStep, ...],
    candidate_head: str | None = None,
    verified_head: str | None = None,
    facets: tuple[str, ...] = (),
    planning_authority: tuple[PlanningArtifactAuthority, ...] = (),
    source_revision: str | None = None,
):
    return registry._manager_create_workflow_run(
        work_id=authority.work_id,
        repo=authority.repo,
        claim_key=claim_key,
        source_revision=source_revision or work_actions.work_authority_digest(authority),
        workspace_root="/tmp/workspace",
        combo="feature-oneshot",
        current_phase=current_phase,
        steps=steps,
        issue_refs=tuple(f"{authority.repo}#{n}" for n in authority.mapped_issues),
        openspec_refs=authority.mapped_openspec,
        candidate_head=candidate_head,
        verified_head=verified_head,
        facets=facets,
        needs_human_reason=(
            fixture_needs_human_reason() if "needs_human" in facets else None
        ),
        gate_status="running",
        planning_authority=planning_authority,
    )


def _base_steps(*, verify_result: str, review_result: str) -> tuple[WorkflowStep, ...]:
    return (
        _step("claim", "manager-claim", gate_result="passed"),
        _step("define", "planner-define", gate_result="passed"),
        _step("plan", "planner-plan", gate_result="passed"),
        _step("build", "subagent-build", gate_result="passed"),
        _step("verify", "reviewer-verify", gate_result=verify_result),
        _step("review", "reviewer-review", gate_result=review_result),
        _step("ship", "manager-ship", gate_result="pending"),
    )


# ---------------------------------------------------------------------------
# AC1（regression）：retry-build 只重跑 builder，invalidate verify/review
# ---------------------------------------------------------------------------


def test_retry_build_invalidates_verify_and_review_but_reruns_only_builder(
    tmp_path: Path,
) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    steps = _base_steps(verify_result="passed", review_result="passed")
    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="review",
        steps=steps,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=("needs_human",),
    )
    result = work_actions.execute_work_action(
        args={
            "action": "retry-build",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_candidate": HEAD,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )
    updated = result["result"]["run"]
    assert updated["current_phase"] == "build"
    by_phase = {step["phase"]: step for step in updated["steps"]}
    assert by_phase["build"]["gate_result"] == "pending"
    assert by_phase["verify"]["gate_result"] == "pending"
    assert by_phase["review"]["gate_result"] == "pending"
    assert updated["verified_head"] is None
    assert result["result"]["retry_classification"] == "model_repair"
    # #216 追加：分類同步持久化到 WorkflowRun 本身（供 completion draft 讀取）。
    assert updated["retry_classification"] == "model_repair"


# ---------------------------------------------------------------------------
# AC2：retry-verify 候選不變時只重跑 verification，不重建 candidate
# ---------------------------------------------------------------------------


def test_retry_verify_reruns_only_verification_without_rebuilding_candidate(
    tmp_path: Path,
) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    steps = _base_steps(verify_result="needs_human", review_result="pending")
    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="verify",
        steps=steps,
        candidate_head=HEAD,
        facets=("needs_human",),
    )
    result = work_actions.execute_work_action(
        args={
            "action": "retry-verify",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_candidate": HEAD,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )
    updated = result["result"]["run"]
    assert updated["current_phase"] == "verify"
    by_phase = {step["phase"]: step for step in updated["steps"]}
    # build 完全不動：still "passed"，attempts 未增加
    assert by_phase["build"]["gate_result"] == "passed"
    assert run.attempts.get("build", 0) == updated["attempts"].get("build", 0)
    assert by_phase["verify"]["gate_result"] == "pending"
    assert updated["candidate_head"] == HEAD
    assert "needs_human" not in updated["facets"]
    # candidate 完全不變的 verification 重跑不是模型修復（#208 根因3）：
    # 不得計入 model failure 指標，也不得吃 #218 的 repair budget。
    assert result["result"]["retry_classification"] == "orchestrator_retry"
    assert updated["retry_classification"] == "orchestrator_retry"


def test_retry_verify_rejects_candidate_mismatch(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    steps = _base_steps(verify_result="needs_human", review_result="pending")
    _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="verify",
        steps=steps,
        candidate_head=HEAD,
        facets=("needs_human",),
    )
    with pytest.raises(RuntimeError, match="Candidate CAS mismatch"):
        work_actions.execute_work_action(
            args={
                "action": "retry-verify",
                "repo": "acme/demo",
                "work_id": "demo",
                "issue": 12,
                "actor": "operator",
                "expected_candidate": "c" * 40,
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
        )


def test_retry_verify_rejects_non_verify_phase(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    steps = _base_steps(verify_result="passed", review_result="needs_human")
    _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="review",
        steps=steps,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=("needs_human",),
    )
    with pytest.raises(RuntimeError, match="requires verify-phase workflow"):
        work_actions.execute_work_action(
            args={
                "action": "retry-verify",
                "repo": "acme/demo",
                "work_id": "demo",
                "issue": 12,
                "actor": "operator",
                "expected_candidate": HEAD,
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
        )


# ---------------------------------------------------------------------------
# AC3：retry-review 不重跑 builder；缺 frozen plan 時 pre-dispatch fail
# ---------------------------------------------------------------------------


def test_retry_review_reruns_only_review_without_rebuilding_or_reverifying(
    tmp_path: Path,
) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    steps = _base_steps(verify_result="passed", review_result="needs_human")
    plan_authority = (
        PlanningArtifactAuthority(
            ref="docs/superpowers/plans/demo.md",
            kind="plan",
            work_id="demo",
            baseline_sha256="a" * 64,
        ),
    )
    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="review",
        steps=steps,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=("needs_human",),
        planning_authority=plan_authority,
    )
    result = work_actions.execute_work_action(
        args={
            "action": "retry-review",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_candidate": HEAD,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )
    updated = result["result"]["run"]
    assert updated["current_phase"] == "review"
    by_phase = {step["phase"]: step for step in updated["steps"]}
    assert by_phase["build"]["gate_result"] == "passed"
    assert by_phase["verify"]["gate_result"] == "passed"
    assert by_phase["review"]["gate_result"] == "pending"
    assert run.attempts.get("verify", 0) == updated["attempts"].get("verify", 0)
    assert updated["candidate_head"] == HEAD
    assert updated["verified_head"] == HEAD
    # 重跑 review 本身即是 review 交接修復：candidate 未變，非 model repair。
    assert result["result"]["retry_classification"] == "review_handoff_failure"
    assert updated["retry_classification"] == "review_handoff_failure"


def test_retry_review_without_frozen_plan_fails_pre_dispatch(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    steps = _base_steps(verify_result="passed", review_result="needs_human")
    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="review",
        steps=steps,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=("needs_human",),
        planning_authority=(),  # 沒有冷凍 plan authority
    )
    with pytest.raises(RuntimeError, match="requires frozen plan authority"):
        work_actions.execute_work_action(
            args={
                "action": "retry-review",
                "repo": "acme/demo",
                "work_id": "demo",
                "issue": 12,
                "actor": "operator",
                "expected_candidate": HEAD,
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
        )
    # pre-dispatch fail：完全沒有狀態變更
    unchanged = registry.get_workflow_run(run.run_id)
    assert unchanged.current_phase == "review"
    assert unchanged.facets == ("needs_human",)
    review_step = next(step for step in unchanged.steps if step.phase == "review")
    assert review_step.gate_result == "needs_human"


# ---------------------------------------------------------------------------
# AC4：source-owner／claim sequencing repair 不觸發 builder
# ---------------------------------------------------------------------------


def test_source_owner_conflict_blocks_before_any_builder_dispatch(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    calls: list[tuple[str, str, str | None]] = []

    def conflicting_starter(bound_authority, claim_key, reason):
        calls.append((bound_authority.work_id, claim_key, reason))
        raise RuntimeError(
            "source-owner transfer incomplete: source-owner-old still owns an overlapping issue"
        )

    result = work_actions.execute_work_action(
        args={
            "action": "start",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
        workflow_starter=conflicting_starter,
        now=lambda: 200,
    )
    assert result["result"] == {
        "action": "blocked",
        "reason": "source-owner-repair-pending",
        "run": None,
        "retry_classification": "source_owner_repair",
    }
    # 從未成功建立任何 WorkflowRun（也就從未派過任何 builder job）。
    assert list(registry.list_workflow_runs()) == []
    assert len(calls) == 1


def test_unrelated_runtime_error_from_starter_still_propagates(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    def broken_starter(bound_authority, claim_key, reason):
        raise RuntimeError("unrelated infra failure")

    with pytest.raises(RuntimeError, match="unrelated infra failure"):
        work_actions.execute_work_action(
            args={
                "action": "start",
                "repo": "acme/demo",
                "work_id": "demo",
                "issue": 12,
                "actor": "operator",
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
            workflow_starter=broken_starter,
            now=lambda: 200,
        )


# ---------------------------------------------------------------------------
# AC5：authority restart 只 invalidate 依賴已變更 authority hash 的 stage
# ---------------------------------------------------------------------------


def test_authority_restart_invalidates_only_verify_and_review_preserving_build(
    tmp_path: Path,
) -> None:
    old_authority, _ = _authority(tmp_path, source_revisions=["issue:12@open", "openspec:demo@1"])
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(old_authority)
    steps = _base_steps(verify_result="passed", review_result="passed")
    run = _make_run(
        registry,
        authority=old_authority,
        claim_key=claim_key,
        current_phase="review",
        steps=steps,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=(),
    )
    # WorkAuthority 宣告改變（issue 內容更新）——重新讀取新版 snapshot。
    new_snapshot = _snapshot(
        tmp_path / "snapshot.json",
        source_revisions=["issue:12@updated", "openspec:demo@1"],
    )
    new_authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=new_snapshot
    )
    assert work_actions._expected_claim_key(new_authority) != claim_key

    result = work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo", "issue": 12},
        requested_by="operator",
        snapshot_path=new_snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )
    assert result["result"]["action"] == "resume"
    run_payload = result["result"]["run"]
    assert run_payload["current_phase"] == "verify"
    by_phase = {step["phase"]: step for step in run_payload["steps"]}
    assert by_phase["build"]["gate_result"] == "passed"  # candidate 保持不變
    assert by_phase["verify"]["gate_result"] == "pending"
    assert by_phase["review"]["gate_result"] == "pending"
    assert run_payload["candidate_head"] == HEAD  # candidate 沒被重建
    assert run_payload["verified_head"] is None
    assert run_payload["retry_classification"] == "authority_restart"
    assert run_payload["source_revision"] == work_actions.work_authority_digest(new_authority)


def test_authority_restart_does_not_invalidate_build_phase_run(tmp_path: Path) -> None:
    """build phase（candidate 尚未產出下游可評估內容）不套用 authority-restart
    精準 invalidation——沒有 verify/review 可 invalidate，維持原樣 resume。"""

    old_authority, _ = _authority(tmp_path, source_revisions=["issue:12@open", "openspec:demo@1"])
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(old_authority)
    steps = _base_steps(verify_result="pending", review_result="pending")
    _make_run(
        registry,
        authority=old_authority,
        claim_key=claim_key,
        current_phase="build",
        steps=steps,
        facets=(),
    )
    new_snapshot = _snapshot(
        tmp_path / "snapshot.json",
        source_revisions=["issue:12@updated", "openspec:demo@1"],
    )
    result = work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo", "issue": 12},
        requested_by="operator",
        snapshot_path=new_snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )
    run_payload = result["result"]["run"]
    assert run_payload["current_phase"] == "build"
    assert run_payload.get("retry_classification") is None


# ---------------------------------------------------------------------------
# #373：authority restart 必須同步更新 claim_key，否則
# work_actions._claim_action 的 mismatch 觸發條件（canonical_run.claim_key !=
# _expected_claim_key(authority)）永久為真——每次 automatic scan 都重新觸發
# reset，剝除 needs_human、改寫 source_revision、attempts["verify"] 無界累加，
# 進而讓 manager.resume_workflow_run 撞上 workflow job binding mismatch，形成
# 永久重觸發迴圈。
# ---------------------------------------------------------------------------


def test_authority_restart_reset_syncs_claim_key_to_new_authority(tmp_path: Path) -> None:
    """#373 根因（最小重現）：_manager_reset_workflow_for_authority_restart 只
    改寫 source_revision，從未同步 claim_key。"""

    old_authority, _ = _authority(tmp_path, source_revisions=["issue:12@open", "openspec:demo@1"])
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    old_claim_key = work_actions._expected_claim_key(old_authority)
    steps = _base_steps(verify_result="passed", review_result="passed")
    run = _make_run(
        registry,
        authority=old_authority,
        claim_key=old_claim_key,
        current_phase="review",
        steps=steps,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=(),
    )
    new_snapshot = _snapshot(
        tmp_path / "snapshot.json",
        source_revisions=["issue:12@updated", "openspec:demo@1"],
    )
    new_authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=new_snapshot
    )
    new_digest = work_actions.work_authority_digest(new_authority)
    expected_new_claim_key = work_actions._expected_claim_key(new_authority)
    assert expected_new_claim_key != old_claim_key

    updated = registry._manager_reset_workflow_for_authority_restart(
        run.run_id, authority_digest=new_digest
    )

    assert updated.source_revision == new_digest
    # 根因斷言：reset 後 claim_key 必須跟著更新到新 authority 對應的 expected
    # key，否則下一次 automatic scan 的 mismatch 判定永遠為真。
    assert updated.claim_key == expected_new_claim_key


def test_repeated_automatic_scan_on_unchanged_authority_does_not_retrigger_restart(
    tmp_path: Path,
) -> None:
    """#373 迴圈重現：同一 authority digest 下，重複的 automatic scan（模擬多次
    daemon tick）不得每次都重新觸發 authority-restart reset——否則 needs_human
    facet 每 tick 被剝除、attempts["verify"] 無界累加、workflow job binding
    mismatch 永久重複（見 issue #373 2026-08-10 comment 的完整迴圈）。"""

    old_authority, _ = _authority(tmp_path, source_revisions=["issue:12@open", "openspec:demo@1"])
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    old_claim_key = work_actions._expected_claim_key(old_authority)
    steps = _base_steps(verify_result="passed", review_result="passed")
    run = _make_run(
        registry,
        authority=old_authority,
        claim_key=old_claim_key,
        current_phase="review",
        steps=steps,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=(),
    )
    new_snapshot = _snapshot(
        tmp_path / "snapshot.json",
        source_revisions=["issue:12@updated", "openspec:demo@1"],
    )
    new_authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=new_snapshot
    )

    # Tick 1：authority 真的變了（issue 內容更新），第一次 restart 合法觸發。
    tick1 = work_actions._claim_action(
        args={"action": "auto-scan"},
        authority=new_authority,
        now_epoch=200,
        state_path=tmp_path / "runs.json",
        automatic=True,
        auto_label=True,
        workflow_registry=registry,
    )
    assert tick1["action"] == "resume"
    assert tick1["run"]["retry_classification"] == "authority_restart"
    after_tick1 = registry.get_workflow_run(run.run_id)
    attempts_after_tick1 = after_tick1.attempts.get("verify", 0)
    assert attempts_after_tick1 >= 1
    assert after_tick1.claim_key == work_actions._expected_claim_key(new_authority)

    # 模擬 manager.py `_job_for_workflow_card` 撞上 workflow job binding
    # mismatch 後，manager_daemon.py 的 except handler 把 needs_human 寫回
    # （見 manager_daemon.py 的 resume 迴圈 except 分支）。
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=fixture_needs_human_reason(),
    )

    # Tick 2～4：authority 完全沒再變（模擬後續多次 daemon tick，snapshot 沒
    # 有新變更）。根治後：claim_key 已與 tick1 的新 authority 同步，mismatch
    # 判定不再為真——不再重新觸發 restart，needs_human 不被無限剝除，
    # attempts 不再無界累加。
    for _ in range(3):
        tick_result = work_actions._claim_action(
            args={"action": "auto-scan"},
            authority=new_authority,
            now_epoch=200,
            state_path=tmp_path / "runs.json",
            automatic=True,
            auto_label=True,
            workflow_registry=registry,
        )
        current = registry.get_workflow_run(run.run_id)
        assert tick_result["action"] == "needs_human"
        assert "needs_human" in current.facets
        assert current.attempts.get("verify", 0) == attempts_after_tick1
        assert current.claim_key == work_actions._expected_claim_key(new_authority)


# ---------------------------------------------------------------------------
# 追加：retry_classification 在 WorkflowRun 上的 provenance 語意（供
# work_bridge._completion_draft 讀取寫入 CompletionRecord，maintainer 追加
# 派工 1）——一般 phase 推進（_manager_update_workflow_run）保持既有值不變，
# 直到下一次 retry 明確覆寫。
# ---------------------------------------------------------------------------


def test_retry_classification_persists_across_normal_workflow_updates(tmp_path: Path) -> None:
    authority, _ = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    steps = _base_steps(verify_result="needs_human", review_result="pending")
    run = _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="verify",
        steps=steps,
        candidate_head=HEAD,
        facets=("needs_human",),
    )
    reset = registry._manager_reset_workflow_for_retry_verify(
        run.run_id,
        expected_candidate=HEAD,
        retry_classification="model_repair",
    )
    assert reset.retry_classification == "model_repair"
    # 一般更新（例如 verify 通過推進到 review）不帶 retry_classification 參數時
    # 維持既有值——不會被悄悄清成 None。
    advanced = registry._manager_update_workflow_run(
        run.run_id,
        current_phase="review",
    )
    assert advanced.retry_classification == "model_repair"


# ---------------------------------------------------------------------------
# _classify_retry(trigger=...)：#216 補齊三類判準（直接指定，不靠狀態反推）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        ("authority-restart", work_actions.RetryClassification.AUTHORITY_RESTART),
        ("review-handoff-failure", work_actions.RetryClassification.REVIEW_HANDOFF_FAILURE),
        ("source-owner-repair", work_actions.RetryClassification.SOURCE_OWNER_REPAIR),
    ],
)
def test_classify_retry_trigger_returns_expected_classification(trigger, expected) -> None:
    assert work_actions._classify_retry(None, None, trigger=trigger) == expected


def test_classify_retry_rejects_unknown_trigger() -> None:
    with pytest.raises(ValueError, match="不支援的 trigger"):
        work_actions._classify_retry(None, None, trigger="not-a-real-trigger")


def test_stat_retry_classifications_aggregates_workflow_runs(tmp_path: Path) -> None:
    """#208 驗收：「retry 分類欄位落地，cortex stat 可依原因分類彙總」的最小彙總面。"""
    import io
    from contextlib import redirect_stdout

    from paulsha_cortex.coordinator import cli as coordinator_cli

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    for idx, classification in enumerate(
        ["model_repair", "model_repair", "orchestrator_retry", None]
    ):
        run = registry._manager_create_workflow_run(
            repo="hamanpaul/paulsha-cortex",
            work_id=f"agg-{idx}",
            claim_key=f"claim:v1:{str(idx) * 64}",
            source_revision="rev-agg",
            workspace_root="/tmp/workspace",
            combo="feature-oneshot",
            current_phase="build",
            steps=(_step("build", "subagent-build", gate_result="pending"),),
            issue_refs=(f"hamanpaul/paulsha-cortex#{900 + idx}",),
        )
        if classification is not None:
            registry._manager_update_workflow_run(
                run.run_id, retry_classification=classification
            )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = coordinator_cli.main(
            ["stat", "--retry-classifications"], registry=registry
        )
    assert exit_code == 0
    payload = json.loads(buffer.getvalue())
    assert payload == {
        "retry_classifications": {
            "model_repair": 2,
            "orchestrator_retry": 1,
            "unclassified": 1,
        }
    }


def test_stat_without_job_id_and_without_flag_errors(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import cli as coordinator_cli

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    exit_code = coordinator_cli.main(["stat"], registry=registry)
    assert exit_code == 1
