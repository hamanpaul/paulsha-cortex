---
status: accepted
work_item: fix-slice-failed-deadend
---

# fix-slice-failed-deadend Todo

## Tasks

- [ ] 將 issue #153、active OpenSpec change `2026-07-26-fix-slice-failed-deadend` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex 完成 #153 修復（TDD）：failed slice 恢復路徑 + registry 磁碟重載。
- [ ] ForeignReview 通過；operator 對抗 review 核可。
- [ ] failed slice 可恢復（不再 dead-end）；registry 可從磁碟重載（驗證根因修復）。