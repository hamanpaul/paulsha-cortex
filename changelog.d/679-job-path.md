### Fixed
- **#679：job 的 `PATH` 沒有一個來源是被決定過的——兩層都補、fail-closed，並禁止驗證
  指令自帶 `PATH`**。降權模式下每一個 job 解到哪一份 CLI，取決於三件全部沒人裁決過的
  事：六份模板 unit 沒有一份有 `Environment=PATH=`；`job_runner.build_job_env()` 對
  `PSC_*_PATH` **fail-open**（未宣告就不寫這個鍵）；而 `PATH` 當時還在**轉發類**白名單
  上，於是未宣告時 job 靜默拿到 **Manager daemon 的** `PATH`。三條路的終點一樣：
  `claude`／`agy` rc=127，而 `codex` **靜默**解到 `/usr/bin/codex`（實機 0.42.0，
  toolchain 那份 0.147.0）——不報錯，只是產出來自一支 operator 從未判讀過的 CLI。
  builder／reviewer／gate 三個角色全中。

  **四件事**：

  1. **`build_job_env()` 改 fail-closed（裁決 (a)：raise）**——新增
     `job_runner.resolve_job_path()`，`PSC_BUILDER_PATH`／`PSC_REVIEWER_PATH`／
     `PSC_GATE_PATH` 未宣告時以 `job-runner-path-undeclared` 當場失敗，訊息帶得出
     「哪個變數」與「去哪一份 unit 取正規值」。**不採選項 (b)（退回產生器預設）**：
     本 repo 對「未宣告即用預設」已有明確立場（#453「registry 永不寫入預設值」），
     而「job 解哪一份 CLI」正是必須有人做的決定；替他做一次只會把「未宣告」與
     「宣告成這樣」壓成同一種狀態，下一次漂移一樣看不見。既有部署會在下一次派工
     直接 fail——**那是對的**，現況是靜默跑錯版本（升級步驟見 runbook 5-5b）。
  2. **`PATH` 移出轉發類白名單**——這才是本票真正的 fail-open，issue 的證據鏈少了
     這一環：daemon 的 `PATH` 帶著 `<deploy_root>/venv/bin`（等於把 job 的 `python3`
     綁回 Manager 的 venv），且是否含 `<toolchain>/bin` 純看該機器的 EnvironmentFile
     被誰手動加過什麼。與 `HOME`／`VIRTUAL_ENV`（早就在排除表上）同一類錯誤，
     現改列 `BUILDER_SYNTHESIZED_ENV` ＋ `EXCLUDED_ENV_RATIONALE`。
  3. **`Environment=PATH=` 寫進六份模板 unit**（permgen 機械產生，與 Manager 端變數
     同源）——#640 當時判斷「寫在 unit 上會被 shim 丟掉」對了一半：spec 的 env 確實是
     job 的完整環境，但產生它的那一支當時 fail-open，於是**兩層同時為空**。現在
     `job_shim` 在 spec 沒有 `PATH` 時退回 unit 這一層（root-owned、可逐字稽核，
     因此不是 fail-open），兩層都缺才拒絕 exec，且該失敗發生在**接管 log 之前**
     （理由進 journal，而不是變成一份空的 job log）。第二層同時涵蓋「手工組 spec
     繞過產生器」（#645 的同型前例）與「spool 裡還躺著升級前寫的舊 spec」。
  4. **驗證方法**——runbook 共用探針 `psc_run_under` 移除 `--setenv=PATH=`，
     `psc_probe_path()` 整支刪除；4e／5-3／5-4／附錄 A 五處自帶 `PATH` 的驗證改為
     絕對路徑或改走新的 4e-2。新增 `permgen.build_path_resolution_probe()` ／ CLI
     `trust_root path-probe`：**角色 × executor 全列舉**的反向不變式（剖面跟著
     executor 走，因此 `codex` 與 `claude` 驗的是**兩份不同的 unit**），以**零額外
     env** 起 job、斷言解到 `<toolchain>/bin/<cli>`，並與同一支檔案的絕對路徑版本
     逐字比對（不另立第二份會漂移的版本清單）。產出**刻意不含任何 `--setenv=`**，
     由 `path_probe_env_injections()` ＋ 測試釘住。

  **為什麼它活過五輪驗證**：runbook 4e／5-2b、#661 與 #664 的量測、事故當天的每一次
  探針，全部自帶 `--setenv=PATH=…`——**驗證環境供應了 production 不供應的東西**，
  於是缺陷在結構上不可能被觀察到。runbook 4e 甚至逐字預言了症狀、連 0.42.0 這個版本
  號都寫對了，但那一條是 `sudo -u … env PATH=…` 跑的，所以它驗的是「toolchain 裡那份
  是對的版本」，不是「job 實際會解到哪一份」。這是「綠燈不承載語意」的**第五**個實例
  且是新的一類：前四次（#638／#657／#673 兩次）是「複本比 production 弱或強」，這次是
  **複本比 production 多**。#677 立下「加固面複本必須全量機械導出」，本票再推一格：
  **複本必須連「production 沒有設什麼」也一起複製**——`unit_replica_properties()` 天生
  做得到，要拿掉的是探針額外疊上去的那一行。

  **順手收掉一條第三份真相**：runbook 5-5 手打的 `PSC_GATE_PATH=/usr/local/bin:/usr/bin:/bin`
  少了 toolchain 段，與 permgen（#666 的 `SYSTEM_PROGRAMS` note 逐字記著
  `<toolchain>/bin ＋ JOB_PATH_SYSTEM_TAIL`）不一致。三個 `PSC_*_PATH` 現一律由產生器
  導出，runbook 不再手打。

  **測試**：`tests/test_job_path_fail_closed_679.py`（47 條）——三個角色各自缺席／空值
  的 fail-closed、角色互不污染、錯誤訊息的 `trust_root unit` 旗標與
  `permgen.JOB_UNIT_CLI_FLAG` 一致、六份 unit 皆有 `Environment=PATH=` 且值由
  `PathLayout` 導出（換部署根即跟著換）、shim 三種情形（spec 優先／退回 unit／兩層都缺
  即 `ShimError` 且不建 log）、**複本的 PATH 逐字來自 unit 且 unit 沒有時複本也沒有**、
  探針產生器的可執行行不得出現 `--setenv=`（含偵測器自己的 negative control）、**探針呼叫共用的 `psc_run_under` 而不自帶第二份定義**（未定義時 fail-closed）、矩陣
  完整性與剖面對應。OS 層語意（真的解到哪個檔）需要第二個 UID ＋ 真實 systemd 加固面
  ＋ 兩份同名不同版的 CLI，本環境全部沒有，因此以**具名 `@pytest.mark.skip` ＋ 完整
  理由**標示，並列出改由哪三個地方守——不靜默通過。
