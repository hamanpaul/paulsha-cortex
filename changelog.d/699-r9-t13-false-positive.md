### Fixed

- R9 族 1 的 **T1.3 斷言測錯東西**，已改（#699）：舊寫法 `cortex work ship --help`
  只是 argparse 印字就結束，而 job **必須**能執行那個 venv——`cortex-job-shim` 本身就是
  `/opt/cortex/venv/bin/python3 -m …job_shim`。0818 實測 `builder` 與 `reviewer-planner`
  **兩個 subject 都**回 `SUCCEEDED (FAIL)`，正說明它與身分無關。改為斷言 operator CLI
  **做得到事**（以 job 身分跑真正的 work-action 會停在「manager daemon 未就緒」，
  因為 control socket 與 control queue 都已被拒），並把「binary 可執行」降為 `d()`
  設計性讀取。

### Added

- runbook 第 8 步記錄 **0818 兩個 subject 的 R9 族 1–4 實測結果**，並標明 T3.9 對
  `reviewer-planner` 的失敗是**已知且已追蹤**（#698，`#685` 的 U-9 代價實體化），
  避免下一個 operator 把它讀成新事故。附一條可重用的判準：**同一條在所有 subject 上
  同時失敗時，先懷疑斷言，不要先懷疑邊界。**
