---
status: accepted
work_item: porcelain-service
---

# Tasks

- [ ] 1.1 RED：`tests/test_porcelain_service.py`（install/start/stop/restart 成對驗證、status 三模式與版本、logs 兩來源、uninstall/--purge、`--json`）。
- [ ] 1.2 `paulsha_cortex/porcelain/service.py` 七子命令實作 + 復用 `_runtime_probe`。
- [ ] 1.3 `_FAMILY_MODULES` 登記 service 模組。
- [ ] 1.4 README CLI 段落補 `service` 家族（R-16）；`changelog.d/porcelain-service.md` 與 `CHANGELOG.md [Unreleased]` 同步（條目含 porcelain-service 字樣）。
- [ ] 1.5 pytest 全綠、policy_check 0 fail、`git diff --check` 乾淨。
