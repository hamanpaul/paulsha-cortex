---
status: accepted
work_item: feat-task-type-combo-selector
---

# feat-task-type-combo-selector Todo

## Tasks

- [x] 將 issue #202、active OpenSpec change `2026-08-04-feat-task-type-combo-selector` 與本 Todo 綁定為同一 confirmed Work Item（依賴：`design-task-type-taxonomy`（#139）骨架先落地）。
- [x] coordinator 派工 copilot（gpt-5.4）以 TDD 完成 #202：先依 `docs/superpowers/plans/feat-task-type-combo-selector.md` 寫 RED 測試，再實作到 GREEN。
- [ ] ForeignReview（claude/sonnet）review 通過；operator 驗收核可（含 fix-standard 對 comment 草稿補 define/plan 卡的偏離追認）。
  - ForeignReview（獨立 verification）已跑過一輪並判定 FAILED（`claim.mapped_issue_titles` 對 durable snapshot 不可用時未 fail-soft），本輪已由 claude/sonnet 修復並重新確認 pytest／policy_check 綠燈；operator 驗收核可尚待人工 merge 前完成，故此項暫不勾選。
- [x] `changelog.d/feat-task-type-combo-selector.md` fragment 已新增且已 commit（R-09 硬性 gate）；`CHANGELOG.md [Unreleased]` 有對應 entry。
- [x] 帶 PR 上下文執行 `policy_check`（`--pr-title`／`--pr-body`／`--pr-labels`／`--pr-base-ref`／`--pr-head-ref`）確認 fail: 0；全套 pytest 通過。
