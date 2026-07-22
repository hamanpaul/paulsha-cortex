from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.github_delivery import (
    COPILOT_REVIEWER_LOGIN,
    CopilotReview,
    DeliveryFacts,
    GitHubCheck,
    MergeStatus,
    ReviewThread,
)
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import GateEvidenceRef


HEAD = "a" * 40
TREE = "b" * 40


def _only_journal_row(state: Path) -> dict:
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["schema"] == "cortex-delivery-journal/v1"
    assert len(payload["runs"]) == 1
    return next(iter(payload["runs"].values()))


def _initialize_delivery_journal(*, snapshot: Path, state: Path) -> dict:
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=state.parent / "jobs.json")
    work_actions._load_work_run(
        state_path=state,
        workflow_registry=registry,
        authority=authority,
    )
    return _only_journal_row(state)


def _init_repo(root: Path, repo: str = "acme/demo") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", f"git@github.com:{repo}.git"],
            check=True,
        )
    return root


def _pr_metadata(path: Path, *, title="fix(work): 修正工作流程", body="Closes #12") -> Path:
    path.write_text(
        json.dumps({"title": title, "body": body, "labels": ["enhancement"]}),
        encoding="utf-8",
    )
    return path


def _snapshot(
    path: Path,
    *,
    issues=(12,),
    source_revisions=("issue:12@open", "openspec:demo@1"),
    provider_revision="gh-1",
    auto_label=True,
    prs=(8,),
    changes=("demo",),
    todo_paths=("docs/todo.md",),
) -> Path:
    _init_repo(path.parent)
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": provider_revision,
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "mapped_issues": list(issues),
                        "mapped_prs": list(prs),
                        "mapped_openspec": list(changes),
                        "mapped_todo_paths": list(todo_paths),
                        "confirmed_todo": True,
                        "auto_label": auto_label,
                        "source_revisions": list(source_revisions),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_start_is_restart_idempotent_and_auto_uses_typed_label_command(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    first = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    second = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert first["result"]["run"]["run_id"] == second["result"]["run"]["run_id"]

    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    work_actions.execute_work_action(
        args={
            "action": "auto",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "enabled": True,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=runner,
    )
    assert calls[0][0][:4] == ["gh", "api", "--method", "POST"]
    assert calls[0][1]["shell"] is False
    with pytest.raises(ValueError, match="strict boolean"):
        work_actions.execute_work_action(
            args={
                "action": "auto",
                "repo": "acme/demo",
                "work_id": "demo",
                "issue": 12,
                "enabled": 1,
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
        )


def test_resume_existing_needs_human_reenters_canonical_starter(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    initial = work_actions._fallback_workflow_starter(
        registry, tmp_path / "runs.json"
    )(authority, claim_key, "planning-failed")
    calls: list[tuple[str, str | None]] = []

    def starter(bound_authority, bound_claim_key, reason):
        assert bound_authority == authority
        calls.append((bound_claim_key, reason))
        return registry._manager_update_workflow_run(
            initial.run_id,
            current_phase="plan",
            facets=(),
            attempts={"claim": 1, "define": 2, "plan": 1},
        )

    result = work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        now=lambda: 200,
        workflow_registry=registry,
        workflow_starter=starter,
    )

    assert calls == [(claim_key, None)]
    assert result["result"]["run"]["current_phase"] == "plan"
    assert result["result"]["run"]["facets"] == []


def test_retry_build_requires_exact_candidate_and_resets_downstream_authority(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    initial = work_actions._fallback_workflow_starter(
        registry, tmp_path / "runs.json"
    )(authority, work_actions._expected_claim_key(authority), None)
    passed = tuple(
        replace(step, gate_result="passed")
        if step.phase in {"build", "verify", "review"}
        else step
        for step in initial.steps
    )
    for phase in ("plan", "build", "verify"):
        registry._manager_update_workflow_run(initial.run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        initial.run_id,
        current_phase="review",
        steps=passed,
        attempts={"build": 1, "verify": 1, "review": 1},
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=("needs_human",),
        gate_refs=(GateEvidenceRef("foreign-review", "/evidence/review.json", "f" * 64),),
        gate_status="running",
    )
    args = {
        "action": "retry-build",
        "repo": "acme/demo",
        "work_id": "demo",
        "issue": 12,
        "actor": "operator",
    }
    with pytest.raises(RuntimeError, match="Candidate CAS mismatch"):
        work_actions.execute_work_action(
            args={**args, "expected_candidate": "c" * 40},
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
        )

    delivery_side_effect_started = tuple(
        replace(step, gate_result="passed") if step.phase == "ship" else step
        for step in passed
    )
    registry._manager_update_workflow_run(initial.run_id, steps=delivery_side_effect_started)
    with pytest.raises(ValueError, match="Manager-owned archive authority"):
        work_actions.execute_work_action(
            args={**args, "expected_candidate": HEAD},
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
        )
    registry._manager_update_workflow_run(initial.run_id, steps=passed)

    result = work_actions.execute_work_action(
        args={**args, "expected_candidate": HEAD},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )

    reset = registry.get_workflow_run(initial.run_id)
    assert result["result"]["action"] == "retry-build"
    assert reset.current_phase == "build"
    assert reset.candidate_head == HEAD
    assert reset.verified_head is None
    assert reset.facets == ()
    assert reset.gate_refs == ()
    assert reset.attempts["build"] == 2
    build_steps = [step for step in reset.steps if step.phase == "build"]
    assert all(step.gate_result == "passed" for step in build_steps[:-1])
    assert build_steps[-1].gate_result == "pending"
    assert "Do not claim archive" in str(build_steps[-1].action)
    assert "Commit or adopt a tested descendant Candidate" in str(
        build_steps[-1].action
    )
    assert "Inspect any existing worktree repair commits" in str(
        build_steps[-1].action
    )
    assert all(
        step.gate_result == "pending"
        for step in reset.steps
        if step.phase in {"verify", "review", "ship"}
    )


def test_retry_build_preserves_only_manager_owned_archive_authority(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    initial = work_actions._fallback_workflow_starter(
        registry, tmp_path / "runs.json"
    )(authority, work_actions._expected_claim_key(authority), None)
    archived = tuple(
        replace(
            step,
            executor="cortex-manager",
            model="deterministic",
            domain="cortex",
            gate_result="passed",
        )
        if step.phase == "ship" and step.card == "openspec-archive"
        else replace(step, gate_result="passed")
        if step.phase in {"build", "verify", "review"}
        else step
        for step in initial.steps
    )
    for phase in ("plan", "build", "verify"):
        registry._manager_update_workflow_run(initial.run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        initial.run_id,
        current_phase="review",
        steps=archived,
        attempts={"build": 1, "verify": 2, "review": 2},
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=("needs_human", "degraded"),
        gate_refs=(GateEvidenceRef("foreign-review", "/evidence/review.json", "f" * 64),),
        gate_status="failed",
    )

    result = work_actions.execute_work_action(
        args={
            "action": "retry-build",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_candidate": HEAD,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )

    reset = registry.get_workflow_run(initial.run_id)
    assert result["result"]["action"] == "retry-build"
    assert reset.current_phase == "build"
    assert reset.facets == ("degraded",)
    assert reset.gate_refs == ()
    archive = next(step for step in reset.steps if step.card == "openspec-archive")
    assert (
        archive.executor,
        archive.model,
        archive.domain,
        archive.gate_result,
    ) == ("cortex-manager", "deterministic", "cortex", "passed")
    policy = next(step for step in reset.steps if step.card == "policy-commit")
    assert policy.gate_result == "pending"
    assert all(
        step.gate_result == "pending"
        for step in reset.steps
        if step.phase in {"verify", "review"}
    )
    assert "Preserve the Manager-owned official OpenSpec archive" in str(
        next(step for step in reset.steps if step.card == "subagent-build").action
    )
    assert "Commit or adopt a tested descendant Candidate" in str(
        next(step for step in reset.steps if step.card == "subagent-build").action
    )
    assert "Inspect any existing worktree repair commits" in str(
        next(step for step in reset.steps if step.card == "subagent-build").action
    )


def test_retry_build_recovers_unbound_builder_terminalization(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    initial = work_actions._fallback_workflow_starter(
        registry, tmp_path / "runs.json"
    )(authority, work_actions._expected_claim_key(authority), None)
    repair_card = next(
        step.card for step in reversed(initial.steps) if step.phase == "build"
    )
    terminalization_failed = tuple(
        replace(step, gate_result="passed")
        if step.phase == "build" and step.card != repair_card
        else replace(step, gate_result="pending")
        if step.phase == "build"
        else replace(
            step,
            executor="cortex-manager",
            model="deterministic",
            domain="cortex",
            gate_result="passed",
        )
        if step.phase == "ship" and step.card == "openspec-archive"
        else step
        for step in initial.steps
    )
    for phase in ("plan", "build"):
        registry._manager_update_workflow_run(initial.run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        initial.run_id,
        steps=terminalization_failed,
        attempts={"build": 2},
        candidate_head=HEAD,
        facets=("needs_human",),
        gate_status="running",
    )
    job_args = {
        "task": "wf-demo-subagent-build",
        "persona": "builder",
        "branch": "feature/demo",
        "pane": "",
        "worktree": str(tmp_path),
        "dispatch_head": HEAD,
        "executor": "codex",
        "model_id": "gpt-primary",
        "independence_domain": "openai",
        "workflow_run_id": initial.run_id,
        "workflow_claim_key": initial.claim_key,
        "workflow_repo": initial.repo,
        "workflow_card": repair_card,
        "workflow_phase": "build",
        "workflow_repo_root": str(tmp_path),
        "workflow_input_root": str(tmp_path),
        "source_revision": initial.source_revision,
    }
    failed_job = registry.create_job(**job_args)
    registry.update_headless_result(
        failed_job["job_id"], status="failed", exit_code=1
    )
    action_args = {
        "action": "retry-build",
        "repo": "acme/demo",
        "work_id": "demo",
        "issue": 12,
        "actor": "operator",
        "expected_candidate": HEAD,
    }
    with pytest.raises(ValueError, match="unbound terminal builder evidence"):
        work_actions.execute_work_action(
            args=action_args,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
        )

    successful_job = registry.create_job(**job_args)
    registry.update_headless_result(
        successful_job["job_id"], status="exited", exit_code=0
    )
    result = work_actions.execute_work_action(
        args=action_args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )

    reset = registry.get_workflow_run(initial.run_id)
    assert result["result"]["action"] == "retry-build"
    assert reset.current_phase == "build"
    assert reset.candidate_head == HEAD
    assert reset.facets == ()
    assert reset.attempts["build"] == 3
    assert registry.get_job(successful_job["job_id"])["workflow_evidence"] is None
    assert "declared input snapshots" in str(
        next(step for step in reset.steps if step.card == repair_card).action
    )


def test_abandon_supersedes_exact_pre_delivery_run_with_immutable_reason(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", prs=())
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    run_id = started["result"]["run"]["run_id"]
    run = registry.get_workflow_run(run_id)
    job = registry.create_job(
        task="wf-abandon-guard",
        persona="planner",
        kind="build",
        branch="feature/demo",
        pane="",
        worktree=run.workspace_root,
        executor="codex",
        model_id="gpt",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="define-card",
        workflow_phase="define",
        workflow_repo_root=run.workspace_root,
        source_revision=run.source_revision,
    )
    args = {
        "action": "abandon",
        "repo": "acme/demo",
        "work_id": "demo",
        "issue": 12,
        "actor": "operator",
        "expected_run_id": run_id,
        "reason": "Superseded by the clean terminal canary.",
    }
    with pytest.raises(ValueError, match="refuses active workflow job"):
        work_actions.execute_work_action(
            args=args,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            workflow_registry=registry,
        )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=1)
    with pytest.raises(RuntimeError, match="CAS mismatch"):
        work_actions.execute_work_action(
            args={**args, "expected_run_id": "workflow-" + "b" * 20},
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            workflow_registry=registry,
        )
    registry._manager_update_workflow_run(run_id, pr_refs=("acme/demo#8",))
    with pytest.raises(ValueError, match="only permits pre-delivery"):
        work_actions.execute_work_action(
            args=args,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            workflow_registry=registry,
        )
    registry._manager_update_workflow_run(run_id, pr_refs=())
    assert not (state.parent / "evidence" / "work-abandon").exists()

    first = work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        workflow_registry=registry,
    )
    abandoned = registry.get_workflow_run(run_id)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    item = payload["work_items"][0]
    item["mapped_issues"] = [12, 13]
    item["source_revisions"].append("issue:13@open")
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    registry._manager_create_workflow_run(
        work_id="demo",
        repo="acme/demo",
        claim_key="claim:v1:" + "c" * 64,
        source_revision="authority-drifted",
        workspace_root=abandoned.workspace_root,
        combo=abandoned.combo,
        current_phase="define",
        steps=abandoned.steps,
        issue_refs=("acme/demo#12", "acme/demo#13"),
        openspec_refs=("demo",),
    )
    second = work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        workflow_registry=registry,
    )

    assert first == second
    abandoned = registry.get_workflow_run(run_id)
    assert abandoned.status == "superseded"
    assert "blocked" in abandoned.facets
    assert abandoned.completion_record_path is None
    evidence = Path(first["result"]["evidence"]["ref"])
    assert evidence.is_file()
    assert evidence.stat().st_mode & 0o222 == 0
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["run_id"] == run_id
    assert payload["actor"] == "operator"
    assert payload["reason"] == args["reason"]
    assert str(evidence) in abandoned.evidence_refs

    with pytest.raises(RuntimeError, match="different authority"):
        work_actions.execute_work_action(
            args={**args, "reason": "A different reason."},
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            workflow_registry=registry,
        )


def test_abandon_evidence_is_immutable_at_link_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "runs.json"
    body = {
        "schema": "cortex-work-abandon/v1",
        "repo": "acme/demo",
        "work_id": "demo",
        "run_id": "workflow-" + "a" * 20,
        "authority_digest": "b" * 64,
        "actor": "operator",
        "reason": "Superseded by the terminal canary.",
    }
    real_link = os.link

    def crash_after_link(source, target):
        real_link(source, target)
        assert Path(target).stat().st_mode & 0o222 == 0
        raise RuntimeError("simulated crash after link")

    monkeypatch.setattr(os, "link", crash_after_link)
    with pytest.raises(RuntimeError, match="simulated crash"):
        work_actions._abandon_record(body, state_path=state)

    monkeypatch.setattr(os, "link", real_link)
    replay = work_actions._abandon_record(body, state_path=state)
    evidence = Path(replay["ref"])
    assert evidence.is_file()
    assert evidence.stat().st_mode & 0o222 == 0


def test_abandon_evidence_temp_collision_preserves_foreign_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "runs.json"
    body = {
        "schema": "cortex-work-abandon/v1",
        "repo": "acme/demo",
        "work_id": "demo",
        "run_id": "workflow-" + "a" * 20,
        "authority_digest": "b" * 64,
        "actor": "operator",
        "reason": "Superseded by the terminal canary.",
    }
    digest = work_actions.verification.canonical_json_hash(body)
    root = tmp_path / "evidence" / "work-abandon"
    root.mkdir(parents=True)
    target = root / f"{body['run_id']}-{digest}.json"
    temporary = root / f".{target.name}.collision.tmp"
    temporary.write_text("foreign temporary\n", encoding="utf-8")
    monkeypatch.setattr(
        work_actions,
        "uuid4",
        lambda: SimpleNamespace(hex="collision"),
    )

    with pytest.raises(RuntimeError, match="temporary collision"):
        work_actions._abandon_record(body, state_path=state)

    assert temporary.read_text(encoding="utf-8") == "foreign temporary\n"
    assert not target.exists()


def test_abandon_evidence_rejects_non_regular_existing_target(tmp_path: Path) -> None:
    state = tmp_path / "runs.json"
    body = {
        "schema": "cortex-work-abandon/v1",
        "repo": "acme/demo",
        "work_id": "demo",
        "run_id": "workflow-" + "a" * 20,
        "authority_digest": "b" * 64,
        "actor": "operator",
        "reason": "Superseded by the terminal canary.",
    }
    digest = work_actions.verification.canonical_json_hash(body)
    root = tmp_path / "evidence" / "work-abandon"
    root.mkdir(parents=True)
    target = root / f"{body['run_id']}-{digest}.json"
    target.mkdir()

    with pytest.raises(RuntimeError, match="workflow abandon evidence conflict"):
        work_actions._abandon_record(body, state_path=state)


def test_abandon_evidence_rejects_oversized_target_without_reading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "runs.json"
    body = {
        "schema": "cortex-work-abandon/v1",
        "repo": "acme/demo",
        "work_id": "demo",
        "run_id": "workflow-" + "a" * 20,
        "authority_digest": "b" * 64,
        "actor": "operator",
        "reason": "Superseded by the terminal canary.",
    }
    digest = work_actions.verification.canonical_json_hash(body)
    root = tmp_path / "evidence" / "work-abandon"
    root.mkdir(parents=True)
    target = root / f"{body['run_id']}-{digest}.json"
    target.write_bytes(b"x" * 5000)
    target.chmod(0o444)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: (_ for _ in ()).throw(AssertionError("must not read")),
    )

    with pytest.raises(RuntimeError, match="workflow abandon evidence conflict"):
        work_actions._abandon_record(body, state_path=state)


def test_review_attest_writes_immutable_exact_head_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    run_id = started["result"]["run"]["run_id"]
    for phase in ("plan", "build", "verify", "review"):
        registry._manager_update_workflow_run(run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=HEAD,
        verified_head=HEAD,
        pr_refs=("acme/demo#8",),
        gate_refs=(GateEvidenceRef("foreign-review", "/evidence/foreign.json", "f" * 64),),
        gate_status="passed",
        facets=("needs_human", "degraded"),
    )

    class GitHub:
        def __init__(self, *, runner):
            pass

        def fetch_delivery_facts(self, **kwargs):
            return DeliveryFacts(
                head=HEAD, mergeable=True, mergeable_state="clean",
                checks=(GitHubCheck("pytest", "completed", "success"),),
                copilot_reviews=(), review_threads=(), closing_issues=(12,),
                active_openspec_absent=True, archive_present=True,
            )

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    args = {
        "action": "review-attest", "repo": "acme/demo", "work_id": "demo",
        "actor": "maintainer@example", "verdict": "approved",
        "summary": "Exact-HEAD adversarial review passed.", "findings": [],
    }
    first = work_actions.execute_work_action(
        args=args, requested_by="operator", snapshot_path=snapshot, state_path=state,
        now=lambda: 210, workflow_registry=registry,
    )
    second = work_actions.execute_work_action(
        args=args, requested_by="operator", snapshot_path=snapshot, state_path=state,
        now=lambda: 210, workflow_registry=registry,
    )

    assert first == second
    evidence = Path(first["result"]["ref"])
    assert evidence.is_file()
    assert evidence.stat().st_mode & 0o222 == 0
    persisted = registry.get_workflow_run(run_id)
    review = next(ref for ref in persisted.gate_refs if ref.kind == "maintainer-review")
    assert review.ref == str(evidence)
    assert review.sha256 == first["result"]["hash"]
    assert persisted.facets == ("degraded",)


def test_typed_maintainer_review_can_reenter_only_copilot_needs_human_stop() -> None:
    evidence = {
        "maintainer_review_path": "/evidence/maintainer.json",
        "maintainer_review_hash": "a" * 64,
    }

    assert work_actions._recoverable_maintainer_ship_stop(
        ship={"phase": "needs_human", "reason": "copilot-finding-budget-exhausted"},
        args=evidence,
    )
    assert work_actions._recoverable_maintainer_ship_stop(
        ship={"phase": "needs_human", "reason": "copilot-review-timeout"},
        args=evidence,
    )
    assert not work_actions._recoverable_maintainer_ship_stop(
        ship={"phase": "needs_human", "reason": "external-merge-without-authorization"},
        args=evidence,
    )
    with pytest.raises(ValueError, match="path/hash must be supplied together"):
        work_actions._recoverable_maintainer_ship_stop(
            ship={"phase": "needs_human", "reason": "copilot-review-timeout"},
            args={"maintainer_review_path": "/evidence/maintainer.json"},
        )


def test_ship_reenters_copilot_stop_through_bound_maintainer_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run_id = started["result"]["run"]["run_id"]
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    maintainer_body = {
        "schema": "cortex-maintainer-review/v1",
        "repo": "acme/demo",
        "work_id": "demo",
        "run_id": run_id,
        "authority_digest": work_actions.work_authority_digest(authority),
        "pr_number": 8,
        "candidate": HEAD,
        "actor": "maintainer",
        "requested_by": "operator",
        "verdict": "approved",
        "summary": "Exact-HEAD review passed.",
        "findings": [],
        "reviewed_at_epoch": 205.0,
    }
    maintainer = work_actions._maintainer_review_record(
        maintainer_body, state_path=state
    )
    foreign_payload = {"state": "passed", "candidate": HEAD}
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps(foreign_payload), encoding="utf-8")
    foreign_hash = work_actions.verification.canonical_json_hash(foreign_payload)
    for phase in ("plan", "build", "verify", "review"):
        registry._manager_update_workflow_run(run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=HEAD,
        verified_head=HEAD,
        pr_refs=("acme/demo#8",),
        gate_refs=(
            GateEvidenceRef("foreign-review", str(foreign), foreign_hash),
            GateEvidenceRef("maintainer-review", maintainer["ref"], maintainer["hash"]),
        ),
        gate_status="passed",
        facets=("needs_human",),
    )
    _initialize_delivery_journal(snapshot=snapshot, state=state)
    persisted = json.loads(state.read_text(encoding="utf-8"))
    row = persisted["runs"][run_id]
    row["ship"] = {
        "phase": "needs_human",
        "reason": "copilot-finding-budget-exhausted",
        "head": HEAD,
        "tree_hash": TREE,
        "fix_rounds": 3,
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
    }
    state.write_text(json.dumps(persisted), encoding="utf-8")
    merged = {"value": False}

    class GitHub:
        def __init__(self, *, runner):
            pass

        def ensure_pr_metadata(self, **kwargs):
            pass

        def fetch_delivery_facts(self, **kwargs):
            return DeliveryFacts(
                head=HEAD,
                mergeable=True,
                mergeable_state="clean",
                checks=(GitHubCheck("pytest", "completed", "success"),),
                copilot_reviews=(),
                review_threads=(),
                closing_issues=(12,),
                active_openspec_absent=True,
                archive_present=True,
            )

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(
                merged=merged["value"],
                pr_head=HEAD,
                merge_commit="c" * 40 if merged["value"] else None,
            )

    class Orchestrator:
        def __init__(self, *, github, now):
            pass

        def merge_if_ready(self, **kwargs):
            assert kwargs["copilot"] is None
            assert kwargs["maintainer_review"].path == maintainer["ref"]
            merged["value"] = True
            return SimpleNamespace(expected_head=HEAD, expected_tree_hash=TREE)

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", Orchestrator)
    monkeypatch.setattr(
        work_actions,
        "_validate_foreign_review",
        lambda *args, **kwargs: foreign_payload,
    )
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: PreflightResult(
            passed=True,
            failed_stage=None,
            policy=CommandResult(("policy",), 0, "", ""),
            ci_parity=CommandResult(("preflight",), 0, "", ""),
            head=HEAD,
            tree_hash=TREE,
        ),
    )
    result = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": ["docs/todo.md"],
            "foreign_review_path": str(foreign),
            "foreign_review_hash": foreign_hash,
            "maintainer_review_path": maintainer["ref"],
            "maintainer_review_hash": maintainer["hash"],
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )

    assert result["result"]["action"] == "merged-awaiting-closure"
    assert result["result"]["review_kind"] == "maintainer-review"
    assert _only_journal_row(state)["ship"]["phase"] == "merged"


def test_auto_without_issue_mutates_every_mapped_issue(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", issues=(12, 13))
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    result = work_actions.execute_work_action(
        args={
            "action": "auto",
            "repo": "acme/demo",
            "work_id": "demo",
            "enabled": True,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        now=lambda: 200,
        runner=runner,
    )
    assert result["result"] == {"action": "auto", "enabled": True, "issues": [12, 13]}
    assert [call[4] for call in calls] == [
        "repos/acme/demo/issues/12/labels",
        "repos/acme/demo/issues/13/labels",
    ]


def test_auto_without_issue_fails_closed_if_any_label_mutation_fails(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", issues=(12, 13))
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        failed = "issues/13/" in " ".join(argv)
        return SimpleNamespace(returncode=1 if failed else 0, stdout="{}", stderr="boom" if failed else "")

    with pytest.raises(RuntimeError, match="auto-label mutation failed"):
        work_actions.execute_work_action(
            args={
                "action": "auto",
                "repo": "acme/demo",
                "work_id": "demo",
                "enabled": False,
            },
            requested_by="operator",
            snapshot_path=snapshot,
            now=lambda: 200,
            runner=runner,
        )
    assert len(calls) == 2


def test_preflight_authorization_hash_excludes_drifting_command_output() -> None:
    first = PreflightResult(
        True,
        None,
        CommandResult(("policy",), 0, "first stdout", "first stderr"),
        CommandResult(("preflight",), 0, "duration=1", ""),
        HEAD,
        TREE,
    )
    second = PreflightResult(
        True,
        None,
        CommandResult(("policy",), 0, "different stdout", "different stderr"),
        CommandResult(("preflight",), 0, "duration=999", "warning text"),
        HEAD,
        TREE,
    )
    assert work_actions._preflight_hash(first) == work_actions._preflight_hash(second)


def test_source_change_starts_new_canonical_run(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    first = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    _snapshot(
        snapshot,
        source_revisions=("issue:12@open", "openspec:demo@2"),
        provider_revision="gh-2",
    )
    changed = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert changed["result"]["action"] == "claim"
    assert changed["result"]["run"]["run_id"] != first["result"]["run"]["run_id"]
    runs = JobRegistry(state_path=state.parent / "jobs.json").list_workflow_runs()
    assert len([run for run in runs if run.status == "ongoing"]) == 1
    old = next(run for run in runs if run.run_id == first["result"]["run"]["run_id"])
    assert old.status == "superseded"
    assert "blocked" in old.facets


def test_auto_scan_does_not_supersede_active_run_when_planning_adds_sources(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    first = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    _snapshot(
        snapshot,
        source_revisions=(
            "issue:12@open",
            "openspec:demo@1",
            "superpowers_plan:docs/demo.md@active",
        ),
        provider_revision="gh-2",
    )

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "cortex:auto-on-going"}]}),
            stderr="",
        ),
        workflow_registry=registry,
    )

    assert result[0]["action"] == "resume"
    assert result[0]["run"]["run_id"] == first["result"]["run"]["run_id"]
    assert len(registry.list_workflow_runs()) == 1
    assert registry.list_workflow_runs()[0].status == "ongoing"


def test_canonical_claim_excludes_derived_sources_and_volatile_github_revisions(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "canonical.json"

    def write(
        *,
        provider_revision: str,
        issue_revision: str,
        pr_revision: str,
        issue_status: str = "open",
        openspec_status: str = "active",
    ) -> None:
        snapshot.write_text(
            json.dumps(
                {
                    "schema": "work-items-snapshot/v1",
                    "providers": {
                        "github:acme/demo": {
                            "provider_id": "github:acme/demo",
                            "status": "ok",
                            "last_attempt_at": "2026-07-17T00:00:00Z",
                            "last_success_at": "2026-07-17T00:00:00Z",
                            "revision": provider_revision,
                            "diagnostics": [],
                            "sources": [],
                            "observations": {},
                        }
                    },
                    "work_items": [
                        {
                            "repo": "acme/demo",
                            "work_id": "demo",
                            "sources": [
                                {
                                    "source_id": "github_issue:acme/demo#12",
                                    "kind": "github_issue",
                                    "ref": "acme/demo#12",
                                    "revision": issue_revision,
                                    "status": issue_status,
                                    "confidence": "confirmed",
                                    "provider": "github:acme/demo",
                                },
                                {
                                    "source_id": "github_pr:acme/demo#8",
                                    "kind": "github_pr",
                                    "ref": "acme/demo#8",
                                    "revision": pr_revision,
                                    "status": "open",
                                    "confidence": "confirmed",
                                    "provider": "github:acme/demo",
                                },
                                {
                                    "source_id": "openspec:acme/demo:demo",
                                    "kind": "openspec",
                                    "ref": "demo",
                                    "revision": "spec-content",
                                    "status": openspec_status,
                                    "confidence": "confirmed",
                                    "provider": "repo:acme/demo",
                                },
                                {
                                    "source_id": "workflow_run:acme/demo:run-1",
                                    "kind": "workflow_run",
                                    "ref": "run-1",
                                    "revision": "registry:9",
                                    "status": "ongoing",
                                    "confidence": "confirmed",
                                    "provider": "workflow:acme/demo",
                                },
                                {
                                    "source_id": "completion_record:acme/demo:run-1",
                                    "kind": "completion_record",
                                    "ref": "run-1",
                                    "revision": "completion:9",
                                    "status": "valid",
                                    "confidence": "confirmed",
                                    "provider": "workflow:acme/demo",
                                },
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    write(provider_revision="gh-1", issue_revision="updated:1", pr_revision="updated:1")
    first = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    first_digest = work_actions.work_authority_digest(first)
    assert all("workflow_run" not in row and "completion_record" not in row for row in first.source_revisions)

    write(provider_revision="gh-2", issue_revision="updated:2", pr_revision="updated:2")
    refreshed = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    assert refreshed.source_revisions == first.source_revisions
    assert work_actions.work_authority_digest(refreshed) == first_digest

    write(
        provider_revision="gh-3",
        issue_revision="updated:3",
        pr_revision="updated:3",
        issue_status="closed",
        openspec_status="archived",
    )
    completed = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    completed_candidate = work_actions.ClaimCandidate(
        authority=completed,
        repo=completed.repo,
        work_id=completed.work_id,
        source_revisions=completed.source_revisions,
        confirmed_todo=completed.confirmed_todo,
        confirmed_issue=12,
        auto_label=False,
        active_run_id=None,
        active_claim_key=None,
    )
    completed_key = work_actions.build_claim_key(completed_candidate)

    write(
        provider_revision="gh-4",
        issue_revision="updated:4",
        pr_revision="updated:4",
        issue_status="open",
        openspec_status="archived",
    )
    reopened = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    assert work_actions.work_authority_digest(reopened) != work_actions.work_authority_digest(
        completed
    )

    write(
        provider_revision="gh-5",
        issue_revision="updated:5",
        pr_revision="updated:5",
        issue_status="closed",
        openspec_status="active",
    )
    reactivated = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    assert work_actions.work_authority_digest(reactivated) != work_actions.work_authority_digest(
        completed
    )
    decision = work_actions.decide_manual_start(
        work_actions.ClaimCandidate(
            authority=reactivated,
            repo=reactivated.repo,
            work_id=reactivated.work_id,
            source_revisions=reactivated.source_revisions,
            confirmed_todo=reactivated.confirmed_todo,
            confirmed_issue=12,
            auto_label=False,
            active_run_id="done-run",
            active_claim_key=completed_key,
            active_status="done",
            active_snapshot_hash=completed.snapshot_hash,
            active_source_revisions=completed.source_revisions,
            active_provider_revision=completed.github_provider_revision,
            active_authority_digest=work_actions.work_authority_digest(completed),
        ),
        now_epoch=reactivated.github_last_success_epoch + 1,
    )
    assert decision.action == "claim"
    assert decision.reason is None


def test_canonical_authority_loader_ignores_issue_only_topic_rows(tmp_path: Path) -> None:
    snapshot = tmp_path / "canonical.json"
    provider = {
        "provider_id": "github:acme/demo",
        "status": "ok",
        "last_attempt_at": "2026-07-17T00:00:00Z",
        "last_success_at": "2026-07-17T00:00:00Z",
        "revision": "gh-1",
        "diagnostics": [],
        "sources": [],
        "observations": {},
    }
    issue = {
        "source_id": "github_issue:acme/demo#12",
        "kind": "github_issue",
        "ref": "acme/demo#12",
        "revision": "issue-open",
        "status": "open",
        "confidence": "confirmed",
        "provider": "github:acme/demo",
    }
    openspec = {
        "source_id": "openspec:acme/demo:demo",
        "kind": "openspec",
        "ref": "demo",
        "revision": "spec-content",
        "status": "active",
        "confidence": "confirmed",
        "provider": "repo:acme/demo",
    }
    workflow = {
        "source_id": "workflow_run:acme/demo:run-1",
        "kind": "workflow_run",
        "ref": "run-1",
        "revision": "registry:1",
        "status": "ongoing",
        "confidence": "confirmed",
        "provider": "workflow:acme/demo",
    }
    snapshot.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {"github:acme/demo": provider},
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "issue:acme/demo#99",
                        "next_actions": [],
                        "sources": [{**issue, "ref": "acme/demo#99", "source_id": "github_issue:acme/demo#99"}],
                    },
                    {
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "next_actions": [],
                        "sources": [issue, openspec, workflow],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )

    assert authority.mapped_issues == (12,)
    assert authority.mapped_openspec == ("demo",)


def test_snapshot_refresh_noise_keeps_same_semantic_run(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    first = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    first_snapshot_hash = first["result"]["run"]["snapshot_hash"]
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload.update(
        {
            "sequence": 99,
            "written_at": "2026-07-17T12:00:00Z",
            "fleet_noise": {"other/repo": "changed"},
        }
    )
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    refreshed = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert refreshed["result"]["action"] == "resume"
    assert refreshed["result"]["run"]["run_id"] == first["result"]["run"]["run_id"]
    assert refreshed["result"]["run"]["snapshot_hash"] != first_snapshot_hash


def test_explicit_resume_selects_unique_done_ship_run_after_authority_changes(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        source_revisions=("issue:12@closed", "openspec:demo@archived"),
    )
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    terminal = SimpleNamespace(
        run_id="workflow-" + "a" * 20,
        repo="acme/demo",
        work_id="demo",
        claim_key="claim:v1:" + "b" * 64,
        status="done",
        current_phase="ship",
        facets=(),
        issue_refs=("acme/demo#12",),
        openspec_refs=("demo",),
        to_dict=lambda: {
            "run_id": "workflow-" + "a" * 20,
            "repo": "acme/demo",
            "work_id": "demo",
            "current_phase": "ship",
            "status": "done",
        },
    )
    registry = SimpleNamespace(list_workflow_runs=lambda: [terminal])

    result = work_actions._claim_action(
        args={"action": "resume"},
        authority=authority,
        now_epoch=200,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
        workflow_starter=lambda *_args: (_ for _ in ()).throw(
            AssertionError("terminal refresh must not create a new run")
        ),
    )

    assert result["action"] == "resume"
    assert result["reason"] == "active-workflow"
    assert result["run"]["run_id"] == terminal.run_id


def test_periodic_auto_scan_claims_labeled_work_and_persists_missing_issue(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    claimed = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "cortex:auto-on-going"}]}),
            stderr="",
        ),
    )
    assert claimed[0]["action"] == "claim"
    assert claimed[0]["run"]["status"] == "ongoing"

    missing_snapshot = _snapshot(
        tmp_path / "missing.json",
        issues=(),
        source_revisions=("openspec:demo@2",),
    )
    missing_state = tmp_path / "missing-runs.json"
    attention = work_actions.run_auto_claim_scan(
        snapshot_path=missing_snapshot,
        state_path=missing_state,
        now=lambda: 200,
    )
    assert attention[0]["action"] == "needs_human"
    assert attention[0]["run"]["reason"] == "missing_issue"

    _snapshot(
        missing_snapshot,
        issues=(12,),
        source_revisions=("issue:12@open", "openspec:demo@3"),
        provider_revision="gh-3",
    )
    resumed = work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=missing_snapshot,
        state_path=missing_state,
        now=lambda: 200,
    )
    assert resumed["result"]["action"] == "claim"
    assert resumed["result"]["run"]["status"] == "ongoing"


def test_periodic_auto_scan_reads_every_issue_and_any_label_claims(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", issues=(12, 13))
    calls: list[str] = []

    def runner(argv, **kwargs):
        calls.append(argv[-1])
        labels = [{"name": "cortex:auto-on-going"}] if argv[-1].endswith("/13") else []
        return SimpleNamespace(returncode=0, stdout=json.dumps({"labels": labels}), stderr="")

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        now=lambda: 200,
        runner=runner,
    )
    assert calls == ["repos/acme/demo/issues/12", "repos/acme/demo/issues/13"]
    assert result[0]["action"] == "claim"


def test_periodic_auto_scan_fails_closed_if_any_mapped_issue_read_fails(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", issues=(12, 13))

    def runner(argv, **kwargs):
        if argv[-1].endswith("/13"):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "cortex:auto-on-going"}]}),
            stderr="",
        )

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        now=lambda: 200,
        runner=runner,
    )
    assert result == [{
        "repo": "acme/demo",
        "work_id": "demo",
        "action": "blocked",
        "reason": "github-label-read-failed",
    }]
    assert not (tmp_path / "runs.json").exists()


