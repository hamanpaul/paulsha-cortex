---
status: accepted
work_item: fix-verification-contract-hash-overwrite
---

# Spec: fix-verification-contract-hash-overwrite

## Requirements

### R1. 缺陷敘述（defect narrative）

驗證結果寫入路徑會覆寫 slice 的 pinned contract hash。`_apply_verification_result`
以 `update_slice(verification_hash=...)` 寫入「當前驗證證據」的 hash，但該值落在
`slice_row["verification"]["hash"]`，而檢查路徑（`_current_verification_ref` 等）
把同一欄位解讀為 **pinned contract hash**。同一欄位同時承載兩種語意，導致契約 pin
被證據值汙染。

### R2. 現況根因證據（current-state evidence）

- 寫入路徑：`_apply_verification_result` → `update_slice(verification_hash=...)`
  → 覆寫 `slice_row["verification"]["hash"]`。
- 檢查路徑：`_current_verification_ref` 讀 `slice_row["verification"]["hash"]`
  並視為 pinned contract hash 做比對。
- 後果：契約 pin 遭覆寫後，後續比對出現偽 `pinned-input-mismatch`；
  完成紀錄（completion record）與 handoff manifest 一併帶出錯誤的 hash。
- 真實系統重現：`add-cortex-version-flag-build` 出現 mismatch，可作為
  authoritative validation evidence。

### R3. 目標狀態

- contract hash 與 evidence hash 為兩個獨立的權威身分，各自有專屬欄位。
- 驗證結果寫入不得以任何路徑變更 contract hash。
- 僅 `create_slice` / `repin_slice` 可有意地設定或更新 contract hash。
- 所有讀取端（`_current_verification_ref`、completion record、handoff manifest）
  從正確欄位取值。

### R4. 範圍界線與明確排除

- **範圍內**：欄位語意分離（field-semantics separation）與相應的寫入／讀取對齊。
- **排除**：不重寫 pinned-input mismatch 的判定邏輯本身。
- **排除**：不承接 terminal replay / dirty-recheck 的 work item 擁有權。

### R5. 相容性（已損毀資料）

對既有已被覆寫的持久化列（`verification.hash` 已遭證據值汙染），必須提供處理方式：
遷移邏輯或明確的 operator 復原指引，使服務不因此 crash 或永久卡在 mismatch。

### R6. 驗證期待（verification expectations）

- 套用驗證結果前後，contract hash 保持不變。
- foreign-review 啟動失敗不再重新引入 `pinned-input-mismatch`。
- status / failure 類證據永不寫入 contract hash 欄位。
- `repin_slice` 仍可有意更新 contract hash。
- 上述皆需有測試覆蓋；並以 `add-cortex-version-flag-build` 重現案例確認修復。
