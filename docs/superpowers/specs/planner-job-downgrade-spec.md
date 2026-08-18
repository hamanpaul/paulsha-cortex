---
status: accepted
work_item: planner-job-downgrade
---

# planner-job-downgrade Specification

issue #672：planning（define／brainstorm）從未降權——`planning_runtime` 的四個 adapter
與所有 probe 至今仍在 **Manager 行程內**、以 `cortex-manager` 身分 `subprocess.run`
執行模型 CLI，而該帳號的 passwd 註記逐字寫著 `no model code`。#615（M2）建了
`cortex-reviewer-job@.service` 並把 **reviewer** 導上去，planner 走的是完全不同的一條
code path，從未接上模板 unit。

本 spec 定案「把 planning 的模型執行搬到 job runner」這件結構性搬遷的**契約面**：
搬遷後哪些性質必須逐條成立、哪些現行防線必須等價重建、哪一條會退步、以及失敗時
必須能被分辨成什麼。**本票只交付 spec／design／plan，不含任何 `paulsha_cortex/` 的
程式修改**；實作切分見 `docs/superpowers/plans/planner-job-downgrade.md`。

## 背景

現況缺口（皆已在 `origin/main@ae089c3` 與實機部署上複驗）：

- **執行面**：`paulsha_cortex/coordinator/planning_runtime.py:830` 的 `_invoke_json()`
  以 `runner(argv, cwd=str(sandbox), …)` 呼叫，`runner` 預設值為 `subprocess.run`
  （同檔 `:1070`）。`_planning_argv()`（同檔 `:43`）回傳的是**裸 executor argv**
  （`codex exec …`／`claude -p …`／`build_agy_argv(...)`），沒有 `systemd-run`、沒有
  `cortex-job-shim`、沒有任何模板 unit。
- **呼叫端**：`paulsha_cortex/coordinator/manager_daemon.py:970` 與 `:1241` 把
  `planning_runtime.build_production_planning_runtime` 接進 daemon 的 periodic tick
  與 request executor，daemon 由 `cortex-manager.service` 以 `User=cortex-manager` 執行。
- **repo 自己的註記已寫明狀態**：`paulsha_cortex/coordinator/job_runner.py:405-408`
  `JOB_ROLE_REVIEW` 的 rationale 說「M2（#615）：在此之前它們仍在 Manager 行程內以
  Manager 帳號執行」。reviewer 已藉 `launcher.py:1239 _is_review_persona()` 接上
  job runner（`read_only` 是三個判準之一），**但 planner 根本沒有走 `SubprocessLauncher`
  這條路**——它走 `planning_runtime._invoke_json`。那段 rationale 描述的「M2 之前的狀態」，
  對 planner 而言就是現在的狀態。
- **登記表已經把這條列成逾期項**：`paulsha_cortex/trust_root/permgen.py` 的
  `deferred_run_dependencies()` 第四項 `manager-claude-credential` 逐字寫著
  「`planning_runtime` 的 JSON 呼叫在 **Manager 行程內**直接 exec `claude`（不是派一個
  降權 job）」，而它的 `disposition` 給的兩條路之一正是「裁決『Manager 不直接跑模型、
  planning 一律走降權 job』」。本票就是走那一條。
- **現行部署下 planner 結構性不可用**（#672 實測）：Manager unit 帶
  `MemoryDenyWriteExecute=yes` ⇒ node 型 executor（codex／copilot）在 Manager 行程內
  必崩；`ProtectSystem=strict` 讓 `$HOME` 唯讀 ⇒ agy 連 log／state 目錄都建不出來；
  `/var/lib/cortex-manager` 下**零 executor 憑證**。三者疊起來，
  `probe_agy_capability()` 與所有 `_probe_identity()` 必然全滅，
  `select_secondary_planner()` 因此永遠回 `no-heterogeneous-planner`（#668）。
