---
work_item: docs-only-lifecycle-canary-v2
---

## Why

第一條 live canary 揭露並修正 production auto-claim 的 test isolation 缺口，但其 durable authority 已被污染，不能直接修改 registry 後冒充完整閉合。需要全新 confirmed authority 進行乾淨的 docs-only canary。

## What Changes

- 在 unified lifecycle 操作文件留下 clean live canary evidence。
- 驗證 confirmed Todo + issue auto label 可進入異質 brainstorm、build、review 與 strict delivery closure。
- 不修改 runtime code、public API 或既有治理語意。
- 初始 change 刻意不提供 accepted superpowers plan，讓 completeness gate 必須執行異質 brainstorm。

## Capabilities

### New Capabilities

- `lifecycle-clean-live-canary`: 定義乾淨 docs-only lifecycle canary 的 terminal evidence。

### Modified Capabilities

無。

## Impact

只影響 `docs/unified-work-lifecycle.md`、本 OpenSpec change 與 issue #27；不改 production code。
