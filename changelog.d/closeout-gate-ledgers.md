### Fixed

- **#716 canary 結案只對 build job 要求 gate ledger**：driver 原本對所有非 ship phase 的 job 都要求 Manager 權威 gate ledger。但 Manager 只在 build phase 產生 ledger（`GATE_LEDGER_REQUIRED_PHASES`）；verify／review 的 reviewer 在唯讀沙箱中不跑 gate，模板模式下根本沒有 ledger。現在 driver 以 `GATE_LEDGER_PHASES` 逐字鏡射 manager 的常數，並由測試釘住兩者一致（#716）。
