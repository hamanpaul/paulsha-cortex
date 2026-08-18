---
status: accepted
work_item: planner-job-downgrade
---

# planner-job-downgrade Design

對應 spec：`docs/superpowers/specs/planner-job-downgrade-spec.md`（issue #672）。
實作切分：`docs/superpowers/plans/planner-job-downgrade.md`。

## 背景與現況查證（main @ `ae089c3`，實機 0818）

程式面（皆已逐條複驗）：

| 事實 | 錨點 |
|---|---|
| planning 的模型呼叫是行程內 `subprocess.run` | `planning_runtime.py:830` `_invoke_json`、`:1070` 預設 runner |
| planning argv 是裸 executor argv，無 `systemd-run`／無 shim | `planning_runtime.py:43` `_planning_argv` |
| 呼叫端是 daemon（`User=cortex-manager`） | `manager_daemon.py:970`、`:1241` |
| reviewer 已接上 job runner，planner 沒有 | `launcher.py:1203` `_is_review_persona` → `:1239` `_job_role` |
| repo 自己已把這條記成逾期項 | `job_runner.py:405-408`、`permgen.deferred_run_dependencies()` 第 1／3／4 項 |
| 帳號註記與行為矛盾 | `getent passwd cortex-manager` → `… no model code …` |

部署面（本票以唯讀方式複驗）：

| 事實 | 觀測 |
|---|---|
| 部署為 B 案模板模式 | `/opt/cortex/etc/cortex-manager.env`：`PSC_JOB_RUNNER=systemd-template` |
| primary planner 是 codex | 同檔：`PSC_MANAGER_EXECUTOR=codex` |
| 六份 job 模板 unit 已安裝 | `/etc/systemd/system/cortex-{job,reviewer-job,gate-job}[-jit]@.service` |
| reviewer unit 的 RWP 只有兩條 | `…/cache`、`…/coordinator/review-verdicts`——**不含** `.codex/auth.json` |
| reviewer unit 無執行時間上限 | 無 `RuntimeMaxSec=`／`TimeoutStartSec=` |
| planner 憑證已落位 | `.codex/auth.json`（檔 job-owned 0600／目錄 root-owned）、`.gemini → cache/gemini`（root-owned symlink） |
| **八份 unit 全帶 `SystemCallErrorNumber=EPERM`** | `cortex-reviewer-job@.service:148`、`cortex-reviewer-job-jit@.service:162`、`cortex-job-jit@.service:162`、`cortex-manager.service:89`——被過濾的 syscall 回 `EPERM` 而非 `KILL_PROCESS`，V8 走 fallback（見 D6） |
| Manager unit 帶 `MemoryDenyWriteExecute=yes` | `cortex-manager.service:83`——這是 node 型 executor **在 Manager 行程內**結構性不可用的真正原因（與 seccomp 無關） |
| `jit` 剖面與 `strict` 的唯一差異是 MDWE | `cortex-reviewer-job-jit@.service:156` `MemoryDenyWriteExecute=no`，unit 檔自己逐字寫明「這是本檔與 strict 剖面唯一的差異」 |
| **PATH 缺口** | env 檔無 `PSC_REVIEWER_PATH`，Manager unit 無 `Environment=PATH=` ⇒ job 的 PATH＝PID 1 預設（`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin`）；`codex` 解到 `/usr/bin/codex`＝`codex-cli 0.42.0`，toolchain 那份是 `0.147.0`；`claude`／`agy` 在該 PATH 上不存在 |

## Decisions

### D1 執行後端抽象成 `PlanningInvoker`，而不是在 `_invoke_json` 裡塞 `if degraded:`

`planning_runtime` 目前把「怎麼跑一個 executor」與「怎麼把輸出變成 JSON」揉在同一支
`_invoke_json` 裡。直接在裡面分岔會產生兩個問題：(i) 現行 in-process 路徑的十條防線
（sandbox 複製、雙向快照、drift 收容）有一半在 job 模式下不適用，混寫會讓「哪幾條
在哪個模式下生效」變成要靠讀 `if` 才知道；(ii) probe 與四個 adapter 共用 `_invoke_json`，
分岔寫在裡面等於每個呼叫端都要重新論證一次自己走的是哪條路。

定案：抽出一層**只負責「拿 identity + prompt，回傳 stdout 與退出狀態」**的介面：

```python
class PlanningInvocation(Protocol):
    identity: ModelIdentity
    prompt: str
    purpose: str        # "probe" / "questioner" / "secondary" / "integrator"，只進 instance 名與診斷
    timeout_seconds: int

class PlanningInvoker(Protocol):
    def run(self, invocation: PlanningInvocation) -> PlanningOutcome: ...
    # PlanningOutcome: (stdout, stderr, returncode, diagnostics)
```

兩份實作：

- `InProcessPlanningInvoker`——現行 `_invoke_json` 的**前半段**原封搬進來（sandbox 複製、
  `cwd=sandbox`、雙向快照、drift 收容、hermetic claude env、`subprocess` 逾時）。
  行為逐字不變，既有測試不改一行。
- `JobPlanningInvoker`——見 D3／D4。

JSON 抽取（`_extract_json`、`_find_json_object`、envelope 處理）留在共用層，兩個
invoker 都吃同一份——這是本 repo 反覆買到教訓的地方（#401、#516、#520 都是「同一件事
兩份真相」）。

選擇點只有一個：`job_runner.resolve_runner_mode(os.environ)`，與 launcher 同一支函式；
非法值在該函式已 fail-closed。**刻意不新增 `PSC_PLANNING_INVOKER`**——第二個開關的
失效模式是「以為降權了、其實沒有」，而那種失敗看起來是成功的。

### D2 十條防線的逐條對應表（本設計的核心交付）