- **誤報已經發生過**：#670 的 agy code fence 偽失敗（約 17%）被壓成
  `no-heterogeneous-planner`，把一個**格式解析問題**報成**拓撲問題**，排查方向整個帶偏。
  本 spec 的 R6 就是為了讓這一類誤報結構上不可能。
  （#670 的**本體**已由 PR #674 修好：`probe_agy_capability()` 現在會 `strip_code_fence()`、
  失敗帶 `stdout_excerpt()`，並修掉 `agy models` 改成 `id\tDisplay Name` 兩欄之後造成的
  100% `model-not-listed`。本 spec 以修後形狀為準；R6 要解的是**上游把診斷吃掉**那一層，
  那一層 #674 沒碰、也不該由它碰。）
- **本票新查到的部署缺口**（0818，本票查證，尚未有票）：
  `/opt/cortex/etc/cortex-manager.env` **沒有宣告 `PSC_REVIEWER_PATH`**，Manager unit 也
  沒有 `Environment=PATH=`。因此降權 job 的 PATH 就是 PID 1 的預設
  （`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin`），而
  `/opt/cortex/toolchain/bin` 不在其中。實測：`codex` 解到系統層的 `/usr/bin/codex`
  ＝ `codex-cli 0.42.0`，toolchain 那一份是 `codex-cli 0.147.0`；`claude`／`agy` 在
  該 PATH 上**完全不存在**（rc=127）。這正是 reviewer 模板 unit 註解裡預先警告過的
  「跑得起來但版本不是你以為的那個」，只是那段註解描述的 EnvironmentFile 內容從未落地。

## Goals

- 讓「Manager 永不執行模型程式碼」這句話在 **define 階段也成立**，而不是只對 builder
  與 reviewer 成立。
- 搬遷過程中**不弄丟**現行 `_invoke_json` 的任何一條防線；弄不回來的必須被逐條指名為
  安全退步，並附代價評估。
- 讓 probe 的成本從「每輪 tick × 每個 planning identity 各起一批 job」降到可接受，且
  快取本身不得成為一條「綠燈不承載語意」的新缺口。
- 讓 planning 失敗可以被機械分辨成「job 起不來 / executor 死 / 輸出不合約」三類，
  並讓 `no-heterogeneous-planner` 永遠攜帶逐候選拒因。
- 讓部署面（憑證、PATH、剖面）由登記表與產生器機械導出，而不是由 runbook 手抄。

## Requirements

### R1 planning 的每一次模型呼叫 MUST 經 job runner 以 job 帳號執行（對應 D1、D9）

`PSC_JOB_RUNNER` ∈ {`systemd-run`, `systemd-template`} 時，`planning_runtime` 的
`invoke_primary`／`questioner`／`secondary`／`integrator`（`planning_runtime.py:1105-1145`）
與**所有 probe**（`_probe_identity()`、`probe_agy_capability()` 的兩次 CLI 呼叫）
MUST 經 `job_runner` 起一個 `JOB_ROLE_REVIEW` 的降權 job 執行，MUST NOT 在 Manager
行程內直接 `subprocess.run` 任何 executor argv。

`PSC_JOB_RUNNER=direct` 時 MUST 逐字維持現行 in-process 行為（含 `_copy_planning_sandbox`
與前後兩次 `_tree_snapshot`），本 spec 不改變 direct 模式的任何語意。

執行後端的選擇 MUST 只有一個輸入：`job_runner.resolve_runner_mode(os.environ)`——與
`launcher.SubprocessLauncher._downgraded_mode()` **同一支函式**。MUST NOT 新增
planning 專用的第二個開關（例如 `PSC_PLANNING_INVOKER`）；第二個開關就是第二份真相，
而它的失效模式是「以為降權了、其實沒有」。

角色 MUST 是 `job_runner.JOB_ROLE_REVIEW`，由呼叫端在**建構期**固定，MUST NOT 從
prompt、job spec、模型輸出或任何 job 可影響的來源導出（比照 `launcher._job_role()`
的既有紀律；`job_runner.SPEC_FORBIDDEN_KEYS` 已在寫端與讀端各擋一次身分欄位）。

