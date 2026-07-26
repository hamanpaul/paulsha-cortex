---
status: accepted
work_item: fix-service-install-overwrite
---

# fix-service-install-overwrite Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_fix_service_install_overwrite.py`：
  - 既有有效 config（venv A）+ 呼叫者 venv B → 不覆寫，raise 清晰錯誤。
  - 既有 config 無效或同 venv → 正常安裝。
  - 首次安裝（無既有 config）→ 正常安裝。
  - 先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/deploy/installer.py`：新增 idempotent guard——偵測既有 config 指向不同有效 venv 時拒絕覆寫。

### 3. 同步與驗證

- [ ] `changelog.d/fix-service-install-overwrite.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#148` 條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-26-fix-service-install-overwrite/tasks.md` 對應項並以 conventional commit 提交。