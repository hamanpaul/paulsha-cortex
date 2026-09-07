---
status: accepted
work_item: fix-dirty-recheck-idempotency
---

# Dirty recheck 結果未變時的寫入冪等

## Boundary

- Issue：`hamanpaul/paulsha-cortex#496`；正式 work_id 保持不變。
- `complete_tick` 對 needs_human 且 verification summary 為
  `candidate-worktree-dirty`／`candidate-worktree-dirty-after-verification` 的 slice
  刻意重驗，讓 operator 清乾淨 worktree 後能自動脫困；該 recheck 不得移除。
- 本票只在 recheck 呼叫 `_apply_verification_result` 前判斷是否為真實轉換；
  不改原語其他呼叫點的語意，不實作 #497 terminal supersession 或 #821 persistence。
- #501 的 contract／evidence hash 分離已在本次基底：`registry.py` 的
  `_read_current_verification_evidence_hash`／`_normalize_loaded_slice_verification`
  與 manager 寫入 `current_verification_evidence_hash` 維持原意。本票不得讀寫
  `slice_row["verification"]["hash"]` 作為當前 evidence hash，也不重做 #501。
- 不改不可變 evidence writer／quarantine、fanout gate、tick clock、證據位址或
  verification runner 的驗證能力。#496 與 #497 各自測試與交付，不建立第三個 bucket-C run。

## Tasks

- [ ] 在 `complete_tick` dirty recheck 中，先以 `_validate_result_evidence` 驗證新
      evidence，再在 `_apply_verification_result` 呼叫點建立 no-op 閘門；不把
      所有呼叫者的去重責任下沉到 `_apply_verification_result`。
- [ ] 比較 canonical 內容 hash 與 slice 的 `current_verification_evidence_hash`，
      並比較預期 state／gate_state、summary、candidate 與 current evidence refs；
      全部一致才略過。只有 ref path 相同不能判成未變；合法 hash 缺失或目前證據
      不可解析不能當成相等。若內容相同但需修正 slice 狀態／引用，仍作一次真實轉換。
- [ ] 相同結果不 `record_action`、不追加 evidence_history、不 `update_slice`；
      驗證仍確實執行。若需要 last_rechecked_at 觀測，與 lifecycle history 分離，
      屬 optional；不能藉此恢復每 tick 的歷史 append。
- [ ] hash／status／gate_state／candidate／summary／refs 任一真實變更，沿原路徑
      記錄恰好一筆 action 及 evidence_history，更新 current evidence hash；下一次
      相同結果即 no-op。不改 contract verification hash。
- [ ] `_validate_result_evidence` 對不可讀／schema 不合法／payload mismatch 失敗時
      維持既有保守錯誤處理，不放行為成功、不視為證據未變；不得以 catch-all equality
      或空值相等遮蔽壞證據。
- [ ] 新增 `tests/test_dirty_recheck_idempotency_496.py` 或等價 focused 檔案，沿
      `tests/test_pre_candidate_recovery.py::test_candidate_worktree_dirty_reevaluation_on_tick`
      fixture：fake verification runner 回當前相同候選／內容，連續10次
      `complete_tick`，斷言 runner 仍被呼叫、action/history 數從既有基準完全不變、
      current evidence hash 不變；修前應 RED，不用降低 history limit 偽造不增長。
- [ ] 同一 fixture 在第K次改回傳證據 details，維持同 path 但 canonical hash 改變；
      斷言只增加一筆 action/history 且 hash 更新，後續 ticks 不再增加。此為受控
      測試 evidence fixture，不授權生產 writer 覆寫既有不可變證據。
- [ ] 另測 dirty→verified、candidate 改變、gate_state 或 refs 不一致修復，每次真實
      轉換恰好一次；原 `test_candidate_worktree_dirty_reevaluation_on_tick` 保持綠燈。
      不要求此測試依賴 #497 或 #821 已實作。
- [ ] 壞檔／不可讀／payload mismatch 均保持 fail-closed；contract hash 不受 recheck
      改動。沿既有 #501 normalization 測試確認舊資料讀取相容，不重寫 migration。
- [ ] 新增本正式 work_id 對應的 changelog fragment，同步
      `CHANGELOG.md [Unreleased]`；記錄 RED／GREEN、必要完整 gates、獨立 review、
      正確 HEAD merge 與 runtime 重複 tick 的 evidence/history delta。進件 accepted
      不代表任何測試或部署已完成。

## 歷史證據與關聯

- issue #496 的歷史現場為約116秒增加33筆 verification-failed action/history；
  該量測不等於這次 host 的最新值。成因是 `_apply_verification_result` 呼叫
  `record_action` 無條件追加，不是 `update_slice` 自己追加 history。
- #497 消除不該進入 completion 的舊 attempt；本票消除合法 dirty recheck 中未變
  結果的重複寫入；#501 已分離 contract/evidence hash；#821 管控檔案與 history 大小。
  各項完成狀態必須獨立驗證。
