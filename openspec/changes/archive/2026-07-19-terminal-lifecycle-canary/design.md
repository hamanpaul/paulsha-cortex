---
work_item: terminal-lifecycle-canary
---

## Context

Repo-local override 將 issue #31 與新的 active OpenSpec confirmed mapping。Bootstrap merge 後才加入 auto label。

## Goals / Non-Goals

- Goal：驗證 auto claim、`agy/google` brainstorm、docs-only build、ForeignReview、archive、preflight、current-HEAD maintainer review、merge commit 與 done projection。
- Non-goal：修改 runtime code、直接修改 Manager registry，或對其他 repo 啟用 auto label。

## Decisions

- 使用全新 issue/work ID/OpenSpec；前兩條 canary 保留 fail-closed 診斷紀錄。
- accepted planning artifacts 必須由 primary planner 整合 `agy/google` evidence 後發布。
- 依 operator 指示使用 exact-HEAD maintainer adversarial review；其餘 strict gates不變。

## Risks / Trade-offs

- 任一 typed output 或 gate 不通過即維持 `needs_human`，不得宣稱完成。
