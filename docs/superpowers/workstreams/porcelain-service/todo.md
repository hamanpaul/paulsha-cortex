---
status: accepted
work_item: porcelain-service
---

# porcelain-service Todo

## Tasks

- [ ] 將 issue #90、active OpenSpec change `porcelain-service` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 copilot（gpt-5.4）完成 `service` 七子命令 + 註冊表登記（TDD）。
- [ ] ForeignReview（claude/sonnet）、自動 push/PR、bot review、merge 與 archive 閉合。
- [ ] pipx 重裝後 `cortex service install/start/stop/restart/status/logs/uninstall` 三模式（systemd/fallback/未安裝）行為正確；`stop` 後以 `systemctl --user list-timers` 確認 timer 一併停止。
