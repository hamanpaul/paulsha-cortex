### Fixed

- 修正 Claude commit-required headless builder 未授權測試與提交的阻塞：以 per-job allowlist 放行 `git add`、`git commit` 及 operator 宣告的精確 Python 測試命令，保留 `acceptEdits`、事件 hook 與既有路徑授權；不修改全域設定。
