from __future__ import annotations

import inspect
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.autonomy import dispatch_ready
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


class FakeDispatcher:
    """包真 JobRegistry；poll_headless_done 依 poll_map 腳本化轉態。"""

    def __init__(self, registry: JobRegistry, poll_map: dict | None = None,
                 raise_on: set | None = None) -> None:
        self._registry = registry
        self._poll_map = poll_map or {}   # job_id -> "exited"/"failed"
        self._raise_on = raise_on or set()  # job_id -> 模擬 poll 例外

    def poll_headless_done(self, job_id: str) -> dict:
        if job_id in self._raise_on:
            raise RuntimeError(f"poll 爆炸: {job_id}")
        status = self._poll_map.get(job_id)
        if status is None:
            return self._registry.get_job(job_id)  # 仍在跑
        return self._registry.update_headless_result(
            job_id, status=status, exit_code=0 if status == "exited" else 1
        )


def _reg(tmp: str) -> JobRegistry:
    return JobRegistry(state_path=Path(tmp) / "jobs.json")


def _make_job(reg: JobRegistry, slice_id: str, *, worktree: str | None = None, branch: str | None = None) -> dict:
    return reg.create_job(
        task=slice_id, persona="builder", branch=branch or f"feature/{slice_id}",
        pane="", worktree=worktree or f"/wt/{slice_id}",
        executor="copilot", session_name=slice_id, pid=4242,
        log_path=f"/logs/{slice_id}.jsonl",
    )


def _dispatch_meta(slice_id: str, *, plan: str = "p.md") -> dict:
    return {
        "slice_id": slice_id,
        "dispatch": "auto",
        "plan": plan,
        "depends_on": [],
        "target_branch": "main",
        "verification": {
            "docs_class": "code",
            "review_policy": "required",
            "required_artifacts": [],
            "checks": [{"kind": "persona-scope"}],
            "tests": [],
            "full_suite": {
                "argv": ["python3", "-m", "pytest", "-q"],
                "cwd": ".",
                "timeout_seconds": 30,
                "baseline": "no-regression",
            },
        },
        "_pinned_inputs": {
            "spec_path": f"/specs/{slice_id}.md",
            "spec_hash": "0" * 64,
            "plan_path": plan,
            "plan_hash": "1" * 64,
            "target_branch": "main",
            "target_remote": "origin",
            "verification_hash": "2" * 64,
        },
    }


def _git_ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _proc_ok() -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _proc_fail(returncode: int) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="", stderr="")


def _persona_catalog(*, builder_paths: list[str]) -> str:
    return (
        "roles:\n"
        "  manager:\n"
        "    role: manager\n"
        "    version: v1\n"
        "    summary: manager\n"
        "    allowed_phases: [define, plan, build, verify, review, ship]\n"
        "    write_paths: [\"**\"]\n"
        "    allowed_tools: [bash]\n"
        "  builder:\n"
        "    role: builder\n"
        "    version: v1\n"
        "    summary: builder\n"
        f"    write_paths: [{', '.join(f'\"{path}\"' for path in builder_paths)}]\n"
        "    allowed_phases: [build, verify]\n"
        "    allowed_tools: [bash]\n"
        "  reviewer:\n"
        "    role: reviewer\n"
        "    version: v1\n"
        "    summary: reviewer\n"
        "    allowed_phases: [review]\n"
        "    write_paths: [\"**\"]\n"
        "    allowed_tools: [bash]\n"
    )


def _verification_contract(*, docs_class: str = "code") -> dict:
    return {
        "docs_class": docs_class,
        "review_policy": "required" if docs_class in {"code", "normative"} else "not-required",
        "required_artifacts": [],
        "checks": [
            {"kind": "persona-scope"},
            {
                "kind": "command",
                "name": "policy",
                "argv": ["python3", "-m", "pytest", "-q", "tests/policy.py"],
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


def _create_slice(
    reg: JobRegistry,
    root: Path,
    job: dict,
    *,
    docs_class: str = "code",
    dispatch_base: str = "a" * 40,
) -> dict:
    slice_id = job["task"]
    contract = _verification_contract(docs_class=docs_class)
    (root / ".git").mkdir(exist_ok=True)
    spec_path = root / "specs" / f"{slice_id}.md"
    plan_path = root / "docs" / "superpowers" / "plans" / f"{slice_id}.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        (
            "---\n"
            "dispatch: auto\n"
            f"slice_id: {slice_id}\n"
            f"plan: docs/superpowers/plans/{slice_id}.md\n"
            "target_branch: main\n"
            "verification:\n"
            f"  docs_class: {docs_class}\n"
            "  required_artifacts: []\n"
            "  checks:\n"
            "    - kind: persona-scope\n"
            "    - kind: command\n"
            "      name: policy\n"
            "      argv: [python3, -m, pytest, -q, tests/policy.py]\n"
            "      cwd: .\n"
            "      timeout_seconds: 30\n"
            "  tests: []\n"
            "  full_suite:\n"
            "    argv: [python3, -m, pytest, -q]\n"
            "    cwd: .\n"
            "    timeout_seconds: 60\n"
            "    baseline: no-regression\n"
            "---\n"
        ),
        encoding="utf-8",
    )
    plan_path.write_text("# plan\n", encoding="utf-8")
    try:
        return reg.create_slice(
            slice_id=slice_id,
            spec_path=str(spec_path),
            spec_hash=manager.verification.sha256_bytes(spec_path.read_bytes()),
            plan_path=str(plan_path),
            plan_hash=manager.verification.sha256_bytes(plan_path.read_bytes()),
            target_branch="main",
            target_remote="origin",
            verification_hash=manager.verification.canonical_json_hash(contract),
            verification=contract,
            dispatch_base=dispatch_base,
            builder_job_id=job["job_id"],
            reviewer_job_id=None,
            candidate=None,
        )
    finally:
        reg.update_slice(slice_id, state="building", builder_job_id=job["job_id"])


class CompleteTickDoneTests(unittest.TestCase):
    def test_exited_job_without_slice_proof_becomes_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-a")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads((hdir / "slice-a.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["completion"], "exited")
            self.assertEqual(manifest["slice_id"], "slice-a")
            self.assertEqual(manifest["completed_at"], "T0")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-a", "gate_status": "needs_human"}])
            self.assertFalse(manager.autonomy.default_is_satisfied("slice-a", handoff_dir=str(hdir)))
            self.assertEqual(summary["errors"], [])


class WorkflowJobPromptTests(unittest.TestCase):
    def _run(self, *, openspec_refs: tuple[str, ...] = ("issue-116",)) -> SimpleNamespace:
        return SimpleNamespace(
            run_id="run-1",
            work_id="work-1",
            repo="owner/repo",
            source_revision="a" * 40,
            candidate_head=None,
            openspec_refs=openspec_refs,
        )

    def test_commit_required_build_card_prompt_includes_tasks_guidance(self) -> None:
        step = WorkflowStep(
            phase="build",
            persona="builder",
            card="subagent-build",
            executor=None,
            model=None,
            domain=None,
            inputs=(),
            outputs=(),
        )

        prompt = manager._workflow_job_prompt(
            self._run(),
            step,
            builder_job_id="job-1",
            coordinator_root="/tmp/coordinator",
        )

        self.assertIn("openspec/changes/issue-116/tasks.md checkboxes", prompt)
        self.assertIn("never modify pinned input files such as the plan document", prompt)

    def test_commit_required_build_card_without_openspec_ref_uses_generic_tasks_path(self) -> None:
        step = WorkflowStep(
            phase="build",
            persona="builder",
            card="tdd-red",
            executor=None,
            model=None,
            domain=None,
            inputs=(),
            outputs=(),
        )

        prompt = manager._workflow_job_prompt(
            self._run(openspec_refs=()),
            step,
            builder_job_id="job-1",
            coordinator_root="/tmp/coordinator",
        )

        self.assertIn("openspec/changes/<change>/tasks.md checkboxes", prompt)
        self.assertIn("never modify pinned input files such as the plan document", prompt)

    def test_non_commit_required_cards_do_not_include_tasks_guidance(self) -> None:
        worktree_step = WorkflowStep(
            phase="build",
            persona="builder",
            card="worktree-isolation",
            executor=None,
            model=None,
            domain=None,
            inputs=(),
            outputs=(),
        )
        verification_step = WorkflowStep(
            phase="verify",
            persona="reviewer",
            card="verification",
            executor=None,
            model=None,
            domain=None,
            inputs=(),
            outputs=("reports/verify/work-1.md",),
        )

        worktree_prompt = manager._workflow_job_prompt(
            self._run(),
            worktree_step,
            builder_job_id=None,
            coordinator_root="/tmp/coordinator",
        )
        verification_prompt = manager._workflow_job_prompt(
            self._run(),
            verification_step,
            builder_job_id="job-1",
            coordinator_root="/tmp/coordinator",
            candidate_checkout="candidate",
        )

        self.assertNotIn("tasks.md checkboxes", worktree_prompt)
        self.assertNotIn("never modify pinned input files", worktree_prompt)
        self.assertNotIn("tasks.md checkboxes", verification_prompt)
        self.assertNotIn("never modify pinned input files", verification_prompt)


