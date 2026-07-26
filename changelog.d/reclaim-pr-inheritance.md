### Fixed
- **Issue #175：reclaim PR inheritance**：`coordinator` 在 needs_human 與 start phase 都不再帶入任何 `pr_refs`，並且 `GitHubTerminalProvider` 不會把 `state=CLOSED` 但未合併的 PR 轉為 `closing_links`，避免 closed PR 仍污染關聯主線閉環。
