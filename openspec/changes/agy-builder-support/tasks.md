---
status: accepted
work_item: agy-builder-support
---

# Tasks

- [x] RED：在 `tests/test_coordinator_agy_launcher.py` 新增 builder 形態測試（`--mode accept-edits`、`--add-dir <worktree>`、
      無 `--sandbox`、`allow_unsafe` 附 `--dangerously-skip-permissions`），以及
      `SubprocessLauncher(executor="agy", allow_unsafe=True)` 不拋錯的測試；先確認全數失敗。
- [x] 實作 `build_agy_argv` 三分支與 `SubprocessLauncher.__init__` 的 agy 放寬；`commit_required` 傳遞擴及 agy。
- [x] 補上 registry `build` capability 與 writable launcher 的機械對應測試；commit-required Git metadata
      與 launcher forwarding 均有回歸測試；無 worktree 的 builder 形態 fail-closed。
- [x] 回歸：planner／reviewer 形態既有測試維持綠；planning runtime／agy probe 明示 `read_only=True`。
- [x] 更新 `launcher.py` 相關註解以反映 agy builder 可寫；補 `changelog.d/` 碎片。
- [ ] Manager 於交付前執行 authoritative preflight 並採信 Candidate；archive、merge、issue closure 與 done
      判定不屬本卡。
