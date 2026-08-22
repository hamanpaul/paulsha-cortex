---
status: accepted
work_item: fix-verification-contract-hash-overwrite
---

# Design: fix-verification-contract-hash-overwrite

## Decisions

### D1. 分離兩個權威身分

保留 `slice_row["verification"]["hash"]` 作為 **pinned contract hash**（唯一語意）。
當前驗證證據 hash 移至獨立欄位，命名為 `current_verification_evidence_hash`。
兩者不再共用同一欄位，避免語意重疊。

### D2. 解耦寫入 API

- 移除 `_apply_verification_result` 透過 `update_slice(verification_hash=...)`
  變更 contract hash 的能力；驗證結果套用路徑不得觸及 contract hash 欄位。
- 以語意明確的證據寫入路徑取代（寫 `current_verification_evidence_hash`）。
- contract pin 的寫入權限僅保留於 `create_slice` 與 `repin_slice`。

### D3. 對齊所有讀取端

- `_current_verification_ref`：以 `verification_hash` 取 contract hash。
- completion record：contract hash 取自 `verification_hash`；證據取自
  獨立的 `verification_evidence_hash`。
- handoff manifest：同上，兩個欄位分別輸出，不互相代入。

### D4. 既有損毀列的處理

對先前已被覆寫 `verification.hash` 的 legacy 列，採下列之一並明確落地：
遷移邏輯（偵測並修正／清空受汙染欄位），或明確的 operator 復原指引
（如以 `repin_slice` 重新 pin）。目標是恢復服務且不 crash。

### D5. 邊界驗收測試

- 套用驗證結果前後，contract hash 不變。
- foreign-review 啟動失敗不再引入 `pinned-input-mismatch`。
- status / failure 證據不汙染 contract hash。
- `repin_slice` 仍能有意更新 contract hash。

### D6. 不在本設計範圍

不重寫 pinned-input mismatch 判定邏輯；不承接 terminal replay / dirty-recheck。
