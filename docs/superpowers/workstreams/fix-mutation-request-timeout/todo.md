---
status: accepted
work_item: fix-mutation-request-timeout
---

# fix-mutation-request-timeout Todo

## Tasks

- [ ] 將 issue #152、active OpenSpec change `2026-07-25-fix-mutation-request-timeout` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex（gpt-5.3-codex-spark）完成 #152 修復（TDD）：mutation request 分級 timeout + pending 語意 + exit code 區別。
- [ ] ForeignReview（claude/sonnet）通過；operator（Copilot CLI session）對抗 review 核可。
- [ ] `cortex run tick --wait` 不再因 5s 誤報失敗（驗證 #152）；逾時訊息含 req_id 與追蹤指引。