class CompleteTickFailedAndInFlightTests(unittest.TestCase):
    def test_failed_job_writes_failed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-b")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "failed"})
            hdir = Path(d) / "handoff"
            manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            manifest = json.loads((hdir / "slice-b.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "failed")
            self.assertEqual(manifest["completion"], "failed")

    def test_in_flight_job_not_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-c")
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            summary = manager.complete_tick(disp, handoff_dir=str(hdir))
            self.assertFalse((hdir / "slice-c.json").exists())
            self.assertEqual(summary["completed"], [])
            self.assertIn(job["job_id"], summary["polled"])


class CompleteTickReconcileTests(unittest.TestCase):
    def test_terminal_job_missing_manifest_is_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-d")
            reg.update_headless_result(job["job_id"], status="exited", exit_code=0)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            self.assertTrue((hdir / "slice-d.json").exists())
            self.assertEqual(summary["completed"], [{"slice_id": "slice-d", "gate_status": "needs_human"}])
            self.assertEqual(summary["polled"], [])

    def test_same_job_rescan_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-e")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            manifest_path = hdir / "slice-e.json"
            first_text = manifest_path.read_text(encoding="utf-8")
            first_mtime_ns = manifest_path.stat().st_mtime_ns
            second = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T1")
            second_text = manifest_path.read_text(encoding="utf-8")
            second_mtime_ns = manifest_path.stat().st_mtime_ns
            manifest = json.loads(second_text)
            self.assertEqual(manifest["job_id"], job["job_id"])
            self.assertEqual(manifest["completed_at"], "T0")
            self.assertEqual(first_text, second_text)
            self.assertEqual(first_mtime_ns, second_mtime_ns)
            self.assertEqual(second["completed"], [])
            self.assertEqual(second["polled"], [])

    def test_concurrent_same_slice_terminals_warn_and_dedup(self) -> None:
        # 不變量已由 registry 擋住；此處人工污染狀態檔，釘住 complete_tick 面對既有壞狀態
        # 仍會去重與記 warning 的降級行為。
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            first = _make_job(reg, "slice-dup")
            second = {**first, "job_id": "slice-dup-2", "worktree": "/wt/slice-dup-2"}
            reg._jobs.append(second)
            disp = FakeDispatcher(
                reg,
                poll_map={first["job_id"]: "failed", second["job_id"]: "exited"},
            )
            hdir = Path(d) / "handoff"

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            # 去重：兩個 terminal job 同 slice 只回一筆 completed。
            self.assertEqual(len(summary["completed"]), 1)
            self.assertEqual(summary["completed"][0]["slice_id"], "slice-dup")
            # warning 恰記一次。
            self.assertEqual(
                summary["warnings"],
                [{"slice_id": "slice-dup", "warning": "same-slice concurrent terminals"}],
            )
            # 後者勝一致性：manifest 與 completed 的 gate_status 一致，job_id 為兩者之一。
            manifest = json.loads((hdir / "slice-dup.json").read_text(encoding="utf-8"))
            self.assertIn(manifest["job_id"], {first["job_id"], second["job_id"]})
            self.assertEqual(summary["completed"][0]["gate_status"], manifest["gate_status"])

    def test_requeue_overwrites_manifest_for_new_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            first = _make_job(reg, "slice-requeue")
            disp = FakeDispatcher(reg, poll_map={first["job_id"]: "failed"})
            hdir = Path(d) / "handoff"

            manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            second_job = _make_job(reg, "slice-requeue")
            disp = FakeDispatcher(
                reg,
                poll_map={first["job_id"]: "failed", second_job["job_id"]: "exited"},
            )
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T1")

            manifest = json.loads((hdir / "slice-requeue.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["job_id"], second_job["job_id"])
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["completion"], "exited")
            self.assertEqual(manifest["completed_at"], "T1")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-requeue", "gate_status": "needs_human"}])
            from paulsha_cortex.coordinator import autonomy
            self.assertFalse(autonomy.default_is_satisfied("slice-requeue", handoff_dir=str(hdir)))

    def test_legacy_manifest_without_job_id_is_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-legacy")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "slice-legacy.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "slice_id": "slice-legacy",
                        "gate_status": "failed",
                        "completion": "failed",
                        "exit_code": 1,
                        "branch": "feature/legacy",
                        "gate_verdict": None,
                        "completed_at": "OLD",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["job_id"], job["job_id"])
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["completed_at"], "T0")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-legacy", "gate_status": "needs_human"}])

    def test_retryable_needs_human_manifest_is_reprocessed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-retryable")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "slice-retryable.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "slice_id": "slice-retryable",
                        "job_id": job["job_id"],
                        "gate_status": "needs_human",
                        "completion": "exited",
                        "exit_code": 0,
                        "branch": job["branch"],
                        "gate_reason": "verification-runner-error",
                        "gate_verdict": None,
                        "verification_evidence_path": None,
                        "verification_evidence_hash": None,
                        "completed_at": "OLD",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T1")

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["completed_at"], "T1")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-retryable", "gate_status": "needs_human"}])

    def test_same_job_legacy_passed_manifest_is_reprocessed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-legacy-pass")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "slice-legacy-pass.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "slice_id": "slice-legacy-pass",
                        "job_id": job["job_id"],
                        "gate_status": "passed",
                        "completion": "exited",
                        "exit_code": 0,
                        "branch": job["branch"],
                        "gate_verdict": {"legacy": True},
                        "gate_reason": None,
                        "completed_at": "OLD",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T1")

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["completed_at"], "T1")
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertFalse(manager.autonomy.default_is_satisfied("slice-legacy-pass", handoff_dir=str(hdir)))
            self.assertEqual(summary["completed"], [{"slice_id": "slice-legacy-pass", "gate_status": "needs_human"}])

    def test_corrupt_manifest_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-corrupt")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "slice-corrupt.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text("{not json", encoding="utf-8")

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["job_id"], job["job_id"])
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-corrupt", "gate_status": "needs_human"}])
            self.assertEqual(summary["errors"], [])

    def test_invalid_utf8_manifest_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-invalid-utf8")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "slice-invalid-utf8.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(b"\x80not-utf8")

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["job_id"], job["job_id"])
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-invalid-utf8", "gate_status": "needs_human"}])
            self.assertEqual(summary["errors"], [])

    def test_symlink_manifest_path_is_rejected_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-symlink")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "slice-symlink.json"
            target_path = Path(d) / "outside.json"
            target_path.write_text('{"outside": true}\n', encoding="utf-8")
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.symlink_to(target_path)

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            self.assertEqual(target_path.read_text(encoding="utf-8"), '{"outside": true}\n')
            self.assertEqual(summary["completed"], [])
            self.assertEqual([e["job_id"] for e in summary["errors"]], [job["job_id"]])

    def test_symlink_handoff_dir_still_writes_manifest(self) -> None:
        # 迴歸釘死：本專案部署 handoff_dir 落在 symlink 樹下（~/.agents → ~/notes/...）。
        # complete_tick MUST 正常寫盤，不得因上層 symlink 誤拒（原 _is_safe_handoff_root P0）。
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-hdir-link")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            real_dir = Path(d) / "real_state"
            real_dir.mkdir()
            hdir = Path(d) / "agents_link"  # symlink → real_state
            hdir.symlink_to(real_dir, target_is_directory=True)

            summary = manager.complete_tick(disp, handoff_dir=str(hdir / "handoff"), clock=lambda: "T0")

            manifest = json.loads((real_dir / "handoff" / "slice-hdir-link.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-hdir-link", "gate_status": "needs_human"}])
            self.assertEqual(summary["errors"], [])