def test_unlink_persists_exclusion_and_link_removes_it(tmp_path: Path) -> None:
    repo_root = _init_repo(tmp_path / "repo")
    common = {
        "repo": "acme/demo",
        "work_id": "demo",
        "issue": 12,
        "repo_root": str(repo_root),
    }
    work_actions.execute_work_action(
        args={"action": "unlink", **common},
        requested_by="operator",
    )
    text = (repo_root / ".cortex" / "work-items.yaml").read_text(encoding="utf-8")
    assert "excludes:" in text and "acme/demo#12" in text
    work_actions.execute_work_action(
        args={"action": "link", **common},
        requested_by="operator",
    )
    payload = work_actions.safe_load(
        (repo_root / ".cortex" / "work-items.yaml").read_text(encoding="utf-8")
    )
    assert payload["work_items"]["demo"]["excludes"] == []
    assert payload["work_items"]["demo"]["links"][0]["ref"] == "acme/demo#12"


def test_typed_path_and_openspec_links_and_exclusions_are_canonical(tmp_path: Path) -> None:
    repo_root = _init_repo(tmp_path / "repo")
    base = {"repo": "acme/demo", "work_id": "demo", "repo_root": str(repo_root)}
    for kind, ref in (
        ("path", "docs/superpowers/specs/demo.md"),
        ("openspec", "unified-work-lifecycle"),
    ):
        work_actions.execute_work_action(
            args={"action": "link", "kind": kind, "ref": ref, **base},
            requested_by="operator",
        )
    work_actions.execute_work_action(
        args={
            "action": "unlink",
            "kind": "path",
            "ref": "docs/superpowers/specs/demo.md",
            **base,
        },
        requested_by="operator",
    )
    payload = work_actions.safe_load(
        (repo_root / ".cortex" / "work-items.yaml").read_text(encoding="utf-8")
    )["work_items"]["demo"]
    assert {"kind": "openspec", "ref": "unified-work-lifecycle"} in payload["links"]
    assert {
        "kind": "path",
        "ref": "docs/superpowers/specs/demo.md",
    } in payload["excludes"]


