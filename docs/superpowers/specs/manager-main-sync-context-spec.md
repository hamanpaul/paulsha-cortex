---
status: accepted
work_item: manager-main-sync-context
issue: 990
domain_breadth: 0
state_consistency: 2
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 6
sizing: yellow
---

# Manager main-sync stop context 規格

## Requirements

本 work item 的 production scope 僅限 `paulsha_cortex/coordinator/manager.py`。它消費 #988 更新後的 typed `MainSyncContext` 契約；該契約必須能表示 Candidate 尚未驗證就發生的 stop。共用 `_persist_main_sync_stop(run_id, context, ...)` writer 將 `needs_human` facet、`delivery-needs-human` reason 與 `needs_human_reason.context.main_sync` 寫入同一筆 WorkflowRun durable update。

`context.main_sync` 必須是 #988 更新後定義的 typed JSON-native 結構。它完整保留已驗證 Candidate C；若 stop 發生在 Candidate 驗證前，則依更新後 #988 wire contract 保留原始 invalid/abbreviated C observation，且明確標為 diagnostic-only、不得授權任何 action。其餘欄位完整保留 `main_head` M 或 null、每一條原樣 conflict path、`repair_kind`、`skipped_reason`，以及可為 null 的 `failure.stage`、`failure.returncode`、`failure.error_kind`、`failure.main_head`。已取得合法 M 後的 fetch 後錯誤必須保留同一 M；Candidate 驗證前或 probe 未取得合法 M 的 failure，M 與 failure main head 都明確為 null。Manager 不解讀或改寫 #988 欄位形狀，不得 stringify、截斷、移除或另存這些欄位來代替 WorkflowRun context。

Candidate 驗證前的 failure read-back 必須保留原 invalid/abbreviated C observation、typed failure 與 M=null；這類 context 不能曝光 `retry-build`。Writer 必須保留既有 `delivery_reason`、`detail` 與 `needs_human_reason.context` 的其他欄位；寫入失敗或讀回不一致時不得回報成功、不得曝光 `retry-build`，且不得清除不屬於本 writer 的 context。

兩個正式 Manager ship-validator `needs_human` 分支都必須使用同一 writer：

1. `apply_workflow_action` 中已 passed review card 的 `advance-ship` replay branch。
2. 最後 review evidence 完成後的 `advance-phase` transition branch。

每個分支都要經實際 Manager wrapper 更新 WorkflowRun，再從 registry store 讀回並逐欄比較 C 或 invalid-C observation、M/null、完整 paths、repair/skipped/failure 欄位與原 delivery reason/detail；Candidate 驗證前 failure 另須證明 retry-build 不可用。只檢查 evidence 或 writer 的暫存輸入不符合此要求。

`workflow_status_entry` 的 next-step hint 必須使用 #989 `work_actions.py` recovery helper 的 action availability/result。只有 helper 判定可用時才顯示 `retry-build`；`main-sync-unavailable` 的 `resume` 與符合 #989 精確條件的 `review-advance-failed` `resume` hint 必須和 helper 結果一致。缺漏、損壞、更新失敗或 read-back mismatch 的 context 一律 fail closed，不宣稱 retry action 可用。

使用 typed synthetic context 驗收 `repair-budget-exhausted` 與 `registry-reset-refused` 兩種未來 stop：writer 應在同一 needs_human WorkflowRun 持久化原 C/invalid-C observation、M/null、完整 paths、repair kind、skipped reason 和 failure 欄位；從 registry store 讀回完全相同的值。本 work item 不執行 automatic repair、reset 或 Builder。

本 work item 不改 probe 算法、`work_actions.py` recovery action、job selector、Builder/merge/push，也不產生 merge commit。#972 與 #943 的完整驗收不因本子票完成而縮小；只有 #972 全部 AC 與 bare-origin 整合驗收完成後，才解除 #973 對 #972 的依賴。