若不做：`no model code` 這條帳號註記在 define 階段是假的，而 planner 正是**吃
untrusted issue 內容**的那個角色；D6「三分已生效」的全稱宣稱在 planner 這一支不成立。

#### Scenario: 降權模式下的 planning 呼叫

- **WHEN** `PSC_JOB_RUNNER=systemd-template` 且 Manager 觸發 define／brainstorm
- **THEN** 每一次模型呼叫落成一個 `cortex-reviewer-job@<instance>.service` 實例，
  `User=cortex-reviewer-planner` 由 root-owned unit 檔決定
- **AND** `cortex-manager` 的行程樹內不出現任何 executor 可執行檔

#### Scenario: direct 模式回歸

- **WHEN** `PSC_JOB_RUNNER` 未設或為 `direct`
- **THEN** planning 的行為與本票之前逐字相同（含 sandbox 複製與 drift 收容）

### R2 一次性 sandbox 的防線 MUST 逐條有對應，缺一條 MUST 在設計中明講「這條會退步」（對應 D2、D3）

`_invoke_json` 現行的防線由十條組成（`tempfile.TemporaryDirectory`、
`_copy_planning_sandbox`、`cwd=sandbox`、呼叫後 sandbox 快照比對、呼叫前後 operator
樹快照比對、`_contain_operator_drift` 的備份與報告、`_seed_hermetic_claude_env`、
`subprocess` 逾時、`capture_output` 取回 stdout、codex 的 `-o last.json` 第二輸出候選）。

搬遷設計 MUST 對這十條逐條給出「誰、在哪裡、怎麼保證」的對應，且 MUST 明示每一條
落在下列三類的哪一類：**等價**、**升級（由偵測改為 kernel 阻擋）**、**退步**。
退步項 MUST 附代價評估與可觀測的替代訊號，MUST NOT 以「反正 sandbox 是拋棄式的」
帶過而不記錄。

其中兩條的結論在本 spec 直接固定：

1. **operator 工作樹的保護 MUST 從「事後偵測 + fail-closed」升級為「mount 層不可寫」**。
   `repo-source-tree`（`/var/lib/cortex/repos/<slug>`）MUST NOT 出現在 reviewer 模板
   unit 的 `ReadWritePaths=` 中（現況已成立，且該 unit 註解已逐字寫明「下方
   ReadWritePaths **不含**來源樹」）。因此「planner 經絕對路徑寫 operator 樹」這條
   路在降權模式下由 `ProtectSystem=strict` 直接關掉，不再依賴呼叫端記得比對雜湊。
2. **「模型是否弄髒了自己的拋棄式 sandbox」這條偵測 MUST NOT 由被驗方自證**。
   job 側 wrapper 自行 snapshot 再寫進 log 是**明確禁止**的實作（違反 #628／#540
   「被驗方不得在自己的進程裡產生自己的驗收證據」）。設計 MUST 在「Manager 側可驗」
   與「結構上不可能發生」兩條路裡擇一，並在 design 註明取捨。

若不做：搬遷會靜默弄丟 operator 樹的唯一防線，而症狀（planning 產出正常）與正常
狀態完全一樣——這正是本 repo 已經記錄過三次的「綠燈不承載語意」家族。

#### Scenario: 模型嘗試寫 operator 工作樹

- **WHEN** 降權模式下 planner 以絕對路徑寫 `/var/lib/cortex/repos/<slug>` 底下任一檔
- **THEN** 該寫入被 `ProtectSystem=strict` 擋下（EROFS），Manager 側無需比對雜湊
- **AND** planning 呼叫本身的成敗由 executor 的退出碼與輸出決定，不受此影響

### R3 probe 結果 MUST 跨 tick 快取，且快取判準 MUST 涵蓋執行後端（對應 D5）

`build_production_planning_runtime()` 目前在**每次建構**時對每個 planning-capable
identity 跑一次 probe，而它由 `manager.run_auto_claim_scan()`（periodic tick）與
`apply_work_action()` 兩條路徑呼叫。搬到 job 之後，每個 probe 就是一個 job
（`probe_agy_capability()` 是**兩個**：`agy models` 加一次 smoke）。

