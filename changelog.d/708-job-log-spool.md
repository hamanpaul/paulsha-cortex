### Fixed

- **builder／gate 的 job log 目錄沒有寫入授權——define 首次收斂後 builder job 立刻
  死在 shim 開 log 之前（#708）。** `job_shim._take_over_stdio()` 在**接管 stdio 之前**
  就 `os.open(log_path)`，而 builder 的 log 落在 Manager 的 dispatch log 目錄
  （`<coordinator_root>/logs/workflow/`，`0700 cortex-manager`、零具名 ACL）⇒ 實機
  `[Errno 13] Permission denied`、unit `78/CONFIG`、Manager 端
  `job-runner-template-instance-start-failed`——**失敗發生在它能記錄失敗之前**。

### Changed

- **三個降權 principal 的 job log spool 由同一條規則導出**（`registry.JOB_LOG_SPOOLS`）。
  #686 為 planner 做過同一件事但只做了那一格，代價就是本票。現在
  `builder`／`reviewer`／`gate` 各有一個登記表資產，掛在該 principal **既有**的輸出
  通道底下（`commit-spool`／`review-verdict-spool`／`gate-ledger-spool`），由兩條
  **import 期斷言**強制：缺一格 `registry` 載不起來、掛到不是既有通道或沒有嚴格落在
  通道之內則 `permgen` 載不起來。「只修一格」因此在**結構上做不到**（先例：#698 的
  `EXECUTOR_ENFORCEMENT_LEAVES`）。
- **可寫面逐字不變、零部署動作**：三格都被 `read_write_paths()` 的 `_minimize()`
  吃掉，三份模板 unit 的 `ReadWritePaths=` 與 #708 issue body 的實機證據逐字相同；
  per-job 那一格的權限由通道的 default ACL 自動繼承。
- **Manager 端的 harvest 路徑逐字不變**：`<log_dir>/<slice>.jsonl` 改以 **hard link**
  指向 job 那一格的同一個 inode。`log_path` 這個字面量、exit sentinel、gate ledger、
  spool key 的推導、`usage_extractors` 與 `_log_tail` 一個位元組都沒有變。刻意不是
  symlink（shim 以 `O_NOFOLLOW` 開 log；而且 symlink 由名字解析，job 換得掉指向）。
  刻意不把 log 目錄加進 RWP：那一層住著 gate ledger 與 exit sentinel（#604）。
- **gate 的 log 改由 Manager 預先建立**（`0620`）。舊路徑與 ledger 共用同一格、由
  job 自己建，帶降權 unit 的 `UMask=0077` ⇒ `0600 cortex-gate`，Manager 讀不到——
  gate 失敗時逐字原因只存在於一個看不見的檔裡（#638 缺陷 2 的同一個機制）。
- **per-job log 一格的生命週期收斂到 `spool_slot.prepare_job_log()`**（三個 principal
  共用同一份實作，planning 那一份改為委派）。

### Added

- **shim 失敗的機器可讀紀錄**（`<log 那一格>/shim-error.json`，`job_shim.write_shim_error`
  → `job_runner.read_shim_error`）。shim 在接管 log 之前的失敗原本只進 unit journal，
  而 Manager 帳號讀不到那份 journal，端上只看得到 `systemctl exit=1`。紀錄由 job 帳號
  寫 ⇒ **可偽造、不進任何採信路徑**，只用來把排查從 journal 拉回 Manager 這一側。
- **反向不變式的實機探針**：`python3 -m paulsha_cortex.trust_root job-log-probe`
  （`permgen.build_job_log_probe`）。每個 principal 以**零額外 env**、真實模板 unit 的
  加固面正向斷言「寫得出自己那一格 log ＋ Manager 讀得回來」，反向斷言「Manager 的
  dispatch log 目錄仍然寫不進去」。加固面複本一律走 `psc_run_under`／
  `unit_replica_properties()` 全量導出（D13：**不得自組 `--property=`、不得自帶
  `--setenv=PATH=`**，由 `path_probe_env_injections()` 機械釘住）；那一格由
  `spool_slot.prepare_job_log()` **本人**建，不是手工前置物（#645 的教訓）。
- runbook 第 **4e-2d** 步：實機遷移命令、三條 `getfacl` 形狀驗證、三份 unit 的 RWP
  逐字不變、反向不變式、真實派工 smoke 的 inode 比對，含 0818 三個部署陷阱的複述。
