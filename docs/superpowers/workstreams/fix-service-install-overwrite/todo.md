---
status: accepted
work_item: fix-service-install-overwrite
---

# fix-service-install-overwrite Todo

## Tasks

- [ ] 將 issue #148、active OpenSpec change `2026-07-26-fix-service-install-overwrite` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex 完成 #148 修復（TDD）：installer idempotent guard 防止不同 venv 覆寫。
- [ ] ForeignReview 通過；operator 對抗 review 核可。
- [ ] 既有有效 config + 不同 venv 呼叫 `install service` 不覆寫（驗證根因修復）。