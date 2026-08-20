"""#750：repair 回合的跨卡回饋——打回 candidate 的 verification 判定進 retry_context。

#606 的 retry_context 只看同一張卡的前次 job；verification 判定在另一張卡上且因
harvest fail-closed 沒有綁進 run，repair builder 因此盲修（實機：verification-22
failed → repair -23 加了測試 → verification-24 同因再 failed）。本檔釘：最近一顆
非通過 verify/review terminal 被選中、passing 略過、缺席回 None、欄位有界、
`_workflow_retry_context` 把它掛在 `review_rejection` 鍵下、首派仍回 None。
"""

from __future__ import annotations

import unittest
from unittest import mock

from paulsha_cortex.coordinator import manager


class _Run:
    run_id = "workflow-aaaaaaaaaaaaaaaaaaaa"


class _Registry:
    def __init__(self, jobs):
        self._jobs = jobs

    def list_jobs(self):
        return self._jobs


def _job(job_id, phase="verify", status="exited", log="/tmp/x.jsonl", run_id=_Run.run_id):
    return {
        "job_id": job_id,
        "workflow_run_id": run_id,
        "workflow_phase": phase,
        "status": status,
        "log_path": log,
    }


class PriorReviewRejectionTests(unittest.TestCase):
    def _with_terminals(self, jobs, terminals):
        """terminals: job log_path → raw terminal dict（模擬 _extract_terminal_json）。"""

        def fake_extract(log_path):
            raw = terminals.get(log_path)
            if raw is None:
                raise ValueError("no JSON")
            return raw

        return mock.patch.object(manager, "_extract_terminal_json", side_effect=fake_extract)

    def test_latest_non_passing_verify_terminal_is_selected(self) -> None:
        jobs = [
            _job("v-1", log="/l/1"),
            _job("v-2", log="/l/2"),
        ]
        terminals = {
            "/l/1": {"status": "failed", "summary": "old", "details": {}},
            "/l/2": {
                "status": "failed",
                "summary": "candidate fails: four checkpoints untouched",
                "details": {
                    "findings": [{"severity": "blocking", "requirement": "spec req 2"}],
                    "conformance": {"spec_req_2_checkpoints": "FAIL"},
                },
            },
        }
        with self._with_terminals(jobs, terminals):
            context = manager._prior_review_rejection(_Run(), _Registry(jobs))
        self.assertEqual(context["job_id"], "v-2")
        self.assertEqual(context["source"], "reviewer-terminal")
        self.assertIn("four checkpoints", context["summary"])
        self.assertIn("spec req 2", context["findings"][0])
        self.assertEqual(context["conformance"]["spec_req_2_checkpoints"], "FAIL")

    def test_passing_terminals_and_other_runs_are_skipped(self) -> None:
        jobs = [
            _job("v-ok", log="/l/ok"),
            _job("v-other", log="/l/other", run_id="workflow-bbbbbbbbbbbbbbbbbbbb"),
            _job("b-1", phase="build", log="/l/b"),
        ]
        terminals = {
            "/l/ok": {"status": "passed", "summary": "fine", "details": {}},
            "/l/other": {"status": "failed", "summary": "other run", "details": {}},
            "/l/b": {"status": "failed", "summary": "build", "details": {}},
        }
        with self._with_terminals(jobs, terminals):
            self.assertIsNone(manager._prior_review_rejection(_Run(), _Registry(jobs)))

    def test_absent_registry_or_unreadable_logs_return_none(self) -> None:
        self.assertIsNone(manager._prior_review_rejection(_Run(), None))
        jobs = [_job("v-1", log="/l/broken")]
        with self._with_terminals(jobs, {}):
            self.assertIsNone(manager._prior_review_rejection(_Run(), _Registry(jobs)))

    def test_summary_is_bounded(self) -> None:
        jobs = [_job("v-1", log="/l/1")]
        terminals = {
            "/l/1": {
                "status": "failed",
                "summary": "x" * (manager.RETRY_CONTEXT_EVIDENCE_LIMIT * 2),
                "details": {},
            }
        }
        with self._with_terminals(jobs, terminals):
            context = manager._prior_review_rejection(_Run(), _Registry(jobs))
        self.assertEqual(len(context["summary"]), manager.RETRY_CONTEXT_EVIDENCE_LIMIT)


class RetryContextMergeTests(unittest.TestCase):
    def test_rejection_lands_under_its_own_key(self) -> None:
        prior = [{"job_id": "b-1", "log_path": ""}]
        rejection = {"source": "reviewer-terminal", "status": "failed", "summary": "s"}
        context = manager._workflow_retry_context(prior, review_rejection=rejection)
        self.assertEqual(context["review_rejection"], rejection)
        self.assertEqual(context["attempt"], 2)

    def test_without_rejection_the_context_shape_is_unchanged(self) -> None:
        prior = [{"job_id": "b-1", "log_path": ""}]
        context = manager._workflow_retry_context(prior)
        self.assertNotIn("review_rejection", context)

    def test_first_dispatch_stays_none(self) -> None:
        self.assertIsNone(
            manager._workflow_retry_context((), review_rejection={"status": "failed"})
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
