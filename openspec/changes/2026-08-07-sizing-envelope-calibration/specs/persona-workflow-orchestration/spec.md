---
status: proposed
work_item: sizing-envelope-calibration
---

## ADDED Requirements

### Requirement: `invariant_ceiling` 的校準來源必須可追溯，其餘封套欄位不得無差別附加校準標記

`invariant_ceiling`（`#209` 定義的供給側靜態欄位）MUST 附帶 `calibration_source`
（`"estimated"` 或 `"measured"`）與 `calibrated_at`（ISO8601 timestamp 或 `null`）兩個
provenance 欄位，掛在同一個 `(executor, model_id)` 複合鍵上。`accepts_bands`／
`consistency_scope`／`acceptance_modes` 三個封套欄位 MUST NOT 附加對應的校準來源標記——
它們是 operator 宣告而非可觀測統計量。

#### Scenario: 樣本不足時保留既有值並標註未校準

- **WHEN** 某 `(executor, model_id)` 身分的已交付 run 樣本數 `<3`
- **THEN** `invariant_ceiling` 保留既有（手估或前次校準）值，`calibration_source` 維持
  `"estimated"`，MUST NOT 輸出 `0` 或任何假造的中位數

#### Scenario: 樣本足夠時可標記為已實測

- **WHEN** 某身分的已交付 run 樣本數 `≥3`，且通過率-vs-`invariant_count` 曲線可算出明確
  衰減點
- **THEN** `invariant_ceiling` 可更新為該衰減點，`calibration_source` 標為 `"measured"`，
  `calibrated_at` 記錄計算時間

### Requirement: 難度後驗與 `invariant_ceiling` 兩個 estimator 必須使用各自正確粒度的資料源

難度後驗 estimator MUST 以 `CompletionRecord.work_authority.merge_commit` 的本地 git diff
行數為資料源，MUST NOT 使用 `sizing_declaration_drift`（模組個數，粒度不符）作為 diff LOC
的替代來源。`invariant_ceiling` estimator MUST 以每次已交付 run 的 `invariant_count`
歷史快照為橫軸，MUST NOT 在該欄位尚未持久化前產出任何非未校準的 `invariant_ceiling` 值。

#### Scenario: 難度後驗讀取正確資料源

- **WHEN** 計算某 topic 的難度尺度
- **THEN** estimator 讀取該 topic 已交付 `CompletionRecord` 的 `merge_commit`，對本地 git
  歷史取 diff 行數中位數，MUST NOT 拿 `declared_modules`／`actual_modules` 個數代入公式

#### Scenario: `invariant_count` 歷史值未就緒時 estimator 拒絕產出校準值

- **WHEN** `CompletionRecord` 尚未攜帶 `invariant_count` 歷史快照欄位
- **THEN** `invariant_ceiling` estimator MUST 回報未校準（`calibration_source: "estimated"`），
  MUST NOT 嘗試以其他資料（如 `sizing_declaration_drift`）替代計算

### Requirement: `consistency_scope` 維持產物種類集合語意，不得升級為路徑 glob

`consistency_scope` MUST 維持 `#209` 已凍結的產物種類集合語意（`code`／`test`／`spec`／
`openspec`／`changelog`／`docs`／`pr`／`issue` 八值子集）。任何把 `consistency_scope`
改為路徑 glob 比對（例如與 builder persona `write_paths` 直接比對路徑模式）的變更 MUST
以獨立於 `consistency_scope` 之外的欄位或機制落地，MUST NOT 修改 `consistency_scope`
本身的型別契約。

#### Scenario: 路徑範圍需求不得覆寫既有欄位契約

- **WHEN** 未來需要「builder persona 只能寫特定路徑範圍」這類 glob 檢查
- **THEN** 該檢查 MUST 落在一個新欄位或既有 `write_paths` 機制上，`consistency_scope`
  仍只回答「這個身分處理哪些產物種類」，兩者不得合併
