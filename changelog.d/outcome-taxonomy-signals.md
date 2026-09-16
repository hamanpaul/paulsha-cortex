# outcome-taxonomy-signals

- **#826 outcome taxonomy signals**：新增 `effort_not_supported`／`executable_not_found`／`launch_failed`
  到 headless provider failure taxonomy，並把 text/structured 訊號映射到 environment family。
  `Dispatcher.poll_headless_done()`、`autonomy._fail_launching_job()` 與 workflow launch exception
  現在都會把 typed `provider_outcome` 與 `launch-failed` runtime diagnostic 寫入 registry；reload 後
  `classification_from_job()` 仍能 round-trip 讀回。workflow/slice consumer 會保留 structured authority
  與 runtime-contract 優先序，`effort_not_supported`／`executable_not_found` 可在既有合格候選內有界 reroute，
  `launch_failed` 則保留原始 exception 或缺 handle 理由，不自動 retry 或 reroute。
