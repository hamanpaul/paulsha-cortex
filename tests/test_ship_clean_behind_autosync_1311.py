from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import (
    gate_ledger,
    manager,
    runtime_preflight,
    terminal_contract,
    work_bridge,
    work_actions,
)
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep
from paulsha_cortex.monitor.providers import WorkflowRegistryProvider
from paulsha_cortex.monitor.work_api import WorkReadModelStore

from git_fixtures import make_job_clone
from test_preflight_closeout_order import _ship_harness
from test_monitor_work_api import _item, _snapshot
from test_main_probe_gate_987 import (
    _advance_origin_main,
    _advance_origin_with,
    _wire_local_origin,
    _git,
)


class _ShipWorkspaceCreator:
    def __init__(self, repo: Path, root: Path) -> None:
        self.repo = repo
        self.root = root
        self.created: list[Path] = []

    def create(self, branch: str, *, job_id: str, base_sha: str) -> Path:
        target = self.root / job_id
        workspace = make_job_clone(self.repo, target, branch=branch)
        self.created.append(workspace)
        assert _git(workspace, "rev-parse", "HEAD") == base_sha
        return workspace


class _VerificationLauncher:
    def __init__(self, executor: str, model_id: str) -> None:
        self.executor = executor
        self.model_id = model_id
        self.calls: list[dict[str, str]] = []

    def as_read_only(self):
        return self

    def as_review_only(self, *, terminal_kind: str):
        return self

    def executor_environment(self) -> runtime_preflight.ExecutorEnvironment:
        return runtime_preflight.ExecutorEnvironment(
            name=f"{self.executor}-workflow",
            interpreter=(sys.executable,),
            path=os.environ.get("PATH", ""),
            home=os.path.expanduser("~"),
            provider_identity=self.executor,
        )

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        self.calls.append({"slice_id": slice_id, "prompt": prompt, "worktree": worktree})
        return LaunchHandle(
            executor=self.executor,
            model_id=self.model_id,
            session_name=slice_id,
            pid=4242,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


def _seed_candidate_bound_build_ledger(
    harness,
) -> tuple[dict[str, object], dict[str, object]]:
    """Seed a valid build ledger so autosync must reject it for the new Candidate."""

    build_job = next(
        job
        for job in harness.registry.list_jobs()
        if job.get("workflow_run_id") == harness.run_id
        and job.get("workflow_phase") == "build"
        and job.get("persona") == "builder"
    )
    log_path = harness.state_root / "logs" / "original-build.jsonl"
    build_job = {
        **build_job,
        "log_path": str(log_path),
        "workflow_test_policy": "focused",
    }
    payload = gate_ledger.build_ledger(
        [
            {
                "name": terminal_contract.RED_REQUIRED_TEST_GATE_NAME,
                "command": "pytest -q",
                "exit_code": 0,
                "status": "passed",
            }
        ],
        slice_id=str(build_job["job_id"]),
        worktree_state={"probe": "ok", "head": harness.candidate.lower()},
    )
    gate_ledger.write_ledger_payload(
        terminal_contract.gate_ledger_path(log_path), payload
    )
    context = manager._verification_gate_ledger_context(harness.run, build_job)
    assert context is not None
    return build_job, context


class _IdentitylessShipWorkspaceCreator(_ShipWorkspaceCreator):
    def create(self, branch: str, *, job_id: str, base_sha: str) -> Path:
        workspace = super().create(branch, job_id=job_id, base_sha=base_sha)
        for key in ("user.name", "user.email"):
            subprocess.run(
                ["git", "-C", str(workspace), "config", "--local", "--unset-all", key],
                check=True,
                capture_output=True,
                text=True,
            )
        return workspace


def _production_validator(harness, tmp_path: Path, *, creator, probe_runner=subprocess.run):
    return work_bridge.build_production_ship_validator(
        registry=harness.registry,
        coordinator_root=harness.state_root,
        snapshot_path=harness.snapshot,
        runner=harness.runner,
        probe_runner=probe_runner,
        workspace_creator=creator,
    )


def _advance_to_ship(harness, validator) -> dict[str, object]:
    run = harness.run
    review_step = next(step for step in run.steps if step.phase == "review")
    return manager.apply_workflow_action(
        harness.registry,
        args={
            "action": "advance",
            "run_id": run.run_id,
            "card_id": review_step.card,
            "current_phase": "ship",
        },
        identity_registry=IdentityRegistry.from_rows([]),
        ship_validator=validator,
        coordinator_root=harness.state_root,
        trusted_terminal=True,
    )


def test_clean_behind_main_is_merged_by_manager_and_only_verify_is_reopened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    initial = harness.run
    initial_steps = tuple(
        replace(step, card="verification") if step.phase == "verify" else step
        for step in initial.steps
    )
    harness.registry._manager_update_workflow_run(
        initial.run_id,
        steps=initial_steps,
    )
    origin = _wire_local_origin(harness, tmp_path / "origin")
    main_head = _advance_origin_main(origin, tmp_path / "main-advance")
    creator = _ShipWorkspaceCreator(harness.repo, tmp_path / "ship-workspaces")
    validator = _production_validator(harness, tmp_path, creator=creator)
    before_jobs = harness.registry.list_jobs()
    original_builder, original_gate_ledger = _seed_candidate_bound_build_ledger(harness)

    result = _advance_to_ship(harness, validator)

    updated = harness.run
    assert result["reason"] == "main-sync-autosync-reverify"
    assert result["current_phase"] == "verify"
    assert result["main_sync_autosync"]["count"] == 1
    assert updated.current_phase == "verify"
    assert updated.candidate_head != harness.candidate
    assert updated.verified_head is None
    assert "needs_human" not in updated.facets
    assert all(step.gate_result == "pending" for step in updated.steps if step.phase == "verify")
    assert all(step.gate_result == "passed" for step in updated.steps if step.phase == "review")
    assert any("main-sync-probe" in ref for ref in updated.evidence_refs)
    autosync_ref = next(ref for ref in updated.evidence_refs if "/main-sync-autosync/" in ref)
    envelope = json.loads(Path(autosync_ref).read_text(encoding="utf-8"))
    event = envelope["payload"]
    assert event["outcome"] == "merged"
    assert event["candidate_before"] == harness.candidate
    assert event["main_head"] == main_head
    assert event["candidate_after"] == updated.candidate_head
    assert envelope["hash"] == work_bridge.verification.canonical_json_hash(event)
    provider = WorkflowRegistryProvider(
        updated.repo,
        state_path=harness.registry._state_path,
    ).scan()
    projection = provider.observations["main_sync_autosync"][updated.work_id]
    assert projection["count"] == 1
    shown = WorkReadModelStore(
        _snapshot(
            _item(updated.work_id, "ongoing", repo=updated.repo),
            providers={provider.provider_id: provider},
        )
    ).get_work_item(updated.work_id, repo=updated.repo)
    assert shown["main_sync_autosync"] == projection
    parents = _git(harness.repo, "rev-list", "--parents", "-n", "1", updated.candidate_head).split()
    assert parents == [updated.candidate_head, harness.candidate, main_head]
    after_jobs = harness.registry.list_jobs()
    new_jobs = [job for job in after_jobs if job["job_id"] not in {row["job_id"] for row in before_jobs}]
    assert [(job["workflow_phase"], job["workflow_card"]) for job in new_jobs] == [
        ("ship", "main-sync-autosync")
    ]
    assert not any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert not harness.runner.saw_push()
    assert not harness.runner.saw_gh()

    autosync_job = next(
        job for job in new_jobs if job["workflow_card"] == "main-sync-autosync"
    )

    monkeypatch.setattr(manager, "_EXECUTOR_AUTH_CACHE", {})
    manager._EXECUTOR_AUTH_CACHE["claude"] = runtime_preflight.ProviderFreshness(
        provider_id="claude",
        status="ok",
        observed_at=time.time(),
        ttl_seconds=900.0,
        source="snapshot",
    )
    monkeypatch.setattr(
        manager,
        "_validated_brainstorm_planning_authority",
        lambda bound_run, **_kwargs: (
            bound_run.planning_authority,
            bound_run.planning_source_revision,
        ),
    )
    current = harness.registry.get_workflow_run(updated.run_id)
    dispatcher = SimpleNamespace(
        _registry=harness.registry,
        _git_runner=None,
        poll_headless_done=lambda job_id: harness.registry.get_job(job_id),
    )
    launcher = _VerificationLauncher("claude", "sonnet-main-sync")

    tick = manager.resume_workflow_run(
        dispatcher,
        run_id=current.run_id,
        identities=IdentityRegistry.from_rows(
            [
                {
                    "executor": "claude",
                    "model_id": "sonnet-main-sync",
                    "independence_domain": "anthropic",
                    "capabilities": ["review"],
                }
            ]
        ),
        launcher_factory=lambda _identity: launcher,
        coordinator_root=harness.state_root,
    )

    assert tick["reason"] == "in-flight"
    verify_job = harness.registry.get_job(str(tick["job_id"]))
    verify_step = next(step for step in current.steps if step.phase == "verify")
    builder_jobs, builder_job_id, manager_gate_ledger = (
        manager._workflow_stage_execution_builder_context(
            current, verify_step, harness.registry
        )
    )
    # The previous build ledger is valid for the pre-sync candidate only. The verify card
    # must run its own checks against the merged candidate instead of inheriting stale gate
    # evidence whose worktree_state.head still names the pre-sync commit.
    assert original_gate_ledger["candidate"] == harness.candidate
    assert manager._verification_gate_ledger_context(current, original_builder) is None
    assert [job["workflow_card"] for job in builder_jobs] == ["main-sync-autosync"]
    assert builder_job_id == autosync_job["job_id"]
    assert builder_jobs[-1]["branch"] == "feature/14-work"
    assert manager._workflow_build_handoff_base(
        current, builder_jobs=builder_jobs, card="post-sync-build"
    ) == current.candidate_head
    assert manager_gate_ledger is None
    assert verify_job["workflow_card"] == "verification"
    assert verify_job["subject_head"] == updated.candidate_head
    assert verify_job["workflow_builder_job_id"] == autosync_job["job_id"]
    assert verify_job["branch"] == autosync_job["branch"]
    assert launcher.calls
    assert original_gate_ledger["sha256"] not in launcher.calls[0]["prompt"]


def test_autosync_real_git_merge_uses_source_identity_when_ship_clone_has_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    origin = _wire_local_origin(harness, tmp_path / "origin")
    main_head = _advance_origin_main(origin, tmp_path / "main-advance")

    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        "[user]\n"
        "\tname = Source Global Identity\n"
        "\temail = source-global@example.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("user.name", "user.email"):
        subprocess.run(
            ["git", "-C", str(harness.repo), "config", "--local", "--unset-all", key],
            check=True,
            capture_output=True,
            text=True,
        )

    creator = _IdentitylessShipWorkspaceCreator(harness.repo, tmp_path / "ship-workspaces")
    validator = _production_validator(harness, tmp_path, creator=creator)

    result = _advance_to_ship(harness, validator)

    assert result["main_sync_autosync"]["outcome"] == "merged"
    assert result["main_sync_autosync"]["main_head"] == main_head
    workspace = creator.created[0]
    commit_identity = _git(
        workspace,
        "show",
        "-s",
        "--format=%an%n%ae%n%cn%n%ce",
        result["candidate_head"],
    ).splitlines()
    assert commit_identity == [
        "Source Global Identity",
        "source-global@example.invalid",
        "Source Global Identity",
        "source-global@example.invalid",
    ]
    for key in ("user.name", "user.email"):
        probe = subprocess.run(
            ["git", "-C", str(workspace), "config", "--local", "--get", key],
            check=False,
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 1


def test_autosync_fails_closed_when_no_effective_identity_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    origin = _wire_local_origin(harness, tmp_path / "origin")
    _advance_origin_main(origin, tmp_path / "main-advance")

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("PSC_MAIN_SYNC_GIT_IDENTITY", raising=False)
    for key in ("user.name", "user.email"):
        subprocess.run(
            ["git", "-C", str(harness.repo), "config", "--local", "--unset-all", key],
            check=True,
            capture_output=True,
            text=True,
        )

    creator = _IdentitylessShipWorkspaceCreator(harness.repo, tmp_path / "ship-workspaces")
    validator = _production_validator(harness, tmp_path, creator=creator)

    result = _advance_to_ship(harness, validator)

    assert result["main_sync_autosync"]["outcome"] == "merge-failed"
    assert result["reason"] == "main-sync-identity-missing"
    assert harness.run.candidate_head == harness.candidate
    assert "needs_human" in harness.run.facets


def test_configured_autosync_identity_takes_precedence_and_must_be_well_formed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PSC_MAIN_SYNC_GIT_IDENTITY",
        "Configured Identity <configured@example.invalid>",
    )
    assert work_bridge._main_sync_autosync_git_identity(tmp_path) == (
        "Configured Identity",
        "configured@example.invalid",
    )

    monkeypatch.setenv("PSC_MAIN_SYNC_GIT_IDENTITY", "malformed identity")
    assert work_bridge._main_sync_autosync_git_identity(tmp_path) is None


def test_autosync_limit_counts_successful_syncs_but_not_failed_merges() -> None:
    events = [
        {"outcome": "merge-failed"},
        {"outcome": "merged"},
        {"outcome": "harvest-failed"},
    ]

    assert work_bridge._main_sync_autosync_completed_count(events) == 1


def test_main_sync_autosync_limit_stops_with_a_retryable_reason_and_showable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PSC_MAIN_SYNC_AUTOSYNC_MAX", "1")
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    origin = _wire_local_origin(harness, tmp_path / "origin")
    _advance_origin_main(origin, tmp_path / "main-advance-1")
    creator = _ShipWorkspaceCreator(harness.repo, tmp_path / "ship-workspaces")
    validator = _production_validator(harness, tmp_path, creator=creator)

    first = _advance_to_ship(harness, validator)
    updated = harness.run
    verify_steps = tuple(
        WorkflowStep.from_dict({**step.to_dict(), "gate_result": "passed"})
        if step.phase == "verify"
        else step
        for step in updated.steps
    )
    updated = harness.registry._manager_update_workflow_run(
        updated.run_id,
        current_phase="review",
        steps=verify_steps,
        verified_head=updated.candidate_head,
    )
    second_main = _advance_origin_with(
        origin,
        tmp_path / "main-advance-2",
        files={"UPSTREAM2.md": "second main movement\n"},
        message="second main movement",
    )

    second = _advance_to_ship(harness, validator)

    stopped = harness.run
    assert first["reason"] == "main-sync-autosync-reverify"
    assert second["reason"] == "main-moving-too-fast"
    assert second["main_sync_autosync"]["outcome"] == "limit-exceeded"
    assert second["main_sync_autosync"]["count"] == 1
    assert second["main_sync_autosync"]["limit"] == 1
    assert stopped.current_phase == "review"
    assert stopped.candidate_head == updated.candidate_head
    assert "needs_human" in stopped.facets
    assert second_main == second["main_sync_autosync"]["main_head"]
    assert work_actions._main_sync_retry_context(stopped) is not None
    attempt_ref = second["main_sync_autosync"]["evidence_ref"]
    attempt = json.loads(Path(attempt_ref).read_text(encoding="utf-8"))["payload"]
    assert attempt["outcome"] == "limit-exceeded"
    assert attempt_ref in stopped.evidence_refs


def test_main_moving_during_sync_returns_to_needs_human_with_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    origin = _wire_local_origin(harness, tmp_path / "origin")
    _advance_origin_main(origin, tmp_path / "main-advance-1")
    calls = 0
    late_main: str | None = None

    def advance_before_autosync_fetch(args, **kwargs):
        nonlocal calls, late_main
        if isinstance(args, (list, tuple)) and "fetch" in args:
            calls += 1
            if calls == 2:
                late_main = _advance_origin_with(
                    origin,
                    tmp_path / "main-advance-2",
                    files={"UPSTREAM2.md": "main moved during autosync\n"},
                    message="main moved during autosync",
                )
        return subprocess.run(args, **kwargs)

    creator = _ShipWorkspaceCreator(harness.repo, tmp_path / "ship-workspaces")
    validator = _production_validator(
        harness,
        tmp_path,
        creator=creator,
        probe_runner=advance_before_autosync_fetch,
    )

    result = _advance_to_ship(harness, validator)

    stopped = harness.run
    assert result["reason"] == "candidate-behind-main"
    assert result["main_sync_autosync"]["outcome"] == "main-advanced-during-sync"
    assert result["main_sync_autosync"]["observed_main_head"] == late_main
    assert stopped.candidate_head == harness.candidate
    assert "needs_human" in stopped.facets
    assert result["main_sync_autosync"]["evidence_ref"] in stopped.evidence_refs
    assert not harness.runner.saw_push()
    assert not harness.runner.saw_gh()


def test_conflict_does_not_enter_clean_behind_autosync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, main_files, expected_reason in (
        ("conflict", {"README.md": "main change\n"}, "candidate-conflicts-with-main"),
    ):
        case_root = tmp_path / name
        case_root.mkdir(parents=True)
        harness = _ship_harness(
            case_root,
            monkeypatch,
            active_change=False,
            archived_change=True,
            probe_runner=subprocess.run,
        )
        default_branch = _git(harness.repo, "branch", "--show-current")
        _git(harness.repo, "checkout", "--quiet", "feature/14-work")
        (harness.repo / "README.md").write_text("feature change\n", encoding="utf-8")
        _git(harness.repo, "add", "README.md")
        _git(harness.repo, "commit", "-qm", "feature readme")
        candidate = _git(harness.repo, "rev-parse", "HEAD")
        _git(harness.repo, "checkout", "--quiet", default_branch)
        run = harness.registry._manager_update_workflow_run(
            harness.run_id,
            candidate_head=candidate,
            verified_head=candidate,
        )
        from test_preflight_closeout_order import _seed_foreign_review

        _seed_foreign_review(
            registry=harness.registry,
            run=run,
            repo=harness.repo,
            candidate=candidate,
            state_root=harness.state_root,
            worktree=harness.worktree,
        )
        origin = _wire_local_origin(harness, case_root / "origin")
        _advance_origin_with(
            origin,
            case_root / "main-advance",
            files=main_files,
            message="main conflict",
        )
        creator = _ShipWorkspaceCreator(harness.repo, case_root / "ship-workspaces")
        validator = _production_validator(harness, case_root, creator=creator)

        result = _advance_to_ship(harness, validator)

        assert result["reason"] == expected_reason
        assert harness.run.candidate_head == candidate
        assert "needs_human" in harness.run.facets
        assert not any("main-sync-autosync" in ref for ref in harness.run.evidence_refs)
        assert not any(call and call[0] == "preflight" for call in harness.runner.calls)
        assert not harness.runner.saw_push()
        assert not harness.runner.saw_gh()


def test_main_probe_failure_keeps_the_existing_manual_stop_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    _wire_local_origin(harness, tmp_path / "origin")

    def fail_main_fetch(args, **kwargs):
        if isinstance(args, (list, tuple)) and "fetch" in args:
            return SimpleNamespace(returncode=1, stdout="", stderr="forced fetch failure")
        return subprocess.run(args, **kwargs)

    creator = _ShipWorkspaceCreator(harness.repo, tmp_path / "ship-workspaces")
    validator = _production_validator(
        harness,
        tmp_path,
        creator=creator,
        probe_runner=fail_main_fetch,
    )

    result = _advance_to_ship(harness, validator)

    assert result["reason"] == "main-sync-unavailable"
    assert harness.run.candidate_head == harness.candidate
    assert "needs_human" in harness.run.facets
    assert not any("main-sync-autosync" in ref for ref in harness.run.evidence_refs)
    assert not any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert not harness.runner.saw_push()
    assert not harness.runner.saw_gh()
