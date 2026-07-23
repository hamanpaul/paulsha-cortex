---
status: accepted
work_item: porcelain-service
---

# porcelain-service Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_porcelain_service.py`（tmp systemd unit 檔 + mock `systemctl`/`journalctl` 呼叫 + fallback pid 檔 fixtures）：`install` 呼叫既有 installer（mock）、`start`/`stop`/`restart` service+timer 成對驗證、`status` 三模式輸出與版本/exec path drift、`logs` 兩種來源、`uninstall`/`--purge`、`--json` schema `cortex-porcelain/service/v1`；先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/porcelain/service.py`：`service install/start/stop/restart/status/logs/uninstall` 七子命令；`status` 復用 `_runtime_probe.probe_service_runtime()`。
- [ ] `paulsha_cortex/porcelain/__init__.py`：`_FAMILY_MODULES` 加入 service 模組（僅此一行）。

### 3. 同步與驗證

- [ ] README 的 CLI 命令面補 `service` 家族一段（R-16）。
- [ ] 新增 `changelog.d/porcelain-service.md` fragment，`CHANGELOG.md [Unreleased]` `### Added` 加入含 `porcelain-service` 字樣的條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 完成後勾選 `openspec/changes/porcelain-service/tasks.md` 對應項並以 conventional commit 提交（不得改動本 plan 檔）。
