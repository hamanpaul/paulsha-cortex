---
status: accepted
work_item: agy-builder-support
---

# Agy Builder Support Todo

## Boundary

- Issue: `hamanpaul/paulsha-cortex#799`（owner 裁決走 A：真正支援 agy build）。
- Scope 限於 `paulsha_cortex/coordinator/launcher.py` 的 `build_agy_argv`／`SubprocessLauncher.__init__` 與
  `tests/test_coordinator_agy_launcher.py`；不動其他 executor、不動 registry schema、不動 doctor。

## Tasks

- [ ] RED：在 `tests/test_coordinator_agy_launcher.py` 新增 builder 形態測試（`--mode accept-edits`、`--add-dir <worktree>`、
      無 `--sandbox`、`allow_unsafe` 附 `--dangerously-skip-permissions`），以及 `SubprocessLauncher(executor="agy", allow_unsafe=True)`
      不拋錯的測試；先確認全數失敗。
- [ ] 實作 `build_agy_argv` 三分支與 `SubprocessLauncher.__init__` 的 agy 放寬；`commit_required` 傳遞擴及 agy。
- [ ] 回歸：planner／reviewer 形態既有測試維持綠；`test_agy_argv_is_headless_plan_sandbox_and_keeps_prompt_single` 不變。
- [ ] 更新 `launcher.py` 相關註解（1148-1156、1795-1815）以反映 agy builder 可寫；補 `changelog.d/` 碎片。
- [ ] 跑 focused／full repository gates，candidate evidence 記入 Cortex 後交付。
