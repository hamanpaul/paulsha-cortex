---
work_item: docs-only-lifecycle-canary
---

## Context

Canary 使用 repo-local override 將 issue #20 與 active OpenSpec confirmed mapping。Issue 在 bootstrap merge 後才加 `cortex:auto-on-going`，避免準備 artifacts 時提前 claim。

## Goals / Non-Goals

- Goal：驗證 auto claim、異質 brainstorm、docs-only build、ForeignReview、archive、preflight、current-HEAD maintainer review、merge commit 與 done projection。
- Non-goal：修改 runtime code，或對其他 repo 啟用 auto label。

## Decisions

- 只新增一段 live canary evidence 文件，將 blast radius 限在 docs。
- 初始 change 不建立 accepted superpowers plan；Manager 必須由 primary planner 與 `agy/google` secondary 補齊 planning authority。
- 本次依 operator 指示，以 exact-HEAD maintainer adversarial review 取代等待 Copilot；其餘 strict gates不變。

## Risks / Trade-offs

- Model output 若不符合 typed artifact contract，workflow 應 fail-closed 到 `needs_human`，不得繞過。
- Canary 未通過前不對其他 repo 加 auto label。