| # | 現行機制（in-process） | 它買到什麼 | job 側對應 | 判定 |
|---|---|---|---|---|
| D-a | `tempfile.TemporaryDirectory(prefix="cortex-planning-")` | 一次性、呼叫結束即銷毀 | Manager 在 worktree pool 下建 per-invocation scratch（`<pool>/<instance>`），呼叫結束由 Manager `rmtree`；`CollectMode=inactive-or-failed` 讓 unit 自己也不留 | **等價** |
| D-b | `_copy_planning_sandbox(worktree, …)` 整棵複製 repo | 讓模型有一份可讀的樹，且不是 operator 本尊 | 見 U-1：預設**不複製**（空 scratch），因為四個 adapter 的輸入全部已在 prompt 內 | **裁決中**（U-1） |
| D-c | `cwd=sandbox` | 模型的 cwd 不是 operator 樹 | spec 的 `working_directory` = scratch；shim 依 spec chdir | **等價** |
| D-d | 呼叫後 `_tree_snapshot(sandbox) != sandbox_before` ⇒ 失敗 | 偵測模型違反 read-only 契約 | 見 D-d 專段（下方） | **升級或退步，取決於 U-2** |
| D-e | 呼叫前後 `_tree_snapshot(worktree)` 比對 operator 樹 | 偵測經絕對路徑寫 operator 樹 | `ProtectSystem=strict` ＋ RWP 不含 `repo-source-tree` ⇒ **kernel 直接擋** | **升級**（偵測 → 阻擋） |
| D-f | `_contain_operator_drift`：唯讀 diff → 備份 → 報告 → 三道閘門的逐路徑還原 | drift 的 evidence 與不可逆抹除的保命索 | job 模式下 operator 樹不可能被 job 改，這一整套對 job 路徑是 dead code；**保留在 in-process invoker** 供 direct 模式使用 | **等價（範圍縮小）** |
| D-g | `_seed_hermetic_claude_env`：一次性 `CLAUDE_CONFIG_DIR`，隔離 operator 的 plugin／hooks／MCP／user CLAUDE.md | 避免 operator 的 `~/.claude` 注入讓模型跑題 | job 的 HOME 是 `cortex-reviewer-planner`；`ProtectHome=yes` 讓 `/home` 整個不可見；unit 的 env 白名單不含 `CLAUDE_CONFIG_DIR` | **升級**（從複製憑證＋改 env，變成帳號隔離） |
| D-h | `subprocess.run(timeout=…)` | 呼叫必然在上限內返回 | Manager 側 `Popen.wait(timeout)` → 逾時則 `systemctl stop <unit>`（見 D4） | **等價，但需新程式與實測** |
| D-i | `capture_output=True` 取 stdout | 取回模型輸出 | spec 的 `log_path`（Manager-owned，Manager 先 truncate、shim 降權後以 O_APPEND 接管）；Manager 讀檔 | **等價**（reviewer 已在用同一條通道） |
| D-j | codex 的 `-o <temp>/last.json` 第二輸出候選 | stdout 被 CLI envelope 污染時的乾淨來源 | 落點在 job-owned scratch，Manager 讀不到 ⇒ `_extract_json` 的雙候選退成單候選 | **退步（小）**，見 R-2 |

**D-d 專段**——「模型是否弄髒了自己的拋棄式 sandbox」：

- 現行做法是 Manager 在呼叫前後各對 sandbox 做一次 `_tree_snapshot`。降權之後
  scratch 是 job-owned（`UMask=0077` ⇒ 0700），而 #623 已實測「Manager 走不進
  builder 的樹」——同一條結論適用。所以**這個偵測在 job 模式下由 Manager 執行不可行**。
- 讓 job 側 wrapper 自行 snapshot 再寫進 log 是**明確拒絕**的路：那是被驗方在自己的
  進程裡產生自己的驗收證據（#628／#540 的原則），而且它能證明的東西恰好是它最有
  動機造假的東西。
- 剩下兩條路：
  1. **接受退步**：不做這個偵測。論證是 scratch 每次呼叫新建即刪、job-owned、不承載
     任何 durable state，弄髒它不產生任何後果；失去的只是「模型嘗試寫」這個**行為
     訊號**（今天它會讓該次呼叫 fail-closed）。
  2. **讓它結構上不可能**：把 scratch 設成 Manager-owned 且**不在** job unit 的 RWP
     內 ⇒ 模型連寫都寫不進去，偵測需求消失。代價是 executor 需要一個可寫落點
     （agy 的 `log_dir`、codex 的 `-o`），而 unit 已有 `PrivateTmp=yes`，job 拿得到
     一個私有 `/tmp`——把那些落點指過去即可。
- **本設計傾向 (2)**，因為它把一條「偵測」變成「不可能」，與 D-e 同一個方向；但它
  需要逐 executor 實測「cwd 唯讀時能不能跑」。這是 U-2，交 operator 裁決。
  若 operator 選 (1)，本設計要求 R-1 這條退步被逐字寫進 runbook 與 CHANGELOG，
  不得只活在程式註解裡。

### D3 輸出通道：沿用 reviewer 已經走通的「Manager-owned log ＋ shim O_APPEND」

不新開通道。`launcher.launch()` 在模板模式下已經：Manager 先 `Path(log_path).write_bytes(b"")`
把檔案建成 Manager-owned 並清空，spec 帶 `log_path`，shim 在降權**之後**以 O_APPEND
接管——這正是「Manager 讀得到、job 只能追加」的形狀。planning 沿用它。

三個附帶約束：

1. planning 的 wrapper **不跑 gate、不產 bundle、不寫 verdict、不寫 sentinel**。
   `_is_review_persona()` 為真時 `commit_bundle` 已是 `None`；`write_sentinel=not degraded`
   已把 sentinel 交給 Manager 側記帳 shell。planning 的 wrapper 因此應該退化成
   「只有模型 argv」一段——本設計要求它**顯式**只有那一段，不靠既有旗標碰巧為 None。
2. log 內容 MUST 只有模型的 stdout／stderr。任何 wrapper 自產的文字都會污染
   `_extract_json` 的輸入（本 repo 已有先例：`build_wrapper_script` 把 gate 階段的
   輸出導向 `/dev/null` 就是為了不污染 terminal evidence）。
3. `_extract_json` 現行接受兩個候選（`output_path` 的內容優先、再 stdout）。job 模式下
   只剩 stdout（D-j 的退步）。若實測顯示 codex 的 stdout 在 job 環境下不可靠，
   替代方案是再開一格「planning result spool」，形狀逐條比照 `review-verdict-spool`
   （Manager-owned 目錄、job 只有 `wx` 無 `r`、收割後封口）。**那是新開一條 job→Manager
   的寫入面**，因此列為 U-3 交裁決，不由本設計預先決定。

### D4 逾時：Manager 側 `wait(timeout)` → `systemctl stop`，不靠 unit 的 `RuntimeMaxSec`

兩條路：

- **unit 側 `RuntimeMaxSec=`**：加在 root-owned 模板 unit 上。優點是 kernel 保證、
  job 動不了；缺點是**上限變成部署常數**，而 planning 的四種呼叫（probe 45s、
  questioner／secondary／integrator 120s）本來就有不同的上限，而 job spec
  **結構性禁止攜帶任何 property**（`SPEC_FORBIDDEN_KEYS`）——那條禁令是對的，不該為
  逾時鑽洞。
