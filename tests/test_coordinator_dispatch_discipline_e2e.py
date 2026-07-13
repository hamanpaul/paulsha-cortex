from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from paulsha_cortex.coordinator import autonomy, completion, manager, review, verification
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.registry import JobRegistry


SCRIPT = Path(__file__).resolve().parents[1] / "paulsha_cortex/scripts/reap-codex-brokers.sh"


def _git_ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _git_nonzero(returncode: int = 1) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="", stderr="")


def _reg(tmp: str) -> JobRegistry:
    return JobRegistry(state_path=Path(tmp) / "jobs.json")


def _make_job(reg: JobRegistry, slice_id: str, *, worktree: str | None = None, branch: str | None = None) -> dict:
    return reg.create_job(
        task=slice_id,
        persona="builder",
        branch=branch or f"feature/{slice_id}",
        pane="",
        worktree=worktree or f"/wt/{slice_id}",
        executor="copilot",
        session_name=slice_id,
        pid=4242,
        log_path=f"/logs/{slice_id}.jsonl",
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


class FakeDispatcher:
    def __init__(self, registry: JobRegistry, poll_map: dict | None = None, raise_on: set | None = None) -> None:
        self._registry = registry
        self._poll_map = poll_map or {}
        self._raise_on = raise_on or set()

    def poll_headless_done(self, job_id: str) -> dict:
        if job_id in self._raise_on:
            raise RuntimeError(f"poll exploded: {job_id}")
        status = self._poll_map.get(job_id)
        if status is None:
            return self._registry.get_job(job_id)
        return self._registry.update_headless_result(
            job_id,
            status=status,
            exit_code=0 if status == "exited" else 1,
        )


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
    def send(self, pane_id, text):  # pragma: no cover
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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _cmdline(*argv: str) -> bytes:
    return ("\0".join(argv) + "\0").encode("utf-8")


def _stat_text(pid: int, *, ppid: int, starttime: int) -> str:
    fields = [str(pid), "(node)", "S", str(ppid), *(["0"] * 17), str(starttime), "0", "0", "0"]
    return " ".join(fields)


def _write_proc_pid(
    proc_root: Path,
    pid: int,
    *,
    ppid: int,
    starttime: int,
    cwd: Path | None,
    cmdline: bytes,
) -> None:
    pid_dir = proc_root / str(pid)
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "stat").write_text(_stat_text(pid, ppid=ppid, starttime=starttime), encoding="utf-8")
    (pid_dir / "cmdline").write_bytes(cmdline)
    if cwd is not None:
        cwd.mkdir(parents=True, exist_ok=True)
        os.symlink(cwd, pid_dir / "cwd", target_is_directory=True)


