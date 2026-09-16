- **#799：agy builder launcher 支援**：builder 使用 `--mode accept-edits` 並限制在
  provisioned worktree；`allow_unsafe` 只在明確指定時附加
  `--dangerously-skip-permissions`，`commit_required` 會同步放行 linked-worktree Git
  metadata；write-forbidden builder 維持嚴格 `plan+sandbox`，並以唯讀
  `--add-dir` 檢視 provisioned worktree；host overlay 有／無 `build` capability 皆以測試釘住
  direct launcher 的 writable argv 形狀，capability 閘控維持在 roster 選擇，packaged fallback
  roster 的 planner／reviewer `plan+sandbox` 形態維持不變。
- 部署提醒：host overlay 宣告 agy `build` capability 後，trust-root 的 builder 帳號尚未持有
  agy 憑證（`~/.gemini` 只掛在 reviewer-planner），實際派工會在 per-job 憑證投影階段
  fail-closed；須待 issue #805 補 `(builder, agy)` 的 `CREDENTIALED_ACCOUNTS` 與
  toolchain/egress 剖面。