probe 結果 MUST 落成 Manager-owned 的 durable 快取，並 MUST 滿足：

- 快取 key MUST 至少涵蓋：`(executor, model_id)`、executor 可執行檔的解析結果與其
  inode 指紋、該帳號憑證檔的指紋、加固剖面名與模板 unit 檔本身的指紋、roster
  解析結果的內容雜湊、以及 **`PSC_JOB_RUNNER` 的值**。
- 最後一項是硬性要求：direct 模式取得的 probe 結論 MUST NOT 被 job 模式採信（反之亦然）。
  兩者的執行環境（PATH、HOME、憑證、seccomp、MDWE）完全不同，共用一格快取等於讓
  開發機的成功替生產環境背書。
- 快取檔不存在／JSON 損毀／schema 版本不符／指紋不符 MUST 一律視為 miss 並重探；
  MUST NOT 因為「上一次是 ready」而在無法重探時沿用 ready（**fail-closed**）。
- 快取 MUST 同時保存失敗側的完整診斷（reason、diagnostic、退出碼、stdout 前 200 字），
  供 R6 的失敗分類消費。
- 快取資產 MUST 進 `trust_root` 登記表，writers／readers 只有 MANAGER，且 MUST NOT
  出現在任何 job 模板 unit 的 `ReadWritePaths=` 中。

若不做：每輪 tick 起一批 job 去問模型「你是誰」，成本不可接受；而若快取做成
fail-open，一個曾經 ready 過的 provider 會在憑證過期後繼續被當成可用，症狀是
planning 到很後面才失敗。

#### Scenario: 模板 unit 換版後的重探

- **WHEN** operator 重跑產生器落下新的 `cortex-reviewer-job@.service`（剖面表改動、
  RWP 增列、加固項調整任一者）
- **THEN** 全部 probe 快取因模板 unit 指紋改變而失效並重探
- **AND** 不需要任何人記得手動清快取

#### Scenario: 快取檔損毀

- **WHEN** 快取 JSON 無法解析
- **THEN** 視為 miss 重探，MUST NOT 沿用任何舊結論
- **AND** 落一筆結構化 log，指明是快取損毀而非 probe 失敗

### R4 加固剖面 MUST 沿用單一判定點，MUST NOT 新增第二份剖面表（對應 D6）

planner 的 job MUST 經 `job_runner.prepare_systemd_template(env, job_id=…,
executor=identity.executor, role=JOB_ROLE_REVIEW)` 取得剖面，剖面的唯一輸入 MUST 是
`identity.executor`，未登記的 executor MUST fail-closed（不落到寬鬆那一份）。

planning 路徑 MUST NOT 自帶任何 executor→剖面的對應表；那張表的唯一真相在
`permgen` 的 `EXECUTOR_HARDENING_PROFILE`（由 `EXECUTOR_TOOLS.needs_node` 機械導出），
`job_runner` 那一份是成對契約、已有測試釘住兩邊逐字相等。

**現行剖面表是對的，本 spec 不預設任何待修的剖面缺口。** #673 原 body 主張
`SystemCallFilter=@system-service` 會讓 codex／copilot 在全部 job unit 下靜默 rc=1；
該主張**已被開票者自行更正並撤回**，#673 由 **PR #677** 以「不放寬任何 syscall」收尾。
實機複驗（本票獨立確認）：八份 unit
（六份 job 模板 ＋ manager ＋ monitor）**全部帶 `SystemCallErrorNumber=EPERM`**
（`cortex-reviewer-job@.service:148`、兩份 `-jit` 的 `:162`、`cortex-manager.service:89`），
從權限產生器落地的第一個 commit 起就在。有這一條時被過濾的 syscall 回 `EPERM` 而非
`SECCOMP_RET_KILL_PROCESS`，V8 走 fallback，codex／copilot 照常啟動。在真 unit 的
**完整** property 集合下實測：codex／copilot 在 `jit` 剖面 rc=0，claude／agy 兩種剖面
皆 rc=0——**預設派工路徑沒有壞**。實際被過濾的是 `pkey_alloc`（`@pkey`），不是
`landlock_*`／`seccomp`（`@sandbox`）；加 `@sandbox` 對症狀完全無效。

