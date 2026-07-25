---
status: accepted
work_item: fix-dispatch-exception-detail
---

# fix-dispatch-exception-detail Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_fix_dispatch_exception_detail.py`：
  - `DispatchReadyError(errors={sid: FileNotFoundError(...)}, jobs=[]).__str__()` 含 `sid`、`FileNotFoundError`、與路徑訊息。
  - tick handler 遇 `DispatchReadyError` 時 response `errors` 含 per-slice dict（sid/type/message）；`jobs` 保留。
  - manager.log 每行首欄為可解析 ISO-8601（log capture fixture）。
  - 先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/coordinator/autonomy.py`：`DispatchReadyError.__str__` 組 per-slice 摘要（cap message 長度）。
- [ ] `paulsha_cortex/coordinator/manager_daemon.py`：tick handler 把 `DispatchReadyError.errors` 寫入 response `errors`；log 寫入加 ISO-8601 前綴 helper。

### 3. 同步與驗證

- [ ] `changelog.d/fix-dispatch-exception-detail.md`；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#100` 字樣條目。
- [ ] README 對應段同步（R-18：dispatch 失敗例外透傳、manager.log 時間戳）。
- [ ] `python3 -m pytest tests/ -q` 全綠；`policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-25-fix-dispatch-exception-detail/tasks.md` 並以 conventional commit 提交。