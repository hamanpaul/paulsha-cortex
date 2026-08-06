---
status: accepted
work_item: fix-persona-catalog-portability-v2
---

# fix-persona-catalog-portability Todo

## Tasks

- [ ] 將 issue #295（primary）與 #291（duplicate）、active OpenSpec change `2026-08-04-fix-persona-catalog-portability` 與本 Todo 綁定為同一 confirmed Work Item（multi-issue）；delivery PR body closing keywords 同時涵蓋 `Closes #295` 與 `Closes #291`。
- [ ] coordinator 派工 copilot（gpt-5.4）以 TDD 完成：先寫 RED 測試（`docs/superpowers/plans/fix-persona-catalog-portability-v2.md` Section 1），再實作到 GREEN。
- [ ] ForeignReview（claude/sonnet）review 通過；operator 驗收核可。
- [ ] `changelog.d/fix-persona-catalog-portability.md` fragment 已新增且已 commit（R-09 硬性 gate）；`CHANGELOG.md [Unreleased]` 有對應 entry。
- [ ] 帶 PR 上下文執行 `policy_check`（`--pr-title`／`--pr-body`／`--pr-labels`／`--pr-base-ref`／`--pr-head-ref`）確認 fail: 0；全套 pytest 通過。

## 附註（dogfood 審計）

- 2026-08-04：首次 claim（run `workflow-e351e829c925c40f400d`）因 secondary planner
  身分缺 planning capability 的環境性 stall 而 abandon 釋放；身分矩陣已補
  agy planning＋live_probe。本次 authority 補綁 design／plan 路徑連結後重新 claim。
