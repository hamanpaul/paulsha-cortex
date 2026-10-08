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
    review,
    runtime_preflight,
    terminal_contract,
    work_bridge,
    work_actions,
)
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.workflow import GateEvidenceRef, WorkflowStep
from paulsha_cortex.monitor.providers import WorkflowRegistryProvider
from paulsha_cortex.monitor.work_api import WorkReadModelStore

from diagnostic_fixtures import fixture_needs_human_reason
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

    def as_commit_required(self):
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


class _CandidateCheckingBuildWorkspaceCreator:
    def __init__(self, repo: Path, root: Path) -> None:
        self.repo = repo
        self.root = root
        self.calls: list[tuple[str, str, str]] = []

    def create(self, branch: str, *, job_id: str, base_sha: str, **_kwargs) -> Path:
        target = self.root / job_id
        workspace = make_job_clone(self.repo, target, branch=branch)
        actual_head = _git(workspace, "rev-parse", "HEAD")
        self.calls.append((branch, base_sha, actual_head))
        if actual_head != base_sha:
            raise ValueError("existing worktree branch has commits outside requested base")
        return workspace


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


def _advance_to_ship(
    harness,
    validator,
    *,
    job_id: str | None = None,
    identities: IdentityRegistry | None = None,
) -> dict[str, object]:
    run = harness.run
    review_step = next(step for step in run.steps if step.phase == "review")
    args = {
        "action": "advance",
        "run_id": run.run_id,
        "card_id": review_step.card,
        "current_phase": "ship",
    }
    if job_id is not None:
        args["job_id"] = job_id
    return manager.apply_workflow_action(
        harness.registry,
        args=args,
        identity_registry=identities or IdentityRegistry.from_rows([]),
        ship_validator=validator,
        coordinator_root=harness.state_root,
        trusted_terminal=True,
    )


