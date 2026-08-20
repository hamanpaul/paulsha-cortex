# 759-preflight-evidence-output

- **`#759` pr-preflight evidence 記錄 backend 逐字輸出（有界尾段）**——「只記
  returncode」讓每一次 preflight 失敗都要靠實機重現才能定位（0820 delivery 首走
  連三環皆如此：venv 缺 pytest、manager env 的 env-red、MDWE×node）。policy 與
  ci_parity 各補 `stdout_tail`／`stderr_tail`（各 2000 字、保尾段——short summary
  在尾巴，與 gate ledger detail 同向）。
