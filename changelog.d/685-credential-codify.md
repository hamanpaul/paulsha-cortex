### Fixed
- **#685（#672 票 D）：permgen 把 planner／reviewer 的憑證面 codify——`executor_credential_relpath`
  從**單一部署決定**擴成 **per-(account, executor) 表**（U-5 裁決）**。0818 的三份登入態是
  手工 `install` ＋ 手工 `ln -s` 落位的，**重跑 runbook 不會產生它們**；本票讓它可重現：
  兩軸（`permgen.CREDENTIALED_ACCOUNTS` × `EXECUTOR_CREDENTIALS`）展開成登記表資產，
  `asset_paths()`／`scaffold_directories()`／`IN_PLACE_CONTENT_WRITE_ASSETS`／unit 的
  `ReadWritePaths` 全部由它機械導出，加一格憑證不必改產生器。
  **U-4 追認**：`cortex-reviewer-planner` 同時持有 openai／google／anthropic 三份登入態為
  核可狀態，design 的安全退步 **R-3**（該帳號被攻陷時多邊 token 一起失，而 planner 正是吃
  untrusted issue 內容的角色）是**明文接受的有界殘餘風險**，在後續任何「planner 攻擊面」
  討論中不得被當成未知。
- **U-7 裁決落地：agy 的可寫狀態樹以 symlink 類資產進登記表（design 的選項 (a)）**——
  登記表新增 symlink kind（`permgen.SYMLINK_ASSETS`／`PermissionEntry.is_symlink`），命令
  形態是 `ln -sfn` ＋ `chown -h`，**刻意不出 `chmod`**：Linux 沒有 `lchmod`，而
  `chown`／`chmod` 對 symlink 一律跟著走 ⇒ 裸用會改到 `cache/gemini` **那棵樹**的 owner，
  而那棵樹歸 job 帳號正是本形狀的全部重點。守衛掛在**父目錄**（`[ ! -e <HOME> ] ||`）而
  不是自己身上——`ln` 是建立動作，「不存在就跳過」對它沒有意義，真正要跳過的是
  「本方案沒有這個帳號」。
- **驗收條件因 #686 的實測而改寫，這一條必須逐字讀。** issue 原文要求「reviewer 模板 unit
  的 RWP 逐字含憑證**檔案**」，前提是「codex 的登入態＝一個 `auth.json`」。#686 在完整
  reviewer unit 沙箱下實測推翻了那個前提：codex 需要 `$CODEX_HOME`（預設 `~/.codex`）
  **整個目錄**可寫（`state_5.sqlite`／`logs_2.sqlite`／`sessions/`／`skills/`／`plugins/`／
  `thread-writer-locks/`……檔名帶版本序號），唯讀時回
  `failed to initialize in-process app-server client: Read-only file system`，且**與 cwd
  無關**。照字面滿足原驗收會產出一個 **codex 仍然跑不起來**的部署。因此
  `cortex-reviewer-planner` 的三格改走新形狀 `CredentialShape.HOME_REDIRECT_TREE`：HOME 底下
  一條 **root-owned symlink** 導進該帳號既有的 `cache`（`~/.codex → cache/codex`、
  `~/.gemini → cache/gemini`、`~/.claude → cache/claude`），不變式換成**更強的那一條**——
  **模板 unit 的 `ReadWritePaths=` 逐字不變、零新增可寫面**（`cache` 早已在其中，
  `_minimize()` 吃掉子路徑），而 symlink 放在 root-owned 的 HOME 裡，job 換不掉指向。
- **claude 的憑證缺口一併關掉**——#686 的驗收矩陣裡 claude 是「CLI rc=0、卻回
  `Not logged in · Please run /login`」的那一列，而 **reviewer 的預設 executor 就是
  claude**：缺它時「reviewer 已降權」（#615 M2）買到的是一個跑不動的 job。新增登記表資產
  `reviewer-planner-claude-state`。job 模式下 `CLAUDE_CONFIG_DIR` 在
  `job_runner.DENIED_ENV_NAMES` 內（design D-g 的帳號隔離取代了 in-process 的一次性
  config dir），因此 claude 解到的就是 `$HOME/.claude`。
