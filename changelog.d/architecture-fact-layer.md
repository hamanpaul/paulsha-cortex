---
type: docs
---
- 新增 `docs/architecture/facts.json` 作為 revision-pinned 架構事實層，並提供 `architecture.json` 與互動式 `architecture.html` projection；事實層以 accepted ADR 與現行 source contract 為 evidence，固定 Monitor read model、Manager single-writer、WorkflowRun/gate 與 remote completion closure 的權威邊界，避免不同 Agent 各自重建互相矛盾的 Cortex 拓撲。
