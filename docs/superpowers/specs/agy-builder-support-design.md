---
status: accepted
work_item: agy-builder-support
---

# Agy Builder Support Design

## Decisions

- `build_agy_argv` 依語境三分：`read_only` → `--mode plan --sandbox`（不變）；`review_only` → `--mode plan --sandbox --add-dir <worktree>`
  （不變）；其餘（builder）→ `--mode accept-edits --add-dir <worktree>`。builder 形態**不加** `--sandbox`：#568 實測 Antigravity 的
  sandbox 會對需要 unsandboxed 的工具 headless auto-deny 而零輸出，build 必然需要 shell／git；隔離由 Manager provision 的
  worktree 與 trust-root 帳號邊界承擔。
- `allow_unsafe=True` 時 builder 形態附 `--dangerously-skip-permissions`，取代現行 `raise ValueError("agy executor does not support unsafe mode")`；
  `SubprocessLauncher.__init__` 對 agy 的 `allow_unsafe` 拒絕同步移除（cg 的拒絕維持不變，cg 仍是 zero-tool）。
- commit 契約沿用 `build_claude_argv` 的做法：builder prompt 內含 commit 要求，Manager 以 branch commits／sentinel 偵測；
  `SubprocessLauncher.__init__` 對 executor 集合 `{"codex","copilot","claude","cg"}` 的 `commit_required` 傳遞擴及 `agy`。
- `_verdict_spool_dir` 對 agy 的顯性拒絕與 planner／reviewer 相關註解維持（reviewer 面不在本 work item）。
- 測試以 argv 純函式斷言為主，不啟動真實 agy 行程；模型身分 `agy/gemini-3.7-flash-high` 的 `build` capability 與
  launcher 形態的交叉驗證放在同一測試檔。
