- **#799：agy builder launcher 支援**：builder 使用 `--mode accept-edits` 並限制在
  provisioned worktree；`allow_unsafe` 只在明確指定時附加
  `--dangerously-skip-permissions`，`commit_required` 會同步放行 linked-worktree Git
  metadata；write-forbidden builder 維持嚴格 `plan+sandbox`，並以唯讀
  `--add-dir` 檢視 provisioned worktree；宣告 `build` capability 的 host overlay 以測試釘住 writable argv 形狀，
  packaged fallback roster 的 planner／reviewer `plan+sandbox` 形態維持不變。
