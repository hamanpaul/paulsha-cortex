"""#929：slice lane/request consumers 尚未消費 durable executor backoff 的 RED 測試。"""

from __future__ import annotations

from _pinned_spec_support import materialize_spec

import hashlib
import inspect
import json
import logging
import time
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import executor_backoff, manager, manager_daemon
from paulsha_cortex.coordinator.autonomy import dispatch_ready
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.launcher import LaunchHandle, SubprocessLauncher
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
                "timeout_seconds": 300,
            },
        ],
        "tests": [],
        "full_suite": {
            "argv": ["python3", "-m", "pytest", "-q"],
            "cwd": ".",
            "timeout_seconds": 300,
            "baseline": "no-regression",
        },
    }


def _meta(
    slice_id: str,
    *,
    executor: str | None = None,
    model_id: str | None = None,
) -> dict[str, object]:
    spec_path, spec_hash = materialize_spec(f"/specs/{slice_id}.md")
    verification = _verification_contract()
    return {
        "path": spec_path,
        "dispatch": "auto",
        "slice_id": slice_id,
        "plan": "docs/superpowers/plans/example.md",
        "depends_on": [],
        "target_branch": "main",
        "verification": verification,
        "parse_error": None,
        "executor": executor,
        "model_id": model_id,
        "_pinned_inputs": {
            "spec_path": spec_path,
            "spec_hash": spec_hash,
            "plan_path": "docs/superpowers/plans/example.md",
            "plan_hash": "1" * 64,
            "target_branch": "main",
            "target_remote": "origin",
            "verification_hash": "2" * 64,
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


def _fake_git_runner(args: list[str]):
    if not args:
        return ""
    if args[0] == "rev-parse":
        return "f" * 40
    if len(args) >= 5 and args[0] == "-C" and args[2] == "fetch":
        return ""
    if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
        return "f" * 40
    if len(args) >= 6 and args[0] == "-C" and args[2] == "merge-base":
        return ""
    return ""


def _outcome_input(outcome: str, reason: str, reset_at: float | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "outcome": outcome,
        "authority": "structured",
        "reason": reason,
        "retryable": outcome == "rate_limited",
    }
    if reset_at is not None:
        payload["reset_at"] = reset_at
    payload_bytes = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "outcome": outcome,
        "authority": "structured",
        "payload": payload,
        "payload_fingerprint": hashlib.sha256(payload_bytes).hexdigest(),
        "reason": reason,
        "policy_revision": "executor-backoff/v1",
        "rate_limited_base_seconds": 10.0,
        "quota_base_seconds": 40.0,
        "backoff_multiplier_base": 2.0,
        "backoff_max_exponent": 4,
        "reset_margin_seconds": 5.0,
        "reset_provenance": "structured" if reset_at is not None else "absent",
        "reset_parser": None,
        "evidence_ref": "fixture://executor-backoff-slice-lane",
    }


def _record_backoff(
    coordinator_root: Path,
    *,
    executor: str,
    model_id: str,
    reason: str,
    reset_at: float,
) -> float:
    now = time.time()
    result = executor_backoff.record_backoff(
        coordinator_root,
        executor,
        model_id,
        now=now,
        outcome=_outcome_input("rate_limited", reason, reset_at),
        reset_at=reset_at,
        reason=reason,
        job_id=f"{executor}-{model_id}-job",
        event_epoch=now,
    )
    assert result.observation.value == "valid"
    return reset_at + executor_backoff.RESET_MARGIN_SECONDS


class _WorktreeCreator:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self.calls: list[tuple[str, str | None, str | None]] = []

    def create(self, branch: str, base_sha: str | None = None, *, job_id: str | None = None) -> str:
        self.calls.append((branch, base_sha, job_id))
        path = self._base_dir / branch.replace("/", "__")
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


class _DispatchContext:
    def __init__(self, registry: JobRegistry, worktree_creator: _WorktreeCreator) -> None:
        self._registry = registry
        self._worktree_creator = worktree_creator
        self._git_runner = _fake_git_runner

    def poll_headless_done(self, job_id: str) -> dict[str, object]:
        return self._registry.get_job(job_id)


class _RecordingLauncher:
    def __init__(self, *, executor: str, model_id: str | None) -> None:
        self.executor = executor
        self._model = model_id
        self.calls: list[dict[str, str]] = []
        self.commit_required_calls = 0

    @property
    def model(self) -> str | None:
        return self._model

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
        return LaunchHandle(
            executor=self.executor,
            model_id=self._model,
            session_name=slice_id,
            pid=4242,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def _failed_slice_registry(state_path: Path, *, slice_id: str = "slice-a") -> JobRegistry:
    registry = JobRegistry(state_path=state_path)
    builder_job = registry.create_job(
        task=slice_id,
        persona="builder",
        branch=f"feature/{slice_id}",
        pane="",
        worktree=f"/wt/{slice_id}",
    )
    registry.create_slice(
        slice_id=slice_id,
        spec_path=f"specs/{slice_id}.md",
        spec_hash="spec-sha",
        plan_path=f"plans/{slice_id}.md",
        plan_hash="plan-sha",
        target_branch="main",
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=None,
        candidate=None,
    )
    registry.update_slice(slice_id, state="failed", gate_state="failed")
    return registry


def _backoff_skip(
    *,
    slice_id: str = "slice-a",
    executor: str = "codex",
    model_id: str = "gpt-5.4-codex",
    retry_after_epoch: float | None = 1234.0,
    reason: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "slice_id": slice_id,
        "executor": executor,
        "model_id": model_id,
        "retry_after_epoch": retry_after_epoch,
    }
    if reason is not None:
        payload["reason"] = reason
    return payload


def test_dispatch_ready_signature_exposes_keyword_only_backoff_skip_sink() -> None:
    parameter = inspect.signature(dispatch_ready).parameters.get("backoff_skips")

    assert parameter is not None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


def test_subprocess_launcher_public_model_survives_clone_helpers(tmp_path: Path) -> None:
    model_id = "gpt-5.4-codex"
    launcher = SubprocessLauncher(executor="codex", model=model_id)

    clones = [
        launcher,
        launcher.as_read_only(),
        launcher.as_review_only(terminal_kind="workflow-review-result"),
        launcher.as_verdict_spool_writer(str(tmp_path / "verdict-spool")),
        launcher.as_commit_required(),
        launcher.as_write_forbidden(),
    ]

    assert [getattr(clone, "model", None) for clone in clones] == [model_id] * len(clones)


def test_dispatch_ready_skips_known_backoff_before_pending_slice_worktree_and_launch(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    coordinator_root = tmp_path / "runtime" / "coordinator"
    retry_after_epoch = _record_backoff(
        coordinator_root,
        executor="codex",
        model_id="gpt-5.4-codex",
        reason="synthetic-rate-limit",
        reset_at=time.time() + 600.0,
    )
    registry = JobRegistry(state_path=coordinator_root / "jobs.json")
    worktree_creator = _WorktreeCreator(tmp_path / "worktrees")
    dispatcher = _DispatchContext(registry, worktree_creator)
    default_launcher = _RecordingLauncher(executor="copilot", model_id=None)

    with caplog.at_level(logging.INFO):
        jobs = dispatch_ready(
            [_meta("slice-known", executor="codex", model_id="gpt-5.4-codex")],
            is_satisfied=lambda _slice_id: True,
            dispatcher=dispatcher,
            persona="builder",
            launcher=default_launcher,
            identity_registry=_identity_registry(),
            launcher_factory=lambda identity: _RecordingLauncher(
                executor=identity.executor,
                model_id=identity.model_id,
            ),
            git_runner=_fake_git_runner,
        )

    assert jobs == []
    assert registry.list_slices() == []
    assert registry.list_jobs() == []
    assert worktree_creator.calls == []
    assert default_launcher.calls == []
    assert any(
        record.levelno == logging.INFO
        and "slice-known" in record.getMessage()
        and "codex" in record.getMessage()
        and "gpt-5.4-codex" in record.getMessage()
        and str(int(retry_after_epoch)) in record.getMessage()
        for record in caplog.records
    )


def test_dispatch_ready_uses_launcher_identity_for_unknown_store_without_side_effects(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    coordinator_root = tmp_path / "runtime" / "coordinator"
    coordinator_root.mkdir(parents=True, exist_ok=True)
    (coordinator_root / executor_backoff.STATE_FILENAME).write_text("{bad-json", encoding="utf-8")
    registry = JobRegistry(state_path=coordinator_root / "jobs.json")
    worktree_creator = _WorktreeCreator(tmp_path / "worktrees")
    dispatcher = _DispatchContext(registry, worktree_creator)
    launcher = _RecordingLauncher(executor="codex", model_id="gpt-5.4-codex")

    with caplog.at_level(logging.INFO):
        jobs = dispatch_ready(
            [_meta("slice-unknown")],
            is_satisfied=lambda _slice_id: True,
            dispatcher=dispatcher,
            persona="builder",
            launcher=launcher,
            git_runner=_fake_git_runner,
        )

    assert jobs == []
    assert registry.list_slices() == []
    assert registry.list_jobs() == []
    assert worktree_creator.calls == []
    assert launcher.calls == []
    assert any(
        record.levelno == logging.INFO
        and "slice-unknown" in record.getMessage()
        and "codex" in record.getMessage()
        and "gpt-5.4-codex" in record.getMessage()
        and "unknown" in record.getMessage()
        for record in caplog.records
    )


def test_run_tick_surfaces_dispatch_skipped_by_backoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    expected_skip = _backoff_skip()
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    dispatcher = _DispatchContext(registry, _WorktreeCreator(tmp_path / "worktrees"))

    def fake_dispatch_ready(
        metas,
        predicate,
        dispatcher_arg,
        *,
        backoff_skips=None,
        **_kwargs,
    ):
        assert metas[0]["slice_id"] == "slice-a"
        assert dispatcher_arg is dispatcher
        if backoff_skips is not None:
            backoff_skips.append(dict(expected_skip))
        return []

    monkeypatch.setattr(manager.autonomy, "dispatch_ready", fake_dispatch_ready)

    result = manager.run_tick(
        dispatcher,
        metas=[{"slice_id": "slice-a", "dispatch": "auto", "plan": "plans/slice-a.md", "depends_on": []}],
        launcher=object(),
        is_satisfied=lambda _slice_id: True,
        handoff_dir=str(tmp_path / "handoff"),
        clock=lambda: "T0",
    )

    assert result["dispatched"] == []
    assert result["dispatch_skipped_by_backoff"] == [expected_skip]


def test_retry_build_returns_dispatch_skipped_by_backoff_instead_of_raising(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime" / "coordinator" / "jobs.json"
    registry = _failed_slice_registry(state_path)
    dispatcher = Dispatcher(registry, pane_sender=None, worktree_creator=None)
    expected_skip = _backoff_skip()

    def fake_dispatch_ready(
        metas,
        predicate,
        dispatcher_arg,
        *,
        backoff_skips=None,
        **_kwargs,
    ):
        assert metas[0]["slice_id"] == "slice-a"
        assert dispatcher_arg is dispatcher
        if backoff_skips is not None:
            backoff_skips.append(dict(expected_skip))
        return []

    result = manager.apply_slice_action(
        dispatcher,
        slice_id="slice-a",
        action="retry-build",
        actor="operator",
        specs_dir="specs",
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        scan_specs_fn=lambda _specs_dir: [
            {
                "slice_id": "slice-a",
                "dispatch": "auto",
                "plan": "plans/slice-a.md",
                "depends_on": [],
            }
        ],
        dispatch_ready_fn=fake_dispatch_ready,
    )

    assert result["slice_id"] == "slice-a"
    assert result["action"] == "retry-build"
    assert result["dispatch_skipped_by_backoff"] == [expected_skip]


def test_retry_build_request_uses_spec_identity_launcher_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path))
    state_path = tmp_path / "runtime" / "coordinator" / "jobs.json"
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    plan_path = tmp_path / "docs" / "superpowers" / "plans" / "example.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("plan\n", encoding="utf-8")
    (specs_dir / "slice-a.md").write_text(
        "---\n"
        "dispatch: auto\n"
        "slice_id: slice-a\n"
        "plan: docs/superpowers/plans/example.md\n"
        "target_branch: main\n"
        "executor: codex\n"
        "model_id: gpt-5.4-codex\n"
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
        "      timeout_seconds: 300\n"
        "  tests: []\n"
        "  full_suite:\n"
        "    argv: [python3, -m, pytest, -q]\n"
        "    cwd: .\n"
        "    timeout_seconds: 300\n"
        "    baseline: no-regression\n"
        "---\n\n"
        "body\n",
        encoding="utf-8",
    )
    registry = _failed_slice_registry(state_path)
    registry.update_status(str(registry.get_slice("slice-a")["builder_job_id"]), "exited")
    dispatcher = _DispatchContext(registry, _WorktreeCreator(tmp_path / "worktrees"))
    resolve_calls: list[tuple[str | None, str | None]] = []
    created_launchers: list[_RecordingLauncher] = []

    def fake_resolve(executor, injected, *, allow_unsafe, model):
        resolve_calls.append((executor, model))
        if executor is None and model is None:
            return injected
        launcher = _RecordingLauncher(executor=executor or "copilot", model_id=model)
        created_launchers.append(launcher)
        return launcher

    monkeypatch.setattr(manager_daemon, "_resolve_launcher", fake_resolve)

    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(specs_dir),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        workflow_identity_registry=_identity_registry(),
    )

    result = request_executor(
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

    relaunched_job = registry.get_job(str(result["job_id"]))
    identity_launcher = next(
        launcher
        for launcher in created_launchers
        if launcher.executor == "codex" and launcher.model == "gpt-5.4-codex"
    )

    assert result["slice_id"] == "slice-a"
    assert result["action"] == "retry-build"
    assert ("codex", "gpt-5.4-codex") in resolve_calls
    assert relaunched_job["executor"] == "codex"
    assert relaunched_job["model_id"] == "gpt-5.4-codex"
    assert identity_launcher.commit_required_calls == 1
    assert identity_launcher.calls and identity_launcher.calls[0]["slice_id"] == "slice-a"


def test_dispatch_request_returns_backoff_skip_instead_of_index_error(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    dispatcher = Dispatcher(registry, pane_sender=None, worktree_creator=None)
    expected_skip = _backoff_skip()

    def fake_dispatch_ready(
        metas,
        predicate,
        dispatcher_arg,
        *,
        backoff_skips=None,
        **_kwargs,
    ):
        assert metas[0]["slice_id"] == "slice-a"
        assert dispatcher_arg is dispatcher
        if backoff_skips is not None:
            backoff_skips.append(dict(expected_skip))
        return []

    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        scan_specs_fn=lambda _specs_dir: [_meta("slice-a")],
        dispatch_ready_fn=fake_dispatch_ready,
    )

    result = request_executor(
        {
            "type": "dispatch",
            "args": {"slice_id": "slice-a"},
            "requested_by": "operator",
        }
    )

    assert result == {
        "slice_id": "slice-a",
        "dispatch_skipped_by_backoff": [expected_skip],
    }


def test_fanout_request_surfaces_dispatch_skipped_by_backoff(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    dispatcher = Dispatcher(registry, pane_sender=None, worktree_creator=None)
    expected_skip = _backoff_skip(reason="executor-backoff-store-unknown", retry_after_epoch=None)

    def fake_dispatch_ready(
        metas,
        predicate,
        dispatcher_arg,
        *,
        backoff_skips=None,
        **_kwargs,
    ):
        assert [meta["slice_id"] for meta in metas] == ["slice-a"]
        assert dispatcher_arg is dispatcher
        if backoff_skips is not None:
            backoff_skips.append(dict(expected_skip))
        return []

    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        scan_specs_fn=lambda _specs_dir: [_meta("slice-a")],
        dispatch_ready_fn=fake_dispatch_ready,
    )

    result = request_executor(
        {
            "type": "fanout",
            "args": {},
            "requested_by": "operator",
        }
    )

    assert result["dispatch_skipped"] is False
    assert result["dispatched"] == []
    assert result["dispatch_skipped_by_backoff"] == [expected_skip]
