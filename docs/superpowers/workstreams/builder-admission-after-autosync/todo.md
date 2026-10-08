---
status: accepted
work_item: builder-admission-after-autosync
---

# PR 已存在且經過 autosync 的 run 能 retry-build：Builder admission 認得 Manager 自產的推送

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1363`（含 issue 留言的 #1309 案例）。
- 現況：#1360 修好 delivery journal 的 `claim_key` 同步後，PR 已存在的 run 執行 `retry-build`、`resume`、`retry-card` 時，仍在 `manager.builder_todo_admission_decision`（約 15093 行）以 `builder-todo-authority-changed` 被拒：authority_revision 不等於 `run.source_revision`，而 `builder_authority_matches_claim_era`（先比對 claim era，不符時以 `self_delivery_evidence` 重試）也不成立。
- 現場：`retry-build-killed-repair`（PR #1349）、`auto-retry-fallback`（PR #1328）、`task-memory-disposition`（PR #1332）。
- 推測待確認：autosync 推到 PR 分支後，`self_delivery_evidence` 沒有把它算成 Manager 自產的交付事實；或 Builder admission 讀的 authority 來源與 #1360 修的 journal 不同。
- 不放寬真正改變交付目標的檢查：PR 改綁、issue 關閉、todo 改寫仍須拒絕。

## Tasks

- [ ] **T1 RED**：以正式生命週期重現：claim → build → ship 開 PR → autosync 推到 PR → retry-build，現行實作回 `builder-todo-authority-changed`。在 PR 中寫明實際不相符的欄位。
- [ ] **T2 最小修正**：只修 T1 查到的原因，讓 Manager 自己的推送（含 autosync）被 admission 認定為自產交付事實。
- [ ] **T3 fail closed**：PR 改綁、issue 關閉、todo 改寫時仍拒絕，reason 正確。
- [ ] **T4 恢復**：PR 已存在、經過 autosync 的 run，`retry-build` 能派出 builder；已經卡住的現有 run，升級後 retry-build 也能派出。
- [ ] **T5 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
