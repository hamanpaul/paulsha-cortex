### Added

- **#672：planner 降權的設計交付（spec ＋ design ＋ 實作切分計畫，零 code）**——
  planning（define／brainstorm）從來沒有被降權：`planning_runtime.py:830` `_invoke_json()`
  以 `subprocess.run`（同檔 `:1070` 的預設 runner）在**呼叫端行程內**執行，`:43`
  `_planning_argv()` 回傳的是裸 executor argv（無 `systemd-run`、無 `cortex-job-shim`、
  無模板 unit），而呼叫端是以 `User=cortex-manager` 執行的 daemon
  （`manager_daemon.py:970`／`:1241`）。`#615`（M2）建了 `cortex-reviewer-job@.service`
  並把 **reviewer** 導上去（`launcher.py:1239 _is_review_persona()`），planner 走的是
  完全不同的一條 code path。repo 自己已經在兩處把這條記成逾期項：
  `job_runner.py:405-408` 的 `JOB_ROLE_REVIEW` rationale，以及
  `permgen.deferred_run_dependencies()` 第四項——後者的 `disposition` 給的兩條路之一
  正是「裁決『Manager 不直接跑模型、planning 一律走降權 job』」，本票走那一條。

  **這是結構性搬遷，因此本票只出設計，不動一行 `paulsha_cortex/`。**
  交付：`docs/superpowers/specs/planner-job-downgrade-{spec,design}.md`、
  `docs/superpowers/plans/planner-job-downgrade.md`、
  `openspec/changes/2026-08-18-planner-job-downgrade/`（三件套 ＋
  `persona-workflow-orchestration` 的 delta）。

  設計定案的八條（R1–R8）：

  - **R1 執行身分**——降權模式下 planning 的四個 adapter 與**所有 probe** 經
    `JOB_ROLE_REVIEW` 的模板 unit 執行；`direct` 逐字不變；執行後端的選擇只有一個輸入
    （`job_runner.resolve_runner_mode`，與 launcher 同一支函式），**刻意不新增第二個
    開關**——第二個開關的失效模式是「以為降權了、其實沒有」，而那種失敗看起來是成功的。
  - **R2 一次性 sandbox 的等價重建**——把 `_invoke_json` 現行的**十條**防線逐條列成
    對應表，每條標成等價／升級／退步。operator 工作樹的保護從「事後比對雜湊 ＋
    fail-closed」**升級**為 `ProtectSystem=strict` ＋ RWP 不含 `repo-source-tree` 的
    mount 層不可寫；claude 的 hermetic config 從「複製憑證 ＋ 改 env」**升級**為帳號
    隔離 ＋ `ProtectHome=yes`。兩條**退步**逐條指名（見下）。
  - **R3 probe 快取**——`build_production_planning_runtime()` 目前每次建構都重跑全部
    probe，而 `_probe_identity` 每次做**兩次整棵 repo 的 `copytree`**、
    `probe_agy_capability` 是**兩次 CLI 呼叫**，且該函式由 periodic tick 呼叫。定案：
    Manager-owned durable 快取，指紋涵蓋 `PSC_JOB_RUNNER`／executor 可執行檔 inode／
    憑證檔／加固剖面 ＋ **模板 unit 檔本身**／roster 內容雜湊，一律 **fail-closed**
    （絕不因「上次是 ready」而在無法重探時沿用）。模板 unit 進指紋是刻意的：**任何**讓
    operator 重跑產生器落新 unit 的改動，在落檔那一刻就讓全部快取自動失效重探，
    不靠任何人記得清快取。
  - **R4 剖面路由**——剖面唯一來源是 `prepare_systemd_template(executor=…)`，planning
    側零對應表。**現行 `EXECUTOR_HARDENING_PROFILE` 實測可用**（codex／copilot → `jit`，
    claude／agy → `strict`），planner 上 job **不需要任何剖面面的前置修正**。
    本設計早期版本寫著「假設 #673 已修」，該前提已由開票者更正撤回並移除（#673 由
    **PR #677** 以「不放寬任何 syscall」收尾）：八份 unit
    （六份 job 模板 ＋ manager ＋ monitor）**全部帶 `SystemCallErrorNumber=EPERM`**
    （`cortex-reviewer-job@.service:148`、兩份 `-jit` 的 `:162`、`cortex-manager.service:89`），
    從權限產生器落地的第一個 commit 起就在；有這一條時被過濾的 syscall 回 `EPERM` 而非
    `SECCOMP_RET_KILL_PROCESS`，V8 走 fallback ⇒ codex／copilot 照常啟動。真 unit 完整
    property 集合下實測：codex／copilot 在 `jit` 剖面 rc=0，claude／agy 兩剖面皆 rc=0
    ——**預設派工路徑沒有壞**。實際被過濾的是 `pkey_alloc`（`@pkey`），不是原先猜測的
    `landlock_*`／`seccomp`（`@sandbox`）。**另消費 #677 建立的第二個維度**：seccomp
    過濾語意在剖面**之外**（`PROFILE_LOCKED_KEYS` 兩剖面逐字相同、`filtered_syscalls`／
    `filtered_syscall_surfaces()`／`seccomp_filter_is_fatal()`）——因此 probe 快取的指紋
    不能只放剖面名（要放**模板 unit 檔本身**），而 `executor-silent-exit` 的診斷要帶
    `seccomp_filter_is_fatal()` 的結果（#673 走偏正因為當時沒有任何地方回答得了它）。
  - **R5 憑證**——0818 已部署的 `.codex/auth.json`（檔 job-owned／目錄 root-owned）與
    `.gemini → cache/gemini` 在**功能上足夠**，**治理上不足**：實機 reviewer 模板 unit 的
    `ReadWritePaths` 只有 `cache` 與 `review-verdicts`，**不含憑證檔**——unit 自己的註解
    花七行描述它該怎麼掛，但登記表只有 `builder-executor-credential` 一列，產生器產不出來。
    定案沿用 #671 已建立的 `IN_PLACE_CONTENT_WRITE_ASSETS` 與
    `inapplicable_home_anchored_assets()`（後者正是把 #640 當年「不敢登記第二份憑證」的
    唯一理由拆掉的那一個），**不重造機制**。
  - **R6 錯誤語意**——三分：`planning-job-start-failed`／`planning-executor-failed`
    （含 `executor-silent-exit` 子類：rc≠0 而 stdout 與 stderr 皆空，即「連錯誤訊息都
    沒有」的那一種；本設計不對其成因預設任何特定 syscall）／`planning-output-malformed`。
    並把 `no-heterogeneous-planner` 從一個**沒有任何附加資訊的字面值**改成
    「結論 ＋ 逐候選拒因表」——`select_secondary_planner()` 今天把每個候選落選的真正理由
    用 `continue` 吃掉，#670 的 code fence 偽失敗因此被報成拓撲問題。**PR #674 已把
    probe 那一端修好**（`strip_code_fence()`／`stdout_excerpt()`／`agy models` 兩欄漂移
    造成的 100% `model-not-listed`），但那份診斷仍會在上游被吃掉——兩者剛好接得起來，
    缺任何一半都還是查不出原因。拒因表含 environment 級原因時 classification 改判
    `environment`，讓 `recover-planning` 有路（今天一律 `content` ⇒ 死路）。
    快取的 ledger 形狀沿用 PR #675 的 `not_claimable`（`schema`／`items`／
    `first_observed_at`／`observations`／條件解除自動清除／原子寫入），只在一處刻意不同：
    `not_claimable.load_ledger()` 對壞檔 raise，probe 快取**不得** raise（一份輔助紀錄
    不該取得否決整個 planning 的權力），改為「視為 miss ＋ 落可辨識診斷」，
    而 fail-closed 的實質不變——**它永遠不會因為讀不到而回答 ready**。
  - **R7 逾時**——實機 `cortex-reviewer-job@.service` 沒有 `RuntimeMaxSec=`／
    `TimeoutStartSec=`，而 `systemctl start --wait` 會一路等；定案由 Manager 側
    `wait(timeout)` → `systemctl stop` 強制終止，並確認 unit 離開 active（否則下一輪
    撞 `instance-busy`，錯誤訊息與逾時無關）。
  - **R8 驗收 ＋ D13**——凡宣稱「某 executor 在某加固剖面下**可用或不可用**」，驗證環境
    MUST 由**已落檔 unit 機械讀出全部 property** 再複製，MUST NOT 手抄子集。
    **該機制已由 PR #677 落地**（`permgen.unit_replica_properties()` 契約「全帶，不選」、
    少任一加固鍵即 `UnitReplicaDriftError` 且 stdout 保持空；CLI `trust_root unit-replica`；
    runbook 共用探針 `psc_run_under`），本設計**消費它、不重造**——實作票的矩陣不得自行組
    `--property=` 清單。成對約束（`SystemCallFilter=` 必與 `SystemCallErrorNumber=` 同進
    同出）已由 `PROFILE_LOCKED_KEYS` 固化。規則的**理由**仍記在設計裡，因為 planner 是
    下一個會做這種宣稱的地方：**判準雙向，本 repo 已有四個實例、兩個方向都出現過**：#638、#657
    手抄得比 production 寬 ⇒ **假綠**（斷言真空）；#673 原 body 與其 repro 手抄得比
    production 嚴（漏 `SystemCallErrorNumber=EPERM`）⇒ **假紅**，並據此開票要求放寬
    seccomp。假紅更貴——它會讓人去「修」一個不存在的問題，並在修的過程中放寬一條真的
    有用的加固項。因此規則的正確形式是「驗證環境 ≠ production 環境在結構上不可能」，
    與偏差方向無關。#643 早已記錄過 `SystemCallErrorNumber=EPERM` 這一條，#673 仍然
    重蹈——**已經寫下來的教訓，在下一次用手抄複本時不會自動生效**，這正是它必須變成
    機械規則而非一段註解的理由。矩陣每格另記「解析到的絕對路徑 ＋ 版本字串」。

  **本票查證時新發現、尚無票的部署缺口（一張）**：PATH 宣告缺口——`/opt/cortex/etc/cortex-manager.env`
  **沒有宣告 `PSC_REVIEWER_PATH`**，Manager unit 也沒有 `Environment=PATH=` ⇒ 降權 job
  的 PATH 就是 PID 1 的預設（`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin`），
  而 `/opt/cortex/toolchain/bin` 不在其中。實測：`codex` 解到 `/usr/bin/codex`＝
  `codex-cli 0.42.0`（toolchain 那份是 `0.147.0`），`claude`／`agy` **不存在** ⇒ rc=127。
  reviewer 模板 unit 的註解已預先寫過這條該怎麼修，只是那一行從未落到 env 檔裡。
  **這條同時影響今天的 reviewer job**，需獨立開票，且是 planner 上 job 的前置。
  `claude`／`agy` 的 rc=127 至少會失敗；**`codex` 那一種不會失敗**——它安靜地用舊 CLI
  產出結果，只有把 `resolved_binary` 與 `binary_version` 記進失敗診斷與驗收矩陣才看得見。

  **誠實標註的安全退步（4 條）**：
  (R-1) 「模型弄髒自己的拋棄式 sandbox」的偵測在 job 模式下 Manager 無法執行
  （job-owned 0700 樹進不去，而讓 job 自證違反 #628／#540）——失去的是一個**行為訊號**，
  不影響任何 durable state；選未決項 U-2 的「scratch 對 job 唯讀」可把它從退步變成
  「結構上不可能」。
  (R-2) codex 的 `-o last.json` 第二輸出候選消失，`_extract_json` 從雙候選退成單候選。
  (R-3) planner 帳號同時持有兩個 provider 的憑證（0818 已部署的事實），該帳號被攻陷時
  兩邊 token 一起失，而 planner 正是吃 untrusted issue 內容的角色。
  (R-4) 逾時終止從「父進程 kill 子進程」變成「Manager `systemctl stop`」。

  **未決裁決（8 條，交 operator，本票不擅自決定）**：sandbox 要不要放 repo 複本（U-1）、
  scratch 對 job 可寫或唯讀（U-2）、codex 第二輸出通道要不要保留（U-3）、planner 帳號
  是否核可雙 independence domain 憑證（U-4）、`executor_credential_relpath` 要不要擴成
  per-(account, executor) 表（U-5）、probe 快取 TTL 與 warm-up 入口（U-6）、agy 狀態樹
  在登記表裡的表達方式（U-7）、模板 unit 要不要加 `RuntimeMaxSec=` 第二層保險（U-8）。

  **實作切分成六張票**（A 拒因表／三分 → B invoker 抽象 → C probe 快取 → E job
  invoker → F 切換與宣稱更正；D permgen codify 與 E 平行），A／B／C **不依賴任何部署面
  改動**，可在 direct 部署上獨立 land 並各自產生價值；切換本身則是一次到位（probe 快取
  的指紋含 `PSC_JOB_RUNNER`，一個部署不能一半走 job、一半走 in-process）。票 E 的前置
  只有一張獨立的部署面票（PATH 宣告），**沒有剖面面前置**——原本另列的「runbook 驗證
  方法改機械讀取」已由 PR #677 一併完成。
