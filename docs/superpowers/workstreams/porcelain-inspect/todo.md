---
status: accepted
work_item: porcelain-inspect
---

# porcelain-inspect Todo

## Tasks

- [ ] 將 issue #89、active OpenSpec change `porcelain-inspect` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 copilot（gpt-5.4）完成 `_runtime_probe` 共用模組 + `inspect` 六子命令 + 註冊表登記（TDD）。
- [ ] ForeignReview（claude/sonnet）、自動 push/PR、bot review、merge 與 archive 閉合。
- [ ] pipx 重裝後 `cortex inspect status/job/ready/work/doctor/service` 與 `--json` 全數可用，且與底層 `status`/`jobs`/`stat`/`list`/`work show`/`doctor` 輸出內容一致；`inspect service` 能在殭屍 venv 情境下正確標示。
