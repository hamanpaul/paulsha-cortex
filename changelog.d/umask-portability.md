# umask-portability

### Fixed
- **`#723`(a)：`test_tree_snapshot_covers_empty_directories_directory_links_and_modes` 對
  umask 不可攜——寫死的 `chmod(0o700)` 在 `UMask=0077` 的 unit 下是 no-op。**
  gate 的 job 跑在 `cortex-gate-job@.service`（逐字 `UMask=0077`），該 unit 底下
  `empty.mkdir()` 建出來的目錄**已經**是 `0700`，於是接在後面的 `empty.chmod(0o700)`
  什麼都沒改，`_tree_snapshot()` 前後兩個雜湊逐字相同，`assert ... != baseline` 必紅。
  實機 ledger（`wf-6c37c77ca1-worktree-isolation-3.gates.json`）逐字
  `AssertionError: assert '687b1390…' != '687b1390…'`。同一份程式在 CI 與 operator
  本機全綠——這條**只在 gate 的執行環境紅**，因為測試隱含了「預設 umask 建出來的目錄
  mode ≠ 0700」這個只在 operator 一般 shell 成立的前提。
  **修法**：判準改成「chmod 成一個**與現況不同**的 mode」，而不是 chmod 成某個字面值。
  `mutated_mode = baseline_mode ^ 0o001` 由**實測到的** `baseline_mode` 導出，因此在任何
  umask（0077／022／0 皆已實跑）下都必然與現況不同；翻的是 other-execute 位元，owner
  `rwx` 不動，`_tree_snapshot()` 的走訪不受影響。回復用的 `chmod(baseline_mode)` 原本
  就已經是動態的，不需要改。
  **測試仍在驗原本那個性質**：把 `_tree_snapshot()` 的 `metadata.st_mode` 換成
  `stat.S_IFMT(metadata.st_mode)`（保留檔案型別、丟掉權限位元）做 mutation 驗證，三種
  umask 下這條都轉紅——斷言沒有被拿掉，也沒有被放寬成恆真。另補兩條輔助斷言
  （`mutated_mode != baseline_mode`、chmod 後實際 mode 等於 `mutated_mode`），讓將來若
  再退化成 no-op 時失敗訊息直接指到成因，而不是只看到兩個一樣的雜湊。
  ⚠️ **jit 剖面也是 `UMask=0077`**，故本修正與 `#723`(b) 的剖面裁決無關，兩條路都受益。
  `#723`(b)（strict 剖面的 `MemoryDenyWriteExecute` 殺 node，`test_openspec_archive_purpose.py`）
  屬 operator 裁決，本次**不動**。
