"""#310：pinned planning input 的 drift 檢查對 task checkbox 更新的容忍。

卡片契約要求 builder 勾選 openspec change tasks.md 的 checkbox；嚴格 raw-hash
drift 比對使 verify 派工必然 fail-closed。修法：operator_root 的同 ref 檔案
（已驗證等於 baseline）作為 baseline bytes，raw-hash 不符時對 authority
kind=plan 且 basename 為 tasks.md／todo.md 的 ref 做 checkbox-insensitive
比對；其餘差異維持 fail-closed。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority

REF = "openspec/changes/2026-08-04-demo/tasks.md"
BASELINE = """---
status: accepted
work_item: demo
---

# Tasks

- [ ] 1.1 RED：新增測試。
- [ ] 1.2 GREEN：實作。
"""


def _setup(tmp_path: Path, worktree_content: str):
    operator_root = tmp_path / "operator"
    worktree = tmp_path / "worktree"
    for root, content in ((operator_root, BASELINE), (worktree, worktree_content)):
        target = root / REF
        target.parent.mkdir(parents=True)
        target.write_text(content, encoding="utf-8")
    run = SimpleNamespace(
        run_id="workflow-" + "a" * 20,
        work_id="demo",
        repo="hamanpaul/paulsha-cortex",
        source_revision="rev-demo",
        workspace_root=str(operator_root),
        planning_authority=(
            PlanningArtifactAuthority(
                ref=REF,
                kind="plan",
                work_id="demo",
                baseline_sha256=hashlib.sha256(BASELINE.encode()).hexdigest(),
            ),
        ),
    )
    return operator_root, worktree, run


def test_checkbox_only_drift_tolerated(tmp_path: Path) -> None:
    ticked = BASELINE.replace("- [ ] 1.1", "- [x] 1.1").replace("- [ ] 1.2", "- [X] 1.2")
    _, worktree, run = _setup(tmp_path, ticked)
    rows = manager._workflow_input_snapshot(
        run=run,
        repo_root=worktree,
        patterns=(REF,),
        coordinator_root=tmp_path / "coord",
    )
    assert rows[0]["path"] == REF
    assert rows[0]["authority"] == "planning-authority"


def test_substantive_drift_still_fail_closed(tmp_path: Path) -> None:
    mutated = BASELINE.replace("新增測試。", "改掉任務語意。")
    _, worktree, run = _setup(tmp_path, mutated)
    with pytest.raises(ValueError, match="planning input drift"):
        manager._workflow_input_snapshot(
            run=run,
            repo_root=worktree,
            patterns=(REF,),
            coordinator_root=tmp_path / "coord",
        )


def test_checkbox_drift_plus_substantive_change_fail_closed(tmp_path: Path) -> None:
    mutated = BASELINE.replace("- [ ] 1.1", "- [x] 1.1").replace("實作。", "偷改範圍。")
    _, worktree, run = _setup(tmp_path, mutated)
    with pytest.raises(ValueError, match="planning input drift"):
        manager._workflow_input_snapshot(
            run=run,
            repo_root=worktree,
            patterns=(REF,),
            coordinator_root=tmp_path / "coord",
        )


def test_non_plan_kind_checkbox_drift_fail_closed(tmp_path: Path) -> None:
    ticked = BASELINE.replace("- [ ] 1.1", "- [x] 1.1")
    operator_root, worktree, run = _setup(tmp_path, ticked)
    run.planning_authority = (
        PlanningArtifactAuthority(
            ref=REF,
            kind="spec",
            work_id="demo",
            baseline_sha256=hashlib.sha256(BASELINE.encode()).hexdigest(),
        ),
    )
    with pytest.raises(ValueError, match="planning input drift"):
        manager._workflow_input_snapshot(
            run=run,
            repo_root=worktree,
            patterns=(REF,),
            coordinator_root=tmp_path / "coord",
        )


def test_reviewer_authority_map_uses_candidate_hash_for_ticked_tasks(tmp_path: Path) -> None:
    ticked = BASELINE.replace("- [ ] 1.1", "- [x] 1.1")
    operator_root, worktree, run = _setup(tmp_path, ticked)
    mapping = manager._authority_map_with_checkbox_tolerance(run, candidate_root=worktree)
    assert mapping[REF] == hashlib.sha256(ticked.encode()).hexdigest()


def test_reviewer_authority_map_keeps_baseline_on_substantive_change(tmp_path: Path) -> None:
    mutated = BASELINE.replace("實作。", "偷改。")
    operator_root, worktree, run = _setup(tmp_path, mutated)
    mapping = manager._authority_map_with_checkbox_tolerance(run, candidate_root=worktree)
    assert mapping[REF] == run.planning_authority[0].baseline_sha256
