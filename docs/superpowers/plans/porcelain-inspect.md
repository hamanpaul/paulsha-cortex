---
status: accepted
work_item: porcelain-inspect
---

# porcelain-inspect Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_porcelain_inspect.py`（tmp control root + fake job registry + fake systemd unit 檔 fixtures）：涵蓋 `status`/`job`/`ready`/`work`/`doctor`/`service` 六子命令、human 與 `--json` 輸出一致性、`service` 殭屍偵測情境（unit 指向不存在 venv）、查無對象 exit 1、`--json` schema `cortex-porcelain/inspect/v1`；先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/porcelain/_runtime_probe.py`：`probe_service_runtime(instance)` 共用探測函式（模式/unit 狀態/pid/exec path/版本/`stale`）。
- [ ] `paulsha_cortex/porcelain/inspect.py`：`inspect status/job/ready/work/doctor/service` 六子命令，包裝既有查詢邏輯，唯讀、不經 control queue。
- [ ] `paulsha_cortex/porcelain/__init__.py`：`_FAMILY_MODULES` 加入 inspect 模組（僅此一行）。

### 3. 同步與驗證

- [ ] README 的 CLI 命令面補 `inspect` 家族一段（R-16）。
- [ ] 新增 `changelog.d/porcelain-inspect.md` fragment，`CHANGELOG.md [Unreleased]` `### Added` 加入含 `porcelain-inspect` 字樣的條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 完成後勾選 `openspec/changes/porcelain-inspect/tasks.md` 對應項並以 conventional commit 提交（不得改動本 plan 檔）。
