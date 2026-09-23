"""#208 收口 wiring 2：Yellow 先 plan review 再派（機械部分）＋通過後 freeze 接線。

落點：``manager._dispatch_workflow_card`` 的 plan phase 完成掛載點（#223 已放
Red 攔截；Yellow 分支加在同一位置）＋ ``work_actions._claim_action`` 組
``ClaimCandidate`` 時傳入 ``active_plan_review_passed``。

驗收條件對應：
1. ``run.sizing_band == "yellow"`` 時，推進 build 前呼叫
   ``planning.plan_review_gate()``；gate ready → 放行進 build 且
   ``plan_review_passed`` 寫回 True。
2. gate 不 ready 且 terminal（policy-scope-conflict）→ 設 ``needs_human``，
   不推進。
3. gate 不 ready 且 non-terminal → 不推進，run 現狀原樣保留（可重試）。
4. Green／band None：完全不呼叫 gate，維持現行為（#223 已定案的 fail-soft）。
5. freeze 接線：``_claim_action`` 組出的 ``ClaimCandidate.active_plan_review_passed``
   對 Yellow band 的 run 反映其持久化 ``plan_review_passed``；非 Yellow band
   （或沒有 active run）一律視為已通過（比照 pre-#213 立即凍結行為）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from recovery_registry_receipt_support import recovery_registry_receipt_openspec_refs
from paulsha_cortex.coordinator import manager, work_actions, work_bridge
from paulsha_cortex.coordinator.claim import ClaimCandidate, load_work_authority
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.planning import PlanningArtifact, _frontmatter_and_body
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo


def _manifest():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "yellow plan review", change="yellow-plan-review-208")
    assert result.workflow_manifest is not None
    return result.workflow_manifest


_READY_PLAN_BODY = (
    "---\nstatus: accepted\ninvariant_count: 1\nartifact_classes: [code]\n---\n"
    "# Plan\n## Tasks\n- 實作 code 變動，補 changelog、CLI 說明與 test。\n"
)
_TERMINAL_PLAN_BODY = (
    "---\nstatus: accepted\ninvariant_count: 1\nartifact_classes: [code]\n"
    "scope_excludes: [changelog]\n---\n"
    "# Plan\n## Tasks\n- 實作 code 變動。\n"
)
_NONTERMINAL_PLAN_BODY = (
    "---\nstatus: accepted\ninvariant_count: 1\nartifact_classes: [code]\n---\n"
    "# Plan\n## Tasks\n- 實作 code 變動。\n"
)


def _write_planning_artifacts(root: Path, *, plan_body: str) -> tuple[PlanningArtifactAuthority, ...]:
    bodies = {
        "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nFixed.\n",
        "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nFixed.\n",
        "plan": plan_body,
    }
    authority: list[PlanningArtifactAuthority] = []
    for kind, body in bodies.items():
        ref = f"docs/{kind}.md"
        path = root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        authority.append(
            PlanningArtifactAuthority(
                ref=ref, kind=kind, work_id="yellow-plan-review-208", baseline_sha256=digest
            )
        )
    return tuple(authority)


def _dispatcher(registry: JobRegistry):
    return type("D", (), {"_registry": registry, "_git_runner": None})()


def _must_not_launch(_identity):
    raise AssertionError("yellow band 的 plan review 判定不得啟動任何 launcher")


def _make_run(tmp_path: Path, *, plan_body: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    authority = _write_planning_artifacts(repo, plan_body=plan_body)
    run = registry._manager_create_workflow_run(
        work_id="yellow-plan-review-208",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#208",),
        openspec_refs=("yellow-plan-review-208",),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
        planning_authority=authority,
        sizing_score=5,
        sizing_band="yellow",
    )
    return registry, run


def _dispatch(registry, run):
    return manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=run,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_must_not_launch,
        coordinator_root=Path("/tmp") / "cortex-yellow-plan-review-coordinator",
    )


# ---------------------------------------------------------------------------
# AC1: gate ready → 放行進 build，plan_review_passed 寫回 True
# ---------------------------------------------------------------------------


def test_yellow_band_ready_gate_advances_to_build_and_persists_plan_review_passed(
    tmp_path: Path,
) -> None:
    registry, run = _make_run(tmp_path, plan_body=_READY_PLAN_BODY)

    result = _dispatch(registry, run)

    persisted = registry.get_workflow_run(run.run_id)
    assert result is None
    assert persisted.current_phase == "build"
    assert persisted.plan_review_passed is True
    assert registry.list_jobs() == []


# ---------------------------------------------------------------------------
# AC2: gate 不 ready 且 terminal → needs_human，不推進
# ---------------------------------------------------------------------------


def test_yellow_band_terminal_gate_failure_sets_needs_human_without_advancing(
    tmp_path: Path,
) -> None:
    registry, run = _make_run(tmp_path, plan_body=_TERMINAL_PLAN_BODY)

    result = _dispatch(registry, run)

    persisted = registry.get_workflow_run(run.run_id)
    assert result["current_phase"] == "plan"
    assert result["reason"] == "plan-review-contract_compatibility"
    assert persisted.current_phase == "plan"
    assert persisted.facets == ("needs_human",)
    assert persisted.plan_review_passed is False
    assert registry.list_jobs() == []


# ---------------------------------------------------------------------------
# AC3: gate 不 ready 且 non-terminal → 不推進，run 原樣保留（可重試）
# ---------------------------------------------------------------------------


def test_yellow_band_non_terminal_gate_failure_does_not_advance_and_is_retryable(
    tmp_path: Path,
) -> None:
    registry, run = _make_run(tmp_path, plan_body=_NONTERMINAL_PLAN_BODY)

    result = _dispatch(registry, run)

    persisted = registry.get_workflow_run(run.run_id)
    assert result["current_phase"] == "plan"
    assert result["reason"] == "plan-review-retry-contract_compatibility"
    assert persisted.current_phase == "plan"
    assert persisted.facets == ()
    assert persisted.plan_review_passed is False
    assert persisted.attempts == {"plan": 1}
    assert registry.list_jobs() == []

    # 同一份未修訂的 plan 重複 dispatch 給出相同（可重試）結果，不會意外推進。
    again = _dispatch(registry, registry.get_workflow_run(run.run_id))
    assert again["reason"] == "plan-review-retry-contract_compatibility"
    assert registry.get_workflow_run(run.run_id).current_phase == "plan"


# ---------------------------------------------------------------------------
# AC4: Green／band None 完全不呼叫 gate（regression，鎖住既有 #223 fail-soft）
# ---------------------------------------------------------------------------


def test_green_band_never_calls_plan_review_gate_even_with_a_terminal_plan(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    authority = _write_planning_artifacts(repo, plan_body=_TERMINAL_PLAN_BODY)
    run = registry._manager_create_workflow_run(
        work_id="yellow-plan-review-208",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#208",),
        openspec_refs=("yellow-plan-review-208",),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
        planning_authority=authority,
        sizing_score=2,
        sizing_band="green",
    )

    result = _dispatch(registry, run)

    persisted = registry.get_workflow_run(run.run_id)
    # 若 gate 真的被呼叫，_TERMINAL_PLAN_BODY 的 scope_excludes 衝突會轉
    # needs_human；Green band 必須完全不受影響，正常推進到 build。
    assert result is None
    assert persisted.current_phase == "build"
    assert persisted.facets == ()
    assert persisted.plan_review_passed is False


# ---------------------------------------------------------------------------
# AC5: freeze 接線——_claim_action 組 ClaimCandidate 時的 active_plan_review_passed
# ---------------------------------------------------------------------------


def test_claim_action_freeze_wiring_reflects_persisted_plan_review_passed_for_yellow_band(
    tmp_path: Path,
) -> None:
    seen: list[ClaimCandidate] = []
    original_decide = work_actions.decide_manual_start

    def _spy(candidate, **kwargs):
        seen.append(candidate)
        return original_decide(candidate, **kwargs)

    work_actions.decide_manual_start = _spy  # type: ignore[assignment]
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        registry = JobRegistry(state_path=tmp_path / "registry.json")
        planning_authority = _write_planning_artifacts(repo, plan_body=_READY_PLAN_BODY)
        authority_obj = _snapshot_authority(
            tmp_path,
            repo="acme/demo",
            work_id="yellow-plan-review-208",
            openspec_refs=("yellow-plan-review-208",),
        )
        claim_key = work_actions._expected_claim_key(authority_obj)
        run = registry._manager_create_workflow_run(
            work_id="yellow-plan-review-208",
            repo="acme/demo",
            claim_key=claim_key,
            source_revision=work_actions.work_authority_digest(authority_obj),
            workspace_root=str(repo),
            combo="feature-oneshot",
            current_phase="plan",
            steps=_manifest().steps,
            issue_refs=("acme/demo#14",),
            openspec_refs=("yellow-plan-review-208",),
            pr_refs=(),
            attempts={"plan": 1},
            gate_status="running",
            planning_authority=planning_authority,
            sizing_score=5,
            sizing_band="yellow",
        )

        work_actions._claim_action(
            args={"action": "start", "issue": 14},
            authority=authority_obj,
            now_epoch=200,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
            workflow_starter=lambda *_a: (_ for _ in ()).throw(
                AssertionError("resume 不應該再啟動一次 workflow_starter")
            ),
        )
        assert seen[-1].active_plan_review_passed is False

        registry._manager_update_workflow_run(run.run_id, plan_review_passed=True)
        seen.clear()
        work_actions._claim_action(
            args={"action": "start", "issue": 14},
            authority=authority_obj,
            now_epoch=200,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
            workflow_starter=lambda *_a: (_ for _ in ()).throw(
                AssertionError("resume 不應該再啟動一次 workflow_starter")
            ),
        )
        assert seen[-1].active_plan_review_passed is True
    finally:
        work_actions.decide_manual_start = original_decide


def _snapshot_authority(tmp_path: Path, *, repo: str, work_id: str, openspec_refs: tuple[str, ...]):
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
                        "repo": repo,
                        "work_id": work_id,
                        "mapped_issues": [14],
                        "mapped_prs": [],
                        "mapped_openspec": list(openspec_refs),
                        "mapped_todo_paths": [],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": ["issue:14@open"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return load_work_authority(repo=repo, work_id=work_id, snapshot_path=snapshot)


def test_yellow_band_without_artifact_classes_declaration_fails_soft_and_is_observable(
    tmp_path: Path,
) -> None:
    """fail-soft 分支（verifier finding 3）：Yellow band 但 plan 未宣告
    artifact_classes（既有舊 plan 常態）→ gate 不可跑、維持現行為正常推進；
    可觀測性由 run 狀態導出：yellow + 已進 build + plan_review_passed=False
    即代表 gate 因輸入不可得被跳過（未通過任何審查）。"""
    legacy_plan_body = (
        "---\n"
        "status: accepted\n"
        "domain_breadth: 1\n"
        "state_consistency: 1\n"
        "invariant_count: 1\n"
        "---\n"
        "# plan\n\n## Tasks\n\n- [ ] 舊 plan 沒有 artifact_classes 宣告\n"
    )
    registry, run = _make_run(tmp_path, plan_body=legacy_plan_body)

    result = _dispatch(registry, run)

    persisted = registry.get_workflow_run(run.run_id)
    assert result is None
    assert persisted.current_phase == "build"
    # 觀測標記：yellow band 進了 build 但 plan_review_passed 仍為 False
    # → 消費端可據此辨識「gate 被 fail-soft 跳過」而非「審查通過」。
    assert persisted.sizing_band == "yellow"
    assert persisted.plan_review_passed is False


def test_recovery_registry_receipt_plan_twins_keep_first_and_last_helpers_in_lockstep() -> None:
    root = Path(__file__).resolve().parents[1]
    openspec_refs = recovery_registry_receipt_openspec_refs(root)
    todo_ref = "docs/superpowers/workstreams/recovery-registry-receipt/todo.md"
    tasks_ref = openspec_refs["tasks"]
    todo_text = (root / todo_ref).read_text(encoding="utf-8")
    tasks_text = (root / tasks_ref).read_text(encoding="utf-8")

    def checklist_lines(text: str) -> tuple[str, ...]:
        return tuple(
            line.replace("[x]", "[ ]")
            for line in text.splitlines()
            if line.startswith("- [")
        )

    todo_frontmatter, _, _ = _frontmatter_and_body(todo_text)
    tasks_frontmatter, _, _ = _frontmatter_and_body(tasks_text)
    for field_name in (
        "domain_breadth",
        "state_consistency",
        "invariant_count",
        "artifact_classes",
    ):
        assert todo_frontmatter[field_name] == tasks_frontmatter[field_name]
    assert checklist_lines(todo_text) == checklist_lines(tasks_text)

    todo_artifact = PlanningArtifact(kind="plan", ref=todo_ref, text=todo_text)
    tasks_artifact = PlanningArtifact(kind="plan", ref=tasks_ref, text=tasks_text)
    first_todo = manager._evaluate_yellow_plan_review((todo_artifact, tasks_artifact))
    first_tasks = manager._evaluate_yellow_plan_review((tasks_artifact, todo_artifact))
    assert first_todo is not None
    assert first_tasks is not None
    assert first_todo.ready is True
    assert first_tasks.ready is True
    assert first_todo.failed_check is None
    assert first_tasks.failed_check is None

    rows_with_todo_last = [
        {"kind": "spec", "ref": "docs/superpowers/specs/recovery-registry-receipt-spec.md"},
        {"kind": "design", "ref": "docs/superpowers/specs/recovery-registry-receipt-design.md"},
        {"kind": "spec", "ref": openspec_refs["proposal"]},
        {"kind": "design", "ref": openspec_refs["design"]},
        {"kind": "plan", "ref": tasks_ref},
        {"kind": "plan", "ref": todo_ref},
    ]
    rows_with_tasks_last = [
        {"kind": "spec", "ref": "docs/superpowers/specs/recovery-registry-receipt-spec.md"},
        {"kind": "design", "ref": "docs/superpowers/specs/recovery-registry-receipt-design.md"},
        {"kind": "plan", "ref": todo_ref},
        {"kind": "spec", "ref": openspec_refs["proposal"]},
        {"kind": "design", "ref": openspec_refs["design"]},
        {"kind": "plan", "ref": tasks_ref},
    ]
    assert work_bridge.current_sizing_snapshot(
        workspace_root=root,
        combo_name="feature-oneshot",
        artifact_rows=rows_with_todo_last,
    ) == (6, "yellow")
    assert work_bridge.current_sizing_snapshot(
        workspace_root=root,
        combo_name="feature-oneshot",
        artifact_rows=rows_with_tasks_last,
    ) == (6, "yellow")
