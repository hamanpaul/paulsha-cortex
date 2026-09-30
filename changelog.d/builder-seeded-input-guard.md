### Fixed

- **#1215 builder 刪除 seed 的 pinned 輸入**：commit-required builder 的 contract 說明 Manager 會把候選沒追蹤的 pinned 輸入以 untracked 檔 seed 進工作區，`source_material` 列出的路徑不得刪除、搬移、stash 或 `git clean`。build phase 的 builder terminalization 失敗（exit 0 但 evidence 未綁定）在 `retry-build` admission 成立時，next_actions 會列出 `retry-build`（#1215）。
