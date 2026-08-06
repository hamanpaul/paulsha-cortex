### Fixed
- **Issue #313：verify phase 移出 gate ledger 必要集**：verification 卡以
  review-only 沙箱啟動，`launcher._should_run_gates` 依設計不讓唯讀 reviewer
  跑 gate（也不寫 ledger）；`GATE_LEDGER_REQUIRED_PHASES` 含 verify 使
  verification 卡的 passed terminal 一律「沒有可重驗的 gate ledger」
  fail-closed（結構性永不可過）。收斂為 `{build}`；verify 的獨立證據層是
  deterministic verification report 管線。
