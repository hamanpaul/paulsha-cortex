---
status: accepted
work_item: multi-issue-worktree
---

# multi-issue-worktree Todo

## Tasks

- [ ] 將 issue #134、active OpenSpec change `2026-07-26-multi-issue-worktree` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex（gpt-5.3-codex-spark）完成 #134 修復（TDD）：multi-issue Work Item 進 build（canonical primary issue branch naming）+ repo-scoped builder worktree（`run.workspace_root`）。
- [ ] ForeignReview（agy/gemini-3.6-flash）adversarial-review 通過；operator（Copilot CLI session）驗收核可。
- [ ] 2-issue Work Item claim→build dispatch 成功；Manager repo ≠ WorkflowRun repo 時 worktree 建於 run repo sibling pool；單 issue 既有 lifecycle 不退化；delivery PR closing keywords 涵蓋全部 mapped issues（驗證 #134）。