---
status: accepted
work_item: dispatch-reliability-batch
---

# dispatch-reliability-batch Todo

## Tasks

- [ ] 將 issue #152、#100、#99、active OpenSpec change `2026-07-25-dispatch-reliability` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex（gpt-5.3-codex-spark）完成三項修復（TDD）：mutation request 分級 timeout + pending 語意（#152）、DispatchReadyError per-slice 摘要 + tick response 透傳 + manager.log ISO-8601 前綴（#100）、git runner `git -C` + installer WorkingDirectory（#99）。
- [ ] ForeignReview（claude/sonnet）通過；operator（Copilot CLI session）對抗 review 核可。
- [ ] pipx 重裝後從 systemd 啟動之 manager daemon 可成功 fanout（驗證 #99 根因修復）；`cortex run tick --wait` 不再因 5s 誤報失敗（#152）；dispatch 失敗時 tick response `errors` 含底層例外摘要且 manager.log 含時間戳（#100）。