---
status: accepted
work_item: docs-archived-spec-purpose
---

## Goals

修正 `openspec archive` 產生的 capability specs 含 `Purpose: TBD` stub，要麼在 archive 時要求填入 Purpose，要麼批次更新既有 specs。

## Why

`#158` 回報：`openspec archive` 產生的 capability specs 在 `openspec/specs/*/spec.md` 含 `Purpose: TBD - created by archiving change <name>. Update Purpose after archive.`。所有 archived specs 都有此 stub。需修正 archive 命令或批次更新既有 specs。

受影響 specs：persona-workflow-orchestration、porcelain-service-lifecycle、release-engineering-pipeline、porcelain-guided-bootstrap、porcelain-run-recover-verbs、porcelain-inspect-surface、unified-work-read-model、governed-delivery-closure、cli-version-reporting。

## What Changes

- `paulsha_cortex/cli/openspec*.py`（若修正 archive 命令）：archive 時 prompt/require Purpose，或從 change proposal 推導 Purpose。
- `openspec/specs/**`：批次更新既有 specs 的 `Purpose: TBD` 為適當文字。

## Capabilities

### Modified Capabilities

- `openspec-archive`：archive 產生的 spec 不再含 `Purpose: TBD` stub；既有 specs 批次更新。