@pytest.mark.parametrize(
    "extra",
    [
        {"kind": "path", "ref": "../escape"},
        {"kind": "openspec", "ref": "Bad Slug"},
        {"kind": "github_pr", "ref": "other/repo#2"},
        {"issue": 12, "kind": "github_issue", "ref": "acme/demo#12"},
    ],
)
def test_link_rejects_malformed_or_conflicting_source(tmp_path: Path, extra: dict) -> None:
    repo_root = _init_repo(tmp_path / "repo")
    with pytest.raises(ValueError):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(repo_root),
                **extra,
            },
            requested_by="operator",
        )


def test_override_writer_rejects_symlink_escape(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    outside = tmp_path / "outside"
    _init_repo(repo_root)
    outside.mkdir()
    (repo_root / ".cortex").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(repo_root),
                "kind": "path",
                "ref": "docs/demo.md",
            },
            requested_by="operator",
        )


def test_override_writer_rejects_malformed_existing_schema(tmp_path: Path) -> None:
    repo_root = _init_repo(tmp_path / "repo")
    override = repo_root / ".cortex" / "work-items.yaml"
    override.parent.mkdir(parents=True)
    override.write_text(
        "version: 1\nwork_items:\n  demo:\n    title: demo\n    links:\n      - kind: path\n        ref: ../escape\n    excludes: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="canonical repo-relative"):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(repo_root),
                "kind": "openspec",
                "ref": "demo",
            },
            requested_by="operator",
        )


