from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager, work_actions
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
from paulsha_cortex.coordinator.workflow import GateEvidenceRef, PlanningArtifactAuthority

from diagnostic_fixtures import fixture_needs_human_reason


HEAD = "a" * 40
TREE = "b" * 40


def test_canonical_workflow_run_ignores_superseded_history(tmp_path: Path) -> None:
    """Delivery binds the sole live run, not identical historical refs."""

    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    refs = {
        "repo": authority.repo,
        "work_id": authority.work_id,
        "source_revision": work_actions.work_authority_digest(authority),
        "issue_refs": tuple(f"{authority.repo}#{n}" for n in authority.mapped_issues),
        "openspec_refs": authority.mapped_openspec,
        "pr_refs": tuple(f"{authority.repo}#{n}" for n in authority.mapped_prs),
    }
    historical = SimpleNamespace(**refs, status="superseded")
    active = SimpleNamespace(**refs, status="ongoing")
    registry = SimpleNamespace(list_workflow_runs=lambda: [historical, active])

    assert work_actions._canonical_workflow_run(
        workflow_registry=registry, authority=authority
    ) is active


def test_canonical_workflow_run_accepts_run_owned_openspec_overlay(
    tmp_path: Path,
) -> None:
    """Planning-created OpenSpec refs must not orphan the live workflow."""

    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    step = SimpleNamespace(
        phase="define",
        card="openspec-propose",
        outputs=("openspec/changes/demo/proposal.md",),
    )
    active = SimpleNamespace(
        repo=authority.repo,
        work_id=authority.work_id,
        source_revision=work_actions.work_authority_digest(authority),
        issue_refs=tuple(f"{authority.repo}#{n}" for n in authority.mapped_issues),
        openspec_refs=(),
        pr_refs=tuple(f"{authority.repo}#{n}" for n in authority.mapped_prs),
        steps=(step,),
        status="ongoing",
    )
    registry = SimpleNamespace(list_workflow_runs=lambda: [active])

    assert work_actions._canonical_workflow_run(
        workflow_registry=registry, authority=authority
    ) is active


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


def _seed_verified_run_with_gate(
    registry: JobRegistry,
    run_id: str,
    *,
    phase: str = "verify",
    pr_refs: tuple[str, ...] = (),
    facets: tuple[str, ...] = ("needs_human",),
    verify_attempts: int = 2,
) -> object:
    for current in ("plan", "build", "verify", "review"):
        if current == "review" and phase != "review":
            break
        registry._manager_update_workflow_run(run_id, current_phase=current)
    attempts = {"claim": 1, "plan": 1, "build": 1, "verify": verify_attempts}
    if phase == "review":
        attempts["review"] = 1
    return registry._manager_update_workflow_run(
        run_id,
        candidate_head=HEAD,
        verified_head=HEAD,
        pr_refs=pr_refs,
        attempts=attempts,
        evidence_refs=("reports/verify/accepted.md",),
        gate_refs=(GateEvidenceRef("foreign-review", "reports/review/accepted.md", "e" * 64),),
        gate_status="passed",
        facets=facets,
        needs_human_reason=fixture_needs_human_reason(),
    )


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


def test_periodic_auto_claim_scan_retries_run_stuck_at_define(tmp_path: Path) -> None:
    """issue #420 RED→GREEN：auto-claim 建立的 run 若在 `apply_workflow_action
    (action="start")` 的 claim→define→plan 同步續推段中途被中斷（例如
    `_load_planning_artifacts` 一類、未被任何 needs_human 分支接住的例外），
    會停在 `current_phase="define"`、facets 乾淨、`brainstorm_required`
    仍是預設 False——`decide_auto_claim` 對這個 authority 之後每輪都只會判定
    `_resume_decision` 的 `action="resume"`（因為 `active_status=="ongoing"`）。

    修復前：`_claim_action` 的既有-run 分支只在 `args["action"] == "resume"`
    （即人工經 `cortex work resume` 觸發）才重呼叫 `workflow_starter`；
    periodic auto-claim scan 固定帶 `args={"action": "auto-scan"}`，永遠不滿足
    這個字面比對，導致 `workflow_starter` 永遠不會再被呼叫、run 永久卡在
    define——這正是 explicit intake 在同一 request 內能同步跑 define→plan→
    build、而 auto-claim 建的 run 卻無人接手續推的根因。

    修復後：`automatic=True`（auto-claim scan 的呼叫方式）且
    `decision.action == "resume"` 時也觸發同樣的重試，讓下一輪 periodic tick
    的 auto-claim scan 能把這個 run 續推到 plan（甚至更後面）。
    """
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    # reason=None -> facets=()，模擬「define 已建立、但續推被中斷」的乾淨卡住
    # 狀態，不是 needs_human。
    stuck = work_actions._fallback_workflow_starter(
        registry, tmp_path / "runs.json"
    )(authority, claim_key, None)
    assert stuck.current_phase == "define"
    assert stuck.facets == ()
    assert stuck.brainstorm_required is False

    calls: list[tuple[str, str | None]] = []

    def retrying_starter(bound_authority, bound_claim_key, reason):
        calls.append((bound_claim_key, reason))
        # 模擬 start_canonical_workflow 重跑一次 define 續推段這次成功，
        # 推進到 plan（比照 apply_workflow_action 的 report.complete 分支）。
        return registry._manager_update_workflow_run(
            stuck.run_id,
            current_phase="plan",
            attempts={**stuck.attempts, "plan": 1},
            brainstorm_required=False,
            facets=(),
        )

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        now=lambda: 200,
        runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "cortex:auto-on-going"}]}),
            stderr="",
        ),
        workflow_registry=registry,
        workflow_starter=retrying_starter,
    )

    assert calls == [(claim_key, None)]
    assert result[0]["action"] == "resume"
    assert result[0]["run"]["run_id"] == stuck.run_id
    assert result[0]["run"]["current_phase"] == "plan"
    assert registry.get_workflow_run(stuck.run_id).current_phase == "plan"


def test_periodic_auto_claim_scan_does_not_retry_needs_human_run(tmp_path: Path) -> None:
    """#420 修復的邊界：只有 `decision.action == "resume"`（active_status ==
    "ongoing"，facets 乾淨）才在 auto-claim scan 觸發重試。needs_human run
    的 `decision.action == "needs_human"`，不受影響——維持 #373 的守衛：
    auto-claim 不得自動清除或重試 needs_human run，仍須等人工
    `cortex work resume` 接手。
    """
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    stuck = work_actions._fallback_workflow_starter(
        registry, tmp_path / "runs.json"
    )(authority, claim_key, "planning-runtime-unavailable")
    assert stuck.facets == ("needs_human",)

    def forbidden_starter(*args, **kwargs):
        raise AssertionError(
            "auto-claim scan must not re-invoke workflow_starter for a "
            "needs_human run"
        )

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        now=lambda: 200,
        runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "cortex:auto-on-going"}]}),
            stderr="",
        ),
        workflow_registry=registry,
        workflow_starter=forbidden_starter,
    )

    assert result[0]["action"] == "needs_human"
    assert registry.get_workflow_run(stuck.run_id).current_phase == "define"
    assert registry.get_workflow_run(stuck.run_id).facets == ("needs_human",)


def test_retry_build_requires_exact_candidate_and_resets_downstream_authority(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        source_revisions=("issue:12@open",),
        changes=(),
    )
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
        needs_human_reason=fixture_needs_human_reason(),
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
    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        source_revisions=("issue:12@open",),
        changes=(),
    )
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
        needs_human_reason=fixture_needs_human_reason(),
    )

    result = work_actions.execute_work_action(
        args={
            "action": "retry-build",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_candidate": HEAD,
            "builder_executor": "codex",
            "builder_model": "gpt-5.6-luna",
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )

    reset = registry.get_workflow_run(initial.run_id)
    assert result["result"]["action"] == "retry-build"
    assert reset.current_phase == "build"
    assert reset.model_chain_override == {
        "builder": {"executor": "codex", "model_id": "gpt-5.6-luna"}
    }
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
    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        source_revisions=("issue:12@open",),
        changes=(),
    )
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
        needs_human_reason=fixture_needs_human_reason(),
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


