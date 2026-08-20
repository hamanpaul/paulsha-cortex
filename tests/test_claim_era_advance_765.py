"""#765：advance 的 terminal-job 選擇必須以 claim era 過濾。

authority restart（#373）重算 claim_key 並把 verify/review 打回 pending——語意是
「在新 era 下重驗」。前代 era 的 terminal job 若仍被 advance 撿起，
`_job_for_workflow_card` 的 claim_key 綁定必炸且每 tick 重炸（實機：operator
`work link openspec` 觸發 restart 後 run 卡死於
`workflow job binding mismatch: workflow_claim_key`，resume 永遠到不了重新派工）。
"""

from __future__ import annotations

import inspect
import unittest

from paulsha_cortex.coordinator import manager


class ClaimEraAdvanceTests(unittest.TestCase):
    def test_advance_selection_filters_by_current_claim_key(self) -> None:
        """resume 的 jobs 選擇必須含 claim era 條件（source-pin：與 dispatch 端的
        matching 不同——那裡靠全 era 保 instance 編號唯一性，不得過濾）。"""

        source = inspect.getsource(manager.resume_workflow_run)
        anchor = source.index("job = jobs[-1] if jobs else dispatch_or_stop")
        selection = source[:anchor]
        selection = selection[selection.rindex("jobs = ["):]
        self.assertIn('job.get("workflow_claim_key") in (None, run.claim_key)', selection)

    def test_dispatch_matching_stays_era_agnostic(self) -> None:
        """派工端 matching 本身維持全 era（retry-context／sandbox 清理需要完整歷史），
        但 reuse／retry 判定走同 era 的 `reusable` 子集（#765 第五出口）。"""

        source = inspect.getsource(manager._dispatch_workflow_card)
        anchor = source.index("matching = [")
        list_block = source[anchor : source.index("]", anchor) + 1]
        self.assertNotIn("workflow_claim_key", list_block)
        reuse_anchor = source.index("reusable = [")
        reuse_block = source[reuse_anchor : source.index("]", reuse_anchor) + 1]
        self.assertIn('workflow_claim_key") in (None, run.claim_key)', reuse_block)
        self.assertIn("if reusable and not retryable_latest:", source)
        self.assertIn("return reusable[-1]", source)

class RetryCardEraTests(unittest.TestCase):
    def test_retry_card_target_jobs_filter_by_claim_era(self) -> None:
        from paulsha_cortex.coordinator import work_actions

        class _Run:
            run_id = "workflow-" + "a" * 20
            current_phase = "verify"
            candidate_head = "c" * 40
            claim_key = "claim:v1:" + "1" * 64

        def job(claim, evidence=None):
            return {
                "workflow_run_id": _Run.run_id,
                "workflow_phase": "verify",
                "workflow_card": "verification",
                "subject_head": _Run.candidate_head,
                "workflow_claim_key": claim,
                "workflow_evidence": evidence,
            }

        old_era = job("claim:v1:" + "0" * 64, evidence={"bound": True})
        current = job(_Run.claim_key)
        rows = work_actions._retry_card_target_jobs(
            _Run(), [old_era, current], card="verification"
        )
        self.assertEqual(rows, [current])

class RecoveryEraTests(unittest.TestCase):
    def test_recovery_selection_filters_by_claim_era(self) -> None:
        source = inspect.getsource(manager.resume_workflow_run)
        anchor = source.index("recovery_jobs = [")
        block = source[anchor : anchor + 900]
        self.assertIn('job.get("workflow_claim_key") in (None, run.claim_key)', block)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
