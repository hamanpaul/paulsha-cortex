---
status: accepted
work_item: monitor-canonical-todo-qualification
owner_issue: 1063
parent_issue: 1054
domain_breadth: 1
state_consistency: 1
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
applicable_contract_rules:
  - R-09
  - R-16
  - R-19
  - R-22
---

# Monitor canonical Todo qualification

## Goals

為 #1063 定義可供 #1054 消費的 qualified canonical Todo source result，並在任何 override write 前守住一般 Monitor path link 的存在、安全與 scanner eligibility。

## Why

現行 RepoWorkProvider 對 canonical workstream Todo 只 glob、讀 work_item 和 revision；無法證明 issue provenance、matching work_item 或可用 Tasks。Correlation path guard 以 non-strict resolve 允許 repo 內不存在 path，work link 寫 override 前也沒有要求 target 是 existing Monitor scanner source。

## Requirements

- Canonical Todo 才能被標為 qualified；qualification 同時驗證 issue provenance、exact work_item、Tasks-v1 與同 WorkAuthority path ownership。
- Qualification result 綁定 source path/revision 並保留 Tasks validation，WorkAuthority 對 #1054 暴露 typed qualified_todos；raw mapped_todo_paths 語意不改。
- 一般 kind:path link 僅檢查現存 repo-contained safe regular file 與 Monitor scanner eligibility，保留 Todo/spec/design/plan link 用途；link 本身不聲稱 Todo qualified。
- 對 issue provenance、work_item、Tasks 或 path 不合格的輸入 fail closed；連結失敗前後 override bytes 相同。

## Capabilities

### Modified Capabilities

- unified-work-read-model: add qualified Todo source results and require existing safe Monitor-scanned source paths for kind:path links.

## Non-Goals

不實作 Manager first-Builder gate、Monitor freshness/generation、claim reconciliation、existing Candidate/PR recovery、ship、merge、deployment或 GitHub author identity verification。
