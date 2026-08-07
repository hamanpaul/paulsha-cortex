---
status: accepted
work_item: feat-task-type-combo-selector
---

# Tasks

- [x] 1.1 RED：依 `docs/superpowers/plans/feat-task-type-combo-selector.md` 的 TDD RED 章節新增 `tests/test_combo_selector.py` 與 `tests/test_combo_selector_wiring.py`，確認全部失敗（前置：#139 的 `paulsha_cortex/deck/task_types.py` 已落地）。
- [x] 1.2 實作至 GREEN，範圍限於 `docs/superpowers/specs/feat-task-type-combo-selector-spec.md` 的 Requirements（R1-R6）；不繞過 deck compile policy gate 與 `validate_manager_spine`，不自建 taxonomy 值域。
- [x] 1.3 `changelog.d/feat-task-type-combo-selector.md` fragment 與 `CHANGELOG.md [Unreleased]` entry（#202）；`docs/unified-work-lifecycle.md` 與 CLI help（`--combo`／`--combo-selections`）同步。
- [x] 1.4 `python3 -m pytest tests/ -q` 全綠；帶 PR 上下文的 `policy_check` 0 fail；`git diff --check` 乾淨；authoritative delivery preflight（metadata mode）pass。

## 驗收

`fix` 標題 work item 穩定選到 `fix-standard`、`feat` 選到 `feature-oneshot` 且結果決定性；明示 `--combo` override 永遠優先並記 `explicit-override` 來源、未知 combo fail-closed；`unknown_type`／`ambiguous`／matched type 互斥 fail-closed 帶逐 issue 診斷且不建 run；absent／unparseable／combo-null／snapshot-drift 全部 bypass 沿用 `feature-oneshot` 並在 `combo_selection` 留可觀測標記；fix-standard 可載入且 manifest 通過 `validate_manager_spine`；`cortex stat --combo-selections` 可彙總；providers 投影非 degraded。
