# outcome-taxonomy-signals

- **#826 outcome taxonomy signals**：新增 `effort_not_supported`／`executable_not_found`／`launch_failed`
  到 headless provider failure taxonomy，並把 text/structured 訊號映射到 environment family。
  `Dispatcher.poll_headless_done()`、`autonomy._fail_launching_job()` 與 workflow launch exception
  現在都會把 typed `provider_outcome` 與 `launch-failed` runtime diagnostic 寫入 registry；reload 後
  `classification_from_job()` 仍能 round-trip 讀回；foreign-review launch exception 也會即時投影
  `foreign-review-provider-<outcome>` gate reason，而不是舊的 `foreign-review-launch-error:*`。
  workflow/slice consumer 會保留 structured authority 與 runtime-contract 優先序；已知 launcher／
  executor 的 shell command-not-found、可信空 `127`，以及 typed launch exception 能證明缺的是 provider executable
  （如 `copilot`／`codex`／`claude`／`agy`／`cg` 或精確 executor 名）時才會進 `executable_not_found`；
  缺 log／讀不到 log 的 `127` 維持 unknown/hint，`bash`／`sh`／`git`／`systemctl`／`systemd-run`
  這類 shared launch infrastructure 缺失則保留 `launch_failed`。`effort_not_supported`／`executable_not_found` 可在既有合格候選內有界 reroute，
  reroute 也會重跑該 card 的完整 runtime preflight，因此缺 `module:pytest` 等能力的替代候選會被跳過；
  實際重派會沿用 preflight 核可的同一個 specialized launcher／executor environment，若沒有任何
  runtime-qualified 替代候選則維持停止並等待人工；`launch_failed` 則保留原始 exception 或缺 handle
  理由，不自動 retry 或 reroute。
