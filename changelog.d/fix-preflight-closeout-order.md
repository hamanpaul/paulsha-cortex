### Fixed
- Issue #263：ship 先做本地 closeout、preflight 失敗改 typed stop（通過後照舊自動建立 PR），並補齊 slice-based review frozen authority materialize/hash 驗證。
- PR #336 code review 追加修正：
  - `review.prepare_review_worktree()` authority materialize 的 ref 安全檢查改為「先驗證後動作」——在任何 `mkdir`/寫入之前，先拒絕含 `..` 或絕對路徑的 ref，避免依賴迴圈內後段的 `relative_to` 檢查兜底；新增涵蓋 `..` 與絕對路徑惡意 ref 的迴歸測試。
  - `work_bridge._manager_archive_applied()` 改為委派 `manager._manager_archive_applied()`（不再各自維護一份 `any(...)`／`len==1` 邏輯），避免 crash/retry 造成 run.steps 出現多筆 passed 的 openspec-archive step 時，兩處判定漂移導致誤判已完成而跳過 local closeout。
  - `manager._slice_review_authority_inputs()` 對相對 `spec`／`plan` path 改以 `repo_root` 為 base 解析，對齊 `_pinned_input_mismatches()` 既有支援的 legacy/回復情境相對 plan path，避免誤用當前 cwd 解析造成讀錯檔或拋例外。
