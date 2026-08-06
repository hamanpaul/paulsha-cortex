---
status: accepted
work_item: design-task-type-taxonomy-v2
---

# design-task-type-taxonomy Todo

## Tasks

- [ ] 將 issue #139、active OpenSpec change `2026-08-04-design-task-type-taxonomy` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 copilot（gpt-5.4）以 TDD 完成 #139：先寫 RED 測試，再實作到 GREEN。
- [ ] ForeignReview（claude（sonnet））review 通過；operator 驗收核可。
- [ ] `changelog.d/design-task-type-taxonomy.md` fragment 已新增且已 commit（R-09 硬性 gate）；`CHANGELOG.md [Unreleased]` 有對應 entry。
- [ ] 帶 PR 上下文執行 `policy_check`（`--pr-title`／`--pr-body`／`--pr-labels`／`--pr-base-ref`／`--pr-head-ref`）確認 fail: 0；全套 pytest 通過。
