---
type: docs
scope: refine
---
#946／#948 進件：新增 work item `executor-backoff-reconcile-replay`（#946：admission 對 reconciliation PENDING 以
`record_backoff` 重播 missing terminal 補 ack、unknown 診斷帶 pending／replayed 計數、`_poll_workflow_job` 接 #830
decision 契約）與 `copilot-review-adopt-existing`（#948：ship 段 request 前採信既有 exact-HEAD Copilot review、
`ReviewLoop` 以採信時刻計 timeout、`copilot-*` stop 的 `next_actions` 補 `review-attest`）各自的 accepted 三件套；
兩者實算 sizing 分別 5／6（Yellow）。docs-only。
