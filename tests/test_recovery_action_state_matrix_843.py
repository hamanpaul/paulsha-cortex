"""#843 R02／R03／R09：正式入口 → 持久 registry → read model 的 recovery 狀態矩陣。

每個註冊的 work recovery action 都有一個情境（重用各 producer suite 既有 fixture），
經正式 dispatcher ``work_actions.execute_work_action``（``resume`` 另經 daemon
``build_request_executor``，因為它的接續派工只在那一層發生）送出，然後：

- R02 正例：以**全新** ``JobRegistry`` 重讀持久狀態，再以 daemon status provider
  的 attention 投影（read model）核對結果；負例：錯 phase／錯卡／active job／缺
  actor 或 reason／stale exact-run／錯狀態，拒絕後 registry、evidence 檔案、job 數
  與 attention 全不變。每個 action×類別必須是 probe、冪等 no-op、指向 gap ledger
  的 strict xfail 已知缺口，或寫明理由的 N/A；新增 action 或類別而未分類會讓覆蓋
  測試失敗。
- R03：在 action 讀完 authority／candidate、準備第一次持久化之前，由另一個
  registry writer 注入 drift（新 generation／candidate／binding）；正式入口必須
  fail closed，持久狀態逐欄等於 drift 後的狀態，新 generation 不被作用。
- R09：把 attention 投影出的每個 ``next_actions`` 以同一 read model 可得的欄位
  組成請求，逐項送回正式入口，必須被受理。
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import pytest

from paulsha_cortex import recovery_action_contracts as contracts
from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import (
    claim as claim_module,
    gate_runner,
    manager_daemon,
    work_actions,
)
from paulsha_cortex.coordinator.github_delivery import GitHubDeliveryClient
from paulsha_cortex.coordinator.registry import JobRegistry, RegistryRevisionConflict
from paulsha_cortex.coordinator.workflow import GateEvidenceRef

from diagnostic_fixtures import fixture_needs_human_reason

import test_candidate_base_refreeze_731 as refreeze_fixture
import test_gate_acceptance_chain_540 as regenerate_fixture
import test_midchain_builder_retry_545 as retry_card_fixture
import test_planning_claim_recovery as planning_fixture
import test_post_merge_run_resolution_1141 as delivered_fixture
import test_reclaim_budget_reset_519 as reclaim_fixture
import test_recover_superseded_776 as superseded_fixture
import test_pre_candidate_recovery as pre_candidate_repo_fixture
import test_recovery_action_exposure_546 as pre_candidate_fixture
import test_recovery_action_rechain_1173 as rechain_fixture
import test_recovery_action_supersession_1174 as supersession_fixture
import test_repair_commit_recovery as repair_fixture
import test_review_gate_adjudication_exit as review_fixture
import test_work_actions as work_fixture


ROOT = Path(__file__).resolve().parents[1]
GAP_LEDGER_PATH = ROOT / "docs" / "recovery-action-gaps-843.json"
HEAD = "a" * 40
DRIFTED_HEAD = "e" * 40
OTHER_RUN_ID = "workflow-" + "0" * 20
NOW = 1_758_000_000.0

#: 票面 R02 的負例類別，外加「錯狀態」（needs_human／superseded／PR 前置不符）。
NEGATIVE_CATEGORIES = (
    "wrong-phase",
    "wrong-card",
    "active-job",
    "missing-actor-reason",
    "stale-exact-run",
    "wrong-state",
)


# ---------------------------------------------------------------------------
# 情境與觀測
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    action: str
    root: Path
    registry: JobRegistry
    state_path: Path
    args: dict[str, Any]
    repo: str
    work_id: str
    run_id: str | None
    snapshot: Path | None = None
    authority: Any = None
    execute_kwargs: dict[str, Any] = field(default_factory=dict)
    projection_state: str = "available"
    submit: Callable[["Scenario", dict[str, Any]], dict[str, Any]] | None = None
    env: dict[str, str] = field(default_factory=dict)
    patches: list[tuple[Any, str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _submit(sc: Scenario, args: dict[str, Any] | None = None) -> dict[str, Any]:
    request = dict(sc.args if args is None else args)
    if sc.submit is not None:
        return sc.submit(sc, request)
    return work_actions.execute_work_action(
        args=request,
        requested_by="operator",
        snapshot_path=sc.snapshot,
        state_path=sc.state_path,
        workflow_registry=sc.registry,
        now=lambda: NOW,
        **sc.execute_kwargs,
    )


def _install_authority_seam(sc: Scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    """沒有 snapshot fixture 的情境（776／546）改走 authority loader seam。"""

    if sc.authority is not None:
        monkeypatch.setattr(
            work_actions, "load_work_authority", lambda **_kwargs: sc.authority
        )


def _fresh(sc: Scenario) -> JobRegistry:
    return JobRegistry(state_path=sc.registry._state_path)


def _durable_state(sc: Scenario) -> dict[str, Any]:
    fresh = _fresh(sc)
    return {
        "runs": {run.run_id: run.to_dict() for run in fresh.list_workflow_runs()},
        "jobs": {job["job_id"]: job for job in fresh.list_jobs()},
        "slices": {row["slice_id"]: row for row in fresh.list_slices()},
        "reclaim_resets": fresh.list_reclaim_resets(),
    }


def _files(root: Path) -> dict[str, str]:
    observed: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts or path.name.endswith(".lock"):
            continue
        if path.is_symlink():
            observed[str(relative)] = f"symlink:{path.readlink()}"
        elif path.is_file():
            observed[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            observed[f"{relative}/"] = "dir"
    return observed


def _attention(sc: Scenario, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """daemon status provider 的 attention 投影（read model），以全新 registry 讀持久狀態。"""

    if sc.snapshot is not None:
        snapshot = sc.snapshot

        def projection_state(*, repo: str, work_id: str) -> str:
            return work_actions.work_authority_projection_state(
                repo=repo, work_id=work_id, snapshot_path=snapshot
            )
    else:
        def projection_state(*, repo: str, work_id: str) -> str:
            return sc.projection_state

    monkeypatch.setattr(manager_daemon, "work_authority_projection_state", projection_state)
    provider = manager_daemon.build_runtime_status_provider(
        registry=_fresh(sc),
        specs_dir=str(sc.root / "status-specs"),
        handoff_dir=str(sc.root / "status-handoff"),
        scan_specs_fn=lambda _path: [],
        ready_units_fn=lambda _metas, _predicate: [],
        now_fn=lambda: "2026-09-29T00:00:00+00:00",
    )
    return {
        entry["run_id"]: entry
        for entry in provider()["attention"]
        if entry.get("kind") == "workflow_run"
    }


def _attention_summary(attention: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        run_id: {
            "phase": entry["current_phase"],
            "reason": entry["reason"],
            "next_actions": entry["next_actions"],
        }
        for run_id, entry in attention.items()
    }


def _observe(sc: Scenario, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return {
        "durable": _durable_state(sc),
        "files": _files(sc.root),
        "attention": _attention_summary(_attention(sc, monkeypatch)),
    }


def _force_run_fields(registry: JobRegistry, run_id: str, **fields: Any) -> None:
    """測試佈置專用：繞過 phase 轉移驗證直接改 run 欄位（同 569 既有手法）。"""

    registry.get_workflow_run(run_id)
    index = registry._find_workflow_run_index(run_id)
    registry._workflows[index] = replace(registry._workflows[index], **fields)
    registry._persist()


def _active_job(registry: JobRegistry, run_id: str, *, card: str = "active-card", phase: str = "build") -> dict:
    run = registry.get_workflow_run(run_id)
    job = registry.create_job(
        task=f"active-{card}",
        persona="builder",
        branch=f"feature/{card}",
        pane="",
        worktree=str(Path(run.workspace_root) / f"active-{card}"),
        workflow_run_id=run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=card,
        workflow_phase=phase,
    )
    registry.update_status(job["job_id"], "running")
    return registry.get_job(job["job_id"])


# ---------------------------------------------------------------------------
# 情境建構（逐 action 重用 producer suite 的既有 fixture）
# ---------------------------------------------------------------------------


def _started_run(root: Path, *, prs: tuple[int, ...] = ()):
    snapshot = work_fixture._snapshot(
        root / "snapshot.json",
        source_revisions=("issue:12@open",),
        changes=(),
        prs=prs,
    )
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=root / "jobs.json")
    run = work_actions._fallback_workflow_starter(registry, root / "runs.json")(
        authority, work_actions._expected_claim_key(authority), None
    )
    return snapshot, authority, registry, run


def _advance(registry: JobRegistry, run, *, phase: str) -> None:
    order = ("claim", "define", "plan", "build", "verify", "review")
    current = registry.get_workflow_run(run.run_id).current_phase
    for next_phase in order[order.index(current) + 1 : order.index(phase) + 1]:
        registry._manager_update_workflow_run(run.run_id, current_phase=next_phase)


def _resume_scenario(root: Path) -> Scenario:
    """#260 R6 的 resume fixture，改用本檔無 mapped PR 的 snapshot 以通過 delivery 對帳。"""

    snapshot, authority, registry, _started = _started_run(root)
    registry._manager_abandon_workflow_run(_started.run_id, evidence_ref="abandon:fixture-reset")
    worktree = root / "wt"
    repair_fixture._init_repair_worktree(worktree)
    run = registry._manager_create_workflow_run(
        work_id=authority.work_id,
        repo=authority.repo,
        claim_key=work_actions._expected_claim_key(authority),
        source_revision=work_actions.work_authority_digest(authority),
        workspace_root=str(root / "workspace"),
        combo="feature-oneshot",
        current_phase="build",
        steps=repair_fixture._repair_steps(),
        issue_refs=tuple(f"{authority.repo}#{n}" for n in authority.mapped_issues),
        openspec_refs=authority.mapped_openspec,
        facets=("needs_human",),
        needs_human_reason=fixture_needs_human_reason(),
        gate_status="running",
    )
    failed_job = repair_fixture._seed_failed_builder_job(
        registry, run=run, worktree=worktree, base_head="0" * 40, exit_code=17
    )
    identities = repair_fixture.IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            }
        ]
    )
    dispatcher = repair_fixture._ResumeDispatcher(registry, worktree)

    def work_action_fn(*, args, requested_by):
        return work_actions.execute_work_action(
            args=args,
            requested_by=requested_by,
            snapshot_path=snapshot,
            state_path=root / "runs.json",
            workflow_registry=registry,
            now=lambda: NOW,
        )

    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(root / "specs"),
        handoff_dir=str(root / "handoff"),
        launcher=repair_fixture._FakeLauncher(),
        workflow_identity_registry=identities,
        work_action_fn=work_action_fn,
    )

    def submit(_sc: Scenario, args: dict[str, Any]) -> dict[str, Any]:
        return executor(build_request(req_type="work-action", args=args, requested_by="operator"))

    return Scenario(
        action="resume",
        root=root,
        registry=registry,
        state_path=root / "runs.json",
        args={"action": "resume", "repo": "acme/demo", "work_id": "demo"},
        repo="acme/demo",
        work_id="demo",
        run_id=run.run_id,
        snapshot=snapshot,
        submit=submit,
        # daemon 派 builder 前重讀受監控 authority；指向本情境 snapshot。
        patches=[
            (
                manager_daemon,
                "load_work_authority",
                lambda *, repo, work_id, **_kwargs: work_actions.load_work_authority(
                    repo=repo, work_id=work_id, snapshot_path=snapshot
                ),
            )
        ],
        extra={"failed_job_id": failed_job["job_id"]},
    )


