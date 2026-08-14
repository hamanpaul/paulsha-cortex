"""issue #524 迴歸測試：planning 成功、已推進到 build 的 in-flight run 不得被
新 claim 作廢；前代 artifact 殘留也不得讓下一代 fail-closed。

生產現場（work_id ``fix-brainstorm-revalidation-diagnostics``／issue #514，
2026-08-14 04:55-05:00）：

* ``workflow-009fe9ab303df196209d`` 04:55:07 claim，``workflow-claim``／
  ``brainstorming``／``openspec-propose``／``writing-plans`` 四張卡全 passed、
  phase 已達 ``build``，卻在 04:56:42 被系統自行 supersede，且無任何
  work-abandon evidence。
* 同一毫秒建立的 ``workflow-952a3652afc51ab4f29c`` 與其後的
  ``workflow-7bb3a83c2c1fc37359d5`` 只完成 ``workflow-claim``，四分鐘內用掉
  全部三次 reclaim 額度（#519 熔斷），無一次失敗肇因於工作項本身。

根因（以三個 persisted claim_key 反算複現，見 PR body）：``claim_key`` 由
``work_authority_digest`` 導出，而該 digest 折入 ``source_revisions``——run 自己
的 planning 卡把 spec/design/plan 寫進 governed roots 後，monitor 會把它們當成
**新的 confirmed source** 併入 authority，於是 digest 改變、``claim_key`` 漂移。
三個世代的 claim_key 恰好對應「無 planning artifact」／「+2 個 spec」／
「+2 個 spec +1 個 plan」三種 source 集合，證明 run 是被自己的成功產出擠掉的。

``_claim_action`` 的 active-run 偵測因此找不到那個 run：第一段用漂移後的
``_expected_claim_key(authority)`` 比對 persisted ``run.claim_key`` 必然落空，
第二段 fallback 又只在 ``automatic`` 或 ``args["action"] == "resume"`` 時才跑，
``start``／``intake`` 這兩個入口整段跳過。canonical_run 於是為 None，claim 路徑
把它當成全新 claim，``_manager_create_workflow_run`` 再無條件把同 (repo, work_id)
的 ongoing run 全部標成 superseded。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, work_actions
from paulsha_cortex.coordinator.claim import load_work_authority
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.work_bridge import _artifact_rows
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority

_REPO = "acme/demo"
_WORK_ID = "fix-inflight-supersede"


def _snapshot(path: Path, *, source_revisions: list[str]) -> Path:
    """以 legacy row 形狀寫一份 durable snapshot。

    ``source_revisions`` 是本測試唯一變動的軸——模擬 run 自己的 planning 卡把
    artifact 寫進 governed roots 之後，monitor 把它們併入 authority 的效果。
    """

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
                        "repo": _REPO,
                        "work_id": _WORK_ID,
                        "mapped_issues": [514],
                        "mapped_prs": [],
                        "mapped_openspec": [],
                        "mapped_todo_paths": [
                            f"docs/superpowers/workstreams/{_WORK_ID}/todo.md"
                        ],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": source_revisions,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


_BASE_REVISIONS = [f"todo:{_REPO}:docs/superpowers/workstreams/{_WORK_ID}/todo.md@identity:todo"]
# run 自己的 brainstorming／writing-plans 卡發佈後，monitor 併入的新 confirmed
# source——這正是讓 claim_key 漂移的那三筆。
_PLANNING_REVISIONS = [
    f"superpowers_spec:{_REPO}:docs/superpowers/specs/{_WORK_ID}-spec.md@identity:spec",
    f"superpowers_spec:{_REPO}:docs/superpowers/specs/{_WORK_ID}-design.md@identity:design",
    f"superpowers_plan:{_REPO}:docs/superpowers/plans/{_WORK_ID}.md@identity:plan",
]


def _claim(*, registry, starter, authority, state_path: Path, action: str):
    """比照生產呼叫端：只有 ``run_auto_claim_scan`` 會帶 ``automatic=True``
    （見 work_actions.run_auto_claim_scan），其餘入口一律 False。"""

    return work_actions._claim_action(
        args={"action": action},
        authority=authority,
        now_epoch=200,
        state_path=state_path,
        automatic=action == "auto-scan",
        workflow_registry=registry,
        workflow_starter=starter,
    )


@pytest.mark.parametrize("action", ["start", "intake", "auto-scan", "resume"])
def test_inflight_run_survives_claim_after_own_planning_artifacts_drift_claim_key(
    tmp_path: Path, action: str
) -> None:
    """#524 (A)：run 自己的 planning 產出讓 claim_key 漂移之後，任何 claim 入口
    都不得把仍在 flight、未失敗的 run 作廢。

    四個 action 一起參數化的理由：``auto-scan``／``resume`` 走的是既有 fallback
    （#216 AC5 已保護，屬防迴歸），``start``／``intake`` 才是生產現場踩到的
    未保護入口。四者行為必須一致，否則同一個 run 會因為呼叫端不同而生死有別。
    """

    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    starter = work_actions._fallback_workflow_starter(registry, state)

    before = load_work_authority(
        repo=_REPO,
        work_id=_WORK_ID,
        snapshot_path=_snapshot(tmp_path / "before.json", source_revisions=_BASE_REVISIONS),
    )
    first = _claim(
        registry=registry, starter=starter, authority=before, state_path=state, action=action
    )
    assert first["action"] == "claim"
    run_id = first["run"]["run_id"]

    # 把 run 推進到 build——即現場那個「四張卡全 passed」的健康 in-flight 狀態。
    for phase in ("plan", "build"):
        registry._manager_update_workflow_run(run_id, current_phase=phase)
    inflight = registry.get_workflow_run(run_id)
    assert inflight.status == "ongoing"
    assert inflight.current_phase == "build"
    assert inflight.facets == ()

    # planning artifact 併入 authority -> digest 改變 -> claim_key 漂移。
    after = load_work_authority(
        repo=_REPO,
        work_id=_WORK_ID,
        snapshot_path=_snapshot(
            tmp_path / "after.json", source_revisions=_BASE_REVISIONS + _PLANNING_REVISIONS
        ),
    )
    assert work_actions._expected_claim_key(after) != inflight.claim_key

    second = _claim(
        registry=registry, starter=starter, authority=after, state_path=state, action=action
    )

    # 不得開新世代，也不得把原 run 作廢。
    runs = registry.list_workflow_runs()
    assert [run.run_id for run in runs] == [run_id], "in-flight run 不得被新 claim 換代"
    survivor = registry.get_workflow_run(run_id)
    assert survivor.status == "ongoing"
    assert survivor.current_phase == "build"
    assert "blocked" not in survivor.facets
    assert second["action"] == "resume"
    assert second["run"]["run_id"] == run_id


def test_failed_run_stays_reclaimable(tmp_path: Path) -> None:
    """#524 (A) 的反向邊界：``needs_human`` 的 run 不算「在 flight 且未失敗」，
    既有的換代出口必須原封不動——保護傘不得寬到把已失敗的 run 也鎖死，否則
    #416／#256 的 abandon→reclaim 復原路徑會一起壞掉。"""

    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    starter = work_actions._fallback_workflow_starter(registry, state)

    before = load_work_authority(
        repo=_REPO,
        work_id=_WORK_ID,
        snapshot_path=_snapshot(tmp_path / "before.json", source_revisions=_BASE_REVISIONS),
    )
    first = _claim(
        registry=registry, starter=starter, authority=before, state_path=state, action="start"
    )
    run_id = first["run"]["run_id"]
    registry._manager_update_workflow_run(run_id, facets=("needs_human",))

    after = load_work_authority(
        repo=_REPO,
        work_id=_WORK_ID,
        snapshot_path=_snapshot(
            tmp_path / "after.json", source_revisions=_BASE_REVISIONS + _PLANNING_REVISIONS
        ),
    )
    second = _claim(
        registry=registry, starter=starter, authority=after, state_path=state, action="start"
    )

    # 已失敗的 run 仍可被換代取代（既有行為），不得被 #524 的保護傘鎖住。
    assert second["action"] == "claim"
    assert registry.get_workflow_run(run_id).status == "superseded"
    assert len(registry.list_workflow_runs()) == 2


