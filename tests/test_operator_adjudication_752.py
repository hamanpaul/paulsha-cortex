"""#752：verify 階段的人裁通道——`retry-card --reason` → Manager evidence → prompt。

design/todo 矛盾這類判定 reviewer 只能 needs_human（design 被 planning authority
釘死不可 mid-run 修訂、builder 寫進 candidate 的註記不可採信、`review-attest` 只
受理 review phase）。operator 裁決經 bounded CLI 落成 Manager-owned immutable
evidence，dispatch 端讀回進 retry_context 的 `operator_adjudications` 鍵。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paulsha_cortex.coordinator import manager, work_actions


class _Run:
    run_id = "workflow-cccccccccccccccccccc"


def _write_evidence(root: Path, *, run_id: str, reason: str, created_at: str = "") -> Path:
    target_dir = root / "evidence" / "operator-adjudication"
    target_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "schema": work_actions.OPERATOR_ADJUDICATION_SCHEMA,
        "run_id": run_id,
        "card": "verification",
        "actor": "operator",
        "reason": reason,
        "created_at": created_at,
    }
    digest = f"{abs(hash(reason)) % (16**8):08x}" * 8
    path = target_dir / f"{run_id}-{digest[:64]}.json"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


class OperatorAdjudicationsReaderTests(unittest.TestCase):
    def test_reads_bounded_rows_for_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_evidence(root, run_id=_Run.run_id, reason="D3 superseded: todo wins")
            _write_evidence(root, run_id="workflow-dddddddddddddddddddd", reason="other run")
            rows = manager._operator_adjudications(_Run(), root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "operator-adjudication")
            self.assertIn("todo wins", rows[0]["reason"])

    def test_absent_directory_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(manager._operator_adjudications(_Run(), Path(tmp)))
        self.assertIsNone(manager._operator_adjudications(_Run(), None))

    def test_at_most_three_latest_rows_survive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import os, time
            for index in range(5):
                path = _write_evidence(
                    root, run_id=_Run.run_id, reason=f"ruling-{index}"
                )
                stamp = time.time() + index
                os.utime(path, (stamp, stamp))
            rows = manager._operator_adjudications(_Run(), root)
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[-1]["reason"], "ruling-4")


class RetryContextMergeTests(unittest.TestCase):
    def test_adjudications_land_under_their_own_key(self) -> None:
        prior = [{"job_id": "v-1", "log_path": ""}]
        rows = [{"source": "operator-adjudication", "reason": "todo wins"}]
        context = manager._workflow_retry_context(prior, operator_adjudications=rows)
        self.assertEqual(context["operator_adjudications"], rows)

    def test_without_adjudications_the_shape_is_unchanged(self) -> None:
        prior = [{"job_id": "v-1", "log_path": ""}]
        context = manager._workflow_retry_context(prior)
        self.assertNotIn("operator_adjudications", context)


class RetryCardReasonValidationTests(unittest.TestCase):
    def _call(self, reason):
        args = {
            "action": "retry-card",
            "repo": "o/r",
            "work_id": "w",
            "expected_run_id": "workflow-" + "a" * 20,
            "card": "verification",
            "reason": reason,
        }
        # 只驗前置檢查——非法 reason 必須在動到 run 之前被拒。
        with self.assertRaises(ValueError) as ctx:
            work_actions._retry_card_action(
                args=args, authority=None, workflow_registry=None, state_path=None
            )
        return str(ctx.exception)

    def test_empty_reason_is_rejected_before_any_side_effect(self) -> None:
        self.assertIn("reason", self._call("   "))

    def test_oversized_reason_is_rejected(self) -> None:
        self.assertIn("reason", self._call("x" * 4001))

    def test_reason_without_durable_state_path_is_rejected(self) -> None:
        message = self._call("D3 superseded: todo wins")
        self.assertIn("durable state path", message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
