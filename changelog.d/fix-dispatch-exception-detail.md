### Fixed

- **Issue #100：tick 的 dispatch 失敗回報與日誌時間戳**：`DispatchReadyError` 改為輸出每個 slice 的詳細錯誤摘要，`manager.tick` 將失敗切面轉為包含 `slice_id`/`type`/`message` 的錯誤回傳，並保留已啟動 `jobs`；`manager_daemon` 錯誤輸出改為 `ISO-8601` 時間戳前綴。
