---
status: accepted
work_item: release-user-level-profile
---

# release workflow 使用者層級發版模式設計

## Decisions

### D1 加一個模式，不改原本的路

trust-root 模式的閘門，是好幾張票（#1122、#1263 等）累積出來的發版治理，這次不動。`user-level` 是另一條明確標示的路，只跳過「驗證 Trust Root 安裝」的那幾步。這一版本來就不提供 Trust Root 安裝，跳過這幾步不代表放寬 Trust Root 的發版標準。

### D2 用 `if:` 條件分流

每個只屬於 trust-root 的 job 或步驟，都加上 `if: inputs.profile == 'trust-root'`；只屬於 user-level 的，則加 `if: inputs.profile == 'user-level'`。不複製一份 workflow，避免兩份日後漂移。`release` job 的 `needs` 若包含 `qualification-gate`，要改成在 user-level 下也能執行，例如 `needs` 同時列出兩條路的 job，並用 `if: always() && ...` 判斷成功的是哪一條。實作時以 GitHub Actions 對被略過的 job 的語意為準，並在測試裡固定下來。

### D3 artifact 名稱分開

user-level 的 wheel 以不同的 artifact 名稱上傳，例如 `user-level-dist`。這樣 trust-root 的 `qualified-dist` 永遠只代表通過 RC 的產物，兩種產物不會混淆。

### D4 固定聲明寫死在 workflow

release notes 的聲明直接寫在 workflow 裡，由測試比對字串，不讓呼叫端傳入，避免 user-level release 漏掉聲明。

### D5 ingress 只補測試

`release_ingress.asset_names()` 預期三個資產（wheel、install-input、manifest），缺少時預期會拒絕。先寫測試確認；只有測試證明現行沒有 fail closed，才改程式。

### D6 Sizing

一個 workflow 檔加測試，可能加一處 ingress 測試，不碰 state，`domain_breadth: 0`、`state_consistency: 0`。