- **Manager 側**：`Popen.wait(timeout=…)`，逾時則以 `systemctl stop <unit>`（polkit
  已放行 `stop`），再落 `planning-job-timeout`。

定案走 **Manager 側**，並**額外**建議 operator 在模板 unit 上加一條寬鬆的
`RuntimeMaxSec=`（例如 1800s）作為第二層保險——那一層不需要精確，只需要保證
「Manager 掛掉之後不會留下永遠在跑的模型 job」。這條屬部署面建議，不是本設計的
硬性要求。

停止之後 MUST 確認 unit 已離開 active：下一次同 `job_id` 的呼叫會撞
`prepare_systemd_template` 的 `job-runner-template-instance-busy`，而那個錯誤訊息
與「逾時」毫無關係，會把下一輪的排查完全帶偏。

### D5 probe 快取：Manager-owned durable state，指紋含執行後端，fail-closed

**成本現況**（本票量出來的）：`build_production_planning_runtime()` 對每個
planning-capable identity 各跑一次 probe，而 `_probe_identity` 每次都做**兩次整棵 repo
的 `copytree`**（baseline ＋ checkout）；agy 走的 `probe_agy_capability` 則是**兩次
CLI 呼叫**（`agy models` ＋ smoke）。這個函式由 `manager.run_auto_claim_scan()`
（periodic tick，實機 `PSC_MANAGER_INTERVAL_SECONDS=600`）與 `apply_work_action()`
兩條路徑呼叫。搬到 job 之後，每個 probe 就是一個 unit 實例，agy 是兩個。

**落點**：新增登記表資產 `planning-probe-cache`，路徑 `<coordinator_root>/planning-probe-cache.json`，
Manager-owned `0600`，writers／readers 只有 `Principal.MANAGER`。**刻意不進任何 job
模板 unit 的 RWP**：job 不該知道別的 provider 的探測結果，更不該寫得動它——快取一旦
可由 job 寫，「這個 provider 是 ready 的」就變成模型可以自證的東西。

**指紋（cache key 的第二半）**——任何會改變 probe 結論的輸入都要在裡面：

| 輸入 | 為什麼 | 取法 |
|---|---|---|
| `PSC_JOB_RUNNER` 的值 | direct 與 job 的執行環境（PATH／HOME／憑證／seccomp／MDWE）完全不同 | 字面值 |
| executor 可執行檔 | 換版就是換行為（實機同名兩份差 105 個小版本） | 以該角色的 PATH 解析出的**絕對路徑** ＋ `st_dev/st_ino/st_size/st_mtime_ns` |
| 憑證檔 | refresh／換帳號 | `st_size/st_mtime_ns`（**不讀內容**，避免把 token 帶進雜湊的任何中間狀態） |
| 加固剖面**與 seccomp 維度** | #643（MDWE 與 V8 互斥）＋ #677（seccomp 過濾語意是剖面**之外**的第二個維度，`PROFILE_LOCKED_KEYS` 兩剖面逐字相同）——**只放剖面名涵蓋不到第二維** | `resolve_hardening_profile(executor)` ＋ **模板 unit 檔本身**的 `st_size/st_mtime_ns` |
| roster | overlay 改了候選就變了 | `load_model_identities()` 解析結果的 canonical JSON 雜湊 |

模板 unit 檔進指紋是刻意的：**任何**讓 operator 重跑產生器落新 unit 的改動（RWP 增列、
加固項調整、新增 executor），在落檔那一刻就讓全部快取自動失效並重探，不需要任何人
記得清快取。這是把「部署動作」與「快取失效」綁成同一件事——本 repo 已經有太多
「機制對了但沒人記得觸發」的例子。

**TTL**：ready 與 not-ready 分開（建議預設 `ready=3600s`、`not_ready=300s`）。
理由：失敗要快速重試（暫時性的服務錯誤、限流、以及模型輸出的隨機不從，短時間內
就會自己好），成功不需要頻繁重確認（重確認就是一個 job 的成本）。
兩者由 env 覆寫，實際值列為 U-6。

**probe 本體以 PR #674 修後的形狀為準。** #674 已讓 `probe_agy_capability()`
`strip_code_fence()`、失敗帶 `stdout_excerpt()`（前 200 字、空白壓成單一空格、空輸出
標 `<empty>`），並修掉 `agy models` 改成 `id\tDisplay Name` 兩欄之後造成的 100%
`model-not-listed`。快取的「失敗診斷」欄位因此**直接沿用** `CapabilityProbe.diagnostic`
——不要在快取層再造一份自己的節錄邏輯（那就是第二份真相）。

**ledger 形狀沿用 `not_claimable`（PR #675）的既有先例**，不另立一套：
`schema` 版本字串 ＋ `items` 以穩定 key 索引 ＋ 每筆帶 `first_observed_at`／
`last_observed_at`／`observations`（operator 因此看得出「這個 provider 掛多久了」）＋
條件解除時自動清除。原子寫入（temp ＋ `os.replace` ＋ 目錄 fsync）也照抄
`not_claimable._save()`。

**一處刻意的不同**：`not_claimable.load_ledger()` 對壞掉的檔案 **raise**（理由是
「靜默當成空的等於把盲區再造一次」）。probe 快取**不得** raise——它壞掉時若把整個
planning 拖垮，那是一份輔助紀錄取得了它不該有的否決權。改為「視為 miss ＋ 落一筆
可辨識的診斷」，而 fail-closed 的實質仍在：**它永遠不會因為讀不到而回答 ready**。
兩者是同一條原則（不得靜默產生有利答案）在不同後果下的兩種實作，設計上必須寫明
差異，否則下一個人會照抄 raise。

**失效與錯誤時的行為（fail-closed）**：

- 檔案不存在／JSON 壞掉／schema 版本不符／指紋不符 ⇒ 視為 **miss**，重探。
- 重探失敗 ⇒ **not ready**（與今天相同）。
- **絕不**因為「上次是 ready」而在無法重探時沿用 ready。
- 快取損毀 MUST 落一筆可辨識的 log（`planning-probe-cache-unreadable`），與
  「probe 失敗」分開——否則會出現「快取檔壞了，症狀卻報成 provider 不可用」。

**fail-closed 的代價（明講）**：快取失效的那一輪 tick，planning 會多付一批 job 的
時間；若那一刻環境剛好壞了（憑證過期、PATH 缺口未補、toolchain 換版換壞），那一批
job 會全滅，於是 planning 從「上次成功」直接掉成 `no-heterogeneous-planner`。
fail-open 可以掩蓋這件事、讓 planning 繼續跑，但代價是「憑證過期／unit 換版之後仍
宣稱 ready」——那正是本 repo 已經記錄多次的「綠燈不承載語意」。選 fail-closed，
並由 D8 的拒因表讓掉下去的原因當場可見。

