### Fixed

- **#1223 brainstorm／planning 派工納入 quota admission**：planner 的模型呼叫（questioner、secondary、integrator）走與 workflow card 相同的准入：shadow 只記錄，enforce 下不可行時不呼叫模型、run 標 `quota-admission-insufficient` 並寫可重試 wait receipt（投影 `resume`）；可行時同步預留、以帶 PID／start ticks 的 synthetic job 綁定，呼叫結束即收斂，Manager 重啟後可對帳；可解析的終局用量記入 own-job ledger。能力探測不納管；planning identity 的 quota fallback 另由後續票處理（#1223）。
