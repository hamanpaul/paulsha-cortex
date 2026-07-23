---
status: accepted
work_item: porcelain-init-sample
---

# Design

## Decisions

- **必補欄位清單內建化**：對照 F4 落差表寫死於輸出邏輯，不要求使用者逆向工程測試程式碼。
- **combo 白名單校驗**：未知 `--combo` 於校驗層 exit 2，不呼叫 `deck compile`。
- **絕不翻 auto**：程式碼路徑不存在寫入 `dispatch: auto` 的分支。
- **薄包裝**：不重新實作 deck 編譯邏輯，只疊加清單與指引。
