"""#716：deployment canary 的 Copilot findings 修正回合要能從真實的 `work show` 投影觸發。

`qualification/driver.py::_copilot_findings_candidate` 讀 `cortex work show --json` 的
`blocking_reason.context`（`delivery_reason`、`candidate`），再以 `retry-build` 交給
builder 修正。但 monitor 投影 needs_human 理由時只帶 reason／detail／source／
evidence_refs，`context` 被丟掉——driver 的單元測試用的是手寫 envelope，實機上修正
回合從未觸發過（canary run 36812339738 直接以 delivery-needs-human 收場）。

本檔從 registry 真實寫入的 DiagnosticReason 出發，經 `WorkflowRegistryProvider` 投影，
再交給 driver 判斷，鎖住整條路徑。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paulsha_cortex.coordinator.diagnostics import diagnostic_reason  # noqa: E402
from paulsha_cortex.coordinator.registry import JobRegistry  # noqa: E402
from paulsha_cortex.monitor.providers import WorkflowRegistryProvider  # noqa: E402
from test_diagnostic_invariant_family_527 import _seed_run  # noqa: E402
from test_qualification_driver_hardening import _load_driver  # noqa: E402

CANDIDATE = "c" * 40


def _project(tmp_path: Path, **context: str) -> dict:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    run = _seed_run(registry)
    # 與 manager 的 ship validator 寫入同形（`manager.apply_workflow_action:advance-ship`）。
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        needs_human_reason=diagnostic_reason(
            "delivery-needs-human",
            "ship validator 判定交付需要人工介入：copilot-findings",
            source="manager.apply_workflow_action:advance-ship",
            run_id=run.run_id,
            work_id=run.work_id,
            card="code-review",
            **context,
        ),
    )
    result = WorkflowRegistryProvider("hamanpaul/paulsha-cortex", state_path=state).scan()
    assert result.status == "ok"
    return result.observations["needs_human_reasons"][run.work_id]


def test_work_show_projection_carries_the_reason_context(tmp_path: Path) -> None:
    projected = _project(
        tmp_path, candidate=CANDIDATE, delivery_reason="copilot-findings"
    )

    assert projected["context"]["candidate"] == CANDIDATE
    assert projected["context"]["delivery_reason"] == "copilot-findings"
    assert projected["context"]["card"] == "code-review"


def test_canary_fix_round_triggers_from_the_real_projection(tmp_path: Path) -> None:
    driver = _load_driver()
    projected = _project(
        tmp_path, candidate=CANDIDATE, delivery_reason="copilot-findings"
    )

    # `monitor.work_api.get_work_item` 把這一列原樣放進 envelope 的 `blocking_reason`。
    assert driver._copilot_findings_candidate({"blocking_reason": projected}) == CANDIDATE


def test_canary_fix_round_ignores_other_delivery_reasons(tmp_path: Path) -> None:
    driver = _load_driver()
    projected = _project(
        tmp_path, candidate=CANDIDATE, delivery_reason="copilot-review-timeout"
    )

    assert driver._copilot_findings_candidate({"blocking_reason": projected}) is None


def test_canary_fix_round_triggers_from_a_projected_verify_stop(tmp_path: Path) -> None:
    """#716：verify 明示停止的 context 帶 candidate，經真實投影後 driver 能走 retry-build。"""
    driver = _load_driver()
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    run = _seed_run(registry)
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        needs_human_reason=diagnostic_reason(
            "verification-terminal-explicit-stop",
            "verification terminal 明示要求停止（status=failed）：summary=tasks unchecked",
            source="manager._poll_workflow_job:explicit-stop",
            run_id=run.run_id,
            work_id=run.work_id,
            card="verification",
            phase="verify",
            candidate=CANDIDATE,
        ),
    )
    result = WorkflowRegistryProvider("hamanpaul/paulsha-cortex", state_path=state).scan()
    projected = result.observations["needs_human_reasons"][run.work_id]

    assert driver._canary_fix_round({"blocking_reason": projected}) == (
        CANDIDATE,
        "verification-terminal-explicit-stop",
    )
