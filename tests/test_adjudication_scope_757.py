"""#757：operator 裁決是 run 級，獨立於 retry_context 隨每次派工出現。

verify/review 的 retry-context matching 以 candidate 定錨——candidate 換新即空，
#752 把裁決掛在 retry_context 底下，裁決因此消失（實機 verification-37：
operator_adjudications=False，reviewer 重新升級已裁決的 D3 矛盾）。
"""

from __future__ import annotations

import inspect
import unittest

from paulsha_cortex.coordinator import manager


class AdjudicationScopeTests(unittest.TestCase):
    def test_prompt_builder_accepts_adjudications_independently(self) -> None:
        parameters = inspect.signature(manager._workflow_job_prompt).parameters
        self.assertIn("operator_adjudications", parameters)

    def test_dispatch_passes_adjudications_outside_retry_context(self) -> None:
        """呼叫端必須把裁決作為獨立參數傳遞，不得只藏在 retry_context 內。"""

        source = inspect.getsource(manager._dispatch_workflow_card)
        independent = "operator_adjudications=_operator_adjudications(run, coordinator_root)"
        self.assertIn(independent, source)

    def test_prompt_embeds_adjudications_as_their_own_contract_key(self) -> None:
        source = inspect.getsource(manager._workflow_job_prompt)
        self.assertIn('contract["operator_adjudications"]', source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
