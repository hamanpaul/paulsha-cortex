from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, manager_daemon, verification
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.registry import JobRegistry


class _PaneSender:
    def send(self, pane_id: str, command: str) -> None:  # pragma: no cover - seam only
        _ = (pane_id, command)


class _WorktreeCreator:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self.calls: list[tuple[str, str | None]] = []

    def create(self, branch: str, base_sha: str | None = None) -> str:
        self.calls.append((branch, base_sha))
        worktree = self._base_dir / branch.replace("/", "__")
        worktree.mkdir(parents=True, exist_ok=True)
        return str(worktree)


class _Launcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        self.calls.append({"slice_id": slice_id, "prompt": prompt, "worktree": worktree, "log_dir": log_dir})
        return LaunchHandle(
            executor="copilot",
            model_id="gpt-5.3-codex",
            session_name=f"session-{slice_id}",
            pid=4321,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def _git_runner(args: list[str]):
    if not args:
        return ""
    if len(args) >= 5 and args[0] == "-C" and args[2] == "fetch":
        return ""
    if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
        target = args[3]
        if target.startswith("refs/remotes/"):
            return "f" * 40
        return "e" * 40
    if len(args) >= 6 and args[0] == "-C" and args[2] == "merge-base" and args[3] == "--is-ancestor":
        return ""
    if args[0] == "rev-parse":
        return "e" * 40
    return ""


def _write_spec(repo_root: Path, slice_id: str) -> tuple[Path, Path]:
    specs_dir = repo_root / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    plan_path = repo_root / "docs" / "superpowers" / "plans" / f"{slice_id}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(f"# {slice_id}\n", encoding="utf-8")
    spec_path = specs_dir / f"{slice_id}.md"
    spec_path.write_text(
        "---\n"
        "dispatch: auto\n"
        f"slice_id: {slice_id}\n"
        f"plan: {plan_path.relative_to(repo_root).as_posix()}\n"
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
    return spec_path, plan_path


def _create_slice_in_needs_human(
    registry: JobRegistry,
    *,
    repo_root: Path,
    slice_id: str,
    builder_status: str = "failed",
) -> dict:
    spec_path, plan_path = _write_spec(repo_root, slice_id)
    builder_job = registry.create_job(
        task=slice_id,
        persona="builder",
        branch=f"feature/{slice_id}",
        pane="",
        worktree=str(repo_root / "worktrees" / "old"),
        dispatch_head="a" * 40,
        executor="copilot",
        session_name=f"builder-{slice_id}",
        pid=1111,
        log_path=str(repo_root / "logs" / f"{slice_id}.jsonl"),
    )
    if builder_status in {"exited", "failed"}:
        registry.update_headless_result(
            builder_job["job_id"],
            status=builder_status,
            exit_code=0 if builder_status == "exited" else 1,
        )
    verification_contract = {
        "docs_class": "code",
        "review_policy": "required",
        "required_artifacts": [],
        "checks": [{"kind": "persona-scope"}],
        "tests": [],
        "full_suite": {
            "argv": ["python3", "-m", "pytest", "-q"],
            "cwd": ".",
            "timeout_seconds": 60,
            "baseline": "no-regression",
        },
    }
    slice_row = registry.create_slice(
        slice_id=slice_id,
        spec_path=str(spec_path),
        spec_hash=verification.sha256_bytes(spec_path.read_bytes()),
        plan_path=str(plan_path),
        plan_hash=verification.sha256_bytes(plan_path.read_bytes()),
        target_branch="main",
        target_remote="origin",
        verification_hash=verification.canonical_json_hash(verification_contract),
        verification=verification_contract,
        dispatch_base="a" * 40,
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=None,
        candidate="b" * 40,
    )
    return registry.update_slice(
        slice_id,
        state="needs_human",
        gate_state="needs_human",
        current_evidence_refs=["old-evidence.json"],
        current_evaluation_refs=["old-evaluation.json"],
        candidate="b" * 40,
    )


def test_retry_build_dispatches_new_builder_and_clears_current_refs(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    state_path = tmp_path / "runtime" / "coordinator" / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    _create_slice_in_needs_human(registry, repo_root=repo_root, slice_id="slice-a")

    dispatcher = Dispatcher(
        registry=registry,
        pane_sender=_PaneSender(),
        worktree_creator=_WorktreeCreator(tmp_path / "worktrees"),
        git_runner=_git_runner,
    )
    launcher = _Launcher()

    result = manager.apply_slice_action(
        dispatcher,
        slice_id="slice-a",
        action="retry-build",
        actor="operator",
        specs_dir=str(repo_root / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=launcher,
        git_runner=_git_runner,
    )

    slice_row = registry.get_slice("slice-a")
    assert result["action"] == "retry-build"
    assert result["slice_id"] == "slice-a"
    assert result["job_id"] == slice_row["builder_job_id"]
    assert slice_row["state"] == "building"
    assert slice_row["gate_state"] == "pending"
    assert slice_row["current_evidence_refs"] == []
    assert slice_row["current_evaluation_refs"] == []
    assert slice_row["candidate"] is None
    assert slice_row["actions"][-1]["action"] == "operator-retry-build"
    assert slice_row["actions"][-1]["actor"] == "operator"
    assert slice_row["actions"][-1]["result"] == "ok"
    assert launcher.calls and launcher.calls[0]["slice_id"] == "slice-a"


def test_retry_review_rejected_without_verified_evidence(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    state_path = tmp_path / "runtime" / "coordinator" / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    registry_slice = _create_slice_in_needs_human(registry, repo_root=repo_root, slice_id="slice-a")
    registry.update_slice(registry_slice["slice_id"], current_evidence_refs=[])

    dispatcher = Dispatcher(
        registry=registry,
        pane_sender=_PaneSender(),
        worktree_creator=_WorktreeCreator(tmp_path / "worktrees"),
        git_runner=_git_runner,
    )

    with pytest.raises(ValueError, match="action-not-allowed"):
        manager.apply_slice_action(
            dispatcher,
            slice_id="slice-a",
            action="retry-review",
            actor="operator",
            specs_dir=str(repo_root / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            review_launcher=_Launcher(),
            review_executor="copilot",
            git_runner=_git_runner,
        )


def test_abandon_marks_slice_failed_and_records_result(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    state_path = tmp_path / "runtime" / "coordinator" / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    _create_slice_in_needs_human(registry, repo_root=repo_root, slice_id="slice-a")
    dispatcher = Dispatcher(
        registry=registry,
        pane_sender=_PaneSender(),
        worktree_creator=_WorktreeCreator(tmp_path / "worktrees"),
        git_runner=_git_runner,
    )

    result = manager.apply_slice_action(
        dispatcher,
        slice_id="slice-a",
        action="abandon",
        actor="operator",
        specs_dir=str(repo_root / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        git_runner=_git_runner,
    )

    slice_row = registry.get_slice("slice-a")
    assert result["action"] == "abandon"
    assert slice_row["state"] == "failed"
    assert slice_row["gate_state"] == "failed"
    assert slice_row["actions"][-1]["action"] == "operator-abandon"
    assert slice_row["actions"][-1]["result"] == "ok"


def test_runtime_status_provider_includes_attention_next_actions(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    state_path = tmp_path / "runtime" / "coordinator" / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    slice_row = _create_slice_in_needs_human(
        registry,
        repo_root=repo_root,
        slice_id="slice-a",
        builder_status="exited",
    )

    evidence = verification.write_verification_evidence(
        {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": "slice-a",
            "candidate": "b" * 40,
            "status": "verified",
            "summary": "verification-succeeded",
            "details": {"ok": True},
        },
        coordinator_root=state_path.parent,
    )
    registry.update_slice("slice-a", current_evidence_refs=[evidence["path"]], candidate="b" * 40)
    (handoff_dir / "slice-a.json").write_text(
        json.dumps(
            {
                "slice_id": "slice-a",
                "job_id": slice_row["builder_job_id"],
                "gate_status": "needs_human",
                "gate_reason": "foreign-review-absent",
            }
        ),
        encoding="utf-8",
    )

    provider = manager_daemon.build_runtime_status_provider(
        registry=registry,
        specs_dir=str(repo_root / "specs"),
        handoff_dir=str(handoff_dir),
        scan_specs_fn=lambda specs_dir: [],
        git_runner=_git_runner,
    )

    status = provider()

    assert "attention" in status
    assert len(status["attention"]) == 1
    attention = status["attention"][0]
    assert attention["slice_id"] == "slice-a"
    assert attention["slice_state"] == "needs_human"
    assert attention["reason"] == "foreign-review-absent"
    assert set(attention["next_actions"]) == {"retry-build", "retry-verify", "retry-review", "abandon"}
