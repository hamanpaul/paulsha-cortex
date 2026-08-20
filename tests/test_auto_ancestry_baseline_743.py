"""#743：auto 路徑的 ancestry baseline 與採信端同一條導出。

採信端的 baseline 是 `run.candidate_head or job["dispatch_head"]`；#738 首版的
auto 路徑各算一份（直接拿 `dispatch_head`——run 層級、claim 時凍結、後續卡逐字
繼承首張卡的值），中段 build 卡的 ledger 於是量在錯的基線上、被「baseline 不符
視同缺席」守衛正確拒絕，每張卡都得人工 `regenerate-gates`。
"""

from __future__ import annotations

import unittest
from unittest import mock

from paulsha_cortex.coordinator import gate_runner, manager


class _Run:
    def __init__(self, candidate_head):
        self.candidate_head = candidate_head


class _Registry:
    def __init__(self, run):
        self._run = run

    def get_workflow_run(self, run_id):
        if self._run is None:
            raise KeyError(run_id)
        return self._run


class GateAncestryBaselineTests(unittest.TestCase):
    def test_candidate_head_wins_when_present(self) -> None:
        head = "B" * 40
        baseline = manager._gate_ancestry_baseline(
            _Registry(_Run(head)), {"workflow_run_id": "workflow-x"}
        )
        self.assertEqual(baseline, "b" * 40)

    def test_absent_candidate_falls_back_to_job_derivation(self) -> None:
        for run in (_Run(None), None):
            baseline = manager._gate_ancestry_baseline(
                _Registry(run), {"workflow_run_id": "workflow-x"}
            )
            self.assertIsNone(baseline)
        self.assertIsNone(manager._gate_ancestry_baseline(None, {}))


class EnsureGateLedgerBaselineTests(unittest.TestCase):
    def _capture_baseline(self, *, job, explicit):
        captured = {}

        def fake_run_declared_gates(**kwargs):
            captured.update(kwargs)
            return {"gates": []}

        env = {"PSC_JOB_RUNNER": "systemd-template"}
        with mock.patch.object(
            gate_runner, "run_declared_gates", side_effect=fake_run_declared_gates
        ), mock.patch.object(
            gate_runner, "spool_key_for_job", return_value="wf-x-card-1"
        ), mock.patch.object(
            gate_runner.Path, "exists", return_value=False
        ), mock.patch.object(
            gate_runner.Path, "is_dir", return_value=True
        ):
            gate_runner.ensure_gate_ledger(
                job,
                phases=frozenset({"build"}),
                env=env,
                ancestry_baseline=explicit,
            )
        return captured.get("ancestry_baseline")

    def _job(self, dispatch_head):
        return {
            "workflow_phase": "build",
            "log_path": "/tmp/x/wf-x-card-1.jsonl",
            "worktree": "/tmp/x/worktree",
            "job_id": "wf-x-card-1",
            "dispatch_head": dispatch_head,
        }

    def test_explicit_baseline_overrides_job_derivation(self) -> None:
        baseline = self._capture_baseline(
            job=self._job("a" * 40), explicit="B" * 40
        )
        self.assertEqual(baseline, "b" * 40)

    def test_without_explicit_baseline_dispatch_head_is_used(self) -> None:
        baseline = self._capture_baseline(job=self._job("a" * 40), explicit=None)
        self.assertEqual(baseline, "a" * 40)

    def test_malformed_explicit_baseline_becomes_none(self) -> None:
        baseline = self._capture_baseline(
            job=self._job(None), explicit="not-a-sha"
        )
        self.assertIsNone(baseline)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
