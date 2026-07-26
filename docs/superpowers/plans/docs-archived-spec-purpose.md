---
status: accepted
work_item: docs-archived-spec-purpose
---

# docs-archived-spec-purpose Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_openspec_archive_purpose.py`：
  - `openspec archive` 產生的 spec.md 不含 `Purpose: TBD`。
  - 含從 change proposal 推導的 Purpose 文字。
  - 先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/cli/openspec*.py`：archive 時從 change proposal Goals 段推導 Purpose。
- [ ] 批次更新 9 個既有 specs 的 `Purpose: TBD` 行。

### 3. 同步與驗證

- [ ] `changelog.d/docs-archived-spec-purpose.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#158` 條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-26-docs-archived-spec-purpose/tasks.md` 對應項並以 conventional commit 提交。