def test_clean_behind_main_reopens_verify_and_review_for_new_candidate(
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
    assert all(step.gate_result == "pending" for step in updated.steps if step.phase == "review")
    assert not any(ref.kind == "foreign-review" for ref in updated.gate_refs)
    assert updated.attempts["review"] == initial.attempts.get("review", 0) + 1
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
    bound_source, autosync_author = manager._review_builder_job_binding(
        harness.registry,
        run=current,
        builder_job_id=autosync_job["job_id"],
        candidate=updated.candidate_head,
    )
    assert bound_source["job_id"] == autosync_job["job_id"]
    assert autosync_author is True


def _seed_review_for_candidate_source(
    harness,
    *,
    candidate: str,
    source_job: dict[str, object],
) -> GateEvidenceRef:
    run = harness.registry.get_workflow_run(harness.run_id)
    review_step = next(step for step in run.steps if step.phase == "review")
    reviewer_job = harness.registry.create_job(
        task=f"wf-{run.run_id}-review-{candidate[:12]}",
        persona="reviewer",
        kind="review",
        branch=str(source_job["branch"]),
        pane="",
        worktree=str(source_job["worktree"]),
        executor="claude",
        model_id="sonnet-review",
        independence_domain="anthropic",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=review_step.card,
        workflow_phase="review",
        workflow_repo_root=str(source_job["workflow_repo_root"]),
        source_revision=run.source_revision,
    )
    harness.registry.update_headless_result(
        reviewer_job["job_id"], status="exited", exit_code=0
    )
    evaluation = review.build_gate_evaluation(
        slice_id=f"{run.run_id}-{review_step.card}",
        state="passed",
        reason="accepted",
        builder_job_id=str(source_job["job_id"]),
        reviewer_job_id=str(reviewer_job["job_id"]),
        candidate=candidate,
        launch_identity={
            "builder": {
                "executor": str(source_job["executor"]),
                "model_id": str(source_job["model_id"]),
                "independence_domain": str(source_job["independence_domain"]),
            },
            "reviewer": {
                "executor": "claude",
                "model_id": "sonnet-review",
                "independence_domain": "anthropic",
            },
        },
    )
    bound_reviewer = harness.registry.get_job(reviewer_job["job_id"])
    envelope = {
        "schema_version": 1,
        "kind": "review",
        "job": {
            "job_id": bound_reviewer["job_id"],
            "run_id": run.run_id,
            "claim_key": run.claim_key,
            "repo": run.repo,
            "source_revision": run.source_revision,
            "card_id": review_step.card,
            "phase": "review",
            "inputs": [],
            "outputs": [],
            "output_baseline": [],
        },
        "payload": evaluation,
        "artifacts": [],
    }
    content = (
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    digest = work_bridge.hashlib.sha256(content).hexdigest()
    relative = Path("evidence") / "workflow" / f"{digest}.json"
    target = harness.state_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    harness.registry.bind_workflow_evidence(
        str(reviewer_job["job_id"]),
        locator={"kind": "review", "path": relative.as_posix(), "hash": digest},
        subject_head=candidate,
    )
    return GateEvidenceRef("foreign-review", str(target), digest)


@pytest.mark.parametrize("route", ["review-after-sync", "old-review-invalidated"])
def test_autosync_review_routes_reach_exact_candidate_merge_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    from paulsha_cortex.coordinator.github_delivery import (
        COPILOT_REVIEWER_LOGIN,
        CopilotReview,
        DeliveryFacts,
        GitHubCheck,
        MergeStatus,
    )

    import test_preflight_closeout_order as closeout_order

    original_snapshot = closeout_order._snapshot

    def snapshot_with_pr(path: Path, **_kwargs) -> Path:
        snapshot = original_snapshot(path, mapped_prs=(17,))
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        item = payload["work_items"][0]
        item["source_revisions"].append("github_pr:acme/demo#17@pr-1")
        snapshot.write_text(json.dumps(payload), encoding="utf-8")
        return snapshot

    monkeypatch.setattr(closeout_order, "_snapshot", snapshot_with_pr)
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    harness.registry._manager_update_workflow_run(
        harness.run_id,
        pr_refs=("acme/demo#17",),
    )
    origin = _wire_local_origin(harness, tmp_path / "origin")
    _advance_origin_main(origin, tmp_path / "main-advance")
    creator = _ShipWorkspaceCreator(harness.repo, tmp_path / "ship-workspaces")
    monkeypatch.setattr(work_bridge, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_bridge,
        "run_preflight",
        lambda **_kwargs: SimpleNamespace(
            passed=True,
            failed_stage=None,
            policy=SimpleNamespace(argv=("policy",), returncode=0),
            ci_parity=SimpleNamespace(argv=("preflight",), returncode=0),
            head=harness.registry.get_workflow_run(harness.run_id).candidate_head,
            tree_hash="5" * 40,
        ),
    )
    validator = work_bridge.build_production_ship_validator(
        registry=harness.registry,
        coordinator_root=harness.state_root,
        snapshot_path=harness.snapshot,
        runner=harness.runner,
        now=lambda: 200.0,
        workspace_creator=creator,
        probe_runner=subprocess.run,
    )
    sync = _advance_to_ship(harness, validator)
    run = harness.registry.get_workflow_run(harness.run_id)
    assert sync["reason"] == "main-sync-autosync-reverify"
    assert all(step.gate_result == "pending" for step in run.steps if step.phase == "review")
    assert not any(ref.kind == "foreign-review" for ref in run.gate_refs)
    if route == "old-review-invalidated":
        assert run.candidate_head != harness.candidate
        assert all(step.gate_result == "pending" for step in run.steps if step.phase == "verify")
        with pytest.raises(
            RuntimeError,
            match="delivery requires one canonical review evidence job",
        ):
            work_bridge._workflow_evidence_envelope(
                registry=harness.registry,
                state_root=harness.state_root,
                run=run,
                phase="review",
                expected_candidate=run.candidate_head,
            )

    candidate = str(run.candidate_head)
    source_job = next(
        job
        for job in harness.registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_card") == "main-sync-autosync"
    )
    fresh_review = _seed_review_for_candidate_source(
        harness,
        candidate=candidate,
        source_job=source_job,
    )
    run = harness.registry._manager_update_workflow_run(
        run.run_id,
        current_phase="review",
        verified_head=candidate,
        steps=tuple(
            replace(step, gate_result="passed")
            if step.phase in {"verify", "review"}
            else step
            for step in run.steps
        ),
        gate_refs=(fresh_review,),
        pr_refs=(f"{run.repo}#17",),
    )
    authority = work_actions.load_work_authority(
        repo=run.repo,
        work_id=run.work_id,
        snapshot_path=harness.snapshot,
    )
    work_actions._load_work_run(
        state_path=harness.state_root / "delivery-journal.json",
        workflow_registry=harness.registry,
        authority=authority,
    )

    monkeypatch.setattr(
        work_bridge,
        "run_preflight",
        lambda **_kwargs: SimpleNamespace(
            passed=True,
            failed_stage=None,
            policy=SimpleNamespace(argv=("policy",), returncode=0),
            ci_parity=SimpleNamespace(argv=("preflight",), returncode=0),
            head=candidate,
            tree_hash="5" * 40,
        ),
    )
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **_kwargs: SimpleNamespace(
            passed=True,
            failed_stage=None,
            policy=SimpleNamespace(argv=("policy",), returncode=0),
            ci_parity=SimpleNamespace(argv=("preflight",), returncode=0),
            head=candidate,
            tree_hash="5" * 40,
        ),
    )
    merge_state = {"merged": False}

    class _DeliveryGitHub:
        def __init__(self, *, runner) -> None:
            pass

        def ensure_pr_metadata(self, **_kwargs) -> None:
            return None

        def fetch_delivery_facts(self, **_kwargs):
            return DeliveryFacts(
                head=candidate,
                mergeable=True,
                mergeable_state="clean",
                checks=(GitHubCheck("pytest", "completed", "success"),),
                copilot_reviews=(
                    CopilotReview(
                        review_id=9,
                        commit_id=candidate,
                        state="COMMENTED",
                        body="approved",
                        author=COPILOT_REVIEWER_LOGIN,
                        submitted_at_epoch=199.0,
                    ),
                ),
                review_threads=(),
                closing_issues=(14,),
                active_openspec_absent=True,
                archive_present=True,
            )

        def fetch_merge_status(self, **_kwargs):
            return MergeStatus(
                merged=merge_state["merged"],
                pr_head=candidate,
                merge_commit="c" * 40 if merge_state["merged"] else None,
            )

    class _MergeOrchestrator:
        def __init__(self, *, github, now) -> None:
            pass

        def merge_if_ready(self, **kwargs):
            assert kwargs["expected_head"] == candidate
            review_payload = json.loads(
                Path(kwargs["foreign_review"].path).read_text(encoding="utf-8")
            )
            assert review_payload["candidate"] == candidate
            merge_state["merged"] = True
            return SimpleNamespace(
                expected_head=candidate,
                expected_tree_hash="5" * 40,
            )

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", _DeliveryGitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", _MergeOrchestrator)
    monkeypatch.setattr(
        work_actions,
        "_canonical_repo_root",
        lambda value, repo: Path(str(value)),
    )
    result = validator(run=run, candidate=candidate)

    assert result["status"] == "pending"
    assert merge_state["merged"] is True
    journal = json.loads(
        (harness.state_root / "delivery-journal.json").read_text(encoding="utf-8")
    )
    ship = journal["runs"][run.run_id]["ship"]
    assert ship["phase"] == "merged"
    assert ship["head"] == candidate
    assert ship["merge_authorization"]["payload"]["head"] == candidate