def _pr_lifecycle_runner(states: dict[int, dict]):
    """Fake ``gh`` runner answering ``gh api repos/<repo>/pulls/<N>`` from a
    map of PR number -> raw pull payload (``state`` / ``merged_at``)."""

    def runner(argv, **kwargs):
        assert argv[:2] == ["gh", "api"], argv
        match = re.search(r"/pulls/(\d+)$", argv[2])
        assert match is not None, argv[2]
        payload = states.get(int(match.group(1)))
        if payload is None:
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    return runner


def _canonical_rate_limited_snapshot(path: Path, *, revision="gh-rev-lkg") -> Path:
    """Canonical-schema snapshot whose ``github:acme/demo`` provider is degraded
    by a *rate limit* yet still carries a last-known-good revision/timestamp."""

    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github:acme/demo": {
                        "status": "degraded",
                        "revision": revision,
                        "last_success_at": "2026-08-07T11:19:26Z",
                        "diagnostics": [
                            "github rate limit exceeded",
                            "github:acme/demo stale",
                        ],
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "sources": [
                            {
                                "confidence": "confirmed",
                                "kind": "todo",
                                "ref": "docs/todo.md",
                                "source_id": "todo:demo",
                                "revision": "todo-rev-1",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_retire_delivered_supersedes_ongoing_run_when_all_prs_terminal(
    tmp_path: Path,
) -> None:
    """Gap 1: a delivered orphan (``ongoing`` + terminal ``pr_refs``) that the
    pre-delivery ``abandon`` gate refuses gets an explicit, evidence-backed
    retirement — but only once *every* PR is proven terminal."""

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
    registry._manager_update_workflow_run(
        run_id, pr_refs=("acme/demo#110", "acme/demo#171")
    )

    args = {
        "action": "retire-delivered",
        "repo": "acme/demo",
        "work_id": "demo",
        "actor": "operator",
        "expected_run_id": run_id,
        "reason": "Delivered outside the pipeline; PRs terminal.",
    }

    # A run with no pr_refs is not a delivered orphan — use abandon instead.
    registry._manager_update_workflow_run(run_id, pr_refs=())
    with pytest.raises(RuntimeError, match="requires a delivered run with pr refs"):
        work_actions.execute_work_action(
            args=args,
            requested_by="operator",
            runner=_pr_lifecycle_runner({}),
            snapshot_path=snapshot,
            state_path=state,
            workflow_registry=registry,
        )
    registry._manager_update_workflow_run(
        run_id, pr_refs=("acme/demo#110", "acme/demo#171")
    )

    # A still-open PR must refuse and leave the run untouched (no evidence).
    with pytest.raises(RuntimeError, match="non-terminal PR"):
        work_actions.execute_work_action(
            args=args,
            requested_by="operator",
            runner=_pr_lifecycle_runner(
                {
                    110: {"state": "closed", "merged_at": "2026-08-01T00:00:00Z"},
                    171: {"state": "open", "merged_at": None},
                }
            ),
            snapshot_path=snapshot,
            state_path=state,
            workflow_registry=registry,
        )
    assert not (state.parent / "evidence" / "work-retire-delivered").exists()
    assert registry.get_workflow_run(run_id).status == "ongoing"

    terminal_runner = _pr_lifecycle_runner(
        {
            110: {"state": "closed", "merged_at": "2026-08-01T00:00:00Z"},
            171: {"state": "closed", "merged_at": None},
        }
    )
    first = work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        runner=terminal_runner,
        snapshot_path=snapshot,
        state_path=state,
        workflow_registry=registry,
    )
    retired = registry.get_workflow_run(run_id)
    assert retired.status == "superseded"
    assert "blocked" in retired.facets
    assert retired.completion_record_path is None
    assert first["result"]["action"] == "retired-delivered"
    assert first["result"]["pr_terminal_status"] == [
        {"ref": "acme/demo#110", "state": "merged"},
        {"ref": "acme/demo#171", "state": "closed_unmerged"},
    ]
    evidence = Path(first["result"]["evidence"]["ref"])
    assert evidence.is_file()
    assert evidence.stat().st_mode & 0o222 == 0
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["schema"] == "cortex-work-retire-delivered/v1"
    assert payload["run_id"] == run_id
    assert payload["actor"] == "operator"
    assert payload["reason"] == args["reason"]
    assert payload["pr_terminal_status"] == first["result"]["pr_terminal_status"]
    assert str(evidence) in retired.evidence_refs

    # Idempotent re-entry re-derives the proof from the durable evidence
    # (no GitHub call), so it stays sound even if the provider is throttled.
    second = work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        runner=_pr_lifecycle_runner({}),
        snapshot_path=snapshot,
        state_path=state,
        workflow_registry=registry,
    )
    assert first == second

    # A different reason must not silently mint a second retirement.
    with pytest.raises(RuntimeError, match="different authority"):
        work_actions.execute_work_action(
            args={**args, "reason": "A different reason."},
            requested_by="operator",
            runner=terminal_runner,
            snapshot_path=snapshot,
            state_path=state,
            workflow_registry=registry,
        )


def test_abandon_still_refuses_any_pr_refs_run(tmp_path: Path) -> None:
    """Gap 1 guard: the new retire path must NOT weaken abandon — a run with
    pr_refs is still rejected by the strict pre-delivery gate."""

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
    registry._manager_update_workflow_run(run_id, pr_refs=("acme/demo#8",))
    with pytest.raises(ValueError, match="only permits pre-delivery"):
        work_actions.execute_work_action(
            args={
                "action": "abandon",
                "repo": "acme/demo",
                "work_id": "demo",
                "issue": 12,
                "actor": "operator",
                "expected_run_id": run_id,
                "reason": "should be refused",
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            workflow_registry=registry,
        )


def test_retire_delivered_proceeds_under_rate_limited_authority(tmp_path: Path) -> None:
    """Gap 2: retirement tolerates a canonical GitHub authority degraded purely
    by a rate limit (last-known-good present), while non-retirement actions on
    the same throttled snapshot still fail closed."""

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
    registry._manager_update_workflow_run(run_id, pr_refs=("acme/demo#8",))

    # The provider is now rate-limited (canonical schema, last-known-good kept).
    _canonical_rate_limited_snapshot(snapshot)

    from paulsha_cortex.coordinator.claim import (
        AuthorityValidationError,
        REASON_PROVIDER_RATE_LIMITED_CANONICAL,
    )

    # resume (needs fresh authority) must still fail closed under rate limit.
    with pytest.raises(AuthorityValidationError) as excinfo:
        work_actions.execute_work_action(
            args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            workflow_registry=registry,
        )
    assert excinfo.value.reason_code == REASON_PROVIDER_RATE_LIMITED_CANONICAL

    # retire-delivered proceeds anyway using the last-known-good authority.
    result = work_actions.execute_work_action(
        args={
            "action": "retire-delivered",
            "repo": "acme/demo",
            "work_id": "demo",
            "actor": "operator",
            "expected_run_id": run_id,
            "reason": "Throttled but must still clear the orphan.",
        },
        requested_by="operator",
        runner=_pr_lifecycle_runner(
            {8: {"state": "closed", "merged_at": "2026-08-01T00:00:00Z"}}
        ),
        snapshot_path=snapshot,
        state_path=state,
        workflow_registry=registry,
    )
    assert result["result"]["action"] == "retired-delivered"
    assert registry.get_workflow_run(run_id).status == "superseded"


def test_abandon_orphan_rescue_allows_refs_drift_when_authority_lost_all_mappings(
    tmp_path: Path,
) -> None:
    """issue #410（孤兒救援窄放行）：work item 改名／重識別後，舊識別的
    authority 失去 issue 與 openspec 映射（tombstone row 只剩 path 錨點），
    run 的 refs 與 authority 恆不相等——嚴格相等守衛使孤兒 run 永遠不可
    abandon。僅在「authority 兩類映射皆空、run 仍留 refs」的孤兒簽名下放行；
    authority 映射非空但內容不同（真正的 refs 漂移）仍必須 fail-closed。"""
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
    args = {
        "action": "abandon",
        "repo": "acme/demo",
        "work_id": "demo",
        "actor": "operator",
        "expected_run_id": run_id,
        "reason": "issue 410 孤兒救援：改名後清理舊識別的 ongoing run。",
    }

    # 真正的 refs 漂移（authority 仍有映射、只是內容不同）必須維持 fail-closed。
    drifted = _snapshot(
        tmp_path / "snapshot-drifted.json",
        issues=(13,),
        source_revisions=("issue:13@open", "openspec:demo@1"),
    )
    with pytest.raises(RuntimeError, match="refs differ"):
        work_actions.execute_work_action(
            args=args,
            requested_by="operator",
            snapshot_path=drifted,
            state_path=state,
            workflow_registry=registry,
        )

    # 孤兒簽名：authority 失去全部 issue／openspec 映射（僅剩 todo 錨點）。
    orphaned = _snapshot(
        tmp_path / "snapshot-orphan.json",
        issues=(),
        changes=(),
        prs=(),
        source_revisions=("todo:docs/todo.md@1",),
    )
    result = work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        snapshot_path=orphaned,
        state_path=state,
        workflow_registry=registry,
    )
    assert result["result"]["action"] == "abandoned"
    rescued = registry.get_workflow_run(run_id)
    assert rescued.status == "superseded"


