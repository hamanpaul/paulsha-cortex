---
status: accepted
work_item: fix-deck-emit-frontmatter
---

# fix-deck-emit-frontmatter Todo

## Tasks

- [ ] 將 issue #101、active OpenSpec change `2026-07-26-fix-deck-emit-frontmatter` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex 完成 #101 修復（TDD）：`deck compile --emit` 產生符合 auto dispatch contract 的 frontmatter。
- [ ] 補 auto dispatch contract 文件至 docs/。
- [ ] ForeignReview 通過；operator 對抗 review 核可。
- [ ] `deck compile --emit` 產生的 frontmatter 可被 dispatch_ready 接受（驗證根因修復）。