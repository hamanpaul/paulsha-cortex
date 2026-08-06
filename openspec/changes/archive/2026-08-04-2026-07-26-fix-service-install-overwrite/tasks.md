---
status: accepted
work_item: fix-service-install-overwrite
---

# Tasks

- [x] [RED] `tests/test_fix_service_install_overwrite.py`：既有有效 config（指向 venv A）+ 不同呼叫者 venv（venv B）呼叫 `install service` → 不覆寫既有 config（或 raise 清晰錯誤）。
- [x] [RED] 既有 config 無效或指向同一 venv 時 → 正常安裝/更新（idempotent）。
- [x] [RED] 首次安裝（無既有 config）→ 正常安裝。
- [x] [實作] `paulsha_cortex/deploy/installer.py`：新增 idempotent guard——偵測既有 config 指向不同（有效）venv 時拒絕覆寫並 raise 清晰錯誤訊息。
- [x] [同步與驗證] `changelog.d/fix-service-install-overwrite.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#148` 條目。
- [x] [同步與驗證] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [x] [同步與驗證] 勾選本 tasks.md 對應項並以 conventional commit 提交。
