# monitor 設定檔載入規則

`paulsha_cortex.monitor.config.load_config` 提供兩種載入模式：

- `config_path` 明確傳入：
  - 只載入指定的 `project-cortex.yaml`。
  - 不會合併任何 ambient 的 `project-hippo.yaml`（完全顯式）。
- `config_path` 未傳入：
  - 依序使用 `PSC_MONITOR_CONFIG`、`PAULSHACLAW_CONFIG`、標準 `project-cortex.yaml` 或 legacy `paulshaclaw.yaml`。
  - 合併對應路徑下可見的 `project-hippo.yaml`，以提供 ambient projects。

這個行為是為了讓「給定 explicit config」與「主機預設 ambient 設定」之間維持明確邊界：

- 在測試與腳本中指定 `config_path`，可避免意外引入外部環境專案清單。
- 未指定時仍保留既有監控預設行為，會套用 ambient `project-hippo.yaml`（若存在）。
