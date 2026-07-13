from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.coordinator import autonomy, completion, verification
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.registry import JobRegistry


def _git_ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _git_nonzero(returncode: int = 1) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="", stderr="")


def _meta(
    *,
    slice_id: str,
    spec_path: Path,
    dispatch: str = "auto",
    depends_on: list[str] | None = None,
    target_branch: str = "main",
    target_remote: str = "origin",
) -> dict:
    verification_contract = {
        "docs_class": "code",
        "review_policy": "required",
        "required_artifacts": [],
        "checks": [
            {"kind": "persona-scope"},
            {
                "kind": "command",
                "name": "policy",
                "argv": ["python3", "-m", "pytest", "-q"],
                "cwd": ".",
                "timeout_seconds": 30,
            },
        ],
        "tests": [],
        "full_suite": {
            "argv": ["python3", "-m", "pytest", "-q"],
            "cwd": ".",
            "timeout_seconds": 60,
            "baseline": "no-regression",
        },
    }
    return {
        "path": str(spec_path),
        "dispatch": dispatch,
        "slice_id": slice_id,
        "plan": f"docs/superpowers/plans/{slice_id}.md",
        "depends_on": list(depends_on or []),
        "target_branch": target_branch,
        "verification": verification_contract,
        "_pinned_inputs": {
            "spec_path": str(spec_path),
            "spec_hash": "0" * 64,
            "plan_path": f"docs/superpowers/plans/{slice_id}.md",
            "plan_hash": "1" * 64,
            "target_branch": target_branch,
            "target_remote": target_remote,
            "verification_hash": "2" * 64,
            "verification": verification_contract,
        },
    }


def _seed_dependency_completion(
    *,
    root: Path,
    handoff_dir: Path,
    slice_id: str,
    candidate: str,
    target_sha: str,
    target_branch: str = "main",
    target_remote: str = "origin",
) -> dict:
    verification_ref = verification.write_verification_evidence(
        {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": slice_id,
            "candidate": candidate,
            "status": "verified",
            "summary": "verification-succeeded",
            "details": {"ok": True},
        },
        coordinator_root=root,
    )
    record = completion.write_completion_record(
        {
            "schema_version": completion.COMPLETION_SCHEMA_VERSION,
            "slice_id": slice_id,
            "spec_hash": "0" * 64,
            "plan_hash": "1" * 64,
            "verification_hash": "2" * 64,
            "builder_job_id": f"{slice_id}-builder-1",
            "reviewer_job_id": None,
            "dispatch_base": "a" * 40,
            "candidate": candidate,
            "target_branch": target_branch,
            "target_remote": target_remote,
            "target_ref": f"refs/remotes/{target_remote}/{target_branch}",
            "target_ref_sha": target_sha,
            "verification_evidence_path": verification_ref["path"],
            "verification_evidence_hash": verification_ref["hash"],
            "review_policy": "not-required",
            "docs_class": "informational",
            "review_evaluation_path": None,
            "review_evaluation_hash": None,
            "completed_at": "2026-07-12T00:00:00+00:00",
        },
        coordinator_root=root,
    )
    manifest_path = handoff_dir / f"{slice_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "slice_id": slice_id,
                "job_id": f"{slice_id}-builder-1",
                "gate_status": "passed",
                "completion": "exited",
                "exit_code": 0,
                "branch": f"feature/{slice_id}",
                "gate_reason": "candidate-merged",
                "gate_verdict": None,
                "verification_evidence_path": verification_ref["path"],
                "verification_evidence_hash": verification_ref["hash"],
                "review_evaluation_path": None,
                "review_evaluation_hash": None,
                "completion_record_path": record["path"],
                "completion_record_hash": record["hash"],
                "slice_state": "completed",
                "spec_hash": "0" * 64,
                "plan_hash": "1" * 64,
                "verification_hash": "2" * 64,
                "completed_at": "2026-07-12T00:00:00+00:00",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return record


class _NoPaneSender:
    def send(self, pane_id, text):  # pragma: no cover - headless path must not call
        raise AssertionError("headless dispatch must not send pane commands")


class _RecordingWorktreeCreator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def create(self, branch: str, *, base_sha: str | None = None) -> str:
        self.calls.append((branch, base_sha))
        return f"/fake/wt/{branch.replace('/', '-')}"


