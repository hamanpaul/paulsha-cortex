### Added
- **#629 / gate 執行身分：第四個帳號 `cortex-gate`（`UidScheme` 三分 → 四分）**——
  `#604`／PR `#628` 把 gate ledger 與 exit sentinel 的**作者**收斂到 Manager，但刻意
  沒做執行面：operator 宣告的 gate 命令（`PSC_GATE_CMD_*`）在 **builder 完全掌控內容的
  工作樹**上執行，`pytest` 會載入該樹的 `conftest.py` 與 plugin ⇒ **執行者取得任意程式碼
  執行**。後果是 `PSC_JOB_RUNNER=systemd-template` 下 build 卡恆因 `require_ledger` 而
  `gate-ledger-missing` fail closed——安全，但不能用。本次把缺的那一半補上：
  - **`permgen.FOUR_WAY_SCHEME` 成為定案方案**（`DEFAULT_SCHEME`）。既有三個帳號逐一
    皆不可承接 gate：`cortex-builder`＝模型自證（違反 `#540` 的 acceptance chain）、
    `cortex-manager`＝把任意程式碼執行引到持 spawn 授權、擁有全部 durable state 的
    帳號（`#629` 本文的整段論證）、`cortex-reviewer-planner`＝它是寫 verdict 的那一個
    （等於把 `#638`／`#639` 剛關掉的通道重新打開）。唯一認真的無新帳號候選
    `DynamicUser=yes` **結構性不成立**：本方案兩端都靠具名 POSIX ACL 授權，而
    `setfacl -m u:<名>:…` 在產生權限的當下就要解析得到那個帳號。
  - **`account_of` 支援 `ABSENT_ACCOUNT`**（語意與 `#626` 的部署決定型欄位逐字相同）：
    `three-way`／`two-way` 明示「本方案沒有 gate 角色」，因此不產生 gate 的帳號／unit／
    ACL／polkit 字幹，而**不是**讓整個產生器拒絕輸出。缺鍵（既非帳號也非明示不存在）
    仍然 fail-closed 並在訊息裡逐條列出「為什麼不能併到既有帳號」。
  - **`coordinator/gate_runner.py`（新模組）**：Manager 起 `cortex-gate-job@<id>.service`
    執行宣告的 gate，產出經 spool 回到 Manager 手上。它是**單一進入點**——自動路徑
    （`manager.terminalize_workflow_job`，排在 `_assert_terminal_gate_consistency` 正
    前方）與 operator 明著要求的 `regenerate-gates` 走同一支。
  - **登記表兩個新資產**：`gate-ledger-spool`（`<coordinator>/gate-ledger-spool/<job>/`，
    Tier-0，gate 只獲 **`wx` 無 `r`**，per-job 生命週期整條走 `#639` 的
    `coordinator/spool_slot.py`，不另寫一份）與 `gate-worktree-pool`
    （`<agents_root>/gate-worktree/`，gate-owned `0700`）。`repo-worktree` 的 readers
    補一個 `GATE`（**`rX`，沒有 `w`**）。
  - **`job_runner` 第三個 job 角色 `gate`**：沿用 `#615` 的 `JOB_ROLE_CONFIG` 表，沒有
    任何 `if role == …` 分支。`prepare_systemd_template()` 對 gate 角色**要求
    `executor=None`**（gate 不跑模型 CLI，剖面不得跟著 `PSC_MANAGER_EXECUTOR` 漂移），
    對模型角色仍**要求 `executor` 非 None**（`#643` 的既有紀律）。
  - **polkit 沿用 `#643`／`#615` 的單一交替 pattern 擴字幹**：`3 角色 × 2 剖面 ＝ 六個
    具名模板`，**不加第二條 `addRule`**，全檔仍只有一個 `return polkit.Result.YES`；
    pattern 的字幹來源改為「**本方案實際落檔**的角色」（`downgraded_job_principals()`），
    因此 `three-way`／`two-way` 產出的規則不會提一份那台機器上不存在的 unit。