def test_all_actions_reject_malformed_repo_and_repo_root_remote_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        work_actions.execute_work_action(
            args={"action": "start", "repo": "bad/repo/extra", "work_id": "demo"},
            requested_by="operator",
            snapshot_path=tmp_path / "missing",
        )


def test_repo_root_must_be_exact_git_toplevel(tmp_path: Path) -> None:
    root = _init_repo(tmp_path / "repo")
    nested = root / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="top-level"):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(nested),
                "kind": "openspec",
                "ref": "demo",
            },
            requested_by="operator",
        )
    root = _init_repo(tmp_path / "other", repo="acme/other")
    with pytest.raises(ValueError, match="remote.*match"):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(root),
                "kind": "openspec",
                "ref": "demo",
            },
            requested_by="operator",
        )


def test_path_link_rejects_repo_internal_symlink(tmp_path: Path) -> None:
    root = _init_repo(tmp_path / "repo")
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    (root / "linked.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        work_actions.execute_work_action(
            args={
                "action": "link",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(root),
                "kind": "path",
                "ref": "linked.md",
            },
            requested_by="operator",
        )


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"pr_number": 9}, "PR.*authorized"),
        ({"change": "other"}, "OpenSpec.*authorized"),
        ({"todo_paths": ["docs/other.md"]}, "Todo.*authorized"),
    ],
)
def test_ship_payload_refs_must_exactly_match_work_authority(
    tmp_path: Path, override: dict, reason: str
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    args = {
        "action": "ship",
        "repo": "acme/demo",
        "work_id": "demo",
        "repo_root": str(tmp_path),
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
        "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        **override,
    }
    with pytest.raises(RuntimeError, match=reason):
        work_actions.execute_work_action(
            args=args,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
        )


@pytest.mark.parametrize(
    "snapshot_overrides",
    [
        {"prs": (8, 9)},
        {"changes": ("demo", "other")},
        {"todo_paths": ("docs/todo.md", "docs/other.md")},
    ],
)
def test_ship_needs_human_when_authority_has_multiple_delivery_targets(
    tmp_path: Path, snapshot_overrides: dict
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", **snapshot_overrides)
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    result = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": ["docs/todo.md"],
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert result["result"] == {
        "action": "needs_human",
        "reason": "multiple-delivery-targets-unsupported",
    }
    persisted = _only_journal_row(state)
    assert "status" not in persisted
    assert JobRegistry(state_path=state.parent / "jobs.json").list_workflow_runs()[0].facets == (
        "needs_human",
    )


def test_ship_resume_rearms_prebinding_target_cardinality_stop(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import work_bridge
    from paulsha_cortex.coordinator.claim import work_authority_digest

    snapshot = _snapshot(tmp_path / "snapshot.json", todo_paths=())
    state = tmp_path / "delivery-journal.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    stopped = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": [],
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert stopped["result"]["reason"] == "multiple-delivery-targets-unsupported"

    _snapshot(
        snapshot,
        todo_paths=("docs/todo.md",),
        source_revisions=("issue:12@open", "openspec:demo@1", "todo:docs/todo.md@1"),
    )
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=state.parent / "jobs.json")
    run = registry.list_workflow_runs()[0]
    run = registry._manager_update_workflow_run(
        run.run_id,
        source_revision=work_authority_digest(authority),
        facets=("needs_human", "degraded"),
    )
    work_bridge._rebase_delivery_journal_authority(
        state_root=state.parent,
        run=run,
        authority=authority,
    )

    active = tmp_path / "openspec" / "changes" / "demo"
    active.mkdir(parents=True)
    (active / "tasks.md").write_text("- [x] ready\n", encoding="utf-8")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "demo.md").write_text("fixed\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n### Fixed\n- ready\n", encoding="utf-8"
    )

    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    resumed = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": ["docs/todo.md"],
            "pr_metadata_path": str(tmp_path / "pr.json"),
        },
        requested_by="operator",
        runner=runner,
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )

    assert resumed["result"]["action"] == "archive-applied-needs-commit"
    assert "ship" not in _only_journal_row(state)
    assert JobRegistry(state_path=state.parent / "jobs.json").get_workflow_run(
        run.run_id
    ).facets == ("degraded",)


