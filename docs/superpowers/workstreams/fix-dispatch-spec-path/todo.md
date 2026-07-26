---
status: accepted
work_item: fix-dispatch-spec-path
---

# fix-dispatch-spec-path Todo

## Tasks

- [ ] 將 issue #98、active OpenSpec change `2026-07-26-fix-dispatch-spec-path` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex 完成 #98 修復（TDD）：`_infer_repo_root` spec 在 repo 外時回傳 `paths.repo_root()`。
- [ ] ForeignReview 通過；operator 對抗 review 核可。
- [ ] spec 位於 repo 外 + `PSC_REPO_ROOT` 已設定時 dispatch pinning 不再失敗（驗證根因修復）。