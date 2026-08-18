# 683-planning-invoker

- **#683（#672 票 B）：planning 的執行方式抽象成 `PlanningInvoker`——純重構、行為零改變**
  ——修法前 `planning_runtime._invoke_json()` 把三件事揉在同一支函式裡：**怎麼跑一個
  executor**（一次性 sandbox、`cwd`、hermetic env、逾時、雙向樹快照、drift 收容）、
  **跑之前準備什麼**（prompt 組裝）、**跑完之後怎麼解讀**（JSON 抽取與 CLI envelope 處理）。
  票 E（#686）要把第一件事換成降權 job、第二／第三件事逐字不變——沒有接縫時那次改動只能
  變成 `_invoke_json` 裡的 `if degraded:`，於是「D2 的十條防線各自在哪個模式下生效」變成
  要靠讀 `if` 才知道，而四個 adapter 與 probe 共用同一支函式，等於每個呼叫端都要重新論證
  一次自己走的是哪條路（design D1）。本票只交付接縫：
  - **新增介面**：`PlanningInvocation`（identity／prompt／purpose／timeout／worktree／
    evidence_root／run_id）、`PlanningOutcome`（returncode／stdout／stderr／output_text／
    diagnostics）、`PlanningInvoker` Protocol。
  - **切面**：`InProcessPlanningInvoker` 收下「**怎麼跑**」的全部——D2 的十條防線
    （D-a 一次性 tempdir、D-b sandbox 複本、D-c `cwd=sandbox`、D-d sandbox 弄髒即
    fail-closed、D-e operator 樹雙向快照、D-f drift 唯讀收容、D-g claude hermetic
    `CLAUDE_CONFIG_DIR`、D-h `subprocess` 逾時、D-i `capture_output`、D-j codex `-o`
    第二輸出候選）逐字搬入，一行邏輯未改。呼叫端只留「**跑之前跑之後**」：prompt 組裝、
    rc 判定、JSON 抽取。**JSON 抽取刻意留在共用層**（`_extract_json_candidates`），
    兩個 invoker 吃同一份 envelope 處理與 fail-closed 判準——本 repo 已在 #401／#516／
    #520 買過三次「同一件事兩份真相」的單。
  - **選擇點只有一個**：`_select_planning_invoker(env)` 唯一輸入是 `PSC_JOB_RUNNER`，
    與 launcher 共用 `job_runner.resolve_runner_mode()`（非法值在該函式已 fail-closed）。
    **刻意不新增 `PSC_PLANNING_INVOKER` 之類的第二個開關**——第二個開關的失效模式是
    「以為降權了、其實沒有」，而那種失敗看起來是成功的。票 B 尚無第二個實作，因此三種
    模式目前全部回 in-process，這正是「行為零改變」的意思。
  - **全部呼叫端改走接縫**：四個 adapter（`invoke_primary`／`questioner`／`secondary`／
    `integrator`）與 `_probe_identity` 走 `invoker.run()`；`probe_agy_capability()`
    過去直接吃裸 `runner`、**繞過 `_invoke_json` 的全部防線**，現改吃
    `invoker.capability_probe_runner()`——它不是 prompt 呼叫而是兩步 CLI 協定
    （`agy models` ＋ smoke），協定本身的真相留在 `model_identities`（複製一份就是第二份
    真相），invoker 只交出執行接縫，**兩次 CLI 呼叫各算一次 invocation**。direct 模式下
    該接縫就是底層 runner 本身，行為逐字不變。
  - **`subprocess.run` 在 `planning_runtime.py` 內只剩 `InProcessPlanningInvoker`
    一處**（issue #683 驗收第二條，由 AST 測試機械釘住）。
  - **唯一刻意的行為差異**（design D1 明文要求）：daemon 路徑（不注入 `runner`／`invoker`）
    現在會在建構 planning runtime 時解析 `PSC_JOB_RUNNER`，非法值 fail-closed 成
    `JobRunnerError`。launcher 早已對同一個值 fail-closed，因此不會產生新的
    「本來能跑、現在不能跑」的部署；`manager` 端會把它記成
    `planning-runtime-initialization-failed`（`environment` 級）。
  - 兩個精確度細節：`-o last.json` 的讀取條件與修法前逐字相同（只在 rc==0 且 stdout 是
    字串時才讀），否則失敗路徑上的讀取錯誤會換掉例外型別，而例外**型別名**正是
    `_probe_identity` 的 `safe-probe-failed` diagnostic 唯一內容、也是票 A
    `classify_probe_failure()` 的分類輸入；`invoker` 與 `runner` 兩個注入口互斥
    （同時給等於同一件事有兩份真相），fail-closed。
  - 測試 `tests/test_planning_invoker_672.py`（5 條）；**既有 planning／probe 測試一行
    未改、全數綠燈**——那就是「純重構」的定義，也是本票行為零改變的主要證據。
