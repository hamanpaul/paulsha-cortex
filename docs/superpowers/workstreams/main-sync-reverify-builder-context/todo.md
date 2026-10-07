---
status: accepted
work_item: main-sync-reverify-builder-context
---

# clean-behind 自動同步後，verification 卡要能找到 candidate 的來源 job

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1336`。
- 現況：#1311 的自動同步成功後（candidate 變成 manager `main-sync-autosync` job 做出的 merge commit），下一輪 tick 派 verification 卡時，`manager._workflow_stage_execution_builder_context` 只收 builder job，或 `workflow_card == "openspec-archive"` 的 manager job，且要求 `subject_head == run.candidate_head`。找不到符合的 job，就丟出 `workflow reviewer builder job unavailable`，自動同步實際上無法使用。
- 相關：#1334（自動同步 merge 沒有 committer 身分），兩張都修好後自動同步才能端到端運作。
- 不放寬 verification 對 candidate、gate ledger 的既有檢查。

## Tasks

- [ ] **T1 RED**：端到端測試 clean-behind → 自動同步 merge 成功 → 下一輪 tick 派 verification 卡，現行實作會丟出 `workflow reviewer builder job unavailable`。
- [ ] **T2 來源 job**：`_workflow_stage_execution_builder_context` 把 `main-sync-autosync` 的 manager job 視為 candidate 的來源，與 `openspec-archive` 同等對待；`_dispatch_workflow_card` 後續推導 branch、base 的地方一併檢查。
- [ ] **T3 gate ledger**：明確定義自動同步後 verification 要使用哪份 gate ledger（沿用原 build 卡的 ledger，或重跑 gate），並寫測試固定行為。
- [ ] **T4 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
