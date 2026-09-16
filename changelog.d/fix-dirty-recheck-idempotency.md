---
type: fix
scope: coordinator
---
**Issue #496：dirty recheck 結果未變時不再每 tick 追加 verification-failed action／evidence_history**

`complete_tick` 對 needs_human 且 summary 為 `candidate-worktree-dirty`／
`candidate-worktree-dirty-after-verification` 的 slice 每 tick 重驗，讓 operator 清乾淨
worktree 後能自動脫困；舊實作重驗後無條件 `_apply_verification_result`，一個放著不管的
dirty slice 六天累積 92k 筆 `verification-failed` history／actions、jobs.json 膨脹到 58.7 MB。
本次只在該呼叫點加 no-op 閘門（`_dirty_recheck_is_noop`）：canonical 內容 hash 對 slice 的
`current_verification_evidence_hash`（#501 已分離的欄位）、state／gate_state、candidate、
summary、current evidence refs 全部一致才略過 `record_action`／`update_slice`；驗證本身仍每
tick 執行。同 path 不同內容、缺合法 hash、current evidence 不可解析或 refs 指向別處都不算
相等，沿原路徑做恰好一次真實轉換；新 evidence 不可讀／hash／payload 不符維持既有 fail-closed。
不動 contract hash、immutable evidence writer、#497 terminal supersession 與 #821 persistence。