因此本 spec 的 R4 就是「照現行 `EXECUTOR_HARDENING_PROFILE` 機械導出」，
planner 上 job 之後不需要任何剖面面的前置修正。

**但剖面不是唯一的加固維度**（PR #677）：seccomp 過濾語意是**剖面之外的第二個維度**
——`permgen.PROFILE_LOCKED_KEYS`（`SystemCallFilter`／`SystemCallErrorNumber`）兩份剖面
逐字相同，`ToolchainProgram.filtered_syscalls` 與 `filtered_syscall_surfaces()` 記錄
實機量到的過濾項，`seccomp_filter_is_fatal()` 回答「被過濾會不會殺行程」，
`_validate_seccomp_tolerance()` 在 import 時強制承重的那條不得被靜默拿掉。
本 spec 對此的要求是**消費、不重造**：R3 的快取指紋因此 MUST 含**模板 unit 檔本身**
（只放剖面名涵蓋不到第二維），R6 的 `executor-silent-exit` 診斷 MUST 帶
`seccomp_filter_is_fatal()` 的結果。

剖面表若日後因其他理由改動（新增 executor、新增剖面名），需要跟著動的**只有**
`permgen` 的那張表與 `job_runner` 的成對常數；本設計零改動——這正是「不新增第二個
判定點」買到的東西。唯二會波及本設計的是：(i) R3 的快取指紋必須含模板 unit 檔本身，
剖面表改動才會自動使快取失效；(ii) 若新增剖面名，R8 的驗收矩陣要多一列。

### R5 planner 帳號的憑證 MUST 由登記表機械導出（對應 D7）

`cortex-reviewer-planner` 已於 0818 部署（本票複驗）：

```
/var/lib/cortex-reviewer-planner/
  drwxr-xr-x root:root                       .codex/
  -rw------- cortex-reviewer-planner:…       .codex/auth.json      ← 檔 job-owned、目錄 root-owned
  lrwxrwxrwx root:root .gemini -> /var/lib/cortex-reviewer-planner/cache/gemini
  drwx------ cortex-reviewer-planner:…       cache/                ← 已在 unit 的 ReadWritePaths 內
```

這份部署在**功能上**足夠（agy 的可寫狀態樹落在已放行的 `cache`；`agy models` 列出
`gemini-3.1-pro-high`，capability smoke 逐位元等於 expected），但在**治理上**不足，
兩個缺口：

1. 實機 `cortex-reviewer-job@.service` 的 `ReadWritePaths=` 只有
   `/var/lib/cortex-reviewer-planner/cache` 與 `/var/lib/cortex/coordinator/review-verdicts`
   ——**不含 `.codex/auth.json`**。該 unit 自己的註解花了七行描述這條路徑該怎麼掛，
   但登記表只有 `builder-executor-credential` 一列（#698 之後該資產叫
   `builder-codex-state`，且兩個帳號**共用同一列憑證表**），所以產生器產不出它。淨效果：
   `ProtectSystem=strict` 下憑證**讀得到、改不了**，token 過期那天靜默 refresh 失敗。
   這正是 `permgen.deferred_run_dependencies()` 第一項逐字描述的逾期項。
2. `.gemini → cache/gemini` 這個 root-owned symlink 是一條**手動落位、登記表不知道**
   的部署決定。換一台機器部署、或重跑產生器，它不會出現。

因此本 spec 要求：planner 帳號的憑證面 MUST 由 `trust_root` 登記表表達，MUST NOT 只
由 runbook 步驟落位。具體 MUST 涵蓋：`cortex-reviewer-planner` 的 executor 憑證檔
（列入 `IN_PLACE_CONTENT_WRITE_ASSETS`，`ReadWritePaths` 掛檔案本身而非父目錄）、
以及 agy 狀態樹的錨定方式。