def test_ship_rejects_old_run_after_current_authority_changes(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    _snapshot(
        snapshot,
        source_revisions=("issue:12@open", "openspec:demo@2"),
        provider_revision="gh-2",
    )
    with pytest.raises(RuntimeError, match="current WorkAuthority"):
        work_actions.execute_work_action(
            args={
                "action": "ship",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(tmp_path),
                "pr_number": 8,
                "change": "demo",
                "todo_paths": ["docs/todo.md"],
                "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
        )


def test_ship_rejects_authorized_repo_path_symlink(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    outside = tmp_path / "outside-change"
    outside.mkdir()
    active_root = tmp_path / "openspec" / "changes"
    active_root.mkdir(parents=True)
    (active_root / "demo").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        work_actions.execute_work_action(
            args={
                "action": "ship",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(tmp_path),
                "pr_number": 8,
                "change": "demo",
                "todo_paths": ["docs/todo.md"],
                "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
        )


def test_default_ship_runtime_is_resumable_and_connects_all_delivery_gates(
    monkeypatch, tmp_path: Path
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    foreign = tmp_path / "foreign.json"
    foreign.write_text("{}", encoding="utf-8")
    completion = tmp_path / "completion.json"
    completion.write_text("{}", encoding="utf-8")
    review_available = {"value": False}
    merged_state = {"value": False}
    calls = []

    class GitHub:
        def __init__(self, *, runner):
            calls.append("github-init")

        def ensure_pr_metadata(self, **kwargs):
            calls.append("ensure-pr-metadata")

        def fetch_delivery_facts(self, **kwargs):
            calls.append("remote-archive-gate")
            reviews = ()
            if review_available["value"]:
                reviews = (
                    CopilotReview(
                        review_id=9,
                        commit_id=HEAD,
                        state="COMMENTED",
                        body="ok",
                        author=COPILOT_REVIEWER_LOGIN,
                        submitted_at_epoch=205,
                    ),
                )
            return DeliveryFacts(
                head=HEAD,
                mergeable=True,
                mergeable_state="clean",
                checks=(GitHubCheck("pytest", "completed", "success"),),
                copilot_reviews=reviews,
                review_threads=(),
                closing_issues=(12,),
                active_openspec_absent=True,
                archive_present=True,
            )

        def request_copilot(self, **kwargs):
            calls.append("request-copilot")

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(
                merged=merged_state["value"],
                pr_head=HEAD,
                merge_commit="c" * 40 if merged_state["value"] else None,
            )

    class Orchestrator:
        def __init__(self, *, github, now):
            calls.append("orchestrator-init")

        def merge_if_ready(self, **kwargs):
            calls.append("merge-if-ready")
            durable = _only_journal_row(state)
            assert durable["ship"]["phase"] == "merge-authorized"
            assert durable["ship"]["merge_authorization"]["hash"]
            authorization_path = Path(durable["ship"]["merge_authorization"]["path"])
            assert authorization_path.is_file()
            assert authorization_path.stat().st_mode & 0o222 == 0
            assert kwargs["authority"].mapped_issues == (12,)
            assert kwargs["preflight"].head == HEAD
            assert kwargs["copilot"].review_id == 9
            merged_state["value"] = True
            return SimpleNamespace(expected_head=HEAD, expected_tree_hash=TREE)

        def verify_remote_closure(self, **kwargs):
            calls.append("remote-closure")
            assert kwargs["expected_head"] == HEAD
            return SimpleNamespace(
                facts=SimpleNamespace(merge_commit="c" * 40),
                completion_record={"path": "/evidence/completion.json", "hash": "d" * 64},
            )

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", Orchestrator)
    foreign_normalized = {"state": "passed", "candidate": HEAD}
    monkeypatch.setattr(
        work_actions,
        "_validate_foreign_review",
        lambda *args, **kwargs: foreign_normalized,
    )
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: PreflightResult(
            passed=True,
            failed_stage=None,
            policy=CommandResult(("policy",), 0, "", ""),
            ci_parity=CommandResult(("preflight",), 0, "", ""),
            head=HEAD,
            tree_hash=TREE,
        ),
    )
    base = {
        "action": "ship",
        "repo": "acme/demo",
        "work_id": "demo",
        "repo_root": str(tmp_path),
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
        "foreign_review_path": str(foreign),
        "foreign_review_hash": work_actions.verification.canonical_json_hash(foreign_normalized),
        "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
    }
    first = work_actions.execute_work_action(
        args=base,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert first["result"]["action"] == "awaiting-copilot"
    assert "request-copilot" in calls

    review_available["value"] = True
    second = work_actions.execute_work_action(
        args=base,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
    )
    assert second["result"]["action"] == "merged-awaiting-closure"
    assert "merge-if-ready" in calls

    third = work_actions.execute_work_action(
        args={**base, "completion_record_path": str(completion)},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 220,
    )
    assert third["result"]["action"] == "done"
    assert "remote-closure" in calls


def test_ship_runs_official_archive_before_preflight(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    (tmp_path / "openspec" / "changes" / "demo").mkdir(parents=True)
    (tmp_path / "openspec" / "changes" / "demo" / "tasks.md").write_text(
        "- [x] complete\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n- **demo**: done\n", encoding="utf-8"
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": ["docs/todo.md"],
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=runner,
    )
    assert result["result"]["action"] == "archive-applied-needs-commit"
    assert calls[-1][0] == ["openspec", "archive", "-y", "demo"]
    assert calls[0][0] == ["openspec", "validate", "demo", "--strict"]


def test_review_findings_persist_across_heads_and_third_round_needs_human(
    monkeypatch, tmp_path: Path
) -> None:
    heads = ["a" * 40, "b" * 40, "c" * 40]
    current = {"head": heads[0], "review": False, "submitted": 201.0, "now": 200.0}
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: current["now"],
    )

    class GitHub:
        def __init__(self, *, runner):
            pass

        def ensure_pr_metadata(self, **kwargs):
            pass

        def fetch_delivery_facts(self, **kwargs):
            reviews = ()
            if current["review"]:
                reviews = (
                    CopilotReview(
                        review_id=heads.index(current["head"]) + 1,
                        commit_id=current["head"],
                        state="COMMENTED",
                        body="finding",
                        author=COPILOT_REVIEWER_LOGIN,
                        submitted_at_epoch=current["submitted"],
                    ),
                )
            return DeliveryFacts(
                head=current["head"],
                mergeable=True,
                mergeable_state="clean",
                checks=(),
                copilot_reviews=reviews,
                review_threads=(ReviewThread("thread", False, False),),
                closing_issues=(12,),
                active_openspec_absent=True,
                archive_present=True,
            )

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(False, current["head"], None)

        def request_copilot(self, **kwargs):
            pass

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: PreflightResult(
            True,
            None,
            CommandResult(("policy",), 0, "", ""),
            CommandResult(("preflight",), 0, "", ""),
            current["head"],
            TREE,
        ),
    )
    base = {
        "action": "ship",
        "repo": "acme/demo",
        "work_id": "demo",
        "repo_root": str(tmp_path),
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
        "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
    }
    for index, head in enumerate(heads):
        current.update(head=head, review=False, now=200.0 + index * 20, submitted=201.0 + index * 20)
        requested = work_actions.execute_work_action(
            args=base,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: current["now"],
        )
        assert requested["result"]["action"] == "awaiting-copilot"
        current["review"] = True
        current["now"] += 2
        reviewed = work_actions.execute_work_action(
            args=base,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: current["now"],
        )
        if index < 2:
            assert reviewed["result"]["action"] == "fix-required"
        else:
            assert reviewed["result"] == {
                "action": "needs_human",
                "reason": "copilot-finding-budget-exhausted",
            }
    persisted = _only_journal_row(state)
    assert "status" not in persisted
    assert JobRegistry(state_path=state.parent / "jobs.json").list_workflow_runs()[0].facets == (
        "needs_human",
    )
    assert persisted["ship"]["fix_rounds"] == 2


def test_ship_fix_required_persists_capped_reviewer_findings(
    monkeypatch, tmp_path: Path
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )

    threads = tuple(
        ReviewThread(
            thread_id=f"thread-{index}",
            resolved=False,
            outdated=False,
            path=f"src/file_{index}.py",
            line=100 + index,
            body_excerpt=f"finding {index}",
        )
        for index in range(12)
    )
    current = {"review_ready": False}

    class GitHub:
        def __init__(self, *, runner):
            pass

        def ensure_pr_metadata(self, **kwargs):
            pass

        def fetch_delivery_facts(self, **kwargs):
            reviews = ()
            if current["review_ready"]:
                reviews = (
                    CopilotReview(
                        review_id=9,
                        commit_id=HEAD,
                        state="COMMENTED",
                        body="findings",
                        author=COPILOT_REVIEWER_LOGIN,
                        submitted_at_epoch=205.0,
                    ),
                )
            return DeliveryFacts(
                head=HEAD,
                mergeable=True,
                mergeable_state="clean",
                checks=(),
                copilot_reviews=reviews,
                review_threads=threads,
                closing_issues=(12,),
                active_openspec_absent=True,
                archive_present=True,
            )

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(False, HEAD, None)

        def request_copilot(self, **kwargs):
            pass

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: PreflightResult(
            True,
            None,
            CommandResult(("policy",), 0, "", ""),
            CommandResult(("preflight",), 0, "", ""),
            HEAD,
            TREE,
        ),
    )
    args = {
        "action": "ship",
        "repo": "acme/demo",
        "work_id": "demo",
        "repo_root": str(tmp_path),
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
        "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
    }

    requested = work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert requested["result"]["action"] == "awaiting-copilot"

    current["review_ready"] = True
    reviewed = work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 207,
    )

    assert reviewed["result"] == {
        "action": "fix-required",
        "reason": "copilot-findings",
        "findings": 12,
    }
    persisted = _only_journal_row(state)
    assert persisted["ship"]["phase"] == "needs-fix"
    assert persisted["ship"]["findings"] == [
        {"path": f"src/file_{index}.py", "line": 100 + index, "body": f"finding {index}"}
        for index in range(10)
    ]


def test_external_merge_without_durable_authorization_needs_human(monkeypatch, tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    merge_calls = []

    class GitHub:
        def __init__(self, *, runner):
            pass

        def ensure_pr_metadata(self, **kwargs):
            pass

        def fetch_delivery_facts(self, **kwargs):
            return DeliveryFacts(
                head=HEAD,
                mergeable=False,
                mergeable_state="unknown",
                checks=(),
                copilot_reviews=(),
                review_threads=(),
                closing_issues=(12,),
                active_openspec_absent=True,
                archive_present=True,
            )

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(True, HEAD, "c" * 40)

        def request_copilot(self, **kwargs):
            raise AssertionError("merged PR must not request review")

    class Orchestrator:
        def __init__(self, **kwargs):
            pass

        def merge_if_ready(self, **kwargs):
            merge_calls.append(kwargs)
            raise AssertionError("merged PR must not merge again")

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", Orchestrator)
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: PreflightResult(
            True,
            None,
            CommandResult(("policy",), 0, "", ""),
            CommandResult(("preflight",), 0, "", ""),
            HEAD,
            TREE,
        ),
    )
    result = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
                "pr_number": 8,
                "change": "demo",
                "todo_paths": ["docs/todo.md"],
                "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert result["result"] == {
        "action": "needs_human",
        "reason": "external-merge-without-authorization",
    }
    assert merge_calls == []


def test_crash_reconcile_uses_stable_authorization_without_rerunning_preflight(
    monkeypatch, tmp_path: Path
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    _initialize_delivery_journal(snapshot=snapshot, state=state)
    persisted = json.loads(state.read_text(encoding="utf-8"))
    run = next(iter(persisted["runs"].values()))
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    authorization = work_actions._authorization_record(
        {
            "schema": "cortex-merge-authorization/v1",
            "run_id": run["run_id"],
            "workflow_step_ids": run["workflow_step_ids"],
            "repo": "acme/demo",
            "work_id": "demo",
            "authority_digest": work_actions.work_authority_digest(authority),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": ["docs/todo.md"],
            "head": HEAD,
            "tree_hash": TREE,
            "copilot_requested_at_epoch": 200.0,
            "copilot_review_id": 9,
            "copilot_hash": "1" * 64,
            "foreign_review_path": "/evidence/review.json",
            "foreign_review_hash": "2" * 64,
            "preflight_hash": "3" * 64,
            "checks_hash": "4" * 64,
        },
        state_path=state,
    )
    run["delivery_binding"] = {
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
    }
    run["ship"] = {
        "phase": "merge-authorized",
        "head": HEAD,
        "tree_hash": TREE,
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
        "merge_authorization": authorization,
    }
    state.write_text(json.dumps(persisted), encoding="utf-8")

    class GitHub:
        def __init__(self, *, runner):
            pass

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(True, HEAD, "c" * 40)

        def ensure_pr_metadata(self, **kwargs):
            raise AssertionError("crash reconciliation must not rewrite PR metadata")

    class Orchestrator:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", Orchestrator)
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("crash reconciliation must not rerun preflight")
        ),
    )
    result = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": ["docs/todo.md"],
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 220,
    )
    assert result["result"]["action"] == "merged-awaiting-closure"


