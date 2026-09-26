"""issue #414：plan 卡被 deterministic pass 時不驗證宣告 outputs，導致下一棒
build 的 declared input 必缺。

根因（生產實測，run workflow-e18785ac）：``assess_planning_completeness`` 只看
kind 覆蓋率——workstream todo（kind=plan、accepted）就足以讓 planning 判定
complete，manager 於是把 plan 卡（如 ``writing-plans-light``）deterministic
pass，卻從未檢查卡片自己宣告的 ``produces`` glob（如
``docs/superpowers/plans/*<task-slug>*.md``）是否真的命中檔案；todo 的 ref
通常不落在該 pattern 內。下一棒 build 卡宣告同一 pattern 為 declared
input，``_workflow_input_snapshot`` 找不到檔案便 raise
``ValueError: workflow declared input missing: ...``，run 卡死在 needs_human。

掛載點：``manager._dispatch_workflow_card`` 的 planner/plan phase 完成、
deterministic pass 之前（見 `_plan_card_declared_outputs_present` /
`_materialize_plan_card_output`）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo

TASK = "fix log error dedup v3"
TASK_SLUG = "fix-log-error-dedup-v3"  # slugify_task(TASK) 的結果，比照生產事故 run 的 task slug
CANONICAL_PLAN_REF = f"docs/superpowers/plans/{TASK_SLUG}.md"
# 生產事故形狀：workstream todo 以 kind=plan 頂替，其 ref 不落在
# writing-plans-light 宣告的 canonical outputs glob 內。
TODO_REF = f"docs/superpowers/plans/workstream/{TASK_SLUG}/todo.md"


def _manifest():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "small-fix.yaml", cards)
    result = compile_combo(combo, cards, TASK)
    assert result.workflow_manifest is not None
    return result.workflow_manifest


def _write(root: Path, ref: str, body: str) -> PlanningArtifactAuthority:
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return PlanningArtifactAuthority(ref=ref, kind="plan", work_id=TASK_SLUG, baseline_sha256=digest)


_SPEC_BODY = "---\nstatus: accepted\n---\n# Spec\n## Requirements\nFixed.\n"
_DESIGN_BODY = "---\nstatus: accepted\n---\n# Design\n## Decisions\nFixed.\n"
_TODO_BODY = (
    "---\nstatus: accepted\n---\n# Workstream todo\n## Tasks\n- [ ] do the fix\n"
)


def _write_spec_and_design(root: Path) -> tuple[PlanningArtifactAuthority, ...]:
    spec_ref = f"docs/superpowers/specs/{TASK_SLUG}-spec.md"
    design_ref = f"docs/superpowers/specs/{TASK_SLUG}-design.md"
    spec_path = root / spec_ref
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(_SPEC_BODY, encoding="utf-8")
    design_path = root / design_ref
    design_path.write_text(_DESIGN_BODY, encoding="utf-8")
    return (
        PlanningArtifactAuthority(
            ref=spec_ref,
            kind="spec",
            work_id=TASK_SLUG,
            baseline_sha256=hashlib.sha256(_SPEC_BODY.encode("utf-8")).hexdigest(),
        ),
        PlanningArtifactAuthority(
            ref=design_ref,
            kind="design",
            work_id=TASK_SLUG,
            baseline_sha256=hashlib.sha256(_DESIGN_BODY.encode("utf-8")).hexdigest(),
        ),
    )


def _dispatcher(registry: JobRegistry):
    return type("D", (), {"_registry": registry, "_git_runner": None})()


def _must_not_launch(_identity):
    raise AssertionError("plan phase 一律走 deterministic pass 或 materialize fallback，不得啟動 launcher")


def _make_run(tmp_path: Path, *, plan_ref: str, plan_body: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    spec_authority, design_authority = _write_spec_and_design(repo)
    plan_authority = _write(repo, plan_ref, plan_body)
    run = registry._manager_create_workflow_run(
        work_id=TASK_SLUG,
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="small-fix",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#414",),
        openspec_refs=(),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
        planning_authority=(spec_authority, design_authority, plan_authority),
    )
    return registry, repo, run


def test_todo_anchored_plan_outputs_missing_get_materialized_not_skipped(tmp_path: Path) -> None:
    """RED（修復前）：todo 的 ref 不落在 writing-plans-light 宣告的
    ``docs/superpowers/plans/*<task-slug>*.md`` 內，舊行為直接 deterministic
    pass、從不檢查——下一棒 build 派工時 declared input 必缺（見本檔案下方
    `test_build_dispatch_after_materialize_finds_declared_input`，重現生產
    event 的 ValueError）。GREEN（修復後）：deterministic pass 前偵測到
    outputs 缺席，materialize 一份 canonical plan 檔案，讓宣告的 outputs
    真的存在。
    """
    registry, repo, run = _make_run(tmp_path, plan_ref=TODO_REF, plan_body=_TODO_BODY)

    canonical_path = repo / CANONICAL_PLAN_REF
    assert not canonical_path.exists()  # 修復前後皆成立：materialize 前，canonical 路徑本來就沒有檔案

    result = manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=run,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_must_not_launch,
        coordinator_root=tmp_path / "coordinator",
    )

    persisted = registry.get_workflow_run(run.run_id)

    # 核心斷言：deterministic pass 不得在 outputs 缺席時直接放行——修復後必須
    # 是「materialize 後放行」，canonical 路徑真的有檔案。
    assert canonical_path.is_file()
    assert canonical_path.read_text(encoding="utf-8") == _TODO_BODY

    assert result is None
    assert persisted.current_phase == "build"
    planner = next(step for step in persisted.steps if step.card == "writing-plans-light")
    assert planner.gate_result == "passed"
    assert (planner.executor, planner.model, planner.domain) == (
        "cortex-manager",
        "deterministic",
        "cortex",
    )
    # materialize 出的 plan artifact 必須被記進 planning_authority——build 端
    # `_workflow_input_snapshot` 的 authority fallback 靠這筆記錄把內容 seed
    # 進獨立的 build worktree（build worktree 是全新 git worktree，不是
    # workspace_root 本身）。
    materialized = next(
        (item for item in persisted.planning_authority if item.ref == CANONICAL_PLAN_REF), None
    )
    assert materialized is not None
    assert materialized.kind == "plan"
    assert materialized.work_id == TASK_SLUG
    assert materialized.baseline_sha256 == hashlib.sha256(_TODO_BODY.encode("utf-8")).hexdigest()
    assert registry.list_jobs() == []


def test_materialized_plan_publication_journals_before_registry_commit(
    tmp_path: Path, monkeypatch
) -> None:
    registry, _repo, run = _make_run(tmp_path, plan_ref=TODO_REF, plan_body=_TODO_BODY)
    coordinator = tmp_path / "coordinator"
    journal = coordinator / "planning-transactions" / f"{run.run_id}.json"
    original_update = registry._manager_update_workflow_run

    def observe_commit(run_id: str, **kwargs):
        assert journal.is_file(), "missing planning journal before plan registry commit"
        payload = json.loads(journal.read_text(encoding="utf-8"))
        assert payload["phase"] == "prepared"
        assert payload["expected_planning_authority"] == kwargs["planning_authority"][-1].to_dict()
        return original_update(run_id, **kwargs)

    monkeypatch.setattr(registry, "_manager_update_workflow_run", observe_commit)
    manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=run,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_must_not_launch,
        coordinator_root=coordinator,
    )

    assert not journal.exists()


class _HardCrash(BaseException):
    pass


def test_materialized_plan_journal_rolls_back_when_registry_commit_did_not_happen(
    tmp_path: Path, monkeypatch
) -> None:
    registry, repo, run = _make_run(tmp_path, plan_ref=TODO_REF, plan_body=_TODO_BODY)
    coordinator = tmp_path / "coordinator"
    canonical = repo / CANONICAL_PLAN_REF
    original_rollback = manager._PlanningPublicationTransaction.rollback
    monkeypatch.setattr(manager._PlanningPublicationTransaction, "rollback", lambda self, **kwargs: ())

    def crash_before_commit(_run_id: str, **_kwargs):
        raise _HardCrash("before registry commit")

    monkeypatch.setattr(registry, "_manager_update_workflow_run", crash_before_commit)
    with pytest.raises(_HardCrash, match="before registry commit"):
        manager.dispatch_workflow_card(
            _dispatcher(registry),
            run=run,
            identities=IdentityRegistry.from_rows([]),
            launcher_factory=_must_not_launch,
            coordinator_root=coordinator,
        )

    monkeypatch.setattr(manager._PlanningPublicationTransaction, "rollback", original_rollback)
    journal = coordinator / "planning-transactions" / f"{run.run_id}.json"
    assert canonical.is_file()
    assert journal.is_file()
    report = manager.reconcile_planning_transactions(
        registry=registry, coordinator_root=coordinator, now=10**12, grace_seconds=0
    )

    assert report[0]["outcome"] == "rolled-back"
    assert not canonical.exists()
    assert not journal.exists()


def test_materialized_plan_journal_commits_when_registry_update_preceded_crash(
    tmp_path: Path, monkeypatch
) -> None:
    registry, repo, run = _make_run(tmp_path, plan_ref=TODO_REF, plan_body=_TODO_BODY)
    coordinator = tmp_path / "coordinator"
    canonical = repo / CANONICAL_PLAN_REF
    original_update = registry._manager_update_workflow_run
    original_rollback = manager._PlanningPublicationTransaction.rollback
    monkeypatch.setattr(manager._PlanningPublicationTransaction, "rollback", lambda self, **kwargs: ())

    def crash_after_commit(run_id: str, **kwargs):
        original_update(run_id, **kwargs)
        raise _HardCrash("after registry commit")

    monkeypatch.setattr(registry, "_manager_update_workflow_run", crash_after_commit)
    with pytest.raises(_HardCrash, match="after registry commit"):
        manager.dispatch_workflow_card(
            _dispatcher(registry),
            run=run,
            identities=IdentityRegistry.from_rows([]),
            launcher_factory=_must_not_launch,
            coordinator_root=coordinator,
        )

    monkeypatch.setattr(manager._PlanningPublicationTransaction, "rollback", original_rollback)
    journal = coordinator / "planning-transactions" / f"{run.run_id}.json"
    assert canonical.is_file()
    assert journal.is_file()
    report = manager.reconcile_planning_transactions(
        registry=registry, coordinator_root=coordinator, now=10**12, grace_seconds=0
    )

    assert report[0]["outcome"] == "committed"
    assert canonical.read_text(encoding="utf-8") == _TODO_BODY
    assert not journal.exists()


def test_build_dispatch_after_materialize_finds_declared_input(tmp_path: Path) -> None:
    """端到端重現 + 修復驗證：materialize 之後，下一棒 subagent-build 派工的
    declared input 檢查（``_workflow_input_snapshot``）必須找得到
    ``docs/superpowers/plans/*<task-slug>*.md``——這正是生產事故（run
    workflow-e18785ac）拋出 ``ValueError: workflow declared input missing``
    的位置。舊行為（deterministic pass 不驗證 outputs）會讓這裡直接炸掉。
    """
    registry, repo, run = _make_run(tmp_path, plan_ref=TODO_REF, plan_body=_TODO_BODY)

    # 先跑 plan phase 的 deterministic pass（含 materialize）。
    manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=run,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_must_not_launch,
        coordinator_root=tmp_path / "coordinator",
    )
    run = registry.get_workflow_run(run.run_id)
    assert run.current_phase == "build"

    # build worktree 是獨立 git worktree（見 seams.ScriptWorktreeCreator），
    # 這裡不需要真的建；直接呼叫 `_workflow_input_snapshot`（build 端 declared
    # input 檢查本體，issue #414 描述的 ValueError 就是它拋出的）驗證 pattern
    # 真的能透過 authority fallback 找到 materialize 出的內容。
    step = next(item for item in run.steps if item.card == "subagent-build")
    fresh_worktree = tmp_path / "fresh-build-worktree"
    fresh_worktree.mkdir()
    # 修復前：todo 的 ref 不在 planning_authority 對得上 pattern 的集合內
    # （deterministic pass 從未 materialize），這裡會直接
    # raise ValueError("workflow declared input missing: ...")——正是
    # issue #414 描述的生產事故症狀。修復後：materialize 出的 authority
    # 項讓下面這行順利跑完，不炸。
    snapshot = manager._workflow_input_snapshot(
        run=run,
        repo_root=fresh_worktree,
        patterns=step.inputs,
        coordinator_root=tmp_path / "coordinator",
    )
    assert any(entry["path"] == CANONICAL_PLAN_REF for entry in snapshot)
    # seed 出來的內容必須就是新落地的 canonical plan 檔案，且真的寫進了這個
    # 全新的 worktree（不是 workspace_root 本身）。
    assert (fresh_worktree / CANONICAL_PLAN_REF).read_text(encoding="utf-8") == _TODO_BODY


def test_plan_outputs_already_present_pass_unchanged(tmp_path: Path) -> None:
    """既有正路不受影響：outputs 已經存在時（plan 的 ref 本來就落在宣告的
    canonical glob 內），deterministic pass 行為與修復前完全一致——不觸發
    materialize，不多寫檔案，不多一筆 planning_authority。
    """
    registry, repo, run = _make_run(tmp_path, plan_ref=CANONICAL_PLAN_REF, plan_body=_TODO_BODY)

    before_mtime = (repo / CANONICAL_PLAN_REF).stat().st_mtime_ns

    result = manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=run,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_must_not_launch,
        coordinator_root=tmp_path / "coordinator",
    )

    persisted = registry.get_workflow_run(run.run_id)
    assert result is None
    assert persisted.current_phase == "build"
    assert len(persisted.planning_authority) == 3  # spec + design + plan，沒有多出 materialize 的第 4 筆
    assert (repo / CANONICAL_PLAN_REF).stat().st_mtime_ns == before_mtime  # 沒被重寫
    planner = next(step for step in persisted.steps if step.card == "writing-plans-light")
    assert planner.outputs == tuple(
        item.ref for item in (
            next(a for a in persisted.planning_authority if a.kind == "spec"),
            next(a for a in persisted.planning_authority if a.kind == "design"),
            next(a for a in persisted.planning_authority if a.kind == "plan"),
        )
    )
