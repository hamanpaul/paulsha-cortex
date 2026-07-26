---
status: accepted
work_item: fix-builder-write-paths
---

# fix-builder-write-paths Specification

`#118`：修正 builder persona `write_paths` 硬編碼 `paulsha_cortex/**`，使跨 repo 派工時 builder 可寫入目標 repo 路徑。

## Requirements

### R1 write_paths 相對 worktree root

builder persona 的 `write_paths` MUST 相對於 worktree root，非絕對於 cortex repo。跨 repo dispatch 時 MUST 涵蓋目標 repo 路徑。可採 `["**"]` 由 worktree boundary 限制，或從 `run.workspace_root` 動態推導。

### R2 跨 repo dispatch 不拒絕寫入

跨 repo dispatch（target repo 非 `paulsha_cortex`）時，builder MUST NOT 因 `write_paths` 不符而拒絕寫入。

### R3 向後相容

cortex repo 內 dispatch 時行為不變——`write_paths` 仍涵蓋 cortex 路徑。

### R4 限制

- stdlib-only；TDD。
- 不得改變既有對外 CLI envelope schema。
- `test_zero_dependency_runtime` 續綠；`python3 -m policy_check --repo .` 0 fail。