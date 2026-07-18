---
work_item: docs-only-lifecycle-canary-v2
---

## Context

Canary 使用 repo-local override 將 issue #27 與新的 active OpenSpec confirmed mapping。Bootstrap merge 後才加入 `cortex:auto-on-going`，避免 artifacts 尚未在 default branch 時提前 claim。

## Goals / Non-Goals

- Goal：驗證 auto claim、`agy/google` 異質 brainstorm、docs-only build、ForeignReview、archive、preflight、current-HEAD maintainer review、merge commit 與 done projection。
- Non-goal：修改 runtime code、直接修改 Manager registry，或對其他 repo 啟用 auto label。

## Decisions

- 使用全新 issue、work ID 與 OpenSpec，保留第一條 canary 的 fail-closed 診斷紀錄。
- 初始 change 不建立 accepted superpowers plan；Manager 必須由 primary planner 與 `agy/google` secondary 補齊 planning authority。
- 依 operator 指示，以 exact-HEAD maintainer adversarial review取代等待 Copilot；其餘 strict gates不變。

## Risks / Trade-offs

- Model output 若不符合 typed artifact contract，workflow 應 fail-closed 到 `needs_human`，不得繞過。
- Canary 未通過前不對其他 repo 加 auto label。
