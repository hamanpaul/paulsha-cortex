---
status: accepted
work_item: docs-archived-spec-purpose
---

# Tasks

- [x] [RED] `tests/test_openspec_archive_purpose.py`：`openspec archive` 產生的 spec.md 不含 `Purpose: TBD`（含從 change proposal 推導或 prompt 的 Purpose 文字）。
- [x] [實作] `paulsha_cortex/cli/openspec*.py`：archive 時從 change proposal 推導 Purpose（如取 `## Goals` 段首段），或 prompt 使用者輸入。
- [x] [實作] 批次更新既有 specs 的 `Purpose: TBD`：
  - persona-workflow-orchestration
  - porcelain-service-lifecycle
  - release-engineering-pipeline
  - porcelain-guided-bootstrap
  - porcelain-run-recover-verbs
  - porcelain-inspect-surface
  - unified-work-read-model
  - governed-delivery-closure
  - cli-version-reporting
- [x] [同步與驗證] `changelog.d/docs-archived-spec-purpose.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#158` 條目。
- [x] [同步與驗證] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [x] [同步與驗證] 勾選本 tasks.md 對應項並以 conventional commit 提交。
