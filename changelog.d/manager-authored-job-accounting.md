# manager-authored-job-accounting

- **`#604` gate ledger 與 exit sentinel 的作者收斂到 Manager（降權後具名信任缺口的第一步）**——
  登記表資產 `gate-ledger`（Manager 的 dispatch log 目錄，同時放 `<slice>.gates.json`
  與 `<slice>.exit`）宣告 `writers=(MANAGER,)`，但兩個檔一直是由
  `launcher.build_wrapper_script` 產生的 wrapper **在 job 進程內**寫。2026-08-17
  Phase 2b M1 實機上線（`PSC_JOB_RUNNER=systemd-template`，builder job 真的以
  `uid=cortex-builder` 跑）之後，這條路同時有信任面與可行性兩個問題：sentinel 是
  `dispatcher.poll_headless_done` 的第一判準、ledger 是 `authorize_terminal` 採信
  `passed` 的唯一背書，卻由被驗方自報（違反 `#540` 的「model 既不能自證成功、也不能
  自證失敗」）；而那個目錄在 Phase 2b 是 `0700 cortex-manager`、且**不在** builder
  模板 unit 的 `ReadWritePaths=` 內（`ProtectSystem=strict`），job 寫進去必然 EROFS
  ——降權模式下每個 builder job 都會落到「行程已死、無 sentinel」的分支被記成 failed。
  - **exit sentinel 改由 Manager 記帳**：新增
    `job_runner.build_manager_exit_recorder_argv()`，把 `systemd-run --wait`／
    `systemctl start --wait` 的 client argv 包進一層跑在 **Manager 身分**的
    `bash -c`，由它寫下 client 的 `$?`。**sentinel 路徑逐字不變**（harvest 端零改動），
    變的只有「誰是寫者」。`--wait` 的 client 本來就存活到 unit 結束，因此 pid 判活語意
    也不變。
  - **起動確認改判準**：記帳 shell 在 client 起不來時也會寫 sentinel，故
    `confirm_transient_unit_started()`／`confirm_template_instance_started()` 新增
    `manager_authored_sentinel=` 旗標——為真時判準改為「確認窗內 client 已結束且狀態
    非 0」（`--wait` 的 client 不可能在 200ms 內走完一次真正的 unit 啟動；極端情況下
    誤判的結果是**拒絕這次派工**，仍是 fail-closed）。未傳旗標＝既有語意，零回歸。
  - **降權模式下 job 側 wrapper 不再寫這兩個檔**：`build_wrapper_script()` 新增
    `write_sentinel=`，降權時連同 gate 階段一併移除（`_should_run_gates()` 對降權模式
    回 `False`）。
  - **採信端拒絕外來作者**：新增 `terminal_contract.foreign_evidence_author()`
    （`lstat` 取 owner 與本行程 effective uid 比對）。`read_gate_ledger()` 對非
    Manager 產生的 ledger 以新 reason `gate-ledger-foreign-author` fail closed；
    `dispatcher._read_exit_sentinel()` 對非 Manager 產生／非普通檔的 sentinel 視同
    尚未寫下（沿用既有 fail-closed 分支記成 failed）。direct 模式下 job 與 Manager
    同 uid，這兩條永遠不會命中——**既有部署零影響**。
  - **尚未處理（後續票）**：gate 的**執行面**仍未移出 builder。直接把 gate 重跑放進
    Manager 進程會讓 builder 完全掌控的 worktree（`conftest.py`／pytest plugin）取得
    `cortex-manager` 身分的任意程式碼執行，比本票要修的缺口更嚴重；正確落點是一個
    既非 builder、也非 Manager 的 gate 執行身分，需要動 permgen 的 UID scheme 與 unit
    產生器。在那之前降權模式不產生 ledger，build 卡照 `require_ledger` fail closed
    ——沒有獨立證據就不採信，而不是採信一份 builder 自己寫的。
  - 新增 `tests/test_manager_authored_job_accounting_604.py`：釘住「builder 對
    `gate-ledger` 目錄零寫入權」的 permgen 結構事實、降權 wrapper 內不得出現 sentinel／
    ledger 落點、外來作者的 ledger／sentinel 一律不採信、以及 direct 模式零回歸。
