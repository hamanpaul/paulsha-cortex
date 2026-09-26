---
status: accepted
work_item: porcelain-run-work-retry-card-option
issue: 1030
---

# `run work retry-card --card` 規格（#1030）

## Requirements

### Problem and boundary

對應 live [#1030](https://github.com/hamanpaul/paulsha-cortex/issues/1030)。`cortex run work` 已有 action/run-scoped builder override 與 `--payload`，但沒有正式 `--card` option。Manager 的 `retry-card` 已要求 exact card ID，故標準命令在 argparse 階段因 unknown option 退出。用 `--payload` JSON 帶 `card` 可行；2026-09-24 已用它為 #1020/#1021 recovery 派出 Copilot jobs #962/#963。此 CLI 修正不阻塞該恢復，也不代表 jobs 已完成。

唯一產品變更是為 porcelain `run work retry-card` 提供直接、可發現的 card selector，並同步 help / README。#843 是全 recovery action 的契約矩陣與入口一致性驗收；本票修明確的 producer 參數缺口，不替代、不重做該矩陣。

### R1030.1 — 精確轉送 card selector

`cortex run work retry-card <work_id> --repo <owner/repo> --expected-run-id <workflow-id> --card <card-id>` MUST 在提交的既有 `work-action` request 中帶入同一個 `card`、`action`、`work_id`、`repo` 與 `expected_run_id`。現有 `--builder-executor`／`--builder-model` 等 run-scoped options MUST 原樣保留在同一 request；後續 dispatch 與 identity/capability 檢查沿用現有 Manager 行為。

`card` syntax MUST 繼續遵守現有 `control.contract` exact-card 格式與 `retry-card` exact `expected_run_id` CAS；不得放寬 `retry-card` action／phase／下一張待派卡／terminal job／accepted-evidence 驗證。#557 的 `regenerate-gates` 選擇器使用相同 card 格式，僅在有多張符合條件的 build 卡時要求提供。

### R1030.2 — action scope fail closed

`--card` 對 `action=retry-card` 與 `action=regenerate-gates` 有效；其他 action 若帶 `--card` MUST 在送 request 前明確報錯，不能默默忽略或轉送。`retry-card` 缺少 `--card` 或提供不合法 card 時，沿用正式 `work-action` contract 的拒絕，且不得寫入 request。`regenerate-gates` 的 `--card` 選擇該 run 內符合條件的 build job；多張卡時必須指定，只有一張時省略仍沿用取最新 job 的相容行為（#557）。

### R1030.3 — 保留 payload 兼容

`--payload` 仍是可選 JSON object 檔案入口，card 不再需要靠它提供。為兼容當前 workaround，`--payload` 單獨提供 `card` 的舊命令仍可用；若同一 invocation 同時提供 `--card` 與不同的 payload `card`，CLI MUST 在送 request 前拒絕，不能讓 payload 靜默覆蓋明示 selector。相同值重複可保留或明確拒絕，實作須以單一測試鎖定行為。

此項只約束 CLI 對 `card` selector 的合併，不擴大 Manager 接受的欄位。`_retry_card_action` 仍以既有 allowlist 拒絕其他 caller input；不得由 `--payload` 是 JSON object 推論 retry-card 支援任意 evidence refs。其他 payload 欄位仍依各 action 原有的 Manager contract 判定。

### R1030.4 — help 與操作範例

`cortex run work --help` MUST 顯示 `--card` 及 `retry-card`／`regenerate-gates` 的用途。README recovery 範例 MUST 展示 `--expected-run-id`、`--card`，並可同時帶已有 run-scoped builder override；說明 `--payload` 不是此命令選 card 的必要輸入，payload 欄位仍受各 action 既有 Manager contract／allowlist 限制。

### Non-goals

不改 `retry-card` 的 Manager action／authorization、control contract、`cortex work retry-card` 或 `cortex recover work` 入口，不重做 #843 recovery matrix，不變更 #1020/#1021 review 判定、job內容或 release gates。CLI request accepted 不等於 replacement job 啟動或成功。
