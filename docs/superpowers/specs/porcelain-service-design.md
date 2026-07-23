---
status: accepted
work_item: porcelain-service
---

# porcelain-service Design

## Goals

把 `systemctl`/`journalctl` 的操作細節收斂成 `cortex service` 一組直覺命令，同時修正「只停 service、timer 仍週期喚醒」與「daemon 跑過期碼無人察覺」兩個 dogfood 已實證的維運風險。

## Decisions

- **復用 B3 探測模組**：`service status` 直接 import `paulsha_cortex/porcelain/_runtime_probe.py` 的 `probe_service_runtime()`，不重複實作模式/版本偵測；本批次落地前提為 B3 已登記該共用模組，若順序反轉則本批次補上並由 B3 事後復用。
- **service+timer 固定成對**：`start`/`stop`/`restart` 內部固定操作 `<instance>.service` 與 `<instance>.timer` 兩個 unit name，任一操作不完整（例如 timer 存在但 service 不存在）即視為異常並回報，不視為部分成功。
- **fallback 行程管理**：無 systemd 時，`service` 改以 pid 檔或 process 搜尋管理 daemon 行程；`install` 在此模式下明確印出「systemd 不可用，改用 fallback，缺少的能力：`--follow` 日誌串流」等具體差異。
- **危險操作邊界**：`uninstall` 預設只停用/移除 unit 檔，不動 runtime env 與既有日誌；`--purge` 才清除，避免誤刪運維留存證據。
