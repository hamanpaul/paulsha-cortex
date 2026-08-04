---
status: accepted
work_item: feat-task-type-combo-selector
---

## Goals

Manager 建立 workflow 時依 #139 taxonomy 的 normalized `task_type` 自動選出且只選出一個 deck combo（`feat` → `feature-oneshot`、`fix` → 新增的 `fix-standard`），明示 override 永遠優先；ambiguous／unknown_type fail-closed 帶診斷，absent／unparseable 與 combo 缺口走可觀測 bypass 沿用現行預設 combo，選擇 provenance 進 WorkflowRun 與 `cortex stat`（#202）。

## Why

production claim 路徑的 combo 寫死在 `work_bridge.default_workflow_manifest`（無條件 `feature-oneshot`），任務類型從未影響選牌；`fix` 佔實測 issue 最大宗（24/68）卻沒有對應 combo。#139 已凍結 taxonomy 契約與 combo 映射欄位（缺口以 null 明示），issue #202 comment（2026-07-27）裁決 additive with fallback：只有 ambiguous fail-closed，absent／unparseable bypass 且必須可觀測——否則三分之一工作長期不進新機制而無人察覺。另考據發現 comment 附的 fix-standard 草稿（7 卡）缺 define／plan phase 卡，無法通過掛載點強制的 `validate_manager_spine` 全 phase spine，須補回兩張 planner 卡才能落地。

## What Changes

- 新增 `paulsha_cortex/deck/selector.py` 純函式 selector：消費 #139 的 `classify_title` 與 `load_task_types` 映射（不得自建解析）；聚合規則——任一 `unknown_type`／`ambiguous` 或 matched type 互斥 → fail-closed 拋帶逐 issue 診斷的錯誤且不建 run；恰一 matched type → 查映射自動選牌；映射 null／absent／unparseable／snapshot-drift → bypass 沿用 `feature-oneshot`。
- 訊號來源＝durable work snapshot canonical row 的 `github_issue` source `title`（系統事實；canonical hash 與 `authority.snapshot_hash` 交叉比對，不符即 bypass），caller 參數不成為分類輸入。
- 掛載於 `start_canonical_workflow`：`default_workflow_manifest` 以 `combo_name` 參數取代寫死字串（預設值維持現行為）；deck compile policy gate 與 `validate_manager_spine` 不被繞過。
- 新增 `paulsha_cortex/deck/data/combos/fix-standard.yaml`（comment 草稿七卡＋補回 `openspec-propose`／`writing-plans` 滿足全 phase spine；兩 gate 原樣）；`task-types.yaml` 的 `fix` 映射改為 `fix-standard`。
- 明示 override：`cortex work start --combo <id>`／`cortex run work start --combo <id>`，control 契約驗證格式、selector 以 `load_combo` fail-closed 驗證存在；provenance 記 `explicit-override`。
- 可觀測面：WorkflowRun 新增 additive 可選欄位 `combo_selection`（source／task_type／combo／reason），Monitor 投影白名單同步（#205 教訓）；`cortex stat --combo-selections` 依 `source × task_type` 彙總。
- CLI help（R-16）與 `docs/unified-work-lifecycle.md` 同步。

## Capabilities

### Modified Capabilities

- `persona-workflow-orchestration`：詳見 `docs/superpowers/specs/feat-task-type-combo-selector-spec.md` 的 Requirements 與 `docs/superpowers/specs/feat-task-type-combo-selector-design.md` 的 Decisions。
