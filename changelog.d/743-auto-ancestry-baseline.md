# 743-auto-ancestry-baseline

- **`#743` auto 路徑的 ancestry baseline 改由採信端同一條導出供給——中段 build 卡
  不再每張都要人工 `regenerate-gates`。** #738 首版讓 `ensure_gate_ledger` 自行從
  `job["dispatch_head"]` 導 baseline，但那是 run 層級、claim 時凍結、後續 build 卡
  逐字繼承首張卡的值，不是這張卡 clone 的 handoff base；採信端的真導出是
  `run.candidate_head or dispatch_head`。兩端各算一份（#521／#722 修過的同型），
  中段卡的 ledger 量在錯的基線上、被「baseline 不符視同缺席」守衛正確拒絕。修法：
  `_run_gate_execution_identity` 經 registry 取 `run.candidate_head`（合法 sha 時）
  傳給 `ensure_gate_ledger` 新增的 `ancestry_baseline` 參數；未給時維持 job 導出
  （首張卡兩者同值）。實機（run `workflow-85114100` subagent-build-17）逐字命中：
  ledger 記 `59a7a9b`（stale dispatch_head）、採信端要 `b3b9aedc`（candidate_head）。
