---
status: accepted
work_item: reclaim-pr-inheritance
---

# reclaim-pr-inheritance Todo

## Tasks

- [ ] 將 issue #175、active OpenSpec change `2026-07-26-reclaim-pr-inheritance` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex（gpt-5.3-codex-spark）完成 #175 修復（TDD）：新 claim run `pr_refs=()`（不繼承 mapped_prs）+ terminal provider 跳過 closed-unmerged PR 的 closing 關聯。
- [ ] ForeignReview（agy/gemini-3.6-flash）adversarial-review 通過；operator（Copilot CLI session）驗收核可。
- [ ] re-claim 後新 run 不繼承舊 PR、自行開新 PR 交付；closed-unmerged PR 不再貢獻 mapped_prs（驗證 #175）。