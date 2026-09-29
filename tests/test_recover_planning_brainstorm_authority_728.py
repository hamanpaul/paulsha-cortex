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

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import claim as claim_module
from paulsha_cortex.coordinator import (
    manager,
    manager_daemon,
    work_actions,
    work_bridge,
    workflow,
)
from paulsha_cortex.coordinator.launcher import LaunchHandle
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
    jobs_before = registry.list_jobs()
    assert before.current_phase == "define"
    assert "needs_human" in before.facets
    assert not [ref for ref in before.gate_refs if ref.kind == "brainstorm"]

    result = _recover(run_id, registry, state, snapshot)
    after = registry.get_workflow_run(run_id)

    # 恢復本身仍然成立——封鎖被解除。
    assert result["action"] == "recovered"
    assert result["reason"] == "planning-recovery-unblocked"
    assert registry.list_jobs() == jobs_before
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


@pytest.mark.parametrize("crash_point", ["before-registry-commit", "after-registry-commit"])
def test_recover_planning_crash_restart_replays_one_immutable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_point: str
) -> None:
    """持久化前後 crash 都可由 fresh registry 重送同一正式 action。"""

    run_id, registry, state, snapshot = _seed(
        tmp_path, brainstorm_required=False
    )
    original_update = registry._manager_update_workflow_run

    def crash_at_registry_commit(target_run_id, **fields):
        if crash_point == "before-registry-commit":
            raise RuntimeError("injected crash before registry commit")
        original_update(target_run_id, **fields)
        raise RuntimeError("injected crash after registry commit")

    monkeypatch.setattr(registry, "_manager_update_workflow_run", crash_at_registry_commit)
    with pytest.raises(RuntimeError, match="injected crash"):
        _recover(run_id, registry, state, snapshot)

    monkeypatch.undo()
    restarted = JobRegistry(state_path=registry._state_path)
    result = _recover(run_id, restarted, state, snapshot)
    run = restarted.get_workflow_run(run_id)

    assert run.current_phase == "plan"
    assert "needs_human" not in run.facets
    assert result["reason"] in {"planning-recovery-unblocked", "already-recovered"}
    records = [
        path
        for path in (tmp_path / "evidence" / "planning-recovery").glob("*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("schema")
        == "cortex-work-planning-recovery/v1"
    ]
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["schema"] == "cortex-work-planning-recovery/v1"
    assert record["run_id"] == run_id


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


# ==========================================================================
# 段 4：#843 R05——recover-planning 只 unblock；下一拍才真正接續
# ==========================================================================
#
# 呼叫鏈（本段測試逐段釘住）：
#
# - 同次呼叫：daemon `work-action` 只對 start/resume/retry-* /intake 追加
#   `dispatch_workflow_card`（`manager_daemon.build_request_executor`），
#   `recover-planning` 不在集合內 ⇒ 零 job、零 launcher 呼叫。
# - 下一拍（`manager_daemon.build_periodic_tick_runner`）：
#   1. 先跑 auto-claim scan（`manager.run_auto_claim_scan` →
#      `work_actions.run_auto_claim_scan` → `_claim_action(auto-scan)` 對
#      ongoing run 回 `resume` → `workflow_starter` ＝
#      `work_bridge.start_canonical_workflow`）；define phase 的 planning
#      （brainstorm）只在這條 producer 內以 planning runtime 同步執行，
#      不是 registry Job。
#   2. 再跑 resume 迴圈（ongoing、非 needs_human、phase ∈ define..review）
#      → `manager.resume_workflow_run` → `dispatch_workflow_card`；卡片派工
#      只接受 plan/build/verify/review（`manager._dispatch_workflow_card` 的
#      phase 守衛），plan phase 的 `writing-plans` 才會產生 planner Job。


class _PlannerLauncher:
    """記錄每一次 planner 啟動；不啟動任何真實 process。"""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def as_read_only(self):
        return self

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        self._sink.append(slice_id)
        return LaunchHandle(
            executor="codex",
            model_id="gpt-primary",
            session_name=slice_id,
            pid=100,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


def _planner_identities() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            }
        ]
    )


class _TickDispatcher:
    """daemon 傳給 resume 的 dispatcher 形狀；in-flight job 原樣回報。"""

    def __init__(self, registry: JobRegistry) -> None:
        self._registry = registry
        self._git_runner = None

    def poll_headless_done(self, job_id: str):
        return self._registry.get_job(job_id)


