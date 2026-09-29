"""#1170：越過 abandon pre-delivery 閘門的 needs_human run 不得被投影 abandon。

`abandon` 的 registry admission（`_manager_validate_workflow_abandon`）拒絕帶 PR
refs、已進 ship、ship step 已通過或已有 completion record 的 run。needs_human 的
`next_actions` 投影（claim／status attention／Monitor work list 共用
`claim.needs_human_next_actions`）必須以**同一個**判準
（`registry.workflow_run_pre_delivery`）決定 abandon 在不在集合裡，而不是看 job 層
剛好有沒有算出其他動作：

- 越過閘門的 run：不論 job 層投影給了什麼（包括 `("resume",)`、空集合或投影本身
  失敗），都不得出現 abandon；有 retry lane 時以它為出口，否則給註冊的 `resume`，
  #728 的非空保證維持成立。
- pre-delivery run：abandon 照舊永遠在集合裡（#256 R3），與 job 層動作並列。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import claim as claim_module, manager, work_actions
from paulsha_cortex.coordinator.registry import workflow_run_pre_delivery
from paulsha_cortex.monitor import providers

from test_post_merge_run_resolution_1141 import (
    REPO,
    WORK_ID,
    _mark_resume_failed,
    _open_pr_snapshot,
    _ship_lane,
)
from test_recovery_action_exposure_546 import _seed_pre_candidate_recovery
from test_review_gate_adjudication_exit import _review_fixture


RUN_ID = "workflow-" + "0" * 20


@pytest.mark.parametrize(
    ("job_actions", "expected"),
    [
        # 審查指出的情境：open PR、沒有 retry lane，job 層只給 resume。
        (("resume",), ("resume",)),
        # job 層投影不可得／有 active job 時的空集合：舊邏輯退回必被拒的 abandon。
        ((), ("resume",)),
        # 任何來源誤帶 abandon 都要被濾掉。
        (("abandon",), ("resume",)),
        (("retry-review", "retry-build"), ("retry-review", "retry-build")),
        (("retry-card",), ("retry-card",)),
        (("review-attest",), ("resume", "review-attest")),
        (("regenerate-gates",), ("resume", "regenerate-gates")),
    ],
)
def test_past_pre_delivery_projection_never_offers_abandon(
    job_actions: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    actions = claim_module.needs_human_next_actions(
        phase="ship",
        planning_failure_classification=None,
        job_recovery_actions=job_actions,
        pre_delivery=False,
    )

    assert actions == expected
    assert "abandon" not in actions
    hint = claim_module.needs_human_next_step_hint(
        phase="ship",
        planning_failure_classification=None,
        work_id=WORK_ID,
        repo=REPO,
        run_id=RUN_ID,
        job_recovery_actions=job_actions,
        pre_delivery=False,
    )
    assert "cortex work abandon" not in hint
    assert ("cortex work resume demo --repo acme/demo" in hint) == ("resume" in actions)


def test_past_pre_delivery_define_run_keeps_recover_planning_without_abandon() -> None:
    """recover-planning 的前置驗不看 delivery 狀態；越過閘門的 define run 仍以它為
    出口，只拿掉必被拒的 abandon。"""

    actions = claim_module.needs_human_next_actions(
        phase="define",
        planning_failure_classification="environment",
        pre_delivery=False,
    )

    assert actions == ("recover-planning",)
    assert claim_module.needs_human_next_actions(
        phase="define", planning_failure_classification="environment"
    ) == ("recover-planning", "abandon")


@pytest.mark.parametrize(
    "job_actions",
    [
        (),
        ("recover-pre-candidate",),
        ("regenerate-gates", "retry-card"),
        ("retry-review", "retry-build"),
    ],
)
def test_pre_delivery_projection_keeps_abandon_beside_admitted_lanes(
    job_actions: tuple[str, ...],
) -> None:
    actions = claim_module.needs_human_next_actions(
        phase="build",
        planning_failure_classification=None,
        job_recovery_actions=job_actions,
    )

    assert actions == ("abandon", *job_actions)
    assert "resume" not in actions


@pytest.mark.parametrize(
    ("fields", "pre_delivery"),
    [
        ({}, True),
        ({"pr_refs": (f"{REPO}#8",)}, False),
        ({"current_phase": "ship"}, False),
        ({"steps": (SimpleNamespace(phase="ship", gate_result="passed"),)}, False),
        ({"completion_record_path": "completion.json"}, False),
    ],
)
def test_pre_delivery_predicate_covers_every_abandon_gate_signal(
    fields: dict[str, object], pre_delivery: bool
) -> None:
    run = SimpleNamespace(
        current_phase="review",
        pr_refs=(),
        steps=(SimpleNamespace(phase="ship", gate_result="pending"),),
        completion_record_path=None,
    )
    for name, value in fields.items():
        setattr(run, name, value)

    assert workflow_run_pre_delivery(run) is pre_delivery


# ship phase／completion record 的 run 需要完整 ship gate 與 completion 欄位才建得
# 出來，只在上面的判準單元測試覆蓋；這裡以 registry 實際 admission 驗兩個可直接
# 佈置的訊號。
@pytest.mark.parametrize("variant", ["pr-refs", "ship-step-passed"])
def test_projection_shares_the_abandon_pre_delivery_gate(
    variant: str, tmp_path: Path
) -> None:
    registry, run, _authority = _seed_pre_candidate_recovery(tmp_path)
    probe = str(tmp_path / "abandon-probe.json")
    # 對照組：pre-delivery run 的 abandon admission 受理，投影也給 abandon。
    assert workflow_run_pre_delivery(run)
    registry._manager_validate_workflow_abandon(run.run_id, evidence_ref=probe)
    assert "abandon" in manager.workflow_status_entry(registry, run)["next_actions"]

    if variant == "pr-refs":
        updated = registry._manager_update_workflow_run(run.run_id, pr_refs=(f"{REPO}#8",))
    else:
        updated = registry._manager_update_workflow_run(
            run.run_id,
            steps=tuple(
                replace(step, gate_result="passed") if step.phase == "ship" else step
                for step in run.steps
            ),
        )

    assert not workflow_run_pre_delivery(updated)
    with pytest.raises(ValueError, match="pre-delivery"):
        registry._manager_validate_workflow_abandon(updated.run_id, evidence_ref=probe)
    entry = manager.workflow_status_entry(registry, updated)
    assert entry["next_actions"]
    assert "abandon" not in entry["next_actions"]
    assert "resume" in entry["next_actions"]
    assert "cortex work abandon" not in entry["next_step_hint"]


def _open_pr_resume_failed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    lane = _ship_lane(tmp_path, monkeypatch, copilot_reviewed=False)
    _open_pr_snapshot(lane.snapshot)
    assert lane.ship(now=2000.0)["action"] == "awaiting-copilot"
    _mark_resume_failed(lane)
    run = lane.registry.get_workflow_run(lane.run_id)
    assert run.pr_refs
    return lane, run


def test_open_pr_run_read_models_agree_on_resume_without_abandon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claim（resume 回應）、status attention、Monitor work list 三個 read model
    對同一個 open PR、沒有 retry lane 的 review run 給出相同、不含 abandon 的
    next_actions。"""

    snapshot, _authority, registry, run = _review_fixture(
        tmp_path, reason="resume-workflow-failed"
    )
    assert run.pr_refs

    entry = manager.workflow_status_entry(registry, run)
    assert entry["next_actions"] == ["resume"]

    resumed = work_actions.execute_work_action(
        args={"action": "resume", "repo": REPO, "work_id": WORK_ID, "issue": 12},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
        workflow_starter=lambda _authority, _claim_key, _reason: registry.get_workflow_run(
            run.run_id
        ),
    )["result"]
    assert resumed["action"] == "needs_human"
    assert resumed["next_actions"] == ["resume"]
    assert "cortex work abandon" not in resumed["next_step_hint"]

    monkeypatch.setattr(
        work_actions, "work_authority_projection_state", lambda **_: "available"
    )
    rows = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    projected = providers._workflow_next_actions_projection(
        rows["workflows"],
        repo=REPO,
        job_rows=rows["jobs"],
        slice_rows=[],
        state_path=tmp_path / "jobs.json",
    )
    assert projected[WORK_ID] == {"run_id": run.run_id, "actions": ["resume"]}


@pytest.mark.parametrize("job_projection", ["unavailable", "active-job"])
def test_open_pr_run_never_falls_back_to_abandon(
    job_projection: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """job 層投影失敗或 run 仍有 active job 時，舊邏輯的保底集合退回必被拒的
    abandon；現在保底集合同樣依 pre-delivery 閘門改給 resume。"""

    lane, run = _open_pr_resume_failed_run(tmp_path, monkeypatch)
    if job_projection == "unavailable":

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("job registry unreadable")

        monkeypatch.setattr(work_actions, "_phase_recovery_actions", unavailable)
    else:
        lane.registry.create_job(
            task="wf-ship-in-flight",
            persona="builder",
            branch="feature/demo",
            pane="",
            worktree=str(tmp_path / "in-flight"),
            workflow_run_id=run.run_id,
            workflow_card="ship",
            workflow_phase="ship",
        )

    entry = manager.workflow_status_entry(lane.registry, run)

    assert entry["next_actions"] == ["resume"]
    assert "cortex work abandon" not in entry["next_step_hint"]
