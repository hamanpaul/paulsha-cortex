# daemon-tick-clock-not-idle

- **#819 daemon periodic tick 時鐘與 idle 設定**：
  - 修正 `manager_daemon.run_loop` periodic lane 在 `dispatch_skipped="not-idle"` 時未推進時鐘導致每個 poll cycle hot loop 的問題；跳過時推進 `last_tick_monotonic`，且保持 `daemon.idle=False`、不增加錯誤計數或觸發熔斷。
  - periodic tick runner 的 `max_load` 預設由固定 1.0 改為 CPU-aware 計算（`max(1.0, (os.cpu_count() or 1) * 0.5)`）；若需保留既有門檻，可顯式設定 `PSC_MANAGER_MAX_LOAD=1.0`。
  - daemon 新增 CLI 參數 `--max-load` 與環境變數 `PSC_MANAGER_MAX_LOAD`；CLI 顯式非法值（NaN／Inf／零／負數／非數值）由 argparse 報錯（exit 2），env 非法值則安全回落至 CPU-aware 預設。
  - 新增 `PSC_MANAGER_REQUIRE_IDLE`（支援 0/false/off/no 關閉）與 `--no-require-idle` CLI 旗標；manual tick request lane 仍維持 1.0 預設與既有語意不變。
