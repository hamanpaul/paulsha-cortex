---
status: accepted
work_item: fix-builder-write-paths
---

## Goals

修正 builder persona 的 `write_paths` 硬編碼 `paulsha_cortex/**`，使跨 repo 派工時 builder 能寫入目標 repo 的路徑。

## Why

`#118` 回報：`paulsha_cortex/persona/personas.yaml` builder role 的 `write_paths: ["paulsha_cortex/**", ...]` 硬編碼。派工到不同 repo（如 paulshaclaw）時，builder 拒絕寫入因為目標 repo 路徑不符 `paulsha_cortex/**`。`write_paths` 烤進 persona catalog，非從目標 repo 動態推導。

## What Changes

- `paulsha_cortex/persona/personas.yaml`：builder `write_paths` 改為動態——從 `run.workspace_root` / 目標 repo 結構推導，或使用 `["**"]` 由 worktree boundary 限制，或 per-repo 可配置。
- `paulsha_cortex/persona/contract.py` 或 `render.py`：render persona contract 時動態推導 `write_paths`。

## Capabilities

### Modified Capabilities

- `persona-contract`：builder `write_paths` 相對於 worktree root，非絕對於 cortex repo。跨 repo 派工時 builder 可寫入目標 repo 路徑。