**快取內容**：除 `ready: bool` 外，MUST 存失敗側的完整診斷（`reason`、`diagnostic`、
`returncode`、`stdout_prefix`（≤200 字）、`unit`、`hardening_profile`、`resolved_binary`、
`binary_version`），供 D8 的拒因表消費。這一條讓「快取」同時變成「上次到底怎麼失敗的」
的唯一可查落點——今天這份資訊在 `_failed_agy("malformed-output")` 之後就蒸發了（#670）。

### D6 剖面路由：零新增判定點，且現行剖面表**實測可用**、無前置修正

`JobPlanningInvoker` 取得剖面的唯一途徑是
`job_runner.prepare_systemd_template(env, job_id=…, executor=identity.executor,
role=JOB_ROLE_REVIEW)`。planning 側**不持有**任何 executor→剖面的對應表。

`prepare_systemd_template` 已經滿足本 spec 需要的每一條：`executor` 必填無預設、
未登記 executor fail-closed（`cg` 目前刻意不在表內，那是正確結果不是缺口）、
`SPEC_FORBIDDEN_KEYS` 在寫端與讀端各擋一次剖面欄位、兩份 unit 都是 root-owned。

**本設計原本寫著「假設 #673 已修」，這個前提是錯的，已移除。** #673 原 body 主張
`SystemCallFilter=@system-service` 讓 codex／copilot 在全部 job unit 下靜默 rc=1；
該主張已由開票者自行更正撤回，#673 由 **PR #677** 以「不放寬任何 syscall」收尾。
更正後的事實（本票獨立複驗）：

- 八份 unit（六份 job 模板 ＋ manager ＋ monitor）**全部帶
  `SystemCallErrorNumber=EPERM`**——`cortex-reviewer-job@.service:148`、
  `cortex-reviewer-job-jit@.service:162`、`cortex-job-jit@.service:162`、
  `cortex-manager.service:89`——而且從權限產生器落地的第一個 commit 起就在。
- 有這一條時，被過濾的 syscall 回 `EPERM` 而非 `SECCOMP_RET_KILL_PROCESS`，
  V8 走 fallback ⇒ **codex／copilot 照常啟動**。
- 真 unit 的**完整** property 集合下實測：codex／copilot 在 `jit` 剖面 rc=0，
  claude／agy 在兩種剖面皆 rc=0。**預設派工路徑沒有壞。**
- 實際被過濾的是 `pkey_alloc`（`@pkey`），不是原先猜測的 `landlock_*`／`seccomp`
  （`@sandbox`）；加 `@sandbox` 對症狀完全無效。
- 誤判來源：repro 手抄十條 property、漏抄 `SystemCallErrorNumber=EPERM`，比 production
  **更嚴格**，於是製造出 production 不存在的 rc=1。這條教訓寫成硬規則見 **D13**。

**結論**：現行 `EXECUTOR_HARDENING_PROFILE`（codex／copilot → `jit`，claude／agy →
`strict`）就是對的，planner 上 job 之後**不需要任何剖面面的前置修正**。
`jit` 剖面與 `strict` 的唯一差異仍是 `MemoryDenyWriteExecute=no`（#643 的既有結論，
unit 檔自己逐字寫明「這是本檔與 strict 剖面唯一的差異」），那一條與 V8 的 JIT 互斥
是真的，本設計不動它。

**#677 另外建立了一個本設計必須消費、而不是重新發明的東西**：seccomp 過濾語意是
**剖面之外的第二個維度**。

- `permgen.SECCOMP_FATALITY_KEY = "SystemCallErrorNumber"`、
  `PROFILE_LOCKED_KEYS = {"SystemCallFilter", "SystemCallErrorNumber"}`——這兩個鍵
  **兩份剖面逐字相同**，剖面之間不分岔。
- `ToolchainProgram.filtered_syscalls` 記錄實機量到、有 kernel audit 背書的被過濾
  syscall；`filtered_syscall_surfaces()` 機械導出「哪個程式在哪個加固面上會撞到什麼」。
  現況四筆（`codex`／`copilot` 在自己的 jit 剖面、`srt` 在 claude 的 strict 剖面、
  `openspec` 在 Manager unit），**全部 `fatal=False`**——因為 `SystemCallErrorNumber=EPERM`。
- `seccomp_filter_is_fatal(table)` 把「被過濾會不會殺行程」變成一個可查詢的函式；
  `_validate_seccomp_tolerance()` 在 import 時強制，承重的那條因此**不可能被靜默拿掉**。

對本設計的兩個直接影響：(i) D5 的快取指紋**不能只放剖面名**——seccomp 維度與剖面
正交，只有把**模板 unit 檔本身**放進指紋才涵蓋得到（本設計原本就是這樣，#677 讓這個
選擇有了明確的理由而不只是保守）；(ii) D8 的 `executor-silent-exit` 診斷 MUST 帶上
`seccomp_filter_is_fatal()` 的結果——那是「這次空輸出到底該不該懷疑 seccomp」的
機械答案，而 #673 整張票就是因為沒有這個答案才走偏。

剖面表若日後因其他理由改動，本設計受影響處：

| 改動 | 本設計受影響處 |
|---|---|
| 新增 executor 進 `EXECUTOR_TOOLS`／`EXECUTOR_HARDENING_PROFILE` | **零改動**（那張表的真相在 permgen，本設計只消費 `resolve_hardening_profile`） |
| 新增剖面名（例如將來真有第三種加固形態） | `TEMPLATE_UNIT_SUFFIX_BY_PROFILE` 多一個值 ⇒ 本設計的**驗收矩陣多一列**；程式仍零改動 |
| 任何 unit 檔內容變更 | D5 指紋的「模板 unit 檔」隨之改變 ⇒ 快取自動全失效重探（正確行為） |

換句話說，本設計對剖面表的耦合**只在驗收矩陣與快取失效**，不在程式結構——這就是
R4「不新增第二個判定點」買到的東西。

### D7 憑證：把 0818 已部署的事實 codify 進登記表，不重造 #671 的機制

0818 的部署在**功能上足夠**：`.codex/auth.json` 讓 codex 有登入態；`.gemini →
cache/gemini` 讓 agy 的可寫狀態樹落在已在 RWP 內的 `cache`，因此
`ProtectSystem=strict` 下 agy 建得出 log／state（`agy models` 列出
`gemini-3.1-pro-high`、capability smoke 逐位元等於 expected）。

**治理上不足**，兩個缺口：

