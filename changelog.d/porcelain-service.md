### Added

- **porcelain-service service 管理 CLI**：新增 `cortex service install/start/stop/restart/status/logs/uninstall` 家族、versioned `cortex-porcelain/service/v1` JSON 輸出，以及 systemd/fallback runtime 與 log source 切換。

### Fixed

- **porcelain-service lifecycle guardrails**：所有 `service` 子命令現在共用 instance 驗證，`logs --follow` 改為 systemd-only 串流且 fallback 明確拒絕，systemctl/journalctl 失敗時 `--json` 也會回傳一致的 `cortex-porcelain/service/v1` 錯誤 envelope。