def _start_run_with_planning_authority(
    tmp_path: Path,
    *,
    registry: JobRegistry,
    ref: str,
    content: bytes,
    kind: str = "spec",
) -> tuple[str, Path]:
    """啟動一個 run，並把 `ref` 綁定為它的 planning_authority——模擬 brainstorm
    define 已成功發佈該檔案到 workspace_root（未 git 提交）。回傳 (run_id,
    絕對路徑)。"""

    snapshot = _snapshot(tmp_path / "snapshot.json", prs=())
    state = tmp_path / "runs.json"
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
    workspace_root = Path(run.workspace_root)
    target = workspace_root / ref
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    registry._manager_update_workflow_run(
        run_id,
        planning_authority=(
            PlanningArtifactAuthority(
                ref=ref,
                kind=kind,
                work_id="demo",
                baseline_sha256=hashlib.sha256(content).hexdigest(),
            ),
        ),
    )
    return run_id, target


def _abandon_args(run_id: str) -> dict:
    return {
        "action": "abandon",
        "repo": "acme/demo",
        "work_id": "demo",
        "issue": 12,
        "actor": "operator",
        "expected_run_id": run_id,
        "reason": "issue 416：清掉未提交 planning artifact 殘留的回歸測試。",
    }


def test_abandon_gc_removes_untracked_planning_artifact_matching_baseline_hash(
    tmp_path: Path,
) -> None:
    """issue #416：run 被 abandon 前，brainstorm define 已把 spec/design 發佈到
    workspace_root（未 git 提交）。abandon 之前沒人回滾這些殘留——下一世代
    重新 claim、brainstorm 再對同一 destinations 發佈時，`_publish_planning_
    artifacts` 對「檔案已存在但無對應 authority」一律 fail-closed 拒收
    （`lacks current planning authority`），殘留檔變成死鎖地雷。

    這裡驗證 abandon 之後，未追蹤且 hash 與發佈時 baseline 相符的殘留檔會被
    回滾刪除——RED（修法前）：`_abandon_action` 完全不知道 planning_authority
    的存在，檔案在 abandon 之後依然原封不動留著。
    """

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    ref = "docs/superpowers/specs/fix-416-spec.md"
    content = b"# Fix 416 Spec\n\n## Requirements\n\ndemo\n"
    run_id, target = _start_run_with_planning_authority(
        tmp_path, registry=registry, ref=ref, content=content,
    )
    assert target.is_file()

    work_actions.execute_work_action(
        args=_abandon_args(run_id),
        requested_by="operator",
        snapshot_path=tmp_path / "snapshot.json",
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )

    abandoned = registry.get_workflow_run(run_id)
    assert abandoned.status == "superseded"
    assert not target.exists(), (
        "abandon 之後未追蹤、hash 相符的殘留 planning artifact 必須被回滾刪除"
        "（issue #416：否則下一世代 brainstorm 重新發佈同一路徑時會被 authority"
        " 檢查必拒）"
    )


