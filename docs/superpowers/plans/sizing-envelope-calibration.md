---
status: proposed
work_item: sizing-envelope-calibration
---

# sizing-envelope-calibration Plan

issue #210 §4 列出的六項交付，逐項對照 main @ `a2e8d0c` 現況，標記「已完成（不必重做）」或
「待設計（本票 D/R 條目已定案，留待實作票落地）」。完整 Requirements／Decisions 見
`docs/superpowers/specs/sizing-envelope-calibration-{spec,design}.md`；openspec change 見
`openspec/changes/2026-08-07-sizing-envelope-calibration/`。

## 已完成（本票不重做）

| issue §4 交付項 | 落地位置 | 對應 issue |
|---|---|---|
| repair 上限依 band 分級，取代全域常數 | `paulsha_cortex/coordinator/delivery.py:46-68`（`REPAIR_BUDGET_BY_BAND`／`repair_budget_for_band()`） | `#218` |
| plan 驗收條件逐條編號化，`invariant_count` 可機械計數 | `paulsha_cortex/coordinator/planning.py:462-472`（`_plan_review_envelope`，強制宣告 `invariant_count`／`artifact_classes`） | `#212` |
| `sizing_declaration_drift` 資料 schema（宣告-實際落差記錄） | `paulsha_cortex/coordinator/completion.py:49-57,274-283,382-385`（`SIZING_DECLARATION_DRIFT_FIELDS`） | `#222` |

## 待設計（本票已定案裁決，見對應 D/R 條目）

| issue §4 交付項 | 待決議點 | 本票裁決 |
|---|---|---|
| `resource-inventory.yaml` 增設 `calibration_source`／`calibrated_at` | 掛載檔案／掛載欄位範圍 | 併入 `model-identities.yaml`（沿用 `#209` D3），只掛在 `invariant_ceiling` 一個欄位（design D1／spec R1） |
| 難度後驗 estimator（讀 workflow registry + 已合併 PR diff） | 資料源選擇 | `CompletionRecord.work_authority.merge_commit` 本地 `git diff --shortstat`，非 `sizing_declaration_drift`（design D2／spec R2） |
| `invariant_ceiling` estimator（通過率曲線） | 所需歷史資料是否存在 | **發現新缺口**：`invariant_count` 從未持久化，需先補 `CompletionRecord` 新欄位（design D3／spec R3） |
| — | 「一次通過率」操作型定義 | 排除非 `model_repair` 的 `retry_classification`（design D4／spec R4） |
| `consistency_scope` 以 glob 宣告 | 是否升級為路徑 glob | **不採納**，維持 `#209` 已凍結的產物種類集合語意（design D6／spec R7） |
| `cortex stat` 顯示校準來源 | 觸發時機：批次 vs 即時 | 比照既有四個彙總旗標，即時查詢（design D5／spec R5／R8） |

## 交付順序

`#209` 欄位 schema PR 落地 → 本票兩張前置票（`plan_invariant_count` 欄位／
`calibration_source`+`calibrated_at` 欄位，可平行）→ 難度後驗 estimator／
`invariant_ceiling` estimator → `cortex stat --calibration`。完整票序與各自驗收條件見
`openspec/changes/2026-08-07-sizing-envelope-calibration/tasks.md`「建議後續實作票切分」。

## 交付要件

- [x] `docs/superpowers/specs/sizing-envelope-calibration-{spec,design}.md`。
- [x] `openspec/changes/2026-08-07-sizing-envelope-calibration/`。
- [x] 更新 `docs/superpowers/workstreams/cost-governance-cluster/todo.md` 的 `#210` 條目。
- [x] `changelog.d/sizing-envelope-calibration.md` fragment。
- [x] `CHANGELOG.md [Unreleased]` 對應 entry。
- [x] `python3 -m pytest -q` 全綠（docs-only，基線不變）。
- [ ] 帶 PR 上下文執行 `policy_check`，確認 fail: 0（PR 階段執行，非本次 subagent 範圍）。