`executor_credential_relpath` 目前是**單一部署決定**（`.codex/auth.json`），一個帳號
只表達得了一份憑證——而 planner 需要**兩個 independence domain** 才有異質性。
把它擴成 per-(account, executor) 的表（#668 的 B 案）是**新的安全決策**而非既有裁決
的延伸：擴大的是該帳號被攻陷時的 provider 曝險面。本 spec **不做這個決定**，只要求
設計把選項與取捨列清楚交給 operator（見 design 的未決問題總覽 U-4／U-5）。

若不做：憑證是「這台機器上有、下一台沒有」的隱性狀態，而失效症狀（token 過期）
與 planning 內容問題長得一樣。

### R6 planning 失敗 MUST 三分，且 `no-heterogeneous-planner` MUST 攜帶逐候選拒因（對應 D8）

搬到 job 之後，同一個「planning 沒成功」有三個結構上不同的來源，MUST 能被機械分辨：

| 族 | 條件 | classification |
|---|---|---|
| `planning-job-start-failed` | job 起不來：polkit 拒絕、模板未安裝、shim 不可執行、spec spool 不可寫、instance 已 active、`confirm_template_instance_started` 逾時 | `environment` |
| `planning-executor-failed` | job 起來了、executor 非零退出或無任何輸出 | `environment` |
| `planning-output-malformed` | executor 正常退出、輸出不符 JSON 契約 | `content`（既有 transient-service 例外仍適用） |

`planning-executor-failed` MUST 另外標記 **`executor-silent-exit`** 子類：rc≠0 且
stdout 與 stderr 皆空。這一類是整個家族裡最難查的一種——**連錯誤訊息都沒有**，因此
歸因會落到模型、prompt、逾時或憑證，而不會落到執行環境。它 MUST 被顯式命名，並
MUST NOT 被壓成任何拓撲原因。

該子類的診斷 MUST 帶 `permgen.seccomp_filter_is_fatal()` 的結果，並 SHOULD 帶
`filtered_syscall_surfaces()` 中對應該 (executor, 剖面) 的已知過濾項。理由：這條子類
唯一的資訊價值就是「該往哪個方向查」，而「seccomp 是否致命」正是最容易被誤猜、
也最容易機械回答的那一維——#673 整張票走偏，正因為當時沒有任何地方回答得了它。

（#673 原 body 曾把這一類的一個實例歸因到 `SystemCallFilter`，該歸因已被開票者更正
撤回——但「rc≠0 而完全無輸出」這個**類別**本身確實存在且值得具名，這是本子類保留的
理由；本 spec 不對它的成因預設任何特定 syscall。）

`select_secondary_planner()` 回傳 `no-heterogeneous-planner` 時，MUST 同時攜帶
**逐候選的拒因表**：每個 planning-capable identity 記錄它為什麼沒被選中
（`same-domain`／`probe-not-ready:<reason>:<diagnostic>`／`probe-identity-mismatch`／
`probe-absent`），且 probe 側的 diagnostic MUST 帶得動 R3 快取裡保存的實際證據
（stdout 前 200 字、退出碼）。`run_heterogeneous_brainstorm` MUST 把該表渲染進
`BrainstormResult.reason`。

`_classify_planning_failure()` MUST 能從拒因表看出「全部候選都是 environment 級拒因」
並把整體改判 `environment`——今天 `no-heterogeneous-planner` 一律落 `content`，而
`content` 在 `_resume_decision` 一律不浮現 `recover-planning`，等於一條死路。

若不做：#670 那一類誤報會原封不動地重演，只是這次的誤報來源會多出「job 起不來」
與「seccomp 靜默」兩種，而它們的症狀（空輸出）與模型不聽話完全無法區分。

#### Scenario: agy 回傳被 code fence 包住的正確 JSON

- **WHEN** probe 的 stdout 是 ` ```json\n{…}\n``` `
- **THEN** blocking reason 為
  `no-heterogeneous-planner (agy/gemini-3.1-pro-high: probe-not-ready malformed-output …)`
- **AND** operator 從 reason 就看得出這是格式問題，不必重跑六遍才發現

