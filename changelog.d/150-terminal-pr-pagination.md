fix(monitor): GitHubTerminalProvider 改為 cursor 分頁聚合 pull requests，並設 20 頁硬上限；超限時維持顯式失敗，避免第 101 個 PR 讓 terminal provider 永久 degraded。
