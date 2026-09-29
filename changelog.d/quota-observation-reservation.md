# quota-observation-reservation

- **`#836` shadow 模式記錄 Cortex 自家受管 job 的終局用量**：periodic tick 的
  `manager.reconcile_quota_admission_reservations()` 新增第三步
  `harvest_quota_terminal_usage()`，shadow 與 enforce 都執行——依 registry
  與 admit receipt 的事實（enforce 用 `quota_decision_id` 精確比對；shadow 用
  attempt ordinal＋`selected` executor／model_id，對不上或有歧義就不記）把
  已終局 workflow job 的 usage 經既有 `_quota_admission_record_terminal_usage`
  寫進 quota ledger。idempotency key 與 enforce settle 路徑相同，重複
  harvest、重啟、`on_settled` 先記過都只有一份；記到一半 crash 的缺口下一輪
  補齊。結果以 `terminal_usage`（`recorded`／`failed`／`skipped` 理由）進
  daemon tick summary。`QuotaShadowService.record_terminal_usage()` 新增選填
  `known_idempotency_keys`，命中已在 ledger 的 key 直接算 duplicate，不再逐筆
  重讀 ledger；`quota_ledger.caller_idempotency_key()` 統一 caller key 前綴。
- **`#836` 測試**：新增 `tests/test_quota_terminal_usage_harvest_836.py`——
  controller／worker／reviewer 三個 profile 同帳號經 production helper 記帳後
  short／week 剩餘量恰為 snapshot 減總用量（重疊 group binding、原樣重播、
  重啟、跨 profile 重播都不多扣）；真 `dispatch_workflow_card`（shadow）＋
  真 `update_headless_result` usage 抽取後 periodic reconcile 收割一次、再跑與
  重啟不增 row；enforce 的 `on_settled` 與收割不重記且 reservation 釋放後只剩
  usage 一份扣減；ordinal／identity／歧義／精確 decision id／半途 crash／缺
  usage／缺 unit mapping 的收割判定。