def test_design_artifact_authority_kind_matches_publication_taxonomy(tmp_path: Path) -> None:
    """#524 (B)：``docs/superpowers/specs/<slug>-design.md`` 必須以 kind
    ``design`` 承接，下一代才吃得下前代殘留。

    monitor 的 provider 規則把 ``docs/superpowers/specs/**/*.md`` 一律標成
    ``superpowers_spec``（見 monitor/providers.py），``_artifact_rows`` 過去照單
    全收成 kind ``spec``；但 planning 產線的 canonical destination
    （planning_runtime.py 的 ``"design": f"docs/superpowers/specs/{slug}-design.md"``）
    是 kind ``design``。兩邊 taxonomy 不一致，下一代 claim 時 seed 進
    ``run.planning_authority`` 的 design 檔就掛著 kind ``spec``，等
    brainstorming 用 kind ``design`` 重新發佈同一路徑，
    ``_publish_planning_artifacts`` 的 ``owner.kind != row["kind"]`` 立刻
    fail-closed，正是現場 ``workflow-7bb3a83c2c1fc37359d5`` 的
    ``primary-artifact-write-rejected: ... lacks current planning authority:
    ...-design.md``。
    """

    root = tmp_path / "repo"
    spec_ref = f"docs/superpowers/specs/{_WORK_ID}-spec.md"
    design_ref = f"docs/superpowers/specs/{_WORK_ID}-design.md"
    plan_ref = f"docs/superpowers/plans/{_WORK_ID}.md"
    for ref in (spec_ref, design_ref, plan_ref):
        target = root / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# 前代殘留\n", encoding="utf-8")

    authority = load_work_authority(
        repo=_REPO,
        work_id=_WORK_ID,
        snapshot_path=_snapshot(
            tmp_path / "snapshot.json",
            source_revisions=_BASE_REVISIONS + _PLANNING_REVISIONS,
        ),
    )
    rows = {row["ref"]: row["kind"] for row in _artifact_rows(root, authority)}

    assert rows[spec_ref] == "spec"
    assert rows[design_ref] == "design", "design 檔不得以 kind spec 承接"
    assert rows[plan_ref] == "plan"


