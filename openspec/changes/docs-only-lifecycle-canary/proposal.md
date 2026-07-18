---
work_item: docs-only-lifecycle-canary
---

## Why

Unified lifecycle 已完成實作與部署，但仍需要一條低風險 docs-only live canary，證明 confirmed Todo + issue auto label 能進入異質 brainstorm、build、review 與 strict delivery closure。

## What Changes

- 在 unified lifecycle 操作文件留下 live canary evidence 記錄。
- 不修改 runtime code、public API 或既有治理語意。
- 初始 change 刻意不提供 accepted superpowers plan，讓 completeness gate 必須執行異質 brainstorm。

## Capabilities

### New Capabilities

- `lifecycle-live-canary`: 定義 docs-only lifecycle canary 必須留下可審查的操作證據。

### Modified Capabilities

無。

## Impact

只影響 `docs/unified-work-lifecycle.md`、本 OpenSpec change 與 canary issue；不改 production code。