class CompleteTickVerificationTests(unittest.TestCase):
    def test_verification_runner_exception_marks_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-f", worktree=str(worktree))
            _create_slice(reg, root, job, docs_class="code")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"

            def boom(**kwargs):
                raise RuntimeError("verify 爆炸")

            manager.complete_tick(
                disp,
                handoff_dir=str(hdir),
                clock=lambda: "T0",
                verification_runner=boom,
            )
            manifest = json.loads((hdir / "slice-f.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "verification-runner-error")
            self.assertFalse(manager.autonomy.default_is_satisfied("slice-f", handoff_dir=str(hdir)))

    def test_invalid_runner_candidate_payload_marks_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-runner-bad-candidate", worktree=str(worktree))
            _create_slice(reg, root, job, docs_class="code")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"
            candidate = "b" * 40

            summary = manager.complete_tick(
                disp,
                handoff_dir=str(hdir),
                clock=lambda: "T0",
                git_runner=lambda args: {
                    ("-C", str(root), "rev-parse", "feature/slice-runner-bad-candidate"): _git_ok(candidate),
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok(candidate),
                }[tuple(args)],
                verification_runner=lambda **kwargs: {
                    "path": str(root / "forged.json"),
                    "hash": "0" * 64,
                    "payload": {
                        "schema_version": 1,
                        "slice_id": "slice-runner-bad-candidate",
                        "candidate": "not-a-sha",
                        "status": "reviewing",
                        "summary": "verification-succeeded",
                        "details": {"ok": True},
                    },
                },
            )

            manifest = json.loads((hdir / "slice-runner-bad-candidate.json").read_text(encoding="utf-8"))
            slice_row = reg.get_slice("slice-runner-bad-candidate")
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "verification-runner-error")
            self.assertEqual(manifest["gate_verdict"]["status"], "needs_human")
            self.assertEqual(manifest["gate_verdict"]["summary"], "verification-runner-error")
            self.assertIsNotNone(manifest["verification_evidence_path"])
            self.assertEqual(slice_row["state"], "needs_human")
            self.assertEqual(slice_row["gate_state"], "needs_human")
            self.assertEqual(
                summary["completed"],
                [{"slice_id": "slice-runner-bad-candidate", "gate_status": "needs_human"}],
            )

    def test_tampered_runner_evidence_path_and_hash_mark_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-runner-bad-evidence", worktree=str(worktree))
            _create_slice(reg, root, job, docs_class="code")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"
            candidate = "b" * 40

            def verification_runner(**kwargs):
                evidence = manager.verification.write_verification_evidence(
                    {
                        "schema_version": 1,
                        "slice_id": "slice-runner-bad-evidence",
                        "candidate": candidate,
                        "status": "reviewing",
                        "summary": "verification-succeeded",
                        "details": {"ok": True},
                    },
                    coordinator_root=root,
                )
                return {
                    **evidence,
                    "path": str(root / "forged.json"),
                    "hash": "f" * 64,
                }

            summary = manager.complete_tick(
                disp,
                handoff_dir=str(hdir),
                clock=lambda: "T0",
                git_runner=lambda args: {
                    ("-C", str(root), "rev-parse", "feature/slice-runner-bad-evidence"): _git_ok(candidate),
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok(candidate),
                }[tuple(args)],
                verification_runner=verification_runner,
            )

            manifest = json.loads((hdir / "slice-runner-bad-evidence.json").read_text(encoding="utf-8"))
            slice_row = reg.get_slice("slice-runner-bad-evidence")
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "verification-runner-error")
            self.assertIsNone(manifest["gate_verdict"])
            self.assertIsNone(manifest["verification_evidence_path"])
            self.assertEqual(slice_row["state"], "needs_human")
            self.assertEqual(slice_row["gate_state"], "needs_human")
            self.assertEqual(
                summary["completed"],
                [{"slice_id": "slice-runner-bad-evidence", "gate_status": "needs_human"}],
            )

    def test_successful_code_verification_moves_slice_to_reviewing_without_releasing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-g", worktree=str(worktree))
            reg.attach_launch_handle(job["job_id"], executor="copilot", session_name="slice-g", pid=1, log_path="/log")
            reg._find_job(job["job_id"])["model_id"] = "claude-haiku-4.5"
            _create_slice(reg, root, job, docs_class="code")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"
            candidate = "b" * 40
            dispatch_base = "a" * 40
            persona_catalog = _persona_catalog(builder_paths=["**"])
            config_root = root / "config"
            config_root.mkdir(parents=True, exist_ok=True)
            (config_root / "model-identities.yaml").write_text(
                (
                    "schema_version: 1\n"
                    "identities:\n"
                    "  - executor: copilot\n"
                    "    model_id: claude-haiku-4.5\n"
                    "    independence_domain: anthropic\n"
                    "  - executor: codex\n"
                    "    model_id: gpt-5.4\n"
                    "    independence_domain: openai\n"
                ),
                encoding="utf-8",
            )

            class _GitRunner:
                def __init__(self) -> None:
                    self.calls = 0

                def __call__(self, args: list[str]):
                    review_worktree = str(root / ".psc-review-worktrees" / "slice-g-slice-g-2")
                    mapping = {
                        ("-C", str(root), "rev-parse", "feature/slice-g"): _git_ok(candidate),
                        ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok(candidate),
                        ("-C", review_worktree, "rev-parse", "HEAD"): _git_ok(candidate),
                        ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): _git_ok(""),
                        ("-C", str(root), "merge-base", "--is-ancestor", dispatch_base, candidate): _git_ok(""),
                        ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + ".." + candidate): _git_ok(""),
                        ("-C", str(root), "show", dispatch_base + ":paulsha_cortex/persona/personas.yaml"): _git_ok(persona_catalog),
                        ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + "..." + candidate): _git_ok(""),
                        ("-C", str(root), "worktree", "add", "--detach", str(root / ".psc-verification-worktrees" / "slice-g-aaaaaaaaaaaa"), dispatch_base): _git_ok(""),
                        ("-C", str(root), "worktree", "remove", "--force", str(root / ".psc-verification-worktrees" / "slice-g-aaaaaaaaaaaa")): _git_ok(""),
                    }
                    return mapping[tuple(args)]

            def proc_runner(argv, **kwargs):
                if "shell" in kwargs:
                    self.assertFalse(kwargs["shell"])
                if "env" in kwargs:
                    self.assertEqual(
                        set(kwargs["env"]) - {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV"},
                        set(),
                    )
                return _proc_ok()

            class _ReviewLauncher:
                def launch(self, *, slice_id, prompt, worktree, log_dir):
                    from paulsha_cortex.coordinator.launcher import LaunchHandle

                    return LaunchHandle(
                        executor="codex",
                        model_id="gpt-5.4",
                        session_name=slice_id,
                        pid=222,
                        log_path=f"{log_dir}/{slice_id}.jsonl",
                    )

            with mock.patch.dict("os.environ", {"PSC_PROJECT_CONFIG_ROOT": str(config_root)}, clear=False):
                summary = manager.complete_tick(
                    disp,
                    handoff_dir=str(hdir),
                    metas=[
                        {"slice_id": "slice-g", "dispatch": "auto", "plan": "p-g.md", "depends_on": []},
                        {"slice_id": "downstream", "dispatch": "auto", "plan": "p-down.md", "depends_on": ["slice-g"]},
                    ],
                    clock=lambda: "T0",
                    git_runner=_GitRunner(),
                    subprocess_runner=proc_runner,
                    review_launcher=_ReviewLauncher(),
                    review_executor="codex",
                    review_model="gpt-5.4",
                )

            slice_row = reg.get_slice("slice-g")
            self.assertFalse((hdir / "slice-g.json").exists())
            self.assertEqual(summary["completed"], [])
            self.assertEqual(slice_row["state"], "reviewing")
            self.assertEqual(slice_row["gate_state"], "pending")
            self.assertFalse(manager.autonomy.default_is_satisfied("slice-g", handoff_dir=str(hdir)))
            self.assertEqual(summary["released"], [])

    def test_review_required_slice_without_review_identity_becomes_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-review-absent", worktree=str(worktree))
            _create_slice(reg, root, job, docs_class="code")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"
            candidate = "b" * 40

            with mock.patch.dict(
                "os.environ",
                {
                    "PSC_PROJECT_CONFIG_ROOT": str(root / "config"),
                },
                clear=False,
            ):
                (root / "config").mkdir(parents=True, exist_ok=True)
                (root / "config" / "model-identities.yaml").write_text(
                    (
                        "schema_version: 1\n"
                        "identities:\n"
                        "  - executor: copilot\n"
                        "    model_id: claude-haiku-4.5\n"
                        "    independence_domain: anthropic\n"
                    ),
                    encoding="utf-8",
                )
                reg.attach_launch_handle(
                    job["job_id"], executor="copilot", session_name="slice-review-absent", pid=1, log_path="/log"
                )
                reg._find_job(job["job_id"])["model_id"] = "claude-haiku-4.5"
                summary = manager.complete_tick(
                    disp,
                    handoff_dir=str(hdir),
                    clock=lambda: "T0",
                    git_runner=lambda args: {
                        ("-C", str(root), "rev-parse", "feature/slice-review-absent"): _git_ok(candidate),
                        ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok(candidate),
                    }[tuple(args)],
                    verification_runner=lambda **kwargs: manager.verification.write_verification_evidence(
                        {
                            "schema_version": 1,
                            "slice_id": "slice-review-absent",
                            "candidate": candidate,
                            "status": "reviewing",
                            "summary": "verification-succeeded",
                            "details": {"ok": True},
                        },
                        coordinator_root=root,
                    ),
                )

            manifest = json.loads((hdir / "slice-review-absent.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_verdict"]["state"], "absent")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-review-absent", "gate_status": "needs_human"}])

    def test_existing_inflight_reviewer_prevents_duplicate_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-review-inflight", worktree=str(worktree))
            reg.attach_launch_handle(job["job_id"], executor="copilot", session_name="slice-review-inflight", pid=1, log_path="/log")
            reg._find_job(job["job_id"])["model_id"] = "claude-haiku-4.5"
            _create_slice(reg, root, job, docs_class="code")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"
            candidate = "b" * 40
            dispatch_base = "a" * 40
            persona_catalog = _persona_catalog(builder_paths=["**"])
            config_root = root / "config"
            config_root.mkdir(parents=True, exist_ok=True)
            (config_root / "model-identities.yaml").write_text(
                (
                    "schema_version: 1\n"
                    "identities:\n"
                    "  - executor: copilot\n"
                    "    model_id: claude-haiku-4.5\n"
                    "    independence_domain: anthropic\n"
                    "  - executor: codex\n"
                    "    model_id: gpt-5.4\n"
                    "    independence_domain: openai\n"
                ),
                encoding="utf-8",
            )

            class _GitRunner:
                def __call__(self, args: list[str]):
                    review_worktree = str(root / ".psc-review-worktrees" / "slice-review-inflight-slice-review-inflight-2")
                    return {
                        ("-C", str(root), "rev-parse", "feature/slice-review-inflight"): _git_ok(candidate),
                        ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok(candidate),
                        ("-C", review_worktree, "rev-parse", "HEAD"): _git_ok(candidate),
                        ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): _git_ok(""),
                        ("-C", str(root), "merge-base", "--is-ancestor", dispatch_base, candidate): _git_ok(""),
                        ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + ".." + candidate): _git_ok(""),
                        ("-C", str(root), "show", dispatch_base + ":paulsha_cortex/persona/personas.yaml"): _git_ok(persona_catalog),
                        ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + "..." + candidate): _git_ok(""),
                        ("-C", str(root), "worktree", "add", "--detach", str(root / ".psc-verification-worktrees" / "slice-review-inflight-aaaaaaaaaaaa"), dispatch_base): _git_ok(""),
                        ("-C", str(root), "worktree", "remove", "--force", str(root / ".psc-verification-worktrees" / "slice-review-inflight-aaaaaaaaaaaa")): _git_ok(""),
                        ("-C", str(root), "worktree", "add", "--detach", review_worktree, candidate): _git_ok(""),
                    }[tuple(args)]

            class _ReviewLauncher:
                def __init__(self) -> None:
                    self.calls = 0

                def launch(self, *, slice_id, prompt, worktree, log_dir):
                    from paulsha_cortex.coordinator.launcher import LaunchHandle

                    self.calls += 1
                    return LaunchHandle(
                        executor="codex",
                        model_id="gpt-5.4",
                        session_name=slice_id,
                        pid=222,
                        log_path=f"{log_dir}/{slice_id}.jsonl",
                    )

            launcher = _ReviewLauncher()
            with mock.patch.dict("os.environ", {"PSC_PROJECT_CONFIG_ROOT": str(config_root)}, clear=False):
                first = manager.complete_tick(
                    disp,
                    handoff_dir=str(hdir),
                    clock=lambda: "T0",
                    git_runner=_GitRunner(),
                    subprocess_runner=lambda *args, **kwargs: _proc_ok(),
                    review_launcher=launcher,
                    review_executor="codex",
                    review_model="gpt-5.4",
                )
                second = manager.complete_tick(
                    disp,
                    handoff_dir=str(hdir),
                    clock=lambda: "T1",
                    git_runner=_GitRunner(),
                    subprocess_runner=lambda *args, **kwargs: _proc_ok(),
                    review_launcher=launcher,
                    review_executor="codex",
                    review_model="gpt-5.4",
                )

            self.assertEqual(first["completed"], [])
            self.assertEqual(second["completed"], [])
            self.assertEqual(launcher.calls, 1)
            self.assertEqual(len(reg.list_jobs()), 2)

    def test_stale_review_job_preserves_history_but_clears_current_ref(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            builder = _make_job(reg, "slice-stale-review", worktree=str(root / "candidate"))
            reg.attach_launch_handle(builder["job_id"], executor="copilot", model_id="claude-haiku-4.5", session_name="slice-stale-review", pid=1, log_path="/log")
            reviewer = reg.create_job(
                task="slice-stale-review",
                persona="reviewer",
                kind="review",
                branch="feature/slice-stale-review",
                pane="",
                worktree=str(root / "review"),
                executor="codex",
                model_id="gpt-5.4",
                independence_domain="openai",
                subject_head="b" * 40,
                spec_hash="old-spec",
                plan_hash="old-plan",
                verification_hash="old-verification",
            )
            reg.update_status(reviewer["job_id"], "exited")
            slice_row = reg.create_slice(
                slice_id="slice-stale-review",
                spec_path=str(root / "spec.md"),
                spec_hash="new-spec",
                plan_path=str(root / "plan.md"),
                plan_hash="new-plan",
                target_branch="main",
                target_remote="origin",
                verification_hash="new-verification",
                verification={"docs_class": "code", "review_policy": "required"},
                dispatch_base="a" * 40,
                builder_job_id=builder["job_id"],
                reviewer_job_id=reviewer["job_id"],
                candidate="c" * 40,
            )
            reg.update_slice("slice-stale-review", state="building", candidate="c" * 40)
            reg.update_slice("slice-stale-review", state="reviewing", candidate="c" * 40)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = root / "handoff"

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads((hdir / "slice-stale-review.json").read_text(encoding="utf-8"))
            stored = reg.get_slice("slice-stale-review")
            self.assertEqual(manifest["gate_reason"], "stale-input")
            self.assertEqual(manifest["gate_verdict"]["reason"], "stale-input")
            self.assertEqual(stored["current_evaluation_refs"], [])
            self.assertEqual(len(stored["evaluation_history"]), 1)
            self.assertEqual(summary["completed"], [{"slice_id": "slice-stale-review", "gate_status": "needs_human"}])

    def test_reviewer_non_json_output_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            builder = _make_job(reg, "slice-review-output", worktree=str(root / "candidate"))
            reg.attach_launch_handle(builder["job_id"], executor="copilot", model_id="claude-haiku-4.5", session_name="slice-review-output", pid=1, log_path="/builder-log")
            reviewer_log = root / "review.log"
            reviewer_log.write_text("not-json\n", encoding="utf-8")
            reviewer = reg.create_job(
                task="slice-review-output",
                persona="reviewer",
                kind="review",
                branch="feature/slice-review-output",
                pane="",
                worktree=str(root / "review"),
                executor="codex",
                model_id="gpt-5.4",
                independence_domain="openai",
                session_name="slice-review-output-2",
                pid=2,
                log_path=str(reviewer_log),
                subject_head="b" * 40,
                spec_hash="new-spec",
                plan_hash="new-plan",
                verification_hash="new-verification",
            )
            reg.update_status(reviewer["job_id"], "exited")
            reg.create_slice(
                slice_id="slice-review-output",
                spec_path=str(root / "spec.md"),
                spec_hash="new-spec",
                plan_path=str(root / "plan.md"),
                plan_hash="new-plan",
                target_branch="main",
                target_remote="origin",
                verification_hash="new-verification",
                verification={"docs_class": "code", "review_policy": "required"},
                dispatch_base="a" * 40,
                builder_job_id=builder["job_id"],
                reviewer_job_id=reviewer["job_id"],
                candidate="b" * 40,
            )
            reg.update_slice("slice-review-output", state="building", candidate="b" * 40)
            reg.update_slice("slice-review-output", state="reviewing", candidate="b" * 40)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = root / "handoff"

            summary = manager.complete_tick(
                disp,
                handoff_dir=str(hdir),
                clock=lambda: "T0",
                git_runner=lambda args: _git_ok("b" * 40),
            )

            manifest = json.loads((hdir / "slice-review-output.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_verdict"]["reason"], "invalid-process-output")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-review-output", "gate_status": "needs_human"}])

    def test_foreign_review_launch_avoids_registry_private_mutation(self) -> None:
        source = inspect.getsource(manager._launch_foreign_review)
        self.assertNotIn("registry._find_job", source)
        self.assertNotIn("registry._persist", source)

    def test_successful_informational_verification_stays_verified_until_candidate_merged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-info", worktree=str(worktree))
            _create_slice(reg, root, job, docs_class="informational")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"
            candidate = "b" * 40
            target_head = "c" * 40
            dispatch_base = "a" * 40
            persona_catalog = _persona_catalog(builder_paths=["**"])

            def git_runner(args: list[str]):
                key = tuple(args)
                if key == ("-C", str(root), "rev-parse", "feature/slice-info"):
                    return _git_ok(candidate)
                if key == ("-C", str(worktree), "rev-parse", "HEAD"):
                    return _git_ok(candidate)
                if key == ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"):
                    return _git_ok("")
                return {
                    ("-C", str(root), "fetch", "--no-tags", "origin", "main"): _git_ok(""),
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/main"): _git_ok(target_head),
                    ("-C", str(root), "merge-base", "--is-ancestor", candidate, target_head): _proc_fail(1),
                    ("-C", str(root), "merge-base", "--is-ancestor", dispatch_base, candidate): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + ".." + candidate): _git_ok(""),
                    ("-C", str(root), "show", dispatch_base + ":paulsha_cortex/persona/personas.yaml"): _git_ok(persona_catalog),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + "..." + candidate): _git_ok(""),
                    ("-C", str(root), "worktree", "add", "--detach", str(root / ".psc-verification-worktrees" / "slice-info-aaaaaaaaaaaa"), dispatch_base): _git_ok(""),
                    ("-C", str(root), "worktree", "remove", "--force", str(root / ".psc-verification-worktrees" / "slice-info-aaaaaaaaaaaa")): _git_ok(""),
                }[key]

            summary = manager.complete_tick(
                disp,
                handoff_dir=str(hdir),
                metas=[
                    {"slice_id": "slice-info", "dispatch": "auto", "plan": "p-info.md", "depends_on": []},
                    {"slice_id": "downstream", "dispatch": "auto", "plan": "p-down.md", "depends_on": ["slice-info"]},
                ],
                clock=lambda: "T0",
                git_runner=git_runner,
                subprocess_runner=lambda *args, **kwargs: _proc_ok(),
            )

            manifest = json.loads((hdir / "slice-info.json").read_text(encoding="utf-8"))
            slice_row = reg.get_slice("slice-info")
            self.assertEqual(manifest["gate_status"], "verified")
            self.assertEqual(manifest["gate_reason"], "candidate-not-merged")
            self.assertIsNone(manifest["completion_record_path"])
            self.assertEqual(slice_row["state"], "verified")
            self.assertEqual(slice_row["gate_state"], "passed")
            self.assertFalse(
                manager.autonomy.default_is_satisfied(
                    "slice-info",
                    handoff_dir=str(hdir),
                    repo_root=root,
                    git_runner=git_runner,
                )
            )
            self.assertEqual(summary["released"], [])

    def test_successful_informational_verification_records_completion_after_merge(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-info", worktree=str(worktree))
            _create_slice(reg, root, job, docs_class="informational")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"
            candidate = "b" * 40
            target_head = "c" * 40
            dispatch_base = "a" * 40
            persona_catalog = _persona_catalog(builder_paths=["**"])

            def git_runner(args: list[str]):
                key = tuple(args)
                if key == ("-C", str(root), "rev-parse", "feature/slice-info"):
                    return _git_ok(candidate)
                if key == ("-C", str(worktree), "rev-parse", "HEAD"):
                    return _git_ok(candidate)
                if key == ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"):
                    return _git_ok("")
                return {
                    ("-C", str(root), "fetch", "--no-tags", "origin", "main"): _git_ok(""),
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/main"): _git_ok(target_head),
                    ("-C", str(root), "merge-base", "--is-ancestor", candidate, target_head): _git_ok(""),
                    ("-C", str(root), "merge-base", "--is-ancestor", dispatch_base, candidate): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + ".." + candidate): _git_ok(""),
                    ("-C", str(root), "show", dispatch_base + ":paulsha_cortex/persona/personas.yaml"): _git_ok(persona_catalog),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + "..." + candidate): _git_ok(""),
                    ("-C", str(root), "worktree", "add", "--detach", str(root / ".psc-verification-worktrees" / "slice-info-aaaaaaaaaaaa"), dispatch_base): _git_ok(""),
                    ("-C", str(root), "worktree", "remove", "--force", str(root / ".psc-verification-worktrees" / "slice-info-aaaaaaaaaaaa")): _git_ok(""),
                }[key]

            summary = manager.complete_tick(
                disp,
                handoff_dir=str(hdir),
                metas=[
                    {"slice_id": "slice-info", "dispatch": "auto", "plan": "p-info.md", "depends_on": []},
                    {"slice_id": "downstream", "dispatch": "auto", "plan": "p-down.md", "depends_on": ["slice-info"]},
                ],
                clock=lambda: "T0",
                git_runner=git_runner,
                subprocess_runner=lambda *args, **kwargs: _proc_ok(),
            )

            manifest = json.loads((hdir / "slice-info.json").read_text(encoding="utf-8"))
            slice_row = reg.get_slice("slice-info")
            self.assertEqual(manifest["gate_status"], "passed")
            self.assertEqual(manifest["gate_reason"], "candidate-merged")
            self.assertEqual(slice_row["state"], "completed")
            self.assertEqual(slice_row["gate_state"], "passed")
            self.assertIsNotNone(manifest["completion_record_path"])
            self.assertIsNotNone(manifest["completion_record_hash"])
            self.assertTrue(
                manager.autonomy.default_is_satisfied(
                    "slice-info",
                    handoff_dir=str(hdir),
                    repo_root=root,
                    git_runner=git_runner,
                )
            )
            self.assertEqual(summary["released"], ["downstream"])

    def test_candidate_equal_to_dispatch_base_marks_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            candidate = "a" * 40
            job = _make_job(reg, "slice-h", worktree=str(worktree))
            _create_slice(reg, root, job, docs_class="code", dispatch_base=candidate)
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"

            summary = manager.complete_tick(
                disp,
                handoff_dir=str(hdir),
                clock=lambda: "T0",
                git_runner=lambda args: {
                    ("-C", str(root), "rev-parse", "feature/slice-h"): _git_ok(candidate),
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok(candidate),
                    ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): _git_ok(""),
                }[tuple(args)],
                subprocess_runner=lambda *args, **kwargs: _proc_ok(),
            )

            manifest = json.loads((hdir / "slice-h.json").read_text(encoding="utf-8"))
            slice_row = reg.get_slice("slice-h")
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "candidate-not-advanced")
            self.assertEqual(slice_row["state"], "needs_human")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-h", "gate_status": "needs_human"}])

    def test_force_pushed_non_descendant_candidate_marks_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            dispatch_base = "a" * 40
            candidate = "b" * 40
            job = _make_job(reg, "slice-i", worktree=str(worktree))
            _create_slice(reg, root, job, docs_class="code", dispatch_base=dispatch_base)
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"

            summary = manager.complete_tick(
                disp,
                handoff_dir=str(hdir),
                clock=lambda: "T0",
                git_runner=lambda args: {
                    ("-C", str(root), "rev-parse", "feature/slice-i"): _git_ok(candidate),
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok(candidate),
                    ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): _git_ok(""),
                    ("-C", str(root), "merge-base", "--is-ancestor", dispatch_base, candidate): SimpleNamespace(returncode=1, stdout="", stderr=""),
                }[tuple(args)],
                subprocess_runner=lambda *args, **kwargs: _proc_ok(),
            )

            manifest = json.loads((hdir / "slice-i.json").read_text(encoding="utf-8"))
            slice_row = reg.get_slice("slice-i")
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "candidate-not-descendant")
            self.assertEqual(slice_row["state"], "needs_human")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-i", "gate_status": "needs_human"}])

    def test_branch_ref_divergence_after_snapshot_marks_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            dispatch_base = "a" * 40
            candidate = "b" * 40
            moved = "c" * 40
            job = _make_job(reg, "slice-j", worktree=str(worktree))
            _create_slice(reg, root, job, docs_class="informational", dispatch_base=dispatch_base)
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"
            persona_catalog = _persona_catalog(builder_paths=["**"])
            branch_responses = [_git_ok(candidate), _git_ok(moved)]

            def git_runner(args: list[str]):
                key = tuple(args)
                if key == ("-C", str(root), "rev-parse", "feature/slice-j"):
                    return branch_responses.pop(0)
                return {
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok(candidate),
                    ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): _git_ok(""),
                    ("-C", str(root), "merge-base", "--is-ancestor", dispatch_base, candidate): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + ".." + candidate): _git_ok(""),
                    ("-C", str(root), "show", dispatch_base + ":paulsha_cortex/persona/personas.yaml"): _git_ok(persona_catalog),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + "..." + candidate): _git_ok(""),
                    ("-C", str(root), "worktree", "add", "--detach", str(root / ".psc-verification-worktrees" / "slice-j-aaaaaaaaaaaa"), dispatch_base): _git_ok(""),
                    ("-C", str(root), "worktree", "remove", "--force", str(root / ".psc-verification-worktrees" / "slice-j-aaaaaaaaaaaa")): _git_ok(""),
                }[key]

            summary = manager.complete_tick(
                disp,
                handoff_dir=str(hdir),
                clock=lambda: "T0",
                git_runner=git_runner,
                subprocess_runner=lambda *args, **kwargs: _proc_ok(),
            )

            manifest = json.loads((hdir / "slice-j.json").read_text(encoding="utf-8"))
            slice_row = reg.get_slice("slice-j")
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "candidate-ref-diverged")
            self.assertEqual(slice_row["state"], "needs_human")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-j", "gate_status": "needs_human"}])

    def test_manifest_hides_evidence_when_slice_update_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-k", worktree=str(worktree))
            reg.attach_launch_handle(job["job_id"], executor="copilot", session_name="slice-k", pid=1, log_path="/log")
            reg._find_job(job["job_id"])["model_id"] = "claude-haiku-4.5"
            _create_slice(reg, root, job, docs_class="code")
            original_record_action = reg.record_action
            config_root = root / "config"
            config_root.mkdir(parents=True, exist_ok=True)
            (config_root / "model-identities.yaml").write_text(
                (
                    "schema_version: 1\n"
                    "identities:\n"
                    "  - executor: copilot\n"
                    "    model_id: claude-haiku-4.5\n"
                    "    independence_domain: anthropic\n"
                    "  - executor: codex\n"
                    "    model_id: gpt-5.4\n"
                    "    independence_domain: openai\n"
                ),
                encoding="utf-8",
            )

            def failing_record_action(*args, **kwargs):
                raise RuntimeError("persist failed")

            reg.record_action = failing_record_action  # type: ignore[assignment]
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"
            candidate = "b" * 40
            dispatch_base = "a" * 40
            persona_catalog = _persona_catalog(builder_paths=["**"])

            def git_runner(args: list[str]):
                return {
                    ("-C", str(root), "rev-parse", "feature/slice-k"): _git_ok(candidate),
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok(candidate),
                    ("-C", str(worktree), "status", "--porcelain", "--untracked-files=all"): _git_ok(""),
                    ("-C", str(root), "merge-base", "--is-ancestor", dispatch_base, candidate): _git_ok(""),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + ".." + candidate): _git_ok(""),
                    ("-C", str(root), "show", dispatch_base + ":paulsha_cortex/persona/personas.yaml"): _git_ok(persona_catalog),
                    ("-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", dispatch_base + "..." + candidate): _git_ok(""),
                    ("-C", str(root), "worktree", "add", "--detach", str(root / ".psc-verification-worktrees" / "slice-k-aaaaaaaaaaaa"), dispatch_base): _git_ok(""),
                    ("-C", str(root), "worktree", "remove", "--force", str(root / ".psc-verification-worktrees" / "slice-k-aaaaaaaaaaaa")): _git_ok(""),
                }[tuple(args)]

            try:
                summary = manager.complete_tick(
                    disp,
                    handoff_dir=str(hdir),
                    clock=lambda: "T0",
                    git_runner=git_runner,
                    subprocess_runner=lambda *args, **kwargs: _proc_ok(),
                )
            finally:
                reg.record_action = original_record_action  # type: ignore[assignment]

            manifest = json.loads((hdir / "slice-k.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "verification-state-update-error")
            self.assertIsNone(manifest["gate_verdict"])
            self.assertIsNone(manifest["verification_evidence_path"])
            self.assertIsNone(manifest["verification_evidence_hash"])
            self.assertEqual(summary["completed"], [{"slice_id": "slice-k", "gate_status": "needs_human"}])
            self.assertEqual(reg.get_slice("slice-k")["state"], "building")

            with mock.patch.dict("os.environ", {"PSC_PROJECT_CONFIG_ROOT": str(config_root)}, clear=False):
                summary = manager.complete_tick(
                    disp,
                    handoff_dir=str(hdir),
                    clock=lambda: "T1",
                    git_runner=git_runner,
                    subprocess_runner=lambda *args, **kwargs: _proc_ok(),
                )

            manifest = json.loads((hdir / "slice-k.json").read_text(encoding="utf-8"))
            slice_row = reg.get_slice("slice-k")
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_verdict"]["state"], "absent")
            self.assertIsNotNone(manifest["verification_evidence_path"])
            self.assertEqual(slice_row["state"], "needs_human")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-k", "gate_status": "needs_human"}])

    def test_retryable_runner_error_does_not_poison_later_success(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-l", worktree=str(worktree))
            reg.attach_launch_handle(job["job_id"], executor="copilot", session_name="slice-l", pid=1, log_path="/log")
            reg._find_job(job["job_id"])["model_id"] = "claude-haiku-4.5"
            _create_slice(reg, root, job, docs_class="code")
            original_record_action = reg.record_action
            candidate = "b" * 40
            config_root = root / "config"
            config_root.mkdir(parents=True, exist_ok=True)
            (config_root / "model-identities.yaml").write_text(
                (
                    "schema_version: 1\n"
                    "identities:\n"
                    "  - executor: copilot\n"
                    "    model_id: claude-haiku-4.5\n"
                    "    independence_domain: anthropic\n"
                ),
                encoding="utf-8",
            )

            def git_runner(args: list[str]):
                return {
                    ("-C", str(root), "rev-parse", "feature/slice-l"): _git_ok(candidate),
                    ("-C", str(worktree), "rev-parse", "HEAD"): _git_ok(candidate),
                }[tuple(args)]

            def failing_record_action(*args, **kwargs):
                raise RuntimeError("persist failed")

            reg.record_action = failing_record_action  # type: ignore[assignment]
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = root / "handoff"

            try:
                manager.complete_tick(
                    disp,
                    handoff_dir=str(hdir),
                    clock=lambda: "T0",
                    git_runner=git_runner,
                    verification_runner=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
                )
            finally:
                reg.record_action = original_record_action  # type: ignore[assignment]

            def success_runner(**kwargs):
                return manager.verification.write_verification_evidence(
                    {
                        "schema_version": 1,
                        "slice_id": "slice-l",
                        "candidate": candidate,
                        "status": "reviewing",
                        "summary": "verification-succeeded",
                        "details": {"ok": True},
                    },
                    coordinator_root=root,
                )

            with mock.patch.dict("os.environ", {"PSC_PROJECT_CONFIG_ROOT": str(config_root)}, clear=False):
                summary = manager.complete_tick(
                    disp,
                    handoff_dir=str(hdir),
                    clock=lambda: "T1",
                    git_runner=git_runner,
                    verification_runner=success_runner,
                )

            manifest = json.loads((hdir / "slice-l.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_verdict"]["state"], "absent")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-l", "gate_status": "needs_human"}])

    def test_pinned_spec_hash_mismatch_marks_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            spec_path = Path(d) / "specs" / "slice-a.md"
            plan_path = Path(d) / "docs" / "superpowers" / "plans" / "slice-a.md"
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(
                "---\n"
                "dispatch: auto\n"
                "slice_id: slice-a\n"
                "plan: docs/superpowers/plans/slice-a.md\n"
                "target_branch: main\n"
                "verification:\n"
                "  docs_class: code\n"
                "  required_artifacts: []\n"
                "  checks:\n"
                "    - kind: persona-scope\n"
                "    - kind: command\n"
                "      name: policy\n"
                "      argv: [python3, -m, pytest, -q]\n"
                "      cwd: .\n"
                "      timeout_seconds: 30\n"
                "  tests: []\n"
                "  full_suite:\n"
                "    argv: [python3, -m, pytest, -q]\n"
                "    cwd: .\n"
                "    timeout_seconds: 60\n"
                "    baseline: no-regression\n"
                "---\n",
                encoding="utf-8",
            )
            plan_path.write_text("# plan\n", encoding="utf-8")
            job = _make_job(reg, "slice-a")
            reg.create_slice(
                slice_id="slice-a",
                spec_path=str(spec_path),
                spec_hash="old-spec-hash",
                plan_path=str(plan_path),
                plan_hash="plan-hash",
                target_branch="main",
                target_remote="origin",
                verification_hash="verification-hash",
                dispatch_base="base-sha",
                builder_job_id=job["job_id"],
                reviewer_job_id=None,
                candidate=None,
            )
            reg.update_slice("slice-a", state="building", builder_job_id=job["job_id"])
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads((hdir / "slice-a.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "pinned-input-mismatch")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-a", "gate_status": "needs_human"}])

    def test_failed_job_with_pinned_spec_hash_mismatch_marks_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            spec_path = Path(d) / "specs" / "slice-a.md"
            plan_path = Path(d) / "docs" / "superpowers" / "plans" / "slice-a.md"
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(
                "---\n"
                "dispatch: auto\n"
                "slice_id: slice-a\n"
                "plan: docs/superpowers/plans/slice-a.md\n"
                "target_branch: main\n"
                "verification:\n"
                "  docs_class: code\n"
                "  required_artifacts: []\n"
                "  checks:\n"
                "    - kind: persona-scope\n"
                "    - kind: command\n"
                "      name: policy\n"
                "      argv: [python3, -m, pytest, -q]\n"
                "      cwd: .\n"
                "      timeout_seconds: 30\n"
                "  tests: []\n"
                "  full_suite:\n"
                "    argv: [python3, -m, pytest, -q]\n"
                "    cwd: .\n"
                "    timeout_seconds: 60\n"
                "    baseline: no-regression\n"
                "---\n",
                encoding="utf-8",
            )
            plan_path.write_text("# plan\n", encoding="utf-8")
            job = _make_job(reg, "slice-a")
            reg.create_slice(
                slice_id="slice-a",
                spec_path=str(spec_path),
                spec_hash="old-spec-hash",
                plan_path=str(plan_path),
                plan_hash="plan-hash",
                target_branch="main",
                target_remote="origin",
                verification_hash="verification-hash",
                dispatch_base="base-sha",
                builder_job_id=job["job_id"],
                reviewer_job_id=None,
                candidate=None,
            )
            reg.update_slice("slice-a", state="building", builder_job_id=job["job_id"])
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "failed"})
            hdir = Path(d) / "handoff"

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads((hdir / "slice-a.json").read_text(encoding="utf-8"))
            slice_row = reg.get_slice("slice-a")
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "pinned-input-mismatch")
            self.assertEqual(manifest["completion"], "failed")
            self.assertEqual(slice_row["state"], "needs_human")
            self.assertEqual(slice_row["gate_state"], "needs_human")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-a", "gate_status": "needs_human"}])

    def test_spec_reparse_failure_still_marks_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            spec_path = Path(d) / "specs" / "slice-a.md"
            plan_path = Path(d) / "docs" / "superpowers" / "plans" / "slice-a.md"
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            spec_bytes = b"\x80not-utf8"
            spec_path.write_bytes(spec_bytes)
            plan_bytes = b"# plan\n"
            plan_path.write_bytes(plan_bytes)
            job = _make_job(reg, "slice-a")
            reg.create_slice(
                slice_id="slice-a",
                spec_path=str(spec_path),
                spec_hash=manager.verification.sha256_bytes(spec_bytes),
                plan_path=str(plan_path),
                plan_hash=manager.verification.sha256_bytes(plan_bytes),
                target_branch="main",
                target_remote="origin",
                verification_hash=manager.verification.canonical_json_hash(None),
                dispatch_base="base-sha",
                builder_job_id=job["job_id"],
                reviewer_job_id=None,
                candidate=None,
            )
            reg.update_slice("slice-a", state="building", builder_job_id=job["job_id"])
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads((hdir / "slice-a.json").read_text(encoding="utf-8"))
            slice_row = reg.get_slice("slice-a")
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "pinned-input-mismatch")
            self.assertEqual(slice_row["state"], "needs_human")
            self.assertEqual(slice_row["gate_state"], "needs_human")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-a", "gate_status": "needs_human"}])
            self.assertEqual(summary["errors"], [])