#### Scenario: executor 在 job unit 下靜默非零退出

- **WHEN** job 成功啟動但 executor 回非零且 stdout 與 stderr 皆空
- **THEN** 失敗落 `planning-executor-failed / executor-silent-exit`，classification
  為 `environment`，reason 指名 unit、加固剖面與實際解析到的可執行檔絕對路徑
- **AND** MUST NOT 出現 `no-heterogeneous-planner` 作為唯一線索

### R7 逾時 MUST 由 Manager 側強制終止（對應 D4）

現行 `_invoke_json` 以 `subprocess.run(..., timeout=timeout_seconds)` 保證呼叫必然在
上限內返回。降權模式下 Manager 拿到的是 `systemctl start --wait` 的 client 進程，而
實機 `cortex-reviewer-job@.service` **沒有 `RuntimeMaxSec=`／`TimeoutStartSec=`**
（本票複驗），因此一個掛住的模型會讓 client 無限等待。

降權模式 MUST 在 Manager 側強制上限：等待逾時後 MUST 主動停止該 unit（polkit 已
放行 `stop`），並落 `planning-job-timeout`（歸 `environment`）。MUST NOT 只是放棄
等待而讓 job 繼續跑——那會讓下一次同名 instance 撞上
`job-runner-template-instance-busy`，症狀與逾時完全無關。

若不做：一次 planning 逾時會把 Manager 的 periodic tick 永久卡住。

### R8 加固面的驗證 MUST 走既有的機械導出機制（對應 D13、D6、D10）

任何形如「某 executor 在某加固剖面下可用**或不可用**」的宣稱，驗證環境 MUST 由
**已落檔的 unit 機械讀出全部 property** 再複製，MUST NOT 手抄 property 子集。

**這條的機制已由 PR #677 落地，本 spec 要求消費它、不得重造**：

| 機制 | 用途 |
|---|---|
| `permgen.unit_replica_properties(unit_text, *, instance, require_hardening=True)` | 已落檔 unit → `systemd-run --property=` 的**完整**清單（契約「全帶，不選」）；少任一加固鍵即 `UnitReplicaDriftError` 且 stdout 保持空 |
| CLI `python -m paulsha_cortex.trust_root unit-replica <unit｜->` | 上一支的命令列入口（`-` 讀 stdin，可接 `systemctl cat`） |
| runbook 第 4e 步的 `psc_run_under <unit 字幹> <命令>` | runbook 與測試共用**同一個**「真實加固面」定義 |

實作票的驗收矩陣 MUST 走 `psc_run_under`／`unit-replica`。測試或 runbook 裡任何
**手寫的 `--property=` 清單**都違反本條。

**判準是雙向的**：手抄子集比 production **寬**會產生假綠（宣稱可用、實機不可用），
比 production **嚴**會產生假紅（宣稱不可用、實機其實好的）。兩個方向都必須擋。
本 repo 已有四個實例，而且兩個方向都出現過：

| 事故 | 手抄的偏差方向 | 產生的錯誤 |
|---|---|---|
| #638 | 單 UID 環境讓 ACL 斷言真空（比 production 寬） | **假綠**——斷言恆真，什麼都沒驗到 |
| #657 | 同型（比 production 寬） | **假綠** |
| #673 原 body | 十條 property 複本漏抄 `SystemCallErrorNumber=EPERM`（比 production **嚴**） | **假紅**——製造出 production 不存在的 rc=1，並據此開了一張要求放寬 seccomp 的票 |
| #673 的 repro 步驟本身 | 同上 | **假紅**——第二次踩同一個坑，只是這次是在「修 #643 漏抓」的名義下 |

第四個實例特別值得記錄：它是在**已經知道「手抄子集會出錯」**的前提下，又一次用手抄
子集去驗證，並且錯誤方向翻轉。這**強化**而非削弱本條要求——「機械讀取」不是為了防
某一個特定方向的錯，而是為了讓「驗證環境 ≠ production 環境」在結構上不可能發生。
`require_hardening=True` 的預設與 `UnitReplicaDriftError` 就是這句話的可執行形式。