class _Launcher:
    def launch(self, *, slice_id, prompt, worktree, log_dir):
        return LaunchHandle(
            executor="copilot",
            model_id=None,
            session_name=slice_id,
            pid=321,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


class DependencyAncestryDispatchTests(unittest.TestCase):
    def test_dispatch_ready_uses_target_ref_sha_for_worktree_base(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".git").mkdir()
            handoff = root / "handoff"
            target_sha = "c" * 40
            candidate = "b" * 40
            _seed_dependency_completion(
                root=root,
                handoff_dir=handoff,
                slice_id="up",
                candidate=candidate,
                target_sha=target_sha,
            )

            reg = JobRegistry(state_path=root / "jobs.json")
            wt = _RecordingWorktreeCreator()
            dispatcher = Dispatcher(registry=reg, pane_sender=_NoPaneSender(), worktree_creator=wt)
            metas = [
                _meta(slice_id="up", spec_path=root / "specs" / "up.md", dispatch="hold"),
                _meta(slice_id="down", spec_path=root / "specs" / "down.md", depends_on=["up"]),
            ]

            def git_runner(args: list[str]):
                key = tuple(args)
                mapping = {
                    ("-C", str(root), "fetch", "--no-tags", "origin", "main"): "",
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/main"): target_sha,
                    ("-C", str(root), "merge-base", "--is-ancestor", candidate, target_sha): "",
                    ("rev-parse", "feature/down"): "d" * 40,
                }
                return mapping[key]

            jobs = autonomy.dispatch_ready(
                metas,
                is_satisfied=lambda slice_id: slice_id == "up",
                dispatcher=dispatcher,
                launcher=_Launcher(),
                handoff_dir=str(handoff),
                git_runner=git_runner,
            )

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["task"], "down")
            self.assertEqual(wt.calls, [("feature/down", target_sha)])
            self.assertEqual(reg.get_slice("down")["dispatch_base"], target_sha)

    def test_dispatch_ready_blocks_when_upstream_candidate_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".git").mkdir()
            handoff = root / "handoff"
            target_sha = "c" * 40
            candidate = "b" * 40
            _seed_dependency_completion(
                root=root,
                handoff_dir=handoff,
                slice_id="up",
                candidate=candidate,
                target_sha=target_sha,
            )

            reg = JobRegistry(state_path=root / "jobs.json")
            wt = _RecordingWorktreeCreator()
            dispatcher = Dispatcher(registry=reg, pane_sender=_NoPaneSender(), worktree_creator=wt)
            metas = [
                _meta(slice_id="up", spec_path=root / "specs" / "up.md", dispatch="hold"),
                _meta(slice_id="down", spec_path=root / "specs" / "down.md", depends_on=["up"]),
            ]

            def git_runner(args: list[str]):
                key = tuple(args)
                mapping = {
                    ("-C", str(root), "fetch", "--no-tags", "origin", "main"): _git_ok(""),
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/main"): _git_ok(target_sha),
                    ("-C", str(root), "merge-base", "--is-ancestor", candidate, target_sha): _git_nonzero(1),
                }
                return mapping[key]

            with self.assertRaises(autonomy.DispatchReadyError):
                autonomy.dispatch_ready(
                    metas,
                    is_satisfied=lambda slice_id: slice_id == "up",
                    dispatcher=dispatcher,
                    launcher=_Launcher(),
                    handoff_dir=str(handoff),
                    git_runner=git_runner,
                )
            self.assertEqual(reg.list_jobs(), [])
            self.assertEqual(wt.calls, [])

    def test_dispatch_ready_blocks_dependency_target_branch_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".git").mkdir()
            handoff = root / "handoff"
            target_sha = "c" * 40
            candidate = "b" * 40
            _seed_dependency_completion(
                root=root,
                handoff_dir=handoff,
                slice_id="up",
                candidate=candidate,
                target_sha=target_sha,
                target_branch="release",
            )

            reg = JobRegistry(state_path=root / "jobs.json")
            dispatcher = Dispatcher(
                registry=reg,
                pane_sender=_NoPaneSender(),
                worktree_creator=_RecordingWorktreeCreator(),
            )
            metas = [
                _meta(slice_id="up", spec_path=root / "specs" / "up.md", dispatch="hold", target_branch="release"),
                _meta(slice_id="down", spec_path=root / "specs" / "down.md", depends_on=["up"], target_branch="main"),
            ]

            def git_runner(args: list[str]):
                key = tuple(args)
                mapping = {
                    ("-C", str(root), "fetch", "--no-tags", "origin", "main"): _git_ok(""),
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/main"): _git_ok(target_sha),
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/release"): _git_ok(target_sha),
                    ("-C", str(root), "merge-base", "--is-ancestor", candidate, target_sha): _git_ok(""),
                }
                return mapping[key]

            with self.assertRaises(autonomy.DispatchReadyError):
                autonomy.dispatch_ready(
                    metas,
                    is_satisfied=lambda slice_id: slice_id == "up",
                    dispatcher=dispatcher,
                    launcher=_Launcher(),
                    handoff_dir=str(handoff),
                    git_runner=git_runner,
                )
            self.assertEqual(reg.list_jobs(), [])


if __name__ == "__main__":
    unittest.main()