def test_abandon_reconciles_uncommitted_planning_publication_journal(
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
    transaction = manager._PlanningPublicationTransaction(
        root=Path(run.workspace_root),
        run_id=run_id,
        journal_root=tmp_path,
    )
    target = Path(run.workspace_root) / "docs/superpowers/specs/fix-558-residue.md"
    transaction.publish(
        target,
        b"# Uncommitted publication residue\n",
        baseline_hash=None,
        kind="artifact",
    )
    assert transaction.journal_path is not None

    work_actions.execute_work_action(
        args=_abandon_args(run_id),
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        workflow_registry=registry,
    )

    assert not target.exists(), "abandon 必須由 transaction journal 回收未提交發佈殘留"
    assert not transaction.journal_path.exists()


def test_abandon_gc_preserves_planning_artifact_with_hash_drift(tmp_path: Path) -> None:
    """operator 手動改過殘留檔（現存內容與發佈時 baseline_sha256 不符）時，GC
    必須留檔並且不得誤刪——不可信的 drift 交給人工判斷，不能悄悄清掉。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    ref = "docs/superpowers/specs/fix-416-drift-spec.md"
    original = b"# Fix 416 Spec\n\n## Requirements\n\noriginal\n"
    run_id, target = _start_run_with_planning_authority(
        tmp_path, registry=registry, ref=ref, content=original,
    )
    target.write_bytes(b"# Fix 416 Spec\n\n## Requirements\n\noperator edited this\n")

    work_actions.execute_work_action(
        args=_abandon_args(run_id),
        requested_by="operator",
        snapshot_path=tmp_path / "snapshot.json",
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )

    abandoned = registry.get_workflow_run(run_id)
    assert abandoned.status == "superseded"
    assert target.is_file(), "hash 不符（operator 手改）的殘留檔必須保留，不可誤刪"
    assert b"operator edited this" in target.read_bytes()


def test_abandon_gc_preserves_git_tracked_planning_artifact(tmp_path: Path) -> None:
    """已被 git 追蹤的檔案（例如 operator 刻意 commit 過）不屬於「未提交殘留」
    範圍，GC 必須完全不碰它——只清未追蹤的發佈殘留。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    ref = "docs/superpowers/specs/fix-416-tracked-spec.md"
    content = b"# Fix 416 Spec\n\n## Requirements\n\ntracked\n"
    run_id, target = _start_run_with_planning_authority(
        tmp_path, registry=registry, ref=ref, content=content,
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", ref], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "-m", "commit the planning artifact"],
        check=True,
    )

    work_actions.execute_work_action(
        args=_abandon_args(run_id),
        requested_by="operator",
        snapshot_path=tmp_path / "snapshot.json",
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )

    abandoned = registry.get_workflow_run(run_id)
    assert abandoned.status == "superseded"
    assert target.is_file(), "git 已追蹤的檔案必須保留，GC 只清未追蹤的發佈殘留"


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
    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        source_revisions=("issue:12@open", "openspec:demo@1"),
    )
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
    _seed_verified_run_with_gate(
        registry,
        run_id,
        phase="review",
        pr_refs=("acme/demo#8",),
        facets=("needs_human", "degraded"),
    )
    plan_ref = "docs/superpowers/plans/demo.md"
    plan_bytes = b"# Accepted plan\n"
    plan_path = Path(registry.get_workflow_run(run_id).workspace_root) / plan_ref
    plan_path.parent.mkdir(parents=True)
    plan_path.write_bytes(plan_bytes)
    registry._manager_update_workflow_run(
        run_id,
        planning_authority=(
            PlanningArtifactAuthority(
                ref=plan_ref,
                kind="plan",
                work_id="demo",
                baseline_sha256=hashlib.sha256(plan_bytes).hexdigest(),
            ),
        ),
    )
    original_claim = registry.get_workflow_run(run_id)
    _snapshot(
        snapshot,
        source_revisions=(
            "issue:12@open",
            "openspec:demo@1",
            f"superpowers_plan:acme/demo:{plan_ref}@identity:{plan_ref}",
        ),
        provider_revision="gh-2",
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
    assert persisted.claim_key == original_claim.claim_key
    assert persisted.source_revision == original_claim.source_revision
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
        needs_human_reason=fixture_needs_human_reason(),
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
    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        prs=(),
        source_revisions=("issue:12@open", "openspec:demo@1"),
    )
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
    run_id = first["result"]["run"]["run_id"]
    run = registry.get_workflow_run(run_id)
    plan_ref = "docs/superpowers/plans/demo.md"
    plan_bytes = b"# Accepted plan\n"
    plan_path = Path(run.workspace_root) / plan_ref
    plan_path.parent.mkdir(parents=True)
    plan_path.write_bytes(plan_bytes)
    registry._manager_update_workflow_run(
        run_id,
        planning_authority=(
            PlanningArtifactAuthority(
                ref=plan_ref,
                kind="plan",
                work_id="demo",
                baseline_sha256=hashlib.sha256(plan_bytes).hexdigest(),
            ),
        ),
    )
    before = _seed_verified_run_with_gate(
        registry, run_id, verify_attempts=3
    )
    initial_job_count = len(registry.list_jobs())
    _snapshot(
        snapshot,
        prs=(),
        source_revisions=(
            "issue:12@open",
            "openspec:demo@1",
            f"superpowers_plan:acme/demo:{plan_ref}@identity:{plan_ref}",
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
    assert result[0]["run"]["run_id"] == run_id
    assert len(registry.list_workflow_runs()) == 1
    after = registry.get_workflow_run(run_id)
    assert after.status == "ongoing"
    assert after.claim_key == before.claim_key
    assert after.source_revision == before.source_revision
    assert after.current_phase == before.current_phase
    assert after.candidate_head == before.candidate_head
    assert after.verified_head == before.verified_head
    assert after.steps == before.steps
    assert after.attempts == before.attempts
    assert after.evidence_refs == before.evidence_refs
    assert after.gate_refs == before.gate_refs
    assert after.gate_status == before.gate_status
    assert after.needs_human_reason == before.needs_human_reason
    assert len(registry.list_jobs()) == initial_job_count


def test_resume_does_not_reset_gates_when_manager_pr_becomes_authority_source(
    tmp_path: Path,
) -> None:
    original_sources = ("issue:12@open", "openspec:demo@1")
    snapshot = _snapshot(
        tmp_path / "snapshot.json", prs=(), source_revisions=original_sources
    )
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
    before = _seed_verified_run_with_gate(
        registry, run_id, phase="review", pr_refs=("acme/demo#17",)
    )
    initial_job_count = len(registry.list_jobs())
    pr_source = "github_pr:acme/demo#17@identity:acme/demo#17;state:open"
    _snapshot(
        snapshot,
        prs=(17,),
        source_revisions=(*original_sources, pr_source),
        provider_revision="gh-2",
    )

    result = work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )

    assert result["result"]["action"] == "resume"
    assert result["result"]["run"]["run_id"] == run_id
    after = registry.get_workflow_run(run_id)
    assert after.claim_key == before.claim_key
    assert after.source_revision == before.source_revision
    assert after.current_phase == before.current_phase
    assert after.candidate_head == before.candidate_head
    assert after.verified_head == before.verified_head
    assert after.steps == before.steps
    assert after.attempts == before.attempts
    assert after.gate_refs == before.gate_refs
    assert after.evidence_refs == before.evidence_refs
    assert after.needs_human_reason == before.needs_human_reason
    assert len(registry.list_jobs()) == initial_job_count


def test_resume_restarts_exactly_once_for_a_real_authority_change(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        prs=(),
        source_revisions=("issue:12@open", "openspec:demo@1"),
    )
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
    before = _seed_verified_run_with_gate(registry, run_id)
    _snapshot(
        snapshot,
        prs=(),
        source_revisions=("issue:12@closed", "openspec:demo@1"),
        provider_revision="gh-2",
    )

    first = work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )
    reset = registry.get_workflow_run(run_id)
    second = work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 220,
        workflow_registry=registry,
    )
    settled = registry.get_workflow_run(run_id)

    assert first["result"]["run"]["run_id"] == run_id
    assert second["result"]["run"]["run_id"] == run_id
    assert reset.claim_key == work_actions._expected_claim_key(
        work_actions.load_work_authority(
            repo="acme/demo", work_id="demo", snapshot_path=snapshot
        )
    )
    assert reset.source_revision != before.source_revision
    assert reset.attempts["verify"] == before.attempts["verify"] + 1
    assert reset.gate_refs == ()
    assert settled.attempts == reset.attempts
    assert settled.gate_refs == reset.gate_refs


def test_auto_scan_does_not_restart_existing_candidate_pr_after_authority_change(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        prs=(8,),
        source_revisions=("issue:12@open", "openspec:demo@1"),
    )
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
    before = _seed_verified_run_with_gate(
        registry,
        run_id,
        phase="review",
        pr_refs=("acme/demo#8",),
    )
    _snapshot(
        snapshot,
        prs=(8,),
        source_revisions=("issue:12@closed", "openspec:demo@1"),
        provider_revision="gh-2",
    )
    jobs_before = registry.list_jobs()

    results = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "cortex:auto-on-going"}]}),
            stderr="",
        ),
        workflow_registry=registry,
    )

    after = registry.get_workflow_run(run_id)
    assert results[0]["action"] == "blocked"
    assert results[0]["reason"] == "candidate-recovery-requires-explicit-resume"
    assert after == before
    assert registry.list_workflow_runs() == [before]
    assert registry.list_jobs() == jobs_before


_EXISTING_CANDIDATE_REPO = "acme/demo"
_EXISTING_CANDIDATE_RUN_ID = "workflow-52d048b72adbd5cae06f"
_EXISTING_CANDIDATE_HEAD = "7ba7e877c94ff4eee72ba796ea9f8962953ed5cc"
_EXISTING_CANDIDATE_PR = 1049


def _prepare_existing_candidate_recovery(tmp_path: Path):
    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        prs=(_EXISTING_CANDIDATE_PR,),
        source_revisions=("issue:12@open", "openspec:demo@1"),
    )
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["work_items"][0]["repo"] = _EXISTING_CANDIDATE_REPO
    payload["work_items"][0]["work_id"] = "demo"
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    state = tmp_path / "delivery-journal.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    started = work_actions.execute_work_action(
        args={
            "action": "start",
            "repo": _EXISTING_CANDIDATE_REPO,
            "work_id": "demo",
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    generated_id = started["result"]["run"]["run_id"]
    generated = registry.get_workflow_run(generated_id)
    fixture_run = replace(generated, run_id=_EXISTING_CANDIDATE_RUN_ID)
    registry._workflows[registry._find_workflow_run_index(generated_id)] = fixture_run
    registry._persist()
    reviewed = _seed_verified_run_with_gate(
        registry,
        _EXISTING_CANDIDATE_RUN_ID,
        phase="review",
        pr_refs=(f"{_EXISTING_CANDIDATE_REPO}#{_EXISTING_CANDIDATE_PR}",),
    )
    passed_steps = tuple(
        replace(
            step,
            gate_result="passed",
            domain=(
                "recovery-builder"
                if step.phase == "build"
                else "recovery-reviewer"
                if step.phase in {"verify", "review"}
                else step.domain
            ),
        )
        if step.phase in {"build", "verify", "review", "ship"}
        else step
        for step in reviewed.steps
    )
    before = registry._manager_update_workflow_run(
        reviewed.run_id,
        current_phase="ship",
        steps=passed_steps,
        gate_refs=(
            *reviewed.gate_refs,
            GateEvidenceRef("copilot", "/evidence/copilot.json", "d" * 64),
        ),
        candidate_head=_EXISTING_CANDIDATE_HEAD,
        verified_head=_EXISTING_CANDIDATE_HEAD,
    )
    old_authority = work_actions.load_work_authority(
        repo=_EXISTING_CANDIDATE_REPO,
        work_id="demo",
        snapshot_path=snapshot,
    )
    journal = work_actions._load_runs(state)
    row = work_actions._delivery_journal_row(before, old_authority)
    row["ship"] = {
        "phase": "needs_human",
        "reason": "multiple-delivery-targets-unsupported",
        "head": _EXISTING_CANDIDATE_HEAD,
        "pr_number": _EXISTING_CANDIDATE_PR,
    }
    journal["runs"][before.run_id] = row
    work_actions._save_runs(state, journal)

    _snapshot(
        snapshot,
        prs=(_EXISTING_CANDIDATE_PR,),
        source_revisions=("issue:12@closed", "openspec:demo@1", "todo:docs/todo.md@1"),
        provider_revision="gh-2",
    )
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["work_items"][0]["repo"] = _EXISTING_CANDIDATE_REPO
    payload["work_items"][0]["work_id"] = "demo"
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    return snapshot, state, registry, before


def _candidate_pr_read_runner(*, head: str, state: str = "open"):
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        assert argv[:2] == ["gh", "api"]
        assert argv[2] == (
            f"repos/{_EXISTING_CANDIDATE_REPO}/pulls/{_EXISTING_CANDIDATE_PR}"
        )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"state": state, "head": {"sha": head}, "merged_at": None}
            ),
            stderr="",
        )

    return runner, calls