def test_v2_authorization_rejects_tampered_maintainer_review_on_replay(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    active = {"run_id": "workflow-" + "a" * 20, "workflow_step_ids": ["review", "ship"]}
    binding = {"pr_number": 8, "change": "demo", "todo_paths": ["docs/todo.md"]}
    review = tmp_path / "maintainer-review.json"
    review_payload = {"schema": "maintainer-review/v1", "candidate": HEAD, "verdict": "approved"}
    review.write_text(json.dumps(review_payload), encoding="utf-8")
    review.chmod(0o444)
    body = {
        "schema": "cortex-merge-authorization/v2",
        "run_id": active["run_id"],
        "workflow_step_ids": active["workflow_step_ids"],
        "repo": "acme/demo",
        "work_id": "demo",
        "authority_digest": work_actions.work_authority_digest(authority),
        **binding,
        "head": HEAD,
        "tree_hash": TREE,
        "review_kind": "maintainer-review",
        "review_ref": str(review),
        "review_hash": work_actions.verification.canonical_json_hash(review_payload),
        "foreign_review_path": str(tmp_path / "foreign.json"),
        "foreign_review_hash": "2" * 64,
        "preflight_hash": "3" * 64,
        "checks_hash": "4" * 64,
    }
    authorization = work_actions._authorization_record(body, state_path=state)
    assert work_actions._authorization_identity_matches(
        authorization,
        active=active,
        authority=authority,
        binding=binding,
        head=HEAD,
        tree_hash=TREE,
    )

    terminal_authority = authority.__class__._verified(
        repo=authority.repo,
        work_id=authority.work_id,
        mapped_issues=authority.mapped_issues,
        mapped_prs=authority.mapped_prs,
        mapped_openspec=authority.mapped_openspec,
        mapped_todo_paths=authority.mapped_todo_paths,
        confirmed_todo=authority.confirmed_todo,
        auto_label=authority.auto_label,
        snapshot_hash="9" * 64,
        source_revisions=(*authority.source_revisions, "github-pr:8@state:closed"),
        provider_id=authority.github_provider_id,
        provider_revision=authority.github_provider_revision,
        last_success_epoch=authority.github_last_success_epoch,
    )
    assert work_actions.work_authority_digest(terminal_authority) != body["authority_digest"]
    assert not work_actions._authorization_identity_matches(
        authorization,
        active=active,
        authority=terminal_authority,
        binding=binding,
        head=HEAD,
        tree_hash=TREE,
    )
    assert work_actions._authorization_identity_matches(
        authorization,
        active=active,
        authority=terminal_authority,
        binding=binding,
        head=HEAD,
        tree_hash=TREE,
        terminal_reconciliation=True,
    )

    review.chmod(0o600)
    review.write_text(json.dumps({**review_payload, "verdict": "rejected"}), encoding="utf-8")
    review.chmod(0o444)

    assert not work_actions._authorization_identity_matches(
        authorization,
        active=active,
        authority=terminal_authority,
        binding=binding,
        head=HEAD,
        tree_hash=TREE,
        terminal_reconciliation=True,
    )


@pytest.mark.parametrize("use_replacement", [False, True])
def test_cached_done_replays_remote_closure_instead_of_trusting_local_state(
    monkeypatch, tmp_path: Path, use_replacement: bool
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    _initialize_delivery_journal(snapshot=snapshot, state=state)
    persisted = json.loads(state.read_text(encoding="utf-8"))
    run = next(iter(persisted["runs"].values()))
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    authorization = work_actions._authorization_record(
        {
            "schema": "cortex-merge-authorization/v1",
            "run_id": run["run_id"],
            "workflow_step_ids": run["workflow_step_ids"],
            "repo": "acme/demo",
            "work_id": "demo",
            "authority_digest": work_actions.work_authority_digest(authority),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": ["docs/todo.md"],
            "head": HEAD,
            "tree_hash": TREE,
            "copilot_requested_at_epoch": 200.0,
            "copilot_review_id": 9,
            "copilot_hash": "1" * 64,
            "foreign_review_path": "/evidence/review.json",
            "foreign_review_hash": "2" * 64,
            "preflight_hash": "3" * 64,
            "checks_hash": "4" * 64,
        },
        state_path=state,
    )
    run["delivery_binding"] = {
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
    }
    run["ship"] = {
        "phase": "done",
        "head": HEAD,
        "tree_hash": TREE,
        "todo_paths": ["docs/todo.md"],
        "merge_authorization": authorization,
        "completion_record": {"path": "/evidence/record.json", "hash": "d" * 64},
    }
    state.write_text(json.dumps(persisted), encoding="utf-8")
    closure_calls = []
    replacement = tmp_path / "replacement-completion.json"
    replacement.write_text(json.dumps({"record": "replacement"}), encoding="utf-8")

    class GitHub:
        def __init__(self, *, runner):
            pass

    class Orchestrator:
        def __init__(self, **kwargs):
            pass

        def verify_remote_closure(self, **kwargs):
            closure_calls.append(kwargs)
            return SimpleNamespace(
                facts=SimpleNamespace(merge_commit="c" * 40),
                completion_record={
                    "path": (
                        "/evidence/replacement-record.json"
                        if use_replacement
                        else "/evidence/record.json"
                    ),
                    "hash": ("e" if use_replacement else "d") * 64,
                },
            )

    from paulsha_cortex.coordinator import completion

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", Orchestrator)
    cached_reads = []

    def read_cached(*args, **kwargs):
        cached_reads.append((args, kwargs))
        return {"record": True}

    monkeypatch.setattr(completion, "read_completion_record", read_cached)
    ship_args = {
        "action": "ship",
        "repo": "acme/demo",
        "work_id": "demo",
        "repo_root": str(tmp_path),
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
        "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
    }
    if use_replacement:
        ship_args["completion_record_path"] = str(replacement)
    result = work_actions.execute_work_action(
        args=ship_args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    assert result["result"]["action"] == "done"
    assert len(closure_calls) == 1
    assert closure_calls[0]["authority"].snapshot_hash == run["snapshot_hash"]
    assert closure_calls[0]["completion_payload"] == (
        {"record": "replacement"} if use_replacement else {"record": True}
    )
    assert len(cached_reads) == (0 if use_replacement else 1)
    refreshed = json.loads(state.read_text(encoding="utf-8"))
    refreshed_run = next(iter(refreshed["runs"].values()))
    assert refreshed_run["ship"]["completion_record"] == result["result"]["completion_record"]


@pytest.mark.parametrize(
    ("tasks", "title", "body", "reason"),
    [
        ("- [ ] pending\n", "fix(work): 修正工作流程", "Closes #12", "tasks-incomplete"),
        ("- [x] done\n", "fix(work): invalid", "Closes #12", "PR metadata blocked"),
        ("- [x] done\n", "fix(work): 修正工作流程", "Relates #12", "PR metadata blocked"),
    ],
)
def test_archive_and_pr_metadata_fail_closed_before_mutation(
    tmp_path: Path, tasks: str, title: str, body: str, reason: str
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    change = tmp_path / "openspec" / "changes" / "demo"
    change.mkdir(parents=True)
    (change / "tasks.md").write_text(tasks, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n- **demo**: done\n", encoding="utf-8"
    )
    metadata = _pr_metadata(tmp_path / "pr.json", title=title, body=body)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match=reason):
        work_actions.execute_work_action(
            args={
                "action": "ship",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(tmp_path),
                "pr_number": 8,
                "change": "demo",
                "todo_paths": ["docs/todo.md"],
                "pr_metadata_path": str(metadata),
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
            runner=runner,
        )
    assert ["openspec", "archive", "-y", "demo"] not in calls


def test_archive_requires_change_specific_changelog(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    change_dir = tmp_path / "openspec" / "changes" / "demo"
    change_dir.mkdir(parents=True)
    (change_dir / "tasks.md").write_text("- [x] complete\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n- **other**: done\n", encoding="utf-8"
    )

    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="changelog-missing"):
        work_actions.execute_work_action(
            args={
                "action": "ship",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(tmp_path),
                "pr_number": 8,
                "change": "demo",
                "todo_paths": ["docs/todo.md"],
                "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
            runner=runner,
        )


def test_archive_allows_advisory_r22_doc_reference_warning(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    change_dir = tmp_path / "openspec" / "changes" / "demo"
    change_dir.mkdir(parents=True)
    (change_dir / "tasks.md").write_text("- [x] complete\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n- **demo**: done\n", encoding="utf-8"
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["python3", "-m", "policy_check"]:
            return SimpleNamespace(
                returncode=0,
                stdout="WARN R-22 doc-reference stale link",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = work_actions.execute_work_action(
        args={
            "action": "ship",
            "repo": "acme/demo",
            "work_id": "demo",
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": "demo",
            "todo_paths": ["docs/todo.md"],
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=runner,
    )

    assert result["result"]["action"] == "archive-applied-needs-commit"
    assert calls[-1] == ["openspec", "archive", "-y", "demo"]


def test_archive_blocks_nonzero_policy_check_with_doc_reference_invalid(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    change_dir = tmp_path / "openspec" / "changes" / "demo"
    change_dir.mkdir(parents=True)
    (change_dir / "tasks.md").write_text("- [x] complete\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n- **demo**: done\n", encoding="utf-8"
    )

    def runner(argv, **kwargs):
        if argv[:3] == ["python3", "-m", "policy_check"]:
            return SimpleNamespace(
                returncode=1,
                stdout="WARN R-22 doc-reference stale link",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="doc-reference-invalid"):
        work_actions.execute_work_action(
            args={
                "action": "ship",
                "repo": "acme/demo",
                "work_id": "demo",
                "repo_root": str(tmp_path),
                "pr_number": 8,
                "change": "demo",
                "todo_paths": ["docs/todo.md"],
                "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 200,
            runner=runner,
        )
