---
status: accepted
work_item: fix-dispatch-exception-detail
---

# fix-dispatch-exception-detail Todo

## Tasks

- [ ] 將 issue #100、active OpenSpec change `2026-07-25-fix-dispatch-exception-detail` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex（gpt-5.3-codex-spark）完成 #100 修復（TDD）：DispatchReadyError per-slice 摘要、tick response `errors` 透傳、manager.log ISO-8601 前綴。
- [ ] ForeignReview（claude/sonnet）通過；operator（Copilot CLI session）對抗 review 核可。
- [ ] dispatch 失敗時 tick response `errors` 含底層例外摘要且 manager.log 含時間戳（驗證 #100）。