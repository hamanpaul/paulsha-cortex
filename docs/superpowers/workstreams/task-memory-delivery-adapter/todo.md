---
status: accepted
work_item: task-memory-delivery-adapter
---

# Task memory delivery adapter Todo

## Scope

Owner：Cortex #857；dependency：Hippo #146。Cortex 只做 generic host adapter/validation，不改 Hippo core、Cortex global routing 或任何 project special case。

## Tasks

- [ ] accepted spec/design/plan、此 Todo 與 `.cortex/work-items.yaml` registration 一致且可由 isolated worker 讀取。
- [ ] task envelope 與 capability matrix contract fixture；缺 identity、跨 scope、unsupported schema、manifest mismatch 均 fail closed。
- [ ] inline/context-delivered、readonly snapshot/materialized ready/offer、僅由 tool/provider 成功返回內容產生的 content-returned、manifest-bound note-fetch、ineligible/read-failed receipt 與 retry idempotency focused tests。
- [ ] Manager single-writer sidecar/receipt projection；worker 不直寫 Hippo ledger，legacy strict KPI 不變。
- [ ] 每條支援 path 5 次 canary、Cortex denied negative control、不同 repository/task kind；eligible authorized retrieval success rate ≥95%。
- [ ] 完整 tests/policy/CI/review/merge/read-model evidence；任何 blocker 保留 needs_human，不以 issue/plan/open PR 冒稱產品完成。
