---
status: accepted
work_item: design-task-type-taxonomy-v2
---

# design-task-type-taxonomy Todo

## Tasks

- [x] 將 issue #139、active OpenSpec change `2026-08-04-design-task-type-taxonomy` 與本 Todo 綁定為同一 confirmed Work Item。（`.cortex/work-items.yaml` 已有 `design-task-type-taxonomy-v2` 條目連結本 todo 與 spec。）
- [ ] coordinator 派工 copilot（gpt-5.4）以 TDD 完成 #139：先寫 RED 測試，再實作到 GREEN。（實際交付路徑與此不同：契約骨架與多數測試已隨 #202 的 PR #335 由該票的 TDD 循環提前落地；本輪由 Claude（Opus）在既有 worktree 直接補齊 plan 缺漏的四項測試並核對驗收面，非 coordinator 派工 copilot 走出的獨立 RED→GREEN 循環，故此項不勾。）
- [ ] ForeignReview（claude（sonnet））review 通過；operator 驗收核可。
- [x] `changelog.d/design-task-type-taxonomy-v2.md` fragment 已新增且已 commit（R-09 硬性 gate）；`CHANGELOG.md [Unreleased]` 有對應 entry。
- [x] 帶 PR 上下文執行 `policy_check`（`--pr-title`／`--pr-body`／`--pr-labels`／`--pr-base-ref`／`--pr-head-ref`）確認 fail: 0；全套 pytest 通過。
