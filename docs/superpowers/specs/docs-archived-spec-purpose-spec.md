---
status: accepted
work_item: docs-archived-spec-purpose
---

# docs-archived-spec-purpose Specification

`#158`：修正 `openspec archive` 產生 `Purpose: TBD` stub，並批次更新既有 specs。

## Requirements

### R1 archive 不再產生 TBD

`openspec archive` 產生的 `spec.md` MUST NOT 含 `Purpose: TBD`。MUST 從 change proposal 推導 Purpose 或 prompt 使用者輸入。

### R2 既有 specs 批次更新

以下 9 個 specs 的 `Purpose: TBD` 行 MUST 更新為適當文字：
- persona-workflow-orchestration
- porcelain-service-lifecycle
- release-engineering-pipeline
- porcelain-guided-bootstrap
- porcelain-run-recover-verbs
- porcelain-inspect-surface
- unified-work-read-model
- governed-delivery-closure
- cli-version-reporting

### R3 限制

- TDD；archive 命令測試 + 既有 specs 更新。
- `python3 -m policy_check --repo .` 0 fail。