- **#716：`agent-loop-probe` qualification harness 現在走 `SubprocessLauncher.launch()` 的
  真實 template-dispatch path**，以 `bash -c <wrapper>` job spec 啟動 `codex exec`，
  並在讀取落檔 unit 時先用 `unit_replica_properties()` fail-closed 驗證
  `IPAddressDeny=any` + `Environment=HTTPS_PROXY=` 的 egress pair，再把 repository
  command / child process / forbidden path / forbidden host / no-unsafe-fallback 與
  executor/model、unit hash、candidate SHA、artifact hash、child tree、
  exit reason、SKIP／fallback／quota／model mismatch evidence 綁進 probe contract。
