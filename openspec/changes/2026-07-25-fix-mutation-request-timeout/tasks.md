---
status: accepted
work_item: fix-mutation-request-timeout
---

# Tasks

- [ ] 1.1 RED：`tests/test_fix_mutation_request_timeout.py` 涵蓋分級 timeout 取用、逾時 pending 路徑含 req_id 與追蹤指引、exit code 區別。
- [ ] 1.2 `coordinator/cli.py` `_REQUEST_TIMEOUTS` 表 + `_submit_mutation_request` 分級 timeout + pending 路徑 + `EXIT_SUBMITTED_PENDING`（#152）。
- [ ] 1.3 `changelog.d/fix-mutation-request-timeout.md` 與 `CHANGELOG.md [Unreleased]` `### Fixed` 同步（#152）；README 對應段同步（R-18）。
- [ ] 1.4 focused regression 全綠、`policy_check --repo .` 0 fail、`git diff --check` 乾淨。