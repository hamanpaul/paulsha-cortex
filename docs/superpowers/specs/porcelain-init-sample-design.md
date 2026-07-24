---
status: accepted
work_item: porcelain-init-sample
---

# porcelain-init-sample Design

## Goals

讓新手在不理解 deck/spec 概念的情況下，也能產出第一個可成功執行、且不會被誤觸自動派工的 sample workflow（issue #93）。

## Decisions

- **必補欄位清單內建化**：直接把 dogfood F4「deck emit 骨架與 auto 派工契約落差」的對照表（`plan` glob→exact path、`target_branch: null`→`main`、`verification: null`→persona-scope + name=policy command + full_suite baseline 物件）寫死於輸出邏輯，避免使用者逆向工程 `tests/test_coordinator_verification.py`。
- **combo 白名單校驗**：`--combo` 對照既有 deck combo 註冊清單做校驗，未知值於校驗層 exit 2，不進入 `deck compile` 呼叫。
- **絕不翻 auto**：`init-sample` 的程式碼路徑中不存在任何寫入 `dispatch: auto` 的分支；翻 auto 只在輸出文字中以「怎麼手動改」呈現，不提供旗標捷徑。
- **薄包裝 `deck compile --emit`**：不重新實作 deck 編譯邏輯，只在既有輸出之上疊加必補欄位清單與下一步指引。