def _retry_build_scenario(root: Path) -> Scenario:
    snapshot, _authority, registry, run = _started_run(root)
    _advance(registry, run, phase="review")
    passed = tuple(
        replace(step, gate_result="passed")
        if step.phase in {"build", "verify", "review"}
        else step
        for step in run.steps
    )
    registry._manager_update_workflow_run(
        run.run_id,
        steps=passed,
        attempts={"build": 1, "verify": 1, "review": 1},
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=("needs_human",),
        gate_refs=(GateEvidenceRef("foreign-review", "/evidence/review.json", "f" * 64),),
        gate_status="running",
        needs_human_reason=fixture_needs_human_reason(),
    )
    return Scenario(
        action="retry-build",
        root=root,
        registry=registry,
        state_path=root / "runs.json",
        args={
            "action": "retry-build",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_candidate": HEAD,
        },
        repo="acme/demo",
        work_id="demo",
        run_id=run.run_id,
        snapshot=snapshot,
    )


def _retry_card_scenario(root: Path) -> Scenario:
    snapshot, registry, run, job_id = retry_card_fixture._stuck_run(root)
    return Scenario(
        action="retry-card",
        root=root,
        registry=registry,
        state_path=root / "runs.json",
        args={
            "action": "retry-card",
            "repo": retry_card_fixture.REPO,
            "work_id": retry_card_fixture.WORK_ID,
            "issue": 12,
            "actor": "operator",
            "expected_run_id": run.run_id,
            "card": "tdd-red",
        },
        repo=retry_card_fixture.REPO,
        work_id=retry_card_fixture.WORK_ID,
        run_id=run.run_id,
        snapshot=snapshot,
        extra={"old_job_id": job_id},
    )


def _retry_verify_scenario(root: Path) -> Scenario:
    snapshot, _authority, registry, run = _started_run(root)
    _advance(registry, run, phase="verify")
    steps = tuple(
        replace(step, gate_result="passed")
        if step.phase in {"claim", "define", "plan", "build"}
        else step
        for step in run.steps
    )
    updated = registry._manager_update_workflow_run(
        run.run_id,
        steps=steps,
        attempts={"build": 1, "verify": 1},
        candidate_head=HEAD,
        facets=("needs_human",),
        gate_status="failed",
        needs_human_reason=fixture_needs_human_reason(),
    )
    verify_card = next(step.card for step in updated.steps if step.phase == "verify")
    old = registry.create_job(
        task="wf-verification",
        persona="reviewer",
        kind="review",
        branch="feature/12-demo",
        pane="",
        worktree=str(root / "verify-sandbox"),
        subject_head=HEAD,
        workflow_run_id=run.run_id,
        workflow_claim_key=updated.claim_key,
        workflow_repo=updated.repo,
        workflow_card=verify_card,
        workflow_phase="verify",
    )
    registry.update_headless_result(old["job_id"], status="exited", exit_code=0)
    return Scenario(
        action="retry-verify",
        root=root,
        registry=registry,
        state_path=root / "runs.json",
        args={
            "action": "retry-verify",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_candidate": HEAD,
        },
        repo="acme/demo",
        work_id="demo",
        run_id=run.run_id,
        snapshot=snapshot,
        extra={"old_job_id": old["job_id"]},
    )


def _retry_review_scenario(root: Path) -> Scenario:
    snapshot, _authority, registry, run = review_fixture._review_fixture(root)
    return Scenario(
        action="retry-review",
        root=root,
        registry=registry,
        state_path=root / "runs.json",
        args={
            "action": "retry-review",
            "repo": review_fixture.REPO,
            "work_id": review_fixture.WORK_ID,
            "issue": 12,
            "actor": "operator",
            "expected_candidate": review_fixture.HEAD,
        },
        repo=review_fixture.REPO,
        work_id=review_fixture.WORK_ID,
        run_id=run.run_id,
        snapshot=snapshot,
    )


_PLANNING_REASON = "planning identity probe unavailable"


def _recover_planning_scenario(root: Path) -> Scenario:
    run_id, registry, state, snapshot = planning_fixture._seed_planning_failure_run(
        root, classification="environment", reason=_PLANNING_REASON
    )
    return Scenario(
        action="recover-planning",
        root=root,
        registry=registry,
        state_path=state,
        args={
            "action": "recover-planning",
            "repo": "acme/demo",
            "work_id": "demo",
            "expected_run_id": run_id,
            "failure_classification": "environment",
            "failure_reason": _PLANNING_REASON,
        },
        repo="acme/demo",
        work_id="demo",
        run_id=run_id,
        snapshot=snapshot,
    )


def _recover_pre_candidate_scenario(root: Path) -> Scenario:
    registry, run, authority = pre_candidate_fixture._seed_pre_candidate_recovery(root)
    # worktree reclaim 的 git runner 以 PSC_REPO_ROOT 為準；自備本機 repo，
    # 不讓 reclaim 落到 checkout（#612）。
    repo = pre_candidate_repo_fixture._seed_repo(root)
    return Scenario(
        action="recover-pre-candidate",
        root=root,
        registry=registry,
        state_path=root / "runs.json",
        args={"action": "recover-pre-candidate", "repo": run.repo, "work_id": run.work_id},
        repo=run.repo,
        work_id=run.work_id,
        run_id=run.run_id,
        authority=authority,
        env={"PSC_REPO_ROOT": str(repo)},
        extra={"slice_id": "work-slice"},
    )


def _recover_repair_commit_scenario(root: Path) -> Scenario:
    authority, snapshot = repair_fixture._authority(root)
    registry = JobRegistry(state_path=root / "jobs.json")
    worktree = root / "wt"
    base_head = repair_fixture._init_repair_worktree(worktree)
    repaired_head = repair_fixture._commit_repair(worktree)
    run = repair_fixture._make_run(
        registry,
        authority=authority,
        claim_key=work_actions._expected_claim_key(authority),
        current_phase="build",
        steps=repair_fixture._repair_steps(),
        candidate_head=base_head,
        facets=("needs_human",),
    )
    failed_job = repair_fixture._seed_failed_builder_job(
        registry,
        run=run,
        worktree=worktree,
        base_head=base_head,
        log_path=root / "logs" / "builder.jsonl",
    )
    repair_fixture._write_gate_worktree_state(
        failed_job, head=repaired_head, ancestry_baseline=base_head, ancestry_ok=True
    )
    return Scenario(
        action="recover-repair-commit",
        root=root,
        registry=registry,
        state_path=root / "runs.json",
        args={
            "action": "recover-repair-commit",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_run_id": run.run_id,
            "expected_candidate": repaired_head,
        },
        repo="acme/demo",
        work_id="demo",
        run_id=run.run_id,
        snapshot=snapshot,
        extra={"base_head": base_head, "repaired_head": repaired_head},
    )