# 在任何 monkeypatch 之前取得 production scan 本體；同一測試可跑多拍 tick。
_PRODUCTION_AUTO_CLAIM_SCAN = work_actions.run_auto_claim_scan


class _LabelRead:
    returncode = 0
    stdout = json.dumps({"labels": [{"name": claim_module.AUTO_LABEL}]})
    stderr = ""


def _materialize_plan_inputs(registry: JobRegistry, run_id: str) -> None:
    """writing-plans 卡宣告的 input（openspec proposal）落地到 run 的工作樹。"""

    run = registry.get_workflow_run(run_id)
    for step in run.steps:
        if step.phase != "plan":
            continue
        for relative in step.inputs:
            target = Path(run.workspace_root) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# proposal\n", encoding="utf-8")


def _recover_through_daemon_request(
    tmp_path: Path, run_id, registry, state, snapshot, launched: list[str]
) -> dict:
    """正式 daemon `work-action` 請求：與 production 同一個 request executor。"""

    executor = manager_daemon.build_request_executor(
        dispatcher=_TickDispatcher(registry),
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=_PlannerLauncher(launched),
        workflow_identity_registry=_planner_identities(),
        work_action_fn=lambda *, args, requested_by: work_actions.execute_work_action(
            args=args,
            requested_by=requested_by,
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
            workflow_registry=registry,
        ),
    )
    return executor(
        build_request(
            req_type="work-action",
            args={
                "action": "recover-planning",
                "repo": "acme/demo",
                "work_id": "demo",
                "expected_run_id": run_id,
                "failure_classification": "environment",
                "failure_reason": REASON,
            },
            requested_by="operator",
        )
    )


def _next_periodic_tick(
    tmp_path: Path,
    registry: JobRegistry,
    *,
    snapshot: Path,
    state: Path,
    launched: list[str],
    producer_calls: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """跑一次 production periodic tick runner。

    auto-claim 走 production `manager.run_auto_claim_scan` 的 starter 接線；只把
    GitHub label 讀取與 snapshot 路徑換成本機 fixture，並把 define producer
    （`work_bridge.start_canonical_workflow`，production 會在其中以 planning
    runtime 跑 brainstorm）換成記錄呼叫的替身——測試環境沒有 trusted repo root
    與真實 model runtime。
    """

    def scan_with_fixture_inputs(**kwargs):
        return _PRODUCTION_AUTO_CLAIM_SCAN(
            snapshot_path=snapshot,
            state_path=state,
            runner=lambda *_args, **_kwargs: _LabelRead(),
            sleeper=lambda _seconds: None,
            now=lambda: 200,
            **kwargs,
        )

    def record_define_producer(*, registry, authority, claim_key, **kwargs):
        producer_calls.append(
            {
                "work_id": authority.work_id,
                "claim_key": claim_key,
                "needs_human_reason": kwargs.get("needs_human_reason"),
                "runtime_factory": kwargs.get("runtime_factory"),
            }
        )
        return next(
            run
            for run in registry.list_workflow_runs()
            if run.claim_key == claim_key and run.status == "ongoing"
        )

    monkeypatch.setattr(work_actions, "run_auto_claim_scan", scan_with_fixture_inputs)
    monkeypatch.setattr(work_bridge, "start_canonical_workflow", record_define_producer)
    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=_TickDispatcher(registry),
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=_PlannerLauncher(launched),
        run_tick_fn=lambda *_args, **_kwargs: {
            "dispatch_skipped": False,
            "dispatched": [],
            "completed": [],
            "errors": [],
            "reaped": None,
        },
        scan_specs_fn=lambda _specs_dir: [],
        workflow_identity_registry=_planner_identities(),
    )
    return runner()


