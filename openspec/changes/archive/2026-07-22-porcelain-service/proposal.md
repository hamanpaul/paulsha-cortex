---
status: accepted
work_item: porcelain-service
---

## Goals

讓一般使用者管理 cortex 背景服務時不必記 `systemctl`/`journalctl` 細節，並修正「service 與 timer 操作不同步」「運行版本不可見」兩個已實證的維運缺口（issue #90）。

## Why

deep research 與 dogfood canary 指出：F2 daemon env 與 CLI 旗標雙軌造成 executor/model 設定不同步風險；F3 舊 daemon 長期跑過期碼而無人察覺；且 systemd 的 service/timer 若只操作其一，行為會出現「以為停了、其實還在跑」的落差。B4 以任務導向的 `service` 命令收斂這些操作，並顯性化運行版本。

## What Changes

- 新增 `paulsha_cortex/porcelain/service.py`：`cortex service install/start/stop/restart/status/logs/uninstall`（唯讀查詢 + 生命週期 mutation、`--json`、exit code 契約）。
- `service status` 復用 B3 的 `_runtime_probe.probe_service_runtime()`。
- `porcelain._FAMILY_MODULES` 登記 service 模組。
- README 命令面補 service 家族段（R-16）。

## Capabilities

### New Capabilities

- `porcelain-service-lifecycle`: cortex 背景服務的生命週期契約——install/start/stop/restart/status/logs/uninstall，明確區分 systemd/fallback/未安裝三模式並顯示運行版本。
