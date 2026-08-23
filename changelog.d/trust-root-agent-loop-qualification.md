- **#716（RED）：新增 real agent-loop qualification contract tests**，要求新的
  `agent-loop-probe` 走真實 `codex exec` template dispatch、覆蓋 repository
  command / child process / forbidden path / forbidden host /
  no-unsafe-fallback，並把 SKIP／fallback／quota／model mismatch 鎖成
  fail-closed。
