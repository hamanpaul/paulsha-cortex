---
status: accepted
work_item: fix-mutation-request-timeout
---

# fix-mutation-request-timeout Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_fix_mutation_request_timeout.py`：
  - `_submit_mutation_request` 對 `fanout`/`tick` 取 ≥60s、`complete`/`work`/`run` ≥30s、其他 5s（mock `poll_done_fn` 控制逾時，斷言傳入的 timeout 值）。
  - 逾時路徑回傳 pending 結果含 req_id 與追蹤指引訊息、exit code `EXIT_SUBMITTED_PENDING` 區別於失敗；成功路徑不變。
  - 先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/coordinator/cli.py`：`_REQUEST_TIMEOUTS` 表 + `_submit_mutation_request` 依 req_type 取 timeout；逾時 pending 路徑 + `EXIT_SUBMITTED_PENDING` 常數。

### 3. 同步與驗證

- [ ] `changelog.d/fix-mutation-request-timeout.md`；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#152` 字樣條目。
- [ ] README 對應段同步（R-18：mutation request 分級 timeout 與 pending 語意）。
- [ ] `python3 -m pytest tests/ -q` 全綠；`policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-25-fix-mutation-request-timeout/tasks.md` 並以 conventional commit 提交。