- **`deferred_run_dependencies()` 縮短一項，另兩項的理由整段換掉**——
  `reviewer-planner-executor-credential`（#640 寫「M2 落地時補第二列」、M2 早已落地 ⇒
  逾期未做）**已關閉**；#671 釘住這條逾期事實的測試按其設計意圖翻成正向形態
  （`test_the_reviewer_credential_gap_is_closed_without_widening_the_write_surface`）。
  `reviewer-planner-codex-hooks` **留著但升為 U-9**：它與 codex 的可用性在 `$CODEX_HOME`
  這一層**互斥**（要 hooks 就要有一個 job 換不掉的 root-owned 檔在一棵 job 必須整棵可寫的
  樹裡）——原本「補第二列即可」的理由已被推翻，不是「還沒補」。
  `manager-claude-credential` **留著**：U-5 解除了它的機械阻礙（表達得了了），但「要不要給
  Manager 一份模型憑證」的答案是**不要**（Manager 是 durable state owner ＋ spawn 授權
  持有者，passwd 註記逐字寫著 `no model code`），它由票 F（#687）切換後隨 direct 路徑
  一起消失，本票不預先刪掉一件還沒成立的事。
- **票 C（#684）的已知限制解除**——`planning_probe_cache._credential_path()` 跟著
  `executor_credential_of()` 的新簽章走（多一個 `executor` 參數），並改 `stat`
  **token 葉檔**而不是資產節點：`stat` 一條 symlink 只看得到目標目錄的 mtime，token
  就地覆寫時它不變 ⇒ 「憑證換了」偵測不到。agy 的 `~/.gemini` 與 claude 的 `~/.claude`
  因此**現在進得了指紋**（票 C PR body 明列的待補項）。未登記的 (account, executor)
  fail-closed，由 `compute_fingerprint` 的 `_safe` 收成穩定的
  `<unresolved:UnregisteredExecutorCredentialError>` 標記——**取不到答案本身也是一個會變的
  答案**（與 PATH 那一格同一條原則）。
- **`PSC_REVIEWER_HOME` 是本票的成對前置，不是別張票的事**——三份登入態的路徑**全部以
  `$HOME` 為根**，而模板模式下 shim 以 `os.execvpe` 整份換掉環境，unit 的
  `Environment=HOME=` 到不了模型（#686 實機更正）。`HOME` 沒宣告時它們在 job 內一條都解
  不到，而症狀（`$HOME is not defined`／`Not logged in`）與「憑證沒放好」長得一模一樣。
  新增 `permgen.JOB_HOME_ENV_BY_PRINCIPAL` 與 `PathLayout.job_home_value()`（與
  `job_runner.JOB_ROLE_CONFIG.home_env` 的成對契約由測試釘住，比照 #679 的 PATH），
  模板 unit 的憑證段直接印出 operator 要落進 EnvironmentFile 的那一行。

### Security
- **新增安全退步 R-6（明講，不是順手接受的）**：`HOME_REDIRECT_TREE` 的目標樹由 job 帳號
  擁有 ⇒ 樹裡的 token 葉檔**可被該 job 刪除或替換**（builder 的 `IN_PLACE_FILE` 擋得住
  「刪／換」，這裡擋不住）。影響面限於該帳號自己的登入態；換到的是 codex／claude
  **能不能起得來**。直接後果是同一棵樹裡**不得**再放任何 root-owned 的 enforcement 檔
  ——`reviewer-planner-codex-hooks` 因此升為 U-9。同時**修掉**一個 #640 刻意接受的代價：
  「暫存檔 ＋ rename 原子替換」形式的 refresh 在這個形狀下走得通。
- **builder 一行未改，且它的同型缺口已記錄**：`builder-executor-credential` 維持 #640 裁決
  (b)（RWP 逐字掛在檔案本身、父目錄 root-owned、runbook 第 4e-2 步的三條反向驗證不變）。
  但 #686 的量測同樣適用它——builder 在模板 unit 下跑 codex 會撞到同一條 `$CODEX_HOME`
  唯讀阻斷。改它會同時賣掉 `codex-hooks` 的 enforcement（一棵 job 擁有的樹裡放不住
  root-owned 檔），因此屬 **U-9** 的同一個裁決，本票不擅自做。

### Changed
- `docs/superpowers/runbooks/trust-root-phase2b-setup.md`：新增第 **4e-2b** 步
  （`cortex-reviewer-planner` 三份登入態的四步部署順序：骨架目標 → 遷移 0818 手動落位的
  舊目錄 → 由權限計畫落 symlink → 放 token，含跨 UID 的「換不掉指向／寫得進樹」反向驗證，
  與三個 executor 在真實加固面下的 rc 驗收）；既有的 per-account 憑證段改為 builder 專用
  並指向新步驟。**加固面一律走既有共用探針 `psc_run_under`**（property 由
  `permgen.unit_replica_properties()` 從落檔的 unit 全量導出），**未新增任何手寫的
  `--property=` 清單、未自帶 `--setenv=PATH=`**（design D13）。
