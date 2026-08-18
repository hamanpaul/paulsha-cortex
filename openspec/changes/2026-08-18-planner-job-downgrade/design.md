---
status: accepted
work_item: planner-job-downgrade
---

# planner-job-downgrade Design

本檔為摘要；完整論證（含十條防線的逐條對應表、未決問題總覽、安全退步總覽）在
`docs/superpowers/specs/planner-job-downgrade-design.md`。

## Decisions

### D1 執行後端抽象成 `PlanningInvoker`

抽出「拿 identity + prompt、回傳 stdout 與退出狀態」的介面，兩份實作
（`InProcessPlanningInvoker` 保留現行十條防線；`JobPlanningInvoker` 走 job runner）。
JSON 抽取留在共用層，兩者吃同一份。選擇點只有
`job_runner.resolve_runner_mode(os.environ)`——與 launcher 同一支函式，**刻意不新增
planning 專用的第二個開關**（第二個開關的失效模式是「以為降權了、其實沒有」）。

### D2 十條防線的逐條對應

`tempfile` 一次性、`cwd=sandbox`、drift 收容、hermetic claude env、逾時、stdout 取回
＝等價或升級；operator 樹保護從「事後比對雜湊」升級為 `ProtectSystem=strict` ＋
RWP 不含 `repo-source-tree` 的 **mount 層不可寫**；兩條退步逐條指名：
**R-1** sandbox dirty 偵測（Manager 進不去 job-owned 樹，且明確禁止由被驗方自證）、
**R-2** codex `-o last.json` 第二輸出候選消失。兩條各有可避免路徑（U-2、U-3）。

### D3 輸出通道沿用 reviewer 已走通的「Manager-owned log ＋ shim O_APPEND」

不新開通道。planning 的 wrapper 顯式退化成「只有模型 argv」一段——不跑 gate、不產
bundle、不寫 verdict、不寫 sentinel，且不靠既有旗標碰巧為 None。

### D4 逾時由 Manager 側 `wait(timeout)` → `systemctl stop`

不靠 unit 的 `RuntimeMaxSec=`：planning 的四種呼叫上限不同，而 job spec **結構性禁止
攜帶任何 property**（`SPEC_FORBIDDEN_KEYS`），那條禁令是對的，不該為逾時鑽洞。
停止後必須確認 unit 離開 active，否則下一輪會撞 `instance-busy`，錯誤訊息與逾時無關。

### D5 probe 快取：Manager-owned、指紋含執行後端、fail-closed

新增登記表資產 `planning-probe-cache`（Manager-owned 0600，writers／readers 只有
MANAGER，**不進任何 job unit 的 RWP**——快取一旦可由 job 寫，「這個 provider 是 ready 的」
就變成模型可以自證的東西）。指紋含 `PSC_JOB_RUNNER`（direct 的成功不得替 job 模式背書）、
executor 可執行檔 inode 指紋、憑證檔指紋、加固剖面 ＋ **模板 unit 檔本身**的指紋、
roster 內容雜湊。模板 unit 進指紋是刻意的：**任何**讓 operator 重跑產生器落新 unit 的
改動，在落檔那一刻就讓全部快取自動失效重探，不需要任何人記得清快取。

### D6 剖面零新增判定點；現行剖面表**實測可用**，無前置修正

剖面唯一來源是 `prepare_systemd_template(executor=…)`，planning 側零對應表。

本設計原本寫著「假設 #673 已修」，**該前提是錯的，已移除**。八份 unit 全帶
`SystemCallErrorNumber=EPERM`（`cortex-reviewer-job@.service:148`、兩份 `-jit` 的 `:162`、
`cortex-manager.service:89`），被過濾的 syscall 回 `EPERM` 而非 `SECCOMP_RET_KILL_PROCESS`，
V8 走 fallback ⇒ codex／copilot 照常啟動；真 unit 完整 property 集合下實測
codex／copilot 在 `jit` rc=0、claude／agy 兩剖面皆 rc=0。實際被過濾的是 `pkey_alloc`
（`@pkey`），加 `@sandbox` 無效。**現行 `EXECUTOR_HARDENING_PROFILE` 就是對的**，
planner 上 job 不需要任何剖面面的前置修正。#673 已由 PR #677 以「不放寬任何 syscall」收尾。
剖面表日後若因其他理由改動，本設計零改動、只動驗收矩陣。

**#677 另立了一個本設計必須消費的維度**：seccomp 過濾語意在剖面**之外**
——`PROFILE_LOCKED_KEYS`（`SystemCallFilter`／`SystemCallErrorNumber`）兩剖面逐字相同，
`ToolchainProgram.filtered_syscalls` 與 `filtered_syscall_surfaces()` 記錄實機量到、
有 audit 背書的過濾項（現況四筆全部 `fatal=False`），`seccomp_filter_is_fatal()` 回答
「被過濾會不會殺行程」，`_validate_seccomp_tolerance()` 在 import 時強制承重那條不得被
靜默拿掉。影響：D5 的指紋不能只放剖面名（要放**模板 unit 檔本身**），D8 的
`executor-silent-exit` 診斷要帶 `seccomp_filter_is_fatal()` 的結果。

