### Fixed

- **#1233 backoff lock 在 root 建立前後分裂**：`record_backoff` 等 writer 先建好 coordinator root 再拿 exclusive lock，lock 一律落在 root 內。reader 在 root 不存在時直接讀（此時沒有已提交的 state），不再到祖先目錄建 lock。修正前，root 由 writer 建立的那段時間裡，writer 和 reader 會各自持有不同的 lock 檔，reader 可能讀到 state 還沒寫完（CI 上偶發的 `missing`）（#1233）。
