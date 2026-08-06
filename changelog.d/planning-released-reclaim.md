### Fixed
- **Issue #299：planning_released 釋放後同 claim_key 可重新 claim**：
  `work_bridge.start_canonical_workflow` 的 existing-run reuse guard 原對
  `superseded` run 無條件短路，未 honor #256 D4 的 `planning_released` 釋放語意，
  導致 abandon→reclaim 永久死路。新增 `_claimable_existing_runs` 過濾已釋放
  run；registry 層以 attempt 鹽化 run_id 建新 run 的既有支援因此可達。未釋放的
  superseded／done／ongoing run 維持原短路行為。
