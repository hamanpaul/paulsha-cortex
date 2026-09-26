"""#1042：新欄位只在有值時序列化，舊版 Monitor 的封閉白名單讀新版 jobs.json 不會拒收。"""

from __future__ import annotations

from test_workflow_decomposition_depth_223 import _run

from paulsha_cortex.coordinator.workflow import WorkflowRun


def test_run_without_receipt_omits_new_plan_review_fields() -> None:
    payload = _run().to_dict()

    assert "plan_review_receipt" not in payload
    assert "planning_drift_stop" not in payload
    restored = WorkflowRun.from_dict(payload)
    assert restored.plan_review_receipt is None
    assert restored.planning_drift_stop is None
