---
status: accepted
work_item: agy-builder-support
---

# Tasks

- [x] RED：在 `tests/test_coordinator_agy_launcher.py` 新增 builder 形態測試（`--mode accept-edits`、`--add-dir <worktree>`、
      無 `--sandbox`、`allow_unsafe` 附 `--dangerously-skip-permissions`），以及
      `SubprocessLauncher(executor="agy", allow_unsafe=True)` 不拋錯的測試；先確認全數失敗。
- [x] 實作 `build_agy_argv` 三分支與 `SubprocessLauncher.__init__` 的 agy 放寬；`commit_required` 傳遞擴及 agy。
- [x] 補上 registry overlay `build` capability 與 writable launcher 形狀的機械對應測試（預期 mode／
      sandbox／worktree 由 loader 宣告導出）；commit-required Git metadata 與真實 launcher argv
      forwarding 均有回歸測試；unsafe／commit-required builder intent 無 worktree 皆
      fail-closed，default 無 worktree 則維持歷史 direct planning 形態。
- [x] 回歸：planner／reviewer 形態既有測試維持綠；planning runtime／agy probe 明示 `read_only=True`。
- [x] 更新 `launcher.py` 相關註解以反映 agy builder 可寫；補 `changelog.d/` 碎片。
- [x] 依 retry_context 重現並補強 F1／F2／F3 的機械回歸覆蓋；agy 的 builder 判定固定為
      「有 provisioned worktree 且非 read-only/reviewer」，無 worktree 的歷史 direct planning
      維持 `plan+sandbox`；本卡交付範圍仍止於 pre-archive。
- [ ] Manager 於交付前執行 authoritative preflight 並採信 Candidate；archive、merge、issue closure 與 done
      判定不屬本卡。
