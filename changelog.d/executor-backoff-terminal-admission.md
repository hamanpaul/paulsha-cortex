- **#928 executor backoff terminal admission**：新增 `reset_hint.py` 解析 Retry-After 與 Codex
  `Try again at ...` 文字 reset hint，workflow/slice terminal 會把 rate-limited／quota 終局寫進
  durable executor-backoff store；workflow admission 與 provider reroute 會在 runtime preflight 前
  過濾 executor×model cooldown，派出的 replacement job 會保留 `dispatch_reroute` 收據；若 store
  unreadable 或與 terminal inventory 對帳未完成，Manager 直接回 `executor-backoff-unknown`，
  不捏造 `retry_after_epoch`。slice request/tick consumer 仍由 #929 承接。