def _regenerate_gates_scenario(root: Path) -> Scenario:
    snapshot, registry, run, ledger = regenerate_fixture._stuck_run(root)
    return Scenario(
        action="regenerate-gates",
        root=root,
        registry=registry,
        state_path=root / "runs.json",
        args={
            "action": "regenerate-gates",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_run_id": run.run_id,
        },
        repo="acme/demo",
        work_id="demo",
        run_id=run.run_id,
        snapshot=snapshot,
        extra={"ledger": ledger},
    )


def _abandon_scenario(root: Path) -> Scenario:
    snapshot, _authority, registry, run = _started_run(root)
    _advance(registry, run, phase="build")
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        gate_status="failed",
        needs_human_reason=fixture_needs_human_reason(),
    )
    return Scenario(
        action="abandon",
        root=root,
        registry=registry,
        state_path=root / "runs.json",
        args={
            "action": "abandon",
            "repo": "acme/demo",
            "work_id": "demo",
            "issue": 12,
            "actor": "operator",
            "expected_run_id": run.run_id,
            "reason": "recovery matrix abandon",
        },
        repo="acme/demo",
        work_id="demo",
        run_id=run.run_id,
        snapshot=snapshot,
    )


def _retire_delivered_scenario(root: Path) -> Scenario:
    snapshot, _authority, registry, run = _started_run(root)
    registry._manager_update_workflow_run(
        run.run_id,
        pr_refs=("acme/demo#110",),
        facets=("needs_human",),
        needs_human_reason=fixture_needs_human_reason(),
    )
    return Scenario(
        action="retire-delivered",
        root=root,
        registry=registry,
        state_path=root / "runs.json",
        args={
            "action": "retire-delivered",
            "repo": "acme/demo",
            "work_id": "demo",
            "actor": "operator",
            "expected_run_id": run.run_id,
            "reason": "recovery matrix retire",
        },
        repo="acme/demo",
        work_id="demo",
        run_id=run.run_id,
        snapshot=snapshot,
        execute_kwargs={
            "runner": work_fixture._pr_lifecycle_runner(
                {110: {"state": "closed", "merged_at": "2026-09-01T00:00:00Z"}}
            )
        },
    )


def _recover_superseded_scenario(root: Path) -> Scenario:
    registry = superseded_fixture._make_registry(root)
    run = superseded_fixture._create_run(registry, phase="verify")
    registry._manager_update_workflow_run(run.run_id, status="superseded", facets=("blocked",))
    return Scenario(
        action="recover-superseded",
        root=root,
        registry=registry,
        state_path=root / "jobs.json",
        args=superseded_fixture._args(run.run_id),
        repo=superseded_fixture._REPO,
        work_id=superseded_fixture._WORK_ID,
        run_id=run.run_id,
        authority=superseded_fixture._AUTHORITY,
    )


def _reset_reclaim_budget_scenario(root: Path) -> Scenario:
    snapshot = reclaim_fixture._snapshot(root / "snapshot.json")
    registry = JobRegistry(state_path=root / "jobs.json")
    burned = reclaim_fixture._burn_generations(registry, 3)
    return Scenario(
        action="reset-reclaim-budget",
        root=root,
        registry=registry,
        state_path=root / "journal.jsonl",
        args={
            "action": "reset-reclaim-budget",
            "repo": "acme/demo",
            "work_id": "demo",
            "actor": "operator",
            "reason": "recovery matrix reset",
        },
        repo="acme/demo",
        work_id="demo",
        run_id=None,
        snapshot=snapshot,
        extra={"burned": burned},
    )


def _refreeze_base_scenario(root: Path) -> Scenario:
    upstream, workspace, snapshot, registry = refreeze_fixture._fixture(root)
    run = refreeze_fixture._run(
        registry,
        workspace=workspace,
        facets=("needs_human",),
        needs_human_reason=fixture_needs_human_reason(),
    )
    advanced = refreeze_fixture._advance_upstream(upstream, filename="later.txt")
    return Scenario(
        action="refreeze-base",
        root=root,
        registry=registry,
        state_path=root / "journal.jsonl",
        args={
            "action": "refreeze-base",
            "repo": refreeze_fixture._REPO,
            "work_id": refreeze_fixture._WORK_ID,
            "actor": "operator",
            "reason": "recovery matrix refreeze",
            "expected_run_id": run.run_id,
        },
        repo=refreeze_fixture._REPO,
        work_id=refreeze_fixture._WORK_ID,
        run_id=run.run_id,
        snapshot=snapshot,
        extra={"advanced": advanced},
    )


def _rechain_scenario(root: Path) -> Scenario:
    registry, run = rechain_fixture._registry(root)
    authority = rechain_fixture._authority()
    authority.github_provider_revision = None
    return Scenario(
        action="rechain",
        root=root,
        registry=registry,
        state_path=root / "state.json",
        args=rechain_fixture._action_args(run),
        repo=rechain_fixture.REPO,
        work_id=rechain_fixture.WORK_ID,
        run_id=run.run_id,
        authority=authority,
    )


def _supersede_attempt_scenario(root: Path) -> Scenario:
    registry, run = rechain_fixture._registry(root)
    accepted_job = supersession_fixture._seed_job(registry, run)
    job = supersession_fixture._seed_job(registry, run, status="failed", with_evidence=False)
    authority = rechain_fixture._authority()
    authority.github_provider_revision = None
    return Scenario(
        action="supersede-attempt", root=root, registry=registry,
        state_path=root / "state.json",
        args=supersession_fixture._args(run, job), repo=rechain_fixture.REPO,
        work_id=rechain_fixture.WORK_ID, run_id=run.run_id,
        authority=authority,
        extra={"old_job_id": job["job_id"], "accepted_job_id": accepted_job["job_id"]},
    )


SCENARIOS: dict[str, Callable[[Path], Scenario]] = {
    "resume": _resume_scenario,
    "retry-build": _retry_build_scenario,
    "retry-card": _retry_card_scenario,
    "retry-verify": _retry_verify_scenario,
    "retry-review": _retry_review_scenario,
    "recover-planning": _recover_planning_scenario,
    "recover-pre-candidate": _recover_pre_candidate_scenario,
    "recover-repair-commit": _recover_repair_commit_scenario,
    "regenerate-gates": _regenerate_gates_scenario,
    "abandon": _abandon_scenario,
    "retire-delivered": _retire_delivered_scenario,
    "recover-superseded": _recover_superseded_scenario,
    "reset-reclaim-budget": _reset_reclaim_budget_scenario,
    "refreeze-base": _refreeze_base_scenario,
    "rechain": _rechain_scenario,
    "supersede-attempt": _supersede_attempt_scenario,
}


