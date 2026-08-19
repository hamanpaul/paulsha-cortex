"""#728：`recover-planning` 的出口狀態必須是 planning-authority 對帳的合法入口狀態。

現場（run ``workflow-ef40fb2793c5b83818d9``，`brainstorm_required=true`）：

1. define 因環境類 planning 失敗停在 `needs_human`；
2. operator 修好環境後下 `recover-planning`，逐字回 ``action=recovered``／
   ``phase=plan``，recovery evidence 記 ``"recovered_phase": "plan"``、
   ``"recovery_basis": "planning-runtime-retry"``；
3. 下一拍 periodic tick 立刻 ``planning-authority-reconciliation-failed``：
   ``ValueError: workflow brainstorm evidence missing``，而該 attention 的
   ``next_actions`` 是**空的** ⇒ CLI 面無路可走，只剩 abandon 整代重來。

## (A)/(B) 裁決：(B)

``recovery_basis: "planning-runtime-retry"`` 的語意是「**解除封鎖、讓下一拍
重跑**」，不是「recover 內部已經重跑過 planning」。逐字證據：

- `work_actions._recover_planning_action` 全程沒有任何 planner／runtime 呼叫
  （沒有 `runtime_factory`、沒有 `run_heterogeneous_brainstorm`、不寫
  `gate_refs`／`planning_authority`／`planning_source_revision`）；
- 唯一產生 brainstorm gate evidence 的路徑是 `manager.apply_workflow_action`
  的 define 段，它與 `current_phase="plan"` 在同一次 registry 原子寫入內完成；
- 而那條路徑的入口守衛逐字是
  ``if run.current_phase not in {"claim", "define"}: return "already-claimed"``，
  `work_bridge.start_canonical_workflow` 另有 ``if existing_run.current_phase
  != "define": return existing_run``。

⇒ 推進到 `plan` 不是「前進」，是**永久關掉**產生背書的唯一入口。修在推進側。

## 本檔釘住的三件事

1. 出口 phase 由 `workflow.brainstorm_authority_bound` 決定，不再寫死 `plan`；
2. recover 與 reconciliation **共用同一個函式**（identity 斷言），不是兩份等價
   的條件式；
3. `planning-authority-reconciliation-failed` 的 attention 條目永遠給得出至少
   一個 `next_actions`——fail-closed 可以，無出路不行。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import claim as claim_module
from paulsha_cortex.coordinator import manager, work_actions, workflow
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import GateEvidenceRef

from diagnostic_fixtures import fixture_needs_human_reason  # noqa: E402
from test_planning_claim_recovery import (  # noqa: E402
    _run_recovery_action,
    _seed_planning_failure_run,
)

REASON = "planning identity probe unavailable"

# `manager.apply_workflow_action` 的 define 段入口守衛與
# `work_bridge.start_canonical_workflow` 的短路所共用的 phase 集合。這裡逐字
# 重述一次，是為了讓「集合漂移」在本檔直接紅掉。
BRAINSTORM_PRODUCER_REACHABLE = frozenset({"claim", "define"})


def _seed(tmp_path: Path, *, brainstorm_required: bool, gate_refs=()):
    """停在 define／needs_human 的環境類 planning 失敗 run。"""

    run_id, registry, state, snapshot = _seed_planning_failure_run(
        tmp_path, classification="environment", reason=REASON
    )
    registry._manager_update_workflow_run(
        run_id,
        brainstorm_required=brainstorm_required,
        gate_refs=tuple(gate_refs),
    )
    return run_id, registry, state, snapshot


def _recover(run_id, registry, state, snapshot) -> dict:
    return _run_recovery_action(
        run_id=run_id,
        snapshot=snapshot,
        state=state,
        registry=registry,
        expected_run_id=run_id,
        classification="environment",
        reason=REASON,
    )["result"]


def _brainstorm_ref(tmp_path: Path) -> GateEvidenceRef:
    """一份形狀合法的 brainstorm gate ref（本檔只看 `kind`，不做內容重驗）。"""

    target = tmp_path / "evidence" / "planning" / "brainstorm-own.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"schema_version": 1, "kind": "brainstorm-peer"}), encoding="utf-8"
    )
    return GateEvidenceRef(kind="brainstorm", ref=str(target), sha256="a" * 64)


# ==========================================================================
# 段 1：(B) 的落地——出口 phase 依前置條件決定，不再寫死 `plan`
# ==========================================================================


def test_recover_keeps_a_brainstorm_required_run_in_define(tmp_path: Path) -> None:
    """本 issue 的核心：沒有背書就不得被推進到一個下一拍必定拒絕的 phase。"""

    run_id, registry, state, snapshot = _seed(tmp_path, brainstorm_required=True)
    before = registry.get_workflow_run(run_id)
    assert before.current_phase == "define"
    assert "needs_human" in before.facets
    assert not [ref for ref in before.gate_refs if ref.kind == "brainstorm"]

    result = _recover(run_id, registry, state, snapshot)
    after = registry.get_workflow_run(run_id)

    # 恢復本身仍然成立——封鎖被解除。
    assert result["action"] == "recovered"
    assert result["reason"] == "planning-recovery-dispatched"
    assert "needs_human" not in after.facets
    assert "blocked" not in after.facets
    # 但 phase 留在 define，交還給正常流程重跑並自然產生 evidence。
    assert result["recovered_phase"] == "define"
    assert after.current_phase == "define"
    # 而且 recover 不得偽造背書：gate_refs 一個都不准長出來。
    assert after.gate_refs == ()
    assert after.brainstorm_required is True

    # 稽核紀錄必須誠實記下出口 phase（#256 R4）。
    audit = json.loads(Path(result["evidence"]["ref"]).read_text(encoding="utf-8"))
    assert audit["previous_phase"] == "define"
    assert audit["recovered_phase"] == "define"
    assert audit["recovery_basis"] == "planning-runtime-retry"


def test_recover_still_advances_a_run_that_needs_no_brainstorm(tmp_path: Path) -> None:
    """`brainstorm_required=False` 的既有行為不得被本修正改掉。"""

    run_id, registry, state, snapshot = _seed(tmp_path, brainstorm_required=False)

    result = _recover(run_id, registry, state, snapshot)
    after = registry.get_workflow_run(run_id)

    assert result["recovered_phase"] == "plan"
    assert after.current_phase == "plan"
    assert "needs_human" not in after.facets


def test_recover_advances_when_the_run_already_owns_a_brainstorm_ref(
    tmp_path: Path,
) -> None:
    """背書已在該 run 自己身上時，推進到 `plan` 仍是合法入口狀態。"""

    run_id, registry, state, snapshot = _seed(
        tmp_path,
        brainstorm_required=True,
        gate_refs=(_brainstorm_ref(tmp_path),),
    )

    result = _recover(run_id, registry, state, snapshot)
    after = registry.get_workflow_run(run_id)

    assert result["recovered_phase"] == "plan"
    assert after.current_phase == "plan"
    assert [ref.kind for ref in after.gate_refs] == ["brainstorm"]


@pytest.mark.parametrize(
    ("brainstorm_required", "owns_ref", "expected_phase"),
    [
        (False, False, "plan"),
        (False, True, "plan"),
        (True, False, "define"),
        (True, True, "plan"),
    ],
)
def test_recover_phase_and_gate_refs_matrix(
    tmp_path: Path,
    brainstorm_required: bool,
    owns_ref: bool,
    expected_phase: str,
) -> None:
    """`brainstorm_required` true/false × recover 前後的 phase 與 gate_refs。"""

    refs = (_brainstorm_ref(tmp_path),) if owns_ref else ()
    run_id, registry, state, snapshot = _seed(
        tmp_path, brainstorm_required=brainstorm_required, gate_refs=refs
    )
    before = registry.get_workflow_run(run_id)
    assert before.current_phase == "define"

    result = _recover(run_id, registry, state, snapshot)
    after = registry.get_workflow_run(run_id)

    assert result["recovered_phase"] == expected_phase
    assert after.current_phase == expected_phase
    # recover 從不新增、也不移除 gate evidence——它不是背書的生產者。
    assert after.gate_refs == before.gate_refs
    assert after.brainstorm_required == before.brainstorm_required


# ==========================================================================
# 段 2：迴歸釘住——recover 的出口狀態 ≡ reconciliation 的合法入口狀態
# ==========================================================================


def test_both_sides_consume_one_and_the_same_precondition_function() -> None:
    """兩者共用同一組前置條件斷言，不得各寫一份（#708／#710／#712 的形狀）。"""

    assert (
        work_actions.brainstorm_authority_bound
        is manager.brainstorm_authority_bound
        is workflow.brainstorm_authority_bound
    )


