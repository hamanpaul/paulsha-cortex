---
status: accepted
work_item: porcelain-service
---

# Design

## Decisions

- **service+timer 固定成對**：`start`/`stop`/`restart` 內部固定操作 `<instance>.service` 與 `<instance>.timer`，缺一即回報異常。
- **復用運行時探測**：`status` import B3 的 `_runtime_probe.probe_service_runtime()`，不重複實作版本/exec path drift 偵測。
- **三模式訊息明確化**：systemd/fallback/未安裝三種模式在每個子命令輸出中明確標示，不回報假成功。
- **`--purge` 才清 runtime env**：`uninstall` 預設保留 runtime env 與日誌，降低誤刪風險。