class CompleteTickErrorAndReleaseTests(unittest.TestCase):
    def test_per_job_poll_error_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            a = _make_job(reg, "slice-h")
            b = _make_job(reg, "slice-i")
            disp = FakeDispatcher(reg, poll_map={b["job_id"]: "exited"}, raise_on={a["job_id"]})
            hdir = Path(d) / "handoff"
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            self.assertTrue((hdir / "slice-i.json").exists())
            self.assertFalse((hdir / "slice-h.json").exists())
            self.assertEqual(summary["completed"], [{"slice_id": "slice-i", "gate_status": "needs_human"}])
            self.assertEqual([e["job_id"] for e in summary["errors"]], [a["job_id"]])

    def test_downstream_not_released_by_exited_job_alone(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            up = _make_job(reg, "up")
            disp = FakeDispatcher(reg, poll_map={up["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            metas = [
                {"slice_id": "up", "dispatch": "auto", "plan": "p-up.md", "depends_on": []},
                {"slice_id": "down", "dispatch": "auto", "plan": "p-down.md", "depends_on": ["up"]},
            ]
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), metas=metas, clock=lambda: "T0")
            self.assertEqual(summary["released"], [])
            from paulsha_cortex.coordinator import autonomy
            self.assertFalse(autonomy.default_is_satisfied("up", handoff_dir=str(hdir)))

    def test_invalid_utf8_manifest_repair_still_keeps_downstream_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            up = _make_job(reg, "up")
            disp = FakeDispatcher(reg, poll_map={up["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "up.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(b"\x80not-utf8")
            metas = [
                {"slice_id": "down", "dispatch": "auto", "plan": "p-down.md", "depends_on": ["up"]},
            ]

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), metas=metas, clock=lambda: "T0")

            self.assertEqual(summary["completed"], [{"slice_id": "up", "gate_status": "needs_human"}])
            self.assertEqual(summary["released"], [])
            self.assertEqual(summary["errors"], [])


class CompleteTickGuardTests(unittest.TestCase):
    def test_job_without_valid_slice_id_goes_to_errors(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-x")
            # 直接污染 registry 內部 job 的 task 成 None，模擬 corrupt 狀態
            reg._jobs[0]["task"] = None
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            self.assertFalse((hdir / "None.json").exists())
            self.assertEqual(summary["completed"], [])
            self.assertEqual([e["job_id"] for e in summary["errors"]], [job["job_id"]])

    def test_cyclic_metas_does_not_crash_tick(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "a")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            cyclic = [
                {"slice_id": "a", "dispatch": "auto", "plan": "pa.md", "depends_on": ["b"]},
                {"slice_id": "b", "dispatch": "auto", "plan": "pb.md", "depends_on": ["a"]},
            ]
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), metas=cyclic, clock=lambda: "T0")
            # 完成側仍寫出 manifest；released 因環被停用而省略
            self.assertTrue((hdir / "a.json").exists())
            self.assertEqual(summary["completed"], [{"slice_id": "a", "gate_status": "needs_human"}])
            self.assertNotIn("released", summary)

    def test_unsafe_slice_id_rejected_no_escape_write(self) -> None:
        for bad in ["../evil", "/abs/evil", "a/b", "..", ".", "x/../y", "with space"]:
            with tempfile.TemporaryDirectory() as d:
                reg = _reg(d)
                job = _make_job(reg, "ok")
                reg._jobs[0]["task"] = bad   # 模擬不安全/corrupt slice_id
                disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
                hdir = Path(d) / "handoff"
                summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
                self.assertEqual(summary["completed"], [], f"{bad!r} 應被拒")
                self.assertEqual([e["job_id"] for e in summary["errors"]], [job["job_id"]])
                # 確認沒有任何檔案被寫到 hdir 外或 hdir 內
                self.assertFalse(hdir.exists() and any(hdir.iterdir()), f"{bad!r} 不應寫出檔案")


class RunTickTests(unittest.TestCase):
    def test_not_idle_skips_fanout_but_still_completes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "x")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(
                disp, metas=[], require_idle=True, max_load=1.0,
                idle_probe=lambda: (99.0, 99.0, 99.0), handoff_dir=str(hdir), clock=lambda: "T0",
            )
            # fanout 被 idle gate 擋，但完成側仍跑（review F-C）
            self.assertEqual(summary["dispatch_skipped"], "not-idle")
            self.assertEqual(summary["dispatched"], [])
            self.assertEqual(summary["completed"], [{"slice_id": "x", "gate_status": "needs_human"}])
            self.assertTrue((hdir / "x.json").exists())

    def test_runs_fanout_and_complete_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "y")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(
                disp, metas=[], require_idle=True, max_load=1.0,
                idle_probe=lambda: (0.0, 0.0, 0.0), handoff_dir=str(hdir), clock=lambda: "T0",
            )
            self.assertFalse(summary["dispatch_skipped"])
            self.assertEqual(summary["completed"], [{"slice_id": "y", "gate_status": "needs_human"}])
            self.assertTrue((hdir / "y.json").exists())

    def test_fanout_failure_does_not_block_complete(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "done-slice")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            metas = [{"slice_id": "ready-one", "dispatch": "auto", "plan": "p.md", "depends_on": []}]
            summary = manager.run_tick(
                disp, metas=metas, launcher=None, is_satisfied=lambda s: True,
                handoff_dir=str(hdir), clock=lambda: "T0",
            )
            self.assertFalse(summary["dispatch_skipped"])
            self.assertTrue(any(e.get("stage") == "fanout" for e in summary["errors"]))
            self.assertEqual(summary["completed"], [{"slice_id": "done-slice", "gate_status": "needs_human"}])

    def test_invalid_utf8_dependency_manifest_does_not_create_fanout_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "up.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(b"\x80not-utf8")
            metas = [
                {"slice_id": "down", "dispatch": "auto", "plan": "p-down.md", "depends_on": ["up"]},
            ]

            summary = manager.run_tick(
                disp, metas=metas, launcher=None, handoff_dir=str(hdir), clock=lambda: "T0",
            )

            self.assertEqual(summary["dispatched"], [])
            self.assertFalse(any(e.get("stage") == "fanout" for e in summary["errors"]))


    def test_in_flight_slice_not_redispatched(self) -> None:
        # slice 已有 dispatched job → 本趟 fanout 不得再對它派工（review F-A 冪等）
        class _RecordingLauncher:
            def __init__(self) -> None:
                self.launched: list[str] = []

            def launch(self, *, slice_id, prompt, worktree, log_dir):  # pragma: no cover
                self.launched.append(slice_id)
                raise AssertionError(f"in-flight slice 不應被重派: {slice_id}")

        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            _make_job(reg, "s")  # status=dispatched（in-flight）
            disp = FakeDispatcher(reg, poll_map={})  # s 維持 dispatched
            hdir = Path(d) / "handoff"
            launcher = _RecordingLauncher()
            metas = [{"slice_id": "s", "dispatch": "auto", "plan": "p.md", "depends_on": []}]
            summary = manager.run_tick(
                disp, metas=metas, launcher=launcher, is_satisfied=lambda x: True,
                handoff_dir=str(hdir), clock=lambda: "T0",
            )
            self.assertEqual(launcher.launched, [])
            self.assertEqual(summary["dispatched"], [])
            self.assertFalse(summary["dispatch_skipped"])

    def test_reaper_result_recorded_in_summary(self) -> None:
        # 收尾 janitor（#161）：傳入 reaper → complete 後呼叫一次，結果進 summary["reaped"]
        calls = []
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "z")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(
                disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0",
                reaper=lambda: calls.append(1) or {"ran": True, "applied": True, "returncode": 0},
            )
            self.assertEqual(calls, [1])
            self.assertEqual(summary["reaped"], {"ran": True, "applied": True, "returncode": 0})
            self.assertEqual(summary["completed"], [{"slice_id": "z", "gate_status": "needs_human"}])

    def test_reaper_exception_does_not_break_tick(self) -> None:
        # janitor 失敗一律不破壞 tick：reaped=None、errors 收 stage=reap、完成側照常
        def _boom():
            raise RuntimeError("reap 爆炸")

        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "w")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(
                disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0", reaper=_boom,
            )
            self.assertIsNone(summary["reaped"])
            self.assertTrue(any(e.get("stage") == "reap" for e in summary["errors"]))
            self.assertEqual(summary["completed"], [{"slice_id": "w", "gate_status": "needs_human"}])

    def test_no_reaper_disables_janitor(self) -> None:
        # 預設不傳 reaper → reaped=None，且不產生 reap 相關 error（單測不誤觸真實回收）
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0")
            self.assertIsNone(summary["reaped"])
            self.assertFalse(any(e.get("stage") == "reap" for e in summary["errors"]))