def _prepare(action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    root = tmp_path / "scenario"
    root.mkdir()
    # recover-pre-candidate 的 handoff 目錄是相對路徑（autonomy.DEFAULT_HANDOFF_DIR）；
    # 一律以情境根為 cwd，避免任何 action 寫進 checkout。
    monkeypatch.chdir(root)
    # regenerate-gates 依當前 gate 宣告重跑；以確定性命令代表 gate。
    monkeypatch.setenv("PSC_GATE_CMD_PYTEST", "python3 -c pass")
    sc = SCENARIOS[action](root)
    for name, value in sc.env.items():
        monkeypatch.setenv(name, value)
    for target, name, value in sc.patches:
        monkeypatch.setattr(target, name, value)
    _install_authority_seam(sc, monkeypatch)
    return sc


# ---------------------------------------------------------------------------
# R02 正例：正式入口 → 全新 registry 重讀 → attention read model
# ---------------------------------------------------------------------------


def _expect_resume(sc: Scenario, result: dict, fresh: JobRegistry, attention: dict) -> None:
    payload = result["result"]
    run = fresh.get_workflow_run(sc.run_id)
    jobs = fresh.list_jobs()
    assert payload["job_id"] != sc.extra["failed_job_id"]
    assert [job["job_id"] for job in jobs] == [sc.extra["failed_job_id"], payload["job_id"]]
    assert fresh.get_job(payload["job_id"])["workflow_card"] == "subagent-build"
    assert "needs_human" not in run.facets
    assert sc.run_id not in attention


def _expect_retry_build(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    assert result["result"]["action"] == "retry-build"
    assert run.current_phase == "build"
    assert run.candidate_head == HEAD
    assert "needs_human" not in run.facets
    build_steps = [step for step in run.steps if step.phase == "build"]
    assert build_steps[-1].gate_result == "pending"
    assert sc.run_id not in attention


def _expect_retry_card(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    assert result["result"]["reason"] == "builder-card-redispatched"
    assert "needs_human" not in run.facets
    assert next(step for step in run.steps if step.card == "tdd-red").gate_result == "pending"
    assert fresh.get_job(sc.extra["old_job_id"])["status"] == "exited"
    assert sc.run_id not in attention


def _expect_retry_verify(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    assert result["result"]["action"] == "retry-verify"
    assert run.current_phase == "verify"
    assert "needs_human" not in run.facets
    assert all(step.gate_result == "pending" for step in run.steps if step.phase == "verify")
    # 沒有精準 reviewer terminal recovery 的舊 exited verify job 改標 failed。
    assert fresh.get_job(sc.extra["old_job_id"])["status"] == "failed"
    assert sc.run_id not in attention


def _expect_retry_review(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    assert result["result"]["action"] == "retry-review"
    assert run.current_phase == "review"
    assert run.verified_head == review_fixture.HEAD
    assert "needs_human" not in run.facets
    assert sc.run_id not in attention


def _expect_recover_planning(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    assert result["result"]["reason"] == "planning-recovery-unblocked"
    assert "needs_human" not in run.facets
    assert fresh.list_jobs() == []
    assert sc.run_id not in attention


def _expect_recover_pre_candidate(sc, result, fresh, attention) -> None:
    row = fresh.get_slice(sc.extra["slice_id"])
    assert result["result"]["reason"] == "pre-candidate-slice-reset"
    assert (row["state"], row["gate_state"], row["builder_job_id"]) == ("pending", "pending", None)
    # work 層只重置 owner slice，WorkflowRun 的 needs_human facet 不動；read model 仍
    # 提供 recover-pre-candidate，因為 admission 對「pending 且無 binding」回冪等
    # already-recovered（再送一次不再改任何狀態）。
    assert "needs_human" in fresh.get_workflow_run(sc.run_id).facets
    assert "recover-pre-candidate" in attention[sc.run_id]["next_actions"]
    durable = _durable_state(sc)
    replay = _submit(sc)
    assert replay["result"]["reason"] == "already-recovered"
    assert _durable_state(sc) == durable


def _expect_recover_repair_commit(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    assert result["result"]["reason"] == "repair-commit-adopted"
    assert run.candidate_head == sc.extra["repaired_head"]
    assert run.current_phase == "verify"
    assert "needs_human" not in run.facets
    assert sc.run_id not in attention


def _expect_regenerate_gates(sc, result, fresh, attention) -> None:
    assert result["result"]["reason"] == "gate-ledger-regenerated"
    payload = json.loads(sc.extra["ledger"].read_text(encoding="utf-8"))
    assert [row["name"] for row in payload["gates"]] == ["pytest"]
    # 只重生成證據、不改判：run 仍在 attention，下一步由 resume 重新採信。
    assert "needs_human" in fresh.get_workflow_run(sc.run_id).facets
    assert sc.run_id in attention


def _expect_abandon(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    assert result["result"]["action"] == "abandoned"
    assert run.status == "superseded"
    assert result["result"]["evidence"]["ref"] in run.evidence_refs
    assert sc.run_id not in attention


def _expect_retire_delivered(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    assert run.status == "superseded"
    assert result["result"]["evidence"]["ref"] in run.evidence_refs
    assert sc.run_id not in attention


def _expect_recover_superseded(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    assert result["result"]["action"] == "recovered-superseded"
    assert (run.status, run.current_phase) == ("ongoing", "verify")
    assert result["result"]["evidence"]["ref"] in run.evidence_refs
    assert fresh.list_jobs() == []
    assert sc.run_id not in attention


def _expect_reset_reclaim_budget(sc, result, fresh, attention) -> None:
    payload = result["result"]
    assert payload["action"] == "reclaim-budget-reset"
    resets = fresh.list_reclaim_resets()
    assert len(resets) == 1
    assert sorted(resets[0]["cleared_run_ids"]) == sorted(sc.extra["burned"])
    assert resets[0]["evidence_ref"] == payload["evidence"]["ref"]
    # 熔斷水位只追加授權列，run 歷史一列不動、不建立 Job。
    assert all(run.status == "superseded" for run in fresh.list_workflow_runs())
    assert fresh.list_jobs() == []
    assert attention == {}


def _expect_refreeze_base(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    assert result["result"]["reason"] == "candidate-base-refrozen"
    assert run.frozen_readiness["base_sha"] == sc.extra["advanced"]
    assert run.candidate_head is None
    # 只換凍結基底、不改 phase／facet、不派工：run 仍待 operator 下一步。
    assert "needs_human" in run.facets
    assert fresh.list_jobs() == []
    assert sc.run_id in attention


def _expect_rechain(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    assert result["result"]["action"] == "rechain"
    assert run.model_chain_override == {
        "planner": {"executor": "claude", "model_id": "sonnet"},
        "builder": {"executor": "claude", "model_id": "sonnet"},
        "reviewer": {"executor": "agy", "model_id": "gemini-3.1-pro-high"},
    }
    assert run.resolved_model_chain is None
    assert "needs_human" not in run.facets
    assert run.evidence_refs[-1] == result["result"]["evidence"]["ref"]
    assert len(fresh.list_jobs()) == 0
    assert len(rechain_fixture._audit_files(sc.root)) == 1
    assert sc.run_id not in attention


def _expect_supersede_attempt(sc, result, fresh, attention) -> None:
    run = fresh.get_workflow_run(sc.run_id)
    old_job = fresh.get_job(sc.extra["old_job_id"])
    assert result["result"]["action"] == "supersede-attempt"
    assert result["result"]["reason"] == "attempt-superseded"
    assert old_job["status"] == "failed"
    assert old_job["workflow_stage_execution_receipt"]["key"]
    accepted_job = fresh.get_job(sc.extra["accepted_job_id"])
    assert accepted_job["workflow_evidence"] is not None
    assert result["result"]["evidence"]["ref"] in run.evidence_refs
    assert "needs_human" not in run.facets
    assert fresh.list_jobs() == [accepted_job, old_job]
    assert sc.run_id not in attention


POSITIVE_EXPECTATIONS: dict[str, Callable[..., None]] = {
    "resume": _expect_resume,
    "retry-build": _expect_retry_build,
    "retry-card": _expect_retry_card,
    "retry-verify": _expect_retry_verify,
    "retry-review": _expect_retry_review,
    "recover-planning": _expect_recover_planning,
    "recover-pre-candidate": _expect_recover_pre_candidate,
    "recover-repair-commit": _expect_recover_repair_commit,
    "regenerate-gates": _expect_regenerate_gates,
    "abandon": _expect_abandon,
    "retire-delivered": _expect_retire_delivered,
    "recover-superseded": _expect_recover_superseded,
    "reset-reclaim-budget": _expect_reset_reclaim_budget,
    "refreeze-base": _expect_refreeze_base,
    "rechain": _expect_rechain,
    "supersede-attempt": _expect_supersede_attempt,
}


def test_r02_matrix_covers_every_registered_work_recovery_action() -> None:
    assert set(SCENARIOS) == set(contracts.RECOVERY_WORK_ACTIONS)
    assert set(POSITIVE_EXPECTATIONS) == set(contracts.RECOVERY_WORK_ACTIONS)


@pytest.mark.parametrize("action", sorted(contracts.RECOVERY_WORK_ACTIONS))
def test_r02_public_entry_positive_reaches_persisted_registry_and_read_model(
    action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sc = _prepare(action, tmp_path, monkeypatch)
    before = _attention(sc, monkeypatch)
    if sc.run_id is not None and action not in {"recover-superseded"}:
        # 前置：正例情境確實出現在 attention（needs_human）。
        assert sc.run_id in before

    result = _submit(sc)

    fresh = _fresh(sc)
    POSITIVE_EXPECTATIONS[action](sc, result, fresh, _attention(sc, monkeypatch))


# ---------------------------------------------------------------------------
# R02 負例：每個 action×類別是 probe、冪等 no-op、strict xfail 缺口或明確 N/A
# ---------------------------------------------------------------------------


def _without(args: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in args.items() if key not in keys}


def _args_probe(**override: Any) -> Callable[[Scenario], dict[str, Any]]:
    def probe(sc: Scenario) -> dict[str, Any]:
        return {**sc.args, **override}

    return probe


def _drop_probe(*keys: str) -> Callable[[Scenario], dict[str, Any]]:
    return lambda sc: _without(sc.args, *keys)


def _stale_run_probe(sc: Scenario) -> dict[str, Any]:
    return {**sc.args, "expected_run_id": OTHER_RUN_ID}


def _phase_probe(phase: str) -> Callable[[Scenario], dict[str, Any]]:
    def probe(sc: Scenario) -> dict[str, Any]:
        _force_run_fields(sc.registry, sc.run_id, current_phase=phase)
        return dict(sc.args)

    return probe


def _active_job_probe(*, phase: str = "build") -> Callable[[Scenario], dict[str, Any]]:
    def probe(sc: Scenario) -> dict[str, Any]:
        _active_job(sc.registry, sc.run_id, phase=phase)
        return dict(sc.args)

    return probe


def _facets_probe(*facets: str) -> Callable[[Scenario], dict[str, Any]]:
    def probe(sc: Scenario) -> dict[str, Any]:
        _force_run_fields(
            sc.registry, sc.run_id, facets=tuple(facets), needs_human_reason=None
        )
        return dict(sc.args)

    return probe


def _pre_candidate_active_writer(sc: Scenario) -> dict[str, Any]:
    row = sc.registry.get_slice(sc.extra["slice_id"])
    job = sc.registry.create_job(
        task="work-slice",
        persona="builder",
        branch="feature/work",
        pane="",
        worktree=str(sc.root / "active-writer"),
        owner_identity=row["owner_identity"],
        attempt_id=row["attempt_id"],
    )
    sc.registry.update_status(job["job_id"], "running")
    return dict(sc.args)


def _repair_stale_candidate(sc: Scenario) -> dict[str, Any]:
    return {**sc.args, "expected_candidate": DRIFTED_HEAD}


def _retire_open_pr(sc: Scenario) -> dict[str, Any]:
    sc.execute_kwargs["runner"] = work_fixture._pr_lifecycle_runner(
        {110: {"state": "open", "merged_at": None}}
    )
    return dict(sc.args)


def _abandon_delivered(sc: Scenario) -> dict[str, Any]:
    _force_run_fields(sc.registry, sc.run_id, pr_refs=("acme/demo#8",))
    return dict(sc.args)


def _superseded_still_ongoing(sc: Scenario) -> dict[str, Any]:
    _force_run_fields(sc.registry, sc.run_id, status="ongoing")
    return dict(sc.args)


def _reclaim_nothing_to_reset(sc: Scenario) -> dict[str, Any]:
    fresh_root = sc.root / "no-generation"
    fresh_root.mkdir()
    sc.registry = JobRegistry(state_path=fresh_root / "jobs.json")
    sc.registry.list_jobs()
    return dict(sc.args)


def _refreeze_with_candidate(sc: Scenario) -> dict[str, Any]:
    _force_run_fields(sc.registry, sc.run_id, candidate_head="a" * 40)
    return dict(sc.args)


def _regenerate_wrong_card(sc: Scenario) -> dict[str, Any]:
    return {**sc.args, "card": "worktree-isolation"}


N_A = "n/a"
NOOP = "noop"
GAP = "gap"

#: action → 類別 → 下列之一：
#: - probe（callable）：必須拒絕，且 registry／evidence／job 數／attention 不變。
#: - (NOOP, probe, reason, 說明)：不拒絕但回無副作用的冪等回應；狀態同樣不變。
#: - (GAP, probe, gap_id)：契約應拒絕、目前未拒絕的已知缺口（strict xfail）。
#: - (N_A, 理由)：理由必須指出契約為何不適用，而不是「沒寫測試」。
NEGATIVE_MATRIX: dict[str, dict[str, Any]] = {
    "resume": {
        "wrong-phase": (N_A, "resume 以 WorkAuthority 找唯一 canonical run，任何 phase 都回 claim decision；phase 由後續 resume_workflow_run 處理"),
        "wrong-card": (N_A, "resume 沒有 card selector"),
        "active-job": (N_A, "active job 屬 resume_workflow_run 的 dispatch admission（不重複派工），不是 work-action 的拒絕條件"),
        "missing-actor-reason": (N_A, "resume 不收 actor／reason"),
        "stale-exact-run": (N_A, "resume 沒有 expected-run CAS；以 claim era 與 authority digest 定位 canonical run"),
        "wrong-state": (N_A, "resume 對非 needs_human run 回 claim decision，不是拒絕"),
    },
    "retry-build": {
        "wrong-phase": _phase_probe("plan"),
        "wrong-card": (N_A, "retry-build 固定重開最後一張 builder 卡，沒有 card selector"),
        "active-job": _active_job_probe(),
        "missing-actor-reason": (N_A, "actor／reason 選填；只有 post-pass adjudication 才強制 reason"),
        "stale-exact-run": _args_probe(expected_candidate=DRIFTED_HEAD),
        "wrong-state": _facets_probe(),
    },
    "retry-card": {
        "wrong-phase": _phase_probe("define"),
        "wrong-card": _args_probe(card="subagent-build"),
        "active-job": _active_job_probe(),
        "missing-actor-reason": (N_A, "actor 選填；reason 只是選填的 operator 裁決"),
        "stale-exact-run": _stale_run_probe,
        "wrong-state": _facets_probe(),
    },
    "retry-verify": {
        "wrong-phase": _phase_probe("build"),
        "wrong-card": (N_A, "retry-verify 是 phase 級 reset，沒有 card selector"),
        "active-job": _active_job_probe(phase="verify"),
        "missing-actor-reason": (N_A, "retry-verify 不收 reason，actor 選填"),
        "stale-exact-run": _args_probe(expected_candidate=DRIFTED_HEAD),
        "wrong-state": _facets_probe(),
    },
    "retry-review": {
        "wrong-phase": _phase_probe("verify"),
        "wrong-card": (N_A, "retry-review 是 phase 級 reset，沒有 card selector"),
        "active-job": _active_job_probe(phase="review"),
        "missing-actor-reason": (N_A, "actor 選填；reason 只是選填的 operator 裁決"),
        "stale-exact-run": _args_probe(expected_candidate=DRIFTED_HEAD),
        "wrong-state": _facets_probe(),
    },
    "recover-planning": {
        "wrong-phase": (
            NOOP,
            _phase_probe("plan"),
            "already-recovered",
            "#256 設計：run 已離開 define 時一律回冪等 already-recovered（即使沒有"
            " planning-recovery 紀錄），不改任何狀態；回應名稱不代表本次有恢復",
        ),
        "wrong-card": (N_A, "recover-planning 沒有 card selector"),
        "active-job": (N_A, "recover-planning 只 unblock、不派工；是否派 planner 由下一拍 dispatch admission 決定"),
        "missing-actor-reason": _drop_probe("failure_reason"),
        "stale-exact-run": _stale_run_probe,
        "wrong-state": _facets_probe(),
    },
    "recover-pre-candidate": {
        "wrong-phase": (N_A, "admission 以 owner slice／builder job／attempt marker 為準，不看 workflow phase"),
        "wrong-card": (N_A, "recover-pre-candidate 沒有 card selector"),
        "active-job": _pre_candidate_active_writer,
        "missing-actor-reason": (N_A, "actor 缺席時以 requested_by 記錄；不收 reason"),
        "stale-exact-run": _args_probe(expected_candidate=HEAD),
        "wrong-state": (N_A, "slice 已 pending 且無 binding 時是 already-recovered 冪等回應，不是拒絕"),
    },
    "recover-repair-commit": {
        "wrong-phase": _phase_probe("verify"),
        "wrong-card": (N_A, "只接受最後一張 build card，沒有 card selector"),
        "active-job": _active_job_probe(),
        "missing-actor-reason": (N_A, "actor 缺席時以 requested_by 記錄；不收 reason"),
        "stale-exact-run": _stale_run_probe,
        "wrong-state": _repair_stale_candidate,
    },
    "regenerate-gates": {
        "wrong-phase": (N_A, "只重跑 gate-ledger phase 的 terminal job ledger，不以 run phase 判定"),
        "wrong-card": _regenerate_wrong_card,
        "active-job": (N_A, "只重寫已 terminal 的舊 job ledger，不建立 Job、不改 run，active job 不受影響"),
        "missing-actor-reason": (N_A, "regenerate-gates 不收 reason，actor 選填"),
        "stale-exact-run": _stale_run_probe,
        "wrong-state": _facets_probe(),
    },
    "abandon": {
        "wrong-phase": (N_A, "claim–review 皆屬 pre-delivery 可 abandon；ship phase 與 PR refs 同屬 pre-delivery admission，由 wrong-state probe 以 PR refs 覆蓋"),
        "wrong-card": (N_A, "abandon 沒有 card selector"),
        "active-job": _active_job_probe(),
        "missing-actor-reason": _drop_probe("reason"),
        "stale-exact-run": _stale_run_probe,
        "wrong-state": _abandon_delivered,
    },
    "retire-delivered": {
        "wrong-phase": (N_A, "admission 以 ongoing、PR refs 與每個 PR terminal proof 為準，不看 phase"),
        "wrong-card": (N_A, "retire-delivered 沒有 card selector"),
        "active-job": _active_job_probe(),
        "missing-actor-reason": _drop_probe("reason"),
        "stale-exact-run": _stale_run_probe,
        "wrong-state": _retire_open_pr,
    },
    "recover-superseded": {
        "wrong-phase": _phase_probe("build"),
        "wrong-card": (N_A, "recover-superseded 沒有 card selector"),
        "active-job": _active_job_probe(phase="verify"),
        "missing-actor-reason": _drop_probe("reason"),
        "stale-exact-run": _stale_run_probe,
        "wrong-state": _superseded_still_ongoing,
    },
    "reset-reclaim-budget": {
        "wrong-phase": (N_A, "reclaim ledger 操作，不取得任何 run"),
        "wrong-card": (N_A, "reset-reclaim-budget 沒有 card selector"),
        "active-job": (N_A, "不取得 workflow／job ownership，也不建立 Job"),
        "missing-actor-reason": _drop_probe("reason"),
        "stale-exact-run": (N_A, "熔斷前提是沒有 active run 可 CAS；以 superseded 世代集合為準"),
        "wrong-state": _reclaim_nothing_to_reset,
    },
    "refreeze-base": {
        "wrong-phase": _phase_probe("verify"),
        "wrong-card": (N_A, "refreeze-base 沒有 card selector"),
        "active-job": _active_job_probe(),
        "missing-actor-reason": _drop_probe("reason"),
        "stale-exact-run": _stale_run_probe,
        "wrong-state": _refreeze_with_candidate,
    },
    "rechain": {
        "wrong-phase": _phase_probe("define"),
        "wrong-card": (N_A, "rechain 以精確 run／candidate／era CAS 定位，沒有 card selector"),
        "active-job": _active_job_probe(),
        "missing-actor-reason": _drop_probe("actor", "reason"),
        "stale-exact-run": _stale_run_probe,
        "wrong-state": _facets_probe(),
    },
    "supersede-attempt": {
        "wrong-phase": _phase_probe("define"),
        "wrong-card": _args_probe(card="other-card"),
        "active-job": _active_job_probe(),
        "missing-actor-reason": _drop_probe("actor", "reason"),
        "stale-exact-run": _stale_run_probe,
        "wrong-state": _facets_probe(),
    },
}


def _negative_probe(entry: Any) -> Callable[[Scenario], dict[str, Any]]:
    if callable(entry):
        return entry
    return entry[1]


def _negative_cases() -> list[Any]:
    cases: list[Any] = []
    for action, row in sorted(NEGATIVE_MATRIX.items()):
        for category, entry in row.items():
            if callable(entry) or entry[0] == NOOP:
                cases.append(pytest.param(action, category, id=f"{action}-{category}"))
            elif entry[0] == GAP:
                cases.append(
                    pytest.param(
                        action,
                        category,
                        id=f"{action}-{category}",
                        marks=pytest.mark.xfail(
                            strict=True,
                            raises=pytest.fail.Exception,
                            reason=f"known gap {entry[2]}: 正式入口未拒絕",
                        ),
                    )
                )
    return cases


def test_r02_negative_matrix_classifies_every_action_and_category() -> None:
    assert set(NEGATIVE_MATRIX) == set(contracts.RECOVERY_WORK_ACTIONS)
    ledger = json.loads(GAP_LEDGER_PATH.read_text(encoding="utf-8"))
    open_gap_ids = {gap["id"] for gap in ledger["gaps"] if gap["status"] != "closed"}
    for action, row in NEGATIVE_MATRIX.items():
        assert set(row) == set(NEGATIVE_CATEGORIES), action
        for category, entry in row.items():
            if callable(entry):
                continue
            kind = entry[0]
            if kind == N_A:
                assert isinstance(entry[1], str) and entry[1].strip(), (action, category)
            elif kind == NOOP:
                assert callable(entry[1]) and entry[2] and entry[3].strip(), (action, category)
            else:
                assert kind == GAP and callable(entry[1]), (action, category)
                assert entry[2] in open_gap_ids, (action, category, entry[2])
    # 有實際 probe（含 noop／gap）的組合數下限，避免整列被改成 N/A 仍綠。
    assert len(_negative_cases()) >= 40


@pytest.mark.parametrize(("action", "category"), _negative_cases())
def test_r02_public_entry_rejection_leaves_registry_evidence_jobs_and_read_model_unchanged(
    action: str, category: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sc = _prepare(action, tmp_path, monkeypatch)
    entry = NEGATIVE_MATRIX[action][category]
    args = _negative_probe(entry)(sc)
    before = _observe(sc, monkeypatch)

    if not callable(entry) and entry[0] == NOOP:
        result = _submit(sc, args)
        assert result["result"]["reason"] == entry[2]
    else:
        with pytest.raises((RuntimeError, ValueError)):
            _submit(sc, args)

    after = _observe(sc, monkeypatch)
    assert after["durable"] == before["durable"]
    assert len(after["durable"]["jobs"]) == len(before["durable"]["jobs"])
    assert after["files"] == before["files"]
    assert after["attention"] == before["attention"]


def test_r02_recover_pre_candidate_refuses_a_still_running_bound_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """綁定的 builder job 仍在跑時，拒絕回收並維持 registry/read model 零副作用。"""

    sc = _prepare("recover-pre-candidate", tmp_path, monkeypatch)
    row = sc.registry.get_slice(sc.extra["slice_id"])
    sc.registry._find_job(row["builder_job_id"])["status"] = "running"
    sc.registry._persist()
    before = _observe(sc, monkeypatch)

    with pytest.raises((RuntimeError, ValueError)):
        _submit(sc)

    assert _observe(sc, monkeypatch) == before
    # read model 與 admission 同源：active writer 存在時不投影 recover-pre-candidate。
    assert "recover-pre-candidate" not in before["attention"][sc.run_id]["next_actions"]


def test_r02_recover_pre_candidate_refuses_an_active_sibling_for_same_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sc = _prepare("recover-pre-candidate", tmp_path, monkeypatch)
    row = sc.registry.get_slice(sc.extra["slice_id"])
    sibling = sc.registry.create_job(
        task=row["slice_id"],
        persona="reviewer",
        branch="feature/recovery",
        pane="",
        worktree=str(tmp_path / "sibling-worktree"),
        owner_identity=row["owner_identity"],
        attempt_id=row["attempt_id"],
    )
    before = _observe(sc, monkeypatch)

    with pytest.raises((RuntimeError, ValueError), match="active builder attempt"):
        _submit(sc)

    assert _observe(sc, monkeypatch) == before
    assert sc.registry.get_job(sibling["job_id"])["status"] == "dispatched"


# ---------------------------------------------------------------------------
# R03：admission 讀完、第一次持久化之前注入另一個 writer 的 drift
# ---------------------------------------------------------------------------


_DRIFT_CLAIM_KEY = "claim:v1:" + "9" * 64


def _drift_new_generation(sc: Scenario, other: JobRegistry) -> None:
    """另一個 writer 以新 claim era 建立新 generation（同 work 的舊 run 被 supersede）。"""

    current = other.get_workflow_run(sc.run_id)
    new_run = other._manager_create_workflow_run(
        work_id=current.work_id,
        repo=current.repo,
        claim_key=_DRIFT_CLAIM_KEY,
        source_revision="9" * 64,
        workspace_root=current.workspace_root,
        combo=current.combo,
        current_phase="claim",
        steps=current.steps,
        issue_refs=current.issue_refs,
        openspec_refs=current.openspec_refs,
        gate_status="running",
    )
    sc.extra["drift_run_id"] = new_run.run_id


def _drift_candidate(sc: Scenario, other: JobRegistry) -> None:
    """另一個 writer 採信了新的 Candidate（新 candidate generation）。"""

    current = other.get_workflow_run(sc.run_id)
    fields: dict[str, Any] = {"candidate_head": DRIFTED_HEAD}
    if current.verified_head is not None:
        fields["verified_head"] = DRIFTED_HEAD
    other._manager_update_workflow_run(sc.run_id, **fields)


def _drift_slice_binding(sc: Scenario, other: JobRegistry) -> None:
    """另一個 writer 為同一 owner slice 綁上新的 builder attempt。"""

    row = other.get_slice(sc.extra["slice_id"])
    job = other.create_job(
        task=row["slice_id"],
        persona="builder",
        branch="feature/work",
        pane="",
        worktree=str(sc.root / "drift-builder"),
        owner_identity=row["owner_identity"],
        attempt_id=row["attempt_id"],
    )
    other.update_headless_result(job["job_id"], status="failed", exit_code=1)
    other.update_slice(row["slice_id"], builder_job_id=job["job_id"])
    sc.extra["drift_job_id"] = job["job_id"]


def _drift_new_superseded_generation(sc: Scenario, other: JobRegistry) -> None:
    """熔斷水位的 read 之後，又有一個世代被 supersede。"""

    sc.extra["drift_burned"] = reclaim_fixture._burn_generations(other, 1, offset=7)


#: action → drift 注入方式。resume 的注入點在 daemon 的 resume_workflow_run 派工
#: 持久化之前；regenerate-gates 不寫 registry，注入點改在 gate 執行（ledger 寫入）
#: 之前。
DRIFT_MATRIX: dict[str, Callable[[Scenario, JobRegistry], None]] = {
    "resume": _drift_new_generation,
    "retry-build": _drift_candidate,
    "retry-card": _drift_new_generation,
    "retry-verify": _drift_candidate,
    "retry-review": _drift_candidate,
    "recover-planning": _drift_new_generation,
    "recover-pre-candidate": _drift_slice_binding,
    "recover-repair-commit": _drift_candidate,
    "regenerate-gates": _drift_new_generation,
    "abandon": _drift_new_generation,
    "retire-delivered": _drift_new_generation,
    "recover-superseded": _drift_new_generation,
    "reset-reclaim-budget": _drift_new_superseded_generation,
    "refreeze-base": _drift_new_generation,
    "rechain": _drift_candidate,
    "supersede-attempt": _drift_candidate,
}

#: action 在第一次 registry 持久化之前就依 #275 順序先落地的 content-addressed
#: 稽核檔（prepare record）。drift 拒絕後允許留下，但不得被任何持久狀態引用。
PREPARE_RECORD_DIRS: dict[str, tuple[str, ...]] = {
    # engineering-outcomes：#275 先寫 canonical outcome 再轉 registry；commit 被
    # drift 拒絕時殘留的 outcome 是已知缺口，另由下方 strict xfail 測試列管。
    "abandon": ("evidence/work-abandon", "engineering-outcomes/"),
    "retire-delivered": ("evidence/work-retire-delivered", "engineering-outcomes/"),
    # claim decision 讀取時 lazy 初始化 delivery journal 的衍生列（以 run_id 為鍵），
    # 早於 daemon 的 resume 派工，與 recovery commit 無關。
    "resume": ("runs.json",),
    "recover-superseded": ("evidence/work-recover-superseded",),
    "recover-planning": ("evidence/planning-recovery",),
    "recover-repair-commit": ("evidence/work-repair-adoption",),
    "reset-reclaim-budget": ("evidence/work-reclaim-reset",),
    "refreeze-base": ("evidence/work-candidate-base-refreeze",),
    "rechain": ("evidence/work-model-chain-readjudication",),
    "supersede-attempt": ("evidence/work-attempt-supersession",),
}


def test_r03_drift_matrix_covers_every_registered_work_recovery_action() -> None:
    assert set(DRIFT_MATRIX) == set(contracts.RECOVERY_WORK_ACTIONS)


def _install_drift(
    sc: Scenario, monkeypatch: pytest.MonkeyPatch, drift: Callable[[Scenario, JobRegistry], None]
) -> list[dict[str, Any]]:
    fired: list[dict[str, Any]] = []

    def inject() -> None:
        if fired:
            return
        other = JobRegistry(state_path=sc.registry._state_path)
        drift(sc, other)
        fired.append(_durable_state(sc))

    if sc.action == "regenerate-gates":
        original_run = gate_runner.run_declared_gates

        def run_declared_gates(**kwargs):
            inject()
            return original_run(**kwargs)

        monkeypatch.setattr(gate_runner, "run_declared_gates", run_declared_gates)
        return fired

    original_persist = sc.registry._persist

    def persist() -> None:
        inject()
        return original_persist()

    monkeypatch.setattr(sc.registry, "_persist", persist)
    return fired


@pytest.mark.parametrize("action", sorted(contracts.RECOVERY_WORK_ACTIONS))
def test_r03_public_entry_fails_closed_on_read_to_commit_drift(
    action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sc = _prepare(action, tmp_path, monkeypatch)
    files_before = _files(sc.root)
    fired = _install_drift(sc, monkeypatch, DRIFT_MATRIX[action])

    if action == "regenerate-gates":
        # 不寫 registry：只重寫 admission 當下選定的舊 terminal job ledger。
        result = _submit(sc)
        assert result["result"]["run_id"] == sc.run_id
    else:
        with pytest.raises((RegistryRevisionConflict, RuntimeError, ValueError)):
            _submit(sc)

    assert len(fired) == 1, "drift 必須在第一次持久化之前注入"
    drifted = fired[0]
    after = _durable_state(sc)
    # 正式入口沒有任何一筆寫入落在 drift 之後的 generation 上。
    assert after == drifted
    new_run_id = sc.extra.get("drift_run_id")
    if new_run_id is not None:
        assert after["runs"][new_run_id]["status"] == "ongoing"
    # drift 前先落地的 prepare record 不得被持久狀態引用（不能成為部分寫入）。
    durable_text = json.dumps(after, sort_keys=True)
    allowed = PREPARE_RECORD_DIRS.get(action, ())
    changed = {
        path
        for path, digest in _files(sc.root).items()
        if files_before.get(path) != digest and not path.endswith("/")
    }
    for path in sorted(changed):
        if path.startswith("jobs.json") or path.startswith("logs/"):
            continue
        assert any(path.startswith(prefix) for prefix in allowed), path
        assert Path(path).name not in durable_text, path


def _drift_active_job(sc: Scenario, other: JobRegistry) -> None:
    """另一個 writer 在 admission 之後為同一 run 派出新 job（run 仍 ongoing）。"""

    sc.extra["drift_job_id"] = _active_job(other, sc.run_id, card="drift-card")["job_id"]


def _outcome_rows(sc: Scenario) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((sc.root / "engineering-outcomes").glob("*.jsonl")):
        rows.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return rows


@pytest.mark.parametrize("action", ["abandon", "retire-delivered"])
def test_r03_rejected_retirement_leaves_no_terminal_engineering_outcome(
    action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """drift 讓 commit 被拒、run 仍 ongoing 且有 active job 時，外部 outbox 不得
    出現本動作的終局 outcome（目前 #275 先寫 outcome 才轉 registry）。"""

    sc = _prepare(action, tmp_path, monkeypatch)
    outcomes_before = _outcome_rows(sc)
    fired = _install_drift(sc, monkeypatch, _drift_active_job)

    with pytest.raises((RegistryRevisionConflict, RuntimeError, ValueError)):
        _submit(sc)

    assert len(fired) == 1
    run = _fresh(sc).get_workflow_run(sc.run_id)
    assert run.status == "ongoing"
    assert _outcome_rows(sc) == outcomes_before


@pytest.mark.parametrize("action", ["abandon", "retire-delivered"])
def test_r03_post_commit_crash_replay_emits_outcome_once(
    action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registry commit 前進但 outcome append crash 時，重啟重送會冪等補齊 outcome。"""

    sc = _prepare(action, tmp_path, monkeypatch)
    outcomes_before = _outcome_rows(sc)
    emit = work_actions.engineering_outcome.emit_outcome
    calls = 0

    def crash_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected crash between registry commit and outcome append")
        return emit(*args, **kwargs)

    monkeypatch.setattr(work_actions.engineering_outcome, "emit_outcome", crash_once)
    with pytest.raises(RuntimeError, match="between registry commit and outcome append"):
        _submit(sc)

    committed = _fresh(sc).get_workflow_run(sc.run_id)
    assert committed.status == "superseded"
    assert _outcome_rows(sc) == outcomes_before

    sc.registry = _fresh(sc)
    replay = _submit(sc)
    assert replay["result"]["action"] in {"abandoned", "retired-delivered"}
    assert len(_outcome_rows(sc)) == len(outcomes_before) + 1

    _submit(sc)
    assert len(_outcome_rows(sc)) == len(outcomes_before) + 1


# ---------------------------------------------------------------------------
# R09：read model 投影出的每個 next_action 原樣送回正式入口
# ---------------------------------------------------------------------------


def _planning_failure_from_evidence(entry: dict[str, Any]) -> dict[str, str]:
    """operator 依 attention 的 evidence_refs 讀阻塞證據，取得逐字 classification／reason。"""

    for ref in entry["evidence_refs"]:
        path = Path(ref)
        if path.parent.name != "planning-recovery" or not path.is_file():
            continue
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("schema") == "cortex-planning-failure/v1":
            return {
                "failure_classification": body["classification"],
                "failure_reason": body["reason"],
            }
    raise AssertionError("read model evidence_refs 沒有 planning failure 紀錄")


def _request_from_read_model(entry: dict[str, Any], action: str) -> dict[str, Any]:
    """只用 attention 條目（含其 hint 與 evidence_refs）與 operator 自填的 actor／reason 組請求。"""

    request: dict[str, Any] = {
        "action": action,
        "repo": entry["repo"],
        "work_id": entry["work_id"],
    }
    run_id = entry["run_id"]
    if action in {"abandon", "retire-delivered"}:
        request.update(expected_run_id=run_id, actor="operator", reason="read-model round trip")
    elif action == "retry-card":
        request.update(expected_run_id=run_id, card=entry["card"])
    elif action == "regenerate-gates":
        request.update(expected_run_id=run_id)
    elif action == "recover-planning":
        request.update(expected_run_id=run_id, **_planning_failure_from_evidence(entry))
    elif action in {"retry-review", "retry-build"}:
        match = re.search(r"--expected-candidate ([0-9a-f]{40})", entry["next_step_hint"])
        assert match is not None, entry["next_step_hint"]
        request.update(
            expected_candidate=match.group(1),
            actor="operator",
            reason="read-model round trip adjudication",
        )
    elif action == "resume":
        pass
    elif action != "recover-pre-candidate":
        raise AssertionError(f"R09 尚未定義 {action} 的 read-model 請求組法")
    return request


def _blocking_findings_run(root: Path, *, pr_refs: tuple[str, ...] | None) -> Scenario:
    """review 退回 blocking-findings；Candidate 是 workspace 內真的 commit，讓
    retry-build 的 exact Candidate tree 檢查（mapped_openspec 單一 change）可實跑。"""

    sc = _retry_review_scenario(root)
    workspace = Path(sc.registry.get_workflow_run(sc.run_id).workspace_root)
    candidate = repair_fixture._init_repair_worktree(workspace)
    fields: dict[str, Any] = {"candidate_head": candidate, "verified_head": candidate}
    if pr_refs is not None:
        fields["pr_refs"] = pr_refs
    _force_run_fields(sc.registry, sc.run_id, **fields)
    return sc


def _blocking_findings_before_delivery(root: Path, monkeypatch) -> Scenario:
    return _blocking_findings_run(root, pr_refs=())


def _blocking_findings_with_open_pr(root: Path, monkeypatch) -> Scenario:
    # review fixture 依 mapped PR 帶 pr_refs：PR 已開、尚未 merge。
    return _blocking_findings_run(root, pr_refs=None)


def _planning_failure_before_delivery(root: Path, monkeypatch) -> Scenario:
    # production 的 start（work_bridge）建立 run 時 pr_refs 為空；fallback starter
    # 依 mapped PR 預填，這裡還原成 production 形狀。
    sc = SCENARIOS["recover-planning"](root)
    _force_run_fields(sc.registry, sc.run_id, pr_refs=())
    return sc


def _delivered_merged_journal(root: Path, monkeypatch) -> Scenario:
    lane = delivered_fixture._ship_lane(root, monkeypatch)
    delivered_fixture._merge_via_ship_lane(lane)
    delivered_fixture._mark_resume_failed(lane)
    # ship lane 以假 GitHub client 完成 merge；retire 的 terminal proof 改回正式
    # client，經下方 fake runner 讀 PR 終局。
    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHubDeliveryClient)
    return Scenario(
        action="retire-delivered",
        root=root,
        registry=lane.registry,
        state_path=lane.journal,
        args={},
        repo=delivered_fixture.REPO,
        work_id=delivered_fixture.WORK_ID,
        run_id=lane.run_id,
        snapshot=lane.snapshot,
        execute_kwargs={
            "runner": work_fixture._pr_lifecycle_runner(
                {
                    delivered_fixture.PR: {
                        "state": "closed",
                        "merged_at": "2026-09-01T00:00:00Z",
                    }
                }
            )
        },
    )


def _open_pr_awaiting_copilot(root: Path, monkeypatch) -> Scenario:
    """production ship lane 開了 PR、等 Copilot review 時 resume 失敗（#1141 對照組）。"""

    lane = delivered_fixture._ship_lane(root, monkeypatch, copilot_reviewed=False)
    delivered_fixture._open_pr_snapshot(lane.snapshot)
    assert lane.ship(now=2000.0)["action"] == "awaiting-copilot"
    delivered_fixture._mark_resume_failed(lane)
    return Scenario(
        action="abandon",
        root=root,
        registry=lane.registry,
        state_path=lane.journal,
        args={},
        repo=delivered_fixture.REPO,
        work_id=delivered_fixture.WORK_ID,
        run_id=lane.run_id,
        snapshot=lane.snapshot,
    )


def _delivered_without_authority(root: Path, monkeypatch) -> Scenario:
    sc = _retire_delivered_scenario(root)
    payload = json.loads(sc.snapshot.read_text(encoding="utf-8"))
    payload["work_items"] = []
    sc.snapshot.write_text(json.dumps(payload), encoding="utf-8")
    fake_runner = sc.execute_kwargs["runner"]
    real_status = work_actions._retire_delivered_pr_terminal_status
    # 投影端在 production 以同一個 GitHub terminal proof 判定；測試以同一個 fake
    # runner 供給，兩端讀同一份 PR 終局事實。
    monkeypatch.setattr(
        work_actions,
        "_retire_delivered_pr_terminal_status",
        lambda run, *, repo, runner=None: real_status(run, repo=repo, runner=fake_runner),
    )
    return sc


def _scenario_builder(action: str) -> Callable[[Path, Any], Scenario]:
    return lambda root, _monkeypatch: SCENARIOS[action](root)


#: 情境 → (builder, 預期 read model 投影的 next_actions)。預期值寫死，讓投影
#: 多出或少了動作都會被看見，而不是只驗「投影出來的都能過」。
ROUND_TRIP_SCENARIOS: dict[str, tuple[Callable[[Path, Any], Scenario], tuple[str, ...]]] = {
    "build-card-terminal-without-evidence": (
        _scenario_builder("retry-card"),
        ("abandon", "regenerate-gates", "retry-card"),
    ),
    "planning-environment-failure": (
        _planning_failure_before_delivery,
        ("recover-planning", "abandon"),
    ),
    "pre-candidate-owner-slice": (
        _scenario_builder("recover-pre-candidate"),
        ("abandon", "recover-pre-candidate"),
    ),
    "blocking-findings-before-delivery": (
        _blocking_findings_before_delivery,
        ("abandon", "retry-review", "retry-build"),
    ),
    # #1170：帶 PR refs 的 run 越過 abandon 的 pre-delivery 閘門，只投影可受理的
    # retry lane；pre-delivery 對照組（上一列）仍保留 abandon。
    "blocking-findings-with-open-pr": (
        _blocking_findings_with_open_pr,
        ("retry-review", "retry-build"),
    ),
    "delivered-merged-journal": (_delivered_merged_journal, ("retire-delivered",)),
    # open PR 的 recovery exit 由 action admission 決定，不能退回 pre-delivery abandon。
    "open-pr-awaiting-copilot": (_open_pr_awaiting_copilot, ("resume",)),
    "delivered-without-authority": (_delivered_without_authority, ("retire-delivered",)),
}

#: 投影曾提供但正式入口拒絕的缺口已由 issue-1170 修復。
ROUND_TRIP_GAPS: dict[tuple[str, str], str] = {}


def _prepare_round_trip(
    scenario_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Scenario:
    root = tmp_path / "scenario"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("PSC_GATE_CMD_PYTEST", "python3 -c pass")
    sc = ROUND_TRIP_SCENARIOS[scenario_id][0](root, monkeypatch)
    for name, value in sc.env.items():
        monkeypatch.setenv(name, value)
    for target, name, value in sc.patches:
        monkeypatch.setattr(target, name, value)
    _install_authority_seam(sc, monkeypatch)
    return sc


def _round_trip_cases() -> list[Any]:
    cases: list[Any] = []
    for scenario_id, (_builder, actions) in ROUND_TRIP_SCENARIOS.items():
        for action in actions:
            gap = ROUND_TRIP_GAPS.get((scenario_id, action))
            marks = (
                [
                    pytest.mark.xfail(
                        strict=True,
                        raises=(RuntimeError, ValueError),
                        reason=f"known gap {gap}: 投影提供但正式入口拒絕",
                    )
                ]
                if gap
                else []
            )
            cases.append(
                pytest.param(scenario_id, action, id=f"{scenario_id}-{action}", marks=marks)
            )
    return cases


def _projection_emittable_recovery_actions() -> set[str]:
    """投影函式原始碼中出現、且屬於註冊 recovery action 的字串常數。"""

    sources = (
        work_actions._phase_recovery_actions,
        work_actions._blocking_findings_recovery_actions,
        work_actions.recovery_actions_without_work_authority,
        claim_module.needs_human_next_actions,
    )
    found: set[str] = set()
    for function in sources:
        tree = ast.parse(inspect.cleandoc("\n" + inspect.getsource(function)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in contracts.RECOVERY_WORK_ACTIONS:
                    found.add(node.value)
    return found


def test_r09_round_trip_scenarios_cover_every_projectable_recovery_action() -> None:
    projectable = _projection_emittable_recovery_actions()
    exercised = {
        action for _builder, actions in ROUND_TRIP_SCENARIOS.values() for action in actions
    }
    assert projectable, "投影函式應至少宣告 abandon"
    assert projectable <= exercised, sorted(projectable - exercised)
    assert set(ROUND_TRIP_GAPS.values()) <= {
        gap["id"]
        for gap in json.loads(GAP_LEDGER_PATH.read_text(encoding="utf-8"))["gaps"]
        if gap["status"] != "closed"
    }


@pytest.mark.parametrize(("scenario_id", "action"), _round_trip_cases())
def test_r09_every_projected_next_action_is_accepted_by_the_formal_entry(
    scenario_id: str, action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sc = _prepare_round_trip(scenario_id, tmp_path, monkeypatch)
    entry = _attention(sc, monkeypatch)[sc.run_id]
    assert tuple(entry["next_actions"]) == ROUND_TRIP_SCENARIOS[scenario_id][1]

    request = _request_from_read_model(entry, action)
    result = _submit(sc, request)

    assert result["result"]
    assert result.get("work_id") == entry["work_id"]
