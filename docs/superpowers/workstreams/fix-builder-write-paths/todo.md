---
status: accepted
work_item: fix-builder-write-paths
---

# fix-builder-write-paths Todo

## Tasks

- [ ] 將 issue #118、active OpenSpec change `2026-07-26-fix-builder-write-paths` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex 完成 #118 修復（TDD）：builder `write_paths` 改為動態 `["**"]`，跨 repo dispatch 不拒絕寫入。
- [ ] ForeignReview 通過；operator 對抗 review 核可。
- [ ] 跨 repo dispatch（如 paulshaclaw）時 builder 可成功寫入目標 repo 路徑（驗證根因修復）。