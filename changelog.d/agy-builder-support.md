- **#799：agy builder launcher 支援**：builder 使用 `--mode accept-edits` 並限制在
  provisioned worktree；`allow_unsafe` 只在明確指定時附加
  `--dangerously-skip-permissions`，`commit_required` 會同步放行 linked-worktree Git
  metadata，planner／reviewer 的 `plan+sandbox` 形態維持不變。
