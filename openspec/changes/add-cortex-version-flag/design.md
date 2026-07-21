# Design

## Decisions

- **路由前處理**：`--version` 在 `main()` 子命令路由之前攔截，比照 `-h/--help` 頂層特殊處理，不落入 coordinator 透傳的必填 `cmd` 檢查。
- **版本來源**：`importlib.metadata.version("paulsha-cortex")` 為權威；`PackageNotFoundError` fallback `0.0.0+unknown`。不讀 repo `VERSION` 檔（安裝環境不存在）。
- **輸出契約**：單行 `cortex <version>`，stdout，exit 0。
- **最小範圍**：canary 紀律——只加一個旗標，其他一律不動。
