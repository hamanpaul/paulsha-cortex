---
work_item: terminal-lifecycle-canary
---

## Why

前兩條 canary 以 fail-closed 方式揭露 test isolation、resume routing、active-run source churn 與 disposable planner 啟動缺口；修正合併後需要全新 authority 驗證 terminal closure。

## What Changes

- 在 unified lifecycle 操作文件留下 terminal live canary evidence。
- 驗證 confirmed Todo + issue auto label、異質 brainstorm、docs-only build、review 與 strict delivery closure。
- 初始 change 不提供 accepted superpowers plan，強制 completeness gate 執行異質 brainstorm。
- 不修改 runtime code或既有治理語意。

## Capabilities

### New Capabilities

- `lifecycle-terminal-live-canary`: 定義 terminal docs-only canary 的可審查 evidence。

### Modified Capabilities

無。

## Impact

只影響 `.cortex/work-items.yaml`、`docs/unified-work-lifecycle.md`、
`docs/superpowers/workstreams/terminal-lifecycle-canary/todo.md`、本 OpenSpec change 與 issue #31。
