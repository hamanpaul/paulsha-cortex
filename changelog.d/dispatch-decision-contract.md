---
type: fix
scope: coordinator
---
**Issue #830：派工結果的非 Job 決策契約——合法 needs-decomposition 不再在消費端炸 `KeyError: 'job_id'`**

`_dispatch_workflow_card` 對 plan 完成且 sizing_band=red 的 run 合法回傳
`{run_id, current_phase, reason: "needs-decomposition"}`（#223）而不建立 Job；daemon 的
workflow-action start 與 work-action（start／intake／resume／retry-*）消費端、manager
`resume_workflow_run` 的派工後段與兩處 provider retry 過去只驗 `is not None` 就讀 `["job_id"]`，
start request 直接 KeyError，periodic resume 再撞同型錯誤時把 needs_decomposition 改寫成
needs_human／resume-workflow-failed。新增 `manager.classify_dispatch_result` 作單一分類契約：
真 Job（registry 綁定到同一 run）、合法 decision（保留 reason）、確定性 transition（None 但 phase
已推進）、None；forged job_id／錯 run／malformed dict 一律 `ValueError` fail-closed。五個消費端全部
接上：request 回應新增 `dispatch.kind` 投影並重讀最新 run，真 Job 才帶 `job_id`；forced retry
遇決策仍走既有 needs_human 補償並附 producer reason；provider retry 遇決策原樣回傳、不計 retry。
不改 durable Job／WorkflowRun schema、sizing 門檻、reviewer independence 與 recovery CAS。