### Changed
- **`work_actions._regenerate_gates_action` 的執行面收斂到 gate 執行身分（#629）**——
  它原本在 **Manager 進程內**直接呼叫 `gate_ledger.write_gate_ledger()`，等於以
  `cortex-manager` 的身分在 builder 完全掌控的工作樹上跑 `pytest`。`direct` 模式下
  builder 與 Manager 同 UID，這件事本來就沒有邊界可言；OS 隔離上線後它是一條**真的**
  提權路徑，而且是最容易被忽略的那一種（不在派工熱路徑上，只在 operator 手動救援時
  才走到）。改為與自動路徑共用 `gate_runner.run_declared_gates()`，因此不會出現「自動的
  那條降權了、手動的那條還在 Manager 進程裡跑」。降權模式下 gate 起不來時**不退回
  Manager 進程內執行**，診斷碼原樣回報。
- **`gate_ledger` 新增 `--snapshot-from`／`--publish`**：gate 命令一律在**拋棄式副本**
  上執行。唯讀不可行（`pytest` 要寫 `.pytest_cache`／`__pycache__`，`npm test`／`make`
  更是必寫，掛成唯讀只會讓每個真實 gate 以 EROFS 收場＝#629 要修掉的「安全但不能用」）；
  副本另外買到「gate 的寫入不污染 builder 交付的樹」與「快照在單一時點取得，builder
  留下的背景行程改不了跑到一半的樹」。symlink **原樣複製、絕不跟隨**（跟隨會把樹外
  內容複製進 gate 的可寫區，或走進無界遞迴）。
- **`UidScheme.headless_accounts()` 的來源改為 `registry.UNTRUSTED_EXECUTION_PRINCIPALS`**
  （headless persona ＋ headless hook ＋ **gate**），因此「Manager-owned／deployment 樹
  對這些帳號零寫入」這條核心不變式自動涵蓋 gate。同時新增
  `model_job_accounts()`——只有**跑模型 CLI** 的帳號需要 root-owned `~/.codex` 與
  executor 憑證骨架，gate 兩者都不要。

### Fixed
- **權威 gate ledger 一律由 Manager 自己重寫，spool 內容以不受信任輸入對待（#629）**——
  讓 gate 直接寫 `gate-ledger` 會被 `#628` 的 `foreign_evidence_author()` 當場以
  `gate-ledger-foreign-author` 拒掉；而那個資產**同時**是 exit sentinel 的落點，開放
  寫入面等於讓一個跑 untrusted code 的帳號能偽造**任何** job 的完成狀態。Manager 讀
  spool 時逐項驗形狀、gate 名稱必須落在 operator 的宣告集合內、payload 有界、`status`
  由 `exit_code` 重算（與採信端 `_ledger_outcomes()` 同一條紀律）。
- **產生的 job unit 檔頭不再固定寫 `--job`**：`JOB_UNIT_CLI_FLAG` 讓「重跑用哪一行」
  跟著角色走。原本 reviewer／gate 的 unit 檔頭都寫著 `unit <scheme> --job`，operator
  照抄會拿到 builder 的 unit **覆蓋掉**這一份。檔頭裡的 `Manager（cortex-svc）` 也改為
  由 scheme 導出（二分時代的字面量殘留）。

### Security
- **spec §R3 新增「誠實的取捨：gate 執行身分買到的是圍堵，不是不可偽造」**——第四帳號
  **不**使 gate 結果不可偽造：取得任意程式碼執行的 builder 依然能影響**自己那一份**
  ledger 的內容，那是「執行對方交出來的測試碼」這件事本身的性質，換誰執行都一樣。
  買到的是那個執行**碰不到**：Manager 的 durable state（含 exit sentinel）、builder 的
  工作樹（只有 `rX`）、verdict／commit 通道、他人在 gate spool 的那一格（`wx` 無 `r`）、
  以及任何 polkit grant。任何引用本 spec 的稽核或 PR 描述 MUST NOT 把它敘述成
  「gate ledger 不可被 builder 影響」。
- **spec §R9 新增族 5「gate 執行身分」**（T5.1–T5.10，含 negative control）；runbook
  對應第 8b-3 步（族 7）。runbook 另新增第 1 步的第四個帳號、5-2c／5-2d（兩份 gate
  模板 ＋ **真實加固面下**的驗證，含「拿掉 ACL 之後 build 卡必須 fail closed」的負向
  對照）、5-5 的 `PSC_GATE_*` 設定，並把全文 scheme token 由 `three-way` 改為
  `four-way`（附三分機器的升級路徑：只需建帳號、重跑權限 script、落兩份 unit ＋ 重跑
  polkit，第 3／4 步完全不動）。
