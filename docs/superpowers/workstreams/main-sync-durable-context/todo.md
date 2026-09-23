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

# Main-sync durable context todo

## Tasks

- [ ] 在 Manager `_persist_main_sync_stop` API 以 child 02 typed `MainSyncContext` 保存同次 stop reason/context，保留 C/M/full path array/repair_kind/skipped_reason/typed failure 及原 delivery_reason/detail。
- [ ] 將 writer 接入兩個 Manager ship-validator `needs_human` branches：通過 review card 的 `advance-ship` replay branch，以及最後 review evidence 驅動的 `advance-phase` transition branch。
- [ ] 對上述兩個 wrapper branch 各加 post-wrapper WorkflowRun store read-back test：manual non-CHANGELOG stop、fetch 後 failure M preservation、每條 full path、原 delivery_reason/detail、typed C/M context 均完全保存。
- [ ] 加 writer API 的 synthetic `repair-budget-exhausted` 與 `registry-reset-refused` durable read-back tests、missing/corrupt fields、write/read-back mismatch fail closed；action authority 必須使用已 read-back 的 run record。
- [ ] 在 `workflow_status_entry` 使用 child 03 work_actions helper，驗 retry-build/resume hints 只對應實際 next_actions。
- [ ] 更新 lifecycle docs/changelog/Unreleased，跑 focused Manager/status tests 與 full policy/test gates。

## Sizing inputs

單一 production module；同一 WorkflowRun 的 stop facet/reason/context 要原子一致寫入並 read-back，state consistency 2。此 issue 自身 accepted triplet 對 spec stability 貢獻 0；fix-standard acceptance=2/orchestration=2，正式結果 6／Yellow。

## Dependencies

Blocked by descendants 01, 02, and 03. This API and its synthetic budget/refusal read-back tests are prerequisites for live #973, which remains blocked by #972.
