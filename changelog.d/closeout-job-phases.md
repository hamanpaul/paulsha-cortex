### Fixed

- **#716 canary 結案檢查的 job phase 鏈**：driver 原本要求 plan、build、verify、review、ship 五個 phase 都要有 registry job。但 plan（writing-plans-light）與 ship（archive／policy-commit）由 Manager 執行，define 的 planner 走 planning runtime，這些都不會產生 job，canary 在 probe 的 PR 已合入、issue 已關閉之後，仍會卡在這一關。現在改為只要求 builder／reviewer 步驟的 phase 有 job，錯誤訊息會列出缺少與實際觀察到的 phase（#716）。