class _HeadlessDispatcher:
    """有 _registry 的 fake：供 dispatch_ready 記 job + complete_tick poll。"""

    def __init__(self, registry: JobRegistry) -> None:
        self._registry = registry

    def poll_headless_done(self, job_id: str) -> dict:
        return self._registry.update_headless_result(job_id, status="exited", exit_code=0)


class _RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        from paulsha_cortex.coordinator.launcher import LaunchHandle

        self.calls.append({"slice_id": slice_id, "worktree": worktree})
        return LaunchHandle(
            executor="copilot", model_id=None, session_name=slice_id, pid=100 + len(self.calls),
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


class DispatchHeadBaselineTests(unittest.TestCase):
    """dispatch baseline 應持久化，且 builder exited 不能直接成為 DAG satisfaction。"""

    def test_dispatch_ready_persists_dispatch_head(self) -> None:
        # 注入 git_runner 回固定 baseline → 應寫進 job 與 registry（修前為 None）
        def git_runner(args):
            if args and args[0] == "rev-parse":
                return "f" * 40
            if len(args) >= 5 and args[0] == "-C" and args[2] == "fetch":
                return ""
            if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
                return "e" * 40
            if len(args) >= 6 and args[0] == "-C" and args[2] == "merge-base":
                return ""
            return ""

        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = _HeadlessDispatcher(reg)
            launcher = _RecordingLauncher()
            metas = [_dispatch_meta("slice-x")]
            jobs = dispatch_ready(
                metas, is_satisfied=lambda _id: True, dispatcher=disp,
                launcher=launcher, git_runner=git_runner,
            )
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["dispatch_head"], "f" * 40)
            self.assertEqual(reg.list_jobs()[0]["dispatch_head"], "f" * 40)

    def test_dispatch_ready_git_failure_records_none_not_crash(self) -> None:
        # baseline 取不到（git 例外）→ dispatch_head=None，但不破壞派工（graceful）
        def boom(args):
            if args and args[0] == "rev-parse":
                raise RuntimeError("git 爆炸")
            if len(args) >= 5 and args[0] == "-C" and args[2] == "fetch":
                return ""
            if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
                return "e" * 40
            if len(args) >= 6 and args[0] == "-C" and args[2] == "merge-base":
                return ""
            return ""

        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = _HeadlessDispatcher(reg)
            launcher = _RecordingLauncher()
            metas = [_dispatch_meta("slice-y")]
            jobs = dispatch_ready(
                metas, is_satisfied=lambda _id: True, dispatcher=disp,
                launcher=launcher, git_runner=boom,
            )
            self.assertEqual(len(jobs), 1)
            self.assertIsNone(jobs[0]["dispatch_head"])

    def test_dispatch_pipeline_keeps_exited_job_out_of_completion_path(self) -> None:
        def git_runner(args):
            if args and args[0] == "rev-parse":
                return "f" * 40
            if len(args) >= 5 and args[0] == "-C" and args[2] == "fetch":
                return ""
            if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
                return "e" * 40
            if len(args) >= 6 and args[0] == "-C" and args[2] == "merge-base":
                return ""
            return ""

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            reg = JobRegistry(state_path=root / "jobs.json")
            disp = _HeadlessDispatcher(reg)
            launcher = _RecordingLauncher()
            metas = [_dispatch_meta("slice-x")]
            jobs = dispatch_ready(
                metas, is_satisfied=lambda _id: True, dispatcher=disp,
                launcher=launcher, git_runner=git_runner,
            )
            self.assertTrue(jobs[0]["dispatch_head"], "baseline 應非 null")
            hdir = root / "handoff"
            manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            manifest = json.loads((hdir / "slice-x.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertFalse(manager.autonomy.default_is_satisfied("slice-x", handoff_dir=str(hdir)))


if __name__ == "__main__":
    unittest.main()
