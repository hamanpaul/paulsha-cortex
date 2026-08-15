from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.claim import (
    ClaimCandidate,
    WorkAuthority,
    _resume_decision,
    build_claim_key,
    load_work_authority,
    work_authority_digest,
)
from paulsha_cortex.coordinator.registry import JobRegistry

from diagnostic_fixtures import fixture_needs_human_reason

RECOVERY_ACTION = "recover-planning"


def _snapshot(
    path: Path,
    *,
    issues=(12,),
    source_revisions=("issue:12@open", "openspec:demo@1"),
    provider_revision="gh-1",
    auto_label=True,
    prs=(8,),
    changes=("demo",),
    todo_paths=("docs/todo.md",),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": provider_revision,
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "mapped_issues": list(issues),
                        "mapped_prs": list(prs),
                        "mapped_openspec": list(changes),
                        "mapped_todo_paths": list(todo_paths),
                        "confirmed_todo": True,
                        "auto_label": auto_label,
                        "source_revisions": list(source_revisions),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _authority(tmp_path: Path) -> WorkAuthority:
    return load_work_authority(
        repo="acme/demo",
        work_id="demo",
        snapshot_path=_snapshot(tmp_path / "snapshot.json"),
    )


def _candidate(authority: WorkAuthority) -> ClaimCandidate:
    return ClaimCandidate(
        authority=authority,
        repo="acme/demo",
        work_id="demo",
        source_revisions=authority.source_revisions,
        confirmed_todo=authority.confirmed_todo,
        confirmed_issue=12,
        auto_label=False,
        active_run_id=None,
        active_claim_key=None,
    )


def _needs_human_candidate(
    tmp_path: Path,
    *,
    phase: str | None = "define",
    failure_classification: str | None = "environment",
    failure_reason: str | None = "planning identity probe unavailable",
) -> ClaimCandidate:
    authority = _authority(tmp_path)
    base = _candidate(authority)
    return replace(
        base,
        active_run_id="workflow-" + "a" * 20,
        active_claim_key=build_claim_key(base),
        active_status="needs_human",
        active_snapshot_hash=authority.snapshot_hash,
        active_source_revisions=authority.source_revisions,
        active_provider_revision=authority.github_provider_revision,
        active_authority_digest=work_authority_digest(authority),
        active_phase=phase,
        active_planning_failure_classification=failure_classification,
        active_planning_failure_reason=failure_reason,
    )


def _evidence_record(root: Path, *, run_id: str, classification: str, reason: str) -> str:
    evidence = root / "evidence" / "planning-recovery"
    evidence.mkdir(parents=True, exist_ok=True)
    target = evidence / f"{run_id}-{classification}.json"
    target.write_text(
        json.dumps(
            {
                "schema": "cortex-planning-failure/v1",
                "run_id": run_id,
                "classification": classification,
                "reason": reason,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return str(target)


def _start_define_run(*, snapshot: Path, state: Path, registry: JobRegistry) -> str:
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    return started["result"]["run"]["run_id"]


def _seed_planning_failure_run(
    tmp_path: Path, *,
    classification: str,
    reason: str,
) -> tuple[str, JobRegistry, Path, Path]:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run_id = _start_define_run(
        snapshot=snapshot,
        state=state,
        registry=registry,
    )
    failure_record = _evidence_record(
        tmp_path,
        run_id=run_id,
        classification=classification,
        reason=reason,
    )
    registry._manager_update_workflow_run(
        run_id,
        current_phase="define",
        facets=("needs_human",),
        attempts={"claim": 1, "define": 1},
        evidence_refs=(failure_record,),
        needs_human_reason=fixture_needs_human_reason(),
    )
    return run_id, registry, state, snapshot


def _run_recovery_action(
    *,
    run_id: str,
    snapshot: Path,
    state: Path,
    registry: JobRegistry,
    expected_run_id: str | None = None,
    classification: str,
    reason: str,
) -> dict:
    args: dict[str, object] = {
        "action": RECOVERY_ACTION,
        "repo": "acme/demo",
        "work_id": "demo",
        "failure_classification": classification,
        "failure_reason": reason,
    }
    if expected_run_id is not None:
        args["expected_run_id"] = expected_run_id
    return work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )


def test_environment_failure_is_recoverable(tmp_path: Path) -> None:
    run_id, registry, state, snapshot = _seed_planning_failure_run(
        tmp_path,
        classification="environment",
        reason="planning identity probe unavailable",
    )
    before = registry.get_workflow_run(run_id)

    recovered = _run_recovery_action(
        run_id=run_id,
        snapshot=snapshot,
        state=state,
        registry=registry,
        expected_run_id=run_id,
        classification="environment",
        reason="planning identity probe unavailable",
    )
    result_run = recovered["result"]["run"]

    assert recovered["result"]["action"] == "recovered"
    assert recovered["result"]["reason"] == "planning-recovery-dispatched"
    assert result_run["run_id"] == run_id
    assert result_run["current_phase"] in {"plan", "build", "verify", "review", "ship"}
    assert result_run["source_revision"] == before.source_revision
    # R1：離開 define/needs_human 才算真的前向恢復，不能只寫 evidence。
    assert before.current_phase == "define"
    assert "needs_human" in before.facets
    assert "needs_human" not in result_run["facets"]
    assert "blocked" not in result_run["facets"]
    # R4：稽核必須記錄觸發者、判定依據、恢復前後狀態。
    audit = json.loads(
        Path(recovered["result"]["evidence"]["ref"]).read_text(encoding="utf-8")
    )
    assert audit["schema"] == "cortex-work-planning-recovery/v1"
    assert audit["run_id"] == run_id
    assert audit["actor"] == "operator"
    assert audit["failure_classification"] == "environment"
    assert audit["failure_reason"] == "planning identity probe unavailable"
    assert audit["failure_evidence_ref"].endswith(f"{run_id}-environment.json")
    assert audit["previous_phase"] == "define"
    assert "needs_human" in audit["previous_facets"]
    assert audit["recovered_phase"] == result_run["current_phase"]


def test_content_failure_is_not_recoverable(tmp_path: Path) -> None:
    run_id, registry, state, snapshot = _seed_planning_failure_run(
        tmp_path,
        classification="content",
        reason="blocking marker missing",
    )
    with pytest.raises(Exception, match="content"):
        _run_recovery_action(
            run_id=run_id,
            snapshot=snapshot,
            state=state,
            registry=registry,
            expected_run_id=run_id,
            classification="content",
            reason="blocking marker missing",
        )

    # R1 fail-closed：內容類失敗被拒後，run 必須原封不動（沒被推進、
    # 沒被清掉 needs_human、沒留下恢復稽核紀錄）。
    after = registry.get_workflow_run(run_id)
    assert after.current_phase == "define"
    assert "needs_human" in after.facets
    assert not [
        ref
        for ref in after.evidence_refs
        if "cortex-work-planning-recovery" in Path(ref).read_text(encoding="utf-8")
    ]


def test_resume_returns_reason_and_next_actions(tmp_path: Path) -> None:
    decision = _resume_decision(_needs_human_candidate(tmp_path))

    assert decision.action == "needs_human"
    assert decision.reason == "human-intervention-required"
    assert hasattr(decision, "next_actions"), "needs_human resume decision must expose next_actions"
    next_actions = decision.next_actions
    assert isinstance(next_actions, (list, tuple))
    # R2：停在 define 的環境類 planning 失敗，恢復出口必須被浮現，
    # 不能只給「放棄」這一條。
    assert RECOVERY_ACTION in next_actions
    assert "abandon" in next_actions
    # R2：回應必須帶得出實際 blocking reason，而不是只有通用動作語意。
    assert decision.blocking_reason == (
        "planning-failure:environment:planning identity probe unavailable"
    )


def test_resume_hides_recovery_for_content_failure(tmp_path: Path) -> None:
    """R1 fail-closed：內容類失敗不得被 resume 宣傳成可恢復。"""

    decision = _resume_decision(
        _needs_human_candidate(
            tmp_path,
            failure_classification="content",
            failure_reason="blocking marker missing",
        )
    )

    assert decision.action == "needs_human"
    assert RECOVERY_ACTION not in decision.next_actions
    assert "abandon" in decision.next_actions
    assert decision.blocking_reason == "planning-failure:content:blocking marker missing"


def test_resume_without_planning_evidence_offers_no_recovery(tmp_path: Path) -> None:
    """拿不到失敗 evidence 時不得宣稱恢復可用，blocking reason 也不得編造。"""

    decision = _resume_decision(
        _needs_human_candidate(
            tmp_path,
            failure_classification=None,
            failure_reason=None,
        )
    )

    assert decision.next_actions == ("abandon",)
    assert decision.blocking_reason is None


def test_resume_response_surfaces_next_actions(tmp_path: Path) -> None:
    """R2：operator 走 `cortex work resume` 就要看得到下一步，不必翻 registry。"""

    run_id, registry, state, snapshot = _seed_planning_failure_run(
        tmp_path,
        classification="environment",
        reason="planning identity probe unavailable",
    )
    resumed = work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 201,
        workflow_registry=registry,
    )["result"]

    assert resumed["action"] == "needs_human"
    assert resumed["run"]["run_id"] == run_id
    assert RECOVERY_ACTION in resumed["next_actions"]
    assert "abandon" in resumed["next_actions"]
    assert resumed["blocking_reason"] == (
        "planning-failure:environment:planning identity probe unavailable"
    )


def test_abandon_allows_reclaim(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", prs=())
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    started = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    run_id = started["result"]["run"]["run_id"]

    work_actions.execute_work_action(
        args={
            "action": "abandon",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_run_id": run_id,
            "reason": "temp planning recover test",
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 201,
        workflow_registry=registry,
    )

    reclaimed = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 202,
        workflow_registry=registry,
    )

    assert reclaimed["result"]["action"] == "claim"
    assert reclaimed["result"]["run"]["run_id"] != run_id
    # R3：重新 claim 必須是在「authority 組成完全沒變」下成立的正常流程——
    # claim key（repo + work_id + authority digest）保持同一把，不是靠改
    # digest 繞過；舊 run 則確實被釋放而非仍佔著位置。
    original = registry.get_workflow_run(run_id)
    reclaimed_run = registry.get_workflow_run(reclaimed["result"]["run"]["run_id"])
    assert reclaimed_run.claim_key == original.claim_key
    assert original.status == "superseded"
    assert "planning_released" in original.facets
    assert reclaimed_run.status == "ongoing"


def test_existing_blocked_runs_unaffected(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    run_id = started["result"]["run"]["run_id"]
    registry._manager_update_workflow_run(
        run_id,
        status="superseded",
        facets=("blocked",),
        evidence_refs=(),
    )

    from paulsha_cortex.coordinator.work_bridge import workflow_status

    before = registry.get_workflow_run(run_id)
    assert workflow_status(before) == "blocked"

    with pytest.raises(Exception, match="blocked|persisted-block"):
        _run_recovery_action(
            run_id=run_id,
            snapshot=snapshot,
            state=state,
            registry=registry,
            expected_run_id=run_id,
            classification="environment",
            reason="legacy blocked run should stay blocked",
        )

    # 釋放標記不得回溯改變既有 blocked run：既沒被寫入 planning_released，
    # 也沒被推離原 phase。
    after = registry.get_workflow_run(run_id)
    assert workflow_status(after) == "blocked"
    assert "planning_released" not in after.facets
    assert after.current_phase == before.current_phase


def test_recovery_requires_expected_run_id(tmp_path: Path) -> None:
    run_id, registry, state, snapshot = _seed_planning_failure_run(
        tmp_path,
        classification="environment",
        reason="identity unavailable",
    )
    with pytest.raises(Exception, match="expected_run_id"):
        _run_recovery_action(
            run_id=run_id,
            snapshot=snapshot,
            state=state,
            registry=registry,
            classification="environment",
            reason="identity unavailable",
        )

    # CAS 必須真的比對，而不是「有給就放行」：指向別的 run id 要被擋掉。
    with pytest.raises(Exception, match="CAS"):
        _run_recovery_action(
            run_id=run_id,
            snapshot=snapshot,
            state=state,
            registry=registry,
            expected_run_id="workflow-" + "b" * 20,
            classification="environment",
            reason="identity unavailable",
        )

    # 呼叫端自述的 reason 與系統寫入的 evidence 不符時同樣 fail-closed。
    with pytest.raises(Exception, match="failure_reason"):
        _run_recovery_action(
            run_id=run_id,
            snapshot=snapshot,
            state=state,
            registry=registry,
            expected_run_id=run_id,
            classification="environment",
            reason="a different story",
        )

    after = registry.get_workflow_run(run_id)
    assert after.current_phase == "define"
    assert "needs_human" in after.facets


def test_recovery_is_idempotent(tmp_path: Path) -> None:
    run_id, registry, state, snapshot = _seed_planning_failure_run(
        tmp_path,
        classification="environment",
        reason="identity recovered after runtime restart",
    )
    first = _run_recovery_action(
        run_id=run_id,
        snapshot=snapshot,
        state=state,
        registry=registry,
        expected_run_id=run_id,
        classification="environment",
        reason="identity recovered after runtime restart",
    )
    runs_after_first = len(registry.list_workflow_runs())

    second = _run_recovery_action(
        run_id=run_id,
        snapshot=snapshot,
        state=state,
        registry=registry,
        expected_run_id=run_id,
        classification="environment",
        reason="identity recovered after runtime restart",
    )
    runs_after_second = len(registry.list_workflow_runs())

    assert first["result"]["run"]["run_id"] == second["result"]["run"]["run_id"]
    assert runs_after_second == runs_after_first
    # R4：重送不得產生第二份恢復稽核紀錄或第二個 planning 派工，
    # 且第二次必須明確回報是重放而非再次派工。
    assert first["result"]["reason"] == "planning-recovery-dispatched"
    assert second["result"]["reason"] == "already-recovered"
    recovery_records = sorted(
        (tmp_path / "evidence" / "planning-recovery").glob("*.json")
    )
    assert (
        len(
            [
                path
                for path in recovery_records
                if json.loads(path.read_text(encoding="utf-8")).get("schema")
                == "cortex-work-planning-recovery/v1"
            ]
        )
        == 1
    )
