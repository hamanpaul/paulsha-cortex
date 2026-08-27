- Release preflight 改為逐一驗證 exact PR head 最新的 Tests、Persona Scope、Policy Check 與
  RC qualification workflow run；必要 gate 缺失、執行中或失敗仍會 fail closed，但事故留下的
  非必要第三方歷史 check 不再永久阻擋 release。