def _write_snapshot(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_killer(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$KILL_LOG\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_reaper_script(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "REAP_PS_SNAPSHOT": str(tmp_path / "snapshot.txt"),
        "REAP_PROC_ROOT": str(tmp_path / "proc"),
        "REAP_KILL_CMD": str(tmp_path / "fake-kill.sh"),
        "KILL_LOG": str(tmp_path / "kill.log"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


class DispatchDisciplineCanaryTests(unittest.TestCase):
    def test_exit_success_with_missing_artifact_stays_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-missing-artifact", worktree=str(worktree))
            _create_slice(reg, root, job, docs_class="code")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            handoff = root / "handoff"
            candidate = "b" * 40

            summary = manager.complete_tick(
                disp,
                handoff_dir=str(handoff),
                clock=lambda: "T0",
                verification_runner=lambda **kwargs: manager.verification.write_verification_evidence(
                    {
                        "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
                        "slice_id": "slice-missing-artifact",
                        "candidate": candidate,
                        "status": "needs_human",
                        "summary": "required-artifact-missing",
                        "details": {"missing": ["reports/policy.json"]},
                    },
                    coordinator_root=root,
                ),
            )

            manifest = json.loads((handoff / "slice-missing-artifact.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "required-artifact-missing")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-missing-artifact", "gate_status": "needs_human"}])

    def test_verification_passed_with_same_domain_reviewer_becomes_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            worktree = root / "candidate"
            worktree.mkdir()
            job = _make_job(reg, "slice-review-absent", worktree=str(worktree))
            reg.attach_launch_handle(
                job["job_id"],
                executor="copilot",
                session_name="slice-review-absent",
                pid=1,
                log_path="/log",
            )
            reg._find_job(job["job_id"])["model_id"] = "claude-haiku-4.5"
            _create_slice(reg, root, job, docs_class="code")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            handoff = root / "handoff"
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
                    "  - executor: claude\n"
                    "    model_id: claude-sonnet-4.5\n"
                    "    independence_domain: anthropic\n"
                ),
                encoding="utf-8",
            )

            with mock.patch.dict("os.environ", {"PSC_PROJECT_CONFIG_ROOT": str(config_root)}, clear=False):
                summary = manager.complete_tick(
                    disp,
                    handoff_dir=str(handoff),
                    clock=lambda: "T0",
                    review_executor="claude",
                    review_model="claude-sonnet-4.5",
                    verification_runner=lambda **kwargs: manager.verification.write_verification_evidence(
                        {
                            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
                            "slice_id": "slice-review-absent",
                            "candidate": candidate,
                            "status": "reviewing",
                            "summary": "verification-succeeded",
                            "details": {"ok": True},
                        },
                        coordinator_root=root,
                    ),
                )

            manifest = json.loads((handoff / "slice-review-absent.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_verdict"]["state"], "absent")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-review-absent", "gate_status": "needs_human"}])

    def test_stale_reviewer_head_keeps_audit_history_without_verifying(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            root = Path(d)
            builder = _make_job(reg, "slice-stale-review", worktree=str(root / "candidate"))
            reg.attach_launch_handle(
                builder["job_id"],
                executor="copilot",
                model_id="claude-haiku-4.5",
                session_name="slice-stale-review",
                pid=1,
                log_path="/log",
            )
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
            reg.create_slice(
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
            handoff = root / "handoff"

            summary = manager.complete_tick(disp, handoff_dir=str(handoff), clock=lambda: "T0")

            manifest = json.loads((handoff / "slice-stale-review.json").read_text(encoding="utf-8"))
            stored = reg.get_slice("slice-stale-review")
            self.assertEqual(manifest["gate_status"], "needs_human")
            self.assertEqual(manifest["gate_reason"], "stale-input")
            self.assertEqual(stored["current_evaluation_refs"], [])
            self.assertEqual(len(stored["evaluation_history"]), 1)
            self.assertEqual(summary["completed"], [{"slice_id": "slice-stale-review", "gate_status": "needs_human"}])

    def test_reviewed_candidate_blocks_until_preserving_merge_reaches_target(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            bare = tmp / "remote.git"
            repo = tmp / "repo"
            coordinator_root = tmp / "coordinator"
            handoff = tmp / "handoff"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
            _git(repo, "config", "user.name", "canary")
            _git(repo, "config", "user.email", "canary@example.com")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "base")
            _git(repo, "branch", "-M", "main")
            _git(repo, "remote", "add", "origin", str(bare))
            _git(repo, "push", "-u", "origin", "main")
            base_sha = _git(repo, "rev-parse", "HEAD")

            _git(repo, "checkout", "-b", "feature/slice-remote")
            (repo / "README.md").write_text("candidate\n", encoding="utf-8")
            _git(repo, "commit", "-am", "candidate")
            candidate = _git(repo, "rev-parse", "HEAD").lower()
            _git(repo, "checkout", "main")
            _git(repo, "fetch", "--no-tags", "origin", "main")

            verification_ref = verification.write_verification_evidence(
                {
                    "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
                    "slice_id": "slice-remote",
                    "candidate": candidate,
                    "status": "reviewing",
                    "summary": "verification-succeeded",
                    "details": {"ok": True},
                },
                coordinator_root=coordinator_root,
            )
            review_payload = review.build_gate_evaluation(
                slice_id="slice-remote",
                state="passed",
                reason="accepted",
                builder_job_id="slice-remote-builder-1",
                reviewer_job_id="slice-remote-review-1",
                candidate=candidate,
                launch_identity={
                    "builder": {
                        "executor": "copilot",
                        "model_id": "builder-model",
                        "independence_domain": "builder-domain",
                    },
                    "reviewer": {
                        "executor": "codex",
                        "model_id": "reviewer-model",
                        "independence_domain": "reviewer-domain",
                    },
                },
            )
            review_ref = review.write_gate_evaluation(review_payload, coordinator_root=coordinator_root)
            review_path = Path(review_ref["path"])
            review_hash = review_ref["hash"]
            record = completion.write_completion_record(
                {
                    "schema_version": completion.COMPLETION_SCHEMA_VERSION,
                    "slice_id": "slice-remote",
                    "spec_hash": "1" * 64,
                    "plan_hash": "2" * 64,
                    "verification_hash": "3" * 64,
                    "builder_job_id": "slice-remote-builder-1",
                    "reviewer_job_id": "slice-remote-review-1",
                    "dispatch_base": base_sha.lower(),
                    "candidate": candidate,
                    "target_branch": "main",
                    "target_remote": "origin",
                    "target_ref": "refs/remotes/origin/main",
                    "target_ref_sha": base_sha.lower(),
                    "verification_evidence_path": verification_ref["path"],
                    "verification_evidence_hash": verification_ref["hash"],
                    "review_policy": "required",
                    "docs_class": "code",
                    "review_evaluation_path": str(review_path),
                    "review_evaluation_hash": review_hash,
                    "completed_at": "2026-07-12T00:00:00+00:00",
                },
                coordinator_root=coordinator_root,
            )
            handoff.mkdir(parents=True, exist_ok=True)
            (handoff / "slice-remote.json").write_text(
                json.dumps(
                    {
                        "slice_id": "slice-remote",
                        "job_id": "slice-remote-builder-1",
                        "gate_status": "verified",
                        "completion": "exited",
                        "exit_code": 0,
                        "branch": "feature/slice-remote",
                        "gate_reason": "candidate-not-merged",
                        "gate_verdict": review_payload,
                        "verification_evidence_path": verification_ref["path"],
                        "verification_evidence_hash": verification_ref["hash"],
                        "review_evaluation_path": str(review_path),
                        "review_evaluation_hash": review_hash,
                        "completion_record_path": record["path"],
                        "completion_record_hash": record["hash"],
                        "slice_state": "completed",
                        "spec_hash": "1" * 64,
                        "plan_hash": "2" * 64,
                        "verification_hash": "3" * 64,
                        "completed_at": "2026-07-12T00:00:00+00:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            self.assertIsNone(
                completion.load_completion_from_handoff(
                    "slice-remote",
                    handoff_dir=str(handoff),
                    repo_root=repo,
                )
            )

            _git(repo, "merge", "--no-ff", "feature/slice-remote", "-m", "merge candidate")
            _git(repo, "push", "origin", "main")
            _git(repo, "fetch", "--no-tags", "origin", "main")

            loaded = completion.load_completion_from_handoff(
                "slice-remote",
                handoff_dir=str(handoff),
                repo_root=repo,
            )
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["candidate"], candidate)

    def test_dispatch_ready_requires_target_base_to_include_upstream_candidates(self) -> None:
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
            metas = [
                _meta(slice_id="up", spec_path=root / "specs" / "up.md", dispatch="hold"),
                _meta(slice_id="down", spec_path=root / "specs" / "down.md", depends_on=["up"]),
            ]

            reg_ok = JobRegistry(state_path=root / "jobs-ok.json")
            wt_ok = _RecordingWorktreeCreator()
            dispatcher_ok = Dispatcher(registry=reg_ok, pane_sender=_NoPaneSender(), worktree_creator=wt_ok)

            def git_runner_ok(args: list[str]):
                mapping = {
                    ("-C", str(root), "fetch", "--no-tags", "origin", "main"): _git_ok(""),
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/main"): _git_ok(target_sha),
                    ("-C", str(root), "merge-base", "--is-ancestor", candidate, target_sha): _git_ok(""),
                    ("rev-parse", "feature/down"): "d" * 40,
                }
                return mapping[tuple(args)]

            jobs = autonomy.dispatch_ready(
                metas,
                is_satisfied=lambda slice_id: slice_id == "up",
                dispatcher=dispatcher_ok,
                launcher=_Launcher(),
                handoff_dir=str(handoff),
                git_runner=git_runner_ok,
            )
            self.assertEqual(len(jobs), 1)
            self.assertEqual(wt_ok.calls, [("feature/down", target_sha)])

            reg_stale = JobRegistry(state_path=root / "jobs-stale.json")
            wt_stale = _RecordingWorktreeCreator()
            dispatcher_stale = Dispatcher(
                registry=reg_stale,
                pane_sender=_NoPaneSender(),
                worktree_creator=wt_stale,
            )

            def git_runner_stale(args: list[str]):
                mapping = {
                    ("-C", str(root), "fetch", "--no-tags", "origin", "main"): _git_ok(""),
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/main"): _git_ok(target_sha),
                    ("-C", str(root), "merge-base", "--is-ancestor", candidate, target_sha): _git_nonzero(1),
                }
                return mapping[tuple(args)]

            with self.assertRaises(autonomy.DispatchReadyError):
                autonomy.dispatch_ready(
                    metas,
                    is_satisfied=lambda slice_id: slice_id == "up",
                    dispatcher=dispatcher_stale,
                    launcher=_Launcher(),
                    handoff_dir=str(handoff),
                    git_runner=git_runner_stale,
                )
            self.assertEqual(reg_stale.list_jobs(), [])
            self.assertEqual(wt_stale.calls, [])

    def test_restart_only_satisfies_dependencies_after_slice_state_completed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            handoff = root / "handoff"
            candidate = "b" * 40
            target_sha = "c" * 40
            _seed_dependency_completion(
                root=root,
                handoff_dir=handoff,
                slice_id="up",
                candidate=candidate,
                target_sha=target_sha,
            )
            manifest_path = handoff / "up.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["slice_state"] = "verified"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

            def git_runner(args: list[str]):
                mapping = {
                    ("-C", str(root), "rev-parse", "refs/remotes/origin/main"): _git_ok(target_sha),
                    ("-C", str(root), "merge-base", "--is-ancestor", candidate, target_sha): _git_ok(""),
                }
                return mapping[tuple(args)]

            self.assertFalse(
                autonomy.default_is_satisfied(
                    "up",
                    handoff_dir=str(handoff),
                    repo_root=root,
                    git_runner=git_runner,
                )
            )

            manifest["slice_state"] = "completed"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            self.assertTrue(
                autonomy.default_is_satisfied(
                    "up",
                    handoff_dir=str(handoff),
                    repo_root=root,
                    git_runner=git_runner,
                )
            )

    def test_reaper_never_signals_foreign_roots_or_changed_identity(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            proc_root = tmp / "proc"
            proc_root.mkdir()
            project_root = tmp / "workspace" / "project"
            other_root = tmp / "workspace" / "project-other"
            _write_killer(tmp / "fake-kill.sh")

            _write_proc_pid(
                proc_root,
                301,
                ppid=1,
                starttime=30101,
                cwd=other_root / "slice-b",
                cmdline=_cmdline("node", "app-server-broker.mjs", "serve", "--cwd", str(other_root / "slice-b")),
            )
            _write_proc_pid(
                proc_root,
                302,
                ppid=1,
                starttime=30202,
                cwd=project_root / "mutated-cmdline",
                cmdline=_cmdline("node", "other-broker.mjs", "serve", "--cwd", str(project_root / "mutated-cmdline")),
            )
            _write_snapshot(
                tmp / "snapshot.txt",
                [
                    "1 0 /sbin/init",
                    f"301 1 node app-server-broker.mjs serve --cwd {other_root / 'slice-b'}",
                    f"302 1 node app-server-broker.mjs serve --cwd {project_root / 'mutated-cmdline'}",
                ],
            )

            result = _run_reaper_script(tmp, "--apply", "--cwd-root", str(project_root))
            self.assertEqual(result.returncode, 0, result.stderr)
            kill_log = tmp / "kill.log"
            if kill_log.exists():
                self.assertEqual(kill_log.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