def test_r05_recover_planning_unblocks_and_the_next_tick_dispatches_the_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """brainstorm 不需要：同次呼叫零 job；下一拍 periodic tick 派出一顆 planner job。"""

    run_id, registry, state, snapshot = _seed(tmp_path, brainstorm_required=False)
    _materialize_plan_inputs(registry, run_id)
    launched: list[str] = []
    producer_calls: list[dict] = []

    # 對照組：recover 之前同一個 tick 對 needs_human run 什麼都不做，後面的
    # planner job 因此只可能來自 recover 解除的封鎖。
    blocked_tick = _next_periodic_tick(
        tmp_path,
        registry,
        snapshot=snapshot,
        state=state,
        launched=launched,
        producer_calls=producer_calls,
        monkeypatch=monkeypatch,
    )
    assert blocked_tick["errors"] == []
    assert registry.list_jobs() == []
    assert launched == []
    assert producer_calls == []
    assert "needs_human" in registry.get_workflow_run(run_id).facets

    response = _recover_through_daemon_request(
        tmp_path, run_id, registry, state, snapshot, launched
    )

    # 同次呼叫：只 unblock，沒有任何派工。
    assert response["result"]["reason"] == "planning-recovery-unblocked"
    assert response["result"]["recovered_phase"] == "plan"
    assert "job_id" not in response["result"]
    assert "dispatch" not in response["result"]
    assert registry.list_jobs() == []
    assert launched == []
    recovered = registry.get_workflow_run(run_id)
    assert recovered.current_phase == "plan"
    assert "needs_human" not in recovered.facets

    summary = _next_periodic_tick(
        tmp_path,
        registry,
        snapshot=snapshot,
        state=state,
        launched=launched,
        producer_calls=producer_calls,
        monkeypatch=monkeypatch,
    )

    # 下一拍：resume 迴圈真的建立並啟動一顆 planner job。
    assert summary["errors"] == []
    jobs = registry.list_jobs()
    assert len(jobs) == 1
    planner_job = jobs[0]
    assert planner_job["workflow_run_id"] == run_id
    assert planner_job["persona"] == "planner"
    assert planner_job["workflow_phase"] == "plan"
    assert planner_job["workflow_card"] == "writing-plans"
    assert planner_job["workflow_claim_key"] == recovered.claim_key
    assert planner_job["status"] == "dispatched"
    assert launched == [planner_job["job_id"]]
    after_tick = registry.get_workflow_run(run_id)
    assert after_tick.status == "ongoing"
    assert after_tick.current_phase == "plan"
    assert "needs_human" not in after_tick.facets


def test_r05_brainstorm_required_next_tick_reenters_the_define_producer_without_a_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """brainstorm 必要：run 留在 define；下一拍由 auto-claim 重進 define producer，
    card dispatcher 不為 define 建 Job（planning 在 producer 內同步執行）。"""

    run_id, registry, state, snapshot = _seed(tmp_path, brainstorm_required=True)
    launched: list[str] = []
    producer_calls: list[dict] = []

    # 對照組：needs_human 的 run 在 recover 之前不會被 tick 重新送進 producer
    # （#373：auto-claim 不得自動重試 needs_human run）。
    blocked_tick = _next_periodic_tick(
        tmp_path,
        registry,
        snapshot=snapshot,
        state=state,
        launched=launched,
        producer_calls=producer_calls,
        monkeypatch=monkeypatch,
    )
    assert blocked_tick["errors"] == []
    assert producer_calls == []
    assert registry.list_jobs() == []

    response = _recover_through_daemon_request(
        tmp_path, run_id, registry, state, snapshot, launched
    )

    assert response["result"]["reason"] == "planning-recovery-unblocked"
    assert response["result"]["recovered_phase"] == "define"
    assert registry.list_jobs() == []
    assert launched == []
    recovered = registry.get_workflow_run(run_id)

    summary = _next_periodic_tick(
        tmp_path,
        registry,
        snapshot=snapshot,
        state=state,
        launched=launched,
        producer_calls=producer_calls,
        monkeypatch=monkeypatch,
    )

    assert summary["errors"] == []
    # 下一拍實際做的事：以同一個 canonical claim 重進 define producer，
    # 且沿用 production 的 planning runtime（brainstorm 在 producer 內執行）。
    assert producer_calls == [
        {
            "work_id": "demo",
            "claim_key": recovered.claim_key,
            "needs_human_reason": None,
            "runtime_factory": (
                manager_daemon.planning_runtime.build_production_planning_runtime
            ),
        }
    ]
    assert [
        (claim["work_id"], claim["action"]) for claim in summary["auto_claims"]
    ] == [("demo", "resume")]
    # resume 迴圈看到 define run，但 card dispatcher 不接受 define：零 Job、
    # 零 launcher 呼叫，run 也沒有因此被標回 needs_human。
    assert registry.list_jobs() == []
    assert launched == []
    after_tick = registry.get_workflow_run(run_id)
    assert after_tick.status == "ongoing"
    assert after_tick.current_phase == "define"
    assert "needs_human" not in after_tick.facets
    assert after_tick.gate_refs == ()
