---
status: accepted
work_item: porcelain-service
---

# Tasks

- [x] 1.1 RED：`tests/test_porcelain_service.py`（install fallback `--json`、start/stop/restart 成對驗證與無 systemd graceful failure、status 三模式與版本、logs 兩來源、uninstall/--purge 與無 systemd graceful failure、`--json`）。
- [x] 1.2 `paulsha_cortex/porcelain/service.py` 七子命令實作、install 正確回報 systemd/fallback mode，並在無 systemd 時讓 lifecycle/uninstall 顯性失敗且復用 `_runtime_probe`；repair round 補上全子命令共用 instance 驗證、systemd-only `logs --follow` 串流與 failure-path `--json` envelope。
- [x] 1.3 `_FAMILY_MODULES` 登記 service 模組。
- [x] 1.4 README CLI 段落補 `service` 家族（R-16）；`changelog.d/porcelain-service.md` 與 `CHANGELOG.md [Unreleased]` 同步（條目含 porcelain-service 字樣）。
- [x] 1.5 `tests/test_porcelain_service.py` focused regression pack 全綠、`python3 -m policy_check --repo .` 0 fail、`git diff --check` 乾淨。
