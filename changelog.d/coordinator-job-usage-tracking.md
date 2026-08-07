### Added
- **Issue #325：job record 收斂 token usage——per-lane 成本歸屬的最小底座**：新增
  `paulsha_cortex/coordinator/usage_extractors.py`，依 executor 從 headless session
  log 抽取 token 用量——codex 讀最後一筆 `turn.completed.usage`（累計值）；claude
  優先讀最後一筆 `result.usage`（`cache_read_input_tokens` 對映 `cached_input_tokens`，
  非 `cache_creation_input_tokens`），缺席時 fallback 逐行累加 `message.usage`；
  copilot **刻意不讀** `result.usage`（該欄位是 session 層 premiumRequests/duration
  統計、不含 token 數，若比照 codex/claude 讀取會得到看似合理但語意錯誤的資料），
  改為逐行累加 `assistant.message.data.outputTokens`；agy 目前受 headless permission
  問題阻塞、無正常樣本，明確標記 unsupported。全程 fail-soft，任何解析失敗只回報
  `usage=None` + `usage_reason`，不拋例外、不影響 job 的 status/exit_code 判定。
  job record 新增 `usage`／`usage_raw`／`usage_reason`／`started_at`／`exited_at`
  五個欄位（`attach_launch_handle` 寫入 `started_at`，`update_headless_result` 寫入
  `exited_at` 並觸發用量抽取），`_validate_loaded_job` 對既有無此欄位的歷史 job
  向後相容（`None` 一律放行，非 `None` 才驗型別）。新增
  `paulsha_cortex/coordinator/usage_aggregate.py::aggregate_usage_by_run` 依
  `workflow_run_id` 加總同一 run 底下所有 job 的用量，並掛上
  `cortex stat --usage-by-run WORKFLOW_RUN_ID` 輸出彙總 JSON。
