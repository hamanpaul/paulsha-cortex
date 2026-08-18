### Fixed
- **#686（#672 票 E）：`JobPlanningInvoker`——planning 的模型呼叫改走
  `cortex-reviewer-job@.service`，Manager 行程樹不再出現任何 executor**——`cortex-manager`
  的 passwd 註記逐字寫著 `no model code`，而 planning（define／brainstorm）的四個 adapter
  與**全部** probe 至今仍在 daemon 行程內以該身分 `subprocess.run` 模型 CLI（#672）。
  #615（M2）只把 reviewer 導上模板 unit，planner 走的是完全不同的一條 code path。本票補齊
  那半條：新增 `coordinator/planning_job.py` 的 `JobPlanningInvoker`（票 B 立的
  `PlanningInvoker` 介面的第二個實作），一次 planning 呼叫＝一個模板 unit 實例，身分由
  root-owned unit 的 `User=cortex-reviewer-planner` 決定、剖面由 `identity.executor` 單一
  決定、spec 結構性不得攜帶身分／剖面欄位。**不複製任何一份 `job_runner` 的邏輯**：
  preflight／instance 推導／spec 形狀／env 白名單／起動確認全部走既有函式。
  選擇點仍只有 `job_runner.resolve_runner_mode()` 一個；`systemd-run`（A 案）**fail-closed
  而不退回行程內執行**——A 案下加固面由呼叫端而非 root-owned unit 決定，而退回 in-process
  的失敗**看起來像成功**，那正是 #672 要消除的失效模式。
- **U-2 裁決＝planner scratch 對 job 唯讀，且它是登記表機械導出的性質、不是一個 `if`**——
  新增登記表資產 `planning-scratch-pool`（writers 只有 `Principal.MANAGER`），
  `required_write_targets()` 因此機械地不收它，它不出現在**任何** job 模板 unit 的
  `ReadWritePaths=`，`ProtectSystem=strict` 下模型連寫都寫不進去。design 標記的安全退步
  **R-1**（「模型弄髒自己的拋棄式 sandbox」的偵測在 job 側 Manager 做不到）因此從「失去
  行為訊號」升級成「結構上不可能」。executor 的可寫落點改指向 unit 的 `PrivateTmp=yes`
  私有 `/tmp`（per-invocation、job-owned、unit 結束即消失、Manager 看不到）。
- **planning 的輸出通道不新開寫入面**——新增登記表資產 `planning-job-log-spool`，路徑掛在
  既有 `review-verdict-spool` **底下**（`<spool>/planning-logs/<instance>/planning.log`）。
  那個帳號今天本來就對這棵樹有 `wx`，而 `read_write_paths()` 的 `_minimize()` 會吃掉被涵蓋
  的子路徑 ⇒ 模板 unit 的 `ReadWritePaths=` **逐字不變、零部署動作**、default ACL 自動
  繼承。design D3 第一句是「不新開通道」、U-3 更把新開 job→Manager 寫入面列為未決，本票
  因此不動用它。log 檔由 **Manager 預先建立且 mode 為 `0620`**：job 建的檔由 job 擁有
  （`UMask=0077`）Manager 讀不到（#638 缺陷 2），而用 `0600` 建檔會把繼承來的
  `user:<planner>:wx` 的 ACL mask 壓成 `#effective:---`（#638 缺陷 1 的同一個機制）。
- **失敗語意三分在 job 側落地**——`PlanningJobError` 攜帶票 A 的族名，
  `_probe_identity` 讓它**原樣**成為 `CapabilityProbe.reason`（票 A 的
  `_PROBE_REASON_FAMILIES` 已預留這條路），拒因表因此看得到「job 起不來」與「executor 死」
  的差別，而不是一律退化成型別名。`executor-silent-exit`（rc≠0 且輸出全空）的診斷指名
  `unit`／`hardening_profile`／`resolved_binary`／**`--version` 字串**／
  `permgen.seccomp_filter_is_fatal()` 的機械答案——最後一項正是 #673 整張票走偏的原因
  （當時沒有任何地方回答得了「該不該懷疑 seccomp」），版本字串則是 #681 那類「只比路徑會
  漏掉」的缺陷唯一看得見的地方。逾時走 D4 的 Manager 側 `wait(timeout)` →
  `systemctl stop` → **確認 unit 離開 active**（不確認的話下一輪會撞
  `job-runner-template-instance-busy`，而那個症狀與逾時完全無關）。

### Changed
- `docs/superpowers/runbooks/trust-root-phase2b-setup.md`：新增第 4e-3／4e-4／4e-5 步
  （唯讀 scratch 的 EROFS 驗證與逐 executor 實測表、跨 UID log 通道的 ACL mask 驗證、
  「Manager 行程樹無 executor」的取證程序）與第 5-5c 步（補宣告 `PSC_REVIEWER_HOME`／
  `PSC_GATE_HOME`）。加固面一律走既有共用探針 `psc_run_under`（其 property 清單由
  `permgen.unit_replica_properties()` 從**落檔的 unit** 全量導出），**未新增任何手寫的
  `--property=` 清單、未自帶 `--setenv=PATH=`**（design D13）。
- `job_runner.build_job_env()` 的 docstring 更正一句**事實錯誤**：原文稱「`HOME` 未給時
  systemd 依 passwd 填入正確值（而且模板 unit 另有一行 `Environment=HOME=`）」——那對
  模板模式不成立，`cortex-job-shim` 以 `os.execvpe(command[0], command, job_env)` 把環境
  **整份換掉**，unit 的 `Environment=HOME=` 到不了模型行程。實機 0818 複驗：未宣告
  `PSC_REVIEWER_HOME` 時降權 planning job 的 agy 死在
  `resolving log directory: getting home directory: $HOME is not defined`；補上之後同一條
  呼叫 rc=0。與 #679 的 PATH 缺口**同型**（builder 那一份有宣告，另外兩個角色沒有）。
  本票只更正事實並補 runbook 步驟，**不**順手把 `HOME` 也改成 fail-closed——那會讓所有
  角色的既有派工在 EnvironmentFile 補齊前當場失敗，屬於需要獨立票的改動。
