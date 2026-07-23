---
status: accepted
work_item: porcelain-bootstrap
---

# porcelain-bootstrap Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_porcelain_bootstrap.py`（mock B3 `inspect`／B4 `service` 命令函式 + fake PATH/登入態 fixtures）：preflight 各項缺失情境 exit 4 與修法文案、`--dry-run` 只預覽不動作、正常流程依序呼叫 service install/start 與 inspect status/doctor、`--sample` 失敗降級不影響整體 exit code、`--json` schema `cortex-porcelain/bootstrap/v1`；先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/porcelain/bootstrap.py`：`bootstrap` 命令，preflight + 呼叫 B4 `service.install`/`service.start` + 呼叫 B3 `inspect.status_summary`/`inspect.doctor_summary` + 選配呼叫 B7 `init_sample`。
- [ ] `paulsha_cortex/porcelain/__init__.py`：`_FAMILY_MODULES` 加入 bootstrap 模組（僅此一行）。

### 3. 同步與驗證

- [ ] README 補 `bootstrap` 一段，含「10 分鐘上手」快速路徑指引（R-16）。
- [ ] 新增 `changelog.d/porcelain-bootstrap.md` fragment，`CHANGELOG.md [Unreleased]` `### Added` 加入含 `porcelain-bootstrap` 字樣的條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 完成後勾選 `openspec/changes/porcelain-bootstrap/tasks.md` 對應項並以 conventional commit 提交（不得改動本 plan 檔）。
