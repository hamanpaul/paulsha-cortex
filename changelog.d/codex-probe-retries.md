### Fixed

- **#716 Codex app-server 探針重試**：deployment canary 的 codex status 探針改為最多 3 次、每次 60 秒（總上限與原本 90 秒×2 相同）。app-server 查 `account/read` 偶爾會卡住，重開 process 就會恢復；2026-09-30 兩度連續逾時兩次，但同一個 main 重跑即通過（#716）。