def test_retry_build_after_autosync_uses_autosynced_candidate_branch_and_base(
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
    ship_creator = _ShipWorkspaceCreator(harness.repo, tmp_path / "ship-workspaces")
    result = _advance_to_ship(
        harness,
        _production_validator(harness, tmp_path, creator=ship_creator),
    )
    assert result["main_sync_autosync"]["outcome"] == "merged"
    candidate = str(result["candidate_head"])

    run = harness.registry._manager_update_workflow_run(
        harness.run_id,
        facets=("needs_human",),
        needs_human_reason=fixture_needs_human_reason(),
    )
    run = harness.registry._manager_reset_workflow_for_retry_build(
        run.run_id,
        expected_candidate=candidate,
        repair_action="Repair the candidate after autosync.",
    )
    work_actions._record_retry_build_receipt(
        run=run,
        state_path=harness.registry._state_path,
    )

    monkeypatch.setattr(manager, "_EXECUTOR_AUTH_CACHE", {})
    manager._EXECUTOR_AUTH_CACHE["codex"] = runtime_preflight.ProviderFreshness(
        provider_id="codex",
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
    workspace_creator = _CandidateCheckingBuildWorkspaceCreator(
        harness.repo, tmp_path / "repair-workspaces"
    )
    dispatcher = SimpleNamespace(
        _registry=harness.registry,
        _git_runner=None,
        _worktree_creator=workspace_creator,
        poll_headless_done=lambda job_id: harness.registry.get_job(job_id),
    )
    launcher = _VerificationLauncher("codex", "gpt-repair")
    tick = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows(
            [
                {
                    "executor": "codex",
                    "model_id": "gpt-repair",
                    "independence_domain": "openai",
                    "capabilities": ["build"],
                }
            ]
        ),
        launcher_factory=lambda _identity: launcher,
        coordinator_root=harness.state_root,
    )

    assert tick["reason"] in {"in-flight", "card-terminal-malformed-retry"}
    repair_job = harness.registry.get_job(str(tick["job_id"]))
    assert repair_job["workflow_phase"] == "build"
    assert repair_job["branch"] == "feature/14-work"
    assert workspace_creator.calls == [
        ("feature/14-work", candidate, candidate)
    ]


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
    source_job = next(
        job
        for job in harness.registry.list_jobs()
        if job.get("workflow_run_id") == updated.run_id
        and job.get("workflow_card") == "main-sync-autosync"
    )
    fresh_review = _seed_review_for_candidate_source(
        harness,
        candidate=str(updated.candidate_head),
        source_job=source_job,
    )
    review_job = next(
        job
        for job in harness.registry.list_jobs()
        if job.get("workflow_run_id") == updated.run_id
        and job.get("workflow_phase") == "review"
        and job.get("subject_head") == updated.candidate_head
    )
    updated = harness.registry._manager_update_workflow_run(
        updated.run_id,
        gate_refs=(fresh_review,),
    )
    second_main = _advance_origin_with(
        origin,
        tmp_path / "main-advance-2",
        files={"UPSTREAM2.md": "second main movement\n"},
        message="second main movement",
    )

    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "claude",
                "model_id": "sonnet-review",
                "independence_domain": "anthropic",
                "capabilities": ["review"],
            }
        ]
    )
    second = _advance_to_ship(
        harness,
        validator,
        job_id=str(review_job["job_id"]),
        identities=identities,
    )

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
