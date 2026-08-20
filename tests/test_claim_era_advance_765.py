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
        self.assertIn('job.get("workflow_claim_key") == run.claim_key', selection)

    def test_dispatch_matching_stays_era_agnostic(self) -> None:
        """派工端 matching（instance 編號＋retry-context）維持全 era。"""

        source = inspect.getsource(manager._dispatch_workflow_card)
        anchor = source.index("matching = [")
        block = source[anchor : anchor + 600]
        self.assertNotIn("workflow_claim_key", block)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
