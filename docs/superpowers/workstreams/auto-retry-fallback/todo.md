---
status: accepted
work_item: auto-retry-fallback
---

# gate／review 失敗自動重試並自動換 builder，不停在 needs_human

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1306`。
- owner 原則：cortex 是高可信自主開發工具，可以自動恢復的失敗不要停下來等人。
- 現況：`GateContradictionError`（builder 自稱 passed、gate 實際失敗）、review 不通過、builder 反覆失敗，都會讓 run 停在 needs_human，要 operator 手動 `retry-build`／`rechain`。`rechain` 會清掉 needs_human，但 `retry-build`／`retry-card` 又要求 needs_human；review 階段在 `rechain` 後下 `resume`，會在同一個 candidate 上重派 review 卡。
- 不放寬任何 gate、review 或 independence 規則；自動化只取代「人工按下重試」這一步。

## Tasks

- [x] **T1 RED**：測試 gate 矛盾與 review 不通過時，現行會轉 needs_human；rechain 之後 review 階段的 resume 會重派舊 candidate 的審查。
- [x] **T2 自動重試**：gate 矛盾與 review 不通過時，Manager 自動以 exact candidate 執行 retry-build，把失敗的測試輸出摘要或 review findings 帶進下一次 build 的輸入。每個 run 有可設定的重試上限，計數與原因持久化。
- [x] **T3 自動換 builder**：同一 builder 在同一張卡連續失敗達上限，或命中已知的 executor 環境錯誤（例如權限被拒、sandbox panic），自動改派身分矩陣中下一個合格且有 build 能力的身分，維持 reviewer 獨立性，並留下稽核紀錄。
- [x] **T4 用盡才交人工**：重試與換 builder 都用盡時才轉 needs_human，附上完整的嘗試紀錄與建議動作。
- [x] **T5 rechain 一步到位**：`work rechain` 增加接續動作選項（retry-build 或 retry-card），或讓 rechain 後保持可重試狀態；review 階段不得因 rechain 而在舊 candidate 上重派審查。
- [x] **T6 可觀測與文件**：`status`／`work show` 顯示重試次數、換 builder 的紀錄與剩餘額度。新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
