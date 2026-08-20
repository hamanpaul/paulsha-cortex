"""#760：--skip-tests 的 FullSuiteEvidence 契約接線（producer＋consumer）。

manager 環境是第三個 env-red 執行面；gate 已在自己的環境獨立跑過全套且綠、CI 又會
在 PR 上重驗，delivery preflight 第三跑只會把已驗訊號變成結構性 block。
"""

from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from paulsha_cortex.coordinator import manager, preflight, work_bridge


class RecordAndLoadRoundtripTests(unittest.TestCase):
    def test_external_record_is_loadable_and_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = "a" * 40
            now = time.time()
            written = preflight.record_external_full_suite_evidence(
                tree_hash=tree,
                command=("python3", "-m", "pytest", "-q"),
                completed_at_epoch=now,
                state_root=tmp,
            )
            self.assertEqual(written.tree_hash, tree)
            fresh = preflight.fresh_full_suite_evidence(
                tree_hash=tree, now_epoch=now + 60, state_root=tmp
            )
            self.assertIsNotNone(fresh)
            self.assertEqual(fresh.evidence_hash, written.evidence_hash)

    def test_identical_rewrite_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = "b" * 40
            kwargs = dict(
                tree_hash=tree,
                command=("python3", "-m", "pytest", "-q"),
                completed_at_epoch=123.0,
                state_root=tmp,
            )
            first = preflight.record_external_full_suite_evidence(**kwargs)
            second = preflight.record_external_full_suite_evidence(**kwargs)
            self.assertEqual(first.evidence_hash, second.evidence_hash)

    def test_stale_or_absent_evidence_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = "c" * 40
            self.assertIsNone(
                preflight.fresh_full_suite_evidence(
                    tree_hash=tree, now_epoch=time.time(), state_root=tmp
                )
            )
            preflight.record_external_full_suite_evidence(
                tree_hash=tree,
                command=("python3", "-m", "pytest", "-q"),
                completed_at_epoch=100.0,
                state_root=tmp,
            )
            self.assertIsNone(
                preflight.fresh_full_suite_evidence(
                    tree_hash=tree,
                    now_epoch=100.0 + preflight.DEFAULT_FULL_SUITE_MAX_AGE_SECONDS + 1,
                    state_root=tmp,
                )
            )


def _make_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


class ConsumerPredicateTests(unittest.TestCase):
    def test_requests_skip_only_when_fresh_evidence_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            candidate = _make_repo(repo)
            with mock.patch.object(
                work_bridge, "load_fresh_full_suite_evidence", return_value=object()
            ) as loader:
                self.assertTrue(
                    work_bridge._candidate_skip_tests_request(
                        worktree=repo, candidate=candidate, now=time.time
                    )
                )
                tree = subprocess.run(
                    ["git", "rev-parse", f"{candidate}^{{tree}}"],
                    cwd=repo, capture_output=True, text=True, check=True,
                ).stdout.strip().lower()
                self.assertEqual(loader.call_args.kwargs["tree_hash"], tree)
            with mock.patch.object(
                work_bridge, "load_fresh_full_suite_evidence", return_value=None
            ):
                self.assertFalse(
                    work_bridge._candidate_skip_tests_request(
                        worktree=repo, candidate=candidate, now=time.time
                    )
                )

    def test_unreadable_worktree_never_requests_skip(self) -> None:
        self.assertFalse(
            work_bridge._candidate_skip_tests_request(
                worktree=Path("/nonexistent"), candidate="d" * 40, now=time.time
            )
        )


class ProducerTests(unittest.TestCase):
    def _job(self, log="/l/x.jsonl"):
        return {"log_path": log}

    class _Run:
        workspace_root = None

    def test_red_required_semantics_never_record(self) -> None:
        """ledger pytest failed（tdd-red 的 RED）不得留下 full-suite evidence。"""

        with mock.patch.object(
            manager.terminal_contract, "read_gate_ledger",
            return_value=({"gates": [{"name": "pytest", "exit_code": 1, "status": "failed"}]}, "d"),
        ), mock.patch.object(manager.preflight, "record_external_full_suite_evidence") as rec:
            manager._record_candidate_full_suite_evidence(
                self._job(), run=self._Run(), candidate="e" * 40
            )
        rec.assert_not_called()

    def test_green_ledger_records_candidate_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            candidate = _make_repo(repo)
            run = type("R", (), {"workspace_root": str(repo)})()
            with mock.patch.object(
                manager.terminal_contract, "read_gate_ledger",
                return_value=({"gates": [{"name": "pytest", "exit_code": 0, "status": "passed"}]}, "d"),
            ), mock.patch.object(
                manager.gate_ledger, "load_gate_specs",
                return_value=(type("S", (), {"name": "pytest", "argv": ("python3", "-m", "pytest", "-q")})(),),
            ), mock.patch.object(
                manager.preflight, "record_external_full_suite_evidence"
            ) as rec:
                manager._record_candidate_full_suite_evidence(
                    self._job(), run=run, candidate=candidate
                )
            rec.assert_called_once()
            tree = subprocess.run(
                ["git", "rev-parse", f"{candidate}^{{tree}}"],
                cwd=repo, capture_output=True, text=True, check=True,
            ).stdout.strip().lower()
            self.assertEqual(rec.call_args.kwargs["tree_hash"], tree)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