def test_explicit_resume_restarts_same_candidate_and_pr_after_fresh_authority(
    tmp_path: Path,
) -> None:
    snapshot, state, registry, before = _prepare_existing_candidate_recovery(tmp_path)
    journal_before = work_actions._load_runs(state)
    runner, reads = _candidate_pr_read_runner(head=_EXISTING_CANDIDATE_HEAD)

    result = work_actions.execute_work_action(
        args={"action": "resume", "repo": _EXISTING_CANDIDATE_REPO, "work_id": "demo"},
        requested_by="operator",
        runner=runner,
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
        workflow_starter=lambda *_args: (_ for _ in ()).throw(
            AssertionError("recovery must only re-enter verify/review")
        ),
    )

    after = registry.get_workflow_run(before.run_id)
    assert result["result"]["action"] == "resume"
    assert after.current_phase == "verify"
    assert after.status == "ongoing"
    assert after.candidate_head == before.candidate_head
    assert after.pr_refs == before.pr_refs
    assert after.verified_head is None
    assert after.steps[0].gate_result == "passed"
    assert all(
        step.gate_result == "pending"
        for step in after.steps
        if step.phase in {"verify", "review"}
    )
    assert after.attempts["build"] == before.attempts["build"]
    assert after.attempts["verify"] == before.attempts["verify"] + 1
    journal_after = work_actions._load_runs(state)
    assert journal_after["revision"] == journal_before["revision"]
    assert journal_after["runs"][before.run_id] == journal_before["runs"][before.run_id]
    assert len(reads) == 4


def test_explicit_resume_fails_closed_when_existing_pr_head_differs(
    tmp_path: Path,
) -> None:
    snapshot, state, registry, before = _prepare_existing_candidate_recovery(tmp_path)
    journal_before = work_actions._load_runs(state)
    runner, reads = _candidate_pr_read_runner(head="f" * 40)

    result = work_actions.execute_work_action(
        args={"action": "resume", "repo": _EXISTING_CANDIDATE_REPO, "work_id": "demo"},
        requested_by="operator",
        runner=runner,
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )

    assert result["result"]["action"] == "blocked"
    assert result["result"]["reason"] == "existing-pr-head-mismatch"
    assert registry.get_workflow_run(before.run_id) == before
    journal_after = work_actions._load_runs(state)
    assert journal_after["revision"] == journal_before["revision"]
    assert journal_after["runs"][before.run_id] == journal_before["runs"][before.run_id]
    assert len(reads) == 2


def test_explicit_resume_does_not_reset_existing_candidate_with_active_job(
    tmp_path: Path,
) -> None:
    snapshot, state, registry, before = _prepare_existing_candidate_recovery(tmp_path)
    job = registry.create_job(
        task="recovery-active-job",
        persona="reviewer",
        kind="review",
        branch="feature/recovery",
        pane="",
        worktree=before.workspace_root,
        workflow_run_id=before.run_id,
        workflow_claim_key=before.claim_key,
        workflow_repo=before.repo,
        workflow_card="review",
        workflow_phase="review",
        workflow_repo_root=before.workspace_root,
        source_revision=before.source_revision,
    )
    runner, reads = _candidate_pr_read_runner(head=_EXISTING_CANDIDATE_HEAD)

    result = work_actions.execute_work_action(
        args={"action": "resume", "repo": _EXISTING_CANDIDATE_REPO, "work_id": "demo"},
        requested_by="operator",
        runner=runner,
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )

    assert result["result"]["action"] == "blocked"
    assert result["result"]["reason"] == "existing-candidate-active-job"
    assert registry.get_workflow_run(before.run_id) == before
    assert registry.get_job(job["job_id"])["status"] == "dispatched"
    assert reads == []


def test_existing_candidate_recovery_rebases_same_delivery_journal_row_once(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import work_bridge

    snapshot, state, registry, before = _prepare_existing_candidate_recovery(tmp_path)
    journal_before = work_actions._load_runs(state)
    runner, _reads = _candidate_pr_read_runner(head=_EXISTING_CANDIDATE_HEAD)
    work_actions.execute_work_action(
        args={"action": "resume", "repo": _EXISTING_CANDIDATE_REPO, "work_id": "demo"},
        requested_by="operator",
        runner=runner,
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )
    recovered = registry.get_workflow_run(before.run_id)
    authority = work_actions.load_work_authority(
        repo=_EXISTING_CANDIDATE_REPO,
        work_id="demo",
        snapshot_path=snapshot,
    )

    work_bridge._rebase_delivery_journal_authority(
        state_root=state.parent,
        run=recovered,
        authority=authority,
    )
    first_readback = work_actions._load_runs(state)
    work_bridge._rebase_delivery_journal_authority(
        state_root=state.parent,
        run=recovered,
        authority=authority,
    )
    second_readback = work_actions._load_runs(state)

    assert recovered.run_id == _EXISTING_CANDIDATE_RUN_ID
    assert len(first_readback["runs"]) == 1
    row = first_readback["runs"][recovered.run_id]
    assert row["claim_key"] == recovered.claim_key
    assert row["authority_digest"] == work_actions.work_authority_digest(authority)
    assert row["source_revisions"] == list(authority.source_revisions)
    assert row["mapped_prs"] == [_EXISTING_CANDIDATE_PR]
    assert row["ship"]["head"] == _EXISTING_CANDIDATE_HEAD
    assert row["ship"]["pr_number"] == _EXISTING_CANDIDATE_PR
    assert row.get("publication_events", {}) == journal_before["runs"][recovered.run_id].get(
        "publication_events", {}
    )
    assert second_readback["revision"] == first_readback["revision"]
    assert second_readback["runs"] == first_readback["runs"]


def test_resume_does_not_exempt_foreign_planning_source_by_prefix(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        prs=(),
        source_revisions=("issue:12@open", "openspec:demo@1"),
    )
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
    before = _seed_verified_run_with_gate(registry, run_id)
    foreign_ref = "docs/superpowers/plans/foreign.md"
    _snapshot(
        snapshot,
        prs=(),
        source_revisions=(
            "issue:12@open",
            "openspec:demo@1",
            f"superpowers_plan:acme/demo:{foreign_ref}@identity:{foreign_ref}",
        ),
        provider_revision="gh-2",
    )

    work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )

    after = registry.get_workflow_run(run_id)
    assert after.source_revision != before.source_revision
    assert after.attempts["verify"] == before.attempts["verify"] + 1
    assert after.gate_refs == ()


