# auto-retry-fallback

- **#1306 gate／review 自動恢復與 builder fallback**：Manager 對 gate contradiction 與 blocking review findings 以已驗證的 exact candidate 自動 retry-build，將失敗摘要或 review findings 帶入下一次 build，並持久記錄每 run 的重試上限、嘗試原因與 builder 轉換。預設每位 builder 可重試 2 次，可在建立 run 時以 `PSC_WORKFLOW_AUTO_RETRY_LIMIT` 設定 0–10；此設定只控制 gate／review recovery，provider-failure retry 仍維持獨立的固定上限 2。用盡可用重試與獨立 builder 後才轉 `needs_human`。已知 permission／sandbox executor 錯誤可提前換派合格 builder，verify／review 階段 rechain 則回到 build 並重新執行下游檢查。`status` 與 `work show` 顯示重試次數、剩餘額度及 builder 轉換紀錄。
