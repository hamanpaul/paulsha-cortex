---
status: accepted
work_item: test-r21-regex-resilience
---

# test-r21-regex-resilience Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_onboarding_docs_contract.py`：新增測試案例：
  - CRLF bash fence 被 `BASH_FENCE_RE` 捕獲。
  - fence marker 後 trailing whitespace 的 code block 被捕獲。
  - Windows `C:\Users\...` 被 `PERSONAL_ABSOLUTE_PATH_RE` 捕獲。
  - 先確認 RED。

### 2. 實作

- [ ] `tests/test_onboarding_docs_contract.py`：
  - `BASH_FENCE_RE` 改為容忍 `\r?\n` + `[ \t]*` + `re.DOTALL`。
  - `PERSONAL_ABSOLUTE_PATH_RE` 新增 Windows drive-letter path。

### 3. 同步與驗證

- [ ] `changelog.d/test-r21-regex-resilience.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#169` 條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-26-test-r21-regex-resilience/tasks.md` 對應項並以 conventional commit 提交。