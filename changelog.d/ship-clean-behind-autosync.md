# #1311 ship clean-behind 自動同步

Manager 在受控 ship clone 合入已 probe 的 exact `origin/main`，只重跑 verify 與 ship probe，並沿用原 foreign review；每 run 預設上限 3 次，可用 `PSC_MAIN_SYNC_AUTOSYNC_MAX` 設 0–10。自動同步次數、main SHA、新 Candidate 與停止原因會寫入 run evidence，並由 `cortex work show` 顯示。
