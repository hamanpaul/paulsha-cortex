---
status: accepted
work_item: main-sync-durable-context
domain_breadth: 0
state_consistency: 2
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 6
sizing: yellow
---

# Main-sync durable context 規格

## Requirements

本 work item 只改 `manager.py`，透過 child 02 的 DiagnosticReason v3 `MainSyncContext`，將 stop context durable 寫入同一 WorkflowRun，並在 status attention 投影對齊 child 03 action hint。

- Manager `_persist_main_sync_stop` API 在同一 WorkflowRun update 保存 needs_human facet/reason、既有 delivery_reason/detail 與 JSON-native `context.main_sync`：C、M|null、完整 conflict paths array、repair_kind、skipped_reason、typed failure (stage/returncode/error_kind/main_head)。不得把 nested value stringify 到 legacy string context。
- 此 writer 必須接上兩個現行 Manager ship-validator `needs_human` branches，且兩者各有 wrapper-to-registry-read-back integration test：(1) `apply_workflow_action` 的 review card 已通過後 `advance-ship` replay branch；(2) 最後一張 review evidence 完成時的 `advance-phase` transition branch。兩個 branch 均讀回原始 C/M/full paths/failure/repair fields，並保留各自的 `delivery_reason` 與 detail。
- fetch 後故障的 `failure.main_head=M` 必須原樣進 durable context。invalid SHA 不當成可重試 M；缺合法 M 的 failure 使用 null。
- 同一 writer API 接受 automatic repair candidate 的 skipped stop reason。用 synthetic input 測 `repair-budget-exhausted`、`registry-reset-refused`，read-back 必須保留原 C/M/full paths/repair_kind/skipped_reason；這只是 writer-contract test，不觸發 repair/reset/dispatch。
- Durable read-back 是 action authority；evidence-only、validator 暫存 dict 或截短 reason 不可替代。寫入或 read-back 失敗時不曝光 retry-build。
- 確認 fetch error 已修復後的 operator resume 會由 Manager ship validator 第二次呼叫產生新 probe；不得因 resume 僅重入非-define `start_canonical_workflow` no-op 而宣告成功。Child 01 負責 fail→repair→resume→reprobe bare-origin integration，Child 04 負責首輪 stop 在上述兩種 Manager wrapper branch 都能 durable/read back。
- `workflow_status_entry` 使用 child 03 的 work_actions 公用 action/hint helper；只有實際可用 action 才顯示相符 status hint，resume hint 也須與 status next action 一致。
- 本票不 probe、不 reset、不派 Builder、不 merge/push。對應 #972 wrapper/context/read-back/status AC；#943 complete merge/ship AC 留給 #973。
