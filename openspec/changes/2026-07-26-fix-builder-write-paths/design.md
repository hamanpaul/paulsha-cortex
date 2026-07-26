---
status: accepted
work_item: fix-builder-write-paths
---

# fix-builder-write-paths Design

## Decisions

### D1 write_paths 相對 worktree root

builder `write_paths` 改為 `["**"]`（或從目標 repo 結構推導的 pattern），由 worktree boundary 限制寫入範圍。關鍵洞察：`write_paths` 應相對於 worktree root，非絕對於 cortex repo。

### D2 render 時動態推導

`persona/contract.py` 或 `render.py` render persona contract 時，從 `run.workspace_root` / 目標 repo 結構推導 `write_paths`。若 `run` 有 `workspace_root`，以該 root 為基準展開 glob pattern。

### D3 向後相容

cortex repo 內 dispatch 時行為不變——`["**"]` 涵蓋 cortex 路徑。既有測試若依賴特定 `write_paths` 值需同步更新。

### 風險與 mitigation

- `["**"]` 範圍可能過寬 → 由 worktree boundary 限制，builder 不可越界。
- 既有測試斷言特定 `write_paths` 值 → 需更新斷言為動態推導結果或 `["**"]`。