"""#738：candidate 驗證下放 gate ledger 的行為契約。

三分部署下 Manager 讀不進 builder 的樹（#641 收掉唯讀 ACL），
`_verify_exact_candidate` 的 `git -C <job 樹>` 結構上必死。修法照 #629／#641
裁定：worktree 狀態（HEAD／dirty／ancestry）由 gate 執行身分在受控 checkout
（快照副本）上收集、寫進 ledger 的 `worktree_state`，Manager 只消費 ledger。
本檔釘四段：收集端、spool 驗證端、argv 封閉性、Manager 消費端（含「ledger
有 state 時完全不碰 builder 樹」與「缺席時逐字退回既有路徑」）。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from paulsha_cortex.coordinator import gate_ledger, gate_runner, manager, terminal_contract


def _git(cwd: Path, *argv: str) -> str:
    proc = subprocess.run(
        ["git", *argv], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _make_repo(root: Path) -> tuple[str, str]:
    """一個 base→head 兩個 commit 的 repo，回傳 (base_sha, head_sha)。"""

    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "b.txt").write_text("2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "head")
    head = _git(root, "rev-parse", "HEAD")
    return base.lower(), head.lower()


# ---------------------------------------------------------------------------
# 1. 收集端
# ---------------------------------------------------------------------------

class CollectWorktreeStateTests(unittest.TestCase):
    def test_clean_repo_reports_head_and_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base, head = _make_repo(repo)
            state = gate_ledger.collect_worktree_state(repo, ancestry_baseline=base)
            self.assertEqual(state["probe"], "ok")
            self.assertEqual(state["head"], head)
            self.assertEqual(state["dirty_total"], 0)
            self.assertEqual(state["dirty"], [])
            self.assertEqual(state["ancestry_baseline"], base)
            self.assertIs(state["ancestry_ok"], True)

    def test_non_descendant_baseline_is_false_not_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base, head = _make_repo(repo)
            # 從 base 岔出一條 side：head 不是 side 的祖先。
            _git(repo, "checkout", "-q", "-b", "side", base)
            (repo / "c.txt").write_text("3\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "side")
            state = gate_ledger.collect_worktree_state(repo, ancestry_baseline=head)
            self.assertEqual(state["probe"], "ok")
            self.assertIs(state["ancestry_ok"], False)

    def test_dirty_worktree_is_recorded_not_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _make_repo(repo)
            (repo / "stray.txt").write_text("x\n", encoding="utf-8")
            state = gate_ledger.collect_worktree_state(repo)
            self.assertEqual(state["dirty_total"], 1)
            self.assertIn("stray.txt", state["dirty"][0])

    def test_non_git_tree_reports_probe_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain"
            plain.mkdir()
            state = gate_ledger.collect_worktree_state(plain)
            self.assertTrue(str(state["probe"]).startswith("error:"), state)
            self.assertIsNone(state["head"])

    def test_malformed_baseline_reports_probe_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _make_repo(repo)
            state = gate_ledger.collect_worktree_state(
                repo, ancestry_baseline="not-a-sha"
            )
            self.assertTrue(str(state["probe"]).startswith("error:"), state)
            self.assertIsNone(state["ancestry_ok"])

    def test_write_gate_ledger_embeds_worktree_state(self) -> None:
        """direct 模式的落點：payload 與檔案都帶 `worktree_state`。"""

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base, head = _make_repo(repo)
            out = Path(tmp) / "ledger.json"
            payload = gate_ledger.write_gate_ledger(
                ledger_path=out,
                worktree=repo,
                env={},
                ancestry_baseline=base,
            )
            self.assertEqual(
                payload[gate_ledger.WORKTREE_STATE_KEY]["head"], head
            )
            on_disk = json.loads(out.read_text(encoding="utf-8"))
            self.assertIs(
                on_disk[gate_ledger.WORKTREE_STATE_KEY]["ancestry_ok"], True
            )


# ---------------------------------------------------------------------------
# 2. spool 驗證端
# ---------------------------------------------------------------------------

class ReadGateSpoolWorktreeStateTests(unittest.TestCase):
    def _spool_payload(self, state: object) -> dict:
        return {
            "schema_version": terminal_contract.GATE_LEDGER_SCHEMA_VERSION,
            "kind": terminal_contract.GATE_LEDGER_KIND,
            "slice_id": "s",
            "gates": [],
            gate_ledger.WORKTREE_STATE_KEY: state,
        }

    def _read(self, payload: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return gate_runner.read_gate_spool(path, env={})

    def test_valid_state_passes_through(self) -> None:
        head = "a" * 40
        result = self._read(
            self._spool_payload(
                {
                    "head": head,
                    "dirty": ["?? x.txt"],
                    "dirty_total": 1,
                    "probe": "ok",
                    "ancestry_baseline": "b" * 40,
                    "ancestry_ok": True,
                }
            )
        )
        state = result[gate_ledger.WORKTREE_STATE_KEY]
        self.assertEqual(state["head"], head)
        self.assertIs(state["ancestry_ok"], True)

    def test_absent_state_is_tolerated(self) -> None:
        payload = self._spool_payload(None)
        payload.pop(gate_ledger.WORKTREE_STATE_KEY)
        result = self._read(payload)
        self.assertNotIn(gate_ledger.WORKTREE_STATE_KEY, result)

    def test_malformed_head_fails_closed(self) -> None:
        with self.assertRaises(gate_runner.GateRunnerError) as ctx:
            self._read(
                self._spool_payload(
                    {
                        "head": "not-a-sha",
                        "dirty": [],
                        "dirty_total": 0,
                        "probe": "ok",
                        "ancestry_baseline": None,
                        "ancestry_ok": None,
                    }
                )
            )
        self.assertEqual(ctx.exception.reason, "gate-spool-invalid")

    def test_unbounded_dirty_list_fails_closed(self) -> None:
        with self.assertRaises(gate_runner.GateRunnerError):
            self._read(
                self._spool_payload(
                    {
                        "head": None,
                        "dirty": ["x"] * (gate_ledger.MAX_DIRTY_PATHS + 1),
                        "dirty_total": 999,
                        "probe": "ok",
                        "ancestry_baseline": None,
                        "ancestry_ok": None,
                    }
                )
            )


# ---------------------------------------------------------------------------
# 3. argv 封閉性
# ---------------------------------------------------------------------------

class BuildGateArgvTests(unittest.TestCase):
    def test_baseline_extends_argv_and_absence_leaves_it_unchanged(self) -> None:
        base_kwargs = dict(
            python="/opt/x/bin/python3",
            ledger_out="/spool/ledger.json",
            snapshot="/gate/snap",
            source_worktree="/worktree/job",
        )
        bare = gate_runner.build_gate_argv(**base_kwargs)
        self.assertNotIn("--assert-ancestor", bare)
        sha = "c" * 40
        argv = gate_runner.build_gate_argv(**base_kwargs, ancestry_baseline=sha)
        index = argv.index("--assert-ancestor")
        self.assertEqual(argv[index + 1], sha)
        # baseline 只是多兩個 token，前綴逐字不變——argv 仍然封閉。
        self.assertEqual(argv[: len(bare)], bare)


# ---------------------------------------------------------------------------
# 4. Manager 消費端
# ---------------------------------------------------------------------------

def _forbidden_git_runner(*argv, **kwargs):
    raise AssertionError(f"manager must not touch the builder tree: {argv!r}")


class _SentinelGitCalled(RuntimeError):
    pass


def _sentinel_git_runner(*argv, **kwargs):
    raise _SentinelGitCalled(str(argv))


class ManagerConsumesLedgerTests(unittest.TestCase):
    def _job(self, tmp: Path, *, candidate: str, dispatch_head: str | None = None) -> dict:
        log_path = tmp / "job.jsonl"
        log_path.write_text("", encoding="utf-8")
        job = {
            "subject_head": candidate,
            "worktree": str(tmp / "unreadable-builder-tree"),
            "persona": "builder",
            "log_path": str(log_path),
        }
        if dispatch_head is not None:
            job["dispatch_head"] = dispatch_head
        return job

    def _write_ledger(self, job: dict, state: dict) -> None:
        path = terminal_contract.gate_ledger_path(job["log_path"])
        payload = {
            "schema_version": terminal_contract.GATE_LEDGER_SCHEMA_VERSION,
            "kind": terminal_contract.GATE_LEDGER_KIND,
            "slice_id": "s",
            "gates": [],
            gate_ledger.WORKTREE_STATE_KEY: state,
        }
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    def _state(self, **overrides: object) -> dict:
        state: dict = {
            "head": None,
            "dirty": [],
            "dirty_total": 0,
            "probe": "ok",
            "ancestry_baseline": None,
            "ancestry_ok": None,
        }
        state.update(overrides)
        return state

    def test_ledger_head_match_never_touches_the_builder_tree(self) -> None:
        candidate = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), candidate=candidate)
            self._write_ledger(job, self._state(head=candidate))
            result = manager._verify_exact_candidate(
                job, git_runner=_forbidden_git_runner
            )
            self.assertEqual(result, candidate)

    def test_ledger_head_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), candidate="a" * 40)
            self._write_ledger(job, self._state(head="b" * 40))
            with self.assertRaises(ValueError) as ctx:
                manager._verify_exact_candidate(job, git_runner=_forbidden_git_runner)
            self.assertIn("not exact worktree HEAD", str(ctx.exception))

    def test_probe_error_falls_back_to_the_existing_git_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), candidate="a" * 40)
            self._write_ledger(
                job, self._state(head="a" * 40, probe="error: rev-parse HEAD: boom")
            )
            with self.assertRaises(_SentinelGitCalled):
                manager._verify_exact_candidate(job, git_runner=_sentinel_git_runner)

    def test_absent_ledger_falls_back_to_the_existing_git_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), candidate="a" * 40)
            with self.assertRaises(_SentinelGitCalled):
                manager._verify_exact_candidate(job, git_runner=_sentinel_git_runner)

    def test_reviewer_lane_is_untouched_by_the_ledger(self) -> None:
        """reviewer 走 Manager 自己 clone 的樹（#650），照舊直接 git。"""

        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), candidate="a" * 40)
            job["persona"] = "reviewer"
            job["workflow_repo_root"] = str(Path(tmp) / "manager-owned")
            self._write_ledger(job, self._state(head="a" * 40))
            with self.assertRaises(_SentinelGitCalled):
                manager._verify_exact_candidate(job, git_runner=_sentinel_git_runner)

    def test_transition_consumes_ledger_ancestry_true(self) -> None:
        candidate = "a" * 40
        baseline = "b" * 40
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), candidate=candidate, dispatch_head=baseline)
            self._write_ledger(
                job,
                self._state(
                    head=candidate, ancestry_baseline=baseline, ancestry_ok=True
                ),
            )
            result = manager._verify_build_candidate_transition(
                job, previous_candidate=None, git_runner=_forbidden_git_runner
            )
            self.assertEqual(result, candidate)

    def test_transition_consumes_ledger_ancestry_false(self) -> None:
        candidate = "a" * 40
        baseline = "b" * 40
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), candidate=candidate, dispatch_head=baseline)
            self._write_ledger(
                job,
                self._state(
                    head=candidate, ancestry_baseline=baseline, ancestry_ok=False
                ),
            )
            with self.assertRaises(ValueError) as ctx:
                manager._verify_build_candidate_transition(
                    job, previous_candidate=None, git_runner=_forbidden_git_runner
                )
            self.assertIn("not a descendant", str(ctx.exception))

    def test_transition_baseline_mismatch_is_treated_as_absent(self) -> None:
        """陳舊 ledger（針對別的基線量的答案）不得被採信——退回既有路徑。"""

        candidate = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), candidate=candidate, dispatch_head="b" * 40)
            self._write_ledger(
                job,
                self._state(
                    head=candidate, ancestry_baseline="c" * 40, ancestry_ok=True
                ),
            )
            with self.assertRaises(_SentinelGitCalled):
                manager._verify_build_candidate_transition(
                    job, previous_candidate=None, git_runner=_sentinel_git_runner
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
