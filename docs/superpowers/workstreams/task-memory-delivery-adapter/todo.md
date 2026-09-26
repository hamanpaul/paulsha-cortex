---
status: accepted
work_item: task-memory-delivery-adapter
---

# Task memory delivery adapter Todo

## Scope

Owner：Cortex #857；dependency：Hippo #146。Cortex 只做 generic host adapter/validation，不改 Hippo core、Cortex global routing 或任何 project special case。

## Tasks

- [x] accepted spec/design/plan、此 Todo 與 `.cortex/work-items.yaml` registration 綁定同一 work item；自動化測試確認唯一 #857 registration、Hippo #146 dependency 與可讀文件 refs。
- [x] task envelope 與 capability matrix consumer；缺 identity、跨 scope、unsupported schema、capability mismatch、manifest/hash mismatch 均 fail closed。
- [x] inline/context-delivered、readonly snapshot-ready/read、manifest-bound note-fetch、ineligible/read-failed receipt 與 retry idempotency focused tests；content-returned 僅在實際讀取後出現。
- [x] Manager receipt ingress 驗證 persisted WorkflowRun/Job routing、事件前序與 applied artifact 對該 Job worktree 的 no-follow SHA，再寫入 0700/0600 append-only sidecar；worker 不直寫 Hippo ledger，legacy strict KPI 分離。
- [x] 每條支援 path 的 deterministic fixture 有 5 次成功與 5 次負例，涵蓋兩個 repository、build/verify、permission denial、cross-scope 與 relay/舊輸出 blocker；fixture-only eligible success 為 5/5，不能代替 live gate。
- [ ] 在隔離 installed/live provider 上各跑至少 5 次成功與負例，機械確認 eligible authorized retrieval rate ≥95%，再決定是否開始 task-level utility trial。
- [ ] 正式 Manager dispatch/provider receipt hook、full suite、PR-context policy/CI/review/merge 與 live read-model evidence；任何 blocker 保留 needs_human，不以 issue/plan/open PR 冒稱產品完成。

## 本地交付狀態（2026-09-26）

已完成 Cortex 端 public Hippo payload v1 consumer、task-scoped delivery、Manager receipt sidecar、read-only `cortex work show --task-memory` projection 與離線 fixture。Hippo #146 已發布的 helper 提供 envelope builder/validator，但沒有 production retrieval provider；adapter 所需的 candidate hash/version/manifest 與讀取 callback 因此以 optional v1 extension fixture 驗證，缺少 extension/provider 時 fail closed。離線 5/5 不代表 installed/live ≥95% gate 已過；現有 dispatch 不因本次修改改變。
