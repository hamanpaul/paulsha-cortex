---
status: accepted
work_item: porcelain-run-recover
---

# porcelain-run-recover Todo

## Tasks

- [ ] 將 issue #92、active OpenSpec change `porcelain-run-recover` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 copilot（gpt-5.4）完成 `run`／`recover` 兩家族 + 註冊表登記（TDD）。
- [ ] ForeignReview（claude/sonnet）、自動 push/PR、bot review、merge 與 archive 閉合。
- [ ] pipx 重裝後 `cortex run tick/fanout/complete/work` 與 `cortex recover slice/work/brokers/service` 全數與對應既有低階命令行為等價，且都能輸出 request_id 供 `cortex request` 家族追蹤。
