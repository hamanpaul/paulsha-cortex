---
status: accepted
work_item: test-r21-regex-resilience
---

# Tasks

- [x] [RED] `tests/test_onboarding_docs_contract.py`：新增測試案例：
  - [x] CRLF bash fence（`\r\n`）的 code block 被 `BASH_FENCE_RE` 捕獲（非跳過）。
  - [x] fence marker 後有 trailing whitespace（如 ` ```bash `）的 code block 被捕獲。
  - [x] Windows `C:\Users\...` absolute path 被 `PERSONAL_ABSOLUTE_PATH_RE` 捕獲。
  - [x] 先確認 RED（既有 regex 在這些案例上失敗）。
- [x] [實作] `tests/test_onboarding_docs_contract.py`：
  - `BASH_FENCE_RE` 改為 `re.compile(r"```bash[ \t]*\r?\n(.*?)```", re.DOTALL)`。
  - `PERSONAL_ABSOLUTE_PATH_RE` 新增 Windows drive-letter pattern（如 `[A-Za-z]:\\Users\\`）。
- [x] [同步與驗證] `changelog.d/test-r21-regex-resilience.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#169` 條目。
- [x] [同步與驗證] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [x] [同步與驗證] 勾選本 tasks.md 對應項並以 conventional commit 提交。