1. reviewer 模板 unit 的 `ReadWritePaths=` 不含 `.codex/auth.json` ⇒ 憑證讀得到、
   **改不了** ⇒ token 過期那天 refresh 靜默失敗。這是
   `permgen.deferred_run_dependencies()` 第一項逐字描述的逾期項，它的 `disposition`
   已經寫好「補登記表第二列（產生器一行都不必改）」。
2. `.gemini` 那條 root-owned symlink 是手動落位的，登記表不知道 ⇒ 換機器部署或重跑
   產生器不會出現。

**#671 已經把當年的阻礙拆掉了，不要重造**：

- `permgen.IN_PLACE_CONTENT_WRITE_ASSETS`——葉檔資產的 `ReadWritePaths` 掛在**檔案
  本身**而非折算成父目錄。憑證正是這一族（檔 job-owned、目錄 root-owned）。
  補進來只要把 `reviewer-planner-executor-credential` 加進這個集合。
- `permgen.inapplicable_home_anchored_assets()`——掛在「本方案不存在的帳號」HOME 下
  的資產機械地不進 RWP。這正是 #640 當年「不敢登記第二份憑證」的唯一理由
  （二分部署下該路徑不存在 ⇒ systemd 對不存在的 RWP 目標讓 unit 起不來）。
  該理由**已經失效**，登記第二列不會再弄壞二分部署。
- `required_write_targets()` 已經同時吃 `IN_PLACE_CONTENT_WRITE_ASSETS`、
  `inapplicable_home_anchored_assets()` 與 `RETIRED_JOB_WRITE_ASSETS` 三者，
  補一列不需要改產生器。

**部署順序**（重要，unit 註解已寫明）：憑證檔不存在時，帶著該 RWP 的 unit 會**起不來**
（那是刻意的 fail-closed）。因此順序必須是「憑證先落位 → 再重跑產生器落新 unit →
再 `daemon-reload`」。0818 已完成第一步，因此這張票的部署風險比 #640 當時低。

**agy 狀態樹的表達方式**有兩個選項，列為 U-7：
(a) 把 `.gemini → cache/gemini` 登記成一個 symlink 類資產（登記表目前沒有這個 kind，
    要新增）；
(b) 不登記 symlink，改在 job env 上設 `GEMINI_*`／`XDG_*` 讓 agy 的狀態樹直接落在
    `cache` 底下（若 agy 支援）——那樣就不需要 symlink，`cache` 已在 RWP 內。
(b) 較乾淨（少一種資產 kind），但需要先查 agy 的路徑解析順序。

**雙 provider 憑證**（#668 G2）：planner 帳號要有異質性，就得同時持有兩個
independence domain 的憑證。`PathLayout.executor_credential_relpath` 目前是**單一
部署決定**（`.codex/auth.json`），一個帳號只表達得了一份。要表達兩份就得把它擴成
per-(account, executor) 的表。**這是新的安全決策**：擴大的是該帳號被攻陷時的
provider 曝險面（同時失去兩邊的 token）。本設計不做這個決定，見 U-4／U-5。

順帶：`permgen.deferred_run_dependencies()` 的第三項
（`reviewer-planner-codex-hooks`，`asset_paths()` 把 `codex-hooks` 寫死在 builder HOME 下）
與本題同族，它的 `disposition` 已寫「與 reviewer 憑證同一張票處理」。實作票 D 一併收。

### D8 錯誤語意：三分 ＋ 逐候選拒因表，讓「格式問題被報成拓撲問題」結構上不可能

**現況為什麼會誤報**：`select_secondary_planner()` 走完候選後回
`SecondarySelection("needs_human", "no-heterogeneous-planner", None)`——一個**沒有任何
附加資訊**的字面值。而每個候選被跳過的真正理由（同 domain？probe 沒 ready？ready
但身分不符？）全部在迴圈裡被 `continue` 吃掉。#670 的實測就是這樣：agy 的 stdout 是
完全正確的 JSON、只是被 code fence 包住，結論卻是「沒有異質 planner」。

**三分**（新增具名族，皆帶結構化 detail）：

| 族 | 判準 | 來源 | classification |
|---|---|---|---|
| `planning-job-start-failed` | job 起不來 | `prepare_systemd_template` 的 `JobRunnerError`（polkit／模板未安裝／shim 不可執行／spool 不可寫／instance busy）、`confirm_template_instance_started` 逾時、spec 寫入失敗 | `environment` |
| `planning-executor-failed` | job 起來、executor 非零退出或零輸出 | Manager 側記帳 shell 寫下的 rc ＋ log 尾段 | `environment` |
| `planning-output-malformed` | executor 正常退出、輸出不符契約 | `_extract_json` 既有的例外（已帶 stdout 前 160 字） | `content`（既有 transient-service 例外仍適用） |

`planning-executor-failed` 的 **`executor-silent-exit` 子類**：rc≠0 且 stdout 與 stderr
皆空。這是本 repo 稱為「連錯誤訊息都沒有」的那一種——歸因會落到模型、prompt、逾時或
憑證，而不會落到執行環境。它 MUST 在 reason 裡指名 `unit`、`hardening_profile`、
`resolved_binary`——因為那三個就是唯一的線索來源。

（#673 原 body 曾把這一類的一個實例歸因到 `SystemCallFilter`，該歸因已被開票者更正
撤回，見 D6。但「rc≠0 而完全無輸出」這個**類別**確實存在且值得具名——這正是它難查的
證據本身：一個無輸出的失敗，連要不要懷疑執行環境都判斷不了。本子類因此保留，
但不對成因預設任何特定 syscall。）

**這一格的診斷 MUST 帶 `permgen.seccomp_filter_is_fatal()` 的結果**（PR #677 提供）。
理由是這條子類唯一的資訊價值就在「該往哪個方向查」，而 seccomp 是否致命正是最容易
被誤猜、也最容易機械回答的那一維：`SystemCallErrorNumber=EPERM` 在時答案是「不該懷疑
seccomp」，不在時才是「該懷疑」。#673 整張票走偏，就是因為當時沒有任何地方回答得了
這個問題——現在有了，本設計必須把它接進診斷，而不是讓下一個人再猜一次。
同理，`filtered_syscall_surfaces()` 已經逐筆記錄「哪個程式在哪個加固面上撞到什麼」，
診斷若能指出「本 executor 在本剖面上的已知過濾項為 X 且 fatal=False」，
`executor-silent-exit` 就從一個死路變成一條有方向的線索。

**拒因表**：`SecondarySelection` 增加一個欄位

