"""#208 收口 wiring 3：repair／re-claim 後 band 重算。

落點：``work_actions`` 的 ``_retry_build_action``／``_retry_verify_action``／
``_retry_review_action`` 成功路徑，重算條件與輸入同 wiring 1（共用
``work_bridge.current_sizing_snapshot``）。

驗收條件對應：
1. 三條 retry 路徑成功時，算得出來就把新算出的 sizing_score/sizing_band 寫回
   run（「確實發生」：舊值與重算值不同，證明真的重跑了，不是沿用舊值）。
2. 算不出來（fail-soft，例如 workspace_root 上已經沒有 plan 檔案）時維持現值
   不動，不得讓既有測試變紅。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority, WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason

HEAD = "b" * 40

_PERSONA_BY_PHASE = {
    "claim": "manager",
    "define": "planner",
    "plan": "planner",
    "build": "builder",
    "verify": "reviewer",
    "review": "reviewer",
    "ship": "manager",
}


def _step(phase: str, card: str, *, gate_result: str = "pending") -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona=_PERSONA_BY_PHASE[phase],
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        gate_result=gate_result,
    )


def _base_steps(*, verify_result: str, review_result: str) -> tuple[WorkflowStep, ...]:
    return (
        _step("claim", "manager-claim", gate_result="passed"),
        _step("define", "planner-define", gate_result="passed"),
        _step("plan", "planner-plan", gate_result="passed"),
        _step("build", "subagent-build", gate_result="passed"),
        _step("verify", "reviewer-verify", gate_result=verify_result),
        _step("review", "reviewer-review", gate_result=review_result),
        _step("ship", "manager-ship", gate_result="pending"),
    )


def _init_repo(root: Path, repo: str = "acme/demo") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", f"git@github.com:{repo}.git"],
        check=True,
    )
    return root


def _write_planning_docs(root: Path, *, declare_sizing_dimensions: bool) -> tuple[PlanningArtifactAuthority, ...]:
    base = root / "openspec/changes/demo"
    base.mkdir(parents=True, exist_ok=True)
    bodies = {
        "spec": ("proposal.md", "---\nstatus: accepted\n---\n# Spec\n## Requirements\nReady.\n"),
        "design": ("design.md", "---\nstatus: accepted\n---\n# Design\n## Decisions\nReady.\n"),
    }
    plan_frontmatter = "status: accepted\n"
    if declare_sizing_dimensions:
        plan_frontmatter += "domain_breadth: 1\nstate_consistency: 1\n"
    bodies["plan"] = ("tasks.md", f"---\n{plan_frontmatter}---\n# Tasks\n## Task 1\nBuild.\n")

    authority: list[PlanningArtifactAuthority] = []
    for kind, (filename, body) in bodies.items():
        ref = f"openspec/changes/demo/{filename}"
        (root / ref).write_text(body, encoding="utf-8")
        import hashlib

        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        authority.append(
            PlanningArtifactAuthority(ref=ref, kind=kind, work_id="demo", baseline_sha256=digest)
        )
    return tuple(authority)


def _snapshot(path: Path) -> Path:
    path.write_text(
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
                        "mapped_issues": [12],
                        "mapped_prs": [8],
                        "mapped_openspec": ["demo"],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": ["issue:12@open", "openspec:demo@1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _authority(tmp_path: Path):
    snapshot = _snapshot(tmp_path / "snapshot.json")
    return (
        work_actions.load_work_authority(repo="acme/demo", work_id="demo", snapshot_path=snapshot),
        snapshot,
    )


def _make_run(
    registry: JobRegistry,
    *,
    authority,
    claim_key: str,
    current_phase: str,
    steps: tuple[WorkflowStep, ...],
    workspace_root: Path,
    candidate_head: str | None = None,
    verified_head: str | None = None,
    facets: tuple[str, ...] = (),
    planning_authority: tuple[PlanningArtifactAuthority, ...] = (),
    sizing_score: int | None = None,
    sizing_band: str | None = None,
):
    return registry._manager_create_workflow_run(
        work_id=authority.work_id,
        repo=authority.repo,
        claim_key=claim_key,
        source_revision=work_actions.work_authority_digest(authority),
        workspace_root=str(workspace_root),
        combo="feature-oneshot",
        current_phase=current_phase,
        steps=steps,
        issue_refs=tuple(f"{authority.repo}#{n}" for n in authority.mapped_issues),
        openspec_refs=authority.mapped_openspec,
        candidate_head=candidate_head,
        verified_head=verified_head,
        facets=facets,
        needs_human_reason=(
            fixture_needs_human_reason() if "needs_human" in facets else None
        ),
        gate_status="running",
        planning_authority=planning_authority,
        sizing_score=sizing_score,
        sizing_band=sizing_band,
    )


# ---------------------------------------------------------------------------
# AC1：retry-build 成功路徑重算 sizing（stale green → 重算後的實際值）
# ---------------------------------------------------------------------------


def test_retry_build_recomputes_sizing_on_success(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    repo = _init_repo(tmp_path / "repo")
    planning_authority = _write_planning_docs(repo, declare_sizing_dimensions=True)
    steps = _base_steps(verify_result="passed", review_result="passed")
    _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="review",
        steps=steps,
        workspace_root=repo,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=("needs_human",),
        planning_authority=planning_authority,
        sizing_score=2,
        sizing_band="green",
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

    updated = result["result"]["run"]
    # feature-oneshot 真實 combo：gate_spine=4、cards=11、
    # persona_binding_count=11 → acceptance_surfaces=2/spec_stability=2/
    # orchestration=2；加上宣告的 domain_breadth=1/state_consistency=1 → 8。
    assert updated["sizing_score"] == 8
    assert updated["sizing_band"] == "red"
    persisted = registry.get_workflow_run(updated["run_id"])
    assert persisted.sizing_score == 8
    assert persisted.sizing_band == "red"


def test_retry_build_fails_soft_and_leaves_stale_sizing_when_recompute_unavailable(
    tmp_path: Path,
) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    repo = _init_repo(tmp_path / "repo")
    # 舊 plan：沒有宣告 domain_breadth/state_consistency（#221 之前的 plan）。
    planning_authority = _write_planning_docs(repo, declare_sizing_dimensions=False)
    steps = _base_steps(verify_result="passed", review_result="passed")
    _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="review",
        steps=steps,
        workspace_root=repo,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=("needs_human",),
        planning_authority=planning_authority,
        sizing_score=2,
        sizing_band="green",
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

    updated = result["result"]["run"]
    assert updated["sizing_score"] == 2
    assert updated["sizing_band"] == "green"


# ---------------------------------------------------------------------------
# AC1（cont'd）：retry-verify 成功路徑重算 sizing
# ---------------------------------------------------------------------------


def test_retry_verify_recomputes_sizing_on_success(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    repo = _init_repo(tmp_path / "repo")
    planning_authority = _write_planning_docs(repo, declare_sizing_dimensions=True)
    steps = _base_steps(verify_result="needs_human", review_result="pending")
    _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="verify",
        steps=steps,
        workspace_root=repo,
        candidate_head=HEAD,
        facets=("needs_human",),
        planning_authority=planning_authority,
        sizing_score=2,
        sizing_band="green",
    )

    result = work_actions.execute_work_action(
        args={
            "action": "retry-verify",
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

    updated = result["result"]["run"]
    assert updated["sizing_score"] == 8
    assert updated["sizing_band"] == "red"


# ---------------------------------------------------------------------------
# AC1（cont'd）：retry-review 成功路徑重算 sizing
# ---------------------------------------------------------------------------


def test_retry_review_recomputes_sizing_on_success(tmp_path: Path) -> None:
    authority, snapshot = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    claim_key = work_actions._expected_claim_key(authority)
    repo = _init_repo(tmp_path / "repo")
    planning_authority = _write_planning_docs(repo, declare_sizing_dimensions=True)
    steps = _base_steps(verify_result="passed", review_result="needs_human")
    _make_run(
        registry,
        authority=authority,
        claim_key=claim_key,
        current_phase="review",
        steps=steps,
        workspace_root=repo,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=("needs_human",),
        planning_authority=planning_authority,
        sizing_score=2,
        sizing_band="green",
    )

    result = work_actions.execute_work_action(
        args={
            "action": "retry-review",
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

    updated = result["result"]["run"]
    assert updated["sizing_score"] == 8
    assert updated["sizing_band"] == "red"
