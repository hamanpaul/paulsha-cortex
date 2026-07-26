---
status: accepted
work_item: fix-builder-write-paths
---

# fix-builder-write-paths Design

## Decisions

### D1 採 `["**"]` + worktree boundary

builder `write_paths` 改為 `["**"]`，由 worktree boundary 限制寫入範圍。理由：persona 在 worktree 內執行，`["**"]` 等價「worktree 內任意路徑」，不需列舉特定 repo 的子路徑。

### D2 render 時動態推導（如需更精確）

若 `run.workspace_root` 可用，render 時可推導更精確的 pattern（如目標 repo 的主要 code 目錄）。但 `["**"]` 作為通用 fallback 已足夠。

### D3 personas.yaml 改動

`personas.yaml` 中 builder 的 `write_paths` 欄位改為 `["**"]`（或標記為 dynamic）。`contract.py` / `render.py` render 時直接使用，不再硬編碼 repo 名。

### 風險與 mitigation

- `["**"]` 範圍廣 → worktree boundary 為硬限制，builder 無法越界寫入。
- 既有測試斷言 `paulsha_cortex/**` → 需更新為 `**` 或動態推導結果。