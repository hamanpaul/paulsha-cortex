- **#716：新增會實際執行的 `agent-loop-probe` qualification harness 與 CLI**，重用真實
  `codex exec` template dispatch seam（`build_codex_argv` /
  `prepare_systemd_template` / `build_job_env` / `build_job_spec` /
  `write_job_spec` / `systemctl start --wait`），並把 repository command /
  child process / forbidden path / forbidden host / no-unsafe-fallback 與
  executor/model、unit hash、candidate SHA、artifact hash、child tree、
  exit reason、SKIP／fallback／quota／model mismatch fail-closed evidence
  釘進 probe contract。