def test_precondition_phase_set_matches_the_brainstorm_producer_guard() -> None:
    """前置條件放行的 phase，必須正好是 brainstorm 生產者仍可達的 phase。

    集合一旦漂移，就會再造一次本 issue：要嘛在還生得出背書的 phase 硬要背書
    （define wedge），要嘛在生不出背書的 phase 放行（authority 漏洞）。
    """

    assert (
        workflow.PLANNING_PUBLICATION_PENDING_PHASES == BRAINSTORM_PRODUCER_REACHABLE
    )


@pytest.mark.parametrize(
    ("brainstorm_required", "owns_ref"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_recover_exit_state_is_a_legal_reconciliation_entry_state(
    tmp_path: Path,
    brainstorm_required: bool,
    owns_ref: bool,
) -> None:
    """本 issue 的迴歸釘：recover 之後的 run 不得再是對帳拒收的狀態。"""

    refs = (_brainstorm_ref(tmp_path),) if owns_ref else ()
    run_id, registry, state, snapshot = _seed(
        tmp_path, brainstorm_required=brainstorm_required, gate_refs=refs
    )

    _recover(run_id, registry, state, snapshot)
    after = registry.get_workflow_run(run_id)

    assert workflow.brainstorm_authority_bound(after) is True


def test_recovered_brainstorm_run_survives_the_next_tick(tmp_path: Path) -> None:
    """端到端：recover 之後的下一拍不得再回 `planning-authority-reconciliation-failed`。

    這正是現場逐字回報的那一拍（`manager.resume_workflow_run:planning-authority`）。
    """

    run_id, registry, state, snapshot = _seed(tmp_path, brainstorm_required=True)
    _recover(run_id, registry, state, snapshot)

    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=tmp_path,
    )

    assert result["reason"] != "planning-authority-reconciliation-failed"
    assert registry.get_workflow_run(run_id).current_phase == "define"


# ==========================================================================
# 段 3：`next_actions` 不得為空——fail-closed 可以，無出路不行
# ==========================================================================


@pytest.mark.parametrize("phase", [None, *workflow.WORKFLOW_PHASES])
@pytest.mark.parametrize("classification", [None, "environment", "content", "bogus"])
def test_needs_human_next_actions_is_never_empty(
    phase: str | None, classification: str | None
) -> None:
    """基礎集合的機械保證：`abandon` 永遠在，因此不可能回空集合。"""

    actions = claim_module.needs_human_next_actions(
        phase=phase, planning_failure_classification=classification
    )

    assert actions
    assert "abandon" in actions
    # R1 fail-closed：recover-planning 只在「停在 define 的環境類失敗」浮現。
    assert ("recover-planning" in actions) == (
        phase == "define" and classification == "environment"
    )


def _reconciliation_failed_run(tmp_path: Path):
    """直接構造現場那個狀態：plan phase／`brainstorm_required`／無背書。"""

    from test_workflow_production_wiring import _manifest  # noqa: E402

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="wedge",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={"plan": 1},
        facets=("needs_human",),
        gate_status="running",
        brainstorm_required=True,
        needs_human_reason=fixture_needs_human_reason(),
    )
    return registry, run


def test_reconciliation_failed_attention_entry_always_offers_an_action(
    tmp_path: Path,
) -> None:
    """現場逐字的 `next_actions: []` 不得再出現。

    對帳本身仍然 fail-closed（沒有背書就不得取得 authority），但 attention
    條目必須給得出一個合法動作。
    """

    registry, run = _reconciliation_failed_run(tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=tmp_path,
        operator_resume=True,
    )
    assert result["reason"] == "planning-authority-reconciliation-failed"

    entry = manager.workflow_status_entry(
        registry, registry.get_workflow_run(run.run_id)
    )

    assert entry["kind"] == "workflow_run"
    assert entry["next_actions"], "planning-authority-reconciliation-failed 不得無出路"
    assert "abandon" in entry["next_actions"]


def test_attention_next_actions_survive_a_broken_job_registry(tmp_path: Path) -> None:
    """曝光面即使算不出 job 層動作，也不得退化成空集合。"""

    registry, run = _reconciliation_failed_run(tmp_path)

    class _Broken:
        def list_jobs(self):
            raise RuntimeError("registry unavailable")

    entry = manager.workflow_status_entry(_Broken(), registry.get_workflow_run(run.run_id))

    assert entry["next_actions"] == ["abandon"]