### D7 憑證 codify：沿用 #671 的機制，不重造

`IN_PLACE_CONTENT_WRITE_ASSETS`（RWP 掛檔案本身而非父目錄）與
`inapplicable_home_anchored_assets()`（二分部署下機械排除）已經把 #640 當年
「不敢登記第二份憑證」的唯一理由拆掉。補一列不需要改產生器。
部署順序固定為「憑證落位 → 重跑產生器 → daemon-reload」（憑證不存在時帶該 RWP 的
unit 會刻意起不來）。雙 provider 憑證屬新的安全決策，列為 U-4／U-5 交 operator。

### D8 錯誤語意三分 ＋ 逐候選拒因表

`planning-job-start-failed`／`planning-executor-failed`（含 `executor-silent-exit`
子類——「連錯誤訊息都沒有」的那一種；診斷 MUST 帶 `seccomp_filter_is_fatal()` 的結果，
不對成因預設任何特定 syscall）／`planning-output-malformed`。
`SecondarySelection` 增 `rejections`，`no-heterogeneous-planner` 從「結論」變成
「結論 ＋ 每個候選為什麼落選」，並在 reason 裡渲染。
`_classify_planning_failure` 對含 environment 級拒因的情形改判 environment，讓
`recover-planning` 有路（今天一律 `content` ⇒ 死路）。

### D9 一次 planning 呼叫 = 一個 unit 實例

instance 名 `plan-<run_id 前 12 字>-<purpose>-<序號>`，先過
`job_workspace.JOB_SEGMENT_RE`。`purpose` 只是識別字串，不進任何決策欄位。
probe 在 `run_id="ephemeral"` 的呼叫端必須另帶隨機後綴，否則並行 probe 互撞
`instance-busy`。

### D10 PATH 缺口是實作票 E 的前置

env 檔無 `PSC_REVIEWER_PATH` ⇒ job 的 PATH 是 PID 1 預設 ⇒ `claude`／`agy` rc=127、
`codex` 解到 `0.42.0`。`claude`／`agy` 的 rc=127 至少會失敗；**`codex` 那一種不會失敗**
——它安靜地用舊 CLI 產出結果。後者只有把 `resolved_binary` 與 `binary_version` 記進
失敗診斷與驗收矩陣才看得見（D8／R8）。

### D11 分階段 land：能，但切換一次到位

票 A／B／C 可在 direct 部署上獨立 land 並各自產生價值。切換不能一半一半——probe 快取
的指紋含 `PSC_JOB_RUNNER`，混用會讓兩種語意的結論在同一輪並存。

### D12 明確不做：非同步 planning 狀態機

與本票要解的「執行身分」正交，且需要 `WorkflowRun` schema 改動。本票維持同步阻塞語意
（粒度與今天相同，只多出 job 啟動延遲）。

### D13 驗證方法的硬規則：加固面一律機械導出——**機制已由 PR #677 落地，本設計消費它**

凡宣稱「某 executor 在某加固剖面下可用／不可用」，驗證環境 MUST 由已落檔 unit 機械
讀出全部 property 再複製，MUST NOT 手抄子集。**這條已經是程式**：
`permgen.unit_replica_properties()`（契約「全帶，不選」，`require_hardening=True` 下
少任一加固鍵即 `UnitReplicaDriftError` 且 stdout 保持空）、CLI
`trust_root unit-replica`、runbook 第 4e 步的共用探針 `psc_run_under`。
實作票的驗收矩陣 MUST 走它們，MUST NOT 自行組 `--property=` 清單。
`SystemCallFilter=` 與 `SystemCallErrorNumber=` 的成對約束已由 `PROFILE_LOCKED_KEYS`
固化（只帶前者會把「回 `EPERM`、呼叫方可 fallback」偷換成「行程直接死」，而兩者症狀
都是空輸出）。

**判準雙向，四個實例兩個方向**：#638、#657 手抄得比 production **寬** ⇒ **假綠**
（斷言真空）；#673 原 body 與其 repro 手抄得比 production **嚴**（漏 
`SystemCallErrorNumber=EPERM`）⇒ **假紅**，並據此開票要求放寬 seccomp。假紅更貴——它會
讓人去「修」一個不存在的問題，並在修的過程中放寬一條真的有用的加固項。因此規則的正確
形式是「驗證環境 ≠ production 環境在結構上不可能」，與偏差方向無關。

#643 早已記錄過 `SystemCallErrorNumber=EPERM` 這一條，#673 仍然重蹈——**已經寫下來的
教訓，在下一次用手抄複本時不會自動生效**，這正是它必須變成機械規則而非註解的理由。
規則的理由記在本設計裡，因為 planner 是**下一個**會做這種宣稱的地方；
runbook 第 4e／5-2b 步已由 #677 一併轉為機械導出，不需另開票。