```python
@dataclass(frozen=True)
class CandidateRejection:
    executor: str
    model_id: str
    domain: str
    reason: str        # same-domain / probe-absent / probe-not-ready / probe-identity-mismatch
    diagnostic: str    # probe 的 diagnostic（含 stdout 前 200 字、rc、unit）
```

`no-heterogeneous-planner` 從**結論**變成**結論 ＋ 每個候選為什麼落選**。
`run_heterogeneous_brainstorm` 把它渲染進 `BrainstormResult.reason`，例如：

```
no-heterogeneous-planner (agy/gemini-3.1-pro-high[google]: probe-not-ready
malformed-output ```json{"capability":"cortex-plan-…; claude/…[anthropic]:
probe-not-ready planning-job-start-failed job-runner-template-unit-missing;
codex/…[openai]: same-domain)
```

**這一條就是「讓誤報不可能」的機制本身**：只要拒因表是必填且渲染在 reason 裡，
「格式問題」與「拓撲問題」在同一個字串裡就是兩個不同的欄位，讀的人不需要重跑六遍
才發現。

**分類改判**：`_classify_planning_failure()` 目前對 `no-heterogeneous-planner` 一律
落 `content`，而 `content` 在 `_resume_decision` 一律不浮現 `recover-planning` ⇒ 死路。
改成：拒因表中**只要有一條是 environment 級**（job 起不來、executor 死、probe 快取
損毀），整體改判 `environment`，讓 recover-planning 有路。這與 #416／#533／#554 已經
建立的三條例外是同一個模式（`_is_planning_authority_residue_failure`、
`_is_planning_transient_service_failure`、`_is_planning_worktree_drift_failure`），
不是新發明。

### D9 instance 命名與併發：一次 planning 呼叫 = 一個 unit 實例

`prepare_systemd_template` 用 `template_instance_id(job_id)` 算 instance，並在起動前
檢查同名 unit 是否 active（因為 `systemctl start` 對已 active 的 unit 會**靜默回 0**）。
planning 的一輪 brainstorm 會連續起 questioner → secondary → integrator 三個呼叫，
加上 probe，因此 `job_id` MUST 每次呼叫唯一。

定案格式：`plan-<run_id 前 12 字>-<purpose>-<序號>`，其中 `purpose` ∈
{`probe`, `questioner`, `secondary`, `integrator`}，序號解決重試。
`job_workspace.JOB_SEGMENT_RE`（`[A-Za-z0-9][A-Za-z0-9_.-]{0,62}`）是形狀的唯一真相，
命名必須先過它。

`purpose` 進 instance 名是刻意的：`systemctl list-units 'cortex-reviewer-job@*'` 的輸出
因此直接說得出「這一批 job 在做什麼」，不需要回頭查 spec。它**不**進 spec 的任何
決策欄位，只是識別字串。

probe 的 `run_id` 在非 daemon 呼叫端是 `"ephemeral"`（`build_production_planning_runtime`
的預設值），因此 probe 的 instance 名 MUST 另外帶時間戳或隨機後綴，否則兩個並行的
probe 會互撞 `instance-busy`。

### D10 PATH：本票新查到的部署缺口，是實作票 C 的前置

實機 `/opt/cortex/etc/cortex-manager.env` 沒有 `PSC_REVIEWER_PATH`，Manager unit 也沒有
`Environment=PATH=`。`build_job_env()` 的 PATH 來源是
「`manager_env["PATH"]`，被 `PSC_<ROLE>_PATH` 覆寫」——覆寫不存在，所以 job 拿到的是
PID 1 的 `PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin`。

實測後果：

| executor | 在該 PATH 上解析到 | 版本 | toolchain 那一份 |
|---|---|---|---|
| `codex` | `/usr/bin/codex` | `codex-cli 0.42.0` | `0.147.0` |
| `copilot` | `/usr/bin/copilot` | （未量） | toolchain 另有一份 |
| `claude` | **不存在** → rc=127 | — | `/opt/cortex/toolchain/bin/claude` |
| `agy` | **不存在** → rc=127 | — | `/opt/cortex/toolchain/bin/agy` |

reviewer 模板 unit 的註解已經預先寫過這件事該怎麼修（「真正的來源是 Manager 端
root-owned EnvironmentFile 裡的 `PSC_REVIEWER_PATH=/opt/cortex/toolchain/bin:…`」），
只是那一行從未落到 env 檔裡。#671 的 `permgen` 已有 `job_path_value()` 這類
「產生器出值、operator 落進 root-owned EnvironmentFile」的形態，補這一條不需要新機制。

**這條同時影響今天的 reviewer job**（不只是未來的 planner），因此它應該是一張獨立的
部署面票，並列為實作票 E 的前置。它也是 D8 為什麼要把 `resolved_binary` 與
`binary_version` 放進失敗診斷的直接理由：`claude` rc=127 與「模型不聽話」在
blocking reason 上必須長得不一樣，而**「跑起來了但跑的是 0.42.0 而不是 0.147.0」
甚至不會失敗**——它會安靜地產出一份用舊版 CLI 跑出來的結果。這一種只有把版本字串
記進診斷才看得見。

### D11 分階段 land：能，但「切換」那一刻是一次到位

**能分階段**的理由：`PSC_JOB_RUNNER` 是既有開關，`direct` 分支逐字保留現行行為。
因此票 1／2／3（診斷、抽象、快取）可以先 land 並在 direct 部署上獨立產生價值：

- 票 1（拒因表 ＋ 三分）：今天的 `no-heterogeneous-planner` 立刻開始講真話。
  **不依賴任何部署面改動。** PR #674 已經把 probe 那一端的診斷補齊
  （`stdout_excerpt()`），票 1 要做的是讓那份診斷**活著抵達** blocking reason
  ——兩者剛好接得起來，而缺任何一半都還是查不出原因。
- 票 2（invoker 抽象）：純重構，direct 路徑逐字等價。
- 票 3（probe 快取）：direct 模式立刻受益——省掉每輪 tick 每個 identity 兩次整棵
  repo 的 `copytree`。

**不能分階段**的是切換本身：一個部署上不能「一半 planning 走 job、一半走 in-process」。
理由是 probe 快取的指紋含 `PSC_JOB_RUNNER`，混用會讓兩種語意的結論在同一輪裡並存，
而 `select_secondary_planner` 分不出來。因此票 5（`JobPlanningInvoker`）land 之後，
生產部署的切換是「改 env → 重啟 → 全部 planning 走 job」一次到位。

**中間狀態的可運作性**：票 5 land 但生產仍為 direct 的期間，系統完全可運作
（direct 路徑不變）。票 5 land 且切換之後、票 4（憑證 codify）之前，系統也可運作
——0818 手動落位的憑證在**這台機器上**有效，只是換機器會壞。因此票 4 與票 5 沒有
硬性先後，但票 4 應盡量在切換前完成，否則「這台機器可以、下一台不行」會變成一條
沒有票追蹤的隱性狀態。

### D12 明確不做：非同步 planning 狀態機

把 planning 拆成「起 job → 記錄在 spool → 下一個 tick 收割」的狀態機，會讓 Manager
的 tick 不再被 planning 阻塞。這是對的方向，但：(i) 它與本票要解的「執行身分」正交；
(ii) 它需要 planning 的三段呼叫（questioner→secondary→integrator）各自有 durable 的
中間態，而那是 `WorkflowRun` schema 的改動；(iii) 它會讓本票的驗收面爆炸。

本票維持同步：降權模式下阻塞源從「子進程」換成「`systemctl start --wait` 的 client」，
粒度不變（今天 `_invoke_json` 一樣阻塞 tick 最多 120s × 3）。多出來的只有 job 啟動
延遲（`DEFAULT_START_TIMEOUT_MS=200` 是起動確認的窗口，實際 unit 起動另計）。
留給後續票。

### D13 驗證方法的硬規則：加固面一律機械導出——**機制已由 PR #677 落地，本設計消費它**

本設計早期版本把這條寫成「本票要求新增的一條硬規則」。**PR #677 已經把它做成程式**，
因此本節改為兩件事：記錄規則的**理由**（它仍然是本設計驗收面的依據），以及明確指出
planner 的驗收 **MUST 消費既有機制、MUST NOT 自行組 property 清單**。

**規則**：凡是宣稱「某 executor 在某加固剖面下可用**或不可用**」，驗證環境 MUST 由
**已落檔的 unit 機械讀出全部 property** 再複製。MUST NOT 手抄 property 子集。

**現成的機制（不要重造）**：

| 機制 | 用途 |
|---|---|
| `permgen.unit_replica_properties(unit_text, *, instance, require_hardening=True)` | 一份已落檔 unit → `systemd-run --property=` 的**完整**清單。契約是「全帶，不選」；落檔 unit 少任一加固鍵即 `UnitReplicaDriftError` 且 stdout 保持空 |
| CLI `python -m paulsha_cortex.trust_root unit-replica <unit｜->` | 上一支的命令列入口，`-` 讀 stdin（可直接接 `systemctl cat`） |
| runbook 第 4e 步的 `psc_run_under <unit 字幹> <命令>` | runbook 與測試**共用同一個「真實加固面」定義**，因此兩邊不會漂移 |
| `permgen.seccomp_filter_is_fatal(table)` | 「被過濾的 syscall 會不會殺行程」的機械答案 |
| `permgen.PROFILE_LOCKED_KEYS` | `SystemCallFilter`／`SystemCallErrorNumber` 兩份剖面逐字相同，剖面之間不分岔 |

實作票 E 的驗收矩陣 MUST 走 `psc_run_under`／`unit-replica`；任何在測試或 runbook 裡
出現的「手寫 `--property=` 清單」都是本規則要擋的東西。

**為什麼這條規則值得記在本設計裡（而不是只留在 #677）**：因為 planner 是**下一個**
會做這種宣稱的地方。規則的理由如下——

| 事故 | 手抄的偏差方向 | 產生的錯誤 | 後果 |
|---|---|---|---|
| #638 | 單 UID 環境讓 ACL 斷言真空（比 production **寬**） | **假綠** | 斷言恆真，什麼都沒驗到 |
| #657 | 同型（比 production **寬**） | **假綠** | 同上 |
| #673 原 body | 十條 property 複本漏抄 `SystemCallErrorNumber=EPERM`（比 production **嚴**） | **假紅** | 製造出 production 不存在的 rc=1，據此開票要求放寬 seccomp |
| #673 的 repro 步驟 | 同上 | **假紅** | 在「修 #643 漏抓」的名義下，第二次踩同一個坑 |

前三次都是假綠，因此很容易把這條規則記成「防止把不夠嚴的環境當成夠嚴」。
第四次證明那個記法是錯的：**手抄比 production 嚴的子集同樣有害**，而且後果更貴
——它會讓人去「修」一個不存在的問題，並在修的過程中放寬一條真的有用的加固項。
規則的正確形式因此是「驗證環境 ≠ production 環境」這件事**在結構上不可能發生**，
與偏差方向無關；`unit_replica_properties()` 的 `require_hardening=True` 預設
與 `UnitReplicaDriftError` 就是這句話的可執行形式。

**成對約束**（本次事故的直接教訓，#677 已用 `PROFILE_LOCKED_KEYS` 固化）：
`SystemCallFilter=` 與 `SystemCallErrorNumber=` 必須同進同出。只帶前者會把
「被過濾的 syscall 回 `EPERM`（呼叫方可 fallback）」偷換成
「`SECCOMP_RET_KILL_PROCESS`（行程直接死）」，而兩者的症狀（空輸出）一模一樣。
#643 當年已經記錄過這一條，#673 仍然重蹈——**已經寫下來的教訓，在下一次用手抄複本時
不會自動生效**，這正是為什麼它必須是機械規則而不是一段註解。

## 風險與緩解

| 風險 | 影響 | 緩解 |
|---|---|---|
| 以手抄 property 子集驗收 | 兩個方向都會錯：偏寬得假綠、偏嚴得假紅（後者更貴——會去「修」一個不存在的問題並放寬真的有用的加固項） | **D13**：走 PR #677 已落地的 `unit_replica_properties()`／`trust_root unit-replica`／runbook 的 `psc_run_under`，`require_hardening=True` 下少任一加固鍵即 `UnitReplicaDriftError`；不得手寫 `--property=` 清單 |
| PATH 缺口未補 | `claude`／`agy` rc=127；`codex` 安靜地跑到 0.42.0 舊版（**不會失敗**，只是產出來自舊 CLI） | 獨立前置票；R8 的驗收矩陣與 D8 的失敗診斷都強制記錄「解析到的絕對路徑 ＋ 版本字串」 |
| 憑證 RWP 第二列讓 unit 起不來 | 全部 reviewer／planner job 停擺 | 憑證檔已於 0818 落位（本票複驗）；部署順序固定為「憑證 → 產生器 → daemon-reload」；runbook 補一條起動後 `systemctl status` 的反向驗證 |
| probe 快取 fail-closed 造成「上一輪好好的、這一輪全滅」 | planning 掉進 needs_human | 這是刻意取捨（見 D5）；緩解是 D8 的拒因表讓原因當場可見，而非只留一個 `no-heterogeneous-planner` |
| 逾時後 unit 沒停乾淨 | 下一輪撞 `instance-busy`，錯誤訊息與逾時無關 | D4 要求停止後確認離開 active；建議 operator 另加寬鬆的 `RuntimeMaxSec=` 作第二層 |
| `_extract_json` 的第二候選消失（D-j） | codex 的 envelope 若污染 stdout，抽取失敗 | 實測驗證；若成立則走 U-3 的 result spool |
| 抽象層引入行為漂移 | direct 模式靜默改變 | 票 2 的驗收是「既有 planning 測試一行不改全綠」 |

## 未決問題總覽（需 operator 拍板，本票不擅自決定）

- **U-1 planning sandbox 要不要放 repo 複本。**
  (a) 維持整棵複製（現行語意，成本是每次呼叫兩次 `copytree`）；
  (b) 改成空 scratch（四個 adapter 的輸入全部已在 prompt 內：`secondary` 由
      `_planning_source_material()` 在 Manager 側讀好嵌進 prompt，questioner／integrator
      吃的是 JSON）；
  (c) 走 `job_workspace` 的 per-job clone（與 builder 同形，但 planner 不需要 git）。
  取捨：(b) 最便宜且是**收緊**（模型從「理論上讀得到 repo」變成「讀不到」），但它
  改變了現行行為——今天若有模型靠讀 sandbox 補足 context，(b) 會讓它拿不到。
  傾向 (b)，但這是行為改變，需要 operator 確認。

- **U-2 scratch 對 job 是可寫還是唯讀**（＝ D-d 的兩條路）。
  (1) 可寫 ＋ 接受「sandbox dirty 偵測」退步；
  (2) 唯讀（Manager-owned、不進 RWP）＋ 把 executor 的可寫落點指向 `PrivateTmp` 的
      私有 `/tmp` ⇒ 偵測需求消失。
  傾向 (2)，但需要逐 executor 實測「cwd 唯讀時能不能跑」（agy 的 `log_dir`、codex 的
  `-o`、claude 的 `--add-dir`）。

- **U-3 codex 的第二輸出通道要不要保留。**
  若實測顯示 job 模式下 codex 的 stdout 不可靠，是否新開一格「planning result spool」
  （形狀比照 `review-verdict-spool`：Manager-owned 目錄、job `wx` 無 `r`、收割後封口）。
  代價是**新開一條 job→Manager 的寫入面**——今天 planner 一條都沒有。

- **U-4 planner 帳號是否核可持有兩個 independence domain 的憑證。**
  這是 #668 G2 的同一題，本票不重複裁決，但它是「planner 有沒有異質性」的前提。
  選項：(a) 核可雙 domain（曝險面擴大，換得異質 planner 可用）；
  (b) 只給一份憑證 ⇒ 異質 planner 結構性不可得 ⇒ 需要另一條路徑滿足
      「不完整規格必須經異質雙模型 brainstorm」這條既有 requirement
      （例如把 secondary 派到**另一個帳號**，那是第五個 UID）。

- **U-5 `executor_credential_relpath` 要不要擴成 per-(account, executor) 表。**
  這是 U-4 的機械面。(a) 擴表（#668 B 案，產生器改動不小）；
  (b) 維持單一 ⇒ 每個帳號只有一份憑證 ⇒ U-4 只能選 (b)。
  兩者互相牽制，應一起裁決。

- **U-6 probe 快取的 TTL 值，以及要不要提供 warm-up 入口。**
  建議值 `ready=3600s`／`not_ready=300s`，但實際節奏由 operator 對 planning 頻率的
  期望決定。warm-up（部署後主動跑一次全 probe，而不是等第一次 planning 撞上）會讓
  runbook 多一步，但也讓「這台機器的 planner 到底能不能用」在部署當下就有答案，
  而不是等到第一張卡。

- **U-7 agy 狀態樹在登記表裡的表達方式。**
  (a) 把 `.gemini → cache/gemini` 登記成 symlink 類資產（登記表要新增 kind）；
  (b) 改用 env 讓 agy 的狀態樹直接落在 `cache` 底下（若 agy 支援，就不需要 symlink）。
  傾向 (b)，但需要先查 agy 的路徑解析順序。

- **U-8 模板 unit 要不要加 `RuntimeMaxSec=` 作為第二層逾時保險。**
  本設計的 D4 已在 Manager 側解決逾時；unit 側那一層只保「Manager 掛掉後不留孤兒 job」。
  加它會讓所有 job（含 builder 的長任務）共用同一個上限，值要 operator 定。

## 安全退步總覽（本設計誠實標註）

| 編號 | 退步 | 代價 | 是否可避免 |
|---|---|---|---|
| **R-1** | 「模型弄髒自己的拋棄式 sandbox」的偵測，在 job 模式下 Manager 無法執行 | 失去一個**行為訊號**（今天它會讓該次呼叫 fail-closed）；不影響任何 durable state——scratch 是一次性、job-owned、每次新建即刪 | **可避免**：選 U-2 的 (2) 就變成「結構上不可能」而非退步 |
| **R-2** | codex 的 `-o last.json` 第二輸出候選消失，`_extract_json` 從雙候選退成單候選 | 若 codex 的 stdout 被 CLI envelope 污染而 `-o` 是唯一乾淨來源，該次呼叫會落 `planning-output-malformed` | **可避免**：選 U-3 的 result spool，但那是新開一條寫入面 |
| **R-3** | planner 帳號同時持有 openai 與 google 兩個 provider 的憑證（0818 已部署的事實） | 該帳號被攻陷時兩邊 token 一起失，而 planner 正是吃 untrusted issue 內容的角色 | **不可避免**（除非放棄異質 planner）；屬 U-4，本設計只 codify 已部署的事實 |
| **R-4** | 逾時終止從「父進程 kill 子進程」變成「Manager `systemctl stop`」 | 若 unit 不理 SIGTERM 或 polkit 的 `stop` 有缺口，逾時後 job 可能仍在跑 | **部分可避免**：U-8 的 `RuntimeMaxSec=` 是第二層 |

以下**不是**退步，列出來避免被誤讀成退步：

- operator 工作樹的保護（D-e）：從「事後偵測 + fail-closed」**升級**為 mount 層不可寫。
- claude 的 hermetic config（D-g）：從「複製憑證 + 改 env」**升級**為帳號隔離
  ＋`ProtectHome=yes`。
- `_contain_operator_drift`（D-f）：範圍縮小到 direct 模式，因為 job 模式下 operator
  樹結構性不可能被 job 改；程式不刪。
