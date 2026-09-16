- **#805 deployment-canary credential wiring**：依 install plan 的
  `required_credentials` 條件式匯入獨立的 builder/AGY credential，並讓 optional secret
  進入 canary redaction scan；release packaged config 與既有 builder/codex default 不變。
