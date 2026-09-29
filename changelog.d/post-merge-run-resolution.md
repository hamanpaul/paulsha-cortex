---
type: fixed
scope: coordinator
---
修正 #1141 ship lane 自動 merge 後 run 失聯：Monitor 把交付 PR 以 terminal 狀態關聯進
WorkAuthority、`Closes #N` 關票後，authority digest 離開 claim-era，periodic tick 續跑同一
run 時 `_canonical_workflow_run` 擲 `delivery WorkflowRun does not match current WorkAuthority`。
ship 重入（`_ship_action` 的 run 綁定與 `_validate_current_run_authority`）改為另外承認：
delivery journal 以完整 merge authorization 證明本 run 的交付 PR 已 merge，且 authority 的 PR
集合恰好只有這個 PR；符合時綁回同一 run、以 current authority 重驗 remote closure，寫入
CompletionRecord 並標 done。authority 出現其他 PR、PR 未由本 run merge，或非 ship 動作，
維持 fail-closed。`_merged_delivery_journal_bound`（#887）改接受 40-hex git tree id 與 run
自產的 OpenSpec change，先前 production 的 merge authorization 永遠不被承認。已交付 run 停在
`needs_human` 時，status／work list／claim 的 `next_actions` 與 `next_step_hint` 改指向
`retire-delivered`，不再給出必被 pre-delivery admission 拒絕的 `abandon`。
