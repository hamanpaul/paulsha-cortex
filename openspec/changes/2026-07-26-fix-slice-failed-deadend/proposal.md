---
status: accepted
work_item: fix-slice-failed-deadend
---

## Goals

修正 slice state machine `failed` 為終端 sink 無退出路徑，使 failed slice 可恢復（retry-build / reset / re-dispatch），並確保 registry daemon 可從磁碟重載。

## Why

`#153` 回報：`paulsha_cortex/coordinator/registry.py` 的 slice state machine 有 `"failed": frozenset({"failed"})`——`failed` 無 outgoing transitions。failed slice `actions: []`、repin 被拒（`ValueError: 非法 slice state repin: 'failed'`）、`retry-build` 不允許（僅 `needs_human` 有）。且 registry daemon 將 jobs 保留在記憶體，刪除 `jobs.json` 無效。

## What Changes

- `paulsha_cortex/coordinator/registry.py`：
  - 新增 `failed → needs_human` 或 `failed → building` transition，或新增 `reset` action 清除 failed state 允許 re-dispatch。
  - 確保 registry daemon 可從磁碟重載（若 `jobs.json` 被修改）。
- `tests/`：failed state recovery 路徑測試。

## Capabilities

### Modified Capabilities

- `coordinator-registry`：`failed` slice state 不再是終端 sink；可恢復至 `needs_human` 或 re-dispatch。registry 可從磁碟重載。