**成對約束**：`SystemCallFilter=` 與 `SystemCallErrorNumber=` MUST 同進同出
（#677 已以 `permgen.PROFILE_LOCKED_KEYS` 固化）。只帶前者會把「被過濾的 syscall 回
`EPERM`（呼叫方可 fallback）」偷換成「`SECCOMP_RET_KILL_PROCESS`（行程直接死）」，
而症狀（空輸出）一模一樣。#643 已記錄過這一條，#673 仍然重蹈。

驗收矩陣 MUST 至少涵蓋：`{codex, claude, agy} × {該 executor 實際解析到的剖面}`，
每格記錄 rc、stdout 前 80 字、以及**解析到的可執行檔絕對路徑與版本字串**——最後一項
是本票新查到的 PATH 缺口（見背景段）要求的：跑得起來不代表跑的是預期那一份，
而「解析到非預期版本」**不會表現為失敗**。

#### Scenario: 以 property 子集複本取得的宣稱

- **WHEN** 驗證僅以帶部分 property 的 transient unit 執行
- **THEN** 該結果 MUST NOT 被當作「可用」的證據
- **AND** 同樣 MUST NOT 被當作「不可用」的證據——偏嚴的複本會產生假紅

#### Scenario: 落檔 unit 少了加固鍵

- **WHEN** 以 `unit_replica_properties(..., require_hardening=True)` 導出複本而該 unit
  缺少任一加固鍵
- **THEN** MUST 以 `UnitReplicaDriftError` fail-closed，且 stdout 保持空
- **AND** MUST NOT 產出一份「能跑但比 production 弱」的複本

## 非目標

- **不改成非同步 planning**。把 planning 拆成跨 tick 的狀態機（spool + resume）是更
  大的一次重構，且與本票要解的「執行身分」正交。本票維持同步阻塞語意——降權模式
  下阻塞源從子進程換成 `systemctl start --wait`，粒度不變。留給後續票。
- **不改 planning 的 prompt、契約、驗收判準**。`_JSON_OUTPUT_CONTRACT`、
  `required_heading_hint()`、`validate_question_pack` 等一律不動。
- **不重做 #670**（agy probe 的 code fence）。它的**本體**已由 PR #674 修好
  （`strip_code_fence()`／`stdout_excerpt()`／`agy models` 兩欄漂移）；本票要解的是
  上游把診斷吃掉那一層（R6），兩者不重疊。
- **不改剖面表、不改 unit 的加固項**。現行 `EXECUTOR_HARDENING_PROFILE` 實測可用
  （見 R4），本票不預設任何待修的剖面缺口，也不對 `SystemCallFilter` 提出任何放寬。
- **不做 provider 層的獨立**。job 帳號用的仍是與 operator 同一個 provider 帳號；
  三分買到的是檔案系統層隔離，不是 provider 層獨立（`builder-executor-credential`
  的登記表 note 已有完整論證）。
- **不動 unit 檔、不動部署**。本票是設計；unit 的實際改動（憑證 RWP 第二列）在實作
  票，且需 operator 先行裁決。

## 驗收面

- R1–R8 逐條可對應到 `origin/main` 上至少一個具體檔案／函式作為改動錨點
  （`planning_runtime.py`、`job_runner.py`、`launcher.py`、`model_identities.py`、
  `planning.py`、`manager.py`、`trust_root/permgen.py`、`trust_root/registry.py`）。
- R2 的十條防線對應表可逐條轉譯成一個測試或一條 unit 檔斷言。
- R3 的快取指紋可轉譯成「改動任一輸入 ⇒ 快取失效」的參數化測試。
- R6 的三分可轉譯成三個 RED 測試（各自對應一種注入的失敗），且
  `no-heterogeneous-planner` 的 reason 字串可用正規表示式釘住必含拒因表。
- R8 的矩陣是實機步驟，落在 runbook 而非測試；其**輸出格式**（含解析路徑與版本）
  由本 spec 固定。
