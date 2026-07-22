---
status: accepted
work_item: porcelain-request
---

# porcelain-request Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_porcelain_request.py`（tmp control root fixtures）：list 排序/標注、show 雙態、wait 退出碼 0/1/3、logs 關聯、`--json` schema `cortex-porcelain/request/v1`；先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/porcelain/request.py`：`request list/show/wait/logs` 四子命令，路徑取自 `control.constants`，唯讀、不經 control queue。
- [ ] `paulsha_cortex/porcelain/__init__.py`：`_FAMILY_MODULES` 加入 request 模組；並承接 B1 review findings——`load_commands()` 改為 fail-open（家族模組 import/註冊失敗時記錄並跳過，不影響既有命令）且對 `register_commands` 缺失/非 callable 給出含模組名的明確錯誤。
- [ ] 補上 `openspec/specs/porcelain-command-registry/spec.md` 的 Purpose 敘述（B1 archive 鷹架殘留 TBD）。

### 3. 同步與驗證

- [ ] README 的 CLI 命令面補 `request` 家族一段（R-16）。
- [ ] 新增 `changelog.d/porcelain-request.md` fragment，`CHANGELOG.md [Unreleased]` `### Added` 加入含 `porcelain-request` 字樣的條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 完成後勾選 `openspec/changes/porcelain-request/tasks.md` 對應項並以 conventional commit 提交（不得改動本 plan 檔）。
