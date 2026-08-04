---
status: accepted
work_item: fix-preflight-closeout-order
---

# fix-preflight-closeout-order Todo

## Tasks

- [ ] 將 issue #263、active OpenSpec change `2026-08-04-fix-preflight-closeout-order` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 copilot（gpt-5.4）以 TDD 完成 #263：先依 `docs/superpowers/plans/fix-preflight-closeout-order.md` 寫 RED 測試，再實作到 GREEN。
- [ ] ForeignReview（claude/sonnet）review 通過；operator 驗收核可。
- [ ] `changelog.d/fix-preflight-closeout-order.md` fragment 已新增且已 commit（R-09 硬性 gate）；`CHANGELOG.md [Unreleased]` 有對應 entry。
- [ ] 帶 PR 上下文執行 `policy_check`（`--pr-title`／`--pr-body`／`--pr-labels`／`--pr-base-ref`／`--pr-head-ref`）確認 fail: 0；全套 pytest 通過。
