### Added
- **R0.5 D6 / trust-root Phase 2b：operator 0816 第三輪裁決 A+B 的程式碼側——三分 UID
  定案 ＋ `job_runner` 的 template-instance 模式**（Refs #584）

  **A（三分定案）**：`permgen.DEFAULT_SCHEME = THREE_WAY_SCHEME`
  （`cortex-manager`／`cortex-reviewer-planner`／`cortex-builder`），`trust_root` CLI
  未指定 scheme 時一律出三分，`two-way` 保留為需**顯式**打出的向後相容選項（打錯字
  不會靜默退回較寬鬆的方案）。判準是「injection 可達的任何進程都不持有 spawn 授權」：
  二分下 reviewer／planner 與 Manager 併帳，任一被攻陷即取得 polkit 的 start grant。
  既有不變式測試本來就對兩案參數化跑，因此三分無需新增任何 policy 分支。
  polkit 產生器的預設方案同步改為 **B（`PolkitPlan.TEMPLATE`）**：subject＝
  `cortex-manager`、action＝`manage-units`、verb ∈ {start, stop}、unit pattern＝
  `^cortex-job@…\.service$`，且 `plan_residual_risk()` 對 B 回傳空 tuple。

  **B（root-owned 模板 unit）**：`coordinator/job_runner.py` 新增第三模式
  `PSC_JOB_RUNNER=systemd-template`。builder job 改為 (a) 把 per-job spec
  （command／worktree／白名單 env／log 路徑）**原子**寫進 Manager-owned spool
  `<coordinator_root>/job-specs/<instance>.json`；(b)
  `systemctl start --wait --no-ask-password cortex-job@<instance>.service`。
  spec **不含任何身分欄位**（`SPEC_FORBIDDEN_KEYS` 在寫端與讀端各擋一次）——`User=`
  只存在於 root-owned 的模板 unit 檔，Manager 帳號**選不了 UID、也給不了命令列**。

  **新資產**：`config.paths.job_spec_spool_root()` ＋ R1 登記表 `job-spec-spool`
  （Manager-owned，writer 只有 Manager、builder 在 reader 面）。permgen 因此機械產出
  owner-only（0700）＋ builder 唯讀 ACL（`rX`），並讓 Manager 的 `ReadWritePaths` 少
  一條「額外可寫路徑」例外（原本的 `<agents_root>/jobs` run.sh spool 由本資產取代）。

  **shim**：模板 unit 的 `ExecStart=` 固定為 `<deploy_root>/bin/cortex-job-shim %i`。
  permgen 的 `build_job_shim()` 產出 root-owned 的啟動 stub（以部署 venv 的絕對路徑
  interpreter exec `paulsha_cortex.coordinator.job_shim`，**不**走 `/usr/bin/env`），
  shim 邏輯本體是 repo 內的可測模組：驗 instance 名 → `O_NOFOLLOW` 讀 spec → 白名單
  schema 驗證（含身分欄位與憑證 env 守衛）→ 接管 log → chdir → `execvpe`。
  新增 CLI：`python3 -m paulsha_cortex.trust_root shim [three-way|two-way]`。

  **判活與 log 沿用既有機制**：`--wait` 讓 `systemctl` client 存活到 unit 結束
  （`systemctl(1)`，systemd ≥ 232），`dispatcher.pid_alive()` 的 pid 判活與 exit
  sentinel 皆零改動；harvest 讀的 log 路徑逐字不變（`<log_dir>/<slice_id>.jsonl`）。
  log 導引刻意**不用** `StandardOutput=append:`——那個檔由 PID 1（root）在降權**之前**
  開啟，路徑上任何一段由 Manager 掌控就成了 root-follows-symlink 的提權面；改由 shim
  在已降權之後以 `O_NOFOLLOW` 接管。

  **fail-fast**（一律 `DiagnosticReason` fail-closed，**絕不**退回其他模式）：
  `systemctl` 缺席／未以 systemd 開機／帳號或 group 不存在／模板 unit 未安裝／shim
  未安裝或不可執行／spec spool 缺席／**同名 instance 已在跑**（`systemctl start` 對
  active unit 會靜默回 0）／spec 寫入失敗。

  **零回歸**：`direct` 與 `systemd-run` 兩模式逐字不變，既有 63 個 job_runner 測試零
  改動。新增 `tests/test_trust_root_job_template_ab.py`（71 測試）。全套
  `python3 -m pytest -q` → **3437 passed, 49 subtests**（main 基準 3366＋71，零回歸）。

### Fixed
- **`preflight_systemd_run()` 的 `which` seam 改為 lazy 解析**——原本 `which=shutil.which`
  在 **def 時**就把函式物件綁進預設值，`mock.patch.object(job_runner.shutil, "which", …)`
  因此打不到它：`test_preflight_failure_propagates` 實際是靠「本機沒有 `cortex-builder`
  帳號」而不是它想驗的分支通過的（在有該帳號的主機上會紅）。改為 `which or shutil.which`
  後行為完全相同，但測試驗到的是它宣稱要驗的東西。

### Changed
- **誠實邊界**：`PSC_JOB_RUNNER` 預設仍是 `direct`；template 模式要生效需 Phase 2b
  安裝（三帳號＋模板 unit＋polkit 規則＋shim），屬部署期動作，不在本次變更範圍。