def test_resume_restarts_when_accepted_planning_bytes_drift(tmp_path: Path) -> None:
    base_sources = ("issue:12@open", "openspec:demo@1")
    snapshot = _snapshot(
        tmp_path / "snapshot.json", prs=(), source_revisions=base_sources
    )
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
    ref = "docs/superpowers/plans/demo.md"
    accepted_bytes = b"# Accepted plan\n"
    path = Path(run.workspace_root) / ref
    path.parent.mkdir(parents=True)
    path.write_bytes(accepted_bytes)
    registry._manager_update_workflow_run(
        run_id,
        planning_authority=(
            PlanningArtifactAuthority(
                ref=ref,
                kind="plan",
                work_id="demo",
                baseline_sha256=hashlib.sha256(accepted_bytes).hexdigest(),
            ),
        ),
    )
    before = _seed_verified_run_with_gate(registry, run_id)
    path.write_bytes(b"# Drifted plan\n")
    source = f"superpowers_plan:acme/demo:{ref}@identity:{ref}"
    _snapshot(
        snapshot,
        prs=(),
        source_revisions=(*base_sources, source),
        provider_revision="gh-2",
    )

    work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )

    after = registry.get_workflow_run(run_id)
    assert after.source_revision != before.source_revision
    assert after.attempts["verify"] == before.attempts["verify"] + 1
    assert after.gate_refs == ()


@pytest.mark.parametrize("mismatch", ["pr-ref", "pr-candidate"])
def test_resume_restarts_when_manager_pr_does_not_match_run_candidate(
    tmp_path: Path, mismatch: str
) -> None:
    base_sources = ("issue:12@open", "openspec:demo@1")
    snapshot = _snapshot(
        tmp_path / "snapshot.json", prs=(), source_revisions=base_sources
    )
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
    run_pr_refs = ("acme/demo#18",) if mismatch == "pr-ref" else ("acme/demo#17",)
    before = _seed_verified_run_with_gate(
        registry, run_id, phase="review", pr_refs=run_pr_refs
    )
    if mismatch == "pr-candidate":
        registry._manager_update_workflow_run(run_id, verified_head="f" * 40)
        before = registry.get_workflow_run(run_id)
    source = "github_pr:acme/demo#17@identity:acme/demo#17;state:open"
    _snapshot(
        snapshot,
        prs=(17,),
        source_revisions=(*base_sources, source),
        provider_revision="gh-2",
    )

    work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )

    after = registry.get_workflow_run(run_id)
    assert after.source_revision != before.source_revision
    assert after.attempts["verify"] == before.attempts["verify"] + 1
    assert after.gate_refs == ()


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


def test_periodic_auto_scan_claims_labeled_work_and_skips_missing_issue(tmp_path: Path) -> None:
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

    # #669：missing-issue 的情境改用**獨立的 coordinator root**。原本兩段共用
    # `tmp_path/jobs.json`，而舊行為建立 missing_issue run 時
    # `_manager_create_workflow_run` 會把同 (repo, work_id) 的 ongoing run 全部
    # 標成 superseded——上一段剛 claim 成功的 run 就是被它作廢的（這正是「判定
    # missing_issue 卻建 run」的第二層傷害），最後那個 `resume` 才會回 `claim`。
    # 修正後不再建 run，共用 registry 會讓 resume 正確地接回上一段那個 run
    # （回 `resume`），與本測試想驗的「missing → issue 補上 → 可 claim」無關。
    missing_root = tmp_path / "missing"
    missing_snapshot = _snapshot(
        missing_root / "missing.json",
        issues=(),
        source_revisions=("openspec:demo@2",),
    )
    missing_state = missing_root / "runs.json"
    attention = work_actions.run_auto_claim_scan(
        snapshot_path=missing_snapshot,
        state_path=missing_state,
        now=lambda: 200,
    )
    # #669：`missing_issue` 不再物化成 run——掃描回報 `not_claimable` 且 run 為
    # None，理由改落在耐久的 `not-claimable` ledger（operator 從 `cortex status`
    # 的 `not_claimable` 區塊查得到）。
    assert attention[0]["action"] == "not_claimable"
    assert attention[0]["reason"] == "missing_issue"
    assert attention[0]["run"] is None

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
    # R0.5 D1 語意更新：targeted 複驗在**第一次確認 label** 後即停（early-break），
    # 之後的 issue 不再讀取——fail-closed 適用於「確認前」的讀取失敗。
    # 故本測試讓第一個 mapped issue（12）讀取失敗：複驗尚無任何確認 → 整體 blocked。
    snapshot = _snapshot(tmp_path / "snapshot.json", issues=(12, 13))

    def runner(argv, **kwargs):
        if argv[-1].endswith("/12"):
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
    stopped_run = JobRegistry(state_path=state.parent / "jobs.json").list_workflow_runs()[0]
    assert stopped_run.needs_human_reason["reason"] == "multiple-delivery-targets-unsupported"
    assert "unlink" not in stopped_run.needs_human_reason["detail"]
    assert "發布 canonical Todo" in stopped_run.needs_human_reason["detail"]
    assert "等 Monitor 更新" in stopped_run.needs_human_reason["detail"]

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
        needs_human_reason=fixture_needs_human_reason(),
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
    assert calls[0][0] == ["openspec", "validate", "demo", "--type", "change", "--strict"]