def test_next_generation_republishes_inherited_design_artifact(tmp_path: Path) -> None:
    """#524 (B) 端到端：以 ``_artifact_rows`` seed 出來的 authority 承接前代殘留
    後，下一代 brainstorming 重新發佈同一組路徑必須成功，不得再撞
    ``planning artifact lacks current planning authority``。"""

    root = tmp_path / "repo"
    design_ref = f"docs/superpowers/specs/{_WORK_ID}-design.md"
    residue = root / design_ref
    residue.parent.mkdir(parents=True, exist_ok=True)
    residue.write_text("# 前代殘留 design\n", encoding="utf-8")

    authority = load_work_authority(
        repo=_REPO,
        work_id=_WORK_ID,
        snapshot_path=_snapshot(
            tmp_path / "snapshot.json",
            source_revisions=_BASE_REVISIONS + _PLANNING_REVISIONS,
        ),
    )
    row = next(row for row in _artifact_rows(root, authority) if row["ref"] == design_ref)
    seeded = (
        PlanningArtifactAuthority(
            ref=design_ref,
            kind=row["kind"],
            work_id=_WORK_ID,
            baseline_sha256=manager._sha256_path(residue),
        ),
    )

    republished = "---\nstatus: accepted\n---\n# Design\n\n## Decisions\n\n新一代重新發佈。\n"
    manager._publish_planning_artifacts(
        str(root),
        [{"kind": "design", "path": design_ref, "content": republished}],
        work_id=_WORK_ID,
        allowed_refs=(design_ref,),
        authorities=seeded,
    )
    assert residue.read_text(encoding="utf-8") == republished
