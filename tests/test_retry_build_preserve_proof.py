from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from git_fixtures import make_fake_repo
from paulsha_cortex.coordinator import autonomy, manager, manager_daemon, verification
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry


def _verification_contract() -> dict[str, object]:
    return {
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
                "timeout_seconds": 60,
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


def _identity_registry() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-5.4-codex",
                "independence_domain": "openai",
            }
        ]
    )


def _git_runner(args: list[str]):
    if not args:
        return ""
    if args[0] == "rev-parse":
        return "f" * 40
    if len(args) >= 5 and args[0] == "-C" and args[2] == "fetch":
        return ""
    if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
        target = args[3]
        if target.startswith("refs/remotes/"):
            return "f" * 40
        return "e" * 40
    if len(args) >= 6 and args[0] == "-C" and args[2] == "merge-base":
        return ""
    return ""


class _WorktreeCreator:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self.calls: list[tuple[str, str | None, str | None]] = []

    def create(self, branch: str, base_sha: str | None = None, *, job_id: str | None = None) -> str:
        self.calls.append((branch, base_sha, job_id))
        worktree = self._base_dir / branch.replace("/", "__")
        worktree.mkdir(parents=True, exist_ok=True)
        return str(worktree)


class _RaisingWorktreeCreator(_WorktreeCreator):
    def __init__(
        self,
        base_dir: Path,
        exc: Exception,
        *,
        before_raise: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(base_dir)
        self._exc = exc
        self._before_raise = before_raise

    def create(self, branch: str, base_sha: str | None = None, *, job_id: str | None = None) -> str:
        self.calls.append((branch, base_sha, job_id))
        if self._before_raise is not None:
            self._before_raise()
        raise self._exc


class _RecordingLauncher:
    def __init__(
        self,
        *,
        executor: str = "copilot",
        model_id: str | None = None,
        launch_exc: Exception | None = None,
    ) -> None:
        self.executor = executor
        self._model_id = model_id
        self._launch_exc = launch_exc
        self.calls: list[dict[str, str]] = []
        self.commit_required_calls = 0

    @property
    def model(self) -> str | None:
        return self._model_id

    def as_commit_required(self):
        self.commit_required_calls += 1
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        self.calls.append(
            {
                "slice_id": slice_id,
                "prompt": prompt,
                "worktree": worktree,
                "log_dir": log_dir,
            }
        )
        if self._launch_exc is not None:
            raise self._launch_exc
        return LaunchHandle(
            executor=self.executor,
            model_id=self._model_id,
            session_name=f"session-{slice_id}",
            pid=4242,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def _write_spec(
    repo_root: Path,
    slice_id: str,
    *,
    body: str = "body\n",
    executor: str | None = None,
    model_id: str | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    specs_dir = repo_root / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    plan_path = repo_root / "docs" / "superpowers" / "plans" / f"{slice_id}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(f"# {slice_id}\n", encoding="utf-8")
    verification_contract = _verification_contract()
    identity_block = ""
    if executor is not None:
        identity_block += f"executor: {executor}\n"
    if model_id is not None:
        identity_block += f"model_id: {model_id}\n"
    spec_path = specs_dir / f"{slice_id}.md"
    spec_path.write_text(
        "---\n"
        "dispatch: auto\n"
        f"slice_id: {slice_id}\n"
        f"plan: {plan_path.relative_to(repo_root).as_posix()}\n"
        "target_branch: main\n"
        f"{identity_block}"
        "verification:\n"
        "  docs_class: code\n"
        "  review_policy: required\n"
        "  required_artifacts: []\n"
        "  checks:\n"
        "    - kind: persona-scope\n"
        "    - kind: command\n"
        "      name: policy\n"
        "      argv: [python3, -m, pytest, -q]\n"
        "      cwd: .\n"
        "      timeout_seconds: 60\n"
        "  tests: []\n"
        "  full_suite:\n"
        "    argv: [python3, -m, pytest, -q]\n"
        "    cwd: .\n"
        "    timeout_seconds: 60\n"
        "    baseline: no-regression\n"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return spec_path, plan_path, verification_contract


def _scan_slice(specs_dir: Path, slice_id: str) -> dict[str, object]:
    meta = next(
        candidate
        for candidate in autonomy.scan_specs(str(specs_dir))
        if candidate.get("slice_id") == slice_id
    )
    assert isinstance(meta.get("parse_error"), type(None))
    return meta


def _create_recovery_slice(
    registry: JobRegistry,
    *,
    repo_root: Path,
    slice_id: str = "slice-a",
    state: str = "needs_human",
    candidate: str | None = "b" * 40,
    include_reviewer: bool = True,
    executor: str | None = None,
    model_id: str | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object] | None]:
    spec_path, plan_path, verification_contract = _write_spec(
        repo_root,
        slice_id,
        executor=executor,
        model_id=model_id,
    )
    builder_job = registry.create_job(
        task=slice_id,
        persona="builder",
        branch=f"feature/{slice_id}",
        pane="",
        worktree=str(repo_root / "worktrees" / "old-builder"),
        dispatch_head="a" * 40,
        executor="copilot",
        session_name=f"builder-{slice_id}",
        pid=1111,
        log_path=str(repo_root / "logs" / f"{slice_id}.jsonl"),
    )
    registry.update_headless_result(builder_job["job_id"], status="exited", exit_code=0)
    reviewer_job = None
    if include_reviewer:
        reviewer_job = registry.create_job(
            task=slice_id,
            persona="reviewer",
            kind="review",
            branch=f"feature/{slice_id}",
            pane="",
            worktree=str(repo_root / "worktrees" / "old-reviewer"),
            subject_head=candidate,
            executor="claude",
            model_id="claude-sonnet-4.5",
        )
        registry.update_status(reviewer_job["job_id"], "exited")
    registry.create_slice(
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
        reviewer_job_id=reviewer_job["job_id"] if reviewer_job is not None else None,
        candidate=candidate,
    )
    registry.update_slice(
        slice_id,
        state=state,
        gate_state="failed" if state == "failed" else "needs_human",
        current_evidence_refs=["old-evidence.json"],
        current_evaluation_refs=["old-evaluation.json"],
        current_verification_evidence_hash="d" * 64,
    )
    return registry.get_slice(slice_id), builder_job, reviewer_job


def _create_pending_slice(
    registry: JobRegistry,
    *,
    repo_root: Path,
    slice_id: str = "slice-a",
) -> dict[str, object]:
    spec_path, plan_path, verification_contract = _write_spec(repo_root, slice_id, body="pending\n")
    registry.create_slice(
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
        builder_job_id=None,
        reviewer_job_id=None,
        candidate="b" * 40,
    )
    registry.update_slice(
        slice_id,
        current_evidence_refs=["pending-evidence.json"],
        current_evaluation_refs=["pending-evaluation.json"],
        current_verification_evidence_hash="e" * 64,
        candidate="b" * 40,
    )
    return registry.get_slice(slice_id)


def _slice_snapshot(slice_row: dict[str, object]) -> dict[str, object]:
    return {
        "spec_path": slice_row["spec"]["path"],
        "spec_hash": slice_row["spec"]["hash"],
        "plan_path": slice_row["plan"]["path"],
        "plan_hash": slice_row["plan"]["hash"],
        "target_branch": slice_row["target_branch"],
        "target_remote": slice_row["target_remote"],
        "verification_hash": slice_row["verification"]["hash"],
        "dispatch_base": slice_row.get("dispatch_base"),
        "builder_job_id": slice_row.get("builder_job_id"),
        "reviewer_job_id": slice_row.get("reviewer_job_id"),
        "candidate": slice_row.get("candidate"),
        "current_verification_evidence_hash": slice_row.get("current_verification_evidence_hash"),
        "current_evidence_refs": list(slice_row.get("current_evidence_refs") or []),
        "current_evaluation_refs": list(slice_row.get("current_evaluation_refs") or []),
        "state": slice_row["state"],
        "gate_state": slice_row["gate_state"],
        "actions_len": len(slice_row["actions"]),
        "evidence_history_len": len(slice_row["evidence_history"]),
        "evaluation_history_len": len(slice_row["evaluation_history"]),
    }


def _assert_preserved_recovery_slice(
    current: dict[str, object],
    snapshot: dict[str, object],
) -> None:
    assert current["spec"]["path"] == snapshot["spec_path"]
    assert current["spec"]["hash"] == snapshot["spec_hash"]
    assert current["plan"]["path"] == snapshot["plan_path"]
    assert current["plan"]["hash"] == snapshot["plan_hash"]
    assert current["target_branch"] == snapshot["target_branch"]
    assert current["target_remote"] == snapshot["target_remote"]
    assert current["verification"]["hash"] == snapshot["verification_hash"]
    assert current.get("builder_job_id") == snapshot["builder_job_id"]
    assert current.get("reviewer_job_id") == snapshot["reviewer_job_id"]
    assert current.get("candidate") == snapshot["candidate"]
    assert (
        current.get("current_verification_evidence_hash")
        == snapshot["current_verification_evidence_hash"]
    )
    assert current.get("current_evidence_refs") == snapshot["current_evidence_refs"]
    assert current.get("current_evaluation_refs") == snapshot["current_evaluation_refs"]
    assert current.get("dispatch_base") == snapshot["dispatch_base"]
    assert current["state"] == snapshot["state"]
    assert current["gate_state"] == snapshot["gate_state"]
    assert len(current["actions"]) == snapshot["actions_len"] + 1
    assert len(current["evidence_history"]) == snapshot["evidence_history_len"]
    assert len(current["evaluation_history"]) == snapshot["evaluation_history_len"]
    assert current["actions"][-1]["action"] == "dispatch-failed"
    assert current["actions"][-1]["actor"] == "manager"
    assert current["actions"][-1]["state"] == snapshot["state"]
    assert current["actions"][-1]["gate_state"] == snapshot["gate_state"]


def _new_jobs(registry: JobRegistry, *, existing_ids: set[str]) -> list[dict[str, object]]:
    return [job for job in registry.list_jobs() if job["job_id"] not in existing_ids]


def test_retry_build_worktree_failure_preserves_existing_proof(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo_root = tmp_path / "repo"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    slice_row, _, _ = _create_recovery_slice(registry, repo_root=repo_root)
    snapshot = _slice_snapshot(slice_row)
    expected_actions = manager.allowed_slice_actions(registry, slice_row)
    spec_path = Path(str(slice_row["spec"]["path"]))
    spec_path.write_text(spec_path.read_text(encoding="utf-8") + "\nretry-context\n", encoding="utf-8")
    rewritten_hash = verification.sha256_bytes(spec_path.read_bytes())
    worktree_creator = _RaisingWorktreeCreator(
        tmp_path / "worktrees",
        ValueError("existing worktree branch has commits outside requested base"),
    )
    dispatcher = Dispatcher(registry, pane_sender=None, worktree_creator=worktree_creator, git_runner=_git_runner)
    launcher = _RecordingLauncher()

    with caplog.at_level("INFO", logger=autonomy.__name__):
        with pytest.raises(autonomy.DispatchReadyError):
            manager.apply_slice_action(
                dispatcher,
                slice_id="slice-a",
                action="retry-build",
                actor="operator",
                specs_dir=str(repo_root / "specs"),
                handoff_dir=str(tmp_path / "handoff"),
                launcher=launcher,
                git_runner=_git_runner,
            )

    restored = registry.get_slice("slice-a")
    assert restored["spec"]["hash"] == snapshot["spec_hash"]
    assert restored["spec"]["hash"] != rewritten_hash
    _assert_preserved_recovery_slice(restored, snapshot)
    assert manager.allowed_slice_actions(registry, restored) == expected_actions
    assert worktree_creator.calls == [("feature/slice-a", "f" * 40, "slice-a")]
    assert launcher.calls == []
    success_logs = [
        record
        for record in caplog.records
        if record.name == autonomy.__name__
        and record.getMessage() == "recovery dispatch failure settled"
    ]
    assert len(success_logs) == 1
    assert success_logs[0].slice_id == "slice-a"
    assert success_logs[0].restored is True
    assert (
        success_logs[0].error_summary
        == "ValueError: existing worktree branch has commits outside requested base"
    )


@pytest.mark.parametrize(
    ("label", "executor", "model_id", "launcher_factory"),
    [
        (
            "launcher_factory unavailable",
            "codex",
            "gpt-5.4-codex",
            None,
        ),
        (
            "unknown identity",
            "codex",
            "missing-model",
            lambda identity: _RecordingLauncher(executor=identity.executor, model_id=identity.model_id),
        ),
    ],
)
def test_retry_build_identity_barriers_preserve_recovery_slice(
    tmp_path: Path,
    label: str,
    executor: str,
    model_id: str,
    launcher_factory,
) -> None:
    repo_root = tmp_path / f"repo-{label.replace(' ', '-')}"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / label.replace(" ", "-") / "jobs.json")
    slice_row, _, _ = _create_recovery_slice(
        registry,
        repo_root=repo_root,
        executor=executor,
        model_id=model_id,
    )
    snapshot = _slice_snapshot(slice_row)
    expected_actions = manager.allowed_slice_actions(registry, slice_row)
    worktree_creator = _WorktreeCreator(tmp_path / "worktrees")
    dispatcher = Dispatcher(registry, pane_sender=None, worktree_creator=worktree_creator, git_runner=_git_runner)
    launcher = _RecordingLauncher()

    with pytest.raises(autonomy.DispatchReadyError):
        manager.apply_slice_action(
            dispatcher,
            slice_id="slice-a",
            action="retry-build",
            actor="operator",
            specs_dir=str(repo_root / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            launcher=launcher,
            identity_registry=_identity_registry(),
            launcher_factory=launcher_factory,
            git_runner=_git_runner,
        )

    restored = registry.get_slice("slice-a")
    _assert_preserved_recovery_slice(restored, snapshot)
    assert manager.allowed_slice_actions(registry, restored) == expected_actions
    assert worktree_creator.calls == []
    assert launcher.calls == []


def test_retry_build_spawn_failure_preserves_proof_and_complete_tick_skips_unbound_job(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    slice_row, _, reviewer_job = _create_recovery_slice(registry, repo_root=repo_root)
    assert reviewer_job is not None
    snapshot = _slice_snapshot(slice_row)
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = handoff_dir / "slice-a.json"
    manifest_path.write_text(
        json.dumps(
            {
                "slice_id": "slice-a",
                "job_id": reviewer_job["job_id"],
                "gate_status": "needs_human",
                "gate_evaluation": {"path": "old-evaluation.json"},
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest_bytes = manifest_path.read_bytes()
    existing_job_ids = {job["job_id"] for job in registry.list_jobs()}
    worktree_creator = _WorktreeCreator(tmp_path / "worktrees")
    dispatcher = Dispatcher(registry, pane_sender=None, worktree_creator=worktree_creator, git_runner=_git_runner)
    launcher = _RecordingLauncher(launch_exc=RuntimeError("spawn failed before attach"))

    with pytest.raises(autonomy.DispatchReadyError):
        manager.apply_slice_action(
            dispatcher,
            slice_id="slice-a",
            action="retry-build",
            actor="operator",
            specs_dir=str(repo_root / "specs"),
            handoff_dir=str(handoff_dir),
            launcher=launcher,
            git_runner=_git_runner,
        )

    restored = registry.get_slice("slice-a")
    _assert_preserved_recovery_slice(restored, snapshot)
    failed_jobs = _new_jobs(registry, existing_ids=existing_job_ids)
    assert len(failed_jobs) == 1
    assert failed_jobs[0]["status"] == "failed"
    assert failed_jobs[0]["runtime_diagnostic"]["reason"] == "launch-failed"
    verification_calls: list[dict[str, object]] = []

    def _verification_runner(**kwargs):
        verification_calls.append(kwargs)
        return None

    first = manager.complete_tick(
        dispatcher,
        handoff_dir=str(handoff_dir),
        verification_runner=_verification_runner,
    )
    second = manager.complete_tick(
        dispatcher,
        handoff_dir=str(handoff_dir),
        verification_runner=_verification_runner,
    )

    assert manifest_path.read_bytes() == manifest_bytes
    assert first["completed"] == []
    assert second["completed"] == []
    assert not any(error.get("job_id") == failed_jobs[0]["job_id"] for error in first["errors"])
    assert not any(error.get("job_id") == failed_jobs[0]["job_id"] for error in second["errors"])
    assert verification_calls == []
    after_ticks = registry.get_slice("slice-a")
    assert len(after_ticks["actions"]) == snapshot["actions_len"] + 1
    assert len(after_ticks["evidence_history"]) == snapshot["evidence_history_len"]
    assert len(after_ticks["evaluation_history"]) == snapshot["evaluation_history_len"]


@pytest.mark.parametrize("candidate", ["b" * 40, None])
def test_retry_build_failed_recovery_state_stays_failed(tmp_path: Path, candidate: str | None) -> None:
    repo_root = tmp_path / f"repo-{'with' if candidate else 'without'}-candidate"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / f"{'with' if candidate else 'without'}-candidate.json")
    slice_row, _, _ = _create_recovery_slice(
        registry,
        repo_root=repo_root,
        state="failed",
        candidate=candidate,
        include_reviewer=candidate is not None,
    )
    snapshot = _slice_snapshot(slice_row)
    expected_actions = manager.allowed_slice_actions(registry, slice_row)
    dispatcher = Dispatcher(
        registry,
        pane_sender=None,
        worktree_creator=_RaisingWorktreeCreator(
            tmp_path / "worktrees",
            ValueError("existing worktree branch has commits outside requested base"),
        ),
        git_runner=_git_runner,
    )

    with pytest.raises(autonomy.DispatchReadyError):
        manager.apply_slice_action(
            dispatcher,
            slice_id="slice-a",
            action="retry-build",
            actor="operator",
            specs_dir=str(repo_root / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            launcher=_RecordingLauncher(),
            git_runner=_git_runner,
        )

    restored = registry.get_slice("slice-a")
    assert restored["state"] == "failed"
    assert restored["gate_state"] == "failed"
    _assert_preserved_recovery_slice(restored, snapshot)
    assert manager.allowed_slice_actions(registry, restored) == expected_actions


def test_retry_build_control_request_path_preserves_recovery_slice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    make_fake_repo(repo_root)
    monkeypatch.setenv("PSC_REPO_ROOT", str(repo_root))
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    slice_row, _, _ = _create_recovery_slice(
        registry,
        repo_root=repo_root,
        executor="codex",
        model_id="gpt-5.4-codex",
    )
    snapshot = _slice_snapshot(slice_row)
    expected_actions = manager.allowed_slice_actions(registry, slice_row)
    worktree_creator = _WorktreeCreator(tmp_path / "worktrees")
    dispatcher = Dispatcher(registry, pane_sender=None, worktree_creator=worktree_creator, git_runner=_git_runner)
    default_launcher = _RecordingLauncher()

    def _resolve_launcher(executor, injected, *, allow_unsafe, model):
        _ = allow_unsafe
        if executor == "codex" and model == "gpt-5.4-codex":
            raise RuntimeError("spec identity launch barrier")
        return injected

    monkeypatch.setattr(manager_daemon, "_resolve_launcher", _resolve_launcher)
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(repo_root / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=default_launcher,
        workflow_identity_registry=_identity_registry(),
    )

    with pytest.raises(autonomy.DispatchReadyError):
        request_executor(
            {
                "type": "slice-action",
                "args": {
                    "slice_id": "slice-a",
                    "action": "retry-build",
                    "actor": "operator",
                },
                "requested_by": "operator",
            }
        )

    restored = registry.get_slice("slice-a")
    _assert_preserved_recovery_slice(restored, snapshot)
    assert manager.allowed_slice_actions(registry, restored) == expected_actions
    assert worktree_creator.calls == []
    assert default_launcher.calls == []


def test_retry_build_restore_guard_preserves_concurrent_candidate_write(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    _create_recovery_slice(registry, repo_root=repo_root)
    dispatcher = Dispatcher(
        registry,
        pane_sender=None,
        worktree_creator=_RaisingWorktreeCreator(
            tmp_path / "worktrees",
            ValueError("existing worktree branch has commits outside requested base"),
            before_raise=lambda: registry.update_slice("slice-a", candidate="c" * 40),
        ),
        git_runner=_git_runner,
    )

    with pytest.raises(autonomy.DispatchReadyError):
        manager.apply_slice_action(
            dispatcher,
            slice_id="slice-a",
            action="retry-build",
            actor="operator",
            specs_dir=str(repo_root / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            launcher=_RecordingLauncher(),
            git_runner=_git_runner,
        )

    updated = registry.get_slice("slice-a")
    assert updated["state"] == "needs_human"
    assert updated["gate_state"] == "needs_human"
    assert updated["candidate"] == "c" * 40


def test_retry_build_restore_guard_degrades_when_restore_repin_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    _create_recovery_slice(registry, repo_root=repo_root)
    dispatcher = Dispatcher(
        registry,
        pane_sender=None,
        worktree_creator=_RaisingWorktreeCreator(
            tmp_path / "worktrees",
            ValueError("existing worktree branch has commits outside requested base"),
        ),
        git_runner=_git_runner,
    )
    real_repin = registry.repin_slice
    repin_calls = 0

    def _repin_slice(*args, **kwargs):
        nonlocal repin_calls
        repin_calls += 1
        if repin_calls == 2:
            raise RuntimeError("restore repin failed")
        return real_repin(*args, **kwargs)

    monkeypatch.setattr(registry, "repin_slice", _repin_slice)

    with pytest.raises(autonomy.DispatchReadyError):
        manager.apply_slice_action(
            dispatcher,
            slice_id="slice-a",
            action="retry-build",
            actor="operator",
            specs_dir=str(repo_root / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            launcher=_RecordingLauncher(),
            git_runner=_git_runner,
        )

    updated = registry.get_slice("slice-a")
    assert repin_calls == 2
    assert updated["state"] == "needs_human"
    assert updated["gate_state"] == "needs_human"


def test_retry_build_restore_guard_preserves_concurrent_evidence_write(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    _create_recovery_slice(registry, repo_root=repo_root)

    def _concurrent_evidence() -> None:
        registry.record_action(
            "slice-a",
            action="concurrent-evidence",
            actor="other",
            evidence_refs=["new-evidence.json"],
        )

    dispatcher = Dispatcher(
        registry,
        pane_sender=None,
        worktree_creator=_RaisingWorktreeCreator(
            tmp_path / "worktrees",
            ValueError("existing worktree branch has commits outside requested base"),
            before_raise=_concurrent_evidence,
        ),
        git_runner=_git_runner,
    )

    with pytest.raises(autonomy.DispatchReadyError):
        manager.apply_slice_action(
            dispatcher,
            slice_id="slice-a",
            action="retry-build",
            actor="operator",
            specs_dir=str(repo_root / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            launcher=_RecordingLauncher(),
            git_runner=_git_runner,
        )

    updated = registry.get_slice("slice-a")
    assert updated["state"] == "needs_human"
    assert updated["gate_state"] == "needs_human"
    assert updated["current_evidence_refs"] == ["new-evidence.json"]


def test_retry_build_succeeds_after_fixing_worktree_barrier(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    slice_row, _, _ = _create_recovery_slice(registry, repo_root=repo_root)
    snapshot = _slice_snapshot(slice_row)
    spec_path = Path(str(slice_row["spec"]["path"]))
    spec_path.write_text(spec_path.read_text(encoding="utf-8") + "\nretry-context\n", encoding="utf-8")
    dispatcher = Dispatcher(
        registry,
        pane_sender=None,
        worktree_creator=_RaisingWorktreeCreator(
            tmp_path / "worktrees-1",
            ValueError("existing worktree branch has commits outside requested base"),
        ),
        git_runner=_git_runner,
    )

    with pytest.raises(autonomy.DispatchReadyError):
        manager.apply_slice_action(
            dispatcher,
            slice_id="slice-a",
            action="retry-build",
            actor="operator",
            specs_dir=str(repo_root / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            launcher=_RecordingLauncher(),
            git_runner=_git_runner,
        )

    current = registry.get_slice("slice-a")
    assert current["spec"]["hash"] == snapshot["spec_hash"]
    _assert_preserved_recovery_slice(current, snapshot)
    dispatcher._worktree_creator = _WorktreeCreator(tmp_path / "worktrees-2")
    result = manager.apply_slice_action(
        dispatcher,
        slice_id="slice-a",
        action="retry-build",
        actor="operator",
        specs_dir=str(repo_root / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=_RecordingLauncher(),
        git_runner=_git_runner,
    )

    updated = registry.get_slice("slice-a")
    assert result["action"] == "retry-build"
    assert result["job_id"] == updated["builder_job_id"]
    assert updated["state"] == "building"
    assert updated["gate_state"] == "pending"
    assert updated["candidate"] is None
    assert updated["reviewer_job_id"] is None
    assert updated["current_evidence_refs"] == []
    assert updated["current_evaluation_refs"] == []


def test_initial_dispatch_failure_without_recovery_slice_still_creates_needs_human_row(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    _write_spec(repo_root, "slice-a")
    meta = _scan_slice(repo_root / "specs", "slice-a")
    dispatcher = Dispatcher(
        registry,
        pane_sender=None,
        worktree_creator=_RaisingWorktreeCreator(
            tmp_path / "worktrees",
            ValueError("existing worktree branch has commits outside requested base"),
        ),
        git_runner=_git_runner,
    )

    with pytest.raises(autonomy.DispatchReadyError):
        autonomy.dispatch_ready(
            [meta],
            is_satisfied=lambda _slice_id: True,
            dispatcher=dispatcher,
            persona="builder",
            launcher=_RecordingLauncher(),
            git_runner=_git_runner,
        )

    created = registry.get_slice("slice-a")
    assert created["state"] == "needs_human"
    assert created["gate_state"] == "needs_human"
    assert created["builder_job_id"] is None
    assert created["candidate"] is None
    assert created["dispatch_base"] == "f" * 40
    assert created["actions"][-1]["action"] == "dispatch-failed"


def test_pending_slice_launch_failure_still_repins_and_needs_human(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    pending = _create_pending_slice(registry, repo_root=repo_root)
    spec_path = Path(str(pending["spec"]["path"]))
    spec_path.write_text(spec_path.read_text(encoding="utf-8") + "\nupdated\n", encoding="utf-8")
    rewritten_hash = verification.sha256_bytes(spec_path.read_bytes())
    meta = _scan_slice(repo_root / "specs", "slice-a")
    dispatcher = Dispatcher(
        registry,
        pane_sender=None,
        worktree_creator=_RaisingWorktreeCreator(
            tmp_path / "worktrees",
            ValueError("existing worktree branch has commits outside requested base"),
        ),
        git_runner=_git_runner,
    )

    with pytest.raises(autonomy.DispatchReadyError):
        autonomy.dispatch_ready(
            [meta],
            is_satisfied=lambda _slice_id: True,
            dispatcher=dispatcher,
            persona="builder",
            launcher=_RecordingLauncher(),
            git_runner=_git_runner,
        )

    updated = registry.get_slice("slice-a")
    assert updated["state"] == "needs_human"
    assert updated["gate_state"] == "needs_human"
    assert updated["spec"]["hash"] == rewritten_hash
    assert updated["builder_job_id"] is None
    assert updated["candidate"] is None


def test_bound_launch_failed_job_still_finalizes_failed_manifest(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    _write_spec(repo_root, "slice-a")
    meta = _scan_slice(repo_root / "specs", "slice-a")
    dispatcher = Dispatcher(
        registry,
        pane_sender=None,
        worktree_creator=_WorktreeCreator(tmp_path / "worktrees"),
        git_runner=_git_runner,
    )

    with pytest.raises(autonomy.DispatchReadyError):
        autonomy.dispatch_ready(
            [meta],
            is_satisfied=lambda _slice_id: True,
            dispatcher=dispatcher,
            persona="builder",
            launcher=_RecordingLauncher(launch_exc=RuntimeError("spawn failed before attach")),
            git_runner=_git_runner,
        )

    failed_slice = registry.get_slice("slice-a")
    failed_job = registry.get_job(str(failed_slice["builder_job_id"]))
    assert failed_job["status"] == "failed"
    assert failed_job["runtime_diagnostic"]["reason"] == "launch-failed"
    handoff_dir = tmp_path / "handoff"
    summary = manager.complete_tick(dispatcher, handoff_dir=str(handoff_dir))
    manifest = json.loads((handoff_dir / "slice-a.json").read_text(encoding="utf-8"))

    assert summary["completed"] == [{"slice_id": "slice-a", "gate_status": "failed"}]
    assert manifest["job_id"] == failed_job["job_id"]
    assert manifest["gate_status"] == "failed"
    assert manifest["gate_reason"].startswith("builder-failed")
    updated = registry.get_slice("slice-a")
    assert updated["state"] == "failed"
    assert updated["gate_state"] == "failed"