def _drive_repair_budget_cycle(
    monkeypatch,
    tmp_path: Path,
    *,
    heads: list[str],
    sizing_score: int | None = None,
    sizing_band: str | None = None,
) -> tuple[list[dict], Path]:
    """Push ``len(heads)`` candidates, each reviewed with one blocking finding.

    #218 AC1/AC2：work-item repair budget 依 band 參數化，第 3 次
    model_repair（第 3 個 head）後強制 needs_human，不建立下一版 run。回傳每
    輪 review 的結果，供呼叫端依 band 斷言確切的停止點。
    """

    current = {"head": heads[0], "review": False, "submitted": 201.0, "now": 200.0}
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=state.parent / "jobs.json")
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: current["now"],
        workflow_registry=registry,
    )
    if sizing_band is not None:
        registry._manager_update_workflow_run(
            started["result"]["run"]["run_id"],
            sizing_score=sizing_score,
            sizing_band=sizing_band,
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
    results: list[dict] = []
    for index, head in enumerate(heads):
        current.update(head=head, review=False, now=200.0 + index * 20, submitted=201.0 + index * 20)
        requested = work_actions.execute_work_action(
            args=base,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: current["now"],
            workflow_registry=registry,
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
            workflow_registry=registry,
        )
        results.append(reviewed["result"])
        if reviewed["result"]["action"] == "needs_human":
            break
    return results, state


def test_review_findings_persist_across_heads_and_third_round_needs_human_default_band(
    monkeypatch, tmp_path: Path
) -> None:
    """band 未掛（None）時沿用現行 MAX_FIX_ROUNDS=2：第 3 輪才 needs_human（#218 AC1）。"""

    heads = ["a" * 40, "b" * 40, "c" * 40]
    results, state = _drive_repair_budget_cycle(monkeypatch, tmp_path, heads=heads)
    assert [result["action"] for result in results] == [
        "fix-required",
        "fix-required",
        "needs_human",
    ]
    final = results[-1]
    assert final["reason"] == "copilot-finding-budget-exhausted"
    assert final["repair_rounds_used"] == 2
    assert final["repair_rounds_budget"] == 2
    assert final["repair_rounds_remaining"] == 0
    assert final["legal_next_steps"] == ("maintainer-review",)
    # #218 AC3：已重複 stage 與預估 invalidation 範圍（ship 階段 repair 迴圈
    # 不回頭讓 build/verify 失效，範圍即當前 phase）。
    assert final["repeated_stage"]
    assert final["invalidation_scope"] == (final["repeated_stage"],)
    persisted = _only_journal_row(state)
    assert "status" not in persisted
    assert JobRegistry(state_path=state.parent / "jobs.json").list_workflow_runs()[0].facets == (
        "needs_human",
    )
    # #218：計數提升到 work-item 層（active 頂層），不再只活在 active["ship"] 裡。
    assert persisted["repair_rounds"] == 2


def test_review_findings_green_band_needs_human_after_a_single_fix_round(
    monkeypatch, tmp_path: Path
) -> None:
    """#218 AC1：green band 只有 1 次 fix round 預算，第 2 輪即 needs_human。"""

    heads = ["a" * 40, "b" * 40, "c" * 40]
    results, state = _drive_repair_budget_cycle(
        monkeypatch, tmp_path, heads=heads, sizing_score=0, sizing_band="green"
    )
    assert [result["action"] for result in results] == ["fix-required", "needs_human"]
    final = results[-1]
    assert final["reason"] == "copilot-finding-budget-exhausted"
    assert final["repair_rounds_used"] == 1
    assert final["repair_rounds_budget"] == 1
    assert final["repair_rounds_remaining"] == 0
    persisted = _only_journal_row(state)
    assert persisted["repair_rounds"] == 1


def test_review_findings_yellow_band_matches_default_two_fix_rounds(
    monkeypatch, tmp_path: Path
) -> None:
    """#218 AC1：yellow band 明確帶 sizing_band="yellow"，行為與現行預設一致。"""

    heads = ["a" * 40, "b" * 40, "c" * 40]
    results, state = _drive_repair_budget_cycle(
        monkeypatch, tmp_path, heads=heads, sizing_score=4, sizing_band="yellow"
    )
    assert [result["action"] for result in results] == [
        "fix-required",
        "fix-required",
        "needs_human",
    ]
    assert results[-1]["repair_rounds_budget"] == 2
    persisted = _only_journal_row(state)
    assert persisted["repair_rounds"] == 2


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


def _review_steps_passed(steps):
    """走到 ship needs-fix 時，Cortex 自己的 review gate 必然已通過。"""
    import dataclasses

    return tuple(
        dataclasses.replace(step, gate_result="passed") if step.phase == "review" else step
        for step in steps
    )


def test_review_disposition_requires_resolved_same_head_and_resumes_ship(
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
    foreign_path = tmp_path / "foreign-review.json"
    foreign_payload = {"state": "passed", "candidate": HEAD}
    foreign_path.write_text(json.dumps(foreign_payload), encoding="utf-8")
    foreign_hash = work_actions.verification.canonical_json_hash(foreign_payload)
    for phase in ("plan", "build", "verify", "review"):
        registry._manager_update_workflow_run(run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=HEAD,
        verified_head=HEAD,
        pr_refs=("acme/demo#8",),
        gate_refs=(GateEvidenceRef("foreign-review", str(foreign_path), foreign_hash),),
        gate_status="passed",
        facets=("needs_human",),
        needs_human_reason=fixture_needs_human_reason(),
        steps=_review_steps_passed(registry.get_workflow_run(run_id).steps),
    )
    current = {
        "head": HEAD,
        "threads": (ReviewThread("thread-1", False, False, "src/demo.py", 7, "finding"),),
        "merged": False,
    }

    class GitHub:
        def __init__(self, *, runner):
            pass

        def ensure_pr_metadata(self, **kwargs):
            pass

        def fetch_delivery_facts(self, **kwargs):
            return DeliveryFacts(
                head=current["head"],
                mergeable=True,
                mergeable_state="clean",
                checks=(GitHubCheck("ci", "completed", "success"),),
                copilot_reviews=(
                    CopilotReview(
                        review_id=9,
                        commit_id=HEAD,
                        state="COMMENTED",
                        body="finding",
                        author=COPILOT_REVIEWER_LOGIN,
                        submitted_at_epoch=205,
                    ),
                ),
                review_threads=current["threads"],
                closing_issues=(12,),
                active_openspec_absent=True,
                archive_present=True,
            )

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(current["merged"], current["head"], "c" * 40 if current["merged"] else None)

        def request_copilot(self, **kwargs):
            raise AssertionError("existing exact-HEAD Copilot review must be reused")

    class Orchestrator:
        def __init__(self, *, github, now):
            self.github = github

        def merge_if_ready(self, **kwargs):
            current["merged"] = True
            return SimpleNamespace(
                expected_head=kwargs["expected_head"],
                expected_tree_hash=kwargs["expected_tree_hash"],
            )

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
    monkeypatch.setattr(
        work_actions,
        "_validate_foreign_review",
        lambda *_args, **_kwargs: foreign_payload,
    )
    base = {
        "repo_root": str(tmp_path),
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
        "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        "foreign_review_path": str(foreign_path),
        "foreign_review_hash": foreign_hash,
    }

    def ship(now_epoch: float) -> dict:
        return work_actions.execute_work_action(
            args={"action": "ship", "repo": "acme/demo", "work_id": "demo", **base},
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: now_epoch,
            workflow_registry=registry,
        )["result"]

    finding = ship(207)
    assert finding["action"] == "fix-required"
    assert _only_journal_row(state)["ship"]["phase"] == "needs-fix"

    still_open = ship(208)
    assert still_open["action"] == "fix-required"
    assert still_open["reason"] == "review-threads-unresolved"
    assert still_open["next_actions"] == ["review-disposition"]
    disposition_args = {
        "action": "review-disposition",
        "repo": "acme/demo",
        "work_id": "demo",
        "actor": "maintainer",
        "reason": "討論已完成，此 finding 不阻擋合併。",
    }
    with pytest.raises(RuntimeError, match="all PR review threads resolved"):
        work_actions.execute_work_action(
            args=disposition_args,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 209,
            workflow_registry=registry,
        )
    assert not (tmp_path / "evidence" / "review-disposition").exists()

    current["threads"] = (ReviewThread("thread-1", True, False, "src/demo.py", 7, "finding"),)
    still_needs_operator = ship(210)
    assert still_needs_operator["action"] == "fix-required"
    assert still_needs_operator["reason"] == "review-disposition-required"
    assert still_needs_operator["next_actions"] == ["review-disposition"]
    registry._manager_update_workflow_run(
        run_id,
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=work_actions.diagnostic_reason(
            "review-disposition-required",
            "PR review threads 已 resolved，仍需要 operator disposition。",
            source="test_review_disposition_935",
            run_id=run_id,
            work_id="demo",
            head=HEAD,
        ),
    )
    resumed_attention = work_actions.execute_work_action(
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 211,
        workflow_registry=registry,
        workflow_starter=lambda *_args: registry.get_workflow_run(run_id),
    )["result"]
    assert "review-disposition" in resumed_attention["next_actions"]
    assert "cortex work review-disposition" in resumed_attention["next_step_hint"]

    current["head"] = "c" * 40
    with pytest.raises(RuntimeError, match="PR HEAD mismatch"):
        work_actions.execute_work_action(
            args=disposition_args,
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 212,
            workflow_registry=registry,
        )

    current["head"] = HEAD
    recorded = work_actions.execute_work_action(
        args=disposition_args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 212,
        workflow_registry=registry,
    )["result"]
    assert recorded["action"] == "review-disposition-recorded"
    record_path = Path(recorded["ref"])
    assert record_path.is_file()
    assert record_path.stat().st_mode & 0o222 == 0
    row = _only_journal_row(state)
    assert row["ship"]["phase"] == "needs-fix"
    assert row["ship"]["findings"] == [
        {"path": "src/demo.py", "line": 7, "body": "finding"}
    ]
    assert row["review_dispositions"] == [
        {"ref": str(record_path), "hash": recorded["hash"]}
    ]
    assert "needs_human" not in registry.get_workflow_run(run_id).facets

    current["threads"] = (
        ReviewThread("thread-1", True, False, "src/demo.py", 7, "finding"),
        ReviewThread("thread-2", True, False, "src/other.py", 8, "new thread"),
    )
    stale_thread_set = ship(213)
    assert stale_thread_set["action"] == "fix-required"
    assert stale_thread_set["reason"] == "review-disposition-required"
    current["threads"] = (
        ReviewThread("thread-1", True, False, "src/demo.py", 7, "finding"),
    )

    current["head"] = "c" * 40
    with pytest.raises(RuntimeError, match="ship HEAD differs from authenticated GitHub PR"):
        ship(214)
    current["head"] = HEAD

    resumed = ship(216)
    assert resumed == {"action": "merged-awaiting-closure", "head": HEAD}
    row = _only_journal_row(state)
    assert row["review_finding_history"][0]["finding_count"] == 1
    assert row["review_finding_history"][0]["review_id"] == 9
    assert row["review_finding_history"][0]["disposition_ref"] == str(record_path)
    assert row["review_dispositions"] == [
        {"ref": str(record_path), "hash": recorded["hash"]}
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

    with pytest.raises(RuntimeError, match="change-specific-changelog-entry-missing"):
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


def test_local_archive_gate_accepts_issue_prefixed_changelog_fragment(
    tmp_path: Path,
) -> None:
    change = "footer-agent-selection"
    tasks = tmp_path / "openspec" / "changes" / change / "tasks.md"
    tasks.parent.mkdir(parents=True)
    tasks.write_text("- [x] complete\n", encoding="utf-8")
    fragment = tmp_path / "changelog.d" / f"341-{change}.md"
    fragment.parent.mkdir(parents=True)
    fragment.write_text("# 341: 修正頁尾 agent 選擇\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n", encoding="utf-8")

    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    work_actions._validate_local_archive_inputs(
        repo_root=tmp_path,
        change=change,
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



# issue #203：`intake` 合成「（必要時）link + start」，取代已停用的低階 dispatch，
# 作為單一「拿到一個 issue/task 就進件」入口。三個場景對齊簡報 tests 欄：
# (a) work_id 已有 confirmed authority，省略 issue 仍能建 run（等價 start）；
# (b) 首次 intake 帶尚未被 mapped_todo_paths 涵蓋的 kind/ref，先寫一筆 override
#     link 再依既有已授權的 issue 建 run，第二次相同輸入為幂等；
# (c) 未 link 且未帶 issue/kind/ref 時 fail-closed，不建立任何 WorkflowRun。


def test_intake_without_link_args_starts_using_existing_confirmed_authority(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    expected_claim_key = work_actions._expected_claim_key(authority)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    intake = work_actions.execute_work_action(
        args={"action": "intake", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        now=lambda: 200,
        workflow_registry=registry,
        workflow_starter=work_actions._fallback_workflow_starter(
            registry, tmp_path / "runs.json"
        ),
    )

    assert intake["result"]["linked"] is False
    assert intake["result"]["link_result"] is None
    assert intake["result"]["action"] == "claim"
    assert intake["result"]["run"]["claim_key"] == expected_claim_key
    assert len(registry.list_workflow_runs()) == 1

    # 第二次以相同輸入重送必須幂等：同一個 run_id，不新增第二個 active run。
    second = work_actions.execute_work_action(
        args={"action": "intake", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        now=lambda: 200,
        workflow_registry=registry,
        workflow_starter=work_actions._fallback_workflow_starter(
            registry, tmp_path / "runs.json"
        ),
    )
    assert second["result"]["run"]["run_id"] == intake["result"]["run"]["run_id"]
    assert len(registry.list_workflow_runs()) == 1


def test_intake_writes_missing_link_then_claims_using_preexisting_issue_authority(
    tmp_path: Path,
) -> None:
    """首次 intake 帶一個尚未反映在受監控快照 mapped_todo_paths 的 Todo path
    ref：intake 必須先把它寫進 override（供下一輪 monitor correlation 採信），
    再依快照裡已經確認過的 issue（monitor 早於本次 override 就已授權）建立
    run——見 `work_actions._intake_action` docstring：override 檔與受監控快照
    是兩份分開維護的狀態，寫 override 這一步本身不會讓當下這次呼叫的快照
    立刻反映新 mapped_todo_paths，intake 因此仰賴『同一列已有其他明文授權來
    源（這裡是 mapped_issues）』才能在同一次呼叫內完成 claim，這是刻意的簡化
    決策（見函式 docstring）。
    """
    repo_root = _init_repo(tmp_path / "repo")
    snapshot = _snapshot(tmp_path / "snapshot.json", todo_paths=())
    override_path = repo_root / ".cortex" / "work-items.yaml"
    assert not override_path.exists()
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    starter = work_actions._fallback_workflow_starter(registry, tmp_path / "runs.json")

    first = work_actions.execute_work_action(
        args={
            "action": "intake",
            "repo": "acme/demo",
            "work_id": "demo",
            "kind": "path",
            "ref": "docs/todo.md",
            "repo_root": str(repo_root),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        now=lambda: 200,
        workflow_registry=registry,
        workflow_starter=starter,
    )

    assert first["result"]["linked"] is True
    assert first["result"]["link_result"]["source"] == {
        "kind": "path",
        "ref": "docs/todo.md",
    }
    assert override_path.exists()
    override_payload = work_actions.safe_load(override_path.read_text(encoding="utf-8"))
    assert {"kind": "path", "ref": "docs/todo.md"} in (
        override_payload["work_items"]["demo"]["links"]
    )
    assert first["result"]["action"] == "claim"
    first_run_id = first["result"]["run"]["run_id"]
    assert len(registry.list_workflow_runs()) == 1

    second = work_actions.execute_work_action(
        args={
            "action": "intake",
            "repo": "acme/demo",
            "work_id": "demo",
            "kind": "path",
            "ref": "docs/todo.md",
            "repo_root": str(repo_root),
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        now=lambda: 200,
        workflow_registry=registry,
        workflow_starter=starter,
    )
    assert second["result"]["run"]["run_id"] == first_run_id
    assert len(registry.list_workflow_runs()) == 1


def test_intake_without_any_authority_or_link_args_fails_closed_without_creating_a_run(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-1",
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "mapped_issues": [],
                        "mapped_prs": [],
                        "mapped_openspec": [],
                        "mapped_todo_paths": [],
                        "confirmed_todo": False,
                        "auto_label": False,
                        "source_revisions": ["issue:12@open"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    with pytest.raises(ValueError, match="work-action intake 需要"):
        work_actions.execute_work_action(
            args={"action": "intake", "repo": "acme/demo", "work_id": "demo"},
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            now=lambda: 200,
            workflow_registry=registry,
            workflow_starter=work_actions._fallback_workflow_starter(
                registry, tmp_path / "runs.json"
            ),
        )

    assert registry.list_workflow_runs() == []


def test_review_disposition_refuses_when_cortex_review_gate_not_passed(tmp_path: Path) -> None:
    """disposition 只裁決 ship 段 Copilot finding；Cortex review gate 未通過不得繞過。"""
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
        run_id, candidate_head=HEAD, verified_head=HEAD, facets=("needs_human",),
        needs_human_reason=fixture_needs_human_reason(),
    )
    with pytest.raises(RuntimeError, match="only applies to ship Copilot findings"):
        work_actions.execute_work_action(
            args={
                "action": "review-disposition",
                "repo": "acme/demo",
                "work_id": "demo",
                "actor": "maintainer",
                "reason": "review gate 尚未通過時不得裁決續行。",
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state,
            now=lambda: 209,
            workflow_registry=registry,
        )


@pytest.mark.parametrize(
    "ship_state",
    [
        {"phase": "merge-authorized", "merge_authorization": {"ref": "auth.json", "hash": "a" * 64}},
        {"phase": "review-requested", "merge_authorization": {"ref": "auth.json", "hash": "a" * 64}},
        {"phase": "merged"},
    ],
)
def test_explicit_resume_does_not_reset_existing_candidate_after_merge_authorization(
    tmp_path: Path, ship_state: dict
) -> None:
    """merge 已授權或已進入 merge 的交付，中止後 resume 不得把 run 退回 verify。"""
    snapshot, state, registry, before = _prepare_existing_candidate_recovery(tmp_path)
    journal = work_actions._load_runs(state)
    row = journal["runs"][before.run_id]
    row["ship"] = {**(row.get("ship") or {}), **ship_state}
    work_actions._save_runs(state, journal)
    runner, reads = _candidate_pr_read_runner(head=_EXISTING_CANDIDATE_HEAD)

    result = work_actions.execute_work_action(
        args={"action": "resume", "repo": _EXISTING_CANDIDATE_REPO, "work_id": "demo"},
        requested_by="operator",
        runner=runner,
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )

    assert result["result"]["action"] == "blocked"
    assert result["result"]["reason"] == "existing-candidate-merge-authorized"
    assert registry.get_workflow_run(before.run_id) == before
